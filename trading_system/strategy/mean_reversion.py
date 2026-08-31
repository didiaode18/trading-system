"""
均值回归策略（V10.0 P1-① 新增）
==============================
震荡市/熊市捕捉超跌反弹，与CANSLIM成长策略互补。

核心逻辑:
  1. 超跌识别: RSI<30 + 价格跌破布林下轨 + 偏离MA20超过-8%
  2. 反弹确认: 当日放量阳线 + 量比>1.5
  3. 风控硬约束: 硬止损-5% + 止盈+8% + 持仓不超过5天
  4. Regime门控: BEAR模式禁用，RANGE模式权重50%，BULL模式权重30%

使用方式:
    from strategy.mean_reversion import MeanReversionStrategy
    strategy = MeanReversionStrategy()
    candidates = strategy.screen(stock_data, regime="RANGE")

适用场景:
  - 大盘震荡市（RANGE）: 权重50%，捕捉板块轮动超跌
  - 大盘熊市（BEAR）: 禁用（风险过高）
  - 大盘牛市（BULL）: 权重30%，作为成长策略补充

与CANSLIM对比:
  | 维度 | CANSLIM | 均值回归 |
  | 策略类型 | 趋势跟踪 | 逆势反弹 |
  | 适用Regime | BULL | RANGE |
  | 持仓周期 | 中线(周-月) | 短线(1-5天) |
  | 止损 | ATR动态 | 硬止损-5% |
  | 相关系数 | 目标<0.3 |
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

# 策略参数
RSI_OVERSOLD = 30           # RSI超卖阈值
BOLLINGER_STD = 2.0         # 布林带标准差倍数
MA_DEVIATION = -0.08        # 偏离MA20阈值(-8%)
VOLUME_RATIO = 1.5          # 反弹确认量比
HARD_STOP_LOSS = -0.05      # 硬止损-5%
HARD_TAKE_PROFIT = 0.08     # 止盈+8%
MAX_HOLDING_DAYS = 5        # 最大持仓天数

# Regime权重
REGIME_WEIGHTS = {
    "BULL": 0.3,    # 牛市: 低权重补充
    "RANGE": 0.5,   # 震荡: 主战场
    "BEAR": 0.0,    # 熊市: 禁用
}


class MeanReversionStrategy:
    """均值回归策略"""

    def __init__(self):
        self.name = "均值回归"
        self.version = "V10.0"

    def screen(self, df: pd.DataFrame, code: str = "", regime: str = "RANGE") -> dict:
        """筛选超跌反弹候选

        参数:
            df: 个股日线数据 (columns: open, high, low, close, volume)
            code: 股票代码
            regime: 当前大盘状态 ("BULL"/"RANGE"/"BEAR")

        返回:
            {
                "signal": bool,         # 是否触发信号
                "score": float,         # 信号强度(0-10)
                "reasons": [...],       # 触发原因
                "entry_price": float,   # 建议入场价
                "stop_loss": float,     # 止损价
                "take_profit": float,   # 止盈价
                "regime_weight": float, # Regime权重
            }
        """
        # Regime门控
        regime_weight = REGIME_WEIGHTS.get(regime, 0.0)
        if regime_weight <= 0:
            return {
                "signal": False,
                "score": 0,
                "reasons": [f"Regime={regime}，策略禁用"],
                "entry_price": 0,
                "stop_loss": 0,
                "take_profit": 0,
                "regime_weight": 0,
            }

        if df is None or len(df) < 30:
            return {
                "signal": False,
                "score": 0,
                "reasons": ["数据不足"],
                "entry_price": 0,
                "stop_loss": 0,
                "take_profit": 0,
                "regime_weight": regime_weight,
            }

        reasons = []
        score = 0

        # ---- 条件1: RSI超卖 ----
        rsi = self._calc_rsi(df["close"], period=14)
        if rsi < RSI_OVERSOLD:
            score += 3
            reasons.append(f"RSI超卖({rsi:.1f}<{RSI_OVERSOLD})")

        # ---- 条件2: 跌破布林下轨 ----
        bollinger_lower = self._calc_bollinger_lower(df["close"], period=20, std=BOLLINGER_STD)
        current_price = df["close"].iloc[-1]
        if current_price < bollinger_lower:
            score += 3
            reasons.append(f"跌破布林下轨({current_price:.2f}<{bollinger_lower:.2f})")

        # ---- 条件3: 偏离MA20超过阈值 ----
        ma20 = df["close"].rolling(20).mean().iloc[-1]
        deviation = (current_price - ma20) / ma20
        if deviation < MA_DEVIATION:
            score += 2
            reasons.append(f"偏离MA20({deviation:.1%}<{MA_DEVIATION:.1%})")

        # ---- 条件4: 反弹确认（当日放量阳线）----
        if len(df) >= 2:
            today = df.iloc[-1]
            yesterday = df.iloc[-2]
            is_positive = today["close"] > today["open"]
            volume_ratio = today["volume"] / yesterday["volume"] if yesterday["volume"] > 0 else 1.0

            if is_positive and volume_ratio > VOLUME_RATIO:
                score += 2
                reasons.append(f"放量阳线(量比{volume_ratio:.1f}>{VOLUME_RATIO})")

        # 信号判定（score >= 5 触发）
        signal = score >= 5

        # 计算入场/止损/止盈价
        entry_price = current_price if signal else 0
        stop_loss = current_price * (1 + HARD_STOP_LOSS) if signal else 0
        take_profit = current_price * (1 + HARD_TAKE_PROFIT) if signal else 0

        return {
            "signal": signal,
            "score": min(10, score),
            "reasons": reasons,
            "entry_price": round(entry_price, 2),
            "stop_loss": round(stop_loss, 2),
            "take_profit": round(take_profit, 2),
            "regime_weight": regime_weight,
            "rsi": round(rsi, 1),
            "deviation": round(deviation, 3),
        }

    def check_exit(self, entry_price: float, current_price: float, holding_days: int) -> dict:
        """检查是否触发退出条件

        参数:
            entry_price: 入场价
            current_price: 当前价
            holding_days: 持仓天数

        返回:
            {"exit": bool, "reason": str, "pnl_pct": float}
        """
        if entry_price <= 0:
            return {"exit": False, "reason": "无入场价", "pnl_pct": 0}

        pnl_pct = (current_price - entry_price) / entry_price

        # 硬止损
        if pnl_pct <= HARD_STOP_LOSS:
            return {"exit": True, "reason": f"触发硬止损({pnl_pct:.1%})", "pnl_pct": pnl_pct}

        # 止盈
        if pnl_pct >= HARD_TAKE_PROFIT:
            return {"exit": True, "reason": f"触发止盈({pnl_pct:.1%})", "pnl_pct": pnl_pct}

        # 超时退出
        if holding_days >= MAX_HOLDING_DAYS:
            return {"exit": True, "reason": f"持仓超时({holding_days}天>={MAX_HOLDING_DAYS})", "pnl_pct": pnl_pct}

        return {"exit": False, "reason": "继续持有", "pnl_pct": pnl_pct}

    # ============================================================
    # 技术指标计算
    # ============================================================

    def _calc_rsi(self, series: pd.Series, period: int = 14) -> float:
        """计算RSI指标"""
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1] if len(rsi) > 0 else 50

    def _calc_bollinger_lower(self, series: pd.Series, period: int = 20, std: float = 2.0) -> float:
        """计算布林带下轨"""
        ma = series.rolling(window=period).mean()
        std_dev = series.rolling(window=period).std()
        lower = ma - std * std_dev
        return lower.iloc[-1] if len(lower) > 0 else series.iloc[-1]


# ============================================================
# 便捷函数（供选股引擎调用）
# ============================================================

def screen_mean_reversion(df: pd.DataFrame, code: str = "", regime: str = "RANGE") -> dict:
    """均值回归筛选（便捷函数）"""
    strategy = MeanReversionStrategy()
    return strategy.screen(df, code, regime)


def check_exit(entry_price: float, current_price: float, holding_days: int) -> dict:
    """退出检查（便捷函数）"""
    strategy = MeanReversionStrategy()
    return strategy.check_exit(entry_price, current_price, holding_days)


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # 示例：从数据库加载数据并筛选
    from data.data_loader import init_db, load_daily_data

    conn = init_db()
    test_codes = ["000858", "600519", "000001"]  # 五粮液、茅台、平安

    print("=" * 60)
    print("均值回归策略筛选示例")
    print("=" * 60)

    for code in test_codes:
        try:
            df = load_daily_data(code, conn, days=60)
            if df is not None and len(df) >= 30:
                result = screen_mean_reversion(df, code, regime="RANGE")
                print(f"\n{code}: signal={result['signal']}, score={result['score']}")
                if result['signal']:
                    print(f"  入场价: {result['entry_price']}")
                    print(f"  止损价: {result['stop_loss']}")
                    print(f"  止盈价: {result['take_profit']}")
                    print(f"  原因: {', '.join(result['reasons'])}")
        except Exception as e:
            print(f"{code}: 错误 - {e}")

    conn.close()
