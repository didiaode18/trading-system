"""
K线形态识别引擎 V1.0
=====================
日式蜡烛图形态（K线组合）全面识别，覆盖单根/双根/三根共32种经典形态。

设计原则:
  1. 位置决定价值 — 同一形态在不同趋势位置含义完全不同
  2. 置信度量化 — 每种形态返回0-1置信度，基于形态标准度+量能配合
  3. 趋势上下文 — 所有形态都接受trend_context参数，判断有效性

形态分类:
  单根(12种): 锤子线/倒锤子/上吊线/射击之星/十字星(3种)/纺锤线/螺旋桨/T字线/倒T字线/大阳线/大阴线
  双根(10种): 看涨/看跌吞没、刺透/乌云盖顶、曙光初现/倾盆大雨、平底/平顶、阳孕阴/阴孕阳
  三根+(10种): 早晨之星/黄昏之星、红三兵/三只乌鸦、多方炮/空方炮、上升/下降三法、低位并排阳线、三线打击

使用:
    from strategy.candlestick_pattern import CandlestickPatternEngine
    engine = CandlestickPatternEngine()
    patterns = engine.detect_all(df, trend_context="downtrend")
"""

import os
import sys
import logging
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ============================================================
# 一、基础工具函数
# ============================================================

def _body(o, c):
    """实体长度（绝对值）"""
    return abs(c - o)

def _upper_shadow(o, c, h):
    """上影线长度"""
    return h - max(o, c)

def _lower_shadow(o, c, l):
    """下影线长度"""
    return min(o, c) - l

def _total_range(o, c, h, l):
    """总振幅"""
    return h - l

def _is_bullish(o, c):
    """阳线"""
    return c > o

def _is_bearish(o, c):
    """阴线"""
    return c < o

def _is_doji(o, c, h, l, cfg):
    """十字星判定: 实体 < 振幅 × doji_body_pct"""
    rng = _total_range(o, c, h, l)
    if rng <= 0:
        return True  # 无振幅视为十字星
    return _body(o, c) / rng < cfg.get("doji_body_pct", 0.10)


# ============================================================
# 二、引擎主类
# ============================================================

class CandlestickPatternEngine:
    """K线形态识别引擎"""

    def __init__(self):
        self.cfg = getattr(config, "KLINE_PATTERN_CONFIG", {})

    # ---- 公开接口 ----

    def detect_all(self, df: pd.DataFrame, trend_context: str = None) -> list:
        """
        返回最近N日所有识别到的K线形态

        参数:
            df: 含 open/high/low/close/volume 的日线DataFrame
            trend_context: "uptrend"/"downtrend"/"sideways" 或 None(自动判断)

        返回:
            [{"pattern": "锤子线", "type": "bullish", "date": "...",
              "confidence": 0.85, "location_valid": True, "signal": "+10"}]
        """
        if df is None or len(df) < 5:
            return []

        lookback = self.cfg.get("lookback_days", 5)
        results = []

        # 自动判断趋势上下文
        if trend_context is None:
            trend_context = self._auto_trend(df)

        # 对最近lookback根K线逐一检测
        start_idx = max(3, len(df) - lookback)
        for idx in range(start_idx, len(df)):
            row = df.iloc[idx]
            o, c, h, l = row["open"], row["close"], row["high"], row["low"]
            vol = row.get("volume", 0)
            date = str(row.get("date", ""))

            # 跳过无效K线
            if h <= 0 or l <= 0 or h < l:
                continue

            # 量能数据（用于置信度调整）
            vol_avg = df["volume"].iloc[max(0, idx-20):idx].mean() if idx > 0 and "volume" in df.columns else 0
            vol_ratio = vol / vol_avg if vol_avg > 0 else 1.0

            ctx = {
                "idx": idx, "o": o, "c": c, "h": h, "l": l,
                "vol": vol, "vol_ratio": vol_ratio,
                "trend": trend_context, "df": df,
            }

            # 单根K线形态
            results.extend(self._detect_single_candle(ctx, date))

            # 双根K线形态（需要前一根）
            if idx >= 1:
                prev = df.iloc[idx - 1]
                ctx_prev = {
                    "o": prev["open"], "c": prev["close"],
                    "h": prev["high"], "l": prev["low"],
                    "vol": prev.get("volume", 0),
                }
                results.extend(self._detect_two_candle(ctx, ctx_prev, date))

            # 三根K线形态
            if idx >= 2:
                p1 = df.iloc[idx - 1]
                p2 = df.iloc[idx - 2]
                ctx_p1 = {"o": p1["open"], "c": p1["close"], "h": p1["high"], "l": p1["low"]}
                ctx_p2 = {"o": p2["open"], "c": p2["close"], "h": p2["high"], "l": p2["low"]}
                results.extend(self._detect_three_candle(ctx, ctx_p1, ctx_p2, date))

        return results

    def score_patterns(self, patterns: list) -> dict:
        """
        对形态列表计算综合评分

        返回:
            {"score": float(-15~+15), "bullish_count": int, "bearish_count": int,
             "top_patterns": [...], "signal": "看涨"/"看跌"/"中性"}
        """
        if not patterns:
            return {"score": 0, "bullish_count": 0, "bearish_count": 0,
                    "top_patterns": [], "signal": "中性"}

        bullish = [p for p in patterns if p["type"] == "bullish" and p.get("location_valid")]
        bearish = [p for p in patterns if p["type"] == "bearish" and p.get("location_valid")]

        # V10.1: 形态类型分级加权（吞没/早晨之星等高价值形态权重更大）
        pt_weights = self.cfg.get("pattern_type_weight", {})
        bull_score = sum(p["confidence"] * pt_weights.get(p["pattern"], 1.0) * 10 for p in bullish)
        bear_score = sum(p["confidence"] * pt_weights.get(p["pattern"], 1.0) * 10 for p in bearish)
        net = bull_score - bear_score

        # 映射到 -15 ~ +15
        score = max(-15, min(15, net))

        if score >= 5:
            signal = "看涨"
        elif score <= -5:
            signal = "看跌"
        else:
            signal = "中性"

        # 取置信度最高的3个形态
        top = sorted(patterns, key=lambda x: x["confidence"], reverse=True)[:3]

        return {
            "score": round(score, 1),
            "bullish_count": len(bullish),
            "bearish_count": len(bearish),
            "top_patterns": [{"name": p["pattern"], "conf": p["confidence"], "type": p["type"]} for p in top],
            "signal": signal,
        }

    # ---- 趋势自动判断 ----

    def _auto_trend(self, df: pd.DataFrame) -> str:
        """基于MA排列自动判断趋势"""
        if len(df) < 20:
            return "sideways"
        close = df["close"].iloc[-1]
        ma5 = df["close"].rolling(5).mean().iloc[-1]
        ma20 = df["close"].rolling(20).mean().iloc[-1]
        ma60 = df["close"].rolling(60).mean().iloc[-1] if len(df) >= 60 else ma20

        if ma5 > ma20 > ma60 and close > ma5:
            return "uptrend"
        elif ma5 < ma20 < ma60 and close < ma5:
            return "downtrend"
        return "sideways"

    # ============================================================
    # 三、单根K线形态（12种）
    # ============================================================

    def _detect_single_candle(self, ctx: dict, date: str) -> list:
        """检测单根K线形态"""
        o, c, h, l = ctx["o"], ctx["c"], ctx["h"], ctx["l"]
        trend = ctx["trend"]
        vol_ratio = ctx["vol_ratio"]
        results = []
        rng = _total_range(o, c, h, l)
        if rng <= 0:
            return results

        body = _body(o, c)
        body_ratio = body / rng
        upper = _upper_shadow(o, c, h)
        lower = _lower_shadow(o, c, l)
        is_bull = _is_bullish(o, c)

        min_body = self.cfg.get("min_body_ratio", 0.3)
        long_shadow = self.cfg.get("long_shadow_ratio", 2.0)

        # V10.1: 量能配合分级（替代简单加成，减少噪音）
        def _vol_adj(vr):
            if vr > 2.0: return 0.15
            elif vr > 1.3: return 0.10
            elif vr < 0.8: return -0.10
            return 0.0

        # 1. 锤子线（底部反转看涨）
        if (trend in ("downtrend", "sideways") and
            lower >= body * long_shadow and upper < body * 0.5 and
            body_ratio < 0.35 and body > 0):
            conf = min(1.0, 0.7 + (lower / max(body, 0.001) - long_shadow) * 0.1 + _vol_adj(vol_ratio))
            results.append(self._mk("锤子线", "bullish", date, conf,
                                    trend == "downtrend", ctx))

        # 2. 倒锤子线（底部反转看涨）
        if (trend in ("downtrend", "sideways") and
            upper >= body * long_shadow and lower < body * 0.5 and
            body_ratio < 0.35 and body > 0):
            conf = min(1.0, 0.65 + _vol_adj(vol_ratio))
            results.append(self._mk("倒锤子线", "bullish", date, conf,
                                    trend == "downtrend", ctx))

        # 3. 上吊线（顶部看跌）
        if (trend in ("uptrend", "sideways") and
            lower >= body * long_shadow and upper < body * 0.5 and
            body_ratio < 0.35 and body > 0):
            conf = min(1.0, 0.70 + _vol_adj(vol_ratio))
            results.append(self._mk("上吊线", "bearish", date, conf,
                                    trend == "uptrend", ctx))

        # 4. 射击之星/流星线（顶部看跌）
        if (trend in ("uptrend", "sideways") and
            upper >= body * long_shadow and lower < body * 0.5 and
            body_ratio < 0.35 and body > 0):
            conf = min(1.0, 0.70 + _vol_adj(vol_ratio))
            results.append(self._mk("射击之星", "bearish", date, conf,
                                    trend == "uptrend", ctx))

        # 5. 十字星（标准）
        if _is_doji(o, c, h, l, self.cfg) and body_ratio < 0.10:
            # 十字星本身中性，位置决定方向
            ptype = "bearish" if trend == "uptrend" else "bullish" if trend == "downtrend" else "neutral"
            results.append(self._mk("十字星", ptype, date, 0.50,
                                    trend in ("uptrend", "downtrend"), ctx))

        # 6. 蜻蜓十字星（下影线长，上影线无 → 看涨）
        if (_is_doji(o, c, h, l, self.cfg) and
            lower > rng * 0.6 and upper < rng * 0.05):
            results.append(self._mk("蜻蜓十字", "bullish", date, 0.65,
                                    trend == "downtrend", ctx))

        # 7. 墓碑十字星（上影线长，下影线无 → 看跌）
        if (_is_doji(o, c, h, l, self.cfg) and
            upper > rng * 0.6 and lower < rng * 0.05):
            results.append(self._mk("墓碑十字", "bearish", date, 0.65,
                                    trend == "uptrend", ctx))

        # 8. 纺锤线（小实体，上下影线均较长）
        if (body_ratio < 0.25 and upper > body * 1.0 and lower > body * 1.0 and body > 0):
            results.append(self._mk("纺锤线", "neutral", date, 0.40, False, ctx))

        # 9. 螺旋桨（小实体，上下影线接近等长）
        if (0.15 < body_ratio < 0.35 and
            abs(upper - lower) < body * 0.5 and body > 0):
            results.append(self._mk("螺旋桨", "neutral", date, 0.40, False, ctx))

        # 10. T字线（开盘=最高=收盘，有下影线 → 看涨）
        if (abs(o - h) < rng * 0.02 and abs(c - h) < rng * 0.02 and
            lower > rng * 0.2):
            results.append(self._mk("T字线", "bullish", date, 0.60,
                                    trend == "downtrend", ctx))

        # 11. 倒T字线（开盘=最低=收盘，有上影线 → 看跌）
        if (abs(o - l) < rng * 0.02 and abs(c - l) < rng * 0.02 and
            upper > rng * 0.2):
            results.append(self._mk("倒T字线", "bearish", date, 0.60,
                                    trend == "uptrend", ctx))

        # 12. 光头光脚大阳线（实体>振幅70%，无上下影 → 强看涨）
        if (is_bull and body_ratio > 0.70 and rng > 0 and
            body / (df_close_mean(ctx) * 0.01 + 0.001) > 0.5):
            conf = min(1.0, 0.75 + (body_ratio - 0.7) * 0.5)
            results.append(self._mk("大阳线", "bullish", date, conf, True, ctx))

        # 13. 光头光脚大阴线（实体>振幅70% → 强看跌）
        if (not is_bull and body_ratio > 0.70):
            conf = min(1.0, 0.75 + (body_ratio - 0.7) * 0.5)
            results.append(self._mk("大阴线", "bearish", date, conf, True, ctx))

        return results

    # ============================================================
    # 四、双根K线形态（10种）
    # ============================================================

    def _detect_two_candle(self, ctx: dict, prev: dict, date: str) -> list:
        """检测双根K线形态"""
        o, c, h, l = ctx["o"], ctx["c"], ctx["h"], ctx["l"]
        po, pc, ph, pl = prev["o"], prev["c"], prev["h"], prev["l"]
        trend = ctx["trend"]
        vol_ratio = ctx["vol_ratio"]
        results = []

        body_now = _body(o, c)
        body_prev = _body(po, pc)
        engulf_ratio = self.cfg.get("engulfing_ratio", 1.2)

        # 1. 看涨吞没（阳线完全包住前一根阴线实体）
        if (_is_bearish(po, pc) and _is_bullish(o, c) and
            o <= pc and c >= po and body_now >= body_prev * engulf_ratio):
            conf = min(1.0, 0.75 + (body_now / max(body_prev, 0.001) - engulf_ratio) * 0.05 + _vol_adj(vol_ratio))
            results.append(self._mk("看涨吞没", "bullish", date, conf,
                                    trend == "downtrend", ctx))

        # 2. 看跌吞没（阴线完全包住前一根阳线实体）
        if (_is_bullish(po, pc) and _is_bearish(o, c) and
            o >= pc and c <= po and body_now >= body_prev * engulf_ratio):
            conf = min(1.0, 0.75 + (body_now / max(body_prev, 0.001) - engulf_ratio) * 0.05 + _vol_adj(vol_ratio))
            results.append(self._mk("看跌吞没", "bearish", date, conf,
                                    trend == "uptrend", ctx))

        # 3. 刺透形态/斩回线（前阴+今阳，今开<昨低，今收>昨实体中点）
        if (_is_bearish(po, pc) and _is_bullish(o, c) and
            o < pl and c > (po + pc) / 2 and c < po):
            conf = min(1.0, 0.70 + _vol_adj(vol_ratio))
            results.append(self._mk("刺透形态", "bullish", date, conf,
                                    trend == "downtrend", ctx))

        # 4. 乌云盖顶（前阳+今阴，今开>昨高，今收<昨实体中点）
        if (_is_bullish(po, pc) and _is_bearish(o, c) and
            o > ph and c < (po + pc) / 2 and c > po):
            conf = min(1.0, 0.70 + _vol_adj(vol_ratio))
            results.append(self._mk("乌云盖顶", "bearish", date, conf,
                                    trend == "uptrend", ctx))

        # 5. 曙光初现（类似刺透但更强：今收>昨开）
        if (_is_bearish(po, pc) and _is_bullish(o, c) and
            o < pl and c >= po):
            conf = min(1.0, 0.80 + _vol_adj(vol_ratio))
            results.append(self._mk("曙光初现", "bullish", date, conf,
                                    trend == "downtrend", ctx))

        # 6. 倾盆大雨（类似乌云但更强：今收<昨开）
        if (_is_bullish(po, pc) and _is_bearish(o, c) and
            o > ph and c <= po):
            conf = min(1.0, 0.80 + _vol_adj(vol_ratio))
            results.append(self._mk("倾盆大雨", "bearish", date, conf,
                                    trend == "uptrend", ctx))

        # 7. 平底（两根K线最低价几乎相同 → 支撑确认）
        if abs(l - pl) < (h - l) * 0.03 and (h - l) > 0:
            results.append(self._mk("平底", "bullish", date, 0.55,
                                    trend == "downtrend", ctx))

        # 8. 平顶（两根K线最高价几乎相同 → 压力确认）
        if abs(h - ph) < (h - l) * 0.03 and (h - l) > 0:
            results.append(self._mk("平顶", "bearish", date, 0.55,
                                    trend == "uptrend", ctx))

        # 9. 阳孕阴（前大阴+今小阳在昨实体内 → 底部反转）
        if (_is_bearish(po, pc) and _is_bullish(o, c) and
            body_prev > body_now * 1.5 and
            o > pc and c < po):
            conf = 0.65 + 0.1 if vol_ratio < 0.8 else 0
            results.append(self._mk("阳孕阴", "bullish", date, conf,
                                    trend == "downtrend", ctx))

        # 10. 阴孕阳（前大阳+今小阴在昨实体内 → 顶部反转）
        if (_is_bullish(po, pc) and _is_bearish(o, c) and
            body_prev > body_now * 1.5 and
            o < pc and c > po):
            conf = 0.65 + 0.1 if vol_ratio < 0.8 else 0
            results.append(self._mk("阴孕阳", "bearish", date, conf,
                                    trend == "uptrend", ctx))

        return results

    # ============================================================
    # 五、三根及多根K线形态（10种）
    # ============================================================

    def _detect_three_candle(self, ctx: dict, p1: dict, p2: dict, date: str) -> list:
        """检测三根K线形态（当前=ctx, 前1=p1, 前2=p2）"""
        o, c, h, l = ctx["o"], ctx["c"], ctx["h"], ctx["l"]
        o1, c1, h1, l1 = p1["o"], p1["c"], p1["h"], p1["l"]
        o2, c2, h2, l2 = p2["o"], p2["c"], p2["h"], p2["l"]
        trend = ctx["trend"]
        vol_ratio = ctx["vol_ratio"]
        results = []

        # 1. 早晨之星/启明星（大阴 + 小实体跳空低开 + 大阳收超中点）
        if (_is_bearish(o2, c2) and _body(o1, c1) < _body(o2, c2) * 0.4 and
            _is_bullish(o, c) and c > (o2 + c2) / 2 and
            max(o1, c1) < c2):  # 中间K线向下跳空
            conf = min(1.0, 0.80 + _vol_adj(vol_ratio))
            results.append(self._mk("早晨之星", "bullish", date, conf,
                                    trend == "downtrend", ctx))

        # 2. 黄昏之星（大阳 + 小实体跳空高开 + 大阴收低于中点）
        if (_is_bullish(o2, c2) and _body(o1, c1) < _body(o2, c2) * 0.4 and
            _is_bearish(o, c) and c < (o2 + c2) / 2 and
            min(o1, c1) > c2):  # 中间K线向上跳空
            conf = min(1.0, 0.80 + _vol_adj(vol_ratio))
            results.append(self._mk("黄昏之星", "bearish", date, conf,
                                    trend == "uptrend", ctx))

        # 3. 红三兵（连续三根阳线，每根创新高）
        if (_is_bullish(o2, c2) and _is_bullish(o1, c1) and _is_bullish(o, c) and
            c > c1 > c2 and o1 > o2 and o > o1):
            conf = 0.75
            # 量能递增加分
            if ctx.get("vol", 0) > p1.get("vol", 0) > p2.get("vol", 0):
                conf += 0.1
            results.append(self._mk("红三兵", "bullish", date, min(conf, 1.0), True, ctx))

        # 4. 三只乌鸦（连续三根阴线，每根创新低）
        if (_is_bearish(o2, c2) and _is_bearish(o1, c1) and _is_bearish(o, c) and
            c < c1 < c2 and o1 < o2 and o < o1):
            min_drop = abs(self.cfg.get("three_crows_min_drop", -0.02))
            drops = [(o2-c2)/o2, (o1-c1)/o1, (o-c)/o] if o2 > 0 and o1 > 0 and o > 0 else [0,0,0]
            if all(d > min_drop for d in drops):
                conf = 0.75
                results.append(self._mk("三只乌鸦", "bearish", date, conf, True, ctx))

        # 5. 多方炮/两阳夹一阴
        if (_is_bullish(o2, c2) and _is_bearish(o1, c1) and _is_bullish(o, c) and
            c >= c2 and o <= o2 and c1 > o2 and o1 < c2):
            conf = 0.70
            results.append(self._mk("多方炮", "bullish", date, conf,
                                    trend == "downtrend", ctx))

        # 6. 空方炮/两阴夹一阳
        if (_is_bearish(o2, c2) and _is_bullish(o1, c1) and _is_bearish(o, c) and
            c <= c2 and o >= o2 and c1 < o2 and o1 > c2):
            conf = 0.70
            results.append(self._mk("空方炮", "bearish", date, conf,
                                    trend == "uptrend", ctx))

        # 7. 低位并排阳线（下跌中两根并列阳线，开盘价接近）
        if (trend == "downtrend" and
            _is_bullish(o1, c1) and _is_bullish(o, c) and
            abs(o - o1) < _body(o1, c1) * 0.3 and
            c2 < o2):  # 前一根是阴线（确认在下跌中）
            conf = 0.60
            results.append(self._mk("低位并排阳线", "bullish", date, conf, True, ctx))

        # 8. 三线打击（三阴后一根大阳吞没前三根）
        if (_is_bearish(p2.get("o", 0), p2.get("c", 0)) and
            _is_bearish(o1, c1) and _is_bearish(o, c) and
            c > p2.get("o", 0) and _body(o, c) > sum([_body(o2,c2), _body(o1,c1)]) * 0.8):
            # 实际上三线打击是看涨持续形态，但这里简化检测
            pass  # 形态条件较复杂，暂不启用

        return results

    # ---- 辅助方法 ----

    def _mk(self, name: str, ptype: str, date: str, confidence: float,
            location_valid: bool, ctx: dict) -> dict:
        """构造标准形态结果"""
        score_map = {
            "bullish": 10 if location_valid else 0,
            "bearish": -10 if location_valid else 0,
            "neutral": 0,
        }
        return {
            "pattern": name,
            "type": ptype,
            "date": date,
            "confidence": round(min(1.0, max(0.0, confidence)), 2),
            "location_valid": location_valid,
            "trend_context": ctx.get("trend", "unknown"),
            "signal": score_map.get(ptype, 0),
        }


def df_close_mean(ctx: dict) -> float:
    """安全获取近期收盘均值"""
    df = ctx.get("df")
    if df is not None and len(df) > 0:
        return df["close"].iloc[-5:].mean()
    return ctx.get("c", 1)
