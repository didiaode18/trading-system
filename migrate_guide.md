# 操盘密码交易系统 - 迁移指南（笔记本 → 台式电脑）

> 最后同步日期: 2026-07-28 | 对应代码版本: 操盘密码 V9.0
> 适用场景: 将本系统从一台 Windows 电脑完整迁移到另一台 Windows 电脑。

---

## 一、迁移前准备（旧机器）

### 1. 确认需要携带的文件
代码仓库会自动排除以下内容（见 `.gitignore`），如需保留请**手动拷贝**：

| 文件/目录 | 说明 | 是否必须携带 |
|---|---|---|
| `trading_system/data/stock_db.db` | 历史K线数据库（不带也可，新机器会重新拉取，约3-5分钟） | 建议携带 |
| `trading_system/data/trade_journal.db` | 交易日志（历史成交/绩效记录） | **必须携带** |
| `trading_system/holdings.json` + 根目录 `holdings.json` | 当前持仓（双份同步） | **必须携带** |
| `trading_system/config_local.py` | 邮箱授权码等敏感配置 | **必须携带**（或在新机器重新填写） |
| `trading_system/data/*.pkl / *.json 缓存` | 运行缓存 | 可不带（自动重建） |
| `trading_system/logs/` | 运行日志 | 可不带 |
| `trading_system/output/` | 报告与运行状态 | 建议携带 `risk_state.json`（风控熔断状态） |

### 2. 推荐做法
- 直接整包压缩项目文件夹（含上表数据文件）拷贝，新机器上再按本指南初始化。
- 或 git 推送代码 + 单独拷贝数据文件（`data/*.db`、`holdings.json`、`config_local.py`）。

---

## 二、新机器环境搭建

### 1. 安装 Python
- 版本要求：**3.9 ~ 3.11**（当前开发环境为 3.10.8）。
- 安装时勾选 "Add Python to PATH"。

### 2. 安装依赖
```powershell
cd <项目根目录>
pip install -r requirements.txt
```
可选依赖（按需）：
```powershell
pip install xgboost lightgbm win10toast streamlit
```

### 3. 填写敏感配置
二选一：
- **方案A（推荐）**：复制 `trading_system/config_local.example.py` 为 `trading_system/config_local.py`，填写 `EMAIL_AUTH_CODE`（QQ邮箱授权码）。
- **方案B**：复制 `.env.example` 为 `.env` 填写。授权码获取：QQ邮箱 → 设置 → 账户 → POP3/SMTP服务 → 生成授权码。

优先级：`config_local.py` > 系统环境变量/`.env` > 空（空则邮件通知不可用，其余功能不受影响）。

### 4. 初始化 + 自检
```powershell
python setup.py            # 初始化数据库、拉取历史数据（若未携带stock_db.db）
python setup_check.py      # 环境自检，输出通过/未通过清单
python setup_check.py --email   # 可选：实测SMTP授权码是否有效
```

---

## 三、数据库迁移说明

- 系统所有数据库路径均为**项目相对路径**（`trading_system/data/*.db`），随文件夹拷贝即可用，无需修改任何路径配置。
- 如未携带 `stock_db.db`：首次运行 `python setup.py` 会自动重建并拉取候选池历史数据。
- `trade_journal.db` 记录了历史交易绩效，**无法自动重建**，请务必拷贝。

---

## 四、Windows 任务计划重新注册

旧机器的任务计划不会随文件迁移，需要在新机器重新注册（以管理员身份打开 PowerShell）：

```powershell
$root = "<项目根目录绝对路径>"

# 一键自动化调度（推荐，盘后报告+盘中执行全覆盖）
schtasks /Create /TN "TradingSystem_Auto" /SC ONSTART /TR "python $root\run.py auto"

# 或分任务注册：
# 盘后条件单（每个交易日 15:30）
schtasks /Create /TN "TradingSystem_Orders" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:30 /TR "python $root\run.py orders"
# 盘后分析报告（每个交易日 15:35）
schtasks /Create /TN "TradingSystem_Report" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:35 /TR "python $root\run.py report"
# 盘中监控（每个交易日 09:15）
schtasks /Create /TN "TradingSystem_Intraday" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:15 /TR "python -m trading_system.main --monitor"
```

> 提示：具体任务时间与入口以旧机器 `schtasks /Query /V /FO LIST` 导出为准；也可直接使用 `python run.py auto` 前台常驻方式替代任务计划。

---

## 五、调度器启动方式

```powershell
python run.py auto                     # 推荐：自动化调度守护进程（一键）
python run.py orders                   # 生成次日条件单
python run.py report                   # 盘后综合分析报告
python run.py status                   # 系统状态与持仓概览
python -m trading_system.main --monitor  # 盘中实时监控
python -m trading_system.scheduler     # 定时调度器
```

---

## 六、迁移后验证清单

按顺序执行，全部通过即迁移成功：

- [ ] `python setup_check.py` 无 ✗ 项
- [ ] `python -c "import trading_system"` 无报错
- [ ] `python -c "from trading_system import config; print(config.EMAIL_AUTH_CODE != '')"` 输出 `True`
- [ ] `holdings.json` 双份（根目录 + trading_system/）内容一致
- [ ] `python -m trading_system.main --no-update` 或 `python run.py status` 可正常运行
- [ ] （可选）`python setup_check.py --email` SMTP 登录成功

---

## 七、已知平台依赖说明

- 系统为纯 Python 实现，**无 DLL/COM 组件依赖**。
- `win10toast`（桌面弹窗）仅 Windows 可用，缺失时自动跳过。
- `xtquant`（miniQMT 自动交易）可选，需从券商 QMT 安装目录复制，未开通 QMT 无需处理。
