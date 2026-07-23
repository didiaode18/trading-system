"""
Meta-Labeling 信号二级过滤模型
==============================
基于 López de Prado《Advances in Financial Machine Learning》第3章

核心思想:
  一级模型（趋势策略）产生买卖信号 → 二级模型（本模块）判断信号是否值得执行
  
  不是预测"涨还是跌"，而是预测"这个信号会不会赚钱"

过滤维度:
  1. 信号强度（quality_score）
  2. 量价确认度（volume confirmation）
  3. 大盘环境（market regime）
  4. 板块强度（sector momentum）
  5. 信号拥挤度（crowding）
  6. 技术共振度（multi-indicator agreement）
  7. 波动率环境（volatility regime）

输出:
  confidence: 0~1 信号置信度
  decision: "execute" / "observe" / "reject"
  reasons: 过滤原因列表

使用方式:
    from strategy.meta_label import MetaLabelFilter
    filter = MetaLabelFilter()
    result = filter.evaluate(code, signal, df, market_info, sector_info, all_signals)
"""

import pandas as pd
import numpy as np
import logging
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class MetaLabelFilter:
    """
    Meta-Labeling 信号过滤器
    
    对每个一级信号进行二次评估，输出置信度和执行建议
    """

    def __init__(self, config_override: dict = None):
        """
        参数:
            config_override: 覆盖默认参数
        """
        cfg = config_override or {}
        # 置信度阈值
        self.execute_threshold = cfg.get("execute_threshold", 0.55)   # >= 此值执行
        self.observe_threshold = cfg.get("observe_threshold", 0.35)   # >= 此值观察，< 此值拒绝
        # 各维度权重（总和=1.0）
        self.weights = cfg.get("weights", {
            "signal_strength": 0.20,    # 信号自身质量
            "volume_confirm": 0.18,     # 量价确认
            "market_regime": 0.18,      # 大盘环境
            "sector_momentum": 0.15,    # 板块动量
            "crowding": 0.10,           # 信号拥挤度（反向）
            "technical_agree": 0.12,    # 技术共振
            "volatility": 0.07,         # 波动率环境
        })
        # 历史信号记录（用于拥挤度计算）
        self._signal_history = []

    def evaluate(self, code: str, signal: dict, df: pd.DataFrame,
                 market_info: dict = None, sector_info: dict = None,
                 all_signals: list = None) -> dict:
        """
        评估单个信号的置信度
        
        参数:
            code: 股票代码
            signal: 一级策略信号 (generate_strategy_signal输出)
            df: 该股票的日线DataFrame（含技术指标）
            market_info: 大盘状态 {"market_state": "up/neutral/down", "confidence": 0~1}
            sector_info: 板块信息 {"score": 0~100, "is_valid": bool}
            all_signals: 当前所有信号列表 [(code, sig), ...]（用于拥挤度）
        
        返回:
            {
                "confidence": float,      # 0~1 综合置信度
                "decision": str,          # "execute" / "observe" / "reject"
                "scores": dict,           # 各维度得分
                "reasons": list,          # 过滤/降级原因
                "original_signal": dict,  # 原始信号（不修改）
            }
        """
        if market_info is None:
            market_info = {}
        if sector_info is None:
            sector_info = {}
        if all_signals is None:
            all_signals = []

        scores = {}
        reasons = []

        # ---- 1. 信号强度 (0~1) ----
        scores["signal_strength"] = self._score_signal_strength(signal)

        # ---- 2. 量价确认 (0~1) ----
        scores["volume_confirm"] = self._score_volume_confirmation(signal, df)

        # ---- 3. 大盘环境 (0~1) ----
        scores["market_regime"] = self._score_market_regime(market_info)

        # ---- 4. 板块动量 (0~1) ----
        scores["sector_momentum"] = self._score_sector_momentum(sector_info, df)

        # ---- 5. 信号拥挤度 (0~1, 越高越好=越不拥挤) ----
        scores["crowding"] = self._score_crowding(code, signal, all_signals)

        # ---- 6. 技术共振 (0~1) ----
        scores["technical_agree"] = self._score_technical_agreement(signal, df)

        # ---- 7. 波动率环境 (0~1) ----
        scores["volatility"] = self._score_volatility_regime(df)

        # ---- 加权合成 ----
        confidence = sum(
            scores[dim] * self.weights.get(dim, 0)
            for dim in scores
        )
        confidence = max(0.0, min(1.0, confidence))

        # ---- 硬性否决条件（无论得分多高都降级）----
        veto_reasons = self._check_veto_conditions(signal, df, market_info)
        if veto_reasons:
            confidence = min(confidence, 0.30)
            reasons.extend(veto_reasons)

        # ---- 决策 ----
        if confidence >= self.execute_threshold:
            decision = "execute"
        elif confidence >= self.observe_threshold:
            decision = "observe"
            reasons.append(f"置信度{confidence:.0%}未达执行阈值{self.execute_threshold:.0%}，降级为观察")
        else:
            decision = "reject"
            reasons.append(f"置信度{confidence:.0%}低于观察阈值{self.observe_threshold:.0%}，拒绝执行")

        # 记录信号历史
        self._signal_history.append({
            "code": code,
            "date": signal.get("date", ""),
            "confidence": confidence,
            "decision": decision,
        })

        return {
            "confidence": round(confidence, 3),
            "decision": decision,
            "scores": {k: round(v, 3) for k, v in scores.items()},
            "reasons": reasons,
            "original_signal": signal,
        }

    def batch_evaluate(self, signals: list, data_dict: dict,
                       market_info: dict = None, sector_info_map: dict = None) -> list:
        """
        批量评估所有信号
        
        参数:
            signals: [(code, signal_dict), ...]
            data_dict: {code: DataFrame}
            market_info: 大盘状态
            sector_info_map: {code: sector_info}
        
        返回:
            [(code, signal, meta_result), ...]
        """
        if sector_info_map is None:
            sector_info_map = {}

        results = []
        for code, sig in signals:
            df = data_dict.get(code)
            if df is None or df.empty:
                # 无数据，默认通过
                results.append((code, sig, {
                    "confidence": 0.5,
                    "decision": "execute",
                    "scores": {},
                    "reasons": ["无数据，默认通过"],
                    "original_signal": sig,
                }))
                continue

            sector_info = sector_info_map.get(code, {})
            meta = self.evaluate(code, sig, df, market_info, sector_info, signals)
            results.append((code, sig, meta))

        # 统计
        execute_count = sum(1 for _, _, m in results if m["decision"] == "execute")
        observe_count = sum(1 for _, _, m in results if m["decision"] == "observe")
        reject_count = sum(1 for _, _, m in results if m["decision"] == "reject")
        logger.info(f"[Meta-Label] 信号过滤: {len(results)}个信号 → "
                    f"执行{execute_count} | 观察{observe_count} | 拒绝{reject_count}")

        return results

    # ============================================================
    # 各维度评分函数
    # ============================================================

    def _score_signal_strength(self, signal: dict) -> float:
        """信号自身质量评分"""
        score = 0.5  # 基础分

        # quality_score (0~100)
        qs = signal.get("quality_score", 50)
        if qs >= 80:
            score = 0.9
        elif qs >= 65:
            score = 0.75
        elif qs >= 50:
            score = 0.55
        elif qs >= 35:
            score = 0.35
        else:
            score = 0.2

        # 双买点共振加分
        reason = signal.get("signal_reason", "")
        if "双买点共振" in reason or "★" in reason:
            score = min(1.0, score + 0.15)

        # 卖出信号（止损/破位）不需要过滤，直接高分
        if signal.get("sell_signal"):
            score = 0.85

        return score

    def _score_volume_confirmation(self, signal: dict, df: pd.DataFrame) -> float:
        """量价确认度"""
        if df is None or len(df) < 20:
            return 0.5

        latest = df.iloc[-1]
        score = 0.5

        # 买入信号：需要缩量（回踩）或放量（突破）确认
        if signal.get("buy_signal"):
            vol = latest.get("volume", 0)
            vol_ma = latest.get("vol_ma20", 0) if "vol_ma20" in df.columns else df["volume"].tail(20).mean()

            if vol_ma > 0:
                vol_ratio = vol / vol_ma
                buy_type = signal.get("buy_type", "")

                if "回踩" in buy_type or "缩量" in signal.get("signal_reason", ""):
                    # 缩量回踩：量比 < 0.7 为佳
                    if vol_ratio < 0.5:
                        score = 0.9
                    elif vol_ratio < 0.7:
                        score = 0.75
                    elif vol_ratio < 1.0:
                        score = 0.55
                    else:
                        score = 0.3  # 放量回踩，可能是假回踩
                elif "突破" in buy_type:
                    # 放量突破：量比 > 1.5 为佳
                    if vol_ratio > 2.0:
                        score = 0.9
                    elif vol_ratio > 1.5:
                        score = 0.75
                    elif vol_ratio > 1.2:
                        score = 0.55
                    else:
                        score = 0.3  # 缩量突破，可能是假突破

        # 卖出信号：放量下跌确认
        elif signal.get("sell_signal"):
            vol = latest.get("volume", 0)
            vol_ma = df["volume"].tail(20).mean() if len(df) >= 20 else vol
            if vol_ma > 0 and vol / vol_ma > 1.3:
                score = 0.85  # 放量确认
            else:
                score = 0.65  # 缩量卖出信号，可能是洗盘

        # OBV/CMF确认（如果有）
        if "obv" in df.columns and len(df) >= 5:
            obv_trend = df["obv"].iloc[-1] - df["obv"].iloc[-5]
            if signal.get("buy_signal") and obv_trend > 0:
                score = min(1.0, score + 0.1)
            elif signal.get("buy_signal") and obv_trend < 0:
                score = max(0.1, score - 0.1)

        return score

    def _score_market_regime(self, market_info: dict) -> float:
        """大盘环境评分"""
        state = market_info.get("market_state", "neutral")
        confidence = market_info.get("confidence", 0.5)

        if state == "up":
            return 0.7 + 0.3 * confidence  # 0.7~1.0
        elif state == "neutral":
            return 0.45 + 0.1 * confidence  # 0.45~0.55
        elif state == "down":
            return 0.3 - 0.15 * confidence  # 0.15~0.30
        else:
            return 0.5

    def _score_sector_momentum(self, sector_info: dict, df: pd.DataFrame) -> float:
        """板块动量评分"""
        score = 0.5

        # 板块评分
        sector_score = sector_info.get("score", 50)
        if sector_score >= 70:
            score = 0.85
        elif sector_score >= 55:
            score = 0.65
        elif sector_score >= 40:
            score = 0.45
        else:
            score = 0.25

        # 板块有效性
        if not sector_info.get("is_valid", True):
            score = max(0.2, score - 0.2)

        # 个股相对板块强度
        if df is not None and len(df) >= 20:
            stock_change_5d = (df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100 if len(df) >= 6 else 0
            if stock_change_5d > 3:
                score = min(1.0, score + 0.1)  # 强于板块
            elif stock_change_5d < -3:
                score = max(0.1, score - 0.1)  # 弱于板块

        return score

    def _score_crowding(self, code: str, signal: dict, all_signals: list) -> float:
        """
        信号拥挤度（反向指标：越拥挤分越低）
        
        同赛道同时出买入信号的股票越多，每只的置信度越低
        """
        if not all_signals or not signal.get("buy_signal"):
            return 0.7  # 卖出信号不受拥挤度影响

        # 统计同方向信号数量
        buy_signals = [(c, s) for c, s in all_signals
                       if s.get("buy_signal") and c != code]

        if not buy_signals:
            return 0.9  # 唯一买入信号

        # 同赛道信号
        my_info = config.get_stock_info(code)
        my_sector = my_info.get("赛道", "")
        same_sector = 0
        for c, s in buy_signals:
            info = config.get_stock_info(c)
            if info.get("赛道", "") == my_sector:
                same_sector += 1

        # 拥挤度惩罚
        total_buy = len(buy_signals) + 1  # 包含自己
        if same_sector >= 3:
            return 0.25  # 同赛道3只以上同时出信号，极度拥挤
        elif same_sector >= 2:
            return 0.45
        elif total_buy >= 5:
            return 0.50  # 总共5只以上买入信号
        elif total_buy >= 3:
            return 0.65
        else:
            return 0.80

    def _score_technical_agreement(self, signal: dict, df: pd.DataFrame) -> float:
        """
        技术共振度：多个独立指标是否同向确认
        """
        if df is None or len(df) < 60:
            return 0.5

        latest = df.iloc[-1]
        agreements = 0
        total_checks = 0

        # 检查各指标方向
        if signal.get("buy_signal"):
            # MA排列
            total_checks += 1
            if ("ma5" in df.columns and "ma10" in df.columns and "ma20" in df.columns):
                if latest["ma5"] > latest["ma10"] > latest["ma20"]:
                    agreements += 1

            # MACD
            total_checks += 1
            if "macd_hist" in df.columns:
                if latest["macd_hist"] > 0:
                    agreements += 1

            # RSI
            total_checks += 1
            if "rsi" in df.columns and not pd.isna(latest["rsi"]):
                if 40 < latest["rsi"] < 70:  # 健康区间
                    agreements += 1

            # 布林带位置
            total_checks += 1
            if "boll_mid" in df.columns and not pd.isna(latest.get("boll_mid")):
                if latest["close"] > latest["boll_mid"]:
                    agreements += 1

            # MA60方向
            total_checks += 1
            if "ma60" in df.columns and len(df) >= 65:
                ma60_slope = df["ma60"].iloc[-1] - df["ma60"].iloc[-5]
                if ma60_slope > 0:
                    agreements += 1

        elif signal.get("sell_signal"):
            # 卖出信号的共振
            total_checks += 1
            if "ma20" in df.columns and latest["close"] < latest["ma20"]:
                agreements += 1

            total_checks += 1
            if "macd_hist" in df.columns and latest["macd_hist"] < 0:
                agreements += 1

            total_checks += 1
            if "ma60" in df.columns and len(df) >= 65:
                ma60_slope = df["ma60"].iloc[-1] - df["ma60"].iloc[-5]
                if ma60_slope < 0:
                    agreements += 1

        if total_checks == 0:
            return 0.5

        ratio = agreements / total_checks
        return 0.2 + 0.8 * ratio  # 映射到 0.2~1.0

    def _score_volatility_regime(self, df: pd.DataFrame) -> float:
        """
        波动率环境评分
        低波动 → 趋势策略有效（高分）
        高波动 → 假信号多（低分）
        """
        if df is None or len(df) < 20:
            return 0.5

        # 20日波动率（年化）
        returns = df["close"].pct_change().tail(20)
        vol_20d = returns.std() * np.sqrt(252)

        # ATR相对波动率
        if "atr" in df.columns and not pd.isna(df["atr"].iloc[-1]):
            atr_pct = df["atr"].iloc[-1] / df["close"].iloc[-1]
        else:
            atr_pct = vol_20d / np.sqrt(252)

        # 评分：年化波动率 15%~30% 为正常
        if vol_20d < 0.15:
            return 0.85  # 低波动，趋势清晰
        elif vol_20d < 0.25:
            return 0.70  # 正常波动
        elif vol_20d < 0.40:
            return 0.50  # 偏高波动
        elif vol_20d < 0.60:
            return 0.30  # 高波动，假信号多
        else:
            return 0.15  # 极端波动

    # ============================================================
    # 硬性否决条件
    # ============================================================

    def _check_veto_conditions(self, signal: dict, df: pd.DataFrame,
                               market_info: dict) -> list:
        """
        硬性否决条件（无论得分多高都降级）
        """
        reasons = []

        # 1. 大盘极端弱势 + 买入信号
        if signal.get("buy_signal"):
            state = market_info.get("market_state", "neutral")
            conf = market_info.get("confidence", 0.5)
            if state == "down" and conf > 0.7:
                reasons.append("大盘明确熊市(置信度>70%)，买入信号强制降级")

        # 2. 连续跌停后反弹（可能是死猫跳）
        if df is not None and len(df) >= 5 and signal.get("buy_signal"):
            recent_5 = df.tail(5)
            limit_down_days = 0
            for i in range(len(recent_5)):
                if i > 0:
                    chg = (recent_5["close"].iloc[i] / recent_5["close"].iloc[i-1] - 1)
                    if chg < -0.09:
                        limit_down_days += 1
            if limit_down_days >= 2:
                reasons.append(f"近5日有{limit_down_days}个跌停，反弹可能是死猫跳")

        # 3. 财报前5天（如果有事件日历数据）
        # 由 event_calendar 模块提供，此处预留接口

        # 4. 单日振幅 > 15%（极端行情）
        if df is not None and len(df) >= 1:
            latest = df.iloc[-1]
            if "intraday_range" in df.columns and not pd.isna(latest.get("intraday_range")):
                if latest["intraday_range"] > 0.15:
                    reasons.append(f"当日振幅{latest['intraday_range']:.1%}异常，信号不可靠")

        return reasons


# ============================================================
# 便捷函数
# ============================================================

def apply_meta_label(signals: list, data_dict: dict,
                     market_info: dict = None, sector_info_map: dict = None,
                     config_override: dict = None) -> list:
    """
    对信号列表应用Meta-Labeling过滤
    
    参数:
        signals: [(code, signal_dict), ...]
        data_dict: {code: DataFrame}
        market_info: 大盘状态
        sector_info_map: {code: sector_info}
        config_override: 覆盖默认参数
    
    返回:
        filtered_signals: [(code, signal_dict), ...] 只保留execute和observe的
        meta_results: {code: meta_result} 所有信号的评估结果
    """
    filter_obj = MetaLabelFilter(config_override)
    results = filter_obj.batch_evaluate(signals, data_dict, market_info, sector_info_map)

    filtered = []
    meta_map = {}
    for code, sig, meta in results:
        meta_map[code] = meta
        if meta["decision"] in ("execute", "observe"):
            # 将置信度写入信号（供条件单展示）
            sig["meta_confidence"] = meta["confidence"]
            sig["meta_decision"] = meta["decision"]
            if meta["reasons"]:
                sig["meta_reasons"] = meta["reasons"]
            filtered.append((code, sig))
        else:
            logger.info(f"  [Meta-Label] {code} 信号被拒绝: {meta['reasons']}")

    return filtered, meta_map
