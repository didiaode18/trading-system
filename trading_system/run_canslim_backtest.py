# -*- coding: utf-8 -*-
"""
CANSLIM选股引擎 + 综合分析报告 历史回测验证
=============================================
基于真实历史数据验证两大核心模块的有效性

回测内容:
A. CANSLIM选股引擎: 六因子预测效果/买点触发率/止损保护/行业配额
B. 综合分析报告: composite评分预测力/调仓逻辑/加仓计划

数据源: 本地SQLite (config.DB_PATH)
标的: STOCK_POOL + SECTOR_CANDIDATES 全部标的
区间: 2022-01-01 ~ 当前
成本: 佣金万2.5 + 印花税千1 + 滑点0.1% + T+1
"""

import sys
import os
import time
import logging
import datetime
import sqlite3
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("canslim_backtest")

# ============================================================
# 回测参数
# ============================================================
START_DATE = "2022-01-01"
END_DATE = datetime.date.today().strftime("%Y-%m-%d")
INITIAL_CAPITAL = config.TOTAL_CAPITAL  # ~73万
COMMISSION_RATE = 0.00025   # 佣金万2.5
STAMP_TAX = 0.001           # 印花税千1
SLIPPAGE = 0.001            # 滑点0.1%
SCAN_INTERVAL = 5           # 每5个交易日扫描一次(周度)


# ============================================================
# 一、数据加载
# ============================================================

def load_data():
    """从SQLite加载全部历史数据"""
    if not os.path.exists(config.DB_PATH):
        logger.error(f"[FAIL] 数据库不存在: {config.DB_PATH}")
        return {}

    conn = sqlite3.connect(config.DB_PATH)
    query = "SELECT code, date, open, close, high, low, volume, amount FROM daily_kline ORDER BY code, date"
    df_all = pd.read_sql(query, conn)
    conn.close()

    data_dict = {}
    for code, group in df_all.groupby("code"):
        df = group.copy()
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        if len(df) > 250:
            data_dict[code] = df

    logger.info(f"[OK] 加载 {len(data_dict)} 只股票, 日期范围 {df_all['date'].min()} ~ {df_all['date'].max()}")
    return data_dict


def compute_indicators(df):
    """计算技术指标(向量化)"""
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma20_slope"] = df["ma20"].diff(3)
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_dif"] = ema12 - ema26
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()

    # RSI
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ATR
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    df["atr"] = pd.Series(tr, index=df.index).rolling(14).mean()

    # BOLL
    df["boll_mid"] = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    df["boll_upper"] = df["boll_mid"] + 2 * std20
    df["boll_lower"] = df["boll_mid"] - 2 * std20

    df["pct_change"] = df["close"].pct_change()
    return df


# ============================================================
# 二、CANSLIM因子评分(简化回测版)
# ============================================================

def calc_canslim_factors(df, idx, all_data=None, use_v2=False):
    """
    在df的第idx行计算CANSLIM六因子评分(简化版,与stock_screener.py逻辑一致)
    返回: {N, S, L, CAI, P, W, total}

    V2改进:
      P0-1: P因子从均值回归(RSI超卖)改为动量导向(RSI偏强+MACD金叉)
      P0-2: CAI从固定8分改为20日动量代理(对齐实盘stock_screener.py)
      P2-1: N因子增加120日新高维度
      P2-2: S因子满分对齐实盘(20→10)
    """
    if idx < 60:
        return None

    row = df.iloc[idx]
    close = row["close"]
    ma20 = row["ma20"]
    ma60 = row["ma60"]
    volume = row["volume"]
    vol_ma20 = row["vol_ma20"]

    if pd.isna(ma20) or pd.isna(ma60):
        return None

    factors = {}

    # N因子(新事物): 创新高+突破形态 (满分20)
    n_score = 0
    if idx >= 60:
        high_60 = df["high"].iloc[idx-60:idx].max()
        if close >= high_60 * 0.98:
            n_score += 12
        if close > ma20 and ma20 > ma60:
            n_score += 8
        # V2: 120日新高维度(更长期的突破更可靠)
        if use_v2 and idx >= 120:
            high_120 = df["high"].iloc[idx-120:idx].max()
            if close >= high_120:
                n_score += 5  # 创半年新高额外加分
    factors["N"] = min(n_score, 20)

    # S因子(供需): 量价配合
    # V2: 满分对齐实盘(20→10)
    s_max = 10 if use_v2 else 20
    s_score = 0
    if not pd.isna(vol_ma20) and vol_ma20 > 0:
        vol_ratio = volume / vol_ma20
        if vol_ratio < 0.7 and close >= ma20 * 0.99:
            s_score += 6 if use_v2 else 12  # 缩量回踩
        if vol_ratio > 1.5 and close > df["close"].iloc[max(0, idx-1)]:
            s_score += 4 if use_v2 else 8   # 放量上涨
        if idx >= 5:
            vol_5d = df["volume"].iloc[idx-5:idx].mean()
            if vol_5d < vol_ma20 * 0.8:
                s_score += 3 if use_v2 else 5  # 近5日缩量
    factors["S"] = min(s_score, s_max)

    # L因子(龙头): 相对强度 (满分20)
    l_score = 0
    if idx >= 20:
        change_20d = (close / df["close"].iloc[idx-20] - 1) * 100
        if change_20d > 10:
            l_score += 12
        elif change_20d > 5:
            l_score += 8
        elif change_20d > 0:
            l_score += 4
        # V2: 60日相对强度(更长期的龙头更可靠)
        if use_v2 and idx >= 60:
            change_60d = (close / df["close"].iloc[idx-60] - 1) * 100
            if change_60d > 20:
                l_score += 5
            elif change_60d > 10:
                l_score += 3
    if not pd.isna(ma20) and close > ma20:
        l_score += 5
    if not pd.isna(ma60) and close > ma60:
        l_score += 3
    factors["L"] = min(l_score, 20)

    # CAI因子(基本面)
    if use_v2:
        # V2(P0-2): 用20日动量代理替代固定8分(对齐实盘stock_screener.py逻辑)
        if idx >= 21:
            _proxy_chg = (close / df["close"].iloc[idx-21] - 1) * 100
            if _proxy_chg > 10:
                cai_score = 10
            elif _proxy_chg > 5:
                cai_score = 8
            elif _proxy_chg > 0:
                cai_score = 6
            else:
                cai_score = 4
        else:
            cai_score = 6
        factors["CAI"] = cai_score
    else:
        # V1: 固定中性分
        factors["CAI"] = 8

    # P因子(买点): 回踩支撑+技术形态 (满分20)
    if use_v2:
        # V2(P0-1): 从均值回归(RSI超卖)改为动量导向
        p_score = 0
        ma20_slope = row["ma20_slope"]
        if not pd.isna(ma20_slope) and ma20_slope > 0:
            p_score += 6  # MA20向上(趋势确认)
        if not pd.isna(row.get("rsi")):
            rsi = row["rsi"]
            # V2: 动量导向 - RSI偏强(50-70)给高分，超卖(30-50)不再奖励
            if 50 <= rsi < 70:
                p_score += 8  # 健康动量区间
            elif 40 <= rsi < 50:
                p_score += 4  # 中性偏弱(给少量分)
            # RSI<30不再给分(超跌不等于买点)
        # V2: MACD金叉确认(替代MA20回踩)
        if not pd.isna(row.get("macd_dif")) and not pd.isna(row.get("macd_dea")):
            if row["macd_dif"] > row["macd_dea"]:
                p_score += 6  # MACD在零轴上方/金叉
        factors["P"] = min(p_score, 20)
    else:
        # V1: 原逻辑(RSI超卖+MA20回踩)
        p_score = 0
        ma20_slope = row["ma20_slope"]
        if not pd.isna(ma20_slope) and ma20_slope > 0:
            p_score += 6  # MA20向上
        if not pd.isna(row.get("rsi")):
            rsi = row["rsi"]
            if 30 < rsi < 50:
                p_score += 8  # RSI超卖回升区间
            elif 50 <= rsi < 70:
                p_score += 5  # RSI偏强
        low = row["low"]
        if not pd.isna(ma20) and low <= ma20 * 1.01 and close >= ma20:
            p_score += 6  # 回踩MA20确认
        factors["P"] = min(p_score, 20)

    # W因子(大盘): 简化为基于指数均线(满分+5/-5)
    w_score = 0
    if "000300" in (all_data or {}):
        idx_df = all_data["000300"]
        idx_date = row["date"]
        idx_mask = idx_df["date"] <= idx_date
        if idx_mask.sum() >= 60:
            idx_row = idx_df[idx_mask].iloc[-1]
            idx_close = idx_row["close"]
            idx_ma20 = idx_row.get("ma20", np.nan)
            idx_ma60 = idx_row.get("ma60", np.nan)
            if not pd.isna(idx_ma20) and not pd.isna(idx_ma60):
                if idx_close > idx_ma20 > idx_ma60:
                    w_score = 5
                elif idx_close < idx_ma20 < idx_ma60:
                    w_score = -5
    factors["W"] = w_score

    total = factors["N"] + factors["S"] + factors["L"] + factors["CAI"] + factors["P"] + factors["W"]
    factors["total"] = total
    return factors


# ============================================================
# 三、Composite评分(与generate_holdings_report.py一致)
# ============================================================

def calc_composite(df, idx, use_v2=False):
    """计算composite = 50 + trend_score*8 + momentum_score*6

    V2改进: 增加量价维度(volume_trend)
    """
    if idx < 60:
        return None

    row = df.iloc[idx]
    close = row["close"]
    ma5, ma10, ma20, ma60 = row["ma5"], row["ma10"], row["ma20"], row["ma60"]
    ma20_slope = row["ma20_slope"]
    rsi = row["rsi"]
    macd_dif, macd_dea = row["macd_dif"], row["macd_dea"]

    if pd.isna(ma20):
        return None

    # trend_score
    trend_score = 0
    if not pd.isna(ma5) and not pd.isna(ma10):
        if ma5 > ma10 > ma20:
            trend_score += 2
        elif ma5 < ma10 < ma20:
            trend_score -= 2
    if not pd.isna(ma20_slope):
        trend_score += 1 if ma20_slope > 0 else -1
    if not pd.isna(ma60):
        if close > ma60:
            trend_score += 1
        else:
            trend_score -= 1

    # momentum_score
    momentum_score = 0
    if not pd.isna(rsi):
        if rsi > 70:
            momentum_score -= 1
        elif rsi < 30:
            momentum_score += 1
        elif rsi > 55:
            momentum_score += 0.5
    if not pd.isna(macd_dif) and not pd.isna(macd_dea):
        momentum_score += 1 if macd_dif > macd_dea else -1

    # V2: 量价维度
    vol_trend = 0
    if use_v2:
        vol_ma20 = row.get("vol_ma20", np.nan)
        if not pd.isna(vol_ma20) and vol_ma20 > 0:
            vol_ratio = row["volume"] / vol_ma20
            if vol_ratio > 1.3 and close > row.get("open", close):
                vol_trend = 1   # 放量上涨
            elif vol_ratio < 0.6 and close < ma20:
                vol_trend = -1  # 缩量下跌

    composite = 50 + trend_score * 8 + momentum_score * 6 + (vol_trend * 4 if use_v2 else 0)
    composite = max(0, min(100, composite))
    return {"composite": composite, "trend_score": trend_score, "momentum_score": momentum_score}


# ============================================================
# 四、CANSLIM选股引擎回测
# ============================================================

def run_canslim_backtest(data_dict, use_v2=False):
    """
    A. CANSLIM选股引擎回测
    - 每SCAN_INTERVAL天扫描一次,记录推荐信号
    - 跟踪5/10/20日前瞻收益
    """
    logger.info("=" * 60)
    logger.info("  [A] CANSLIM选股引擎回测")
    logger.info("=" * 60)

    # 预计算指标
    precomputed = {}
    for code, df in data_dict.items():
        precomputed[code] = compute_indicators(df)
    logger.info(f"  [OK] 指标预计算完成: {len(precomputed)} 只")

    # 获取交易日历(用000300)
    if "000300" not in precomputed:
        logger.error("[FAIL] 无000300基准数据")
        return {}
    calendar = precomputed["000300"]["date"].values
    start_mask = calendar >= START_DATE
    end_mask = calendar <= END_DATE
    trade_dates = calendar[start_mask & end_mask]
    logger.info(f"  [OK] 回测区间: {trade_dates[0]} ~ {trade_dates[-1]} ({len(trade_dates)} 交易日)")

    # 候选股(排除000300和ETF)
    candidates = [c for c in precomputed.keys()
                  if c != "000300" and not c.startswith("588") and not c.startswith("159")]
    logger.info(f"  [OK] 候选标的: {len(candidates)} 只")

    # 记录所有信号
    signals = []  # {date, code, score, factors, forward_5d, forward_10d, forward_20d}
    scan_count = 0

    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        scan_count += 1

        for code in candidates:
            df = precomputed[code]
            # 找到scan_date在df中的位置
            date_mask = df["date"] <= scan_date
            if date_mask.sum() < 60:
                continue
            idx = date_mask.sum() - 1

            # 硬性筛选(简化): MA20向上 + 收盘价在MA20上方(强势) 或 weak_score>=25(弱势)
            row = df.iloc[idx]
            ma20 = row["ma20"]
            ma20_slope = row["ma20_slope"]
            close = row["close"]
            if pd.isna(ma20) or pd.isna(ma20_slope):
                continue

            # 判断市场环境
            idx_df = precomputed["000300"]
            idx_mask = idx_df["date"] <= scan_date
            if idx_mask.sum() < 60:
                continue
            idx_row = idx_df[idx_mask].iloc[-1]
            market_up = (not pd.isna(idx_row["ma20"]) and not pd.isna(idx_row["ma60"]) and
                         idx_row["close"] > idx_row["ma20"] > idx_row["ma60"])

            # 硬性筛选
            if market_up:
                if ma20_slope <= 0 or close < ma20:
                    continue
            else:
                # 弱势: 简化为close不能偏离MA20太远
                if close < ma20 * 0.85:
                    continue

            # 流动性
            if "amount" in df.columns:
                avg_amt = df["amount"].iloc[max(0, idx-19):idx+1].mean()
                if not pd.isna(avg_amt) and avg_amt < config.MIN_DAILY_AMOUNT:
                    continue

            # CANSLIM评分
            factors = calc_canslim_factors(df, idx, precomputed, use_v2=use_v2)
            if factors is None:
                continue

            # 前瞻收益
            fwd = {}
            for days in [5, 10, 20]:
                if idx + days < len(df):
                    fwd_close = df["close"].iloc[idx + days]
                    fwd[f"{days}d"] = (fwd_close - close) / close * 100
                else:
                    fwd[f"{days}d"] = np.nan

            signals.append({
                "date": scan_date,
                "code": code,
                "close": close,
                "score": factors["total"],
                "N": factors["N"], "S": factors["S"], "L": factors["L"],
                "CAI": factors["CAI"], "P": factors["P"], "W": factors["W"],
                "market_up": market_up,
                **fwd
            })

    logger.info(f"  [OK] 扫描{scan_count}次, 产生{len(signals)}个信号")

    if not signals:
        return {"error": "无信号"}

    sig_df = pd.DataFrame(signals)

    # --- 统计分析 ---
    results = {}

    # A1: 按评分分组统计前瞻收益
    logger.info("\n  --- A1: 评分分组前瞻收益 ---")
    score_bins = [(70, 105, ">=70"), (50, 70, "50-69"), (35, 50, "35-49"), (0, 35, "<35")]
    score_stats = []
    for lo, hi, label in score_bins:
        subset = sig_df[(sig_df["score"] >= lo) & (sig_df["score"] < hi)]
        if len(subset) == 0:
            continue
        stat = {
            "group": label,
            "count": len(subset),
            "avg_5d": subset["5d"].mean(),
            "avg_10d": subset["10d"].mean(),
            "avg_20d": subset["20d"].mean(),
            "win_rate_5d": (subset["5d"] > 0).mean() * 100,
            "win_rate_20d": (subset["20d"] > 0).mean() * 100,
        }
        score_stats.append(stat)
        logger.info(f"    {label}: N={stat['count']}, 5d={stat['avg_5d']:+.2f}%, "
                    f"20d={stat['avg_20d']:+.2f}%, WR20={stat['win_rate_20d']:.1f}%")
    results["score_stats"] = score_stats

    # A2: 各因子贡献分析
    logger.info("\n  --- A2: 因子贡献分析 ---")
    factor_stats = []
    for fname in ["N", "S", "L", "P", "W"]:
        high = sig_df[sig_df[fname] >= sig_df[fname].quantile(0.7)]
        low = sig_df[sig_df[fname] <= sig_df[fname].quantile(0.3)]
        if len(high) > 0 and len(low) > 0:
            diff = high["20d"].mean() - low["20d"].mean()
            factor_stats.append({
                "factor": fname,
                "high_20d": high["20d"].mean(),
                "low_20d": low["20d"].mean(),
                "spread": diff,
                "effective": diff > 0.5
            })
            mark = "[OK]" if diff > 0.5 else "[WARN]"
            logger.info(f"    {mark} {fname}: 高分组20d={high['20d'].mean():+.2f}% vs "
                        f"低分组={low['20d'].mean():+.2f}%, 差={diff:+.2f}%")
    results["factor_stats"] = factor_stats

    # A3: min_buy_score阈值验证
    logger.info("\n  --- A3: 买入阈值验证 ---")
    threshold_stats = []
    for threshold in [35, 40, 45, 50, 55, 60]:
        above = sig_df[sig_df["score"] >= threshold]
        if len(above) < 10:
            continue
        wr = (above["20d"] > 0).mean() * 100
        avg = above["20d"].mean()
        threshold_stats.append({
            "threshold": threshold,
            "count": len(above),
            "win_rate_20d": wr,
            "avg_20d": avg
        })
        logger.info(f"    >={threshold}: N={len(above)}, WR20={wr:.1f}%, avg20d={avg:+.2f}%")
    results["threshold_stats"] = threshold_stats

    # A4: 止损保护效果
    logger.info("\n  --- A4: 止损保护效果(10%) ---")
    buy_signals = sig_df[sig_df["score"] >= 50].copy()
    if len(buy_signals) > 0:
        # 模拟: 买入后20日内最大回撤
        stop_triggered = 0
        stop_saved = 0  # 止损后继续下跌(止损有效)
        total_checked = 0
        for _, sig in buy_signals.iterrows():
            code = sig["code"]
            df = precomputed[code]
            date_mask = df["date"] <= sig["date"]
            if date_mask.sum() < 1:
                continue
            idx = date_mask.sum() - 1
            buy_price = sig["close"]
            stop_price = buy_price * (1 - config.INITIAL_STOP_LOSS_PCT)
            # 检查后续20日
            triggered = False
            min_after_stop = 0
            for d in range(1, min(21, len(df) - idx)):
                future_low = df["low"].iloc[idx + d]
                if future_low <= stop_price:
                    triggered = True
                    # 止损后继续跌了多少
                    if idx + d + 5 < len(df):
                        min_after_stop = min(min_after_stop,
                                             (df["close"].iloc[idx+d:idx+d+5].min() - stop_price) / stop_price * 100)
                    break
            if triggered:
                stop_triggered += 1
                if min_after_stop < -2:
                    stop_saved += 1
                total_checked += 1

        stop_rate = stop_triggered / len(buy_signals) * 100 if len(buy_signals) > 0 else 0
        save_rate = stop_saved / stop_triggered * 100 if stop_triggered > 0 else 0
        logger.info(f"    买入信号N={len(buy_signals)}, 触发止损={stop_triggered}({stop_rate:.1f}%), "
                    f"止损后继续跌={stop_saved}({save_rate:.1f}%)")
        results["stop_loss"] = {
            "total_signals": len(buy_signals),
            "triggered": stop_triggered,
            "trigger_rate": stop_rate,
            "saved": stop_saved,
            "save_rate": save_rate
        }

    # A5: 三档买点触发率
    logger.info("\n  --- A5: 三档买点触发率 ---")
    bp_stats = {"aggressive": 0, "moderate": 0, "conservative": 0, "total": 0}
    bp_profits = {"aggressive": [], "moderate": [], "conservative": []}
    for _, sig in buy_signals.iterrows():
        code = sig["code"]
        df = precomputed[code]
        date_mask = df["date"] <= sig["date"]
        if date_mask.sum() < 2:
            continue
        idx = date_mask.sum() - 1
        close = sig["close"]
        ma5 = df["ma5"].iloc[idx]
        ma10 = df["ma10"].iloc[idx]
        ma20 = df["ma20"].iloc[idx]

        aggressive_buy = close * 1.005
        moderate_buy = max(ma5, ma10) * 1.005 if not pd.isna(ma5) and not pd.isna(ma10) else close * 0.99
        conservative_buy = ma20 * 1.005 if not pd.isna(ma20) else close * 0.97

        bp_stats["total"] += 1
        # 检查5日内是否触发
        # FIX: aggressive 改为每个信号独立判断（原条件 bp_stats["aggressive"] == bp_stats["total"] - 1 导致漏计），触及计一次并 break 防重复计数
        for d in range(1, min(6, len(df) - idx)):
            low = df["low"].iloc[idx + d]
            if low <= aggressive_buy:
                bp_stats["aggressive"] += 1
                # 用触及当日 low 计算前瞻收益（保持原语义）
                fwd_10 = (df["close"].iloc[min(idx+d+10, len(df)-1)] - low) / low * 100 if idx+d+10 < len(df) else 0
                bp_profits["aggressive"].append(fwd_10)
                break
        for d in range(1, min(6, len(df) - idx)):
            low = df["low"].iloc[idx + d]
            fwd_10 = (df["close"].iloc[min(idx+d+10, len(df)-1)] - low) / low * 100 if idx+d+10 < len(df) else 0
            if low <= moderate_buy:
                bp_stats["moderate"] += 1
                bp_profits["moderate"].append(fwd_10)
                break
        for d in range(1, min(6, len(df) - idx)):
            low = df["low"].iloc[idx + d]
            if low <= conservative_buy:
                bp_stats["conservative"] += 1
                fwd_10 = (df["close"].iloc[min(idx+d+10, len(df)-1)] - low) / low * 100 if idx+d+10 < len(df) else 0
                bp_profits["conservative"].append(fwd_10)
                break

    total_bp = max(bp_stats["total"], 1)
    logger.info(f"    激进买点: 触发{bp_stats['aggressive']}次({bp_stats['aggressive']/total_bp*100:.1f}%), "
                f"avg10d={np.mean(bp_profits['aggressive']) if bp_profits['aggressive'] else 0:+.2f}%")
    logger.info(f"    稳健买点: 触发{bp_stats['moderate']}次({bp_stats['moderate']/total_bp*100:.1f}%), "
                f"avg10d={np.mean(bp_profits['moderate']) if bp_profits['moderate'] else 0:+.2f}%")
    logger.info(f"    保守买点: 触发{bp_stats['conservative']}次({bp_stats['conservative']/total_bp*100:.1f}%), "
                f"avg10d={np.mean(bp_profits['conservative']) if bp_profits['conservative'] else 0:+.2f}%")
    results["buy_points"] = bp_stats
    results["buy_point_profits"] = {k: np.mean(v) if v else 0 for k, v in bp_profits.items()}

    results["sig_df"] = sig_df
    results["precomputed"] = precomputed
    return results


# ============================================================
# 五、综合分析报告回测
# ============================================================

def run_composite_backtest(data_dict, precomputed, use_v2=False):
    """
    B. 综合分析报告逻辑回测
    - composite评分预测力
    - 调仓逻辑验证
    - 加仓计划验证
    """
    logger.info("\n" + "=" * 60)
    logger.info("  [B] 综合分析报告逻辑回测")
    logger.info("=" * 60)

    candidates = [c for c in precomputed.keys()
                  if c != "000300" and not c.startswith("588") and not c.startswith("159")]

    # B1: Composite评分预测力
    logger.info("\n  --- B1: Composite评分预测力 ---")
    composite_records = []

    calendar = precomputed["000300"]["date"].values
    start_mask = calendar >= START_DATE
    end_mask = calendar <= END_DATE
    trade_dates = calendar[start_mask & end_mask]

    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        for code in candidates:
            df = precomputed[code]
            date_mask = df["date"] <= scan_date
            if date_mask.sum() < 60:
                continue
            idx = date_mask.sum() - 1

            result = calc_composite(df, idx, use_v2=use_v2)
            if result is None:
                continue

            close = df["close"].iloc[idx]
            fwd_20d = np.nan
            if idx + 20 < len(df):
                fwd_20d = (df["close"].iloc[idx + 20] - close) / close * 100

            composite_records.append({
                "date": scan_date,
                "code": code,
                "composite": result["composite"],
                "trend_score": result["trend_score"],
                "momentum_score": result["momentum_score"],
                "fwd_20d": fwd_20d
            })

    comp_df = pd.DataFrame(composite_records)
    logger.info(f"  [OK] 计算{len(comp_df)}条composite记录")

    # 分组对比
    comp_bins = [(70, 101, ">=70"), (55, 70, "55-69"), (45, 55, "45-54"), (30, 45, "30-44"), (0, 30, "<30")]
    comp_stats = []
    for lo, hi, label in comp_bins:
        subset = comp_df[(comp_df["composite"] >= lo) & (comp_df["composite"] < hi)]
        if len(subset) < 5:
            continue
        stat = {
            "group": label,
            "count": len(subset),
            "avg_20d": subset["fwd_20d"].mean(),
            "win_rate": (subset["fwd_20d"] > 0).mean() * 100,
        }
        comp_stats.append(stat)
        logger.info(f"    composite {label}: N={stat['count']}, 20d={stat['avg_20d']:+.2f}%, "
                    f"WR={stat['win_rate']:.1f}%")

    # B2: 调仓逻辑验证
    # FIX: 阈值改从 config.REBALANCE_CONFIG 单一来源读取，与实盘口径对齐（原硬编码 40/65/20 为错误旧值）
    _rb_cfg = getattr(config, "REBALANCE_CONFIG", {"score_gap": 25, "sell_threshold": 30, "buy_threshold": 70})
    SELL_THRESHOLD = _rb_cfg["sell_threshold"]   # 30
    BUY_THRESHOLD = _rb_cfg["buy_threshold"]     # 70
    SCORE_GAP = _rb_cfg["score_gap"]             # 25
    logger.info(f"\n  --- B2: 调仓逻辑验证(SELL<{SELL_THRESHOLD}, BUY>{BUY_THRESHOLD}, GAP>={SCORE_GAP}) ---")

    rebalance_events = []
    # 每个扫描日检查是否有调仓机会
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        day_scores = comp_df[comp_df["date"] == scan_date].copy()
        if len(day_scores) < 2:
            continue

        sells = day_scores[day_scores["composite"] < SELL_THRESHOLD]
        buys = day_scores[day_scores["composite"] > BUY_THRESHOLD]

        if len(sells) > 0 and len(buys) > 0:
            worst = sells.sort_values("composite").iloc[0]
            best = buys.sort_values("composite", ascending=False).iloc[0]
            gap = best["composite"] - worst["composite"]
            if gap >= SCORE_GAP:
                rebalance_events.append({
                    "date": scan_date,
                    "sell_code": worst["code"],
                    "sell_score": worst["composite"],
                    "sell_fwd20": worst["fwd_20d"],
                    "buy_code": best["code"],
                    "buy_score": best["composite"],
                    "buy_fwd20": best["fwd_20d"],
                    "gap": gap
                })

    if rebalance_events:
        rb_df = pd.DataFrame(rebalance_events)
        sell_avg = rb_df["sell_fwd20"].mean()
        buy_avg = rb_df["buy_fwd20"].mean()
        benefit = buy_avg - sell_avg
        logger.info(f"    调仓事件: {len(rb_df)}次")
        logger.info(f"    卖出标的20d平均: {sell_avg:+.2f}%")
        logger.info(f"    买入标的20d平均: {buy_avg:+.2f}%")
        logger.info(f"    调仓收益差: {benefit:+.2f}% {'[OK]有效' if benefit > 0 else '[WARN]无效'}")
        rebalance_result = {
            "events": len(rb_df),
            "sell_avg_20d": sell_avg,
            "buy_avg_20d": buy_avg,
            "benefit": benefit
        }
    else:
        # FIX: 文案与实际阈值同步（SCORE_GAP 取自 config.REBALANCE_CONFIG）
        logger.info(f"    [WARN] 无调仓事件触发(SCORE_GAP={SCORE_GAP}可能过大)")
        # 测试不同GAP
        for gap_test in [10, 15, 20, 25, 30]:
            cnt = 0
            for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
                scan_date = trade_dates[scan_idx]
                day_scores = comp_df[comp_df["date"] == scan_date]
                if len(day_scores) < 2:
                    continue
                sells = day_scores[day_scores["composite"] < SELL_THRESHOLD]
                buys = day_scores[day_scores["composite"] > BUY_THRESHOLD]
                if len(sells) > 0 and len(buys) > 0:
                    gap = buys["composite"].max() - sells["composite"].min()
                    if gap >= gap_test:
                        cnt += 1
            logger.info(f"      GAP>={gap_test}: {cnt}次触发")
        rebalance_result = {"events": 0, "benefit": 0}

    # B3: 加仓计划验证(触发价=压力位or现价+5%, 加仓30%)
    logger.info("\n  --- B3: 加仓计划验证 ---")
    add_events = []
    high_comp = comp_df[comp_df["composite"] >= 60].copy()
    for _, rec in high_comp.iterrows():
        code = rec["code"]
        df = precomputed[code]
        date_mask = df["date"] <= rec["date"]
        if date_mask.sum() < 20:
            continue
        idx = date_mask.sum() - 1
        close = df["close"].iloc[idx]
        add_price = close * 1.05  # 现价+5%触发加仓

        # 检查10日内是否触发加仓
        triggered = False
        for d in range(1, min(11, len(df) - idx)):
            if df["high"].iloc[idx + d] >= add_price:
                triggered = True
                # 加仓后10日收益
                if idx + d + 10 < len(df):
                    add_ret = (df["close"].iloc[idx + d + 10] - add_price) / add_price * 100
                    add_events.append({"ret_10d": add_ret, "triggered": True})
                break
        if not triggered:
            add_events.append({"ret_10d": 0, "triggered": False})

    if add_events:
        add_df = pd.DataFrame(add_events)
        trigger_rate = add_df["triggered"].mean() * 100
        triggered_subset = add_df[add_df["triggered"]]
        avg_ret = triggered_subset["ret_10d"].mean() if len(triggered_subset) > 0 else 0
        wr = (triggered_subset["ret_10d"] > 0).mean() * 100 if len(triggered_subset) > 0 else 0
        logger.info(f"    加仓候选: {len(add_df)}只, 触发率: {trigger_rate:.1f}%")
        logger.info(f"    触发后10d收益: {avg_ret:+.2f}%, 胜率: {wr:.1f}%")
        add_result = {"candidates": len(add_df), "trigger_rate": trigger_rate,
                      "avg_ret": avg_ret, "win_rate": wr}
    else:
        add_result = {"candidates": 0}

    return {
        "comp_stats": comp_stats,
        "comp_df": comp_df,
        "rebalance": rebalance_result,
        "add_position": add_result
    }


# ============================================================
# 六、组合模拟回测(完整策略)
# ============================================================

def run_portfolio_backtest(data_dict, precomputed, use_v2=False, version="v1",
                          stop_loss_pct=None, max_positions=None, scan_interval=None):
    """
    完整组合回测: 模拟CANSLIM选股+composite评分+止损+调仓

    V2(use_v2=True): 激进改进 - V2因子+熊市闸门完全暂停+时间止损15d/3%+阶梯止盈12%/3%+动态仓位
    V3(version="v3"): 最小改进 - V1因子+V1组合规则+仅增加熊市闸门(完全暂停买入)

    参数优化(仅对version="v3"生效):
      stop_loss_pct: 止损幅度(默认config.INITIAL_STOP_LOSS_PCT=0.10)
      max_positions: 最大持仓数(默认7)
      scan_interval: 扫描间隔天数(默认SCAN_INTERVAL=5)
    """
    logger.info("\n" + "=" * 60)
    logger.info("  [C] 组合模拟回测(完整策略)")
    logger.info("=" * 60)

    candidates = [c for c in precomputed.keys()
                  if c != "000300" and not c.startswith("588") and not c.startswith("159")]

    calendar = precomputed["000300"]["date"].values
    start_mask = calendar >= START_DATE
    trade_dates = calendar[start_mask & (calendar <= END_DATE)]

    # 组合状态
    cash = INITIAL_CAPITAL
    positions = {}  # {code: {shares, buy_price, buy_date, buy_idx, hold_days}}
    trades = []
    daily_values = []
    max_value = INITIAL_CAPITAL
    max_drawdown = 0

    # V2: 市场环境跟踪
    _bear_pause_count = 0  # 熊市暂停计数

    for t_idx, date in enumerate(trade_dates):
        # 更新持仓市值
        portfolio_value = cash
        for code, pos in list(positions.items()):
            df = precomputed[code]
            mask = df["date"] == date
            if mask.any():
                price = df[mask].iloc[0]["close"]
                portfolio_value += pos["shares"] * price

        daily_values.append({"date": date, "value": portfolio_value})
        if portfolio_value > max_value:
            max_value = portfolio_value
        dd = (max_value - portfolio_value) / max_value
        if dd > max_drawdown:
            max_drawdown = dd

        # 判断当日市场环境
        idx_df = precomputed["000300"]
        idx_mask = idx_df["date"] <= date
        _regime = "RANGE"
        if idx_mask.sum() >= 60:
            _idx_row = idx_df[idx_mask].iloc[-1]
            if not pd.isna(_idx_row.get("ma20")) and not pd.isna(_idx_row.get("ma60")):
                if _idx_row["close"] > _idx_row["ma20"] > _idx_row["ma60"]:
                    _regime = "BULL"
                elif _idx_row["close"] < _idx_row["ma60"] and _idx_row["ma20"] < _idx_row["ma60"]:
                    _regime = "BEAR"

        # 每日止损/止盈/时间止损检查
        for code in list(positions.keys()):
            pos = positions[code]
            df = precomputed[code]
            mask = df["date"] == date
            if not mask.any():
                continue
            row = df[mask].iloc[0]
            price = row["close"]
            pnl_pct = (price - pos["buy_price"]) / pos["buy_price"]
            pos["hold_days"] = pos.get("hold_days", 0) + 1

            # 止损
            _sl = stop_loss_pct if stop_loss_pct is not None else config.INITIAL_STOP_LOSS_PCT
            if pnl_pct <= -_sl:
                sell_price = price * (1 - SLIPPAGE)
                revenue = sell_price * pos["shares"]
                cost = revenue * (COMMISSION_RATE + STAMP_TAX)
                cash += revenue - cost
                trades.append({
                    "code": code, "action": "sell", "date": date,
                    "price": sell_price, "shares": pos["shares"],
                    "pnl_pct": pnl_pct * 100 - 0.35, "reason": "stop_loss"
                })
                del positions[code]
                continue

            # 移动止盈
            highest = pos.get("highest", pos["buy_price"])
            pos["highest"] = max(highest, price)
            drawdown_from_high = (pos["highest"] - price) / pos["highest"]

            if use_v2:
                # V2(P1-4): 阶梯式移动止盈
                # 浮盈>12%: 回落3%止盈 | 浮盈>20%: 回落5%止盈
                if pnl_pct > 0.20 and drawdown_from_high > 0.05:
                    sell_price = price * (1 - SLIPPAGE)
                    revenue = sell_price * pos["shares"]
                    cost = revenue * (COMMISSION_RATE + STAMP_TAX)
                    cash += revenue - cost
                    trades.append({
                        "code": code, "action": "sell", "date": date,
                        "price": sell_price, "shares": pos["shares"],
                        "pnl_pct": pnl_pct * 100 - 0.35, "reason": "trailing_stop_20"
                    })
                    del positions[code]
                    continue
                elif pnl_pct > 0.12 and drawdown_from_high > 0.03:
                    sell_price = price * (1 - SLIPPAGE)
                    revenue = sell_price * pos["shares"]
                    cost = revenue * (COMMISSION_RATE + STAMP_TAX)
                    cash += revenue - cost
                    trades.append({
                        "code": code, "action": "sell", "date": date,
                        "price": sell_price, "shares": pos["shares"],
                        "pnl_pct": pnl_pct * 100 - 0.35, "reason": "trailing_stop_12"
                    })
                    del positions[code]
                    continue
            else:
                # V1: 浮盈>15%后回落6%止盈
                if pnl_pct > 0.15 and drawdown_from_high > 0.06:
                    sell_price = price * (1 - SLIPPAGE)
                    revenue = sell_price * pos["shares"]
                    cost = revenue * (COMMISSION_RATE + STAMP_TAX)
                    cash += revenue - cost
                    trades.append({
                        "code": code, "action": "sell", "date": date,
                        "price": sell_price, "shares": pos["shares"],
                        "pnl_pct": pnl_pct * 100 - 0.35, "reason": "trailing_stop"
                    })
                    del positions[code]
                    continue

            # V2(P1-2): 时间止损 - 持仓>15天且浮盈<3%则卖出(提高资金效率)
            if use_v2 and pos["hold_days"] >= 15 and pnl_pct < 0.03:
                sell_price = price * (1 - SLIPPAGE)
                revenue = sell_price * pos["shares"]
                cost = revenue * (COMMISSION_RATE + STAMP_TAX)
                cash += revenue - cost
                trades.append({
                    "code": code, "action": "sell", "date": date,
                    "price": sell_price, "shares": pos["shares"],
                    "pnl_pct": pnl_pct * 100 - 0.35, "reason": "time_stop"
                })
                del positions[code]
                continue

        # 扫描买入
        _si = scan_interval if scan_interval is not None else SCAN_INTERVAL
        if t_idx % _si != 0 or t_idx < 60:
            continue

        _mp = max_positions if max_positions is not None else 7
        if len(positions) >= _mp:  # 最大持仓
            continue

        # V2: 熊市闸门 - BEAR市场暂停买入
        if use_v2 and _regime == "BEAR":
            _bear_pause_count += 1
            continue

        # V3: 熊市闸门 - BEAR市场暂停买入(唯一与V1不同的地方)
        if version == "v3" and _regime == "BEAR":
            _bear_pause_count += 1
            continue

        # 扫描候选
        day_signals = []
        for code in candidates:
            if code in positions:
                continue
            df = precomputed[code]
            date_mask = df["date"] <= date
            if date_mask.sum() < 60:
                continue
            idx = date_mask.sum() - 1

            factors = calc_canslim_factors(df, idx, precomputed, use_v2=use_v2)
            if factors is None:
                continue

            # 买入条件
            idx_df = precomputed["000300"]
            idx_mask = idx_df["date"] <= date
            if idx_mask.sum() < 60:
                continue
            idx_row = idx_df[idx_mask].iloc[-1]
            market_up = (not pd.isna(idx_row.get("ma20")) and not pd.isna(idx_row.get("ma60")) and
                         idx_row["close"] > idx_row["ma20"] > idx_row["ma60"])

            if use_v2:
                # V2: 弱势门槛收紧 - 非强势市场>=50
                min_score = 50  # 统一门槛
            else:
                # V1/V3: 原逻辑
                min_score = 50 if market_up else 35

            if factors["total"] >= min_score:
                # 硬性筛选
                row = df.iloc[idx]
                if row["close"] < row["ma20"] * 0.95:
                    continue
                # V2 only: MA60下行不买
                if use_v2 and not pd.isna(row.get("ma60")) and idx >= 5:
                    ma60_5ago = df["ma60"].iloc[max(0, idx-5)]
                    if not pd.isna(ma60_5ago) and row["ma60"] < ma60_5ago * 0.995:
                        continue
                day_signals.append({"code": code, "score": factors["total"], "close": row["close"]})

        # 按评分排序,买入前2只
        day_signals.sort(key=lambda x: x["score"], reverse=True)
        for sig in day_signals[:2]:
            if len(positions) >= _mp:
                break
            code = sig["code"]
            buy_price = sig["close"] * (1 + SLIPPAGE)

            # V2(P1-3): 动态仓位 - 震荡市缩减仓位
            if use_v2 and _regime == "RANGE":
                position_size = INITIAL_CAPITAL / 10  # 震荡市10分仓(原7分仓)
            else:
                position_size = INITIAL_CAPITAL / 7

            shares = int(position_size / buy_price / 100) * 100
            if shares < 100:
                continue
            cost = buy_price * shares
            commission = cost * COMMISSION_RATE
            if cash < cost + commission:
                continue
            cash -= cost + commission
            positions[code] = {
                "shares": shares, "buy_price": buy_price,
                "buy_date": date, "highest": buy_price, "hold_days": 0
            }
            trades.append({
                "code": code, "action": "buy", "date": date,
                "price": buy_price, "shares": shares, "pnl_pct": 0, "reason": "canslim_buy"
            })

    # 回测结束平仓
    final_date = trade_dates[-1]
    for code in list(positions.keys()):
        df = precomputed[code]
        mask = df["date"] == final_date
        if mask.any():
            price = df[mask].iloc[0]["close"] * (1 - SLIPPAGE)
            pos = positions[code]
            pnl_pct = (price - pos["buy_price"]) / pos["buy_price"]
            revenue = price * pos["shares"]
            cost = revenue * (COMMISSION_RATE + STAMP_TAX)
            cash += revenue - cost
            trades.append({
                "code": code, "action": "sell", "date": final_date,
                "price": price, "shares": pos["shares"],
                "pnl_pct": pnl_pct * 100 - 0.35, "reason": "end"
            })
    positions.clear()

    # 绩效统计
    final_value = cash
    total_return = (final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL
    days = len(trade_dates)
    annual_return = (1 + total_return) ** (252 / max(days, 1)) - 1

    sell_trades = [t for t in trades if t["action"] == "sell"]
    total_trades = len(sell_trades)
    win_trades = len([t for t in sell_trades if t["pnl_pct"] > 0])
    win_rate = win_trades / total_trades * 100 if total_trades > 0 else 0
    avg_win = np.mean([t["pnl_pct"] for t in sell_trades if t["pnl_pct"] > 0]) if win_trades > 0 else 0
    avg_loss = abs(np.mean([t["pnl_pct"] for t in sell_trades if t["pnl_pct"] <= 0])) if (total_trades - win_trades) > 0 else 1
    # FIX: 注释澄清：此变量名为 profit_factor，但计算实为盈亏比(payoff ratio = 平均盈利/平均亏损)，
    # 而非 profit factor(总盈利/总亏损)；不重命名以避免下游连锁改动
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

    # 夏普
    dv_df = pd.DataFrame(daily_values)
    if len(dv_df) > 1:
        daily_rets = dv_df["value"].pct_change().dropna()
        sharpe = (daily_rets.mean() - 0.03/252) / daily_rets.std() * np.sqrt(252) if daily_rets.std() > 0 else 0
    else:
        sharpe = 0

    logger.info(f"  [OK] 组合回测完成:")
    logger.info(f"    最终资产: {final_value:,.0f} (初始{INITIAL_CAPITAL:,.0f})")
    logger.info(f"    总收益: {total_return*100:+.2f}%, 年化: {annual_return*100:+.2f}%")
    logger.info(f"    最大回撤: {max_drawdown*100:.2f}%")
    logger.info(f"    夏普比率: {sharpe:.2f}")
    logger.info(f"    交易次数: {total_trades}, 胜率: {win_rate:.1f}%")
    logger.info(f"    盈亏比: {profit_factor:.2f}")

    return {
        "final_value": final_value,
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "daily_values": dv_df,
        "trades": trades
    }


# ============================================================
# 七、报告生成
# ============================================================

def generate_html_report(canslim_results, composite_results, portfolio_results):
    """生成HTML回测报告"""
    today = datetime.date.today().strftime("%Y-%m-%d")

    # 评分分组表格
    score_rows = ""
    for s in canslim_results.get("score_stats", []):
        score_rows += f"<tr><td>{s['group']}</td><td>{s['count']}</td><td>{s['avg_5d']:+.2f}%</td><td>{s['avg_10d']:+.2f}%</td><td>{s['avg_20d']:+.2f}%</td><td>{s['win_rate_20d']:.1f}%</td></tr>"

    # 因子贡献表格
    factor_rows = ""
    for f in canslim_results.get("factor_stats", []):
        mark = "[OK]" if f["effective"] else "[WARN]"
        factor_rows += f"<tr><td>{f['factor']}</td><td>{f['high_20d']:+.2f}%</td><td>{f['low_20d']:+.2f}%</td><td>{f['spread']:+.2f}%</td><td>{mark}</td></tr>"

    # 阈值表格
    thresh_rows = ""
    for t in canslim_results.get("threshold_stats", []):
        thresh_rows += f"<tr><td>>={t['threshold']}</td><td>{t['count']}</td><td>{t['win_rate_20d']:.1f}%</td><td>{t['avg_20d']:+.2f}%</td></tr>"

    # Composite表格
    comp_rows = ""
    for c in composite_results.get("comp_stats", []):
        comp_rows += f"<tr><td>{c['group']}</td><td>{c['count']}</td><td>{c['avg_20d']:+.2f}%</td><td>{c['win_rate']:.1f}%</td></tr>"

    # 组合绩效
    pf = portfolio_results
    rb = composite_results.get("rebalance", {})
    sl = canslim_results.get("stop_loss", {})

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:'Microsoft YaHei',sans-serif;padding:20px;background:#f5f5f5}}
.container{{max-width:1000px;margin:0 auto}}
h1{{color:#2c3e50;border-bottom:3px solid #3498db;padding-bottom:10px}}
h2{{color:#34495e;margin-top:25px}}
table{{width:100%;border-collapse:collapse;margin:12px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
th{{background:#34495e;color:#fff;padding:10px 8px;font-size:13px}}
td{{padding:8px;text-align:center;border-bottom:1px solid #ecf0f1;font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:15px 0}}
.card{{background:#fff;border-radius:8px;padding:15px;text-align:center;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
.card .v{{font-size:22px;font-weight:bold}}
.card .l{{font-size:11px;color:#7f8c8d;margin-top:4px}}
.up{{color:#e74c3c}}.down{{color:#27ae60}}
.note{{background:#d4edda;padding:12px;border-radius:6px;margin:15px 0;font-size:13px;border-left:4px solid #28a745}}
.warn{{background:#fff3cd;padding:12px;border-radius:6px;margin:15px 0;font-size:13px;border-left:4px solid #ffc107}}
</style></head><body><div class="container">
<h1>CANSLIM选股引擎 + 综合分析报告 历史回测验证</h1>
<p>回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,.0f}元 | 成本: 万2.5+千1+滑点0.1% | 生成: {today}</p>

<div class="cards">
<div class="card"><div class="v {'up' if pf['total_return']>0 else 'down'}">{pf['total_return']*100:+.1f}%</div><div class="l">总收益率</div></div>
<div class="card"><div class="v">{pf['annual_return']*100:+.1f}%</div><div class="l">年化收益率</div></div>
<div class="card"><div class="v down">-{pf['max_drawdown']*100:.1f}%</div><div class="l">最大回撤</div></div>
<div class="card"><div class="v">{pf['sharpe']:.2f}</div><div class="l">夏普比率</div></div>
<div class="card"><div class="v">{pf['win_rate']:.1f}%</div><div class="l">胜率</div></div>
<div class="card"><div class="v">{pf['profit_factor']:.2f}</div><div class="l">盈亏比</div></div>
<div class="card"><div class="v">{pf['total_trades']}</div><div class="l">交易次数</div></div>
<div class="card"><div class="v">{pf['avg_win']:.1f}%/{pf['avg_loss']:.1f}%</div><div class="l">平均盈/亏</div></div>
</div>

<h2>A1: CANSLIM评分分组前瞻收益</h2>
<table><tr><th>评分组</th><th>信号数</th><th>5日收益</th><th>10日收益</th><th>20日收益</th><th>20日胜率</th></tr>{score_rows}</table>

<h2>A2: 六因子贡献分析</h2>
<table><tr><th>因子</th><th>高分组20d</th><th>低分组20d</th><th>差值</th><th>有效性</th></tr>{factor_rows}</table>

<h2>A3: 买入阈值验证</h2>
<table><tr><th>阈值</th><th>信号数</th><th>20日胜率</th><th>20日均值</th></tr>{thresh_rows}</table>

<h2>A4: 止损保护效果</h2>
<div class="{'note' if sl.get('save_rate',0)>50 else 'warn'}">
止损触发率: {sl.get('trigger_rate',0):.1f}% | 止损后继续下跌(有效保护): {sl.get('save_rate',0):.1f}%
</div>

<h2>B1: Composite评分预测力</h2>
<table><tr><th>Composite</th><th>样本数</th><th>20日收益</th><th>胜率</th></tr>{comp_rows}</table>

<h2>B2: 调仓逻辑验证</h2>
<div class="{'note' if rb.get('benefit',0)>0 else 'warn'}">
调仓事件: {rb.get('events',0)}次 | 卖出后20d: {rb.get('sell_avg_20d',0):+.2f}% | 买入后20d: {rb.get('buy_avg_20d',0):+.2f}% | 收益差: {rb.get('benefit',0):+.2f}%
</div>

</div></body></html>"""
    return html


# ============================================================
# 八、主函数
# ============================================================

def generate_comparison_html(v1_cs, v2_cs, v1_comp, v2_comp, v1_pf, v2_pf):
    """生成V1 vs V2对比HTML报告"""
    today = datetime.date.today().strftime("%Y-%m-%d")

    def _card(label, v1_val, v2_val, fmt="+.1f", suffix="%", higher_better=True):
        """生成对比卡片"""
        diff = v2_val - v1_val
        color = "#e74c3c" if (diff > 0) == higher_better else "#27ae60"
        return (f'<div class="card"><div class="l">{label}</div>'
                f'<div class="v">{v1_val:{fmt}}{suffix} → <span style="color:{color}">{v2_val:{fmt}}{suffix}</span></div>'
                f'<div class="l" style="color:{color}">{diff:+.1f}{suffix}</div></div>')

    cards = ""
    cards += _card("总收益", v1_pf["total_return"]*100, v2_pf["total_return"]*100)
    cards += _card("年化收益", v1_pf["annual_return"]*100, v2_pf["annual_return"]*100)
    cards += _card("最大回撤", v1_pf["max_drawdown"]*100, v2_pf["max_drawdown"]*100, higher_better=False)
    cards += _card("夏普比率", v1_pf["sharpe"], v2_pf["sharpe"], fmt=".2f", suffix="")
    cards += _card("胜率", v1_pf["win_rate"], v2_pf["win_rate"], fmt=".1f")
    cards += _card("盈亏比", v1_pf["profit_factor"], v2_pf["profit_factor"], fmt=".2f", suffix="")
    cards += _card("交易次数", v1_pf["total_trades"], v2_pf["total_trades"], fmt=".0f", suffix="", higher_better=False)
    # 信号数
    _v1_sig = len(v1_cs.get("sig_df", [])) if "sig_df" in v1_cs else 0
    _v2_sig = len(v2_cs.get("sig_df", [])) if "sig_df" in v2_cs else 0
    cards += _card("信号数", _v1_sig, _v2_sig, fmt=".0f", suffix="", higher_better=False)

    # 评分分组对比表
    score_rows = ""
    v1_scores = {s["group"]: s for s in v1_cs.get("score_stats", [])}
    v2_scores = {s["group"]: s for s in v2_cs.get("score_stats", [])}
    for grp in [">=70", "50-69", "35-49", "<35"]:
        s1 = v1_scores.get(grp, {})
        s2 = v2_scores.get(grp, {})
        if s1 or s2:
            n1 = s1.get("count", 0)
            n2 = s2.get("count", 0)
            r5_1 = s1.get("avg_5d", 0)
            r5_2 = s2.get("avg_5d", 0)
            r20_1 = s1.get("avg_20d", 0)
            r20_2 = s2.get("avg_20d", 0)
            wr1 = s1.get("win_rate_20d", 0)
            wr2 = s2.get("win_rate_20d", 0)
            score_rows += (f"<tr><td>{grp}</td>"
                          f"<td>{n1}</td><td>{r5_1:+.2f}%</td><td>{r20_1:+.2f}%</td><td>{wr1:.1f}%</td>"
                          f"<td>{n2}</td><td>{r5_2:+.2f}%</td><td>{r20_2:+.2f}%</td><td>{wr2:.1f}%</td></tr>")

    # 因子对比表
    factor_rows = ""
    v1_factors = {f["factor"]: f for f in v1_cs.get("factor_stats", [])}
    v2_factors = {f["factor"]: f for f in v2_cs.get("factor_stats", [])}
    for fname in ["N", "S", "L", "CAI", "P", "W"]:
        f1 = v1_factors.get(fname, {})
        f2 = v2_factors.get(fname, {})
        sp1 = f1.get("spread", 0)
        sp2 = f2.get("spread", 0)
        m1 = "[OK]" if f1.get("effective") else "[WARN]"
        m2 = "[OK]" if f2.get("effective") else "[WARN]"
        factor_rows += (f"<tr><td>{fname}</td>"
                       f"<td>{f1.get('high_20d',0):+.2f}%</td><td>{f1.get('low_20d',0):+.2f}%</td><td>{sp1:+.2f}%</td><td>{m1}</td>"
                       f"<td>{f2.get('high_20d',0):+.2f}%</td><td>{f2.get('low_20d',0):+.2f}%</td><td>{sp2:+.2f}%</td><td>{m2}</td></tr>")

    # Composite对比表
    comp_rows = ""
    v1_comps = {s["group"]: s for s in v1_comp.get("comp_stats", [])}
    v2_comps = {s["group"]: s for s in v2_comp.get("comp_stats", [])}
    for grp in [">=70", "55-69", "45-54", "30-44", "<30"]:
        s1 = v1_comps.get(grp, {})
        s2 = v2_comps.get(grp, {})
        if s1 or s2:
            comp_rows += (f"<tr><td>{grp}</td>"
                         f"<td>{s1.get('count',0)}</td><td>{s1.get('avg_20d',0):+.2f}%</td><td>{s1.get('win_rate',0):.1f}%</td>"
                         f"<td>{s2.get('count',0)}</td><td>{s2.get('avg_20d',0):+.2f}%</td><td>{s2.get('win_rate',0):.1f}%</td></tr>")

    # 交易原因统计
    v1_trades = v1_pf.get("trades", [])
    v2_trades = v2_pf.get("trades", [])
    v1_sell = [t for t in v1_trades if t["action"] == "sell"]
    v2_sell = [t for t in v2_trades if t["action"] == "sell"]
    v1_reasons = {}
    v2_reasons = {}
    for t in v1_sell:
        r = t.get("reason", "other")
        v1_reasons[r] = v1_reasons.get(r, 0) + 1
    for t in v2_sell:
        r = t.get("reason", "other")
        v2_reasons[r] = v2_reasons.get(r, 0) + 1
    reason_rows = ""
    all_reasons = sorted(set(list(v1_reasons.keys()) + list(v2_reasons.keys())))
    for r in all_reasons:
        reason_rows += f"<tr><td>{r}</td><td>{v1_reasons.get(r,0)}</td><td>{v2_reasons.get(r,0)}</td></tr>"

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:'Microsoft YaHei',sans-serif;padding:20px;background:#f5f5f5}}
.container{{max-width:1100px;margin:0 auto}}
h1{{color:#2c3e50;border-bottom:3px solid #3498db;padding-bottom:10px}}
h2{{color:#34495e;margin-top:25px}}
table{{width:100%;border-collapse:collapse;margin:12px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
th{{background:#34495e;color:#fff;padding:10px 8px;font-size:13px}}
td{{padding:8px;text-align:center;border-bottom:1px solid #ecf0f1;font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:15px 0}}
.card{{background:#fff;border-radius:8px;padding:15px;text-align:center;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
.card .v{{font-size:18px;font-weight:bold}}
.card .l{{font-size:11px;color:#7f8c8d;margin-top:4px}}
.up{{color:#e74c3c}}.down{{color:#27ae60}}
.note{{background:#d4edda;padding:12px;border-radius:6px;margin:15px 0;font-size:13px;border-left:4px solid #28a745}}
.warn{{background:#fff3cd;padding:12px;border-radius:6px;margin:15px 0;font-size:13px;border-left:4px solid #ffc107}}
.improve{{background:#e8f5e9;padding:12px;border-radius:6px;margin:15px 0;font-size:13px;border-left:4px solid #4caf50}}
</style></head><body><div class="container">
<h1>CANSLIM选股引擎 V1 vs V2 改进对比报告</h1>
<p>回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,.0f}元 | 生成: {today}</p>

<div class="improve">
<b>V2改进清单:</b><br>
[P0-1] P因子动量化: RSI 30-50超卖不再奖励 → RSI 50-70动量区间给高分 + MACD金叉确认<br>
[P0-2] CAI动态化: 固定8分 → 20日动量代理(4-10分) 对齐实盘<br>
[P0-3] 熊市闸门: BEAR市场暂停买入<br>
[P1-1] 弱势门槛: score>=35 → >=50统一门槛 + MA60下行不买<br>
[P1-2] 时间止损: 持仓>15天且浮盈<3%自动卖出<br>
[P1-3] 动态仓位: 震荡市10分仓(原7分仓)<br>
[P1-4] 阶梯止盈: 12%回落3%止盈 + 20%回落5%止盈<br>
[P2-1] N因子增强: 加120日新高维度<br>
[P2-2] S因子对齐: 满分20→10 对齐实盘<br>
[P2-3] Composite增强: 加量价维度
</div>

<h2>组合绩效对比</h2>
<div class="cards">{cards}</div>

<h2>A1: 评分分组前瞻收益对比</h2>
<table><tr><th rowspan="2">评分组</th><th colspan="4">V1(基线)</th><th colspan="4">V2(改进)</th></tr>
<tr><th>N</th><th>5d</th><th>20d</th><th>WR20</th><th>N</th><th>5d</th><th>20d</th><th>WR20</th></tr>{score_rows}</table>

<h2>A2: 因子贡献对比</h2>
<table><tr><th rowspan="2">因子</th><th colspan="4">V1(基线)</th><th colspan="4">V2(改进)</th></tr>
<tr><th>高分20d</th><th>低分20d</th><th>差值</th><th>有效</th><th>高分20d</th><th>低分20d</th><th>差值</th><th>有效</th></tr>{factor_rows}</table>

<h2>B1: Composite评分预测力对比</h2>
<table><tr><th rowspan="2">Composite</th><th colspan="3">V1(基线)</th><th colspan="3">V2(改进)</th></tr>
<tr><th>N</th><th>20d</th><th>WR</th><th>N</th><th>20d</th><th>WR</th></tr>{comp_rows}</table>

<h2>卖出原因分布</h2>
<table><tr><th>卖出原因</th><th>V1次数</th><th>V2次数</th></tr>{reason_rows}</table>

</div></body></html>"""
    return html


def run():
    total_start = time.time()
    print("=" * 78)
    print("  CANSLIM + 综合分析报告 历史回测验证 V1 vs V2 对比")
    print(f"  区间: {START_DATE} ~ {END_DATE} | 资金: {INITIAL_CAPITAL:,.0f}")
    print("=" * 78)

    logger.info(
        "回测口径声明：本回测按信号日收盘价(+滑点)买入；"
        "V2改进: P因子动量化/CAI动态化/熊市闸门/时间止损/阶梯止盈/动态仓位"
    )

    # 1. 加载数据
    data_dict = load_data()
    if len(data_dict) < 10:
        print("[FAIL] 数据不足")
        return

    # ===== V1 基线回测 =====
    logger.info("\n" + "=" * 60)
    logger.info("  V1 基线回测")
    logger.info("=" * 60)
    canslim_v1 = run_canslim_backtest(data_dict, use_v2=False)
    precomputed = canslim_v1.get("precomputed", {})
    composite_v1 = run_composite_backtest(data_dict, precomputed, use_v2=False)
    portfolio_v1 = run_portfolio_backtest(data_dict, precomputed, use_v2=False)

    # ===== V2 改进后回测 =====
    logger.info("\n" + "=" * 60)
    logger.info("  V2 改进后回测")
    logger.info("=" * 60)
    canslim_v2 = run_canslim_backtest(data_dict, use_v2=True)
    composite_v2 = run_composite_backtest(data_dict, precomputed, use_v2=True)
    portfolio_v2 = run_portfolio_backtest(data_dict, precomputed, use_v2=True)

    # ===== V3 温和改进回测(V2因子 + 温和组合规则) =====
    logger.info("\n" + "=" * 60)
    logger.info("  V3 最小改进回测(V1因子+熊市闸门)")
    logger.info("=" * 60)
    portfolio_v3 = run_portfolio_backtest(data_dict, precomputed, version="v3")

    # ===== 生成V1 vs V2对比报告(V2因子指标=V3因子指标) =====
    html = generate_comparison_html(
        canslim_v1, canslim_v2,
        composite_v1, composite_v2,
        portfolio_v1, portfolio_v2
    )
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    report_path = os.path.join(config.OUTPUT_DIR, f"canslim_backtest_v2_{datetime.date.today().strftime('%Y%m%d')}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    elapsed = time.time() - total_start
    print(f"\n{'=' * 78}")
    print(f"  [OK] V1/V2/V3 对比回测完成! 耗时: {elapsed:.1f}秒")
    print(f"  HTML报告(V1 vs V2因子): {report_path}")

    # ===== 三版本组合绩效对比 =====
    print(f"\n{'━' * 78}")
    print(f"  组合绩效三版本对比")
    print(f"{'━' * 78}")
    print(f"  {'指标':<16} {'V1(基线)':>14} {'V2(V2因子+激进组合)':>20} {'V3(V1因子+熊市闸门)':>20}")
    print(f"  {'-' * 72}")
    for label, key, fmt, suffix in [
        ("总收益", "total_return", "+.2f", "%"),
        ("年化收益", "annual_return", "+.2f", "%"),
        ("最大回撤", "max_drawdown", ".2f", "%"),
        ("夏普比率", "sharpe", ".2f", ""),
        ("胜率", "win_rate", ".1f", "%"),
        ("盈亏比", "profit_factor", ".2f", ""),
        ("交易次数", "total_trades", ".0f", ""),
    ]:
        v1v = portfolio_v1[key]
        v2v = portfolio_v2[key]
        v3v = portfolio_v3[key]
        if key in ("total_return", "annual_return", "max_drawdown", "win_rate"):
            v1s = f"{v1v*100:{fmt}}{suffix}"
            v2s = f"{v2v*100:{fmt}}{suffix}"
            v3s = f"{v3v*100:{fmt}}{suffix}"
        else:
            v1s = f"{v1v:{fmt}}{suffix}"
            v2s = f"{v2v:{fmt}}{suffix}"
            v3s = f"{v3v:{fmt}}{suffix}"
        print(f"  {label:<16} {v1s:>14} {v2s:>20} {v3s:>20}")

    # V3卖出原因分析
    v3_trades = portfolio_v3.get("trades", [])
    v3_sells = [t for t in v3_trades if t["action"] == "sell"]
    v3_reasons = {}
    for t in v3_sells:
        r = t.get("reason", "other")
        v3_reasons[r] = v3_reasons.get(r, 0) + 1
    print(f"\n  V3卖出原因: {v3_reasons}")

    # ===== V4 参数优化扫描(V3基础上测试止损/仓位/频率组合) =====
    print(f"\n{'━' * 78}")
    print(f"  V4 参数优化扫描(V3基础上调整止损/仓位/频率)")
    print(f"{'━' * 78}")

    param_grid = [
        # (stop_loss, max_pos, scan_interval, label)
        (0.10, 7, 5, "V3基线(SL10%/P7/F5)"),
        (0.12, 7, 5, "SL12%"),
        (0.15, 7, 5, "SL15%"),
        (0.10, 5, 5, "P5"),
        (0.10, 7, 3, "F3"),
        (0.12, 5, 5, "SL12%+P5"),
        (0.12, 7, 3, "SL12%+F3"),
        (0.10, 5, 3, "P5+F3"),
        (0.12, 5, 3, "SL12%+P5+F3"),
        (0.15, 5, 3, "SL15%+P5+F3"),
    ]

    sweep_results = []
    _prev_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.WARNING)  # 扫描期间静默
    for sl, mp, si, label in param_grid:
        pf = run_portfolio_backtest(
            data_dict, precomputed, version="v3",
            stop_loss_pct=sl, max_positions=mp, scan_interval=si
        )
        sweep_results.append({
            "label": label, "sl": sl, "mp": mp, "si": si,
            "total_return": pf["total_return"],
            "annual_return": pf["annual_return"],
            "max_drawdown": pf["max_drawdown"],
            "sharpe": pf["sharpe"],
            "win_rate": pf["win_rate"],
            "profit_factor": pf["profit_factor"],
            "total_trades": pf["total_trades"],
        })
    logging.getLogger().setLevel(_prev_level)  # 恢复日志级别

    # 打印参数扫描结果表
    print(f"\n  {'参数组合':<24} {'总收益':>8} {'年化':>8} {'回撤':>8} {'夏普':>6} {'胜率':>7} {'盈亏比':>6} {'交易':>5}")
    print(f"  {'-' * 76}")
    best_idx = 0
    best_sharpe = -999
    for i, r in enumerate(sweep_results):
        ret_s = f"{r['total_return']*100:+.2f}%"
        ann_s = f"{r['annual_return']*100:+.2f}%"
        dd_s = f"{r['max_drawdown']*100:.2f}%"
        sh_s = f"{r['sharpe']:.2f}"
        wr_s = f"{r['win_rate']:.1f}%"
        pf_s = f"{r['profit_factor']:.2f}"
        tr_s = f"{r['total_trades']:.0f}"
        marker = " ★" if r['sharpe'] > best_sharpe else ""
        if r['sharpe'] > best_sharpe:
            best_sharpe = r['sharpe']
            best_idx = i
        print(f"  {r['label']:<24} {ret_s:>8} {ann_s:>8} {dd_s:>8} {sh_s:>6} {wr_s:>7} {pf_s:>6} {tr_s:>5}{marker}")

    best = sweep_results[best_idx]
    print(f"\n  ★ 最优参数组合: {best['label']}")
    print(f"    止损={best['sl']*100:.0f}% 最大持仓={best['mp']}只 扫描频率={best['si']}天")
    print(f"    总收益={best['total_return']*100:+.2f}% 夏普={best['sharpe']:.2f} 回撤={best['max_drawdown']*100:.2f}%")

    # 保存完整结果JSON
    def _to_native(obj):
        if isinstance(obj, dict):
            return {k: _to_native(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [_to_native(v) for v in obj]
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    import json
    v3_json_path = os.path.join(config.OUTPUT_DIR, "canslim_backtest_v3_summary.json")
    with open(v3_json_path, "w", encoding="utf-8") as f:
        json.dump(_to_native({
            "v1": {k: portfolio_v1[k] for k in ["total_return","annual_return","max_drawdown","sharpe","win_rate","profit_factor","total_trades","avg_win","avg_loss"]},
            "v2": {k: portfolio_v2[k] for k in ["total_return","annual_return","max_drawdown","sharpe","win_rate","profit_factor","total_trades","avg_win","avg_loss"]},
            "v3": {k: portfolio_v3[k] for k in ["total_return","annual_return","max_drawdown","sharpe","win_rate","profit_factor","total_trades","avg_win","avg_loss"]},
            "v3_sell_reasons": v3_reasons,
            "v4_sweep": sweep_results,
            "v4_best": best,
        }), f, ensure_ascii=False, indent=2)
    print(f"  V3 JSON: {v3_json_path}")
    print(f"{'=' * 78}")

    return {
        "v1": {"canslim": canslim_v1, "composite": composite_v1, "portfolio": portfolio_v1},
        "v2": {"canslim": canslim_v2, "composite": composite_v2, "portfolio": portfolio_v2},
        "v3": {"portfolio": portfolio_v3},
        "report_path": report_path
    }


if __name__ == "__main__":
    run()
