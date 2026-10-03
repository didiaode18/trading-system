# -*- coding: utf-8 -*-
"""
V27 大盘系统性风险提前预警 — 历史回测验证
==========================================
核心目标: 验证V27各层指标能否在大跌发生前1-5个交易日提前发出预警信号

回测方法:
  1. 从stock_db.db加载沪深300 + 全市场日线数据 (2023-01 ~ 2026-09)
  2. 逐日计算市场宽度(breadth) = 个股在MA20上方的比例
  3. 识别大跌事件: 单日跌幅>2% 或 3日累计跌幅>5%
  4. 对每个交易日模拟运行 scan_systemic_risk_daily()，记录各层触发
  5. 统计: 各层在大跌前的命中率、领先天数、误报率

局限性:
  - L2(盘中跌幅硬止损)为盘中实时指标，日线回测无法模拟
  - L4(北向资金/融资余额)历史数据不在本地DB，回测中跳过
  - L5(波动率突变)在回测中额外实现
"""

import sys, os, sqlite3, warnings, datetime, logging, json
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')

# 路径设置
_bt_dir = os.path.dirname(os.path.abspath(__file__))
_proj = os.path.dirname(os.path.dirname(_bt_dir))  # 项目根目录
ts_dir = os.path.join(_proj, 'trading_system')
sys.path.insert(0, ts_dir)
sys.path.insert(0, _proj)
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("v27_bt")

# ============================================================
# 回测参数
# ============================================================
START_DATE = "2023-01-01"
END_DATE = "2026-09-10"
BENCHMARK = "000300"  # 沪深300

# 大跌事件定义
CRASH_1D_PCT = -2.0       # 单日跌幅阈值(%)
CRASH_3D_PCT = -5.0       # 3日累计跌幅阈值(%)
CRASH_5D_PCT = -7.0       # 5日累计跌幅阈值(%)

# V27各层参数(与config.py一致，回测中直接计算不依赖config)
MA_ACCEL_WINDOW = 5        # L1: 斜率回望窗口
MA_ACCEL_THRESHOLDS = [-0.05, -0.10, -0.15]  # L1: score 2/3/4 阈值
BREADTH_DROP_THRESH = 15   # L3: 5日宽度下降阈值(pp)
BREADTH_LOW_THRESH = 30    # L3: 宽度低阈值(%)
VOL_LOOKBACK_SHORT = 10    # L5: 短期波动率回望
VOL_LOOKBACK_LONG = 20     # L5: 长期波动率回望
VOL_RATIO_THRESH = 1.5     # L5: 短/长波动率比>此值→放大


# ============================================================
# 一、数据加载
# ============================================================
def load_data():
    """加载沪深300 + 全市场日线数据"""
    db_path = config.DB_PATH
    conn = sqlite3.connect(db_path)
    
    # 沪深300
    bm_df = pd.read_sql(
        "SELECT date,open,close,high,low,volume FROM daily_kline "
        "WHERE code=? AND date>=? ORDER BY date",
        conn, params=(BENCHMARK, START_DATE))
    
    # 全市场(用于计算宽度)
    all_df = pd.read_sql(
        "SELECT code,date,close FROM daily_kline "
        "WHERE date>=? ORDER BY code,date",
        conn, params=(START_DATE,))
    
    conn.close()
    
    for c in ['open', 'close', 'high', 'low', 'volume']:
        bm_df[c] = pd.to_numeric(bm_df[c], errors='coerce')
    all_df['close'] = pd.to_numeric(all_df['close'], errors='coerce')
    
    bm_df = bm_df.dropna(subset=['close']).reset_index(drop=True)
    all_df = all_df.dropna(subset=['close'])
    
    logger.info(f"沪深300: {len(bm_df)}条 ({bm_df['date'].min()} ~ {bm_df['date'].max()})")
    logger.info(f"全市场: {all_df['code'].nunique()}只, {len(all_df)}条")
    
    return bm_df, all_df


# ============================================================
# 二、计算市场宽度(breadth) — 每日个股在MA20上方的比例
# ============================================================
def compute_breadth_series(all_df):
    """计算每日市场宽度: 收盘价在MA20上方的个股占比(%)"""
    # 为每只股票计算MA20
    all_df = all_df.copy()
    all_df['ma20'] = all_df.groupby('code')['close'].transform(lambda x: x.rolling(20).mean())
    
    # 过滤掉MA20为NaN的(上市不足20日)
    valid = all_df.dropna(subset=['ma20'])
    
    # 每日统计
    daily = valid.groupby('date').apply(
        lambda g: round((g['close'] > g['ma20']).sum() / len(g) * 100, 1) 
        if len(g) > 0 else 50.0
    ).reset_index(name='breadth')
    
    return daily


# ============================================================
# 三、识别大跌事件
# ============================================================
def identify_crash_events(bm_df):
    """识别沪深300大跌事件"""
    bm = bm_df.copy()
    bm['pct_1d'] = bm['close'].pct_change() * 100
    bm['pct_3d'] = bm['close'].pct_change(3) * 100
    bm['pct_5d'] = bm['close'].pct_change(5) * 100
    
    events = []
    used_dates = set()
    
    for i in range(1, len(bm)):
        date = str(bm.iloc[i]['date'])
        if date in used_dates:
            continue
        
        pct_1d = bm.iloc[i]['pct_1d']
        pct_3d = bm.iloc[i]['pct_3d'] if not pd.isna(bm.iloc[i]['pct_3d']) else 0
        pct_5d = bm.iloc[i]['pct_5d'] if not pd.isna(bm.iloc[i]['pct_5d']) else 0
        
        is_crash = False
        crash_type = ""
        
        if not pd.isna(pct_1d) and pct_1d <= CRASH_1D_PCT:
            is_crash = True
            crash_type = f"单日{pct_1d:.1f}%"
        if not pd.isna(pct_3d) and pct_3d <= CRASH_3D_PCT:
            is_crash = True
            crash_type += f" | 3日{pct_3d:.1f}%"
        if not pd.isna(pct_5d) and pct_5d <= CRASH_5D_PCT:
            is_crash = True
            crash_type += f" | 5日{pct_5d:.1f}%"
        
        if is_crash:
            events.append({
                'date': date,
                'idx': i,
                'close': bm.iloc[i]['close'],
                'pct_1d': round(pct_1d, 2) if not pd.isna(pct_1d) else 0,
                'pct_3d': round(pct_3d, 2) if not pd.isna(pct_3d) else 0,
                'pct_5d': round(pct_5d, 2) if not pd.isna(pct_5d) else 0,
                'type': crash_type,
            })
            # 冷却: 同一次大跌5日内不重复计数
            for j in range(max(0, i-2), min(len(bm), i+3)):
                used_dates.add(str(bm.iloc[j]['date']))
    
    logger.info(f"大跌事件: {len(events)}次")
    for e in events:
        logger.info(f"  {e['date']}: {e['type']} (close={e['close']:.2f})")
    
    return events


# ============================================================
# 四、逐日V27信号回放
# ============================================================
def replay_v27_signals(bm_df, breadth_df):
    """逐日模拟V27系统性风险扫描
    
    返回: {date: {L1: {...}, L3: {...}, L5: {...}, max_level: str}}
    """
    # 构建宽度字典
    breadth_dict = dict(zip(breadth_df['date'].astype(str), breadth_df['breadth']))
    
    results = {}
    dates = [str(d) for d in bm_df['date'].values]
    
    for i in range(30, len(bm_df)):  # 需要至少30日数据计算MA20
        date = dates[i]
        if date < START_DATE or date > END_DATE:
            continue
        
        # 截取到当日的DataFrame
        sub_df = bm_df.iloc[:i+1].copy()
        close = sub_df['close'].values
        
        # ---- L1: MA20斜率加速度 ----
        l1_result = _calc_l1_ma_accel(close)
        
        # ---- L3: 宽度收窄速率 ----
        # 获取最近5个交易日的宽度
        recent_dates = dates[max(0, i-4):i+1]
        breadth_history = [breadth_dict.get(d, 50.0) for d in recent_dates]
        breadth_now = breadth_dict.get(date, 50.0)
        l3_result = _calc_l3_breadth(breadth_now, breadth_history)
        
        # ---- L5: 波动率突变 ----
        l5_result = _calc_l5_volatility(close)
        
        # 综合级别
        max_level = None
        level_rank = {"warm": 1, "ready": 2, "force": 3}
        for r in [l1_result, l3_result, l5_result]:
            lv = r.get("level")
            if lv and (max_level is None or level_rank.get(lv, 0) > level_rank.get(max_level, 0)):
                max_level = lv
        
        # 共振升级: L1+L3 → 至少ready
        if l1_result.get("score", 0) >= 2 and l3_result.get("triggered"):
            if max_level is None or level_rank.get(max_level, 0) < level_rank.get("ready", 0):
                max_level = "ready"
        
        results[date] = {
            "L1": l1_result,
            "L3": l3_result,
            "L5": l5_result,
            "max_level": max_level,
            "breadth": breadth_now,
        }
    
    return results


def _calc_l1_ma_accel(close):
    """L1: MA20斜率加速度(五级评分)"""
    if len(close) < 30:
        return {"triggered": False, "level": None, "score": 0}
    
    ma20 = pd.Series(close).rolling(20).mean()
    w = MA_ACCEL_WINDOW
    
    if pd.isna(ma20.iloc[-1]) or pd.isna(ma20.iloc[-(w+1)]):
        return {"triggered": False, "level": None, "score": 0}
    
    slope_now = (ma20.iloc[-1] - ma20.iloc[-(w+1)]) / ma20.iloc[-(w+1)] * 100
    
    lookback = w + 5
    if len(ma20) < lookback + 1 or pd.isna(ma20.iloc[-(lookback+1)]):
        return {"triggered": False, "level": None, "score": 0}
    
    slope_5d = (ma20.iloc[-(w+1)] - ma20.iloc[-(lookback+1)]) / ma20.iloc[-(lookback+1)] * 100
    accel = slope_now - slope_5d
    
    if accel >= 0:
        score = 0
    elif accel >= -0.05:
        score = 1
    elif accel >= -0.10:
        score = 2
    elif accel >= -0.15 or slope_now >= -0.1:
        score = 3
    else:
        score = 4
    
    level = None
    if score >= 4:
        level = "force"
    elif score >= 2:
        level = "ready"
    elif score >= 1:
        level = "warm"
    
    return {"triggered": score >= 2, "level": level, "score": score,
            "accel": round(accel, 4), "slope_now": round(slope_now, 4)}


def _calc_l3_breadth(breadth_now, breadth_history):
    """L3: 宽度收窄速率"""
    if not breadth_history or len(breadth_history) < 2:
        if breadth_now < BREADTH_LOW_THRESH:
            return {"triggered": True, "level": "warm", "breadth": breadth_now}
        return {"triggered": False, "level": None, "breadth": breadth_now}
    
    breadth_5d_ago = breadth_history[0]
    change = breadth_now - breadth_5d_ago
    
    level = None
    if change <= -BREADTH_DROP_THRESH:
        level = "ready"
    elif breadth_now < BREADTH_LOW_THRESH and change < -5:
        level = "warm"
    elif change <= -(BREADTH_DROP_THRESH * 0.6):
        level = "warm"
    
    return {"triggered": level is not None, "level": level,
            "breadth": breadth_now, "change": round(change, 1)}


def _calc_l5_volatility(close):
    """L5: 波动率突变(短期/长期波动率比)"""
    if len(close) < VOL_LOOKBACK_LONG + 5:
        return {"triggered": False, "level": None}
    
    returns = pd.Series(close).pct_change().dropna()
    vol_short = returns.iloc[-VOL_LOOKBACK_SHORT:].std() * np.sqrt(252) * 100
    vol_long = returns.iloc[-VOL_LOOKBACK_LONG:].std() * np.sqrt(252) * 100
    
    if vol_long == 0 or pd.isna(vol_long):
        return {"triggered": False, "level": None}
    
    ratio = vol_short / vol_long
    
    level = None
    if ratio >= 2.0:
        level = "ready"   # 波动率翻倍→准备减仓
    elif ratio >= VOL_RATIO_THRESH:
        level = "warm"    # 波动率放大→风险升温
    
    return {"triggered": level is not None, "level": level,
            "vol_ratio": round(ratio, 2),
            "vol_short": round(vol_short, 1), "vol_long": round(vol_long, 1)}


# ============================================================
# 五、统计分析
# ============================================================
def analyze_lead_time(events, signals):
    """分析各层信号在大跌前的触发情况"""
    all_dates = sorted(signals.keys())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    
    layer_names = ["L1", "L3", "L5"]
    results = {ln: {"hits": [], "lead_days": [], "false_alarms": 0, "total_triggers": 0}
               for ln in layer_names}
    results["综合"] = {"hits": [], "lead_days": [], "false_alarms": 0, "total_triggers": 0}
    
    # 对每个大跌事件，查找提前触发
    for ev in events:
        crash_date = ev['date']
        crash_idx = date_to_idx.get(crash_date)
        if crash_idx is None:
            continue
        
        for ln in layer_names + ["综合"]:
            # 往前查1-10天
            first_trigger = None
            lead = None
            for lookback in range(1, 11):  # 1~10天
                if crash_idx - lookback < 0:
                    break
                prev_date = all_dates[crash_idx - lookback]
                sig = signals.get(prev_date, {})
                
                if ln == "综合":
                    triggered = sig.get("max_level") is not None
                else:
                    layer_sig = sig.get(ln, {})
                    triggered = layer_sig.get("triggered", False)
                
                if triggered:
                    first_trigger = prev_date
                    lead = lookback
            
            if lead is not None:
                results[ln]["hits"].append({
                    "crash_date": crash_date,
                    "trigger_date": first_trigger,
                    "lead_days": lead,
                    "crash_detail": ev['type'],
                })
                results[ln]["lead_days"].append(lead)
    
    # 统计各层总触发次数和误报
    for date, sig in signals.items():
        for ln in layer_names:
            layer_sig = sig.get(ln, {})
            if layer_sig.get("triggered"):
                results[ln]["total_triggers"] += 1
        if sig.get("max_level") is not None:
            results["综合"]["total_triggers"] += 1
    
    # 误报 = 触发但后续5日无大跌
    for ln in layer_names + ["综合"]:
        hit_dates = set(h["trigger_date"] for h in results[ln]["hits"])
        for date, sig in signals.items():
            if ln == "综合":
                triggered = sig.get("max_level") is not None
            else:
                triggered = sig.get(ln, {}).get("triggered", False)
            
            if triggered:
                # 检查触发后5日内是否有大跌
                d_idx = date_to_idx.get(date)
                if d_idx is None:
                    continue
                has_crash_after = False
                for fwd in range(1, 6):
                    if d_idx + fwd >= len(all_dates):
                        break
                    fwd_date = all_dates[d_idx + fwd]
                    if any(e['date'] == fwd_date for e in events):
                        has_crash_after = True
                        break
                if not has_crash_after:
                    results[ln]["false_alarms"] += 1
    
    return results


def analyze_m_factor_lag(bm_df, events):
    """对比: M因子(传统MA20/MA60判定)的滞后天数"""
    bm = bm_df.copy()
    bm['ma20'] = bm['close'].rolling(20).mean()
    bm['ma60'] = bm['close'].rolling(60).mean()
    
    lag_results = []
    for ev in events:
        crash_date = ev['date']
        crash_idx = bm[bm['date'].astype(str) == crash_date].index
        if len(crash_idx) == 0:
            continue
        ci = crash_idx[0]
        
        # 找到M因子首次判定为"down"的日期
        # down条件: close < MA20 AND close < MA60, 或 MA20 < MA60 AND close < MA20
        m_down_date = None
        for j in range(max(0, ci-30), ci+1):
            row = bm.iloc[j]
            if pd.isna(row['ma20']) or pd.isna(row['ma60']):
                continue
            if row['close'] < row['ma20'] and row['close'] < row['ma60']:
                m_down_date = str(row['date'])
                break
            if row['ma20'] < row['ma60'] and row['close'] < row['ma20']:
                m_down_date = str(row['date'])
                break
        
        if m_down_date:
            # 计算滞后天数
            try:
                d1 = datetime.datetime.strptime(m_down_date, "%Y-%m-%d")
                d2 = datetime.datetime.strptime(crash_date, "%Y-%m-%d")
                lag = (d2 - d1).days
            except:
                lag = None
            lag_results.append({
                "crash_date": crash_date,
                "m_down_date": m_down_date,
                "lag_days": lag,
            })
    
    return lag_results


# ============================================================
# 六、报告生成
# ============================================================
def generate_report(events, signals, analysis, m_factor_lag, breadth_df):
    """生成完整回测报告"""
    lines = []
    lines.append("=" * 80)
    lines.append("  V27 大盘系统性风险提前预警 — 历史回测验证报告")
    lines.append("=" * 80)
    lines.append(f"  回测区间: {START_DATE} ~ {END_DATE}")
    lines.append(f"  基准指数: 沪深300({BENCHMARK})")
    lines.append(f"  大跌定义: 单日跌>{abs(CRASH_1D_PCT)}% | 3日累计跌>{abs(CRASH_3D_PCT)}% | 5日累计跌>{abs(CRASH_5D_PCT)}%")
    lines.append(f"  回测交易日: {len(signals)}天")
    lines.append("")
    
    # === 大跌事件清单 ===
    lines.append("━" * 80)
    lines.append("  一、历史大跌事件清单")
    lines.append("━" * 80)
    lines.append(f"  {'日期':<12} {'1日涨跌':>8} {'3日累计':>8} {'5日累计':>8} {'类型'}")
    lines.append("  " + "-" * 70)
    for ev in events:
        lines.append(f"  {ev['date']:<12} {ev['pct_1d']:>+7.1f}% {ev['pct_3d']:>+7.1f}% {ev['pct_5d']:>+7.1f}%  {ev['type']}")
    lines.append(f"\n  共 {len(events)} 次大跌事件")
    lines.append("")
    
    # === 各层命中率 ===
    lines.append("━" * 80)
    lines.append("  二、各层指标命中率与领先天数")
    lines.append("━" * 80)
    
    layer_labels = {"L1": "L1-MA斜率加速度", "L3": "L3-宽度收窄速率", 
                    "L5": "L5-波动率突变", "综合": "综合(共振升级)"}
    
    lines.append(f"  {'指标层':<18} {'命中':>4} {'总大跌':>6} {'命中率':>6} {'总触发':>6} "
                 f"{'误报':>4} {'误报率':>6} {'平均领先':>8} {'中位数':>6}")
    lines.append("  " + "-" * 76)
    
    for ln in ["L1", "L3", "L5", "综合"]:
        a = analysis[ln]
        n_hits = len(a["hits"])
        n_events = len(events)
        hit_rate = n_hits / n_events * 100 if n_events > 0 else 0
        n_triggers = a["total_triggers"]
        n_false = a["false_alarms"]
        false_rate = n_false / n_triggers * 100 if n_triggers > 0 else 0
        avg_lead = np.mean(a["lead_days"]) if a["lead_days"] else 0
        med_lead = np.median(a["lead_days"]) if a["lead_days"] else 0
        
        lines.append(f"  {layer_labels[ln]:<18} {n_hits:>4} {n_events:>6} {hit_rate:>5.0f}% "
                     f"{n_triggers:>6} {n_false:>4} {false_rate:>5.0f}% "
                     f"{avg_lead:>7.1f}天 {med_lead:>5.1f}天")
    lines.append("")
    
    # === 领先天数分布 ===
    lines.append("━" * 80)
    lines.append("  三、领先天数分布（提前N天触发）")
    lines.append("━" * 80)
    
    for ln in ["L1", "L3", "L5", "综合"]:
        a = analysis[ln]
        if not a["lead_days"]:
            lines.append(f"  {layer_labels[ln]}: 无命中记录")
            continue
        
        lines.append(f"\n  {layer_labels[ln]}:")
        # 按领先天数分组统计
        dist = {}
        for d in a["lead_days"]:
            bucket = f"{d}天"
            dist[bucket] = dist.get(bucket, 0) + 1
        
        # 按天数排序
        for d_str in sorted(dist.keys(), key=lambda x: int(x.replace("天", ""))):
            bar = "█" * dist[d_str]
            lines.append(f"    提前{d_str:>3}: {dist[d_str]:>2}次 {bar}")
    lines.append("")
    
    # === 逐事件详情 ===
    lines.append("━" * 80)
    lines.append("  四、逐事件信号触发详情")
    lines.append("━" * 80)
    
    for ev in events:
        lines.append(f"\n  ▶ {ev['date']}: {ev['type']}")
        for ln in ["L1", "L3", "L5"]:
            a = analysis[ln]
            hit = next((h for h in a["hits"] if h["crash_date"] == ev["date"]), None)
            if hit:
                lines.append(f"    {layer_labels[ln]}: ✅ 提前{hit['lead_days']}天触发 ({hit['trigger_date']})")
            else:
                lines.append(f"    {layer_labels[ln]}: ❌ 未触发")
        
        # 综合
        a = analysis["综合"]
        hit = next((h for h in a["hits"] if h["crash_date"] == ev["date"]), None)
        if hit:
            lines.append(f"    综合: ✅ 提前{hit['lead_days']}天触发")
        else:
            lines.append(f"    综合: ❌ 无信号")
    lines.append("")
    
    # === M因子滞后对比 ===
    lines.append("━" * 80)
    lines.append("  五、V27 vs M因子滞后性对比")
    lines.append("━" * 80)
    
    if m_factor_lag:
        lags = [r["lag_days"] for r in m_factor_lag if r["lag_days"] is not None]
        if lags:
            lines.append(f"  M因子(传统MA20/MA60)判定\"下降趋势\"的滞后天数:")
            lines.append(f"    平均: {np.mean(lags):.1f}天 | 中位数: {np.median(lags):.1f}天 | "
                        f"最大: {max(lags)}天 | 最小: {min(lags)}天")
            lines.append(f"    分布: {sorted(lags)}")
            lines.append("")
            
            # 对比V27
            v27_leads = analysis["综合"]["lead_days"]
            if v27_leads:
                lines.append(f"  V27综合信号领先天数:")
                lines.append(f"    平均: {np.mean(v27_leads):.1f}天 | 中位数: {np.median(v27_leads):.1f}天")
                improvement = np.mean(lags) - np.mean(v27_leads)
                lines.append(f"    → V27比M因子平均提前 {improvement:.1f} 天")
            else:
                lines.append(f"  V27综合信号: 无命中记录")
        else:
            lines.append("  M因子滞后数据不足")
    else:
        lines.append("  M因子滞后数据不足")
    lines.append("")
    
    # === 结论 ===
    lines.append("━" * 80)
    lines.append("  六、结论与建议")
    lines.append("━" * 80)
    
    # 自动分析结论
    hit_rates = {}
    for ln in ["L1", "L3", "L5", "综合"]:
        a = analysis[ln]
        hit_rates[ln] = len(a["hits"]) / len(events) * 100 if events else 0
    
    lines.append(f"  1. 命中率: L1={hit_rates['L1']:.0f}% | L3={hit_rates['L3']:.0f}% | "
                f"L5={hit_rates['L5']:.0f}% | 综合={hit_rates['综合']:.0f}%")
    
    best_layer = max(hit_rates, key=hit_rates.get)
    lines.append(f"  2. 最优单指标: {best_layer} (命中率{hit_rates[best_layer]:.0f}%)")
    
    v27_leads = analysis["综合"]["lead_days"]
    if v27_leads:
        lines.append(f"  3. V27综合领先天数: 平均{np.mean(v27_leads):.1f}天, 中位数{np.median(v27_leads):.1f}天")
    
    if m_factor_lag:
        m_lags = [r["lag_days"] for r in m_factor_lag if r["lag_days"] is not None]
        if m_lags and v27_leads:
            improvement = np.mean(m_lags) - np.mean(v27_leads)
            if improvement > 0:
                lines.append(f"  4. 结论: V27比传统M因子平均提前 {improvement:.1f} 天发出预警")
            else:
                lines.append(f"  4. 结论: V27与传统M因子效果相当，需结合其他维度评估")
    
    lines.append("")
    lines.append("  ─── 局限性说明 ───")
    lines.append("  1. L2(盘中跌幅硬止损)为盘中实时指标，日线回测无法模拟")
    lines.append("  2. L4(北向资金/融资余额)历史数据不在本地DB，回测中跳过")
    lines.append("  3. 宽度(breadth)基于数据库中177只股票计算，非全市场5000+只")
    lines.append("  4. 回测区间2023-2026，样本量有限，统计显著性需持续观察")
    lines.append("")
    lines.append("=" * 80)
    
    return "\n".join(lines)


# ============================================================
# 七、主函数
# ============================================================
def main():
    logger.info("=" * 60)
    logger.info("V27 大盘系统性风险提前预警 — 历史回测")
    logger.info("=" * 60)
    
    # 1. 加载数据
    logger.info("[1/5] 加载数据...")
    bm_df, all_df = load_data()
    
    # 2. 计算市场宽度
    logger.info("[2/5] 计算市场宽度...")
    breadth_df = compute_breadth_series(all_df)
    logger.info(f"  宽度序列: {len(breadth_df)}天, 均值{breadth_df['breadth'].mean():.1f}%, "
                f"最低{breadth_df['breadth'].min():.1f}%, 最高{breadth_df['breadth'].max():.1f}%")
    
    # 3. 识别大跌事件
    logger.info("[3/5] 识别大跌事件...")
    events = identify_crash_events(bm_df)
    
    # 4. 逐日V27信号回放
    logger.info("[4/5] 逐日V27信号回放...")
    signals = replay_v27_signals(bm_df, breadth_df)
    logger.info(f"  回测天数: {len(signals)}")
    
    # 统计各层触发总次数
    for ln in ["L1", "L3", "L5"]:
        cnt = sum(1 for s in signals.values() if s.get(ln, {}).get("triggered"))
        logger.info(f"  {ln}触发: {cnt}天")
    cnt_composite = sum(1 for s in signals.values() if s.get("max_level") is not None)
    logger.info(f"  综合触发: {cnt_composite}天")
    
    # 5. 统计分析
    logger.info("[5/5] 统计分析...")
    analysis = analyze_lead_time(events, signals)
    m_factor_lag = analyze_m_factor_lag(bm_df, events)
    
    # 生成报告
    report = generate_report(events, signals, analysis, m_factor_lag, breadth_df)
    print("\n" + report)
    
    # 保存
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
    os.makedirs(out_dir, exist_ok=True)
    
    report_path = os.path.join(out_dir, 'v27_systemic_risk_backtest.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    # JSON结果
    def _native(obj):
        if isinstance(obj, dict):
            return {k: _native(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [_native(v) for v in obj]
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj
    
    json_data = _native({
        "backtest_period": f"{START_DATE} ~ {END_DATE}",
        "crash_events": len(events),
        "backtest_days": len(signals),
        "analysis": {
            ln: {
                "hits": len(a["hits"]),
                "hit_rate": round(len(a["hits"]) / len(events) * 100, 1) if events else 0,
                "lead_days": a["lead_days"],
                "avg_lead": round(float(np.mean(a["lead_days"])), 1) if a["lead_days"] else 0,
                "median_lead": round(float(np.median(a["lead_days"])), 1) if a["lead_days"] else 0,
                "total_triggers": a["total_triggers"],
                "false_alarms": a["false_alarms"],
            } for ln, a in analysis.items()
        },
        "m_factor_lag": m_factor_lag,
        "events": events,
    })
    json_path = os.path.join(out_dir, 'v27_systemic_risk_backtest.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"报告: {report_path}")
    logger.info(f"JSON: {json_path}")


if __name__ == "__main__":
    main()
