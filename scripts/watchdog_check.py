# -*- coding: utf-8 -*-
"""
系统看门狗 V1.0（V4.0 G8）
==========================
外部进程视角巡检调度器与盘中监控两条心跳链路，防止"静默失效"：
  - .scheduler_heartbeat  主调度器心跳（scheduler.py主循环每轮写入）
  - .monitor_heartbeat    盘中监控心跳（IntradayMonitor每轮扫描写入）

检查规则:
  1. 调度器心跳超过阈值(320秒)未更新 → CRITICAL（全部定时任务停摆）
  2. 盘中时段(09:35-15:00, 交易日)监控心跳超过阈值(15分钟)未更新 → CRITICAL（预警失效）
  3. 非盘中时段监控心跳缺失 → INFO（仅记录，不告警）

运行: python scripts/watchdog_check.py
建议: 注册为Windows任务计划，盘中每10分钟执行一次
日志: 追加写入 trading_system/logs/watchdog.log
退出码: 0=正常 1=存在CRITICAL
"""

import os
import sys
import io
import datetime

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # trading-system根目录
TS_DIR = os.path.join(BASE_DIR, "trading_system")
OUTPUT_DIR = os.path.join(TS_DIR, "output")
LOG_DIR = os.path.join(TS_DIR, "logs")

SCHEDULER_HEARTBEAT = os.path.join(OUTPUT_DIR, ".scheduler_heartbeat")
MONITOR_HEARTBEAT = os.path.join(OUTPUT_DIR, ".monitor_heartbeat")
SCHEDULER_STALE_SECONDS = 320      # 与scheduler.py HEARTBEAT_STALE_SECONDS保持一致
MONITOR_STALE_SECONDS = 900        # 与scheduler.py MONITOR_HEARTBEAT_STALE_SECONDS保持一致


def _log(line: str):
    """控制台 + 追加写watchdog.log"""
    print(line)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "watchdog.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_heartbeat(path: str):
    """读取心跳文件 -> (datetime, pid, status) 或 None"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            parts = f.read().strip().split("|")
        ts = datetime.datetime.fromisoformat(parts[0])
        pid = parts[1] if len(parts) > 1 else "?"
        status = parts[2] if len(parts) > 2 else ""
        return ts, pid, status
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _is_trading_day(d: datetime.date) -> bool:
    """交易日判定（优先复用scheduler实现，失败则退化为工作日判定）"""
    try:
        sys.path.insert(0, TS_DIR)
        from scheduler import is_trading_day
        return is_trading_day(d)
    except Exception:
        return d.weekday() < 5


def main() -> int:
    now = datetime.datetime.now()
    hm = now.hour * 100 + now.minute
    in_session = 935 <= hm <= 1500 and _is_trading_day(now.date())
    critical = []

    _log(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] watchdog巡检开始 (盘中={in_session})")

    # ---- 1. 调度器心跳 ----
    hb = _read_heartbeat(SCHEDULER_HEARTBEAT)
    if hb is None:
        critical.append("调度器心跳文件不存在，主调度器可能从未启动")
    else:
        age = (now - hb[0]).total_seconds()
        if age > SCHEDULER_STALE_SECONDS:
            critical.append(f"调度器心跳已{age / 60:.0f}分钟未更新(阈值{SCHEDULER_STALE_SECONDS // 60}分钟)，全部定时任务疑似停摆")
        else:
            _log(f"  ✅ 调度器心跳正常: {age:.0f}秒前 (PID={hb[1]})")

    # ---- 2. 监控心跳 ----
    hb = _read_heartbeat(MONITOR_HEARTBEAT)
    if in_session:
        if hb is None:
            critical.append("盘中时段监控心跳文件不存在，盘中监控循环可能从未启动")
        else:
            age = (now - hb[0]).total_seconds()
            if age > MONITOR_STALE_SECONDS:
                critical.append(f"盘中监控心跳已{age / 60:.0f}分钟未更新(阈值{MONITOR_STALE_SECONDS // 60}分钟)，预警疑似静默失效")
            else:
                _log(f"  ✅ 监控心跳正常: {age:.0f}秒前 (状态={hb[2] or '-'}, PID={hb[1]})")
    else:
        if hb is not None:
            _log(f"  ℹ️ 非盘中，监控心跳最近状态={hb[2] or '-'} ({(now - hb[0]).total_seconds() / 60:.0f}分钟前)")
        else:
            _log("  ℹ️ 非盘中，监控心跳缺失（不告警）")

    # ---- 结论 ----
    if critical:
        for c in critical:
            _log(f"  🔴 CRITICAL: {c}")
        # 尽力发送邮件告警（失败不影响退出码）
        try:
            sys.path.insert(0, TS_DIR)
            from notify.email_notify import send_email
            body = "".join(f"<p>🔴 {c}</p>" for c in critical)
            body += f"<p>巡检时间: {now.strftime('%Y-%m-%d %H:%M:%S')}</p>"
            send_email(f"[Watchdog] 系统心跳异常 {len(critical)}项 - {now.date()}", body)
            _log("  📧 watchdog告警邮件已发送")
        except Exception as e:
            _log(f"  ⚠️ watchdog告警邮件发送失败: {e}")
        return 1

    _log("  ✅ 巡检通过，无异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
