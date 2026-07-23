"""
因子合成模块
============
- 因子正交化（去除共线性，VIF>10剔除）
- 加权合成（IC加权/等权/自定义）
- 输出综合因子得分
"""

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class CompositeFactor:
    """
    因子合成器
    
    用法:
        comp = CompositeFactor()
        score = comp.compute_score(factor_df, method="ic_weighted")
    """

    def __init__(self, vif_threshold: float = 10.0):
        self.vif_threshold = vif_threshold

    def orthogonalize(self, factor_df: pd.DataFrame) -> pd.DataFrame:
        """
        因子正交化：逐步回归去除共线性
        
        返回:
            正交化后的因子DataFrame
        """
        if factor_df.empty or factor_df.shape[1] < 2:
            return factor_df

        # 去除NaN行
        clean = factor_df.dropna()
        if len(clean) < 10:
            return factor_df

        # 计算VIF，剔除高共线性因子
        selected = self._remove_high_vif(clean)
        logger.info(f"正交化: {factor_df.shape[1]}个因子 → {len(selected)}个 (VIF<{self.vif_threshold})")

        return factor_df[selected]

    def _remove_high_vif(self, df: pd.DataFrame) -> list:
        """逐步剔除VIF>阈值的因子"""
        cols = list(df.columns)
        if len(cols) < 2:
            return cols

        # 标准化
        standardized = (df - df.mean()) / df.std().replace(0, 1)

        remaining = cols.copy()
        while True:
            if len(remaining) < 2:
                break

            # 计算每个因子的VIF
            vifs = {}
            for i, col in enumerate(remaining):
                others = [c for c in remaining if c != col]
                if not others:
                    vifs[col] = 1.0
                    continue
                try:
                    X = standardized[others].values
                    y = standardized[col].values
                    # R² = 1 - SS_res/SS_tot
                    X_with_const = np.column_stack([np.ones(len(X)), X])
                    beta = np.linalg.lstsq(X_with_const, y, rcond=None)[0]
                    y_pred = X_with_const @ beta
                    ss_res = np.sum((y - y_pred) ** 2)
                    ss_tot = np.sum((y - y.mean()) ** 2)
                    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
                    vif = 1 / (1 - r2) if r2 < 1 else 999
                except Exception:
                    vif = 1.0
                vifs[col] = vif

            # 找最大VIF
            max_vif_col = max(vifs, key=vifs.get)
            if vifs[max_vif_col] > self.vif_threshold:
                remaining.remove(max_vif_col)
                logger.debug(f"  剔除 {max_vif_col} (VIF={vifs[max_vif_col]:.1f})")
            else:
                break

        return remaining

    def compute_score(self, factor_df: pd.DataFrame,
                      weights: dict = None, method: str = "equal") -> pd.Series:
        """
        计算综合因子得分
        
        参数:
            factor_df: 因子DataFrame (index=样本, columns=因子)
            weights: 自定义权重 {factor_name: weight}
            method: "equal"(等权) / "ic_weighted"(IC加权) / "custom"(自定义)
        
        返回:
            综合得分Series
        """
        if factor_df.empty:
            return pd.Series(dtype=float)

        # 标准化（Z-Score）
        standardized = (factor_df - factor_df.mean()) / factor_df.std().replace(0, 1)
        standardized = standardized.fillna(0)

        if method == "equal":
            # 等权合成
            score = standardized.mean(axis=1)

        elif method == "ic_weighted":
            # IC加权（需要外部传入weights）
            if weights:
                w = pd.Series(weights)
                # 只保留存在的因子
                common = standardized.columns.intersection(w.index)
                if len(common) > 0:
                    w_norm = w[common] / w[common].abs().sum()
                    score = (standardized[common] * w_norm).sum(axis=1)
                else:
                    score = standardized.mean(axis=1)
            else:
                score = standardized.mean(axis=1)

        elif method == "custom" and weights:
            w = pd.Series(weights)
            common = standardized.columns.intersection(w.index)
            if len(common) > 0:
                score = (standardized[common] * w[common]).sum(axis=1)
            else:
                score = standardized.mean(axis=1)
        else:
            score = standardized.mean(axis=1)

        return score

    def rank_score(self, score: pd.Series) -> pd.Series:
        """将得分转为排名百分位(0~1)"""
        return score.rank(pct=True)
