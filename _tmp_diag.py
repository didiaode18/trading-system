# -*- coding: utf-8 -*-
"""盘中预警状态诊断V2"""
import os, datetime, time

# 心跳检查
hb = r'd:\workspace\trading-system\trading_system\output\.scheduler_heartbeat'
mtime = os.path.getmtime(hb)
age = time.time() - mtime
hb_time = datetime.datetime.fromtimestamp(mtime)
print(f'心跳最后更新: {hb_time.strftime("%Y-%m-%d %H:%M:%S")}')
print(f'距今: {age:.0f}秒 ({age/60:.1f}分钟)')
status = "正常" if age < 120 else "异常(>2分钟未更新)"
print(f'状态: {status}')

# 调度器日志
print()
log_dir = r'd:\workspace\trading-system\trading_system\logs'
today = '20260820'
log_file = os.path.join(log_dir, f'scheduler_{today}.log')
if os.path.exists(log_file):
    with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    print(f'调度器日志: {len(lines)} 行')
    keywords = ['启动','监控','加载','预警','门控','止损','梯度','买点','VWAP','异动','持仓','PID','持仓股','条件单','推送']
    for l in lines:
        ls = l.strip()
        if any(k in ls for k in keywords):
            print(f'  {ls}')
else:
    print(f'日志文件不存在: {log_file}')
    print('logs目录内容:')
    for f in sorted(os.listdir(log_dir))[-15:]:
        fp = os.path.join(log_dir, f)
        sz = os.path.getsize(fp)
        print(f'  {f} ({sz} bytes)')

# daemon.out 检查
print()
for dp in [r'd:\workspace\trading-system\scheduler_daemon.out',
           r'd:\workspace\trading-system\trading_system\scheduler_daemon.out',
           r'd:\workspace\trading-system\trading_system\logs\scheduler_daemon.out']:
    if os.path.exists(dp):
        print(f'daemon.out: {dp}')
        with open(dp, 'r', encoding='gbk', errors='replace') as f:
            all_lines = f.readlines()
        today_lines = [l for l in all_lines if '2026-08-20' in l]
        print(f'  今日日志: {len(today_lines)} 行')
        # 最后20行
        for l in all_lines[-20:]:
            print(f'  {l.rstrip()}')
        break
else:
    print('daemon.out 未找到')
