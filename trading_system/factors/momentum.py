"""
动量因子库
==========
15+动量因子：N日收益率、相对强度、加速度、威廉指标等
"""

import pandas as pd
import numpy as np


def return_5d(df):
    """5日收益率"""
    return df["close"].pct_change(5)

def return_10d(df):
    """10日收益率"""
    return df["close"].pct_change(10)

def return_20d(df):
    """20日收益率"""
    return df["close"].pct_change(20)

def return_60d(df):
    """60日收益率"""
    return df["close"].pct_change(60)

def momentum_acceleration(df):
    """动量加速度: 5日收益率的5日变化"""
    return df["close"].pct_change(5).diff(5)

def relative_strength(df):
    """相对强度: 20日收益率排名百分位（需外部基准，此处用绝对值）"""
    ret = df["close"].pct_change(20)
    return ret.rolling(60).rank(pct=True)

def williams_r(df):
    """Williams %R(14)"""
    high_14 = df["high"].rolling(14).max()
    low_14 = df["low"].rolling(14).min()
    return (high_14 - df["close"]) / (high_14 - low_14).replace(0, np.nan) * -100

def roc_12(df):
    """变动率ROC(12)"""
    return df["close"].pct_change(12) * 100

def price_position_60(df):
    """60日价格位置: (当前价-60日最低)/(60日最高-60日最低)"""
    high = df["high"].rolling(60).max()
    low = df["low"].rolling(60).min()
    return (df["close"] - low) / (high - low).replace(0, np.nan)

def new_high_count(df):
    """创N日新高次数(20日内)"""
    is_high = (df["close"] >= df["close"].rolling(60).max()).astype(int)
    return is_high.rolling(20).sum()

def consecutive_up_days(df):
    """连续上涨天数"""
    up = (df["close"].diff() > 0).astype(int)
    # 计算连续1的长度
    groups = (up != up.shift()).cumsum()
    return up.groupby(groups).cumsum()

def consecutive_down_days(df):
    """连续下跌天数"""
    down = (df["close"].diff() < 0).astype(int)
    groups = (down != down.shift()).cumsum()
    return down.groupby(groups).cumsum()

def avg_true_range_momentum(df):
    """ATR动量: (收盘-开盘) / ATR"""
    atr = _atr(df)
    return (df["close"] - df["open"]) / atr.replace(0, np.nan)

def _atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def price_velocity(df):
    """价格速度: 10日线性回归斜率"""
    def slope(series):
        x = np.arange(len(series))
        if len(series) < 2:
            return 0
        return np.polyfit(x, series.values, 1)[0]
    return df["close"].rolling(10).apply(slope, raw=False) / df["close"]

def gap_strength(df):
    """跳空强度: (今日开盘-昨日收盘) / 昨日收盘"""
    return (df["open"] - df["close"].shift(1)) / df["close"].shift(1)


MOMENTUM_FACTORS = {
    "return_5d": {"func": return_5d, "desc": "5日收益率", "dir": 1},
    "return_10d": {"func": return_10d, "desc": "10日收益率", "dir": 1},
    "return_20d": {"func": return_20d, "desc": "20日收益率", "dir": 1},
    "return_60d": {"func": return_60d, "desc": "60日收益率", "dir": 1},
    "momentum_accel": {"func": momentum_acceleration, "desc": "动量加速度", "dir": 1},
    "relative_strength": {"func": relative_strength, "desc": "相对强度", "dir": 1},
    "williams_r": {"func": williams_r, "desc": "Williams %R"},
    "roc_12": {"func": roc_12, "desc": "ROC(12)", "dir": 1},
    "price_pos_60": {"func": price_position_60, "desc": "60日价格位置", "dir": 1},
    "new_high_cnt": {"func": new_high_count, "desc": "创新高次数", "dir": 1},
    "consec_up": {"func": consecutive_up_days, "desc": "连涨天数", "dir": 1},
    "consec_down": {"func": consecutive_down_days, "desc": "连跌天数", "dir": -1},
    "atr_momentum": {"func": avg_true_range_momentum, "desc": "ATR动量", "dir": 1},
    "price_velocity": {"func": price_velocity, "desc": "价格速度", "dir": 1},
    "gap_strength": {"func": gap_strength, "desc": "跳空强度", "dir": 1},
}
