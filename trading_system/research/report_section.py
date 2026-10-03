# -*- coding: utf-8 -*-
"""
证据分级版报告区块（样板）
==========================
把 Claim 结论与 CatalystGrader 分级结果渲染为可嵌入盘后报告/邮件的区块，
作为"样板先行"的第一个落地示例，供人工确认标签样式与颗粒度后再推广。

区块结构:
    一、关键结论（经 validator 校验/降级，每条带 L1/L2/L3 标签+正反证据）
    二、事件催化剂（双分级：事件可信度 × 标的受益度，含传导链）
    三、市场雷达/待核实（L3 传闻隔离区，无任何买卖建议）

安全约束:
    - 无内容/异常一律返回空字符串，绝不阻断报告
    - 传闻只进雷达区；C 类概念公司不出现在推荐位
"""

import logging
from typing import List, Optional

from .evidence_tags import (
    LEVEL_ASSUMPTION, render_benefit_badge_html, render_benefit_badge_text,
)
from .claim import Claim
from .validator import EvidenceValidator
from .catalyst import CatalystGrader

logger = logging.getLogger(__name__)

_SECTION_TITLE = "证据分级研究（样板区块）"


def build_evidence_section(claims: Optional[List[Claim]] = None,
                           events: Optional[List[dict]] = None,
                           as_html: bool = True) -> str:
    """
    生成证据分级报告区块。

    流程:
        1. validator.enforce(claims) → 拦截裸结论、降级单方证据结论
        2. CatalystGrader.grade_event(每个事件) → 双分级
        3. 按"关键结论 / 事件催化剂 / 市场雷达"三区渲染

    参数:
        claims: Claim 结论列表（可为空）
        events: 事件字典列表（可为空，结构见 catalyst.grade_event）
        as_html: True=HTML（邮件/dashboard），False=纯文本（企业微信）
    返回:
        区块字符串；无有效内容时返回空字符串
    """
    try:
        validator = EvidenceValidator()
        grader = CatalystGrader()

        # 1. 校验并处置结论
        passed_claims, violations = validator.enforce(claims or [])
        # 2. 事件双分级
        graded_events = [grader.grade_event(ev) for ev in (events or [])]
        # 分离：可交易事件 vs 雷达区（L3 传闻）
        decision_events = [g for g in graded_events if not grader.is_radar_only(g)]
        radar_events = [g for g in graded_events if grader.is_radar_only(g)]

        # 无任何有效内容 → 静默降级
        if not passed_claims and not decision_events and not radar_events:
            return ""

        if as_html:
            return _render_html(passed_claims, decision_events,
                                radar_events, violations)
        return _render_text(passed_claims, decision_events,
                            radar_events, violations)
    except Exception as e:
        logger.warning(f"[证据分级区块] 渲染失败，静默降级: {e}")
        return ""


# ============================================================
# HTML 渲染
# ============================================================
def _render_html(claims, decision_events, radar_events, violations) -> str:
    parts = [f"<div style='margin:10px 0;padding:10px;"
             f"border-left:4px solid #1565c0;background:#f7f9fc'>"]
    parts.append(f"<div style='font-size:15px;font-weight:700;"
                 f"color:#1565c0'>{_SECTION_TITLE}</div>")

    # 一、关键结论
    if claims:
        parts.append("<div style='font-size:13px;font-weight:600;"
                     "margin-top:8px'>一、关键结论（带证据等级）</div>")
        for c in claims:
            parts.append(c.to_html())

    # 二、事件催化剂
    if decision_events:
        parts.append("<div style='font-size:13px;font-weight:600;"
                     "margin-top:8px'>二、事件催化剂（双分级）</div>")
        for g in decision_events:
            parts.append(_event_html(g))

    # 三、市场雷达/待核实（L3 隔离区）
    if radar_events:
        parts.append("<div style='font-size:13px;font-weight:600;"
                     "margin-top:8px;color:#757575'>"
                     "三、市场雷达 / 待核实（传闻隔离区，非交易依据）</div>")
        for g in radar_events:
            parts.append(_radar_event_html(g))

    # 校验脚注：被拦截/降级的结论数
    if violations:
        blocked = sum(1 for v in violations if v["action"] == "block")
        downgraded = sum(1 for v in violations if v["action"] == "downgrade")
        parts.append(f"<div style='font-size:11px;color:#9e9e9e;margin-top:6px'>"
                     f"证据校验: 拦截裸结论 {blocked} 条，降级 {downgraded} 条"
                     f"（无来源/无反证/口径混用）</div>")

    parts.append("</div>")
    return "".join(parts)


def _event_html(graded: dict) -> str:
    """渲染单个可交易事件（含传导链 + 公司分级）。"""
    head = (f"<div style='margin:4px 0'>"
            f"<span style='font-weight:600'>[{graded['event_level_name']}·L{graded['event_level']}]</span> "
            f"{graded.get('source', '')}</div>")
    # 传导链
    chain_html = ""
    if graded.get("chain"):
        steps = " → ".join(
            f"{link['stage']}<sub>L{link['level']}</sub>"
            for link in graded["chain"])
        chain_html = (f"<div style='font-size:12px;color:#595959'>"
                      f"传导链: {steps}</div>")
    # 公司分级
    comp_html = ""
    comps = graded.get("companies", [])
    if comps:
        items = []
        for c in comps:
            badge = render_benefit_badge_html(c["grade"])
            note = ""
            if c.get("grade") == "A" and c.get("disclosure_detail"):
                note = f"（{c['disclosure_detail']}）"
            elif c.get("grade") == "B":
                note = f"（传导: {c.get('transmission_path', '')}，仅观察）"
            tag = "" if c.get("tradeable") else " <span style='color:#c62828'>[禁推荐]</span>"
            items.append(f"{c.get('name', '')}{c.get('code', '')} {badge}{note}{tag}")
        comp_html = (f"<div style='font-size:12px'>"
                     + "；".join(items) + "</div>")
    return head + chain_html + comp_html


def _radar_event_html(graded: dict) -> str:
    """渲染雷达区传闻（无公司分级、无买卖建议）。"""
    return (f"<div style='margin:3px 0;font-size:12px;color:#757575'>"
            f"[传闻·L3] {graded.get('source', '')} — {graded.get('reason', '')}"
            f"</div>")


# ============================================================
# 纯文本渲染
# ============================================================
def _render_text(claims, decision_events, radar_events, violations) -> str:
    lines = [f"◆ {_SECTION_TITLE}"]

    if claims:
        lines.append("一、关键结论（带证据等级）")
        for c in claims:
            lines.append(c.to_text())

    if decision_events:
        lines.append("二、事件催化剂（双分级）")
        for g in decision_events:
            lines.append(_event_text(g))

    if radar_events:
        lines.append("三、市场雷达 / 待核实（传闻隔离区，非交易依据）")
        for g in radar_events:
            lines.append(f"  [传闻·L3] {g.get('source', '')} — {g.get('reason', '')}")

    if violations:
        blocked = sum(1 for v in violations if v["action"] == "block")
        downgraded = sum(1 for v in violations if v["action"] == "downgrade")
        lines.append(f"（证据校验: 拦截 {blocked} 条，降级 {downgraded} 条）")

    return "\n".join(lines)


def _event_text(graded: dict) -> str:
    lines = [f"  [{graded['event_level_name']}·L{graded['event_level']}] "
             f"{graded.get('source', '')}"]
    if graded.get("chain"):
        steps = " → ".join(f"{link['stage']}(L{link['level']})"
                           for link in graded["chain"])
        lines.append(f"    传导链: {steps}")
    for c in graded.get("companies", []):
        badge = render_benefit_badge_text(c["grade"])
        note = ""
        if c.get("grade") == "A" and c.get("disclosure_detail"):
            note = f"（{c['disclosure_detail']}）"
        elif c.get("grade") == "B":
            note = f"（传导: {c.get('transmission_path', '')}，仅观察）"
        tag = "" if c.get("tradeable") else " [禁推荐]"
        lines.append(f"    {c.get('name', '')}{c.get('code', '')} {badge}{note}{tag}")
    return "\n".join(lines)
