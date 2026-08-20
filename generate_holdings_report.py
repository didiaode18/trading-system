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
from data.data_loader import fetch_stock_daily_baostock, load_daily_data, _bs_logout
# FIX: 清理死代码无用 import（generate_trading_plan grep 确认本文件零使用，仅删导入）
from strategy.recommend_engine import run_recommendation
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

# 交易行为自诊断（频繁操作警示，失败时降级）
try:
    from execution.trade_behavior import (
        analyze_trade_behavior, load_trades_from_json, render_behavior_alert_html
    )
    HAS_TRADE_BEHAVIOR = True
except Exception:
    HAS_TRADE_BEHAVIOR = False

# V2.8新增: 回调加仓风控检查（强势潜力股下跌加仓策略，失败时降级，不影响报告主流程）
try:
    from risk.risk_control import check_pullback_add_risk
    HAS_PULLBACK_RISK = True
except Exception:
    HAS_PULLBACK_RISK = False

# 批2-S6新增: 组合风控行动项区块（失败时降级，不影响报告主流程）
try:
    from strategy.portfolio_risk import PortfolioRiskManager
    from strategy.portfolio_risk_actions import build_risk_action_section
    HAS_PORTFOLIO_RISK = True
except Exception:
    HAS_PORTFOLIO_RISK = False

# 资金仓位智能规划板块（独立模块后处理增强，失败时降级，不影响报告主流程）
try:
    from strategy.capital_planner import build_capital_plan_section
    HAS_CAPITAL_PLANNER = True
except Exception:
    HAS_CAPITAL_PLANNER = False

# 批2-S6新增: 执行确认清单（持仓快照diff，失败时降级，不影响报告主流程）
try:
    from execution.execution_closure import diff_and_confirm
    HAS_EXEC_CLOSURE = True
except Exception:
    HAS_EXEC_CLOSURE = False

# 增强1新增: 市场情绪与资金面面板（全数据源降级，失败不影响报告主流程）
try:
    from strategy.market_pulse import get_market_pulse, render_pulse_html
    HAS_MARKET_PULSE = True
except Exception:
    HAS_MARKET_PULSE = False

# 增强3新增: 风控熔断状态横幅（读取既有风控状态文件，失败降级不展示）
try:
    from risk.risk_control import RiskStateManager, StrategyFailureDetector
    HAS_RISK_STATE = True
except Exception:
    HAS_RISK_STATE = False

# V10.0新增: 持仓末位淘汰排名（四维加权评分，失败降级不展示）
try:
    from strategy.holding_ranking import rank_holdings, generate_ranking_html
    HAS_HOLDING_RANKING = True
except Exception:
    HAS_HOLDING_RANKING = False

# V10.0新增: K线形态识别引擎（第六维度评分，失败降级不展示）
try:
    from strategy.candlestick_pattern import CandlestickPatternEngine
    HAS_CANDLESTICK_PATTERN = True
except Exception:
    HAS_CANDLESTICK_PATTERN = False

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
    """从 holdings.json 读取持仓列表，统一数据源消除多文件硬编码不同步

    FIX P0(2026-08-10): 仅保留 shares>0 的真实持仓。
    原实现返回全集（含shares=0已清仓标的），导致总览表/逐只技术分析/操作建议/
    条件单/操盘密码/龙虎榜等面板混入未持有标的，参与汇总、排序与建议生成。
    """
    # FIX: 统一使用config.get_holdings_file()路径解析，与主调度器保持一致
    holdings_file = config.get_holdings_file()
    try:
        with open(holdings_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        result = []
        for code, v in data.items():
            if not isinstance(v, dict):
                continue
            if (v.get("shares", 0) or 0) <= 0:
                continue  # 已清仓/零持仓标的: 不进入综合分析报告任何段落
            result.append({
                "code": code,
                "名称": v.get("name", code),
                "赛道": v.get("sector", "其他"),
                "shares": v.get("shares", 0),
                "buy_price": v.get("buy_price", 0),
                "buy_date": v.get("buy_date", ""),
                "highest": v.get("highest", 0),
                "stop_loss_cfg": v.get("stop_loss", 0),
                "stock_type": v.get("stock_type", "龙头"),
            })
        return result
    except Exception:
        return _FALLBACK_HOLDINGS


holdings_list = _load_holdings_from_json()


# ============================================================
# 增强3: 风控熔断状态横幅（只读既有风控状态，不改变风控主逻辑）
# ============================================================
_SF_LEVEL_META = {
    "normal":  ("正常", "#27ae60", "alert-success"),
    "degrade": ("降级", "#f57c00", "alert-warning"),
    "pause":   ("暂停买入", "#e67e22", "alert-warning"),
    "breaker": ("熔断", "#e74c3c", "alert-danger"),
}


def _render_circuit_breaker_banner() -> str:
    """
    汇总账户熔断/暂停与策略失效三级熔断状态，渲染置顶横幅。
    数据源: output/risk_state.json + output/strategy_failure_state.json（只读）。
    一切异常降级为不展示（返回空串），绝不影响报告主流程。
    """
    if not HAS_RISK_STATE:
        return ""
    alerts = []
    try:
        mgr = RiskStateManager()
        paused, pause_reason = mgr.is_paused()
        if paused:
            alerts.append(f"⛔ 账户熔断/暂停中: {pause_reason}，期间禁止新开仓")
        elif mgr.state.get("weekly_force_reduce"):
            alerts.append("⚠️ 周度亏损熔断已触发: 强制降仓至30%以下")
        _cl = mgr.state.get("consecutive_losses", 0)
        if _cl >= 2 and not paused:
            alerts.append(f"⚠️ 已连续亏损{_cl}笔（3笔触发暂停熔断），新仓从严")
    except Exception:
        pass
    try:
        det = StrategyFailureDetector()
        _lvl = det.state.get("current_level", "normal")
        _name, _color, _cls = _SF_LEVEL_META.get(_lvl, _SF_LEVEL_META["normal"])
        if _lvl != "normal":
            alerts.append(f"📉 策略失效检测: {_name}（滚动胜率/期望退化，按规则限仓或暂停买入）")
    except Exception:
        pass

    if not alerts:
        # 正常态给一行低调展示，避免用户疑惑"是否漏了"
        return ('<div class="alert alert-success">🛡️ 风控熔断状态: '
                '正常（无账户熔断/暂停，策略失效检测正常）</div>')
    body = "<br>".join(alerts)
    # 取最严重级别的配色（alerts顺序: 账户熔断 > 周熔断 > 连亏 > 策略失效）
    _cls = "alert-danger" if ("⛔" in alerts[0] or "熔断已触发" in alerts[0]) else "alert-warning"
    return f'<div class="alert {_cls}">🛡️ 风控熔断状态横幅<br>{body}</div>'


# ============================================================
# V5(问题三): 今日行动看板 —— 分级色块卡片 + 风险/机会横幅
# 展示参数用模块级常量，不写入 config.py；任何异常降级为不展示
# ============================================================
SPOTLIGHT_LOSS_RED_PCT = 8.0      # 浮亏≥8% 或跌破止损线 → 顶部红色横幅
SPOTLIGHT_PROFIT_GREEN_PCT = 3.0  # 浮盈≥3% → 绿色横幅机会提示

_SPOT_TIER_META = {
    0: ("🚨 立即执行（止损/清仓）", "#cf1322"),
    1: ("⚡ 今日必做（减仓/止盈）", "#fa8c16"),
    2: ("⚠️ 关注（设条件单）", "#d48806"),
    3: ("✅ 持有观察", "#389e0d"),
}


def _render_action_spotlight(holdings: dict) -> str:
    """
    渲染"今日行动看板": 置顶红/绿横幅 + 按紧急度分级的色块卡片。

    分级: 🚨立即执行(止损/清仓) → ⚡今日必做(减仓/止盈)
          → ⚠️关注(设条件单) → ✅持有观察
    色卡: 红=卖出止损 / 橙=减仓 / 绿=持有；卡片含建议动作、建议股数/比例、
    触发价/执行价、执行时限、理由一句话。异常返回空串(调用方降级)。
    """
    held = {c: h for c, h in holdings.items() if h.get("数量", 0) > 0}
    if not held:
        return ""

    html = '<h2>🎯 今日行动看板（按紧急度排序）</h2>'

    # ---- 1. 红色横幅: 浮亏≥8% 或已跌破止损线（不允许折叠进表格） ----
    reds = []
    for code, h in held.items():
        if h.get("占位成本"):
            continue
        pnl = h.get("盈亏比例", 0)
        if h.get("止损已破") or pnl <= -SPOTLIGHT_LOSS_RED_PCT:
            _tag = "已跌破止损线" if h.get("止损已破") else f"浮亏{pnl:.1f}%≥8%"
            reds.append(f'{h["名称"]}（{_tag}，止损价{h.get("止损价", 0):.2f}，立即执行止损纪律）')
    if reds:
        html += ('<div style="background:#fff1f0;border:2px solid #cf1322;border-radius:8px;'
                 'padding:12px 16px;margin:8px 0">'
                 '<div style="color:#cf1322;font-size:15px;font-weight:bold">'
                 '🚨 风险警告 — 以下标的必须优先处置：</div>'
                 f'<div style="color:#a8071a;font-size:13px;margin-top:5px;line-height:1.7">'
                 f'{"<br>".join(reds)}</div></div>')

    # ---- 2. 绿色横幅: 浮盈≥3% 止盈/加仓机会 ----
    greens = []
    for code, h in held.items():
        if h.get("占位成本"):
            continue
        pnl = h.get("盈亏比例", 0)
        if pnl >= SPOTLIGHT_PROFIT_GREEN_PCT:
            greens.append(f'{h["名称"]}（浮盈+{pnl:.1f}%）')
    if greens:
        html += ('<div style="background:#f6ffed;border:2px solid #389e0d;border-radius:8px;'
                 'padding:12px 16px;margin:8px 0">'
                 '<div style="color:#389e0d;font-size:15px;font-weight:bold">'
                 '✅ 机会提示 — 以下标的浮盈已达标(≥3%)，可考虑分批止盈锁利或评估加仓：</div>'
                 f'<div style="color:#237804;font-size:13px;margin-top:5px">'
                 f'{"、".join(greens)}</div></div>')

    # ---- 3. 分级色块卡片 ----
    groups = {0: [], 1: [], 2: [], 3: []}
    for code, h in held.items():
        action = h.get("操作建议", "") or "持有观察"
        tech = h.get("技术", {}) or {}
        comp = tech.get("composite", 0) if tech.get("valid") else 0
        pnl = h.get("盈亏比例", 0)
        shares = h.get("数量", 0)
        latest = h.get("最新", 0)
        stop = h.get("止损价", 0)
        reason = h.get("建议理由", "")

        if h.get("止损已破") or action == "建议止损" or "清仓" in action \
                or (not h.get("占位成本") and pnl <= -SPOTLIGHT_LOSS_RED_PCT):
            lvl = 0
            _detail = (f'建议动作: <b>卖出止损</b> | 建议股数: 全部{shares}股 | '
                       f'触发价: 止损价{stop:.2f}{"（已破）" if h.get("止损已破") else ""} | '
                       f'执行时限: 开盘即执行')
        elif "减仓" in action or "止盈" in action:
            lvl = 1
            _sell = max(int(shares * 0.5 / 100) * 100, min(100, shares))
            _tp1 = h.get("止盈目标1", 0)
            _exec = _tp1 if _tp1 > 0 else latest
            _detail = (f'建议动作: <b>{action}</b> | 建议股数: {_sell}股(约50%) | '
                       f'执行价: {_exec:.2f} | 执行时限: 14:50前')
        elif action == "谨慎持有" or (tech.get("valid") and comp < 45 and pnl < 0):
            lvl = 2
            _detail = (f'建议动作: <b>{action}</b> | 建议股数: 暂不动 | '
                       f'触发价: 条件单{stop:.2f}止损 | 执行时限: 今日设好条件单')
        else:
            lvl = 3
            _detail = (f'建议动作: <b>{action}</b> | 建议股数: 全部持有 | '
                       f'触发价: 关注止损价{stop:.2f} | 执行时限: 常规跟踪')

        groups[lvl].append((code, h, _detail, reason))

    _card_style = {
        0: ("#cf1322", "#fff1f0", "🚨"),
        1: ("#fa8c16", "#fff7e6", "⚡"),
        2: ("#d48806", "#fffbe6", "⚠️"),
        3: ("#389e0d", "#f6ffed", "✅"),
    }
    for lvl in (0, 1, 2, 3):
        items = groups[lvl]
        if not items:
            continue
        _title, _tcolor = _SPOT_TIER_META[lvl]
        html += (f'<div style="margin:14px 0 6px;font-size:14px;font-weight:bold;'
                 f'color:{_tcolor}">{_title}（{len(items)}只）</div>')
        for code, h, _detail, _reason in items:
            _color, _bg, _icon = _card_style[lvl]
            html += (f'<div style="border-left:6px solid {_color};background:{_bg};'
                     f'border-radius:8px;padding:10px 14px;margin:8px 0">'
                     f'<div style="font-size:14px;font-weight:bold;color:{_color}">'
                     f'{_icon} {code} {h["名称"]}</div>'
                     f'<div style="font-size:12px;color:#555;margin-top:4px">{_detail}</div>'
                     f'<div style="font-size:12px;color:#777;margin-top:3px">💡 {_reason}</div>'
                     f'</div>')

    html += ('<p style="color:#888;font-size:11px;margin:6px 0">'
             '⏰ 时限口径: 紧急标的开盘即执行，其余建议14:30-14:50执行避免早盘假突破；'
             '看板基于收盘数据生成，次一交易日适用。</p>')
    return html


# 主模式参数
# FIX: 修复止损比例与config不一致，统一使用config.INITIAL_STOP_LOSS_PCT(0.10)
STOP_LOSS_PCT = config.INITIAL_STOP_LOSS_PCT  # 固定止损: 统一使用config配置的止损比例
# FIX: 回落止盈从硬编码5%改为按stock_type从config.DRAWDOWN_STOP读取（龙头7%/成长6%/弹性5%）
_TYPE_TO_DRAWDOWN_KEY = {"龙头": "龙头稳健", "弹性": "高弹性", "成长": "成长赛道"}
REBOUND_FROM_LOW = 0.02    # 低点反弹2%触发

# FIX: ETF/基金代码前缀统一口径（原多处口径不一致: "5"开头过宽/缺518等，
# 导致仓位上限、龙虎榜跳过、两融跳过判断结果不一致）
# V5修复: 补入"512/515/516"（原缺512导致512010医药ETF未跳过龙虎榜/两融，报NoneType错）
_ETF_PREFIXES = ("159", "510", "511", "512", "513", "515", "516", "518", "560", "562", "588")

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
    # V3.3评分差异化改造: 旧公式(50+trend_score*8+momentum_score*6)仅有约35个可达整数分，
    # 导致大量持仓评分趋同。改为四维连续评分(趋势40%+动量25%+量能20%+位置15%)，
    # 各维先算0-100子分再加权，保留1位小数；保留50中性基准与0-100截断框架，
    # 消费方阈值(30/45/55/60/70等)全部不变。设计参考 trend_forecast._composite_score。
    _prev_close = df["close"].iloc[-2] if len(df) >= 2 else close

    # -- 趋势维(40%): 均线排列程度(40%) + MA20斜率幅度(35%) + 相对MA60偏离幅度(25%) --
    _align_parts = []
    for _fast, _slow in ((ma5, ma10), (ma10, ma20), (ma20, ma60)):
        if _slow and _slow > 0:
            _gap_pct = (_fast - _slow) / _slow * 100
            _align_parts.append(max(-1.0, min(1.0, _gap_pct / 1.5)))  # 相邻均线偏离±1.5%饱和
    _align_part = sum(_align_parts) / len(_align_parts) if _align_parts else 0.0
    if not pd.isna(ma20_slope) and ma20 and ma20 > 0:
        _slope_pct = ma20_slope / ma20 * 100  # MA20三日变化率(%)
        _slope_part = max(-1.0, min(1.0, _slope_pct / 2.0))  # ±2%饱和，幅度线性给分(替代只判符号)
    else:
        _slope_part = 0.0
    if ma60 and ma60 > 0:
        _dev60_pct = (close - ma60) / ma60 * 100
        _dev60_part = max(-1.0, min(1.0, _dev60_pct / 8.0))  # 距MA60偏离±8%饱和
    else:
        _dev60_part = 0.0
    trend_dim_score = 50 + (_align_part * 0.4 + _slope_part * 0.35 + _dev60_part * 0.25) * 50
    trend_dim_score = max(0.0, min(100.0, trend_dim_score))

    # -- 动量维(25%): RSI14连续映射(40%) + MACD柱强度(40%) + 5日动量(20%) --
    _rsi_part = max(-1.0, min(1.0, (rsi - 50) / 25))  # 以50为中性线性展开(替代三档)
    if not pd.isna(macd_hist) and close > 0:
        _hist_pct = macd_hist / close * 100  # 柱体相对价格标准化幅度
        _macd_part = max(-1.0, min(1.0, _hist_pct / 1.0))  # 红柱放大加分/绿柱放大减分(替代仅金叉布尔)
    else:
        _macd_part = 0.0
    _m5d_part = max(-1.0, min(1.0, momentum_5d / 8.0))  # 5日动量±8%饱和
    momentum_dim_score = 50 + (_rsi_part * 0.4 + _macd_part * 0.4 + _m5d_part * 0.2) * 50
    momentum_dim_score = max(0.0, min(100.0, momentum_dim_score))

    # -- 量能维(20%): 量比连续映射并结合涨跌方向(放量上涨加分/放量下跌减分) --
    # 数据缺失时给中性50分(中性值不改变总分语义)
    _vol_valid = (not pd.isna(vol_ma20)) and vol_ma20 > 0 and volume > 0
    if _vol_valid:
        if vol_ratio < 0.7:
            _vol_b = -min(1.0, (0.7 - vol_ratio) / 0.5) * 0.5  # 缩量轻微减分
        elif vol_ratio <= 1.0:
            _vol_b = (vol_ratio - 0.7) / 0.3 * 0.2
        elif vol_ratio <= 2.0:
            _vol_b = 0.2 + (vol_ratio - 1.0) * 0.6  # 温和放量1.0-2.0加分
        elif vol_ratio <= 3.0:
            _vol_b = 0.8 - (vol_ratio - 2.0) * 0.3  # 较大放量开始回落
        else:
            _vol_b = 0.5 - min(1.0, (vol_ratio - 3.0) / 2.0) * 0.5  # 极端放量>3适度回落
        volume_score = 50 + (_vol_b * 40 if close >= _prev_close else -_vol_b * 40)
        volume_score = max(0.0, min(100.0, volume_score))
    else:
        volume_score = 50.0

    # -- 位置维(15%): 布林%B位置 + 距MA20乖离(适度区间高分，深度超买/超卖减分) --
    _boll_width = boll_upper - boll_lower
    _pb = (close - boll_lower) / _boll_width if _boll_width > 0 else 0.5
    _pb_part = max(0.0, 1.0 - abs(_pb - 0.65) / 0.45)  # %B≈0.65最佳，超买(>1)/超卖(<0.2)减分
    if ma20 and ma20 > 0:
        _bias_pct = (close - ma20) / ma20 * 100
        _bias_part = max(0.0, 1.0 - abs(_bias_pct - 2.0) / 8.0)  # 乖离+2%附近最佳
    else:
        _bias_part = 0.5
    position_score = 20 + 45 * _pb_part + 35 * _bias_part
    position_score = max(0.0, min(100.0, position_score))

    composite = (trend_dim_score * 0.40 + momentum_dim_score * 0.25
                 + volume_score * 0.20 + position_score * 0.15)
    composite = round(max(0.0, min(100.0, composite)), 1)
    score_detail = f"趋{trend_dim_score:.0f} 动{momentum_dim_score:.0f} 量{volume_score:.0f} 位{position_score:.0f}"
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
        "momentum_score": momentum_score,
        "trend_dir": trend_dir,
        "trend_signals": trend_signals,
        "momentum_signals": momentum_signals,
        "composite": composite,
        # V3.3新增: 四维子分与简况(供展示/调试，消费方仍只读composite)
        "trend_dim_score": round(trend_dim_score, 1),
        "momentum_dim_score": round(momentum_dim_score, 1),
        "volume_score": round(volume_score, 1),
        "position_score": round(position_score, 1),
        "score_detail": score_detail,
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
        # V9.0: 优先从本地DB加载（带进程内缓存），避免冗余网络请求
        df = load_daily_data(code, days=200)
        if df.empty or len(df) < 30:
            # 降级：本地DB无数据时回退到baostock直连
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
def _patch_benchmark_tail(df):
    """V5修复: 腾讯日K补齐基准指数缺失的最近K线

    baostock日线存在滞后(干跑实测000300缺口3天)，若尾bar非当日，
    大盘状态检测会用陈旧数据误判(实测: 当日下跌却判震荡置信度5%，
    而同源本地DB门面判熊市65%)。用腾讯前复权日K补齐缺口bar，
    指标由 compute_indicators 重算；异常静默降级返回原df。
    """
    try:
        if df is None or df.empty:
            return df
        last_date = str(df["date"].iloc[-1])[:10]
        if last_date >= datetime.date.today().isoformat():
            return df
        import requests
        _bench_code = str(getattr(config, "BENCHMARK_INDEX", "000300")).replace(".", "")[-6:]
        _prefix = "sh" if _bench_code.startswith(("0", "5", "6", "9")) and _bench_code[:3] != "399" else "sz"
        _sym = f"{_prefix}{_bench_code}"
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={_sym},day,,,20,qfq")
        r = requests.get(url, timeout=8)
        node = r.json()["data"][_sym]
        bars = node.get("qfqday") or node.get("day") or []
        rows = []
        for b in bars:
            if not b or str(b[0]) <= last_date:
                continue
            rows.append({"date": str(b[0]), "open": float(b[1]),
                         "close": float(b[2]), "high": float(b[3]),
                         "low": float(b[4]),
                         "volume": float(b[5]) if len(b) > 5 else 0})
        if rows:
            import pandas as _pd
            df = _pd.concat([df, _pd.DataFrame(rows)], ignore_index=True)
            print(f"[大盘] 基准缺口{len(rows)}根已由腾讯日K补齐(最新{rows[-1]['date']})")
    except Exception as e:
        print(f"[大盘] 基准数据补丁失败({e})，沿用原数据")
    return df


benchmark_df = None
try:
    # V9.0: 优先从本地DB加载基准数据
    benchmark_df = load_daily_data(config.BENCHMARK_INDEX, days=200)
    if benchmark_df.empty:
        benchmark_start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m-%d")
        benchmark_df = fetch_stock_daily_baostock(config.BENCHMARK_INDEX, start_date=benchmark_start)
    if not benchmark_df.empty:
        benchmark_df = _patch_benchmark_tail(benchmark_df)
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

# 源③: data/scan_cache.json 动态扫描（校验时间戳≤24h，按当日涨幅取前N，失败降级跳过）
# V1.1扩面: 取前 SCAN_EXPAND_MAX 只（原10只→20只）
_expand = getattr(config, 'CANDIDATE_POOL_EXPAND_ENABLED', True)
_scan_expand_max = getattr(config, 'SCAN_EXPAND_MAX', 20) if _expand else 10
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
        for _sd in _scan_details[:_scan_expand_max]:
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
    max_total=getattr(config, "REPORT_CANDIDATE_MAX", 40) if _expand else 20,
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
            # V9.0: 优先从本地DB加载，降级到baostock
            df = load_daily_data(code_c, days=200)
            if df.empty or len(df) < 30:
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

    # FIX P0(2026-08-10): 占位成本识别 —— 券商成本异常(负/零)时holdings.json按规范以
    # ≤0.10极小值占位(如海康威视0.01)。若直接计算盈亏会得+数十万%荒谬值，并误触发
    # "浮盈>15%强制锁利"。占位语义=已全部回本: 盈亏比例归零、展示"已回本"特殊标注。
    _placeholder_cost = bool(0 < buy_price <= 0.10 and latest > 0 and latest / buy_price >= 50)
    pnl_pct = ((latest - buy_price) / buy_price * 100) if buy_price > 0 and latest > 0 else 0
    if _placeholder_cost:
        pnl_pct = 0.0
    pnl_amount = (latest - buy_price) * shares if buy_price > 0 and latest > 0 else 0
    market_value = latest * shares if latest > 0 else 0

    # FIX P2: 止损线只升不降（Ratchet原则）—— 浮亏时止损价不得低于原始硬止损(成本×90%)
    if buy_price > 0 and pnl_pct < 0 and stop_loss > 0:
        hard_floor = round(buy_price * (1 - STOP_LOSS_PCT), 2)
        if stop_loss < hard_floor:
            stop_loss = hard_floor
    # FIX P0: 止损价上限保护 —— 止损价永远不能高于现价（深度浮亏/除权后Ratchet会反转）
    # FIX P0b: 浮亏持仓例外 —— 成本×90%硬止损≥现价说明止损已破，应保留硬止损价并标记
    #          "止损已破"提示立即处置，绝不能降回现价×90%（否则止损线跟随价格下移，违背Ratchet）
    stop_broken = False
    if stop_loss > 0 and latest > 0 and stop_loss >= latest:
        if buy_price > 0 and pnl_pct < 0:
            stop_broken = True  # 保留硬止损价(成本×90%)，标记已破
        else:
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

    # 特殊规则: 浮盈>15%强制提示锁利（占位成本=已回本持仓不适用，避免虚假浮盈触发）
    if pnl_pct >= 15 and shares > 0 and not _placeholder_cost and "止盈" not in action and "清仓" not in action:
        action = "止盈减仓"
        action_reason = f"浮盈{pnl_pct:.1f}%已超15%，建议至少减仓1/2锁住利润，剩余设移动止盈"
        action_color = "#e74c3c"

    holdings[code] = {
        "名称": item["名称"],
        "赛道": item["赛道"],
        "买入日期": item.get("buy_date", ""),
        "最新": latest,
        "涨跌幅": change_pct,
        "最高": high,
        "最低": low,
        "振幅": amplitude,
        "换手率": turnover,
        "止损价": stop_loss,
        "止损已破": stop_broken,
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
        "占位成本": _placeholder_cost,
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
_LHB_SKIP_PREFIXES = _ETF_PREFIXES  # ETF/基金
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
            if code_h.startswith(_ETF_PREFIXES):
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

# --- 批2-S6: 组合风控评估（全流程仅构造一次，相关性结果供下方惩罚展示复用） ---
portfolio_risk_manager = None
portfolio_risk_report = None
if HAS_PORTFOLIO_RISK:
    try:
        _prm_holdings = {
            _c: {
                "shares": _h.get("数量", 0),
                "buy_price": _h.get("成本", 0),
                "current_price": _h.get("最新", 0),
                "sector": _h.get("赛道", "其他"),
            }
            for _c, _h in holdings.items() if _h.get("数量", 0) > 0
        }
        portfolio_risk_manager = PortfolioRiskManager(hist_dataframes, _prm_holdings)
        portfolio_risk_report = portfolio_risk_manager.full_risk_report()
        print(f"  组合风控: 评分{portfolio_risk_report.get('risk_score')}/100 "
              f"({portfolio_risk_report.get('overall_level')}) | "
              f"预警{len(portfolio_risk_report.get('alerts', []))}项")
    except Exception as _prme:
        portfolio_risk_manager = None
        portfolio_risk_report = None
        print(f"  组合风控: 计算失败({_prme})")

# --- 批2-S6: 相关性卖出惩罚（观察模式，仅展示标签，不参与排序） ---
corr_penalty_tags = {}
if getattr(config, "CORRELATION_PENALTY_ENABLED", False) and isinstance(portfolio_risk_report, dict):
    try:
        _corr_matrix = (portfolio_risk_report.get("correlation") or {}).get("matrix")
        if _corr_matrix is not None and not getattr(_corr_matrix, "empty", True):
            for _code, _h in holdings.items():
                if _h.get("数量", 0) <= 0 or _code not in _corr_matrix.columns:
                    continue
                # 与其余持仓的平均相关系数
                _others = [float(_v) for _c2, _v in _corr_matrix[_code].items() if _c2 != _code]
                _avg_corr = sum(_others) / len(_others) if _others else 0.0
                _tech_score = tech_analysis.get(_code, {}).get("composite", 50)
                _tags = []
                if _avg_corr > 0.7 and _tech_score < 50:
                    _tags.append(f"高相关+弱势(均相关{_avg_corr:.2f})")
                if _h.get("止损已破"):
                    _tags.append("止损已破")
                if _tags:
                    corr_penalty_tags[_code] = " | ".join(_tags)
            if corr_penalty_tags:
                print(f"  相关性惩罚(观察模式): {len(corr_penalty_tags)}只标的命中展示标签")
    except Exception as _cpbe:
        corr_penalty_tags = {}
        print(f"  相关性惩罚: 计算失败({_cpbe})")

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
        # FIX P0(2026-08-07): holdings为中文键(数量/成本/历史最高)，而trend_forecast读英文键
        # (buy_price/shares/highest)，此前错位导致盈亏恒为0、深亏相关建议分支永不触发
        if _holding_info.get("数量", 0) > 0:
            _holding_en = {
                "shares": _holding_info.get("数量", 0),
                "buy_price": _holding_info.get("成本", 0),
                "highest": _holding_info.get("历史最高", 0),
            }
        else:
            _holding_en = {}
        _result = forecaster.analyze_stock(_code, _df, _holding_en)
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
body{{font-family:'Microsoft YaHei',Arial,sans-serif;padding:0;background:#f0f2f5;font-size:13px;line-height:1.6}}
.container{{max-width:960px;margin:0 auto}}
.header{{background:#1a1a2e;color:#fff;padding:20px 24px}}
.header h1{{margin:0;font-size:20px;color:#fff}}
.header .sub{{font-size:12px;color:#b0b8c8;margin-top:6px}}
.content{{background:#fff;padding:20px 24px}}
h2{{color:#2c3e50;font-size:15px;margin:24px 0 12px;border-left:4px solid #3498db;padding-left:10px}}
h3{{font-size:14px;color:#333;margin:12px 0 8px}}
.metric-table{{width:100%;border-spacing:8px;border-collapse:separate}}
.metric-cell{{background:#f8f9fa;border-radius:6px;padding:12px;text-align:center;width:25%}}
.metric-value{{font-size:18px;font-weight:bold}}
.metric-label{{font-size:11px;color:#7f8c8d;margin-top:4px}}
.up{{color:#e74c3c}} .down{{color:#27ae60}}
.pnl-danger{{color:#cf1322;font-weight:bold;font-size:14px}}
.stop-loss-warn{{color:#cf1322;font-weight:bold;background:#fff1f0}}
table{{width:100%;border-collapse:collapse;margin:10px 0;font-size:12px}}
th{{background:#34495e;color:#fff;padding:8px 6px;text-align:center;font-size:12px}}
td{{padding:7px 6px;border-bottom:1px solid #eee;text-align:center}}
tr:nth-child(even){{background:#f8f9fa}}
.alert{{padding:12px;border-radius:4px;margin:10px 0;font-size:12px}}
.alert-danger{{background:#ffebee;border-left:4px solid #e74c3c}}
.alert-success{{background:#e8f5e9;border-left:4px solid #4caf50}}
.alert-warning{{background:#fff3cd;border-left:4px solid #ffc107}}
.alert-info{{background:#e3f2fd;border-left:4px solid #2196f3}}
.stock-card{{border:1px solid #e0e0e0;border-radius:6px;margin:15px 0;padding:0;overflow:hidden}}
.stock-card-header{{padding:12px 16px;border-bottom:1px solid #eee;background:#fafafa}}
.stock-card-body{{padding:14px 16px}}
.tag{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold;color:#fff;margin:2px}}
.tag-hold{{background:#2196f3}} .tag-reduce{{background:#ff9800}} .tag-stop{{background:#f44336}} .tag-add{{background:#4caf50}}
.score-bar{{height:8px;border-radius:4px;background:#eee;margin:4px 0;position:relative}}
.score-fill{{height:100%;border-radius:4px;position:absolute;left:0;top:0}}
.badge{{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;color:#fff;margin-left:5px}}
.badge-rt{{background:#4caf50}} .badge-stale{{background:#ff9800}}
.footer{{text-align:center;color:#999;font-size:11px;margin-top:20px;padding-top:12px;border-top:1px solid #eee;line-height:1.8}}
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
.explain-compact{{font-size:11px;color:#888;margin:4px 0;padding:4px 8px;background:#fafafa;border-radius:4px}}
@media screen and (max-width:640px){{
.container{{max-width:100%}}
.content{{padding:12px}}
table{{font-size:11px}}
.header h1{{font-size:17px}}
.stock-card{{margin:10px 0}}
.metric-cell{{padding:6px 4px;font-size:11px}}
}}
</style></head><body><div class="container">
<div class="header">
<h1>📊 持仓综合分析报告 <span class="mode-badge">技术面+条件单</span></h1>
<div class="sub">日期: {today} | 行情: {quote_time_str} ({data_source}) | 止损规则: 成本×{1-STOP_LOSS_PCT:.0%}硬止损(Ratchet只升不降) | 持仓周期: 3天-4周波段</div>
</div>
<div class="content">
"""

# ---- 🌡️ 市场情绪与资金面面板（增强1: 市场全局研判置顶 + 隔夜外盘前瞻） ----
if HAS_MARKET_PULSE:
    try:
        _pulse_data = get_market_pulse()
        html += render_pulse_html(_pulse_data)
        _pd_deg = _pulse_data.get("degraded", [])
        print(f"  市场情绪面板: {_pulse_data.get('level', '未知')}"
              f"({_pulse_data.get('temperature')}分)" +
              (f" | 数据源降级{len(_pd_deg)}项" if _pd_deg else ""))
    except Exception as _ple:
        print(f"  市场情绪面板: 失败({_ple})")
# 隔夜外盘前瞻（并入市场情绪板块，不再独立h2标题）
try:
    if overnight_data.get("available"):
        html += '<div style="margin-top:8px;padding-top:8px;border-top:1px dashed #ddd">'
        html += _overnight.get_summary_html()
        html += '</div>'
except Exception:
    pass  # 外盘板块渲染失败不影响报告

# ---- 🛡️ 风控熔断状态横幅（增强3: 熔断可视化置顶） ----
try:
    _cb_banner = _render_circuit_breaker_banner()
    if _cb_banner:
        html += _cb_banner
except Exception as _cbe:
    print(f"  熔断状态横幅: 失败({_cbe})")

# ---- 🚨 交易行为自诊断警示横幅（置顶显著展示） ----
behavior_result = None
if HAS_TRADE_BEHAVIOR:
    try:
        _tb_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trades_today.json')
        if os.path.exists(_tb_file):
            _tb_trades, _tb_date = load_trades_from_json(_tb_file)
            if _tb_trades and (not _tb_date or _tb_date == today):
                behavior_result = analyze_trade_behavior(
                    _tb_trades, total_capital=config.TOTAL_CAPITAL, date=_tb_date or today)
                _tb_banner = render_behavior_alert_html(behavior_result)
                if _tb_banner:
                    html += _tb_banner
                    print(f"  交易行为诊断: {behavior_result['headline']}")
    except Exception as _tbe:
        print(f"  交易行为诊断: 失败({_tbe})")

# ---- 🎯 今日行动看板（V5问题三: 风险/机会横幅+分级色卡，置于情绪面板后、明细表前） ----
try:
    _spot_html = _render_action_spotlight(holdings)
    if _spot_html:
        html += _spot_html
        print(f"  今日行动看板: 已生成")
except Exception as _spe:
    print(f"  今日行动看板: 渲染失败({_spe})，已降级跳过")

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
    # FIX P1(2026-08-07): 单只上限收口config统一阈值（原硬编码20%与scheduler强制减仓线15%打架），
    # 并按个股/ETF分别适用不同上限（与scheduler口径一致）
    _limit_stock_pct = getattr(config, 'MAX_SINGLE_STOCK_RATIO', 0.15) * 100
    _limit_etf_pct = getattr(config, 'MAX_SINGLE_ETF_RATIO', 0.20) * 100
    for code_w, info_w in sorted_mvs:
        _limit_pct = _limit_etf_pct if code_w.startswith(_ETF_PREFIXES) else _limit_stock_pct
        if info_w['pct'] > _limit_pct:
            html += f'<div class="alert alert-danger">🚨 <b>{info_w["name"]}仓位{info_w["pct"]:.1f}%</b>，超出单只上限{_limit_pct:.0f}%！建议分批减仓。</div>'
    # 集中度预警：前2大持仓
    if len(sorted_mvs) >= 2:
        top2_pct = sorted_mvs[0][1]['pct'] + sorted_mvs[1][1]['pct']
        top2_names = f"{sorted_mvs[0][1]['name']}({sorted_mvs[0][1]['pct']:.1f}%) + {sorted_mvs[1][1]['name']}({sorted_mvs[1][1]['pct']:.1f}%)"
        if top2_pct > 60:
            html += f'<div class="alert alert-warning">⚡ 持仓集中度: {top2_names} = <b>{top2_pct:.1f}%</b>集中在2只标的，风险较高。建议单只不超30%。</div>'
    html += f'<div class="alert alert-info">💰 账户总市值: ¥{total_mv:,.2f} | 持仓{len(holdings)}只</div>'
except Exception as e:
    html += f'<div class="alert alert-warning">仓位数据读取异常: {e}</div>'

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
        html += f'<div style="background:#52c41a;width:{_rp_bar_w:.1f}%;height:14px;border-radius:4px"></div></div>'
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
html += f'<div class="alert alert-info">📡 行情源: {data_source} | 时间: {quote_time_str or now} | 止损规则: 浮亏持仓按成本×{1-STOP_LOSS_PCT:.0%}硬止损(Ratchet只升不降)，浮盈持仓按最新价×{1-STOP_LOSS_PCT:.0%} | 止盈基于压力位/ATR推算</div>'

html += '<table><tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>最新价</th><th>盈亏%</th><th>仓位%</th><th>技术评分</th><th>趋势</th><th>止损价</th><th>止盈目标</th><th>操作建议</th></tr>'
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

    # 盈亏颜色（A股惯例红涨绿跌）；占位成本持仓特殊标注"已回本"
    pnl = h.get("盈亏比例", 0)
    if h.get("占位成本"):
        pnl_str = "已回本"
        pnl_color = "#e74c3c"
    else:
        pnl_color = "#e74c3c" if pnl >= 0 else "#27ae60"
        pnl_str = f"{pnl:+.1f}%" if h.get("成本", 0) > 0 else "-"

    # 止盈显示
    tp1 = h.get("止盈目标1", 0)
    tp_str = f"{tp1:.3f}" if tp1 > 0 else "-"

    # 操作建议
    action = h.get("操作建议", "-")
    action_color = h.get("建议颜色", "#333")

    # V10.2: 浮亏>=8%整行红色背景+pnl-danger；|盈亏|>5%放大字号
    _row_bg = 'background:#fff1f0;' if pnl <= -8 else ''
    html += f'<tr style="{_row_bg}"><td>{code}</td><td><b>{h["名称"]}</b></td>'
    html += f'<td>{h.get("数量", 0)}</td>'
    html += f'<td>{h.get("成本", 0):.3f}</td>' if h.get("成本", 0) > 0 else '<td>-</td>'
    html += f'<td><b>{h["最新"]:.3f}</b></td>'
    # V10.2: 浮亏>=8%整行红色背景+pnl-danger；|盈亏|>5%放大字号
    if pnl <= -8:
        html += f'<td class="pnl-danger" style="font-size:16px">{pnl_str}</td>'
    else:
        _pnl_font_size = f'font-size:16px;' if abs(pnl) > 5 else ''
        html += f'<td style="color:{pnl_color};font-weight:bold;{_pnl_font_size}">{pnl_str}</td>'
    # V4.2(P1-4): 单票仓位敞口列 —— 超上限标红(与scheduler超限减仓单口径一致:
    # 个股MAX_SINGLE_STOCK_RATIO默认15%，ETF MAX_SINGLE_ETF_RATIO默认20%)
    _pos_ratio = h.get("市值", 0) / _CAPITAL if _CAPITAL > 0 else 0
    _is_etf = code.startswith(_ETF_PREFIXES)
    _pos_cap = getattr(config, "MAX_SINGLE_ETF_RATIO", 0.20) if _is_etf else getattr(config, "MAX_SINGLE_STOCK_RATIO", 0.15)
    _pos_style = "color:#cf1322;font-weight:bold" if _pos_ratio > _pos_cap else "color:#555"
    _pos_suffix = " ⚠️超限" if _pos_ratio > _pos_cap else ""
    html += f'<td style="{_pos_style}">{_pos_ratio*100:.1f}%{_pos_suffix}</td>'
    # V3.3: 评分保留1位小数展示(凸显差异化)，四维简况放title悬浮提示；颜色分档沿用60/40不变
    _score_title = tech.get("score_detail", "") if tech.get("valid") else ""
    html += f'<td style="color:{score_color};font-weight:bold" title="{_score_title}">{comp:.1f}</td>'
    html += f'<td>{trend_dir}</td>'
    _stop_str = f'{h["止损价"]:.3f}' + (' ⚠️已破' if h.get("止损已破") else '')
    # V10.2: 止损价逼近(距止损<3%)用stop-loss-warn红底白字
    _sl_dist_pct = abs(h["最新"] - h['止损价']) / h["最新"] * 100 if h["最新"] > 0 else 999
    _sl_style = 'class="stop-loss-warn"' if _sl_dist_pct < 3 else 'style="color:#e74c3c;font-weight:bold"'
    html += f'<td {_sl_style}>{_stop_str}</td>'
    html += f'<td style="color:#1976d2;font-weight:bold">{tp_str}</td>'
    html += f'<td style="color:{action_color};font-weight:bold">{action}</td></tr>'
html += '</table>'

# ---- V4.2(P1-4): 板块集中度提示行(赛道经粗粒度归一化，与sector_divergence同源) ----
try:
    _conc_map = get_coarse_sector_map() if HAS_REPORT_EXT else {}
    _sector_mv = {}
    for _c, _h in holdings.items():
        if _h.get("数量", 0) <= 0:
            continue
        _sec = _conc_map.get(_h.get("赛道", "未知"), _h.get("赛道", "未知"))
        _sector_mv[_sec] = _sector_mv.get(_sec, 0) + _h.get("市值", 0)
    if _sector_mv and current_mv > 0:
        _top3 = sorted(_sector_mv.items(), key=lambda x: -x[1])[:3]
        _top3_pct = sum(v for _, v in _top3) / current_mv
        _top3_str = "、".join(f"{k} {v/current_mv*100:.0f}%" for k, v in _top3)
        _conc_color = "#cf1322" if _top3_pct > 0.60 else ("#ad6800" if _top3_pct > 0.45 else "#389e0d")
        _conc_warn = " ⚠️集中度偏高，建议分散" if _top3_pct > 0.60 else ""
        html += f'<p style="color:{_conc_color};margin:6px 0;font-size:12px">🧺 板块集中度: 前三 {_top3_str}，合计 {_top3_pct*100:.0f}%（建议≤60%）{_conc_warn}</p>'
except Exception:
    pass  # 集中度展示失败不影响报告主体

# ---- 批2-S6: 组合风控行动项区块（紧跟总览表之后；开关关闭/异常时跳过，主报告照常） ----
if HAS_PORTFOLIO_RISK and getattr(config, "PORTFOLIO_RISK_ACTIONS_ENABLED", True):
    try:
        if isinstance(portfolio_risk_report, dict) and portfolio_risk_report:
            _risk_section_html = build_risk_action_section(
                portfolio_risk_report, portfolio_risk_report.get("rebalance"))
            if _risk_section_html:
                html += _risk_section_html
    except Exception as _rae:
        print(f"  组合风控区块: 渲染失败({_rae})，已跳过")

# ---- 资金仓位智能规划板块（资金仪表盘/集中度/调仓清单/买点可行性/禁买联动） ----
if HAS_CAPITAL_PLANNER and getattr(config, "CAPITAL_PLANNER_ENABLED", True):
    try:
        _capital_section_html = build_capital_plan_section(portfolio_risk_report)
        if _capital_section_html:
            html += _capital_section_html
            print("  资金仓位智能规划: 板块已生成")
    except Exception as _cpe:
        print(f"  资金仓位智能规划: 渲染失败({_cpe})，已跳过")

# ---- V2.5: 今日操作回顾 ----
html += '<h2>一-2、今日操作回顾</h2>'
try:
    _trades_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trades_today.json')
    if os.path.exists(_trades_file):
        with open(_trades_file, 'r', encoding='utf-8') as _tf:
            _trades_data = json.load(_tf)
        # FIX(2026-08-12): 旧日期成交记录不得以"今日操作回顾"名义展示，
        # 日期≠今日时整段降级提示（与交易行为诊断的 _tb_date==today 口径对齐）
        _trades_date = _trades_data.get('date', '')
        if _trades_date and _trades_date != today:
            html += f'<p style="color:#999">今日暂无成交记录（最近一次成交: {_trades_date}）</p>'
        else:
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
        # 频繁操作诊断摘要（跟随操作回顾展示）
        if behavior_result and behavior_result.get("total_trades", 0) > 0:
            _sev_color = {"severe": "#cf1322", "warning": "#ad6800", "ok": "#389e0d"}[behavior_result["severity"]]
            html += f'<p style="color:{_sev_color};font-weight:bold">🚨 行为诊断: {behavior_result["headline"]}</p>'
    else:
        html += '<p style="color:#999">无委托数据文件(trades_today.json)</p>'
except Exception as e:
    html += f'<p style="color:#999">委托数据读取失败: {e}</p>'

# ---- 批2-S6: 执行确认清单（合并入操作回顾末尾，持仓快照diff，有变化才展示） ----
if HAS_EXEC_CLOSURE:
    try:
        # FIX P0(2026-08-10): shares>0防御过滤，与总览表标的范围保持一致
        _ec_holdings = {
            _c: {
                "name": _h.get("名称", _c),
                "shares": _h.get("数量", 0),
                "buy_price": _h.get("成本", 0),
                "stop_loss": _h.get("止损价", 0),
            }
            for _c, _h in holdings.items() if _h.get("数量", 0) > 0
        }
        _ec_result = diff_and_confirm(_ec_holdings)
        if _ec_result.get("has_changes") and _ec_result.get("html"):
            html += '<h3 style="color:#389e0d;border-left:4px solid #4caf50;padding-left:8px;margin-top:16px">✅ 执行确认清单（持仓变动核对）</h3>'
            html += _ec_result["html"]
    except Exception as _ece:
        print(f"  执行确认清单: 生成失败({_ece})，已跳过")

# ---- 📋 今日操作执行清单（前移: 紧急操作指令优先展示） ----
html += '<h2>📋 今日操作执行清单</h2>'

# ---- V4.2(P1-4): 组合回撤警戒条（daily_nav表近20日最大回撤，仅提示不自动执行） ----
try:
    import sqlite3 as _sqlite3_dd
    _dd_db = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "trading_system", "data", "trade_journal.db")
    _dd_html = ""
    if os.path.exists(_dd_db):
        _conn_dd = _sqlite3_dd.connect(_dd_db)
        _dd_rows = _conn_dd.execute(
            "SELECT drawdown FROM daily_nav ORDER BY date DESC LIMIT 20").fetchall()
        _conn_dd.close()
        _dds = [abs(float(r[0])) for r in _dd_rows if r[0] is not None]
        if len(_dds) >= 5:
            _max_dd = max(_dds)
            _dd_color = "#cf1322" if _max_dd > 0.08 else ("#ad6800" if _max_dd > 0.05 else "#389e0d")
            _dd_tag = " ⚠️已破警戒线，优先降仓防守" if _max_dd > 0.08 else ""
            _dd_html = (f'<div style="color:{_dd_color};margin:6px 0;font-size:12px">'
                        f'📉 组合回撤: 近20日最大回撤 {_max_dd*100:.1f}%（警戒线8%）{_dd_tag}</div>')
        else:
            _dd_html = '<div style="color:#888;margin:6px 0;font-size:11px">📉 组合回撤: 净值数据积累中（≥5日后展示）</div>'
    html += _dd_html
except Exception:
    pass  # 回撤展示失败不影响执行清单主体

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
    # V4.2(P2-8): 市场阶段标识（观察模式，仅展示不参与仓位计算；历史序列不足60条时自降级为积累进度）
    try:
        from strategy.market_regime import estimate_market_phase
        _phase_info = estimate_market_phase(regime_result)
        if _phase_info.get("available") and _phase_info.get("phase"):
            html += (f'<br><b>市场阶段:</b> 🔬{_phase_info["phase"]}'
                     f'（已持续{_phase_info["days"]}天 · 观察模式，不参与仓位计算）'
                     f'<br><span style="color:#888;font-size:11px">依据: {_phase_info["note"]}</span>')
        else:
            html += (f'<br><b>市场阶段:</b> <span style="color:#888">🔬 数据积累中'
                     f'（{_phase_info.get("accumulated_days", 0)}/{_phase_info.get("required_days", 60)}天，达标后展示初期/中期/末期）</span>')
    except Exception:
        pass  # 阶段展示失败不影响大盘状态卡片主体
    # V4.2(P2-9): 分环境持仓处理原则（仓位区间仍为既有POSITION_MAP口径，仅展示）
    _env_principle = {
        "BULL": "持股为主+止损Ratchet上移锁盈，破位才卖；不追高加仓",
        "RANGE": "高抛低吸，单票≤10%；反弹至压力位优先减浮盈仓",
        "BEAR": "降仓防守，严格止损；不抄底不补仓，等待企稳信号再入场",
    }.get(_state, "")
    if _env_principle:
        html += f'<br><b>当前环境持仓原则:</b> {_env_principle}'
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
        # FIX: 剩余减仓金额不足1手时原逻辑强制卖100股，可能大幅超出目标金额
        # （如剩余目标2000元、股价50元→强卖100股=5000元）；
        # 现改为: 1手金额≤剩余目标2倍时按最小单位卖100股，否则目标已近似达成直接结束
        _need = _remaining_reduce / _price
        _sell_shares = int(min(_need, _shares) / 100) * 100
        if _sell_shares <= 0:
            if _price * 100 <= _remaining_reduce * 2:
                _sell_shares = min(100, _shares)
            else:
                break
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
        # 批2-S6: 相关性惩罚标签（观察模式，开关关闭时字典为空→无任何展示变化）
        _corr_tag = corr_penalty_tags.get(_code, "")
        _corr_tag_html = (f'<br><span style="color:#cf222e;font-weight:bold;font-size:11px">'
                          f'🔗 {_corr_tag}</span>') if _corr_tag else ''
        html += f'''<tr><td style="padding:6px;border:1px solid #f0f0f0;text-align:center">{_idx}</td>
<td style="padding:6px;border:1px solid #f0f0f0">{_code} {_name}<br><span style="color:#888">浮盈{_pnl:+.1f}%</span>{_corr_tag_html}</td>
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

# ---- V2.8: 回调加仓准备 —— 大盘状态推导 + 风控入参持仓字典 ----
# 大盘状态: 由沪深300最近一日涨跌映射（>0.5%→up, >-0.5%→neutral, 否则weak），盘后market_drop_pct传0.0
_market_state = "neutral"
try:
    if benchmark_df is not None and len(benchmark_df) >= 2:
        _b_last = float(benchmark_df["close"].iloc[-1])
        _b_prev = float(benchmark_df["close"].iloc[-2])
        if _b_prev > 0:
            _b_pct = _b_last / _b_prev - 1
            _market_state = "up" if _b_pct > 0.005 else ("neutral" if _b_pct > -0.005 else "weak")
except Exception:
    _market_state = "neutral"
# check_pullback_add_risk 入参口径: {code: {shares, buy_price, current_price}}
# FIX P0(2026-08-10): shares>0防御过滤，已清仓标的不得参与回调加仓风控
_risk_holdings = {
    _c: {"shares": _h.get("数量", 0), "buy_price": _h.get("成本", 0), "current_price": _h.get("最新", 0)}
    for _c, _h in holdings.items() if _h.get("数量", 0) > 0
}

# ---- 逐只详细分析 ----
html += '<h2>二、逐只技术分析 + 操作建议 + 条件单</h2>'

# V10.1: 操盘密码预计算（合并入逐只分析卡片，不再独立板块）
_caopan_results_map = {}
try:
    _caopan_engine = CaopanEngine()
    for _cp_code, _cp_h in holdings.items():
        _cp_df = hist_dataframes.get(_cp_code)
        if _cp_df is not None and len(_cp_df) >= 60:
            _cp_r = _caopan_engine.analyze(_cp_df, code=_cp_code, name=_cp_h["名称"])
            if "error" not in _cp_r:
                _caopan_results_map[_cp_code] = _cp_r
    # 趋势降级预警（提前到逐只分析前）
    _cp_downgrades = [v for v in _caopan_results_map.values() if v.get('trend_level', 3) <= 2]
    if _cp_downgrades:
        html += '<div class="alert alert-danger">🚨 趋势降级预警: ' + ', '.join([f'{v["name"]}({v["trend_desc"]})' for v in _cp_downgrades]) + ' → 建议清仓/禁止加仓</div>'
    # 生成图表
    if _caopan_results_map:
        _caopan_dir = os.path.join(config.PROJECT_ROOT, 'output', 'caopan')
        os.makedirs(_caopan_dir, exist_ok=True)
        for _cp_cr in _caopan_results_map.values():
            _cp_chart_path = os.path.join(_caopan_dir, f'caopan_{_cp_cr["code"]}_{today.replace("-","")}.html')
            generate_caopan_chart(_cp_cr, output_path=_cp_chart_path)
except Exception as _cp_e:
    print(f"  操盘密码预计算: 失败({_cp_e})")

# 增强4: 逻辑止损所需的弱势板块清单（复用赛道轮动缓存，失败降级为空）
_weak_sectors = []
try:
    _ws_path = os.path.join(config.PROJECT_ROOT, "data", "sector_rotation_cache.json")
    if os.path.exists(_ws_path):
        with open(_ws_path, "r", encoding="utf-8") as _wsf:
            _weak_sectors = json.load(_wsf).get("weak", []) or []
except Exception:
    _weak_sectors = []

for code, h in holdings.items():
    tech = h["技术"]
    latest = h["最新"]
    chg_class = "up" if h['涨跌幅'] >= 0 else "down"

    # V4.2(P1-5): 条件单"距触发价还差X%"辅助函数 —— 回答"为什么没触发"
    def _dist_str(trigger):
        try:
            trigger = float(trigger or 0)
            if trigger <= 0 or latest <= 0:
                return ""
            _pct = abs(latest - trigger) / latest * 100
            _dir = "还跌" if trigger < latest else "还涨"
            return f'<br><span style="font-size:11px;color:#888;font-weight:normal">现价{latest:.3f}，{_dir}{_pct:.1f}%触发</span>'
        except Exception:
            return ""

    # V2.5: 已清仓标的跳过技术分析
    if h.get("数量", 0) == 0:
        html += f'<div class="stock-card"><div class="stock-card-header">{code} {h["名称"]} <span style="color:#999">({h["赛道"]})</span> <span class="tag tag-stop">已清仓</span></div>'
        html += f'<div class="stock-card-body"><p style="color:#999">今日已清仓，不再生成技术分析。最新价: {latest:.3f}</p></div></div>'
        continue

    if not tech.get("valid"):
        html += f'<div class="stock-card"><div class="stock-card-header">{code} {h["名称"]} <span style="color:#999">({h["赛道"]})</span></div>'
        html += f'<div class="stock-card-body"><p style="color:#999">技术分析数据不足: {tech.get("error", "未知")}</p>'
        html += f'<p>止损价: <b style="color:#e74c3c">{h["止损价"]:.3f}</b> | 止盈目标: <b style="color:#1976d2">{h.get("止盈目标1", 0):.3f}</b></p></div></div>'
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

    # ---- V10.0: K线形态信号提取（从forecast_results中获取）----
    _pattern_html = ""
    try:
        _fc = forecast_results.get(code, {})
        _pat = _fc.get("pattern", {})
        if _pat and _pat.get("patterns"):
            _pat_signal = _pat.get("signal", "中性")
            _pat_color = {"看涨": "#e74c3c", "看跌": "#4caf50", "中性": "#999"}.get(_pat_signal, "#999")
            _pat_bull = _pat.get("bullish_count", 0)
            _pat_bear = _pat.get("bearish_count", 0)
            _pat_top = _pat.get("top_patterns", [])
            _pat_tags = []
            for _tp in _pat_top[:4]:
                _tp_name = _tp.get("pattern", "")
                _tp_conf = _tp.get("confidence", 0)
                _tp_type = _tp.get("type", "")
                _tp_loc = "✓位置有效" if _tp.get("location_valid") else "位置存疑"
                _tp_color = "#e74c3c" if _tp_type == "bullish" else "#4caf50"
                _pat_tags.append(f'<span style="display:inline-block;background:{_tp_color}15;color:{_tp_color};border:1px solid {_tp_color}40;border-radius:3px;padding:1px 6px;margin:2px;font-size:11px">{_tp_name}({_tp_loc} {_tp_conf:.0%})</span>')
            _pattern_html = f'''<div style="margin:6px 0;font-size:12px;background:#fafafa;border:1px solid #eee;border-radius:4px;padding:8px 10px">
<b>📊 K线形态:</b> <span style="color:{_pat_color};font-weight:bold">{_pat_signal}</span>
<span style="font-size:11px;color:#888;margin-left:6px">(看涨{_pat_bull}个 / 看跌{_pat_bear}个)</span><br>
<div style="margin-top:4px">{"".join(_pat_tags)}</div>
</div>'''
    except Exception:
        pass

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

    # ---- V2.8: 加仓计划分流（回调加仓优先，突破加仓带守卫；修复与②止盈单同价冲突）----
    _ma20_val = tech.get("ma20", latest)
    _resistances = tech.get("resistances", [])
    # 回调加仓风控预检（复用已加载的 hist_dataframes，零额外网络请求；异常降级为不通过）
    _pullback_risk = {"pass": False, "reason": "回调加仓检测未执行", "add_shares": 0}
    try:
        if HAS_PULLBACK_RISK and code in hist_dataframes:
            _pullback_risk = check_pullback_add_risk(
                code, _risk_holdings, hist_dataframes[code],
                market_state=_market_state, market_drop_pct=0.0)
    except Exception as _pb_e:
        _pullback_risk = {"pass": False, "reason": f"回调加仓检测异常({_pb_e})", "add_shares": 0}

    add_card_html = ""  # 加仓计划卡片（不满足条件时不渲染）
    if _pullback_risk.get("pass"):
        # ---- 分支A: 回调加仓（风控通过，逆势加仓企稳区间）----
        # 触发价口径与 intraday_decision.py 回调加仓一致: 现价×0.999，且不高于MA20企稳价
        add_trigger_price = round(latest * 0.999, 3)
        if isinstance(_ma20_val, (int, float)) and _ma20_val > 0:
            add_trigger_price = round(min(latest * 0.999, float(_ma20_val)), 3)
        # 互斥校验: 与②止盈价相等时再偏移1%（回调价低于现价，通常不会冲突）
        if tp1 > 0 and add_trigger_price == tp1:
            add_trigger_price = round(add_trigger_price * 1.01, 3)
        # 加仓股数: 风控返回口径=现有股数×50%取整百（下限100股）
        add_shares = int(_pullback_risk.get("add_shares", 0) or 0)
        if add_shares <= 0:
            add_shares = max(int(shares * 0.5 / 100) * 100, 100)
        add_amount = add_shares * add_trigger_price
        add_stop_loss = float(_pullback_risk.get("stop_loss", 0) or 0)
        _sig_type = _pullback_risk.get("signal_type", "pullback_ma20")
        _sig_desc = "超跌反弹(RSI<30且触及布林下轨)" if _sig_type == "oversold_rebound" else "缩量回踩MA20企稳"
        _sl_basis = "现价×95%" if _sig_type == "oversold_rebound" else "MA20支撑×97%"
        add_card_html = f"""
<div style="background:#eef6ff;border:1px solid #b3d9ff;border-radius:6px;padding:10px 14px;margin:10px 0">
<b style="color:#1565c0;font-size:13px">[ADD] 加仓计划（回调加仓 · 风控通过）</b>
<table style="margin-top:6px;font-size:12px">
<tr><th style="width:100px;background:#e3f2fd">项目</th><th style="background:#e3f2fd">具体条件</th></tr>
<tr><td><b>加仓触发价</b></td><td style="color:#1565c0;font-weight:bold">{add_trigger_price:.3f} 元（现价×0.999/MA20企稳区间，回调{_pullback_risk.get('pullback_pct', 0):.1%}企稳）</td></tr>
<tr><td><b>信号类型</b></td><td>{_sig_desc} | {_pullback_risk.get('reason', '')}</td></tr>
<tr><td><b>加仓仓位</b></td><td>按现有持仓50%加仓 | 加仓股数: <b>{add_shares}</b>股 / {add_amount:,.0f}元</td></tr>
<tr><td><b>加仓后止损</b></td><td style="color:#e74c3c">{add_stop_loss:.3f} 元（{_sl_basis}，跌破即止损新加部分）</td></tr>
<tr><td><b>与②止盈单关系</b></td><td style="font-size:11px">回调企稳区间加仓，与②止盈单不冲突（止盈价位于现价上方）</td></tr>
</table>
</div>"""
    else:
        # ---- 分支B: 突破型加仓（触发价=压力位×1.01突破确认，与止盈价拉开距离）----
        # 渲染守卫: 仅评分>=60 且 操作建议不含减仓/止损/清仓 时才允许加仓
        _no_reduce = not any(k in action for k in ("减仓", "止损", "清仓"))
        if comp >= 60 and _no_reduce:
            if _resistances and _resistances[0][1] > latest * 1.01:
                add_trigger_price = round(_resistances[0][1] * 1.01, 3)
                _add_trigger_desc = f"突破{_resistances[0][0]}压力位×1.01确认"
            else:
                # 无合适压力位: 现价+5%退化逻辑，同样乘1.01突破确认偏移
                add_trigger_price = round(latest * 1.05 * 1.01, 3)
                _add_trigger_desc = "现价+5%再×1.01突破确认"
            # 互斥校验: 加仓触发价不得与②止盈单触发价相同（相等则再偏移1%）
            if tp1 > 0 and add_trigger_price == tp1:
                add_trigger_price = round(add_trigger_price * 1.01, 3)
            # 加仓股数: 现有持仓的30%（取整百）
            add_shares = max(int(shares * 0.3 / 100) * 100, 100)
            add_amount = add_shares * add_trigger_price
            # 加仓前置条件
            _add_conds = ["技术评分>=60", "DK D点确认或量比>1.5", "板块指数同步走强"]
            add_preconditions = " + ".join(_add_conds)
            # 加仓后综合止损
            _avg_cost_after = (cost_price * shares + add_trigger_price * add_shares) / (shares + add_shares) if (shares + add_shares) > 0 else cost_price
            add_stop_loss = round(_avg_cost_after * (1 - STOP_LOSS_PCT), 3)
            add_card_html = f"""
<div style="background:#eef6ff;border:1px solid #b3d9ff;border-radius:6px;padding:10px 14px;margin:10px 0">
<b style="color:#1565c0;font-size:13px">[ADD] 加仓计划（突破加仓）</b>
<table style="margin-top:6px;font-size:12px">
<tr><th style="width:100px;background:#e3f2fd">项目</th><th style="background:#e3f2fd">具体条件</th></tr>
<tr><td><b>加仓触发价</b></td><td style="color:#1565c0;font-weight:bold">{add_trigger_price:.3f} 元（{_add_trigger_desc}）</td></tr>
<tr><td><b>加仓仓位</b></td><td>按现有持仓30%加仓 | 加仓股数: <b>{add_shares}</b>股 / {add_amount:,.0f}元</td></tr>
<tr><td><b>前置条件</b></td><td style="font-size:11px">{add_preconditions}</td></tr>
<tr><td><b>加仓后止损</b></td><td style="color:#e74c3c">{add_stop_loss:.3f} 元（综合成本×{1-STOP_LOSS_PCT:.0%}）</td></tr>
<tr><td><b>与②止盈单关系</b></td><td style="font-size:11px">仅在②止盈单未触发且放量突破确认时执行（触发价已高于止盈价）</td></tr>
</table>
</div>"""

    # 隔夜外盘条件单提示（仅当行业影响分>=3时显示）
    _overnight_hint = ""
    try:
        if overnight_data.get("available"):
            _hint = _overnight.get_condition_hint_for_sector(h.get("赛道", ""))
            if _hint:
                _overnight_hint = f'<div style="background:#fff8e1;border:1px solid #ffcc02;border-radius:4px;padding:8px 12px;margin:8px 0;font-size:12px">{_hint}</div>'
    except Exception:
        pass

    # V10.1: 操盘密码摘要行（从预计算结果中取，合并入逐只卡片）
    _caopan_card_row = ""
    _cp_data = _caopan_results_map.get(code)
    if _cp_data:
        _cp_tl = _cp_data.get('trend_level', 3)
        _cp_tl_color = {5:'#e53935',4:'#ff7043',3:'#ff9800',2:'#66bb6a',1:'#4caf50'}.get(_cp_tl, '#333')
        _cp_dk = _cp_data.get('dk_signal') or '无'
        _cp_dk_g = _cp_data.get('dk_grade', '')
        _cp_rr = _cp_data.get('risk_reward', {})
        _cp_rr_v = _cp_rr.get('risk_reward_1', 0)
        _cp_rr_ok = _cp_rr.get('passed', False)
        _cp_dev = _cp_data.get('deviation_pct', 0)
        _cp_act = _cp_data.get('action_suggestion', {}).get('desc', '观望')
        _caopan_card_row = (f'<table style="width:100%;font-size:12px;margin:6px 0"><tr><td><b>操盘密码</b></td>'
            f'<td>趋势<span style="color:{_cp_tl_color};font-weight:bold">{_cp_tl}级</span> | '
            f'DK:<b>{_cp_dk}</b>(/{_cp_dk_g}) | '
            f'乖离{_cp_dev:+.1f}% | '
            f'盈亏比<span style="color:{"#4caf50" if _cp_rr_ok else "#f44336"}">{_cp_rr_v:.1f}:1</span> | '
            f'{_cp_act}</td></tr></table>')

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

    # 增强4: 逻辑止损/提前退出（买入逻辑被破坏即主动离场，不等价格止损）
    _logic_exits = []
    try:
        _t_ma20 = tech.get("ma20")
        if _t_ma20 and latest > 0 and float(_t_ma20) > latest:
            _logic_exits.append(f"已失守MA20({_t_ma20:.3f})，3日内收不回则离场")
        _t_hist = tech.get("macd_hist")
        if _t_hist is not None and float(_t_hist) < 0 and tech.get("momentum_5d", 0) < -5:
            _logic_exits.append("动能衰竭(MACD绿柱+5日跌幅>5%)，反弹减仓")
    except Exception:
        pass
    if h.get("赛道", "") in _weak_sectors:
        _logic_exits.append(f"所属赛道已转弱({h.get('赛道', '')})，考虑换股或提前离场")
    _logic_exits.append("压力位放量滞涨/买入逻辑失效→主动离场，不等价格止损")
    _logic_exit_html = "；".join(_logic_exits)

    # FIX P0(2026-08-10): 占位成本盈亏行特殊标注（预构造HTML，避免f-string内反斜杠）
    # V10.2: 浮亏>=8%整行红色背景+pnl-danger；|盈亏|>5%放大字号
    if h.get('占位成本'):
        _pnl_line_html = '已回本（券商成本异常，0.01占位）'
    elif pnl_pct <= -8:
        _pnl_line_html = f'<span class="pnl-danger" style="font-size:18px">{pnl_pct:+.1f}% ({pnl_amt:+,.0f}元)</span>'
    else:
        _pnl_line_color = '#e74c3c' if pnl_pct >= 0 else '#27ae60'
        _pnl_font_sz = 'font-size:18px;' if abs(pnl_pct) > 5 else ''
        _pnl_line_html = f'<b style="color:{_pnl_line_color};{_pnl_font_sz}">{pnl_pct:+.1f}% ({pnl_amt:+,.0f}元)</b>'
    _cost_note = '（占位·已回本）' if h.get('占位成本') else ''
    # V10.2: 止损价逼近预警（距止损<3%时红底白字）
    _sl_proximity_pct = abs(latest - h['止损价']) / latest * 100 if latest > 0 else 999
    _sl_proximity_warn = '<span style="color:#cf1322;font-weight:bold;font-size:11px"> ⚠️逼近止损</span>' if _sl_proximity_pct < 3 else ''

    html += f"""
<div class="stock-card">
<div class="stock-card-header">{code} {h['名称']} {action_tag} <span style="font-size:12px;color:#888">({h['赛道']})</span>
<span style="float:right;font-size:13px">{trend_dir}</span></div>
<div class="stock-card-body">

<div style="background:{advice_bg};border-left:4px solid {advice_border};padding:10px 14px;margin:8px 0;border-radius:4px">
<b style="color:{action_color};font-size:15px">📌 操作建议: {action}</b><br>
<span style="font-size:12px;color:#555">{action_reason}</span>
</div>

<div style="margin:8px 0">
<span style="font-size:12px;color:#666">综合评分: <b style="color:{bar_color}">{comp}/100</b></span>
<div class="score-bar"><div class="score-fill" style="width:{comp}%;background:{bar_color}"></div></div>
</div>

<table class="metric-table"><tr>
<td class="metric-cell"><div class="metric-label">最新价</div><div class="metric-value {chg_class}">{latest:.3f}</div><div class="metric-label">{h['涨跌幅']:+.2f}%</div></td>
<td class="metric-cell"><div class="metric-label">成本/数量</div><div style="font-size:12px"><b>{cost_price:.3f}</b>{_cost_note}<br>{shares}股</div></td>
<td class="metric-cell"><div class="metric-label">盈亏</div><div style="font-size:13px">{_pnl_line_html}</div></td>
</tr><tr>
<td class="metric-cell"><div class="metric-label">均线</div><div style="font-size:11px">MA5:<b>{tech['ma5']}</b> MA10:<b>{tech['ma10']}</b> MA20:<b>{tech['ma20']}</b></div></td>
<td class="metric-cell"><div class="metric-label">动量</div><div style="font-size:11px">RSI:<b>{tech['rsi']}</b> MACD:<b>{tech['macd_dif']}/{tech['macd_dea']}</b></div></td>
<td class="metric-cell"><div class="metric-label">波动</div><div style="font-size:11px">ATR:<b>{tech['atr']}</b> 布林:<b>{tech['boll_lower']}-{tech['boll_upper']}</b></div></td>
</tr></table>

<div style="margin:8px 0;font-size:12px">
<b>支撑位:</b> <span style="color:#4caf50">{support_str}</span><br>
<b>压力位:</b> <span style="color:#e74c3c">{resist_str}</span>
</div>

<ul class="signal-list">
{''.join(f'<li>{s}</li>' for s in all_signals)}
</ul>
{_pattern_html}

<table>
<tr><th>条件单类型</th><th>具体设置</th><th>优先级</th><th>有效期</th></tr>
<tr><td><b>① 止损单</b></td><td class="{'stop-loss-warn' if _sl_proximity_pct < 3 else ''}" style="{'color:#cf1322;font-weight:bold;background:#fff1f0' if _sl_proximity_pct < 3 else 'color:#e74c3c;font-weight:bold'}">触发价 {h['止损价']:.3f}，委托价 {h['止损价']*0.995:.3f}（最新价×{1-STOP_LOSS_PCT:.0%}）{_dist_str(h['止损价'])}{_sl_proximity_warn}</td><td>★★★必挂</td><td>20天</td></tr>
<tr><td><b>② 止盈单(减仓)</b></td><td style="color:#1976d2;font-weight:bold">触发价 {tp1:.3f}，卖出{shares//2}股（{tp_basis}）{_dist_str(tp1)}<br><span style="font-size:11px;color:#666;font-weight:normal">触及先减仓锁盈；若后续放量突破该压力位（见加仓计划触发价），剩余仓位可持有</span></td><td>★★建议</td><td>15天</td></tr>
<tr><td><b>③ 止盈单(清仓)</b></td><td style="color:#1976d2">触发价 {tp2:.3f}，全部清仓{_dist_str(tp2)}</td><td>★可选</td><td>20天</td></tr>
<tr><td><b>④ 回落卖出</b></td><td style="color:#ff9800">最高{h.get('回落基准', h['最高']):.3f}回落{h.get('回落比例', 0.07)*100:.0f}%至 {h['回落触发']:.3f} 卖出{_dist_str(h['回落触发'])}</td><td>★★建议</td><td>10天</td></tr>
<tr><td><b>⑤ 逻辑止损(提前退出)</b></td><td style="color:#ff9800;font-size:11px">{_logic_exit_html}</td><td>★★★心法</td><td>每日盘后检查</td></tr>
</table>
{_overnight_hint}
{add_card_html}
{_caopan_card_row}

<div style="font-size:11px;color:#666;margin-top:5px">[INFO] {hold_reason} | 建议持有: {hold_suggest}</div>
</div></div>"""

# ---- V2.7: 次日开盘调仓计划 ----
# V3.2回测诊断: 调仓增益为-0.85%(202次调仓全部负增益)，收紧卖出条件+买入追高过滤
# FIX: 阈值改从 config.REBALANCE_CONFIG 单一来源读取（数值不变，与 V3.2 口径一致）
_reb_cfg = getattr(config, "REBALANCE_CONFIG", {"score_gap": 25, "sell_threshold": 30, "buy_threshold": 70})
REBALANCE_SCORE_GAP = _reb_cfg["score_gap"]  # V3.2: 评分差门槛从20升至25（减少无效调仓）
REBALANCE_SELL_THRESHOLD = _reb_cfg["sell_threshold"]  # V3.2: 卖出门槛从40降至30（仅极端弱势才卖）
REBALANCE_BUY_THRESHOLD = _reb_cfg["buy_threshold"]  # V3.2: 买入门槛从65升至70（更严格筛选）
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
# FIX P1(2026-08-07): 原json.load无异常保护，文件损坏/截断(超时kill高发产物)会炸掉整份报告
verify_path = os.path.join(config.PROJECT_ROOT, 'output', 'win_rate_verification.json')
if os.path.exists(verify_path):
    try:
        with open(verify_path, 'r', encoding='utf-8') as f:
            vr = json.load(f)
        html += f"""
<h2>📊 系统胜率验证摘要</h2>
<table class="metric-table"><tr>
<td class="metric-cell"><div class="metric-value">{vr.get('win_rate', 0)}%</div><div class="metric-label">真实胜率(1322笔)</div></td>
<td class="metric-cell"><div class="metric-value">{vr.get('profit_factor', 0)}</div><div class="metric-label">盈亏比</div></td>
<td class="metric-cell"><div class="metric-value down">{vr.get('total_pnl', 0):+,.0f}</div><div class="metric-label">总盈亏(元)</div></td>
<td class="metric-cell"><div class="metric-value">{vr.get('avg_hold_days', 0)}天</div><div class="metric-label">平均持仓</div></td>
</tr></table>
<div class="alert alert-warning">📋 历史验证结论: T+0胜率64.7%(唯一正收益) | 持仓越长胜率越低 | 日均18笔过度交易 → 当前已限制每日≤3笔</div>"""
    except Exception as _vr_e:
        print(f"  [WARN] 胜率验证摘要读取失败，已跳过: {_vr_e}")

# ---- V4.1(M1): 实盘偏差监控（回测预期 vs G2实盘cohort结算）----
# 只提示不干预；样本不足时仅标注积累中，异常静默降级不影响主报告
try:
    from output.live_divergence import calc_live_vs_backtest_divergence, render_divergence_html
    _div_html = render_divergence_html(calc_live_vs_backtest_divergence())
    if _div_html:
        html += _div_html
except Exception as _div_e:
    print(f"  [WARN] 实盘偏差监控区块生成失败，已跳过: {_div_e}")

# ---- V4.1(M2): 条件单执行归因（近3日生成 vs 真实成交匹配）----
try:
    from execution.order_attribution import collect_order_execution_stats, render_order_attribution_html
    _oa_html = render_order_attribution_html(collect_order_execution_stats(lookback_days=3), lookback_days=3)
    if _oa_html:
        html += _oa_html
except Exception as _oa_e:
    print(f"  [WARN] 条件单执行归因区块生成失败，已跳过: {_oa_e}")

# ---- V4.1(P3): 滑点执行归因摘要（校准建议仅展示待确认，不自动改参）----
try:
    from execution.slippage_section import render_slippage_section_html
    _sl_html = render_slippage_section_html(lookback_days=7)
    if _sl_html:
        html += _sl_html
except Exception as _sl_e:
    print(f"  [WARN] 滑点归因区块生成失败，已跳过: {_sl_e}")

# ---- V4.1(M3): 预警闭环统计（台账命中率/后验收益）----
try:
    from notify.alert_stats_section import render_alert_stats_html
    _al_html = render_alert_stats_html()
    if _al_html:
        html += _al_html
except Exception as _al_e:
    print(f"  [WARN] 预警统计区块生成失败，已跳过: {_al_e}")

# ---- V4.1(E3): 系统健康度（G8双心跳 + G2结算链路）----
try:
    from output.system_health_section import render_system_health_html
    _sh_html = render_system_health_html()
    if _sh_html:
        html += _sh_html
except Exception as _sh_e:
    print(f"  [WARN] 系统健康度区块生成失败，已跳过: {_sh_e}")

# ---- V10.0: 持仓健康度与末位淘汰排名 ----
if HAS_HOLDING_RANKING:
    try:
        # 构建rank_holdings所需的持仓字典（使用原始字段）
        _ranking_holdings = {}
        _ranking_tech = {}
        for _rk_code, _rk_h in holdings.items():
            if _rk_h.get("数量", 0) <= 0:
                continue
            _ranking_holdings[_rk_code] = {
                "name": _rk_h["名称"],
                "shares": _rk_h["数量"],
                "buy_price": _rk_h["成本"],
                "current_price": _rk_h["最新"],
                "sector": _rk_h["赛道"],
                "buy_date": _rk_h.get("买入日期", ""),
            }
            _tech = _rk_h.get("技术", {})
            if _tech.get("valid"):
                _ranking_tech[_rk_code] = {
                    "composite": _tech.get("composite", 50),
                    "trend": _tech.get("coarse_trend", _tech.get("trend", "横盘整理")),
                }
        _ranking_result = rank_holdings(_ranking_holdings, _ranking_tech)
        _ranking_html = generate_ranking_html(_ranking_result)
        if _ranking_html:
            html += _ranking_html
        print(f"  持仓排名: 健康度{_ranking_result['health_score']:.0f} | "
              f"淘汰建议{len(_ranking_result['eliminate_suggestions'])}只 | "
              f"平均评分{_ranking_result['summary']['avg_score']:.0f}")
    except Exception as _rk_e:
        print(f"  [WARN] 持仓排名区块生成失败: {_rk_e}")

# ---- 推荐股票（五层引擎完整交易计划）----
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
        # 增强4: 风险点结构化（逐条列表替代管道拼接，分类图标提示）
        _risk_items = plan.get("risk_notes") or ["暂无显著风险提示"]
        risks_html = "".join(f"<li style='margin:2px 0'>⚠️ {r}</li>" for r in _risk_items)
        # 增强4: 仓位区间化（弱势环境取下限，避免单一点位仓位误导）
        _pos_pct = plan.get("position_pct", 0)
        _pos_low = _pos_pct * 0.75
        # 信号列表
        signals_html = ", ".join(l4.get("signals", [])[:5])

        html += f"""
<div class="stock-card" style="border-left:5px solid #4caf50">
<div class="stock-card-header">🌟 推荐{idx}: {rec['code']} {rec['name']} <span style="font-size:12px;color:#888">({rec['sector']}/{rec['type']})</span>
<span style="float:right;font-size:14px;color:#4caf50;font-weight:bold">综合评分 {rec['total_score']}</span></div>
<div class="stock-card-body">
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
<tr><td><b>逻辑止损</b></td><td style="color:#ff9800;font-size:11px">跌破买入逻辑即离场（不等价格止损）：买点失效回落/放量滞涨于{plan['first_resistance_name']}/所属板块转弱</td></tr>
<tr><td><b>目标价位</b></td><td>第一目标: <b style="color:#e74c3c">{plan['target_1']:.2f}元</b>(减仓1/2) | 第二目标: <b style="color:#e74c3c">{plan['target_2']:.2f}元</b>(清仓)</td></tr>
<tr><td><b>盈亏比</b></td><td style="font-weight:bold;color:{'#4caf50' if plan['risk_reward']>=2.5 else '#ff9800'}">{plan['risk_reward']}:1 {'✅达标' if plan['risk_reward']>=2.5 else '⚠️偏低'}</td></tr>
<tr><td><b>建议仓位</b></td><td><b>{_pos_low:.1f}% ~ {_pos_pct:.1f}%</b>（弱势/熔断环境取下限）（约{plan['buy_shares']}股 / {plan['buy_amount']:,.0f}元）| 单笔风险≤总资金2%</td></tr>
<tr><td><b>支撑位</b></td><td style="color:#4caf50">{plan['first_support_name']}: {plan['first_support']:.2f}元 | 距支撑{plan['dist_to_support_pct']:.1f}%</td></tr>
<tr><td><b>压力位</b></td><td style="color:#e74c3c">{plan['first_resistance_name']}: {plan['target_1']:.2f}元</td></tr>
<tr><td><b>风险提示</b></td><td style="color:#ff9800;font-size:11px"><ul style="margin:2px 0;padding-left:18px">{risks_html}</ul></td></tr>
</table>

<div style="font-size:11px;color:#666;margin-top:8px;padding-top:5px;border-top:1px dashed #eee">
<b>技术信号:</b> {signals_html}<br>
<b>赛道评分:</b> {l2['score']}/100 | <b>趋势评分:</b> {l4['score']}/100 | <b>买点评分:</b> {plan['score']}/100 | ATR: {plan['atr']}
</div>
</div></div>"""

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

# ---- 面板七：龙虎榜/游资动向 ----
html += '<h2>五、龙虎榜/游资动向</h2>'
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
    html += '<div class="explain-compact">💡 龙虎榜=大额交易公示栏 | 机构连续买入→中线可跟 | 纯游资主导+上榜频繁→不追高，设好止损</div>'
    # 风险预警
    risk_stocks = [code_h for code_h, r in lhb_results.items() if r.get('risk_warning')]
    if risk_stocks:
        names = [holdings[c]["名称"] for c in risk_stocks if c in holdings]
        html += f'<div class="alert alert-danger">🚨 龙虎榜风险预警: {", ".join(names)} 游资主导且上榜频繁，短线风险较高</div>'
else:
    html += '<div class="alert alert-warning">龙虎榜数据暂不可用</div>'

# ---- 面板八：融资融券信号 ----
html += '<h2>六、融资融券信号</h2>'
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
    html += '<div class="explain-compact">💡 融资=借钱做多/融券=借股做空 | 融资连5日净买入+余额上升→正面 | 余额拐点→减仓 | 融券异常→勿加仓</div>'
else:
    html += '<div class="alert alert-warning">融资融券数据暂不可用</div>'

# ---- 面板九：解禁风险预警 ----
html += '<h2>七、解禁风险预警</h2>'
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
    html += '<div class="explain-compact">💡 解禁=限售股可卖出 | 占流通股&lt;5%影响小 | 5-15%中等 | &gt;15%高冲击→回避2周 | 解禁后不跌反涨=接盘强</div>'
    # 市场解禁摘要
    if release_summary and release_summary.get('total_stocks', 0) > 0:
        html += f'<div style="font-size:11px;color:#666;margin-top:5px">📅 市场解禁: 未来30天共{release_summary.get("total_stocks", 0)}只标的解禁'
        if release_summary.get('peak_week'):
            html += f' | 高峰: {release_summary["peak_week"]}'
        html += '</div>'
else:
    html += '<div class="alert alert-warning">解禁风险数据暂不可用</div>'

# ---- 面板十：四级资金流向 ----
html += '<h2>八、四级资金流向</h2>'
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

        html += f'<div class="stock-card"><div class="stock-card-header">{h["名称"]}({code_h}) <span style="font-size:12px;color:#888">{dir_text}</span>'
        if pattern_class:
            html += f' <span class="pattern-tag {pattern_class}">{pattern_text}</span>'
        elif pattern == 'neutral':
            html += f' <span class="pattern-tag" style="background:#95a5a6;color:white">{pattern_text}</span>'
        html += '</div>'

        html += '<div class="stock-card-body">'
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
        html += '</div>'
else:
    html += '<div class="alert alert-warning">四级资金流数据暂不可用</div>'

# 四级资金流摘要提示
html += '<div class="explain-compact">💡 吸筹=大单悄悄买→跟随持有 | 放量拉升=拿住不追高 | 对倒骗线=大单买小单卖→坚决不追 | 正常=等待方向</div>'

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
止损规则: 最新价×{1-STOP_LOSS_PCT:.0%}（Ratchet只升不降，浮亏时底线=成本价×{1-STOP_LOSS_PCT:.0%}）<br>
行情时间: {quote_time_str or now} | {today}<br>
<span style="color:#bbb">⚠️ 仅供参考，非投资建议 | 股市有风险，投资需谨慎</span>
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
# 样例/定制发送: HOLDINGS_REPORT_SUBJECT 环境变量可整体覆盖标题（默认行为不变）
_env_subject = os.environ.get("HOLDINGS_REPORT_SUBJECT")
if _env_subject:
    subject = _env_subject
print(f"[发送] {subject}")
# 批2-S6: HOLDINGS_REPORT_NO_EMAIL 环境变量门（dry-run，默认行为不变）
if os.environ.get("HOLDINGS_REPORT_NO_EMAIL"):
    print("dry-run 模式：邮件未发送")
else:
    result = send_email(subject, html)
    print(f"[结果] {'✅ 发送成功' if result else '❌ 发送失败'}")
    # FIX: 发送失败时以退出码 1 结束，使 scheduler.py 的退出码判定与失败告警真正生效（dry-run 分支不受影响）
    if not result:
        print("[错误] 邮件发送失败，退出码 1（供调度器告警链路捕获）")
        sys.exit(1)
