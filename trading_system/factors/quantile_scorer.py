"""
分位数打分法（Quantile Ranking Scorer）
======================================
替代传统 Z-Score 标准化的因子打分方案，对 A 股极端值、厚尾分布更友好。

核心思路:
  Z-Score: (x - mean) / std  → 受极端值影响大
  分位数:  rank(x) / N * 100  → 仅依赖排序，不受绝对值影响

A股适配优势:
  - 厚尾分布: A股因子分布远非正态，Z-Score 假设正态分布会失真
  - 极端值: 个别股票PE>1000, 市值>1万亿, Z-Score 被极端值拉偏
  - 稳健性: 分位数排名仅依赖排序关系，对单调变换不变

使用方式:
    from factors.quantile_scorer import QuantileScorer, compare_scoring_methods
    scorer = QuantileScorer()
    scored = scorer.rank_score(factor_df)

    # 对比实验
    result = compare_scoring_methods(factor_df, forward_ret, sector_map)
    result.print_summary()
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)


class QuantileScorer:
    """
    分位数打分器

    将因子值转换为 0-100 分位排名，替代 Z-Score 标准化。
    支持分组排名（按行业分组内排名）和全市场排名。
    """

    def __init__(self, method: str = "average", ascending: bool = True):
        """
        参数:
            method: 排名方法 ("average" / "min" / "max" / "dense")
            ascending: True=值越大排名越高, False=值越小排名越高
        """
        self.method = method
        self.ascending = ascending

    def rank_score(self, factor_df: pd.DataFrame,
                   group_map: dict = None) -> pd.DataFrame:
        """
        全截面分位数打分

        参数:
            factor_df: index=股票代码, columns=因子值 (单列或多列)
            group_map: {code: sector} 分组排名用 (可选)

        返回:
            同形状 DataFrame, 值为 0-100 分位
        """
        if factor_df is None or factor_df.empty:
            return factor_df

        result = factor_df.copy()

        if group_map:
            # 分组内排名
            group_series = pd.Series(group_map)
            for col in result.columns:
                combined = pd.DataFrame({
                    "value": result[col],
                    "group": result.index.map(lambda x: group_map.get(x, "unknown"))
                })
                result[col] = combined.groupby("group")["value"].rank(
                    method=self.method, pct=True, ascending=self.ascending
                ) * 100
        else:
            # 全市场排名
            for col in result.columns:
                result[col] = result[col].rank(
                    method=self.method, pct=True, ascending=self.ascending
                ) * 100

        return result

    def rank_score_cross_section(self, factor_values: pd.Series,
                                 group_labels: pd.Series = None) -> pd.Series:
        """
        单期截面分位数打分

        参数:
            factor_values: index=股票代码, values=因子值
            group_labels: index=股票代码, values=行业名 (可选)

        返回:
            Series, 值为 0-100
        """
        if factor_values is None or factor_values.empty:
            return factor_values

        if group_labels is not None:
            combined = pd.DataFrame({"value": factor_values, "group": group_labels})
            ranked = combined.groupby("group")["value"].rank(
                method=self.method, pct=True, ascending=self.ascending
            ) * 100
            return ranked
        else:
            return factor_values.rank(
                method=self.method, pct=True, ascending=self.ascending
            ) * 100


def compare_scoring_methods(factor_df: pd.DataFrame,
                            forward_ret: pd.DataFrame,
                            sector_map: dict = None,
                            n_groups: int = 10) -> dict:
    """
    对比实验: Z-Score vs 分位数打分

    对同一批因子，分别用两种标准化方法处理后，跑分层回测对比 IC/IR/单调性。

    参数:
        factor_df: 因子面板数据 (index=date, columns=MultiIndex(code, factor) 或 dict)
        forward_ret: 前瞻收益面板 (index=date, columns=codes)
        sector_map: {code: sector} 行业分组 (可选)
        n_groups: 分层数

    返回:
        {"zscore": {...metrics}, "quantile": {...metrics}}
    """
    from scipy.stats import spearmanr

    results = {}

    for method_name, scorer_fn in [("zscore", _zscore_transform),
                                    ("quantile", _quantile_transform)]:
        try:
            # 标准化
            processed = scorer_fn(factor_df, sector_map)

            # 逐期计算截面IC
            ic_list = []
            common_dates = processed.index.intersection(forward_ret.index).sort_values()

            for date in common_dates:
                f截面 = processed.loc[date]
                r截面 = forward_ret.loc[date]

                if isinstance(f截面, pd.DataFrame):
                    # 多因子: 取第一列
                    f截面 = f截面.iloc[:, 0]

                common = f截面.dropna().index.intersection(r截面.dropna().index)
                if len(common) < 30:
                    continue

                try:
                    ic, _ = spearmanr(f截面[common].values, r截面[common].values)
                    if not np.isnan(ic):
                        ic_list.append(ic)
                except Exception:
                    pass

            if ic_list:
                ic_arr = np.array(ic_list)
                results[method_name] = {
                    "ic_mean": float(ic_arr.mean()),
                    "ic_std": float(ic_arr.std()) if len(ic_arr) > 1 else 1e-12,
                    "icir": float(ic_arr.mean() / ic_arr.std()) if len(ic_arr) > 1 and ic_arr.std() > 1e-12 else 0.0,
                    "t_stat": float(ic_arr.mean() / (ic_arr.std() / np.sqrt(len(ic_arr)))) if len(ic_arr) > 1 else 0.0,
                    "ic_positive_rate": float((ic_arr > 0).mean()),
                    "n_periods": len(ic_list),
                }
            else:
                results[method_name] = {"ic_mean": 0, "icir": 0, "t_stat": 0, "n_periods": 0}

        except Exception as e:
            logger.warning(f"对比实验 {method_name} 失败: {e}")
            results[method_name] = {"error": str(e)}

    return results


def _zscore_transform(factor_df: pd.DataFrame, sector_map: dict = None) -> pd.DataFrame:
    """Z-Score 标准化 (复用现有逻辑)"""
    result = factor_df.copy()
    for col in result.columns:
        s = result[col]
        mean = s.mean()
        std = s.std()
        if std > 1e-12:
            result[col] = (s - mean) / std
        else:
            result[col] = 0.0
        # 截断 ±5
        result[col] = result[col].clip(-5, 5)
    return result


def _quantile_transform(factor_df: pd.DataFrame, sector_map: dict = None) -> pd.DataFrame:
    """分位数排名转换"""
    scorer = QuantileScorer()
    result = factor_df.copy()
    for col in result.columns:
        result[col] = result[col].rank(method="average", pct=True) * 100
    return result
