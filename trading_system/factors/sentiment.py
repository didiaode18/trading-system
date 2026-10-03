# -*- coding: utf-8 -*-
"""
情绪因子（Sentiment Factor）
==============================
核心逻辑: 市场情绪（涨跌停结构/换手/量比/杠杆资金/龙虎榜/北向）
对短期收益有预测力，统一入库接受 IC 监控与降权。

因子清单（口径抽取自 strategy/margin_monitor.py · lhb_analyzer.py ·
zt_monitor.py · capital_flow.py，此处重构为标准 (df) -> Series 接口）:
1. limit_ratio:        涨跌停比（近20日涨停天数-跌停天数占比，OHLCV可算）
2. turnover_rate:      换手率（需 turnover 列，缺失 → NaN）
3. volume_ratio:       量比（当日成交量/近5日均量，OHLCV可算）
4. margin_balance_chg: 融资余额变化率（需 margin_balance 列，缺失 → NaN）
5. lhb_net_buy:        龙虎榜净买入/成交额（需 lhb_net_amount 列，缺失 → NaN）
6. seal_rate:          涨停封板率（触板后收板占比，OHLCV代理，需 high/close）
7. north_flow_ratio:   北向净流入/成交额（需 north_flow 列，停披 → NaN）

铁律: 数据缺失（接口限流/北向停披/无该列）时因子值标记 NaN 静默降级，
禁止填 0 造成误判（0 会被当成"情绪中性"参与打分）。
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 涨跌停判定阈值（与 strategy/zt_monitor.py 口径一致：主板±10%，留0.5%容差）
LIMIT_PCT = 0.095
LIMIT_WINDOW = 20


def _pct_change(df: pd.DataFrame) -> pd.Series:
    """日涨跌幅：优先 pre_close 列（除权除息准确），否则 close.pct_change()。"""
    if "pre_close" in df.columns:
        pre = pd.to_numeric(df["pre_close"], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        chg = (close - pre) / pre.where(pre > 0)
        # pre_close 无效的行回退到 close 环比
        chg = chg.where(pre > 0, close.pct_change())
        return chg
    return pd.to_numeric(df["close"], errors="coerce").pct_change()


def calc_limit_ratio(df: pd.DataFrame) -> pd.Series:
    """
    涨跌停比：近20日（涨停天数 - 跌停天数）/ 20

    范围 [-1, 1]，正值=情绪偏多。需要至少20根K线，不足 → NaN。
    """
    n = len(df)
    if n < LIMIT_WINDOW or "close" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    chg = _pct_change(df)
    is_zt = (chg >= LIMIT_PCT).astype(float).where(chg.notna())
    is_dt = (chg <= -LIMIT_PCT).astype(float).where(chg.notna())
    zt_cnt = is_zt.rolling(LIMIT_WINDOW, min_periods=LIMIT_WINDOW).sum()
    dt_cnt = is_dt.rolling(LIMIT_WINDOW, min_periods=LIMIT_WINDOW).sum()
    return (zt_cnt - dt_cnt) / LIMIT_WINDOW


def calc_turnover_rate(df: pd.DataFrame) -> pd.Series:
    """换手率(%)：直接取 turnover 列（数据层注入），缺列 → 全 NaN。"""
    if "turnover" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    return pd.to_numeric(df["turnover"], errors="coerce")


def calc_volume_ratio(df: pd.DataFrame) -> pd.Series:
    """量比：当日成交量 / 近5日均量（不含当日），>1 放量。不足6根K线 → NaN。"""
    if "volume" not in df.columns or len(df) < 6:
        return pd.Series(np.nan, index=df.index)
    vol = pd.to_numeric(df["volume"], errors="coerce")
    avg5 = vol.shift(1).rolling(5, min_periods=5).mean()
    return vol / avg5.where(avg5 > 0)


def calc_margin_balance_chg(df: pd.DataFrame, window: int = 5) -> pd.Series:
    """
    融资余额变化率：margin_balance 近5日变化 / 5日前值（杠杆资金情绪）

    口径与 strategy/margin_monitor.py 的余额变化监控一致。
    缺 margin_balance 列（接口限流/未抓取）→ 全 NaN，禁止填 0。
    """
    if "margin_balance" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    mb = pd.to_numeric(df["margin_balance"], errors="coerce")
    prev = mb.shift(window)
    return (mb - prev) / prev.where(prev > 0)


def calc_lhb_net_buy(df: pd.DataFrame) -> pd.Series:
    """
    龙虎榜净买入强度：lhb_net_amount / 当日成交额（游资情绪）

    口径与 strategy/lhb_analyzer.py 的净买入分析一致。
    缺列 → 全 NaN；列存在但值为 0（当日未上榜）→ 0 是真实"无净买入"，保留。
    """
    if "lhb_net_amount" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    net = pd.to_numeric(df["lhb_net_amount"], errors="coerce")
    if "amount" in df.columns:
        amt = pd.to_numeric(df["amount"], errors="coerce")
        return net / amt.where(amt > 0)
    return net


def calc_seal_rate(df: pd.DataFrame) -> pd.Series:
    """
    涨停封板率（OHLCV代理）：近20日内，收盘封板天数 / 盘中触板天数

    口径与 strategy/zt_monitor.py 的封板成功率(zt_rate)一致：
    触板 = high >= 涨停价；封板 = close >= 涨停价。
    近20日无触板记录或K线不足 → NaN（分母为0不填0）。
    """
    n = len(df)
    if n < LIMIT_WINDOW or "close" not in df.columns or "high" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    base = df["pre_close"] if "pre_close" in df.columns else pd.to_numeric(df["close"], errors="coerce").shift(1)
    base = pd.to_numeric(base, errors="coerce")
    limit_price = base * (1 + LIMIT_PCT)
    high = pd.to_numeric(df["high"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    touched = (high >= limit_price) & limit_price.notna()
    sealed = (close >= limit_price) & limit_price.notna()
    touch_cnt = touched.astype(float).rolling(LIMIT_WINDOW, min_periods=LIMIT_WINDOW).sum()
    seal_cnt = sealed.astype(float).rolling(LIMIT_WINDOW, min_periods=LIMIT_WINDOW).sum()
    # 无触板 → NaN（情绪不可观测，而非"封板率0"）
    return seal_cnt / touch_cnt.where(touch_cnt > 0)


def calc_north_flow_ratio(df: pd.DataFrame) -> pd.Series:
    """
    北向净流入强度：north_flow / 当日成交额（外资情绪）

    北向数据停披（2024-08 起实时额度不再披露）时列缺失 → 全 NaN，
    禁止填 0 误判为"外资中性"。
    """
    if "north_flow" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    flow = pd.to_numeric(df["north_flow"], errors="coerce")
    if "amount" in df.columns:
        amt = pd.to_numeric(df["amount"], errors="coerce")
        return flow / amt.where(amt > 0)
    return flow


# 因子注册字典
SENTIMENT_FACTORS = {
    "limit_ratio": {
        "func": calc_limit_ratio,
        "desc": "情绪:近20日涨跌停比(涨停-跌停天数占比)",
        "dir": 1,
    },
    "turnover_rate": {
        "func": calc_turnover_rate,
        "desc": "情绪:换手率(需turnover列,缺失NaN降级)",
        "dir": 1,
    },
    "volume_ratio": {
        "func": calc_volume_ratio,
        "desc": "情绪:量比(当日量/近5日均量)",
        "dir": 1,
    },
    "margin_balance_chg": {
        "func": calc_margin_balance_chg,
        "desc": "情绪:融资余额5日变化率(杠杆资金,缺失NaN降级)",
        "dir": 1,
    },
    "lhb_net_buy": {
        "func": calc_lhb_net_buy,
        "desc": "情绪:龙虎榜净买入/成交额(游资,缺失NaN降级)",
        "dir": 1,
    },
    "seal_rate": {
        "func": calc_seal_rate,
        "desc": "情绪:涨停封板率(近20日触板收板占比)",
        "dir": 1,
    },
    "north_flow_ratio": {
        "func": calc_north_flow_ratio,
        "desc": "情绪:北向净流入/成交额(停披NaN降级)",
        "dir": 1,
    },
}
