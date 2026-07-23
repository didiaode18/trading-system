"""
Barra风格因子归因
=================
将组合收益分解为风格因子暴露 + 选股Alpha

核心功能:
  1. 风格因子构建（市场/规模/价值/动量/质量/波动率）
  2. 组合因子暴露计算
  3. 收益归因分解（因子收益 vs 选股Alpha）
  4. Alpha稳定性评估
  5. 因子拥挤度检测

原理（Barra风险模型简化版）:
  R_portfolio = β_market × R_market + β_size × R_size + β_value × R_value
                + β_momentum × R_momentum + β_quality × R_quality + Alpha + ε

  如果收益主要来自β_market（市场涨），说明选股能力弱
  如果Alpha > 0 且稳定，说明有真正的选股能力

使用方式:
    from attribution.barra import BarraAttribution
    barra = BarraAttribution()
    result = barra.attribute(portfolio_returns, factor_returns)
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class BarraAttribution:
    """Barra风格因子归因"""

    FACTOR_NAMES = ["market", "size", "value", "momentum", "quality", "volatility"]
    FACTOR_CN = {
        "market": "市场Beta",
        "size": "规模因子",
        "value": "价值因子",
        "momentum": "动量因子",
        "quality": "质量因子",
        "volatility": "低波因子",
    }

    def __init__(self, lookback: int = 60):
        self.lookback = lookback

    def attribute(self, portfolio_returns: pd.Series, factor_returns: pd.DataFrame,
                  holdings: dict = None, data_dict: dict = None) -> dict:
        """
        执行因子归因
        
        参数:
            portfolio_returns: 组合日收益率序列
            factor_returns: 因子日收益率 DataFrame (columns=因子名)
            holdings: 当前持仓（用于计算暴露）
            data_dict: {code: DataFrame}（用于计算因子暴露）
        
        返回:
            {
                "factor_exposures": dict,    # 各因子暴露（Beta）
                "factor_contribution": dict, # 各因子收益贡献
                "alpha": float,              # 选股Alpha（年化）
                "alpha_tstat": float,        # Alpha显著性
                "r_squared": float,          # 模型拟合度
                "residual_vol": float,       # 残差波动率（特质风险）
                "interpretation": str,       # 解读
            }
        """
        if portfolio_returns is None or len(portfolio_returns) < 20:
            return {"error": "收益率数据不足"}

        # 对齐数据
        if factor_returns is not None and not factor_returns.empty:
            common_idx = portfolio_returns.index.intersection(factor_returns.index)
            y = portfolio_returns.loc[common_idx].values
            X = factor_returns.loc[common_idx].values
            factor_names = factor_returns.columns.tolist()
        else:
            # 无外部因子数据，用持仓数据构建简化因子
            y = portfolio_returns.values
            X, factor_names = self._build_factors_from_holdings(holdings, data_dict, len(y))
            if X is None:
                return {"error": "无法构建因子数据"}

        n = len(y)
        if n < 20:
            return {"error": f"有效样本不足({n}<20)"}

        # ---- OLS回归: y = Xβ + α + ε ----
        # 加入截距项（Alpha）
        X_with_const = np.column_stack([np.ones(n), X])

        try:
            # β = (X'X)^-1 X'y
            beta = np.linalg.lstsq(X_with_const, y, rcond=None)[0]
        except np.linalg.LinAlgError:
            return {"error": "回归计算失败（多重共线性）"}

        alpha_daily = beta[0]  # 日度Alpha
        factor_betas = beta[1:]  # 因子暴露

        # 拟合值与残差
        y_hat = X_with_const @ beta
        residuals = y - y_hat
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        # Alpha统计显著性
        n_params = X_with_const.shape[1]
        mse = ss_res / max(1, n - n_params)
        try:
            var_beta = mse * np.linalg.inv(X_with_const.T @ X_with_const)
            alpha_se = np.sqrt(var_beta[0, 0])
            alpha_tstat = alpha_daily / alpha_se if alpha_se > 0 else 0
        except np.linalg.LinAlgError:
            alpha_tstat = 0

        # 年化
        alpha_annual = alpha_daily * 252
        residual_vol_annual = np.std(residuals) * np.sqrt(252)

        # 因子收益贡献
        factor_contribution = {}
        for i, name in enumerate(factor_names):
            # 贡献 = 暴露 × 因子平均收益
            factor_mean = np.mean(X[:, i]) * 252
            contribution = factor_betas[i] * factor_mean
            factor_contribution[name] = {
                "exposure": round(float(factor_betas[i]), 4),
                "factor_return_annual": round(float(factor_mean), 4),
                "contribution_annual": round(float(contribution), 4),
                "cn": self.FACTOR_CN.get(name, name),
            }

        # 总因子贡献
        total_factor_contrib = sum(
            v["contribution_annual"] for v in factor_contribution.values()
        )

        # ---- 解读 ----
        interpretation = self._interpret(
            alpha_annual, alpha_tstat, r_squared,
            factor_contribution, total_factor_contrib
        )

        return {
            "factor_exposures": {
                name: round(float(factor_betas[i]), 4)
                for i, name in enumerate(factor_names)
            },
            "factor_contribution": factor_contribution,
            "alpha_annual": round(float(alpha_annual), 4),
            "alpha_daily": round(float(alpha_daily), 6),
            "alpha_tstat": round(float(alpha_tstat), 3),
            "alpha_significant": abs(alpha_tstat) > 2.0,
            "r_squared": round(float(r_squared), 4),
            "residual_vol_annual": round(float(residual_vol_annual), 4),
            "total_factor_contribution": round(float(total_factor_contrib), 4),
            "portfolio_return_annual": round(float(np.mean(y) * 252), 4),
            "interpretation": interpretation,
            "sample_size": n,
        }

    def _build_factors_from_holdings(self, holdings: dict, data_dict: dict,
                                     n_days: int):
        """从持仓数据构建简化因子"""
        if not data_dict or "000300" not in data_dict:
            return None, []

        # 市场因子：沪深300收益率
        benchmark = data_dict["000300"]
        if len(benchmark) < n_days + 1:
            return None, []

        market_ret = benchmark["close"].pct_change().tail(n_days).values

        # 简化：只用市场因子
        X = market_ret.reshape(-1, 1)
        return X, ["market"]

    def _interpret(self, alpha, tstat, r2, contributions, total_contrib) -> str:
        """生成归因解读"""
        parts = []

        # Alpha评估
        if alpha > 0.05 and tstat > 2:
            parts.append(f"★选股Alpha显著为正(年化{alpha:.1%}, t={tstat:.1f})，具备真实选股能力")
        elif alpha > 0 and tstat > 1.5:
            parts.append(f"选股Alpha为正(年化{alpha:.1%})但显著性一般(t={tstat:.1f})")
        elif alpha < 0:
            parts.append(f"⚠️选股Alpha为负(年化{alpha:.1%})，选股在拖累收益")
        else:
            parts.append(f"选股Alpha接近零(年化{alpha:.1%})，收益主要来自因子暴露")

        # 市场Beta
        market_contrib = contributions.get("market", {}).get("contribution_annual", 0)
        if abs(market_contrib) > abs(total_contrib) * 0.7:
            parts.append("收益主要来自市场Beta（跟着大盘涨），非选股功劳")

        # 拟合度
        if r2 > 0.8:
            parts.append(f"模型拟合度高(R²={r2:.2f})，收益可被因子解释")
        elif r2 < 0.4:
            parts.append(f"模型拟合度低(R²={r2:.2f})，收益来源复杂或数据不足")

        return " | ".join(parts)
