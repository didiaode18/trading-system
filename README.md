# 操盘密码 V3.0 — A股中线波段量化交易系统

> 中线波段(3天-4周) · 条件单驱动 · 多因子选股 · 纪律自动化
> 数据源: baostock + akshare | 输出: HTML邮件报告 + 图表

---

## 系统定位

个人量化交易辅助决策系统，核心解决：**管不住手，让情绪毁掉策略**。

- 盘前/盘后自动生成完整交易计划（趋势分析 + 条件单 + 选股推荐）
- 九大分析模块：趋势/DK信号/资金/筹码/板块/多因子/仓位/预警/持仓盈亏
- 美观HTML邮件推送（卡片式布局 + K线图 + 资金流向图）
- 风控硬约束 + 操作纪律锁，彻底消除人为干预

---

## 工程结构

```
trading-system/
├── caopan_report.py              # ★ 操盘密码主程序（报告生成+邮件发送）
├── caopan_runner.py              # 分析引擎运行器（P0-P5模块调用）
├── daily_orders.py               # 每日条件单生成器（盘后独立运行）
├── holdings.json                 # 当前持仓数据（手动维护）
├── run.py                        # 统一CLI入口
├── qmt_trader.py                 # QMT自动交易执行器（可选）
├── auto_scheduler.py             # 自动化调度守护进程
├── requirements.txt              # Python依赖
│
└── trading_system/               # 核心包
    ├── config.py                 # 全局配置（资金/邮箱/股票池/风控参数）
    ├── main.py                   # 主程序（盘后完整分析流程）
    ├── scheduler.py              # 定时任务调度（Windows Task Scheduler）
    │
    ├── data/                     # 数据层
    │   └── data_loader.py        #   历史K线（baostock前复权）
    │
    ├── strategy/                 # 策略层
    │   ├── trend_strategy.py     #   核心趋势策略（自适应通道+5级趋势）
    │   ├── stock_screener.py     #   五层量化选股引擎
    │   ├── multi_timeframe.py    #   多周期共振
    │   ├── sector_rotation.py    #   板块轮动
    │   ├── sector_flow.py        #   板块资金流向
    │   ├── pool_manager.py       #   股票池管理
    │   ├── position.py           #   ATR动态仓位
    │   └── portfolio_analyzer.py #   仓位分析
    │
    ├── risk/                     # 风控层
    │   ├── risk_control.py       #   熔断/仓位/强制卖出
    │   └── position_sizing.py    #   仓位管理（Kelly/风险平价）
    │
    ├── output/                   # 输出层
    │   ├── report_email.py       #   ★ 美观HTML邮件模板
    │   ├── report_charts.py      #   ★ 图表生成（K线/资金/仓位/板块）
    │   ├── eastmoney_orders.py   #   条件单（东方财富格式）
    │   └── reports/              #   报告输出目录
    │
    ├── notify/                   # 通知层
    │   ├── email_notify.py       #   邮件发送（QQ SMTP SSL）
    │   └── wechat_notify.py      #   企业微信（备用）
    │
    └── backtest/                 # 回测层
        └── backtest_engine.py    #   事件驱动回测引擎
```

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
EMAIL_AUTH_CODE = "xxxx"          # SMTP授权码（QQ邮箱设置中生成）
EMAIL_RECEIVER = "your@qq.com"    # 收件邮箱
```

编辑 `holdings.json`（当前持仓，从券商截图更新）：
```json
{
  "588000": {
    "name": "科创50",
    "shares": 318100,
    "buy_price": 1.953,
    "cost": 1.953,
    "highest_price": 1.980,
    "buy_date": "2026-07-10",
    "sector": "指数ETF",
    "stock_type": "弹性"
  }
}
```

---

## 报告体系（4+1）

| 时间 | 报告 | 邮件数 | 内容 |
|------|------|--------|------|
| 08:30 | 盘前作战计划 | 1封 | 市场环境+操作清单+关键价位+仓位建议 |
| 15:30 | 盘后深度复盘 | 1封 | 九大模块全量分析+K线图+资金图 |
| 15:30 | 条件单操作计划 | 1封 | 止损/止盈/时间条件单卡片 |
| 15:30 | 紧急预警 | 0-1封 | 仅有触发时发送 |
| 周六 10:00 | 周策略报告 | 1封 | 本周绩效+板块轮动+仓位再平衡 |

---

## 命令一览

```bash
# 操盘密码报告系统
python caopan_report.py --morning     # 盘前作战计划
python caopan_report.py --evening     # 盘后深度复盘 + 条件单 + 预警
python caopan_report.py --weekly      # 周策略报告
python caopan_report.py --scheduler   # 启动定时调度（常驻）
python caopan_report.py --install     # 安装Windows定时任务

# 条件单独立生成
python daily_orders.py                # 生成次日条件单HTML

# 统一入口
python run.py report                  # 盘后综合分析
python run.py orders                  # 条件单
python run.py auto                    # 全自动调度
```

---

## 定时任务配置

### 方式一：Windows Task Scheduler（推荐）
```bash
python caopan_report.py --install
```
自动创建以下任务：
- `CaopanReport_Morning` — 每日 08:30
- `CaopanReport_Evening` — 每日 15:30
- `CaopanReport_Weekly` — 每周六 10:00

### 方式二：内置调度器
```bash
python caopan_report.py --scheduler
```

### 方式三：auto_scheduler.py 守护进程
```bash
python auto_scheduler.py
```

---

## 持仓更新方法

当持仓发生变化时，需同步更新以下文件：

1. **`holdings.json`** — 主持仓数据（shares/buy_price/cost/sector）
2. **`daily_orders.py`** — `holdings_list` 列表（code/名称/赛道/数量/成本）
3. **`caopan_report.py`** — `NAME_MAP` 字典（代码→名称映射）
4. **`caopan_runner.py`** — `NAME_MAP` 字典（同上）

> 提示：holdings.json 中已包含 name 字段时，NAME_MAP 仅作为备用。

---

## 九大分析模块

| 模块 | 功能 | 核心指标 |
|------|------|----------|
| P0 趋势 | 自适应趋势通道 | ATR动态EMA + 5级趋势强度 |
| P1 DK信号 | 三重共振确认 | 均线交叉+量能+趋势匹配 |
| P2 资金 | 四层交叉验证 | 主力大单+北向+融资+龙虎榜 |
| P3 筹码 | 筹码分布分析 | 获利盘/集中度/控盘度 |
| P4 板块 | 板块轮动监控 | 5日/20日动量+资金流向 |
| P5 多因子 | 综合选股评分 | 技术+资金+动量+基本面 |
| 仓位 | 动态仓位管理 | Kelly公式+风险平价 |
| 预警 | 智能预警系统 | 止损/获利盘骤降/跌停/超卖 |
| 盈亏 | 持仓盈亏追踪 | 浮盈浮亏+市值统计 |

---

## 风控硬约束

- 每日最多交易 **3笔**
- 单只仓位 **≤25%**
- 总仓位 **≤90%**（现金≥10%）
- 单笔风险 **≤总资金2%**
- 单日亏损>5% → **熔断**
- 放量大跌>8% → **无条件离场**

---

## 操作纪律锁（铁律）

1. 禁止盘中手动买卖 — 一切以条件单为准
2. 禁止越跌越补 — 浮亏绝对不加仓
3. 禁止追涨杀跌 — 非信号驱动不操作
4. 止损仅看收盘价 — 盘中跳水不割肉
5. 持仓不超过7只 — 硬性限制

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| 数据源 | baostock(历史K线) + akshare(备用) |
| 数据处理 | pandas / numpy / scipy |
| 图表 | matplotlib (Agg后端, base64嵌入邮件) |
| 通知 | QQ邮箱 SMTP SSL (465端口) |
| 调度 | Windows Task Scheduler / schedule库 |

---

## 版本历史

| 版本 | 核心变更 |
|------|----------|
| V3.0 | 操盘密码报告系统：美观HTML邮件+图表+条件单卡片+拆分多封发送 |
| V7.0 | 五层选股引擎 + QMT自动交易 + 条件单生成器 + 统一CLI |
| V6.0 | 全赛道选股 + 6维度趋势预测 + 定时任务体系 |
| V5.0 | 双买点体系 + 双轨止盈 + 移动止损V3 |

---

## 免责声明

本系统仅供学习研究使用，不构成任何投资建议。股市有风险，投资需谨慎。

---

*最后更新: 2026-07-24*
