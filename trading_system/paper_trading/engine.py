# -*- coding: utf-8 -*-
"""
模拟盘引擎（engine）
====================
逐日事件驱动推进 paper 策略，与实盘/回测决策流程一致：
  当日收盘 → 更新行情 → 解除T+1 → 调用策略生成订单 → 撮合成交 → 记录净值。

复用而非重写：
  - 行情馈送复用 backtest/data_feed.py 的 DataFeed；
  - 撮合/滑点/手续费/印花税/T+1 全部复用 backtest/broker.py（经 SimulatorBroker 薄封装）；
  - 策略函数与回测同一签名 strategy_fn(date, feed, broker) -> list[Order]。

铁律：默认观察模式，只记录净值与建议，绝不触发任何真实下单；禁止使用未来数据。
性能：状态每次从 start_date 全量重放（保证持仓/资金连续），20 交易日 < 30 秒。
"""

import os
import sys
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from ..backtest.data_feed import DataFeed
    from ..backtest.broker import Order, CostConfig, MatchMode
except ImportError:  # pragma: no cover - 顶层包路径兼容
    from backtest.data_feed import DataFeed
    from backtest.broker import Order, CostConfig, MatchMode

try:
    from .simulator_broker import SimulatorBroker
    from .tracker import PaperTracker
except ImportError:  # pragma: no cover
    from paper_trading.simulator_broker import SimulatorBroker
    from paper_trading.tracker import PaperTracker

logger = logging.getLogger(__name__)


def _resolve_capital(config: dict) -> float:
    """初始资金优先级：config.initial_capital > 全局 TOTAL_CAPITAL > 100万。"""
    try:
        cap = float((config or {}).get("initial_capital", 0) or 0)
        if cap > 0:
            return cap
    except Exception:
        pass
    try:
        import config as _c
        cap = float(getattr(_c, "TOTAL_CAPITAL", 0) or 0)
        if cap > 0:
            return cap
    except Exception:
        pass
    return 1_000_000.0


class PaperTradingEngine:
    """单个策略的模拟盘引擎（以 strategy_name 做命名空间隔离）。"""

    def __init__(self, strategy_name: str, strategy_fn, data_dict: dict,
                 config: dict = None, start_date: str = None,
                 initial_capital: float = None, tracker: PaperTracker = None,
                 cost_config: CostConfig = None, match_price: str = "signal_price"):
        """
        参数:
            strategy_name: 策略名（命名空间）
            strategy_fn:   与回测同签名 (date, feed, broker) -> list[Order]
            data_dict:     {code: DataFrame} 历史日线（复用 DataFeed）
            config:        PAPER_TRADING_CONFIG（含 initial_capital 等）
            start_date:    模拟盘起始交易日（全量重放起点）
            initial_capital: 初始资金（None 时按 config/全局推导）
            tracker:       净值台账（None=默认库）
            cost_config:   交易成本（None=回测默认，费率与回测一致）
            match_price:   撮合价来源（signal_price/next_open/next_vwap）
        """
        self.strategy_name = strategy_name
        self.strategy_fn = strategy_fn
        self.data_dict = data_dict or {}
        self.config = config or {}
        self.start_date = start_date
        self.initial_capital = float(initial_capital or _resolve_capital(self.config))
        self.tracker = tracker or PaperTracker()
        self.match_price = match_price
        self._cost_config = cost_config
        self._day_fills = []
        self._current_date = None
        self.broker = SimulatorBroker(
            initial_capital=self.initial_capital,
            cost_config=cost_config,
            match_mode=MatchMode.REALISTIC,
            match_price=match_price,
            on_fill=self._capture_fill,
        )
        self.feed = None

    # ---- 成交捕获 ----
    def _capture_fill(self, fill):
        self._day_fills.append(fill)

    # ---- 单交易日推进 ----
    def advance(self, date: str) -> dict:
        """推进一个交易日：更新行情 → T+1解冻 → 策略信号 → 撮合 → 记录净值。

        要求 self.feed 已构建且 broker 状态处于 date 的前一交易日（run_range 保证）。
        """
        day = {"date": date, "trades": 0, "total_value": self.initial_capital,
               "nav": 1.0, "cash": self.initial_capital, "position_ratio": 0.0}
        try:
            self._current_date = date
            self._day_fills = []

            # 1. 新交易日：解除前一日 T+1 冻结（复用 broker 逻辑）
            self.broker.new_day(date)

            # 2. 当日行情
            price_dict = self.feed.get_price_dict(date)

            # 3. 更新持仓最高价（供回落止盈）
            for code, price in price_dict.items():
                self.broker.update_highest(code, price)

            # 4. 策略生成订单（与回测同签名，异常降级为空）
            try:
                orders = self.strategy_fn(date, self.feed, self.broker) or []
            except Exception as e:
                logger.warning(f"[模拟盘] {self.strategy_name} 策略执行异常 {date}: {e}")
                orders = []

            # 5. 撮合（先卖后买，复用 SimBroker 全部费率/风控）
            sell_orders = [o for o in orders if getattr(o, "direction", "") == "sell"]
            buy_orders = [o for o in orders if getattr(o, "direction", "") == "buy"]
            for order in sell_orders:
                bar = self.feed.get_bar(order.code, date)
                self.broker.execute_sell(order, bar)
            for order in buy_orders:
                bar = self.feed.get_bar(order.code, date)
                self.broker.execute_buy(order, bar)

            # 6. 记录当日成交
            for fill in self._day_fills:
                self.tracker.record_trade(
                    strategy=self.strategy_name, date=date, code=fill.code,
                    action=fill.direction, shares=fill.shares, price=fill.price,
                    cost=fill.total_cost, commission=fill.commission,
                    stamp_tax=fill.stamp_tax, reason=fill.reason)

            # 7. 记录每日净值
            total_value = self.broker.get_total_value(price_dict)
            nav = total_value / self.initial_capital if self.initial_capital else 1.0
            cash = self.broker.cash
            position_value = total_value - cash
            position_ratio = position_value / total_value if total_value else 0.0
            self.tracker.record_daily(
                strategy=self.strategy_name, date=date, total_value=total_value,
                nav=nav, cash=cash, position_value=position_value,
                position_ratio=position_ratio, trades_count=len(self._day_fills))

            day.update({
                "trades": len(self._day_fills),
                "total_value": round(total_value, 2),
                "nav": round(nav, 6),
                "cash": round(cash, 2),
                "position_ratio": round(position_ratio, 4),
            })
        except Exception as e:
            logger.warning(f"[模拟盘] {self.strategy_name} advance({date}) 异常(降级): {e}")
            day["error"] = str(e)
        return day

    # ---- 批量推进 ----
    def run_range(self, start_date: str = None, end_date: str = None) -> dict:
        """从 start_date 全量重放至 end_date，逐日 advance 并记录净值。

        每次重置 broker 状态并重放，保证持仓/资金连续（无需序列化 broker）。
        """
        result = {"strategy": self.strategy_name, "days": 0, "daily": [],
                  "final_nav": 1.0, "start": start_date, "end": end_date}
        try:
            start = start_date or self.start_date
            self.feed = DataFeed(self.data_dict, start, end_date)
            if not self.feed.trading_dates:
                result["error"] = "无可用交易日数据"
                return result
            self.broker.reset()
            for date in self.feed.trading_dates:
                day = self.advance(date)
                result["daily"].append(day)
            result["days"] = len(result["daily"])
            if result["daily"]:
                result["final_nav"] = result["daily"][-1].get("nav", 1.0)
            result["start"] = self.feed.trading_dates[0]
            result["end"] = self.feed.trading_dates[-1]
            logger.info(f"[模拟盘] {self.strategy_name} 推进{result['days']}交易日 "
                        f"({result['start']}~{result['end']}) 期末净值{result['final_nav']:.4f}")
        except Exception as e:
            logger.warning(f"[模拟盘] {self.strategy_name} run_range 异常(降级): {e}")
            result["error"] = str(e)
        return result

    def advance_to(self, date: str) -> dict:
        """推进至指定交易日（从 start_date 全量重放），供盘后每日增量调用。"""
        return self.run_range(self.start_date, date)


# ============================================================
# paper 策略注册表（供 scheduler 盘后推进 / 生命周期入口使用）
# ============================================================
PAPER_STRATEGIES = {}


def register_paper_strategy(name: str, strategy_fn, data_dict: dict = None,
                            data_loader=None, start_date: str = None,
                            initial_capital: float = None, cost_config=None,
                            match_price: str = "signal_price", config: dict = None):
    """注册一个 paper 策略实例（回测通过后调用）。

    data_dict 与 data_loader 二选一：data_loader() 返回 {code: DataFrame}，
    供盘后按最新数据重建馈送。
    """
    PAPER_STRATEGIES[name] = {
        "name": name, "strategy_fn": strategy_fn, "data_dict": data_dict,
        "data_loader": data_loader, "start_date": start_date,
        "initial_capital": initial_capital, "cost_config": cost_config,
        "match_price": match_price, "config": config,
    }
    logger.info(f"[模拟盘] 注册 paper 策略: {name}")
    return PAPER_STRATEGIES[name]


def build_engine(name: str, tracker: PaperTracker = None,
                 config: dict = None) -> PaperTradingEngine:
    """按注册信息构建引擎；未注册返回 None。"""
    reg = PAPER_STRATEGIES.get(name)
    if not reg:
        return None
    data_dict = reg.get("data_dict")
    if data_dict is None and reg.get("data_loader"):
        try:
            data_dict = reg["data_loader"]() or {}
        except Exception as e:
            logger.warning(f"[模拟盘] {name} data_loader 失败(降级): {e}")
            data_dict = {}
    cfg = reg.get("config") or config or {}
    return PaperTradingEngine(
        strategy_name=name, strategy_fn=reg["strategy_fn"], data_dict=data_dict,
        config=cfg, start_date=reg.get("start_date"),
        initial_capital=reg.get("initial_capital"), tracker=tracker,
        cost_config=reg.get("cost_config"), match_price=reg.get("match_price", "signal_price"))


def advance_all(end_date: str = None, tracker: PaperTracker = None,
                config: dict = None) -> dict:
    """推进所有已注册 paper 策略至 end_date（默认今日）。返回 {name: result}。

    开关关闭或未注册任何策略时安全 no-op，不报错。
    """
    out = {}
    try:
        cfg = config
        if cfg is None:
            try:
                import config as _c
                cfg = getattr(_c, "PAPER_TRADING_CONFIG", {}) or {}
            except Exception:
                cfg = {}
        if not cfg.get("enabled", False):
            logger.info("[模拟盘] PAPER_TRADING_CONFIG.enabled=False，跳过推进")
            return out
        end = end_date or datetime.date.today().isoformat()
        tracker = tracker or PaperTracker()
        for name in list(PAPER_STRATEGIES.keys()):
            eng = build_engine(name, tracker=tracker, config=cfg)
            if eng is None:
                continue
            out[name] = eng.advance_to(end)
    except Exception as e:
        logger.warning(f"[模拟盘] advance_all 异常(降级): {e}")
    return out
