# -*- coding: utf-8 -*-
"""收集V5详细统计数据"""
import sys, os, sqlite3, warnings
warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_system'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import pandas as pd
import numpy as np
from backtest_real import backtest_stock_v5, analyze_trades

conn = sqlite3.connect(config.DB_PATH)
df_all = pd.read_sql('SELECT code, date, open, close, high, low, volume FROM daily_kline ORDER BY code, date ASC', conn)
conn.close()

data_dict = {}
for code, group in df_all.groupby('code'):
    df = group.copy()
    for col in ['open','close','high','low','volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['close'])
    df = df[df['volume']>0].reset_index(drop=True)
    df = df[df['date']>='2022-10-01'].reset_index(drop=True)
    if len(df) > 100:
        data_dict[code] = df

all_trades = []
for code in data_dict:
    if code == '000300': continue
    df = data_dict[code]
    info = config.get_stock_info(code)
    info_dict = {"名称": info.get("名称", code), "类型": info.get("类型", "龙头"), "行业": info.get("赛道", "其他")}
    try:
        trades = backtest_stock_v5(df, code, info_dict)
        all_trades.extend(trades)
    except: pass

stats = analyze_trades(all_trades)
print('=== V5 FULL STATS ===')
for k, v in sorted(stats.items()):
    if isinstance(v, (int, float, str)):
        print(f'  {k}: {v}')

print('\n=== SELL REASON ===')
sell_stats = {}
for t in all_trades:
    r = t.get('sell_reason', 'unknown')
    if r not in sell_stats:
        sell_stats[r] = {'count':0, 'wins':0, 'total':0}
    sell_stats[r]['count'] += 1
    if t['net_profit'] > 0: sell_stats[r]['wins'] += 1
    sell_stats[r]['total'] += t['net_profit']
for r, d in sorted(sell_stats.items(), key=lambda x: -x[1]['count']):
    wr = d['wins']/d['count']*100 if d['count']>0 else 0
    avg = d['total']/d['count'] if d['count']>0 else 0
    pct = d['count']/len(all_trades)*100
    print(f'  {r:25s}: {d["count"]:>4d}笔({pct:>4.1f}%), WR={wr:>5.1f}%, avg={avg:>+.2f}%')

print('\n=== REGIME ===')
regime_stats = {}
for t in all_trades:
    rg = t.get('regime', 'RANGE')
    if rg not in regime_stats:
        regime_stats[rg] = {'count':0, 'wins':0, 'total':0}
    regime_stats[rg]['count'] += 1
    if t['net_profit'] > 0: regime_stats[rg]['wins'] += 1
    regime_stats[rg]['total'] += t['net_profit']
for rg in ['BULL','RANGE','BEAR']:
    if rg in regime_stats:
        s = regime_stats[rg]
        wr = s['wins']/s['count']*100 if s['count']>0 else 0
        avg = s['total']/s['count'] if s['count']>0 else 0
        print(f'  {rg:6s}: {s["count"]:>4d}笔, WR={wr:>5.1f}%, avg={avg:>+.2f}%, total={s["total"]:>+.1f}%')

print('\n=== HALF-YEAR ===')
hy = {}
for t in all_trades:
    bd = t.get('buy_date','')
    if len(bd)>=7:
        half = 'H1' if int(bd[5:7])<=6 else 'H2'
        pk = bd[:4] + half
        if pk not in hy: hy[pk] = {'count':0, 'wins':0, 'total':0}
        hy[pk]['count'] += 1
        if t['net_profit']>0: hy[pk]['wins'] += 1
        hy[pk]['total'] += t['net_profit']
for pk in sorted(hy.keys()):
    s = hy[pk]
    wr = s['wins']/s['count']*100 if s['count']>0 else 0
    avg = s['total']/s['count'] if s['count']>0 else 0
    print(f'  {pk}: {s["count"]:>4d}笔, WR={wr:>5.1f}%, total={s["total"]:>+.1f}%, avg={avg:>+.2f}%')

print('\n=== TOP STOCKS ===')
ss = {}
for t in all_trades:
    key = t['code'] + '|' + t.get('name', t['code'])
    if key not in ss:
        ss[key] = {'count':0, 'wins':0, 'total':0}
    ss[key]['count'] += 1
    if t['net_profit'] > 0: ss[key]['wins'] += 1
    ss[key]['total'] += t['net_profit']
ranked = sorted(ss.items(), key=lambda x: -x[1]['total'])
print('  Top10 盈利:')
for key, s in ranked[:10]:
    code, name = key.split('|')
    wr = s['wins']/s['count']*100 if s['count']>0 else 0
    print(f'    {code} {name:10s}: {s["count"]:>3d}笔, WR={wr:>5.1f}%, total={s["total"]:>+.1f}%')
print('  Top10 亏损:')
for key, s in ranked[-10:]:
    code, name = key.split('|')
    wr = s['wins']/s['count']*100 if s['count']>0 else 0
    print(f'    {code} {name:10s}: {s["count"]:>3d}笔, WR={wr:>5.1f}%, total={s["total"]:>+.1f}%')
