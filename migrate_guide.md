# 操盘密码交易系统 — 完整迁移指南

> 版本: V9.0 | 最后更新: 2026-08-16 | 适用场景: 全新部署 / 跨机器迁移 / 环境重建
>
> 配套工具: `migrate.bat`（Windows 一键初始化批处理，自动执行步骤 2~4）

---

## 目录

1. [目标环境准备](#1-目标环境准备)
2. [代码获取与目录结构](#2-代码获取与目录结构)
3. [依赖安装](#3-依赖安装)
4. [配置文件迁移](#4-配置文件迁移)
5. [数据与数据库迁移](#5-数据与数据库迁移)
6. [定时任务配置](#6-定时任务配置)
7. [验证清单](#7-验证清单)
8. [常见问题排查](#8-常见问题排查)
9. [附录：文件关系与兼容性说明](#9-附录文件关系与兼容性说明)

---

## 1. 目标环境准备

### 1.1 系统要求

| 项目 | 要求 | 说明 |
|------|------|------|
| 操作系统 | Windows 10/11（64位） | 主要开发平台；Linux 可运行核心模块（无 GUI 依赖时） |
| Python | **3.9 ~ 3.11**（推荐 3.10.x） | `setup_check.py` 会检测版本，不在范围内将报错 |
| 磁盘空间 | ≥ 2 GB | 数据库 + 依赖包 + 日志/输出文件 |
| 网络 | 可访问 baostock / 东方财富 API | 首次拉取历史数据约需 3~5 分钟 |
| 内存 | ≥ 4 GB | 回测/ML 模块在大数据量时消耗较高 |

### 1.2 Python 安装注意事项

- Windows 安装时务必勾选 **"Add Python to PATH"**
- 推荐使用 [Python 官方安装包](https://www.python.org/downloads/)，避免使用 Anaconda（路径冲突风险）
- 安装完成后验证：
  ```powershell
  python --version     # 应输出 Python 3.10.x
  pip --version        # 应输出 pip 2x.x
  ```

### 1.3 虚拟环境（可选但推荐）

```powershell
cd <项目根目录>
python -m venv venv
.\venv\Scripts\Activate.ps1    # 后续所有 pip / python 命令在此环境中执行
```

---

## 2. 代码获取与目录结构

### 2.1 获取代码

**方式 A — Git 克隆（推荐）**
```powershell
git clone <仓库地址> trading-system
cd trading-system
```

**方式 B — 整包拷贝**
直接将项目文件夹复制到目标机器，保留完整目录结构。

### 2.2 目录结构总览

```
trading-system/                   # 项目根目录
├── trading_system/               # 核心代码包
│   ├── config.py                 # 公共默认配置（随仓库版本更新）
│   ├── config_local.py           # ★ 私有配置（需手动创建，.gitignore 排除）
│   ├── config_local.example.py   # 私有配置模板
│   ├── holdings.json             # ★ 当前持仓（需手动填写）
│   ├── main.py                   # 核心分析入口
│   ├── scheduler.py              # 定时调度器
│   ├── data/                     # 数据层
│   │   ├── data_loader.py        # 数据加载（含 LRU 缓存）
│   │   ├── realtime.py           # 实时行情
│   │   ├── stock_db.db           # SQLite K线数据库（自动重建）
│   │   └── trade_journal.db      # 交易日志（★ 必须迁移，不可重建）
│   ├── strategy/                 # 策略引擎
│   ├── risk/                     # 风控模块（17 道关卡）
│   ├── backtest/                 # 回测引擎
│   ├── ml/                       # 机器学习模块
│   ├── notify/                   # 通知模块（邮件/钉钉/企微）
│   ├── factors/                  # 因子计算
│   ├── position/                 # 仓位管理
│   ├── execution/                # 执行归因
│   ├── attribution/              # 绩效归因
│   ├── dashboard/                # Streamlit 仪表盘（可选）
│   ├── logs/                     # 运行日志（自动生成）
│   └── output/                   # 报告输出（自动生成）
├── run.py                        # 统一命令行入口
├── setup.py                      # 初始化向导脚本
├── setup_check.py                # 环境自检脚本
├── requirements.txt              # 依赖清单
├── migrate.bat                   # Windows 一键初始化批处理
├── scheduler_daemon.bat          # 调度器看门狗批处理
├── .env                          # 环境变量配置（可选，.gitignore 排除）
└── docs/                         # 知识库文档
```

### 2.3 不随 Git 迁移的文件（需手动拷贝）

以下文件被 `.gitignore` 排除，迁移时须**手动携带**：

| 文件 | 重要性 | 说明 |
|------|--------|------|
| `trading_system/config_local.py` | **必须** | 邮箱授权码等敏感配置 |
| `trading_system/holdings.json` | **必须** | 当前持仓数据 |
| `holdings.json`（根目录） | **必须** | 持仓副本（双份同步机制） |
| `trading_system/data/trade_journal.db` | **必须** | 历史交易绩效，不可重建 |
| `trading_system/data/stock_db.db` | 建议携带 | 不带也可，`setup.py` 会自动重建（约 3~5 分钟） |
| `.env` | 可选 | 如使用环境变量方案 |
| `trading_system/output/risk_state.json` | 建议携带 | 风控熔断状态 |

---

## 3. 依赖安装

### 3.1 核心依赖（必须）

```powershell
cd <项目根目录>
pip install -r requirements.txt
```

核心依赖清单：

| 分类 | 包名 | 用途 |
|------|------|------|
| 数据源 | `baostock` ≥ 0.8.8 | 主数据源：证券宝（免费 A 股日线/基本面） |
| 数据源 | `akshare` ≥ 1.10.0 | 备用数据源：东方财富（实时行情/资金流） |
| 数据处理 | `pandas`, `numpy`, `scipy` | 数据处理与统计计算 |
| 机器学习 | `scikit-learn`, `joblib` | HMM 市场状态检测 / 模型持久化 |
| 输出 | `openpyxl`, `schedule` | Excel 条件单生成 / 定时任务调度 |
| 可视化 | `matplotlib`, `pyecharts`, `tqdm` | 图表绘制 / 交互式图表 / 进度条 |

### 3.2 可选依赖（按需安装）

```powershell
# ML 预测增强
pip install xgboost lightgbm

# Windows 桌面弹窗通知
pip install win10toast

# 可视化仪表盘
pip install streamlit

# 开发测试
pip install pytest
```

> 可选依赖缺失时系统会自动降级运行，不影响核心功能。

### 3.3 自动交易接口（需开通 QMT 后安装）

`xtquant`（miniQMT 交易接口）不在 PyPI 上，需从券商 QMT 安装目录手动复制。未开通 QMT 无需处理。

### 3.4 国内镜像加速（可选）

```powershell
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 4. 配置文件迁移

V9.0 采用**三层配置架构**，优先级从高到低：

```
config_local.py（私有覆盖） > .env 环境变量 > config.py（公共默认值）
```

### 4.1 创建 config_local.py（推荐方案）

**步骤 1** — 从模板复制：
```powershell
copy trading_system\config_local.example.py trading_system\config_local.py
```

**步骤 2** — 编辑 `trading_system/config_local.py`，填写以下必填项（★ 标记）：

```python
# ★ 必填：账户资金（登录券商 APP → 资产查询）
TOTAL_CAPITAL = 500000       # 总资金（元）
AVAILABLE_CASH = 100000      # 可用资金（元）

# ★ 必填：核心股票池
STOCK_POOL = {
    "600519": {"名称": "贵州茅台", "赛道": "白酒消费", "类型": "龙头"},
    # 按需添加...
}

# ★ 必填：QQ 邮箱 SMTP 授权码（16 位，非 QQ 密码）
# 获取方式：QQ 邮箱 → 设置 → 账户 → POP3/SMTP 服务 → 生成授权码
EMAIL_AUTH_CODE = "abcdefghijklmnop"

# ★ 必填：发件人 / 收件人邮箱
EMAIL_SENDER = "your_qq@qq.com"
EMAIL_RECEIVER = "your_qq@qq.com"

# 可选：钉钉/企微机器人 Webhook
# DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=xxx"
# WECHAT_WORK_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
```

> `config_local.py` 已加入 `.gitignore`，不会被提交到仓库，可安全存放敏感信息。

**快捷方式** — 运行 `setup.py` 时会自动检测 `config_local.py` 是否存在，若不存在则从模板复制并启动交互式填写向导。

### 4.2 使用 .env 环境变量（替代方案）

如果不使用 `config_local.py`，也可通过 `.env` 文件配置：

```powershell
copy .env.example .env
```

编辑 `.env` 文件：
```
EMAIL_AUTH_CODE=你的16位授权码
EMAIL_SENDER=your_qq@qq.com
EMAIL_RECEIVER=your_qq@qq.com
```

> `.env` 同样已加入 `.gitignore`。`config.py` 启动时自动加载。

### 4.3 填写持仓文件

编辑 `trading_system/holdings.json`（同时同步到根目录 `holdings.json`）：

```json
{
  "600519": {
    "name": "贵州茅台",
    "shares": 100,
    "buy_price": 1800.00,
    "current_price": 1850.00,
    "sector": "白酒消费",
    "stock_type": "龙头",
    "stop_loss": 1620.00,
    "trailing_stop": 1620.00,
    "buy_date": "2026-08-16",
    "market_value": 185000.00,
    "pnl": 5000.00,
    "pnl_pct": 2.78
  }
}
```

**字段说明**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 股票名称 |
| `shares` | 是 | 持仓股数 |
| `buy_price` | 是 | 买入均价 |
| `current_price` | 是 | 当前价格 |
| `sector` | 是 | 所属赛道 |
| `stock_type` | 是 | `"龙头"` 或 `"弹性"`（影响仓位上限） |
| `stop_loss` | 是 | 止损价 |
| `trailing_stop` | 是 | 移动止损价 |
| `buy_date` | 否 | 买入日期 |
| `market_value` | 否 | 市值（系统会自动更新） |
| `pnl` / `pnl_pct` | 否 | 盈亏额/盈亏比（系统自动计算） |

### 4.4 配置优先级总结

```
┌──────────────────────────────────────────────────────────────┐
│  优先级从高到低                                               │
│                                                              │
│  ① config_local.py  → 私有覆盖（敏感信息、个人参数）          │
│  ② .env             → 环境变量（轻量配置）                    │
│  ③ config.py        → 公共默认值（随版本更新，勿改敏感信息）  │
└──────────────────────────────────────────────────────────────┘
```

任何一层未设置某项，系统会自动回退到下一层的默认值。全部未设置时，邮件通知不可用，其余功能正常运行。

---

## 5. 数据与数据库迁移

### 5.1 数据库文件说明

| 文件 | 路径 | 是否必须迁移 | 可自动重建 |
|------|------|-------------|-----------|
| `stock_db.db` | `trading_system/data/` | 建议携带 | ✓ 运行 `setup.py` 自动拉取 |
| `trade_journal.db` | `trading_system/data/` | **必须携带** | ✗ 不可重建 |

- **`stock_db.db`**：SQLite 格式，存储所有候选股历史 K 线。不带的话首次运行 `setup.py` 会自动重建，约 3~5 分钟。
- **`trade_journal.db`**：记录历史交易绩效、成交明细。**无法自动重建**，务必从旧机器拷贝。

### 5.2 迁移方式

直接拷贝 `.db` 文件到目标机器的对应目录即可。所有数据库路径均为**项目相对路径**，随文件夹拷贝即可用，无需修改任何配置。

```powershell
# 示例：从 U 盘恢复
copy E:\backup\stock_db.db trading_system\data\stock_db.db
copy E:\backup\trade_journal.db trading_system\data\trade_journal.db
```

### 5.3 缓存文件

以下缓存文件可不携带，系统运行后会自动重建：

- `trading_system/data/*_cache.json`（各类缓存）
- `trading_system/data/*.pkl`（pickle 缓存）
- `trading_system/data/ic_history.json`
- `trading_system/data/slippage_history.json`
- `trading_system/data/vol_scale_state.json`

### 5.4 V9.0 数据层优化说明

V9.0 对数据加载层进行了性能优化，迁移时需注意：

- **LRU 缓存**：`data_loader.py` 新增了进程内 LRU 缓存（`functools.lru_cache`），以 DB 文件修改时间作为失效触发器。迁移后首次启动会自动加载缓存，无需额外操作。
- **报告数据源**：`generate_holdings_report.py` 优先从本地 SQLite 读取数据，仅在本地数据不足时降级到 baostock 直连。确保 `stock_db.db` 已携带或已通过 `setup.py` 初始化。

---

## 6. 定时任务配置

### 6.1 Windows 任务计划程序

旧机器的任务计划不会随文件迁移，需要在新机器重新注册。以管理员身份打开 PowerShell：

#### 方式 A — 一键自动化调度（推荐）

```powershell
$root = "<项目根目录绝对路径>"
schtasks /Create /TN "TradingSystem_Auto" /SC ONSTART /TR "python $root\run.py auto"
```

> `run.py auto` 启动自动化调度守护进程，自动覆盖盘后报告 + 盘中执行全部任务。

#### 方式 B — 分任务注册

```powershell
$root = "<项目根目录绝对路径>"

# 盘后条件单（每个交易日 15:30）
schtasks /Create /TN "TradingSystem_Orders" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:30 /TR "python $root\run.py orders"

# 盘后分析报告（每个交易日 15:35）
schtasks /Create /TN "TradingSystem_Report" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:35 /TR "python $root\run.py report"

# 盘中监控（每个交易日 09:15）
schtasks /Create /TN "TradingSystem_Intraday" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:15 /TR "python -m trading_system.main --monitor"
```

#### 方式 C — 调度器看门狗

使用 `scheduler_daemon.bat` 配合任务计划实现心跳监控：

```powershell
# 每 5 分钟检查一次调度器心跳，失联时自动重启
schtasks /Create /TN "TradingSystem_SchedulerDaemon" /SC MINUTE /MO 5 /TR "$root\scheduler_daemon.bat"
```

#### 管理命令

```powershell
schtasks /Query /TN "TradingSystem_Auto" /V /FO LIST   # 查看任务详情
schtasks /Change /TN "TradingSystem_Auto" /DISABLE      # 禁用任务
schtasks /Delete /TN "TradingSystem_Auto" /F             # 删除任务
```

### 6.2 Linux crontab（可选）

如果在 Linux 服务器运行（无 GUI 依赖时可用）：

```bash
# 编辑 crontab
crontab -e

# 添加以下条目（假设项目路径为 /opt/trading-system）
ROOT=/opt/trading-system
PYTHON=/usr/bin/python3

# 盘后条件单（周一~五 15:30）
30 15 * * 1-5 $PYTHON $ROOT/run.py orders >> $ROOT/trading_system/logs/cron.log 2>&1

# 盘后分析报告（周一~五 15:35）
35 15 * * 1-5 $PYTHON $ROOT/run.py report >> $ROOT/trading_system/logs/cron.log 2>&1

# 盘中监控（周一~五 09:15）
15 9 * * 1-5 $PYTHON -m trading_system.main --monitor >> $ROOT/trading_system/logs/cron.log 2>&1
```

---

## 7. 验证清单

按顺序执行以下检查，全部通过即迁移成功。

### 7.1 自动自检

```powershell
python setup_check.py          # 完整自检（不发测试邮件）
python setup_check.py --email  # 可选：实测 SMTP 授权码是否有效
python setup_check.py --strict # 严格模式：任何警告也视为失败
```

自检覆盖 8 大维度：
1. Python 版本（3.9 ~ 3.11）
2. 核心依赖包（10 个必须 + 5 个可选）
3. 配置文件（config.py / config_local.py / .env）
4. 目录结构（data / logs / output / strategy / risk / notify）
5. 数据库（stock_db.db 可读性 / trade_journal.db）
6. 持仓文件（holdings.json 格式与内容）
7. 邮箱配置（SMTP 参数 / 可选登录测试）
8. 包导入（trading_system / 风控引擎 / 核心策略）

### 7.2 手动验证

```powershell
# ① 包导入正常
python -c "import trading_system"

# ② 配置加载正常，授权码已设置
python -c "from trading_system import config; print(config.EMAIL_AUTH_CODE != '')"
# 应输出 True

# ③ 持仓文件双份一致
python -c "import json; a=json.load(open('holdings.json')); b=json.load(open('trading_system/holdings.json')); print('一致' if a==b else '不一致')"

# ④ 系统状态查看
python run.py status

# ⑤ 盘后分析可正常运行（不实际发送，仅验证流程）
python run.py report
```

### 7.3 完整通过标准

- [ ] `python setup_check.py` 无 ✗ 项
- [ ] `python -c "import trading_system"` 无报错
- [ ] `config_local.py` 中 `EMAIL_AUTH_CODE` 已配置
- [ ] `holdings.json` 双份内容一致
- [ ] `python run.py status` 可正常运行
- [ ] （可选）`python setup_check.py --email` SMTP 登录成功

---

## 8. 常见问题排查

### 8.1 网络代理问题

**现象**：`baostock.login()` 超时或连接失败。

**排查**：
```powershell
# 检查是否能直连数据源
python -c "import baostock as bs; lg = bs.login(); print(lg.error_code, lg.error_msg)"
```

**解决**：
- 如使用公司代理，确保代理允许 `baostock` 和东方财富 API 的出站连接
- 临时关闭代理测试：`$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''`
- baostock 使用 TCP 长连接，某些代理会断开长连接，建议对 `*.baostock.com` 加白名单

### 8.2 数据源限流

**现象**：批量拉取数据时部分股票返回空或超时。

**解决**：
- 系统已内置请求间隔（`data_loader.py` 自动限流），正常情况下不会触发
- 如仍遇到限流，可在 `setup.py` 拉取完成后手动重试失败个股：
  ```python
  from trading_system.data.data_loader import batch_update_all, init_db
  conn = init_db()
  batch_update_all(conn, codes=["600519", "000858"])  # 指定重试代码
  ```

### 8.3 邮件发送失败

**现象**：`setup_check.py --email` 报 SMTP 登录失败。

**排查**：
1. 确认授权码是 **16 位 QQ 邮箱授权码**，不是 QQ 密码
2. 确认 QQ 邮箱已开启 POP3/SMTP 服务
3. 确认 `EMAIL_SENDER` 与授权码对应的 QQ 邮箱一致

**获取新授权码**：
登录 QQ 邮箱网页版 → 设置 → 账户 → POP3/SMTP 服务 → 开启 → 按提示用手机发短信 → 获得 16 位授权码

> 授权码可能过期，建议每年更新一次。

### 8.4 Python 版本不兼容

**现象**：`import` 报 `SyntaxError` 或 `ModuleNotFoundError`。

**解决**：
- 确认 Python 版本在 3.9 ~ 3.11 范围内
- Python 3.12+ 可能因部分依赖未适配而出错，建议降级到 3.10
- 如多版本共存，使用 `py -3.10 -m pip install -r requirements.txt` 指定版本

### 8.5 数据库锁定

**现象**：`sqlite3.OperationalError: database is locked`。

**解决**：
- 确保没有多个进程同时写入数据库（如同时运行两个 `run.py report`）
- Windows 下检查残留进程：`Get-Process python | Where-Object {$_.StartTime -lt (Get-Date).AddHours(-1)}`
- 如仍报锁定，重启后重试（SQLite 锁在进程退出后自动释放）

### 8.6 编码问题

**现象**：控制台输出乱码或 `UnicodeDecodeError`。

**解决**：
- 系统已内置 Windows 控制台 UTF-8 修复（`run.py`、`setup_check.py` 等入口脚本自动处理）
- 如仍遇到，手动设置：`chcp 65001` 后再运行
- PowerShell 执行策略限制时：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### 8.7 xtquant / QMT 相关

**现象**：`run.py trade` 报 `ModuleNotFoundError: No module named 'xtquant'`。

**解决**：
- `xtquant` 不在 PyPI 上，需从券商 QMT 安装目录复制 `xtquant` 文件夹到 Python 的 `site-packages` 目录
- 未开通 QMT 可忽略，其余功能不受影响
- 测试连接：`python run.py trade --test`

---

## 9. 附录：文件关系与兼容性说明

### 9.1 migrate.bat 与本指南的关系

`migrate.bat` 是本指南的**自动化快捷版**，执行以下 4 步：

| 步骤 | 对应指南章节 | 操作 |
|------|-------------|------|
| 1/4 | §3 依赖安装 | `pip install -r requirements.txt` |
| 2/4 | §5 数据库初始化 | `python setup.py`（含交互式配置向导） |
| 3/4 | §7 验证清单 | `python setup_check.py` |
| 4/4 | — | 打印后续手动操作提示 |

> 推荐流程：先阅读本指南了解全貌，然后运行 `migrate.bat` 自动完成基础初始化，再按 §4.3 和 §6 手动完成持仓填写和定时任务注册。

### 9.2 与旧版迁移文档的兼容性

本指南完全替代旧版迁移说明。V9.0 的主要变更：

| 变更项 | 旧版 | V9.0 |
|--------|------|------|
| 配置方式 | 仅 `config.py` 单文件 | 三层架构：`config.py` + `config_local.py` + `.env` |
| 数据加载 | 每次直接查 SQLite | LRU 缓存 + DB mtime 失效 |
| 报告数据源 | 直接调 baostock API | 优先本地 DB，降级 baostock |
| 废弃文件 | 存在 `backtest_v6.py` 等 | 已清理，不影响新版本 |
| `load_holdings` | 7 处重复实现 | 统一委托 `config.load_holdings()` |

### 9.3 从旧版升级的步骤

如果已在运行旧版系统，升级到 V9.0 只需：

```powershell
# 1. 拉取最新代码
git pull

# 2. 创建 config_local.py（如尚未创建）
copy trading_system\config_local.example.py trading_system\config_local.py
# 编辑填写授权码等敏感信息

# 3. 更新依赖（如有新增）
pip install -r requirements.txt --upgrade

# 4. 验证
python setup_check.py
```

> 无需重新初始化数据库或重新拉取数据，所有变更向后兼容。

---

> 如有本指南未覆盖的问题，请运行 `python setup_check.py` 获取诊断信息，或查阅 `docs/` 目录下的知识库文档。
