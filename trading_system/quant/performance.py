"""
绩效分析 + 净值曲线输出
========================
计算核心指标:
- 年化收益率、累计收益率
- 最大回撤、最大回撤持续天数
- 夏普比率（无风险利率3%）
- Calmar比率（年化/最大回撤）
- 胜率、盈亏比
- 平均持仓天数、换手率
- 月度收益分布

使用方式:
    from quant.performance import calc_performance, print_report, export_nav_curve
    perf = calc_performance(result["daily_values"], result["trades"])
    print_report(perf)
    export_nav_curve(result["daily_values"], "nav_curve.csv")
"""

import os
import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_performance(daily_values: pd.DataFrame, trades: pd.DataFrame,
                     risk_free_rate: float = 0.03) -> dict:
    """
    计算完整绩效指标

    参数:
        daily_values: 每日净值DataFrame (date, total_value, nav, ...)
        trades: 交易记录DataFrame
        risk_free_rate: 无风险年利率（默认3%）

    返回:
        绩效指标字典
    """
    if daily_values.empty:
        return {"error": "无数据"}

    nav = daily_values["nav"].values
    n_days = len(nav)

    # ---- 收益指标 ----
    total_return = nav[-1] - 1.0
    annual_return = (1 + total_return) ** (252 / max(n_days, 1)) - 1

    # ---- 最大回撤 ----
    cummax = np.maximum.accumulate(nav)
    drawdown = (cummax - nav) / cummax
    max_drawdown = drawdown.max()

    # 最大回撤持续天数
    max_dd_duration = 0
    current_dd_duration = 0
    for dd in drawdown:
        if dd > 0:
            current_dd_duration += 1
            max_dd_duration = max(max_dd_duration, current_dd_duration)
        else:
            current_dd_duration = 0

    # ---- 夏普比率 ----
    daily_returns = np.diff(nav) / nav[:-1]
    if len(daily_returns) > 1 and np.std(daily_returns) > 0:
        excess_return = np.mean(daily_returns) - risk_free_rate / 252
        sharpe = excess_return / np.std(daily_returns) * np.sqrt(252)
    else:
        sharpe = 0

    # ---- Calmar比率 ----
    calmar = annual_return / max_drawdown if max_drawdown > 0 else 0

    # ---- 波动率 ----
    annual_vol = np.std(daily_returns) * np.sqrt(252) if len(daily_returns) > 1 else 0

    # ---- 交易统计 ----
    sell_trades = trades[trades["action"] == "sell"] if not trades.empty else pd.DataFrame()
    total_trades = len(sell_trades)

    if total_trades > 0:
        win_trades = len(sell_trades[sell_trades["pnl"] > 0])
        lose_trades = total_trades - win_trades
        win_rate = win_trades / total_trades

        avg_win = sell_trades[sell_trades["pnl"] > 0]["pnl"].mean() if win_trades > 0 else 0
        avg_lose = abs(sell_trades[sell_trades["pnl"] < 0]["pnl"].mean()) if lose_trades > 0 else 1
        profit_factor = avg_win / avg_lose if avg_lose > 0 else 0

        avg_hold_days = sell_trades["hold_days"].mean()
        total_commission = trades["commission"].sum() if "commission" in trades.columns else 0
        total_tax = trades["stamp_tax"].sum() if "stamp_tax" in trades.columns else 0
        total_slippage = trades["slippage_cost"].sum() if "slippage_cost" in trades.columns else 0
    else:
        win_trades = lose_trades = 0
        win_rate = profit_factor = avg_hold_days = 0
        total_commission = total_tax = total_slippage = 0

    # ---- 月度收益 ----
    monthly_returns = _calc_monthly_returns(daily_values)

    return {
        # 收益
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        # 风险
        "max_drawdown": round(max_drawdown, 4),
        "max_dd_duration": max_dd_duration,
        "annual_volatility": round(annual_vol, 4),
        # 风险调整
        "sharpe_ratio": round(sharpe, 3),
        "calmar_ratio": round(calmar, 3),
        # 交易
        "total_trades": total_trades,
        "win_trades": win_trades,
        "lose_trades": lose_trades,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 3),
        "avg_hold_days": round(avg_hold_days, 1),
        # 成本
        "total_commission": round(total_commission, 2),
        "total_tax": round(total_tax, 2),
        "total_slippage": round(total_slippage, 2),
        "total_cost": round(total_commission + total_tax + total_slippage, 2),
        # 月度
        "monthly_returns": monthly_returns,
        # 基础
        "trade_days": n_days,
    }


def _calc_monthly_returns(daily_values: pd.DataFrame) -> list:
    """计算月度收益分布"""
    if daily_values.empty or "date" not in daily_values.columns:
        return []

    df = daily_values.copy()
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")

    monthly = []
    for month, group in df.groupby("month"):
        if len(group) >= 2:
            month_return = group["nav"].iloc[-1] / group["nav"].iloc[0] - 1
            monthly.append({
                "month": str(month),
                "return": round(month_return, 4),
            })

    return monthly


def export_nav_curve(daily_values: pd.DataFrame, output_path: str):
    """
    输出净值曲线CSV

    列: date, nav, total_value, position_count
    """
    if daily_values.empty:
        logger.warning("无净值数据可导出")
        return

    export_cols = ["date", "nav", "total_value"]
    if "position_count" in daily_values.columns:
        export_cols.append("position_count")

    daily_values[export_cols].to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info(f"净值曲线已导出: {output_path} ({len(daily_values)}行)")


def print_report(perf: dict, result: dict = None):
    """打印格式化回测报告"""
    if "error" in perf:
        print(f"回测失败: {perf['error']}")
        return

    print()
    print("=" * 65)
    print("  P0 量化回测报告 - 多因子选股策略")
    print("=" * 65)

    if result:
        print(f"  回测区间: {result.get('start_date', '')} ~ {result.get('end_date', '')} "
              f"({perf['trade_days']}个交易日)")
        print(f"  初始资金: {result.get('initial_capital', 0):,.0f} 元")
        print(f"  最终资产: {result.get('final_value', 0):,.0f} 元")
        params = result.get("params", {})
        print(f"  交易成本: 佣金{params.get('commission', 0):.4%} + "
              f"印花税{params.get('stamp_tax', 0):.3%} + 滑点{params.get('slippage', 0):.2%}")

    print()
    print("  ┌─────────── 收益指标 ───────────┐")
    print(f"  │ 累计收益率:   {perf['total_return']:>10.2%}")
    print(f"  │ 年化收益率:   {perf['annual_return']:>10.2%}")
    print(f"  │ 年化波动率:   {perf['annual_volatility']:>10.2%}")
    print("  └────────────────────────────────┘")

    print()
    print("  ┌─────────── 风险指标 ───────────┐")
    print(f"  │ 最大回撤:     {perf['max_drawdown']:>10.2%}")
    print(f"  │ 回撤持续:     {perf['max_dd_duration']:>10d} 天")
    print(f"  │ 夏普比率:     {perf['sharpe_ratio']:>10.3f}")
    print(f"  │ Calmar比率:   {perf['calmar_ratio']:>10.3f}")
    print("  └────────────────────────────────┘")

    print()
    print("  ┌─────────── 交易统计 ───────────┐")
    print(f"  │ 交易次数:     {perf['total_trades']:>10d}")
    print(f"  │ 盈利/亏损:    {perf['win_trades']:>5d} / {perf['lose_trades']:<5d}")
    print(f"  │ 胜率:         {perf['win_rate']:>10.1%}")
    print(f"  │ 盈亏比:       {perf['profit_factor']:>10.3f}")
    print(f"  │ 平均持仓:     {perf['avg_hold_days']:>10.1f} 天")
    print("  └────────────────────────────────┘")

    print()
    print("  ┌─────────── 交易成本 ───────────┐")
    print(f"  │ 总佣金:       {perf['total_commission']:>10,.0f} 元")
    print(f"  │ 总印花税:     {perf['total_tax']:>10,.0f} 元")
    print(f"  │ 总滑点:       {perf['total_slippage']:>10,.0f} 元")
    print(f"  │ 总成本:       {perf['total_cost']:>10,.0f} 元")
    print("  └────────────────────────────────┘")

    # 月度收益
    monthly = perf.get("monthly_returns", [])
    if monthly:
        print()
        print("  ┌─────────── 月度收益(近12月) ───────────┐")
        for m in monthly[-12:]:
            bar = "+" * int(m["return"] * 200) if m["return"] > 0 else "-" * int(-m["return"] * 200)
            print(f"  │ {m['month']}: {m['return']:>7.2%} {bar}")
        print("  └────────────────────────────────────────┘")

    print()
    print("=" * 65)
