# -*- coding: utf-8 -*-
"""
质量因子 + 情绪因子入库 测试（quant-system-upgrade 任务A）
==========================================================
覆盖验收清单：报告期前视检查、缺失数据降级（NaN 而非 0）、
注册表 sentiment/quality 两类因子齐全、口径正确性抽查。
全部用合成数据，不依赖真实数据源。
"""

import numpy as np
import pandas as pd
import pytest

from factors.quality import (
    align_by_pub_date, calc_roe, calc_ocf_ratio, calc_cashflow_stability,
    calc_debt_ratio, calc_roe_improvement, QUALITY_FACTORS,
)
from factors.sentiment import (
    calc_limit_ratio, calc_turnover_rate, calc_volume_ratio,
    calc_margin_balance_chg, calc_lhb_net_buy, calc_seal_rate,
    calc_north_flow_ratio, SENTIMENT_FACTORS,
)
from factors.registry import get_registry


# ---- 合成数据工具 ----
def _make_ohlcv(closes, volumes=None, highs=None):
    n = len(closes)
    closes = [float(c) for c in closes]
    return pd.DataFrame({
        "date": pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "close": closes,
        "high": [float(h) for h in highs] if highs is not None else [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes],
        "volume": [float(v) for v in volumes] if volumes is not None else [1_000_000.0] * n,
        "amount": [c * 1_000_000 for c in closes],
    })


# ============================================================
# 质量因子：报告期前视检查
# ============================================================
def test_quality_lookahead_protection():
    """t期财报在披露日之前不可用（前视防护）"""
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"],
        "pubDate": ["2026-01-15", "2026-04-30", "2026-03-01", None],
        "roe": [10.0, 12.0, 14.0, 16.0],
    })
    aligned = align_by_pub_date(df, "roe")
    # 第0行 pubDate(1/15) > date(1/1) → 前视，应为 NaN
    assert np.isnan(aligned.iloc[0])
    assert np.isnan(aligned.iloc[1])          # pubDate 4/30 > 2/1 → 前视 NaN
    assert aligned.iloc[2] == 14.0            # pubDate 3/1 == date 3/1 → 可用
    assert np.isnan(aligned.iloc[3])          # 无披露日 → 不可用


def test_quality_no_pubdate_column_passthrough():
    """无 pubDate 列时原样返回（调用方自行保证口径）"""
    df = pd.DataFrame({"date": ["2026-01-01", "2026-01-02"], "roe": [10.0, 11.0]})
    aligned = align_by_pub_date(df, "roe")
    assert aligned.tolist() == [10.0, 11.0]


def test_roe_factor_with_alignment():
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-02-01"],
        "pubDate": ["2025-12-01", "2026-05-01"],
        "roe": [10.0, 12.0],
    })
    roe = calc_roe(df)
    assert roe.iloc[0] == 10.0     # 披露日早于交易日 → 可用
    assert np.isnan(roe.iloc[1])   # 披露日晚于交易日 → NaN


# ============================================================
# 质量因子：缺失数据降级（NaN，禁止填0）
# ============================================================
def test_quality_missing_columns_degrade_to_nan():
    df = _make_ohlcv([10.0] * 30)  # 纯OHLCV，无任何基本面列
    for fn in [calc_roe, calc_ocf_ratio, calc_cashflow_stability,
               calc_debt_ratio, calc_roe_improvement]:
        s = fn(df)
        assert len(s) == len(df)
        assert s.isna().all(), f"{fn.__name__} 缺列时应全NaN"


def test_ocf_ratio_negative_profit_nan():
    """净利润<=0 时 OCF/NP 无意义 → NaN（而非截断为0）"""
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-02-01"],
        "ocf": [5e7, -3e7],
        "net_profit": [-1e7, -5e7],
    })
    ratio = calc_ocf_ratio(df)
    assert ratio.isna().all()


def test_cashflow_stability_values():
    df = pd.DataFrame({
        "date": pd.bdate_range("2026-01-01", periods=10).strftime("%Y-%m-%d"),
        "ocf": [100.0] * 10,  # 完全稳定 → std=0 → NaN（不编造inf）
    })
    s = calc_cashflow_stability(df, window=8)
    assert s.isna().all()
    df2 = df.copy()
    df2["ocf"] = [100.0, 110.0, 95.0, 105.0, 100.0, 108.0, 97.0, 103.0, 101.0, 99.0]
    s2 = calc_cashflow_stability(df2, window=8)
    assert s2.iloc[-1] > 3.0  # 均值~102/标准差~5 → 高稳定性


def test_roe_improvement_diff():
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-02-01", "2026-03-01"],
        "roe": [10.0, 12.0, 9.0],
    })
    imp = calc_roe_improvement(df)
    assert np.isnan(imp.iloc[0])
    assert imp.iloc[1] == pytest.approx(2.0)
    assert imp.iloc[2] == pytest.approx(-3.0)


# ============================================================
# 情绪因子：OHLCV可算口径
# ============================================================
def _limit_up_down_df():
    """25根平稳K线 + 3涨停 + 2跌停（涨停日high=close*1.01，含1天触板未封）"""
    closes = [10.0] * 25
    closes += [11.0, 12.1, 13.31]                 # 3个涨停（收盘封板）
    closes += [12.0, 10.8]                         # 2个跌停
    n = len(closes)
    highs = [c * 1.005 for c in closes]
    volumes = [1_000_000.0] * n
    df = _make_ohlcv(closes, volumes, highs)
    # 追加1天触板未封：high=涨停价，close回落
    extra = pd.DataFrame({
        "date": ["2026-03-01"], "close": [11.0], "high": [10.8 * 1.10],
        "low": [10.5], "volume": [2_000_000.0], "amount": [2.2e7],
    })
    return pd.concat([df, extra], ignore_index=True)


def test_limit_ratio_counts():
    df = _limit_up_down_df()
    lr = calc_limit_ratio(df)
    # 末20日窗口内：3涨停2跌停 → (3-2)/20 = 0.05
    assert lr.iloc[-1] == pytest.approx(0.05)
    # 前段平稳K线（窗口已满且无NaN）→ 0.0（真实可观测的中性）
    assert lr.iloc[20] == pytest.approx(0.0)


def test_limit_ratio_insufficient_bars_nan():
    df = _make_ohlcv([10.0] * 19)  # <20根
    assert calc_limit_ratio(df).isna().all()


def test_volume_ratio():
    closes = [10.0] * 10
    volumes = [1_000_000.0] * 9 + [2_000_000.0]
    df = _make_ohlcv(closes, volumes)
    vr = calc_volume_ratio(df)
    assert vr.iloc[-1] == pytest.approx(2.0)  # 2倍放量


def test_seal_rate():
    df = _limit_up_down_df()
    sr = calc_seal_rate(df)
    # 末20日窗口：触板4天（3封板涨停+1触板未封），封板3天 → 0.75
    assert sr.iloc[-1] == pytest.approx(0.75)


def test_seal_rate_no_touch_nan():
    """近20日无触板 → NaN（情绪不可观测，禁止填0）"""
    df = _make_ohlcv([10.0] * 30)  # 平稳无涨停
    assert calc_seal_rate(df).isna().all()


# ============================================================
# 情绪因子：外部数据缺失降级（NaN，禁止填0）
# ============================================================
def test_sentiment_missing_external_data_nan():
    df = _make_ohlcv([10.0] * 30)  # 无 turnover/margin_balance/lhb/north_flow 列
    for fn in [calc_turnover_rate, calc_margin_balance_chg,
               calc_lhb_net_buy, calc_north_flow_ratio]:
        s = fn(df)
        assert len(s) == len(df)
        assert s.isna().all(), f"{fn.__name__} 数据缺失时应全NaN而非0"


def test_sentiment_with_data_columns():
    n = 30
    df = _make_ohlcv([10.0] * n)
    df["turnover"] = [2.5] * n
    df["margin_balance"] = list(np.linspace(1e9, 1.05e9, n))
    df["lhb_net_amount"] = [0.0] * (n - 1) + [5e6]
    df["north_flow"] = [1e6] * n
    assert calc_turnover_rate(df).iloc[-1] == pytest.approx(2.5)
    mb = calc_margin_balance_chg(df)
    # 融资余额由1e9线性增至1.05e9，5日变化率≈+0.83%
    assert mb.iloc[-1] == pytest.approx(0.00828, abs=1e-4)
    lhb = calc_lhb_net_buy(df)
    assert lhb.iloc[-2] == pytest.approx(0.0)      # 未上榜=真实0，保留
    assert lhb.iloc[-1] > 0                        # 净买入500万/成交额1000万
    assert calc_north_flow_ratio(df).iloc[-1] == pytest.approx(0.1)


# ============================================================
# 注册表：五类因子齐全
# ============================================================
def test_registry_has_quality_and_sentiment():
    reg = get_registry()
    # 质量因子（Phase2 + V9.0扩展）
    for name in ["quality_proxy", "q_roe", "q_gross_margin", "q_net_margin",
                 "q_ocf_ratio", "q_cashflow_stability", "q_roe_improvement"]:
        meta = reg.get(name)
        assert meta is not None, f"质量因子 {name} 未注册"
        assert meta.category == "quality"
    # 情绪因子（V9.0新增）
    for name in SENTIMENT_FACTORS:
        meta = reg.get(name)
        assert meta is not None, f"情绪因子 {name} 未注册"
        assert meta.category == "sentiment"
    # 原有大类不受影响
    for cat in ["technical", "volume", "momentum"]:
        assert len(reg.list_by_category(cat)) > 0


def test_debt_ratio_direction_negative():
    reg = get_registry()
    meta = reg.get("q_debt_ratio")
    assert meta is not None
    assert meta.direction == -1  # 资产负债率越低越好


def test_compute_all_includes_new_factors():
    """compute_all 全量计算不报错，新因子列存在（值可为NaN）"""
    reg = get_registry()
    df = _limit_up_down_df()
    out = reg.compute_all(df, categories=["sentiment", "quality"])
    assert "limit_ratio" in out.columns
    assert "volume_ratio" in out.columns
    # 无基本面列 → 质量因子不产出列（长度校验通过但值为NaN被保留）或列缺失均可，
    # 关键是不抛异常、不产生0填充
    if "q_roe" in out.columns:
        assert out["q_roe"].isna().all()
