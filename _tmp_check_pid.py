# -*- coding: utf-8 -*-
"""检查新调度器(PID 43772)的完整日志"""
import re

with open(r'd:\workspace\trading-system\trading_system\logs\scheduler_daemon.out', 'r', encoding='gbk', errors='replace') as f:
    lines = f.readlines()

# 找新调度器的启动位置（从末尾往前找第二个PID行）
pid_positions = []
for i, l in enumerate(lines):
    if 'PID: 43772' in l:
        pid_positions.append(i)

if pid_positions:
    start_idx = pid_positions[-1]  # 最后一个匹配
    print(f"新调度器(PID 43772)日志起始行: {start_idx}")
    print(f"总行数: {len(lines)}")
    print("=" * 70)
    print("【新调度器完整日志（最近200行）】")
    print("=" * 70)
    recent = lines[max(0, len(lines)-200):]
    for l in recent:
        l = l.strip()
        if l:
            print(f"  {l}")
else:
    print("未找到PID 43772的日志，显示最近100行:")
    for l in lines[-100:]:
        l = l.strip()
        if l:
            print(f"  {l}")
