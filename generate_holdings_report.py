# -*- coding: utf-8 -*-
"""
持仓综合分析报告生成器 V4（技术分析+条件单）
============================================
功能:
  1. 实时行情获取（腾讯API）
  2. 历史K线获取（baostock前复权）→ 计算技术指标
  3. 趋势分析（MA系统+MACD+RSI+布林带+ATR）
  4. 支撑/压力位计算
  5. 仓位风险预警（集中度/超限）
  6. 条件单生成（主模式：纯实时价）
  7. 综合评分 + 建议持有时间
  8. HTML报告 → 邮件发送
"""
import sys
import os
import io
import json
import datetime
import warnings
warnings.filterwarnings('ignore')

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_system'))

import numpy as np
import pandas as pd
import config
from notify.email_notify import send_email
from data.realtime import fetch_realtime_batch
from data.data_loader import fetch_stock_daily_baostock, _bs_logout
from strategy.recommend_engine import run_recommendation, generate_trading_plan

today = datetime.date.today().strftime("%Y-%m-%d")
now = datetime.datetime.now().strftime("%H:%M:%S")

# ============================================================
# 一、持仓列表（只需代码+名称，无需成本价）
# ============================================================
holdings_list = [
    {"code": "588000", "名称": "科创50", "赛道": "指数ETF"},
    {"code": "001309", "名称": "德明利", "赛道": "存储芯片"},
    {"code": "002558", "名称": "巨人网络", "赛道": "游戏"},
    {"code": "600036", "名称": "招商银行", "赛道": "银行"},
    {"code": "159205", "名称": "创业东财", "赛道": "指数ETF"},
    {"code": "002185", "名称": "华天科技", "赛道": "半导体封测"},
]

# 主模式参数
STOP_LOSS_PCT = 0.08       # 固定止损: 最新价跌8%
DRAWDOWN_FROM_HIGH = 0.05  # 高点回落5%触发
REBOUND_FROM_LOW = 0.02    # 低点反弹2%触发

# ============================================================
# 二、技术指标计算
# ============================================================
def compute_indicators(df):
    """计算全套技术指标"""
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["ma20_slope"] = df["ma20"].diff(3)

    # RSI(14)
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_dif"] = ema12 - ema26
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = 2 * (df["macd_dif"] - df["macd_dea"])

    # 布林带
    df["boll_mid"] = df["close"].rolling(20).mean()
    boll_std = df["close"].rolling(20).std()
    df["boll_upper"] = df["boll_mid"] + 2 * boll_std
    df["boll_lower"] = df["boll_mid"] - 2 * boll_std

    # ATR(14)
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.rolling(14).mean()

    return df


def analyze_technical(df, realtime_price=None):
    """对单只股票做技术面综合分析"""
    if df.empty or len(df) < 30:
        return {"valid": False, "error": "数据不足"}

    latest = df.iloc[-1]
    close = realtime_price if realtime_price and realtime_price > 0 else latest["close"]

    ma5 = latest.get("ma5", close)
    ma10 = latest.get("ma10", close)
    ma20 = latest.get("ma20", close)
    ma60 = latest.get("ma60", close)
    rsi = latest.get("rsi", 50)
    macd_dif = latest.get("macd_dif", 0)
    macd_dea = latest.get("macd_dea", 0)
    macd_hist = latest.get("macd_hist", 0)
    atr = latest.get("atr", close * 0.02)
    boll_upper = latest.get("boll_upper", close * 1.1)
    boll_lower = latest.get("boll_lower", close * 0.9)
    vol_ma20 = latest.get("vol_ma20", 0)
    volume = latest.get("volume", 0)
    ma20_slope = latest.get("ma20_slope", 0)

    # 处理NaN
    for v in [ma5, ma10, ma20, ma60, rsi, macd_dif, macd_dea, atr, boll_upper, boll_lower]:
        if pd.isna(v):
            v = close

    # ---- 趋势判断 ----
    trend_score = 0
    trend_signals = []

    if not pd.isna(ma5) and not pd.isna(ma10) and not pd.isna(ma20):
        if ma5 > ma10 > ma20:
            trend_score += 2
            trend_signals.append("均线多头排列")
        elif ma5 < ma10 < ma20:
            trend_score -= 2
            trend_signals.append("均线空头排列")

    if not pd.isna(ma20_slope):
        if ma20_slope > 0:
            trend_score += 1
            trend_signals.append("MA20向上")
        else:
            trend_score -= 1
            trend_signals.append("MA20向下")

    if not pd.isna(ma60) and close > ma60:
        trend_score += 1
        trend_signals.append("站上MA60")
    elif not pd.isna(ma60) and close < ma60:
        trend_score -= 1
        trend_signals.append("跌破MA60")

    # ---- 动量判断 ----
    momentum_score = 0
    momentum_signals = []

    if not pd.isna(rsi):
        if rsi > 70:
            momentum_score -= 1
            momentum_signals.append(f"RSI超买({rsi:.0f})")
        elif rsi < 30:
            momentum_score += 1
            momentum_signals.append(f"RSI超卖({rsi:.0f})")
        elif rsi > 55:
            momentum_score += 0.5
            momentum_signals.append(f"RSI偏强({rsi:.0f})")
        else:
            momentum_signals.append(f"RSI中性({rsi:.0f})")

    if not pd.isna(macd_dif) and not pd.isna(macd_dea):
        if macd_dif > macd_dea:
            momentum_score += 1
            momentum_signals.append("MACD金叉")
        else:
            momentum_score -= 1
            momentum_signals.append("MACD死叉")

    # 5日动量
    if len(df) >= 6:
        momentum_5d = (close - df["close"].iloc[-6]) / df["close"].iloc[-6] * 100
    else:
        momentum_5d = 0

    # ---- 量能分析 ----
    vol_ratio = volume / vol_ma20 if not pd.isna(vol_ma20) and vol_ma20 > 0 else 1.0
    vol_signal = ""
    if vol_ratio > 1.5:
        vol_signal = f"放量({vol_ratio:.1f}倍)"
    elif vol_ratio < 0.6:
        vol_signal = f"缩量({vol_ratio:.1f}倍)"
    else:
        vol_signal = f"量能正常({vol_ratio:.1f}倍)"

    # ---- 支撑/压力位 ----
    supports = []
    resistances = []
    if not pd.isna(ma20) and ma20 < close:
        supports.append(("MA20", ma20))
    elif not pd.isna(ma20) and ma20 > close:
        resistances.append(("MA20", ma20))
    if not pd.isna(ma60) and ma60 < close:
        supports.append(("MA60", ma60))
    elif not pd.isna(ma60) and ma60 > close:
        resistances.append(("MA60", ma60))
    if not pd.isna(boll_lower) and boll_lower < close:
        supports.append(("布林下轨", boll_lower))
    if not pd.isna(boll_upper) and boll_upper > close:
        resistances.append(("布林上轨", boll_upper))

    # 近20日高低点
    if len(df) >= 20:
        recent_high = df["high"].iloc[-20:].max()
        recent_low = df["low"].iloc[-20:].min()
        if recent_high > close:
            resistances.append(("20日高点", recent_high))
        if recent_low < close:
            supports.append(("20日低点", recent_low))

    supports.sort(key=lambda x: x[1], reverse=True)
    resistances.sort(key=lambda x: x[1])

    first_support = supports[0][1] if supports else close * 0.95
    first_resistance = resistances[0][1] if resistances else close * 1.05

    # ---- 综合评分 (0-100) ----
    composite = 50 + trend_score * 8 + momentum_score * 6
    composite = max(0, min(100, composite))

    # ---- 趋势方向文字 ----
    if composite >= 70:
        trend_dir = "📈 强势上涨"
    elif composite >= 55:
        trend_dir = "↗️ 偏多震荡"
    elif composite >= 45:
        trend_dir = "➡️ 横盘整理"
    elif composite >= 30:
        trend_dir = "↘️ 偏空震荡"
    else:
        trend_dir = "📉 弱势下跌"

    # ---- 建议持有时间（基于技术面）----
    if composite >= 70 and not pd.isna(ma20_slope) and ma20_slope > 0:
        hold_suggest = "15-20天"
        hold_reason = "多头趋势明确，MA20向上，可中线持有"
    elif composite >= 55:
        hold_suggest = "10-15天"
        hold_reason = "趋势偏多，关注MA20支撑是否有效"
    elif composite >= 45:
        hold_suggest = "5-10天"
        hold_reason = "横盘整理中，等待方向选择"
    elif composite >= 30:
        hold_suggest = "3-5天"
        hold_reason = "趋势偏弱，密切关注止损位"
    else:
        hold_suggest = "⚠️1-3天"
        hold_reason = "空头趋势，建议尽快减仓或止损"

    return {
        "valid": True,
        "close": close,
        "ma5": round(ma5, 3) if not pd.isna(ma5) else None,
        "ma10": round(ma10, 3) if not pd.isna(ma10) else None,
        "ma20": round(ma20, 3) if not pd.isna(ma20) else None,
        "ma60": round(ma60, 3) if not pd.isna(ma60) else None,
        "rsi": round(rsi, 1) if not pd.isna(rsi) else None,
        "macd_dif": round(macd_dif, 4) if not pd.isna(macd_dif) else None,
        "macd_dea": round(macd_dea, 4) if not pd.isna(macd_dea) else None,
        "macd_hist": round(macd_hist, 4) if not pd.isna(macd_hist) else None,
        "atr": round(atr, 3) if not pd.isna(atr) else None,
        "boll_upper": round(boll_upper, 3) if not pd.isna(boll_upper) else None,
        "boll_lower": round(boll_lower, 3) if not pd.isna(boll_lower) else None,
        "vol_ratio": round(vol_ratio, 2),
        "vol_signal": vol_signal,
        "momentum_5d": round(momentum_5d, 2),
        "trend_score": trend_score,
        "trend_dir": trend_dir,
        "trend_signals": trend_signals,
        "momentum_signals": momentum_signals,
        "composite": composite,
        "supports": [(n, round(v, 3)) for n, v in supports[:3]],
        "resistances": [(n, round(v, 3)) for n, v in resistances[:3]],
        "first_support": round(first_support, 3),
        "first_resistance": round(first_resistance, 3),
        "hold_suggest": hold_suggest,
        "hold_reason": hold_reason,
    }


# ============================================================
# 三、获取实时行情
# ============================================================
print("=" * 60)
print("  持仓综合分析报告 V4（技术分析+条件单）")
print("=" * 60)

codes = [h["code"] for h in holdings_list]
print(f"\n[行情] 正在获取 {len(codes)} 只标的实时行情...")
quotes = fetch_realtime_batch(codes)
print(f"[行情] 成功获取 {len(quotes)} 只")

# ============================================================
# 四、获取历史K线 + 技术分析
# ============================================================
print(f"\n[技术] 正在获取历史K线并计算技术指标...")
tech_analysis = {}

for item in holdings_list:
    code = item["code"]
    try:
        start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m-%d")
        df = fetch_stock_daily_baostock(code, start_date=start)
        if not df.empty and len(df) >= 30:
            df = compute_indicators(df)
            rt_price = quotes.get(code, {}).get("price", 0)
            tech_analysis[code] = analyze_technical(df, rt_price)
            comp = tech_analysis[code].get("composite", 0)
            trend = tech_analysis[code].get("trend_dir", "")
            print(f"  {code} {item['名称']}: {len(df)}根K线 | 评分{comp} | {trend}")
        else:
            tech_analysis[code] = {"valid": False, "error": "数据不足"}
            print(f"  {code} {item['名称']}: ⚠️数据不足")
    except Exception as e:
        tech_analysis[code] = {"valid": False, "error": str(e)}
        print(f"  {code} {item['名称']}: ❌获取失败({e})")

try:
    _bs_logout()
except:
    pass

# ============================================================
# 四B、五层选股引擎扫描候选池
# ============================================================
print(f"\n[选股] 五层引擎扫描SECTOR_CANDIDATES候选池...")
held_codes = set(codes)

# 收集所有非持仓候选股
candidate_stocks = []
for sector_name, sector_info in config.SECTOR_CANDIDATES.items():
    for code_c, info_c in sector_info.get("stocks", {}).items():
        if code_c not in held_codes and not code_c.startswith("300") and not code_c.startswith("688"):
            candidate_stocks.append({
                "code": code_c,
                "name": info_c.get("名称", code_c),
                "sector": info_c.get("细分", sector_name),
                "type": info_c.get("类型", "龙头"),
            })

print(f"[选股] 共{len(candidate_stocks)}只非持仓候选股")

# 批量获取候选股实时价
cand_codes = [c["code"] for c in candidate_stocks]
cand_quotes = fetch_realtime_batch(cand_codes)
print(f"[选股] 实时价获取: {len(cand_quotes)}/{len(cand_codes)}只")

# 获取历史K线 + 运行五层引擎
candidate_data = []
try:
    for item in candidate_stocks:
        code_c = item["code"]
        try:
            start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m-%d")
            df = fetch_stock_daily_baostock(code_c, start_date=start)
            if not df.empty and len(df) >= 30:
                df = compute_indicators(df)
                rt_price = cand_quotes.get(code_c, {}).get("price", 0)
                rt_change = cand_quotes.get(code_c, {}).get("change_pct", 0)
                candidate_data.append({
                    "code": code_c,
                    "name": item["name"],
                    "sector": item["sector"],
                    "type": item["type"],
                    "df": df,
                    "realtime_price": rt_price,
                    "realtime_change": rt_change,
                })
        except Exception:
            pass
    _bs_logout()
except:
    pass

print(f"[选股] 数据就绪{len(candidate_data)}只，运行五层筛选...")
rec_result = run_recommendation(candidate_data, top_n=3)
recommendations = rec_result["recommended"]
watchlist = rec_result["watchlist"]

print(f"[选股] 结果: 推荐{len(recommendations)}只 | 观察{len(watchlist)}只 | 淘汰{rec_result['rejected_count']}只")
for r in recommendations:
    plan = r["plan"]
    print(f"  ✅ {r['code']} {r['name']} [{r['sector']}] 评分{r['total_score']} | "
          f"买入{plan['buy_low']}-{plan['buy_high']} | 止损{plan['stop_loss']} | "
          f"盈亏比{plan['risk_reward']}:1 | 仓位{plan['position_pct']}%")
for w in watchlist[:3]:
    print(f"  👀 {w['code']} {w['name']} [{w['sector']}] 评分{w['total_score']} | {w['trend_dir']}")

# ============================================================
# 五、构建完整数据
# ============================================================
holdings = {}
for item in holdings_list:
    code = item["code"]
    quote = quotes.get(code, {})
    latest = quote.get("price", 0)
    change_pct = quote.get("change_pct", 0)
    prev_close = quote.get("prev_close", 0)
    high = quote.get("high", 0)
    low = quote.get("low", 0)
    amplitude = quote.get("amplitude", 0)
    turnover = quote.get("turnover", 0)
    quote_time = quote.get("time", "")
    source = quote.get("source", "N/A")

    if latest <= 0:
        latest = prev_close if prev_close > 0 else 0
        price_status = "⚠️昨收"
    else:
        price_status = f"✅实时"

    # 主模式: 止损 = 最新价 × 92%
    stop_loss = round(latest * (1 - STOP_LOSS_PCT), 2) if latest > 0 else 0
    drawdown_trigger = round(high * (1 - DRAWDOWN_FROM_HIGH), 2) if high > 0 else 0
    rebound_trigger = round(low * (1 + REBOUND_FROM_LOW), 2) if low > 0 else 0

    holdings[code] = {
        "名称": item["名称"],
        "赛道": item["赛道"],
        "最新": latest,
        "涨跌幅": change_pct,
        "最高": high,
        "最低": low,
        "振幅": amplitude,
        "换手率": turnover,
        "止损价": stop_loss,
        "回落触发": drawdown_trigger,
        "反弹触发": rebound_trigger,
        "价格状态": price_status,
        "行情时间": quote_time,
        "数据源": source,
        "技术": tech_analysis.get(code, {"valid": False}),
    }

# ============================================================
# 六、生成HTML报告
# ============================================================
print(f"\n[生成] 构建综合分析报告...")

sample_quote = next(iter(quotes.values()), {})
quote_time_str = sample_quote.get("time", now)
data_source = sample_quote.get("source", "tencent")

html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:'Microsoft YaHei',sans-serif;padding:15px;background:#f0f2f5;font-size:13px}}
.container{{max-width:1000px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:20px 25px;border-radius:10px 10px 0 0}}
.header h1{{margin:0;font-size:20px}}
.header .sub{{font-size:12px;opacity:.8;margin-top:5px}}
.content{{background:#fff;padding:20px 25px;border-radius:0 0 10px 10px;box-shadow:0 2px 10px rgba(0,0,0,.1)}}
h2{{color:#2c3e50;font-size:16px;margin-top:25px;border-left:4px solid #3498db;padding-left:10px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0}}
.card{{background:#f8f9fa;border-radius:8px;padding:12px;text-align:center}}
.card .v{{font-size:18px;font-weight:bold}}
.card .l{{font-size:11px;color:#7f8c8d;margin-top:4px}}
.up{{color:#e74c3c}} .down{{color:#27ae60}}
table{{width:100%;border-collapse:collapse;margin:10px 0;font-size:12px}}
th{{background:#34495e;color:#fff;padding:8px 6px;text-align:center}}
td{{padding:7px 6px;border-bottom:1px solid #eee;text-align:center}}
.alert{{padding:12px;border-radius:6px;margin:10px 0;font-size:12px}}
.alert-danger{{background:#ffebee;border-left:4px solid #e74c3c}}
.alert-success{{background:#e8f5e9;border-left:4px solid #4caf50}}
.alert-warning{{background:#fff3cd;border-left:4px solid #ffc107}}
.alert-info{{background:#e3f2fd;border-left:4px solid #2196f3}}
.stock-card{{border:1px solid #e0e0e0;border-radius:8px;margin:15px 0;padding:15px;page-break-inside:avoid}}
.stock-card h3{{margin:0 0 10px;font-size:15px;border-bottom:2px solid #3498db;padding-bottom:6px}}
.meta{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:8px 0}}
.meta span{{font-size:12px;color:#555}}
.tag{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold;color:#fff;margin:2px}}
.tag-hold{{background:#2196f3}} .tag-reduce{{background:#ff9800}} .tag-stop{{background:#f44336}} .tag-add{{background:#4caf50}}
.score-bar{{height:8px;border-radius:4px;background:#eee;margin:4px 0;position:relative}}
.score-fill{{height:100%;border-radius:4px;position:absolute;left:0;top:0}}
.badge{{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;color:#fff;margin-left:5px}}
.badge-rt{{background:#4caf50}} .badge-stale{{background:#ff9800}}
.footer{{text-align:center;color:#999;font-size:11px;margin-top:15px;padding-top:10px;border-top:1px solid #eee}}
.mode-badge{{display:inline-block;background:#9c27b0;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;margin-left:8px}}
.signal-list{{font-size:11px;color:#555;margin:5px 0;padding-left:15px}}
.signal-list li{{margin:2px 0}}
</style></head><body><div class="container">
<div class="header">
<h1>📊 持仓综合分析报告 <span class="mode-badge">技术面+条件单</span></h1>
<div class="sub">日期: {today} | 行情: {quote_time_str} ({data_source}) | 止损规则: 最新价×{1-STOP_LOSS_PCT:.0%} | 持仓周期: 3天-4周波段</div>
</div>
<div class="content">
"""

# ---- 风险预警 ----
html += '<h2>⚠️ 仓位风险预警</h2>'
# 科创50仓位69.61%严重超限
html += '<div class="alert alert-danger">🚨 <b>科创50仓位69.61%</b>，严重超出ETF单只上限20%！建议分批减仓至20%以内，释放资金分散配置。</div>'
html += '<div class="alert alert-warning">⚡ 持仓集中度: 科创50(69.6%) + 德明利(19.1%) = <b>88.7%</b>集中在2只标的，风险极高。建议单只不超30%。</div>'

# ---- 总览表 ----
html += '<h2>一、实时行情 + 技术评分总览</h2>'
html += f'<div class="alert alert-info">📡 行情源: {data_source} | 时间: {quote_time_str or now} | 主模式: 止损=最新价×{1-STOP_LOSS_PCT:.0%}（不依赖成本价）</div>'

html += '<table><tr><th>代码</th><th>名称</th><th>最新价</th><th>今日涨跌</th><th>技术评分</th><th>趋势方向</th><th>RSI</th><th>MACD</th><th>止损价</th><th>建议持有</th></tr>'
for code, h in holdings.items():
    tech = h["技术"]
    chg_class = "up" if h['涨跌幅'] >= 0 else "down"
    comp = tech.get("composite", 0) if tech.get("valid") else 0
    trend_dir = tech.get("trend_dir", "N/A") if tech.get("valid") else "数据不足"
    rsi_val = tech.get("rsi", "-") if tech.get("valid") else "-"
    macd_state = ""
    if tech.get("valid"):
        macd_state = "金叉" if (tech.get("macd_dif", 0) or 0) > (tech.get("macd_dea", 0) or 0) else "死叉"
    hold_s = tech.get("hold_suggest", "-") if tech.get("valid") else "-"

    # 评分颜色
    if comp >= 60:
        score_color = "#4caf50"
    elif comp >= 40:
        score_color = "#ff9800"
    else:
        score_color = "#f44336"

    html += f'<tr><td>{code}</td><td><b>{h["名称"]}</b></td>'
    html += f'<td><b>{h["最新"]:.3f}</b></td>'
    html += f'<td class="{chg_class}">{h["涨跌幅"]:+.2f}%</td>'
    html += f'<td style="color:{score_color};font-weight:bold">{comp}</td>'
    html += f'<td>{trend_dir}</td>'
    html += f'<td>{rsi_val}</td>'
    html += f'<td style="color:{"#e74c3c" if macd_state=="金叉" else "#27ae60"}">{macd_state}</td>'
    html += f'<td style="color:#e74c3c;font-weight:bold">{h["止损价"]:.2f}</td>'
    html += f'<td style="color:#1976d2">{hold_s}</td></tr>'
html += '</table>'

# ---- 逐只详细分析 ----
html += '<h2>二、逐只技术分析 + 条件单</h2>'

for code, h in holdings.items():
    tech = h["技术"]
    latest = h["最新"]
    chg_class = "up" if h['涨跌幅'] >= 0 else "down"

    if not tech.get("valid"):
        html += f'<div class="stock-card"><h3>{code} {h["名称"]} <span style="color:#999">({h["赛道"]})</span></h3>'
        html += f'<p style="color:#999">技术分析数据不足: {tech.get("error", "未知")}</p>'
        html += f'<p>止损价: <b style="color:#e74c3c">{h["止损价"]:.2f}</b> (最新价{latest:.3f}×92%)</p></div>'
        continue

    comp = tech["composite"]
    trend_dir = tech["trend_dir"]
    hold_suggest = tech["hold_suggest"]
    hold_reason = tech["hold_reason"]

    # 评分条颜色
    if comp >= 60:
        bar_color = "#4caf50"
        action_tag = '<span class="tag tag-hold">持有</span>'
    elif comp >= 40:
        bar_color = "#ff9800"
        action_tag = '<span class="tag tag-reduce">关注</span>'
    else:
        bar_color = "#f44336"
        action_tag = '<span class="tag tag-stop">警惕</span>'

    # 支撑/压力位文字
    support_str = " | ".join([f"{n}:{v:.2f}" for n, v in tech["supports"][:3]]) if tech["supports"] else "无明显支撑"
    resist_str = " | ".join([f"{n}:{v:.2f}" for n, v in tech["resistances"][:3]]) if tech["resistances"] else "无明显压力"

    # 信号列表
    all_signals = tech["trend_signals"] + tech["momentum_signals"] + [tech["vol_signal"]]

    html += f"""
<div class="stock-card">
<h3>{code} {h['名称']} {action_tag} <span style="font-size:12px;color:#888">({h['赛道']})</span>
<span style="float:right;font-size:13px">{trend_dir}</span></h3>

<div style="margin:8px 0">
<span style="font-size:12px;color:#666">综合评分: <b style="color:{bar_color}">{comp}/100</b></span>
<div class="score-bar"><div class="score-fill" style="width:{comp}%;background:{bar_color}"></div></div>
</div>

<div class="meta">
<span>最新价: <b>{latest:.3f}</b> <span class="{chg_class}">({h['涨跌幅']:+.2f}%)</span></span>
<span>MA5: <b>{tech['ma5']}</b> | MA10: <b>{tech['ma10']}</b></span>
<span>MA20: <b>{tech['ma20']}</b> | MA60: <b>{tech['ma60']}</b></span>
<span>RSI(14): <b>{tech['rsi']}</b></span>
<span>MACD DIF: <b>{tech['macd_dif']}</b> | DEA: <b>{tech['macd_dea']}</b></span>
<span>ATR(14): <b>{tech['atr']}</b> | 5日动量: <b>{tech['momentum_5d']:+.2f}%</b></span>
<span>布林上轨: <b>{tech['boll_upper']}</b> | 下轨: <b>{tech['boll_lower']}</b></span>
<span>量能: <b>{tech['vol_signal']}</b></span>
<span>建议持有: <b style="color:#1976d2">{hold_suggest}</b></span>
</div>

<div style="margin:8px 0;font-size:12px">
<b>支撑位:</b> <span style="color:#4caf50">{support_str}</span><br>
<b>压力位:</b> <span style="color:#e74c3c">{resist_str}</span>
</div>

<ul class="signal-list">
{''.join(f'<li>{s}</li>' for s in all_signals)}
</ul>

<table>
<tr><th>条件单</th><th>设置（主模式·基于最新价{latest:.3f}）</th><th>有效期</th></tr>
<tr><td><b>① 定价止损</b></td><td style="color:#e74c3c;font-weight:bold">触发价 {h['止损价']:.2f}，委托价 {h['止损价']*0.995:.2f}</td><td>20天</td></tr>
<tr><td><b>② 14:50时间单</b></td><td style="color:#e74c3c">最新价≤{h['止损价']:.2f} 则卖出</td><td>10天</td></tr>
<tr><td><b>③ 回落卖出</b></td><td style="color:#ff9800">日高{h['最高']:.3f}回落至 {h['回落触发']:.2f} 卖出</td><td>10天</td></tr>
<tr><td><b>④ 反弹买入</b></td><td style="color:#4caf50">日低{h['最低']:.3f}反弹至 {h['反弹触发']:.2f} 买入</td><td>5天</td></tr>
</table>
<div style="font-size:11px;color:#666;margin-top:5px">💡 {hold_reason}</div>
</div>"""

# ---- 系统胜率验证摘要 ----
verify_path = os.path.join(config.PROJECT_ROOT, 'output', 'win_rate_verification.json')
if os.path.exists(verify_path):
    with open(verify_path, 'r', encoding='utf-8') as f:
        vr = json.load(f)
    html += f"""
<h2>四、系统胜率验证摘要</h2>
<div class="cards">
<div class="card"><div class="v">{vr.get('win_rate', 0)}%</div><div class="l">真实胜率(1322笔)</div></div>
<div class="card"><div class="v">{vr.get('profit_factor', 0)}</div><div class="l">盈亏比</div></div>
<div class="card"><div class="v down">{vr.get('total_pnl', 0):+,.0f}</div><div class="l">总盈亏(元)</div></div>
<div class="card"><div class="v">{vr.get('avg_hold_days', 0)}天</div><div class="l">平均持仓</div></div>
</div>
<div class="alert alert-warning">📋 历史验证结论: T+0胜率64.7%(唯一正收益) | 持仓越长胜率越低 | 日均18笔过度交易 → 当前已限制每日≤3笔</div>"""

# ---- 推荐股票（五层引擎完整交易计划） ----
html += '<h2>三、今日推荐标的（五层筛选·完整交易计划）</h2>'
html += f'<div class="alert alert-info">🔍 扫描{rec_result["total_scanned"]}只候选股（8大赛道）| 五层筛选: 排雷→赛道→基本面→趋势→买点 | 通过{len(recommendations)}只</div>'

if recommendations:
    for idx, rec in enumerate(recommendations, 1):
        plan = rec["plan"]
        layers = rec["layers"]
        l2 = layers["L2_赛道"]
        l4 = layers["L4_趋势"]

        # 推荐逻辑
        reasons_html = "".join([f"<li>{r}</li>" for r in rec["reasons"]])
        # 风险提示
        risks_html = " | ".join(plan["risk_notes"])
        # 信号列表
        signals_html = ", ".join(l4.get("signals", [])[:5])

        html += f"""
<div class="stock-card" style="border-left:5px solid #4caf50">
<h3>🌟 推荐{idx}: {rec['code']} {rec['name']} <span style="font-size:12px;color:#888">({rec['sector']}/{rec['type']})</span>
<span style="float:right;font-size:14px;color:#4caf50;font-weight:bold">综合评分 {rec['total_score']}</span></h3>

<div style="background:#f0f7ff;padding:10px;border-radius:6px;margin:8px 0">
<b>📌 推荐逻辑:</b>
<ul style="margin:5px 0;padding-left:20px;font-size:12px">{reasons_html}</ul>
</div>

<table>
<tr><th style="width:120px">项目</th><th>具体设置（可直接挂条件单）</th></tr>
<tr><td><b>最新价</b></td><td><b>{rec['price']:.2f}元</b> ({rec['change_pct']:+.2f}%) | {rec['trend_dir']}</td></tr>
<tr><td><b>买入区间</b></td><td style="color:#1976d2;font-weight:bold;font-size:14px">{plan['buy_low']:.2f} ~ {plan['buy_high']:.2f} 元</td></tr>
<tr><td><b>买点类型</b></td><td style="color:#722ed1">{plan['entry_type']}</td></tr>
<tr><td><b>止损价格</b></td><td style="color:#e74c3c;font-weight:bold;font-size:14px">{plan['stop_loss']:.2f} 元（收盘价触发，跌幅{(1-plan['stop_loss']/rec['price'])*100:.1f}%）</td></tr>
<tr><td><b>目标价位</b></td><td>第一目标: <b style="color:#e74c3c">{plan['target_1']:.2f}元</b>(减仓1/2) | 第二目标: <b style="color:#e74c3c">{plan['target_2']:.2f}元</b>(清仓)</td></tr>
<tr><td><b>盈亏比</b></td><td style="font-weight:bold;color:{'#4caf50' if plan['risk_reward']>=2.5 else '#ff9800'}">{plan['risk_reward']}:1 {'✅达标' if plan['risk_reward']>=2.5 else '⚠️偏低'}</td></tr>
<tr><td><b>建议仓位</b></td><td><b>{plan['position_pct']:.1f}%</b>（{plan['buy_shares']}股 / {plan['buy_amount']:,.0f}元）| 单笔风险≤总资金2%</td></tr>
<tr><td><b>支撑位</b></td><td style="color:#4caf50">{plan['first_support_name']}: {plan['first_support']:.2f}元 | 距支撑{plan['dist_to_support_pct']:.1f}%</td></tr>
<tr><td><b>压力位</b></td><td style="color:#e74c3c">{plan['first_resistance_name']}: {plan['target_1']:.2f}元</td></tr>
<tr><td><b>风险提示</b></td><td style="color:#ff9800;font-size:11px">⚠️ {risks_html}</td></tr>
</table>

<div style="font-size:11px;color:#666;margin-top:8px;padding-top:5px;border-top:1px dashed #eee">
<b>技术信号:</b> {signals_html}<br>
<b>赛道评分:</b> {l2['score']}/100 | <b>趋势评分:</b> {l4['score']}/100 | <b>买点评分:</b> {plan['score']}/100 | ATR: {plan['atr']}
</div>
</div>"""

    # 观察池
    if watchlist:
        html += '<h3 style="font-size:14px;color:#ff9800;margin-top:15px">👀 观察池（未达买入标准，等待更好价格）</h3>'
        html += '<table><tr><th>代码</th><th>名称</th><th>赛道</th><th>评分</th><th>趋势</th><th>未通过原因</th></tr>'
        for w in watchlist[:5]:
            reject = ""
            if w["total_score"] < 55:
                reject = f"综合分{w['total_score']}不足55"
            elif w.get("plan", {}) and w["plan"].get("risk_reward", 0) < 2.0:
                reject = f"盈亏比{w['plan']['risk_reward']}不足2.0"
            else:
                reject = f"趋势分{w['layers']['L4_趋势']['score']}不足40"
            html += f'<tr><td>{w["code"]}</td><td>{w["name"]}</td><td>{w["sector"]}</td>'
            html += f'<td>{w["total_score"]}</td><td>{w["trend_dir"]}</td><td style="font-size:11px">{reject}</td></tr>'
        html += '</table>'
else:
    html += '<div class="alert alert-warning">⛔ 当前无符合五层筛选标准的推荐标的。候选池均处于弱势或盈亏比不达标，建议空仓等待。</div>'

# ---- footer ----
html += f"""
<div class="footer">
本报告由交易系统自动生成 | 技术面分析(baostock前复权) + 实时行情({data_source}) + 条件单(主模式)<br>
止损规则: 最新价×{1-STOP_LOSS_PCT:.0%} | 不依赖成本价 | 行情时间: {quote_time_str or now} | {today}<br>
⚠️ 仅供参考，非投资建议 | 股市有风险，投资需谨慎
</div>
</div></div></body></html>"""

# ============================================================
# 七、保存 + 发送
# ============================================================
report_path = os.path.join(config.PROJECT_ROOT, 'output', f'holdings_analysis_{today.replace("-", "")}.html')
os.makedirs(os.path.dirname(report_path), exist_ok=True)
with open(report_path, 'w', encoding='utf-8') as f:
    f.write(html)
print(f"[保存] {report_path}")

subject = f"[综合分析报告] 6只标的 技术面+条件单 | {today} {now[:5]}"
print(f"[发送] {subject}")
result = send_email(subject, html)
print(f"[结果] {'✅ 发送成功' if result else '❌ 发送失败'}")
