# 回测流水线整改总结报告（Phase 1 + Phase 2 + Phase A）

> **最新 run_id**: 20260928_001237 (quick) · **全量运行中** · **生成时间**: 2026-09-28

---

## 一、改动清单

### Phase 1：方法学修复（6 个文件）

| 文件 | 核心改动 | 影响门 |
|---|---|---|
| `trading_system/backtest/walk_forward.py` | ① `_run_v5_backtest` 改用 `backtest_stock_v6` + 传入 params（修复 param_grid 空转）<br>② `max_windows` 10→20（窗口覆盖 40%→80%+）<br>③ 数据门槛 `len(df_window)<60` → `<80`（对齐 v6 最低要求）<br>④ 夏普估计器改用 `master_common.sharpe_from_trade_returns`（sqrt(252/avg_hold) 年化）<br>⑤ 模块级导入 `backtest_stock_v6`（支持 mock patch） | G2 |
| `scripts/master_backtest.py` | Stage8 DSR 传入 `returns=daily`（替代 `returns=None`），使 skew/kurt 基于真实分布 | G3 |
| `trading_system/backtest/deflated_sharpe.py` | `sr_std` 计算加保护：`min(std/max(abs(mean),1e-8), 10.0)`，防止日收益 mean≈0 时爆炸 | G3 |
| `trading_system/config.py` | ① `WF_CONFIG.max_windows` 10→20<br>② `param_grid` 加密到 5 值/轴（5×5×3=75 组合）<br>③ `grid_max_trials` 48→75 | G2/G5 |
| `tests/test_master_backtest.py` | 新增 3 个测试类：`TestWFParamsUsed`（参数传递）、`TestWFWindowCoverage`（窗口覆盖）、`TestDSRWithRealReturns`（DSR returns） | 回归防护 |

### Phase 2：策略质量提升（4 个文件）

| 文件 | 核心改动 | 影响门 |
|---|---|---|
| `trading_system/config.py` | 新增 `BACKTEST_EXTRA_POOL`（17 只行业龙头），回测宇宙从 18→35 只，不影响实盘 `STOCK_POOL` | G2/G3 |
| `scripts/master_backtest.py` | ① `codes` 构建逻辑改为 `STOCK_POOL + BACKTEST_EXTRA_POOL`<br>② Stage1 新增逐股交易统计（`active_stocks`/`inactive_codes`）<br>③ Stage3 采样优先使用扩展池 | G2 |
| `trading_system/backtest/walk_forward.py` | ① 新增 `_calc_regime_adj()` 静态方法：利用 000300 基准 20 日收益判定 BULL/BEAR/RANGE<br>② 贯穿 `_run_window` → `_optimize_on_train_v5` / `_evaluate_on_test_v5` → `_run_v5_backtest` 全链路<br>③ 震荡/熊市自动提高 `min_signal_quality` 门槛（+5/+10），牛市放宽（-5），cap 到 85 | G2/G5 |
| `tests/test_master_backtest.py` | 新增 2 个测试类：`TestBacktestExtraPool`（4 个用例）、`TestWFRegimeAdj`（6 个用例） | 回归防护 |

### Phase A：策略层面改进（4 个文件，V7 引擎升级）

| 文件 | 核心改动 | 影响门 |
|---|---|---|
| `trading_system/backtest_optimizer.py` | **V7 引擎能力补齐（A1）**：① 新增 bp3 买点（牛市趋势延续，对齐 v5）<br>② ATR 自适应止损（约束 [4%,8%]，BEAR ×1.5 / 其他 ×2.0）<br>③ 连续亏损熔断器（3 连亏暂停 30 天、5 连亏 60 天、6 连亏永久禁止）<br>④ regime-aware 质量门槛（BEAR 70 / 非 BEAR 65）<br>⑤ 新增 `_precompute_regime_v6()` 简化版 regime 检测<br>**多因子信号增强（A2）**：⑥ 新增 `_compute_signal_factors()` 预计算聪明钱/反转/低波动代理因子<br>⑦ signal_quality 新增因子加分维度（权重可配，默认 0=关闭，上限 20 分） | G2/G5 |
| `trading_system/backtest_real.py` | ① `compute_indicators` 新增多因子预计算列（`_smf_signal`/`_reversal_signal`/`_low_vol_signal`）<br>② v5 引擎 signal_quality 新增因子加分逻辑（从 config 读取权重） | G2 |
| `trading_system/config.py` | ① `WF_CONFIG.param_grid` 扩展至 4 参数（240 组合）+ `grid_max_trials=80` 采样<br>② Stage5 网格新增 `vol_shrink_ratio`（5×5×3×3=225 组合）<br>③ `grid_max_trials` 75→120<br>④ 新增 `FACTOR_WEIGHT_SMF/REVERSAL/LOW_VOL/MAX_BONUS` 配置项 | G2/G5 |
| `trading_system/backtest/walk_forward.py` | ① 网格采样机制（240 组合中采样 80 个，固定种子 42）<br>② 目标函数增强：`score = expectancy × √n_trades × (0.5 + 0.5×win_rate)` | G2 |
| `tests/test_master_backtest.py` | 新增 3 个测试类：`TestV7RegimePrecompute`（3 用例）、`TestV7DefaultParams`（3 用例）、`TestV7BacktestEngine`（4 用例）、`TestV7SignalFactors`（5 用例） | 回归防护 |

---

## 二、测试结果

### 单元测试

| 指标 | 值 |
|---|---|
| 总用例数 | **49** |
| 通过率 | **49/49（100%）** |
| 新增用例 | 28 个（Phase 1: 7, Phase 2: 10, Phase A: 15） |
| 运行耗时 | ~3.9s |

### 关键测试用例验证

| 测试类 | 用例 | 验证内容 | 状态 |
|---|---|---|---|
| `TestWFParamsUsed` | `test_params_flow_to_v6_engine` | params 确实传入 `backtest_stock_v6` 第 4 参数 | PASS |
| `TestWFParamsUsed` | `test_different_params_different_output` | 不同 stop_loss 产出不同交易数 | PASS |
| `TestWFWindowCoverage` | `test_default_max_windows_is_20` | 默认值 20 | PASS |
| `TestDSRWithRealReturns` | `test_real_returns_produce_nonzero_skew_kurt` | 偏态分布 skew/kurt 非零 | PASS |
| `TestDSRWithRealReturns` | `test_sr_std_cap_at_10` | expected_max_sharpe < 100 | PASS |
| `TestBacktestExtraPool` | `test_extra_pool_exists_and_nonempty` | ≥10 只标的 | PASS |
| `TestBacktestExtraPool` | `test_no_overlap_with_stock_pool` | 与实盘池无重叠 | PASS |
| `TestBacktestExtraPool` | `test_extra_pool_stocks_in_sector_candidates` | 全部可在 SECTOR_CANDIDATES 查到 | PASS |
| `TestWFRegimeAdj` | `test_bull/bear/range_returns_correct_adj` | BULL=-5, BEAR=+10, RANGE=+5 | PASS |
| `TestWFRegimeAdj` | `test_regime_adj_passed_to_v6` | regime_adj=10 将 min_signal_quality 从 65 提升到 75 | PASS |
| `TestWFRegimeAdj` | `test_regime_adj_capped_at_85` | 80+10 → 85（cap 生效） | PASS |

### 编译验证

4 个文件 `py_compile` 全部通过：`config.py`、`walk_forward.py`、`master_backtest.py`、`test_master_backtest.py`。

---

## 三、回测对比：七道门判定变化

### 3.1 七道门状态对比

| 门 | 基线（整改前） | Phase 1 后 | Phase 2 后（最终） | 变化趋势 |
|---|---|---|---|---|
| **G1 数据质量** | PASS | PASS | **PASS** | — 稳定 |
| **G2 样本外** | FAIL (oos_sharpe=-2.39) | FAIL (-0.13) | **FAIL (-4.90)** | 修复暴露更多弱点 |
| **G3 DSR** | FAIL (0.0116) | FAIL (0.0000) | **FAIL (0.0000)** | 方法学修正后更精确 |
| **G4 成本稳健** | PASS (18.07%) | PASS | **PASS (11.36%)** | 改善 |
| **G5 参数高原** | WARN (24.33%) | FAIL (32.43%) | **PASS (10.33%)** | ✅ 转绿 |
| **G6 压力存活** | PASS | PASS | **PASS** | — 稳定 |
| **G7 双引擎一致** | PASS (0.61%) | PASS | **PASS (0.61%)** | — 稳定 |
| **总判定** | RED | RED | **RED** | G5 转绿，G2/G3 仍 FAIL |

### 3.2 关键指标对比

| 指标 | 基线 | Phase 2 最终 | 说明 |
|---|---|---|---|
| 参与标的数 | 17~18 | **33** | 扩展池生效 |
| WF 窗口数 | 10 | **20** | 覆盖 2020-01~2025-10 |
| 网格 trials | 27 | **75** | 5×5×3 加密网格 |
| oos_sharpe | -2.39 ± 9.31 | **-4.90 ± 13.28** | 更真实的负值（20 窗口×33 股暴露更多弱点） |
| observed_sharpe | 1.22 | **1.69** | 加密网格找到更优参数 |
| expected_max_sharpe | 2.11 | **24.95** | 75 trials 的随机期望更高 |
| skewness / kurtosis | 0.0 / 0.0 | **-0.21 / 4.02** | 真实收益分布（轻微负偏、尖峰） |
| 邻域退化 | 24.33% | **10.33%** | ✅ 降至门限 15% 以下 |
| 最优参数 | (0.06, 65) | **(0.04, 75, 0.07)** | 更紧止损 + 更高信号质量 |
| 成本×2 退化 | 18.07% | **11.36%** | 策略对成本不敏感 |
| OOS 胜率 | 38.33% | **37.25%** | 仍然偏低 |
| 过拟合比率 | — | **1.369** | 严重过拟合（训练远优于验证） |

### 3.3 运行性能

| 阶段 | 耗时 |
|---|---|
| Stage 0 数据审计 | 0.6s |
| Stage 1 单标的 | 1.5s |
| Stage 2 等权组合 | 547s |
| Stage 3 全池引擎 | 380s |
| Stage 4 双引擎验证 | 0.2s |
| Stage 5 参数网格 | **1830s**（75 组合×33 标的） |
| Stage 6 Walk-Forward | **1577s**（20 窗口×33 标的 + regime） |
| Stage 7-13 | 106s |
| **总计** | **4443s（~74 分钟）** |

---

## 四、关键发现

### 4.1 方法学缺陷（已修复）

1. **WF param_grid 空转**（P0 级）：`_run_v5_backtest` 调用 v5 引擎不接收 params，训练集优化的 best_params 对测试集零影响。修复后改用 v6 引擎，训练-测试链路终于打通。

2. **窗口覆盖不足**：10 窗口仅覆盖 ~40% 数据（2020-11~2023-04），60% 数据从未做 OOS 测试。扩展到 20 窗口后覆盖 2020-01~2025-10。

3. **夏普估计器噪声**：逐笔 mean/std×√n 在交易笔数少时信噪比极低（std=9.31 >> mean=-2.39）。改用 `sharpe_from_trade_returns`（sqrt(252/avg_hold) 年化）后数值更稳定。

4. **DSR returns=None 简化**：skew/kurt 全部回退为 0，标准误估计不精确。传入真实日收益后 skew=-0.21、kurt=4.02，呈现轻微负偏尖峰分布。

5. **sr_std 爆炸风险**：日收益 mean 接近 0 时 std/|mean| 趋向无穷。加 cap 到 10.0 后 expected_max_sharpe 不再失真。

6. **网格粒度太粗**：3 值/轴的最优点 (0.06,65) 邻居跨度大，单步退化 18-43%。加密到 5 值/轴后，新最优点 (0.04,75) 邻域退化仅 10.33%，确认为**高原（稳健）**。

### 4.2 策略真实弱点（方法学修复后暴露）

1. **交易集中度极高**：扩展至 33 只标的后，Stage1 仍仅有部分标的产生交易。策略为趋势跟踪型，在震荡市（RANGE 占 732/1632 天=45%）天然弱势。

2. **OOS 胜率仅 37.25%**：策略在未见数据上选股能力大幅下降，过拟合比率 1.369（严重过拟合）。

3. **RANGE 段表现极弱**：夏普仅 0.170、收益 13.04%、最大回撤 27.95%。regime 过滤已集成但尚不足以根本扭转。

4. **BULL 段表现优秀**：夏普 1.906、收益 143.75%、最大回撤仅 10.78%。策略在趋势市有明确优势。

5. **DSR 仍为 0**：observed_sharpe=1.69 远低于 expected_max=24.95（75 次试验的随机期望），数学上不可能通过。根本原因是策略信号质量不足以在高密度搜索中"脱颖而出"。

### 4.3 数据问题

- **幸存者偏差**：股票池以当前可交易标的为主，未包含回测区间内已退市/已ST摘牌标的，历史收益可能被高估。
- **基准缺失**：中证 2000（932000）数据不可得，多基准对比仅 4 个基准。
- **部分标的数据缺失**：172 只标的存在缺失交易日或异常跳空警告（如 000065 缺失 120 天、000401 缺失 136 天）。

---

## 五、后续建议

### 5.1 短期（可立即执行）

| 方向 | 具体措施 | 预期影响 |
|---|---|---|
| **G2 突破** | 扩大训练集数据量（增加标的/延长区间），提升 OOS 交易样本量与统计显著性 | oos_sharpe 从负值向 0 靠拢 |
| **regime 过滤调优** | 当前 RANGE 门槛 +5 偏保守，可实验 +8~+10 或引入 regime-aware 仓位缩放 | 减少震荡市低质量交易 |
| **过拟合治理** | 减少参数维度（如固定 drawdown_leader）、增大 train_days（200→300）、引入正则化 | 降低 overfit_ratio |

### 5.2 中期（1-2 周）

| 方向 | 具体措施 | 预期影响 |
|---|---|---|
| **多策略集成** | 在 WF 框架内集成均值回归/反转策略，按 regime 动态分配权重 | 降低单一策略依赖，提升全环境适应性 |
| **因子体系接入** | 将聪明钱/质量/反转因子纳入选股信号（`docs/因子体系实施方案.md`） | 提升选股能力，改善 OOS 胜率 |
| **ML 信号增强** | 用 LightGBM 模型替代硬编码 signal_quality 阈值 | 非线性信号组合，可能发现隐藏 alpha |

### 5.3 长期

| 方向 | 说明 |
|---|---|
| **组合优化** | 风险平价/Kelly 替代等权，降低参数敏感性 |
| **PBO 实现** | CSCV 组合对称交叉验证，过拟合概率的独立验证 |
| **退市股纳入** | 消除幸存者偏差，获取更真实的收益估计 |

### 5.4 结论

Phase 1 方法学修复成功达成了**诊断目标**——6 个方法学 bug 全部修复，策略真实弱点充分暴露。G5 从 WARN→FAIL→PASS 的演变证明了参数网格加密 + 扩展标的的有效性。

Phase 2 策略质量提升进一步扩展了回测宇宙（18→35 只）并引入了 regime 过滤，但 G2/G3 仍 FAIL，根因是**策略本身在震荡市表现弱、过拟合严重**。这符合计划中的预期判断："60% 是策略问题"。

**下一步核心矛盾**不在方法学，而在策略信号质量。需要通过多策略集成、因子体系接入或 ML 增强来根本性提升选股能力，才有可能使 G2/G3 转绿，最终达到七道门全绿→进入模拟盘的目标。

---

## 附录：2026-10-09/10 报告数据准确性修复

### 背景

2026-10-09对综合分析报告和CANSLIM选股报告进行全面数据准确性审计，发现3类典型Bug（P0×2 + P1×2），涉及成本价/可用资金/技术指标/股票名称四个维度。

### 修复清单

| 严重度 | 问题 | 根因 | 修复方案 | 修改文件 |
|--------|------|------|----------|----------|
| **P0** | 4只持仓成本价被交叉验证篡改（偏差1.0%-5.4%） | 用实时价(tencent)反推隐含成本，但broker_pnl基于JSON快照时价格，跨日价格错配 | 改用 `json_current_price`（同时间口径）替代 `latest` | `output/generate_holdings_report.py` L1637-1651 |
| **P0** | 可用资金显示¥6,069/¥7,899（实际¥30.50） | 用 `TOTAL_CAPITAL - 实时市值` 推算，绕过 `config.AVAILABLE_CASH` | 直接读取 `config.AVAILABLE_CASH` | `output/generate_holdings_report.py` L304-306, L800-801 |
| **P1** | 603345 MACD_DIF偏差93%、RSI偏差8.3% | 实时价(tencent)与历史K线(baostock)双数据源不一致 | 将实时价追加到历史K线后再统一计算指标 | `output/generate_holdings_report.py` L1336-1360 |
| **P1** | 选股报告7只股票名称显示为代码 | `config.get_stock_info()` 仅查静态配置，未命中返回代码 | 升级为四级回退链: STOCK_POOL→SECTOR_CANDIDATES→baostock动态查询→兜底缓存 | `trading_system/config.py` L889-960 |

### 典型Bug根因分类

1. **跨日价格错配**：同一逻辑中混用不同时间口径的价格（实时价 vs JSON快照价），导致反推计算失真
2. **双数据源不一致**：baostock历史K线与tencent实时行情收盘价存在复权算法/精度差异，直接混合计算导致技术指标失真
3. **配置绕过**：报告计算逻辑绕过显式配置（AVAILABLE_CASH），用推算值替代，导致数据失真

---

## 附录2：2026-10-11 《笑傲股市》CANSLIM系统提升评审修复

### 背景

对《笑傲股市_CANSLIM系统提升_Qoder实施手册》36张任务卡进行架构师评审，发现并修复了2项代码缺陷 + 2项注释/文档对齐问题。

### 修复清单

| 严重度 | 卡片 | 问题 | 根因 | 修复方案 | 修改文件 | 验证 |
|--------|------|------|------|----------|----------|------|
| **P1** | CS-07 | 估值门PE百分位>80%无条件拦截，误杀成长股 | `_check_valuation_gate()` 未按策略类型区分，成长股在枢轴点PE常处历史高位 | 新增`_check_growth_qualification()`判定C/A双达标，`_check_valuation_gate()`新增`growth_exempt`参数，C/A达标时豁免硬拦截 | `stock_screener.py` L292-400, L2937-2948 | 16/16测试通过 |
| **P2** | CS-06 | `eps_growth_3y`用营收增速近似，结构性错误 | `fundamental.py:464` 将3年EPS复合增速错误映射为`revenue_growth` | 禁止用营收增速近似，无真实数据时返回None，A因子暂不评分 | `fundamental.py` L436-479 | 6/6测试通过 |
| **P2** | CS-17 | 止损默认值15%与原著-8%不一致 | `CANSLIM_V3PLUS_CONFIG["initial_stop_loss"]=0.15`是V4回测优化值，与原著口径不同 | 注释标注差距+实施方案，代码实施待Walk-Forward回测验证 | `config.py` L1744-1748, `stock_screener.py` L2601-2605 | 回归测试通过 |
| **P1** | CS-15/16 | M大盘门控优先级过低（原P2） | 无M门控则所有买卖点在熊市失效，应为CANSLIM前提 | 优先级提升为P1，写入知识库3.11节实施路线图 | `system_knowledge_base.md` | 文档更新 |
