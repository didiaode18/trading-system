"""
多因子库模块
============
50+因子的注册、计算、IC监控与合成

子模块:
- registry.py: 因子注册表与统一接口
- technical.py: 技术因子（MA/MACD/RSI/布林/ATR/KDJ...）
- volume.py: 量价因子（OBV/VWAP/量比/换手率...）
- momentum.py: 动量因子（N日收益率/相对强度/加速度...）
- quality.py: 质量因子（ROE/毛利率/现金流...）
- sentiment.py: 情绪因子（涨跌停比/融资余额/北向...）
- ic_monitor.py: IC/IR计算 + 衰减预警
- composite.py: 因子正交化 + 加权合成
"""

from factors.registry import FactorRegistry, compute_all_factors
from factors.ic_monitor import ICMonitor
from factors.composite import CompositeFactor

__all__ = [
    "FactorRegistry", "compute_all_factors",
    "ICMonitor", "CompositeFactor",
]
