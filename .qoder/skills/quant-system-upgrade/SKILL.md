---
name: quant-system-upgrade
description: 量化交易系统升级路线图与实施指南。当用户要求优化交易系统、新增策略模块、提升胜率、增加回测能力、或进行系统架构升级时使用。覆盖回测引擎、因子库、机器学习、仓位优化、绩效归因、模拟盘六大核心能力建设。
---

# 量化交易系统升级 Skill

## 系统现状（V7.1）

当前已具备：趋势策略(双买点)+移动止损+双轨止盈+反洗盘保护+多空共识+主力行为评分+CANSLIM选股+组合风控+盘中监控+邮件推送

## 升级路线图（按优先级排序）

### Phase 1: 回测引擎（最高优先级）

**目标**: 策略改动前必须经过历史验证，杜绝"拍脑袋"改参数

**实施路径**:
```
trading_system/backtest/
├── __init__.py
├── engine.py          # 回测引擎核心（事件驱动）
├── data_feed.py       # 历史数据馈送（复用data_loader）
├── broker.py          # 模拟撮合（含滑点+手续费）
├── metrics.py         # 绩效指标计算
├── walk_forward.py    # 滚动窗口防过拟合
└── report.py          # 回测报告HTML生成
```

**核心要求**:
1. 事件驱动架构：逐日遍历，模拟真实交易决策流程
2. 滑点模型：买入+0.1%，卖出-0.1%（A股T+1限制）
3. 手续费：万2.5佣金 + 千1印花税（卖出）
4. 必须输出：年化收益率、最大回撤、夏普比率、Calmar比率、胜率、盈亏比
5. Walk-Forward：训练集60天+验证集20天滚动，防止过拟合
6. 对比基线：沪深300买入持有

**关键代码模式**:
```python
class BacktestEngine:
    def run(self, strategy_fn, start_date, end_date, initial_capital=100000):
        """
        strategy_fn(date, data_dict, holdings) -> signals
        逐日调用策略函数，模拟执行
        """
        for date in trading_dates:
            # 1. 更新行情
            # 2. 检查止损/止盈（用当日收盘价）
            # 3. 调用策略生成信号
            # 4. 模拟成交（次日开盘价+滑点）
            # 5. 记录净值
```

**验收标准**: 对现有trend_strategy回测2年数据，输出完整绩效报告

---

### Phase 2: 多因子库 + IC监控

**目标**: 从5个指标扩展到50+因子，并监控因子有效性衰减

**实施路径**:
```
trading_system/factors/
├── __init__.py
├── registry.py        # 因子注册表
├── technical.py       # 技术因子（MA/MACD/RSI/布林/ATR/KDJ...）
├── volume.py          # 量价因子（OBV/VWAP/量比/换手率...）
├── momentum.py        # 动量因子（N日收益率/相对强度/加速度...）
├── quality.py         # 质量因子（ROE/毛利率/现金流...）
├── sentiment.py       # 情绪因子（涨跌停比/融资余额/北向...）
├── ic_monitor.py      # IC/IR计算 + 衰减预警
└── composite.py       # 因子正交化 + 加权合成
```

**核心要求**:
1. 每个因子必须计算IC（信息系数）和IR（信息比率）
2. IC衰减监控：连续5天IC<0.02的因子自动降权
3. 因子正交化：去除共线性（VIF>10的因子剔除）
4. 分层回测：按因子值分5组，验证单调性

---

### Phase 3: 机器学习信号增强

**目标**: 用ML模型对传统信号做二次确认，降低假信号率

**实施路径**:
```
trading_system/ml/
├── __init__.py
├── features.py        # 特征工程（从因子库提取）
├── models/
│   ├── xgboost_signal.py   # XGBoost买卖信号分类
│   ├── lstm_predict.py     # LSTM价格趋势预测
│   └── ensemble.py         # 模型融合
├── trainer.py         # 训练管线（含交叉验证）
├── predictor.py       # 推理接口
└── monitor.py         # 模型漂移监控
```

**核心要求**:
1. 标签定义：未来5日收益>3%=正样本，<-3%=负样本
2. 特征：从因子库取Top20因子（按IC排序）
3. 训练窗口：滚动120天训练，预测未来5天
4. 集成规则：传统信号 + ML概率>0.65 才出最终信号
5. 漂移监控：预测准确率连续10天<55%触发重训练

**约束**: ML仅做"确认/否决"，不独立产生交易信号

---

### Phase 4: 动态仓位管理

**目标**: 从固定比例升级为Kelly公式+风险平价

**实施路径**:
```
trading_system/position/
├── kelly.py           # Kelly公式仓位计算
├── risk_parity.py     # 风险平价配置
├── dynamic_sizing.py  # 动态仓位（波动率倒数加权）
└── rebalance.py       # 自动再平衡触发器
```

**核心公式**:
```
Kelly仓位 = (胜率 * 盈亏比 - 败率) / 盈亏比
实际仓位 = Kelly * 0.5（半Kelly，降低波动）
风险平价: w_i = (1/σ_i) / Σ(1/σ_j)
```

**约束**: 单只最大仓位不超过15%，总仓位不超过90%

---

### Phase 5: 绩效归因系统

**目标**: 每笔交易都能追溯盈亏来源

**实施路径**:
```
trading_system/attribution/
├── __init__.py
├── trade_log.py       # 完整交易记录（买入→持有→卖出）
├── brinson.py         # Brinson归因（配置+选股+交互）
├── alpha_beta.py      # Alpha/Beta分离（CAPM回归）
├── sharpe_decompose.py # 夏普比率分解
└── report.py          # 周度/月度归因报告
```

**核心输出**:
- 每笔交易：持有天数、收益率、最大浮盈、最大浮亏、MFE/MAE
- 组合层面：Alpha来源（选股 vs 择时）、行业贡献、因子暴露

---

### Phase 6: 模拟盘验证

**目标**: 新策略上线前必须跑1个月模拟盘

**实施路径**:
```
trading_system/paper_trading/
├── __init__.py
├── simulator.py       # 模拟交易引擎
├── tracker.py         # 持仓跟踪 + 每日净值
├── comparator.py      # 模拟盘 vs 实盘偏差分析
└── graduation.py      # 毕业条件判定（模拟盘达标后转实盘）
```

**毕业条件**: 模拟盘运行20个交易日，夏普>1.0 且 最大回撤<10%

---

## 实施原则

1. **回测先行**: 任何策略改动必须先跑回测，对比改动前后指标
2. **渐进上线**: 新模块先以"建议"模式运行，不直接产生信号
3. **防过拟合**: 样本外验证 + Walk-Forward + 蒙特卡洛
4. **性能约束**: 全流程（含回测）不超过5分钟
5. **向后兼容**: 不破坏现有main.py流程，新模块以插件形式接入

## 技术约束

- Python 3.10+，不引入重型框架（不用Django/Flask）
- ML仅用scikit-learn + XGBoost + PyTorch（LSTM）
- 数据源复用现有baostock + akshare
- 所有新模块必须可独立测试（`python -m backtest.engine --test`）
- 配置集中在config.py，新增配置段用注释分隔

## 验收检查清单

每完成一个Phase，验证：
- [ ] 单元测试通过（`python -m pytest tests/test_xxx.py`）
- [ ] 集成到main.py不报错
- [ ] 回测报告/邮件正常生成
- [ ] 性能指标不低于基线
- [ ] 代码有完整docstring
