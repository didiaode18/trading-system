# -*- coding: utf-8 -*-
"""
系统看门狗 V2.0（关键进程存活监控体系）
========================================
外部进程视角巡检所有关键进程/线程，防止"静默失效"。

监控对象 (5项):
  ┌─────────────────────┬──────────────┬───────────┬────────────────────┐
  │ 监控目标            │ 检测方式     │ 超时阈值  │ 告警级别           │
  ├─────────────────────┼──────────────┼───────────┼────────────────────┤
  │ 1. 主调度器         │ 心跳文件     │ 320秒     │ CRITICAL           │
  │ 2. 盘中监控线程     │ 心跳文件     │ 900秒     │ CRITICAL           │
  │ 3. 买点快路径       │ 状态文件     │ 600秒     │ WARNING            │
  │ 4. 选股评分任务     │ 状态文件     │ 2400秒    │ WARNING            │
  │ 5. 调度器日志       │ 文件mtime    │ 600秒     │ WARNING(盘中)      │
  └─────────────────────┴──────────────┴───────────┴────────────────────┘

心跳文件:
  - .scheduler_heartbeat  主调度器心跳（scheduler.py主循环每轮写入）
  - .monitor_heartbeat    盘中监控心跳（IntradayMonitor每轮扫描写入）
  - .buy_point_state      买点快路径状态（每3分钟执行后写入）
  - .screener_state       选股评分状态（10:00/14:00执行后写入）

告警通道:
  - CRITICAL → 钉钉 + 邮件双通道立即告警
  - WARNING  → 邮件告警
  - INFO     → 每日盘后生成存活状态日报（15:05后发送）

防误报设计:
  - 非交易时段（夜间/周末）自动放宽检测阈值
  - 告警去重：同一问题当日只发一次告警
  - 启动宽限期：09:35前不检测盘中监控心跳

运行: python scripts/watchdog_check.py
建议: 注册为Windows任务计划，盘中每10分钟执行一次
日志: 追加写入 trading_system/logs/watchdog.log
退出码: 0=全部正常 1=存在CRITICAL异常
"""

import os
import sys
import io
import json
import datetime
import logging

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ============================================================
# 路径常量
# ============================================================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TS_DIR = os.path.join(BASE_DIR, "trading_system")
OUTPUT_DIR = os.path.join(TS_DIR, "output")
LOG_DIR = os.path.join(TS_DIR, "logs")
DATA_DIR = os.path.join(TS_DIR, "data")

# 心跳/状态文件
SCHEDULER_HEARTBEAT = os.path.join(OUTPUT_DIR, ".scheduler_heartbeat")
MONITOR_HEARTBEAT = os.path.join(OUTPUT_DIR, ".monitor_heartbeat")
BUY_POINT_STATE = os.path.join(OUTPUT_DIR, ".buy_point_state")
SCREENER_STATE = os.path.join(OUTPUT_DIR, ".screener_state")
SCHEDULER_PID_FILE = os.path.join(OUTPUT_DIR, ".scheduler.pid")

# 去重状态文件
_WD_DEDUP_FILE = os.path.join(DATA_DIR, "watchdog_dedup.json")

# ============================================================
# 超时阈值（盘中时段）
# ============================================================
SCHEDULER_STALE_SECONDS = 320       # 与scheduler.py HEARTBEAT_STALE_SECONDS保持一致
MONITOR_STALE_SECONDS = 900         # 与scheduler.py MONITOR_HEARTBEAT_STALE_SECONDS保持一致
BUY_POINT_STALE_SECONDS = 600       # 买点快路径每3分钟执行，10分钟阈值留足余量
SCREENER_STALE_SECONDS = 2400       # 选股评分10:00/14:00执行，40分钟阈值
LOG_STALE_SECONDS = 600             # 日志文件10分钟无写入视为异常

# 非盘中时段放宽倍数（夜间/周末阈值×3）
_OFF_HOURS_MULTIPLIER = 3

# 启动宽限期：此时间前不检测盘中监控心跳
_GRACE_HOUR, _GRACE_MINUTE = 9, 35

# 日报发送时间窗口
_DAILY_REPORT_HOUR = 15
_DAILY_REPORT_MINUTE = 5

# ============================================================
# 日志
# ============================================================
_logger = None


def _setup_logger():
    """配置watchdog专用logger（同时输出控制台+文件）"""
    global _logger
    if _logger is not None:
        return _logger
    _logger = logging.getLogger("watchdog")
    _logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    # 控制台
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    _logger.addHandler(ch)
    # 文件
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(os.path.join(LOG_DIR, "watchdog.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        _logger.addHandler(fh)
    except Exception:
        pass
    return _logger


def _log(line: str):
    """输出日志（控制台+文件）"""
    lg = _setup_logger()
    lg.info(line)


# ============================================================
# 工具函数
# ============================================================

def _read_heartbeat(path: str):
    """读取心跳/状态文件 -> (datetime, pid, status) 或 None"""
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


def _is_pid_alive(pid_str: str) -> bool:
    """检测PID是否存活（Windows兼容）"""
    if not pid_str or pid_str == "?":
        return False
    try:
        pid = int(pid_str)
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


# ============================================================
# 告警去重
# ============================================================

def _load_dedup() -> dict:
    """加载去重状态"""
    try:
        with open(_WD_DEDUP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_dedup(state: dict):
    """保存去重状态"""
    try:
        os.makedirs(os.path.dirname(_WD_DEDUP_FILE), exist_ok=True)
        with open(_WD_DEDUP_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


def _should_alert(issue_key: str, dedup_state: dict) -> bool:
    """判断是否需要发送告警（同一问题当日只发一次）"""
    today = datetime.date.today().isoformat()
    # 清理非今日条目
    stale = [k for k in dedup_state if not dedup_state[k].startswith(today)]
    for k in stale:
        del dedup_state[k]
    key = f"{today}_{issue_key}"
    if key in dedup_state:
        return False
    dedup_state[key] = datetime.datetime.now().isoformat()
    _save_dedup(dedup_state)
    return True


# ============================================================
# 通知发送
# ============================================================

def _send_email_alert(subject: str, body_html: str):
    """发送邮件告警"""
    try:
        sys.path.insert(0, TS_DIR)
        from notify.email_notify import send_email
        send_email(subject, body_html)
        _log(f"  📧 告警邮件已发送: {subject}")
        return True
    except Exception as e:
        _log(f"  ⚠️ 告警邮件发送失败: {e}")
        return False


def _send_dingtalk_alert(title: str, content: str):
    """发送钉钉告警（复用send_notification接口）"""
    try:
        sys.path.insert(0, TS_DIR)
        from notify.wechat_notify import send_notification
        result = send_notification(title, content)
        if result.get("dingtalk"):
            _log(f"  📱 钉钉告警已发送: {title}")
        else:
            _log(f"  ⚠️ 钉钉告警未发送(webhook可能未配置)")
        return result.get("dingtalk", False)
    except Exception as e:
        _log(f"  ⚠️ 钉钉告警发送异常: {e}")
        return False


# ============================================================
# 主检测逻辑
# ============================================================

def main() -> int:
    now = datetime.datetime.now()
    today = now.date()
    hm = now.hour * 100 + now.minute
    is_td = _is_trading_day(today)
    in_session = 935 <= hm <= 1500 and is_td
    # 启动宽限期：09:35前不检测盘中监控心跳
    in_grace = now.hour < _GRACE_HOUR or (now.hour == _GRACE_HOUR and now.minute < _GRACE_MINUTE)
    # 非盘中时段放宽阈值
    threshold_mult = 1 if in_session else _OFF_HOURS_MULTIPLIER
    # 日报时间窗口
    is_daily_report_time = (now.hour == _DAILY_REPORT_HOUR and now.minute >= _DAILY_REPORT_MINUTE) or \
                           (now.hour == _DAILY_REPORT_HOUR + 1 and now.minute <= 10)

    log = _setup_logger()
    log.info(f"=== watchdog V2.0 巡检开始 (盘中={in_session}, 交易日={is_td}, 宽限={in_grace}) ===")

    critical_issues = []   # [(issue_key, message)]
    warning_issues = []    # [(issue_key, message)]
    info_items = []        # [(name, status_str)]
    dedup = _load_dedup()

    # ============================================================
    # 1. 主调度器心跳 (CRITICAL)
    # ============================================================
    hb = _read_heartbeat(SCHEDULER_HEARTBEAT)
    stale_thresh = SCHEDULER_STALE_SECONDS * threshold_mult
    if hb is None:
        if is_td and not (now.hour < 8):
            # 交易日08:00后心跳文件不应缺失
            msg = "调度器心跳文件不存在，主调度器可能从未启动"
            critical_issues.append(("scheduler_missing", msg))
            info_items.append(("主调度器", "❌ 心跳文件缺失"))
        else:
            info_items.append(("主调度器", "ℹ️ 非工作时段，心跳缺失不告警"))
    else:
        age = (now - hb[0]).total_seconds()
        pid_alive = _is_pid_alive(hb[1])
        if age > stale_thresh:
            msg = (f"调度器心跳已{age/60:.0f}分钟未更新"
                   f"(阈值{stale_thresh//60}分钟, PID={hb[1]}, 存活={pid_alive})")
            critical_issues.append(("scheduler_stale", msg))
            info_items.append(("主调度器", f"❌ 心跳{age/60:.0f}分钟未更新"))
        else:
            _log(f"  ✅ 调度器心跳正常: {age:.0f}秒前 (PID={hb[1]}, 存活={pid_alive})")
            info_items.append(("主调度器", f"✅ {age:.0f}秒前"))

    # ============================================================
    # 2. 盘中监控线程心跳 (CRITICAL, 盘中时段+宽限期后)
    # ============================================================
    hb = _read_heartbeat(MONITOR_HEARTBEAT)
    mon_stale = MONITOR_STALE_SECONDS * threshold_mult
    if in_session and not in_grace:
        if hb is None:
            msg = "盘中时段监控心跳文件不存在，盘中监控循环可能从未启动"
            critical_issues.append(("monitor_missing", msg))
            info_items.append(("盘中监控", "❌ 心跳文件缺失"))
        else:
            age = (now - hb[0]).total_seconds()
            if age > mon_stale:
                msg = (f"盘中监控心跳已{age/60:.0f}分钟未更新"
                       f"(阈值{mon_stale//60}分钟, 状态={hb[2] or '-'})")
                critical_issues.append(("monitor_stale", msg))
                info_items.append(("盘中监控", f"❌ 心跳{age/60:.0f}分钟未更新"))
            else:
                _log(f"  ✅ 监控心跳正常: {age:.0f}秒前 (状态={hb[2]}, PID={hb[1]})")
                info_items.append(("盘中监控", f"✅ {age:.0f}秒前 ({hb[2]})"))
    else:
        if hb is not None:
            age = (now - hb[0]).total_seconds()
            _log(f"  ℹ️ 非盘中/宽限期，监控心跳: {age/60:.0f}分钟前 (状态={hb[2]})")
            info_items.append(("盘中监控", f"ℹ️ 非盘中 {age/60:.0f}分钟前"))
        else:
            info_items.append(("盘中监控", "ℹ️ 非盘中，心跳缺失"))

    # ============================================================
    # 3. 买点快路径状态 (WARNING, 盘中时段)
    # ============================================================
    bp = _read_heartbeat(BUY_POINT_STATE)
    bp_stale = BUY_POINT_STALE_SECONDS * threshold_mult
    if in_session:
        if bp is None:
            # 首次执行前（09:30后约3分钟才首次运行），给10分钟宽限
            if hm >= 945:
                msg = "买点快路径状态文件不存在(已过09:45)，快路径可能从未执行"
                warning_issues.append(("buy_point_missing", msg))
                info_items.append(("买点快路径", "⚠️ 状态文件缺失"))
            else:
                info_items.append(("买点快路径", "ℹ️ 09:45前，等待首次执行"))
        else:
            age = (now - bp[0]).total_seconds()
            if age > bp_stale:
                msg = f"买点快路径已{age/60:.0f}分钟未执行(阈值{bp_stale//60}分钟)"
                warning_issues.append(("buy_point_stale", msg))
                info_items.append(("买点快路径", f"⚠️ {age/60:.0f}分钟未执行"))
            else:
                _log(f"  ✅ 买点快路径正常: {age/60:.1f}分钟前")
                info_items.append(("买点快路径", f"✅ {age/60:.1f}分钟前"))
    else:
        if bp is not None:
            age = (now - bp[0]).total_seconds()
            info_items.append(("买点快路径", f"ℹ️ 非盘中 {age/60:.0f}分钟前"))
        else:
            info_items.append(("买点快路径", "ℹ️ 非盘中"))

    # ============================================================
    # 4. 选股评分任务状态 (WARNING, 盘中时段)
    # ============================================================
    sc = _read_heartbeat(SCREENER_STATE)
    sc_stale = SCREENER_STALE_SECONDS * threshold_mult
    if in_session:
        if sc is None:
            # 首次评分在10:00运行，10:40前不告警
            if hm >= 1040:
                msg = "选股评分状态文件不存在(已过10:40)，评分任务可能从未执行"
                warning_issues.append(("screener_missing", msg))
                info_items.append(("选股评分", "⚠️ 状态文件缺失"))
            else:
                info_items.append(("选股评分", "ℹ️ 10:40前，等待首次执行"))
        else:
            age = (now - sc[0]).total_seconds()
            if age > sc_stale:
                msg = f"选股评分已{age/60:.0f}分钟未执行(阈值{sc_stale//60}分钟)"
                warning_issues.append(("screener_stale", msg))
                info_items.append(("选股评分", f"⚠️ {age/60:.0f}分钟未执行"))
            else:
                _log(f"  ✅ 选股评分正常: {age/60:.1f}分钟前")
                info_items.append(("选股评分", f"✅ {age/60:.1f}分钟前"))
    else:
        if sc is not None:
            age = (now - sc[0]).total_seconds()
            info_items.append(("选股评分", f"ℹ️ 非盘中 {age/60:.0f}分钟前"))
        else:
            info_items.append(("选股评分", "ℹ️ 非盘中"))

    # ============================================================
    # 5. 调度器日志文件新鲜度 (WARNING, 盘中时段)
    # ============================================================
    log_file = os.path.join(LOG_DIR, f"scheduler_{today.strftime('%Y%m%d')}.log")
    if in_session:
        if not os.path.exists(log_file):
            if hm >= 935:
                msg = f"今日调度器日志不存在({log_file})"
                warning_issues.append(("log_missing", msg))
                info_items.append(("调度日志", "⚠️ 今日日志缺失"))
            else:
                info_items.append(("调度日志", "ℹ️ 09:35前，等待日志创建"))
        else:
            try:
                mtime = os.path.getmtime(log_file)
                age = (now - datetime.datetime.fromtimestamp(mtime)).total_seconds()
                if age > LOG_STALE_SECONDS * threshold_mult:
                    msg = f"调度器日志已{age/60:.0f}分钟无写入"
                    warning_issues.append(("log_stale", msg))
                    info_items.append(("调度日志", f"⚠️ {age/60:.0f}分钟无写入"))
                else:
                    info_items.append(("调度日志", f"✅ {age/60:.0f}分钟前写入"))
            except Exception:
                info_items.append(("调度日志", "⚠️ 无法读取mtime"))
    else:
        info_items.append(("调度日志", "ℹ️ 非盘中"))

    # ============================================================
    # 告警发送
    # ============================================================

    # --- CRITICAL: 钉钉 + 邮件 ---
    if critical_issues:
        new_critical = []
        for key, msg in critical_issues:
            _log(f"  🔴 CRITICAL: {msg}")
            if _should_alert(key, dedup):
                new_critical.append(msg)

        if new_critical:
            # 邮件
            body = "".join(f"<p>🔴 {m}</p>" for m in new_critical)
            body += f"<p>巡检时间: {now.strftime('%Y-%m-%d %H:%M:%S')}</p>"
            body += "<p><small>同一问题当日不重复告警</small></p>"
            _send_email_alert(
                f"[Watchdog] 🔴 系统心跳异常 {len(new_critical)}项 - {now.date()}",
                body
            )
            # 钉钉
            dt_content = (f"🔴 **系统关键进程异常** ({len(new_critical)}项)\n\n"
                          + "\n".join(f"- {m}" for m in new_critical)
                          + f"\n\n> {now.strftime('%Y-%m-%d %H:%M:%S')}")
            _send_dingtalk_alert(
                f"[股票] [Watchdog] 🔴 系统关键进程异常 {len(new_critical)}项",
                dt_content
            )
    else:
        _log("  ✅ 无CRITICAL异常")

    # --- WARNING: 仅邮件 ---
    if warning_issues:
        new_warnings = []
        for key, msg in warning_issues:
            _log(f"  ⚠️ WARNING: {msg}")
            if _should_alert(key, dedup):
                new_warnings.append(msg)

        if new_warnings:
            body = "".join(f"<p>⚠️ {m}</p>" for m in new_warnings)
            body += f"<p>巡检时间: {now.strftime('%Y-%m-%d %H:%M:%S')}</p>"
            _send_email_alert(
                f"[Watchdog] ⚠️ 任务执行异常 {len(new_warnings)}项 - {now.date()}",
                body
            )

    # ============================================================
    # 每日盘后日报 (15:05-16:00 首次巡检时发送)
    # ============================================================
    if is_daily_report_time and is_td:
        _daily_key = f"daily_report_{today}"
        if _should_alert(_daily_key, dedup):
            _log("  📊 生成每日存活状态日报...")
            lines = [f"**📊 交易系统每日存活状态日报**",
                     f"**{now.strftime('%Y-%m-%d')}**", ""]
            for name, status in info_items:
                lines.append(f"- {name}: {status}")
            lines.append("")
            lines.append(f"> CRITICAL: {len(critical_issues)}项 | WARNING: {len(warning_issues)}项")
            lines.append(f"> 巡检时间: {now.strftime('%H:%M:%S')}")

            dt_report = "\n".join(lines)
            _send_dingtalk_alert("[股票] [Watchdog] 📊 每日存活状态日报", dt_report)

            html_body = ("<h3>📊 交易系统每日存活状态日报</h3>"
                         + f"<p>{now.strftime('%Y-%m-%d')}</p><ul>"
                         + "".join(f"<li>{n}: {s}</li>" for n, s in info_items)
                         + f"</ul><p>CRITICAL: {len(critical_issues)}项 | "
                         + f"WARNING: {len(warning_issues)}项</p>")
            _send_email_alert(
                f"[Watchdog] 📊 每日存活状态日报 - {now.date()}",
                html_body
            )

    # ============================================================
    # 结论
    # ============================================================
    total_issues = len(critical_issues) + len(warning_issues)
    if critical_issues:
        _log(f"=== 巡检完成: 🔴 CRITICAL {len(critical_issues)}项, "
             f"⚠️ WARNING {len(warning_issues)}项 ===")
        return 1

    if warning_issues:
        _log(f"=== 巡检完成: ⚠️ WARNING {len(warning_issues)}项, 无CRITICAL ===")
    else:
        _log("=== 巡检完成: ✅ 全部正常 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
