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

today = datetime.date.today().strftime("%Y-%m-%d")
now = datetime.datetime.now().strftime("%H:%M:%S")
tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

# ============================================================
# 当前持仓（从券商截图更新，只需代码+名称+持仓数量+成本价）
# ============================================================
holdings_list = [
    {"code": "588000", "名称": "科创50", "赛道": "指数ETF", "数量": 170100, "成本": 1.978},
    {"code": "603501", "名称": "豪威集团", "赛道": "CIS芯片", "数量": 700, "成本": 98.643},
    {"code": "002558", "名称": "巨人网络", "赛道": "游戏", "数量": 1000, "成本": 25.647},
    {"code": "159205", "名称": "创业东财", "赛道": "指数ETF", "数量": 1500, "成本": 1.301},
    {"code": "002185", "名称": "华天科技", "赛道": "半导体封测", "数量": 1100, "成本": 14.500},
    {"code": "000858", "名称": "五粮液", "赛道": "白酒", "数量": 300, "成本": 73.530},
    {"code": "001309", "名称": "德明利", "赛道": "存储芯片", "数量": 100, "成本": 453.135},
    {"code": "002415", "名称": "海康威视", "赛道": "AI视觉", "数量": 1500, "成本": 35.530},
    {"code": "600036", "名称": "招商银行", "赛道": "银行", "数量": 1200, "成本": 38.820},
    {"code": "601012", "名称": "隆基绿能", "赛道": "光伏", "数量": 600, "成本": 12.520},
]

# 主模式参数
STOP_LOSS_PCT = 0.08       # 固定止损: 最新价跌8%
DRAWDOWN_FROM_HIGH = 0.05  # 高点回落5%触发
REBOUND_FROM_LOW = 0.02    # 低点反弹2%触发
TOTAL_CAPITAL = 710000     # 总资金
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
# 条件单生成逻辑
# ============================================================
def generate_condition_orders(holding, df, realtime_price):
    """
    为单只持仓生成次日条件单
    返回: list of order dicts
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
    atr = latest.get("atr", price * 0.025)
    if pd.isna(atr) or atr <= 0:
        atr = price * 0.025
    if pd.isna(ma20):
        ma20 = price

    # 判断趋势
    trend_up = price > ma20 and (not pd.isna(ma60) and price > ma60)
    trend_down = price < ma20

    # ---- 条件单1: 定价止损（必挂，20天有效）----
    stop_loss_price = round(price * (1 - STOP_LOSS_PCT), 3)
    orders.append({
        "类型": "定价卖出",
        "优先级": "★★★必挂",
        "证券代码": code,
        "证券名称": name,
        "方向": "卖出",
        "触发价": stop_loss_price,
        "数量": qty,
        "有效期": "20个交易日",
        "说明": f"最新价{price:.3f}×92%={stop_loss_price:.3f}，跌破即走，不犹豫",
    })

    # ---- 条件单2: 时间条件单（14:50尾盘确认，10天）----
    # 如果趋势向下，14:50时若仍低于MA20则卖出
    if trend_down:
        time_price = round(min(price, ma20) * 0.998, 3)
        orders.append({
            "类型": "时间条件单",
            "优先级": "★★★必挂",
            "证券代码": code,
            "证券名称": name,
            "方向": "卖出",
            "触发价": time_price,
            "触发时间": "14:50",
            "数量": qty,
            "有效期": "10个交易日",
            "说明": f"趋势偏弱，14:50若≤{time_price:.3f}(MA20附近)则尾盘清仓",
        })

    # ---- 条件单3: 回落卖出（保护利润，10天）----
    # 从日内最高回落5%触发
    drawdown_price = round(price * (1 - DRAWDOWN_FROM_HIGH), 3)
    orders.append({
        "类型": "回落卖出",
        "优先级": "★★建议",
        "证券代码": code,
        "证券名称": name,
        "方向": "卖出",
        "触发价": drawdown_price,
        "监控方式": f"日高回落{int(DRAWDOWN_FROM_HIGH*100)}%",
        "数量": qty,
        "有效期": "10个交易日",
        "说明": f"若冲高后从最高点回落5%（约{drawdown_price:.3f}），锁定利润离场",
    })

    # ---- 条件单4: 反弹买入（仅趋势向上时，5天）----
    if trend_up and not pd.isna(ma20):
        rebound_price = round(ma20 * (1 + REBOUND_FROM_LOW), 3)
        buy_qty = min(int(TOTAL_CAPITAL * 0.05 / price / 100) * 100, 500)
        if buy_qty >= 100:
            orders.append({
                "类型": "反弹买入",
                "优先级": "★可选",
                "证券代码": code,
                "证券名称": name,
                "方向": "买入",
                "触发价": rebound_price,
                "数量": buy_qty,
                "有效期": "5个交易日",
                "说明": f"回踩MA20({ma20:.3f})后反弹2%确认支撑有效，小仓补入{buy_qty}股",
            })

    return orders


# ============================================================
# 风控检查
# ============================================================
def risk_check(holdings_with_price):
    """风控检查，返回预警列表"""
    alerts = []
    total_value = sum(h["市值"] for h in holdings_with_price)

    for h in holdings_with_price:
        ratio = h["市值"] / total_value * 100 if total_value > 0 else 0
        h["仓位占比"] = ratio

        # 单只超25%
        if ratio > MAX_SINGLE_POSITION * 100:
            alerts.append({
                "级别": "🔴严重",
                "内容": f"{h['名称']}({h['code']})仓位{ratio:.1f}%超限(>{MAX_SINGLE_POSITION*100:.0f}%)，"
                        f"必须减仓至{MAX_SINGLE_POSITION*100:.0f}%以下！"
                        f"建议明日减仓{int((ratio - MAX_SINGLE_POSITION*100) / 100 * total_value / h['price'] / 100) * 100}股",
            })
        elif ratio > 15:
            alerts.append({
                "级别": "🟡警告",
                "内容": f"{h['名称']}({h['code']})仓位{ratio:.1f}%偏高(>15%)，注意分散",
            })

        # 浮亏超10%
        pnl_pct = (h["price"] / h["成本"] - 1) * 100 if h["成本"] > 0 else 0
        if pnl_pct < -10:
            alerts.append({
                "级别": "🔴严重",
                "内容": f"{h['名称']}浮亏{pnl_pct:.1f}%超-10%红线！明日必须执行止损，不允许补仓！",
            })

    # 总仓位检查
    cash_ratio = (TOTAL_CAPITAL - total_value) / TOTAL_CAPITAL * 100
    if cash_ratio < 5:
        alerts.append({
            "级别": "🔴严重",
            "内容": f"可用资金仅{cash_ratio:.1f}%（几乎满仓），完全丧失机动性！"
                    f"必须减仓至少20%释放资金",
        })

    return alerts


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

# 4. 风控检查
print(f"\n[风控] 检查仓位风险...")
risk_alerts = risk_check(holdings_with_price)
for alert in risk_alerts:
    print(f"  {alert['级别']} {alert['内容']}")

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

# ---- 风控预警 ----
html += '<h2>⚠️ 风控预警</h2>'
if risk_alerts:
    for alert in risk_alerts:
        cls = "alert-danger" if "严重" in alert["级别"] else "alert-warning"
        html += f'<div class="alert {cls}">{alert["级别"]} {alert["内容"]}</div>'
else:
    html += '<div class="alert alert-success">✅ 仓位风控正常</div>'

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
    ratio = h.get("仓位占比", 0)
    ratio_color = "#e53935" if ratio > 25 else ("#ff9800" if ratio > 15 else "#333")

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
