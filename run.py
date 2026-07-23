#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
高胜率A股交易操作系统 V7.0 - 统一命令行入口
============================================

用法:
  python run.py report        # 盘后综合分析报告（技术分析+条件单+推荐）
  python run.py orders        # 生成次日条件单操作计划
  python run.py trade         # 启动QMT盘中自动交易（需开通权限）
  python run.py trade --test  # 测试QMT连接
  python run.py auto          # 启动自动化调度守护进程
  python run.py auto --once   # 仅执行一次盘后任务
  python run.py setup         # 环境初始化（安装依赖+检查配置）
  python run.py backtest      # 运行策略回测
  python run.py screen        # 全赛道选股扫描
  python run.py status        # 查看系统状态与持仓概览

快捷方式:
  python run.py               # 等同于 python run.py report
"""

import sys
import os
import subprocess
import io

# Windows控制台编码修复
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 确保项目路径正确
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADING_SYSTEM_DIR = os.path.join(BASE_DIR, "trading_system")
sys.path.insert(0, TRADING_SYSTEM_DIR)
sys.path.insert(0, BASE_DIR)

PYTHON = sys.executable

# 命令 → 脚本映射
COMMANDS = {
    "report": {
        "script": "generate_holdings_report.py",
        "desc": "盘后综合分析报告（技术面+条件单+五层选股推荐）",
    },
    "orders": {
        "script": "daily_orders.py",
        "desc": "生成次日条件单操作计划（邮件+JSON）",
    },
    "trade": {
        "script": "qmt_trader.py",
        "desc": "QMT盘中自动交易执行器",
    },
    "auto": {
        "script": "auto_scheduler.py",
        "desc": "自动化调度守护进程（盘后报告+盘中执行）",
    },
    "setup": {
        "script": "setup.py",
        "desc": "环境初始化（安装依赖+检查配置）",
    },
    "backtest": {
        "script": os.path.join("trading_system", "backtest_real.py"),
        "desc": "策略回测引擎",
    },
    "screen": {
        "script": os.path.join("trading_system", "main.py"),
        "desc": "全赛道选股扫描",
    },
}


def print_help():
    """打印帮助信息"""
    print("""
╔══════════════════════════════════════════════════════════════╗
║     高胜率A股交易操作系统 V7.0                              ║
║     中线波段(3天-4周) · 条件单驱动 · 纪律自动化            ║
╠══════════════════════════════════════════════════════════════╣
║                                                            ║
║  用法: python run.py <command> [options]                   ║
║                                                            ║
║  核心命令:                                                 ║
║    report     盘后综合分析报告（技术+条件单+推荐）          ║
║    orders     生成次日条件单操作计划                        ║
║    trade      QMT盘中自动交易（需开通权限）                ║
║    auto       自动化调度守护进程                            ║
║                                                            ║
║  辅助命令:                                                 ║
║    setup      环境初始化                                   ║
║    backtest   策略回测                                     ║
║    screen     全赛道选股扫描                               ║
║    status     系统状态与持仓概览                           ║
║                                                            ║
║  选项:                                                     ║
║    --test     测试模式（trade命令专用）                    ║
║    --once     单次执行（auto命令专用）                     ║
║    --setup    配置指南（auto命令专用）                     ║
║    -h/--help  显示帮助                                     ║
║                                                            ║
║  典型工作流:                                               ║
║    1. 每晚15:30  python run.py orders   → 生成条件单       ║
║    2. 每晚15:35  python run.py report   → 分析报告         ║
║    3. 次日09:25  python run.py trade    → 自动执行         ║
║    或一键:       python run.py auto     → 全自动           ║
║                                                            ║
╚══════════════════════════════════════════════════════════════╝
""")


def print_status():
    """显示系统状态"""
    import datetime
    print(f"\n{'='*50}")
    print(f"  系统状态 | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")

    # 检查依赖
    deps = ["baostock", "akshare", "pandas", "numpy", "scipy"]
    print("\n[依赖检查]")
    for dep in deps:
        try:
            __import__(dep)
            print(f"  ✅ {dep}")
        except ImportError:
            print(f"  ❌ {dep} (未安装)")

    # 检查xtquant
    try:
        __import__("xtquant")
        print(f"  ✅ xtquant (QMT自动交易)")
    except ImportError:
        print(f"  ⚠️ xtquant (未安装，QMT自动交易不可用)")

    # 检查配置文件
    print("\n[配置文件]")
    config_path = os.path.join(TRADING_SYSTEM_DIR, "config.py")
    if os.path.exists(config_path):
        print(f"  ✅ config.py")
    holdings_path = os.path.join(BASE_DIR, "holdings.json")
    if os.path.exists(holdings_path):
        import json
        with open(holdings_path, "r", encoding="utf-8") as f:
            holdings = json.load(f)
        print(f"  ✅ holdings.json ({len(holdings)}只持仓)")
    else:
        print(f"  ⚠️ holdings.json 不存在")

    # 检查输出目录
    output_dir = os.path.join(TRADING_SYSTEM_DIR, "output")
    if os.path.exists(output_dir):
        files = os.listdir(output_dir)
        html_files = [f for f in files if f.endswith(".html")]
        json_files = [f for f in files if f.endswith(".json")]
        print(f"  ✅ output/ ({len(html_files)}个报告, {len(json_files)}个数据文件)")

    # 检查邮件配置
    print("\n[邮件配置]")
    try:
        from notify.email_notify import send_email
        print(f"  ✅ 邮件模块可用")
    except Exception as e:
        print(f"  ❌ 邮件模块异常: {e}")

    print(f"\n{'='*50}\n")


def run_command(cmd: str, args: list):
    """执行指定命令"""
    if cmd not in COMMANDS:
        print(f"❌ 未知命令: {cmd}")
        print(f"   可用命令: {', '.join(COMMANDS.keys())}")
        return 1

    info = COMMANDS[cmd]
    script_path = os.path.join(BASE_DIR, info["script"])

    if not os.path.exists(script_path):
        print(f"❌ 脚本不存在: {script_path}")
        return 1

    # 构建命令
    full_cmd = [PYTHON, script_path] + args

    print(f"▶ 执行: {info['desc']}")
    print(f"  脚本: {info['script']}")
    print(f"{'─'*50}")

    # 对于trade和auto命令，直接继承终端（交互式）
    if cmd in ("trade", "auto"):
        result = subprocess.run(full_cmd, cwd=BASE_DIR)
        return result.returncode
    else:
        result = subprocess.run(
            full_cmd, cwd=BASE_DIR,
            encoding="utf-8", errors="replace"
        )
        return result.returncode


def main():
    args = sys.argv[1:]

    # 无参数或help
    if not args or args[0] in ("-h", "--help", "help"):
        print_help()
        return 0

    cmd = args[0]
    cmd_args = args[1:]

    # status命令直接处理
    if cmd == "status":
        print_status()
        return 0

    # 其他命令转发到对应脚本
    return run_command(cmd, cmd_args)


if __name__ == "__main__":
    sys.exit(main() or 0)
