"""
技术因子库
==========
20+技术因子：均线、MACD、RSI、布林带、ATR、KDJ、CCI等
每个因子函数签名: (df: DataFrame) -> Series
"""

import pandas as pd
import numpy as np


# ============================================================
# 均线类因子
# ============================================================

def ma5(df): return df["close"].rolling(5).mean()
def ma10(df): return df["close"].rolling(10).mean()
def ma20(df): return df["close"].rolling(20).mean()
def ma60(df): return df["close"].rolling(60).mean()

def ma20_bias(df):
    """MA20乖离率 = (收盘价 - MA20) / MA20"""
    ma = df["close"].rolling(20).mean()
    return (df["close"] - ma) / ma

def ma60_bias(df):
    """MA60乖离率"""
    ma = df["close"].rolling(60).mean()
    return (df["close"] - ma) / ma

def ma_cross_signal(df):
    """均线多空排列得分: MA5>MA10>MA20>MA60 各+1分"""
    m5 = df["close"].rolling(5).mean()
    m10 = df["close"].rolling(10).mean()
    m20 = df["close"].rolling(20).mean()
    m60 = df["close"].rolling(60).mean()
    score = ((m5 > m10).astype(int) + (m10 > m20).astype(int) +
             (m20 > m60).astype(int))
    return score

def ma_slope_20(df):
    """MA20斜率（5日变化率）"""
    ma = df["close"].rolling(20).mean()
    return ma.pct_change(5)


# ============================================================
# MACD 因子
# ============================================================

def macd_dif(df):
    """MACD DIF线"""
    ema12 = df["close"].ewm(span=12).mean()
    ema26 = df["close"].ewm(span=26).mean()
    return ema12 - ema26

def macd_dea(df):
    """MACD DEA线"""
    dif = macd_dif(df)
    return dif.ewm(span=9).mean()

def macd_hist(df):
    """MACD柱状图"""
    return (macd_dif(df) - macd_dea(df)) * 2

def macd_cross(df):
    """MACD金叉/死叉信号: 1=金叉, -1=死叉, 0=无"""
    dif = macd_dif(df)
    dea = macd_dea(df)
    cross_up = ((dif > dea) & (dif.shift(1) <= dea.shift(1))).astype(int)
    cross_down = ((dif < dea) & (dif.shift(1) >= dea.shift(1))).astype(int) * -1
    return cross_up + cross_down


# ============================================================
# RSI 因子
# ============================================================

def rsi_6(df):
    """RSI(6)"""
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(6).mean()
    loss = (-delta.clip(upper=0)).rolling(6).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def rsi_14(df):
    """RSI(14)"""
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def rsi_divergence(df):
    """RSI背离: 价格新高但RSI未新高=-1(顶背离), 价格新低但RSI未新低=1(底背离)"""
    rsi = rsi_14(df)
    price_high = df["close"] == df["close"].rolling(20).max()
    rsi_not_high = rsi < rsi.rolling(20).max()
    top_div = (price_high & rsi_not_high).astype(int) * -1

    price_low = df["close"] == df["close"].rolling(20).min()
    rsi_not_low = rsi > rsi.rolling(20).min()
    bottom_div = (price_low & rsi_not_low).astype(int)

    return top_div + bottom_div


# ============================================================
# 布林带因子
# ============================================================

def boll_position(df):
    """布林带位置: (价格-下轨)/(上轨-下轨), 0~1"""
    ma = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    upper = ma + 2 * std
    lower = ma - 2 * std
    width = upper - lower
    return (df["close"] - lower) / width.replace(0, np.nan)

def boll_width(df):
    """布林带宽度（波动率代理）"""
    ma = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    return (4 * std) / ma

def boll_breakout(df):
    """布林带突破: 1=突破上轨, -1=跌破下轨"""
    ma = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    upper = ma + 2 * std
    lower = ma - 2 * std
    up = (df["close"] > upper).astype(int)
    down = (df["close"] < lower).astype(int) * -1
    return up + down


# ============================================================
# ATR 因子
# ============================================================

def atr_14(df):
    """ATR(14)"""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(14).mean()

def atr_ratio(df):
    """ATR比率 = ATR/收盘价（标准化波动率）"""
    return atr_14(df) / df["close"]


# ============================================================
# KDJ 因子
# ============================================================

def kdj_k(df):
    """KDJ K值"""
    low_9 = df["low"].rolling(9).min()
    high_9 = df["high"].rolling(9).max()
    rsv = (df["close"] - low_9) / (high_9 - low_9).replace(0, np.nan) * 100
    return rsv.ewm(com=2).mean()

def kdj_d(df):
    """KDJ D值"""
    return kdj_k(df).ewm(com=2).mean()

def kdj_j(df):
    """KDJ J值"""
    return 3 * kdj_k(df) - 2 * kdj_d(df)


# ============================================================
# CCI 因子
# ============================================================

def cci_14(df):
    """CCI(14)"""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma_tp = tp.rolling(14).mean()
    md = tp.rolling(14).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - ma_tp) / (0.015 * md.replace(0, np.nan))


# ============================================================
# 注册表
# ============================================================

TECHNICAL_FACTORS = {
    # 均线
    "ma5": {"func": ma5, "desc": "5日均线"},
    "ma10": {"func": ma10, "desc": "10日均线"},
    "ma20": {"func": ma20, "desc": "20日均线"},
    "ma60": {"func": ma60, "desc": "60日均线"},
    "ma20_bias": {"func": ma20_bias, "desc": "MA20乖离率", "dir": 1},
    "ma60_bias": {"func": ma60_bias, "desc": "MA60乖离率", "dir": 1},
    "ma_cross_signal": {"func": ma_cross_signal, "desc": "均线多空排列(0-3)", "dir": 1},
    "ma_slope_20": {"func": ma_slope_20, "desc": "MA20斜率", "dir": 1},
    # MACD
    "macd_dif": {"func": macd_dif, "desc": "MACD DIF", "dir": 1},
    "macd_dea": {"func": macd_dea, "desc": "MACD DEA", "dir": 1},
    "macd_hist": {"func": macd_hist, "desc": "MACD柱", "dir": 1},
    "macd_cross": {"func": macd_cross, "desc": "MACD金叉死叉", "dir": 1},
    # RSI
    "rsi_6": {"func": rsi_6, "desc": "RSI(6)"},
    "rsi_14": {"func": rsi_14, "desc": "RSI(14)"},
    "rsi_divergence": {"func": rsi_divergence, "desc": "RSI背离", "dir": 1},
    # 布林带
    "boll_position": {"func": boll_position, "desc": "布林带位置(0-1)"},
    "boll_width": {"func": boll_width, "desc": "布林带宽度"},
    "boll_breakout": {"func": boll_breakout, "desc": "布林带突破", "dir": 1},
    # ATR
    "atr_14": {"func": atr_14, "desc": "ATR(14)"},
    "atr_ratio": {"func": atr_ratio, "desc": "ATR/价格比"},
    # KDJ
    "kdj_k": {"func": kdj_k, "desc": "KDJ K值"},
    "kdj_d": {"func": kdj_d, "desc": "KDJ D值"},
    "kdj_j": {"func": kdj_j, "desc": "KDJ J值"},
    # CCI
    "cci_14": {"func": cci_14, "desc": "CCI(14)"},
}
