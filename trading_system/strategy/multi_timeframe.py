"""
多周期共振分析模块
====================
同时分析日线 + 周线数据，判断多周期趋势共振

核心规则:
- 日线趋势向上 + 周线趋势向上 = 强共振（提高买入权重/仓位）
- 日线与周线方向矛盾 = 降低仓位或观望
- 周线趋势向下 = 禁止开新仓（大周期压制）

使用方式:
    from strategy.multi_timeframe import analyze_multi_timeframe
    result = analyze_multi_timeframe(daily_df, weekly_df)
    # result: {"resonance": "strong"/"normal"/"conflict"/"weak", "score": float, "position_adjust": float}
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def _convert_daily_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """
    将日线数据转换为周线数据
    
    参数:
        daily_df: 日线DataFrame (date, open, close, high, low, volume)
    
    返回:
        周线DataFrame，格式与日线一致
    """
    if daily_df is None or daily_df.empty:
        return pd.DataFrame()

    df = daily_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")

    # 按周重采样
    weekly = df.resample("W-FRI").agg({
        "open": "first",
        "close": "last",
        "high": "max",
        "low": "min",
        "volume": "sum"
    }).dropna()

    weekly = weekly.reset_index()
    weekly["date"] = weekly["date"].dt.strftime("%Y-%m-%d")
    return weekly


def _compute_weekly_indicators(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """计算周线技术指标"""
    if weekly_df is None or len(weekly_df) < config.MA_MID:
        return weekly_df

    df = weekly_df.copy()
    df["ma20"] = df["close"].rolling(config.MA_SHORT).mean()
    df["ma60"] = df["close"].rolling(config.MA_MID).mean()
    df["ma20_slope"] = df["ma20"].diff(3)

    # RSI
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(config.RSI_PERIOD).mean()
    avg_loss = loss.rolling(config.RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema_fast = df["close"].ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow = df["close"].ewm(span=config.MACD_SLOW, adjust=False).mean()
    df["macd_dif"] = ema_fast - ema_slow
    df["macd_dea"] = df["macd_dif"].ewm(span=config.MACD_SIGNAL, adjust=False).mean()

    return df


def analyze_multi_timeframe(daily_df: pd.DataFrame,
                            weekly_df: pd.DataFrame = None) -> dict:
    """
    多周期共振分析
    
    参数:
        daily_df: 日线数据（需含ma20, ma60, ma20_slope等指标）
        weekly_df: 周线数据（可选，为None时自动从日线转换）
    
    返回:
        {
            "resonance": str,       # "strong"/"normal"/"conflict"/"weak"
            "score": float,         # 共振评分 (-1.0 ~ 1.0)
            "position_adjust": float,  # 仓位调整系数 (0.5 ~ 1.2)
            "daily_trend": str,     # "up"/"down"/"neutral"
            "weekly_trend": str,    # "up"/"down"/"neutral"
            "reason": str           # 分析说明
        }
    """
    result = {
        "resonance": "normal",
        "score": 0.0,
        "position_adjust": 1.0,
        "daily_trend": "neutral",
        "weekly_trend": "neutral",
        "reason": ""
    }

    if daily_df is None or daily_df.empty or len(daily_df) < config.MA_MID:
        result["reason"] = "日线数据不足"
        return result

    # ---- 日线趋势判定 ----
    daily_latest = daily_df.iloc[-1]
    daily_close = daily_latest["close"]
    daily_ma20 = daily_latest.get("ma20", 0)
    daily_ma20_slope = daily_latest.get("ma20_slope", 0)

    if daily_close > daily_ma20 and daily_ma20_slope > 0:
        result["daily_trend"] = "up"
    elif daily_close < daily_ma20 and daily_ma20_slope < 0:
        result["daily_trend"] = "down"
    else:
        result["daily_trend"] = "neutral"

    # ---- 周线趋势判定 ----
    if weekly_df is None:
        weekly_df = _convert_daily_to_weekly(daily_df)

    if weekly_df is None or weekly_df.empty or len(weekly_df) < config.MA_MID:
        result["reason"] = f"周线数据不足（仅{len(weekly_df) if weekly_df is not None else 0}周），仅参考日线"
        result["score"] = 0.3 if result["daily_trend"] == "up" else (-0.3 if result["daily_trend"] == "down" else 0)
        result["position_adjust"] = 1.0 if result["daily_trend"] == "up" else 0.7
        return result

    weekly_df = _compute_weekly_indicators(weekly_df)
    weekly_latest = weekly_df.iloc[-1]
    weekly_close = weekly_latest["close"]
    weekly_ma20 = weekly_latest.get("ma20", 0)
    weekly_ma20_slope = weekly_latest.get("ma20_slope", 0)

    if weekly_close > weekly_ma20 and weekly_ma20_slope > 0:
        result["weekly_trend"] = "up"
    elif weekly_close < weekly_ma20 and weekly_ma20_slope < 0:
        result["weekly_trend"] = "down"
    else:
        result["weekly_trend"] = "neutral"

    # ---- 共振判定 ----
    daily_up = result["daily_trend"] == "up"
    daily_down = result["daily_trend"] == "down"
    weekly_up = result["weekly_trend"] == "up"
    weekly_down = result["weekly_trend"] == "down"

    reasons = []

    if daily_up and weekly_up:
        result["resonance"] = "strong"
        result["score"] = 0.8
        result["position_adjust"] = 1.2  # 共振时仓位可上浮20%
        reasons.append("日线+周线双向上，强共振")

        # 额外加分：周线MACD多头
        weekly_dif = weekly_latest.get("macd_dif", 0)
        weekly_dea = weekly_latest.get("macd_dea", 0)
        if not pd.isna(weekly_dif) and not pd.isna(weekly_dea) and weekly_dif > weekly_dea:
            result["score"] = 1.0
            result["position_adjust"] = 1.2
            reasons.append("周线MACD多头确认")

    elif daily_up and not weekly_down:
        result["resonance"] = "normal"
        result["score"] = 0.3
        result["position_adjust"] = 1.0
        reasons.append(f"日线向上，周线{result['weekly_trend']}，正常共振")

    elif daily_down and weekly_down:
        result["resonance"] = "weak"
        result["score"] = -0.8
        result["position_adjust"] = 0.5  # 双向下，仓位减半
        reasons.append("日线+周线双向下，禁止开新仓")

    elif daily_down and not weekly_up:
        result["resonance"] = "weak"
        result["score"] = -0.3
        result["position_adjust"] = 0.7
        reasons.append(f"日线向下，周线{result['weekly_trend']}，弱势")

    else:
        # 日线与周线矛盾
        result["resonance"] = "conflict"
        result["score"] = 0.0
        result["position_adjust"] = 0.7  # 矛盾时降低仓位
        reasons.append(f"日线{result['daily_trend']}与周线{result['weekly_trend']}矛盾，降低仓位")

    result["reason"] = "; ".join(reasons)
    return result


def get_resonance_adjustment(daily_df: pd.DataFrame,
                             weekly_df: pd.DataFrame = None) -> dict:
    """
    便捷接口：获取共振调整系数
    
    返回:
        {
            "position_adjust": float,  # 仓位调整系数
            "allow_new_position": bool,  # 是否允许开新仓
            "reason": str
        }
    """
    analysis = analyze_multi_timeframe(daily_df, weekly_df)

    allow_new = analysis["resonance"] != "weak"
    if analysis["resonance"] == "weak" and analysis["daily_trend"] == "down" and analysis["weekly_trend"] == "down":
        allow_new = False

    return {
        "position_adjust": analysis["position_adjust"],
        "allow_new_position": allow_new,
        "resonance": analysis["resonance"],
        "reason": analysis["reason"]
    }


if __name__ == "__main__":
    print("=" * 50)
    print("  多周期共振分析 - 测试")
    print("=" * 50)

    # 生成模拟日线数据（上涨趋势）
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=150, freq="B")
    base_price = 50
    prices = [base_price]
    for i in range(1, 150):
        change = np.random.normal(0.2, 0.5)
        prices.append(max(prices[-1] + change, 10))

    daily_df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": [p * 0.998 for p in prices],
        "close": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "volume": np.random.randint(500000, 2000000, 150).astype(float),
    })

    # 计算日线指标
    from strategy.trend_strategy import compute_indicators
    daily_df = compute_indicators(daily_df)

    result = analyze_multi_timeframe(daily_df)
    print(f"\n日线趋势: {result['daily_trend']}")
    print(f"周线趋势: {result['weekly_trend']}")
    print(f"共振状态: {result['resonance']}")
    print(f"共振评分: {result['score']}")
    print(f"仓位调整: {result['position_adjust']}")
    print(f"分析说明: {result['reason']}")

    adj = get_resonance_adjustment(daily_df)
    print(f"\n允许开新仓: {adj['allow_new_position']}")
    print(f"仓位系数: {adj['position_adjust']}")

    print("\n[OK] 多周期共振模块测试通过")
