# -*- coding: utf-8 -*-
"""
Stage 10 · 成本敏感性（full-history-backtest 新增）
====================================================
四档场景同策略回测：零成本 / 当前成本 / 成本×2 / 滑点×3。
输出各档夏普与年化，计算"成本脆弱性"（成本×2 后夏普退化幅度）。

准入门 G4：成本×2 后夏普退化 < 30%，否则策略依赖低换手/低滑点假设，实盘风险高。

复用而非重写：回测走 backtest_optimizer.backtest_stock_v6，其佣金/滑点为
模块级常量（调用时读取），本模块通过 monkeypatch 这些常量实现四档，
不复制出第二套撮合/费率代码；夏普走 master_common。
成本脆弱性判定抽为纯函数 cost_degradation，便于单元测试构造"成本敏感"坏样本。
"""

import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

try:
    from . import master_common
except ImportError:  # pragma: no cover
    from backtest import master_common

logger = logging.getLogger(__name__)

MBC = config.MASTER_BACKTEST_CONFIG if hasattr(config, "MASTER_BACKTEST_CONFIG") else {}
GATES = MBC.get("gates", {})
COST_DEGRADE_MAX = GATES.get("G4_cost_degradation_max", 0.30)


def cost_degradation(sharpe_current: float, sharpe_x2: float) -> dict:
    """成本脆弱性判定纯函数（可独立测试）。

    参数:
        sharpe_current: 当前成本档夏普
        sharpe_x2: 成本×2 档夏普
    返回:
        {sharpe_degradation_x2, verdict, threshold}
        verdict ∈ {"robust", "fragile", "degenerate"}
        sharpe_degradation_x2: 相对退化比例（可 >1，可为负表示更优）
    """
    if sharpe_current is None or sharpe_x2 is None or not np.isfinite(sharpe_current) \
            or not np.isfinite(sharpe_x2):
        return {"sharpe_degradation_x2": None, "verdict": "insufficient",
                "threshold": COST_DEGRADE_MAX}
    if sharpe_current <= 0:
        # 当前档本身非正夏普：退化比例无意义，直接判脆弱
        return {"sharpe_degradation_x2": 1.0 if sharpe_x2 < sharpe_current else 0.0,
                "verdict": "degenerate", "threshold": COST_DEGRADE_MAX}
    deg = (sharpe_current - sharpe_x2) / abs(sharpe_current)
    if deg < COST_DEGRADE_MAX:
        verdict = "robust"
    elif deg < 0.60:
        verdict = "fragile"
    else:
        verdict = "degenerate"
    return {"sharpe_degradation_x2": round(float(deg), 4),
            "verdict": verdict, "threshold": COST_DEGRADE_MAX}


# 四档成本倍率（作用于 backtest_optimizer 的模块级费率常量）
_TIERS = [
    ("zero", 0.0, 0.0),        # 零成本对照：佣金 0、滑点 0
    ("current", 1.0, 1.0),     # 当前成本
    ("cost_x2", 2.0, 1.0),     # 佣金/印花税等 ×2
    ("slippage_x3", 1.0, 3.0), # 滑点 ×3
]


def _run_tier(data_dict: dict, codes: list, params: dict,
              comm_mult: float, slip_mult: float) -> dict:
    """在指定成本倍率下跑 v6 回测，返回 {sharpe, annual, n_trades, win_rate, total_return}。"""
    import backtest_optimizer as bo
    # 记录原值 → monkeypatch → 恢复
    orig_comm = bo.COMMISSION
    orig_sl = bo.SLIPPAGE_LEADER
    orig_sf = bo.SLIPPAGE_FLEX
    bo.COMMISSION = orig_comm * comm_mult
    bo.SLIPPAGE_LEADER = orig_sl * slip_mult
    bo.SLIPPAGE_FLEX = orig_sf * slip_mult
    try:
        all_trades = []
        for code in codes:
            df = data_dict.get(code)
            if df is None or len(df) < 80:
                continue
            try:
                info = _stock_info(code)
                all_trades.extend(bo.backtest_stock_v6(df.copy(), code, info, params))
            except Exception as e:
                logger.debug(f"[Stage10] {code} 回测失败: {e}")
    finally:
        bo.COMMISSION = orig_comm
        bo.SLIPPAGE_LEADER = orig_sl
        bo.SLIPPAGE_FLEX = orig_sf

    rets = master_common.trades_to_return_series(all_trades)
    if len(rets) < 2:
        return {"sharpe": 0.0, "annual": 0.0, "n_trades": len(all_trades),
                "win_rate": 0.0, "total_return": 0.0, "note": "样本不足"}
    avg_hold = float(np.mean([t.get("hold_days", 5) for t in all_trades])) if all_trades else 5.0
    sharpe = master_common.sharpe_from_trade_returns(rets, avg_hold)
    wins = int((rets > 0).sum())
    return {
        "sharpe": round(float(sharpe), 4),
        "annual": round(float(np.mean(rets) * (252.0 / max(1.0, avg_hold))), 4),
        "n_trades": len(all_trades),
        "win_rate": round(wins / len(rets), 4),
        "total_return": round(float(np.sum(rets)), 4),
    }


def _stock_info(code: str) -> dict:
    info = {}
    try:
        info = config.get_stock_info(code) or {}
    except Exception:
        info = {}
    return {
        "名称": info.get("名称", code),
        "类型": info.get("类型", "龙头"),
        "行业": info.get("行业", info.get("赛道", "其他")),
    }


def run_cost_ladder(data_dict: dict, codes: list, quick: bool = False,
                    params: dict = None) -> dict:
    """成本敏感性主入口。

    返回:
      {status, tiers: {zero,current,cost_x2,slippage_x3},
       sharpe_degradation_x2, verdict}
    """
    if not codes or not data_dict:
        return {"status": "skipped", "reason": "无标的/无数据"}
    if quick:
        codes = codes[:6]
    params = params or {}

    tiers = {}
    for name, cm, sm in _TIERS:
        try:
            tiers[name] = _run_tier(data_dict, codes, params, cm, sm)
        except Exception as e:
            tiers[name] = {"error": str(e)}

    cur = tiers.get("current", {})
    x2 = tiers.get("cost_x2", {})
    deg = cost_degradation(cur.get("sharpe"), x2.get("sharpe"))

    return {
        "status": "ok",
        "tiers": tiers,
        "sharpe_degradation_x2": deg["sharpe_degradation_x2"],
        "verdict": deg["verdict"],
        "threshold": deg["threshold"],
    }
