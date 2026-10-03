# -*- coding: utf-8 -*-
"""
证据校验器 EvidenceValidator
============================
报告生成时的"投研纪律闸门"：扫描全部结论，对无标签/无来源/无反证/口径混用
的结论执行拦截或降级，确保报告中不出现"裸结论"。

硬性规则（对应 SKILL 一）:
    1. L1 事实缺来源 → 拦截（不得作为事实输出）
    2. L1 事实缺报告期 → 拦截
    3. 关键结论（买卖/评级/目标价）只有单方证据 → 降级 L3
    4. L2 推断缺推理链条或置信度 → 降级 L3
    5. L3 假设缺验证条件 → 拦截（宁可不输出）
    6. 报告期口径混用（季度/年度/TTM 同串） → 标记违规

设计原则:
    - 校验只产出"违规清单 + 处置后的结论"，不抛异常、不阻断报告
    - 无法通过校验的结论宁可丢弃，绝不编造来源或反证
"""

import logging
import re
from typing import List, Tuple

from .evidence_tags import (
    LEVEL_FACT, LEVEL_INFERENCE, LEVEL_ASSUMPTION,
)
from .claim import Claim

logger = logging.getLogger(__name__)

# 报告期口径标记：季度(Q1-Q4/一季报...)、年度(年报/全年)、滚动(TTM)
_PERIOD_PATTERNS = {
    "quarter": re.compile(r"(Q[1-4]|[一二三四]季报|单季|季度)"),
    "annual": re.compile(r"(年报|全年|年度|FY\d{2,4})"),
    "ttm": re.compile(r"(TTM|滚动|近12个月|近十二个月)"),
}


class Violation:
    """单条违规记录。"""

    def __init__(self, claim_text: str, code: str, message: str, action: str):
        """
        参数:
            claim_text: 违规结论文本
            code: 违规代码（no_source/no_period/one_sided/...）
            message: 违规说明
            action: 处置动作 "block"(拦截) 或 "downgrade"(降级)
        """
        self.claim_text = claim_text
        self.code = code
        self.message = message
        self.action = action

    def to_dict(self) -> dict:
        return {
            "claim": self.claim_text,
            "code": self.code,
            "message": self.message,
            "action": self.action,
        }

    def __repr__(self):
        return f"<Violation {self.code} action={self.action} claim={self.claim_text[:20]!r}>"


class EvidenceValidator:
    """报告结论证据校验器。"""

    def validate_claim(self, claim: Claim) -> List[Violation]:
        """
        校验单条结论，返回违规清单（可能为空）。

        参数:
            claim: 待校验的 Claim 对象
        返回:
            Violation 列表；空列表表示通过
        """
        violations = []
        text = claim.text or ""
        lv = claim.level

        # 规则1/2: L1 事实必须有来源与报告期
        if lv == LEVEL_FACT:
            if not (claim.source or "").strip():
                violations.append(Violation(
                    text, "no_source",
                    "L1 事实缺来源，不得作为事实输出", "block"))
            if not (claim.period or "").strip():
                violations.append(Violation(
                    text, "no_period",
                    "L1 事实缺报告期", "block"))

        # 规则4: L2 推断必须有推理链条与置信度
        if lv == LEVEL_INFERENCE:
            if not (claim.reasoning or "").strip():
                violations.append(Violation(
                    text, "no_reasoning",
                    "L2 推断缺推理链条，降级为假设", "downgrade"))
            if claim.confidence is None:
                violations.append(Violation(
                    text, "no_confidence",
                    "L2 推断缺置信度，降级为假设", "downgrade"))

        # 规则5: L3 假设必须有验证条件
        if lv == LEVEL_ASSUMPTION:
            if not (claim.verify_condition or "").strip():
                violations.append(Violation(
                    text, "no_verify_condition",
                    "L3 假设缺验证条件，宁可不输出", "block"))

        # 规则3: 关键结论必须正反证据齐备，否则降级
        if claim.is_decision and not claim.has_both_sides:
            missing = []
            if not claim.has_pro:
                missing.append("支持证据")
            if not claim.has_con:
                missing.append("反对证据")
            violations.append(Violation(
                text, "one_sided",
                f"关键结论缺{'/'.join(missing)}，降级为假设", "downgrade"))

        # 规则6: 报告期口径混用检查
        mixed = self._detect_mixed_period(claim.period)
        if mixed:
            violations.append(Violation(
                text, "mixed_period",
                f"报告期口径混用（{'/'.join(mixed)}），须分开标注", "block"))

        return violations

    def _detect_mixed_period(self, period: str) -> List[str]:
        """检测同一报告期字符串是否混用了多种口径。"""
        if not period:
            return []
        hit = [name for name, pat in _PERIOD_PATTERNS.items()
               if pat.search(period)]
        return hit if len(hit) >= 2 else []

    def validate_report(self, sections) -> List[dict]:
        """
        校验整份报告的全部结论，返回违规清单（dict 形式，供日志/报告展示）。

        参数:
            sections: Claim 列表，或 {区块名: [Claim,...]} 字典
        返回:
            违规记录 dict 列表（含所属区块）
        """
        claims = self._flatten(sections)
        all_violations = []
        for claim in claims:
            for v in self.validate_claim(claim):
                d = v.to_dict()
                all_violations.append(d)
        if all_violations:
            logger.warning(f"[证据校验] 发现 {len(all_violations)} 条违规结论，"
                           f"已按规则拦截/降级")
        return all_violations

    def enforce(self, sections) -> Tuple[List[Claim], List[dict]]:
        """
        校验并处置：返回"可安全输出的结论"与"违规清单"。

        处置逻辑:
            - 命中任一 block 违规 → 丢弃该结论
            - 仅命中 downgrade 违规 → 降级为 L3 后保留
              （降级后若 L3 仍缺验证条件，则补默认验证条件，避免二次拦截）

        参数:
            sections: Claim 列表，或 {区块名: [Claim,...]} 字典
        返回:
            (处置后的 Claim 列表, 违规记录 dict 列表)
        """
        claims = self._flatten(sections)
        passed: List[Claim] = []
        violations: List[dict] = []

        for claim in claims:
            vs = self.validate_claim(claim)
            for v in vs:
                violations.append(v.to_dict())

            if not vs:
                passed.append(claim)
                continue

            has_block = any(v.action == "block" for v in vs)
            if has_block:
                # 拦截：宁可不输出
                logger.info(f"[证据校验] 拦截裸结论: {claim.text[:30]!r} "
                            f"({[v.code for v in vs]})")
                continue

            # 仅降级：降为 L3 并确保有验证条件
            reasons = "；".join(v.message for v in vs)
            new_claim = claim.downgraded_copy(reason=reasons)
            if not (new_claim.verify_condition or "").strip():
                new_claim.verify_condition = "[降级] 待补充验证条件"
            passed.append(new_claim)

        return passed, violations

    def _flatten(self, sections) -> List[Claim]:
        """把 sections（list 或 dict）摊平为 Claim 列表。"""
        claims: List[Claim] = []
        if sections is None:
            return claims
        if isinstance(sections, dict):
            for _name, items in sections.items():
                claims.extend(self._flatten(items))
        elif isinstance(sections, (list, tuple)):
            for item in sections:
                if isinstance(item, Claim):
                    claims.append(item)
                elif isinstance(item, (list, tuple, dict)):
                    claims.extend(self._flatten(item))
        elif isinstance(sections, Claim):
            claims.append(sections)
        return claims
