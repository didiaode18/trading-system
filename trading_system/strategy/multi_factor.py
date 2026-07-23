# -*- coding: utf-8 -*-
"""
多因子选股评分系统 (Multi-Factor Stock Scoring)
================================================
对标同花顺i问财/东方财富智能选股

四维评分体系:
  技术面(30%): 趋势级别 + DK信号 + 乖离率 + 筹码集中度
  资金面(30%): 北向加仓 + 主力连流 + 板块资金 + 资金评分
  动量面(20%): 5日/20日涨幅 + 相对强度 + 板块动量
  基本面(20%): ROE + 营收增速 + PEG (akshare可选)

输出:
  - 综合评分 0-100
  - 各维度子分
  - 排名推荐

使用:
    from strategy.multi_factor import MultiFactorScorer
    scorer = MultiFactorScorer()
    scores = scorer.score_all(results)
"""

import pandas as pd
import numpy as np
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

FACTOR_CONFIG = {
    "weight_technical": 0.30,   # 技术面权重
    "weight_fund": 0.30,        # 资金面权重
    "weight_momentum": 0.20,    # 动量面权重
    "weight_fundamental": 0.20, # 基本面权重
    "top_n": 10,                # 推荐TOP N
    "min_score_buy": 70,        # 买入最低分
    "min_score_watch": 55,      # 关注最低分
}


class MultiFactorScorer:
    """多因子评分器"""

    def __init__(self, config: dict = None):
        self.cfg = {**FACTOR_CONFIG, **(config or {})}

    def score_all(self, results: List[dict]) -> List[dict]:
        """
        对所有标的进行多因子评分

        参数:
            results: CaopanEngine.analyze()的结果列表

        返回:
            按综合分排序的结果列表（含各维度分数）
        """
        scored = []
        for r in results:
            score_detail = self._score_single(r)
            score_detail["code"] = r.get("code", "")
            score_detail["name"] = r.get("name", "")
            score_detail["close"] = r.get("close", 0)
            scored.append(score_detail)

        # 按综合分排序
        scored.sort(key=lambda x: x["total_score"], reverse=True)
        for i, s in enumerate(scored):
            s["rank"] = i + 1
            s["recommendation"] = self._get_recommendation(s["total_score"])

        return scored

    def _score_single(self, r: dict) -> dict:
        """单只标的四维评分"""
        tech = self._score_technical(r)
        fund = self._score_fund(r)
        momentum = self._score_momentum(r)
        fundamental = self._score_fundamental(r)

        total = (
            tech * self.cfg["weight_technical"] +
            fund * self.cfg["weight_fund"] +
            momentum * self.cfg["weight_momentum"] +
            fundamental * self.cfg["weight_fundamental"]
        )

        return {
            "total_score": round(total, 1),
            "technical": round(tech, 1),
            "fund": round(fund, 1),
            "momentum": round(momentum, 1),
            "fundamental": round(fundamental, 1),
        }

    def _score_technical(self, r: dict) -> float:
        """
        技术面评分 (0-100)
        - 趋势级别: 5级=100, 4级=75, 3级=50, 2级=25, 1级=0
        - DK信号: D点+高分=加分, K点=减分
        - 乖离率: 正常区间加分, 超买/超卖减分
        - 筹码集中度: 集中=加分
        """
        score = 50.0

        # 趋势级别 (权重最大)
        trend = r.get("trend_level", 3)
        trend_score = {5: 100, 4: 75, 3: 50, 2: 25, 1: 0}.get(trend, 50)
        score = score * 0.4 + trend_score * 0.6  # 趋势占技术面60%

        # DK信号
        dk = r.get("dk_signal")
        dk_strength = r.get("dk_strength", 0)
        dk_filtered = r.get("dk_filtered", False)
        if dk == "D" and not dk_filtered:
            score += dk_strength * 0.2  # 最多加20分
        elif dk == "K" and not dk_filtered:
            score -= dk_strength * 0.15

        # 乖离率
        deviation = r.get("deviation_pct", 0)
        if -3 <= deviation <= 5:
            score += 5  # 正常区间
        elif deviation > 10:
            score -= 10  # 超买
        elif deviation < -8:
            score -= 5   # 超卖（可能是机会但也可能是趋势下跌）

        # 筹码集中度
        chip = r.get("chip")
        if chip:
            conc = chip.get("concentration", 0.5)
            if conc < 0.10:
                score += 10  # 极度集中
            elif conc < 0.15:
                score += 5
            elif conc > 0.30:
                score -= 10  # 极度分散

            # 控盘度
            ctrl_score = chip.get("control_level", {}).get("score", 0)
            score += ctrl_score * 0.1  # 最多加10分

        return max(0, min(100, score))

    def _score_fund(self, r: dict) -> float:
        """
        资金面评分 (0-100)
        - 北向资金: 连续加仓=加分
        - 主力连流: 连续流入天数
        - 资金模式: 温和建仓/放量拉升=加分, 对倒骗线=减分
        - 资金综合评分
        """
        score = 50.0

        # 主力资金连续流入
        streak = r.get("main_flow_streak", 0)
        score += min(20, streak * 5)  # 每天+5，最多+20

        # 主力连续流出
        out_streak = r.get("main_outflow_streak", 0)
        score -= min(15, out_streak * 5)

        # 资金模式
        pattern = r.get("fund_pattern", "normal")
        pattern_scores = {"mild_build": 15, "surge": 10, "fake": -20, "normal": 0}
        score += pattern_scores.get(pattern, 0)

        # 顶背离
        if r.get("top_divergence"):
            score -= 15

        # 底背离
        if r.get("bottom_divergence"):
            score += 10

        # 真实资金数据评分
        fund_data = r.get("fund_data", {})
        fd_score = fund_data.get("score", 50)
        # 融合真实数据评分 (权重40%)
        score = score * 0.6 + fd_score * 0.4

        return max(0, min(100, score))

    def _score_momentum(self, r: dict) -> float:
        """
        动量面评分 (0-100)
        - 5日涨幅
        - 20日涨幅
        - 相对强度 (vs 大盘)
        - 量能配合
        """
        score = 50.0
        df = r.get("df_analyzed")

        if df is not None and len(df) >= 20:
            close = df["close"].values

            # 5日涨幅
            r5 = (close[-1] - close[-5]) / close[-5] if close[-5] > 0 else 0
            score += max(-15, min(15, r5 * 200))

            # 20日涨幅
            r20 = (close[-1] - close[-20]) / close[-20] if close[-20] > 0 else 0
            score += max(-10, min(10, r20 * 100))

            # 量能: 5日均量 vs 20日均量
            if "volume" in df.columns:
                vol = df["volume"].values
                vol_5 = vol[-5:].mean()
                vol_20 = vol[-20:].mean()
                if vol_20 > 0:
                    vol_ratio = vol_5 / vol_20
                    if vol_ratio > 1.3 and r5 > 0:
                        score += 10  # 放量上涨
                    elif vol_ratio < 0.7 and r5 > 0:
                        score -= 5   # 缩量上涨（动能不足）
                    elif vol_ratio > 1.5 and r5 < 0:
                        score -= 10  # 放量下跌（恐慌）

        return max(0, min(100, score))

    def _score_fundamental(self, r: dict) -> float:
        """
        基本面评分 (0-100)
        当前: 基于可用数据的简化评估
        未来: 接入akshare财务数据 (ROE/营收增速/PEG)
        """
        score = 50.0  # 默认中性

        # 尝试获取基本面数据
        code = r.get("code", "")
        try:
            import akshare as ak
            # 个股基本面指标
            df_fin = ak.stock_financial_abstract_ths(symbol=code)
            if df_fin is not None and not df_fin.empty:
                # ROE
                roe = df_fin.get("净资产收益率", pd.Series([0])).iloc[-1]
                if roe > 15:
                    score += 20
                elif roe > 10:
                    score += 10
                elif roe < 5:
                    score -= 10
        except Exception:
            pass  # 基本面数据不可用时保持中性

        return max(0, min(100, score))

    def _get_recommendation(self, score: float) -> str:
        """根据分数给出推荐"""
        if score >= self.cfg["min_score_buy"]:
            return "强烈推荐"
        elif score >= self.cfg["min_score_watch"]:
            return "关注"
        elif score >= 40:
            return "观望"
        else:
            return "回避"


def factor_summary(scored: List[dict]) -> str:
    """多因子评分摘要"""
    lines = ["📊 多因子选股评分"]
    lines.append(f"  {'排名':<4} {'标的':<10} {'综合':<6} {'技术':<6} {'资金':<6} {'动量':<6} {'基本面':<6} {'推荐'}")
    lines.append("  " + "─" * 65)

    for s in scored:
        rec_icon = {"强烈推荐": "🔴", "关注": "🟠", "观望": "🟡", "回避": "⚪"}.get(s["recommendation"], "")
        lines.append(
            f"  {s['rank']:<4} {s['name']:<10} {s['total_score']:>5.1f} "
            f"{s['technical']:>5.1f} {s['fund']:>5.1f} {s['momentum']:>5.1f} "
            f"{s['fundamental']:>5.1f} {rec_icon}{s['recommendation']}"
        )

    # 推荐池
    recommended = [s for s in scored if s["recommendation"] in ("强烈推荐", "关注")]
    if recommended:
        names = "、".join([s["name"] for s in recommended])
        lines.append(f"\n  💡 推荐池: {names}")

    return "\n".join(lines)
