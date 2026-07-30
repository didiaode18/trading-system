"""
反主力操控分析模块 V2.0
========================
识别主力洗盘、诱多、诱空等操控行为，辅助止损/止盈决策

核心检测:
  1. 量价背离识别（洗盘 vs 出货）—— ATR自适应窗口(5-10天)
  2. 洗盘特征检测 —— ATR自适应窗口(7-14天)
  3. 诱多/诱空陷阱检测
  4. 主力行为评分（0-100）
  5. 评分影响止损阈值 —— 基于ATR动态倍数
  6. 对倒骗线检测 —— 量能阈值自适应
  7. 大单净流入异常检测（压盘吸筹识别）

使用方式:
    from strategy.anti_manipulation import AntiManipulationAnalyzer
    analyzer = AntiManipulationAnalyzer()
    result = analyzer.analyze(code, df, holding)
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class AntiManipulationAnalyzer:
    """主力行为识别与反操控分析"""

    def __init__(self):
        # 检测参数
        self.volume_spike_ratio = 2.0       # 放量判定: 量>均量2倍
        self.volume_shrink_ratio = 0.5      # 缩量判定: 量<均量50%
        self.bull_trap_vol_ratio = 0.70     # 诱多: 突破前高但量能<前高70%
        self.bear_trap_recover_days = 3     # 诱空: 跌破支撑后3日内收回
        self.wash_score_threshold = 60      # 洗盘判定阈值
        self.atr_amplitude_ratio = 2.0      # 异常振幅: >2倍ATR

    def analyze(self, code: str, df: pd.DataFrame, holding: dict = None) -> dict:
        """
        对单只股票进行主力行为分析

        参数:
            code: 股票代码
            df: 含技术指标的日线DataFrame
            holding: 持仓信息（可选）

        返回:
            {
                "manipulation_score": 0-100,  # 主力操控概率
                "wash_trading": bool,         # 疑似洗盘
                "bull_trap": bool,            # 疑似诱多
                "bear_trap": bool,            # 疑似诱空
                "volume_price_state": str,    # 量价关系状态
                "suggestion": str,            # 操作建议
                "confidence": float,          # 置信度(0-1)
                "detail": str,                # 详细分析说明
                "stop_loss_adjust": float,    # 止损调整幅度(正=放宽)
            }
        """
        result = {
            "manipulation_score": 50,
            "wash_trading": False,
            "bull_trap": False,
            "bear_trap": False,
            "volume_price_state": "正常",
            "suggestion": "正常操作",
            "confidence": 0.5,
            "detail": "",
            "stop_loss_adjust": 0.0,
        }

        if df.empty or len(df) < 20:
            result["detail"] = "数据不足，无法分析"
            return result

        details = []
        score = 50  # 基础分50（中性）

        # ---- 1. 量价背离分析 ----
        vp_state, vp_score, vp_detail = self._analyze_volume_price(df)
        result["volume_price_state"] = vp_state
        score += vp_score
        details.append(vp_detail)

        # ---- 2. 洗盘特征检测 ----
        wash_score, wash_detail = self._detect_wash_trading(df)
        if wash_score > 0:
            score += wash_score
            details.append(wash_detail)
            if score >= self.wash_score_threshold:
                result["wash_trading"] = True

        # ---- 3. 诱多陷阱检测 ----
        bull_trap, bt_detail = self._detect_bull_trap(df)
        result["bull_trap"] = bull_trap
        if bull_trap:
            score -= 15  # 诱多=看空信号
            details.append(bt_detail)

        # ---- 4. 诱空陷阱检测 ----
        bear_trap, bear_detail = self._detect_bear_trap(df)
        result["bear_trap"] = bear_trap
        if bear_trap:
            score += 15  # 诱空=看多信号
            details.append(bear_detail)

        # ---- 5. 异常波动检测 ----
        abnormal_score, abn_detail = self._detect_abnormal_volatility(df)
        score += abnormal_score
        if abnormal_score != 0:
            details.append(abn_detail)

        # ---- 6. 连续K线形态 ----
        pattern_score, pat_detail = self._detect_kline_pattern(df)
        score += pattern_score
        if pattern_score != 0:
            details.append(pat_detail)

        # ---- 7. 大单净流入异常检测（压盘吸筹） ----
        accum_result = self.detect_accumulation_anomaly(df)
        if accum_result.get("is_accumulation"):
            score += 15  # 压盘吸筹 → 洗盘概率增加15分
            details.append(accum_result["description"])
            logger.info(f"[{code}] 检测到压盘吸筹信号(置信度{accum_result['confidence']:.2f}, 持续{accum_result['days']}天)")

        # 限制评分范围
        score = max(0, min(100, score))
        result["manipulation_score"] = score

        # ---- 生成建议 ----
        result.update(self._generate_suggestion(score, result, holding, df))
        result["detail"] = " | ".join(details[:4])  # 最多4条

        # 置信度：基于数据量和信号强度
        signal_strength = abs(score - 50) / 50  # 0~1
        data_confidence = min(1.0, len(df) / 60)  # 60天以上数据满分
        result["confidence"] = round(signal_strength * 0.6 + data_confidence * 0.4, 2)

        return result

    # ============================================================
    # ATR计算与动态窗口辅助方法
    # ============================================================

    def _compute_atr(self, df: pd.DataFrame, period: int = 20) -> float:
        """
        计算N日ATR（Average True Range）
        使用标准公式: True Range的N日移动平均
        """
        if len(df) < period + 1:
            period = max(1, len(df) - 1)
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        tr_list = []
        start_idx = len(highs) - period
        for i in range(max(1, start_idx), len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_list.append(tr)
        return np.mean(tr_list) if tr_list else 0.0

    def _get_dynamic_window(self, df: pd.DataFrame, min_window: int, max_window: int,
                            low_vol_pct: float = 0.015, high_vol_pct: float = 0.03) -> int:
        """
        根据ATR/price比率动态计算检测窗口
        - ATR/price > high_vol_pct → max_window（高波动用大窗口）
        - ATR/price < low_vol_pct  → min_window（低波动用小窗口）
        - 中间线性插值
        """
        atr = self._compute_atr(df, 20)
        price = df["close"].iloc[-1] if not df.empty else 1.0
        if price <= 0:
            return min_window
        atr_pct = atr / price
        if atr_pct >= high_vol_pct:
            return max_window
        elif atr_pct <= low_vol_pct:
            return min_window
        else:
            ratio = (atr_pct - low_vol_pct) / (high_vol_pct - low_vol_pct)
            return int(min_window + ratio * (max_window - min_window))

    def _get_dynamic_vol_threshold(self, df: pd.DataFrame) -> float:
        """
        对倒骗线量能阈值动态化：基于20日成交量均值+2倍标准差
        返回量比阈值（相对均量的倍数），最低1.5
        """
        if len(df) < 20:
            return 1.5
        vol_series = df["volume"].tail(20)
        vol_ma20 = vol_series.mean()
        vol_std20 = vol_series.std()
        if vol_ma20 <= 0:
            return 1.5
        vol_threshold = vol_ma20 + 2 * vol_std20
        return max(1.5, vol_threshold / vol_ma20)

    # ============================================================
    # 量价背离分析（ATR自适应窗口）
    # ============================================================

    def _analyze_volume_price(self, df: pd.DataFrame) -> tuple:
        """
        量价关系分析（窗口根据ATR动态调整5-10天）:
        - 价跌量缩 = 洗盘概率大 (+20)
        - 价跌量增 = 真出货 (-15)
        - 价涨量缩 = 诱多嫌疑 (-10)
        - 价涨量增 = 健康上涨 (+5)
        """
        if len(df) < 5:
            return "数据不足", 0, ""

        # ATR动态窗口: 高波动10天，低波动5天
        window = self._get_dynamic_window(df, min_window=5, max_window=10)
        window = min(window, len(df))
        recent = df.tail(window)
        logger.debug(f"量价背离分析窗口={window}天(ATR自适应)")

        price_change = (recent["close"].iloc[-1] - recent["close"].iloc[0]) / recent["close"].iloc[0]

        # 成交量均值对比
        vol_ma20 = df["volume"].tail(20).mean() if len(df) >= 20 else df["volume"].mean()
        recent_vol_avg = recent["volume"].mean()
        vol_ratio = recent_vol_avg / vol_ma20 if vol_ma20 > 0 else 1.0

        # 动态量能阈值（对倒骗线检测）
        dyn_vol_threshold = self._get_dynamic_vol_threshold(df)

        if price_change < -0.02:  # 价格下跌>2%
            if vol_ratio < self.volume_shrink_ratio:
                state = "缩量回调"
                score = 20
                detail = f"价跌量缩({window}日量比{vol_ratio:.2f})，洗盘概率大"
            elif vol_ratio > dyn_vol_threshold:
                state = "放量下跌"
                score = -15
                detail = f"价跌量增({window}日量比{vol_ratio:.2f}>阈值{dyn_vol_threshold:.2f})，出货嫌疑"
            else:
                state = "放量下跌"
                score = -10
                detail = f"价跌量增({window}日量比{vol_ratio:.2f})，偏空"
        elif price_change > 0.02:  # 价格上涨>2%
            if vol_ratio < self.volume_shrink_ratio:
                state = "缩量上涨"
                score = -10
                detail = f"价涨量缩({window}日量比{vol_ratio:.2f})，诱多嫌疑"
            else:
                state = "放量上涨"
                score = 5
                detail = f"价涨量增({window}日量比{vol_ratio:.2f})，健康上涨"
        else:
            state = "量价平稳"
            score = 0
            detail = ""

        return state, score, detail

    # ============================================================
    # 洗盘特征检测
    # ============================================================

    def _detect_wash_trading(self, df: pd.DataFrame) -> tuple:
        """
        洗盘特征检测（窗口根据ATR动态调整7-14天）:
        1. 快速下跌后快速收回（V型/长下影线）
        2. 下跌时缩量，反弹时放量
        3. 连续小阴线后突然大阳
        4. 跌破重要均线后快速收回
        """
        score = 0
        details = []

        if len(df) < 10:
            return 0, ""

        # ATR动态窗口: 高波动14天，低波动7天
        window = self._get_dynamic_window(df, min_window=7, max_window=14)
        window = min(window, len(df))
        logger.debug(f"洗盘特征检测窗口={window}天(ATR自适应)")

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        recent = df.tail(window)

        # 特征1: 长下影线（下影线>实体2倍）
        body = abs(latest["close"] - latest["open"])
        lower_shadow = min(latest["open"], latest["close"]) - latest["low"]
        if body > 0 and lower_shadow > body * 2:
            score += 15
            details.append("长下影线(洗盘特征)")

        # 特征2: 连续小阴线后大阳（使用动态窗口）
        if len(df) >= window:
            small_yin_count = 0
            for i in range(window - 1):
                row = recent.iloc[i]
                if row["close"] < row["open"]:  # 阴线
                    change = (row["open"] - row["close"]) / row["open"]
                    if change < 0.02:  # 小阴线(<2%)
                        small_yin_count += 1
            # 最后一天是大阳线
            last_change = (latest["close"] - latest["open"]) / latest["open"] if latest["open"] > 0 else 0
            yin_threshold = max(3, int(window * 0.6))  # 60%以上为小阴线
            if small_yin_count >= yin_threshold and last_change > 0.03:
                score += 20
                details.append(f"连续小阴后大阳({window}日内{small_yin_count}根小阴,典型洗盘完成)")

        # 特征3: 跌破MA20后快速收回
        if len(df) >= 21 and "ma20" in df.columns:
            ma20 = latest.get("ma20", 0)
            prev_close = prev["close"]
            curr_close = latest["close"]
            if ma20 > 0 and prev_close < ma20 and curr_close > ma20:
                score += 10
                details.append("跌破MA20后收回(诱空洗盘)")

        # 特征4: 急跌后缩量企稳（使用动态窗口）
        if len(df) >= window:
            max_drop = 0
            for i in range(-(window), -1):
                idx = len(df) + i
                if idx > 0:
                    day_change = (df.iloc[idx]["close"] - df.iloc[idx-1]["close"]) / df.iloc[idx-1]["close"]
                    max_drop = min(max_drop, day_change)
            # 有过急跌(>3%)但最近企稳
            last_change = (latest["close"] - prev["close"]) / prev["close"] if prev["close"] > 0 else 0
            if max_drop < -0.03 and abs(last_change) < 0.01:
                vol_ratio = latest["volume"] / df["volume"].tail(20).mean() if df["volume"].tail(20).mean() > 0 else 1
                if vol_ratio < 0.6:
                    score += 10
                    details.append(f"急跌后缩量企稳({window}日内最大跌幅{max_drop:.1%},洗盘尾声)")

        detail_str = "+".join(details) if details else ""
        return score, detail_str

    # ============================================================
    # 诱多陷阱检测
    # ============================================================

    def _detect_bull_trap(self, df: pd.DataFrame) -> tuple:
        """
        诱多特征:
        1. 突破前高但量能不足(<前高成交量70%)
        2. 涨停板打开后放量（出货）
        3. 高位放量长上影线
        """
        if len(df) < 20:
            return False, ""

        latest = df.iloc[-1]
        close = latest["close"]
        volume = latest["volume"]

        # 检测1: 突破前高但量能不足
        high_20d = df["high"].iloc[-21:-1].max()  # 前20日最高价
        if close > high_20d:
            # 找到前高那天的成交量
            high_idx = df["high"].iloc[-21:-1].idxmax()
            high_day_vol = df.loc[high_idx, "volume"] if high_idx in df.index else volume
            if high_day_vol > 0 and volume < high_day_vol * self.bull_trap_vol_ratio:
                return True, f"突破前高{high_20d:.2f}但量能仅{volume/high_day_vol:.0%}(诱多)"

        # 动态量能阈值（基于个股流动性自适应）
        dyn_vol_threshold = self._get_dynamic_vol_threshold(df)

        # 检测2: 高位放量长上影线
        if len(df) >= 20:
            ma20 = latest.get("ma20", close)
            if ma20 > 0 and close > ma20 * 1.10:  # 高于MA20 10%以上
                upper_shadow = latest["high"] - max(latest["close"], latest["open"])
                body = abs(latest["close"] - latest["open"])
                vol_ma = df["volume"].tail(20).mean()
                if body > 0 and upper_shadow > body * 2 and volume > vol_ma * dyn_vol_threshold:
                    return True, f"高位放量长上影(量比>{dyn_vol_threshold:.2f}动态阈值,诱多出货)"

        return False, ""

    # ============================================================
    # 诱空陷阱检测
    # ============================================================

    def _detect_bear_trap(self, df: pd.DataFrame) -> tuple:
        """
        诱空特征:
        1. 跌破支撑后3日内快速收回
        2. 跌停板打开后缩量（恐慌盘释放完毕）
        3. 低位放量长下影线
        """
        if len(df) < 20:
            return False, ""

        latest = df.iloc[-1]
        close = latest["close"]
        volume = latest["volume"]  # V7.1: 修复未定义变量

        # 检测1: 跌破MA20后3日内收回
        if "ma20" in df.columns and len(df) >= 23:
            ma20_series = df["ma20"].iloc[-5:]
            close_series = df["close"].iloc[-5:]
            # 3天前跌破MA20
            if len(ma20_series) >= 4:
                was_below = close_series.iloc[-4] < ma20_series.iloc[-4] if not pd.isna(ma20_series.iloc[-4]) else False
                now_above = close > latest.get("ma20", close)
                if was_below and now_above:
                    return True, "跌破MA20后3日内收回(诱空)"

        # 检测2: 低位放量长下影线
        if len(df) >= 20:
            ma20 = latest.get("ma20", close)
            if ma20 > 0 and close < ma20 * 0.95:  # 低于MA20 5%以上
                lower_shadow = min(latest["close"], latest["open"]) - latest["low"]
                body = abs(latest["close"] - latest["open"])
                vol_ma = df["volume"].tail(20).mean()
                if body > 0 and lower_shadow > body * 2 and volume > vol_ma * 1.3:
                    return True, "低位放量长下影(诱空吸筹)"

        return False, ""

    # ============================================================
    # 异常波动检测
    # ============================================================

    def _detect_abnormal_volatility(self, df: pd.DataFrame) -> tuple:
        """
        异常波动:
        - 日内振幅 > 2*ATR → 主力操控嫌疑
        - 尾盘异动（最后30分钟成交量占比>30%，用日成交量突变推断）
        """
        score = 0
        details = []

        if len(df) < 14:
            return 0, ""

        latest = df.iloc[-1]

        # 计算ATR(14) - 优先使用统一方法
        if "atr" in df.columns and not pd.isna(latest.get("atr", np.nan)):
            atr = latest.get("atr", 0)
        else:
            atr = self._compute_atr(df, 14)

        # 日内振幅
        amplitude = latest["high"] - latest["low"]
        if atr > 0 and amplitude > atr * self.atr_amplitude_ratio:
            score += 10
            details.append(f"振幅异常({amplitude/atr:.1f}倍ATR)")

        # 成交量突变（今日量>5日均量2倍）
        vol_ma5 = df["volume"].tail(6).iloc[:-1].mean() if len(df) >= 6 else 0
        if vol_ma5 > 0 and latest["volume"] > vol_ma5 * 2.5:
            score += 5
            details.append("成交量突变(>2.5倍5日均量)")

        detail_str = "+".join(details) if details else ""
        return score, detail_str

    # ============================================================
    # K线形态检测
    # ============================================================

    def _detect_kline_pattern(self, df: pd.DataFrame) -> tuple:
        """
        典型主力操控K线形态:
        - 连续小阴线后突然大阳（洗盘完成）: +15
        - 高位十字星+放量（变盘信号）: -5
        - 地天板/大幅V反（极端洗盘）: +25
        """
        score = 0
        details = []

        if len(df) < 5:
            return 0, ""

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # 地天板检测: 从跌停到涨停（或接近）
        if prev["close"] > 0:
            day_range = (latest["high"] - latest["low"]) / prev["close"]
            if day_range > 0.15:  # 日内振幅>15%
                # 收盘在高位
                close_position = (latest["close"] - latest["low"]) / (latest["high"] - latest["low"]) if latest["high"] > latest["low"] else 0.5
                if close_position > 0.8:
                    score += 25
                    details.append("地天板/大幅V反(极端洗盘)")

        # 高位十字星（量能阈值动态化）
        if len(df) >= 20:
            ma20 = latest.get("ma20", latest["close"])
            body = abs(latest["close"] - latest["open"])
            total_range = latest["high"] - latest["low"]
            dyn_vol_threshold = self._get_dynamic_vol_threshold(df)
            if ma20 > 0 and latest["close"] > ma20 * 1.08:
                if total_range > 0 and body / total_range < 0.1:  # 十字星
                    vol_ma = df["volume"].tail(20).mean()
                    if latest["volume"] > vol_ma * dyn_vol_threshold:
                        score -= 5
                        details.append(f"高位放量十字星(量比>{dyn_vol_threshold:.2f}动态阈值,变盘)")

        detail_str = "+".join(details) if details else ""
        return score, detail_str

    # ============================================================
    # 生成操作建议
    # ============================================================

    def _generate_suggestion(self, score: int, result: dict, holding: dict,
                              df: pd.DataFrame = None) -> dict:
        """
        根据评分生成操作建议和止损调整
        V2.0: 止损调整改用ATR动态倍数，并设置合理性边界
        """
        suggestion = "正常操作"
        stop_adjust = 0.0

        # 计算ATR用于动态止损
        atr = self._compute_atr(df, 20) if df is not None and not df.empty else 0
        price = df["close"].iloc[-1] if df is not None and not df.empty else 0
        # 原始止损基准（默认8%）
        base_stop_pct = 0.08

        if score >= 75:
            suggestion = "高度疑似洗盘，建议持有观望，放宽止损"
            # 洗盘高分：放宽 1.5 * ATR
            if atr > 0 and price > 0:
                stop_adjust = min(1.5 * atr / price, base_stop_pct * 0.20)  # 放宽不超过原始止损的20%
                logger.debug(f"止损放宽: 1.5*ATR={1.5*atr/price:.3f}, 上限={base_stop_pct*0.20:.3f}")
            else:
                stop_adjust = 0.03  # 回退到固定值
        elif score >= 60:
            suggestion = "疑似洗盘，建议观望，暂不触发止损"
            if atr > 0 and price > 0:
                stop_adjust = min(1.0 * atr / price, base_stop_pct * 0.20)
            else:
                stop_adjust = 0.02
        elif score >= 45:
            suggestion = "主力行为不明显，按正常策略操作"
            stop_adjust = 0.0
        elif score >= 30:
            suggestion = "偏空信号，注意风险，收紧止损"
            # 出货偏空：收紧 0.5 * ATR
            if atr > 0 and price > 0:
                stop_adjust = -min(0.5 * atr / price, base_stop_pct * 0.50)  # 收紧不低于原始止损的50%
                logger.debug(f"止损收紧: 0.5*ATR={0.5*atr/price:.3f}, 上限={base_stop_pct*0.50:.3f}")
            else:
                stop_adjust = -0.01
        else:
            suggestion = "真破位概率大，严格执行止损"
            if atr > 0 and price > 0:
                stop_adjust = -min(1.0 * atr / price, base_stop_pct * 0.50)
            else:
                stop_adjust = -0.02

        # 诱多特殊处理
        if result.get("bull_trap"):
            suggestion = "疑似诱多，勿追高，已持仓考虑减仓"
            if atr > 0 and price > 0:
                stop_adjust = -min(0.5 * atr / price, base_stop_pct * 0.50)
            else:
                stop_adjust = -0.01

        # 诱空特殊处理
        if result.get("bear_trap"):
            suggestion = "疑似诱空，勿恐慌割肉，可逢低补仓"
            if atr > 0 and price > 0:
                stop_adjust = min(1.0 * atr / price, base_stop_pct * 0.20)
            else:
                stop_adjust = 0.02

        return {
            "suggestion": suggestion,
            "stop_loss_adjust": round(stop_adjust, 4),
        }

    # ============================================================
    # 大单净流入异常检测（压盘吸筹）
    # ============================================================

    def detect_accumulation_anomaly(self, df: pd.DataFrame,
                                    capital_flow_data: dict = None) -> dict:
        """
        检测大单净流入异常（压盘吸筹）

        检测逻辑：主力大单连续3天以上净流出，但股价不跌（跌幅<1%或上涨）→ 判定为"压盘吸筹"

        参数:
            df: 日线DataFrame，需含close列；可选含capital_flow_net(大单净流入)列
            capital_flow_data: 外部资金流数据（可选），格式 {"net_flow": [最近N日净流入列表]}

        返回:
            {"is_accumulation": bool, "confidence": float, "days": int, "description": str}
        """
        result = {
            "is_accumulation": False,
            "confidence": 0.0,
            "days": 0,
            "description": ""
        }

        if df.empty or len(df) < 5:
            return result

        # 获取大单净流入数据
        net_flows = None
        if capital_flow_data and "net_flow" in capital_flow_data:
            net_flows = capital_flow_data["net_flow"]
        elif "capital_flow_net" in df.columns:
            net_flows = df["capital_flow_net"].tail(10).tolist()

        if net_flows is None or len(net_flows) < 3:
            # 无资金流数据，使用量价关系近似推断：
            # 放量下跌日但收盘价接近开盘价 → 近似大单流出但股价不跌
            recent = df.tail(10)
            consecutive_days = 0
            max_consecutive = 0
            vol_ma = df["volume"].tail(20).mean() if len(df) >= 20 else df["volume"].mean()
            # 使用位置索引遍历，避免时间索引类型问题
            for i in range(len(recent)):
                row = recent.iloc[i]
                # 近似：当日成交量 > 均量 且 上影线较长 且 收盘跌幅 < 1%
                is_large_outflow_approx = (
                    row["volume"] > vol_ma * 1.2
                    and row["high"] > max(row["open"], row["close"]) * 1.01  # 有上影
                )
                # 获取前一日收盘价用于计算涨跌幅
                row_pos = len(df) - len(recent) + i
                if row_pos > 0:
                    prev_close = df.iloc[row_pos - 1]["close"]
                    price_chg = (row["close"] - prev_close) / prev_close if prev_close > 0 else 0
                else:
                    price_chg = 0
                price_not_dropping = price_chg > -0.01

                if is_large_outflow_approx and price_not_dropping:
                    consecutive_days += 1
                    max_consecutive = max(max_consecutive, consecutive_days)
                else:
                    consecutive_days = 0

            if max_consecutive >= 3:
                confidence = min(0.9, 0.5 + (max_consecutive - 3) * 0.1)
                result["is_accumulation"] = True
                result["confidence"] = round(confidence, 2)
                result["days"] = max_consecutive
                result["description"] = f"疑似压盘吸筹(连续{max_consecutive}天大单流出但股价不跌,置信度{confidence:.0%})"
            return result

        # 有真实资金流数据时：检测连续净流出但股价不跌
        closes = df["close"].tail(len(net_flows) + 1).tolist()
        consecutive_days = 0
        max_consecutive = 0
        for i in range(len(net_flows)):
            if net_flows[i] < 0:  # 净流出
                # 对应日的价格变化
                if i + 1 < len(closes):
                    price_chg = (closes[i + 1] - closes[i]) / closes[i] if closes[i] > 0 else 0
                else:
                    price_chg = 0
                if price_chg > -0.01:  # 股价不跌
                    consecutive_days += 1
                    max_consecutive = max(max_consecutive, consecutive_days)
                else:
                    consecutive_days = 0
            else:
                consecutive_days = 0

        if max_consecutive >= 3:
            confidence = min(0.95, 0.6 + (max_consecutive - 3) * 0.1)
            result["is_accumulation"] = True
            result["confidence"] = round(confidence, 2)
            result["days"] = max_consecutive
            result["description"] = f"压盘吸筹(连续{max_consecutive}天主力净流出但股价不跌,置信度{confidence:.0%})"

        return result

    # ============================================================
    # 盘中止损洗盘识别（V3.0新增 - 供alert_engine R5联动）
    # ============================================================

    def detect_stop_loss_wash(self, code: str, df: pd.DataFrame,
                              stop_loss: float, current_price: float,
                              market_change_pct: float = 0.0,
                              sector_change_pct: float = 0.0) -> dict:
        """
        盘中止损触及时的洗盘概率评估（轻量级，适合实时调用）

        核心逻辑:
          1. 假突破检测: 价格触及止损后已收回止损上方 → 高概率洗盘
          2. 量能衰减: 触及止损时放量但非持续恐慌性抛售 → 洗盘
          3. 被动跌破: 大盘/板块整体回调带动，个股相对强度未破坏 → 洗盘
          4. 支撑完整: MA20/MA60未破，趋势结构完好 → 洗盘
          5. 真破位特征: 连续放量+板块崩塌+均线全破 → 非洗盘

        参数:
            code: 股票代码
            df: 含技术指标的日线DataFrame（至少20根）
            stop_loss: 止损价
            current_price: 当前实时价格
            market_change_pct: 大盘(沪深300)当日涨跌幅(%)
            sector_change_pct: 所属板块当日涨跌幅(%)

        返回:
            {
                "is_likely_wash": bool,      # 是否大概率洗盘
                "wash_probability": float,    # 洗盘概率(0-1)
                "should_block_stop": bool,    # 是否建议拦截止损
                "reasons": list,             # 判定依据列表
                "risk_factors": list,        # 风险因素（支持止损的理由）
                "suggested_action": str,     # 建议操作
            }
        """
        result = {
            "is_likely_wash": False,
            "wash_probability": 0.0,
            "should_block_stop": False,
            "reasons": [],
            "risk_factors": [],
            "suggested_action": "",
        }

        if df.empty or len(df) < 20 or stop_loss <= 0 or current_price <= 0:
            result["suggested_action"] = "数据不足，按正常止损执行"
            return result

        wash_score = 0.0  # 洗盘概率累加（满分1.0）
        risk_score = 0.0  # 真破位概率累加
        reasons = []
        risk_factors = []

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else latest

        # ---- 因子1: 假突破检测（权重最高 0.45）----
        # 当前价已收回止损上方 → 极强洗盘信号
        if current_price > stop_loss:
            recover_pct = (current_price - stop_loss) / stop_loss
            if recover_pct > 0.02:  # 收回2%以上 → 强假突破
                wash_score += 0.45
                reasons.append(f"假突破确认: 已收回止损上方{recover_pct*100:.1f}%(强信号)")
            elif recover_pct > 0.01:  # 收回1%-2%
                wash_score += 0.35
                reasons.append(f"假突破确认: 已收回止损上方{recover_pct*100:.1f}%")
            else:
                wash_score += 0.20
                reasons.append(f"触及后微幅收回(仅{recover_pct*100:.2f}%)")
        else:
            # 仍在止损下方，检查日内是否曾收回
            day_low = latest.get("low", current_price)
            if day_low < stop_loss and current_price > day_low * 1.02:
                wash_score += 0.10
                reasons.append("日内探底后有回升迹象")
            risk_factors.append("当前仍在止损下方")

        # ---- 因子2: 量能模式（权重 0.25）----
        vol_ma20 = df["volume"].tail(20).mean() if "volume" in df.columns else 0
        today_vol = latest.get("volume", 0)
        if vol_ma20 > 0 and today_vol > 0:
            vol_ratio = today_vol / vol_ma20
            if vol_ratio > 2.5:
                # 极端放量: 可能是恐慌抛售（真破位）也可能是对倒洗盘
                # 关键区分: 放量但价格收回 → 洗盘; 放量且价格持续下方 → 出货
                if current_price > stop_loss:
                    wash_score += 0.15
                    reasons.append(f"放量{vol_ratio:.1f}倍但价格已收回(对倒洗盘特征)")
                else:
                    risk_score += 0.20
                    risk_factors.append(f"放量{vol_ratio:.1f}倍且价格未收回(恐慌抛售)")
            elif vol_ratio < 1.2:
                # 缩量触及止损 → 非恐慌性，洗盘概率大
                wash_score += 0.25
                reasons.append(f"缩量触及(量比{vol_ratio:.2f}<1.2，非恐慌性抛售)")
            else:
                # 温和放量，中性
                wash_score += 0.05
        else:
            # 无成交量数据，给中性分
            wash_score += 0.05

        # ---- 因子3: 被动跌破（权重 0.20）----
        # 大盘/板块整体下跌带动，个股相对强度未破坏
        stock_change = 0.0
        if latest["close"] > 0:
            # V3.1: 用最新K线收盘价（昨日）作为基准，而非前天
            stock_change = (current_price - latest["close"]) / latest["close"] * 100

        if market_change_pct < -1.0 or sector_change_pct < -1.5:
            # 大盘/板块明显下跌
            relative_strength = stock_change - market_change_pct
            if relative_strength > 0:  # 个股跌幅小于大盘
                wash_score += 0.20
                reasons.append(f"被动跌破: 大盘{market_change_pct:.1f}%，个股相对强度+{relative_strength:.1f}%")
            elif relative_strength > -1.0:  # 个股略弱于大盘但差距不大
                wash_score += 0.10
                reasons.append(f"板块联动下跌(大盘{market_change_pct:.1f}%，相对强度{relative_strength:.1f}%)")
            else:
                risk_score += 0.10
                risk_factors.append(f"个股弱于大盘(相对强度{relative_strength:.1f}%)")
        else:
            # 大盘正常，个股独立下跌 → 更可能是自身问题
            if stock_change < -3:
                risk_score += 0.10
                risk_factors.append(f"大盘正常但个股独立下跌{stock_change:.1f}%")

        # ---- 因子4: 趋势支撑完整性（权重 0.20）----
        ma20 = latest.get("ma20", 0)
        ma60 = latest.get("ma60", 0) if "ma60" in df.columns else 0
        if pd.isna(ma20):
            ma20 = 0
        if pd.isna(ma60):
            ma60 = 0

        support_intact = 0
        if ma20 > 0 and current_price > ma20:
            support_intact += 1
        if ma60 > 0 and current_price > ma60:
            support_intact += 1
        # 检查MA20是否仍在上行（趋势未破坏）
        if len(df) >= 5 and "ma20" in df.columns:
            ma20_5ago = df["ma20"].iloc[-5]
            if not pd.isna(ma20_5ago) and ma20 > ma20_5ago:
                support_intact += 1  # MA20仍上行

        if support_intact >= 2:
            wash_score += 0.20
            reasons.append(f"趋势支撑完好(MA20/MA60未破，结构健康)")
        elif support_intact == 1:
            wash_score += 0.10
            reasons.append("部分支撑仍在")
        else:
            risk_score += 0.15
            risk_factors.append("均线支撑已全面失守")

        # ---- 综合判定 ----
        # 真破位硬否决: 连续放量下跌+均线全破 → 无论洗盘分多高都不拦
        consecutive_vol_drop = 0
        if len(df) >= 3 and "volume" in df.columns:
            for i in range(-3, 0):
                row = df.iloc[i]
                prev_row = df.iloc[i - 1]
                if row["close"] < prev_row["close"] and row["volume"] > vol_ma20 * 1.3:
                    consecutive_vol_drop += 1

        hard_reject = (consecutive_vol_drop >= 3 and support_intact == 0)
        if hard_reject:
            risk_score += 0.30
            risk_factors.append(f"连续{consecutive_vol_drop}日放量下跌+均线全破(真破位)")

        # 最终概率（V3.1: 假突破强信号时降低风险因子惩罚权重）
        # 当价格已明确收回止损上方>2%时，趋势因素不应完全抵消假突破信号
        if current_price > stop_loss * 1.02:
            # 强假突破: 风险惩罚降低到30%
            wash_probability = min(0.95, max(0.05, wash_score - risk_score * 0.3))
        else:
            wash_probability = min(0.95, max(0.05, wash_score - risk_score * 0.5))
        is_likely_wash = wash_probability >= 0.55 and not hard_reject
        should_block = wash_probability >= 0.60 and not hard_reject

        result["is_likely_wash"] = is_likely_wash
        result["wash_probability"] = round(wash_probability, 2)
        result["should_block_stop"] = should_block
        result["reasons"] = reasons
        result["risk_factors"] = risk_factors

        # 生成建议
        if should_block:
            result["suggested_action"] = "高概率洗盘，建议暂不执行止损，等待15分钟确认"
        elif is_likely_wash:
            result["suggested_action"] = "疑似洗盘，建议观望，若5分钟内未收回止损则执行"
        elif hard_reject:
            result["suggested_action"] = "真破位特征明确，坚决执行止损"
        else:
            result["suggested_action"] = "洗盘概率不高，建议按纪律执行止损"

        logger.info(f"[反洗盘] {code} 止损触及评估: 洗盘概率{wash_probability:.0%} | "
                    f"{'拦截' if should_block else '不拦截'} | {result['suggested_action']}")

        return result

    # ============================================================
    # V9.0 P1-3: 主力四阶段判定（吸筹/洗盘/拉升/出货）
    # ============================================================

    def classify_main_force_stage(self, code: str, df: pd.DataFrame,
                                   holding: dict = None) -> dict:
        """
        判定主力操作阶段（V9.0 P1-3）

        四阶段:
          吸筹期: 底部横盘>20天 + 换手率温和 + OBV上升
          洗盘期: 急跌缩量 + 快速收回 + 不破关键均线
          拉升期: 连续阳线 + 突破平台 + 量增价升
          出货期: 高位放量滞涨 + 内外盘失衡

        返回:
            {
                "stage": "accumulation"/"wash"/"markup"/"distribution"/"unknown",
                "confidence": float,
                "detail": str,
                "signal": str,  # 操作建议
            }
        """
        stage_cfg = getattr(config, 'MAIN_FORCE_STAGE_CONFIG', {})
        result = {
            "stage": "unknown",
            "confidence": 0.3,
            "detail": "数据不足",
            "signal": "正常操作",
        }

        if df is None or len(df) < 30:
            return result

        close = df["close"]
        volume = df["volume"]
        high = df["high"]
        low = df["low"]

        vol_ma20 = volume.tail(20).mean()
        price_current = close.iloc[-1]
        price_20ago = close.iloc[-20]
        high_60 = high.tail(60).max() if len(df) >= 60 else high.max()
        low_60 = low.tail(60).min() if len(df) >= 60 else low.min()

        # 位置判定: 当前价在60日区间中的位置
        price_range = high_60 - low_60
        position_pct = (price_current - low_60) / price_range if price_range > 0 else 0.5

        # --- 吸筹期判定 ---
        accum_days = stage_cfg.get("accumulation_min_days", 20)
        turnover_range = stage_cfg.get("accumulation_turnover_range", [1.0, 3.0])
        # 底部横盘: 近20日振幅<10% + 位于低位
        recent_range = (high.tail(accum_days).max() - low.tail(accum_days).min()) / low.tail(accum_days).min()
        is_bottom = position_pct < 0.3
        is_consolidating = recent_range < 0.10

        # OBV趋势
        obv = self._compute_obv(df)
        obv_rising = obv[-1] > obv[-10] if len(obv) >= 10 else False

        if is_bottom and is_consolidating and obv_rising:
            result.update({
                "stage": "accumulation",
                "confidence": 0.7,
                "detail": f"底部横盘{accum_days}天(振幅{recent_range*100:.1f}%) + OBV上升 | 吸筹末期",
                "signal": "前瞻关注: 吸筹末期，突破平台后可介入",
            })
            return result

        # --- 拉升期判定 ---
        markup_days = stage_cfg.get("markup_min_consecutive_up", 3)
        markup_vol = stage_cfg.get("markup_vol_increase", 1.5)
        # 连续阳线
        consecutive_up = 0
        for i in range(len(df) - 1, max(len(df) - 10, -1), -1):
            if close.iloc[i] > df["open"].iloc[i]:
                consecutive_up += 1
            else:
                break
        # 量增
        recent_vol = volume.tail(3).mean()
        vol_increasing = recent_vol > vol_ma20 * markup_vol
        # 均线多头
        ma5 = close.tail(5).mean()
        ma20 = close.tail(20).mean()
        ma_bullish = ma5 > ma20 and price_current > ma5

        if consecutive_up >= markup_days and vol_increasing and ma_bullish:
            result.update({
                "stage": "markup",
                "confidence": 0.75,
                "detail": f"连续{consecutive_up}阳 + 量增{recent_vol/vol_ma20:.1f}倍 + 均线多头 | 拉升期",
                "signal": "持有: 拉升期不轻易卖出，跟踪移动止损",
            })
            return result

        # --- 出货期判定 ---
        dist_vol_ratio = stage_cfg.get("distribution_high_vol_ratio", 2.0)
        dist_stagnation = stage_cfg.get("distribution_stagnation", 0.01)
        is_high = position_pct > 0.7
        high_vol = volume.iloc[-1] > vol_ma20 * dist_vol_ratio
        today_change = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] if len(close) > 1 else 0
        stagnation = abs(today_change) < dist_stagnation

        if is_high and high_vol and stagnation:
            result.update({
                "stage": "distribution",
                "confidence": 0.7,
                "detail": f"高位(位置{position_pct*100:.0f}%) + 放量{volume.iloc[-1]/vol_ma20:.1f}倍 + 滞涨{today_change*100:.2f}% | 出货期",
                "signal": "警告: 出货期特征，建议分批减仓",
            })
            return result

        # --- 洗盘期判定 ---
        # 急跌缩量 + 位于中位 + 不破MA20
        today_drop = today_change < -0.02
        vol_shrink = volume.iloc[-1] < vol_ma20 * 0.7
        above_ma20 = price_current > ma20 * 0.97  # 不破MA20的3%

        if today_drop and vol_shrink and above_ma20 and position_pct > 0.3:
            result.update({
                "stage": "wash",
                "confidence": 0.65,
                "detail": f"急跌{today_change*100:.1f}% + 缩量{volume.iloc[-1]/vol_ma20:.1f}倍 + 不破MA20 | 洗盘期",
                "signal": "持有: 洗盘特征，不宜恐慌卖出",
            })
            return result

        result["detail"] = f"位置{position_pct*100:.0f}% | 无明确阶段特征"
        return result

    def _compute_obv(self, df: pd.DataFrame) -> np.ndarray:
        """计算OBV能量潮"""
        close = df["close"].values
        volume = df["volume"].values
        obv = np.zeros(len(close))
        for i in range(1, len(close)):
            if close[i] > close[i - 1]:
                obv[i] = obv[i - 1] + volume[i]
            elif close[i] < close[i - 1]:
                obv[i] = obv[i - 1] - volume[i]
            else:
                obv[i] = obv[i - 1]
        return obv


    def batch_analyze(self, data_dict: dict, holdings: dict = None) -> dict:
        """
        批量分析所有股票

        返回: {code: analysis_result}
        """
        if holdings is None:
            holdings = {}

        results = {}
        for code, df in data_dict.items():
            if df.empty or len(df) < 20:
                continue
            holding = holdings.get(code)
            results[code] = self.analyze(code, df, holding)

        return results


# 模块级便捷函数
_default_analyzer = None

def get_analyzer() -> AntiManipulationAnalyzer:
    """获取默认分析器实例"""
    global _default_analyzer
    if _default_analyzer is None:
        _default_analyzer = AntiManipulationAnalyzer()
    return _default_analyzer


def analyze_manipulation(code: str, df: pd.DataFrame, holding: dict = None) -> dict:
    """便捷函数：分析单只股票的主力行为"""
    return get_analyzer().analyze(code, df, holding)
