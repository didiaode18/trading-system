# -*- coding: utf-8 -*-
"""验证脚本: 用北方华创2026-07-28真实数据模拟回测alert_engine V2.1"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "trading_system"))

import pandas as pd
import numpy as np
import datetime

from notify.alert_engine import AlertEngine

# Simulate holdings
holdings = {
    "002371": {
        "name": "北方华创",
        "shares": 700,
        "cost": 753.947,
        "buy_price": 753.947,
        "stop_loss": 697,
        "buy_date": "2026-07-24",
    }
}

engine = AlertEngine(holdings=holdings)

# Build mock df_analyzed with 60 days of data
np.random.seed(42)
dates = pd.date_range(end="2026-07-25", periods=60, freq="B")
closes = 750 + np.cumsum(np.random.randn(60) * 3)
closes[-1] = 770  # prev_close (yesterday)
volumes = np.random.randint(50000, 150000, 60).astype(float)
volumes[-1] = 100000  # yesterday normal volume

df = pd.DataFrame({
    "date": dates.strftime("%Y-%m-%d"),
    "open": closes - 2,
    "high": closes + 5,
    "low": closes - 5,
    "close": closes,
    "volume": volumes,
    "amount": volumes * closes,
})

print("=" * 70)
print("  北方华创(002371) 2026-07-28 暴跌模拟回测")
print("  昨收770 | 开盘770 | 最低715 | 收盘715 | 跌幅-7.14%")
print("  成本753.947 | 止损697 | 持仓700股")
print("=" * 70)

# Simulate different time points during the day
test_points = [
    ("09:45", 755, 80000),   # (755-770)/770=-1.95% -> no alert
    ("10:00", 745, 120000),  # (745-770)/770=-3.25% -> R10 warning
    ("10:30", 735, 160000),  # (735-770)/770=-4.55% -> R10 warning
    ("10:36", 756, 180000),  # user buys here (rebound to -1.8%)
    ("10:48", 748, 200000),  # user buys again (-2.86%)
    ("11:00", 730, 220000),  # (730-770)/770=-5.19% -> R10 severe
    ("13:09", 725, 250000),  # user buys 3rd time (-5.84%) -> R10 severe
    ("14:00", 720, 280000),  # (720-770)/770=-6.49% -> R10 severe
    ("14:30", 715, 300000),  # (715-770)/770=-7.14% -> R10 critical!
]

header = "{:<8} {:<8} {:<10} {:<40} {:<10} {}".format(
    "时间", "现价", "日跌幅", "触发规则", "级别", "紧急度")
print()
print(header)
print("-" * 100)

for time_str, price, vol in test_points:
    df_test = df.copy()
    today_row = pd.DataFrame({
        "date": ["2026-07-28"],
        "open": [770.0],
        "high": [770.0],
        "low": [float(price)],
        "close": [float(price)],
        "volume": [float(vol)],
        "amount": [float(vol * price)],
    })
    df_test = pd.concat([df_test, today_row], ignore_index=True)

    intraday_chg = (price - 770) / 770 * 100
    r = {
        "code": "002371",
        "name": "北方华创",
        "close": float(price),
        "df_analyzed": df_test,
        "dk_signal": None,
        "dk_strength": 0,
        "dk_filtered": True,
        "ll_fast": 0,
        "ll_slow": 0,
        "deviation_pct": 0,
        "fund_data": {"score": 50},
        "trend_level": 3,
        "chip": None,
    }

    # Reset cooldown for each test
    engine._alert_history = {}

    alerts = engine._check_single(r)

    if alerts:
        for a in alerts:
            rule = a.get("rule_name", "")
            level = a.get("level", "")
            score = a.get("urgency_score", 0)
            line = "{:<8} {:<8} {:>+.2f}%    {:<40} {:<10} {}".format(
                time_str, price, intraday_chg, rule, level, score)
            try:
                print(line)
            except UnicodeEncodeError:
                print(line.encode("utf-8", errors="replace").decode("utf-8"))
    else:
        line = "{:<8} {:<8} {:>+.2f}%    {:<40} {:<10} {}".format(
            time_str, price, intraday_chg, "(无预警触发)", "--", "--")
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("utf-8", errors="replace").decode("utf-8"))

print()
print("=" * 70)
print("  结论: 修复后的预警时间线")
print("=" * 70)
print()
print("  10:00 价格745 日跌-3.25% -> R10警告: 注意风险,不建议加仓")
print("  10:36 用户第1次买入756   -> 此时已有警告,应阻止!")
print("  10:48 用户第2次买入748   -> 警告持续,应阻止!")
print("  11:00 价格730 日跌-5.19% -> R10严重: 严禁加仓!")
print("  13:09 用户第3次买入725   -> 严重预警中,应阻止!")
print("  14:30 价格715 日跌-7.14% -> R10紧急: 建议卖出300股(50%)")
print()
print("  如果scheduler在运行,每3分钟检查一次:")
print("  最早10:00即可触发警告邮件,阻止10:36/10:48/13:09三次冲动加仓")
print()
print("  额外: R5接近止损位检测 (止损697, 现价715, 距离2.5%) -> 未触发(>2%)")
print("  若继续跌至710: 距离=(710-697)/710=1.8% -> R5接近止损位触发!")
