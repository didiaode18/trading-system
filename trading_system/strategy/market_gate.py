"""
大盘门控与自适应阈值引擎（从 intraday_monitor.py 提取）
======================================================
纯函数接口 —— 无类实例依赖（校准参数通过显式参数传入）。

功能:
  - 大盘门控: 大盘急跌时延迟/压制个股预警
  - 自适应急跌阈值: 根据大盘波动率 regime 动态调整
  - 系统性风险硬止损: 大盘跌幅超阈值零延迟响应
  - 均值回归 Z-Score: ATR+VWAP 检测极端偏离

使用:
    from strategy.market_gate import market_gate_check, get_adaptive_rapid_drop_threshold
"""
import datetime
import logging
import math

logger = logging.getLogger(__name__)


# ============================================================
# 大盘门控
# ============================================================

def market_gate_check(market_quote: dict, prev_gate_state: str) -> str:
    """大盘门控: 根据大盘状态决定个股预警的发送策略

    参数:
        market_quote: 大盘行情 dict（含 change_pct 等）
        prev_gate_state: 上一轮门控状态（"pass"/"delay"/"suppress"）

    返回:
        "pass"     — 正常发送所有预警
        "delay"    — 大盘急跌中，非 critical 预警入队列延迟
        "suppress" — 大盘极端下跌，完全压制个股预警

    设计逻辑:
      大盘急跌(-1%~-2.5%)时，个股普遍跟跌，此时发出的止损/减仓预警
      在大盘企稳后往往迅速失效（价格反弹），导致"卖在最低点"。
      通过门控延迟，等大盘企稳后再补发，显著提高预警有效性。
    """
    if not market_quote:
        return "pass"

    mkt_chg = market_quote.get("change_pct", 0)

    if mkt_chg <= -2.5:
        new_state = "suppress"
    elif mkt_chg < -1.0:
        new_state = "delay"
    else:
        new_state = "pass"

    if new_state != prev_gate_state:
        logger.info(f"[大盘门控] 状态转换: {prev_gate_state}→{new_state} "
                    f"(大盘涨跌{mkt_chg:+.2f}%)")

    return new_state


# ============================================================
# 自适应急跌阈值
# ============================================================

def get_adaptive_rapid_drop_threshold(market_quote: dict,
                                       default_rapid_drop_pct: float = -0.03,
                                       calibration: dict = None) -> float:
    """根据大盘波动率 regime 动态调整急跌阈值

    原理: 高波动环境下频繁触发急跌预警会产生大量误报，
          低波动环境下收紧阈值可提前捕捉风险。

    规则:
      - 高波动（大盘振幅>2% 或 跌幅>2%）: 放宽到 -4.0%
      - 低波动（大盘振幅<0.5% 且 跌幅<0.5%）: 收紧到 -2.5%
      - 正常: 保持 default_rapid_drop_pct（默认 -3.0%）

    参数:
        market_quote: 大盘行情 dict
        default_rapid_drop_pct: 默认急跌阈值（负数，如 -0.03）
        calibration: 校准参数字典（含 threshold_factors 和 observation_mode）

    返回: 急跌阈值（负数百分比，如 -3.0 表示跌3%）
    """
    if not market_quote:
        return default_rapid_drop_pct * 100

    mkt_amplitude = abs(market_quote.get("change_pct", 0))
    mkt_change = market_quote.get("change_pct", 0)

    if mkt_change < -2.0 or mkt_amplitude > 2.0:
        base_threshold = -4.0
    elif mkt_amplitude < 0.5 and abs(mkt_change) < 0.5:
        base_threshold = -2.5
    else:
        base_threshold = default_rapid_drop_pct * 100

    # 校准因子（仅观察期外生效）
    if calibration and not calibration.get("observation_mode", True):
        factor = calibration.get("threshold_factors", {}).get("急跌预警", 1.0)
        base_threshold *= factor

    return base_threshold


# ============================================================
# 系统性风险硬止损
# ============================================================

def check_systemic_risk_alert(market_quote: dict, gate_state: str,
                                cooldown_state: dict = None) -> list:
    """大盘跌幅超阈值时生成系统性风险预警

    与门控联动: 门控进入delay/suppress时自动触发
    冷却: 同级别30分钟内只触发1次

    参数:
        market_quote: 大盘行情 dict
        gate_state: 当前门控状态
        cooldown_state: 冷却状态 dict（会就地修改，传入 _systemic_risk_cooldown）

    返回: 预警 dict 列表
    """
    alerts = []
    if not market_quote:
        return alerts

    mkt_chg = market_quote.get("change_pct", 0)
    now = datetime.datetime.now()

    # 动态导入 config 以避免循环依赖
    try:
        import config
        warning_pct = getattr(config, 'SYSTEMIC_RISK_INTRADAY_WARNING_PCT', -1.5)
        critical_pct = getattr(config, 'SYSTEMIC_RISK_INTRADAY_CRITICAL_PCT', -2.5)
    except Exception:
        warning_pct = -1.5
        critical_pct = -2.5

    if mkt_chg <= critical_pct:
        level = "critical"
        risk_label = "🔴 强制减仓"
        rule_name = "V27-大盘硬止损(>2.5%)"
    elif mkt_chg <= warning_pct:
        level = "high"
        risk_label = "🟠 准备减仓"
        rule_name = "V27-大盘硬止损(>1.5%)"
    else:
        return alerts

    # 冷却检查
    if cooldown_state is None:
        cooldown_state = {}
    cooldown_key = f"systemic_risk_{level}"
    last_alert_time = cooldown_state.get(cooldown_key)
    if last_alert_time and (now - last_alert_time).total_seconds() < 30 * 60:
        return alerts

    cooldown_state[cooldown_key] = now

    msg = (f"{risk_label}: 沪深300日内跌幅{mkt_chg:+.2f}%"
           f" | 预警线{warning_pct}% / 临界线{critical_pct}%"
           f" | 建议立即检查持仓，执行减仓保护")

    alerts.append({
        "type": "systemic_risk_intraday",
        "level": level,
        "code": "000300",
        "name": "大盘系统性风险",
        "message": msg,
        "urgency_score": 95 if level == "critical" else 80,
        "rule_name": rule_name,
        "icon": "🚨",
        "time": now.strftime("%H:%M"),
    })

    logger.warning(f"[盘中预警-{level.upper()}] {msg}")
    return alerts


# ============================================================
# 均值回归 Z-Score
# ============================================================

def is_mean_reversion_zone(current_price: float, vwap: float,
                            atr: float) -> tuple:
    """判断当前价格是否处于均值回归高概率区域

    使用 VWAP + ATR 计算标准化偏离度(Z-Score):
      Z = (价格 - VWAP) / ATR
      |Z| > 2 意味着价格偏离日内均价超过2倍真实波幅，回归概率极高

    返回:
        (is_zone: bool, z_score: float)
        is_zone=True 且 z_score<-2 → 极端低估，止损信号不可靠
        is_zone=True 且 z_score>2  → 极端高估，止盈信号可加强
    """
    if vwap <= 0 or current_price <= 0 or not atr or atr <= 0:
        return False, 0.0

    z_score = (current_price - vwap) / atr
    return (abs(z_score) > 2.0), z_score
