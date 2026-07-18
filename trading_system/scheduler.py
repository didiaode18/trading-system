"""
定时任务调度模块
==================
每个交易日自动运行交易分析流程并发送邮件报告

运行方式:
  python scheduler.py              # 启动调度器（前台运行）
  python scheduler.py --install    # 安装为Windows任务计划（开机自启）
  python scheduler.py --uninstall  # 卸载Windows任务计划

调度时间:
  - 每个交易日 08:30 自动运行完整流程
  - 非交易日（周末/法定节假日）自动跳过
"""

import os
import sys
import datetime
import logging
import argparse
import subprocess

# 确保项目根目录在sys.path中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import config

try:
    import schedule
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False

logger = logging.getLogger("scheduler")


# ============================================================
# 一、交易日判断
# ============================================================

# 中国法定节假日（需每年手动更新，或使用第三方库如chinese_calendar）
# 格式: "YYYY-MM-DD"
HOLIDAYS = set()

# 周末调休上班日（需每年手动更新）
WORKDAYS = set()


def is_trading_day(date: datetime.date = None) -> bool:
    """
    判断是否为交易日
    
    规则:
    - 周末默认非交易日（除非在WORKDAYS中）
    - 法定节假日非交易日（在HOLIDAYS中）
    - 其他日期默认为交易日
    """
    if date is None:
        date = datetime.date.today()

    # 调休上班日
    if date.strftime("%Y-%m-%d") in WORKDAYS:
        return True

    # 法定节假日
    if date.strftime("%Y-%m-%d") in HOLIDAYS:
        return False

    # 周末
    if date.weekday() >= 5:  # 5=周六, 6=周日
        return False

    return True


def next_trading_day(date: datetime.date = None) -> datetime.date:
    """获取下一个交易日"""
    if date is None:
        date = datetime.date.today()

    next_day = date + datetime.timedelta(days=1)
    while not is_trading_day(next_day):
        next_day += datetime.timedelta(days=1)
    return next_day


# ============================================================
# 二、任务执行
# ============================================================

def run_daily_task():
    """执行每日交易分析任务"""
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过")
        return

    logger.info(f"[{today}] 开始执行每日交易分析...")

    try:
        # 调用main.py的完整流程
        from main import run_daily_pipeline
        signals = run_daily_pipeline(skip_update=False, report_only=False)

        buy_count = sum(1 for _, s in signals if s.get("buy_signal"))
        sell_count = sum(1 for _, s in signals if s.get("sell_signal"))

        logger.info(f"[{today}] 任务完成: {buy_count}个买入信号, {sell_count}个卖出信号")

    except Exception as e:
        logger.error(f"[{today}] 任务执行失败: {e}", exc_info=True)

        # 发送错误通知邮件
        try:
            from notify.email_notify import send_email
            send_email(
                f"[交易系统] 运行异常 - {today}",
                f"<p>每日交易分析任务执行失败:</p><pre>{str(e)}</pre>"
            )
        except Exception:
            pass


def run_weekly_task():
    """每周日执行股票池更新提醒"""
    today = datetime.date.today()
    if today.weekday() == 6:  # 周日
        logger.info(f"[{today}] 周日提醒：请检查并更新股票池")
        try:
            from notify.email_notify import send_email
            send_email(
                f"[交易系统] 周日提醒 - {today}",
                "<p>今天是周日，请检查并更新下周的股票池(config.py)。</p>"
                "<p>同时建议运行回测验证策略参数。</p>"
            )
        except Exception:
            pass


# ============================================================
# 三、调度器
# ============================================================

def start_scheduler():
    """启动调度器"""
    if not HAS_SCHEDULE:
        logger.error("schedule库未安装，请运行: pip install schedule")
        logger.info("或使用Windows任务计划程序手动配置")
        return

    logger.info("=" * 50)
    logger.info("  交易系统定时调度器")
    logger.info(f"  启动时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 50)

    # 每个交易日 08:30 运行
    schedule.every().day.at("08:30").do(run_daily_task)

    # 每周日 10:00 运行周度任务
    schedule.every().sunday.at("10:00").do(run_weekly_task)

    logger.info("调度器已启动，等待执行...")
    logger.info(f"  每日任务: 08:30 (仅交易日)")
    logger.info(f"  周度任务: 周日 10:00")

    while True:
        schedule.run_pending()

        # 如果当前不在交易时段且还没到明天08:30，可以休眠
        now = datetime.datetime.now()
        if now.hour >= 16 or (now.hour < 8):
            # 非交易时段，每5分钟检查一次
            import time
            time.sleep(300)
        else:
            import time
            time.sleep(60)


# ============================================================
# 四、Windows任务计划程序
# ============================================================

def install_windows_task():
    """安装为Windows任务计划"""
    python_exe = sys.executable
    script_path = os.path.abspath(__file__)
    task_name = "TradingSystem_Daily"

    # 创建基本任务
    cmd = (
        f'schtasks /create /tn "{task_name}" '
        f'/tr "\"{python_exe}\" \"{script_path}\" --run-once" '
        f'/sc daily /st 08:30 '
        f'/f'
    )

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[OK] Windows任务计划已安装: {task_name}")
            print(f"  执行时间: 每天 08:30")
            print(f"  执行命令: {python_exe} {script_path} --run-once")
        else:
            print(f"[FAIL] 安装失败: {result.stderr}")
            print("  请以管理员身份运行此命令")
    except Exception as e:
        print(f"[ERROR] 安装异常: {e}")


def uninstall_windows_task():
    """卸载Windows任务计划"""
    task_name = "TradingSystem_Daily"
    cmd = f'schtasks /delete /tn "{task_name}" /f'

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[OK] Windows任务计划已卸载: {task_name}")
        else:
            print(f"[FAIL] 卸载失败: {result.stderr}")
    except Exception as e:
        print(f"[ERROR] 卸载异常: {e}")


# ============================================================
# 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="交易系统定时调度器")
    parser.add_argument("--install", action="store_true",
                        help="安装为Windows任务计划")
    parser.add_argument("--uninstall", action="store_true",
                        help="卸载Windows任务计划")
    parser.add_argument("--run-once", action="store_true",
                        help="立即运行一次（供任务计划调用）")
    args = parser.parse_args()

    # 配置日志
    log_dir = config.LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"scheduler_{datetime.date.today().strftime('%Y%m%d')}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

    if args.install:
        install_windows_task()
    elif args.uninstall:
        uninstall_windows_task()
    elif args.run_once:
        run_daily_task()
    else:
        start_scheduler()


if __name__ == "__main__":
    main()
