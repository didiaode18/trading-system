# -*- coding: utf-8 -*-
"""
夏普比率分解
==============
把策略收益拆分为 Beta 收益（市场暴露）与 Alpha 收益（选股/择时能力），
并输出主动收益、主动波动（跟踪误差）与信息比率：

    R_p = Alpha + Beta × R_b + ε
    主动收益 = R_p - R_b（超额收益序列）
    主动波动 = std(主动收益) × √252（跟踪误差）
    信息比率 IR = 年化主动收益 / 主动波动
    夏普 = (年化收益 - 无风险利率) / 年化波动

与 alpha_beta.py 的 CAPM 回归口径一致（OLS），本模块侧重"波动/风险调整"
维度的分解；数据不足(<10个观测)时静默降级返回 ok=False，禁止编造数据。
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
MIN_OBSERVATIONS = 10
DEFAULT_RISK_FREE = 0.03


def decompose_sharpe(strategy_returns: pd.Series,
                     benchmark_returns: pd.Series,
                     risk_free_rate: float = DEFAULT_RISK_FREE) -> dict:
    """
    夏普比率分解：Beta收益 vs Alpha收益 + 主动风险三件套

    参数:
        strategy_returns:  策略日收益序列（小数口径）
        benchmark_returns: 基准日收益序列（如沪深300）
        risk_free_rate:    年化无风险利率（默认3%，与 metrics.calc_sharpe_ratio 一致）

    返回:
        {
            "ok": bool,
            "sharpe": 策略夏普,
            "beta": Beta系数,
            "alpha_annual": 年化Alpha(回归残差口径),
            "r_squared": 拟合优度,
            "total_return_annual": 策略年化收益,
            "benchmark_return_annual": 基准年化收益,
            "beta_return_annual": Beta×基准年化（市场暴露贡献）,
            "alpha_return_annual": 年化收益 - Beta贡献（含现金拖累）,
            "active_return_annual": 主动收益(年化, R_p - R_b),
            "active_volatility": 主动波动(跟踪误差, 年化),
            "information_ratio": 信息比率,
            "volatility": 策略年化波动,
            "message": 降级原因(ok=False时),
        }
    """
    empty = {
        "ok": False, "sharpe": 0.0, "beta": 0.0, "alpha_annual": 0.0,
        "r_squared": 0.0, "total_return_annual": 0.0, "benchmark_return_annual": 0.0,
        "beta_return_annual": 0.0, "alpha_return_annual": 0.0,
        "active_return_annual": 0.0, "active_volatility": 0.0,
        "information_ratio": 0.0, "volatility": 0.0,
        "message": "",
    }

    if strategy_returns is None or benchmark_returns is None:
        empty["message"] = "收益序列缺失，夏普分解降级"
        return empty

    # 按日期对齐（两序列索引可能不完全一致）
    aligned = pd.concat([
        pd.to_numeric(strategy_returns, errors="coerce").rename("sp"),
        pd.to_numeric(benchmark_returns, errors="coerce").rename("bm"),
    ], axis=1, join="inner").dropna()

    if len(aligned) < MIN_OBSERVATIONS:
        empty["message"] = (f"样本不足({len(aligned)}<{MIN_OBSERVATIONS}个观测)，"
                            f"夏普分解降级")
        return empty

    y = aligned["sp"].values
    x = aligned["bm"].values
    n = len(y)

    # OLS: y = alpha_daily + beta * x
    X = np.column_stack([np.ones(n), x])
    try:
        params = np.linalg.lstsq(X, y, rcond=None)[0]
        alpha_daily, beta = float(params[0]), float(params[1])
    except Exception as e:
        empty["message"] = f"回归失败: {e}"
        return empty

    y_pred = X @ params
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # 年化（算术口径 mean×252，保证收益分解可加：
    # total = beta_return + alpha_return，与 OLS 回归一致）
    def _annual(daily_mean: float) -> float:
        return daily_mean * TRADING_DAYS

    total_annual = _annual(float(np.mean(y)))
    bench_annual = _annual(float(np.mean(x)))
    volatility = float(np.std(y, ddof=1)) * np.sqrt(TRADING_DAYS) if n > 1 else 0.0
    sharpe = ((total_annual - risk_free_rate) / volatility) if volatility > 1e-12 else 0.0

    # Beta收益 = Beta × 基准年化；Alpha收益 = 总收益 - Beta收益（= alpha_daily×252）
    beta_return_annual = beta * bench_annual
    alpha_return_annual = total_annual - beta_return_annual

    # 主动收益/主动波动/信息比率
    active = y - x
    active_return = _annual(float(np.mean(active)))
    active_vol = float(np.std(active, ddof=1)) * np.sqrt(TRADING_DAYS) if n > 1 else 0.0
    ir = (active_return / active_vol) if active_vol > 1e-12 else 0.0

    return {
        "ok": True,
        "sharpe": round(sharpe, 4),
        "beta": round(beta, 4),
        "alpha_annual": round(alpha_daily * TRADING_DAYS, 4),
        "r_squared": round(r_squared, 4),
        "total_return_annual": round(total_annual, 4),
        "benchmark_return_annual": round(bench_annual, 4),
        "beta_return_annual": round(beta_return_annual, 4),
        "alpha_return_annual": round(alpha_return_annual, 4),
        "active_return_annual": round(active_return, 4),
        "active_volatility": round(active_vol, 4),
        "information_ratio": round(ir, 4),
        "volatility": round(volatility, 4),
        "risk_free_rate": risk_free_rate,
        "observations": n,
        "message": "",
    }


def render_sharpe_decompose_html(decomp: dict) -> str:
    """
    夏普分解区块（HTML，供周报/盘后报告嵌入）

    ok=False（数据不足）时返回空字符串静默隐藏。
    """
    if not decomp or not decomp.get("ok"):
        return ""

    pct = lambda x: f"{x * 100:+.2f}%"
    html = ['<div class="sharpe-decompose" style="margin:12px 0">']
    html.append('<h3 style="margin:8px 0">夏普分解（Beta vs Alpha）</h3>')
    html.append('<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:8px 0">')
    cards = [
        ("夏普比率", f"{decomp['sharpe']:.2f}", "#2980b9"),
        ("信息比率 IR", f"{decomp['information_ratio']:.2f}", "#8e44ad"),
        ("Beta", f"{decomp['beta']:.2f}", "#7f8c8d"),
        ("R²", f"{decomp['r_squared']:.2f}", "#7f8c8d"),
    ]
    for label, val, color in cards:
        html.append(f'<div style="background:#f8f9fa;border-radius:8px;padding:10px;'
                    f'text-align:center"><div style="font-size:16px;font-weight:bold;'
                    f'color:{color}">{val}</div>'
                    f'<div style="font-size:11px;color:#7f8c8d;margin-top:3px">{label}</div></div>')
    html.append('</div>')

    html.append('<table style="width:100%;border-collapse:collapse;font-size:13px">')
    for label, key in [
        ("策略年化收益", "total_return_annual"),
        ("基准年化收益", "benchmark_return_annual"),
        ("Beta收益（市场暴露）", "beta_return_annual"),
        ("Alpha收益（选股/择时）", "alpha_return_annual"),
        ("主动收益（年化）", "active_return_annual"),
        ("主动波动（跟踪误差）", "active_volatility"),
    ]:
        html.append(f'<tr><td style="padding:5px;color:#7f8c8d">{label}</td>'
                    f'<td style="text-align:right"><b>{pct(decomp.get(key, 0.0))}</b></td></tr>')
    html.append('</table>')
    html.append(f'<div style="font-size:11px;color:#95a5a6;margin-top:4px">'
                f'样本 {decomp.get("observations", 0)} 个交易日 | '
                f'无风险利率 {decomp.get("risk_free_rate", DEFAULT_RISK_FREE):.1%}</div>')
    html.append('</div>')
    return "".join(html)
