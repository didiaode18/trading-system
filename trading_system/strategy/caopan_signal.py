# -*- coding: utf-8 -*-
"""
操盘密码自适应趋势策略引擎 V2.0
================================
从「单点信号展示工具」升级为「可验证、强过滤、自适应、闭环执行的中线趋势策略增强引擎」

核心升级:
  1. 控盘生命线: 固定EMA → ATR自适应趋势通道 + 5级趋势分级 + 乖离率4档指引
  2. DK买卖点: 单纯交叉 → 三重共振确认 + 假信号回检 + 信号强度分级
  3. 资金监控: 单一估算 → 多维交叉验证 + 行为模式识别 + 背离预警
  4. 信号过滤: 无 → 市场环境自适应开关 + 盈亏比硬门槛 + 多周期共振
  5. 执行闭环: 信号→风控→仓位→条件单→动态止损

使用:
    from strategy.caopan_signal import CaopanEngine
    engine = CaopanEngine()
    result = engine.analyze(df, code="002415", name="海康威视")
"""

import pandas as pd
import numpy as np
import logging
import sys
import os
from typing import Dict, List, Optional, Tuple
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)
CFG = config.CAOPAN_CONFIG


# ============================================================
# 一、自适应控盘生命线（ATR动态周期 + 5级趋势 + 乖离率分级）
# ============================================================

def compute_adaptive_life_lines(df: pd.DataFrame,
                                 fast_base: int = None,
                                 slow_base: int = None) -> pd.DataFrame:
    """
    自适应控盘生命线 V2.0

    升级点:
    - ATR分位数动态调整EMA周期（高波动拉长过滤噪音，低波动缩短提升灵敏度）
    - 5级趋势分级（强上升/弱上升/震荡/弱下跌/强下跌）
    - 乖离率4档交易指引
    - 均线开口率计算
    """
    fast_base = fast_base or CFG["life_line_fast"]
    slow_base = slow_base or CFG["life_line_slow"]
    atr_period = CFG["atr_period"]
    atr_lookback = CFG["atr_lookback"]
    stretch = CFG["adaptive_stretch"]

    df = df.copy()

    # --- ATR计算 ---
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(atr_period).mean()

    # --- 自适应周期调整 ---
    if CFG.get("adaptive_enabled", True):
        atr_pctile = df["atr"].rolling(atr_lookback).rank(pct=True)
        # 高波动(>80%分位) → 周期拉长20%
        # 低波动(<20%分位) → 周期缩短20%
        fast_period = np.where(atr_pctile > CFG["atr_high_pct"],
                               int(fast_base * (1 + stretch)),
                               np.where(atr_pctile < CFG["atr_low_pct"],
                                        int(fast_base * (1 - stretch)),
                                        fast_base))
        slow_period = np.where(atr_pctile > CFG["atr_high_pct"],
                               int(slow_base * (1 + stretch)),
                               np.where(atr_pctile < CFG["atr_low_pct"],
                                        int(slow_base * (1 - stretch)),
                                        slow_base))
        # 逐行计算自适应EMA（使用最新周期）
        ll_fast = np.zeros(len(df))
        ll_slow = np.zeros(len(df))
        for i in range(len(df)):
            fp = int(fast_period[i]) if not np.isnan(fast_period[i]) else fast_base
            sp = int(slow_period[i]) if not np.isnan(slow_period[i]) else slow_base
            alpha_f = 2.0 / (fp + 1)
            alpha_s = 2.0 / (sp + 1)
            if i == 0:
                ll_fast[i] = close.iloc[0]
                ll_slow[i] = close.iloc[0]
            else:
                ll_fast[i] = alpha_f * close.iloc[i] + (1 - alpha_f) * ll_fast[i-1]
                ll_slow[i] = alpha_s * close.iloc[i] + (1 - alpha_s) * ll_slow[i-1]
        df["ll_fast"] = ll_fast
        df["ll_slow"] = ll_slow
        df["ll_fast_period"] = fast_period
        df["ll_slow_period"] = slow_period
    else:
        df["ll_fast"] = close.ewm(span=fast_base, adjust=False).mean()
        df["ll_slow"] = close.ewm(span=slow_base, adjust=False).mean()
        df["ll_fast_period"] = fast_base
        df["ll_slow_period"] = slow_base

    # --- 均线斜率与开口率 ---
    slope_period = 5
    df["ll_fast_slope"] = df["ll_fast"].diff(slope_period) / df["ll_fast"].shift(slope_period)
    df["ll_slow_slope"] = df["ll_slow"].diff(slope_period) / df["ll_slow"].shift(slope_period)
    # 均线开口率 = (LL1 - LL2) / LL2
    df["ll_spread"] = (df["ll_fast"] - df["ll_slow"]) / df["ll_slow"]
    df["ll_spread_change"] = df["ll_spread"].diff(3)  # 开口变化方向

    # --- 5级趋势分级 ---
    min_slope = 0.001
    df["trend_level"] = _classify_trend_5level(df, min_slope)

    # --- 乖离率（股价与LL1的偏离）---
    df["deviation_pct"] = (close - df["ll_fast"]) / df["ll_fast"]
    df["deviation_action"] = _deviation_guidance(df["deviation_pct"])

    # --- 穿越频率（震荡市检测用）---
    df["above_ll1"] = (close > df["ll_fast"]).astype(int)
    df["cross_ll1"] = df["above_ll1"].diff().abs()
    df["cross_freq_20d"] = df["cross_ll1"].rolling(20).sum()

    # --- 支撑/压力（上下文感知）---
    # 上升趋势: LL1=第一支撑, LL2=强支撑, 压力=近期高点
    # 下跌趋势: LL1=第一压力, LL2=强压力, 支撑=近期低点
    # 震荡: 支撑=min(LL1,LL2), 压力=max(LL1,LL2)
    df["support_ll"] = np.where(
        df["close"] > df["ll_fast"],
        df["ll_fast"],  # 上升趋势: LL1为支撑
        np.where(df["close"] < df["ll_slow"],
                 df["close"].rolling(20).min(),  # 下跌趋势: 近期低点为支撑
                 df["ll_slow"])  # 震荡: LL2为支撑
    )
    df["resistance_ll"] = np.where(
        df["close"] < df["ll_fast"],
        df["ll_fast"],  # 下跌趋势: LL1为压力
        np.where(df["close"] > df["ll_slow"],
                 df["close"].rolling(20).max(),  # 上升趋势: 近期高点为压力
                 df["ll_fast"])  # 震荡: LL1为压力
    )

    return df


def _classify_trend_5level(df: pd.DataFrame, min_slope: float) -> pd.Series:
    """
    5级趋势分级:
    5=强上升: 双线向上 + 开口放大 + 股价在LL1上方
    4=弱上升: 双线向上 + 开口收窄 + 股价在LL1与LL2之间
    3=震荡: 双线走平/方向不一致 + 股价反复穿越
    2=弱下跌: 双线向下 + 开口收窄 + 股价在LL1与LL2之间
    1=强下跌: 双线向下 + 开口放大 + 股价在LL1下方
    """
    fast_up = df["ll_fast_slope"] > min_slope
    fast_down = df["ll_fast_slope"] < -min_slope
    slow_up = df["ll_slow_slope"] > min_slope
    slow_down = df["ll_slow_slope"] < -min_slope
    spread_expanding = df["ll_spread_change"] > 0
    spread_contracting = df["ll_spread_change"] < 0
    above_ll1 = df["close"] > df["ll_fast"]
    below_ll1 = df["close"] < df["ll_fast"]
    between = (df["close"] >= df["ll_slow"]) & (df["close"] <= df["ll_fast"])

    conditions = [
        fast_up & slow_up & spread_expanding & above_ll1,       # 强上升
        fast_up & slow_up & spread_contracting,                  # 弱上升
        fast_down & slow_down & spread_contracting,              # 弱下跌
        fast_down & slow_down & spread_expanding & below_ll1,   # 强下跌
    ]
    choices = [5, 4, 2, 1]
    return pd.Series(np.select(conditions, choices, default=3), index=df.index)


def _deviation_guidance(deviation: pd.Series) -> pd.Series:
    """乖离率4档交易指引"""
    ob = CFG["deviation_overbought"]
    high = CFG["deviation_high"]
    os_level = CFG["deviation_oversold"]

    conditions = [
        deviation > ob,
        (deviation > high) & (deviation <= ob),
        (deviation >= -high) & (deviation <= high),
        deviation < os_level,
    ]
    choices = ["超买减仓1/2", "偏高减仓1/3", "正常持有", "超卖观察"]
    return pd.Series(np.select(conditions, choices, default="正常持有"), index=deviation.index)


# ============================================================
# 二、三重共振DK信号 + 假信号回检 + 强度分级
# ============================================================

def generate_dk_signals_v2(df: pd.DataFrame) -> pd.DataFrame:
    """
    DK买卖点 V2.0 - 三重共振确认

    三重确认:
      1. 均线交叉: LL1上穿/下穿LL2（基础信号）
      2. 量能验证: D点需放量(≥20日均量×1.2)，K点无需量能
      3. 趋势匹配: 上升趋势只保留D点，下跌只保留K点，震荡全部屏蔽

    信号强度:
      强(≥70): 三重确认 + 周线同向
      中(≥50): 三重确认 + 周线中性
      弱(<50): 仅均线交叉，量能/趋势不匹配 → 过滤

    假信号回检:
      金叉后3日内收盘跌破LL2 → 标记假D点
    """
    df = df.copy()
    vol_ratio_threshold = CFG["dk_volume_confirm_ratio"]
    vol_ma_period = CFG["dk_volume_ma_period"]
    false_days = CFG["dk_false_signal_days"]
    min_medium = CFG["dk_min_strength_medium"]
    cross_freq_threshold = CFG["market_cross_freq_threshold"]

    # 量能均线
    df["vol_ma20"] = df["volume"].rolling(vol_ma_period).mean()
    df["vol_confirm"] = df["volume"] >= df["vol_ma20"] * vol_ratio_threshold

    # 均线交叉检测
    df["ll_cross_up"] = (df["ll_fast"] > df["ll_slow"]) & (df["ll_fast"].shift(1) <= df["ll_slow"].shift(1))
    df["ll_cross_down"] = (df["ll_fast"] < df["ll_slow"]) & (df["ll_fast"].shift(1) >= df["ll_slow"].shift(1))

    # 初始化信号列
    df["dk_signal"] = None
    df["dk_strength"] = 0
    df["dk_reason"] = ""
    df["dk_grade"] = ""  # strong/medium/weak/false
    df["dk_filtered"] = False  # 是否被过滤

    for i in range(1, len(df)):
        row = df.iloc[i]
        trend = row.get("trend_level", 3)
        cross_up = row.get("ll_cross_up", False)
        cross_down = row.get("ll_cross_down", False)
        vol_ok = row.get("vol_confirm", False)
        cross_freq = row.get("cross_freq_20d", 0)

        # === 震荡市屏蔽 ===
        is_oscillation = (trend == 3) or (cross_freq >= cross_freq_threshold)
        if is_oscillation and CFG.get("oscillation_auto_shield", True):
            continue  # 震荡市不产生DK信号

        # === D点（三重确认）===
        if cross_up and trend >= 3:  # 震荡及以上都可出D点（震荡市得分降低）
            score = 0
            reasons = []

            # 第一重: 均线金叉（已满足）
            score += 30
            reasons.append("LL1上穿LL2")

            # 第二重: 量能验证（放量加分，缩量不扣分）
            if vol_ok:
                score += 25
                reasons.append("放量确认")
            else:
                score += 10  # 缩量金叉也给分（从5提升至10）

            # 第三重: 趋势匹配
            if trend >= 4:
                score += 20
                reasons.append(f"趋势{trend}级")
            else:  # trend == 3 震荡市
                score += 10
                reasons.append(f"震荡{trend}级(弱)")

            # 加分项: 资金流确认
            main_flow = row.get("main_flow", 0)
            retail_flow = row.get("retail_flow", 0)
            if main_flow > 0:
                score += 10
                reasons.append("主力净买入")
            if retail_flow < 0:
                score += 5
                reasons.append("散户离场")

            # 加分项: 缩量回踩后金叉（洗盘结束）
            vol_ratio = row["volume"] / row["vol_ma20"] if row["vol_ma20"] > 0 else 1
            if vol_ratio < 0.8:
                score += 10
                reasons.append("缩量回踩")

            # 信号强度分级
            if score >= CFG["dk_min_strength_strong"]:
                grade = "strong"
            elif score >= min_medium:
                grade = "medium"
            else:
                grade = "weak"

            # 弱信号过滤
            if grade == "weak":
                df.iloc[i, df.columns.get_loc("dk_filtered")] = True

            df.iloc[i, df.columns.get_loc("dk_signal")] = "D"
            df.iloc[i, df.columns.get_loc("dk_strength")] = min(score, 100)
            df.iloc[i, df.columns.get_loc("dk_reason")] = "+".join(reasons)
            df.iloc[i, df.columns.get_loc("dk_grade")] = grade

        # === K点（三重确认）===
        elif cross_down and trend <= 3:  # 震荡及以下都出K点（从仅<=2放宽至<=3）
            score = 0
            reasons = []

            # 第一重: 均线死叉
            score += 30
            reasons.append("LL1下穿LL2")

            # 第二重: K点无需量能验证（下跌不需要放量）
            score += 20
            reasons.append("空头确认")

            # 第三重: 趋势匹配
            if trend <= 2:
                score += 20
                reasons.append(f"趋势{trend}级")
            else:  # trend == 3 震荡市
                score += 10
                reasons.append(f"震荡{trend}级(弱)")

            # 加分项
            main_flow = row.get("main_flow", 0)
            if main_flow < 0:
                score += 15
                reasons.append("主力净卖出")
            if row.get("retail_flow", 0) > 0:
                score += 5
                reasons.append("散户接盘")

            if score >= CFG["dk_min_strength_strong"]:
                grade = "strong"
            elif score >= min_medium:
                grade = "medium"
            else:
                grade = "weak"

            if grade == "weak":
                df.iloc[i, df.columns.get_loc("dk_filtered")] = True

            df.iloc[i, df.columns.get_loc("dk_signal")] = "K"
            df.iloc[i, df.columns.get_loc("dk_strength")] = min(score, 100)
            df.iloc[i, df.columns.get_loc("dk_reason")] = "+".join(reasons)
            df.iloc[i, df.columns.get_loc("dk_grade")] = grade

    # === 假信号回检 ===
    df = _detect_false_signals(df, false_days)

    return df


def _detect_false_signals(df: pd.DataFrame, window: int) -> pd.DataFrame:
    """
    假信号回检: 金叉后N日内收盘跌破LL2 → 标记假D点
    """
    for i in range(len(df)):
        if df.iloc[i].get("dk_signal") == "D" and df.iloc[i].get("dk_grade") != "false":
            # 检查后续N日
            for j in range(i+1, min(i+window+1, len(df))):
                if df.iloc[j]["close"] < df.iloc[j]["ll_slow"]:
                    df.iloc[i, df.columns.get_loc("dk_grade")] = "false"
                    df.iloc[i, df.columns.get_loc("dk_filtered")] = True
                    df.iloc[i, df.columns.get_loc("dk_reason")] += "+假信号(跌破LL2)"
                    break
    return df


# ============================================================
# 三、多维资金验证 + 行为模式 + 背离预警
# ============================================================

def compute_fund_flow_v2(df: pd.DataFrame) -> pd.DataFrame:
    """
    资金监控 V2.0 - 多维验证体系

    四层验证:
      1. 大单主力资金（量价推算）
      2. 资金趋势性（连续方向）
      3. 量价配合度
      4. 背离检测

    行为模式:
      温和建仓 / 放量拉升 / 对倒骗线
    """
    df = df.copy()
    large_ratio = CFG["large_order_ratio"]
    ma_period = CFG["fund_flow_ma_period"]
    div_days = CFG["fund_divergence_days"]

    # --- 第一层: 主力资金估算 ---
    avg_price = (df["high"] + df["low"] + df["close"]) / 3
    price_range = (df["high"] - df["low"]).replace(0, np.nan)
    close_position = (df["close"] - df["low"]) / price_range
    direction_strength = close_position * 2 - 1

    df["vol_ma_ext"] = df["volume"].rolling(ma_period * 4).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma_ext"].replace(0, np.nan)

    df["main_flow"] = (
        df["volume"] * avg_price * large_ratio *
        direction_strength * df["vol_ratio"].fillna(1)
    )
    df["retail_flow"] = (
        df["volume"] * avg_price * (1 - large_ratio) *
        (-direction_strength) * (2 - df["vol_ratio"].fillna(1))
    )

    # --- 第二层: 资金趋势性 ---
    df["main_flow_ma"] = df["main_flow"].rolling(ma_period).mean()
    df["main_inflow_streak"] = 0
    streak = 0
    for i in range(len(df)):
        if df["main_flow"].iloc[i] > 0:
            streak += 1
        else:
            streak = 0
        df.iloc[i, df.columns.get_loc("main_inflow_streak")] = streak

    df["main_outflow_streak"] = 0
    streak = 0
    for i in range(len(df)):
        if df["main_flow"].iloc[i] < 0:
            streak += 1
        else:
            streak = 0
        df.iloc[i, df.columns.get_loc("main_outflow_streak")] = streak

    # --- 第三层: 量价配合度 ---
    df["price_change"] = df["close"].pct_change()
    # 量价配合: 涨+放量=配合, 涨+缩量=背离
    df["vol_price_sync"] = (
        ((df["price_change"] > 0) & (df["vol_ratio"] > 1)) |
        ((df["price_change"] < 0) & (df["vol_ratio"] < 1))
    ).astype(int)

    # --- 第四层: 背离检测 ---
    # 顶背离: 价格创新高 + 主力连续N日净流出
    df["price_high_20d"] = df["close"].rolling(20).max()
    df["is_price_new_high"] = df["close"] >= df["price_high_20d"]
    df["top_divergence"] = (
        df["is_price_new_high"] &
        (df["main_outflow_streak"] >= div_days)
    )

    # 底背离: 价格创新低 + 主力连续N日净流入
    df["price_low_20d"] = df["close"].rolling(20).min()
    df["is_price_new_low"] = df["close"] <= df["price_low_20d"]
    df["bottom_divergence"] = (
        df["is_price_new_low"] &
        (df["main_inflow_streak"] >= div_days)
    )

    # --- 行为模式识别 ---
    df["fund_pattern"] = _identify_fund_pattern(df)

    # --- 资金信号有效性（多层确认）---
    df["fund_signal_valid"] = (
        (df["main_flow"] > 0) &
        (df["main_inflow_streak"] >= 2) &
        (df["vol_price_sync"] == 1)
    ) | (
        (df["main_flow"] < 0) &
        (df["main_outflow_streak"] >= 2)
    )

    return df


def _identify_fund_pattern(df: pd.DataFrame) -> pd.Series:
    """
    资金行为模式识别:
    - mild_build: 温和建仓（连续3-5日小幅流入+股价缓涨）
    - surge: 放量拉升（单日大额流入+大涨）
    - fake: 对倒骗线（主力流入但股价滞涨+散户大幅流入）
    - normal: 正常
    """
    patterns = pd.Series("normal", index=df.index)
    mild_days = CFG["fund_mild_build_days"]

    for i in range(mild_days, len(df)):
        row = df.iloc[i]
        streak = row.get("main_inflow_streak", 0)
        price_chg = row.get("price_change", 0)
        vol_ratio = row.get("vol_ratio", 1)
        retail = row.get("retail_flow", 0)
        main = row.get("main_flow", 0)

        # 温和建仓: 连续流入 + 每日涨幅<3%
        if streak >= mild_days and 0 < price_chg < 0.03 and vol_ratio < 1.5:
            patterns.iloc[i] = "mild_build"
        # 放量拉升: 单日大额 + 大涨>5%
        elif price_chg > 0.05 and vol_ratio > 2.0 and main > 0:
            patterns.iloc[i] = "surge"
        # 对倒骗线: 主力流入但股价滞涨 + 散户大幅流入
        elif main > 0 and abs(price_chg) < 0.01 and retail > 0 and vol_ratio > 1.5:
            patterns.iloc[i] = "fake"

    return patterns


# ============================================================
# 四、市场环境自适应 + 盈亏比门槛 + 多周期共振
# ============================================================

def detect_market_environment(df: pd.DataFrame) -> dict:
    """
    市场环境自适应检测

    返回:
        {"mode": "trend"/"oscillation", "confidence": 0-1,
         "cross_freq": int, "volatility": float, "description": str}
    """
    if len(df) < 30:
        return {"mode": "unknown", "confidence": 0, "description": "数据不足"}

    latest = df.iloc[-1]
    cross_freq = latest.get("cross_freq_20d", 0)
    threshold = CFG["market_cross_freq_threshold"]
    vol_period = CFG["market_vol_period"]

    # 20日波动率
    returns = df["close"].pct_change().tail(vol_period)
    volatility = returns.std() * np.sqrt(252) if len(returns) > 5 else 0

    # 均线开口率
    spread = abs(latest.get("ll_spread", 0))

    # 判定
    if cross_freq >= threshold:
        mode = "oscillation"
        confidence = min(0.9, cross_freq / (threshold * 2))
        desc = f"震荡市(穿越{int(cross_freq)}次/20日)，DK信号自动屏蔽"
    elif spread > 0.02 and cross_freq <= 2:
        mode = "trend"
        confidence = min(0.9, spread / 0.05)
        desc = f"趋势市(开口{spread*100:.1f}%)，DK信号有效"
    else:
        mode = "transition"
        confidence = 0.5
        desc = "趋势转换期，信号需谨慎"

    return {
        "mode": mode,
        "confidence": round(confidence, 2),
        "cross_freq": int(cross_freq),
        "volatility": round(volatility, 4),
        "spread": round(spread, 4),
        "description": desc,
    }


def calc_risk_reward(df: pd.DataFrame, entry_price: float, trend_level: int) -> dict:
    """
    盈亏比硬门槛计算 V2.1

    修复:
    - 止损位不超过2倍ATR（防止止损太远）
    - 处理负风险情况（LL2在入场价上方时）
    - 目标位使用近期高点+ATR组合
    """
    latest = df.iloc[-1]
    ll2 = latest.get("ll_slow", entry_price * 0.95)
    atr = latest.get("atr", entry_price * 0.03)
    max_risk_atr = CFG.get("max_risk_atr_mult", 2.0)

    # 止损位: LL2下方2%，但不超过2倍ATR
    ll2_stop = ll2 * (1 - CFG["stop_loss_below_ll2"])
    atr_stop = entry_price - atr * max_risk_atr
    # 取两者中更近的（更保守的）
    stop_loss = max(ll2_stop, atr_stop)

    # 如果止损位在入场价上方（下跌趋势中LL2还没跟上），用ATR止损
    if stop_loss >= entry_price:
        stop_loss = entry_price - atr * 1.5

    # 目标位
    recent_high = df["high"].iloc[-60:].max()
    target_1 = max(recent_high, entry_price + atr * 2)
    target_2 = entry_price + atr * 3.5

    risk = entry_price - stop_loss
    reward_1 = target_1 - entry_price
    reward_2 = target_2 - entry_price

    # 风险必须为正
    if risk <= 0:
        risk = atr * 1.5  # 回退到ATR止损
        stop_loss = entry_price - risk

    rr_1 = reward_1 / risk if risk > 0 else 0
    rr_2 = reward_2 / risk if risk > 0 else 0

    min_rr = CFG["min_risk_reward"]
    passed = rr_1 >= min_rr

    # 仓位联动: 盈亏比越高仓位越大
    if CFG.get("rr_position_scale", True) and passed:
        position_pct = min(15, max(5, (rr_1 - min_rr) * 5 + 8))
    else:
        position_pct = 5 if passed else 0

    return {
        "entry_price": round(entry_price, 3),
        "stop_loss": round(stop_loss, 3),
        "target_1": round(target_1, 3),
        "target_2": round(target_2, 3),
        "risk_reward_1": round(rr_1, 2),
        "risk_reward_2": round(rr_2, 2),
        "passed": passed,
        "position_pct": round(position_pct, 1),
        "risk_amount": round(risk, 3),
    }


def check_weekly_trend(df: pd.DataFrame) -> dict:
    """
    多周期共振: 周线趋势判定

    规则:
    - 日线D点 + 周线上升 → 强信号
    - 日线D点 + 周线下跌 → 过滤，禁止开仓
    """
    if len(df) < 50:
        return {"trend": "unknown", "aligned": True}

    # 模拟周线: 5日聚合
    weekly_close = df["close"].iloc[-50:].groupby(np.arange(len(df["close"].iloc[-50:])) // 5).last()
    weekly_ma = weekly_close.rolling(CFG["weekly_ma_period"] // 5 + 1).mean()

    if len(weekly_ma) < 3:
        return {"trend": "unknown", "aligned": True}

    latest_w = weekly_ma.iloc[-1]
    prev_w = weekly_ma.iloc[-3]

    if latest_w > prev_w:
        return {"trend": "up", "aligned": True, "desc": "周线上升"}
    elif latest_w < prev_w:
        return {"trend": "down", "aligned": False, "desc": "周线下跌"}
    else:
        return {"trend": "flat", "aligned": True, "desc": "周线走平"}


# ============================================================
# 五、CaopanEngine V2.0 主引擎
# ============================================================

class CaopanEngine:
    """
    操盘密码自适应趋势策略引擎 V2.0

    整合: 自适应生命线 + 三重DK + 多维资金 + 环境过滤 + 盈亏比 + 周线共振
    """

    def __init__(self, params: dict = None):
        self.params = {**CFG, **(params or {})}

    def analyze(self, df: pd.DataFrame, code: str = "", name: str = "") -> dict:
        """完整分析单只标的"""
        if df is None or len(df) < 60:
            return {"code": code, "name": name, "error": "数据不足(需≥60根K线)"}

        # 1. 自适应生命线
        df = compute_adaptive_life_lines(df, self.params["life_line_fast"], self.params["life_line_slow"])
        # 2. 资金流
        df = compute_fund_flow_v2(df)
        # 3. DK信号
        df = generate_dk_signals_v2(df)

        latest = df.iloc[-1]
        trend_level = int(latest.get("trend_level", 3))
        trend_names = {5: "强上升", 4: "弱上升", 3: "震荡", 2: "弱下跌", 1: "强下跌"}
        trend_desc = trend_names.get(trend_level, "未知")

        # 4. 市场环境
        env = detect_market_environment(df)

        # 5. 盈亏比
        close = latest["close"]
        rr = calc_risk_reward(df, close, trend_level)

        # 6. 周线共振
        weekly = check_weekly_trend(df)

        # 7. 筹码分布
        from strategy.chip_distribution import ChipAnalyzer
        chip_result = ChipAnalyzer().analyze(df, current_price=close)

        # 7.5 真实资金数据
        from data.real_fund_data import RealFundData
        fund_data = RealFundData().analyze(code, df)

        # 8. 综合操作建议
        action = self._generate_action_v2(latest, df, env, rr, weekly)

        # 9. 信号最终判定
        dk = latest.get("dk_signal")
        dk_strength = int(latest.get("dk_strength", 0))
        dk_grade = latest.get("dk_grade", "")
        dk_filtered = latest.get("dk_filtered", False)

        # 多周期过滤: D点+周线下跌 → 禁止
        if dk == "D" and not weekly.get("aligned", True):
            dk_filtered = True
            dk_grade = "filtered_weekly"

        # 盈亏比过滤
        if dk == "D" and not rr["passed"]:
            dk_filtered = True
            dk_grade = "filtered_rr"

        return {
            "code": code, "name": name,
            "trend_level": trend_level,
            "trend_desc": trend_desc,
            "ll_fast": round(latest.get("ll_fast", 0), 3),
            "ll_slow": round(latest.get("ll_slow", 0), 3),
            "ll_fast_direction": "↑" if latest.get("ll_fast_slope", 0) > 0 else "↓",
            "ll_slow_direction": "↑" if latest.get("ll_slow_slope", 0) > 0 else "↓",
            "ll_spread": round(latest.get("ll_spread", 0) * 100, 2),
            "deviation_pct": round(latest.get("deviation_pct", 0) * 100, 2),
            "deviation_action": latest.get("deviation_action", ""),
            "dk_signal": dk,
            "dk_strength": dk_strength,
            "dk_reason": latest.get("dk_reason", ""),
            "dk_grade": dk_grade,
            "dk_filtered": dk_filtered,
            "main_flow_today": round(latest.get("main_flow", 0), 0),
            "retail_flow_today": round(latest.get("retail_flow", 0), 0),
            "main_flow_streak": int(latest.get("main_inflow_streak", 0)),
            "main_outflow_streak": int(latest.get("main_outflow_streak", 0)),
            "fund_pattern": latest.get("fund_pattern", "normal"),
            "top_divergence": bool(latest.get("top_divergence", False)),
            "bottom_divergence": bool(latest.get("bottom_divergence", False)),
            "market_env": env,
            "risk_reward": rr,
            "weekly_trend": weekly,
            "action_suggestion": action,
            "close": round(close, 3),
            "support_price": round(latest.get("support_ll", latest.get("ll_slow", 0)), 3),
            "resistance_price": round(latest.get("resistance_ll", latest.get("ll_fast", 0)), 3),
            "atr": round(latest.get("atr", 0), 3),
            "chip": chip_result if "error" not in chip_result else None,
            "fund_data": fund_data,
            "df_analyzed": df,
        }

    def _generate_action_v2(self, latest, df, env, rr, weekly) -> dict:
        """V2.0操作建议（融合所有维度）"""
        trend = int(latest.get("trend_level", 3))
        deviation = latest.get("deviation_pct", 0)
        dk = latest.get("dk_signal")
        dk_grade = latest.get("dk_grade", "")
        dk_filtered = latest.get("dk_filtered", False)
        pattern = latest.get("fund_pattern", "normal")
        top_div = latest.get("top_divergence", False)
        outflow = int(latest.get("main_outflow_streak", 0))

        # 趋势降级预警
        if trend <= 2:
            return {"type": "clear", "desc": "清仓(下跌趋势)", "urgency": "critical",
                    "detail": f"趋势{trend}级(下跌)，主力连续卖出，及时清仓"}

        if trend == 3:
            if dk == "D" and not dk_filtered:
                return {"type": "buy", "desc": "震荡低吸(弱)", "urgency": "normal",
                        "detail": "震荡市D点，仅轻仓高抛低吸"}
            return {"type": "hold", "desc": "观望(震荡)", "urgency": "normal",
                    "detail": env["description"]}

        # 上升趋势(trend 4/5)
        # 超买检查
        if deviation > CFG["deviation_overbought"]:
            return {"type": "reduce", "desc": "超买减仓1/2", "urgency": "warning",
                    "detail": f"偏离{deviation*100:.1f}%>10%超买区，减仓1/2锁定利润"}
        if deviation > CFG["deviation_high"]:
            return {"type": "reduce", "desc": "偏高减仓1/3", "urgency": "warning",
                    "detail": f"偏离{deviation*100:.1f}%偏高区，可减仓1/3"}

        # 顶背离预警
        if top_div:
            return {"type": "reduce", "desc": "顶背离减仓", "urgency": "critical",
                    "detail": "价格新高但主力连续流出，顶背离，收紧止损+减仓"}

        # 对倒骗线
        if pattern == "fake":
            return {"type": "hold", "desc": "警惕对倒骗线", "urgency": "warning",
                    "detail": "主力流入但股价滞涨+散户大幅流入，疑似对倒，禁止加仓"}

        # D点信号
        if dk == "D" and not dk_filtered:
            if dk_grade == "strong" and rr["passed"] and weekly.get("aligned", True):
                return {"type": "add", "desc": "强D点加仓", "urgency": "high",
                        "detail": f"强信号({latest.get('dk_reason','')})，盈亏比{rr['risk_reward_1']}:1达标，周线同向"}
            elif dk_grade == "medium" and rr["passed"]:
                return {"type": "buy", "desc": "中D点建仓", "urgency": "high",
                        "detail": f"中信号({latest.get('dk_reason','')})，盈亏比{rr['risk_reward_1']}:1"}
            else:
                return {"type": "watch", "desc": "D点观察(条件不全)", "urgency": "normal",
                        "detail": f"D点但{'盈亏比不达标' if not rr['passed'] else '周线不同向'}"}

        # 温和建仓模式
        if pattern == "mild_build":
            return {"type": "buy", "desc": "温和建仓跟进", "urgency": "normal",
                    "detail": "主力连续小幅流入+股价缓涨，回踩LL1可建仓"}

        # 默认持有
        return {"type": "hold", "desc": f"持有({latest.get('trend_level',3)}级上升)", "urgency": "normal",
                "detail": f"上升趋势持有，偏离{deviation*100:.1f}%正常，止损沿LL2上移"}

    # === 批量扫描 ===
    def scan_filter(self, data_dict: dict, names: dict = None) -> List[dict]:
        """严格筛选: 上升趋势 + 有效D点 + 盈亏比达标 + 周线同向"""
        results = []
        for code, df in data_dict.items():
            name = (names or {}).get(code, code)
            try:
                r = self.analyze(df, code=code, name=name)
                if "error" in r:
                    continue
                # 四层筛选
                if (r["trend_level"] >= 4 and
                    r.get("dk_signal") == "D" and
                    not r.get("dk_filtered", True) and
                    r["risk_reward"]["passed"] and
                    r.get("weekly_trend", {}).get("aligned", False)):
                    results.append(r)
            except Exception:
                continue
        results.sort(key=lambda x: x["dk_strength"], reverse=True)
        return results

    # === 回测引擎 V2.1 ===
    def backtest(self, df: pd.DataFrame, code: str = "",
                 initial_capital: float = 100000) -> dict:
        """V2.1回测: 三重确认DK + 回踩入场 + LL1追踪止损 + 趋势降级退出"""
        if df is None or len(df) < 80:
            return {"error": "数据不足"}

        df = compute_adaptive_life_lines(df, self.params["life_line_fast"], self.params["life_line_slow"])
        df = compute_fund_flow_v2(df)
        df = generate_dk_signals_v2(df)

        commission = self.params["backtest_commission"]
        stamp_tax = self.params["backtest_stamp_tax"]
        slippage = self.params["backtest_slippage"]
        pullback_enabled = CFG.get("dk_pullback_entry", True)
        pullback_days = CFG.get("dk_pullback_days", 5)

        cash = initial_capital
        shares = 0
        buy_price = 0
        trades = []
        daily_values = []
        pending_d = None  # 待回踩的D点信号
        pending_d_idx = 0
        highest_since_buy = 0  # 买入后最高价（追踪止损用）

        for i in range(60, len(df)):
            row = df.iloc[i]
            date = row.get("date", str(i))
            close = row["close"]
            total_value = cash + shares * close
            daily_values.append({"date": date, "value": total_value})

            dk = row.get("dk_signal")
            dk_grade = row.get("dk_grade", "")
            dk_filtered = row.get("dk_filtered", False)
            trend = int(row.get("trend_level", 3))
            ll_fast = row.get("ll_fast", close)
            ll_slow = row.get("ll_slow", close)
            deviation = row.get("deviation_pct", 0)

            # === 买入逻辑 ===
            if shares == 0:
                # 方式A: D点金叉入场（含回踩）
                if dk == "D" and not dk_filtered and trend >= 3 and dk_grade in ("strong", "medium"):
                    if pullback_enabled:
                        pending_d = {"idx": i, "grade": dk_grade, "strength": row.get("dk_strength", 0),
                                     "reason": row.get("dk_reason", "")}
                        pending_d_idx = i
                    else:
                        rr = calc_risk_reward(df.iloc[:i+1], close, trend)
                        if rr["passed"]:
                            buy_cost = close * (1 + slippage)
                            buy_shares = int(cash * 0.9 / buy_cost / 100) * 100
                            if buy_shares >= 100:
                                cost = buy_shares * buy_cost
                                comm = max(cost * commission, 5)
                                cash -= (cost + comm)
                                shares = buy_shares
                                buy_price = buy_cost
                                highest_since_buy = close
                                trades.append({"date": date, "action": "buy", "price": round(buy_cost, 3),
                                               "shares": buy_shares, "reason": f"D点({dk_grade},{row.get('dk_strength',0)})"})

                # 方式A续: D点回踩入场
                elif pending_d and (i - pending_d_idx) <= pullback_days:
                    if close <= ll_fast * 1.01 and close >= ll_slow:
                        rr = calc_risk_reward(df.iloc[:i+1], close, trend)
                        if rr["passed"] and trend >= 3:
                            buy_cost = close * (1 + slippage)
                            buy_shares = int(cash * 0.9 / buy_cost / 100) * 100
                            if buy_shares >= 100:
                                cost = buy_shares * buy_cost
                                comm = max(cost * commission, 5)
                                cash -= (cost + comm)
                                shares = buy_shares
                                buy_price = buy_cost
                                highest_since_buy = close
                                trades.append({"date": date, "action": "buy", "price": round(buy_cost, 3),
                                               "shares": buy_shares,
                                               "reason": f"D点回踩({pending_d['grade']},{pending_d['strength']})"})
                                pending_d = None
                elif pending_d and (i - pending_d_idx) > pullback_days:
                    pending_d = None

                # 方式B: 上升趋势中缩量回踩LL1入场（操盘密码核心: 缩量回踩生命线=买点）
                elif trend >= 4 and ll_fast > ll_slow:
                    # 条件: 股价回踩LL1附近 + 缩量 + 主力未大幅流出
                    vol_ma = row.get("vol_ma20", 1)
                    vol_ratio = row.get("volume", 0) / vol_ma if vol_ma > 0 else 1
                    main_flow = row.get("main_flow", 0)
                    touch_ll1 = (close <= ll_fast * 1.005) and (close >= ll_fast * 0.985)
                    if touch_ll1 and vol_ratio < 0.85 and main_flow >= 0:
                        rr = calc_risk_reward(df.iloc[:i+1], close, trend)
                        if rr["passed"]:
                            buy_cost = close * (1 + slippage)
                            buy_shares = int(cash * 0.9 / buy_cost / 100) * 100
                            if buy_shares >= 100:
                                cost = buy_shares * buy_cost
                                comm = max(cost * commission, 5)
                                cash -= (cost + comm)
                                shares = buy_shares
                                buy_price = buy_cost
                                highest_since_buy = close
                                trades.append({"date": date, "action": "buy", "price": round(buy_cost, 3),
                                               "shares": buy_shares,
                                               "reason": f"缩量回踩LL1(趋势{trend}级)"})

            # === 卖出逻辑 ===
            elif shares > 0:
                sell = False
                reason = ""
                highest_since_buy = max(highest_since_buy, close)
                atr = row.get("atr", close * 0.03)
                gain_pct = (close - buy_price) / buy_price if buy_price > 0 else 0
                days_held = i - trades[-1].get("_idx", i) if trades else 0

                # 1. LL2动态止损（硬止损，任何时候都生效）
                dynamic_stop = ll_slow * (1 - CFG["stop_loss_below_ll2"])
                if close < dynamic_stop:
                    sell, reason = True, "跌破LL2动态止损"

                # 2. K点卖出（优先级高于追踪止损）
                elif dk == "K" and not dk_filtered and dk_grade in ("strong", "medium"):
                    sell, reason = True, f"K点({dk_grade})"

                # 3. 超买止盈
                elif deviation > CFG["deviation_overbought"]:
                    sell, reason = True, f"超买{deviation*100:.0f}%止盈"

                # 4. 趋势降级清仓
                elif trend <= 2:
                    sell, reason = True, "趋势降级清仓"

                # 5. 顶背离
                elif row.get("top_divergence", False):
                    sell, reason = True, "顶背离止盈"

                # 6. LL1追踪止盈（仅盈利>5%后，从高点回落>10%才触发，给足空间）
                elif close < ll_fast and gain_pct > 0.05:
                    pullback_from_high = (highest_since_buy - close) / highest_since_buy if highest_since_buy > 0 else 0
                    if pullback_from_high > 0.10:
                        sell, reason = True, "LL1追踪止盈(回吐保护)"

                if sell:
                    sell_price = close * (1 - slippage)
                    revenue = shares * sell_price
                    comm = max(revenue * commission, 5)
                    tax = revenue * stamp_tax
                    cash += (revenue - comm - tax)
                    pnl = (sell_price - buy_price) * shares - comm - tax
                    trades.append({"date": date, "action": "sell", "price": round(sell_price, 3),
                                   "shares": shares, "pnl": round(pnl, 2),
                                   "pnl_pct": round((sell_price/buy_price-1)*100, 2), "reason": reason})
                    shares = 0
                    buy_price = 0
                    highest_since_buy = 0

        # 强制平仓
        if shares > 0:
            last_close = df.iloc[-1]["close"]
            revenue = shares * last_close
            comm = max(revenue * commission, 5)
            tax = revenue * stamp_tax
            cash += (revenue - comm - tax)
            pnl = (last_close - buy_price) * shares - comm - tax
            trades.append({"date": "end", "action": "sell", "price": round(last_close, 3),
                           "shares": shares, "pnl": round(pnl, 2),
                           "pnl_pct": round((last_close/buy_price-1)*100, 2), "reason": "回测结束"})

        return self._calc_metrics(trades, daily_values, initial_capital, code)

    def _calc_metrics(self, trades, daily_values, initial_capital, code) -> dict:
        """计算回测绩效"""
        sells = [t for t in trades if t["action"] == "sell"]
        n = len(sells)
        if n == 0:
            return {"code": code, "total_trades": 0, "error": "无交易信号"}

        wins = [t for t in sells if t.get("pnl", 0) > 0]
        losses = [t for t in sells if t.get("pnl", 0) <= 0]
        win_rate = len(wins) / n
        avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
        avg_lose = abs(np.mean([t["pnl"] for t in losses])) if losses else 0
        # 盈亏比: 全赢时设为99.9，全亏时设为0
        if avg_lose > 0 and avg_win > 0:
            pf = avg_win / avg_lose
        elif avg_lose == 0 and avg_win > 0:
            pf = 99.9  # 全赢
        else:
            pf = 0
        total_pnl = sum(t.get("pnl", 0) for t in sells)
        total_return = total_pnl / initial_capital

        values = [v["value"] for v in daily_values]
        max_dd = 0
        peak = values[0] if values else initial_capital
        for v in values:
            if v > peak: peak = v
            dd = (peak - v) / peak
            if dd > max_dd: max_dd = dd

        days = len(daily_values)
        annual = (1 + total_return) ** (252 / max(days, 1)) - 1
        if len(values) > 1:
            rets = pd.Series(values).pct_change().dropna()
            sharpe = (rets.mean() - 0.03/252) / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
        else:
            sharpe = 0

        validation = {"annual_return": annual >= 0.20, "max_drawdown": max_dd <= 0.20,
                      "profit_factor": pf >= 2.0, "win_rate": win_rate >= 0.35}

        return {
            "code": code, "total_trades": n, "win_trades": len(wins), "lose_trades": len(losses),
            "win_rate": round(win_rate, 4), "profit_factor": round(pf, 2),
            "total_pnl": round(total_pnl, 2), "total_return": round(total_return, 4),
            "annual_return": round(annual, 4), "max_drawdown": round(max_dd, 4),
            "sharpe_ratio": round(sharpe, 2), "avg_win": round(avg_win, 2),
            "avg_lose": round(avg_lose, 2), "backtest_days": days,
            "trades": trades, "validation": validation, "all_pass": all(validation.values()),
        }

    # === 参数优化 ===
    def optimize_params(self, df: pd.DataFrame, code: str = "") -> List[dict]:
        """参数网格搜索 + 鲁棒性验证"""
        grid = self.params.get("optimize_grid", CFG["optimize_grid"])
        keys = list(grid.keys())
        values = list(grid.values())
        combos = list(product(*values))
        results = []

        for combo in combos:
            params = dict(zip(keys, combo))
            if params.get("life_line_fast", 10) >= params.get("life_line_slow", 30):
                continue
            engine = CaopanEngine(params={**self.params, **params})
            bt = engine.backtest(df, code=code)
            if "error" not in bt and bt["total_trades"] >= 3:
                results.append({
                    "params": params, "sharpe": bt["sharpe_ratio"],
                    "annual_return": bt["annual_return"], "max_drawdown": bt["max_drawdown"],
                    "win_rate": bt["win_rate"], "profit_factor": bt["profit_factor"],
                    "total_trades": bt["total_trades"], "all_pass": bt["all_pass"],
                })

        results.sort(key=lambda x: x["sharpe"], reverse=True)
        return results

    def train_test_split_backtest(self, df: pd.DataFrame, code: str = "") -> dict:
        """样本内外验证: 70%训练 + 30%验证"""
        split = int(len(df) * CFG["backtest_train_ratio"])
        train_df = df.iloc[:split].reset_index(drop=True)
        test_df = df.iloc[split:].reset_index(drop=True)

        train_bt = self.backtest(train_df, code=f"{code}_train")
        test_bt = self.backtest(test_df, code=f"{code}_test")

        train_ret = train_bt.get("total_return", 0)
        test_ret = test_bt.get("total_return", 0)
        robust = test_ret >= train_ret * 0.8 if train_ret > 0 else False

        return {
            "train": train_bt, "test": test_bt,
            "train_return": round(train_ret, 4),
            "test_return": round(test_ret, 4),
            "robust": robust,
            "desc": f"训练{train_ret:.1%}/验证{test_ret:.1%} {'✅鲁棒' if robust else '❌过拟合风险'}"
        }


# 便捷函数
def quick_analyze(df, code="", name=""):
    return CaopanEngine().analyze(df, code=code, name=name)

def quick_backtest(df, code=""):
    return CaopanEngine().backtest(df, code=code)
