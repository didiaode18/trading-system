"""
模拟盘验证模块
==============
新策略上线前必须跑模拟盘：
- 模拟交易引擎（不实际下单）
- 持仓跟踪 + 每日净值
- 毕业条件：20天夏普>1.0 且 最大回撤<10%
"""

from paper_trading.simulator import PaperTradingSimulator

__all__ = ["PaperTradingSimulator"]
