# -*- coding: utf-8 -*-
"""
因子预处理四步法（V7.0 Phase0 新增）
====================================
所有因子接入生产前必须执行此流水线：
1. 去极值: 0.5%分位MAD缩尾（Winsorize）
2. 标准化: Z-Score
3. 中性化: 行业+市值联合中性化（调用 neutralize.py）
4. 缺失值: 行业均值填充（禁止用0填充）

用法:
    from factors.preprocessor import FactorPreprocessor
    pp = FactorPreprocessor()
    processed = pp.process(factor_df, sector_map, mcap_map)
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class FactorPreprocessor:
    """
    因子预处理器

    四步法流水线，每步可独立调用也可整体执行。
    设计原则: 任何步骤异常时原样返回，绝不抛异常影响主流程。
    """

    def __init__(self, winsorize_pct: float = 0.005,
                 zscore_clip: float = 5.0):
        """
        参数:
            winsorize_pct: 缩尾百分比（双侧），默认0.5%
            zscore_clip: Z-Score截断阈值，超过此值截断（防极端值）
        """
        self.winsorize_pct = winsorize_pct
        self.zscore_clip = zscore_clip

    def process(self, factor_df: pd.DataFrame,
                sector_map: dict = None,
                mcap_map: dict = None) -> pd.DataFrame:
        """
        执行完整四步预处理

        参数:
            factor_df: index=股票代码, columns=因子值
            sector_map: {code: industry_name}（中性化用）
            mcap_map: {code: total_mcap}（市值中性化用）

        返回:
            同形状DataFrame，预处理后的因子值
        """
        try:
            if factor_df is None or factor_df.empty:
                return factor_df

            result = factor_df.copy()

            # Step 1: 去极值
            for col in result.columns:
                result[col] = self.winsorize(result[col])

            # Step 2: 标准化
            for col in result.columns:
                result[col] = self.zscore(result[col])

            # Step 3: 中性化
            if sector_map or mcap_map:
                result = self.neutralize(result, sector_map, mcap_map)

            # Step 4: 缺失值填充
            if sector_map:
                result = self.fill_missing(result, sector_map)
            else:
                result = result.fillna(0)

            return result
        except Exception as e:
            logger.warning(f"[预处理] 流水线异常，原样返回: {e}")
            return factor_df.copy()

    def winsorize(self, series: pd.Series) -> pd.Series:
        """
        Step 1: 去极值（MAD缩尾）

        方法: 将超过上下限的值截断到边界值
        上下限 = 中位数 ± 5 * MAD
        其中 MAD = median(|x - median(x)|)
        """
        try:
            s = pd.to_numeric(series, errors="coerce")
            if s.dropna().empty or len(s.dropna()) < 5:
                return s

            median = s.median()
            mad = (s - median).abs().median()
            if mad < 1e-10:
                return s

            # MAD-based bounds
            upper = median + 5 * mad
            lower = median - 5 * mad

            # 同时用分位数缩尾（取更严格的）
            q_low = s.quantile(self.winsorize_pct)
            q_high = s.quantile(1 - self.winsorize_pct)

            bound_low = max(lower, q_low)
            bound_high = min(upper, q_high)

            return s.clip(bound_low, bound_high)
        except Exception as e:
            logger.debug(f"[去极值] 异常: {e}")
            return series.copy()

    def zscore(self, series: pd.Series) -> pd.Series:
        """
        Step 2: Z-Score标准化

        z = (x - mean) / std
        超过 zscore_clip 的值截断
        """
        try:
            s = pd.to_numeric(series, errors="coerce")
            mean = s.mean()
            std = s.std()
            if std is None or std < 1e-10 or np.isnan(std):
                # FIX P2-1: 无方差时全填0（含NaN），避免 NaN 传播
                return pd.Series(0.0, index=s.index)
            z = (s - mean) / std
            # FIX P2-1: 标准化后兑底填充剩余NaN（如原始数据含NaN）
            z = z.fillna(0)
            return z.clip(-self.zscore_clip, self.zscore_clip)
        except Exception as e:
            logger.debug(f"[标准化] 异常: {e}")
            return series.copy()

    def neutralize(self, factor_df: pd.DataFrame,
                   sector_map: dict = None,
                   mcap_map: dict = None) -> pd.DataFrame:
        """
        Step 3: 中性化（调用 neutralize.py）

        行业+市值联合中性化，取残差
        """
        try:
            from factors.neutralize import neutralize_factor_matrix
            return neutralize_factor_matrix(factor_df, sector_map, mcap_map)
        except ImportError:
            logger.debug("[中性化] neutralize模块不可用，跳过")
            return factor_df
        except Exception as e:
            logger.warning(f"[中性化] 异常，跳过: {e}")
            return factor_df

    def fill_missing(self, factor_df: pd.DataFrame,
                     sector_map: dict = None) -> pd.DataFrame:
        """
        Step 4: 缺失值填充

        优先用行业均值填充，无行业信息时用全市场均值
        禁止用0填充（0在Z-Score后是中位数，不代表缺失）
        """
        try:
            result = factor_df.copy()
            if sector_map and len(sector_map) > 0:
                # 按行业填充均值
                sectors = pd.Series(
                    {code: sector_map.get(code, "未知") for code in result.index}
                )
                for col in result.columns:
                    for sector in sectors.unique():
                        mask = sectors == sector
                        sector_mean = result.loc[mask, col].mean()
                        if not np.isnan(sector_mean):
                            result.loc[mask & result[col].isna(), col] = sector_mean
                # 剩余NaN用全市场均值
                result = result.fillna(result.mean())
            else:
                result = result.fillna(result.mean())

            # 最终兜底: 仍NaN则填0
            result = result.fillna(0)
            return result
        except Exception as e:
            logger.warning(f"[缺失值] 填充异常: {e}")
            return factor_df.fillna(0)


# ============================================================
# 便捷接口
# ============================================================

_GLOBAL_PREPROCESSOR = None


def get_preprocessor() -> FactorPreprocessor:
    """获取全局预处理器（单例）"""
    global _GLOBAL_PREPROCESSOR
    if _GLOBAL_PREPROCESSOR is None:
        _GLOBAL_PREPROCESSOR = FactorPreprocessor()
    return _GLOBAL_PREPROCESSOR


def preprocess_factors(factor_df: pd.DataFrame,
                       sector_map: dict = None,
                       mcap_map: dict = None) -> pd.DataFrame:
    """便捷接口: 四步预处理"""
    return get_preprocessor().process(factor_df, sector_map, mcap_map)
