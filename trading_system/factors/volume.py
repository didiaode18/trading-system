"""
量价因子库
==========
15+量价因子：OBV、VWAP、量比、换手率、资金流等
"""

import pandas as pd
import numpy as np


def obv(df):
    """OBV能量潮"""
    direction = np.sign(df["close"].diff())
    return (direction * df["volume"]).cumsum()

def obv_slope(df):
    """OBV斜率（5日变化）"""
    return obv(df).pct_change(5)

def volume_ratio_5(df):
    """量比(5日): 当日成交量 / 5日均量"""
    ma5_vol = df["volume"].rolling(5).mean()
    return df["volume"] / ma5_vol.replace(0, np.nan)

def volume_ratio_20(df):
    """量比(20日): 当日成交量 / 20日均量"""
    ma20_vol = df["volume"].rolling(20).mean()
    return df["volume"] / ma20_vol.replace(0, np.nan)

def volume_shrink(df):
    """缩量程度: 5日均量 / 20日均量（<1表示缩量）"""
    ma5 = df["volume"].rolling(5).mean()
    ma20 = df["volume"].rolling(20).mean()
    return ma5 / ma20.replace(0, np.nan)

def vwap(df):
    """VWAP成交量加权平均价"""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_tp_vol = (typical_price * df["volume"]).cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)

def vwap_bias(df):
    """VWAP乖离: (收盘价 - VWAP) / VWAP"""
    v = vwap(df)
    return (df["close"] - v) / v.replace(0, np.nan)

def money_flow_index(df):
    """MFI资金流量指标(14日)"""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    mf = tp * df["volume"]
    pos_mf = mf.where(tp > tp.shift(1), 0).rolling(14).sum()
    neg_mf = mf.where(tp < tp.shift(1), 0).rolling(14).sum()
    ratio = pos_mf / neg_mf.replace(0, np.nan)
    return 100 - 100 / (1 + ratio)

def accumulation_distribution(df):
    """AD累积/派发线"""
    clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / \
          (df["high"] - df["low"]).replace(0, np.nan)
    return (clv * df["volume"]).cumsum()

def chaikin_money_flow(df):
    """CMF蔡金资金流(20日)"""
    clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / \
          (df["high"] - df["low"]).replace(0, np.nan)
    mf_vol = clv * df["volume"]
    return mf_vol.rolling(20).sum() / df["volume"].rolling(20).sum().replace(0, np.nan)

def volume_price_corr(df):
    """量价相关性(20日滚动)"""
    return df["close"].rolling(20).corr(df["volume"])

def high_low_volume_ratio(df):
    """高低点量比: 上涨日平均量 / 下跌日平均量(10日)"""
    up = df["close"].diff() > 0
    up_vol = df["volume"].where(up).rolling(10).mean()
    down_vol = df["volume"].where(~up).rolling(10).mean()
    return up_vol / down_vol.replace(0, np.nan)

def volume_breakout(df):
    """放量突破: 量>20日均量2倍 且 价格创10日新高"""
    vol_spike = df["volume"] > df["volume"].rolling(20).mean() * 2
    price_high = df["close"] >= df["close"].rolling(10).max()
    return (vol_spike & price_high).astype(int)

def turnover_rate_proxy(df):
    """换手率代理: 成交量/20日均量（无真实换手率时用）"""
    return df["volume"] / df["volume"].rolling(20).mean().replace(0, np.nan)

def volume_trend(df):
    """量能趋势: 20日均量的5日变化率"""
    ma20 = df["volume"].rolling(20).mean()
    return ma20.pct_change(5)


VOLUME_FACTORS = {
    "obv": {"func": obv, "desc": "OBV能量潮", "dir": 1},
    "obv_slope": {"func": obv_slope, "desc": "OBV斜率", "dir": 1},
    "volume_ratio_5": {"func": volume_ratio_5, "desc": "量比(5日)"},
    "volume_ratio_20": {"func": volume_ratio_20, "desc": "量比(20日)"},
    "volume_shrink": {"func": volume_shrink, "desc": "缩量程度", "dir": -1},
    "vwap": {"func": vwap, "desc": "VWAP"},
    "vwap_bias": {"func": vwap_bias, "desc": "VWAP乖离", "dir": 1},
    "mfi_14": {"func": money_flow_index, "desc": "MFI资金流(14)"},
    "ad_line": {"func": accumulation_distribution, "desc": "AD累积派发", "dir": 1},
    "cmf_20": {"func": chaikin_money_flow, "desc": "CMF蔡金资金流", "dir": 1},
    "vol_price_corr": {"func": volume_price_corr, "desc": "量价相关性"},
    "hl_vol_ratio": {"func": high_low_volume_ratio, "desc": "涨跌量比", "dir": 1},
    "vol_breakout": {"func": volume_breakout, "desc": "放量突破", "dir": 1},
    "turnover_proxy": {"func": turnover_rate_proxy, "desc": "换手率代理"},
    "vol_trend": {"func": volume_trend, "desc": "量能趋势", "dir": 1},
}
