# -*- coding: utf-8 -*-
"""
资金仓位智能规划模块（综合分析报告板块） V1.0
=================================================
独立模块方案（后处理增强，不修改任何既有模块的返回结构）:
  将分散在 config(资金参数) / risk.position_sizing(仓位引擎) /
  strategy.portfolio_analyzer(持仓优化) / data.buy_alert_levels(当日买点)
  中的既有计算串联为一个报告板块，输出:

  一、资金仪表盘      总资金/可用资金/持仓市值/仓位占比/现金比例达标判定
  二、全局禁买结论    仓位≥90%满仓线 或 可用现金<5% 时红色醒目横幅
  三、持仓集中度      单票超限清单(股数级减仓建议)/赛道Top3/个股HHI
  四、调仓操作清单    复用 portfolio_analyzer.generate_optimization 的
                      止损/减仓/时间止损 actions(含股数+回笼金额)
  五、买点资金可行性  逐条校验当日 buy_alert_levels:
                      可买/资金不足/仓位已满/已持仓禁加/最小单位超限/冷却作废
  六、仓位引擎建议    PositionSizer.calc_positions 对可买标的的股数建议

安全约束:
  - build_capital_plan_section() 是唯一入口，任何异常都返回降级HTML，绝不抛出。
  - 不写任何落盘文件，纯只读计算。
"""

import os
import json
import logging
import datetime

logger = logging.getLogger(__name__)

# 与 generate_holdings_report.py 的 _ETF_PREFIXES 口径保持一致
_ETF_PREFIXES = ("159", "510", "511", "512", "513", "515", "516", "518", "560", "562", "588")

_RED = "#cf1322"
_ORANGE = "#bc4c00"
_YELLOW = "#ad6800"
_GREEN = "#389e0d"
_BLUE = "#0969da"
_GRAY = "#57606a"


def _fmt(v, pattern="{:,.0f}", na="N/A"):
    try:
        if v is None:
            return na
        return pattern.format(float(v))
    except (TypeError, ValueError):
        return na


def _cell(text, bold=False, color=None):
    style = "padding:6px 10px;border-bottom:1px solid #e5e7eb;"
    if bold:
        style += "font-weight:bold;"
    if color:
        style += f"color:{color};"
    return f"<td style='{style}'>{text}</td>"


def _load_buy_levels():
    """读取当日买点清单（valid_date 必须等于今日），过期/异常返回 None"""
    try:
        import config
        path = os.path.join(getattr(config, "DATA_DIR", "data"), "buy_alert_levels.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        today = datetime.date.today().isoformat()
        if data.get("valid_date") != today:
            return None
        return data
    except Exception:
        return None


def _load_buy_block_codes():
    """读取禁加仓冷却清单（文件可能不存在，返回代码集合）"""
    try:
        import config
        path = os.path.join(getattr(config, "DATA_DIR", "data"), "buy_block.json")
        if not os.path.exists(path):
            return set()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        today = datetime.date.today().isoformat()
        codes = set()
        # FIX(2026-08-14): 写侧 add_buy_block 落盘结构为 {"blocked": {code: {...}}}，
        # 原遍历顶层键只能取到 "blocked" 字符串键，真实冷却标的永远无法识别
        for code, info in ((data or {}).get("blocked") or {}).items():
            if not isinstance(info, dict):
                continue
            until = str(info.get("until", info.get("expire", "")))
            if not until or until >= today:  # 无期限或尚未过期
                codes.add(code)
        return codes
    except Exception:
        return set()


# ============================================================
# 一、资金仪表盘计算
# ============================================================

def compute_capital_dashboard(holdings_raw: dict) -> dict:
    """
    计算资金状态仪表盘（全部取自 config 券商口径参数，纯计算无I/O除持仓外）

    参数:
        holdings_raw: config.load_holdings() 返回的原始持仓
    返回:
        {total_capital, available_cash, mv, position_ratio, cash_ratio,
         reserve_ratio, reserve_met, holding_count, max_holdings,
         full_ban, near_full, ban_reasons}
    """
    import config
    total = getattr(config, "TOTAL_CAPITAL", 0) or 0
    avail = getattr(config, "AVAILABLE_CASH", 0) or 0
    reserve_ratio = getattr(config, "CASH_RESERVE_RATIO", 0.10)
    full_th = getattr(config, "FULL_POSITION_THRESHOLD", 0.90)
    near_th = getattr(config, "NEAR_FULL_POSITION", 0.80)
    max_holdings = getattr(config, "MAX_HOLDINGS", 7)

    mv = 0.0
    count = 0
    for info in (holdings_raw or {}).values():
        if not isinstance(info, dict):
            continue
        shares = info.get("shares", 0)
        price = info.get("current_price", 0) or info.get("buy_price", 0)
        if shares and shares > 0 and price and price > 0:
            mv += shares * price
            count += 1

    position_ratio = mv / total if total > 0 else 0
    cash_ratio = avail / total if total > 0 else 0

    ban_reasons = []
    if position_ratio >= full_th:
        ban_reasons.append(
            f"仓位{position_ratio*100:.1f}% ≥ 满仓禁买线{full_th*100:.0f}%（config.FULL_POSITION_THRESHOLD）")
    if cash_ratio < 0.05:
        ban_reasons.append(
            f"可用现金仅{_fmt(avail)}元（{cash_ratio*100:.2f}%），低于5%熔断线")
    if avail < total * reserve_ratio:
        ban_reasons.append(
            f"可用现金低于10%保留底线{_fmt(total * reserve_ratio)}元（config.CASH_RESERVE_RATIO）")

    return {
        "total_capital": total,
        "available_cash": avail,
        "mv": mv,
        "position_ratio": position_ratio,
        "cash_ratio": cash_ratio,
        "reserve_ratio": reserve_ratio,
        "reserve_met": avail >= total * reserve_ratio,
        "holding_count": count,
        "max_holdings": max_holdings,
        "full_ban": position_ratio >= full_th or cash_ratio < 0.05,
        "near_full": position_ratio >= near_th,
        "ban_reasons": ban_reasons,
    }


# ============================================================
# 二、持仓集中度（单票超限/赛道/HHI）
# ============================================================

def compute_concentration(holdings_raw: dict, dashboard: dict) -> dict:
    """
    单票超限清单（含股数级减仓建议）、赛道Top3、个股HHI
    单票上限口径与总览表一致: ETF MAX_SINGLE_ETF_RATIO / 股票 MAX_SINGLE_STOCK_RATIO
    赛道字段缺失时降级用板块轮动模块的粗粒度映射(get_coarse_sector_map)，避免"其他"假集中
    """
    import config
    sector_map = {}
    try:
        from strategy.sector_divergence import get_coarse_sector_map
        sector_map = get_coarse_sector_map() or {}
    except Exception:
        sector_map = {}
    total = dashboard["total_capital"]
    stock_cap = getattr(config, "MAX_SINGLE_STOCK_RATIO", 0.15)
    etf_cap = getattr(config, "MAX_SINGLE_ETF_RATIO", 0.20)

    over = []
    sectors = {}
    weights = []
    for code, info in (holdings_raw or {}).items():
        if not isinstance(info, dict):
            continue
        shares = info.get("shares", 0) or 0
        price = info.get("current_price", 0) or 0
        if shares <= 0 or price <= 0 or total <= 0:
            continue
        mv = shares * price
        ratio = mv / total
        weights.append(ratio)
        sector = info.get("sector") or ""
        if not sector:
            # holdings.sector 缺失（券商同步未回填）→ 依次用配置池按代码查/粗粒度映射兑底
            try:
                sector = (config.get_stock_info(code) or {}).get("赛道", "") or ""
            except Exception:
                sector = ""
        if not sector:
            sector = sector_map.get(code, "其他")
        sector = sector_map.get(sector, sector)  # 粗粒度归一化，与总览表同源
        sectors[sector] = sectors.get(sector, 0) + mv

        cap = etf_cap if code.startswith(_ETF_PREFIXES) else stock_cap
        if ratio > cap:
            target_shares = int(total * cap / price / 100) * 100
            sell_shares = max(0, ((shares - target_shares) // 100) * 100)
            if sell_shares >= 100:
                over.append({
                    "code": code,
                    "name": info.get("name", code),
                    "ratio": ratio,
                    "cap": cap,
                    "sell_shares": sell_shares,
                    "amount": round(sell_shares * price, 0),
                })
    over.sort(key=lambda x: -(x["ratio"] - x["cap"]))

    top3 = sorted(sectors.items(), key=lambda x: -x[1])[:3]
    mv_total = dashboard["mv"]
    top3_pct = sum(v for _, v in top3) / mv_total if mv_total > 0 else 0
    hhi = sum(w * w for w in weights)

    return {
        "over_limit": over,
        "top3": [(k, v, v / mv_total if mv_total > 0 else 0) for k, v in top3],
        "top3_pct": top3_pct,
        "hhi": hhi,
    }


# ============================================================
# 三、调仓操作清单（复用 portfolio_analyzer 优化引擎）
# ============================================================

def compute_rebalance_actions(holdings_raw: dict) -> dict:
    """
    调用 strategy.portfolio_analyzer.analyze_portfolio 生成
    止损/减仓/时间止损 actions（含股数+回笼金额），失败返回空结构
    """
    try:
        from strategy.portfolio_analyzer import analyze_portfolio
        result = analyze_portfolio(holdings_raw) or {}
        optimization = result.get("optimization") or {}
        return {
            "actions": optimization.get("actions", []) or [],
            "total_recover": optimization.get("total_recover", 0),
            "new_cash": optimization.get("new_cash", 0),
            "new_cash_ratio": optimization.get("new_cash_ratio", 0),
            "risk_alerts": result.get("risk_alerts", []) or [],
        }
    except Exception as e:
        logger.warning(f"[资金规划] portfolio_analyzer 调用失败: {e}")
        return {"actions": [], "total_recover": 0, "new_cash": 0,
                "new_cash_ratio": 0, "risk_alerts": []}


# ============================================================
# 四、买点资金可行性校验
# ============================================================

def check_buy_feasibility(levels: list, dashboard: dict,
                          holdings_raw: dict, blocked_codes: set) -> list:
    """
    逐条校验当日买点的可执行性，返回带 verdict/status 的列表。
    判定顺序（先全局后个体）:
      仓位已满(禁买) → 冷却期 → 已持仓禁当日再加 → 最小单位超单票上限
      → 持仓数超限需先腾名额 → 可用资金不足 → 可小仓位试错 → 可买
    """
    import config
    total = dashboard["total_capital"]
    avail = dashboard["available_cash"]
    stock_cap = getattr(config, "MAX_SINGLE_STOCK_RATIO", 0.15)
    etf_cap = getattr(config, "MAX_SINGLE_ETF_RATIO", 0.20)

    rows = []
    remaining = avail  # 逐条扣减，模拟多买点同时执行的资金消耗
    for lv in levels or []:
        if not isinstance(lv, dict):
            continue
        code = str(lv.get("code", ""))
        name = lv.get("name", code)
        base = lv.get("base_price", 0) or lv.get("moderate_buy", 0) or 0
        amount = lv.get("first_amount", 0) or 0
        shares = lv.get("first_shares", 0) or 0
        stop = lv.get("stop_loss", 0) or 0
        if base <= 0 or shares <= 0:
            continue
        cap = etf_cap if code.startswith(_ETF_PREFIXES) else stock_cap
        cap_amount = total * cap

        verdict, status, note = "⛔禁止买入", "ban", ""
        if dashboard["full_ban"]:
            note = "仓位已满/现金熔断：先执行调仓清单回笼现金，今日不得新开仓"
        elif code in blocked_codes:
            note = "标的在禁加仓冷却清单(buy_block)内，冷却期未过"
        elif code in holdings_raw:
            verdict, status = "⛔已持仓·禁加仓", "held"
            note = "指令卡约束：信号触发当日禁止对已持仓标的再加仓"
        elif amount > cap_amount:
            verdict, status = "⛔最小单位超限", "unit"
            gap = amount - cap_amount
            note = (f"1手金额{_fmt(amount)}元 > 单票上限{cap*100:.0f}%"
                    f"({_fmt(cap_amount)}元)，缺口{_fmt(gap)}元，A股无法拆小")
        elif dashboard["holding_count"] >= dashboard["max_holdings"]:
            verdict, status = "⛔仓位已满·先腾名额", "limit"
            note = (f"持仓{dashboard['holding_count']}只 ≥ 上限"
                    f"{dashboard['max_holdings']}只，须先卖出低分持仓腾出名额")
        elif remaining < amount:
            if remaining >= base * 100:
                affordable = int(remaining / base / 100) * 100
                verdict, status = "⚠️可小仓位试错", "trial"
                note = (f"资金不足：建议{_fmt(amount)}元，仅余{_fmt(remaining)}元，"
                        f"可买{affordable}股({_fmt(affordable * base)}元)")
                remaining -= affordable * base
            else:
                verdict, status = "⛔资金不足", "cash"
                note = f"需{_fmt(amount)}元，剩余可用仅{_fmt(remaining)}元"
        else:
            verdict, status = "✅可买", "ok"
            note = f"建议{shares}股/{_fmt(amount)}元，止损{stop:.2f}"
            remaining -= amount

        rows.append({
            "code": code, "name": name, "price": base,
            "shares": shares, "amount": amount, "stop": stop,
            "verdict": verdict, "status": status, "note": note,
        })
    return rows


def compute_sizer_suggestions(feas_rows: list, dashboard: dict) -> list:
    """
    对"可买/可小仓位试错"的买点调用 PositionSizer.calc_positions
    给出风险预算/半凯利口径的建议股数（失败返回空列表）
    """
    try:
        from risk.position_sizing import PositionSizer
        candidates = []
        for r in feas_rows:
            if r["status"] not in ("ok", "trial") or r["price"] <= 0:
                continue
            candidates.append({
                "code": r["code"], "name": r["name"],
                "close": r["price"],
                "support_price": r["stop"] if r["stop"] > 0 else r["price"] * 0.95,
                "trend_level": 4,       # 已通过选股五层筛选，按震荡以上对待
                "atr": r["price"] * 0.02,
                "risk_reward": {"risk_reward_1": 1.5},
            })
        if not candidates:
            return []
        sizer = PositionSizer(total_capital=dashboard["total_capital"],
                              available_cash=dashboard["available_cash"])
        plan = sizer.calc_positions(candidates, holdings={})
        return plan.get("positions", []) or []
    except Exception as e:
        logger.warning(f"[资金规划] PositionSizer 调用失败: {e}")
        return []


# ============================================================
# 五、HTML 渲染
# ============================================================

def _render_dashboard(d: dict) -> str:
    ok = (f"<span style='color:{_GREEN};font-weight:bold'>✔ 达标</span>",
          f"<span style='color:{_RED};font-weight:bold'>✘ 未达标</span>")
    reserve_html = ok[0] if d["reserve_met"] else ok[1]
    count_color = _RED if d["holding_count"] > d["max_holdings"] else _GRAY
    cards = (
        f"<b>总资金</b> {_fmt(d['total_capital'])}元　｜　"
        f"<b>可用资金</b> <span style='color:{_RED if d['cash_ratio'] < 0.05 else _GREEN};"
        f"font-weight:bold'>{_fmt(d['available_cash'])}元({d['cash_ratio']*100:.2f}%)</span>　｜　"
        f"<b>持仓市值</b> {_fmt(d['mv'])}元　｜　"
        f"<b>总仓位</b> <span style='color:{_RED if d['position_ratio'] >= 0.9 else _GRAY};"
        f"font-weight:bold'>{d['position_ratio']*100:.1f}%</span>"
        f"（禁买线{d.get('full_th_pct', 90)}%）　｜　"
        f"<b>现金≥10%底线</b> {reserve_html}　｜　"
        f"<b>持仓数</b> <span style='color:{count_color};font-weight:bold'>"
        f"{d['holding_count']}只</span>（上限{d['max_holdings']}只）"
    )
    return ("<div style='border:1px solid #d0d7de;border-radius:6px;padding:12px 16px;"
            "margin:12px 0;background:#f6f8fa;font-size:13px;line-height:1.9;'>" +
            cards + "</div>")


def _render_ban_banner(d: dict) -> str:
    if not d["full_ban"]:
        if d["near_full"]:
            return ("<div style='border-left:4px solid %s;background:#fff8e6;"
                    "padding:10px 14px;margin:10px 0;font-size:13px;'>"
                    "⚠️ <b>仓位已达%.1f%%（≥80%%近满仓线）</b>：只允许减仓，不建议新开仓。"
                    "</div>" % (_ORANGE, d["position_ratio"] * 100))
        return ""
    items = "".join(f"<li style='margin:3px 0;'>{r}</li>" for r in d["ban_reasons"])
    return ("<div style='border:3px solid %s;background:#fff1f0;border-radius:6px;"
            "padding:14px 18px;margin:12px 0;'>"
            "<div style='color:%s;font-size:18px;font-weight:bold;margin-bottom:8px;'>"
            "⛔ 禁止买入 —— 今日所有买点均不得执行</div>"
            "<ul style='margin:4px 0;padding-left:20px;font-size:13px;color:#24292f;'>%s</ul>"
            "<div style='font-size:13px;margin-top:6px;color:#24292f;'>"
            "正确动作：按下方调仓清单<b>先卖出回笼现金</b>，待仓位&lt;90%%且现金≥10%%后再谈买入。"
            "</div></div>" % (_RED, _RED, items))


def _render_concentration(c: dict) -> str:
    parts = ["<div style='margin:10px 0;'>"
             "<div style='font-weight:bold;font-size:14px;margin-bottom:6px;'>① 持仓集中度</div>"]
    if c["over_limit"]:
        rows = []
        for o in c["over_limit"]:
            rows.append(
                "<tr>"
                + _cell(o["code"])
                + _cell(o["name"], bold=True)
                + _cell(f"{o['ratio']*100:.1f}%", bold=True, color=_RED)
                + _cell(f"{o['cap']*100:.0f}%")
                + _cell(f"减仓{o['sell_shares']}股（约{_fmt(o['amount'])}元）", color=_RED)
                + "</tr>")
        parts.append(
            "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
            "<tr style='background:#f6f8fa;'>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>代码</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>名称</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>当前仓位</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>上限</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>处置建议</th>"
            "</tr>" + "".join(rows) + "</table>")
    else:
        parts.append(f"<div style='color:{_GREEN};font-size:13px;'>✔ 无单票超限</div>")

    top3_str = "、".join(f"{k} {p*100:.0f}%" for k, _, p in c["top3"]) or "-"
    top3_color = _RED if c["top3_pct"] > 0.60 else (_YELLOW if c["top3_pct"] > 0.45 else _GREEN)
    hhi_color = _RED if c["hhi"] > 0.25 else (_YELLOW if c["hhi"] > 0.15 else _GREEN)
    parts.append(
        f"<p style='font-size:12px;color:#24292f;margin:6px 0;'>"
        f"🧺 赛道前三: {top3_str}，合计 <span style='color:{top3_color};font-weight:bold'>"
        f"{c['top3_pct']*100:.0f}%</span>（建议≤60%）　|　"
        f"个股HHI <span style='color:{hhi_color};font-weight:bold'>{c['hhi']:.3f}</span>"
        f"（&gt;0.25 集中度偏高）</p>")
    parts.append("</div>")
    return "".join(parts)


def _render_actions(a: dict) -> str:
    parts = ["<div style='margin:10px 0;'>"
             "<div style='font-weight:bold;font-size:14px;margin-bottom:6px;'>"
             "② 调仓操作清单（止损/减仓/时间止损，含股数与回笼金额）</div>"]
    if not a["actions"]:
        parts.append(f"<div style='color:{_GREEN};font-size:13px;'>✔ 无需强制调仓</div></div>")
        return "".join(parts)
    action_colors = {"止损卖出": _RED, "减仓": _ORANGE, "时间止损清仓": _RED}
    rows = []
    for act in a["actions"]:
        if not isinstance(act, dict):
            continue
        kind = act.get("action", "")
        color = action_colors.get(kind, _GRAY)
        rows.append(
            "<tr>"
            + _cell(f"<span style='color:#fff;background:{color};padding:2px 8px;"
                    f"border-radius:3px;font-size:12px;'>{kind}</span>")
            + _cell(act.get("code", ""))
            + _cell(act.get("name", ""), bold=True)
            + _cell(_fmt(act.get("shares")))
            + _cell(_fmt(act.get("recover_amount")) + "元")
            + _cell(act.get("detail", "") + "｜" + act.get("reason", ""))
            + "</tr>")
    parts.append(
        "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
        "<tr style='background:#f6f8fa;'>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>动作</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>代码</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>名称</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>股数</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>回笼金额</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>原因</th>"
        "</tr>" + "".join(rows) + "</table>"
        f"<div style='color:#24292f;font-size:12px;margin-top:4px;background:#f0f9f4;"
        f"border-radius:4px;padding:6px 10px;display:inline-block;'>"
        f"执行后预计回笼 <b>{_fmt(a['total_recover'])}元</b>，现金升至 "
        f"<b>{_fmt(a['new_cash'])}元（{a['new_cash_ratio']:.1f}%）</b></div>")
    parts.append("</div>")
    return "".join(parts)


def _render_feasibility(rows: list) -> str:
    parts = ["<div style='margin:10px 0;'>"
             "<div style='font-weight:bold;font-size:14px;margin-bottom:6px;'>"
             "③ 今日买点资金可行性校验（buy_alert_levels）</div>"]
    if not rows:
        parts.append("<div style='color:%s;font-size:13px;'>今日无有效买点清单"
                     "（未生成或已过期）</div></div>" % _GRAY)
        return "".join(parts)
    status_colors = {"ok": _GREEN, "trial": _YELLOW, "cash": _RED, "limit": _RED,
                     "held": _ORANGE, "unit": _RED, "ban": _RED}
    trs = []
    for r in rows:
        color = status_colors.get(r["status"], _GRAY)
        trs.append(
            "<tr>"
            + _cell(f"<span style='color:#fff;background:{color};padding:2px 8px;"
                    f"border-radius:3px;font-size:12px;white-space:nowrap;'>{r['verdict']}</span>")
            + _cell(r["code"])
            + _cell(r["name"], bold=True)
            + _cell(_fmt(r["price"], "{:.2f}"))
            + _cell(_fmt(r["amount"]) + "元")
            + _cell(_fmt(r["stop"], "{:.2f}"))
            + _cell(r["note"])
            + "</tr>")
    parts.append(
        "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
        "<tr style='background:#f6f8fa;'>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>结论</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>代码</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>名称</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>基准价</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>首仓金额</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>止损</th>"
        "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>判定依据</th>"
        "</tr>" + "".join(trs) + "</table>")
    parts.append("</div>")
    return "".join(parts)


def _render_sizer(sizer_positions: list) -> str:
    if not sizer_positions:
        return ""
    rows = []
    for p in sizer_positions:
        if not isinstance(p, dict) or not p.get("suggested_shares"):
            continue
        rows.append(
            "<tr>"
            + _cell(p.get("code", ""))
            + _cell(p.get("name", ""), bold=True)
            + _cell(_fmt(p.get("suggested_shares")))
            + _cell(_fmt(p.get("suggested_amount")) + "元")
            + _cell(f"{p.get('position_pct', 0):.1f}%")
            + _cell(_fmt(p.get("stop_loss"), "{:.2f}"))
            + "</tr>")
    if not rows:
        return ""
    return ("<div style='margin:10px 0;'>"
            "<div style='font-weight:bold;font-size:14px;margin-bottom:6px;'>"
            "④ 仓位引擎建议（PositionSizer 风险预算/半凯利口径）</div>"
            "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
            "<tr style='background:#f6f8fa;'>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>代码</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>名称</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>建议股数</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>金额</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>占比</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>止损</th>"
            "</tr>" + "".join(rows) + "</table>"
            "<p style='color:%s;font-size:11px;margin:4px 0'>ℹ️ 仅对判定为"
            "「可买/可小仓位试错」的标的计算；单笔风险预算2%%本金、半凯利钳位。</p></div>" % _GRAY)


# ============================================================
# 唯一入口
# ============================================================

def build_capital_plan_section(portfolio_risk_report: dict = None) -> str:
    """
    生成"资金仓位智能规划"报告板块HTML。

    参数:
        portfolio_risk_report: 批2-S6 已计算的 full_risk_report()（可选，
            用于在禁买横幅中补充组合风险评分联动说明）
    返回: HTML字符串；任何异常返回降级提示，绝不抛出。
    """
    try:
        import config
        holdings_raw = config.load_holdings()
        dashboard = compute_capital_dashboard(holdings_raw)
        dashboard["full_th_pct"] = int(getattr(config, "FULL_POSITION_THRESHOLD", 0.90) * 100)

        parts = ["<h2>💰 资金仓位智能规划</h2>",
                 _render_dashboard(dashboard)]

        # 禁买横幅 + 风控评分联动说明
        banner = _render_ban_banner(dashboard)
        if banner and isinstance(portfolio_risk_report, dict):
            score = portfolio_risk_report.get("risk_score")
            level = portfolio_risk_report.get("overall_level", "")
            if score is not None:
                banner += ("<div style='color:%s;font-size:12px;margin-top:-6px;"
                           "margin-bottom:10px;'>联动: 组合风险评分 %.1f/100（%s），"
                           "高风险时全局仓位应按「组合风险行动清单」缩放建议执行。</div>"
                           % (_GRAY, float(score), level))
        parts.append(banner)

        # ① 集中度
        try:
            parts.append(_render_concentration(compute_concentration(holdings_raw, dashboard)))
        except Exception as _ce:
            logger.warning(f"[资金规划] 集中度渲染失败: {_ce}")

        # ② 调仓操作清单
        actions = compute_rebalance_actions(holdings_raw)
        parts.append(_render_actions(actions))

        # ③ 买点资金可行性 + ④ 仓位引擎建议
        levels_data = _load_buy_levels()
        levels = levels_data.get("levels", []) if isinstance(levels_data, dict) else []
        feas_rows = check_buy_feasibility(
            levels, dashboard, holdings_raw, _load_buy_block_codes())
        parts.append(_render_feasibility(feas_rows))
        parts.append(_render_sizer(compute_sizer_suggestions(feas_rows, dashboard)))

        parts.append(
            f"<p style='color:{_GRAY};font-size:11px;margin:4px 0'>"
            "ℹ️ 资金口径为券商同步值(config.TOTAL_CAPITAL/AVAILABLE_CASH)，"
            "买点清单取当日 buy_alert_levels.json；本板块为只读计算，"
            "不替代盘中买点到价提醒的实时闸门。</p>")
        return "".join(p for p in parts if p)
    except Exception as e:
        logger.warning(f"[资金规划] 板块生成失败: {e}")
        return ("<div style='border:1px solid #d0d7de;border-radius:6px;padding:12px 16px;"
                "margin:12px 0;background:#f6f8fa;'>"
                "<div style='font-weight:bold;font-size:15px;'>💰 资金仓位智能规划</div>"
                f"<div style='color:#57606a;font-size:13px;'>数据不足（{e}）</div></div>")
