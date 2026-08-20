# -*- coding: utf-8 -*-
# 用 GBK 编码读取日志（Windows 默认编码）

with open(r'd:\workspace\trading-system\trading_system\logs\scheduler_daemon.out', 'r', encoding='gbk', errors='replace') as f:
    lines = f.readlines()

print("=" * 70)
print("【002156 通富微电 今日 intraday_monitor 全部记录】")
print("=" * 70)

for i, line in enumerate(lines):
    if '002156' in line and '2026-08-19' in line and 'intraday_monitor' in line:
        print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【大盘门控状态变迁（今日）】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and 'intraday_monitor' in line:
        if '门控' in line or '状态转换' in line or 'gate' in line.lower():
            print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【门控状态统计（每10分钟）】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and '门控状态' in line and 'intraday_monitor' in line:
        print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【梯度减仓/止损触发生成记录】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and ('梯度减仓' in line or '止损' in line or '条件单' in line):
        print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【预警发送/延迟/穿透记录（含002156）】")
print("=" * 70)
for i, line in enumerate(lines):
    if '2026-08-19' in line and '002156' in line:
        if '延迟' in line or '发送' in line or '穿透' in line or '队列' in line or '立即' in line:
            print(f"L{i+1}: {line.rstrip()}")

print("\n" + "=" * 70)
print("【调度器启动/重启记录】")
print("=" * 70)
for i, line in enumerate(lines):
    if ('启动' in line or 'PID' in line or 'started' in line or '调度器' in line or '加载' in line) and '2026-08-19' in line:
        print(f"L{i+1}: {line.rstrip()}")
