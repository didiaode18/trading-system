# 操盘密码 V9.0 — A股中线波段量化交易系统

> 中线波段(3天-4周) · 条件单驱动 · 多因子选股 · 纪律自动化
> 数据源: baostock + 腾讯行情API | 输出: HTML邮件报告 + 条件单 + QMT执行

---

## 系统定位

个人量化交易辅助决策系统，核心解决：**管不住手，让情绪毁掉策略**。

- 盘前/盘后自动生成完整交易计划（趋势分析 + 条件单 + 选股推荐）
- V5.3回测引擎：等权10只标的 **组合收益+47%，最大回撤-8%，超额收益+25%**
- 13个功能模块层覆盖完整量化交易链路
- 风控硬约束 + 操作纪律锁，彻底消除人为干预

---

## 工程结构

```
trading-system/
├── caopan_report.py              # ★ 操盘密码主程序（报告+邮件）
├── caopan_runner.py              # 分析引擎运行器
├── daily_orders.py               # 每日条件单生成器
├── run.py                        # 统一CLI入口
├── qmt_trader.py                 # QMT自动交易执行器
├── auto_scheduler.py             # 自动化调度守护进程
├── requirements.txt              # Python依赖
│
└── trading_system/               # 核心包（13个模块层）
    ├── config.py                 # 全局配置（资金/邮箱/股票池/风控参数）
    ├── backtest_real.py          # ★ V5.3回测引擎（numpy化，5.8x加速）
    ├── main.py                   # 主程序（盘后完整分析流程）
    ├── scheduler.py              # 定时任务调度
    │
    ├── data/                     # ① 数据层 - 行情获取(baostock/腾讯实时)
    ├── strategy/                 # ② 策略层 - 趋势/选股/条件单/反洗盘
    ├── risk/                     # ③ 风控层 - 熔断/仓位/强制卖出
    ├── backtest/                 # ④ 回测层 - 事件驱动/蒙特卡洛/Walk-Forward
    ├── factors/                  # ⑤ 因子层 - 动量/技术/量能/复合因子
    ├── position/                 # ⑥ 仓位层 - Kelly/风险平价/波动率目标
    ├── quant/                    # ⑦ 量化层 - 多因子引擎/组合优化
    ├── ml/                       # ⑧ 机器学习 - 特征工程/预测/监控
    ├── execution/                # ⑨ 执行层 - TWAP/滑点追踪
    ├── monitor/                  # ⑩ 监控层 - 盘中实时预警
    ├── attribution/              # ⑪ 归因层 - Alpha/Beta/Barra
    ├── notify/                   # ⑫ 通知层 - 邮件(QQ SMTP)/企业微信
    └── paper_trading/            # ⑬ 模拟交易 - 纸上交易验证
```

---

## 核心回测引擎（backtest_real.py V5.3）

### 性能指标

| 指标 | 数值 |
|------|------|
| 等权回测10只标的耗时 | **1.5秒**（含邮件），纯计算~0.3秒 |
| 性能优化加速比 | **5.8x**（8.7s → 1.5s） |
| 组合收益（2023-2026） | +47.3% |
| 最大回撤 | -8.0% |
| 超额收益（vs 沪深300） | +25.4% |
| 平均胜率 | ~62.5% |

### 核心技术

- **numpy向量化**：消除pandas iloc开销，内循环纯数组索引
- **ATR自适应止损**：`stop = ATR(14) × multiplier`，约束[4%,10%]，BEAR收紧
- **渐进式熔断器**：3连亏暂停20天 → 5连亏暂停60天 → 6连亏永久禁止
- **市场环境自适应**：MA20/MA60判定BULL/BEAR/RANGE，动态调整门槛和止盈
- **双轨止盈**：阶梯(8%卖1/3, 20%再卖1/3) + 回落止盈(底仓)
- **预计算滚动窗口**：rolling min/max/mean一次计算，循环内O(1)查询

---

## 策略版本演进

| 版本 | 核心变更 | 关键指标 |
|------|----------|----------|
| V2.0 | 基础趋势跟踪（MA20回踩） | 胜率~40% |
| V4.0 | MA60确认 + 止损分段 | 胜率~45% |
| V5.0 | 双买点 + 硬性过滤 + 双轨止盈 + 信号质量评分 | 胜率~50% |
| V5.1 | 市场环境自适应 + ATR止损 + 急涨过滤 | 胜率~55% |
| V5.2 | 个股适应性过滤（排除低波动白马） | 茅台12→6笔 |
| **V5.3** | **移除时间止损 + 渐进式熔断器（极简原则）** | **胜率62.5%，+47.3%** |
| V6.0 | 全赛道选股 + 趋势预测 + 定时任务体系 | 系统级扩展 |

> V5.3设计哲学：每次迭代只保留1-2项真正有效的改进，其余方案即使看似合理也需实证验证。

---

## 快速开始

### 环境要求
- Python 3.10+
- Windows（定时任务依赖 schtasks）

### 安装
```bash
pip install -r requirements.txt
```

### 配置

编辑 `trading_system/config.py`：
```python
TOTAL_CAPITAL = 737_834.70        # 总资金
EMAIL_SENDER = "your@qq.com"      # QQ邮箱
EMAIL_AUTH_CODE = "xxxx"          # SMTP授权码
EMAIL_RECEIVER = "your@qq.com"    # 收件邮箱
```

### 运行回测验证
```bash
cd trading_system
python run_equal_weight_backtest.py   # 等权10只标的回测（~1.5秒）
python run_full_backtest.py           # 74只全量回测
```

---

## 报告体系（4+1）

| 时间 | 报告 | 内容 |
|------|------|------|
| 08:30 | 盘前作战计划 | 市场环境+操作清单+关键价位 |
| 15:30 | 盘后深度复盘 | 九大模块全量分析+K线图 |
| 15:30 | 条件单操作计划 | 止损/止盈/时间条件单卡片 |
| 15:30 | 紧急预警 | 仅有触发时发送 |
| 周六 10:00 | 周策略报告 | 本周绩效+板块轮动 |

---

## 命令一览

```bash
# 操盘密码报告系统
python caopan_report.py --morning     # 盘前作战计划
python caopan_report.py --evening     # 盘后深度复盘
python caopan_report.py --weekly      # 周策略报告
python caopan_report.py --scheduler   # 启动定时调度

# 回测验证
cd trading_system
python run_equal_weight_backtest.py   # 等权分散回测（10只×10万）
python run_full_backtest.py           # 全量74只回测

# 统一入口
python run.py report                  # 盘后综合分析
python run.py orders                  # 条件单
python run.py auto                    # 全自动调度
```

---

## 风控硬约束

- 单只仓位 **≤25%**，总仓位 **≤90%**
- 单笔风险 **≤总资金2%**
- 单日亏损>5% → **熔断**
- 放量大跌>8% → **无条件离场**
- 连续亏损 → **渐进式暂停**（3连亏20天 / 5连亏60天 / 6连亏永久禁止）

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| 数据源 | baostock(历史K线) + 腾讯行情API(实时) |
| 数据处理 | pandas / numpy（回测引擎纯numpy向量化） |
| 图表 | matplotlib (Agg后端, base64嵌入邮件) |
| 通知 | QQ邮箱 SMTP SSL (465端口) |
| 调度 | Windows Task Scheduler / schedule库 |
| 数据库 | SQLite (stock_db.db) |

---

## 免责声明

本系统仅供学习研究使用，不构成任何投资建议。股市有风险，投资需谨慎。

---

*最后更新: 2026-07-28*
