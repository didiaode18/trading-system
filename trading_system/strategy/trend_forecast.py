"""
持仓趋势预测分析模块
====================
对每只持仓股票进行未来趋势预测，生成分析报告并发送到邮箱。

分析维度:
  1. 趋势方向预测（MA系统+MACD+线性回归斜率）
  2. 动量预测（RSI+KDJ+成交量趋势）
  3. 支撑/压力位计算（布林带+历史高低点+筹码密集区）
  4. 波动率预测（ATR+历史波动率）
  5. 综合评分 + 操作建议 + 最佳操作时间窗口

输出:
  - 每只股票一份独立分析（趋势方向/置信度/目标价/止损价/时间窗口）
  - HTML邮件报告（含操作建议表格）
  - 定时执行建议（盘前/盘中/盘后）

使用:
  在main.py中集成调用，或独立运行:
  python -m strategy.trend_forecast
"""

import pandas as pd
import numpy as np
import logging
import datetime
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class TrendForecaster:
    """持仓趋势预测分析器"""

    def __init__(self, lookback_days=None):
        self.lookback_days = lookback_days or getattr(config, 'FORECAST_LOOKBACK_DAYS', 120)

    # ============================================================
    # 一、单只股票趋势预测
    # ============================================================

    def analyze_stock(self, code: str, df: pd.DataFrame, holding: dict = None) -> dict:
        """
        对单只股票进行完整趋势预测分析

        参数:
            code: 股票代码
            df: 含技术指标的日线DataFrame（需已compute_indicators）
            holding: 持仓信息 {"shares", "buy_price", "highest_price", ...}

        返回:
            预测分析结果字典
        """
        if df.empty or len(df) < 30:
            return {"code": code, "error": "数据不足", "valid": False}

        name = config.get_stock_name(code)
        latest = df.iloc[-1]
        current_price = latest["close"]

        # ---- 1. 趋势方向分析 ----
        trend_result = self._analyze_trend(df)

        # ---- 2. 动量分析 ----
        momentum_result = self._analyze_momentum(df)

        # ---- 3. 支撑压力位 ----
        levels_result = self._calc_support_resistance(df)

        # ---- 4. 波动率分析 ----
        volatility_result = self._analyze_volatility(df)

        # ---- 5. 量价关系 ----
        volume_result = self._analyze_volume(df)

        # ---- 6. 综合评分 ----
        composite = self._composite_score(
            trend_result, momentum_result, levels_result,
            volatility_result, volume_result, current_price
        )

        # ---- 7. 持仓盈亏分析 ----
        holding_analysis = {}
        if holding:
            buy_price = holding.get("buy_price", current_price)
            shares = holding.get("shares", 0)
            highest = holding.get("highest_price", current_price)
            pnl_pct = (current_price - buy_price) / buy_price * 100 if buy_price > 0 else 0
            drawdown_from_high = (current_price - highest) / highest * 100 if highest > 0 else 0
            market_value = shares * current_price

            holding_analysis = {
                "buy_price": buy_price,
                "shares": shares,
                "market_value": market_value,
                "pnl_pct": pnl_pct,
                "pnl_amount": (current_price - buy_price) * shares,
                "highest_price": highest,
                "drawdown_from_high": drawdown_from_high,
            }

        # ---- 8. 操作建议 + 最佳时间窗口 ----
        advice = self._generate_advice(
            composite, trend_result, momentum_result,
            levels_result, holding_analysis, current_price
        )

        return {
            "code": code,
            "name": name,
            "valid": True,
            "current_price": current_price,
            "analysis_date": datetime.date.today().strftime("%Y-%m-%d"),
            "trend": trend_result,
            "momentum": momentum_result,
            "levels": levels_result,
            "volatility": volatility_result,
            "volume": volume_result,
            "composite": composite,
            "holding": holding_analysis,
            "advice": advice,
        }

    # ============================================================
    # 二、趋势方向分析
    # ============================================================

    def _analyze_trend(self, df: pd.DataFrame) -> dict:
        """
        趋势方向判断:
        - MA排列（多头/空头/缠绕）
        - MACD金叉/死叉 + 柱状图方向
        - 线性回归斜率（20日/60日）
        - 价格相对MA20/MA60位置
        """
        latest = df.iloc[-1]
        close = df["close"].values

        # MA排列
        ma5 = latest.get("ma5", 0)
        ma10 = latest.get("ma10", 0)
        ma20 = latest.get("ma20", 0)
        ma60 = latest.get("ma60", 0)

        if ma5 > ma10 > ma20 > ma60:
            ma_pattern = "多头排列"
            ma_score = 2
        elif ma5 < ma10 < ma20 < ma60:
            ma_pattern = "空头排列"
            ma_score = -2
        elif ma5 > ma10 > ma20:
            ma_pattern = "偏多排列"
            ma_score = 1
        elif ma5 < ma10 < ma20:
            ma_pattern = "偏空排列"
            ma_score = -1
        else:
            ma_pattern = "均线缠绕"
            ma_score = 0

        # MACD状态
        macd_dif = latest.get("macd_dif", 0)
        macd_dea = latest.get("macd_dea", 0)
        macd_hist = latest.get("macd_hist", 0)

        # 判断金叉/死叉（最近3天）
        macd_signal = "中性"
        macd_score = 0
        if len(df) >= 3:
            prev_hist = df["macd_hist"].iloc[-2] if "macd_hist" in df.columns else 0
            if macd_hist > 0 and prev_hist <= 0:
                macd_signal = "金叉(刚发生)"
                macd_score = 2
            elif macd_hist < 0 and prev_hist >= 0:
                macd_signal = "死叉(刚发生)"
                macd_score = -2
            elif macd_hist > 0:
                # 柱状图放大还是缩小
                if len(df) >= 5:
                    hist_trend = df["macd_hist"].iloc[-5:].values
                    if hist_trend[-1] > hist_trend[-3]:
                        macd_signal = "红柱放大(多头增强)"
                        macd_score = 1
                    else:
                        macd_signal = "红柱缩小(多头减弱)"
                        macd_score = 0.5
            elif macd_hist < 0:
                if len(df) >= 5:
                    hist_trend = df["macd_hist"].iloc[-5:].values
                    if hist_trend[-1] < hist_trend[-3]:
                        macd_signal = "绿柱放大(空头增强)"
                        macd_score = -1
                    else:
                        macd_signal = "绿柱缩小(空头减弱)"
                        macd_score = -0.5

        # 线性回归斜率（20日）
        slope_20 = 0
        if len(close) >= 20:
            x = np.arange(20)
            y = close[-20:]
            slope_20 = np.polyfit(x, y, 1)[0]
            slope_20_pct = slope_20 / current_price_safe(close[-1]) * 100
        else:
            slope_20_pct = 0

        # 60日斜率
        slope_60 = 0
        if len(close) >= 60:
            x = np.arange(60)
            y = close[-60:]
            slope_60 = np.polyfit(x, y, 1)[0]
            slope_60_pct = slope_60 / current_price_safe(close[-1]) * 100
        else:
            slope_60_pct = 0

        # 价格vs均线位置
        above_ma20 = close[-1] > ma20 if ma20 > 0 else True
        above_ma60 = close[-1] > ma60 if ma60 > 0 else True

        # 综合趋势评分 (-5 ~ +5)
        trend_score = ma_score + macd_score
        if above_ma20:
            trend_score += 0.5
        else:
            trend_score -= 0.5
        if above_ma60:
            trend_score += 0.5
        else:
            trend_score -= 0.5
        if slope_20_pct > 0.3:
            trend_score += 0.5
        elif slope_20_pct < -0.3:
            trend_score -= 0.5

        # 趋势方向判定
        if trend_score >= 3:
            direction = "强势上涨"
        elif trend_score >= 1.5:
            direction = "温和上涨"
        elif trend_score > -1.5:
            direction = "横盘震荡"
        elif trend_score > -3:
            direction = "温和下跌"
        else:
            direction = "加速下跌"

        return {
            "direction": direction,
            "score": round(trend_score, 1),
            "ma_pattern": ma_pattern,
            "macd_signal": macd_signal,
            "slope_20d_pct": round(slope_20_pct, 2),
            "slope_60d_pct": round(slope_60_pct, 2),
            "above_ma20": above_ma20,
            "above_ma60": above_ma60,
            "ma20": round(ma20, 2) if ma20 else 0,
            "ma60": round(ma60, 2) if ma60 else 0,
        }

    # ============================================================
    # 三、动量分析
    # ============================================================

    def _analyze_momentum(self, df: pd.DataFrame) -> dict:
        """RSI + KDJ + 动量变化率"""
        latest = df.iloc[-1]
        close = df["close"].values

        # RSI
        rsi = latest.get("rsi", 50)
        if rsi >= 80:
            rsi_state = "严重超买"
            rsi_score = -2
        elif rsi >= 70:
            rsi_state = "超买"
            rsi_score = -1
        elif rsi <= 20:
            rsi_state = "严重超卖"
            rsi_score = 2
        elif rsi <= 30:
            rsi_state = "超卖"
            rsi_score = 1
        elif 45 <= rsi <= 55:
            rsi_state = "中性"
            rsi_score = 0
        elif rsi > 55:
            rsi_state = "偏强"
            rsi_score = 0.5
        else:
            rsi_state = "偏弱"
            rsi_score = -0.5

        # KDJ（自行计算）
        k_value, d_value, j_value = self._calc_kdj(df)
        if j_value > 100:
            kdj_state = "超买区"
            kdj_score = -1
        elif j_value < 0:
            kdj_state = "超卖区"
            kdj_score = 1
        elif k_value > d_value:
            kdj_state = "金叉向上"
            kdj_score = 1
        else:
            kdj_state = "死叉向下"
            kdj_score = -1

        # 5日动量（近5天涨跌幅）
        momentum_5d = 0
        if len(close) >= 6:
            momentum_5d = (close[-1] - close[-6]) / close[-6] * 100

        # 10日动量
        momentum_10d = 0
        if len(close) >= 11:
            momentum_10d = (close[-1] - close[-11]) / close[-11] * 100

        # 动量加速度（5日动量 vs 前5日动量）
        momentum_accel = 0
        if len(close) >= 11:
            prev_5d = (close[-6] - close[-11]) / close[-11] * 100
            momentum_accel = momentum_5d - prev_5d

        momentum_score = rsi_score + kdj_score
        if momentum_5d > 3:
            momentum_score += 0.5
        elif momentum_5d < -3:
            momentum_score -= 0.5

        return {
            "score": round(momentum_score, 1),
            "rsi": round(rsi, 1),
            "rsi_state": rsi_state,
            "kdj_k": round(k_value, 1),
            "kdj_d": round(d_value, 1),
            "kdj_j": round(j_value, 1),
            "kdj_state": kdj_state,
            "momentum_5d_pct": round(momentum_5d, 2),
            "momentum_10d_pct": round(momentum_10d, 2),
            "momentum_accel": round(momentum_accel, 2),
        }

    # ============================================================
    # 四、支撑压力位计算
    # ============================================================

    def _calc_support_resistance(self, df: pd.DataFrame) -> dict:
        """计算关键支撑位和压力位"""
        latest = df.iloc[-1]
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        current = close[-1]

        # 布林带
        boll_upper = latest.get("boll_upper", current * 1.1)
        boll_lower = latest.get("boll_lower", current * 0.9)
        boll_mid = latest.get("boll_mid", current)

        # 近期高低点
        lookback = min(60, len(df))
        recent_high = max(high[-lookback:])
        recent_low = min(low[-lookback:])

        # MA支撑/压力
        ma20 = latest.get("ma20", current)
        ma60 = latest.get("ma60", current)

        # 收集所有支撑位（低于当前价）
        supports = []
        if ma20 < current:
            supports.append(("MA20", ma20))
        if ma60 < current:
            supports.append(("MA60", ma60))
        if boll_lower < current:
            supports.append(("布林下轨", boll_lower))
        if recent_low < current:
            supports.append(("近期低点", recent_low))

        # 收集所有压力位（高于当前价）
        resistances = []
        if ma20 > current:
            resistances.append(("MA20", ma20))
        if ma60 > current:
            resistances.append(("MA60", ma60))
        if boll_upper > current:
            resistances.append(("布林上轨", boll_upper))
        if recent_high > current:
            resistances.append(("近期高点", recent_high))

        # 排序：支撑位从高到低，压力位从低到高
        supports.sort(key=lambda x: x[1], reverse=True)
        resistances.sort(key=lambda x: x[1])

        # 第一支撑/压力
        first_support = supports[0][1] if supports else current * 0.95
        first_resistance = resistances[0][1] if resistances else current * 1.05

        return {
            "current_price": round(current, 2),
            "boll_upper": round(boll_upper, 2),
            "boll_mid": round(boll_mid, 2),
            "boll_lower": round(boll_lower, 2),
            "recent_high": round(recent_high, 2),
            "recent_low": round(recent_low, 2),
            "first_support": round(first_support, 2),
            "first_support_name": supports[0][0] if supports else "估算",
            "first_resistance": round(first_resistance, 2),
            "first_resistance_name": resistances[0][0] if resistances else "估算",
            "supports": [(n, round(v, 2)) for n, v in supports[:3]],
            "resistances": [(n, round(v, 2)) for n, v in resistances[:3]],
            "distance_to_support_pct": round((current - first_support) / current * 100, 1),
            "distance_to_resistance_pct": round((first_resistance - current) / current * 100, 1),
        }

    # ============================================================
    # 五、波动率分析
    # ============================================================

    def _analyze_volatility(self, df: pd.DataFrame) -> dict:
        """ATR + 历史波动率 + 波动率趋势"""
        close = df["close"].values

        # ATR
        atr = df.iloc[-1].get("atr", 0)
        atr_pct = atr / current_price_safe(close[-1]) * 100 if atr > 0 else 0

        # 20日历史波动率（年化）
        if len(close) >= 21:
            returns = np.diff(np.log(close[-21:]))
            vol_20d = np.std(returns) * np.sqrt(252) * 100
        else:
            vol_20d = 0

        # 波动率趋势（近5日ATR vs 前20日ATR均值）
        if "atr" in df.columns and len(df) >= 25:
            atr_recent = df["atr"].iloc[-5:].mean()
            atr_prev = df["atr"].iloc[-25:-5].mean()
            vol_trend = "放大" if atr_recent > atr_prev * 1.2 else ("缩小" if atr_recent < atr_prev * 0.8 else "平稳")
        else:
            vol_trend = "未知"

        # 预测明日波动范围
        expected_range = atr if atr > 0 else close[-1] * 0.02
        expected_high = close[-1] + expected_range * 0.6
        expected_low = close[-1] - expected_range * 0.6

        return {
            "atr": round(atr, 2),
            "atr_pct": round(atr_pct, 2),
            "volatility_20d_annual": round(vol_20d, 1),
            "vol_trend": vol_trend,
            "expected_high": round(expected_high, 2),
            "expected_low": round(expected_low, 2),
            "expected_range_pct": round(expected_range / current_price_safe(close[-1]) * 100, 1),
        }

    # ============================================================
    # 六、量价关系分析
    # ============================================================

    def _analyze_volume(self, df: pd.DataFrame) -> dict:
        """成交量趋势 + 量价配合度"""
        close = df["close"].values
        volume = df["volume"].values

        if len(volume) < 20:
            return {"score": 0, "state": "数据不足"}

        vol_ma20 = df.iloc[-1].get("vol_ma20", np.mean(volume[-20:]))
        current_vol = volume[-1]
        vol_ratio = current_vol / vol_ma20 if vol_ma20 > 0 else 1

        # 量价配合
        price_up = close[-1] > close[-2] if len(close) >= 2 else True
        if price_up and vol_ratio > 1.2:
            vp_state = "放量上涨(健康)"
            vp_score = 1
        elif price_up and vol_ratio < 0.7:
            vp_state = "缩量上涨(动力不足)"
            vp_score = -0.5
        elif not price_up and vol_ratio > 1.5:
            vp_state = "放量下跌(恐慌)"
            vp_score = -2
        elif not price_up and vol_ratio < 0.7:
            vp_state = "缩量下跌(抛压减轻)"
            vp_score = 0.5
        else:
            vp_state = "量价平稳"
            vp_score = 0

        # 5日量能趋势
        if len(volume) >= 10:
            vol_5d_avg = np.mean(volume[-5:])
            vol_prev_5d_avg = np.mean(volume[-10:-5])
            vol_trend_pct = (vol_5d_avg - vol_prev_5d_avg) / vol_prev_5d_avg * 100 if vol_prev_5d_avg > 0 else 0
        else:
            vol_trend_pct = 0

        return {
            "score": vp_score,
            "vol_ratio": round(vol_ratio, 2),
            "vp_state": vp_state,
            "vol_trend_5d_pct": round(vol_trend_pct, 1),
            "current_vol": int(current_vol),
            "vol_ma20": int(vol_ma20),
        }

    # ============================================================
    # 七、综合评分
    # ============================================================

    def _composite_score(self, trend, momentum, levels, volatility, volume, current_price) -> dict:
        """
        综合评分（0-100分）
        - 趋势权重 40%
        - 动量权重 25%
        - 量价权重 20%
        - 位置权重 15%（距支撑/压力）
        """
        # 趋势分（-5~+5 映射到 0~100）
        trend_norm = (trend["score"] + 5) / 10 * 100

        # 动量分（-4~+4 映射到 0~100）
        momentum_norm = (momentum["score"] + 4) / 8 * 100

        # 量价分（-2~+2 映射到 0~100）
        volume_norm = (volume.get("score", 0) + 2) / 4 * 100

        # 位置分（距支撑近=高分，距压力近=低分）
        dist_support = levels.get("distance_to_support_pct", 5)
        dist_resistance = levels.get("distance_to_resistance_pct", 5)
        if dist_support + dist_resistance > 0:
            position_norm = dist_support / (dist_support + dist_resistance) * 100
        else:
            position_norm = 50

        # 加权综合
        total_score = (
            trend_norm * 0.40 +
            momentum_norm * 0.25 +
            volume_norm * 0.20 +
            position_norm * 0.15
        )

        # 评级
        if total_score >= 75:
            rating = "强烈看多"
            rating_color = "#e74c3c"
        elif total_score >= 60:
            rating = "偏多"
            rating_color = "#f39c12"
        elif total_score >= 40:
            rating = "中性震荡"
            rating_color = "#95a5a6"
        elif total_score >= 25:
            rating = "偏空"
            rating_color = "#27ae60"
        else:
            rating = "强烈看空"
            rating_color = "#16a085"

        # 未来3-5日预测方向
        if total_score >= 65:
            forecast_3d = "大概率上涨"
            forecast_confidence = min(85, int(total_score))
        elif total_score >= 50:
            forecast_3d = "震荡偏多"
            forecast_confidence = min(70, int(total_score))
        elif total_score >= 35:
            forecast_3d = "震荡偏空"
            forecast_confidence = min(70, int(100 - total_score))
        else:
            forecast_3d = "大概率下跌"
            forecast_confidence = min(85, int(100 - total_score))

        return {
            "total_score": round(total_score, 1),
            "rating": rating,
            "rating_color": rating_color,
            "trend_score": round(trend_norm, 1),
            "momentum_score": round(momentum_norm, 1),
            "volume_score": round(volume_norm, 1),
            "position_score": round(position_norm, 1),
            "forecast_3d": forecast_3d,
            "forecast_confidence": forecast_confidence,
        }

    # ============================================================
    # 八、操作建议 + 最佳时间窗口
    # ============================================================

    def _generate_advice(self, composite, trend, momentum, levels, holding, current_price) -> dict:
        """生成操作建议和最佳执行时间"""
        score = composite["total_score"]
        pnl_pct = holding.get("pnl_pct", 0) if holding else 0

        # 操作建议
        if score >= 70:
            if holding and pnl_pct > 0:
                action = "持有待涨"
                detail = "趋势向好，继续持有，上移止损保护利润"
            else:
                action = "持有/可加仓"
                detail = "多头趋势明确，可考虑逢低加仓"
            urgency = "低"
        elif score >= 55:
            action = "持有观望"
            detail = "趋势偏多但动量一般，暂不操作，等待确认"
            urgency = "低"
        elif score >= 40:
            if holding and pnl_pct < -5:
                action = "警惕反弹减仓"
                detail = "震荡区间，若反弹至压力位可减仓降低风险"
                urgency = "中"
            else:
                action = "观望等待"
                detail = "方向不明确，等待突破后再操作"
                urgency = "低"
        elif score >= 25:
            if holding:
                action = "逢高减仓"
                detail = "趋势偏空，建议反弹到压力位附近减仓"
                urgency = "高"
            else:
                action = "回避"
                detail = "空头趋势，不宜介入"
                urgency = "中"
        else:
            if holding:
                action = "尽快止损"
                detail = "强烈空头趋势，建议果断止损离场"
                urgency = "紧急"
            else:
                action = "严禁买入"
                detail = "加速下跌阶段，绝对不要抄底"
                urgency = "高"

        # 最佳操作时间窗口
        timing = self._calc_best_timing(trend, momentum, levels, score)

        # 目标价和止损价
        target_price = levels.get("first_resistance", current_price * 1.05)
        stop_price = levels.get("first_support", current_price * 0.95)

        # 如果持仓亏损严重，止损价用买入价-10%
        if holding and pnl_pct < -8:
            buy_price = holding.get("buy_price", current_price)
            stop_price = max(stop_price, buy_price * 0.90)

        # V7.1: 计算置信度百分比
        # 基于评分强度 + 趋势/动量一致性
        score_strength = abs(score - 50) / 50  # 0~1
        trend_momentum_agree = 1 if (trend["score"] > 0 and momentum["score"] > 0) or \
                                    (trend["score"] < 0 and momentum["score"] < 0) else 0.5
        confidence_pct = int(50 + score_strength * 35 + trend_momentum_agree * 10)
        confidence_pct = max(30, min(95, confidence_pct))

        # V7.1: 结构化操作建议
        action_ratio = ""  # 减仓比例
        if "减仓" in action:
            if score < 25:
                action_ratio = "全部"
            elif score < 35:
                action_ratio = "1/2"
            else:
                action_ratio = "1/3"

        return {
            "action": action,
            "detail": detail,
            "urgency": urgency,
            "timing": timing,
            "target_price": round(target_price, 2),
            "stop_price": round(stop_price, 2),
            "risk_reward_ratio": round(
                abs(target_price - current_price) / max(abs(current_price - stop_price), 0.01), 1
            ),
            # V7.1: 新增结构化字段
            "confidence_pct": confidence_pct,
            "action_ratio": action_ratio,
            "key_price": round(stop_price, 2),  # 关键价位(止损价)
            "structured_advice": f"{action}({confidence_pct}%)" + (f" 减{action_ratio}" if action_ratio else ""),
        }

    def _calc_best_timing(self, trend, momentum, levels, score) -> dict:
        """
        计算最佳操作时间窗口
        基于:
        - 趋势强度决定紧迫性
        - RSI超买超卖决定等待还是立即
        - 波动率决定盘中还是盘前
        """
        rsi = momentum.get("rsi", 50)
        vol_trend = ""  # 从volatility获取
        urgency_level = "常规"

        # 时间建议
        if score <= 25:
            # 紧急止损 - 明天开盘就操作
            best_time = "明日09:30-09:45"
            period = "盘前集合竞价挂单"
            reason = "空头趋势明确，越早止损损失越小"
            urgency_level = "紧急"
        elif score <= 35:
            # 逢高减仓 - 等反弹
            if rsi < 40:
                best_time = "明日10:00-10:30"
                period = "盘中反弹时段"
                reason = "等待超卖反弹后减仓，可获得更好价格"
            else:
                best_time = "明日09:45-10:15"
                period = "早盘冲高时段"
                reason = "利用早盘惯性冲高减仓"
            urgency_level = "较急"
        elif score >= 70:
            # 强势持有 - 无需操作
            best_time = "暂不操作"
            period = "持续跟踪"
            reason = "趋势向好，耐心持有，关注止盈位即可"
        elif score >= 55:
            # 偏多 - 可逢低加仓
            dist_support = levels.get("distance_to_support_pct", 3)
            if dist_support < 2:
                best_time = "明日09:30-10:00"
                period = "接近支撑位时"
                reason = "价格接近支撑位，可考虑低吸加仓"
            else:
                best_time = "等待回踩MA20"
                period = "未来2-3日"
                reason = "等价格回踩20日均线附近再加仓，性价比更高"
        else:
            # 震荡 - 观望
            best_time = "暂不操作"
            period = "等待方向选择"
            reason = "震荡区间内不操作，等突破方向明确"

        return {
            "best_time": best_time,
            "period": period,
            "reason": reason,
            "urgency_level": urgency_level,
        }

    # ============================================================
    # 九、批量分析
    # ============================================================

    def batch_analyze(self, data_dict: dict, holdings: dict) -> list:
        """
        批量分析所有持仓股票

        参数:
            data_dict: {code: DataFrame} 含技术指标
            holdings: {code: holding_info}

        返回:
            分析结果列表（按综合评分排序）
        """
        results = []
        for code, holding in holdings.items():
            if code in data_dict:
                df = data_dict[code]
                result = self.analyze_stock(code, df, holding)
                if result.get("valid"):
                    results.append(result)
            else:
                logger.warning(f"  {code} 无行情数据，跳过预测")

        # 按评分排序（低分在前 = 风险高的排前面）
        results.sort(key=lambda x: x["composite"]["total_score"])
        return results

    # ============================================================
    # 十、KDJ计算辅助
    # ============================================================

    def _calc_kdj(self, df: pd.DataFrame, n=9, m1=3, m2=3):
        """计算KDJ指标"""
        try:
            low_n = df["low"].rolling(n).min()
            high_n = df["high"].rolling(n).max()
            rsv = (df["close"] - low_n) / (high_n - low_n) * 100
            rsv = rsv.fillna(50)

            k = rsv.ewm(com=m1 - 1, adjust=False).mean()
            d = k.ewm(com=m2 - 1, adjust=False).mean()
            j = 3 * k - 2 * d

            return k.iloc[-1], d.iloc[-1], j.iloc[-1]
        except Exception:
            return 50, 50, 50


# ============================================================
# 辅助函数
# ============================================================

def current_price_safe(price):
    """防止除零"""
    return price if price > 0 else 1


# ============================================================
# 邮件报告生成
# ============================================================

def send_forecast_email(results: list) -> bool:
    """
    发送持仓趋势预测分析邮件

    参数:
        results: batch_analyze()的返回结果列表
    """
    from notify.email_notify import send_email

    if not results:
        logger.info("  无持仓数据，跳过趋势预测邮件")
        return False

    today = datetime.date.today().strftime("%Y-%m-%d")
    title_prefix = getattr(config, 'FORECAST_EMAIL_TITLE', "持仓趋势预测")
    subject = f"[{title_prefix}] {today} | {len(results)}只持仓分析"

    html = _build_forecast_html(results, today)
    return send_email(subject, html)


def _build_forecast_html(results: list, today: str) -> str:
    """构建趋势预测HTML邮件"""

    # 统计概览
    bullish = sum(1 for r in results if r["composite"]["total_score"] >= 60)
    bearish = sum(1 for r in results if r["composite"]["total_score"] < 40)
    neutral = len(results) - bullish - bearish

    # 紧急操作提醒
    urgent_items = [r for r in results if r["advice"]["urgency"] in ("紧急", "高")]

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 15px; background: #f0f2f5; }}
    .container {{ max-width: 950px; margin: 0 auto; }}
    .header {{ background: linear-gradient(135deg, #2c3e50, #3498db); color: white; padding: 20px 25px; border-radius: 10px 10px 0 0; }}
    .header h1 {{ margin: 0; font-size: 20px; }}
    .header .sub {{ font-size: 12px; opacity: 0.8; margin-top: 5px; }}
    .content {{ background: white; padding: 20px 25px; border-radius: 0 0 10px 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 15px 0; }}
    .summary-box {{ text-align: center; padding: 12px; background: #f8f9fa; border-radius: 8px; }}
    .summary-box .num {{ font-size: 24px; font-weight: bold; }}
    .summary-box .label {{ font-size: 11px; color: #888; margin-top: 3px; }}
    .alert-box {{ background: #fff1f0; border: 2px solid #ff4d4f; border-radius: 8px; padding: 12px 15px; margin: 15px 0; }}
    .alert-box h4 {{ margin: 0 0 8px; color: #ff4d4f; font-size: 14px; }}
    .alert-box p {{ margin: 4px 0; font-size: 13px; }}
    .stock-card {{ border: 1px solid #e8e8e8; border-radius: 8px; margin: 12px 0; overflow: hidden; }}
    .stock-header {{ padding: 10px 15px; display: flex; justify-content: space-between; align-items: center; }}
    .stock-body {{ padding: 12px 15px; font-size: 13px; }}
    .stock-body table {{ width: 100%; border-collapse: collapse; }}
    .stock-body td {{ padding: 5px 8px; border-bottom: 1px solid #f0f0f0; }}
    .stock-body td:first-child {{ color: #888; width: 100px; }}
    .score-badge {{ display: inline-block; padding: 3px 12px; border-radius: 12px; color: white; font-weight: bold; font-size: 13px; }}
    .action-badge {{ display: inline-block; padding: 3px 10px; border-radius: 4px; font-weight: bold; font-size: 12px; }}
    .timing-box {{ background: #e6f7ff; border: 1px solid #91d5ff; border-radius: 6px; padding: 8px 12px; margin-top: 8px; }}
    .timing-box .time {{ font-weight: bold; color: #1890ff; }}
    .footer {{ text-align: center; color: #999; font-size: 11px; margin-top: 15px; padding-top: 10px; border-top: 1px solid #eee; }}
    .up {{ color: #e74c3c; }} .down {{ color: #27ae60; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>持仓趋势预测分析报告</h1>
        <div class="sub">日期: {today} | 持仓: {len(results)}只 | 分析模型: MA+MACD+RSI+KDJ+量价+布林带</div>
    </div>
    <div class="content">
        <div class="summary-grid">
            <div class="summary-box"><div class="num up">{bullish}</div><div class="label">看多</div></div>
            <div class="summary-box"><div class="num" style="color:#95a5a6">{neutral}</div><div class="label">中性</div></div>
            <div class="summary-box"><div class="num down">{bearish}</div><div class="label">看空</div></div>
            <div class="summary-box"><div class="num" style="color:#e74c3c">{len(urgent_items)}</div><div class="label">需紧急操作</div></div>
        </div>
"""

    # 紧急操作提醒
    if urgent_items:
        html += '<div class="alert-box"><h4>!! 紧急操作提醒 !!</h4>'
        for r in urgent_items:
            html += f'<p><b>{r["code"]} {r["name"]}</b>: {r["advice"]["action"]} - {r["advice"]["detail"]} '
            html += f'| 最佳时间: <b>{r["advice"]["timing"]["best_time"]}</b></p>'
        html += '</div>'

    # 每只股票详细分析卡片
    for r in results:
        score = r["composite"]["total_score"]
        rating = r["composite"]["rating"]
        rating_color = r["composite"]["rating_color"]
        trend = r["trend"]
        momentum = r["momentum"]
        levels = r["levels"]
        volatility = r["volatility"]
        volume = r["volume"]
        advice = r["advice"]
        holding = r.get("holding", {})

        # 卡片头部颜色
        if score >= 60:
            header_bg = "#f6ffed"
            border_color = "#b7eb8f"
        elif score >= 40:
            header_bg = "#fffbe6"
            border_color = "#ffe58f"
        else:
            header_bg = "#fff1f0"
            border_color = "#ffa39e"

        # 持仓盈亏
        pnl_html = ""
        if holding:
            pnl_pct = holding.get("pnl_pct", 0)
            pnl_class = "up" if pnl_pct >= 0 else "down"
            pnl_html = f'<span class="{pnl_class}" style="font-weight:bold">{pnl_pct:+.1f}%</span>'

        html += f"""
        <div class="stock-card" style="border-color:{border_color}">
            <div class="stock-header" style="background:{header_bg}">
                <div>
                    <b style="font-size:15px">{r["code"]} {r["name"]}</b>
                    &nbsp; 现价: {r["current_price"]:.2f}
                    &nbsp; 盈亏: {pnl_html}
                </div>
                <div>
                    <span class="score-badge" style="background:{rating_color}">{score:.0f}分 {rating}</span>
                </div>
            </div>
            <div class="stock-body">
                <table>
                    <tr><td>趋势方向</td><td><b>{trend["direction"]}</b> | {trend["ma_pattern"]} | MACD: {trend["macd_signal"]}</td></tr>
                    <tr><td>20日斜率</td><td>{trend["slope_20d_pct"]:+.2f}%/日 | 60日: {trend["slope_60d_pct"]:+.2f}%/日</td></tr>
                    <tr><td>动量状态</td><td>RSI={momentum["rsi"]}({momentum["rsi_state"]}) | KDJ: {momentum["kdj_state"]} | 5日涨幅: {momentum["momentum_5d_pct"]:+.1f}%</td></tr>
                    <tr><td>量价关系</td><td>{volume.get("vp_state", "N/A")} | 量比: {volume.get("vol_ratio", 0):.2f}</td></tr>
                    <tr><td>支撑位</td><td>{levels["first_support"]:.2f} ({levels["first_support_name"]}) | 距支撑: {levels["distance_to_support_pct"]:.1f}%</td></tr>
                    <tr><td>压力位</td><td>{levels["first_resistance"]:.2f} ({levels["first_resistance_name"]}) | 距压力: {levels["distance_to_resistance_pct"]:.1f}%</td></tr>
                    <tr><td>波动预测</td><td>明日区间: {volatility["expected_low"]:.2f} ~ {volatility["expected_high"]:.2f} | ATR: {volatility["atr_pct"]:.1f}%</td></tr>
                    <tr><td>3日预测</td><td><b>{r["composite"]["forecast_3d"]}</b> (置信度: {r["composite"]["forecast_confidence"]}%)</td></tr>
                    <tr><td>操作建议</td><td><b>{advice["action"]}</b> - {advice["detail"]}</td></tr>
                    <tr><td>目标/止损</td><td>目标: {advice["target_price"]:.2f} | 止损: {advice["stop_price"]:.2f} | 盈亏比: {advice["risk_reward_ratio"]:.1f}:1</td></tr>
                </table>
                <div class="timing-box">
                    <span class="time">最佳操作时间: {advice["timing"]["best_time"]}</span>
                    &nbsp;({advice["timing"]["period"]})
                    <br><small>{advice["timing"]["reason"]}</small>
                </div>
            </div>
        </div>
"""

    # 定时执行建议
    html += """
        <div style="background:#f6f6f6; border-radius:8px; padding:12px 15px; margin-top:15px;">
            <b>定时分析执行时间建议:</b><br>
            <small>
            - 盘前分析 (08:30): 基于前日收盘数据生成当日操作计划<br>
            - 盘后分析 (15:30): 收盘后更新数据，生成次日趋势预测<br>
            - 紧急股票会在报告顶部红色区域标注，请优先处理
            </small>
        </div>
"""

    html += f"""
        <div class="footer">
            本报告由交易系统自动生成 | 仅供参考，不构成投资建议<br>
            趋势预测基于历史数据和技术指标，不保证未来走势 | 请结合基本面和市场环境综合判断
        </div>
    </div>
</div>
</body>
</html>"""

    return html


# ============================================================
# 独立运行入口
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    from data.data_loader import init_db, load_daily_data
    from strategy.trend_strategy import compute_indicators
    import json

    print("=" * 60)
    print("  持仓趋势预测分析")
    print("=" * 60)

    # 加载持仓
    holdings_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "holdings.json")
    if os.path.exists(holdings_file):
        with open(holdings_file, "r", encoding="utf-8") as f:
            holdings = json.load(f)
    else:
        print("未找到holdings.json")
        sys.exit(1)

    # 加载数据
    conn = init_db()
    data_dict = {}
    for code in holdings:
        df = load_daily_data(code, conn, days=120)
        if not df.empty and len(df) >= 20:
            df = compute_indicators(df)
            data_dict[code] = df

    conn.close()

    # 执行分析
    forecaster = TrendForecaster()
    results = forecaster.batch_analyze(data_dict, holdings)

    # 输出摘要
    print(f"\n分析完成: {len(results)}只股票\n")
    for r in results:
        score = r["composite"]["total_score"]
        print(f"  {r['code']} {r['name']}: {score:.0f}分 [{r['composite']['rating']}] "
              f"| {r['advice']['action']} | 最佳时间: {r['advice']['timing']['best_time']}")

    # 发送邮件
    if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
        ok = send_forecast_email(results)
        print(f"\n邮件发送: {'成功' if ok else '失败'}")
    else:
        print("\n邮箱未配置，跳过邮件发送")
