"""
盘前作战计划 V2.0 - 中线波段版
================================
每个交易日08:30发送，回答核心问题：今天要做什么？

3个板块（聚焦今日操作）:
1. 今日操作清单 - 具体到"价格+动作+数量"
2. 持仓关键价位速查 - 止损/目标/现价/距离
3. 大盘环境 - 一句话

使用方式:
    from output.morning_brief import send_morning_brief, prepare_morning_brief_data
    brief_data = prepare_morning_brief_data(holdings, data_dict, ...)
    send_morning_brief(brief_data)
"""

import os
import sys
import datetime
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ============================================================
# 一、数据准备
# ============================================================

def _calc_holding_days(buy_date_str: str) -> int:
    """计算持仓交易日数"""
    try:
        buy_date = datetime.datetime.strptime(buy_date_str, "%Y-%m-%d").date()
        delta = (datetime.date.today() - buy_date).days
        return max(1, int(delta / 1.4))
    except Exception:
        return 0


def _judge_wave_status(row) -> str:
    """判断波段状态"""
    close = row.get("close", 0)
    ma5 = row.get("ma5", close)
    ma10 = row.get("ma10", close)
    ma20 = row.get("ma20", close)
    ma20_slope = row.get("ma20_slope", 0)

    if ma20 == 0:
        return "数据不足"
    if ma5 > ma10 > ma20 and close > ma20:
        return "上升波段"
    elif close < ma20 and ma20_slope <= 0:
        return "破位预警"
    elif close < ma5 and ma20_slope > 0:
        return "回调中"
    elif abs((close - ma20) / ma20 * 100) < 3:
        return "横盘整理"
    elif close > ma20:
        return "上升波段"
    else:
        return "弱势"


def prepare_morning_brief_data(holdings: dict, data_dict: dict,
                                market_strength: str = "normal",
                                max_pos: float = 0.7,
                                signals: list = None) -> dict:
    """
    准备盘前作战计划数据

    参数:
        holdings: 持仓字典 {code: {shares, buy_price, buy_date, ...}}
        data_dict: 行情数据 {code: DataFrame}
        market_strength: 大盘状态
        max_pos: 建议最大仓位
        signals: 信号列表 [(code, sig), ...]
    """
    return {
        "holdings": holdings,
        "data_dict": data_dict,
        "market_strength": market_strength,
        "max_pos": max_pos,
        "signals": signals or [],
    }


# ============================================================
# 二、HTML构建
# ============================================================

_CSS = """
body { font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 10px; background: #f0f2f5; }
.container { max-width: 850px; margin: 0 auto; background: #fff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.08); overflow: hidden; }
.header { background: linear-gradient(135deg, #e65100, #f57c00); color: #fff; padding: 20px 24px; }
.header h1 { margin: 0; font-size: 20px; font-weight: 600; }
.header .meta { font-size: 12px; opacity: 0.85; margin-top: 6px; }
.section { padding: 16px 24px; border-bottom: 1px solid #f0f0f0; }
.section:last-child { border-bottom: none; }
.section-title { font-size: 15px; font-weight: 600; color: #e65100; margin: 0 0 12px 0; padding-left: 10px; border-left: 3px solid #f57c00; }
table { width: 100%; border-collapse: collapse; font-size: 12px; margin: 8px 0; }
th { background: #fff3e0; color: #e65100; padding: 8px 6px; text-align: center; font-weight: 600; }
td { padding: 7px 6px; border-bottom: 1px solid #f5f5f5; text-align: center; }
.text-red { color: #d32f2f !important; }
.text-green { color: #2e7d32 !important; }
.text-orange { color: #e65100 !important; }
.action-item { padding: 10px 14px; margin: 6px 0; border-radius: 8px; font-size: 13px; line-height: 1.6; }
.action-exec { background: #fff7e6; border-left: 4px solid #fa8c16; }
.action-watch { background: #f6ffed; border-left: 4px solid #52c41a; }
.action-urgent { background: #fff1f0; border-left: 4px solid #d32f2f; }
.market-bar { background: #f5f5f5; border-radius: 8px; padding: 12px 16px; font-size: 14px; text-align: center; }
.footer { padding: 12px 24px; text-align: center; font-size: 11px; color: #999; background: #fafafa; }
"""


def _build_today_actions(brief_data: dict) -> str:
    """板块1: 今日操作清单"""
    holdings = brief_data.get("holdings", {})
    data_dict = brief_data.get("data_dict", {})
    signals = brief_data.get("signals", [])

    # 信号映射
    sig_map = {}
    for item in signals:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            sig_map[item[0]] = item[1]

    actions = []  # (priority, html)

    for code, pos in holdings.items():
        df = data_dict.get(code)
        if df is None or df.empty:
            continue

        row = df.iloc[-1]
        close = row["close"]
        buy_price = pos.get("buy_price", 0)
        shares = pos.get("shares", 0)
        name = config.get_stock_name(code)
        holding_days = _calc_holding_days(pos.get("buy_date", ""))
        pnl_pct = (close - buy_price) / buy_price * 100 if buy_price > 0 else 0

        sig = sig_map.get(code, {})
        stop_price = sig.get("stop_loss_current", 0) or sig.get("stop_loss_initial", 0)
        if not stop_price:
            stop_price = buy_price * 0.90
        target1 = buy_price * 1.10

        wave = _judge_wave_status(row)
        ma20 = row.get("ma20", 0)
        dist_to_stop = (close - stop_price) / close * 100 if close > 0 else 0

        # 紧急: 破位或距止损<3%
        if wave == "破位预警" or dist_to_stop < 3:
            actions.append((1, f"""<div class="action-item action-urgent">
                <b>[紧急]</b> {code} {name}: 挂止损单 盘中价<={stop_price:.2f} 卖出全部{shares}股
            </div>"""))

        # 执行: 到达目标
        elif close >= target1:
            sell_shares = int(shares / 3 / 100) * 100
            actions.append((2, f"""<div class="action-item action-exec">
                <b>[执行]</b> {code} {name}: 挂止盈单 盘中价>={target1:.2f} 卖出{sell_shares}股
            </div>"""))

        # 卖出信号
        elif sig.get("sell_signal"):
            reason = sig.get("signal_reason", "卖出信号")
            actions.append((2, f"""<div class="action-item action-exec">
                <b>[执行]</b> {code} {name}: {reason}，盘中卖出
            </div>"""))

        # 观察: 回调
        elif wave == "回调中":
            actions.append((4, f"""<div class="action-item action-watch">
                <b>[观察]</b> {code} {name}: 若回踩{ma20:.2f}企稳，14:00后可考虑加仓
            </div>"""))

        # 时间预警
        elif holding_days > 30 and pnl_pct < 3:
            actions.append((3, f"""<div class="action-item action-watch">
                <b>[时间预警]</b> {code} {name}: 持仓{holding_days}天浮盈{pnl_pct:.1f}%，关注是否换股
            </div>"""))

    if not actions:
        actions.append((5, """<div class="action-item action-watch">
            <b>[无操作]</b> 所有持仓正常，今日无需操作，继续持有
        </div>"""))

    actions.sort(key=lambda x: x[0])

    return f"""
<div class="section">
    <h2 class="section-title">今日操作清单</h2>
    {''.join(a[1] for a in actions)}
</div>"""


def _build_price_table(brief_data: dict) -> str:
    """板块2: 持仓关键价位速查"""
    holdings = brief_data.get("holdings", {})
    data_dict = brief_data.get("data_dict", {})
    signals = brief_data.get("signals", [])

    sig_map = {}
    for item in signals:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            sig_map[item[0]] = item[1]

    rows = []
    for code, pos in holdings.items():
        df = data_dict.get(code)
        if df is None or df.empty:
            continue

        row = df.iloc[-1]
        close = row["close"]
        buy_price = pos.get("buy_price", 0)
        name = config.get_stock_name(code)

        sig = sig_map.get(code, {})
        stop_price = sig.get("stop_loss_current", 0) or sig.get("stop_loss_initial", 0)
        if not stop_price:
            stop_price = buy_price * 0.90
        target1 = buy_price * 1.10
        target2 = buy_price * 1.20

        dist_stop = (close - stop_price) / close * 100 if close > 0 else 0
        dist_target = (target1 - close) / close * 100 if close > 0 else 0

        stop_class = "text-red" if dist_stop < 5 else ""
        target_class = "text-green" if dist_target < 5 else ""

        rows.append(f"""<tr>
            <td>{code}</td>
            <td>{name}</td>
            <td class="text-red">{stop_price:.2f}</td>
            <td class="text-green">{target1:.2f}</td>
            <td>{target2:.2f}</td>
            <td>{close:.3f}</td>
            <td class="{stop_class}">{dist_stop:.1f}%</td>
            <td class="{target_class}">{dist_target:.1f}%</td>
        </tr>""")

    return f"""
<div class="section">
    <h2 class="section-title">持仓关键价位速查</h2>
    <table>
        <tr><th>代码</th><th>名称</th><th>止损价</th><th>目标1</th><th>目标2</th><th>现价</th><th>距止损</th><th>距目标</th></tr>
        {''.join(rows)}
    </table>
    <div style="font-size:11px;color:#999;margin-top:6px;">* 所有价位均为盘中实时触发，非收盘价</div>
</div>"""


def _build_market_env(brief_data: dict) -> str:
    """板块3: 大盘环境（一句话）"""
    market_strength = brief_data.get("market_strength", "normal")
    max_pos = brief_data.get("max_pos", 0.7)

    state_map = {
        "strong": f"强势市场，仓位可至{max_pos:.0%}，趋势股持有为主",
        "normal": f"震荡市，仓位控制{max_pos:.0%}以内，高抛低吸",
        "weak": f"弱势市场，仓位控制{max_pos:.0%}以内，防守为主，减少操作",
    }
    desc = state_map.get(market_strength, "未知状态")

    return f"""
<div class="section">
    <h2 class="section-title">大盘环境</h2>
    <div class="market-bar">{desc}</div>
</div>"""


def _build_quant_morning(brief_data: dict) -> str:
    """板块4: 量化晨报 - 今日风控要点"""
    try:
        from quant.report_enhancer import QuantReportEnhancer
        enhancer = QuantReportEnhancer()
        panel = enhancer.compute_panel(
            brief_data.get("holdings", {}),
            brief_data.get("data_dict", {}),
        )
    except Exception:
        return ""

    if not panel:
        return ""

    # 提取关键信息
    mr = panel.get("market_regime", {})
    regime = mr.get("regime", "未知")
    regime_color = mr.get("regime_color", "#999")
    sentiment = mr.get("sentiment", 50)
    pos_limit = mr.get("position_limit", 0.7)

    rs = panel.get("risk_status", {})
    alerts = rs.get("alert_count", 0)
    details = rs.get("details", [])
    alert_items = [d for d in details if d.get("status") != "正常"]

    fh = panel.get("factor_health", {})
    fh_verdict = fh.get("verdict", "")

    overall = panel.get("overall_score", 0)

    # 构建预警列表
    alert_html = ""
    for a in alert_items[:3]:
        code = a.get("code", "")
        name = config.get_stock_name(code)
        status = a.get("status", "")
        atr_stop = a.get("atr_stop", 0)
        alert_html += f'<div style="padding:6px 10px;margin:4px 0;background:#fff1f0;border-radius:6px;font-size:12px;">⚠️ {code} {name}: {status}，ATR止损{atr_stop:.2f}</div>'

    if not alert_html:
        alert_html = '<div style="padding:6px 10px;background:#f6ffed;border-radius:6px;font-size:12px;">✅ 无风控预警，正常持有</div>'

    return f"""
<div class="section" style="background:#f8f9ff;">
    <h2 class="section-title" style="border-left-color:#ff6f00;">🧠 量化晨报</h2>
    <div style="display:flex;justify-content:space-between;margin-bottom:10px;">
        <span>市场: <b style="color:{regime_color}">{regime}</b> (情绪{sentiment}分)</span>
        <span>仓位上限: <b>{pos_limit:.0%}</b></span>
        <span>综合评分: <b style="color:{'#2e7d32' if overall >= 70 else '#f57c00' if overall >= 50 else '#d32f2f'}">{overall:.0f}</b></span>
    </div>
    <div style="font-size:12px;color:#555;margin-bottom:8px;">因子状态: {fh_verdict}</div>
    <div style="font-size:13px;font-weight:600;margin-bottom:6px;">今日风控要点:</div>
    {alert_html}
</div>"""


# ============================================================
# 三、主构建 + 发送
# ============================================================

def _build_discipline_morning(brief_data: dict) -> str:
    """板块: 今日操作预算 + 纪律约束（P0 行为纠偏）"""
    try:
        from quant.risk_manager import RiskManager
        rm = RiskManager()
    except Exception:
        return ""

    holdings = brief_data.get("holdings", {})
    trade_history = brief_data.get("trade_history", [])
    sector_map = brief_data.get("sector_map", {})
    today = datetime.date.today().strftime("%Y-%m-%d")

    # 冷却期检查
    cooldown_list = []
    if trade_history:
        sell_records = [t for t in trade_history if t.get("direction") == "sell"]
        for t in sell_records:
            cd = rm.check_cooldown(t.get("code", ""), t.get("date", ""), today)
            if cd["in_cooldown"]:
                cooldown_list.append(cd)

    # 最小持仓检查
    min_hold_list = []
    for code, pos in holdings.items():
        buy_date = pos.get("buy_date", "")
        if buy_date:
            chk = rm.check_min_holding_days(buy_date, today)
            if not chk["can_sell"]:
                chk["code"] = code
                min_hold_list.append(chk)

    # 行业暴露
    sector_report = rm.check_sector_concentration(holdings, sector_map)
    sector_exposure = sector_report.get("sector_exposure", {})
    violations = sector_report.get("violations", [])

    # 构建冷却期HTML
    cooldown_html = ""
    for cd in cooldown_list[:5]:
        code = cd.get("code", "")
        name = config.get_stock_name(code)
        days_left = cd.get("remaining_days", 0)
        cooldown_html += f'<span style="display:inline-block;background:#fff1f0;border:1px solid #ffa39e;border-radius:4px;padding:2px 8px;margin:2px;font-size:11px;">⛔ {code} {name} (剩{days_left}天)</span>'

    # 构建最小持仓HTML
    min_hold_html = ""
    for mh in min_hold_list[:5]:
        code = mh.get("code", "")
        name = config.get_stock_name(code)
        h_days = mh.get("holding_days", 0)
        min_hold_html += f'<span style="display:inline-block;background:#fffbe6;border:1px solid #ffe58f;border-radius:4px;padding:2px 8px;margin:2px;font-size:11px;">🔒 {code} {name} (持{h_days}天,禁卖)</span>'

    # 行业暴露HTML
    sector_html = ""
    sorted_sectors = sorted(sector_exposure.items(), key=lambda x: x[1], reverse=True)[:4]
    for s_name, s_pct in sorted_sectors:
        s_color = "#d32f2f" if s_pct > 0.30 else "#f57c00" if s_pct > 0.20 else "#2e7d32"
        sector_html += f'<span style="margin-right:10px;font-size:12px;"><b style="color:{s_color}">{s_name} {s_pct:.0%}</b></span>'

    return f"""
<div class="section" style="background:#fff8f0;border-left:4px solid #fa541c;">
    <h2 class="section-title" style="border-left-color:#fa541c;">🎯 今日操作预算</h2>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <span style="font-size:18px;font-weight:700;color:#fa541c;">今日最多操作: 3笔</span>
        <span style="font-size:12px;color:#999;">含买卖 | 超限请停手反思</span>
    </div>
    {'<div style="margin:6px 0;"><b style="font-size:12px;color:#d32f2f;">⛔ 冷却期标的(禁买):</b><br>' + cooldown_html + '</div>' if cooldown_html else ''}
    {'<div style="margin:6px 0;"><b style="font-size:12px;color:#d48806;">🔒 未达最小持仓(禁卖):</b><br>' + min_hold_html + '</div>' if min_hold_html else ''}
    {'<div style="margin:6px 0;"><b style="font-size:12px;">📊 行业暴露:</b> ' + sector_html + ('<span style="color:#d32f2f;font-size:11px;"> ⚠️超限!</span>' if violations else '') + '</div>' if sector_html else ''}
    <div style="font-size:11px;color:#999;margin-top:8px;border-top:1px solid #f0f0f0;padding-top:6px;">
        纪律红线: 日≤3笔操作 | 卖出后冷却3天 | 持仓<3天禁卖 | 单行业≤30%
    </div>
</div>"""

def build_morning_brief_html(brief_data: dict) -> str:
    """构建盘前作战计划HTML"""
    holdings = brief_data.get("holdings", {})
    today = datetime.date.today().strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.date.today().weekday()]

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_CSS}</style></head><body>
<div class="container">
<div class="header">
    <h1>盘前作战计划 | {today} {weekday}</h1>
    <div class="meta">中线波段(3天-4周) | 持仓{len(holdings)}只 | 所有条件单为盘中实时价格触发</div>
</div>
"""
    html += _build_today_actions(brief_data)
    html += _build_price_table(brief_data)
    html += _build_market_env(brief_data)
    html += _build_discipline_morning(brief_data)  # P0今日操作预算
    html += _build_quant_morning(brief_data)  # 量化晨报

    html += f"""
<div class="footer">
    中线波段交易系统 | 仅供参考，不构成投资建议
</div>
</div></body></html>"""

    return html


def send_morning_brief(brief_data: dict) -> bool:
    """发送盘前作战计划"""
    from notify.email_notify import send_email

    today = datetime.date.today().strftime("%Y-%m-%d")
    holdings = brief_data.get("holdings", {})

    # 统计今日操作数
    data_dict = brief_data.get("data_dict", {})
    action_count = 0
    for code, pos in holdings.items():
        df = data_dict.get(code)
        if df is None or df.empty:
            continue
        row = df.iloc[-1]
        close = row["close"]
        buy_price = pos.get("buy_price", 0)
        target1 = buy_price * 1.10
        sig_stop = pos.get("buy_price", 0) * 0.90
        wave = _judge_wave_status(row)
        if wave == "破位预警" or close >= target1 or (close - sig_stop) / close * 100 < 3:
            action_count += 1

    subject = f"[盘前作战计划] {today} | {action_count}项操作"

    html = build_morning_brief_html(brief_data)
    return send_email(subject, html)
