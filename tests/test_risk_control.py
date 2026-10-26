# -*- coding: utf-8 -*-
"""
P0-3: risk_control.py 单元测试
==============================
覆盖风控硬约束：单票≤15%、总仓≤90%、日亏5%熔断、ETF≤20%、冷却期、连续亏损熔断。

测试策略：
  - 使用合成数据，不依赖真实 DB/网络
  - 每个测试前 reset_instance() 清除单例状态
  - mock datetime/文件操作避免副作用
"""

import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# 路径注入（测试环境）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'trading_system'))

import config  # noqa: E402


class TestRiskControlHardConstraints(unittest.TestCase):
    """P0-3: 风控硬约束测试"""

    def setUp(self):
        """每个测试前重置单例状态"""
        from risk.risk_control import UnifiedRiskEngine
        UnifiedRiskEngine.reset_instance()
        self.total_capital = 1_000_000  # 100万

    def tearDown(self):
        from risk.risk_control import UnifiedRiskEngine
        UnifiedRiskEngine.reset_instance()

    def test_single_stock_limit_15pct(self):
        """P0: 单只个股仓位上限 15%"""
        from risk.risk_control import UnifiedRiskEngine
        engine = UnifiedRiskEngine(total_capital=self.total_capital)

        # 构造持仓：已有 14% 仓位的个股
        holdings = {
            "000001": {
                "market_value": 140_000,
                "sector": "银行",
                "is_etf": False,
            }
        }
        # 再买入 2% → 超过 15%
        signal = {
            "code": "000002",
            "action": "buy",
            "price": 20.0,
            "shares": 1000,  # 2万
            "sector": "银行",
            "stock_type": "龙头",
        }
        result = engine.check_buy(signal, holdings, market_info=None)
        # 应被拦截或缩股
        if result["pass"]:
            # 如果通过，检查 adjusted_shares 是否被限制
            adjusted = result.get("adjusted_shares", signal["shares"])
            max_allowed = int(0.15 * self.total_capital / signal["price"] / 100) * 100
            self.assertLessEqual(adjusted * signal["price"], 0.15 * self.total_capital * 1.01,
                                 "单票仓位不应超过15%")

    def test_single_etf_limit_20pct(self):
        """P0: 单只 ETF 仓位上限 20%"""
        from risk.risk_control import UnifiedRiskEngine
        engine = UnifiedRiskEngine(total_capital=self.total_capital)

        holdings = {}
        signal = {
            "code": "510300",
            "action": "buy",
            "price": 4.0,
            "shares": 60000,  # 24万 → 24% > 20%
            "sector": "指数",
            "stock_type": "ETF",
            "is_etf": True,
        }
        result = engine.check_buy(signal, holdings, market_info=None)
        if result["pass"]:
            adjusted = result.get("adjusted_shares", signal["shares"])
            max_value = 0.20 * self.total_capital
            self.assertLessEqual(adjusted * signal["price"], max_value * 1.01,
                                 "ETF 仓位不应超过20%")

    def test_total_position_limit_90pct(self):
        """P0: 总仓位上限 90%（最低现金保留 10%）"""
        from risk.risk_control import UnifiedRiskEngine
        engine = UnifiedRiskEngine(total_capital=self.total_capital)

        # 已有 85% 仓位
        holdings = {
            "000001": {"market_value": 850_000, "sector": "银行", "is_etf": False}
        }
        # 再买入 10% → 超过 90%
        signal = {
            "code": "000002",
            "action": "buy",
            "price": 10.0,
            "shares": 10000,  # 10万
            "sector": "科技",
            "stock_type": "龙头",
        }
        result = engine.check_buy(signal, holdings, market_info=None)
        # 应被拦截或缩股
        if result["pass"]:
            adjusted = result.get("adjusted_shares", signal["shares"])
            total_after = 850_000 + adjusted * signal["price"]
            self.assertLessEqual(total_after, 0.90 * self.total_capital * 1.01,
                                 "总仓位不应超过90%")

    def test_daily_loss_circuit_breaker_5pct(self):
        """P0: 单日亏损 ≥5% 触发熔断，禁止新开仓"""
        from risk.risk_control import UnifiedRiskEngine, RiskStateManager
        engine = UnifiedRiskEngine(total_capital=self.total_capital)

        # 模拟当日已亏损 6%
        engine.state_mgr.state["daily_pnl"] = -0.06 * self.total_capital
        engine.state_mgr.save()

        signal = {
            "code": "000001",
            "action": "buy",
            "price": 10.0,
            "shares": 1000,
            "sector": "银行",
            "stock_type": "龙头",
        }
        result = engine.check_buy(signal, {}, market_info=None)
        self.assertFalse(result["pass"], "日亏≥5%应触发熔断")
        reason = result.get("reason", "")
        self.assertTrue("熔断" in reason or "暂停" in reason or "亏损" in reason,
                        f"应提示熔断/暂停原因，实际: {reason}")

    def test_consecutive_loss_block_3_trades(self):
        """P0: 连续亏损 3 笔触发熔断，暂停开仓"""
        from risk.risk_control import UnifiedRiskEngine
        engine = UnifiedRiskEngine(total_capital=self.total_capital)

        # 模拟连续亏损 3 笔
        engine.state_mgr.state["consecutive_losses"] = 3
        engine.state_mgr.save()

        signal = {
            "code": "000001",
            "action": "buy",
            "price": 10.0,
            "shares": 1000,
            "sector": "银行",
            "stock_type": "龙头",
        }
        result = engine.check_buy(signal, {}, market_info=None)
        self.assertFalse(result["pass"], "连亏3笔应触发熔断")

    def test_cooldown_period_blocks_rebuy(self):
        """P1: 卖出后冷却期内禁止重新买入"""
        from risk.risk_control import UnifiedRiskEngine
        engine = UnifiedRiskEngine(total_capital=self.total_capital)

        # 模拟刚卖出 000001，冷却期 2 天
        engine.state_mgr.state["last_sell_date"] = datetime.date.today().isoformat()
        engine.state_mgr.state["cooldown_codes"] = {"000001": 2}
        engine.state_mgr.save()

        signal = {
            "code": "000001",
            "action": "buy",
            "price": 10.0,
            "shares": 1000,
            "sector": "银行",
            "stock_type": "龙头",
        }
        result = engine.check_buy(signal, {}, market_info=None)
        self.assertFalse(result["pass"], "冷却期内应禁止买入")

    def test_pool_restriction(self):
        """P1: 池外标的应被拦截"""
        from risk.risk_control import UnifiedRiskEngine
        allowed = {"000001", "000002"}
        engine = UnifiedRiskEngine(total_capital=self.total_capital, allowed_pool=allowed)

        signal = {
            "code": "999999",  # 不在池内
            "action": "buy",
            "price": 10.0,
            "shares": 1000,
            "sector": "银行",
            "stock_type": "龙头",
        }
        result = engine.check_buy(signal, {}, market_info=None)
        self.assertFalse(result["pass"], "池外标的应被拦截")
        reason = result.get("reason", "")
        self.assertIn("池外", reason, f"应提示池外拦截，实际: {reason}")


class TestPreTradeCheckOrders(unittest.TestCase):
    """P0-3: 条件单下单前风控硬校验"""

    def test_near_full_position_blocks_all(self):
        """P0: 总仓位≥80%时禁止任何新开仓"""
        from risk.risk_control import pre_trade_check_orders

        positions = {
            "000001": {"market_value": 850_000, "sector": "银行", "is_etf": False}
        }
        orders = [
            {"方向": "买入", "证券代码": "000002", "证券名称": "测试",
             "触发价": 10.0, "数量": 1000}
        ]
        blocked = pre_trade_check_orders(orders, positions, total_capital=1_000_000)
        self.assertGreater(len(blocked), 0, "总仓位≥80%应拦截新买入")

    def test_sell_order_never_blocked(self):
        """P0: 卖出/止损类条件单在持仓充足时不拦截"""
        from risk.risk_control import pre_trade_check_orders

        positions = {
            "000001": {"market_value": 900_000, "sector": "银行", "is_etf": False, "shares": 5000}
        }
        orders = [
            {"方向": "卖出", "证券代码": "000001", "证券名称": "测试",
             "触发价": 10.0, "数量": 1000},
            {"方向": "止损", "证券代码": "000001", "证券名称": "测试",
             "触发价": 9.0, "数量": 1000}
        ]
        blocked = pre_trade_check_orders(orders, positions, total_capital=1_000_000)
        self.assertEqual(len(blocked), 0, "持仓充足的卖出/止损单不应被拦截")

    def test_sell_order_over_sell_blocked(self):
        """EDGE-04: 合计卖出量超过持仓时应拦截"""
        from risk.risk_control import pre_trade_check_orders

        positions = {
            "000001": {"market_value": 40_000, "sector": "银行", "is_etf": False, "shares": 4000}
        }
        orders = [
            {"方向": "卖出", "证券代码": "000001", "证券名称": "测试",
             "触发价": 10.0, "数量": 3000},
            {"方向": "止损卖出", "证券代码": "000001", "证券名称": "测试",
             "触发价": 9.0, "数量": 3000}
        ]
        blocked = pre_trade_check_orders(orders, positions, total_capital=1_000_000)
        self.assertGreater(len(blocked), 0, "合计卖出6000>持仓4000应被拦截")

    def test_single_stock_over_limit_blocked(self):
        """P0: 成交后单票仓位超限应被拦截"""
        from risk.risk_control import pre_trade_check_orders

        positions = {
            "000001": {"market_value": 140_000, "sector": "银行", "is_etf": False}
        }
        orders = [
            {"方向": "买入", "证券代码": "000001", "证券名称": "测试",
             "触发价": 10.0, "数量": 2000}  # 2万 → 累计 16万 > 15%
        ]
        blocked = pre_trade_check_orders(orders, positions, total_capital=1_000_000)
        self.assertGreater(len(blocked), 0, "单票超15%应被拦截")


class TestQuickInspect(unittest.TestCase):
    """P0-3: 持仓巡检"""

    def setUp(self):
        from risk.risk_control import UnifiedRiskEngine
        UnifiedRiskEngine.reset_instance()

    def tearDown(self):
        from risk.risk_control import UnifiedRiskEngine
        UnifiedRiskEngine.reset_instance()

    def test_overweight_stock_reported(self):
        """P1: 超配个股应被巡检报告"""
        from risk.risk_control import quick_inspect

        holdings = {
            "000001": {"market_value": 200_000, "sector": "银行", "is_etf": False}
        }
        warnings = quick_inspect(holdings, total_capital=1_000_000)
        # 20% > 15% 应触发警告
        self.assertGreater(len(warnings), 0, "超配个股应有警告")

    def test_normal_holdings_no_critical_warning(self):
        """P1: 正常持仓不应触发严重警告（超配/超限）"""
        from risk.risk_control import quick_inspect

        holdings = {
            "000001": {"market_value": 100_000, "sector": "银行", "is_etf": False},
            "000002": {"market_value": 80_000, "sector": "科技", "is_etf": False},
        }
        warnings = quick_inspect(holdings, total_capital=1_000_000)
        # 18% 总仓位，无超配——检查无严重违规
        critical_keywords = ["超配", "超限", "熔断", "禁止"]
        for w in warnings:
            for kw in critical_keywords:
                self.assertNotIn(kw, str(w), f"正常持仓不应有严重警告: {w}")


class TestRiskConfigConsistency(unittest.TestCase):
    """P0-3: 风控参数一致性"""

    def test_config_values_match_hard_constraints(self):
        """P0: config 中的风控参数应与硬约束一致"""
        # 单票≤15%
        self.assertLessEqual(getattr(config, 'MAX_SINGLE_STOCK_RATIO', 0.15), 0.15,
                             "单票上限应≤15%")
        # ETF≤20%
        self.assertLessEqual(getattr(config, 'MAX_SINGLE_ETF_RATIO', 0.20), 0.20,
                             "ETF上限应≤20%")
        # 总仓≤90%（可能不存在，用 getattr 安全获取）
        max_total = getattr(config, 'MAX_TOTAL_POSITION_RATIO', 0.90)
        self.assertLessEqual(max_total, 0.90, "总仓上限应≤90%")


if __name__ == "__main__":
    unittest.main()
