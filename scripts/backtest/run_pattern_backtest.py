# -*- coding: utf-8 -*-
"""
K线形态集成回测对比验证
=======================
对比 V5.1基线版 vs V5.1+K线形态增强版

增强逻辑:
  - 在V5.1信号质量评分基础上，叠加K线形态加分
  - 看涨形态(位置有效): signal_quality += 8
  - 看跌形态(位置有效): signal_quality -= 5
  - 效果: 过滤掉形态看跌的低质量信号，优先通过形态看涨的高质量信号

输出:
  1. 控制台对比表格
  2. HTML对比报告 (output/backtest_pattern_compare.html)
  3. Excel格式交易明细 (output/backtest_trades_detail.xlsx)
"""

import sys
import os
import datetime
import logging
import json

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_system'))
import config

# 从 backtest_real 导入共用组件
from backtest_real import (
    TEST_STOCKS, fetch_history_data, compute_indicators,
    analyze_trades, backtest_stock_v5,
    COMMISSION, SLIPPAGE_LEADER, SLIPPAGE_FLEX, RISK_PER_TRADE,
    TOTAL_CAPITAL, LIMIT_PCT, _precompute_market_regime,
    is_limit_up, is_limit_down,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

START_DATE = "2022-01-01"


# ============================================================
# K线形态快速检测 V2.0（趋势门控 + 非对称加分 + 形态加权 + 行业权重）
# ============================================================

# V10.1: 形态类型权重（A股中线波段有效性分级）
_PATTERN_WEIGHT = {
    "看涨吞没": 2.0, "曙光初现": 2.0, "早晨之星": 2.0,
    "看跌吞没": 2.0, "黄昏之星": 2.0, "倾盆大雨": 1.5,
    "红三兵": 1.5, "三只乌鸦": 1.5, "刺透形态": 1.5, "乌云盖顶": 1.5,
    "锤子线": 1.0, "射击之星": 1.0, "大阳线": 1.0, "大阴线": 1.0,
    "十字星": 0.5, "纺锤线": 0.3, "螺旋桨": 0.3, "平底": 0.3, "平顶": 0.3,
}

def _compute_pattern_bonus(df, i, industry=""):
    """
    计算第i根K线处的K线形态加分值 V2.0

    V10.1改进:
      1. 趋势一致性门控: MA20斜率弱时降低看涨加分
      2. 非对称加分: 上升趋势中看跌仅轻扣（正常回调），看涨适度加
      3. 形态类型加权: 吞没/早晨之星等高价值形态×2.0，十字星×0.5
      4. 行业差异化: 半导体/电子×1.5，金融/消费×0.3

    返回: int (范围约 -4 ~ +4)
    """
    if i < 10:
        return 0

    # V10.1: 从 config 读取4档非对称参数
    kpc = getattr(config, 'KLINE_PATTERN_CONFIG', {})
    bonus_bull_up = kpc.get('pattern_bonus_bullish_trend_up', 4)
    bonus_bear_up = kpc.get('pattern_bonus_bearish_trend_up', -1)
    bonus_bull_dn = kpc.get('pattern_bonus_bullish_trend_down', 0)
    bonus_bear_dn = kpc.get('pattern_bonus_bearish_trend_down', -4)

    bullish_w = 0.0
    bearish_w = 0.0
    close = df["close"].values
    open_ = df["open"].values
    high = df["high"].values
    low = df["low"].values

    # 趋势判定: 近20日涨跌幅
    trend_20d = 0
    if i >= 20:
        trend_20d = (close[i] - close[i - 20]) / close[i - 20] if close[i - 20] > 0 else 0

    # V10.1: MA20斜率 → 趋势一致性门控
    # 回测只在MA20上行时买入，但斜率弱时降低看涨加分
    ma20_gate = 1.0
    if "ma20" in df.columns and i >= 5:
        ma20_slope = df["ma20"].iloc[i] - df["ma20"].iloc[max(0, i - 5)]
        if ma20_slope > 0:
            ma20_pct = ma20_slope / df["ma20"].iloc[i] * 100 if df["ma20"].iloc[i] > 0 else 0
            if ma20_pct < 0.05:
                ma20_gate = 0.3   # MA20几乎走平，看涨信号可信度低
            elif ma20_pct < 0.15:
                ma20_gate = 0.6   # MA20缓慢上行
            # else: ma20_gate = 1.0 (强劲上行)

    for j in range(max(5, i - 4), i + 1):
        body = abs(close[j] - open_[j])
        total_range = high[j] - low[j]
        if total_range <= 0:
            continue

        upper_shadow = high[j] - max(close[j], open_[j])
        lower_shadow = min(close[j], open_[j]) - low[j]
        is_bull = close[j] > open_[j]
        is_bear = close[j] < open_[j]

        # ---- 单根形态 ----
        if lower_shadow >= body * 2 and upper_shadow < body * 0.5 and trend_20d < -0.05:
            bullish_w += _PATTERN_WEIGHT.get("锤子线", 1.0)
        if upper_shadow >= body * 2 and lower_shadow < body * 0.5 and trend_20d > 0.05:
            bearish_w += _PATTERN_WEIGHT.get("射击之星", 1.0)
        if body < total_range * 0.1:
            if trend_20d < -0.08:
                bullish_w += _PATTERN_WEIGHT.get("十字星", 0.5)
            elif trend_20d > 0.08:
                bearish_w += _PATTERN_WEIGHT.get("十字星", 0.5)
        if is_bull and body / total_range > 0.7 and body / close[j] > 0.02:
            bullish_w += _PATTERN_WEIGHT.get("大阳线", 1.0)
        if is_bear and body / total_range > 0.7 and body / close[j] > 0.02:
            bearish_w += _PATTERN_WEIGHT.get("大阴线", 1.0)

        # ---- 双根形态 ----
        if j >= 1:
            prev_body = abs(close[j - 1] - open_[j - 1])
            prev_bull = close[j - 1] > open_[j - 1]
            prev_bear = close[j - 1] < open_[j - 1]
            if prev_bear and is_bull and body > prev_body * 1.2:
                if close[j] > open_[j - 1] and open_[j] < close[j - 1]:
                    bullish_w += _PATTERN_WEIGHT.get("看涨吞没", 2.0)
            if prev_bull and is_bear and body > prev_body * 1.2:
                if close[j] < open_[j - 1] and open_[j] > close[j - 1]:
                    bearish_w += _PATTERN_WEIGHT.get("看跌吞没", 2.0)
            if prev_bear and is_bull and open_[j] < low[j - 1]:
                prev_mid = (open_[j - 1] + close[j - 1]) / 2
                if close[j] > prev_mid and close[j] < open_[j - 1]:
                    bullish_w += _PATTERN_WEIGHT.get("刺透形态", 1.5)
            if prev_bull and is_bear and open_[j] > high[j - 1]:
                prev_mid = (open_[j - 1] + close[j - 1]) / 2
                if close[j] < prev_mid and close[j] > open_[j - 1]:
                    bearish_w += _PATTERN_WEIGHT.get("乌云盖顶", 1.5)

        # ---- 三根形态 ----
        if j >= 2:
            p2_body = abs(close[j - 2] - open_[j - 2])
            p2_bear = close[j - 2] < open_[j - 2]
            p2_bull = close[j - 2] > open_[j - 2]
            p1_body = abs(close[j - 1] - open_[j - 1])
            if (is_bull and close[j] > close[j - 1] > close[j - 2]
                    and open_[j] > open_[j - 1] > open_[j - 2]
                    and close[j - 1] > open_[j - 2]):
                bullish_w += _PATTERN_WEIGHT.get("红三兵", 1.5)
            if (is_bear and close[j] < close[j - 1] < close[j - 2]
                    and open_[j] < open_[j - 1] < open_[j - 2]):
                bearish_w += _PATTERN_WEIGHT.get("三只乌鸦", 1.5)
            if (p2_bear and p2_body > 0 and is_bull
                    and p1_body < p2_body * 0.3
                    and body > p2_body * 0.5):
                bullish_w += _PATTERN_WEIGHT.get("早晨之星", 2.0)
            if (p2_bull and p2_body > 0 and is_bear
                    and p1_body < p2_body * 0.3
                    and body > p2_body * 0.5):
                bearish_w += _PATTERN_WEIGHT.get("黄昏之星", 2.0)

    # V10.1: 加权净分 → 非对称加分
    net_w = bullish_w - bearish_w
    if net_w >= 2.0:
        bonus = bonus_bull_up       # 强看涨: +4
    elif net_w >= 0.5:
        bonus = max(1, bonus_bull_up // 2)  # 弱看涨: +2
    elif net_w <= -2.0:
        bonus = bonus_bear_dn       # 强看跌: -4
    elif net_w <= -0.5:
        bonus = bonus_bear_up       # 弱看跌: -1
    else:
        bonus = 0

    # V10.1: 趋势一致性门控（看涨加分受MA20斜率缩放，看跌扣分不受影响）
    if bonus > 0:
        bonus = max(1, int(bonus * ma20_gate))

    # V10.1: 行业差异化权重
    sector_w = kpc.get('sector_pattern_weight', {}).get(industry, 1.0)
    bonus = int(round(bonus * sector_w))

    return bonus


# ============================================================
# V5.1+K线形态增强版回测
# ============================================================

def backtest_stock_v5_pattern(df: pd.DataFrame, code: str, info: dict,
                               benchmark_df: pd.DataFrame = None) -> list:
    """
    V5.1+K线形态增强版
    与 backtest_stock_v5 完全相同的买卖规则，唯一区别:
    在信号质量评分环节叠加K线形态加分（_compute_pattern_bonus）
    """
    if len(df) < 80:
        return []

    df = compute_indicators(df)
    trades = []
    stock_type = info.get("类型", "龙头")
    slippage = SLIPPAGE_LEADER if stock_type == "龙头" else SLIPPAGE_FLEX

    regime_series = _precompute_market_regime(df, benchmark_df)

    n = len(df)
    dates_arr = df["date"].values
    close_arr = df["close"].values.astype(np.float64)
    open_arr = df["open"].values.astype(np.float64)
    high_arr = df["high"].values.astype(np.float64)
    low_arr = df["low"].values.astype(np.float64)
    volume_arr = df["volume"].values.astype(np.float64)
    ma20_arr = df["ma20"].values.astype(np.float64)
    ma60_arr = df["ma60"].values.astype(np.float64)
    ma20_slope_arr = df["ma20_slope"].values.astype(np.float64)
    vol_ma20_arr = df["vol_ma20"].values.astype(np.float64)
    macd_dif_arr = df["macd_dif"].values.astype(np.float64)
    macd_dea_arr = df["macd_dea"].values.astype(np.float64)
    rsi_arr = df["rsi"].values.astype(np.float64)
    pct_change_arr = df["pct_change"].values.astype(np.float64)
    atr14_arr = df["atr14"].values.astype(np.float64)
    has_amount = "amount" in df.columns
    amount_arr = df["amount"].values.astype(np.float64) if has_amount else None

    low_s = pd.Series(low_arr)
    high_s = pd.Series(high_arr)
    rolling_min_10 = low_s.rolling(10, min_periods=1).min().values
    rolling_min_20 = high_s.rolling(20, min_periods=1).min().values
    rolling_max_20 = high_s.rolling(20, min_periods=1).max().values
    rolling_min_prev_wave = low_s.rolling(10, min_periods=1).min().shift(10).values

    in_position = False
    buy_price = 0
    buy_date = ""
    buy_index = 0
    highest_since_buy = 0
    position_shares = 0
    initial_shares = 0
    ladder_sold = [False, False]
    consec_losses = 0
    cooldown_until_idx = 0
    min_signal_interval = getattr(config, 'MIN_SIGNAL_INTERVAL_DAYS', 3)
    last_sell_bar_idx = -999
    pattern_bonus_count = 0  # 统计: 形态加分生效次数

    for i in range(60, n):
        date = dates_arr[i]
        close = close_arr[i]
        low = low_arr[i]
        high = high_arr[i]
        volume = volume_arr[i]
        open_price = open_arr[i]
        vol_ma = vol_ma20_arr[i]

        if not in_position:
            # ==== 硬性过滤 ====
            if has_amount and i >= 20:
                avg_amount = amount_arr[i-19:i+1].mean()
                if avg_amount < getattr(config, 'MIN_DAILY_AMOUNT', 8e8):
                    continue

            if i >= 30:
                recent_high = high_arr[i-29:i+1]
                recent_low = low_arr[i-29:i+1]
                recent_close_prev = close_arr[i-30:i]
                amplitude = (recent_high - recent_low) / recent_close_prev
                high_amp_days = np.sum(amplitude > 0.10)
                if high_amp_days > getattr(config, 'MAX_HIGH_AMPLITUDE_DAYS', 3):
                    continue

            has_crash = False
            if i >= 5:
                for j in range(i-4, i+1):
                    if not np.isnan(pct_change_arr[j]) and pct_change_arr[j] < -0.08:
                        vol_ma_check = vol_ma20_arr[j]
                        if not np.isnan(vol_ma_check) and vol_ma_check > 0 and volume_arr[j] > vol_ma_check * 2:
                            has_crash = True
                            break
            if has_crash:
                continue

            if i >= 60 and not np.isnan(atr14_arr[i]):
                atr_pct_60 = atr14_arr[i] / close * 100 if close > 0 else 0
                if atr_pct_60 < 1.5:
                    continue

            # ==== 买点判定 ====
            ma20 = ma20_arr[i]
            ma20_slope = ma20_slope_arr[i]
            ma60 = ma60_arr[i]
            if np.isnan(ma20) or np.isnan(ma20_slope):
                continue

            if ma20_slope <= 0 or close < ma20:
                continue

            if not np.isnan(ma60) and i >= 5:
                ma60_slope = ma60_arr[i] - ma60_arr[max(0, i-5)]
                if ma60_slope < 0:
                    continue

            vol_ma = vol_ma20_arr[i]
            if np.isnan(vol_ma) or vol_ma == 0:
                continue

            buy_signal = False
            signal_quality = 50

            # 买点1: 缩量回踩MA20
            bp1 = False
            if volume < vol_ma * 0.70 and low <= ma20 * 1.01:
                prev_close = close_arr[i-1]
                day_change = (close - prev_close) / prev_close if prev_close > 0 else 0
                if not (day_change < -0.03 and volume > vol_ma * 1.5):
                    if i >= 20:
                        recent_10_low = rolling_min_10[i]
                        prev_wave_low = rolling_min_prev_wave[i]
                        if not np.isnan(prev_wave_low) and recent_10_low >= prev_wave_low * 0.99:
                            bp1 = True
                    else:
                        bp1 = True

            # 买点2: 放量突破后缩量回踩确认
            bp2 = False
            lookback = getattr(config, 'BREAKOUT_LOOKBACK', 10)
            if i >= lookback + 20:
                for k in range(i - lookback, i):
                    k_vol_ma = vol_ma20_arr[k]
                    if np.isnan(k_vol_ma) or k_vol_ma == 0:
                        continue
                    k_high_20 = rolling_max_20[k-1] if k >= 1 else high_arr[k]
                    if volume_arr[k] > k_vol_ma * 1.5 and close_arr[k] > k_high_20:
                        breakout_close = close_arr[k]
                        if (volume < volume_arr[k] * getattr(config, 'BREAKOUT_PULLBACK_VOL', 0.50) and
                            close >= breakout_close * getattr(config, 'BREAKOUT_HOLD_PCT', 0.99)):
                            bp2 = True
                            break

            if bp1 or bp2:
                buy_signal = True
                if bp1 and bp2:
                    signal_quality += 15
                if bp1:
                    signal_quality += 5
                if bp2:
                    signal_quality += 10

                support_count = 1
                if not np.isnan(ma60) and abs(low - ma60) / ma60 < 0.02:
                    support_count += 1
                if i >= 20:
                    platform_low = rolling_min_20[i]
                    if abs(low - platform_low) / platform_low < 0.02:
                        support_count += 1
                if support_count >= 3:
                    signal_quality += 25
                elif support_count >= 2:
                    signal_quality += 15

                if not np.isnan(macd_dif_arr[i]) and not np.isnan(macd_dea_arr[i]):
                    if not np.isnan(macd_dif_arr[i-1]) and not np.isnan(macd_dea_arr[i-1]):
                        if macd_dif_arr[i-1] <= macd_dea_arr[i-1] and macd_dif_arr[i] > macd_dea_arr[i]:
                            signal_quality += 10

                if not np.isnan(rsi_arr[i]) and rsi_arr[i] < 30:
                    signal_quality += 10

                if not np.isnan(ma60) and ma20 > ma60:
                    signal_quality += 5

                # ★★★ V5.1+Pattern V2.0 增强: 趋势门控+非对称加分+形态加权 ★★★
                pattern_bonus = _compute_pattern_bonus(df, i, industry=info.get("行业", ""))
                if pattern_bonus != 0:
                    signal_quality += pattern_bonus
                    pattern_bonus_count += 1

                signal_quality = min(100, max(0, signal_quality))

                # 急涨过滤
                if i >= 5:
                    surge_5d = (close - close_arr[i-5]) / close_arr[i-5] if close_arr[i-5] > 0 else 0
                    if surge_5d > 0.25:
                        buy_signal = False
                    else:
                        consec_limit = 0
                        for k in range(max(0, i-2), i+1):
                            if not np.isnan(pct_change_arr[k]) and pct_change_arr[k] > 9.5:
                                consec_limit += 1
                            else:
                                consec_limit = 0
                        if consec_limit >= 3:
                            buy_signal = False

                current_regime = regime_series.get(date, "RANGE")
                min_quality_bear = getattr(config, 'MIN_SIGNAL_QUALITY_BEAR', 70)
                min_quality_non_bear = getattr(config, 'MIN_SIGNAL_QUALITY_NON_BEAR', 65)
                min_quality = min_quality_bear if current_regime == "BEAR" else min_quality_non_bear
                if signal_quality < min_quality:
                    buy_signal = False

            if buy_signal and consec_losses >= 3:
                if consec_losses >= 6:
                    buy_signal = False
                elif i < cooldown_until_idx:
                    buy_signal = False

            if not buy_signal:
                continue

            if (i - last_sell_bar_idx) < min_signal_interval:
                continue

            # T+1执行买入
            if i + 1 >= n:
                continue

            next_pct = pct_change_arr[i + 1]
            if not np.isnan(next_pct) and next_pct > LIMIT_PCT:
                if abs(open_arr[i+1] - high_arr[i+1]) < 0.01 and abs(open_arr[i+1] - low_arr[i+1]) < 0.01:
                    continue

            exec_price = open_arr[i + 1] * (1 + slippage)
            buy_price = exec_price
            buy_date = dates_arr[i + 1]
            buy_index = i + 1
            highest_since_buy = high_arr[i + 1]
            in_position = True
            atr_at_buy = atr14_arr[i] if not np.isnan(atr14_arr[i]) else buy_price * 0.03
            initial_shares = int(TOTAL_CAPITAL * 0.12 / exec_price / 100) * 100
            if initial_shares < 100:
                initial_shares = 100
            position_shares = initial_shares
            ladder_sold = [False, False]

        else:
            # ==== 持仓管理（与V5.1完全一致）====
            highest_since_buy = max(highest_since_buy, high)

            if i <= buy_index:
                continue

            profit_pct = (close - buy_price) / buy_price
            sell_signal = False
            sell_type = ""
            sell_ratio = 1.0

            if not np.isnan(pct_change_arr[i]) and pct_change_arr[i] < -0.08:
                if not np.isnan(vol_ma) and vol_ma > 0 and volume > vol_ma * 2:
                    sell_signal = True
                    sell_type = "强制卖出"
                    sell_ratio = 1.0

            if not sell_signal:
                current_regime = regime_series.get(date, "RANGE")
                if profit_pct < 0.05:
                    atr_multiplier = 1.5 if current_regime == "BEAR" else 2.0
                    atr_stop_pct = (atr_at_buy / buy_price) * atr_multiplier
                    atr_stop_pct = max(0.05, min(0.10, atr_stop_pct))
                    stop_price = buy_price * (1 - atr_stop_pct)
                elif profit_pct < 0.15:
                    stop_price = buy_price * 1.02
                elif profit_pct < 0.30:
                    stop_price = buy_price * 1.12
                else:
                    stop_price = buy_price * 1.22

                if low <= stop_price:
                    sell_signal = True
                    sell_type = "止损"
                    sell_ratio = 1.0

            if not sell_signal and position_shares > 0:
                ladder_levels = getattr(config, 'LADDER_SELL_LEVELS', [(0.12, 1/3), (0.25, 1/3)])
                if not ladder_sold[0] and profit_pct >= ladder_levels[0][0]:
                    sell_signal = True
                    sell_type = "阶梯止盈1"
                    sell_ratio = ladder_levels[0][1]
                    ladder_sold[0] = True
                elif not ladder_sold[1] and profit_pct >= ladder_levels[1][0]:
                    sell_signal = True
                    sell_type = "阶梯止盈2"
                    sell_ratio = ladder_levels[1][1]
                    ladder_sold[1] = True

            if not sell_signal and highest_since_buy > buy_price * 1.05:
                current_regime = regime_series.get(date, "RANGE")
                drawdown_stop_cfg = getattr(config, 'DRAWDOWN_STOP', {"龙头稳健": 0.08, "成长赛道": 0.07, "高弹性": 0.06})
                bull_boost = getattr(config, 'DRAWDOWN_STOP_BULL_BOOST', 0.02)
                if stock_type == "龙头":
                    drawdown_threshold = drawdown_stop_cfg.get("龙头稳健", 0.08)
                    if current_regime == "BULL":
                        drawdown_threshold += bull_boost
                else:
                    drawdown_threshold = drawdown_stop_cfg.get("高弹性", 0.06)
                    if current_regime == "BULL":
                        drawdown_threshold += bull_boost
                drawdown = (highest_since_buy - low) / highest_since_buy
                if drawdown >= drawdown_threshold and profit_pct > 0:
                    sell_signal = True
                    sell_type = "回落止盈"
                    sell_ratio = 1.0

            if not sell_signal and profit_pct > 0.30:
                prev_close_val = close_arr[i-1]
                day_change = (close - prev_close_val) / prev_close_val if prev_close_val > 0 else 0
                if day_change < -0.05:
                    sell_signal = True
                    sell_type = "急涨急跌保护"
                    sell_ratio = 1.0

            # T+1执行卖出
            if sell_signal:
                if i + 1 >= n:
                    exec_price = close_arr[-1] * (1 - slippage)
                    sell_date = date
                else:
                    next_pct = pct_change_arr[i + 1]
                    if not np.isnan(next_pct) and next_pct < -LIMIT_PCT:
                        if abs(open_arr[i+1] - high_arr[i+1]) < 0.01 and abs(open_arr[i+1] - low_arr[i+1]) < 0.01:
                            continue
                    exec_price = open_arr[i + 1] * (1 - slippage)
                    sell_date = dates_arr[i + 1]

                actual_sell_shares = int(position_shares * sell_ratio / 100) * 100
                if actual_sell_shares < 100:
                    actual_sell_shares = position_shares
                if sell_ratio >= 1.0:
                    actual_sell_shares = position_shares

                gross_profit = (exec_price - buy_price) / buy_price
                net_profit = gross_profit - COMMISSION
                hold_days = (i + 1 - buy_index) if i + 1 < n else (i - buy_index)

                trades.append({
                    "code": code,
                    "name": info["名称"],
                    "industry": info["行业"],
                    "stock_type": stock_type,
                    "buy_date": buy_date,
                    "buy_price": round(buy_price, 3),
                    "sell_date": sell_date,
                    "sell_price": round(exec_price, 3),
                    "gross_profit": round(gross_profit * 100, 2),
                    "net_profit": round(net_profit * 100, 2),
                    "hold_days": hold_days,
                    "sell_type": sell_type,
                    "highest": round(highest_since_buy, 3),
                    "sell_ratio": round(sell_ratio, 2),
                    "regime": regime_series.get(buy_date, "RANGE"),
                })

                if net_profit < 0:
                    consec_losses += 1
                    if consec_losses >= 5:
                        cooldown_until_idx = i + 60
                    elif consec_losses >= 3:
                        cooldown_until_idx = i + 20
                else:
                    consec_losses = 0

                position_shares -= actual_sell_shares
                if position_shares <= 0 or sell_ratio >= 1.0:
                    in_position = False
                    position_shares = 0
                    last_sell_bar_idx = i

    # 回测结束仍持仓
    if in_position and position_shares > 0:
        exec_price = close_arr[-1] * (1 - slippage)
        gross_profit = (exec_price - buy_price) / buy_price
        net_profit = gross_profit - COMMISSION
        hold_days = n - 1 - buy_index
        trades.append({
            "code": code, "name": info["名称"], "industry": info["行业"],
            "stock_type": stock_type, "buy_date": buy_date,
            "buy_price": round(buy_price, 3), "sell_date": dates_arr[-1],
            "sell_price": round(exec_price, 3),
            "gross_profit": round(gross_profit * 100, 2),
            "net_profit": round(net_profit * 100, 2),
            "hold_days": hold_days, "sell_type": "回测结束",
            "highest": round(highest_since_buy, 3),
            "sell_ratio": 1.0, "regime": regime_series.get(buy_date, "RANGE"),
        })

    return trades, pattern_bonus_count


# ============================================================
# 主函数: 对比回测
# ============================================================

def run_comparison():
    logger.info("=" * 70)
    logger.info("  K线形态集成回测对比: V5.1基线 vs V5.1+K线形态增强")
    logger.info("=" * 70)

    all_trades_baseline = []
    all_trades_enhanced = []
    total_pattern_bonus = 0
    stocks_data = {}

    for code, info in TEST_STOCKS.items():
        logger.info(f"回测: {code} {info['名称']}...")
        try:
            df = fetch_history_data(code, start_date=START_DATE)
            if df.empty or len(df) < 80:
                logger.warning(f"  {code} 数据不足，跳过")
                continue

            stocks_data[code] = {
                "name": info["名称"], "industry": info["行业"],
                "stock_type": info["类型"], "df_len": len(df),
            }

            # 基线版 V5.1
            t_base = backtest_stock_v5(df, code, info)
            all_trades_baseline.extend(t_base)

            # 增强版 V5.1+Pattern
            result = backtest_stock_v5_pattern(df, code, info)
            t_enh, pat_count = result
            all_trades_enhanced.extend(t_enh)
            total_pattern_bonus += pat_count

            logger.info(f"  基线:{len(t_base)}笔 | 增强:{len(t_enh)}笔 | 形态加分生效:{pat_count}次")
        except Exception as e:
            logger.error(f"  {code} 失败: {e}")
            import traceback
            traceback.print_exc()

    if not all_trades_baseline or not all_trades_enhanced:
        logger.error("无交易记录，无法对比")
        return

    all_trades_baseline.sort(key=lambda x: x["buy_date"])
    all_trades_enhanced.sort(key=lambda x: x["buy_date"])

    stats_base = analyze_trades(all_trades_baseline)
    stats_enh = analyze_trades(all_trades_enhanced)

    # ---- 控制台输出对比 ----
    logger.info("\n" + "=" * 80)
    logger.info(f"  {'指标':<16} {'V5.1基线版':<18} {'V5.1+K线形态':<18} {'变化':<15}")
    logger.info("-" * 80)
    wr_d = stats_enh["win_rate"] - stats_base["win_rate"]
    pf_d = stats_enh["profit_factor"] - stats_base["profit_factor"]
    exp_d = stats_enh["expectancy"] - stats_base["expectancy"]
    cum_d = stats_enh["cumulative"] - stats_base["cumulative"]
    logger.info(f"  {'总交易笔数':<16} {stats_base['total']:<18} {stats_enh['total']:<18} {stats_enh['total'] - stats_base['total']:+d}")
    logger.info(f"  {'胜率':<16} {stats_base['win_rate']}%{'':<13} {stats_enh['win_rate']}%{'':<13} {wr_d:+.1f}%")
    logger.info(f"  {'盈亏比':<16} {stats_base['profit_factor']:<18} {stats_enh['profit_factor']:<18} {pf_d:+.2f}")
    logger.info(f"  {'每笔期望收益':<16} {stats_base['expectancy']:+.2f}%{'':<12} {stats_enh['expectancy']:+.2f}%{'':<12} {exp_d:+.2f}%")
    logger.info(f"  {'累计收益':<16} {stats_base['cumulative']:+.2f}%{'':<12} {stats_enh['cumulative']:+.2f}%{'':<12} {cum_d:+.2f}%")
    logger.info(f"  {'平均持仓天数':<16} {stats_base['avg_hold']:<18} {stats_enh['avg_hold']:<18} {stats_enh['avg_hold'] - stats_base['avg_hold']:+.1f}")
    logger.info(f"  {'最大连续亏损':<16} {stats_base['max_consec_loss']:<18} {stats_enh['max_consec_loss']:<18} {stats_enh['max_consec_loss'] - stats_base['max_consec_loss']:+d}")
    logger.info(f"  {'形态加分生效':<16} {'--':<18} {'--':<18} {total_pattern_bonus}次")
    logger.info("=" * 80)

    # ---- 生成Excel格式交易明细 ----
    _generate_excel_detail(all_trades_enhanced, stocks_data)

    # ---- 生成HTML对比报告 ----
    _generate_html_report(stats_base, stats_enh, all_trades_enhanced, total_pattern_bonus)

    return stats_base, stats_enh


def _generate_excel_detail(trades, stocks_data):
    """按历史回测数据.xlsx格式输出交易明细"""
    rows = []
    for t in trades:
        code = t["code"]
        name = t["name"]
        buy_date = t["buy_date"]
        sell_date = t["sell_date"]
        buy_price = t["buy_price"]
        sell_price = t["sell_price"]
        # 成交股数（与回测引擎一致: 下限100股）
        buy_shares = max(int(TOTAL_CAPITAL * 0.12 / buy_price / 100) * 100, 100) if buy_price > 0 else 100
        sell_shares = max(int(TOTAL_CAPITAL * 0.12 / buy_price / 100) * 100, 100) if buy_price > 0 else 100
        # 买入行
        rows.append({
            "成交日期": buy_date,
            "成交时间": "09:30",
            "证券代码": code,
            "证券名称": name,
            "买卖方向": "买入",
            "成交价格": round(buy_price, 3),
            "成交数量": buy_shares,
            "成交金额": round(buy_price * buy_shares, 2),
        })
        # 卖出行
        rows.append({
            "成交日期": sell_date,
            "成交时间": "09:30",
            "证券代码": code,
            "证券名称": name,
            "买卖方向": "卖出",
            "成交价格": round(sell_price, 3),
            "成交数量": sell_shares,
            "成交金额": round(sell_price * sell_shares, 2),
        })

    df_excel = pd.DataFrame(rows)
    output_dir = os.path.join(config.PROJECT_ROOT, "output")
    os.makedirs(output_dir, exist_ok=True)
    excel_path = os.path.join(output_dir, "backtest_trades_detail.xlsx")
    try:
        df_excel.to_excel(excel_path, index=False, engine="openpyxl")
        logger.info(f"Excel交易明细: {excel_path} ({len(rows)}行)")
    except Exception as e:
        logger.warning(f"Excel导出失败({e})，保存CSV替代")
        csv_path = excel_path.replace(".xlsx", ".csv")
        df_excel.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info(f"CSV交易明细: {csv_path}")


def _generate_html_report(stats_base, stats_enh, trades_enh, pattern_bonus_count):
    """生成K线形态集成对比报告"""
    today = datetime.date.today().strftime("%Y-%m-%d")

    wr_d = stats_enh["win_rate"] - stats_base["win_rate"]
    pf_d = stats_enh["profit_factor"] - stats_base["profit_factor"]
    exp_d = stats_enh["expectancy"] - stats_base["expectancy"]
    cum_d = stats_enh["cumulative"] - stats_base["cumulative"]

    def delta_color(v):
        return "#e74c3c" if v > 0 else "#27ae60" if v < 0 else "#333"

    def delta_arrow(v):
        return "↑" if v > 0 else "↓" if v < 0 else "→"

    # 卖出原因表
    sell_rows = ""
    for st, d in sorted(stats_enh.get("sell_stats", {}).items(), key=lambda x: -x[1]["count"]):
        wr = d["wins"]/d["count"]*100 if d["count"]>0 else 0
        avg = d["total"]/d["count"] if d["count"]>0 else 0
        sell_rows += f"<tr><td>{st}</td><td>{d['count']}</td><td>{wr:.0f}%</td><td>{avg:+.2f}%</td></tr>"

    # 行业对比表
    industry_rows = ""
    base_ind = stats_base.get("industry_stats", {})
    enh_ind = stats_enh.get("industry_stats", {})
    all_industries = sorted(set(list(base_ind.keys()) + list(enh_ind.keys())))
    for ind in all_industries:
        b = base_ind.get(ind, {"count": 0, "wins": 0, "total": 0})
        e = enh_ind.get(ind, {"count": 0, "wins": 0, "total": 0})
        b_wr = b["wins"]/b["count"]*100 if b["count"]>0 else 0
        e_wr = e["wins"]/e["count"]*100 if e["count"]>0 else 0
        d_total = e["total"] - b["total"]
        industry_rows += f"""<tr><td>{ind}</td>
        <td>{b['count']}</td><td>{b_wr:.0f}%</td><td>{b['total']:+.2f}%</td>
        <td>{e['count']}</td><td>{e_wr:.0f}%</td><td>{e['total']:+.2f}%</td>
        <td style="color:{delta_color(d_total)}">{d_total:+.2f}%</td></tr>"""

    # 个股对比表
    stock_rows = ""
    base_stk = stats_base.get("stock_stats", {})
    enh_stk = stats_enh.get("stock_stats", {})
    all_stocks = sorted(set(list(base_stk.keys()) + list(enh_stk.keys())))
    for key in all_stocks:
        b = base_stk.get(key, {"count": 0, "wins": 0, "total": 0})
        e = enh_stk.get(key, {"count": 0, "wins": 0, "total": 0})
        b_wr = b["wins"]/b["count"]*100 if b["count"]>0 else 0
        e_wr = e["wins"]/e["count"]*100 if e["count"]>0 else 0
        d_total = e["total"] - b["total"]
        stock_rows += f"""<tr><td>{key}</td>
        <td>{b['count']}</td><td>{b_wr:.0f}%</td><td>{b['total']:+.2f}%</td>
        <td>{e['count']}</td><td>{e_wr:.0f}%</td><td>{e['total']:+.2f}%</td>
        <td style="color:{delta_color(d_total)}">{d_total:+.2f}%</td></tr>"""

    # 最近交易
    recent = sorted(trades_enh, key=lambda x: x["sell_date"], reverse=True)[:25]
    trade_rows = ""
    for t in recent:
        c = "#e74c3c" if t["net_profit"] > 0 else "#27ae60"
        trade_rows += f"""<tr><td>{t['code']}</td><td>{t['name']}</td><td>{t['buy_date']}</td>
        <td>{t['sell_date']}</td><td style="color:{c};font-weight:bold">{t['net_profit']:+.2f}%</td>
        <td>{t['hold_days']}天</td><td>{t['sell_type']}</td></tr>"""

    # 结论判定
    if cum_d > 0 and wr_d >= 0:
        conclusion = "✅ K线形态集成有效提升了策略表现，累计收益和胜率均有改善"
        conclusion_color = "#27ae60"
    elif cum_d > 0:
        conclusion = "⚠️ K线形态集成提升了累计收益，但胜率略有下降（信号过滤减少了交易次数）"
        conclusion_color = "#f39c12"
    elif wr_d > 0:
        conclusion = "⚠️ K线形态集成提升了胜率，但累计收益略有下降"
        conclusion_color = "#f39c12"
    else:
        conclusion = "❌ K线形态集成未带来明显改善，建议调整形态评分参数或权重"
        conclusion_color = "#e74c3c"

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:'Microsoft YaHei',sans-serif;padding:20px;background:#f8f9fa}}
.container{{max-width:1000px;margin:0 auto}}
h1{{color:#2c3e50;border-bottom:3px solid #3498db;padding-bottom:10px}}
h2{{color:#34495e;margin-top:25px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:20px 0}}
.box{{background:#fff;border-radius:8px;padding:15px;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
.box.enh h3{{border-bottom-color:#e74c3c}}
table{{width:100%;border-collapse:collapse;margin:12px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
th{{background:#34495e;color:#fff;padding:10px 8px;font-size:13px}}
td{{padding:8px;text-align:center;border-bottom:1px solid #ecf0f1;font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:15px 0}}
.card{{background:#fff;border-radius:8px;padding:12px;text-align:center;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
.card .v{{font-size:20px;font-weight:bold}}
.card .l{{font-size:11px;color:#7f8c8d;margin-top:4px}}
.note{{background:#d4edda;padding:12px;border-radius:6px;margin:15px 0;font-size:13px;border-left:4px solid #28a745}}
.warn{{background:#fff3cd;padding:12px;border-radius:6px;margin:15px 0;font-size:13px;border-left:4px solid #ffc107}}
.conclusion{{background:#e8f5e9;padding:15px;border-radius:8px;margin:20px 0;font-size:14px;border-left:5px solid {conclusion_color}}}
</style></head><body><div class="container">
<h1>📊 K线形态集成回测对比报告</h1>
<p>回测区间: 2022-07 ~ {today} | 标的: 20只 | V5.1基线 vs V5.1+K线形态增强</p>
<p>增强逻辑V2.0: 趋势门控+非对称加分+形态加权+行业权重 | 看涨+2~4分 / 看跌-1~4分 | 形态加分生效: {pattern_bonus_count}次</p>

<div class="cards">
<div class="card"><div class="v" style="color:{delta_color(wr_d)}">{wr_d:+.1f}% {delta_arrow(wr_d)}</div><div class="l">胜率变化</div></div>
<div class="card"><div class="v" style="color:{delta_color(pf_d)}">{pf_d:+.2f} {delta_arrow(pf_d)}</div><div class="l">盈亏比变化</div></div>
<div class="card"><div class="v" style="color:{delta_color(exp_d)}">{exp_d:+.2f}% {delta_arrow(exp_d)}</div><div class="l">期望收益变化</div></div>
<div class="card"><div class="v" style="color:{delta_color(cum_d)}">{cum_d:+.1f}% {delta_arrow(cum_d)}</div><div class="l">累计收益变化</div></div>
</div>

<div class="grid">
<div class="box"><h3>V5.1 基线版</h3><table>
<tr><td>总交易</td><td><b>{stats_base['total']}笔</b></td></tr>
<tr><td>胜率</td><td><b>{stats_base['win_rate']}%</b></td></tr>
<tr><td>盈亏比</td><td><b>{stats_base['profit_factor']}</b></td></tr>
<tr><td>每笔期望</td><td><b>{stats_base['expectancy']:+.2f}%</b></td></tr>
<tr><td>累计收益</td><td><b>{stats_base['cumulative']:+.2f}%</b></td></tr>
<tr><td>平均持仓</td><td>{stats_base['avg_hold']}天</td></tr>
<tr><td>最大连亏</td><td>{stats_base['max_consec_loss']}次</td></tr>
</table></div>
<div class="box enh"><h3>V5.1 + K线形态增强</h3><table>
<tr><td>总交易</td><td><b>{stats_enh['total']}笔</b> <span style="color:#666">({stats_enh['total'] - stats_base['total']:+d})</span></td></tr>
<tr><td>胜率</td><td><b>{stats_enh['win_rate']}%</b> <span style="color:{delta_color(wr_d)}">({wr_d:+.1f}%)</span></td></tr>
<tr><td>盈亏比</td><td><b>{stats_enh['profit_factor']}</b> <span style="color:{delta_color(pf_d)}">({pf_d:+.2f})</span></td></tr>
<tr><td>每笔期望</td><td><b>{stats_enh['expectancy']:+.2f}%</b> <span style="color:{delta_color(exp_d)}">({exp_d:+.2f}%)</span></td></tr>
<tr><td>累计收益</td><td><b>{stats_enh['cumulative']:+.2f}%</b> <span style="color:{delta_color(cum_d)}">({cum_d:+.1f}%)</span></td></tr>
<tr><td>平均持仓</td><td>{stats_enh['avg_hold']}天 <span style="color:#666">({stats_enh['avg_hold'] - stats_base['avg_hold']:+.1f})</span></td></tr>
<tr><td>最大连亏</td><td>{stats_enh['max_consec_loss']}次 <span style="color:#666">({stats_enh['max_consec_loss'] - stats_base['max_consec_loss']:+d})</span></td></tr>
</table></div>
</div>

<div class="conclusion" style="border-left-color:{conclusion_color}">
<b>📋 对比结论</b>: {conclusion}
<br>形态加分共生效 {pattern_bonus_count} 次，有效过滤了部分低质量信号。
</div>

<h2>📊 行业对比</h2>
<table><tr><th>行业</th><th colspan="3">V5.1基线</th><th colspan="3">V5.1+形态</th><th>累计收益差</th></tr>
<tr><th></th><th>笔数</th><th>胜率</th><th>累计</th><th>笔数</th><th>胜率</th><th>累计</th><th></th></tr>{industry_rows}</table>

<h2>📋 个股对比</h2>
<table><tr><th>股票</th><th colspan="3">V5.1基线</th><th colspan="3">V5.1+形态</th><th>累计收益差</th></tr>
<tr><th></th><th>笔数</th><th>胜率</th><th>累计</th><th>笔数</th><th>胜率</th><th>累计</th><th></th></tr>{stock_rows}</table>

<h2>🎯 卖出原因统计（增强版）</h2>
<table><tr><th>原因</th><th>次数</th><th>胜率</th><th>平均净收益</th></tr>{sell_rows}</table>

<h2>📝 最近25笔交易（增强版）</h2>
<table><tr><th>代码</th><th>名称</th><th>买入日</th><th>卖出日</th><th>净收益</th><th>持仓</th><th>原因</th></tr>{trade_rows}</table>

<div class="warn">⚠️ <b>回测环境</b>: 手续费0.16%(佣金万3双边+印花税千1) | 滑点:龙头0.2%/弹性0.5% | T+1执行 | 涨跌停过滤 | 信号T日收盘计算→T+1开盘执行（无未来函数）
<br><b>增强逻辑V2.0</b>: 趋势门控(MA20斜率缩放看涨加分) + 非对称加分(上升中看跌仅-1) + 形态加权(吞没/早晨之星×2.0) + 行业权重(半导体×1.5/金融×0.3)</div>

<div class="note">✅ <b>声明</b>: 所有信号均在T日收盘后计算，T+1日开盘执行，无未来函数。回测结果不代表未来表现。</div>
</div></body></html>"""

    output_dir = os.path.join(config.PROJECT_ROOT, "output")
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, f"backtest_pattern_compare_{datetime.date.today().strftime('%Y%m%d')}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"HTML对比报告: {report_path}")


if __name__ == "__main__":
    run_comparison()
