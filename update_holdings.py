# -*- coding: utf-8 -*-
"""
每日持仓更新工具 V1.0
======================
收盘后运行，将最新持仓数据喂入系统，自动更新:
  1. holdings.json（持仓明细）
  2. config.TOTAL_CAPITAL / AVAILABLE_CASH（运行时覆盖）
  3. 同步根目录与trading_system/下的holdings.json

使用方式:
  方式1（交互式）: python update_holdings.py
  方式2（命令行）: python update_holdings.py --cash 250000 --input holdings_input.txt
  方式3（单只更新）: python update_holdings.py --code 002371 --shares 400 --price 718.5

输入格式（交互式/文件，每行一只）:
  代码,数量,成本价,现价,止损价
  002371,400,753.947,718.5,697
  002415,2300,34.301,36.14,34.5

  特殊指令:
  - 输入 "del 代码" 删除某只持仓（已清仓）
  - 输入 "done" 结束输入
"""

import os
import sys
import json
import datetime
import argparse
import shutil

# 路径设置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADING_SYSTEM_DIR = os.path.join(SCRIPT_DIR, "trading_system")
sys.path.insert(0, TRADING_SYSTEM_DIR)

import config


def get_holdings_paths():
    """获取所有需要同步的holdings.json路径"""
    paths = []
    # 主路径: trading_system/holdings.json
    primary = os.path.join(TRADING_SYSTEM_DIR, "holdings.json")
    paths.append(primary)
    # 根目录路径: holdings.json（兼容旧脚本）
    legacy = os.path.join(SCRIPT_DIR, "holdings.json")
    if legacy != primary:
        paths.append(legacy)
    return paths


def load_current_holdings():
    """加载当前holdings.json"""
    holdings_file = config.get_holdings_file()
    if os.path.exists(holdings_file):
        with open(holdings_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_holdings(holdings: dict):
    """保存holdings.json到所有路径（保持同步）"""
    paths = get_holdings_paths()
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(holdings, f, ensure_ascii=False, indent=2)
    print(f"  ✅ holdings.json 已更新 ({len(paths)}个路径同步)")


def update_config_runtime(total_capital: float, available_cash: float):
    """运行时覆盖config中的资金参数（不修改config.py文件）"""
    config.TOTAL_CAPITAL = total_capital
    config.AVAILABLE_CASH = available_cash
    print(f"  ✅ config.TOTAL_CAPITAL = {total_capital:,.2f}")
    print(f"  ✅ config.AVAILABLE_CASH = {available_cash:,.2f}")


def parse_input_line(line: str) -> dict:
    """解析一行输入: 代码,数量,成本价,现价,止损价"""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    try:
        return {
            "code": parts[0],
            "shares": int(float(parts[1])),
            "buy_price": float(parts[2]),
            "current_price": float(parts[3]),
            "stop_loss": float(parts[4]) if len(parts) > 4 and parts[4] else 0,
        }
    except (ValueError, IndexError):
        return None


def interactive_mode():
    """交互式输入持仓"""
    print("\n" + "=" * 55)
    print("  📊 每日持仓更新工具 V1.0")
    print("  格式: 代码,数量,成本价,现价,止损价")
    print("  指令: 'del 代码'=删除 | 'done'=完成")
    print("=" * 55)

    holdings = load_current_holdings()
    print(f"\n  当前持仓 {sum(1 for v in holdings.values() if v.get('shares', 0) > 0)} 只:")
    for code, h in holdings.items():
        if h.get("shares", 0) > 0:
            print(f"    {code} {h.get('name', '')} {h['shares']}股 成本{h.get('buy_price', 0):.3f}")

    print("\n  请输入最新持仓（逐行输入，done结束）:")
    updated_codes = set()

    while True:
        try:
            line = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue
        if line.lower() == "done":
            break

        # 删除指令
        if line.lower().startswith("del "):
            del_code = line[4:].strip()
            if del_code in holdings:
                holdings[del_code]["shares"] = 0
                holdings[del_code]["buy_price"] = 0
                holdings[del_code]["stop_loss"] = 0
                holdings[del_code]["reason"] = f"已清仓({datetime.date.today()}手动更新)"
                print(f"    🗑️  已删除: {del_code}")
            else:
                print(f"    ⚠️ 未找到: {del_code}")
            continue

        # 解析持仓行
        parsed = parse_input_line(line)
        if not parsed:
            print("    ⚠️ 格式错误，请用: 代码,数量,成本价,现价,止损价")
            continue

        code = parsed["code"]
        # 获取股票名称（从现有数据或config）
        existing = holdings.get(code, {})
        name = existing.get("name", "")
        if not name:
            name = config.get_stock_name(code) if hasattr(config, 'get_stock_name') else code

        holdings[code] = {
            "name": name,
            "shares": parsed["shares"],
            "buy_price": parsed["buy_price"],
            "current_price": parsed["current_price"],
            "stop_loss": parsed["stop_loss"],
            "highest": max(existing.get("highest", 0), parsed["current_price"]),
            "buy_date": existing.get("buy_date", datetime.date.today().isoformat()),
            "reason": existing.get("reason", "手动更新"),
            "sector": existing.get("sector", "其他"),
        }
        updated_codes.add(code)
        print(f"    ✓ {code} {name} {parsed['shares']}股 成本{parsed['buy_price']:.3f} 现价{parsed['current_price']:.3f}")

    return holdings, updated_codes


def file_mode(input_file: str):
    """从文件读取持仓"""
    holdings = load_current_holdings()
    updated_codes = set()

    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("del "):
                del_code = line[4:].strip()
                if del_code in holdings:
                    holdings[del_code]["shares"] = 0
                    holdings[del_code]["buy_price"] = 0
                    holdings[del_code]["stop_loss"] = 0
                    holdings[del_code]["reason"] = f"已清仓({datetime.date.today()}批量更新)"
                continue

            parsed = parse_input_line(line)
            if not parsed:
                continue
            code = parsed["code"]
            existing = holdings.get(code, {})
            name = existing.get("name", config.get_stock_name(code) if hasattr(config, 'get_stock_name') else code)

            holdings[code] = {
                "name": name,
                "shares": parsed["shares"],
                "buy_price": parsed["buy_price"],
                "current_price": parsed["current_price"],
                "stop_loss": parsed["stop_loss"],
                "highest": max(existing.get("highest", 0), parsed["current_price"]),
                "buy_date": existing.get("buy_date", datetime.date.today().isoformat()),
                "reason": existing.get("reason", "批量更新"),
                "sector": existing.get("sector", "其他"),
            }
            updated_codes.add(code)

    return holdings, updated_codes


def single_mode(code: str, shares: int, cost: float, price: float, stop_loss: float = 0):
    """单只更新模式"""
    holdings = load_current_holdings()
    existing = holdings.get(code, {})
    name = existing.get("name", config.get_stock_name(code) if hasattr(config, 'get_stock_name') else code)

    holdings[code] = {
        "name": name,
        "shares": shares,
        "buy_price": cost,
        "current_price": price,
        "stop_loss": stop_loss,
        "highest": max(existing.get("highest", 0), price),
        "buy_date": existing.get("buy_date", datetime.date.today().isoformat()),
        "reason": existing.get("reason", "单只更新"),
        "sector": existing.get("sector", "其他"),
    }
    return holdings, {code}


def calc_totals(holdings: dict, available_cash: float = None):
    """计算总资产和可用现金"""
    total_market_value = sum(
        h.get("shares", 0) * h.get("current_price", h.get("buy_price", 0))
        for h in holdings.values()
        if h.get("shares", 0) > 0
    )

    if available_cash is None:
        # 未指定现金时，用 config.TOTAL_CAPITAL - 持仓市值 推算（保持总资产不变）
        # 但如果持仓变化大，提示用户手动输入
        old_capital = getattr(config, 'TOTAL_CAPITAL', 0)
        available_cash = max(0, old_capital - total_market_value)

    total_capital = total_market_value + available_cash
    return total_capital, available_cash, total_market_value


def print_summary(holdings: dict, total_capital: float, available_cash: float, total_mv: float):
    """打印更新摘要"""
    active = {c: h for c, h in holdings.items() if h.get("shares", 0) > 0}
    total_cost = sum(h["shares"] * h.get("buy_price", 0) for h in active.values())
    total_pnl = total_mv - total_cost
    position_ratio = total_mv / total_capital * 100 if total_capital > 0 else 0

    print("\n" + "=" * 55)
    print("  📋 持仓更新摘要")
    print("=" * 55)
    print(f"  总资产:     {total_capital:>12,.2f} 元")
    print(f"  持仓市值:   {total_mv:>12,.2f} 元 ({position_ratio:.1f}%)")
    print(f"  可用现金:   {available_cash:>12,.2f} 元 ({100-position_ratio:.1f}%)")
    print(f"  持仓盈亏:   {total_pnl:>+12,.2f} 元 ({total_pnl/total_cost*100 if total_cost > 0 else 0:+.1f}%)")
    print(f"  活跃持仓:   {len(active)} 只")
    print("-" * 55)

    for code, h in sorted(active.items(), key=lambda x: x[1]["shares"] * x[1].get("current_price", 0), reverse=True):
        mv = h["shares"] * h.get("current_price", 0)
        pnl_pct = (h.get("current_price", 0) / h.get("buy_price", 1) - 1) * 100 if h.get("buy_price", 0) > 0 else 0
        ratio = mv / total_capital * 100 if total_capital > 0 else 0
        flag = "⚠️超限" if ratio > 15 else ""
        print(f"  {code} {h.get('name', ''):<6} {h['shares']:>6}股 "
              f"市值{mv/10000:>6.1f}万 占比{ratio:>5.1f}% "
              f"盈亏{pnl_pct:>+6.1f}% {flag}")

    # 风险检查
    print("-" * 55)
    alerts = []
    if position_ratio > 80:
        alerts.append(f"⚠️ 总仓位{position_ratio:.1f}%过高(>80%)，建议减至60%以下")
    if available_cash < total_capital * 0.05:
        alerts.append(f"⚠️ 现金仅{available_cash:.0f}元(<5%)，无应急缓冲")
    for code, h in active.items():
        mv = h["shares"] * h.get("current_price", 0)
        if mv / total_capital > 0.15:
            alerts.append(f"⚠️ {h.get('name', code)}占比{mv/total_capital*100:.1f}%>15%上限")
    if alerts:
        for a in alerts:
            print(f"  {a}")
    else:
        print("  ✅ 仓位结构健康，无超限预警")
    print("=" * 55)


def main():
    parser = argparse.ArgumentParser(description="每日持仓更新工具")
    parser.add_argument("--cash", type=float, default=None, help="可用现金(元)")
    parser.add_argument("--input", type=str, default=None, help="持仓输入文件路径")
    parser.add_argument("--code", type=str, default=None, help="单只更新: 股票代码")
    parser.add_argument("--shares", type=int, default=None, help="单只更新: 数量")
    parser.add_argument("--cost", type=float, default=0, help="单只更新: 成本价")
    parser.add_argument("--price", type=float, default=None, help="单只更新: 现价")
    parser.add_argument("--stop", type=float, default=0, help="单只更新: 止损价")
    parser.add_argument("--show", action="store_true", help="仅显示当前持仓，不修改")
    args = parser.parse_args()

    # 仅显示模式
    if args.show:
        holdings = load_current_holdings()
        total_capital, available_cash, total_mv = calc_totals(holdings, args.cash)
        print_summary(holdings, total_capital, available_cash, total_mv)
        return

    # 选择输入模式
    if args.code:
        if args.shares is None or args.price is None:
            print("❌ 单只模式需要 --shares 和 --price 参数")
            return
        holdings, updated = single_mode(args.code, args.shares, args.cost, args.price, args.stop)
    elif args.input:
        if not os.path.exists(args.input):
            print(f"❌ 文件不存在: {args.input}")
            return
        holdings, updated = file_mode(args.input)
    else:
        holdings, updated = interactive_mode()

    if not updated and not args.code:
        print("\n  未输入任何更新，退出。")
        return

    # 计算总资产
    total_capital, available_cash, total_mv = calc_totals(holdings, args.cash)

    # 如果用户未指定cash且持仓变化大，提示确认
    if args.cash is None:
        print(f"\n  💡 计算得可用现金: {available_cash:,.2f}元")
        try:
            confirm = input("  按回车确认，或输入实际可用现金: ").strip()
            if confirm:
                available_cash = float(confirm)
                total_capital = total_mv + available_cash
        except (EOFError, KeyboardInterrupt):
            pass

    # 保存
    save_holdings(holdings)
    update_config_runtime(total_capital, available_cash)

    # 打印摘要
    print_summary(holdings, total_capital, available_cash, total_mv)

    print(f"\n  📅 更新时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("  ✅ 完成！16:15综合分析报告将使用最新数据。\n")


if __name__ == "__main__":
    main()

