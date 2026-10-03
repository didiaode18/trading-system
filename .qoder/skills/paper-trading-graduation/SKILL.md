---
name: paper-trading-graduation
description: 模拟盘引擎与策略毕业机制。当用户要求新策略上线前验证、搭建模拟交易、跟踪模拟盘净值、判定策略能否转实盘时使用。实现模拟撮合、每日净值、模拟盘与回测预期偏差分析、毕业条件判定，并与策略生命周期状态机打通。
---
# 模拟盘引擎 + 毕业机制 Skill

## 背景与定位
系统（V9.0）已具备策略生命周期状态机（research → backtest → paper → live → retired/rejected，载体 `trading_system/data/strategy_lifecycle.json`）与 `PAPER_TRADING_CONFIG` 开关，但缺少支撑 `paper` 状态的模拟盘引擎本体。本技能补齐该缺口：**任何通过回测的策略，必须在模拟盘"毕业"后才允许进入实盘。**

## 实施路径
```
trading_system/paper_trading/
├── __init__.py
├── engine.py          # 模拟盘引擎（逐日推进，复用回测数据馈送与撮合）
├── simulator_broker.py# 模拟券商（薄封装 backtest/broker.py，禁止重复实现费率逻辑）
├── tracker.py         # 持仓跟踪 + 每日净值（写入 SQLite paper_pnl 表）
├── comparator.py      # 模拟盘实际表现 vs 回测预期偏差分析
├── graduation.py      # 毕业条件判定（达标→可转 live，不达标→给出原因/延期/淘汰）
└── report.py          # 模拟盘报告区块（嵌入现有盘后/周报，静默降级）
```

## 核心要求
1. **复用而非重写**：行情馈送复用 `backtest/data_feed.py`，撮合/滑点/手续费/印花税/T+1 全部复用 `backtest/broker.py`；若 broker 接口需微调，在 broker.py 内做向后兼容改造，不得复制出第二套费率代码。
2. **逐日事件驱动**：与实盘决策流程一致——当日收盘后更新行情、检查止损止盈、调用策略生成信号、次日开盘价模拟成交，禁止用未来数据。
3. **每日净值留痕**：逐日记录总市值、净值、仓位、现金、当日成交；写入现有 SQLite（`trading_system/data/` 下数据库），表名 `paper_pnl` / `paper_trades`，不新建散落文件。
4. **偏差分析**：对比同策略回测在相同日期的预期净值/信号，输出：净值偏差、信号一致率、成交滑点偏差、漏信号/多信号清单；偏差超阈值在报告中标红。
5. **毕业判定**（默认值，可在 config 的 PAPER_TRADING_CONFIG 覆盖）：
   - 最少运行 **20 个交易日**；
   - 夏普比率 **> 1.0**；
   - 最大回撤 **< 10%**；
   - 模拟盘 vs 回测净值日偏差绝对值均值 **< 2%**（验证策略实盘可复现性）；
   - 达标 → 生命周期状态建议 `paper → live`（仅生成建议，由人工确认）；
   - 不达标 → 输出具体失败项，状态保持 paper（延期）或转 rejected（如偏差持续过大）。
6. **多策略并行**：一个策略一个 paper 实例（以策略名命名空间），互不干扰；支持同时跑多个候选策略。

## 关键代码模式
```python
class PaperTradingEngine:
    def __init__(self, strategy_name: str, strategy_fn, config: dict):
        self.strategy_name = strategy_name
        self.strategy_fn = strategy_fn          # 与回测同一签名 (date, data_dict, holdings) -> signals
        self.broker = SimulatorBroker()         # 内部委托 backtest/broker.py
        self.tracker = PaperTracker(strategy_name)

    def advance(self, date):
        """推进一个交易日：更新行情 → 止损止盈 → 策略信号 → 次日成交 → 记录净值"""

    def run_range(self, start_date, end_date):
        """批量推进（补跑历史模拟盘），逐日调用 advance"""


class GraduationJudge:
    def evaluate(self, strategy_name) -> dict:
        """返回 {graduated: bool, metrics: {...}, failed_items: [...], next_state: 'live'|'paper'|'rejected'}"""
```

## 与现有模块的接线
- 在 `trading_system/scheduler.py` 中增加盘后任务：交易日 16:30 推进所有 paper 策略一个交易日（受 PAPER_TRADING_CONFIG.enabled 开关控制，默认关闭）。
- 生命周期注册入口：策略回测通过后，由现有流程写入 paper 状态并创建 paper 实例；不改动 main.py 主流程。
- dashboard `策略管理` 页读取 paper 数据展示净值与毕业进度（数据不足时静默隐藏）。

## 实施原则
1. **默认观察模式**：引擎只记录与建议，不触发任何真实下单；毕业转 live 必须人工确认。
2. **静默降级**：数据不足（如新股 K 线不够）时报告区块自动隐藏并注明原因，禁止编造数据。
3. **向后兼容**：不破坏现有 main.py / scheduler.py 流程，以插件与任务注册形式接入。
4. **性能约束**：单个 paper 策略推进 20 个交易日耗时 < 30 秒。

## 验收清单
- [ ] `python -m pytest tests/test_paper_trading.py` 通过（需自行编写：含毕业达标、不达标、偏差过大三类用例）
- [ ] 任选一个现有策略注册为 paper，推进 20 个交易日，输出每日净值与偏差报告
- [ ] 达标策略自动生成"可毕业转 live"建议；不达标策略列出具体失败项
- [ ] 模拟成交的滑点/费率与 backtest/broker.py 完全一致（抽查 3 笔对账）
- [ ] 开关关闭时全流程不执行、不报错；dashboard 与报告区块静默降级正常
