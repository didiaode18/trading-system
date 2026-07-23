# -*- coding: utf-8 -*-
"""
每日条件单自动生成器 V1.0
==========================
盘后运行 → 生成次日完整操作计划 → 邮件发送
核心目标: 消除盘中人为干预，所有操作提前锁定

输出:
1. 次日条件单设置清单（照抄到东方财富APP）
2. 操作纪律锁（今日禁止事项）
3. 风控预警（仓位/集中度）
4. 推荐标的条件单（如果有）

使用: python daily_orders.py
"""

import sys
import os
import datetime
import logging
import io

# Windows控制台编码修复
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "trading_system"))

import numpy as np
import pandas as pd
import config
from notify.email_notify import send_email
from data.realtime import fetch_realtime_batch
from data.data_loader import fetch_stock_daily_baostock, _bs_logout
from strategy.recommend_engine import run_recommendation, generate_trading_plan
from risk.risk_control import RiskGate, RISK_CONFIG

today = datetime.date.today().strftime("%Y-%m-%d")
now = datetime.datetime.now().strftime("%H:%M:%S")
tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

# ============================================================
# 当前持仓（从券商截图更新，只需代码+名称+持仓数量+成本价）
# ============================================================
holdings_list = [
    {"code": "588000", "名称": "科创50", "赛道": "指数ETF", "数量": 318100, "成本": 1.953},
    {"code": "603501", "名称": "豪威集团", "赛道": "CIS芯片", "数量": 800, "成本": 97.530},
    {"code": "002558", "名称": "巨人网络", "赛道": "游戏", "数量": 1200, "成本": 31.082},
    {"code": "159205", "名称": "创业东财", "赛道": "指数ETF", "数量": 1100, "成本": 1.759},
    {"code": "002185", "名称": "华天科技", "赛道": "半导体封测", "数量": 100, "成本": -0.164},
]

# 主模式参数
STOP_LOSS_PCT = 0.08       # 固定止损: 最新价跌8%
DRAWDOWN_FROM_HIGH = 0.05  # 高点回落5%触发
REBOUND_FROM_LOW = 0.02    # 低点反弹2%触发
TOTAL_CAPITAL = 424000     # 总资金
MAX_DAILY_TRADES = 3       # 每日最大交易笔数
MAX_SINGLE_POSITION = 0.25 # 单只最大仓位25%

logging.basicConfig(level=logging.INFO, format="%(message)s")


# ============================================================
# 技术指标计算
# ============================================================
def compute_indicators(df):
    """计算MA/MACD/RSI/ATR/布林带"""
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    # RSI(14)
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["close"].ewm(span=12).mean()
    ema26 = df["close"].ewm(span=26).mean()
    df["macd_dif"] = ema12 - ema26
    df["macd_dea"] = df["macd_dif"].ewm(span=9).mean()
    df["macd_hist"] = 2 * (df["macd_dif"] - df["macd_dea"])

    # 布林带
    df["boll_mid"] = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    df["boll_upper"] = df["boll_mid"] + 2 * std20
    df["boll_lower"] = df["boll_mid"] - 2 * std20

    # ATR(14)
    high_low = df["high"] - df["low"]
    high_close = abs(df["high"] - df["close"].shift(1))
    low_close = abs(df["low"] - df["close"].shift(1))
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # 量比
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    return df


# ============================================================
# 条件单生成逻辑 V2（精简版：1必挂+1可选，趋势决定类型）
# ============================================================
def generate_condition_orders(holding, df, realtime_price):
    """
    为单只持仓生成次日条件单（V2精简版）

    规则:
      - 所有持仓统一标配: 1张止损单(必挂) + 1张止盈单(可选)
      - 止损三档自动匹配:
          浮亏/浮盈<5%: 初始止损 = 成本×0.9
          浮盈5%~15%: 保本止损 = 成本价
          浮盈>15%: 移动止盈 = 阶段高点回落5%
      - 多头趋势: 止损单 + 回落卖出止盈单
      - 空头趋势: 只允许止损单，禁止任何买入/加仓，强制给出清仓建议
      - 默认14:50尾盘触发，过滤盘中杂波
    """
    orders = []
    code = holding["code"]
    name = holding["名称"]
    qty = holding["数量"]
    cost = holding["成本"]
    price = realtime_price if realtime_price > 0 else df["close"].iloc[-1]

    latest = df.iloc[-1]
    ma20 = latest.get("ma20", price)
    ma60 = latest.get("ma60", price)
    if pd.isna(ma20):
        ma20 = price
    if pd.isna(ma60):
        ma60 = price

    # 判断趋势方向
    is_bullish = price > ma20 and ma20 > ma60  # 均线多头排列
    is_bearish = price < ma20  # 空头趋势

    # 计算浮盈率
    pnl_pct = (price / cost - 1) * 100 if cost > 0 else 0

    # ================================================================
    # 止损单（必挂）—— 三档自动匹配
    # ================================================================
    if pnl_pct < 5:
        # 第一档: 初始止损 = 成本×0.9（成本下10%）
        stop_price = round(cost * 0.9, 3)
        stop_type = "初始止损"
        stop_note = f"浮盈{pnl_pct:.1f}%<5%，初始止损=成本{cost:.3f}×90%"
    elif pnl_pct < 15:
        # 第二档: 保本止损 = 成本价（保证不亏本金）
        stop_price = round(cost, 3)
        stop_type = "保本止损"
        stop_note = f"浮盈{pnl_pct:.1f}%(5%~15%)，保本止损=成本价{cost:.3f}"
    else:
        # 第三档: 移动止盈 = 阶段高点回落5%
        recent_high = df["high"].iloc[-20:].max() if len(df) >= 20 else price
        stop_price = round(recent_high * 0.95, 3)
        stop_type = "移动止盈"
        stop_note = f"浮盈{pnl_pct:.1f}%>15%，移动止盈=20日高{recent_high:.3f}回落5%"

    # 止损单不能高于当前价（否则立即触发）
    if stop_price >= price:
        stop_price = round(price * 0.95, 3)
        stop_note += f"（调整: 止损≥现价，改为现价×95%）"

    orders.append({
        "类型": f"止损单({stop_type})",
        "优先级": "★★★必挂",
        "证券代码": code,
        "证券名称": name,
        "方向": "卖出",
        "触发价": stop_price,
        "触发时间": "14:50",
        "数量": qty,
        "有效期": "20个交易日",
        "说明": stop_note,
    })

    # ================================================================
    # 按趋势方向决定第二张条件单
    # ================================================================
    if is_bullish:
        # 多头趋势: 回落卖出止盈单（保护利润）
        drawdown_price = round(price * (1 - DRAWDOWN_FROM_HIGH), 3)
        orders.append({
            "类型": "回落止盈",
            "优先级": "★★建议",
            "证券代码": code,
            "证券名称": name,
            "方向": "卖出",
            "触发价": drawdown_price,
            "监控方式": f"日高回落{int(DRAWDOWN_FROM_HIGH*100)}%",
            "触发时间": "盘中实时",
            "数量": qty,
            "有效期": "10个交易日",
            "说明": f"多头持有，从最高点回落5%({drawdown_price:.3f})锁定利润",
        })
    else:
        # 空头趋势: 禁止任何买入/加仓，强制给出减仓/清仓建议
        orders.append({
            "类型": "强制减仓",
            "优先级": "★★★必挂",
            "证券代码": code,
            "证券名称": name,
            "方向": "卖出",
            "触发价": round(price * 0.998, 3),
            "触发时间": "14:50",
            "数量": qty if is_bearish and pnl_pct < -10 else max(qty // 2, 100),
            "有效期": "5个交易日",
            "说明": f"空头趋势(价格<MA20)，浮盈{pnl_pct:.1f}%，"
                    f"{'14:50清仓' if pnl_pct < -10 else '14:50减仓1/2'}，禁止补仓",
        })

    return orders


# ============================================================
# 持仓健康度巡检（四级分类 + 处置方案）
# ============================================================
def health_inspection(holdings_with_price, all_data):
    """
    持仓风险四级巡检，返回按紧急程度排序的结果
    等级: 危险 > 预警 > 关注 > 健康
    """
    gate = RiskGate(total_capital=TOTAL_CAPITAL)
    # 构建 RiskGate 需要的 holdings dict
    holdings_dict = {}
    for h in holdings_with_price:
        code = h["code"]
        df = all_data.get(code)
        ma20 = df["ma20"].iloc[-1] if df is not None and not pd.isna(df["ma20"].iloc[-1]) else h["price"]
        ma60 = df["ma60"].iloc[-1] if df is not None and not pd.isna(df["ma60"].iloc[-1]) else h["price"]
        stock_type = "etf" if h.get("赛道") == "指数ETF" else "stock"
        holdings_dict[code] = {
            "name": h["名称"],
            "cost": h["成本"],
            "price": h["price"],
            "shares": h["数量"],
            "sector": h["赛道"],
            "type": stock_type,
            "ma20": ma20,
            "ma60": ma60,
        }
    results = gate.inspect_holdings(holdings_dict)

    # 为危险/预警级标的生成具体调仓方案
    for r in results:
        code = r["code"]
        h = next((x for x in holdings_with_price if x["code"] == code), None)
        if not h:
            continue
        price = h["price"]
        qty = h["数量"]
        cost = h["成本"]
        pnl_pct = r["pnl_pct"]

        # 仓位超标 → 计算减仓方案
        if r["over_limit"] and r["reduce_shares"] > 0:
            reduce_qty = r["reduce_shares"]
            # 分批减仓: 分2档
            batch1 = (reduce_qty // 2 // 100) * 100
            batch2 = reduce_qty - batch1
            r["reduce_plan"] = {
                "total_reduce": reduce_qty,
                "batch1": {"shares": batch1, "price": round(price * 0.998, 3), "note": "第一档: 开盘即挂"},
                "batch2": {"shares": batch2, "price": round(price * 0.995, 3), "note": "第二档: 反弹到MA5附近"},
                "target_ratio": r["position_ratio"] - (reduce_qty * price / TOTAL_CAPITAL * 100),
            }
        else:
            r["reduce_plan"] = None

        # 危险级 → 清仓条件单参数
        if r["level"] == "危险":
            r["clear_order"] = {
                "direction": "卖出",
                "shares": qty,
                "trigger_price": round(price * 0.998, 3),
                "trigger_time": "14:50",
                "note": f"浮亏{pnl_pct:.1f}%，无条件清仓",
            }
        elif r["level"] == "预警":
            reduce_qty = max(qty // 2 // 100 * 100, 100)
            r["clear_order"] = {
                "direction": "卖出",
                "shares": reduce_qty,
                "trigger_price": round(price * 0.998, 3),
                "trigger_time": "14:50",
                "note": f"空头趋势+浮亏{pnl_pct:.1f}%，反弹减仓{reduce_qty}股",
            }
        else:
            r["clear_order"] = None

    return results


# ============================================================
# 纪律锁
# ============================================================
def generate_discipline_lock():
    """生成今日操作纪律（禁止事项）"""
    return [
        f"🚫 明日最多交易{MAX_DAILY_TRADES}笔，超过即锁仓不动",
        "🚫 禁止在盘中临时决定买入/卖出，所有操作必须在前一晚通过条件单设定",
        "🚫 禁止越跌越补（单只当日最多补仓1次，且必须通过条件单触发）",
        "🚫 禁止追涨杀跌：看到涨了想追、跌了想割，都是情绪，不是信号",
        "🚫 禁止满仓操作：任何时刻现金比例不得低于10%",
        "✅ 唯一允许的手动操作：条件单触发后确认成交",
        "✅ 如果手痒想操作：先等10分钟，问自己'这是系统信号还是情绪？'",
    ]


# ============================================================
# 主流程
# ============================================================
print("=" * 60)
print(f"  每日条件单生成器 V1.0 | {today} {now}")
print(f"  目标: 生成{tomorrow}操作计划，消除盘中人为干预")
print("=" * 60)

# 1. 获取实时行情
codes = [h["code"] for h in holdings_list]
print(f"\n[行情] 获取{len(codes)}只持仓实时价...")
quotes = fetch_realtime_batch(codes)
print(f"[行情] 成功{len(quotes)}只")

# 2. 获取历史K线
print(f"\n[K线] 获取历史数据...")
all_data = {}
try:
    for h in holdings_list:
        code = h["code"]
        start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m-%d")
        df = fetch_stock_daily_baostock(code, start_date=start)
        if not df.empty and len(df) >= 20:
            df = compute_indicators(df)
            all_data[code] = df
            print(f"  {code} {h['名称']}: {len(df)}根K线")
    _bs_logout()
except Exception as e:
    print(f"  [警告] K线获取异常: {e}")

# 3. 构建持仓数据
holdings_with_price = []
for h in holdings_list:
    code = h["code"]
    rt = quotes.get(code, {})
    price = rt.get("price", 0)
    if price <= 0 and code in all_data:
        price = all_data[code]["close"].iloc[-1]
    market_value = price * h["数量"]
    holdings_with_price.append({
        **h,
        "price": price,
        "市值": market_value,
        "change_pct": rt.get("change_pct", 0),
    })

# 4. 持仓健康度巡检（四级分类）
print(f"\n[巡检] 持仓健康度四级分类...")
health_results = health_inspection(holdings_with_price, all_data)
for r in health_results:
    icon = {"危险": "🔴", "预警": "🟠", "关注": "🟡", "健康": "🟢"}.get(r["level"], "⚪")
    print(f"  {icon} {r['name']}({r['code']}): {r['level']} | 浮盈{r['pnl_pct']:+.1f}% | {r['action']}")
    if r["over_limit"]:
        print(f"     ⚠️ 仓位{r['position_ratio']:.1f}%超标，建议减仓{r['reduce_shares']}股")

# 5. 生成条件单
print(f"\n[条件单] 生成次日操作计划...")
all_orders = []
for h in holdings_list:
    code = h["code"]
    if code in all_data:
        rt_price = quotes.get(code, {}).get("price", 0)
        orders = generate_condition_orders(h, all_data[code], rt_price)
        all_orders.extend(orders)
        print(f"  {h['名称']}: {len(orders)}条条件单")

# 6. 纪律锁
discipline = generate_discipline_lock()

# ============================================================
# 生成HTML报告
# ============================================================
print(f"\n[报告] 生成条件单报告...")

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>次日条件单 {tomorrow}</title>
<style>
body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
h1 {{ color: #1a237e; font-size: 20px; border-bottom: 3px solid #1a237e; padding-bottom: 10px; }}
h2 {{ color: #333; font-size: 16px; margin-top: 25px; }}
.alert {{ padding: 12px 15px; border-radius: 6px; margin: 10px 0; font-size: 13px; }}
.alert-danger {{ background: #ffebee; border-left: 4px solid #e53935; color: #b71c1c; }}
.alert-warning {{ background: #fff3e0; border-left: 4px solid #ff9800; color: #e65100; }}
.alert-success {{ background: #e8f5e9; border-left: 4px solid #4caf50; color: #1b5e20; }}
.alert-info {{ background: #e3f2fd; border-left: 4px solid #2196f3; color: #0d47a1; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin: 10px 0; background: white; }}
th {{ background: #1a237e; color: white; padding: 8px 6px; text-align: left; }}
td {{ padding: 7px 6px; border-bottom: 1px solid #eee; }}
tr:hover {{ background: #f5f5f5; }}
.order-card {{ background: white; border-radius: 8px; padding: 15px; margin: 10px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
.order-must {{ border-left: 5px solid #e53935; }}
.order-suggest {{ border-left: 5px solid #ff9800; }}
.order-optional {{ border-left: 5px solid #4caf50; }}
.discipline {{ background: #fce4ec; border: 2px solid #e53935; border-radius: 8px; padding: 15px; margin: 15px 0; }}
.discipline li {{ margin: 8px 0; font-size: 13px; }}
.price-tag {{ font-size: 16px; font-weight: bold; color: #e53935; }}
.tag {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: bold; }}
.tag-must {{ background: #e53935; color: white; }}
.tag-suggest {{ background: #ff9800; color: white; }}
.tag-optional {{ background: #4caf50; color: white; }}
</style></head><body>

<h1>📋 {tomorrow} 条件单操作计划</h1>
<p style="color:#666;font-size:12px">生成时间: {today} {now} | 总资金: {TOTAL_CAPITAL:,.0f}元 | 持仓{len(holdings_list)}只</p>
"""

# ---- 持仓健康度报告（优先级: 危险>预警>关注>健康）----
html += '<h2>🏥 持仓健康度巡检报告</h2>'
level_colors = {"危险": "#e53935", "预警": "#ff9800", "关注": "#ffc107", "健康": "#4caf50"}
level_bg = {"危险": "#ffebee", "预警": "#fff3e0", "关注": "#fffde7", "健康": "#e8f5e9"}
for r in health_results:
    lv = r["level"]
    color = level_colors.get(lv, "#333")
    bg = level_bg.get(lv, "#f5f5f5")
    icon = {"危险": "🔴", "预警": "🟠", "关注": "🟡", "健康": "🟢"}.get(lv, "⚪")
    html += f'<div class="alert" style="background:{bg};border-left:4px solid {color};margin:8px 0">'
    html += f'<b>{icon} [{lv}] {r["name"]}({r["code"]})</b> '
    html += f'| 浮盈<b style="color:{"#e53935" if r["pnl_pct"]<0 else "#4caf50"}">{r["pnl_pct"]:+.1f}%</b> '
    html += f'| 仓位{r["position_ratio"]:.1f}% '
    html += f'| <b>建议: {r["action"]}</b>'
    # 仓位超标调仓方案
    if r.get("reduce_plan"):
        rp = r["reduce_plan"]
        html += f'<br><small>📉 调仓方案: 减仓{rp["total_reduce"]}股至{rp["target_ratio"]:.1f}% '
        html += f'| 第一档{rp["batch1"]["shares"]}股@{rp["batch1"]["price"]:.3f}({rp["batch1"]["note"]}) '
        html += f'| 第二档{rp["batch2"]["shares"]}股@{rp["batch2"]["price"]:.3f}({rp["batch2"]["note"]})</small>'
    # 清仓/减仓条件单
    if r.get("clear_order"):
        co = r["clear_order"]
        html += f'<br><small>📝 条件单: {co["direction"]} {co["shares"]}股 @ {co["trigger_price"]:.3f} '
        html += f'触发时间{co["trigger_time"]} | {co["note"]}</small>'
    html += '</div>'

# ---- 风控预警 ----
html += '<h2>⚠️ 风控状态</h2>'
gate = RiskGate(total_capital=TOTAL_CAPITAL)
total_mv = sum(h["市值"] for h in holdings_with_price)
cash_ratio = (TOTAL_CAPITAL - total_mv) / TOTAL_CAPITAL * 100
html += f'<div class="alert alert-info">总仓位: {total_mv/TOTAL_CAPITAL*100:.1f}% | 现金: {cash_ratio:.1f}% | '
html += f'ETF上限{RISK_CONFIG["etf_max_ratio"]*100:.0f}% | 个股上限{RISK_CONFIG["stock_max_ratio"]*100:.0f}% | '
html += f'赛道上限{RISK_CONFIG["sector_max_ratio"]*100:.0f}%</div>'

# ---- 纪律锁 ----
html += '<h2>🔒 操作纪律锁（铁律，不可违反）</h2>'
html += '<div class="discipline"><ul>'
for d in discipline:
    html += f'<li>{d}</li>'
html += '</ul></div>'

# ---- 条件单清单 ----
html += f'<h2>📝 条件单设置清单（共{len(all_orders)}条，照抄到东方财富APP）</h2>'
html += '<div class="alert alert-info">💡 操作步骤: 打开东方财富APP → 交易 → 智能条件单 → 逐条添加以下条件单 → 确认后盘中不再操作</div>'

# 按优先级排序
priority_order = {"★★★必挂": 0, "★★建议": 1, "★可选": 2}
all_orders.sort(key=lambda x: priority_order.get(x["优先级"], 9))

for idx, order in enumerate(all_orders, 1):
    if "必挂" in order["优先级"]:
        card_cls = "order-must"
        tag_cls = "tag-must"
    elif "建议" in order["优先级"]:
        card_cls = "order-suggest"
        tag_cls = "tag-suggest"
    else:
        card_cls = "order-optional"
        tag_cls = "tag-optional"

    trigger_time = order.get("触发时间", "盘中实时")
    html += f"""
<div class="order-card {card_cls}">
<b>#{idx}</b> <span class="tag {tag_cls}">{order['优先级']}</span>
<b>{order['类型']}</b> | {order['证券代码']} {order['证券名称']} | <b>{order['方向']}</b>
<table>
<tr><td style="width:80px"><b>触发价</b></td><td class="price-tag">{order['触发价']:.3f} 元</td></tr>
<tr><td><b>触发时间</b></td><td>{trigger_time}</td></tr>
<tr><td><b>数量</b></td><td>{order['数量']} 股</td></tr>
<tr><td><b>有效期</b></td><td>{order['有效期']}</td></tr>
<tr><td><b>说明</b></td><td style="font-size:11px;color:#555">{order['说明']}</td></tr>
</table>
</div>"""

# ---- 持仓总览 ----
html += '<h2>📊 当前持仓总览</h2>'
html += '<table><tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>最新价</th><th>市值</th><th>仓位</th><th>浮盈亏</th><th>趋势</th></tr>'
for h in sorted(holdings_with_price, key=lambda x: x["市值"], reverse=True):
    pnl = (h["price"] / h["成本"] - 1) * 100 if h["成本"] > 0 else 0
    pnl_color = "#e53935" if pnl < 0 else "#4caf50"
    ratio = h["市值"] / TOTAL_CAPITAL * 100 if TOTAL_CAPITAL > 0 else 0
    ratio_color = "#e53935" if ratio > 20 else ("#ff9800" if ratio > 15 else "#333")

    # 趋势判断
    code = h["code"]
    trend = "—"
    if code in all_data:
        last = all_data[code].iloc[-1]
        if h["price"] > last.get("ma20", 0) and h["price"] > last.get("ma60", 0):
            trend = "📈多头"
        elif h["price"] < last.get("ma20", 0):
            trend = "📉空头"
        else:
            trend = "➡️震荡"

    html += f'<tr><td>{h["code"]}</td><td><b>{h["名称"]}</b></td><td>{h["数量"]:,}</td>'
    html += f'<td>{h["成本"]:.3f}</td><td>{h["price"]:.3f}</td>'
    html += f'<td>{h["市值"]:,.0f}</td>'
    html += f'<td style="color:{ratio_color};font-weight:bold">{ratio:.1f}%</td>'
    html += f'<td style="color:{pnl_color}">{pnl:+.1f}%</td>'
    html += f'<td>{trend}</td></tr>'
html += '</table>'

# ---- 明日操作摘要 ----
must_orders = [o for o in all_orders if "必挂" in o["优先级"]]
html += f"""
<h2>📌 明日操作摘要</h2>
<div class="alert alert-danger">
<b>必须执行:</b> {len(must_orders)}条必挂条件单（止损+时间单），开盘前全部设好<br>
<b>绝对禁止:</b> 盘中手动买卖、越跌越补、追涨杀跌<br>
<b>最大交易:</b> {MAX_DAILY_TRADES}笔/天（含条件单触发），超过即停手<br>
<b>核心原则:</b> 条件单没触发 = 不操作。没信号就是最大的信号。
</div>
"""

html += f"""
<hr style="margin-top:30px">
<p style="color:#999;font-size:11px;text-align:center">
自动生成 by 量化交易系统 | {today} {now}<br>
纪律 > 判断 | 系统 > 情绪 | 活着 > 赚钱
</p>
</body></html>"""

# ============================================================
# 保存 + 发送
# ============================================================
output_dir = os.path.join(os.path.dirname(__file__), "trading_system", "output")
os.makedirs(output_dir, exist_ok=True)
date_str = datetime.date.today().strftime("%Y%m%d")
filepath = os.path.join(output_dir, f"daily_orders_{date_str}.html")

with open(filepath, "w", encoding="utf-8") as f:
    f.write(html)
print(f"\n[保存] {filepath}")

# 输出JSON文件（供QMT执行器读取）
import json
orders_json = {
    "date": today,
    "generated_at": f"{today} {now}",
    "total_capital": TOTAL_CAPITAL,
    "max_daily_trades": MAX_DAILY_TRADES,
    "orders": [],
}
for idx, order in enumerate(all_orders, 1):
    orders_json["orders"].append({
        "order_id": f"ORD_{date_str}_{idx:03d}",
        **order,
    })

json_path = os.path.join(output_dir, f"orders_{date_str}.json")
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(orders_json, f, ensure_ascii=False, indent=2)
print(f"[保存] QMT执行文件: {json_path}")

# 发送邮件
subject = f"[条件单] {tomorrow}操作计划 | {len(all_orders)}条(必挂{len(must_orders)}条) | {today}"
try:
    send_email(subject, html)
    print(f"[发送] ✅ 邮件发送成功")
except Exception as e:
    print(f"[发送] ❌ 邮件发送失败: {e}")

print(f"\n{'='*60}")
print(f"  完成! 明日{len(all_orders)}条条件单已生成")
print(f"  必挂: {len(must_orders)}条 | 建议: {len([o for o in all_orders if '建议' in o['优先级']])}条 | 可选: {len([o for o in all_orders if '可选' in o['优先级']])}条")
print(f"  请在今晚/明早开盘前，照抄到东方财富APP条件单中")
print(f"  盘中不操作！不操作！不操作！")
print(f"{'='*60}")
