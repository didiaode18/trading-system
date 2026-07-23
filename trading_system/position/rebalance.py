"""
自动再平衡触发器
================
当持仓偏离目标权重超过阈值时触发再平衡
"""

import logging

logger = logging.getLogger(__name__)


class RebalanceTrigger:
    """
    再平衡触发器
    
    用法:
        trigger = RebalanceTrigger(threshold=0.05)
        if trigger.should_rebalance(current_weights, target_weights):
            orders = trigger.calc_rebalance_orders(...)
    """

    def __init__(self, threshold: float = 0.05):
        """
        参数:
            threshold: 偏离阈值（5%），超过则触发再平衡
        """
        self.threshold = threshold

    def should_rebalance(self, current_weights: dict, target_weights: dict) -> bool:
        """判断是否需要再平衡"""
        max_drift = self.max_drift(current_weights, target_weights)
        if max_drift > self.threshold:
            logger.info(f"触发再平衡: 最大偏离{max_drift:.1%} > 阈值{self.threshold:.0%}")
            return True
        return False

    def max_drift(self, current_weights: dict, target_weights: dict) -> float:
        """计算最大偏离"""
        all_codes = set(list(current_weights.keys()) + list(target_weights.keys()))
        max_d = 0
        for code in all_codes:
            cur = current_weights.get(code, 0)
            tgt = target_weights.get(code, 0)
            drift = abs(cur - tgt)
            max_d = max(max_d, drift)
        return max_d

    def calc_rebalance_orders(self, current_weights: dict, target_weights: dict,
                              total_capital: float, prices: dict) -> list:
        """
        计算再平衡订单
        
        返回:
            [{"code": "002371", "action": "buy/sell", "amount": 5000, "shares": 100}, ...]
        """
        orders = []
        all_codes = set(list(current_weights.keys()) + list(target_weights.keys()))

        for code in all_codes:
            cur = current_weights.get(code, 0)
            tgt = target_weights.get(code, 0)
            drift = tgt - cur

            if abs(drift) < self.threshold * 0.5:
                continue  # 偏离太小，不操作

            amount = drift * total_capital
            price = prices.get(code, 0)
            if price <= 0:
                continue

            shares = int(abs(amount) / price)
            shares = (shares // 100) * 100
            if shares <= 0:
                continue

            orders.append({
                "code": code,
                "action": "buy" if drift > 0 else "sell",
                "amount": round(amount, 0),
                "shares": shares,
                "drift": round(drift, 4),
            })

        return orders
