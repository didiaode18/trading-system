# 高胜率A股交易操作系统 V2.0

基于「高胜率A股交易操作系统V2.0」规则构建的量化交易系统，实现从选股、信号扫描、风控校验到条件单输出的全流程自动化。

## 系统特性

- **全市场选股**：两阶段筛选（预筛选+详细分析），从4992只A股中筛选符合V2.0规则的标的
- **趋势策略**：20日/60日均线趋势判定、缩量回踩买点、移动止损、双轨止盈
- **风控熔断**：单笔亏损<=2%、日度/周度熔断、仓位上限、现金安全垫
- **自动输出**：条件单Excel、文本报告、钉钉/企微通知
- **数据管理**：baostock主数据源 + akshare备用，SQLite本地日线数据库

## 项目结构

```
chat-1/
├── trading_system/              # 核心系统模块
│   ├── __init__.py              # 包初始化
│   ├── config.py                # 全局配置（股票池、参数、风控阈值）
│   ├── main.py                  # 完整流程入口
│   ├── data/
│   │   ├── __init__.py
│   │   ├── data_loader.py       # 数据获取与增量更新
│   │   └── stock_db.db          # SQLite日线数据库（自动生成）
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── trend_strategy.py    # 核心趋势策略函数
│   │   └── position.py          # 仓位计算
│   ├── risk/
│   │   ├── __init__.py
│   │   └── risk_control.py      # 风控熔断校验
│   ├── notify/
│   │   ├── __init__.py
│   │   └── wechat_notify.py     # 钉钉/企微通知
│   └── output/
│       ├── __init__.py
│       └── condition_sheet.py   # 条件单Excel生成
├── stock_screener.py            # 全市场选股程序
├── 中线趋势交易信号.py          # 信号扫描入口
├── 持仓信号监控.py              # 持仓监控入口
├── holdings.json                # 持仓数据（需手动维护）
├── requirements.txt             # Python依赖
├── .gitignore                   # Git忽略规则
└── README.md                    # 本文件
```

## 快速开始

### 1. 环境要求

- Python 3.8+
- Windows / macOS / Linux

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

依赖说明：
- `baostock`: 主数据源（证券宝，免费稳定）
- `akshare`: 备用数据源（东方财富）
- `pandas`: 数据处理
- `numpy`: 数值计算
- `openpyxl`: Excel条件单生成

### 3. 配置系统

编辑 `trading_system/config.py`：

```python
# 总资金
TOTAL_CAPITAL = 759_965.54

# 股票池
STOCK_POOL = {
    "002371": {"名称": "北方华创", "赛道": "半导体设备", "类型": "龙头"},
    "002409": {"名称": "雅克科技", "赛道": "半导体材料", "类型": "龙头"},
    # ... 更多股票
}

# 风控参数
MAX_SINGLE_LOSS_RATIO = 0.02     # 单笔最大亏损2%
DAILY_LOSS_LIMIT_1 = 0.02        # 日度熔断L1
WEEKLY_LOSS_LIMIT = 0.08         # 周度熔断
```

### 4. 维护持仓

编辑 `holdings.json`：

```json
{
  "002371": {
    "shares": 300,
    "buy_price": 774.574,
    "highest_price": 774.574,
    "first_batch_done": true,
    "sector": "半导体设备",
    "stock_type": "龙头",
    "current_price": 677.0
  }
}
```

### 5. 运行系统

#### 全市场选股（推荐先运行）

```bash
# 全市场扫描，输出前20只
python stock_screener.py --top 20

# 按赛道关键词筛选
python stock_screener.py --sector 银行 --top 10

# 调整最低成交额门槛（默认8亿）
python stock_screener.py --amount 5 --top 30
```

#### 信号扫描

```bash
# 更新数据并扫描
python 中线趋势交易信号.py

# 使用已有数据（跳过更新）
python 中线趋势交易信号.py --no-update
```

#### 持仓监控

```bash
# 更新数据并监控
python 持仓信号监控.py

# 使用已有数据
python 持仓信号监控.py --no-update
```

#### 完整流程

```bash
# 完整运行（数据更新+信号生成+条件单Excel+通知）
python trading_system/main.py

# 跳过数据更新
python trading_system/main.py --no-update

# 仅输出文本报告
python trading_system/main.py --report
```

## 核心规则说明

### 选股规则（V2.0）

| 规则 | 说明 |
|------|------|
| 流动性 | 日均成交额 >= 8亿 |
| 趋势 | 股价站稳MA20，MA20向上 |
| 中期趋势 | 股价在MA60上方 |
| 股性稳定 | 近30日单日振幅>10%天数 <= 3天 |
| 排除 | ST股、北交所、退市股 |

### 交易规则

| 规则 | 说明 |
|------|------|
| 买点 | 缩量回踩20日线（量缩30%+，最低价触及±1%） |
| 止损 | 买入价下方10%，仅收盘价触发 |
| 移动止损 | 浮盈5%->保本，15%->盈利10%，30%->盈利20% |
| 止盈 | 双轨制：阶梯目标（8%/20%）+ 回落止盈 |
| 仓位 | 分批建仓：40%试仓 + 60%加仓 |

### 风控规则

| 规则 | 说明 |
|------|------|
| 单笔亏损 | <= 总资金2% |
| 日度熔断 | 亏损>=2%停止买入，>=3%清弱势仓 |
| 周度熔断 | 亏损>=8%全仓降至3成，休息1周 |
| 仓位上限 | 龙头15%、弹性8%、赛道40% |
| 现金安全垫 | >= 10% |

## 配置通知

编辑 `trading_system/config.py`：

```python
# 钉钉机器人
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=xxx"

# 企业微信机器人
WECHAT_WORK_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
```

## 数据源说明

| 数据源 | 说明 | 状态 |
|--------|------|------|
| baostock | 证券宝，免费稳定，支持全历史日线 | 主数据源 |
| akshare | 东方财富，数据丰富但可能限流 | 备用数据源 |

系统自动在数据源间切换，优先使用baostock，失败后尝试akshare。

## 输出文件

- `trading_system/output/条件单_YYYY-MM-DD.xlsx`: 每日条件单Excel
- `trading_system/output/选股结果_YYYYMMDD.csv`: 选股结果CSV
- `trading_system/logs/trading_YYYYMMDD.log`: 运行日志

## 常见问题

### Q: 数据获取失败怎么办？

A: 系统会自动切换到备用数据源。如果两个数据源都失败，检查网络连接。

### Q: 如何更新股票池？

A: 编辑 `trading_system/config.py` 中的 `STOCK_POOL` 字典，每周日更新，盘中不临时新增。

### Q: 如何修改持仓数据？

A: 编辑 `holdings.json`，系统会在下次运行时自动加载。

### Q: 选股程序运行太慢？

A: 使用 `--sector` 参数按赛道筛选，或 `--top` 限制输出数量。全市场扫描约需22分钟。

## 版本历史

- **V2.0** (2026-07): 初始版本，完整实现V2.0规则
  - 全市场选股程序（两阶段筛选）
  - 趋势策略、仓位计算、风控熔断
  - baostock + akshare 双数据源
  - 条件单Excel、钉钉/企微通知

## 许可证

本项目仅供学习研究使用，不构成任何投资建议。股市有风险，投资需谨慎。
