"""
P2 多策略组
============
3个独立策略，可并行运行:
1. 动量策略: 20日动量+均线趋势（原有因子引擎）
2. 均值回归策略: 超跌反弹（RSI<30 + 布林下轨）
3. 事件驱动策略: 业绩超预期 + 放量突破（简化版）

每个策略独立输出选股信号，由portfolio.py进行资金分配

使用方式:
    from quant.strategies import MomentumStrategy, MeanReversionStrategy, EventStrategy
    strat = MomentumStrategy()
    signals = strat.generate_signals(data_dict, date)
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class BaseStrategy:
    """策略基类"""

    def __init__(self, name: str):
        self.name = name
        self.signal_history = []  # 历史信号记录

    def generate_signals(self, data_dict: dict, date: str) -> list:
        """生成选股信号，返回 [(code, score, reason), ...]"""
        raise NotImplementedError

    def get_recent_performance(self, data_dict: dict, date: str,
                                lookback_days: int = 60) -> dict:
        """计算策略近期表现（用于动态权重）"""
        if not self.signal_history:
            return {"sharpe": 0, "max_dd": 0, "win_rate": 0}

        # 简化：统计历史信号的胜率
        recent = [s for s in self.signal_history if s.get("date", "") >= date[:8]]
        if not recent:
            return {"sharpe": 0, "max_dd": 0, "win_rate": 0}

        wins = sum(1 for s in recent if s.get("pnl_pct", 0) > 0)
        win_rate = wins / len(recent) if recent else 0
        avg_pnl = np.mean([s.get("pnl_pct", 0) for s in recent])
        std_pnl = np.std([s.get("pnl_pct", 0) for s in recent]) if len(recent) > 1 else 1

        return {
            "sharpe": avg_pnl / std_pnl if std_pnl > 0 else 0,
            "max_dd": min(s.get("pnl_pct", 0) for s in recent),
            "win_rate": win_rate,
        }


class MomentumStrategy(BaseStrategy):
    """
    动量策略: 强者恒强（增加买入冷静期）
    - 20日收益率排名前列
    - MA5 > MA10 > MA20 多头排列
    - 成交量温和放大
    - 买入冷静期: 信号触发后延迟1天执行（避免追涨）
    """

    def __init__(self, lookback: int = 20, top_pct: float = 0.1):
        super().__init__("momentum")
        self.lookback = lookback
        self.top_pct = top_pct

    def generate_signals(self, data_dict: dict, date: str) -> list:
        signals = []

        for code, df in data_dict.items():
            df_cut = df[df["date"] <= date]
            if len(df_cut) < 60:
                continue

            close = df_cut["close"].values
            volume = df_cut["volume"].values

            # 20日动量
            momentum = (close[-1] - close[-self.lookback - 1]) / close[-self.lookback - 1]

            # 均线多头排列
            ma5 = np.mean(close[-5:])
            ma10 = np.mean(close[-10:])
            ma20 = np.mean(close[-20:])
            bullish = 1 if (ma5 > ma10 > ma20) else 0

            # 量能配合（5日均量 > 20日均量 * 0.8）
            vol_5 = np.mean(volume[-5:])
            vol_20 = np.mean(volume[-20:])
            vol_ok = 1 if vol_5 > vol_20 * 0.8 else 0

            # 综合得分
            score = momentum * 0.5 + bullish * 0.3 + vol_ok * 0.2

            if momentum > 0.05 and bullish:  # 至少5%动量+多头排列
                signals.append((code, score, f"动量{momentum:.1%}+多头排列[冷静期1天]"))

        # 按得分排序
        signals.sort(key=lambda x: x[1], reverse=True)
        return signals[:20]  # 返回TOP20


class MeanReversionStrategy(BaseStrategy):
    """
    均值回归策略: 超跌反弹
    - RSI < 30（超卖）
    - 价格触及布林带下轨
    - 成交量萎缩（恐慌抛售尾声）
    - 基本面不差（非ST、非连续跌停）
    """

    def __init__(self, rsi_threshold: float = 30, boll_period: int = 20):
        super().__init__("mean_reversion")
        self.rsi_threshold = rsi_threshold
        self.boll_period = boll_period

    def generate_signals(self, data_dict: dict, date: str) -> list:
        signals = []

        for code, df in data_dict.items():
            df_cut = df[df["date"] <= date]
            if len(df_cut) < 60:
                continue

            close = df_cut["close"].values
            n = len(close)

            # RSI计算
            deltas = np.diff(close[-15:])
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            avg_gain = np.mean(gains) if len(gains) > 0 else 0
            avg_loss = np.mean(losses) if len(losses) > 0 else 1
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            # 布林带
            ma20 = np.mean(close[-self.boll_period:])
            std20 = np.std(close[-self.boll_period:])
            boll_lower = ma20 - 2 * std20

            # 超卖条件
            if rsi < self.rsi_threshold and close[-1] <= boll_lower * 1.02:
                # 成交量萎缩（近3日量 < 20日均量 * 0.6）
                volume = df_cut["volume"].values
                vol_3 = np.mean(volume[-3:])
                vol_20 = np.mean(volume[-20:])
                vol_shrink = vol_3 < vol_20 * 0.6

                # 非连续暴跌（近5日最大单日跌幅<8%）
                daily_returns = np.diff(close[-6:]) / close[-6:-1]
                no_crash = all(r > -0.08 for r in daily_returns)

                if vol_shrink and no_crash:
                    score = (self.rsi_threshold - rsi) / self.rsi_threshold
                    signals.append((code, score, f"RSI={rsi:.0f}超卖+布林下轨"))

        signals.sort(key=lambda x: x[1], reverse=True)
        return signals[:10]


class EventStrategy(BaseStrategy):
    """
    事件驱动策略（优化版）:
    - 放量突破60日新高（产业资本信号代理）
    - 连续3日温和放量（资金持续流入）
    - 突破确认: 需连续2日站稳新高（避免假突破追涨）
    """

    def __init__(self, breakout_period: int = 60, vol_increase: float = 1.5):
        super().__init__("event_driven")
        self.breakout_period = breakout_period
        self.vol_increase = vol_increase

    def generate_signals(self, data_dict: dict, date: str) -> list:
        signals = []

        for code, df in data_dict.items():
            df_cut = df[df["date"] <= date]
            if len(df_cut) < self.breakout_period + 5:
                continue

            close = df_cut["close"].values
            volume = df_cut["volume"].values

            # 条件1: 突破N日新高 + 连续2日站稳（避免假突破）
            high_n = np.max(close[-self.breakout_period - 1:-1])
            is_breakout = close[-1] > high_n
            is_breakout_confirmed = is_breakout and close[-2] > high_n * 0.99  # 前一日也接近/超过新高

            # 条件2: 放量（今日量 > 5日均量 * 1.5）
            vol_today = volume[-1]
            vol_5 = np.mean(volume[-6:-1])
            is_vol_surge = vol_today > vol_5 * self.vol_increase

            # 条件3: 连续3日温和放量
            vol_3 = volume[-3:]
            vol_increasing = all(vol_3[i] >= vol_3[i-1] * 0.9 for i in range(1, 3))

            if is_breakout_confirmed and is_vol_surge:
                score = (close[-1] / high_n - 1) * 10 + (vol_today / vol_5 - 1)
                reason = f"突破{self.breakout_period}日新高(2日确认)+放量{vol_today/vol_5:.1f}倍"
                signals.append((code, score, reason))
            elif is_breakout and vol_increasing:
                score = (close[-1] / high_n - 1) * 5
                signals.append((code, score, "突破新高+持续放量[待确认]"))

        signals.sort(key=lambda x: x[1], reverse=True)
        return signals[:10]


class PullbackStrategy(BaseStrategy):
    """
    回调买入策略（新增 - 减少追涨杀跌）:
    - MA20仍向上（趋势未破）
    - 从近期高点回落5-10%（回调到位）
    - 缩量企稳（近2日量缩至5日均量的70%以下）
    - 价格在MA20上方或附近（未破位）

    核心逻辑: 不追涨，等回调到位再买
    """

    def __init__(self, drawdown_min: float = 0.05, drawdown_max: float = 0.12,
                 vol_shrink_ratio: float = 0.7):
        super().__init__("pullback")
        self.drawdown_min = drawdown_min
        self.drawdown_max = drawdown_max
        self.vol_shrink_ratio = vol_shrink_ratio

    def generate_signals(self, data_dict: dict, date: str) -> list:
        signals = []

        for code, df in data_dict.items():
            df_cut = df[df["date"] <= date]
            if len(df_cut) < 60:
                continue

            close = df_cut["close"].values
            volume = df_cut["volume"].values
            n = len(close)

            # 条件1: MA20向上（趋势未破）
            ma20 = np.mean(close[-20:])
            ma20_prev = np.mean(close[-25:-5]) if n >= 25 else ma20
            ma20_up = ma20 > ma20_prev * 1.005  # MA20至少上升0.5%

            if not ma20_up:
                continue

            # 条件2: 从20日高点回落5-12%
            high_20 = np.max(close[-20:])
            drawdown = (high_20 - close[-1]) / high_20 if high_20 > 0 else 0

            if not (self.drawdown_min <= drawdown <= self.drawdown_max):
                continue

            # 条件3: 缩量企稳（近2日量 < 5日均量 * 0.7）
            vol_2 = np.mean(volume[-2:])
            vol_5 = np.mean(volume[-5:])
            vol_shrink = vol_2 < vol_5 * self.vol_shrink_ratio

            # 条件4: 价格在MA20上方或附近(不超3%下方)
            near_ma20 = close[-1] >= ma20 * 0.97

            if vol_shrink and near_ma20:
                # 得分: 回调越接近理想区间(5-8%)得分越高
                ideal_dd = 0.07
                dd_score = 1 - abs(drawdown - ideal_dd) / ideal_dd
                score = max(0.1, dd_score)
                reason = (f"回调{drawdown:.1%}到位+MA20向上+"
                          f"缩量企稳[冷静期1天]")
                signals.append((code, score, reason))

        signals.sort(key=lambda x: x[1], reverse=True)
        return signals[:10]


# ============================================================
# 策略工厂
# ============================================================

def get_all_strategies() -> list:
    """获取所有策略实例"""
    return [
        MomentumStrategy(),
        MeanReversionStrategy(),
        EventStrategy(),
        PullbackStrategy(),  # 新增回调策略
    ]
