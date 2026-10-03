# -*- coding: utf-8 -*-
"""
盘前条件单邮件生成器
====================
19:00 定时任务：生成次日条件单 → 邮件发送 + QMT JSON 落盘

接口:
  send_eastmoney_orders_email(signals, holdings, data_dict, news_risk=None) -> bool
  _save_qmt_orders_json(signals, holdings, data_dict) -> None
"""

import os
import json
import logging
import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def _build_order_html(signals, holdings, data_dict, news_risk=None):
    """构建条件单邮件 HTML"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M")

    # 分类信号
    buy_signals = []
    sell_signals = []
    add_signals = []
    for code, sig in signals:
        if sig.get("buy_signal"):
            buy_signals.append((code, sig))
        elif sig.get("sell_signal"):
            sell_signals.append((code, sig))
        if sig.get("add_position"):
            add_signals.append((code, sig))

    # 持仓盈亏概览
    holding_rows = []
    total_mv = 0
    total_cost = 0
    for code, pos in holdings.items():
        name = pos.get("name", config.get_stock_name(code))
        shares = pos.get("shares", 0)
        buy_price = pos.get("buy_price", 0)
        current_price = pos.get("current_price", buy_price)
        cost = shares * buy_price
        mv = shares * current_price
        pnl = mv - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0
        total_mv += mv
        total_cost += cost
        color = "text-red" if pnl < 0 else "text-green"
        holding_rows.append(
            f'<tr><td>{code}</td><td>{name}</td><td>{shares}</td>'
            f'<td>{buy_price:.2f}</td><td>{current_price:.2f}</td>'
            f'<td class="{color}">{pnl:+.0f} ({pnl_pct:+.1f}%)</td></tr>'
        )

    total_pnl = total_mv - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    pnl_color = "text-red" if total_pnl < 0 else "text-green"

    # 买入条件单表格
    buy_rows = []
    for code, sig in buy_signals:
        name = config.get_stock_name(code)
        price = sig.get("buy_price", 0)
        stop = sig.get("stop_loss_initial", price * 0.9)
        shares = sig.get("position", {}).get("shares", 0)
        reason = sig.get("signal_reason", "")
        score = sig.get("quality_score", "")
        score_str = f"{score:.0f}" if isinstance(score, (int, float)) else str(score)
        # 新闻风险标注
        risk_tag = ""
        if news_risk and code in news_risk:
            nr = news_risk[code]
            if nr.get("level", 0) >= 2:
                risk_tag = ' <span class="text-red">[新闻风险]</span>'
        buy_rows.append(
            f'<tr class="buy"><td>{code}</td><td>{name}</td>'
            f'<td>{price:.2f}</td><td>{shares}</td>'
            f'<td>{stop:.2f}</td><td>{score_str}</td>'
            f'<td>{reason}{risk_tag}</td></tr>'
        )

    # 卖出条件单表格
    sell_rows = []
    for code, sig in sell_signals:
        name = config.get_stock_name(code)
        price = sig.get("sell_price", 0)
        reason = sig.get("signal_reason", "")
        sell_rows.append(
            f'<tr class="sell"><td>{code}</td><td>{name}</td>'
            f'<td>{price:.2f}</td><td>{reason}</td></tr>'
        )

    # 加仓信号表格
    add_rows = []
    for code, sig in add_signals:
        name = config.get_stock_name(code)
        price = sig.get("buy_price", sig.get("current_price", 0))
        reason = sig.get("signal_reason", "")
        add_rows.append(
            f'<tr><td>{code}</td><td>{name}</td>'
            f'<td>{price:.2f}</td><td>{reason}</td></tr>'
        )

    # 组装 HTML
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
    .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    h1 {{ color: #1a1a2e; border-bottom: 3px solid #4472C4; padding-bottom: 10px; font-size: 22px; }}
    h2 {{ color: #333; font-size: 16px; margin-top: 20px; border-left: 4px solid #4472C4; padding-left: 10px; }}
    .meta {{ color: #888; font-size: 12px; margin-bottom: 20px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
    th {{ background: #4472C4; color: white; padding: 8px 12px; text-align: center; font-size: 13px; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #eee; text-align: center; font-size: 13px; }}
    tr:nth-child(even) {{ background: #f8f9fa; }}
    .buy {{ background: #E2EFDA !important; }}
    .sell {{ background: #FCE4D6 !important; }}
    .text-red {{ color: #DC3545; font-weight: bold; }}
    .text-green {{ color: #28A745; font-weight: bold; }}
    .stat-box {{ display: inline-block; background: #f0f4ff; padding: 10px 20px; margin: 5px; border-radius: 6px; text-align: center; }}
    .stat-box .label {{ font-size: 12px; color: #666; }}
    .stat-box .value {{ font-size: 20px; font-weight: bold; color: #333; }}
    .footer {{ color: #999; font-size: 11px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 10px; }}
</style>
</head>
<body>
<div class="container">
    <h1>📋 次日条件单 ({today})</h1>
    <div class="meta">生成时间: {now} | 信号: 买入{len(buy_signals)} 卖出{len(sell_signals)} 加仓{len(add_signals)}</div>

    <div>
        <div class="stat-box"><div class="label">持仓市值</div><div class="value">{total_mv/10000:.1f}万</div></div>
        <div class="stat-box"><div class="label">总盈亏</div><div class="value {pnl_color}">{total_pnl:+.0f} ({total_pnl_pct:+.1f}%)</div></div>
        <div class="stat-box"><div class="label">买入信号</div><div class="value" style="color:#28A745">{len(buy_signals)}</div></div>
        <div class="stat-box"><div class="label">卖出信号</div><div class="value" style="color:#DC3545">{len(sell_signals)}</div></div>
    </div>
"""

    if holding_rows:
        html += f"""
    <h2>当前持仓</h2>
    <table><tr><th>代码</th><th>名称</th><th>股数</th><th>成本</th><th>现价</th><th>盈亏</th></tr>
    {"".join(holding_rows)}
    </table>
"""

    if buy_rows:
        html += f"""
    <h2>买入条件单</h2>
    <table><tr><th>代码</th><th>名称</th><th>买入价</th><th>股数</th><th>止损价</th><th>评分</th><th>原因</th></tr>
    {"".join(buy_rows)}
    </table>
"""

    if sell_rows:
        html += f"""
    <h2>卖出条件单</h2>
    <table><tr><th>代码</th><th>名称</th><th>卖出价</th><th>原因</th></tr>
    {"".join(sell_rows)}
    </table>
"""

    if add_rows:
        html += f"""
    <h2>加仓信号</h2>
    <table><tr><th>代码</th><th>名称</th><th>参考价</th><th>原因</th></tr>
    {"".join(add_rows)}
    </table>
"""

    if not buy_rows and not sell_rows and not add_rows:
        html += '<div class="alert" style="background:#FFF3CD;border:1px solid #FFC107;padding:12px;border-radius:4px;margin:10px 0;">次日无操作信号，继续持仓观望</div>\n'

    html += """
    <div class="footer">
        本条件单由交易系统自动生成，请结合盘面实际情况判断是否执行。<br>
        止损价为初始建议值，请根据最新行情调整。
    </div>
</div>
</body>
</html>"""
    return html


def send_eastmoney_orders_email(signals, holdings, data_dict, news_risk=None):
    """
    发送盘前条件单邮件（19:00定时任务）

    参数:
        signals: [(code, signal_dict), ...] 交易信号列表
        holdings: {code: {name, shares, buy_price, current_price, ...}} 持仓
        data_dict: {code: pd.DataFrame} 行情数据
        news_risk: {code: {level, ...}} 新闻风险预警（可选）

    返回: 是否发送成功
    """
    from notify.email_notify import send_email

    today = datetime.date.today().strftime("%Y-%m-%d")
    buy_count = sum(1 for _, s in signals if s.get("buy_signal"))
    sell_count = sum(1 for _, s in signals if s.get("sell_signal"))

    subject = f"📋 次日条件单 {today} | 买{buy_count} 卖{sell_count}"
    html = _build_order_html(signals, holdings, data_dict, news_risk)

    # 同时落盘备份
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(output_dir, exist_ok=True)
    archive_path = os.path.join(output_dir, f"eastmoney_orders_{today.replace('-', '')}.html")
    try:
        with open(archive_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        logger.warning(f"条件单落盘失败: {e}")

    return send_email(subject, html)


def _save_qmt_orders_json(signals, holdings, data_dict):
    """
    生成QMT执行JSON文件，供QMT自动交易执行器读取

    参数:
        signals: [(code, signal_dict), ...]
        holdings: {code: {name, shares, buy_price, ...}}
        data_dict: {code: pd.DataFrame}
    """
    orders = []
    for code, sig in signals:
        order = {"code": code, "name": config.get_stock_name(code)}
        if sig.get("buy_signal"):
            order["action"] = "BUY"
            order["price"] = sig.get("buy_price", 0)
            order["shares"] = sig.get("position", {}).get("shares", 0)
            order["stop_loss"] = sig.get("stop_loss_initial", 0)
        elif sig.get("sell_signal"):
            order["action"] = "SELL"
            order["price"] = sig.get("sell_price", 0)
            order["shares"] = holdings.get(code, {}).get("shares", 0)
        else:
            continue
        order["reason"] = sig.get("signal_reason", "")
        orders.append(order)

    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.date.today().strftime("%Y%m%d")
    json_path = os.path.join(output_dir, f"qmt_orders_{today}.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"date": today, "orders": orders}, f, ensure_ascii=False, indent=2)
        logger.info(f"QMT条件单JSON已保存: {json_path} ({len(orders)}条)")
    except Exception as e:
        logger.error(f"QMT JSON保存失败: {e}")
