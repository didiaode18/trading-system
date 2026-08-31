# -*- coding: utf-8 -*-
"""
L3 策略级历史回测
================
多策略横向对比: CANSLIM V6.0 / CANSLIM V5.2 / MA20趋势 / MA20+MA60双趋势 / 组合策略
复用: v52_v60_backtest_compare.py 数据加载 + backtest/metrics.py 绩效指标

运行: python scripts/run_strategy_backtest.py
预估耗时: 3~10分钟
"""
import sys, os, time, sqlite3, datetime, warnings, json
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'trading_system'))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import config
from backtest.metrics import (
    calc_annual_return, calc_max_drawdown, calc_sharpe_ratio,
    calc_sortino_ratio, calc_calmar_ratio, calc_win_rate,
    calc_profit_factor, calc_expectancy, calc_cvar, calc_alpha_beta
)

# ============================================================
# 配置
# ============================================================
START_DATE = "2023-01-01"
END_DATE = "2026-08-20"
SCAN_INTERVAL = 5
INITIAL_CAPITAL = getattr(config, 'TOTAL_CAPITAL', 730000) or 730000
COMMISSION_RATE = 0.00025  # 佣金万2.5
STAMP_TAX = 0.001  # 印花税千1(卖出)
SLIPPAGE = 0.001  # 滑点0.1%

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

# ============================================================
# 1. 数据加载(复用v52_v60模式)
# ============================================================
def load_all_data():
    """从SQLite加载全部历史K线"""
    db_path = config.DB_PATH
    if not os.path.exists(db_path):
        print(f"[FAIL] 数据库不存在: {db_path}")
        return {}, None
    conn = sqlite3.connect(db_path)
    q = "SELECT code, date, open, close, high, low, volume, amount FROM daily_kline ORDER BY code, date"
    df_all = pd.read_sql(q, conn)
    conn.close()
    data_dict = {}
    for code, grp in df_all.groupby("code"):
        df = grp.copy()
        for c in ["open", "close", "high", "low", "volume", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        if len(df) > 100:
            data_dict[code] = df
    print(f"[OK] 加载 {len(data_dict)} 只股票")
    return data_dict, df_all['date'].max()

def compute_indicators(df):
    """计算技术指标"""
    df = df.copy()
    for w in [5, 10, 20, 60]:
        df[f"ma{w}"] = df["close"].rolling(w).mean()
    df["ma20_slope"] = df["ma20"].diff(3)
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_dif"] = ema12 - ema26
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["pct_change"] = df["close"].pct_change()
    return df

def judge_market(idx_df, scan_date):
    """判断市场环境"""
    mask = idx_df["date"] <= scan_date
    if mask.sum() < 60:
        return None
    row = idx_df[mask].iloc[-1]
    close, ma20, ma60 = row["close"], row.get("ma20", np.nan), row.get("ma60", np.nan)
    if pd.isna(ma20) or pd.isna(ma60):
        return None
    if close > ma20 > ma60:
        state = "up"
    elif close < ma20 < ma60:
        state = "down"
    elif close > ma20:
        state = "neutral"
    else:
        state = "neutral_weak"
    breadth = 50.0
    if close > ma20:
        breadth = 55 + min(20, (close / ma20 - 1) * 200)
    else:
        breadth = 45 - min(20, (1 - close / ma20) * 200)
    breadth = max(10, min(90, breadth))
    return {"state": state, "close": close, "ma20": ma20, "ma60": ma60,
            "breadth": breadth, "is_weak": state in ("down", "neutral", "neutral_weak")}

# ============================================================
# 2. 策略实现
# ============================================================

# --- 策略S1: CANSLIM V6.0 ---
def strategy_canslim_v60(data_dict, precomputed, idx_df, trade_dates, candidates):
    """CANSLIM V6.0选股策略"""
    signals = []
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        mkt = judge_market(idx_df, scan_date)
        if mkt is None:
            continue
        for code in candidates:
            df = precomputed[code]
            date_mask = df["date"] <= scan_date
            if date_mask.sum() < 60:
                continue
            idx = date_mask.sum() - 1
            # V6.0硬筛
            passed, _ = v60_hard_filter(df, idx, mkt)
            if not passed:
                continue
            sc = v60_score(df, idx, mkt)
            if sc is None or sc["total"] < 28:
                continue
            # 前瞻收益
            fwd = {}
            for days in [5, 10, 20]:
                if idx + days < len(df):
                    fwd[days] = (df["close"].iloc[idx + days] - df["close"].iloc[idx]) / df["close"].iloc[idx] * 100
                else:
                    fwd[days] = np.nan
            signals.append({"date": scan_date, "code": code, "score": sc["total"],
                            "market_state": mkt["state"], "fwd": fwd})
    return signals

# --- 策略S2: CANSLIM V5.2 ---
def strategy_canslim_v52(data_dict, precomputed, idx_df, trade_dates, candidates):
    """CANSLIM V5.2选股策略"""
    signals = []
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        mkt = judge_market(idx_df, scan_date)
        if mkt is None:
            continue
        for code in candidates:
            df = precomputed[code]
            date_mask = df["date"] <= scan_date
            if date_mask.sum() < 60:
                continue
            idx = date_mask.sum() - 1
            passed, _ = v52_hard_filter(df, idx, mkt)
            if not passed:
                continue
            sc = v52_score(df, idx, mkt)
            if sc is None or sc["total"] < 35:
                continue
            fwd = {}
            for days in [5, 10, 20]:
                if idx + days < len(df):
                    fwd[days] = (df["close"].iloc[idx + days] - df["close"].iloc[idx]) / df["close"].iloc[idx] * 100
                else:
                    fwd[days] = np.nan
            signals.append({"date": scan_date, "code": code, "score": sc["total"],
                            "market_state": mkt["state"], "fwd": fwd})
    return signals

# --- 策略S3: MA20趋势跟踪 ---
def strategy_ma20_trend(data_dict, precomputed, idx_df, trade_dates, candidates):
    """MA20金叉买入/死叉卖出"""
    trades = []
    for code in candidates:
        df = precomputed[code]
        mask = (df["date"] >= START_DATE) & (df["date"] <= END_DATE)
        active = df[mask].reset_index(drop=True)
        if len(active) < 60:
            continue
        position = None
        for i in range(1, len(active)):
            date = active["date"].iloc[i]
            close = active["close"].iloc[i]
            ma20 = active["ma20"].iloc[i]
            ma20_prev = active["ma20"].iloc[i - 1]
            if pd.isna(ma20) or pd.isna(ma20_prev):
                continue
            # 金叉: close上穿MA20
            if position is None and close > ma20 and active["close"].iloc[i - 1] <= ma20_prev:
                position = {"entry_date": date, "entry_price": close * (1 + SLIPPAGE), "code": code}
            # 死叉: close下穿MA20
            elif position is not None and close < ma20 and active["close"].iloc[i - 1] >= ma20_prev:
                exit_price = close * (1 - SLIPPAGE)
                pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
                pnl_pct -= COMMISSION_RATE * 2 * 100 + STAMP_TAX * 100  # 扣除成本
                trades.append({**position, "exit_date": date, "exit_price": exit_price,
                               "pnl_pct": pnl_pct, "strategy": "MA20"})
                position = None
    return trades

# --- 策略S4: MA20+MA60双趋势 ---
def strategy_dual_ma(data_dict, precomputed, idx_df, trade_dates, candidates):
    """MA20>MA60且close>MA20买入, close<MA60卖出"""
    trades = []
    for code in candidates:
        df = precomputed[code]
        mask = (df["date"] >= START_DATE) & (df["date"] <= END_DATE)
        active = df[mask].reset_index(drop=True)
        if len(active) < 60:
            continue
        position = None
        for i in range(1, len(active)):
            date = active["date"].iloc[i]
            close = active["close"].iloc[i]
            ma20 = active["ma20"].iloc[i]
            ma60 = active["ma60"].iloc[i]
            if pd.isna(ma20) or pd.isna(ma60):
                continue
            # 买入: MA20>MA60 且 close上穿MA20
            if position is None and ma20 > ma60 and close > ma20:
                prev_close = active["close"].iloc[i - 1]
                prev_ma20 = active["ma20"].iloc[i - 1]
                if not pd.isna(prev_ma20) and prev_close <= prev_ma20:
                    position = {"entry_date": date, "entry_price": close * (1 + SLIPPAGE), "code": code}
            # 卖出: close<MA60
            elif position is not None and close < ma60:
                exit_price = close * (1 - SLIPPAGE)
                pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
                pnl_pct -= COMMISSION_RATE * 2 * 100 + STAMP_TAX * 100
                trades.append({**position, "exit_date": date, "exit_price": exit_price,
                               "pnl_pct": pnl_pct, "strategy": "DualMA"})
                position = None
    return trades

# --- 策略S5: 组合策略(CANSLIM选股+趋势确认) ---
def strategy_combined(data_dict, precomputed, idx_df, trade_dates, candidates):
    """CANSLIM V6.0选股 + MA20趋势确认"""
    signals = strategy_canslim_v60(data_dict, precomputed, idx_df, trade_dates, candidates)
    # 只保留MA20>MA60的信号(趋势确认)
    filtered = []
    for sig in signals:
        code = sig["code"]
        df = precomputed[code]
        date_mask = df["date"] <= sig["date"]
        if date_mask.sum() < 60:
            continue
        idx = date_mask.sum() - 1
        ma20 = df["ma20"].iloc[idx]
        ma60 = df["ma60"].iloc[idx]
        if pd.isna(ma20) or pd.isna(ma60):
            continue
        if ma20 > ma60:  # 趋势确认
            filtered.append(sig)
    return filtered

# ============================================================
# 3. V5.2/V6.0筛选/打分(从v52_v60_backtest_compare.py简化复用)
# ============================================================
def v52_hard_filter(df, idx, market_info):
    row = df.iloc[idx]
    close, ma20, ma20_slope = row["close"], row["ma20"], row["ma20_slope"]
    if pd.isna(ma20) or pd.isna(ma20_slope):
        return False, "均线不足"
    if market_info["state"] == "down":
        return False, "M=down禁止"
    if idx >= 20:
        high_20 = df["high"].iloc[idx-20:idx+1].max()
        dd = close / high_20 - 1 if high_20 > 0 else 0
        if dd < -0.12:
            return False, f"深跌{dd:.1%}"
    if idx >= 61:
        chg60 = close / df["close"].iloc[idx-60] - 1
        if chg60 < -0.20:
            return False, f"60日跌{chg60:.1%}"
    return True, "通过"

def v52_score(df, idx, market_info):
    row = df.iloc[idx]
    close, ma20, ma60 = row["close"], row["ma20"], row["ma60"]
    vol, vol_ma = row["volume"], row["vol_ma20"]
    if pd.isna(ma20) or pd.isna(ma60):
        return None
    factors = {}
    n = 0
    if idx >= 60:
        h60 = df["high"].iloc[idx-60:idx+1].max()
        if close >= h60 * 0.98: n += 12
        if close > ma20 > ma60: n += 8
    factors["N"] = min(n, 20)
    s = 0
    if not pd.isna(vol_ma) and vol_ma > 0:
        vr = vol / vol_ma
        if vr < 0.7 and close >= ma20 * 0.99: s += 6
        if vr > 1.5 and close > df["close"].iloc[max(0, idx-1)]: s += 4
    factors["S"] = min(s, 10)
    l = 0
    if idx >= 20:
        c20 = (close / df["close"].iloc[idx-20] - 1) * 100
        if c20 > 10: l += 12
        elif c20 > 5: l += 8
        elif c20 > 0: l += 4
    if close > ma20: l += 5
    if close > ma60: l += 3
    factors["L"] = min(l, 20)
    factors["CAI"] = 10
    p = 0
    ms = row["ma20_slope"]
    if not pd.isna(ms) and ms > 0: p += 6
    if not pd.isna(row.get("rsi")):
        rsi = row["rsi"]
        if 30 < rsi < 50: p += 8
        elif 50 <= rsi < 70: p += 5
    if not pd.isna(ma20) and row["low"] <= ma20 * 1.01 and close >= ma20: p += 6
    factors["P"] = min(p, 20)
    factors["W"] = 5 if market_info["state"] == "up" else (-5 if market_info["state"] == "down" else 0)
    return {"factors": factors, "total": sum(factors.values())}

def v60_hard_filter(df, idx, market_info):
    row = df.iloc[idx]
    close, ma20, ma20_slope = row["close"], row["ma20"], row["ma20_slope"]
    if pd.isna(ma20) or pd.isna(ma20_slope):
        return False, "均线不足"
    is_weak = market_info["is_weak"]
    max_dd = -0.18 if is_weak else -0.12
    if idx >= 20:
        high_20 = df["high"].iloc[idx-20:idx+1].max()
        dd = close / high_20 - 1 if high_20 > 0 else 0
        if dd < max_dd:
            return False, f"深跌{dd:.1%}"
    if idx >= 61:
        chg60 = close / df["close"].iloc[idx-60] - 1
        max_60d = -0.28 if is_weak else -0.20
        if chg60 < max_60d:
            return False, f"60日跌{chg60:.1%}"
    if is_weak:
        ws = 0
        if close > ma20: ws += 30
        elif (close - ma20) / ma20 > -0.05: ws += 20
        elif (close - ma20) / ma20 > -0.10: ws += 10
        if ma20_slope > 0: ws += 15
        if ws < 30:
            return False, f"弱势评分{ws}<30"
    return True, "通过"

def v60_score(df, idx, market_info):
    row = df.iloc[idx]
    close, ma20, ma60 = row["close"], row["ma20"], row["ma60"]
    vol, vol_ma = row["volume"], row["vol_ma20"]
    if pd.isna(ma20) or pd.isna(ma60):
        return None
    factors = {}
    n = 0
    if idx >= 60:
        h60 = df["high"].iloc[idx-60:idx+1].max()
        if close >= h60 * 0.98: n += 12
        if close > ma20 > ma60: n += 8
    factors["N"] = min(n, 20)
    s = 0
    if not pd.isna(vol_ma) and vol_ma > 0:
        vr = vol / vol_ma
        if vr < 0.7 and close >= ma20 * 0.99: s += 6
        if vr > 1.5 and close > df["close"].iloc[max(0, idx-1)]: s += 4
    factors["S"] = min(s, 10)
    l = 0
    if idx >= 20:
        c20 = (close / df["close"].iloc[idx-20] - 1) * 100
        if c20 > 10: l += 12
        elif c20 > 5: l += 8
        elif c20 > 0: l += 4
    if close > ma20: l += 5
    if close > ma60: l += 3
    factors["L"] = min(l, 20)
    cai = 0
    if idx >= 21:
        mom_chg = (close / df["close"].iloc[idx-21] - 1) * 100
        mom_s = max(0, min(20, 10 + mom_chg * 0.5))
    else:
        mom_s = 10
    vol_std = df["close"].pct_change().iloc[max(0,idx-20):idx+1].std() * 100 if idx >= 20 else 3
    vol_s = max(0, min(20, 15 - vol_std * 2))
    cai = round(mom_s * 0.4 + vol_s * 0.3 + 10 * 0.3)
    factors["CAI"] = max(2, min(18, cai))
    p = 0
    ms = row["ma20_slope"]
    if not pd.isna(ms) and ms > 0: p += 6
    if not pd.isna(row.get("rsi")):
        rsi = row["rsi"]
        if 50 <= rsi < 70: p += 8
        elif 40 <= rsi < 50: p += 4
    if not pd.isna(row.get("macd_dif")) and not pd.isna(row.get("macd_dea")):
        if row["macd_dif"] > row["macd_dea"]: p += 6
    factors["P"] = min(p, 20)
    factors["W"] = 5 if market_info["state"] == "up" else (-5 if market_info["state"] == "down" else 0)
    v = 0
    if idx >= 60:
        c60 = (close / df["close"].iloc[idx-60] - 1) * 100
        if c60 < -10: v = 4
        elif c60 < 0: v = 3
        elif c60 < 10: v = 2
        elif c60 < 30: v = 1
    factors["V"] = min(v, 5)
    return {"factors": factors, "total": sum(factors.values())}

# ============================================================
# 4. 信号→模拟交易(选股策略专用)
# ============================================================
def signals_to_trades(signals, precomputed, hold_days=20):
    """将选股信号转为模拟交易(固定持有N天后卖出)"""
    trades = []
    for sig in signals:
        code = sig["code"]
        df = precomputed[code]
        date_mask = df["date"] <= sig["date"]
        if date_mask.sum() < 1:
            continue
        idx = date_mask.sum() - 1
        entry_price = df["close"].iloc[idx] * (1 + SLIPPAGE)
        exit_idx = idx + hold_days
        if exit_idx >= len(df):
            continue
        exit_price = df["close"].iloc[exit_idx] * (1 - SLIPPAGE)
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        pnl_pct -= COMMISSION_RATE * 2 * 100 + STAMP_TAX * 100
        trades.append({
            "entry_date": sig["date"], "exit_date": df["date"].iloc[exit_idx],
            "code": code, "entry_price": entry_price, "exit_price": exit_price,
            "pnl_pct": pnl_pct, "score": sig["score"],
            "market_state": sig["market_state"],
        })
    return trades

# ============================================================
# 5. 绩效分析
# ============================================================
def analyze_strategy(name, trades):
    """计算策略完整绩效指标"""
    if not trades:
        return {"name": name, "trade_count": 0}
    df = pd.DataFrame(trades)
    pnl = df["pnl_pct"]
    result = {
        "name": name,
        "trade_count": len(df),
        "win_rate": (pnl > 0).mean() * 100,
        "avg_pnl": pnl.mean(),
        "median_pnl": pnl.median(),
        "profit_factor": abs(pnl[pnl > 0].mean() / pnl[pnl < 0].mean()) if (pnl < 0).any() and (pnl > 0).any() else 0,
        "max_win": pnl.max(),
        "max_loss": pnl.min(),
        "avg_hold_days": None,
        # 累计收益: 用等权分配(每笔用1/N资金)避免全仓复利失真
        "total_return": pnl.mean() / 100 * len(df),  # 简单累计=平均收益×笔数
    }
    # 按市场环境分组
    if "market_state" in df.columns:
        for state in ["up", "down", "neutral", "neutral_weak"]:
            sub = df[df["market_state"] == state]["pnl_pct"]
            if len(sub) > 0:
                result[f"{state}_count"] = len(sub)
                result[f"{state}_wr"] = (sub > 0).mean() * 100
                result[f"{state}_avg"] = sub.mean()
    # 月度统计
    if "entry_date" in df.columns:
        df["month"] = pd.to_datetime(df["entry_date"]).dt.to_period("M")
        monthly = df.groupby("month")["pnl_pct"].agg(["count", "mean"]).reset_index()
        result["monthly_count"] = len(monthly)
        result["positive_months"] = (monthly["mean"] > 0).sum()
    return result

def print_comparison(results):
    """打印策略对比表"""
    print(f"\n{'='*90}")
    print(f"  {'指标':<24}", end="")
    for r in results:
        print(f" {r['name']:>14}", end="")
    print(f"\n{'-'*90}")

    rows = [
        ("交易笔数", "trade_count", ".0f", ""),
        ("胜率%", "win_rate", ".1f", "%"),
        ("平均收益%", "avg_pnl", "+.2f", "%"),
        ("中位收益%", "median_pnl", "+.2f", "%"),
        ("盈亏比", "profit_factor", ".2f", ""),
        ("最大单笔盈利%", "max_win", "+.1f", "%"),
        ("最大单笔亏损%", "max_loss", "+.1f", "%"),
        ("累计收益%(简单)", None, None, None),  # 特殊处理
    ]
    for label, key, fmt, suffix in rows:
        print(f"  {label:<24}", end="")
        for r in results:
            if key is None:
                val = (r.get("total_return", 0) or 0) * 100
                print(f" {val:>+13.1f}%", end="")
            else:
                val = r.get(key, 0) or 0
                try:
                    s = format(val, fmt) + suffix
                except:
                    s = f"{val:.2f}{suffix}"
                print(f" {s:>14}", end="")
        print()

    # 弱势市场
    print(f"  {'-'*90}")
    print(f"  {'弱势市场(down/neutral_weak)':<24}", end="")
    for r in results:
        down_cnt = (r.get("down_count", 0) or 0) + (r.get("neutral_weak_count", 0) or 0)
        down_avg = 0
        if down_cnt > 0:
            d1 = r.get("down_avg", 0) or 0
            d2 = r.get("neutral_weak_avg", 0) or 0
            down_avg = (d1 * (r.get("down_count", 0) or 0) + d2 * (r.get("neutral_weak_count", 0) or 0)) / max(1, down_cnt)
        print(f" {down_cnt:>5}笔/{down_avg:>+6.2f}%".rjust(15), end="")
    print()

# ============================================================
# 6. 主函数
# ============================================================
def main():
    t0 = time.time()
    print("=" * 90)
    print("  L3 策略级历史回测")
    print(f"  区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,.0f}")
    print("=" * 90)

    # 加载数据
    print("\n[1/7] 加载历史数据...")
    data_dict, max_date = load_all_data()
    if len(data_dict) < 10:
        print("[FAIL] 数据不足")
        return
    print(f"  数据截止: {max_date}")

    # 预计算指标
    print("\n[2/7] 预计算技术指标...")
    precomputed = {}
    for code, df in data_dict.items():
        precomputed[code] = compute_indicators(df)

    if "000300" not in precomputed:
        print("[FAIL] 无000300基准")
        return
    idx_df = precomputed["000300"]
    calendar = idx_df["date"].values
    mask = (calendar >= START_DATE) & (calendar <= END_DATE)
    trade_dates = calendar[mask]
    candidates = [c for c in precomputed if c != "000300" and not c.startswith("588") and not c.startswith("159")]
    print(f"  候选股: {len(candidates)} 只, 交易日: {len(trade_dates)} 天")

    # 运行各策略
    strategies = {
        "CANSLIM_V60": ("S1:CANSLIM V6.0", strategy_canslim_v60),
        "CANSLIM_V52": ("S2:CANSLIM V5.2", strategy_canslim_v52),
        "MA20_Trend": ("S3:MA20趋势", strategy_ma20_trend),
        "DualMA": ("S4:双均线趋势", strategy_dual_ma),
        "Combined": ("S5:组合策略", strategy_combined),
    }

    all_results = []
    all_trades = {}

    for i, (key, (label, fn)) in enumerate(strategies.items()):
        print(f"\n[{i+3}/7] 运行 {label}...")
        t1 = time.time()
        if key in ("MA20_Trend", "DualMA"):
            trades = fn(data_dict, precomputed, idx_df, trade_dates, candidates)
        else:
            signals = fn(data_dict, precomputed, idx_df, trade_dates, candidates)
            trades = signals_to_trades(signals, precomputed)
        elapsed = time.time() - t1
        print(f"  交易笔数: {len(trades)}, 耗时: {elapsed:.1f}s")
        result = analyze_strategy(label, trades)
        all_results.append(result)
        all_trades[key] = trades

    # 对比输出
    print(f"\n[7/7] 策略对比分析...")
    print_comparison(all_results)

    # 保存结果
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, "strategy_backtest_result.json")
    save_data = {}
    for r in all_results:
        name = r["name"]
        save_data[name] = {k: (v if not isinstance(v, (np.floating, np.integer)) else float(v))
                           for k, v in r.items()}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[OK] 结果已保存: {output_path}")

    total_time = time.time() - t0
    print(f"\n{'='*90}")
    print(f"  总耗时: {total_time:.1f}秒")
    print(f"{'='*90}")

if __name__ == "__main__":
    main()
