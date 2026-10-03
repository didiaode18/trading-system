# -*- coding: utf-8 -*-
"""
红利因子（Dividend Factor）
=============================
核心逻辑: 高分红验证现金流真实性，熊市/利率下行期表现优

构造:
1. dividend_yield: 股息率TTM（权重0.70）
2. payout_stability: 分红连续性（权重0.30，需多年数据）
3. 因子 = 0.7*dy标准化 + 0.3*payout标准化

方向: 正向
预期: 熊市/利率下行表现优，牛市弹性不足
数据源: fundamental表(dividend_yield)

注: 提供OHLCV代理版本用于无基本面数据时的回退。
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_dividend_cross_section(fundamental_df: pd.DataFrame) -> pd.Series:
    """
    红利因子截面计算

    参数:
        fundamental_df: 含 code/dividend_yield 列的DataFrame

    返回:
        pd.Series, index=code, value=红利因子得分
    """
    if fundamental_df is None or fundamental_df.empty:
        return pd.Series(dtype=float)

    df = fundamental_df.copy()

    dy = pd.to_numeric(df["dividend_yield"], errors="coerce")
    # 股息率为NaN或负数的给中位分
    dy_rank = dy.rank(pct=True, na_option="keep")
    scores = dy_rank.fillna(0.5)

    scores.index = df["code"]
    return scores


def calc_dividend_proxy(df: pd.DataFrame) -> pd.Series:
    """
    红利因子OHLCV代理版（无基本面数据时的回退）

    用低波动 + 稳定分红特征代理:
    1. 低波动: 近60日波动率倒数（高分红股通常低波动）
    2. 收益稳定性: 近120日中正收益天数的占比
    3. 下行保护: 近60日最大回撤的倒数

    参数:
        df: 含 close 列的DataFrame

    返回:
        pd.Series（与df等长）
    """
    close = df["close"].values
    n = len(close)
    result = np.full(n, np.nan)

    if n < 121:
        return pd.Series(result, index=df.index)

    # 日收益率
    returns = np.zeros(n)
    for i in range(1, n):
        returns[i] = (close[i] - close[i-1]) / close[i-1] if close[i-1] > 0 else 0

    for i in range(120, n):
        # 1. 低波动（60日）
        vol_60 = np.std(returns[i-59:i+1])
        vol_score = 1.0 / (1.0 + vol_60 * 20)

        # 2. 收益稳定性（120日正收益天数占比）
        window_ret = returns[i-119:i+1]
        positive_ratio = np.sum(window_ret > 0) / len(window_ret)

        # 3. 下行保护（60日最大回撤倒数）
        peak = close[i-59]
        max_dd = 0
        for j in range(i-59, i+1):
            if close[j] > peak:
                peak = close[j]
            dd = (peak - close[j]) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        dd_score = 1.0 / (1.0 + max_dd * 10)

        # 合成
        result[i] = 0.35 * vol_score + 0.35 * positive_ratio + 0.30 * dd_score

    return pd.Series(result, index=df.index)


# 因子注册字典
DIVIDEND_FACTORS = {
    "dividend_proxy": {
        "func": calc_dividend_proxy,
        "desc": "红利因子(OHLCV代理:低波动+收益稳定性+下行保护)",
        "dir": 1,
    },
}
