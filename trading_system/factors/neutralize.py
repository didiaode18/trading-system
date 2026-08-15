# -*- coding: utf-8 -*-
"""
V4.0(G9): 因子行业/市值中性化工具
==================================
提供截面因子中性化能力，消除因子值中行业beta与市值beta的干扰，
使因子排序反映个股自身alpha而非行业/市值风格暴露。

方法（经典Barra式截面回归取残差的轻量实现，无sklearn依赖）:
    factor_i = α + β·ln(mcap_i) + γ_sector(i) + ε_i
    neutralized_i = ε_i（残差）

降级策略（风控优先，绝不抛异常影响主流程）:
    - 行业内样本 < 3: 该行业退化为全市场去均值
    - 无市值数据: 仅做行业中性化
    - 无行业映射: 仅做市值中性化（有市值时）或原样返回
    - 全市场样本 < 5: 原样返回（截面太小中性化无统计意义）

用法示例:
    from factors.neutralize import neutralize_factor
    mom = pd.Series({"600001": 12.3, "000002": 5.1, ...})  # 因子截面值
    neutral = neutralize_factor(mom, sector_map, mcap_map)
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 行业内样本低于该值时，行业内回归无统计意义，退化为全市场去均值
MIN_SECTOR_SAMPLES = 3
# 全市场截面样本低于该值时不做中性化，原样返回
MIN_TOTAL_SAMPLES = 5


def neutralize_by_sector(factor_series: pd.Series, sector_map: dict) -> pd.Series:
    """
    行业中性化（行业内去均值）

    参数:
        factor_series: 因子截面值，index=股票代码
        sector_map: {code: 行业名}，缺失行业的股票归入"未知"组

    返回: 与输入同index的Series（行业内去均值后的残差）。
          样本不足/异常时原样返回副本，绝不抛异常。
    """
    try:
        if factor_series is None or len(factor_series) < MIN_TOTAL_SAMPLES:
            return factor_series.copy() if factor_series is not None else factor_series
        sector_map = sector_map or {}
        s = pd.to_numeric(factor_series, errors="coerce")
        valid = s.dropna()
        if len(valid) < MIN_TOTAL_SAMPLES:
            return factor_series.copy()

        result = s.astype(float).copy()
        sectors = pd.Series(
            [str(sector_map.get(c, "未知")) for c in valid.index], index=valid.index)
        # 行业内样本>=MIN_SECTOR_SAMPLES: 组内去均值；否则并入全市场均值
        global_mean = float(valid.mean())
        group_means = valid.groupby(sectors).transform("mean")
        group_counts = valid.groupby(sectors).transform("size")
        means = group_means.where(group_counts >= MIN_SECTOR_SAMPLES, global_mean)
        result.loc[valid.index] = valid - means
        return result
    except Exception as e:
        logger.warning(f"[因子中性化] 行业中性化异常，原样返回: {e}")
        return factor_series.copy() if factor_series is not None else factor_series


def neutralize_factor(factor_series: pd.Series, sector_map: dict = None,
                      mcap_map: dict = None) -> pd.Series:
    """
    行业+市值联合中性化（截面回归取残差）

    流程:
        1. 行业中性化（行业内去均值）
        2. 有市值数据时，对 ln(市值) 做行业内OLS回归，取残差；
           无市值数据时直接返回行业中性化结果

    参数:
        factor_series: 因子截面值，index=股票代码
        sector_map: {code: 行业名}（可为None）
        mcap_map: {code: 总市值(元)}（可为None；<=0或缺失的股票跳过市值回归）

    返回: 与输入同index的Series。任何异常原样返回副本，绝不抛异常。
    """
    try:
        if factor_series is None or len(factor_series) < MIN_TOTAL_SAMPLES:
            return factor_series.copy() if factor_series is not None else factor_series

        # 第一步: 行业中性化
        step1 = neutralize_by_sector(factor_series, sector_map) if sector_map else step1_copy(factor_series)
        step1 = step1.astype(float)  # 避免int截面写回float残差触发dtype告警

        # 第二步: 市值中性化（行业内对ln(mcap)回归取残差）
        if not mcap_map:
            return step1

        s = pd.to_numeric(step1, errors="coerce").dropna()
        if len(s) < MIN_TOTAL_SAMPLES:
            return step1

        ln_mcap = pd.Series(
            {c: np.log(float(v)) for c, v in mcap_map.items()
             if c in s.index and v and float(v) > 0})
        common = s.index.intersection(ln_mcap.index)
        if len(common) < MIN_TOTAL_SAMPLES:
            return step1

        sectors = pd.Series(
            [str((sector_map or {}).get(c, "未知")) for c in common], index=common)
        result = step1.copy()
        for _sector, idx in sectors.groupby(sectors).groups.items():
            idx = list(idx)
            y = s.loc[idx].values.astype(float)
            x = ln_mcap.loc[idx].values.astype(float)
            if len(idx) < MIN_SECTOR_SAMPLES or np.std(x) < 1e-9:
                continue  # 组内样本/方差不足，跳过市值回归
            try:
                # OLS: y = a + b*x → 残差
                A = np.column_stack([np.ones(len(x)), x])
                beta, *_ = np.linalg.lstsq(A, y, rcond=None)
                residual = y - A @ beta
                result.loc[idx] = residual
            except Exception:
                continue
        return result
    except Exception as e:
        logger.warning(f"[因子中性化] 联合中性化异常，原样返回: {e}")
        return factor_series.copy() if factor_series is not None else factor_series


def step1_copy(factor_series: pd.Series) -> pd.Series:
    """内部辅助: 无行业映射时的起点副本（数值化）"""
    return pd.to_numeric(factor_series, errors="coerce").copy()


def neutralize_factor_matrix(factor_df: pd.DataFrame, sector_map: dict = None,
                             mcap_map: dict = None) -> pd.DataFrame:
    """
    批量中性化: 对DataFrame的每一列（因子）分别做联合中性化

    参数:
        factor_df: index=股票代码, columns=因子名
        sector_map / mcap_map: 同 neutralize_factor

    返回: 同形状DataFrame。输入为空时原样返回。
    """
    try:
        if factor_df is None or factor_df.empty:
            return factor_df
        out = pd.DataFrame(index=factor_df.index)
        for col in factor_df.columns:
            out[col] = neutralize_factor(factor_df[col], sector_map, mcap_map)
        return out
    except Exception as e:
        logger.warning(f"[因子中性化] 批量中性化异常，原样返回: {e}")
        return factor_df
