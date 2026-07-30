# -*- coding: utf-8 -*-
"""
全任务串行执行脚本 - 按时间顺序手动触发所有核心定时任务
=====================================================
绕过交易时段检查，依次执行并发送邮件到 563646039@qq.com
"""
import sys
import os
import time
import datetime
import logging
import traceback

# 环境设置
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "trading_system"))
sys.path.insert(0, os.path.dirname(__file__))

import config

# 日志配置
log_dir = config.LOG_DIR
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(log_dir, f"run_all_{datetime.date.today().strftime('%Y%m%d')}.log"),
            encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("run_all_tasks")

# 执行结果记录
RESULTS = []


def record(task_name, status, email_sent, elapsed, note=""):
    """记录任务执行结果"""
    RESULTS.append({
        "task": task_name,
        "status": status,
        "email": email_sent,
        "elapsed": elapsed,
        "note": note,
    })
    icon = {"成功": "OK", "失败": "FAIL", "跳过": "SKIP"}.get(status, "??")
    print(f"\n  [{icon}] {task_name} | {status} | 邮件:{'是' if email_sent else '否'} | {elapsed:.1f}s | {note}")


def run_task(task_name, func, bypass_time_check=False):
    """通用任务执行包装器"""
    print(f"\n{'='*60}")
    print(f"  >>> 开始执行: {task_name}")
    print(f"{'='*60}")
    start = time.time()
    try:
        result = func()
        elapsed = time.time() - start
        # 判断邮件是否发送（根据返回值或函数特性）
        email_sent = True  # 默认认为发送了
        note = ""
        if result is False:
            record(task_name, "跳过", False, elapsed, "函数返回False/None")
        elif isinstance(result, dict):
            if result.get("success") is False:
                note = result.get("error", result.get("report_text", ""))[:80]
                record(task_name, "失败", False, elapsed, note)
            else:
                record(task_name, "成功", True, elapsed, note)
        else:
            record(task_name, "成功", True, elapsed, note)
    except Exception as e:
        elapsed = time.time() - start
        err_msg = str(e)[:100]
        logger.error(f"  任务异常: {task_name} | {err_msg}")
        traceback.print_exc()
        record(task_name, "失败", False, elapsed, err_msg)


# ============================================================
# 任务1: 盘前作战计划 (08:30)
# ============================================================
def task_1_forecast_morning():
    """直接调用 run_forecast_morning"""
    from scheduler import run_forecast_morning
    run_forecast_morning()


# ============================================================
# 任务2: 竞价选股报告 (09:25)
# ============================================================
def task_2_screener():
    """直接调用 run_morning_screener"""
    from scheduler import run_morning_screener
    run_morning_screener()


# ============================================================
# 任务3: 盘中异动预警 (09:40-14:50)
# ============================================================
def task_3_intraday_alert():
    """绕过时间检查，直接调用底层函数"""
    from strategy.intraday_alert import run_intraday_alert
    result = run_intraday_alert(send_email=True)
    return result


# ============================================================
# 任务4: 盘中实时决策报告 (09:45-14:45)
# ============================================================
def task_4_intraday_decision():
    """绕过时间检查，直接调用底层函数"""
    from strategy.intraday_decision import run_intraday_decision
    result = run_intraday_decision(send_email_flag=True)
    return result


# ============================================================
# 任务5: 盘后完整分析 (15:30)
# ============================================================
def task_5_daily_analysis():
    """调用 run_daily_task"""
    from scheduler import run_daily_task
    run_daily_task()


# ============================================================
# 任务6: 盘后趋势预测 (15:35) - 已废弃
# ============================================================
def task_6_forecast_pm():
    """已合并到盘后综合日报，仅打印说明"""
    from scheduler import run_forecast_afternoon
    run_forecast_afternoon()
    return False  # 标记为跳过


# ============================================================
# 任务7: 条件单生成 (19:00)
# ============================================================
def task_7_orders():
    """调用 run_morning_reminder (条件单)"""
    from scheduler import run_morning_reminder
    run_morning_reminder()


# ============================================================
# 任务8: 持仓股深度预警 (alert_engine 单次)
# ============================================================
def task_8_alert_engine():
    """绕过时间检查，直接调用 AlertEngine"""
    import json
    from notify.alert_engine import AlertEngine, _fetch_and_analyze, send_alert_email

    holdings_data = {}
    try:
        with open(config.HOLDINGS_FILE, "r", encoding="utf-8") as f:
            holdings_data = json.load(f)
    except Exception:
        pass

    engine = AlertEngine(holdings=holdings_data)
    results = _fetch_and_analyze(holdings_data)
    if results:
        triggered = engine.check_alerts(results)
        if triggered:
            critical = [a for a in triggered if a.get("level") in ("critical", "high")]
            print(f"  持仓预警触发{len(triggered)}条(critical/high: {len(critical)}条)")
            if critical:
                send_alert_email(critical)
                print(f"  预警邮件已发送({len(critical)}条)")
            return {"success": True, "alert_count": len(triggered)}
        else:
            print(f"  无预警触发（所有持仓正常）")
            # 即使无预警也发一封确认邮件
            from notify.email_notify import send_email
            today = datetime.date.today().strftime("%Y-%m-%d")
            now = datetime.datetime.now().strftime("%H:%M")
            html = f"""<html><body style="font-family:Microsoft YaHei;padding:20px">
            <h2 style="color:#28a745">持仓股深度预警 - 单次检查报告</h2>
            <p>时间: {today} {now}</p>
            <p style="color:#28a745;font-size:16px;font-weight:bold">
            检查结果: 所有持仓标的正常，无预警触发。</p>
            <p>已检查规则: 11大规则(DK/生命线/乖离/资金/止损/涨跌停/筹码/趋势/死叉/日内跌幅/推荐失效)</p>
            <p>持仓标的: {', '.join(holdings_data.keys())}</p>
            <hr><p style="color:#999;font-size:11px">仅供参考，不构成投资建议</p>
            </body></html>"""
            send_email(f"[操盘密码] 持仓预警检查(无异常) | {today} {now}", html)
            return {"success": True, "alert_count": 0}
    else:
        print(f"  数据获取失败，无法执行预警检查")
        return {"success": False, "error": "数据获取失败"}


# ============================================================
# 主执行流程
# ============================================================
def main():
    print("=" * 70)
    print("  交易系统 - 全任务串行执行")
    print(f"  时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  邮箱: {config.EMAIL_RECEIVER}")
    print(f"  任务数: 8个核心任务")
    print("=" * 70)

    tasks = [
        ("1. 盘前作战计划(08:30)", task_1_forecast_morning),
        ("2. 竞价选股报告(09:25)", task_2_screener),
        ("3. 盘中异动预警(每10min)", task_3_intraday_alert),
        ("4. 盘中决策报告(每15min)", task_4_intraday_decision),
        ("5. 盘后完整分析(15:30)", task_5_daily_analysis),
        ("6. 盘后趋势预测(15:35)", task_6_forecast_pm),
        ("7. 条件单生成(19:00)", task_7_orders),
        ("8. 持仓深度预警(每3min)", task_8_alert_engine),
    ]

    total_start = time.time()

    for task_name, func in tasks:
        run_task(task_name, func)
        # 任务间隔3秒，避免API频率限制
        time.sleep(3)

    total_elapsed = time.time() - total_start

    # 输出汇总表
    print("\n\n")
    print("=" * 90)
    print("  执行汇总表")
    print("=" * 90)
    header = f"{'任务名':<30} {'状态':<6} {'邮件':<5} {'耗时':<8} {'备注'}"
    print(header)
    print("-" * 90)
    for r in RESULTS:
        line = f"{r['task']:<30} {r['status']:<6} {'是' if r['email'] else '否':<5} {r['elapsed']:<8.1f} {r['note'][:40]}"
        print(line)
    print("-" * 90)

    success_count = sum(1 for r in RESULTS if r["status"] == "成功")
    fail_count = sum(1 for r in RESULTS if r["status"] == "失败")
    skip_count = sum(1 for r in RESULTS if r["status"] == "跳过")
    email_count = sum(1 for r in RESULTS if r["email"])

    print(f"\n  总计: {len(RESULTS)}个任务 | 成功{success_count} | 失败{fail_count} | 跳过{skip_count} | 邮件{email_count}封")
    print(f"  总耗时: {total_elapsed:.1f}秒 ({total_elapsed/60:.1f}分钟)")
    print("=" * 90)


if __name__ == "__main__":
    main()
