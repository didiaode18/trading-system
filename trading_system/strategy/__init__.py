# -*- coding: utf-8 -*-
"""
策略层 (strategy/)
================
核心交易策略与选股引擎

模块清单:
    trend_strategy.py      核心趋势策略（双买点+双轨止盈+移动止损V3）
    trend_forecast.py      持仓趋势预测（6维度评分+时间窗口）
    recommend_engine.py    五层量化推荐引擎（排雷→赛道→基本面→趋势→买点）
    stock_screener.py      CANSLIM全赛道选股引擎
    multi_timeframe.py     多周期共振分析
    sector_rotation.py     板块轮动检测
    pool_manager.py        股票池管理（核心池/观察池）
    position.py            仓位计算（ATR动态仓位）
    portfolio_analyzer.py  仓位分析（资金优化方案）
    portfolio_risk.py      组合风险管理（相关性/HHI/VaR）
    capital_flow.py        资金流向分析（北向/主力）
    fundamental.py         基本面数据（PE/ROE/增速）
    market_regime.py       大盘状态识别（HMM三状态）
    mean_reversion.py      均值回归策略（震荡市）
    anti_manipulation.py   反洗盘保护（主力行为识别）
    consensus.py           多空共识信号
    signal_decay.py        信号衰减模型
    meta_label.py          元标签（信号质量评估）
    chip_distribution.py   筹码分布分析
    event_calendar.py      事件日历（解禁/减持/财报）
    news_monitor.py        新闻风控监控
    intraday_monitor.py    盘中实时监控与预警
    market_scanner.py      全市场动态扫描
    trade_journal.py       交易日志与绩效归因
"""
