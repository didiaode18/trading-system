"""
升级版回测引擎 V2.0 (P1风控增强)
====================================
严格模拟A股交易规则:
- T+1: 买入次日才能卖出
- 涨跌停: 涨停不可买入，跌停不可卖出
- 手续费: 佣金万2.5（双向）+ 印花税千1（卖出）
- 滑点: 动态滑点（按流动性分级）
- 防未来函数: 信号T日收盘生成，T+1日开盘价执行

P1风控增强:
- 个股级风控: 固定止损 + ATR动态止损 + 移动止盈
- 大盘择时: 牛熊震荡三态 -> 动态仓位
- 仓位约束: 单票上限15%

使用方式:
    from quant.engine import QuantBacktestEngine
    from quant.risk_manager import RiskManager
    rm = RiskManager()
    engine = QuantBacktestEngine(initial_capital=1000000, risk_manager=rm)
    result = engine.run(data_dict, factor_engine, "2021-01-01", "2026-01-01")
"""

import logging
import datetime
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class QuantBacktestEngine:
    """
    量化回测引擎（严格A股规则）

    交易规则:
    - T+1: 当日买入不可当日卖出
    - 涨跌停: 主板±10%, 创业板±20%（简化为±10%）
    - 最小交易单位: 100股
    - 手续费: 佣金万2.5(双向) + 印花税千1(卖出)
    - 滑点: 买入+0.1%, 卖出-0.1%
    """

    def __init__(self, initial_capital: float = 1_000_000,
                 commission: float = 0.00025,
                 stamp_tax: float = 0.001,
                 slippage: float = 0.001,
                 max_positions: int = 10,
                 limit_pct: float = 0.098,
                 risk_manager=None):
        """
        参数:
            initial_capital: 初始资金
            commission: 佣金费率（双向）
            stamp_tax: 印花税（仅卖出）
            slippage: 基础滑点比例
            max_positions: 最大持仓数
            limit_pct: 涨跌停判断阈值（9.8%留容差）
            risk_manager: RiskManager实例（P1风控，None则不启用）
        """
        self.initial_capital = initial_capital
        self.commission = commission
        self.stamp_tax = stamp_tax
        self.slippage = slippage
        self.max_positions = max_positions
        self.limit_pct = limit_pct
        self.risk_manager = risk_manager

        # 状态
        self.cash = initial_capital
        self.positions = {}  # {code: {shares, buy_price, buy_date, cost}}
        self.trades = []
        self.daily_values = []
        self.pending_orders = []  # 待执行订单（T+1）
        self.market_state = "unknown"  # 当前市场状态

    def reset(self):
        """重置引擎状态"""
        self.cash = self.initial_capital
        self.positions = {}
        self.trades = []
        self.daily_values = []
        self.pending_orders = []
        self.market_state = "unknown"
        if self.risk_manager:
            self.risk_manager.reset()

    # ============================================================
    # 一、主回测循环
    # ============================================================

    def run(self, data_dict: dict, factor_engine,
            start_date: str, end_date: str,
            rebalance_days: int = 5,
            benchmark_data=None) -> dict:
        """
        运行回测

        参数:
            data_dict: {code: DataFrame} 全量日线数据
            factor_engine: FactorEngine实例
            start_date: 回测开始日期
            end_date: 回测结束日期
            rebalance_days: 调仓间隔（交易日）
            benchmark_data: 基准指数数据（用于大盘择时，DataFrame或None）

        返回:
            回测结果字典
        """
        self.reset()

        # 构建交易日历（所有股票日期的并集）
        all_dates = set()
        for code, df in data_dict.items():
            dates = df[(df["date"] >= start_date) & (df["date"] <= end_date)]["date"]
            all_dates.update(dates.tolist())
        trade_dates = sorted(all_dates)

        if len(trade_dates) < 60:
            return {"error": f"交易日不足: {len(trade_dates)}天"}

        logger.info(f"回测区间: {trade_dates[0]} ~ {trade_dates[-1]} ({len(trade_dates)}个交易日)")
        logger.info(f"参数: 资金={self.initial_capital:,.0f}, 调仓={rebalance_days}天, "
                   f"持仓上限={self.max_positions}, 佣金={self.commission:.4%}, 滑点={self.slippage:.2%}")

        # 预构建日期索引（加速查找）
        date_index = {}  # {code: {date: row_dict}}
        for code, df in data_dict.items():
            df_filtered = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
            date_index[code] = {row["date"]: row for _, row in df_filtered.iterrows()}

        # 逐日模拟
        day_counter = 0
        for i, date in enumerate(trade_dates):
            day_counter += 1

            # 1. 执行前一天的挂单（T+1执行）
            self._execute_pending_orders(date, date_index)

            # 2. P1风控检查（止损+择时）
            if self.risk_manager:
                risk_result = self.risk_manager.daily_risk_check(
                    self.positions, date_index, date, data_dict, benchmark_data
                )
                self.market_state = risk_result["market_state"]

                # 止损卖出（次日执行）
                if risk_result["stop_sells"] and i + 1 < len(trade_dates):
                    exec_date = trade_dates[i + 1]
                    for code, reason in risk_result["stop_sells"]:
                        if code in self.positions:
                            self.pending_orders.append({
                                "action": "sell",
                                "code": code,
                                "exec_date": exec_date,
                                "signal_date": date,
                                "reason": reason,
                            })

            # 3. 调仓日：生成新的交易信号
            if day_counter % rebalance_days == 1:
                self._generate_rebalance_orders(
                    date, data_dict, factor_engine, date_index, trade_dates, i
                )

            # 4. 记录每日净值
            self._record_daily(date, date_index)

            # 进度日志
            if (i + 1) % 250 == 0:
                nav = self.daily_values[-1]["nav"] if self.daily_values else 1.0
                logger.info(f"  进度: {i+1}/{len(trade_dates)}, NAV={nav:.4f}, "
                           f"持仓={len(self.positions)}只")

        # 回测结束：强制平仓
        self._force_close_all(trade_dates[-1], date_index)

        return self._build_result(trade_dates)

    # ============================================================
    # 二、调仓逻辑
    # ============================================================

    def _generate_rebalance_orders(self, date, data_dict, factor_engine,
                                    date_index, trade_dates, current_idx):
        """
        调仓日：用因子引擎选股，生成次日执行的订单

        防未来函数：只用date及之前的数据计算因子
        P1增强：受大盘择时仓位限制
        """
        # 用因子引擎对当日截面打分
        scored_df = factor_engine.score_universe(data_dict, date)
        if scored_df.empty:
            return

        # P1: 大盘择时限制持仓数
        effective_max = self.max_positions
        if self.risk_manager:
            # 熊市减少持仓数
            if self.market_state == "熊市":
                effective_max = max(3, self.max_positions // 3)
            elif self.market_state == "震荡":
                effective_max = max(5, int(self.max_positions * 0.6))

        # 选出目标持仓
        target_codes = [row["code"] for _, row in scored_df.head(effective_max).iterrows()]

        # 确定次日日期（T+1执行）
        if current_idx + 1 >= len(trade_dates):
            return
        exec_date = trade_dates[current_idx + 1]

        # 生成卖出订单（不在目标中的持仓）
        for code in list(self.positions.keys()):
            if code not in target_codes:
                self.pending_orders.append({
                    "action": "sell",
                    "code": code,
                    "exec_date": exec_date,
                    "signal_date": date,
                })

        # 生成买入订单（在目标中但未持仓的）
        current_holdings = set(self.positions.keys())
        # 扣除待卖出的
        pending_sells = set(o["code"] for o in self.pending_orders if o["action"] == "sell")
        available_slots = effective_max - len(current_holdings - pending_sells)

        if available_slots > 0:
            buy_candidates = [c for c in target_codes if c not in current_holdings]
            for code in buy_candidates[:available_slots]:
                self.pending_orders.append({
                    "action": "buy",
                    "code": code,
                    "exec_date": exec_date,
                    "signal_date": date,
                })

    # ============================================================
    # 三、订单执行（T+1日开盘价）
    # ============================================================

    def _execute_pending_orders(self, date, date_index):
        """执行当日到期的挂单"""
        remaining = []

        for order in self.pending_orders:
            if order["exec_date"] != date:
                remaining.append(order)
                continue

            code = order["code"]
            code_data = date_index.get(code, {})
            row = code_data.get(date)

            if row is None:
                continue  # 当日无数据（停牌），跳过

            open_price = row["open"]
            prev_close = self._get_prev_close(code, date, code_data)

            if open_price <= 0 or prev_close <= 0:
                continue

            # 涨跌停判断
            change_pct = (open_price - prev_close) / prev_close

            if order["action"] == "buy":
                # 涨停不可买入（开盘价已涨停）
                if change_pct >= self.limit_pct:
                    logger.debug(f"  [{code}] {date} 涨停，取消买入")
                    continue
                self._execute_buy(code, open_price, date, date_index)

            elif order["action"] == "sell":
                # 跌停不可卖出
                if change_pct <= -self.limit_pct:
                    # 跌停卖不出，延后一天
                    order["exec_date"] = self._next_date(date, code_data)
                    if order["exec_date"]:
                        remaining.append(order)
                    continue
                self._execute_sell(code, open_price, date, date_index)

        self.pending_orders = remaining

    def _execute_buy(self, code: str, price: float, date: str, date_index: dict = None):
        """执行买入（含动态滑点+手续费+仓位约束）"""
        # 动态滑点
        slip = self.slippage
        if self.risk_manager and date_index:
            slip = self.risk_manager.get_slippage(code, date_index, date, self.slippage)
        exec_price = price * (1 + slip)

        # 仓位约束：单票上限
        total_value = self.cash
        for c, p in self.positions.items():
            total_value += p["buy_price"] * p["shares"]

        if self.risk_manager:
            shares = self.risk_manager.calc_position_size(
                code, exec_price, total_value, self.positions
            )
        else:
            target_amount = self.cash / max(1, self.max_positions - len(self.positions))
            shares = int(target_amount / exec_price)
            shares = (shares // 100) * 100

        if shares <= 0:
            return

        # 手续费
        cost = exec_price * shares
        commission_fee = max(cost * self.commission, 5)  # 最低5元
        total_cost = cost + commission_fee

        if total_cost > self.cash:
            # 资金不足，减少股数
            shares = int((self.cash - 5) / exec_price)
            shares = (shares // 100) * 100
            if shares <= 0:
                return
            cost = exec_price * shares
            commission_fee = max(cost * self.commission, 5)
            total_cost = cost + commission_fee

        # 扣款
        self.cash -= total_cost

        # 记录持仓
        self.positions[code] = {
            "shares": shares,
            "buy_price": exec_price,
            "buy_date": date,
            "cost": total_cost,
        }

        # 记录交易
        self.trades.append({
            "date": date,
            "code": code,
            "action": "buy",
            "price": exec_price,
            "shares": shares,
            "amount": cost,
            "commission": commission_fee,
            "slippage_cost": price * slip * shares,
        })

    def _execute_sell(self, code: str, price: float, date: str, date_index: dict = None):
        """执行卖出（含动态滑点+手续费+印花税）"""
        if code not in self.positions:
            return

        pos = self.positions[code]
        shares = pos["shares"]

        # T+1检查：买入当日不可卖出
        if pos["buy_date"] == date:
            return

        # 动态滑点
        slip = self.slippage
        if self.risk_manager and date_index:
            slip = self.risk_manager.get_slippage(code, date_index, date, self.slippage)
        exec_price = price * (1 - slip)

        # 收入
        revenue = exec_price * shares
        commission_fee = max(revenue * self.commission, 5)
        stamp_tax_fee = revenue * self.stamp_tax
        net_revenue = revenue - commission_fee - stamp_tax_fee

        # 盈亏
        pnl = net_revenue - pos["cost"]
        pnl_pct = pnl / pos["cost"] if pos["cost"] > 0 else 0

        # 持仓天数
        try:
            hold_days = (datetime.datetime.strptime(date, "%Y-%m-%d") -
                        datetime.datetime.strptime(pos["buy_date"], "%Y-%m-%d")).days
        except Exception:
            hold_days = 0

        # 入账
        self.cash += net_revenue

        # 记录交易
        self.trades.append({
            "date": date,
            "code": code,
            "action": "sell",
            "price": exec_price,
            "shares": shares,
            "amount": revenue,
            "commission": commission_fee,
            "stamp_tax": stamp_tax_fee,
            "slippage_cost": price * slip * shares,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "hold_days": hold_days,
        })

        # 删除持仓 + 清理移动止损记录
        del self.positions[code]
        if self.risk_manager and code in self.risk_manager.trailing_highs:
            del self.risk_manager.trailing_highs[code]

    # ============================================================
    # 四、辅助函数
    # ============================================================

    def _get_prev_close(self, code, date, code_data) -> float:
        """获取前一日收盘价"""
        dates_before = [d for d in code_data.keys() if d < date]
        if not dates_before:
            return 0
        prev_date = max(dates_before)
        return code_data[prev_date]["close"]

    def _next_date(self, date, code_data) -> str:
        """获取下一个有数据的日期"""
        dates_after = [d for d in code_data.keys() if d > date]
        return min(dates_after) if dates_after else None

    def _record_daily(self, date, date_index):
        """记录每日净值"""
        total_value = self.cash
        for code, pos in self.positions.items():
            code_data = date_index.get(code, {})
            row = code_data.get(date)
            if row is not None:
                total_value += row["close"] * pos["shares"]
            else:
                total_value += pos["buy_price"] * pos["shares"]  # 停牌用买入价

        nav = total_value / self.initial_capital
        self.daily_values.append({
            "date": date,
            "total_value": total_value,
            "cash": self.cash,
            "nav": nav,
            "position_count": len(self.positions),
        })

    def _force_close_all(self, date, date_index):
        """回测结束强制平仓"""
        for code in list(self.positions.keys()):
            code_data = date_index.get(code, {})
            row = code_data.get(date)
            if row is not None:
                self._execute_sell(code, row["close"], date, date_index)
            else:
                pos = self.positions[code]
                self.cash += pos["buy_price"] * pos["shares"]
                del self.positions[code]

    def _build_result(self, trade_dates) -> dict:
        """构建回测结果"""
        if not self.daily_values:
            return {"error": "无回测数据"}

        df_nav = pd.DataFrame(self.daily_values)
        df_trades = pd.DataFrame(self.trades) if self.trades else pd.DataFrame()

        return {
            "initial_capital": self.initial_capital,
            "final_value": df_nav["total_value"].iloc[-1],
            "daily_values": df_nav,
            "trades": df_trades,
            "start_date": trade_dates[0],
            "end_date": trade_dates[-1],
            "trade_days": len(trade_dates),
            "params": {
                "commission": self.commission,
                "stamp_tax": self.stamp_tax,
                "slippage": self.slippage,
                "max_positions": self.max_positions,
            }
        }
