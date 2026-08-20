# -*- coding: utf-8 -*-
# 深度追踪 002156 预警链路

# 1. 读取日志 - 尝试多种编码
for enc in ['utf-8', 'gbk', 'gb18030', 'latin-1']:
    try:
        with open(r'd:\workspace\trading-system\trading_system\logs\scheduler_daemon.out', 'r', encoding=enc, errors='replace') as f:
            lines = f.readlines()
        # 检查是否能正确显示中文
        sample = ''.join(lines[:5])
        if 'INFO' in sample:
            print(f"日志编码: {enc}")
            break
    except:
        continue

print("=" * 70)
print("【002156 通富微电 今日全部预警记录】")
print("=" * 70)

for i, line in enumerate(lines):
    if '002156' in line and '2026-08-19' in line:
        # 只取 intraday_monitor 的日志（预警链路核心）
        if 'intraday_monitor' in line:
            print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【大盘门控状态变迁（今日）】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and 'intraday_monitor' in line:
        if '门控' in line or 'gate' in line.lower() or 'delay' in line or 'pass' in line or 'suppress' in line:
            print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【_send_alert 调用记录（今日）】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and ('_send_alert' in line or '发送' in line or '延迟' in line or '入队列' in line or '穿透' in line):
        if '002156' in line or '通富' in line:
            print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【预警延迟队列补发记录】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and ('补发' in line or '延迟补发' in line or 'delayed_queue' in line or '队列' in line):
        print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【调度器重启记录】")
print("=" * 70)
for i, line in enumerate(lines):
    if '启动' in line or 'started' in line.lower() or 'PID' in line or 'pid' in line or '调度器' in line:
        if '2026-08-19' in line:
            print(f"L{i+1}: {line.rstrip()}")
