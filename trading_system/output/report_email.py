# -*- coding: utf-8 -*-
"""
操盘密码邮件模板构建器 V2.0
============================
生成美观的HTML邮件，支持：
- 卡片式布局 + 颜色编码
- 持仓总览表格
- 条件单详情（止损/止盈/时间单）
- K线图/资金流向图/仓位饼图
- 选股推荐
- 风控状态 + 操作纪律锁
- 拆分多封发送
"""

import datetime
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 基础HTML框架
# ============================================================

def _wrap_html(title: str, subtitle: str, body: str) -> str:
    """包装完整HTML邮件"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:'Microsoft YaHei','PingFang SC',Arial,sans-serif">
<div style="max-width:900px;margin:0 auto;padding:15px">
    <!-- 头部 -->
    <div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);color:white;padding:22px 30px;border-radius:12px 12px 0 0">
        <h1 style="margin:0;font-size:22px;font-weight:700">{title}</h1>
        <div style="font-size:12px;opacity:0.75;margin-top:6px">{today} {now} | {subtitle}</div>
    </div>
    <!-- 内容 -->
    <div style="background:white;padding:25px 30px;border-radius:0 0 12px 12px;box-shadow:0 4px 15px rgba(0,0,0,0.08)">
        {body}
    </div>
    <!-- 底部 -->
    <div style="text-align:center;color:#999;font-size:11px;margin-top:12px;padding:8px">
        操盘密码V3.0 自动生成 | 仅供参考，不构成投资建议 | 股市有风险，投资需谨慎
    </div>
</div>
</body></html>"""


def _section(title: str, content: str, icon: str = "📊") -> str:
    """生成一个区块"""
    return f"""
    <div style="margin:18px 0;border:1px solid #e8e8e8;border-radius:10px;overflow:hidden">
        <div style="background:#fafbfc;padding:12px 18px;border-bottom:1px solid #e8e8e8;font-weight:700;font-size:14px;color:#1a1a2e">
            {icon} {title}
        </div>
        <div style="padding:15px 18px">{content}</div>
    </div>"""


def _metric_cards(metrics: list) -> str:
    """生成指标卡片行 metrics: [(label, value, color), ...]"""
    cards = ""
    for label, value, color in metrics:
        cards += f"""
        <div style="flex:1;min-width:100px;background:#f8f9fa;border-radius:8px;padding:12px 8px;text-align:center;margin:4px">
            <div style="font-size:11px;color:#888">{label}</div>
            <div style="font-size:18px;font-weight:700;color:{color};margin-top:3px">{value}</div>
        </div>"""
    return f'<div style="display:flex;flex-wrap:wrap;gap:6px">{cards}</div>'


def _alert_box(text: str, level: str = "warning") -> str:
    """警告框 level: danger/warning/success/info"""
    colors = {
        "danger": ("#fff1f0", "#ff4d4f", "#cf1322"),
        "warning": ("#fffbe6", "#faad14", "#ad6800"),
        "success": ("#f6ffed", "#52c41a", "#389e0d"),
        "info": ("#e6f7ff", "#1890ff", "#096dd9"),
    }
    bg, border, text_color = colors.get(level, colors["info"])
    return f"""<div style="background:{bg};border:1px solid {border};border-left:4px solid {border};
        border-radius:6px;padding:10px 14px;margin:8px 0;font-size:13px;color:{text_color}">{text}</div>"""


# ============================================================
# 持仓总览表格
# ============================================================

def build_holdings_table(results: list, holdings: dict) -> str:
    """生成持仓总览表格"""
    rows = ""
    total_cost = 0
    total_value = 0

    for r in results:
        code = r.get("code", "")
        name = r.get("name", code)
        info = holdings.get(code, {})
        shares = info.get("shares", 0)
        cost = info.get("cost", 0) or info.get("buy_price", 0)
        close = r.get("close", 0)
        trend_level = r.get("trend_level", 3)

        if shares and close > 0:
            cost_val = shares * cost
            mkt_val = shares * close
            # 成本<=0表示已完全回本，盈亏比例无意义
            if cost > 0:
                pnl_pct = (mkt_val - cost_val) / cost_val * 100
            else:
                pnl_pct = 100.0  # 已回本视为正收益
            total_mkt = sum(holdings.get(c, {}).get("shares", 0) * max(holdings.get(c, {}).get("cost", 0) or holdings.get(c, {}).get("buy_price", 0), 0) for c in holdings) or 1
            pos_pct = mkt_val / total_mkt * 100
            total_cost += max(cost_val, 0)
            total_value += mkt_val
        else:
            pnl_pct = 0
            mkt_val = 0
            pos_pct = 0

        pnl_color = "#e74c3c" if pnl_pct >= 0 else "#27ae60"
        trend_map = {5: "🔴强升", 4: "🟠弱升", 3: "🔵震荡", 2: "🟢弱跌", 1: "⚫强跌"}
        trend_text = trend_map.get(trend_level, "-")

        rows += f"""<tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 6px;text-align:center;font-size:12px">{code}</td>
            <td style="padding:8px 6px;text-align:center;font-size:12px;font-weight:600">{name}</td>
            <td style="padding:8px 6px;text-align:center;font-size:12px">{shares:,}</td>
            <td style="padding:8px 6px;text-align:center;font-size:12px">{cost:.3f}</td>
            <td style="padding:8px 6px;text-align:center;font-size:12px">{close:.3f}</td>
            <td style="padding:8px 6px;text-align:center;font-size:12px">{mkt_val:,.0f}</td>
            <td style="padding:8px 6px;text-align:center;font-size:12px">{pos_pct:.1f}%</td>
            <td style="padding:8px 6px;text-align:center;font-size:12px;font-weight:700;color:{pnl_color}">{pnl_pct:+.1f}%</td>
            <td style="padding:8px 6px;text-align:center;font-size:12px">{trend_text}</td>
        </tr>"""

    total_pnl = (total_value - total_cost) / total_cost * 100 if total_cost > 0 else 0
    total_pnl_color = "#e74c3c" if total_pnl >= 0 else "#27ae60"

    table = f"""
    <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead>
            <tr style="background:#34495e;color:white">
                <th style="padding:10px 6px;text-align:center">代码</th>
                <th style="padding:10px 6px;text-align:center">名称</th>
                <th style="padding:10px 6px;text-align:center">数量</th>
                <th style="padding:10px 6px;text-align:center">成本</th>
                <th style="padding:10px 6px;text-align:center">最新价</th>
                <th style="padding:10px 6px;text-align:center">市值</th>
                <th style="padding:10px 6px;text-align:center">仓位</th>
                <th style="padding:10px 6px;text-align:center">浮盈亏</th>
                <th style="padding:10px 6px;text-align:center">趋势</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
        <tfoot>
            <tr style="background:#f8f9fa;font-weight:700">
                <td colspan="5" style="padding:10px 6px;text-align:right">合计</td>
                <td style="padding:10px 6px;text-align:center">{total_value:,.0f}</td>
                <td style="padding:10px 6px;text-align:center">100%</td>
                <td style="padding:10px 6px;text-align:center;color:{total_pnl_color}">{total_pnl:+.1f}%</td>
                <td style="padding:10px 6px;text-align:center">-</td>
            </tr>
        </tfoot>
    </table>"""
    return table


# ============================================================
# 条件单详情
# ============================================================

def build_conditional_orders(results: list, holdings: dict) -> str:
    """生成条件单操作计划"""
    orders_html = ""
    order_num = 0

    for r in results:
        code = r.get("code", "")
        name = r.get("name", code)
        info = holdings.get(code, {})
        shares = info.get("shares", 0)
        cost = info.get("cost", 0) or info.get("buy_price", 0)
        close = r.get("close", 0)
        trend_level = r.get("trend_level", 3)

        if not shares or close <= 0:
            continue

        # 成本<=0表示已完全回本，使用特殊逻辑
        if cost <= 0:
            pnl_pct = 100.0  # 已回本视为正收益
        else:
            pnl_pct = (close - cost) / cost * 100

        # 止损条件单
        if cost <= 0:
            # 已回本持仓：使用现价回落止损（现价×85%）
            stop_price = round(close * 0.85, 3)
            order_num += 1
            orders_html += _order_card(
                order_num, "止损条件单(回本仓保护)", code, name, "卖出",
                stop_price, "14:50", shares, "20个交易日",
                f"已回本持仓，保护性止损 = 现价 {close:.3f} × 85%",
                "warning"
            )
        elif pnl_pct < 5:
            stop_price = round(cost * 0.90, 3)
            order_num += 1
            orders_html += _order_card(
                order_num, "止损条件单(初始止损)", code, name, "卖出",
                stop_price, "14:50", shares, "20个交易日",
                f"浮盈 {pnl_pct:+.1f}% < 5%，初始止损 = 成本 {cost:.3f} × 90%",
                "danger"
            )
        elif pnl_pct < 15:
            stop_price = round(cost * 1.02, 3)
            order_num += 1
            orders_html += _order_card(
                order_num, "止损条件单(保本止损)", code, name, "卖出",
                stop_price, "14:50", shares, "20个交易日",
                f"浮盈 {pnl_pct:+.1f}%，保本止损 = 成本 {cost:.3f} × 102%",
                "warning"
            )
        else:
            stop_price = round(cost * 1.12, 3)
            order_num += 1
            orders_html += _order_card(
                order_num, "止盈条件单(移动止盈)", code, name, "卖出",
                stop_price, "14:50", shares, "20个交易日",
                f"浮盈 {pnl_pct:+.1f}%，锁定利润 = 成本 {cost:.3f} × 112%",
                "success"
            )

        # 时间条件单（持仓超20天且浮盈<3%）
        buy_date_str = info.get("buy_date", "")
        if buy_date_str:
            try:
                buy_date = datetime.datetime.strptime(buy_date_str, "%Y-%m-%d").date()
                hold_days = (datetime.date.today() - buy_date).days
                if hold_days > 20 and pnl_pct < 3:
                    order_num += 1
                    orders_html += _order_card(
                        order_num, "时间条件单(效率止损)", code, name, "卖出",
                        close, "14:50", shares, "5个交易日",
                        f"持仓{hold_days}天，浮盈{pnl_pct:+.1f}%<3%，资金效率低",
                        "info"
                    )
            except (ValueError, TypeError):
                pass

        # 下跌趋势清仓单
        if trend_level <= 2:
            order_num += 1
            orders_html += _order_card(
                order_num, "清仓条件单(趋势破位)", code, name, "卖出",
                round(close * 0.99, 3), "09:35", shares, "5个交易日",
                f"趋势{trend_level}级(下跌)，主力连续卖出，及时清仓",
                "danger"
            )

    if not orders_html:
        orders_html = '<p style="color:#999;text-align:center;padding:20px">当前无需设置条件单</p>'

    # 操作步骤提示
    guide = """
    <div style="background:#e6f7ff;border:1px solid #91d5ff;border-radius:8px;padding:12px 16px;margin-bottom:15px;font-size:12px;color:#096dd9">
        <b>操作步骤:</b> 打开东方财富APP → 交易 → 智能条件单 → 逐条添加以下条件单 → 确认后盘中不再操作
    </div>"""

    return guide + orders_html


def _order_card(num: int, title: str, code: str, name: str, direction: str,
                trigger_price: float, trigger_time: str, quantity: int,
                validity: str, desc: str, level: str) -> str:
    """生成单个条件单卡片"""
    border_colors = {
        "danger": "#ff4d4f",
        "warning": "#faad14",
        "success": "#52c41a",
        "info": "#1890ff",
    }
    border_color = border_colors.get(level, "#1890ff")
    badge = "🔴必挂" if level == "danger" else ("🟡建议" if level == "warning" else "🟢可选")
    dir_color = "#e74c3c" if direction == "卖出" else "#27ae60"

    return f"""
    <div style="border:1px solid #e8e8e8;border-left:4px solid {border_color};border-radius:8px;padding:14px 16px;margin:10px 0">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <span style="font-weight:700;font-size:13px;color:#333">#{num} {title} | {code} {name} | <span style="color:{dir_color}">{direction}</span></span>
            <span style="font-size:11px;background:{border_color};color:white;padding:2px 8px;border-radius:10px">{badge}</span>
        </div>
        <table style="width:100%;font-size:12px;border-collapse:collapse">
            <tr><td style="padding:4px 0;color:#888;width:80px">触发价</td><td style="font-weight:700;color:#e74c3c;font-size:14px">{trigger_price:.3f} 元</td></tr>
            <tr><td style="padding:4px 0;color:#888">触发时间</td><td>{trigger_time}</td></tr>
            <tr><td style="padding:4px 0;color:#888">数量</td><td>{quantity:,} 股</td></tr>
            <tr><td style="padding:4px 0;color:#888">有效期</td><td>{validity}</td></tr>
            <tr><td style="padding:4px 0;color:#888">说明</td><td style="color:#666">{desc}</td></tr>
        </table>
    </div>"""


# ============================================================
# 风控状态 + 操作纪律锁
# ============================================================

def build_risk_section(results: list, holdings: dict, plan: dict = None) -> str:
    """生成风控状态+操作纪律锁"""
    # 计算仓位
    total_value = 0
    total_capital = 750000
    for r in results:
        code = r.get("code", "")
        info = holdings.get(code, {})
        shares = info.get("shares", 0)
        close = r.get("close", 0)
        if shares and close > 0:
            total_value += shares * close

    pos_pct = total_value / total_capital * 100 if total_capital > 0 else 0
    cash_pct = 100 - pos_pct

    risk_bar = f"""
    <div style="background:#e6f7ff;border-radius:8px;padding:12px 16px;margin-bottom:12px;font-size:13px;color:#096dd9;font-weight:600">
        总仓位: {pos_pct:.1f}% | 现金: {cash_pct:.1f}% | ETF上限20% | 个股上限15% | 赛道上限40%
    </div>"""

    # 操作纪律锁
    discipline = """
    <div style="border:2px solid #ff4d4f;border-radius:10px;padding:15px 18px;margin-top:12px">
        <div style="font-weight:700;font-size:14px;color:#cf1322;margin-bottom:10px">🔒 操作纪律锁（铁律，不可违反）</div>
        <div style="font-size:12px;line-height:2;color:#333">
            ❌ 明日最多交易3笔（含条件单触发），超过即停手<br>
            ❌ 禁止在盘中临时决定买入/卖出<br>
            ❌ 禁止越跌越补（补仓必须有系统信号）<br>
            ❌ 禁止追涨杀跌<br>
            ❌ 禁止满仓操作（现金≥10%）<br>
            ✅ 唯一允许的手动操作：条件单触发后确认成交<br>
            ✅ 如果手痒想操作：先等10分钟，问自己"这是系统信号还是情绪？"
        </div>
    </div>"""

    return risk_bar + discipline


# ============================================================
# 明日操作摘要
# ============================================================

def build_operation_summary(results: list, holdings: dict) -> str:
    """生成明日操作摘要"""
    # 统计条件单数量
    order_count = 0
    for r in results:
        code = r.get("code", "")
        info = holdings.get(code, {})
        shares = info.get("shares", 0)
        cost = info.get("cost", 0) or info.get("buy_price", 0)
        close = r.get("close", 0)
        trend_level = r.get("trend_level", 3)
        if shares and cost and close > 0:
            order_count += 1  # 止损单
            if trend_level <= 2:
                order_count += 1  # 清仓单

    summary = f"""
    <div style="background:linear-gradient(135deg,#fff1f0,#fff7e6);border:1px solid #ffccc7;border-radius:10px;padding:16px 20px">
        <div style="font-weight:700;font-size:14px;color:#cf1322;margin-bottom:10px">⭐ 明日操作摘要</div>
        <div style="font-size:13px;line-height:2;color:#333">
            <b>必须执行:</b> {order_count}条必挂条件单（止损+时间单），开盘前全部设好<br>
            <b>绝对禁止:</b> 盘中手动买卖、越跌越补、追涨杀跌<br>
            <b>最大交易:</b> 3笔/天（含条件单触发），超过即停手<br>
            <b>核心原则:</b> 条件单没触发 = 不操作。没信号就是最大的信号。
        </div>
    </div>"""
    return summary


# ============================================================
# 选股推荐
# ============================================================

def build_stock_picks(scored: list) -> str:
    """生成选股推荐表格"""
    if not scored:
        return '<p style="color:#999;text-align:center">当前无推荐标的</p>'

    rows = ""
    for i, item in enumerate(scored[:5], 1):
        name = item.get("name", "")
        code = item.get("code", "")
        total = item.get("total_score", 0)
        tech = item.get("technical", 0)
        fund = item.get("fund", 0)
        momentum = item.get("momentum", 0)

        if total >= 70:
            rec = "🔴强烈推荐"
            rec_color = "#e74c3c"
        elif total >= 55:
            rec = "🟠关注"
            rec_color = "#f39c12"
        else:
            rec = "🟡观望"
            rec_color = "#999"

        rows += f"""<tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px;text-align:center;font-size:12px">{i}</td>
            <td style="padding:8px;text-align:center;font-size:12px;font-weight:600">{name}</td>
            <td style="padding:8px;text-align:center;font-size:12px;font-weight:700;color:{rec_color}">{total:.1f}</td>
            <td style="padding:8px;text-align:center;font-size:12px">{tech:.0f}</td>
            <td style="padding:8px;text-align:center;font-size:12px">{fund:.0f}</td>
            <td style="padding:8px;text-align:center;font-size:12px">{momentum:.0f}</td>
            <td style="padding:8px;text-align:center;font-size:12px;color:{rec_color}">{rec}</td>
        </tr>"""

    return f"""
    <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#34495e;color:white">
            <th style="padding:10px 6px;font-size:12px">#</th>
            <th style="padding:10px 6px;font-size:12px">标的</th>
            <th style="padding:10px 6px;font-size:12px">综合</th>
            <th style="padding:10px 6px;font-size:12px">技术</th>
            <th style="padding:10px 6px;font-size:12px">资金</th>
            <th style="padding:10px 6px;font-size:12px">动量</th>
            <th style="padding:10px 6px;font-size:12px">推荐</th>
        </tr></thead>
        <tbody>{rows}</tbody>
    </table>"""


# ============================================================
# 完整邮件组装
# ============================================================

def build_morning_email(results: list, holdings: dict, sector_result: dict = None,
                        plan: dict = None, charts: dict = None) -> str:
    """
    盘前作战计划邮件（08:30）
    内容：市场环境 + 板块方向 + 操作清单 + 关键价位 + 仓位
    """
    body = ""

    # 摘要卡片
    total_value = sum(
        holdings.get(r.get("code", ""), {}).get("shares", 0) * r.get("close", 0)
        for r in results if holdings.get(r.get("code", ""), {}).get("shares", 0)
    )
    total_cost = sum(
        holdings.get(r.get("code", ""), {}).get("shares", 0) *
        (holdings.get(r.get("code", ""), {}).get("cost", 0) or holdings.get(r.get("code", ""), {}).get("buy_price", 0))
        for r in results if holdings.get(r.get("code", ""), {}).get("shares", 0)
    )
    pnl_pct = (total_value - total_cost) / total_cost * 100 if total_cost > 0 else 0
    pnl_color = "#e74c3c" if pnl_pct >= 0 else "#27ae60"

    body += _metric_cards([
        ("总市值", f"{total_value/10000:.1f}万", "#333"),
        ("总盈亏", f"{pnl_pct:+.1f}%", pnl_color),
        ("持仓数", f"{len(results)}只", "#333"),
        ("风险等级", plan.get("portfolio_risk", {}).get("risk_level", "-") if plan else "-", "#f39c12"),
    ])

    # 板块方向
    if sector_result:
        ranked = sector_result.get("ranked", [])
        if ranked:
            top = ranked[0]
            bottom = ranked[-1]
            body += _section("板块方向", f"""
                <div style="font-size:13px;line-height:1.8">
                    <span style="color:#e74c3c;font-weight:700">最强: {top['sector']} ({top['return_5d']:+.1f}%)</span> |
                    <span style="color:#27ae60;font-weight:700">最弱: {bottom['sector']} ({bottom['return_5d']:+.1f}%)</span>
                </div>""", "📊")

    # 今日操作
    ops = ""
    for r in results:
        action = r.get("action_suggestion", {})
        urgency = action.get("urgency", "normal")
        if urgency in ("critical", "high"):
            icon = "🚨" if urgency == "critical" else "⚡"
            ops += f'<div style="padding:6px 0;font-size:13px">{icon} <b>{r["name"]}</b>: {action.get("desc","")} | {action.get("detail","")}</div>'
    if ops:
        body += _section("今日操作", ops, "⚡")

    # 关键价位
    prices = ""
    for r in results:
        close = r.get("close", 0)
        support = r.get("support_price", 0)
        resistance = r.get("resistance_price", 0)
        trend = r.get("trend_desc", "")
        prices += f'<div style="padding:4px 0;font-size:12px"><b>{r["name"]:<8}</b> 现价{close:.2f} | 支撑{support:.2f} | 压力{resistance:.2f} | {trend}</div>'
    body += _section("关键价位", prices, "📍")

    # 仓位图
    if charts and charts.get("position_pie"):
        body += _section("仓位分布", f'<img src="{charts["position_pie"]}" style="width:100%;max-width:400px;border-radius:8px">', "🥧")

    return _wrap_html("📋 盘前作战计划", "操盘密码V3.0 | 30秒看完今天怎么操作", body)


def build_evening_email(results: list, holdings: dict, sector_result: dict = None,
                        scored: list = None, plan: dict = None, charts: dict = None) -> str:
    """
    盘后深度复盘邮件（15:30）
    内容：持仓表格 + 趋势 + 资金 + 筹码 + 板块 + 多因子 + 仓位 + 风险 + 图表
    """
    body = ""

    # 持仓总览
    body += _section("持仓总览", build_holdings_table(results, holdings), "💼")

    # 趋势分布
    trend_names = {5: "强上升", 4: "弱上升", 3: "震荡", 2: "弱下跌", 1: "强下跌"}
    trend_html = ""
    for lv in [5, 4, 3, 2, 1]:
        stocks = [r for r in results if r.get("trend_level") == lv]
        if stocks:
            names = "、".join([r["name"] for r in stocks])
            trend_html += f'<div style="padding:3px 0;font-size:13px"><b>{trend_names[lv]}({lv}级):</b> {names}</div>'
    body += _section("趋势分布", trend_html, "📈")

    # 资金动向
    fund_html = ""
    for r in results:
        fd = r.get("fund_data", {})
        streak = r.get("main_flow_streak", 0)
        score = fd.get("score", 50)
        signal = fd.get("signal", "-")
        color = "#e74c3c" if score >= 60 else ("#27ae60" if score <= 40 else "#f39c12")
        fund_html += f'<div style="padding:3px 0;font-size:12px"><b>{r["name"]:<8}</b> 主力连流{streak}天 | 评分<span style="color:{color};font-weight:700">{score}</span> | {signal}</div>'
    body += _section("资金动向", fund_html, "💰")

    # 资金流向图
    if charts and charts.get("fund_flow"):
        body += _section("资金流向图", f'<img src="{charts["fund_flow"]}" style="width:100%;border-radius:8px">', "📊")

    # 筹码分布
    chip_html = ""
    for r in results:
        chip = r.get("chip")
        if not chip:
            continue
        pr = chip.get("profit_ratio", 0)
        conc = chip.get("concentration", 0)
        ctrl = chip.get("control_level", {})
        pattern = chip.get("pattern", {})
        chip_html += f'<div style="padding:3px 0;font-size:12px"><b>{r["name"]:<8}</b> 获利{pr*100:.0f}% | 集中{conc*100:.1f}% | {ctrl.get("level","-")}({ctrl.get("score",0)}分) | {pattern.get("name","-")}</div>'
    body += _section("筹码分布", chip_html, "🎯")

    # 板块轮动
    if sector_result:
        ranked = sector_result.get("ranked", [])
        sector_html = ""
        for m in ranked:
            status_color = {"启动": "#e74c3c", "加速": "#f39c12", "上升": "#e74c3c", "震荡": "#999", "流出": "#27ae60", "下跌": "#27ae60"}.get(m["status"], "#999")
            sector_html += f'<div style="padding:3px 0;font-size:12px"><b>{m["sector"]:<8}</b> 动量{m["momentum_score"]:.0f}分 | 5日{m["return_5d"]:+.1f}% | <span style="color:{status_color};font-weight:600">{m["status"]}</span></div>'
        # 轮动信号
        signals = sector_result.get("signals", [])
        for s in signals[:3]:
            sector_html += f'<div style="padding:3px 0;font-size:12px;color:#f39c12">⚡ {s["desc"]}</div>'
        body += _section("板块轮动", sector_html, "🔄")

        # 板块图
        if charts and charts.get("sector_bar"):
            body += _section("板块动量图", f'<img src="{charts["sector_bar"]}" style="width:100%;border-radius:8px">', "📊")

    # 多因子评分
    if scored:
        body += _section("多因子评分", build_stock_picks(scored), "🏆")

    # 仓位管理
    if plan:
        risk = plan.get("portfolio_risk", {})
        rebalance = plan.get("rebalance", [])
        pos_html = f'<div style="font-size:13px;margin-bottom:10px">总资金: {plan.get("total_capital",0)/10000:.1f}万 | 配置: {plan.get("total_allocated",0)/10000:.1f}万 | 风险: {risk.get("risk_level","-")} | 回撤: {risk.get("max_drawdown_est",0):.1f}%</div>'
        if rebalance:
            pos_html += '<div style="font-size:12px">'
            for rb in rebalance[:8]:
                icon = "🔴" if rb["action"] in ("买入", "加仓") else "🟢"
                pos_html += f'<div style="padding:3px 0">{icon} {rb["name"]} {rb["action"]} {rb["shares"]}股 ({rb["amount"]/10000:.1f}万) | {rb["reason"]}</div>'
            pos_html += '</div>'
        body += _section("仓位管理", pos_html, "💼")

    # 风险提示
    risk_html = ""
    for r in results:
        if r.get("trend_level", 3) <= 2:
            risk_html += f'<div style="padding:3px 0;font-size:12px">🚨 {r["name"]}: 下跌趋势({r.get("trend_level")}级)</div>'
        if r.get("fund_pattern") == "fake":
            risk_html += f'<div style="padding:3px 0;font-size:12px">⚠️ {r["name"]}: 对倒骗线</div>'
    if risk_html:
        body += _section("风险提示", risk_html, "⚠️")

    return _wrap_html("📊 盘后深度复盘", f"操盘密码V3.0 | {len(results)}只标的全量分析", body)


def build_orders_email(results: list, holdings: dict, plan: dict = None) -> str:
    """
    条件单邮件（19:00）
    内容：持仓总览 + 条件单详情 + 风控 + 纪律锁 + 明日摘要
    """
    body = ""

    # 持仓总览
    body += _section("当前持仓总览", build_holdings_table(results, holdings), "💼")

    # 明日操作摘要
    body += _section("明日操作摘要", build_operation_summary(results, holdings), "⭐")

    # 条件单
    body += _section("条件单操作计划", build_conditional_orders(results, holdings), "📋")

    # 风控状态
    body += _section("风控状态", build_risk_section(results, holdings, plan), "🛡️")

    return _wrap_html("📋 条件单操作计划", f"操盘密码V3.0 | 总资金{750000/10000:.0f}万 | 持{len([r for r in results if holdings.get(r.get('code',''),{}).get('shares',0)])}只", body)


def build_weekly_email(results: list, holdings: dict, sector_result: dict = None,
                       plan: dict = None, charts: dict = None) -> str:
    """
    周策略报告邮件（周六 10:00）
    """
    body = ""

    # 本周表现
    perf_html = ""
    items = []
    for r in results:
        code = r.get("code", "")
        info = holdings.get(code, {})
        shares = info.get("shares", 0)
        cost = info.get("cost", 0) or info.get("buy_price", 0)
        close = r.get("close", 0)
        if shares and cost and close > 0:
            pnl = (close - cost) / cost * 100
            items.append((r["name"], pnl))
    items.sort(key=lambda x: x[1], reverse=True)
    for name, pnl in items:
        color = "#e74c3c" if pnl >= 0 else "#27ae60"
        icon = "📈" if pnl >= 0 else "📉"
        perf_html += f'<div style="padding:3px 0;font-size:13px">{icon} {name}: <span style="color:{color};font-weight:700">{pnl:+.1f}%</span></div>'
    body += _section("本周持仓表现", perf_html, "📈")

    # 板块轮动
    if sector_result:
        ranked = sector_result.get("ranked", [])
        sector_html = ""
        for m in ranked:
            sector_html += f'<div style="padding:3px 0;font-size:12px"><b>{m["sector"]:<8}</b> 动量{m["momentum_score"]:.0f}分 | 5日{m["return_5d"]:+.1f}% | {m["status"]}</div>'
        body += _section("板块轮动趋势", sector_html, "🔄")

    # 仓位再平衡
    if plan:
        rebalance = plan.get("rebalance", [])
        rb_html = ""
        if rebalance:
            for rb in rebalance:
                icon = "🔴" if rb["action"] in ("买入", "加仓") else "🟢"
                rb_html += f'<div style="padding:3px 0;font-size:12px">{icon} {rb["name"]} {rb["action"]} {rb["shares"]}股 | {rb["reason"]}</div>'
        else:
            rb_html = '<div style="color:#52c41a;font-size:13px">✅ 当前仓位合理，无需调整</div>'
        risk = plan.get("portfolio_risk", {})
        rb_html += f'<div style="margin-top:8px;font-size:12px;color:#888">风险: {risk.get("risk_level","-")} | 回撤预估{risk.get("max_drawdown_est",0):.1f}%</div>'
        body += _section("仓位再平衡", rb_html, "⚖️")

    # 下周策略
    strong = [r for r in results if r.get("trend_level", 3) >= 4]
    weak = [r for r in results if r.get("trend_level", 3) <= 2]
    strategy_html = ""
    if strong:
        names = "、".join([r["name"] for r in strong])
        strategy_html += f'<div style="padding:4px 0;font-size:13px;color:#e74c3c"><b>持有:</b> {names} (上升趋势，持股待涨)</div>'
    if weak:
        names = "、".join([r["name"] for r in weak])
        strategy_html += f'<div style="padding:4px 0;font-size:13px;color:#27ae60"><b>回避:</b> {names} (下跌趋势，不抄底)</div>'
    d_signals = [r for r in results if r.get("dk_signal") == "D" and not r.get("dk_filtered")]
    if d_signals:
        names = "、".join([r["name"] for r in d_signals])
        strategy_html += f'<div style="padding:4px 0;font-size:13px;color:#f39c12"><b>关注:</b> {names} (D点信号，等待回踩确认)</div>'
    body += _section("下周策略", strategy_html, "🎯")

    # 仓位饼图
    if charts and charts.get("position_pie"):
        body += _section("仓位分布", f'<img src="{charts["position_pie"]}" style="width:100%;max-width:400px;border-radius:8px">', "🥧")

    return _wrap_html("📅 周策略报告", f"操盘密码V3.0 | 第{datetime.date.today().isocalendar()[1]}周", body)
