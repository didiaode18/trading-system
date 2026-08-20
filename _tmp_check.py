# -*- coding: utf-8 -*-
import json

print("=" * 60)
print("【当前持仓状态】")
print("=" * 60)
with open('holdings.json', 'r', encoding='utf-8') as f:
    h = json.load(f)

# 字典结构: {code: {data}}
if isinstance(h, dict) and 'holdings' not in h:
    stocks = [{'code': k, **v} for k, v in h.items() if isinstance(v, dict)]
elif isinstance(h, dict):
    stocks = h.get('holdings', [])
else:
    stocks = h

total_mv = 0
total_cost = 0
danger_list = []

for s in stocks:
    code = s.get('code', '')
    name = s.get('name', '')
    cp = s.get('current_price', 0)
    cost = s.get('buy_price', s.get('cost_price', 0))
    shares = s.get('shares', 0)
    mv = s.get('market_value', cp * shares)
    sl = s.get('stop_loss', s.get('stop_loss_price', 0))
    
    total_mv += mv
    total_cost += cost * shares
    
    if cost > 0:
        pnl_pct = (cp / cost - 1) * 100
        dist_sl = (cp / sl - 1) * 100 if sl > 0 else 999
    else:
        pnl_pct = 0
        dist_sl = 999
    
    flag = ""
    if dist_sl < 3:
        flag = " ⚠极度接近止损"
        danger_list.append((code, name, pnl_pct, dist_sl))
    elif pnl_pct < -8:
        flag = " ⚠深套"
        danger_list.append((code, name, pnl_pct, dist_sl))
    
    print(f"  {code} {name:<8s} 浮盈={pnl_pct:+6.1f}%  距止损={dist_sl:+5.1f}%  市值={mv:>10.0f}{flag}")

if total_cost > 0:
    port_pnl = (total_mv / total_cost - 1) * 100
else:
    port_pnl = 0

print(f"\n  持仓数: {len(stocks)}")
print(f"  组合总浮盈: {port_pnl:+.2f}%")
print(f"  总市值: {total_mv:,.0f}")

if danger_list:
    print(f"\n  ⚠ 危险标的({len(danger_list)}只):")
    for c, n, p, d in danger_list:
        print(f"    {c} {n}: 浮盈{p:+.1f}%, 距止损{d:+.1f}%")

print("\n" + "=" * 60)
print("【系统风控锁定状态】")
print("=" * 60)
print("  1. 盘中监控门控: delay模式 (大盘跌-2.25%)")
print("     → warning/critical/emergency穿透发送, info级延迟")
print("     → 所有买入信号被拦截")
print("  2. 买点提醒闸门: L2级别 (跌≤-1.5%)")
print("     → 全部买点暂停推送")
print("  3. 反冲动铁律: 系统性风险锁触发")
print("     → 大盘跌>-1.5% 暂停一切买入")
print("  4. 浮亏禁加仓铁律:")
print(f"     → 组合浮盈 {port_pnl:+.2f}%, 浮亏状态下禁止加仓")

print("\n" + "=" * 60)
print("【结论: 是否可以抄底?】")
print("=" * 60)
print(f"  大盘跌幅: ~-2.5% (接近suppress阈值-2.5%)")
print(f"  组合浮盈: {port_pnl:+.2f}%")
print(f"  危险标的: {len(danger_list)}只接近止损")
print("")
print("  ❌ 绝对禁止抄底加仓")
print("")
print("  理由:")
print("  1. 大盘跌-2.5%属于系统性风险释放,非情绪错杀")
print("  2. 系统三重锁定: 门控delay+买点L2暂停+系统性风险锁")
print("  3. 组合已浮亏,多只标的逼近止损,加仓=扩大风险敞口")
print("  4. 大盘跌速快,可能继续下探,接飞刀风险极高")
print("")
print("  正确操作:")
print("  → 检查止损单是否挂好(特别是接近止损的标的)")
print("  → 浮亏>-8%的标的考虑主动减仓")
print("  → 等待大盘企稳(MA20站稳+缩量+门控解除)后再考虑")
