"""
风险平价配置
============
w_i = (1/σ_i) / Σ(1/σ_j)
每只股票对组合的风险贡献相等
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def risk_parity_weights(volatilities: dict, max_weight: float = None) -> dict:
    """
    风险平价权重计算
    
    参数:
        volatilities: {code: 年化波动率} 如 {"002371": 0.35, "600584": 0.28}
        max_weight: 单只最大权重（默认自适应: max(0.15, 0.9/n)）
    
    返回:
        {code: weight} 权重字典（总和=1）
    """
    if not volatilities:
        return {}

    codes = list(volatilities.keys())
    n = len(codes)
    vols = np.array([volatilities[c] for c in codes])

    # 自适应max_weight: 至少允许略高于等权
    if max_weight is None:
        max_weight = max(0.15, 0.9 / n)

    # 避免除零
    vols = np.maximum(vols, 0.01)

    # 风险平价: w_i = (1/σ_i) / Σ(1/σ_j)
    inv_vol = 1.0 / vols
    weights = inv_vol / inv_vol.sum()

    # 限制单只最大权重
    weights = np.minimum(weights, max_weight)
    # 重新归一化
    weights = weights / weights.sum()

    return {codes[i]: round(float(weights[i]), 4) for i in range(n)}


def risk_parity_from_returns(returns_dict: dict, lookback: int = 60,
                             max_weight: float = 0.15) -> dict:
    """
    从收益率数据计算风险平价权重
    
    参数:
        returns_dict: {code: Series(日收益率)}
        lookback: 回看天数
        max_weight: 单只最大权重
    
    返回:
        {code: weight}
    """
    vols = {}
    for code, returns in returns_dict.items():
        if returns is not None and len(returns) >= 20:
            recent = returns.tail(lookback)
            annual_vol = recent.std() * np.sqrt(252)
            vols[code] = annual_vol

    return risk_parity_weights(vols, max_weight)


def equal_risk_contribution(returns_df: pd.DataFrame, max_weight: float = 0.15) -> dict:
    """
    等风险贡献（ERC）- 风险平价的协方差版本
    
    参数:
        returns_df: DataFrame (columns=stocks, rows=dates)
    
    返回:
        {code: weight}
    """
    if returns_df.empty or returns_df.shape[1] < 2:
        return {}

    cov = returns_df.cov() * 252  # 年化协方差矩阵
    codes = list(cov.columns)
    n = len(codes)

    # 简化版：用对角线（各自方差）近似
    vols = np.sqrt(np.diag(cov.values))
    vols = np.maximum(vols, 0.01)

    inv_vol = 1.0 / vols
    weights = inv_vol / inv_vol.sum()
    weights = np.minimum(weights, max_weight)
    weights = weights / weights.sum()

    return {codes[i]: round(float(weights[i]), 4) for i in range(n)}
