"""
回测引擎模块
==============
基于历史数据验证交易策略的胜率、收益、回撤等指标

核心功能:
- 模拟每日运行策略信号，按信号执行买卖
- 记录每笔交易的买入价、卖出价、持仓天数、盈亏
- 输出回测报告：总收益率、年化收益率、最大回撤、夏普比率、胜率、盈亏比
- 支持参数优化模式

使用方式:
    from backtest.backtest_engine import run_backtest
    result = run_backtest(stock_codes, data_dict, initial_capital=1000000)
"""

import pandas as pd
import numpy as np
import logging
import datetime
import sys
import os
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategy.trend_strategy import compute_indicators, generate_strategy_signal
from strategy.position import calc_first_batch

logger = logging.getLogger(__name__)


# ============================================================
# 一、回测核心
# ============================================================

class BacktestEngine:
    """回测引擎"""

    def __init__(self, initial_capital: float = None):
        self.initial_capital = initial_capital or config.TOTAL_CAPITAL
        self.cash = self.initial_capital
        self.positions = {}  # {code: {"shares": int, "buy_price": float, "buy_date": str}}
        self.trades = []     # 交易记录
        self.daily_values = []  # 每日净值记录
        self.max_capital = self.initial_capital
        self.max_drawdown = 0

    def _get_total_value(self, price_dict: dict) -> float:
        """计算总资产（现金+持仓市值）"""
        total = self.cash
        for code, pos in self.positions.items():
            price = price_dict.get(code, pos["buy_price"])
            total += pos["shares"] * price
        return total

    def _record_daily(self, date: str, price_dict: dict):
        """记录每日净值"""
        total = self._get_total_value(price_dict)
        self.daily_values.append({
            "date": date,
            "total_value": total,
            "cash": self.cash,
            "position_count": len(self.positions)
        })

        # 更新最大回撤
        if total > self.max_capital:
            self.max_capital = total
        drawdown = (self.max_capital - total) / self.max_capital
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

    def _execute_buy(self, code: str, price: float, shares: int, date: str):
        """执行买入"""
        cost = price * shares
        if cost > self.cash:
            # 资金不足，按可用资金买入
            shares = int(self.cash / price)
            shares = (shares // 100) * 100
            if shares == 0:
                return
            cost = price * shares

        self.cash -= cost
        if code in self.positions:
            # 加仓：计算均价
            old = self.positions[code]
            total_shares = old["shares"] + shares
            avg_price = (old["buy_price"] * old["shares"] + price * shares) / total_shares
            self.positions[code] = {
                "shares": total_shares,
                "buy_price": round(avg_price, 3),
                "buy_date": date
            }
        else:
            self.positions[code] = {
                "shares": shares,
                "buy_price": price,
                "buy_date": date
            }

        self.trades.append({
            "date": date,
            "code": code,
            "action": "buy",
            "price": price,
            "shares": shares,
            "amount": cost
        })

    def _execute_sell(self, code: str, price: float, date: str,
                      sell_ratio: float = 1.0):
        """执行卖出"""
        if code not in self.positions:
            return

        pos = self.positions[code]
        sell_shares = int(pos["shares"] * sell_ratio)
        sell_shares = (sell_shares // 100) * 100
        if sell_shares == 0:
            sell_shares = pos["shares"]  # 至少卖出全部

        revenue = price * sell_shares
        self.cash += revenue

        pnl = (price - pos["buy_price"]) * sell_shares
        pnl_pct = (price - pos["buy_price"]) / pos["buy_price"]
        hold_days = (datetime.datetime.strptime(date, "%Y-%m-%d") -
                     datetime.datetime.strptime(pos["buy_date"], "%Y-%m-%d")).days

        self.trades.append({
            "date": date,
            "code": code,
            "action": "sell",
            "price": price,
            "shares": sell_shares,
            "amount": revenue,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "hold_days": hold_days
        })

        if sell_ratio >= 1.0:
            del self.positions[code]
        else:
            self.positions[code]["shares"] -= sell_shares

    def run(self, stock_codes: list, data_dict: dict,
            start_date: str = None, end_date: str = None) -> dict:
        """
        运行回测
        
        参数:
            stock_codes: 股票代码列表
            data_dict: {code: DataFrame} 历史日线数据
            start_date: 回测开始日期
            end_date: 回测结束日期
        
        返回:
            回测结果字典
        """
        if not data_dict:
            return {"error": "无数据"}

        # 确定回测日期范围
        all_dates = set()
        for code, df in data_dict.items():
            for d in df["date"]:
                all_dates.add(d)

        all_dates = sorted(all_dates)
        if start_date:
            all_dates = [d for d in all_dates if d >= start_date]
        if end_date:
            all_dates = [d for d in all_dates if d <= end_date]

        if len(all_dates) < config.MA_MID:
            return {"error": f"数据不足，需要至少{config.MA_MID}个交易日"}

        logger.info(f"回测区间: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}个交易日)")

        # 逐日模拟
        for date in all_dates:
            price_dict = {}
            for code in stock_codes:
                df = data_dict.get(code)
                if df is None:
                    continue
                row = df[df["date"] == date]
                if not row.empty:
                    price_dict[code] = row.iloc[0]["close"]

            # 对每只股票检查信号
            for code in stock_codes:
                df = data_dict.get(code)
                if df is None:
                    continue

                # 截取到当前日期的数据
                df_up_to_date = df[df["date"] <= date].copy()
                if len(df_up_to_date) < config.MA_MID:
                    continue

                df_up_to_date = compute_indicators(df_up_to_date)
                holding = self.positions.get(code)

                signal = generate_strategy_signal(df_up_to_date, holding)

                # 执行卖出
                if signal["sell_signal"] and code in self.positions:
                    sell_price = signal.get("sell_price", price_dict.get(code, 0))
                    if sell_price > 0:
                        self._execute_sell(code, sell_price, date)

                # 执行买入（无持仓时）
                if signal["buy_signal"] and code not in self.positions:
                    buy_price = signal.get("buy_price", price_dict.get(code, 0))
                    stop_loss = signal.get("stop_loss_initial", buy_price * 0.9)
                    if buy_price > 0:
                        stock_type = config.get_stock_info(code).get("类型", "龙头")
                        batch = calc_first_batch(buy_price, stop_loss, stock_type, self.initial_capital)
                        if batch["pass_risk"] and batch["shares"] > 0:
                            self._execute_buy(code, buy_price, batch["shares"], date)

            # 记录每日净值
            self._record_daily(date, price_dict)

        # 强制平仓（回测结束时）
        if self.positions:
            final_date = all_dates[-1]
            for code in list(self.positions.keys()):
                df = data_dict.get(code)
                if df is not None:
                    last_price = df.iloc[-1]["close"]
                    self._execute_sell(code, last_price, final_date)

        return self._generate_report(all_dates)

    def _generate_report(self, all_dates: list) -> dict:
        """生成回测报告"""
        if not self.daily_values:
            return {"error": "无回测数据"}

        df = pd.DataFrame(self.daily_values)
        trades_df = pd.DataFrame(self.trades) if self.trades else pd.DataFrame()

        # 基本指标
        final_value = df["total_value"].iloc[-1]
        total_return = (final_value - self.initial_capital) / self.initial_capital

        # 年化收益率
        days = len(all_dates)
        annual_return = (1 + total_return) ** (252 / max(days, 1)) - 1

        # 最大回撤
        cummax = df["total_value"].cummax()
        drawdown = (cummax - df["total_value"]) / cummax
        max_drawdown = drawdown.max()

        # 夏普比率（假设无风险利率3%）
        daily_returns = df["total_value"].pct_change().dropna()
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() - 0.03 / 252) / daily_returns.std() * np.sqrt(252)
        else:
            sharpe = 0

        # 交易统计
        sell_trades = trades_df[trades_df["action"] == "sell"] if not trades_df.empty else pd.DataFrame()
        total_trades = len(sell_trades)
        win_trades = len(sell_trades[sell_trades["pnl"] > 0]) if total_trades > 0 else 0
        lose_trades = total_trades - win_trades
        win_rate = win_trades / total_trades if total_trades > 0 else 0

        avg_win = sell_trades[sell_trades["pnl"] > 0]["pnl"].mean() if win_trades > 0 else 0
        avg_lose = abs(sell_trades[sell_trades["pnl"] < 0]["pnl"].mean()) if lose_trades > 0 else 1
        profit_factor = avg_win / avg_lose if avg_lose > 0 else 0

        avg_hold_days = sell_trades["hold_days"].mean() if total_trades > 0 else 0

        report = {
            "initial_capital": self.initial_capital,
            "final_value": round(final_value, 2),
            "total_return": round(total_return, 4),
            "annual_return": round(annual_return, 4),
            "max_drawdown": round(max_drawdown, 4),
            "sharpe_ratio": round(sharpe, 2),
            "total_trades": total_trades,
            "win_trades": win_trades,
            "lose_trades": lose_trades,
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 2),
            "avg_hold_days": round(avg_hold_days, 1),
            "backtest_days": days,
            "start_date": all_dates[0],
            "end_date": all_dates[-1],
            "daily_values": df,
            "trades": trades_df
        }

        return report


# ============================================================
# 二、参数优化
# ============================================================

def optimize_parameters(stock_codes: list, data_dict: dict,
                        param_grid: dict = None,
                        initial_capital: float = None) -> list:
    """
    参数优化：遍历参数组合，找出最优参数
    
    参数:
        stock_codes: 股票代码列表
        data_dict: 历史日线数据
        param_grid: 参数网格，如:
            {
                "MA_SHORT": [10, 20, 30],
                "VOLUME_SHRINK_RATIO": [0.2, 0.3, 0.4],
                "INITIAL_STOP_LOSS_PCT": [0.08, 0.10, 0.12]
            }
        initial_capital: 初始资金
    
    返回:
            [(params_dict, report_dict), ...] 按夏普比率排序
    """
    if param_grid is None:
        param_grid = {
            "MA_SHORT": [15, 20, 25],
            "VOLUME_SHRINK_RATIO": [0.25, 0.30, 0.35],
            "INITIAL_STOP_LOSS_PCT": [0.08, 0.10, 0.12]
        }

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(product(*values))

    results = []
    total = len(combinations)

    for i, combo in enumerate(combinations, 1):
        params = dict(zip(keys, combo))

        # 临时修改config参数
        original = {}
        for k, v in params.items():
            if hasattr(config, k):
                original[k] = getattr(config, k)
                setattr(config, k, v)

        # 运行回测
        engine = BacktestEngine(initial_capital)
        report = engine.run(stock_codes, data_dict)

        # 恢复参数
        for k, v in original.items():
            setattr(config, k, v)

        if "error" not in report:
            results.append((params, report))
            logger.info(f"[{i}/{total}] {params} -> 夏普={report.get('sharpe_ratio', 0):.2f}, "
                       f"收益={report.get('total_return', 0):.2%}")

    # 按夏普比率排序
    results.sort(key=lambda x: x[1].get("sharpe_ratio", 0), reverse=True)
    return results


# ============================================================
# 三、便捷接口
# ============================================================

def run_backtest(stock_codes: list = None, data_dict: dict = None,
                 initial_capital: float = None,
                 start_date: str = None, end_date: str = None) -> dict:
    """
    便捷回测接口
    
    参数:
        stock_codes: 股票代码列表（默认取config.STOCK_POOL）
        data_dict: {code: DataFrame} 历史数据
        initial_capital: 初始资金
        start_date: 开始日期
        end_date: 结束日期
    
    返回:
        回测结果字典
    """
    if stock_codes is None:
        stock_codes = list(config.STOCK_POOL.keys())

    engine = BacktestEngine(initial_capital)
    report = engine.run(stock_codes, data_dict, start_date, end_date)
    return report


def format_backtest_report(report: dict) -> str:
    """格式化回测报告为文本"""
    if "error" in report:
        return f"回测失败: {report['error']}"

    lines = [
        "=" * 60,
        "  回测报告",
        "=" * 60,
        f"  回测区间: {report['start_date']} ~ {report['end_date']} ({report['backtest_days']}个交易日)",
        f"  初始资金: {report['initial_capital']:,.0f} 元",
        f"  最终资产: {report['final_value']:,.0f} 元",
        "",
        "  --- 收益指标 ---",
        f"  总收益率:   {report['total_return']:.2%}",
        f"  年化收益率: {report['annual_return']:.2%}",
        f"  最大回撤:   {report['max_drawdown']:.2%}",
        f"  夏普比率:   {report['sharpe_ratio']:.2f}",
        "",
        "  --- 交易统计 ---",
        f"  交易次数:   {report['total_trades']}",
        f"  盈利次数:   {report['win_trades']}",
        f"  亏损次数:   {report['lose_trades']}",
        f"  胜率:       {report['win_rate']:.1%}",
        f"  盈亏比:     {report['profit_factor']:.2f}",
        f"  平均持仓:   {report['avg_hold_days']:.0f}天",
        "=" * 60,
    ]

    # 交易明细
    trades = report.get("trades")
    if trades is not None and not trades.empty:
        sell_trades = trades[trades["action"] == "sell"]
        if not sell_trades.empty:
            lines.append("\n  交易明细（最近10笔）:")
            for _, t in sell_trades.tail(10).iterrows():
                pnl_icon = "+" if t.get("pnl", 0) > 0 else ""
                name = config.STOCK_POOL.get(t["code"], {}).get("名称", t["code"])
                lines.append(f"    {t['date']} {t['code']}{name}: "
                           f"卖出{t['shares']}股@{t['price']:.2f}, "
                           f"盈亏{pnl_icon}{t.get('pnl', 0):,.0f}元({t.get('pnl_pct', 0):.2%}), "
                           f"持仓{t.get('hold_days', 0)}天")

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("=" * 50)
    print("  回测引擎 - 测试")
    print("=" * 50)

    # 生成模拟数据
    np.random.seed(42)
    data_dict = {}
    for code in list(config.STOCK_POOL.keys())[:3]:  # 只用前3只测试
        dates = pd.date_range("2024-01-01", periods=250, freq="B")
        base = 50 + np.random.random() * 100
        prices = [base]
        for i in range(1, 250):
            change = np.random.normal(0.05, 0.8)
            prices.append(max(prices[-1] + change, 10))

        df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": [p * 0.998 for p in prices],
            "close": prices,
            "high": [p * 1.015 for p in prices],
            "low": [p * 0.985 for p in prices],
            "volume": np.random.randint(500000, 2000000, 250).astype(float),
        })
        data_dict[code] = df

    report = run_backtest(initial_capital=1000000, data_dict=data_dict)
    print(format_backtest_report(report))

    print("\n[OK] 回测引擎测试完成")
