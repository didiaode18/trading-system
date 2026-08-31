"""
宏观先行指标叠加（V10.1 Top7 新增）
====================================
基于PMI、社融、M2等宏观先行指标，评估当前宏观环境对A股的影响，
输出仓位调整建议（±5%）。

书中理念: 经济周期与政策市是A股最大的β来源。
宏观先行指标（PMI>50扩张、社融放量、M2回升）通常领先股市3-6个月。

数据来源:
  - PMI: 官方制造业PMI（>50扩张, <50收缩）
  - 社融: 社会融资规模同比增速
  - M2: 广义货币同比增速
  
  默认通过 akshare 获取最新数据，失败时使用手动配置值。

评分规则:
  - 三项指标均向好 → 宏观加分+5%仓位
  - 两项向好 → +2.5%
  - 一项向好 → 中性
  - 零项向好 → -5%仓位

使用方式:
    from strategy.macro_indicator import get_macro_position_adjustment
    adj = get_macro_position_adjustment()  # 返回 -0.05 ~ +0.05
"""

import os
import sys
import json
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# ============================================================
# 宏观指标阈值配置
# ============================================================
PMI_EXPANSION_THRESHOLD = 50.0    # PMI > 50 = 扩张
M2_YOY_THRESHOLD = 8.0            # M2同比 > 8% = 宽松
SF_YOY_THRESHOLD = 10.0           # 社融同比 > 10% = 放量

# 手动覆盖值（当自动获取失败时使用）
# 格式: {"pmi": 50.5, "m2_yoy": 9.0, "sf_yoy": 12.0}
_MANUAL_OVERRIDE = None

# 缓存
_MACRO_CACHE = {"computed": False, "data": {}, "timestamp": None}
_CACHE_TTL_HOURS = 24  # 宏观数据每日更新一次


def _fetch_macro_data() -> dict:
    """
    获取最新宏观数据
    
    返回:
        {"pmi": float, "m2_yoy": float, "sf_yoy": float, "source": str}
    """
    global _MANUAL_OVERRIDE
    
    # 优先使用手动覆盖值
    if _MANUAL_OVERRIDE:
        _MANUAL_OVERRIDE["source"] = "manual"
        return _MANUAL_OVERRIDE
    
    # 尝试从akshare获取
    try:
        data = {}
        
        # PMI: 官方制造业PMI
        try:
            import akshare as ak
            pmi_df = ak.macro_china_pmi()
            if pmi_df is not None and len(pmi_df) > 0:
                # 取最新一期
                latest = pmi_df.iloc[-1]
                data["pmi"] = float(latest.iloc[1]) if len(latest) > 1 else None
        except Exception as e:
            logger.debug(f"[宏观] PMI获取失败: {e}")
        
        # M2同比
        try:
            import akshare as ak
            m2_df = ak.macro_china_money_supply()
            if m2_df is not None and len(m2_df) > 0:
                latest = m2_df.iloc[-1]
                # M2同比通常在特定列
                for col in latest.index:
                    if 'M2' in str(col) and '同比' in str(col):
                        data["m2_yoy"] = float(latest[col])
                        break
        except Exception as e:
            logger.debug(f"[宏观] M2获取失败: {e}")
        
        # 如果成功获取了至少一项数据
        if data:
            data["source"] = "akshare"
            return data
            
    except ImportError:
        logger.debug("[宏观] akshare未安装，使用配置默认值")
    
    # 回退到配置默认值
    default_pmi = getattr(config, 'MACRO_DEFAULT_PMI', 50.0)
    default_m2 = getattr(config, 'MACRO_DEFAULT_M2', 8.0)
    default_sf = getattr(config, 'MACRO_DEFAULT_SF', 10.0)
    
    return {
        "pmi": default_pmi,
        "m2_yoy": default_m2,
        "sf_yoy": default_sf,
        "source": "config_default",
    }


def compute_macro_score() -> dict:
    """
    计算宏观环境综合评分
    
    返回:
        {
            "score": int,          # 向好指标数量 (0-3)
            "position_adj": float, # 仓位调整 (-0.05 ~ +0.05)
            "pmi": float,
            "m2_yoy": float,
            "sf_yoy": float,
            "detail": str,
            "source": str,
        }
    """
    global _MACRO_CACHE
    
    # 检查缓存
    if _MACRO_CACHE.get("computed") and _MACRO_CACHE.get("timestamp"):
        age_hours = (datetime.datetime.now() - _MACRO_CACHE["timestamp"]).total_seconds() / 3600
        if age_hours < _CACHE_TTL_HOURS:
            return _MACRO_CACHE["data"]
    
    data = _fetch_macro_data()
    
    pmi = data.get("pmi")
    m2_yoy = data.get("m2_yoy")
    sf_yoy = data.get("sf_yoy")
    
    # 计算向好指标数量
    good_count = 0
    details = []
    
    if pmi is not None:
        if pmi >= PMI_EXPANSION_THRESHOLD:
            good_count += 1
            details.append(f"PMI={pmi:.1f}(扩张)")
        else:
            details.append(f"PMI={pmi:.1f}(收缩)")
    
    if m2_yoy is not None:
        if m2_yoy >= M2_YOY_THRESHOLD:
            good_count += 1
            details.append(f"M2={m2_yoy:.1f}%(宽松)")
        else:
            details.append(f"M2={m2_yoy:.1f}%(偏紧)")
    
    if sf_yoy is not None:
        if sf_yoy >= SF_YOY_THRESHOLD:
            good_count += 1
            details.append(f"社融={sf_yoy:.1f}%(放量)")
        else:
            details.append(f"社融={sf_yoy:.1f}%(缩量)")
    
    # 仓位调整映射
    MACRO_MAX_ADJ = getattr(config, 'MACRO_POSITION_ADJ_MAX', 0.05)
    if good_count >= 3:
        position_adj = MACRO_MAX_ADJ
    elif good_count == 2:
        position_adj = MACRO_MAX_ADJ / 2
    elif good_count == 1:
        position_adj = 0
    else:
        position_adj = -MACRO_MAX_ADJ
    
    detail_str = " | ".join(details) + f" → {good_count}/3向好 → 仓位{'+' if position_adj >= 0 else ''}{position_adj:.1%}"
    logger.info(f"[宏观指标] {detail_str}")
    
    result = {
        "score": good_count,
        "position_adj": round(position_adj, 4),
        "pmi": pmi,
        "m2_yoy": m2_yoy,
        "sf_yoy": sf_yoy,
        "detail": detail_str,
        "source": data.get("source", "unknown"),
    }
    
    _MACRO_CACHE = {
        "computed": True,
        "data": result,
        "timestamp": datetime.datetime.now(),
    }
    
    return result


def get_macro_position_adjustment() -> float:
    """
    获取宏观环境仓位调整值（便捷接口）
    
    返回: float (-0.05 ~ +0.05)
    """
    if not getattr(config, 'MACRO_INDICATOR_ENABLED', True):
        return 0.0
    
    result = compute_macro_score()
    return result.get("position_adj", 0.0)


def set_manual_macro_data(pmi: float = None, m2_yoy: float = None, sf_yoy: float = None):
    """
    手动设置宏观数据（当自动获取失败时覆盖）
    
    参数:
        pmi: PMI值（如50.5）
        m2_yoy: M2同比（如9.0）
        sf_yoy: 社融同比（如12.0）
    """
    global _MANUAL_OVERRIDE, _MACRO_CACHE
    _MANUAL_OVERRIDE = {}
    if pmi is not None:
        _MANUAL_OVERRIDE["pmi"] = pmi
    if m2_yoy is not None:
        _MANUAL_OVERRIDE["m2_yoy"] = m2_yoy
    if sf_yoy is not None:
        _MANUAL_OVERRIDE["sf_yoy"] = sf_yoy
    # 清除缓存以强制重新计算
    _MACRO_CACHE = {"computed": False, "data": {}, "timestamp": None}


def reset_macro_cache():
    """重置宏观数据缓存"""
    global _MACRO_CACHE
    _MACRO_CACHE = {"computed": False, "data": {}, "timestamp": None}
