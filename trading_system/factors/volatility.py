# -*- coding: utf-8 -*-
"""
低波动因子（Low Volatility Factor）
=====================================
核心逻辑: 低波动异象，长期复利跑赢高波动

构造:
1. vol_20d = std(daily_return, 20)
2. vol_60d = std(daily_return, 60)
3. max_dd_20d = 近20日最大回撤
4. beta = cov(stock_ret, index_ret) / var(index_ret)
5. 因子 = -(0.4*vol_20d + 0.3*vol_60d + 0.2*max_dd + 0.1*beta)

方向: 正向（低波动得分高 = 波动率低）
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_low_volatility(df: pd.DataFrame,
                        index_df: pd.DataFrame = None) -> pd.Series:
    """
    低波动因子

    参数:
        df: 含 close 列的DataFrame
        index_df: 基准指数数据（含 close 列），用于计算beta。None则跳过beta

    返回:
        pd.Series（与df等长），值越大表示波动越低
    """
    close = df["close"].values
    n = len(close)
    result = np.full(n, np.nan)

    if n < 61:
        return pd.Series(result, index=df.index)

    # 日收益率
    returns = np.zeros(n)
    for i in range(1, n):
        returns[i] = (close[i] - close[i-1]) / close[i-1] if close[i-1] > 0 else 0

    # 指数收益率（如有）
    if index_df is not None and len(index_df) >= n:
        idx_close = index_df["close"].values[-n:]
        idx_returns = np.zeros(n)
        for i in range(1, n):
            idx_returns[i] = (idx_close[i] - idx_close[i-1]) / idx_close[i-1] if idx_close[i-1] > 0 else 0
    else:
        idx_returns = None

    for i in range(60, n):
        # 20日波动率
        vol_20 = np.std(returns[i-19:i+1])

        # 60日波动率
        vol_60 = np.std(returns[i-59:i+1])

        # 20日最大回撤
        peak = close[i-19]
        max_dd = 0
        for j in range(i-19, i+1):
            if close[j] > peak:
                peak = close[j]
            dd = (peak - close[j]) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # Beta（如有指数数据）
        beta = 1.0
        if idx_returns is not None:
            stock_ret = returns[i-59:i+1]
            index_ret = idx_returns[i-59:i+1]
            cov = np.mean(stock_ret * index_ret) - np.mean(stock_ret) * np.mean(index_ret)
            var_idx = np.var(index_ret)
            if var_idx > 0:
                beta = cov / var_idx

        # 综合低波动得分（取反，低波动=高分）
        result[i] = -(0.4 * vol_20 + 0.3 * vol_60 + 0.2 * max_dd + 0.1 * abs(beta - 1))

    return pd.Series(result, index=df.index)


# 因子注册字典
VOLATILITY_FACTORS = {
    "low_volatility": {
        "func": calc_low_volatility,
        "desc": "低波动因子(20d/60d波动+回撤+beta)",
        "dir": 1,
    },
}
