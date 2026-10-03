# -*- coding: utf-8 -*-
"""
流动性因子（Liquidity Factor）
================================
核心逻辑: 低换手长期收益稳定，Amihud非流动性度量

构造:
1. avg_turnover_20: 20日日均换手率（取反，低换手得分高）
2. turnover_vol: 换手率的波动率（取反，稳定得分高）
3. amihud_illiq: |return| / amount（Amihud非流动性，越高越不流动）
4. 因子 = -(0.4*turnover + 0.3*turnover_vol + 0.3*amihud)

方向: 正向（低流动性得分高）
注意: 可做风控前置过滤（极端低流动性=退市风险）
数据源: daily_kline(turnover字段) / OHLCV代理版

注: turnover字段需要数据更新后才可用。
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_liquidity_proxy(df: pd.DataFrame) -> pd.Series:
    """
    流动性因子OHLCV代理版

    用成交金额的稳定性 + 量价冲击代理流动性:
    1. Amihud非流动性: mean(|return| / amount) - 越高越不流动
    2. 成交量稳定性: 20日均量/60日均量 的稳定性
    3. 换手代理: 成交额在股票池中的相对排名（截面计算，此处用时序近似）

    参数:
        df: 含 close/volume/amount 列的DataFrame

    返回:
        pd.Series（与df等长），值越大表示流动性越低（得分越高）
    """
    close = df["close"].values
    volume = df["volume"].values
    amount = df["amount"].values if "amount" in df.columns else None
    n = len(close)
    result = np.full(n, np.nan)

    if n < 61:
        return pd.Series(result, index=df.index)

    # 日收益率
    returns = np.zeros(n)
    for i in range(1, n):
        returns[i] = (close[i] - close[i-1]) / close[i-1] if close[i-1] > 0 else 0

    for i in range(60, n):
        # 1. Amihud非流动性
        if amount is not None:
            amihud_vals = []
            for j in range(i-19, i+1):
                if amount[j] > 0:
                    amihud_vals.append(abs(returns[j]) / amount[j])
            if amihud_vals:
                amihud = np.mean(amihud_vals)
                amihud_norm = 1.0 / (1.0 + amihud * 1e10)  # 归一化
            else:
                amihud_norm = 0.5
        else:
            # 用volume近似
            vol_mean = np.mean(volume[i-19:i+1])
            if vol_mean > 0:
                ret_abs = np.mean(np.abs(returns[i-19:i+1]))
                amihud_proxy = ret_abs / vol_mean
                amihud_norm = 1.0 / (1.0 + amihud_proxy * 1e8)
            else:
                amihud_norm = 0.5

        # 2. 成交量稳定性（短期/长期比值的稳定性）
        vol_20 = np.mean(volume[i-19:i+1])
        vol_60 = np.mean(volume[i-59:i+1])
        if vol_60 > 0:
            vol_ratio = vol_20 / vol_60
            # 接近1表示稳定
            stability = 1.0 - abs(vol_ratio - 1.0)
            stability = max(0, min(1, stability))
        else:
            stability = 0.5

        # 3. 成交额水平（相对自身的长期水平）
        if amount is not None:
            amt_20 = np.mean(amount[i-19:i+1])
            amt_60 = np.mean(amount[i-59:i+1])
            amt_level = amt_20 / amt_60 if amt_60 > 0 else 1.0
            amt_norm = min(amt_level, 3.0) / 3.0
        else:
            amt_norm = 0.5

        # 合成: 取反（低流动性 = 高得分）
        result[i] = 0.40 * amihud_norm + 0.35 * stability + 0.25 * (1.0 - amt_norm)

    return pd.Series(result, index=df.index)


def calc_liquidity_turnover(df: pd.DataFrame) -> pd.Series:
    """
    流动性因子换手率版（需turnover字段）

    参数:
        df: 含 turnover 列的DataFrame

    返回:
        pd.Series（与df等长），低换手 = 高得分
    """
    if "turnover" not in df.columns:
        return pd.Series(np.nan, index=df.index)

    turnover = df["turnover"].values
    n = len(turnover)
    result = np.full(n, np.nan)

    if n < 20:
        return pd.Series(result, index=df.index)

    for i in range(19, n):
        window = turnover[i-19:i+1]
        valid = window[~np.isnan(window)]
        if len(valid) < 10:
            continue

        avg_turn = np.mean(valid)
        turn_vol = np.std(valid)

        # 低换手 + 低波动 = 高得分（取反）
        result[i] = -(0.6 * avg_turn + 0.4 * turn_vol)

    return pd.Series(result, index=df.index)


# 因子注册字典
LIQUIDITY_FACTORS = {
    "liquidity_proxy": {
        "func": calc_liquidity_proxy,
        "desc": "流动性因子(Amihud非流动性+量稳定性)",
        "dir": 1,
    },
}
