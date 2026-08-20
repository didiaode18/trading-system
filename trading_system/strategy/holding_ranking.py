"""
持仓末位淘汰排名模块 V1.0
============================
基于多维度加权评分，对全部持仓进行排名，识别弱势标的并给出淘汰建议

设计原则:
  1. 相对排名而非绝对阈值 —— 即使全部盈利，也淘汰最弱的
  2. 四维加权评分 —— 技术面(40%) + 盈亏表现(30%) + 趋势强度(20%) + 资金效率(10%)
  3. 仅在持仓数超限时触发淘汰建议 —— 持仓≤6只时不主动淘汰
  4. 与现有机制互补 —— 不替代止损/梯度减仓，而是在它们之上增加"相对强弱"视角

触发时机:
  - 每日16:15综合分析报告（作为"持仓健康度"板块）
  - 每周六周度策略报告（作为"周末持仓审视"板块）

使用方式:
    from strategy.holding_ranking import rank_holdings, generate_ranking_html
    result = rank_holdings(holdings, tech_scores)
"""

import os
import sys
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ============================================================
# 一、配置
# ============================================================

# V10.0: 持仓末位淘汰配置（可从config.py覆盖）
_DEFAULT_ELIMINATION_CONFIG = {
    # 触发条件
    "min_holdings_to_trigger": 7,      # 持仓数≥7只时才触发淘汰建议（≤6只不淘汰）
    "max_holdings_target": 6,          # 淘汰目标数量（精简到此数）
    "eliminate_count": 1,              # 每次最多建议淘汰1只（保守策略）

    # 评分维度权重（四维加和=1.0）
    "weight_tech_score": 0.40,         # 技术评分（0-100分，来自trend_forecast）
    "weight_pnl_rank": 0.30,           # 盈亏表现排名（百分位，0-100）
    "weight_trend_strength": 0.20,     # 趋势强度（基于均线排列+动量）
    "weight_capital_efficiency": 0.10, # 资金效率（仓位利用率）

    # 淘汰门槛
    "eliminate_score_threshold": 35,   # 综合排名分<35才标记"建议淘汰"
    "eliminate_score_warning": 45,     # 综合排名分<45标记"关注"

    # 保护规则（防止误淘汰）
    "protect_recent_buy_days": 5,      # 买入≤5天的不淘汰（给新买入标的观察期）
    "protect_profit_above_pct": 15,    # 浮盈>15%的不淘汰（强势股保护）
    "protect_sector_unique": True,     # 唯一赛道标的保护（该赛道仅剩1只时不淘汰）
}


def _get_elimination_config():
    """获取淘汰配置（优先从config.py读取，否则用默认值）"""
    cfg = dict(_DEFAULT_ELIMINATION_CONFIG)
    override = getattr(config, "ELIMINATION_CONFIG", {})
    if override:
        cfg.update(override)
    return cfg


# ============================================================
# 二、核心排名逻辑
# ============================================================

def rank_holdings(holdings: dict, tech_scores: dict = None) -> dict:
    """
    对全部持仓进行多维度排名

    参数:
        holdings: 持仓字典 {code: {shares, buy_price, current_price, ...}}
        tech_scores: 技术评分字典 {code: {"composite": float, "trend": str, ...}}
                     如果为None，则仅使用持仓数据中的信息

    返回:
        {
            "rankings": [...],          # 排名列表（从强到弱）
            "eliminate_suggestions": [...],  # 建议淘汰列表
            "summary": {...},           # 排名概况
            "health_score": float,      # 持仓健康度（0-100）
        }
    """
    cfg = _get_elimination_config()

    if tech_scores is None:
        tech_scores = {}

    # 1. 收集有效持仓
    positions = []
    for code, h in holdings.items():
        shares = h.get("shares", 0)
        buy_price = h.get("buy_price", 0)
        current_price = h.get("current_price", buy_price)
        if shares <= 0 or buy_price <= 0:
            continue

        pnl_pct = (current_price / buy_price - 1) * 100
        market_value = shares * current_price
        total_capital = getattr(config, "TOTAL_CAPITAL", 0) or getattr(config, "RISK_UNIFIED_CONFIG", {}).get("total_capital", 650000)
        position_ratio = market_value / total_capital * 100 if total_capital > 0 else 0

        # 技术评分
        tech = tech_scores.get(code, {})
        tech_composite = tech.get("composite", 50.0)  # 默认50（中性）
        trend_label = tech.get("trend", "横盘整理")

        # 买入日期
        buy_date_str = h.get("buy_date", "")
        try:
            buy_dt = datetime.date.fromisoformat(buy_date_str)
            hold_days = (datetime.date.today() - buy_dt).days
        except (ValueError, TypeError):
            hold_days = 999  # 未知日期视为长期持仓

        positions.append({
            "code": code,
            "name": h.get("name", code),
            "shares": shares,
            "buy_price": buy_price,
            "current_price": current_price,
            "market_value": market_value,
            "position_ratio": round(position_ratio, 2),
            "pnl_pct": round(pnl_pct, 2),
            "tech_score": round(tech_composite, 1),
            "trend": trend_label,
            "hold_days": hold_days,
            "buy_date": buy_date_str,
            "sector": h.get("sector", "其他"),
        })

    if not positions:
        return {
            "rankings": [],
            "eliminate_suggestions": [],
            "summary": {"total": 0, "avg_score": 0, "health_score": 0},
            "health_score": 0,
        }

    # 2. 四维评分计算

    # 2a. 技术评分维度（直接使用composite，0-100）
    # 已在positions中

    # 2b. 盈亏表现排名（百分位排名）
    pnl_values = sorted([p["pnl_pct"] for p in positions])
    n = len(positions)
    for p in positions:
        # 百分位排名：最好的=100，最差的=0
        rank_idx = pnl_values.index(p["pnl_pct"])
        p["pnl_rank_pct"] = round(rank_idx / max(n - 1, 1) * 100, 1)

    # 2c. 趋势强度评分（基于技术评分+趋势标签+持仓天数内的表现）
    trend_bonus = {
        "强势上涨": 20, "偏多震荡": 10, "横盘整理": 0,
        "偏空震荡": -10, "弱势下跌": -20,
    }
    for p in positions:
        trend_base = p["tech_score"]  # 技术评分作为基础
        bonus = 0
        for label, b in trend_bonus.items():
            if label in p["trend"]:
                bonus = b
                break
        # 趋势强度 = 技术评分 * 0.7 + 趋势加分 * 3（映射到0-100）
        p["trend_strength"] = max(0, min(100, trend_base * 0.7 + bonus * 3 + 15))

    # 2d. 资金效率评分（仓位利用率）
    # 理想仓位: 10-15%（满分），过低(<3%)或过高(>20%)扣分
    for p in positions:
        pr = p["position_ratio"]
        if pr >= 8 and pr <= 18:
            p["capital_efficiency"] = 80  # 理想区间
        elif pr >= 5 and pr <= 25:
            p["capital_efficiency"] = 60  # 可接受
        elif pr < 3:
            p["capital_efficiency"] = 20  # 仓位太小，资金浪费
        elif pr < 5:
            p["capital_efficiency"] = 40  # 偏低
        else:
            p["capital_efficiency"] = 50  # 偏高但可接受

    # 3. 加权综合评分
    for p in positions:
        p["ranking_score"] = round(
            p["tech_score"] * cfg["weight_tech_score"]
            + p["pnl_rank_pct"] * cfg["weight_pnl_rank"]
            + p["trend_strength"] * cfg["weight_trend_strength"]
            + p["capital_efficiency"] * cfg["weight_capital_efficiency"]
        , 1)

    # 4. 排序（从强到弱）
    positions.sort(key=lambda x: x["ranking_score"], reverse=True)
    for i, p in enumerate(positions):
        p["rank"] = i + 1

    # 5. 淘汰建议生成
    eliminate_suggestions = []
    total_holdings = len(positions)

    if total_holdings >= cfg["min_holdings_to_trigger"]:
        # 从最弱的开始检查
        eliminated_count = 0
        # 统计各赛道持仓数（用于唯一赛道保护）
        sector_counts = {}
        for p in positions:
            sector_counts[p["sector"]] = sector_counts.get(p["sector"], 0) + 1

        for p in reversed(positions):  # 从最弱开始
            if eliminated_count >= cfg["eliminate_count"]:
                break

            # 保护规则检查
            protected = False
            protect_reason = ""

            # 保护1: 新买入标的（≤5天）
            if p["hold_days"] <= cfg["protect_recent_buy_days"]:
                protected = True
                protect_reason = f"买入仅{p['hold_days']}天(观察期)"

            # 保护2: 强势股（浮盈>15%）
            elif p["pnl_pct"] >= cfg["protect_profit_above_pct"]:
                protected = True
                protect_reason = f"浮盈{p['pnl_pct']:.1f}%>15%(强势保护)"

            # 保护3: 唯一赛道
            elif cfg["protect_sector_unique"] and sector_counts.get(p["sector"], 0) <= 1:
                protected = True
                protect_reason = f"{p['sector']}唯一持仓(赛道保护)"

            if protected:
                p["protected"] = True
                p["protect_reason"] = protect_reason
                continue

            # 淘汰判定
            if p["ranking_score"] < cfg["eliminate_score_threshold"]:
                action = "建议清仓"
            elif p["ranking_score"] < cfg["eliminate_score_warning"]:
                action = "建议减仓50%"
            else:
                continue  # 评分够高，不淘汰

            # 计算释放资金
            release_amount = p["market_value"]
            if action == "建议减仓50%":
                release_amount = p["market_value"] * 0.5

            eliminate_suggestions.append({
                "code": p["code"],
                "name": p["name"],
                "rank": p["rank"],
                "ranking_score": p["ranking_score"],
                "action": action,
                "pnl_pct": p["pnl_pct"],
                "tech_score": p["tech_score"],
                "position_ratio": p["position_ratio"],
                "market_value": p["market_value"],
                "release_amount": round(release_amount, 0),
                "sector": p["sector"],
                "reason": _build_eliminate_reason(p),
            })
            eliminated_count += 1

    # 6. 持仓健康度（0-100）
    if positions:
        avg_score = sum(p["ranking_score"] for p in positions) / len(positions)
        # 健康度 = 平均排名分 * (1 - 持仓超限惩罚)
        over_limit_penalty = max(0, (total_holdings - cfg["max_holdings_target"])) * 5
        health_score = max(0, min(100, avg_score - over_limit_penalty))
    else:
        avg_score = 0
        health_score = 0

    return {
        "rankings": positions,
        "eliminate_suggestions": eliminate_suggestions,
        "summary": {
            "total": total_holdings,
            "avg_score": round(avg_score, 1),
            "max_holdings_target": cfg["max_holdings_target"],
            "min_holdings_to_trigger": cfg["min_holdings_to_trigger"],
            "eliminate_count": len(eliminate_suggestions),
        },
        "health_score": round(health_score, 1),
        "scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _build_eliminate_reason(p: dict) -> str:
    """构建淘汰理由"""
    reasons = []
    if p["tech_score"] < 50:
        reasons.append(f"技术评分仅{p['tech_score']:.0f}分(偏弱)")
    if p["pnl_pct"] < 0:
        reasons.append(f"浮亏{p['pnl_pct']:.1f}%")
    elif p["pnl_pct"] < 3:
        reasons.append(f"浮盈仅{p['pnl_pct']:.1f}%(收益微薄)")
    if p["position_ratio"] < 3:
        reasons.append(f"仓位仅{p['position_ratio']:.1f}%(资金浪费)")
    if p["hold_days"] > 15 and p["pnl_pct"] < 5:
        reasons.append(f"持仓{p['hold_days']}天收益不足")
    if not reasons:
        reasons.append(f"综合排名{p['rank']}/{p.get('_total', '?')}位，评分{p['ranking_score']:.0f}偏低")
    return "；".join(reasons)


# ============================================================
# 三、HTML报告生成
# ============================================================

def generate_ranking_html(result: dict) -> str:
    """生成持仓排名HTML板块"""
    rankings = result["rankings"]
    suggestions = result["eliminate_suggestions"]
    summary = result["summary"]
    health = result["health_score"]
    scan_time = result.get("scan_time", "")

    if not rankings:
        return ""

    # 健康度颜色
    if health >= 70:
        health_color = "#52C41A"
        health_label = "健康"
    elif health >= 50:
        health_color = "#FA8C16"
        health_label = "一般"
    else:
        health_color = "#FF4D4F"
        health_label = "需优化"

    html = f"""
<h2>持仓健康度与末位淘汰排名</h2>
<div style="font-size:12px;color:#888;margin-bottom:10px">
    扫描时间: {scan_time} | 持仓{summary['total']}只 | 
    目标≤{summary['max_holdings_target']}只 | 
    健康度: <span style="color:{health_color};font-weight:bold;font-size:16px">{health:.0f}</span>/100 
    <span style="color:{health_color}">({health_label})</span>
</div>
"""

    # 淘汰建议（醒目区域）
    if suggestions:
        html += """
<div style="background:#FFF1F0;border:1px solid #FFA39E;border-radius:8px;padding:14px;margin:12px 0">
    <div style="font-weight:bold;color:#CF1322;font-size:14px;margin-bottom:8px">
        📉 末位淘汰建议（持仓{total}只 &gt; 目标{target}只）
    </div>
""".format(total=summary["total"], target=summary["max_holdings_target"])

        for s in suggestions:
            html += """
    <div style="background:white;border-radius:6px;padding:10px;margin:6px 0;border-left:4px solid #FF4D4F">
        <div style="font-weight:bold;color:#333">
            排名#{rank} {name}({code}) — {action}
        </div>
        <div style="font-size:12px;color:#666;margin-top:4px">
            综合评分: {score:.0f} | 技术评分: {tech:.0f} | 浮盈: {pnl:+.1f}% | 仓位: {pos:.1f}%
        </div>
        <div style="font-size:12px;color:#1890FF;margin-top:4px">
            淘汰理由: {reason}
        </div>
        <div style="font-size:12px;color:#52C41A;margin-top:2px">
            释放资金: {release:,.0f}元
        </div>
    </div>
""".format(
                rank=s["rank"], name=s["name"], code=s["code"],
                action=s["action"], score=s["ranking_score"],
                tech=s["tech_score"], pnl=s["pnl_pct"],
                pos=s["position_ratio"], reason=s["reason"],
                release=s["release_amount"],
            )
        html += "</div>\n"
    else:
        if summary["total"] <= summary["max_holdings_target"]:
            html += """
<div style="background:#F6FFED;border:1px solid #B7EB8F;border-radius:8px;padding:14px;margin:12px 0">
    <div style="font-weight:bold;color:#389E0D;font-size:14px">
        ✅ 持仓数量合理（{total}只 ≤ 目标{target}只），无需淘汰
    </div>
</div>
""".format(total=summary["total"], target=summary["max_holdings_target"])
        else:
            html += """
<div style="background:#FFF7E6;border:1px solid #FFD591;border-radius:8px;padding:14px;margin:12px 0">
    <div style="font-weight:bold;color:#D46B08;font-size:14px">
        ⚠️ 持仓{total}只略超目标{target}只，但当前无评分低于淘汰线的标的，继续观察
    </div>
</div>
""".format(total=summary["total"], target=summary["max_holdings_target"])

    # 全量排名表
    html += """
<div style="margin-top:15px">
    <div style="font-size:14px;font-weight:bold;margin-bottom:8px;border-left:4px solid #1890FF;padding-left:10px">
        持仓综合排名（从强到弱）
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
        <tr style="background:#fafafa">
            <th style="padding:8px;text-align:center;border-bottom:2px solid #e8e8e8">排名</th>
            <th style="padding:8px;text-align:left;border-bottom:2px solid #e8e8e8">标的</th>
            <th style="padding:8px;text-align:center;border-bottom:2px solid #e8e8e8">综合评分</th>
            <th style="padding:8px;text-align:center;border-bottom:2px solid #e8e8e8">技术评分</th>
            <th style="padding:8px;text-align:center;border-bottom:2px solid #e8e8e8">盈亏排名</th>
            <th style="padding:8px;text-align:center;border-bottom:2px solid #e8e8e8">趋势强度</th>
            <th style="padding:8px;text-align:center;border-bottom:2px solid #e8e8e8">资金效率</th>
            <th style="padding:8px;text-align:center;border-bottom:2px solid #e8e8e8">浮盈</th>
            <th style="padding:8px;text-align:center;border-bottom:2px solid #e8e8e8">仓位</th>
            <th style="padding:8px;text-align:center;border-bottom:2px solid #e8e8e8">状态</th>
        </tr>
"""

    for p in rankings:
        # 行背景色
        if p.get("protected"):
            bg = "#FFFBE6"  # 受保护：浅黄
        elif p["ranking_score"] < 35:
            bg = "#FFF1F0"  # 建议淘汰：浅红
        elif p["ranking_score"] < 45:
            bg = "#FFF7E6"  # 关注：浅橙
        else:
            bg = "white"

        # 状态标签
        if p.get("protected"):
            status = f'<span style="color:#D48806">🛡️ {p["protect_reason"]}</span>'
        elif p["ranking_score"] < 35:
            status = '<span style="color:#CF1322;font-weight:bold">🔴 建议淘汰</span>'
        elif p["ranking_score"] < 45:
            status = '<span style="color:#D46B08">🟡 关注</span>'
        elif p["rank"] <= 3:
            status = '<span style="color:#389E0D">🟢 核心</span>'
        else:
            status = '<span style="color:#1890FF">🔵 正常</span>'

        # 盈亏颜色
        pnl_color = "#CF1322" if p["pnl_pct"] < 0 else "#389E0D"

        html += f"""
        <tr style="background:{bg}">
            <td style="padding:8px;text-align:center;border-bottom:1px solid #f0f0f0;font-weight:bold">#{p['rank']}</td>
            <td style="padding:8px;text-align:left;border-bottom:1px solid #f0f0f0">
                <b>{p['name']}</b>({p['code']})<br>
                <span style="font-size:10px;color:#888">{p['sector']} | {p['trend']}</span>
            </td>
            <td style="padding:8px;text-align:center;border-bottom:1px solid #f0f0f0;font-weight:bold;font-size:14px">{p['ranking_score']:.0f}</td>
            <td style="padding:8px;text-align:center;border-bottom:1px solid #f0f0f0">{p['tech_score']:.0f}</td>
            <td style="padding:8px;text-align:center;border-bottom:1px solid #f0f0f0">{p['pnl_rank_pct']:.0f}%</td>
            <td style="padding:8px;text-align:center;border-bottom:1px solid #f0f0f0">{p['trend_strength']:.0f}</td>
            <td style="padding:8px;text-align:center;border-bottom:1px solid #f0f0f0">{p['capital_efficiency']:.0f}</td>
            <td style="padding:8px;text-align:center;border-bottom:1px solid #f0f0f0;color:{pnl_color}">{p['pnl_pct']:+.1f}%</td>
            <td style="padding:8px;text-align:center;border-bottom:1px solid #f0f0f0">{p['position_ratio']:.1f}%</td>
            <td style="padding:8px;text-align:center;border-bottom:1px solid #f0f0f0">{status}</td>
        </tr>
"""

    html += """
    </table>
</div>

<div style="background:#E6F7FF;border:1px solid #91D5FF;border-radius:8px;padding:12px;margin-top:15px;font-size:12px">
    <div style="font-weight:bold;color:#096DD9;margin-bottom:6px">📊 评分维度说明</div>
    <div style="color:#333;line-height:1.8">
        <b>综合评分</b> = 技术评分×40% + 盈亏排名×30% + 趋势强度×20% + 资金效率×10%<br>
        <b>淘汰规则</b>: 持仓≥{trigger}只时触发 | 综合评分&lt;{threshold}标记淘汰 | 买入≤{protect_days}天/浮盈&gt;{protect_pct}%/唯一赛道标的受保护<br>
        <b>健康度</b>: 全部持仓平均综合评分 - 超限惩罚(每超1只扣5分)
    </div>
</div>
""".format(
        trigger=summary["min_holdings_to_trigger"],
        threshold=_get_elimination_config()["eliminate_score_threshold"],
        protect_days=_get_elimination_config()["protect_recent_buy_days"],
        protect_pct=_get_elimination_config()["protect_profit_above_pct"],
    )

    return html
