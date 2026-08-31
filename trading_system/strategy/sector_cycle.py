"""
行业景气度因子（V10.1 Top5 新增）
================================
基于ETF行业轮动动量数据，判定各行业的景气周期阶段，
为CANSLIM选股提供行业层面的加减分。

景气周期四阶段映射:
  - 复苏期(recovery): ETF动量由负转正 + 加速度为正 → 加分+3
  - 扩张期(expansion): 动量持续为正 + 排名靠前 → 加分+2
  - 放缓期(slowdown): 动量由正转负 或 排名下滑 → 减分-2
  - 收缩期(contraction): 动量持续为负 + 排名靠后 → 减分-3

与现有模块关系:
  - etf_rotation: 提供ETF动量排名（数据源）
  - filter_strong_sectors: 基于个股的板块强弱（技术面）
  - sector_cycle: 基于ETF动量的景气周期判定（基本面+动量综合）

使用方式:
    from strategy.sector_cycle import get_sector_cycle_score
    score_adj = get_sector_cycle_score("半导体")  # 返回 -3 ~ +3
"""

import os
import sys
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# ============================================================
# 景气度评分配置（通过config覆盖）
# ============================================================
# 各周期阶段对应的CANSLIM分数调整
CYCLE_SCORE_MAP = {
    "recovery": 3,       # 复苏期: 景气度拐点，最佳布局时点
    "expansion": 2,      # 扩张期: 景气上行中，趋势延续
    "stable": 0,         # 稳定期: 中性，不调整
    "slowdown": -2,      # 放缓期: 景气度下行，谨慎
    "contraction": -3,   # 收缩期: 景气度最差，回避
    "unknown": 0,        # 无数据: 中性
}

# 缓存（每次运行计算一次）
_CYCLE_CACHE = {"computed": False, "sectors": {}, "timestamp": None}
_CACHE_TTL_HOURS = 12  # 缓存有效期


def _classify_cycle_phase(chg_5d: float, chg_20d: float, accel: float, rank_pct: float) -> str:
    """
    根据ETF动量指标判定景气周期阶段
    
    参数:
        chg_5d: 5日涨幅%
        chg_20d: 20日涨幅%
        accel: 加速度（5d涨幅 vs 20d涨幅/4）
        rank_pct: 排名百分位（0=最强, 1=最弱）
    
    返回:
        "recovery" / "expansion" / "stable" / "slowdown" / "contraction"
    """
    # 收缩期: 动量持续为负 + 排名靠后
    if chg_5d < -1.0 and chg_20d < -2.0 and rank_pct > 0.70:
        return "contraction"
    
    # 放缓期: 动量由正转负 或 加速度明确为负
    if (chg_5d < 0 and chg_20d > 0) or (accel < -1.5 and rank_pct > 0.55):
        return "slowdown"
    
    # 复苏期: 动量由负转正 + 加速度为正（拐点信号）
    if chg_5d > 0 and chg_20d < 0 and accel > 0:
        return "recovery"
    
    # 扩张期: 动量持续为正 + 排名靠前
    if chg_5d > 0 and chg_20d > 0 and rank_pct < 0.40:
        return "expansion"
    
    # 其他情况: 稳定/中性
    return "stable"


def compute_sector_cycles(etf_rotation_result: dict = None) -> dict:
    """
    计算各行业景气周期阶段
    
    参数:
        etf_rotation_result: ETF轮动结果（若为None则自动获取）
    
    返回:
        {
            "半导体": {"phase": "expansion", "score_adj": 2, "chg_5d": 2.3, "chg_20d": 1.5, "rank_pct": 0.2},
            "新能源": {"phase": "recovery", "score_adj": 3, ...},
            ...
        }
    """
    global _CYCLE_CACHE
    
    # 检查缓存
    if _CYCLE_CACHE.get("computed", False):
        if _CYCLE_CACHE.get("timestamp"):
            age_hours = (datetime.datetime.now() - _CYCLE_CACHE["timestamp"]).total_seconds() / 3600
            if age_hours < _CACHE_TTL_HOURS:
                return _CYCLE_CACHE["sectors"]
    
    # 获取ETF轮动数据
    if etf_rotation_result is None:
        try:
            from strategy.etf_rotation import run_etf_rotation
            etf_rotation_result = run_etf_rotation(use_cache=True)
        except Exception as e:
            logger.warning(f"[景气度] ETF轮动数据获取失败: {e}")
            return {}
    
    if not etf_rotation_result or not etf_rotation_result.get("available"):
        return {}
    
    rankings = etf_rotation_result.get("rankings", [])
    total = len(rankings)
    if total == 0:
        return {}
    
    sectors = {}
    for i, r in enumerate(rankings):
        sector = r["sector"]
        chg_5d = r.get("chg_5d", 0)
        chg_20d = r.get("chg_20d", 0)
        accel = r.get("accel", 0)
        rank_pct = (i + 1) / total
        
        phase = _classify_cycle_phase(chg_5d, chg_20d, accel, rank_pct)
        score_adj = CYCLE_SCORE_MAP.get(phase, 0)
        
        sectors[sector] = {
            "phase": phase,
            "score_adj": score_adj,
            "chg_5d": round(chg_5d, 2),
            "chg_20d": round(chg_20d, 2),
            "accel": round(accel, 2),
            "rank_pct": round(rank_pct, 2),
        }
    
    _CYCLE_CACHE = {
        "computed": True,
        "sectors": sectors,
        "timestamp": datetime.datetime.now(),
    }
    
    # 日志输出景气度概览
    recovery_sectors = [s for s, v in sectors.items() if v["phase"] == "recovery"]
    expansion_sectors = [s for s, v in sectors.items() if v["phase"] == "expansion"]
    contraction_sectors = [s for s, v in sectors.items() if v["phase"] == "contraction"]
    logger.info(f"[景气度] 复苏{len(recovery_sectors)}个({','.join(recovery_sectors[:3]) or '无'}) "
                f"扩张{len(expansion_sectors)}个({','.join(expansion_sectors[:3]) or '无'}) "
                f"收缩{len(contraction_sectors)}个({','.join(contraction_sectors[:3]) or '无'})")
    
    return sectors


def get_sector_cycle_score(sector_name: str, etf_rotation_result: dict = None) -> int:
    """
    获取特定行业的景气度分数调整值
    
    参数:
        sector_name: 行业名称
        etf_rotation_result: ETF轮动结果（可选，避免重复计算）
    
    返回:
        int: 分数调整值（-3 ~ +3）
    """
    if not getattr(config, 'SECTOR_CYCLE_ENABLED', True):
        return 0
    
    sectors = compute_sector_cycles(etf_rotation_result)
    if not sectors:
        return 0
    
    info = sectors.get(sector_name, {})
    return info.get("score_adj", 0)


def get_sector_cycle_summary() -> str:
    """
    生成景气度摘要文本（供报告嵌入）
    
    返回:
        str: 格式化摘要
    """
    sectors = compute_sector_cycles()
    if not sectors:
        return "行业景气度数据暂不可用"
    
    # 按周期阶段分组
    phases = {"recovery": [], "expansion": [], "stable": [], "slowdown": [], "contraction": []}
    for sector, info in sectors.items():
        phase = info.get("phase", "stable")
        if phase in phases:
            phases[phase].append(sector)
    
    lines = ["【行业景气度周期】"]
    if phases["recovery"]:
        lines.append(f"  复苏期(+3): {', '.join(phases['recovery'])}")
    if phases["expansion"]:
        lines.append(f"  扩张期(+2): {', '.join(phases['expansion'])}")
    if phases["slowdown"]:
        lines.append(f"  放缓期(-2): {', '.join(phases['slowdown'])}")
    if phases["contraction"]:
        lines.append(f"  收缩期(-3): {', '.join(phases['contraction'])}")
    
    return "\n".join(lines)


def reset_cycle_cache():
    """重置景气度缓存（每次选股运行时调用）"""
    global _CYCLE_CACHE
    _CYCLE_CACHE = {"computed": False, "sectors": {}, "timestamp": None}
