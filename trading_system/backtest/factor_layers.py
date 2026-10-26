# -*- coding: utf-8 -*-
"""
Stage 12 · 因子分层回测（full-history-backtest）
==================================================
对核心因子按分位数分 N 组（默认 5 组），按调仓频率滚动持有，
输出各组年化收益、多空组合收益/夏普、截面 IC/ICIR/单调性。

复用而非重写：
  - 因子计算走 factors.registry（get_registry().compute）；
  - 分层回测走 backtest.decile_backtest.DecileBacktest；
数据不足（可用标的过少/日期过短/因子未注册）静默降级并如实标注，禁止编造。

无未来函数：分组用 T 日因子值，收益用 T→T+forward_days 的前瞻收益。
"""

import logging
import os
import sys

import numpy as np
import pandas as pd
import config

try:
    from .decile_backtest import DecileBacktest
except ImportError:  # pragma: no cover
    from backtest.decile_backtest import DecileBacktest

logger = logging.getLogger(__name__)

MBC = config.MASTER_BACKTEST_CONFIG if hasattr(config, "MASTER_BACKTEST_CONFIG") else {}
FL = MBC.get("factor_layer", {})
N_GROUPS = FL.get("n_groups", 5)
FORWARD_DAYS = FL.get("forward_days", 20)
REBALANCE_FREQ = FL.get("rebalance_freq", 20)
FACTORS = FL.get("factors", ["mom_20d", "ma20_bias", "vol_ratio", "turnover_rate"])
MIN_STOCKS = FL.get("min_stocks", 8)


def _build_panels(data_dict: dict):
    """构造收盘价面板与前瞻收益面板（index=date, columns=code）。"""
    close = {}
    for code, df in data_dict.items():
        if df is None or len(df) < 60 or "close" not in df.columns or "date" not in df.columns:
            continue
        s = df.copy()
        s["date"] = s["date"].astype(str)
        s = s.dropna(subset=["close"]).drop_duplicates(subset=["date"]).set_index("date")["close"]
        close[code] = s.astype(float)
    if len(close) < MIN_STOCKS:
        return None, None, len(close)
    close_panel = pd.DataFrame(close).sort_index()
    # 前瞻收益：T→T+forward_days（用未来收盘，仅作分组后收益度量，不进入信号）
    fwd = close_panel.shift(-FORWARD_DAYS) / close_panel - 1.0
    return close_panel, fwd, len(close)


def _factor_panel(registry, name: str, data_dict: dict, close_panel: pd.DataFrame):
    """计算某因子的截面面板（index=date, columns=code）。"""
    cols = {}
    for code, df in data_dict.items():
        if code not in close_panel.columns or df is None or len(df) < 60:
            continue
        try:
            ser = registry.compute(name, df.copy())
        except Exception:
            ser = None
        if ser is None or len(ser) == 0:
            continue
        s = df[["date"]].copy()
        s["date"] = s["date"].astype(str)
        s["_f"] = np.asarray(ser, dtype=float)[:len(s)]
        s = s.dropna(subset=["_f"]).drop_duplicates(subset=["date"]).set_index("date")["_f"]
        if len(s) > 0:
            cols[code] = s
    if len(cols) < MIN_STOCKS:
        return None
    panel = pd.DataFrame(cols).sort_index()
    return panel.reindex(close_panel.index)


def _summarize_result(name: str, res) -> dict:
    groups = []
    for g in getattr(res, "groups", []) or []:
        groups.append({
            "group": int(getattr(g, "group", 0)),
            "annual_return": round(float(getattr(g, "annual_return", 0)), 4),
            "cum_return": round(float(getattr(g, "cum_return", 0)), 4),
            "sharpe": round(float(getattr(g, "sharpe", 0)), 4),
            "max_drawdown": round(float(getattr(g, "max_drawdown", 0)), 4),
            "win_rate": round(float(getattr(g, "win_rate", 0)), 4),
            "avg_turnover": round(float(getattr(g, "avg_turnover", 0)), 4),
        })
    return {
        "status": "ok",
        "factor": name,
        "n_groups": int(getattr(res, "n_groups", N_GROUPS)),
        "groups": groups,
        "long_short_return": round(float(getattr(res, "long_short_return", 0)), 4),
        "long_short_sharpe": round(float(getattr(res, "long_short_sharpe", 0)), 4),
        "ic_mean": round(float(getattr(res, "ic_mean", 0)), 4),
        "icir": round(float(getattr(res, "icir", 0)), 4),
        "t_stat": round(float(getattr(res, "t_stat", 0)), 4),
        "monotonicity_score": round(float(getattr(res, "monotonicity_score", 0)), 4),
        "n_stocks": int(getattr(res, "n_stocks", 0)),
    }


def _govern_factor(rec: dict) -> dict:
    """P1-3: 单因子 IC 治理判定。

    基于业界常用阈值将原始 IC 指标转为可执行的保留/剔除建议：
      effective : |IC|>=0.03 且 |ICIR|>=0.3 且 |t|>=2（显著且稳定）
      weak      : |IC|>=0.02（方向性弱，需观察）
      ineffective: 其余（无预测力，建议剔除）
    另标注 IC 符号：负 IC 说明因子方向应反转使用。
    """
    ic = abs(float(rec.get("ic_mean", 0) or 0))
    icir = abs(float(rec.get("icir", 0) or 0))
    t = abs(float(rec.get("t_stat", 0) or 0))
    if ic >= 0.03 and icir >= 0.3 and t >= 2.0:
        verdict = "effective"
    elif ic >= 0.02:
        verdict = "weak"
    else:
        verdict = "ineffective"
    sign = "negative" if float(rec.get("ic_mean", 0) or 0) < 0 else "positive"
    return {"ic_verdict": verdict, "ic_sign": sign,
            "suggest": "keep" if verdict == "effective" else (
                "watch" if verdict == "weak" else "drop"),
            "invert_if_used": sign == "negative"}


def run_factor_layering(data_dict: dict) -> dict:
    """因子分层回测主入口。

    返回:
      {status, factors: {name: {...}}, skipped: [name...], note}
    """
    if not data_dict:
        return {"status": "skipped", "reason": "无数据"}
    close_panel, fwd, n_stocks = _build_panels(data_dict)
    if close_panel is None:
        return {"status": "skipped",
                "reason": f"可用标的不足（{n_stocks} < {MIN_STOCKS}），因子分层静默降级"}

    try:
        from factors.registry import get_registry
        registry = get_registry()
    except Exception as e:
        return {"status": "skipped", "reason": f"因子注册表不可用: {e}"}

    results = {}
    skipped = []
    for name in FACTORS:
        try:
            if registry.get(name) is None:
                skipped.append({"factor": name, "reason": "因子未注册"})
                continue
            fpanel = _factor_panel(registry, name, data_dict, close_panel)
            if fpanel is None:
                skipped.append({"factor": name, "reason": "有效截面标的不足"})
                continue
            bt = DecileBacktest(fpanel, fwd, n_groups=N_GROUPS,
                                rebalance_freq=REBALANCE_FREQ, min_stocks_per_group=2)
            res = bt.run()
            if not getattr(res, "groups", None):
                skipped.append({"factor": name, "reason": "分层回测无有效分组（日期/标的不足）"})
                continue
            results[name] = _summarize_result(name, res)
            # P1-3: 附加 IC 治理判定
            results[name].update(_govern_factor(results[name]))
        except Exception as e:
            skipped.append({"factor": name, "reason": f"计算异常: {e}"})
            logger.debug(f"[Stage12] 因子 {name} 分层失败: {e}")

    n_ok = len(results)
    # P1-3: IC 治理汇总——有效/观察/剔除清单，供报告与后续因子库决策
    keep = [k for k, v in results.items() if v.get("suggest") == "keep"]
    watch = [k for k, v in results.items() if v.get("suggest") == "watch"]
    drop = [k for k, v in results.items() if v.get("suggest") == "drop"]
    governance = {
        "effective": keep, "weak": watch, "ineffective": drop,
        "n_effective": len(keep), "n_total": n_ok,
        "effective_ratio": round(len(keep) / n_ok, 4) if n_ok else 0.0,
        "verdict": ("因子体系有效" if n_ok and len(keep) / n_ok >= 0.5
                    else "因子体系覆盖不足：多数因子无预测力，需扩充/替换"),
        "note": ("negative IC 因子应反转使用；ineffective 因子建议从选股模型剔除。"
                 if results else "无有效因子结果"),
    }
    return {
        "status": "ok" if n_ok > 0 else "skipped",
        "n_stocks": n_stocks,
        "n_groups": N_GROUPS,
        "forward_days": FORWARD_DAYS,
        "rebalance_freq": REBALANCE_FREQ,
        "factors": results,
        "ic_governance": governance,
        "skipped": skipped,
        "note": ("部分因子因数据/注册不足已跳过并标注。"
                 if skipped else "全部配置因子分层完成。"),
    }
