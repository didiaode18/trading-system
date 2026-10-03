# -*- coding: utf-8 -*-
"""
Brinson 归因 + 夏普分解 测试（quant-system-upgrade 任务B）
==========================================================
核心验收：Brinson 三项合计 = 实际超额收益（误差 < 0.1pct）；
数据不足静默降级；周报区块渲染/隐藏；夏普分解 Beta/Alpha 口径正确。
"""

import numpy as np
import pandas as pd
import pytest

from attribution.brinson import (
    brinson_attribution, build_sector_maps, render_brinson_section_html,
    CHECK_TOLERANCE,
)
from attribution.sharpe_decompose import (
    decompose_sharpe, render_sharpe_decompose_html, MIN_OBSERVATIONS,
)


# ============================================================
# Brinson：三项合计恒等式（误差 < 0.1pct）
# ============================================================
def test_brinson_identity_two_sectors():
    """两行业手工算例：AR+SR+IR 精确等于实际超额收益"""
    wp = {"科技": 0.6, "消费": 0.4}
    wb = {"科技": 0.5, "消费": 0.5}
    rp = {"科技": 0.10, "消费": -0.02}
    rb = {"科技": 0.08, "消费": 0.02}
    res = brinson_attribution(wp, wb, rp, rb)
    assert res["ok"] is True
    assert res["check_passed"] is True
    assert res["check_error"] < CHECK_TOLERANCE  # 误差 < 0.1pct

    # 手工验证（Brinson-Fachler）
    rb_total = 0.5 * 0.08 + 0.5 * 0.02          # 0.05
    rp_total = 0.6 * 0.10 + 0.4 * (-0.02)        # 0.052
    ar = (0.6 - 0.5) * (0.08 - rb_total) + (0.4 - 0.5) * (0.02 - rb_total)
    sr = 0.5 * (0.10 - 0.08) + 0.5 * (-0.02 - 0.02)
    ir = (0.6 - 0.5) * (0.10 - 0.08) + (0.4 - 0.5) * (-0.02 - 0.02)
    assert res["allocation"] == pytest.approx(ar, abs=1e-6)
    assert res["selection"] == pytest.approx(sr, abs=1e-6)
    assert res["interaction"] == pytest.approx(ir, abs=1e-6)
    assert res["total_excess"] == pytest.approx(rp_total - rb_total, abs=1e-6)
    assert res["actual_excess"] == pytest.approx(0.002, abs=1e-6)


def test_brinson_identity_random_portfolios():
    """随机多行业组合：三项合计恒等于超额收益（含组合权重<1的现金情形）"""
    rng = np.random.default_rng(42)
    for _ in range(20):
        sectors = [f"S{i}" for i in range(5)]
        wp_raw = rng.uniform(0, 1, 5)
        wb_raw = rng.uniform(0.5, 1.5, 5)
        wp = {s: w / wp_raw.sum() * rng.uniform(0.7, 1.0) for s, w in zip(sectors, wp_raw)}
        wb = {s: w / wb_raw.sum() for s, w in zip(sectors, wb_raw)}
        rp = {s: float(rng.uniform(-0.1, 0.1)) for s in sectors}
        rb = {s: float(rng.uniform(-0.1, 0.1)) for s in sectors}
        res = brinson_attribution(wp, wb, rp, rb)
        assert res["ok"] is True
        assert res["check_passed"] is True, res["message"]
        assert abs(res["allocation"] + res["selection"] + res["interaction"]
                   - res["actual_excess"]) < CHECK_TOLERANCE


def test_brinson_pure_allocation_no_selection():
    """组合与基准行业收益相同 → 选股效应=0，超额全部来自配置"""
    sectors = ["A", "B", "C"]
    rb = {"A": 0.05, "B": -0.03, "C": 0.01}
    wp = {"A": 0.6, "B": 0.2, "C": 0.2}
    wb = {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}
    res = brinson_attribution(wp, wb, dict(rb), rb)
    assert res["selection"] == pytest.approx(0.0, abs=1e-9)
    assert res["interaction"] == pytest.approx(0.0, abs=1e-9)
    assert res["allocation"] == pytest.approx(res["actual_excess"], abs=1e-6)


# ============================================================
# Brinson：静默降级
# ============================================================
def test_brinson_missing_data_degrades():
    empty = brinson_attribution({}, {"A": 1.0}, {}, {"A": 0.01})
    assert empty["ok"] is False
    assert "缺失" in empty["message"]
    zero_w = brinson_attribution({"A": 0.0}, {"A": 1.0}, {"A": 0.01}, {"A": 0.01})
    assert zero_w["ok"] is False  # 权重合计为0 → 降级


def test_brinson_sector_mismatch_handled():
    """组合持有基准没有的行业（超配新行业）：缺失侧按0权重/0收益，恒等式仍成立"""
    wp = {"科技": 0.5, "新兴": 0.5}
    wb = {"科技": 1.0}
    rp = {"科技": 0.02, "新兴": 0.10}
    rb = {"科技": 0.01}
    res = brinson_attribution(wp, wb, rp, rb)
    assert res["ok"] is True
    assert res["check_passed"] is True
    sectors = {r["sector"] for r in res["sectors"]}
    assert sectors == {"科技", "新兴"}


# ============================================================
# build_sector_maps：持仓 → Brinson 输入
# ============================================================
def test_build_sector_maps():
    holdings = {
        "600001": {"shares": 1000, "current_price": 10.0, "赛道": "科技"},
        "600002": {"shares": 1000, "current_price": 20.0, "sector": "消费"},
        "600003": {"shares": 500, "current_price": 20.0, "赛道": "科技"},
    }
    weekly = {"600001": 5.0, "600002": -2.0, "600003": 1.0}
    pw, pr, bw, br = build_sector_maps(
        holdings, weekly,
        benchmark_sector_weights={"科技": 0.5, "消费": 0.5},
        benchmark_sector_returns={"科技": 0.03, "消费": 0.01})
    # 科技市值 10000+10000=20000，消费 20000 → 各50%
    assert pw == pytest.approx({"科技": 0.5, "消费": 0.5})
    # 科技收益 = (10000*5% + 10000*1%)/20000 = 3%
    assert pr["科技"] == pytest.approx(0.03)
    assert pr["消费"] == pytest.approx(-0.02)
    assert bw == {"科技": 0.5, "消费": 0.5}


def test_build_sector_maps_no_benchmark_degrades():
    """无基准行业数据 → 返回空（调用方静默降级），禁止等权编造基准"""
    holdings = {"600001": {"shares": 1000, "current_price": 10.0, "赛道": "科技"}}
    pw, pr, bw, br = build_sector_maps(holdings, {"600001": 5.0})
    assert (pw, pr, bw, br) == ({}, {}, {}, {})


# ============================================================
# Brinson：周报区块渲染
# ============================================================
def test_render_brinson_section():
    res = brinson_attribution(
        {"科技": 0.6, "消费": 0.4}, {"科技": 0.5, "消费": 0.5},
        {"科技": 0.10, "消费": -0.02}, {"科技": 0.08, "消费": 0.02})
    html = render_brinson_section_html(res)
    assert "Brinson" in html
    assert "配置效应" in html and "选股效应" in html and "交互效应" in html
    assert "科技" in html


def test_render_brinson_section_hidden_on_degrade():
    assert render_brinson_section_html({"ok": False}) == ""
    assert render_brinson_section_html(None) == ""


def test_render_brinson_section_warns_on_check_fail():
    res = brinson_attribution(
        {"A": 0.6, "B": 0.4}, {"A": 0.5, "B": 0.5},
        {"A": 0.1, "B": 0.0}, {"A": 0.08, "B": 0.02})
    res["check_passed"] = False  # 模拟校验未通过
    res["message"] = "误差超阈值"
    html = render_brinson_section_html(res)
    assert "误差超阈值" in html  # 黄框提示而非静默


# ============================================================
# 夏普分解
# ============================================================
def _returns(n, daily, seed=None):
    if seed is not None:
        rng = np.random.default_rng(seed)
        daily = rng.normal(daily, 0.01, n)
    idx = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.Series(np.asarray(daily, dtype=float), index=idx)


def test_sharpe_decompose_beta_alpha_split():
    """策略 = 1.5×基准 → Beta≈1.5，Alpha收益≈0，R²≈1"""
    bench = _returns(120, 0.0005, seed=7)
    strat = bench * 1.5
    res = decompose_sharpe(strat, bench)
    assert res["ok"] is True
    assert res["beta"] == pytest.approx(1.5, abs=1e-6)
    assert res["r_squared"] == pytest.approx(1.0, abs=1e-6)
    assert res["beta_return_annual"] == pytest.approx(
        1.5 * res["benchmark_return_annual"], abs=1e-4)
    assert res["alpha_return_annual"] == pytest.approx(0.0, abs=1e-4)
    # 主动收益 = (1.5-1)×基准年化
    assert res["active_return_annual"] == pytest.approx(
        0.5 * res["benchmark_return_annual"], abs=1e-3)
    assert res["active_volatility"] > 0
    assert res["information_ratio"] != 0


def test_sharpe_decompose_pure_alpha():
    """策略 = 基准 + 恒定日alpha → Beta≈1，Alpha收益为正"""
    bench = _returns(120, 0.0003, seed=11)
    strat = bench + 0.001  # 每日+0.1%独立alpha
    res = decompose_sharpe(strat, bench)
    assert res["beta"] == pytest.approx(1.0, abs=1e-6)
    assert res["alpha_annual"] == pytest.approx(0.001 * 252, abs=1e-3)
    assert res["alpha_return_annual"] > 0
    assert res["sharpe"] > 0


def test_sharpe_decompose_insufficient_sample():
    strat = _returns(MIN_OBSERVATIONS - 1, 0.001)
    bench = _returns(MIN_OBSERVATIONS - 1, 0.0005)
    res = decompose_sharpe(strat, bench)
    assert res["ok"] is False
    assert "样本不足" in res["message"]
    assert render_sharpe_decompose_html(res) == ""  # 静默隐藏


def test_sharpe_decompose_none_input():
    res = decompose_sharpe(None, None)
    assert res["ok"] is False


def test_sharpe_decompose_index_alignment():
    """两序列日期不完全重合 → 按交集对齐"""
    bench = _returns(60, 0.0005, seed=3)
    strat = bench.iloc[20:].copy() * 1.2  # 只有后40天重合
    res = decompose_sharpe(strat, bench)
    assert res["ok"] is True
    assert res["observations"] == 40


def test_render_sharpe_decompose_html():
    bench = _returns(60, 0.0005, seed=5)
    res = decompose_sharpe(bench * 1.2 + 0.0005, bench)
    html = render_sharpe_decompose_html(res)
    assert "夏普分解" in html
    assert "信息比率" in html
    assert "Beta收益" in html and "Alpha收益" in html
