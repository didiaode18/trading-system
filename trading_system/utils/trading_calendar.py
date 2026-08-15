# -*- coding: utf-8 -*-
"""
公共交易日历模块
================
提供交易日判断与下一交易日推算，供选股报告/买点提醒等模块共用。

重要说明:
  HOLIDAYS/WORKDAYS 常量复制自 trading_system/scheduler.py（行129-150），
  故意使用独立常量副本而非 import scheduler，避免循环依赖与导入 scheduler
  带来的重量级副作用（调度器初始化、任务注册等）。
  两处需同步维护：每年按国务院放假公告更新后，务必同时更新 scheduler.py。
"""

import datetime

# 复制自 scheduler.py HOLIDAYS（来源: scheduler.py 行129-142，需同步维护）
# FIX: 填入2026年A股法定节假日和调休日，修复节假日判断失效
# 注意：具体日期以国务院年度公告为准，当前为合理预估值
HOLIDAYS = {
    # 元旦
    "2026-01-01", "2026-01-02",
    # 春节（预估，以国务院公告为准）
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22",
    # 清明节
    "2026-04-05", "2026-04-06", "2026-04-07",
    # 劳动节
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午节
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋节+国庆节
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
}

# 复制自 scheduler.py WORKDAYS（来源: scheduler.py 行145-150，需同步维护）
# 周末调休上班日（需每年手动更新）
WORKDAYS = {
    # 调休上班日（周末补班）
    "2026-02-14", "2026-02-28",  # 春节前后调休
    "2026-04-26",  # 劳动节调休
    "2026-10-10",  # 国庆调休
}


def is_trading_day(d: datetime.date) -> bool:
    """判断是否为A股交易日（口径与 scheduler.is_trading_day 一致）

    规则:
    - 调休补班日(WORKDAYS)为交易日（即使是周末）
    - 法定节假日(HOLIDAYS)非交易日
    - 周末非交易日
    - 其他日期为交易日
    """
    ds = d.strftime("%Y-%m-%d")
    if ds in WORKDAYS:
        return True
    if d.weekday() >= 5:
        return False
    if ds in HOLIDAYS:
        return False
    return True


def next_trading_day(d: datetime.date) -> datetime.date:
    """返回 d 之后的下一个交易日（不含 d 本身），跳过周末与法定节假日，支持调休补班日"""
    nd = d + datetime.timedelta(days=1)
    while not is_trading_day(nd):
        nd += datetime.timedelta(days=1)
    return nd
