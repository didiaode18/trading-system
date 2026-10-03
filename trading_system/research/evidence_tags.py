# -*- coding: utf-8 -*-
"""
证据等级定义与标签渲染
======================
投研证据分级体系的"渲染层"：统一 L1/L2/L3 三级标签的文本与 HTML 呈现，
避免标签样式散落在各报告模块中硬编码。

三级标注规范（适用于系统全部文字结论）:
    [事实·L1] 可核验的客观数据/已发生事件，必须注明报告期、发布日期、来源
    [推断·L2] 基于 L1 数据的逻辑推理，必须写明推理链条并给出置信度
    [假设·L3] 尚未证实的判断/预测，必须显式标注并列出验证条件

事件催化剂双分级:
    事件证据分级（可信度）: L1 正式公告 / L2 权威报道 / L3 市场传闻
    标的受益分级（相关度）: A 直接受益 / B 间接受益 / C 概念沾边

设计原则:
    - 纯渲染，无副作用，不依赖数据源
    - 未知等级一律降级为 L3（最保守），禁止编造高等级标签
"""

import logging

logger = logging.getLogger(__name__)

# ============================================================
# 一、证据等级常量
# ============================================================
LEVEL_FACT = 1          # 事实：可核验客观数据/已发生事件
LEVEL_INFERENCE = 2     # 推断：基于 L1 数据的逻辑推理
LEVEL_ASSUMPTION = 3    # 假设：尚未证实的判断/预测

# 等级中文名
LEVEL_NAMES = {
    LEVEL_FACT: "事实",
    LEVEL_INFERENCE: "推断",
    LEVEL_ASSUMPTION: "假设",
}

# 文本标签（纯文本 / 企业微信）
LEVEL_TAGS_TEXT = {
    LEVEL_FACT: "[事实·L1]",
    LEVEL_INFERENCE: "[推断·L2]",
    LEVEL_ASSUMPTION: "[假设·L3]",
}

# HTML 标签配色（事实=蓝、推断=橙、假设=灰）
_LEVEL_COLORS = {
    LEVEL_FACT: "#1565c0",
    LEVEL_INFERENCE: "#e65100",
    LEVEL_ASSUMPTION: "#757575",
}

# ============================================================
# 二、事件证据分级（可信度）与标的受益分级（相关度）
# ============================================================
EVENT_LEVEL_NAMES = {
    LEVEL_FACT: "正式公告",       # 交易所公告/政府文件/公司公告
    LEVEL_INFERENCE: "权威报道",  # 主流财经媒体/行业机构/券商研报
    LEVEL_ASSUMPTION: "市场传闻",  # 社媒/未署名爆料/"据悉·网传"
}

BENEFIT_GRADE_NAMES = {
    "A": "直接受益",   # 已披露订单/产能/客户/收入，附公告来源
    "B": "间接受益",   # 业务链条相关但无直接披露，需说明传导路径
    "C": "概念沾边",   # 仅概念标签或互动平台表态，无实质业务证据
}

# 决策隔离：仅 L1/L2 事件 + A/B 类标的可进入交易决策
TRADEABLE_EVENT_LEVELS = {LEVEL_FACT, LEVEL_INFERENCE}
TRADEABLE_BENEFIT_GRADES = {"A", "B"}


def normalize_level(level) -> int:
    """
    归一化证据等级，非法值一律降级为 L3（最保守）。

    参数:
        level: 期望 1/2/3，接受 int 或可转 int 的字符串
    返回:
        合法的等级整数（1/2/3）
    """
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return LEVEL_ASSUMPTION
    return lv if lv in LEVEL_NAMES else LEVEL_ASSUMPTION


def render_tag_text(level, period: str = "", source: str = "") -> str:
    """
    渲染纯文本证据标签。

    格式: [事实·L1｜报告期｜来源]（缺项自动省略对应分隔段）

    参数:
        level: 证据等级 1/2/3
        period: 报告期（如 "2026Q2"、"TTM"）
        source: 来源（如 "公司公告 2026-08-15"）
    返回:
        纯文本标签字符串
    """
    lv = normalize_level(level)
    parts = [LEVEL_TAGS_TEXT[lv].strip("[]")]
    if period:
        parts.append(str(period))
    if source:
        parts.append(str(source))
    return "[" + "｜".join(parts) + "]"


def render_tag_html(level, period: str = "", source: str = "") -> str:
    """
    渲染 HTML 证据标签（带配色徽章）。

    参数:
        level: 证据等级 1/2/3
        period: 报告期
        source: 来源
    返回:
        HTML <span> 徽章字符串
    """
    lv = normalize_level(level)
    color = _LEVEL_COLORS.get(lv, _LEVEL_COLORS[LEVEL_ASSUMPTION])
    inner = LEVEL_NAMES[lv] + "·L%d" % lv
    detail = []
    if period:
        detail.append(_escape(str(period)))
    if source:
        detail.append(_escape(str(source)))
    detail_html = ("｜" + "｜".join(detail)) if detail else ""
    return (f"<span style='color:{color};font-weight:600'>"
            f"[{inner}{detail_html}]</span>")


def render_benefit_badge_text(grade: str) -> str:
    """渲染标的受益分级纯文本徽章，如 [受益·A 直接受益]。"""
    g = (grade or "C").upper()
    if g not in BENEFIT_GRADE_NAMES:
        g = "C"
    return f"[受益·{g} {BENEFIT_GRADE_NAMES[g]}]"


def render_benefit_badge_html(grade: str) -> str:
    """渲染标的受益分级 HTML 徽章（A=绿、B=橙、C=灰）。"""
    g = (grade or "C").upper()
    if g not in BENEFIT_GRADE_NAMES:
        g = "C"
    color = {"A": "#2e7d32", "B": "#e65100", "C": "#757575"}.get(g, "#757575")
    return (f"<span style='color:{color};font-weight:600'>"
            f"[受益·{g} {BENEFIT_GRADE_NAMES[g]}]</span>")


def _escape(text: str) -> str:
    """HTML 转义，防止来源文本中的尖括号破坏结构。"""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
