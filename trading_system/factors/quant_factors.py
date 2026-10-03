# -*- coding: utf-8 -*-
"""
量化因子模块（从 quant/factors.py 迁移统一到 factors/ 层）
============================================================
将原 FactorEngine 的 7 个因子适配为 FactorRegistry 标准接口。

因子列表:
1. qe_momentum: 20日收益率（正向）
2. qe_turnover: 量比代理换手率（适中）
3. qe_volatility: 20日波动率（反向，低波优先）
4. qe_vol_price_div: 量价背离（反向）
5. qe_ma_trend: 均线趋势得分（正向）
6. qe_breakout: 突破因子（正向）
7. qe_pullback: 回调买入因子（正向）

注: 前缀 qe_ 表示来自 quant engine，与 factors/ 层已有因子区分。
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# 单因子计算函数（返回与 df 等长的 Series）
# ============================================================

def calc_qe_momentum(df: pd.DataFrame) -> pd.Series:
    """20日收益率（正向，强者恒强）"""
    close = df["close"]
    result = close.pct_change(20)
    result.iloc[:20] = np.nan
    result = result.fillna(0).where(close.shift(20) > 0, np.nan)
    return result


def calc_qe_turnover(df: pd.DataFrame) -> pd.Series:
    """量比代理换手率: 5日均量/20日均量（适中，非极端）"""
    volume = df["volume"]
    vol_5 = volume.rolling(5, min_periods=5).mean()
    vol_20 = volume.rolling(20, min_periods=20).mean()
    result = vol_5 / vol_20
    result = result.where(vol_20 > 0, 1.0)
    return result


def calc_qe_volatility(df: pd.DataFrame) -> pd.Series:
    """20日收益率标准差（反向，低波优先）"""
    close = df["close"]
    daily_ret = close.pct_change()
    result = daily_ret.rolling(20, min_periods=20).std()
    return result


def calc_qe_vol_price_div(df: pd.DataFrame) -> pd.Series:
    """量价背离: 近10天价涨量缩天数占比（反向）"""
    close = df["close"]
    volume = df["volume"]
    price_up = (close > close.shift(1)).astype(int)
    vol_down = (volume < volume.shift(1)).astype(int)
    divergence = price_up & vol_down
    result = divergence.rolling(10, min_periods=10).sum() / 10.0
    return result


def calc_qe_ma_trend(df: pd.DataFrame) -> pd.Series:
    """均线趋势: MA5>MA10>MA20 得分 0-1（正向）"""
    close = df["close"]
    ma5 = close.rolling(5, min_periods=5).mean()
    ma10 = close.rolling(10, min_periods=10).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    score = ((close > ma5).astype(int)
             + (ma5 > ma10).astype(int)
             + (ma10 > ma20).astype(int))
    result = score / 3.0
    return result


def calc_qe_breakout(df: pd.DataFrame) -> pd.Series:
    """突破因子: 当前价/60日最高价（正向，接近新高）"""
    close = df["close"]
    high_60 = close.rolling(60, min_periods=60).max()
    result = close / high_60
    result = result.where(high_60 > 0, 0.5)
    return result


def calc_qe_pullback(df: pd.DataFrame) -> pd.Series:
    """
    回调买入因子: 从高点回落5-10% + MA20向上 + 缩量企稳
    得分越高 = 回调到位 + 趋势未破 + 缩量确认
    """
    close = df["close"]
    volume = df["volume"]

    high_20 = close.rolling(20, min_periods=20).max()
    drawdown = (high_20 - close) / high_20
    drawdown = drawdown.fillna(0)

    ma20 = close.rolling(20, min_periods=20).mean()
    ma20_prev = close.shift(5).rolling(20, min_periods=20).mean()
    ma20_slope = (ma20 - ma20_prev) / ma20_prev
    ma20_slope = ma20_slope.fillna(0)

    # 回调得分（向量化分段）
    pb_score = pd.Series(0.0, index=df.index)
    pb_score[(drawdown >= 0.05) & (drawdown <= 0.10)] = 1.0
    pb_score[(drawdown >= 0.03) & (drawdown < 0.05)] = 0.6
    pb_score[(drawdown > 0.10) & (drawdown <= 0.15)] = 0.5

    # 趋势确认
    trend = pd.Series(0.2, index=df.index)
    trend[ma20_slope > 0.01] = 1.0
    trend[(ma20_slope > -0.01) & (ma20_slope <= 0.01)] = 0.6

    above_ma20 = pd.Series(0.4, index=df.index)
    above_ma20[close > ma20] = 1.0

    # 缩量企稳
    vol_2 = volume.rolling(2, min_periods=2).mean()
    vol_5 = volume.rolling(5, min_periods=5).mean()
    shrink = pd.Series(0.7, index=df.index)
    shrink[(vol_5 > 0) & (vol_2 < vol_5 * 0.7)] = 1.0

    result = pb_score * trend * above_ma20 * shrink
    return result


# ============================================================
# 因子注册字典（供 FactorRegistry._auto_register 使用）
# ============================================================

QUANT_ENGINE_FACTORS = {
    "qe_momentum": {
        "func": calc_qe_momentum,
        "desc": "20日收益率(quant engine)",
        "dir": 1,
    },
    "qe_turnover": {
        "func": calc_qe_turnover,
        "desc": "量比代理换手(quant engine)",
        "dir": -1,  # 适中，非极端，反向用于降权
    },
    "qe_volatility": {
        "func": calc_qe_volatility,
        "desc": "20日波动率(quant engine)",
        "dir": -1,  # 反向: 低波优先
    },
    "qe_vol_price_div": {
        "func": calc_qe_vol_price_div,
        "desc": "量价背离(quant engine)",
        "dir": -1,  # 反向: 低背离好
    },
    "qe_ma_trend": {
        "func": calc_qe_ma_trend,
        "desc": "均线趋势得分(quant engine)",
        "dir": 1,
    },
    "qe_breakout": {
        "func": calc_qe_breakout,
        "desc": "突破因子(quant engine)",
        "dir": 1,
    },
    "qe_pullback": {
        "func": calc_qe_pullback,
        "desc": "回调买入因子(quant engine)",
        "dir": 1,
    },
}
