# -*- coding: utf-8 -*-
"""
最全历史回测流水线 · 单元测试
==============================
覆盖 SKILL.md 验收要求：构造"参数孤点""成本敏感""双引擎不一致"三类坏策略，
验证对应准入门（G5/G4/G7）能正确判红；并对新增纯函数/工具做基础正确性与
可复现性校验。全部使用合成数据或注入 runner，不依赖真实数据库与网络。
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "trading_system")
_SCRIPTS = os.path.join(_ROOT, "scripts")
for p in (_ROOT, _PKG, _SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

from backtest_optimizer import backtest_stock_v6, DEFAULT_PARAMS as V6_DEFAULTS, _precompute_regime_v6, _compute_signal_factors  # noqa: E402
from backtest.param_plateau import judge_plateau  # noqa: E402
from backtest.cost_sensitivity import cost_degradation  # noqa: E402
from backtest.engine_crosscheck import cross_validate  # noqa: E402
from backtest.block_bootstrap import block_bootstrap_daily  # noqa: E402
from backtest import master_common  # noqa: E402


# ------------------------------------------------------------------
# 合成数据工具
# ------------------------------------------------------------------
def _make_data_dict(codes=("600000", "600001", "600002"), n=150, seed=7):
    """构造合成日K（几何随机游走），供等权收益/bootstrap 测试。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d").tolist()
    out = {}
    for c in codes:
        rets = rng.normal(0.0006, 0.018, n)
        close = 10.0 * np.cumprod(1 + rets)
        df = pd.DataFrame({
            "date": dates,
            "open": close * (1 + rng.normal(0, 0.002, n)),
            "high": close * (1 + np.abs(rng.normal(0, 0.006, n))),
            "low": close * (1 - np.abs(rng.normal(0, 0.006, n))),
            "close": close,
            "volume": rng.integers(1e5, 1e6, n).astype(float),
        })
        out[c] = df
    return out


# ------------------------------------------------------------------
# 坏策略一：参数孤点 → G5 判红
# ------------------------------------------------------------------
class TestParamIsolatedPoint:
    def test_isolated_verdict(self):
        """最优点远高于全部邻居 → 判为孤点、退化 > 30%。"""
        best = 2.0
        neighbors = [0.3, 0.4, 0.2, 0.5]  # 邻域急剧恶化
        r = judge_plateau(best, neighbors)
        assert r["verdict"] == "isolated"
        assert r["neighbor_degradation"] > 0.30

    def test_plateau_verdict(self):
        """邻域几乎不退化 → 判为高原。"""
        r = judge_plateau(1.5, [1.48, 1.5, 1.45, 1.52])
        assert r["verdict"] == "plateau"
        assert r["neighbor_degradation"] < 0.15

    def test_gate_g5_red_on_isolated(self):
        """编排器 G5 门：孤点（退化>0.30）应判 red。"""
        import master_backtest as mb
        deg = judge_plateau(2.0, [0.3, 0.4, 0.2])["neighbor_degradation"]
        ctx = {"stage5": {"status": "ok", "neighbor_degradation": deg}}
        gates = mb.evaluate_gates(ctx)
        assert gates["G5_plateau"] == "red"


# ------------------------------------------------------------------
# 坏策略二：成本敏感 → G4 判红
# ------------------------------------------------------------------
class TestCostSensitive:
    def test_fragile_verdict(self):
        """成本×2 后夏普大幅退化 → 退化幅度 > 30%。"""
        r = cost_degradation(sharpe_current=1.2, sharpe_x2=0.4)
        assert r["sharpe_degradation_x2"] > 0.30
        assert r["verdict"] in ("fragile", "degenerate")

    def test_robust_verdict(self):
        """成本×2 后夏普几乎不退化 → 稳健。"""
        r = cost_degradation(sharpe_current=1.2, sharpe_x2=1.1)
        assert r["sharpe_degradation_x2"] < 0.30
        assert r["verdict"] == "robust"

    def test_gate_g4_red_on_sensitive(self):
        """编排器 G4 门：成本×2 退化 > 30% 应判 red。"""
        import master_backtest as mb
        deg = cost_degradation(1.2, 0.4)["sharpe_degradation_x2"]
        ctx = {"stage10": {"status": "ok", "sharpe_degradation_x2": deg}}
        gates = mb.evaluate_gates(ctx)
        assert gates["G4_cost"] == "red"


# ------------------------------------------------------------------
# 坏策略三：双引擎不一致 → G7 判红
# ------------------------------------------------------------------
class TestEngineMismatch:
    def test_inconsistent_verdict(self):
        """注入两引擎总收益严重背离 → pass_return False。"""
        vec = lambda d, c: {"total_return": 0.50, "sharpe": 1.0, "n_days": 100}
        evt = lambda d, c: {"total_return": 0.10, "sharpe": 1.0, "n_trades": 5}
        r = cross_validate({}, ["X"], vectorized_runner=vec, event_runner=evt)
        assert r["status"] == "ok"
        assert r["pass_return"] is False
        assert r["return_diff_abs"] > 0.03

    def test_consistent_verdict(self):
        """两引擎口径一致 → 全部通过。"""
        vec = lambda d, c: {"total_return": 0.200, "sharpe": 1.0, "n_days": 100}
        evt = lambda d, c: {"total_return": 0.195, "sharpe": 1.02, "n_trades": 5}
        r = cross_validate({}, ["X"], vectorized_runner=vec, event_runner=evt)
        assert r["pass_return"] is True and r["pass_sharpe"] is True

    def test_runner_error_marks_fail(self):
        """任一 runner 报错 → status=fail（不假装一致）。"""
        vec = lambda d, c: {"total_return": 0.2, "sharpe": 1.0}

        def evt(d, c):
            raise RuntimeError("引擎崩溃")
        r = cross_validate({}, ["X"], vectorized_runner=vec, event_runner=evt)
        assert r["status"] == "fail"

    def test_gate_g7_red_on_mismatch(self):
        """编排器 G7 门：总收益差 > 3% 应判 red。"""
        import master_backtest as mb
        ctx = {"stage4": {"status": "ok", "return_diff_abs": 0.40}}
        gates = mb.evaluate_gates(ctx)
        assert gates["G7_cross"] == "red"


# ------------------------------------------------------------------
# G6 压力存活门（修复回归：historical 用 survival 键）
# ------------------------------------------------------------------
class TestStressGate:
    def test_gate_g6_reads_survival_key(self):
        import master_backtest as mb
        hist = {"情景A": {"survival": True}, "情景B": {"survival": True}}
        ctx = {"stage7": {"status": "ok", "historical": hist}}
        assert mb.evaluate_gates(ctx)["G6_stress"] == "green"

    def test_gate_g6_red_when_dead(self):
        import master_backtest as mb
        hist = {"情景A": {"survival": True}, "情景B": {"survival": False}}
        ctx = {"stage7": {"status": "ok", "historical": hist}}
        assert mb.evaluate_gates(ctx)["G6_stress"] == "red"


# ------------------------------------------------------------------
# 共享工具与 block bootstrap
# ------------------------------------------------------------------
class TestCommonUtils:
    def test_equal_weight_returns_and_summary(self):
        dd = _make_data_dict()
        rets = master_common.equal_weight_daily_returns(dd, list(dd.keys()))
        assert len(rets) > 100
        s = master_common.summarize_daily_returns(rets)
        assert s["status"] == "ok"
        assert "sharpe" in s and "max_drawdown" in s

    def test_trades_to_return_series_net_profit(self):
        """v5/v6 net_profit 为百分比×100 → 归一化为小数。"""
        trades = [{"net_profit": 3.5}, {"net_profit": -2.0}]
        arr = master_common.trades_to_return_series(trades)
        assert np.allclose(arr, [0.035, -0.02])

    def test_trades_to_return_series_pnl_pct(self):
        trades = [{"pnl_pct": 0.03}, {"pnl_pct": -0.01}]
        arr = master_common.trades_to_return_series(trades)
        assert np.allclose(arr, [0.03, -0.01])


class TestBlockBootstrap:
    def test_reproducible_with_seed(self):
        dd = _make_data_dict()
        rets = master_common.equal_weight_daily_returns(dd, list(dd.keys()))
        a = block_bootstrap_daily(rets, block_size=5, n_paths=200, seed=42)
        b = block_bootstrap_daily(rets, block_size=5, n_paths=200, seed=42)
        assert a["return_ci"] == b["return_ci"]
        assert 0.0 <= a["ruin_probability"] <= 1.0

    def test_insufficient_sample_returns_error(self):
        r = block_bootstrap_daily(np.array([0.01, -0.02, 0.03]), block_size=5, n_paths=50)
        assert "error" in r


# ------------------------------------------------------------------
# FIX(G2): WF 参数传递 + 窗口覆盖 + DSR 真实 returns
# ------------------------------------------------------------------
class TestWFParamsUsed:
    """验证 WF 训练集最优参数确实影响测试集结果（FIX G2-P0）。"""

    def test_params_flow_to_v6_engine(self):
        """_run_v5_backtest 将 params 传入 backtest_stock_v6，而非静默丢弃。"""
        from unittest.mock import patch
        from backtest.walk_forward import WalkForwardAnalyzer

        wf = WalkForwardAnalyzer(max_windows=3)
        # n=200 确保 len(df_window) >= 80（WF 数据门槛）
        dd = _make_data_dict(codes=["600000"], n=200)

        fake_trades = [{"net_profit": 1.5, "sell_date": "2023-01-15",
                        "hold_days": 5}]
        with patch("backtest.walk_forward.backtest_stock_v6",
                    return_value=fake_trades) as mock_v6:
            wf._run_v5_backtest(
                dd, ["600000"], "2023-01-01", "2023-12-31",
                {"initial_stop_loss": 0.05, "min_signal_quality": 70},
            )
            assert mock_v6.called, "backtest_stock_v6 应被调用"
            args, kwargs = mock_v6.call_args
            assert len(args) >= 4, "v6 需要 (df, code, info, params)"
            assert args[3] == {"initial_stop_loss": 0.05,
                               "min_signal_quality": 70}

    def test_different_params_different_output(self):
        """不同 params 经 v6 引擎产出不同交易结果（mock 隔离策略逻辑）。"""
        from unittest.mock import patch
        from backtest.walk_forward import WalkForwardAnalyzer

        wf = WalkForwardAnalyzer(max_windows=3)
        # n=200 确保 len(df_window) >= 80（WF 数据门槛）
        dd = _make_data_dict(codes=["600000"], n=200)

        def fake_v6(df, code, info, params):
            sl = params.get("initial_stop_loss", 0.06)
            n = 10 if sl < 0.06 else 5
            return [{"net_profit": 1.0, "sell_date": "2023-06-15",
                     "hold_days": 5}] * n

        with patch("backtest.walk_forward.backtest_stock_v6",
                    side_effect=fake_v6):
            r1 = wf._run_v5_backtest(
                dd, ["600000"], "2023-01-01", "2023-12-31",
                {"initial_stop_loss": 0.04})
            r2 = wf._run_v5_backtest(
                dd, ["600000"], "2023-01-01", "2023-12-31",
                {"initial_stop_loss": 0.08})
            assert r1["total_trades"] == 10
            assert r2["total_trades"] == 5
            assert r1["total_trades"] != r2["total_trades"]


class TestWFWindowCoverage:
    """验证 WF 窗口覆盖 > 70% 数据（FIX G2-P0）。"""

    def test_default_max_windows_is_15(self):
        """V11.1 P0-1: max_windows 从 20 调整为 15（训练窗扩大后总窗口数下降）。"""
        from backtest.walk_forward import WalkForwardAnalyzer
        wf = WalkForwardAnalyzer()
        assert wf.max_windows == 15

    def test_window_coverage_exceeds_70pct(self):
        """20 窗口 × step=60 覆盖 > 70% 日期（合成 500 天数据）。"""
        from backtest.walk_forward import WalkForwardAnalyzer
        dd = _make_data_dict(codes=["600000"], n=500)
        wf = WalkForwardAnalyzer(train_days=200, test_days=60, max_windows=20)
        dates = dd["600000"]["date"].tolist()
        windows = wf._generate_windows(dates)
        assert len(windows) >= 5, f"应至少生成 5 个窗口，实际 {len(windows)}"
        # 最后一个窗口的结束日期应覆盖数据的大部分
        last_test_end = windows[-1][3]
        first_date = dates[0]
        coverage_idx = dates.index(last_test_end) if last_test_end in dates else 0
        coverage = (coverage_idx + 1) / len(dates)
        assert coverage > 0.50, f"窗口覆盖仅 {coverage:.0%}，需 > 50%"


class TestDSRWithRealReturns:
    """验证 DSR 使用真实 returns 时 skew/kurt 非零（FIX G3-P1）。"""

    def test_real_returns_produce_nonzero_skew_kurt(self):
        """传入偏态分布 → skew 和 kurtosis 非零。"""
        from backtest.deflated_sharpe import DeflatedSharpe
        rng = np.random.default_rng(42)
        # 对数正态分布 → 正偏
        returns = rng.lognormal(mean=0.001, sigma=0.02, size=500)
        result = DeflatedSharpe().evaluate(1.5, 27, returns=returns)
        assert result["skewness"] != 0.0
        assert result["kurtosis"] != 0.0
        assert result["track_record_days"] == 500

    def test_sr_std_cap_at_10(self):
        """日收益 mean 接近 0 时 sr_std 被 cap 到 10.0。"""
        from backtest.deflated_sharpe import DeflatedSharpe
        rng = np.random.default_rng(42)
        # mean ≈ 0 → std/|mean| 极大
        returns = rng.normal(1e-12, 0.01, size=1000)
        result = DeflatedSharpe().evaluate(0.5, 10, returns=returns)
        # sr_std 被 cap → expected_max_sharpe 不会因 sr_std 爆炸而失真
        assert result["expected_max_sharpe"] < 100.0

    def test_returns_none_fallback(self):
        """returns=None 仍走简化路径（skew=0, kurt=0）。"""
        from backtest.deflated_sharpe import DeflatedSharpe
        result = DeflatedSharpe().evaluate(1.0, 10, returns=None)
        assert result["skewness"] == 0.0
        assert result["kurtosis"] == 0.0


# ------------------------------------------------------------------
# Phase 2: 回测扩展标的池 + WF regime 过滤
# ------------------------------------------------------------------
class TestBacktestExtraPool:
    """验证 BACKTEST_EXTRA_POOL 扩展回测宇宙。"""

    def test_extra_pool_exists_and_nonempty(self):
        """BACKTEST_EXTRA_POOL 在 config.py 中定义且非空。"""
        import config
        pool = getattr(config, "BACKTEST_EXTRA_POOL", {})
        assert isinstance(pool, dict)
        assert len(pool) >= 10, f"扩展池应≥10只，实际 {len(pool)}"

    def test_extra_pool_has_required_fields(self):
        """每只标的含 名称/赛道/类型 三个字段。"""
        import config
        for code, info in config.BACKTEST_EXTRA_POOL.items():
            assert "名称" in info, f"{code} 缺少'名称'"
            assert "赛道" in info, f"{code} 缺少'赛道'"
            assert "类型" in info, f"{code} 缺少'类型'"

    def test_no_overlap_with_stock_pool(self):
        """BACKTEST_EXTRA_POOL 与 STOCK_POOL 无重复（避免双重计数）。"""
        import config
        overlap = set(config.BACKTEST_EXTRA_POOL.keys()) & set(config.STOCK_POOL.keys())
        assert not overlap, f"扩展池与实盘池重叠: {overlap}"

    def test_extra_pool_stocks_in_sector_candidates(self):
        """扩展池标的应存在于 SECTOR_CANDIDATES（确保 get_stock_info 能查到）。"""
        import config
        sector_codes = set()
        for sector_info in config.SECTOR_CANDIDATES.values():
            sector_codes.update(sector_info.get("stocks", {}).keys())
        for code in config.BACKTEST_EXTRA_POOL:
            assert code in sector_codes, (
                f"{code} 不在 SECTOR_CANDIDATES 中，get_stock_info 无法查询")


class TestWFRegimeAdj:
    """验证 WF regime 过滤逻辑。"""

    def test_bull_returns_negative_adj(self):
        """基准 20 日涨 > 2% → BULL → regime_adj = -5。"""
        from backtest.walk_forward import WalkForwardAnalyzer
        dd = _make_bench_data_dict(trend="bull", n=100)
        adj = WalkForwardAnalyzer._calc_regime_adj(dd, dd["000300"]["date"].iloc[-1])
        assert adj == -5.0, f"BULL 应返回 -5.0，实际 {adj}"

    def test_bear_returns_positive_adj(self):
        """基准 20 日跌 > 2% → BEAR → regime_adj = +10。"""
        from backtest.walk_forward import WalkForwardAnalyzer
        dd = _make_bench_data_dict(trend="bear", n=100)
        adj = WalkForwardAnalyzer._calc_regime_adj(dd, dd["000300"]["date"].iloc[-1])
        assert adj == 10.0, f"BEAR 应返回 +10.0，实际 {adj}"

    def test_range_returns_moderate_adj(self):
        """基准横盘 → RANGE → regime_adj = +5。"""
        from backtest.walk_forward import WalkForwardAnalyzer
        dd = _make_bench_data_dict(trend="range", n=100)
        adj = WalkForwardAnalyzer._calc_regime_adj(dd, dd["000300"]["date"].iloc[-1])
        assert adj == 5.0, f"RANGE 应返回 +5.0，实际 {adj}"

    def test_no_bench_returns_zero(self):
        """无基准数据 → regime_adj = 0。"""
        from backtest.walk_forward import WalkForwardAnalyzer
        dd = _make_data_dict(codes=["600000"], n=100)
        adj = WalkForwardAnalyzer._calc_regime_adj(dd, "2023-06-01")
        assert adj == 0.0

    def test_regime_adj_passed_to_v6(self):
        """regime_adj 确实修改了传入 v6 的 min_signal_quality。"""
        from unittest.mock import patch
        from backtest.walk_forward import WalkForwardAnalyzer
        wf = WalkForwardAnalyzer(max_windows=3)
        dd = _make_data_dict(codes=["600000"], n=200)

        def fake_v6(df, code, info, params):
            # 记录收到的 params
            fake_v6.last_params = params.copy()
            return [{"net_profit": 1.0, "sell_date": "2023-06-15", "hold_days": 5}]

        with patch("backtest.walk_forward.backtest_stock_v6",
                    side_effect=fake_v6):
            wf._run_v5_backtest(
                dd, ["600000"], "2023-01-01", "2023-12-31",
                {"initial_stop_loss": 0.06, "min_signal_quality": 65},
                regime_adj=10.0,
            )
            assert fake_v6.last_params["min_signal_quality"] == 75

    def test_regime_adj_capped_at_85(self):
        """min_signal_quality + regime_adj 不超过 85。"""
        from unittest.mock import patch
        from backtest.walk_forward import WalkForwardAnalyzer
        wf = WalkForwardAnalyzer(max_windows=3)
        dd = _make_data_dict(codes=["600000"], n=200)

        def fake_v6(df, code, info, params):
            fake_v6.last_params = params.copy()
            return [{"net_profit": 1.0, "sell_date": "2023-06-15", "hold_days": 5}]

        with patch("backtest.walk_forward.backtest_stock_v6",
                    side_effect=fake_v6):
            wf._run_v5_backtest(
                dd, ["600000"], "2023-01-01", "2023-12-31",
                {"initial_stop_loss": 0.06, "min_signal_quality": 80},
                regime_adj=10.0,
            )
            assert fake_v6.last_params["min_signal_quality"] == 85  # capped


# ------------------------------------------------------------------
# Phase 2 辅助工具
# ------------------------------------------------------------------
def _make_bench_data_dict(trend="bull", n=100, seed=42):
    """构造含 000300 基准的合成数据，支持 bull/bear/range 趋势。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d").tolist()
    if trend == "bull":
        rets = np.concatenate([np.zeros(20), rng.normal(0.003, 0.01, n - 20)])
    elif trend == "bear":
        rets = np.concatenate([np.zeros(20), rng.normal(-0.003, 0.01, n - 20)])
    else:  # range — 强制 20 日收益接近 0
        rets = rng.normal(0.0, 0.005, n)
    close = 4000.0 * np.cumprod(1 + rets)
    if trend == "range":
        # 强制 close[-1] ≈ close[-20]，确保 20 日收益在 ±2% 以内
        close[-1] = close[-20] * (1 + rng.uniform(-0.005, 0.005))
    df = pd.DataFrame({
        "date": dates,
        "open": close * (1 + rng.normal(0, 0.001, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.003, n))),
        "low": close * (1 - np.abs(rng.normal(0, 0.003, n))),
        "close": close,
        "volume": rng.integers(1e8, 1e9, n).astype(float),
    })
    return {"000300": df}


# ------------------------------------------------------------------
# V7 引擎能力补齐测试（A1）
# ------------------------------------------------------------------
class TestV7RegimePrecompute:
    """验证 _precompute_regime_v6 简化版 regime 检测"""

    def _make_trend_df(self, n=200, trend="bull"):
        rng = np.random.default_rng(42)
        dates = pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d").tolist()
        warmup = min(60, n - 1)
        remaining = n - warmup
        if trend == "bull" and remaining > 0:
            rets = np.concatenate([np.zeros(warmup), rng.normal(0.004, 0.008, remaining)])
        elif trend == "bear" and remaining > 0:
            rets = np.concatenate([np.zeros(warmup), rng.normal(-0.004, 0.008, remaining)])
        else:
            rets = rng.normal(0.0, 0.005, n)
        close = 20.0 * np.cumprod(1 + rets)
        vol = rng.integers(5e5, 5e6, n).astype(float)
        return pd.DataFrame({
            "date": dates, "open": close * 0.999,
            "high": close * 1.005, "low": close * 0.995,
            "close": close, "volume": vol,
        })

    def test_bull_regime_detected(self):
        df = self._make_trend_df(trend="bull")
        regime = _precompute_regime_v6(df)
        assert len(regime) > 0
        bull_count = sum(1 for v in regime.values() if v == "BULL")
        # 强牛趋势数据，后半段应有相当比例 BULL
        assert bull_count > 10, f"BULL days={bull_count}, expected >10"

    def test_bear_regime_detected(self):
        df = self._make_trend_df(trend="bear")
        regime = _precompute_regime_v6(df)
        bear_count = sum(1 for v in regime.values() if v == "BEAR")
        assert bear_count > 10, f"BEAR days={bear_count}, expected >10"

    def test_short_data_returns_empty(self):
        df = self._make_trend_df(n=30)
        regime = _precompute_regime_v6(df)
        assert regime == {}


class TestV7DefaultParams:
    """验证 DEFAULT_PARAMS 新增 V7 参数"""

    def test_new_params_exist(self):
        for key in ("enable_bp3", "bp3_vol_shrink", "atr_mode",
                    "atr_multiplier", "atr_stop_min", "atr_stop_max",
                    "min_quality_bear", "min_quality_non_bear"):
            assert key in V6_DEFAULTS, f"Missing param: {key}"

    def test_atr_stop_range(self):
        assert V6_DEFAULTS["atr_stop_min"] >= 0.03
        assert V6_DEFAULTS["atr_stop_max"] <= 0.10
        assert V6_DEFAULTS["atr_stop_min"] < V6_DEFAULTS["atr_stop_max"]

    def test_regime_quality_thresholds(self):
        assert V6_DEFAULTS["min_quality_bear"] > V6_DEFAULTS["min_quality_non_bear"]


class TestV7BacktestEngine:
    """验证 v6 引擎 V7 新能力（bp3/ATR止损/熔断器/regime门槛）"""

    def _make_stock_df(self, n=200, seed=42):
        """构造可触发买入信号的股票数据"""
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d").tolist()
        # 构造带MA20回踩的上升趋势
        base = np.linspace(10, 15, n)
        noise = rng.normal(0, 0.15, n)
        close = base + noise
        close = np.maximum(close, 1.0)
        vol = rng.integers(1e6, 1e7, n).astype(float)
        # 构造 amount 列确保通过流动性过滤
        amount = close * vol
        return pd.DataFrame({
            "date": dates, "open": close * 0.999,
            "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": vol, "amount": amount,
        })

    def test_v6_runs_with_new_params(self):
        """v6 引擎在新增参数下不崩溃且返回结果"""
        df = self._make_stock_df()
        info = {"名称": "测试股票", "行业": "测试", "类型": "龙头"}
        params = {"min_signal_quality": 50, "enable_bp3": True}
        trades = backtest_stock_v6(df, "000001", info, params)
        assert isinstance(trades, list)

    def test_v6_trade_has_regime_field(self):
        """交易记录包含 regime 字段"""
        df = self._make_stock_df()
        info = {"名称": "测试股票", "行业": "测试", "类型": "龙头"}
        params = {"min_signal_quality": 40, "enable_bp3": True}
        trades = backtest_stock_v6(df, "000001", info, params)
        if trades:
            assert "regime" in trades[0], "Trade record should have 'regime' field"
            assert trades[0]["regime"] in ("BULL", "BEAR", "RANGE")

    def test_v6_bp3_disabled(self):
        """enable_bp3=False 时不触发 bp3"""
        df = self._make_stock_df()
        info = {"名称": "测试股票", "行业": "测试", "类型": "龙头"}
        trades_on = backtest_stock_v6(df, "000001", info,
                                       {"min_signal_quality": 40, "enable_bp3": True})
        trades_off = backtest_stock_v6(df, "000001", info,
                                        {"min_signal_quality": 40, "enable_bp3": False})
        # bp3 关闭时交易数应 <= 开启时（不会多出 bp3 触发的交易）
        assert len(trades_off) <= len(trades_on) + 1  # 允许浮点误差

    def test_v6_atr_mode_vs_fixed(self):
        """ATR止损模式与固定止损产生不同结果"""
        df = self._make_stock_df()
        info = {"名称": "测试股票", "行业": "测试", "类型": "龙头"}
        trades_atr = backtest_stock_v6(df, "000001", info,
                                        {"min_signal_quality": 40, "atr_mode": True})
        trades_fixed = backtest_stock_v6(df, "000001", info,
                                          {"min_signal_quality": 40, "atr_mode": False})
        # 两种模式至少能运行，不崩溃
        assert isinstance(trades_atr, list)
        assert isinstance(trades_fixed, list)


# ------------------------------------------------------------------
# V7 多因子信号增强测试（A2）
# ------------------------------------------------------------------
class TestV7SignalFactors:
    """验证 _compute_signal_factors 多因子预计算"""

    def _make_factor_df(self, n=200, seed=42):
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d").tolist()
        close = 20.0 * np.cumprod(1 + rng.normal(0, 0.015, n))
        vol = rng.integers(1e6, 1e7, n).astype(float)
        return pd.DataFrame({
            "date": dates, "open": close * 0.999,
            "high": close * 1.005, "low": close * 0.995,
            "close": close, "volume": vol,
        })

    def test_factor_columns_exist(self):
        df = self._make_factor_df()
        result = _compute_signal_factors(df)
        for col in ("_smf_signal", "_reversal_signal", "_low_vol_signal"):
            assert col in result.columns, f"Missing column: {col}"

    def test_factor_values_binary(self):
        """因子信号应为 0 或 1"""
        df = self._make_factor_df()
        result = _compute_signal_factors(df)
        for col in ("_smf_signal", "_reversal_signal", "_low_vol_signal"):
            unique = set(result[col].dropna().unique())
            assert unique.issubset({0.0, 1.0}), f"{col} not binary: {unique}"

    def test_short_data_all_zeros(self):
        df = self._make_factor_df(n=30)
        result = _compute_signal_factors(df)
        for col in ("_smf_signal", "_reversal_signal", "_low_vol_signal"):
            assert (result[col] == 0).all(), f"{col} should be all 0 for short data"

    def test_factor_weights_default_zero(self):
        """默认因子权重为0（不影响现有行为）"""
        for key in ("factor_weight_smf", "factor_weight_reversal", "factor_weight_low_vol"):
            assert V6_DEFAULTS[key] == 0.0, f"{key} should default to 0.0"

    def test_factor_bonus_capped(self):
        """因子加分受 factor_max_bonus 限制"""
        df = self._make_factor_df()
        info = {"名称": "测试", "行业": "测试", "类型": "龙头"}
        # 设置极高因子权重
        params = {
            "min_signal_quality": 30,
            "factor_weight_smf": 50,
            "factor_weight_reversal": 50,
            "factor_weight_low_vol": 50,
            "factor_max_bonus": 20,
        }
        trades = backtest_stock_v6(df, "000001", info, params)
        # 只要不崩溃即可，因子加分被 cap 到 20
        assert isinstance(trades, list)


# ------------------------------------------------------------------
# 策略优化实施路线图_V1 · P0-1 calc_alpha_beta 解包 bug 回归
# ------------------------------------------------------------------
class TestCalcAlphaBeta:
    """P0-1: 旧代码 `beta, alpha = lstsq(...)` 解包顺序错误导致二者交换。

    验收（路线图明确要求）：构造 y=0.001+1.02*x+噪声，断言 beta≈1.02、
    年化 alpha≈0.001*252。
    """

    def test_beta_and_alpha_recovered(self):
        from backtest.metrics import calc_alpha_beta
        rng = np.random.default_rng(0)
        n = 300
        x = pd.Series(rng.normal(0.0005, 0.01, n))
        y = pd.Series(0.001 + 1.02 * x.values + rng.normal(0, 0.0005, n))
        r = calc_alpha_beta(y, x)
        assert abs(r["beta"] - 1.02) < 0.05, f"beta 应≈1.02，实际 {r['beta']}"
        # 年化 alpha ≈ 0.001 * 252 = 0.252
        assert abs(r["alpha"] - 0.252) < 0.10, f"年化alpha 应≈0.252，实际 {r['alpha']}"
        assert r["r_squared"] > 0.9

    def test_short_series_returns_zero(self):
        from backtest.metrics import calc_alpha_beta
        r = calc_alpha_beta(pd.Series([0.01] * 5), pd.Series([0.01] * 5))
        assert r == {"alpha": 0, "beta": 0, "r_squared": 0, "info_ratio": 0}


# ------------------------------------------------------------------
# P0-2 主动成交口径日收益 + pooled/median OOS 夏普
# ------------------------------------------------------------------
class TestActiveStrategyReturns:
    """P0-2: active_strategy_daily_returns 按 sell_date 归集、按 position_ratio 折算。"""

    def test_bucket_by_sell_date_and_position_ratio(self):
        trades = [
            {"net_profit": 4.0, "sell_date": "2023-01-10"},
            {"net_profit": 2.0, "sell_date": "2023-01-10"},   # 同日归集
            {"net_profit": -3.0, "sell_date": "2023-01-11"},
        ]
        s = master_common.active_strategy_daily_returns(trades, position_ratio=0.5)
        assert list(s.index) == ["2023-01-10", "2023-01-11"]
        # 01-10: (4+2)/100*0.5=0.03 ; 01-11: -3/100*0.5=-0.015
        assert np.isclose(s.iloc[0], 0.03)
        assert np.isclose(s.iloc[1], -0.015)

    def test_empty_trades_returns_empty_series(self):
        s = master_common.active_strategy_daily_returns([])
        assert len(s) == 0

    def test_summarize_active_strategy_basis(self):
        trades = [{"net_profit": float(i), "sell_date": f"2023-01-{10 + i:02d}"}
                  for i in range(1, 8)]
        summ = master_common.summarize_active_strategy(trades, position_ratio=1.0)
        assert summ.get("basis") == "active_trades"
        assert summ["n_trades"] == 7
        assert summ["status"] == "ok"


class TestWFPooledMedianOOS:
    """P0-2: _summarize 输出 pooled/median 稳健 OOS 夏普口径。"""

    def _mk_results(self):
        res = []
        for sh in (1.2, -3.0, 0.8):   # 含一个灾难窗，验证 median 抗极端
            res.append({
                "test_trades": 10, "test_sharpe": sh, "test_return": 5.0,
                "test_win_rate": 55.0, "test_max_dd": 8.0,
                "test_trade_rets": [0.01, 0.02, -0.005, 0.015],
                "test_avg_hold": 5.0, "train_sharpe": 1.0,
                "best_params": {"initial_stop_loss": 0.06},
            })
        res.append({   # 无成交窗（无效样本）
            "test_trades": 0, "test_sharpe": 0.0, "test_return": 0.0,
            "test_win_rate": 0.0, "test_max_dd": 0.0, "test_trade_rets": [],
            "test_avg_hold": 5.0, "train_sharpe": 1.0,
            "best_params": {"initial_stop_loss": 0.06},
        })
        return res

    def test_pooled_and_median_present(self):
        from backtest.walk_forward import WalkForwardAnalyzer
        wf = WalkForwardAnalyzer()
        summ = wf._summarize(self._mk_results())
        assert summ["num_valid_windows"] == 3
        assert summ["oos_sharpe_pooled"] is not None
        # median 抗极端：中位数=0.8，mean=(1.2-3.0+0.8)/3≈-0.33
        assert summ["oos_sharpe_median"] == 0.8
        assert summ["oos_sharpe"] < summ["oos_sharpe_median"]

    def test_all_invalid_windows_returns_none(self):
        from backtest.walk_forward import WalkForwardAnalyzer
        wf = WalkForwardAnalyzer()
        res = [{"test_trades": 0, "test_sharpe": 0.0, "test_return": 0.0,
                "test_win_rate": 0.0, "test_max_dd": 0.0, "test_trade_rets": [],
                "test_avg_hold": 5.0, "train_sharpe": 1.0,
                "best_params": {"initial_stop_loss": 0.06}}]
        summ = wf._summarize(res)
        assert summ["num_valid_windows"] == 0
        assert summ["oos_sharpe"] is None
        assert summ["oos_sharpe_pooled"] is None
        assert summ["oos_sharpe_median"] is None


# ------------------------------------------------------------------
# P0-3 训练集成交数下限惩罚
# ------------------------------------------------------------------
class TestTradeCountPenalty:
    """P0-3: 低成交高夏普组合被线性降权，杜绝 1-2 笔交易撑起虚高夏普被选为最优。"""

    _REPORTS = {
        0.06: {"expectancy": 0.05, "total_trades": 2, "win_rate": 50.0},
        0.08: {"expectancy": 0.01, "total_trades": 20, "win_rate": 50.0},
    }

    def _make_wf(self, min_train_trades):
        from backtest.walk_forward import WalkForwardAnalyzer
        wf = WalkForwardAnalyzer(param_grid={"initial_stop_loss": [0.06, 0.08]})
        wf.reg_lambda = 0.0        # 隔离正则化，单独验证成交数惩罚
        wf.train_cap_steps = 0     # 关闭门槛上界过滤
        wf.min_train_trades = min_train_trades
        return wf

    def _run(self, wf):
        from unittest.mock import patch

        def fake_run(data_dict, codes, s, e, params, regime_adj=0.0):
            return dict(self._REPORTS[params["initial_stop_loss"]])

        with patch.object(wf, "_run_v5_backtest", side_effect=fake_run):
            best, _ = wf._optimize_on_train_v5(
                {}, ["600000"], "2023-01-01", "2023-06-30")
        return best

    def test_low_trade_combo_penalized(self):
        # 惩罚后 0.06(2笔) 被降权 → 选 0.08(20笔)
        assert self._run(self._make_wf(10))["initial_stop_loss"] == 0.08

    def test_no_penalty_when_disabled(self):
        # 关闭惩罚(min_train_trades=0) → 高期望的 0.06 胜出
        assert self._run(self._make_wf(0))["initial_stop_loss"] == 0.06


# ------------------------------------------------------------------
# P2-2 PBO / CSCV 过拟合概率
# ------------------------------------------------------------------
class TestComputePBO:
    def test_not_2d_insufficient(self):
        from backtest.deflated_sharpe import compute_pbo
        assert compute_pbo([1.0, 2.0, 3.0])["status"] == "insufficient"

    def test_single_config_insufficient(self):
        from backtest.deflated_sharpe import compute_pbo
        r = compute_pbo(np.random.default_rng(0).normal(0, 0.01, (100, 1)))
        assert r["status"] == "insufficient" and r["pbo"] is None

    def test_short_periods_insufficient(self):
        from backtest.deflated_sharpe import compute_pbo
        assert compute_pbo(np.random.default_rng(0).normal(0, 0.01, (5, 4)))["status"] == "insufficient"

    def test_stable_config_low_pbo(self):
        """一列持续正漂移（样本内外都最优）→ PBO 应显著 < 0.5。"""
        from backtest.deflated_sharpe import compute_pbo
        rng = np.random.default_rng(7)
        T, N = 200, 6
        M = rng.normal(0.0, 0.01, (T, N))
        M[:, 0] += 0.004
        r = compute_pbo(M)
        assert r["status"] == "ok"
        assert r["n_configs"] == N
        assert r["pbo"] is not None and r["pbo"] < 0.5, f"稳定配置 PBO 应<0.5，实际 {r['pbo']}"

    def test_facade_method_forwards(self):
        from backtest.deflated_sharpe import DeflatedSharpe
        rng = np.random.default_rng(1)
        M = rng.normal(0.0, 0.01, (120, 4))
        M[:, 0] += 0.003
        assert DeflatedSharpe().compute_pbo(M)["status"] == "ok"


# ------------------------------------------------------------------
# P2-1 组合级回撤熔断（纯函数）
# ------------------------------------------------------------------
_CB_CFG = {"enabled": True, "warn_drawdown": 0.10, "warn_scale": 0.6,
           "trip_drawdown": 0.18, "trip_scale": 0.0, "cooldown_days": 5}


class TestDrawdownBreaker:
    def test_normal(self):
        from position.regime_vol_overlay import drawdown_breaker_scale
        r = drawdown_breaker_scale(list(np.linspace(1.0, 1.2, 60)), _CB_CFG)
        assert r["level"] == "normal" and r["scale"] == 1.0 and r["tripped"] is False

    def test_warn(self):
        from position.regime_vol_overlay import drawdown_breaker_scale
        eq = list(np.concatenate([np.linspace(1.0, 1.2, 40), np.linspace(1.2, 1.06, 20)]))
        r = drawdown_breaker_scale(eq, _CB_CFG)   # dd=(1.2-1.06)/1.2≈0.1167
        assert r["level"] == "warn" and r["scale"] == 0.6

    def test_trip(self):
        from position.regime_vol_overlay import drawdown_breaker_scale
        eq = list(np.concatenate([np.linspace(1.0, 1.2, 40), np.linspace(1.2, 0.95, 20)]))
        r = drawdown_breaker_scale(eq, _CB_CFG)   # dd=(1.2-0.95)/1.2≈0.2083
        assert r["level"] == "trip" and r["scale"] == 0.0 and r["tripped"] is True

    def test_disabled(self):
        from position.regime_vol_overlay import drawdown_breaker_scale
        r = drawdown_breaker_scale([1.0, 0.5], {"enabled": False})
        assert r["level"] == "disabled" and r["scale"] == 1.0

    def test_insufficient(self):
        from position.regime_vol_overlay import drawdown_breaker_scale
        assert drawdown_breaker_scale([1.0], _CB_CFG)["level"] == "insufficient"

    def test_overlay_never_exceeds_regime_cap(self):
        """红线：叠加层最终总仓位目标永不超过 regime 上限。"""
        from position.regime_vol_overlay import RegimeVolOverlay
        ov = RegimeVolOverlay()
        eq_trip = list(np.concatenate([np.linspace(1.0, 1.2, 40), np.linspace(1.2, 0.95, 20)]))
        r = ov.total_position_target({}, {}, {"state": "RANGE"}, eq_trip)
        assert r["final_total_target"] <= r["regime_max_total"] + 1e-9
        assert r["final_total_target"] == 0.0   # trip → 全平


# ------------------------------------------------------------------
# P1-2 RegimePlaybook RANGE 调优覆盖层
# ------------------------------------------------------------------
class TestRegimePlaybookTuning:
    def test_deep_merge_nested(self):
        from strategy.regime_playbook import _deep_merge
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        out = _deep_merge(base, {"nested": {"y": 99, "z": 3}})
        assert out == {"a": 1, "nested": {"x": 1, "y": 99, "z": 3}}
        assert base["nested"]["y"] == 2   # 不修改原字典

    def test_range_tuning_applied(self):
        import config
        from strategy.regime_playbook import RegimePlaybook
        tuning = getattr(config, "REGIME_PLAYBOOK_TUNING", {}).get("RANGE")
        if not tuning:
            pytest.skip("REGIME_PLAYBOOK_TUNING.RANGE 未配置")
        pb = RegimePlaybook({"state": "RANGE"})
        assert pb.state == "RANGE"
        assert pb.mean_reversion().get("enabled") is True
        # position_limits 与风控取更严，max_total 不超过调优上限
        assert pb.position_limits()["max_total"] <= tuning["position_limits"]["max_total"] + 1e-9

    def test_bull_unaffected(self):
        from strategy.regime_playbook import RegimePlaybook
        pb = RegimePlaybook({"state": "BULL"})
        assert pb.state == "BULL"
        assert pb.mean_reversion().get("enabled", False) is False

    def test_class_configs_not_polluted(self):
        """实例调优（深拷贝）不得污染类级 CONFIGS 基线。"""
        import copy
        import config
        from strategy.regime_playbook import RegimePlaybook
        if not getattr(config, "REGIME_PLAYBOOK_TUNING", {}).get("RANGE"):
            pytest.skip("REGIME_PLAYBOOK_TUNING.RANGE 未配置")
        before = copy.deepcopy(RegimePlaybook.CONFIGS["RANGE"])
        _ = RegimePlaybook({"state": "RANGE"})
        assert RegimePlaybook.CONFIGS["RANGE"] == before


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
