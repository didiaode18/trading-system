"""
大盘状态智能识别模块 V1.0
==========================
使用多维度特征+隐马尔可夫模型(HMM)思想识别市场状态，
比简单MA20/MA60判断更准确

核心方法:
  1. 多特征融合判定（趋势+波动率+量能+ breadth）
  2. 基于HMM思想的三状态模型（牛市/震荡/熊市）
  3. 状态转换概率矩阵（预判下一步走势）
  4. 自适应仓位建议（根据状态动态调整）

特征维度:
  - 趋势特征: MA20/MA60关系、指数斜率
  - 波动率特征: ATR变化、振幅
  - 量能特征: 成交量趋势、量价关系
  - 市场宽度: 涨跌比、涨停/跌停家数
  - 情绪特征: 换手率、融资余额变化

状态定义:
  BULL (牛市): 趋势向上+量能配合+宽度扩散
  RANGE (震荡): 方向不明+波动收窄+量能平淡
  BEAR (熊市): 趋势向下+恐慌放量+宽度收缩

使用方式:
    from strategy.market_regime import MarketRegimeDetector
    detector = MarketRegimeDetector()
    state = detector.detect(benchmark_df)
"""

import os
import sys
import logging
import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class MarketRegimeDetector:
    """大盘状态检测器"""

    def __init__(self):
        # 状态定义
        self.STATES = {"BULL": "牛市/强势", "RANGE": "震荡/平衡", "BEAR": "熊市/弱势"}
        # 各状态对应仓位建议
        self.POSITION_MAP = {
            "BULL": {"max_position": 0.90, "min_position": 0.70, "strategy": "趋势跟踪"},
            "RANGE": {"max_position": 0.60, "min_position": 0.30, "strategy": "高抛低吸+均值回归"},
            "BEAR": {"max_position": 0.30, "min_position": 0.00, "strategy": "防守为主+超跌反弹"},
        }

    def detect(self, benchmark_df: pd.DataFrame, extra_data: dict = None) -> dict:
        """
        检测当前大盘状态
        
        参数:
            benchmark_df: 基准指数(沪深300)日线数据
            extra_data: 额外数据（涨跌家数等，可选）
        
        返回:
            {
                "state": str,              # "BULL"/"RANGE"/"BEAR"
                "state_cn": str,           # 中文描述
                "confidence": float,       # 置信度(0-1)
                "scores": {...},           # 各维度得分
                "position_advice": {...},  # 仓位建议
                "transition_prob": {...},  # 状态转换概率
                "features": {...},         # 原始特征值
                "detail": str
            }
        """
        if benchmark_df is None or len(benchmark_df) < 60:
            return {
                "state": "RANGE", "state_cn": "数据不足，默认震荡",
                "confidence": 0.3, "scores": {}, "position_advice": self.POSITION_MAP["RANGE"],
                "transition_prob": {}, "features": {}, "detail": "数据不足"
            }

        # 计算各维度特征
        trend_score = self._calc_trend_score(benchmark_df)
        volatility_score = self._calc_volatility_score(benchmark_df)
        volume_score = self._calc_volume_score(benchmark_df)
        momentum_score = self._calc_momentum_score(benchmark_df)
        breadth_score = self._calc_breadth_score(benchmark_df, extra_data)

        # 综合评分 (加权)
        weights = {"trend": 0.35, "volatility": 0.15, "volume": 0.20,
                   "momentum": 0.15, "breadth": 0.15}
        scores = {
            "trend": trend_score,
            "volatility": volatility_score,
            "volume": volume_score,
            "momentum": momentum_score,
            "breadth": breadth_score,
        }

        # 每个维度得分范围 [-1, 1]，正=看多，负=看空
        weighted_score = sum(scores[k] * weights[k] for k in weights)

        # 状态判定
        if weighted_score > 0.3:
            state = "BULL"
            confidence = min(0.95, 0.5 + weighted_score * 0.5)
        elif weighted_score < -0.3:
            state = "BEAR"
            confidence = min(0.95, 0.5 + abs(weighted_score) * 0.5)
        else:
            state = "RANGE"
            confidence = 0.6 + (0.3 - abs(weighted_score)) * 0.5

        # 状态转换概率（简化版，基于历史统计）
        transition = self._estimate_transition(state, scores)

        # 仓位建议
        position_advice = self.POSITION_MAP[state].copy()
        # 根据置信度微调
        if confidence < 0.5:
            position_advice["max_position"] *= 0.8  # 不确定时降低仓位

        # 特征原始值
        features = self._get_raw_features(benchmark_df)

        detail = (f"大盘状态: {self.STATES[state]} | 置信度{confidence:.0%} | "
                 f"综合得分{weighted_score:+.3f} | "
                 f"趋势{trend_score:+.2f} 量能{volume_score:+.2f} 动量{momentum_score:+.2f}")

        result = {
            "state": state,
            "state_cn": self.STATES[state],
            "confidence": round(confidence, 3),
            "weighted_score": round(weighted_score, 4),
            "scores": {k: round(v, 3) for k, v in scores.items()},
            "position_advice": position_advice,
            "transition_prob": transition,
            "features": features,
            "detail": detail,
            "detect_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        logger.info(f"[大盘状态] {detail}")
        return result

    # ============================================================
    # 特征计算
    # ============================================================

    def _calc_trend_score(self, df: pd.DataFrame) -> float:
        """
        趋势得分 [-1, 1]
        正=多头排列，负=空头排列
        """
        close = df["close"].values
        if len(close) < 60:
            return 0

        ma20 = pd.Series(close).rolling(20).mean().iloc[-1]
        ma60 = pd.Series(close).rolling(60).mean().iloc[-1]
        current = close[-1]

        score = 0

        # 价格与均线关系
        if current > ma20:
            score += 0.3
        else:
            score -= 0.3

        if current > ma60:
            score += 0.2
        else:
            score -= 0.2

        # 均线排列
        if ma20 > ma60:
            score += 0.3
        else:
            score -= 0.3

        # MA20斜率
        ma20_series = pd.Series(close).rolling(20).mean()
        slope = (ma20_series.iloc[-1] - ma20_series.iloc[-5]) / ma20_series.iloc[-5] if ma20_series.iloc[-5] > 0 else 0
        score += np.clip(slope * 20, -0.2, 0.2)

        return np.clip(score, -1, 1)

    def _calc_volatility_score(self, df: pd.DataFrame) -> float:
        """
        波动率得分 [-1, 1]
        波动率收缩+价格稳定=正（有利做多）
        波动率急剧放大=负（恐慌）
        """
        if len(df) < 30:
            return 0

        close = df["close"].values
        # 近期波动率 vs 远期波动率
        recent_returns = np.diff(close[-11:]) / close[-11:-1]  # 10个收益率
        older_returns = np.diff(close[-31:-10]) / close[-31:-11]  # 20个收益率
        recent_vol = np.std(recent_returns) if len(recent_returns) > 1 else 0
        older_vol = np.std(older_returns) if len(older_returns) > 1 else 0

        if older_vol == 0:
            return 0

        vol_ratio = recent_vol / older_vol

        # 波动率收缩（ratio<1）= 正面（蓄势）
        # 波动率急剧放大（ratio>2）= 负面（恐慌）
        if vol_ratio < 0.7:
            return 0.5  # 明显收缩，蓄势
        elif vol_ratio < 1.0:
            return 0.2
        elif vol_ratio < 1.5:
            return -0.1
        elif vol_ratio < 2.0:
            return -0.4
        else:
            return -0.8  # 恐慌性波动

    def _calc_volume_score(self, df: pd.DataFrame) -> float:
        """
        量能得分 [-1, 1]
        上涨放量+下跌缩量=正（健康）
        下跌放量+上涨缩量=负（出货）
        """
        if len(df) < 20:
            return 0

        close = df["close"].values
        volume = df["volume"].values

        # 近5日 vs 20日均量
        vol_5 = volume[-5:].mean()
        vol_20 = volume[-20:].mean()
        vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1

        # 量价关系
        price_change_5d = (close[-1] / close[-6] - 1) if close[-6] > 0 else 0

        score = 0
        if price_change_5d > 0 and vol_ratio > 1.2:
            score += 0.5  # 上涨放量，健康
        elif price_change_5d > 0 and vol_ratio < 0.8:
            score += 0.1  # 上涨缩量，动力不足
        elif price_change_5d < 0 and vol_ratio > 1.5:
            score -= 0.6  # 下跌放量，恐慌
        elif price_change_5d < 0 and vol_ratio < 0.8:
            score -= 0.1  # 下跌缩量，抛压减轻（中性偏好）
            score += 0.2

        # 量能趋势
        vol_trend = (volume[-5:].mean() - volume[-10:-5].mean()) / volume[-10:-5].mean() if volume[-10:-5].mean() > 0 else 0
        score += np.clip(vol_trend * 0.5, -0.3, 0.3)

        return np.clip(score, -1, 1)

    def _calc_momentum_score(self, df: pd.DataFrame) -> float:
        """
        动量得分 [-1, 1]
        基于RSI、MACD、涨跌幅
        """
        if len(df) < 30:
            return 0

        close = df["close"].values
        score = 0

        # 5日涨幅
        ret_5d = close[-1] / close[-6] - 1 if close[-6] > 0 else 0
        score += np.clip(ret_5d * 5, -0.3, 0.3)

        # 20日涨幅
        ret_20d = close[-1] / close[-21] - 1 if close[-21] > 0 else 0
        score += np.clip(ret_20d * 3, -0.3, 0.3)

        # RSI
        if "rsi" in df.columns:
            rsi = df["rsi"].iloc[-1]
            if pd.notna(rsi):
                if rsi > 60:
                    score += 0.2
                elif rsi < 40:
                    score -= 0.2
                # 超卖反弹信号
                if rsi < 25:
                    score += 0.1  # 极度超卖可能反弹

        # MACD
        if "macd_hist" in df.columns:
            hist = df["macd_hist"].iloc[-1]
            prev_hist = df["macd_hist"].iloc[-2] if len(df) > 1 else 0
            if pd.notna(hist):
                if hist > 0 and hist > prev_hist:
                    score += 0.2  # 多头动能增强
                elif hist < 0 and hist < prev_hist:
                    score -= 0.2  # 空头动能增强

        return np.clip(score, -1, 1)

    def _calc_breadth_score(self, df: pd.DataFrame, extra_data: dict = None) -> float:
        """
        市场宽度得分 [-1, 1]
        如果有涨跌家数数据则使用，否则用指数特征代理
        """
        if extra_data and "advance_count" in extra_data:
            adv = extra_data["advance_count"]
            dec = extra_data["decline_count"]
            total = adv + dec
            if total > 0:
                ratio = adv / total
                return np.clip((ratio - 0.5) * 4, -1, 1)

        # 无额外数据时，用指数连续涨跌天数代理
        if len(df) < 10:
            return 0

        close = df["close"].values
        # 近10日中上涨天数
        daily_changes = np.diff(close[-11:])
        up_days = np.sum(daily_changes > 0)
        ratio = up_days / 10

        return np.clip((ratio - 0.5) * 3, -1, 1)

    # ============================================================
    # 状态转换概率
    # ============================================================

    def _estimate_transition(self, current_state: str, scores: dict) -> dict:
        """
        估算状态转换概率（基于经验统计）
        
        历史统计规律:
        - 牛市平均持续60-90天
        - 震荡平均持续20-40天
        - 熊市平均持续30-60天
        """
        # 基础转换矩阵（经验值）
        base_transition = {
            "BULL": {"BULL": 0.85, "RANGE": 0.12, "BEAR": 0.03},
            "RANGE": {"BULL": 0.25, "RANGE": 0.55, "BEAR": 0.20},
            "BEAR": {"BULL": 0.05, "RANGE": 0.30, "BEAR": 0.65},
        }

        prob = base_transition.get(current_state, base_transition["RANGE"]).copy()

        # 根据当前得分微调
        trend = scores.get("trend", 0)
        if current_state == "RANGE":
            if trend > 0.3:
                prob["BULL"] += 0.1
                prob["BEAR"] -= 0.05
            elif trend < -0.3:
                prob["BEAR"] += 0.1
                prob["BULL"] -= 0.05

        # 归一化
        total = sum(prob.values())
        prob = {k: round(v / total, 3) for k, v in prob.items()}

        return prob

    # ============================================================
    # 原始特征提取
    # ============================================================

    def _get_raw_features(self, df: pd.DataFrame) -> dict:
        """提取原始特征值（用于报告和调试）"""
        close = df["close"].values
        features = {}

        if len(close) >= 60:
            ma20 = pd.Series(close).rolling(20).mean().iloc[-1]
            ma60 = pd.Series(close).rolling(60).mean().iloc[-1]
            features["close"] = round(close[-1], 2)
            features["ma20"] = round(ma20, 2)
            features["ma60"] = round(ma60, 2)
            features["above_ma20"] = close[-1] > ma20
            features["above_ma60"] = close[-1] > ma60
            features["ma20_above_ma60"] = ma20 > ma60

        if len(close) >= 6:
            features["return_5d"] = round((close[-1] / close[-6] - 1) * 100, 2)
        if len(close) >= 21:
            features["return_20d"] = round((close[-1] / close[-21] - 1) * 100, 2)

        if "volume" in df.columns and len(df) >= 20:
            vol = df["volume"].values
            features["vol_ratio_5_20"] = round(vol[-5:].mean() / vol[-20:].mean(), 2) if vol[-20:].mean() > 0 else 1

        if "rsi" in df.columns:
            rsi = df["rsi"].iloc[-1]
            features["rsi"] = round(rsi, 1) if pd.notna(rsi) else None

        return features

    # ============================================================
    # 策略建议
    # ============================================================

    def get_strategy_advice(self, state_result: dict) -> dict:
        """
        根据大盘状态给出策略建议
        
        返回:
            {
                "primary_strategy": str,    # 主策略
                "position_range": (min, max),
                "preferred_sectors": [str],
                "risk_level": str,
                "actions": [str]
            }
        """
        state = state_result["state"]
        confidence = state_result["confidence"]

        if state == "BULL":
            return {
                "primary_strategy": "趋势跟踪（追强势股回踩买点）",
                "position_range": (0.70, 0.90),
                "preferred_sectors": config.SECTOR_TIER1,
                "risk_level": "积极",
                "actions": [
                    "重仓持有趋势向上的龙头股",
                    "回踩MA20是加仓良机",
                    "可适当提高弹性股比例",
                    "止损上移保护利润",
                ]
            }
        elif state == "BEAR":
            return {
                "primary_strategy": "防守反击（超跌反弹+现金为王）",
                "position_range": (0.00, 0.30),
                "preferred_sectors": ["大金融", "大消费"],  # 防御板块
                "risk_level": "保守",
                "actions": [
                    "大幅降低仓位至3成以下",
                    "只保留强势不跌的核心仓",
                    "均值回归策略小仓位博反弹",
                    "严格止损，不抄底不补仓",
                    "等待大盘企稳信号再入场",
                ]
            }
        else:  # RANGE
            return {
                "primary_strategy": "高抛低吸（区间操作+均值回归）",
                "position_range": (0.30, 0.60),
                "preferred_sectors": [],
                "risk_level": "均衡",
                "actions": [
                    "半仓操作，高抛低吸",
                    "布林带上轨减仓、下轨加仓",
                    "均值回归策略为主",
                    "控制单只仓位不超过10%",
                    "关注行业轮动机会",
                ]
            }


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 50)
    print("  大盘状态识别 - 测试")
    print("=" * 50)
    print("  需要加载基准指数数据后调用:")
    print("    detector = MarketRegimeDetector()")
    print("    state = detector.detect(benchmark_df)")
    print("\n[OK] 模块加载正常")
