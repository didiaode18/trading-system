"""
Black-Litterman 观点融合仓位优化
================================
结合市场均衡（先验）与主观观点（条件），贝叶斯融合得到最优权重

核心思想:
  纯量化模型可能产生极端配置（全仓一只股），
  纯主观判断又缺乏纪律性。
  Black-Litterman将两者融合：
  - 先验：市场均衡权重（从市值/等权推导）
  - 观点：你的判断（如"半导体未来1月涨10%"）
  - 输出：贝叶斯后验最优权重

公式:
  E[R] = [(τΣ)^-1 + P'Ω^-1 P]^-1 × [(τΣ)^-1 Π + P'Ω^-1 Q]
  w* = (δΣ)^-1 × E[R]

简化实现:
  使用缩放因子τ控制观点影响力，
  τ越小 → 越信任市场均衡；τ越大 → 越信任主观观点

使用方式:
    from position.black_litterman import BlackLittermanOptimizer
    bl = BlackLittermanOptimizer()
    weights = bl.optimize(codes, views, data_dict)
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class BlackLittermanOptimizer:
    """Black-Litterman 仓位优化器"""

    def __init__(self, tau: float = 0.05, risk_aversion: float = 2.5,
                 max_weight: float = 0.25, min_weight: float = 0.0):
        """
        参数:
            tau: 观点不确定性缩放因子（0.01~0.10，越大越信任观点）
            risk_aversion: 风险厌恶系数（δ，越大越保守）
            max_weight: 单只最大权重
            min_weight: 单只最小权重（0=允许空仓）
        """
        self.tau = tau
        self.risk_aversion = risk_aversion
        self.max_weight = max_weight
        self.min_weight = min_weight

    def optimize(self, codes: list, views: dict = None,
                 data_dict: dict = None, market_caps: dict = None) -> dict:
        """
        计算BL最优权重
        
        参数:
            codes: 股票代码列表
            views: 观点字典 {code: expected_return}（如 {"002415": 0.10}）
                   或 {code: {"return": 0.10, "confidence": 0.7}}
            data_dict: {code: DataFrame}（用于计算协方差）
            market_caps: {code: market_cap}（用于均衡权重，默认等权）
        
        返回:
            {
                "weights": {code: weight},
                "expected_returns": {code: er},
                "equilibrium_weights": {code: w},
                "views_applied": list,
                "interpretation": str,
            }
        """
        n = len(codes)
        if n == 0:
            return {"weights": {}, "error": "无标的"}

        # ---- 1. 均衡权重（先验）----
        if market_caps:
            total_cap = sum(market_caps.get(c, 1) for c in codes)
            w_eq = np.array([market_caps.get(c, 1) / total_cap for c in codes])
        else:
            w_eq = np.ones(n) / n  # 等权

        # ---- 2. 协方差矩阵 ----
        Sigma = self._calc_covariance(codes, data_dict)
        if Sigma is None:
            # 无法计算协方差，返回等权
            weights = {c: round(1.0/n, 4) for c in codes}
            return {"weights": weights, "error": "数据不足，返回等权"}

        # ---- 3. 隐含均衡收益 Π = δ × Σ × w_eq ----
        Pi = self.risk_aversion * Sigma @ w_eq

        # ---- 4. 观点矩阵 ----
        if views:
            P, Q, Omega = self._build_views(codes, views)
        else:
            P, Q, Omega = None, None, None

        # ---- 5. BL后验收益 ----
        if P is not None and Q is not None:
            # E[R] = [(τΣ)^-1 + P'Ω^-1 P]^-1 × [(τΣ)^-1 Π + P'Ω^-1 Q]
            tau_Sigma = self.tau * Sigma
            try:
                tau_Sigma_inv = np.linalg.inv(tau_Sigma)
                Omega_inv = np.linalg.inv(Omega)

                M = np.linalg.inv(tau_Sigma_inv + P.T @ Omega_inv @ P)
                E_R = M @ (tau_Sigma_inv @ Pi + P.T @ Omega_inv @ Q)
            except np.linalg.LinAlgError:
                E_R = Pi  # 矩阵奇异，回退到均衡
        else:
            E_R = Pi

        # ---- 6. 最优权重 w* = (δΣ)^-1 × E[R] ----
        try:
            Sigma_inv = np.linalg.inv(Sigma)
            w_opt = (1 / self.risk_aversion) * Sigma_inv @ E_R
        except np.linalg.LinAlgError:
            w_opt = w_eq

        # ---- 7. 约束处理 ----
        w_opt = self._apply_constraints(w_opt)

        # 归一化
        w_sum = w_opt.sum()
        if w_sum > 0:
            w_opt = w_opt / w_sum

        # ---- 8. 输出 ----
        weights = {codes[i]: round(float(w_opt[i]), 4) for i in range(n)}
        expected_returns = {codes[i]: round(float(E_R[i]), 4) for i in range(n)}
        eq_weights = {codes[i]: round(float(w_eq[i]), 4) for i in range(n)}

        # 解读
        interpretation = self._interpret(weights, eq_weights, views)

        return {
            "weights": weights,
            "expected_returns": expected_returns,
            "equilibrium_weights": eq_weights,
            "views_applied": list(views.keys()) if views else [],
            "tau": self.tau,
            "risk_aversion": self.risk_aversion,
            "interpretation": interpretation,
        }

    def views_from_signals(self, signals: list, data_dict: dict = None) -> dict:
        """
        从策略信号自动生成观点
        
        规则:
        - 买入信号 → 预期收益 +5%~+15%（基于信号质量）
        - 卖出信号 → 预期收益 -5%~-10%
        - 加仓信号 → 预期收益 +3%~+8%
        """
        views = {}
        for code, sig in signals:
            if sig.get("buy_signal"):
                quality = sig.get("quality_score", 50)
                expected = 0.05 + (quality / 100) * 0.10  # 5%~15%
                confidence = 0.5 + (quality / 100) * 0.3  # 0.5~0.8
                views[code] = {"return": expected, "confidence": confidence}
            elif sig.get("sell_signal"):
                views[code] = {"return": -0.07, "confidence": 0.7}
            elif sig.get("add_position"):
                views[code] = {"return": 0.05, "confidence": 0.6}

        return views

    # ============================================================
    # 内部方法
    # ============================================================

    def _calc_covariance(self, codes: list, data_dict: dict,
                         lookback: int = 60) -> np.ndarray:
        """计算年化协方差矩阵"""
        if not data_dict:
            return None

        returns_list = []
        valid_codes = []
        for code in codes:
            if code not in data_dict or len(data_dict[code]) < lookback:
                continue
            df = data_dict[code]
            ret = df["close"].pct_change().tail(lookback).dropna()
            if len(ret) < 20:
                continue
            returns_list.append(ret.values)
            valid_codes.append(code)

        if len(returns_list) < 2:
            return None

        # 对齐长度
        min_len = min(len(r) for r in returns_list)
        returns_matrix = np.array([r[:min_len] for r in returns_list])

        # 年化协方差
        cov = np.cov(returns_matrix) * 252

        # 确保正定（加小量对角）
        cov += np.eye(len(valid_codes)) * 1e-6

        return cov

    def _build_views(self, codes: list, views: dict):
        """构建观点矩阵 P, Q, Ω"""
        n = len(codes)
        view_codes = [c for c in views.keys() if c in codes]
        k = len(view_codes)

        if k == 0:
            return None, None, None

        # P: k×n 观点矩阵（每行一个观点）
        P = np.zeros((k, n))
        Q = np.zeros(k)  # 观点收益
        Omega = np.zeros((k, k))  # 观点不确定性

        for i, code in enumerate(view_codes):
            idx = codes.index(code)
            P[i, idx] = 1.0  # 绝对观点

            view = views[code]
            if isinstance(view, dict):
                Q[i] = view.get("return", 0.05)
                conf = view.get("confidence", 0.5)
                # 不确定性 = (1/confidence - 1) × 方差
                Omega[i, i] = (1 / conf - 1) * 0.04  # 假设年化方差4%
            else:
                Q[i] = float(view)
                Omega[i, i] = 0.04  # 默认不确定性

        return P, Q, Omega

    def _apply_constraints(self, weights: np.ndarray) -> np.ndarray:
        """应用权重约束"""
        # 截断
        weights = np.clip(weights, self.min_weight, self.max_weight)
        # 归一化
        w_sum = weights.sum()
        if w_sum > 0:
            weights = weights / w_sum
        return weights

    def _interpret(self, weights, eq_weights, views) -> str:
        """生成解读"""
        if not views:
            return "无主观观点，返回均衡配置"

        # 找出观点导致的最大偏离
        max_dev_code = ""
        max_dev = 0
        for code in weights:
            dev = abs(weights[code] - eq_weights.get(code, 0))
            if dev > max_dev:
                max_dev = dev
                max_dev_code = code

        name = config.get_stock_name(max_dev_code)
        return (
            f"BL融合{len(views)}个观点后，"
            f"{name}({max_dev_code})权重偏离均衡最大({max_dev:.1%})，"
            f"τ={self.tau}控制观点影响力"
        )
