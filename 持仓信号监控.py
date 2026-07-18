"""
持仓信号监控器
================
实时监控已持仓个股的移动止损、止盈触发、加仓条件
每日盘后运行一次，输出持仓健康度报告

使用方法:
    python 持仓信号监控.py              # 更新数据并监控
    python 持仓信号监控.py --no-update  # 使用已有数据
"""

import os
import sys
import json
import datetime
import logging

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "trading_system"))

import config
from data.data_loader import init_db, batch_update_all, load_daily_data
from strategy.trend_strategy import (
    compute_indicators, check_sell_signal,
    compute_trailing_stop
)
from risk.risk_control import RiskState, daily_risk_summary, judge_market_strength

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

HOLDINGS_FILE = os.path.join(PROJECT_ROOT, "holdings.json")


def load_holdings() -> dict:
    """加载持仓数据"""
    if not os.path.exists(HOLDINGS_FILE):
        logger.warning(f"未找到持仓文件: {HOLDINGS_FILE}")
        return {}
    with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def monitor_holdings(skip_update: bool = False):
    """
    监控所有持仓，输出健康度报告
    """
    today = datetime.date.today().strftime("%Y-%m-%d")
    holdings = load_holdings()

    if not holdings:
        print("=" * 60)
        print("  当前无持仓")
        print("=" * 60)
        return

    print("=" * 60)
    print(f"  持仓信号监控 - {today}")
    print(f"  持仓: {len(holdings)}只 | 总资金: {config.TOTAL_CAPITAL:,.0f}元")
    print("=" * 60)

    # 更新数据
    conn = init_db()
    if not skip_update:
        print("\n[1/3] 更新行情数据...")
        codes_to_update = list(holdings.keys())
        codes_to_update.append(config.BENCHMARK_INDEX)
        for code in codes_to_update:
            from data.data_loader import update_stock_to_db
            try:
                count = update_stock_to_db(conn, code)
                print(f"  {code}: 新增{count}条" if count > 0 else f"  {code}: 无新数据")
            except Exception as e:
                print(f"  {code}: 更新失败 - {e}")
    else:
        print("\n[1/3] 跳过数据更新")

    # 判定行情
    benchmark_df = load_daily_data(config.BENCHMARK_INDEX, conn, days=120)
    market_strength = judge_market_strength(benchmark_df) if not benchmark_df.empty else "normal"
    print(f"\n  行情强度: {market_strength}")

    # 逐只监控
    print("\n[2/3] 逐只持仓监控...")
    print("-" * 60)

    alerts = []       # 需要操作的提醒
    risk_state = RiskState()
    risk_state.total_capital = config.TOTAL_CAPITAL

    for code, pos in holdings.items():
        name = config.STOCK_POOL.get(code, {}).get("名称", code)
        buy_price = pos.get("buy_price", 0)
        shares = pos.get("shares", 0)
        highest = pos.get("highest_price", buy_price)

        df = load_daily_data(code, conn, days=120)
        if df.empty or len(df) < config.MA_SHORT:
            print(f"\n  {code} {name}: 数据不足")
            continue

        df = compute_indicators(df)
        latest = df.iloc[-1]
        current_price = latest["close"]

        # 更新当前价格
        pos["current_price"] = current_price
        # 更新最高价
        if current_price > highest:
            highest = current_price
            pos["highest_price"] = highest

        # 计算浮盈
        profit_pct = (current_price - buy_price) / buy_price if buy_price > 0 else 0
        profit_amount = shares * (current_price - buy_price)
        market_value = shares * current_price

        # 计算移动止损
        trailing_stop = compute_trailing_stop(buy_price, current_price)

        # 检查卖出信号
        sell_result = check_sell_signal(df, buy_price, {
            "shares": shares,
            "highest_price": highest,
            "stock_type": pos.get("stock_type", "龙头"),
            "sector": pos.get("sector", "")
        })

        # 打印持仓信息
        status_emoji = "[+]" if profit_pct >= 0 else "[-]"
        print(f"\n  {code} {name} {status_emoji}")
        print(f"    持仓: {shares}股 | 成本: {buy_price:.2f} | 现价: {current_price:.2f}")
        print(f"    浮盈: {profit_pct:.2%} ({profit_amount:+,.0f}元)")
        print(f"    市值: {market_value:,.0f}元 | 仓位占比: {market_value/config.TOTAL_CAPITAL:.1%}")
        print(f"    最高价: {highest:.2f} | 移动止损: {trailing_stop:.2f}")
        print(f"    距止损: {(current_price - trailing_stop)/current_price:.1%}")

        # 判断状态
        if sell_result["signal"]:
            print(f"    >>> [警告] {sell_result['reason']}")
            alerts.append({
                "code": code, "name": name, "type": "SELL",
                "price": sell_result["sell_price"],
                "reason": sell_result["reason"]
            })
        elif profit_pct >= config.MIN_PROFIT_TO_ADD and not pos.get("first_batch_done", True):
            print(f"    >>> [提示] 浮盈达标，可加仓第二批")
            alerts.append({
                "code": code, "name": name, "type": "ADD",
                "price": current_price,
                "reason": f"浮盈{profit_pct:.2%} >= {config.MIN_PROFIT_TO_ADD:.0%}"
            })
        else:
            # 判断止损距离
            dist_to_stop = (current_price - trailing_stop) / current_price
            if dist_to_stop < 0.03:
                print(f"    >>> [注意] 距止损线仅{dist_to_stop:.1%}，密切关注")
                alerts.append({
                    "code": code, "name": name, "type": "WARNING",
                    "price": current_price,
                    "reason": f"距移动止损仅{dist_to_stop:.1%}"
                })
            else:
                print(f"    状态: 正常持有")

        # 更新风控状态
        risk_state.current_positions[code] = pos

    # 汇总
    print("\n" + "=" * 60)
    print("  监控汇总")
    print("=" * 60)

    if alerts:
        for a in alerts:
            tag = {"SELL": "[卖出]", "ADD": "[加仓]", "WARNING": "[注意]"}
            print(f"  {tag.get(a['type'], '[?]')} {a['code']} {a['name']} @ {a['price']:.2f}")
            print(f"    {a['reason']}")
    else:
        print("  所有持仓正常，无需操作")

    # 风控摘要
    print("\n" + daily_risk_summary(risk_state, market_strength))

    # 保存更新后的持仓
    with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)
    print(f"\n  持仓数据已更新: {HOLDINGS_FILE}")

    conn.close()
    return alerts


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="持仓信号监控")
    parser.add_argument("--no-update", action="store_true", help="跳过数据更新")
    args = parser.parse_args()
    monitor_holdings(skip_update=args.no_update)
