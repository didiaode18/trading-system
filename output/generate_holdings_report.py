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
# P1-5: 文件已迁移至 output/，路径需上溯一级到项目根目录
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, 'trading_system'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import pandas as pd
import config
from notify.email_notify import send_email
from data.realtime import fetch_realtime_batch
from data.data_loader import fetch_stock_daily_baostock, load_daily_data, _bs_logout
# FIX: 清理死代码无用 import（generate_trading_plan grep 确认本文件零使用，仅删导入）
from strategy.recommend_engine import run_recommendation
from strategy.caopan_signal import CaopanEngine
from caopan_chart import generate_caopan_chart  # P1-5: 同目录直接导入

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

# 任务#1新增: 板块背离检测（失败时降级，不影响报告主流程）
try:
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

# 批2-S6新增: 组合风控评估（失败时降级，不影响报告主流程）
try:
    from strategy.portfolio_risk import PortfolioRiskManager
    HAS_PORTFOLIO_RISK = True
except Exception:
    HAS_PORTFOLIO_RISK = False

# [V5删除] capital_planner/execution_closure/holding_ranking 已不再使用

# 增强1新增: 市场情绪与资金面面板（全数据源降级，失败不影响报告主流程）
try:
    from strategy.market_pulse import get_market_pulse, render_pulse_html
    HAS_MARKET_PULSE = True
except Exception:
    HAS_MARKET_PULSE = False

# V9.1新增: 板块轮动监控（板块景气雷达，失败降级不影响报告主流程）
try:
    from strategy.sector_flow import SectorMonitor, get_stock_sector_map, detect_intraday_sector_surges, render_intraday_sector_banner
    HAS_SECTOR_FLOW = True
except Exception:
    HAS_SECTOR_FLOW = False

# V10新增: 调仓建议生成器（ActionPlanner，失败降级不影响报告主流程）
try:
    from strategy.action_planner import ActionPlanner
    HAS_ACTION_PLANNER = True
except Exception:
    HAS_ACTION_PLANNER = False

# 增强3新增: 风控熔断状态横幅（读取既有风控状态文件，失败降级不展示）
try:
    from risk.risk_control import RiskStateManager, StrategyFailureDetector
    HAS_RISK_STATE = True
except Exception:
    HAS_RISK_STATE = False

# [V5删除] holding_ranking 已不再使用

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
                # FIX(2026-08-26) BUG-01: 券商盈亏字段透传，供主循环交叉验证成本口径
                "broker_pnl": v.get("pnl"),
                # FIX P0(2026-10-09): 透传JSON快照时的最新价，供成本交叉验证使用同时间口径价格
                "json_current_price": v.get("current_price", 0),
            })
        return result
    except Exception as _e:
        # FIX(2026-08-26) BUG-06: 禁止静默回退到陈旧硬编码持仓，异常时打日志+返回空列表
        print(f"[FATAL] holdings.json 加载失败({_e})，使用空列表（不再回退到硬编码）")
        return []


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
# V5重构: 明日行动指令卡 —— 5秒识别核心操作
# 合并原执行清单+调仓计划+决策矩阵+板块轮动+反冲动锁
# 按紧急度分4档: 🚨紧急 → ⚡重要 → ⚠️关注 → ✅持有
# ============================================================
SPOTLIGHT_LOSS_RED_PCT = 8.0      # 浮亏≥8% 或跌破止损线 → 紧急档
SPOTLIGHT_PROFIT_GREEN_PCT = 3.0  # 浮盈≥3% → 重要档(止盈机会)

_SPOT_TIER_META = {
    0: ("🚨 紧急（止损/清仓）", "#cf1322"),
    1: ("⚡ 重要（减仓/止盈）", "#fa8c16"),
    2: ("⚠️ 关注（设条件单）", "#d48806"),
    3: ("✅ 持有（无需操作）", "#389e0d"),
}


def _render_action_spotlight(holdings, regime_result=None, vol_result=None,
                             target_pct=0.6, current_pct=0.0, current_mv=0.0,
                             _CAPITAL=1000000, sell_candidates=None,
                             recommendations=None, sector_divergence=None,
                             forecast_results=None, corr_penalty_tags=None,
                             anti_impulse_warnings=None) -> str:
    """
    V5重构: 渲染"明日行动指令卡"——5秒识别核心操作。
    合并原执行清单+调仓计划+决策矩阵+板块轮动+反冲动锁。
    按紧急度分4档输出，底部附换股/加仓建议。
    异常返回空串(调用方降级)。
    """
    held = {c: h for c, h in holdings.items() if h.get("数量", 0) > 0}
    if not held:
        return ""

    html = '<h2>🎯 明日行动指令卡</h2>'

    # ---- 大盘环境 + 仓位状态（合并为顶部信息条） ----
    _state_cn, _state_color = "未知", "#999"
    _strategy = ""
    if regime_result:
        _state = regime_result.get("state", "")
        _state_cn = regime_result.get("state_cn", _state)
        _state_color = {"BULL": "#52c41a", "RANGE": "#faad14", "BEAR": "#ff4d4f"}.get(_state, "#999")
        try:
            from strategy.market_regime import MarketRegimeDetector as _MRD
            _advice = _MRD().get_strategy_advice(regime_result)
            _strategy = _advice.get("primary_strategy", "")
        except Exception:
            pass
    # FIX P0(2026-10-09): 可用资金直接读取config.AVAILABLE_CASH，不再从TOTAL_CAPITAL-市值推算。
    # 原逻辑(TOTAL_CAPITAL-实时市值)因实时价格变动导致可用资金失真(如报告6069/7899 vs 实际30.50)。
    _avail_cash = getattr(config, 'AVAILABLE_CASH', max(0, _CAPITAL - current_mv))
    _vol_desc = ""
    if vol_result:
        _vol_scale = vol_result["scale"]
        _vol_desc = f'<br><b>波动率:</b> {vol_result["vol_regime"]}(缩放{_vol_scale:.2f})'
    _pos_tier_name = ("满仓" if current_pct > 0.85 else "重仓" if current_pct > 0.65
                      else "标准" if current_pct > 0.35 else "轻仓" if current_pct > 0.10 else "清仓")
    html += (f'<div style="background:#f0f5ff;border:2px solid #adc6ff;border-radius:8px;padding:12px 16px;margin:8px 0">'
             f'<div style="font-size:13px">'
             f'<b>大盘环境:</b> <span style="color:{_state_color};font-weight:bold;font-size:15px">{_state_cn}</span>'
             f'<span style="color:#888;margin-left:8px">目标仓位 {target_pct:.0%}</span>'
             f'{"<span style=color:#888;margin-left:8px>策略: " + _strategy + "</span>" if _strategy else ""}'
             f'{_vol_desc}'
             f'</div>'
             f'<div style="font-size:13px;margin-top:4px">'
             f'<b>仓位状态:</b> <span style="font-weight:bold;font-size:15px">{_pos_tier_name}</span>'
             f' 当前 {current_pct:.0%} → 目标 {target_pct:.0%}'
             f' | 可用资金 <b style="color:#1976d2">{_avail_cash:,.0f}元</b>'
             f'</div></div>')

    # ---- 反冲动锁警告（置顶，有信号时才展示） ----
    if anti_impulse_warnings:
        _warn_names = ", ".join(set(f"{w['name']}({w['direction']})" for w in anti_impulse_warnings[:5]))
        html += (f'<div style="background:#fff2e8;border:1px solid #ffbb96;border-radius:6px;padding:10px 14px;margin:8px 0;color:#d4380d">'
                 f'<b>⚠️ 反冲动锁:</b> {anti_impulse_warnings[0].get("date","")}有操作: {_warn_names}'
                 f'<br><span style="font-size:12px">如今日建议方向与昨日操作相反，请冷静24小时再决策</span></div>')

    # ---- 按紧急度分4档分类 ----
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
            _sell_sh = shares
            _trigger = f'止损价{stop:.2f}{"（已破）" if h.get("止损已破") else ""}'
            _timing = "开盘即执行"
            _detail = (f'全部 <b>{_sell_sh}股</b> | 触发: {_trigger} | '
                       f'时限: {_timing}')
        elif "减仓" in action or "止盈" in action or \
                (not h.get("占位成本") and pnl >= SPOTLIGHT_PROFIT_GREEN_PCT):
            lvl = 1
            _sell = max(int(shares * 0.5 / 100) * 100, min(100, shares))
            _tp1 = h.get("止盈目标1", 0)
            _exec = _tp1 if _tp1 > 0 else latest
            _detail = (f'<b>{action}</b> | {_sell}股(约50%) | '
                       f'执行价: {_exec:.2f} | 14:50前')
        elif action == "谨慎持有" or (tech.get("valid") and comp < 45 and pnl < 0):
            lvl = 2
            _detail = (f'<b>{action}</b> | 暂不动 | '
                       f'设条件单止损 {stop:.2f} | 今日设好')
        else:
            lvl = 3
            _detail = (f'<b>{action}</b> | 全部持有 | '
                       f'关注止损 {stop:.2f} | 常规跟踪')

        # 相关性惩罚标签（观察模式）
        _corr_tag = ""
        if corr_penalty_tags and code in corr_penalty_tags:
            _corr_tag = corr_penalty_tags[code]

        groups[lvl].append((code, h, _detail, reason, _corr_tag))

    # ---- 渲染4档色块卡片 ----
    _card_style = {
        0: ("#cf1322", "#fff1f0", "🚨"),
        1: ("#fa8c16", "#fff7e6", "⚡"),
        2: ("#d48806", "#fffbe6", "⚠️"),
        3: ("#389e0d", "#f6ffed", "✅"),
    }
    _has_any_action = False
    for lvl in (0, 1, 2, 3):
        items = groups[lvl]
        if not items:
            continue
        if lvl < 3:
            _has_any_action = True
        _title, _tcolor = _SPOT_TIER_META[lvl]
        html += (f'<div style="margin:14px 0 6px;font-size:14px;font-weight:bold;'
                 f'color:{_tcolor}">{_title}（{len(items)}只）</div>')
        for code, h, _detail, _reason, _corr_tag in items:
            _color, _bg, _icon = _card_style[lvl]
            _corr_html = (f'<br><span style="color:#cf222e;font-weight:bold;font-size:11px">'
                          f'🔗 {_corr_tag}</span>') if _corr_tag else ''
            html += (f'<div style="border-left:6px solid {_color};background:{_bg};'
                     f'border-radius:8px;padding:10px 14px;margin:8px 0">'
                     f'<div style="font-size:14px;font-weight:bold;color:{_color}">'
                     f'{_icon} {code} {h["名称"]}</div>'
                     f'<div style="font-size:12px;color:#555;margin-top:4px">{_detail}{_corr_html}</div>'
                     f'<div style="font-size:12px;color:#777;margin-top:3px">💡 {_reason}</div>'
                     f'</div>')

    # ---- 减仓指令表（仓位超目标时展示，吸收原执行清单逻辑） ----
    need_reduce = current_pct - target_pct
    reduce_amount = need_reduce * _CAPITAL if need_reduce > 0 else 0
    if need_reduce > 0.02 and reduce_amount > 5000 and sell_candidates:
        _has_any_action = True
        html += (f'<div style="margin:12px 0;padding:10px 14px;background:#fff1f0;'
                 f'border:1px solid #ffa39e;border-radius:6px">'
                 f'<b style="color:#cf1322;font-size:13px">📉 减仓计划:</b> '
                 f'当前 {current_pct:.0%} 超目标 {target_pct:.0%}，建议减持约 {reduce_amount/10000:.1f}万元'
                 f'</div>')
        html += ('<table style="width:100%;border-collapse:collapse;font-size:12px;margin:8px 0">'
                 '<tr style="background:#fff1f0">'
                 '<th style="padding:6px;border:1px solid #ffa39e">代码/名称</th>'
                 '<th style="padding:6px;border:1px solid #ffa39e">卖出股数</th>'
                 '<th style="padding:6px;border:1px solid #ffa39e">挂单价</th>'
                 '<th style="padding:6px;border:1px solid #ffa39e">时间窗口</th></tr>')
        _remaining_reduce = reduce_amount
        for _pri, _code, _h, _tech in sell_candidates:
            if _remaining_reduce <= 0:
                break
            _shares = _h.get("数量", 0)
            _price = _h.get("最新", 0)
            if _shares <= 0 or _price <= 0:
                continue
            # V25(P0-2): 指令卡vs减仓表矛盾标注 —— 指令卡为"持有"类但出现在减仓表时，
            # 明确标注"仓位再平衡"原因，避免用户困惑"为什么让我持有又让我卖"
            _action = _h.get("操作建议", "")
            _hold_actions = ("继续持有", "持有待涨", "持有观望", "谨慎持有")
            _is_hold_but_reduce = _action in _hold_actions
            _name = _h.get("名称", _code)
            _pnl = _h.get("盈亏比例", 0)
            _need = _remaining_reduce / _price
            _sell_shares = int(min(_need, _shares) / 100) * 100
            if _sell_shares <= 0:
                if _price * 100 <= _remaining_reduce * 2:
                    _sell_shares = min(100, _shares)
                else:
                    break
            _sell_shares = min(_sell_shares, _shares)
            _support = 0
            _fc = (forecast_results or {}).get(_code, {})
            if _fc:
                _support = _fc.get("levels", {}).get("first_support", 0)
            if _pnl < -5:
                _order_price = round(_price, 2)
                _time_window = "开盘即执行"
            else:
                _order_price = round(_support, 2) if _support > 0 and _support < _price else round(_price * 0.99, 2)
                _time_window = "14:30-14:50"
            _remaining_reduce -= _sell_shares * _price
            # V25(P0-2): 持有类标的出现在减仓表时，标注"仓位再平衡"原因
            _reduce_note = f'<br><span style="color:#1565c0;font-size:10px">⚖️ 仓位再平衡(非个股看空)</span>' if _is_hold_but_reduce else ''
            html += (f'<tr><td style="padding:6px;border:1px solid #f0f0f0">{_code} {_name}'
                     f'<br><span style="color:#888">浮盈{_pnl:+.1f}%</span>{_reduce_note}</td>'
                     f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;font-weight:bold">{_sell_shares}股</td>'
                     f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center">{_order_price}</td>'
                     f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center">{_time_window}</td></tr>')
        html += '</table>'

    # ---- 加仓指令表（仓位低于目标+有推荐候选时展示） ----
    if current_pct < target_pct - 0.02 and recommendations:
        _add_total = (target_pct - current_pct) * _CAPITAL
        html += (f'<div style="margin:12px 0;padding:10px 14px;background:#f6ffed;'
                 f'border:1px solid #b7eb8f;border-radius:6px">'
                 f'<b style="color:#389e0d;font-size:13px">📈 加仓建议:</b> '
                 f'当前 {current_pct:.0%} 低于目标 {target_pct:.0%}，可增配约 {_add_total/10000:.1f}万元'
                 f'</div>')
        html += ('<table style="width:100%;border-collapse:collapse;font-size:12px;margin:8px 0">'
                 '<tr style="background:#f6ffed">'
                 '<th style="padding:6px;border:1px solid #b7eb8f">代码/名称</th>'
                 '<th style="padding:6px;border:1px solid #b7eb8f">买入区间</th>'
                 '<th style="padding:6px;border:1px solid #b7eb8f">止损价</th>'
                 '<th style="padding:6px;border:1px solid #b7eb8f">建议股数</th></tr>')
        for _ar in recommendations[:3]:
            _ap = _ar.get("plan") or {}
            _a_bl = _ap.get("buy_low", 0)
            _a_bh = _ap.get("buy_high", 0)
            _a_sl = _ap.get("stop_loss", 0)
            _a_price = _ar.get("price", 0) or _a_bh
            if _a_price <= 0:
                continue
            _a_pos_pct = _ap.get("position_pct", 0) or 0
            _a_shares = int(_a_pos_pct / 100.0 * _CAPITAL / _a_price / 100) * 100
            if _a_shares <= 0:
                _a_shares = 100
            _a_range = f"{_a_bl:.2f}~{_a_bh:.2f}" if _a_bl > 0 and _a_bh > 0 else f"{_a_price*0.98:.2f}~{_a_price*1.01:.2f}(参考)"
            _a_sl_str = f"{_a_sl:.2f}" if _a_sl > 0 else f"{_a_price*0.95:.2f}(-5%)"
            html += (f'<tr><td style="padding:6px;border:1px solid #f0f0f0">{_ar.get("code","")} {_ar.get("name","")}'
                     f'<br><span style="color:#888;font-size:11px">评分{_ar.get("total_score",0)}</span></td>'
                     f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;color:#1976d2;font-weight:bold">{_a_range}</td>'
                     f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;color:#e74c3c">{_a_sl_str}</td>'
                     f'<td style="padding:6px;border:1px solid #f0f0f0;text-align:center;font-weight:bold">{_a_shares}股</td></tr>')
        html += '</table>'

    # ---- 换股建议（板块轮动: 减持X→关注Y，仅有信号时展示） ----
    if sector_divergence and sector_divergence.get("available"):
        _sd_lag = sector_divergence.get("lagging_positions", [])
        _sd_missed = sector_divergence.get("missed_hotspots", [])
        _sd_strong_set = set(sector_divergence.get("strong_sectors", []))
        _sd_cmap = {}
        try:
            from strategy.sector_divergence import get_coarse_sector_map as _gct
            _sd_cmap = _gct() or {}
        except Exception:
            pass
        # 从推荐+观察池按强势赛道筛选替换候选
        _sd_hot_picks = []
        for _cand in list(recommendations or []):
            _c_coarse = _sd_cmap.get(_cand.get("sector", ""), _cand.get("sector", ""))
            if _c_coarse in _sd_strong_set and _cand not in _sd_hot_picks:
                _sd_hot_picks.append(_cand)
        if _sd_lag:
            for _lp in _sd_lag[:3]:
                _y_txt = ""
                if _sd_hot_picks:
                    _yp = _sd_hot_picks.pop(0)
                    _y_txt = f'{_yp.get("name", "")}({_yp.get("code", "")}·评分{_yp.get("total_score", 0)})'
                html += (f'<div style="background:#f0f7ff;border:1px solid #b3d9ff;border-radius:6px;padding:10px 14px;margin:8px 0">'
                         f'🔁 减持 <b style="color:#cf1322">{_lp["name"]}</b>'
                         f'（约{_lp["建议减持金额"]/10000:.1f}万元/{_lp["建议减持股数"]}股，{_lp.get("原因", "赛道弱势")}）')
                if _y_txt:
                    html += f' → 关注 <b style="color:#389e0d">{_y_txt}</b>'
                else:
                    html += ' → 暂无强势赛道候选，建议先减持后等待买点'
                html += '</div>'

    # ---- 底部备注 ----
    if not _has_any_action:
        html += '<div style="background:#f6ffed;border:1px solid #b7eb8f;border-radius:6px;padding:12px 16px;margin:10px 0;color:#389e0d;font-weight:bold">✅ 当前持仓状态良好，无需操作</div>'
    html += ('<p style="color:#888;font-size:11px;margin:6px 0">'
             '⏰ 时限口径: 紧急标的开盘即执行，其余建议14:30-14:50执行避免早盘假突破；'
             '指令卡基于收盘数据生成，次一交易日适用。</p>')
    return html


# ============================================================
# V9.1: 板块景气雷达 —— 强势板块排名 + 持仓板块健康度 + 轮动信号
# ============================================================
def _render_sector_radar(sector_result: dict, holdings: dict) -> str:
    """
    渲染“板块景气雷达”HTML段落

    参数:
        sector_result: SectorMonitor.analyze() 返回的结果
        holdings: 持仓字典 {code: {名称, 数量, 市值, 赛道, ...}}

    异常返回空字符串（调用方降级）。
    """
    if not sector_result:
        return ""

    html = '<h2>📡 板块景气雷达</h2>'

    # ---- 1. 强势板块TOP5排名表 ----
    ranked = sector_result.get("ranked", [])
    if ranked:
        html += '<h3>🔥 板块动量排名 TOP5</h3>'
        html += ('<table style="width:100%;border-collapse:collapse;font-size:12px">'
                 '<tr style="background:#34495e;color:#fff">'
                 '<th style="padding:6px">排名</th>'
                 '<th style="padding:6px">板块</th>'
                 '<th style="padding:6px">5日涨幅</th>'
                 '<th style="padding:6px">20日涨幅</th>'
                 '<th style="padding:6px">动量分</th>'
                 '<th style="padding:6px">状态</th></tr>')
        _status_colors = {
            "启动": "#4caf50", "加速": "#e74c3c", "上升": "#2196f3",
            "震荡": "#ff9800", "流出": "#9e9e9e", "下跌": "#f44336",
        }
        for m in ranked[:5]:
            _r5 = m.get("return_5d", 0)
            _r5_color = "#e74c3c" if _r5 >= 0 else "#27ae60"
            _r20 = m.get("return_20d", 0)
            _r20_color = "#e74c3c" if _r20 >= 0 else "#27ae60"
            _status = m.get("status", "")
            _st_color = _status_colors.get(_status, "#555")
            _score = m.get("momentum_score", 0)
            _score_color = "#e74c3c" if _score >= 65 else ("#ff9800" if _score >= 45 else "#27ae60")
            # V9.2: 近期爆发标签（5日涨幅>5%标注🔥）
            _hot_label = ""
            if _r5 > 5:
                _hot_label = ' <span style="color:#e74c3c;font-size:11px">🔥近期爆发</span>'
            elif _r5 > 3:
                _hot_label = ' <span style="color:#ff9800;font-size:11px">📈加速中</span>'
            _sector_name = m.get("sector", "")
            html += (f'<tr><td style="padding:5px;border:1px solid #eee;text-align:center">{m.get("rank", "")}</td>'
                     f'<td style="padding:5px;border:1px solid #eee;font-weight:bold">{_sector_name}{_hot_label}</td>'
                     f'<td style="padding:5px;border:1px solid #eee;color:{_r5_color};font-weight:bold">{_r5:+.2f}%</td>'
                     f'<td style="padding:5px;border:1px solid #eee;color:{_r20_color};font-weight:bold">{_r20:+.2f}%</td>'
                     f'<td style="padding:5px;border:1px solid #eee;color:{_score_color};font-weight:bold">{_score:.0f}</td>'
                     f'<td style="padding:5px;border:1px solid #eee"><span style="color:{_st_color};font-weight:bold">{_status}</span></td></tr>')
        html += '</table>'

    # ---- 2. 持仓板块健康度 ----
    _sector_map = {}
    try:
        _sector_map = get_stock_sector_map()
    except Exception:
        pass
    _holding_sectors = []
    _sector_metrics = sector_result.get("sector_metrics", {})
    for code, h in holdings.items():
        if h.get("数量", 0) <= 0:
            continue
        _name = h.get("名称", code)
        _sec = _sector_map.get(code, h.get("赛道", "其他"))
        _metrics = _sector_metrics.get(_sec, {})
        _holding_sectors.append({
            "code": code, "name": _name, "sector": _sec,
            "status": _metrics.get("status", "未知"),
            "momentum": _metrics.get("momentum_score", 0),
            "r5": _metrics.get("return_5d", 0),
        })

    if _holding_sectors:
        html += '<h3>🧺 持仓板块健康度</h3>'
        html += ('<table style="width:100%;border-collapse:collapse;font-size:12px">'
                 '<tr style="background:#34495e;color:#fff">'
                 '<th style="padding:6px">代码/名称</th>'
                 '<th style="padding:6px">所属板块</th>'
                 '<th style="padding:6px">5日涨幅</th>'
                 '<th style="padding:6px">动量分</th>'
                 '<th style="padding:6px">状态</th>'
                 '<th style="padding:6px">建议</th></tr>')
        for hs in _holding_sectors:
            _st = hs["status"]
            _st_color = _status_colors.get(_st, "#555")
            _mom = hs["momentum"]
            _mom_color = "#e74c3c" if _mom >= 60 else ("#ff9800" if _mom >= 40 else "#27ae60")
            _r5 = hs["r5"]
            _r5_color = "#e74c3c" if _r5 >= 0 else "#27ae60"
            # V9.2: 持仓板块近期爆发标签
            _sec_hot = ""
            if _r5 > 5:
                _sec_hot = ' 🔥'
            elif _r5 > 3:
                _sec_hot = ' 📈'
            _sec_name = hs["sector"]
            # 操作建议
            if _st in ("流出", "下跌") or _mom < 30:
                _advice = "⚠️ 考虑减仓"
                _adv_color = "#cf1322"
            elif _st == "启动":
                _advice = "✅ 持有待涨"
                _adv_color = "#389e0d"
            elif _st == "加速":
                _advice = "✅ 持有/可加仓"
                _adv_color = "#389e0d"
            else:
                _advice = "✅ 持有观察"
                _adv_color = "#555"
            html += (f'<tr><td style="padding:5px;border:1px solid #eee">{hs["code"]} {hs["name"]}</td>'
                     f'<td style="padding:5px;border:1px solid #eee;font-weight:bold">{_sec_name}{_sec_hot}</td>'
                     f'<td style="padding:5px;border:1px solid #eee;color:{_r5_color};font-weight:bold">{_r5:+.2f}%</td>'
                     f'<td style="padding:5px;border:1px solid #eee;color:{_mom_color};font-weight:bold">{_mom:.0f}</td>'
                     f'<td style="padding:5px;border:1px solid #eee"><span style="color:{_st_color};font-weight:bold">{_st}</span></td>'
                     f'<td style="padding:5px;border:1px solid #eee;color:{_adv_color};font-weight:bold">{_advice}</td></tr>')
        html += '</table>'

    # ---- 3. 轮动信号 + 建议关注/回避方向 ----
    signals = sector_result.get("signals", [])
    _startup = [s for s in signals if s.get("type") == "startup"]
    _accel = [s for s in signals if s.get("type") == "accelerate"]
    _outflow = [s for s in signals if s.get("type") == "outflow"]

    if _startup or _accel or _outflow or ranked:
        html += '<h3>🧭 板块方向指引</h3>'
        _guide_items = []
        if _startup:
            for s in _startup[:3]:
                _guide_items.append(f'<span style="color:#4caf50">🚀 {s["desc"]}</span>')
        if _accel:
            for s in _accel[:3]:
                _guide_items.append(f'<span style="color:#e74c3c">⚡ {s["desc"]}</span>')
        if _outflow:
            for s in _outflow[:3]:
                _guide_items.append(f'<span style="color:#9e9e9e">⚠️ {s["desc"]}</span>')
        # 建议关注/回避
        _top_strong = [m["sector"] for m in ranked[:3]] if ranked else []
        _bottom_weak = [m["sector"] for m in ranked[-2:]] if len(ranked) >= 2 else []
        if _top_strong:
            _guide_items.append(f'<b style="color:#389e0d">✅ 关注方向: {"、".join(_top_strong)}</b>')
        if _bottom_weak:
            _guide_items.append(f'<b style="color:#cf1322">❌ 回避方向: {"、".join(_bottom_weak)}</b>')

        if _guide_items:
            html += '<div style="background:#f0f7ff;border:1px solid #b3d9ff;border-radius:6px;padding:10px 14px;margin:8px 0;font-size:12px">'
            html += '<br>'.join(_guide_items)
            html += '</div>'

    # ---- 4. 持仓预警 ----
    alerts = sector_result.get("alerts", [])
    if alerts:
        html += '<div style="margin:8px 0">'
        for a in alerts[:5]:
            _icon = "🚨" if a.get("level") == "danger" else "⚠️"
            _color = "#cf1322" if a.get("level") == "danger" else "#fa8c16"
            html += f'<div style="color:{_color};font-size:12px;margin:4px 0">{_icon} {a.get("desc", "")}</div>'
        html += '</div>'

    # ---- 摘要 ----
    _summary = sector_result.get("summary", "")
    if _summary:
        html += f'<p style="color:#888;font-size:11px;margin:4px 0">{_summary}</p>'

    return html


def _render_action_plan(regime_result, sector_result, holdings, all_data,
                        total_capital, current_pct, target_pct) -> str:
    """
    V10: 渲染"今日行动清单"——基于regime+板块+持仓的调仓建议面板。
    放在市场情绪面板之后、行动指令卡之前。

    包含:
      - 仓位仪表盘（当前 vs 目标）
      - 减仓清单（BEAR模式优先展示）
      - 加仓清单（BULL/低位模式展示）
      - 换仓配对（弱势→强势）

    异常返回空串（调用方降级）。
    """
    if not HAS_ACTION_PLANNER:
        return ""
    # FIX(2026-10-08): ActionPlanner 只认英文键 schema(name/shares/buy_price/sector)，
    # 而报告主流程的 holdings 是中文键(名称/数量/成本/市值)。直传会让
    # _calc_current_position() 恒为0 → 行动清单显示"仓位0% / 可用资金=全部本金 /
    # 需加仓"，与"明日行动指令卡"的真实仓位自相矛盾；减仓/换仓清单也因读不到
    # 持仓而恒为空。此处按 daily_orders.py 的 _holdings_dict 同一口径归一化。
    ap_holdings = {}
    for _c, _h in (holdings or {}).items():
        if not isinstance(_h, dict):
            continue
        _sh = _h.get("shares", _h.get("数量", 0)) or 0
        if _sh <= 0:
            continue
        ap_holdings[_c] = {
            "name": _h.get("name") or _h.get("名称") or _c,
            "shares": _sh,
            "buy_price": _h.get("buy_price", _h.get("成本", 0)) or 0,
            "current_price": _h.get("current_price", _h.get("最新", 0)) or 0,
            "sector": _h.get("sector") or _h.get("赛道") or "",
            "market_value": _h.get("market_value", _h.get("市值", 0)) or 0,
        }
    try:
        planner = ActionPlanner(
            regime_result=regime_result,
            sector_result=sector_result,
            holdings=ap_holdings,
            all_data=all_data,
            total_capital=total_capital,
        )
        plan = planner.generate_action_plan()
    except Exception as _e:
        print(f"  ActionPlanner: 生成失败({_e})")
        return ""

    if not plan:
        return ""

    # FIX(2026-10-08): 与主流程仓位口径交叉校验，偏差>5pp 说明归一化又失效了
    _ap_pos = plan.get("current_position", 0) or 0
    if current_pct and abs(_ap_pos - current_pct) > 0.05:
        print(f"  ⚠️ ActionPlanner仓位{_ap_pos:.0%}与主流程{current_pct:.0%}偏差过大，"
              f"以主流程口径为准展示")
        plan["current_position"] = current_pct
        plan["position_gap"] = plan.get("position_target", 0.6) - current_pct

    regime_state = plan.get("regime_state", "RANGE")
    regime_action = plan.get("regime_action", "")
    reduce_list = plan.get("reduce_list", [])
    add_list = plan.get("add_list", [])
    switch_list = plan.get("switch_list", [])
    pos_target = plan.get("position_target", 0.6)
    pos_current = plan.get("current_position", 0.0)
    pos_gap = plan.get("position_gap", 0.0)
    urgency = plan.get("urgency", "hold")
    confidence = plan.get("confidence", 0.0)

    # regime状态颜色
    _state_colors = {"BULL": "#52c41a", "RANGE": "#faad14", "BEAR": "#ff4d4f"}
    _state_color = _state_colors.get(regime_state, "#999")
    _state_cn = {"BULL": "牛市", "RANGE": "震荡", "BEAR": "熊市"}.get(regime_state, regime_state)

    # 紧急度颜色
    _urgency_colors = {"immediate": "#ff4d4f", "gradual": "#faad14", "hold": "#52c41a"}
    _urg_color = _urgency_colors.get(urgency, "#999")
    _urg_text = {"immediate": "立即执行", "gradual": "分批执行", "hold": "持仓等待"}.get(urgency, urgency)

    html = '<h2>\U0001f3af 今日行动清单（V10 Regime驱动）</h2>'

    # ---- 1. 仓位仪表盘 ----
    _gap_dir = "需加仓" if pos_gap > 0.02 else ("需减仓" if pos_gap < -0.02 else "已达标")
    _gap_color = "#1976d2" if pos_gap > 0.02 else ("#cf1322" if pos_gap < -0.02 else "#389e0d")
    # FIX P0(2026-10-09): 同上，可用资金直接读取config.AVAILABLE_CASH
    _avail_cash = getattr(config, 'AVAILABLE_CASH', max(0, total_capital - pos_current * total_capital))

    # 仓位进度条
    _bar_width = min(int(pos_current * 100), 100)
    _target_left = min(int(pos_target * 100), 100)
    _bar_color = "#52c41a" if abs(pos_gap) < 0.05 else ("#ff4d4f" if pos_gap < -0.1 else "#faad14")

    html += (f'<div style="background:#f0f5ff;border:2px solid #adc6ff;border-radius:8px;padding:14px 16px;margin:8px 0">'
             f'<div style="display:flex;justify-content:space-between;align-items:center">'
             f'<div>'
             f'<b>Regime:</b> <span style="color:{_state_color};font-weight:bold;font-size:16px">{_state_cn}</span>'
             f'<span style="color:#888;margin-left:10px;font-size:12px">{regime_action}</span>'
             f'</div>'
             f'<div style="text-align:right">'
             f'<b>紧急度:</b> <span style="color:{_urg_color};font-weight:bold;font-size:14px">{_urg_text}</span>'
             f'<span style="color:#bbb;margin-left:6px;font-size:11px">置信度{confidence:.0%}</span>'
             f'</div></div>'
             # 仓位进度条
             f'<div style="margin-top:10px">'
             f'<div style="font-size:12px;color:#555;margin-bottom:4px">'
             f'仓位: <b style="font-size:14px">{pos_current:.0%}</b>'
             f' → 目标 <b style="font-size:14px">{pos_target:.0%}</b>'
             f' <span style="color:{_gap_color};font-weight:bold">({_gap_dir} {abs(pos_gap):.0%})</span>'
             f' | 可用资金 <b style="color:#1976d2">¥{_avail_cash:,.0f}</b>'
             f'</div>'
             f'<div style="background:#eee;border-radius:4px;height:12px;position:relative;width:100%">'
             f'<div style="background:{_bar_color};height:100%;width:{_bar_width}px;border-radius:4px;transition:width 0.3s"></div>'
             f'<div style="position:absolute;left:{_target_left}%;top:-2px;width:2px;height:16px;background:#333"></div>'
             f'</div>'
             f'</div></div>')

    # ---- 2. 减仓清单 ----
    if reduce_list:
        html += (f'<div style="margin:12px 0 6px;font-size:14px;font-weight:bold;color:#cf1322">'
                 f'\U0001f4c9 减仓清单（{len(reduce_list)}只）</div>')
        for item in reduce_list:
            _name = item.get("name", "")
            _code = item.get("code", "")
            _reason = item.get("reason", "")
            _shares = item.get("shares", 0)
            _pnl = item.get("pnl_pct", 0)
            _sector = item.get("sector", "")
            _priority = item.get("priority", "")
            # FIX(2026-10-08): 防御型强转——priority 历史上有 int/str 两种形态，
            # int 时 `"必减" in _priority` 会 TypeError 并把整个面板静默丢掉
            _priority = _priority if isinstance(_priority, str) else (
                "必减" if isinstance(_priority, (int, float)) and _priority >= 5 else "建议减")
            _conf = item.get("confidence", 0) or 0
            _pnl_color = "#27ae60" if _pnl >= 0 else "#cf1322"
            _pri_bg = "#fff1f0" if "必减" in _priority else "#fff7e6"
            _pri_border = "#ffa39e" if "必减" in _priority else "#ffd591"
            _pri_tag = (f' <span style="background:#cf1322;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px">'
                        f'{_priority}</span>') if _priority else ''
            html += (f'<div style="border-left:5px solid #cf1322;background:{_pri_bg};'
                     f'border:1px solid {_pri_border};border-radius:6px;padding:10px 14px;margin:6px 0">'
                     f'<div style="font-size:13px;font-weight:bold;color:#cf1322">'
                     f'{_code} {_name} <span style="font-size:11px;color:#888">[{_sector}]</span>'
                     f'{_pri_tag}'
                     f'</div>'
                     f'<div style="font-size:12px;color:#555;margin-top:4px">'
                     f'建议卖出 <b>{_shares}股</b> | '
                     f'浮盈 <span style="color:{_pnl_color};font-weight:bold">{_pnl:+.1f}%</span> | '
                     f'置信度 {_conf:.0%}'
                     f'</div>'
                     f'<div style="font-size:11px;color:#888;margin-top:2px">\U0001f4a1 {_reason}</div>'
                     f'</div>')

    # ---- 3. 加仓清单 ----
    if add_list:
        html += (f'<div style="margin:12px 0 6px;font-size:14px;font-weight:bold;color:#389e0d">'
                 f'\U0001f4c8 加仓清单（{len(add_list)}只）</div>')
        for item in add_list:
            _name = item.get("name", "")
            _code = item.get("code", "")
            _reason = item.get("reason", "")
            _sector = item.get("sector", "")
            _buy_range = item.get("buy_range", "")
            _stop = item.get("stop_loss", 0)
            _conf = item.get("confidence", 0)
            _stop_html = f' | 止损 <b style="color:#cf1322">{_stop:.2f}</b>' if _stop > 0 else ''
            html += (f'<div style="border-left:5px solid #389e0d;background:#f6ffed;'
                     f'border:1px solid #b7eb8f;border-radius:6px;padding:10px 14px;margin:6px 0">'
                     f'<div style="font-size:13px;font-weight:bold;color:#389e0d">'
                     f'{_code} {_name} <span style="font-size:11px;color:#888">[{_sector}]</span>'
                     f'</div>'
                     f'<div style="font-size:12px;color:#555;margin-top:4px">'
                     f'买入区间 <b style="color:#1976d2">{_buy_range}</b>'
                     f'{_stop_html}'
                     f' | 置信度 {_conf:.0%}'
                     f'</div>'
                     f'<div style="font-size:11px;color:#888;margin-top:2px">\U0001f4a1 {_reason}</div>'
                     f'</div>')

    # ---- 4. 换仓配对 ----
    if switch_list:
        html += (f'<div style="margin:12px 0 6px;font-size:14px;font-weight:bold;color:#1565c0">'
                 f'\U0001f501 换仓配对（{len(switch_list)}组）</div>')
        for pair in switch_list:
            # FIX(2026-10-08): ActionPlanner._identify_switch_pairs 输出的是扁平键
            # (sell_code/sell_name/buy_code/buy_name)，而此处只读嵌套的 sell/buy 字典，
            # 导致换仓配对渲染成"卖出  → 买入 "空白行。兼容两种形态。
            _sell = pair.get("sell") or {
                "code": pair.get("sell_code", ""), "name": pair.get("sell_name", ""),
                "sector": pair.get("sell_sector", "")}
            _buy = pair.get("buy") or {
                "code": pair.get("buy_code", ""), "name": pair.get("buy_name", ""),
                "sector": pair.get("buy_sector", "")}
            _reason = pair.get("reason", "")
            _conf = pair.get("confidence", 0) or 0
            html += (f'<div style="background:#f0f7ff;border:1px solid #b3d9ff;border-radius:6px;'
                     f'padding:10px 14px;margin:6px 0">'
                     f'<div style="font-size:13px">'
                     f'卖出 <b style="color:#cf1322">{_sell.get("code", "")} {_sell.get("name", "")}</b>'
                     f' <span style="font-size:11px;color:#888">[{_sell.get("sector", "")}]</span>'
                     f' → 买入 <b style="color:#389e0d">{_buy.get("code", "")} {_buy.get("name", "")}</b>'
                     f' <span style="font-size:11px;color:#888">[{_buy.get("sector", "")}]</span>'
                     f'</div>'
                     f'<div style="font-size:11px;color:#888;margin-top:4px">\U0001f4a1 {_reason} (置信度{_conf:.0%})</div>'
                     f'</div>')

    # ---- 5. 无操作时 ----
    if not reduce_list and not add_list and not switch_list:
        html += ('<div style="background:#f6ffed;border:1px solid #b7eb8f;border-radius:6px;'
                 'padding:12px 16px;margin:10px 0;color:#389e0d;font-weight:bold">'
                 '✅ 当前持仓与regime匹配，无需调仓操作</div>')

    # ---- 6. 底部说明 ----
    _pos_limits = planner.playbook.position_limits()
    _risk_rules = planner.playbook.risk_rules()
    html += ('<p style="color:#888;font-size:11px;margin:6px 0">'
             '⚙️ 行动清单由RegimePlaybook驱动: '
             f'{_state_cn}模式下'
             f'选股门槛{planner.playbook.pass_threshold()}分 | '
             f'仓位上限{_pos_limits.get("max_total", 0.6):.0%} | '
             f'止损{_risk_rules.get("stop_loss_pct", 0.06):.0%}'
             ' | 基于收盘数据生成，次一交易日适用</p>')

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

    # FIX P2(2026-10-09): MA NaN 回退修复——rolling()数据不足时值为NaN，
    # dict.get(key, default) 不会触发回退（key存在但value=NaN），需显式检查 pd.isna()
    def _safe_get(series, key, fallback):
        v = series.get(key, fallback)
        return fallback if (v is None or (isinstance(v, float) and pd.isna(v))) else v

    ma5 = _safe_get(latest, "ma5", close)
    ma10 = _safe_get(latest, "ma10", close)
    ma20 = _safe_get(latest, "ma20", close)
    ma60 = _safe_get(latest, "ma60", close)
    rsi = _safe_get(latest, "rsi", 50)
    macd_dif = _safe_get(latest, "macd_dif", 0)
    macd_dea = _safe_get(latest, "macd_dea", 0)
    macd_hist = _safe_get(latest, "macd_hist", 0)
    atr = _safe_get(latest, "atr", close * 0.02)
    boll_upper = _safe_get(latest, "boll_upper", close * 1.1)
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
            # FIX(2026-08-26) BUG-05: K线新鲜度校验 —— 检测本地DB数据截止日，
            # 超过1个交易日的数据在报告中标注陈旧风险（技术评分基于过期指标）。
            _kline_last_date = str(df.index[-1].date()) if hasattr(df.index[-1], 'date') else str(df.index[-1])[:10]
            _kline_fresh = True
            _today_str = datetime.date.today().isoformat()
            try:
                _days_gap = (datetime.date.fromisoformat(_today_str) - datetime.date.fromisoformat(_kline_last_date)).days
                if _days_gap > 1:
                    _kline_fresh = False
                    print(f"  [数据质量] {code}: 数据缺口{_days_gap}天(最新{_kline_last_date})，技术评分可能失真")
            except Exception:
                pass
            # FIX P1(2026-10-09): 统一技术指标数据源 —— 实时行情(tencent)与历史K线(baostock)
            # 收盘价可能不同（复权算法/精度差异），导致MA/RSI/MACD与展示价格错位。
            # 修复：将最后一行OHLC替换为实时行情值，确保指标基于与展示价一致的数据计算。
            _rt_q = quotes.get(code, {})
            _rt_px = _rt_q.get("price", 0)
            if _rt_px > 0:
                _last_date = str(df["date"].iloc[-1])[:10] if "date" in df.columns else ""
                _today_str = datetime.date.today().isoformat()
                try:
                    if _last_date == _today_str:
                        # 历史数据已含今日→替换OHLC为实时值
                        df.iloc[-1, df.columns.get_loc("open")] = float(_rt_q.get("open", _rt_px) or _rt_px)
                        df.iloc[-1, df.columns.get_loc("close")] = float(_rt_px)
                        df.iloc[-1, df.columns.get_loc("high")] = float(_rt_q.get("high", _rt_px) or _rt_px)
                        df.iloc[-1, df.columns.get_loc("low")] = float(_rt_q.get("low", _rt_px) or _rt_px)
                    elif _last_date < _today_str:
                        # 历史数据不含今日→追加实时行为新行
                        _new_row = pd.DataFrame([{
                            "date": _today_str, "open": float(_rt_q.get("open", _rt_px) or _rt_px),
                            "close": float(_rt_px), "high": float(_rt_q.get("high", _rt_px) or _rt_px),
                            "low": float(_rt_q.get("low", _rt_px) or _rt_px), "volume": 0
                        }])
                        df = pd.concat([df, _new_row], ignore_index=True)
                except Exception as _ue:
                    print(f"  [数据质量] {code}: 实时数据合并失败({_ue})，沿用原历史数据计算指标")
            df = compute_indicators(df)
            hist_dataframes[code] = df  # 保存原始数据
            rt_price = quotes.get(code, {}).get("price", 0)
            tech_analysis[code] = analyze_technical(df, rt_price)
            comp = tech_analysis[code].get("composite", 0)
            trend = tech_analysis[code].get("trend_dir", "")
            _freshness_tag = "" if _kline_fresh else " ⚠️陈旧"
            print(f"  {code} {item['名称']}: {len(df)}根K线 | 评分{comp} | {trend}{_freshness_tag}")
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

    # FIX(2026-08-26) BUG-02: 持久化止损参与Ratchet取大 —— holdings.json中已抬升的止损
    # 不应被丢弃（原实现stop_loss_cfg读入后全文零使用，导致长春高新止损87.001已破却漏报）
    _stop_loss_cfg = item.get("stop_loss_cfg", 0)
    if _stop_loss_cfg > 0:
        stop_loss = max(stop_loss, _stop_loss_cfg)

    # 成本/持仓数据
    shares = item.get("shares", 0)
    buy_price = item.get("buy_price", 0)
    highest_hist = item.get("highest", 0)

    # FIX(2026-08-26) BUG-01 + FIX P0(2026-10-09): 成本口径交叉验证 —— 用券商盈亏反推隐含成本价，
    # 当buy_price(买入均价)与券商持仓盈亏使用的成本价偏差>0.5%时，修正为隐含成本价。
    # FIX P0(2026-10-09): 原逻辑用实时价latest反推成本，但broker_pnl基于JSON快照时的价格计算，
    # 跨日/盘中价格变动导致隐含成本失真（如603002成本被篡改为17.645，真实18.655）。
    # 修正：使用json_current_price（与broker_pnl同时间口径）代替latest。
    _broker_pnl = item.get("broker_pnl")
    _json_px = item.get("json_current_price", 0)
    _cost_corrected = False
    if _broker_pnl is not None and shares > 0 and _json_px > 0 and buy_price > 0:
        _implied_cost = _json_px - _broker_pnl / shares
        if _implied_cost > 0:
            _cost_drift = abs(buy_price / _implied_cost - 1)
            if _cost_drift > 0.005:  # 偏差>0.5%触发修正
                buy_price = round(_implied_cost, 4)
                _cost_corrected = True

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
        _result = forecaster.analyze_stock(_code, _df, _holding_en, _regime=regime_result.get("state") if regime_result else None)
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

# --- 6. 板块背离检测（失败降级不影响主流程） ---
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
        # ---- FIX(2026-08-28): 报告发送前最终一致性关卡 ----
        # 情绪数据与实际持仓表现严重矛盾时插入警示横幅，
        # 不阻塞发送（报告其他段落仍有价值），但让用户一眼看到数据可疑。
        _pulse_warn = None
        _act = _pulse_data.get("activity", {}) or {}
        _p_up = _act.get("up")
        _p_lu = _act.get("limit_up")
        _p_level = _pulse_data.get("level", "")
        if _p_up is not None and _p_lu is not None and holdings:
            # 持仓标的当日实际涨跌统计（行情已加载）
            _h_up_count = sum(1 for h in holdings.values()
                              if h.get("涨跌幅", 0) > 0)
            _h_total = len(holdings)
            # 矛盾检测: 持仓多数上涨但情绪"恐慌"，或持仓多数下跌但情绪"狂热"
            if _h_total > 0:
                _h_up_ratio = _h_up_count / _h_total
                if _h_up_ratio >= 0.6 and _p_level == "恐慌":
                    _pulse_warn = (f"⚠️ 数据矛盾警示: 持仓{_h_up_count}/{_h_total}只上涨"
                                   f"({_h_up_ratio:.0%})，但市场情绪显示\"恐慌\""
                                   f"——涨跌家数数据可能陈旧，请以实际行情为准")
                elif _h_up_ratio <= 0.3 and _p_level == "狂热":
                    _pulse_warn = (f"⚠️ 数据矛盾警示: 持仓{_h_up_count}/{_h_total}只上涨"
                                   f"({_h_up_ratio:.0%})，但市场情绪显示\"狂热\""
                                   f"——涨跌家数数据可能异常，请以实际行情为准")
        html += render_pulse_html(_pulse_data)
        if _pulse_warn:
            html += (f'<div class="alert" style="background:#fff3e0;'
                     f'border-left:4px solid #ff9800;margin-top:4px;'
                     f'padding:8px 12px;font-size:13px;color:#e65100">'
                     f'{_pulse_warn}</div>')
            print(f"  ⚠️ 情绪数据矛盾: {_pulse_warn[:60]}...")
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

# ---- 📡 板块景气雷达（V9.1: 接入SectorMonitor，强势板块排名+持仓健康度+轮动信号） ----
_sector_radar_result = None
if HAS_SECTOR_FLOW:
    try:
        _sm = SectorMonitor()
        # 复用已加载的K线数据（hist_dataframes）
        _sm_holdings_data = {}
        for _c, _df in hist_dataframes.items():
            if _df is not None and not _df.empty:
                _sm_holdings_data[_c] = _df
        _sm_holdings_info = {}
        for _c, _h in holdings.items():
            if _h.get("数量", 0) > 0:
                _sm_holdings_info[_c] = {"name": _h.get("名称", _c)}
        _sector_radar_result = _sm.analyze(_sm_holdings_data, _sm_holdings_info)
        _ranked_cnt = len(_sector_radar_result.get("ranked", []))
        _signal_cnt = len(_sector_radar_result.get("signals", []))
        _alert_cnt = len(_sector_radar_result.get("alerts", []))
        print(f"  板块景气雷达: {_ranked_cnt}板块排名 | {_signal_cnt}轮动信号 | {_alert_cnt}持仓预警")
    except Exception as _sme:
        _sector_radar_result = None
        print(f"  板块景气雷达: 失败({_sme})")

if _sector_radar_result:
    _radar_html = _render_sector_radar(_sector_radar_result, holdings)
    if _radar_html:
        html += _radar_html

# V9.2: 盘中板块异动横幅（实时akshare数据，捕捉当日爆发板块）
try:
    _surge_result = detect_intraday_sector_surges(threshold_pct=2.0, top_n=5)
    if _surge_result.get("available"):
        _surge_banner = render_intraday_sector_banner(_surge_result)
        if _surge_banner:
            html += _surge_banner
            _top_names = [s["name"] for s in _surge_result.get("top_sectors", [])[:3]]
            print(f"  板块异动: TOP3={_top_names} surge={_surge_result.get('surge_detected')}")
except Exception as _surge_e:
    print(f"  板块异动: 失败({_surge_e})")

# ---- 🎯 V10: 今日行动清单（Regime驱动调仓建议，放在板块雷达之后、风控之前） ----
try:
    _action_plan_html = _render_action_plan(
        regime_result=regime_result,
        sector_result=_sector_radar_result,
        holdings=holdings,
        all_data=hist_dataframes,
        total_capital=_CAPITAL,
        current_pct=current_pct,
        target_pct=target_pct,
    )
    if _action_plan_html:
        html += _action_plan_html
        print(f"  V10行动清单: 已生成")
except Exception as _ape:
    print(f"  V10行动清单: 渲染失败({_ape})，已降级跳过")

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

# ---- 🎯 明日行动指令卡（V5重构: 合并执行清单+调仓+决策矩阵+板块轮动+反冲动锁） ----
try:
    _spot_html = _render_action_spotlight(
        holdings, regime_result=regime_result, vol_result=vol_result,
        target_pct=target_pct, current_pct=current_pct, current_mv=current_mv,
        _CAPITAL=_CAPITAL, sell_candidates=sell_candidates,
        recommendations=recommendations, sector_divergence=sector_divergence,
        forecast_results=forecast_results, corr_penalty_tags=corr_penalty_tags,
        anti_impulse_warnings=anti_impulse_warnings)
    if _spot_html:
        html += _spot_html
        print(f"  明日行动指令卡: 已生成")
except Exception as _spe:
    print(f"  明日行动指令卡: 渲染失败({_spe})，已降级跳过")

# ---- 风险预警 ----
html += '<div class="alert alert-warning" style="margin:8px 0">⚠️ 仓位风险预警</div>'

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

# [V5删除] 回本目标计划（数学展示无操作价值）

# ---- 总览表 ----
html += '<h2>一、持仓综合总览（行情+盈亏+操作建议）</h2>'
html += f'<div class="alert alert-info">📡 行情源: {data_source} | 时间: {quote_time_str or now} | 成本口径: 券商成本价(交叉验证修正) | 止损规则: 浮亏持仓按成本×{1-STOP_LOSS_PCT:.0%}硬止损(Ratchet只升不降+持久止损取大)，浮盈持仓按最新价×{1-STOP_LOSS_PCT:.0%} | 止盈基于压力位/ATR推算</div>'

html += '<table><tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>最新价</th><th>盈亏%</th><th>仓位%</th><th>技术评分</th><th>止损价</th><th>止盈目标</th><th>操作建议</th></tr>'
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
    html += f'<td style="color:{score_color};font-weight:bold">{comp:.1f}</td>'

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

# [V5删除] 组合风控行动项/资金仓位智能规划（已合并入行动指令卡）

# [V5删除] 操作回顾/执行确认（与盘后日报重复/内部核对工具）

# [V5删除] 执行清单/大盘卡片/减仓加仓/决策矩阵/反冲动锁/板块轮动（已合并入明日行动指令卡）
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
            try:
                _cp_r = _caopan_engine.analyze(_cp_df, code=_cp_code, name=_cp_h["名称"])
                if "error" not in _cp_r:
                    _caopan_results_map[_cp_code] = _cp_r
            except Exception as _cp_stock_e:
                # FIX(2026-08-26) BUG-10: 单只操盘密码失败不影响其余标的（原实现一只KeyError全模块降级）
                print(f"  操盘密码[{_cp_code}]: 跳过({_cp_stock_e})")
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
    all_signals = tech["trend_signals"] + [tech["vol_signal"]]  # V5精简: 仅保留趋势+量能信号

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

    # ---- V24: 加仓计划情绪门控（恐慌/熔断环境不生成加仓计划，修复"建议减仓同时出加仓计划"矛盾）----
    _add_plan_env_blocked = False
    _add_plan_block_reason = ""
    try:
        _pulse_temp = (locals().get('_pulse_data') or {}).get('temperature')
        _pulse_level = (locals().get('_pulse_data') or {}).get('level', '')
        if _pulse_temp is not None and _pulse_temp <= 40:
            _add_plan_env_blocked = True
            _add_plan_block_reason = f"情绪恐慌({_pulse_temp}分)"
    except Exception:
        pass
    if not _add_plan_env_blocked:
        try:
            from risk.risk_control import StrategyFailureDetector
            _sfd_st = StrategyFailureDetector().get_status()
            if _sfd_st.get("current_level") in ("breaker", "pause"):
                _add_plan_env_blocked = True
                _add_plan_block_reason = f"策略{_sfd_st['current_level']}"
        except Exception:
            pass

    # ---- V2.8: 加仓计划分流（回调加仓优先，突破加仓带守卫；修复与②止盈单同价冲突）----
    _ma20_val = tech.get("ma20", latest)
    _resistances = tech.get("resistances", [])
    # 回调加仓风控预检（复用已加载的 hist_dataframes，零额外网络请求；异常降级为不通过）
    _pullback_risk = {"pass": False, "reason": "回调加仓检测未执行", "add_shares": 0}
    if _add_plan_env_blocked:
        _pullback_risk = {"pass": False, "reason": f"环境门控({_add_plan_block_reason})，跳过加仓检测", "add_shares": 0}
    else:
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
        # V25(P1-4): 突破加仓同样受情绪门控约束，恐慌环境不出加仓计划
        _no_reduce = not any(k in action for k in ("减仓", "止损", "清仓"))
        if comp >= 60 and _no_reduce and not _add_plan_env_blocked:
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
    _cost_note = '（占位·已回本）' if h.get('占位成本') else '<span style="font-size:10px;color:#999">(成本价)</span>'
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
{('<tr><td><b>④ 回落卖出</b></td><td style="color:#ff9800">最高' + f'{h.get("回落基准", h["最高"]):.3f}' + f'回落{h.get("回落比例", 0.07)*100:.0f}%至 {h["回落触发"]:.3f} 卖出{_dist_str(h["回落触发"])}</td><td>★★建议</td><td>10天</td></tr>') if h.get('回落触发', 0) > 0 else '<tr><td><b>④ 回落卖出</b></td><td style="color:#999;font-size:11px" colspan="3">⚠️ 持仓期最高价未记录，无法生成回落卖出条件单（请运行 update_holdings.py 补写 highest 字段）</td></tr>'}
<tr><td><b>⑤ 逻辑止损(提前退出)</b></td><td style="color:#ff9800;font-size:11px">{_logic_exit_html}</td><td>★★★心法</td><td>每日盘后检查</td></tr>
</table>
{_overnight_hint}
{add_card_html}
{_caopan_card_row}


</div></div>"""

# [V5删除] 次日调仓计划（已合并入明日行动指令卡）

# [V5删除] 7个系统指标区块（胜率/偏差/归因/滑点/预警/健康度/排名）
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
html += '<h2>附录：参考数据</h2>'
html += '<div class="alert alert-info" style="font-size:12px">以下数据仅供参考，仅展示有信号的标的</div>'
html += '<h3>龙虎榜/游资动向</h3>'
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
html += '<h3>融资融券信号</h3>'
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
html += '<h3>解禁风险预警</h3>'
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
html += '<h3>四级资金流向</h3>'
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

# [V5删除] 持仓未来趋势预判（与逐只卡片技术分析重复）

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

# ---- FIX(2026-08-28) P2: 报告发送前摘要打印（供日志事后排查） ----
try:
    if HAS_MARKET_PULSE:
        from trading_system.strategy.market_pulse import get_pulse_summary
        _summary = get_pulse_summary()
        _h_up_n = sum(1 for h in holdings.values() if h.get("涨跌幅", 0) > 0)
        _h_total_n = len(holdings)
        print(f"[报告摘要] 情绪:{_summary.get('level', '?')}"
              f"({_summary.get('temperature', '?')}分) | "
              f"涨/跌:{_summary.get('up', '?')}/{_summary.get('down', '?')} | "
              f"涨停/跌停:{_summary.get('limit_up', '?')}/{_summary.get('limit_down', '?')} | "
              f"成交额:{_summary.get('amount', '?')}亿 | "
              f"持仓涨跌:{_h_up_n}/{_h_total_n}")
        if _summary.get("warnings"):
            for _sw in _summary["warnings"]:
                print(f"  ⚠️ 数据质量: {_sw}")
        if _summary.get("degraded"):
            print(f"  ⚠️ 降级: {'；'.join(_summary['degraded'])}")
except Exception as _rse:
    print(f"[报告摘要] 打印失败({_rse})")

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
