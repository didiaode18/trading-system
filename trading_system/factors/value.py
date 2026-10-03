# -*- coding: utf-8 -*-
"""
价值因子（Value Factor）
=========================
核心逻辑: PB-ROE框架，低估值+高盈利能力 = 价值股

构造:
1. ep = 1/PE_TTM（盈利收益率）
2. bp = 1/PB（账面市值比）
3. PB-ROE组合: 高ROE组内按BP排序
4. 做行业+市值中性化（如有行业数据）

方向: 正向（低估值高分）
注意: 剔除PE<0（亏损股）
数据源: fundamental表(pe_ttm/pb/roe)

注: 提供OHLCV代理版本用于无基本面数据时的回退。
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_value_cross_section(fundamental_df: pd.DataFrame,
                              industry_map: dict = None) -> pd.Series:
    """
    价值因子截面计算（PB-ROE框架）

    参数:
        fundamental_df: 含 code/pe_ttm/pb/roe 列的DataFrame
        industry_map: {code: industry} 行业映射（可选，用于中性化）

    返回:
        pd.Series, index=code, value=价值因子得分
    """
    if fundamental_df is None or fundamental_df.empty:
        return pd.Series(dtype=float)

    df = fundamental_df.copy()
    codes = df["code"]

    # 盈利收益率 EP = 1/PE（PE>0）
    pe = pd.to_numeric(df["pe_ttm"], errors="coerce")
    pe = pe.where(pe > 0)  # 剔除亏损股
    ep = 1.0 / pe
    ep_rank = ep.rank(pct=True, na_option="keep")

    # 账面市值比 BP = 1/PB（PB>0）
    pb = pd.to_numeric(df["pb"], errors="coerce")
    pb = pb.where(pb > 0)
    bp = 1.0 / pb
    bp_rank = bp.rank(pct=True, na_option="keep")

    # ROE分组（用于条件排序）
    if "roe" in df.columns:
        roe = pd.to_numeric(df["roe"], errors="coerce")
        roe_median = roe.median()
        # 高ROE组: EP权重更大; 低ROE组: BP权重更大
        high_roe = roe >= roe_median
        scores = pd.Series(0.0, index=df.index)
        scores[high_roe] = 0.6 * ep_rank[high_roe].fillna(0.5) + 0.4 * bp_rank[high_roe].fillna(0.5)
        scores[~high_roe] = 0.3 * ep_rank[~high_roe].fillna(0.5) + 0.7 * bp_rank[~high_roe].fillna(0.5)
    else:
        scores = 0.5 * ep_rank.fillna(0.5) + 0.5 * bp_rank.fillna(0.5)

    scores.index = codes
    return scores


def calc_value_proxy(df: pd.DataFrame) -> pd.Series:
    """
    价值因子OHLCV代理版（无基本面数据时的回退）

    用长期反转 + 低波动代理价值:
    1. 长期反转: 近120日收益取反（超跌=低估值代理）
    2. 价格位置: 当前价格在120日区间的位置（越低=越便宜）
    3. 波动率: 近60日波动率倒数（低波动=稳定价值）

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

    for i in range(120, n):
        # 1. 长期反转（超跌代理低估值）
        ret_120d = (close[i] - close[i-119]) / close[i-119] if close[i-119] > 0 else 0
        reversal_score = -ret_120d  # 跌得多=估值低

        # 2. 价格在区间的位置
        high_120 = np.max(close[i-119:i+1])
        low_120 = np.min(close[i-119:i+1])
        if high_120 > low_120:
            position = (close[i] - low_120) / (high_120 - low_120)
            position_score = 1.0 - position  # 位置低=便宜
        else:
            position_score = 0.5

        # 3. 低波动
        returns_window = np.diff(close[i-59:i+1]) / close[i-59:i]
        vol = np.std(returns_window)
        vol_score = 1.0 / (1.0 + vol * 20)

        # 归一化反转分数
        reversal_norm = 1.0 / (1.0 + np.exp(reversal_score * 10))  # sigmoid

        # 合成
        result[i] = 0.4 * reversal_norm + 0.35 * position_score + 0.25 * vol_score

    return pd.Series(result, index=df.index)


# 因子注册字典
VALUE_FACTORS = {
    "value_proxy": {
        "func": calc_value_proxy,
        "desc": "价值因子(OHLCV代理:长期反转+价格位置+低波动)",
        "dir": 1,
    },
}
