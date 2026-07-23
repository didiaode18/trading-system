# 高胜率A股交易操作系统 V7.0

> 中线波段(3天-4周) · 条件单驱动 · 五层选股 · 纪律自动化
> 数据源: baostock + 腾讯行情API | 输出: 邮件报告 + QMT自动执行

---

## 系统定位

个人量化交易辅助决策系统，解决核心痛点：**管不住手，让情绪毁掉策略**。

- 盘后自动生成完整交易计划（条件单 + 技术分析 + 推荐标的）
- 盘中通过QMT自动执行，彻底消除人为干预
- 五层选股引擎确保推荐标的质量（排雷→赛道→基本面→趋势→买点）
- 风控硬约束：每日≤3笔、单只≤25%、现金≥10%、单笔风险≤2%

---

## 工程结构

```
trading-system/
│
├── run.py                        # 统一CLI入口（python run.py <command>）
├── daily_orders.py               # 每日条件单生成器（盘后运行）
├── generate_holdings_report.py   # 盘后综合分析报告（技术面+推荐）
├── qmt_trader.py                 # QMT自动交易执行器（盘中运行）
├── auto_scheduler.py             # 自动化调度守护进程
├── setup.py                      # 环境初始化
├── holdings.json                 # 当前持仓数据（手动维护）
├── requirements.txt              # Python依赖
│
└── trading_system/               # 核心包
    ├── __init__.py               # 包说明 + 版本号
    ├── config.py                 # 全局配置（参数中心）
    ├── main.py                   # 主程序（盘后完整分析流程）
    ├── scheduler.py              # Windows定时任务管理
    │
    ├── data/                     # 数据层
    │   ├── data_loader.py        #   历史K线（baostock前复权）
    │   └── realtime.py           #   实时行情（腾讯API + 东方财富备源）
    │
    ├── strategy/                 # 策略层
    │   ├── trend_strategy.py     #   核心趋势策略（双买点+双轨止盈）
    │   ├── trend_forecast.py     #   持仓趋势预测（6维度评分）
    │   ├── recommend_engine.py   #   ★五层量化推荐引擎
    │   ├── stock_screener.py     #   CANSLIM全赛道选股
    │   ├── multi_timeframe.py    #   多周期共振
    │   ├── sector_rotation.py    #   板块轮动
    │   ├── pool_manager.py       #   股票池管理
    │   ├── position.py           #   ATR动态仓位
    │   ├── portfolio_analyzer.py #   仓位分析
    │   ├── portfolio_risk.py     #   组合风险（相关性/HHI/VaR）
    │   ├── capital_flow.py       #   资金流向（北向/主力）
    │   ├── fundamental.py        #   基本面（PE/ROE/增速）
    │   ├── market_regime.py      #   大盘状态（HMM三状态）
    │   ├── anti_manipulation.py  #   反洗盘保护
    │   ├── consensus.py          #   多空共识信号
    │   ├── signal_decay.py       #   信号衰减模型
    │   ├── meta_label.py         #   元标签（信号质量）
    │   ├── chip_distribution.py  #   筹码分布
    │   ├── event_calendar.py     #   事件日历
    │   ├── news_monitor.py       #   新闻风控
    │   ├── intraday_monitor.py   #   盘中监控
    │   └── trade_journal.py      #   交易日志
    │
    ├── risk/                     # 风控层
    │   └── risk_control.py       #   熔断/仓位/强制卖出/移动止损V3
    │
    ├── output/                   # 输出层
    │   ├── eastmoney_orders.py   #   条件单（东方财富格式）
    │   ├── condition_sheet.py    #   条件单表格
    │   ├── daily_digest.py       #   盘后日报（6大面板）
    │   ├── morning_brief.py      #   晨间简报
    │   └── weekly_review.py      #   周度仓位分析
    │
    ├── notify/                   # 通知层
    │   ├── email_notify.py       #   邮件（QQ SMTP + HTML）
    │   └── wechat_notify.py      #   企业微信（备用）
    │
    ├── backtest/                 # 回测层
    │   ├── engine.py             #   事件驱动回测引擎
    │   ├── broker.py             #   模拟撮合（T+1/滑点/手续费）
    │   ├── metrics.py            #   绩效指标（夏普/Calmar/Sortino）
    │   ├── walk_forward.py       #   滚动窗口防过拟合
    │   ├── monte_carlo.py        #   蒙特卡洛模拟
    │   └── report.py             #   HTML可视化报告
    │
    ├── factors/                  # 因子层
    │   ├── momentum.py           #   动量因子
    │   ├── technical.py          #   技术因子
    │   ├── volume.py             #   量能因子
    │   ├── composite.py          #   复合因子
    │   └── ic_monitor.py         #   IC监控
    │
    ├── position/                 # 仓位层
    │   ├── kelly.py              #   Kelly公式
    │   ├── risk_parity.py        #   风险平价
    │   ├── vol_target.py         #   波动率目标
    │   ├── pyramid.py            #   金字塔加仓
    │   └── black_litterman.py    #   BL模型
    │
    ├── quant/                    # 量化层
    │   ├── engine.py             #   多因子引擎
    │   ├── factors.py            #   因子库
    │   ├── portfolio.py          #   组合优化
    │   ├── risk_manager.py       #   风险管理
    │   └── performance.py        #   绩效归因
    │
    ├── ml/                       # 机器学习
    │   ├── features.py           #   特征工程
    │   ├── predictor.py          #   预测模型
    │   ├── trainer.py            #   训练器
    │   └── monitor.py            #   模型监控
    │
    ├── execution/                # 执行层
    │   ├── twap.py               #   TWAP算法
    │   └── slippage_tracker.py   #   滑点追踪
    │
    ├── monitor/                  # 监控层
    │   └── intraday_monitor.py   #   盘中实时预警
    │
    └── attribution/              # 归因层
        ├── alpha_beta.py         #   Alpha/Beta分解
        ├── barra.py              #   Barra多因子归因
        └── trade_log.py          #   交易记录分析
```

---

## 快速开始

### 环境要求
- Python 3.10+
- Windows（定时任务/QMT）

### 安装
```bash
pip install -r requirements.txt
python setup.py          # 初始化环境
```

### 配置
编辑 `trading_system/config.py`：
```python
TOTAL_CAPITAL = 710_000       # 总资金
EMAIL_SENDER = "your@qq.com"  # QQ邮箱
EMAIL_AUTH_CODE = "xxxx"      # SMTP授权码
```

编辑 `holdings.json`（当前持仓）：
```json
{
  "588000": {"shares": 170100, "buy_price": 1.978, "sector": "指数ETF"},
  "600036": {"shares": 1200, "buy_price": 38.82, "sector": "银行"}
}
```

---

## 命令一览

```bash
python run.py report     # 盘后综合分析报告
python run.py orders     # 生成次日条件单
python run.py trade      # QMT盘中自动交易
python run.py auto       # 全自动调度（常驻）
python run.py status     # 系统状态检查
python run.py backtest   # 策略回测
python run.py screen     # 全赛道选股
```

---

## 每日工作流

| 时间 | 命令 | 输出 |
|------|------|------|
| 15:30 | `python run.py orders` | 次日条件单（邮件+JSON） |
| 15:35 | `python run.py report` | 综合分析报告（技术+推荐） |
| 09:25 | `python run.py trade` | 盘中自动执行条件单 |
| 全天 | `python run.py auto` | 一键全自动（替代上面3个） |

---

## 五层选股引擎

推荐标的必须通过全部五层筛选，输出完整交易计划：

| 层级 | 功能 | 核心指标 |
|------|------|----------|
| L1 排雷 | 一票否决 | ST/流动性<3亿/庄股/暴跌 |
| L2 赛道 | 行业景气 | 60日RPS/均线多头/MA20斜率 |
| L3 基本面 | 安全垫 | ROE≥8%/增速≥20%/估值分位 |
| L4 趋势 | 核心驱动 | MA系统/MACD/RSI/量能结构 |
| L5 买点 | 盈亏比 | 回踩支撑/盈亏比≥2.5:1/仓位 |

输出格式（可直接挂条件单）：
- 买入区间 + 止损价 + 目标位(T1/T2) + 建议仓位 + 风险提示

---

## 条件单体系

| 类型 | 触发规则 | 有效期 |
|------|----------|--------|
| 定价止损 | 最新价×92%（主模式，不依赖成本） | 20天 |
| 时间条件单 | 14:50尾盘+价格≤MA20 | 10天 |
| 回落卖出 | 日高回落5% | 10天 |
| 反弹买入 | 日低反弹2%（MA20支撑确认） | 5天 |

---

## 风控硬约束

- 每日最多交易 **3笔**
- 单只仓位 **≤25%**
- 总仓位 **≤90%**（现金≥10%）
- 单笔风险 **≤总资金2%**
- 单日亏损>5% → **熔断**
- 放量大跌>8% → **无条件离场**

---

## QMT自动交易（可选）

### 开通条件
- 东方财富账户资产≥50万
- 联系客户经理申请QMT权限

### 配置步骤
1. 下载QMT客户端，以"独立交易"模式登录
2. 从QMT安装目录复制 `xtquant` 到 Python `site-packages/`
3. 修改 `qmt_trader.py` 中的 `QMT_CONFIG`
4. 运行 `python run.py trade --test` 验证连接

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| 数据源 | baostock(历史K线) + 腾讯行情API(实时) + akshare(备用) |
| 数据处理 | pandas / numpy / scipy |
| 机器学习 | scikit-learn (HMM/聚类) |
| 交易接口 | xtquant (miniQMT) |
| 通知 | QQ邮箱SMTP / 企业微信 |
| 调度 | Windows Task Scheduler / 内置守护进程 |

---

## 版本历史

| 版本 | 核心变更 |
|------|----------|
| V7.0 | 五层选股引擎 + QMT自动交易 + 条件单生成器 + 统一CLI |
| V6.0 | 全赛道选股 + 6维度趋势预测 + 定时任务体系 |
| V5.0 | 双买点体系 + 双轨止盈 + 移动止损V3 |
| V2.0 | 基础框架（数据+策略+风控+通知） |
# 高胜率A股交易操作系统 V6.0

> 基于「高胜率A股交易操作系统V2.0优化版」交易手册，结合量化回测验证的智能交易辅助系统
> 纯邮件通知 + 手动操作模式，不执行任何自动交易

## 📋 系统概述

本系统是一个**交易辅助决策系统**，核心功能：
- 每日盘后自动分析持仓标的，生成买卖信号
- 持仓趋势预测分析（6维度综合评分 + 最佳操作时间窗口）
- 全赛道选股引擎（CANSLIM多因子 + 行业配额均衡）
- 智能条件单生成（东方财富APP格式，含止损/止盈/买入推荐）
- 风控熔断与仓位控制（满仓禁止加仓/单笔风险≤2%）
- 6个Windows定时任务自动执行，邮件推送所有报告

**重要说明**：本系统不执行自动交易，所有操作需手动在东方财富APP完成。

---

## 🏗️ 系统架构

```
trading-system/
├── holdings.json              # 当前持仓数据（手动维护）
├── requirements.txt           # Python依赖
│
└── trading_system/
    ├── config.py              # 全局配置（参数中心）
    ├── main.py                # 主程序入口（盘后完整分析）
    ├── scheduler.py           # 定时任务调度（6个独立任务）
    ├── backtest_real.py       # 真实环境回测引擎
    │
    ├── strategy/              # 策略模块
    │   ├── trend_strategy.py  # 核心趋势策略（买点1/2 + 卖出体系）
    │   ├── trend_forecast.py  # 持仓趋势预测（6维度评分+时间窗口）
    │   ├── stock_screener.py  # 全赛道选股引擎（CANSLIM+行业配额）
    │   ├── market_regime.py   # 大盘状态识别（HMM三状态模型）
    │   ├── mean_reversion.py  # 均值回归策略（震荡市超跌反弹）
    │   ├── multi_timeframe.py # 多周期共振分析
    │   ├── sector_rotation.py # 板块轮动检测
    │   ├── pool_manager.py    # 股票池管理（核心池/观察池）
    │   ├── position.py        # 仓位计算（ATR动态仓位）
    │   ├── portfolio_analyzer.py  # 仓位分析（资金优化方案）
    │   ├── portfolio_risk.py  # 组合风险管理（相关性/HHI/VaR）
    │   ├── capital_flow.py    # 资金流向分析（北向/主力）
    │   ├── fundamental.py     # 基本面数据（PE/ROE/增速）
    │   ├── market_scanner.py  # 全市场动态扫描
    │   ├── intraday_monitor.py # 盘中实时监控与预警
    │   └── trade_journal.py   # 交易日志与绩效归因
    │
    ├── risk/                  # 风控模块
    │   └── risk_control.py    # 风控检查（熔断/仓位/强制卖出）
    │
    ├── output/                # 输出模块
    │   ├── eastmoney_orders.py # 条件单生成（双轨止盈版）
    │   └── condition_sheet.py  # 条件单表格
    │
    ├── notify/                # 通知模块
    │   ├── email_notify.py    # 邮件推送（QQ邮箱SMTP）
    │   └── wechat_notify.py   # 企业微信推送（备用）
    │
    ├── data/                  # 数据模块
    │   └── data_loader.py     # 数据获取（baostock + akshare）
    │
    └── backtest/              # 回测模块
        └── backtest_engine.py # 回测引擎
```

---

## ⏰ 定时任务（6个独立Windows任务）

| 任务名 | 时间 | 功能 | 命令 |
|--------|------|------|------|
| TradingSystem_MorningReminder | 19:00 | 盘前条件单提醒（前晚发送） | `--run-morning-reminder` |
| TradingSystem_ForecastAM | 08:30 | 盘前趋势预测 | `--run-forecast-am` |
| TradingSystem_Screener | 09:25 | 竞价后选股报告 | `--run-screener` |
| TradingSystem_Daily | 15:30 | 盘后完整分析 | `--run-once` |
| TradingSystem_ForecastPM | 15:35 | 盘后趋势预测 | `--run-forecast-pm` |
| TradingSystem_Weekly | 16:00 | 周五仓位分析 | `--run-weekly` |

```bash
# 安装所有定时任务
python scheduler.py --install

# 卸载所有定时任务
python scheduler.py --uninstall

# 手动运行某个任务
python scheduler.py --run-morning-reminder
python scheduler.py --run-forecast-am
python scheduler.py --run-once
```

---

## 📧 邮件报告列表

| 报告 | 频率 | 内容 |
|------|------|------|
| 条件单邮件 | 每日19:00 | 持仓止损/止盈 + Top5买入推荐 |
| 趋势预测 | 每日08:30 + 15:35 | 持仓6维度分析 + 操作建议 + 时间窗口 |
| 选股报告 | 每日09:25 | CANSLIM入选 + 行业分布 + 买点/止损 |
| 盘后日报 | 每日15:30 | 信号汇总 + 风控面板 + 盈亏统计 |
| 仓位分析 | 每周五16:00 | 资金优化方案 + 风险预警 |

---

## ✨ 核心策略体系

### 1. 双买点体系
| 买点 | 条件 | 质量加分 |
|------|------|----------|
| 买点1 | 缩量回踩MA20（量缩30%+触及支撑） | 基础50分 |
| 买点2 | 放量突破后缩量回踩确认 | +10分 |
| 双买点共振 | 同时满足买点1+买点2 | +15分 |

### 2. 双轨止盈
```
第一轨（阶梯止盈）:
  - 浮盈 8%  → 卖出 1/3 仓位
  - 浮盈 20% → 再卖出 1/3 仓位

第二轨（回落止盈）:
  - 剩余 1/3 底仓 → 高点回落 5%(龙头)/4%(赛道)/3%(弹性) 卖出
```

### 3. 移动止损 V3.0
| 浮盈区间 | 止损位置 |
|----------|----------|
| < 5% | 初始止损（成本-8%） |
| 5% - 15% | 保本+2%（覆盖手续费） |
| 15% - 30% | 锁定盈利12% |
| > 30% | 锁定盈利22% |

### 4. 趋势预测（6维度综合评分）
- 趋势方向（40%）：MA排列 + MACD + 线性回归斜率
- 动量状态（25%）：RSI + KDJ + 成交量趋势
- 量价关系（20%）：量价配合度 + 资金流向
- 位置分析（15%）：布林带位置 + 支撑压力位

### 5. 选股引擎（全赛道版）
- 硬性过滤：趋势/流动性/振幅/暴跌/下降通道
- CANSLIM多因子打分（0-100分）
- 行业配额均衡（动态调整）
- 弱势行情自动切换观察模式

### 6. 强制卖出
- 单日放量大跌 > 8% 且量 > 均量2倍 → **无条件离场**

---

## 🚀 快速开始

### 环境要求
- Python 3.10+
- Windows（定时任务依赖schtasks）

### 安装
```bash
pip install -r requirements.txt
```

### 配置
编辑 `trading_system/config.py`：
```python
# 资金配置
TOTAL_CAPITAL = 726_245.36
AVAILABLE_CASH = 76_074.26

# 邮箱配置（QQ邮箱SMTP）
EMAIL_SENDER = "your_email@qq.com"
EMAIL_AUTH_CODE = "your_auth_code"
EMAIL_RECEIVER = "your_email@qq.com"

# 股票池（每周日更新）
STOCK_POOL = {
    "002371": {"名称": "北方华创", "赛道": "半导体设备", "类型": "龙头"},
    # ...
}
```

编辑 `holdings.json`（当前持仓）：
```json
{
    "600584": {"shares": 1600, "buy_price": 92.589, "sector": "半导体封测", "stock_type": "龙头"},
    "600036": {"shares": 1700, "buy_price": 38.700, "sector": "银行", "stock_type": "龙头"}
}
```

### 运行
```bash
cd trading_system

# 安装定时任务（推荐，自动执行所有报告）
python scheduler.py --install

# 或手动运行
python main.py                    # 盘后完整分析
python scheduler.py --run-morning-reminder  # 条件单邮件
python scheduler.py --run-forecast-am       # 趋势预测
```

---

## ⚠️ 交易铁则

1. **浮亏绝对不加仓** - 代码层面已禁止
2. **持仓不超过7只** - 硬性限制
3. **单笔亏损≤2%总资金** - 风控检查
4. **时间红线** - 9:30-10:00 / 14:30-15:00 禁止开仓
5. **止损仅看收盘价** - 盘中跳水不割肉
6. **满仓禁止买入** - 仓位≥90%绝对禁止，≥80%禁止新开仓

---

## 📊 回测验证（2022-2025，20只股票）

| 指标 | V2.0原版 | V5.0优化版 | 改善 |
|------|----------|------------|------|
| 胜率 | 33.8% | **61.9%** | +28.1% |
| 每笔期望 | -0.88% | **+1.97%** | +2.85% |
| 最大连亏 | 15次 | 11次 | -4次 |

> 回测环境：手续费0.3% + 滑点0.2%/0.5% + T+1 + 涨跌停过滤（无未来函数）

---

## 📝 更新日志

### V6.0 (2026-07-20)
- 新增持仓趋势预测模块（6维度评分 + 最佳操作时间窗口）
- 新增6个独立Windows定时任务（盘前/盘后/选股/条件单/仓位）
- 条件单邮件升级：持仓止损止盈 + Top5买入推荐
- 全赛道选股引擎（8大行业候选池 + 动态配额）
- 移除自动交易模块（纯邮件通知 + 手动操作）
- 盘前条件单改到19:00发送（方便提前挂单）

### V5.0
- 双买点体系（缩量回踩 + 突破回踩确认）
- 硬性过滤（流动性/振幅/暴跌）
- 信号质量评分（0-100分）
- 双轨止盈（阶梯+回落）
- 强制卖出（放量大跌>8%）
- 移动止损V3.0（保本+2%）

### V4.0
- 真实环境回测引擎
- 双重趋势确认（MA20+MA60）

### V2.0
- 基础趋势策略
- 条件单生成
- 邮件推送

---

## ⚖️ 免责声明

本系统仅供学习研究使用，不构成任何投资建议。股市有风险，投资需谨慎。

---

*最后更新: 2026-07-20*
# 高胜率A股交易操作系统 V5.0

> 基于「高胜率A股交易操作系统V2.0优化版」交易手册，结合量化回测验证的智能交易辅助系统

## 📋 系统概述

本系统是一个**交易辅助决策系统**，核心功能包括：
- 每日盘后自动分析持仓标的，生成买卖信号
- 智能条件单生成（东方财富APP格式）
- 股票池动态管理（核心池+观察池）
- 风控熔断与仓位控制
- 邮件自动推送日报/条件单

**重要说明**：本系统不执行自动交易，所有操作需手动在东方财富APP完成。

---

## 🏗️ 系统架构

```
trading_system/
├── config.py              # 全局配置（参数中心）
├── main.py                # 主程序入口
├── scheduler.py           # 定时任务调度
├── backtest_real.py       # V5.0真实环境回测引擎
│
├── strategy/              # 策略模块
│   ├── trend_strategy.py  # 核心趋势策略（买点1/2 + 卖出体系）
│   ├── stock_screener.py  # 选股引擎（硬性过滤 + CANSLIM打分）
│   ├── pool_manager.py    # 股票池管理（核心池/观察池）
│   ├── position.py        # 仓位计算
│   └── ...
│
├── risk/                  # 风控模块
│   └── risk_control.py    # 风控检查（熔断/仓位/强制卖出）
│
├── output/                # 输出模块
│   ├── eastmoney_orders.py # 条件单生成（双轨止盈版）
│   └── condition_sheet.py  # 条件单表格
│
├── notify/                # 通知模块
│   └── email_notify.py    # 邮件推送（综合日报）
│
└── data/                  # 数据模块
    └── data_loader.py     # 数据获取（baostock）
```

---

## ✨ V5.0 核心优化

### 1. 双买点体系
| 买点 | 条件 | 质量加分 |
|------|------|----------|
| 买点1 | 缩量回踩MA20（量缩30%+触及支撑） | 基础50分 |
| 买点2 | 放量突破后缩量回踩确认 | +10分 |
| 双买点共振 | 同时满足买点1+买点2 | +15分 |

### 2. 硬性过滤（6个标准）
- ✅ 趋势合格：MA20向上 + 收盘价站稳
- ✅ 流动性：日均成交额 ≥ 8亿
- ✅ 股性稳定：近30日振幅>10%天数 ≤ 3天
- ✅ 无放量暴跌：近5日无单日跌幅>8%且放量
- ✅ 非下降通道：MA20/MA60不能全部向下
- ✅ 回调不创新低：近10日低点不破前波低点

### 3. 双轨止盈
```
第一轨（阶梯止盈）:
  - 浮盈 8%  → 卖出 1/3 仓位
  - 浮盈 20% → 再卖出 1/3 仓位

第二轨（回落止盈）:
  - 剩余 1/3 底仓 → 高点回落 5%(龙头)/4%(赛道)/3%(弹性) 卖出
```

### 4. 移动止损 V3.0
| 浮盈区间 | 止损位置 |
|----------|----------|
| < 5% | 初始止损（成本-8%） |
| 5% - 15% | 保本+2%（覆盖手续费） |
| 15% - 30% | 锁定盈利12% |
| > 30% | 锁定盈利22% |

### 5. 强制卖出
- 单日放量大跌 > 8% 且量 > 均量2倍 → **无条件离场**

---

## 📊 回测验证（2022-2025，20只股票）

| 指标 | V2.0原版 | V5.0优化版 | 改善 |
|------|----------|------------|------|
| 胜率 | 33.8% | **61.9%** | +28.1% |
| 每笔期望 | -0.88% | **+1.97%** | +2.85% |
| 最大连亏 | 15次 | 11次 | -4次 |

> 回测环境：手续费0.3% + 滑点0.2%/0.5% + T+1 + 涨跌停过滤（无未来函数）

---

## 🚀 快速开始

### 环境要求
- Python 3.10+
- 依赖包：`pip install -r requirements.txt`

### 配置
编辑 `config.py`：
```python
# 股票池（每周日更新）
STOCK_POOL = {
    "002371": {"名称": "北方华创", "赛道": "半导体设备", "类型": "龙头"},
    # ...
}

# 邮箱配置（QQ邮箱SMTP）
EMAIL_SENDER = "your_email@qq.com"
EMAIL_AUTH_CODE = "your_auth_code"
```

### 运行
```bash
# 每日盘后分析（15:30后）
python main.py

# 运行回测
python backtest_real.py

# 定时任务（自动调度）
python scheduler.py
```

---

## 📧 邮件报告

### 条件单邮件（每日盘后）
- 止损条件单（仅收盘价触发）
- 阶梯止盈条件单（8%/20%）
- 回落止盈条件单（底仓保护）
- 时间红线标注

### 综合日报（V3.0）
- 大盘状态面板
- 持仓盈亏面板
- 今日信号面板（含质量评分）
- 风控仪表盘
- 周度统计（周五）

---

## ⚠️ 交易铁则

1. **浮亏绝对不加仓** - 代码层面已禁止
2. **持仓不超过7只** - 硬性限制
3. **单笔亏损≤2%总资金** - 风控检查
4. **时间红线** - 9:30-10:00 / 14:30-15:00 禁止开仓
5. **止损仅看收盘价** - 盘中跳水不割肉

---

## 📁 文件说明

| 文件 | 说明 |
|------|------|
| `config.py` | 所有可调参数集中管理 |
| `backtest_real.py` | V5.0回测引擎（含V2/V4/V5对比） |
| `strategy/trend_strategy.py` | 核心买卖信号逻辑 |
| `strategy/stock_screener.py` | 选股引擎（硬性过滤+CANSLIM） |
| `strategy/pool_manager.py` | 股票池动态管理 |
| `output/eastmoney_orders.py` | 条件单生成（双轨止盈版） |
| `notify/email_notify.py` | 邮件推送（综合日报） |

---

## 📝 更新日志

### V5.0 (2026-07-20)
- 新增买点2（放量突破回踩确认）
- 新增硬性过滤（流动性/振幅/暴跌）
- 新增信号质量评分（0-100分）
- 升级双轨止盈（阶梯+回落）
- 新增强制卖出（放量大跌>8%）
- 升级移动止损V3.0（保本+2%）
- 新增股票池管理（核心池+观察池）
- 升级综合日报（6大面板）

### V4.0
- 真实环境回测引擎
- 双重趋势确认（MA20+MA60）

### V2.0
- 基础趋势策略
- 条件单生成
- 邮件推送

---

## ⚖️ 免责声明

本系统仅供学习研究使用，不构成任何投资建议。股市有风险，投资需谨慎。

---

*最后更新: 2026-07-20*
