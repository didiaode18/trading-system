"""
盘后综合日报 V2.0 - 中线波段版
================================
面向持仓3天-4周的中线波段交易者
30秒内回答3个问题: 盈亏如何？波段走到哪？明天做什么？

3+1板块:
1. 持仓波段仪表盘 - 盈亏/止损/目标/波段状态/操作
2. 操作清单 - 明日具体动作(价格+动作+数量)
3. 趋势健康度 - 每只票的波段诊断
4. 辅助简报 - 选股/风控(简短)

使用方式:
    from output.daily_digest import send_daily_digest
    send_daily_digest(digest_data)

digest_data 结构:
    {
        "holdings": {code: {shares, buy_price, highest_price, buy_date, ...}},
        "data_dict": {code: DataFrame(含ma5/ma10/ma20/ma60/atr等)},
        "signals": [(code, sig_dict), ...],
        "market": {"market_state", "suggested_position"},
        "screener": {...},
        "risk_report": {...},
    }
"""

import os
import sys
import datetime
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ============================================================
# 一、波段状态判断
# ============================================================

def _calc_holding_days(buy_date_str: str) -> int:
    """计算持仓交易日数（简化为自然日/1.4估算）"""
    try:
        buy_date = datetime.datetime.strptime(buy_date_str, "%Y-%m-%d").date()
        delta = (datetime.date.today() - buy_date).days
        return max(1, int(delta / 1.4))  # 粗略估算交易日
    except Exception:
        return 0


def _judge_wave_status(row, df) -> dict:
    """
    判断波段状态:
    - 上升波段: MA5>MA10>MA20 且价格在MA20上方
    - 横盘整理: 均线缠绕，价格在MA20附近(+-3%)
    - 回调中: 价格跌破MA5但MA20仍向上
    - 破位预警: 价格跌破MA20且MA20走平/向下
    """
    close = row.get("close", 0)
    ma5 = row.get("ma5", close)
    ma10 = row.get("ma10", close)
    ma20 = row.get("ma20", close)
    ma20_slope = row.get("ma20_slope", 0)

    if ma20 == 0:
        return {"status": "数据不足", "color": "#999", "desc": "数据不足"}

    pct_to_ma20 = (close - ma20) / ma20 * 100

    if ma5 > ma10 > ma20 and close > ma20:
        return {"status": "上升波段", "color": "#2e7d32", "desc": "多头排列，趋势向上"}
    elif close < ma20 and ma20_slope <= 0:
        return {"status": "破位预警", "color": "#d32f2f", "desc": "跌破MA20且均线走平/向下"}
    elif close < ma5 and ma20_slope > 0:
        return {"status": "回调中", "color": "#e65100", "desc": "短期回调，MA20仍向上"}
    elif abs(pct_to_ma20) < 3:
        return {"status": "横盘整理", "color": "#f57c00", "desc": "均线缠绕，方向待选择"}
    elif close > ma20:
        return {"status": "上升波段", "color": "#2e7d32", "desc": "价格在MA20上方"}
    else:
        return {"status": "弱势", "color": "#d32f2f", "desc": "价格在MA20下方"}


def _get_support_resistance(df) -> tuple:
    """获取近期支撑位和压力位"""
    if df is None or len(df) < 10:
        return 0, 0
    recent = df.tail(20)
    support = recent["low"].min()
    resistance = recent["high"].max()
    return round(support, 3), round(resistance, 3)


def _generate_action(code, pos, row, wave_status, stop_price, target1) -> str:
    """生成具体操作建议"""
    close = row.get("close", 0)
    buy_price = pos.get("buy_price", 0)
    shares = pos.get("shares", 0)
    holding_days = _calc_holding_days(pos.get("buy_date", ""))
    pnl_pct = (close - buy_price) / buy_price * 100 if buy_price > 0 else 0

    # 破位 -> 清仓
    if wave_status["status"] == "破位预警":
        return f"跌破{stop_price:.2f}清仓"

    # 到达目标 -> 减仓
    if close >= target1 and target1 > 0:
        sell_shares = int(shares / 3 / 100) * 100  # 减1/3，取整到100股
        return f"已到位，明日减仓{sell_shares}股"

    # 时间止损预警
    if holding_days > 30 and pnl_pct < 3:
        return f"持仓{holding_days}天浮盈仅{pnl_pct:.1f}%，考虑换股"

    # 正常持有
    if wave_status["status"] == "上升波段":
        return f"持有，目标{target1:.2f}减1/3"

    # 回调中
    if wave_status["status"] == "回调中":
        ma20 = row.get("ma20", 0)
        return f"回调观察，MA20({ma20:.2f})企稳则持有"

    return "持有观望"


# ============================================================
# 二、HTML构建
# ============================================================

_CSS = """
body { font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 10px; background: #f0f2f5; }
.container { max-width: 900px; margin: 0 auto; background: #fff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.08); overflow: hidden; }
.header { background: linear-gradient(135deg, #1a237e, #283593); color: #fff; padding: 20px 24px; }
.header h1 { margin: 0; font-size: 20px; font-weight: 600; }
.header .meta { font-size: 12px; opacity: 0.8; margin-top: 6px; }
.section { padding: 16px 24px; border-bottom: 1px solid #f0f0f0; }
.section:last-child { border-bottom: none; }
.section-title { font-size: 15px; font-weight: 600; color: #1a237e; margin: 0 0 12px 0; padding-left: 10px; border-left: 3px solid #3f51b5; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { background: #f5f6fa; padding: 8px 6px; text-align: center; font-weight: 600; color: #333; border-bottom: 2px solid #e0e0e0; }
td { padding: 7px 6px; text-align: center; border-bottom: 1px solid #f0f0f0; }
tr:hover { background: #fafbff; }
.text-red { color: #d32f2f !important; }
.text-green { color: #2e7d32 !important; }
.text-orange { color: #e65100 !important; }
.action-item { padding: 10px 14px; margin: 6px 0; border-radius: 8px; font-size: 13px; line-height: 1.6; }
.action-urgent { background: #fff1f0; border-left: 4px solid #d32f2f; }
.action-exec { background: #fff7e6; border-left: 4px solid #fa8c16; }
.action-watch { background: #f6ffed; border-left: 4px solid #52c41a; }
.action-time { background: #f0f5ff; border-left: 4px solid #1890ff; }
.wave-card { background: #f8f9ff; border-radius: 8px; padding: 12px 16px; margin: 8px 0; }
.wave-card .title { font-weight: 600; font-size: 13px; margin-bottom: 6px; }
.wave-card .detail { font-size: 12px; color: #555; line-height: 1.8; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; color: #fff; }
.footer { padding: 12px 24px; text-align: center; font-size: 11px; color: #999; background: #fafafa; }
"""


def _build_position_table(holdings, data_dict, signals_map):
    """板块1: 持仓波段仪表盘"""
    rows = []
    total_cost = 0
    total_value = 0

    for code, pos in holdings.items():
        df = data_dict.get(code)
        if df is None or df.empty:
            continue

        row = df.iloc[-1]
        close = row["close"]
        buy_price = pos.get("buy_price", 0)
        shares = pos.get("shares", 0)
        buy_date = pos.get("buy_date", "")
        name = config.get_stock_name(code)

        holding_days = _calc_holding_days(buy_date)
        pnl_pct = (close - buy_price) / buy_price * 100 if buy_price > 0 else 0
        cost_val = buy_price * shares
        cur_val = close * shares
        total_cost += cost_val
        total_value += cur_val

        # 波段状态
        wave = _judge_wave_status(row, df)

        # 止损价（从信号或默认-10%）
        sig = signals_map.get(code, {})
        stop_price = sig.get("stop_loss_current", 0) or sig.get("stop_loss_initial", 0)
        if not stop_price:
            stop_price = buy_price * 0.90

        # 目标价（阶梯止盈第1档: +10%）
        target1 = buy_price * 1.10

        # 操作建议
        action = _generate_action(code, pos, row, wave, stop_price, target1)

        pnl_class = "text-green" if pnl_pct >= 0 else "text-red"

        rows.append(f"""<tr>
            <td>{code}</td>
            <td>{name}</td>
            <td>{holding_days}天</td>
            <td>{buy_price:.3f}</td>
            <td>{close:.3f}</td>
            <td class="{pnl_class}">{pnl_pct:+.1f}%</td>
            <td class="text-red">{stop_price:.2f}</td>
            <td class="text-green">{target1:.2f}</td>
            <td><span class="badge" style="background:{wave['color']}">{wave['status']}</span></td>
            <td style="text-align:left;font-size:11px">{action}</td>
        </tr>""")

    total_pnl = (total_value - total_cost) / total_cost * 100 if total_cost > 0 else 0
    total_pnl_class = "text-green" if total_pnl >= 0 else "text-red"

    html = f"""
<div class="section">
    <h2 class="section-title">持仓波段仪表盘</h2>
    <div style="margin-bottom:10px;font-size:13px;">
        总成本: <b>{total_cost:,.0f}</b>元 | 总市值: <b>{total_value:,.0f}</b>元 |
        总盈亏: <b class="{total_pnl_class}">{total_pnl:+.1f}%</b> ({total_value-total_cost:+,.0f}元)
    </div>
    <table>
        <tr><th>代码</th><th>名称</th><th>持仓</th><th>成本</th><th>现价</th><th>盈亏</th><th>止损</th><th>目标</th><th>波段</th><th>操作</th></tr>
        {''.join(rows)}
    </table>
</div>"""
    return html


def _build_action_list(holdings, data_dict, signals_map):
    """板块2: 操作清单（明日具体动作）"""
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

        sig = signals_map.get(code, {})
        stop_price = sig.get("stop_loss_current", 0) or sig.get("stop_loss_initial", 0)
        if not stop_price:
            stop_price = buy_price * 0.90
        target1 = buy_price * 1.10

        wave = _judge_wave_status(row, df)
        ma20 = row.get("ma20", 0)

        # 紧急: 破位或距止损<3%
        dist_to_stop = (close - stop_price) / close * 100 if close > 0 else 0
        if wave["status"] == "破位预警" or dist_to_stop < 3:
            actions.append((1, f"""<div class="action-item action-urgent">
                <b>[紧急]</b> {code} {name}: 盘中价<={stop_price:.2f} 全部止损卖出{shares}股
                <span style="color:#999">（距止损仅{dist_to_stop:.1f}%）</span>
            </div>"""))

        # 执行: 到达目标价
        elif close >= target1:
            sell_shares = int(shares / 3 / 100) * 100
            actions.append((2, f"""<div class="action-item action-exec">
                <b>[明日执行]</b> {code} {name}: 盘中价>={target1:.2f} 减仓{sell_shares}股（阶梯止盈第1档）
            </div>"""))

        # 时间预警
        elif holding_days > 30 and pnl_pct < 3:
            actions.append((3, f"""<div class="action-item action-time">
                <b>[时间预警]</b> {code} {name}: 已持仓{holding_days}天，浮盈仅{pnl_pct:.1f}%，接近时间止损线
            </div>"""))

        # 观察: 回调中
        elif wave["status"] == "回调中":
            actions.append((4, f"""<div class="action-item action-watch">
                <b>[持续观察]</b> {code} {name}: 回调至MA20({ma20:.2f})附近，企稳则继续持有
            </div>"""))

    # 卖出信号
    for code, sig in signals_map.items():
        if sig.get("sell_signal") and code in holdings:
            name = config.get_stock_name(code)
            reason = sig.get("signal_reason", "卖出信号")
            if not any(code in a[1] for a in actions if a[0] == 1):
                actions.append((1, f"""<div class="action-item action-urgent">
                    <b>[卖出信号]</b> {code} {name}: {reason}
                </div>"""))

    if not actions:
        actions.append((5, """<div class="action-item action-watch">
            <b>[无操作]</b> 所有持仓趋势正常，继续持有
        </div>"""))

    actions.sort(key=lambda x: x[0])

    html = f"""
<div class="section">
    <h2 class="section-title">明日操作清单</h2>
    {''.join(a[1] for a in actions)}
</div>"""
    return html


def _build_wave_health(holdings, data_dict):
    """板块3: 趋势健康度（每只票的波段诊断）"""
    cards = []

    for code, pos in holdings.items():
        df = data_dict.get(code)
        if df is None or df.empty:
            continue

        row = df.iloc[-1]
        close = row["close"]
        name = config.get_stock_name(code)
        wave = _judge_wave_status(row, df)

        ma5 = row.get("ma5", 0)
        ma10 = row.get("ma10", 0)
        ma20 = row.get("ma20", 0)
        ma60 = row.get("ma60", 0)

        # 均线排列描述
        if ma5 > ma10 > ma20:
            ma_desc = "5>10>20 多头排列"
        elif ma5 < ma10 < ma20:
            ma_desc = "5<10<20 空头排列"
        else:
            ma_desc = "均线缠绕"

        # 量价关系
        vol = row.get("volume", 0)
        vol_ma = row.get("vol_ma20", 1)
        if vol_ma > 0 and vol < vol_ma * 0.7:
            vol_desc = "缩量（正常回踩）"
        elif vol_ma > 0 and vol > vol_ma * 1.5:
            vol_desc = "放量（注意方向）"
        else:
            vol_desc = "量能平稳"

        # 支撑压力
        support, resistance = _get_support_resistance(df)

        # 结论
        if wave["status"] == "上升波段":
            conclusion = "趋势完好，持有等目标"
        elif wave["status"] == "回调中":
            conclusion = f"短期回调，关注MA20({ma20:.2f})支撑"
        elif wave["status"] == "破位预警":
            conclusion = "趋势破坏，严格执行止损"
        elif wave["status"] == "横盘整理":
            conclusion = "方向待选择，耐心等待突破"
        else:
            conclusion = "观望"

        cards.append(f"""
    <div class="wave-card">
        <div class="title">{code} {name} <span class="badge" style="background:{wave['color']}">{wave['status']}</span></div>
        <div class="detail">
            均线: {ma_desc} | 量价: {vol_desc}<br>
            支撑: {support:.2f} | 压力: {resistance:.2f} | MA20: {ma20:.2f} | MA60: {ma60:.2f}<br>
            <b>结论: {conclusion}</b>
        </div>
    </div>""")

    html = f"""
<div class="section">
    <h2 class="section-title">趋势健康度</h2>
    {''.join(cards)}
</div>"""
    return html


def _build_brief_panel(digest_data):
    """板块4: 辅助简报（选股+风控，简短）"""
    items = []

    # 选股简报
    screener = digest_data.get("screener", {})
    if screener:
        qualified = screener.get("qualified_count", 0)
        watch_count = len(screener.get("watch_list", []))
        if qualified > 0:
            # 列出前2只
            qualified_list = screener.get("qualified_stocks", [])[:2]
            stock_names = ", ".join(
                f"{s.get('code', '')}({config.get_stock_name(s.get('code', ''))})"
                for s in qualified_list
            ) if qualified_list else f"{qualified}只"
            items.append(f"新选股: {qualified}只入选 ({stock_names})")
        if watch_count > 0:
            items.append(f"观察池: {watch_count}只待跟踪")

    # 风控简报
    risk_report = digest_data.get("risk_report", {})
    if risk_report:
        risk_score = risk_report.get("risk_score", 0)
        alerts = risk_report.get("alerts", [])
        if alerts:
            items.append(f"风险评分: {risk_score:.0f}分 | 预警: {alerts[0]}")
        else:
            items.append(f"风险评分: {risk_score:.0f}分 | 无重大风险")

    # 大盘环境（一句话）
    market = digest_data.get("market", {})
    market_state = market.get("market_state", "normal")
    max_pos = market.get("suggested_position", 0.7)
    state_map = {
        "strong": f"强势市场，仓位可至{max_pos:.0%}",
        "normal": f"震荡市，仓位控制{max_pos:.0%}以内",
        "weak": f"弱势市场，仓位控制{max_pos:.0%}以内，防守为主",
    }
    items.append(f"大盘: {state_map.get(market_state, '未知')}")

    if not items:
        return ""

    html = f"""
<div class="section">
    <h2 class="section-title">辅助简报</h2>
    <div style="font-size:13px;line-height:2;color:#555;">
        {'<br>'.join('• ' + item for item in items)}
    </div>
</div>"""
    return html


# ============================================================
# 板块5: 量化智能面板 (P0-P4能力注入)
# ============================================================

def _build_quant_panel(digest_data: dict) -> str:
    """
    量化智能面板 - 打败机构的核心武器
    回答5个问题: 优势还在吗？环境如何？主力在干啥？信号可靠吗？风险可控吗？
    """
    quant_panel = digest_data.get("quant_panel", {})
    if not quant_panel:
        # 尝试实时计算
        try:
            from quant.report_enhancer import QuantReportEnhancer
            enhancer = QuantReportEnhancer()
            quant_panel = enhancer.compute_panel(
                digest_data.get("holdings", {}),
                digest_data.get("data_dict", {}),
                signals=digest_data.get("signals", []),
            )
        except Exception as e:
            logger.warning(f"量化面板计算失败: {e}")
            return ""

    if not quant_panel:
        return ""

    # 综合评分
    overall_score = quant_panel.get("overall_score", 0)
    score_color = "#2e7d32" if overall_score >= 70 else "#f57c00" if overall_score >= 50 else "#d32f2f"
    score_label = "强势" if overall_score >= 70 else "中性" if overall_score >= 50 else "谨慎"

    # 因子有效性
    fh = quant_panel.get("factor_health", {})
    fh_status = fh.get("status", "unknown")
    fh_color = {"excellent": "#2e7d32", "good": "#4caf50", "warning": "#ff9800", "danger": "#d32f2f"}.get(fh_status, "#999")
    mom_ic = fh.get("momentum_ic", 0)
    trend_ic = fh.get("trend_ic", 0)
    fh_verdict = fh.get("verdict", "")

    # 市场环境
    mr = quant_panel.get("market_regime", {})
    regime = mr.get("regime", "未知")
    regime_color = mr.get("regime_color", "#999")
    sentiment = mr.get("sentiment", 50)
    breadth = mr.get("breadth", 0)
    mr_advice = mr.get("advice", "")

    # 筹码分析
    ca = quant_panel.get("chip_analysis", {})
    avg_profit = ca.get("avg_profit_ratio", 0)
    avg_vs_cost = ca.get("avg_vs_main_cost", 0)
    ca_summary = ca.get("summary", "")

    # 组合风险
    pr = quant_panel.get("portfolio_risk", {})
    corr = pr.get("correlation", 0)
    vol = pr.get("volatility", 0)
    max_dd = pr.get("max_dd_20d", 0)
    risk_level = pr.get("risk_level", "中")
    risk_advice = pr.get("risk_advice", "")

    # 风控状态
    rs = quant_panel.get("risk_status", {})
    alert_count = rs.get("alert_count", 0)
    rs_summary = rs.get("summary", "")
    risk_details = rs.get("details", [])

    # 构建风控明细行
    risk_rows = ""
    for rd in risk_details[:8]:
        code = rd.get("code", "")
        name = config.get_stock_name(code)
        status = rd.get("status", "正常")
        status_color = rd.get("status_color", "#2e7d32")
        atr_stop = rd.get("atr_stop", 0)
        trailing_dd = rd.get("trailing_dd", 0) * 100
        pnl = rd.get("pnl_pct", 0) * 100
        hold_days = rd.get("hold_days", 0)

        risk_rows += f"""<tr>
            <td>{code}<br><small>{name}</small></td>
            <td style="color:{status_color};font-weight:600">{status}</td>
            <td>{atr_stop:.2f}</td>
            <td>{trailing_dd:.1f}%</td>
            <td class="{'text-green' if pnl > 0 else 'text-red'}">{pnl:+.1f}%</td>
            <td>{hold_days}天</td>
        </tr>"""

    html = f"""
<div class="section" style="background:linear-gradient(135deg,#f8f9ff,#f0f4ff);">
    <h2 class="section-title" style="border-left-color:#ff6f00;">🧠 量化智能面板</h2>

    <!-- 综合评分 -->
    <div style="text-align:center;margin:10px 0 16px;">
        <span style="font-size:36px;font-weight:700;color:{score_color};">{overall_score:.0f}</span>
        <span style="font-size:14px;color:{score_color};margin-left:8px;">{score_label}</span>
        <div style="font-size:11px;color:#888;margin-top:4px;">综合量化评分 (因子25% + 环境25% + 筹码20% + 风险15% + 风控15%)</div>
    </div>

    <!-- 5大指标卡片 -->
    <table style="margin-bottom:12px;">
        <tr>
            <th style="width:20%">指标</th>
            <th style="width:20%">状态</th>
            <th>详情</th>
        </tr>
        <tr>
            <td><b>因子有效性</b></td>
            <td><span class="badge" style="background:{fh_color}">{fh_verdict[:6] if fh_verdict else 'N/A'}</span></td>
            <td>动量IC={mom_ic:.3f} | 趋势IC={trend_ic:.3f}</td>
        </tr>
        <tr>
            <td><b>市场环境</b></td>
            <td><span class="badge" style="background:{regime_color}">{regime}</span></td>
            <td>情绪{sentiment}分 | 上涨占比{breadth:.0%} | {mr_advice}</td>
        </tr>
        <tr>
            <td><b>筹码分布</b></td>
            <td>获利盘{avg_profit:.0%}</td>
            <td>vs主力成本{avg_vs_cost:+.1f}% | {ca_summary}</td>
        </tr>
        <tr>
            <td><b>组合风险</b></td>
            <td><span class="badge" style="background:{'#d32f2f' if risk_level == '高' else '#f57c00' if risk_level == '中' else '#2e7d32'}">{risk_level}</span></td>
            <td>相关性{corr:.2f} | 年化波动{vol:.1%} | 20日回撤{max_dd:.1%} | {risk_advice}</td>
        </tr>
        <tr>
            <td><b>风控状态</b></td>
            <td style="color:{'#d32f2f' if alert_count > 0 else '#2e7d32'};font-weight:600">{alert_count}只预警</td>
            <td>{rs_summary}</td>
        </tr>
    </table>

    <!-- 风控明细 -->
    {'<h3 style="font-size:13px;color:#333;margin:12px 0 6px;">持仓风控明细</h3><table><tr><th>股票</th><th>状态</th><th>ATR止损</th><th>距高点</th><th>盈亏</th><th>持仓</th></tr>' + risk_rows + '</table>' if risk_rows else ''}

    <div style="font-size:11px;color:#999;margin-top:10px;text-align:center;">
        量化模型输出 | 因子IC>0.05为有效 | 相关性<0.4为分散良好 | 仅供参考
    </div>
</div>"""
    return html


# ============================================================
# 三、主构建函数
# ============================================================

def _build_discipline_panel(digest_data: dict) -> str:
    """板块: 交易行为监控（P0 行为纠偏）"""
    try:
        from quant.risk_manager import RiskManager
        rm = RiskManager()
    except Exception:
        return ""

    holdings = digest_data.get("holdings", {})
    today_trades = digest_data.get("today_trades", 0)
    trade_history = digest_data.get("trade_history", [])
    sector_map = digest_data.get("sector_map", {})
    today = datetime.date.today().strftime("%Y-%m-%d")

    report = rm.get_discipline_report(
        today_trades=today_trades,
        positions=holdings,
        trade_history=trade_history,
        current_date=today,
        sector_map=sector_map,
    )

    # --- 交易频率 ---
    freq = report.get("frequency", {})
    freq_level = freq.get("level", "normal")
    freq_color = {"normal": "#2e7d32", "warning": "#f57c00", "danger": "#d32f2f"}.get(freq_level, "#999")
    freq_icon = {"normal": "✅", "warning": "⚠️", "danger": "⛔"}.get(freq_level, "")
    freq_text = f"{freq.get('today_trades', 0)}/{freq.get('max_daily', 5)}笔"

    # --- 行业集中度 ---
    sector = report.get("sector", {})
    sector_pass = sector.get("pass", True)
    violations = sector.get("violations", [])
    max_sector_name = sector.get("max_sector", "")
    max_sector_pct = sector.get("max_pct", 0)
    sector_color = "#2e7d32" if sector_pass else "#d32f2f"
    sector_icon = "✅" if sector_pass else "⚠️"

    # --- 冷却期 ---
    cooldown_list = report.get("cooldown_list", [])
    cooldown_html = ""
    for cd in cooldown_list[:5]:
        code = cd.get("code", "")
        name = config.get_stock_name(code)
        days_left = cd.get("remaining_days", 0)
        cooldown_html += f'<span style="display:inline-block;background:#fff1f0;border:1px solid #ffa39e;border-radius:4px;padding:2px 8px;margin:2px 4px;font-size:11px;">{code} {name} (剩{days_left}天)</span>'

    # --- 最小持仓 ---
    min_hold = report.get("min_hold_alerts", [])
    min_hold_html = ""
    for mh in min_hold[:5]:
        code = mh.get("code", "")
        name = config.get_stock_name(code)
        h_days = mh.get("holding_days", 0)
        m_days = mh.get("min_days", 3)
        min_hold_html += f'<span style="display:inline-block;background:#fffbe6;border:1px solid #ffe58f;border-radius:4px;padding:2px 8px;margin:2px 4px;font-size:11px;">{code} {name} (持{h_days}天<{m_days}天)</span>'

    # --- 组装HTML ---
    html = f"""
<div class="section" style="background:#fffdf5;">
    <h2 class="section-title" style="border-left-color:#fa541c;">🚨 交易纪律监控</h2>
    <table style="margin-bottom:10px;">
        <tr>
            <th style="width:25%">检查项</th>
            <th style="width:20%">状态</th>
            <th>详情</th>
        </tr>
        <tr>
            <td><b>今日交易笔数</b></td>
            <td style="color:{freq_color};font-weight:700">{freq_icon} {freq_text}</td>
            <td>日预算≤{freq.get('max_daily', 5)}笔 | 剩余{freq.get('remaining', 0)}笔</td>
        </tr>
        <tr>
            <td><b>行业集中度</b></td>
            <td style="color:{sector_color};font-weight:700">{sector_icon} {max_sector_name} {max_sector_pct:.0%}</td>
            <td>{'超限: ' + ', '.join(violations) if violations else '单行业≤30%，分散良好'}</td>
        </tr>
        <tr>
            <td><b>冷却期标的</b></td>
            <td>{'⚠️ ' + str(len(cooldown_list)) + '只' if cooldown_list else '✅ 无'}</td>
            <td>{cooldown_html if cooldown_html else '卖出后3天内禁止再买'}</td>
        </tr>
        <tr>
            <td><b>最小持仓天数</b></td>
            <td>{'⚠️ ' + str(len(min_hold)) + '只未达标' if min_hold else '✅ 全部达标'}</td>
            <td>{min_hold_html if min_hold_html else '持仓≥ 3天才允许主动卖出(止损除外)'}</td>
        </tr>
    </table>
    <div style="font-size:11px;color:#999;text-align:center;">
        纪律规则: 日≤5笔 | 同标的卖出后冷却3天 | 最小持仓3天 | 单行业≤30%
    </div>
</div>"""
    return html

def build_daily_digest_html(digest_data: dict) -> str:
    """构建中线波段版盘后综合日报HTML"""
    holdings = digest_data.get("holdings", {})
    data_dict = digest_data.get("data_dict", {})
    signals = digest_data.get("signals", [])

    # 构建信号映射
    signals_map = {}
    for item in signals:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            signals_map[item[0]] = item[1]
        elif isinstance(item, dict):
            signals_map[item.get("code", "")] = item

    today = datetime.date.today().strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.date.today().weekday()]
    now = datetime.datetime.now().strftime("%H:%M")

    # 统计
    buy_count = sum(1 for _, s in signals if isinstance(s, dict) and s.get("buy_signal")) if signals else 0
    sell_count = sum(1 for _, s in signals if isinstance(s, dict) and s.get("sell_signal")) if signals else 0

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_CSS}</style></head><body>
<div class="container">
<div class="header">
    <h1>盘后日报 | {today} {weekday}</h1>
    <div class="meta">中线波段(3天-4周) | 持仓{len(holdings)}只 | 买入信号{buy_count} 卖出信号{sell_count} | {now}生成</div>
</div>
"""

    # 3+1+1+1 板块 (原3+1 + 量化智能面板 + 交易纪律监控)
    html += _build_position_table(holdings, data_dict, signals_map)
    html += _build_action_list(holdings, data_dict, signals_map)
    html += _build_wave_health(holdings, data_dict)
    html += _build_discipline_panel(digest_data)  # P0交易纪律监控
    html += _build_quant_panel(digest_data)  # P0-P4量化智能面板
    html += _build_brief_panel(digest_data)

    # 页脚
    html += f"""
<div class="footer">
    中线波段交易系统 | 所有条件单为盘中实时价格触发 | 仅供参考，不构成投资建议<br>
    总资金: {config.TOTAL_CAPITAL:,.0f}元
</div>
</div></body></html>"""

    return html


# ============================================================
# 四、发送函数
# ============================================================

def send_daily_digest(digest_data: dict) -> bool:
    """
    构建并发送盘后综合日报（中线波段版）

    参数:
        digest_data: 含 holdings, data_dict, signals, market, screener, risk_report
    返回:
        是否发送成功
    """
    from notify.email_notify import send_email

    today = datetime.date.today().strftime("%Y-%m-%d")
    holdings = digest_data.get("holdings", {})

    # 计算总盈亏用于标题
    total_cost = 0
    total_value = 0
    for code, pos in holdings.items():
        df = digest_data.get("data_dict", {}).get(code)
        if df is not None and not df.empty:
            close = df.iloc[-1]["close"]
            total_cost += pos.get("buy_price", 0) * pos.get("shares", 0)
            total_value += close * pos.get("shares", 0)

    pnl_pct = (total_value - total_cost) / total_cost * 100 if total_cost > 0 else 0

    subject = f"[盘后日报] {today} | 持仓{len(holdings)}只 | 盈亏{pnl_pct:+.1f}%"

    html = build_daily_digest_html(digest_data)
    return send_email(subject, html)
