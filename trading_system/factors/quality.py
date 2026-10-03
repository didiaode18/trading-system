# -*- coding: utf-8 -*-
"""
质量因子（Quality Factor）
===========================
核心逻辑: 高ROE + 低杠杆 + 高毛利率 = 优质企业

构造:
1. ROE: 净资产收益率（权重0.35）
2. gross_margin: 毛利率（权重0.25）
3. debt_ratio_inv: 1 - 资产负债率/100（权重0.20）
4. pe_quality: PE倒数（低PE高质量好，权重0.20）

方向: 正向（质量越高分越高）
预期: 熊市防御性极强，长期风险调整收益最优
数据源: fundamental表(roe/gross_margin/debt_ratio/pe_ttm)

注: 提供OHLCV代理版本用于无基本面数据时的回退。
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_quality_cross_section(fundamental_df: pd.DataFrame) -> pd.Series:
    """
    质量因子截面计算（多股票同一时点）

    参数:
        fundamental_df: 含 code/roe/gross_margin/debt_ratio/pe_ttm 列的DataFrame

    返回:
        pd.Series, index=code, value=质量因子得分
    """
    if fundamental_df is None or fundamental_df.empty:
        return pd.Series(dtype=float)

    df = fundamental_df.copy()
    scores = pd.Series(0.0, index=df["code"])
    weights_total = 0.0

    # 1. ROE（越高越好）
    if "roe" in df.columns:
        roe = pd.to_numeric(df["roe"], errors="coerce")
        roe_rank = roe.rank(pct=True, na_option="keep")
        scores += 0.35 * roe_rank.fillna(0.5)
        weights_total += 0.35

    # 2. 毛利率（越高越好）
    if "gross_margin" in df.columns:
        gm = pd.to_numeric(df["gross_margin"], errors="coerce")
        gm_rank = gm.rank(pct=True, na_option="keep")
        scores += 0.25 * gm_rank.fillna(0.5)
        weights_total += 0.25

    # 3. 负债率倒数（越低越好）
    if "debt_ratio" in df.columns:
        dr = pd.to_numeric(df["debt_ratio"], errors="coerce")
        dr_inv = 1.0 - dr / 100.0  # 负债率% → 资产净值比
        dr_rank = dr_inv.rank(pct=True, na_option="keep")
        scores += 0.20 * dr_rank.fillna(0.5)
        weights_total += 0.20

    # 4. PE质量（低PE = 高质量代理）
    if "pe_ttm" in df.columns:
        pe = pd.to_numeric(df["pe_ttm"], errors="coerce")
        # 剔除负PE（亏损股）
        pe = pe.where(pe > 0)
        pe_inv = 1.0 / pe
        pe_rank = pe_inv.rank(pct=True, na_option="keep")
        scores += 0.20 * pe_rank.fillna(0.5)
        weights_total += 0.20

    # 归一化
    if weights_total > 0:
        scores = scores / weights_total

    return scores.set_index(df["code"]) if "code" in df.columns else scores


def calc_quality_proxy(df: pd.DataFrame) -> pd.Series:
    """
    质量因子OHLCV代理版（无基本面数据时的回退）

    用价格稳定性 + 盈利持续性代理质量:
    1. 收益稳定性: 近60日中正收益天数的占比
    2. 回撤控制: 近60日最大回撤的倒数
    3. 波动稳定性: 近20日波动率/近60日波动率（越稳定越好）

    参数:
        df: 含 close 列的DataFrame

    返回:
        pd.Series（与df等长）
    """
    close = df["close"].values
    n = len(close)
    result = np.full(n, np.nan)

    if n < 61:
        return pd.Series(result, index=df.index)

    # 日收益率
    returns = np.zeros(n)
    for i in range(1, n):
        returns[i] = (close[i] - close[i-1]) / close[i-1] if close[i-1] > 0 else 0

    for i in range(60, n):
        window_ret = returns[i-59:i+1]

        # 1. 正收益天数占比
        positive_ratio = np.sum(window_ret > 0) / len(window_ret)

        # 2. 回撤控制的倒数
        peak = close[i-59]
        max_dd = 0
        for j in range(i-59, i+1):
            if close[j] > peak:
                peak = close[j]
            dd = (peak - close[j]) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        dd_score = 1.0 / (1.0 + max_dd * 10)  # 归一化

        # 3. 波动稳定性
        vol_20 = np.std(returns[i-19:i+1])
        vol_60 = np.std(returns[i-59:i+1])
        stability = vol_60 / (vol_20 + 1e-8)  # >1 表示近期波动低于长期

        # 合成
        result[i] = 0.4 * positive_ratio + 0.35 * dd_score + 0.25 * min(stability, 2.0) / 2.0

    return pd.Series(result, index=df.index)


# ============================================================
# V9.0 任务A: 标准质量因子（基本面列版）
# 逻辑口径抽取自 strategy/fundamental.py · financial_report.py · moat.py，
# 统一为 (df) -> Series 因子接口，注册后接受 IC 监控与降权。
# 数据缺失时返回全 NaN 静默降级，禁止填 0。
# ============================================================

def align_by_pub_date(df: pd.DataFrame, value_col: str,
                      pub_col: str = "pubDate",
                      date_col: str = "date") -> pd.Series:
    """
    报告期前视防护：财务数据仅在披露日(pubDate)之后可用

    t 期财报在披露日前属于未来信息，直接使用会引入前视偏差。
    本函数将 value_col 对齐到披露日：pubDate > 当前行 date 的记录置 NaN。

    参数:
        df: 含 date_col / value_col 的DataFrame，可选 pub_col 列
        value_col: 财务指标列名（如 roe）
        pub_col: 披露日列名（baostock 口径 pubDate）
        date_col: 交易日列名

    返回:
        pd.Series（与df等长），披露日之后的行才有值
    """
    values = pd.to_numeric(df[value_col], errors="coerce")
    if pub_col not in df.columns or date_col not in df.columns:
        # 无披露日字段：调用方自行保证口径，这里原样返回
        return values
    try:
        pub = pd.to_datetime(df[pub_col], errors="coerce")
        cur = pd.to_datetime(df[date_col], errors="coerce")
        # 披露日晚于当前交易日 → 前视，置 NaN
        values = values.where(pub <= cur)
        # 无披露日的行同样视为不可用
        values = values.where(pub.notna())
    except Exception as e:
        logger.debug(f"align_by_pub_date 对齐失败({value_col}): {e}")
    return values


def _fundamental_series(df: pd.DataFrame, col: str) -> pd.Series:
    """读取基本面列（自动做披露日对齐），缺列返回全 NaN。"""
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index)
    return align_by_pub_date(df, col)


def calc_roe(df: pd.DataFrame) -> pd.Series:
    """ROE 净资产收益率(%)，越高越好。缺 roe 列 → 全 NaN。"""
    return _fundamental_series(df, "roe")


def calc_gross_margin(df: pd.DataFrame) -> pd.Series:
    """毛利率(%)，越高越好。缺列 → 全 NaN。"""
    return _fundamental_series(df, "gross_margin")


def calc_net_margin(df: pd.DataFrame) -> pd.Series:
    """净利率(%)，越高越好。缺列 → 全 NaN。"""
    return _fundamental_series(df, "net_margin")


def calc_ocf_ratio(df: pd.DataFrame) -> pd.Series:
    """
    经营现金流/净利润（盈利含金量）

    净利润 <= 0 时无意义，置 NaN（而非截断为 0）。
    缺 ocf / net_profit 列 → 全 NaN。
    """
    if "ocf" not in df.columns or "net_profit" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    ocf = _fundamental_series(df, "ocf")
    np_ = _fundamental_series(df, "net_profit")
    np_valid = np_.where(np_ > 0)
    return ocf / np_valid


def calc_cashflow_stability(df: pd.DataFrame, window: int = 8) -> pd.Series:
    """
    现金流稳定性：经营现金流滚动均值/滚动标准差（类夏普，越高越稳）

    默认窗口 8 期（季度财报约 2 年）。标准差为 0 或缺列 → NaN。
    """
    if "ocf" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    ocf = _fundamental_series(df, "ocf")
    roll_mean = ocf.rolling(window, min_periods=window).mean()
    roll_std = ocf.rolling(window, min_periods=window).std()
    ratio = roll_mean / roll_std.where(roll_std > 0)
    return ratio


def calc_debt_ratio(df: pd.DataFrame) -> pd.Series:
    """资产负债率(%)，越低越好（注册方向 dir=-1）。缺列 → 全 NaN。"""
    return _fundamental_series(df, "debt_ratio")


def calc_roe_improvement(df: pd.DataFrame) -> pd.Series:
    """
    ROE 改善：环比差分 roe.diff()（正=盈利能力改善）

    首行与缺列 → NaN。
    """
    roe = _fundamental_series(df, "roe")
    return roe.diff()


# 因子注册字典
QUALITY_FACTORS = {
    "quality_proxy": {
        "func": calc_quality_proxy,
        "desc": "质量因子(OHLCV代理:收益稳定性+回撤控制)",
        "dir": 1,
    },
    "q_roe": {
        "func": calc_roe,
        "desc": "质量:ROE净资产收益率(披露日对齐)",
        "dir": 1,
    },
    "q_gross_margin": {
        "func": calc_gross_margin,
        "desc": "质量:毛利率(披露日对齐)",
        "dir": 1,
    },
    "q_net_margin": {
        "func": calc_net_margin,
        "desc": "质量:净利率(披露日对齐)",
        "dir": 1,
    },
    "q_ocf_ratio": {
        "func": calc_ocf_ratio,
        "desc": "质量:经营现金流/净利润(盈利含金量)",
        "dir": 1,
    },
    "q_cashflow_stability": {
        "func": calc_cashflow_stability,
        "desc": "质量:现金流稳定性(滚动均值/标准差)",
        "dir": 1,
    },
    "q_debt_ratio": {
        "func": calc_debt_ratio,
        "desc": "质量:资产负债率(越低越好)",
        "dir": -1,
    },
    "q_roe_improvement": {
        "func": calc_roe_improvement,
        "desc": "质量:ROE环比改善",
        "dir": 1,
    },
}
