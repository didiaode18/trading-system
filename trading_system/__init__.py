"""
高胜率A股交易操作系统 V2.0
=========================

基于「高胜率A股交易操作系统V2.0」规则构建的量化交易系统。

模块结构:
    - data: 行情数据获取与增量更新
    - strategy: 趋势策略与仓位计算
    - risk: 风控熔断校验
    - notify: 钉钉/企微通知
    - output: 条件单Excel生成

核心功能:
    - 全市场选股（stock_screener.py）
    - 中线趋势交易信号扫描（中线趋势交易信号.py）
    - 持仓信号监控（持仓信号监控.py）
    - 完整交易流程（main.py）
"""

__version__ = "2.0.0"
__author__ = "Trading System V2.0"

from . import config
from .data import data_loader
from .strategy import trend_strategy, position
from .risk import risk_control
from .notify import wechat_notify
from .output import condition_sheet

__all__ = [
    "config",
    "data_loader",
    "trend_strategy",
    "position",
    "risk_control",
    "wechat_notify",
    "condition_sheet",
]
