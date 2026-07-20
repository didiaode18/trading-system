"""
优化版回测引擎 V6.0
====================
基于大规模模拟结果，针对性优化:

优化点:
1. 市场环境过滤: 熊市减少交易（MA60向下时不开仓）
2. 时间止损: 持仓超过25个交易日强制平仓
3. 收紧止损: 初始止损从8%收紧到7%
4. 行业过滤: 避开历史表现差的行业
5. 提高信号质量门槛: 从55分提高到60分
6. 动态仓位: 根据信号质量调整仓位
"""

import sys
import os
import datetime
import logging

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from backtest_real import (
    fetch_history_data, compute_indicators, is_limit_up, is_limit_down,
    COMMISSION, SLIPPAGE_LEADER, SLIPPAGE_FLEX, LIMIT_PCT
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# V6.0 优化参数
# ============================================================

# 时间止损（交易日）
MAX_HOLD_DAYS = 25

# 初始止损（从8%收紧到7%）
INITIAL_STOP_LOSS = 0.07

# 信号质量门槛（从55提高到60）
MIN_SIGNAL_QUALITY = 60

# 行业黑名单（历史表现差的行业）
INDUSTRY_BLACKLIST = ["建材", "面板", "电力"]

# 市场环境过滤（True=启用）
USE_MARKET_FILTER = True

TOTAL_CAPITAL = 760000


def backtest_stock_v6(df: pd.DataFrame, code: str, info: dict) -> list:
    """
    V6.0优化版回测引擎
    
    相比V5.0的改进:
    - 市场环境过滤: MA60向下时不开新仓
    - 时间止损: 超过25天强制平仓
    - 收紧止损: 7%初始止损
    - 行业过滤: 避开黑名单行业
    - 更高质量门槛: 60分
    """
    if len(df) < 80:
        return []
    
    # 行业过滤
    industry = info.get("行业", "")
    if industry in INDUSTRY_BLACKLIST:
        return []
    
    df = compute_indicators(df)
    trades = []
    stock_type = info.get("类型", "龙头")
    slippage = SLIPPAGE_LEADER if stock_type == "龙头" else SLIPPAGE_FLEX
    
    # 状态
    in_position = False
    buy_price = 0
    buy_date = ""
    buy_index = 0
    highest_since_buy = 0
    position_shares = 0
    initial_shares = 0
    ladder_sold = [False, False]
    
    for i in range(60, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        date = row["date"]
        close = row["close"]
        low = row["low"]
        high = row["high"]
        volume = row["volume"]
        
        if not in_position:
            # ==== 市场环境过滤 ====
            if USE_MARKET_FILTER:
                ma60 = row["ma60"]
                if not pd.isna(ma60) and i >= 10:
                    # MA60近10日斜率
                    ma60_slope = df["ma60"].iloc[i] - df["ma60"].iloc[max(0, i-10)]
                    # MA60明确向下时不开仓
                    if ma60_slope < 0 and close < ma60:
                        continue
            
            # ==== 硬性过滤 ====
            # 1. 流动性
            if "amount" in df.columns and i >= 20:
                avg_amount = df["amount"].iloc[i-19:i+1].mean()
                if avg_amount < getattr(config, 'MIN_DAILY_AMOUNT', 8e8):
                    continue
            
            # 2. 股性稳定
            if i >= 30:
                recent_30 = df.iloc[i-29:i+1]
                amplitude = (recent_30["high"] - recent_30["low"]) / recent_30["close"].shift(1)
                high_amp_days = (amplitude > 0.10).sum()
                if high_amp_days > getattr(config, 'MAX_HIGH_AMPLITUDE_DAYS', 3):
                    continue
            
            # 3. 无放量暴跌
            if i >= 5:
                recent_5 = df.iloc[i-4:i+1]
                has_crash = False
                for j in range(len(recent_5)):
                    r = recent_5.iloc[j]
                    if not pd.isna(r["pct_change"]) and r["pct_change"] < -0.08:
                        vol_ma_check = df["vol_ma20"].iloc[i-4+j] if not pd.isna(df["vol_ma20"].iloc[i-4+j]) else 0
                        if vol_ma_check > 0 and r["volume"] > vol_ma_check * 2:
                            has_crash = True
                            break
                if has_crash:
                    continue
            
            # ==== 买点判定 ====
            ma20 = row["ma20"]
            ma20_slope = row["ma20_slope"]
            ma60 = row["ma60"]
            if pd.isna(ma20) or pd.isna(ma20_slope):
                continue
            
            # 基础条件
            if ma20_slope <= 0 or close < ma20:
                continue
            
            # MA60不能向下
            if not pd.isna(ma60) and i >= 5:
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
            if volume < vol_ma * 0.70 and low <= ma20 * 1.01:
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
            lookback = getattr(config, 'BREAKOUT_LOOKBACK', 10)
            if i >= lookback + 20:
                for k in range(i - lookback, i):
                    k_row = df.iloc[k]
                    k_vol_ma = df["vol_ma20"].iloc[k]
                    if pd.isna(k_vol_ma) or k_vol_ma == 0:
                        continue
                    k_high_20 = df["high"].iloc[max(0, k-19):k].max()
                    if k_row["volume"] > k_vol_ma * 1.5 and k_row["close"] > k_high_20:
                        breakout_close = k_row["close"]
                        if (volume < k_row["volume"] * getattr(config, 'BREAKOUT_PULLBACK_VOL', 0.50) and
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
                
                # 均线多头
                if not pd.isna(ma60) and ma20 > ma60:
                    signal_quality += 5
                
                signal_quality = min(100, signal_quality)
                
                # V6: 提高质量门槛到60分
                if signal_quality < MIN_SIGNAL_QUALITY:
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
            
            # V6: 根据信号质量动态调整仓位
            base_ratio = 0.10  # 基础10%
            quality_bonus = (signal_quality - 60) * 0.002  # 每多1分加0.2%
            position_ratio = min(0.15, base_ratio + quality_bonus)
            
            initial_shares = int(TOTAL_CAPITAL * position_ratio / exec_price / 100) * 100
            if initial_shares < 100:
                initial_shares = 100
            position_shares = initial_shares
            ladder_sold = [False, False]
        
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
            
            # ---- V6新增: 时间止损 ----
            hold_days = i - buy_index
            if hold_days >= MAX_HOLD_DAYS:
                sell_signal = True
                sell_type = "时间止损"
                sell_ratio = 1.0
            
            # ---- 强制卖出 ----
            if not sell_signal:
                if not pd.isna(row["pct_change"]) and row["pct_change"] < -0.08:
                    if not pd.isna(vol_ma) and vol_ma > 0 and volume > vol_ma * 2:
                        sell_signal = True
                        sell_type = "强制卖出"
                        sell_ratio = 1.0
            
            # ---- 移动止损（V6: 收紧初始止损到7%）----
            if not sell_signal:
                if profit_pct < 0.05:
                    stop_price = buy_price * (1 - INITIAL_STOP_LOSS)  # 7%
                elif profit_pct < 0.15:
                    stop_price = buy_price * 1.02
                elif profit_pct < 0.30:
                    stop_price = buy_price * 1.12
                else:
                    stop_price = buy_price * 1.22
                
                if close <= stop_price:
                    sell_signal = True
                    sell_type = "止损"
                    sell_ratio = 1.0
            
            # ---- 趋势破位 ----
            if not sell_signal and not pd.isna(row["ma60"]):
                ma60_slope = df["ma60"].iloc[i] - df["ma60"].iloc[max(0, i-3)]
                if close < row["ma60"] and ma60_slope < 0:
                    sell_signal = True
                    sell_type = "趋势破位"
                    sell_ratio = 1.0
            
            # ---- 双轨止盈 ----
            if not sell_signal and position_shares > 0:
                if not ladder_sold[0] and profit_pct >= 0.08:
                    sell_signal = True
                    sell_type = "阶梯止盈1"
                    sell_ratio = 1/3
                    ladder_sold[0] = True
                elif not ladder_sold[1] and profit_pct >= 0.20:
                    sell_signal = True
                    sell_type = "阶梯止盈2"
                    sell_ratio = 1/3
                    ladder_sold[1] = True
            
            # ---- 回落止盈 ----
            if not sell_signal and highest_since_buy > buy_price * 1.05:
                drawdown_threshold = 0.05 if stock_type == "龙头" else 0.03
                drawdown = (highest_since_buy - close) / highest_since_buy
                if drawdown >= drawdown_threshold and profit_pct > 0:
                    sell_signal = True
                    sell_type = "回落止盈"
                    sell_ratio = 1.0
            
            # ---- MACD死叉 ----
            if not sell_signal and profit_pct > 0:
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
                    "industry": industry,
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
                })
                
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
        hold_days_final = len(df) - 1 - buy_index
        trades.append({
            "code": code,
            "name": info["名称"],
            "industry": industry,
            "stock_type": stock_type,
            "buy_date": buy_date,
            "buy_price": round(buy_price, 3),
            "sell_date": last_row["date"],
            "sell_price": round(exec_price, 3),
            "gross_profit": round(gross_profit * 100, 2),
            "net_profit": round(net_profit * 100, 2),
            "hold_days": hold_days_final,
            "sell_type": "回测结束",
            "highest": round(highest_since_buy, 3),
            "sell_ratio": 1.0,
        })
    
    return trades


def compare_v5_v6():
    """对比V5和V6"""
    from backtest_extended import EXTENDED_STOCKS
    from backtest_real import backtest_stock_v5, analyze_trades
    
    logger.info("=" * 70)
    logger.info("  V5.0 vs V6.0 对比回测")
    logger.info("=" * 70)
    
    trades_v5 = []
    trades_v6 = []
    
    for idx, (code, info) in enumerate(EXTENDED_STOCKS.items(), 1):
        logger.info(f"[{idx}/{len(EXTENDED_STOCKS)}] {code} {info['名称']}...")
        try:
            df = fetch_history_data(code, start_date="2020-01-01")
            if df.empty or len(df) < 120:
                continue
            
            t5 = backtest_stock_v5(df, code, info)
            t6 = backtest_stock_v6(df, code, info)
            trades_v5.extend(t5)
            trades_v6.extend(t6)
            logger.info(f"  V5:{len(t5)}笔 | V6:{len(t6)}笔")
        except Exception as e:
            logger.error(f"  失败: {e}")
    
    if not trades_v5 or not trades_v6:
        logger.error("无交易记录")
        return
    
    trades_v5.sort(key=lambda x: x["buy_date"])
    trades_v6.sort(key=lambda x: x["buy_date"])
    
    stats_v5 = analyze_trades(trades_v5)
    stats_v6 = analyze_trades(trades_v6)
    
    logger.info("\n" + "=" * 70)
    logger.info(f"  {'指标':<12} {'V5.0':<15} {'V6.0':<15} {'变化':<15}")
    logger.info("-" * 70)
    logger.info(f"  {'总交易':<12} {stats_v5['total']:<15} {stats_v6['total']:<15} {stats_v6['total']-stats_v5['total']:+d}")
    logger.info(f"  {'胜率':<12} {stats_v5['win_rate']}%{'':<10} {stats_v6['win_rate']}%{'':<10} {stats_v6['win_rate']-stats_v5['win_rate']:+.1f}%")
    logger.info(f"  {'盈亏比':<12} {stats_v5['profit_factor']:<15} {stats_v6['profit_factor']:<15} {stats_v6['profit_factor']-stats_v5['profit_factor']:+.2f}")
    logger.info(f"  {'每笔期望':<12} {stats_v5['expectancy']:+.2f}%{'':<9} {stats_v6['expectancy']:+.2f}%{'':<9} {stats_v6['expectancy']-stats_v5['expectancy']:+.2f}%")
    logger.info(f"  {'最大连亏':<12} {stats_v5['max_consec_loss']:<15} {stats_v6['max_consec_loss']:<15} {stats_v6['max_consec_loss']-stats_v5['max_consec_loss']:+d}")
    logger.info("=" * 70)
    
    return stats_v5, stats_v6


if __name__ == "__main__":
    compare_v5_v6()
