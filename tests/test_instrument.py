# -*- coding: utf-8 -*-
"""
品种类型系统 + 费率/仓位/评分 品种差异测试矩阵
===============================================
覆盖: 个股(002xxx)、ETF(5xxxxx)、可转债(12xxxx)
确保每个涉及"代码"的模块对3种品种类型都有正确处理
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'trading_system'))

from utils.instrument import (
    InstrumentType, get_instrument_type, is_etf, is_stock, is_bond,
    get_stamp_tax_rate, get_max_single_position, get_etf_subtype,
)


class TestInstrumentType(unittest.TestCase):
    """品种类型判定"""

    def test_stock_shenzhen(self):
        """深圳个股: 000xxx/002xxx/003xxx"""
        self.assertEqual(get_instrument_type("000001"), InstrumentType.STOCK)
        self.assertEqual(get_instrument_type("002371"), InstrumentType.STOCK)
        self.assertEqual(get_instrument_type("003032"), InstrumentType.STOCK)
        self.assertTrue(is_stock("002371"))
        self.assertFalse(is_etf("002371"))

    def test_stock_chuangyeban(self):
        """创业板: 300xxx/301xxx"""
        self.assertEqual(get_instrument_type("300750"), InstrumentType.STOCK)
        self.assertEqual(get_instrument_type("301269"), InstrumentType.STOCK)

    def test_stock_shanghai(self):
        """上海个股: 600xxx/601xxx/603xxx/688xxx"""
        self.assertEqual(get_instrument_type("600519"), InstrumentType.STOCK)
        self.assertEqual(get_instrument_type("601318"), InstrumentType.STOCK)
        self.assertEqual(get_instrument_type("688981"), InstrumentType.STOCK)

    def test_etf_shanghai(self):
        """上海ETF: 510xxx/512xxx/562xxx/588xxx"""
        self.assertEqual(get_instrument_type("510210"), InstrumentType.ETF)
        self.assertEqual(get_instrument_type("512660"), InstrumentType.ETF)
        self.assertEqual(get_instrument_type("562590"), InstrumentType.ETF)  # 半导材料ETF
        self.assertEqual(get_instrument_type("588200"), InstrumentType.ETF)
        self.assertTrue(is_etf("562590"))
        self.assertFalse(is_stock("562590"))

    def test_etf_shenzhen(self):
        """深圳ETF: 159xxx"""
        self.assertEqual(get_instrument_type("159205"), InstrumentType.ETF)
        self.assertEqual(get_instrument_type("159915"), InstrumentType.ETF)
        self.assertTrue(is_etf("159205"))

    def test_bond(self):
        """可转债: 113xxx/123xxx/127xxx/128xxx"""
        self.assertEqual(get_instrument_type("113050"), InstrumentType.BOND)
        self.assertEqual(get_instrument_type("123050"), InstrumentType.BOND)
        self.assertEqual(get_instrument_type("127045"), InstrumentType.BOND)
        self.assertEqual(get_instrument_type("128100"), InstrumentType.BOND)
        self.assertTrue(is_bond("123050"))
        self.assertFalse(is_etf("123050"))  # 关键: 可转债不应被误判为ETF

    def test_index(self):
        """指数: 000300/399001等"""
        self.assertEqual(get_instrument_type("000300"), InstrumentType.INDEX)
        self.assertEqual(get_instrument_type("399006"), InstrumentType.INDEX)

    def test_unknown(self):
        """未知品种"""
        self.assertEqual(get_instrument_type(""), InstrumentType.UNKNOWN)
        self.assertEqual(get_instrument_type("99"), InstrumentType.UNKNOWN)


class TestStampTax(unittest.TestCase):
    """印花税: ETF/可转债免印花税，个股收取"""

    def test_stock_stamp_tax(self):
        """个股卖出应收取印花税"""
        rate = get_stamp_tax_rate("002371")
        self.assertGreater(rate, 0)

    def test_etf_stamp_tax(self):
        """ETF免印花税"""
        rate = get_stamp_tax_rate("562590")
        self.assertEqual(rate, 0.0)

    def test_bond_stamp_tax(self):
        """可转债免印花税"""
        rate = get_stamp_tax_rate("123050")
        self.assertEqual(rate, 0.0)


class TestMaxPosition(unittest.TestCase):
    """仓位上限: ETF 20% > 个股 15% > 可转债 10%"""

    def test_stock_max_position(self):
        """个股仓位上限15%"""
        pos = get_max_single_position("002371")
        self.assertAlmostEqual(pos, 0.15, places=2)

    def test_etf_max_position(self):
        """ETF仓位上限20%"""
        pos = get_max_single_position("562590")
        self.assertAlmostEqual(pos, 0.20, places=2)

    def test_bond_max_position(self):
        """可转债仓位上限10%"""
        pos = get_max_single_position("123050")
        self.assertAlmostEqual(pos, 0.10, places=2)


class TestETFSubtype(unittest.TestCase):
    """ETF子类型分类"""

    def test_industry_etf(self):
        """行业ETF: 562xxx"""
        self.assertEqual(get_etf_subtype("562590"), "行业ETF")

    def test_broad_etf(self):
        """宽基ETF: 510xxx"""
        self.assertEqual(get_etf_subtype("510210"), "宽基ETF")

    def test_theme_etf(self):
        """主题ETF: 588xxx/159xxx"""
        self.assertEqual(get_etf_subtype("588200"), "主题ETF")
        self.assertEqual(get_etf_subtype("159205"), "主题ETF")


class TestEdgeCases(unittest.TestCase):
    """边界场景: 零资金/空数据/异常输入"""

    def test_empty_code(self):
        """空代码应返回UNKNOWN"""
        self.assertEqual(get_instrument_type(""), InstrumentType.UNKNOWN)
        self.assertFalse(is_etf(""))
        self.assertFalse(is_stock(""))
        self.assertFalse(is_bond(""))

    def test_short_code(self):
        """短代码应返回UNKNOWN"""
        self.assertEqual(get_instrument_type("5"), InstrumentType.UNKNOWN)
        self.assertEqual(get_instrument_type("12"), InstrumentType.UNKNOWN)

    def test_none_like_code(self):
        """异常代码不应崩溃"""
        # get_max_single_position对未知品种应返回默认值
        pos = get_max_single_position("")
        self.assertGreater(pos, 0)
        pos2 = get_max_single_position("99")
        self.assertGreater(pos2, 0)

    def test_stamp_tax_unknown_instrument(self):
        """未知品种印花税应返回默认值"""
        rate = get_stamp_tax_rate("")
        self.assertGreaterEqual(rate, 0)

    def test_etf_subtype_non_etf(self):
        """非ETF品种应返回空字符串"""
        self.assertEqual(get_etf_subtype("002371"), "")
        self.assertEqual(get_etf_subtype(""), "")


class TestBrokerStampTax(unittest.TestCase):
    """回测引擎印花税: 验证broker.py正确使用instrument模块"""

    def test_broker_etf_no_stamp_tax(self):
        """broker卖出ETF不应收取印花税"""
        from backtest.broker import CostConfig, SimBroker
        cost = CostConfig()
        broker = SimBroker(cost)
        # ETF卖出
        tax = broker._calc_stamp_tax(100000, "sell", code="562590")
        self.assertEqual(tax, 0.0)

    def test_broker_stock_stamp_tax(self):
        """broker卖出个股应收取印花税"""
        from backtest.broker import CostConfig, SimBroker
        cost = CostConfig()
        broker = SimBroker(cost)
        tax = broker._calc_stamp_tax(100000, "sell", code="002371")
        self.assertGreater(tax, 0)

    def test_broker_bond_no_stamp_tax(self):
        """broker卖出可转债不应收取印花税"""
        from backtest.broker import CostConfig, SimBroker
        cost = CostConfig()
        broker = SimBroker(cost)
        tax = broker._calc_stamp_tax(100000, "sell", code="123050")
        self.assertEqual(tax, 0.0)


if __name__ == "__main__":
    unittest.main()
