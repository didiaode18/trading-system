# -*- coding: utf-8 -*-
"""持仓同步V14：券商JSON → holdings.json + config_local.py"""
import json, os, shutil, datetime

# ====== 券商原始数据 ======
ACCOUNT = {"总资产": 645965.75, "可用资金": 233523.25, "证券总市值": 412442.40, "累计持仓盈亏": 2696.80}
POSITIONS = [
    {"code":"562590","name":"半导材料","shares":63300,"cost":1.000,"price":1.038,"mv":65705.40,"pnl":2395.63,"pnl_pct":3.800},
    {"code":"002603","name":"以岭药业","shares":3400,"cost":16.838,"price":17.380,"mv":59092.00,"pnl":1842.88,"pnl_pct":3.219},
    {"code":"159611","name":"电力ETF","shares":55500,"cost":1.063,"price":1.028,"mv":57054.00,"pnl":-1948.16,"pnl_pct":-3.293},
    {"code":"600760","name":"中航沈飞","shares":1300,"cost":43.489,"price":43.750,"mv":56875.00,"pnl":339.86,"pnl_pct":0.600},
    {"code":"000661","name":"长春高新","shares":600,"cost":84.060,"price":84.640,"mv":50784.00,"pnl":347.91,"pnl_pct":0.690},
    {"code":"601919","name":"中远海控","shares":2500,"cost":14.922,"price":16.570,"mv":41425.00,"pnl":4120.54,"pnl_pct":11.044},
    {"code":"001979","name":"招商蛇口","shares":4800,"cost":7.532,"price":7.670,"mv":36816.00,"pnl":662.96,"pnl_pct":1.832},
    {"code":"600256","name":"广汇能源","shares":4600,"cost":5.636,"price":6.150,"mv":28290.00,"pnl":2365.63,"pnl_pct":9.120},
    {"code":"000506","name":"招金黄金","shares":400,"cost":19.683,"price":19.700,"mv":7880.00,"pnl":7.00,"pnl_pct":0.086},
    {"code":"600206","name":"有研新材","shares":100,"cost":63.189,"price":56.440,"mv":5644.00,"pnl":-674.89,"pnl_pct":-10.681},
    {"code":"603221","name":"爱丽家居","shares":100,"cost":34.084,"price":28.770,"mv":2877.00,"pnl":-531.37,"pnl_pct":-15.591},
    {"code":"002156","name":"通富微电","shares":0,"cost":0,"price":64.130,"mv":0,"pnl":-2716.03,"pnl_pct":0},
    {"code":"600809","name":"山西汾酒","shares":0,"cost":0,"price":118.440,"mv":0,"pnl":-3515.16,"pnl_pct":0},
]

# ====== 旧持仓 ======
with open(r'd:\workspace\trading-system\holdings.json', 'r', encoding='utf-8') as f:
    old = json.load(f)

# ====== 勾稽校验 ======
print("=" * 60)
print("【勾稽校验】")
print("=" * 60)
active = [p for p in POSITIONS if p["shares"] > 0]
cleared = [p for p in POSITIONS if p["shares"] == 0]
total_mv = sum(p["mv"] for p in active)
print(f"  活跃标的: {len(active)} 只, 清仓标的: {len(cleared)} 只")
print(f"  市值合计: {total_mv:,.2f} (账户: {ACCOUNT['证券总市值']:,.2f}) -> {'PASS' if abs(total_mv - ACCOUNT['证券总市值']) < 1 else 'FAIL'}")
cash = ACCOUNT['总资产'] - ACCOUNT['证券总市值']
print(f"  现金: {cash:,.2f}")
print(f"  现金+市值: {cash + total_mv:,.2f} (总资产: {ACCOUNT['总资产']:,.2f}) -> {'PASS' if abs(cash + total_mv - ACCOUNT['总资产']) < 1 else 'FAIL'}")

# ====== Diff 分析 ======
print("\n" + "=" * 60)
print("【Diff 分析】")
print("=" * 60)
for p in POSITIONS:
    code = p["code"]
    if code in old:
        old_shares = old[code].get("shares", 0)
        if p["shares"] == 0 and old_shares > 0:
            print(f"  清仓 {code} {p['name']} ({old_shares}->0股)")
        elif p["shares"] != old_shares:
            print(f"  变更 {code} {p['name']}: {old_shares}->{p['shares']}股")
    else:
        if p["shares"] > 0:
            print(f"  新增 {code} {p['name']}: {p['shares']}股")

# ====== 构建新 holdings.json ======
now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
SECTOR_MAP = {
    "000661": ("医药", "龙头"), "002603": ("医药", "弹性"),
    "001979": ("房地产", "龙头"), "600256": ("能源", "弹性"),
    "601919": ("航运", "弹性"), "600760": ("军工", "龙头"),
    "159611": ("指数ETF", "弹性"), "600206": ("半导体材料", "弹性"),
    "603221": ("家居", "弹性"), "562590": ("半导体材料", "弹性"),
    "000506": ("黄金", "弹性"),
}
BUY_DATE_MAP = {
    "000661": "2026-08-16", "002603": "2026-08-16",
    "001979": "2026-08-17", "600256": "2026-08-16",
    "601919": "2026-08-16", "600760": "2026-08-16",
    "159611": "2026-08-16", "600206": "2026-08-19",
    "603221": "2026-08-19", "562590": "2026-08-20",
    "000506": "2026-08-20",
}

new_holdings = {}
for p in active:
    code = p["code"]
    sector, stock_type = SECTOR_MAP.get(code, ("其他", "弹性"))
    buy_date = BUY_DATE_MAP.get(code, "2026-08-20")
    buy_price = p["cost"]

    # 止损：Ratchet原则
    if code in old:
        old_sl = old[code].get("stop_loss", 0)
        new_sl_calc = round(buy_price * 0.9, 3)
        stop_loss = max(old_sl, new_sl_calc) if old_sl > 0 else new_sl_calc
        rev = old[code].get("revision_count", 0) + 1
    else:
        stop_loss = round(buy_price * 0.9, 3)
        rev = 1

    new_holdings[code] = {
        "name": p["name"],
        "shares": p["shares"],
        "buy_price": buy_price,
        "current_price": p["price"],
        "sector": sector,
        "stock_type": stock_type,
        "stop_loss": stop_loss,
        "trailing_stop": stop_loss,
        "buy_date": buy_date,
        "market_value": p["mv"],
        "pnl": p["pnl"],
        "pnl_pct": p["pnl_pct"],
        "updated_at": now_str,
        "revision_count": rev,
        "last_update_source": "券商实盘同步"
    }

# ====== 写入 ======
src = r'd:\workspace\trading-system\holdings.json'
backup = f'd:\\workspace\\trading-system\\holdings_backup_{datetime.date.today().strftime("%Y%m%d")}.json'
shutil.copy2(src, backup)
print(f"\n  备份: {backup}")

with open(src, 'w', encoding='utf-8') as f:
    json.dump(new_holdings, f, ensure_ascii=False, indent=2)
print(f"  写入: {src} ({len(new_holdings)}只)")

src2 = r'd:\workspace\trading-system\trading_system\holdings.json'
with open(src2, 'w', encoding='utf-8') as f:
    json.dump(new_holdings, f, ensure_ascii=False, indent=2)
print(f"  同步: {src2}")

# ====== 汇总 ======
print("\n" + "=" * 60)
print("【更新汇总】")
print("=" * 60)
print(f"  持仓数: {len(old)} -> {len(new_holdings)}")
print(f"  总资产: {ACCOUNT['总资产']:,.2f}")
print(f"  证券市值: {ACCOUNT['证券总市值']:,.2f}")
print(f"  可用现金: {cash:,.2f}")
print(f"  累计盈亏: {ACCOUNT['累计持仓盈亏']:,.2f}")
print(f"\n  各标的:")
for code, h in new_holdings.items():
    sl_dist = (h['current_price'] / h['stop_loss'] - 1) * 100 if h['stop_loss'] > 0 else 0
    flag = " !!接近止损" if sl_dist < 3 else ""
    print(f"    {code} {h['name']:<8s} {h['shares']:>6d}股 浮盈{h['pnl_pct']:+6.1f}% 距止损{sl_dist:+5.1f}%{flag}")
