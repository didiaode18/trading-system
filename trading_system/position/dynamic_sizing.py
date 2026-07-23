"""
动态仓位计算
============
波动率倒数加权：波动越大，仓位越小
结合ATR和信号强度动态调整
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def dynamic_position_size(volatility: float, signal_strength: float = 1.0,
                          base_ratio: float = 0.10,
                          max_ratio: float = 0.15,
                          min_ratio: float = 0.03) -> float:
    """
    动态仓位 = 基础仓位 * 波动率调整 * 信号强度
    
    参数:
        volatility: 年化波动率 (如0.35表示35%)
        signal_strength: 信号强度 (0.5~2.0)
        base_ratio: 基础仓位比例
        max_ratio: 最大仓位
        min_ratio: 最小仓位
    
    返回:
        仓位比例
    """
    # 波动率调整：波动越大仓位越小
    # 基准波动率30%，波动率每增加10%，仓位减少20%
    vol_adjust = 0.30 / max(volatility, 0.10)
    vol_adjust = np.clip(vol_adjust, 0.5, 2.0)

    # 信号强度调整
    signal_adjust = np.clip(signal_strength, 0.5, 2.0)

    position = base_ratio * vol_adjust * signal_adjust
    return round(np.clip(position, min_ratio, max_ratio), 4)


def atr_position_size(atr: float, price: float, total_capital: float,
                      risk_per_trade: float = 0.02) -> int:
    """
    ATR仓位法：每笔交易风险 = 总资金 * risk_per_trade
    
    参数:
        atr: 14日ATR
        price: 当前价格
        total_capital: 总资金
        risk_per_trade: 单笔风险比例(默认2%)
    
    返回:
        建议股数（100的整数倍）
    """
    if atr <= 0 or price <= 0:
        return 0

    risk_amount = total_capital * risk_per_trade
    # 止损距离 = 2*ATR
    stop_distance = 2 * atr
    shares = int(risk_amount / stop_distance)
    shares = (shares // 100) * 100

    # 检查是否超过最大仓位
    max_shares = int(total_capital * 0.15 / price)
    max_shares = (max_shares // 100) * 100
    shares = min(shares, max_shares)

    return shares


def volatility_adjusted_ratio(df: pd.DataFrame, base_ratio: float = 0.10) -> float:
    """
    从行情数据计算波动率调整后的仓位
    
    参数:
        df: 含close列的DataFrame
        base_ratio: 基础仓位
    """
    if df is None or len(df) < 20:
        return base_ratio

    returns = df["close"].pct_change().dropna()
    annual_vol = returns.tail(60).std() * np.sqrt(252)

    return dynamic_position_size(annual_vol, base_ratio=base_ratio)
