# -*- coding: utf-8 -*-
"""
交易行为自诊断模块
==================
针对当日成交明细做"过度操作"诊断，量化频繁交易浪费的成本，
供综合分析报告顶部显著警示横幅使用。

诊断维度:
  1. 交易频率     - 成交笔数/涉及标的数/换手率（成交额÷总资金）
  2. 追高成本     - 同一标的在更低买价已出现后，仍以更高价继续买入的额外支出
  3. 杀低损失     - 同一标的在更高卖价已出现后，仍以更低价继续卖出的少收金额
  4. 日内回转     - 先卖后买回（回补溢价）/ 先买后卖（日内T亏损），FIFO匹配
  5. 闪电翻转     - 买入后30分钟内即卖出且亏损的笔数与金额
  6. 交易费用     - 佣金(万2.5/笔最低5元)+印花税(卖出股票万5)+过户费(万0.1双边)

"如果不操作能省多少钱" = 交易费用 + 追高成本 + 杀低损失 + 日内回转净亏损

输入格式(trades列表元素):
  {"time": "HH:MM:SS", "code": "000858", "name": "五粮液",
   "direction": "买入"|"卖出"(或"buy"/"sell"), "qty": int, "price": float, "amount": float}
"""

import os
import json
import datetime
from collections import deque

# ---- 费用参数（可按券商实际调整） ----
COMMISSION_RATE = 0.00025    # 佣金 万2.5
COMMISSION_MIN = 5.0         # 单笔最低佣金
STAMP_TAX_RATE = 0.0005      # 印花税 万5（仅卖出股票，ETF免征）
TRANSFER_FEE_RATE = 0.00001  # 过户费 万0.1（双边）

# ---- 行为阈值 ----
FREQUENCY_SEVERE = 20        # 成交笔数≥20 → 严重
FREQUENCY_WARNING = 10       # 成交笔数≥10 → 警示
TURNOVER_SEVERE = 1.0        # 换手率≥100% → 严重
TURNOVER_WARNING = 0.5       # 换手率≥50% → 警示
PRICE_NOISE = 0.002          # 价差<0.2%视为正常噪音
QUICK_FLIP_MINUTES = 30      # 闪电翻转判定窗口


def _is_etf(code: str) -> bool:
    """ETF/LOF判定（免印花税品种）"""
    code = str(code)
    return code.startswith(('159', '16', '51', '56', '58', '501', '513', '518'))


def _parse_time(t: str):
    """HH:MM:SS → datetime.time，失败返回 None"""
    try:
        parts = str(t).replace('：', ':').split(':')
        return datetime.time(int(parts[0]), int(parts[1]),
                             int(parts[2]) if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return None


def _minutes_between(t1, t2) -> float:
    """两个 time 的分钟差（t2 - t1）"""
    if t1 is None or t2 is None:
        return 1e9
    s1 = t1.hour * 3600 + t1.minute * 60 + t1.second
    s2 = t2.hour * 3600 + t2.minute * 60 + t2.second
    return (s2 - s1) / 60.0


def estimate_commission(trades: list) -> dict:
    """估算当日交易费用"""
    commission = 0.0
    stamp = 0.0
    transfer = 0.0
    for t in trades:
        amount = float(t.get("amount", 0) or 0)
        is_sell = _is_sell(t)
        commission += max(COMMISSION_MIN, amount * COMMISSION_RATE)
        transfer += amount * TRANSFER_FEE_RATE
        if is_sell and not _is_etf(t.get("code", "")):
            stamp += amount * STAMP_TAX_RATE
    total = commission + stamp + transfer
    return {"commission": round(commission, 2), "stamp": round(stamp, 2),
            "transfer": round(transfer, 2), "total": round(total, 2)}


def _is_sell(t: dict) -> bool:
    d = str(t.get("direction", "")).strip()
    return d in ("卖出", "sell", "证券卖出", "S")


def analyze_trade_behavior(trades: list, total_capital: float = 1_000_000,
                           date: str = "") -> dict:
    """
    核心诊断函数（纯函数，可单测）

    参数:
        trades: 当日成交明细列表
        total_capital: 账户总资金（用于换手率）
        date: 成交日期字符串

    返回: 诊断结果dict（含 total_waste / severity / issues 等）
    """
    trades = [t for t in trades if t.get("qty", 0) and t.get("price", 0)]
    if not trades:
        return {"total_trades": 0, "severity": "ok", "total_waste": 0,
                "issues": [], "headline": "今日无成交"}

    # 按标的分组并按时间排序
    by_code = {}
    for t in trades:
        code = str(t.get("code", ""))
        item = {
            "time": _parse_time(t.get("time", "")),
            "name": t.get("name", code),
            "sell": _is_sell(t),
            "qty": int(t["qty"]),
            "price": float(t["price"]),
            "amount": float(t.get("amount") or t["qty"] * t["price"]),
        }
        by_code.setdefault(code, []).append(item)
    for items in by_code.values():
        items.sort(key=lambda x: (x["time"] or datetime.time(0, 0)))

    buy_amount = sum(t["amount"] for items in by_code.values() for t in items if not t["sell"])
    sell_amount = sum(t["amount"] for items in by_code.values() for t in items if t["sell"])
    turnover = (buy_amount + sell_amount) / total_capital if total_capital > 0 else 0

    issues = []           # 每只标的的问题明细
    chase_cost = 0.0      # 追高成本
    panic_cost = 0.0      # 杀低损失
    roundtrip_loss = 0.0  # 日内回转净亏损（正数=亏损）
    quick_flip_count = 0
    quick_flip_loss = 0.0

    for code, items in sorted(by_code.items()):
        name = items[0]["name"]
        c_chase = c_panic = c_round = 0.0
        c_flip_n = 0
        c_flip_loss = 0.0

        # ---- 追高/杀低（滚动极值法，只惩罚更低买/更高卖已出现后的反向操作）----
        run_min_buy = None
        run_max_sell = None
        for it in items:
            if not it["sell"]:
                if run_min_buy is not None and it["price"] > run_min_buy * (1 + PRICE_NOISE):
                    c_chase += (it["price"] - run_min_buy) * it["qty"]
                run_min_buy = it["price"] if run_min_buy is None else min(run_min_buy, it["price"])
            else:
                if run_max_sell is not None and it["price"] < run_max_sell * (1 - PRICE_NOISE):
                    c_panic += (run_max_sell - it["price"]) * it["qty"]
                run_max_sell = it["price"] if run_max_sell is None else max(run_max_sell, it["price"])

        # ---- 日内回转（FIFO匹配: 先卖后买=回补溢价, 先买后卖=日内T盈亏）----
        open_sells = deque()   # 未回补的卖出 (price, qty, time)
        open_buys = deque()    # 未平掉的买入 (price, qty, time)
        for it in items:
            if it["sell"]:
                remain = it["qty"]
                while remain > 0 and open_buys:
                    bp, bq, bt = open_buys[0]
                    m = min(remain, bq)
                    pnl = (it["price"] - bp) * m
                    if pnl < 0:
                        c_round += -pnl
                        if _minutes_between(bt, it["time"]) <= QUICK_FLIP_MINUTES:
                            c_flip_n += 1
                            c_flip_loss += -pnl
                    else:
                        c_round -= pnl  # 日内T盈利抵减
                    remain -= m
                    if m >= bq:
                        open_buys.popleft()
                    else:
                        open_buys[0] = (bp, bq - m, bt)
                if remain > 0:
                    open_sells.append((it["price"], remain, it["time"]))
            else:
                remain = it["qty"]
                while remain > 0 and open_sells:
                    sp, sq, st = open_sells[0]
                    m = min(remain, sq)
                    cost = (it["price"] - sp) * m
                    c_round += cost  # 正=溢价买回(亏损)，负=高卖低接(盈利)
                    remain -= m
                    if m >= sq:
                        open_sells.popleft()
                    else:
                        open_sells[0] = (sp, sq - m, st)
                if remain > 0:
                    open_buys.append((it["price"], remain, it["time"]))

        chase_cost += c_chase
        panic_cost += c_panic
        roundtrip_loss += c_round
        quick_flip_count += c_flip_n
        quick_flip_loss += c_flip_loss

        # 该标的的问题描述
        probs = []
        if c_chase > 1:
            probs.append(f"追高买入多付{c_chase:.0f}元")
        if c_panic > 1:
            probs.append(f"越卖越低少收{c_panic:.0f}元")
        if c_round > 1:
            probs.append(f"日内回转亏损{c_round:.0f}元")
        if c_flip_n > 0:
            probs.append(f"闪电翻转{c_flip_n}笔亏{c_flip_loss:.0f}元")
        if probs:
            issues.append({"code": code, "name": name,
                           "cost": round(c_chase + c_panic + max(c_round, 0), 2),
                           "desc": "；".join(probs)})

    fees = estimate_commission(trades)
    roundtrip_loss = max(roundtrip_loss, 0.0)  # 净盈利不算浪费
    total_waste = fees["total"] + chase_cost + panic_cost + roundtrip_loss

    # ---- 评级 ----
    n = len(trades)
    score = 0
    if n >= FREQUENCY_SEVERE or turnover >= TURNOVER_SEVERE:
        score = 2
    elif n >= FREQUENCY_WARNING or turnover >= TURNOVER_WARNING:
        score = 1
    if total_waste >= 3000:
        score = 2
    elif total_waste >= 800 and score == 0:
        score = 1
    severity = ["ok", "warning", "severe"][score]

    issues.sort(key=lambda x: -x["cost"])
    headline = (f"今日成交{n}笔/涉及{len(by_code)}只标的/换手率{turnover:.0%}，"
                f"频繁操作估计浪费 {total_waste:,.0f} 元"
                f"（追高{chase_cost:,.0f} + 杀低{panic_cost:,.0f} + "
                f"日内回转{roundtrip_loss:,.0f} + 费用{fees['total']:,.0f}）。"
                f"若不操作，这笔钱本可留在账户里。")

    return {
        "date": date or datetime.date.today().isoformat(),
        "total_trades": n,
        "symbols": len(by_code),
        "buy_amount": round(buy_amount, 2),
        "sell_amount": round(sell_amount, 2),
        "turnover": round(turnover, 4),
        "fees": fees,
        "chase_cost": round(chase_cost, 2),
        "panic_cost": round(panic_cost, 2),
        "roundtrip_loss": round(roundtrip_loss, 2),
        "quick_flip": {"count": quick_flip_count, "loss": round(quick_flip_loss, 2)},
        "total_waste": round(total_waste, 2),
        "issues": issues,
        "severity": severity,
        "headline": headline,
    }


# ============================================================
# 报告渲染
# ============================================================

def render_behavior_alert_html(result: dict) -> str:
    """渲染报告顶部显著警示横幅（severity=ok 时返回绿条简短提示）"""
    if not result or result.get("total_trades", 0) == 0:
        return ""

    if result["severity"] == "severe":
        bg, border, icon = "#fff1f0", "#ffa39e", "🚨"
        title_color = "#cf1322"
    elif result["severity"] == "warning":
        bg, border, icon = "#fffbe6", "#ffe58f", "⚠️"
        title_color = "#ad6800"
    else:
        bg, border, icon = "#f6ffed", "#b7eb8f", "✅"
        title_color = "#389e0d"

    html = f'''<div style="background:{bg};border:2px solid {border};border-radius:8px;padding:16px 20px;margin:12px 0">
<div style="font-size:16px;font-weight:bold;color:{title_color}">{icon} 交易行为自诊断：频繁操作警示</div>
<div style="margin-top:8px;font-size:14px;color:#333;line-height:1.8">
<b style="color:{title_color};font-size:15px">{result["headline"]}</b></div>
<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:8px">
<span style="background:#fff;border:1px solid {border};border-radius:6px;padding:6px 12px">成交笔数 <b>{result["total_trades"]}</b></span>
<span style="background:#fff;border:1px solid {border};border-radius:6px;padding:6px 12px">涉及标的 <b>{result["symbols"]}</b>只</span>
<span style="background:#fff;border:1px solid {border};border-radius:6px;padding:6px 12px">换手率 <b>{result["turnover"]:.0%}</b></span>
<span style="background:#fff;border:1px solid {border};border-radius:6px;padding:6px 12px">追高成本 <b style="color:#cf1322">{result["chase_cost"]:,.0f}</b>元</span>
<span style="background:#fff;border:1px solid {border};border-radius:6px;padding:6px 12px">杀低损失 <b style="color:#cf1322">{result["panic_cost"]:,.0f}</b>元</span>
<span style="background:#fff;border:1px solid {border};border-radius:6px;padding:6px 12px">日内回转亏损 <b style="color:#cf1322">{result["roundtrip_loss"]:,.0f}</b>元</span>
<span style="background:#fff;border:1px solid {border};border-radius:6px;padding:6px 12px">交易费用 <b>{result["fees"]["total"]:,.0f}</b>元</span>
</div>'''

    if result["issues"]:
        html += '<table style="margin-top:10px"><tr><th>标的</th><th>问题诊断</th><th>浪费金额(元)</th></tr>'
        for it in result["issues"][:10]:
            html += (f'<tr><td><b>{it["name"]}</b>({it["code"]})</td>'
                     f'<td style="text-align:left">{it["desc"]}</td>'
                     f'<td style="color:#cf1322;font-weight:bold">{it["cost"]:,.0f}</td></tr>')
        html += '</table>'

    if result["severity"] == "severe":
        html += '''<div style="margin-top:10px;padding:10px 12px;background:#fff;border-radius:6px;border-left:4px solid #cf1322;color:#555;font-size:12px;line-height:1.8">
<b style="color:#cf1322">纪律提醒：</b>当日换手率过高时，手续费与情绪化价差损失会持续侵蚀收益。
建议：① 每笔下单前自问"这是计划内的操作吗"；② 同一标的当日第2次反向操作需间隔≥1小时；
③ 追高买入前先看当日已有更低成交价；④ 非止损/止盈触发的临时起意操作一律不做。</div>'''

    html += '</div>'
    return html


def render_behavior_summary_text(result: dict) -> str:
    """纯文本版（供日志/终端/邮件正文）"""
    if not result or result.get("total_trades", 0) == 0:
        return ""
    icon = {"severe": "🚨", "warning": "⚠️", "ok": "✅"}[result["severity"]]
    lines = [f"{icon} [交易行为自诊断] {result['headline']}"]
    for it in result["issues"][:10]:
        lines.append(f"   - {it['name']}({it['code']}): {it['desc']}")
    return "\n".join(lines)


# ============================================================
# 数据加载与CLI
# ============================================================

def load_trades_from_json(path: str) -> tuple:
    """加载 trades_today.json 格式，返回 (trades, date)"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    trades = [t for t in data.get("trades", []) if t.get("status", "已成") == "已成"]
    return trades, data.get("date", "")


def load_trades_from_xlsx(path: str) -> tuple:
    """加载券商导出成交明细xlsx，返回 (trades, date)"""
    import pandas as pd
    df = pd.read_excel(path)
    col_map = {"成交时间": "time", "证券代码": "code", "证券名称": "name",
               "委托方向": "direction", "成交数量": "qty",
               "成交价格": "price", "成交金额": "amount"}
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    trades = []
    for _, r in df.iterrows():
        direction = str(r.get("direction", ""))
        if "买" in direction:
            direction = "买入"
        elif "卖" in direction:
            direction = "卖出"
        else:
            continue
        trades.append({
            "time": str(r.get("time", "")),
            "code": str(r.get("code", "")).zfill(6),
            "name": str(r.get("name", "")),
            "direction": direction,
            "qty": int(r.get("qty", 0)),
            "price": float(r.get("price", 0)),
            "amount": float(r.get("amount", 0) or 0),
        })
    return trades, ""


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("用法: python trade_behavior.py <成交明细.xlsx|trades_today.json> [总资金]")
        _sys.exit(1)
    _path = _sys.argv[1]
    _capital = float(_sys.argv[2]) if len(_sys.argv) > 2 else 1_000_000
    if _path.lower().endswith(".xlsx"):
        _trades, _date = load_trades_from_xlsx(_path)
    else:
        _trades, _date = load_trades_from_json(_path)
    _result = analyze_trade_behavior(_trades, _capital, _date)
    print(render_behavior_summary_text(_result))
    print(json.dumps(_result, ensure_ascii=False, indent=2))
