# -*- coding: utf-8 -*-
"""
Stage 0 · 数据质量审计（full-history-backtest）
================================================
对回测数据集做上线前的体检，输出阻断级/警告级/通过三级结论。
禁止在数据缺失时输出"全部正常"——任何缺项都如实标注。

审计项（对应 SKILL.md Stage 0）:
  1. 交易日完整性：以 utils.trading_calendar 为基准列出缺失交易日；
  2. 单股检查：数据长度、停牌（零成交）、OHLC 一致性、异常跳空、重复日期、负价/负量；
  3. 除权除息：复权方式声明（数据层为前复权），标记疑似除权缺口；
  4. 幸存者偏差：股票池是否含退市/ST 摘牌标的，否则显式声明高估风险；
  5. 结论分级：blocking（不可用）/ warning（可继续需声明）/ pass。

复用而非重写：交易日历复用 utils.trading_calendar，涨跌停幅度复用 utils.board_rules。
"""

import logging
import os
import sys

import numpy as np
import pandas as pd
import config

try:
    from utils.trading_calendar import is_trading_day
except ImportError:  # pragma: no cover
    from trading_system.utils.trading_calendar import is_trading_day

try:
    from utils.board_rules import get_limit_pct
except ImportError:  # pragma: no cover
    def get_limit_pct(code, name=""):
        return 0.095

logger = logging.getLogger(__name__)

# 阈值（可被 config.MASTER_BACKTEST_CONFIG 覆盖，此处给默认）
MIN_ROWS = 60                 # 单股最少K线，低于此视为不可用于回测
OHLC_VIOLATION_BLOCK = 0.02   # OHLC 违规比例 > 2% → 阻断级
GAP_THRESHOLD_EXTRA = 0.02    # 超过板块涨跌停幅度 + 2pct 视为异常跳空
MISSING_DAY_WARN = 0.05       # 缺失交易日比例 > 5% → 警告


def _expected_trading_days(start: str, end: str) -> list:
    """按交易日历枚举 [start, end] 内的应有交易日（字符串列表）。"""
    import datetime as _dt
    try:
        d0 = _dt.date.fromisoformat(str(start)[:10])
        d1 = _dt.date.fromisoformat(str(end)[:10])
    except Exception:
        return []
    days = []
    d = d0
    while d <= d1:
        if is_trading_day(d):
            days.append(d.strftime("%Y-%m-%d"))
        d += _dt.timedelta(days=1)
    return days


def audit_one_stock(code: str, df: pd.DataFrame, expected_days: list) -> dict:
    """单只标的数据体检，返回问题清单（不含结论分级，由汇总层判定）。"""
    res = {
        "code": code,
        "rows": int(len(df)),
        "issues": [],          # 警告级
        "blocking": [],        # 阻断级
    }
    if df is None or len(df) == 0:
        res["blocking"].append("空数据")
        return res

    d = df.copy()
    if "date" in d.columns:
        d["date"] = d["date"].astype(str)

    # 数据长度
    if len(d) < MIN_ROWS:
        res["blocking"].append(f"数据长度不足({len(d)}<{MIN_ROWS})")

    # 重复日期
    if "date" in d.columns:
        dup = int(d["date"].duplicated().sum())
        if dup > 0:
            res["issues"].append(f"重复日期{dup}条")

    # 负价/负量
    for col in ["open", "high", "low", "close"]:
        if col in d.columns:
            neg = int((pd.to_numeric(d[col], errors="coerce") <= 0).sum())
            if neg > 0:
                res["blocking"].append(f"{col}非正价{neg}条")
    if "volume" in d.columns:
        negv = int((pd.to_numeric(d["volume"], errors="coerce") < 0).sum())
        if negv > 0:
            res["blocking"].append(f"负成交量{negv}条")

    # 停牌（零成交）
    if "volume" in d.columns:
        zero_vol = int((pd.to_numeric(d["volume"], errors="coerce") == 0).sum())
        if zero_vol > 0:
            res["issues"].append(f"停牌/零成交{zero_vol}天")

    # OHLC 一致性
    if {"open", "high", "low", "close"}.issubset(d.columns):
        o = pd.to_numeric(d["open"], errors="coerce")
        h = pd.to_numeric(d["high"], errors="coerce")
        l = pd.to_numeric(d["low"], errors="coerce")
        c = pd.to_numeric(d["close"], errors="coerce")
        bad = ((h < l) | (h < o) | (h < c) | (l > o) | (l > c)).sum()
        bad = int(bad)
        ratio = bad / max(1, len(d))
        if bad > 0:
            msg = f"OHLC不一致{bad}条({ratio:.1%})"
            (res["blocking"] if ratio > OHLC_VIOLATION_BLOCK else res["issues"]).append(msg)

    # 异常跳空（超板块涨跌幅限制但非一字板）
    if "close" in d.columns and len(d) > 1:
        close = pd.to_numeric(d["close"], errors="coerce")
        pct = close.pct_change().abs()
        # FIX P1-17: 传 name 以正确识别主板 ST（±5%）——名称从 config 解析
        try:
            _nm = config.get_stock_info(code).get("名称", "") or ""
        except Exception:
            _nm = ""
        limit = get_limit_pct(code, _nm)
        abnormal = int((pct > limit + GAP_THRESHOLD_EXTRA).sum())
        if abnormal > 0:
            res["issues"].append(f"异常跳空{abnormal}天(|涨跌|>{limit + GAP_THRESHOLD_EXTRA:.0%})")

    # 交易日完整性
    if expected_days and "date" in d.columns:
        have = set(d["date"].tolist())
        # 只统计该股票首末日期之间的应有交易日
        first, last = d["date"].iloc[0], d["date"].iloc[-1]
        span = [x for x in expected_days if first <= x <= last]
        missing = [x for x in span if x not in have]
        miss_ratio = len(missing) / max(1, len(span))
        res["missing_days"] = len(missing)
        res["missing_ratio"] = round(miss_ratio, 4)
        if miss_ratio > MISSING_DAY_WARN:
            res["issues"].append(f"缺失交易日{len(missing)}天({miss_ratio:.1%})")

    return res


def audit_dataset(data_dict: dict, codes: list = None) -> dict:
    """全库数据质量审计入口。

    返回:
      {status, blocking_errors, warnings, summary, per_stock, survivorship_bias, conclusion}
      status: ok / fail；blocking_errors 为空且数据可用 → ok。
    """
    if not data_dict:
        return {
            "status": "fail",
            "blocking_errors": ["数据集为空，无法审计"],
            "warnings": [],
            "summary": {"n_stocks": 0},
            "per_stock": [],
            "survivorship_bias": "无法评估（无数据）",
            "conclusion": "blocking",
        }

    codes = codes or [c for c in data_dict.keys() if c != "000300"]
    codes = [c for c in codes if c in data_dict]

    # 全库日期范围 → 应有交易日
    all_dates = []
    for c in codes:
        df = data_dict[c]
        if df is not None and len(df) and "date" in df.columns:
            all_dates.append(str(df["date"].iloc[0]))
            all_dates.append(str(df["date"].iloc[-1]))
    expected_days = []
    if all_dates:
        expected_days = _expected_trading_days(min(all_dates), max(all_dates))

    per_stock = []
    blocking_errors = []
    warnings = []
    n_ok = 0
    for c in codes:
        r = audit_one_stock(c, data_dict[c], expected_days)
        per_stock.append(r)
        if r["blocking"]:
            blocking_errors.append(f"{c}: " + ";".join(r["blocking"]))
        if r["issues"]:
            warnings.append(f"{c}: " + ";".join(r["issues"]))
        if not r["blocking"]:
            n_ok += 1

    # 幸存者偏差声明：P2-1——检测回测宇宙是否已纳入退市/ST摘牌标的。
    # 无法从数据集本身识别退市股，故以 config.BACKTEST_DELISTED_POOL 登记为准：
    #   池为空或池内标的均无数据 → 一律显式声明存在偏差（SKILL 要求，禁止谎报无偏）。
    delisted_pool = set(getattr(config, "BACKTEST_DELISTED_POOL", {}).keys())
    n_delisted_included = len([c for c in codes if c in delisted_pool])
    if n_delisted_included > 0:
        survivorship = (
            f"回测宇宙已纳入 {n_delisted_included} 只退市/ST摘牌标的，"
            "幸存者偏差已部分缓解；但退市池覆盖仍不完整，历史收益可能小幅高估。"
        )
    else:
        survivorship = (
            "股票池以当前可交易标的为主，未包含回测区间内已退市/已ST摘牌标的，"
            "结果存在幸存者偏差，历史收益可能被高估。"
        )

    # 结论分级
    if not codes or n_ok == 0:
        conclusion = "blocking"
        status = "fail"
    elif blocking_errors:
        # 有阻断级问题但仍有可用标的 → 标注为 fail（G1 判红），列出具体问题
        conclusion = "blocking"
        status = "fail"
    elif warnings:
        conclusion = "warning"
        status = "ok"
    else:
        conclusion = "pass"
        status = "ok"

    # 只保留有问题的标的明细，控制存档体积
    problem_stocks = [r for r in per_stock if r["issues"] or r["blocking"]][:50]

    summary = {
        "n_stocks": len(codes),
        "n_ok": n_ok,
        "n_blocking": len(blocking_errors),
        "n_warnings": len(warnings),
        "date_range": [min(all_dates), max(all_dates)] if all_dates else [],
        "expected_trading_days": len(expected_days),
    }

    return {
        "status": status,
        "blocking_errors": blocking_errors[:100],
        "warnings": warnings[:200],
        "summary": summary,
        "per_stock": problem_stocks,
        "survivorship_bias": survivorship,
        "n_delisted_included": n_delisted_included,
        "adjustment_note": "数据层统一使用前复权价（adjustflag=2）计算收益，成交以真实价为基准。",
        "conclusion": conclusion,
    }
