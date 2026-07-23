"""
事件驱动回测引擎 V2.0
======================
完整模拟A股交易环境：
- 逐日事件驱动（行情到达→策略决策→撮合执行→净值记录）
- 真实交易成本（滑点+佣金+印花税）
- T+1限制 + 涨跌停限制
- 支持自定义策略函数
- 对比沪深300基准
- 输出完整绩效报告

使用方式:
    from backtest.engine import BacktestEngineV2, run_backtest_v2
    result = run_backtest_v2(stock_codes, data_dict, initial_capital=1000000)
"""

import logging
import datetime
import pandas as pd
import numpy as np
from typing import Callable, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from backtest.broker import SimBroker, Order, CostConfig
from backtest.data_feed import DataFeed
from backtest.metrics import generate_performance_report, format_report_text

logger = logging.getLogger(__name__)


# ============================================================
# 策略函数类型定义
# ============================================================
# strategy_fn(date, data_feed, broker) -> list[Order]
StrategyFn = Callable[[str, DataFeed, SimBroker], list]


# ============================================================
# 默认策略：使用现有trend_strategy
# ============================================================

def default_strategy(date: str, feed: DataFeed, broker: SimBroker) -> list:
    """
    默认策略：复用现有trend_strategy的买卖信号
    
    逻辑：
    1. 对每只股票计算技术指标
    2. 生成买卖信号
    3. 转换为Order列表
    """
    from strategy.trend_strategy import compute_indicators, generate_strategy_signal
    from strategy.position import calc_first_batch

    orders = []

    for code in feed.stock_codes:
        # 获取历史数据
        df = feed.get_full_history(code, date)
        if df is None or len(df) < config.MA_MID:
            continue

        # 计算指标
        df = compute_indicators(df)

        # 获取当前持仓（兼容格式）
        holding = broker.get_holding_dict(code)

        # 生成信号
        signal = generate_strategy_signal(df, holding)

        # 卖出信号
        if signal.get("sell_signal") and holding:
            bar = feed.get_bar(code, date)
            sell_price = signal.get("sell_price") or (bar["close"] if bar else 0)
            if sell_price > 0:
                orders.append(Order(
                    code=code,
                    direction="sell",
                    target_shares=holding["shares"],
                    price=sell_price,
                    date=date,
                    reason=signal.get("signal_reason", "策略卖出"),
                ))

        # 买入信号（无持仓时）
        elif signal.get("buy_signal") and not holding:
            bar = feed.get_bar(code, date)
            buy_price = signal.get("buy_price") or (bar["close"] if bar else 0)
            stop_loss = signal.get("stop_loss_initial", buy_price * 0.9)

            if buy_price > 0:
                stock_type = config.get_stock_info(code).get("类型", "龙头")
                batch = calc_first_batch(buy_price, stop_loss, stock_type, broker.initial_capital)
                if batch.get("pass_risk") and batch.get("shares", 0) > 0:
                    orders.append(Order(
                        code=code,
                        direction="buy",
                        target_shares=batch["shares"],
                        price=buy_price,
                        date=date,
                        reason=signal.get("signal_reason", "策略买入"),
                    ))

    return orders


# ============================================================
# 回测引擎 V2.0
# ============================================================

class BacktestEngineV2:
    """
    事件驱动回测引擎
    
    每日流程:
    1. new_day: 解除T+1冻结
    2. 获取当日行情
    3. 调用策略函数生成订单
    4. 执行订单（含滑点+手续费）
    5. 记录净值
    """

    def __init__(self, initial_capital: float = None, cost_config: CostConfig = None):
        self.initial_capital = initial_capital or config.TOTAL_CAPITAL
        self.cost_config = cost_config or CostConfig()
        self.broker: Optional[SimBroker] = None
        self.feed: Optional[DataFeed] = None
        self.daily_values: list[dict] = []
        self.order_log: list[dict] = []

    def run(self, data_dict: dict, strategy_fn: StrategyFn = None,
            start_date: str = None, end_date: str = None,
            benchmark_code: str = None) -> dict:
        """
        运行回测
        
        参数:
            data_dict: {code: DataFrame} 历史数据
            strategy_fn: 策略函数 (默认使用trend_strategy)
            start_date: 回测开始日期
            end_date: 回测结束日期
            benchmark_code: 基准指数代码（默认沪深300）
        
        返回:
            绩效报告字典
        """
        if strategy_fn is None:
            strategy_fn = default_strategy

        # 初始化
        self.broker = SimBroker(self.initial_capital, self.cost_config)
        self.feed = DataFeed(data_dict, start_date, end_date)
        self.daily_values = []
        self.order_log = []

        if not self.feed.trading_dates:
            return {"error": "无有效交易日数据"}

        logger.info(f"回测启动: {self.feed.date_range[0]} ~ {self.feed.date_range[1]}, "
                   f"初始资金={self.initial_capital:,.0f}")

        # 逐日回测
        for date in self.feed.trading_dates:
            self._process_day(date, strategy_fn)

        # 回测结束：强制平仓
        self._force_close_all()

        # 生成报告
        return self._build_report(benchmark_code)

    def _process_day(self, date: str, strategy_fn: StrategyFn):
        """处理单个交易日"""
        # 1. 新的一天，解除T+1冻结
        self.broker.new_day(date)

        # 2. 获取当日行情
        price_dict = self.feed.get_price_dict(date)

        # 3. 更新持仓最高价
        for code, price in price_dict.items():
            self.broker.update_highest(code, price)

        # 4. 调用策略生成订单
        try:
            orders = strategy_fn(date, self.feed, self.broker)
        except Exception as e:
            logger.warning(f"策略执行异常 {date}: {e}")
            orders = []

        # 5. 执行订单（先卖后买）
        sell_orders = [o for o in orders if o.direction == "sell"]
        buy_orders = [o for o in orders if o.direction == "buy"]

        for order in sell_orders:
            bar = self.feed.get_bar(order.code, date)
            fill = self.broker.execute_sell(order, bar)
            if fill:
                self.order_log.append({
                    "date": date, "code": order.code, "action": "sell",
                    "shares": fill.shares, "price": fill.price,
                    "cost": fill.total_cost, "reason": order.reason,
                })

        for order in buy_orders:
            bar = self.feed.get_bar(order.code, date)
            fill = self.broker.execute_buy(order, bar)
            if fill:
                self.order_log.append({
                    "date": date, "code": order.code, "action": "buy",
                    "shares": fill.shares, "price": fill.price,
                    "cost": fill.total_cost, "reason": order.reason,
                })

        # 6. 记录每日净值
        total_value = self.broker.get_total_value(price_dict)
        self.daily_values.append({
            "date": date,
            "total_value": total_value,
            "cash": self.broker.cash,
            "position_count": len(self.broker.positions),
            "position_value": total_value - self.broker.cash,
        })

    def _force_close_all(self):
        """回测结束强制平仓"""
        if not self.feed.trading_dates:
            return
        final_date = self.feed.trading_dates[-1]
        for code in list(self.broker.positions.keys()):
            bar = self.feed.get_bar(code, final_date)
            if bar:
                pos = self.broker.positions[code]
                order = Order(
                    code=code, direction="sell",
                    target_shares=pos.shares,
                    price=bar["close"],
                    date=final_date,
                    reason="回测结束强制平仓",
                )
                self.broker.execute_sell(order, bar)

    def _build_report(self, benchmark_code: str = None) -> dict:
        """构建绩效报告"""
        if not self.daily_values:
            return {"error": "无回测数据"}

        df = pd.DataFrame(self.daily_values)
        equity_curve = df["total_value"]
        dates = df["date"].tolist()

        # 交易记录
        trades_df = pd.DataFrame(self.order_log) if self.order_log else pd.DataFrame()

        # 计算每笔卖出的盈亏
        if not trades_df.empty and "action" in trades_df.columns:
            # 匹配买卖计算盈亏
            trades_df = self._calc_trade_pnl(trades_df)

        # 基准数据
        benchmark_curve = None
        if benchmark_code and benchmark_code in self.feed.data_dict:
            bench_df = self.feed.data_dict[benchmark_code]
            bench_in_range = bench_df[bench_df["date"].isin(dates)]
            if not bench_in_range.empty:
                # 归一化到初始资金
                benchmark_curve = bench_in_range["close"].reset_index(drop=True)
                benchmark_curve = benchmark_curve / benchmark_curve.iloc[0] * self.initial_capital

        # 生成报告
        report = generate_performance_report(
            equity_curve=equity_curve,
            trades=trades_df,
            dates=dates,
            benchmark_curve=benchmark_curve,
            initial_capital=self.initial_capital,
        )

        # 附加信息
        report["total_commission"] = round(self.broker.total_commission, 2)
        report["total_stamp_tax"] = round(self.broker.total_stamp_tax, 2)
        report["total_cost"] = round(self.broker.total_cost_paid, 2)
        report["daily_values"] = df
        report["trades"] = trades_df

        return report

    def _calc_trade_pnl(self, trades_df: pd.DataFrame) -> pd.DataFrame:
        """计算每笔交易的盈亏"""
        # 按股票分组，匹配买卖
        pnl_records = []
        buy_records = {}  # {code: [buy_info]}

        for _, row in trades_df.iterrows():
            code = row["code"]
            if row["action"] == "buy":
                if code not in buy_records:
                    buy_records[code] = []
                buy_records[code].append({
                    "buy_date": row["date"],
                    "buy_price": row["price"],
                    "buy_shares": row["shares"],
                })
            elif row["action"] == "sell":
                if code in buy_records and buy_records[code]:
                    buy_info = buy_records[code].pop(0)
                    pnl = (row["price"] - buy_info["buy_price"]) * row["shares"]
                    pnl_pct = (row["price"] - buy_info["buy_price"]) / buy_info["buy_price"]
                    hold_days = (datetime.datetime.strptime(row["date"], "%Y-%m-%d") -
                                datetime.datetime.strptime(buy_info["buy_date"], "%Y-%m-%d")).days
                    pnl_records.append({
                        "code": code,
                        "buy_date": buy_info["buy_date"],
                        "sell_date": row["date"],
                        "buy_price": buy_info["buy_price"],
                        "sell_price": row["price"],
                        "shares": row["shares"],
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 4),
                        "hold_days": hold_days,
                        "reason": row.get("reason", ""),
                    })

        if pnl_records:
            return pd.DataFrame(pnl_records)
        return trades_df


# ============================================================
# 便捷接口
# ============================================================

def run_backtest_v2(stock_codes: list = None, data_dict: dict = None,
                    initial_capital: float = None,
                    start_date: str = None, end_date: str = None,
                    strategy_fn: StrategyFn = None,
                    benchmark_code: str = "000300") -> dict:
    """
    便捷回测接口 V2
    
    参数:
        stock_codes: 股票代码列表（默认取config.STOCK_POOL）
        data_dict: {code: DataFrame} 历史数据
        initial_capital: 初始资金
        start_date: 开始日期
        end_date: 结束日期
        strategy_fn: 自定义策略函数
        benchmark_code: 基准指数代码
    
    返回:
        绩效报告字典
    """
    if stock_codes is None:
        stock_codes = list(config.STOCK_POOL.keys())

    if data_dict is None:
        from backtest.data_feed import load_data_from_db
        data_dict = load_data_from_db(stock_codes, start_date or "2024-01-01",
                                      end_date or datetime.date.today().strftime("%Y-%m-%d"))

    engine = BacktestEngineV2(initial_capital)
    report = engine.run(data_dict, strategy_fn, start_date, end_date, benchmark_code)
    return report


# ============================================================
# 命令行入口
# ============================================================

def _simple_ma_strategy(date: str, feed: DataFeed, broker: SimBroker) -> list:
    """简单均线策略（用于测试）：MA20上穿MA60买入，下穿卖出"""
    orders = []
    for code in feed.stock_codes:
        if code == "000300":
            continue
        df = feed.get_history(code, date, lookback=70)
        if df is None or len(df) < 60:
            continue
        ma20 = df["close"].tail(20).mean()
        ma60 = df["close"].tail(60).mean()
        bar = feed.get_bar(code, date)
        if not bar:
            continue
        holding = broker.get_holding_dict(code)
        # 金叉买入
        if ma20 > ma60 and not holding:
            shares = int(broker.cash * 0.2 / bar["close"])
            shares = (shares // 100) * 100
            if shares > 0:
                orders.append(Order(code=code, direction="buy",
                                   target_shares=shares, price=bar["close"],
                                   date=date, reason="MA20>MA60"))
        # 死叉卖出
        elif ma20 < ma60 and holding:
            orders.append(Order(code=code, direction="sell",
                               target_shares=holding["shares"], price=bar["close"],
                               date=date, reason="MA20<MA60"))
    return orders


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    print("=" * 60)
    print("  回测引擎 V2.0 - 模拟数据测试")
    print("=" * 60)

    # 生成模拟数据
    np.random.seed(42)
    data_dict = {}
    codes = list(config.STOCK_POOL.keys())[:5]

    for code in codes:
        dates = pd.date_range("2024-01-01", periods=300, freq="B")
        base = 30 + np.random.random() * 120
        prices = [base]
        for i in range(1, 300):
            change = np.random.normal(0.0003, 0.02)
            prices.append(max(prices[-1] * (1 + change), 5))

        df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": [p * (1 + np.random.uniform(-0.01, 0.01)) for p in prices],
            "close": prices,
            "high": [p * (1 + abs(np.random.normal(0, 0.015))) for p in prices],
            "low": [p * (1 - abs(np.random.normal(0, 0.015))) for p in prices],
            "volume": np.random.randint(500000, 5000000, 300).astype(float),
        })
        data_dict[code] = df

    # 添加基准
    bench_prices = [4000]
    for i in range(1, 300):
        bench_prices.append(bench_prices[-1] * (1 + np.random.normal(0.0002, 0.01)))
    data_dict["000300"] = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=300, freq="B").strftime("%Y-%m-%d"),
        "open": bench_prices,
        "close": bench_prices,
        "high": [p * 1.005 for p in bench_prices],
        "low": [p * 0.995 for p in bench_prices],
        "volume": np.random.randint(500000000, 2000000000, 300).astype(float),
    })

    # 运行回测（使用简单MA策略测试引擎）
    report = run_backtest_v2(
        stock_codes=codes,
        data_dict=data_dict,
        initial_capital=1000000,
        strategy_fn=_simple_ma_strategy,
        benchmark_code="000300",
    )

    # 输出报告
    print(format_report_text(report))
    print(f"\n  交易成本: 佣金{report.get('total_commission', 0):,.0f}元 + "
          f"印花税{report.get('total_stamp_tax', 0):,.0f}元 = "
          f"合计{report.get('total_cost', 0):,.0f}元")

    # 生成HTML报告
    from backtest.report import generate_html_report
    html_path = generate_html_report(report)
    print(f"  HTML报告: {html_path}")
    print("\n[OK] 回测引擎V2测试完成")
