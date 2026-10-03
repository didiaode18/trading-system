# -*- coding: utf-8 -*-
"""
证据分级研究模块测试
====================
覆盖 SKILL 验收清单五类用例:
    1. 三级标签渲染（L1/L2/L3 文本+HTML）
    2. 缺来源拦截（L1 无来源/无报告期 → block）
    3. 无反证降级（关键结论单方证据 → 降级 L3）
    4. 传闻隔离（L3 事件只进雷达区，禁止买卖建议）
    5. 概念公司过滤（C 类禁止进入推荐，B 类仅观察）
"""

import pytest

from research.evidence_tags import (
    LEVEL_FACT, LEVEL_INFERENCE, LEVEL_ASSUMPTION,
    render_tag_text, render_tag_html, normalize_level,
)
from research.claim import Claim
from research.validator import EvidenceValidator
from research.catalyst import CatalystGrader
from research.report_section import build_evidence_section
from research.bridge import news_risk_to_events, split_radar_and_body


# ============================================================
# 一、三级标签渲染
# ============================================================
class TestEvidenceTags:
    def test_three_level_text_tags(self):
        """三级文本标签格式正确，含报告期与来源。"""
        t1 = render_tag_text(LEVEL_FACT, period="2026Q2", source="公司公告")
        assert "事实·L1" in t1 and "2026Q2" in t1 and "公司公告" in t1

        t2 = render_tag_text(LEVEL_INFERENCE)
        assert "推断·L2" in t2

        t3 = render_tag_text(LEVEL_ASSUMPTION)
        assert "假设·L3" in t3

    def test_three_level_html_tags(self):
        """三级 HTML 标签带配色且转义来源。"""
        h1 = render_tag_html(LEVEL_FACT, source="<公司>公告")
        assert "事实·L1" in h1 and "&lt;公司&gt;" in h1
        assert "<span" in h1

    def test_illegal_level_downgrades_to_l3(self):
        """非法等级一律归一化为 L3（最保守）。"""
        assert normalize_level(99) == LEVEL_ASSUMPTION
        assert normalize_level(None) == LEVEL_ASSUMPTION
        assert normalize_level("2") == LEVEL_INFERENCE

    def test_claim_auto_renders_both_sides(self):
        """Claim 自渲染包含正反证据。"""
        c = Claim("建议买入", level=LEVEL_FACT, period="2026Q2",
                  source="公司公告", pro_evidence=["ROE 18%"],
                  con_evidence=["负债率偏高"], is_decision=True)
        txt = c.to_text()
        assert "支持" in txt and "反对" in txt
        assert "事实·L1" in txt


# ============================================================
# 二、缺来源拦截
# ============================================================
class TestNoSourceBlocking:
    def test_l1_without_source_blocked(self):
        """L1 事实缺来源 → 拦截，不出现在输出中。"""
        v = EvidenceValidator()
        bad = Claim("营收增长 30%", level=LEVEL_FACT, period="2026Q2", source="")
        good = Claim("营收 100 亿", level=LEVEL_FACT, period="2026Q2",
                     source="公司年报")
        passed, violations = v.enforce([bad, good])
        # 无来源的被拦截
        assert all("营收增长 30%" not in c.text for c in passed)
        assert good in passed
        assert any(vio["code"] == "no_source" for vio in violations)

    def test_l1_without_period_blocked(self):
        """L1 事实缺报告期 → 拦截。"""
        v = EvidenceValidator()
        bad = Claim("ROE 18%", level=LEVEL_FACT, period="", source="公司公告")
        passed, violations = v.enforce([bad])
        assert passed == []
        assert any(vio["code"] == "no_period" for vio in violations)

    def test_l3_without_verify_condition_blocked(self):
        """L3 假设缺验证条件 → 宁可不输出。"""
        v = EvidenceValidator()
        bad = Claim("明年利润翻倍", level=LEVEL_ASSUMPTION, verify_condition="")
        passed, violations = v.enforce([bad])
        assert passed == []
        assert any(vio["code"] == "no_verify_condition" for vio in violations)

    def test_mixed_period_blocked(self):
        """报告期口径混用（季度+TTM）→ 拦截。"""
        v = EvidenceValidator()
        bad = Claim("毛利率 40%", level=LEVEL_FACT, period="2026Q2 TTM",
                    source="公司公告")
        passed, violations = v.enforce([bad])
        assert any(vio["code"] == "mixed_period" for vio in violations)


# ============================================================
# 三、无反证降级
# ============================================================
class TestOneSidedDowngrade:
    def test_decision_with_only_pro_downgraded(self):
        """关键结论只有支持证据 → 降级为 L3。"""
        v = EvidenceValidator()
        c = Claim("建议买入", level=LEVEL_FACT, period="2026Q2",
                  source="公司公告", pro_evidence=["订单饱满"],
                  con_evidence=[], is_decision=True)
        passed, violations = v.enforce([c])
        assert any(vio["code"] == "one_sided" for vio in violations)
        # 降级后仍保留（未命中 block），但等级变 L3
        assert len(passed) == 1
        assert passed[0].level == LEVEL_ASSUMPTION

    def test_decision_with_both_sides_kept(self):
        """关键结论正反证据齐备 → 保持原等级。"""
        v = EvidenceValidator()
        c = Claim("建议买入", level=LEVEL_FACT, period="2026Q2",
                  source="公司公告", pro_evidence=["订单饱满"],
                  con_evidence=["估值偏高"], is_decision=True)
        passed, violations = v.enforce([c])
        assert len(passed) == 1
        assert passed[0].level == LEVEL_FACT
        assert not any(vio["code"] == "one_sided" for vio in violations)

    def test_l2_without_reasoning_downgraded(self):
        """L2 推断缺推理链条 → 降级 L3。"""
        v = EvidenceValidator()
        c = Claim("盈利将改善", level=LEVEL_INFERENCE, reasoning="",
                  confidence=None)
        passed, violations = v.enforce([c])
        assert any(vio["code"] in ("no_reasoning", "no_confidence")
                   for vio in violations)
        assert passed[0].level == LEVEL_ASSUMPTION


# ============================================================
# 四、传闻隔离
# ============================================================
class TestRumorIsolation:
    def test_rumor_event_not_tradeable(self):
        """L3 传闻事件不可交易，只进雷达区。"""
        g = CatalystGrader()
        graded = g.grade_event({
            "text": "网传某公司将获大额订单",
            "source_type": "rumor",
            "source": "社交媒体",
            "companies": [{"code": "000001", "name": "测试股",
                           "has_disclosure": True,
                           "disclosure_detail": "订单10亿"}],
        })
        assert graded["event_level"] == LEVEL_ASSUMPTION
        assert graded["tradeable"] is False
        assert g.is_radar_only(graded) is True
        # 即便公司标 A 类，传闻事件也不得进推荐
        assert g.filter_recommendable(graded) == []

    def test_rumor_keyword_forces_l3(self):
        """文本含"据悉/网传"关键词，即便来源标公告也强制 L3。"""
        g = CatalystGrader()
        graded = g.grade_event({
            "text": "据悉公司将重组",
            "source_type": "company_announcement",
        })
        assert graded["event_level"] == LEVEL_ASSUMPTION

    def test_rumor_only_in_radar_section(self):
        """报告中传闻只出现在雷达区，无任何"可进入决策"标注。"""
        events = [{"text": "网传利好", "source_type": "rumor",
                   "source": "社媒"}]
        html = build_evidence_section(claims=[], events=events, as_html=True)
        # 传闻进入雷达隔离区
        assert "市场雷达" in html
        assert "传闻·L3" in html
        # 雷达区不得给出"可进入决策"的正向标注
        assert "可进入决策" not in html
        # 传闻事件不产生任何标的推荐（无受益徽章）
        assert "受益·" not in html


# ============================================================
# 五、概念公司过滤
# ============================================================
class TestConceptCompanyFilter:
    def test_concept_only_company_excluded(self):
        """C 类概念公司禁止进入推荐结果。"""
        g = CatalystGrader()
        graded = g.grade_event({
            "text": "政策支持新能源",
            "source_type": "government_document",
            "source": "发改委文件 2026-09-01",
            "companies": [
                {"code": "000001", "name": "实质受益", "has_disclosure": True,
                 "disclosure_detail": "中标 5 亿项目 公告 2026-09-02"},
                {"code": "000002", "name": "概念沾边", "concept_only": True},
            ],
        })
        recommendable = g.filter_recommendable(graded)
        codes = [c["code"] for c in recommendable]
        assert "000001" in codes
        assert "000002" not in codes  # C 类被过滤

    def test_b_grade_observe_only(self):
        """B 类间接受益只能观察并标注推断。"""
        g = CatalystGrader()
        graded = g.grade_event({
            "text": "行业景气上行",
            "source_type": "broker_research",
            "source": "某券商研报",
            "companies": [{"code": "000003", "name": "产业链相关",
                           "transmission_path": "上游材料→中游制造"}],
        })
        rec = g.filter_recommendable(graded)
        assert len(rec) == 1
        assert rec[0]["grade"] == "B"
        assert rec[0].get("observe_only") is True

    def test_a_grade_without_disclosure_downgraded(self):
        """标 A 类但缺披露明细 → 证据不足降级 B。"""
        g = CatalystGrader()
        graded = g.grade_event({
            "text": "公司公告扩产",
            "source_type": "company_announcement",
            "source": "公司公告",
            "companies": [{"code": "000004", "name": "缺明细",
                           "has_disclosure": True, "disclosure_detail": ""}],
        })
        comp = graded["companies"][0]
        assert comp["grade"] == "B"


# ============================================================
# 六、报告区块集成
# ============================================================
class TestReportSection:
    def test_empty_content_returns_empty(self):
        """无内容时返回空字符串（静默降级）。"""
        assert build_evidence_section(claims=[], events=[]) == ""

    def test_section_contains_tags_and_radar(self):
        """区块同时含关键结论标签与雷达区。"""
        claims = [Claim("2026Q2 ROE 18.5%", level=LEVEL_FACT,
                        period="2026Q2", source="公司公告 2026-08-15")]
        events = [{"text": "网传重组", "source_type": "rumor", "source": "社媒"}]
        html = build_evidence_section(claims=claims, events=events, as_html=True)
        assert "关键结论" in html
        assert "事实·L1" in html
        assert "市场雷达" in html

    def test_text_mode_render(self):
        """纯文本模式正常渲染（企业微信）。"""
        claims = [Claim("2026Q2 ROE 18.5%", level=LEVEL_FACT,
                        period="2026Q2", source="公司公告")]
        txt = build_evidence_section(claims=claims, events=[], as_html=False)
        assert "关键结论" in txt and "事实·L1" in txt
        assert "<" not in txt  # 纯文本不含 HTML 标签

    def test_naked_conclusion_never_in_output(self):
        """裸结论（无来源）绝不出现在最终区块中。"""
        claims = [
            Claim("营收 100 亿", level=LEVEL_FACT, period="2026Q2",
                  source="公司年报"),
            Claim("利润暴增", level=LEVEL_FACT, period="", source=""),  # 裸结论
        ]
        txt = build_evidence_section(claims=claims, events=[], as_html=False)
        assert "营收 100 亿" in txt
        assert "利润暴增" not in txt


# ============================================================
# 七、news_monitor 桥接适配
# ============================================================
class TestNewsBridge:
    def test_news_risk_converted_and_graded(self):
        """news_monitor 输出转为分级事件，媒体消息不当 A 类。"""
        news_risk = {
            "000001": {"level": 2, "name": "测试股",
                       "alerts": [{"title": "大股东减持", "time": "2026-09-20",
                                   "category": "警惕", "level": 2}],
                       "in_holdings": True},
        }
        events = news_risk_to_events(news_risk)
        assert len(events) == 1
        # 媒体消息无披露 → 公司不得为 A 类
        assert events[0]["companies"][0]["grade"] != "A"

    def test_low_level_news_goes_radar(self):
        """level==1 关注类新闻强制降 L3，进雷达区。"""
        news_risk = {
            "000002": {"level": 1, "name": "弱证据股",
                       "alerts": [{"title": "业绩下滑", "category": "关注",
                                   "level": 1}],
                       "in_holdings": False},
        }
        events = news_risk_to_events(news_risk)
        split = split_radar_and_body(events)
        assert len(split["radar"]) == 1
        assert split["body"] == []

    def test_empty_news_returns_empty(self):
        """空输入返回空列表（静默降级）。"""
        assert news_risk_to_events({}) == []
        assert news_risk_to_events(None) == []

    def test_config_toggle_exists(self):
        """config 中存在证据分级开关，默认观察模式。"""
        import config
        cfg = getattr(config, "EVIDENCE_GRADING_CONFIG", None)
        assert cfg is not None
        assert cfg["observe_only"] is True
        assert cfg["isolate_rumor"] is True
