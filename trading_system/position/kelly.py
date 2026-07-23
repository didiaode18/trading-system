"""
Kelly公式仓位计算
================
Kelly仓位 = (胜率 * 盈亏比 - 败率) / 盈亏比
实际仓位 = Kelly * 0.5（半Kelly，降低波动）
约束: 单只最大15%
"""

import numpy as np
import logging

logger = logging.getLogger(__name__)

MAX_SINGLE_POSITION = 0.15  # 单只最大15%


def kelly_position(win_rate: float, profit_factor: float,
                   max_ratio: float = MAX_SINGLE_POSITION) -> float:
    """
    Kelly公式计算最优仓位比例
    
    参数:
        win_rate: 历史胜率 (0~1)
        profit_factor: 盈亏比 (平均盈利/平均亏损)
        max_ratio: 单只最大仓位
    
    返回:
        仓位比例 (0~max_ratio)
    """
    if profit_factor <= 0 or win_rate <= 0:
        return 0.0

    # Kelly公式: f = (p*b - q) / b
    # p=胜率, q=败率, b=盈亏比
    q = 1 - win_rate
    kelly = (win_rate * profit_factor - q) / profit_factor

    # 限制范围
    kelly = max(0, min(kelly, max_ratio))
    return round(kelly, 4)


def half_kelly_position(win_rate: float, profit_factor: float,
                        max_ratio: float = MAX_SINGLE_POSITION) -> float:
    """
    半Kelly仓位（推荐，降低波动）
    
    半Kelly = Kelly * 0.5
    牺牲少量期望收益，大幅降低回撤
    """
    full = kelly_position(win_rate, profit_factor, max_ratio * 2)
    return round(full * 0.5, 4)


def kelly_from_trades(trades: list) -> float:
    """
    从交易记录计算Kelly仓位
    
    参数:
        trades: [{"pnl_pct": 0.05}, {"pnl_pct": -0.03}, ...]
    
    返回:
        半Kelly仓位比例
    """
    if not trades:
        return 0.05  # 默认5%

    wins = [t["pnl_pct"] for t in trades if t.get("pnl_pct", 0) > 0]
    losses = [t["pnl_pct"] for t in trades if t.get("pnl_pct", 0) < 0]

    if not wins or not losses:
        return 0.05

    win_rate = len(wins) / len(trades)
    avg_win = np.mean(wins)
    avg_loss = abs(np.mean(losses))
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 1

    return half_kelly_position(win_rate, profit_factor)


def portfolio_kelly(holdings_stats: list, total_capital: float) -> dict:
    """
    组合Kelly仓位分配
    
    参数:
        holdings_stats: [{"code": "002371", "win_rate": 0.6, "profit_factor": 2.0}, ...]
        total_capital: 总资金
    
    返回:
        {code: {"ratio": 0.12, "amount": 89821, "kelly": 0.24}}
    """
    result = {}
    total_ratio = 0

    for h in holdings_stats:
        code = h["code"]
        kelly = kelly_position(h.get("win_rate", 0.5), h.get("profit_factor", 1.5))
        half = kelly * 0.5
        result[code] = {
            "kelly": round(kelly, 4),
            "ratio": round(half, 4),
            "amount": round(half * total_capital, 0),
        }
        total_ratio += half

    # 如果总仓位>90%，等比缩放
    if total_ratio > 0.90:
        scale = 0.90 / total_ratio
        for code in result:
            result[code]["ratio"] = round(result[code]["ratio"] * scale, 4)
            result[code]["amount"] = round(result[code]["ratio"] * total_capital, 0)

    return result
