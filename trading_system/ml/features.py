"""
特征工程模块
============
从因子库提取ML模型输入特征：
- 自动从FactorRegistry获取Top因子
- 标准化 + 缺失值处理
- 标签生成（未来5日收益>3%=1, <-3%=-1, 其余=0）

【修复说明】
- 保留 label=0（震荡期）样本，随机采样50%以控制类别不平衡
- 新增 apply_temporal_decay() 指数时间衰减，近期样本权重更高
- prepare_dataset 返回 temporal_weights 供训练器使用
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
    # FIX: 修复 fillna(method="ffill") 在 pandas 2.1+ 废弃的问题，改用 ffill() 方法
    factor_df = factor_df.ffill().fillna(0)

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


def apply_temporal_decay(n_samples: int, half_life_days: int = 60,
                         trading_days_per_year: int = 250) -> np.ndarray:
    """
    【新增】计算指数时间衰减权重
    
    金融数据具有时效性，近期的市场模式比远期更能预测未来走势。
    使用指数衰减函数：weight = exp(-λ × i)
    其中 i=0 为最新样本，i=n_samples-1 为最旧样本。
    半衰期默认为60个交易日（约3个月）。
    
    参数:
        n_samples: 样本总数
        half_life_days: 半衰期（交易日数），默认60天
        trading_days_per_year: 年交易日数（用于日志提示）
    
    返回:
        权重数组 np.ndarray，最新样本权重=1.0，最旧样本权重最低
    """
    # λ = ln(2) / half_life，使经过 half_life 个样本后权重恰好为0.5
    decay_rate = np.log(2) / half_life_days
    # 位置索引：0=最新样本，n_samples-1=最旧样本
    positions = np.arange(n_samples)[::-1]  # 反转：最后一条=0（最新）
    weights = np.exp(-decay_rate * positions)

    # 日志输出衰减情况
    if n_samples > 1:
        min_weight = weights[0]  # 最旧样本的权重
        logger.info(
            f"时间衰减: {n_samples}样本, 半衰期{half_life_days}天, "
            f"最旧样本权重={min_weight:.3f}"
        )

    return weights


def prepare_dataset(data_dict: dict, forward_days: int = 5,
                    threshold: float = 0.03,
                    keep_zero_label_ratio: float = 0.5,
                    temporal_half_life: int = 60) -> tuple:
    """
    准备训练数据集（多股票合并）
    
    【修复】保留 label=0（震荡期）样本，通过随机采样控制比例。
    震荡期的"不操作"信号对模型同样重要，不应全部丢弃。
    
    【新增】计算时间衰减权重，近期样本获得更高训练权重。
    
    参数:
        data_dict: {code: DataFrame}
        forward_days: 预测未来N天
        threshold: 涨跌阈值
        keep_zero_label_ratio: label=0 样本的保留比例，默认0.5（随机采样50%）
                               设为1.0则保留全部震荡期样本
        temporal_half_life: 时间衰减半衰期（交易日），默认60天
    
    返回:
        (X: DataFrame, y: Series, temporal_weights: np.ndarray)
        temporal_weights 可直接传入 trainer.train() 的 temporal_weights 参数
    """
    all_X = []
    all_y = []
    all_temporal = []  # 收集每只股票的时间衰减权重
    rng = np.random.RandomState(42)  # 固定随机种子，保证可复现

    for code, df in data_dict.items():
        if df is None or len(df) < 80:
            continue

        features = build_features(df)
        labels = build_labels(df, forward_days, threshold)

        if features.empty:
            continue

        # 【修复】不再过滤掉 label==0，改为保留所有标签类型
        # 只去掉最后 forward_days 行（无有效标签）和未来收益为NaN的行
        has_future_label = pd.Series(range(len(df))) < len(df) - forward_days
        valid_idx = labels.notna() & has_future_label.values

        # 对 label!=0 的样本全部保留，label==0 的样本随机采样
        nonzero_mask = valid_idx & (labels != 0)
        zero_mask = valid_idx & (labels == 0)

        # 确定 label=0 的采样索引
        zero_indices = labels.index[zero_mask]
        if len(zero_indices) > 0 and keep_zero_label_ratio < 1.0:
            n_keep = max(1, int(len(zero_indices) * keep_zero_label_ratio))
            sampled_zero_idx = rng.choice(zero_indices, size=n_keep, replace=False)
            sampled_zero_mask = pd.Series(False, index=labels.index)
            sampled_zero_mask.loc[sampled_zero_idx] = True
            # 合并：全部非零标签 + 采样的零标签
            final_mask = nonzero_mask | sampled_zero_mask
        else:
            final_mask = valid_idx  # keep_zero_label_ratio=1.0时保留全部

        X = features[final_mask]
        y = labels[final_mask]

        if len(X) > 0:
            # 【新增】计算该股票的时间衰减权重
            temporal_w = apply_temporal_decay(
                len(X), half_life_days=temporal_half_life
            )
            X = X.copy()
            X["code"] = code  # 加入股票标识
            all_X.append(X)
            all_y.append(y)
            all_temporal.append(temporal_w)

    if not all_X:
        return pd.DataFrame(), pd.Series(dtype=int), np.array([])

    X_combined = pd.concat(all_X, ignore_index=True)
    y_combined = pd.concat(all_y, ignore_index=True)
    temporal_combined = np.concatenate(all_temporal)

    # 去掉code列（不用于训练）
    if "code" in X_combined.columns:
        X_combined = X_combined.drop(columns=["code"])

    logger.info(
        f"ML数据集: {len(X_combined)}样本, {X_combined.shape[1]}特征, "
        f"涨{(y_combined == 1).sum()}, 震荡{(y_combined == 0).sum()}, "
        f"跌{(y_combined == -1).sum()}"
    )

    return X_combined, y_combined, temporal_combined
