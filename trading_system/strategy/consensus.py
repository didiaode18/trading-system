"""
多空共识机制模块 V2.0
======================
统一四份报告（信号明细/趋势预测/量价分析/资金流向）的方向判断，避免矛盾结论

核心逻辑:
  - 趋势策略信号权重 35%（买卖信号最强权重）
  - 趋势预测评分权重 30%（forecast composite score映射）
  - 量价健康度权重 20%（volume-price analysis）
  - 资金流维度权重 15%（capital flow score映射）

输出:
  - 统一方向判断（看多/看空/中性/分歧）
  - 置信度百分比
  - 矛盾检测与原因说明
  - 结构化操作建议

使用方式:
    from strategy.consensus import compute_consensus, batch_consensus
    result = compute_consensus(code, df, holding, signal_dict, forecast_result, capital_flow_result=flow_result)
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_consensus(code: str, df: pd.DataFrame, holding: dict = None,
                      signal_dict: dict = None, forecast_result: dict = None,
                      manipulation_result: dict = None,
                      capital_flow_result: dict = None,
                      weekly_trend: dict = None,
                      sentiment_result: dict = None) -> dict:
    """
    计算单只股票的多空共识

    参数:
        code: 股票代码
        df: 含技术指标的日线DataFrame
        holding: 持仓信息
        signal_dict: trend_strategy.generate_strategy_signal() 的输出
        forecast_result: TrendForecaster.analyze_stock() 的输出
        manipulation_result: AntiManipulationAnalyzer.analyze() 的输出
        capital_flow_result: CapitalFlowAnalyzer.calc_flow_score() 的输出
        weekly_trend: V10.1 周线趋势(check_weekly_trend()返回)
        sentiment_result: V10.1 市场情绪(MarketSentiment.analyze()返回)

    返回:
        {
            "direction": "看多/看空/中性/分歧",
            "score": -100~+100,
            "confidence": 0~100,
            "conflict": bool,
            "conflict_reason": str,
            "action": "持有/减仓/清仓/加仓/观望",
            "action_confidence": "xx%",
            "key_price": float,       # 关键价位
            "components": {...},      # 各模块贡献分
            "trap_warning": str,      # V10.1: 诱多/诱空预警
        }
    """
    result = {
        "direction": "中性",
        "score": 0,
        "confidence": 50,
        "conflict": False,
        "conflict_reason": "",
        "action": "观望",
        "action_confidence": "50%",
        "key_price": 0.0,
        "components": {},
        "trap_warning": "",  # V10.1: 诱多/诱空预警
    }

    if df.empty or len(df) < 10:
        return result

    latest = df.iloc[-1]
    current_price = latest["close"]
    result["key_price"] = current_price

    # ============================================================
    # 1. 趋势策略信号贡献 (-35 ~ +35)
    # ============================================================
    strategy_score = 0
    strategy_direction = "中性"
    if signal_dict:
        if signal_dict.get("sell_signal"):
            strategy_score = -35
            strategy_direction = "看空"
        elif signal_dict.get("buy_signal"):
            strategy_score = 35
            strategy_direction = "看多"
        elif signal_dict.get("add_position"):
            strategy_score = 18
            strategy_direction = "偏多"
        else:
            # 无信号时，根据持仓浮盈判断
            if holding and holding.get("buy_price"):
                pnl = (current_price - holding["buy_price"]) / holding["buy_price"]
                if pnl > 0.05:
                    strategy_score = 9
                    strategy_direction = "偏多"
                elif pnl < -0.05:
                    strategy_score = -9
                    strategy_direction = "偏空"

    result["components"]["strategy"] = {
        "score": strategy_score,
        "direction": strategy_direction,
        "weight": "35%",
    }

    # ============================================================
    # 2. 趋势预测评分贡献 (-30 ~ +30)
    # ============================================================
    forecast_score = 0
    forecast_direction = "中性"
    forecast_total = 50  # 默认中性
    if forecast_result and forecast_result.get("valid", True):
        composite = forecast_result.get("composite", {})
        forecast_total = composite.get("total_score", 50)
        # 将0-100映射到-30~+30
        forecast_score = (forecast_total - 50) / 50 * 30

        if forecast_total >= 65:
            forecast_direction = "看多"
        elif forecast_total >= 55:
            forecast_direction = "偏多"
        elif forecast_total <= 35:
            forecast_direction = "看空"
        elif forecast_total <= 45:
            forecast_direction = "偏空"

    result["components"]["forecast"] = {
        "score": round(forecast_score, 1),
        "direction": forecast_direction,
        "total_score": forecast_total,
        "weight": "30%",
    }

    # ============================================================
    # 3. 量价健康度贡献 (-20 ~ +20)
    # ============================================================
    vp_score = 0
    vp_direction = "中性"
    _trap_warnings = []  # V10.1: 收集诱多/诱空预警

    if manipulation_result:
        manip_score = manipulation_result.get("manipulation_score", 50)
        # 主力评分高=洗盘概率大=看多；低=真破位=看空
        vp_score = (manip_score - 50) / 50 * 20

        if manip_score >= 65:
            vp_direction = "偏多(洗盘)"
        elif manip_score <= 35:
            vp_direction = "偏空(出货)"

        # 诱多/诱空直接影响
        if manipulation_result.get("bull_trap"):
            vp_score = min(vp_score, -12)
            vp_direction = "诱多风险"
        if manipulation_result.get("bear_trap"):
            vp_score = max(vp_score, 12)
            vp_direction = "诱空机会"

        # V10.1: 早期征兆预警(从anti_manipulation获取)
        if manipulation_result.get("early_bull_trap"):
            _trap_warnings.append("⚠️诱多早期征兆: " + manipulation_result.get("early_bull_trap_detail", ""))
            vp_score = min(vp_score, -8)
        if manipulation_result.get("early_bear_trap"):
            _trap_warnings.append("⚠️诱空早期征兆: " + manipulation_result.get("early_bear_trap_detail", ""))
            vp_score = max(vp_score, 8)
    else:
        # 简单量价分析
        if len(df) >= 5:
            vol_ma = df["volume"].tail(20).mean() if len(df) >= 20 else df["volume"].mean()
            recent_vol = df["volume"].tail(3).mean()
            price_change = (df["close"].iloc[-1] - df["close"].iloc[-3]) / df["close"].iloc[-3] if df["close"].iloc[-3] > 0 else 0

            if price_change > 0 and recent_vol > vol_ma * 1.2:
                vp_score = 8
                vp_direction = "量价配合"
            elif price_change < 0 and recent_vol < vol_ma * 0.7:
                vp_score = 8
                vp_direction = "缩量回调"
            elif price_change < 0 and recent_vol > vol_ma * 1.3:
                vp_score = -12
                vp_direction = "放量下跌"

    # ---- V10.1增强: OBV背离斜率 ----
    _obv_divergence = _calc_obv_divergence(df)
    if _obv_divergence is not None:
        # OBV顶背离: 价格涨但OBV不涨 → 诱多先行
        if _obv_divergence > 0.05:
            vp_score = min(vp_score, -6)
            vp_direction = "OBV顶背离(诱多先行)" if "诱多" not in vp_direction else vp_direction
            _trap_warnings.append(f"OBV顶背离: 价格涨但量能未跟随(偏离{_obv_divergence*100:.1f}%)")
        # OBV底背离: 价格跌但OBV不跌 → 诱空先行
        elif _obv_divergence < -0.05:
            vp_score = max(vp_score, 6)
            vp_direction = "OBV底背离(诱空先行)" if "诱空" not in vp_direction else vp_direction
            _trap_warnings.append(f"OBV底背离: 价格跌但量能未跟随(偏离{_obv_divergence*100:.1f}%)")

    # ---- V10.1增强: 量价弹性系数 ----
    _vp_elasticity = _calc_volume_price_elasticity(df)
    if _vp_elasticity is not None:
        if _vp_elasticity < 0.3:
            # 弹性极低: 放量也推不动价格 → 诱多环境(上方抛压重)
            vp_score = min(vp_score, -4)
            _trap_warnings.append(f"量价弹性极低({_vp_elasticity:.2f}): 放量推不动价格")
        elif _vp_elasticity > 3.0:
            # 弹性极高: 缩量也能涨 → 惜售/控盘度高(可能是诱空后吸筹完成)
            vp_score = max(vp_score, 4)

    result["components"]["volume_price"] = {
        "score": round(vp_score, 1),
        "direction": vp_direction,
        "weight": "20%",
        "obv_divergence": round(_obv_divergence, 4) if _obv_divergence is not None else None,
        "vp_elasticity": round(_vp_elasticity, 2) if _vp_elasticity is not None else None,
    }

    # ============================================================
    # 4. 资金流维度贡献 (-15 ~ +15)
    # ============================================================
    flow_score = 0
    flow_direction = "中性"
    flow_total = 50  # 默认中性
    if capital_flow_result:
        flow_total = capital_flow_result.get("total_score", 50)
        # 将0-100映射到-15~+15
        flow_score = (flow_total - 50) / 50 * 15

        if flow_total >= 70:
            flow_direction = "看多(资金流入)"
        elif flow_total >= 55:
            flow_direction = "偏多(资金温和)"
        elif flow_total <= 30:
            flow_direction = "看空(资金流出)"
        elif flow_total <= 45:
            flow_direction = "偏空(资金偏弱)"

    result["components"]["capital_flow"] = {
        "score": round(flow_score, 1),
        "direction": flow_direction,
        "total_score": flow_total,
        "weight": "15%",
    }

    # ============================================================
    # 5. V10.1: 市场情绪逆向指标 (-5 ~ +5)
    # ============================================================
    sentiment_score = 0
    sentiment_direction = "中性"
    if sentiment_result:
        sentiment_total = sentiment_result.get("sentiment_score", 50)
        # 将0-100映射到-5~+5 (逆向: 极端乐观→看空, 极端悲观→看多)
        sentiment_score = (sentiment_total - 50) / 50 * -5  # 注意取反

        if sentiment_total >= 80:
            sentiment_direction = "极端乐观(逆向看空)"
            _trap_warnings.append("市场情绪极端乐观，注意诱多风险")
        elif sentiment_total >= 65:
            sentiment_direction = "偏乐观"
        elif sentiment_total <= 20:
            sentiment_direction = "极端悲观(逆向看多)"
            _trap_warnings.append("市场情绪极端悲观，注意诱空机会")
        elif sentiment_total <= 35:
            sentiment_direction = "偏悲观"

    result["components"]["sentiment"] = {
        "score": round(sentiment_score, 1),
        "direction": sentiment_direction,
        "weight": "5%",
    }

    # ============================================================
    # 6. 综合评分
    # ============================================================
    total_score = strategy_score + forecast_score + vp_score + flow_score + sentiment_score
    total_score = max(-100, min(100, total_score))
    result["score"] = round(total_score, 1)

    # 方向判定
    if total_score >= 30:
        result["direction"] = "看多"
    elif total_score >= 10:
        result["direction"] = "偏多"
    elif total_score <= -30:
        result["direction"] = "看空"
    elif total_score <= -10:
        result["direction"] = "偏空"
    else:
        result["direction"] = "中性"

    # ============================================================
    # 7. 矛盾检测（V10.1: 新增多时间框架矛盾）
    # ============================================================
    conflict, conflict_reason = _detect_conflict(
        strategy_direction, forecast_direction, vp_direction, flow_direction,
        strategy_score, forecast_score, signal_dict, forecast_total,
        flow_total, weekly_trend=weekly_trend,
    )
    result["conflict"] = conflict
    result["conflict_reason"] = conflict_reason
    if conflict:
        result["direction"] = "分歧"

    # V10.1: 写入诱多/诱空预警
    if _trap_warnings:
        result["trap_warning"] = " | ".join(_trap_warnings[:3])

    # ============================================================
    # 8. 置信度计算
    # ============================================================
    # V9.2: 引入历史准确率加权（各模块历史预测表现影响置信度）
    directions = [strategy_direction, forecast_direction, vp_direction, flow_direction]
    module_names = ["trend_forecast", "trend_forecast", "trend_forecast", "consensus"]
    bullish_count = sum(1 for d in directions if "多" in d)
    bearish_count = sum(1 for d in directions if "空" in d)

    # 基础置信度（一致性计数）
    if bullish_count == 4 or bearish_count == 4:
        confidence = 90  # 四模块一致
    elif bullish_count == 3 or bearish_count == 3:
        confidence = 80  # 三模块一致
    elif bullish_count == 2 or bearish_count == 2:
        confidence = 65  # 两模块一致
    elif conflict:
        confidence = 40  # 存在矛盾
    else:
        confidence = 55  # 中性

    # V9.2: 历史准确率校准加成
    try:
        from monitor.prediction_tracker import PredictionTracker
        _pt = PredictionTracker()
        _acc_report = _pt.get_accuracy_report()
        _by_source = _acc_report.get("by_source", {})
        # 各模块历史准确率
        _forecast_acc = _by_source.get("trend_forecast", {}).get("accuracy", 0.5)
        _consensus_acc = _by_source.get("consensus", {}).get("accuracy", 0.5)
        # 校准: 历史准确率高时提升置信度，低时降低
        _avg_acc = (_forecast_acc + _consensus_acc) / 2 if (_forecast_acc + _consensus_acc) > 0 else 0.5
        _calibration_bonus = (_avg_acc - 0.5) * 20  # 准确率60%→+2, 40%→-2, 70%→+4
        confidence = int(confidence + _calibration_bonus)
    except Exception:
        pass  # 无历史数据时使用原始置信度

    # 信号强度加成
    signal_strength = abs(total_score) / 100
    confidence = int(confidence + signal_strength * 10)
    confidence = max(30, min(95, confidence))
    result["confidence"] = confidence

    # ============================================================
    # 9. 操作建议
    # ============================================================
    action, action_conf = _generate_action(
        total_score, confidence, holding, signal_dict,
        manipulation_result, current_price
    )
    result["action"] = action
    result["action_confidence"] = f"{action_conf}%"

    # ============================================================
    # 10. V9.2: 共识预测接入PredictionTracker（打通预测验证闭环）
    # ============================================================
    try:
        from monitor.prediction_tracker import PredictionTracker
        _tracker = PredictionTracker()
        # 方向映射: 看多/偏多 → "偏多", 看空/偏空 → "偏空", 分歧/中性 → "中性"
        _dir = result.get("direction", "中性")
        if "多" in _dir:
            _pt_dir = "偏多"
        elif "空" in _dir:
            _pt_dir = "偏空"
        else:
            _pt_dir = "中性"
        _tracker.record_forecast(
            code=code,
            direction=_pt_dir,
            confidence=confidence,
            composite_score=total_score,
            source="consensus",
            extra={"action": action, "conflict": result.get("conflict", False)},
        )
    except Exception:
        pass  # 持久化失败不影响主流程

    return result


def _calc_obv_divergence(df: pd.DataFrame) -> float:
    """V10.1: 计算OBV-价格背离斜率

    返回: price_slope - obv_slope
      > 0.05: 顶背离(价格涨但OBV不涨, 诱多先行)
      < -0.05: 底背离(价格跌但OBV不跌, 诱空先行)
      None: 数据不足或OBV基数为0，不可用
    """
    if len(df) < 20:
        return None
    close = df["close"].values
    volume = df["volume"].values

    # 计算OBV
    obv = np.zeros(len(close))
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]

    # 20日斜率
    lookback = min(20, len(df) - 1)
    price_base = close[-lookback - 1] if close[-lookback - 1] > 0 else None
    obv_base = abs(obv[-lookback - 1]) if obv[-lookback - 1] != 0 else None

    # W3修复: 基数为0时返回None，避免虚假背离值
    if price_base is None or obv_base is None:
        return None

    price_slope = (close[-1] - close[-lookback - 1]) / price_base
    obv_slope = (obv[-1] - obv[-lookback - 1]) / obv_base

    return price_slope - obv_slope


def _calc_volume_price_elasticity(df: pd.DataFrame) -> float:
    """V10.1: 计算量价弹性系数

    弹性 = 近5日价格涨幅 / 近5日成交量变化率
    弹性低(<0.3): 放量也推不动价格 → 上方抛压重(诱多环境)
    弹性高(>3.0): 缩量也能涨 → 惜售/控盘度高
    """
    if len(df) < 10:
        return None

    close = df["close"].values
    volume = df["volume"].values

    # 近5日价格涨幅
    price_change_5d = (close[-1] - close[-6]) / close[-6] if close[-6] > 0 else 0

    # 近5日成交量变化率
    vol_5d_recent = np.mean(volume[-5:])
    vol_5d_prev = np.mean(volume[-10:-5])
    vol_change = (vol_5d_recent - vol_5d_prev) / vol_5d_prev if vol_5d_prev > 0 else 0

    if abs(vol_change) < 0.01:  # 成交量几乎不变
        return None

    elasticity = price_change_5d / vol_change
    return elasticity


def _detect_conflict(strategy_dir, forecast_dir, vp_dir, flow_dir,
                     strategy_score, forecast_score,
                     signal_dict, forecast_total, flow_total,
                     weekly_trend=None) -> tuple:
    """检测模块间矛盾 + V10.1多时间框架矛盾"""
    # C2修复: V10.1 日线/周线矛盾检测提到最前面（最高优先级）
    # 原先放在末尾会被前面5个 return True 分支永久遮蔽，永远无法触发
    if weekly_trend and signal_dict:
        weekly_dir = weekly_trend.get("trend", "unknown")
        if signal_dict.get("buy_signal") and weekly_dir == "down":
            return True, "日线买入信号但周线下跌，假突破风险高(多时间框架矛盾)"
        if signal_dict.get("sell_signal") and weekly_dir == "up":
            return True, "日线卖出信号但周线仍上升，洗盘概率大(多时间框架矛盾)"

    # 策略说卖但预测看多
    if signal_dict and signal_dict.get("sell_signal") and forecast_total > 60:
        return True, f"短期止损触发但中期趋势评分{forecast_total:.0f}分(偏多)，建议区分短/中期操作"

    # 策略说买但预测看空
    if signal_dict and signal_dict.get("buy_signal") and forecast_total < 40:
        return True, f"买入信号触发但趋势评分仅{forecast_total:.0f}分(偏空)，注意假突破风险"

    # 策略和预测方向完全相反
    if "空" in strategy_dir and "多" in forecast_dir and abs(strategy_score) > 20 and abs(forecast_score) > 20:
        return True, "策略信号(空)与趋势预测(多)方向相反，短期压力vs中期向好"

    if "多" in strategy_dir and "空" in forecast_dir and abs(strategy_score) > 20 and abs(forecast_score) > 20:
        return True, "策略信号(多)与趋势预测(空)方向相反，短期反弹vs中期走弱"

    # 策略看多但资金流看空（可能假突破）
    if "多" in strategy_dir and "空" in flow_dir and abs(strategy_score) > 15 and flow_total < 40:
        return True, f"策略信号(多)与资金流(空，{flow_total:.0f}分)方向相反，注意资金不配合"

    # 策略看空但资金流看多（可能洗盘）
    if "空" in strategy_dir and "多" in flow_dir and abs(strategy_score) > 15 and flow_total > 65:
        return True, f"策略信号(空)与资金流(多，{flow_total:.0f}分)方向相反，可能为洗盘动作"

    return False, ""


def _generate_action(total_score, confidence, holding, signal_dict,
                     manipulation_result, current_price) -> tuple:
    """生成结构化操作建议"""
    # 强制卖出信号优先
    if signal_dict and signal_dict.get("sell_signal"):
        reason = signal_dict.get("signal_reason", "")
        if "强制" in reason or "放量大跌" in reason:
            return "清仓", max(confidence, 85)
        # 检查是否疑似洗盘
        if manipulation_result and manipulation_result.get("wash_trading"):
            manip_score = manipulation_result.get("manipulation_score", 0)
            if manip_score >= 65:
                return "持有(疑似洗盘)", max(confidence - 10, 50)
        return "减仓", max(confidence, 70)

    # 有持仓
    if holding and holding.get("buy_price"):
        buy_price = holding["buy_price"]
        pnl_pct = (current_price - buy_price) / buy_price * 100

        if total_score >= 30:
            if pnl_pct > 15:
                return "持有(可部分止盈)", confidence
            return "持有", confidence
        elif total_score >= 10:
            return "持有观望", confidence
        elif total_score >= -10:
            if pnl_pct < -5:
                return "警惕(设好止损)", confidence
            return "持有观望", confidence
        elif total_score >= -30:
            return "减仓1/3", confidence
        else:
            return "清仓", confidence

    # 无持仓
    if signal_dict and signal_dict.get("buy_signal"):
        if total_score >= 20:
            return "可买入", confidence
        else:
            return "观望(信号弱)", confidence

    if total_score >= 40:
        return "关注(偏多)", confidence
    elif total_score <= -30:
        return "回避", confidence
    else:
        return "观望", confidence


def batch_consensus(data_dict: dict, holdings: dict = None,
                    signals: list = None, forecast_results: list = None,
                    manipulation_results: dict = None,
                    capital_flow_results: dict = None,
                    weekly_trends: dict = None,
                    sentiment_result: dict = None) -> dict:
    """
    批量计算所有持仓股的共识

    参数:
        data_dict: {code: DataFrame}
        holdings: {code: holding_info}
        signals: [(code, signal_dict), ...] 策略信号
        forecast_results: [forecast_result, ...] 预测结果
        manipulation_results: {code: manip_result} 主力分析
        capital_flow_results: {code: flow_result} 资金流分析结果
        weekly_trends: V10.1 {code: weekly_trend_result} 周线趋势
        sentiment_result: V10.1 市场情绪分析结果(全局共用)

    返回:
        {code: consensus_result}
    """
    if holdings is None:
        holdings = {}
    if manipulation_results is None:
        manipulation_results = {}
    if capital_flow_results is None:
        capital_flow_results = {}
    if weekly_trends is None:
        weekly_trends = {}

    # 构建信号字典映射
    signal_map = {}
    if signals:
        for code, sig in signals:
            signal_map[code] = sig

    # 构建预测结果映射
    forecast_map = {}
    if forecast_results:
        for r in forecast_results:
            if r.get("code"):
                forecast_map[r["code"]] = r

    results = {}
    # 只对持仓股计算共识
    target_codes = set(holdings.keys())
    if signals:
        target_codes.update(code for code, _ in signals)

    for code in target_codes:
        df = data_dict.get(code)
        if df is None or df.empty:
            continue

        holding = holdings.get(code)
        signal_dict = signal_map.get(code)
        forecast_result = forecast_map.get(code)
        manip_result = manipulation_results.get(code)
        flow_result = capital_flow_results.get(code)
        weekly = weekly_trends.get(code)

        results[code] = compute_consensus(
            code, df, holding, signal_dict, forecast_result, manip_result,
            capital_flow_result=flow_result,
            weekly_trend=weekly,
            sentiment_result=sentiment_result,
        )

    return results
