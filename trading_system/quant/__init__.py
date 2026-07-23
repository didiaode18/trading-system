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

P2: 多策略组合
  - strategies: 动量/均值回归/事件驱动策略组
  - portfolio: 多策略动态资金分配
  - monitor: 策略有效性监控(IC/IR/衰减/漂移)

P3: 实盘执行
  - execution: TWAP拆单 + OMS订单管理
  - live_monitor: 实盘监控 + 每日复盘

P4: 极致优化
  - advanced: Walk Forward寻优/熔断/另类数据/A股特色
"""
