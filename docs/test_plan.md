# 量化交易系统 深度测试方案 v1.0

> 适用系统: CANSLIM选股引擎V6.0 + 趋势策略 + 真实环境回测  
> 编写日期: 2026-08-21  
> 代码仓库: `d:\workspace\trading-system`

---

## 1. 测试范围与目标

### 1.1 核心模块覆盖矩阵

| # | 模块 | 代码位置 | 测试目标 | 验收标准 |
|---|------|----------|----------|----------|
| M1 | CANSLIM选股引擎V6.0 | `strategy/stock_screener.py` (4219行) | 七因子评分正确性、IC/IR动态加权、Regime检测 | 因子IC>0.03, 评分排序与前瞻收益单调性>80% |
| M2 | 趋势策略引擎 | `strategy/trend_strategy.py` + 操盘密码 | MA20/MA60信号生成、买卖点逻辑 | 趋势识别准确率>55%, 假信号率<40% |
| M3 | 事件驱动回测引擎 | `backtest/engine.py` (647行) | 订单撮合、T+1约束、涨跌停处理 | 与逐笔回测偏差<2% |
| M4 | 真实环境回测 | `backtest_real.py` (1364行) | 滑点/手续费/双轨止盈/ATR止损 | 模拟真实交易成本下策略仍正期望 |
| M5 | Walk-Forward验证 | `backtest/walk_forward.py` (404行) | 滚动窗口无过拟合、OOS Sharpe>0.5 | 训练集/测试集Sharpe比>0.7 |
| M6 | 风控模块 | `risk/risk_control.py` (2512行) | 三级仓位限制、熔断机制、止损线 | 100%拦截违规下单, 熔断触发延迟<1天 |
| M7 | 仓位管理 | `position/kelly.py` + `dynamic_sizing.py` | Kelly公式、15%上限、波动率调整 | 单股≤15%, 总仓位符合市场状态约束 |
| M8 | 盘中预警 | `monitor/intraday_monitor.py` (330行) | 三源合一、反冲动锁、系统风险检测 | 预警延迟<30s, 误报率<20% |
| M9 | 买点到价提醒 | `notify/buy_point_alert.py` (500行) | 大盘闸门、暴跌作废、冷却期 | 闸门拦截率100%, 禁买期无信号泄漏 |

### 1.2 测试类型覆盖

| 层级 | 名称 | 目标 | 脚本位置 |
|------|------|------|----------|
| L1 | 单元测试 | 各模块核心函数逻辑正确性 | `tests/test_deep_quant.py` |
| L2 | 集成测试 | 模块间数据流转与接口契约 | `tests/test_integration.py` (扩展现有) |
| L3 | 策略回测 | 单策略历史表现统计 | `scripts/run_strategy_backtest.py` |
| L4 | 系统级回测 | 端到端含摩擦成本模拟 | 复用 `backtest_real.py` + `engine.py` |
| L5 | 压力测试 | 极端行情下系统鲁棒性 | `scripts/run_stress_test.py` |

---

## 2. 数据准备方案

### 2.1 数据源

| 数据 | 来源 | 说明 |
|------|------|------|
| 历史K线 | `trading_system/data/stock_db.db` → `daily_kline` 表 | 22万条日线, 166只股票, 2020-01-02 ~ 2026-08-19 |
| 基准指数 | 同上表中 `code='000300'` (沪深300) | 用于市场环境判断、Alpha/Beta计算 |
| 基本面数据 | 无独立表，CANSLIM中C/A/I因子用代理指标 | 回测脚本中用动量/波动率/换手率代理 |

### 2.2 数据区间划分（Walk-Forward方式）

```
┌─────────────────────────────────────────────────────────┐
│ 2020-01 ─── 2022-01 ─── 2023-01 ─── 2024-01 ─── 2025-01 ─── 2026-08 │
│ ◄── 冷启动 ──►│                                            │
│                ├─ W1训练 ─┤─ W1测试 ─┤                    │
│                            ├─ W2训练 ─┤─ W2测试 ─┤        │
│                                        ├─ W3训练 ─┤─W3测─┤│
│                                                              │
│ 2020Q1~Q4: 熊市→牛市(冷启动期, 不参与评估)                   │
│ 2022: 全年弱势/熊市 ← 压力测试重点区间                       │
│ 2023: 震荡市 ← 策略适应性测试                                │
│ 2024Q4: 牛市 ← 进攻能力测试                                  │
│ 2025: 结构性行情 ← 样本外验证                                  │
└─────────────────────────────────────────────────────────┘
```

**Walk-Forward窗口配置**（复用 `walk_forward.py` 默认参数）：
- 训练窗口: 200个交易日（~10个月）
- 测试窗口: 60个交易日（~3个月）
- 滚动步长: 60个交易日
- 预计生成 8~10 个窗口

### 2.3 市场环境标注

| 区间 | 市场状态 | 特征 | 测试重点 |
|------|----------|------|----------|
| 2022-01 ~ 2022-12 | 熊市/弱势 | 持续下跌, breadth<30 | M因子防护、深跌自适应 |
| 2023-01 ~ 2023-12 | 震荡市 | 区间波动, 板块轮动 | 选股精度、假信号控制 |
| 2024-01 ~ 2024-12 | 先弱后强 | Q4政策驱动反弹 | 趋势捕捉、仓位跟进 |
| 2025-01 ~ 2026-08 | 结构性行情 | 分化加剧 | 因子IC稳定性 |

---

## 3. 分层测试设计

### 3.1 L1 单元测试

**脚本**: `tests/test_deep_quant.py`  
**运行**: `pytest tests/test_deep_quant.py -v`  
**预估耗时**: <30秒（无网络依赖）

#### 3.1.1 CANSLIM选股引擎 (M1)

| 用例ID | 测试项 | 输入 | 预期输出 | 验证点 |
|--------|--------|------|----------|--------|
| UT-101 | N因子-新高评分 | 60日新高close | N≥12 | 接近60日高点得满分 |
| UT-102 | N因子-非新高评分 | 60日低点close | N=0 | 远离高点不得分 |
| UT-103 | S因子-缩量企稳 | vol<0.7*vol_ma, close≈ma20 | S≥6 | 缩量止跌识别 |
| UT-104 | L因子-多周期动量 | 20日涨>10%, 60日涨>20% | L≥17 | 多周期共振 |
| UT-105 | CAI多代理-高动量低波动 | mom>15%, vol_std<2 | CAI≥14 | 多代理合成正确性 |
| UT-106 | CAI多代理-低动量高波动 | mom<-5%, vol_std>5 | CAI≤6 | 劣质标的低分 |
| UT-107 | V估值因子-低估值 | 60日跌幅>10% | V=4 | 深度回调高分 |
| UT-108 | V估值因子-高估值 | 60日涨幅>30% | V=0 | 过热标的零分 |
| UT-109 | 总评分范围 | 极端好/坏场景 | 15≤score≤80 | 评分不越界 |
| UT-110 | IC/IR加权权重 | 已知IC序列 | 权重和=1, 高IR因子权重大 | 归一化+单调性 |

#### 3.1.2 风控模块 (M6)

| 用例ID | 测试项 | 输入 | 预期输出 | 验证点 |
|--------|--------|------|----------|--------|
| UT-201 | 三级仓位-ETF上限 | ETF买入25% | 拦截, 上限20% | `pre_trade_check_orders` |
| UT-202 | 三级仓位-个股上限 | 个股买入18% | 拦截, 上限15% | 单股硬限制 |
| UT-203 | 总仓位-弱势市 | 指数<MA20, 总仓90% | 拦截, 上限50% | 动态仓位限制 |
| UT-204 | 连续亏损熔断 | 连亏3笔 | 暂停5天 | `RiskStateManager` |
| UT-205 | 日亏损熔断 | 单日亏3.5% | 当日禁止开仓 | 熔断阈值 |
| UT-206 | 冷却期检查 | 卖出后第2天再买同股 | 拦截 | 3天冷却期 |
| UT-207 | 止损线Ratchet | 浮盈20%后回撤至-5% | 触发止损 | 只升不降原则 |
| UT-208 | 浮亏加仓禁止 | 持仓浮亏>0 | 拒绝加仓 | 绝对禁止规则 |

#### 3.1.3 仓位管理 (M7)

| 用例ID | 测试项 | 输入 | 预期输出 | 验证点 |
|--------|--------|------|----------|--------|
| UT-301 | Kelly基本公式 | win_rate=0.5, pf=2.0 | f≈0.25 | 公式正确性 |
| UT-302 | 半Kelly | 同上 | f≈0.125 | 半Kelly=Kelly×0.5 |
| UT-303 | Kelly上限截断 | win_rate=0.8, pf=3.0 | f≤0.15 | 15%硬上限 |
| UT-304 | 零胜率Kelly | win_rate=0 | f=0 | 不开仓 |
| UT-305 | 波动率调整仓位 | 高波动(>3%) | 仓位缩减 | 波动率倒数加权 |
| UT-306 | ATR仓位计算 | ATR=2, price=50 | 合理股数 | 风险预算法 |

#### 3.1.4 回测引擎 (M3/M4)

| 用例ID | 测试项 | 输入 | 预期输出 | 验证点 |
|--------|--------|------|----------|--------|
| UT-401 | T+1约束 | 当日买入 | 次日才可卖 | 不可当日平仓 |
| UT-402 | 涨停不买入 | 涨停价封板 | 不成交 | 涨跌停处理 |
| UT-403 | 跌停不卖出 | 跌停价封板 | 挂单不成交 | 跌停流动性 |
| UT-404 | 手续费扣除 | 买入10万 | 扣除佣金+印花税 | 成本正确性 |
| UT-405 | 滑点模拟 | 收盘价±0.5% | 成交价偏移 | 滑点模型 |

#### 3.1.5 因子计算 (factors/)

| 用例ID | 测试项 | 输入 | 预期输出 | 验证点 |
|--------|--------|------|----------|--------|
| UT-501 | IC计算 | 已知因子值+收益 | IC∈[-1,1] | Spearman秩相关 |
| UT-502 | IC衰减检测 | 连续5天IC下降 | is_decaying=True | 阈值判定 |
| UT-503 | 因子正交化 | 高相关因子对 | VIF降低 | 多重共线性消除 |
| UT-504 | 分层回测 | 5层分组 | 单调性检验 | 顶层收益>底层 |

---

### 3.2 L2 集成测试

**运行**: `pytest tests/test_integration.py -v -k "integration"`  
**预估耗时**: 2~5分钟（需加载历史数据）

| 用例ID | 测试链路 | 验证点 |
|--------|----------|--------|
| IT-01 | 选股→评分→排序 | `run_stock_screener`输出按score降序, 无NaN因子 |
| IT-02 | 选股→风控→仓位 | 选股信号经`pre_trade_check_orders`后仓位合规 |
| IT-03 | 选股→回测→指标 | CANSLIM信号输入`backtest_real`, 输出含Sharpe/Calmar/MDD |
| IT-04 | 回测→Monte Carlo | 回测交易记录输入MC, 输出含VaR/CVaR/破产概率 |
| IT-05 | 回测→Walk-Forward | 数据输入WF, 窗口间无数据泄漏(训练/测试日期不重叠) |
| IT-06 | 风控→预警→通知 | 触发熔断后`AlertEngine`发出风险预警 |
| IT-07 | 买点提醒→大盘闸门 | `_market_gate()`返回禁止时, 无买入信号输出 |
| IT-08 | IC监控→因子加权 | IC衰减因子权重自动降低 |

---

### 3.3 L3 策略回测

**脚本**: `scripts/run_strategy_backtest.py`  
**运行**: `python scripts/run_strategy_backtest.py`  
**预估耗时**: 3~10分钟

#### 3.3.1 回测策略清单

| 策略ID | 策略名称 | 实现基础 | 核心参数 |
|--------|----------|----------|----------|
| S1 | CANSLIM选股V6.0 | `v52_v60_backtest_compare.py` → `v60_hard_filter` + `v60_score` | 买入线28, 弱势门槛30 |
| S2 | CANSLIM选股V5.2(基线) | 同上 → `v52_hard_filter` + `v52_score` | 买入线35, 弱势门槛40 |
| S3 | MA20趋势跟踪 | `backtest/engine.py` → `default_strategy` | MA20金叉买/死叉卖 |
| S4 | MA20+MA60双趋势 | `backtest_real.py` → `backtest_stock_v5` | 含市场环境自适应 |
| S5 | 组合策略(选股+趋势) | `run_canslim_backtest.py` → `run_portfolio_backtest` | 评分Top5+趋势确认 |

#### 3.3.2 回测指标体系

每个策略输出以下指标（复用 `backtest/metrics.py`）：

```
核心收益指标:
  - 年化收益率 (calc_annual_return)
  - 累计收益率
  - 月度收益分布 (calc_monthly_returns)

风险指标:
  - 最大回撤 (calc_max_drawdown) — 含峰/谷日期
  - 年化波动率
  - VaR(95%) / CVaR(95%) (calc_cvar)
  - 下行波动率

风险调整收益:
  - 夏普比率 (calc_sharpe_ratio, Rf=3%)
  - Sortino比率 (calc_sortino_ratio)
  - Calmar比率 (calc_calmar_ratio)
  - Alpha / Beta (calc_alpha_beta, 基准=沪深300)

交易质量:
  - 胜率 (calc_win_rate)
  - 盈亏比 (calc_profit_factor)
  - 每笔期望收益 (calc_expectancy)
  - MFE/MAE (calc_mfe_mae)
  - 平均持仓天数
  - 换手率 (calc_turnover)
```

#### 3.3.3 V5.2 vs V6.0 对比框架

复用 `scripts/v52_v60_backtest_compare.py` 已有结果，补充以下维度：

| 对比维度 | V5.2指标 | V6.0指标 | 判定规则 |
|----------|----------|----------|----------|
| 弱势市场覆盖率 | 弱势信号/总候选 | 同左 | V6.0>V5.2 |
| 信号质量(20d收益) | 平均+1.40% | 平均+1.23% | 差异<0.5pp可接受 |
| 低质量信号占比 | 0% | 5.1% | <10%可接受 |
| CAI因子IC | 0.0000 | +0.0529 | V6.0>V5.2 |
| 最大回撤 | -7.01% | -6.68% | V6.0≤V5.2 |

---

### 3.4 L4 系统级回测

**运行**: 复用 `backtest_real.py` + `backtest/engine.py`  
**预估耗时**: 5~15分钟

#### 3.4.1 端到端约束验证

| 约束项 | 实现位置 | 验证方法 |
|--------|----------|----------|
| T+1交割 | `engine.py` L204 `_process_day` | 检查所有交易买入→卖出间隔≥1天 |
| 涨跌停限制 | `engine.py` SimBroker | 涨停不买入、跌停不卖出 |
| 交易成本 | `engine.py` cost_config | 佣金万2.5 + 印花税千1 + 滑点0.1% |
| 最小交易单位 | `engine.py` | 买入≥100股(1手) |
| 回测结束平仓 | `engine.py` L285 `_force_close_all` | 最后交易日强制清仓 |

#### 3.4.2 全链路模拟流程

```
数据加载 → 指标预计算 → 市场环境判断 → 选股信号 → 风控校验 → 仓位计算 → 下单执行 → 持仓管理 → 止损/止盈 → 绩效统计
   ↑                                                                                    ↓
   └────────────── Walk-Forward滚动(避免前视偏差) ←──────────────────────────────────────┘
```

---

### 3.5 L5 压力测试

**脚本**: `scripts/run_stress_test.py`  
**运行**: `python scripts/run_stress_test.py`  
**预估耗时**: 2~5分钟

#### 3.5.1 历史极端行情场景

复用 `backtest/monte_carlo.py` → `HISTORICAL_SCENARIOS`：

| 场景 | 触发条件 | 预期最大损失 | 恢复时间 |
|------|----------|-------------|----------|
| 2015股灾 | 指数连续跌停, -45%/50天 | ≤30% | >180天 |
| 2018贸易战 | 全年单边下跌, -32%/230天 | ≤25% | >230天 |
| 2020疫情 | 急跌急涨, -16%/42天 | ≤12% | <90天 |
| 2022暴跌 | 三重打击, -28%/80天 | ≤20% | >120天 |
| 2024微盘股崩 | 流动性危机, -20%/28天 | ≤15% | <60天 |

#### 3.5.2 组合压力指标

```
- 组合VaR(95%): 单日最大损失不超过总资金5%
- 组合CVaR(95%): 尾部平均损失不超过8%
- 连续止损次数: 熔断触发后5天内恢复
- 破产概率: Monte Carlo 1000次模拟中破产率<1%
- 最大连亏笔数: 历史交易中连续亏损≤5笔
```

#### 3.5.3 数据缺失降级测试

| 场景 | 模拟方法 | 预期行为 |
|------|----------|----------|
| 停牌股(无数据) | 删除某股连续10日数据 | 跳过该股, 不报错 |
| 指标计算不足 | 新股仅30日数据 | 标记"均线不足", 不生成信号 |
| 数据库连接失败 | 模拟DB不存在 | 输出明确错误, 不崩溃 |
| 基准指数缺失 | 删除000300数据 | 降级为无市场环境判断 |

---

## 4. 关键验证指标汇总

### 4.1 选股能力指标

| 指标 | 计算方式 | 合格线 | 优秀线 |
|------|----------|--------|--------|
| 因子IC | Spearman(因子值, 20d前瞻收益) | >0.03 | >0.05 |
| 因子IR | mean(IC) / std(IC) | >0.3 | >0.5 |
| 信号频率 | 信号数/扫描次数 | 10~30% | 15~25% |
| 评分单调性 | 5层分组收益递减 | 3/4层满足 | Top>Bottom显著 |

### 4.2 信号质量指标

| 指标 | 合格线 | 优秀线 |
|------|--------|--------|
| 5日胜率 | >45% | >50% |
| 10日胜率 | >45% | >50% |
| 20日平均收益 | >+0.5% | >+1.5% |
| 评分Top组vs Bottom组收益差 | >+1.0% | >+2.0% |

### 4.3 风险控制指标

| 指标 | 合格线 | 红线 |
|------|--------|------|
| 最大回撤 | <-25% | >-35% |
| VaR(95%) | <-3% | >-5% |
| 极端亏损笔数(>10%) | ≤3笔 | >5笔 |
| 止损触发率 | 20~40% | >60%(过度止损) |
| 盈亏比 | >2.0 | >3.0 |

### 4.4 执行质量指标

| 指标 | 合格线 | 说明 |
|------|--------|------|
| 滑点偏差 | <0.3% | 实际成交价 vs 信号价 |
| 成交率 | >90% | 信号到成交的转化率 |
| 冲击成本 | <0.1% | 小资金可忽略 |

---

## 5. 对比基线

### 5.1 V5.2 vs V6.0 双版本对比

| 维度 | V5.2基线(commit `6dd1859`) | V6.0改进(commit `f39383d`) |
|------|---------------------------|---------------------------|
| M因子 | down=禁止买入 | down=允许15%轻仓 |
| 深跌防护 | 固定12% | 弱势18%/强势12% |
| 买入线 | 固定35分 | 弱势28分+breadth动态(最低20) |
| CAI因子 | 固定10分 | 多代理(动量40%+波动30%+换手30%) |
| V因子 | 无 | PE/PB百分位反向(0-5分) |
| IC/IR加权 | 无 | 因子稳定性动态加权 |
| 因子正交化 | 无 | 高相关因子自动降权 |

**对比执行**: 已有 `scripts/v52_v60_backtest_compare.py` 完成首轮对比，本次测试在此基础上扩展：
- 增加分市场环境分位数分析
- 增加因子IC时序稳定性检验
- 增加Walk-Forward框架下的样本外验证

### 5.2 策略横向对比

| 对比项 | CANSLIM选股 | MA20趋势跟踪 | 双均线趋势 |
|--------|------------|-------------|-----------|
| 信号频率 | 中(每5日扫描) | 高(每日) | 高(每日) |
| 持仓周期 | 5~20日 | 10~60日 | 20~120日 |
| 适用市场 | 全市场 | 趋势市 | 强趋势市 |
| 预期Sharpe | >0.8 | >0.5 | >0.4 |
| 预期MDD | <-20% | <-25% | <-30% |

---

## 6. 输出交付物

### 6.1 文档交付

| 交付物 | 路径 | 格式 |
|--------|------|------|
| 测试方案文档 | `docs/test_plan.md` | Markdown(本文件) |
| L1单元测试脚本 | `tests/test_deep_quant.py` | Python/pytest |
| L3策略回测脚本 | `scripts/run_strategy_backtest.py` | Python |
| L5压力测试脚本 | `scripts/run_stress_test.py` | Python |
| 结果报告生成器 | `scripts/generate_test_report.py` | Python |

### 6.2 报告模板

**控制台摘要**（每次测试运行自动输出）:
```
================================================================
  量化系统深度测试报告  2026-08-21
================================================================
  L1 单元测试:  XX/XX 通过 (XX.X%)    [PASS/FAIL]
  L2 集成测试:  XX/XX 通过 (XX.X%)    [PASS/FAIL]
  L3 策略回测:  X个策略完成            [PASS/FAIL]
  L4 系统回测:  X个场景完成            [PASS/FAIL]
  L5 压力测试:  X个场景完成            [PASS/FAIL]
================================================================
  总耗时: XX分XX秒
  详细报告: output/test_report_YYYYMMDD.html
================================================================
```

**HTML报告**: 复用 `backtest/report.py` 的Chart.js模板，增加：
- 多策略对比雷达图
- 因子IC时序折线图
- Walk-Forward窗口热力图
- 压力测试场景瀑布图

---

## 7. 执行计划

### 7.1 优先级与依赖关系

```
优先级 P0 (立即执行, 无依赖):
  ├── L1 单元测试 → tests/test_deep_quant.py
  │     依赖: 无
  │     耗时: <30s
  │
  └── L3 策略回测 → scripts/run_strategy_backtest.py
        依赖: 历史数据(stock_db.db)
        耗时: 3~10min

优先级 P1 (L1通过后执行):
  ├── L2 集成测试 → tests/test_integration.py
  │     依赖: L1通过
  │     耗时: 2~5min
  │
  └── L5 压力测试 → scripts/run_stress_test.py
        依赖: L3回测结果(交易记录)
        耗时: 2~5min

优先级 P2 (L3完成后执行):
  ├── L4 系统级回测 → 复用 backtest_real.py + engine.py
  │     依赖: L3策略参数确认
  │     耗时: 5~15min
  │
  └── 报告生成 → scripts/generate_test_report.py
        依赖: 所有测试完成
        耗时: <1min
```

### 7.2 执行时间表

| 阶段 | 任务 | 预估耗时 | 前置条件 |
|------|------|----------|----------|
| Day 1 | L1单元测试编写+运行 | 1h | 无 |
| Day 1 | L3策略回测脚本编写+运行 | 1.5h | 数据库可用 |
| Day 2 | L2集成测试编写+运行 | 1h | L1通过 |
| Day 2 | L5压力测试脚本编写+运行 | 1h | L3完成 |
| Day 3 | L4系统级回测运行 | 2h | L3参数确认 |
| Day 3 | 报告生成+结果分析 | 1h | 全部完成 |
| **合计** | | **~7.5h** | |

### 7.3 快速启动命令

```powershell
# 1. L1单元测试
cd d:\workspace\trading-system
pytest tests/test_deep_quant.py -v --tb=short

# 2. L3策略回测
python scripts/run_strategy_backtest.py

# 3. L5压力测试
python scripts/run_stress_test.py

# 4. 生成综合报告
python scripts/generate_test_report.py

# 5. 全量测试一键运行
pytest tests/test_deep_quant.py -v; python scripts/run_strategy_backtest.py; python scripts/run_stress_test.py; python scripts/generate_test_report.py
```

---

## 附录A: 现有代码复用映射

| 测试需求 | 复用文件 | 复用方式 |
|----------|----------|----------|
| CANSLIM双版本对比 | `scripts/v52_v60_backtest_compare.py` | 直接调用 `run_backtest()` + `analyze_signals()` |
| CANSLIM因子回测 | `trading_system/run_canslim_backtest.py` | 调用 `run_canslim_backtest()` / `run_portfolio_backtest()` |
| 真实环境回测 | `trading_system/backtest_real.py` | 调用 `backtest_stock_v5()` |
| 事件驱动回测 | `trading_system/backtest/engine.py` | 调用 `BacktestEngineV2.run()` |
| Walk-Forward | `trading_system/backtest/walk_forward.py` | 调用 `WalkForwardAnalyzer.run()` |
| Monte Carlo | `trading_system/backtest/monte_carlo.py` | 调用 `MonteCarloStressTest.run()` |
| 历史压力测试 | 同上 → `historical_stress_test()` | 直接调用 |
| 绩效指标 | `trading_system/backtest/metrics.py` | 调用各 `calc_*` 函数 |
| HTML报告 | `trading_system/backtest/report.py` | 调用 `generate_html_report()` |
| 风控校验 | `trading_system/risk/risk_control.py` | 调用 `pre_trade_check_orders()` / `quick_risk_check()` |
| Kelly仓位 | `trading_system/position/kelly.py` | 调用 `kelly_position()` / `half_kelly_position()` |
| IC监控 | `trading_system/factors/ic_monitor.py` | 调用 `ICMonitor.calc_ic()` |

## 附录B: 测试用例编号规则

```
UT-XXX: L1单元测试 (Unit Test)
IT-XXX: L2集成测试 (Integration Test)
S-X:    L3策略 (Strategy)
ST-XXX: L5压力测试 (Stress Test)
```
