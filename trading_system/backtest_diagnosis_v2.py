# -*- coding: utf-8 -*-
"""
全面策略诊断回测 V2（增强版）
================================================================
修复V1问题 + 新增模块:
  [FIX] 盘中预警pct_change单位bug（小数→百分比）
  [NEW] 模块5: Composite评分(generate_holdings_report)分组预测力验证
  [NEW] 模块6: 调仓逻辑回测（卖出<40/买入>65/分差≥20）
  [NEW] 模块7: 17道风控关卡拦截率模拟
  [NEW] HTML报告输出

输出: output/diagnosis_v2_YYYYMMDD.html + .json + 控制台摘要
"""

import sys, os, time, logging, datetime, sqlite3, warnings, json
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("diagnosis_v2")

# ============================================================
# 回测参数
# ============================================================
START_DATE = "2022-01-01"
END_DATE = datetime.date.today().strftime("%Y-%m-%d")
INITIAL_CAPITAL = config.TOTAL_CAPITAL
COMMISSION_RATE = 0.00025
STAMP_TAX = 0.001
SLIPPAGE = 0.001
SCAN_INTERVAL = 5
MAX_HOLDINGS = config.MAX_HOLDINGS  # 7

# ============================================================
# 数据加载与指标计算
# ============================================================
def load_data():
    if not os.path.exists(config.DB_PATH):
        logger.error(f"数据库不存在: {config.DB_PATH}")
        return {}
    conn = sqlite3.connect(config.DB_PATH)
    df_all = pd.read_sql("SELECT code,date,open,close,high,low,volume,amount FROM daily_kline ORDER BY code,date", conn)
    conn.close()
    data_dict = {}
    for code, group in df_all.groupby("code"):
        df = group.copy()
        for col in ["open","close","high","low","volume","amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        if len(df) > 250:
            data_dict[code] = df
    logger.info(f"加载 {len(data_dict)} 只股票, {df_all['date'].min()} ~ {df_all['date'].max()}")
    return data_dict

def compute_indicators(df):
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
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
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    prev_close = np.roll(close, 1); prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    df["atr"] = pd.Series(tr, index=df.index).rolling(14).mean()
    # [FIX] pct_change 转为百分比单位（×100），与阈值2.0/5.0对齐
    df["pct_change"] = df["close"].pct_change() * 100
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]
    # 布林带
    df["boll_mid"] = df["close"].rolling(20).mean()
    boll_std = df["close"].rolling(20).std()
    df["boll_upper"] = df["boll_mid"] + 2 * boll_std
    df["boll_lower"] = df["boll_mid"] - 2 * boll_std
    return df

def get_market_regime(idx_df, date):
    mask = idx_df["date"] <= date
    if mask.sum() < 60:
        return "RANGE"
    row = idx_df[mask].iloc[-1]
    if pd.isna(row["ma20"]) or pd.isna(row["ma60"]):
        return "RANGE"
    if row["close"] > row["ma20"] > row["ma60"]:
        return "BULL"
    elif row["close"] < row["ma20"] < row["ma60"]:
        return "BEAR"
    return "RANGE"

# ============================================================
# CANSLIM因子评分(与stock_screener.py一致)
# ============================================================
def calc_canslim_factors(df, idx, idx_df=None):
    if idx < 60:
        return None
    row = df.iloc[idx]
    close, ma20, ma60 = row["close"], row["ma20"], row["ma60"]
    volume, vol_ma20 = row["volume"], row["vol_ma20"]
    if pd.isna(ma20) or pd.isna(ma60):
        return None
    factors = {}
    # N因子(20分): 新高+趋势
    n = 0
    high_60 = df["high"].iloc[idx-60:idx].max()
    if close >= high_60 * 0.98: n += 12
    if close > ma20 and ma20 > ma60: n += 8
    factors["N"] = min(n, 20)
    # S因子(20分): 量价
    s = 0
    if not pd.isna(vol_ma20) and vol_ma20 > 0:
        vr = volume / vol_ma20
        if vr < 0.7 and close >= ma20 * 0.99: s += 12
        if vr > 1.5 and close > df["close"].iloc[max(0,idx-1)]: s += 8
        if idx >= 5 and df["volume"].iloc[idx-5:idx].mean() < vol_ma20 * 0.8: s += 5
    factors["S"] = min(s, 20)
    # L因子(20分): 龙头动量
    l = 0
    if idx >= 20:
        c20 = (close / df["close"].iloc[idx-20] - 1) * 100
        if c20 > 10: l += 12
        elif c20 > 5: l += 8
        elif c20 > 0: l += 4
    if close > ma20: l += 5
    if close > ma60: l += 3
    factors["L"] = min(l, 20)
    # CAI(固定8)
    factors["CAI"] = 8
    # P因子(20分): 买点
    p = 0
    ms = row["ma20_slope"]
    if not pd.isna(ms) and ms > 0: p += 6
    rsi = row.get("rsi", np.nan)
    if not pd.isna(rsi):
        if 30 < rsi < 50: p += 8
        elif 50 <= rsi < 70: p += 5
    if not pd.isna(ma20) and row["low"] <= ma20 * 1.01 and close >= ma20: p += 6
    factors["P"] = min(p, 20)
    # W因子(大盘±5)
    w = 0
    if idx_df is not None:
        regime = get_market_regime(idx_df, row["date"])
        if regime == "BULL": w = 5
        elif regime == "BEAR": w = -5
    factors["W"] = w
    factors["total"] = factors["N"] + factors["S"] + factors["L"] + factors["CAI"] + factors["P"] + factors["W"]
    return factors

# ============================================================
# Composite评分(与generate_holdings_report.py一致)
# ============================================================
def calc_composite_score(df, idx):
    """复制generate_holdings_report.py的Composite评分逻辑"""
    if idx < 60:
        return None
    row = df.iloc[idx]
    close = row["close"]
    ma5, ma10, ma20, ma60 = row["ma5"], row["ma10"], row["ma20"], row["ma60"]
    ma20_slope = row["ma20_slope"]
    rsi = row["rsi"]
    macd_dif, macd_dea = row["macd_dif"], row["macd_dea"]
    volume, vol_ma20 = row["volume"], row["vol_ma20"]
    if pd.isna(ma20) or pd.isna(close):
        return None
    # 趋势评分
    trend_score = 0
    if not pd.isna(ma5) and not pd.isna(ma10):
        if ma5 > ma10 > ma20: trend_score += 2
        elif ma5 < ma10 < ma20: trend_score -= 2
    if not pd.isna(ma20_slope):
        if ma20_slope > 0: trend_score += 1
        else: trend_score -= 1
    if not pd.isna(ma60):
        if close > ma60: trend_score += 1
        elif close < ma60: trend_score -= 1
    # 动量评分
    momentum_score = 0
    if not pd.isna(rsi):
        if rsi > 70: momentum_score -= 1
        elif rsi < 30: momentum_score += 1
        elif rsi > 55: momentum_score += 0.5
    if not pd.isna(macd_dif) and not pd.isna(macd_dea):
        if macd_dif > macd_dea: momentum_score += 1
        else: momentum_score -= 1
    # Composite = 50 + trend*8 + momentum*6
    composite = 50 + trend_score * 8 + momentum_score * 6
    composite = max(0, min(100, composite))
    return {"composite": composite, "trend_score": trend_score, "momentum_score": momentum_score}

# ============================================================
# 模块1: CANSLIM因子有效性
# ============================================================
def run_factor_analysis(precomputed, idx_df, candidates, trade_dates):
    logger.info("=" * 60)
    logger.info("  [1] CANSLIM因子有效性分析")
    logger.info("=" * 60)
    signals = []
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        regime = get_market_regime(idx_df, scan_date)
        year = str(scan_date)[:4]
        for code in candidates:
            df = precomputed[code]
            mask = df["date"] <= scan_date
            if mask.sum() < 60: continue
            idx = mask.sum() - 1
            row = df.iloc[idx]
            if pd.isna(row["ma20"]): continue
            if "amount" in df.columns:
                avg_amt = df["amount"].iloc[max(0,idx-19):idx+1].mean()
                if not pd.isna(avg_amt) and avg_amt < config.MIN_DAILY_AMOUNT:
                    continue
            factors = calc_canslim_factors(df, idx, idx_df)
            if factors is None: continue
            close = row["close"]
            fwd = {}
            for d in [5, 10, 20]:
                if idx + d < len(df):
                    fwd[f"{d}d"] = (df["close"].iloc[idx+d] - close) / close * 100
                else:
                    fwd[f"{d}d"] = np.nan
            signals.append({"date": scan_date, "code": code, "year": year,
                           "regime": regime, "close": close, **factors, **fwd})
    sig_df = pd.DataFrame(signals)
    logger.info(f"  总信号数: {len(sig_df)}")
    results = {"total_signals": len(sig_df)}
    # 1A: 评分区间分组
    bins = [(80, 200, ">=80"), (60, 80, "60-79"), (40, 60, "40-59"), (0, 40, "<40")]
    score_stats = []
    for lo, hi, label in bins:
        sub = sig_df[(sig_df["total"] >= lo) & (sig_df["total"] < hi)]
        if len(sub) < 5: continue
        st = {"group": label, "n": len(sub),
              "avg_5d": sub["5d"].mean(), "avg_10d": sub["10d"].mean(), "avg_20d": sub["20d"].mean(),
              "wr_5d": (sub["5d"]>0).mean()*100, "wr_10d": (sub["10d"]>0).mean()*100,
              "wr_20d": (sub["20d"]>0).mean()*100}
        score_stats.append(st)
        logger.info(f"    {label}: N={st['n']}, 5d={st['avg_5d']:+.2f}%, 20d={st['avg_20d']:+.2f}%, WR20={st['wr_20d']:.1f}%")
    results["score_stats"] = score_stats
    # 1B: 分市场环境
    regime_stats = []
    for regime in ["BULL", "RANGE", "BEAR"]:
        sub = sig_df[sig_df["regime"] == regime]
        high = sub[sub["total"] >= 60]
        low = sub[sub["total"] < 40]
        if len(high) < 5 or len(low) < 5: continue
        st = {"regime": regime, "n_high": len(high), "n_low": len(low),
              "high_20d": high["20d"].mean(), "low_20d": low["20d"].mean(),
              "spread": high["20d"].mean() - low["20d"].mean(),
              "high_wr": (high["20d"]>0).mean()*100, "low_wr": (low["20d"]>0).mean()*100}
        regime_stats.append(st)
        logger.info(f"    {regime}: 高分20d={st['high_20d']:+.2f}%(WR{st['high_wr']:.0f}%) vs 低分={st['low_20d']:+.2f}%, 差={st['spread']:+.2f}%")
    results["regime_stats"] = regime_stats
    # 1C: 分年度
    year_stats = []
    for year in sorted(sig_df["year"].unique()):
        sub = sig_df[(sig_df["year"] == year) & (sig_df["total"] >= 60)]
        if len(sub) < 10: continue
        st = {"year": year, "n": len(sub), "avg_20d": sub["20d"].mean(),
              "wr_20d": (sub["20d"]>0).mean()*100, "avg_5d": sub["5d"].mean()}
        year_stats.append(st)
        logger.info(f"    {year}: N={st['n']}, 20d={st['avg_20d']:+.2f}%, WR20={st['wr_20d']:.1f}%")
    results["year_stats"] = year_stats
    # 1D: 因子IC
    factor_ic = []
    for fname in ["N", "S", "L", "P", "W"]:
        q70 = sig_df[fname].quantile(0.7)
        q30 = sig_df[fname].quantile(0.3)
        high = sig_df[sig_df[fname] >= q70]
        low = sig_df[sig_df[fname] <= q30]
        if len(high) < 10 or len(low) < 10: continue
        spread = high["20d"].mean() - low["20d"].mean()
        ic_by_regime = {}
        for regime in ["BULL", "RANGE", "BEAR"]:
            rh = sig_df[(sig_df["regime"]==regime) & (sig_df[fname]>=q70)]
            rl = sig_df[(sig_df["regime"]==regime) & (sig_df[fname]<=q30)]
            if len(rh) >= 5 and len(rl) >= 5:
                ic_by_regime[regime] = rh["20d"].mean() - rl["20d"].mean()
        factor_ic.append({"factor": fname, "spread_20d": spread,
                         "high_avg": high["20d"].mean(), "low_avg": low["20d"].mean(),
                         "ic_by_regime": ic_by_regime})
        logger.info(f"    {fname}: spread={spread:+.2f}%, IC={ic_by_regime}")
    results["factor_ic"] = factor_ic
    # 1E: 硬筛效果
    all_signals_no_filter = []
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL * 4):
        scan_date = trade_dates[scan_idx]
        for code in candidates[:15]:
            df = precomputed[code]
            mask = df["date"] <= scan_date
            if mask.sum() < 80: continue
            idx = mask.sum() - 1
            factors = calc_canslim_factors(df, idx, idx_df)
            if factors is None: continue
            close = df["close"].iloc[idx]
            fwd20 = (df["close"].iloc[idx+20] - close) / close * 100 if idx + 20 < len(df) else np.nan
            ma20_slope = df["ma20_slope"].iloc[idx]
            passes_filter = (not pd.isna(ma20_slope) and ma20_slope > 0 and close >= df["ma20"].iloc[idx])
            all_signals_no_filter.append({"passes": passes_filter, "fwd20": fwd20})
    filter_df = pd.DataFrame(all_signals_no_filter)
    if len(filter_df) > 20:
        passed = filter_df[filter_df["passes"]]
        failed = filter_df[~filter_df["passes"]]
        results["filter_effect"] = {"passed_n": len(passed), "failed_n": len(failed),
                        "passed_avg20": passed["fwd20"].mean() if len(passed)>0 else 0,
                        "failed_avg20": failed["fwd20"].mean() if len(failed)>0 else 0}
        logger.info(f"    硬筛增益: {results['filter_effect']['passed_avg20']-results['filter_effect']['failed_avg20']:+.2f}%")
    results["sig_df"] = sig_df
    return results

# ============================================================
# 模块2: 盘中预警双通道阈值回测 [FIXED]
# ============================================================
def run_intraday_threshold_backtest(precomputed, idx_df, candidates, trade_dates):
    logger.info("\n" + "=" * 60)
    logger.info("  [2] 盘中预警双通道阈值回测 (pct_change已修复为百分比)")
    logger.info("=" * 60)
    channel_a_signals = []
    channel_b_signals = []
    for scan_idx in range(60, len(trade_dates), 1):
        scan_date = trade_dates[scan_idx]
        for code in candidates:
            df = precomputed[code]
            mask = df["date"] == scan_date
            if not mask.any(): continue
            idx = df[mask].index[0]
            if idx < 20 or idx + 5 >= len(df): continue
            row = df.iloc[idx]
            pct = row["pct_change"]  # 现在是百分比单位(2.0 = 2%)
            vr = row["vol_ratio"]
            if pd.isna(pct) or pd.isna(vr): continue
            close = row["close"]
            fwd = {}
            for d in [1, 3, 5]:
                if idx + d < len(df):
                    fwd[f"{d}d"] = (df["close"].iloc[idx+d] - close) / close * 100
                else:
                    fwd[f"{d}d"] = np.nan
            # 通道A: 涨≥2% + 量比≥1.5
            if pct >= 2.0 and vr >= 1.5:
                channel_a_signals.append({"date": scan_date, "code": code, "pct": pct, "vr": vr, **fwd})
            # 通道B: 涨≥5% + 量比≥2 + 成交额≥3亿
            amt = row.get("amount", 0)
            if pct >= 5.0 and vr >= 2.0 and (not pd.isna(amt) and amt >= 3e8):
                channel_b_signals.append({"date": scan_date, "code": code, "pct": pct, "vr": vr, **fwd})
    results = {}
    if channel_a_signals:
        a_df = pd.DataFrame(channel_a_signals)
        results["channel_a"] = {
            "n": len(a_df),
            "avg_1d": a_df["1d"].mean(), "avg_3d": a_df["3d"].mean(), "avg_5d": a_df["5d"].mean(),
            "wr_1d": (a_df["1d"]>0).mean()*100, "wr_3d": (a_df["3d"]>0).mean()*100,
            "wr_5d": (a_df["5d"]>0).mean()*100,
            "miss_rate": (a_df["5d"] < -3).mean()*100
        }
        logger.info(f"  通道A(涨≥2%+量比≥1.5): N={len(a_df)}")
        logger.info(f"    T+1={results['channel_a']['avg_1d']:+.2f}%(WR{results['channel_a']['wr_1d']:.0f}%), "
                    f"T+3={results['channel_a']['avg_3d']:+.2f}%, T+5={results['channel_a']['avg_5d']:+.2f}%")
        logger.info(f"    误报率(5d跌>3%): {results['channel_a']['miss_rate']:.1f}%")
    else:
        logger.info("  通道A: 无信号")
    if channel_b_signals:
        b_df = pd.DataFrame(channel_b_signals)
        results["channel_b"] = {
            "n": len(b_df),
            "avg_1d": b_df["1d"].mean(), "avg_3d": b_df["3d"].mean(), "avg_5d": b_df["5d"].mean(),
            "wr_1d": (b_df["1d"]>0).mean()*100, "wr_3d": (b_df["3d"]>0).mean()*100,
            "wr_5d": (b_df["5d"]>0).mean()*100,
            "miss_rate": (b_df["5d"] < -3).mean()*100
        }
        logger.info(f"  通道B(涨≥5%+量比≥2+额≥3亿): N={len(b_df)}")
        logger.info(f"    T+1={results['channel_b']['avg_1d']:+.2f}%(WR{results['channel_b']['wr_1d']:.0f}%), "
                    f"T+3={results['channel_b']['avg_3d']:+.2f}%, T+5={results['channel_b']['avg_5d']:+.2f}%")
        logger.info(f"    误报率(5d跌>3%): {results['channel_b']['miss_rate']:.1f}%")
    else:
        logger.info("  通道B: 无信号")
    # 阈值敏感性测试
    logger.info("\n  --- 阈值敏感性(通道A变体) ---")
    threshold_tests = []
    for pct_th in [1.5, 2.0, 3.0, 4.0, 5.0]:
        for vr_th in [1.0, 1.5, 2.0, 2.5]:
            cnt = 0; fwd5_sum = 0; wr_cnt = 0
            for scan_idx in range(60, len(trade_dates), 3):
                scan_date = trade_dates[scan_idx]
                for code in candidates[:25]:
                    df = precomputed[code]
                    mask = df["date"] == scan_date
                    if not mask.any(): continue
                    idx = df[mask].index[0]
                    if idx < 20 or idx + 5 >= len(df): continue
                    row = df.iloc[idx]
                    if pd.isna(row["pct_change"]) or pd.isna(row["vol_ratio"]): continue
                    if row["pct_change"] >= pct_th and row["vol_ratio"] >= vr_th:
                        cnt += 1
                        f5 = (df["close"].iloc[idx+5] - row["close"]) / row["close"] * 100
                        fwd5_sum += f5
                        if f5 > 0: wr_cnt += 1
            if cnt > 10:
                threshold_tests.append({"pct_th": pct_th, "vr_th": vr_th, "n": cnt,
                                       "avg_5d": fwd5_sum/cnt, "wr_5d": wr_cnt/cnt*100})
    results["threshold_tests"] = threshold_tests
    if threshold_tests:
        best = max(threshold_tests, key=lambda x: x["avg_5d"])
        logger.info(f"    最优组合: 涨≥{best['pct_th']}%+量比≥{best['vr_th']}, "
                    f"N={best['n']}, avg5d={best['avg_5d']:+.2f}%, WR={best['wr_5d']:.0f}%")
    return results

# ============================================================
# 模块3: 风控体系有效性
# ============================================================
def run_risk_control_backtest(precomputed, idx_df, candidates, trade_dates):
    logger.info("\n" + "=" * 60)
    logger.info("  [3] 风控体系有效性回测")
    logger.info("=" * 60)
    results = {}
    # 收集买入事件
    buy_events = []
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        for code in candidates:
            df = precomputed[code]
            mask = df["date"] <= scan_date
            if mask.sum() < 60: continue
            idx = mask.sum() - 1
            factors = calc_canslim_factors(df, idx, idx_df)
            if factors and factors["total"] >= 50:
                buy_events.append({"code": code, "idx": idx, "date": scan_date, "close": df["close"].iloc[idx]})
    logger.info(f"  买入事件总数: {len(buy_events)}")
    # 3A: 止损对比
    logger.info("\n  --- 3A: 止损线对比 ---")
    stop_results = []
    for stop_pct in [0.05, 0.07, 0.10, 0.15]:
        triggered = 0; saved = 0; hurt = 0; total_pnl = 0
        for ev in buy_events:
            df = precomputed[ev["code"]]
            idx = ev["idx"]
            buy_price = ev["close"]
            stop_price = buy_price * (1 - stop_pct)
            stopped = False
            for d in range(1, min(21, len(df) - idx)):
                if df["low"].iloc[idx+d] <= stop_price:
                    stopped = True; triggered += 1
                    if idx+d+5 < len(df):
                        after_min = df["close"].iloc[idx+d:idx+d+5].min()
                        if after_min < stop_price * 0.97: saved += 1
                        else: hurt += 1
                    total_pnl += -stop_pct * 100
                    break
            if not stopped:
                if idx + 20 < len(df):
                    total_pnl += (df["close"].iloc[idx+20] - buy_price) / buy_price * 100
        n = len(buy_events)
        stop_results.append({"stop_pct": stop_pct*100, "triggered": triggered,
            "trigger_rate": triggered/n*100 if n>0 else 0,
            "saved": saved, "hurt": hurt,
            "save_rate": saved/triggered*100 if triggered>0 else 0,
            "avg_pnl": total_pnl/n if n>0 else 0})
        logger.info(f"    止损{stop_pct*100:.0f}%: 触发{triggered}({triggered/n*100:.1f}%), 有效{saved}({saved/max(triggered,1)*100:.0f}%), PnL={total_pnl/n:+.2f}%")
    results["stop_comparison"] = stop_results
    # 3B: 满仓追高
    high_pos_signals = []; low_pos_signals = []
    for ev in buy_events:
        df = precomputed[ev["code"]]
        idx = ev["idx"]
        if idx + 10 >= len(df) or idx < 5: continue
        fwd10 = (df["close"].iloc[idx+10] - ev["close"]) / ev["close"] * 100
        recent_gain = (ev["close"] / df["close"].iloc[idx-5] - 1) * 100
        if recent_gain > 8: high_pos_signals.append(fwd10)
        else: low_pos_signals.append(fwd10)
    if high_pos_signals and low_pos_signals:
        results["full_position"] = {
            "high_pos_n": len(high_pos_signals), "high_pos_avg10": np.mean(high_pos_signals),
            "high_pos_wr": (np.array(high_pos_signals)>0).mean()*100,
            "low_pos_n": len(low_pos_signals), "low_pos_avg10": np.mean(low_pos_signals),
            "low_pos_wr": (np.array(low_pos_signals)>0).mean()*100}
        logger.info(f"    追高: N={len(high_pos_signals)}, avg10d={np.mean(high_pos_signals):+.2f}%")
        logger.info(f"    正常: N={len(low_pos_signals)}, avg10d={np.mean(low_pos_signals):+.2f}%")
    # 3C: 连亏暂停
    consecutive_loss_rets = []; normal_rets = []
    for i in range(2, len(buy_events)):
        ev = buy_events[i]
        df = precomputed[ev["code"]]
        idx = ev["idx"]
        if idx + 10 >= len(df): continue
        fwd10 = (df["close"].iloc[idx+10] - ev["close"]) / ev["close"] * 100
        prev1, prev2 = buy_events[i-1], buy_events[i-2]
        df1, df2 = precomputed[prev1["code"]], precomputed[prev2["code"]]
        idx1, idx2 = prev1["idx"], prev2["idx"]
        if idx1+10 < len(df1) and idx2+10 < len(df2):
            ret1 = (df1["close"].iloc[idx1+10] - prev1["close"]) / prev1["close"] * 100
            ret2 = (df2["close"].iloc[idx2+10] - prev2["close"]) / prev2["close"] * 100
            if ret1 < 0 and ret2 < 0: consecutive_loss_rets.append(fwd10)
            else: normal_rets.append(fwd10)
    if consecutive_loss_rets:
        results["consecutive_loss"] = {
            "after_loss_n": len(consecutive_loss_rets), "after_loss_avg": np.mean(consecutive_loss_rets),
            "after_loss_wr": (np.array(consecutive_loss_rets)>0).mean()*100,
            "normal_n": len(normal_rets), "normal_avg": np.mean(normal_rets) if normal_rets else 0,
            "normal_wr": (np.array(normal_rets)>0).mean()*100 if normal_rets else 0}
        logger.info(f"    连亏后: avg10d={np.mean(consecutive_loss_rets):+.2f}%, WR={(np.array(consecutive_loss_rets)>0).mean()*100:.0f}%")
        logger.info(f"    正常:   avg10d={np.mean(normal_rets):+.2f}%, WR={(np.array(normal_rets)>0).mean()*100:.0f}%")
    return results

# ============================================================
# 模块4: 组合模拟
# ============================================================
def run_portfolio_backtest(precomputed, idx_df, candidates, trade_dates):
    logger.info("\n" + "=" * 60)
    logger.info("  [4] 组合模拟回测")
    logger.info("=" * 60)
    cash = INITIAL_CAPITAL
    positions = {}
    trades = []
    daily_values = []
    max_value = INITIAL_CAPITAL
    max_drawdown = 0
    for t_idx, date in enumerate(trade_dates):
        pv = cash
        for code, pos in list(positions.items()):
            df = precomputed[code]
            mask = df["date"] == date
            if mask.any(): pv += pos["shares"] * df[mask].iloc[0]["close"]
        daily_values.append({"date": date, "value": pv})
        if pv > max_value: max_value = pv
        dd = (max_value - pv) / max_value
        if dd > max_drawdown: max_drawdown = dd
        # 止损/止盈
        for code in list(positions.keys()):
            pos = positions[code]
            df = precomputed[code]
            mask = df["date"] == date
            if not mask.any(): continue
            price = df[mask].iloc[0]["close"]
            pnl = (price - pos["buy_price"]) / pos["buy_price"]
            if pnl <= -0.10:
                sell_price = price * (1 - SLIPPAGE)
                revenue = sell_price * pos["shares"]
                cash += revenue - revenue * (COMMISSION_RATE + STAMP_TAX)
                trades.append({"code": code, "date": date, "pnl_pct": pnl*100-0.35, "reason": "stop_loss"})
                del positions[code]
            elif pnl > 0.15:
                pos["highest"] = max(pos.get("highest", pos["buy_price"]), price)
                if (pos["highest"] - price) / pos["highest"] > 0.06:
                    sell_price = price * (1 - SLIPPAGE)
                    revenue = sell_price * pos["shares"]
                    cash += revenue - revenue * (COMMISSION_RATE + STAMP_TAX)
                    trades.append({"code": code, "date": date, "pnl_pct": pnl*100-0.35, "reason": "trailing"})
                    del positions[code]
        # 买入
        if t_idx % SCAN_INTERVAL != 0 or t_idx < 60: continue
        if len(positions) >= MAX_HOLDINGS: continue
        regime = get_market_regime(idx_df, date)
        if regime == "BEAR": continue
        day_sigs = []
        for code in candidates:
            if code in positions: continue
            df = precomputed[code]
            mask = df["date"] <= date
            if mask.sum() < 60: continue
            idx = mask.sum() - 1
            factors = calc_canslim_factors(df, idx, idx_df)
            if factors is None: continue
            min_score = 50 if regime == "BULL" else 55
            if factors["total"] >= min_score:
                row = df.iloc[idx]
                if row["close"] >= row["ma20"] * 0.95:
                    day_sigs.append({"code": code, "score": factors["total"], "close": row["close"]})
        day_sigs.sort(key=lambda x: x["score"], reverse=True)
        for sig in day_sigs[:2]:
            if len(positions) >= MAX_HOLDINGS: break
            buy_price = sig["close"] * (1 + SLIPPAGE)
            pos_size = INITIAL_CAPITAL / MAX_HOLDINGS
            shares = int(pos_size / buy_price / 100) * 100
            if shares < 100: continue
            cost = buy_price * shares
            if cash < cost * 1.001: continue
            cash -= cost * (1 + COMMISSION_RATE)
            positions[sig["code"]] = {"shares": shares, "buy_price": buy_price, "highest": buy_price}
            trades.append({"code": sig["code"], "date": date, "pnl_pct": 0, "reason": "buy"})
    # 平仓
    final_date = trade_dates[-1]
    for code in list(positions.keys()):
        df = precomputed[code]
        mask = df["date"] == final_date
        if mask.any():
            price = df[mask].iloc[0]["close"] * (1 - SLIPPAGE)
            pos = positions[code]
            pnl = (price - pos["buy_price"]) / pos["buy_price"]
            cash += price * pos["shares"] * (1 - COMMISSION_RATE - STAMP_TAX)
            trades.append({"code": code, "date": final_date, "pnl_pct": pnl*100-0.35, "reason": "end"})
    # 绩效
    final_value = cash
    total_return = (final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL
    days = len(trade_dates)
    annual_return = (1 + total_return) ** (252 / max(days, 1)) - 1
    sell_trades = [t for t in trades if t["reason"] != "buy"]
    win_trades = [t for t in sell_trades if t["pnl_pct"] > 0]
    loss_trades = [t for t in sell_trades if t["pnl_pct"] <= 0]
    win_rate = len(win_trades) / max(len(sell_trades), 1) * 100
    avg_win = np.mean([t["pnl_pct"] for t in win_trades]) if win_trades else 0
    avg_loss = abs(np.mean([t["pnl_pct"] for t in loss_trades])) if loss_trades else 1
    profit_factor = avg_win / max(avg_loss, 0.01)
    dv_df = pd.DataFrame(daily_values)
    daily_rets = dv_df["value"].pct_change().dropna()
    sharpe = (daily_rets.mean() - 0.03/252) / daily_rets.std() * np.sqrt(252) if daily_rets.std() > 0 else 0
    calmar = annual_return / max(max_drawdown, 0.01)
    yearly = {}
    dv_df["year"] = dv_df["date"].astype(str).str[:4]
    for year, grp in dv_df.groupby("year"):
        if len(grp) < 20: continue
        yr_ret = (grp["value"].iloc[-1] / grp["value"].iloc[0] - 1) * 100
        yr_max_dd = 0; peak = grp["value"].iloc[0]
        for v in grp["value"]:
            if v > peak: peak = v
            dd = (peak - v) / peak
            if dd > yr_max_dd: yr_max_dd = dd
        yearly[year] = {"return": yr_ret, "max_dd": yr_max_dd*100}
    logger.info(f"  最终: {final_value:,.0f} | 收益: {total_return*100:+.2f}% | 年化: {annual_return*100:+.2f}%")
    logger.info(f"  回撤: {max_drawdown*100:.2f}% | 夏普: {sharpe:.2f} | Calmar: {calmar:.2f}")
    logger.info(f"  交易: {len(sell_trades)}笔 | 胜率: {win_rate:.1f}% | 盈亏比: {profit_factor:.2f}")
    return {"final_value": final_value, "total_return": total_return, "annual_return": annual_return,
            "max_drawdown": max_drawdown, "sharpe": sharpe, "calmar": calmar,
            "total_trades": len(sell_trades), "win_rate": win_rate, "profit_factor": profit_factor,
            "avg_win": avg_win, "avg_loss": avg_loss, "yearly": yearly, "daily_values": dv_df}

# ============================================================
# 模块5: Composite评分分组预测力验证 [NEW]
# ============================================================
def run_composite_analysis(precomputed, idx_df, candidates, trade_dates):
    logger.info("\n" + "=" * 60)
    logger.info("  [5] Composite评分分组预测力验证")
    logger.info("=" * 60)
    signals = []
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        regime = get_market_regime(idx_df, scan_date)
        for code in candidates:
            df = precomputed[code]
            mask = df["date"] <= scan_date
            if mask.sum() < 60: continue
            idx = mask.sum() - 1
            cs = calc_composite_score(df, idx)
            if cs is None: continue
            close = df["close"].iloc[idx]
            fwd = {}
            for d in [5, 10, 20]:
                if idx + d < len(df):
                    fwd[f"{d}d"] = (df["close"].iloc[idx+d] - close) / close * 100
                else:
                    fwd[f"{d}d"] = np.nan
            signals.append({"date": scan_date, "code": code, "regime": regime, **cs, **fwd})
    sig_df = pd.DataFrame(signals)
    logger.info(f"  Composite信号总数: {len(sig_df)}")
    results = {"total_signals": len(sig_df)}
    # 分组: >=70 / 55-69 / 45-54 / 30-44 / <30
    bins = [(70, 101, ">=70(强势)"), (55, 70, "55-69(偏多)"), (45, 55, "45-54(横盘)"),
            (30, 45, "30-44(偏空)"), (0, 30, "<30(弱势)")]
    group_stats = []
    for lo, hi, label in bins:
        sub = sig_df[(sig_df["composite"] >= lo) & (sig_df["composite"] < hi)]
        if len(sub) < 10: continue
        st = {"group": label, "n": len(sub),
              "avg_5d": sub["5d"].mean(), "avg_10d": sub["10d"].mean(), "avg_20d": sub["20d"].mean(),
              "wr_5d": (sub["5d"]>0).mean()*100, "wr_20d": (sub["20d"]>0).mean()*100}
        group_stats.append(st)
        logger.info(f"    {label}: N={st['n']}, 5d={st['avg_5d']:+.2f}%, 20d={st['avg_20d']:+.2f}%, WR20={st['wr_20d']:.1f}%")
    results["group_stats"] = group_stats
    # 分市场环境
    regime_stats = []
    for regime in ["BULL", "RANGE", "BEAR"]:
        sub = sig_df[sig_df["regime"] == regime]
        high = sub[sub["composite"] >= 65]
        low = sub[sub["composite"] < 40]
        if len(high) < 5 or len(low) < 5: continue
        st = {"regime": regime, "n_high": len(high), "n_low": len(low),
              "high_20d": high["20d"].mean(), "low_20d": low["20d"].mean(),
              "spread": high["20d"].mean() - low["20d"].mean()}
        regime_stats.append(st)
        logger.info(f"    {regime}: 高(≥65)20d={st['high_20d']:+.2f}% vs 低(<40)={st['low_20d']:+.2f}%, 差={st['spread']:+.2f}%")
    results["regime_stats"] = regime_stats
    return results

# ============================================================
# 模块6: 调仓逻辑回测 [NEW]
# ============================================================
def run_rebalance_backtest(precomputed, idx_df, candidates, trade_dates):
    logger.info("\n" + "=" * 60)
    logger.info("  [6] 调仓逻辑回测 (卖出<40/买入>65/分差≥20)")
    logger.info("=" * 60)
    SELL_TH = 40; BUY_TH = 65; GAP_TH = 20
    # 模拟: 每5天计算所有候选的composite，模拟调仓决策
    swap_events = []
    hold_events = []
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        scores = []
        for code in candidates:
            df = precomputed[code]
            mask = df["date"] <= scan_date
            if mask.sum() < 60: continue
            idx = mask.sum() - 1
            cs = calc_composite_score(df, idx)
            if cs is None: continue
            close = df["close"].iloc[idx]
            fwd20 = (df["close"].iloc[idx+20] - close) / close * 100 if idx + 20 < len(df) else np.nan
            scores.append({"code": code, "composite": cs["composite"], "fwd20": fwd20, "date": scan_date})
        if len(scores) < 5: continue
        scores.sort(key=lambda x: x["composite"], reverse=True)
        worst = scores[-1]  # 最低分
        best = scores[0]    # 最高分
        gap = best["composite"] - worst["composite"]
        # 调仓条件: worst<40 AND best>65 AND gap>=20
        if worst["composite"] < SELL_TH and best["composite"] > BUY_TH and gap >= GAP_TH:
            swap_events.append({"date": scan_date, "sell_score": worst["composite"],
                               "buy_score": best["composite"], "gap": gap,
                               "sell_fwd20": worst["fwd20"], "buy_fwd20": best["fwd20"]})
        else:
            # 持有不动: 记录worst的后续表现
            hold_events.append({"date": scan_date, "worst_score": worst["composite"], "worst_fwd20": worst["fwd20"]})
    results = {"swap_count": len(swap_events), "hold_count": len(hold_events)}
    if swap_events:
        sw_df = pd.DataFrame(swap_events)
        results["swap_stats"] = {
            "avg_sell_fwd20": sw_df["sell_fwd20"].mean(),
            "avg_buy_fwd20": sw_df["buy_fwd20"].mean(),
            "improvement": sw_df["buy_fwd20"].mean() - sw_df["sell_fwd20"].mean(),
            "sell_wr": (sw_df["sell_fwd20"]>0).mean()*100,
            "buy_wr": (sw_df["buy_fwd20"]>0).mean()*100,
            "avg_gap": sw_df["gap"].mean()
        }
        logger.info(f"  调仓触发: {len(swap_events)}次")
        logger.info(f"    卖出标的20d: {results['swap_stats']['avg_sell_fwd20']:+.2f}% (WR{results['swap_stats']['sell_wr']:.0f}%)")
        logger.info(f"    买入标的20d: {results['swap_stats']['avg_buy_fwd20']:+.2f}% (WR{results['swap_stats']['buy_wr']:.0f}%)")
        logger.info(f"    调仓增益: {results['swap_stats']['improvement']:+.2f}%")
    if hold_events:
        h_df = pd.DataFrame(hold_events)
        results["hold_stats"] = {"avg_worst_fwd20": h_df["worst_fwd20"].mean(),
                                 "worst_wr": (h_df["worst_fwd20"]>0).mean()*100}
        logger.info(f"  持有不动: {len(hold_events)}次, 最低分标的20d={results['hold_stats']['avg_worst_fwd20']:+.2f}%")
    # 阈值敏感性
    logger.info("\n  --- 调仓阈值敏感性 ---")
    threshold_sensitivity = []
    for sell_th in [30, 35, 40, 45]:
        for gap_th in [15, 20, 25, 30]:
            cnt = 0; improvement_sum = 0
            for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
                scan_date = trade_dates[scan_idx]
                scores = []
                for code in candidates[:20]:
                    df = precomputed[code]
                    mask = df["date"] <= scan_date
                    if mask.sum() < 60: continue
                    idx = mask.sum() - 1
                    cs = calc_composite_score(df, idx)
                    if cs is None: continue
                    close = df["close"].iloc[idx]
                    fwd20 = (df["close"].iloc[idx+20] - close) / close * 100 if idx + 20 < len(df) else np.nan
                    scores.append({"composite": cs["composite"], "fwd20": fwd20})
                if len(scores) < 5: continue
                scores.sort(key=lambda x: x["composite"], reverse=True)
                worst, best = scores[-1], scores[0]
                gap = best["composite"] - worst["composite"]
                if worst["composite"] < sell_th and best["composite"] > 65 and gap >= gap_th:
                    if not pd.isna(worst["fwd20"]) and not pd.isna(best["fwd20"]):
                        cnt += 1
                        improvement_sum += best["fwd20"] - worst["fwd20"]
            if cnt > 3:
                threshold_sensitivity.append({"sell_th": sell_th, "gap_th": gap_th, "n": cnt,
                                             "avg_improvement": improvement_sum/cnt})
    results["threshold_sensitivity"] = threshold_sensitivity
    if threshold_sensitivity:
        best_ts = max(threshold_sensitivity, key=lambda x: x["avg_improvement"])
        logger.info(f"    最优: 卖<{best_ts['sell_th']}/差≥{best_ts['gap_th']}, N={best_ts['n']}, 增益={best_ts['avg_improvement']:+.2f}%")
    return results

# ============================================================
# 模块7: 17道风控关卡拦截率模拟 [NEW]
# ============================================================
def run_risk_gate_simulation(precomputed, idx_df, candidates, trade_dates):
    logger.info("\n" + "=" * 60)
    logger.info("  [7] 17道风控关卡拦截率模拟")
    logger.info("=" * 60)
    # 模拟各关卡的拦截效果
    # 关卡定义(简化版，对应risk_control.py的17道关卡)
    gates = {
        "关卡-1_熊市禁买": {"desc": "BEAR市禁止开仓", "check": lambda r: r == "BEAR"},
        "关卡0_满仓禁止": {"desc": "仓位≥90%禁止买入", "check": None},  # 特殊处理
        "关卡1_单股仓位": {"desc": "单股>15%拦截", "check": None},
        "关卡2_赛道集中": {"desc": "单赛道>40%拦截", "check": None},
        "关卡3_日亏损": {"desc": "日亏>3%暂停", "check": None},
        "关卡4_连亏暂停": {"desc": "连亏2笔暂停", "check": None},
        "关卡5_止损": {"desc": "个股-10%止损", "check": None},
        "关卡6_移动止损": {"desc": "浮盈回落6%止盈", "check": None},
        "关卡7_时间止损": {"desc": "持有>20天无盈利", "check": None},
    }
    # 统计各关卡在回测期间的"触发次数"
    gate_stats = {}
    # 关卡-1: 熊市禁买
    bear_days = sum(1 for d in trade_dates if get_market_regime(idx_df, d) == "BEAR")
    gate_stats["关卡-1_熊市禁买"] = {"triggered": bear_days, "total": len(trade_dates),
                                     "rate": bear_days/len(trade_dates)*100, "desc": "BEAR市天数(禁止开仓)"}
    # 关卡5: 止损触发
    stop_count = 0; trail_count = 0; time_stop = 0
    buy_events = []
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL):
        scan_date = trade_dates[scan_idx]
        regime = get_market_regime(idx_df, scan_date)
        if regime == "BEAR": continue
        for code in candidates[:20]:
            df = precomputed[code]
            mask = df["date"] <= scan_date
            if mask.sum() < 60: continue
            idx = mask.sum() - 1
            factors = calc_canslim_factors(df, idx, idx_df)
            if factors and factors["total"] >= 50:
                buy_events.append({"code": code, "idx": idx, "date": scan_date, "close": df["close"].iloc[idx]})
    for ev in buy_events:
        df = precomputed[ev["code"]]
        idx = ev["idx"]
        buy_price = ev["close"]
        stopped = False
        for d in range(1, min(21, len(df) - idx)):
            price = df["close"].iloc[idx+d]
            pnl = (price - buy_price) / buy_price
            if pnl <= -0.10:
                stop_count += 1; stopped = True; break
            if pnl > 0.15:
                highest = max(buy_price, df["high"].iloc[idx:idx+d+1].max())
                if (highest - price) / highest > 0.06:
                    trail_count += 1; stopped = True; break
        if not stopped:
            if idx + 20 < len(df):
                pnl20 = (df["close"].iloc[idx+20] - buy_price) / buy_price
                if pnl20 <= 0: time_stop += 1
    total_buys = len(buy_events)
    gate_stats["关卡5_止损"] = {"triggered": stop_count, "total": total_buys,
                                "rate": stop_count/max(total_buys,1)*100, "desc": "10%止损触发"}
    gate_stats["关卡6_移动止损"] = {"triggered": trail_count, "total": total_buys,
                                    "rate": trail_count/max(total_buys,1)*100, "desc": "浮盈回落6%止盈"}
    gate_stats["关卡7_时间止损"] = {"triggered": time_stop, "total": total_buys,
                                    "rate": time_stop/max(total_buys,1)*100, "desc": "20天无盈利"}
    # 关卡4: 连亏
    consec_count = 0
    for i in range(2, len(buy_events)):
        ev1, ev2 = buy_events[i-1], buy_events[i-2]
        df1, df2 = precomputed[ev1["code"]], precomputed[ev2["code"]]
        idx1, idx2 = ev1["idx"], ev2["idx"]
        if idx1+10 < len(df1) and idx2+10 < len(df2):
            r1 = (df1["close"].iloc[idx1+10] - ev1["close"]) / ev1["close"]
            r2 = (df2["close"].iloc[idx2+10] - ev2["close"]) / ev2["close"]
            if r1 < 0 and r2 < 0: consec_count += 1
    gate_stats["关卡4_连亏暂停"] = {"triggered": consec_count, "total": max(len(buy_events)-2, 1),
                                    "rate": consec_count/max(len(buy_events)-2,1)*100, "desc": "连亏2笔触发"}
    # 输出
    logger.info(f"  总买入事件: {total_buys}")
    for gate_name, st in sorted(gate_stats.items()):
        logger.info(f"    {gate_name}: 触发{st['triggered']}次({st['rate']:.1f}%) - {st['desc']}")
    return gate_stats

# ============================================================
# HTML报告生成 [NEW]
# ============================================================
def generate_html_report(results, output_path):
    """生成完整HTML诊断报告"""
    r1, r2, r3, r4, r5, r6, r7 = results
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>策略诊断报告 V2</title>
<style>
body {{ font-family: 'Microsoft YaHei', sans-serif; margin: 20px; background: #f5f5f5; }}
.container {{ max-width: 1200px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
h1 {{ color: #1a237e; border-bottom: 3px solid #1a237e; padding-bottom: 10px; }}
h2 {{ color: #283593; margin-top: 30px; border-left: 4px solid #3f51b5; padding-left: 12px; }}
h3 {{ color: #3949ab; }}
table {{ border-collapse: collapse; width: 100%; margin: 15px 0; font-size: 13px; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: center; }}
th {{ background: #e8eaf6; color: #1a237e; font-weight: bold; }}
tr:nth-child(even) {{ background: #f5f5f5; }}
.good {{ color: #2e7d32; font-weight: bold; }}
.bad {{ color: #c62828; font-weight: bold; }}
.warn {{ color: #f57f17; font-weight: bold; }}
.metric {{ display: inline-block; margin: 10px; padding: 15px 25px; background: #e8eaf6; border-radius: 8px; text-align: center; }}
.metric .value {{ font-size: 24px; font-weight: bold; color: #1a237e; }}
.metric .label {{ font-size: 12px; color: #666; margin-top: 5px; }}
.alert {{ padding: 12px 16px; border-radius: 4px; margin: 10px 0; }}
.alert-danger {{ background: #ffebee; border-left: 4px solid #c62828; }}
.alert-warning {{ background: #fff8e1; border-left: 4px solid #f57f17; }}
.alert-success {{ background: #e8f5e9; border-left: 4px solid #2e7d32; }}
</style></head><body><div class="container">
<h1>📊 全面策略诊断报告 V2</h1>
<p>回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,.0f}元 | 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
"""
    # 绩效总览
    p = r4
    html += f"""<h2>一、绩效总览</h2>
<div>
<div class="metric"><div class="value {'bad' if p['total_return']<0 else 'good'}">{p['total_return']*100:+.1f}%</div><div class="label">总收益率</div></div>
<div class="metric"><div class="value {'bad' if p['annual_return']<0 else 'good'}">{p['annual_return']*100:+.1f}%</div><div class="label">年化收益</div></div>
<div class="metric"><div class="value {'bad' if p['max_drawdown']>0.2 else 'good'}">{p['max_drawdown']*100:.1f}%</div><div class="label">最大回撤</div></div>
<div class="metric"><div class="value {'bad' if p['sharpe']<1 else 'good'}">{p['sharpe']:.2f}</div><div class="label">夏普比率</div></div>
<div class="metric"><div class="value {'bad' if p['calmar']<1 else 'good'}">{p['calmar']:.2f}</div><div class="label">Calmar比率</div></div>
<div class="metric"><div class="value {'bad' if p['win_rate']<40 else 'good'}">{p['win_rate']:.1f}%</div><div class="label">胜率</div></div>
<div class="metric"><div class="value {'bad' if p['profit_factor']<2.5 else 'good'}">{p['profit_factor']:.2f}</div><div class="label">盈亏比</div></div>
<div class="metric"><div class="value">{p['total_trades']}</div><div class="label">交易笔数</div></div>
</div>
"""
    # 分年度
    html += "<h3>分年度绩效</h3><table><tr><th>年度</th><th>收益率</th><th>最大回撤</th><th>判定</th></tr>"
    for year, ys in sorted(p["yearly"].items()):
        cls = "good" if ys["return"] > 0 else "bad"
        judge = "✅" if ys["return"] > 5 else ("⚠️" if ys["return"] > -5 else "❌")
        html += f"<tr><td>{year}</td><td class='{cls}'>{ys['return']:+.1f}%</td><td>{ys['max_dd']:.1f}%</td><td>{judge}</td></tr>"
    html += "</table>"
    # CANSLIM因子
    html += "<h2>二、CANSLIM因子有效性</h2>"
    html += "<h3>评分区间分组</h3><table><tr><th>分组</th><th>样本数</th><th>5d收益</th><th>10d收益</th><th>20d收益</th><th>20d胜率</th></tr>"
    for st in r1.get("score_stats", []):
        html += f"<tr><td>{st['group']}</td><td>{st['n']}</td><td>{st['avg_5d']:+.2f}%</td><td>{st['avg_10d']:+.2f}%</td><td>{st['avg_20d']:+.2f}%</td><td>{st['wr_20d']:.1f}%</td></tr>"
    html += "</table>"
    # 因子IC
    html += "<h3>因子IC热力图(20d收益差)</h3><table><tr><th>因子</th><th>整体spread</th><th>BULL</th><th>RANGE</th><th>BEAR</th><th>判定</th></tr>"
    for fi in r1.get("factor_ic", []):
        ic = fi["ic_by_regime"]
        judge = "✅有效" if fi["spread_20d"] > 0.5 else ("❌无效" if fi["spread_20d"] < 0 else "⚠️弱")
        cls = "good" if fi["spread_20d"] > 0.5 else ("bad" if fi["spread_20d"] < 0 else "warn")
        bull_str = f"{ic['BULL']:+.2f}%" if 'BULL' in ic else 'N/A'
        range_str = f"{ic['RANGE']:+.2f}%" if 'RANGE' in ic else 'N/A'
        bear_str = f"{ic['BEAR']:+.2f}%" if 'BEAR' in ic else 'N/A'
        html += f"<tr><td><b>{fi['factor']}</b></td><td class='{cls}'>{fi['spread_20d']:+.2f}%</td>"
        html += f"<td>{bull_str}</td><td>{range_str}</td><td>{bear_str}</td>"
        html += f"<td>{judge}</td></tr>"
    html += "</table>"
    # 盘中预警
    html += "<h2>三、盘中预警双通道回测</h2>"
    if "channel_a" in r2:
        ca = r2["channel_a"]
        html += f"""<div class="alert alert-{'success' if ca['avg_5d']>0 else 'warning'}">
<b>通道A</b>(涨≥2%+量比≥1.5): N={ca['n']} | T+1={ca['avg_1d']:+.2f}%(WR{ca['wr_1d']:.0f}%) | T+5={ca['avg_5d']:+.2f}%(WR{ca['wr_5d']:.0f}%) | 误报率={ca['miss_rate']:.1f}%</div>"""
    if "channel_b" in r2:
        cb = r2["channel_b"]
        html += f"""<div class="alert alert-{'success' if cb['avg_5d']>0 else 'warning'}">
<b>通道B</b>(涨≥5%+量比≥2+额≥3亿): N={cb['n']} | T+1={cb['avg_1d']:+.2f}%(WR{cb['wr_1d']:.0f}%) | T+5={cb['avg_5d']:+.2f}%(WR{cb['wr_5d']:.0f}%) | 误报率={cb['miss_rate']:.1f}%</div>"""
    if r2.get("threshold_tests"):
        html += "<h3>阈值敏感性</h3><table><tr><th>涨幅阈值</th><th>量比阈值</th><th>信号数</th><th>5d收益</th><th>5d胜率</th></tr>"
        for tt in sorted(r2["threshold_tests"], key=lambda x: x["avg_5d"], reverse=True)[:10]:
            html += f"<tr><td>≥{tt['pct_th']}%</td><td>≥{tt['vr_th']}</td><td>{tt['n']}</td><td>{tt['avg_5d']:+.2f}%</td><td>{tt['wr_5d']:.0f}%</td></tr>"
        html += "</table>"
    # 风控
    html += "<h2>四、风控体系有效性</h2>"
    html += "<h3>止损线对比</h3><table><tr><th>止损线</th><th>触发次数</th><th>触发率</th><th>有效率</th><th>误杀次数</th><th>平均PnL</th></tr>"
    for st in r3.get("stop_comparison", []):
        html += f"<tr><td>{st['stop_pct']:.0f}%</td><td>{st['triggered']}</td><td>{st['trigger_rate']:.1f}%</td><td>{st['save_rate']:.0f}%</td><td>{st['hurt']}</td><td>{st['avg_pnl']:+.2f}%</td></tr>"
    html += "</table>"
    if "consecutive_loss" in r3:
        cl = r3["consecutive_loss"]
        html += f"""<div class="alert alert-danger"><b>连亏后继续交易</b>: avg10d={cl['after_loss_avg']:+.2f}%(WR{cl['after_loss_wr']:.0f}%) vs 正常={cl['normal_avg']:+.2f}%(WR{cl['normal_wr']:.0f}%) → 差距{cl['normal_avg']-cl['after_loss_avg']:.2f}%</div>"""
    # Composite
    html += "<h2>五、Composite评分预测力</h2>"
    html += "<table><tr><th>分组</th><th>样本数</th><th>5d收益</th><th>20d收益</th><th>20d胜率</th></tr>"
    for st in r5.get("group_stats", []):
        html += f"<tr><td>{st['group']}</td><td>{st['n']}</td><td>{st['avg_5d']:+.2f}%</td><td>{st['avg_20d']:+.2f}%</td><td>{st['wr_20d']:.1f}%</td></tr>"
    html += "</table>"
    # 调仓
    html += "<h2>六、调仓逻辑验证</h2>"
    if "swap_stats" in r6:
        ss = r6["swap_stats"]
        html += f"""<div class="alert alert-{'success' if ss['improvement']>0 else 'warning'}">
调仓触发{r6['swap_count']}次 | 卖出标的20d={ss['avg_sell_fwd20']:+.2f}% | 买入标的20d={ss['avg_buy_fwd20']:+.2f}% | <b>增益={ss['improvement']:+.2f}%</b></div>"""
    # 风控关卡
    html += "<h2>七、风控关卡拦截率</h2>"
    html += "<table><tr><th>关卡</th><th>触发次数</th><th>总数</th><th>拦截率</th><th>说明</th></tr>"
    for gate_name, st in sorted(r7.items()):
        html += f"<tr><td>{gate_name}</td><td>{st['triggered']}</td><td>{st['total']}</td><td>{st['rate']:.1f}%</td><td>{st['desc']}</td></tr>"
    html += "</table>"
    html += "</div></body></html>"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"  HTML报告: {output_path}")

# ============================================================
# 主函数
# ============================================================
def run():
    t0 = time.time()
    print("=" * 60)
    print("  全面策略诊断回测 V2（增强版）")
    print(f"  区间: {START_DATE} ~ {END_DATE} | 资金: {INITIAL_CAPITAL:,.0f}")
    print("=" * 60)
    data_dict = load_data()
    if len(data_dict) < 10:
        print("[FAIL] 数据不足"); return
    precomputed = {code: compute_indicators(df) for code, df in data_dict.items()}
    idx_df = precomputed.get("000300")
    if idx_df is None:
        print("[FAIL] 无000300基准"); return
    calendar = idx_df["date"].values
    trade_dates = calendar[(calendar >= START_DATE) & (calendar <= END_DATE)]
    candidates = [c for c in precomputed if c != "000300" and not c.startswith("588") and not c.startswith("159")]
    logger.info(f"候选标的: {len(candidates)}只, 交易日: {len(trade_dates)}天")
    # 运行七大模块
    r1 = run_factor_analysis(precomputed, idx_df, candidates, trade_dates)
    r2 = run_intraday_threshold_backtest(precomputed, idx_df, candidates, trade_dates)
    r3 = run_risk_control_backtest(precomputed, idx_df, candidates, trade_dates)
    r4 = run_portfolio_backtest(precomputed, idx_df, candidates, trade_dates)
    r5 = run_composite_analysis(precomputed, idx_df, candidates, trade_dates)
    r6 = run_rebalance_backtest(precomputed, idx_df, candidates, trade_dates)
    r7 = run_risk_gate_simulation(precomputed, idx_df, candidates, trade_dates)
    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  回测完成! 耗时: {elapsed:.1f}秒")
    print(f"{'='*60}")
    # 保存结果
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    today = datetime.date.today().strftime('%Y%m%d')
    json_path = os.path.join(config.OUTPUT_DIR, f"diagnosis_v2_{today}.json")
    save_data = {
        "factor": {k:v for k,v in r1.items() if k != "sig_df"},
        "intraday": r2, "risk": r3,
        "portfolio": {k:v for k,v in r4.items() if k != "daily_values"},
        "composite": r5, "rebalance": r6, "risk_gates": r7
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"  JSON: {json_path}")
    # HTML报告
    html_path = os.path.join(config.OUTPUT_DIR, f"diagnosis_v2_{today}.html")
    generate_html_report((r1, r2, r3, r4, r5, r6, r7), html_path)
    print(f"  HTML: {html_path}")
    return save_data

if __name__ == "__main__":
    run()
