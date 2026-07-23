"""
特征工程模块
============
从因子库提取ML模型输入特征：
- 自动从FactorRegistry获取Top因子
- 标准化 + 缺失值处理
- 标签生成（未来5日收益>3%=1, <-3%=-1, 其余=0）
"""

import logging
import pandas as pd
import numpy as np

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)


def build_features(df: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
    """
    从行情数据构建ML特征
    
    参数:
        df: 含OHLCV的DataFrame
        top_n: 使用Top N个因子
    
    返回:
        特征DataFrame
    """
    from factors.registry import get_registry

    registry = get_registry()
    factor_df = registry.compute_all(df)

    if factor_df.empty:
        return pd.DataFrame()

    # 取Top N因子（按IC排序，无IC时取全部）
    top_factors = registry.get_top_factors(top_n)
    if top_factors:
        available = [f for f in top_factors if f in factor_df.columns]
        factor_df = factor_df[available]

    # 填充NaN（前向填充+0填充）
    factor_df = factor_df.fillna(method="ffill").fillna(0)

    # 标准化
    factor_df = (factor_df - factor_df.mean()) / factor_df.std().replace(0, 1)

    return factor_df


def build_labels(df: pd.DataFrame, forward_days: int = 5,
                 threshold: float = 0.03) -> pd.Series:
    """
    生成标签
    
    参数:
        df: 含close列的DataFrame
        forward_days: 预测未来N天
        threshold: 涨跌阈值
    
    返回:
        标签Series: 1=涨, -1=跌, 0=震荡
    """
    future_return = df["close"].shift(-forward_days) / df["close"] - 1
    labels = pd.Series(0, index=df.index)
    labels[future_return > threshold] = 1
    labels[future_return < -threshold] = -1
    return labels


def prepare_dataset(data_dict: dict, forward_days: int = 5,
                    threshold: float = 0.03) -> tuple:
    """
    准备训练数据集（多股票合并）
    
    参数:
        data_dict: {code: DataFrame}
    
    返回:
        (X: DataFrame, y: Series)
    """
    all_X = []
    all_y = []

    for code, df in data_dict.items():
        if df is None or len(df) < 80:
            continue

        features = build_features(df)
        labels = build_labels(df, forward_days, threshold)

        if features.empty:
            continue

        # 对齐并去除NaN标签
        valid_idx = labels.notna() & (labels != 0)  # 只取明确涨跌
        # 去掉最后forward_days行（无标签）
        valid_idx = valid_idx & (pd.Series(range(len(df))) < len(df) - forward_days).values

        X = features[valid_idx]
        y = labels[valid_idx]

        if len(X) > 0:
            X["code"] = code  # 加入股票标识
            all_X.append(X)
            all_y.append(y)

    if not all_X:
        return pd.DataFrame(), pd.Series(dtype=int)

    X_combined = pd.concat(all_X, ignore_index=True)
    y_combined = pd.concat(all_y, ignore_index=True)

    # 去掉code列（不用于训练）
    if "code" in X_combined.columns:
        X_combined = X_combined.drop(columns=["code"])

    logger.info(f"ML数据集: {len(X_combined)}样本, {X_combined.shape[1]}特征, "
               f"正样本{(y_combined == 1).sum()}, 负样本{(y_combined == -1).sum()}")

    return X_combined, y_combined
