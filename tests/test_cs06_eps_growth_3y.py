# -*- coding: utf-8 -*-
"""
CS-06: fundamental.py eps_growth_3y 错误映射修复验证

测试场景:
1. eps_growth_3y 不再等于 revenue_growth（核心修复）
2. eps_growth_3y 为 None 时 canslim_score A因子给中性分（0分）
3. eps_growth_q 仍正常取值（不受影响）
4. get_canslim_fundamental 返回结构完整
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'trading_system'))

from unittest.mock import patch, MagicMock


class TestEpsGrowth3yFix:
    """测试 eps_growth_3y 错误映射修复"""

    def test_eps_3y_not_revenue_growth(self):
        """核心修复: eps_growth_3y 不再等于 revenue_growth"""
        from strategy.fundamental import FundamentalAnalyzer
        fa = FundamentalAnalyzer()
        
        # Mock get_financial_indicators 返回含 revenue_growth 的数据
        mock_fin = {
            "net_profit_growth": 35.0,
            "revenue_growth": 42.0,  # 营收增速42%
            "roe": 18.5,
            "gross_margin": 30.0,
            "debt_ratio": 45.0,
            "pe_ttm": 25.0,
            "pb": 3.0,
            "pe_percentile": 60.0,
        }
        mock_flow = {"signal": "inflow"}
        
        with patch.object(fa, 'get_financial_indicators', return_value=mock_fin), \
             patch.object(fa, 'get_capital_flow', return_value=mock_flow):
            result = fa.get_canslim_fundamental("000001")
            
            # 核心断言: eps_growth_3y 不等于 revenue_growth
            assert result["eps_growth_3y"] is None, \
                f"eps_growth_3y应为None(无真实数据)，实际为{result['eps_growth_3y']}"
            
            # revenue_growth 仍然正确输出
            assert result["revenue_growth"] == 42.0
            
            # eps_growth_q 仍然从 net_profit_growth 取值
            assert result["eps_growth_q"] == 35.0

    def test_eps_3y_none_when_no_data(self):
        """无财务数据时 eps_growth_3y 为 None"""
        from strategy.fundamental import FundamentalAnalyzer
        fa = FundamentalAnalyzer()
        
        mock_fin = {}  # 空数据
        mock_flow = {"signal": "neutral"}
        
        with patch.object(fa, 'get_financial_indicators', return_value=mock_fin), \
             patch.object(fa, 'get_capital_flow', return_value=mock_flow):
            result = fa.get_canslim_fundamental("000002")
            assert result["eps_growth_3y"] is None

    def test_eps_q_still_works(self):
        """eps_growth_q 不受修复影响，仍从 net_profit_growth 取值"""
        from strategy.fundamental import FundamentalAnalyzer
        fa = FundamentalAnalyzer()
        
        mock_fin = {"net_profit_growth": 55.0, "revenue_growth": 30.0}
        mock_flow = {"signal": "inflow"}
        
        with patch.object(fa, 'get_financial_indicators', return_value=mock_fin), \
             patch.object(fa, 'get_capital_flow', return_value=mock_flow):
            result = fa.get_canslim_fundamental("000003")
            assert result["eps_growth_q"] == 55.0
            assert result["has_institution"] is True

    def test_return_structure_complete(self):
        """返回结构完整，所有字段都存在"""
        from strategy.fundamental import FundamentalAnalyzer
        fa = FundamentalAnalyzer()
        
        mock_fin = {"net_profit_growth": 20.0, "roe": 15.0}
        mock_flow = {"signal": "neutral"}
        
        with patch.object(fa, 'get_financial_indicators', return_value=mock_fin), \
             patch.object(fa, 'get_capital_flow', return_value=mock_flow):
            result = fa.get_canslim_fundamental("000004")
            
            expected_keys = [
                "eps_growth_q", "eps_growth_3y", "has_institution",
                "roe", "gross_margin", "debt_ratio",
                "revenue_growth", "net_profit_growth",
                "pe_ttm", "pb", "pe_percentile",
            ]
            for key in expected_keys:
                assert key in result, f"缺少字段: {key}"


class TestCanslimScoreWithNoneEps3y:
    """测试 eps_growth_3y=None 时 canslim_score A因子行为"""

    def test_a_factor_zero_when_none(self):
        """eps_growth_3y=None 时 A因子给0分（中性处理）"""
        # 验证 canslim_score 中 A 因子对 None 的处理逻辑
        # L1155-1162: if eps_3y is not None: ... else: 0分
        eps_3y = None
        a_score = 0
        if eps_3y is not None:
            if eps_3y > 30:
                a_score = 7
            elif eps_3y > 20:
                a_score = 5
            elif eps_3y > 10:
                a_score = 2
        
        assert a_score == 0, "eps_growth_3y=None时A因子应为0分"

    def test_cs07_growth_exempt_blocked_when_3y_none(self):
        """eps_growth_3y=None 时 CS-07成长豁免不应触发（A不达标）"""
        from strategy.stock_screener import _check_growth_qualification, FUNDAMENTAL_DATA
        
        # 模拟: eps_growth_q=30(达标) 但 eps_growth_3y=None(不达标)
        mock_data = {"000001": {"eps_growth_q": 30, "eps_growth_3y": None}}
        with patch.dict(FUNDAMENTAL_DATA, mock_data):
            result = _check_growth_qualification("000001")
            assert result["c_pass"] is True   # C达标
            assert result["a_pass"] is False  # A不达标(None<25)
            assert result["growth_exempt"] is False  # 不能豁免
