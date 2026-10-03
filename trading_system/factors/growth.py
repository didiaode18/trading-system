# -*- coding: utf-8 -*-
"""
成长因子（Growth Factor）
==========================
核心逻辑: 盈利高速增长，CANSLIM的核心C因子

构造:
1. eps_growth_q: 单季度净利润增速（权重0.30）
2. revenue_growth: 营收同比增速（权重0.25）
3. eps_accel: 净利润增速的环比加速度（权重0.25）
4. garp: 增速/PE（GARP策略，降波动）（权重0.20）

方向: 正向
预期: 财报季效果最强，年化超额6-8%
数据源: fundamental表(net_profit_growth/revenue_growth/eps_growth_q/pe_ttm)

注: 提供OHLCV代理版本用于无基本面数据时的回退。
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_growth_cross_section(fundamental_df: pd.DataFrame) -> pd.Series:
    """
    成长因子截面计算

    参数:
        fundamental_df: 含 code/net_profit_growth/revenue_growth/eps_growth_q/pe_ttm 列

    返回:
        pd.Series, index=code, value=成长因子得分
    """
    if fundamental_df is None or fundamental_df.empty:
        return pd.Series(dtype=float)

    df = fundamental_df.copy()
    scores = pd.Series(0.0, index=df.index)
    weights_total = 0.0

    # 1. 净利润增速
    if "net_profit_growth" in df.columns:
        npg = pd.to_numeric(df["net_profit_growth"], errors="coerce")
        npg_rank = npg.rank(pct=True, na_option="keep")
        scores += 0.30 * npg_rank.fillna(0.5)
        weights_total += 0.30

    # 2. 营收增速
    if "revenue_growth" in df.columns:
        rg = pd.to_numeric(df["revenue_growth"], errors="coerce")
        rg_rank = rg.rank(pct=True, na_option="keep")
        scores += 0.25 * rg_rank.fillna(0.5)
        weights_total += 0.25

    # 3. 盈利加速度（单季度EPS增速）
    if "eps_growth_q" in df.columns:
        eg = pd.to_numeric(df["eps_growth_q"], errors="coerce")
        eg_rank = eg.rank(pct=True, na_option="keep")
        scores += 0.25 * eg_rank.fillna(0.5)
        weights_total += 0.25

    # 4. GARP（增速/PE，高增速低估值）
    if "net_profit_growth" in df.columns and "pe_ttm" in df.columns:
        npg = pd.to_numeric(df["net_profit_growth"], errors="coerce")
        pe = pd.to_numeric(df["pe_ttm"], errors="coerce")
        pe = pe.where(pe > 0)
        garp = npg / pe
        garp_rank = garp.rank(pct=True, na_option="keep")
        scores += 0.20 * garp_rank.fillna(0.5)
        weights_total += 0.20

    if weights_total > 0:
        scores = scores / weights_total

    scores.index = df["code"]
    return scores


def calc_growth_proxy(df: pd.DataFrame) -> pd.Series:
    """
    成长因子OHLCV代理版（无基本面数据时的回退）

    用价格动量 + 量能扩张代理成长:
    1. 中期动量: 近60日收益（成长股趋势性强）
    2. 量能扩张: 近20日均量/近60日均量（放量=市场认可）
    3. 加速度: 近20日收益 vs 近60日日均收益（增速加快）

    参数:
        df: 含 close/volume 列的DataFrame

    返回:
        pd.Series（与df等长）
    """
    close = df["close"].values
    volume = df["volume"].values
    n = len(close)
    result = np.full(n, np.nan)

    if n < 61:
        return pd.Series(result, index=df.index)

    for i in range(60, n):
        # 1. 中期动量
        ret_60d = (close[i] - close[i-59]) / close[i-59] if close[i-59] > 0 else 0
        momentum_score = min(max(ret_60d, -0.5), 1.0)  # clip到[-0.5, 1.0]
        momentum_norm = (momentum_score + 0.5) / 1.5  # 归一化到[0, 1]

        # 2. 量能扩张
        vol_20 = np.mean(volume[i-19:i+1])
        vol_60 = np.mean(volume[i-59:i+1])
        vol_expansion = vol_20 / vol_60 if vol_60 > 0 else 1.0
        vol_norm = min(vol_expansion, 3.0) / 3.0  # 归一化

        # 3. 加速度（近20日 vs 前40日的日均收益）
        ret_20d = (close[i] - close[i-19]) / close[i-19] if close[i-19] > 0 else 0
        ret_prev_40d = (close[i-20] - close[i-59]) / close[i-59] if close[i-59] > 0 else 0
        accel = ret_20d - ret_prev_40d * (20.0 / 40.0)  # 等比加速
        accel_norm = 1.0 / (1.0 + np.exp(-accel * 10))  # sigmoid

        # 合成
        result[i] = 0.40 * momentum_norm + 0.30 * vol_norm + 0.30 * accel_norm

    return pd.Series(result, index=df.index)


# 因子注册字典
GROWTH_FACTORS = {
    "growth_proxy": {
        "func": calc_growth_proxy,
        "desc": "成长因子(OHLCV代理:动量+量能扩张+加速度)",
        "dir": 1,
    },
}
