"""
核心模块单元测试（V3.2新增）
============================
覆盖: risk_control / stock_screener / position / data_loader / backtest

运行: pytest tests/ -v
"""
import sys
import os
import pytest
import numpy as np

# 确保能导入trading_system
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'trading_system'))


# ============================================================
# 风控模块测试
# ============================================================

class TestRiskControl:
    """UnifiedRiskEngine核心逻辑测试"""

    def test_kelly_position_basic(self):
        """Kelly公式: 胜率60%+盈亏比2.0 → 半Kelly=20%"""
        from position.kelly import half_kelly_position
        pos = half_kelly_position(0.6, 2.0)
        assert 0.05 < pos < 0.25  # 合理范围

    def test_kelly_position_zero_winrate(self):
        """Kelly公式: 胜率0 → 仓位0"""
        from position.kelly import half_kelly_position
        pos = half_kelly_position(0.0, 2.0)
        assert pos == 0.0

    def test_kelly_from_trades(self):
        """Kelly从交易记录计算"""
        from position.kelly import kelly_from_trades
        trades = [
            {"pnl_pct": 0.05}, {"pnl_pct": 0.03}, {"pnl_pct": -0.02},
            {"pnl_pct": 0.04}, {"pnl_pct": -0.03}, {"pnl_pct": 0.06},
        ]
        pos = kelly_from_trades(trades)
        assert 0 < pos <= 0.15

    def test_strategy_failure_detector_normal(self):
        """策略失效检测: 高胜率状态允许买入"""
        from trading_system.risk.risk_control import StrategyFailureDetector
        detector = StrategyFailureDetector()
        # 模拟高胜率交易（15赢只5亏，且亏损不连续）
        for i in range(20):
            if i % 4 == 3:
                detector.record_trade(-0.02)
            else:
                detector.record_trade(0.03)
        allowed, reason, scale = detector.check_buy_allowed()
        assert allowed  # 胜率75%应该允许

    def test_strategy_failure_consecutive_loss(self):
        """策略失效检测: 连续5笔亏损触发熔断"""
        from trading_system.risk.risk_control import StrategyFailureDetector
        detector = StrategyFailureDetector()
        for _ in range(5):
            detector.record_trade(-0.05)
        status = detector.get_status()
        assert status["level"] in ("breaker", "pause")


# ============================================================
# 数据质量测试
# ============================================================

class TestDataQuality:
    """数据质量校验测试"""

    def test_validate_empty_df(self):
        """空DataFrame应报错"""
        import pandas as pd
        from trading_system.data.data_loader import validate_dataframe, DataQualityReport
        report = DataQualityReport()
        df = pd.DataFrame()
        result = validate_dataframe(df, "000001", report)
        assert report.has_errors

    def test_validate_bad_ohlc(self):
        """OHLC逻辑异常应被移除"""
        import pandas as pd
        from trading_system.data.data_loader import validate_dataframe, DataQualityReport
        report = DataQualityReport()
        df = pd.DataFrame({
            "date": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "open": [10.0, 10.5, 10.2],
            "close": [10.5, 10.2, 10.8],
            "high": [10.6, 9.0, 10.9],  # 第2行high<low，异常
            "low": [9.8, 10.0, 10.1],
            "volume": [1000, 1200, 1100],
        })
        result = validate_dataframe(df, "000001", report)
        assert len(result) == 2  # 移除了1行异常
        assert report.has_errors

    def test_validate_zero_price(self):
        """价格为0应被移除"""
        import pandas as pd
        from trading_system.data.data_loader import validate_dataframe, DataQualityReport
        report = DataQualityReport()
        df = pd.DataFrame({
            "date": ["2026-01-01", "2026-01-02"],
            "open": [10.0, 0.0],
            "close": [10.5, 0.0],
            "high": [10.6, 0.0],
            "low": [9.8, 0.0],
            "volume": [1000, 0],
        })
        result = validate_dataframe(df, "000001", report)
        assert len(result) == 1


# ============================================================
# 回测模块测试
# ============================================================

class TestBacktest:
    """回测引擎测试"""

    def test_monte_carlo_basic(self):
        """蒙特卡洛基本运行"""
        from trading_system.backtest.monte_carlo import MonteCarloStressTest
        mc = MonteCarloStressTest(n_simulations=100)
        trades = [{"pnl_pct": np.random.normal(0.01, 0.05)} for _ in range(50)]
        result = mc.run(trades, initial_capital=730000)
        assert "median_final" in result or "simulations" in str(result)

    def test_historical_stress_test(self):
        """历史情景压力测试"""
        from trading_system.backtest.monte_carlo import historical_stress_test
        trades = [{"pnl_pct": 0.03}, {"pnl_pct": -0.05}, {"pnl_pct": 0.02},
                  {"pnl_pct": -0.04}, {"pnl_pct": 0.05}]
        result = historical_stress_test(trades, initial_capital=730000)
        assert "2015股灾" in result
        assert "max_drawdown" in result["2015股灾"]

    def test_strategy_comparator(self):
        """策略对比框架"""
        from trading_system.backtest.compare import StrategyComparator
        comp = StrategyComparator()
        result = comp.compare([
            {"name": "策略A", "trades": [
                {"pnl_pct": 0.05}, {"pnl_pct": -0.03}, {"pnl_pct": 0.04},
                {"pnl_pct": 0.02}, {"pnl_pct": -0.02},
            ]},
            {"name": "策略B", "trades": [
                {"pnl_pct": 0.08}, {"pnl_pct": -0.06}, {"pnl_pct": 0.03},
                {"pnl_pct": -0.05}, {"pnl_pct": 0.07},
            ]},
        ])
        assert "ranking" in result
        assert len(result["ranking"]) == 2
        assert result["ranking"][0]["score"] >= result["ranking"][1]["score"]

    def test_broker_impact_cost(self):
        """交易成本: 冲击成本计算"""
        from trading_system.backtest.broker import CostConfig, SimBroker
        cfg = CostConfig(impact_cost_enabled=True, impact_cost_coeff=0.1)
        broker = SimBroker(730000, cfg)
        # 买入10万元，日成交3亿 → 冲击极小
        price_no_impact = broker._apply_slippage(10.0, "buy", 0, 0)
        price_with_impact = broker._apply_slippage(10.0, "buy", 100000, 300000000)
        assert price_with_impact > price_no_impact  # 有冲击成本更高
        assert price_with_impact - price_no_impact < 0.05  # 但不超过5分


# ============================================================
# P&L追踪测试
# ============================================================

class TestPnLTracker:
    """P&L追踪模块测试"""

    def test_calc_portfolio_pnl(self):
        """组合P&L计算"""
        from trading_system.monitor.pnl_tracker import PnLTracker
        tracker = PnLTracker(db_path=":memory:")
        holdings = {
            "002371": {"shares": 100, "cost": 200.0, "price": 210.0, "name": "北方华创"},
            "600036": {"shares": 500, "cost": 35.0, "price": 33.0, "name": "招商银行"},
        }
        snapshot = tracker.calc_portfolio_pnl(holdings)
        assert snapshot["holdings_count"] == 2
        assert snapshot["total_value"] == 100 * 210 + 500 * 33  # 37500
        assert snapshot["total_pnl"] == (210 - 200) * 100 + (33 - 35) * 500  # 0


# ============================================================
# IC监控测试
# ============================================================

class TestICMonitor:
    """因子IC监控测试"""

    def test_is_negative(self):
        """IC持续为负检测"""
        from trading_system.factors.ic_monitor import ICMonitor
        monitor = ICMonitor(decay_days=5)
        # 模拟连续负IC
        for i in range(6):
            monitor.update("test_factor", -0.03, date=f"2026-07-{20+i:02d}")
        assert monitor.is_negative("test_factor")

    def test_is_decaying(self):
        """IC衰减检测"""
        from trading_system.factors.ic_monitor import ICMonitor
        monitor = ICMonitor(decay_days=5, decay_threshold=0.02)
        for i in range(6):
            monitor.update("weak_factor", 0.01, date=f"2026-07-{20+i:02d}")
        assert monitor.is_decaying("weak_factor")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
