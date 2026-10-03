# -*- coding: utf-8 -*-
"""
盘前作战计划邮件
================
08:30 定时任务：持仓快览 + 大盘展望 + 操作清单 + 条件单提醒

接口:
  prepare_morning_brief_data(holdings, data_dict, market_strength, max_pos) -> dict
  send_morning_brief(brief_data) -> bool
"""

import os
import logging
import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def prepare_morning_brief_data(holdings, data_dict, market_strength="normal", max_pos=0.8):
    """
    准备盘前作战计划数据

    参数:
        holdings: {code: {name, shares, buy_price, current_price, stop_loss, ...}}
        data_dict: {code: pd.DataFrame} 含技术指标的行情数据
        market_strength: "strong" | "normal" | "weak"
        max_pos: 最大仓位比例 (0~1)

    返回: 结构化 brief_data dict
    """
    today = datetime.date.today().strftime("%Y-%m-%d")

    # 持仓概览
    holding_summary = []
    total_mv = 0
    total_cost = 0
    for code, pos in holdings.items():
        name = pos.get("name", config.get_stock_name(code))
        shares = pos.get("shares", 0)
        buy_price = pos.get("buy_price", 0)
        current_price = pos.get("current_price", buy_price)

        # 尝试从 data_dict 更新现价
        if code in data_dict and not data_dict[code].empty:
            current_price = data_dict[code].iloc[-1]["close"]

        cost = shares * buy_price
        mv = shares * current_price
        pnl = mv - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0
        total_mv += mv
        total_cost += cost

        # 止损位
        stop_loss = pos.get("stop_loss", buy_price * 0.9)
        # 距止损空间
        dist_to_stop = (current_price - stop_loss) / current_price * 100 if current_price > 0 else 0

        # 技术指标摘要
        trend = "未知"
        if code in data_dict and not data_dict[code].empty:
            df = data_dict[code]
            close = current_price
            ma5 = df["ma5"].iloc[-1] if "ma5" in df.columns else close
            ma20 = df["ma20"].iloc[-1] if "ma20" in df.columns else close
            if close > ma5 > ma20:
                trend = "多头排列"
            elif close < ma5 < ma20:
                trend = "空头排列"
            else:
                trend = "震荡"

        holding_summary.append({
            "code": code, "name": name, "shares": shares,
            "buy_price": buy_price, "current_price": current_price,
            "pnl": pnl, "pnl_pct": pnl_pct,
            "stop_loss": stop_loss, "dist_to_stop": dist_to_stop,
            "trend": trend,
        })

    total_pnl = total_mv - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    position_ratio = total_mv / config.TOTAL_CAPITAL if config.TOTAL_CAPITAL > 0 else 0

    # 大盘判断
    market_map = {
        "strong": {"label": "强势", "color": "#28A745", "advice": "积极做多，仓位可上限"},
        "normal": {"label": "震荡", "color": "#FD7E14", "advice": "精选个股，控制仓位"},
        "weak": {"label": "弱势", "color": "#DC3545", "advice": "防守为主，降低仓位"},
    }
    market_info = market_map.get(market_strength, market_map["normal"])

    # 操作清单
    action_items = []
    for h in holding_summary:
        # 接近止损
        if h["dist_to_stop"] < 3:
            action_items.append({
                "code": h["code"], "name": h["name"],
                "action": "注意止损",
                "detail": f'距止损仅{h["dist_to_stop"]:.1f}%',
                "priority": "high",
            })
        # 浮盈>15%考虑止盈
        if h["pnl_pct"] > 15:
            action_items.append({
                "code": h["code"], "name": h["name"],
                "action": "考虑止盈",
                "detail": f'浮盈{h["pnl_pct"]:+.1f}%，可启动阶梯止盈',
                "priority": "medium",
            })
        # 浮亏>5%关注
        if h["pnl_pct"] < -5:
            action_items.append({
                "code": h["code"], "name": h["name"],
                "action": "关注止损",
                "detail": f'浮亏{h["pnl_pct"]:.1f}%，检查是否触发止损线',
                "priority": "high",
            })

    return {
        "date": today,
        "holdings": holding_summary,
        "total_mv": total_mv,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "position_ratio": position_ratio,
        "market_strength": market_strength,
        "market_info": market_info,
        "max_pos": max_pos,
        "action_items": action_items,
    }


def send_morning_brief(brief_data):
    """
    发送盘前作战计划邮件

    参数:
        brief_data: prepare_morning_brief_data() 返回的结构化 dict

    返回: 是否发送成功
    """
    from notify.email_notify import send_email

    date = brief_data.get("date", datetime.date.today().strftime("%Y-%m-%d"))
    market_info = brief_data.get("market_info", {})
    holdings = brief_data.get("holdings", [])
    action_items = brief_data.get("action_items", [])
    max_pos = brief_data.get("max_pos", 0.8)
    position_ratio = brief_data.get("position_ratio", 0)

    subject = f"🌅 盘前作战计划 {date} | {market_info.get('label', '震荡')}市"

    # 持仓行
    holding_rows = []
    for h in holdings:
        color = "text-red" if h["pnl"] < 0 else "text-green"
        holding_rows.append(
            f'<tr><td>{h["code"]}</td><td>{h["name"]}</td>'
            f'<td>{h["shares"]}</td><td>{h["current_price"]:.2f}</td>'
            f'<td class="{color}">{h["pnl_pct"]:+.1f}%</td>'
            f'<td>{h["stop_loss"]:.2f}</td><td>{h["dist_to_stop"]:.1f}%</td>'
            f'<td>{h["trend"]}</td></tr>'
        )

    # 操作清单行
    action_rows = []
    for a in action_items:
        priority_color = {"high": "text-red", "medium": "text-orange"}.get(a["priority"], "")
        action_rows.append(
            f'<tr><td>{a["code"]}</td><td>{a["name"]}</td>'
            f'<td class="{priority_color}">{a["action"]}</td>'
            f'<td>{a["detail"]}</td></tr>'
        )

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
    .text-red {{ color: #DC3545; font-weight: bold; }}
    .text-green {{ color: #28A745; font-weight: bold; }}
    .text-orange {{ color: #FD7E14; font-weight: bold; }}
    .stat-box {{ display: inline-block; background: #f0f4ff; padding: 10px 20px; margin: 5px; border-radius: 6px; text-align: center; }}
    .stat-box .label {{ font-size: 12px; color: #666; }}
    .stat-box .value {{ font-size: 20px; font-weight: bold; color: #333; }}
    .alert {{ background: #FFF3CD; border: 1px solid #FFC107; padding: 12px; border-radius: 4px; margin: 10px 0; }}
    .alert-danger {{ background: #F8D7DA; border-color: #F5C6CB; }}
    .footer {{ color: #999; font-size: 11px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 10px; }}
</style>
</head>
<body>
<div class="container">
    <h1>🌅 盘前作战计划</h1>
    <div class="meta">{date} | 大盘: <span style="color:{market_info.get('color', '#333')}">{market_info.get('label', '震荡')}</span></div>

    <div>
        <div class="stat-box"><div class="label">持仓市值</div><div class="value">{brief_data.get('total_mv', 0)/10000:.1f}万</div></div>
        <div class="stat-box"><div class="label">总盈亏</div><div class="value {'text-green' if brief_data.get('total_pnl', 0) >= 0 else 'text-red'}">{brief_data.get('total_pnl', 0):+.0f} ({brief_data.get('total_pnl_pct', 0):+.1f}%)</div></div>
        <div class="stat-box"><div class="label">当前仓位</div><div class="value">{position_ratio:.0%}</div></div>
        <div class="stat-box"><div class="label">仓位上限</div><div class="value">{max_pos:.0%}</div></div>
    </div>

    <div class="alert" style="background:#E8F0FE;border-color:#4472C4;">
        <strong>大盘研判:</strong> {market_info.get('advice', '精选个股，控制仓位')}
    </div>
"""

    if holding_rows:
        html += f"""
    <h2>持仓快览</h2>
    <table><tr><th>代码</th><th>名称</th><th>股数</th><th>现价</th><th>盈亏</th><th>止损</th><th>距止损</th><th>趋势</th></tr>
    {"".join(holding_rows)}
    </table>
"""

    if action_rows:
        html += f"""
    <h2>今日操作清单</h2>
    <table><tr><th>代码</th><th>名称</th><th>操作</th><th>说明</th></tr>
    {"".join(action_rows)}
    </table>
"""
    elif not action_items:
        html += '<div class="alert" style="background:#D4EDDA;border-color:#C3E6CB;">✅ 持仓正常，无需特别操作</div>\n'

    html += """
    <div class="footer">
        盘前作战计划由交易系统自动生成，请结合盘面实际情况判断。<br>
        08:30 盘前提醒 · 仅供参考
    </div>
</div>
</body>
</html>"""

    return send_email(subject, html)
