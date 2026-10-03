---
name: trade-reconciliation
description: QMT实盘成交回报与账实对账闭环。当用户要求核对订单实际成交、拉取券商成交流水、修正本地持仓记账、排查部分成交/拒单/滑点偏差时使用。实现成交回调、流水查询、订单状态回填、盘后券商数据与本地账目逐日对账及差异告警。
---
# 成交回报 + 账实对账 Skill

## 背景与定位
现有 `qmt_trader.py` 实现了下单/撤单/查持仓/查资金，但执行后仅记录本地 `executed` 标志，**没有成交回报回调与成交流水查询**，本地 holdings.json 不与券商实际成交核对。这对应实战中最危险的坑："记账失真——账面盈利、实际亏损"。本技能建立铁律：**一切持仓与盈亏以券商真实成交流水为准，本地估算仅作参考。**

## 实施路径
```
trading_system/execution/
├── fill_callback.py      # XtQuant 成交/委托回报回调注册与解析
├── trade_ledger.py       # 成交流水台账（SQLite：委托→成交→部分成交全过程）
├── order_reconcile.py    # 单笔委托对账（委托量 vs 成交量、均价 vs 预期价、状态）
└── daily_reconcile.py    # 盘后总账对账（券商持仓/资金 vs 本地 holdings/净值）
```
（execution 目录已存在，只新增文件；qmt_trader.py 做接线改造。）

## 核心要求
1. **成交回报回调**：通过 xtquant 的 `register_callback` 注册回调对象，实现：
   - `on_stock_order(order)`：委托状态变化（已报/部分成交/已成交/已撤/拒单）；
   - `on_stock_trade(trade)`：逐笔成交回报（成交编号、委托编号、代码、方向、成交价、成交量）；
   - 每笔回报写入 `trade_ledger`，状态变化全程留痕。
2. **主动流水查询兜底**：连接后与盘后调用 `query_stock_orders` / `query_stock_trades` 拉取当日全量委托与成交，防止回调丢失（断线/启动前成交）；以券商流水为权威数据回填订单。
3. **成交均价与状态回填**：
   - 部分成交：按实际成交量更新，未成交部分标记待成交/可撤；
   - 全部成交：用 Σ(成交价×成交量)/Σ成交量 计算实际均价回填；
   - 拒单/已撤：清除本地执行标志并记录原因，不得保留"已执行"假象。
4. **盘后账实对账**（交易日 15:30 后执行）：
   - 持仓对账：`query_stock_positions` 的代码/数量/可用量/成本 vs holdings.json，逐行输出差异；
   - 资金对账：`query_stock_asset` 的总资产/可用/市值 vs 本地净值计算；
   - 成交对账：当日每笔委托给出"委托量/成交量/委托价/实际均价/滑点"五列；
   - 滑点偏差超过 config 阈值（默认买入 +0.3%、卖出 -0.3%）标红。
5. **差异告警与台账**：账实不一致时立即通过现有企业微信/邮件通道告警（复用 notify，不新增通道），并写入对账台账（SQLite 表 `reconcile_ledger`，含日期、类型、差异明细、是否已处理）；同一差异当日去重，沿用现有看门狗的去重模式。
6. **无 xtquant 可测**：在现有模拟模式（has_xtquant=False）下用构造的回报数据跑通全部逻辑，禁止依赖真实客户端才能测试。

## 关键代码模式
```python
class FillCallback:
    def on_stock_order(self, order):      # 委托状态机更新 → trade_ledger
    def on_stock_trade(self, trade):      # 逐笔成交累积 → 更新订单成交量/均价
    def on_disconnected(self):           # 断线标记，恢复后主动拉流水补全

class DailyReconciler:
    def run(self) -> dict:
        """返回 {positions: [...diff], asset: {...diff}, orders: [...], has_diff: bool}"""
        # 1. 拉券商持仓/资金/成交  2. 读本地 holdings/净值
        # 3. 逐项比对  4. 差异写台账并触发告警
```

## 接线点
- `qmt_trader.py`：connect 成功后 `register_callback(FillCallback())`；ConditionOrderEngine 执行后以成交回报确认实际成交，而非 place_order 返回即视为成功。
- `scheduler.py`：盘后新增对账任务（15:35），结果并入盘后报告区块（execution 已有 slippage_section，扩展为"成交对账区块"）。
- 配置统一加入 config.py 的 QMT_CONFIG / 新增 RECONCILE_CONFIG 段，阈值与开关集中管理。

## 实施原则
1. **券商数据为唯一权威**：本地记账与券商冲突时，以券商流水为准并记录修正过程；自动修正仅生成建议，是否改 holdings 由人工确认（初期）。
2. **先模拟验证后接线**：全部场景在模拟模式测试通过，再接入真实 QMT。
3. **静默降级**：查询失败/数据缺失时明确标注"对账未完成"并重试，禁止输出"全部一致"的假结论。
4. **向后兼容**：不改变现有下单接口签名与风控检查流程。

## 验收清单
- [ ] `python -m pytest tests/test_trade_reconciliation.py` 通过（自行编写：全部成交、部分成交、拒单、回调丢失后流水补全、滑点超标五类用例）
- [ ] 模拟一笔部分成交：系统识别真实成交量、均价，订单状态正确，持仓不被多记
- [ ] 构造 holdings 与券商持仓不一致：盘后报告逐行列出差异并触发一次告警（去重生效）
- [ ] 断线重启后能通过主动流水查询补全缺失成交
- [ ] 对账失败时报告显示"未完成"而非"全部正常"
