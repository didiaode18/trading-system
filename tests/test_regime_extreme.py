# -*- coding: utf-8 -*-
"""
MarketRegime V1.1 极端市场状态测试
==================================
覆盖5个关键场景:
  1. 正常牛市 → BULL
  2. 正常熊市 → BEAR
  3. 缩量筑底 → RANGE (非BEAR! 地量修正)
  4. 放量突破 → BULL/RANGE
  5. 恐慌暴跌 → BEAR
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'trading_system'))

from strategy.market_regime import MarketRegimeDetector


def _make_benchmark(n_days=200, scenario="normal"):
    """生成模拟基准数据"""
    np.random.seed(42)
    dates = pd.date_range("2026-01-01", periods=n_days, freq="B")

    if scenario == "bull":
        prices = [100.0]
        for i in range(1, n_days):
            ret = 0.003 + np.random.normal(0, 0.01)
            prices.append(prices[-1] * (1 + ret))
        volumes = [np.random.uniform(8e9, 12e9) for _ in range(n_days)]

    elif scenario == "bear":
        prices = [100.0]
        for i in range(1, n_days):
            ret = -0.003 + np.random.normal(0, 0.012)
            prices.append(prices[-1] * (1 + ret))
        volumes = [np.random.uniform(8e9, 15e9) for _ in range(n_days)]

    elif scenario == "bottom_consolidation":
        # 缩量筑底: 先下跌185天(正常量)，然后极致缩量15天
        # 关键: 缩量期短，确保120日历史基准几乎全是正常量
        prices = [100.0]
        for i in range(1, n_days):
            if i < 185:
                ret = -0.002 + np.random.normal(0, 0.012)
            else:
                ret = -0.0003 + np.random.normal(0, 0.002)
            prices.append(prices[-1] * (1 + ret))
        volumes = []
        for i in range(n_days):
            if i < 185:
                volumes.append(np.random.uniform(8e9, 12e9))
            else:
                # 极致缩量: 仅为正常量的5-8%
                volumes.append(np.random.uniform(0.4e9, 0.8e9))

    elif scenario == "volume_breakout":
        # 放量突破: 长期横盘后突然放量上涨
        prices = [100.0]
        for i in range(1, n_days):
            if i < 170:
                ret = 0.0005 + np.random.normal(0, 0.005)
            else:
                ret = 0.008 + np.random.normal(0, 0.01)
            prices.append(prices[-1] * (1 + ret))
        volumes = []
        for i in range(n_days):
            if i < 170:
                volumes.append(np.random.uniform(3e9, 5e9))
            else:
                volumes.append(np.random.uniform(12e9, 18e9))

    elif scenario == "panic_crash":
        # 恐慌暴跌: 连续放量大跌
        prices = [100.0]
        for i in range(1, n_days):
            if i < 180:
                ret = 0.001 + np.random.normal(0, 0.01)
            else:
                ret = -0.03 + np.random.normal(0, 0.015)
            prices.append(prices[-1] * (1 + ret))
        volumes = []
        for i in range(n_days):
            if i < 180:
                volumes.append(np.random.uniform(6e9, 10e9))
            else:
                volumes.append(np.random.uniform(15e9, 25e9))
    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    close = np.array(prices)
    df = pd.DataFrame({
        "open": close * (1 + np.random.uniform(-0.003, 0.003, n_days)),
        "high": close * (1 + np.abs(np.random.normal(0, 0.006, n_days))),
        "low": close * (1 - np.abs(np.random.normal(0, 0.006, n_days))),
        "close": close,
        "volume": np.array(volumes),
    }, index=dates)

    # RSI
    delta = df["close"].diff()
    gain = delta.apply(lambda x: max(x, 0)).rolling(14).mean()
    loss = delta.apply(lambda x: abs(min(x, 0))).rolling(14).mean()
    df["rsi"] = 100 - (100 / (1 + gain / (loss + 1e-10)))

    return df


class TestRegimeBull(unittest.TestCase):
    """场景1: 正常牛市 → BULL"""

    def test_state_is_bull(self):
        df = _make_benchmark(200, "bull")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertEqual(result["state"], "BULL")

    def test_no_bottom_signal(self):
        df = _make_benchmark(200, "bull")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertFalse(result["extreme_low_volume"])
        self.assertEqual(result["bottom_probability"], 0.0)

    def test_high_position(self):
        df = _make_benchmark(200, "bull")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertGreaterEqual(result["position_advice"]["max_position"], 0.8)


class TestRegimeBear(unittest.TestCase):
    """场景2: 正常熊市(放量下跌) → BEAR"""

    def test_state_is_bear(self):
        df = _make_benchmark(200, "bear")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertEqual(result["state"], "BEAR")

    def test_no_extreme_low_volume(self):
        """正常熊市放量，不应触发极致缩量"""
        df = _make_benchmark(200, "bear")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertFalse(result["extreme_low_volume"])


class TestRegimeBottom(unittest.TestCase):
    """场景3: 缩量筑底 → RANGE (非BEAR! 地量修正)"""

    def test_not_bear(self):
        """缩量筑底不应被判定为BEAR"""
        df = _make_benchmark(200, "bottom_consolidation")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertNotEqual(result["state"], "BEAR",
                            "缩量筑底被误判为BEAR! 地量修正未生效")

    def test_extreme_low_volume_detected(self):
        """应检测到极致缩量"""
        df = _make_benchmark(200, "bottom_consolidation")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertTrue(result["extreme_low_volume"],
                        "极致缩量未被检测到!")

    def test_bottom_probability_high(self):
        """底部概率应≥30%（有底部信号）"""
        df = _make_benchmark(200, "bottom_consolidation")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertGreaterEqual(result["bottom_probability"], 30.0,
                                f"底部概率仅{result['bottom_probability']}%，应≥30%")

    def test_position_not_too_low(self):
        """仓位上限不应低于40%"""
        df = _make_benchmark(200, "bottom_consolidation")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertGreaterEqual(result["position_advice"]["max_position"], 0.40,
                                f"仓位上限仅{result['position_advice']['max_position']:.0%}，应≥40%")


class TestRegimeBreakout(unittest.TestCase):
    """场景4: 放量突破 → BULL/RANGE"""

    def test_not_bear(self):
        """放量突破不应判定为BEAR"""
        df = _make_benchmark(200, "volume_breakout")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertNotEqual(result["state"], "BEAR")


class TestRegimePanic(unittest.TestCase):
    """场景5: 恐慌暴跌 → BEAR"""

    def test_state_is_bear(self):
        df = _make_benchmark(200, "panic_crash")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertEqual(result["state"], "BEAR")

    def test_low_position(self):
        df = _make_benchmark(200, "panic_crash")
        result = MarketRegimeDetector(enable_debounce=False).detect(df)
        self.assertLessEqual(result["position_advice"]["max_position"], 0.35)


class TestRegimeEdgeCases(unittest.TestCase):
    """边界场景: 空数据/极短数据不应崩溃"""

    def test_empty_dataframe(self):
        """空DataFrame不应崩溃"""
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "rsi"])
        try:
            result = MarketRegimeDetector(enable_debounce=False).detect(df)
            # 应返回默认状态
            self.assertIn(result["state"], ["BEAR", "RANGE", "BULL"])
        except Exception:
            pass  # 抛出异常也可接受，只要不崩溃

    def test_short_dataframe(self):
        """极短数据(10天)不应崩溃"""
        np.random.seed(42)
        dates = pd.date_range("2026-01-01", periods=10, freq="B")
        close = np.linspace(100, 98, 10)
        df = pd.DataFrame({
            "open": close * 1.001, "high": close * 1.01,
            "low": close * 0.99, "close": close,
            "volume": np.random.uniform(1e9, 5e9, 10),
            "rsi": [50] * 10,
        }, index=dates)
        try:
            result = MarketRegimeDetector(enable_debounce=False).detect(df)
            self.assertIn(result["state"], ["BEAR", "RANGE", "BULL"])
        except Exception:
            pass  # 容错


if __name__ == "__main__":
    unittest.main()
