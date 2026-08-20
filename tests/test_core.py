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
        """策略失效检测: 连续亏损触发熔断（V9.3: 阈值从5调整为7，与SFD_CONFIG.consec_loss_breaker一致）"""
        from trading_system.risk.risk_control import StrategyFailureDetector
        import config as _cfg
        _threshold = getattr(_cfg, 'SFD_CONFIG', {}).get('consec_loss_breaker', 7)
        detector = StrategyFailureDetector()
        for _ in range(_threshold):
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

    def test_is_negative(self, tmp_path):
        """IC持续为负检测"""
        from trading_system.factors.ic_monitor import ICMonitor
        # 使用临时目录，避免污染真实 ic_history.json
        monitor = ICMonitor(decay_days=5, history_path=str(tmp_path / "ic_history_test.json"))
        # 模拟连续负IC
        for i in range(6):
            monitor.update("test_factor", -0.03, date=f"2026-07-{20+i:02d}")
        assert monitor.is_negative("test_factor")

    def test_is_decaying(self, tmp_path):
        """IC衰减检测"""
        from trading_system.factors.ic_monitor import ICMonitor
        # 使用临时目录，避免污染真实 ic_history.json
        monitor = ICMonitor(decay_days=5, decay_threshold=0.02, history_path=str(tmp_path / "ic_history_test.json"))
        for i in range(6):
            monitor.update("weak_factor", 0.01, date=f"2026-07-{20+i:02d}")
        assert monitor.is_decaying("weak_factor")


# ============================================================
# 选股引擎V3.5 深跌防护测试（2026-08-06诊断修复）
# ============================================================

class TestHardFilterV35:
    """hard_filter 深跌防护与弱势模式标记测试"""

    @staticmethod
    def _mk_df(closes, ma20=None):
        import pandas as pd
        closes = np.array(closes, dtype=float)
        n = len(closes)
        vol = np.full(n, 5e7)
        df = pd.DataFrame({
            "close": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "volume": vol,
            "amount": vol * closes,
        })
        df["ma20"] = pd.Series(ma20, index=df.index) if ma20 is not None else df["close"].rolling(20).mean()
        df["ma20_slope"] = df["ma20"].diff(3)
        df["ma60"] = df["close"].rolling(60).mean()
        return df

    def test_deep_drawdown_rejected(self):
        """距20日高点回撤>8%的企稳股（弱势模式）必须被拒绝（海天味业案例）"""
        from trading_system.strategy.stock_screener import hard_filter
        # 前50日10元横盘 → 拉升到12元 → 回落至10.5元横盘企稳（回撤-12.5%）
        closes = [10.0] * 50 + [10.5, 11.0, 11.5, 12.0, 12.0, 11.8, 11.5, 11.0, 10.5] + [10.5] * 11
        ma20 = [11.5] * len(closes)  # 横盘企稳，距MA20约-8.7% → weak_score可过25
        df = self._mk_df(closes, ma20)
        res = hard_filter(df, "603288", market_state="down")
        assert res["pass"] is False
        assert "回撤" in res["reason"]

    def test_60d_decline_rejected(self):
        """60日累计跌幅超20%的横盘股必须被拒绝（下降趋势防护）"""
        from trading_system.strategy.stock_screener import hard_filter
        # 前10日15元高位 → 40日阴跌到11.5元 → 后21日横盘企稳（20日回撤<8%但60日跌-23%）
        closes = [15.0] * 10 + list(np.linspace(15.0, 11.5, 40)) + [11.5] * 21
        ma20 = [11.6] * len(closes)
        df = self._mk_df(closes, ma20)
        res = hard_filter(df, "600000", market_state="down")
        assert res["pass"] is False
        assert "60日" in res["reason"]

    def test_strong_uptrend_passes(self):
        """强势上升趋势股（回撤≈0）正常通过硬性筛选"""
        from trading_system.strategy.stock_screener import hard_filter
        closes = list(np.linspace(10.0, 13.0, 70))
        df = self._mk_df(closes)
        res = hard_filter(df, "000001", market_state="up")
        assert res["pass"] is True
        assert res["details"].get("drawdown_from_high", 0) > -0.08

    def test_weak_mode_below_ma20_flag(self):
        """弱势模式通过但破MA20的企稳股必须标记 below_ma20（供输出层降级）"""
        from trading_system.strategy.stock_screener import hard_filter
        # V5.1: weak_score门槛从25提升至40，测试数据需产生≥40分
        # 构造: 距MA20偏离<5%(+20) + 近5日企稳(+20) + 5日正收益(+20) = 60分 ≥40
        closes = [11.0] * 49 + [11.05, 11.08, 11.1, 11.12, 11.15] + [11.1] * 11
        ma20 = [11.2] * len(closes)  # MA20=11.2 > close=11.1
        df = self._mk_df(closes, ma20)
        res = hard_filter(df, "600001", market_state="down")
        assert res["pass"] is True  # weak_score=20(偏离)+20(企稳)+20(5日正收益) = 60 ≥40
        assert bool(res["details"]["below_ma20"]) is True  # numpy.bool_需bool()转换后比较


# ============================================================
# 逆势加仓（回调加仓）风控测试 V1.0（2026-08-07）
# ============================================================

class TestPullbackAddRisk:
    """回调加仓风控检查 check_pullback_add_risk 测试"""

    def _mk_df(self, closes, volumes=None, ma20=None, ma60=None, ma20_slope=None):
        """构造测试用DataFrame"""
        import pandas as pd
        n = len(closes)
        data = {
            "close": closes,
            "high": [c * 1.02 for c in closes],
            "low": [c * 0.98 for c in closes],
            "open": closes,
            "volume": volumes or [5e7] * n,
        }
        df = pd.DataFrame(data)
        if ma20:
            df["ma20"] = ma20
        else:
            df["ma20"] = pd.Series(closes).rolling(20, min_periods=1).mean().tolist()
        if ma60:
            df["ma60"] = ma60
        else:
            df["ma60"] = pd.Series(closes).rolling(60, min_periods=1).mean().tolist()
        if ma20_slope is not None:
            df["ma20_slope"] = ma20_slope
        else:
            df["ma20_slope"] = pd.Series(df["ma20"]).diff(3).fillna(0).tolist()
        # RSI/Bollinger（简化）
        df["rsi"] = 50.0
        df["boll_lower"] = [c * 0.95 for c in closes]
        return df

    def test_pullback_pass_basic(self):
        """正常回调企稳应通过: 回调5%+缩量+触及MA20"""
        from risk.risk_control import check_pullback_add_risk
        # 构造: 先涨到12，再回调到11.4（回调约5%），缩量
        closes = [10.0] * 10 + [11.0] * 5 + [12.0] * 10 + [11.6, 11.5, 11.4]
        volumes = [5e7] * 20 + [5e7] * 5 + [2e7, 1.5e7, 1e7]  # 最后3日缩量
        ma20 = [11.5] * len(closes)  # MA20恒定11.5
        ma60 = [10.5] * len(closes)  # MA60在MA20下方
        df = self._mk_df(closes, volumes, ma20=ma20, ma60=ma60, ma20_slope=[0.01]*len(closes))
        holdings = {"600001": {"shares": 1000, "buy_price": 11.0, "current_price": 11.4, "weight": 0.15}}
        result = check_pullback_add_risk("600001", holdings, df, market_state="neutral")
        assert result["pass"] is True
        assert result["add_shares"] > 0
        assert "回调" in result["reason"]

    def test_pullback_rejected_deep_loss(self):
        """浮亏>8%应拒绝"""
        from risk.risk_control import check_pullback_add_risk
        closes = [10.0] * 25 + [9.0, 8.5, 8.0]  # 大跌，确保>=25行
        df = self._mk_df(closes)
        holdings = {"600001": {"shares": 1000, "buy_price": 10.0, "current_price": 8.0}}
        result = check_pullback_add_risk("600001", holdings, df, market_state="neutral")
        assert result["pass"] is False
        assert "浮亏" in result["reason"]

    def test_pullback_rejected_market_weak(self):
        """大盘弱势应拒绝"""
        from risk.risk_control import check_pullback_add_risk
        closes = [10.0] * 20 + [9.5, 9.4, 9.3]
        df = self._mk_df(closes)
        holdings = {"600001": {"shares": 1000, "buy_price": 10.0}}
        result = check_pullback_add_risk("600001", holdings, df, market_state="down")
        assert result["pass"] is False
        assert "大盘" in result["reason"]

    def test_pullback_rejected_no_volume_shrink(self):
        """未缩量应拒绝"""
        from risk.risk_control import check_pullback_add_risk
        closes = [10.0] * 10 + [11.0] * 5 + [12.0] * 10 + [11.6, 11.5, 11.4]
        volumes = [5e7] * len(closes)  # 全程未缩量
        ma20 = [11.5] * len(closes)
        ma60 = [10.5] * len(closes)
        # 传入正值ma20_slope，避免被步骤5(MA20斜率<=0)先拦截
        df = self._mk_df(closes, volumes, ma20=ma20, ma60=ma60, ma20_slope=[0.01] * len(closes))
        holdings = {"600001": {"shares": 1000, "buy_price": 11.0}}
        result = check_pullback_add_risk("600001", holdings, df, market_state="neutral")
        assert result["pass"] is False
        assert "缩量" in result["reason"] or "未缩量" in result["reason"] or "量比" in result["reason"]

    def test_pullback_rejected_too_deep(self):
        """回调>12%应拒绝（趋势可能反转）"""
        from risk.risk_control import check_pullback_add_risk
        closes = [10.0] * 10 + [12.0] * 15 + [10.0, 9.5, 9.0]  # 从12回调到9=-25%
        volumes = [5e7] * len(closes)  # 确保长度一致
        ma20 = [11.5] * len(closes)
        ma60 = [10.5] * len(closes)
        # buy_price=9.2 → 浮亏=(9.0/9.2-1)=-2.2%<8%，通过步骤4
        # 回调深度=(9.0/12.24-1)≈-26.5%>12%，命中步骤6
        df = self._mk_df(closes, volumes, ma20=ma20, ma60=ma60, ma20_slope=[0.01] * len(closes))
        holdings = {"600001": {"shares": 1000, "buy_price": 9.2}}
        result = check_pullback_add_risk("600001", holdings, df, market_state="neutral")
        assert result["pass"] is False
        assert "过深" in result["reason"]

    def test_pullback_rejected_not_holding(self):
        """非持仓股应拒绝"""
        from risk.risk_control import check_pullback_add_risk
        closes = [10.0] * 20 + [9.5, 9.4, 9.3]
        df = self._mk_df(closes)
        holdings = {}  # 无持仓
        result = check_pullback_add_risk("600001", holdings, df, market_state="neutral")
        assert result["pass"] is False
        assert "非持仓" in result["reason"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
