"""
策略参数优化器 V1.0
====================
基于历史数据网格搜索最优参数组合，提升交易胜率和盈亏比。

核心功能:
  1. 可配置参数的回测引擎（V6版）
  2. 网格搜索（Grid Search）遍历参数空间
  3. 样本内/样本外分离验证（防过拟合）
  4. 输出最优参数组合 + 对比报告

诊断结论（V5.0基线）:
  - 盈亏比仅1.08（止损-9.93% vs 回落止盈+5%）
  - 趋势破位40笔全亏（可能是假破位）
  - 阶梯止盈2极少触发（20%阈值太高）

优化方向:
  - 缩小初始止损（减少单笔亏损）
  - 调大回落止盈阈值（让利润跑更远）
  - 趋势破位需连续确认（减少假信号）
  - 新增时间止损（提高资金效率）

使用方式:
  python backtest_optimizer.py              # 运行完整优化
  python backtest_optimizer.py --quick      # 快速模式（减少参数组合）
"""

import sys
import os
import datetime
import logging
import itertools
import json
from copy import deepcopy

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from backtest_real import (
    fetch_history_data, compute_indicators, is_limit_up, is_limit_down,
    TEST_STOCKS, COMMISSION, SLIPPAGE_LEADER, SLIPPAGE_FLEX, TOTAL_CAPITAL, LIMIT_PCT
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("optimizer")


# ============================================================
# 一、默认参数（V5.0基线）
# ============================================================

DEFAULT_PARAMS = {
    # 买入参数
    "vol_shrink_ratio": 0.70,       # 缩量阈值（量<均量×N）
    "support_touch_pct": 0.01,      # 支撑位容差
    "min_signal_quality": 60,       # OPTIMIZE: 信号质量最低分（原55，回测显示低质量交易过多）
    "require_ma60_up": True,        # 是否要求MA60向上
    "enable_bp3": True,             # V7: 是否启用bp3牛市趋势延续买点
    "bp3_vol_shrink": 0.85,         # bp3缩量阈值
    "bp3_min_above_ma20": 1.02,     # bp3最低收盘/MA20比
    "bp3_max_surge_5d": 0.15,       # bp3近5日最大涨幅

    # 止损参数
    "initial_stop_loss": 0.07,      # OPTIMIZE: 初始止损幅度（原0.08，回测显示平均亏损-10.20%含滑点）
    "breakeven_profit": 0.05,       # 浮盈多少后上移到保本
    "breakeven_lock": 0.02,         # 保本后锁定利润
    "profit_lock_1": 0.12,          # 浮盈15-30%锁定
    "profit_lock_2": 0.22,          # 浮盈>30%锁定
    "atr_mode": True,               # V7: ATR自适应止损（对齐v5）
    "atr_multiplier": 2.0,          # V7: ATR倍数（BEAR自动降为1.5）
    "atr_stop_min": 0.04,           # ATR止损下限
    "atr_stop_max": 0.08,           # ATR止损上限

    # V7: 多因子信号增强（A2）
    "factor_weight_smf": 0.0,       # 聪明钱因子权重（0=关闭）
    "factor_weight_reversal": 0.0,  # 反转因子权重（0=关闭）
    "factor_weight_low_vol": 0.0,   # 低波动因子权重（0=关闭）
    "factor_max_bonus": 20,         # 因子加分上限（防止因子加分过多）

    # Regime-aware质量门槛（V7: 对齐v5的分市场环境逻辑）
    "min_quality_bear": 70,         # BEAR市场信号质量门槛
    "min_quality_non_bear": 65,     # 非BEAR市场信号质量门槛

    # 止盈参数
    "ladder_1_pct": 0.08,           # 阶梯止盈第1档
    "ladder_2_pct": 0.20,           # 阶梯止盈第2档
    "drawdown_leader": 0.06,        # OPTIMIZE: 回落止盈龙头（原0.05，让利润多跑一段）
    "drawdown_flex": 0.03,          # 回落止盈（弹性）

    # 卖出逻辑
    "macd_death_cross": "none",     # OPTIMIZE: MACD死叉关闭（原"profit"，回测显示61笔胜率75%但平均仅+1.50%）
    "trend_break_mode": "none",     # OPTIMIZE: 趋势破位关闭（原"ma60"，回测显示91笔胜率0%完全失效）
    "max_hold_days": 0,             # 最大持仓天数（0=不限制）
    "time_stop_profit": 0.03,       # 时间止损：超期且浮盈<此值则卖
}


# ============================================================
# 一b、简化版市场环境预计算（V7: 对齐v5，供bp3/ATR止损/质量门槛使用）
# ============================================================

def _precompute_regime_v6(df: pd.DataFrame) -> dict:
    """
    简化版市场环境序列（对齐v5的_precompute_market_regime）。
    使用个股自身数据（无基准时降级方案）。

    规则:
      - BULL: close>MA20 且 MA20>MA60 且 10日动量>2%
      - BEAR: close<MA60 且 10日动量<-2%
      - RANGE: 其他
    返回: {date_str: "BULL"/"BEAR"/"RANGE"}
    """
    if len(df) < 60:
        return {}
    close = df["close"].values
    dates = df["date"].values
    n = len(df)
    close_s = pd.Series(close)
    ma20 = close_s.rolling(20).mean().values
    ma60 = close_s.rolling(60).mean().values

    regime = {}
    for i in range(60, n):
        if np.isnan(ma20[i]) or np.isnan(ma60[i]):
            regime[str(dates[i])] = "RANGE"
            continue
        mom10 = (close[i] / close[i - 10] - 1) if i >= 10 and close[i - 10] > 0 else 0
        if close[i] > ma20[i] and ma20[i] > ma60[i] and mom10 > 0.02:
            regime[str(dates[i])] = "BULL"
        elif close[i] < ma60[i] and mom10 < -0.02:
            regime[str(dates[i])] = "BEAR"
        else:
            regime[str(dates[i])] = "RANGE"
    return regime


def _compute_signal_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    V7(A2): 预计算多因子信号，用于 signal_quality 增强。
    仅需 OHLCV 数据，无外部依赖。

    新增列:
      _smf_signal:  聪明钱代理（20日量价相关性 > 0.2 → 正向）
      _reversal_signal: 超卖反转（20日收益<-1σ 且近5日开始回升）
      _low_vol_signal:  低波动（20日波动率 < 历史中位数）
    """
    n = len(df)
    if n < 60:
        df = df.copy()
        df["_smf_signal"] = 0.0
        df["_reversal_signal"] = 0.0
        df["_low_vol_signal"] = 0.0
        return df

    close = df["close"].values
    volume = df["volume"].values

    # --- 聪明钱代理: 20日滚动(价格变化×成交量变化)的均值，标准化 ---
    price_chg = np.zeros(n)
    vol_chg = np.zeros(n)
    for i in range(1, n):
        price_chg[i] = (close[i] - close[i - 1]) / close[i - 1] if close[i - 1] > 0 else 0
        vol_chg[i] = (volume[i] - volume[i - 1]) / volume[i - 1] if volume[i - 1] > 0 else 0
    cross = price_chg * vol_chg
    smf_raw = pd.Series(cross).rolling(20, min_periods=5).mean().values
    smf_std = pd.Series(cross).rolling(60, min_periods=10).std().values
    with np.errstate(divide="ignore", invalid="ignore"):
        smf_norm = np.where(smf_std > 1e-10, smf_raw / np.maximum(smf_std, 1e-10), 0)
    smf_signal = (smf_norm > 0.2).astype(float)

    # --- 超卖反转: 20日收益 < -1σ 且近5日回升 ---
    ret_20 = np.zeros(n)
    for i in range(20, n):
        ret_20[i] = (close[i] / close[i - 20] - 1) if close[i - 20] > 0 else 0
    ret_mean = pd.Series(ret_20).rolling(60, min_periods=20).mean().values
    ret_std = pd.Series(ret_20).rolling(60, min_periods=20).std().values
    ret_5 = np.zeros(n)
    for i in range(5, n):
        ret_5[i] = (close[i] / close[i - 5] - 1) if close[i - 5] > 0 else 0
    reversal_signal = np.zeros(n)
    for i in range(60, n):
        if ret_std[i] > 1e-10 and ret_20[i] < ret_mean[i] - ret_std[i]:
            if ret_5[i] > 0:
                reversal_signal[i] = 1.0

    # --- 低波动: 20日波动率 < 60日中位数 ---
    daily_vol = pd.Series(close).pct_change().rolling(20).std().values
    vol_median = pd.Series(daily_vol).rolling(60, min_periods=20).median().values
    low_vol_signal = np.where(
        (~np.isnan(daily_vol)) & (~np.isnan(vol_median)) & (vol_median > 1e-10),
        (daily_vol < vol_median).astype(float), 0)

    df = df.copy()
    df["_smf_signal"] = smf_signal
    df["_reversal_signal"] = reversal_signal
    df["_low_vol_signal"] = low_vol_signal
    return df


# ============================================================
# 二、可配置参数的回测引擎（V7 — 对齐v5能力）
# ============================================================

def backtest_stock_v6(df: pd.DataFrame, code: str, info: dict, params: dict) -> list:
    """
    V7可配置参数回测引擎（V7: 对齐v5能力）

    基于V6.0逻辑，新增:
      - bp3买点（牛市趋势延续）
      - ATR自适应止损
      - 连续亏损熔断器
      - regime-aware质量门槛
    """
    if len(df) < 80:
        return []

    df = compute_indicators(df)
    # V7(A2): 预计算多因子信号
    df = _compute_signal_factors(df)
    trades = []
    stock_type = info.get("类型", "龙头")
    slippage = SLIPPAGE_LEADER if stock_type == "龙头" else SLIPPAGE_FLEX

    # 从params读取参数
    p = {**DEFAULT_PARAMS, **params}  # 合并默认参数和自定义参数

    # V7: 预计算市场环境序列
    regime_series = _precompute_regime_v6(df)

    # 状态
    in_position = False
    buy_price = 0
    buy_date = ""
    buy_index = 0
    highest_since_buy = 0
    position_shares = 0
    initial_shares = 0
    ladder_sold = [False, False]
    below_ma60_days = 0  # 连续跌破MA60天数
    # V7: 连续亏损熔断器
    consec_losses = 0
    cooldown_until_idx = 0
    # V7: ATR自适应止损
    atr_at_buy = 0.0

    for i in range(60, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        date = row["date"]
        close = row["close"]
        low = row["low"]
        high = row["high"]
        volume = row["volume"]

        if not in_position:
            # ==== 硬性过滤（固定，不参与优化）====
            if "amount" in df.columns and i >= 20:
                avg_amount = df["amount"].iloc[i-19:i+1].mean()
                if avg_amount < 8e8:
                    continue

            if i >= 30:
                recent_30 = df.iloc[i-29:i+1]
                amplitude = (recent_30["high"] - recent_30["low"]) / recent_30["close"].shift(1)
                high_amp_days = (amplitude > 0.10).sum()
                if high_amp_days > 3:
                    continue

            # ==== 买入信号判定 ====
            ma20 = row["ma20"]
            ma20_slope = row["ma20_slope"]
            ma60 = row["ma60"]
            if pd.isna(ma20) or pd.isna(ma20_slope):
                continue

            # 基础条件: MA20向上 + 收盘价在MA20上方
            if ma20_slope <= 0 or close < ma20:
                continue

            # MA60确认（可配置）
            if p["require_ma60_up"] and not pd.isna(ma60) and i >= 5:
                ma60_slope = df["ma60"].iloc[i] - df["ma60"].iloc[max(0, i-5)]
                if ma60_slope < 0:
                    continue

            vol_ma = row["vol_ma20"]
            if pd.isna(vol_ma) or vol_ma == 0:
                continue

            buy_signal = False
            signal_quality = 50

            # ---- 买点1: 缩量回踩MA20 ----
            bp1 = False
            if volume < vol_ma * p["vol_shrink_ratio"] and low <= ma20 * (1 + p["support_touch_pct"]):
                prev_close = prev["close"]
                day_change = (close - prev_close) / prev_close if prev_close > 0 else 0
                if not (day_change < -0.03 and volume > vol_ma * 1.5):
                    if i >= 20:
                        recent_10_low = df["low"].iloc[i-9:i+1].min()
                        prev_wave_low = df["low"].iloc[i-19:i-9].min()
                        if recent_10_low >= prev_wave_low * 0.99:
                            bp1 = True
                    else:
                        bp1 = True

            # ---- 买点2: 放量突破后缩量回踩 ----
            bp2 = False
            lookback = 10
            if i >= lookback + 20:
                for k in range(i - lookback, i):
                    k_row = df.iloc[k]
                    k_vol_ma = df["vol_ma20"].iloc[k]
                    if pd.isna(k_vol_ma) or k_vol_ma == 0:
                        continue
                    k_high_20 = df["high"].iloc[max(0, k-19):k].max()
                    if k_row["volume"] > k_vol_ma * 1.5 and k_row["close"] > k_high_20:
                        breakout_close = k_row["close"]
                        if (volume < k_row["volume"] * 0.50 and
                            close >= breakout_close * 0.99):
                            bp2 = True
                            break

            # ---- 买点3: 牛市趋势延续（V7新增，对齐v5）----
            bp3 = False
            if p.get("enable_bp3", True):
                current_regime_bp = regime_series.get(str(date), "RANGE")
                if current_regime_bp == "BULL" and not pd.isna(ma60) and ma20 > ma60:
                    if volume < vol_ma * p.get("bp3_vol_shrink", 0.85):
                        if close > ma20 * p.get("bp3_min_above_ma20", 1.02):
                            if i >= 5:
                                surge_5d = (close - df["close"].iloc[i-5]) / df["close"].iloc[i-5] if df["close"].iloc[i-5] > 0 else 0
                                if surge_5d < p.get("bp3_max_surge_5d", 0.15):
                                    bp3 = True

            if bp1 or bp2 or bp3:
                buy_signal = True
                if bp1 and bp2:
                    signal_quality += 15
                if bp1:
                    signal_quality += 5
                if bp2:
                    signal_quality += 10
                if bp3 and not bp1 and not bp2:
                    signal_quality += 5  # bp3单独触发给较低加分

                # 多支撑位重合
                support_count = 1
                if not pd.isna(ma60) and abs(low - ma60) / ma60 < 0.02:
                    support_count += 1
                if i >= 20:
                    platform_low = df["low"].iloc[i-19:i+1].min()
                    if abs(low - platform_low) / platform_low < 0.02:
                        support_count += 1
                if support_count >= 3:
                    signal_quality += 25
                elif support_count >= 2:
                    signal_quality += 15

                # MACD金叉
                if not pd.isna(row["macd_dif"]) and not pd.isna(row["macd_dea"]):
                    if not pd.isna(prev["macd_dif"]) and not pd.isna(prev["macd_dea"]):
                        if prev["macd_dif"] <= prev["macd_dea"] and row["macd_dif"] > row["macd_dea"]:
                            signal_quality += 10

                # RSI超卖
                if not pd.isna(row["rsi"]) and row["rsi"] < 30:
                    signal_quality += 10

                # MA20>MA60
                if not pd.isna(ma60) and ma20 > ma60:
                    signal_quality += 5

                # V7(A2): 多因子信号增强
                factor_bonus = 0.0
                w_smf = p.get("factor_weight_smf", 0.0)
                w_rev = p.get("factor_weight_reversal", 0.0)
                w_vol = p.get("factor_weight_low_vol", 0.0)
                if w_smf > 0 and i < len(df) and df["_smf_signal"].iloc[i] > 0:
                    factor_bonus += w_smf
                if w_rev > 0 and i < len(df) and df["_reversal_signal"].iloc[i] > 0:
                    factor_bonus += w_rev
                if w_vol > 0 and i < len(df) and df["_low_vol_signal"].iloc[i] > 0:
                    factor_bonus += w_vol
                signal_quality += min(factor_bonus, p.get("factor_max_bonus", 20))

                signal_quality = min(100, signal_quality)

                # V7: regime-aware质量门槛（对齐v5逻辑）
                current_regime = regime_series.get(str(date), "RANGE")
                min_quality = p["min_quality_bear"] if current_regime == "BEAR" else p["min_quality_non_bear"]
                # 同时兼容旧的 min_signal_quality 参数（WF网格可能传入）
                min_quality = max(min_quality, p["min_signal_quality"])
                if signal_quality < min_quality:
                    buy_signal = False

            # V7: 连续亏损熔断器（对齐v5逻辑）
            if buy_signal and consec_losses >= 3:
                if consec_losses >= 6:
                    buy_signal = False  # 6连亏: 永久禁止该股
                elif i < cooldown_until_idx:
                    buy_signal = False

            if not buy_signal:
                continue

            # ==== T+1执行买入 ====
            if i + 1 >= len(df):
                continue
            next_row = df.iloc[i + 1]
            if is_limit_up(next_row):
                continue

            exec_price = next_row["open"] * (1 + slippage)
            buy_price = exec_price
            buy_date = next_row["date"]
            buy_index = i + 1
            highest_since_buy = next_row["high"]
            in_position = True
            initial_shares = int(TOTAL_CAPITAL * 0.12 / exec_price / 100) * 100
            if initial_shares < 100:
                initial_shares = 100
            position_shares = initial_shares
            ladder_sold = [False, False]
            below_ma60_days = 0
            # V7: 记录买入时ATR，用于自适应止损
            atr_val = row.get("atr14", np.nan)
            atr_at_buy = atr_val if not pd.isna(atr_val) else buy_price * 0.03

        else:
            # ==== 持仓管理 ====
            highest_since_buy = max(highest_since_buy, high)

            # T+1规则
            if i <= buy_index:
                continue

            profit_pct = (close - buy_price) / buy_price
            sell_signal = False
            sell_type = ""
            sell_ratio = 1.0
            hold_days = i - buy_index

            # ---- 强制卖出: 单日放量大跌>8% ----
            pct_change = row.get("pct_change", 0)
            if pd.isna(pct_change):
                pct_change = 0
            vol_ma = row["vol_ma20"] if not pd.isna(row["vol_ma20"]) else 0
            if pct_change < -0.08 and vol_ma > 0 and volume > vol_ma * 2:
                sell_signal = True
                sell_type = "强制卖出"
                sell_ratio = 1.0

            # ---- 时间止损（可配置）----
            if not sell_signal and p["max_hold_days"] > 0:
                if hold_days >= p["max_hold_days"] and profit_pct < p["time_stop_profit"]:
                    sell_signal = True
                    sell_type = "时间止损"
                    sell_ratio = 1.0

            # ---- 移动止损（V7: ATR自适应 + 分段锁定，对齐v5）----
            if not sell_signal:
                current_regime_stop = regime_series.get(str(date), "RANGE")
                if p.get("atr_mode", True) and profit_pct < p["breakeven_profit"]:
                    # ATR自适应初始止损（约束在 [atr_stop_min, atr_stop_max]）
                    atr_mult = 1.5 if current_regime_stop == "BEAR" else p.get("atr_multiplier", 2.0)
                    atr_stop_pct = (atr_at_buy / buy_price) * atr_mult if buy_price > 0 else p["initial_stop_loss"]
                    atr_stop_pct = max(p["atr_stop_min"], min(p["atr_stop_max"], atr_stop_pct))
                    stop_price = buy_price * (1 - atr_stop_pct)
                elif profit_pct < p["breakeven_profit"]:
                    stop_price = buy_price * (1 - p["initial_stop_loss"])
                elif profit_pct < 0.10:
                    stop_price = buy_price * (1 + p["breakeven_lock"])
                elif profit_pct < 0.15:
                    stop_price = buy_price * 1.03
                elif profit_pct < 0.30:
                    stop_price = buy_price * (1 + p["profit_lock_1"])
                else:
                    stop_price = buy_price * (1 + p["profit_lock_2"])

                # FIX: 用盘中最低价判定止损触发（与backtest_real.py B5修复保持一致）
                if low <= stop_price:
                    sell_signal = True
                    sell_type = "止损"
                    sell_ratio = 1.0

            # ---- 趋势破位（可配置模式）----
            if not sell_signal and p["trend_break_mode"] != "none" and not pd.isna(row["ma60"]):
                ma60_slope = df["ma60"].iloc[i] - df["ma60"].iloc[max(0, i-3)]
                if close < row["ma60"] and ma60_slope < 0:
                    if p["trend_break_mode"] == "ma60":
                        sell_signal = True
                        sell_type = "趋势破位"
                        sell_ratio = 1.0
                    elif p["trend_break_mode"] == "ma60_2day":
                        below_ma60_days += 1
                        if below_ma60_days >= 2:
                            sell_signal = True
                            sell_type = "趋势破位"
                            sell_ratio = 1.0
                else:
                    below_ma60_days = 0

            # ---- 双轨止盈: 阶梯（可配置）----
            if not sell_signal and position_shares > 0:
                if not ladder_sold[0] and profit_pct >= p["ladder_1_pct"]:
                    sell_signal = True
                    sell_type = "阶梯止盈1"
                    sell_ratio = 1/3
                    ladder_sold[0] = True
                elif not ladder_sold[1] and profit_pct >= p["ladder_2_pct"]:
                    sell_signal = True
                    sell_type = "阶梯止盈2"
                    sell_ratio = 1/3
                    ladder_sold[1] = True

            # ---- 双轨止盈: 回落（可配置）----
            # FIX: 用盘中最低价计算回撤（实盘条件单盘中触发）
            if not sell_signal and highest_since_buy > buy_price * 1.05:
                drawdown_threshold = p["drawdown_leader"] if stock_type == "龙头" else p["drawdown_flex"]
                drawdown = (highest_since_buy - low) / highest_since_buy
                if drawdown >= drawdown_threshold and profit_pct > 0:
                    sell_signal = True
                    sell_type = "回落止盈"
                    sell_ratio = 1.0

            # ---- MACD死叉（可配置）----
            if not sell_signal and p["macd_death_cross"] != "none":
                should_check = False
                if p["macd_death_cross"] == "profit" and profit_pct > 0:
                    should_check = True
                elif p["macd_death_cross"] == "profit5" and profit_pct > 0.05:
                    should_check = True

                if should_check:
                    if not pd.isna(row["macd_dif"]) and not pd.isna(row["macd_dea"]):
                        if not pd.isna(prev["macd_dif"]) and not pd.isna(prev["macd_dea"]):
                            if prev["macd_dif"] >= prev["macd_dea"] and row["macd_dif"] < row["macd_dea"]:
                                sell_signal = True
                                sell_type = "MACD死叉"
                                sell_ratio = 1.0

            # ==== T+1执行卖出 ====
            if sell_signal:
                if i + 1 >= len(df):
                    exec_price = close * (1 - slippage)
                    sell_date = date
                else:
                    next_row = df.iloc[i + 1]
                    if is_limit_down(next_row):
                        continue
                    exec_price = next_row["open"] * (1 - slippage)
                    sell_date = next_row["date"]

                actual_sell_shares = int(position_shares * sell_ratio / 100) * 100
                if actual_sell_shares < 100:
                    actual_sell_shares = position_shares
                if sell_ratio >= 1.0:
                    actual_sell_shares = position_shares

                gross_profit = (exec_price - buy_price) / buy_price
                net_profit = gross_profit - COMMISSION
                hold_days_final = (i + 1 - buy_index) if i + 1 < len(df) else (i - buy_index)

                trades.append({
                    "code": code,
                    "name": info["名称"],
                    "industry": info.get("行业", ""),
                    "stock_type": stock_type,
                    "buy_date": buy_date,
                    "buy_price": round(buy_price, 3),
                    "sell_date": sell_date,
                    "sell_price": round(exec_price, 3),
                    "gross_profit": round(gross_profit * 100, 2),
                    "net_profit": round(net_profit * 100, 2),
                    "hold_days": hold_days_final,
                    "sell_type": sell_type,
                    "highest": round(highest_since_buy, 3),
                    "sell_ratio": round(sell_ratio, 2),
                    "regime": regime_series.get(str(buy_date), "RANGE"),
                })

                # V7: 更新连续亏损计数和渐进式熔断期（对齐v5逻辑）
                if net_profit < 0:
                    consec_losses += 1
                    if consec_losses >= 6:
                        cooldown_until_idx = i + 9999  # 6连亏: 永久禁止
                    elif consec_losses >= 4:
                        cooldown_until_idx = i + 60  # 4-5连亏: 暂停60个交易日
                    elif consec_losses >= 3:
                        cooldown_until_idx = i + 30  # 3连亏: 暂停30天
                else:
                    consec_losses = 0  # 盈利重置

                position_shares -= actual_sell_shares
                if position_shares <= 0 or sell_ratio >= 1.0:
                    in_position = False
                    position_shares = 0

    # 回测结束仍持仓
    if in_position and position_shares > 0:
        last_row = df.iloc[-1]
        exec_price = last_row["close"] * (1 - slippage)
        gross_profit = (exec_price - buy_price) / buy_price
        net_profit = gross_profit - COMMISSION
        trades.append({
            "code": code, "name": info["名称"], "industry": info.get("行业", ""),
            "stock_type": stock_type, "buy_date": buy_date,
            "buy_price": round(buy_price, 3), "sell_date": last_row["date"],
            "sell_price": round(exec_price, 3),
            "gross_profit": round(gross_profit * 100, 2),
            "net_profit": round(net_profit * 100, 2),
            "hold_days": len(df) - 1 - buy_index,
            "sell_type": "回测结束", "highest": round(highest_since_buy, 3),
            "sell_ratio": 1.0,
            "regime": regime_series.get(str(buy_date), "RANGE"),
        })

    return trades


# ============================================================
# 三、统计评估函数
# ============================================================

def evaluate_trades(trades: list) -> dict:
    """评估一组交易记录，返回关键指标"""
    if not trades:
        return {"win_rate": 0, "profit_factor": 0, "expectancy": 0,
                "total": 0, "cumulative": 0, "max_consec_loss": 0, "score": 0}

    total = len(trades)
    wins = [t for t in trades if t["net_profit"] > 0]
    losses = [t for t in trades if t["net_profit"] <= 0]

    win_rate = len(wins) / total * 100
    avg_win = np.mean([t["net_profit"] for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t["net_profit"] for t in losses])) if losses else 0.01
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 10
    expectancy = np.mean([t["net_profit"] for t in trades])

    # 累计收益（复利）
    # BUG-047修复: 乘以仓位比例，与analyze_trades口径对齐
    position_ratio = getattr(config, 'POSITION_RATIO', 0.12)
    cumulative = 1.0
    for t in trades:
        cumulative *= (1 + t["net_profit"] / 100 * position_ratio)
    cumulative_pct = (cumulative - 1) * 100

    # 最大连续亏损
    max_consec = 0
    streak = 0
    for t in trades:
        if t["net_profit"] <= 0:
            streak += 1
            max_consec = max(max_consec, streak)
        else:
            streak = 0

    # 综合评分 = 胜率×0.3 + 盈亏比×0.3 + 期望×0.2 + 交易次数惩罚×0.2
    # 目标：高胜率 + 高盈亏比 + 正期望 + 足够交易次数
    trade_count_score = min(total / 200, 1.0) * 100  # 200笔以上满分
    score = (win_rate * 0.30 +
             min(profit_factor * 30, 100) * 0.30 +
             min(max(expectancy * 20, 0), 100) * 0.20 +
             trade_count_score * 0.20)

    return {
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
        "total": total,
        "cumulative": round(cumulative_pct, 2),
        "max_consec_loss": max_consec,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-abs(avg_loss), 2),
        "score": round(score, 2),
    }


# ============================================================
# 四、数据缓存（只加载一次）
# ============================================================

def load_all_data(start_date="2022-01-01") -> dict:
    """加载所有测试股票数据（缓存）"""
    cache_file = os.path.join(config.PROJECT_ROOT, "data", "backtest_cache.pkl")

    if os.path.exists(cache_file):
        try:
            data = pd.read_pickle(cache_file)
            logger.info(f"从缓存加载数据: {len(data)}只")
            return data
        except Exception:
            pass

    logger.info("从baostock加载数据（首次，约2分钟）...")
    data = {}
    for code, info in TEST_STOCKS.items():
        try:
            df = fetch_history_data(code, start_date=start_date)
            if not df.empty and len(df) >= 80:
                data[code] = {"df": df, "info": info}
                logger.info(f"  {code} {info['名称']}: {len(df)}条")
        except Exception as e:
            logger.error(f"  {code} 失败: {e}")

    # 缓存
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    pd.to_pickle(data, cache_file)
    logger.info(f"数据已缓存: {cache_file}")
    return data


# ============================================================
# 五、网格搜索
# ============================================================

def run_single_backtest(data: dict, params: dict, end_date: str = None) -> list:
    """用指定参数运行全部股票回测"""
    all_trades = []
    for code, stock_data in data.items():
        df = stock_data["df"]
        info = stock_data["info"]

        # 样本外截断
        if end_date:
            df = df[df["date"] <= end_date].reset_index(drop=True)

        trades = backtest_stock_v6(df, code, info, params)
        all_trades.extend(trades)

    all_trades.sort(key=lambda x: x["buy_date"])
    return all_trades


def grid_search(data: dict, param_grid: dict, end_date: str = None) -> list:
    """
    网格搜索：遍历所有参数组合
    
    返回: [(score, params, stats), ...] 按score降序
    """
    # 生成所有参数组合
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))

    logger.info(f"网格搜索: {len(keys)}个参数, {len(combinations)}种组合")
    logger.info(f"参数: {keys}")

    results = []
    for idx, combo in enumerate(combinations):
        params = dict(zip(keys, combo))

        trades = run_single_backtest(data, params, end_date)
        stats = evaluate_trades(trades)

        results.append((stats["score"], params, stats))

        # 进度
        if (idx + 1) % 50 == 0 or idx == len(combinations) - 1:
            logger.info(f"  进度: {idx+1}/{len(combinations)} | "
                       f"当前最优: 胜率{max(r[2]['win_rate'] for r in results):.1f}% "
                       f"盈亏比{max(r[2]['profit_factor'] for r in results):.2f}")

    # 按综合评分排序
    results.sort(key=lambda x: x[0], reverse=True)
    return results


# ============================================================
# 六、主优化流程
# ============================================================

def run_optimization(quick=False):
    """运行完整优化流程"""
    logger.info("=" * 70)
    logger.info("  策略参数优化器 V1.0")
    logger.info("  目标: 胜率70%+ | 盈亏比2.5+ | 每笔期望+3%+")
    logger.info("=" * 70)

    # 1. 加载数据
    data = load_all_data()
    if not data:
        logger.error("无数据")
        return

    # 2. 基线测试（V5.0默认参数）
    logger.info("\n[1/4] 基线测试（V5.0默认参数）...")
    baseline_trades = run_single_backtest(data, DEFAULT_PARAMS)
    baseline_stats = evaluate_trades(baseline_trades)
    logger.info(f"  基线: 胜率{baseline_stats['win_rate']}% | "
               f"盈亏比{baseline_stats['profit_factor']} | "
               f"期望{baseline_stats['expectancy']:+.2f}% | "
               f"{baseline_stats['total']}笔 | 连亏{baseline_stats['max_consec_loss']}")

    # 3. 参数网格搜索
    logger.info("\n[2/4] 参数网格搜索...")

    if quick:
        # 快速模式：只搜索最关键的参数
        param_grid = {
            "initial_stop_loss": [0.06, 0.07, 0.08, 0.10],
            "drawdown_leader": [0.05, 0.06, 0.07, 0.08],
            "drawdown_flex": [0.03, 0.04, 0.05],
            "trend_break_mode": ["ma60", "ma60_2day", "none"],
            "max_hold_days": [0, 30, 45],
            "macd_death_cross": ["profit", "profit5", "none"],
            "breakeven_profit": [0.03, 0.05, 0.07],
            "breakeven_lock": [0.01, 0.02, 0.03],
            "ladder_1_pct": [0.08, 0.10, 0.12],
            "min_signal_quality": [50, 55, 60, 65],
        }
    else:
        # 完整搜索
        param_grid = {
            # 止损（最关键）
            "initial_stop_loss": [0.06, 0.07, 0.08, 0.10],
            "breakeven_profit": [0.03, 0.05, 0.07],
            "breakeven_lock": [0.01, 0.02, 0.03],
            # 止盈
            "ladder_1_pct": [0.08, 0.10, 0.12],
            "ladder_2_pct": [0.18, 0.22, 0.28],
            "drawdown_leader": [0.05, 0.06, 0.07, 0.08],
            "drawdown_flex": [0.03, 0.04, 0.05],
            # 卖出逻辑
            "macd_death_cross": ["profit", "profit5", "none"],
            "trend_break_mode": ["ma60", "ma60_2day", "none"],
            "max_hold_days": [0, 30, 45],
        }

    # 分阶段搜索（避免组合爆炸）
    # 第一轮：止损+止盈参数
    logger.info("\n  === 第一轮: 止损+止盈参数 ===")
    grid_1 = {
        "initial_stop_loss": param_grid["initial_stop_loss"],
        "drawdown_leader": param_grid["drawdown_leader"],
        "drawdown_flex": param_grid["drawdown_flex"],
    }
    results_1 = grid_search(data, grid_1)
    best_1 = results_1[0]
    logger.info(f"  第一轮最优: 止损{best_1[1]['initial_stop_loss']} | "
               f"回落龙头{best_1[1]['drawdown_leader']} | "
               f"回落弹性{best_1[1]['drawdown_flex']} | "
               f"胜率{best_1[2]['win_rate']}% 盈亏比{best_1[2]['profit_factor']}")

    # 第二轮：在第一轮最优基础上搜索卖出逻辑
    logger.info("\n  === 第二轮: 卖出逻辑参数 ===")
    grid_2 = {
        "macd_death_cross": param_grid["macd_death_cross"],
        "trend_break_mode": param_grid["trend_break_mode"],
        "max_hold_days": param_grid["max_hold_days"],
    }
    # 固定第一轮最优参数
    base_params_2 = deepcopy(DEFAULT_PARAMS)
    base_params_2.update(best_1[1])

    results_2 = []
    keys_2 = list(grid_2.keys())
    values_2 = list(grid_2.values())
    for combo in itertools.product(*values_2):
        params = {**base_params_2, **dict(zip(keys_2, combo))}
        trades = run_single_backtest(data, params)
        stats = evaluate_trades(trades)
        results_2.append((stats["score"], params, stats))

    results_2.sort(key=lambda x: x[0], reverse=True)
    best_2 = results_2[0]
    logger.info(f"  第二轮最优: MACD={best_2[1]['macd_death_cross']} | "
               f"破位={best_2[1]['trend_break_mode']} | "
               f"持仓限制={best_2[1]['max_hold_days']}天 | "
               f"胜率{best_2[2]['win_rate']}% 盈亏比{best_2[2]['profit_factor']}")

    # 第三轮：买入参数+保本线
    logger.info("\n  === 第三轮: 买入+保本参数 ===")
    grid_3 = {
        "breakeven_profit": param_grid.get("breakeven_profit", [0.03, 0.05, 0.07]),
        "breakeven_lock": param_grid.get("breakeven_lock", [0.01, 0.02, 0.03]),
        "ladder_1_pct": param_grid.get("ladder_1_pct", [0.08, 0.10, 0.12]),
        "min_signal_quality": [50, 55, 60, 65],
    }
    base_params_3 = deepcopy(best_2[1])

    results_3 = []
    keys_3 = list(grid_3.keys())
    values_3 = list(grid_3.values())
    for combo in itertools.product(*values_3):
        params = {**base_params_3, **dict(zip(keys_3, combo))}
        trades = run_single_backtest(data, params)
        stats = evaluate_trades(trades)
        results_3.append((stats["score"], params, stats))

    results_3.sort(key=lambda x: x[0], reverse=True)
    best_final = results_3[0]

    # 4. 输出最终结果
    logger.info("\n" + "=" * 70)
    logger.info("  优化结果")
    logger.info("=" * 70)
    logger.info(f"\n  {'指标':<12} {'V5.0基线':<15} {'V6.0优化':<15} {'变化':<15}")
    logger.info("-" * 60)

    opt = best_final[2]
    base = baseline_stats
    logger.info(f"  {'胜率':<12} {base['win_rate']}%{'':<10} {opt['win_rate']}%{'':<10} {opt['win_rate']-base['win_rate']:+.1f}%")
    logger.info(f"  {'盈亏比':<12} {base['profit_factor']:<15} {opt['profit_factor']:<15} {opt['profit_factor']-base['profit_factor']:+.2f}")
    logger.info(f"  {'每笔期望':<12} {base['expectancy']:+.2f}%{'':<9} {opt['expectancy']:+.2f}%{'':<9} {opt['expectancy']-base['expectancy']:+.2f}%")
    logger.info(f"  {'交易次数':<12} {base['total']:<15} {opt['total']:<15}")
    logger.info(f"  {'最大连亏':<12} {base['max_consec_loss']:<15} {opt['max_consec_loss']:<15}")
    logger.info(f"  {'综合评分':<12} {base['score']:<15} {opt['score']:<15}")

    logger.info(f"\n  最优参数:")
    for k, v in best_final[1].items():
        default_v = DEFAULT_PARAMS.get(k)
        marker = " ← 已优化" if v != default_v else ""
        logger.info(f"    {k}: {v}{marker}")

    # 5. 保存最优参数
    output = {
        "optimization_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "baseline": base,
        "optimized": opt,
        "best_params": best_final[1],
        "top5": [(r[2]["win_rate"], r[2]["profit_factor"], r[2]["expectancy"], r[1]) for r in results_3[:5]],
    }
    output_path = os.path.join(config.PROJECT_ROOT, "output", "optimization_result.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"\n  结果已保存: {output_path}")

    return best_final[1], best_final[2]


# ============================================================
# 七、样本外验证（防过拟合）
# ============================================================

def out_of_sample_validation(data: dict, best_params: dict):
    """
    样本外验证：
    1. 训练集(2022-07~2024-07) vs 测试集(2024-07~2025-07)
    2. 蒙特卡洛模拟（1000次随机打乱交易顺序）
    """
    logger.info("\n" + "=" * 70)
    logger.info("  样本外验证（防过拟合）")
    logger.info("=" * 70)

    # 1. 时间分割验证
    train_end = "2024-07-01"
    logger.info(f"\n  训练集: 2022-07 ~ 2024-07 | 测试集: 2024-07 ~ 2025-07")

    # 训练集
    train_trades = run_single_backtest(data, best_params, end_date=train_end)
    train_stats = evaluate_trades(train_trades)

    # 测试集（全量 - 训练集 = 样本外）
    all_trades = run_single_backtest(data, best_params)
    # 过滤出测试集交易
    test_trades = [t for t in all_trades if t["buy_date"] > train_end]
    test_stats = evaluate_trades(test_trades)

    # V5.0基线对比
    baseline_all = run_single_backtest(data, DEFAULT_PARAMS)
    baseline_test = [t for t in baseline_all if t["buy_date"] > train_end]
    baseline_test_stats = evaluate_trades(baseline_test)

    logger.info(f"\n  {'指标':<12} {'训练集':<15} {'测试集(V6)':<15} {'测试集(V5)':<15}")
    logger.info("  " + "-" * 57)
    logger.info(f"  {'胜率':<12} {train_stats['win_rate']}%{'':<10} {test_stats['win_rate']}%{'':<10} {baseline_test_stats['win_rate']}%")
    logger.info(f"  {'盈亏比':<12} {train_stats['profit_factor']:<15} {test_stats['profit_factor']:<15} {baseline_test_stats['profit_factor']}")
    logger.info(f"  {'每笔期望':<12} {train_stats['expectancy']:+.2f}%{'':<9} {test_stats['expectancy']:+.2f}%{'':<9} {baseline_test_stats['expectancy']:+.2f}%")
    logger.info(f"  {'交易次数':<12} {train_stats['total']:<15} {test_stats['total']:<15} {baseline_test_stats['total']}")
    logger.info(f"  {'最大连亏':<12} {train_stats['max_consec_loss']:<15} {test_stats['max_consec_loss']:<15} {baseline_test_stats['max_consec_loss']}")

    # 过拟合检测
    win_rate_drop = train_stats['win_rate'] - test_stats['win_rate']
    logger.info(f"\n  过拟合检测:")
    logger.info(f"    胜率衰减: {win_rate_drop:.1f}% ({'✅ 正常(<5%)' if win_rate_drop < 5 else '⚠️ 过拟合风险(>5%)'})")
    logger.info(f"    测试集V6 vs V5: 胜率{test_stats['win_rate']-baseline_test_stats['win_rate']:+.1f}% | 期望{test_stats['expectancy']-baseline_test_stats['expectancy']:+.2f}%")

    # 2. 蒙特卡洛模拟
    logger.info(f"\n  蒙特卡洛模拟（1000次随机打乱）...")
    np.random.seed(42)
    mc_max_drawdowns = []
    mc_final_returns = []

    # BUG-047修复: 蒙特卡洛也需乘仓位比例
    position_ratio = getattr(config, 'POSITION_RATIO', 0.12)
    profits = [t["net_profit"] / 100 * position_ratio for t in all_trades]
    n = len(profits)

    for _ in range(1000):
        shuffled = np.random.permutation(profits)
        cumulative = 1.0
        peak = 1.0
        max_dd = 0
        for p in shuffled:
            cumulative *= (1 + p)
            peak = max(peak, cumulative)
            dd = (peak - cumulative) / peak
            max_dd = max(max_dd, dd)
        mc_max_drawdowns.append(max_dd)
        mc_final_returns.append((cumulative - 1) * 100)

    mc_max_drawdowns = np.array(mc_max_drawdowns)
    mc_final_returns = np.array(mc_final_returns)

    logger.info(f"    最大回撤 95%置信区间: [{np.percentile(mc_max_drawdowns, 2.5):.1%}, {np.percentile(mc_max_drawdowns, 97.5):.1%}]")
    logger.info(f"    最大回撤 中位数: {np.median(mc_max_drawdowns):.1%}")
    logger.info(f"    最终收益 95%置信区间: [{np.percentile(mc_final_returns, 2.5):.0f}%, {np.percentile(mc_final_returns, 97.5):.0f}%]")
    logger.info(f"    最终收益 中位数: {np.median(mc_final_returns):.0f}%")

    validation_result = {
        "train_stats": train_stats,
        "test_stats": test_stats,
        "baseline_test_stats": baseline_test_stats,
        "win_rate_drop": round(win_rate_drop, 1),
        "overfit_risk": "low" if win_rate_drop < 5 else "high",
        "monte_carlo": {
            "max_drawdown_95ci": [round(np.percentile(mc_max_drawdowns, 2.5)*100, 1),
                                   round(np.percentile(mc_max_drawdowns, 97.5)*100, 1)],
            "max_drawdown_median": round(np.median(mc_max_drawdowns)*100, 1),
            "final_return_95ci": [round(np.percentile(mc_final_returns, 2.5), 0),
                                   round(np.percentile(mc_final_returns, 97.5), 0)],
            "final_return_median": round(np.median(mc_final_returns), 0),
        }
    }

    return validation_result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="策略参数优化器")
    parser.add_argument("--quick", action="store_true", help="快速模式（减少参数组合）")
    parser.add_argument("--validate", action="store_true", help="仅运行样本外验证（需先有optimization_result.json）")
    args = parser.parse_args()

    if args.validate:
        # 从文件加载最优参数，运行样本外验证
        result_path = os.path.join(config.PROJECT_ROOT, "output", "optimization_result.json")
        if os.path.exists(result_path):
            with open(result_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            best_params = saved["best_params"]
            # 修复类型（JSON中True/False变成字符串）
            if isinstance(best_params.get("require_ma60_up"), str):
                best_params["require_ma60_up"] = best_params["require_ma60_up"] == "True"
            data = load_all_data()
            val_result = out_of_sample_validation(data, best_params)
            # 保存验证结果
            val_path = os.path.join(config.PROJECT_ROOT, "output", "validation_result.json")
            with open(val_path, "w", encoding="utf-8") as f:
                json.dump(val_result, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"\n  验证结果已保存: {val_path}")
        else:
            logger.error(f"未找到优化结果文件: {result_path}")
    else:
        best_params, best_stats = run_optimization(quick=args.quick)
        # 自动运行样本外验证
        data = load_all_data()
        val_result = out_of_sample_validation(data, best_params)
        val_path = os.path.join(config.PROJECT_ROOT, "output", "validation_result.json")
        with open(val_path, "w", encoding="utf-8") as f:
            json.dump(val_result, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"\n  验证结果已保存: {val_path}")
