# -*- coding: utf-8 -*-
import re

# 读取 daemon.out 日志
with open(r'd:\workspace\trading-system\trading_system\logs\scheduler_daemon.out', 'r', encoding='utf-8', errors='replace') as f:
    lines = f.readlines()

print("=" * 70)
print("【002156 通富微电 今日预警链路追踪】")
print("=" * 70)

# 过滤 002156 相关日志
for i, line in enumerate(lines):
    if '002156' in line and '2026-08-19' in line:
        print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【门控状态变迁】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and ('门控' in line or 'gate' in line.lower() or 'delay' in line or 'suppress' in line or 'pass' in line):
        # 只打印关键状态转换
        if 'pass→' in line or '→delay' in line or '→pass' in line or '→suppress' in line or '门控状态' in line:
            print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【_send_alert 相关日志（预警发送记录）】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and ('_send_alert' in line or '发送预警' in line or '延迟预警' in line or '延迟补发' in line or '门控延迟' in line or '穿透' in line):
        print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【梯度减仓/止损相关】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and ('梯度' in line or '止损' in line or 'gradient' in line.lower() or 'stop_loss' in line):
        print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【预警延迟队列统计】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and ('延迟队列' in line or '延迟' in line and '只' in line):
        print(f"L{i+1}: {line.rstrip()}")
