# -*- coding: utf-8 -*-
"""
Stage 9 · 多基准对比（full-history-backtest 新增）
====================================================
策略（等权买入持有日收益，见 master_common）对比多只宽基指数：
沪深300 / 中证500 / 中证1000 / 中证2000 / 创业板指。

对每个基准输出：累计收益、年化、Beta、Alpha、信息比率、跟踪误差。
基准数据统一走现有数据层（data_dict 优先，缺失时 data_loader.fetch_stock_daily
兜底）；取不到的基准显式标注 missing，禁止假装"全部正常"。

复用而非重写：alpha/beta/信息比率走 backtest.metrics.calc_alpha_beta，
跟踪误差 = (策略-基准) 日收益标准差 × sqrt(252)。
"""

import logging
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

try:
    from . import master_common, metrics
except ImportError:  # pragma: no cover
    from backtest import master_common, metrics

logger = logging.getLogger(__name__)

MBC = config.MASTER_BACKTEST_CONFIG if hasattr(config, "MASTER_BACKTEST_CONFIG") else {}
BENCHMARKS = MBC.get("benchmarks", {
    "000300": "沪深300", "000905": "中证500", "000852": "中证1000",
    "932000": "中证2000", "399006": "创业板指",
})


def _benchmark_returns(code: str, data_dict: dict, allow_fetch: bool) -> pd.Series:
    """获取基准日收益序列：data_dict 优先，缺失时可尝试数据层兜底。"""
    df = data_dict.get(code)
    if df is None and allow_fetch:
        try:
            from data.data_loader import fetch_stock_daily
            raw = fetch_stock_daily(code)
            if raw is not None and len(raw) >= 30:
                df = raw.copy()
        except Exception as e:
            logger.debug(f"[Stage9] 基准 {code} 数据层获取失败: {e}")
            df = None
    if df is None or len(df) < 2 or "close" not in df.columns or "date" not in df.columns:
        return pd.Series(dtype=float)
    s = df.copy()
    s["date"] = s["date"].astype(str)
    s = s.dropna(subset=["close"]).drop_duplicates(subset=["date"]).set_index("date")["close"]
    s = s.astype(float).sort_index()
    return s.pct_change().dropna()


def compare_benchmarks(data_dict: dict, codes: list, quick: bool = False) -> dict:
    """多基准对比主入口。

    返回:
      {status, strategy: {...}, benchmarks: {code: {name, cum_return, annual,
       beta, alpha, info_ratio, tracking_error, n_days}}, missing: [code...]}
    """
    strat_rets = master_common.equal_weight_daily_returns(data_dict, codes)
    strat = master_common.summarize_daily_returns(strat_rets)
    if strat.get("status") != "ok":
        return {"status": "skipped", "reason": "策略日收益样本不足，无法对比基准"}

    # quick 模式不做网络兜底（避免超时），仅用 data_dict 内已有基准
    allow_fetch = not quick

    results = {}
    missing = []
    for code, name in BENCHMARKS.items():
        bench_rets = _benchmark_returns(code, data_dict, allow_fetch)
        if len(bench_rets) < 10:
            missing.append(code)
            results[code] = {"name": name, "status": "missing",
                             "reason": "基准数据不可得（库中无且未联网兜底）"}
            continue
        s_al, b_al = master_common.align_returns(strat_rets, bench_rets)
        if len(s_al) < 10:
            missing.append(code)
            results[code] = {"name": name, "status": "missing",
                             "reason": f"策略与基准重叠交易日不足({len(s_al)})"}
            continue
        ab = metrics.calc_alpha_beta(s_al, b_al)
        bench_equity = (1.0 + b_al).cumprod()
        bench_cum = float(bench_equity.iloc[-1] - 1.0)
        bench_annual = metrics.calc_annual_return(bench_cum, len(b_al))
        excess = (s_al.values - b_al.values)
        tracking_error = float(np.std(excess) * np.sqrt(252)) if len(excess) > 1 else 0.0
        results[code] = {
            "name": name,
            "status": "ok",
            "n_days": len(s_al),
            "strategy_cum_return": strat["total_return"],
            "strategy_annual": strat["annual_return"],
            "benchmark_cum_return": round(bench_cum, 4),
            "benchmark_annual": round(float(bench_annual), 4),
            "excess_return": round(strat["total_return"] - bench_cum, 4),
            "beta": round(float(ab.get("beta", 0)), 4),
            "alpha": round(float(ab.get("alpha", 0)), 4),
            "r_squared": round(float(ab.get("r_squared", 0)), 4),
            "info_ratio": round(float(ab.get("info_ratio", 0)), 4),
            "tracking_error": round(tracking_error, 4),
        }

    n_ok = sum(1 for v in results.values() if v.get("status") == "ok")
    return {
        "status": "ok" if n_ok > 0 else "skipped",
        "strategy": strat,
        "benchmarks": results,
        "missing": missing,
        "n_available": n_ok,
        "note": ("基准缺失项已如实标注；quick 模式不联网兜底。"
                 if missing else "全部基准均已获取。"),
    }
