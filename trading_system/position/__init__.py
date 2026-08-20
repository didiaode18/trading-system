"""
动态仓位管理模块
================
- Kelly公式仓位计算
- 风险平价配置
- 波动率倒数加权
- 自动再平衡触发器

约束: 单只最大15%，总仓位不超90%
"""

from .kelly import kelly_position, half_kelly_position
from .risk_parity import risk_parity_weights
# FIX: 清理 dynamic_sizing 死代码导出（仅 validate_v8.py 直接引用该模块，无需包级导出；
# pyramid/black_litterman 已整文件删除，原本就未在此导出）
from .rebalance import RebalanceTrigger

__all__ = [
    "kelly_position", "half_kelly_position",
    "risk_parity_weights",
    "RebalanceTrigger",
]
