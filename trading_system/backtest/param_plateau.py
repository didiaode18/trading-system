# -*- coding: utf-8 -*-
"""
Stage 5 · 参数网格 + 高原检测（full-history-backtest）
======================================================
1. 只扫 2-3 个核心参数（初始止损/信号质量门槛/龙头回落止盈），
   逐组合复用 backtest_optimizer.backtest_stock_v6 回测，记录每个 trial 的夏普
   （trial 总数供 Stage 8 DSR 作多重检验次数）；
2. 参数高原检测（新增）：对最优点的全部相邻参数（各轴 ±1 档），
   邻域平均退化 < 15% → "高原（稳健）"；> 30% → "孤点（过拟合）"；
3. 输出二维切片热力图数据与高原/孤点判定。

复用而非重写：回测走 backtest_optimizer.backtest_stock_v6，夏普走 master_common。
高原判定抽为纯函数 judge_plateau，便于单元测试构造"孤点"坏样本。
"""

import itertools
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
PLATEAU_MAX = GATES.get("G5_plateau_degradation_max", 0.15)
ISOLATED_MIN = GATES.get("G5_isolated_degradation", 0.30)


def judge_plateau(best_sharpe: float, neighbor_sharpes: list,
                  plateau_max: float = None, isolated_min: float = None) -> dict:
    """高原判定纯函数（可独立测试）。

    参数:
        best_sharpe: 最优点夏普
        neighbor_sharpes: 最优点各相邻参数（±1 档）的夏普列表
        plateau_max/isolated_min: 阈值（默认取 config）

    返回:
        {neighbor_degradation, verdict, n_neighbors}
        verdict ∈ {"plateau", "marginal", "isolated", "insufficient"}
        neighbor_degradation: 邻域相对最优点的平均退化比例（>=0，越大越像孤点）
    """
    plateau_max = PLATEAU_MAX if plateau_max is None else plateau_max
    isolated_min = ISOLATED_MIN if isolated_min is None else isolated_min

    neighbors = [s for s in neighbor_sharpes if s is not None and np.isfinite(s)]
    if not neighbors or best_sharpe is None or not np.isfinite(best_sharpe):
        return {"neighbor_degradation": None, "verdict": "insufficient", "n_neighbors": len(neighbors)}

    if best_sharpe <= 0:
        # 最优点本身非正夏普：邻域只要不更好就算不上高原，直接判孤点风险
        degradation = 1.0 if max(neighbors) < best_sharpe else 0.0
        verdict = "isolated" if degradation > isolated_min else "marginal"
        return {"neighbor_degradation": round(float(degradation), 4),
                "verdict": verdict, "n_neighbors": len(neighbors)}

    # 平均退化 = mean(max(0, (best - neighbor) / best))
    degs = [max(0.0, (best_sharpe - s) / best_sharpe) for s in neighbors]
    degradation = float(np.mean(degs))

    if degradation < plateau_max:
        verdict = "plateau"
    elif degradation > isolated_min:
        verdict = "isolated"
    else:
        verdict = "marginal"

    return {"neighbor_degradation": round(degradation, 4),
            "verdict": verdict, "n_neighbors": len(neighbors)}


def _stock_info(code: str) -> dict:
    """构造 v6 需要的 info 字典（映射 赛道→行业）。"""
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


def _run_trial(data_dict: dict, codes: list, params: dict) -> list:
    """用指定参数跑全部标的的 v6 回测，返回合并 trades。"""
    from backtest_optimizer import backtest_stock_v6
    all_trades = []
    for code in codes:
        df = data_dict.get(code)
        if df is None or len(df) < 80:
            continue
        try:
            trades = backtest_stock_v6(df.copy(), code, _stock_info(code), params)
            all_trades.extend(trades)
        except Exception as e:
            logger.debug(f"[Stage5] {code} trial 回测失败: {e}")
    return all_trades


def _trial_sharpe(trades: list) -> float:
    """由 trades 计算年化夏普代理。"""
    rets = master_common.trades_to_return_series(trades)
    if len(rets) < 2:
        return 0.0
    avg_hold = float(np.mean([t.get("hold_days", 5) for t in trades])) if trades else 5.0
    return master_common.sharpe_from_trade_returns(rets, avg_hold)


def _build_grid(quick: bool, param_grid: dict) -> dict:
    """按 quick/full 限制网格规模（保证 product <= grid_max_trials）。"""
    limits = MBC.get("quick" if quick else "full", {})
    max_trials = limits.get("grid_max_trials", 48)
    grid = dict(param_grid or MBC.get("param_grid", {}))
    if not grid:
        grid = {"initial_stop_loss": [0.06, 0.08, 0.10]}
    # 若组合数超限，逐轴裁剪取值
    keys = list(grid.keys())
    while np.prod([len(grid[k]) for k in keys]) > max_trials and keys:
        # 优先裁剪取值最多的轴
        k = max(keys, key=lambda kk: len(grid[kk]))
        if len(grid[k]) <= 2:
            keys.remove(k)
            continue
        grid[k] = grid[k][:-1]
    return grid


def run_grid_with_plateau(data_dict: dict, codes: list, quick: bool = False,
                          param_grid: dict = None) -> dict:
    """参数网格 + 高原检测主入口。

    返回:
      {status, best_params, best_sharpe, all_sharpes, n_trials,
       neighbor_degradation, verdict, heatmap, neighbors}
    """
    if not codes or not data_dict:
        return {"status": "skipped", "reason": "无标的/无数据"}

    if quick:
        codes = codes[:6]

    grid = _build_grid(quick, param_grid)
    axes = list(grid.keys())
    values = [grid[a] for a in axes]
    combos = list(itertools.product(*values))

    # 逐 trial 回测，记录夏普（按坐标索引存储，便于找邻居）
    sharpes = {}
    all_sharpes = []
    for pos, combo in enumerate(combos):
        params = dict(zip(axes, combo))
        trades = _run_trial(data_dict, codes, params)
        sh = _trial_sharpe(trades)
        sharpes[pos] = sh
        all_sharpes.append(sh)

    if not all_sharpes or max(all_sharpes) <= -1e9:
        return {"status": "fail", "error": "网格无任何有效夏普（可能全程无交易）",
                "all_sharpes": all_sharpes, "n_trials": len(combos)}

    best_pos = int(np.argmax(all_sharpes))
    best_sharpe = all_sharpes[best_pos]
    best_params = dict(zip(axes, combos[best_pos]))
    best_coord = combos[best_pos]          # 最优点的取值元组
    # 最优点在各轴上的"位置索引"（邻居查找必须用索引而非取值）
    best_idx = [values[i].index(best_coord[i]) for i in range(len(axes))]

    # 找各轴 ±1 档邻居
    neighbor_sharpes = []
    neighbors = []
    for ax_i in range(len(axes)):
        for step in (-1, 1):
            j = best_idx[ax_i] + step
            if 0 <= j < len(values[ax_i]):
                nb_idx = list(best_idx)
                nb_idx[ax_i] = j
                nb_coord = tuple(values[i][nb_idx[i]] for i in range(len(axes)))
                nb_pos = combos.index(nb_coord)
                nb_sh = sharpes.get(nb_pos)
                neighbor_sharpes.append(nb_sh)
                neighbors.append({
                    "param": axes[ax_i],
                    "value": values[ax_i][j],
                    "sharpe": round(nb_sh, 4) if nb_sh is not None else None,
                })

    plateau = judge_plateau(best_sharpe, neighbor_sharpes)

    # 二维热力图切片（取前两轴，其余固定为最优）
    heatmap = _build_heatmap(axes, values, combos, sharpes, best_coord)

    return {
        "status": "ok",
        "axes": axes,
        "grid": grid,
        "best_params": best_params,
        "best_sharpe": round(float(best_sharpe), 4),
        "all_sharpes": [round(float(s), 4) for s in all_sharpes],
        "n_trials": len(combos),
        "neighbor_degradation": plateau["neighbor_degradation"],
        "verdict": plateau["verdict"],
        "neighbors": neighbors,
        "heatmap": heatmap,
    }


def _build_heatmap(axes, values, combos, sharpes, best_coord) -> dict:
    """构造前两轴的二维夏普切片（其余轴固定为最优点取值）。"""
    if len(axes) < 2:
        return {}
    a0, a1 = axes[0], axes[1]
    fixed = {axes[i]: best_coord[i] for i in range(2, len(axes))}
    matrix = []
    for v0 in values[0]:
        row = []
        for v1 in values[1]:
            target = {a0: v0, a1: v1, **fixed}
            combo = tuple(target[a] for a in axes)
            pos = combos.index(combo) if combo in combos else None
            row.append(round(sharpes[pos], 4) if pos is not None else None)
        matrix.append(row)
    return {"x_axis": a1, "y_axis": a0,
            "x_values": list(values[1]), "y_values": list(values[0]),
            "matrix": matrix}
