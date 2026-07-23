"""
筹码分布分析模块
================
基于历史成交量加权计算持仓成本分布（A股实战核心指标）

核心功能:
  1. 筹码分布计算：基于衰减因子的成交量加权成本分布
  2. 套牢盘比例：当前价上方的筹码占比（反弹压力）
  3. 获利盘比例：当前价下方的筹码占比（抛压风险）
  4. 筹码集中度：90%/70%筹码集中区间
  5. 成本重心：加权平均持仓成本
  6. 单峰/多峰判定：筹码是否集中

原理:
  每天的成交量代表当天换手的筹码，新筹码以当天均价为成本。
  随着时间推移，旧筹码被新交易替代（衰减因子模拟换手）。
  最终得到"当前市场上所有持仓者的成本分布"。

使用方式:
    from strategy.chip_distribution import ChipAnalyzer
    analyzer = ChipAnalyzer()
    result = analyzer.analyze(df, current_price=None)
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class ChipAnalyzer:
    """筹码分布分析器"""

    def __init__(self, decay_factor: float = 0.95, price_bins: int = 100):
        """
        参数:
            decay_factor: 每日筹码衰减因子（模拟换手率）
                          0.95 = 每天约5%的筹码被换掉
                          0.90 = 每天约10%（更激进的换手假设）
            price_bins: 价格分箱数量（精度）
        """
        self.decay_factor = decay_factor
        self.price_bins = price_bins

    def analyze(self, df: pd.DataFrame, current_price: float = None,
                lookback: int = 120) -> dict:
        """
        计算筹码分布并输出分析结果
        
        参数:
            df: 日线DataFrame (需含 date, open, close, high, low, volume)
            current_price: 当前价格（默认取最新收盘价）
            lookback: 回看天数（默认120天）
        
        返回:
            {
                "current_price": float,
                "cost_center": float,         # 成本重心（加权均价）
                "profit_ratio": float,        # 获利盘比例 (0~1)
                "trapped_ratio": float,       # 套牢盘比例 (0~1)
                "concentration_90": (low, high),  # 90%筹码集中区间
                "concentration_70": (low, high),  # 70%筹码集中区间
                "peak_count": int,            # 筹码峰数量
                "peaks": list,                # 各峰位置和占比
                "is_single_peak": bool,       # 是否单峰密集
                "pressure_above": float,      # 上方压力（套牢盘密度）
                "support_below": float,       # 下方支撑（获利盘密度）
                "chip_score": float,          # 筹码综合评分 (0~100)
                "signals": list,              # 筹码信号
                "distribution": dict,         # 完整分布数据
            }
        """
        if df is None or len(df) < 20:
            return self._empty_result(current_price)

        # 取最近lookback天
        data = df.tail(lookback).copy()
        if current_price is None:
            current_price = data["close"].iloc[-1]

        # ---- 1. 计算筹码分布 ----
        distribution = self._compute_distribution(data)

        if distribution is None or distribution.sum() == 0:
            return self._empty_result(current_price)

        # 归一化
        distribution = distribution / distribution.sum()
        price_axis = distribution.index.values

        # ---- 2. 核心指标计算 ----
        # 成本重心
        cost_center = np.average(price_axis, weights=distribution.values)

        # 获利盘/套牢盘
        profit_mask = price_axis < current_price
        trapped_mask = price_axis >= current_price
        profit_ratio = distribution[profit_mask].sum()
        trapped_ratio = distribution[trapped_mask].sum()

        # 集中度区间
        conc_90 = self._concentration_range(distribution, 0.90)
        conc_70 = self._concentration_range(distribution, 0.70)

        # 筹码峰检测
        peaks = self._detect_peaks(distribution)
        peak_count = len(peaks)
        is_single_peak = peak_count <= 1

        # 上方压力/下方支撑
        pressure_above = self._calc_pressure(distribution, current_price, "above")
        support_below = self._calc_pressure(distribution, current_price, "below")

        # ---- 3. 综合评分 ----
        chip_score = self._calc_chip_score(
            profit_ratio, trapped_ratio, is_single_peak,
            conc_70, current_price, cost_center, peaks
        )

        # ---- 4. 信号生成 ----
        signals = self._generate_signals(
            profit_ratio, trapped_ratio, is_single_peak,
            conc_70, current_price, cost_center, peaks, pressure_above
        )

        return {
            "current_price": round(current_price, 3),
            "cost_center": round(cost_center, 3),
            "profit_ratio": round(profit_ratio, 4),
            "trapped_ratio": round(trapped_ratio, 4),
            "concentration_90": (round(conc_90[0], 3), round(conc_90[1], 3)),
            "concentration_70": (round(conc_70[0], 3), round(conc_70[1], 3)),
            "peak_count": peak_count,
            "peaks": peaks,
            "is_single_peak": is_single_peak,
            "pressure_above": round(pressure_above, 4),
            "support_below": round(support_below, 4),
            "chip_score": round(chip_score, 1),
            "signals": signals,
            "distribution": {
                "prices": price_axis.tolist(),
                "weights": distribution.values.tolist(),
            },
        }

    def _compute_distribution(self, data: pd.DataFrame) -> pd.Series:
        """
        计算筹码分布（衰减加权法）
        
        每天的新增筹码 = 当天成交量 × 衰减权重
        筹码成本 = 当天均价 (high+low+close)/3 或 (open+close)/2
        """
        # 确定价格范围
        price_min = data["low"].min() * 0.95
        price_max = data["high"].max() * 1.05
        bins = np.linspace(price_min, price_max, self.price_bins + 1)
        bin_centers = (bins[:-1] + bins[1:]) / 2

        # 初始化筹码分布
        chip_dist = np.zeros(self.price_bins)

        n = len(data)
        for i in range(n):
            row = data.iloc[i]
            # 当天筹码衰减
            chip_dist *= self.decay_factor

            # 当天新增筹码（以均价为成本，三角分布模拟）
            avg_price = (row["high"] + row["low"] + row["close"]) / 3
            vol = row["volume"]

            if vol <= 0 or avg_price <= 0:
                continue

            # 三角分布：以均价为峰，high-low为范围
            low_p = row["low"]
            high_p = row["high"]
            if high_p <= low_p:
                high_p = low_p * 1.01

            # 将成交量按三角分布分配到各价格bin
            for j, bp in enumerate(bin_centers):
                if low_p <= bp <= high_p:
                    # 三角权重：越接近均价权重越大
                    if bp <= avg_price:
                        w = (bp - low_p) / (avg_price - low_p + 1e-10)
                    else:
                        w = (high_p - bp) / (high_p - avg_price + 1e-10)
                    chip_dist[j] += vol * max(0, w)

        return pd.Series(chip_dist, index=bin_centers)

    def _concentration_range(self, distribution: pd.Series, pct: float) -> tuple:
        """计算pct%筹码集中区间"""
        cumsum = distribution.cumsum()
        total = cumsum.iloc[-1]
        if total == 0:
            return (0, 0)

        lower_pct = (1 - pct) / 2
        upper_pct = 1 - lower_pct

        lower_idx = (cumsum >= total * lower_pct).idxmax()
        upper_idx = (cumsum >= total * upper_pct).idxmax()

        return (lower_idx, upper_idx)

    def _detect_peaks(self, distribution: pd.Series, min_prominence: float = 0.02) -> list:
        """
        检测筹码峰（局部极大值）
        
        返回: [{"price": float, "weight": float, "label": str}, ...]
        """
        values = distribution.values
        prices = distribution.index.values
        peaks = []

        for i in range(1, len(values) - 1):
            if values[i] > values[i-1] and values[i] > values[i+1]:
                # 检查显著性
                if values[i] >= min_prominence * values.max():
                    peaks.append({
                        "price": round(prices[i], 3),
                        "weight": round(values[i] / values.sum(), 4),
                        "label": f"峰@{prices[i]:.2f}",
                    })

        # 合并相邻峰（距离<2%的合并）
        merged = []
        for p in peaks:
            if merged and abs(p["price"] - merged[-1]["price"]) / merged[-1]["price"] < 0.02:
                # 合并：取权重更大的
                if p["weight"] > merged[-1]["weight"]:
                    merged[-1] = p
            else:
                merged.append(p)

        # 按权重排序
        merged.sort(key=lambda x: x["weight"], reverse=True)
        return merged[:5]  # 最多5个峰

    def _calc_pressure(self, distribution: pd.Series, current_price: float,
                       direction: str) -> float:
        """计算上方压力或下方支撑强度"""
        if direction == "above":
            mask = distribution.index >= current_price
        else:
            mask = distribution.index <= current_price

        relevant = distribution[mask]
        if relevant.sum() == 0:
            return 0.0

        # 距离加权的压力/支撑
        distances = abs(distribution.index[mask] - current_price) / current_price
        # 越近的筹码压力/支撑越大
        weighted = relevant.values * np.exp(-distances.values * 10)
        return weighted.sum() / distribution.sum()

    def _calc_chip_score(self, profit_ratio, trapped_ratio, is_single_peak,
                         conc_70, current_price, cost_center, peaks) -> float:
        """
        筹码综合评分 (0~100)
        
        高分 = 筹码结构健康（适合持有/买入）
        低分 = 筹码结构恶劣（抛压大/支撑弱）
        """
        score = 50.0

        # 1. 获利盘比例（适中为佳：30%~70%）
        if 0.3 <= profit_ratio <= 0.7:
            score += 10  # 健康
        elif profit_ratio > 0.85:
            score -= 10  # 获利盘太多，抛压风险
        elif profit_ratio < 0.15:
            score -= 5   # 套牢盘太多，反弹困难

        # 2. 单峰密集加分
        if is_single_peak:
            score += 15
            # 单峰在当前价附近（±5%）最佳
            if peaks and abs(peaks[0]["price"] - current_price) / current_price < 0.05:
                score += 10
        else:
            score -= 5

        # 3. 集中度（70%筹码区间越窄越好）
        if conc_70[1] > conc_70[0] and current_price > 0:
            conc_width = (conc_70[1] - conc_70[0]) / current_price
            if conc_width < 0.10:
                score += 10  # 高度集中
            elif conc_width < 0.20:
                score += 5
            elif conc_width > 0.40:
                score -= 10  # 极度分散

        # 4. 成本重心与现价关系
        if cost_center > 0:
            deviation = (current_price - cost_center) / cost_center
            if 0 < deviation < 0.10:
                score += 5   # 略高于成本，健康
            elif deviation > 0.30:
                score -= 5   # 远高于成本，获利回吐风险
            elif deviation < -0.10:
                score -= 10  # 低于成本，套牢区

        return max(0, min(100, score))

    def _generate_signals(self, profit_ratio, trapped_ratio, is_single_peak,
                          conc_70, current_price, cost_center, peaks,
                          pressure_above) -> list:
        """生成筹码信号"""
        signals = []

        # 单峰密集 + 价格刚突破峰值 → 强烈看多
        if is_single_peak and peaks:
            peak_price = peaks[0]["price"]
            if current_price > peak_price * 1.02 and profit_ratio > 0.6:
                signals.append("★筹码单峰突破: 价格站上密集区，上方无套牢盘")

        # 获利盘 > 90% → 抛压预警
        if profit_ratio > 0.90:
            signals.append("⚠️获利盘>90%: 短期抛压极大，注意回调")

        # 套牢盘 > 70% → 反弹困难
        if trapped_ratio > 0.70:
            signals.append("⚠️套牢盘>70%: 上方压力沉重，反弹空间有限")

        # 多峰发散 → 方向不明
        if not is_single_peak and len(peaks) >= 3:
            signals.append("筹码多峰发散: 多空分歧大，方向待选择")

        # 价格接近成本重心 → 变盘信号
        if cost_center > 0:
            dev = abs(current_price - cost_center) / cost_center
            if dev < 0.03:
                signals.append("价格贴近成本重心: 即将选择方向")

        # 上方压力密集
        if pressure_above > 0.15:
            signals.append(f"上方压力密集({pressure_above:.0%}): 突破需要放量")

        return signals

    def _empty_result(self, current_price) -> dict:
        """数据不足时的空结果"""
        return {
            "current_price": current_price or 0,
            "cost_center": 0,
            "profit_ratio": 0.5,
            "trapped_ratio": 0.5,
            "concentration_90": (0, 0),
            "concentration_70": (0, 0),
            "peak_count": 0,
            "peaks": [],
            "is_single_peak": False,
            "pressure_above": 0,
            "support_below": 0,
            "chip_score": 50,
            "signals": ["数据不足，无法计算筹码分布"],
            "distribution": None,
        }


# ============================================================
# 便捷函数
# ============================================================

def analyze_chip_distribution(df: pd.DataFrame, current_price: float = None,
                              lookback: int = 120) -> dict:
    """便捷函数：分析单只股票的筹码分布"""
    analyzer = ChipAnalyzer()
    return analyzer.analyze(df, current_price, lookback)


def batch_chip_analysis(data_dict: dict, holdings: dict = None) -> dict:
    """
    批量筹码分析
    
    参数:
        data_dict: {code: DataFrame}
        holdings: {code: holding_info}（可选，用于标注持仓成本线）
    
    返回:
        {code: chip_result}
    """
    analyzer = ChipAnalyzer()
    results = {}
    for code, df in data_dict.items():
        if code == "000300":  # 跳过指数
            continue
        result = analyzer.analyze(df)
        # 如果有持仓，标注成本线位置
        if holdings and code in holdings:
            buy_price = holdings[code].get("buy_price", 0)
            if buy_price > 0:
                result["holding_cost"] = buy_price
                result["cost_vs_center"] = round(
                    (buy_price - result["cost_center"]) / result["cost_center"] * 100, 1
                ) if result["cost_center"] > 0 else 0
        results[code] = result
    return results
