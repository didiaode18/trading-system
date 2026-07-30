# -*- coding: utf-8 -*-
"""
K线组合形态识别引擎 V1.0（V9.0 P1-2）
======================================
识别顶部/底部/持续K线形态，盘后扫描持仓股发出前瞻预警

核心形态:
  顶部反转: 黄昏之星、乌云盖顶、高位长上影、三只乌鸦、高位十字星+放量
  底部反转: 早晨之星、锤子线、看涨吞没、红三兵、缩量企稳突破
  持续形态: 上升三法、下降三法

数据源: 日线OHLCV（stock_db.db / baostock）
嵌入位置: scheduler.py 盘后15:30任务中调用

使用:
    from strategy.kline_pattern import KlinePatternDetector
    detector = KlinePatternDetector()
    result = detector.scan(df, code="002371", name="北方华创")
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)
CFG = getattr(config, 'KLINE_PATTERN_CONFIG', {})


class KlinePatternDetector:
    """K线组合形态识别器"""

    def __init__(self, cfg: dict = None):
        self.cfg = {**CFG, **(cfg or {})}

    def scan(self, df: pd.DataFrame, code: str = "", name: str = "") -> dict:
        """
        扫描最近N根K线，识别形态

        参数:
            df: 含 date/open/high/low/close/volume 的DataFrame
            code: 股票代码
            name: 股票名称

        返回:
            {
                "patterns": [{"name": str, "type": "top"/"bottom"/"continue",
                              "strength": int, "detail": str}],
                "top_count": int,
                "bottom_count": int,
                "suggestion": str,
            }
        """
        result = {"patterns": [], "top_count": 0, "bottom_count": 0, "suggestion": "无异常"}

        if df is None or len(df) < 10:
            result["suggestion"] = "数据不足"
            return result

        lookback = self.cfg.get("lookback_days", 5)
        recent = df.tail(lookback + 5).reset_index(drop=True)  # 多取5根用于上下文

        patterns = []

        # === 顶部形态检测 ===
        p = self._detect_evening_star(recent)
        if p:
            patterns.append(p)
        p = self._detect_dark_cloud(recent)
        if p:
            patterns.append(p)
        p = self._detect_long_upper_shadow(recent)
        if p:
            patterns.append(p)
        p = self._detect_three_crows(recent)
        if p:
            patterns.append(p)
        p = self._detect_high_doji(recent, df)
        if p:
            patterns.append(p)

        # === 底部形态检测 ===
        p = self._detect_morning_star(recent)
        if p:
            patterns.append(p)
        p = self._detect_hammer(recent)
        if p:
            patterns.append(p)
        p = self._detect_bullish_engulfing(recent)
        if p:
            patterns.append(p)
        p = self._detect_three_soldiers(recent)
        if p:
            patterns.append(p)

        # 汇总
        result["patterns"] = patterns
        result["top_count"] = sum(1 for p in patterns if p["type"] == "top")
        result["bottom_count"] = sum(1 for p in patterns if p["type"] == "bottom")

        if result["top_count"] >= 2:
            result["suggestion"] = "多个顶部形态叠加，强烈建议减仓"
        elif result["top_count"] == 1:
            result["suggestion"] = "出现顶部形态，建议警惕并考虑减仓"
        elif result["bottom_count"] >= 2:
            result["suggestion"] = "多个底部形态，关注反弹机会"
        elif result["bottom_count"] == 1:
            result["suggestion"] = "出现底部形态，可持有观察"

        return result

    # ============================================================
    # 辅助计算
    # ============================================================

    def _body(self, row) -> float:
        """实体长度"""
        return abs(row["close"] - row["open"])

    def _upper_shadow(self, row) -> float:
        """上影线"""
        return row["high"] - max(row["close"], row["open"])

    def _lower_shadow(self, row) -> float:
        """下影线"""
        return min(row["close"], row["open"]) - row["low"]

    def _range(self, row) -> float:
        """振幅"""
        return row["high"] - row["low"]

    def _is_bullish(self, row) -> bool:
        return row["close"] > row["open"]

    def _is_bearish(self, row) -> bool:
        return row["close"] < row["open"]

    def _is_doji(self, row) -> bool:
        """十字星: 实体<振幅×doji_body_pct"""
        r = self._range(row)
        if r <= 0:
            return False
        return self._body(row) / r < self.cfg.get("doji_body_pct", 0.10)

    def _is_high_position(self, df: pd.DataFrame, idx: int) -> bool:
        """判断是否处于近期高位（收盘价在近20日最高价附近）"""
        if idx < 20:
            lookback = df.iloc[:idx + 1]
        else:
            lookback = df.iloc[idx - 20:idx + 1]
        high_20 = lookback["high"].max()
        current = df.iloc[idx]["close"]
        return current >= high_20 * 0.95  # 在最高价95%以上视为高位

    # ============================================================
    # 顶部形态
    # ============================================================

    def _detect_evening_star(self, df: pd.DataFrame) -> dict:
        """黄昏之星: 阳线 + 十字星/小实体 + 阴线（三根组合）"""
        if len(df) < 3:
            return None
        d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]

        if not self._is_bullish(d1):
            return None
        if self._body(d1) < self._range(d1) * 0.5:
            return None  # 第一根需为大阳线
        if self._body(d2) > self._body(d1) * 0.3:
            return None  # 第二根为小实体
        if not self._is_bearish(d3):
            return None
        if d3["close"] > (d1["open"] + d1["close"]) / 2:
            return None  # 第三根阴线需深入第一根实体50%以上

        return {
            "name": "黄昏之星",
            "type": "top",
            "strength": 80,
            "detail": f"大阳+小实体+阴线深入，顶部反转信号",
        }

    def _detect_dark_cloud(self, df: pd.DataFrame) -> dict:
        """乌云盖顶: 阳线 + 高开阴线（收盘深入阳线实体>50%）"""
        if len(df) < 2:
            return None
        d1, d2 = df.iloc[-2], df.iloc[-1]

        if not self._is_bullish(d1):
            return None
        if not self._is_bearish(d2):
            return None
        if d2["open"] < d1["close"]:
            return None  # 需高开（开盘>昨收）
        mid = (d1["open"] + d1["close"]) / 2
        if d2["close"] > mid:
            return None  # 收盘需深入阳线实体50%

        return {
            "name": "乌云盖顶",
            "type": "top",
            "strength": 75,
            "detail": f"高开阴线深入前阳{((d1['close']-d2['close'])/self._body(d1)*100):.0f}%",
        }

    def _detect_long_upper_shadow(self, df: pd.DataFrame) -> dict:
        """高位长上影: 上影线>实体×long_shadow_ratio + 处于高位"""
        if len(df) < 5:
            return None
        last = df.iloc[-1]
        body = self._body(last)
        upper = self._upper_shadow(last)
        ratio = self.cfg.get("long_shadow_ratio", 2.0)

        if body <= 0:
            return None
        if upper < body * ratio:
            return None
        if not self._is_high_position(df, len(df) - 1):
            return None

        return {
            "name": "高位长上影",
            "type": "top",
            "strength": 70,
            "detail": f"上影线{upper:.2f}={body:.2f}×{upper/body:.1f}倍，高位受阻",
        }

    def _detect_three_crows(self, df: pd.DataFrame) -> dict:
        """三只乌鸦: 连续3根阴线，每根跌幅>three_crows_min_drop"""
        if len(df) < 3:
            return None
        min_drop = self.cfg.get("three_crows_min_drop", -0.02)
        last3 = df.iloc[-3:]

        for i in range(3):
            row = last3.iloc[i]
            if not self._is_bearish(row):
                return None
            chg = (row["close"] - row["open"]) / row["open"]
            if chg > min_drop:
                return None

        return {
            "name": "三只乌鸦",
            "type": "top",
            "strength": 85,
            "detail": "连续3根阴线，空头强势，下跌趋势确认",
        }

    def _detect_high_doji(self, df: pd.DataFrame, full_df: pd.DataFrame) -> dict:
        """高位十字星+放量: 高位出现十字星且量>均量1.5倍"""
        if len(df) < 2:
            return None
        last = df.iloc[-1]
        if not self._is_doji(last):
            return None
        if not self._is_high_position(full_df, len(full_df) - 1):
            return None
        # 放量判定
        vol_ma = full_df["volume"].tail(20).mean()
        if vol_ma > 0 and last["volume"] > vol_ma * 1.5:
            return {
                "name": "高位十字星(放量)",
                "type": "top",
                "strength": 65,
                "detail": f"高位十字星+量比{last['volume']/vol_ma:.1f}，多空分歧加大",
            }
        return None

    # ============================================================
    # 底部形态
    # ============================================================

    def _detect_morning_star(self, df: pd.DataFrame) -> dict:
        """早晨之星: 阴线 + 十字星/小实体 + 阳线"""
        if len(df) < 3:
            return None
        d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]

        if not self._is_bearish(d1):
            return None
        if self._body(d1) < self._range(d1) * 0.5:
            return None
        if self._body(d2) > self._body(d1) * 0.3:
            return None
        if not self._is_bullish(d3):
            return None
        mid = (d1["open"] + d1["close"]) / 2
        if d3["close"] < mid:
            return None

        return {
            "name": "早晨之星",
            "type": "bottom",
            "strength": 80,
            "detail": "大阴+小实体+阳线收复，底部反转信号",
        }

    def _detect_hammer(self, df: pd.DataFrame) -> dict:
        """锤子线: 下影线>实体×2 + 上影线极短 + 处于低位"""
        if len(df) < 5:
            return None
        last = df.iloc[-1]
        body = self._body(last)
        lower = self._lower_shadow(last)
        upper = self._upper_shadow(last)

        if body <= 0:
            return None
        ratio = self.cfg.get("long_shadow_ratio", 2.0)
        if lower < body * ratio:
            return None
        if upper > body * 0.5:
            return None  # 上影线需极短

        # 低位判定
        low_20 = df["low"].tail(20).min()
        if last["close"] > low_20 * 1.10:
            return None  # 不在低位

        return {
            "name": "锤子线",
            "type": "bottom",
            "strength": 70,
            "detail": f"下影线{lower:.2f}=实体{body:.2f}×{lower/body:.1f}倍，低位止跌",
        }

    def _detect_bullish_engulfing(self, df: pd.DataFrame) -> dict:
        """看涨吞没: 今阳线实体完全包含昨阴线实体"""
        if len(df) < 2:
            return None
        d1, d2 = df.iloc[-2], df.iloc[-1]
        ratio = self.cfg.get("engulfing_ratio", 1.2)

        if not self._is_bearish(d1):
            return None
        if not self._is_bullish(d2):
            return None
        if self._body(d2) < self._body(d1) * ratio:
            return None
        if d2["close"] < d1["open"] or d2["open"] > d1["close"]:
            return None  # 需完全包含

        return {
            "name": "看涨吞没",
            "type": "bottom",
            "strength": 75,
            "detail": f"阳线实体吞没前阴×{self._body(d2)/max(self._body(d1),0.01):.1f}倍",
        }

    def _detect_three_soldiers(self, df: pd.DataFrame) -> dict:
        """红三兵: 连续3根阳线，每根收盘创新高"""
        if len(df) < 3:
            return None
        last3 = df.iloc[-3:]

        for i in range(3):
            if not self._is_bullish(last3.iloc[i]):
                return None
        # 每根收盘高于前一根
        if not (last3.iloc[1]["close"] > last3.iloc[0]["close"] and
                last3.iloc[2]["close"] > last3.iloc[1]["close"]):
            return None

        return {
            "name": "红三兵",
            "type": "bottom",
            "strength": 70,
            "detail": "连续3阳创新高，多头强势推进",
        }


# ============================================================
# 便捷接口（供scheduler盘后任务调用）
# ============================================================

def scan_holdings_patterns(data_dict: dict, holdings: dict) -> list:
    """
    扫描所有持仓股的K线形态

    参数:
        data_dict: {code: DataFrame} 各标的日线数据
        holdings: {code: {name, shares, ...}} 持仓信息

    返回:
        [{"code": str, "name": str, "patterns": [...], "suggestion": str}]
    """
    detector = KlinePatternDetector()
    results = []

    for code, df in data_dict.items():
        if code not in holdings:
            continue
        name = holdings[code].get("name", code)
        result = detector.scan(df, code=code, name=name)
        if result["patterns"]:
            result["code"] = code
            result["name"] = name
            results.append(result)

    return results
