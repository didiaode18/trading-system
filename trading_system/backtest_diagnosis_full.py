# -*- coding: utf-8 -*-
"""
全面策略诊断回测 - CANSLIM因子有效性 + 风控体系 + 盘中预警阈值
================================================================
覆盖用户要求的四大回测范围:
1. CANSLIM选股引擎五因子预测有效性(分市场环境/分年度)
2. 综合分析报告逻辑(Composite评分/调仓/盈亏比)
3. 盘中预警双通道阈值回测(T+1/T+3/T+5收益分布)
4. 风控体系有效性(止损保护/关卡拦截率/满仓禁止效果)

输出: output/backtest_diagnosis_YYYYMMDD.html + 控制台摘要
"""

import sys, os, time, logging, datetime, sqlite3, warnings, json
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("diagnosis")

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
    df["pct_change"] = df["close"].pct_change()
    # 量比
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]
    return df

def get_market_regime(idx_df, date):
    """判断市场环境: BULL/BEAR/RANGE"""
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
# CANSLIM因子评分(与stock_screener.py一致的简化版)
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
    # N因子(20分)
    n = 0
    high_60 = df["high"].iloc[idx-60:idx].max()
    if close >= high_60 * 0.98: n += 12
    if close > ma20 and ma20 > ma60: n += 8
    factors["N"] = min(n, 20)
    # S因子(20分)
    s = 0
    if not pd.isna(vol_ma20) and vol_ma20 > 0:
        vr = volume / vol_ma20
        if vr < 0.7 and close >= ma20 * 0.99: s += 12
        if vr > 1.5 and close > df["close"].iloc[max(0,idx-1)]: s += 8
        if idx >= 5 and df["volume"].iloc[idx-5:idx].mean() < vol_ma20 * 0.8: s += 5
    factors["S"] = min(s, 20)
    # L因子(20分)
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
    # P因子(20分)
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
# 模块1: CANSLIM因子有效性(分市场环境+分年度)
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
            # 硬筛: 成交额
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
    
    # 1A: 按评分区间分组(≥80/60-79/40-59/<40)
    logger.info("\n  --- 1A: 评分区间分组 ---")
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
        logger.info(f"    {label}: N={st['n']}, 5d={st['avg_5d']:+.2f}%, 10d={st['avg_10d']:+.2f}%, "
                    f"20d={st['avg_20d']:+.2f}%, WR20={st['wr_20d']:.1f}%")
    results["score_stats"] = score_stats
    
    # 1B: 分市场环境
    logger.info("\n  --- 1B: 分市场环境(高分组≥60 vs 低分组<40) ---")
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
        logger.info(f"    {regime}: 高分20d={st['high_20d']:+.2f}%(WR{st['high_wr']:.0f}%) vs "
                    f"低分={st['low_20d']:+.2f}%(WR{st['low_wr']:.0f}%), 差={st['spread']:+.2f}%")
    results["regime_stats"] = regime_stats
    
    # 1C: 分年度
    logger.info("\n  --- 1C: 分年度绩效(高分组≥60) ---")
    year_stats = []
    for year in sorted(sig_df["year"].unique()):
        sub = sig_df[(sig_df["year"] == year) & (sig_df["total"] >= 60)]
        if len(sub) < 10: continue
        st = {"year": year, "n": len(sub),
              "avg_20d": sub["20d"].mean(), "wr_20d": (sub["20d"]>0).mean()*100,
              "avg_5d": sub["5d"].mean()}
        year_stats.append(st)
        logger.info(f"    {year}: N={st['n']}, 5d={st['avg_5d']:+.2f}%, 20d={st['avg_20d']:+.2f}%, WR20={st['wr_20d']:.1f}%")
    results["year_stats"] = year_stats
    
    # 1D: 各因子IC(分位组收益差)
    logger.info("\n  --- 1D: 因子IC(高分位30% vs 低分位30% 的20d收益差) ---")
    factor_ic = []
    for fname in ["N", "S", "L", "P", "W"]:
        q70 = sig_df[fname].quantile(0.7)
        q30 = sig_df[fname].quantile(0.3)
        high = sig_df[sig_df[fname] >= q70]
        low = sig_df[sig_df[fname] <= q30]
        if len(high) < 10 or len(low) < 10: continue
        spread = high["20d"].mean() - low["20d"].mean()
        # 分市场环境IC
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
    
    # 1E: 硬筛效果验证
    logger.info("\n  --- 1E: 硬筛条件过滤效果 ---")
    # 对比: 有硬筛 vs 无硬筛的信号质量
    all_signals_no_filter = []
    for scan_idx in range(60, len(trade_dates), SCAN_INTERVAL * 4):  # 稀疏采样
        scan_date = trade_dates[scan_idx]
        for code in candidates[:15]:  # 取部分
            df = precomputed[code]
            mask = df["date"] <= scan_date
            if mask.sum() < 80: continue
            idx = mask.sum() - 1
            factors = calc_canslim_factors(df, idx, idx_df)
            if factors is None: continue
            close = df["close"].iloc[idx]
            if idx + 20 < len(df):
                fwd20 = (df["close"].iloc[idx+20] - close) / close * 100
            else:
                fwd20 = np.nan
            # 检查是否通过硬筛
            ma20_slope = df["ma20_slope"].iloc[idx]
            passes_filter = (not pd.isna(ma20_slope) and ma20_slope > 0 and close >= df["ma20"].iloc[idx])
            all_signals_no_filter.append({"passes": passes_filter, "fwd20": fwd20, "score": factors["total"]})
    
    filter_df = pd.DataFrame(all_signals_no_filter)
    if len(filter_df) > 20:
        passed = filter_df[filter_df["passes"]]
        failed = filter_df[~filter_df["passes"]]
        filter_effect = {"passed_n": len(passed), "failed_n": len(failed),
                        "passed_avg20": passed["fwd20"].mean() if len(passed)>0 else 0,
                        "failed_avg20": failed["fwd20"].mean() if len(failed)>0 else 0}
        logger.info(f"    通过硬筛: N={len(passed)}, avg20d={filter_effect['passed_avg20']:+.2f}%")
        logger.info(f"    未通过:   N={len(failed)}, avg20d={filter_effect['failed_avg20']:+.2f}%")
        logger.info(f"    过滤增益: {filter_effect['passed_avg20']-filter_effect['failed_avg20']:+.2f}%")
        results["filter_effect"] = filter_effect
    
    results["sig_df"] = sig_df
    return results

# ============================================================
# 模块2: 盘中预警双通道阈值回测
# ============================================================
def run_intraday_threshold_backtest(precomputed, idx_df, candidates, trade_dates):
    logger.info("\n" + "=" * 60)
    logger.info("  [2] 盘中预警双通道阈值回测")
    logger.info("=" * 60)
    
    # 模拟通道A: 赛道池内(涨≥2% + 量比≥1.5)
    # 模拟通道B: 全市场(涨≥5% + 量比≥2 + 成交额≥3亿)
    channel_a_signals = []
    channel_b_signals = []
    
    for scan_idx in range(60, len(trade_dates), 1):  # 每日扫描
        scan_date = trade_dates[scan_idx]
        for code in candidates:
            df = precomputed[code]
            mask = df["date"] == scan_date
            if not mask.any(): continue
            idx = df[mask].index[0]
            if idx < 20 or idx + 5 >= len(df): continue
            row = df.iloc[idx]
            pct = row["pct_change"]
            vr = row["vol_ratio"]
            if pd.isna(pct) or pd.isna(vr): continue
            close = row["close"]
            # 前瞻收益
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
    # 通道A统计
    if channel_a_signals:
        a_df = pd.DataFrame(channel_a_signals)
        results["channel_a"] = {
            "n": len(a_df),
            "avg_1d": a_df["1d"].mean(), "avg_3d": a_df["3d"].mean(), "avg_5d": a_df["5d"].mean(),
            "wr_1d": (a_df["1d"]>0).mean()*100, "wr_3d": (a_df["3d"]>0).mean()*100,
            "wr_5d": (a_df["5d"]>0).mean()*100,
            "miss_rate": (a_df["5d"] < -3).mean()*100  # 误报率(触发后跌>3%)
        }
        logger.info(f"  通道A(涨≥2%+量比≥1.5): N={len(a_df)}")
        logger.info(f"    T+1={results['channel_a']['avg_1d']:+.2f}%(WR{results['channel_a']['wr_1d']:.0f}%), "
                    f"T+3={results['channel_a']['avg_3d']:+.2f}%, T+5={results['channel_a']['avg_5d']:+.2f}%")
        logger.info(f"    误报率(5d跌>3%): {results['channel_a']['miss_rate']:.1f}%")
    
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
    
    # 阈值敏感性测试
    logger.info("\n  --- 阈值敏感性(通道A变体) ---")
    threshold_tests = []
    for pct_th in [1.5, 2.0, 3.0, 4.0]:
        for vr_th in [1.0, 1.5, 2.0, 2.5]:
            cnt = 0; fwd5_sum = 0; wr_cnt = 0
            for scan_idx in range(60, len(trade_dates), 3):  # 每3天采样
                scan_date = trade_dates[scan_idx]
                for code in candidates[:20]:
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
    # 输出最优组合
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
    
    # 3A: 止损保护效果(不同止损线对比)
    logger.info("\n  --- 3A: 止损线对比(5%/7%/10%/15%) ---")
    stop_results = []
    # 收集所有"买入事件"(score>=50的信号)
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
                buy_events.append({"code": code, "idx": idx, "date": scan_date,
                                  "close": df["close"].iloc[idx]})
    
    logger.info(f"  买入事件总数: {len(buy_events)}")
    
    for stop_pct in [0.05, 0.07, 0.10, 0.15]:
        triggered = 0; saved = 0; hurt = 0; total_pnl = 0
        for ev in buy_events:
            df = precomputed[ev["code"]]
            idx = ev["idx"]
            buy_price = ev["close"]
            stop_price = buy_price * (1 - stop_pct)
            # 模拟20日持有
            stopped = False
            for d in range(1, min(21, len(df) - idx)):
                if df["low"].iloc[idx+d] <= stop_price:
                    stopped = True
                    triggered += 1
                    # 止损后5日继续跌?
                    if idx+d+5 < len(df):
                        after_min = df["close"].iloc[idx+d:idx+d+5].min()
                        if after_min < stop_price * 0.97:
                            saved += 1  # 止损有效(后面继续跌)
                        else:
                            hurt += 1   # 止损无效(后面反弹了)
                    total_pnl += -stop_pct * 100
                    break
            if not stopped:
                # 持有20日的收益
                if idx + 20 < len(df):
                    ret = (df["close"].iloc[idx+20] - buy_price) / buy_price * 100
                    total_pnl += ret
        
        n = len(buy_events)
        stop_results.append({
            "stop_pct": stop_pct*100, "triggered": triggered,
            "trigger_rate": triggered/n*100 if n>0 else 0,
            "saved": saved, "hurt": hurt,
            "save_rate": saved/triggered*100 if triggered>0 else 0,
            "avg_pnl": total_pnl/n if n>0 else 0
        })
        logger.info(f"    止损{stop_pct*100:.0f}%: 触发{triggered}次({triggered/n*100:.1f}%), "
                    f"有效{saved}次({saved/max(triggered,1)*100:.0f}%), "
                    f"误杀{hurt}次, 平均PnL={total_pnl/n:+.2f}%")
    results["stop_comparison"] = stop_results
    
    # 3B: 满仓禁止加仓效果(模拟有/无该规则)
    logger.info("\n  --- 3B: 满仓禁止加仓规则效果 ---")
    # 简化: 统计在仓位>90%时买入的信号后续表现
    # vs 仓位<70%时买入的信号后续表现
    high_pos_signals = []  # 模拟"满仓时追买"
    low_pos_signals = []   # 正常仓位买入
    for i, ev in enumerate(buy_events):
        df = precomputed[ev["code"]]
        idx = ev["idx"]
        if idx + 10 >= len(df): continue
        fwd10 = (df["close"].iloc[idx+10] - ev["close"]) / ev["close"] * 100
        # 用"该日前5日涨幅>8%"模拟"满仓追高"场景
        if idx >= 5:
            recent_gain = (ev["close"] / df["close"].iloc[idx-5] - 1) * 100
            if recent_gain > 8:
                high_pos_signals.append(fwd10)
            else:
                low_pos_signals.append(fwd10)
    
    if high_pos_signals and low_pos_signals:
        results["full_position"] = {
            "high_pos_n": len(high_pos_signals), "high_pos_avg10": np.mean(high_pos_signals),
            "high_pos_wr": (np.array(high_pos_signals)>0).mean()*100,
            "low_pos_n": len(low_pos_signals), "low_pos_avg10": np.mean(low_pos_signals),
            "low_pos_wr": (np.array(low_pos_signals)>0).mean()*100
        }
        logger.info(f"    追高买入(5日涨>8%): N={len(high_pos_signals)}, "
                    f"avg10d={np.mean(high_pos_signals):+.2f}%, WR={results['full_position']['high_pos_wr']:.0f}%")
        logger.info(f"    正常买入:           N={len(low_pos_signals)}, "
                    f"avg10d={np.mean(low_pos_signals):+.2f}%, WR={results['full_position']['low_pos_wr']:.0f}%")
    
    # 3C: 连亏后暂停效果
    logger.info("\n  --- 3C: 连亏后暂停规则效果 ---")
    # 模拟: 连续2笔亏损后继续交易 vs 暂停3天
    # 统计"前2笔亏损后第3笔"的表现
    consecutive_loss_rets = []
    normal_rets = []
    for i in range(2, len(buy_events)):
        ev = buy_events[i]
        df = precomputed[ev["code"]]
        idx = ev["idx"]
        if idx + 10 >= len(df): continue
        fwd10 = (df["close"].iloc[idx+10] - ev["close"]) / ev["close"] * 100
        # 检查前2笔是否亏损
        prev1 = buy_events[i-1]
        prev2 = buy_events[i-2]
        df1 = precomputed[prev1["code"]]
        df2 = precomputed[prev2["code"]]
        idx1, idx2 = prev1["idx"], prev2["idx"]
        if idx1+10 < len(df1) and idx2+10 < len(df2):
            ret1 = (df1["close"].iloc[idx1+10] - prev1["close"]) / prev1["close"] * 100
            ret2 = (df2["close"].iloc[idx2+10] - prev2["close"]) / prev2["close"] * 100
            if ret1 < 0 and ret2 < 0:
                consecutive_loss_rets.append(fwd10)
            else:
                normal_rets.append(fwd10)
    
    if consecutive_loss_rets:
        results["consecutive_loss"] = {
            "after_loss_n": len(consecutive_loss_rets),
            "after_loss_avg": np.mean(consecutive_loss_rets),
            "after_loss_wr": (np.array(consecutive_loss_rets)>0).mean()*100,
            "normal_n": len(normal_rets),
            "normal_avg": np.mean(normal_rets) if normal_rets else 0,
            "normal_wr": (np.array(normal_rets)>0).mean()*100 if normal_rets else 0
        }
        logger.info(f"    连亏后继续: N={len(consecutive_loss_rets)}, "
                    f"avg10d={np.mean(consecutive_loss_rets):+.2f}%, WR={(np.array(consecutive_loss_rets)>0).mean()*100:.0f}%")
        logger.info(f"    正常情况:   N={len(normal_rets)}, "
                    f"avg10d={np.mean(normal_rets):+.2f}%, WR={(np.array(normal_rets)>0).mean()*100:.0f}%")
    
    return results

# ============================================================
# 模块4: 组合模拟(完整策略+分年度)
# ============================================================
def run_portfolio_backtest(precomputed, idx_df, candidates, trade_dates):
    logger.info("\n" + "=" * 60)
    logger.info("  [4] 组合模拟回测(完整策略)")
    logger.info("=" * 60)
    
    cash = INITIAL_CAPITAL
    positions = {}
    trades = []
    daily_values = []
    max_value = INITIAL_CAPITAL
    max_drawdown = 0
    
    for t_idx, date in enumerate(trade_dates):
        # 更新市值
        pv = cash
        for code, pos in list(positions.items()):
            df = precomputed[code]
            mask = df["date"] == date
            if mask.any():
                pv += pos["shares"] * df[mask].iloc[0]["close"]
        daily_values.append({"date": date, "value": pv})
        if pv > max_value: max_value = pv
        dd = (max_value - pv) / max_value
        if dd > max_drawdown: max_drawdown = dd
        
        # 止损检查
        for code in list(positions.keys()):
            pos = positions[code]
            df = precomputed[code]
            mask = df["date"] == date
            if not mask.any(): continue
            price = df[mask].iloc[0]["close"]
            pnl = (price - pos["buy_price"]) / pos["buy_price"]
            if pnl <= -0.10:  # 10%止损
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
        
        # 每周买入
        if t_idx % SCAN_INTERVAL != 0 or t_idx < 60: continue
        if len(positions) >= MAX_HOLDINGS: continue
        
        regime = get_market_regime(idx_df, date)
        if regime == "BEAR": continue  # 熊市禁止开仓
        
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
    
    # 分年度
    yearly = {}
    dv_df["year"] = dv_df["date"].astype(str).str[:4]
    for year, grp in dv_df.groupby("year"):
        if len(grp) < 20: continue
        yr_ret = (grp["value"].iloc[-1] / grp["value"].iloc[0] - 1) * 100
        yr_max_dd = 0
        peak = grp["value"].iloc[0]
        for v in grp["value"]:
            if v > peak: peak = v
            dd = (peak - v) / peak
            if dd > yr_max_dd: yr_max_dd = dd
        yearly[year] = {"return": yr_ret, "max_dd": yr_max_dd*100}
    
    logger.info(f"  最终资产: {final_value:,.0f} (初始{INITIAL_CAPITAL:,.0f})")
    logger.info(f"  总收益: {total_return*100:+.2f}%, 年化: {annual_return*100:+.2f}%")
    logger.info(f"  最大回撤: {max_drawdown*100:.2f}%, 夏普: {sharpe:.2f}, Calmar: {calmar:.2f}")
    logger.info(f"  交易: {len(sell_trades)}笔, 胜率: {win_rate:.1f}%, 盈亏比: {profit_factor:.2f}")
    logger.info(f"  分年度: {yearly}")
    
    return {"final_value": final_value, "total_return": total_return, "annual_return": annual_return,
            "max_drawdown": max_drawdown, "sharpe": sharpe, "calmar": calmar,
            "total_trades": len(sell_trades), "win_rate": win_rate, "profit_factor": profit_factor,
            "avg_win": avg_win, "avg_loss": avg_loss, "yearly": yearly, "daily_values": dv_df}

# ============================================================
# 主函数
# ============================================================
def run():
    t0 = time.time()
    print("=" * 60)
    print("  全面策略诊断回测")
    print(f"  区间: {START_DATE} ~ {END_DATE} | 资金: {INITIAL_CAPITAL:,.0f}")
    print("=" * 60)
    
    data_dict = load_data()
    if len(data_dict) < 10:
        print("[FAIL] 数据不足"); return
    
    # 预计算
    precomputed = {code: compute_indicators(df) for code, df in data_dict.items()}
    idx_df = precomputed.get("000300")
    if idx_df is None:
        print("[FAIL] 无000300基准"); return
    
    calendar = idx_df["date"].values
    trade_dates = calendar[(calendar >= START_DATE) & (calendar <= END_DATE)]
    candidates = [c for c in precomputed if c != "000300" and not c.startswith("588") and not c.startswith("159")]
    logger.info(f"候选标的: {len(candidates)}只, 交易日: {len(trade_dates)}天")
    
    # 运行四大模块
    r1 = run_factor_analysis(precomputed, idx_df, candidates, trade_dates)
    r2 = run_intraday_threshold_backtest(precomputed, idx_df, candidates, trade_dates)
    r3 = run_risk_control_backtest(precomputed, idx_df, candidates, trade_dates)
    r4 = run_portfolio_backtest(precomputed, idx_df, candidates, trade_dates)
    
    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  回测完成! 耗时: {elapsed:.1f}秒")
    print(f"{'='*60}")
    
    # 保存JSON结果
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    json_path = os.path.join(config.OUTPUT_DIR, f"diagnosis_{datetime.date.today().strftime('%Y%m%d')}.json")
    # 清理不可序列化对象
    save_data = {"factor": {k:v for k,v in r1.items() if k != "sig_df"},
                 "intraday": r2, "risk": r3, "portfolio": {k:v for k,v in r4.items() if k != "daily_values"}}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"  JSON: {json_path}")
    
    return {"factor": r1, "intraday": r2, "risk": r3, "portfolio": r4}

if __name__ == "__main__":
    run()
