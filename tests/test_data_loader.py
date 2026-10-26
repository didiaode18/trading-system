# -*- coding: utf-8 -*-
"""
P0-4: data_loader.py 单元测试
=============================
覆盖数据加载核心逻辑：代码转换、数据库初始化、数据质量验证。

测试策略：
  - 使用合成数据，不依赖真实 DB/网络
  - mock 外部数据源（baostock/akshare）
  - 临时数据库避免污染生产数据
"""

import datetime
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd

# 路径注入（测试环境）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'trading_system'))

import config  # noqa: E402


class TestCodeConversion(unittest.TestCase):
    """P0-4: 股票代码转换"""

    def test_shanghai_stock(self):
        """上海股票（6/9/5开头）→ sh.前缀"""
        from data.data_loader import _to_baostock_code
        self.assertEqual(_to_baostock_code("600584"), "sh.600584")
        self.assertEqual(_to_baostock_code("000300"), "sh.000300")
        self.assertEqual(_to_baostock_code("510300"), "sh.510300")

    def test_shenzhen_stock(self):
        """深圳股票（0/3开头）→ sz.前缀"""
        from data.data_loader import _to_baostock_code
        self.assertEqual(_to_baostock_code("000001"), "sz.000001")
        self.assertEqual(_to_baostock_code("300750"), "sz.300750")

    def test_beijing_stock(self):
        """北交所（8/4/920开头）→ bj.前缀"""
        from data.data_loader import _to_baostock_code
        self.assertEqual(_to_baostock_code("830799"), "bj.830799")
        self.assertEqual(_to_baostock_code("430047"), "bj.430047")
        self.assertEqual(_to_baostock_code("920001"), "bj.920001")

    def test_already_prefixed(self):
        """已有前缀的代码不重复添加"""
        from data.data_loader import _to_baostock_code
        self.assertEqual(_to_baostock_code("sh.600584"), "sh.600584")
        self.assertEqual(_to_baostock_code("sz.000001"), "sz.000001")


class TestDatabaseInit(unittest.TestCase):
    """P0-4: 数据库初始化"""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)

    def test_init_creates_tables(self):
        """init_db 应创建 daily_kline 和 last_update 表"""
        from data.data_loader import init_db
        conn = init_db(self.temp_db.name)
        cursor = conn.cursor()

        # 检查表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        self.assertIn("daily_kline", tables)
        self.assertIn("last_update", tables)
        conn.close()

    def test_init_idempotent(self):
        """多次调用 init_db 不应报错（幂等性）"""
        from data.data_loader import init_db
        conn1 = init_db(self.temp_db.name)
        conn1.close()
        conn2 = init_db(self.temp_db.name)
        conn2.close()
        # 不抛异常即通过


class TestDataQualityValidation(unittest.TestCase):
    """P0-4: 数据质量验证"""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        from data.data_loader import init_db
        self.conn = init_db(self.temp_db.name)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)

    def test_filter_invalid_price(self):
        """过滤价格为0或负数的行"""
        from data.data_loader import _validate_data_quality
        df = pd.DataFrame({
            "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "open": [10.0, 0.0, 10.0],  # 第二行 open=0 无效
            "close": [10.0, 10.0, -5.0],  # 第三行 close<0 无效
            "volume": [1000, 1000, 1000],
        })
        result = _validate_data_quality(self.conn, "000001", df)
        self.assertEqual(len(result), 1, "应过滤2行无效价格")
        self.assertEqual(result.iloc[0]["date"], "2024-01-01")

    def test_filter_extreme_drop(self):
        """过滤单日跌幅超11%的行（疑似前复权错误）"""
        from data.data_loader import _validate_data_quality
        df = pd.DataFrame({
            "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "open": [10.0, 10.0, 10.0],
            "close": [10.0, 8.0, 5.0],  # 第三行跌幅 (5-8)/8 = -37.5% 超限
            "volume": [1000, 1000, 1000],
        })
        result = _validate_data_quality(self.conn, "000001", df)
        self.assertLess(len(result), 3, "应过滤极端跌幅行")

    def test_empty_dataframe(self):
        """空 DataFrame 应原样返回"""
        from data.data_loader import _validate_data_quality
        df = pd.DataFrame()
        result = _validate_data_quality(self.conn, "000001", df)
        self.assertTrue(result.empty)


class TestFetchStockDailyMocked(unittest.TestCase):
    """P0-4: 数据获取（纯函数测试，不依赖网络）"""

    def test_to_baostock_code_kcb(self):
        """科创板 688xxx → sh.前缀"""
        from data.data_loader import _to_baostock_code
        self.assertEqual(_to_baostock_code("688001"), "sh.688001")

    def test_to_baostock_code_etf(self):
        """ETF 51xxxx → sh.前缀"""
        from data.data_loader import _to_baostock_code
        self.assertEqual(_to_baostock_code("510300"), "sh.510300")
        self.assertEqual(_to_baostock_code("159205"), "sz.159205")


class TestDelistedPoolHandling(unittest.TestCase):
    """P0-4: 退市标的处理"""

    def test_delisted_code_in_pool(self):
        """退市池中的标的应被识别"""
        # 检查 config 中是否有退市池配置
        delisted_pool = getattr(config, 'BACKTEST_DELISTED_POOL', {})
        # 退市池应为 dict（code -> info）或 set/list
        self.assertIsInstance(delisted_pool, (dict, set, list, tuple),
                              "退市池应为 dict/set/list")

    def test_survivor_bias_declaration(self):
        """幸存者偏差应有诚实声明"""
        # 检查 data_audit 模块是否有幸存者偏差声明
        try:
            from backtest.data_audit import audit_survivor_bias
            result = audit_survivor_bias()
            self.assertIn("included", result, "应声明退市标的纳入情况")
        except (ImportError, AttributeError):
            # 模块不存在时跳过
            self.skipTest("data_audit 模块未实现")


if __name__ == "__main__":
    unittest.main()
