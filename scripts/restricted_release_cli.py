"""
限售解禁事件预警 CLI
======================
独立运行脚本，不修改 main.py，零外部依赖（仅标准库）。

用法:
    python scripts/restricted_release_cli.py                  # 默认演示
    python scripts/restricted_release_cli.py --code 002415    # 检查单只股票
    python scripts/restricted_release_cli.py --filter 002415,600519,300750  # 过滤选股
    python scripts/restricted_release_cli.py --summary         # 解禁摘要
"""

import argparse
import datetime
import json
import sys
import os
import io

# Windows 终端 UTF-8 输出兼容
if sys.stdout and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_system.strategy.event_calendar import EventCalendar


# ============================================================
# 演示用内置测试数据（当无外部数据源时使用）
# ============================================================
DEMO_RELEASE_DATA = [
    {
        "stock_code": "002415",
        "stock_name": "海康威视",
        "release_date": (datetime.date.today() + datetime.timedelta(days=5)).isoformat(),
        "release_amount": 500000000,
        "release_market_value_ratio": 12.5,
        "release_type": "首发原股东限售股份",
    },
    {
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "release_date": (datetime.date.today() + datetime.timedelta(days=20)).isoformat(),
        "release_amount": 100000000,
        "release_market_value_ratio": 3.2,
        "release_type": "定向增发机构配售股份",
    },
    {
        "stock_code": "300750",
        "stock_name": "宁德时代",
        "release_date": (datetime.date.today() + datetime.timedelta(days=8)).isoformat(),
        "release_amount": 300000000,
        "release_market_value_ratio": 7.8,
        "release_type": "首发原股东限售股份",
    },
    {
        "stock_code": "601318",
        "stock_name": "中国平安",
        "release_date": (datetime.date.today() + datetime.timedelta(days=45)).isoformat(),
        "release_amount": 200000000,
        "release_market_value_ratio": 1.5,
        "release_type": "股权激励限售股份",
    },
    {
        "stock_code": "000858",
        "stock_name": "五粮液",
        "release_date": (datetime.date.today() + datetime.timedelta(days=3)).isoformat(),
        "release_amount": 150000000,
        "release_market_value_ratio": 6.0,
        "release_type": "定向增发机构配售股份",
    },
]


def print_separator(title: str = ""):
    if title:
        print(f"\n{'='*20} {title} {'='*20}")
    else:
        print("=" * 60)


def demo_assess():
    """演示冲击评级逻辑"""
    print_separator("冲击评级演示（内置测试数据）")
    cal = EventCalendar()

    for item in DEMO_RELEASE_DATA:
        result = cal.assess_release_impact(item["stock_code"], item)
        level_tag = {"high": "[HIGH]", "medium": "[MED]", "low": "[LOW]"}.get(
            result["impact_level"], "  "
        )
        print(f"  {level_tag} | {result['stock_code']} | "
              f"解禁占比 {result['release_ratio']:.1f}% | "
              f"{result['days_until_release']}天后 | "
              f"{'⚠️风控' if result['risk_warning'] else '正常'} | "
              f"{result['description']}")


def demo_check_stock(code: str):
    """检查单只股票解禁风险"""
    print_separator(f"个股解禁检查: {code}")
    cal = EventCalendar()

    # 先从演示数据中查找
    found = [d for d in DEMO_RELEASE_DATA if d["stock_code"] == code]
    if found:
        for item in found:
            result = cal.assess_release_impact(code, item)
            print(f"  解禁日期: {result['release_date']}")
            print(f"  解禁占比: {result['release_ratio']:.1f}%")
            print(f"  冲击等级: {result['impact_level']}")
            print(f"  剩余天数: {result['days_until_release']}")
            print(f"  风险预警: {'是' if result['risk_warning'] else '否'}")
            print(f"  评估说明: {result['description']}")
    else:
        # 尝试从线上获取
        result = cal.check_stock_release_risk(code)
        if result["has_release"]:
            for evt in result["events"]:
                print(f"  {evt['release_date']} | 占比 {evt['release_ratio']:.1f}% | "
                      f"{evt['impact_level']} | {evt['description']}")
        else:
            print(f"  {code} 未来90天内无解禁事件（或数据源不可用）")


def demo_filter(codes_str: str):
    """过滤即将解禁的股票"""
    codes = [c.strip() for c in codes_str.split(",") if c.strip()]
    print_separator(f"选股排雷过滤 (输入: {codes})")
    cal = EventCalendar()

    # 用演示数据做本地过滤
    excluded = []
    filtered = []
    for code in codes:
        items = [d for d in DEMO_RELEASE_DATA if d["stock_code"] == code]
        is_excluded = False
        for item in items:
            result = cal.assess_release_impact(code, item)
            if result["impact_level"] == "high":
                excluded.append({"code": code, "reason": result["description"]})
                is_excluded = True
                break
        if not is_excluded:
            filtered.append(code)

    print(f"  输入股票: {codes}")
    print(f"  过滤结果: {filtered}")
    print(f"  排除股票: {len(excluded)}只")
    for e in excluded:
        print(f"    [X] {e['code']}: {e['reason']}")


def demo_summary():
    """解禁摘要"""
    print_separator("未来30天解禁摘要（内置测试数据）")
    cal = EventCalendar()

    high_impact = []
    weekly = {}
    today = datetime.date.today()

    for item in DEMO_RELEASE_DATA:
        result = cal.assess_release_impact(item["stock_code"], item)
        if result["days_until_release"] <= 30:
            if result["impact_level"] == "high":
                high_impact.append(result)
            week_offset = result["days_until_release"] // 7
            w_start = today + datetime.timedelta(weeks=week_offset)
            w_end = today + datetime.timedelta(weeks=week_offset + 1, days=-1)
            label = f"第{week_offset + 1}周({w_start}~{w_end})"
            weekly[label] = weekly.get(label, 0) + 1

    print(f"  即将解禁股票数: {sum(1 for d in DEMO_RELEASE_DATA if cal.assess_release_impact(d['stock_code'], d)['days_until_release'] <= 30)}")
    print(f"  高冲击股票数: {len(high_impact)}")
    for h in high_impact:
        print(f"    [HIGH] {h['stock_code']} | {h['release_ratio']:.1f}% | {h['days_until_release']}天后")
    print(f"  按周分布:")
    for label, count in sorted(weekly.items()):
        print(f"    {label}: {count}只")
    if weekly:
        peak = max(weekly, key=weekly.get)
        print(f"  解禁高峰: {peak} ({weekly[peak]}只)")


def main():
    parser = argparse.ArgumentParser(description="限售解禁事件预警 CLI")
    parser.add_argument("--code", type=str, help="检查单只股票解禁风险")
    parser.add_argument("--filter", type=str, help="过滤选股，逗号分隔代码列表")
    parser.add_argument("--summary", action="store_true", help="显示解禁摘要")
    parser.add_argument("--days", type=int, default=30, help="前瞻天数（默认30）")
    args = parser.parse_args()

    print("[INFO] 限售解禁事件预警系统")
    print(f"   日期: {datetime.date.today().isoformat()}")

    if args.code:
        demo_check_stock(args.code)
    elif args.filter:
        demo_filter(args.filter)
    elif args.summary:
        demo_summary()
    else:
        # 默认：运行全部演示
        demo_assess()
        demo_filter("002415,600519,300750")
        demo_summary()

    print_separator()
    print("[OK] 完成")


if __name__ == "__main__":
    main()
