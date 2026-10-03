# -*- coding: utf-8 -*-
"""
结论对象 Claim
==============
投研结论的最小载体：一条结论 = 文本 + 证据等级 + 报告期 + 来源 + 正反证据。

强制约束（由 validator 校验，本类只负责承载与自渲染）:
    - L1 事实必须有 period 与 source
    - L2 推断必须有推理链条（reasoning）与置信度（confidence）
    - L3 假设必须有验证条件（verify_condition）
    - 关键结论（买卖/评级/目标价）必须同时具备正反证据，否则降级 L3

设计原则:
    - 纯数据对象 + 自渲染，不做校验拦截（校验交给 validator.py）
    - is_decision 标记决策类结论；未显式标记时按关键词自动识别
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .evidence_tags import (
    LEVEL_FACT, LEVEL_INFERENCE, LEVEL_ASSUMPTION,
    normalize_level, render_tag_text, render_tag_html,
)

logger = logging.getLogger(__name__)

# 决策类结论关键词（买卖/评级/目标价），命中即视为"关键结论"，强制正反证据
_DECISION_KEYWORDS = (
    "买入", "卖出", "加仓", "减仓", "推荐", "评级", "目标价",
    "看多", "看空", "增持", "减持", "建仓", "清仓", "止损", "止盈",
)


@dataclass
class Claim:
    """
    投研结论对象。

    字段:
        text: 结论文本（如 "2026Q2 ROE 18.5%"、"建议买入"）
        level: 证据等级 1(事实)/2(推断)/3(假设)
        period: 报告期（L1 必填，如 "2026Q2"/"TTM"/"2026-08-15"）
        source: 来源（L1 必填，如 "公司公告"/"交易所"/"权威行情接口"）
        pro_evidence: 支持证据列表
        con_evidence: 反对证据列表
        verify_condition: L3 假设的验证条件（如何证实/证伪）
        reasoning: L2 推断的推理链条
        confidence: L2 推断的置信度 0~1
        is_decision: 是否为买卖/评级/目标价类关键结论
    """
    text: str
    level: int = LEVEL_ASSUMPTION
    period: str = ""
    source: str = ""
    pro_evidence: List[str] = field(default_factory=list)
    con_evidence: List[str] = field(default_factory=list)
    verify_condition: str = ""
    reasoning: str = ""
    confidence: Optional[float] = None
    is_decision: Optional[bool] = None

    def __post_init__(self):
        # 等级归一化：非法值降级 L3
        self.level = normalize_level(self.level)
        # is_decision 未显式指定时按关键词自动识别
        if self.is_decision is None:
            self.is_decision = self._detect_decision()

    def _detect_decision(self) -> bool:
        """按关键词自动判定是否为决策类关键结论。"""
        return any(kw in (self.text or "") for kw in _DECISION_KEYWORDS)

    @property
    def has_pro(self) -> bool:
        """是否有支持证据。"""
        return bool(self.pro_evidence)

    @property
    def has_con(self) -> bool:
        """是否有反对证据。"""
        return bool(self.con_evidence)

    @property
    def has_both_sides(self) -> bool:
        """关键结论要求：正反证据齐备。"""
        return self.has_pro and self.has_con

    def to_text(self) -> str:
        """
        渲染为纯文本（企业微信/日志）。

        格式:
            结论文本 [事实·L1｜报告期｜来源]
            ├ 支持: ...
            └ 反对: ...
            （L2 附推理链条+置信度；L3 附验证条件）
        """
        tag = render_tag_text(self.level, self.period, self.source)
        lines = [f"{self.text} {tag}"]
        if self.level == LEVEL_INFERENCE and self.reasoning:
            conf = f"，置信度{self.confidence:.0%}" if self.confidence is not None else ""
            lines.append(f"  推理: {self.reasoning}{conf}")
        if self.level == LEVEL_ASSUMPTION and self.verify_condition:
            lines.append(f"  验证条件: {self.verify_condition}")
        if self.pro_evidence:
            lines.append("  ├ 支持: " + "；".join(self.pro_evidence))
        if self.con_evidence:
            lines.append("  └ 反对: " + "；".join(self.con_evidence))
        return "\n".join(lines)

    def to_html(self) -> str:
        """渲染为 HTML 片段（邮件/dashboard）。"""
        tag = render_tag_html(self.level, self.period, self.source)
        parts = [f"<div style='margin:4px 0'>{self.text} {tag}"]
        if self.level == LEVEL_INFERENCE and self.reasoning:
            conf = f"，置信度{self.confidence:.0%}" if self.confidence is not None else ""
            parts.append(f"<div style='font-size:12px;color:#595959'>"
                         f"推理: {self.reasoning}{conf}</div>")
        if self.level == LEVEL_ASSUMPTION and self.verify_condition:
            parts.append(f"<div style='font-size:12px;color:#757575'>"
                         f"验证条件: {self.verify_condition}</div>")
        if self.pro_evidence:
            parts.append("<div style='font-size:12px;color:#2e7d32'>"
                         "├ 支持: " + "；".join(self.pro_evidence) + "</div>")
        if self.con_evidence:
            parts.append("<div style='font-size:12px;color:#c62828'>"
                         "└ 反对: " + "；".join(self.con_evidence) + "</div>")
        parts.append("</div>")
        return "".join(parts)

    def downgraded_copy(self, reason: str = "") -> "Claim":
        """
        返回一个降级为 L3 的副本（原对象不变）。

        用于 validator 对"只有单方证据的关键结论"等场景执行降级而非直接丢弃。
        """
        import copy
        new = copy.deepcopy(self)
        new.level = LEVEL_ASSUMPTION
        if reason and reason not in (new.verify_condition or ""):
            prefix = f"[降级原因: {reason}] "
            new.verify_condition = prefix + (new.verify_condition or "待补充验证条件")
        return new
