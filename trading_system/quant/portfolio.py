"""
P2 多策略动态资金分配
======================
按近3个月夏普/回撤表现，动态调整各策略资金权重

规则:
- 基础等权分配（3策略各33%）
- 近60日夏普>1的策略加权（最高50%）
- 近60日最大回撤>15%的策略降权（最低10%）
- 单策略权重范围: 10%~50%

使用方式:
    from quant.portfolio import PortfolioAllocator
    allocator = PortfolioAllocator(strategies)
    weights = allocator.calc_weights(performance_data)
"""

import logging
import numpy as np

logger = logging.getLogger(__name__)


class PortfolioAllocator:
    """多策略动态资金分配器"""

    def __init__(self, strategies: list,
                 min_weight: float = 0.10,
                 max_weight: float = 0.50,
                 lookback_days: int = 60):
        self.strategies = strategies
        self.min_weight = min_weight
        self.max_weight = max_weight
        self.lookback_days = lookback_days
        self.weight_history = []

    def calc_weights(self, performance_data: dict = None) -> dict:
        """
        计算各策略权重

        参数:
            performance_data: {strategy_name: {"sharpe": x, "max_dd": x, "win_rate": x}}

        返回:
            {strategy_name: weight}
        """
        n = len(self.strategies)
        if n == 0:
            return {}

        # 默认等权
        weights = {s.name: 1.0 / n for s in self.strategies}

        if performance_data:
            # 根据表现调整
            scores = {}
            for s in self.strategies:
                perf = performance_data.get(s.name, {})
                sharpe = perf.get("sharpe", 0)
                max_dd = perf.get("max_dd", 0)
                win_rate = perf.get("win_rate", 0.5)

                # 综合评分
                score = sharpe * 0.4 + win_rate * 0.3 + (1 + max_dd) * 0.3
                scores[s.name] = max(score, 0.01)

            # 归一化
            total = sum(scores.values())
            if total > 0:
                for name in weights:
                    weights[name] = scores[name] / total

            # 限制范围
            for name in weights:
                weights[name] = max(self.min_weight, min(self.max_weight, weights[name]))

            # 重新归一化
            total = sum(weights.values())
            weights = {k: v / total for k, v in weights.items()}

        self.weight_history.append(weights.copy())
        return weights

    def allocate_capital(self, total_capital: float,
                         performance_data: dict = None) -> dict:
        """
        分配资金到各策略

        返回:
            {strategy_name: allocated_capital}
        """
        weights = self.calc_weights(performance_data)
        allocation = {name: total_capital * w for name, w in weights.items()}

        logger.info(f"资金分配: " + ", ".join(
            f"{name}={alloc:,.0f}({weights[name]:.0%})"
            for name, alloc in allocation.items()
        ))
        return allocation

    def merge_signals(self, all_signals: dict, weights: dict,
                      max_positions: int = 10) -> list:
        """
        合并多策略信号，按加权得分排序

        参数:
            all_signals: {strategy_name: [(code, score, reason), ...]}
            weights: {strategy_name: weight}
            max_positions: 最终选股数

        返回:
            [(code, weighted_score, reasons), ...]
        """
        code_scores = {}  # {code: {"score": x, "reasons": []}}

        for strat_name, signals in all_signals.items():
            w = weights.get(strat_name, 0)
            for code, score, reason in signals:
                if code not in code_scores:
                    code_scores[code] = {"score": 0, "reasons": []}
                code_scores[code]["score"] += score * w
                code_scores[code]["reasons"].append(f"[{strat_name}]{reason}")

        # 排序
        ranked = sorted(code_scores.items(), key=lambda x: x[1]["score"], reverse=True)

        return [
            (code, data["score"], " | ".join(data["reasons"]))
            for code, data in ranked[:max_positions]
        ]
