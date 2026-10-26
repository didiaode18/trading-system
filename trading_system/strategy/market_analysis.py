# -*- coding: utf-8 -*-
"""
大盘方向分析模块（M因子 + Regime检测 + 结构性行情）
===================================================
从 stock_screener.py 拆分出的独立子模块，负责大盘方向判断。

公共接口:
  check_market_direction(data_dict) -> dict
  detect_market_regime(data_dict, market_info) -> dict
  _detect_structural_market(data_dict, regime_info) -> dict
"""

import logging
import numpy as np
import pandas as pd

import os, sys
import config

logger = logging.getLogger(__name__)


def check_market_direction(data_dict: dict) -> dict:
    """
    M因子：判断大盘方向，决定是否适合做多

    判断标准（000300沪深300）:
    - 收盘价 > MA20 > MA60 → 上升趋势（可操作）
    - 收盘价 > MA20 但 MA20 < MA60 → 震荡（谨慎操作）
    - 收盘价 < MA20 → 下降趋势（不建议买入）

    返回:
        {
            "market_state": "up" / "neutral" / "down",
            "can_buy": bool,
            "position_limit_ratio": float,
            "detail": str
        }
    """
    index_df = data_dict.get("000300")
    if index_df is None or len(index_df) < 60:
        logger.warning("[M因子] 无沪深300数据，默认中性")
        return {
            "market_state": "neutral",
            "can_buy": True,
            "position_limit_ratio": 0.5,
            "detail": "无指数数据，默认半仓操作",
            "degraded_reason": "无沪深300指数数据，大盘状态默认中性、建议仓位默认半仓，结论可靠性降低"
        }

    latest = index_df.iloc[-1]
    close = latest["close"]
    ma20 = latest.get("ma20", 0)
    ma60 = latest.get("ma60", 0)

    if pd.isna(ma20) or pd.isna(ma60):
        return {
            "market_state": "neutral",
            "can_buy": True,
            "position_limit_ratio": 0.5,
            "detail": "均线数据不足，默认半仓操作",
            "degraded_reason": "沪深300均线数据不足，大盘状态默认中性、建议仓位默认半仓，结论可靠性降低"
        }

    # 20日涨跌幅
    change_20d = (close / index_df["close"].iloc[-20] - 1) * 100 if len(index_df) >= 20 else 0
    # MA20斜率（近5日）
    ma20_slope = 0.0
    if len(index_df) >= 26:
        _ma20_base = index_df["ma20"].iloc[-6]
        if _ma20_base > 0 and not pd.isna(_ma20_base):
            ma20_slope = (ma20 - _ma20_base) / _ma20_base * 100

    if close > ma20 > ma60 and ma20_slope > 0:
        state = "up"
        can_buy = True
        limit = 1.0
        detail = f"上升趋势（沪深300在MA20/MA60上方，MA20斜率{ma20_slope:+.2f}%）→ 满仓操作"
    elif close > ma20:
        state = "neutral"
        can_buy = True
        limit = 0.5
        detail = f"震荡偏强（沪深300在MA20上方但MA20未持续向上，MA20斜率{ma20_slope:+.2f}%）→ 半仓操作"
    elif close > ma60:
        state = "neutral"
        can_buy = True
        limit = 0.3
        detail = f"震荡偏弱（沪深300在MA60上方但跌破MA20）→ 轻仓操作"
    else:
        state = "down"
        _flex_enabled = getattr(config, 'M_FACTOR_FLEXIBLE_ENABLED', True)
        _down_limit = getattr(config, 'M_FACTOR_DOWN_POSITION_LIMIT', 0.15)
        if _flex_enabled:
            can_buy = True
            limit = _down_limit
            detail = f"下降趋势（沪深300跌破MA20和MA60）→ V6.0柔性化: 允许轻仓{_down_limit:.0%}试探"
        else:
            can_buy = False
            limit = 0.0
            detail = f"下降趋势（沪深300跌破MA20和MA60）→ 不建议买入"

    if ma20 < ma60 and close < ma20:
        _flex_enabled = getattr(config, 'M_FACTOR_FLEXIBLE_ENABLED', True)
        _down_limit = getattr(config, 'M_FACTOR_DOWN_POSITION_LIMIT', 0.15)
        if _flex_enabled:
            can_buy = True
            limit = _down_limit
            state = "down"
            detail += f" | V6.0 P0-1: MA20<MA60且收盘<MA20，轻仓{_down_limit:.0%}操作"
        else:
            can_buy = False
            limit = 0.0
            state = "down"
            detail += " | P1-2: MA20<MA60且收盘<MA20，禁止新开仓"

    breadth = 50.0

    # V27: MA20斜率加速度检测
    ma_accel_result = {}
    try:
        from strategy.systemic_risk import check_ma_acceleration
        ma_accel_result = check_ma_acceleration(index_df)
        if ma_accel_result.get("triggered"):
            _accel_level = ma_accel_result.get("level", "warm")
            _accel_detail = ma_accel_result.get("detail", "")
            detail += f" | V27加速度预警({_accel_level}): {_accel_detail}"
            if _accel_level == "ready" and state != "down":
                limit = min(limit, 0.3)
                detail += " → 提前降仓至30%"
                logger.warning(f"[M因子] V27加速度预警触发: 仓位上限提前降至30%")
    except Exception as e:
        logger.debug(f"[M因子] V27加速度检测异常(不影响主流程): {e}")

    logger.info(f"[M因子] 大盘状态: {state} | {detail}")

    return {
        "market_state": state,
        "can_buy": can_buy,
        "position_limit_ratio": limit,
        "detail": detail,
        "index_close": round(close, 2),
        "index_ma20": round(ma20, 2),
        "index_ma60": round(ma60, 2),
        "change_20d": round(change_20d, 2),
        "market_breadth": round(breadth, 1),
        "ma_acceleration": ma_accel_result,
    }


def detect_market_regime(data_dict: dict, market_info: dict) -> dict:
    """
    V5.1: 市场regime检测 —— 基于市场宽度(breadth)判断当前适合趋势跟踪还是均值回归

    判定标准:
    - breadth > 55% → trend_following
    - breadth < 45% → mean_reversion
    - 45-55%      → mixed

    返回:
        {
            "regime": "trend_following" / "mean_reversion" / "mixed",
            "breadth": float,
            "detail": str
        }
    """
    above_ma20 = 0
    total = 0

    for code, df in data_dict.items():
        if code == "000300":
            continue
        if code.startswith(("300", "688", "588", "159")):
            continue
        if len(df) < 25:
            continue

        ma20_val = df["close"].rolling(20).mean().iloc[-1]
        close_val = df["close"].iloc[-1]

        if not pd.isna(ma20_val) and ma20_val > 0:
            total += 1
            if close_val > ma20_val:
                above_ma20 += 1

    breadth = (above_ma20 / total * 100) if total > 0 else 50.0

    if breadth > 55:
        regime = "trend_following"
        detail = f"趋势模式(宽度{breadth:.0f}%>55%，{above_ma20}/{total}只在MA20上方) → 动量/龙头因子加权"
    elif breadth < 45:
        regime = "mean_reversion"
        detail = f"回归模式(宽度{breadth:.0f}%<45%，仅{above_ma20}/{total}只在MA20上方) → 均值回归/周线因子加权"
    else:
        regime = "mixed"
        detail = f"混合模式(宽度{breadth:.0f}%，{above_ma20}/{total}只在MA20上方) → 标准权重"

    logger.info(f"[Regime检测] {detail}")

    try:
        from strategy.systemic_risk import save_breadth_today
        save_breadth_today(round(breadth, 1))
    except Exception as e:
        logger.debug(f"[Regime检测] V27宽度持久化异常(不影响主流程): {e}")

    market_info["market_breadth"] = round(breadth, 1)

    return {
        "regime": regime,
        "breadth": round(breadth, 1),
        "above_ma20_count": above_ma20,
        "total_count": total,
        "detail": detail,
    }


def _detect_structural_market(data_dict: dict, regime_info: dict) -> dict:
    """
    V10.1 Top4: 结构性行情检测

    原理: 计算全市场个股5日收益的标准差(分化度)。
    高分化 + 混合regime = 结构性行情。

    返回:
        {
            "structural": bool,
            "dispersion": float,
            "position_adj": float,
            "detail": str
        }
    """
    returns_5d = []

    for code, df in data_dict.items():
        if code == "000300" or len(df) < 6:
            continue
        if code.startswith(("300", "688", "588", "159")):
            continue
        chg_5d = (df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100
        returns_5d.append(chg_5d)

    if len(returns_5d) < 10:
        return {"structural": False, "dispersion": 0, "position_adj": 1.0, "detail": "数据不足"}

    dispersion = np.std(returns_5d)

    index_chg = 0
    if "000300" in data_dict and len(data_dict["000300"]) >= 6:
        idx_df = data_dict["000300"]
        index_chg = abs(idx_df["close"].iloc[-1] / idx_df["close"].iloc[-6] - 1) * 100

    breadth = regime_info.get("breadth", 50)

    STRUCTURAL_DISPERSION_THRESHOLD = getattr(config, 'STRUCTURAL_DISPERSION_THRESHOLD', 4.0)
    STRUCTURAL_INDEX_THRESHOLD = getattr(config, 'STRUCTURAL_INDEX_THRESHOLD', 3.0)

    is_structural = (
        index_chg < STRUCTURAL_INDEX_THRESHOLD and
        dispersion > STRUCTURAL_DISPERSION_THRESHOLD and
        regime_info.get("regime") == "mixed"
    )

    if is_structural:
        position_adj = max(0.8, min(0.95, 1.0 - (dispersion - STRUCTURAL_DISPERSION_THRESHOLD) * 0.025))
        detail = f"结构性行情: 指数横盘({index_chg:.1f}%) + 高分化(σ={dispersion:.1f}%) → 仓位×{position_adj:.2f}"
    else:
        position_adj = 1.0
        detail = f"非结构性: 指数{'横盘' if index_chg < 3 else '趋势'}({index_chg:.1f}%) 分化σ={dispersion:.1f}%"

    logger.info(f"[结构性行情] {detail}")

    return {
        "structural": is_structural,
        "dispersion": round(dispersion, 2),
        "position_adj": round(position_adj, 2),
        "detail": detail,
    }
