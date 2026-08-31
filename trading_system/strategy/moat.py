"""
护城河代理指标（V10.1 Top6 新增）
================================
基于可量化数据构建护城河(Economic Moat)代理评分。

书中理念: 茅台、长江电力、宁德时代等案例说明，持久竞争优势(护城河)
是长期超额收益的核心来源。但护城河本身难以直接量化。

代理指标设计:
  1. ROE稳定性(40%): 近3年ROE标准差越小越好 → 定价权稳定
  2. 毛利率趋势(35%): 毛利率是否维持或上升 → 竞争优势是否扩大
  3. 行业地位(25%): 行业内RPS排名 → 市场份额优势

评分规则:
  - ROE稳定性: std(ROE) < 3% → 满分40, 3-5% → 30, 5-8% → 20, >8% → 10
  - 毛利率趋势: 上升 → 满分35, 平稳 → 25, 下降 → 10
  - 行业地位: RPS前20% → 满分25, 20-40% → 18, 40-60% → 10, >60% → 5

最终评分: 0-100分, 映射为CANSLIM加分 0-3分

使用方式:
    from strategy.moat import get_moat_score
    moat_adj = get_moat_score("002371")  # 返回 0-3
"""

import os
import sys
import logging

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# ============================================================
# 护城河评分配置
# ============================================================
# ROE稳定性权重
MOAT_ROE_WEIGHT = 0.40
# 毛利率趋势权重
MOAT_MARGIN_WEIGHT = 0.35
# 行业地位权重
MOAT_POSITION_WEIGHT = 0.25

# 护城河分数 → CANSLIM加分映射
# 70+分 → +3, 55-70 → +2, 40-55 → +1, <40 → 0
MOAT_SCORE_TO_BONUS = [
    (70, 3),   # 强护城河
    (55, 2),   # 中等护城河
    (40, 1),   # 弱护城河
    (0, 0),    # 无明显护城河
]

# 缓存
_MOAT_CACHE = {}


def _score_roe_stability(roe_history: list) -> float:
    """
    ROE稳定性评分（0-40分）
    
    参数:
        roe_history: 近3年ROE列表（季度或年度，百分比值如15.0表示15%）
    
    原理: ROE波动越小，说明定价权越稳定，护城河越深
    """
    if not roe_history or len(roe_history) < 3:
        return 10  # 数据不足，给低分
    
    roe_std = np.std(roe_history)
    
    if roe_std < 3:
        return 40  # ROE极稳定
    elif roe_std < 5:
        return 30
    elif roe_std < 8:
        return 20
    else:
        return 10  # ROE波动大


def _score_margin_trend(gross_margins: list) -> float:
    """
    毛利率趋势评分（0-35分）
    
    参数:
        gross_margins: 近3年毛利率列表（百分比值如40.0表示40%）
    
    原理: 毛利率维持或上升说明竞争优势在扩大
    """
    if not gross_margins or len(gross_margins) < 3:
        return 10  # 数据不足
    
    # 计算趋势: 后半段 vs 前半段
    mid = len(gross_margins) // 2
    early_avg = np.mean(gross_margins[:mid])
    late_avg = np.mean(gross_margins[mid:])
    
    change = late_avg - early_avg
    
    if change > 1.0:
        return 35  # 毛利率上升 > 1%
    elif change > -1.0:
        return 25  # 毛利率平稳
    else:
        return 10  # 毛利率下降


def _score_market_position(rps_rank: float) -> float:
    """
    行业地位评分（0-25分）
    
    参数:
        rps_rank: RPS排名百分位（0-1, 0=最强, 1=最弱）
    
    原理: 行业龙头通常具有规模优势和定价权
    """
    if rps_rank is None or rps_rank < 0:
        return 10  # 无数据
    
    if rps_rank < 0.20:
        return 25  # 行业前20%
    elif rps_rank < 0.40:
        return 18
    elif rps_rank < 0.60:
        return 10
    else:
        return 5  # 行业后40%


def compute_moat_score(code: str, fund_data: dict = None, rps_rank: float = None) -> dict:
    """
    计算个股护城河综合评分
    
    参数:
        code: 股票代码
        fund_data: 基本面数据字典（来自FUNDAMENTAL_DATA）
        rps_rank: RPS排名百分位（0-1）
    
    返回:
        {
            "moat_score": float,      # 综合评分 0-100
            "bonus": int,             # CANSLIM加分 0-3
            "roe_score": float,       # ROE稳定性分
            "margin_score": float,    # 毛利率趋势分
            "position_score": float,  # 行业地位分
            "detail": str
        }
    """
    if not getattr(config, 'MOAT_INDICATOR_ENABLED', True):
        return {"moat_score": 0, "bonus": 0, "roe_score": 0, "margin_score": 0, 
                "position_score": 0, "detail": "未启用"}
    
    # 检查缓存
    if code in _MOAT_CACHE:
        return _MOAT_CACHE[code]
    
    if fund_data is None:
        fund_data = {}
    
    # 1. ROE稳定性
    roe_history = fund_data.get("roe_history", [])
    roe_score = _score_roe_stability(roe_history)
    
    # 2. 毛利率趋势
    margin_history = fund_data.get("gross_margin_history", [])
    margin_score = _score_margin_trend(margin_history)
    
    # 3. 行业地位（用RPS排名代理）
    position_score = _score_market_position(rps_rank)
    
    # 综合评分（分量满分分别为40/35/25，总和=100，无需额外加权）
    moat_score = roe_score + margin_score + position_score
    
    # 映射为CANSLIM加分
    bonus = 0
    for threshold, adj in MOAT_SCORE_TO_BONUS:
        if moat_score >= threshold:
            bonus = adj
            break
    
    detail = f"ROE稳{roe_score:.0f}/40 毛利率{margin_score:.0f}/35 地位{position_score:.0f}/25 → 总{moat_score:.0f}分 +{bonus}"
    
    result = {
        "moat_score": round(moat_score, 1),
        "bonus": bonus,
        "roe_score": round(roe_score, 1),
        "margin_score": round(margin_score, 1),
        "position_score": round(position_score, 1),
        "detail": detail,
    }
    
    _MOAT_CACHE[code] = result
    return result


def get_moat_bonus(code: str, fund_data: dict = None, rps_rank: float = None) -> int:
    """
    获取护城河CANSLIM加分（便捷接口）
    
    返回: 0-3
    """
    result = compute_moat_score(code, fund_data, rps_rank)
    return result.get("bonus", 0)


def reset_moat_cache():
    """重置护城河缓存"""
    global _MOAT_CACHE
    _MOAT_CACHE = {}
