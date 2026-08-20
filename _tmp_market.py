# -*- coding: utf-8 -*-
"""快速查看今日大盘与持仓实时行情"""
import sys, os, json
sys.path.insert(0, r'd:\workspace\trading-system\trading_system')

from data.realtime import fetch_realtime_batch, fetch_index_realtime

# 大盘指数
indices = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
}
print("=" * 70)
print("【大盘指数实时行情】")
print("=" * 70)
for code, name in indices.items():
    try:
        q = fetch_index_realtime(code)
        price = q.get('price', 0)
        change_pct = q.get('change_pct', 0)
        if price > 0:
            icon = "+" if change_pct > 0 else "-"
            print(f"  {icon} {name}: {price:.2f} ({change_pct:+.2f}%)")
        else:
            print(f"  ? {name}: 数据获取失败")
    except Exception as e:
        print(f"  ? {name}: {e}")

# 持仓行情
print("\n" + "=" * 70)
print("【持仓实时行情】")
print("=" * 70)
with open(r'd:\workspace\trading-system\holdings.json', 'r', encoding='utf-8') as f:
    holdings = json.load(f)

codes = list(holdings.keys())
quotes = fetch_realtime_batch(codes)
total_mv = 0
total_pnl = 0
print(f"  {'代码':<8s} {'名称':<8s} {'现价':>8s} {'涨跌%':>7s} {'市值':>12s} {'浮盈%':>7s}")
for code in codes:
    h = holdings[code]
    name = h['name']
    q = quotes.get(code, {})
    price = q.get('price', h.get('current_price', 0))
    change_pct = q.get('change_pct', 0)
    mv = h.get('shares', 0) * price
    buy_price = h.get('buy_price', 0)
    pnl_pct = (price / buy_price - 1) * 100 if buy_price > 0 else 0
    total_mv += mv
    total_pnl += (price - buy_price) * h.get('shares', 0)
    icon = "+" if change_pct > 0 else "-" if change_pct < 0 else "="
    print(f"  {icon} {code:<6s} {name:<8s} {price:>8.2f} {change_pct:>+6.2f}% {mv:>12,.0f} {pnl_pct:>+6.1f}%")

cash = 68840.90
total_asset = total_mv + cash
print(f"\n  持仓市值: {total_mv:,.0f}")
print(f"  可用现金: {cash:,.0f}")
print(f"  总资产:   {total_asset:,.0f}")
print(f"  总浮盈:   {total_pnl:,.0f} ({total_pnl/(total_asset-total_pnl)*100:+.2f}%)")
