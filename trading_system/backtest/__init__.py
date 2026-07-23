"""
回测引擎模块 V2.0
==================
事件驱动回测 + Walk-Forward防过拟合 + HTML报告

核心组件:
- engine.py: 事件驱动回测引擎（滑点+手续费+T+1）
- broker.py: 模拟撮合（A股真实交易环境）
- data_feed.py: 历史数据馈送
- metrics.py: 绩效指标计算（夏普/Calmar/Sortino/Alpha/Beta）
- walk_forward.py: 滚动窗口防过拟合
- report.py: HTML可视化报告
"""

from backtest.engine import BacktestEngineV2, run_backtest_v2
from backtest.walk_forward import WalkForwardAnalyzer, run_walk_forward
from backtest.report import generate_html_report
from backtest.metrics import generate_performance_report

__all__ = [
    "BacktestEngineV2", "run_backtest_v2",
    "WalkForwardAnalyzer", "run_walk_forward",
    "generate_html_report", "generate_performance_report",
]
