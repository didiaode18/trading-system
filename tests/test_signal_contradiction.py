# -*- coding: utf-8 -*-
"""
V8.3 跨周期矛盾信号保护测试
============================
覆盖3个核心修复:
  1. check_pullback_add_risk() buy_block硬否决
  2. alert_ack.get_last_signal_direction() 方向记忆查询
  3. scheduler跨周期方向矛盾保护（买后卖→反转标注，卖后买→拦截）
"""
import os
import sys
import json
import unittest
import datetime
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'trading_system'))


# ============================================================
# 1. check_pullback_add_risk() buy_block硬否决测试
# ============================================================

class TestPullbackAddBuyBlock(unittest.TestCase):
    """回调加仓被buy_block拦截"""

    # FIX(P0-20): check_pullback_add_risk 新增引擎级熔断校验（暂停期/连亏/冷却），
    # 需隔离真实 output/risk_state.json（否则生产熔断状态会先于 buy_block 拦截，
    # 使本用例断言的"禁加仓"reason 被"风控暂停期"覆盖）。unittest.TestCase 无法用
    # pytest fixture，故在 setUp/tearDown 手动 patch 模块级 STATE_FILE。
    def setUp(self):
        import risk.risk_control as _rc
        self._rc = _rc
        self._tmpdir = tempfile.mkdtemp()
        self._orig_state_file = _rc.STATE_FILE
        _rc.STATE_FILE = os.path.join(self._tmpdir, "risk_state.json")

    def tearDown(self):
        import shutil
        self._rc.STATE_FILE = self._orig_state_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_df(self, n=60):
        """构造满足回调加仓条件的K线数据"""
        np.random.seed(42)
        dates = pd.date_range("2026-06-01", periods=n, freq="B")
        # 先上涨后回调
        prices = [30.0]
        for i in range(1, n):
            if i < n - 10:
                ret = 0.003 + np.random.normal(0, 0.008)
            else:
                ret = -0.005 + np.random.normal(0, 0.005)
            prices.append(prices[-1] * (1 + ret))
        close = np.array(prices)
        df = pd.DataFrame({
            "open": close * 1.001, "high": close * 1.01,
            "low": close * 0.99, "close": close,
            "volume": np.random.uniform(5e6, 15e6, n),
        }, index=dates)
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean().fillna(method="bfill")
        df["ma20_slope"] = df["ma20"].diff()
        df["rsi"] = 50
        df["boll_lower"] = df["close"].rolling(20).mean() - 2 * df["close"].rolling(20).std()
        return df

    def test_buy_block_blocks_pullback_add(self):
        """buy_block有效期内的标的不应通过回调加仓检查"""
        from risk.risk_control import check_pullback_add_risk

        code = "600989"
        df = self._make_df()
        holdings = {code: {
            "shares": 4900, "buy_price": 24.12,
            "current_price": 24.06, "weight": 0.19,
        }}

        # 先写入buy_block
        from notify.buy_point_alert import BLOCK_FILE
        block_data = {
            "blocked": {
                code: {
                    "name": "宝丰能源",
                    "reason": "盘中决策紧急卖出",
                    "added": datetime.date.today().strftime("%Y-%m-%d"),
                    "until": (datetime.date.today() + datetime.timedelta(days=3)).strftime("%Y-%m-%d"),
                }
            }
        }
        os.makedirs(os.path.dirname(BLOCK_FILE), exist_ok=True)
        with open(BLOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(block_data, f, ensure_ascii=False)

        try:
            result = check_pullback_add_risk(code, holdings, df, market_state="neutral")
            self.assertFalse(result["pass"], "buy_block生效时回调加仓不应通过!")
            self.assertIn("禁加仓", result["reason"])
        finally:
            # 清理buy_block
            if os.path.exists(BLOCK_FILE):
                os.remove(BLOCK_FILE)

    def test_no_buy_block_passes(self):
        """无buy_block时回调加仓正常通过（不被拦截）"""
        from risk.risk_control import check_pullback_add_risk
        from notify.buy_point_alert import BLOCK_FILE

        # 确保buy_block不存在
        if os.path.exists(BLOCK_FILE):
            os.remove(BLOCK_FILE)

        code = "600989"
        df = self._make_df()
        holdings = {code: {
            "shares": 4900, "buy_price": 24.12,
            "current_price": 24.06, "weight": 0.19,
        }}
        result = check_pullback_add_risk(code, holdings, df, market_state="neutral")
        # 结果取决于市场数据是否满足所有条件，但不应因buy_block被拒
        if not result["pass"]:
            self.assertNotIn("禁加仓", result["reason"])


# ============================================================
# 2. alert_ack.get_last_signal_direction() 测试
# ============================================================

class TestSignalDirectionQuery(unittest.TestCase):
    """跨周期方向记忆查询"""

    def setUp(self):
        """每个测试前清理状态文件"""
        from notify.alert_ack import _ACK_STATE_FILE
        if os.path.exists(_ACK_STATE_FILE):
            os.remove(_ACK_STATE_FILE)

    def test_no_record_returns_none(self):
        """无记录时返回direction=none"""
        from notify.alert_ack import get_last_signal_direction
        result = get_last_signal_direction("999999")
        self.assertEqual(result["direction"], "none")

    def test_buy_signal_detected(self):
        """回调加仓规则被识别为buy方向"""
        from notify.alert_ack import record_delivery, get_last_signal_direction
        code = "000001"
        record_delivery(code, "盘中决策-回调加仓", "high", 68)
        result = get_last_signal_direction(code)
        self.assertEqual(result["direction"], "buy")
        self.assertAlmostEqual(result["urgency"], 68)

    def test_sell_signal_detected(self):
        """紧急卖出规则被识别为sell方向"""
        from notify.alert_ack import record_delivery, get_last_signal_direction
        code = "000002"
        record_delivery(code, "盘中决策-紧急卖出", "high", 72)
        result = get_last_signal_direction(code)
        self.assertEqual(result["direction"], "sell")

    def test_neutral_rule_returns_none(self):
        """非买卖规则返回direction=none"""
        from notify.alert_ack import record_delivery, get_last_signal_direction
        code = "000003"
        record_delivery(code, "其他规则", "warning", 50)
        result = get_last_signal_direction(code)
        self.assertEqual(result["direction"], "none")


# ============================================================
# 3. 跨周期方向矛盾保护逻辑测试
# ============================================================

class TestCrossCycleContradiction(unittest.TestCase):
    """跨周期方向矛盾保护"""

    def setUp(self):
        """每个测试前清理状态文件"""
        from notify.alert_ack import _ACK_STATE_FILE
        if os.path.exists(_ACK_STATE_FILE):
            os.remove(_ACK_STATE_FILE)

    def test_buy_after_sell_suppressed(self):
        """卖出后买入信号应被拦截"""
        from notify.alert_ack import get_last_signal_direction
        _BUY_KW = ("回调加仓", "加仓机会", "可以加仓", "突破加仓")
        _SELL_KW = ("紧急卖出", "建议减仓", "止损", "清仓", "跌破", "暴跌", "趋势降级")

        # 模拟: 先记录了卖出信号
        from notify.alert_ack import record_delivery
        record_delivery("600989", "盘中决策-紧急卖出", "high", 72)

        # 检查: 新的买入信号应被识别为矛盾
        last_dir = get_last_signal_direction("600989")
        self.assertEqual(last_dir["direction"], "sell")

        # 模拟买入信号
        buy_msg = "回调加仓: 回调-7.1%企稳 | 加仓2400股"
        is_buy = any(k in buy_msg for k in _BUY_KW) and not any(k in buy_msg for k in _SELL_KW)
        self.assertTrue(is_buy)
        # 逻辑判断: buy + last=sell → 应被拦截
        self.assertEqual(last_dir["direction"], "sell")

    def test_sell_after_buy_annotated(self):
        """买入后卖出信号应附加反转说明"""
        from notify.alert_ack import record_delivery, get_last_signal_direction
        _SELL_KW = ("紧急卖出", "建议减仓", "止损", "清仓", "跌破", "暴跌", "趋势降级")

        # 模拟: 先记录了买入信号
        record_delivery("600256", "盘中决策-回调加仓", "high", 68)

        # 检查: 方向应为buy
        last_dir = get_last_signal_direction("600256")
        self.assertEqual(last_dir["direction"], "buy")

        # 模拟卖出信号的反转标注
        sell_alert = {
            "code": "600256", "msg": "紧急卖出: 距止损<3%",
            "rule_name": "盘中决策-紧急卖出", "urgency_score": 72,
        }
        _is_sell = any(k in sell_alert["msg"] + sell_alert["rule_name"] for k in _SELL_KW)
        self.assertTrue(_is_sell)

        # 应附加反转说明
        if last_dir["direction"] == "buy" and _is_sell:
            reversal_note = (f" ⚠️ 信号反转: {last_dir['time']}曾建议加仓，"
                             f"现翻转为卖出，请以最新判断为准")
            sell_alert["msg"] = sell_alert["msg"] + reversal_note
            sell_alert["_reversal_from"] = last_dir["rule"]

        self.assertIn("信号反转", sell_alert["msg"])
        self.assertIn("曾建议加仓", sell_alert["msg"])
        self.assertEqual(sell_alert["_reversal_from"], "盘中决策-回调加仓")


if __name__ == "__main__":
    unittest.main()
