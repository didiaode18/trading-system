"""
盘中分时形态识别模块（从 intraday_monitor.py 提取）
=====================================================
纯函数接口 —— 无类实例依赖，便于独立测试和复用。

形态:
  - M头(双顶): 两次高点差<0.3% + 跌破颈线
  - 阶梯上攻: 3级台阶，每级上涨>0.5%
  - V反: 急跌>3%后5分钟反弹>2%

使用:
    from strategy.intraday_patterns import detect_intraday_pattern
"""
import datetime


# ============================================================
# M头(双顶)
# ============================================================

def detect_m_head(code: str, name: str, prices: list,
                  cfg: dict, today: str, alerts_sent: set) -> dict:
    """M头(双顶)检测: 两个高点差<0.3% + 跌破颈线"""
    peak_diff_pct = cfg.get("m_head_peak_diff_pct", 0.003)
    neckline_break = cfg.get("m_head_neckline_break", -0.005)

    if len(prices) < 20:
        return None

    # 简化: 将价格序列分为前半/后半，各找最高点
    mid = len(prices) // 2
    first_half = prices[:mid]
    second_half = prices[mid:]

    peak1 = max(first_half)
    peak2 = max(second_half)

    if peak1 <= 0:
        return None
    diff = abs(peak1 - peak2) / peak1
    if diff > peak_diff_pct:
        return None

    # 颈线 = 两峰之间的最低点
    valley = min(prices[mid - 5:mid + 5]) if mid > 5 else min(prices)
    current = prices[-1]

    if current < valley * (1 + neckline_break):
        alert_key = f"{today}_{code}_m_head"
        if alert_key not in alerts_sent:
            alerts_sent.add(alert_key)
            return {
                "level": "warning",
                "type": "分时M头",
                "code": code,
                "name": name,
                "message": f"📉 {name}({code}) 分时M头形成! "
                          f"双顶{peak1:.2f}/{peak2:.2f}(差{diff*100:.2f}%) | "
                          f"已跌破颈线{valley:.2f} | 建议减仓",
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
            }
    return None


# ============================================================
# 阶梯上攻
# ============================================================

def detect_staircase(code: str, name: str, prices: list,
                     cfg: dict, today: str, alerts_sent: set) -> dict:
    """阶梯上攻: 3级台阶，每级上涨>0.5%"""
    min_steps = cfg.get("stair_min_steps", 3)
    min_rise = cfg.get("stair_min_rise_pct", 0.005)

    if len(prices) < 30:
        return None

    segment_size = len(prices) // (min_steps + 1)
    if segment_size < 5:
        return None

    steps = 0
    prev_level = prices[0]
    for i in range(1, min_steps + 2):
        seg_start = i * segment_size
        seg_end = min((i + 1) * segment_size, len(prices))
        if seg_start >= len(prices):
            break
        seg_avg = sum(prices[seg_start:seg_end]) / max(len(prices[seg_start:seg_end]), 1)
        rise = (seg_avg - prev_level) / prev_level if prev_level > 0 else 0
        if rise > min_rise:
            steps += 1
            prev_level = seg_avg

    if steps >= min_steps:
        alert_key = f"{today}_{code}_staircase"
        if alert_key not in alerts_sent:
            alerts_sent.add(alert_key)
            total_rise = (prices[-1] - prices[0]) / prices[0] * 100
            return {
                "level": "info",
                "type": "阶梯上攻",
                "code": code,
                "name": name,
                "message": f"📈 {name}({code}) 分时阶梯上攻! "
                          f"{steps}级台阶，累计+{total_rise:.1f}% | "
                          f"主力有序推升，持有",
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
            }
    return None


# ============================================================
# V反
# ============================================================

def detect_v_reversal(code: str, name: str, prices: list,
                      cfg: dict, today: str, alerts_sent: set) -> dict:
    """V反: 急跌>3%后5分钟反弹>2%"""
    drop_pct = cfg.get("v_reversal_drop_pct", -0.03)
    bounce_pct = cfg.get("v_reversal_bounce_pct", 0.02)

    if len(prices) < 10:
        return None

    recent = prices[-10:]
    min_price = min(recent)
    min_idx = recent.index(min_price)

    before_min = recent[:min_idx] if min_idx > 0 else [prices[-10]]
    local_high = max(before_min) if before_min else prices[-10]

    drop = (min_price - local_high) / local_high if local_high > 0 else 0
    if drop > drop_pct:
        return None

    current = prices[-1]
    bounce = (current - min_price) / min_price if min_price > 0 else 0

    if bounce > bounce_pct:
        alert_key = f"{today}_{code}_v_reversal_intraday"
        if alert_key not in alerts_sent:
            alerts_sent.add(alert_key)
            return {
                "level": "info",
                "type": "分时V反",
                "code": code,
                "name": name,
                "message": f"🔄 {name}({code}) 分时V型反转! "
                          f"急跌{drop*100:.1f}%后反弹+{bounce*100:.1f}% | "
                          f"短期止跌，观察持续性",
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
            }
    return None


# ============================================================
# 统一入口
# ============================================================

def detect_intraday_pattern(code: str, name: str, price_history: list,
                            cfg: dict, today: str, alerts_sent: set) -> list:
    """基于日内价格序列识别分时形态（统一入口）

    参数:
        code: 股票代码
        name: 股票名称
        price_history: [(timestamp, price), ...] 分时价格序列
        cfg: INTRADAY_PATTERN_CONFIG 配置字典
        today: 当日日期字符串
        alerts_sent: 已发送预警 key 集合（用于去重，会就地修改）

    返回: 预警 dict 列表
    """
    alerts = []
    if not cfg.get("enabled", True):
        return alerts

    min_points = cfg.get("min_data_points", 30)
    if len(price_history) < min_points:
        return alerts

    prices = [p[1] for p in price_history]

    m_alert = detect_m_head(code, name, prices, cfg, today, alerts_sent)
    if m_alert:
        alerts.append(m_alert)

    stair_alert = detect_staircase(code, name, prices, cfg, today, alerts_sent)
    if stair_alert:
        alerts.append(stair_alert)

    v_alert = detect_v_reversal(code, name, prices, cfg, today, alerts_sent)
    if v_alert:
        alerts.append(v_alert)

    return alerts


# ============================================================
# V10.1: 开盘定性 (高开低走=出货, 低开高走=吸筹)
# ============================================================

def detect_opening_pattern(code: str, name: str, prices: list,
                           cfg: dict, today: str, alerts_sent: set,
                           auction_result: dict = None) -> dict:
    """开盘30分钟定性分析

    高开低走: open在最高价区域 + close在最低价区域 → "出货"
    低开高走: open在最低价区域 + close在最高价区域 → "吸筹"
    竞价串联: 竞价fake_high_open + 开盘继续下跌 → "诱多确认"

    参数:
        code: 股票代码
        name: 股票名称
        prices: 开盘30分钟内的分时价格序列
        cfg: 配置字典
        today: 当日日期
        alerts_sent: 已发送预警集合
        auction_result: V10.1 竞价轨迹结果(单只)

    返回: 预警dict 或 None
    """
    if len(prices) < 10:
        return None

    open_price = prices[0]
    close_price = prices[-1]
    high_price = max(prices)
    low_price = min(prices)
    price_range = high_price - low_price

    if price_range <= 0 or open_price <= 0:
        return None

    # 位置计算: 0=最低, 1=最高
    open_pos = (open_price - low_price) / price_range
    close_pos = (close_price - low_price) / price_range

    pattern = ""
    label = ""
    detail = ""

    # 高开低走(出货): open在高位区域(>0.7) + close在低位区(<0.3)
    if open_pos > 0.7 and close_pos < 0.3:
        pattern = "high_open_low_walk"
        label = "高开低走(出货)"
        detail = f"开盘{open_price:.2f}→收盘{close_price:.2f} | 高位开盘持续走低"

    # 低开高走(吸筹): open在低位区(<0.3) + close在高位区域(>0.7)
    elif open_pos < 0.3 and close_pos > 0.7:
        pattern = "low_open_high_walk"
        label = "低开高走(吸筹)"
        detail = f"开盘{open_price:.2f}→收盘{close_price:.2f} | 低位开盘持续走高"

    # V10.1: 竞价→盘中串联
    if auction_result:
        auction_pattern = auction_result.get("pattern", "")
        # 竞价试盘诱多 + 开盘继续下跌 → 诱多确认
        if auction_pattern == "fake_high_open" and pattern == "high_open_low_walk":
            pattern = "trap_confirmed"
            label = "诱多确认"
            detail = f"竞价试盘诱多 + 开盘高开低走 = 诱多确认! 勿追高"
        # 竞价真实需求 + 开盘低开高走 → 吸筹确认
        elif auction_pattern == "real_demand" and pattern == "low_open_high_walk":
            pattern = "accumulation_confirmed"
            label = "吸筹确认"
            detail = f"竞价真实需求 + 开盘低开高走 = 吸筹确认! 可关注"

    if not pattern:
        return None

    alert_key = f"{today}_{code}_opening_{pattern}"
    if alert_key in alerts_sent:
        return None
    alerts_sent.add(alert_key)

    level = "warning" if "出货" in label or "诱多" in label else "info"
    icon = "📉" if level == "warning" else "📈"

    return {
        "level": level,
        "type": f"开盘定性-{label}",
        "code": code,
        "name": name,
        "message": f"{icon} {name}({code}) {label}: {detail}",
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
        "pattern": pattern,
    }
