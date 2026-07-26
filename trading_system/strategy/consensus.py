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
import pandas as pd

logger = logging.getLogger(__name__)


def compute_consensus(code: str, df: pd.DataFrame, holding: dict = None,
                      signal_dict: dict = None, forecast_result: dict = None,
                      manipulation_result: dict = None,
                      capital_flow_result: dict = None) -> dict:
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

    result["components"]["volume_price"] = {
        "score": round(vp_score, 1),
        "direction": vp_direction,
        "weight": "20%",
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
    # 5. 综合评分
    # ============================================================
    total_score = strategy_score + forecast_score + vp_score + flow_score
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
    # 6. 矛盾检测
    # ============================================================
    conflict, conflict_reason = _detect_conflict(
        strategy_direction, forecast_direction, vp_direction, flow_direction,
        strategy_score, forecast_score, signal_dict, forecast_total,
        flow_total
    )
    result["conflict"] = conflict
    result["conflict_reason"] = conflict_reason
    if conflict:
        result["direction"] = "分歧"

    # ============================================================
    # 7. 置信度计算
    # ============================================================
    # 四模块方向一致性越高，置信度越高
    directions = [strategy_direction, forecast_direction, vp_direction, flow_direction]
    bullish_count = sum(1 for d in directions if "多" in d)
    bearish_count = sum(1 for d in directions if "空" in d)

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

    # 信号强度加成
    signal_strength = abs(total_score) / 100
    confidence = int(confidence + signal_strength * 10)
    confidence = max(30, min(95, confidence))
    result["confidence"] = confidence

    # ============================================================
    # 8. 操作建议
    # ============================================================
    action, action_conf = _generate_action(
        total_score, confidence, holding, signal_dict,
        manipulation_result, current_price
    )
    result["action"] = action
    result["action_confidence"] = f"{action_conf}%"

    return result


def _detect_conflict(strategy_dir, forecast_dir, vp_dir, flow_dir,
                     strategy_score, forecast_score,
                     signal_dict, forecast_total, flow_total) -> tuple:
    """检测模块间矛盾"""
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
                    capital_flow_results: dict = None) -> dict:
    """
    批量计算所有持仓股的共识

    参数:
        data_dict: {code: DataFrame}
        holdings: {code: holding_info}
        signals: [(code, signal_dict), ...] 策略信号
        forecast_results: [forecast_result, ...] 预测结果
        manipulation_results: {code: manip_result} 主力分析
        capital_flow_results: {code: flow_result} 资金流分析结果

    返回:
        {code: consensus_result}
    """
    if holdings is None:
        holdings = {}
    if manipulation_results is None:
        manipulation_results = {}
    if capital_flow_results is None:
        capital_flow_results = {}

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

        results[code] = compute_consensus(
            code, df, holding, signal_dict, forecast_result, manip_result,
            capital_flow_result=flow_result
        )

    return results
