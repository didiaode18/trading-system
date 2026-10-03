# -*- coding: utf-8 -*-
"""
盘后精简日报
============
15:30 盘后数据确认：持仓盈亏 + 信号摘要 + 大盘状态 + 选股结果

接口:
  send_daily_digest(digest_data, lite_mode=False) -> bool

digest_data 结构:
  {
    "holdings": dict,          # {code: {name, shares, buy_price, current_price, ...}}
    "data_dict": dict,         # {code: pd.DataFrame}
    "signals": list,           # [(code, signal_dict), ...]
    "holdings_count": list,    # 持仓代码列表
    "market": {
      "market_state": str,     # "strong"/"normal"/"weak"
      "strength": str,
      "suggested_position": float,
      "northbound": dict,      # 可选
      "hot_sectors": list,     # 可选
    },
    "vol_info": dict,          # 波动率信息
    "v9_summary": dict,        # V9.0 摘要
    "screener": dict,          # 选股结果
    "portfolio": dict,         # 仓位分析
    "risk_report": dict,       # 组合风控
    "forecast": list,          # 趋势预测
  }
"""

import os
import logging
import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def send_daily_digest(digest_data, lite_mode=False, **kwargs):
    """
    发送每日精简日报

    参数:
        digest_data: 综合日报数据 dict
        lite_mode: True=15:30精简模式, False=16:15完整版

    返回: 是否发送成功
    """
    from notify.email_notify import send_email

    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M")

    holdings = digest_data.get("holdings", {})
    signals = digest_data.get("signals", [])
    market = digest_data.get("market", {})
    v9 = digest_data.get("v9_summary", {})
    screener = digest_data.get("screener", {})
    risk_report = digest_data.get("risk_report", {})

    # 统计
    buy_count = sum(1 for _, s in signals if s.get("buy_signal"))
    sell_count = sum(1 for _, s in signals if s.get("sell_signal"))

    # 持仓盈亏
    total_mv = 0
    total_cost = 0
    holding_rows = []
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
            f'<td>{current_price:.2f}</td>'
            f'<td class="{color}">{pnl:+.0f} ({pnl_pct:+.1f}%)</td></tr>'
        )

    total_pnl = total_mv - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    position_ratio = total_mv / config.TOTAL_CAPITAL if config.TOTAL_CAPITAL > 0 else 0

    # 大盘状态
    market_state = market.get("market_state", "normal")
    market_label = {"strong": "强势", "normal": "震荡", "weak": "弱势"}.get(market_state, "震荡")
    market_color = {"strong": "#28A745", "normal": "#FD7E14", "weak": "#DC3545"}.get(market_state, "#FD7E14")
    suggested_pos = market.get("suggested_position", 0.8)

    # 选股结果
    qualified_count = screener.get("qualified_count", 0) if screener else 0
    watch_count = len(screener.get("watch_list", [])) if screener else 0

    # 风控评分
    risk_score = risk_report.get("risk_score", "") if risk_report else ""
    risk_level = risk_report.get("overall_level", "") if risk_report else ""

    # V9 摘要
    meta = v9.get("meta_label", {})
    vol_scale = v9.get("vol_scale", "")

    subject = f"📊 盘后日报 {today} | 盈亏{total_pnl:+.0f} | 买{buy_count}卖{sell_count}"

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
    .stat-box {{ display: inline-block; background: #f0f4ff; padding: 10px 20px; margin: 5px; border-radius: 6px; text-align: center; }}
    .stat-box .label {{ font-size: 12px; color: #666; }}
    .stat-box .value {{ font-size: 20px; font-weight: bold; color: #333; }}
    .footer {{ color: #999; font-size: 11px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 10px; }}
</style>
</head>
<body>
<div class="container">
    <h1>📊 盘后日报 ({today})</h1>
    <div class="meta">生成时间: {now} | 模式: {"精简" if lite_mode else "完整"}</div>

    <div>
        <div class="stat-box"><div class="label">持仓市值</div><div class="value">{total_mv/10000:.1f}万</div></div>
        <div class="stat-box"><div class="label">总盈亏</div><div class="value {'text-green' if total_pnl >= 0 else 'text-red'}">{total_pnl:+.0f} ({total_pnl_pct:+.1f}%)</div></div>
        <div class="stat-box"><div class="label">仓位</div><div class="value">{position_ratio:.0%}</div></div>
        <div class="stat-box"><div class="label">大盘</div><div class="value" style="color:{market_color}">{market_label}</div></div>
    </div>
"""

    # 持仓概览
    if holding_rows:
        html += f"""
    <h2>持仓盈亏</h2>
    <table><tr><th>代码</th><th>名称</th><th>股数</th><th>现价</th><th>盈亏</th></tr>
    {"".join(holding_rows)}
    </table>
"""

    # 信号摘要
    html += f"""
    <h2>信号摘要</h2>
    <p>买入信号: <strong>{buy_count}</strong>只 | 卖出信号: <strong>{sell_count}</strong>只</p>
"""

    # Meta-Label 统计
    if meta and any(meta.values()):
        html += f'    <p>Meta-Label: 执行{meta.get("execute", 0)} 观察{meta.get("observe", 0)} 拒绝{meta.get("reject", 0)}</p>\n'

    # 波动率
    if vol_scale:
        html += f'    <p>波动率缩放: {vol_scale}</p>\n'

    # 选股结果
    if screener:
        html += f"""
    <h2>选股引擎</h2>
    <p>入选: <strong>{qualified_count}</strong>只 | 观察池: <strong>{watch_count}</strong>只</p>
"""

    # 风控
    if risk_score:
        html += f'    <p>组合风险评分: <strong>{risk_score}</strong>/100 ({risk_level})</p>\n'

    html += f"""
    <div class="footer">
        盘后日报由交易系统自动生成 · {now} · 仅供参考
    </div>
</div>
</body>
</html>"""

    # 落盘备份
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(output_dir, exist_ok=True)
    archive_path = os.path.join(output_dir, f"daily_digest_{today.replace('-', '')}.html")
    try:
        with open(archive_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    return send_email(subject, html)
