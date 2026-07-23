# -*- coding: utf-8 -*-
"""
交易系统自动化调度器 V1.0
========================
一键启动全流程自动化:
  盘后(15:30) → 生成条件单 + 分析报告 + 邮件推送
  盘中(09:25) → 启动QMT条件单监控执行

使用方式:
  python auto_scheduler.py              # 常驻运行（推荐）
  python auto_scheduler.py --once       # 只执行一次盘后任务
  python auto_scheduler.py --setup      # 生成Windows计划任务配置说明

Windows计划任务（替代方案）:
  任务1: 每日15:30运行 python daily_orders.py（生成条件单）
  任务2: 每日15:35运行 python generate_holdings_report.py（分析报告）
  任务3: 每日09:25运行 python qmt_trader.py（盘中自动执行）
"""

import os
import sys
import time
import subprocess
import datetime
import logging
import io

# Windows控制台编码修复
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("auto_scheduler.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

# 调度时间表
SCHEDULE = {
    "盘后条件单": {"time": "15:30", "script": "daily_orders.py", "desc": "生成次日条件单+邮件推送"},
    "盘后分析报告": {"time": "15:35", "script": "generate_holdings_report.py", "desc": "技术分析+推荐标的报告"},
    "盘中自动执行": {"time": "09:25", "script": "qmt_trader.py", "desc": "QMT条件单自动监控执行"},
}


def run_script(script_name: str, desc: str) -> bool:
    """运行指定脚本"""
    script_path = os.path.join(BASE_DIR, script_name)
    if not os.path.exists(script_path):
        logger.error(f"[调度] 脚本不存在: {script_path}")
        return False

    logger.info(f"[调度] ▶ 开始执行: {desc} ({script_name})")
    start_time = time.time()

    try:
        result = subprocess.run(
            [PYTHON, script_path],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=300,  # 5分钟超时
            encoding="utf-8",
            errors="replace",
        )

        elapsed = time.time() - start_time

        if result.returncode == 0:
            logger.info(f"[调度] ✅ 完成: {desc} | 耗时{elapsed:.1f}秒")
            # 打印最后几行输出
            output_lines = result.stdout.strip().split("\n")
            for line in output_lines[-5:]:
                logger.info(f"  {line}")
            return True
        else:
            logger.error(f"[调度] ❌ 失败: {desc} | 返回码{result.returncode}")
            if result.stderr:
                logger.error(f"  错误: {result.stderr[-500:]}")
            return False

    except subprocess.TimeoutExpired:
        logger.error(f"[调度] ❌ 超时: {desc} (超过300秒)")
        return False
    except Exception as e:
        logger.error(f"[调度] ❌ 异常: {desc} | {e}")
        return False


def is_trading_day() -> bool:
    """判断今天是否为交易日（简化版：仅排除周末）"""
    today = datetime.date.today()
    if today.weekday() >= 5:  # 周六日
        return False
    # TODO: 接入节假日API判断法定假日
    return True


def run_after_close_tasks():
    """盘后任务：条件单 + 分析报告"""
    logger.info("=" * 60)
    logger.info(f"  盘后自动任务 | {datetime.date.today()}")
    logger.info("=" * 60)

    if not is_trading_day():
        logger.info("[调度] 今日非交易日，跳过")
        return

    # 1. 生成条件单
    run_script("daily_orders.py", "生成次日条件单")

    # 2. 生成分析报告
    time.sleep(3)  # 间隔3秒避免baostock频率限制
    run_script("generate_holdings_report.py", "技术分析报告")

    logger.info("\n[调度] 盘后任务全部完成 ✅")


def run_scheduler_daemon():
    """常驻调度守护进程"""
    logger.info("=" * 60)
    logger.info("  交易系统自动化调度器 V1.0")
    logger.info(f"  启动时间: {datetime.datetime.now()}")
    logger.info(f"  调度计划:")
    for name, info in SCHEDULE.items():
        logger.info(f"    {info['time']} | {name} | {info['desc']}")
    logger.info("=" * 60)

    executed_today = set()
    last_date = None

    while True:
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        current_time = now.strftime("%H:%M")

        # 日期变更，重置执行记录
        if today_str != last_date:
            executed_today = set()
            last_date = today_str
            if is_trading_day():
                logger.info(f"\n[调度] 新交易日: {today_str}")
            else:
                logger.info(f"\n[调度] 非交易日: {today_str}，休眠")

        # 检查是否有任务需要执行
        if is_trading_day():
            for name, info in SCHEDULE.items():
                task_key = f"{today_str}_{name}"
                if current_time >= info["time"] and task_key not in executed_today:
                    executed_today.add(task_key)

                    if name == "盘中自动执行":
                        # QMT执行器作为后台进程启动
                        logger.info(f"[调度] ▶ 启动盘中自动执行...")
                        script_path = os.path.join(BASE_DIR, info["script"])
                        if os.path.exists(script_path):
                            subprocess.Popen(
                                [PYTHON, script_path],
                                cwd=BASE_DIR,
                                stdout=open("qmt_stdout.log", "a", encoding="utf-8"),
                                stderr=open("qmt_stderr.log", "a", encoding="utf-8"),
                            )
                            logger.info(f"[调度] QMT执行器已在后台启动")
                    else:
                        run_script(info["script"], info["desc"])

        # 每30秒检查一次
        time.sleep(30)


def print_setup_guide():
    """打印Windows计划任务配置指南"""
    python_path = PYTHON
    print("""
╔══════════════════════════════════════════════════════════╗
║     交易系统自动化 - Windows计划任务配置指南           ║
╠══════════════════════════════════════════════════════════╣
║                                                        ║
║  方式一: 常驻进程（推荐）                              ║
║  ─────────────────────                                 ║
║  直接运行: python auto_scheduler.py                    ║
║  开机自启: 放入 startup 文件夹的快捷方式               ║
║                                                        ║
║  方式二: Windows任务计划程序                           ║
║  ─────────────────────────                             ║
║  打开: Win+R → taskschd.msc                           ║
║                                                        ║
║  任务1 - 盘后条件单 (每日15:30)                       ║
║  ─────────────────────────────                         ║
║  程序: {python}
║  参数: daily_orders.py                                 ║
║  起始于: {base_dir}
║                                                        ║
║  任务2 - 分析报告 (每日15:35)                         ║
║  ─────────────────────────────                         ║
║  程序: {python}
║  参数: generate_holdings_report.py                     ║
║  起始于: {base_dir}
║                                                        ║
║  任务3 - QMT自动执行 (每日09:25)                      ║
║  ─────────────────────────────                         ║
║  程序: {python}
║  参数: qmt_trader.py                                   ║
║  起始于: {base_dir}
║                                                        ║
╠══════════════════════════════════════════════════════════╣
║  QMT开通步骤（东方财富）:                             ║
║  1. 联系东方财富客户经理申请QMT权限（门槛50万）       ║
║  2. 下载QMT客户端，以"独立交易"模式登录               ║
║  3. 从QMT安装目录复制xtquant到Python site-packages    ║
║  4. 修改 qmt_trader.py 中的 QMT_CONFIG 配置           ║
║  5. 运行 python qmt_trader.py --test 验证连接         ║
╚══════════════════════════════════════════════════════════╝
""".format(python=python_path, base_dir=BASE_DIR))


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    if "--once" in sys.argv:
        run_after_close_tasks()
    elif "--setup" in sys.argv:
        print_setup_guide()
    else:
        run_scheduler_daemon()
