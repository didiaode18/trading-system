# -*- coding: utf-8 -*-
"""
QMT自动交易执行器 V1.0
======================
基于miniQMT(xtquant)实现全自动条件单执行
彻底消除人为干预，系统信号→自动下单

前置条件:
1. 东方财富/国金证券开通QMT权限（50万/10万门槛）
2. 安装QMT客户端，以"独立交易"模式登录
3. pip install xtquant（或从QMT安装目录复制）

使用方式:
  python qmt_trader.py          # 盘中运行，自动监控+执行
  python qmt_trader.py --test   # 测试连接，不下单

架构:
  每日条件单(daily_orders.py) → 生成orders.json → QMT执行器读取 → 盘中自动监控触发 → 下单
"""

import os
import sys
import json
import time
import datetime
import logging
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
# 供导入 trading_system 内部模块（config_local / risk / execution / utils / notify）
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "trading_system"))

# ============================================================
# 配置
# ============================================================
QMT_CONFIG = {
    # miniQMT连接配置（qmt_path/account_id 优先取 config_local.py，见 _load_local_config）
    "qmt_path": r"D:\国金QMT交易端\userdata_mini",  # QMT安装路径（根据实际修改）
    "account_id": "",       # 资金账号（首次使用时填写）
    "account_type": "STOCK",  # 股票账号

    # 风控硬约束（不可覆盖）
    # BUG-093修复: 单票集中度与risk_control口径对齐(15%)
    "max_daily_trades": 3,        # 每日最大交易笔数
    "max_single_position_pct": 15, # 单只最大仓位%（与risk_control LEADER_STOCK_MAX_RATIO=0.15对齐）
    "max_total_position_pct": 90,  # 总仓位上限%
    "min_cash_pct": 10,           # 最低现金比例%
    "stop_loss_pct": 8,           # 止损线%
    "no_manual_override": True,   # 禁止手动覆盖

    # 交易时间
    "morning_open": "09:30",
    "morning_close": "11:30",
    "afternoon_open": "13:00",
    "afternoon_close": "15:00",

    # 监控频率
    "check_interval_sec": 5,  # 每5秒检查一次条件
}

# 日志配置（FIX P2-07: 绝对路径 + rotation，杜绝随 cwd 漂移与无限增长）
_LOG_DIR = os.path.join(_PROJECT_ROOT, "trading_system", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            os.path.join(_LOG_DIR, "qmt_trader.log"),
            maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# FIX P1-13: 卖出止损限价单超过该秒数仍未成交则撤单重挂（追价），杜绝止损漏单
_REPOST_STALE_SEC = 90


def _load_local_config():
    """FIX P0-18: qmt_path/account_id 等连接参数优先从 config_local.py 读取，
    避免敏感信息硬编码进仓库；缺失则保留 QMT_CONFIG 默认值。全程降级。"""
    try:
        import config_local as _cl
        for _k in ("qmt_path", "account_id", "account_type"):
            _v = getattr(_cl, _k.upper(), None)
            if _v:
                QMT_CONFIG[_k] = _v
    except Exception:
        pass


_load_local_config()


# ============================================================
# 条件单数据结构
# ============================================================
class ConditionOrder:
    """条件单对象"""

    # FIX P0-06: 中英文键名归一映射（daily_orders 写中文键，systemic_risk 写英文键）
    _KEY_ALIASES = {
        "code": ("证券代码", "code", "stock_code"),
        "name": ("证券名称", "name"),
        "direction": ("方向", "direction", "action"),
        "trigger_price": ("触发价", "trigger_price", "price"),
        "quantity": ("数量", "quantity", "qty"),
        "order_type": ("类型", "order_type", "type"),
    }

    def __init__(self, order_dict: dict):
        def _pick(field):
            for k in self._KEY_ALIASES[field]:
                if k in order_dict and order_dict[k] not in (None, ""):
                    return order_dict[k]
            return None

        self.order_id = order_dict.get("order_id", "")
        # FIX P0-06: 全部改用 .get + 键名归一，关键字段缺失时抛 ValueError
        # 由加载方跳过单条（不再 KeyError 导致当日全部条件单加载崩溃）
        self.code = str(_pick("code") or "")
        self.name = str(_pick("name") or self.code)
        self.direction = self._norm_direction(_pick("direction"))
        self.trigger_price = float(_pick("trigger_price") or 0)
        self.quantity = int(_pick("quantity") or 0)
        _otype = _pick("order_type")
        # systemic_risk 的 action 无 order_type，按方向给默认类型
        self.order_type = str(_otype) if _otype else (
            "定价卖出" if self.direction == "卖出" else "反弹买入")
        self.valid_days = order_dict.get("有效期", order_dict.get("valid_days", "10个交易日"))
        self.trigger_time = order_dict.get("触发时间", order_dict.get("trigger_time", ""))  # 如"14:50"
        self.priority = order_dict.get("优先级", order_dict.get("priority", "★★建议"))
        self.note = order_dict.get("说明", order_dict.get("note", ""))

        # FIX P3-#17: 回落卖出/反弹买入的个性化阀值（不传则用模块默认）
        self.drawdown_pct = float(order_dict.get("回落幅度", order_dict.get("drawdown_pct", 0.05)))
        self.rebound_pct = float(order_dict.get("反弹幅度", order_dict.get("rebound_pct", 0.02)))

        # FIX P0-04: 状态字段从 JSON 读取（进程重启后恢复），不再硬编码重置
        self.triggered = bool(order_dict.get("triggered", False))
        self.submitted = bool(order_dict.get("submitted", False))   # 已报单（未确认成交）
        self.executed = bool(order_dict.get("executed", False))     # 已成交（回报/流水回填）
        self.broker_order_id = str(order_dict.get("broker_order_id", "") or "")
        self.submit_ts = float(order_dict.get("submit_ts", 0) or 0)
        self.create_date = order_dict.get("create_date", datetime.date.today().isoformat())
        # FIX P1-#3: 执行失败重试计数（防止 broker 拒单后静默丢单）
        self.retry_count = int(order_dict.get("retry_count", 0) or 0)
        self.last_error = order_dict.get("last_error", "")

        # FIX P0-06: 关键字段缺失即视为无效单，抛异常由加载方跳过并告警
        if not self.code or self.quantity <= 0 or self.direction not in ("买入", "卖出"):
            raise ValueError(
                f"条件单字段无效: code={self.code!r} qty={self.quantity} dir={self.direction!r}")

    @staticmethod
    def _norm_direction(raw) -> str:
        """把 action/direction 的多种写法归一为 买入/卖出。"""
        s = str(raw or "").strip().lower()
        if s in ("买入", "买", "buy", "b", "add"):
            return "买入"
        if s in ("卖出", "卖", "sell", "s", "reduce", "减仓"):
            return "卖出"
        return str(raw or "")

    def to_dict(self):
        return {
            "order_id": self.order_id,
            "证券代码": self.code,
            "证券名称": self.name,
            "方向": self.direction,
            "触发价": self.trigger_price,
            "数量": self.quantity,
            "类型": self.order_type,
            "有效期": self.valid_days,
            "触发时间": self.trigger_time,
            "优先级": self.priority,
            "说明": self.note,
            "回落幅度": self.drawdown_pct,
            "反弹幅度": self.rebound_pct,
            "triggered": self.triggered,
            "submitted": self.submitted,
            "executed": self.executed,
            "broker_order_id": self.broker_order_id,
            "submit_ts": self.submit_ts,
            "create_date": self.create_date,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
        }


# ============================================================
# QMT交易接口（xtquant封装）
# ============================================================
class QMTTrader:
    """
    miniQMT交易接口封装
    需要xtquant库和QMT客户端运行
    """

    def __init__(self, config: dict):
        self.config = config
        self.connected = False
        self.trader = None
        self.account = None
        self.daily_trade_count = 0
        self.today = datetime.date.today().isoformat()
        # 成交回报 + 账实对账（trade-reconciliation）
        self.callback = None   # FillCallback 实例，connect 后建立
        self.ledger = None     # TradeLedger 实例
        # FIX P0-18: 模拟模式 T+1 冻结记账（当日买入不可卖），键=code, 值=当日买入量
        self._sim_today_bought = {}
        self._sim_counter = 0
        # FIX P2-10: 持仓/资金查询短缓存，避免每笔买入重复全量查询
        self._pos_cache = None
        self._pos_cache_ts = 0.0
        self._acct_cache = None
        self._acct_cache_ts = 0.0
        self._cache_ttl = 2.0
        # FIX P2-09: 真实持仓查询是否成功（区分"空仓"与"查询失败"，供集中度 fail-safe）
        self._positions_query_ok = True

        # 尝试导入xtquant
        try:
            from xtquant import xttrader
            from xtquant import xtdata
            from xtquant.xttype import StockAccount
            self.xttrader = xttrader
            self.xtdata = xtdata
            self.StockAccount = StockAccount
            self.has_xtquant = True
            logger.info("[QMT] xtquant库加载成功")
        except ImportError:
            self.has_xtquant = False
            logger.warning("[QMT] xtquant未安装，进入模拟模式")

    def connect(self) -> bool:
        """连接QMT客户端"""
        if not self.has_xtquant:
            logger.warning("[QMT] 模拟模式：xtquant未安装")
            # FIX P0-18: 醒目横幅 + critical，杜绝模拟态被误当实盘"已执行"
            logger.critical(
                "[QMT] " + "!" * 56 + "\n"
                "  ⚠️⚠️⚠️ 当前为【模拟模式】：xtquant 未安装或 QMT 未连接 ⚠️⚠️⚠️\n"
                "  所有委托均为虚拟记录（SIM_ 前缀，source=sim），不会真实成交。\n"
                "  实盘前必须：安装 xtquant + 配置 config_local.py 的 QMT_PATH/ACCOUNT_ID。\n"
                + "!" * 56)
            self.connected = True  # 模拟连接
            self._setup_reconcile()
            return True

        try:
            qmt_path = self.config["qmt_path"]
            if not os.path.exists(qmt_path):
                logger.error(f"[QMT] 路径不存在: {qmt_path}")
                logger.info("[QMT] 请修改QMT_CONFIG中的qmt_path为你的QMT安装路径")
                return False

            # 创建交易对象
            # FIX P2-07: session_id 防同秒撞号（叠加 PID 低位），避免多实例/重启会话号冲突
            session_id = int(time.time()) * 1000 + (os.getpid() % 1000)
            self.trader = self.xttrader.XtQuantTrader(qmt_path, session_id)

            # 启动连接
            self.trader.start()

            # 建立连接
            connect_result = self.trader.connect()
            if connect_result != 0:
                logger.error(f"[QMT] 连接失败，错误码: {connect_result}")
                logger.info("[QMT] 请确保QMT客户端已以'独立交易'模式登录")
                return False

            # 创建账号对象
            account_id = self.config["account_id"]
            if not account_id:
                logger.error("[QMT] 未配置account_id，请在 config_local.py 中设置 ACCOUNT_ID（资金账号）")
                return False

            self.account = self.StockAccount(account_id)

            # 订阅账号
            subscribe_result = self.trader.subscribe(self.account)
            if subscribe_result != 0:
                logger.warning(f"[QMT] 订阅账号返回: {subscribe_result}")

            self.connected = True
            logger.info(f"[QMT] ✅ 连接成功 | 账号: {account_id}")
            self._setup_reconcile()
            return True

        except Exception as e:
            logger.error(f"[QMT] 连接异常: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        if self.trader:
            self.trader.stop()
            self.connected = False
            logger.info("[QMT] 已断开连接")

    def get_positions(self, force_refresh: bool = False) -> List[dict]:
        """查询当前持仓（FIX P2-10: 2秒缓存，避免每笔买入重复全量查询）"""
        _now_ts = time.time()
        if (not force_refresh and self._pos_cache is not None
                and _now_ts - self._pos_cache_ts < self._cache_ttl):
            return self._pos_cache

        if not self.connected:
            self._positions_query_ok = False
            return []

        if not self.has_xtquant:
            logger.info("[QMT模拟] 查询持仓")
            self._positions_query_ok = True  # 模拟空仓为合法状态
            self._pos_cache = []
            self._pos_cache_ts = _now_ts
            return []

        try:
            positions = self.trader.query_stock_positions(self.account)
            result = []
            for pos in positions:
                if pos.volume > 0:
                    result.append({
                        "code": pos.stock_code,
                        "volume": pos.volume,
                        "available": pos.can_use_volume,
                        "cost": pos.open_price,
                        "market_value": pos.market_value,
                    })
            self._positions_query_ok = True
            self._pos_cache = result
            self._pos_cache_ts = _now_ts
            return result
        except Exception as e:
            self._positions_query_ok = False  # FIX P2-09: 查询失败标记，供集中度 fail-safe
            logger.error(f"[QMT] 查询持仓失败: {e}")
            return []

    def get_account_info(self, force_refresh: bool = False) -> dict:
        """查询账户资金（FIX P2-10: 2秒缓存）"""
        _now_ts = time.time()
        if (not force_refresh and self._acct_cache is not None
                and _now_ts - self._acct_cache_ts < self._cache_ttl):
            return self._acct_cache

        if not self.connected:
            return {}

        if not self.has_xtquant:
            logger.info("[QMT模拟] 查询账户资金")
            # FIX P2-#12: 模拟模式现金从 66 元 改为 总资产的3成（~20万），
            # 避免 _risk_check 中 min_cash_pct=10% 阀值必然拒绝所有买入，
            # 使模拟盘可真正验证买入链路。市值同步调低以保证 total = cash + market_value。
            _sim_total = 710000
            _sim_cash = 200000
            _sim = {
                "total_asset": _sim_total,
                "cash": _sim_cash,
                "market_value": _sim_total - _sim_cash,
                "frozen": 0,
            }
            self._acct_cache = _sim
            self._acct_cache_ts = _now_ts
            return _sim

        try:
            asset = self.trader.query_stock_asset(self.account)
            _acct = {
                "total_asset": asset.total_asset,
                "cash": asset.cash,
                "market_value": asset.market_value,
                "frozen": asset.frozen_cash,
            }
            self._acct_cache = _acct
            self._acct_cache_ts = _now_ts
            return _acct
        except Exception as e:
            logger.error(f"[QMT] 查询资金失败: {e}")
            return {}

    def place_order(self, code: str, direction: str, price: float,
                    quantity: int, order_type: str = "限价") -> Optional[int]:
        """
        下单

        参数:
            code: 证券代码（如 588000.SH / 002558.SZ）
            direction: "买入" / "卖出"
            price: 委托价格
            quantity: 委托数量
            order_type: "限价" / "市价"

        返回: order_id 或 None（模拟模式返回 SIM_ 前缀字符串）
        """
        # FIX P0-18: 下单前置时段校验，杜绝非交易时段（实证 00:37）误下单
        if not is_trading_time():
            logger.warning(f"[QMT] 🚫 非交易时段，拒绝下单: {direction} {code} {quantity}股 @ {price}")
            return None

        # 风控检查
        if not self._risk_check(code, direction, price, quantity):
            return None

        # 转换代码格式（588000 → 588000.SH）
        stock_code = self._convert_code(code)

        if not self.has_xtquant:
            logger.info(f"[QMT模拟] 下单: {direction} {code} {quantity}股 @ {price}")
            self.daily_trade_count += 1
            # FIX P0-18: 模拟委托号加 SIM_ 前缀 + source="sim"，与柜台真实号物理隔离，
            # 杜绝 int(time.time()) 撞号污染生产台账
            self._sim_counter += 1
            sim_order_id = f"SIM_{int(time.time() * 1000)}_{self._sim_counter}"
            # FIX P0-14: 模拟 T+1 冻结记账（当日买入次日才可卖）
            if direction == "买入":
                _key = code.split(".")[0]
                self._sim_today_bought[_key] = self._sim_today_bought.get(_key, 0) + quantity
            self._record_order_to_ledger(sim_order_id, code, direction, price, quantity, source="sim")
            return sim_order_id

        try:
            from xtquant.xtconstant import (
                STOCK_BUY, STOCK_SELL,
                FIX_PRICE,  # 限价
                LATEST_PRICE,  # 市价
            )

            # 方向
            if direction == "买入":
                xt_direction = STOCK_BUY
            else:
                xt_direction = STOCK_SELL

            # 价格类型
            if order_type == "市价":
                xt_price_type = LATEST_PRICE
                price = 0  # 市价单价格填0
            else:
                xt_price_type = FIX_PRICE

            # 下单
            order_id = self.trader.order_stock(
                self.account,
                stock_code,
                xt_direction,
                quantity,
                xt_price_type,
                price,
            )

            if order_id and order_id > 0:
                self.daily_trade_count += 1
                logger.info(f"[QMT] ✅ 下单成功 | {direction} {code} {quantity}股 @ {price} | 订单号:{order_id}")
                self._record_order_to_ledger(order_id, code, direction, price, quantity)
                return order_id
            else:
                logger.error(f"[QMT] ❌ 下单失败 | {direction} {code} {quantity}股 @ {price}")
                return None

        except Exception as e:
            logger.error(f"[QMT] 下单异常: {e}")
            return None

    def cancel_order(self, order_id: int) -> bool:
        """撤单"""
        if not self.has_xtquant:
            logger.info(f"[QMT模拟] 撤单: {order_id}")
            return True

        try:
            result = self.trader.cancel_order_stock(self.account, order_id)
            return result == 0
        except Exception as e:
            logger.error(f"[QMT] 撤单失败: {e}")
            return False

    def get_realtime_price(self, code: str) -> float:
        """获取实时价格"""
        if not self.has_xtquant:
            return 0.0

        try:
            stock_code = self._convert_code(code)
            tick = self.xtdata.get_full_tick([stock_code])
            if stock_code in tick:
                return tick[stock_code]["lastPrice"]
        except Exception:
            pass
        return 0.0

    def get_realtime_prices(self, codes: List[str]) -> Dict[str, float]:
        """FIX P2-10: 批量获取实时价（单次 get_full_tick），避免逐只查询拖垮监控频率。"""
        result: Dict[str, float] = {}
        if not self.has_xtquant or not codes:
            return result
        try:
            _map = {c: self._convert_code(c) for c in codes if c}
            stock_codes = list(set(_map.values()))
            tick = self.xtdata.get_full_tick(stock_codes)
            for c, sc in _map.items():
                if sc in tick:
                    result[c] = tick[sc].get("lastPrice", 0.0)
        except Exception as e:
            logger.debug(f"[QMT] 批量取价失败(降级): {e}")
        return result

    # ---- 内部方法 ----

    def _setup_reconcile(self):
        """建立成交回报回调与台账（trade-reconciliation）。

        - 创建 TradeLedger 与 FillCallback；
        - 真实模式：register_callback 注册回报，并主动拉取当日流水兜底；
        - 模拟模式：仅建台账，供 place_order 记录委托、测试构造回报。
        全程 try/except 降级，对账链路异常绝不影响下单主流程。
        """
        try:
            recon_cfg = {}
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), "trading_system"))
                import config as _c
                recon_cfg = getattr(_c, "RECONCILE_CONFIG", {}) or {}
            except Exception:
                pass
            if not recon_cfg.get("enabled", True):
                return
            from execution.trade_ledger import TradeLedger
            from execution.fill_callback import FillCallback
            self.ledger = TradeLedger()
            self.callback = FillCallback(ledger=self.ledger)
            if self.has_xtquant and self.trader is not None and self.account is not None:
                if recon_cfg.get("register_callback", True):
                    self.trader.register_callback(self.callback)
                    logger.info("[QMT] 已注册成交回报回调 FillCallback")
                if recon_cfg.get("sync_on_connect", True):
                    sync = self.callback.sync_from_trader(self)
                    logger.info(f"[QMT] 连接后流水兜底: {sync}")
        except Exception as e:
            logger.warning(f"[QMT] 成交回报/对账初始化失败(降级，不影响交易): {e}")

    def _record_order_to_ledger(self, order_id, code, direction, price, quantity,
                                source="place_order"):
        """将委托登记入台账（成交状态由回报/流水回填，下单仅记委托）。"""
        try:
            if self.ledger is not None and order_id:
                self.ledger.record_order(
                    order_id=order_id, code=code, direction=direction,
                    order_price=price, order_qty=quantity, source=source)
        except Exception as e:
            logger.debug(f"[QMT] 委托入账失败(降级): {e}")

    def _convert_code(self, code: str) -> str:
        """转换代码格式: 588000 → 588000.SH, 002558 → 002558.SZ, 830799 → 830799.BJ"""
        if "." in code:
            return code
        # FIX P2-08: 北交所（4xxxxx / 8xxxxx / 920xxx）优先判定，避免误判 .SZ 必然废单
        if code.startswith(("4", "8")) or code.startswith("920"):
            return f"{code}.BJ"
        if code.startswith(("5", "6", "9")):
            return f"{code}.SH"
        return f"{code}.SZ"

    def _ensure_today(self):
        """FIX P0-17: 跨日重置内存计数（进程跨零点长驻时）。"""
        _today = datetime.date.today().isoformat()
        if _today != self.today:
            self.today = _today
            self.daily_trade_count = 0
            self._sim_today_bought = {}

    def _ledger_trade_count(self) -> int:
        """FIX P0-17: 当日委托笔数以台账为权威（进程重启不清零）。查询失败降级内存计数。"""
        self._ensure_today()
        try:
            if self.ledger is not None:
                rows = self.ledger.get_day_orders(self.today)
                if rows is not None:
                    return len(rows)
        except Exception as e:
            logger.debug(f"[风控] 台账笔数查询失败，降级内存计数: {e}")
        return self.daily_trade_count

    def _get_position_market_value(self, code: str) -> Optional[float]:
        """获取指定标的当前持仓市值（用于单股集中度校验）。

        FIX P2-09: 返回 Optional[float]——>=0 表示确定市值（0=确认未持有）；
        None 表示真实模式下持仓查询不可用，调用方须 fail-safe（拒绝买入）。
        代码后缀差异（588000 vs 588000.SH）统一去除后比较。
        """
        positions = self.get_positions()
        target = code.split(".")[0]
        for pos in positions:
            pos_code = str(pos.get("code", "")).split(".")[0]
            if pos_code == target:
                mv = pos.get("market_value", 0)
                if mv and mv > 0:
                    return float(mv)
                return float(pos.get("volume", 0)) * float(pos.get("cost", 0))
        # 未找到该标的：真实模式且持仓查询失败时 fail-safe（None），否则确认未持有=0
        if self.has_xtquant and not getattr(self, "_positions_query_ok", True):
            return None
        return 0.0

    def _get_available_volume(self, code: str) -> Optional[int]:
        """FIX P0-14: 查询指定标的可卖量(can_use_volume, T+1)。查不到返回 None（调用方 fail-safe）。"""
        positions = self.get_positions()
        target = code.split(".")[0]
        for pos in positions:
            pos_code = str(pos.get("code", "")).split(".")[0]
            if pos_code == target:
                avail = pos.get("available", None)
                if avail is None:
                    avail = pos.get("can_use_volume", None)
                return int(avail) if avail is not None else 0
        # 未持仓：真实模式查询失败时 None，否则 0
        if self.has_xtquant and not getattr(self, "_positions_query_ok", True):
            return None
        return 0

    def _risk_check(self, code: str, direction: str, price: float, quantity: int) -> bool:
        """风控硬约束检查

        FIX P0-#2: 补齐 QMT_CONFIG 中声明但从未使用的 max_single_position_pct /
        max_total_position_pct 校验，避免实盘下单突破单股集中度与总仓位红线。
        FIX P0-17: 买入方向追加 UnifiedRiskEngine 全量风控（regime/回撤熔断/冷却/
        暂停/池外/盈亏比），总仓上限不再固定 90%。
        降级原则：账户信息不可用时仅依赖现金检查，不阻断卖单（卖出只释放风险）。
        """
        # 1. 每日交易笔数限制（FIX P0-17: 以台账为权威，进程重启不清零）
        _day_count = self._ledger_trade_count()
        if _day_count >= self.config["max_daily_trades"]:
            logger.warning(f"[风控] 🚫 拒绝下单: 今日已委托{_day_count}笔，达到上限{self.config['max_daily_trades']}")
            return False

        # 2. 买入时检查仓位（卖出不需现金/仓位校验，只校验每日笔数）
        if direction == "买入":
            account = self.get_account_info()
            if not account:
                logger.warning("[风控] ⚠️ 账户信息不可用，买入降级为拒绝（安全默认）")
                return False

            total = account.get("total_asset", 0) or 0
            cash = account.get("cash", 0) or 0
            frozen = account.get("frozen", 0) or 0
            market_value = account.get("market_value", 0) or 0
            avail_cash = cash - frozen  # FIX P2-09: 可用现金扣除冻结
            order_amount = price * quantity

            if total <= 0:
                logger.warning("[风控] 🚫 拒绝买入: 总资产为0，账户异常")
                return False

            # 2.1 现金绝对额不足（扣冻结）
            if order_amount > avail_cash:
                logger.warning(f"[风控] 🚫 拒绝买入: 需要{order_amount:.0f}元，可用{avail_cash:.0f}元(已扣冻结{frozen:.0f})")
                return False

            # 2.2 最低现金比例（扣冻结）
            min_cash_ratio = self.config.get("min_cash_pct", 10) / 100
            if (avail_cash - order_amount) / total < min_cash_ratio:
                logger.warning(
                    f"[风控] 🚫 拒绝买入: 买入后现金比例"
                    f"{(avail_cash - order_amount) / total:.1%} < {min_cash_ratio:.0%}"
                )
                return False

            # 2.3 总仓位硬顶（绝对上限；regime 动态上限由 2.5 quick_risk_check 把关）
            max_total_ratio = self.config.get("max_total_position_pct", 90) / 100
            new_total_pos = market_value + order_amount
            if new_total_pos / total > max_total_ratio:
                logger.warning(
                    f"[风控] 🚫 拒绝买入 {code}: 买入后总仓位"
                    f"{new_total_pos / total:.1%} > {max_total_ratio:.0%}"
                )
                return False

            # 2.4 单股集中度（FIX P2-09: 默认 15 与配置一致；市值不可用时 fail-safe）
            max_single_ratio = self.config.get("max_single_position_pct", 15) / 100
            existing_mv = self._get_position_market_value(code)
            if existing_mv is None:
                logger.warning(f"[风控] 🚫 拒绝买入 {code}: 持仓市值查询不可用，集中度 fail-safe")
                return False
            new_single_pos = existing_mv + order_amount
            if new_single_pos / total > max_single_ratio:
                logger.warning(
                    f"[风控] 🚫 拒绝买入 {code}: 单股集中度"
                    f"{new_single_pos / total:.1%} > {max_single_ratio:.0%} "
                    f"(现有{existing_mv:.0f}+本次{order_amount:.0f})"
                )
                return False

            # 2.5 FIX P0-17: 接入 UnifiedRiskEngine 全量风控（regime/回撤/熔断/冷却/池/盈亏比）
            if not self._engine_risk_check(code, direction, price, quantity):
                return False

        logger.info(f"[风控] ✅ 通过 | {direction} {code} {quantity}股 @ {price}")
        return True

    def _engine_risk_check(self, code: str, direction: str, price: float, quantity: int) -> bool:
        """FIX P0-17: 买入方向接入 UnifiedRiskEngine（regime/回撤熔断/冷却/
        暂停/池外/赛道/盈亏比），替代原自实现简化校验。异常时 fail-safe 拒绝。"""
        try:
            from risk.risk_control import quick_risk_check
        except Exception as e:
            logger.error(f"[风控] quick_risk_check 导入失败，买入 fail-safe 拒绝: {e}")
            return False
        try:
            holdings = self._build_holdings_for_engine()
            account = self.get_account_info() or {}
            total_capital = account.get("total_asset", 0) or None
            signal = {
                "code": code.split(".")[0],
                "price": price,
                "shares": quantity,
                "buy_signal": True,
            }
            result = quick_risk_check(signal, holdings, total_capital=total_capital)
            if not result.get("pass", False):
                logger.warning(f"[风控] 🚫 引擎拒绝买入 {code}: {result.get('reason', '')}")
                return False
            return True
        except Exception as e:
            logger.error(f"[风控] 引擎校验异常，买入 fail-safe 拒绝 {code}: {e}")
            return False

    def _build_holdings_for_engine(self) -> dict:
        """FIX P0-17: 从 get_positions 构造 quick_risk_check 所需 holdings（键名归一）。"""
        holdings = {}
        for pos in self.get_positions():
            _code = str(pos.get("code", "")).split(".")[0]
            if not _code:
                continue
            vol = int(pos.get("volume", 0) or 0)
            cost = float(pos.get("cost", 0) or 0)
            mv = float(pos.get("market_value", 0) or 0) or vol * cost
            holdings[_code] = {
                "shares": vol,
                "buy_price": cost,
                "current_price": (mv / vol) if vol > 0 else cost,
                "market_value": mv,
            }
        return holdings


# ============================================================
# 条件单执行引擎
# ============================================================
class ConditionOrderEngine:
    """
    条件单监控执行引擎
    盘中运行，每N秒检查一次所有条件单是否触发
    """

    def __init__(self, trader: QMTTrader, orders: List[ConditionOrder]):
        self.trader = trader
        self.orders = orders
        self.executed_orders = []
        self.high_prices = {}  # 记录日内最高价（用于回落卖出）
        self.low_prices = {}   # 记录日内最低价（用于反弹买入）
        # FIX P1-11: 热加载所需文件路径与 mtime
        self._orders_file = None
        self._orders_mtime = 0.0

    def _state_file(self) -> str:
        return os.path.join(_PROJECT_ROOT, "trading_system", "output", "qmt_order_state.json")

    @staticmethod
    def _order_key(o) -> str:
        """条件单唯一键（兼容 ConditionOrder 对象与 dict）。"""
        if isinstance(o, dict):
            oid = o.get("order_id") or ""
            return str(oid) if oid else f"{o.get('证券代码', o.get('code', ''))}|{o.get('方向', o.get('action', ''))}|{o.get('触发价', o.get('trigger_price', ''))}"
        return o.order_id or f"{o.code}|{o.direction}|{o.trigger_price}"

    def load_orders_from_file(self, filepath: str, merge: bool = False):
        """从 JSON 文件加载条件单。

        FIX P0-06: 单条 schema 归一失败仅跳过并告警，不再整表 KeyError 崩溃。
        FIX P1-11: merge=True 时增量合并（保留已触发/报单状态），供盘中热加载。
        """
        if not os.path.exists(filepath):
            logger.warning(f"[引擎] 条件单文件不存在: {filepath}")
            return
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"[引擎] 条件单文件解析失败: {e}")
            return

        raw_orders = data.get("orders", [])
        loaded = []
        skipped = 0
        for o in raw_orders:
            try:
                loaded.append(ConditionOrder(o))
            except Exception as e:
                skipped += 1
                logger.warning(f"[引擎] 跳过无效条件单(schema归一失败): {e} | 原始={o}")
        if skipped:
            logger.warning(f"[引擎] 共跳过 {skipped} 条无效条件单（不影响其余执行）")
            self._notify_load_skipped(filepath, skipped)

        if merge and self.orders:
            self.orders = self._merge_orders(self.orders, loaded)
        else:
            self.orders = loaded
            self._restore_state()  # FIX P0-04: 重启续跑，恢复当日运行态

        self._orders_file = filepath
        try:
            self._orders_mtime = os.path.getmtime(filepath)
        except Exception:
            self._orders_mtime = 0.0
        logger.info(f"[引擎] 加载{len(self.orders)}条条件单（原始{len(raw_orders)}条）")

    def _notify_load_skipped(self, filepath: str, skipped: int):
        """FIX P0-06: schema 不兼容导致跳过时告警（双 schema 冲突需人工排查）。"""
        try:
            from notify.wechat_notify import send_notification
            send_notification(
                "QMT条件单加载告警",
                f"条件单文件 {os.path.basename(filepath)} 有 {skipped} 条 schema 归一失败被跳过，"
                f"请核查 orders 文件中英文键名冲突。")
        except Exception as e:
            logger.error(f"[引擎] 加载告警发送失败(留痕): {e}")

    def reload_if_changed(self):
        """FIX P1-11: 盘中检查文件 mtime，变化则增量合并热加载
        （systemic_risk 盘中减仓单的唯一落地出口）。"""
        if not self._orders_file or not os.path.exists(self._orders_file):
            return
        try:
            mt = os.path.getmtime(self._orders_file)
        except Exception:
            return
        if mt > self._orders_mtime:
            logger.info("[引擎] 检测到条件单文件更新，增量热加载...")
            self.load_orders_from_file(self._orders_file, merge=True)

    def _merge_orders(self, existing, incoming):
        """按 order_id/(code,direction,trigger_price) 合并，保留已有运行状态。"""
        merged = {}
        for o in existing:
            merged[self._order_key(o)] = o
        added = 0
        for o in incoming:
            k = self._order_key(o)
            if k in merged:
                old = merged[k]
                old.quantity = o.quantity
                old.trigger_price = o.trigger_price
                old.note = o.note
            else:
                merged[k] = o
                added += 1
        if added:
            logger.info(f"[引擎] 热加载新增 {added} 条条件单")
        return list(merged.values())

    def _restore_state(self):
        """FIX P0-04: 从状态文件恢复当日运行态（重启续跑，杜绝重复下单）。"""
        sf = self._state_file()
        if not os.path.exists(sf):
            return
        try:
            with open(sf, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return
        if st.get("date") != datetime.date.today().isoformat():
            return  # 跨日状态作废
        state_map = {self._order_key(o): o for o in st.get("orders", [])}
        restored = 0
        for o in self.orders:
            s = state_map.get(self._order_key(o))
            if s:
                o.triggered = bool(s.get("triggered", False))
                o.submitted = bool(s.get("submitted", False))
                o.executed = bool(s.get("executed", False))
                o.broker_order_id = str(s.get("broker_order_id", "") or "")
                o.submit_ts = float(s.get("submit_ts", 0) or 0)
                o.retry_count = int(s.get("retry_count", 0) or 0)
                o.last_error = s.get("last_error", "")
                restored += 1
        if restored:
            logger.info(f"[引擎] 恢复 {restored} 条条件单运行态（重启续跑，防重复下单）")

    def save_state(self):
        """FIX P0-04: 条件单运行状态落盘，进程重启后恢复 triggered/submitted/executed。"""
        try:
            from utils.file_io import atomic_json_write
            payload = {
                "date": datetime.date.today().isoformat(),
                "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "orders": [o.to_dict() for o in self.orders],
            }
            atomic_json_write(self._state_file(), payload)
        except Exception as e:
            logger.warning(f"[引擎] 状态落盘失败(降级): {e}")

    def check_and_execute(self):
        """检查所有条件单，触发则执行"""
        now = datetime.datetime.now()
        current_time = now.strftime("%H:%M")

        # FIX P2-10: 批量预取实时价（单轮一次网络调用），避免逐只查询拖垮监控频率
        _pending = [o for o in self.orders if not (o.triggered or o.executed)]
        _price_map = self.trader.get_realtime_prices([o.code for o in _pending]) if _pending else {}

        for order in self.orders:
            if order.triggered or order.executed:
                continue

            # 获取实时价格（批量优先，未命中则单只降级）
            price = _price_map.get(order.code, 0.0) or self.trader.get_realtime_price(order.code)
            if price <= 0:
                continue

            # 更新日内高低价
            self._update_high_low(order.code, price)

            # 判断是否触发
            triggered = False

            if order.order_type == "定价卖出":
                # 价格跌破触发价
                if price <= order.trigger_price:
                    triggered = True
                    logger.info(f"[触发] 定价卖出 | {order.name} 当前{price} ≤ 触发价{order.trigger_price}")

            elif order.order_type == "时间条件单":
                # 到达指定时间 + 价格条件
                if order.trigger_time and current_time >= order.trigger_time:
                    if price <= order.trigger_price:
                        triggered = True
                        logger.info(f"[触发] 时间条件单 | {order.name} {current_time}≥{order.trigger_time} 且 价格{price}≤{order.trigger_price}")

            elif order.order_type == "回落卖出":
                # FIX P3-#17: 从日内最高回落 —— 使用订单个性化 drawdown_pct（默认5%）
                high = self.high_prices.get(order.code, price)
                drawdown = (high - price) / high if high > 0 else 0
                _dd_threshold = getattr(order, "drawdown_pct", 0.05)
                if drawdown >= _dd_threshold and price <= order.trigger_price:
                    triggered = True
                    logger.info(
                        f"[触发] 回落卖出 | {order.name} 日高{high:.3f}→当前{price:.3f} "
                        f"回落{drawdown * 100:.1f}% ≥ 阀值{_dd_threshold * 100:.1f}%"
                    )

            elif order.order_type == "反弹买入":
                # FIX P3-#17: 从日内最低反弹 —— 使用订单个性化 rebound_pct（默认2%）
                low = self.low_prices.get(order.code, price)
                rebound = (price - low) / low if low > 0 else 0
                _rb_threshold = getattr(order, "rebound_pct", 0.02)
                if price <= order.trigger_price and rebound >= _rb_threshold:
                    triggered = True
                    logger.info(
                        f"[触发] 反弹买入 | {order.name} 日低{low:.3f}→当前{price:.3f} "
                        f"反弹{rebound * 100:.1f}% ≥ 阀值{_rb_threshold * 100:.1f}%"
                    )

            # 执行
            # FIX P1-#3: triggered 后置 + 失败重试
            # 原 Bug：order.triggered = True 先于 _execute_order，一旦 broker 拒单/
            # 网络抖动/涨停无法成交，下一轮扫描会因 `if order.triggered` 直接跳过，
            # 关键止损单静默丢失。现改为：仅当 _execute_order 确认下单成功后才置位；
            # 失败则累加 retry_count，达上限 3 后才放弃并告警（避免无限重试刷单）。
            if triggered:
                # FIX P0-04: 下单前查券商在途委托，防止重启/多轮触发重复下单
                if self._has_inflight_order(order.code, order.direction):
                    order.submitted = True
                    self.save_state()
                    continue
                ok = self._execute_order(order, price)
                if ok:
                    order.triggered = True
                    self.save_state()
                else:
                    order.retry_count += 1
                    _max_retry = 3
                    if order.retry_count >= _max_retry:
                        logger.error(
                            f"[执行] 🚨 {order.name}({order.code}) 连续{order.retry_count}次"
                            f"下单失败，放弃重试 | 最后错误: {order.last_error}"
                        )
                        order.triggered = True  # 标记放弃，避免后续无限重试
                        self._notify_order_failure(order)
                    else:
                        logger.warning(
                            f"[执行] ⚠️ {order.name}({order.code}) 第{order.retry_count}次"
                            f"失败，下轮重试 | 错误: {order.last_error}"
                        )
                    self.save_state()

    def _has_inflight_order(self, code: str, direction: str) -> bool:
        """FIX P0-04: 下单前查券商在途委托，防止重复下单（重启/多轮触发）。
        真实模式查 query_stock_orders；模拟/查询失败降级为 False（不阻断）。"""
        try:
            if not self.trader.has_xtquant or self.trader.trader is None:
                return False
            from execution.fill_callback import _field
            from execution.trade_ledger import (
                canonical_direction, canonical_status,
                STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED)
            target = code.split(".")[0]
            orders = self.trader.trader.query_stock_orders(self.trader.account) or []
            for od in orders:
                oid = _field(od, "order_id", "orderId")
                ocode = str(_field(od, "stock_code", "code", default="")).split(".")[0]
                odir = canonical_direction(_field(od, "order_type", "direction", default=""))
                ostatus = canonical_status(_field(od, "order_status", "status", default=""))
                if (ocode == target and odir == direction
                        and ostatus not in (STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED)):
                    logger.info(f"[引擎] {code} {direction} 已有在途委托(order={oid},状态={ostatus})，跳过重复下单")
                    return True
        except Exception as e:
            logger.debug(f"[引擎] 在途委托查询失败(降级不阻断): {e}")
        return False

    def _execute_order(self, order: ConditionOrder, current_price: float) -> bool:
        """执行条件单

        FIX P1-#3: 返回 bool 供上层判断是否需要重试；失败时写入 order.last_error。
        FIX P0-05: 拿到 order_id 仅代表"已报"(submitted)，非"已成交"(executed)；
        成交由回报/流水回填置 executed，拒单由 rejected_orders 清标志补单。
        FIX P0-14: 卖出前钳制 T+1 可卖量 min(qty, can_use_volume) 并取整百。
        """
        logger.info(f"[执行] {order.direction} {order.code} {order.name} {order.quantity}股 | 触发价{order.trigger_price} 当前{current_price}")

        qty = order.quantity
        # 卖出用触发价（限价），买入用当前价
        if order.direction == "卖出":
            exec_price = order.trigger_price
            # FIX P0-14: T+1 可卖量钳制，避免昨1000+今500卖1500整单被拒
            avail = self.trader._get_available_volume(order.code)
            if avail is not None:
                if avail <= 0:
                    order.last_error = f"可卖量为0(T+1冻结或已卖)，跳过卖出 {qty}股"
                    logger.warning(f"[执行] ⚠️ {order.name} {order.last_error}")
                    return False
                if qty > avail:
                    clamped = (avail // 100) * 100
                    if clamped <= 0:
                        order.last_error = f"可卖量{avail}不足1手，跳过卖出"
                        logger.warning(f"[执行] ⚠️ {order.name} {order.last_error}")
                        return False
                    logger.warning(f"[执行] {order.name} 卖出量 {qty}→{clamped}（T+1可卖量{avail}钳制）")
                    qty = clamped
        else:
            exec_price = current_price

        try:
            order_id = self.trader.place_order(
                code=order.code,
                direction=order.direction,
                price=exec_price,
                quantity=qty,
                order_type="限价",
            )
        except Exception as e:
            order.last_error = f"place_order异常: {e}"
            logger.error(f"[执行] ❌ 异常 | {order.name} {order.last_error}")
            return False

        if order_id:
            # FIX P0-05: 仅置 submitted（已报），executed 由成交回报/流水确认后置位
            order.submitted = True
            order.broker_order_id = str(order_id)
            order.submit_ts = time.time()
            order.quantity = qty  # 回写钳制后实际下单量
            order.last_error = ""
            self.executed_orders.append(order)
            logger.info(f"[执行] ✅ 已报单 | 委托号{order_id}（成交待回报确认）")
            return True
        else:
            order.last_error = "place_order 返回 None（风控拒绝或下单失败）"
            logger.error(f"[执行] ❌ 失败 | {order.name} {order.last_error}")
            return False

    def _notify_order_failure(self, order: ConditionOrder):
        """FIX P1-#3: 重试耗尽后告警（邮件/钉钉），避免关键止损单静默丢失

        降级原则：通知链路异常不得影响主循环，全部 try/except 包裹。
        """
        try:
            msg = (
                f"🚨 QMT条件单执行失败\n"
                f"标的: {order.name}({order.code})\n"
                f"方向: {order.direction} {order.quantity}股 @ {order.trigger_price}\n"
                f"类型: {order.order_type}\n"
                f"重试次数: {order.retry_count}\n"
                f"最后错误: {order.last_error}\n"
                f"请立即手工处理！"
            )
            logger.critical(f"[告警] {msg}")
            # FIX P0-09: 改用真实存在的统一通知入口（原 notify.dingtalk_notify 模块不存在）；
            # 告警失败本身必须留痕（error 日志 + 本地 jsonl），不再静默吞掉
            try:
                from notify.wechat_notify import send_notification
                res = send_notification("QMT条件单执行失败", msg)
                logger.info(f"[告警] 通知发送结果: {res}")
            except Exception as ne:
                logger.error(f"[告警] ❌ 通知发送失败(留痕): {ne}")
                try:
                    _fail_log = os.path.join(_LOG_DIR, "qmt_alert_failed.jsonl")
                    with open(_fail_log, "a", encoding="utf-8") as fp:
                        fp.write(json.dumps({
                            "time": datetime.datetime.now().isoformat(),
                            "order": order.to_dict(), "error": str(ne),
                        }, ensure_ascii=False) + "\n")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[告警] 失败通知处理异常(留痕): {e}")

    def consume_rejected(self):
        """FIX P0-05: 消费成交回报的拒单列表，清除 submitted 标志以便重试/补单，
        并 critical 告警。避免止损单被柜台拒后系统误认为"已执行"而静默漏单。"""
        cb = getattr(self.trader, "callback", None)
        if cb is None:
            return
        rejected = getattr(cb, "rejected_orders", None)
        if not rejected:
            return
        for oid in list(rejected):
            matched = None
            for order in self.orders:
                if order.broker_order_id and str(order.broker_order_id) == str(oid):
                    matched = order
                    break
            if matched:
                matched.submitted = False
                matched.triggered = False
                matched.broker_order_id = ""
                matched.retry_count += 1
                matched.last_error = f"柜台拒单(order={oid})"
                logger.critical(
                    f"[执行] 🚨 拒单 | {matched.name}({matched.code}) {matched.direction} "
                    f"{matched.quantity}股 委托号{oid} 被柜台拒绝，将重试/补单（第{matched.retry_count}次）")
                if matched.retry_count >= 3:
                    matched.triggered = True  # 放弃重试，避免无限刷单
                    self._notify_order_failure(matched)
            else:
                logger.critical(f"[执行] 🚨 收到拒单 order={oid}，但无匹配条件单，请人工核查！")
        cb.rejected_orders = []  # 清空已消费
        self.save_state()

    def sync_fill_status(self):
        """FIX P0-05: 盘中定时以券商流水为权威回填 submitted→executed(已成)
        或清标志(已撤/拒单)；FIX P1-13: 卖出止损限价单长时间不成交则撤单重挂。"""
        cb = getattr(self.trader, "callback", None)
        if cb is None or not self.trader.has_xtquant or self.trader.trader is None:
            return
        try:
            sync = cb.sync_from_trader(self.trader)
            if not sync.get("completed"):
                return
            from execution.trade_ledger import (
                STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED)
            for order in self.orders:
                if not order.broker_order_id or order.executed:
                    continue
                row = self.trader.ledger.get_order(order.broker_order_id) if self.trader.ledger else {}
                if not row:
                    continue
                status = row.get("status", "")
                if status == STATUS_FILLED:
                    order.executed = True
                    logger.info(f"[执行] 💰 确认成交 | {order.name} 委托号{order.broker_order_id} 成交量{row.get('filled_qty')}")
                elif status == STATUS_REJECTED:
                    order.submitted = False
                    order.triggered = False
                    order.broker_order_id = ""
                elif status == STATUS_CANCELLED:
                    order.submitted = False
                    order.triggered = False
                    order.broker_order_id = ""
                    logger.warning(f"[执行] 已撤单 | {order.name}，下轮重挂")
                else:
                    # FIX P1-13: 卖出止损限价单长时间未成交 → 撤单重挂（追价），杜绝止损漏单
                    if (order.direction == "卖出" and order.submitted and order.submit_ts > 0
                            and time.time() - order.submit_ts > _REPOST_STALE_SEC):
                        _stale = int(time.time() - order.submit_ts)
                        if self.trader.cancel_order(order.broker_order_id):
                            logger.warning(
                                f"[执行] 🔁 止损单 {order.name} 委托号{order.broker_order_id} "
                                f"{_stale}秒未成交，已撤单，下轮按最新价重挂")
                            order.submitted = False
                            order.triggered = False
                            order.broker_order_id = ""
                            order.submit_ts = 0
                        else:
                            logger.error(
                                f"[执行] 撤单失败 {order.name} 委托号{order.broker_order_id}，保持原状等待")
            self.save_state()
        except Exception as e:
            logger.debug(f"[执行] 成交状态回填异常(降级): {e}")

    def _update_high_low(self, code: str, price: float):
        """更新日内高低价"""
        if code not in self.high_prices or price > self.high_prices[code]:
            self.high_prices[code] = price
        if code not in self.low_prices or price < self.low_prices[code]:
            self.low_prices[code] = price

    def get_status(self) -> str:
        """获取引擎状态"""
        total = len(self.orders)
        triggered = sum(1 for o in self.orders if o.triggered)
        submitted = sum(1 for o in self.orders if o.submitted)
        executed = sum(1 for o in self.orders if o.executed)
        return (f"条件单: {total}条 | 已触发: {triggered} | 已报单: {submitted} | "
                f"已成交: {executed} | 今日委托: {self.trader._ledger_trade_count()}笔")


# ============================================================
# 主运行逻辑
# ============================================================
def is_trading_time() -> bool:
    """判断当前是否为交易时间（FIX P2-05: 判节假日/调休，非仅 weekday）"""
    now = datetime.datetime.now()
    # FIX P2-05: 用统一交易日历判交易日（含法定节假日与调休补班）
    try:
        from utils.trading_calendar import is_trading_day
        if not is_trading_day(now.date()):
            return False
    except Exception:
        # 日历不可用时降级为仅判周末
        if now.weekday() >= 5:
            return False

    current = now.strftime("%H:%M")
    cfg = QMT_CONFIG
    morning = cfg["morning_open"] <= current <= cfg["morning_close"]
    afternoon = cfg["afternoon_open"] <= current <= cfg["afternoon_close"]
    return morning or afternoon


_PID_FILE = os.path.join(_PROJECT_ROOT, "trading_system", "output", ".qmt_trader.pid")


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否存活（跨平台）。"""
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and code.value == STILL_ACTIVE
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def _acquire_pid_lock() -> bool:
    """FIX P0-04: 单实例 PID 锁，防止 auto_scheduler 重启拉起第二个 qmt_trader 并行下单。"""
    try:
        os.makedirs(os.path.dirname(_PID_FILE), exist_ok=True)
        if os.path.exists(_PID_FILE):
            try:
                with open(_PID_FILE, "r", encoding="utf-8") as f:
                    old_pid = int(f.read().strip() or 0)
            except Exception:
                old_pid = 0
            if old_pid and old_pid != os.getpid() and _pid_alive(old_pid):
                logger.critical(
                    f"[主程序] 🚫 检测到 qmt_trader 已在运行(PID={old_pid})，"
                    f"本实例退出以防并行下单")
                return False
            logger.warning(f"[主程序] 发现陈旧 PID 文件(PID={old_pid} 已退出)，覆盖")
        with open(_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as e:
        logger.warning(f"[主程序] PID 锁获取异常(降级继续): {e}")
        return True


def _release_pid_lock():
    try:
        if os.path.exists(_PID_FILE):
            with open(_PID_FILE, "r", encoding="utf-8") as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(_PID_FILE)
    except Exception:
        pass


def run_monitor():
    """主监控循环"""
    logger.info("=" * 60)
    logger.info("  QMT自动交易执行器 V1.0 启动")
    logger.info(f"  日期: {datetime.date.today()} | 监控间隔: {QMT_CONFIG['check_interval_sec']}秒")
    logger.info("=" * 60)

    # FIX P0-04: 单实例锁，防止多实例并行下单
    if not _acquire_pid_lock():
        return

    try:
        # 1. 连接QMT
        trader = QMTTrader(QMT_CONFIG)
        if not trader.connect():
            logger.error("[主程序] QMT连接失败，退出")
            return

        # 2. 加载今日条件单
        today_str = datetime.date.today().strftime("%Y%m%d")
        orders_file = os.path.join(
            _PROJECT_ROOT, "trading_system", "output", f"orders_{today_str}.json")

        engine = ConditionOrderEngine(trader, [])
        engine.load_orders_from_file(orders_file)

        if not engine.orders:
            logger.warning("[主程序] 无条件单可执行，请先运行 daily_orders.py 生成")
            trader.disconnect()
            return

        # 3. 显示今日计划
        logger.info(f"\n[今日计划] {len(engine.orders)}条条件单:")
        for o in engine.orders:
            logger.info(f"  {o.priority} {o.order_type} | {o.code} {o.name} | {o.direction} {o.quantity}股 @ {o.trigger_price}")

        # 4. 监控循环
        logger.info(f"\n[监控] 开始盘中监控... (Ctrl+C停止)")
        # FIX P2-07: 用轮次计数替代 int(time.time())%30，消除相位漂移导致状态日志整段丢失
        _loop = 0
        _status_every = max(1, int(30 / max(1, QMT_CONFIG["check_interval_sec"])))
        try:
            while True:
                _loop += 1
                if is_trading_time():
                    # FIX P1-11: 盘中热加载（systemic_risk 减仓单落地）
                    engine.reload_if_changed()
                    # FIX P0-05: 消费拒单 + 以流水为权威回填成交状态
                    engine.consume_rejected()
                    engine.sync_fill_status()
                    engine.check_and_execute()

                    if _loop % _status_every == 0:
                        logger.info(f"[状态] {engine.get_status()}")
                else:
                    current = datetime.datetime.now().strftime("%H:%M")
                    if current > "15:00":
                        logger.info("[主程序] 收盘，停止监控")
                        break
                    elif current < "09:30":
                        logger.info("[主程序] 未开盘，等待中...")

                time.sleep(QMT_CONFIG["check_interval_sec"])

        except KeyboardInterrupt:
            logger.info("\n[主程序] 手动停止")

        # 5. 收盘总结
        logger.info(f"\n[收盘总结] {engine.get_status()}")
        for o in engine.executed_orders:
            logger.info(
                f"  ✅ 已报单: {o.direction} {o.name} {o.quantity}股 @ {o.trigger_price} "
                f"委托号{o.broker_order_id}{' (已成交)' if o.executed else ' (成交待确认)'}")
        engine.save_state()
        trader.disconnect()
    finally:
        _release_pid_lock()


def run_test():
    """测试QMT连接"""
    logger.info("[测试] 检查QMT环境...")

    # 检查xtquant
    try:
        import xtquant
        logger.info(f"[测试] ✅ xtquant已安装 | 版本: {getattr(xtquant, '__version__', '未知')}")
    except ImportError:
        logger.warning("[测试] ❌ xtquant未安装")
        logger.info("[测试] 安装方法:")
        logger.info("  1. 从QMT安装目录找到 xtquant 文件夹")
        logger.info("  2. 复制到 Python的 Lib/site-packages/ 目录")
        logger.info("  3. 或: pip install xtquant (如果券商提供了wheel包)")
        return

    # 检查QMT路径
    qmt_path = QMT_CONFIG["qmt_path"]
    if os.path.exists(qmt_path):
        logger.info(f"[测试] ✅ QMT路径存在: {qmt_path}")
    else:
        logger.warning(f"[测试] ❌ QMT路径不存在: {qmt_path}")
        logger.info("[测试] 请修改 QMT_CONFIG['qmt_path'] 为你的QMT安装路径")

    # 尝试连接
    trader = QMTTrader(QMT_CONFIG)
    if trader.connect():
        logger.info("[测试] ✅ QMT连接成功!")
        account = trader.get_account_info()
        if account:
            logger.info(f"[测试] 总资产: {account.get('total_asset', 0):,.0f}元")
            logger.info(f"[测试] 可用资金: {account.get('cash', 0):,.0f}元")
        trader.disconnect()
    else:
        logger.warning("[测试] ❌ QMT连接失败，请确保QMT客户端已登录")


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    else:
        run_monitor()
