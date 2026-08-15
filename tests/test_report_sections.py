# -*- coding: utf-8 -*-
"""
综合报告新增章节纯函数单元测试（任务#1新增）
==============================================
覆盖:
  - recovery_planner: 回本总收益率/目标价分摊/空计划/月化评级分档/进度落盘
  - sector_divergence: 缓存过期降级/粗细赛道映射/weak滞涨识别/100股取整/候选三源合并

运行: pytest tests/test_report_sections.py -v
"""
import sys
import os
import json
import datetime

import pytest

# 确保能导入trading_system（与 tests/test_core.py 的 sys.path 约定一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'trading_system'))

from strategy.recovery_planner import build_recovery_plan, track_progress
from strategy.sector_divergence import (
    load_sector_cache, detect_divergence, get_coarse_sector_map, merge_report_candidates,
)
from execution.trade_behavior import (
    analyze_trade_behavior, estimate_commission, render_behavior_alert_html,
)
import update_holdings


# ============================================================
# 回本计划测试
# ============================================================

class TestRecoveryPlanner:
    """build_recovery_plan / track_progress 核心逻辑测试"""

    def test_required_return_pct(self):
        """总收益率 = 缺口/总市值: 400000/667608 ≈ 59.9%"""
        plan = build_recovery_plan([], 667607.83, 400000)
        assert plan["available"] is True
        assert plan["required_return_pct"] == pytest.approx(400000 / 667607.83, abs=1e-6)
        assert 0.59 < plan["required_return_pct"] < 0.61  # 约59.9%

    def test_target_price_proportional(self):
        """目标价 = 现价 × (1+组合所需收益率)，等比分摊"""
        holdings = [
            {"code": "600036", "name": "招商银行", "shares": 4100, "price": 40.0, "sector": "大金融"},
            {"code": "603288", "name": "海天味业", "shares": 3000, "price": 30.0, "sector": "大消费"},
        ]
        plan = build_recovery_plan(holdings, 100000, 50000)
        req = plan["required_return_pct"]
        assert req == pytest.approx(0.5)
        for st in plan["stock_targets"]:
            expected = round(st["price"] * (1 + req), 2)
            assert st["target_price"] == pytest.approx(expected, abs=0.01)
            assert st["required_gain_pct"] == pytest.approx(50.0)
        # 市值权重: 4100*40=164000 vs 3000*30=90000
        w = {st["code"]: st["weight"] for st in plan["stock_targets"]}
        assert w["600036"] == pytest.approx(164000 / 254000, abs=0.001)

    def test_empty_plan_when_gap_zero(self):
        """target_gap<=0 返回空计划结构，不抛异常"""
        plan = build_recovery_plan([{"code": "x", "shares": 100, "price": 10}], 100000, 0)
        assert plan["available"] is False
        assert plan["stock_targets"] == []
        assert plan["horizons"] == []
        plan2 = build_recovery_plan([], 100000, -5000)
        assert plan2["available"] is False

    def test_empty_plan_when_no_value(self):
        """total_value<=0 返回空计划结构，不抛异常"""
        plan = build_recovery_plan([], 0, 400000)
        assert plan["available"] is False

    def test_monthly_rating_tiers(self):
        """月化评级分档: >4%激进, >2.5%偏积极, 否则可执行"""
        # 缺口59.9%: 3个月月化约16.9% → 激进
        plan = build_recovery_plan([], 667607.83, 400000, months=(3, 12))
        h = {x["months"]: x for x in plan["horizons"]}
        assert h[3]["rating"] == "激进"
        assert h[3]["monthly_required_pct"] > 4
        # 12个月月化约3.99% → 偏积极
        assert h[12]["rating"] == "偏积极"
        assert 2.5 < h[12]["monthly_required_pct"] <= 4

        # 小缺口10%: 12个月月化约0.8% → 可执行
        plan_small = build_recovery_plan([], 100000, 10000, months=(12,))
        assert plan_small["horizons"][0]["rating"] == "可执行"
        assert plan_small["horizons"][0]["monthly_required_pct"] <= 2.5

    def test_need_swap_when_over_resistance(self):
        """目标价超预测压力位15%以上 → need_swap=True"""
        holdings = [{"code": "600036", "name": "招商银行", "shares": 1000, "price": 10.0, "sector": "银行"}]
        # 缺口100% → 目标价20元; 压力位15 → 20 > 15*1.15=17.25 → 需换股
        forecast = {"600036": {"levels": {"first_resistance": 15.0}}}
        plan = build_recovery_plan(holdings, 10000, 10000, forecast_results=forecast)
        st = plan["stock_targets"][0]
        assert st["target_price"] == pytest.approx(20.0)
        assert st["need_swap"] is True

        # 压力位20 → 目标价20 < 20*1.15=23 → 可达
        forecast_ok = {"600036": {"levels": {"first_resistance": 20.0}}}
        plan_ok = build_recovery_plan(holdings, 10000, 10000, forecast_results=forecast_ok)
        assert plan_ok["stock_targets"][0]["need_swap"] is False

    def test_track_progress_and_overwrite(self, tmp_path):
        """进度快照落盘: 追加数组 + 同日覆盖"""
        pf = str(tmp_path / "sub" / "recovery_progress.json")
        snap = track_progress(260000, 400000, pf)
        assert snap is not None
        assert snap["progress_pct"] == pytest.approx(260000 / 660000 * 100, abs=0.01)
        # 同日再次调用 → 覆盖不重复
        track_progress(270000, 400000, pf)
        with open(pf, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["total_value"] == 270000

    def test_track_progress_io_error_returns_none(self, tmp_path):
        """IO异常返回None不抛出（传目录路径触发打开失败）"""
        result = track_progress(100000, 50000, str(tmp_path))
        assert result is None


# ============================================================
# 板块背离测试
# ============================================================

def _fresh_cache(strong=None, weak=None, scores=None):
    """构造新鲜的（未过期）板块轮动缓存"""
    return {
        "sector_scores": scores if scores is not None else {"银行": 30.0, "半导体": 85.0, "白酒": 55.0},
        "strong": strong if strong is not None else ["半导体"],
        "weak": weak if weak is not None else ["银行"],
        "updated": datetime.datetime.now().isoformat(),
    }


class TestSectorDivergence:
    """load_sector_cache / detect_divergence / merge_report_candidates 测试"""

    def test_cache_expired_returns_none(self, tmp_path):
        """updated距今>24h → load_sector_cache返回None（降级信号）"""
        cache_file = str(tmp_path / "sector_rotation_cache.json")
        stale = _fresh_cache()
        stale["updated"] = (datetime.datetime.now() - datetime.timedelta(hours=48)).isoformat()
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(stale, f)
        assert load_sector_cache(cache_file) is None

    def test_cache_fresh_returns_dict(self, tmp_path):
        """新鲜缓存正常返回dict"""
        cache_file = str(tmp_path / "sector_rotation_cache.json")
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(_fresh_cache(), f)
        data = load_sector_cache(cache_file)
        assert data is not None
        assert "sector_scores" in data

    def test_cache_missing_returns_none(self):
        """文件不存在返回None"""
        assert load_sector_cache("/nonexistent/path/cache.json") is None

    def test_none_cache_available_false(self):
        """sector_cache为None → available=False降级结构"""
        result = detect_divergence([{"code": "600036", "name": "招商银行", "sector": "大金融", "市值": 100000, "price": 40.0}], None)
        assert result["available"] is False
        assert result["lagging_positions"] == []

    def test_coarse_sector_mapping(self):
        """粗-细赛道映射: 概念板块体系(2026-08-06)下细分自动归入对应概念，旧兜底映射保留"""
        cmap = get_coarse_sector_map()
        # 新口径: 细分直接归入东方财富概念板块键名
        assert cmap.get("AI视觉") == "人工智能"
        assert cmap.get("半导体设备") == "半导体芯片"
        assert cmap.get("创新药") == "创新药"
        assert cmap.get("白酒") == "白酒"
        # 旧兜底映射保留（配置中已不存在的细分仍走内置表）
        assert cmap.get("银行") == "大金融"
        assert cmap.get("调味品") == "大消费"

    def test_weak_sector_lagging_positions(self):
        """持仓赛道落入weak名单 → 产生lagging_positions，减持金额=市值×0.3"""
        holdings = [
            {"code": "600036", "name": "招商银行", "sector": "银行", "市值": 161048.0, "price": 39.28},
            {"code": "002409", "name": "雅克科技", "sector": "半导体", "市值": 50000.0, "price": 100.0},
        ]
        result = detect_divergence(holdings, _fresh_cache())
        assert result["available"] is True
        lag_codes = [p["code"] for p in result["lagging_positions"]]
        assert "600036" in lag_codes  # 银行在weak名单
        assert "002409" not in lag_codes  # 半导体强势
        lp = [p for p in result["lagging_positions"] if p["code"] == "600036"][0]
        assert lp["建议减持金额"] == pytest.approx(161048.0 * 0.3, abs=0.01)

    def test_reduce_shares_round_100(self):
        """建议减持股数取整100股"""
        holdings = [{"code": "600036", "name": "招商银行", "sector": "银行", "市值": 100000.0, "price": 39.28}]
        result = detect_divergence(holdings, _fresh_cache())
        lp = result["lagging_positions"][0]
        # 30000 / 39.28 = 763.7 → 取整700股
        assert lp["建议减持股数"] == 700
        assert lp["建议减持股数"] % 100 == 0

    def test_score_bottom_third_lagging(self):
        """评分处于后1/3（非weak名单）也判定滞涨"""
        cache = _fresh_cache(strong=["半导体"], weak=[],
                             scores={"半导体": 85.0, "光伏": 70.0, "白酒": 55.0, "银行": 30.0})
        holdings = [{"code": "600036", "name": "招商银行", "sector": "银行", "市值": 100000.0, "price": 40.0}]
        result = detect_divergence(holdings, cache)
        # 4个赛道后1/3 = 最低1个(银行30分) → 滞涨
        assert len(result["lagging_positions"]) == 1

    def test_missed_hotspots_and_top_strong(self):
        """strong名单中无持仓覆盖 → missed_hotspots; top_strong_sectors取最强3"""
        holdings = [{"code": "600036", "name": "招商银行", "sector": "大金融", "市值": 100000.0, "price": 40.0}]
        cache = _fresh_cache(strong=["半导体", "军工航空"], weak=["银行"],
                             scores={"半导体": 85.0, "军工航空": 75.0, "银行": 30.0})
        result = detect_divergence(holdings, cache)
        assert "半导体" in result["missed_hotspots"]
        assert "军工航天" in result["missed_hotspots"]
        assert result["top_strong_sectors"] == ["半导体", "军工航天", "大金融"]

    def test_holding_sector_scores_none_when_unmapped(self):
        """评分映射不到的持仓记None"""
        holdings = [{"code": "159205", "name": "创业东财", "sector": "指数ETF", "市值": 10000.0, "price": 1.0}]
        result = detect_divergence(holdings, _fresh_cache())
        assert result["holding_sector_scores"]["159205"] is None


class TestMergeReportCandidates:
    """merge_report_candidates 三源合并去重测试"""

    def test_dedup_and_priority(self):
        """按code去重，静态池优先保留"""
        static = [{"code": "000001", "name": "A", "type": "龙头"}]
        pool = [{"code": "000001", "name": "A重复", "type": "观察池"},
                {"code": "000002", "name": "B", "type": "观察池"}]
        scan = [{"code": "000002", "name": "B重复", "type": "动态扫描"},
                {"code": "000003", "name": "C", "type": "动态扫描"}]
        merged = merge_report_candidates(static, pool, scan)
        codes = [c["code"] for c in merged]
        assert codes == ["000001", "000002", "000003"]
        assert merged[0]["type"] == "龙头"      # 静态池版本保留
        assert merged[1]["type"] == "观察池"    # 观察池版本保留

    def test_exclude_held_codes(self):
        """已持仓代码被排除"""
        static = [{"code": "600036"}, {"code": "002409"}]
        merged = merge_report_candidates(static, [], [], held_codes={"600036"})
        assert [c["code"] for c in merged] == ["002409"]

    def test_max_total_truncation(self):
        """超过max_total按优先级截断"""
        static = [{"code": f"00000{i}"} for i in range(1, 6)]   # 5只
        pool = [{"code": f"00001{i}"} for i in range(1, 6)]     # 5只
        scan = [{"code": f"00002{i}"} for i in range(1, 20)]    # 19只
        merged = merge_report_candidates(static, pool, scan, max_total=20)
        assert len(merged) == 20
        # 静态池5只+观察池5只全部保留，动态扫描取前10只
        assert all(c["code"] in [x["code"] for x in static] for c in merged[:5])
        assert all(c["code"] in [x["code"] for x in pool] for c in merged[5:10])

    def test_empty_sources(self):
        """空源不抛异常"""
        assert merge_report_candidates([], None, None) == []


# ============================================================
# 交易行为自诊断测试
# ============================================================

class TestTradeBehavior:
    """analyze_trade_behavior 频繁操作诊断核心逻辑测试"""

    def test_empty_trades(self):
        """空成交返回ok不抛异常"""
        r = analyze_trade_behavior([], 100000)
        assert r["severity"] == "ok"
        assert r["total_waste"] == 0

    def test_chase_cost(self):
        """追高成本: 更低买价出现后高价买入的额外支出"""
        trades = [
            {"time": "09:30:00", "code": "159243", "name": "A", "direction": "买入", "qty": 1000, "price": 1.20},
            {"time": "10:00:00", "code": "159243", "name": "A", "direction": "买入", "qty": 2000, "price": 1.26},
        ]
        r = analyze_trade_behavior(trades, 1000000)
        assert r["chase_cost"] == pytest.approx((1.26 - 1.20) * 2000, abs=0.01)

    def test_no_chase_when_buying_lower(self):
        """越买越便宜不算追高"""
        trades = [
            {"time": "09:30:00", "code": "159243", "name": "A", "direction": "买入", "qty": 1000, "price": 1.26},
            {"time": "10:00:00", "code": "159243", "name": "A", "direction": "买入", "qty": 2000, "price": 1.20},
        ]
        r = analyze_trade_behavior(trades, 1000000)
        assert r["chase_cost"] == 0

    def test_panic_cost(self):
        """杀低损失: 更高卖价出现后低价继续卖出"""
        trades = [
            {"time": "09:30:00", "code": "601318", "name": "B", "direction": "卖出", "qty": 400, "price": 53.36},
            {"time": "11:00:00", "code": "601318", "name": "B", "direction": "卖出", "qty": 1500, "price": 53.18},
        ]
        r = analyze_trade_behavior(trades, 1000000)
        assert r["panic_cost"] == pytest.approx((53.36 - 53.18) * 1500, abs=0.01)

    def test_quick_flip_loss(self):
        """闪电翻转: 买入后30分钟内卖出亏损计入回转亏损"""
        trades = [
            {"time": "09:52:55", "code": "600036", "name": "C", "direction": "买入", "qty": 1000, "price": 38.78},
            {"time": "09:57:55", "code": "600036", "name": "C", "direction": "卖出", "qty": 1000, "price": 38.69},
        ]
        r = analyze_trade_behavior(trades, 1000000)
        assert r["roundtrip_loss"] == pytest.approx(90.0, abs=0.01)
        assert r["quick_flip"]["count"] == 1
        assert r["quick_flip"]["loss"] == pytest.approx(90.0, abs=0.01)

    def test_sell_then_buyback_premium(self):
        """先卖后溢价买回计入回转亏损; 高卖低接为盈利抵减"""
        trades = [
            {"time": "09:30:00", "code": "588000", "name": "D", "direction": "卖出", "qty": 1000, "price": 1.745},
            {"time": "10:16:00", "code": "588000", "name": "D", "direction": "买入", "qty": 1000, "price": 1.787},
        ]
        r = analyze_trade_behavior(trades, 1000000)
        assert r["roundtrip_loss"] == pytest.approx((1.787 - 1.745) * 1000, abs=0.01)

    def test_commission_etf_no_stamp(self):
        """ETF卖出免印花税，股票卖出收印花税"""
        fees_etf = estimate_commission([
            {"code": "159599", "direction": "卖出", "qty": 10000, "price": 2.8, "amount": 28000}])
        fees_stock = estimate_commission([
            {"code": "601318", "direction": "卖出", "qty": 1000, "price": 28.0, "amount": 28000}])
        assert fees_etf["stamp"] == 0
        assert fees_stock["stamp"] == pytest.approx(28000 * 0.0005, abs=0.01)

    def test_severe_severity(self):
        """成交笔数≥20且换手率高 → severe，横幅含警示文案"""
        trades = [
            {"time": f"09:{30+i//10}{i%10}:00", "code": "159243", "name": "A",
             "direction": "买入", "qty": 1000, "price": 1.2}
            for i in range(25)
        ]
        r = analyze_trade_behavior(trades, 100000)
        assert r["severity"] == "severe"
        html = render_behavior_alert_html(r)
        assert "频繁操作警示" in html and "纪律提醒" in html

    def test_total_waste_components(self):
        """total_waste = 费用 + 追高 + 杀低 + 回转净亏损"""
        trades = [
            {"time": "09:30:00", "code": "159243", "name": "A", "direction": "买入", "qty": 1000, "price": 1.20},
            {"time": "10:00:00", "code": "159243", "name": "A", "direction": "买入", "qty": 1000, "price": 1.26},
        ]
        r = analyze_trade_behavior(trades, 1000000)
        expected = r["fees"]["total"] + r["chase_cost"] + r["panic_cost"] + r["roundtrip_loss"]
        assert r["total_waste"] == pytest.approx(expected, abs=0.01)


# ============================================================
# 持仓更新V2.0合并规则测试
# ============================================================

class TestHoldingsMerge:
    """update_holdings.merge_holding: 首次字段锁定/止损Ratchet/审计字段"""

    EXISTING = {
        "name": "海天味业", "shares": 900, "buy_price": 40.895,
        "current_price": 36.55, "stop_loss": 36.81, "highest": 40.895,
        "buy_date": "2026-07-25", "reason": "消费食品龙头", "sector": "大消费",
    }

    def _parsed(self, **kw):
        base = {"code": "603288", "name": "海天味业", "shares": 900,
                "buy_price": 40.895, "current_price": 36.50, "stop_loss": 0}
        base.update(kw)
        return base

    def test_new_position_initial_fields(self):
        """新建仓: 首次字段全量写入，未提供止损时默认成本×90%"""
        merged, warns = update_holdings.merge_holding(
            {}, self._parsed(shares=500), source="单只更新")
        assert merged["shares"] == 500
        assert merged["initial_stop_loss"] == pytest.approx(40.895 * 0.9, abs=0.001)
        assert merged["stop_loss"] == pytest.approx(40.895 * 0.9, abs=0.001)
        assert merged["first_shares"] == 500
        assert merged["revision_count"] == 0
        assert merged["first_confirmed_at"] and merged["last_update_source"] == "单只更新"
        assert not warns

    def test_cost_change_rejected_when_shares_same(self):
        """股数不变但输入新成本 → 拒绝覆盖原始成本"""
        merged, warns = update_holdings.merge_holding(
            dict(self.EXISTING), self._parsed(buy_price=38.0), source="文件")
        assert merged["buy_price"] == 40.895
        assert any("已忽略" in w for w in warns)

    def test_cost_change_with_force(self):
        """--force 显式确认 → 允许人工修正成本"""
        merged, warns = update_holdings.merge_holding(
            dict(self.EXISTING), self._parsed(buy_price=38.0), source="文件", force=True)
        assert merged["buy_price"] == 38.0
        assert any("--force" in w for w in warns)

    def test_stop_loss_ratchet_never_down(self):
        """输入止损低于现有 → 按Ratchet保留原值"""
        merged, warns = update_holdings.merge_holding(
            dict(self.EXISTING), self._parsed(stop_loss=33.63), source="交互式")
        assert merged["stop_loss"] == 36.81
        assert any("Ratchet" in w for w in warns)

    def test_stop_loss_zero_keeps_existing(self):
        """输入止损0视为未提供，保留现值绝不置0"""
        merged, _ = update_holdings.merge_holding(
            dict(self.EXISTING), self._parsed(stop_loss=0), source="文件")
        assert merged["stop_loss"] == 36.81

    def test_stop_loss_up_allowed(self):
        """输入更高止损 → 允许上移（只升不降）"""
        merged, _ = update_holdings.merge_holding(
            dict(self.EXISTING), self._parsed(stop_loss=37.5), source="文件")
        assert merged["stop_loss"] == 37.5

    def test_shares_change_accepts_new_cost_keeps_buy_date(self):
        """股数变化=真实交易: 接受新加权成本，但buy_date/initial_stop_loss不变"""
        merged, warns = update_holdings.merge_holding(
            dict(self.EXISTING), self._parsed(shares=1200, buy_price=41.5, stop_loss=37.2), source="文件")
        assert merged["shares"] == 1200
        assert merged["buy_price"] == 41.5
        assert merged["buy_date"] == "2026-07-25"  # 首次建仓日期不变

    def test_daily_fields_and_audit(self):
        """每日字段更新 + revision_count 递增"""
        merged, _ = update_holdings.merge_holding(
            dict(self.EXISTING), self._parsed(current_price=36.20), source="交互式")
        assert merged["current_price"] == 36.20
        assert merged["highest"] == 40.895  # highest只升不降
        assert merged["revision_count"] == 1
        assert merged["last_update_source"] == "交互式"
        assert merged["updated_at"]

