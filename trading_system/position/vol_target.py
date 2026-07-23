"""
波动率目标仓位管理
==================
基于组合波动率目标动态调整总仓位（《AFML》第10章 + 风险预算理论）

核心思想:
  设定组合目标年化波动率（如15%），当市场波动率升高时自动降低仓位，
  当波动率降低时允许更高仓位。保持组合风险暴露恒定。

公式:
  目标仓位比例 = 目标波动率 / 预测波动率 × 基准仓位
  实际仓位 = min(目标仓位, 硬上限) × 信号置信度

功能:
  1. 市场波动率预测（EWMA + 已实现波动率）
  2. 个股波动率计算（ATR-based）
  3. 组合波动率估算（考虑相关性）
  4. 动态仓位缩放因子
  5. 波动率regime识别（低波/正常/高波/极端）

使用方式:
    from position.vol_target import VolTargetManager
    vtm = VolTargetManager(target_vol=0.15)
    scale = vtm.calc_position_scale(data_dict, holdings)
    # scale < 1: 降仓; scale > 1: 可加仓; scale = 1: 正常
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class VolTargetManager:
    """波动率目标仓位管理器"""

    def __init__(self, target_vol: float = 0.15, lookback: int = 20,
                 ewma_lambda: float = 0.94, max_scale: float = 1.3,
                 min_scale: float = 0.3):
        """
        参数:
            target_vol: 目标年化波动率（默认15%）
            lookback: 已实现波动率回看天数
            ewma_lambda: EWMA衰减因子（0.94 = RiskMetrics标准）
            max_scale: 最大仓位缩放（不超过1.3倍）
            min_scale: 最小仓位缩放（不低于0.3倍）
        """
        self.target_vol = target_vol
        self.lookback = lookback
        self.ewma_lambda = ewma_lambda
        self.max_scale = max_scale
        self.min_scale = min_scale

    def calc_position_scale(self, data_dict: dict, holdings: dict = None,
                            market_info: dict = None) -> dict:
        """
        计算仓位缩放因子
        
        参数:
            data_dict: {code: DataFrame} 含日线数据
            holdings: {code: holding_info} 当前持仓
            market_info: 大盘状态（可选）
        
        返回:
            {
                "scale": float,              # 总仓位缩放因子
                "market_vol": float,         # 市场波动率（年化）
                "portfolio_vol": float,      # 组合预测波动率
                "vol_regime": str,           # 波动率regime
                "individual_scales": dict,   # 各股票缩放
                "recommendation": str,       # 建议
            }
        """
        if holdings is None:
            holdings = {}

        # ---- 1. 市场波动率（基准指数）----
        market_vol = self._calc_market_vol(data_dict)

        # ---- 2. 波动率regime ----
        vol_regime = self._classify_vol_regime(market_vol)

        # ---- 3. 组合波动率估算 ----
        portfolio_vol = self._calc_portfolio_vol(data_dict, holdings)

        # ---- 4. 总仓位缩放 ----
        if portfolio_vol > 0:
            raw_scale = self.target_vol / portfolio_vol
        else:
            raw_scale = 1.0

        # 限制范围
        scale = max(self.min_scale, min(self.max_scale, raw_scale))

        # 大盘弱势额外惩罚
        if market_info:
            state = market_info.get("market_state", "neutral")
            if state == "down":
                scale *= 0.7  # 熊市再打7折
            elif state == "neutral":
                scale *= 0.9

        scale = max(self.min_scale, min(self.max_scale, scale))

        # ---- 5. 个股缩放 ----
        individual_scales = self._calc_individual_scales(data_dict, holdings)

        # ---- 6. 建议 ----
        recommendation = self._generate_recommendation(
            scale, vol_regime, market_vol, portfolio_vol
        )

        return {
            "scale": round(scale, 3),
            "market_vol": round(market_vol, 4),
            "portfolio_vol": round(portfolio_vol, 4),
            "vol_regime": vol_regime,
            "individual_scales": individual_scales,
            "target_vol": self.target_vol,
            "recommendation": recommendation,
        }

    def adjust_position(self, original_shares: int, code: str,
                        vol_result: dict) -> int:
        """
        根据波动率调整具体股数
        
        参数:
            original_shares: 原始计算股数
            code: 股票代码
            vol_result: calc_position_scale()的返回值
        
        返回:
            调整后的股数（100的整数倍）
        """
        total_scale = vol_result["scale"]
        individual_scale = vol_result["individual_scales"].get(code, 1.0)

        adjusted = original_shares * total_scale * individual_scale
        # 取整到100股
        adjusted = max(100, int(adjusted / 100) * 100)
        return adjusted

    # ============================================================
    # 内部方法
    # ============================================================

    def _calc_market_vol(self, data_dict: dict) -> float:
        """计算市场波动率（EWMA）"""
        # 优先用沪深300
        benchmark = data_dict.get("000300")
        if benchmark is None or len(benchmark) < self.lookback:
            # 用所有股票的平均波动率
            vols = []
            for code, df in data_dict.items():
                if code == "000300" or len(df) < self.lookback:
                    continue
                vol = self._realized_vol(df)
                if vol > 0:
                    vols.append(vol)
            return np.mean(vols) if vols else 0.20

        return self._ewma_vol(benchmark)

    def _ewma_vol(self, df: pd.DataFrame) -> float:
        """EWMA波动率（RiskMetrics方法）"""
        returns = df["close"].pct_change().dropna()
        if len(returns) < 10:
            return 0.20

        # EWMA方差
        ewma_var = returns.var()  # 初始化
        for r in returns.tail(self.lookback * 2):
            ewma_var = self.ewma_lambda * ewma_var + (1 - self.ewma_lambda) * r ** 2

        # 年化
        annual_vol = np.sqrt(ewma_var * 252)
        return annual_vol

    def _realized_vol(self, df: pd.DataFrame) -> float:
        """已实现波动率（年化）"""
        returns = df["close"].pct_change().tail(self.lookback)
        if len(returns) < 5:
            return 0.0
        return returns.std() * np.sqrt(252)

    def _classify_vol_regime(self, annual_vol: float) -> str:
        """波动率regime分类"""
        if annual_vol < 0.12:
            return "极低波动"
        elif annual_vol < 0.20:
            return "低波动"
        elif annual_vol < 0.30:
            return "正常"
        elif annual_vol < 0.45:
            return "高波动"
        else:
            return "极端波动"

    def _calc_portfolio_vol(self, data_dict: dict, holdings: dict) -> float:
        """
        估算组合波动率（考虑相关性）
        """
        if not holdings:
            return self.target_vol  # 无持仓时返回目标值

        # 收集持仓收益率
        returns_dict = {}
        weights = []
        total_value = 0

        for code, holding in holdings.items():
            if code not in data_dict or len(data_dict[code]) < self.lookback:
                continue
            df = data_dict[code]
            ret = df["close"].pct_change().tail(self.lookback).dropna()
            if len(ret) < 10:
                continue

            shares = holding.get("shares", 0)
            price = holding.get("current_price", holding.get("buy_price", 0))
            value = shares * price
            total_value += value
            returns_dict[code] = ret
            weights.append((code, value))

        if not returns_dict or total_value == 0:
            return self.target_vol

        # 归一化权重
        weight_vec = np.array([v / total_value for _, v in weights])
        codes = [c for c, _ in weights]

        # 构建收益率矩阵
        ret_matrix = pd.DataFrame({c: returns_dict[c] for c in codes}).dropna()
        if len(ret_matrix) < 10:
            return self.target_vol

        # 协方差矩阵
        cov_matrix = ret_matrix.cov() * 252  # 年化

        # 组合波动率 = sqrt(w' * Σ * w)
        w = weight_vec[:len(codes)]
        portfolio_var = w @ cov_matrix.values @ w
        portfolio_vol = np.sqrt(max(0, portfolio_var))

        return portfolio_vol

    def _calc_individual_scales(self, data_dict: dict, holdings: dict) -> dict:
        """计算各股票的个体缩放因子"""
        scales = {}
        for code in list(holdings.keys()) + list(data_dict.keys()):
            if code == "000300":
                continue
            if code not in data_dict or len(data_dict[code]) < self.lookback:
                scales[code] = 1.0
                continue

            df = data_dict[code]
            vol = self._realized_vol(df)

            # 个股目标波动率贡献
            # 高波动股降权，低波动股加权
            if vol > 0:
                # 以25%为基准波动率
                individual_scale = 0.25 / vol
                individual_scale = max(0.5, min(1.5, individual_scale))
            else:
                individual_scale = 1.0

            scales[code] = round(individual_scale, 3)

        return scales

    def _generate_recommendation(self, scale, vol_regime, market_vol,
                                 portfolio_vol) -> str:
        """生成建议"""
        if scale < 0.5:
            return (f"[!] 波动率过高({vol_regime}, 年化{market_vol:.0%})，"
                    f"建议总仓位降至{scale:.0%}，以防守为主")
        elif scale < 0.8:
            return (f"波动率偏高({vol_regime})，建议仓位缩放至{scale:.0%}，"
                    f"减少弹性股配置")
        elif scale > 1.1:
            return (f"低波动环境({vol_regime})，可适当加仓至{scale:.0%}，"
                    f"趋势策略有效性高")
        else:
            return f"波动率正常({vol_regime})，维持当前仓位配置"
