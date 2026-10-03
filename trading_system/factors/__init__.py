"""
多因子库模块
============
50+因子的注册、计算、IC监控与合成

子模块:
- registry.py: 因子注册表与统一接口
- technical.py: 技术因子（MA/MACD/RSI/布林/ATR/KDJ...）
- volume.py: 量价因子（OBV/VWAP/量比/换手率...）
- momentum.py: 动量因子（N日收益率/相对强度/加速度...）
- quant_factors.py: quant engine因子（动量/波动/量价背离/均线趋势/突破/回调）
- quality.py: 质量因子（ROE/毛利率/现金流...）
- value.py: 价值因子（PB-ROE框架/长期反转...）
- growth.py: 成长因子（EPS/营收增速/动量代理...）
- liquidity.py: 流动性因子（Amihud非流动性/换手率...）
- dividend.py: 红利因子（股息率/低波动代理...）
- sentiment.py: 情绪因子（涨跌停比/融资余额/北向...）[待实现]
- ic_monitor.py: IC/IR计算 + 衰减预警
- composite.py: 因子正交化 + 加权合成
- neutralize.py: 行业/市值中性化
- preprocessor.py: 因子预处理四步法（去极值/标准化/中性化/缺失值）
- ta_lib_wrapper.py: TA-Lib封装层（ADX/OBV/Williams%R/MFI/SAR）
- quantile_scorer.py: 分位数打分法（对比Z-Score）
"""

from .registry import FactorRegistry, compute_all_factors
from .ic_monitor import ICMonitor
from .composite import CompositeFactor
from .preprocessor import FactorPreprocessor
from .ta_lib_wrapper import register_talib_factors
from .quantile_scorer import QuantileScorer

__all__ = [
    "FactorRegistry", "compute_all_factors",
    "ICMonitor", "CompositeFactor",
    "FactorPreprocessor",
    "register_talib_factors", "QuantileScorer",
]
