@echo off
REM === Trading System Scheduler Daemon (Watchdog) ===
REM 由任务计划 TradingSystem_SchedulerDaemon 每5分钟触发，等价于原 watchdog 语义:
REM   1) 心跳新鲜(<320秒): 主循环健康, 立即退出(任务实例数秒内结束, 无并发驻留)
REM   2) 心跳失联(>=320秒): 先taskkill PID文件中记录的残留进程(等价原bat心跳监控的强杀),
REM      再前台拉起新主循环并常驻(本bat持有python进程直至其退出)
REM 日志由 scheduler.py 自身写入 trading_system\logs\scheduler_YYYYMMDD.log
REM 说明: 原启动目录bat依赖控制台窗口标题做存活检测, 在无交互式登录会话时无法创建
REM       窗口(start静默失败), 故采用本心跳检测方案作为等价替代。

REM === 通用路径（基于本bat所在目录自动推导，无需硬编码） ===
set "ROOT_DIR=%~dp0"
set "HB_FILE=%ROOT_DIR%trading_system\output\.scheduler_heartbeat"
set "PF_FILE=%ROOT_DIR%trading_system\output\.scheduler.pid"

powershell -NoProfile -Command "$hb='%HB_FILE%'; $pf='%PF_FILE%'; $alive=$false; if(Test-Path $hb){ try{ $ts=(Get-Content $hb -First 1).Split('|')[0]; $alive=((Get-Date)-(Get-Date $ts)).TotalSeconds -lt 320 }catch{} }; if($alive){ exit 10 }; if(Test-Path $pf){ $p=(Get-Content $pf -First 1).Trim(); if($p -match '^\d+$'){ taskkill /PID $p /F 2>$null } }; exit 0"
if errorlevel 10 goto :eof

cd /d "%ROOT_DIR%"
python "%ROOT_DIR%trading_system\scheduler.py" >> "%ROOT_DIR%trading_system\logs\scheduler_daemon.out" 2>&1
