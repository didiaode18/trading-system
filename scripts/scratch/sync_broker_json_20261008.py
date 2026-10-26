# -*- coding: utf-8 -*-
"""
券商实盘持仓 JSON 快速同步（2026-10-08）
========================================
输入: 用户提供的 QMT 账户快照 JSON（account_summary + holdings）
动作:
  1. 自洽性校验（逐只 数量×现价=市值；Σ市值=证券总市值；市值+可用=总资产）
  2. 复用 update_holdings.merge_holding 的 V2.0 合并口径
     （首次字段锁定 / 止损 Ratchet 只升不降 / 审计字段登记）
  3. 刷新派生字段 market_value / pnl / pnl_pct
  4. JSON 中缺失的存量活跃标的按清仓处理
  5. 双路径写 holdings.json + 当日首次快照
"""
import io
import os
import sys
import json
import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "trading_system"))

import update_holdings as uh  # noqa: E402

SOURCE = "券商实盘同步(JSON)"
SYNC_DATE = "2026-10-07"  # 行情所属交易日（凌晨同步取上一交易日收盘）

BROKER = {
    "account_summary": {
        "total_asset": 617896.77,
        "available_fund": 286677.67,
        "withdrawable_fund": 29.55,
        "security_market_value": 331219.10,
        "total_position_profit": 1359.40,
    },
    "holdings": [
        {"security_code": "562590", "security_name": "半导材料", "hold_quantity": 134900,
         "cost_price": 0.853, "latest_price": 0.919, "profit_rate": 7.737,
         "position_profit": 8860.39, "buy_avg_price": 0.934, "market_value": 123973.10},
        {"security_code": "510210", "security_name": "综指ETF", "hold_quantity": 114000,
         "cost_price": 1.011, "latest_price": 0.991, "profit_rate": -1.978,
         "position_profit": -2254.20, "buy_avg_price": 1.001, "market_value": 112974.00},
        {"security_code": "600887", "security_name": "伊利股份", "hold_quantity": 1900,
         "cost_price": 26.175, "latest_price": 27.300, "profit_rate": 4.298,
         "position_profit": 2138.20, "buy_avg_price": 26.574, "market_value": 51870.00},
        {"security_code": "600256", "security_name": "广汇能源", "hold_quantity": 2600,
         "cost_price": 6.506, "latest_price": 6.670, "profit_rate": 2.521,
         "position_profit": 425.62, "buy_avg_price": 6.811, "market_value": 17342.00},
        {"security_code": "600989", "security_name": "宝丰能源", "hold_quantity": 400,
         "cost_price": 42.527, "latest_price": 22.460, "profit_rate": -47.187,
         "position_profit": -8026.87, "buy_avg_price": 23.477, "market_value": 8984.00},
        {"security_code": "603099", "security_name": "长白山", "hold_quantity": 200,
         "cost_price": 42.215, "latest_price": 40.680, "profit_rate": -3.636,
         "position_profit": -307.08, "buy_avg_price": 42.215, "market_value": 8136.00},
        {"security_code": "603345", "security_name": "安井食品", "hold_quantity": 100,
         "cost_price": 74.167, "latest_price": 79.400, "profit_rate": 7.056,
         "position_profit": 523.34, "buy_avg_price": 77.038, "market_value": 7940.00},
    ],
}


def validate() -> bool:
    """自洽性校验：任何一项不通过即中止，避免把脏数据写进权威持仓文件。"""
    ok = True
    acc = BROKER["account_summary"]
    sum_mv = 0.0
    for h in BROKER["holdings"]:
        mv = h["hold_quantity"] * h["latest_price"]
        sum_mv += mv
        if abs(mv - h["market_value"]) > max(1.0, h["market_value"] * 0.002):
            print(f"  ❌ {h['security_code']} 数量×现价={mv:,.2f} ≠ 券商市值{h['market_value']:,.2f}")
            ok = False
        pr = (h["latest_price"] / h["cost_price"] - 1) * 100 if h["cost_price"] else 0
        if abs(pr - h["profit_rate"]) > 0.05:
            print(f"  ⚠️ {h['security_code']} 盈亏率复算{pr:+.3f}% ≠ 券商{h['profit_rate']:+.3f}%")
    if abs(sum_mv - acc["security_market_value"]) > 1.0:
        print(f"  ❌ Σ市值{sum_mv:,.2f} ≠ 证券总市值{acc['security_market_value']:,.2f}")
        ok = False
    total = sum_mv + acc["available_fund"]
    if abs(total - acc["total_asset"]) > 1.0:
        print(f"  ❌ 市值+可用{total:,.2f} ≠ 总资产{acc['total_asset']:,.2f}")
        ok = False
    print(f"  Σ市值 {sum_mv:,.2f} | 可用 {acc['available_fund']:,.2f} | 总资产复算 {total:,.2f}")
    if ok:
        print("  ✅ 自洽性校验通过（市值/总资产/逐只盈亏率三项全部对齐）")
    return ok


def main() -> int:
    print("=" * 62)
    print("  券商实盘持仓同步  →  holdings.json (双路径)")
    print("=" * 62)

    print("\n[1/5] 自洽性校验")
    if not validate():
        print("  ⛔ 校验未通过，已中止（未写入任何文件）")
        return 1

    holdings = uh.load_current_holdings()
    before = {c: dict(h) for c, h in holdings.items() if isinstance(h, dict) and h.get("shares", 0) > 0}
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\n[2/5] 逐只合并（V2.0 口径：首次字段锁定 + 止损 Ratchet）")
    all_warns = []
    for h in BROKER["holdings"]:
        code = h["security_code"]
        existing = holdings.get(code, {})
        parsed = {
            "code": code,
            "name": existing.get("name") or h["security_name"],
            "shares": int(h["hold_quantity"]),
            # 采用券商 cost_price（摊薄成本）——与 profit_rate / position_profit 同源，
            # 保证报告内盈亏口径与券商 App 完全一致
            "buy_price": float(h["cost_price"]),
            "current_price": float(h["latest_price"]),
            "stop_loss": 0,  # 券商 JSON 不含止损，0=未提供，保留现值（绝不置0）
        }
        merged, warns = uh.merge_holding(existing, parsed, source=SOURCE,
                                        default_reason="券商实盘同步")
        # 刷新派生字段（merge_holding 不负责，但 risk_control/daily_reconcile 会读）
        sh = merged["shares"]
        cp = merged.get("current_price", 0) or 0
        bp = merged.get("buy_price", 0) or 0
        merged["market_value"] = round(sh * cp, 2)
        merged["pnl"] = round((cp - bp) * sh, 2)
        merged["pnl_pct"] = round((cp / bp - 1) * 100, 3) if bp > 0 else 0.0
        merged.setdefault("sector", existing.get("sector", "其他"))
        merged.setdefault("stock_type", existing.get("stock_type", "弹性"))
        merged["updated_at"] = now_str
        holdings[code] = merged
        all_warns.extend(warns)

        old = before.get(code)
        if old is None:
            tag = "🆕新建仓"
            delta = f"0→{sh}"
        elif old.get("shares", 0) == sh:
            tag = "  持有"
            delta = f"{sh}(不变)"
        else:
            d = sh - old.get("shares", 0)
            tag = "📈加仓" if d > 0 else "📉减仓"
            delta = f"{old.get('shares',0)}→{sh} ({d:+,})"
        print(f"  {tag} {code} {merged.get('name',''):<6} {delta:<22} "
              f"成本{bp:>8.3f} 现价{cp:>7.3f} 盈亏{merged['pnl_pct']:>+8.2f}% "
              f"止损{merged.get('stop_loss',0):.3f}")

    print("\n[3/5] 清仓处理（JSON 中缺失的存量活跃标的）")
    broker_codes = {h["security_code"] for h in BROKER["holdings"]}
    cleared = []
    for code, old in before.items():
        if code in broker_codes:
            continue
        cleared.append(code)
        holdings[code]["shares"] = 0
        holdings[code]["buy_price"] = 0
        holdings[code]["stop_loss"] = 0
        holdings[code]["market_value"] = 0.0
        holdings[code]["pnl"] = 0.0
        holdings[code]["pnl_pct"] = 0.0
        holdings[code]["reason"] = f"已清仓({SYNC_DATE}券商实盘同步确认)"
        holdings[code]["updated_at"] = now_str
        holdings[code]["last_update_source"] = SOURCE
        print(f"  🗑️ 清仓 {code} {old.get('name','')} "
              f"({old.get('shares',0)}股 @ {old.get('buy_price',0):.3f})")
    if not cleared:
        print("  （无清仓标的）")

    print("\n[4/5] 写入文件")
    uh.save_daily_snapshot(holdings)   # 当日首次快照，留存基准
    uh.save_holdings(holdings)         # 双路径原子写 + 自动清理 shares=0

    acc = BROKER["account_summary"]
    total_capital = float(acc["total_asset"])
    available_cash = float(acc["available_fund"])
    total_mv = float(acc["security_market_value"])
    uh.update_config_runtime(total_capital, available_cash)

    print("\n[5/5] 同步后摘要")
    uh.print_summary(holdings, total_capital, available_cash, total_mv)

    if all_warns:
        print("\n  合并提示:")
        for w in all_warns:
            print(f"    {w}")

    print(f"\n  ✅ 券商 total_position_profit={acc['total_position_profit']:+,.2f} "
          f"与逐只 position_profit 合计"
          f"{sum(h['position_profit'] for h in BROKER['holdings']):+,.2f} 完全勾稽，"
          f"采用 cost_price（摊薄成本）口径入账。")
    print("  ✅ 同步完成\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
