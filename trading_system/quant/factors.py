"""
多因子选股引擎
===============
6个核心因子，等权合成，截面排名打分

P1-3: 因子计算已统一委托给 factors/quant_factors.py（单一数据源），
本模块仅保留 FactorEngine 的截面打分/选股/IC接口。

因子列表:
1. 动量因子: 20日收益率（正向，强者恒强）
2. 换手率因子: 5日平均换手率（适中，非极端）
3. 波动率因子: 20日收益率标准差（反向，低波优先）
4. 量价背离: 价涨量缩天数占比（反向，背离预警）
5. 均线趋势: MA5>MA10>MA20得分（正向）
6. 突破因子: 距60日新高距离（正向，接近新高）

使用方式:
    from quant.factors import FactorEngine
    fe = FactorEngine()
    scored = fe.score_universe(data_dict, date)
    selected = fe.select_stocks(scored, top_n=10)
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class FactorEngine:
    """多因子选股引擎"""

    def __init__(self, factor_weights: dict = None):
        """
        参数:
            factor_weights: 因子权重，默认使用优化权重（降低动量、增加回调）
        """
        self.factor_names = [
            "momentum",      # 动量（降权）
            "turnover",      # 换手率
            "volatility",    # 波动率
            "vol_price_div", # 量价背离
            "ma_trend",      # 均线趋势
            "breakout",      # 突破
            "pullback",      # 回调买入因子（新增）
        ]
        # 优化权重: 降低动量(10%)、增加回调(15%)、增加趋势(20%)
        if factor_weights is None:
            self.weights = {
                "momentum": 0.10,      # 降权: 减少追涨
                "turnover": 0.10,
                "volatility": 0.15,
                "vol_price_div": 0.10,
                "ma_trend": 0.20,      # 增权: 趋势确认
                "breakout": 0.10,      # 降权: 减少追突破
                "pullback": 0.25,      # 新增: 回调买入为主
            }
        else:
            self.weights = factor_weights

    # ============================================================
    # 一、单只股票因子计算
    # ============================================================

    def compute_factors(self, df: pd.DataFrame) -> dict:
        """
        计算单只股票在最新日期的所有因子值

        P1-3: 委托给 factors/quant_factors.py（单一数据源），取序列末值。

        参数:
            df: 日线数据（至少60行），含 date/open/close/high/low/volume

        返回:
            {factor_name: value} 字典，None表示数据不足
        """
        if df is None or len(df) < 60:
            return None

        # P1-3: 统一委托给 factors/quant_factors.py，消除重复实现
        from factors.quant_factors import (
            calc_qe_momentum, calc_qe_turnover, calc_qe_volatility,
            calc_qe_vol_price_div, calc_qe_ma_trend, calc_qe_breakout,
            calc_qe_pullback,
        )

        def _last_val(series):
            """取 Series 最后一个非 NaN 值，全 NaN 则返回 None"""
            v = series.dropna()
            return float(v.iloc[-1]) if len(v) > 0 else None

        # BUG-053修复: 用显式None判断替代or，避免合法值0.0被替换为默认值
        def _safe(val, default):
            return val if val is not None else default

        factors = {
            "momentum":      _safe(_last_val(calc_qe_momentum(df)), 0),
            "turnover":      _safe(_last_val(calc_qe_turnover(df)), 1.0),
            "volatility":    _safe(_last_val(calc_qe_volatility(df)), 0.05),
            "vol_price_div": _safe(_last_val(calc_qe_vol_price_div(df)), 0),
            "ma_trend":      _safe(_last_val(calc_qe_ma_trend(df)), 0),
            "breakout":      _safe(_last_val(calc_qe_breakout(df)), 0.5),
            "pullback":      _safe(_last_val(calc_qe_pullback(df)), 0),
        }
        return factors

    # ============================================================
    # 二、截面排名 + 等权合成
    # ============================================================

    def score_universe(self, data_dict: dict, date: str = None) -> pd.DataFrame:
        """
        对整个股票池进行截面打分

        参数:
            data_dict: {code: DataFrame}
            date: 截止日期（只用该日期及之前的数据），None则用全部

        返回:
            DataFrame: code, momentum, turnover, ..., total_score
            按total_score降序排列
        """
        records = []

        for code, df in data_dict.items():
            # 截取到指定日期
            if date is not None:
                df_cut = df[df["date"] <= date].copy()
            else:
                df_cut = df

            if len(df_cut) < 60:
                continue

            factors = self.compute_factors(df_cut)
            if factors is None:
                continue

            factors["code"] = code
            records.append(factors)

        if not records:
            return pd.DataFrame()

        factor_df = pd.DataFrame(records)

        # 截面排名（百分位 0-1）
        for name in self.factor_names:
            if name not in factor_df.columns:
                factor_df[name] = 0.5
                continue

            col = factor_df[name]

            if name == "volatility":
                # 反向：低波动排名靠前
                factor_df[f"{name}_rank"] = 1 - col.rank(pct=True)
            elif name == "vol_price_div":
                # 反向：低背离排名靠前
                factor_df[f"{name}_rank"] = 1 - col.rank(pct=True)
            elif name == "turnover":
                # 适中：距中位数越近越好
                median_val = col.median()
                factor_df[f"{name}_rank"] = 1 - (col - median_val).abs().rank(pct=True)
            elif name == "pullback":
                # 正向：回调得分越高越好
                factor_df[f"{name}_rank"] = col.rank(pct=True)
            else:
                # 正向：值越大排名越靠前
                factor_df[f"{name}_rank"] = col.rank(pct=True)

        # 等权合成总分（0-100）
        factor_df["total_score"] = 0
        for name in self.factor_names:
            rank_col = f"{name}_rank"
            if rank_col in factor_df.columns:
                factor_df["total_score"] += factor_df[rank_col] * self.weights.get(name, 0) * 100

        # 按总分降序
        factor_df = factor_df.sort_values("total_score", ascending=False).reset_index(drop=True)

        return factor_df

    # ============================================================
    # 三、选股
    # ============================================================

    def select_stocks(self, scored_df: pd.DataFrame,
                      top_n: int = 10,
                      min_score: float = 50,
                      max_volatility: float = 0.05) -> list:
        """
        选出TOP N只股票（增加波动率过滤）

        参数:
            scored_df: score_universe()的输出
            top_n: 选股数量
            min_score: 最低分数线
            max_volatility: 最大波动率阈值（日收益率标准差，超过则排除）

        返回:
            [(code, score), ...] 列表
        """
        if scored_df.empty:
            return []

        # 过滤最低分
        qualified = scored_df[scored_df["total_score"] >= min_score]

        # 波动率过滤: 排除日均波动>5%的高波动标的（减少追涨杀跌）
        if "volatility" in qualified.columns and max_volatility > 0:
            qualified = qualified[qualified["volatility"] <= max_volatility]

        # 取TOP N
        selected = qualified.head(top_n)

        return [(row["code"], row["total_score"]) for _, row in selected.iterrows()]

    # ============================================================
    # 四、因子IC计算（用于后续监控）
    # ============================================================

    def calc_factor_ic(self, data_dict: dict, date: str,
                       forward_days: int = 5) -> dict:
        """
        计算各因子的IC值（信息系数）

        IC = 因子值与未来N日收益的截面相关系数
        |IC| > 0.03 为有效因子

        参数:
            data_dict: 股票数据
            date: 因子计算日期
            forward_days: 前瞻收益天数

        返回:
            {factor_name: ic_value}
        """
        records = []

        for code, df in data_dict.items():
            df_cut = df[df["date"] <= date]
            df_future = df[df["date"] > date].head(forward_days)

            if len(df_cut) < 60 or df_future.empty:
                continue

            factors = self.compute_factors(df_cut)
            if factors is None:
                continue

            # 未来N日收益
            future_return = (df_future.iloc[-1]["close"] - df_cut.iloc[-1]["close"]) / df_cut.iloc[-1]["close"]
            factors["forward_return"] = future_return
            factors["code"] = code
            records.append(factors)

        if len(records) < 30:
            return {}

        df_all = pd.DataFrame(records)
        ic_dict = {}

        for name in self.factor_names:
            if name in df_all.columns:
                ic = df_all[name].corr(df_all["forward_return"], method="spearman")
                ic_dict[name] = round(ic, 4) if not np.isnan(ic) else 0

        return ic_dict
