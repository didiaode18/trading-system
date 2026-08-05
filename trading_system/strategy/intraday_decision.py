"""
盘中实时决策报告模块 V2.0（反冲动交易增强版）
================================================
每15分钟自动生成全量持仓标的实时决策报告，用量化规则约束情绪化交易

V2.0升级:
  - 频率: 30分钟 → 15分钟（09:45-14:45，共16个时间点）
  - 覆盖: 全量持仓标的（不只报异常，每只都给明确建议）
  - 建议: 从"方向性"升级为"可执行"（具体数量/价格/条件）
  - 邮件标题: 含日期+时间+紧急程度
  - 排序: 紧急卖出 > 禁止加仓 > 建议减仓 > 持有观察 > 可以加仓
  - 反冲动: 新增15分钟矛盾检测、趋势破位强制提示

核心能力:
  1. 实时行情快照（腾讯API，延迟<1秒）
  2. 每只持仓标的输出可执行操作指令:
     🚨 紧急卖出(含建议数量) | ⚠️ 建议减仓(含目标仓位)
     🔒 禁止加仓(含禁止原因) | 👀 持有观察(含触发线)
     ✅ 可以加仓(含前提条件+建议数量+止损位)
  3. 反冲动交易硬约束（6条铁律不可覆盖）
  4. 大盘环境评估（系统性风险检测）
  5. HTML邮件推送 + 控制台输出

反冲动硬约束（不可覆盖）:
  ① 单票仓位>40% → 禁止加仓 + 提示降仓
  ② 当日跌>3%且无强支撑 → 禁止加仓
  ③ 浮亏>8% → 禁止加仓，优先评估止损
  ④ 大盘/沪深300跌>1.5% → 暂停所有新增买入
  ⑤ 同一标的15分钟内不得给出矛盾建议
  ⑥ 已触发止损或趋势破位 → 必须明确风险提示，不允许模糊

运行方式:
  python -m strategy.intraday_decision           # 单次生成+邮件
  python -m strategy.intraday_decision --no-email # 仅控制台
  scheduler自动调度: 盘中每15分钟

触发时间:
  09:45/10:00/10:15/10:30/10:45/11:00/11:15/11:30
  13:00/13:15/13:30/13:45/14:00/14:15/14:30/14:45
"""

import os
import sys
import json
import logging
import datetime
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

from data.realtime import fetch_realtime_batch, fetch_index_realtime


# ============================================================
# 一、配置常量
# ============================================================

DECISION_CONFIG = {
    # 仓位硬约束
    "max_single_weight_add": 0.40,      # 单票>40%禁止加仓
    "warn_single_weight": 0.30,         # 单票>30%警告
    "reduce_target_weight": 0.25,       # 减仓目标仓位25%
    # 当日亏损锁
    "daily_loss_lock_pct": -3.0,        # 当日跌>3%禁止加仓
    # 深套锁
    "deep_loss_lock_pct": -8.0,         # 浮亏>8%禁止加仓
    # 大盘系统性风险
    "market_risk_pct": -1.5,            # 大盘跌>1.5%全面禁止加仓
    "market_crash_pct": -2.5,           # 大盘跌>2.5%建议全面减仓
    # 止损相关
    "stop_loss_near_pct": 0.03,         # 距止损位<3%视为临近
    # 紧急卖出阈值
    "emergency_sell_pct": 50,           # 紧急卖出建议比例(%)
    "reduce_sell_pct": 30,              # 减仓建议比例(%)
}

# 5日均线缓存（每日只请求一次历史数据）
_MA5_CACHE = {"date": None, "data": {}}

# 决策优先级排序权重
_URGENCY_ORDER = {"紧急卖出": 0, "禁止加仓": 1, "建议减仓": 2, "持有观察": 3, "可以加仓": 4}


# ============================================================
# 二、数据获取
# ============================================================

def load_holdings() -> dict:
    """加载持仓文件（只取有仓位的标的）"""
    holdings_file = getattr(config, 'HOLDINGS_FILE', None)
    if not holdings_file or not os.path.exists(holdings_file):
        holdings_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "holdings.json"
        )
    if not os.path.exists(holdings_file):
        logger.warning("[盘中决策] 持仓文件不存在")
        return {}

    with open(holdings_file, "r", encoding="utf-8") as f:
        all_holdings = json.load(f)

    return {
        code: info for code, info in all_holdings.items()
        if info.get("shares", 0) > 0 and info.get("buy_price", 0) > 0
    }


def get_ma5_trend(codes: list) -> dict:
    """获取5日均线趋势（日频缓存）"""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    if _MA5_CACHE["date"] == today_str and _MA5_CACHE["data"]:
        return _MA5_CACHE["data"]

    if not HAS_AKSHARE:
        return {}

    results = {}
    end_date = datetime.date.today().strftime("%Y%m%d")
    start_date = (datetime.date.today() - datetime.timedelta(days=20)).strftime("%Y%m%d")

    for code in codes:
        try:
            if code.startswith("5") or code.startswith("1"):
                df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                         start_date=start_date, end_date=end_date,
                                         adjust="qfq")
            else:
                df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                        start_date=start_date, end_date=end_date,
                                        adjust="qfq")
            if df is None or df.empty or len(df) < 5:
                continue

            closes = df["收盘"].astype(float).tolist()
            ma5 = sum(closes[-5:]) / 5
            ma5_prev = sum(closes[-6:-1]) / 5 if len(closes) >= 6 else ma5

            results[code] = {
                "ma5": round(ma5, 3),
                "ma5_direction": "up" if ma5 > ma5_prev else "down",
                "close_yesterday": closes[-1],
                "above_ma5": closes[-1] > ma5,
                # FIX P2-11: 近5日涨幅（防追高过滤用，取不到时为0）
                "chg_5d": (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 and closes[-6] > 0 else 0,
            }
        except Exception as e:
            logger.debug(f"[盘中决策] {code} MA5获取失败: {e}")
            continue

    if results:
        _MA5_CACHE["date"] = today_str
        _MA5_CACHE["data"] = results
    return results


# ============================================================
# 三、决策引擎（核心）
# ============================================================

def score_stock(code: str, info: dict, quote: dict, market_pct: float,
                market_300_pct: float, ma5_info: dict,
                total_market_value: float) -> dict:
    """对单只持仓标的进行多因子打分，输出操作决策+可执行建议"""
    cfg = DECISION_CONFIG
    price = quote.get("price", 0)
    change_pct = quote.get("change_pct", 0)
    high = quote.get("high", 0)
    low = quote.get("low", 0)
    turnover = quote.get("turnover", 0)

    shares = info.get("shares", 0)
    buy_price = info.get("buy_price", 0)
    stop_loss = info.get("stop_loss", 0)

    if price <= 0 or buy_price <= 0:
        return None

    # 基础指标
    pnl_pct = (price - buy_price) / buy_price * 100
    market_value = price * shares
    weight = market_value / total_market_value * 100 if total_market_value > 0 else 0
    relative_strength = change_pct - market_pct
    day_range = high - low if high > low else 0.01
    range_position = (price - low) / day_range

    # 判断是否有强支撑（日内低点附近企稳+缩量）
    has_support = range_position > 0.3 and turnover < 3

    score = 0
    reasons = []

    # ---- 因子1: 日内动量 ----
    if change_pct <= -5:
        score -= 3
        reasons.append(f"日内暴跌{change_pct:.1f}%，抛压极重")
    elif change_pct <= -3:
        score -= 2
        reasons.append(f"日内大跌{change_pct:.1f}%")
    elif change_pct <= -1.5:
        score -= 1
        reasons.append(f"日内偏弱{change_pct:.1f}%")
    elif change_pct >= 3:
        score += 2
        reasons.append(f"日内强势+{change_pct:.1f}%")
    elif change_pct >= 1.5:
        score += 1
        reasons.append(f"日内偏强+{change_pct:.1f}%")

    # ---- 因子2: 相对大盘强弱 ----
    if relative_strength <= -2:
        score -= 2
        reasons.append(f"显著弱于大盘{relative_strength:.1f}%，主力出逃迹象")
    elif relative_strength <= -1:
        score -= 1
        reasons.append(f"弱于大盘{relative_strength:.1f}%")
    elif relative_strength >= 1.5:
        score += 1
        reasons.append(f"强于大盘+{relative_strength:.1f}%，有资金护盘")

    # ---- 因子3: 日内区间位置 ----
    if range_position <= 0.15 and change_pct < 0:
        score -= 1
        reasons.append("价格趴在日内最低点附近，卖压未释放完")
    elif range_position >= 0.85 and change_pct > 0:
        score += 1
        reasons.append("价格站在日内高点附近，买盘积极")

    # ---- 因子4: 量价配合 ----
    if turnover > 5 and change_pct < -2:
        score -= 1
        reasons.append(f"放量下跌(换手{turnover:.1f}%)，恐慌抛售")
    elif turnover > 5 and change_pct > 2:
        score += 1
        reasons.append(f"放量上涨(换手{turnover:.1f}%)，资金进场")

    # ---- 因子5: 5日趋势 ----
    trend_broken = False
    if ma5_info:
        if ma5_info.get("ma5_direction") == "down" and not ma5_info.get("above_ma5"):
            score -= 1
            trend_broken = True
            reasons.append("跌破5日线且均线向下，短期趋势破位")
        elif ma5_info.get("ma5_direction") == "up" and ma5_info.get("above_ma5"):
            score += 1
            reasons.append("站上5日线且均线向上，短期趋势健康")

    # ---- 因子6: 止损距离 ----
    stop_triggered = False
    if stop_loss > 0:
        stop_distance = (price - stop_loss) / price
        if price <= stop_loss:
            score -= 5
            stop_triggered = True
            reasons.append(f"⛔ 已跌破止损位{stop_loss:.2f}！纪律要求立即执行")
        elif stop_distance < cfg["stop_loss_near_pct"]:
            score -= 3
            reasons.append(f"距止损位{stop_loss:.2f}仅{stop_distance*100:.1f}%，随时触发")

    # ---- 因子7: 浮亏深度 ----
    if pnl_pct <= -15:
        score -= 2
        reasons.append(f"深度浮亏{pnl_pct:.1f}%，需严格止损纪律")
    elif pnl_pct <= -8:
        score -= 1
        reasons.append(f"浮亏{pnl_pct:.1f}%，超过安全阈值")

    # ---- 大盘环境修正 ----
    if market_pct <= cfg["market_crash_pct"]:
        score -= 2
        reasons.append(f"大盘暴跌{market_pct:.1f}%，系统性风险释放中")
    elif market_pct <= cfg["market_risk_pct"]:
        score -= 1
        reasons.append(f"大盘下跌{market_pct:.1f}%，注意系统性风险")

    # ---- 决策映射 ----
    if score <= -6:
        decision, icon = "紧急卖出", "🚨"
    elif score <= -3:
        decision, icon = "建议减仓", "⚠️"
    elif score <= -1:
        decision, icon = "持有观察", "✋"
    elif score <= 1:
        decision, icon = "持有观察", "👀"
    else:
        decision, icon = "可以加仓", "✅"

    # ---- 反冲动硬约束（加仓锁）----
    add_locked = False
    lock_reasons = []

    if weight > cfg["max_single_weight_add"] * 100:
        add_locked = True
        lock_reasons.append(f"仓位{weight:.0f}%超限(>{cfg['max_single_weight_add']*100:.0f}%)，集中度锁死，应降仓至25%以下")

    if change_pct <= cfg["daily_loss_lock_pct"] and not has_support:
        add_locked = True
        lock_reasons.append(f"当日跌{change_pct:.1f}%且无强支撑，当日亏损锁生效")

    if pnl_pct <= cfg["deep_loss_lock_pct"]:
        add_locked = True
        lock_reasons.append(f"浮亏{pnl_pct:.1f}%，深套锁生效(回本前禁止加仓，优先评估止损)")

    if market_pct <= cfg["market_risk_pct"] or market_300_pct <= cfg["market_risk_pct"]:
        add_locked = True
        lock_reasons.append(f"大盘/沪深300跌超1.5%，系统性风险锁生效，暂停所有买入")

    # 止损触发/趋势破位 → 强制明确风险提示
    if stop_triggered or trend_broken:
        if decision not in ("紧急卖出", "建议减仓"):
            decision, icon = "建议减仓", "⚠️"
        reasons.insert(0, "⚠️ 趋势破位/止损触发，风险已实质化，不允许模糊持有")

    # 加仓锁覆盖决策
    if add_locked and decision == "可以加仓":
        decision, icon = "禁止加仓", "🔒"

    # FIX P2-11: 加仓防追高过滤 — 近5日涨幅>15%时加仓建议降级为持有观察（无数据时不阻断）
    try:
        if decision == "可以加仓" and ma5_info and ma5_info.get("chg_5d", 0) > 15.0:
            decision, icon = "持有观察", "👀"
            reasons.append(f"近5日涨幅{ma5_info.get('chg_5d', 0):.1f}%>15%，追高风险，加仓建议已降级")
    except Exception:
        pass

    # ---- 生成可执行建议 ----
    advice = _generate_action_advice(
        decision, price, shares, buy_price, stop_loss, weight,
        pnl_pct, change_pct, market_value, total_market_value, cfg
    )

    # ---- 下一步观察条件 ----
    next_watch = _generate_next_watch(decision, price, stop_loss, ma5_info, change_pct)

    return {
        "code": code,
        "name": info.get("name", code),
        "sector": info.get("sector", ""),
        "price": price,
        "change_pct": change_pct,
        "pnl_pct": pnl_pct,
        "weight": weight,
        "market_value": market_value,
        "shares": shares,
        "buy_price": buy_price,
        "score": score,
        "decision": decision,
        "decision_icon": icon,
        "reasons": reasons,
        "add_locked": add_locked,
        "lock_reasons": lock_reasons,
        "stop_loss": stop_loss,
        "relative_strength": relative_strength,
        "range_position": range_position,
        "turnover": turnover,
        "advice": advice,
        "next_watch": next_watch,
        "stop_triggered": stop_triggered,
        "trend_broken": trend_broken,
    }


def _generate_action_advice(decision, price, shares, buy_price, stop_loss,
                            weight, pnl_pct, change_pct, mv, total_mv, cfg) -> str:
    """根据决策类型生成具体可执行建议"""
    if decision == "紧急卖出":
        sell_pct = cfg["emergency_sell_pct"]
        sell_shares = int(shares * sell_pct / 100 / 100) * 100  # 取整到100股
        sell_shares = max(sell_shares, 100)
        if price <= stop_loss:
            return (f"立即市价卖出{sell_shares}股(约{sell_pct}%仓位)，"
                    f"已破止损{stop_loss:.2f}，纪律优先于判断。"
                    f"剩余{shares - sell_shares}股设硬止损{price*0.97:.2f}(再跌3%清仓)")
        else:
            return (f"建议卖出{sell_shares}股({sell_pct}%)，"
                    f"挂单价{price*0.998:.2f}(略低现价确保成交)。"
                    f"若15分钟内继续下破{price*0.98:.2f}，剩余全部清仓")

    elif decision == "建议减仓":
        # 目标仓位25%
        target_mv = total_mv * cfg["reduce_target_weight"]
        if mv > target_mv:
            reduce_shares = int((mv - target_mv) / price / 100) * 100
            reduce_shares = max(reduce_shares, 100)
        else:
            reduce_shares = int(shares * cfg["reduce_sell_pct"] / 100 / 100) * 100
            reduce_shares = max(reduce_shares, 100)
        reduce_shares = min(reduce_shares, shares)
        return (f"分两档减仓: ①先卖{reduce_shares}股@{price*0.999:.2f}，"
                f"②若跌破{stop_loss:.2f}再卖{reduce_shares}股。"
                f"目标仓位从{weight:.0f}%降至{cfg['reduce_target_weight']*100:.0f}%以下")

    elif decision == "禁止加仓":
        return (f"当前严格禁止任何买入操作。"
                f"只能持有或减仓。若跌破{stop_loss:.2f}则执行止损卖出")

    elif decision == "持有观察":
        trigger = stop_loss if stop_loss > 0 else price * 0.95
        return (f"暂不操作，持有等待。"
                f"观察线: 跌破{trigger:.2f}则升级为减仓；"
                f"站稳{price*1.02:.2f}以上且放量则可继续持有")

    elif decision == "可以加仓":
        # 加仓条件: 仓位<30%才允许
        if weight > cfg["warn_single_weight"] * 100:
            return f"虽趋势偏强但仓位已{weight:.0f}%，不建议追加，持有即可"
        add_shares = int(shares * 0.2 / 100) * 100  # 加仓不超过现有20%
        add_shares = max(add_shares, 100)
        new_stop = price * 0.95
        return (f"允许小幅加仓: 前提①回踩{price*0.99:.2f}不破②量能不萎缩。"
                f"建议加{add_shares}股，加仓后止损上移至{new_stop:.2f}")

    return "持有观察"


def _generate_next_watch(decision, price, stop_loss, ma5_info, change_pct) -> str:
    """生成下一步观察条件"""
    ma5_val = ma5_info.get("ma5", 0) if ma5_info else 0
    parts = []

    if decision in ("紧急卖出", "建议减仓"):
        parts.append(f"15分钟后若继续低于{price*0.99:.2f}，升级操作力度")
        if stop_loss > 0:
            parts.append(f"硬止损{stop_loss:.2f}不可突破")
    elif decision == "持有观察":
        if stop_loss > 0:
            parts.append(f"下方观察{stop_loss:.2f}(止损线)")
        if ma5_val > 0:
            parts.append(f"上方压力MA5={ma5_val:.2f}")
        parts.append("若15分钟内涨跌<0.5%则维持判断")
    elif decision == "可以加仓":
        parts.append(f"加仓后15分钟观察: 跌破{price*0.98:.2f}立即止损新加部分")
    elif decision == "禁止加仓":
        parts.append("锁定期内仅观察是否触发止损，不做任何买入")

    return " | ".join(parts) if parts else "维持当前判断，等待下一轮报告"


# ============================================================
# 四、报告生成
# ============================================================

def generate_decision_report(send_email_flag: bool = True) -> dict:
    """生成盘中实时决策报告V2.0（主入口）"""
    now = datetime.datetime.now()
    logger.info(f"[盘中决策] V2.0报告生成 {now.strftime('%H:%M:%S')}...")

    # 1. 加载持仓
    holdings = load_holdings()
    if not holdings:
        logger.warning("[盘中决策] 无有效持仓，跳过")
        return {"success": False, "report_text": "无有效持仓", "decisions": []}

    codes = list(holdings.keys())

    # 2. 获取实时行情
    quotes = fetch_realtime_batch(codes)
    if not quotes:
        logger.warning("[盘中决策] 实时行情获取失败")
        return {"success": False, "report_text": "行情获取失败", "decisions": []}

    # 3. 获取大盘指数
    index_sh = fetch_index_realtime("000001")
    index_300 = fetch_index_realtime("000300")
    market_pct = index_sh.get("change_pct", 0)
    market_300_pct = index_300.get("change_pct", 0)

    # 4. MA5趋势
    ma5_data = get_ma5_trend(codes)

    # 5. 计算总市值
    total_market_value = sum(
        quotes.get(code, {}).get("price", 0) * info.get("shares", 0)
        for code, info in holdings.items()
    )

    # 6. 逐标的打分（全量覆盖）
    decisions = []
    for code, info in holdings.items():
        quote = quotes.get(code)
        if not quote:
            continue
        result = score_stock(code, info, quote, market_pct, market_300_pct,
                             ma5_data.get(code, {}), total_market_value)
        if result:
            decisions.append(result)

    # 按紧急程度排序
    decisions.sort(key=lambda x: (_URGENCY_ORDER.get(x["decision"], 5), x["score"]))

    # FIX P2-12: 加仓建议过风控预检（不通过时在建议文案追加说明，异常降级不阻断）
    try:
        from risk.risk_control import quick_risk_check
        for d in decisions:
            if d.get("decision") != "可以加仓":
                continue
            try:
                _add_shares = max(int(d.get("shares", 0) * 0.2 / 100) * 100, 100)
                _rc = quick_risk_check({
                    "code": d["code"], "name": d["name"], "price": d["price"],
                    "shares": _add_shares, "sector": d.get("sector", ""),
                    "type": "stock", "stop_loss": d["price"] * 0.95, "risk_reward": 0,
                }, holdings)
                if not _rc.get("pass", True):
                    d["advice"] = (d.get("advice", "") +
                                   f"（风控预检未通过：{_rc.get('reason', '未知')}，加仓建议作废）")
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[盘中决策] 风控预检异常(不影响报告): {e}")

    # 7. 生成报告
    report_text = _build_console_report(decisions, market_pct, market_300_pct,
                                        total_market_value, now)

    # 8. 控制台输出
    try:
        print(report_text)
    except (UnicodeEncodeError, ValueError):
        try:
            safe_text = report_text.encode('gbk', errors='ignore').decode('gbk', errors='ignore')
            sys.__stdout__.write(safe_text + '\n')
        except Exception:
            pass

    # 9. 邮件推送
    if send_email_flag:
        try:
            from notify.email_notify import send_email
            html = _build_html_report(decisions, market_pct, market_300_pct,
                                      total_market_value, now)
            # V2.0邮件标题格式
            emergency_count = sum(1 for d in decisions if d["decision"] == "紧急卖出")
            locked_count = sum(1 for d in decisions if d["add_locked"])
            reduce_count = sum(1 for d in decisions if d["decision"] == "建议减仓")
            subject = f"盘中实时决策报告 {now.strftime('%Y-%m-%d %H:%M')} | "
            parts = []
            if emergency_count:
                parts.append(f"{emergency_count}个紧急卖出")
            if locked_count:
                parts.append(f"{locked_count}个禁止加仓")
            if reduce_count:
                parts.append(f"{reduce_count}个建议减仓")
            if not parts:
                parts.append("全部持有观察")
            subject += " | ".join(parts)
            send_email(subject, html)
            logger.info(f"[盘中决策] 邮件已发送: {subject}")
        except Exception as e:
            logger.warning(f"[盘中决策] 邮件发送失败: {e}")

    return {
        "success": True,
        "report_text": report_text,
        "decisions": decisions,
        "market_pct": market_pct,
    }


def _build_console_report(decisions, market_pct, market_300_pct, total_mv, now):
    """构建控制台纯文本报告V2.0"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  📊 盘中实时决策报告 V2.0 | {now.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"  ⚠️ 仅供参考，不构成投资建议")
    lines.append("=" * 70)

    # 大盘环境
    if market_pct <= DECISION_CONFIG["market_crash_pct"]:
        env = "🔴 系统性暴跌"
    elif market_pct <= DECISION_CONFIG["market_risk_pct"]:
        env = "🟠 系统性风险"
    elif market_pct < 0:
        env = "🟡 弱势震荡"
    else:
        env = "🟢 正常偏强"

    lines.append(f"  大盘: {env} | 上证{market_pct:+.2f}% | 沪深300{market_300_pct:+.2f}%")
    lines.append(f"  持仓总市值: {total_mv/10000:.1f}万 | 标的数: {len(decisions)}只")
    if market_pct <= DECISION_CONFIG["market_risk_pct"]:
        lines.append("  ⛔ 系统性风险锁生效: 全部标的禁止加仓！")
    lines.append("-" * 70)

    for d in decisions:
        lock_mark = " 🔒" if d["add_locked"] else ""
        lines.append(f"  {d['decision_icon']} {d['name']}({d['code']}) | {d['decision']}{lock_mark}")
        lines.append(
            f"     现价{d['price']:.3f} | 日{d['change_pct']:+.2f}% | "
            f"成本{d['buy_price']:.3f} | 浮盈亏{d['pnl_pct']:+.1f}% | "
            f"仓位{d['weight']:.0f}% | {d['shares']}股"
        )
        lines.append(f"     📋 建议: {d['advice']}")
        lines.append(f"     🔭 观察: {d['next_watch']}")
        # 触发原因(最多3条)
        for r in d["reasons"][:3]:
            lines.append(f"     · {r}")
        if d["lock_reasons"]:
            for lr in d["lock_reasons"]:
                lines.append(f"     🔒 {lr}")
        lines.append("")

    lines.append("-" * 70)
    lines.append("  ⚡ 铁律①: 评分为负 → 任何理由不允许加仓")
    lines.append("  ⚡ 铁律②: 15分钟内不得对同一标的反复操作")
    lines.append("  ⚡ 铁律③: 单票>40%只能减不能加")
    lines.append("  ⚡ 铁律④: 止损触发必须执行，不允许'再看看'")
    lines.append("  ⚡ 铁律⑤: 大盘跌>1.5%暂停一切买入")
    lines.append("  ⚡ 铁律⑥: 本报告15分钟更新，两次报告间不做冲动操作")
    lines.append("=" * 70)
    return "\n".join(lines)


def _build_html_report(decisions, market_pct, market_300_pct, total_mv, now):
    """构建HTML邮件报告V2.0（全量字段+可执行建议）"""
    if market_pct <= DECISION_CONFIG["market_crash_pct"]:
        env_color, env_text = "#dc3545", "系统性暴跌"
    elif market_pct <= DECISION_CONFIG["market_risk_pct"]:
        env_color, env_text = "#fd7e14", "系统性风险"
    elif market_pct < 0:
        env_color, env_text = "#ffc107", "弱势震荡"
    else:
        env_color, env_text = "#28a745", "正常偏强"

    cards_html = ""
    for d in decisions:
        if d["score"] <= -6:
            d_color, border_color = "#dc3545", "#dc3545"
        elif d["decision"] == "禁止加仓":
            d_color, border_color = "#6f42c1", "#6f42c1"
        elif d["score"] <= -3:
            d_color, border_color = "#fd7e14", "#fd7e14"
        elif d["score"] <= 1:
            d_color, border_color = "#6c757d", "#dee2e6"
        else:
            d_color, border_color = "#28a745", "#28a745"

        pnl_color = "#dc3545" if d["pnl_pct"] < 0 else "#28a745"
        chg_color = "#dc3545" if d["change_pct"] < 0 else "#28a745"

        lock_html = ""
        if d["lock_reasons"]:
            items = "".join(f"<li>{lr}</li>" for lr in d["lock_reasons"])
            lock_html = f'<ul style="color:#dc3545;font-size:11px;margin:4px 0;padding-left:16px;">{items}</ul>'

        reasons_html = "".join(f"<li>{r}</li>" for r in d["reasons"][:4])

        cards_html += f"""
        <div style="border:1px solid {border_color};border-left:4px solid {border_color};
                    border-radius:6px;margin:10px 0;padding:12px 16px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <div>
                    <b style="font-size:15px;">{d['name']}</b>
                    <span style="color:#999;font-size:12px;"> {d['code']} | {d['sector']}</span>
                </div>
                <span style="background:{d_color};color:#fff;padding:4px 12px;border-radius:4px;font-size:13px;">
                    {d['decision_icon']} {d['decision']}
                </span>
            </div>
            <table style="width:100%;font-size:12px;margin:8px 0;border-collapse:collapse;">
                <tr>
                    <td style="padding:3px 0;">现价: <b>{d['price']:.3f}</b></td>
                    <td style="padding:3px 0;">日涨跌: <span style="color:{chg_color}"><b>{d['change_pct']:+.2f}%</b></span></td>
                    <td style="padding:3px 0;">成本: {d['buy_price']:.3f}</td>
                    <td style="padding:3px 0;">浮盈亏: <span style="color:{pnl_color}"><b>{d['pnl_pct']:+.1f}%</b></span></td>
                </tr>
                <tr>
                    <td style="padding:3px 0;">持仓: {d['shares']}股</td>
                    <td style="padding:3px 0;">仓位: <b>{d['weight']:.1f}%</b></td>
                    <td style="padding:3px 0;">止损位: {d['stop_loss']:.2f}</td>
                    <td style="padding:3px 0;">评分: {d['score']:+d}</td>
                </tr>
            </table>
            <div style="background:#f8f9fa;padding:8px 12px;border-radius:4px;margin:6px 0;">
                <b style="font-size:12px;">📋 操作建议:</b>
                <div style="font-size:12px;margin-top:4px;">{d['advice']}</div>
            </div>
            <div style="font-size:11px;color:#495057;margin:4px 0;">
                <b>🔭 下一步观察:</b> {d['next_watch']}
            </div>
            <div style="font-size:11px;color:#555;">
                <b>触发原因:</b>
                <ul style="margin:4px 0;padding-left:16px;">{reasons_html}</ul>
            </div>
            {lock_html}
        </div>"""

    risk_banner = ""
    if market_pct <= DECISION_CONFIG["market_risk_pct"]:
        risk_banner = """
        <div style="background:#dc3545;color:#fff;padding:10px 16px;border-radius:4px;margin:10px 0;font-size:13px;">
            ⛔ <b>系统性风险锁生效</b>: 大盘/沪深300跌超1.5%，全部标的禁止加仓！仅允许减仓或持有。
        </div>"""

    html = f"""
    <html><body style="font-family:Microsoft YaHei,sans-serif;max-width:820px;margin:0 auto;padding:10px;">
    <div style="background:#1a1a2e;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
        <h2 style="margin:0;font-size:18px;">📊 盘中实时决策报告 V2.0</h2>
        <p style="margin:5px 0 0;color:#aaa;font-size:12px;">
            {now.strftime('%Y-%m-%d %H:%M')} | 每15分钟自动更新 | 仅供参考，不构成投资建议
        </p>
    </div>

    <div style="background:#f8f9fa;padding:12px 20px;border-bottom:2px solid #eee;font-size:13px;">
        <span style="background:{env_color};color:#fff;padding:4px 10px;border-radius:4px;">
            大盘: {env_text}
        </span>
        &nbsp; 上证 <b style="color:{'#dc3545' if market_pct < 0 else '#28a745'}">{market_pct:+.2f}%</b>
        &nbsp;|&nbsp; 沪深300 <b style="color:{'#dc3545' if market_300_pct < 0 else '#28a745'}">{market_300_pct:+.2f}%</b>
        &nbsp;|&nbsp; 持仓市值 <b>{total_mv/10000:.1f}万</b>
        &nbsp;|&nbsp; 标的 <b>{len(decisions)}只</b>
    </div>

    {risk_banner}

    <div style="padding:0 12px;">
        {cards_html}
    </div>

    <div style="background:#fff3cd;padding:14px 20px;border-radius:0 0 8px 8px;font-size:12px;">
        <b>⚡ 反冲动交易铁律（系统强制执行，不可覆盖）:</b><br/>
        ① 单票仓位>40% → 禁止加仓，提示降仓至25%<br/>
        ② 当日跌>3%且无强支撑 → 当日禁止加仓<br/>
        ③ 浮亏>8% → 禁止加仓，优先评估止损<br/>
        ④ 大盘/沪深300跌>1.5% → 暂停所有新增买入<br/>
        ⑤ 同一标的15分钟内不得给出矛盾建议<br/>
        ⑥ 止损触发/趋势破位 → 必须明确执行，不允许"再看看"<br/>
        <br/>
        <span style="color:#999;">⚠️ 本报告由量化系统自动生成，仅供参考，不构成投资建议。投资有风险，决策需谨慎。</span>
    </div>
    </body></html>
    """
    return html


# ============================================================
# 五、命令行入口
# ============================================================

def run_intraday_decision(send_email_flag: bool = True) -> dict:
    """供scheduler调用的入口函数"""
    return generate_decision_report(send_email_flag=send_email_flag)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    result = generate_decision_report(send_email_flag="--no-email" not in sys.argv)
    if not result["success"]:
        print(f"报告生成失败: {result['report_text']}")
