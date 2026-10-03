---
name: quant-system-upgrade
description: 量化交易系统升级路线图与实施指南（V9.0基线更新版）。当用户要求优化交易系统、新增策略模块、扩充因子库、完善绩效归因或进行系统架构升级时使用。本版标注六个历史Phase的完成状态，聚焦剩余的质量/情绪因子入库与Brinson归因；模拟盘、成交对账、证据分级已分别移交对应专项技能。
---
# 量化系统升级 Skill（V9.0 基线 · 更新版）

## 系统现状（V9.0，2026-09 审计）
已具备：事件驱动回测（含滑点/费率/T+1）、Walk-Forward、蒙特卡洛、Deflated Sharpe、因子注册表与 IC 动态降权/禁用、ML 信号确认（三分类 + 漂移降级）、Kelly/风险平价/波动率目标、15 道风控关卡、看门狗心跳与自动拉起、Streamlit 仪表盘、Alpha/Beta/Barra 归因、策略生命周期状态机、邮件/企业微信通知。

## 历史 Phase 状态（避免重复建设）
| Phase | 内容 | 状态 |
|---|---|---|
| Phase 1 | 事件驱动回测引擎 | ✅ 已完成（`backtest/`） |
| Phase 2 | 多因子库 + IC 监控 | 🟡 大部分完成，**仅剩质量/情绪两类因子未入库**（本版任务 A） |
| Phase 3 | 机器学习信号增强 | ✅ 已完成（`ml/`，动态训练 + 只确认不发信号） |
| Phase 4 | 动态仓位管理 | ✅ 已完成（`position/`） |
| Phase 5 | 绩效归因系统 | 🟡 Alpha/Beta/Barra 已完成，**缺 Brinson 与夏普分解**（本版任务 B） |
| Phase 6 | 模拟盘验证 | ➡️ 已移交 `paper-trading-graduation` 技能，本技能不再处理 |

另：成交回报与对账 → `trade-reconciliation` 技能；证据分级与事件催化剂 → `evidence-graded-research` 技能。**执行本技能时禁止重做上述已完成或已移交的内容。**

---
## 任务 A：质量因子 + 情绪因子入库
**目标**：把散落在 strategy 中的基本面与情绪逻辑重构为标准因子，注册进 `factors/registry.py`，统一接受 IC 监控与降权。

实施路径：
```
trading_system/factors/
├── quality.py        # 质量因子：ROE、毛利率、净利率、经营现金流/净利润、现金流稳定性、资产负债率、ROE改善
└── sentiment.py      # 情绪因子：涨跌停比、换手率、量比、融资余额变化、龙虎榜净买入、涨停封板率、（北向恢复后）北向净流入
```
核心要求：
1. 因子逻辑从现有 `strategy/fundamental.py · financial_report.py · moat.py`（质量）与 `strategy/margin_monitor.py · lhb_analyzer.py · zt_monitor.py · capital_flow.py`（情绪）中**抽取重构**，保持口径一致；原 strategy 文件改为调用因子函数或保留策略层用途，不允许两套逻辑长期并存。
2. 财务类因子严格对齐报告期，避免前视：t 期财报数据在披露日之后才可用（记录披露日字段）。
3. 注册后自动纳入 IC/IR 计算、分层回测（5 组单调性）、IC 衰减降权与 VIF 共线性检查，流程与现有因子完全一致。
4. `registry.py` 的 `_auto_register` 增加 quality / sentiment 两类自动注册。
5. 情绪因子数据缺失（如接口限流、北向停披）时因子值标记 NaN 并静默降级，禁止填 0 造成误判。

---
## 任务 B：Brinson 归因 + 夏普分解
**目标**：把组合超额收益分解为配置效应、选股效应与交互效应，并分解夏普来源。

实施路径：
```
trading_system/attribution/
├── brinson.py            # Brinson 归因（按行业/赛道：配置效应 AR + 选股效应 SR + 交互效应 IR）
└── sharpe_decompose.py   # 夏普比率分解（收益来源/波动来源，含与基准的对比）
```
核心要求：
1. Brinson 公式：以行业（复用 config 股票池 sector 字段/申万二级口径）为分组，组合权重/收益 vs 基准（沪深300）权重/收益，输出 AR、SR、IR 及合计超额，三项合计须与实际超额收益对得上（误差 < 0.1pct）。
2. 基准权重与行业收益通过现有数据层统一获取，口径与报告其他部分一致。
3. 结果并入周报（V4.1 已有 CAPM 归因区块，扩展为 Brinson 区块）；数据不足静默降级。
4. 夏普分解输出：主动收益、主动波动、信息比率与夏普的构成，区分 Beta 收益与 Alpha 收益。

---
## 实施原则
1. **回测/IC 先行**：新因子必须先跑 IC 与分层回测，验证有效后才注册启用；校准类能力（自动降权等）默认观察模式，只展示不自动生效。
2. **抽取而非复制**：从 strategy 重构因子时消除重复逻辑，改动需保证现有策略行为不变（跑回归测试对比信号）。
3. **向后兼容、插件接入**：不破坏 main.py 流程；新增配置在 config.py 分段集中管理。
4. **性能约束**：全量因子计算 + 回测全流程不超过 5 分钟。

## 技术约束
- Python 3.10+，不引入重型框架；数据复用 baostock / akshare / 腾讯行情。
- 所有新模块可独立测试（`python -m pytest tests/xxx.py`），含完整 docstring。

## 验收清单
任务 A：
- [ ] `python -m pytest tests/test_quality_sentiment_factors.py` 通过（含报告期前视检查、缺失数据降级）
- [ ] 注册表五类因子齐全，新因子输出 IC 曲线、分层回测与行业暴露
- [ ] strategy 中对应逻辑改为复用因子函数，全量信号回归对比无变化
- [ ] 数据缺失时因子静默降级，不产生 0 值误判

任务 B：
- [ ] `python -m pytest tests/test_brinson.py` 通过
- [ ] Brinson 三项合计 = 实际超额收益（误差 < 0.1pct）
- [ ] 周报正确展示 Brinson 区块与夏普分解，数据不足时静默隐藏
