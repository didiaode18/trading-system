# -*- coding: utf-8 -*-
"""
事件催化剂双分级 CatalystGrader
===============================
把"政策/事件 → 产业链环节 → 具体公司"的传导链工程化，对每个事件同时给出
两个维度的分级，实现传闻与概念公司的决策隔离。

双分级维度:
    事件证据分级（可信度）: L1 正式公告 / L2 权威报道 / L3 市场传闻
    标的受益分级（相关度）: A 直接受益 / B 间接受益 / C 概念沾边

决策隔离规则（对应 SKILL 二）:
    1. L3 传闻只进"市场雷达/待核实"区，禁止生成买卖建议与条件单
    2. C 类概念公司禁止进入推荐与选股结果，B 类只能列入观察并标注推断
    3. 传导链每一环都必须标注证据等级
    4. A 类公司必须给出披露内容/规模指标/来源(公告日期)

设计原则:
    - 分级依据显式字段（source_type/disclosure/transmission），不猜测
    - 信息不足时一律取最保守等级（L3 / C），禁止编造来源
"""

import logging
from typing import List, Optional

from .evidence_tags import (
    LEVEL_FACT, LEVEL_INFERENCE, LEVEL_ASSUMPTION,
    TRADEABLE_EVENT_LEVELS, TRADEABLE_BENEFIT_GRADES,
)

logger = logging.getLogger(__name__)

# 事件来源类型 → 事件证据等级
_SOURCE_TYPE_TO_EVENT_LEVEL = {
    # L1 正式公告
    "exchange_announcement": LEVEL_FACT,   # 交易所公告
    "government_document": LEVEL_FACT,     # 政府部门文件
    "company_announcement": LEVEL_FACT,    # 公司公告
    # L2 权威报道
    "financial_media": LEVEL_INFERENCE,    # 主流财经媒体
    "industry_data": LEVEL_INFERENCE,      # 行业机构数据
    "broker_research": LEVEL_INFERENCE,    # 券商研报
    # L3 市场传闻
    "social_media": LEVEL_ASSUMPTION,      # 社媒消息
    "anonymous_tip": LEVEL_ASSUMPTION,     # 未署名爆料
    "rumor": LEVEL_ASSUMPTION,             # 据悉/网传
}

# 传闻类关键词（命中即视为 L3，优先级高于 source_type）
_RUMOR_KEYWORDS = ("据悉", "网传", "传闻", "消息称", "有消息称", "爆料", "或", "疑似")


class CatalystGrader:
    """事件催化剂分级器。"""

    def grade_event_level(self, source_type: str = "", text: str = "") -> int:
        """
        判定事件证据等级（可信度）。

        参数:
            source_type: 来源类型键（见 _SOURCE_TYPE_TO_EVENT_LEVEL）
            text: 事件文本，用于传闻关键词兜底检测
        返回:
            事件等级 1/2/3；未知来源一律 L3
        """
        # 传闻关键词优先：命中即 L3
        if text and any(kw in text for kw in _RUMOR_KEYWORDS):
            return LEVEL_ASSUMPTION
        return _SOURCE_TYPE_TO_EVENT_LEVEL.get(source_type, LEVEL_ASSUMPTION)

    def grade_benefit(self, has_disclosure: bool = False,
                      transmission_path: str = "",
                      concept_only: bool = False) -> str:
        """
        判定标的受益分级（相关度）。

        参数:
            has_disclosure: 是否已披露订单/产能/客户/收入（附公告来源）
            transmission_path: 业务链条传导路径说明（B 类必填）
            concept_only: 是否仅有概念标签/互动平台表态
        返回:
            "A"/"B"/"C"
        """
        if has_disclosure:
            return "A"
        if concept_only:
            return "C"
        if (transmission_path or "").strip():
            return "B"
        # 无披露、无传导路径、非纯概念 → 证据不足，最保守 C
        return "C"

    def grade_event(self, event: dict) -> dict:
        """
        对单个事件执行完整双分级 + 传导链标注。

        参数:
            event: 事件字典，期望字段:
                - title/text: 事件描述
                - source_type: 来源类型键
                - source: 来源说明（公告名/媒体名/日期）
                - chain: 传导链 [{"stage": 环节名, "level": 等级, "source": 来源}]
                - companies: [{"code","name","has_disclosure",
                               "transmission_path","concept_only","disclosure_detail"}]
        返回:
            {
              "event_level": 1/2/3, "event_level_name": str,
              "tradeable": bool, "reason": str,
              "chain": [...], "companies": [{...含 grade/tradeable}]
            }
        """
        event = event or {}
        text = event.get("text") or event.get("title") or ""
        source_type = event.get("source_type", "")
        source = event.get("source", "")

        event_level = self.grade_event_level(source_type, text)
        event_tradeable = event_level in TRADEABLE_EVENT_LEVELS

        # 传导链：每环标注证据等级，缺失等级按 L3
        chain = []
        for link in (event.get("chain") or []):
            lv = link.get("level", LEVEL_ASSUMPTION)
            chain.append({
                "stage": link.get("stage", ""),
                "level": lv,
                "source": link.get("source", ""),
            })

        # 公司分级
        companies = []
        for c in (event.get("companies") or []):
            grade = self.grade_benefit(
                has_disclosure=bool(c.get("has_disclosure")),
                transmission_path=c.get("transmission_path", ""),
                concept_only=bool(c.get("concept_only")),
            )
            # 可交易 = 事件可信(L1/L2) 且 标的 A/B 类
            c_tradeable = event_tradeable and grade in TRADEABLE_BENEFIT_GRADES
            item = {
                "code": c.get("code", ""),
                "name": c.get("name", ""),
                "grade": grade,
                "tradeable": c_tradeable,
                # A 类必须附披露明细；非 A 类清空，避免"裸结论"
                "disclosure_detail": (c.get("disclosure_detail", "")
                                      if grade == "A" else ""),
            }
            if grade == "B":
                item["transmission_path"] = c.get("transmission_path", "")
            # A 类缺披露明细 → 证据不足，降级 B（标注推断）
            if grade == "A" and not (item["disclosure_detail"] or "").strip():
                logger.info(f"[催化剂分级] {item['code']} 标记A类但缺披露明细，降级B")
                item["grade"] = "B"
                item["tradeable"] = event_tradeable  # B 仍可观察
            companies.append(item)

        reason = self._build_reason(event_level, event_tradeable, source)
        return {
            "event_level": event_level,
            "event_level_name": self._event_level_name(event_level),
            "source": source,
            "tradeable": event_tradeable,
            "reason": reason,
            "chain": chain,
            "companies": companies,
        }

    def filter_recommendable(self, graded: dict) -> List[dict]:
        """
        从分级结果中筛出"可进入推荐/选股"的标的。

        隔离规则:
            - 事件 L3 传闻 → 全部剔除（只进雷达区）
            - C 类概念公司 → 剔除
            - B 类 → 保留但标注"仅观察/推断"
            - A 类 → 保留
        """
        if not graded.get("tradeable"):
            return []
        result = []
        for c in graded.get("companies", []):
            if c.get("grade") == "C":
                continue  # 概念沾边禁止进入推荐
            item = dict(c)
            if c.get("grade") == "B":
                item["observe_only"] = True  # B 类只能观察并标注推断
            result.append(item)
        return result

    def is_radar_only(self, graded: dict) -> bool:
        """是否只能进"市场雷达/待核实"区（L3 传闻）。"""
        return graded.get("event_level") == LEVEL_ASSUMPTION

    def _event_level_name(self, level: int) -> str:
        from .evidence_tags import EVENT_LEVEL_NAMES
        return EVENT_LEVEL_NAMES.get(level, "市场传闻")

    def _build_reason(self, level: int, tradeable: bool, source: str) -> str:
        name = self._event_level_name(level)
        if tradeable:
            return f"事件可信（{name}），来源: {source or '已标注'}，可进入决策"
        return f"事件为{name}，来源不足（{source or '无'}），仅进雷达区，禁止买卖建议/条件单"
