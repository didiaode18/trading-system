# -*- coding: utf-8 -*-
"""
news_monitor / event_calendar → CatalystGrader 桥接适配器
=========================================================
把现有新闻风控扫描结果（news_monitor.scan_news_risk 输出）转换为
证据分级事件，使"未分级事件不进入报告正文"，传闻自动隔离到雷达区。

设计原则（样板先行、非侵入）:
    - 只做适配转换，不修改 news_monitor / event_calendar 原有逻辑
    - 新闻来自财经媒体，最高按 L2「权威报道」定级；无公司披露则受益级别
      最多为 B（间接受益，仅观察），杜绝把媒体消息当成 A 类直接利好
    - 数据缺失静默降级，绝不编造来源
"""

import logging
from typing import List

from .catalyst import CatalystGrader
from .evidence_tags import LEVEL_INFERENCE, LEVEL_ASSUMPTION

logger = logging.getLogger(__name__)

# news_monitor 的 level(0正常/1关注/2警惕/3重大利空) → 事件证据等级
# 媒体新闻最高 L2（权威报道）；level>=2 的负面消息保留 L2，其余按 L3 传闻处理
_NEWS_LEVEL_TO_EVENT_LEVEL = {
    3: LEVEL_INFERENCE,   # 重大利空：多为正式披露，按 L2 权威报道
    2: LEVEL_INFERENCE,   # 警惕：权威报道
    1: LEVEL_ASSUMPTION,  # 关注：证据较弱，按 L3 处理（进雷达区）
}


def news_risk_to_events(news_risk: dict,
                        grader: CatalystGrader = None) -> List[dict]:
    """
    把 news_monitor.scan_news_risk 的输出转换为分级事件列表。

    参数:
        news_risk: {code: {"level", "name", "alerts": [{"title","time",
                   "keywords","category","level"}], "in_holdings"}}
        grader: 可复用的 CatalystGrader 实例（默认新建）
    返回:
        分级后的事件 dict 列表（结构见 CatalystGrader.grade_event 返回值）
    """
    grader = grader or CatalystGrader()
    events = []
    if not news_risk:
        return events

    for code, info in news_risk.items():
        try:
            if not isinstance(info, dict):
                continue
            level = int(info.get("level", 1))
            if level < 1:
                continue  # 正常新闻不进入分级
            name = info.get("name", "")
            alerts = info.get("alerts") or []
            # 取最高级别的一条 alert 作为事件代表
            top = alerts[0] if alerts else {}
            title = top.get("title") or top.get("category") or f"{name} 新闻预警"
            # 媒体新闻来源统一按 financial_media；关键词含传闻词会被 grade 自动降 L3
            raw_event = {
                "text": title,
                "source_type": "financial_media",
                "source": f"{name}({code}) 财经媒体 {top.get('time', '')}".strip(),
                "companies": [{
                    "code": code,
                    "name": name,
                    # 媒体消息无公司披露 → 不给 A 类；无明确传导路径 → C（禁推荐）
                    "has_disclosure": False,
                    "concept_only": True,
                }],
            }
            graded = grader.grade_event(raw_event)
            # 二次收紧：媒体 level==1 的关注类强制降为 L3 传闻（进雷达区）
            if level == 1 and graded["event_level"] != LEVEL_ASSUMPTION:
                graded["event_level"] = LEVEL_ASSUMPTION
                graded["tradeable"] = False
                graded["reason"] = "媒体关注类消息证据不足，仅进雷达区"
            events.append(graded)
        except Exception as e:
            logger.debug(f"[证据桥接] {code} 转换失败，跳过: {e}")
            continue

    return events


def split_radar_and_body(graded_events: List[dict]) -> dict:
    """
    把分级事件拆分为"报告正文"与"市场雷达区"两类。

    返回:
        {"body": [可进入正文的事件], "radar": [仅雷达区的传闻事件]}
    """
    body, radar = [], []
    for g in graded_events or []:
        if g.get("event_level") == LEVEL_ASSUMPTION:
            radar.append(g)
        else:
            body.append(g)
    return {"body": body, "radar": radar}
