# -*- coding: utf-8 -*-
"""检查沪深300最新K线数据和报告生成情况"""
import sys, os, json, datetime
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, 'trading_system')

import pandas as pd
from data.data_loader import init_db, load_daily_data

print("=" * 70)
print("1. 检查沪深300(000300)最近K线数据")
print("=" * 70)

conn = init_db()
df = load_daily_data('000300', conn, days=10)
conn.close()

if df.empty:
    print("  ERROR: 无法加载000300数据")
else:
    print(f"  数据条数: {len(df)}")
    print(f"  最新日期: {df.iloc[-1]['date']}")
    print(f"\n  最近5个交易日K线:")
    print(f"  {'日期':<12} {'开盘':>10} {'收盘':>10} {'最高':>10} {'最低':>10} {'涨跌%':>8}")
    print("  " + "-" * 65)
    
    for i in range(max(0, len(df)-5), len(df)):
        r = df.iloc[i]
        chg_pct = ""
        if i > 0:
            prev_close = df.iloc[i-1]['close']
            chg = (r['close'] - prev_close) / prev_close * 100
            chg_pct = f"{chg:+.2f}%"
        print(f"  {r['date']:<12} {r['open']:>10.2f} {r['close']:>10.2f} {r['high']:>10.2f} {r['low']:>10.2f} {chg_pct:>8}")

print(f"\n{'=' * 70}")
print("2. 检查last_update表中000300的最新更新日期")
print("=" * 70)

conn = init_db()
cursor = conn.cursor()
cursor.execute("SELECT code, last_date FROM last_update WHERE code='000300'")
row = cursor.fetchone()
conn.close()

if row:
    print(f"  000300 last_update: code={row[0]}, last_date={row[1]}")
    last_date = datetime.datetime.strptime(row[1], "%Y-%m-%d").date()
    today = datetime.date.today()
    days_stale = (today - last_date).days
    print(f"  距今: {days_stale}天")
    if days_stale > 1:
        print(f"  WARNING: 数据已过时{days_stale}天!")
else:
    print("  000300 不在last_update表中")

print(f"\n{'=' * 70}")
print("3. 检查报告输出文件")
print("=" * 70)

report_path = os.path.join('trading_system', 'output', 'holdings_analysis_20260827.html')
if os.path.exists(report_path):
    size = os.path.getsize(report_path)
    mtime = os.path.getmtime(report_path)
    mtime_dt = datetime.datetime.fromtimestamp(mtime)
    print(f"  报告文件: {report_path}")
    print(f"  大小: {size} bytes")
    print(f"  生成时间: {mtime_dt}")
    
    # 搜索报告中的大盘状态描述
    with open(report_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 查找大盘相关关键词
    for keyword in ['震荡', '牛市', '熊市', '平衡', '强势', '弱势', 'regime', '大盘状态', '市场环境']:
        idx = content.find(keyword)
        if idx >= 0:
            # 提取上下文
            start = max(0, idx - 30)
            end = min(len(content), idx + 80)
            snippet = content[start:end].replace('\n', ' ').strip()
            print(f"  找到 '{keyword}': ...{snippet}...")
else:
    print(f"  报告文件不存在: {report_path}")

# 也检查8/28的报告
report_path_28 = os.path.join('trading_system', 'output', 'holdings_analysis_20260828.html')
if os.path.exists(report_path_28):
    print(f"\n  8/28报告也已生成")
else:
    print(f"\n  8/28报告尚未生成")
