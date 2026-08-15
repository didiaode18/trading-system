# -*- coding: utf-8 -*-
"""
三层分时报告工具 V1.1 (CLI手动模式)
========================
统一入口，提供盘前/盘后/周报三层报告的手动生成能力。

❗ 重要说明 (V1.1):
  本文件为CLI手动报告生成工具，不包含定时调度功能。
  定时任务已统一由 trading_system/scheduler.py 负责。
  scheduler.py 的竞价选股(09:25)已复用本文件的 run_canslim() 函数。

三层架构:
  第一层 [盘前] 盘前快速决策 — 仅展示需要动作和有变化的信息（≤3屏）
  第二层 [盘后] 盘后深度复盘 — 全量分析+条件单+资金面+大白话解读
  第三层 [周报] 周策略报告 — 绩效+轮动+仓位再平衡+下周计划

独立保留（不纳入本工具）:
  - CANSLIM选股报告（09:25，由scheduler.py独立触发）
  - caopan_realtime_report.py 盘中实时图表
  - 盘中紧急预警（止损/跌停触发）
  - strategy_analysis_report.py 回测验证（手动触发）

运行(CLI手动模式):
  python report_dispatcher.py --morning    # 第一层：盘前
  python report_dispatcher.py --evening    # 第二层：盘后
  python report_dispatcher.py --weekly     # 第三层：周报
  python report_dispatcher.py --canslim    # CANSLIM选股
  python report_dispatcher.py --all        # 全部（测试用）

数据完整性约束:
  重构前后所有用户可见数据字段零丢失。宁可保留冗余代码，不可丢失任何指标。
"""

import sys
import os
import io
import json
import datetime
import warnings
warnings.filterwarnings('ignore')

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADING_SYSTEM_DIR = os.path.join(BASE_DIR, "trading_system")
sys.path.insert(0, TRADING_SYSTEM_DIR)
sys.path.insert(0, BASE_DIR)

import numpy as np
import pandas as pd
import config
from notify.email_notify import send_email
from data.realtime import fetch_realtime_batch
from data.data_loader import fetch_stock_daily_baostock, _bs_logout

# FIX: 模块级today/now/weekday_cn原在导入时固化，长驻进程跨天取值会过期，
# 改为函数按需取当前值，各run_*入口处一次性取值，保证同一报告内取值一致
def _today():
    return datetime.date.today().strftime("%Y-%m-%d")


def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _weekday_cn():
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.date.today().weekday()]


# FIX: ETF前缀口径复用 capital_planner._ETF_PREFIXES，导入失败时兜底同值常量
try:
    from strategy.capital_planner import _ETF_PREFIXES
except Exception:
    _ETF_PREFIXES = ("159", "510", "511", "512", "513", "515", "516", "518", "560", "562", "588")


# ============================================================
# 共享数据层（一次获取，三层复用，避免重复API调用）
# ============================================================

def load_holdings() -> list:
    """加载持仓列表（路径统一委托 config）"""
    hjson_path = config.get_holdings_file()
    if os.path.exists(hjson_path):
        with open(hjson_path, 'r', encoding='utf-8') as f:
            hjson = json.load(f)
        # FIX: 仅保留shares>0的真实持仓（口径同generate_holdings_report.py的
        # _load_holdings_from_json），已清仓标的不参与"已持仓排除"与报告
        return [{"code": k, **v} for k, v in hjson.items()
                if isinstance(v, dict) and int(v.get("shares", 0) or 0) > 0]
    # 降级：从config读取
    return [{"code": k, "名称": v.get("名称", k), "赛道": v.get("赛道", "")}
            for k, v in config.STOCK_POOL.items()]


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算技术指标（复用generate_holdings_report.py逻辑）"""
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    # MACD
    ema12 = df["close"].ewm(span=12).mean()
    ema26 = df["close"].ewm(span=26).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9).mean()
    df["macd"] = (df["dif"] - df["dea"]) * 2
    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    # ATR
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    # 布林带
    df["boll_mid"] = df["close"].rolling(20).mean()
    df["boll_std"] = df["close"].rolling(20).std()
    df["boll_upper"] = df["boll_mid"] + 2 * df["boll_std"]
    df["boll_lower"] = df["boll_mid"] - 2 * df["boll_std"]
    # 量能
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    return df


def calc_technical_score(df: pd.DataFrame) -> dict:
    """计算综合技术评分（0-100）+ 趋势方向 + 支撑压力位"""
    if df is None or len(df) < 60:
        return {"valid": False, "error": "数据不足"}

    latest = df.iloc[-1]
    close = latest["close"]
    score = 50  # 基准分

    # 均线系统（±20分）
    ma5, ma10, ma20, ma60 = latest["ma5"], latest["ma10"], latest["ma20"], latest["ma60"]
    if ma5 > ma10 > ma20 > ma60:
        score += 20
        trend_dir = "📈 强势上涨"
    elif ma5 > ma20:
        score += 10
        trend_dir = "↗️ 偏多震荡"
    elif ma5 < ma10 < ma20 < ma60:
        score -= 20
        trend_dir = "📉 弱势下跌"
    elif ma5 < ma20:
        score -= 10
        trend_dir = "↘️ 偏空震荡"
    else:
        trend_dir = "→ 横盘整理"

    # MACD（±10分）
    if latest["dif"] > latest["dea"]:
        score += 10
    else:
        score -= 10

    # RSI（±10分）
    rsi = latest["rsi"]
    if rsi > 70:
        score -= 5  # 超买
    elif rsi < 30:
        score += 5  # 超卖反弹机会
    elif 40 <= rsi <= 60:
        score += 5

    # 量能（±10分）
    vol_ratio = latest["volume"] / latest["vol_ma20"] if latest["vol_ma20"] > 0 else 1
    if vol_ratio > 1.5 and close > latest["ma20"]:
        score += 10
    elif vol_ratio < 0.5:
        score -= 5

    # 动量（±10分）
    if len(df) >= 6:
        momentum_5d = (close / df["close"].iloc[-6] - 1) * 100
        if momentum_5d > 5:
            score += 10
        elif momentum_5d < -5:
            score -= 10
    else:
        momentum_5d = 0

    score = max(0, min(100, score))

    # 支撑/压力位
    supports = []
    resistances = []
    if not np.isnan(ma20):
        supports.append(("MA20", round(ma20, 2)))
    if not np.isnan(ma60):
        supports.append(("MA60", round(ma60, 2)))
    boll_lower = latest.get("boll_lower", 0)
    if not np.isnan(boll_lower) and boll_lower > 0:
        supports.append(("布林下轨", round(boll_lower, 2)))
    boll_upper = latest.get("boll_upper", 0)
    if not np.isnan(boll_upper) and boll_upper > 0:
        resistances.append(("布林上轨", round(boll_upper, 2)))
    recent_high = df["high"].iloc[-20:].max()
    resistances.append(("20日高点", round(recent_high, 2)))

    # 持有建议
    if score >= 70:
        hold_suggest = "5-10天"
        hold_reason = "趋势强势，持有待涨"
    elif score >= 50:
        hold_suggest = "3-5天"
        hold_reason = "震荡偏多，关注突破"
    elif score >= 35:
        hold_suggest = "1-3天"
        hold_reason = "偏弱震荡，设好止损"
    else:
        hold_suggest = "尽快离场"
        hold_reason = "趋势破位，止损优先"

    return {
        "valid": True,
        "composite": round(score, 1),
        "trend_dir": trend_dir,
        "rsi": round(rsi, 1),
        "macd_dif": round(latest["dif"], 3),
        "macd_dea": round(latest["dea"], 3),
        "ma5": round(ma5, 2), "ma10": round(ma10, 2),
        "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "atr": round(latest["atr"], 3),
        "momentum_5d": round(momentum_5d, 2),
        "boll_upper": round(boll_upper, 2),
        "boll_lower": round(boll_lower, 2),
        "vol_signal": f"量比{vol_ratio:.1f}",
        "supports": supports[:3],
        "resistances": resistances[:3],
        "hold_suggest": hold_suggest,
        "hold_reason": hold_reason,
    }


def fetch_shared_data(holdings_list: list) -> dict:
    """
    共享数据获取：行情+K线+技术指标+操盘密码
    返回: {code: {quote, df, tech, caopan}} 的字典
    """
    codes = [h["code"] for h in holdings_list]
    print(f"[数据] 获取{len(codes)}只标的行情...")
    quotes = fetch_realtime_batch(codes)
    print(f"[数据] 行情: {len(quotes)}/{len(codes)}只成功")

    print(f"[数据] 获取历史K线...")
    hist_dataframes = {}
    start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m-%d")
    for code in codes:
        try:
            df = fetch_stock_daily_baostock(code, start_date=start)
            if df is not None and not df.empty and len(df) >= 30:
                hist_dataframes[code] = compute_indicators(df)
        except Exception:
            pass
    _bs_logout()
    print(f"[数据] K线: {len(hist_dataframes)}/{len(codes)}只成功")

    # 操盘密码分析
    from strategy.caopan_signal import CaopanEngine
    caopan_engine = CaopanEngine()
    caopan_results = {}
    for code in codes:
        df = hist_dataframes.get(code)
        if df is not None and len(df) >= 60:
            try:
                name = next((h.get("名称", h.get("name", code)) for h in holdings_list if h["code"] == code), code)
                cr = caopan_engine.analyze(df, code=code, name=name)
                if "error" not in cr:
                    caopan_results[code] = cr
            except Exception:
                pass
    print(f"[数据] 操盘密码: {len(caopan_results)}/{len(codes)}只成功")

    # 组装结果
    shared = {}
    for h in holdings_list:
        code = h["code"]
        quote = quotes.get(code, {})
        df = hist_dataframes.get(code)
        tech = calc_technical_score(df) if df is not None else {"valid": False}
        shared[code] = {
            "info": h,
            "quote": quote,
            "df": df,
            "tech": tech,
            "caopan": caopan_results.get(code),
            "price": quote.get("price", 0),
            "change_pct": quote.get("change_pct", 0),
            "name": h.get("名称", h.get("name", code)),
        }
    return shared


# ============================================================
# HTML公共样式
# ============================================================

COMMON_CSS = """
body{font-family:'Microsoft YaHei',sans-serif;padding:15px;background:#f0f2f5;font-size:13px}
.container{max-width:1000px;margin:0 auto}
.header{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:20px 25px;border-radius:10px 10px 0 0}
.header h1{margin:0;font-size:20px}
.header .sub{font-size:12px;opacity:.8;margin-top:5px}
.content{background:#fff;padding:20px 25px;border-radius:0 0 10px 10px;box-shadow:0 2px 10px rgba(0,0,0,.1)}
h2{color:#2c3e50;font-size:16px;margin-top:25px;border-left:4px solid #3498db;padding-left:10px}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:12px}
th{background:#34495e;color:#fff;padding:8px 6px;text-align:center}
td{padding:7px 6px;border-bottom:1px solid #eee;text-align:center}
.alert{padding:12px;border-radius:6px;margin:10px 0;font-size:12px}
.alert-danger{background:#ffebee;border-left:4px solid #e74c3c}
.alert-success{background:#e8f5e9;border-left:4px solid #4caf50}
.alert-warning{background:#fff3cd;border-left:4px solid #ffc107}
.alert-info{background:#e3f2fd;border-left:4px solid #2196f3}
.up{color:#e74c3c} .down{color:#27ae60}
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold;color:#fff;margin:2px}
.tag-hold{background:#2196f3} .tag-reduce{background:#ff9800} .tag-stop{background:#f44336} .tag-add{background:#4caf50}
.explain-card{background:#f5f5f5;border-radius:8px;padding:14px 16px;margin:12px 0;font-size:12px;color:#555;line-height:1.9;border:1px solid #e8e8e8}
.explain-card .ec-title{font-weight:bold;color:#333;font-size:13px;margin-bottom:6px;display:block}
.explain-card b{color:#333}
.stock-card{border:1px solid #e0e0e0;border-radius:8px;margin:15px 0;padding:15px;page-break-inside:avoid}
.stock-card h3{margin:0 0 10px;font-size:15px;border-bottom:2px solid #3498db;padding-bottom:6px}
.signal-tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:bold}
.signal-bullish{background:#27ae60;color:white}
.signal-bearish{background:#e74c3c;color:white}
.signal-neutral{background:#95a5a6;color:white}
.pattern-tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}
.pattern-accumulation{background:#27ae60;color:white}
.pattern-distribution{background:#e74c3c;color:white}
.pattern-washout{background:#f39c12;color:white}
.footer{text-align:center;color:#999;font-size:11px;margin-top:15px;padding-top:10px;border-top:1px solid #eee}
.mode-badge{display:inline-block;background:#9c27b0;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;margin-left:8px}
"""


# ============================================================
# 第一层：盘前快速决策报告（08:30）
# ============================================================

def run_morning():
    """
    第一层：盘前快速决策报告
    设计原则：只展示"需要动作"和"有变化"的信息，无变化不显示
    目标：≤3屏，3分钟可浏览

    数据完整性清单:
    - 持仓技术评分 ✓
    - DK信号（含强度/等级/是否过滤）✓
    - 操作优先级排序 ✓
    - 支撑/压力价位 ✓
    - 选股观察池明细（代码+名称+入选理由+评分+趋势+关注价位区间）✓
    """
    print("\n" + "=" * 60)
    print("  第一层：盘前快速决策报告")
    print("=" * 60)

    # FIX: 时间变量改为入口处按需取值（原模块级变量在导入时固化）
    today, now, weekday_cn = _today(), _now(), _weekday_cn()

    holdings_list = load_holdings()
    shared = fetch_shared_data(holdings_list)

    # 五层选股（观察池）
    print("[选股] 运行五层选股引擎...")
    watchlist = []
    try:
        from strategy.recommend_engine import run_recommendation
        held_codes = set(h["code"] for h in holdings_list)
        candidate_stocks = []
        for sector_name, sector_info in config.SECTOR_CANDIDATES.items():
            for code_c, info_c in sector_info.get("stocks", {}).items():
                if code_c not in held_codes and not code_c.startswith("300") and not code_c.startswith("688"):
                    candidate_stocks.append({
                        "code": code_c, "name": info_c.get("名称", code_c),
                        "sector": info_c.get("细分", sector_name), "type": info_c.get("类型", "龙头"),
                    })
        cand_quotes = fetch_realtime_batch([c["code"] for c in candidate_stocks])
        candidate_data = []
        start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m-%d")
        for item in candidate_stocks:
            try:
                df = fetch_stock_daily_baostock(item["code"], start_date=start)
                if df is not None and not df.empty and len(df) >= 30:
                    df = compute_indicators(df)
                    rt_price = cand_quotes.get(item["code"], {}).get("price", 0)
                    rt_change = cand_quotes.get(item["code"], {}).get("change_pct", 0)
                    candidate_data.append({**item, "df": df, "realtime_price": rt_price, "realtime_change": rt_change})
            except Exception:
                pass
        _bs_logout()
        if candidate_data:
            rec_result = run_recommendation(candidate_data, top_n=3)
            watchlist = rec_result.get("watchlist", [])
            recommendations = rec_result.get("recommended", [])
            print(f"[选股] 推荐{len(recommendations)}只 | 观察{len(watchlist)}只")
    except Exception as e:
        print(f"[选股] 异常: {e}")
        recommendations = []

    # === 构建HTML ===
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{COMMON_CSS}</style></head><body>
<div class="container">
<div class="header">
<h1>📋 盘前操作清单 <span class="mode-badge">快速决策</span></h1>
<div class="sub">{today} {weekday_cn} | 目标：3分钟读完 → 知道今天该干什么</div>
</div>
<div class="content">
"""

    # ① 大盘环境（2行）
    html += '<h2>① 大盘环境</h2>'
    # 简单判断：基于持仓整体涨跌
    # FIX: 所有change_pct=0时列表为空，np.mean([])返回NaN，增加空列表防护
    _changes_list = [s["change_pct"] for s in shared.values() if s["change_pct"] != 0]
    avg_change = np.mean(_changes_list) if _changes_list else 0
    market_desc = "偏多" if avg_change > 0.5 else "偏空" if avg_change < -0.5 else "震荡"
    html += f'<div class="alert alert-info">持仓平均涨跌: <b>{avg_change:+.2f}%</b> | 市场状态: <b>{market_desc}</b> | 建议仓位: {"70%" if market_desc == "偏多" else "50%" if market_desc == "震荡" else "30%"}</div>'

    # ② 今日必须操作（仅有动作的标的）
    html += '<h2>② 今日必须操作</h2>'
    # FIX: 统一使用config.INITIAL_STOP_LOSS_PCT(10%)，与generate_holdings_report.py一致
    STOP_LOSS_PCT = config.INITIAL_STOP_LOSS_PCT
    action_items = []
    for code, s in shared.items():
        price = s["price"]
        if price <= 0:
            continue
        stop_price = round(price * (1 - STOP_LOSS_PCT), 2)
        tech = s["tech"]
        caopan = s["caopan"]
        actions = []
        # 止损触发检查
        if tech.get("valid") and tech.get("composite", 50) < 30:
            actions.append(("🔴 止损预警", f"评分{tech['composite']}，趋势破位，止损价{stop_price}"))
        # DK信号
        if caopan:
            dk = caopan.get("dk_signal")
            dk_filtered = caopan.get("dk_filtered", False)
            dk_grade = caopan.get("dk_grade", "")
            dk_strength = caopan.get("dk_strength", 0)
            if dk == "K" and not dk_filtered:
                actions.append(("🟡 K点空头", f"强度{dk_strength}分/{dk_grade}，注意减仓"))
            elif dk == "D" and not dk_filtered and dk_grade in ("strong", "medium"):
                actions.append(("🟢 D点多头", f"强度{dk_strength}分/{dk_grade}，可加仓"))
            # 趋势降级
            trend_level = caopan.get("trend_level", 3)
            if trend_level <= 2:
                actions.append(("🚨 趋势降级", f"{caopan.get('trend_desc', '')}({trend_level}级)，禁止加仓"))
        if actions:
            action_items.append((code, s["name"], price, stop_price, actions, tech))

    if action_items:
        # 按紧急程度排序（止损>趋势降级>K点>D点）
        # FIX: 原用emoji字符串Unicode排序（脆弱），改为数字优先级映射
        _urgency_map = {"🔴": 4, "🚨": 3, "🟡": 2, "🟢": 1}
        action_items.sort(key=lambda x: max(_urgency_map.get(a[0][:2], 0) for a in x[4]), reverse=True)
        html += '<table><tr><th>标的</th><th>最新价</th><th>止损价</th><th>操作信号</th><th>评分</th><th>支撑位</th><th>压力位</th></tr>'
        for code, name, price, stop_price, actions, tech in action_items:
            signals_html = "<br>".join([f"{icon} {desc}" for icon, desc in actions])
            comp = tech.get("composite", "-") if tech.get("valid") else "-"
            supports = tech.get("supports", []) if tech.get("valid") else []
            resistances = tech.get("resistances", []) if tech.get("valid") else []
            sup_str = " | ".join([f"{n}:{v}" for n, v in supports[:2]]) if supports else "—"
            res_str = " | ".join([f"{n}:{v}" for n, v in resistances[:2]]) if resistances else "—"
            html += f'<tr><td><b>{name}</b>({code})</td><td>{price:.2f}</td>'
            html += f'<td style="color:#e74c3c;font-weight:bold">{stop_price}</td>'
            html += f'<td style="text-align:left;font-size:11px">{signals_html}</td>'
            html += f'<td>{comp}</td><td style="font-size:11px">{sup_str}</td><td style="font-size:11px">{res_str}</td></tr>'
        html += '</table>'
    else:
        html += '<div class="alert alert-success">✅ 今日无强制操作，持仓正常运行。所有标的趋势正常，无止损/减仓触发。</div>'

    # ③ 操盘密码速览（仅有D/K信号的标的）
    dk_stocks = [(code, s) for code, s in shared.items() if s["caopan"] and s["caopan"].get("dk_signal") and not s["caopan"].get("dk_filtered", False)]
    if dk_stocks:
        html += '<h2>③ 操盘密码DK信号</h2>'
        html += '<table><tr><th>标的</th><th>DK信号</th><th>强度</th><th>等级</th><th>趋势(5级)</th><th>乖离率</th><th>操作建议</th></tr>'
        for code, s in dk_stocks:
            cr = s["caopan"]
            dk = cr.get("dk_signal", "")
            dk_color = "#e53935" if dk == "D" else "#4caf50"
            action = cr.get("action_suggestion", {})
            html += f'<tr><td><b>{s["name"]}</b></td>'
            html += f'<td style="color:{dk_color};font-weight:bold">{dk}</td>'
            html += f'<td>{cr.get("dk_strength", 0)}分</td>'
            html += f'<td>{cr.get("dk_grade", "")}</td>'
            html += f'<td>{cr.get("trend_desc", "")}({cr.get("trend_level", 3)}级)</td>'
            html += f'<td>{cr.get("deviation_pct", 0):.1f}%</td>'
            html += f'<td>{action.get("desc", "观望")}</td></tr>'
        html += '</table>'

    # ④ 次日调仓计划（基于综合评分再平衡）
    # V3.2回测诊断: 调仓增益-0.85%，收紧阈值
    # FIX: 调仓阈值改读config.REBALANCE_CONFIG（config.py未更新时getattr兜底原硬编码值）
    _rb_cfg = getattr(config, "REBALANCE_CONFIG",
                      {"score_gap": 25, "sell_threshold": 30, "buy_threshold": 70})
    REBALANCE_SCORE_GAP = _rb_cfg.get("score_gap", 25)   # V3.2: 20→25
    REBALANCE_SELL_THRESHOLD = _rb_cfg.get("sell_threshold", 30)  # V3.2: 40→30
    REBALANCE_BUY_THRESHOLD = _rb_cfg.get("buy_threshold", 70)   # V3.2: 65→70
    REBALANCE_MAX_POSITION_RATIO = 0.15
    REBALANCE_TRADE_COST_RATE = 0.0015

    html += '<h2>④ 次日调仓计划</h2>'
    html += '<div style="font-size:11px;color:#999;margin-bottom:8px">[DISCLAIMER] 系统量化建议，不构成投资建议</div>'
    try:
        # 收集持仓评分
        _rb_holds = []
        for _c, _s in shared.items():
            _t = _s.get("tech", {})
            if not _t.get("valid"):
                continue
            _info = _s.get("info", {})
            _shares = _info.get("数量", _info.get("shares", 0))
            if _shares <= 0:
                continue
            _rb_holds.append({
                "code": _c, "name": _s["name"],
                "shares": _shares,
                "cost": _info.get("成本", _info.get("buy_price", 0)),
                "price": _s["price"],
                "score": _t.get("composite", 50),
                "pnl_pct": round((_s["price"] - _info.get("成本", _info.get("buy_price", _s["price"]))) / max(_info.get("成本", _info.get("buy_price", _s["price"])), 0.01) * 100, 1),
            })
        # 候选池
        _rb_cands = []
        for _w in (watchlist or []):
            _wp = _w.get("plan") or {}
            _rb_cands.append({
                "code": _w.get("code", ""), "name": _w.get("name", ""),
                "score": _w.get("total_score", 0),
                "sector": _w.get("sector", ""),
                "price": _w.get("price", 0),
                "buy_high": _wp.get("buy_high", 0),
                "stop_loss": _wp.get("stop_loss", 0),
            })
        _rb_holds.sort(key=lambda x: x["score"])
        _rb_cands.sort(key=lambda x: x["score"], reverse=True)

        _rb_has = False
        if _rb_holds and _rb_cands:
            _gap = _rb_cands[0]["score"] - _rb_holds[0]["score"]
            if (_rb_holds[0]["score"] < REBALANCE_SELL_THRESHOLD and
                _rb_cands[0]["score"] > REBALANCE_BUY_THRESHOLD and
                _gap >= REBALANCE_SCORE_GAP):
                _rb_has = True
                # FIX: 原仅处理1只最差持仓，改为循环处理所有评分<50的持仓（与generate_holdings_report一致）
                _sell_items = []
                for _sh in _rb_holds:
                    if _sh["score"] < REBALANCE_SELL_THRESHOLD:
                        _s_ratio = 1.0
                        _s_reason = f"评分{_sh['score']:.0f}<{REBALANCE_SELL_THRESHOLD}，趋势破位，全部清仓"
                    elif _sh["score"] < 50:
                        _s_ratio = 0.5
                        _s_reason = f"评分{_sh['score']:.0f}偏低，减仓50%降低风险"
                    else:
                        continue
                    _s_shares = int(_sh["shares"] * _s_ratio / 100) * 100
                    if _s_shares < 100:
                        _s_shares = _sh["shares"]  # 不足1手则全卖
                    _s_price = round(_sh["price"] * 0.995, 3)
                    _s_amount = _s_shares * _s_price
                    _sell_items.append({**_sh, "sell_shares": _s_shares, "sell_price": _s_price, "sell_amount": _s_amount, "sell_reason": _s_reason})

                # 渲染卖出计划
                _total_sell_amount = sum(s["sell_amount"] for s in _sell_items)
                for _si in _sell_items:
                    html += '<div style="background:#FFF0F0;border:1px solid #ffccc7;border-radius:6px;padding:10px;margin:6px 0">'
                    html += f'<b style="color:#cf1322">[SELL]</b> <b>{_si["name"]}</b>({_si["code"]}) | 评分<b style="color:#cf1322">{_si["score"]:.0f}</b> | 卖{_si["sell_shares"]}股@{_si["sell_price"]:.2f} | 释放{_si["sell_amount"]:,.0f}元 | 理由: {_si["sell_reason"]}</div>'

                # 买入计划（基于释放资金）
                _b_budget = min(_total_sell_amount, config.TOTAL_CAPITAL * REBALANCE_MAX_POSITION_RATIO)
                _best = _rb_cands[0]
                _b_price = _best["buy_high"] if _best["buy_high"] > 0 else round(_best["price"] * 1.005, 2)
                _b_shares = int(_b_budget / max(_b_price, 0.01) / 100) * 100
                _b_amount = _b_shares * _b_price if _b_shares > 0 else 0
                _b_stop = _best["stop_loss"] if _best["stop_loss"] > 0 else round(_b_price * 0.95, 2)
                _cost = (_total_sell_amount + _b_amount) * REBALANCE_TRADE_COST_RATE

                if _b_shares > 0:
                    html += '<div style="background:#F0FFF0;border:1px solid #b7eb8f;border-radius:6px;padding:10px;margin:6px 0">'
                    html += f'<b style="color:#389e0d">[BUY]</b> <b>{_best["name"]}</b>({_best["code"]}) | 评分<b style="color:#389e0d">{_best["score"]:.0f}</b> | 买{_b_shares}股@{_b_price:.2f} | 止损{_b_stop:.2f} | 仓位{_b_amount/max(config.TOTAL_CAPITAL,1)*100:.1f}%</div>'
                html += '<div style="border:1px solid #1976d2;border-radius:6px;padding:8px;margin:6px 0;font-size:12px">'
                html += f'<b style="color:#1976d2">[SWAP]</b> 评分差{_gap:.0f}分 | 卖{len(_sell_items)}只/释放{_total_sell_amount:,.0f}元 | 净额{_total_sell_amount-_b_amount:+,.0f}元 | 交易成本{_cost:,.0f}元 | 风险: 新标的次日低开>3%立即止损</div>'
                print(f"  [SWAP] 调仓: 卖{len(_sell_items)}只(释放{_total_sell_amount:,.0f}元) → 买{_best['name']}(评分{_best['score']:.0f})")

        if not _rb_has:
            _mg = (_rb_cands[0]["score"] - _rb_holds[0]["score"]) if (_rb_holds and _rb_cands) else 0
            html += f'<div class="alert alert-success">[OK] 持仓评分均衡，无需调仓（最大评分差{_mg:.0f}<{REBALANCE_SCORE_GAP}）</div>'
    except Exception as _rb_e:
        html += f'<div class="alert alert-warning">[WARN] 调仓计划异常: {_rb_e}</div>'

    # ⑤ 选股观察池（精简版）
    if watchlist:
        html += '<h2>⑤ 选股观察池</h2>'
        html += '<table><tr><th>代码</th><th>名称</th><th>赛道</th><th>评分</th><th>趋势</th><th>入选理由</th><th>关注价位</th></tr>'
        for w in watchlist[:8]:
            reasons_list = w.get("reasons", [])
            reason_text = "、".join(reasons_list[:2]) if reasons_list else "综合评分达标"
            plan = w.get("plan")
            if plan and plan.get("buy_low") and plan.get("buy_high"):
                price_range = f"{plan['buy_low']:.2f}~{plan['buy_high']:.2f}"
            elif w.get("price", 0) > 0:
                price_range = f"{w['price']*0.95:.2f}~{w['price']*1.02:.2f}(参考)"
            else:
                price_range = "—"
            trend_dir = w.get("trend_dir", "")
            trend_color = "#4caf50" if "强" in trend_dir or "上涨" in trend_dir else "#ff9800" if "震荡" in trend_dir else "#f44336"
            html += f'<tr><td>{w["code"]}</td><td><b>{w["name"]}</b></td><td>{w["sector"]}</td>'
            html += f'<td style="font-weight:bold">{w["total_score"]}</td>'
            html += f'<td style="color:{trend_color}">{trend_dir}</td>'
            html += f'<td style="font-size:11px;text-align:left">{reason_text}</td>'
            html += f'<td style="color:#1976d2;font-weight:bold">{price_range}</td></tr>'
        html += '</table>'

    # ⑥ 风险提醒（仅有变化时显示）
    # 解禁检查
    risk_alerts = []
    try:
        from strategy.event_calendar import EventCalendar
        calendar = EventCalendar()
        for code, s in shared.items():
            try:
                rr = calendar.check_stock_release_risk(code)
                if rr and rr.get("has_release") and rr.get("max_impact") in ("high", "medium"):
                    risk_alerts.append(f"⚠️ {s['name']}({code}) 近期有解禁（{rr.get('max_impact')}冲击）")
            except Exception:
                pass
    except Exception:
        pass
    # 龙虎榜异动
    try:
        from strategy.lhb_analyzer import LHBAnalyzer
        lhb = LHBAnalyzer()
        for code, s in shared.items():
            try:
                lhb_r = lhb.analyze(code, days=5)
                if lhb_r and lhb_r.get("signal") == "bearish":
                    risk_alerts.append(f"⚠️ {s['name']}({code}) 龙虎榜看空信号")
            except Exception:
                pass
    except Exception:
        pass

    if risk_alerts:
        html += '<h2>⑥ 风险提醒</h2>'
        for alert in risk_alerts:
            html += f'<div class="alert alert-warning">{alert}</div>'

    # footer
    html += f"""
<div class="footer">
盘前快速决策报告 | {today} {now} | 三层分时报告体系·第一层<br>
⚠️ 仅供参考，非投资建议 | 股市有风险，投资需谨慎
</div></div></div></body></html>"""

    # 保存+发送
    output_dir = os.path.join(TRADING_SYSTEM_DIR, 'output')
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, f'morning_brief_{today.replace("-", "")}.html')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"[保存] {report_path}")

    subject = f"[盘前] 今日操作清单 | {today} {weekday_cn}"
    print(f"[发送] {subject}")
    result = send_email(subject, html)
    print(f"[结果] {'✅ 发送成功' if result else '❌ 发送失败'}")
    return result


# ============================================================
# 第二层：盘后深度复盘报告（15:30）— 占位，后续SearchReplace补充
# ============================================================

def run_evening():
    """
    第二层：盘后深度复盘报告
    全量分析 + 条件单 + 资金面三件套 + 大白话解读

    数据完整性清单:
    - 逐只条件单（止损价/止盈价/时间单/反弹买入）✓
    - 龙虎榜/游资动向 ✓
    - 融资融券信号 ✓
    - 解禁风险预警 ✓
    - 四级资金流向（主力/散户模式识别）✓
    - 大白话解读卡片（四个板块各一段）✓
    - 操盘密码完整版（趋势5级/DK/乖离率/市场环境）✓
    - 持仓全景诊断（评分/趋势/RSI/MACD/支撑压力）✓
    """
    print("\n" + "=" * 60)
    print("  第二层：盘后深度复盘报告")
    print("=" * 60)

    # FIX: 时间变量改为入口处按需取值（原模块级变量在导入时固化）
    today, now, weekday_cn = _today(), _now(), _weekday_cn()

    holdings_list = load_holdings()
    shared = fetch_shared_data(holdings_list)
    # FIX: 统一使用config.INITIAL_STOP_LOSS_PCT，与盘前报告口径一致（同盘前段②止损价计算）
    STOP_LOSS_PCT = config.INITIAL_STOP_LOSS_PCT

    # === 扩展数据获取（龙虎榜/融资融券/解禁/四级资金流）===
    print("[扩展] 获取龙虎榜/融资融券/解禁/四级资金流...")
    lhb_results, margin_results, release_risks = {}, {}, {}
    flow_results, pattern_results = {}, {}
    try:
        from strategy.lhb_analyzer import LHBAnalyzer
        lhb = LHBAnalyzer()
        for code in shared:
            try:
                lhb_results[code] = lhb.analyze(code, days=10)
            except Exception:
                pass
        print(f"  龙虎榜: {len(lhb_results)}只")
    except Exception as e:
        print(f"  龙虎榜: 失败({e})")
    try:
        from strategy.margin_monitor import MarginMonitor
        margin = MarginMonitor()
        for code in shared:
            try:
                margin_results[code] = margin.calc_margin_signal(code, days=10)
            except Exception:
                pass
        print(f"  融资融券: {len(margin_results)}只")
    except Exception as e:
        print(f"  融资融券: 失败({e})")
    try:
        from strategy.event_calendar import EventCalendar
        calendar = EventCalendar()
        for code in shared:
            try:
                release_risks[code] = calendar.check_stock_release_risk(code)
            except Exception:
                pass
        print(f"  解禁: {len(release_risks)}只")
    except Exception as e:
        print(f"  解禁: 失败({e})")
    try:
        from strategy.capital_flow import CapitalFlowAnalyzer
        cf = CapitalFlowAnalyzer()
        for code in shared:
            try:
                flow_results[code] = cf.analyze_multi_level_flow(code, days=5)
                pattern_results[code] = cf.detect_flow_pattern(code, days=5)
            except Exception:
                pass
        print(f"  四级资金流: {len(flow_results)}只")
    except Exception as e:
        print(f"  四级资金流: 失败({e})")

    # === 构建HTML ===
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{COMMON_CSS}
.meta{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:8px 0}}
.meta span{{font-size:12px;color:#555}}
.score-bar{{height:8px;border-radius:4px;background:#eee;margin:4px 0;position:relative}}
.score-fill{{height:100%;border-radius:4px;position:absolute;left:0;top:0}}
.flow-bar{{height:8px;border-radius:4px}}
.flow-positive{{background:#e74c3c}} .flow-negative{{background:#27ae60}}
</style></head><body>
<div class="container">
<div class="header">
<h1>📊 盘后深度复盘 <span class="mode-badge">全量分析+条件单</span></h1>
<div class="sub">{today} {weekday_cn} | 持仓{len(shared)}只 | 止损规则: 最新价×{1-STOP_LOSS_PCT:.0%}</div>
</div>
<div class="content">
"""

    # V2.5: 今日实盘操作摘要
    html += '<h2>⓪ 今日实盘操作摘要</h2>'
    try:
        _trades_file = os.path.join(BASE_DIR, 'trades_today.json')
        if os.path.exists(_trades_file):
            with open(_trades_file, 'r', encoding='utf-8') as _tf:
                _td = json.load(_tf)
            _trades = [t for t in _td.get('trades', []) if t.get('status') == '已成']
            _ts = _td.get('summary', {})
            if _trades:
                html += f'<p>日期: {_td.get("date", today)} | '
                html += f'买入{_ts.get("buy_count", 0)}笔({_ts.get("buy_amount", 0)/10000:.1f}万) | '
                html += f'卖出{_ts.get("sell_count", 0)}笔({_ts.get("sell_amount", 0)/10000:.1f}万) | '
                html += f'净流入{_ts.get("net_flow", 0)/10000:.1f}万</p>'
                # 按股票汇总
                _stock_ops = {}
                for t in _trades:
                    key = f'{t["code"]} {t["name"]}'
                    if key not in _stock_ops:
                        _stock_ops[key] = {'buy_qty': 0, 'sell_qty': 0, 'buy_amt': 0, 'sell_amt': 0}
                    if t['direction'] == '买入':
                        _stock_ops[key]['buy_qty'] += t['qty']
                        _stock_ops[key]['buy_amt'] += t['amount']
                    else:
                        _stock_ops[key]['sell_qty'] += t['qty']
                        _stock_ops[key]['sell_amt'] += t['amount']
                html += '<table><tr><th>股票</th><th>买入</th><th>卖出</th><th>净操作</th><th>金额(万)</th></tr>'
                for stock, ops in _stock_ops.items():
                    net = ops['buy_qty'] - ops['sell_qty']
                    net_str = f'+{net}' if net > 0 else str(net)
                    net_color = '#e74c3c' if net > 0 else '#27ae60' if net < 0 else '#666'
                    amt = (ops['buy_amt'] + ops['sell_amt']) / 10000
                    html += f'<tr><td>{stock}</td>'
                    html += f'<td>{ops["buy_qty"] if ops["buy_qty"] else "-"}</td>'
                    html += f'<td>{ops["sell_qty"] if ops["sell_qty"] else "-"}</td>'
                    html += f'<td style="color:{net_color};font-weight:bold">{net_str}</td>'
                    html += f'<td>{amt:.2f}</td></tr>'
                html += '</table>'
                cleared = _ts.get('cleared_stocks', [])
                new_pos = _ts.get('new_positions', [])
                if cleared:
                    html += f'<p style="color:#e74c3c">清仓: {", ".join(cleared)}</p>'
                if new_pos:
                    html += f'<p style="color:#1976d2">新建仓: {", ".join(new_pos)}</p>'
            else:
                html += '<p style="color:#999">今日无已成委托</p>'
        else:
            html += '<p style="color:#999">无委托数据</p>'
    except Exception as e:
        html += f'<p style="color:#999">委托数据读取失败: {e}</p>'

    # ① 持仓全景诊断
    html += '<h2>① 持仓全景诊断</h2>'
    html += '<table><tr><th>代码</th><th>名称</th><th>最新价</th><th>涨跌</th><th>评分</th><th>趋势</th><th>RSI</th><th>MACD</th><th>止损价</th><th>建议持有</th></tr>'
    for code, s in shared.items():
        tech = s["tech"]
        price = s["price"]
        chg = s["change_pct"]
        chg_class = "up" if chg >= 0 else "down"
        comp = tech.get("composite", 0) if tech.get("valid") else 0
        trend_dir = tech.get("trend_dir", "N/A") if tech.get("valid") else "数据不足"
        rsi_val = tech.get("rsi", "-") if tech.get("valid") else "-"
        macd_state = ""
        if tech.get("valid"):
            macd_state = "金叉" if (tech.get("macd_dif", 0) or 0) > (tech.get("macd_dea", 0) or 0) else "死叉"
        stop_price = round(price * (1 - STOP_LOSS_PCT), 2) if price > 0 else 0
        hold_s = tech.get("hold_suggest", "-") if tech.get("valid") else "-"
        score_color = "#4caf50" if comp >= 60 else "#ff9800" if comp >= 40 else "#f44336"
        html += f'<tr><td>{code}</td><td><b>{s["name"]}</b></td><td><b>{price:.3f}</b></td>'
        html += f'<td class="{chg_class}">{chg:+.2f}%</td>'
        html += f'<td style="color:{score_color};font-weight:bold">{comp}</td>'
        html += f'<td>{trend_dir}</td><td>{rsi_val}</td>'
        html += f'<td style="color:{"#e74c3c" if macd_state=="金叉" else "#27ae60"}">{macd_state}</td>'
        html += f'<td style="color:#e74c3c;font-weight:bold">{stop_price:.2f}</td>'
        html += f'<td style="color:#1976d2">{hold_s}</td></tr>'
    html += '</table>'

    # ② 逐只条件单
    html += '<h2>② 条件单设置表</h2>'
    for code, s in shared.items():
        price = s["price"]
        if price <= 0:
            continue
        tech = s["tech"]
        stop_price = round(price * (1 - STOP_LOSS_PCT), 2)
        quote = s["quote"]
        day_high = quote.get("high", price)
        day_low = quote.get("low", price)
        fallback_trigger = round(day_high * 0.97, 2)
        rebound_trigger = round(day_low * 1.03, 2)
        # 止盈目标
        target_1 = round(price * 1.08, 2)
        target_2 = round(price * 1.20, 2)
        comp = tech.get("composite", 50) if tech.get("valid") else 50
        hold_reason = tech.get("hold_reason", "") if tech.get("valid") else ""

        html += f'<div class="stock-card"><h3>{code} {s["name"]} '
        if comp >= 60:
            html += '<span class="tag tag-hold">持有</span>'
        elif comp >= 40:
            html += '<span class="tag tag-reduce">关注</span>'
        else:
            html += '<span class="tag tag-stop">警惕</span>'
        html += f' <span style="float:right;font-size:12px;color:#888">评分{comp}</span></h3>'
        html += '<table><tr><th>条件单</th><th>设置</th><th>有效期</th></tr>'
        html += f'<tr><td><b>① 定价止损</b></td><td style="color:#e74c3c;font-weight:bold">触发价 {stop_price}，委托价 {stop_price*0.995:.2f}</td><td>20天</td></tr>'
        html += f'<tr><td><b>② 14:50时间单</b></td><td style="color:#e74c3c">最新价≤{stop_price} 则卖出</td><td>10天</td></tr>'
        html += f'<tr><td><b>③ 回落卖出</b></td><td style="color:#ff9800">日高{day_high:.3f}回落至 {fallback_trigger} 卖出</td><td>10天</td></tr>'
        html += f'<tr><td><b>④ 反弹买入</b></td><td style="color:#4caf50">日低{day_low:.3f}反弹至 {rebound_trigger} 买入</td><td>5天</td></tr>'
        html += f'<tr><td><b>⑤ 止盈目标</b></td><td>第一目标 {target_1}(减仓1/2) | 第二目标 {target_2}(清仓)</td><td>20天</td></tr>'
        html += '</table>'
        if hold_reason:
            html += f'<div style="font-size:11px;color:#666;margin-top:5px">💡 {hold_reason}</div>'
        html += '</div>'

    # ③ 操盘密码完整版
    html += '<h2>③ 操盘密码分析 V2.0</h2>'
    caopan_stocks = [(code, s) for code, s in shared.items() if s["caopan"]]
    if caopan_stocks:
        html += '<table><tr><th>标的</th><th>趋势(5级)</th><th>LL1/LL2</th><th>乖离率</th><th>DK信号</th><th>盈亏比</th><th>市场环境</th><th>操作建议</th></tr>'
        for code, s in caopan_stocks:
            cr = s["caopan"]
            tl = cr.get('trend_level', 3)
            trend_color = {5:'#e53935',4:'#ff7043',3:'#ff9800',2:'#66bb6a',1:'#4caf50'}.get(tl, '#333')
            dk = cr.get('dk_signal') or '无'
            dk_filtered = cr.get('dk_filtered', False)
            dk_color = '#e53935' if dk == 'D' and not dk_filtered else '#4caf50' if dk == 'K' and not dk_filtered else '#999'
            dk_text = f'{dk}({cr.get("dk_strength",0)}分/{cr.get("dk_grade","")})' + ('[过滤]' if dk_filtered else '')
            rr = cr.get('risk_reward', {})
            env = cr.get('market_env', {})
            action = cr.get('action_suggestion', {})
            html += f'<tr><td><b>{s["name"]}</b>({code})</td>'
            html += f'<td style="color:{trend_color};font-weight:bold">{cr.get("trend_desc","")}({tl}级)</td>'
            html += f'<td><span style="color:#f5a623">{cr.get("ll_fast",0):.2f}{cr.get("ll_fast_direction","")}</span>/<span style="color:#9c27b0">{cr.get("ll_slow",0):.2f}{cr.get("ll_slow_direction","")}</span></td>'
            html += f'<td>{cr.get("deviation_pct",0):.1f}% {cr.get("deviation_action","")}</td>'
            html += f'<td style="color:{dk_color};font-weight:bold">{dk_text}</td>'
            html += f'<td style="color:{"#4caf50" if rr.get("passed") else "#f44336"}">{rr.get("risk_reward_1",0):.1f}:1</td>'
            html += f'<td>{env.get("mode","")}</td>'
            html += f'<td>{action.get("desc","观望")}</td></tr>'
        html += '</table>'
        # 趋势降级预警
        downgrades = [(code, s) for code, s in caopan_stocks if s["caopan"].get('trend_level', 3) <= 2]
        if downgrades:
            html += '<div class="alert alert-danger">🚨 趋势降级预警: ' + ', '.join([f'{s["name"]}({s["caopan"]["trend_desc"]})' for _, s in downgrades]) + ' → 建议清仓/禁止加仓</div>'

    # ④ 龙虎榜/游资动向 + 大白话
    html += '<h2>④ 龙虎榜/游资动向</h2>'
    if lhb_results:
        html += '<table><tr><th>股票</th><th>上榜次数</th><th>机构趋势</th><th>游资活跃度</th><th>信号</th></tr>'
        for code, s in shared.items():
            lhb_r = lhb_results.get(code)
            if not lhb_r:
                continue
            sig = lhb_r.get('signal', 'neutral')
            sig_class = {'bullish': 'signal-bullish', 'bearish': 'signal-bearish'}.get(sig, 'signal-neutral')
            sig_text = {'bullish': '看多', 'bearish': '看空', 'neutral': '中性'}.get(sig, sig)
            trend_map = {'increasing': '↑ 上升', 'decreasing': '↓ 下降', 'neutral': '→ 平稳'}
            trend_text = trend_map.get(lhb_r.get('institution_trend', 'neutral'), '')
            html += f'<tr><td><b>{s["name"]}</b>({code})</td><td>{lhb_r.get("lhb_count", 0)}</td>'
            html += f'<td>{trend_text}</td><td>{lhb_r.get("hot_money_score", 0):.0f}分</td>'
            html += f'<td><span class="signal-tag {sig_class}">{sig_text}</span></td></tr>'
        html += '</table>'
    html += '''<div class="explain-card">
<span class="ec-title">💬 大白话解读：龙虎榜是什么？</span>
<b>龙虎榜</b>就像股市的"大额交易公示栏"——当一只股票当天涨跌幅、换手率或振幅达到交易所规定的门槛时，系统会自动公布当天买卖金额最大的前5个席位。<br>
<b>游资介入意味着什么？</b>游资像"快进快出的猎手"，选中一只股票通常意味着短期有题材催化，但来得快去得也快。<br>
<b>实操建议：</b>① 机构席位连续买入 → 中线看好；② 纯游资主导且上榜频繁 → 追高容易被套；③ 已持仓的设好止损，未持仓的不要追。
</div>'''

    # ⑤ 融资融券信号 + 大白话
    html += '<h2>⑤ 融资融券信号</h2>'
    if margin_results:
        html += '<table><tr><th>股票</th><th>融资净买入天数</th><th>余额趋势</th><th>拐点</th><th>融券异常</th><th>信号</th></tr>'
        for code, s in shared.items():
            mr = margin_results.get(code)
            if not mr:
                continue
            sig = mr.get('signal', 'neutral')
            sig_class = {'bullish': 'signal-bullish', 'bearish': 'signal-bearish'}.get(sig, 'signal-neutral')
            sig_text = {'bullish': '看多', 'bearish': '看空', 'neutral': '中性'}.get(sig, sig)
            trend_map = {'increasing': '↑ 上升', 'decreasing': '↓ 下降', 'neutral': '→ 平稳'}
            trend_text = trend_map.get(mr.get('balance_trend', 'neutral'), '')
            turning = '✅ 是' if mr.get('balance_turning') else '—'
            short_anomaly = '⚠️ 异常' if mr.get('short_selling_anomaly') else '—'
            html += f'<tr><td><b>{s["name"]}</b>({code})</td><td>{mr.get("net_buy_days", 0)}天</td>'
            html += f'<td>{trend_text}</td><td style="color:#4caf50">{turning}</td>'
            html += f'<td style="color:#e74c3c">{short_anomaly}</td>'
            html += f'<td><span class="signal-tag {sig_class}">{sig_text}</span></td></tr>'
        html += '</table>'
    html += '''<div class="explain-card">
<span class="ec-title">💬 大白话解读：融资融券怎么看？</span>
<b>融资</b>就是投资者借钱买股票（看多），<b>融券</b>就是借股票来卖（看空）。<br>
<b>融资余额上升</b> → 越来越多人借钱买入，后市乐观；<b>融资余额下降</b> → 信心不足。<br>
<b>融券异常</b> → 突然有大量资金借股票来砸盘，需要高度警惕。<br>
<b>实操建议：</b>① 融资连续5天净买入+余额上升 → 正面；② 余额拐点（由升转降）→ 注意减仓；③ 融券异常 → 不要加仓。
</div>'''

    # ⑥ 解禁风险预警 + 大白话
    html += '<h2>⑥ 解禁风险预警</h2>'
    has_any_release = any(r.get('has_release') for r in release_risks.values()) if release_risks else False
    high_impact = [code for code, r in release_risks.items() if r.get('max_impact') == 'high']
    if high_impact:
        names = [shared[c]["name"] for c in high_impact if c in shared]
        html += f'<div class="alert alert-danger">🚨 高冲击解禁预警: {", ".join(names)}，建议回避新开仓</div>'
    if has_any_release:
        html += '<table><tr><th>股票</th><th>解禁日期</th><th>冲击等级</th><th>距今天数</th></tr>'
        for code, s in shared.items():
            rr = release_risks.get(code)
            if not rr or not rr.get('has_release'):
                continue
            for evt in rr.get('events', []):
                impact = evt.get('impact_level', 'low')
                impact_color = {'high': '#e74c3c', 'medium': '#ff9800', 'low': '#4caf50'}.get(impact, '#999')
                impact_text = {'high': '高冲击', 'medium': '中冲击', 'low': '低冲击'}.get(impact, impact)
                html += f'<tr><td><b>{s["name"]}</b>({code})</td><td>{evt.get("release_date", "")}</td>'
                html += f'<td style="color:{impact_color};font-weight:bold">{impact_text}</td>'
                html += f'<td>{evt.get("days_until_release", "")}天</td></tr>'
        html += '</table>'
    else:
        html += '<div class="alert alert-success">✅ 持仓近30天内无解禁风险</div>'
    html += '''<div class="explain-card">
<span class="ec-title">💬 大白话解读：解禁是什么意思？</span>
<b>解禁</b>就像"限售期到期，锁着的股票终于可以卖了"。大股东、机构持有的股票有锁定期（通常1-3年），锁定期一过就能自由卖出。<br>
<b>多少比例需要警惕？</b>① &lt;5% → 影响较小；② 5%-15% → 中等冲击；③ &gt;15% → 高冲击，建议回避。<br>
<b>实操建议：</b>解禁后股价不跌反涨，说明接盘力量强，反而是好信号。
</div>'''

    # ⑦ 四级资金流向 + 大白话
    html += '<h2>⑦ 四级资金流向</h2>'
    if flow_results:
        for code, s in shared.items():
            fr = flow_results.get(code)
            if not fr or not fr.get('success'):
                continue
            pr = pattern_results.get(code, {})
            pattern = pr.get('pattern', 'neutral')
            pattern_class = {'accumulation': 'pattern-accumulation', 'distribution': 'pattern-distribution', 'washout': 'pattern-washout'}.get(pattern, '')
            pattern_text = {'accumulation': '吸筹', 'distribution': '出货', 'washout': '洗盘', 'neutral': '无明显模式'}.get(pattern, pattern)
            direction = fr.get('main_force_direction', 'neutral')
            dir_map = {'buying': '🔴 主力买入', 'selling': '🟢 主力卖出', 'neutral': '→ 中性'}
            dir_text = dir_map.get(direction, direction)
            html += f'<div class="stock-card"><h3>{s["name"]}({code}) <span style="font-size:12px;color:#888">{dir_text}</span>'
            if pattern_class:
                html += f' <span class="pattern-tag {pattern_class}">{pattern_text}</span>'
            elif pattern == 'neutral':
                html += f' <span class="pattern-tag" style="background:#95a5a6;color:white">{pattern_text}</span>'
            html += '</h3>'
            levels = [('超大单', 'super_large', '#e74c3c'), ('大单', 'large', '#ff7043'), ('中单', 'medium', '#42a5f5'), ('小单', 'small', '#66bb6a')]
            html += '<table><tr><th>级别</th><th>净流入</th><th>趋势</th></tr>'
            for label, key, color in levels:
                level_data = fr.get(key, {})
                net = level_data.get('net_inflow', 0)
                trend = level_data.get('trend', 'neutral')
                trend_t = {'increasing': '↑', 'decreasing': '↓', 'neutral': '→'}.get(trend, trend)
                net_color = '#e74c3c' if net >= 0 else '#27ae60'
                html += f'<tr><td style="color:{color};font-weight:bold">{label}</td>'
                html += f'<td style="color:{net_color}">{net:+,.0f}</td><td>{trend_t}</td></tr>'
            html += '</table>'
            if pr.get('description') and pattern != 'neutral':
                html += f'<div style="font-size:11px;color:#666;margin-top:5px">💡 {pr["description"]}</div>'
            html += '</div>'
    html += '''<div class="explain-card">
<span class="ec-title">💬 大白话解读：四种资金模式分别意味着什么？</span>
<b>四级资金流</b>把市场资金按体量分成四档：超大单（机构）、大单（大户）、中单（中产散户）、小单（小散户）。<br>
<b>🟢 温和建仓（吸筹）：</b>主力在悄悄买入，量不大。<b>操作：跟随持有，等拉升。</b><br>
<b>🔴 放量拉升：</b>大单集中涌入，股价快速上涨。<b>操作：拿住，不追高。</b><br>
<b>⚠️ 对倒骗线：</b>主力"左手倒右手"制造放量假象。<b>操作：坚决不追，逢高减仓。</b><br>
<b>⚪ 正常/无模式：</b>市场观望。<b>操作：按兵不动。</b>
</div>'''

    # ⑧ 仓位建议
    html += '<h2>⑧ 仓位风险预警</h2>'
    try:
        hjson_path = os.path.join(BASE_DIR, 'holdings.json')
        with open(hjson_path, 'r', encoding='utf-8') as fj:
            hjson = json.load(fj)
        total_mv = sum(v.get('shares', 0) * v.get('current_price', 0) for v in hjson.values())
        stock_mvs = {}
        for k, v in hjson.items():
            mv = v.get('shares', 0) * v.get('current_price', 0)
            stock_mvs[k] = {'name': v.get('name', k), 'mv': mv, 'pct': mv / total_mv * 100 if total_mv > 0 else 0}
        sorted_mvs = sorted(stock_mvs.items(), key=lambda x: x[1]['pct'], reverse=True)
        # FIX: 单只上限按ETF/个股区分走config（pct为百分数单位，故×100后比较）
        _limit_etf_pct = getattr(config, 'MAX_SINGLE_ETF_RATIO', 0.20) * 100
        _limit_stock_pct = getattr(config, 'MAX_SINGLE_STOCK_RATIO', 0.15) * 100
        for code_w, info_w in sorted_mvs:
            _limit_pct = _limit_etf_pct if code_w.startswith(_ETF_PREFIXES) else _limit_stock_pct
            if info_w['pct'] > _limit_pct:
                html += f'<div class="alert alert-danger">🚨 <b>{info_w["name"]}仓位{info_w["pct"]:.1f}%</b>，超出单只上限{_limit_pct:.0f}%！建议分批减仓。</div>'
        if len(sorted_mvs) >= 2:
            top2_pct = sorted_mvs[0][1]['pct'] + sorted_mvs[1][1]['pct']
            if top2_pct > 60:
                html += f'<div class="alert alert-warning">⚡ 持仓集中度: 前2大={top2_pct:.1f}%，风险较高。</div>'
        html += f'<div class="alert alert-info">💰 账户总市值: ¥{total_mv:,.2f} | 持仓{len(hjson)}只</div>'
    except Exception:
        pass

    # footer
    html += f"""
<div class="footer">
盘后深度复盘报告 | {today} {now} | 三层分时报告体系·第二层<br>
技术面分析(baostock前复权) + 实时行情 + 条件单 + 资金面<br>
⚠️ 仅供参考，非投资建议 | 股市有风险，投资需谨慎
</div></div></div></body></html>"""

    # 保存+发送
    output_dir = os.path.join(TRADING_SYSTEM_DIR, 'output')
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, f'evening_review_{today.replace("-", "")}.html')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"[保存] {report_path}")

    subject = f"[盘后] 深度复盘+条件单 | {today} {weekday_cn}"
    print(f"[发送] {subject}")
    result = send_email(subject, html)
    print(f"[结果] {'✅ 发送成功' if result else '❌ 发送失败'}")
    return result


# ============================================================
# 第三层：周策略报告（周六10:00）— 占位，后续SearchReplace补充
# ============================================================

def run_weekly():
    """
    第三层：周策略报告
    绩效归因 + 板块轮动 + 仓位再平衡 + 下周计划

    数据完整性清单:
    - 绩效归因（周收益/胜率/盈亏比）✓
    - 板块轮动 ✓
    - 仓位再平衡建议 ✓
    - 下周计划（关注标的+操作预案）✓
    注：回测验证指标（年化/回撤/夏普/胜率/盈亏比）由strategy_analysis_report.py独立手动触发
    """
    print("\n" + "=" * 60)
    print("  第三层：周策略报告")
    print("=" * 60)

    # FIX: 时间变量改为入口处按需取值（原模块级变量在导入时固化）
    today, now, weekday_cn = _today(), _now(), _weekday_cn()

    holdings_list = load_holdings()
    shared = fetch_shared_data(holdings_list)

    # 计算本周绩效（基于持仓涨跌幅）
    weekly_changes = []
    for code, s in shared.items():
        df = s["df"]
        if df is not None and len(df) >= 5:
            week_chg = (df["close"].iloc[-1] / df["close"].iloc[-5] - 1) * 100
            weekly_changes.append({"code": code, "name": s["name"], "chg": week_chg})

    avg_weekly = np.mean([w["chg"] for w in weekly_changes]) if weekly_changes else 0
    win_count = sum(1 for w in weekly_changes if w["chg"] > 0)
    win_rate = win_count / len(weekly_changes) * 100 if weekly_changes else 0

    # 板块轮动（按赛道分组统计）
    sector_perf = {}
    for w in weekly_changes:
        info = next((h for h in holdings_list if h["code"] == w["code"]), {})
        sector = info.get("赛道", info.get("sector", "其他"))
        if sector not in sector_perf:
            sector_perf[sector] = []
        sector_perf[sector].append(w["chg"])
    sector_avg = {k: np.mean(v) for k, v in sector_perf.items()}

    # 仓位分布
    position_data = []
    try:
        hjson_path = os.path.join(BASE_DIR, 'holdings.json')
        with open(hjson_path, 'r', encoding='utf-8') as fj:
            hjson = json.load(fj)
        total_mv = sum(v.get('shares', 0) * v.get('current_price', 0) for v in hjson.values())
        for k, v in hjson.items():
            mv = v.get('shares', 0) * v.get('current_price', 0)
            position_data.append({"code": k, "name": v.get("name", k), "pct": mv / total_mv * 100 if total_mv > 0 else 0, "mv": mv})
        position_data.sort(key=lambda x: x["pct"], reverse=True)
    except Exception:
        total_mv = 0

    # === 构建HTML ===
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{COMMON_CSS}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0}}
.card{{background:#f8f9fa;border-radius:8px;padding:12px;text-align:center}}
.card .v{{font-size:18px;font-weight:bold}}
.card .l{{font-size:11px;color:#7f8c8d;margin-top:4px}}
</style></head><body>
<div class="container">
<div class="header">
<h1>📅 周策略报告 <span class="mode-badge">绩效+轮动+再平衡</span></h1>
<div class="sub">{today} {weekday_cn} | 管中线波段节奏（3天-4周）</div>
</div>
<div class="content">
"""

    # ① 本周绩效
    html += '<h2>① 本周绩效</h2>'
    html += '<div class="cards">'
    html += f'<div class="card"><div class="v {"up" if avg_weekly >= 0 else "down"}">{avg_weekly:+.2f}%</div><div class="l">持仓平均周涨跌</div></div>'
    html += f'<div class="card"><div class="v">{win_rate:.0f}%</div><div class="l">本周胜率({win_count}/{len(weekly_changes)})</div></div>'
    html += f'<div class="card"><div class="v">{len(holdings_list)}</div><div class="l">持仓标的数</div></div>'
    html += f'<div class="card"><div class="v">¥{total_mv:,.0f}</div><div class="l">账户总市值</div></div>'
    html += '</div>'
    # 逐只周涨跌
    if weekly_changes:
        weekly_changes.sort(key=lambda x: x["chg"], reverse=True)
        html += '<table><tr><th>标的</th><th>本周涨跌</th><th>贡献</th></tr>'
        for w in weekly_changes:
            chg_class = "up" if w["chg"] >= 0 else "down"
            html += f'<tr><td><b>{w["name"]}</b>({w["code"]})</td>'
            html += f'<td class="{chg_class}">{w["chg"]:+.2f}%</td>'
            html += f'<td>{"✅" if w["chg"] > 0 else "⚠️" if w["chg"] > -3 else "🚨"}</td></tr>'
        html += '</table>'

    # ② 板块轮动
    html += '<h2>② 板块轮动</h2>'
    if sector_avg:
        sorted_sectors = sorted(sector_avg.items(), key=lambda x: x[1], reverse=True)
        html += '<table><tr><th>赛道</th><th>本周表现</th><th>趋势</th></tr>'
        for sector, avg in sorted_sectors:
            chg_class = "up" if avg >= 0 else "down"
            trend = "📈 强势" if avg > 3 else "↗️ 偏多" if avg > 0 else "↘️ 偏空" if avg > -3 else "📉 弱势"
            html += f'<tr><td><b>{sector}</b></td><td class="{chg_class}">{avg:+.2f}%</td><td>{trend}</td></tr>'
        html += '</table>'

    # ③ 仓位再平衡建议
    html += '<h2>③ 仓位再平衡建议</h2>'
    if position_data:
        html += '<table><tr><th>标的</th><th>当前仓位</th><th>建议</th></tr>'
        for p in position_data:
            if p["pct"] > 25:
                advice = "🚨 超配，建议减仓至20%"
            elif p["pct"] > 20:
                advice = "⚠️ 偏高，关注止损"
            elif p["pct"] < 5:
                advice = "💡 偏低，可考虑加仓"
            else:
                advice = "✅ 正常"
            html += f'<tr><td><b>{p["name"]}</b>({p["code"]})</td><td>{p["pct"]:.1f}%</td><td>{advice}</td></tr>'
        html += '</table>'
        # 集中度预警
        if len(position_data) >= 2:
            top2 = position_data[0]["pct"] + position_data[1]["pct"]
            if top2 > 60:
                html += f'<div class="alert alert-warning">⚡ 前2大持仓集中度{top2:.1f}%，建议分散至单只≤20%</div>'

    # ④ 下周计划
    html += '<h2>④ 下周关注</h2>'
    # 基于当前趋势生成建议
    strong_stocks = [(code, s) for code, s in shared.items() if s["tech"].get("valid") and s["tech"].get("composite", 0) >= 70]
    weak_stocks = [(code, s) for code, s in shared.items() if s["tech"].get("valid") and s["tech"].get("composite", 0) < 35]
    if strong_stocks:
        html += '<div class="alert alert-success">📈 强势持有: ' + ', '.join([f'{s["name"]}({s["tech"]["composite"]}分)' for _, s in strong_stocks]) + ' → 继续持有，跟踪止盈</div>'
    if weak_stocks:
        html += '<div class="alert alert-danger">📉 弱势警惕: ' + ', '.join([f'{s["name"]}({s["tech"]["composite"]}分)' for _, s in weak_stocks]) + ' → 设好止损，反弹减仓</div>'
    if not strong_stocks and not weak_stocks:
        html += '<div class="alert alert-info">ℹ️ 持仓整体处于震荡区间，下周关注方向突破。保持现有仓位，等待信号。</div>'

    # footer
    html += f"""
<div class="footer">
周策略报告 | {today} {now} | 三层分时报告体系·第三层<br>
回测验证指标请运行: python trading_system/strategy_analysis_report.py<br>
⚠️ 仅供参考，非投资建议
</div></div></div></body></html>"""

    # 保存+发送
    output_dir = os.path.join(TRADING_SYSTEM_DIR, 'output')
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, f'weekly_strategy_{today.replace("-", "")}.html')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"[保存] {report_path}")

    week_num = datetime.date.today().isocalendar()[1]
    subject = f"[周报] 本周绩效+下周计划 | W{week_num} {today}"
    print(f"[发送] {subject}")
    result = send_email(subject, html)
    print(f"[结果] {'✅ 发送成功' if result else '❌ 发送失败'}")
    return result


# ============================================================
# CANSLIM独立选股报告（手动/定时共用入口）
# ============================================================

def run_canslim():
    """
    CANSLIM独立选股报告（手动触发 + 09:25定时触发共用此入口）

    市场自适应:
    - 上涨市: min_score=45，正常模式
    - 震荡市: min_score=35，适度放宽
    - 下跌市: 仅输出观察建议，标注"建议空仓等待"

    输出完整性:
    - CANSLIM综合评分(N/S/L/CAI/P五因子) ✓
    - 三档买点(激进/稳健/保守) ✓
    - 止损价及止损幅度% ✓
    - 风险等级标注 ✓
    - 首批建仓股数和金额 ✓
    - 加仓触发价和第二批股数 ✓
    - 最大亏损金额 ✓
    - 行业分布统计 ✓
    - 操作指南 ✓
    - 市场环境简评 ✓
    """
    print("\n" + "=" * 60)
    print("  CANSLIM独立选股报告")
    print("=" * 60)

    # FIX: 时间变量改为入口处按需取值（原模块级变量在导入时固化）
    today, now, weekday_cn = _today(), _now(), _weekday_cn()

    from strategy.stock_screener import run_stock_screener, send_screener_email, check_market_direction

    # 获取候选股票池数据
    holdings_list = load_holdings()
    holdings = {h["code"]: h for h in holdings_list}

    # V3.2: 三层候选池架构（与caopan_report.run_screener对齐）
    # 第1层: SECTOR_CANDIDATES 静态配置池
    sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
    all_codes = set()
    for sector_name, sector_info in sector_candidates.items():
        stocks = sector_info.get("stocks", {})
        all_codes.update(stocks.keys())
    static_count = len(all_codes)

    # 第2层: PoolManager 观察池（动态维护）
    pool_new = 0
    try:
        from strategy.pool_manager import PoolManager
        _pm = PoolManager()
        for code in _pm.get_watch_codes():
            if code not in all_codes:
                all_codes.add(code)
                pool_new += 1
    except Exception:
        pass

    # 第3层: 全市场动态扫描（V4.0 G3: 先构建可投资域，与热股扫描共享单次行情拉取）
    # V1.1扩面: total_max 从 hardcoded 15 → config.SCREENER_SCAN_MAX(30)
    _expand = getattr(config, 'CANDIDATE_POOL_EXPAND_ENABLED', True)
    _screener_scan_max = getattr(config, 'SCREENER_SCAN_MAX', 30) if _expand else 15
    scan_new = 0
    scan_ok = False  # V4.0(G12): 扫描失败时报告需标注数据降级
    _universe_info = None
    try:
        from strategy.market_scanner import (scan_market_hot_stocks,
                                             merge_scan_results_to_pool,
                                             build_investable_universe)
        _uni = build_investable_universe()
        _spot_df = _uni.get("df") if _uni.get("success") else None
        if _uni.get("success"):
            _universe_info = {"size": _uni["size"], "total": _uni["total"],
                              "filter_stats": _uni["filter_stats"]}
        scan_result = scan_market_hot_stocks(total_max=_screener_scan_max, spot_df=_spot_df)
        if scan_result.get("success"):
            scan_ok = True
            new_codes = merge_scan_results_to_pool(scan_result, all_codes)
            for code in new_codes[:_screener_scan_max]:
                all_codes.add(code)
                scan_new += 1
    except Exception:
        pass

    all_codes.add("000300")  # 沪深300指数

    print(f"[选股] 候选股票池: {len(all_codes)}只 "
          f"(静态{static_count} + 观察池{pool_new} + 动态{scan_new} + 指数1)")

    # 批量拉取数据
    start = (datetime.date.today() - datetime.timedelta(days=300)).strftime("%Y-%m-%d")
    data_dict = {}
    for code in all_codes:
        try:
            df = fetch_stock_daily_baostock(code, start_date=start)
            if df is not None and not df.empty and len(df) >= 60:
                df["ma5"] = df["close"].rolling(5).mean()
                df["ma10"] = df["close"].rolling(10).mean()
                df["ma20"] = df["close"].rolling(20).mean()
                df["ma60"] = df["close"].rolling(60).mean()
                df["ma20_slope"] = df["ma20"].diff(3)
                data_dict[code] = df
        except Exception:
            pass
    _bs_logout()
    print(f"[选股] 有效数据: {len(data_dict)}只")

    if len(data_dict) < 5:
        print("[选股] ❌ 数据不足，无法运行选股")
        return None

    # 大盘状态检测 + 自适应严格度
    market_info = check_market_direction(data_dict)
    market_state = market_info["market_state"]
    market_detail = market_info.get("detail", "")

    # 动态调整min_score
    # FIX: 自适应min_score改读config.SCREENER_CONFIG（config.py未更新时get兜底原硬编码值）
    _sc_cfg = getattr(config, "SCREENER_CONFIG", {})
    _min_score_strong = _sc_cfg.get("min_buy_score_strong", 45)
    _min_score_weak = _sc_cfg.get("min_buy_score_weak", 35)
    if market_state == "up":
        adaptive_min_score = _min_score_strong
        mode_desc = f"📈 上涨市 | 正常模式（min_score={_min_score_strong}）"
    elif market_state == "neutral":
        adaptive_min_score = _min_score_weak
        mode_desc = f"↔️ 震荡市 | 适度放宽（min_score={_min_score_weak}）"
    else:  # down
        adaptive_min_score = _min_score_strong  # 保持高分门槛，但标记为观察模式
        mode_desc = "📉 下跌市 | 严格模式（仅观察，不建议买入）"

    print(f"[选股] 大盘状态: {mode_desc}")
    print(f"[选股] {market_detail}")

    # 运行选股引擎
    result = run_stock_screener(data_dict, holdings, min_score=adaptive_min_score)

    # V4.0(G12): 动态扫描失败时标注数据降级，随报告警示区展示
    if not scan_ok:
        result.setdefault("_data_degraded", []).append(
            "全市场动态扫描未成功（网络异常/非盘中），本期仅使用静态+观察池候选，可能遗漏池外强势股")

    # V4.0(G3): 可投资域覆盖率度量（选股域可观测性）
    if _universe_info:
        _cov = len(all_codes) / max(_universe_info["size"], 1) * 100
        _universe_info["coverage_pct"] = round(_cov, 1)
        result["_universe_info"] = _universe_info
        print(f"[选股] 可投资域{_universe_info['size']}只 | "
              f"当前候选池覆盖率{_cov:.1f}%（含静态+观察池+动态）")

    print(f"[选股] ✅ 完成: {result['qualified_count']}只入选 / {result['total_candidates']}只候选")

    if result["stock_pool"]:
        for s in result["stock_pool"]:
            print(f"     {s['code']} {s['name']} | 评分{s['factor_score']} | "
                  f"买点{s['moderate_buy']} | 止损{s['stop_loss']}(-{s['stop_loss_pct']}%) | "
                  f"{s.get('risk_level', '')}")

    # 下跌市标记：不给出买入计划
    if market_state == "down":
        result["_market_override"] = "down"
        result["_market_commentary"] = (
            f"当前大盘处于下降趋势（{market_detail}）。"
            f"历史数据显示，下跌市中买入胜率不足30%，建议空仓等待大盘企稳后再操作。"
            f"以下标的仅供观察跟踪，不建议实际建仓。"
        )
    elif market_state == "neutral":
        result["_market_commentary"] = (
            f"当前大盘处于震荡格局（{market_detail}）。"
            f"建议控制仓位在半仓以内，优先选择强势赛道中缩量回踩支撑的标的，"
            f"严格设置止损，快进快出。"
        )
    else:
        result["_market_commentary"] = (
            f"当前大盘处于上升趋势（{market_detail}）。"
            f"趋势向上时可适度提高仓位，重点关注均线多头排列+放量突破的标的，"
            f"回调至MA20附近是较好的介入时机。"
        )

    # 发送邮件（有入选标的 → 完整报告；无入选 → 简报）
    if result['qualified_count'] > 0:
        success = send_screener_email(result)
        if success:
            print(f"[选股] 📧 选股报告邮件已发送（{result['qualified_count']}只入选）")
        else:
            print(f"[选股] ⚠️ 选股报告邮件发送失败")
    else:
        # 无入选标的：发送"本期无推荐"简报
        reason = "大盘下跌，建议空仓等待" if market_state == "down" else "候选池均不达标（评分/趋势/盈亏比未通过）"
        brief_html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{COMMON_CSS}</style></head><body>
<div class="container">
<div class="header"><h1>📝 CANSLIM选股简报</h1>
<div class="sub">{today} {weekday_cn} | 本期无推荐标的</div></div>
<div class="content">
<div class="alert alert-warning">⛔ <b>本期无符合标准的推荐标的</b><br>原因: {reason}</div>
<h2>市场环境</h2>
<div class="alert alert-info">{mode_desc}<br>{market_detail}</div>
<h2>市场环境简评</h2>
<p style="font-size:13px;line-height:1.8">{result.get('_market_commentary', '')}</p>
<div class="footer">CANSLIM选股报告 | {today} {now} | 三层分时报告体系·独立选股</div>
</div></div></body></html>"""
        brief_subject = f"[CANSLIM选股] {today} | 本期无推荐 | {reason}"
        brief_ok = send_email(brief_subject, brief_html)
        print(f"[选股] 📧 无推荐简报{'已发送' if brief_ok else '发送失败'}")

    return result


# ============================================================
# CLI入口
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="三层分时报告调度器")
    parser.add_argument("--morning", action="store_true", help="第一层：盘前快速决策")
    parser.add_argument("--evening", action="store_true", help="第二层：盘后深度复盘")
    parser.add_argument("--weekly", action="store_true", help="第三层：周策略报告")
    parser.add_argument("--canslim", action="store_true", help="CANSLIM独立选股报告")
    parser.add_argument("--all", action="store_true", help="运行全部（测试用）")
    args = parser.parse_args()

    if args.all:
        run_morning()
        run_evening()
        run_weekly()
        run_canslim()
    elif args.morning:
        run_morning()
    elif args.evening:
        run_evening()
    elif args.weekly:
        run_weekly()
    elif args.canslim:
        run_canslim()
    else:
        parser.print_help()
