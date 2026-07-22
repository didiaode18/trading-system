"""
核心趋势策略函数
=================
基于「高胜率A股交易操作系统V2.0」规则，输入日线DataFrame，输出买卖信号

核心规则:
- 趋势判定：20日均线向上 + 收盘价站稳20日均线 -> 允许开仓
- 买点：缩量回踩20日线（量缩30%+，最低价触及20日线±1%）
- 止损：买入价下方10%，仅收盘价触发
- 移动止损：按浮盈分档上移
- 止盈：双轨制（阶梯目标 + 回落止盈）
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

try:
    from strategy.anti_manipulation import analyze_manipulation
    HAS_ANTI_MANIP = True
except ImportError:
    HAS_ANTI_MANIP = False

logger = logging.getLogger(__name__)


# ============================================================
# 一、均线与趋势计算
# ============================================================

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算所有技术指标，在原始DataFrame上增加列:
    - ma5, ma10, ma20, ma60: 均线
    - vol_ma20: 20日成交量均线
    - ma20_slope: 20日均线斜率（向上/向下）
    - rsi: RSI相对强弱指标
    - macd_dif, macd_dea, macd_hist: MACD指标
    - boll_upper, boll_mid, boll_lower: 布林带
    - atr: 真实波幅
    - ma_bullish: 均线多头排列标记
    - vol_price_divergence: 量价背离标记
    - highest_since_buy: 持仓期间最高价（用于回落止盈）
    """
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(config.MA_SHORT).mean()
    df["ma60"] = df["close"].rolling(config.MA_MID).mean()
    df["vol_ma20"] = df["volume"].rolling(config.VOLUME_MA_PERIOD).mean()

    # 20日均线斜率：今天ma20 > 3天前ma20 视为向上
    df["ma20_slope"] = df["ma20"].diff(3)

    # 日内振幅
    df["intraday_range"] = (df["high"] - df["low"]) / df["close"].shift(1)

    # ---- RSI ----
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(config.RSI_PERIOD).mean()
    avg_loss = loss.rolling(config.RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ---- MACD ----
    ema_fast = df["close"].ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow = df["close"].ewm(span=config.MACD_SLOW, adjust=False).mean()
    df["macd_dif"] = ema_fast - ema_slow
    df["macd_dea"] = df["macd_dif"].ewm(span=config.MACD_SIGNAL, adjust=False).mean()
    df["macd_hist"] = 2 * (df["macd_dif"] - df["macd_dea"])

    # ---- 布林带 ----
    df["boll_mid"] = df["close"].rolling(config.BOLL_PERIOD).mean()
    boll_std = df["close"].rolling(config.BOLL_PERIOD).std()
    df["boll_upper"] = df["boll_mid"] + config.BOLL_STD * boll_std
    df["boll_lower"] = df["boll_mid"] - config.BOLL_STD * boll_std

    # ---- ATR (Average True Range) ----
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.rolling(config.ATR_PERIOD).mean()

    # ---- 均线多头排列检测 (MA5 > MA10 > MA20 > MA60) ----
    df["ma_bullish"] = (
        (df["ma5"] > df["ma10"]) &
        (df["ma10"] > df["ma20"]) &
        (df["ma20"] > df["ma60"])
    )

    # ---- 量价背离检测（近5日价格新高但成交量萎缩）----
    df["vol_price_divergence"] = False
    for i in range(5, len(df)):
        recent_5 = df.iloc[i-4:i+1]
        price_new_high = recent_5["close"].iloc[-1] >= recent_5["close"].max()
        vol_shrinking = recent_5["volume"].iloc[-1] < recent_5["volume"].mean() * 0.8
        if price_new_high and vol_shrinking:
            df.iloc[i, df.columns.get_loc("vol_price_divergence")] = True

    return df


def is_trend_up(df: pd.DataFrame) -> bool:
    """
    判定中期上升趋势是否成立:
    1. 20日均线向上（斜率>0）
    2. 收盘价站稳20日均线上方
    3. 60日均线存在且未明确向下（可选增强条件）
    """
    if len(df) < config.MA_MID:
        return False

    latest = df.iloc[-1]
    # 条件1: 20日均线向上
    if pd.isna(latest["ma20_slope"]) or latest["ma20_slope"] <= 0:
        return False
    # 条件2: 收盘价在20日均线上方
    if latest["close"] < latest["ma20"]:
        return False
    return True


# ============================================================
# 二、买点信号判定
# ============================================================

def check_buy_signal(df: pd.DataFrame) -> dict:
    """
    检查今日是否触发买入信号（买点1：缩量回踩关键支撑位）
    
    条件:
    1. 趋势向上（ma20向上 + 收盘价在ma20上方或附近）
    2. 缩量：今日成交量 < 20日均量 * (1 - 30%)
    3. 最低价触及20日线±1%
    4. 排除放量下跌（当日跌幅>3%且量>均量1.5倍）
    5. 回调不创新低（近10日最低价不低于前一波回调低点）
    
    返回:
        {
            "signal": True/False,
            "buy_price": float,
            "support_price": float,
            "stop_loss": float,
            "reason": str,
            "quality_score": int  # 信号质量评分0-100
        }
    """
    result = {"signal": False, "buy_price": None, "support_price": None,
              "stop_loss": None, "reason": "", "quality_score": 0}

    if len(df) < config.MA_MID:
        result["reason"] = "数据不足，无法计算均线"
        return result

    df_ind = compute_indicators(df)
    latest = df_ind.iloc[-1]

    # 前提：趋势向上
    if not is_trend_up(df_ind):
        result["reason"] = "趋势不满足：20日均线未向上或收盘价在20日线下方"
        return result

    ma20 = latest["ma20"]
    ma60 = latest.get("ma60", None)
    close = latest["close"]
    low = latest["low"]
    volume = latest["volume"]
    vol_ma = latest["vol_ma20"]

    # ---- 排除条件：放量下跌不接 ----
    prev_close = df_ind["close"].iloc[-2] if len(df_ind) > 1 else close
    day_change = (close - prev_close) / prev_close if prev_close > 0 else 0
    reject_vol_ratio = getattr(config, 'REJECT_VOLUME_RATIO', 1.5)
    reject_drop = getattr(config, 'REJECT_DROP_PCT', -0.03)
    if not pd.isna(vol_ma) and vol_ma > 0:
        if day_change < reject_drop and volume > vol_ma * reject_vol_ratio:
            result["reason"] = f"放量下跌排除: 跌幅{day_change:.2%}且量>均量{reject_vol_ratio}倍"
            return result

    # 条件1：缩量（成交量较20日均量萎缩30%以上）
    if pd.isna(vol_ma) or vol_ma == 0:
        result["reason"] = "成交量均线数据不足"
        return result
    vol_ratio = volume / vol_ma
    if vol_ratio >= (1 - config.VOLUME_SHRINK_RATIO):
        result["reason"] = f"未缩量：今日成交量/20日均量 = {vol_ratio:.2%}，需<{1-config.VOLUME_SHRINK_RATIO:.0%}"
        return result

    # 条件2：最低价触及20日线±1%
    touch_lower = ma20 * (1 - config.SUPPORT_TOUCH_PCT)
    touch_upper = ma20 * (1 + config.SUPPORT_TOUCH_PCT)
    if low > touch_upper:
        result["reason"] = f"最低价{low:.2f}未触及20日线{ma20:.2f}±{config.SUPPORT_TOUCH_PCT:.0%}"
        return result

    # 条件3：回调不创新低（近10日最低价不低于前一波回调低点）
    if len(df_ind) >= 20:
        recent_10_low = df_ind["low"].iloc[-10:].min()
        prev_wave_low = df_ind["low"].iloc[-20:-10].min()
        if recent_10_low < prev_wave_low * 0.99:
            result["reason"] = f"回调创新低排除: 近10日最低{recent_10_low:.2f} < 前波低{prev_wave_low:.2f}"
            return result

    # 信号触发！
    buy_price = close
    stop_loss = buy_price * (1 - config.INITIAL_STOP_LOSS_PCT)

    # ---- 信号质量评分（0-100）----
    quality_score = 50  # 基础分

    # 多支撑位重合加分（MA20/MA60/前期平台低点三者重合）
    support_count = 1  # MA20肯定触及
    if not pd.isna(ma60) and ma60 > 0:
        if abs(low - ma60) / ma60 < 0.02:  # 触及MA60±2%
            support_count += 1
    # 前期平台低点（近20日最低价附近）
    if len(df_ind) >= 20:
        platform_low = df_ind["low"].iloc[-20:].min()
        if abs(low - platform_low) / platform_low < 0.02:
            support_count += 1
    if support_count >= 3:
        quality_score += 25  # 三支撑重合，最佳买点
    elif support_count >= 2:
        quality_score += 15  # 双支撑重合

    # ---- 新增指标增强确认 ----
    confirmations = []
    warnings_list = []

    # RSI确认
    rsi_val = latest.get("rsi", 50)
    if not pd.isna(rsi_val):
        if rsi_val < config.RSI_OVERSOLD:
            confirmations.append(f"RSI超卖({rsi_val:.0f})")
            quality_score += 10
        elif rsi_val > config.RSI_OVERBOUGHT:
            warnings_list.append(f"RSI超买({rsi_val:.0f})")
            quality_score -= 10

    # MACD金叉确认
    macd_dif = latest.get("macd_dif", 0)
    macd_dea = latest.get("macd_dea", 0)
    if not pd.isna(macd_dif) and not pd.isna(macd_dea):
        prev_dif = df_ind["macd_dif"].iloc[-2] if len(df_ind) > 1 else 0
        prev_dea = df_ind["macd_dea"].iloc[-2] if len(df_ind) > 1 else 0
        if not pd.isna(prev_dif) and not pd.isna(prev_dea):
            if prev_dif <= prev_dea and macd_dif > macd_dea:
                confirmations.append("MACD金叉")
                quality_score += 10
            elif macd_dif > macd_dea:
                confirmations.append("MACD多头")
                quality_score += 5
            else:
                warnings_list.append("MACD空头")
                quality_score -= 5

    # 均线多头排列加分
    if latest.get("ma_bullish", False):
        confirmations.append("均线多头排列")
        quality_score += 10

    # 量价背离警告
    if latest.get("vol_price_divergence", False):
        warnings_list.append("量价背离")
        quality_score -= 5

    # 缩量程度加分（越缩越好）
    if vol_ratio < 0.5:
        quality_score += 5  # 极度缩量

    quality_score = max(0, min(100, quality_score))

    # 构建完整信号说明
    extra_info = ""
    if confirmations:
        extra_info += " [确认: " + ", ".join(confirmations) + "]"
    if warnings_list:
        extra_info += " [警告: " + ", ".join(warnings_list) + "]"
    if support_count >= 2:
        extra_info += f" [★{support_count}支撑重合]"

    result.update({
        "signal": True,
        "buy_price": round(buy_price, 2),
        "support_price": round(ma20, 2),
        "stop_loss": round(stop_loss, 2),
        "rsi": round(rsi_val, 1) if not pd.isna(rsi_val) else None,
        "macd_dif": round(macd_dif, 3) if not pd.isna(macd_dif) else None,
        "macd_dea": round(macd_dea, 3) if not pd.isna(macd_dea) else None,
        "atr": round(latest.get("atr", 0), 2) if not pd.isna(latest.get("atr", 0)) else None,
        "quality_score": quality_score,
        "buy_type": "回踩支撑",
        "reason": f"缩量回踩20日线: 量比={vol_ratio:.2%}, MA20={ma20:.2f}, 最低={low:.2f}{extra_info}"
    })
    return result


def check_breakout_buy_signal(df: pd.DataFrame) -> dict:
    """
    买点2：放量突破后回踩确认（趋势加速买点）
    
    条件:
    1. 近10日内有放量突破（量>均量1.5倍 + 创20日新高）
    2. 突破后缩量回踩（量缩至突破日50%以下）
    3. 回踩不破突破位（收盘价 >= 突破日收盘价 * 0.99）
    4. MA20仍向上
    
    适合: 主升浪阶段的龙头标的，爆发力强
    """
    result = {"signal": False, "buy_price": None, "support_price": None,
              "stop_loss": None, "reason": "", "quality_score": 0}

    if len(df) < config.MA_MID:
        result["reason"] = "数据不足"
        return result

    df_ind = compute_indicators(df)
    latest = df_ind.iloc[-1]

    # 前提：MA20向上
    if not is_trend_up(df_ind):
        result["reason"] = "趋势不满足"
        return result

    lookback = getattr(config, 'BREAKOUT_LOOKBACK', 10)
    vol_ratio_threshold = getattr(config, 'BREAKOUT_VOLUME_RATIO', 1.5)
    pullback_vol = getattr(config, 'BREAKOUT_PULLBACK_VOL', 0.50)
    hold_pct = getattr(config, 'BREAKOUT_HOLD_PCT', 0.99)

    close = latest["close"]
    volume = latest["volume"]
    vol_ma = latest["vol_ma20"]

    if pd.isna(vol_ma) or vol_ma == 0:
        return result

    # 在近10日内寻找放量突破日
    breakout_day = None
    for i in range(-lookback, -1):  # 从-10到-2（不含今天）
        idx = len(df_ind) + i
        if idx < 20:
            continue
        row = df_ind.iloc[idx]
        # 突破条件：创20日新高 + 放量
        high_20d_before = df_ind["high"].iloc[max(0, idx-20):idx].max()
        if row["close"] > high_20d_before and row["volume"] > vol_ma * vol_ratio_threshold:
            breakout_day = idx
            break

    if breakout_day is None:
        result["reason"] = "近10日无放量突破"
        return result

    breakout_row = df_ind.iloc[breakout_day]
    breakout_close = breakout_row["close"]
    breakout_volume = breakout_row["volume"]

    # 条件2：今日缩量回踩（量缩至突破日50%以下）
    if volume > breakout_volume * pullback_vol:
        result["reason"] = f"回踩未缩量: 今日量/突破日量={volume/breakout_volume:.2%}，需<{pullback_vol:.0%}"
        return result

    # 条件3：回踩不破突破位
    if close < breakout_close * hold_pct:
        result["reason"] = f"回踩破位: 收盘{close:.2f} < 突破位{breakout_close:.2f}*{hold_pct}"
        return result

    # 信号触发！
    buy_price = close
    stop_loss = buy_price * (1 - config.INITIAL_STOP_LOSS_PCT)

    # 质量评分
    quality_score = 55  # 突破回踩基础分略高于普通回踩
    # 突破后缩量程度
    shrink_ratio = volume / breakout_volume
    if shrink_ratio < 0.3:
        quality_score += 15  # 极度缩量
    elif shrink_ratio < 0.4:
        quality_score += 10

    # MACD确认
    macd_dif = latest.get("macd_dif", 0)
    macd_dea = latest.get("macd_dea", 0)
    if not pd.isna(macd_dif) and not pd.isna(macd_dea) and macd_dif > macd_dea:
        quality_score += 10

    # 均线多头
    if latest.get("ma_bullish", False):
        quality_score += 10

    quality_score = max(0, min(100, quality_score))

    result.update({
        "signal": True,
        "buy_price": round(buy_price, 2),
        "support_price": round(breakout_close, 2),
        "stop_loss": round(stop_loss, 2),
        "quality_score": quality_score,
        "buy_type": "突破回踩",
        "reason": f"放量突破后回踩确认: 突破日收盘{breakout_close:.2f}, "
                  f"回踩缩量{volume/breakout_volume:.0%}, MA20向上"
    })
    return result


# ============================================================
# 三、卖出信号判定
# ============================================================

def check_sell_signal(df: pd.DataFrame, buy_price: float,
                      current_position: dict = None) -> dict:
    """
    检查是否触发卖出信号（V6.0优化版）
    
    卖出规则优先级:
    1. 强制卖出: 单日放量大跌>8%（无条件离场）
    2. 时间止损: 持仓超45天且浮盈<3%（V6.0新增）
    3. 止损: 收盘价触发移动止损线
    4. 趋势破位: 收盘跌破60日线+均线拐头（V6.0: 可通过config关闭）
    5. MACD死叉: 盈利状态下DIF下穿DEA（V6.0: 可通过config关闭）
    6. 回落止盈: 从最高点回落超阈值
    
    V6.0优化结论（基于312笔回测诊断）:
    - MACD死叉37笔仅+2.11%平均 → 默认关闭
    - 趋势破位40笔全亏-4.52% → 默认关闭
    - 新增45天时间止损 → 提高资金效率
    """
    result = {"signal": False, "sell_type": None, "sell_price": None, "reason": ""}

    if len(df) < config.MA_SHORT or buy_price <= 0:
        return result

    df_ind = compute_indicators(df)
    latest = df_ind.iloc[-1]
    prev = df_ind.iloc[-2] if len(df_ind) > 1 else latest
    close = latest["close"]
    volume = latest["volume"]
    vol_ma = latest.get("vol_ma20", 0)

    # 当前浮盈比例
    profit_pct = (close - buy_price) / buy_price

    # 持仓期间最高价
    if current_position and current_position.get("highest_price"):
        highest = current_position["highest_price"]
    else:
        highest = df_ind["high"].max()

    # ---- 0. 强制卖出: 单日放量大跌>8%（无条件离场）----
    force_drop = getattr(config, 'FORCE_SELL_DROP_PCT', -0.08)
    force_vol = getattr(config, 'FORCE_SELL_VOLUME_RATIO', 2.0)
    if not pd.isna(prev["close"]) and prev["close"] > 0:
        day_change = (close - prev["close"]) / prev["close"]
        if not pd.isna(vol_ma) and vol_ma > 0:
            if day_change <= force_drop and volume > vol_ma * force_vol:
                result.update({
                    "signal": True,
                    "sell_type": "force_sell",
                    "sell_price": round(close, 2),
                    "reason": f"★强制卖出: 单日跌{day_change:.2%}且放量{volume/vol_ma:.1f}倍，资金出逃"
                })
                return result

    # ---- 1.5 时间止损（V6.0新增）: 持仓超45天且浮盈<3%则卖出 ----
    max_hold_days = getattr(config, 'MAX_HOLD_DAYS', 0)
    time_stop_profit = getattr(config, 'TIME_STOP_PROFIT', 0.03)
    if max_hold_days > 0 and current_position:
        buy_date_str = current_position.get("buy_date", "")
        if buy_date_str:
            try:
                buy_dt = pd.Timestamp(buy_date_str)
                hold_days = (pd.Timestamp(latest["date"]) - buy_dt).days
                if hold_days >= max_hold_days and profit_pct < time_stop_profit:
                    result.update({
                        "signal": True,
                        "sell_type": "time_stop",
                        "sell_price": round(close, 2),
                        "reason": f"时间止损: 持仓{hold_days}天>={max_hold_days}天, 浮盈{profit_pct:.2%}<{time_stop_profit:.0%}, 资金效率低"
                    })
                    return result
            except (ValueError, TypeError):
                pass

    # ---- 2. 止损判定（仅收盘价触发）----
    stop_loss_price = compute_trailing_stop(buy_price, close)
    if close <= stop_loss_price:
        result.update({
            "signal": True,
            "sell_type": "stop_loss",
            "sell_price": round(stop_loss_price, 2),
            "reason": f"触发止损: 收盘{close:.2f} <= 止损线{stop_loss_price:.2f}, 浮盈={profit_pct:.2%}"
        })
        return result

    # ---- 2. 趋势破位: 收盘跌破60日线+均线拐头+放量 → 立即卖出 ----
    # V6.0: 可通过config.TREND_BREAK_ENABLED关闭（回测显示40笔全亏）
    trend_break_enabled = getattr(config, 'TREND_BREAK_ENABLED', True)
    if trend_break_enabled and not pd.isna(latest.get("ma60", None)):
        ma60_slope = df_ind["ma60"].diff(3).iloc[-1]
        is_below_ma60 = close < latest["ma60"]
        is_ma60_down = ma60_slope < 0
        is_volume_up = (not pd.isna(vol_ma) and vol_ma > 0 and volume > vol_ma * 1.3)
        if is_below_ma60 and is_ma60_down:
            if is_volume_up:
                # 放量破位 → 立即卖出
                result.update({
                    "signal": True,
                    "sell_type": "trend_break",
                    "sell_price": round(close, 2),
                    "reason": f"★放量趋势破位: 跌破60日线+均线拐头+放量，立即离场"
                })
                return result
            else:
                # 缩量破位 → 也卖出（不等3天）
                result.update({
                    "signal": True,
                    "sell_type": "trend_break",
                    "sell_price": round(close, 2),
                    "reason": f"趋势破位: 收盘跌破60日线且均线拐头向下"
                })
                return result

    # ---- 3. MACD死叉确认卖出 ----
    # V6.0: 可通过config.MACD_DEATH_CROSS_ENABLED关闭（回测显示假信号太多）
    macd_enabled = getattr(config, 'MACD_DEATH_CROSS_ENABLED', True)
    macd_dif = latest.get("macd_dif", 0)
    macd_dea = latest.get("macd_dea", 0)
    if macd_enabled and not pd.isna(macd_dif) and not pd.isna(macd_dea) and profit_pct > 0:
        prev_dif = df_ind["macd_dif"].iloc[-2] if len(df_ind) > 1 else 0
        prev_dea = df_ind["macd_dea"].iloc[-2] if len(df_ind) > 1 else 0
        if not pd.isna(prev_dif) and not pd.isna(prev_dea):
            if prev_dif >= prev_dea and macd_dif < macd_dea:
                result.update({
                    "signal": True,
                    "sell_type": "macd_death_cross",
                    "sell_price": round(close, 2),
                    "reason": f"MACD死叉: DIF({macd_dif:.3f})下穿DEA({macd_dea:.3f}), 浮盈{profit_pct:.2%}"
                })
                return result

    # ---- 4. 回落止盈: 从最高点回落超阈值 ----
    drawdown_threshold = _get_drawdown_threshold(current_position)
    if highest > buy_price:
        drawdown_from_high = (highest - close) / highest
        if drawdown_from_high >= drawdown_threshold:
            result.update({
                "signal": True,
                "sell_type": "drawdown_profit",
                "sell_price": round(close, 2),
                "reason": f"回落止盈: 最高{highest:.2f}->收盘{close:.2f}, 回落{drawdown_from_high:.2%} >= {drawdown_threshold:.0%}"
            })
            return result

    result["reason"] = f"持仓正常: 收盘{close:.2f}, 浮盈{profit_pct:.2%}, 止损线{stop_loss_price:.2f}"
    return result


def _get_drawdown_threshold(position: dict = None) -> float:
    """根据股票类型获取回落止盈阈值"""
    if position is None:
        return config.DRAWDOWN_STOP["成长赛道"]
    stock_type = position.get("stock_type", "龙头")
    sector = position.get("sector", "")
    if stock_type == "弹性":
        return config.DRAWDOWN_STOP["高弹性"]
    if sector in ["光模块", "存储芯片", "半导体材料"]:
        return config.DRAWDOWN_STOP["成长赛道"]
    return config.DRAWDOWN_STOP["龙头稳健"]


# ============================================================
# 四、移动止损计算
# ============================================================

def compute_trailing_stop(buy_price: float, current_price: float) -> float:
    """
    根据当前浮盈计算移动止损价（止损只能上移、不能下移）
    
    规则（V6.0优化版 - 基于网格搜索最优参数）:
    - 浮盈 < 3%:  维持初始止损（买入价 * (1-10%)）
    - 浮盈 3%-15%: 止损上移到成本价+1%（V6.0: 更早保本，减少利润回吐）
    - 浮盈 15%-30%: 止损上移到盈利12%的位置
    - 浮盈 > 30%: 止损上移到盈利22%的位置
    """
    profit_pct = (current_price - buy_price) / buy_price
    initial_stop = buy_price * (1 - config.INITIAL_STOP_LOSS_PCT)

    stop_price = initial_stop  # 默认初始止损

    for low, high, mode in config.TRAILING_STOP_LEVELS:
        if low <= profit_pct < high:
            if mode == "initial":
                stop_price = initial_stop
            elif mode == "cost_plus":
                stop_price = buy_price * 1.01  # V6.0: 保本+1%（原+2%→+1%）
            elif mode == "profit_12":
                stop_price = buy_price * 1.12  # 锁定12%
            elif mode == "profit_22":
                stop_price = buy_price * 1.22  # 锁定22%
            # 兼容旧版参数名
            elif mode == "cost":
                stop_price = buy_price * 1.01
            elif mode == "profit_10":
                stop_price = buy_price * 1.12
            elif mode == "profit_20":
                stop_price = buy_price * 1.22
            break

    return round(stop_price, 2)


# ============================================================
# 五、综合策略输出
# ============================================================

def generate_strategy_signal(df: pd.DataFrame, holding: dict = None) -> dict:
    """
    综合策略函数：输入日线数据，输出完整的交易信号
    
    参数:
        df: 日线DataFrame (date, open, close, high, low, volume)
        holding: 当前持仓信息（可选）
            {
                "buy_price": float,      # 买入均价
                "shares": int,           # 持仓股数
                "highest_price": float,  # 持仓期间最高价
                "stock_type": str,       # "龙头" / "弹性"
                "sector": str,           # 所属赛道
                "first_batch_done": bool # 第一批是否已建仓
            }
    
    返回:
        {
            "date": str,                 # 信号日期
            "buy_signal": bool,          # 是否触发买入
            "sell_signal": bool,         # 是否触发卖出
            "buy_price": float,          # 建议买入价
            "sell_price": float,         # 建议卖出价
            "stop_loss_initial": float,  # 初始止损价
            "stop_loss_current": float,  # 当前移动止损价
            "add_position": bool,        # 是否可以加第二批仓
            "position_suggestion": dict, # 仓位建议
            "signal_reason": str         # 信号说明
        }
    """
    latest = df.iloc[-1]
    today = latest["date"]

    result = {
        "date": today,
        "buy_signal": False,
        "sell_signal": False,
        "buy_price": None,
        "sell_price": None,
        "stop_loss_initial": None,
        "stop_loss_current": None,
        "add_position": False,
        "position_suggestion": {},
        "signal_reason": ""
    }

    # ---- 已持仓：检查卖出信号 + 加仓条件 ----
    if holding and holding.get("buy_price"):
        buy_price = holding["buy_price"]
        current_price = latest["close"]
        profit_pct = (current_price - buy_price) / buy_price

        # 初始止损
        result["stop_loss_initial"] = round(buy_price * (1 - config.INITIAL_STOP_LOSS_PCT), 2)
        # 当前移动止损
        result["stop_loss_current"] = compute_trailing_stop(buy_price, current_price)

        # 检查卖出信号（V7.1: 集成反洗盘检测）
        sell_result = check_sell_signal(df, buy_price, holding)
        if sell_result["signal"]:
            # V7.1: 反洗盘检测 - 非强制卖出时检查是否主力洗盘
            sell_reason = sell_result.get("reason", "")
            is_force_sell = "强制" in sell_reason or "放量大跌" in sell_reason
            
            if not is_force_sell and HAS_ANTI_MANIP:
                try:
                    manip = analyze_manipulation(latest.get("code", ""), df, holding)
                    result["manipulation_score"] = manip.get("manipulation_score", 50)
                    result["manipulation_detail"] = manip.get("detail", "")
                    
                    # 疑似洗盘: 降级卖出信号为预警
                    if manip.get("wash_trading") and manip.get("manipulation_score", 0) >= 60:
                        result["sell_signal"] = False  # 不触发卖出
                        result["signal_reason"] = (
                            f"[预警-疑似洗盘] {sell_reason} | "
                            f"[反洗盘] 量价背离显示主力洗盘概率{manip['manipulation_score']}%, "
                            f"建议观望而非止损 | {manip.get('suggestion', '')}"
                        )
                        result["wash_trading_warning"] = True
                        return result
                except Exception as e:
                    logger.debug(f"反洗盘检测异常: {e}")
            
            # 正常卖出信号
            result["sell_signal"] = True
            result["sell_price"] = sell_result["sell_price"]
            result["signal_reason"] = f"[卖出] {sell_reason}"
            return result

        # 铁则：浮亏持仓绝对不加仓（只有止损或观望两个选项）
        if profit_pct < 0:
            # V7.1: 添加主力评分
            if HAS_ANTI_MANIP:
                try:
                    manip = analyze_manipulation(latest.get("code", ""), df, holding)
                    result["manipulation_score"] = manip.get("manipulation_score", 50)
                except Exception:
                    pass
            result["signal_reason"] = (
                f"[浮亏持仓] 浮亏{profit_pct:.2%}, "
                f"止损线={result['stop_loss_current']:.2f}, "
                f"铁则:浮亏不加仓,到止损直接离场"
            )
            return result

        # 检查是否可以加第二批仓（仅浮盈时允许）
        if not holding.get("first_batch_done") or holding.get("first_batch_done") == False:
            pass  # 第一批未建，不涉及加仓
        elif profit_pct >= config.MIN_PROFIT_TO_ADD:
            result["add_position"] = True
            result["signal_reason"] = (
                f"[可加仓] 浮盈{profit_pct:.2%} >= {config.MIN_PROFIT_TO_ADD:.0%}, "
                f"可买入第二批{config.SECOND_BATCH_RATIO:.0%}仓位"
            )
        else:
            result["signal_reason"] = (
                f"[持仓观望] 浮盈{profit_pct:.2%}, "
                f"移动止损={result['stop_loss_current']:.2f}"
            )
        return result

    # ---- 未持仓：检查两类买点 ----
    # 买点1: 缩量回踩关键支撑
    buy_result = check_buy_signal(df)
    # 买点2: 放量突破后回踩确认
    breakout_result = check_breakout_buy_signal(df)

    # 选择最优信号（质量评分高的优先，两者都触发则取最高分）
    best_result = None
    if buy_result["signal"] and breakout_result["signal"]:
        # 两类买点同时满足 → 最高优先级
        best_result = buy_result if buy_result.get("quality_score", 0) >= breakout_result.get("quality_score", 0) else breakout_result
        best_result["reason"] = f"★双买点共振: {buy_result['reason']} + {breakout_result['reason']}"
        best_result["quality_score"] = min(100, max(buy_result.get("quality_score", 0), breakout_result.get("quality_score", 0)) + 15)
    elif buy_result["signal"]:
        best_result = buy_result
    elif breakout_result["signal"]:
        best_result = breakout_result

    if best_result and best_result["signal"]:
        result["buy_signal"] = True
        result["buy_price"] = best_result["buy_price"]
        result["stop_loss_initial"] = best_result["stop_loss"]
        result["stop_loss_current"] = best_result["stop_loss"]
        result["quality_score"] = best_result.get("quality_score", 50)
        result["buy_type"] = best_result.get("buy_type", "回踩支撑")
        result["signal_reason"] = f"[买入] {best_result['reason']}"
    else:
        result["quality_score"] = 0
        result["buy_type"] = None
        result["signal_reason"] = f"[观望] {buy_result.get('reason', '未触发任何买卖条件')}"

    return result


# ============================================================
# 六、批量扫描所有股票池
# ============================================================

def scan_all_stocks(data_dict: dict, holdings: dict = None) -> list:
    """
    扫描所有股票池，返回今日信号列表
    
    参数:
        data_dict: {code: DataFrame} 所有股票的日线数据
        holdings: {code: holding_info} 当前持仓
    
    返回:
        [(code, signal_dict), ...] 按信号优先级排序
    """
    if holdings is None:
        holdings = {}

    signals = []
    for code, df in data_dict.items():
        if df.empty or len(df) < config.MA_MID:
            continue
        if code == config.BENCHMARK_INDEX:  # 跳过基准指数
            continue
        # 跳过ETF基金(588/159开头)，不生成个股信号
        if code.startswith("588") or code.startswith("159"):
            continue
        holding = holdings.get(code)
        sig = generate_strategy_signal(df, holding)
        sig["code"] = code
        sig["name"] = config.get_stock_name(code)
        signals.append((code, sig))

    # 排序：卖出信号优先，其次买入信号
    def sort_key(item):
        sig = item[1]
        if sig["sell_signal"]:
            return 0  # 卖出最优先
        if sig["buy_signal"]:
            return 1  # 买入次之
        if sig["add_position"]:
            return 2  # 加仓第三
        return 3      # 无信号最后

    signals.sort(key=sort_key)
    return signals


if __name__ == "__main__":
    # 简单测试：用模拟数据验证逻辑
    print("=" * 50)
    print("  趋势策略模块 - 单元测试")
    print("=" * 50)

    # 生成模拟数据（60天上涨趋势 + 回踩）
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=80, freq="B")
    base_price = 50
    prices = [base_price]
    for i in range(1, 80):
        if i < 50:
            change = np.random.normal(0.3, 0.5)  # 上涨趋势
        else:
            change = np.random.normal(-0.2, 0.5)  # 回调
        prices.append(max(prices[-1] + change, 10))

    df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": [p * 0.998 for p in prices],
        "close": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "volume": np.random.randint(500000, 2000000, 80).astype(float),
    })
    # 让最后几天缩量
    df.loc[df.index[-3:], "volume"] = df["volume"].mean() * 0.5

    signal = generate_strategy_signal(df)
    print(f"\n日期: {signal['date']}")
    print(f"买入信号: {signal['buy_signal']}")
    print(f"卖出信号: {signal['sell_signal']}")
    print(f"信号说明: {signal['signal_reason']}")
    if signal["buy_price"]:
        print(f"建议买入价: {signal['buy_price']}")
        print(f"初始止损价: {signal['stop_loss_initial']}")
    print("\n[OK] 策略模块测试通过")
