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
import json

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
# FIX B4: 统一使用UnifiedRiskEngine替代已废弃的RiskGate
# V4.0(G4): 新增 pre_trade_check_orders 条件单下单前硬校验
from risk.risk_control import UnifiedRiskEngine, RISK_CONFIG, pre_trade_check_orders
from utils.board_rules import is_limit_up as _board_is_limit_up, get_limit_pct
# FIX P2-02: ETF 判定统一走权威源 instrument.is_etf（按代码前缀，含56x/588/159），
# 消除“赛道==指数ETF”与前两位硬编码（漏判 56 开头）的口径分歧。
from utils.instrument import is_etf as _is_etf_code

today = datetime.date.today().strftime("%Y-%m-%d")
now = datetime.datetime.now().strftime("%H:%M:%S")
tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

# ============================================================
# 当前持仓（统一从 holdings.json 读取，消除硬编码不同步）
# ============================================================
# 硬编码降级数据（当 holdings.json 不存在时使用）
_FALLBACK_HOLDINGS = [
    {"code": "588000", "名称": "科创50", "赛道": "指数ETF", "数量": 170100, "成本": 1.063},
    {"code": "002415", "名称": "海康威视", "赛道": "AI视觉", "数量": 500, "成本": 29.124},
    {"code": "603501", "名称": "豪威集团", "赛道": "CIS芯片", "数量": 400, "成本": 145.155},
    {"code": "002409", "名称": "雅克科技", "赛道": "半导体材料", "数量": 400, "成本": 76.065},
    {"code": "002185", "名称": "华天科技", "赛道": "半导体封测", "数量": 0, "成本": 0},
    {"code": "600036", "名称": "招商银行", "赛道": "银行", "数量": 100, "成本": 42.42},
    {"code": "159205", "名称": "创业东财", "赛道": "指数ETF", "数量": 3000, "成本": 1.389},
    {"code": "600276", "名称": "恒瑞医药", "赛道": "创新药", "数量": 100, "成本": 59.05},
    {"code": "603993", "名称": "洛阳钼业", "赛道": "有色资源", "数量": 400, "成本": 20.15},
]


def _load_holdings_from_json():
    """从 holdings.json 读取持仓列表，统一数据源消除多文件硬编码不同步"""
    # FIX: 统一使用config.get_holdings_file()路径解析，与主调度器保持一致
    holdings_file = config.get_holdings_file()
    try:
        with open(holdings_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        result = []
        for code, v in data.items():
            result.append({
                "code": code,
                "名称": v.get("name", code),
                "赛道": v.get("sector", "其他"),
                "数量": v.get("shares", 0),
                "成本": v.get("buy_price", 0),
                "stop_loss": v.get("stop_loss", 0),  # FIX B3: 携带预设止损价
                "buy_date": v.get("buy_date", ""),   # FIX B8: 携带买入日期(时间止损)
            })
        return result
    except Exception:
        return _FALLBACK_HOLDINGS


holdings_list = _load_holdings_from_json()

# 主模式参数
STOP_LOSS_PCT = config.INITIAL_STOP_LOSS_PCT  # FIX B3: 统一从 config 读取(10%)，消除与config的8%vs10%不一致
DRAWDOWN_FROM_HIGH = 0.05  # 高点回落5%触发
REBOUND_FROM_LOW = 0.02    # 低点反弹2%触发
# FIX: 修复TOTAL_CAPITAL硬编码424000与config不一致，改为从 config 引用
TOTAL_CAPITAL = config.TOTAL_CAPITAL     # 总资金（与 config.py 保持一致）
MAX_DAILY_TRADES = 3       # 每日最大交易笔数
# V4.0(G4): 原硬编码25%与config三级仓位硬限制(个股15%)不一致，统一从风控配置引用
MAX_SINGLE_POSITION = RISK_CONFIG["stock_max_ratio"]

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
# 条件单生成逻辑 V3（增强版：分档止损+互斥+去重+价格校验日志）
# ============================================================
def generate_condition_orders(holding, df, realtime_price, _playbook=None):
    """
    为单只持仓生成次日条件单（V3增强版 + V10 regime-aware）

    规则:
      - 趋势≤ 2级(空头): 仅生成1张清仓单(现价×97%，14:50触发)，不重复生成止损单
      - 浮亏状态(现价<成本): 止损价 = 现价 × (1-stop_loss_pct)
      - 浮盈<5%: 初始止损 = 成本×(1-stop_loss_pct)
      - 浮盈5%-15%: 保本止损 = 成本×102%
      - 浮盈>15%: 移动止盈 = 阶段高点×95%
      - 止损/清仓单: 14:50尾盘确认
      - 止盈单: 盘中实时触发
      - 去重: 同标的不得出现2张方向相同、触发价相近(差距<2%)的卖出单

    V10参数:
      _playbook: RegimePlaybook实例，提供regime-aware止损幅度。None时使用默认STOP_LOSS_PCT。
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

    # V10: regime-aware止损幅度（playbook提供时动态调整）
    if _playbook is not None:
        _regime_stop = _playbook.risk_rules().get("stop_loss_pct", STOP_LOSS_PCT)
    else:
        _regime_stop = STOP_LOSS_PCT

    # 判断趋势方向
    is_bullish = price > ma20 and ma20 > ma60
    is_bearish = price < ma20

    # 计算浮盈率
    pnl_pct = (price / cost - 1) * 100 if cost > 0 else 0

    # 阶段高点（20日）
    recent_high = df["high"].iloc[-20:].max() if len(df) >= 20 else price

    # ================================================================
    # 趋势判断: 空头趋势 → 仅生成1张清仓单，不重复生成止损单
    # ================================================================
    if is_bearish and pnl_pct < -10:
        # 深度空头: 清仓单
        clear_price = round(price * 0.97, 3)
        _log_price_check(code, name, "清仓", clear_price, price)
        orders.append({
            "类型": "清仓条件单(趋势破位)",
            "优先级": "★★★必挂",
            "证券代码": code,
            "证券名称": name,
            "方向": "卖出",
            "触发价": clear_price,
            "触发时间": "14:50",
            "数量": qty,
            "有效期": "3个交易日",
            "说明": f"空头趋势(价格<MA20)，浮亏{pnl_pct:.1f}%，清仓=现价{price:.3f}×97%，禁止补仓",
        })
        return orders  # 清仓单优先级最高，不再生成其他单据

    # ================================================================
    # V2.5: 涨停保护机制 —— 当日涨停时不生成止损单，改为"涨停次日止盈单"
    # 背景: 雅克科技2026-07-20涨停后手动卖出，系统应自动生成次日止盈单
    # ================================================================
    today_change_pct = 0
    if len(df) >= 2:
        prev_close = df["close"].iloc[-2]
        if prev_close > 0:
            today_change_pct = (price / prev_close - 1) * 100

    # V2: 统一涨跌停判定（区分板块 — 主板10%/创业板20%/ST 5%/科创板20%）
    _limit_pct = get_limit_pct(code, name) * 100  # 转为百分比
    _limit_tol = _limit_pct - 0.2  # 容差0.2%处理四舍五入
    if today_change_pct >= _limit_tol:  # 当日涨停
        # 涨停板不触发止损，改为生成"次日止盈单"
        next_day_stop = round(price * 0.97, 3)  # 次日从涨停价回落3%触发
        _log_price_check(code, name, "涨停止盈", next_day_stop, price)
        orders.append({
            "类型": "涨停次日止盈",
            "优先级": "★★★必挂",
            "证券代码": code,
            "证券名称": name,
            "方向": "卖出",
            "触发价": next_day_stop,
            "触发时间": "盘中实时",
            "数量": qty,
            "有效期": "1个交易日",
            "说明": f"今日涨停(+{today_change_pct:.1f}%)，次日从涨停价{price:.3f}回落3%({next_day_stop:.3f})锁利。"
                     f"若次日继续涨停则不触发，继续持有。",
        })
        # 涨停时不再生成普通止损单
        return orders

    # ================================================================
    # 止损单（必挂）—— 四档自动匹配
    # ================================================================
    if cost <= 0:
        # 已回本持仓: 保护性止损 = 现价×85%
        stop_price = round(price * 0.85, 3)
        stop_type = "回本仓保护"
        stop_note = f"已回本持仓，保护性止损=现价{price:.3f}×85%"
    elif pnl_pct < 0:
        # FIX B3: 浮亏状态优先使用holdings.json预设止损价，消除多处硬编码不一致
        # FIX P2: 止损线只升不降（Ratchet原则）—— 硬止损 = 成本×(1-stop) 为绝对底线
        hard_floor = round(cost * (1 - _regime_stop), 3)  # V10: regime-aware止损幅度
        preset_stop = holding.get("stop_loss", 0)
        if preset_stop > 0 and preset_stop < price:
            stop_price = round(max(preset_stop, hard_floor), 3)  # 不低于硬止损底线
            stop_type = "预设止损"
            stop_note = f"浮亏{pnl_pct:.1f}%，预设止损{preset_stop:.3f}，硬止损底线{hard_floor:.3f}，取高者"
        else:
            stop_price = hard_floor  # FIX P2: 浮亏时直接使用硬止损
            stop_type = "硬止损"
            stop_note = f"浮亏{pnl_pct:.1f}%，硬止损=成本{cost:.3f}×{1-_regime_stop:.0%}={hard_floor:.3f}(止损线只升不降)"
    elif pnl_pct < 5:
        # 浮盈<5%: 初始止损 = 成本×(1-stop)
        stop_price = round(cost * (1 - _regime_stop), 3)
        stop_type = "初始止损"
        stop_note = f"浮盈{pnl_pct:.1f}%<5%，初始止损=成本{cost:.3f}×{1-_regime_stop:.0%}"
        # 止损价不能高于现价
        if stop_price >= price:
            stop_price = round(price * 0.95, 3)
            stop_note += f"(调整:止损≥现价，改为现价{price:.3f}×95%)"
    elif pnl_pct < 15:
        # 浮盈5%-15%: 保本止损 = 成本×102%
        stop_price = round(cost * 1.02, 3)
        stop_type = "保本止损"
        stop_note = f"浮盈{pnl_pct:.1f}%(5%~15%)，保本止损=成本{cost:.3f}×102%"
        if stop_price >= price:
            stop_price = round(price * 0.95, 3)
            stop_note += f"(调整:保本价≥现价，改为现价×95%)"
    else:
        # 浮盈>15%: 移动止盈 = 阶段高点×95%
        stop_price = round(recent_high * 0.95, 3)
        stop_type = "移动止盈"
        stop_note = f"浮盈{pnl_pct:.1f}%>15%，移动止盈=20日高{recent_high:.3f}×95%"
        if stop_price >= price:
            stop_price = round(price * 0.95, 3)
            stop_note += f"(调整:高点止损≥现价，改为现价×95%)"

    # 价格校验日志
    _log_price_check(code, name, "止损", stop_price, price)

    # 触发时间: 止损单14:50尾盘确认，止盈单盘中实时
    # V3.1: 止损缓冲确认机制 — 触及止损价后不立即触发，等待5-10分钟确认
    trigger_time = "盘中实时" if stop_type == "移动止盈" else "14:50"
    buffer_note = ""
    if stop_type not in ("移动止盈",):
        buffer_note = " | 缓冲确认:触及后等10分钟，仍在止损下方才执行(防插针洗盘)"

    orders.append({
        "类型": f"止损单({stop_type})",
        "优先级": "★★★必挂",
        "证券代码": code,
        "证券名称": name,
        "方向": "卖出",
        "触发价": stop_price,
        "触发时间": trigger_time,
        "数量": qty,
        "有效期": "20个交易日",
        "说明": stop_note + buffer_note,
    })

    # ================================================================
    # FIX B8: 时间止损 —— 持仓超过MAX_HOLD_DAYS且浮盈<5%则强制离场
    # 背景: 科创50持仓409天浮亏-47.5%, 豪威集团435天浮亏-12.4%, 时间止损从未生效
    # ================================================================
    buy_date_str = holding.get("buy_date", "")
    if buy_date_str and pnl_pct < 5:
        try:
            buy_dt = datetime.datetime.strptime(buy_date_str, "%Y-%m-%d").date()
            hold_days = (datetime.date.today() - buy_dt).days
            max_hold = getattr(config, 'MAX_HOLD_DAYS', 20)
            if hold_days > max_hold:
                time_stop_price = round(price * 0.99, 3)  # 次日开盘即卖（现价×99%保护性触发）
                _log_price_check(code, name, "时间止损", time_stop_price, price)
                orders.append({
                    "类型": "时间止损(超期强制离场)",
                    "优先级": "★★★必挂",
                    "证券代码": code,
                    "证券名称": name,
                    "方向": "卖出",
                    "触发价": time_stop_price,
                    "触发时间": "09:30",
                    "数量": qty,
                    "有效期": "1个交易日",
                    "说明": f"持仓{hold_days}天 > 上限{max_hold}天，浮盈{pnl_pct:.1f}%<5%，时间止损强制离场",
                })
        except (ValueError, TypeError):
            pass  # buy_date格式异常时跳过

    # ================================================================
    # 第二张条件单: 多头趋势加一张回落止盈，空头加一张减仓单
    # ================================================================
    if is_bullish and pnl_pct > 5:
        # 多头趋势: 回落卖出止盈单（保护利润）
        drawdown_price = round(recent_high * (1 - DRAWDOWN_FROM_HIGH), 3)
        # 去重检测: 与止损单触发价差距<2%则跳过
        if abs(drawdown_price - stop_price) / stop_price * 100 >= 2.0:
            _log_price_check(code, name, "止盈", drawdown_price, price)
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
                "说明": f"多头持有，从最高点{recent_high:.3f}回落5%({drawdown_price:.3f})锁定利润",
            })
    elif is_bearish:
        # 空头趋势(非深度): 减仓单
        # BUG-102修复: 零股保护，reduce_qty不超过实际持仓
        reduce_qty = min(max(qty // 2 // 100 * 100, 100), qty)
        reduce_price = round(price * 0.998, 3)
        # 去重检测
        if abs(reduce_price - stop_price) / stop_price * 100 >= 2.0:
            _log_price_check(code, name, "减仓", reduce_price, price)
            orders.append({
                "类型": "强制减仓",
                "优先级": "★★★必挂",
                "证券代码": code,
                "证券名称": name,
                "方向": "卖出",
                "触发价": reduce_price,
                "触发时间": "14:50",
                "数量": reduce_qty,
                "有效期": "5个交易日",
                "说明": f"空头趋势(价格<MA20)，浮盈{pnl_pct:.1f}%，14:50减仓{reduce_qty}股，禁止补仓",
            })

    return orders


def _log_price_check(code, name, order_type, trigger_price, current_price):
    """价格校验日志输出"""
    if order_type in ("止损", "清仓", "减仓"):
        if trigger_price < current_price:
            print(f"  [价格校验] {code} {name} {order_type}{trigger_price:.3f} < 现价{current_price:.3f} ✓")
        else:
            print(f"  [价格校验] {code} {name} {order_type}{trigger_price:.3f} ≥ 现价{current_price:.3f} ✗(已调整)")
    elif order_type in ("止盈", "回落止盈"):
        print(f"  [价格校验] {code} {name} {order_type}{trigger_price:.3f} 现价{current_price:.3f} ✓")


# ============================================================
# 持仓健康度巡检（四级分类 + 处置方案）
# ============================================================
def health_inspection(holdings_with_price, all_data, _playbook=None):
    """
    持仓风险四级巡检，返回按紧急程度排序的结果
    等级: 危险 > 预警 > 关注 > 健康

    V10.1: 传入_playbook实现regime-aware仓位上限
    """
    gate = UnifiedRiskEngine(total_capital=TOTAL_CAPITAL)  # FIX B4: 统一风控引擎
    # 构建 UnifiedRiskEngine 需要的 holdings dict
    holdings_dict = {}
    for h in holdings_with_price:
        code = h["code"]
        df = all_data.get(code)
        ma20 = df["ma20"].iloc[-1] if df is not None and not pd.isna(df["ma20"].iloc[-1]) else h["price"]
        ma60 = df["ma60"].iloc[-1] if df is not None and not pd.isna(df["ma60"].iloc[-1]) else h["price"]
        stock_type = "etf" if _is_etf_code(code) else "stock"
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
    results = gate.inspect_holdings(holdings_dict, _playbook=_playbook)

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
def generate_discipline_lock(_playbook=None):
    """生成今日操作纪律（禁止事项）+ V10 regime附加纪律"""
    base = [
        f"🚫 明日最多交易{MAX_DAILY_TRADES}笔，超过即锁仓不动",
        "🚫 禁止在盘中临时决定买入/卖出，所有操作必须在前一晚通过条件单设定",
        "🚫 禁止越跌越补（单只当日最多补仓1次，且必须通过条件单触发）",
        "🚫 禁止追涨杀跌：看到涨了想追、跌了想割，都是情绪，不是信号",
        "🚫 禁止满仓操作：任何时刻现金比例不得低于10%",
        "✅ 唯一允许的手动操作：条件单触发后确认成交",
        "✅ 如果手痒想操作：先等10分钟，问自己'这是系统信号还是情绪？'",
    ]
    # V10: regime附加纪律
    if _playbook is not None:
        base.extend(_playbook.discipline_extra())
    return base


# ============================================================
# 主流程
# ============================================================
print("=" * 60)
print(f"  每日条件单生成器 V1.0 | {today} {now}")
print(f"  目标: 生成{tomorrow}操作计划，消除盘中人为干预")
print("=" * 60)

# V10: 加载RegimePlaybook（regime-aware条件单）
_playbook = None
try:
    from trading_system.strategy.regime_playbook import RegimePlaybook
    from trading_system.strategy.market_regime import detect_market_regime
    _regime_result = detect_market_regime()
    _playbook = RegimePlaybook(_regime_result)
    print(f"\n[Regime] V10 市场状态={_playbook.state}，置信度={_playbook.confidence:.0%}，"
          f"行动模式={_playbook.action_mode_cn()}")
    print(f"  止损幅度={_playbook.risk_rules()['stop_loss_pct']:.0%}，"
          f"仓位上限={_playbook.position_limits()['max_total']:.0%}")
except Exception as _e:
    print(f"\n[Regime] V10 playbook加载失败，使用默认配置: {_e}")

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
health_results = health_inspection(holdings_with_price, all_data, _playbook=_playbook)
for r in health_results:
    icon = {"危险": "🔴", "预警": "🟠", "关注": "🟡", "健康": "🟢"}.get(r["level"], "⚪")
    print(f"  {icon} {r['name']}({r['code']}): {r['level']} | 浮盈{r['pnl_pct']:+.1f}% | {r['action']}")
    if r["over_limit"]:
        print(f"     ⚠️ 仓位{r['position_ratio']:.1f}%超标，建议减仓{r['reduce_shares']}股")

# 5. 生成条件单
# FIX P0-D / FIX(P0-07): 熔断状态约束——原调用不存在的 check_all()（幽灵 API），
# AttributeError 被 except 吞掉后打印"继续正常生成"→ _circuit_breaker_active 恒 False，
# 制造"熔断已生效"的虚假安全感。改调真实 API（state_mgr.is_paused + 连亏/日亏/年熔断组合），
# 异常时 fail-safe 按熔断处理（仅过滤买入类，卖出/止损单不受影响）。
_circuit_breaker_active = False
try:
    from trading_system.risk.risk_control import UnifiedRiskEngine
    _risk_engine = UnifiedRiskEngine()
    _sm = _risk_engine.state_mgr
    _st = _sm.state
    _rc_cfg = getattr(config, "RISK_UNIFIED_CONFIG", {}) or {}
    _triggered = []
    # 暂停期
    _paused, _pause_reason = _sm.is_paused()
    if _paused:
        _triggered.append(_pause_reason or "暂停开仓")
    # 年度回撤熔断
    if _st.get("annual_halt"):
        _triggered.append("年度回撤熔断(annual_halt)")
    # 连亏熔断
    _consec = int(_st.get("consecutive_losses", 0) or 0)
    _consec_limit = int(_rc_cfg.get("consecutive_loss_block", 3))
    if _consec >= _consec_limit:
        _triggered.append(f"连续亏损{_consec}笔≥{_consec_limit}")
    # 日亏熔断（总资产快照差分口径，阀值 2%）
    _tc = float(_st.get("total_capital", 0) or 0)
    _daily_pnl = float(_st.get("daily_pnl", 0) or 0)
    _daily_loss_limit = float(getattr(config, "DAILY_LOSS_LIMIT", 0.02) or 0.02)
    if _tc > 0 and _daily_pnl < 0 and abs(_daily_pnl / _tc) >= _daily_loss_limit:
        _triggered.append(f"当日亏损{abs(_daily_pnl/_tc)*100:.1f}%≥{_daily_loss_limit*100:.0f}%")
    if _triggered:
        _circuit_breaker_active = True
        print(f"\n[风控] ⛔ 熔断状态生效: {', '.join(_triggered)}，禁止生成加仓/买入类条件单")
    else:
        print(f"\n[风控] ✅ 无熔断状态，正常生成条件单")
except Exception as _e:
    # FIX(P0-07): fail-safe —— 风控状态读取异常时按熔断处理（过滤买入类），
    # 绝不"继续正常生成"制造虚假安全感。卖出/止损单不受影响（下方仅过滤买入）。
    _circuit_breaker_active = True
    print(f"\n[风控] ⚠️ 熔断检查异常: {_e}，fail-safe 按熔断处理（禁生成买入类条件单，卖出不受影响）")

print(f"\n[条件单] 生成次日操作计划...")
all_orders = []
for h in holdings_list:
    code = h["code"]
    # FIX P3: 跳过已清仓持仓（shares=0），避免生成0股无效条件单
    if h["数量"] <= 0:
        continue
    if code in all_data:
        rt_price = quotes.get(code, {}).get("price", 0)
        orders = generate_condition_orders(h, all_data[code], rt_price, _playbook=_playbook)
        # FIX P0-D: 熔断时过滤掉买入类条件单
        if _circuit_breaker_active:
            _filtered = [o for o in orders if o.get("方向") != "买入"]
            all_orders.extend(_filtered)
            _buy_count = len(orders) - len(_filtered)
            if _buy_count > 0:
                print(f"  {h['名称']}: {len(_filtered)}条条件单(已过滤{_buy_count}条买入类)")
            else:
                print(f"  {h['名称']}: {len(_filtered)}条条件单")
        else:
            all_orders.extend(orders)
            print(f"  {h['名称']}: {len(orders)}条条件单")

# 5.1 V9.0(P0-4): 仓位硬约束 — 单只持仓>MAX_SINGLE_POSITION自动生成减仓条件单
_position_orders = []
_existing_sell_codes = {o["证券代码"] for o in all_orders if o.get("方向") == "卖出"}
for r in health_results:
    if r.get("reduce_plan") and r["reduce_plan"]["total_reduce"] > 0:
        rp = r["reduce_plan"]
        code = r["code"]
        if code in _existing_sell_codes:
            continue  # 已有卖出单，不重复生成
        _position_orders.append({
            "类型": f"仓位硬约束减仓(>{MAX_SINGLE_POSITION*100:.0f}%)",
            "优先级": "★★★必挂",
            "证券代码": code,
            "证券名称": r["name"],
            "方向": "卖出",
            "触发价": rp["batch1"]["price"],
            "触发时间": "09:30开盘",
            "数量": rp["batch1"]["shares"],
            "有效期": "3个交易日",
            "说明": f"仓位{r['position_ratio']:.1f}%超限(>{MAX_SINGLE_POSITION*100:.0f}%)，减仓{rp['batch1']['shares']}股至{rp['target_ratio']:.1f}%",
        })
if _position_orders:
    all_orders.extend(_position_orders)
    print(f"\n[仓位约束] ✅ 自动生成{len(_position_orders)}条减仓条件单(单只>{MAX_SINGLE_POSITION*100:.0f}%)")
    for po in _position_orders:
        print(f"  {po['证券名称']}: 减仓{po['数量']}股@{po['触发价']:.3f}")

# 5.2 V10(P0-C): ActionPlanner集成 —— 生成调仓建议清单
try:
    from trading_system.strategy.action_planner import ActionPlanner
    # 构建持仓字典（ActionPlanner需要的格式）
    _holdings_dict = {}
    for h in holdings_list:
        _holdings_dict[h["code"]] = {
            "name": h["名称"],
            "shares": h["数量"],
            "buy_price": h.get("成本价", 0),
            "sector": h.get("赛道", ""),
            "market_value": h.get("市值", 0),
        }
    # 板块景气数据（如果有）
    _sector_result = {}
    try:
        from trading_system.strategy.sector_flow import SectorMonitor
        _sector_mon = SectorMonitor()
        _sector_result = _sector_mon.analyze() or {}
    except Exception:
        pass

    _planner = ActionPlanner(
        regime_result=_regime_result if '_regime_result' in dir() else {},
        sector_result=_sector_result,
        holdings=_holdings_dict,
        all_data=all_data,
        total_capital=TOTAL_CAPITAL,
    )
    _action_plan = _planner.generate_action_plan()
    _reduce_list = _action_plan.get("reduce_list", [])
    _add_list = _action_plan.get("add_list", [])
    _switch_list = _action_plan.get("switch_list", [])

    if _reduce_list or _add_list or _switch_list:
        print(f"\n[ActionPlanner] ✅ 调仓建议: 减仓{_reduce_list}只 / 加仓{_add_list}只 / 换仓{_switch_list}对")
        for r in _reduce_list[:3]:
            print(f"  ⬇️ 减仓: {r.get('name', r.get('code'))} | {r.get('reason', '')}")
        for a in _add_list[:3]:
            print(f"  ⬆️ 加仓: {a.get('name', a.get('code'))} | {a.get('reason', '')}")
        for s in _switch_list[:2]:
            print(f"  🔄 换仓: {s.get('sell_name')}→{s.get('buy_name')} | {s.get('reason', '')}")
except ImportError:
    print(f"\n[ActionPlanner] 模块未找到，跳过调仓建议")
except Exception as _e:
    print(f"\n[ActionPlanner] 调仓建议生成失败: {_e}")

# 6. 纪律锁
discipline = generate_discipline_lock(_playbook=_playbook)

# V10: BEAR模式非防御板块持仓自动减仓建议单
if _playbook is not None and _playbook.state == "BEAR":
    _bear_reduce_count = 0
    for h in holdings_with_price:
        if h["数量"] <= 0:
            continue
        _sector = h.get("赛道", "")
        if not _playbook.is_defensive_sector(_sector):
            _reduce_qty = max(h["数量"] // 2 // 100 * 100, 100)
            _reduce_qty = min(_reduce_qty, h["数量"])
            all_orders.append({
                "类型": "板块弱势减仓(BEAR模式)",
                "优先级": "★★建议",
                "证券代码": h["code"],
                "证券名称": h["名称"],
                "方向": "卖出",
                "触发价": round(h["price"] * 0.998, 3),
                "触发时间": "14:50",
                "数量": _reduce_qty,
                "有效期": "3个交易日",
                "说明": f"熊市模式，{_sector}板块非防御型，建议减仓{_reduce_qty}股，转防御板块或现金",
            })
            _bear_reduce_count += 1
    if _bear_reduce_count:
        print(f"\n[BEAR模式] ✅ 自动生成{_bear_reduce_count}条非防御板块减仓建议单")

# 5.2 V10(Phase3): 板块弱势持仓识别 —— 持仓所在板块排名后30%时标注板块弱势警示
_sector_rank_map = {}  # {sector_name: rank_pct} 板块排名百分位(0=最强, 1=最弱)
try:
    from trading_system.strategy.sector_flow import SectorMonitor, get_stock_sector_map
    _sm = SectorMonitor()
    _sm_data = {}
    _sm_info = {}
    _sector_map = get_stock_sector_map()
    for h in holdings_with_price:
        _c = h["code"]
        if _c in all_data and all_data[_c] is not None and len(all_data[_c]) >= 20:
            _sm_data[_c] = all_data[_c]
            _sm_info[_c] = {"name": h.get("名称", _c)}
    if _sm_data:
        _sm_result = _sm.analyze(_sm_data, _sm_info)
        _ranked = _sm_result.get("ranked", [])
        _n_ranked = len(_ranked)
        if _n_ranked > 0:
            for _idx, _m in enumerate(_ranked):
                _sector_rank_map[_m["sector"]] = _idx / max(_n_ranked - 1, 1)
            _weak_threshold = 0.7  # 后30%为弱势
            _weak_holdings = []
            for h in holdings_with_price:
                if h["数量"] <= 0:
                    continue
                _sec = _sector_map.get(h["code"], h.get("赛道", "其他"))
                _rank_pct = _sector_rank_map.get(_sec, 0.5)
                if _rank_pct >= _weak_threshold:
                    _weak_holdings.append({
                        "code": h["code"], "name": h["名称"],
                        "sector": _sec, "rank_pct": _rank_pct,
                    })
            if _weak_holdings:
                print(f"\n[板块弱势] ⚠️ {len(_weak_holdings)}只持仓所在板块排名后30%:")
                for wh in _weak_holdings:
                    print(f"  ⚠️ {wh['name']}({wh['code']}) 板块={wh['sector']} 排名后{100-wh['rank_pct']*100:.0f}%")
                # 为板块弱势持仓的现有条件单追加板块弱势警示标注
                _sector_weak_codes = {wh["code"] for wh in _weak_holdings}
                for order in all_orders:
                    if order.get("证券代码") in _sector_weak_codes and order.get("方向") == "卖出":
                        order["说明"] += " | ⚠️板块弱势(排名后30%)"
                # 对没有现有卖出单的板块弱势持仓，生成提示性减仓建议
                _existing_sell = {o["证券代码"] for o in all_orders if o.get("方向") == "卖出"}
                for wh in _weak_holdings:
                    if wh["code"] not in _existing_sell:
                        _h = next((h for h in holdings_with_price if h["code"] == wh["code"]), None)
                        if _h and _h["数量"] > 0:
                            _reduce_qty = max(_h["数量"] // 3 // 100 * 100, 100)
                            _reduce_qty = min(_reduce_qty, _h["数量"])
                            all_orders.append({
                                "类型": "板块弱势减仓提示",
                                "优先级": "★★建议",
                                "证券代码": wh["code"],
                                "证券名称": wh["name"],
                                "方向": "卖出",
                                "触发价": round(_h["price"] * 0.998, 3),
                                "触发时间": "14:50",
                                "数量": _reduce_qty,
                                "有效期": "3个交易日",
                                "说明": f"板块{wh['sector']}排名后{(1-wh['rank_pct'])*100:.0f}%，建议减仓{_reduce_qty}股(约1/3)，关注板块轮动方向",
                            })
                print(f"  板块弱势减仓建议单: {len([o for o in all_orders if o.get('类型')=='板块弱势减仓提示'])}条")
        else:
            print(f"\n[板块弱势] 板块排名数据为空，跳过")
    else:
        print(f"\n[板块弱势] K线数据不足，跳过")
except Exception as _swe:
    print(f"\n[板块弱势] 板块弱势持仓识别异常(不影响其他条件单): {_swe}")

# 5.5 V4.0(G6): 组合风控再平衡actions并入条件单闭环
# （解决"组合风控只会算不会做"：卖出类动作转条件单进QMT，买入类仅建议不自动转单）
rebalance_plan = None
rebalance_converted = 0
try:
    from strategy.portfolio_risk import PortfolioRiskManager
    _holdings_dict = {
        h["code"]: {
            "shares": h["数量"],
            "buy_price": h["成本"],
            "current_price": h["price"],
            "sector": h["赛道"],
        }
        for h in holdings_with_price if h["数量"] > 0
    }
    if _holdings_dict:
        prm = PortfolioRiskManager(all_data, _holdings_dict)
        rebalance_plan = prm.calc_rebalance_plan()
        _rb_actions = rebalance_plan.get("actions", []) if isinstance(rebalance_plan, dict) else []
        _sell_actions = [a for a in _rb_actions if a.get("action") in ("减仓", "清仓")]
        _buy_actions = [a for a in _rb_actions if a.get("action") == "加仓"]
        # 已有卖出类条件单(止损/清仓优先)的标的不重复转单
        _existing_sell_codes = {o["证券代码"] for o in all_orders if o["方向"] == "卖出"}
        for a in _sell_actions:
            code = a["code"]
            if code in _existing_sell_codes:
                continue
            _h = next((h for h in holdings_with_price if h["code"] == code), None)
            if not _h or _h["price"] <= 0:
                continue
            _shares = min(int(a.get("shares", 0)), _h["数量"])
            if _shares < 100:
                continue
            _trigger = round(_h["price"] * 0.99, 3)  # 限价卖出：现价下方1%，避免滑点扩大
            all_orders.append({
                "类型": f"组合风控再平衡({a['action']})",
                "优先级": "★★建议",
                "证券代码": code,
                "证券名称": a.get("name", code),
                "方向": "卖出",
                "触发价": _trigger,
                "触发时间": "盘中实时",
                "数量": _shares,
                "有效期": "3个交易日",
                "说明": f"组合风控再平衡: {a.get('reason', '')}，约回笼{a.get('amount', 0)/10000:.1f}万元",
                "from_rebalance": True,
            })
            rebalance_converted += 1
            print(f"  [再平衡→条件单] {a.get('name', code)} {a['action']}{_shares}股 @{_trigger:.3f}")
        if rebalance_converted or _buy_actions:
            _buy_note = f"，买入建议{len(_buy_actions)}条（不自动转单，避免绕过选股信号纪律）" if _buy_actions else ""
            print(f"\n[组合风控] 再平衡动作: 卖出类已转条件单{rebalance_converted}条{_buy_note}")
except Exception as e:
    print(f"\n[组合风控] 再平衡动作并入异常(不影响基础条件单): {e}")

# 5.6 V9.0(P1-1): 风险平价再平衡 — 激活position/risk_parity.py接入生产链路
_risk_parity_orders = []
try:
    from position.risk_parity import risk_parity_weights
    from position.rebalance import RebalanceTrigger
    # 计算当前权重
    _total_mv = sum(h["市值"] for h in holdings_with_price if h["市值"] > 0)
    _current_weights = {}
    _vols = {}
    for h in holdings_with_price:
        if h["市值"] > 0 and _total_mv > 0:
            _current_weights[h["code"]] = h["市值"] / _total_mv
            # 用近20日波动率近似
            _df = all_data.get(h["code"])
            if _df is not None and len(_df) >= 20:
                _ret = _df["close"].pct_change().tail(20)
                _vols[h["code"]] = float(_ret.std() * (252 ** 0.5))
    if _vols and _current_weights:
        _target_weights = risk_parity_weights(_vols, max_weight=MAX_SINGLE_POSITION)
        _trigger = RebalanceTrigger(threshold=0.05)
        if _trigger.should_rebalance(_current_weights, _target_weights):
            _rp_orders = _trigger.calc_rebalance_orders(
                _current_weights, _target_weights, _total_mv,
                {h["code"]: h["price"] for h in holdings_with_price}
            )
            _rp_sell_codes = {o["证券代码"] for o in all_orders if o.get("方向") == "卖出"}
            for ro in _rp_orders:
                if ro["action"] == "sell" and ro["code"] not in _rp_sell_codes:
                    _h = next((h for h in holdings_with_price if h["code"] == ro["code"]), None)
                    if _h and _h["price"] > 0:
                        _risk_parity_orders.append({
                            "类型": "风险平价再平衡",
                            "优先级": "★★建议",
                            "证券代码": ro["code"],
                            "证券名称": _h["名称"],
                            "方向": "卖出",
                            "触发价": round(_h["price"] * 0.995, 3),
                            "触发时间": "盘中实时",
                            "数量": ro["shares"],
                            "有效期": "3个交易日",
                            "说明": f"风险平价: 当前{ro.get('drift',0)*100:+.1f}%偏离，减仓{ro['shares']}股",
                        })
            if _risk_parity_orders:
                all_orders.extend(_risk_parity_orders)
                print(f"\n[风险平价] ✅ 生成{len(_risk_parity_orders)}条再平衡条件单")
                for rpo in _risk_parity_orders:
                    print(f"  {rpo['证券名称']}: 减仓{rpo['数量']}股@{rpo['触发价']:.3f}")
            else:
                print(f"\n[风险平价] 目标权重偏离已触发，但卖出单已存在，不重复生成")
        else:
            print(f"\n[风险平价] 当前权重偏离在阈值内，无需再平衡")
except Exception as e:
    print(f"\n[风险平价] 再平衡计算异常(不影响其他条件单): {e}")

# 6.5 V4.0(G4): 条件单下单前风控硬校验（三级仓位上限+现金底线）
_positions_snapshot = {
    h["code"]: {
        "market_value": h["市值"],
        "sector": h["赛道"],
        "is_etf": _is_etf_code(h["code"]),
    }
    for h in holdings_with_price
}
blocked_orders = pre_trade_check_orders(all_orders, _positions_snapshot, TOTAL_CAPITAL)
if blocked_orders:
    print(f"\n[风控] ⛔ {len(blocked_orders)}条买入条件单被pre-trade校验拦截:")
    for b in blocked_orders:
        print(f"  {b['code']} {b['name']}: {b['reason']}")
else:
    print(f"\n[风控] ✅ pre-trade校验通过，无超限买入单")

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
gate = UnifiedRiskEngine(total_capital=TOTAL_CAPITAL)  # FIX B4: 统一风控引擎
total_mv = sum(h["市值"] for h in holdings_with_price)
cash_ratio = (TOTAL_CAPITAL - total_mv) / TOTAL_CAPITAL * 100
html += f'<div class="alert alert-info">总仓位: {total_mv/TOTAL_CAPITAL*100:.1f}% | 现金: {cash_ratio:.1f}% | '
html += f'ETF上限{RISK_CONFIG["etf_max_ratio"]*100:.0f}% | 个股上限{RISK_CONFIG["stock_max_ratio"]*100:.0f}% | '
html += f'赛道上限{RISK_CONFIG["sector_max_ratio"]*100:.0f}%</div>'

# V4.0(G4): pre-trade拦截警示区
if blocked_orders:
    html += '<div class="alert alert-danger"><b>⛔ 下单前风控拦截（以下买入单禁止录入APP/QMT）:</b><br>'
    for b in blocked_orders:
        html += f'• {b["code"]} {b["name"]}: {b["reason"]}<br>'
    html += '</div>'

# V4.0(G6): 组合风控再平衡动作区块
if isinstance(rebalance_plan, dict) and rebalance_plan.get("actions"):
    html += '<h2>⚖️ 组合风控再平衡动作</h2>'
    html += '<table><tr><th>方向</th><th>代码</th><th>名称</th><th>股数</th><th>金额</th><th>原因</th><th>执行状态</th></tr>'
    _converted_codes = {o["证券代码"] for o in all_orders if o.get("from_rebalance")}
    for a in rebalance_plan["actions"]:
        _is_sell = a.get("action") in ("减仓", "清仓")
        if _is_sell and a["code"] in _converted_codes:
            _status = '<b style="color:#1976d2">已转条件单</b>'
        elif _is_sell:
            _status = '已有卖出单/不满足转单条件'
        else:
            _status = '仅建议（不自动转单）'
        _color = "#e53935" if _is_sell else "#4caf50"
        html += (f'<tr><td style="color:{_color}"><b>{a.get("action", "")}</b></td>'
                 f'<td>{a.get("code", "")}</td><td>{a.get("name", "")}</td>'
                 f'<td>{a.get("shares", 0):,}</td><td>{a.get("amount", 0):,.0f}</td>'
                 f'<td style="font-size:11px">{a.get("reason", "")}</td><td>{_status}</td></tr>')
    html += '</table>'
    html += ('<div class="alert alert-info">卖出类再平衡动作已转为条件单并入下方清单与QMT执行文件；'
             '买入类仅展示建议，由选股信号驱动，避免绕过入场纪律。</div>')

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
<b>{order['类型']}</b> | {order['证券代码']} {order['证券名称']} | <b>{order['方向']}</b>"""
    # V4.0(G4): 被pre-trade拦截的订单卡片内红色警示
    if order.get("blocked"):
        html += f'<div class="alert alert-danger">⛔ 已被风控拦截: {order.get("block_reason", "")} —— 请勿录入此单</div>'
    html += f"""
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
# FIX(P2-06): 文件名=执行日（次日）——系统约定 orders_YYYYMMDD.json 的 YYYYMMDD 为执行日
# （对照 scheduler.py 正确实现），原用 today 会导致晚间计划送不到次日执行器。
_exec_date = datetime.date.today() + datetime.timedelta(days=1)
exec_date_str = _exec_date.strftime("%Y%m%d")
orders_json = {
    "date": tomorrow,
    "generated_at": f"{today} {now}",
    "total_capital": TOTAL_CAPITAL,
    "max_daily_trades": MAX_DAILY_TRADES,
    "orders": [],
}
for idx, order in enumerate(all_orders, 1):
    # V4.0(G4): 被pre-trade拦截的买入单不进入QMT执行JSON，防止超限成交
    if order.get("blocked"):
        continue
    orders_json["orders"].append({
        "order_id": f"ORD_{exec_date_str}_{idx:03d}",
        # V4.2(P0-3): 生命周期初始态，盘后由 execution/order_lifecycle.reconcile_daily_orders
        # 回写为 已成交/未触发，补齐G4/G6闭环最后一环
        "status": "已生成",
        **order,
    })

json_path = os.path.join(output_dir, f"orders_{exec_date_str}.json")
# FIX(P2-06): 合并写入——原 open(w) 截断会抹掉同日已追加的盘中止损单（对照 scheduler）。
# 保留已有 orders，按 (证券代码,方向,触发价) 去重后追加新计划单。
_existing_orders = []
if os.path.exists(json_path):
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            _existing_data = json.load(f)
        _existing_orders = _existing_data.get("orders", []) if isinstance(_existing_data, dict) else []
    except Exception as _e:
        print(f"[保存] ⚠️ 读取已有执行文件失败，将仅写新计划单: {_e}")


def _order_key(o):
    return (o.get("证券代码"), o.get("方向"), o.get("触发价"))


_seen_keys = {_order_key(o) for o in _existing_orders}
_merged_orders = list(_existing_orders)
_added = 0
for o in orders_json["orders"]:
    if _order_key(o) not in _seen_keys:
        _merged_orders.append(o)
        _seen_keys.add(_order_key(o))
        _added += 1
orders_json["orders"] = _merged_orders
try:
    from utils.file_io import atomic_json_write
    if not atomic_json_write(json_path, orders_json):
        raise IOError("atomic_json_write 返回 False")
except Exception:
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(orders_json, f, ensure_ascii=False, indent=2)
print(f"[保存] QMT执行文件: {json_path}（保留已有{len(_existing_orders)}条，新增{_added}条，合计{len(_merged_orders)}条）")

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
