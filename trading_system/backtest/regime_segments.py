# -*- coding: utf-8 -*-
"""
Stage 11 · 分段绩效（full-history-backtest 新增）
==================================================
策略（等权买入持有日收益，见 master_common）分段统计：
  1. 自然年分段：每年收益、年化、夏普、最大回撤；
  2. 市场环境分段：复用 strategy/market_regime.py 判定 BULL/RANGE/BEAR，
     分环境统计（BEAR 段验证风控是否避免大幅亏损）；
  3. 历史窗口：在 monte_carlo.HISTORICAL_SCENARIOS 的真实日期窗口内回放
     策略实际表现（有数据的区间），与情景模拟对照。

复用而非重写：市场环境判定走 MarketRegimeDetector(enable_debounce=False)
（禁用去抖避免写文件），指标走 backtest.metrics。
无未来函数：某日的 regime 仅用该日及之前的基准数据判定，作用于其后区间。
"""

import logging
import os
import sys

import numpy as np
import pandas as pd
import config

try:
    from . import master_common, metrics
except ImportError:  # pragma: no cover
    from backtest import master_common, metrics

logger = logging.getLogger(__name__)

RF_RATE = config.MASTER_BACKTEST_CONFIG.get("risk_free_rate", 0.03) \
    if hasattr(config, "MASTER_BACKTEST_CONFIG") else 0.03


def _segment_stats(daily_returns: pd.Series) -> dict:
    """一段日收益的统计（复用 metrics）。"""
    if daily_returns is None or len(daily_returns) < 2:
        return {"status": "insufficient", "n_days": 0 if daily_returns is None else len(daily_returns)}
    equity = (1.0 + daily_returns).cumprod()
    total = float(equity.iloc[-1] - 1.0)
    n = len(daily_returns)
    max_dd, _, _ = metrics.calc_max_drawdown(equity)
    return {
        "status": "ok",
        "n_days": n,
        "start_date": str(daily_returns.index[0]),
        "end_date": str(daily_returns.index[-1]),
        "total_return": round(total, 4),
        "annual_return": round(float(metrics.calc_annual_return(total, n)), 4),
        "sharpe": round(float(metrics.calc_sharpe_ratio(daily_returns, RF_RATE)), 4),
        "max_drawdown": round(float(max_dd), 4),
    }


def _by_year(daily_returns: pd.Series) -> dict:
    years = {}
    idx = pd.to_datetime(daily_returns.index.astype(str), errors="coerce")
    s = pd.Series(daily_returns.values, index=idx).dropna()
    for y, grp in s.groupby(s.index.year):
        years[str(int(y))] = _segment_stats(grp)
    return years


def _regime_labels(bench_df: pd.DataFrame, dates: list, step: int = 20) -> dict:
    """对给定日期序列标注 regime（每 step 个交易日采样一次，向后填充）。

    返回 {date_str: 'BULL'/'RANGE'/'BEAR'}；基准不足或判定失败返回 {}。
    """
    if bench_df is None or len(bench_df) < 80 or "close" not in bench_df.columns:
        return {}
    try:
        from strategy.market_regime import MarketRegimeDetector
    except Exception as e:
        logger.debug(f"[Stage11] market_regime 不可用: {e}")
        return {}

    bd = bench_df.copy()
    bd["date"] = bd["date"].astype(str)
    bd = bd.sort_values("date").reset_index(drop=True)
    date_list = bd["date"].tolist()
    date_pos = {d: i for i, d in enumerate(date_list)}

    detector = MarketRegimeDetector(enable_debounce=False)
    # 抑制 detect 内部 info 日志噪声
    mr_logger = logging.getLogger("strategy.market_regime")
    old_level = mr_logger.level
    mr_logger.setLevel(logging.WARNING)

    sampled = {}
    try:
        for i in range(60, len(bd), step):
            window = bd.iloc[max(0, i - 250):i + 1]
            if len(window) < 60:
                continue
            try:
                res = detector.detect(window)
                sampled[date_list[i]] = res.get("state", "RANGE")
            except Exception:
                continue
    finally:
        mr_logger.setLevel(old_level)

    if not sampled:
        return {}

    # 把采样点 regime 向后填充到每个查询日期（仅用采样日及之前的信息，无未来函数）
    sample_dates = sorted(sampled.keys())
    labels = {}
    for d in dates:
        ds = str(d)
        # 找 <= ds 的最近采样点
        prev = [x for x in sample_dates if x <= ds]
        labels[ds] = sampled[prev[-1]] if prev else "RANGE"
    return labels


def _by_regime(daily_returns: pd.Series, data_dict: dict) -> dict:
    bench_df = data_dict.get("000300")
    if bench_df is None:
        return {"status": "skipped", "reason": "无沪深300基准，无法判定市场环境"}
    dates = [str(d) for d in daily_returns.index]
    labels = _regime_labels(bench_df, dates)
    if not labels:
        return {"status": "skipped", "reason": "市场环境判定不可用（基准数据不足）"}
    grouped = {}
    for state in ("BULL", "RANGE", "BEAR"):
        mask = [labels.get(str(d)) == state for d in daily_returns.index]
        seg = daily_returns[np.array(mask, dtype=bool)]
        if len(seg) >= 2:
            grouped[state] = _segment_stats(seg)
        else:
            grouped[state] = {"status": "insufficient", "n_days": int(len(seg))}
    return {"status": "ok", "segments": grouped}


def _historical_windows(daily_returns: pd.Series) -> dict:
    """在历史情景真实日期窗口内回放策略实际表现（有数据的区间）。"""
    try:
        from backtest.monte_carlo import HISTORICAL_SCENARIOS
    except Exception:
        return {"status": "skipped", "reason": "历史情景库不可用"}
    out = {}
    any_hit = False
    for name, sc in HISTORICAL_SCENARIOS.items():
        period = sc.get("period")
        if not period:
            continue
        start, end = str(period[0]), str(period[1])
        seg = daily_returns[(daily_returns.index >= start) & (daily_returns.index <= end)]
        if len(seg) >= 2:
            stats = _segment_stats(seg)
            stats["scenario_index_drop"] = sc.get("index_drop")
            out[name] = stats
            any_hit = True
        else:
            out[name] = {"status": "no_data", "reason": f"区间 {start}~{end} 无策略数据"}
    return {"status": "ok" if any_hit else "skipped",
            "windows": out,
            "note": "无数据区间已如实标注（策略数据未覆盖该历史窗口）"}


def segmented_performance(data_dict: dict, codes: list) -> dict:
    """分段绩效主入口。

    返回:
      {status, by_year, by_regime, historical_windows, overall}
    """
    daily_returns = master_common.equal_weight_daily_returns(data_dict, codes)
    if len(daily_returns) < 20:
        return {"status": "skipped", "reason": "策略日收益样本不足"}

    return {
        "status": "ok",
        "overall": _segment_stats(daily_returns),
        "by_year": _by_year(daily_returns),
        "by_regime": _by_regime(daily_returns, data_dict),
        "historical_windows": _historical_windows(daily_returns),
    }
