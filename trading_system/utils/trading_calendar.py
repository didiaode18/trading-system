# -*- coding: utf-8 -*-
"""
公共交易日历模块
================
提供交易日判断与下一交易日推算，供选股报告/买点提醒等模块共用。

本模块是 A 股交易日历的**唯一权威来源**（scheduler.py 直接 import 本模块的
HOLIDAYS/WORKDAYS，不再维护副本），避免两处常量不同步导致的判断分歧。

⚠️ 维护铁律（2026-10-08 P0 事故复盘）:
  1. HOLIDAYS 必须按**沪深北交易所休市公告**填写，而不是国务院放假通知。
     国务院放假通知里"周末连休"的周六周日交易所本来就休市，写不写都不影响；
     但把**节后首个工作日**误当假期（如曾误填 2026-10-08）会让全天任务静默跳过。
  2. WORKDAYS 只允许放"A 股周末**开市**日"。国务院调休上班的周六/周日
     （如 2026-02-14、2026-02-28、2026-05-09、2026-09-20、2026-10-10）
     A 股一律休市，**严禁**写入 WORKDAYS —— 否则周六会跑全套任务、发报告、
     生成条件单，甚至让 qmt_trader 认为可以下单。
     A 股历史上从无周末开市先例，因此该集合正常情况下为空。
  3. validate_calendar() 会在导入方启动时自检上述两条，发现周末 WORKDAYS
     立即告警。
"""

import datetime

# A 股休市日（来源: 沪深北交易所 2026 年休市公告，2026-09-17 发布）
# 仅列出"本应是工作日却休市"的日期即可，周末由 weekday 判断兜底。
HOLIDAYS = {
    # 元旦: 1月1日(周四)至3日(周六)休市
    "2026-01-01", "2026-01-02",
    # 春节: 2月15日(周日)至23日(周一)休市 —— FIX: 原缺 02-23(周一)，会误判为交易日
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    # 清明节: 4月4日(周六)至6日(周一)休市 —— FIX: 原误含 04-07(周二，实际正常开市)
    "2026-04-06",
    # 劳动节: 5月1日(周五)至5日(周二)休市
    "2026-05-01", "2026-05-04", "2026-05-05",
    # 端午节: 6月19日(周五)至21日(周日)休市
    "2026-06-19",
    # 中秋节: 9月25日(周五)至27日(周日)休市 —— FIX: 原完全缺失 09-25，当天会误跑全套任务
    "2026-09-25",
    # 国庆节: 10月1日(周四)至7日(周三)休市，**10月8日(周四)起照常开市**
    # FIX(2026-10-08 P0): 原误含 "2026-10-08"，导致节后首个交易日全天任务静默跳过
    #   （盘前作战计划/竞价选股/涨停基因/盘中监控/自愈全部未执行，持仓无监控裸奔）
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}

# A 股周末开市日（历史无先例，正常为空集合）
# FIX(2026-10-08 P0): 原填入国务院调休上班的周末日期(02-14/02-28/04-26/10-10)，
#   但这些日期 A 股一律休市，会让周六被判定为交易日 → 周末误发报告、误生成条件单。
#   保留空集合常量以兼容 `from scheduler import WORKDAYS` 等既有导入。
WORKDAYS = set()


def validate_calendar(verbose: bool = True):
    """日历自检：返回问题列表（空列表=通过）

    检查项:
      1. WORKDAYS 不得含周六/周日（A 股周末从不开市，含周末必为录入错误）
      2. HOLIDAYS 不得含周六/周日（冗余，虽无害但会掩盖真实的工作日休市遗漏）
    """
    problems = []
    for ds in sorted(WORKDAYS):
        try:
            wd = datetime.date.fromisoformat(ds).weekday()
        except ValueError:
            problems.append(f"WORKDAYS 日期格式非法: {ds}")
            continue
        if wd >= 5:
            problems.append(
                f"WORKDAYS 含周末 {ds}（周{'六' if wd == 5 else '日'}）——"
                f"国务院调休上班日 A 股仍休市，应移除")
    for ds in sorted(HOLIDAYS):
        try:
            wd = datetime.date.fromisoformat(ds).weekday()
        except ValueError:
            problems.append(f"HOLIDAYS 日期格式非法: {ds}")
            continue
        if wd >= 5:
            problems.append(f"HOLIDAYS 含周末 {ds}（冗余，建议只保留工作日休市日）")
    if verbose:
        for p in problems:
            print(f"[trading_calendar] ⚠️ {p}")
    return problems


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


def effective_trading_day(now=None) -> datetime.date:
    """报告/买点的"适用交易日"统一口径

    规则（与 notify.buy_point_alert.save_buy_alert_levels 原有判定一致）:
      - 交易日 15:00 前生成 → 当日适用（竞价后 09:25 选股服务于当日盘中交易）
      - 交易日 15:00 后 或 非交易日生成 → 下一交易日适用（盘后/夜间报告服务于次日）

    FIX(2026-10-08): 选股报告标题与正文"适用日期"原先恒取 next_trading_day，
    导致盘中/竞价后生成的报告标注成明日（如 10-08 09:41 生成却写"适用日期 2026-10-09"），
    而同一次运行落盘的买点提醒 valid_date 却是当日 —— 两者口径矛盾。
    """
    if now is None:
        now = datetime.datetime.now()
    today = now.date() if isinstance(now, datetime.datetime) else now
    if is_trading_day(today) and getattr(now, "hour", 0) < 15:
        return today
    return next_trading_day(today)
