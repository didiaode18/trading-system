"""
仓位管理与资金优化分析模块 V1.0
==================================
基于真实持仓数据，分析仓位风险并生成优化方案

核心分析维度:
  1. 仓位集中度分析（单只/赛道/整体）
  2. 资金利用率分析（现金比例）
  3. 盈亏状态分析（浮亏/浮盈分布）
  4. 止损风险预警
  5. 优化建议（减仓/调仓/加仓方案）

使用方式:
    from strategy.portfolio_analyzer import analyze_portfolio, send_portfolio_email
    result = analyze_portfolio(holdings, data_dict)
"""

import os
import sys
import logging
import datetime
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ============================================================
# 一、仓位分析
# ============================================================

def analyze_portfolio(holdings: dict, data_dict: dict = None) -> dict:
    """
    全面分析当前持仓的资金管理与仓位分配
    
    返回:
        {
            "summary": {...},          # 总体概况
            "position_analysis": [...], # 各持仓详情
            "sector_allocation": [...], # 赛道配置分析
            "risk_alerts": [...],       # 风险预警
            "optimization": {...},      # 优化建议
            "target_allocation": {...}, # 目标仓位配置
        }
    """
    total_capital = config.TOTAL_CAPITAL
    total_market_value = 0
    total_cost = 0
    total_pnl = 0

    position_analysis = []
    sector_values = defaultdict(lambda: {"value": 0, "cost": 0, "count": 0, "stocks": []})

    for code, holding in holdings.items():
        shares = holding["shares"]
        buy_price = holding["buy_price"]
        current_price = holding.get("current_price", buy_price)
        sector = holding.get("sector", "其他")
        stock_type = holding.get("stock_type", "龙头")

        market_value = shares * current_price
        cost_value = shares * buy_price
        pnl = market_value - cost_value
        pnl_pct = (current_price / buy_price - 1) * 100
        position_ratio = market_value / total_capital * 100

        total_market_value += market_value
        total_cost += cost_value
        total_pnl += pnl

        # 赛道聚合
        sector_values[sector]["value"] += market_value
        sector_values[sector]["cost"] += cost_value
        sector_values[sector]["count"] += 1
        sector_values[sector]["stocks"].append({
            "code": code,
            "name": config.STOCK_POOL.get(code, {}).get("名称", code),
            "shares": shares,
            "buy_price": buy_price,
            "current_price": current_price,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "position_ratio": position_ratio,
        })

        position_analysis.append({
            "code": code,
            "name": config.STOCK_POOL.get(code, {}).get("名称", code),
            "sector": sector,
            "type": stock_type,
            "shares": shares,
            "buy_price": buy_price,
            "current_price": current_price,
            "market_value": market_value,
            "cost_value": cost_value,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "position_ratio": round(position_ratio, 2),
        })

    # 按仓位占比排序
    position_analysis.sort(key=lambda x: x["position_ratio"], reverse=True)

    cash = total_capital - total_market_value
    cash_ratio = cash / total_capital * 100
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0

    summary = {
        "total_capital": total_capital,
        "total_market_value": round(total_market_value, 2),
        "cash": round(cash, 2),
        "cash_ratio": round(cash_ratio, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "holding_count": len(holdings),
    }

    # 赛道配置分析
    sector_allocation = []
    for sector, data in sorted(sector_values.items(), key=lambda x: x[1]["value"], reverse=True):
        sector_pnl = data["value"] - data["cost"]
        sector_pnl_pct = (data["value"] / data["cost"] - 1) * 100 if data["cost"] > 0 else 0
        sector_allocation.append({
            "sector": sector,
            "value": round(data["value"], 2),
            "ratio": round(data["value"] / total_capital * 100, 2),
            "pnl": round(sector_pnl, 2),
            "pnl_pct": round(sector_pnl_pct, 2),
            "count": data["count"],
            "stocks": data["stocks"],
        })

    # ---- 风险预警 ----
    risk_alerts = []

    # 1. 现金比例过低
    if cash_ratio < 5:
        risk_alerts.append({
            "level": "critical",
            "type": "现金枯竭",
            "detail": f"现金仅{cash:,.0f}元（{cash_ratio:.2f}%），远低于10%安全线",
            "suggestion": "必须减仓回笼资金，保留至少10%现金应对波动和加仓机会"
        })
    elif cash_ratio < 10:
        risk_alerts.append({
            "level": "warning",
            "type": "现金不足",
            "detail": f"现金{cash:,.0f}元（{cash_ratio:.2f}%），低于10%安全线",
            "suggestion": "建议减仓至现金比例达到10%以上"
        })

    # 2. 单只股票仓位超标
    for pos in position_analysis:
        limit = config.LEADER_STOCK_MAX_RATIO * 100 if pos["type"] == "龙头" else config.FLEXIBLE_STOCK_MAX_RATIO * 100
        if pos["position_ratio"] > limit:
            risk_alerts.append({
                "level": "critical" if pos["position_ratio"] > limit * 1.5 else "warning",
                "type": "单股超标",
                "detail": f"{pos['name']}仓位{pos['position_ratio']:.1f}%，超过{pos['type']}上限{limit:.0f}%",
                "suggestion": f"建议减仓至{limit:.0f}%以内（卖出约{int((pos['position_ratio'] - limit) / 100 * total_capital / pos['current_price'] / 100) * 100}股）"
            })

    # 3. 赛道集中度过高
    sector_max = config.SECTOR_MAX_RATIO * 100
    for sa in sector_allocation:
        if sa["ratio"] > sector_max:
            risk_alerts.append({
                "level": "critical",
                "type": "赛道过度集中",
                "detail": f"{sa['sector']}占比{sa['ratio']:.1f}%，超过{sector_max:.0f}%上限",
                "suggestion": f"建议将{sa['sector']}仓位降至{sector_max:.0f}%以内"
            })

    # 4. 深度亏损预警
    for pos in position_analysis:
        if pos["pnl_pct"] < -20:
            risk_alerts.append({
                "level": "critical",
                "type": "深度亏损",
                "detail": f"{pos['name']}浮亏{pos['pnl_pct']:.1f}%（{pos['pnl']:,.0f}元），已超过20%止损线",
                "suggestion": "建议立即止损或至少减半仓位，避免亏损进一步扩大"
            })
        elif pos["pnl_pct"] < -10:
            risk_alerts.append({
                "level": "warning",
                "type": "较大亏损",
                "detail": f"{pos['name']}浮亏{pos['pnl_pct']:.1f}%（{pos['pnl']:,.0f}元）",
                "suggestion": "密切关注，若继续下跌触及-15%应果断止损"
            })

    # 5. 持仓过于分散
    if len(holdings) > 6:
        risk_alerts.append({
            "level": "warning",
            "type": "持仓分散",
            "detail": f"持有{len(holdings)}只股票，超出建议的4-6只",
            "suggestion": "建议精简至4-6只核心标的，集中资金在最强赛道"
        })

    # ---- 优化建议 ----
    optimization = generate_optimization(position_analysis, sector_allocation, summary, holdings)

    return {
        "summary": summary,
        "position_analysis": position_analysis,
        "sector_allocation": sector_allocation,
        "risk_alerts": risk_alerts,
        "optimization": optimization,
        "scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def generate_optimization(position_analysis: list, sector_allocation: list,
                          summary: dict, holdings: dict) -> dict:
    """
    生成具体的仓位优化建议
    
    原则:
    1. 半导体仓位从~70%降至30%以内
    2. 现金比例恢复至15-20%
    3. 深度亏损股止损
    4. 均衡赛道配置
    """
    total_capital = summary["total_capital"]
    actions = []
    target_cash_ratio = 0.15  # 目标现金比例15%

    # 分析当前半导体总仓位
    semi_sectors = {"半导体设备", "半导体材料", "半导体封测", "存储芯片"}
    semi_value = sum(sa["value"] for sa in sector_allocation if sa["sector"] in semi_sectors)
    semi_ratio = semi_value / total_capital

    # ---- 具体操作建议 ----

    # 1. 雅克科技：深度亏损-25%，建议止损
    yake = next((p for p in position_analysis if p["code"] == "002409"), None)
    if yake and yake["pnl_pct"] < -20:
        sell_shares = yake["shares"]
        recover = sell_shares * yake["current_price"]
        actions.append({
            "action": "止损卖出",
            "code": "002409",
            "name": "雅克科技",
            "detail": f"浮亏{yake['pnl_pct']:.1f}%，已超20%止损线",
            "shares": sell_shares,
            "recover_amount": round(recover, 0),
            "priority": 1,
            "reason": "深度亏损股优先止损，避免亏损扩大"
        })

    # 2. 北方华创：单股仓位26.7%超标，建议减半
    bfhc = next((p for p in position_analysis if p["code"] == "002371"), None)
    if bfhc and bfhc["position_ratio"] > 20:
        # 减至12%左右
        target_shares = int(total_capital * 0.12 / bfhc["current_price"] / 100) * 100
        sell_shares = bfhc["shares"] - target_shares
        if sell_shares > 0:
            recover = sell_shares * bfhc["current_price"]
            actions.append({
                "action": "减仓",
                "code": "002371",
                "name": "北方华创",
                "detail": f"仓位{bfhc['position_ratio']:.1f}%超标，减至~12%",
                "shares": sell_shares,
                "recover_amount": round(recover, 0),
                "priority": 2,
                "reason": "单股仓位过重，降低集中度风险"
            })

    # 3. 长电科技：亏损-12.6%，建议减仓
    cdkt = next((p for p in position_analysis if p["code"] == "600584"), None)
    if cdkt and cdkt["pnl_pct"] < -10:
        sell_shares = int(cdkt["shares"] * 0.5 / 100) * 100  # 减半
        if sell_shares > 0:
            recover = sell_shares * cdkt["current_price"]
            actions.append({
                "action": "减仓",
                "code": "600584",
                "name": "长电科技",
                "detail": f"浮亏{cdkt['pnl_pct']:.1f}%，减半仓位",
                "shares": sell_shares,
                "recover_amount": round(recover, 0),
                "priority": 3,
                "reason": "亏损较大，减半控制风险"
            })

    # 4. 中国卫星：亏损-5.8%，可持有但关注
    zgwx = next((p for p in position_analysis if p["code"] == "600118"), None)

    # 计算优化后的预期仓位
    total_recover = sum(a["recover_amount"] for a in actions)
    new_cash = summary["cash"] + total_recover
    new_cash_ratio = new_cash / total_capital * 100

    # 目标配置
    target = {
        "半导体": {"target_ratio": 30, "current_ratio": round(semi_ratio * 100, 1)},
        "军工": {"target_ratio": 20, "current_ratio": round(
            sum(sa["ratio"] for sa in sector_allocation if sa["sector"] in {"卫星导航", "军工航空"}), 1)},
        "面板显示": {"target_ratio": 10, "current_ratio": round(
            next((sa["ratio"] for sa in sector_allocation if sa["sector"] == "面板显示"), 0), 1)},
        "精密制造": {"target_ratio": 10, "current_ratio": round(
            next((sa["ratio"] for sa in sector_allocation if sa["sector"] == "精密制造"), 0), 1)},
        "现金": {"target_ratio": 15, "current_ratio": summary["cash_ratio"]},
    }

    return {
        "actions": actions,
        "total_recover": round(total_recover, 0),
        "new_cash": round(new_cash, 0),
        "new_cash_ratio": round(new_cash_ratio, 1),
        "target_allocation": target,
        "core_principles": [
            "半导体仓位从70%降至30%以内，分散赛道风险",
            "现金比例恢复至15%以上，保留加仓弹药",
            "深度亏损股（>20%）果断止损，不抱幻想",
            "单只股票不超过总资金15%",
            "持仓精简至4-6只核心标的",
            "军工+半导体双主线，面板/精密制造为辅",
        ]
    }


# ============================================================
# 二、生成HTML报告
# ============================================================

def generate_portfolio_html(result: dict) -> str:
    """生成仓位分析HTML报告"""
    s = result["summary"]
    positions = result["position_analysis"]
    sectors = result["sector_allocation"]
    alerts = result["risk_alerts"]
    opt = result["optimization"]
    scan_time = result["scan_time"]

    # 风险等级颜色
    alert_colors = {"critical": "#FF4D4F", "warning": "#FA8C16", "info": "#1890FF"}

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 20px; background: #f0f2f5; }}
    .container {{ max-width: 1050px; margin: 0 auto; }}
    .header {{ background: linear-gradient(135deg, #FF4D4F, #CF1322); color: white; padding: 20px 30px; border-radius: 12px 12px 0 0; }}
    .header h1 {{ margin: 0; font-size: 22px; }}
    .header .subtitle {{ font-size: 13px; opacity: 0.9; margin-top: 5px; }}
    .content {{ background: white; padding: 20px 30px; border-radius: 0 0 12px 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }}
    .section {{ margin: 20px 0; }}
    .section-title {{ font-size: 16px; font-weight: bold; color: #333; margin-bottom: 12px; padding-left: 12px; border-left: 4px solid #FF4D4F; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ background: #fafafa; padding: 10px 8px; text-align: center; border-bottom: 2px solid #e8e8e8; font-weight: bold; color: #333; }}
    td {{ padding: 10px 8px; text-align: center; border-bottom: 1px solid #f0f0f0; }}
    tr:hover {{ background: #fafafa; }}
    .profit {{ color: #FF4D4F; font-weight: bold; }}
    .loss {{ color: #52C41A; font-weight: bold; }}
    .loss-deep {{ color: #CF1322; font-weight: bold; }}
    .stats {{ display: flex; gap: 12px; margin: 15px 0; flex-wrap: wrap; }}
    .stat-box {{ background: #f5f5f5; padding: 12px 18px; border-radius: 8px; text-align: center; flex: 1; min-width: 120px; }}
    .stat-box .label {{ font-size: 11px; color: #888; }}
    .stat-box .value {{ font-size: 18px; font-weight: bold; color: #333; margin-top: 4px; }}
    .alert-box {{ padding: 12px 16px; border-radius: 8px; margin: 8px 0; border-left: 4px solid; }}
    .alert-critical {{ background: #FFF1F0; border-color: #FF4D4F; }}
    .alert-warning {{ background: #FFF7E6; border-color: #FA8C16; }}
    .alert-title {{ font-weight: bold; font-size: 13px; margin-bottom: 4px; }}
    .alert-detail {{ font-size: 12px; color: #666; }}
    .alert-suggest {{ font-size: 12px; color: #1890FF; margin-top: 4px; }}
    .action-card {{ background: #f0f5ff; border: 1px solid #adc6ff; border-radius: 8px; padding: 14px; margin: 10px 0; }}
    .action-card .action-header {{ display: flex; justify-content: space-between; align-items: center; }}
    .action-card .action-name {{ font-weight: bold; font-size: 14px; }}
    .action-card .action-priority {{ background: #FF4D4F; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; }}
    .action-card .action-detail {{ font-size: 12px; color: #666; margin-top: 6px; }}
    .action-card .action-reason {{ font-size: 12px; color: #1890FF; margin-top: 4px; }}
    .progress-bar {{ height: 8px; border-radius: 4px; background: #f0f0f0; margin: 4px 0; overflow: hidden; }}
    .progress-fill {{ height: 100%; border-radius: 4px; }}
    .fill-red {{ background: #FF4D4F; }}
    .fill-green {{ background: #52C41A; }}
    .fill-blue {{ background: #1890FF; }}
    .fill-orange {{ background: #FA8C16; }}
    .guide {{ background: #E6F7FF; border: 1px solid #91D5FF; border-radius: 8px; padding: 15px; margin: 15px 0; font-size: 13px; }}
    .guide h3 {{ margin: 0 0 8px; color: #096DD9; font-size: 14px; }}
    .guide ol {{ margin: 5px 0; padding-left: 20px; line-height: 1.8; }}
    .footer {{ text-align: center; color: #bbb; font-size: 11px; margin-top: 20px; padding-top: 15px; border-top: 1px solid #eee; }}
    .note {{ font-size: 11px; color: #999; }}
    .tag {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; }}
    .tag-semi {{ background: #F9F0FF; color: #722ED1; }}
    .tag-military {{ background: #FFF7E6; color: #D46B08; }}
    .tag-other {{ background: #E6F7FF; color: #1890FF; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>仓位管理与资金优化方案</h1>
        <div class="subtitle">分析时间: {scan_time} | 总资金: {s['total_capital']:,.0f}元 | 持仓{s['holding_count']}只</div>
    </div>
    <div class="content">

        <!-- 总体概况 -->
        <div class="stats">
            <div class="stat-box" style="border-top:3px solid #FF4D4F">
                <div class="label">总资产</div>
                <div class="value">{s['total_capital']:,.0f}</div>
            </div>
            <div class="stat-box" style="border-top:3px solid #FA8C16">
                <div class="label">持仓市值</div>
                <div class="value">{s['total_market_value']:,.0f}</div>
            </div>
            <div class="stat-box" style="border-top:3px solid {'#52C41A' if s['cash_ratio'] >= 10 else '#FF4D4F'}">
                <div class="label">可用现金</div>
                <div class="value" style="color:{'#52C41A' if s['cash_ratio'] >= 10 else '#FF4D4F'}">{s['cash']:,.0f}</div>
                <div class="note">占比{s['cash_ratio']:.1f}% {'✓' if s['cash_ratio'] >= 10 else '⚠ 严重不足'}</div>
            </div>
            <div class="stat-box" style="border-top:3px solid {'#52C41A' if s['total_pnl'] >= 0 else '#FF4D4F'}">
                <div class="label">持仓盈亏</div>
                <div class="value" style="color:{'#52C41A' if s['total_pnl'] >= 0 else '#FF4D4F'}">{s['total_pnl']:+,.0f}</div>
                <div class="note">{s['total_pnl_pct']:+.1f}%</div>
            </div>
        </div>

        <!-- 风险预警 -->
"""

    if alerts:
        html += '        <div class="section"><div class="section-title">风险预警</div>\n'
        for alert in alerts:
            css = "alert-critical" if alert["level"] == "critical" else "alert-warning"
            html += f"""
            <div class="alert-box {css}">
                <div class="alert-title" style="color:{alert_colors.get(alert['level'], '#333')}">⚠ {alert['type']}</div>
                <div class="alert-detail">{alert['detail']}</div>
                <div class="alert-suggest">→ {alert['suggestion']}</div>
            </div>
"""
        html += '        </div>\n'

    # 持仓明细
    html += """
        <div class="section">
            <div class="section-title">当前持仓明细</div>
            <table>
                <tr><th>代码</th><th>名称</th><th>赛道</th><th>类型</th><th>持仓</th><th>成本</th><th>现价</th><th>仓位占比</th><th>浮盈亏</th><th>盈亏额</th></tr>
"""
    for pos in positions:
        pnl_class = "profit" if pos["pnl"] > 0 else ("loss-deep" if pos["pnl_pct"] < -10 else "loss")
        tag_class = "tag-semi" if pos["sector"].startswith("半导体") or pos["sector"] == "存储芯片" else ("tag-military" if "军工" in pos["sector"] or "卫星" in pos["sector"] else "tag-other")
        html += f"""
                <tr>
                    <td>{pos['code']}</td>
                    <td style="font-weight:bold">{pos['name']}</td>
                    <td><span class="tag {tag_class}">{pos['sector']}</span></td>
                    <td>{pos['type']}</td>
                    <td>{pos['shares']}股</td>
                    <td>{pos['buy_price']:.2f}</td>
                    <td>{pos['current_price']:.2f}</td>
                    <td>
                        {pos['position_ratio']:.1f}%
                        <div class="progress-bar"><div class="progress-fill {'fill-red' if pos['position_ratio'] > 20 else 'fill-blue'}" style="width:{min(pos['position_ratio'] * 3, 100)}%"></div></div>
                    </td>
                    <td class="{pnl_class}">{pos['pnl_pct']:+.1f}%</td>
                    <td class="{pnl_class}">{pos['pnl']:+,.0f}元</td>
                </tr>
"""
    html += """
            </table>
        </div>
"""

    # 赛道配置
    html += """
        <div class="section">
            <div class="section-title">赛道配置分析</div>
            <table>
                <tr><th>赛道</th><th>持仓市值</th><th>占比</th><th>盈亏</th><th>股票数</th><th>状态</th></tr>
"""
    sector_max = config.SECTOR_MAX_RATIO * 100
    for sa in sectors:
        status = ""
        status_color = "#52C41A"
        if sa["ratio"] > sector_max:
            status = f"超标（上限{sector_max:.0f}%）"
            status_color = "#FF4D4F"
        elif sa["ratio"] > sector_max * 0.8:
            status = "接近上限"
            status_color = "#FA8C16"
        else:
            status = "正常"

        pnl_class = "profit" if sa["pnl"] > 0 else "loss"
        html += f"""
                <tr>
                    <td style="font-weight:bold">{sa['sector']}</td>
                    <td>{sa['value']:,.0f}元</td>
                    <td>
                        {sa['ratio']:.1f}%
                        <div class="progress-bar"><div class="progress-fill {'fill-red' if sa['ratio'] > sector_max else 'fill-blue'}" style="width:{min(sa['ratio'] * 2, 100)}%"></div></div>
                    </td>
                    <td class="{pnl_class}">{sa['pnl']:+,.0f}元 ({sa['pnl_pct']:+.1f}%)</td>
                    <td>{sa['count']}只</td>
                    <td style="color:{status_color}">{status}</td>
                </tr>
"""
    html += """
            </table>
        </div>
"""

    # 优化建议
    html += """
        <div class="section">
            <div class="section-title">优化操作建议</div>
"""
    if opt["actions"]:
        for action in sorted(opt["actions"], key=lambda x: x["priority"]):
            html += f"""
            <div class="action-card">
                <div class="action-header">
                    <span class="action-name">优先级{action['priority']}: {action['action']} {action['name']}（{action['code']}）</span>
                    <span class="action-priority">P{action['priority']}</span>
                </div>
                <div class="action-detail">{action['detail']} | 卖出{action['shares']}股 | 回笼资金{action['recover_amount']:,.0f}元</div>
                <div class="action-reason">理由: {action['reason']}</div>
            </div>
"""
    else:
        html += '<p style="text-align:center;color:#999;padding:20px">当前仓位合理，无需调整</p>'

    # 优化后预期
    html += f"""
            <div class="stats" style="margin-top:15px">
                <div class="stat-box" style="background:#F6FFED;border:1px solid #B7EB8F">
                    <div class="label">优化后现金</div>
                    <div class="value" style="color:#52C41A">{opt['new_cash']:,.0f}元</div>
                    <div class="note">占比{opt['new_cash_ratio']:.1f}% {'✓' if opt['new_cash_ratio'] >= 10 else '⚠'}</div>
                </div>
                <div class="stat-box" style="background:#F6FFED;border:1px solid #B7EB8F">
                    <div class="label">回笼资金</div>
                    <div class="value" style="color:#1890FF">{opt['total_recover']:,.0f}元</div>
                </div>
            </div>
        </div>
"""

    # 目标配置
    html += """
        <div class="section">
            <div class="section-title">目标仓位配置</div>
            <table>
                <tr><th>赛道/资产</th><th>当前占比</th><th>目标占比</th><th>调整方向</th></tr>
"""
    for name, data in opt["target_allocation"].items():
        diff = data["target_ratio"] - data["current_ratio"]
        if diff > 5:
            direction = f'<span style="color:#52C41A">加仓 +{diff:.0f}%</span>'
        elif diff < -5:
            direction = f'<span style="color:#FF4D4F">减仓 {diff:.0f}%</span>'
        else:
            direction = f'<span style="color:#888">维持</span>'

        html += f"""
                <tr>
                    <td style="font-weight:bold">{name}</td>
                    <td>{data['current_ratio']:.1f}%</td>
                    <td>{data['target_ratio']}%</td>
                    <td>{direction}</td>
                </tr>
"""
    html += """
            </table>
        </div>
"""

    # 核心原则
    html += """
        <div class="guide">
            <h3>仓位管理核心原则</h3>
            <ol>
"""
    for principle in opt["core_principles"]:
        html += f"                <li>{principle}</li>\n"

    html += """
            </ol>
        </div>

        <div class="guide" style="background:#FFF7E6;border-color:#FFD591">
            <h3 style="color:#D46B08">分批减仓执行建议</h3>
            <ol style="margin:5px 0;padding-left:20px;line-height:1.8">
                <li><b>第一步</b>：优先止损雅克科技（浮亏>20%），回笼约13万元</li>
                <li><b>第二步</b>：北方华创减半仓位，回笼约10万元</li>
                <li><b>第三步</b>：长电科技减半，回笼约5万元</li>
                <li><b>第四步</b>：现金比例恢复至15%后，等待新的买入信号</li>
                <li><b>第五步</b>：新资金优先配置军工赛道（当前仅17%，目标20%）</li>
            </ol>
        </div>

        <div class="footer">
            本报告由仓位管理分析模块自动生成 | 仅供参考，不构成投资建议<br>
            股市有风险，投资需谨慎
        </div>
    </div>
</div>
</body>
</html>"""

    return html


# ============================================================
# 三、发送邮件
# ============================================================

def send_portfolio_email(result: dict) -> bool:
    """生成并发送仓位分析邮件"""
    from notify.email_notify import send_email

    alerts_count = len([a for a in result["risk_alerts"] if a["level"] == "critical"])
    subject = f"[仓位分析] 资金优化方案 | {alerts_count}项风险预警 | 建议回笼{result['optimization']['total_recover']:,.0f}元"
    html_content = generate_portfolio_html(result)

    return send_email(subject, html_content)


# ============================================================
# 测试入口
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=" * 50)
    print("  仓位管理分析 - 测试")
    print("=" * 50)

    # 加载持仓
    import json
    holdings_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "holdings.json")
    with open(holdings_file, "r") as f:
        holdings = json.load(f)

    result = analyze_portfolio(holdings)

    print(f"\n总资金: {result['summary']['total_capital']:,.0f}元")
    print(f"现金: {result['summary']['cash']:,.0f}元 ({result['summary']['cash_ratio']:.1f}%)")
    print(f"持仓盈亏: {result['summary']['total_pnl']:+,.0f}元 ({result['summary']['total_pnl_pct']:+.1f}%)")
    print(f"\n风险预警: {len(result['risk_alerts'])}项")
    for alert in result["risk_alerts"]:
        print(f"  [{alert['level']}] {alert['type']}: {alert['detail']}")

    print(f"\n优化建议:")
    for action in result["optimization"]["actions"]:
        print(f"  P{action['priority']}: {action['action']} {action['name']} {action['shares']}股 → 回笼{action['recover_amount']:,.0f}元")

    print(f"\n优化后现金: {result['optimization']['new_cash']:,.0f}元 ({result['optimization']['new_cash_ratio']:.1f}%)")
    print("\n[OK] 仓位分析完成")
