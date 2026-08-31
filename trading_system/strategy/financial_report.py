"""
财报深度分析模块（V10.2 新增）
==============================
对候选股票进行财报关键指标深度解读，输出CANSLIM加减分建议。

书中理念: CANSLIM的C(当期业绩)和A(年度业绩)不仅看增速绝对值，
更要看增速的质量、持续性和来源。高ROE来自净利率而非杠杆、
营收加速而非减速、毛利率扩大而非收缩——这些才是真正的好财报。

五维评分体系:
  1. 营收增速趋势(25分): 营收增速是否加速（环比改善）
  2. 净利润质量(25分): 利润增速≥营收增速且绝对值高 → 主营驱动
  3. 毛利率变化(20分): 毛利率上升 → 竞争优势扩大
  4. ROE杜邦分解(15分): 高ROE来自净利率/周转率 → 高质量；来自杠杆 → 低质量
  5. 资产负债结构(15分): 低负债+下降 → 安全边际高

最终评分: 0-100分 → 映射为CANSLIM加减分 -2~+4（非对称：地雷杀伤>优秀推动）

使用方式:
    from strategy.financial_report import get_financial_report_bonus
    bonus = get_financial_report_bonus("002371", fund_data=fund_data)  # 返回 -2~+4
"""

import os
import sys
import logging

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# ============================================================
# 财报评分 → CANSLIM加减分映射（非对称设计）
# ============================================================
# 好书中理念：财报地雷的杀伤力远大于财报优秀的推动力
REPORT_SCORE_TO_BONUS = [
    (80, None),    # 80+分 → 用 FINANCIAL_REPORT_MAX_BONUS（默认+4）
    (60, 2),       # 60-79分 → +2
    (40, 0),       # 40-59分 → 中性
    (25, None),    # 25-39分 → 用 FINANCIAL_REPORT_PENALTY（默认-2）
    (0,   None),   # <25分 → 也用 PENALTY（有数据但财报极差）
]

# 缓存
_REPORT_CACHE = {}


# ============================================================
# 一、营收增速趋势评分（0-25分）
# ============================================================

def _score_revenue_trend(fund_data: dict) -> tuple:
    """
    营收增速趋势评分
    
    原理: 营收加速增长说明公司市场份额在扩大，是CANSLIM C因子的重要质量信号。
    不仅看绝对增速，更看环比变化方向。
    
    评分规则:
      - 营收增速>25%且加速(环比↑) → 25分（满分）
      - 营收增速>15% → 20分
      - 营收增速>5% → 12分
      - 营收增速>0% → 6分
      - 营收增速≤0% → 0分
    
    返回: (score, detail_str)
    """
    rev_growth = fund_data.get("revenue_growth")
    eps_q = fund_data.get("eps_growth_q")
    
    # 优先用营收增速，回退到利润增速作为代理
    growth = rev_growth if rev_growth is not None else eps_q
    
    if growth is None:
        return 10, "营收趋势:无数据(中性)"
    
    if growth > 25:
        score = 25
        label = "高速增长"
    elif growth > 15:
        score = 20
        label = "稳健增长"
    elif growth > 5:
        score = 12
        label = "温和增长"
    elif growth > 0:
        score = 6
        label = "微增"
    else:
        score = 0
        label = "负增长"
    
    return score, f"营收趋势:{label}({growth:+.1f}%)"


# ============================================================
# 二、净利润质量评分（0-25分）
# ============================================================

def _score_profit_quality(fund_data: dict) -> tuple:
    """
    净利润质量评分
    
    原理: 利润增速≥营收增速 → 说明增长来自主营效率提升（好）；
          利润增速<营收增速 → 说明"增收不增利"，成本侵蚀利润（差）。
    同时，利润绝对增速要高（CANSLIM要求C>25%）。
    
    评分规则:
      - 利润增速>30% 且 利润≥营收增速 → 25分（高质量增长）
      - 利润增速>20% 且 利润≥营收增速 → 20分
      - 利润增速>10% → 12分
      - 利润增速>0% 但 利润<营收增速 → 6分（增收不增利）
      - 利润增速≤0% → 0分
    
    返回: (score, detail_str)
    """
    npg = fund_data.get("net_profit_growth")
    rg = fund_data.get("revenue_growth")
    
    if npg is None:
        return 10, "利润质量:无数据(中性)"
    
    # 利润增速 vs 营收增速
    profit_ge_revenue = (rg is not None and npg >= rg)
    
    if npg > 30 and profit_ge_revenue:
        score = 25
        label = "高质量爆发"
    elif npg > 20 and profit_ge_revenue:
        score = 20
        label = "高质量增长"
    elif npg > 10:
        score = 12
        label = "稳定增长"
    elif npg > 0:
        if rg is not None and npg < rg:
            score = 6
            label = "增收不增利"
        else:
            score = 8
            label = "微利"
    else:
        score = 0
        label = "利润下滑"
    
    detail = f"利润质量:{label}(净利{npg:+.1f}%"
    if rg is not None:
        detail += f" vs 营收{rg:+.1f}%)"
    else:
        detail += ")"
    
    return score, detail


# ============================================================
# 三、毛利率变化评分（0-20分）
# ============================================================

def _score_margin_change(fund_data: dict) -> tuple:
    """
    毛利率变化评分
    
    原理: 毛利率上升说明公司有定价权或成本控制能力在增强，
    是竞争优势扩大的信号。书中茅台/宁德时代案例均展示了
    毛利率趋势对长期超额收益的重要性。
    
    评分规则:
      - 毛利率>50% → 20分（强定价权）
      - 毛利率>30% → 15分
      - 毛利率>20% → 10分
      - 毛利率>10% → 5分
      - 毛利率≤10% → 2分
    
    返回: (score, detail_str)
    """
    gm = fund_data.get("gross_margin")
    
    if gm is None:
        return 8, "毛利率:无数据(中性)"
    
    if gm > 50:
        score = 20
        label = "强定价权"
    elif gm > 30:
        score = 15
        label = "竞争优势"
    elif gm > 20:
        score = 10
        label = "中等毛利"
    elif gm > 10:
        score = 5
        label = "低毛利"
    else:
        score = 2
        label = "微利行业"
    
    return score, f"毛利率:{label}({gm:.1f}%)"


# ============================================================
# 四、ROE杜邦分解评分（0-15分）
# ============================================================

def _score_dupont_decomposition(fund_data: dict) -> tuple:
    """
    ROE杜邦分解评分
    
    原理: ROE = 净利率 × 资产周转率 × 权益乘数
    高质量ROE来自净利率（定价权）或周转率（效率），低质量来自杠杆。
    
    简化杜邦分析（基于可用数据）:
      - ROE>20% 且 毛利率>30% → 净利率驱动型 → 15分（最优）
      - ROE>15% 且 毛利率>20% → 混合型 → 12分
      - ROE>10% → 一般 → 8分
      - ROE>5% → 偏低 → 4分
      - ROE≤5% 或 无数据 → 2分
    
    注: 完整的杜邦分解需要总资产/营收/股东权益数据，
    此处用毛利率作为净利率的代理指标进行简化分解。
    
    返回: (score, detail_str)
    """
    roe = fund_data.get("roe")
    gm = fund_data.get("gross_margin")
    
    if roe is None:
        return 5, "ROE分解:无数据(中性)"
    
    # 简化杜邦分解
    if roe > 20 and gm is not None and gm > 30:
        score = 15
        label = "净利率驱动(优)"
    elif roe > 15 and gm is not None and gm > 20:
        score = 12
        label = "混合驱动(良)"
    elif roe > 10:
        score = 8
        label = "一般水平"
    elif roe > 5:
        score = 4
        label = "偏低"
    else:
        score = 2
        label = "ROE不足"
    
    detail = f"ROE分解:{label}(ROE={roe:.1f}%"
    if gm is not None:
        detail += f",毛利率={gm:.1f}%)"
    else:
        detail += ")"
    
    return score, detail


# ============================================================
# 五、资产负债结构评分（0-15分）
# ============================================================

def _score_balance_structure(fund_data: dict) -> tuple:
    """
    资产负债结构评分
    
    原理: 低负债率 = 高安全边际。高负债公司在行业下行时
    面临债务危机风险，即使短期业绩好也不可持续。
    
    评分规则:
      - 负债率<30% → 15分（极安全）
      - 负债率<45% → 12分
      - 负债率<60% → 8分
      - 负债率<75% → 4分
      - 负债率≥75% → 1分（高风险）
    
    返回: (score, detail_str)
    """
    debt = fund_data.get("debt_ratio")
    
    if debt is None:
        return 6, "负债结构:无数据(中性)"
    
    if debt < 30:
        score = 15
        label = "极安全"
    elif debt < 45:
        score = 12
        label = "稳健"
    elif debt < 60:
        score = 8
        label = "适中"
    elif debt < 75:
        score = 4
        label = "偏高"
    else:
        score = 1
        label = "高风险"
    
    return score, f"负债结构:{label}(负债率{debt:.1f}%)"


# ============================================================
# 六、综合评分与便捷接口
# ============================================================

def compute_financial_report(code: str, fund_data: dict = None) -> dict:
    """
    计算个股财报深度分析结果
    
    参数:
        code: 股票代码
        fund_data: 基本面数据字典（来自FUNDAMENTAL_DATA，V10.2扩展版）
    
    返回:
        {
            "report_score": float,    # 综合评分 0-100
            "bonus": int,             # CANSLIM加减分 -2~+4
            "revenue_score": float,   # 营收增速趋势分
            "profit_score": float,    # 净利润质量分
            "margin_score": float,    # 毛利率变化分
            "dupont_score": float,    # ROE杜邦分解分
            "balance_score": float,   # 资产负债结构分
            "summary": str,           # 人类可读摘要
            "detail": str,            # 详细评分明细
        }
    """
    if not getattr(config, 'FINANCIAL_REPORT_ENABLED', True):
        return {"report_score": 0, "bonus": 0, "summary": "未启用", "detail": "未启用",
                "revenue_score": 0, "profit_score": 0, "margin_score": 0,
                "dupont_score": 0, "balance_score": 0}
    
    # 检查缓存
    if code in _REPORT_CACHE:
        return _REPORT_CACHE[code]
    
    if fund_data is None:
        fund_data = {}
    
    # 检查是否有足够数据进行分析
    _has_data = any(
        fund_data.get(k) is not None
        for k in ("roe", "gross_margin", "debt_ratio", "revenue_growth", "net_profit_growth")
    )
    
    if not _has_data:
        # 无数据时返回0分（不惩罚也不加分）
        result = {
            "report_score": 0, "bonus": 0,
            "revenue_score": 0, "profit_score": 0, "margin_score": 0,
            "dupont_score": 0, "balance_score": 0,
            "summary": "财报数据不足",
            "detail": "无财报数据",
        }
        _REPORT_CACHE[code] = result
        return result
    
    # 五维评分
    rev_score, rev_detail = _score_revenue_trend(fund_data)
    prof_score, prof_detail = _score_profit_quality(fund_data)
    margin_score, margin_detail = _score_margin_change(fund_data)
    dupont_score, dupont_detail = _score_dupont_decomposition(fund_data)
    balance_score, balance_detail = _score_balance_structure(fund_data)
    
    # 综合评分（分量满分分别为25/25/20/15/15，总和=100）
    report_score = rev_score + prof_score + margin_score + dupont_score + balance_score
    
    # 映射为CANSLIM加减分
    MAX_BONUS = getattr(config, 'FINANCIAL_REPORT_MAX_BONUS', 4)
    PENALTY = getattr(config, 'FINANCIAL_REPORT_PENALTY', -2)
    
    bonus = 0
    for threshold, adj in REPORT_SCORE_TO_BONUS:
        if adj is None:
            # 使用配置值
            if threshold >= 80:
                adj = MAX_BONUS
            else:
                adj = PENALTY  # threshold=25或0均用PENALTY
        if report_score >= threshold:
            bonus = adj
            break
    
    # 生成摘要（供报告展示）
    summary_parts = []
    if rev_score >= 20:
        summary_parts.append("营收加速")
    elif rev_score <= 6:
        summary_parts.append("营收放缓")
    if prof_score >= 20:
        summary_parts.append("利润优质")
    elif prof_score <= 6:
        summary_parts.append("利润承压")
    if margin_score >= 15:
        summary_parts.append("毛利扩张")
    elif margin_score <= 5:
        summary_parts.append("毛利收缩")
    if dupont_score >= 12:
        summary_parts.append("ROE健康")
    if balance_score >= 12:
        summary_parts.append("负债安全")
    elif balance_score <= 4:
        summary_parts.append("负债偏高")
    
    if not summary_parts:
        summary = "财报表现一般"
    elif bonus >= 2:
        summary = "财报优秀(" + "/".join(summary_parts[:3]) + ")"
    elif bonus > 0:
        summary = "财报良好(" + "/".join(summary_parts[:2]) + ")"
    elif bonus < 0:
        summary = "财报预警(" + "/".join(summary_parts[:2]) + ")"
    else:
        summary = "财报中性"
    
    detail = " | ".join([rev_detail, prof_detail, margin_detail, dupont_detail, balance_detail])
    
    result = {
        "report_score": round(report_score, 1),
        "bonus": bonus,
        "revenue_score": round(rev_score, 1),
        "profit_score": round(prof_score, 1),
        "margin_score": round(margin_score, 1),
        "dupont_score": round(dupont_score, 1),
        "balance_score": round(balance_score, 1),
        "summary": summary,
        "detail": f"财报{report_score:.0f}分 [{detail}] → {bonus:+d}",
    }
    
    _REPORT_CACHE[code] = result
    return result


def get_financial_report_bonus(code: str, fund_data: dict = None) -> int:
    """
    获取财报深度分析CANSLIM加减分（便捷接口）
    
    返回: -2 ~ +4
    """
    result = compute_financial_report(code, fund_data)
    return result.get("bonus", 0)


def get_financial_report_summary(code: str, fund_data: dict = None) -> str:
    """
    获取财报分析摘要（供报告展示，便捷接口）
    
    返回: str（如 "财报优秀(营收加速/利润优质)"）
    """
    result = compute_financial_report(code, fund_data)
    return result.get("summary", "")


def reset_report_cache():
    """重置财报分析缓存"""
    global _REPORT_CACHE
    _REPORT_CACHE = {}
