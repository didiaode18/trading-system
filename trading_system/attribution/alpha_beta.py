"""
Alpha/Beta归因
==============
CAPM回归分离选股能力(Alpha)和择时能力(Beta)
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def calc_alpha_beta_attribution(strategy_returns: pd.Series,
                                benchmark_returns: pd.Series) -> dict:
    """
    CAPM归因: R_strategy = Alpha + Beta * R_benchmark + epsilon
    
    返回:
        {
            "alpha": 年化Alpha,
            "beta": Beta系数,
            "r_squared": 拟合优度,
            "alpha_contribution": Alpha对收益的贡献,
            "beta_contribution": Beta对收益的贡献,
        }
    """
    if len(strategy_returns) < 10 or len(benchmark_returns) < 10:
        return {"alpha": 0, "beta": 0, "r_squared": 0,
                "alpha_contribution": 0, "beta_contribution": 0}

    n = min(len(strategy_returns), len(benchmark_returns))
    y = strategy_returns.values[:n]
    x = benchmark_returns.values[:n]

    # OLS: y = alpha + beta*x
    X = np.column_stack([np.ones(n), x])
    try:
        params = np.linalg.lstsq(X, y, rcond=None)[0]
        alpha_daily, beta = params[0], params[1]
    except Exception:
        return {"alpha": 0, "beta": 0, "r_squared": 0,
                "alpha_contribution": 0, "beta_contribution": 0}

    # R²
    y_pred = X @ params
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    # 年化
    alpha_annual = alpha_daily * 252

    # 收益分解
    total_return = y.sum()
    beta_return = beta * x.sum()
    alpha_return = total_return - beta_return

    return {
        "alpha": round(alpha_annual, 4),
        "beta": round(beta, 4),
        "r_squared": round(r_squared, 4),
        "alpha_contribution": round(alpha_return, 4),
        "beta_contribution": round(beta_return, 4),
        "total_return": round(total_return, 4),
    }


def timing_attribution(strategy_returns: pd.Series,
                       benchmark_returns: pd.Series) -> dict:
    """
    择时能力评估: Beta在市场上涨时是否更高
    
    T-M模型: R_p = a + b1*R_m + b2*R_m² + e
    b2>0 表示有择时能力
    """
    if len(strategy_returns) < 20:
        return {"timing_ability": 0, "has_timing": False}

    n = min(len(strategy_returns), len(benchmark_returns))
    y = strategy_returns.values[:n]
    x = benchmark_returns.values[:n]
    x2 = x ** 2

    X = np.column_stack([np.ones(n), x, x2])
    try:
        params = np.linalg.lstsq(X, y, rcond=None)[0]
        timing_coef = params[2]  # b2
    except Exception:
        return {"timing_ability": 0, "has_timing": False}

    return {
        "timing_ability": round(timing_coef, 4),
        "has_timing": timing_coef > 0.5,  # b2显著为正
    }
