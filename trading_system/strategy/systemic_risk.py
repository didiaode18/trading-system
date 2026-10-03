"""V27: 大盘系统性风险提前预警模块

解决: M因子/MarketRegime判定"下降趋势"滞后10+日的问题
四层防线:
  L1: MA斜率加速度检测 (领先5-8日, 盘后/选股时运行, 五级评分0-4)
  L2: 盘中大盘跌幅硬止损 (零延迟, 盘中10秒轮询)
  L3: 市场宽度收窄速率 (领先3-5日, 统一预警扫描时运行)
  L4: 资金流外流检测   (辅助确认: 北向资金+融资余额)

分级输出:
  🟡 warm  → 风险升温: 日志+钉钉提醒
  🟠 ready → 准备减仓: 钉钉+邮件+生成减仓条件单
  🔴 force → 强制减仓: 紧急钉钉+邮件+立即生成减仓条件单

多信号共振升级:
  L1(score>=2)+L3 → 至少🟠  |  L1+L3+L4 → 🔴  |  L2+L1(score>=2) → 🔴

调用方式:
  # 盘后/选股时（有日线数据）
  result = scan_systemic_risk_daily(index_df, breadth_now=35.0)

  # 盘中（有实时行情）
  result = scan_systemic_risk_intraday(market_quote, holdings, total_capital)

  # 宽度历史持久化（每日盘后调用）
  save_breadth_today(38.5)  # 当日收盘后记录宽度
"""

import datetime
import json
import logging
import os

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# 状态缓存（进程内持久化，避免重复触发）
# ============================================================
_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "systemic_risk_state.json"
)
_BREADTH_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "breadth_history.json"
)

def _load_state() -> dict:
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"date": "", "level_history": [], "last_alert_level": "", "reduce_orders_generated": 0}

def _save_state(state: dict):
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[系统性风险] 状态保存失败: {e}")

def _get_today_state() -> dict:
    state = _load_state()
    today = datetime.date.today().isoformat()
    if state.get("date") != today:
        state = {"date": today, "level_history": [], "last_alert_level": "",
                 "reduce_orders_generated": 0,
                 "l1_consecutive_high_days": 0, "l1_last_date": ""}
        _save_state(state)
    return state


def _track_l1_consecutive_days(l1_score: int) -> int:
    """跟踪L1连续score>=2的天数（用于连续确认机制）

    规则:
      - score>=2: 连续计数器+1
      - score<2:  连续计数器重置为0
      - 返回: 当前连续天数
    """
    state = _load_state()
    today = datetime.date.today().isoformat()
    last_date = state.get("l1_last_date", "")
    consecutive = state.get("l1_consecutive_high_days", 0)

    if last_date == today:
        # 今天已更新过，直接返回当前值
        return consecutive

    # 检查是否昨天（连续）
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    if l1_score >= 2:
        if last_date == yesterday:
            consecutive += 1
        else:
            consecutive = 1  # 重新开始计数
    else:
        consecutive = 0

    state["l1_consecutive_high_days"] = consecutive
    state["l1_last_date"] = today
    _save_state(state)
    return consecutive


# ============================================================
# 宽度历史持久化（每日盘后记录，供L3使用）
# ============================================================
def save_breadth_today(breadth_pct: float):
    """每日收盘后记录市场宽度，自动维护最近20个交易日历史

    参数: breadth_pct — 当日个股在MA20上方的比例 (0-100)
    """
    history = load_breadth_history_list()
    today = datetime.date.today().isoformat()
    # 幂等: 同一天只记录一次（取最后一次覆盖）
    if history and history[-1].get("date") == today:
        history[-1]["breadth"] = round(breadth_pct, 1)
    else:
        history.append({"date": today, "breadth": round(breadth_pct, 1)})
    # 只保留最近20个交易日
    history = history[-20:]
    try:
        os.makedirs(os.path.dirname(_BREADTH_HISTORY_FILE), exist_ok=True)
        with open(_BREADTH_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[系统性风险] 宽度历史保存失败: {e}")


def load_breadth_history_list() -> list:
    """加载宽度历史列表 [{date, breadth}, ...]"""
    try:
        if os.path.exists(_BREADTH_HISTORY_FILE):
            with open(_BREADTH_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def get_breadth_recent_values(n: int = 5) -> list:
    """获取最近n个交易日的宽度数值列表 [oldest, ..., newest]"""
    history = load_breadth_history_list()
    if not history:
        return []
    return [h["breadth"] for h in history[-n:]]


# ============================================================
# L1: MA斜率加速度检测（领先5-8日）
# ============================================================
def check_ma_acceleration(index_df: pd.DataFrame) -> dict:
    """检测MA20斜率的加速度（二阶导数），五级评分体系

    原理: MA20斜率从正转平/从平转负 → 领先close跌破MA20约5日

    五级评分 (score 0-4):
      score=0: 正常 — 斜率正向且加速
      score=1: 警惕 — 斜率正向但减速（上涨动能衰减）
      score=2: 准备减仓 — 斜率转平/微负（加速度<-0.05%）
      score=3: 高风险 — 斜率负向且加速（加速度<-0.10%）
      score=4: 极高风险 — 斜率陡峭下行（加速度<-0.15%且斜率<-0.1%）

    返回:
        {
            "triggered": bool,     # score>=2 为True
            "level": "warm"/"ready"/"force"/None,  # 映射到预警级别
            "score": int,          # 0-4评分
            "accel": float,        # 加速度值（负=减速下行）
            "slope_now": float,    # 当前MA20斜率(%)
            "slope_5d_ago": float, # 5日前MA20斜率(%)
            "detail": str
        }
    """
    import config
    if not getattr(config, 'SYSTEMIC_RISK_MA_ACCEL_ENABLED', True):
        return {"triggered": False, "level": None, "score": 0, "detail": "未启用"}

    if index_df is None or len(index_df) < 30:
        return {"triggered": False, "level": None, "score": 0, "detail": "数据不足"}

    close = index_df["close"].values
    window = getattr(config, 'SYSTEMIC_RISK_MA_ACCEL_WINDOW', 5)

    # 计算MA20序列
    ma20_series = pd.Series(close).rolling(20).mean()
    if pd.isna(ma20_series.iloc[-1]) or pd.isna(ma20_series.iloc[-(window+1)]):
        return {"triggered": False, "level": None, "score": 0, "detail": "MA20数据不足"}

    # 当前斜率 = (MA20_today - MA20_Nd_ago) / MA20_Nd_ago * 100
    slope_now = (ma20_series.iloc[-1] - ma20_series.iloc[-(window+1)]) / ma20_series.iloc[-(window+1)] * 100

    # 5日前斜率
    lookback = window + 5
    if len(ma20_series) < lookback + 1 or pd.isna(ma20_series.iloc[-(lookback+1)]):
        return {"triggered": False, "level": None, "score": 0, "detail": "回望数据不足"}

    slope_5d_ago = (ma20_series.iloc[-(window+1)] - ma20_series.iloc[-(lookback+1)]) / ma20_series.iloc[-(lookback+1)] * 100

    # 加速度 = 斜率变化
    accel = slope_now - slope_5d_ago

    # ---- 五级评分 ----
    if accel >= 0:
        score = 0  # 正常: 斜率正向且加速
    elif accel >= -0.05:
        score = 1  # 警惕: 斜率正向但减速
    elif accel >= -0.10:
        score = 2  # 准备减仓: 斜率走平/微负
    elif accel >= -0.15 or slope_now >= -0.1:
        score = 3  # 高风险: 斜率负向加速
    else:
        score = 4  # 极高风险: 斜率陡峭下行

    # 评分→预警级别映射
    level = None
    if score >= 4:
        level = "force"   # 🔴 强制减仓
    elif score >= 2:
        level = "ready"   # 🟠 准备减仓
    elif score >= 1:
        level = "warm"    # 🟡 风险升温

    triggered = score >= 2
    score_labels = {0: "正常", 1: "警惕", 2: "准备减仓", 3: "高风险", 4: "极高风险"}
    detail = (f"MA20斜率: {slope_now:+.3f}% → 5日前{slope_5d_ago:+.3f}% | "
              f"加速度={accel:+.3f}% | score={score}({score_labels[score]})")

    return {
        "triggered": triggered,
        "level": level,
        "score": score,
        "accel": round(accel, 4),
        "slope_now": round(slope_now, 4),
        "slope_5d_ago": round(slope_5d_ago, 4),
        "detail": detail,
    }


# ============================================================
# L2: 盘中大盘跌幅硬止损（零延迟）
# ============================================================
def check_intraday_market_drop(market_quote: dict) -> dict:
    """盘中实时检测沪深300日内跌幅，触发分级减仓信号

    参数:
        market_quote: {"change_pct": float, "name": str, ...}

    返回:
        {
            "triggered": bool,
            "level": "warm"/"ready"/"force"/None,
            "change_pct": float,
            "detail": str
        }
    """
    import config
    if not getattr(config, 'SYSTEMIC_RISK_INTRADAY_ENABLED', True):
        return {"triggered": False, "level": None, "detail": "未启用"}

    if not market_quote:
        return {"triggered": False, "level": None, "detail": "无行情数据"}

    change_pct = market_quote.get("change_pct", 0)
    warning_pct = getattr(config, 'SYSTEMIC_RISK_INTRADAY_WARNING_PCT', -1.5)
    critical_pct = getattr(config, 'SYSTEMIC_RISK_INTRADAY_CRITICAL_PCT', -2.5)

    level = None
    if change_pct <= critical_pct:
        level = "force"   # 🔴 强制减仓
    elif change_pct <= warning_pct:
        level = "ready"   # 🟠 准备减仓

    triggered = level is not None
    detail = f"沪深300日内跌幅{change_pct:+.2f}% (预警线{warning_pct}%/临界线{critical_pct}%)"

    return {
        "triggered": triggered,
        "level": level,
        "change_pct": round(change_pct, 2),
        "detail": detail,
    }


# ============================================================
# L3: 市场宽度收窄速率检测（领先3-5日）
# ============================================================
def check_breadth_narrowing(breadth_now: float, breadth_history: list = None) -> dict:
    """检测市场宽度(个股在MA20上方比例)的快速收窄

    参数:
        breadth_now: 当前宽度百分比 (0-100)
        breadth_history: 近5日宽度列表 [day1, day2, ..., day5]

    返回:
        {
            "triggered": bool,
            "level": "warm"/"ready"/None,
            "breadth_now": float,
            "breadth_5d_change": float,
            "detail": str
        }
    """
    import config
    if not getattr(config, 'SYSTEMIC_RISK_BREADTH_ENABLED', True):
        return {"triggered": False, "level": None, "detail": "未启用"}

    drop_threshold = getattr(config, 'SYSTEMIC_RISK_BREADTH_DROP_THRESHOLD', 15)
    low_threshold = getattr(config, 'SYSTEMIC_RISK_BREADTH_LOW_THRESHOLD', 30)

    if breadth_history is None or len(breadth_history) < 2:
        # 无历史数据，仅检查绝对值
        if breadth_now < low_threshold:
            return {
                "triggered": True, "level": "warm",
                "breadth_now": breadth_now, "breadth_5d_change": 0,
                "detail": f"宽度{breadth_now:.0f}%<{low_threshold}%（无历史数据对比）"
            }
        return {"triggered": False, "level": None, "breadth_now": breadth_now,
                "breadth_5d_change": 0, "detail": f"宽度{breadth_now:.0f}%，历史数据不足"}

    breadth_5d_ago = breadth_history[0]
    breadth_change = breadth_now - breadth_5d_ago

    level = None
    # 条件1: 宽度快速下降（5日内下降>15个百分点）
    if breadth_change <= -drop_threshold:
        level = "ready"  # 🟠 准备减仓
    # 条件2: 宽度已很低且仍在下降
    elif breadth_now < low_threshold and breadth_change < -5:
        level = "warm"   # 🟡 风险升温
    # 条件3: 宽度快速收窄但绝对值尚可
    elif breadth_change <= -(drop_threshold * 0.6):
        level = "warm"   # 🟡 风险升温

    triggered = level is not None
    detail = f"市场宽度: {breadth_now:.0f}% (5日前{breadth_5d_ago:.0f}%, 变化{breadth_change:+.0f}pp)"

    return {
        "triggered": triggered,
        "level": level,
        "breadth_now": round(breadth_now, 1),
        "breadth_5d_change": round(breadth_change, 1),
        "detail": detail,
    }


# ============================================================
# L4: 资金流外流检测
# ============================================================
def check_fund_flow_outflow(fund_flow_data: dict = None) -> dict:
    """检测北向资金连续外流 + 融资余额周降

    参数:
        fund_flow_data: {
            "northbound_daily": [{"date": str, "net_flow": float}, ...],
            "margin_balance": float,          # 当前融资余额(亿)
            "margin_balance_1w_ago": float,   # 1周前融资余额(亿)
        }

    返回:
        {
            "triggered": bool,
            "level": "warm"/None,
            "consecutive_days": int,
            "total_outflow": float,
            "margin_drop_pct": float,   # 融资余额周降百分比
            "detail": str
        }
    """
    import config
    if not getattr(config, 'SYSTEMIC_RISK_FUND_FLOW_ENABLED', True):
        return {"triggered": False, "level": None, "detail": "未启用"}

    details = []
    nb_triggered = False
    margin_triggered = False
    consecutive = 0
    total_outflow = 0.0
    margin_drop_pct = 0.0

    # ---- 北向资金连续净流出 ----
    if fund_flow_data and "northbound_daily" in fund_flow_data:
        nb_data = fund_flow_data["northbound_daily"]
        if nb_data and len(nb_data) >= 2:
            threshold = getattr(config, 'SYSTEMIC_RISK_FUND_FLOW_THRESHOLD', -50)
            required_days = getattr(config, 'SYSTEMIC_RISK_FUND_FLOW_DAYS', 3)

            for item in reversed(nb_data[-10:]):
                net = item.get("net_flow", 0)
                if net < threshold:
                    consecutive += 1
                    total_outflow += net
                else:
                    break

            nb_triggered = consecutive >= required_days
            if nb_triggered:
                details.append(f"北向资金连续{consecutive}日净流出(累计{total_outflow:.0f}亿)")

    # ---- 融资余额周降>3% ----
    if fund_flow_data and "margin_balance" in fund_flow_data:
        mb_now = fund_flow_data.get("margin_balance", 0)
        mb_1w = fund_flow_data.get("margin_balance_1w_ago", 0)
        if mb_now > 0 and mb_1w > 0:
            margin_drop_pct = (mb_now - mb_1w) / mb_1w * 100
            margin_threshold = getattr(config, 'SYSTEMIC_RISK_MARGIN_DROP_THRESHOLD', -3.0)
            if margin_drop_pct <= margin_threshold:
                margin_triggered = True
                details.append(f"融资余额周降{margin_drop_pct:.1f}%(>{margin_threshold}%)")

    triggered = nb_triggered or margin_triggered
    level = "warm" if triggered else None
    detail = " | ".join(details) if details else "资金流正常"

    return {
        "triggered": triggered,
        "level": level,
        "consecutive_days": consecutive,
        "total_outflow": round(total_outflow, 1),
        "margin_drop_pct": round(margin_drop_pct, 2),
        "detail": detail,
    }


# ============================================================
# L5: 波动率突变检测（领先1-3日）
# ============================================================
def check_volatility_spike(index_df: pd.DataFrame) -> dict:
    """检测短期波动率相对长期波动率的放大倍数

    原理: 大跌前波动率通常突然放大（恐慌情绪蔓延）

    参数:
        index_df: 指数日线DataFrame（含close列）

    返回:
        {
            "triggered": bool,
            "level": "warm"/"ready"/None,
            "vol_ratio": float,     # 短期/长期波动率比值
            "vol_short": float,     # 5日波动率(%)
            "vol_long": float,      # 20日波动率(%)
            "detail": str
        }
    """
    import config
    if not getattr(config, 'SYSTEMIC_RISK_VOLATILITY_ENABLED', True):
        return {"triggered": False, "level": None, "detail": "未启用"}

    if index_df is None or len(index_df) < 25:
        return {"triggered": False, "level": None, "detail": "数据不足"}

    close = index_df["close"].values
    # 确保价格为正（过滤异常数据）
    if np.any(close[-21:] <= 0):
        return {"triggered": False, "level": None, "detail": "价格数据异常(含非正值)"}
    returns = np.diff(np.log(close[-21:]))  # 最近20日对数收益率

    if len(returns) < 20:
        return {"triggered": False, "level": None, "detail": "收益率数据不足"}

    vol_short = np.std(returns[-5:]) * np.sqrt(252) * 100   # 5日年化波动率
    vol_long = np.std(returns) * np.sqrt(252) * 100          # 20日年化波动率

    if vol_long < 1e-6:
        return {"triggered": False, "level": None, "detail": "长期波动率过低"}

    ratio = vol_short / vol_long

    # V27.1: 阈值调优（原1.5x/2.0x在回测中0%命中，下调至1.2x/1.8x）
    warm_thresh = getattr(config, 'SYSTEMIC_RISK_VOL_WARM_THRESHOLD', 1.2)
    ready_thresh = getattr(config, 'SYSTEMIC_RISK_VOL_READY_THRESHOLD', 1.8)

    level = None
    if ratio >= ready_thresh:
        level = "ready"   # 波动率大幅放大→准备减仓
    elif ratio >= warm_thresh:
        level = "warm"    # 波动率开始放大→风险升温

    triggered = level is not None
    detail = f"波动率比: {ratio:.2f}x (5日{vol_short:.1f}%/20日{vol_long:.1f}%)"

    return {
        "triggered": triggered,
        "level": level,
        "vol_ratio": round(ratio, 2),
        "vol_short": round(vol_short, 2),
        "vol_long": round(vol_long, 2),
        "detail": detail,
    }


# ============================================================
# 综合扫描入口（盘后/选股时，有日线数据）
# ============================================================
def scan_systemic_risk_daily(index_df: pd.DataFrame, breadth_now: float = 50.0,
                              breadth_history: list = None,
                              fund_flow_data: dict = None,
                              m_factor_state: str = None) -> dict:
    """盘后综合扫描：L1+L3+L4+L5

    自动加载宽度历史: 如未传入breadth_history，自动从breadth_history.json读取

    V27.1改进:
      - L1连续N天确认: score>=2连续N天(默认2天)才升级ready，降误报
      - L5波动率突变: 新增第五层防线
      - M因子联动: m_factor_state="down"时信号升级一级

    参数:
        m_factor_state: M因子当前状态 "up"/"neutral"/"down"/None

    返回:
        {
            "risk_level": "warm"/"ready"/"force"/None,
            "alerts": [...],
            "detail": str,
            "components": {...}
        }
    """
    import config
    # 自动加载宽度历史
    if breadth_history is None:
        breadth_history = get_breadth_recent_values(5)

    alerts = []
    max_level = None
    level_rank = {"warm": 1, "ready": 2, "force": 3}

    def _update_max_level(level):
        nonlocal max_level
        if level and (max_level is None or level_rank.get(level, 0) > level_rank.get(max_level, 0)):
            max_level = level

    # L1: MA斜率加速度
    l1 = check_ma_acceleration(index_df)
    if l1["triggered"]:
        alerts.append({"source": "L1", **l1})
        _update_max_level(l1.get("level"))

    # V27.1: L1连续确认机制 — score>=2需连续N天才维持ready，否则降级为warm
    l1_consecutive = _track_l1_consecutive_days(l1.get("score", 0))
    l1_confirmed = l1_consecutive >= getattr(config, 'SYSTEMIC_RISK_L1_CONSECUTIVE_DAYS', 2)
    if l1.get("level") == "ready" and not l1_confirmed and l1.get("score", 0) < 3:
        # 单日score=2但未连续确认→降级为warm（score>=3直接维持ready）
        if max_level == "ready":
            max_level = "warm"
        l1["detail"] += f" | 连续{l1_consecutive}天(未确认)"
        l1["confirmed"] = False
    else:
        l1["confirmed"] = l1_consecutive >= 1

    # L3: 宽度收窄速率
    l3 = check_breadth_narrowing(breadth_now, breadth_history)
    if l3["triggered"]:
        alerts.append({"source": "L3", **l3})
        _update_max_level(l3.get("level"))

    # L4: 资金流外流
    l4 = check_fund_flow_outflow(fund_flow_data)
    if l4["triggered"]:
        alerts.append({"source": "L4", **l4})
        _update_max_level(l4.get("level"))

    # L5: 波动率突变（V27.1新增）
    l5 = check_volatility_spike(index_df)
    if l5["triggered"]:
        alerts.append({"source": "L5", **l5})
        _update_max_level(l5.get("level"))

    # 多信号共振升级规则（设计文档§六）
    # L1(score>=2)+L3 → 至少🟠
    # L1+L3+L4 → 🔴强制减仓
    # L2(盘中)+L1(score>=2) → 🔴
    l1_score = l1.get("score", 0)
    if max_level == "warm" and len(alerts) >= 2:
        max_level = "ready"  # 双信号共振→至少ready
    if l1_score >= 2 and l3["triggered"]:
        if max_level is None or level_rank.get(max_level, 0) < level_rank.get("ready", 0):
            max_level = "ready"
    if l1["triggered"] and l3["triggered"] and l4["triggered"]:
        max_level = "force"  # 三信号共振→强制减仓
    if l1_score >= 3 and l3["triggered"]:
        max_level = "force"  # L1高风险+宽度收窄→强制减仓

    # V27.1: M因子状态联动增强
    m_boost = False
    if getattr(config, 'SYSTEMIC_RISK_MFACTOR_BOOST', True) and m_factor_state == "down":
        # M因子已确认下降趋势→V27信号升级一级（warm→ready, ready→force）
        if max_level == "warm":
            max_level = "ready"
            m_boost = True
        elif max_level == "ready":
            max_level = "force"
            m_boost = True

    detail_parts = [a["detail"] for a in alerts] if alerts else ["无异常信号"]
    if m_boost:
        detail_parts.append("M因子down联动升级")

    return {
        "risk_level": max_level,
        "alerts": alerts,
        "detail": " | ".join(detail_parts),
        "components": {"L1": l1, "L3": l3, "L4": l4, "L5": l5},
        "l1_consecutive_days": l1_consecutive,
        "m_factor_boost": m_boost,
    }


# ============================================================
# 综合扫描入口（盘中，有实时行情）
# ============================================================
def scan_systemic_risk_intraday(market_quote: dict, holdings: dict = None,
                                 total_capital: float = 0) -> dict:
    """盘中快速扫描：L2（盘中跌幅硬止损）

    返回:
        {
            "risk_level": "warm"/"ready"/"force"/None,
            "alerts": [...],
            "detail": str,
            "reduce_orders": [...]  # 需要生成的减仓条件单
        }
    """
    alerts = []
    max_level = None
    level_rank = {"warm": 1, "ready": 2, "force": 3}
    reduce_orders = []

    # L2: 盘中大盘跌幅
    l2 = check_intraday_market_drop(market_quote)
    if l2["triggered"]:
        alerts.append({"source": "L2", **l2})
        max_level = l2.get("level")

        # 生成减仓条件单
        if max_level in ("ready", "force") and holdings and total_capital > 0:
            reduce_orders = _generate_risk_reduce_orders(
                holdings, total_capital, max_level,
                reason=f"大盘系统性风险({l2['detail']})"
            )

    # 状态更新
    if max_level:
        state = _get_today_state()
        state["level_history"].append({
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "level": max_level,
        })
        state["last_alert_level"] = max_level
        state["reduce_orders_generated"] += len(reduce_orders)
        _save_state(state)

    detail_parts = [a["detail"] for a in alerts] if alerts else ["盘中大盘正常"]

    return {
        "risk_level": max_level,
        "alerts": alerts,
        "detail": " | ".join(detail_parts),
        "reduce_orders": reduce_orders,
    }


# ============================================================
# 减仓条件单生成
# ============================================================
def _generate_risk_reduce_orders(holdings: dict, total_capital: float,
                                  level: str, reason: str = "") -> list:
    """根据风险级别生成减仓条件单

    规则:
      ready(🟠): 每只持仓减持1/3
      force(🔴): 每只持仓减持1/2
    """
    reduce_orders = []
    reduce_ratio = 1/3 if level == "ready" else 1/2

    for code, h in holdings.items():
        shares = h.get("shares", 0)
        name = h.get("name", code)
        if shares <= 0:
            continue

        reduce_shares = int(shares * reduce_ratio / 100) * 100  # 整百取整
        if reduce_shares <= 0:
            reduce_shares = 100  # 至少减100股
        if reduce_shares > shares:
            reduce_shares = shares

        reduce_orders.append({
            "code": code,
            "name": name,
            "action": "sell",
            "quantity": reduce_shares,
            "price_type": "market",  # 市价卖出
            "reason": reason,
            "risk_level": level,
            "original_shares": shares,
            "reduce_pct": round(reduce_shares / shares * 100, 1),
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        logger.warning(f"  [系统性风险减仓] {name}({code}) 减持{reduce_shares}股"
                       f"({reduce_shares/shares*100:.0f}%) 风险级别={level}")

    return reduce_orders


def save_risk_reduce_orders(reduce_orders: list) -> str:
    """将减仓条件单写入当日orders JSON文件

    返回: 文件路径
    """
    if not reduce_orders:
        return ""

    import config
    today = datetime.date.today().strftime("%Y%m%d")
    output_dir = config.OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"orders_{today}.json")

    # 追加到已有文件
    existing_orders = []
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            existing_orders = existing_data.get("orders", [])
        except Exception:
            pass

    orders_json = {
        "date": datetime.date.today().strftime("%Y-%m-%d"),
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "systemic_risk_auto_reduce",
        "total_capital": getattr(config, 'TOTAL_CAPITAL', 0),
        "max_daily_trades": 10,
        "orders": existing_orders + reduce_orders,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(orders_json, f, ensure_ascii=False, indent=2)

    logger.warning(f"  [系统性风险] 减仓条件单已生成: {len(reduce_orders)}条 → {json_path}")
    return json_path
