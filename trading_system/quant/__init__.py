# NOTE: 实验性回测分支，未接入生产调度
"""
量化回测模块 (P0-P4)
====================
P0: 最小可行量化系统(MVP)
  - universe: 全A股数据中台 + 风险过滤
  - factors: 多因子选股引擎
  - engine: 回测引擎(T+1/涨跌停/费用)
  - performance: 绩效分析 + 净值曲线
  - run_backtest: 一键回测入口

P1: 风控加固
  - risk_manager: 止损/ATR/移动止盈/大盘择时/动态滑点

# FIX: 清理 strategies/portfolio/monitor/live_monitor/advanced/execution
# 6个死代码模块（已整文件删除，grep 验证零调用），原 P2-P4 模块清单随之移除
"""
