# -*- coding: utf-8 -*-
"""买点提醒状态诊断"""
import json, os, time, datetime

BASE = r'd:\workspace\trading-system\trading_system'

# 1. buy_alert_levels.json — 买点监控清单
print("=" * 70)
print("【买点监控清单 buy_alert_levels.json】")
print("=" * 70)
path1 = os.path.join(BASE, 'data', 'buy_alert_levels.json')
if os.path.exists(path1):
    with open(path1, 'r', encoding='utf-8') as f:
        alerts = json.load(f)
    print(f"  文件存在, 监控标的: {len(alerts)} 只")
    for code, info in alerts.items():
        buy_price = info.get('buy_price', 0)
        alert_price = info.get('alert_price', 0)
        name = info.get('name', '')
        sent = info.get('sent', False)
        valid_date = info.get('valid_date', '')
        created = info.get('created_at', '')
        print(f"  {code} {name:<8s} 买点={buy_price:.2f} 提醒价={alert_price:.2f} "
              f"已发送={'是' if sent else '否'} 生效日={valid_date} 创建={created}")
else:
    print(f"  文件不存在 ❌")

# 2. buy_point_alert_sent.json — 已发送记录
print("\n" + "=" * 70)
print("【已发送记录 buy_point_alert_sent.json】")
print("=" * 70)
path2 = os.path.join(BASE, 'data', 'buy_point_alert_sent.json')
if os.path.exists(path2):
    with open(path2, 'r', encoding='utf-8') as f:
        sent = json.load(f)
    today = datetime.date.today().isoformat()
    today_sent = {k: v for k, v in sent.items() if today in str(v)}
    print(f"  总记录: {len(sent)} 条, 今日({today})已发送: {len(today_sent)} 条")
    for code, info in today_sent.items():
        print(f"  {code}: {info}")
    if not today_sent:
        print(f"  今日暂无发送记录")
else:
    print(f"  文件不存在")

# 3. 检查持仓买点匹配
print("\n" + "=" * 70)
print("【持仓 vs 买点匹配检查】")
print("=" * 70)
holdings_path = os.path.join(BASE, 'holdings.json')
if os.path.exists(holdings_path):
    with open(holdings_path, 'r', encoding='utf-8') as f:
        holdings = json.load(f)

# 获取实时行情对比
try:
    import sys
    sys.path.insert(0, BASE)
    from data.realtime import get_realtime_quotes
    codes = list(holdings.keys())
    quotes = get_realtime_quotes(codes)
    
    print(f"  {'代码':<8s} {'名称':<8s} {'现价':>8s} {'买点':>8s} {'差距':>8s} {'状态':<12s}")
    for code, h in holdings.items():
        name = h.get('name', '')
        current = quotes.get(code, {}).get('price', 0)
        if current <= 0:
            current = h.get('current_price', 0)
        
        # 检查是否在买点清单中
        if code in alerts:
            buy_p = alerts[code].get('buy_price', 0)
            alert_p = alerts[code].get('alert_price', 0)
            if buy_p > 0:
                gap_pct = (current / buy_p - 1) * 100
                if current <= alert_p:
                    status = "已触及买点!"
                elif current <= buy_p * 1.02:
                    status = "接近买点"
                else:
                    status = f"距买点{gap_pct:+.1f}%"
            else:
                gap_pct = 0
                status = "买点未设"
        else:
            buy_p = 0
            gap_pct = 0
            status = "未监控"
        
        print(f"  {code:<8s} {name:<8s} {current:>8.2f} {buy_p:>8.2f} {gap_pct:>+7.1f}% {status}")
except Exception as e:
    print(f"  行情获取失败: {e}")
    for code, h in holdings.items():
        print(f"  {code} {h.get('name','')} (无实时行情)")

# 4. 调度器日志中的买点记录
print("\n" + "=" * 70)
print("【调度器日志-买点相关】")
print("=" * 70)
daemon_out = os.path.join(BASE, 'logs', 'scheduler_daemon.out')
if os.path.exists(daemon_out):
    with open(daemon_out, 'r', encoding='gbk', errors='replace') as f:
        lines = f.readlines()
    today_lines = [l for l in lines if '2026-08-20' in l]
    buy_lines = [l for l in today_lines if '买点' in l or 'buy_point' in l or 'buy_alert' in l]
    if buy_lines:
        for l in buy_lines[-15:]:
            print(f"  {l.strip()}")
    else:
        print(f"  今日日志中无买点相关记录（09:35后开始监控）")
else:
    print(f"  daemon.out 不存在")

# 5. 配置检查
print("\n" + "=" * 70)
print("【买点提醒配置】")
print("=" * 70)
try:
    sys.path.insert(0, BASE)
    import config
    print(f"  BUY_POINT_ALERT_ENABLED: {getattr(config, 'BUY_POINT_ALERT_ENABLED', '未定义')}")
    print(f"  BUY_POINT_ALERT_EMAIL_FALLBACK: {getattr(config, 'BUY_POINT_ALERT_EMAIL_FALLBACK', '未定义')}")
    print(f"  WECHAT_WORK_WEBHOOK: {'已配置' if getattr(config, 'WECHAT_WORK_WEBHOOK', '') else '未配置'}")
    print(f"  DINGTALK_WEBHOOK: {'已配置' if getattr(config, 'DINGTALK_WEBHOOK', '') else '未配置'}")
except Exception as e:
    print(f"  配置读取失败: {e}")
