"""
预警静默管理工具
===================================
V26: 简化为CLI工具（HTTP回调服务器已废弃，自动静默模式无需回调）

功能:
  1. CLI工具: 命令行查看/管理静默状态
  2. 手动确认: 为指定标的+规则登记静默

用法:
  python -m trading_system.notify.alert_ack_server status
  python -m trading_system.notify.alert_ack_server confirm 159611 "R8-趋势级别下降"
  python -m trading_system.notify.alert_ack_server list
"""

import os
import sys
import json
import logging
import argparse
import datetime

# 确保项目根目录在sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from notify.alert_ack import (
        record_delivery, get_ack_record, get_ack_summary,
        _load_state, _today_str, is_delivered
    )
except ImportError:
    from trading_system.notify.alert_ack import (
        record_delivery, get_ack_record, get_ack_summary,
        _load_state, _today_str, is_delivered
    )

logger = logging.getLogger(__name__)


# ============================================================
# CLI工具
# ============================================================

def cli_confirm(args):
    """CLI: 手动登记静默"""
    code = args.code
    rule = args.rule
    level = args.level or "critical"
    urgency = args.urgency or 75

    ok = record_delivery(code, rule, level, urgency)
    if ok:
        print(f"[OK] 已登记静默: {code} {rule} (级别:{level}, 紧急度:{urgency})")
        print(f"   当日该标的同规则预警不再重复推送")
    else:
        print(f"[FAIL] 登记失败，请检查日志")


def cli_status(args):
    """CLI: 查看今日静默状态"""
    state = _load_state()
    today = _today_str()

    if state.get("date") != today:
        print(f"[{today}] 暂无提醒记录")
        return

    delivered = state.get("delivered_records", {})
    if not delivered:
        print(f"[{today}] 暂无提醒记录")
        return

    print(f"今日预警静默记录 ({today}):")
    print("-" * 60)
    for code, info in delivered.items():
        print(f"  {code}: {info.get('rule', '')} | "
              f"级别:{info.get('level', '')} | "
              f"紧急度:{info.get('urgency_score', 0)} | "
              f"提醒时间:{info.get('ack_time', '')}")
    print("-" * 60)
    print(f"共{len(delivered)}条静默记录")


def main():
    parser = argparse.ArgumentParser(description="预警静默管理工具")
    sub = parser.add_subparsers(dest="command")

    # confirm
    p_confirm = sub.add_parser("confirm", help="手动登记静默")
    p_confirm.add_argument("code", help="标的代码(如159611)")
    p_confirm.add_argument("rule", help="规则名称(如R8-趋势级别下降)")
    p_confirm.add_argument("--level", default="critical", help="预警级别")
    p_confirm.add_argument("--urgency", type=int, default=75, help="紧急度")

    # status
    sub.add_parser("status", help="查看今日静默状态")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.command == "confirm":
        cli_confirm(args)
    elif args.command == "status":
        cli_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
