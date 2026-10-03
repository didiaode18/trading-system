# 操盘密码 V10.0 系统架构全景图

> 版本: V10.0 | 最后更新: 2026-09-21 | 基于代码实际实现
> 本文档为代码走读参考，展示系统模块清单、依赖关系和数据流向。

---

## 一、模块全景图（15层）

```
trading_system/
├── config.py                    # 全局配置中心（资金/邮箱/股票池/风控参数）
├── config_local.py              # 用户私有配置（.gitignore排除）
├── main.py                      # 盘后完整分析流程入口
├── scheduler.py                 # 定时任务调度（盘前/盘后/盘中/周末/月度）
│
├── data/                        # [1] 数据层 — 行情获取与存储
│   ├── data_loader.py           #     历史K线（baostock+akshare+本地DB兜底+LRU缓存）
│   ├── realtime.py              #     实时行情（腾讯五档API）
│   ├── real_fund_data.py        #     真实资金数据
│   ├── corporate_action.py      #     除权除息/分红送股处理
│   ├── duckdb_store.py          #     DuckDB列式存储（备选）
│   ├── intraday_feed.py         #     盘中实时数据流
│   ├── router.py                #     数据路由（多源切换）
│   └── sources/                 #     数据源适配器
│       ├── base.py              #         数据源抽象基类
│       ├── baostock_source.py   #         baostock适配器（主数据源）
│       ├── akshare_source.py    #         akshare适配器（备用）
│       └── tushare_source.py    #         tushare适配器（可选）
│
├── strategy/                    # [2] 策略层 — 信号生成与调仓决策（64文件）
│   ├── trend_strategy.py        #     核心趋势策略（5级趋势+自适应通道+MA20/MA60）
│   ├── caopan_signal.py         #     操盘密码引擎（DK信号+生命线+资金监控）
│   ├── stock_screener.py        #     CANSLIM V6.0 选股引擎（六因子+硬性筛选）
│   ├── recommend_engine.py      #     推荐引擎（五层评分模型+regime-aware权重）
│   ├── market_regime.py         #     大盘状态检测（BULL/RANGE/BEAR+防抖+极端应急）
│   ├── regime_playbook.py       #     V10 三态策略手册（仓位限制/通过门槛/止损幅度）
│   ├── action_planner.py        #     V10 调仓建议生成器（减仓/加仓/换仓配对）
│   ├── feedback_loop.py         #     V10 绩效反馈闭环（T+N结算+regime胜率+参数自适应）
│   ├── sector_flow.py           #     板块轮动监控（资金流+景气度+加速度）
│   ├── sector_rotation.py       #     板块轮动（ETF动量排名）
│   ├── sector_rotation_strategy.py  #  行业轮动策略（加速度指标+ETF映射）
│   ├── capital_flow.py          #     资金流向分析（主力/散户/北向）
│   ├── systemic_risk.py         #     系统性风险预警（宽度收窄+恐慌检测）
│   ├── pool_manager.py          #     股票池管理（核心池+观察池+候选池）
│   ├── market_scanner.py        #     全市场扫描（热门股+板块发现）
│   ├── market_sentiment.py      #     市场情绪指标
│   ├── market_pulse.py          #     市场脉搏（多维度健康度）
│   ├── market_gate.py           #     大盘门控（regime-aware买入闸门）
│   ├── market_analysis.py       #     市场分析综合
│   ├── intraday_monitor.py      #     盘中实时监控（策略层）
│   ├── intraday_alert.py        #     盘中预警信号
│   ├── intraday_decision.py     #     盘中交易决策
│   ├── intraday_patterns.py     #     盘中K线形态识别
│   ├── auction_monitor.py       #     集合竞价监控
│   ├── portfolio_analyzer.py    #     仓位优化分析（4规则+时间止损）
│   ├── portfolio_risk.py        #     组合风险管理
│   ├── portfolio_risk_actions.py #    组合风险行动执行
│   ├── position.py              #     仓位计算（Kelly+ATR首仓）
│   ├── anti_manipulation.py     #     反洗盘识别
│   ├── chip_distribution.py     #     筹码分布分析
│   ├── consensus.py             #     多空共识机制
│   ├── signal_decay.py          #     信号衰减模型
│   ├── multi_timeframe.py       #     多周期共振
│   ├── multi_factor.py          #     多因子评分（四维体系）
│   ├── fundamental.py           #     基本面分析
│   ├── financial_report.py      #     财报分析
│   ├── mean_reversion.py        #     均值回归策略
│   ├── trend_forecast.py        #     趋势预测
│   ├── candlestick_pattern.py   #     K线形态识别
│   ├── dividend_enhance.py      #     分红增强策略
│   ├── etf_rotation.py          #     ETF轮动
│   ├── pairs_trading.py         #     配对交易
│   ├── holding_ranking.py       #     持仓排名
│   ├── holder_monitor.py        #     股东监控
│   ├── margin_monitor.py        #     融资融券监控
│   ├── lhb_analyzer.py          #     龙虎榜分析
│   ├── macro_indicator.py       #     宏观指标
│   ├── overnight_linkage.py     #     隔夜联动
│   ├── sector_cycle.py          #     板块周期
│   ├── sector_divergence.py     #     板块背离
│   ├── sector_prediction.py     #     板块预测
│   ├── meta_label.py            #     元标签
│   ├── moat.py                  #     护城河分析
│   ├── recovery_planner.py      #     恢复计划
│   ├── capital_planner.py       #     资金规划
│   ├── dag_scheduler.py         #     DAG调度器
│   ├── event_calendar.py        #     事件日历
│   ├── trade_journal.py         #     交易日志
│   ├── alert_auto_calibrate.py  #     预警自动校准
│   ├── zt_monitor.py            #     涨停监控
│   ├── zt_gene_tracker.py       #     涨停基因追踪
│   ├── news_monitor.py          #     新闻/政策风险扫描
│   └── base.py                  #     策略基类
│
├── risk/                        # [3] 风控层 — 熔断/仓位/强制卖出
│   ├── risk_control.py          #     统一风控引擎（UnifiedRiskEngine 17道关卡+四级熔断+防抖）
│   └── position_sizing.py       #     仓位管理（Kelly/风险预算/策略缩放/零资金保护）
│
├── factors/                     # [4] 因子层 — 多因子计算（20文件）
│   ├── registry.py              #     因子注册表
│   ├── technical.py             #     技术因子（RSI/MACD/布林/KDJ+NaN兜底）
│   ├── momentum.py              #     动量因子（Williams%R+NaN兜底）
│   ├── volume.py                #     量价因子
│   ├── composite.py             #     复合因子
│   ├── preprocessor.py          #     数据预处理（zscore/去极值/标准化）
│   ├── quant_factors.py         #     量化因子统一计算（委托给factors/）
│   ├── ic_monitor.py            #     IC因子监控+自适应权重+cohort结算
│   ├── neutralize.py            #     因子行业/市值中性化
│   ├── dividend.py              #     股息因子
│   ├── growth.py                #     成长因子
│   ├── liquidity.py             #     流动性因子
│   ├── quality.py               #     质量因子
│   ├── reversal.py              #     反转因子
│   ├── smart_money.py           #     聪明钱因子
│   ├── tail_volume.py           #     尾部量能因子
│   ├── value.py                 #     估值因子
│   ├── volatility.py            #     波动率因子
│   ├── volume_distribution.py   #     量能分布因子
│   ├── quantile_scorer.py       #     分位数评分器
│   └── ta_lib_wrapper.py        #     TA-Lib封装
│
├── backtest/                    # [5] 回测层 — 策略验证
│   ├── engine.py                #     事件驱动回测引擎（FIFO分批匹配PnL）
│   ├── walk_forward.py          #     Walk-Forward滚动验证
│   ├── monte_carlo.py           #     蒙特卡洛压力测试
│   ├── metrics.py               #     绩效指标（夏普/回撤/胜率/Calmar）
│   ├── deflated_sharpe.py       #     去膨胀夏普比率
│   ├── broker.py                #     回测券商模拟
│   ├── data_feed.py             #     回测数据供给
│   ├── decile_backtest.py       #     分十组回测
│   ├── param_sweep.py           #     参数扫描
│   ├── parity.py                #     回测一致性校验
│   ├── compare.py               #     策略对比
│   └── report.py                #     回测报告生成
│
├── position/                    # [6] 仓位层 — 仓位管理
│   ├── kelly.py                 #     凯利公式计算
│   ├── vol_target.py            #     波动率目标仓位管理
│   ├── dynamic_sizing.py        #     动态仓位调整
│   ├── rebalance.py             #     再平衡
│   ├── risk_parity.py           #     风险平价
│   └── cost_optimizer.py        #     成本优化器
│
├── quant/                       # [7] 量化引擎层
│   ├── engine.py                #     量化引擎
│   ├── factors.py               #     因子计算（委托给factors/）
│   ├── performance.py           #     绩效分析
│   ├── report_enhancer.py       #     报告增强
│   ├── risk_manager.py          #     量化风险管理
│   ├── run_backtest.py          #     量化回测运行
│   └── universe.py              #     股票池管理
│
├── ml/                          # [8] 机器学习层
│   ├── trainer.py               #     模型训练（LightGBM/XGBoost）
│   ├── predictor.py             #     模型预测
│   ├── features.py              #     特征工程
│   ├── monitor.py               #     模型监控+准确率追踪
│   ├── dataset.py               #     数据集管理
│   └── model_registry.py        #     模型注册表
│
├── execution/                   # [9] 执行层 — 交易执行与归因
│   ├── execution_closure.py     #     执行闭环
│   ├── slippage_tracker.py      #     滑点追踪
│   ├── slippage_model.py        #     滑点模型
│   ├── slippage_section.py      #     滑点归因区块
│   ├── order_attribution.py     #     条件单执行归因
│   ├── order_lifecycle.py       #     订单生命周期
│   ├── algo_trading.py          #     算法交易（TWAP/VWAP）
│   ├── behavior_tracker.py      #     交易行为追踪
│   └── trade_behavior.py        #     交易行为分析
│
├── monitor/                     # [10] 监控层 — 盘中实时预警
│   ├── intraday_monitor.py      #     盘中实时监控（预警规则+信号生成）
│   ├── alert_evaluator.py       #     预警效果评估（5/15/30min回查）
│   ├── pnl_tracker.py           #     盈亏追踪
│   └── prediction_tracker.py    #     预测追踪
│
├── attribution/                 # [11] 归因层 — 绩效归因
│   ├── alpha_beta.py            #     Alpha/Beta归因（CAPM）
│   ├── barra.py                 #     Barra风险模型
│   └── trade_log.py             #     交易日志归因
│
├── notify/                      # [12] 通知层 — 消息推送
│   ├── email_notify.py          #     QQ邮箱SMTP SSL发送
│   ├── wechat_notify.py         #     企业微信Webhook（备用）
│   ├── alert_engine.py          #     预警引擎
│   ├── alert_ledger.py          #     预警效果台账
│   ├── alert_stats_section.py   #     预警闭环统计区块
│   ├── alert_ack.py             #     预警ACK确认（防重复推送）
│   ├── alert_ack_server.py      #     ACK确认服务
│   └── buy_point_alert.py       #     买点提醒（风控闸门+钉钉推送）
│
├── dashboard/                   # [13] 可视化层 — Streamlit仪表盘
│   ├── app.py                   #     Streamlit主应用
│   ├── theme.py                 #     主题样式
│   ├── data_loader.py           #     仪表盘数据加载
│   ├── components/              #     UI组件
│   │   ├── factor_charts.py     #         因子图表
│   │   └── kpi_cards.py         #         KPI卡片
│   └── pages/                   #     页面
│       ├── dashboard.py         #         总览仪表盘
│       ├── portfolio.py         #         持仓分析
│       ├── backtest_analysis.py #         回测分析
│       ├── data_center.py       #         数据中心
│       ├── factor_monitor.py    #         因子监控
│       ├── risk_screen.py       #         风控面板
│       ├── screener_viz.py      #         选股可视化
│       ├── strategy_lifecycle.py #        策略生命周期
│       └── us_sector_overnight.py #       美股隔夜联动
│
├── broker/                      # [14] 券商接口层
│   ├── base.py                  #     券商抽象基类
│   ├── backtest_broker.py       #     回测券商
│   ├── paper_broker.py          #     模拟券商
│   └── factory.py               #     券商工厂
│
└── utils/                       # [15] 工具层
    ├── board_rules.py           #     涨跌停规则
    ├── file_io.py               #     文件IO工具
    ├── instrument.py            #     品种类型判定
    └── trading_calendar.py      #     交易日历
```

---

## 二、模块间依赖关系图

```
config.py ←── 所有模块（全局配置中心）

                    ┌──────────────────────────────────────────────┐
                    │                 入口层                        │
                    │  main.py / scheduler.py / daily_orders.py    │
                    │  caopan_report.py / caopan_runner.py         │
                    └──────┬──────────┬──────────┬────────────────┘
                           │          │          │
              ┌────────────▼──┐  ┌────▼────┐  ┌──▼──────────────┐
              │   data/       │  │strategy/ │  │   notify/       │
              │  数据层       │─→│ 策略层   │─→│   通知层        │
              │  (baostock/   │  │ (信号/   │  │  (邮件/钉钉/   │
              │   腾讯API)    │  │  调仓)   │  │   微信)         │
              └───────┬───────┘  └──┬───┬──┘  └─────────────────┘
                      │             │   │
              ┌───────▼───────┐  ┌──▼───▼──────┐  ┌──────────────┐
              │  factors/     │  │  risk/       │  │  monitor/    │
              │  因子层       │─→│  风控层      │─→│  监控层      │
              │  (20个因子)   │  │  (17道关卡)  │  │  (盘中预警)  │
              └───────────────┘  └──┬──────────┘  └──────────────┘
                                    │
              ┌─────────────────────▼────────────────────────────┐
              │              position/ + execution/               │
              │              仓位层          执行层               │
              │  (Kelly/风险平价/    (滑点/归因/算法交易)        │
              │   波动率目标)                                     │
              └─────────────────────┬────────────────────────────┘
                                    │
              ┌─────────────────────▼────────────────────────────┐
              │  backtest/ + attribution/ + quant/ + ml/         │
              │  回测层        归因层          量化层   ML层      │
              │  (引擎/蒙特卡洛/  (Alpha/Beta)  (组合优化) (预测)│
              │   Walk-Forward)                                   │
              └──────────────────────────────────────────────────┘
                                    │
              ┌─────────────────────▼────────────────────────────┐
              │  dashboard/ + broker/ + utils/                   │
              │  可视化层        券商层        工具层             │
              │  (Streamlit)     (QMT/模拟)   (涨跌停/日历)     │
              └──────────────────────────────────────────────────┘
```

---

## 三、盘后分析完整链路

```
[15:30 触发] scheduler.run_daily_task()
     │
     ├── data/data_loader.batch_update_all()     → 增量更新K线到SQLite
     │
     ├── strategy/trend_strategy.scan_all_stocks() → 趋势信号扫描
     │       └── compute_indicators()             → MA/MACD/RSI/ATR/布林带
     │
     ├── strategy/caopan_signal.analyze()          → DK信号+生命线
     │
     ├── strategy/stock_screener.run_stock_screener() → CANSLIM选股
     │       └── factors/ (20个因子计算)
     │
     ├── strategy/recommend_engine.run_recommendation() → 五层评分
     │       └── regime_playbook.position_limits() → regime-aware仓位约束
     │
     ├── strategy/market_regime.detect()           → BULL/RANGE/BEAR
     │       └── P0-A 防抖确认 + P1-A 极端应急
     │
     ├── risk/risk_control.UnifiedRiskEngine.check() → 17道关卡校验
     │
     ├── strategy/position.calc_first_batch()      → Kelly+ATR仓位
     │
     └── notify/email_notify.send_email()          → 综合日报邮件

[16:15 触发] scheduler.run_holdings_report_task()
     │
     ├── 逐只持仓深度技术诊断
     │
     ├── daily_orders.py → 条件单生成
     │       ├── P0-D: 熔断状态检查 → 过滤买入类条件单
     │       ├── 风控关卡链校验
     │       ├── 仓位硬约束(>MAX_SINGLE自动减仓)
     │       └── P0-C: ActionPlanner调仓建议
     │               ├── regime_playbook → 三态策略
     │               ├── 减仓候选(弱势板块+空头排列+深亏)
     │               ├── 加仓候选(缩量回踩MA20+强势板块+质量门槛)
     │               └── 换仓配对(弱→强)
     │
     └── notify/email_notify.send_email()          → 综合报告邮件
```

---

## 四、盘中预警链路

```
[09:30-15:00] scheduler 启动盘中监控线程
     │
     ├── monitor/intraday_monitor.py
     │       │
     │       ├── 每10s轮询: data/realtime.fetch_realtime_batch()
     │       │
     │       ├── 检测规则:
     │       │   ├── 个股急跌 > 3%       → 紧急预警
     │       │   ├── 大盘急跌 > 1.5%     → 系统性风险
     │       │   ├── 振幅 > 8%           → 异常波动
     │       │   ├── 波动率突变(短期RV>长期RV+2σ) → 波动率预警
     │       │   └── 多因子联合(≥3个warning) → 自动升级critical
     │       │
     │       ├── 风控联动:
     │       │   ├── 大盘急跌 → 设置 systemic_risk 标记
     │       │   │              └── risk_control: 总仓位上限降至30%
     │       │   ├── 盘中暂停 → 设置 intraday_pause 标记
     │       │   │              └── risk_control: 拒绝所有新买入
     │       │   └── P0-D: 熔断状态 → 禁止买入类条件单
     │       │
     │       └── 通知推送:
     │           ├── notify/email_notify → 邮件
     │           ├── notify/buy_point_alert → 钉钉（P0/P1级）
     │           └── notify/alert_ack → ACK确认（防重复推送）
     │
     └── [15:01] 停止监控，清除盘中风险标记
```

---

## 五、V10 智能调仓数据流

```
scheduler.run_holdings_report_task()
     │
     ├── market_regime.detect()
     │       └── regime_result = {state, confidence, scores, ...}
     │
     ├── RegimePlaybook(regime_result)
     │       └── playbook = {position_limits, risk_rules, pass_threshold, ...}
     │               BULL: max=90%, min=70%, stop=8%, threshold=50
     │               RANGE: max=60%, min=30%, stop=6%, threshold=55
     │               BEAR: max=30%, min=0%, stop=5%, threshold=65
     │
     ├── ActionPlanner(regime_result, sector_result, holdings, all_data)
     │       │
     │       ├── generate_action_plan()
     │       │       ├── _identify_reduce_candidates()  → 减仓清单
     │       │       │       (板块弱势+空头排列+深亏无止跌)
     │       │       │
     │       │       ├── _identify_add_candidates()     → 加仓清单
     │       │       │       (强势板块+缩量回踩MA20+动量>10+MA20斜率>0)
     │       │       │       + P2-B: 过滤涨跌停/停牌
     │       │       │       + P0-B: 板块→个股映射桥接
     │       │       │
     │       │       ├── _identify_switch_pairs()       → 换仓配对
     │       │       │       (弱持仓→强候选)
     │       │       │
     │       │       └── _calc_position_target()        → 目标仓位
     │       │               P1-F: min + (max-min)*confidence
     │       │
     │       └── return {reduce_list, add_list, switch_list, position_target}
     │
     ├── daily_orders.py 条件单生成
     │       ├── P0-D: 检查 UnifiedRiskEngine 熔断状态
     │       ├── 止损/止盈/时间条件单
     │       ├── 仓位硬约束减仓单
     │       └── ActionPlanner 调仓建议输出
     │
     └── FeedbackLoop.settle_recommendations()
             ├── T+N 收益结算
             ├── regime/板块分组胜率统计
             └── auto_adjust_parameters() → 参数微调建议
```

---

## 六、外部数据源与存储

```
[外部数据源]
  baostock ──────→ data/data_loader.py ──→ SQLite(stock_db.db)
  akshare ───────→ data/sources/ ────────→ (同上，备用)
  腾讯API ───────→ data/realtime.py ─────→ 实时行情(内存)
  东方财富 ──────→ data/sources/ ────────→ 资金流向/板块数据

[本地存储]
  stock_db.db ─── 历史K线缓存(增量更新)
  trade_journal.db ─── 交易日志
  paper_account.db ─── 模拟账户
  holdings.json ─── 当前持仓
  stock_pool.json ─── 股票池

[状态文件]
  data/regime_state.json ─── Regime状态(防抖用)
  data/alert_cooldown.json ─── 预警冷却状态
  data/ic_history.json ─── IC因子历史
  data/ic_pending.json ─── IC待结算
  data/buy_alert_levels.json ─── 买点预警级别
  data/market_sentiment_cache.json ─── 市场情绪缓存
  data/strategy_lifecycle.json ─── 策略生命周期
```

---

*最后更新: 2026-09-21 (V10.0: 智能调仓架构+风控增强+15模块全景)*
