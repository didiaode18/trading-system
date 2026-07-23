"""
绩效指标计算模块
================
计算回测/实盘的核心绩效指标：
- 年化收益率、最大回撤、夏普比率、Calmar比率、Sortino比率
- 胜率、盈亏比、期望收益
- MFE/MAE（最大有利/不利偏移）
- 月度收益分布
- 基准对比（Alpha/Beta/信息比率）
"""

import numpy as np
import pandas as pd
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 核心绩效指标
# ============================================================

def calc_annual_return(total_return: float, trading_days: int) -> float:
    """年化收益率（基于252个交易日）"""
    if trading_days <= 0:
        return 0.0
    return (1 + total_return) ** (252 / trading_days) - 1


def calc_max_drawdown(equity_curve: pd.Series) -> tuple:
    """
    最大回撤
    
    返回:
        (max_drawdown_pct, peak_date, trough_date)
    """
    cummax = equity_curve.cummax()
    drawdown = (cummax - equity_curve) / cummax
    max_dd = drawdown.max()

    # 找到回撤的峰值和谷值日期
    trough_idx = drawdown.idxmax()
    peak_idx = equity_curve[:trough_idx].idxmax() if trough_idx > 0 else 0

    return max_dd, peak_idx, trough_idx


def calc_sharpe_ratio(daily_returns: pd.Series, risk_free_rate: float = 0.03) -> float:
    """
    夏普比率（年化）
    
    参数:
        daily_returns: 日收益率序列
        risk_free_rate: 无风险利率（默认3%）
    """
    if len(daily_returns) < 2 or daily_returns.std() == 0:
        return 0.0
    excess_return = daily_returns.mean() - risk_free_rate / 252
    return excess_return / daily_returns.std() * np.sqrt(252)


def calc_sortino_ratio(daily_returns: pd.Series, risk_free_rate: float = 0.03) -> float:
    """Sortino比率（只考虑下行波动）"""
    if len(daily_returns) < 2:
        return 0.0
    excess_return = daily_returns.mean() - risk_free_rate / 252
    downside = daily_returns[daily_returns < 0]
    if len(downside) == 0 or downside.std() == 0:
        return 0.0
    return excess_return / downside.std() * np.sqrt(252)


def calc_calmar_ratio(annual_return: float, max_drawdown: float) -> float:
    """Calmar比率 = 年化收益 / 最大回撤"""
    if max_drawdown == 0:
        return 0.0
    return annual_return / max_drawdown


def calc_win_rate(trades: pd.DataFrame) -> float:
    """胜率"""
    if trades.empty:
        return 0.0
    wins = len(trades[trades["pnl"] > 0])
    return wins / len(trades)


def calc_profit_factor(trades: pd.DataFrame) -> float:
    """盈亏比 = 平均盈利 / 平均亏损"""
    if trades.empty:
        return 0.0
    wins = trades[trades["pnl"] > 0]["pnl"]
    losses = trades[trades["pnl"] < 0]["pnl"]
    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = abs(losses.mean()) if len(losses) > 0 else 1
    return avg_win / avg_loss if avg_loss > 0 else 0


def calc_expectancy(trades: pd.DataFrame) -> float:
    """每笔期望收益 = 胜率*平均盈利 - 败率*平均亏损"""
    if trades.empty:
        return 0.0
    win_rate = calc_win_rate(trades)
    wins = trades[trades["pnl"] > 0]["pnl"]
    losses = trades[trades["pnl"] < 0]["pnl"]
    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = abs(losses.mean()) if len(losses) > 0 else 0
    return win_rate * avg_win - (1 - win_rate) * avg_loss


# ============================================================
# MFE/MAE 分析
# ============================================================

def calc_mfe_mae(trades_with_extremes: pd.DataFrame) -> dict:
    """
    计算MFE/MAE统计
    
    参数:
        trades_with_extremes: 含 "mfe"(最大浮盈) 和 "mae"(最大浮亏) 列
    
    返回:
        {"avg_mfe", "avg_mae", "mfe_capture_rate", "mae_tolerance"}
    """
    if trades_with_extremes.empty:
        return {"avg_mfe": 0, "avg_mae": 0, "mfe_capture_rate": 0, "mae_tolerance": 0}

    avg_mfe = trades_with_extremes["mfe"].mean()
    avg_mae = trades_with_extremes["mae"].mean()

    # MFE捕获率 = 实际盈利 / 最大浮盈
    mfe_col = trades_with_extremes["mfe"]
    pnl_col = trades_with_extremes["pnl_pct"]
    valid = mfe_col > 0
    if valid.sum() > 0:
        capture_rate = (pnl_col[valid] / mfe_col[valid]).mean()
    else:
        capture_rate = 0

    return {
        "avg_mfe": round(avg_mfe, 4),
        "avg_mae": round(avg_mae, 4),
        "mfe_capture_rate": round(capture_rate, 4),
        "mae_tolerance": round(abs(avg_mae), 4),
    }


# ============================================================
# 月度收益
# ============================================================

def calc_monthly_returns(equity_curve: pd.Series, dates: list) -> pd.DataFrame:
    """
    计算月度收益分布
    
    返回:
        DataFrame with columns: year, month, return_pct
    """
    if len(dates) != len(equity_curve):
        return pd.DataFrame()

    df = pd.DataFrame({"date": dates, "value": equity_curve.values})
    df["date"] = pd.to_datetime(df["date"])
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month

    monthly = df.groupby(["year", "month"]).agg(
        start_value=("value", "first"),
        end_value=("value", "last")
    ).reset_index()
    monthly["return_pct"] = (monthly["end_value"] - monthly["start_value"]) / monthly["start_value"]

    return monthly[["year", "month", "return_pct"]]


# ============================================================
# 基准对比
# ============================================================

def calc_alpha_beta(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> dict:
    """
    CAPM回归计算Alpha和Beta
    
    返回:
        {"alpha", "beta", "r_squared", "info_ratio"}
    """
    if len(strategy_returns) < 10 or len(benchmark_returns) < 10:
        return {"alpha": 0, "beta": 0, "r_squared": 0, "info_ratio": 0}

    # 对齐长度
    n = min(len(strategy_returns), len(benchmark_returns))
    y = strategy_returns.values[:n]
    x = benchmark_returns.values[:n]

    # OLS回归
    x_with_const = np.column_stack([np.ones(n), x])
    try:
        beta, alpha = np.linalg.lstsq(x_with_const, y, rcond=None)[0]
    except Exception:
        return {"alpha": 0, "beta": 0, "r_squared": 0, "info_ratio": 0}

    # R²
    y_pred = alpha + beta * x
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    # 信息比率 = 超额收益 / 跟踪误差
    excess = y - x
    tracking_error = excess.std() * np.sqrt(252)
    info_ratio = (excess.mean() * 252) / tracking_error if tracking_error > 0 else 0

    return {
        "alpha": round(alpha * 252, 4),  # 年化Alpha
        "beta": round(beta, 4),
        "r_squared": round(r_squared, 4),
        "info_ratio": round(info_ratio, 4),
    }


# ============================================================
# 综合绩效报告
# ============================================================

def generate_performance_report(equity_curve: pd.Series, trades: pd.DataFrame,
                                dates: list, benchmark_curve: pd.Series = None,
                                initial_capital: float = 100000) -> dict:
    """
    生成完整绩效报告
    
    参数:
        equity_curve: 每日净值序列
        trades: 交易记录DataFrame (含pnl, pnl_pct, hold_days列)
        dates: 日期列表
        benchmark_curve: 基准净值序列（可选）
        initial_capital: 初始资金
    
    返回:
        绩效指标字典
    """
    if equity_curve.empty:
        return {"error": "无数据"}

    # 基本收益
    final_value = equity_curve.iloc[-1]
    total_return = (final_value - initial_capital) / initial_capital
    trading_days = len(equity_curve)
    annual_return = calc_annual_return(total_return, trading_days)

    # 回撤
    max_dd, peak_date, trough_date = calc_max_drawdown(equity_curve)

    # 日收益率
    daily_returns = equity_curve.pct_change().dropna()

    # 风险指标
    sharpe = calc_sharpe_ratio(daily_returns)
    sortino = calc_sortino_ratio(daily_returns)
    calmar = calc_calmar_ratio(annual_return, max_dd)

    # 波动率
    annual_vol = daily_returns.std() * np.sqrt(252) if len(daily_returns) > 1 else 0

    # 交易统计
    sell_trades = trades[trades["action"] == "sell"] if not trades.empty and "action" in trades.columns else trades
    total_trades = len(sell_trades)
    win_rate = calc_win_rate(sell_trades) if total_trades > 0 else 0
    profit_factor = calc_profit_factor(sell_trades) if total_trades > 0 else 0
    expectancy = calc_expectancy(sell_trades) if total_trades > 0 else 0
    avg_hold = sell_trades["hold_days"].mean() if total_trades > 0 and "hold_days" in sell_trades.columns else 0

    # 月度收益
    monthly = calc_monthly_returns(equity_curve, dates)

    report = {
        # 收益
        "initial_capital": initial_capital,
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        # 风险
        "max_drawdown": round(max_dd, 4),
        "annual_volatility": round(annual_vol, 4),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "calmar_ratio": round(calmar, 2),
        # 交易
        "total_trades": total_trades,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
        "avg_hold_days": round(avg_hold, 1),
        # 时间
        "trading_days": trading_days,
        "start_date": dates[0] if dates else "",
        "end_date": dates[-1] if dates else "",
        # 月度
        "monthly_returns": monthly,
    }

    # 基准对比
    if benchmark_curve is not None and not benchmark_curve.empty:
        bench_returns = benchmark_curve.pct_change().dropna()
        ab = calc_alpha_beta(daily_returns, bench_returns)
        report.update(ab)
        bench_total = (benchmark_curve.iloc[-1] - benchmark_curve.iloc[0]) / benchmark_curve.iloc[0]
        report["benchmark_return"] = round(bench_total, 4)
        report["excess_return"] = round(total_return - bench_total, 4)

    return report


def format_report_text(report: dict) -> str:
    """格式化绩效报告为文本"""
    if "error" in report:
        return f"回测失败: {report['error']}"

    lines = [
        "=" * 60,
        "  量化回测绩效报告",
        "=" * 60,
        f"  回测区间: {report['start_date']} ~ {report['end_date']} ({report['trading_days']}个交易日)",
        f"  初始资金: {report['initial_capital']:,.0f} 元",
        f"  最终资产: {report['final_value']:,.0f} 元",
        "",
        "  ─── 收益指标 ───",
        f"  总收益率:     {report['total_return']:.2%}",
        f"  年化收益率:   {report['annual_return']:.2%}",
        f"  最大回撤:     {report['max_drawdown']:.2%}",
        f"  年化波动率:   {report.get('annual_volatility', 0):.2%}",
        "",
        "  ─── 风险调整收益 ───",
        f"  夏普比率:     {report['sharpe_ratio']:.2f}",
        f"  Sortino比率:  {report['sortino_ratio']:.2f}",
        f"  Calmar比率:   {report['calmar_ratio']:.2f}",
        "",
        "  ─── 交易统计 ───",
        f"  交易次数:     {report['total_trades']}",
        f"  胜率:         {report['win_rate']:.1%}",
        f"  盈亏比:       {report['profit_factor']:.2f}",
        f"  每笔期望:     {report['expectancy']:,.0f} 元",
        f"  平均持仓:     {report['avg_hold_days']:.0f} 天",
    ]

    # 基准对比
    if "benchmark_return" in report:
        lines.extend([
            "",
            "  ─── 基准对比 ───",
            f"  基准收益:     {report['benchmark_return']:.2%}",
            f"  超额收益:     {report['excess_return']:.2%}",
            f"  Alpha(年化):  {report.get('alpha', 0):.2%}",
            f"  Beta:         {report.get('beta', 0):.2f}",
            f"  信息比率:     {report.get('info_ratio', 0):.2f}",
        ])

    lines.append("=" * 60)
    return "\n".join(lines)
