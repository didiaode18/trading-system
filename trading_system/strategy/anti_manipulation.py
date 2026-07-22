"""
反主力操控分析模块 V1.0
========================
识别主力洗盘、诱多、诱空等操控行为，辅助止损/止盈决策

核心检测:
  1. 量价背离识别（洗盘 vs 出货）
  2. 诱多/诱空陷阱检测
  3. 主力行为评分（0-100）
  4. 评分影响止损阈值

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

        # 限制评分范围
        score = max(0, min(100, score))
        result["manipulation_score"] = score

        # ---- 生成建议 ----
        result.update(self._generate_suggestion(score, result, holding))
        result["detail"] = " | ".join(details[:4])  # 最多4条

        # 置信度：基于数据量和信号强度
        signal_strength = abs(score - 50) / 50  # 0~1
        data_confidence = min(1.0, len(df) / 60)  # 60天以上数据满分
        result["confidence"] = round(signal_strength * 0.6 + data_confidence * 0.4, 2)

        return result

    # ============================================================
    # 量价背离分析
    # ============================================================

    def _analyze_volume_price(self, df: pd.DataFrame) -> tuple:
        """
        量价关系分析:
        - 价跌量缩 = 洗盘概率大 (+20)
        - 价跌量增 = 真出货 (-15)
        - 价涨量缩 = 诱多嫌疑 (-10)
        - 价涨量增 = 健康上涨 (+5)
        """
        if len(df) < 5:
            return "数据不足", 0, ""

        # 最近3天的价格和成交量变化
        recent = df.tail(3)
        price_change = (recent["close"].iloc[-1] - recent["close"].iloc[0]) / recent["close"].iloc[0]

        # 成交量均值对比
        vol_ma20 = df["volume"].tail(20).mean() if len(df) >= 20 else df["volume"].mean()
        recent_vol_avg = recent["volume"].mean()
        vol_ratio = recent_vol_avg / vol_ma20 if vol_ma20 > 0 else 1.0

        if price_change < -0.02:  # 价格下跌>2%
            if vol_ratio < self.volume_shrink_ratio:
                state = "缩量回调"
                score = 20
                detail = f"价跌量缩(量比{vol_ratio:.2f})，洗盘概率大"
            else:
                state = "放量下跌"
                score = -15
                detail = f"价跌量增(量比{vol_ratio:.2f})，出货嫌疑"
        elif price_change > 0.02:  # 价格上涨>2%
            if vol_ratio < self.volume_shrink_ratio:
                state = "缩量上涨"
                score = -10
                detail = f"价涨量缩(量比{vol_ratio:.2f})，诱多嫌疑"
            else:
                state = "放量上涨"
                score = 5
                detail = f"价涨量增(量比{vol_ratio:.2f})，健康上涨"
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
        洗盘特征:
        1. 快速下跌后快速收回（V型/长下影线）
        2. 下跌时缩量，反弹时放量
        3. 连续小阴线后突然大阳
        4. 跌破重要均线后快速收回
        """
        score = 0
        details = []

        if len(df) < 10:
            return 0, ""

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # 特征1: 长下影线（下影线>实体2倍）
        body = abs(latest["close"] - latest["open"])
        lower_shadow = min(latest["open"], latest["close"]) - latest["low"]
        if body > 0 and lower_shadow > body * 2:
            score += 15
            details.append("长下影线(洗盘特征)")

        # 特征2: 连续小阴线后大阳
        if len(df) >= 5:
            recent_5 = df.tail(5)
            small_yin_count = 0
            for i in range(4):
                row = recent_5.iloc[i]
                if row["close"] < row["open"]:  # 阴线
                    change = (row["open"] - row["close"]) / row["open"]
                    if change < 0.02:  # 小阴线(<2%)
                        small_yin_count += 1
            # 最后一天是大阳线
            last_change = (latest["close"] - latest["open"]) / latest["open"] if latest["open"] > 0 else 0
            if small_yin_count >= 3 and last_change > 0.03:
                score += 20
                details.append("连续小阴后大阳(典型洗盘完成)")

        # 特征3: 跌破MA20后快速收回
        if len(df) >= 21 and "ma20" in df.columns:
            ma20 = latest.get("ma20", 0)
            prev_close = prev["close"]
            curr_close = latest["close"]
            if ma20 > 0 and prev_close < ma20 and curr_close > ma20:
                score += 10
                details.append("跌破MA20后收回(诱空洗盘)")

        # 特征4: 急跌后缩量企稳
        if len(df) >= 5:
            max_drop = 0
            for i in range(-5, -1):
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
                    details.append("急跌后缩量企稳(洗盘尾声)")

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

        # 检测2: 高位放量长上影线
        if len(df) >= 20:
            ma20 = latest.get("ma20", close)
            if ma20 > 0 and close > ma20 * 1.10:  # 高于MA20 10%以上
                upper_shadow = latest["high"] - max(latest["close"], latest["open"])
                body = abs(latest["close"] - latest["open"])
                vol_ma = df["volume"].tail(20).mean()
                if body > 0 and upper_shadow > body * 2 and volume > vol_ma * 1.5:
                    return True, "高位放量长上影(诱多出货)"

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

        # 计算ATR(14)
        if "atr" in df.columns:
            atr = latest.get("atr", 0)
        else:
            # 手动计算ATR
            highs = df["high"].tail(14).values
            lows = df["low"].tail(14).values
            closes = df["close"].tail(14).values
            tr_list = []
            for i in range(1, len(highs)):
                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                tr_list.append(tr)
            atr = np.mean(tr_list) if tr_list else 0

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

        # 高位十字星
        if len(df) >= 20:
            ma20 = latest.get("ma20", latest["close"])
            body = abs(latest["close"] - latest["open"])
            total_range = latest["high"] - latest["low"]
            if ma20 > 0 and latest["close"] > ma20 * 1.08:
                if total_range > 0 and body / total_range < 0.1:  # 十字星
                    vol_ma = df["volume"].tail(20).mean()
                    if latest["volume"] > vol_ma * 1.5:
                        score -= 5
                        details.append("高位放量十字星(变盘)")

        detail_str = "+".join(details) if details else ""
        return score, detail_str

    # ============================================================
    # 生成操作建议
    # ============================================================

    def _generate_suggestion(self, score: int, result: dict, holding: dict) -> dict:
        """根据评分生成操作建议和止损调整"""
        suggestion = "正常操作"
        stop_adjust = 0.0

        if score >= 75:
            suggestion = "高度疑似洗盘，建议持有观望，放宽止损"
            stop_adjust = 0.03  # 放宽3%
        elif score >= 60:
            suggestion = "疑似洗盘，建议观望，暂不触发止损"
            stop_adjust = 0.02  # 放宽2%
        elif score >= 45:
            suggestion = "主力行为不明显，按正常策略操作"
            stop_adjust = 0.0
        elif score >= 30:
            suggestion = "偏空信号，注意风险，收紧止损"
            stop_adjust = -0.01  # 收紧1%
        else:
            suggestion = "真破位概率大，严格执行止损"
            stop_adjust = -0.02  # 收紧2%

        # 诱多特殊处理
        if result.get("bull_trap"):
            suggestion = "疑似诱多，勿追高，已持仓考虑减仓"
            stop_adjust = -0.01

        # 诱空特殊处理
        if result.get("bear_trap"):
            suggestion = "疑似诱空，勿恐慌割肉，可逢低补仓"
            stop_adjust = 0.02

        return {
            "suggestion": suggestion,
            "stop_loss_adjust": stop_adjust,
        }

    # ============================================================
    # 批量分析
    # ============================================================

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
