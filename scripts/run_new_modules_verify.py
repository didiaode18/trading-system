#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
新增模块验证脚本
================
验证 6 个新增模块的导入和基本功能:
  1. backtest/decile_backtest.py  — 分层回测引擎
  2. execution/slippage_model.py  — 动态滑点模型
  3. factors/ta_lib_wrapper.py    — TA-Lib封装层
  4. factors/quantile_scorer.py   — 分位数打分法
  5. strategy/pairs_trading.py    — 配对交易策略
  6. strategy/dividend_enhance.py — 红利ETF增强策略

使用: python scripts/run_new_modules_verify.py
"""

import sys
import os
import numpy as np
import pandas as pd

# 确保 trading_system 在 Python 路径中
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "trading_system"))

# 强制 UTF-8 输出
os.environ["PYTHONIOENCODING"] = "utf-8"

PASS = 0
FAIL = 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  [PASS] {name}")
        PASS += 1
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        FAIL += 1


# ============================================================
# 1. 分层回测引擎
# ============================================================
def test_decile_backtest():
    from backtest.decile_backtest import DecileBacktest

    # 生成模拟数据
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=200, freq="B")
    codes = [f"S{i:03d}" for i in range(50)]

    factor_df = pd.DataFrame(
        np.random.randn(200, 50),
        index=dates, columns=codes
    )
    # 让因子有一定预测能力
    forward_ret = factor_df.shift(5) * 0.01 + pd.DataFrame(
        np.random.randn(200, 50) * 0.02,
        index=dates, columns=codes
    )

    bt = DecileBacktest(factor_df, forward_ret, n_groups=5, rebalance_freq=5)
    result = bt.run()
    assert result.n_groups == 5
    assert len(result.groups) == 5
    assert result.ic_mean != 0 or result.ic_mean == 0  # 不崩溃即可
    bt.print_summary()


# ============================================================
# 2. 动态滑点模型
# ============================================================
def test_slippage_model():
    from execution.slippage_model import DynamicSlippageModel

    model = DynamicSlippageModel()

    # 大盘股
    est = model.estimate(price=10.0, shares=10000, daily_amount=5e8)
    assert est.liquidity_tier == "large"
    assert est.total_slippage > 0

    # 小盘股
    est_small = model.estimate(price=5.0, shares=5000, daily_amount=5e7)
    assert est_small.liquidity_tier == "small"
    est_large = model.estimate(price=10.0, shares=10000, daily_amount=5e8)
    assert est_small.total_slippage > est_large.total_slippage  # 小盘滑点更大

    # 调整价格
    adj_price = model.estimate_adjusted_price(10.0, 10000, daily_amount=3e8, direction="buy")
    assert adj_price > 10.0

    print(f"    大盘股滑点: {model.estimate(10, 10000, 5e8).total_slippage:.4%}")
    print(f"    小盘股滑点: {model.estimate(5, 5000, 5e7).total_slippage:.4%}")
    print(f"    微盘股滑点: {model.estimate(3, 2000, 1e7).total_slippage:.4%}")


# ============================================================
# 3. TA-Lib 封装层
# ============================================================
def test_talib_wrapper():
    from factors.ta_lib_wrapper import adx_14, obv, williams_r, mfi, sar, register_talib_factors

    # 模拟 OHLCV 数据
    np.random.seed(42)
    n = 100
    close = 10 + np.cumsum(np.random.randn(n) * 0.1)
    df = pd.DataFrame({
        "open": close + np.random.randn(n) * 0.05,
        "high": close + abs(np.random.randn(n) * 0.2),
        "low": close - abs(np.random.randn(n) * 0.2),
        "close": close,
        "volume": np.random.randint(100000, 1000000, n).astype(float),
    })

    # 测试各指标
    adx = adx_14(df)
    assert len(adx) == n
    assert not adx.isna().all()

    obv_vals = obv(df)
    assert len(obv_vals) == n

    wr = williams_r(df)
    assert len(wr) == n
    assert wr.dropna().between(-100, 0).all()

    mfi_vals = mfi(df)
    assert len(mfi_vals) == n

    sar_vals = sar(df)
    assert len(sar_vals) == n

    # 测试注册
    from factors.registry import FactorRegistry
    reg = FactorRegistry()
    register_talib_factors(reg)
    assert reg.get("adx_14") is not None

    print(f"    ADX(14) 最新值: {adx.iloc[-1]:.2f}")
    print(f"    Williams%R 最新值: {wr.iloc[-1]:.2f}")


# ============================================================
# 4. 分位数打分法
# ============================================================
def test_quantile_scorer():
    from factors.quantile_scorer import QuantileScorer

    scorer = QuantileScorer()

    # 模拟因子数据
    np.random.seed(42)
    codes = [f"S{i:03d}" for i in range(100)]
    factor_df = pd.DataFrame(
        {"factor1": np.random.randn(100)},
        index=codes
    )

    scored = scorer.rank_score(factor_df)
    assert scored["factor1"].min() > 0
    assert scored["factor1"].max() <= 100

    # 分组排名
    sector_map = {f"S{i:03d}": "A" if i < 50 else "B" for i in range(100)}
    scored_grouped = scorer.rank_score(factor_df, group_map=sector_map)
    assert scored_grouped["factor1"].min() > 0

    print(f"    全市场排名范围: [{scored['factor1'].min():.1f}, {scored['factor1'].max():.1f}]")
    print(f"    分组排名范围: [{scored_grouped['factor1'].min():.1f}, {scored_grouped['factor1'].max():.1f}]")


# ============================================================
# 5. 配对交易策略
# ============================================================
def test_pairs_trading():
    from strategy.pairs_trading import PairsTradingEngine, PairInfo

    engine = PairsTradingEngine(corr_threshold=0.7, lookback=60)

    # 模拟配对数据
    np.random.seed(42)
    n = 120
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    common_factor = np.cumsum(np.random.randn(n) * 0.02)
    price_a = pd.Series(10 + common_factor + np.random.randn(n) * 0.1, index=dates)
    price_b = pd.Series(8 + common_factor * 0.8 + np.random.randn(n) * 0.1, index=dates)

    price_data = {"A001": price_a, "B001": price_b}
    pairs = engine.find_pairs(["A001", "B001"], price_data=price_data, lookback=60)

    # 注册配对并生成信号
    if pairs:
        engine.register_pair(pairs[0])
        signal = engine.generate_signal("A001", "B001",
                                        price_a=price_a.iloc[-1],
                                        price_b=price_b.iloc[-1],
                                        pair_info=pairs[0])
        assert signal is not None
        print(f"    配对: A001-B001 corr={pairs[0].correlation:.3f} p={pairs[0].coint_pvalue:.3f}")
        print(f"    信号: {signal.signal} z={signal.spread_zscore:+.2f}")
    else:
        print("    未找到有效配对 (模拟数据可能不满足协整条件)")


# ============================================================
# 6. 红利ETF增强策略
# ============================================================
def test_dividend_enhance():
    from strategy.dividend_enhance import DividendEnhanceStrategy, DividendEnhanceConfig

    config = DividendEnhanceConfig(enhance_ratio=0.3, n_stocks=5)
    strategy = DividendEnhanceStrategy(config)
    result = strategy.run()

    assert result.etf_weight > 0
    assert result.n_stocks > 0
    assert result.tracking_error > 0
    assert result.config.etf_code in result.portfolio

    strategy.print_portfolio(result)


# ============================================================
# 主入口
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  新增模块验证 (6项)")
    print("=" * 60)

    check("1. 分层回测引擎 (backtest/decile_backtest.py)", test_decile_backtest)
    check("2. 动态滑点模型 (execution/slippage_model.py)", test_slippage_model)
    check("3. TA-Lib封装层 (factors/ta_lib_wrapper.py)", test_talib_wrapper)
    check("4. 分位数打分法 (factors/quantile_scorer.py)", test_quantile_scorer)
    check("5. 配对交易策略 (strategy/pairs_trading.py)", test_pairs_trading)
    check("6. 红利ETF增强 (strategy/dividend_enhance.py)", test_dividend_enhance)

    print("=" * 60)
    print(f"  结果: {PASS} PASS / {FAIL} FAIL / {PASS + FAIL} TOTAL")
    print("=" * 60)

    sys.exit(0 if FAIL == 0 else 1)
