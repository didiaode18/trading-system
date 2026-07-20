"""
均值回归策略模块 V1.0
======================
适用于震荡市/弱势行情的短线反弹策略

核心逻辑:
  当市场处于震荡或弱势状态时，趋势策略失效，
  转而使用均值回归策略捕捉超跌反弹机会。

策略原理:
  1. 布林带下轨支撑 + RSI超卖 → 反弹概率大
  2. 股价偏离MA20过远（负偏离>2个标准差）→ 回归动力
  3. 缩量企稳 + 底部放量 → 确认反弹启动
  4. 快进快出，目标利润5-8%，严格止损3-5%

入场条件（需满足至少3项）:
  - RSI(14) < 30（超卖）
  - 收盘价触及或跌破布林带下轨
  - 股价较MA20负偏离 > 5%
  - 近3日缩量（量<均量60%）后当日放量（量>前日1.5倍）
  - MACD柱状图由负转正（金叉前兆）

出场条件:
  - 目标止盈: 反弹至布林带中轨（MA20）或浮盈5-8%
  - 止损: 跌破入场价3-5%无条件离场
  - 时间止损: 持有超过5天未达目标，平仓

使用方式:
    from strategy.mean_reversion import MeanReversionStrategy
    mr = MeanReversionStrategy()
    signals = mr.scan_reversion_signals(data_dict, market_state="weak")
"""

import os
import sys
import logging
import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class MeanReversionStrategy:
    """均值回归策略"""

    def __init__(self):
        # 策略参数
        self.rsi_oversold = config.RSI_OVERSOLD          # RSI超卖阈值(30)
        self.boll_period = config.BOLL_PERIOD            # 布林带周期(20)
        self.boll_std = config.BOLL_STD                  # 布林带标准差(2)
        self.ma_period = config.MA_SHORT                 # 均线周期(20)
        self.target_profit = 0.06                        # 目标利润6%
        self.stop_loss = 0.04                            # 止损4%
        self.max_hold_days = 5                           # 最大持有天数
        self.min_deviation = -0.05                       # 最小负偏离(-5%)
        self.volume_shrink_ratio = 0.60                  # 缩量标准
        self.volume_expand_ratio = 1.5                   # 放量标准
        self.min_conditions = 3                          # 最少满足条件数

    def scan_reversion_signals(self, data_dict: dict, market_state: str = "normal",
                                holdings: dict = None) -> list:
        """
        扫描所有候选股的均值回归信号
        
        参数:
            data_dict: {code: DataFrame}
            market_state: "strong"/"normal"/"weak"
            holdings: 当前持仓（排除已持有的）
        
        返回:
            [{"code", "name", "signal_strength", "conditions_met", "entry_price",
              "target_price", "stop_price", "reason", ...}]
        """
        if holdings is None:
            holdings = {}

        # 均值回归策略在弱势/震荡市更有效
        # 强势市不使用此策略（趋势策略更优）
        if market_state == "strong":
            logger.info("[均值回归] 强势行情，不启用均值回归策略")
            return []

        signals = []
        for code, df in data_dict.items():
            # 跳过指数
            if code == config.BENCHMARK_INDEX:
                continue
            # 跳过已持仓
            if code in holdings:
                continue
            # 数据量检查
            if len(df) < 60:
                continue

            signal = self._check_single_stock(code, df)
            if signal:
                signals.append(signal)

        # 按信号强度排序
        signals.sort(key=lambda x: x["signal_strength"], reverse=True)

        logger.info(f"[均值回归] 扫描{len(data_dict)}只，发现{len(signals)}个反弹信号")
        for sig in signals[:5]:
            logger.info(f"  {sig['code']} {sig['name']}: "
                       f"强度{sig['signal_strength']}/5 | "
                       f"入场{sig['entry_price']:.2f} → "
                       f"目标{sig['target_price']:.2f}(+{self.target_profit:.0%}) | "
                       f"止损{sig['stop_price']:.2f}(-{self.stop_loss:.0%})")

        return signals

    def _check_single_stock(self, code: str, df: pd.DataFrame) -> dict:
        """检查单只股票的均值回归信号"""
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        close = latest["close"]
        conditions_met = 0
        reasons = []

        # ---- 条件1: RSI超卖 ----
        rsi = latest.get("rsi", 50)
        if pd.notna(rsi) and rsi < self.rsi_oversold:
            conditions_met += 1
            reasons.append(f"RSI={rsi:.1f}超卖")

        # ---- 条件2: 触及布林带下轨 ----
        boll_lower = latest.get("boll_lower", 0)
        if pd.notna(boll_lower) and boll_lower > 0:
            if close <= boll_lower * 1.01:  # 容差1%
                conditions_met += 1
                reasons.append("触及布林下轨")

        # ---- 条件3: MA20负偏离过大 ----
        ma20 = latest.get("ma20", 0)
        if pd.notna(ma20) and ma20 > 0:
            deviation = (close - ma20) / ma20
            if deviation < self.min_deviation:
                conditions_met += 1
                reasons.append(f"偏离MA20达{deviation:.1%}")

        # ---- 条件4: 缩量后放量（底部放量确认）----
        vol = latest.get("volume", 0)
        vol_ma = latest.get("vol_ma20", 0)
        prev_vol = prev.get("volume", 0)
        if pd.notna(vol_ma) and vol_ma > 0 and vol > 0:
            # 近3日缩量
            recent_3_vol = df["volume"].tail(4).head(3).mean()
            is_shrink = recent_3_vol < vol_ma * self.volume_shrink_ratio
            # 当日放量
            is_expand = vol > prev_vol * self.volume_expand_ratio if prev_vol > 0 else False
            if is_shrink and is_expand:
                conditions_met += 1
                reasons.append("缩量后放量确认")
            elif vol > vol_ma * 1.3 and close > prev["close"]:
                # 或者当日直接放量上涨
                conditions_met += 1
                reasons.append("放量反弹")

        # ---- 条件5: MACD柱状图改善 ----
        macd_hist = latest.get("macd_hist", 0)
        prev_macd_hist = prev.get("macd_hist", 0)
        if pd.notna(macd_hist) and pd.notna(prev_macd_hist):
            if macd_hist > prev_macd_hist and macd_hist < 0:
                # 柱状图缩短（空头动能减弱）
                conditions_met += 1
                reasons.append("MACD空头动能减弱")
            elif macd_hist > 0 and prev_macd_hist <= 0:
                # 金叉
                conditions_met += 1
                reasons.append("MACD金叉")

        # ---- 额外加分: K线形态 ----
        # 下影线较长（探底回升）
        low = latest.get("low", close)
        high = latest.get("high", close)
        open_p = latest.get("open", close)
        if close > 0 and (high - low) > 0:
            lower_shadow = (min(close, open_p) - low) / (high - low)
            if lower_shadow > 0.6 and close > open_p:
                conditions_met += 0.5
                reasons.append("长下影探底回升")

        # 判断是否满足入场条件
        if conditions_met < self.min_conditions:
            return None

        # ---- 计算入场/目标/止损价 ----
        entry_price = close
        target_price = round(close * (1 + self.target_profit), 2)
        stop_price = round(close * (1 - self.stop_loss), 2)

        # 如果布林中轨(MA20)在目标范围内，以MA20为目标
        if pd.notna(ma20) and ma20 > close and ma20 < target_price:
            target_price = round(ma20, 2)

        # 信号强度 (1-5)
        strength = min(5, int(conditions_met))

        # 获取股票名称
        name = self._get_stock_name(code)
        sector = self._get_stock_sector(code)

        return {
            "code": code,
            "name": name,
            "sector": sector,
            "signal_strength": strength,
            "conditions_met": conditions_met,
            "entry_price": entry_price,
            "target_price": target_price,
            "stop_price": stop_price,
            "target_profit_pct": round((target_price / entry_price - 1) * 100, 1),
            "stop_loss_pct": round((1 - stop_price / entry_price) * 100, 1),
            "rsi": round(rsi, 1) if pd.notna(rsi) else None,
            "deviation_ma20": round((close / ma20 - 1) * 100, 1) if pd.notna(ma20) and ma20 > 0 else None,
            "reason": " | ".join(reasons),
            "strategy": "mean_reversion",
            "max_hold_days": self.max_hold_days,
            "signal_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def calc_position_size(self, signal: dict) -> dict:
        """
        计算均值回归策略的仓位
        
        均值回归是短线策略，仓位应小于趋势策略:
        - 单只不超过总资金8%
        - 总均值回归仓位不超过20%
        """
        price = signal["entry_price"]
        stop_price = signal["stop_price"]
        risk_per_share = price - stop_price

        if risk_per_share <= 0:
            return {"shares": 0, "amount": 0, "ratio": 0}

        # 单笔风险 = 总资金 × 1.5%（均值回归风险更小）
        risk_amount = config.TOTAL_CAPITAL * 0.015
        shares = int(risk_amount / risk_per_share)
        shares = (shares // 100) * 100

        # 仓位上限8%
        max_shares = int(config.TOTAL_CAPITAL * 0.08 / price / 100) * 100
        shares = min(shares, max_shares)

        # 可用资金约束
        available = getattr(config, 'AVAILABLE_CASH', config.TOTAL_CAPITAL * 0.3)
        max_affordable = int(available * 0.3 / price / 100) * 100  # 最多用30%可用资金
        shares = min(shares, max_affordable)

        amount = shares * price
        return {
            "shares": shares,
            "amount": round(amount, 2),
            "ratio": round(amount / config.TOTAL_CAPITAL, 4),
            "risk_amount": round(shares * risk_per_share, 2),
        }

    def _get_stock_name(self, code: str) -> str:
        if code in config.STOCK_POOL:
            return config.STOCK_POOL[code].get("名称", code)
        for sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).values():
            stocks = sector_info.get("stocks", {})
            if code in stocks:
                return stocks[code].get("名称", code)
        return code

    def _get_stock_sector(self, code: str) -> str:
        if code in config.STOCK_POOL:
            return config.STOCK_POOL[code].get("赛道", "其他")
        for sector_name, sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).items():
            if code in sector_info.get("stocks", {}):
                return sector_name
        return "其他"


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 50)
    print("  均值回归策略 - 测试")
    print("=" * 50)
    print("  需要加载数据后调用scan_reversion_signals()")
    print("  示例:")
    print("    mr = MeanReversionStrategy()")
    print("    signals = mr.scan_reversion_signals(data_dict, 'weak')")
    print("\n[OK] 模块加载正常")
