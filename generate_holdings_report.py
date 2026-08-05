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
from strategy.caopan_signal import CaopanEngine
from output.caopan_chart import generate_caopan_chart

try:
    from strategy.fundamental import FundamentalAnalyzer
    HAS_FUNDAMENTAL = True
except Exception:
    HAS_FUNDAMENTAL = False

# 新增面板模块导入（失败时降级）
try:
    from strategy.lhb_analyzer import LHBAnalyzer
    HAS_LHB = True
except Exception:
    HAS_LHB = False

try:
    from strategy.margin_monitor import MarginMonitor
    HAS_MARGIN = True
except Exception:
    HAS_MARGIN = False

try:
    from strategy.event_calendar import EventCalendar
    HAS_CALENDAR = True
except Exception:
    HAS_CALENDAR = False

try:
    from strategy.capital_flow import CapitalFlowAnalyzer
    HAS_CAPITAL_FLOW = True
except Exception:
    HAS_CAPITAL_FLOW = False

# 任务#1新增: 回本计划/板块背离/候选三源合并（失败时降级，不影响报告主流程）
try:
    from strategy.recovery_planner import build_recovery_plan, track_progress
    from strategy.sector_divergence import (
        load_sector_cache, detect_divergence, get_coarse_sector_map, merge_report_candidates
    )
    HAS_REPORT_EXT = True
except Exception:
    HAS_REPORT_EXT = False

today = datetime.date.today().strftime("%Y-%m-%d")
now = datetime.datetime.now().strftime("%H:%M:%S")

# ============================================================
# 一、持仓列表（统一从 holdings.json 读取，消除硬编码不同步）
# ============================================================
# 硬编码降级数据（当 holdings.json 不存在时使用）
_FALLBACK_HOLDINGS = [
    {"code": "002409", "名称": "雅克科技", "赛道": "半导体材料"},
    {"code": "002415", "名称": "海康威视", "赛道": "AI视觉"},
    {"code": "159205", "名称": "创业东方财富", "赛道": "指数ETF"},
    {"code": "588000", "名称": "科创50", "赛道": "指数ETF"},
    {"code": "600036", "名称": "招商银行", "赛道": "银行"},
    {"code": "600276", "名称": "恒瑞医药", "赛道": "创新药"},
    {"code": "601688", "名称": "华泰证券", "赛道": "券商"},
    {"code": "603501", "名称": "豪威集团", "赛道": "CIS芯片"},
    {"code": "603993", "名称": "洛阳钼业", "赛道": "有色资源"},
]


def _load_holdings_from_json():
    """从 holdings.json 读取持仓列表，统一数据源消除多文件硬编码不同步"""
    # FIX: 统一使用config.get_holdings_file()路径解析，与主调度器保持一致
    holdings_file = config.get_holdings_file()
    try:
        with open(holdings_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return [
            {
                "code": code,
                "名称": v.get("name", code),
                "赛道": v.get("sector", "其他"),
                "shares": v.get("shares", 0),
                "buy_price": v.get("buy_price", 0),
                "highest": v.get("highest", 0),
                "stop_loss_cfg": v.get("stop_loss", 0),
                "stock_type": v.get("stock_type", "龙头"),
            }
            for code, v in data.items()
        ]
    except Exception:
        return _FALLBACK_HOLDINGS


holdings_list = _load_holdings_from_json()

# 主模式参数
# FIX: 修复止损比例与config不一致，统一使用config.INITIAL_STOP_LOSS_PCT(0.10)
STOP_LOSS_PCT = config.INITIAL_STOP_LOSS_PCT  # 固定止损: 统一使用config配置的止损比例
# FIX: 回落止盈从硬编码5%改为按stock_type从config.DRAWDOWN_STOP读取（龙头7%/成长6%/弹性5%）
_TYPE_TO_DRAWDOWN_KEY = {"龙头": "龙头稳健", "弹性": "高弹性", "成长": "成长赛道"}
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

    # 处理NaN：FIX: 原循环`for v in list: v=close`为死代码（Python变量重绑定不修改原值），改为逐个赋值
    if pd.isna(ma5): ma5 = close
    if pd.isna(ma10): ma10 = close
    if pd.isna(ma20): ma20 = close
    if pd.isna(ma60): ma60 = close
    if pd.isna(rsi): rsi = 50
    if pd.isna(macd_dif): macd_dif = 0
    if pd.isna(macd_dea): macd_dea = 0
    if pd.isna(atr): atr = close * 0.02
    if pd.isna(boll_upper): boll_upper = close * 1.1
    if pd.isna(boll_lower): boll_lower = close * 0.9

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
    # V3.2回测诊断: Composite在熊市完全反向(高分+0.85% < 低分+2.70%)
    # 修复: 熊市中反转信号(低分=超卖反弹机会)，仅影响文字提示不影响分数
    _composite_regime_note = ""
    try:
        from trading_system.strategy.market_regime import detect_market_regime
        _regime_info = detect_market_regime()
        _cur_regime = _regime_info.get("regime", "range") if isinstance(_regime_info, dict) else "range"
    except Exception:
        _cur_regime = "range"
    if _cur_regime == "bear" and composite >= 65:
        _composite_regime_note = "⚠️熊市高分警告: 回测显示熊市中强势股常为补跌对象，注意回调风险"
    elif _cur_regime == "bear" and composite <= 35:
        _composite_regime_note = "💡熊市低分提示: 超卖反弹机会，但仅限轻仓短线，严格止损"

    # ---- 趋势方向文字 ----
    # V2.8回测优化: 增加超买/超卖提示（回测显示composite>=70的标的20日胜率47% < composite<30的50%，高分别代表超买）
    if composite >= 70:
        trend_dir = "📈 强势上涨(短期超买注意回调)"
    elif composite >= 55:
        trend_dir = "↗️ 偏多震荡"
    elif composite >= 45:
        trend_dir = "➡️ 横盘整理"
    elif composite >= 30:
        trend_dir = "↘️ 偏空震荡"
    else:
        trend_dir = "📉 弱势下跌(超卖可能反弹)"

    # ---- 建议持有时间（基于技术面）----
    # V2.8回测优化: composite>=70超买警告，建议缩短持有时间/设置止盈
    if composite >= 70 and not pd.isna(ma20_slope) and ma20_slope > 0:
        hold_suggest = "10-15天(超买注意止盈)"
        hold_reason = "多头趋势明确但短期超买，回测显示高胜率仅47%，建议设置回落止盈"
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
        "regime_note": _composite_regime_note,  # V3.2: 熊市反转提示
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
hist_dataframes = {}  # 保存历史K线数据（供操盘密码分析用）

for item in holdings_list:
    code = item["code"]
    try:
        start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m-%d")
        df = fetch_stock_daily_baostock(code, start_date=start)
        if not df.empty and len(df) >= 30:
            df = compute_indicators(df)
            hist_dataframes[code] = df  # 保存原始数据
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

# ---- 大盘基准数据获取（供五C段大盘状态检测用，复用baostock会话）----
benchmark_df = None
try:
    benchmark_start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m-%d")
    benchmark_df = fetch_stock_daily_baostock(config.BENCHMARK_INDEX, start_date=benchmark_start)
    if not benchmark_df.empty:
        benchmark_df = compute_indicators(benchmark_df)
        print(f"[大盘] 沪深300: {len(benchmark_df)}根K线获取成功")
    else:
        print(f"[大盘] 沪深300: 数据为空")
except Exception as e:
    print(f"[大盘] 沪深300获取失败: {e}")

try:
    _bs_logout()
except:
    pass

# ============================================================
# 四B、五层选股引擎扫描候选池
# ============================================================
print(f"\n[选股] 三源候选构建: 静态池 + stock_pool观察池 + scan_cache动态扫描...")
held_codes = set(codes)

def _is_tradable_code(_c):
    """排除创业板300/科创板688（与现状逻辑一致）"""
    return not _c.startswith("300") and not _c.startswith("688")

# 源①: config.SECTOR_CANDIDATES 静态候选池（现状逻辑保留）
_static_candidates = []
for sector_name, sector_info in config.SECTOR_CANDIDATES.items():
    for code_c, info_c in sector_info.get("stocks", {}).items():
        if code_c not in held_codes and _is_tradable_code(code_c):
            _static_candidates.append({
                "code": code_c,
                "name": info_c.get("名称", code_c),
                "sector": info_c.get("细分", sector_name),
                "type": info_c.get("类型", "龙头"),
            })

# 源②: stock_pool.json core_pool+watch_pool（每项type标"观察池"，失败降级跳过）
_pool_candidates = []
try:
    _pool_file = os.path.join(config.PROJECT_ROOT, "stock_pool.json")
    with open(_pool_file, "r", encoding="utf-8") as _pf:
        _pool_data = json.load(_pf)
    for _src_key in ("core_pool", "watch_pool"):
        for code_c, info_c in (_pool_data.get(_src_key, {}) or {}).items():
            if code_c not in held_codes and _is_tradable_code(code_c):
                _pool_candidates.append({
                    "code": code_c,
                    "name": info_c.get("名称", code_c),
                    "sector": info_c.get("赛道", ""),
                    "type": "观察池",
                })
except Exception as _pe:
    print(f"[选股] stock_pool.json读取失败(降级跳过): {_pe}")

# 源③: data/scan_cache.json 动态扫描（校验时间戳≤24h，按当日涨幅取前10，失败降级跳过）
_scan_candidates = []
try:
    _scan_file = os.path.join(config.PROJECT_ROOT, "data", "scan_cache.json")
    with open(_scan_file, "r", encoding="utf-8") as _sf:
        _scan_data = json.load(_sf)
    _scan_ts_str = _scan_data.get("saved_at", "") or (_scan_data.get("data", {}) or {}).get("scan_time", "")
    _scan_ok = False
    try:
        _scan_ts = datetime.datetime.strptime(_scan_ts_str[:19], "%Y-%m-%d %H:%M:%S")
        _scan_ok = (datetime.datetime.now() - _scan_ts).total_seconds() <= 24 * 3600
    except Exception:
        _scan_ok = False
    if _scan_ok:
        _scan_details = ((_scan_data.get("data", {}) or {}).get("details", []) or [])
        _scan_details = sorted(_scan_details, key=lambda x: x.get("change_pct", 0), reverse=True)
        for _sd in _scan_details[:10]:
            code_c = _sd.get("code", "")
            if code_c and code_c not in held_codes and _is_tradable_code(code_c):
                _scan_candidates.append({
                    "code": code_c,
                    "name": _sd.get("name", code_c),
                    "sector": _sd.get("sector", ""),
                    "type": "动态扫描",
                })
    else:
        print(f"[选股] scan_cache时间戳缺失或超24h，跳过动态扫描源")
except Exception as _se:
    print(f"[选股] scan_cache.json读取失败(降级跳过): {_se}")

# 三源合并去重（静态池优先→观察池→动态扫描），截断到 REPORT_CANDIDATE_MAX
candidate_stocks = merge_report_candidates(
    _static_candidates, _pool_candidates, _scan_candidates,
    held_codes=held_codes,
    max_total=getattr(config, "REPORT_CANDIDATE_MAX", 20),
) if HAS_REPORT_EXT else _static_candidates

print(f"[选股] 共{len(candidate_stocks)}只非持仓候选股 "
      f"(静态{len(_static_candidates)}/观察池{len(_pool_candidates)}/动态扫描{len(_scan_candidates)})")

# 批量获取候选股实时价
cand_codes = [c["code"] for c in candidate_stocks]
cand_quotes = fetch_realtime_batch(cand_codes)
print(f"[选股] 实时价获取: {len(cand_quotes)}/{len(cand_codes)}只")

# 获取历史K线 + 运行五层引擎
candidate_data = []
try:
    # P0优化: 获取基本面数据(ROE/增速)传入L3层
    _fa = FundamentalAnalyzer() if HAS_FUNDAMENTAL else None
    for item in candidate_stocks:
        code_c = item["code"]
        try:
            start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m-%d")
            df = fetch_stock_daily_baostock(code_c, start_date=start)
            if not df.empty and len(df) >= 30:
                df = compute_indicators(df)
                rt_price = cand_quotes.get(code_c, {}).get("price", 0)
                rt_change = cand_quotes.get(code_c, {}).get("change_pct", 0)
                # 基本面数据获取（有缓存，不会重复请求）
                fund_data = None
                if _fa:
                    try:
                        fin = _fa.get_financial_indicators(code_c)
                        fund_data = {
                            "roe": fin.get("roe"),
                            "profit_growth": fin.get("net_profit_growth"),
                            "revenue_growth": fin.get("revenue_growth"),
                            "pe_ttm": fin.get("pe_ttm"),
                            "pb": fin.get("pb"),
                        }
                    except Exception:
                        pass
                candidate_data.append({
                    "code": code_c,
                    "name": item["name"],
                    "sector": item["sector"],
                    "type": item["type"],
                    "df": df,
                    "realtime_price": rt_price,
                    "realtime_change": rt_change,
                    "fund_data": fund_data,
                })
        except Exception:
            pass
    _bs_logout()
except:
    pass

print(f"[选股] 数据就绪{len(candidate_data)}只，运行五层筛选...")
# P0优化: 传入当前持仓赛道列表，同赛道推荐扣分
_held_sectors = list(set(h.get("赛道", "") for h in holdings_list if h.get("赛道")))
rec_result = run_recommendation(candidate_data, top_n=5, held_sectors=_held_sectors)
recommendations = rec_result["recommended"]
watchlist = rec_result["watchlist"]

print(f"[选股] 结果: 推荐{len(recommendations)}只 | 观察{len(watchlist)}只 | 淘汰{rec_result['rejected_count']}只")
for r in recommendations:
    plan = r["plan"]
    print(f"  ✅ {r['code']} {r['name']} [{r['sector']}] 评分{r['total_score']} | "
          f"买入{plan['buy_low']}-{plan['buy_high']} | 止损{plan['stop_loss']} | "
          f"盈亏比{plan['risk_reward']}:1 | 仓位{plan['position_pct']}%")
for w in watchlist[:12]:
    print(f"  👀 {w['code']} {w['name']} [{w['sector']}] 评分{w['total_score']} | {w['trend_dir']}")

# ============================================================
# 五、构建完整数据（增强版：含成本/盈亏/止盈/操作建议）
# ============================================================

# [前瞻] 获取隔夜外盘联动数据
try:
    from trading_system.strategy.overnight_linkage import OvernightLinkage
    _overnight = OvernightLinkage()
    overnight_data = _overnight.fetch_global_indices()
    print(f"[外盘] 获取成功: {len(overnight_data.get('indices', {}))}个指数 | 偏向: {overnight_data.get('overall_bias', 'N/A')}")
except Exception as e:
    overnight_data = {"available": False}
    print(f"[外盘] 获取失败(不影响报告): {e}")

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

    # 主模式: 止损 = 最新价 × (1-STOP_LOSS_PCT)
    stop_loss = round(latest * (1 - STOP_LOSS_PCT), 2) if latest > 0 else 0

    # 成本/持仓数据
    shares = item.get("shares", 0)
    buy_price = item.get("buy_price", 0)
    highest_hist = item.get("highest", 0)

    # FIX: 回落触发改用持仓历史最高价（而非当日high），比例从config.DRAWDOWN_STOP按stock_type读取
    _stock_type = item.get("stock_type", "龙头")
    _dd_key = _TYPE_TO_DRAWDOWN_KEY.get(_stock_type, "龙头稳健")
    _dd_pct = config.DRAWDOWN_STOP.get(_dd_key, 0.07)
    _ref_high = max(highest_hist, high) if highest_hist > 0 else high  # 持仓期间最高 vs 当日最高取大
    drawdown_trigger = round(_ref_high * (1 - _dd_pct), 2) if _ref_high > 0 else 0
    rebound_trigger = round(low * (1 + REBOUND_FROM_LOW), 2) if low > 0 else 0

    pnl_pct = ((latest - buy_price) / buy_price * 100) if buy_price > 0 and latest > 0 else 0
    pnl_amount = (latest - buy_price) * shares if buy_price > 0 and latest > 0 else 0
    market_value = latest * shares if latest > 0 else 0

    # FIX P2: 止损线只升不降（Ratchet原则）—— 浮亏时止损价不得低于原始硬止损(成本×90%)
    if buy_price > 0 and pnl_pct < 0 and stop_loss > 0:
        hard_floor = round(buy_price * (1 - STOP_LOSS_PCT), 2)
        if stop_loss < hard_floor:
            stop_loss = hard_floor
    # FIX P0: 止损价上限保护 —— 止损价永远不能高于现价（深度浮亏/除权后Ratchet会反转）
    if stop_loss > 0 and latest > 0 and stop_loss >= latest:
        stop_loss = round(latest * (1 - STOP_LOSS_PCT), 2)

    # 技术分析数据
    tech = tech_analysis.get(code, {"valid": False})

    # ---- 止盈目标计算 ----
    take_profit_1 = 0  # 第一止盈目标（减仓1/2）
    take_profit_2 = 0  # 第二止盈目标（清仓）
    tp_basis = ""  # 止盈依据
    if tech.get("valid") and latest > 0:
        resistances = tech.get("resistances", [])
        atr_val = tech.get("atr", 0) or 0
        boll_upper = tech.get("boll_upper", 0) or 0
        first_resist = resistances[0][1] if resistances else 0

        # 策略: 第一目标=最近压力位, 第二目标=次压力位或ATR推算
        if first_resist > latest:
            take_profit_1 = round(first_resist, 3)
            tp_basis = f"压力位({resistances[0][0]})"
        elif boll_upper > latest:
            take_profit_1 = round(boll_upper, 3)
            tp_basis = "布林上轨"
        else:
            # 用ATR推算: 最新价 + 2*ATR
            take_profit_1 = round(latest + 2 * atr_val, 3) if atr_val > 0 else round(latest * 1.08, 3)
            tp_basis = "ATR推算(+2ATR)" if atr_val > 0 else "固定+8%"

        # 第二目标
        if len(resistances) >= 2 and resistances[1][1] > take_profit_1:
            take_profit_2 = round(resistances[1][1], 3)
        elif atr_val > 0:
            take_profit_2 = round(latest + 3.5 * atr_val, 3)
        else:
            take_profit_2 = round(latest * 1.15, 3)
    elif latest > 0:
        # 无技术数据时用固定比例
        take_profit_1 = round(latest * 1.08, 3)
        take_profit_2 = round(latest * 1.15, 3)
        tp_basis = "固定比例(+8%/+15%)"

    # ---- 操作建议生成 ----
    comp_score = tech.get("composite", 0) if tech.get("valid") else 0
    action = ""  # 操作建议
    action_reason = ""  # 建议理由
    action_color = ""  # 显示颜色

    if shares == 0:
        action = "已清仓"
        action_reason = "今日已清仓，无持仓"
        action_color = "#999"
    elif comp_score >= 70 and pnl_pct >= 0:
        action = "继续持有"
        action_reason = f"技术面强势(评分{comp_score})，浮盈{pnl_pct:.1f}%，趋势向上可持有"
        action_color = "#4caf50"
    elif comp_score >= 70 and pnl_pct < 0:
        action = "持有待涨"
        action_reason = f"技术面强势(评分{comp_score})，浮亏{pnl_pct:.1f}%为暂时回调，可耐心持有"
        action_color = "#4caf50"
    elif comp_score >= 55 and pnl_pct >= 5:
        action = "持有+部分止盈"
        action_reason = f"趋势偏多(评分{comp_score})，浮盈{pnl_pct:.1f}%可观，可在目标价减仓1/3锁利"
        action_color = "#1976d2"
    elif comp_score >= 55 and pnl_pct < 0:
        action = "继续持有"
        action_reason = f"趋势偏多(评分{comp_score})，浮亏{pnl_pct:.1f}%可控，MA20支撑有效则持有"
        action_color = "#4caf50"
    elif comp_score >= 45 and pnl_pct >= 0:
        action = "持有观望"
        action_reason = f"横盘整理(评分{comp_score})，浮盈{pnl_pct:.1f}%，等待方向突破后再决策"
        action_color = "#ff9800"
    elif comp_score >= 45 and pnl_pct > -5:
        action = "谨慎持有"
        action_reason = f"横盘偏弱(评分{comp_score})，浮亏{pnl_pct:.1f}%较小，设好止损等待反弹"
        action_color = "#ff9800"
    elif comp_score >= 30 and pnl_pct > -10:
        action = "减仓观望"
        action_reason = f"趋势偏弱(评分{comp_score})，浮亏{pnl_pct:.1f}%，建议减仓1/2降低风险"
        action_color = "#ff9800"
    elif comp_score < 30 or pnl_pct <= -15:
        action = "建议止损"
        action_reason = f"空头趋势(评分{comp_score})，浮亏{pnl_pct:.1f}%较大，建议执行止损纪律"
        action_color = "#e74c3c"
    else:
        action = "关注止损"
        action_reason = f"趋势不明(评分{comp_score})，浮亏{pnl_pct:.1f}%，密切关注止损位"
        action_color = "#ff9800"

    # 特殊规则: 浮盈>15%强制提示锁利
    if pnl_pct >= 15 and shares > 0 and "止盈" not in action and "清仓" not in action:
        action = "止盈减仓"
        action_reason = f"浮盈{pnl_pct:.1f}%已超15%，建议至少减仓1/2锁住利润，剩余设移动止盈"
        action_color = "#e74c3c"

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
        "回落基准": _ref_high,
        "回落比例": _dd_pct,
        "反弹触发": rebound_trigger,
        "价格状态": price_status,
        "行情时间": quote_time,
        "数据源": source,
        "技术": tech,
        # 新增字段
        "数量": shares,
        "成本": buy_price,
        "历史最高": highest_hist,
        "盈亏比例": round(pnl_pct, 2),
        "盈亏金额": round(pnl_amount, 2),
        "市值": round(market_value, 2),
        "止盈目标1": take_profit_1,
        "止盈目标2": take_profit_2,
        "止盈依据": tp_basis,
        "操作建议": action,
        "建议理由": action_reason,
        "建议颜色": action_color,
    }

# ============================================================
# 五B、新增面板数据获取（龙虎榜/融资融券/解禁/四级资金流）
# ============================================================
print(f"\n[扩展] 获取龙虎榜/融资融券/解禁/四级资金流数据...")

# 龙虎榜数据
# P1优化: 仅对中小盘/高波动标的查询龙虎榜，蓝筹/ETF极少上榜，跳过减少无效API调用
_LHB_SKIP_PREFIXES = ("588", "159", "510", "511", "513")  # ETF/基金
_LHB_SKIP_CODES = {"600036", "600276", "002415"}  # 招商银行/恒瑞医药/海康威视(蓝筹极少上榜)
lhb_results = {}
if HAS_LHB:
    try:
        lhb = LHBAnalyzer()
        for code_h in holdings:
            if code_h.startswith(_LHB_SKIP_PREFIXES) or code_h in _LHB_SKIP_CODES:
                continue
            try:
                lhb_results[code_h] = lhb.analyze(code_h, days=10)
            except Exception:
                pass
        print(f"  龙虎榜: {len(lhb_results)}只获取成功(已跳过ETF+蓝筹)")
    except Exception as e:
        print(f"  龙虎榜: 获取失败({e})")

# 融资融券数据
margin_results = {}
if HAS_MARGIN:
    try:
        margin = MarginMonitor()
        for code_h in holdings:
            # P0优化: ETF/基金(588xxx/159xxx/510xxx)不是融资融券标的，跳过
            if code_h.startswith(("588", "159", "510", "511", "513")):
                continue
            try:
                margin_results[code_h] = margin.calc_margin_signal(code_h, days=10)
            except Exception:
                pass
        print(f"  融资融券: {len(margin_results)}只获取成功(已排除ETF)")
    except Exception as e:
        print(f"  融资融券: 获取失败({e})")

# 解禁风险数据
release_summary = {}
release_risks = {}
if HAS_CALENDAR:
    try:
        calendar = EventCalendar()
        release_summary = calendar.get_release_summary(days_ahead=45)
        for code_h in holdings:
            try:
                release_risks[code_h] = calendar.check_stock_release_risk(code_h)
            except Exception:
                pass
        print(f"  解禁风险: {len(release_risks)}只获取成功")
    except Exception as e:
        print(f"  解禁风险: 获取失败({e})")

# 四级资金流数据
flow_results = {}
pattern_results = {}
divergence_results = {}
if HAS_CAPITAL_FLOW:
    try:
        cf = CapitalFlowAnalyzer()
        for code_h in holdings:
            try:
                # P0优化: 合并调用，一次API请求同时完成四级分析+模式识别+价量背离
                _flow, _pattern, _div = cf.analyze_flow_combined(code_h, days=5)
                flow_results[code_h] = _flow
                pattern_results[code_h] = _pattern
                divergence_results[code_h] = _div
            except Exception:
                pass
        print(f"  四级资金流: {len(flow_results)}只获取成功")
    except Exception as e:
        print(f"  四级资金流: 获取失败({e})")

# ============================================================
# 五C、大盘状态 + 波动率环境 + 持仓趋势预测 + 反冲动锁
# ============================================================
print(f"\n[决策] 计算大盘状态/波动率/趋势预测...")

# --- 1. 大盘状态检测 ---
regime_result = None
try:
    from strategy.market_regime import MarketRegimeDetector
    if benchmark_df is not None and not benchmark_df.empty and len(benchmark_df) >= 60:
        detector = MarketRegimeDetector()
        regime_result = detector.detect(benchmark_df)
        print(f"  大盘状态: {regime_result['state_cn']} (置信度{regime_result['confidence']:.0%})")
    else:
        print(f"  大盘状态: 数据不足，跳过")
except Exception as e:
    print(f"  大盘状态: 检测失败({e})")

# --- 2. 波动率仓位缩放 ---
vol_result = None
try:
    from position.vol_target import VolTargetManager
    vtm = VolTargetManager()
    market_info = {}
    if regime_result:
        state_map = {"BEAR": "down", "RANGE": "neutral", "BULL": "up"}
        market_info["market_state"] = state_map.get(regime_result["state"], "neutral")
    # FIX: holdings使用中文键，构建VolTargetManager所需的英文键映射
    _holdings_en = {c: {"shares": h.get("数量", 0), "current_price": h.get("最新", 0), "buy_price": h.get("成本", 0)} for c, h in holdings.items()}
    vol_result = vtm.calc_position_scale(hist_dataframes, _holdings_en, market_info)
    print(f"  波动率: {vol_result['vol_regime']} | 缩放因子{vol_result['scale']:.2f}")
except Exception as e:
    print(f"  波动率: 计算失败({e})")

# --- 3. 目标仓位计算 + 操作指令生成 ---
_CAPITAL = getattr(config, 'TOTAL_CAPITAL', 1000000)
target_pct = 0.6  # 默认60%
if regime_result and vol_result:
    max_pos = regime_result["position_advice"]["max_position"]
    target_pct = max_pos * vol_result["scale"]
    target_pct = max(0.1, min(0.95, target_pct))

current_mv = sum(h.get("市值", 0) for h in holdings.values())
current_pct = current_mv / _CAPITAL if _CAPITAL > 0 else 0
need_reduce = current_pct - target_pct  # >0 表示需减仓
reduce_amount = need_reduce * _CAPITAL  # 需减掉的金额
_reduce_msg = f"需减仓{reduce_amount:.0f}元" if need_reduce > 0.02 else "无需减仓"
print(f"  仓位: 当前{current_pct:.0%} → 目标{target_pct:.0%} | {_reduce_msg}")

# 卖出优先级排序（浮亏深度40% + 趋势破位30% + 仓位集中度30%）
sell_candidates = []
for _code, _h in holdings.items():
    if _h.get("数量", 0) <= 0:
        continue
    _pnl = _h.get("盈亏比例", 0)
    _tech = tech_analysis.get(_code, {})
    _trend_score = _tech.get("composite", 50)
    _mv = _h.get("市值", 0)
    _concentration = _mv / current_mv if current_mv > 0 else 0
    _priority = (-_pnl * 0.4) + ((100 - _trend_score) * 0.3) + (_concentration * 100 * 0.3)
    sell_candidates.append((_priority, _code, _h, _tech))
sell_candidates.sort(key=lambda x: x[0], reverse=True)

# --- 4. 持仓趋势预测 ---
forecast_results = {}
try:
    from strategy.trend_forecast import TrendForecaster
    forecaster = TrendForecaster()
    for _code, _df in hist_dataframes.items():
        _holding_info = holdings.get(_code, {})
        _result = forecaster.analyze_stock(_code, _df, _holding_info)
        if _result.get("valid"):
            forecast_results[_code] = _result
    print(f"  趋势预测: {len(forecast_results)}只分析完成")
except Exception as e:
    print(f"  趋势预测: 失败({e})")

# --- 5. 反冲动锁检测 ---
anti_impulse_warnings = []
try:
    trades_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_today.json")
    if os.path.exists(trades_file):
        with open(trades_file, "r", encoding="utf-8") as tf:
            trades_data = json.load(tf)
        trade_date = trades_data.get("date", "")
        if trade_date:
            days_since = (datetime.date.today() - datetime.date.fromisoformat(trade_date)).days
            if days_since <= 1:
                for t in trades_data.get("trades", []):
                    anti_impulse_warnings.append({
                        "code": t["code"], "name": t["name"],
                        "direction": t["direction"], "date": trade_date
                    })
                print(f"  反冲动锁: 检测到{len(anti_impulse_warnings)}笔近期操作({trade_date})")
except Exception:
    pass

# --- 6. 回本计划 + 板块背离检测（任务#1新增，失败降级不影响主流程） ---
recovery_plan = None
sector_divergence = None
_rp_holdings_input = []
try:
    _rp_holdings_input = [
        {
            "code": _c,
            "name": _h.get("名称", _c),
            "shares": _h.get("数量", 0),
            "price": _h.get("最新", 0),
            "sector": _h.get("赛道", ""),
            "市值": _h.get("市值", 0),
        }
        for _c, _h in holdings.items() if _h.get("数量", 0) > 0
    ]
except Exception:
    pass

if HAS_REPORT_EXT:
    try:
        recovery_plan = build_recovery_plan(
            _rp_holdings_input,
            current_mv,
            getattr(config, "RECOVERY_TARGET_AMOUNT", 0),
            months=getattr(config, "RECOVERY_PLAN_MONTHS", (3, 6, 12)),
            forecast_results=forecast_results,
        )
        print(f"  回本计划: 缺口{getattr(config, 'RECOVERY_TARGET_AMOUNT', 0)/10000:.0f}万 | "
              f"所需组合收益{recovery_plan.get('required_return_pct', 0)*100:.1f}%")
        # 进度快照落盘（失败不影响报告）
        try:
            track_progress(
                current_mv,
                getattr(config, "RECOVERY_TARGET_AMOUNT", 0),
                getattr(config, "RECOVERY_PLAN_FILE",
                        os.path.join(config.PROJECT_ROOT, "output", "recovery_progress.json")),
            )
        except Exception:
            pass
    except Exception as _rpe:
        recovery_plan = None
        print(f"  回本计划: 计算失败({_rpe})")

    try:
        _sec_cache_path = os.path.join(config.PROJECT_ROOT, "data", "sector_rotation_cache.json")
        _sec_cache = load_sector_cache(_sec_cache_path)
        sector_divergence = detect_divergence(_rp_holdings_input, _sec_cache)
        if sector_divergence.get("available"):
            print(f"  板块背离: 滞涨{len(sector_divergence.get('lagging_positions', []))}只 | "
                  f"未覆盖热点{sector_divergence.get('missed_hotspots', [])}")
        else:
            print(f"  板块背离: 赛道轮动缓存不可用（过期或缺失）")
    except Exception as _sde:
        sector_divergence = None
        print(f"  板块背离: 检测失败({_sde})")

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
.signal-tag{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:bold}}
.signal-bullish{{background:#27ae60;color:white}}
.signal-bearish{{background:#e74c3c;color:white}}
.signal-neutral{{background:#95a5a6;color:white}}
.pattern-tag{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}}
.pattern-accumulation{{background:#27ae60;color:white}}
.pattern-distribution{{background:#e74c3c;color:white}}
.pattern-washout{{background:#f39c12;color:white}}
.flow-bar{{height:8px;border-radius:4px}}
.flow-positive{{background:#e74c3c}}
.flow-negative{{background:#27ae60}}
.explain-card{{background:#f5f5f5;border-radius:8px;padding:14px 16px;margin:12px 0;font-size:12px;color:#555;line-height:1.9;border:1px solid #e8e8e8}}
.explain-card .ec-title{{font-weight:bold;color:#333;font-size:13px;margin-bottom:6px;display:block}}
.explain-card b{{color:#333}}
</style></head><body><div class="container">
<div class="header">
<h1>📊 持仓综合分析报告 <span class="mode-badge">技术面+条件单</span></h1>
<div class="sub">日期: {today} | 行情: {quote_time_str} ({data_source}) | 止损规则: 最新价×{1-STOP_LOSS_PCT:.0%} | 持仓周期: 3天-4周波段</div>
</div>
<div class="content">
"""

# ---- 风险预警 ----
html += '<h2>⚠️ 仓位风险预警</h2>'

# 动态计算仓位集中度
# FIX P1: 改用实时行情市值（原 Bug: 用holdings.json静态current_price，盘中偏差>3%）
try:
    total_mv = sum(h.get('市值', 0) for h in holdings.values())
    stock_mvs = {}
    for k, h in holdings.items():
        mv = h.get('市值', 0)
        stock_mvs[k] = {'name': h.get('名称', k), 'mv': mv, 'pct': mv / total_mv * 100 if total_mv > 0 else 0}
    sorted_mvs = sorted(stock_mvs.items(), key=lambda x: x[1]['pct'], reverse=True)
    # 显示超过20%的仓位预警
    for code_w, info_w in sorted_mvs:
        if info_w['pct'] > 20:
            html += f'<div class="alert alert-danger">🚨 <b>{info_w["name"]}仓位{info_w["pct"]:.1f}%</b>，超出单只上限20%！建议分批减仓。</div>'
    # 集中度预警：前2大持仓
    if len(sorted_mvs) >= 2:
        top2_pct = sorted_mvs[0][1]['pct'] + sorted_mvs[1][1]['pct']
        top2_names = f"{sorted_mvs[0][1]['name']}({sorted_mvs[0][1]['pct']:.1f}%) + {sorted_mvs[1][1]['name']}({sorted_mvs[1][1]['pct']:.1f}%)"
        if top2_pct > 60:
            html += f'<div class="alert alert-warning">⚡ 持仓集中度: {top2_names} = <b>{top2_pct:.1f}%</b>集中在2只标的，风险较高。建议单只不超30%。</div>'
    html += f'<div class="alert alert-info">💰 账户总市值: ¥{total_mv:,.2f} | 持仓{len(holdings)}只</div>'
except Exception as e:
    html += f'<div class="alert alert-warning">仓位数据读取异常: {e}</div>'

# ---- 隔夜外盘前瞻板块 ----
try:
    if overnight_data.get("available"):
        html += _overnight.get_summary_html()
except Exception:
    pass  # 外盘板块渲染失败不影响报告

# ---- 零、回本目标计划（任务#1新增） ----
try:
    html += '<h2>零、回本目标计划</h2>'
    if recovery_plan and recovery_plan.get("available"):
        _rp_gap = recovery_plan["target_gap"]
        _rp_tv = recovery_plan["total_value"]
        _rp_req = recovery_plan["required_return_pct"]
        _rp_prog = _rp_tv / (_rp_tv + _rp_gap) * 100 if (_rp_tv + _rp_gap) > 0 else 0
        _rp_bar_w = max(2, min(100, _rp_prog))
        html += '<div style="background:#fafafa;border:1px solid #e8e8e8;border-radius:8px;padding:14px 18px;margin:10px 0">'
        html += f'<b>回本缺口:</b> <span style="color:#cf1322;font-weight:bold;font-size:15px">{_rp_gap/10000:.1f}万元</span>'
        html += f'<span style="color:#888;margin-left:12px">当前市值 {_rp_tv/10000:.1f}万</span>'
        html += f'<span style="color:#888;margin-left:12px">所需组合收益 <b style="color:#cf1322">+{_rp_req*100:.1f}%</b></span>'
        html += f'<div style="background:#eee;border-radius:4px;height:14px;margin-top:8px;position:relative">'
        html += f'<div style="background:linear-gradient(90deg,#52c41a,#1976d2);width:{_rp_bar_w:.1f}%;height:14px;border-radius:4px"></div></div>'
        html += f'<div style="font-size:11px;color:#666;margin-top:4px">回本进度 {_rp_prog:.1f}%（已达成 = 当前市值 / (当前市值+缺口)）</div>'
        html += '</div>'

        # 逐只目标价表
        html += '<table><tr><th>代码</th><th>名称</th><th>现价</th><th>目标价</th><th>所需涨幅</th><th>仓位权重</th><th>可行性</th></tr>'
        for _st in recovery_plan.get("stock_targets", []):
            _feas_color = "#ff9800" if _st.get("need_swap") else "#4caf50"
            _res_note = f"<br><span style='color:#999;font-size:10px'>压力位{_st['resistance']:.2f}</span>" if _st.get("resistance", 0) > 0 else ""
            html += f'<tr><td>{_st["code"]}</td><td><b>{_st["name"]}</b></td>'
            html += f'<td>{_st["price"]:.2f}</td>'
            html += f'<td style="color:#cf1322;font-weight:bold">{_st["target_price"]:.2f}</td>'
            html += f'<td style="color:#cf1322">+{_st["required_gain_pct"]:.1f}%</td>'
            html += f'<td>{_st["weight"]*100:.1f}%</td>'
            html += f'<td style="color:{_feas_color};font-size:11px">{"⚠️" + _st["feasibility"] if _st.get("need_swap") else "✅" + _st["feasibility"]}{_res_note}</td></tr>'
        html += '</table>'

        # 三档期限月化要求表
        html += '<table style="width:60%"><tr><th>回本期限</th><th>月化要求收益</th><th>评级</th></tr>'
        for _hz in recovery_plan.get("horizons", []):
            _rt = _hz["rating"]
            _rt_color = "#cf1322" if _rt == "激进" else ("#ff9800" if _rt == "偏积极" else "#4caf50")
            html += f'<tr><td>{_hz["months"]}个月</td>'
            html += f'<td style="font-weight:bold">+{_hz["monthly_required_pct"]:.2f}%/月</td>'
            html += f'<td style="color:{_rt_color};font-weight:bold">{_rt}</td></tr>'
        html += '</table>'

        html += f'<div class="alert alert-danger">⚠️ <b>风险提示:</b> {"%.0f" % (_rp_gap/10000)}万回本目标需组合上涨约+{_rp_req*100:.0f}%，属于<b>激进目标</b>。'
        html += '以上仅为数学计算展示，不构成任何收益承诺，系统不会因此产生自动交易。请优先控制回撤，切勿为回本而放大仓位或频繁交易。</div>'
    elif recovery_plan is not None:
        html += '<div class="alert alert-success">✅ 回本目标已达成或无缺口，无需回本计划</div>'
    else:
        html += '<div class="alert alert-warning">回本计划模块不可用（导入或计算失败），已降级跳过</div>'
except Exception as _rp_render_e:
    html += f'<div class="alert alert-warning">回本计划渲染失败: {_rp_render_e}</div>'

# ---- 总览表 ----
html += '<h2>一、持仓综合总览（行情+盈亏+操作建议）</h2>'
html += f'<div class="alert alert-info">📡 行情源: {data_source} | 时间: {quote_time_str or now} | 止损规则: 最新价×{1-STOP_LOSS_PCT:.0%} | 止盈基于压力位/ATR推算</div>'

html += '<table><tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>最新价</th><th>盈亏%</th><th>技术评分</th><th>趋势</th><th>止损价</th><th>止盈目标</th><th>操作建议</th></tr>'
for code, h in holdings.items():
    tech = h["技术"]
    chg_class = "up" if h['涨跌幅'] >= 0 else "down"
    comp = tech.get("composite", 0) if tech.get("valid") else 0
    trend_dir = tech.get("trend_dir", "N/A") if tech.get("valid") else "数据不足"

    # 评分颜色
    if comp >= 60:
        score_color = "#4caf50"
    elif comp >= 40:
        score_color = "#ff9800"
    else:
        score_color = "#f44336"

    # 盈亏颜色
    pnl = h.get("盈亏比例", 0)
    pnl_color = "#e74c3c" if pnl >= 0 else "#27ae60"
    pnl_str = f"{pnl:+.1f}%" if h.get("成本", 0) > 0 else "-"

    # 止盈显示
    tp1 = h.get("止盈目标1", 0)
    tp_str = f"{tp1:.3f}" if tp1 > 0 else "-"

    # 操作建议
    action = h.get("操作建议", "-")
    action_color = h.get("建议颜色", "#333")

    html += f'<tr><td>{code}</td><td><b>{h["名称"]}</b></td>'
    html += f'<td>{h.get("数量", 0)}</td>'
    html += f'<td>{h.get("成本", 0):.3f}</td>' if h.get("成本", 0) > 0 else '<td>-</td>'
    html += f'<td><b>{h["最新"]:.3f}</b></td>'
    html += f'<td style="color:{pnl_color};font-weight:bold">{pnl_str}</td>'
    html += f'<td style="color:{score_color};font-weight:bold">{comp}</td>'
    html += f'<td>{trend_dir}</td>'
    html += f'<td style="color:#e74c3c;font-weight:bold">{h["止损价"]:.3f}</td>'
    html += f'<td style="color:#1976d2;font-weight:bold">{tp_str}</td>'
    html += f'<td style="color:{action_color};font-weight:bold">{action}</td></tr>'
html += '</table>'

# ---- V2.5: 今日操作回顾 ----
html += '<h2>一-2、今日操作回顾</h2>'
try:
    _trades_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trades_today.json')
    if os.path.exists(_trades_file):
        with open(_trades_file, 'r', encoding='utf-8') as _tf:
            _trades_data = json.load(_tf)
        _trades = [t for t in _trades_data.get('trades', []) if t.get('status') == '已成']
        _summary = _trades_data.get('summary', {})
        if _trades:
            html += f'<p>日期: {_trades_data.get("date", today)} | '
            html += f'总笔数: {_summary.get("total_trades", len(_trades))} | '
            html += f'买入: {_summary.get("buy_count", 0)}笔 / 卖出: {_summary.get("sell_count", 0)}笔 | '
            html += f'买入金额: {_summary.get("buy_amount", 0)/10000:.2f}万 / 卖出金额: {_summary.get("sell_amount", 0)/10000:.2f}万</p>'
            # 操作明细表
            html += '<table><tr><th>时间</th><th>代码</th><th>名称</th><th>方向</th><th>数量</th><th>价格</th><th>金额(万)</th></tr>'
            for t in _trades:
                dir_color = '#e74c3c' if t['direction'] == '买入' else '#27ae60'
                html += f'<tr><td>{t["time"]}</td><td>{t["code"]}</td><td>{t["name"]}</td>'
                html += f'<td style="color:{dir_color};font-weight:bold">{t["direction"]}</td>'
                html += f'<td>{t["qty"]}</td><td>{t["price"]:.3f}</td><td>{t["amount"]/10000:.2f}</td></tr>'
            html += '</table>'
            # 清仓/新建仓提示
            cleared = _summary.get('cleared_stocks', [])
            new_pos = _summary.get('new_positions', [])
            if cleared:
                html += f'<p style="color:#e74c3c">清仓: {", ".join(cleared)}</p>'
            if new_pos:
                html += f'<p style="color:#1976d2">新建仓: {", ".join(new_pos)}</p>'
        else:
            html += '<p style="color:#999">今日无已成委托</p>'
    else:
        html += '<p style="color:#999">无委托数据文件(trades_today.json)</p>'
except Exception as e:
    html += f'<p style="color:#999">委托数据读取失败: {e}</p>'

# ---- 📋 今日操作执行清单（前移: 紧急操作指令优先展示） ----
html += '<h2>📋 今日操作执行清单</h2>'

# 五档仓位状态（任务#1新增）: ≤10%清仓/≤35%轻仓/≤65%标准/≤85%重仓/>85%满仓
try:
    def _pos_tier(_p):
        if _p <= 0.10:
            return "清仓"
        if _p <= 0.35:
            return "轻仓"
        if _p <= 0.65:
            return "标准"
        if _p <= 0.85:
            return "重仓"
        return "满仓"
    html += f'<div class="alert alert-info">📊 仓位状态: 当前仓位{current_pct*100:.0f}%（{_pos_tier(current_pct)}档）→ 目标{target_pct*100:.0f}%（{_pos_tier(target_pct)}档）</div>'
except Exception:
    pass

# 大盘状态卡片
if regime_result:
    _state = regime_result["state"]
    _state_cn = regime_result["state_cn"]
    _conf = regime_result["confidence"]
    _state_color = {"BULL": "#52c41a", "RANGE": "#faad14", "BEAR": "#ff4d4f"}.get(_state, "#999")
    _strategy = ""
    try:
        from strategy.market_regime import MarketRegimeDetector as _MRD
        _advice = _MRD().get_strategy_advice(regime_result)
        _strategy = _advice.get("primary_strategy", "")
    except Exception:
        pass
    html += f'''<div style="background:#fafafa;border:1px solid #e8e8e8;border-radius:8px;padding:14px 18px;margin:10px 0">
<b>大盘状态:</b> <span style="color:{_state_color};font-weight:bold;font-size:15px">{_state_cn}</span>
<span style="color:#888;margin-left:10px">置信度 {_conf:.0%}</span>
<span style="color:#888;margin-left:10px">策略: {_strategy}</span><br>
<b>目标仓位:</b> {target_pct:.0%} | <b>当前仓位:</b> {current_pct:.0%} | <b>总市值:</b> {current_mv/10000:.1f}万'''
    if vol_result:
        html += f' | <b>波动率:</b> {vol_result["vol_regime"]}(缩放{vol_result["scale"]:.2f})'
    html += '</div>'
else:
    html += '<div style="background:#fffbe6;border:1px solid #ffe58f;border-radius:6px;padding:10px 14px;margin:10px 0;color:#ad6800">大盘数据暂不可用，默认目标仓位60%</div>'

# 操作指令区
if need_reduce > 0.02 and reduce_amount > 5000:
    html += f'<p style="color:#ff4d4f;font-weight:bold;margin:12px 0">⚠️ 需减仓: 当前仓位{current_pct:.0%}超过目标{target_pct:.0%}，建议减持约{reduce_amount/10000:.1f}万元</p>'
    html += '''<table style="width:100%;border-collapse:collapse;font-size:12px;margin:8px 0">
<tr style="background:#fff1f0"><th style="padding:8px;border:1px solid #ffa39e">优先级</th><th style="padding:8px;border:1px solid #ffa39e">代码/名称</th><th style="padding:8px;border:1px solid #ffa39e">操作</th><th style="padding:8px;border:1px solid #ffa39e">卖出股数</th><th style="padding:8px;border:1px solid #ffa39e">挂单价</th><th style="padding:8px;border:1px solid #ffa39e">分笔建议</th><th style="padding:8px;border:1px solid #ffa39e">时间窗口</th></tr>'''
    _remaining_reduce = reduce_amount
    for _idx, (_pri, _code, _h, _tech) in enumerate(sell_candidates, 1):
        if _remaining_reduce <= 0:
            break
        _shares = _h.get("数量", 0)
        _price = _h.get("最新", 0)
        if _shares <= 0 or _price <= 0:
            continue
        _name = _h.get("名称", _code)
        _pnl = _h.get("盈亏比例", 0)
        # 计算卖出股数
        _sell_shares = int(min(_remaining_reduce / _price, _shares) / 100) * 100
        if _sell_shares <= 0:
            _sell_shares = 100
        _sell_shares = min(_sell_shares, _shares)
        # 挂单价: 紧急(浮亏>5%)用现价，否则用支撑位
        _support = 0
        _fc = forecast_results.get(_code, {})
        if _fc:
            _support = _fc.get("levels", {}).get("first_support", 0)
        if _pnl < -5:
            _order_price = round(_price, 2)
            _time_window = "开盘即执行"
            _urgency = "紧急"
        else:
            _order_price = round(_support, 2) if _support > 0 and _support < _price else round(_price * 0.99, 2)
            _time_window = "14:30-14:50"
            _urgency = "建议"
        # 分笔建议
        if _sell_shares > 500:
            _split = f"分2笔: 第1笔现价卖{_sell_shares//2//100*100}股, 第2笔反弹至{round(_price*1.01,2)}再卖"
        else:
            _split = "一笔执行"
        _remaining_reduce -= _sell_shares * _price
        html += f'''<tr><td style="padding:6px;border:1px solid #f0f0f0;text-align:center">{_idx}</td>
<td style="padding:6px;border:1px solid #f0f0f0">{_code} {_name}<br><span style="color:#888">浮盈{_pnl:+.1f}%</span></td>
<td style="padding:6px;border:1px solid #f0f0f0;color:#ff4d4f;font-weight:bold">卖出</td>
<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;font-weight:bold">{_sell_shares}股</td>
<td style="padding:6px;border:1px solid #f0f0f0;text-align:center">{_order_price}</td>
<td style="padding:6px;border:1px solid #f0f0f0;font-size:11px">{_split}</td>
<td style="padding:6px;border:1px solid #f0f0f0;text-align:center">{_time_window}</td></tr>'''
    html += '</table>'
    html += '<p style="color:#888;font-size:11px;margin:4px 0">ℹ️ 操作时间窗口建议: 14:30-14:50执行，避免早盘假突破。紧急标的除外。</p>'
else:
    html += '<div style="background:#f6ffed;border:1px solid #b7eb8f;border-radius:6px;padding:12px 16px;margin:10px 0;color:#389e0d;font-weight:bold">✅ 今日无需减仓操作，维持现有仓位</div>'

# 加仓分支（任务#1新增）: 当前仓位显著低于目标且存在推荐候选时，输出买入指令表
try:
    if current_pct < target_pct - 0.02 and recommendations:
        _add_total = (target_pct - current_pct) * _CAPITAL
        html += f'<p style="color:#389e0d;font-weight:bold;margin:12px 0">📈 可加仓: 当前仓位{current_pct:.0%}低于目标{target_pct:.0%}，可增配约{_add_total/10000:.1f}万元（以下为候选，需人工确认后手动执行）</p>'
        html += '''<table style="width:100%;border-collapse:collapse;font-size:12px;margin:8px 0">
<tr style="background:#f6ffed"><th style="padding:8px;border:1px solid #b7eb8f">代码</th><th style="padding:8px;border:1px solid #b7eb8f">名称</th><th style="padding:8px;border:1px solid #b7eb8f">买入区间</th><th style="padding:8px;border:1px solid #b7eb8f">止损价</th><th style="padding:8px;border:1px solid #b7eb8f">建议股数</th><th style="padding:8px;border:1px solid #b7eb8f">预估金额</th></tr>'''
        for _ar in recommendations[:3]:
            _ap = _ar.get("plan") or {}
            _a_bl = _ap.get("buy_low", 0)
            _a_bh = _ap.get("buy_high", 0)
            _a_sl = _ap.get("stop_loss", 0)
            _a_price = _ar.get("price", 0) or _a_bh
            if _a_price <= 0:
                continue
            # 建议股数 = plan.position_pct(% × 总资金) / 参考价，取整100股
            _a_pos_pct = _ap.get("position_pct", 0) or 0
            _a_shares = int(_a_pos_pct / 100.0 * _CAPITAL / _a_price / 100) * 100
            if _a_shares <= 0:
                _a_shares = 100
            _a_range = f"{_a_bl:.2f}~{_a_bh:.2f}" if _a_bl > 0 and _a_bh > 0 else f"{_a_price*0.98:.2f}~{_a_price*1.01:.2f}(参考)"
            _a_sl_str = f"{_a_sl:.2f}" if _a_sl > 0 else f"{_a_price*0.95:.2f}(-5%)"
            html += f'<tr><td style="padding:6px;border:1px solid #f0f0f0;text-align:center">{_ar.get("code", "")}</td>'
            html += f'<td style="padding:6px;border:1px solid #f0f0f0"><b>{_ar.get("name", "")}</b><br><span style="color:#888;font-size:11px">{_ar.get("sector", "")} 评分{_ar.get("total_score", 0)}</span></td>'
            html += f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;color:#1976d2;font-weight:bold">{_a_range}</td>'
            html += f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;color:#e74c3c">{_a_sl_str}</td>'
            html += f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;font-weight:bold">{_a_shares}股</td>'
            html += f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center">{_a_shares*_a_price:,.0f}元</td></tr>'
        html += '</table>'
        html += '<p style="color:#888;font-size:11px;margin:4px 0">ℹ️ 加仓仅在仓位低于目标2%以上时提示，买入前请确认大盘状态与个股买点，不追高。</p>'
except Exception as _add_e:
    html += f'<div class="alert alert-warning">加仓建议生成异常: {_add_e}</div>'

# 逐只一句话决策矩阵（任务#1新增）: 重仓=评分≥70且赛道非weak；清仓=评分<30或破止损；其余=持有
try:
    _dm_weak = set((sector_divergence or {}).get("weak_sectors", [])) if (sector_divergence and sector_divergence.get("available")) else set()
    _dm_cmap = get_coarse_sector_map() if HAS_REPORT_EXT else {}
    html += '<h3 style="font-size:14px;color:#1565c0;margin-top:15px">🎯 逐只一句话决策矩阵</h3>'
    html += '<table><tr><th>代码</th><th>名称</th><th>综合评分</th><th>所属赛道</th><th>一句话决策</th></tr>'
    for _code, _h in holdings.items():
        if _h.get("数量", 0) <= 0:
            continue
        _dm_tech = _h.get("技术", {})
        _dm_comp = _dm_tech.get("composite", 0) if _dm_tech.get("valid") else 0
        _dm_coarse = _dm_cmap.get(_h.get("赛道", ""), _h.get("赛道", ""))
        _dm_latest = _h.get("最新", 0)
        _dm_stop = _h.get("止损价", 0)
        if _dm_comp >= 70 and _dm_coarse not in _dm_weak:
            _dm_action, _dm_color = "重仓持有（评分强势且赛道非弱势）", "#389e0d"
        elif _dm_comp < 30 or (_dm_stop > 0 and _dm_latest > 0 and _dm_latest <= _dm_stop):
            _dm_action, _dm_color = "清仓（评分过低或已破止损）", "#cf1322"
        else:
            _dm_action, _dm_color = f"持有（评分{_dm_comp}）", "#1976d2"
        html += f'<tr><td>{_code}</td><td><b>{_h.get("名称", _code)}</b></td>'
        html += f'<td style="font-weight:bold">{_dm_comp}</td>'
        html += f'<td>{_dm_coarse or "—"}</td>'
        html += f'<td style="color:{_dm_color};font-weight:bold">{_dm_action}</td></tr>'
    html += '</table>'
except Exception as _dm_e:
    html += f'<div class="alert alert-warning">决策矩阵生成异常: {_dm_e}</div>'

# 反冲动锁警告
if anti_impulse_warnings:
    _warn_names = ", ".join(set(f"{w['name']}({w['direction']})" for w in anti_impulse_warnings[:5]))
    html += f'''<div style="background:#fff2e8;border:1px solid #ffbb96;border-radius:6px;padding:12px 16px;margin:10px 0;color:#d4380d">
<b>⚠️ 反冲动锁警告:</b> 检测到{anti_impulse_warnings[0].get("date","")}有操作记录: {_warn_names}<br>
<span style="font-size:12px">如今日建议方向与昨日操作相反，请冷静24小时再决策，避免情绪化反复操作。</span></div>'''

# ---- 板块轮动与调仓建议（任务#1新增） ----
try:
    html += '<h2>🔄 板块轮动与调仓建议</h2>'
    if sector_divergence is None or not sector_divergence.get("available"):
        html += '<div class="alert alert-warning">板块数据不可用（缓存过期），跳过背离分析</div>'
    else:
        _sd_scores = sector_divergence.get("holding_sector_scores", {})
        _sd_lag = sector_divergence.get("lagging_positions", [])
        _sd_missed = sector_divergence.get("missed_hotspots", [])
        _sd_top = sector_divergence.get("top_strong_sectors", [])
        _sd_cmap = get_coarse_sector_map() if HAS_REPORT_EXT else {}

        # 持仓赛道评分对照表
        html += '<table><tr><th>代码</th><th>名称</th><th>持仓赛道</th><th>粗赛道</th><th>赛道评分</th><th>状态</th></tr>'
        _sd_weak_set = set(sector_divergence.get("weak_sectors", []))
        for _code, _h in holdings.items():
            if _h.get("数量", 0) <= 0:
                continue
            _raw_sector = _h.get("赛道", "")
            _coarse_s = _sd_cmap.get(_raw_sector, _raw_sector)
            _s_score = _sd_scores.get(_code)
            _s_score_str = f"{_s_score:.1f}" if _s_score is not None else "中性"
            if _coarse_s in _sd_weak_set:
                _s_state, _s_color = "弱势", "#cf1322"
            elif _s_score is None:
                _s_state, _s_color = "中性", "#999"
            else:
                _s_state, _s_color = "正常", "#389e0d"
            html += f'<tr><td>{_code}</td><td><b>{_h.get("名称", _code)}</b></td>'
            html += f'<td>{_raw_sector}</td><td>{_coarse_s}</td>'
            html += f'<td style="font-weight:bold">{_s_score_str}</td>'
            html += f'<td style="color:{_s_color};font-weight:bold">{_s_state}</td></tr>'
        html += '</table>'

        # 背离警示: 滞涨暴露 + 今日最强3赛道
        if _sd_lag:
            _lag_names = "、".join(f'{lp["name"]}({_sd_cmap.get(lp.get("sector",""), lp.get("sector",""))}·建议减{lp["建议减持金额"]/10000:.1f}万/{lp["建议减持股数"]}股)' for lp in _sd_lag)
            html += f'<div class="alert alert-danger">⚠️ <b>板块背离警示:</b> 持仓暴露于滞涨赛道: {_lag_names}</div>'
        if _sd_top:
            html += f'<div class="alert alert-info">🔥 今日最强3赛道: {"、".join(_sd_top)}'
            if _sd_missed:
                html += f' | 持仓未覆盖热点: {"、".join(_sd_missed)}'
            html += '</div>'

        # 减持X → 关注Y 建议行（Y从 recommendations/watchlist 按强势赛道筛选）
        _sd_strong_set = set(sector_divergence.get("strong_sectors", []))
        _sd_hot_picks = []
        for _cand in list(recommendations or []) + list(watchlist or []):
            _c_coarse = _sd_cmap.get(_cand.get("sector", ""), _cand.get("sector", ""))
            if _c_coarse in _sd_strong_set and _cand not in _sd_hot_picks:
                _sd_hot_picks.append(_cand)
        if _sd_lag:
            for _lp in _sd_lag[:3]:
                _y_txt = ""
                if _sd_hot_picks:
                    _yp = _sd_hot_picks.pop(0)
                    _y_txt = f'{_yp.get("name", "")}({_yp.get("code", "")}·评分{_yp.get("total_score", 0)})'
                html += '<div style="background:#f0f7ff;border:1px solid #b3d9ff;border-radius:6px;padding:10px 14px;margin:8px 0">'
                html += f'🔁 减持 <b style="color:#cf1322">{_lp["name"]}</b>（约{_lp["建议减持金额"]/10000:.1f}万元/{_lp["建议减持股数"]}股，{_lp.get("原因", "赛道弱势")}）'
                if _y_txt:
                    html += f' → 关注 <b style="color:#389e0d">{_y_txt}</b>'
                else:
                    html += ' → 暂无强势赛道候选可替换，建议先减持后等待买点'
                html += '</div>'
        elif not _sd_missed:
            html += '<div class="alert alert-success">✅ 持仓赛道与市场热点无显著背离，无需板块调仓</div>'
except Exception as _sd_e:
    html += f'<div class="alert alert-warning">板块轮动分析渲染失败: {_sd_e}</div>'

# ---- 逐只详细分析 ----
html += '<h2>二、逐只技术分析 + 操作建议 + 条件单</h2>'

for code, h in holdings.items():
    tech = h["技术"]
    latest = h["最新"]
    chg_class = "up" if h['涨跌幅'] >= 0 else "down"

    # V2.5: 已清仓标的跳过技术分析
    if h.get("数量", 0) == 0:
        html += f'<div class="stock-card"><h3>{code} {h["名称"]} <span style="color:#999">({h["赛道"]})</span> <span class="tag tag-stop">已清仓</span></h3>'
        html += f'<p style="color:#999">今日已清仓，不再生成技术分析。最新价: {latest:.3f}</p></div>'
        continue

    if not tech.get("valid"):
        html += f'<div class="stock-card"><h3>{code} {h["名称"]} <span style="color:#999">({h["赛道"]})</span></h3>'
        html += f'<p style="color:#999">技术分析数据不足: {tech.get("error", "未知")}</p>'
        html += f'<p>止损价: <b style="color:#e74c3c">{h["止损价"]:.3f}</b> | 止盈目标: <b style="color:#1976d2">{h.get("止盈目标1", 0):.3f}</b></p></div>'
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

    # 盈亏信息
    pnl_pct = h.get("盈亏比例", 0)
    pnl_amt = h.get("盈亏金额", 0)
    cost_price = h.get("成本", 0)
    shares = h.get("数量", 0)
    tp1 = h.get("止盈目标1", 0)
    tp2 = h.get("止盈目标2", 0)
    tp_basis = h.get("止盈依据", "")
    action = h.get("操作建议", "")
    action_reason = h.get("建议理由", "")
    action_color = h.get("建议颜色", "#333")

    # ---- V2.7: 加仓计划计算 ----
    _ma20_val = tech.get("ma20", latest)
    _resistances = tech.get("resistances", [])
    _atr_val = tech.get("atr", latest * 0.02)
    # 加仓触发价: 优先用第一压力位突破，否则用MA20*1.03
    if _resistances and _resistances[0][1] > latest * 1.01:
        add_trigger_price = round(_resistances[0][1], 3)
        _add_trigger_desc = f"突破{_resistances[0][0]}压力位"
    else:
        add_trigger_price = round(latest * 1.05, 3)
        _add_trigger_desc = "现价+5%确认突破"
    # 加仓股数: 现有持仓的30%
    add_shares = max(int(shares * 0.3 / 100) * 100, 100)
    add_amount = add_shares * add_trigger_price
    # 加仓前置条件
    _add_conds = []
    if comp >= 60:
        _add_conds.append("技术评分>=60")
    _add_conds.append("DK D点确认或量比>1.5")
    _add_conds.append("板块指数同步走强")
    add_preconditions = " + ".join(_add_conds)
    # 加仓后综合止损
    _avg_cost_after = (cost_price * shares + add_trigger_price * add_shares) / (shares + add_shares) if (shares + add_shares) > 0 else cost_price
    add_stop_loss = round(_avg_cost_after * (1 - STOP_LOSS_PCT), 3)

    # 隔夜外盘条件单提示（仅当行业影响分>=3时显示）
    _overnight_hint = ""
    try:
        if overnight_data.get("available"):
            _hint = _overnight.get_condition_hint_for_sector(h.get("赛道", ""))
            if _hint:
                _overnight_hint = f'<div style="background:#fff8e1;border:1px solid #ffcc02;border-radius:4px;padding:8px 12px;margin:8px 0;font-size:12px">{_hint}</div>'
    except Exception:
        pass

    # 操作建议背景色
    if "止损" in action or "清仓" in action:
        advice_bg = "#fff5f5"
        advice_border = "#e74c3c"
    elif "止盈" in action or "减仓" in action:
        advice_bg = "#fff8e1"
        advice_border = "#ff9800"
    elif "持有" in action:
        advice_bg = "#f0fff0"
        advice_border = "#4caf50"
    else:
        advice_bg = "#f5f5f5"
        advice_border = "#999"

    html += f"""
<div class="stock-card">
<h3>{code} {h['名称']} {action_tag} <span style="font-size:12px;color:#888">({h['赛道']})</span>
<span style="float:right;font-size:13px">{trend_dir}</span></h3>

<div style="background:{advice_bg};border-left:4px solid {advice_border};padding:10px 14px;margin:8px 0;border-radius:4px">
<b style="color:{action_color};font-size:15px">📌 操作建议: {action}</b><br>
<span style="font-size:12px;color:#555">{action_reason}</span>
</div>

<div style="margin:8px 0">
<span style="font-size:12px;color:#666">综合评分: <b style="color:{bar_color}">{comp}/100</b></span>
<div class="score-bar"><div class="score-fill" style="width:{comp}%;background:{bar_color}"></div></div>
</div>

<div class="meta">
<span>最新价: <b>{latest:.3f}</b> <span class="{chg_class}">({h['涨跌幅']:+.2f}%)</span></span>
<span>成本: <b>{cost_price:.3f}</b> | 数量: <b>{shares}</b></span>
<span>盈亏: <b style="color:{'#e74c3c' if pnl_pct>=0 else '#27ae60'}">{pnl_pct:+.1f}% ({pnl_amt:+,.0f}元)</b></span>
<span>MA5: <b>{tech['ma5']}</b> | MA10: <b>{tech['ma10']}</b> | MA20: <b>{tech['ma20']}</b></span>
<span>RSI(14): <b>{tech['rsi']}</b> | MACD: <b>{tech['macd_dif']}/{tech['macd_dea']}</b></span>
<span>ATR(14): <b>{tech['atr']}</b> | 布林: <b>{tech['boll_lower']}-{tech['boll_upper']}</b></span>
</div>

<div style="margin:8px 0;font-size:12px">
<b>支撑位:</b> <span style="color:#4caf50">{support_str}</span><br>
<b>压力位:</b> <span style="color:#e74c3c">{resist_str}</span>
</div>

<ul class="signal-list">
{''.join(f'<li>{s}</li>' for s in all_signals)}
</ul>

<table>
<tr><th>条件单类型</th><th>具体设置</th><th>优先级</th><th>有效期</th></tr>
<tr><td><b>① 止损单</b></td><td style="color:#e74c3c;font-weight:bold">触发价 {h['止损价']:.3f}，委托价 {h['止损价']*0.995:.3f}（最新价×{1-STOP_LOSS_PCT:.0%}）</td><td>★★★必挂</td><td>20天</td></tr>
<tr><td><b>② 止盈单(减仓)</b></td><td style="color:#1976d2;font-weight:bold">触发价 {tp1:.3f}，卖出{shares//2}股（{tp_basis}）</td><td>★★建议</td><td>15天</td></tr>
<tr><td><b>③ 止盈单(清仓)</b></td><td style="color:#1976d2">触发价 {tp2:.3f}，全部清仓</td><td>★可选</td><td>20天</td></tr>
<tr><td><b>④ 回落卖出</b></td><td style="color:#ff9800">最高{h.get('回落基准', h['最高']):.3f}回落{h.get('回落比例', 0.07)*100:.0f}%至 {h['回落触发']:.3f} 卖出</td><td>★★建议</td><td>10天</td></tr>
</table>
{_overnight_hint}

<div style="background:#eef6ff;border:1px solid #b3d9ff;border-radius:6px;padding:10px 14px;margin:10px 0">
<b style="color:#1565c0;font-size:13px">[ADD] 加仓计划</b>
<table style="margin-top:6px;font-size:12px">
<tr><th style="width:100px;background:#e3f2fd">项目</th><th style="background:#e3f2fd">具体条件</th></tr>
<tr><td><b>加仓触发价</b></td><td style="color:#1565c0;font-weight:bold">{add_trigger_price:.3f} 元（{_add_trigger_desc}）</td></tr>
<tr><td><b>加仓仓位</b></td><td>首批50% + 加仓30% + 预留20% | 加仓股数: <b>{add_shares}</b>股 / {add_amount:,.0f}元</td></tr>
<tr><td><b>前置条件</b></td><td style="font-size:11px">{add_preconditions}</td></tr>
<tr><td><b>加仓后止损</b></td><td style="color:#e74c3c">{add_stop_loss:.3f} 元（综合成本×{1-STOP_LOSS_PCT:.0%}）</td></tr>
</table>
</div>

<div style="font-size:11px;color:#666;margin-top:5px">[INFO] {hold_reason} | 建议持有: {hold_suggest}</div>
</div>"""

# ---- V2.7: 次日开盘调仓计划 ----
# V3.2回测诊断: 调仓增益为-0.85%(202次调仓全部负增益)，收紧卖出条件+买入追高过滤
REBALANCE_SCORE_GAP = 25  # V3.2: 评分差门槛从20升至25（减少无效调仓）
REBALANCE_SELL_THRESHOLD = 30  # V3.2: 卖出门槛从40降至30（仅极端弱势才卖）
REBALANCE_BUY_THRESHOLD = 70  # V3.2: 买入门槛从65升至70（更严格筛选）
REBALANCE_MAX_POSITION_RATIO = 0.15  # 单只仓位上限15%
REBALANCE_TRADE_COST_RATE = 0.0015  # 交易成本(印花税+佣金)

html += '<h2>二-B、次日开盘调仓计划（基于综合评分再平衡）</h2>'
html += '<div class="alert alert-warning" style="font-size:12px">[DISCLAIMER] 以下为系统量化建议，实际操作请结合盘面判断，不构成投资建议</div>'

try:
    # 收集持仓评分
    _rebalance_holdings = []
    for _rb_code, _rb_h in holdings.items():
        if _rb_h.get("数量", 0) == 0:
            continue
        _rb_tech = _rb_h.get("技术", {})
        if not _rb_tech.get("valid"):
            continue
        _rebalance_holdings.append({
            "code": _rb_code,
            "name": _rb_h["名称"],
            "shares": _rb_h["数量"],
            "cost": _rb_h.get("成本", 0),
            "price": _rb_h.get("最新", 0),
            "score": _rb_tech.get("composite", 50),
            "pnl_pct": _rb_h.get("盈亏比例", 0),
            "sector": _rb_h.get("赛道", ""),
        })

    # 收集候选池评分(观察池+推荐)
    # 任务#1修复: 从 candidate_data 的 df 补入 momentum_5d（近5日涨幅，%），
    # 使 V3.2 追高过滤(>5%不买)真实生效（此前候选构造从未填充该键，过滤恒不触发）
    _momentum_5d_map = {}
    for _cd in candidate_data:
        try:
            _cdf = _cd.get("df")
            if _cdf is not None and len(_cdf) >= 6:
                _c_prev = float(_cdf["close"].iloc[-6])
                if _c_prev > 0:
                    _momentum_5d_map[_cd.get("code", "")] = round(
                        (float(_cdf["close"].iloc[-1]) / _c_prev - 1) * 100, 2)
        except Exception:
            pass

    _rebalance_candidates = []
    for _rb_w in (watchlist or []):
        _rb_plan = _rb_w.get("plan") or {}
        _rebalance_candidates.append({
            "code": _rb_w.get("code", ""),
            "name": _rb_w.get("name", ""),
            "score": _rb_w.get("total_score", 0),
            "sector": _rb_w.get("sector", ""),
            "price": _rb_w.get("price", 0),
            "buy_low": _rb_plan.get("buy_low", 0),
            "buy_high": _rb_plan.get("buy_high", 0),
            "stop_loss": _rb_plan.get("stop_loss", 0),
            "target_1": _rb_plan.get("target_1", 0),
            "momentum_5d": _momentum_5d_map.get(_rb_w.get("code", ""), 0),
        })
    for _rb_r in (recommendations or []):
        _rb_plan = _rb_r.get("plan") or {}
        _rebalance_candidates.append({
            "code": _rb_r.get("code", ""),
            "name": _rb_r.get("name", ""),
            "score": _rb_r.get("total_score", 0),
            "sector": _rb_r.get("sector", ""),
            "price": _rb_r.get("price", 0),
            "buy_low": _rb_plan.get("buy_low", 0),
            "buy_high": _rb_plan.get("buy_high", 0),
            "stop_loss": _rb_plan.get("stop_loss", 0),
            "target_1": _rb_plan.get("target_1", 0),
            "momentum_5d": _momentum_5d_map.get(_rb_r.get("code", ""), 0),
        })

    # 排序: 持仓按评分升序(最低在前), 候选按评分降序(最高在前)
    _rebalance_holdings.sort(key=lambda x: x["score"])
    _rebalance_candidates.sort(key=lambda x: x["score"], reverse=True)

    # 判定调仓信号
    _sell_list = []
    _buy_list = []
    _has_rebalance = False
    _momentum_blocked = False  # 任务#1新增: 记录追高过滤拦截状态，供未触发原因展示

    if _rebalance_holdings and _rebalance_candidates:
        _worst = _rebalance_holdings[0]
        _best = _rebalance_candidates[0]
        _score_gap = _best["score"] - _worst["score"]

        if (_worst["score"] < REBALANCE_SELL_THRESHOLD and
            _best["score"] > REBALANCE_BUY_THRESHOLD and
            _score_gap >= REBALANCE_SCORE_GAP):
            # V3.2: 买入端追高过滤 - 近5日涨幅>5%的不买（避免追高）
            _best_momentum = _best.get("momentum_5d", 0)
            if _best_momentum > 5:
                _has_rebalance = False  # 最佳候选近期涨幅过大，不调仓
                _momentum_blocked = True
            else:
                _has_rebalance = True

            # 卖出计划: 评分<40的全部清仓, 40-50的减仓50%
            for _sh in _rebalance_holdings:
                if _sh["score"] < REBALANCE_SELL_THRESHOLD:
                    _sell_ratio = 1.0
                    _sell_reason = f"评分{_sh['score']:.0f}<{REBALANCE_SELL_THRESHOLD}，趋势破位，全部清仓"
                elif _sh["score"] < 50:
                    _sell_ratio = 0.5
                    _sell_reason = f"评分{_sh['score']:.0f}偏低，减仓50%降低风险"
                else:
                    continue
                _sell_shares = int(_sh["shares"] * _sell_ratio / 100) * 100
                if _sell_shares < 100:
                    # FIX: 100股持仓“减仓50%”会变成全卖，添加注释明确这是设计意图
                    # 不足1手无法部分卖出，只能全清仓
                    _sell_shares = _sh["shares"]  # 不足1手则全卖（A股最小交易单位100股）
                _sell_price = round(_sh["price"] * 0.995, 3)  # 开盘价-0.5%限价
                _sell_amount = _sell_shares * _sell_price
                _sell_list.append({
                    **_sh,
                    "sell_shares": _sell_shares,
                    "sell_ratio": _sell_ratio,
                    "sell_price": _sell_price,
                    "sell_amount": _sell_amount,
                    "sell_reason": _sell_reason,
                })

            # 买入计划: 基于释放资金计算
            _released_cash = sum(s["sell_amount"] for s in _sell_list)
            _max_buy_amount = config.TOTAL_CAPITAL * REBALANCE_MAX_POSITION_RATIO
            _available_for_buy = min(_released_cash, _max_buy_amount)

            for _bc in _rebalance_candidates[:2]:  # 最多买2只
                if _bc["score"] <= REBALANCE_BUY_THRESHOLD:
                    break
                if _available_for_buy < 5000:  # 资金不足5000不再买入
                    break
                _buy_price = _bc["buy_high"] if _bc["buy_high"] > 0 else (_bc["price"] * 1.005 if _bc["price"] > 0 else 0)
                if _buy_price <= 0:
                    continue
                _buy_shares = int(_available_for_buy / _buy_price / 100) * 100
                if _buy_shares < 100:
                    break
                _buy_amount = _buy_shares * _buy_price
                _stop = _bc["stop_loss"] if _bc["stop_loss"] > 0 else round(_buy_price * 0.95, 2)
                # 三档买点
                _aggressive = round(_buy_price, 2)
                _moderate = round(_buy_price * 0.99, 2)
                _conservative = round(_buy_price * 0.97, 2)
                # 加仓条件
                _add_trigger = round(_buy_price * 1.05, 2)
                _buy_list.append({
                    **_bc,
                    "buy_shares": _buy_shares,
                    "buy_price": _buy_price,
                    "buy_amount": _buy_amount,
                    "stop_loss_price": _stop,
                    "aggressive_buy": _aggressive,
                    "moderate_buy": _moderate,
                    "conservative_buy": _conservative,
                    "add_trigger": _add_trigger,
                    "position_pct": _buy_amount / config.TOTAL_CAPITAL * 100,
                })
                _available_for_buy -= _buy_amount

    # 渲染HTML
    if _has_rebalance and (_sell_list or _buy_list):
        # 卖出区域
        if _sell_list:
            html += '<div style="background:#FFF0F0;border:1px solid #ffccc7;border-radius:6px;padding:12px;margin:10px 0">'
            html += '<b style="color:#cf1322;font-size:14px">[SELL] 卖出计划</b>'
            html += '<table style="margin-top:8px;font-size:12px"><tr><th>标的</th><th>持仓</th><th>成本</th><th>盈亏</th><th>评分</th><th>卖出比例</th><th>挂单价</th><th>卖出股数</th><th>释放资金</th><th>理由</th></tr>'
            for _s in _sell_list:
                _pnl_color = "#e74c3c" if _s["pnl_pct"] >= 0 else "#27ae60"
                html += f'<tr><td><b>{_s["name"]}</b>({_s["code"]})</td>'
                html += f'<td>{_s["shares"]}股</td><td>{_s["cost"]:.2f}</td>'
                html += f'<td style="color:{_pnl_color}">{_s["pnl_pct"]:+.1f}%</td>'
                html += f'<td style="color:#cf1322;font-weight:bold">{_s["score"]:.0f}</td>'
                html += f'<td>{"_ALL_" if _s["sell_ratio"]>=1.0 else "50%"}</td>'
                html += f'<td style="font-weight:bold">{_s["sell_price"]:.3f}</td>'
                html += f'<td>{_s["sell_shares"]}股</td>'
                html += f'<td style="color:#1976d2;font-weight:bold">{_s["sell_amount"]:,.0f}元</td>'
                html += f'<td style="font-size:11px">{_s["sell_reason"]}</td></tr>'
            html += '</table></div>'

        # 买入区域
        if _buy_list:
            html += '<div style="background:#F0FFF0;border:1px solid #b7eb8f;border-radius:6px;padding:12px;margin:10px 0">'
            html += '<b style="color:#389e0d;font-size:14px">[BUY] 买入计划</b>'
            html += '<table style="margin-top:8px;font-size:12px"><tr><th>标的</th><th>评分</th><th>赛道</th><th>买入股数</th><th>三档买点</th><th>止损价</th><th>仓位占比</th><th>加仓条件</th></tr>'
            for _b in _buy_list:
                html += f'<tr><td><b>{_b["name"]}</b>({_b["code"]})</td>'
                html += f'<td style="color:#389e0d;font-weight:bold">{_b["score"]:.0f}</td>'
                html += f'<td>{_b["sector"]}</td>'
                html += f'<td style="font-weight:bold">{_b["buy_shares"]}股 / {_b["buy_amount"]:,.0f}元</td>'
                html += f'<td style="font-size:11px">激进{_b["aggressive_buy"]:.2f} / 稳健{_b["moderate_buy"]:.2f} / 保守{_b["conservative_buy"]:.2f}</td>'
                html += f'<td style="color:#e74c3c">{_b["stop_loss_price"]:.2f}(-5%)</td>'
                html += f'<td>{_b["position_pct"]:.1f}%</td>'
                html += f'<td style="font-size:11px">突破{_b["add_trigger"]:.2f}且放量 + DK D点确认</td></tr>'
            html += '</table></div>'

        # 调仓汇总
        _total_sell = sum(s["sell_amount"] for s in _sell_list)
        _total_buy = sum(b["buy_amount"] for b in _buy_list)
        _net_change = _total_sell - _total_buy
        _trade_cost = (_total_sell + _total_buy) * REBALANCE_TRADE_COST_RATE
        # 调仓前后组合评分
        _before_scores = [(h["score"], h["shares"] * h["price"]) for h in _rebalance_holdings]
        _before_total_val = sum(v for _, v in _before_scores)
        _before_avg = sum(s * v for s, v in _before_scores) / _before_total_val if _before_total_val > 0 else 50
        # 调仓后: 移除卖出的, 加入买入的
        _after_items = [(h["score"], h["shares"] * h["price"]) for h in _rebalance_holdings if h["code"] not in [s["code"] for s in _sell_list if s["sell_ratio"] >= 1.0]]
        _after_items += [(b["score"], b["buy_amount"]) for b in _buy_list]
        _after_total_val = sum(v for _, v in _after_items)
        _after_avg = sum(s * v for s, v in _after_items) / _after_total_val if _after_total_val > 0 else 50

        html += '<div style="border:2px solid #1976d2;border-radius:6px;padding:12px;margin:10px 0">'
        html += '<b style="color:#1976d2;font-size:14px">[SWAP] 调仓汇总</b>'
        html += '<table style="margin-top:8px;font-size:12px"><tr><th style="width:140px">项目</th><th>数值</th></tr>'
        html += f'<tr><td>卖出总额</td><td style="color:#cf1322;font-weight:bold">{_total_sell:,.0f}元</td></tr>'
        html += f'<tr><td>买入总额</td><td style="color:#389e0d;font-weight:bold">{_total_buy:,.0f}元</td></tr>'
        html += f'<tr><td>净资金变动</td><td style="font-weight:bold">{_net_change:+,.0f}元 {"(释放现金)" if _net_change > 0 else "(追加资金)"}</td></tr>'
        html += f'<tr><td>调仓前组合评分</td><td>{_before_avg:.1f}分</td></tr>'
        html += f'<tr><td>调仓后组合评分(预估)</td><td style="color:{"#389e0d" if _after_avg > _before_avg else "#cf1322"};font-weight:bold">{_after_avg:.1f}分 ({_after_avg - _before_avg:+.1f})</td></tr>'
        html += f'<tr><td>交易成本预估</td><td>{_trade_cost:,.0f}元 (印花税+佣金约0.15%)</td></tr>'
        html += f'<tr><td>风险提示</td><td style="font-size:11px;color:#ff9800">换股后若新标的次日低开>3%，立即止损；卖出挂单未成交则撤回，不追卖</td></tr>'
        html += '</table></div>'

        print(f"  [SWAP] 调仓信号触发: 卖{len(_sell_list)}只/买{len(_buy_list)}只 | 评分差{_score_gap:.0f}分 | 净额{_net_change:+,.0f}元")
    else:
        # 无调仓信号
        _max_gap = 0
        if _rebalance_holdings and _rebalance_candidates:
            _max_gap = _rebalance_candidates[0]["score"] - _rebalance_holdings[0]["score"]
        # 任务#1新增: 输出未触发原因（仅拼接现有变量文案，V3.2阈值与判定逻辑不变）
        _rb_reasons = []
        if not _rebalance_holdings:
            _rb_reasons.append("无有效技术评分的持仓")
        if not _rebalance_candidates:
            _rb_reasons.append("候选池无候选标的（观察池/推荐为空）")
        if _rebalance_holdings and _rebalance_candidates:
            _worst_h = _rebalance_holdings[0]
            _best_c = _rebalance_candidates[0]
            _gap_v = _best_c["score"] - _worst_h["score"]
            if _worst_h["score"] >= REBALANCE_SELL_THRESHOLD:
                _rb_reasons.append(f"最差持仓{_worst_h['name']}评分{_worst_h['score']:.0f} ≥ 卖出门槛{REBALANCE_SELL_THRESHOLD}")
            if _best_c["score"] <= REBALANCE_BUY_THRESHOLD:
                _rb_reasons.append(f"最优候选{_best_c['name']}评分{_best_c['score']:.0f} ≤ 买入门槛{REBALANCE_BUY_THRESHOLD}")
            if _gap_v < REBALANCE_SCORE_GAP:
                _rb_reasons.append(f"评分差{_gap_v:.0f}分 < 门槛{REBALANCE_SCORE_GAP}分")
        if _momentum_blocked:
            _rb_reasons.append("最优候选近5日涨幅>5%（追高过滤拦截）")
        _rb_reason_txt = "；".join(_rb_reasons) if _rb_reasons else "未满足调仓条件"
        html += f'<div class="alert alert-success">[OK] 当前持仓评分均衡，无需调仓（最大评分差{_max_gap:.0f}分 < {REBALANCE_SCORE_GAP}分门槛）<br><span style="font-size:11px;color:#666">未触发原因: {_rb_reason_txt}</span></div>'
        print(f"  [SWAP] 无调仓信号（最大评分差{_max_gap:.0f}<{REBALANCE_SCORE_GAP} | 原因: {_rb_reason_txt}）")
except Exception as _rb_e:
    html += f'<div class="alert alert-warning">[WARN] 调仓计划生成异常: {_rb_e}</div>'
    print(f"  [WARN] 调仓计划异常: {_rb_e}")

# ---- 系统胜率验证摘要 ----
verify_path = os.path.join(config.PROJECT_ROOT, 'output', 'win_rate_verification.json')
if os.path.exists(verify_path):
    with open(verify_path, 'r', encoding='utf-8') as f:
        vr = json.load(f)
    html += f"""
<h2>📊 系统胜率验证摘要</h2>
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

    # 观察池（增强版：含入选理由+关注价位）
    if watchlist:
        html += '<h3 style="font-size:14px;color:#ff9800;margin-top:15px">👀 观察池（未达买入标准，等待更好价格）</h3>'
        html += '<table><tr><th>代码</th><th>名称</th><th>赛道</th><th>综合评分</th><th>趋势状态</th><th>入选理由</th><th>关注价位区间</th><th>未达标原因</th></tr>'
        for w in watchlist[:12]:
            # 未通过原因
            watch_reason = w.get("watch_reason", "")
            if not watch_reason:
                if w["total_score"] < 55:
                    watch_reason = f"综合分{w['total_score']}不足55"
                elif w.get("plan") and w["plan"].get("risk_reward", 0) < 2.0:
                    watch_reason = f"盈亏比{w['plan']['risk_reward']:.1f}不足2.0"
                else:
                    watch_reason = f"趋势分{w['layers']['L4_趋势']['score']}不足40"
            # 入选理由（通过了哪几层筛选）
            reasons_list = w.get("reasons", [])
            if reasons_list:
                reason_text = "、".join(reasons_list[:2])
            else:
                # 根据层级得分生成理由
                passed_layers = []
                if w["layers"]["L2_赛道"]["score"] >= 50:
                    passed_layers.append("赛道达标")
                if w["layers"]["L3_基本面"]["score"] >= 40:
                    passed_layers.append("基本面OK")
                if w["layers"]["L4_趋势"]["score"] >= 40:
                    passed_layers.append("趋势向好")
                reason_text = "、".join(passed_layers) if passed_layers else "综合评分达标"
            # 关注价位区间
            plan = w.get("plan")
            if plan and plan.get("buy_low") and plan.get("buy_high"):
                price_range = f"{plan['buy_low']:.2f}~{plan['buy_high']:.2f}元"
            elif w.get("price", 0) > 0:
                # 没有plan时用当前价±5%作为参考区间
                ref_price = w["price"]
                price_range = f"{ref_price*0.95:.2f}~{ref_price*1.02:.2f}元(参考)"
            else:
                price_range = "—"
            # 趋势颜色
            trend_dir = w.get("trend_dir", "")
            trend_color = "#4caf50" if "强" in trend_dir or "上涨" in trend_dir else "#ff9800" if "震荡" in trend_dir else "#f44336"
            html += f'<tr><td>{w["code"]}</td><td><b>{w["name"]}</b></td><td>{w["sector"]}</td>'
            html += f'<td style="font-weight:bold">{w["total_score"]}</td>'
            html += f'<td style="color:{trend_color}">{trend_dir}</td>'
            html += f'<td style="font-size:11px;text-align:left">{reason_text}</td>'
            html += f'<td style="color:#1976d2;font-weight:bold">{price_range}</td>'
            html += f'<td style="font-size:11px;color:#999">{watch_reason}</td></tr>'
        html += '</table>'
        html += f'<div style="font-size:11px;color:#888;margin-top:4px">📌 观察池共{len(watchlist)}只，展示前{min(12, len(watchlist))}只 | “关注价位”为建议挂单区间，到达时可考虑建仓</div>'
else:
    html += '<div class="alert alert-warning">⛔ 当前无符合五层筛选标准的推荐标的。候选池均处于弱势或盈亏比不达标，建议空仓等待。</div>'

# ---- 操盘密码分析板块 V2.0 ----
html += '<h2>五、操盘密码分析 V2.0（自适应趋势+三重DK+多维资金+盈亏比门槛）</h2>'
html += '<div class="alert alert-info">🔑 超越付费软件 | 自适应生命线(EMA10+EMA30) + 三重共振DK + 5级趋势 + 盈亏比硬门槛 + 周线共振 + 震荡市自动屏蔽</div>'

try:
    caopan_engine = CaopanEngine()
    caopan_results = []
    for code, h in holdings.items():
        name = h["名称"]
        df_hist = hist_dataframes.get(code)
        if df_hist is not None and len(df_hist) >= 60:
            cr = caopan_engine.analyze(df_hist, code=code, name=name)
            if "error" not in cr:
                caopan_results.append(cr)

    if caopan_results:
        html += '<table><tr><th>标的</th><th>趋势(5级)</th><th>仓位指引</th><th>LL1/LL2</th><th>乖离率</th><th>DK信号</th><th>盈亏比</th><th>市场环境</th><th>操作建议</th></tr>'
        for cr in caopan_results:
            trend = cr.get('trend_desc', '')
            tl = cr.get('trend_level', 3)
            trend_color = {5:'#e53935',4:'#ff7043',3:'#ff9800',2:'#66bb6a',1:'#4caf50'}.get(tl, '#333')
            dk = cr.get('dk_signal') or '无'
            dk_grade = cr.get('dk_grade', '')
            dk_filtered = cr.get('dk_filtered', False)
            dk_color = '#e53935' if dk == 'D' and not dk_filtered else '#4caf50' if dk == 'K' and not dk_filtered else '#999'
            dk_text = f'{dk}({cr["dk_strength"]}分/{dk_grade})' + ('[过滤]' if dk_filtered else '')
            action = cr.get('action_suggestion', {})
            rr = cr.get('risk_reward', {})
            env = cr.get('market_env', {})
            dev_action = cr.get('deviation_action', '')
            html += f'<tr><td><b>{cr["name"]}</b>({cr["code"]})</td>'
            html += f'<td style="color:{trend_color};font-weight:bold">{trend}({tl}级)</td>'
            # P2优化: 趋势→仓位映射表
            pg = cr.get('position_guide', {})
            pg_text = f'≤{pg.get("max_position",40)}% {pg.get("action","")}'
            pg_color = '#e53935' if pg.get('max_position',40) >= 60 else '#ff9800' if pg.get('max_position',40) >= 30 else '#4caf50'
            html += f'<td style="color:{pg_color};font-size:11px">{pg_text}</td>'
            html += f'<td><span style="color:#f5a623">{cr["ll_fast"]:.2f}{cr["ll_fast_direction"]}</span>/<span style="color:#9c27b0">{cr["ll_slow"]:.2f}{cr["ll_slow_direction"]}</span></td>'
            html += f'<td>{cr["deviation_pct"]:.1f}% <span style="font-size:10px;color:#888">{dev_action}</span></td>'
            html += f'<td style="color:{dk_color};font-weight:bold">{dk_text}</td>'
            html += f'<td style="color:{"#4caf50" if rr.get("passed") else "#f44336"}">{rr.get("risk_reward_1",0):.1f}:1</td>'
            html += f'<td>{env.get("mode","")}</td>'
            html += f'<td>{action.get("desc", "观望")}</td></tr>'
        html += '</table>'

        # 趋势降级预警
        downgrades = [cr for cr in caopan_results if cr.get('trend_level', 3) <= 2]
        if downgrades:
            html += '<div class="alert alert-danger">🚨 趋势降级预警: ' + ', '.join([f'{cr["name"]}({cr["trend_desc"]})' for cr in downgrades]) + ' → 建议清仓/禁止加仓</div>'

        # 生成图表
        caopan_dir = os.path.join(config.PROJECT_ROOT, 'output', 'caopan')
        os.makedirs(caopan_dir, exist_ok=True)
        for cr in caopan_results:
            chart_path = os.path.join(caopan_dir, f'caopan_{cr["code"]}_{today.replace("-","")}.html')
            generate_caopan_chart(cr, output_path=chart_path)
        html += f'<div style="font-size:11px;color:#666;margin-top:5px">📊 详细图表: output/caopan/ 目录（K线+自适应生命线+三重DK标记+资金流+乖离率+市场环境）</div>'
    else:
        html += '<div class="alert alert-warning">数据不足，无法生成操盘密码分析</div>'
except Exception as e:
    html += f'<div class="alert alert-warning">操盘密码分析异常: {e}</div>'

# ---- 面板七：龙虎榜/游资动向 ----
html += '<h2>七、龙虎榜/游资动向</h2>'
if HAS_LHB and lhb_results:
    html += '<table><tr><th>股票</th><th>上榜次数</th><th>机构趋势</th><th>游资活跃度</th><th>信号</th></tr>'
    for code_h, h in holdings.items():
        lhb_r = lhb_results.get(code_h)
        if not lhb_r:
            continue
        sig = lhb_r.get('signal', 'neutral')
        sig_class = {'bullish': 'signal-bullish', 'bearish': 'signal-bearish'}.get(sig, 'signal-neutral')
        sig_text = {'bullish': '看多', 'bearish': '看空', 'neutral': '中性'}.get(sig, sig)
        trend_map = {'increasing': '↑ 上升', 'decreasing': '↓ 下降', 'neutral': '→ 平稳'}
        trend_text = trend_map.get(lhb_r.get('institution_trend', 'neutral'), lhb_r.get('institution_trend', ''))
        hot_score = lhb_r.get('hot_money_score', 0)
        html += f'<tr><td><b>{h["名称"]}</b>({code_h})</td>'
        html += f'<td>{lhb_r.get("lhb_count", 0)}</td>'
        html += f'<td>{trend_text}</td>'
        html += f'<td>{hot_score:.0f}分</td>'
        html += f'<td><span class="signal-tag {sig_class}">{sig_text}</span></td></tr>'
    html += '</table>'
    # 大白话解读
    html += '''<div class="explain-card">
<span class="ec-title">💬 大白话解读：龙虎榜是什么？</span>
<b>龙虎榜</b>就像股市的"大额交易公示栏"——当一只股票当天涨跌幅、换手率或振幅达到交易所规定的门槛时，系统会自动公布当天买卖金额最大的前5个席位（营业部）。<br>
<b>游资介入意味着什么？</b>游资（短线大资金）像"快进快出的猎手"，他们选中一只股票通常意味着短期有题材催化，但来得快去得也快。如果龙虎榜上出现知名游资席位（如"华鑫上海分公司"等），说明短线资金关注度高。<br>
<b>对散户的实操建议：</b>① 机构席位（"机构专用"）连续买入 → 中线看好，可跟随；② 纯游资主导且上榜频繁 → 说明筹码在快速换手，追高容易被套；③ 如果报告提示"游资主导+上榜频繁"，已持仓的设好止损，未持仓的不要追。
</div>'''
    # 风险预警
    risk_stocks = [code_h for code_h, r in lhb_results.items() if r.get('risk_warning')]
    if risk_stocks:
        names = [holdings[c]["名称"] for c in risk_stocks if c in holdings]
        html += f'<div class="alert alert-danger">🚨 龙虎榜风险预警: {", ".join(names)} 游资主导且上榜频繁，短线风险较高</div>'
else:
    html += '<div class="alert alert-warning">龙虎榜数据暂不可用</div>'

# ---- 面板八：融资融券信号 ----
html += '<h2>八、融资融券信号</h2>'
if HAS_MARGIN and margin_results:
    html += '<table><tr><th>股票</th><th>融资净买入天数</th><th>余额趋势</th><th>拐点</th><th>融券异常</th><th>背离/预警</th><th>信号</th></tr>'
    for code_h, h in holdings.items():
        mr = margin_results.get(code_h)
        if not mr:
            continue
        sig = mr.get('signal', 'neutral')
        sig_class = {'bullish': 'signal-bullish', 'bearish': 'signal-bearish', 'cautious': 'signal-bearish'}.get(sig, 'signal-neutral')
        sig_text = {'bullish': '看多', 'bearish': '看空', 'cautious': '谨慎', 'neutral': '中性'}.get(sig, sig)
        trend_map = {'increasing': '↑ 上升', 'decreasing': '↓ 下降', 'neutral': '→ 平稳'}
        trend_text = trend_map.get(mr.get('balance_trend', 'neutral'), mr.get('balance_trend', ''))
        turning = '✅ 是' if mr.get('balance_turning') else '—'
        short_anomaly = '⚠️ 异常' if mr.get('short_selling_anomaly') else '—'
        # P2优化: 背离/急降预警列
        div_type = mr.get('price_margin_divergence', 'none')
        drop_alert = mr.get('balance_drop_alert', False)
        alert_parts = []
        if div_type == 'top':
            alert_parts.append('⚠️顶背离')
        elif div_type == 'bottom':
            alert_parts.append('💡底背离')
        if drop_alert:
            alert_parts.append('⚠️余额急降')
        alert_text = ' '.join(alert_parts) if alert_parts else '—'
        alert_color = '#e53935' if '顶背离' in alert_text or '急降' in alert_text else '#4caf50' if '底背离' in alert_text else '#999'
        html += f'<tr><td><b>{h["名称"]}</b>({code_h})</td>'
        html += f'<td>{mr.get("net_buy_days", 0)}天</td>'
        html += f'<td>{trend_text}</td>'
        html += f'<td style="color:#4caf50">{turning}</td>'
        html += f'<td style="color:#e74c3c">{short_anomaly}</td>'
        html += f'<td style="color:{alert_color};font-size:11px">{alert_text}</td>'
        html += f'<td><span class="signal-tag {sig_class}">{sig_text}</span></td></tr>'
    html += '</table>'
    # 大白话解读
    html += '''<div class="explain-card">
<span class="ec-title">💬 大白话解读：融资融券怎么看？</span>
<b>融资</b>就是投资者借钱买股票（看多），<b>融券</b>就是借股票来卖（看空）。你可以把它理解为"聪明钱的投票器"。<br>
<b>融资余额上升</b> → 越来越多人借钱买入，说明市场对该股后市乐观（相当于"加杠杆做多"）；<b>融资余额下降</b> → 借钱的人在撤退，信心不足。<br>
<b>融券异常</b> → 突然有大量资金借股票来砸盘，可能是内部人/机构提前得到利空消息在做空，需要高度警惕。<br>
<b>实操建议：</b>① 融资连续5天以上净买入 + 余额趋势上升 → 正面信号，可持有；② 余额出现"拐点"（由升转降）→ 注意减仓；③ 融券异常 → 短期内不要加仓，等消息面明朗。
</div>'''
else:
    html += '<div class="alert alert-warning">融资融券数据暂不可用</div>'

# ---- 面板九：解禁风险预警 ----
html += '<h2>九、解禁风险预警</h2>'
if HAS_CALENDAR and release_risks:
    has_any_release = any(r.get('has_release') for r in release_risks.values())
    high_impact_stocks = [code_h for code_h, r in release_risks.items() if r.get('max_impact') == 'high']
    if high_impact_stocks:
        names = [holdings[c]["名称"] for c in high_impact_stocks if c in holdings]
        html += f'<div class="alert alert-danger">🚨 高冲击解禁预警: {", ".join(names)}，建议回避新开仓</div>'
    if has_any_release:
        html += '<table><tr><th>股票</th><th>解禁日期</th><th>冲击等级</th><th>距今天数</th></tr>'
        for code_h, h in holdings.items():
            rr = release_risks.get(code_h)
            if not rr or not rr.get('has_release'):
                continue
            for evt in rr.get('events', []):
                impact = evt.get('impact_level', 'low')
                impact_color = {'high': '#e74c3c', 'medium': '#ff9800', 'low': '#4caf50'}.get(impact, '#999')
                impact_text = {'high': '高冲击', 'medium': '中冲击', 'low': '低冲击'}.get(impact, impact)
                html += f'<tr><td><b>{h["名称"]}</b>({code_h})</td>'
                html += f'<td>{evt.get("release_date", "")}</td>'
                html += f'<td style="color:{impact_color};font-weight:bold">{impact_text}</td>'
                html += f'<td>{evt.get("days_until_release", "")}天</td></tr>'
        html += '</table>'
    else:
        html += '<div class="alert alert-success">✅ 持仓近90天内无解禁风险</div>'
    # 大白话解读
    html += '''<div class="explain-card">
<span class="ec-title">💬 大白话解读：解禁是什么意思？</span>
<b>解禁</b>就像"限售期到期，锁着的股票终于可以卖了"。公司上市时，大股东、机构持有的股票有锁定期（通常1-3年），锁定期一过，这些股票就能自由卖出。<br>
<b>为什么解禁前后股价会波动？</b>因为解禁意味着市场上突然多了大量"可以卖的筹码"。如果持有人选择集中抛售，供大于求，股价就会承压。就像一条路突然多了很多车，自然会堵车。<br>
<b>多少比例需要警惕？</b>① 解禁量占流通股 <b>&lt;5%</b> → 影响较小，不必过度担心；② <b>5%-15%</b> → 中等冲击，解禁前1周注意观察；③ <b>&gt;15%</b> → 高冲击，建议解禁前后2周内不要新开仓，已持仓的设好止损。<br>
<b>实操建议：</b>报告中标红"高冲击"的标的，短期内回避加仓；如果解禁后股价不跌反涨，说明接盘力量强，反而是好信号。
</div>'''
    # 市场解禁摘要
    if release_summary and release_summary.get('total_stocks', 0) > 0:
        html += f'<div style="font-size:11px;color:#666;margin-top:5px">📅 市场解禁: 未来30天共{release_summary.get("total_stocks", 0)}只标的解禁'
        if release_summary.get('peak_week'):
            html += f' | 高峰: {release_summary["peak_week"]}'
        html += '</div>'
else:
    html += '<div class="alert alert-warning">解禁风险数据暂不可用</div>'

# ---- 面板十：四级资金流向 ----
html += '<h2>十、四级资金流向</h2>'
if HAS_CAPITAL_FLOW and flow_results:
    for code_h, h in holdings.items():
        fr = flow_results.get(code_h)
        if not fr or not fr.get('success'):
            continue
        pr = pattern_results.get(code_h, {})
        pattern = pr.get('pattern', 'neutral')
        pattern_class = {'accumulation': 'pattern-accumulation', 'distribution': 'pattern-distribution', 'washout': 'pattern-washout'}.get(pattern, '')
        pattern_text = {'accumulation': '吸筹', 'distribution': '出货', 'washout': '洗盘', 'neutral': '无明显模式'}.get(pattern, pattern)
        direction = fr.get('main_force_direction', 'neutral')
        dir_map = {'buying': '🔴 主力买入', 'selling': '🟢 主力卖出', 'neutral': '→ 中性'}
        dir_text = dir_map.get(direction, direction)

        html += f'<div class="stock-card"><h3>{h["名称"]}({code_h}) <span style="font-size:12px;color:#888">{dir_text}</span>'
        if pattern_class:
            html += f' <span class="pattern-tag {pattern_class}">{pattern_text}</span>'
        elif pattern == 'neutral':
            html += f' <span class="pattern-tag" style="background:#95a5a6;color:white">{pattern_text}</span>'
        html += '</h3>'

        # 四级资金流柱状展示
        levels = [
            ('超大单', 'super_large', '#e74c3c'),
            ('大单', 'large', '#ff7043'),
            ('中单', 'medium', '#42a5f5'),
            ('小单', 'small', '#66bb6a'),
        ]
        html += '<table><tr><th>级别</th><th>净流入(万)</th><th>趋势</th><th>图示</th></tr>'
        for label, key, color in levels:
            level_data = fr.get(key, {})
            net = level_data.get('net_inflow', 0)
            trend = level_data.get('trend', 'neutral')
            trend_text_map = {'increasing': '↑', 'decreasing': '↓', 'net_inflow': '净流入', 'net_outflow': '净流出', 'neutral': '→'}
            trend_t = trend_text_map.get(trend, trend)
            bar_width = min(abs(net) / 1e6 * 5, 100) if net != 0 else 0
            bar_class = 'flow-positive' if net >= 0 else 'flow-negative'
            net_color = '#e74c3c' if net >= 0 else '#27ae60'
            html += f'<tr><td style="color:{color};font-weight:bold">{label}</td>'
            html += f'<td style="color:{net_color}">{net/10000:+,.0f}万</td>'
            html += f'<td>{trend_t}</td>'
            html += f'<td><div class="flow-bar {bar_class}" style="width:{bar_width}px"></div></td></tr>'
        html += '</table>'

        # 模式描述
        if pr.get('description') and pattern != 'neutral':
            html += f'<div style="font-size:11px;color:#666;margin-top:5px">💡 {pr["description"]}</div>'
        # P2优化: 价量背离提示（使用合并调用的预计算结果，无额外API请求）
        divergence = divergence_results.get(code_h, {})
        if divergence.get('type', 'none') != 'none':
            div_color = '#e53935' if divergence['type'] == 'top' else '#4caf50'
            div_icon = '⚠️' if divergence['type'] == 'top' else '💡'
            html += f'<div style="font-size:11px;color:{div_color};margin-top:3px">{div_icon} {divergence["desc"]}(置信度{divergence["confidence"]:.0%})</div>'
        html += '</div>'
else:
    html += '<div class="alert alert-warning">四级资金流数据暂不可用</div>'

# 四级资金流大白话解读（无论数据是否可用都展示）
html += '''<div class="explain-card">
<span class="ec-title">💬 大白话解读：四种资金模式分别意味着什么？</span>
<b>四级资金流</b>把市场上的资金按"体量"分成四档：超大单（机构/游资大佬）、大单（大户）、中单（中产散户）、小单（小散户）。通过观察谁在买、谁在卖，判断主力意图。<br>
<b>🟢 温和建仓（吸筹）：</b>超大单/大单在悄悄买入，但量不大、不拉涨停——像"大鳄在水下慢慢吃货"。这意味着主力看好后市，正在低位收集筹码。<b>操作：跟随持有，等拉升。</b><br>
<b>🔴 放量拉升：</b>大单突然集中涌入，成交量暴增，股价快速上涨——主力开始"发令枪响了"。<b>操作：已持仓的拿住，未入场的等回踩再进，不追高。</b><br>
<b>⚠️ 对倒骗线（假突破）：</b>表面上大单在买，但中单/小单在疯狂卖，且买卖金额异常接近——这是主力"左手倒右手"制造放量假象，引诱散户接盘。<b>操作：看到这种模式坚决不追，已持仓的逢高减仓。</b><br>
<b>⚪ 正常/无明显模式：</b>各档资金没有明显一致性方向，市场处于观望状态。<b>操作：按兵不动，等待方向选择。</b>
</div>'''

# ---- 📈 持仓未来趋势预判 ----
html += '<h2>📈 持仓未来趋势预判</h2>'

# 系统性风险提示
if regime_result and regime_result["state"] == "BEAR":
    html += '<div style="background:#fffbe6;border:1px solid #ffe58f;border-radius:6px;padding:10px 14px;margin:8px 0;color:#ad6800;font-weight:bold">⚠️ 大盘弱势，个股判断可靠性降低，建议统一降低仓位、严格止损</div>'

if forecast_results:
    html += '''<table style="width:100%;border-collapse:collapse;font-size:12px;margin:8px 0">
<tr style="background:#e6f7ff"><th style="padding:8px;border:1px solid #91d5ff">代码/名称</th><th style="padding:8px;border:1px solid #91d5ff">方向</th><th style="padding:8px;border:1px solid #91d5ff">置信度</th><th style="padding:8px;border:1px solid #91d5ff">评分</th><th style="padding:8px;border:1px solid #91d5ff">支撑/压力</th><th style="padding:8px;border:1px solid #91d5ff">操作建议</th><th style="padding:8px;border:1px solid #91d5ff">节奏</th></tr>'''
    for _code, _fc in forecast_results.items():
        _name = _fc.get("name", _code)
        _composite = _fc.get("composite", {})
        _score = _composite.get("total_score", 50)
        _trend = _fc.get("trend", {})
        _direction = _trend.get("direction", "震荡")
        _dir_color = {"上涨": "#ff4d4f", "下跌": "#52c41a", "震荡": "#faad14"}.get(_direction, "#999")
        _advice = _fc.get("advice", {})
        _confidence = _advice.get("confidence_pct", 50)
        _action = _advice.get("action", "观望")
        _detail = _advice.get("detail", "")
        _levels = _fc.get("levels", {})
        _support = _levels.get("first_support", 0)
        _resistance = _levels.get("first_resistance", 0)
        _timing = _advice.get("timing", {})
        _timing_desc = _timing.get("period", "") if isinstance(_timing, dict) else ""
        _score_color = "#ff4d4f" if _score >= 60 else ("#52c41a" if _score < 40 else "#faad14")
        html += f'''<tr><td style="padding:6px;border:1px solid #f0f0f0">{_code} {_name}</td>
<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;color:{_dir_color};font-weight:bold">{_direction}</td>
<td style="padding:6px;border:1px solid #f0f0f0;text-align:center">{_confidence}%</td>
<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;color:{_score_color};font-weight:bold">{_score}</td>
<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;font-size:11px">{_support:.2f} / {_resistance:.2f}</td>
<td style="padding:6px;border:1px solid #f0f0f0"><b>{_action}</b><br><span style="color:#888;font-size:11px">{_detail}</span></td>
<td style="padding:6px;border:1px solid #f0f0f0;font-size:11px">{_timing_desc}</td></tr>'''
    html += '</table>'
else:
    html += '<div style="background:#f5f5f5;border-radius:6px;padding:12px;margin:10px 0;color:#999">趋势预测数据暂不可用</div>'

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

subject = f"[综合分析报告] {len(holdings)}只标的 技术面+条件单 | {today} {now[:5]}"
print(f"[发送] {subject}")
result = send_email(subject, html)
print(f"[结果] {'✅ 发送成功' if result else '❌ 发送失败'}")
