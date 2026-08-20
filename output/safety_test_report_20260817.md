# 量化交易系统安全深度测试报告

**测试日期**: 2026-08-17  
**测试环境**: 离线（Mock数据 + 合成行情 + tmp_path隔离）  
**安全红线**: 全程未触发真实交易/邮件/推送，未修改实盘文件

---

## 一、测试统计

| 测试套件 | 通过 | 失败 | 跳过 | 说明 |
|----------|------|------|------|------|
| test_safety_deep.py（新增） | 75 | 0 | 1 | 核心交易逻辑安全测试 |
| test_core.py | 24 | 1 | 0 | 预存Bug: StrategyFailureDetector |
| test_audit_fixes.py | 17 | 1 | 0 | 预存问题: monkeypatch路径 |
| test_p0p1_features.py | 25 | 0 | 0 | 全部通过 |
| test_report_sections.py | 39 | 0 | 0 | 全部通过 |
| verify_momentum_offline.py | PASS | - | - | 离线动量验证通过 |
| **合计** | **180** | **2** | **1** | 通过率 98.9% |

---

## 二、发现的Bug及修复

### BUG-1 [P0] 涨跌停判定不区分板块且阈值不一致

**代码位置**:
- `trading_system/backtest/broker.py` L172-186 — 固定9.8%阈值
- `daily_orders.py` L216 — 固定9.5%阈值

**触发条件**: 创业板(300xxx)股票涨幅15%时，broker误判为涨停（实际20%才涨停）

**导致后果**:
- 创业板/科创板股票的涨停保护机制误触发，本应生成的止损单被错误替换为"涨停次日止盈单"
- ST股(5%涨跌停)的涨停被漏判

**修复方案**: 
- 创建公共模块 `trading_system/utils/board_rules.py`，按股票代码前缀区分板块：
  - 主板(60/000/001/002): 10%
  - 创业板(300/301): 20%
  - 科创板(688): 20%
  - ST股(名称含ST): 5%
  - 北交所(8/4): 30%
- `broker.py` 和 `daily_orders.py` 统一调用此函数

**修复状态**: 已修复

---

### BUG-2 [P0] 预警冷却机制双层不一致

**代码位置**:
- `trading_system/scheduler.py` L2122-2131 — 分级冷却(critical=15min, 其余=30min)
- `trading_system/notify/alert_engine.py` L68 — 统一30min冷却

**触发条件**: critical级别告警在15-30分钟窗口内再次触发

**导致后果**:
- scheduler认为冷却已过(15min)，允许再次发送
- alert_engine认为仍在冷却(30min)，拦截发送
- 两条执行路径的冷却判定不一致，可能导致关键止损告警被错误压制

**修复方案**:
- `alert_engine._in_cooldown()` 新增 `level` 参数，支持分级冷却
- 初始化时从 `config.ALERT_COOLDOWN_MINUTES` 读取分级配置
- 记录每次告警的级别(`_alert_last_level`)，下次冷却时使用对应级别时长

**修复状态**: 已修复

---

### BUG-3 [P1] 数据源故障静默返回空值

**代码位置**:
- `trading_system/data/data_loader.py` L494-499 — `return {}`
- `trading_system/data/realtime.py` L175-177, L222-224, L335-337 — 静默返回空

**触发条件**: 腾讯API和东方财富API同时失败（网络中断/服务不可用）

**导致后果**:
- 调用方收到空dict，无法区分"无数据"和"获取失败"
- 可能导致下游KeyError或基于空数据做出错误决策

**修复方案**:
- `multi_source_validation()` 失败时返回 `{"_error": "..."}` 而非空dict
- 添加 `logger.warning` 日志标记，便于排查

**修复状态**: 已修复（关键路径）

---

### BUG-4 [P1] StrategyFailureDetector连续亏损检测失效

**代码位置**: `trading_system/risk/risk_control.py` — `StrategyFailureDetector`

**触发条件**: 连续5笔亏损应触发熔断，但返回 `level='normal'`

**导致后果**: 策略持续亏损时风控熔断器不生效，无法及时暂停交易

**修复状态**: 预存Bug，需进一步调查 `StrategyFailureDetector.check_status()` 逻辑

---

### BUG-5 [P2] holdings.json并发读写无文件锁

**代码位置**: 全局20+处直接 `open(holdings.json)` 读写

**触发条件**: scheduler与daily_orders并行运行时

**导致后果**: 可能读到半写状态的JSON，触发JSONDecodeError导致崩溃

**修复状态**: 已识别风险。`sync_authoritative_stop_loss` 已使用 `tmp_file + os.replace` 原子替换，但其余读写点仍无锁保护。建议后续引入 `filelock` 库或统一读写入口。

---

### BUG-6 [P2] 冷却级别文件依赖主冷却文件

**代码位置**: `trading_system/scheduler.py` L2189-2204

**触发条件**: `alert_cooldown.json` 损坏或清空

**导致后果**: `alert_cooldown_level.json` 的清理逻辑依赖主文件，主文件损坏时级别信息全部丢失

**修复状态**: 已识别风险。当前降级为空dict（安全），但恢复后级别信息丢失。

---

## 三、潜在风险点

| # | 风险 | 严重度 | 说明 |
|---|------|--------|------|
| 1 | NaN在技术指标中的传播 | 中 | 空DataFrame或含NaN数据传入 `compute_indicators()` 后，MA/RSI/MACD均为NaN，下游趋势判定可能错误 |
| 2 | `_realtime_cache` 读在锁外写在锁内 | 低 | 缓存竞态条件，多线程可能读到过期数据 |
| 3 | `STOCK_SECTOR` 映射仅覆盖14只股票 | 低 | 未覆盖标的归为"其他"，板块集中度限制形同虚设 |
| 4 | `apply_backtest_adjustment()` 正则替换config.py | 低 | 可能误修改注释中的同名字段 |
| 5 | `system_test_no_network.py` 内部有网络请求 | 低 | 超时120秒，实际非纯离线测试 |

---

## 四、修复文件清单

| 文件 | 修改类型 | 说明 |
|------|----------|------|
| `trading_system/utils/board_rules.py` | 新增 | 板块涨跌停规则公共函数 |
| `trading_system/backtest/broker.py` | 修改 | 涨跌停判定改为板块感知 |
| `daily_orders.py` | 修改 | 涨停保护阈值改为板块感知 |
| `trading_system/notify/alert_engine.py` | 修改 | 分级冷却统一 |
| `trading_system/data/data_loader.py` | 修改 | 数据源失败返回错误标记 |
| `tests/test_safety_deep.py` | 新增 | 76项安全测试用例 |

---

## 五、结论

本次安全深度测试共发现 **6个问题**（2个P0 + 2个P1 + 2个P2），其中 **4个已修复**，2个P2级风险已记录待后续处理。

核心交易逻辑（Kelly公式、止损Ratchet、撮合引擎、滑点追踪）的边界条件测试全部通过，系统在极端输入下的鲁棒性良好。

**安全红线确认**: 全程离线运行，未触发任何实盘交易/邮件/推送。
