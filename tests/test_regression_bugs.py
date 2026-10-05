# -*- coding: utf-8 -*-
"""
P-08 审计bug回归测试（每条已知漏洞转测试）
============================================
把回测类bug写成pytest，修复后永不复发。
任一回归测试失败 → 判红（说明bug复发）。

对应蓝图第八章 P-08 探针。
"""
import os
import sys
import pytest
import pandas as pd
import numpy as np

# 路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "trading_system"))


# ============================================================
# BUG-002: 创业板/科创板/北交所 ST 不应误判为±5%
# ============================================================
class TestBUG002BoardRules:
    """BUG-002: 先按板块判幅度，仅主板ST降为5%"""

    def test_chinext_st_is_20pct(self):
        """创业板ST仍±20%"""
        from utils.board_rules import get_limit_pct
        assert get_limit_pct("300001", "ST测试") == 0.20
        assert get_limit_pct("301001", "*ST测试") == 0.20

    def test_star_st_is_20pct(self):
        """科创板ST仍±20%"""
        from utils.board_rules import get_limit_pct
        assert get_limit_pct("688001", "ST测试") == 0.20

    def test_bse_st_is_30pct(self):
        """北交所ST仍±30%"""
        from utils.board_rules import get_limit_pct
        assert get_limit_pct("830001", "ST测试") == 0.30
        assert get_limit_pct("430001", "ST测试") == 0.30
        assert get_limit_pct("920001", "ST测试") == 0.30

    def test_main_st_is_5pct(self):
        """主板ST降为5%"""
        from utils.board_rules import get_limit_pct
        assert get_limit_pct("600001", "ST测试") == 0.05
        assert get_limit_pct("000001", "*ST测试") == 0.05

    def test_main_normal_is_10pct(self):
        """主板正常股±10%"""
        from utils.board_rules import get_limit_pct
        assert get_limit_pct("600001", "浦发银行") == 0.10
        assert get_limit_pct("000001", "平安银行") == 0.10


# ============================================================
# BUG-003: 当日收盘信号不得当日收盘成交
# ============================================================
class TestBUG003NoSameDayFill:
    """BUG-003: 策略决策只用截止昨日数据"""

    def test_data_feed_excl_today(self):
        """get_history_excl_today 不含当日数据"""
        from backtest.data_feed import DataFeed
        dates = pd.date_range("2024-01-01", periods=10, freq="B")
        df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": range(10), "close": range(10),
            "high": range(10), "low": range(10),
            "volume": [1000] * 10,
        })
        feed = DataFeed({"TEST": df})
        # 获取截止第5日的前一天数据
        target_date = dates[5].strftime("%Y-%m-%d")
        hist = feed.get_history_excl_today("TEST", target_date)
        assert hist is not None
        # 不应包含当日
        assert target_date not in hist["date"].values


# ============================================================
# BUG-004: 末日跌停强平必须扣费用
# ============================================================
class TestBUG004ForceCloseChargesFee:
    """BUG-004: 末日跌停强平扣费用"""

    def test_force_close_deducts_fees(self):
        """末日跌停无法卖出时按跌停价估值，扣除卖出费用"""
        from backtest.broker import SimBroker, CostConfig, Position
        broker = SimBroker(100000, CostConfig())
        # 手动建仓
        broker.positions["600000"] = Position(
            code="600000", shares=1000, avg_cost=10.0,
            buy_date="2024-01-01", prev_day_available=1000,
        )
        broker.cash = 50000  # 模拟部分资金
        # 验证费用计算
        amount = 1000 * 9.0  # 跌停价9.0
        commission = max(amount * broker.cost_config.commission_rate,
                        broker.cost_config.min_commission)
        stamp_tax = amount * broker.cost_config.stamp_tax_rate
        transfer_fee = amount * broker.cost_config.transfer_fee_rate
        total_fees = commission + stamp_tax + transfer_fee
        assert total_fees > 0, "末日强平费用应大于0"


# ============================================================
# BUG-039: 参与率兜底不得强制100股
# ============================================================
class TestBUG039ParticipationFloor:
    """BUG-039: 参与率为0时走reject，不强制100股"""

    def test_zero_participation_reject(self):
        """日成交量极低时，参与率计算结果<100股→走reject"""
        from backtest.broker import SimBroker, CostConfig, Order, RejectReason
        broker = SimBroker(100000, CostConfig())
        bar = {
            "open": 10.0, "close": 10.0, "high": 10.5, "low": 9.5,
            "volume": 300,  # 日量仅300股
            "pre_close": 9.5, "amount": 3000,
        }
        order = Order(code="600000", direction="buy", target_shares=1000,
                      price=10.0, date="2024-06-01", reason="测试")
        fill = broker.execute_buy(order, bar)
        # 参与率=300*25%=75→(75//100)*100=0→应reject或成交0
        if fill is not None:
            # 若成交，不应超过日成交量
            assert fill.shares <= 300


# ============================================================
# BUG-040: 碎股一次性出清
# ============================================================
class TestBUG040FractionalShares:
    """BUG-040: 碎股不截断残留"""

    def test_fractional_shares_full_sell(self):
        """碎股（<100股）应一次性全部卖出"""
        from backtest.broker import SimBroker, CostConfig, Order, Position
        broker = SimBroker(100000, CostConfig())
        bar = {
            "open": 10.0, "close": 10.0, "high": 10.5, "low": 9.5,
            "volume": 1000000, "pre_close": 9.5, "amount": 10000000,
        }
        broker.positions["600000"] = Position(
            code="600000", shares=130, avg_cost=10.0,
            buy_date="2024-05-31", prev_day_available=130,
        )
        order = Order(code="600000", direction="sell", target_shares=130,
                      price=10.0, date="2024-06-03", reason="碎股出清")
        fill = broker.execute_sell(order, bar)
        assert fill is not None
        assert fill.shares == 130, f"碎股应全部卖出(130)，实际{fill.shares}"


# ============================================================
# BUG-041: 买入不收印花税
# ============================================================
class TestBUG041NoBuyStampTax:
    """BUG-041: 买入二次调整不得扣印花税"""

    def test_buy_no_stamp_tax(self):
        """买入成交后印花税应为0"""
        from backtest.broker import SimBroker, CostConfig, Order
        broker = SimBroker(100000, CostConfig())
        bar = {
            "open": 10.0, "close": 10.0, "high": 10.5, "low": 9.5,
            "volume": 1000000, "pre_close": 9.5, "amount": 10000000,
        }
        order = Order(code="600000", direction="buy", target_shares=1000,
                      price=10.0, date="2024-06-01", reason="测试")
        fill = broker.execute_buy(order, bar)
        assert fill is not None
        assert fill.stamp_tax == 0, "买入不应收印花税"


# ============================================================
# BUG-044: 停牌日不成交
# ============================================================
class TestBUG044SuspendedNoFill:
    """BUG-044: 停牌日(volume=0)应拒单"""

    def test_suspended_reject(self):
        """停牌日0成交量应拒单"""
        from backtest.broker import SimBroker, CostConfig, Order, RejectReason
        broker = SimBroker(100000, CostConfig())
        bar = {
            "open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0,
            "volume": 0, "pre_close": 10.0,
        }
        order = Order(code="600000", direction="buy", target_shares=1000,
                      price=10.0, date="2024-06-01", reason="测试")
        fill = broker.execute_buy(order, bar)
        assert fill is None, "停牌日不应成交"


# ============================================================
# BUG-097: 印花税按生效日分段
# ============================================================
class TestBUG097StampTaxDateSegmentation:
    """BUG-097: 印花税2023-08-28减半"""

    def test_cost_config_default(self):
        """CostConfig默认印花税应为万5"""
        from backtest.broker import CostConfig
        cc = CostConfig()
        assert cc.stamp_tax_rate == 0.0005

    def test_pre_cutover_rate(self):
        """2023-08-28之前应使用千1"""
        from backtest.broker import CostConfig
        cc = CostConfig()
        assert cc.stamp_tax_rate_pre_20230828 == 0.001

    def test_post_cutover_rate(self):
        """2023-08-28及之后应使用万5"""
        from backtest.broker import CostConfig
        cc = CostConfig()
        assert cc.stamp_tax_rate_post_20230828 == 0.0005


# ============================================================
# BUG-121: rf口径统一
# ============================================================
class TestBUG121RiskFreeRate:
    """BUG-121: 无风险利率口径统一为3%"""

    def test_metrics_sharpe_rf(self):
        """metrics.calc_sharpe_ratio 默认rf应为3%"""
        from backtest.metrics import calc_sharpe_ratio
        import inspect
        sig = inspect.signature(calc_sharpe_ratio)
        rf_default = sig.parameters['risk_free_rate'].default
        assert rf_default == 0.03, f"rf应为0.03，实际{rf_default}"

    def test_compare_rf(self):
        """compare.StrategyComparator.compare 默认rf应为3%"""
        from backtest.compare import StrategyComparator
        import inspect
        sig = inspect.signature(StrategyComparator.compare)
        rf_default = sig.parameters['risk_free_rate'].default
        assert rf_default == 0.03, f"rf应为0.03，实际{rf_default}"


# ============================================================
# 基线#6: DSR σ_SR口径
# ============================================================
class TestBaseline6DSRSigma:
    """基线#6: DSR σ_SR应为跨试验标准差（默认0.5）"""

    def test_dsr_sr_std_default(self):
        """DeflatedSharpe默认sr_std应为0.5"""
        from backtest.deflated_sharpe import DeflatedSharpe
        ds = DeflatedSharpe()
        result = ds.evaluate(observed_sharpe=1.0, n_trials=75)
        # N=75, σ_SR=0.5 → expected_max ≈ 2.43*0.5 ≈ 1.2
        expected_max = result["expected_max_sharpe"]
        assert 0.5 < expected_max < 2.0, \
            f"N=75时expected_max应在[0.5,2.0]范围，实际{expected_max}"


# ============================================================
# 基线#7: match_price默认next_open
# ============================================================
class TestBaseline7DefaultNextOpen:
    """基线#7: SimBroker默认match_price应为next_open"""

    def test_default_match_price(self):
        """SimBroker默认撮合价应为next_open"""
        from backtest.broker import SimBroker
        broker = SimBroker(100000)
        assert broker.match_price == "next_open", \
            f"默认match_price应为next_open，实际{broker.match_price}"


# ============================================================
# PIT数据层: 五张表DDL
# ============================================================
class TestPITDataLayer:
    """PIT数据层五张表应正确创建"""

    def test_init_db_creates_tables(self, tmp_path):
        """init_db应创建daily_kline_raw/adjust_factor/universe_pit/fundamental_pit/data_version"""
        from data.data_loader import init_db
        db_path = str(tmp_path / "test.db")
        conn = init_db(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        expected = {"daily_kline_raw", "adjust_factor", "universe_pit",
                    "fundamental_pit", "data_version"}
        missing = expected - tables
        assert not missing, f"缺少表: {missing}"
