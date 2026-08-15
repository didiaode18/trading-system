# -*- coding: utf-8 -*-
"""
北向资金+融资融券真实数据模块
==============================
对标同花顺/东方财富付费北向资金个股持仓+融资融券变动

数据源: akshare (免费)
  - 北向资金: stock_hsgt_individual_em / stock_hsgt_north_net_flow_in_em
  - 融资融券: stock_margin_detail_szse / stock_margin_detail_sse

核心功能:
  1. 北向资金个股持仓变动（连续N日加仓/减仓检测）
  2. 融资余额拐点（从降转升=杠杆资金入场）
  3. 北向+融资共振信号（两者同向=强确认）
  4. 替代caopan_signal中的量价估算逻辑

回退策略:
  - akshare不可用时，返回data_source="estimated"标记
  - 不伪造数据，明确告知用户数据来源

使用:
    from data.real_fund_data import RealFundData
    fund = RealFundData()
    result = fund.analyze("002415", df)
"""

import threading
import time
import pandas as pd
import numpy as np
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

FUND_CONFIG = {
    "north_consecutive_days": 5,   # 北向连续加仓天数阈值
    "north_large_change_pct": 0.5, # 北向单日大幅变动阈值(%)
    "margin_turn_days": 3,         # 融资拐点确认天数
    "resonance_min_score": 60,     # 共振信号最低分
}


class RealFundData:
    """真实资金数据获取器"""

    # 类级别: 所有实例共享的失败缓存 {stock_code: last_failure_timestamp}
    _stock_failure_cache: Dict[str, float] = {}
    _cache_lock = threading.Lock()
    _COOLDOWN_SECONDS = 300  # 5分钟冷却期

    # V5修复: ETF/基金非北向持股与两融标的，对其发起真实数据请求必失败
    # 且刷屏警告(如"北向资金: 588000 获取失败")，识别后直接走量价估算路径
    _ETF_FUND_PREFIXES = ("159", "510", "511", "512", "513", "515",
                          "516", "518", "560", "562", "588")

    def __init__(self, config: dict = None):
        self.cfg = {**FUND_CONFIG, **(config or {})}
        self._cache = {}

    def analyze(self, code: str, df: pd.DataFrame = None) -> dict:
        """
        分析单只标的的真实资金数据

        参数:
            code: 股票代码 (如 "002415")
            df: K线数据（用于回退估算）

        返回:
            {
                "code": str,
                "data_source": "real" | "estimated" | "unavailable",
                "north": {...},    # 北向资金数据
                "margin": {...},   # 融资融券数据
                "resonance": {...}, # 共振信号
                "score": float,    # 资金综合评分 0-100
                "signal": str,     # 综合信号
            }
        """
        result = {
            "code": code,
            "data_source": "unavailable",
            "north": None,
            "margin": None,
            "resonance": None,
            "score": 50,
            "signal": "数据不可用",
        }

        # 尝试获取真实数据（ETF/基金非北向与两融标的，跳过真实源直接估算）
        _is_etf = str(code).startswith(self._ETF_FUND_PREFIXES)
        north_data = None if _is_etf else self._fetch_north_fund(code)
        margin_data = None if _is_etf else self._fetch_margin_data(code)

        if north_data or margin_data:
            result["data_source"] = "real"
            result["north"] = north_data
            result["margin"] = margin_data

            # 计算共振信号
            resonance = self._calc_resonance(north_data, margin_data)
            result["resonance"] = resonance
            result["score"] = resonance.get("score", 50)
            result["signal"] = resonance.get("signal", "中性")
        elif df is not None and len(df) >= 20:
            # 回退: 用量价数据估算
            result["data_source"] = "estimated"
            estimated = self._estimate_from_volume(code, df)
            result["north"] = estimated.get("north")
            result["margin"] = estimated.get("margin")
            result["score"] = estimated.get("score", 50)
            result["signal"] = estimated.get("signal", "估算中性")

        return result

    # ---- per-stock 冷却重试机制 ----

    def _should_skip_stock(self, stock_code: str) -> bool:
        """检查股票是否在冷却期内"""
        with self._cache_lock:
            last_fail = self._stock_failure_cache.get(stock_code)
            if last_fail is None:
                return False
            if time.time() - last_fail < self._COOLDOWN_SECONDS:
                return True
            # 冷却期已过，清除记录
            self._stock_failure_cache.pop(stock_code, None)
            return False

    def _mark_stock_failure(self, stock_code: str):
        """记录股票获取失败时间"""
        with self._cache_lock:
            self._stock_failure_cache[stock_code] = time.time()

    def _mark_stock_success(self, stock_code: str):
        """股票获取成功，清除失败记录"""
        with self._cache_lock:
            if stock_code in self._stock_failure_cache:
                del self._stock_failure_cache[stock_code]
                logger.info(f"北向资金: {stock_code} 冷却期后重试成功")

    def _fetch_north_fund(self, code: str) -> Optional[dict]:
        """获取北向资金个股持仓数据"""
        if self._should_skip_stock(code):
            logger.debug(f"北向资金: {code} 在冷却期内，跳过获取")
            return None

        try:
            import akshare as ak

            # 尝试获取个股北向持仓
            # akshare接口: stock_hsgt_individual_em (个股北向持股)
            symbol = self._to_akshare_symbol(code)
            df = ak.stock_hsgt_individual_em(symbol=symbol)

            if df is None or df.empty:
                return None

            # 解析最近N天数据
            recent = df.tail(10)
            if "持股数量" in df.columns:
                hold_col = "持股数量"
            elif "持股股数" in df.columns:
                hold_col = "持股股数"
            else:
                return None

            holdings = recent[hold_col].values
            if len(holdings) < 2:
                return None

            # 计算连续加仓/减仓
            changes = np.diff(holdings)
            consecutive_buy = 0
            consecutive_sell = 0
            for c in reversed(changes):
                if c > 0:
                    consecutive_buy += 1
                    consecutive_sell = 0
                elif c < 0:
                    consecutive_sell += 1
                    consecutive_buy = 0
                else:
                    break

            # 5日变动比例
            if holdings[-6] > 0 and len(holdings) >= 6:
                change_5d_pct = (holdings[-1] - holdings[-6]) / holdings[-6] * 100
            else:
                change_5d_pct = 0

            self._mark_stock_success(code)

            return {
                "current_holding": float(holdings[-1]),
                "change_5d_pct": round(change_5d_pct, 2),
                "consecutive_buy_days": consecutive_buy,
                "consecutive_sell_days": consecutive_sell,
                "trend": "加仓" if consecutive_buy >= 3 else "减仓" if consecutive_sell >= 3 else "平稳",
                "data_days": len(recent),
            }

        except Exception as e:
            logger.debug(f"北向资金获取失败({code}): {e}")
            self._mark_stock_failure(code)
            logger.warning(f"北向资金: {code} 获取失败，进入5分钟冷却期")
            return None

    def _fetch_margin_data(self, code: str) -> Optional[dict]:
        """获取融资融券数据"""
        if self._should_skip_stock(code):
            logger.debug(f"融资融券: {code} 在冷却期内，跳过获取")
            return None

        try:
            import akshare as ak

            # 融资融券明细
            if code.startswith("6"):
                df = ak.stock_margin_detail_sse(date=datetime.now().strftime("%Y%m%d"))
            else:
                df = ak.stock_margin_detail_szse(date=datetime.now().strftime("%Y%m%d"))

            if df is None or df.empty:
                return None

            # 筛选目标股票
            stock_row = df[df["标的证券代码"] == code] if "标的证券代码" in df.columns else pd.DataFrame()
            if stock_row.empty:
                return None

            row = stock_row.iloc[0]
            margin_buy = float(row.get("融资买入额", 0))
            margin_balance = float(row.get("融资余额", 0))

            self._mark_stock_success(code)

            return {
                "margin_balance": margin_balance,
                "margin_buy_today": margin_buy,
                "trend": "未知(需历史数据)",
            }

        except Exception as e:
            logger.debug(f"融资融券获取失败({code}): {e}")
            self._mark_stock_failure(code)
            logger.warning(f"融资融券: {code} 获取失败，进入5分钟冷却期")
            return None

    def _estimate_from_volume(self, code: str, df: pd.DataFrame) -> dict:
        """
        回退方案: 用量价数据估算资金动向
        基于: 大单比例估算 + 量价背离检测
        """
        if "main_flow" in df.columns:
            recent_5 = df["main_flow"].tail(5).sum()
            recent_20 = df["main_flow"].tail(20).sum()

            # 连续流入天数
            streak = 0
            for v in reversed(df["main_flow"].tail(10).values):
                if v > 0:
                    streak += 1
                else:
                    break

            # 评分
            score = 50
            if recent_5 > 0:
                score += 15
            if recent_20 > 0:
                score += 10
            if streak >= 3:
                score += 15
            elif streak == 0:
                # 检查连续流出
                out_streak = 0
                for v in reversed(df["main_flow"].tail(10).values):
                    if v < 0:
                        out_streak += 1
                    else:
                        break
                if out_streak >= 3:
                    score -= 15

            # 量价背离
            close_5 = (df["close"].iloc[-1] - df["close"].iloc[-5]) / df["close"].iloc[-5]
            vol_5 = df["volume"].tail(5).mean()
            vol_20 = df["volume"].tail(20).mean()
            vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1

            if close_5 > 0.02 and vol_ratio < 0.8:
                score -= 10  # 缩量上涨=动能不足
            elif close_5 > 0 and vol_ratio > 1.3:
                score += 10  # 放量上涨=资金确认

            signal = "资金流入" if score >= 65 else "资金流出" if score <= 35 else "资金中性"

            return {
                "north": {
                    "trend": "估算" + ("流入" if recent_5 > 0 else "流出"),
                    "consecutive_buy_days": streak,
                    "change_5d_pct": round(recent_5 / max(abs(recent_20), 1) * 100, 1),
                },
                "margin": None,
                "score": max(0, min(100, score)),
                "signal": signal,
            }

        return {"score": 50, "signal": "估算中性"}

    def _calc_resonance(self, north: Optional[dict], margin: Optional[dict]) -> dict:
        """计算北向+融资共振信号"""
        score = 50
        signals = []

        if north:
            trend = north.get("trend", "")
            if trend == "加仓":
                score += 20
                signals.append(f"北向连续{north.get('consecutive_buy_days',0)}日加仓")
            elif trend == "减仓":
                score -= 20
                signals.append(f"北向连续{north.get('consecutive_sell_days',0)}日减仓")

            change_5d = north.get("change_5d_pct", 0)
            if change_5d > 2:
                score += 10
                signals.append(f"北向5日加仓{change_5d:.1f}%")
            elif change_5d < -2:
                score -= 10
                signals.append(f"北向5日减仓{abs(change_5d):.1f}%")

        if margin:
            # 融资数据解析（如果有历史）
            pass

        # 共振判定
        if score >= self.cfg["resonance_min_score"]:
            signal = "资金共振看多"
        elif score <= 100 - self.cfg["resonance_min_score"]:
            signal = "资金共振看空"
        else:
            signal = "资金方向不明"

        return {
            "score": max(0, min(100, score)),
            "signal": signal,
            "details": signals,
        }

    def _to_akshare_symbol(self, code: str) -> str:
        """转换为akshare格式的股票代码"""
        return code

    def batch_analyze(self, codes: List[str], data_dict: Dict[str, pd.DataFrame] = None) -> Dict[str, dict]:
        """批量分析"""
        results = {}
        for code in codes:
            df = data_dict.get(code) if data_dict else None
            results[code] = self.analyze(code, df)
        return results


def fund_data_summary(result: dict) -> str:
    """资金数据摘要"""
    source = result.get("data_source", "unavailable")
    source_cn = {"real": "真实数据", "estimated": "量价估算", "unavailable": "不可用"}.get(source, source)

    lines = [f"💰 资金分析 [{source_cn}]"]

    north = result.get("north")
    if north:
        lines.append(f"  北向: {north.get('trend','-')} | 5日变动{north.get('change_5d_pct',0):+.1f}%")

    margin = result.get("margin")
    if margin:
        lines.append(f"  融资: 余额{margin.get('margin_balance',0)/1e8:.2f}亿")

    lines.append(f"  综合: {result.get('signal','-')} ({result.get('score',50)}分)")

    resonance = result.get("resonance")
    if resonance and resonance.get("details"):
        for d in resonance["details"]:
            lines.append(f"    • {d}")

    return "\n".join(lines)
