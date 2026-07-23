# -*- coding: utf-8 -*-
"""
筹码分布系统 (Chip Distribution / CYQ)
======================================
对标同花顺/东方财富付费筹码图，用免费K线数据估算筹码分布

核心算法:
  1. 每日成交量按价格区间[low, high]三角分布
  2. 历史筹码按换手率衰减（模拟筹码转移）
  3. 累积得到当前筹码分布曲线

输出指标:
  - 获利盘比例: 当前价以下筹码占比
  - 套牢盘比例: 当前价以上筹码占比
  - 筹码密集峰: 90%筹码集中的价格区间
  - 成本重心: 加权平均持仓成本
  - 集中度(ASR): 筹码分布的离散程度
  - 支撑位: 下方最近筹码密集峰
  - 压力位: 上方最近套牢盘密集区

应用:
  - 主力控盘判断: 获利盘>80% + 集中度高 = 高度控盘
  - 支撑压力: 筹码密集峰 = 天然支撑/压力
  - 买卖信号: 获利盘骤降 = 出货; 套牢盘割肉 = 底部

使用:
    from strategy.chip_distribution import ChipAnalyzer
    analyzer = ChipAnalyzer()
    result = analyzer.analyze(df, current_price=35.77)
"""

import pandas as pd
import numpy as np
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 筹码分布配置
CHIP_CONFIG = {
    "price_bins": 100,          # 价格分箱数（精度）
    "decay_factor": 1.0,        # 换手率衰减系数（1.0=标准衰减）
    "min_days": 60,             # 最少计算天数
    "peak_threshold": 0.03,     # 密集峰判定阈值（占比>3%）
    "concentration_pct": 0.90,  # 集中度计算: 90%筹码区间
    "control_threshold": 0.80,  # 主力控盘: 获利盘>80%
    "panic_threshold": 0.20,    # 恐慌割肉: 获利盘<20%
}


class ChipAnalyzer:
    """筹码分布分析器"""

    def __init__(self, config: dict = None):
        self.cfg = {**CHIP_CONFIG, **(config or {})}

    def analyze(self, df: pd.DataFrame, current_price: float = None,
                lookback: int = 250) -> dict:
        """
        完整筹码分析

        参数:
            df: 含 date/open/high/low/close/volume 的DataFrame
            current_price: 当前价格（None则取最后收盘价）
            lookback: 回看天数（默认250天≈1年）

        返回:
            筹码分析结果字典
        """
        if df is None or len(df) < self.cfg["min_days"]:
            return {"error": f"数据不足(需≥{self.cfg['min_days']}根K线)"}

        # 取最近N天
        df_calc = df.tail(lookback).copy().reset_index(drop=True)

        if current_price is None:
            current_price = df_calc.iloc[-1]["close"]

        # 1. 计算筹码分布
        chip_dist = self._calc_chip_distribution(df_calc)

        # 2. 提取关键指标
        price_bins = chip_dist["price_bins"]
        chips = chip_dist["chips"]
        total_chips = chips.sum()

        if total_chips <= 0:
            return {"error": "筹码计算异常(总量为0)"}

        # 归一化
        chips_pct = chips / total_chips

        # 3. 获利盘/套牢盘
        profit_ratio, trapped_ratio = self._calc_profit_ratio(
            price_bins, chips_pct, current_price)

        # 4. 成本重心
        cost_center = self._calc_cost_center(price_bins, chips_pct)

        # 5. 筹码集中度 (90%筹码区间)
        conc_low, conc_high, concentration = self._calc_concentration(
            price_bins, chips_pct)

        # 6. 筹码密集峰
        peaks = self._find_peaks(price_bins, chips_pct)

        # 7. 支撑/压力位
        support, resistance = self._find_support_resistance(
            price_bins, chips_pct, current_price, peaks)

        # 8. 主力控盘度
        control_level = self._assess_control(
            profit_ratio, concentration, cost_center, current_price)

        # 9. 筹码形态判断
        pattern = self._identify_pattern(
            profit_ratio, concentration, peaks, current_price, cost_center)

        # 10. 历史获利盘变化（最近5天）
        profit_history = self._calc_profit_history(df_calc, lookback=5)

        return {
            "current_price": round(current_price, 3),
            "profit_ratio": round(profit_ratio, 4),       # 获利盘比例 0-1
            "trapped_ratio": round(trapped_ratio, 4),     # 套牢盘比例 0-1
            "cost_center": round(cost_center, 3),         # 成本重心
            "concentration": round(concentration, 4),     # 集中度(越小越集中)
            "conc_low": round(conc_low, 3),              # 90%筹码下界
            "conc_high": round(conc_high, 3),            # 90%筹码上界
            "peaks": peaks,                               # 密集峰列表
            "support": round(support, 3),                # 筹码支撑位
            "resistance": round(resistance, 3),          # 筹码压力位
            "control_level": control_level,              # 控盘度评估
            "pattern": pattern,                          # 筹码形态
            "profit_history": profit_history,            # 近5日获利盘变化
            "chip_dist": {"prices": price_bins.tolist(), "chips": chips_pct.tolist()},
        }

    def _calc_chip_distribution(self, df: pd.DataFrame) -> dict:
        """
        核心算法: 计算筹码分布

        原理:
        - 每天的成交量代表当天换手的筹码
        - 新筹码按三角分布分配到[low, high]区间
        - 旧筹码按换手率衰减: old_chips *= (1 - turnover * decay)
        - 累积所有天的筹码得到最终分布
        """
        n_bins = self.cfg["price_bins"]
        decay = self.cfg["decay_factor"]

        # 确定价格范围（留10%余量）
        price_min = df["low"].min() * 0.95
        price_max = df["high"].max() * 1.05
        price_bins = np.linspace(price_min, price_max, n_bins)
        bin_width = price_bins[1] - price_bins[0]

        # 初始化筹码数组
        chips = np.zeros(n_bins)

        # 计算平均换手率（用于衰减）
        volumes = df["volume"].fillna(0).values
        avg_volume = volumes.mean() if len(volumes) > 0 else 1
        if avg_volume <= 0 or np.isnan(avg_volume):
            avg_volume = 1

        for i in range(len(df)):
            row = df.iloc[i]
            low, high = row["low"], row["high"]
            volume = row["volume"]
            close = row["close"]

            # 跳过无效数据
            if np.isnan(volume) or volume <= 0 or np.isnan(close) or np.isnan(low) or np.isnan(high):
                continue

            # 当日换手率估算
            turnover = volume / avg_volume if avg_volume > 0 else 0.05
            turnover = min(turnover, 0.3)  # 上限30%

            # 旧筹码衰减
            decay_rate = 1 - turnover * decay * 0.5
            chips *= max(decay_rate, 0.7)  # 最多衰减30%

            # 新筹码分配（三角分布，峰值在收盘价）
            if high > low:
                low_idx = max(0, int((low - price_min) / bin_width))
                high_idx = min(n_bins - 1, int((high - price_min) / bin_width))
                close_idx = max(low_idx, min(high_idx, int((close - price_min) / bin_width)))

                if high_idx > low_idx:
                    for j in range(low_idx, high_idx + 1):
                        if j <= close_idx:
                            weight = (j - low_idx + 1) / (close_idx - low_idx + 1)
                        else:
                            weight = (high_idx - j + 1) / (high_idx - close_idx + 1)
                        chips[j] += volume * weight * 0.001
            else:
                # 涨停/跌停一字板
                idx = max(0, min(n_bins - 1, int((close - price_min) / bin_width)))
                chips[idx] += volume * 0.001

        return {"price_bins": price_bins, "chips": chips}

    def _calc_profit_ratio(self, price_bins: np.ndarray, chips_pct: np.ndarray,
                           current_price: float) -> Tuple[float, float]:
        """计算获利盘/套牢盘比例"""
        profit_mask = price_bins <= current_price
        profit_ratio = chips_pct[profit_mask].sum()
        trapped_ratio = 1 - profit_ratio
        return profit_ratio, trapped_ratio

    def _calc_cost_center(self, price_bins: np.ndarray, chips_pct: np.ndarray) -> float:
        """计算成本重心（加权平均价格）"""
        return float(np.average(price_bins, weights=chips_pct))

    def _calc_concentration(self, price_bins: np.ndarray,
                            chips_pct: np.ndarray) -> Tuple[float, float, float]:
        """
        计算筹码集中度
        返回: (90%筹码下界, 90%筹码上界, 集中度比率)
        集中度 = (上界-下界) / 中位价，越小越集中
        """
        cumsum = np.cumsum(chips_pct)
        pct = self.cfg["concentration_pct"]
        lower_pct = (1 - pct) / 2
        upper_pct = 1 - lower_pct

        low_idx = np.searchsorted(cumsum, lower_pct)
        high_idx = np.searchsorted(cumsum, upper_pct)

        low_idx = min(low_idx, len(price_bins) - 1)
        high_idx = min(high_idx, len(price_bins) - 1)

        conc_low = price_bins[low_idx]
        conc_high = price_bins[high_idx]
        median_price = price_bins[np.searchsorted(cumsum, 0.5)]

        concentration = (conc_high - conc_low) / median_price if median_price > 0 else 1.0
        return float(conc_low), float(conc_high), float(concentration)

    def _find_peaks(self, price_bins: np.ndarray, chips_pct: np.ndarray) -> List[dict]:
        """查找筹码密集峰"""
        threshold = self.cfg["peak_threshold"]
        peaks = []

        # 平滑处理
        if len(chips_pct) > 5:
            kernel = np.ones(3) / 3
            smoothed = np.convolve(chips_pct, kernel, mode='same')
        else:
            smoothed = chips_pct

        # 找局部极大值
        for i in range(1, len(smoothed) - 1):
            if (smoothed[i] > smoothed[i-1] and
                smoothed[i] > smoothed[i+1] and
                smoothed[i] > threshold):
                peaks.append({
                    "price": round(float(price_bins[i]), 3),
                    "ratio": round(float(smoothed[i]), 4),
                })

        # 按占比排序，取前3个
        peaks.sort(key=lambda x: x["ratio"], reverse=True)
        return peaks[:3]

    def _find_support_resistance(self, price_bins: np.ndarray, chips_pct: np.ndarray,
                                  current_price: float, peaks: List[dict]) -> Tuple[float, float]:
        """
        基于筹码分布找支撑/压力位
        支撑: 当前价下方最近的筹码密集峰
        压力: 当前价上方最近的筹码密集峰
        """
        support = current_price * 0.95
        resistance = current_price * 1.05

        below_peaks = [p for p in peaks if p["price"] < current_price]
        above_peaks = [p for p in peaks if p["price"] > current_price]

        if below_peaks:
            support = max(below_peaks, key=lambda x: x["price"])["price"]
        if above_peaks:
            resistance = min(above_peaks, key=lambda x: x["price"])["price"]

        return support, resistance

    def _assess_control(self, profit_ratio: float, concentration: float,
                        cost_center: float, current_price: float) -> dict:
        """评估主力控盘度"""
        control_score = 0
        reasons = []

        if profit_ratio > self.cfg["control_threshold"]:
            control_score += 40
            reasons.append(f"获利盘{profit_ratio*100:.0f}%高(锁仓)")
        elif profit_ratio > 0.6:
            control_score += 20
            reasons.append(f"获利盘{profit_ratio*100:.0f}%中高")

        if concentration < 0.10:
            control_score += 35
            reasons.append(f"极度集中({concentration*100:.1f}%)")
        elif concentration < 0.15:
            control_score += 25
            reasons.append(f"高度集中({concentration*100:.1f}%)")
        elif concentration < 0.20:
            control_score += 15
            reasons.append(f"中度集中({concentration*100:.1f}%)")

        cost_deviation = abs(current_price - cost_center) / current_price
        if cost_deviation < 0.05:
            control_score += 25
            reasons.append("成本重心贴近现价")
        elif cost_deviation < 0.10:
            control_score += 15
            reasons.append("成本重心较近")

        if control_score >= 70:
            level = "高度控盘"
        elif control_score >= 45:
            level = "中度控盘"
        elif control_score >= 25:
            level = "轻度控盘"
        else:
            level = "散户主导"

        return {"level": level, "score": control_score, "reasons": reasons}

    def _identify_pattern(self, profit_ratio: float, concentration: float,
                          peaks: List[dict], current_price: float,
                          cost_center: float) -> dict:
        """筹码形态识别"""
        n_peaks = len([p for p in peaks if p["ratio"] > self.cfg["peak_threshold"]])

        if n_peaks <= 1 and concentration < 0.15:
            if profit_ratio > 0.7:
                pattern = "低位单峰密集"
                signal = "buy"
                desc = "筹码高度集中+大部分获利，主力控盘待拉升"
            elif profit_ratio < 0.3:
                pattern = "高位单峰密集"
                signal = "sell"
                desc = "筹码集中在高位+大部分套牢，顶部派发风险"
            else:
                pattern = "中位单峰密集"
                signal = "watch"
                desc = "筹码集中，方向待选择"
        elif n_peaks >= 2:
            pattern = "双峰/多峰形态"
            signal = "hold"
            desc = "上下均有筹码堆积，震荡整理中"
        elif concentration > 0.25:
            pattern = "筹码发散"
            signal = "avoid"
            desc = "筹码分散无主力，不宜介入"
        else:
            pattern = "正常分布"
            signal = "neutral"
            desc = "筹码分布正常，无明显信号"

        if profit_ratio < self.cfg["panic_threshold"]:
            pattern = "恐慌割肉区"
            signal = "watch_bottom"
            desc = f"获利盘仅{profit_ratio*100:.0f}%，极度恐慌，关注底部信号"

        return {"name": pattern, "signal": signal, "desc": desc}

    def _calc_profit_history(self, df: pd.DataFrame, lookback: int = 5) -> List[dict]:
        """计算最近N天的获利盘变化趋势"""
        history = []
        n = len(df)
        if n < lookback + 1:
            return history

        for offset in range(lookback, 0, -1):
            idx = n - offset
            sub_df = df.iloc[:idx+1].copy()
            price = sub_df.iloc[-1]["close"]

            calc_df = sub_df.tail(60).reset_index(drop=True)
            chip_dist = self._calc_chip_distribution(calc_df)
            price_bins = chip_dist["price_bins"]
            chips = chip_dist["chips"]
            total = chips.sum()
            if total > 0:
                chips_pct = chips / total
                profit_mask = price_bins <= price
                ratio = float(chips_pct[profit_mask].sum())
            else:
                ratio = 0.5

            date = sub_df.iloc[-1].get("date", str(idx))
            history.append({"date": date, "profit_ratio": round(ratio, 4)})

        return history


# === 便捷函数 ===
def quick_chip_analyze(df: pd.DataFrame, current_price: float = None) -> dict:
    """快速筹码分析"""
    return ChipAnalyzer().analyze(df, current_price)


def chip_summary(result: dict) -> str:
    """筹码分析摘要文本"""
    if "error" in result:
        return f"❌ {result['error']}"

    lines = [
        f"📊 筹码分布分析 | 现价{result['current_price']:.3f}",
        f"  获利盘: {result['profit_ratio']*100:.1f}% | 套牢盘: {result['trapped_ratio']*100:.1f}%",
        f"  成本重心: {result['cost_center']:.3f} | 集中度: {result['concentration']*100:.1f}%",
        f"  90%筹码区间: [{result['conc_low']:.3f}, {result['conc_high']:.3f}]",
        f"  筹码支撑: {result['support']:.3f} | 筹码压力: {result['resistance']:.3f}",
        f"  控盘度: {result['control_level']['level']}({result['control_level']['score']}分)",
        f"  形态: {result['pattern']['name']} → {result['pattern']['desc']}",
    ]

    if result["peaks"]:
        peaks_str = " | ".join([f"{p['price']:.2f}({p['ratio']*100:.1f}%)" for p in result["peaks"]])
        lines.append(f"  密集峰: {peaks_str}")

    return "\n".join(lines)
