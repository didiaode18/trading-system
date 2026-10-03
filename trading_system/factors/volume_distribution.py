# -*- coding: utf-8 -*-
"""
日内成交量分布因子（暗流因子 / Volume Distribution Factor）
=============================================================
核心逻辑: 日内成交分布的信息密度能稳定预测未来收益

构造:
1. 将日内分钟成交量归一化为分布
2. 计算香农熵: H = -Σ p_i * log(p_i)
3. 流动性弹性: 价格冲击/成交量变化的滚动回归
4. 因子 = 0.5 * 熵标准化 + 0.5 * 弹性标准化

预期: 月度RankIC≈-7.65%, 多空年化29%, IR达3.49
数据源: minute_kline 表（5分钟频率即可近似）

注: 完整版需分钟数据，此处提供日线近似版本。
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_volume_distribution_daily(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    暗流因子日线近似版

    用日成交量的分布特征（波动性/集中度）近似衡量信息密度:
    1. 成交量波动熵: 近window日成交量的分布均匀度
    2. 量价弹性: 价格变化/成交量变化的滚动相关

    参数:
        df: 含 close/volume 列的DataFrame
        window: 计算窗口

    返回:
        pd.Series（与df等长）
    """
    close = df["close"].values
    volume = df["volume"].values
    n = len(close)
    result = np.full(n, np.nan)

    if n < window + 5:
        return pd.Series(result, index=df.index)

    for i in range(window + 4, n):
        # 1. 成交量分布熵
        vol_window = volume[i-window:i]
        vol_sum = np.sum(vol_windows)
        if vol_sum > 0:
            p = vol_windows / vol_sum
            p = p[p > 0]  # 去除零值
            entropy = -np.sum(p * np.log(p + 1e-10))
        else:
            entropy = 0

        # 归一化熵到 [0, 1]
        max_entropy = np.log(window)
        norm_entropy = entropy / max_entropy if max_entropy > 0 else 0.5

        # 2. 量价弹性
        price_changes = np.diff(close[i-window:i+1]) / close[i-window:i]
        vol_changes = np.diff(volume[i-window:i+1].astype(float))
        # 弹性 = |价格变化/成交量变化| 的倒数（弹性越大=同样量变化价格变化越小=流动性越好）
        valid = vol_changes != 0
        if valid.sum() > 3:
            elasticity = np.mean(np.abs(price_changes[valid] / vol_changes[valid]))
            norm_elasticity = 1.0 / (1.0 + elasticity * 1e6)  # 归一化
        else:
            norm_elasticity = 0.5

        # 合成
        result[i] = 0.5 * norm_entropy + 0.5 * norm_elasticity

    return pd.Series(result, index=df.index)


# 因子注册字典
VOLUME_DISTRIBUTION_FACTORS = {
    "vol_dist_entropy": {
        "func": calc_volume_distribution_daily,
        "desc": "暗流因子(成交量分布熵+量价弹性)",
        "dir": 1,
    },
}
