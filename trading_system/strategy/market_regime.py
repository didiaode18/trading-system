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
import json
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
# V3.2: HMM市场状态识别 + 策略自动切换
# ============================================================

class HMMRegimeDetector:
    """HMM市场状态识别器（升级版）
    
    使用sklearn GaussianHMM对多维特征进行无监督聚类，
    识别牛市/震荡/熊市三态，并自动推荐策略组合。
    
    与MarketRegimeDetector的区别:
    - MarketRegimeDetector: 规则式（固定阈值），简单可靠
    - HMMRegimeDetector: 统计式（自适应阈值），更精确但需足够数据
    
    建议使用: 两者融合，HMM作为确认信号
    """

    # 策略映射: 市场状态 → 推荐策略组合
    STRATEGY_MAP = {
        "BULL": {
            "primary": "CANSLIM_V3.2",
            "secondary": ["板块轮动", "操盘密码DK"],
            "weights": {"CANSLIM_V3.2": 0.5, "板块轮动": 0.3, "操盘密码DK": 0.2},
            "position_range": (0.60, 0.90),
        },
        "RANGE": {
            "primary": "均值回归",
            "secondary": ["操盘密码DK", "事件驱动"],
            "weights": {"均值回归": 0.4, "操盘密码DK": 0.3, "事件驱动": 0.3},
            "position_range": (0.30, 0.60),
        },
        "BEAR": {
            "primary": "均值回归",
            "secondary": ["事件驱动"],
            "weights": {"均值回归": 0.6, "事件驱动": 0.4},
            "position_range": (0.00, 0.30),
        },
    }

    def __init__(self, n_states: int = 3, lookback: int = 120):
        self.n_states = n_states
        self.lookback = lookback
        self.model = None
        self.state_labels = {}  # HMM state -> "BULL"/"RANGE"/"BEAR"

    def fit_and_detect(self, benchmark_df: pd.DataFrame) -> dict:
        """
        训练HMM并检测当前状态
        
        参数:
            benchmark_df: 基准指数日线数据(至少120天)
        
        返回:
            {"state": str, "confidence": float, "strategy_config": dict, ...}
        """
        if benchmark_df is None or len(benchmark_df) < self.lookback:
            # 数据不足，回退到规则式检测
            fallback = MarketRegimeDetector()
            result = fallback.detect(benchmark_df)
            result["method"] = "rule_fallback"
            result["strategy_config"] = self.STRATEGY_MAP.get(result["state"], {})
            return result

        try:
            from sklearn.mixture import GaussianMixture

            # 构建特征矩阵
            features = self._build_features(benchmark_df)
            if features is None or len(features) < 60:
                raise ValueError("特征不足")

            # 用GMM代替HMM（更稳定，小样本表现更好）
            gmm = GaussianMixture(
                n_components=self.n_states,
                covariance_type='full',
                random_state=42,
                n_init=3,
            )
            gmm.fit(features)

            # 预测当前状态
            current_features = features[-1:].copy()
            state_idx = gmm.predict(current_features)[0]
            probs = gmm.predict_proba(current_features)[0]
            confidence = float(probs[state_idx])

            # 将GMM state映射到BULL/RANGE/BEAR
            self._map_states(gmm, features, benchmark_df)
            state_label = self.state_labels.get(state_idx, "RANGE")

            # 获取策略配置
            strategy_config = self.STRATEGY_MAP.get(state_label, self.STRATEGY_MAP["RANGE"])

            result = {
                "state": state_label,
                "state_cn": {"BULL": "牛市/强势", "RANGE": "震荡/平衡", "BEAR": "熊市/弱势"}[state_label],
                "confidence": round(confidence, 3),
                "method": "gmm",
                "state_probs": {
                    self.state_labels.get(i, f"state_{i}"): round(float(p), 3)
                    for i, p in enumerate(probs)
                },
                "strategy_config": strategy_config,
                "position_range": strategy_config["position_range"],
                "recommended_strategies": strategy_config["weights"],
                "detect_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            }

            logger.info(f"[HMM状态] {state_label}(置信{confidence:.0%}) "
                       f"推荐策略: {strategy_config['primary']}")
            return result

        except ImportError:
            logger.warning("[HMM状态] sklearn未安装，回退规则式")
            fallback = MarketRegimeDetector()
            result = fallback.detect(benchmark_df)
            result["method"] = "rule_fallback"
            result["strategy_config"] = self.STRATEGY_MAP.get(result["state"], {})
            return result
        except Exception as e:
            logger.warning(f"[HMM状态] 异常: {e}，回退规则式")
            fallback = MarketRegimeDetector()
            result = fallback.detect(benchmark_df)
            result["method"] = "rule_fallback"
            result["strategy_config"] = self.STRATEGY_MAP.get(result["state"], {})
            return result

    def _build_features(self, df: pd.DataFrame) -> np.ndarray:
        """构建多维特征矩阵"""
        close = df["close"].values
        volume = df["volume"].values if "volume" in df.columns else np.ones_like(close)
        n = len(close)
        if n < 60:
            return None

        features = []
        for i in range(60, n):
            window = close[i-60:i+1]
            vol_window = volume[i-20:i+1]

            # 特1: 20日收益率
            ret_20 = (close[i] / close[i-20] - 1) if close[i-20] > 0 else 0
            # 特2: 60日收益率
            ret_60 = (close[i] / close[i-60] - 1) if close[i-60] > 0 else 0
            # 特3: 20日波动率
            returns = np.diff(window[-21:]) / (window[-21:-1] + 1e-10)
            vol_20 = np.std(returns) * np.sqrt(252) if len(returns) > 5 else 0
            # 特4: 量能变化(近5日/前20日)
            vol_ratio = np.mean(vol_window[-5:]) / (np.mean(vol_window[:15]) + 1e-10)
            # 特5: MA20斜率
            ma20_now = np.mean(window[-20:])
            ma20_prev = np.mean(window[-25:-5]) if len(window) >= 25 else ma20_now
            ma20_slope = (ma20_now - ma20_prev) / (ma20_prev + 1e-10)
            # 特6: 价格位置(0-1，在60日高低点之间)
            high_60 = np.max(window)
            low_60 = np.min(window)
            price_pos = (close[i] - low_60) / (high_60 - low_60 + 1e-10)

            features.append([ret_20, ret_60, vol_20, vol_ratio, ma20_slope, price_pos])

        return np.array(features)

    def _map_states(self, gmm, features: np.ndarray, df: pd.DataFrame):
        """将GMM聚类结果映射到BULL/RANGE/BEAR
        
        规则: 用每个cluster的平均收益率排序
        - 最高收益 → BULL
        - 最低收益 → BEAR
        - 中间 → RANGE
        """
        labels = gmm.predict(features)
        cluster_returns = {}
        for i in range(self.n_states):
            mask = labels == i
            if mask.sum() > 0:
                # 用特1(20日收益率)的均值排序
                cluster_returns[i] = features[mask, 0].mean()
            else:
                cluster_returns[i] = 0

        sorted_clusters = sorted(cluster_returns.items(), key=lambda x: x[1], reverse=True)
        if len(sorted_clusters) >= 3:
            self.state_labels[sorted_clusters[0][0]] = "BULL"
            self.state_labels[sorted_clusters[1][0]] = "RANGE"
            self.state_labels[sorted_clusters[2][0]] = "BEAR"
        elif len(sorted_clusters) == 2:
            self.state_labels[sorted_clusters[0][0]] = "BULL"
            self.state_labels[sorted_clusters[1][0]] = "BEAR"
        else:
            self.state_labels[0] = "RANGE"

    def get_strategy_for_state(self, state: str) -> dict:
        """根据市场状态获取推荐策略配置"""
        return self.STRATEGY_MAP.get(state, self.STRATEGY_MAP["RANGE"])


def detect_with_ensemble(benchmark_df: pd.DataFrame) -> dict:
    """V3.2: 融合检测（规则式 + HMM）
    
    两者一致 → 高置信度
    两者不一致 → 以规则式为主，降低置信度
    """
    # 规则式检测
    rule_detector = MarketRegimeDetector()
    rule_result = rule_detector.detect(benchmark_df)

    # HMM检测
    hmm_detector = HMMRegimeDetector()
    hmm_result = hmm_detector.fit_and_detect(benchmark_df)

    rule_state = rule_result["state"]
    hmm_state = hmm_result["state"]

    # 融合决策
    if rule_state == hmm_state:
        final_state = rule_state
        confidence = min(0.95, (rule_result["confidence"] + hmm_result["confidence"]) / 2 + 0.1)
        agreement = True
    else:
        # 不一致时以规则式为主（更稳定）
        final_state = rule_state
        confidence = rule_result["confidence"] * 0.7  # 降低置信度
        agreement = False

    strategy_config = HMMRegimeDetector.STRATEGY_MAP.get(final_state, {})

    return {
        "state": final_state,
        "state_cn": {"BULL": "牛市/强势", "RANGE": "震荡/平衡", "BEAR": "熊市/弱势"}[final_state],
        "confidence": round(confidence, 3),
        "agreement": agreement,
        "rule_state": rule_state,
        "hmm_state": hmm_state,
        "strategy_config": strategy_config,
        "recommended_strategies": strategy_config.get("weights", {}),
        "position_range": strategy_config.get("position_range", (0.3, 0.6)),
        "detail": (f"融合检测: {'agree' if agreement else 'disagree'} | "
                  f"rule={rule_state} hmm={hmm_state} → final={final_state}"),
        "detect_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ============================================================
# 门面函数（2026-08-07 P0修复：综合分析报告逐股调用入口）
# ============================================================
_REGIME_FACADE_CACHE = {"date": None, "result": None}


def detect_market_regime(benchmark_df: pd.DataFrame = None) -> dict:
    """大盘状态检测门面函数（当日进程级缓存）

    2026-08-07修复: 综合分析报告composite评分逐股调用本函数，但此前从未定义，
    ImportError被静默吞掉导致熊市高分警告/低分反弹提示成为死代码。
    - 不传benchmark_df时自动从本地DB加载000300日线
    - 结果按日缓存: 同一进程内当日重复调用直接返回缓存，避免逐股重复拉库
    - 任何失败均降级返回 regime="range"，不阻断主流程

    返回:
        {"regime": "bull"/"range"/"bear", "state": "BULL"/...,
         "confidence": float, "detail": str}
    """
    today = datetime.date.today().isoformat()
    if benchmark_df is None and _REGIME_FACADE_CACHE["date"] == today:
        return _REGIME_FACADE_CACHE["result"]

    if benchmark_df is None or len(benchmark_df) < 60:
        try:
            from data.data_loader import load_daily_data
            benchmark_df = load_daily_data("000300", None, days=250)
        except Exception as e:
            logger.warning(f"[大盘状态] 000300基准数据加载失败，降级为range: {e}")
            benchmark_df = None

    fallback = {"regime": "range", "state": "RANGE", "confidence": 0.3,
                "detail": "基准数据不足，默认震荡"}
    if benchmark_df is None or len(benchmark_df) < 60:
        result = fallback
    else:
        try:
            detect_result = MarketRegimeDetector().detect(benchmark_df)
            result = {
                "regime": detect_result["state"].lower(),
                "state": detect_result["state"],
                "confidence": detect_result.get("confidence", 0.5),
                "detail": detect_result.get("detail", ""),
            }
        except Exception as e:
            logger.warning(f"[大盘状态] 检测失败，降级为range: {e}")
            result = fallback

    # 仅无参调用（自动拉库）写入缓存，显式传df的调用不影响缓存
    if _REGIME_FACADE_CACHE["date"] != today and result is not fallback:
        _REGIME_FACADE_CACHE["date"] = today
        _REGIME_FACADE_CACHE["result"] = result
    return result


# ============================================================
# V4.2(P2-8): 市场阶段估计（初期/中期/末期，观察模式）
# ============================================================
# 设计纪律（评审定案，禁止随意修改阈值）:
#   1. 只用规则不用模型 —— A股完整牛熊周期样本极少，任何调参都会过拟合
#   2. 硬前提: market_regime_history.json 积累≥60条才输出阶段，否则自降级为"数据积累中"
#   3. 观察模式: 本函数结果仅供报告展示，严禁接入 target_pct 等仓位计算（如需接入须人工评审）
#   4. 三态切换<5天显示"阶段确认中"，避免拐点附近误导
REGIME_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "market_regime_history.json")
PHASE_MIN_HISTORY = 60      # 阶段判断最少历史条数
PHASE_CONFIRM_DAYS = 5      # 切换后确认期天数


def _load_regime_history(history_path: str = None) -> list:
    """加载regime历史序列（损坏/不存在返回空表）"""
    path = history_path or REGIME_HISTORY_FILE
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"[阶段估计] 历史序列读取失败: {e}")
    return []


def estimate_market_phase(regime_result: dict = None,
                          history_path: str = None) -> dict:
    """市场阶段估计（观察模式，仅展示不参与仓位）

    参数:
        regime_result: MarketRegimeDetector.detect() 的返回（None时自动检测）
        history_path: 历史序列路径（None=默认，测试可传临时路径）

    返回:
        {"available": bool,         # False=数据不足，报告应显示积累进度
         "accumulated_days": int,   # 已积累条数
         "required_days": int,      # 门槛(60)
         "days": int,               # 当前三态持续天数(含今日)
         "phase": str,              # 如"牛市中期"/"震荡第N天"
         "note": str,               # 依据/风险提示(一句话)
         "transition": bool}        # 切换未满确认期
    """
    fallback = {"available": False, "accumulated_days": 0,
                "required_days": PHASE_MIN_HISTORY, "days": 0,
                "phase": "", "note": "", "transition": False}
    try:
        history = _load_regime_history(history_path)
        # 今日状态: 优先用传入结果；历史末条为今日时也认可
        today_iso = datetime.date.today().isoformat()
        state = None
        if isinstance(regime_result, dict) and regime_result.get("state"):
            state = regime_result["state"]
        elif history and history[-1].get("date") == today_iso:
            state = history[-1].get("state")
        if not state:
            fallback["accumulated_days"] = len(history)
            fallback["note"] = "今日状态不可用，仅显示积累进度"
            return fallback

        # 当前三态持续天数（含今日；历史末条若已是今日则不重复计）
        today_iso = datetime.date.today().isoformat()
        _tail_is_today = bool(history and history[-1].get("date") == today_iso)
        days = 1
        tail = history[:-1] if _tail_is_today else history
        for entry in reversed(tail):
            if entry.get("state") == state:
                days += 1
            else:
                break

        result = {"available": len(history) >= PHASE_MIN_HISTORY,
                  "accumulated_days": len(history) if _tail_is_today else len(history) + 1,
                  "required_days": PHASE_MIN_HISTORY,
                  "days": days, "phase": "", "note": "", "transition": False}

        if not result["available"]:
            result["note"] = "历史序列积累中，不参与阶段判断"
            return result

        # ---- 阶段规则（全部为评审定案的经验阈值，禁止回测调参）----
        scores_hist = [e.get("scores", {}) for e in tail[-25:]]
        if state == "BULL":
            # 宽度背离检查: 指数创近期新高但近5日宽度得分转负(顶部特征)
            closes = [e.get("close") for e in tail[-45:] if e.get("close")]
            new_high = bool(closes and closes[-1] >= max(closes[:-5] or closes))
            recent_breadth = [s.get("breadth", 0) for s in scores_hist[-5:]]
            prior_breadth = [s.get("breadth", 0) for s in scores_hist[:-5][-20:]]
            divergence = (new_high and recent_breadth and prior_breadth and
                          sum(recent_breadth) / len(recent_breadth) <= -0.2 and
                          sum(prior_breadth) / len(prior_breadth) > 0)
            if divergence:
                result["phase"] = "牛市末期"
                result["note"] = "指数新高但市场宽度背离，顶部特征，锁盈优先"
            elif days > 90:
                result["phase"] = "牛市末期"
                result["note"] = f"强势已持续{days}天(>90天)，防高位波动放大"
            elif days >= 20:
                result["phase"] = "牛市中期"
                result["note"] = f"强势持续{days}天，持股+止损上移节奏"
            else:
                result["phase"] = "牛市初期"
                result["note"] = f"强势仅{days}天，趋势待确认，不追高"
        elif state == "BEAR":
            # 蓄势检查: 近5日波动率得分转正且前期为负(波动收缩+抛压减轻)
            recent_vol = [s.get("volatility", 0) for s in scores_hist[-5:]]
            prior_vol = [s.get("volatility", 0) for s in scores_hist[:-5][-20:]]
            contraction = (recent_vol and prior_vol and
                           sum(recent_vol) / len(recent_vol) >= 0.2 and
                           sum(prior_vol) / len(prior_vol) < 0)
            if contraction and days >= 15:
                result["phase"] = "熊市末期"
                result["note"] = "波动收缩+抛压减轻，蓄势特征，但仍不抄底"
            elif days < 15:
                result["phase"] = "熊市初期"
                result["note"] = f"弱势仅{days}天，降仓防守优先，不接飞刀"
            else:
                result["phase"] = "熊市中期"
                result["note"] = f"弱势持续{days}天，轻仓等待企稳信号"
        else:  # RANGE
            result["phase"] = f"震荡第{days}天"
            result["note"] = "震荡市不细分阶段，高抛低吸+单票仓位控制"

        # 切换确认期: 未满5天不输出确定阶段，避免拐点附近误导
        if days < PHASE_CONFIRM_DAYS:
            result["transition"] = True
            result["note"] += "；三态切换未满5天，阶段确认中(判断可能滞后)"
        return result
    except Exception as e:
        logger.warning(f"[阶段估计] 异常降级: {e}")
        return fallback


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
