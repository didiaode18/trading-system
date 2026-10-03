# -*- coding: utf-8 -*-
"""
模拟盘 + 毕业机制 测试（paper-trading-graduation）
==================================================
覆盖验收清单三类核心用例：毕业达标、不达标（延期）、偏差过大（淘汰），
外加：费率与回测完全一致（抽查3笔对账）、开关关闭 no-op、报告区块静默降级、
多策略命名空间隔离、每日净值留痕。全部用合成行情，不依赖真实数据源。
"""

import pytest
import pandas as pd

from backtest.broker import SimBroker, Order, CostConfig
from paper_trading.simulator_broker import SimulatorBroker
from paper_trading.tracker import PaperTracker
from paper_trading.engine import (
    PaperTradingEngine, register_paper_strategy, advance_all, PAPER_STRATEGIES,
)
from paper_trading.comparator import PaperComparator
from paper_trading.graduation import GraduationJudge, suggest_lifecycle_transition
from paper_trading.report import render_paper_section_html


# ---- 合成行情工具 ----
def _dates(n, start="2026-01-01"):
    return pd.bdate_range(start, periods=n).strftime("%Y-%m-%d").tolist()


def _prices(start, returns, n):
    p = [start]
    for i in range(n - 1):
        p.append(round(p[-1] * (1 + returns[i % len(returns)]), 4))
    return p


def _make_df(dates, prices):
    return pd.DataFrame({
        "date": dates,
        "open": prices,
        "close": prices,
        "high": [round(x * 1.01, 4) for x in prices],
        "low": [round(x * 0.99, 4) for x in prices],
        "volume": [100_000_000] * len(prices),
        "amount": [p * 100_000_000 for p in prices],
    })


def _buy_hold(code, cash_pct=0.8):
    """买入并持有策略（与回测同签名 (date, feed, broker) -> list[Order]）。"""
    def fn(date, feed, broker):
        bar = feed.get_bar(code, date)
        if not bar:
            return []
        if broker.get_holding_dict(code) is None:
            shares = int(broker.cash * cash_pct / bar["close"] / 100) * 100
            if shares >= 100:
                return [Order(code=code, direction="buy", target_shares=shares,
                              price=bar["close"], date=date, reason="buy_hold")]
        return []
    return fn


@pytest.fixture
def tracker(tmp_path):
    return PaperTracker(db_path=str(tmp_path / "paper_test.db"))


# ---- 用例1：毕业达标 ----
def test_graduation_pass(tracker):
    code = "600000"
    n = 30
    dates = _dates(n)
    prices = _prices(10.0, [0.006, -0.002], n)  # 正漂移低波动 → 高夏普低回撤
    data = {code: _make_df(dates, prices)}
    eng = PaperTradingEngine("strat_pass", _buy_hold(code), data,
                             initial_capital=1_000_000, tracker=tracker,
                             start_date=dates[0])
    res = eng.run_range(dates[0], dates[-1])
    assert res["days"] == n
    assert tracker.trading_days("strat_pass") == n

    # 回测预期净值 = 模拟盘净值（完全可复现）→ 偏差 0
    paper_nav = tracker.get_nav_series("strat_pass").to_dict()
    comp = PaperComparator(config={"nav_deviation_threshold": 0.02}).compare(
        paper_nav, paper_nav)
    assert comp["ok"] is True
    assert comp["nav_deviation_mean"] == pytest.approx(0.0, abs=1e-9)

    judge = GraduationJudge(config={"graduation_days": 20, "min_sharpe": 1.0,
                                    "max_drawdown": 0.10,
                                    "nav_deviation_threshold": 0.02},
                            tracker=tracker)
    ev = judge.evaluate("strat_pass", comparator_result=comp)
    assert ev["metrics"]["sharpe"] > 1.0
    assert ev["metrics"]["max_drawdown"] < 0.10
    assert ev["metrics"]["trading_days"] >= 20
    assert ev["graduated"] is True
    assert ev["next_state"] == "live"
    assert ev["failed_items"] == []


# ---- 用例2：不达标（延期）----
def test_graduation_fail_delay(tracker):
    code = "600001"
    n = 30
    dates = _dates(n)
    prices = _prices(20.0, [-0.006, -0.002], n)  # 持续下跌 → 夏普为负
    data = {code: _make_df(dates, prices)}
    eng = PaperTradingEngine("strat_fail", _buy_hold(code), data,
                             initial_capital=1_000_000, tracker=tracker,
                             start_date=dates[0])
    eng.run_range(dates[0], dates[-1])

    judge = GraduationJudge(config={"graduation_days": 20, "min_sharpe": 1.0,
                                    "max_drawdown": 0.10}, tracker=tracker)
    ev = judge.evaluate("strat_fail")
    assert ev["graduated"] is False
    assert ev["next_state"] == "paper"  # 无偏差数据 → 延期观察
    assert len(ev["failed_items"]) > 0
    assert any("夏普" in x or "回撤" in x for x in ev["failed_items"])


def test_graduation_fail_insufficient_days(tracker):
    code = "600002"
    n = 10  # < 20 交易日
    dates = _dates(n)
    prices = _prices(10.0, [0.006, -0.002], n)
    data = {code: _make_df(dates, prices)}
    eng = PaperTradingEngine("strat_short", _buy_hold(code), data,
                             initial_capital=1_000_000, tracker=tracker,
                             start_date=dates[0])
    eng.run_range(dates[0], dates[-1])
    judge = GraduationJudge(config={"graduation_days": 20}, tracker=tracker)
    ev = judge.evaluate("strat_short")
    assert ev["graduated"] is False
    assert any("天数不足" in x for x in ev["failed_items"])


# ---- 用例3：偏差过大（淘汰）----
def test_graduation_reject_large_deviation(tracker):
    code = "600003"
    n = 30
    dates = _dates(n)
    prices = _prices(10.0, [0.006, -0.002], n)
    data = {code: _make_df(dates, prices)}
    eng = PaperTradingEngine("strat_dev", _buy_hold(code), data,
                             initial_capital=1_000_000, tracker=tracker,
                             start_date=dates[0])
    eng.run_range(dates[0], dates[-1])

    paper_nav = tracker.get_nav_series("strat_dev").to_dict()
    # 回测预期净值比模拟盘高 10% → 偏差远超 5% 淘汰线
    backtest_nav = {d: v * 1.10 for d, v in paper_nav.items()}
    comp = PaperComparator(config={"nav_deviation_threshold": 0.02}).compare(
        paper_nav, backtest_nav)
    assert comp["nav_deviation_mean"] > 0.05
    assert comp["has_deviation"] is True

    judge = GraduationJudge(config={"graduation_days": 20, "min_sharpe": 1.0,
                                    "max_drawdown": 0.10,
                                    "nav_deviation_threshold": 0.02,
                                    "reject_deviation_threshold": 0.05},
                            tracker=tracker)
    ev = judge.evaluate("strat_dev", comparator_result=comp)
    assert ev["graduated"] is False
    assert ev["next_state"] == "rejected"
    assert any("偏差过大" in x for x in ev["failed_items"])


# ---- 费率与回测完全一致（抽查3笔对账）----
def test_fee_parity_with_backtest_broker():
    bar = {"open": 10.0, "close": 10.0, "high": 10.1, "low": 9.9,
           "volume": 100_000_000, "amount": 1e9, "pre_close": 10.0}
    cc = CostConfig()
    raw = SimBroker(1_000_000, cost_config=cc)
    sim = SimulatorBroker(1_000_000, cost_config=cc)

    cases = [("buy", 1000, 10.0), ("buy", 2000, 10.5), ("buy", 3000, 9.8)]
    for i, (dirn, shares, price) in enumerate(cases):
        date = f"2026-01-0{i+1}"
        o_raw = Order(code="600000", direction=dirn, target_shares=shares,
                      price=price, date=date)
        o_sim = Order(code="600000", direction=dirn, target_shares=shares,
                      price=price, date=date)
        f_raw = raw.execute_buy(o_raw, bar)
        f_sim = sim.execute_buy(o_sim, bar)
        assert f_raw is not None and f_sim is not None
        # 抽查成交价/佣金/印花税/过户费/总成本/股数完全一致
        assert f_raw.price == f_sim.price
        assert f_raw.commission == f_sim.commission
        assert f_raw.stamp_tax == f_sim.stamp_tax
        assert f_raw.transfer_fee == f_sim.transfer_fee
        assert f_raw.total_cost == f_sim.total_cost
        assert f_raw.shares == f_sim.shares
    assert raw.cash == pytest.approx(sim.cash, abs=1e-6)


def test_simulator_broker_passthrough():
    sim = SimulatorBroker(500_000)
    assert sim.initial_capital == 500_000
    assert sim.cash == 500_000
    assert isinstance(sim.positions, dict)


# ---- 开关关闭 no-op ----
def test_switch_off_noop(tracker):
    register_paper_strategy("strat_off", _buy_hold("600000"),
                            data_dict={"600000": _make_df(_dates(5), _prices(10, [0.001], 5))},
                            start_date=_dates(5)[0])
    try:
        out = advance_all(end_date="2026-12-31", tracker=tracker,
                          config={"enabled": False})
        assert out == {}
        assert tracker.list_strategies() == []
    finally:
        PAPER_STRATEGIES.pop("strat_off", None)


def test_advance_all_enabled(tracker):
    code = "600010"
    n = 25
    dates = _dates(n)
    data = {code: _make_df(dates, _prices(10.0, [0.006, -0.002], n))}
    register_paper_strategy("strat_reg", _buy_hold(code), data_dict=data,
                            initial_capital=1_000_000, start_date=dates[0])
    try:
        out = advance_all(end_date=dates[-1], tracker=tracker,
                          config={"enabled": True})
        assert "strat_reg" in out
        assert out["strat_reg"]["days"] == n
        assert tracker.trading_days("strat_reg") == n
    finally:
        PAPER_STRATEGIES.pop("strat_reg", None)


# ---- 多策略命名空间隔离 ----
def test_multi_strategy_isolation(tracker):
    d = _dates(25)
    data_a = {"700001": _make_df(d, _prices(10.0, [0.006, -0.002], 25))}
    data_b = {"700002": _make_df(d, _prices(50.0, [-0.006, -0.002], 25))}
    ea = PaperTradingEngine("A", _buy_hold("700001"), data_a,
                            initial_capital=1_000_000, tracker=tracker, start_date=d[0])
    eb = PaperTradingEngine("B", _buy_hold("700002"), data_b,
                            initial_capital=1_000_000, tracker=tracker, start_date=d[0])
    ea.run_range(d[0], d[-1])
    eb.run_range(d[0], d[-1])
    assert set(tracker.list_strategies()) == {"A", "B"}
    judge = GraduationJudge(config={"graduation_days": 20}, tracker=tracker)
    ev_a = judge.evaluate("A")
    ev_b = judge.evaluate("B")
    assert ev_a["metrics"]["final_nav"] > ev_b["metrics"]["final_nav"]


# ---- 报告区块 ----
def test_report_section(tracker):
    code = "600020"
    n = 30
    dates = _dates(n)
    data = {code: _make_df(dates, _prices(10.0, [0.006, -0.002], n))}
    eng = PaperTradingEngine("strat_rpt", _buy_hold(code), data,
                             initial_capital=1_000_000, tracker=tracker,
                             start_date=dates[0])
    eng.run_range(dates[0], dates[-1])
    cfg = {"enabled": True, "graduation_days": 20, "min_sharpe": 1.0,
           "max_drawdown": 0.10}
    judge = GraduationJudge(config=cfg, tracker=tracker)
    html = render_paper_section_html("strat_rpt", tracker=tracker, judge=judge,
                                     config=cfg)
    assert "模拟盘毕业进度" in html
    assert "strat_rpt" in html


def test_report_section_disabled(tracker):
    html = render_paper_section_html("any", tracker=tracker,
                                     config={"enabled": False})
    assert html == ""


def test_report_section_no_data(tracker):
    html = render_paper_section_html("ghost", tracker=tracker,
                                     config={"enabled": True, "graduation_days": 20})
    assert html == ""


# ---- 生命周期建议（不自动改 state）----
def test_lifecycle_suggestion_no_auto_promote(tracker, tmp_path):
    lpath = tmp_path / "strategy_lifecycle.json"
    import json
    lpath.write_text(json.dumps({
        "strat_x": {"name": "测试策略", "state": "paper"}
    }, ensure_ascii=False), encoding="utf-8")
    ev = {"next_state": "live", "graduated": True, "reason": "达标",
          "failed_items": [], "metrics": {"sharpe": 2.0}, "nav_deviation_mean": 0.0}
    out = suggest_lifecycle_transition("strat_x", ev, lifecycle_path=str(lpath))
    assert out["ok"] is True
    saved = json.loads(lpath.read_text(encoding="utf-8"))
    # state 未被自动修改，仅追加建议
    assert saved["strat_x"]["state"] == "paper"
    assert saved["strat_x"]["graduation_suggestion"]["next_state"] == "live"


# ---- tracker 基础读写 ----
def test_tracker_record_and_read(tracker):
    tracker.record_daily("t1", "2026-01-01", 1_000_000, 1.0, 1_000_000, 0, 0, 0)
    tracker.record_daily("t1", "2026-01-02", 1_010_000, 1.01, 10_000, 1_000_000, 0.99, 2)
    tracker.record_trade("t1", "2026-01-02", "600000", "buy", 1000, 10.0, 10000, 5, 0)
    rows = tracker.get_daily_rows("t1")
    assert len(rows) == 2
    assert rows[-1]["nav"] == 1.01
    assert tracker.last_date("t1") == "2026-01-02"
    assert len(tracker.get_trades("t1")) == 1
    # UPSERT 幂等
    tracker.record_daily("t1", "2026-01-02", 1_020_000, 1.02, 10_000, 1_010_000, 0.99, 3)
    assert len(tracker.get_daily_rows("t1")) == 2
    assert tracker.get_daily_rows("t1")[-1]["nav"] == 1.02
