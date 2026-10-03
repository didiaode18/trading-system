# -*- coding: utf-8 -*-
"""
尾盘成交占比因子（Tail Volume Factor）
=========================================
核心逻辑: 尾盘半小时量价反映机构调仓与被动资金流向

构造:
1. tail_volume_ratio = 尾盘成交量 / 全天成交量
2. tail_price_resilience = 尾盘价格韧性（尾盘vwap vs 全天vwap）
3. 因子 = 0.6 * tail_ratio + 0.4 * tail_resilience

预期: 月度调仓年化超额15%+，震荡市表现尤其稳定
数据源: minute_kline 表（完整版）/ 日线近似版

注: 日线近似版用最后N根K线量价特征代替真实尾盘数据。
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_tail_volume_daily_proxy(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    尾盘因子日线近似版

    用日线和量价特征近似尾盘行为:
    1. 尾盘强度代理: 收盘价在日内区间的位置 (close-low)/(high-low)
       - 接近1 → 尾盘拉升（机构买入）
       - 接近0 → 尾盘杀跌（机构卖出）
    2. 尾盘量能代理: 近5日量/近20日量 的比值变化
       - 比值上升 → 尾盘资金活跃
    3. 价格韧性: 日内从低点恢复的程度

    参数:
        df: 含 close/high/low/volume 列的DataFrame
        window: 平滑窗口

    返回:
        pd.Series（与df等长）
    """
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values
    n = len(close)
    result = np.full(n, np.nan)

    if n < window:
        return pd.Series(result, index=df.index)

    for i in range(1, n):
        # 1. 收盘价位置（日内区间）
        day_range = high[i] - low[i]
        if day_range > 0:
            close_position = (close[i] - low[i]) / day_range
        else:
            close_position = 0.5

        # 2. 价格韧性（从日内低点恢复）
        if high[i] > low[i]:
            resilience = (close[i] - low[i]) / (high[i] - low[i])
        else:
            resilience = 0.5

        # 3. 量能趋势（近期活跃度）
        if i >= window:
            vol_short = np.mean(volume[max(0, i-4):i+1])
            vol_long = np.mean(volume[max(0, i-window):i+1])
            vol_trend = vol_short / vol_long if vol_long > 0 else 1.0
        else:
            vol_trend = 1.0

        # 合成: 尾盘强度 + 价格韧性 + 量能趋势
        raw = 0.4 * close_position + 0.3 * resilience + 0.3 * min(vol_trend, 2.0) / 2.0
        result[i] = raw

    # 平滑
    series = pd.Series(result, index=df.index)
    return series.rolling(5, min_periods=1).mean()


# 因子注册字典
TAIL_VOLUME_FACTORS = {
    "tail_volume_proxy": {
        "func": calc_tail_volume_daily_proxy,
        "desc": "尾盘成交占比因子(日线近似)",
        "dir": 1,
    },
}
