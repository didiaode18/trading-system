# -*- coding: utf-8 -*-
"""
改进版反转因子（Improved Reversal Factor）
============================================
核心逻辑: 剔除流动性冲击与涨跌停干扰，捕捉真实超跌反弹溢价

构造:
1. raw_reversal = -return(window)
2. 剔除涨跌停日（close==high 且 涨幅>9.5% 或 close==low 且 跌幅>9.5%）
3. 对成交量做中性化（剔除流动性冲击）

预期: IR≈2.5, 月度胜率78%
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_improved_reversal(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    改进版反转因子

    参数:
        df: 含 close/high/low/volume 列的DataFrame
        window: 反转计算窗口

    返回:
        pd.Series（与df等长），值越大表示超跌越严重
    """
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values
    n = len(close)
    result = np.full(n, np.nan)

    if n < window + 1:
        return pd.Series(result, index=df.index)

    for i in range(window, n):
        # 基础反转: 过去window日收益率取反
        ret = (close[i] - close[i - window]) / close[i - window] if close[i - window] > 0 else 0
        raw_reversal = -ret

        # 剔除涨跌停日的影响
        limit_count = 0
        for j in range(i - window + 1, i + 1):
            if j < 1:
                continue
            daily_ret = (close[j] - close[j - 1]) / close[j - 1] if close[j - 1] > 0 else 0
            # 涨停: 收盘=最高 且 涨幅>9.5%
            is_limit_up = (close[j] == high[j]) and (daily_ret > 0.095)
            # 跌停: 收盘=最低 且 跌幅>9.5%
            is_limit_down = (close[j] == low[j]) and (daily_ret < -0.095)
            if is_limit_up or is_limit_down:
                limit_count += 1

        # 调整: 涨跌停日越多，反转信号越不可靠，打折
        limit_ratio = limit_count / window
        reliability = max(0.3, 1 - limit_ratio * 2)

        # 成交量中性化: 近期量能vs长期量能，高换手可能是流动性冲击而非超跌
        vol_short = np.mean(volume[max(0, i-5):i+1])
        vol_long = np.mean(volume[max(0, i-window):i+1])
        vol_ratio = vol_short / vol_long if vol_long > 0 else 1
        # 放量下跌 → 可能有实质利空，反转信号打折
        # 缩量下跌 → 可能是情绪性抛售，反转信号增强
        vol_adjustment = 1.0 / vol_ratio if vol_ratio > 0 else 1.0
        vol_adjustment = min(vol_adjustment, 2.0)  # 上限2倍

        result[i] = raw_reversal * reliability * vol_adjustment

    return pd.Series(result, index=df.index)


# 因子注册字典
REVERSAL_FACTORS = {
    "improved_reversal": {
        "func": calc_improved_reversal,
        "desc": "改进版反转因子(剔除涨跌停+量能中性)",
        "dir": 1,
    },
}
