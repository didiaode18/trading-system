# -*- coding: utf-8 -*-
"""
Stage 7 补充 · 日度 Block Bootstrap（full-history-backtest 新增）
================================================================
对策略日收益序列按 5-10 日"块"重采样生成净值路径，保留波动聚集性
（volatility clustering），输出收益与最大回撤的置信区间，
与 monte_carlo.py 的逐笔随机化结果交叉印证。

原理:
  逐笔 permutation 破坏了收益的时序自相关；block bootstrap 以连续块为
  重采样单位，近似保留短期波动聚集，是压力区间的更保守估计。

复用而非重写：与 monte_carlo 并列，不替代；随机种子固定保证可复现。
"""

import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)


def block_bootstrap_daily(daily_returns, block_size: int = 5,
                          n_paths: int = 1000, seed: int = 42,
                          initial_capital: float = 1.0) -> dict:
    """日度块自助法压力区间。

    参数:
        daily_returns: 日收益率序列（array-like 或 pd.Series）
        block_size: 重采样块长度（5-10 日，保留波动聚集）
        n_paths: 模拟路径数
        seed: 随机种子（可复现）
        initial_capital: 初始净值基准（默认 1.0，输出相对值）

    返回:
        {n_paths, block_size, n_obs,
         return_ci: {p5, p50, p95},
         max_drawdown_ci: {p50, p95},
         ruin_probability}  或 {error}
    """
    rets = np.asarray([float(x) for x in (daily_returns.tolist()
                                          if hasattr(daily_returns, "tolist")
                                          else daily_returns)], dtype=float)
    rets = rets[np.isfinite(rets)]
    if len(rets) < max(20, block_size * 2):
        return {"error": f"日收益样本不足（{len(rets)}），无法做 block bootstrap"}

    block_size = int(max(2, min(block_size, len(rets) // 2)))
    rng = np.random.default_rng(seed)
    n = len(rets)
    n_blocks = int(np.ceil(n / block_size))

    final_returns = np.empty(n_paths)
    max_dds = np.empty(n_paths)
    ruin_hits = np.zeros(n_paths, dtype=bool)

    max_start = n - block_size
    for p in range(n_paths):
        # 随机取 n_blocks 个块起点，拼接后截断到 n
        starts = rng.integers(0, max_start + 1, size=n_blocks)
        path = np.concatenate([rets[s:s + block_size] for s in starts])[:n]
        equity = np.cumprod(1.0 + path) * initial_capital
        final_returns[p] = equity[-1] / initial_capital - 1.0
        peak = np.maximum.accumulate(equity)
        dd = (peak - equity) / peak
        max_dds[p] = dd.max()
        ruin_hits[p] = np.any(equity < initial_capital * 0.5)

    return {
        "n_paths": int(n_paths),
        "block_size": block_size,
        "n_obs": int(n),
        "return_ci": {
            "p5": round(float(np.percentile(final_returns, 5)), 4),
            "p50": round(float(np.percentile(final_returns, 50)), 4),
            "p95": round(float(np.percentile(final_returns, 95)), 4),
        },
        "max_drawdown_ci": {
            "p50": round(float(np.percentile(max_dds, 50)), 4),
            "p95": round(float(np.percentile(max_dds, 95)), 4),
        },
        "ruin_probability": round(float(ruin_hits.mean()), 4),
        "seed": seed,
    }
