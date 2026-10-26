# -*- coding: utf-8 -*-
"""
CS-07: 估值门PE百分位误杀成长股 — 成长豁免修复验证

测试场景:
1. C/A双达标 + PE百分位>80% → 豁免放行（核心修复）
2. C/A不达标 + PE百分位>80% → 仍然拦截（原行为保留）
3. PE<0 亏损股 → 无论C/A是否达标一律拦截（不可豁免）
4. C达标A不达标 + PE高 → 拦截（仅C不够）
5. C不达标A达标 + PE高 → 拦截（仅A不够）
6. 无基本面数据 → 放行（原行为不变）
7. _check_growth_qualification 独立验证
"""
import pytest
import sys
import os

# 确保能导入项目模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'trading_system'))

from unittest.mock import patch, MagicMock


class TestCheckGrowthQualification:
    """测试 _check_growth_qualification 成长资质判定"""

    def _make_fund_data(self, eps_q=None, eps_3y=None, **kwargs):
        data = {}
        if eps_q is not None:
            data["eps_growth_q"] = eps_q
        if eps_3y is not None:
            data["eps_growth_3y"] = eps_3y
        data.update(kwargs)
        return data

    def test_both_pass(self):
        """C/A双达标 → growth_exempt=True"""
        from strategy.stock_screener import _check_growth_qualification, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000001": self._make_fund_data(eps_q=30, eps_3y=28)}):
            result = _check_growth_qualification("000001")
            assert result["c_pass"] is True
            assert result["a_pass"] is True
            assert result["growth_exempt"] is True

    def test_c_pass_a_fail(self):
        """C达标A不达标 → growth_exempt=False"""
        from strategy.stock_screener import _check_growth_qualification, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000002": self._make_fund_data(eps_q=30, eps_3y=15)}):
            result = _check_growth_qualification("000002")
            assert result["c_pass"] is True
            assert result["a_pass"] is False
            assert result["growth_exempt"] is False

    def test_c_fail_a_pass(self):
        """C不达标A达标 → growth_exempt=False"""
        from strategy.stock_screener import _check_growth_qualification, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000003": self._make_fund_data(eps_q=10, eps_3y=30)}):
            result = _check_growth_qualification("000003")
            assert result["c_pass"] is False
            assert result["a_pass"] is True
            assert result["growth_exempt"] is False

    def test_both_fail(self):
        """C/A都不达标 → growth_exempt=False"""
        from strategy.stock_screener import _check_growth_qualification, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000004": self._make_fund_data(eps_q=5, eps_3y=8)}):
            result = _check_growth_qualification("000004")
            assert result["growth_exempt"] is False

    def test_no_data(self):
        """无基本面数据 → growth_exempt=False"""
        from strategy.stock_screener import _check_growth_qualification, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {}, clear=True):
            result = _check_growth_qualification("999999")
            assert result["growth_exempt"] is False
            assert "无基本面数据" in result["detail"]

    def test_boundary_25(self):
        """边界值: eps_q=25, eps_3y=25 → 刚好达标"""
        from strategy.stock_screener import _check_growth_qualification, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000005": self._make_fund_data(eps_q=25, eps_3y=25)}):
            result = _check_growth_qualification("000005")
            assert result["c_pass"] is True
            assert result["a_pass"] is True
            assert result["growth_exempt"] is True

    def test_boundary_24(self):
        """边界值: eps_q=24.9 → 不达标"""
        from strategy.stock_screener import _check_growth_qualification, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000006": self._make_fund_data(eps_q=24.9, eps_3y=25)}):
            result = _check_growth_qualification("000006")
            assert result["c_pass"] is False
            assert result["growth_exempt"] is False


class TestCheckValuationGate:
    """测试 _check_valuation_gate 估值门（含CS-07成长豁免）"""

    def _make_fund_data(self, pe_ttm=None, pe_percentile=None, **kwargs):
        data = {}
        if pe_ttm is not None:
            data["pe_ttm"] = pe_ttm
        if pe_percentile is not None:
            data["pe_percentile"] = pe_percentile
        data.update(kwargs)
        return data

    def test_pe_negative_always_blocked(self):
        """PE<0 亏损股 → 无论是否成长豁免，一律拦截"""
        from strategy.stock_screener import _check_valuation_gate, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000001": self._make_fund_data(pe_ttm=-10, pe_percentile=90)}):
            # 即使 growth_exempt=True，PE<0 仍拦截
            result = _check_valuation_gate("000001", growth_exempt=True)
            assert result["pass"] is False
            assert "亏损股" in result["reason"]

    def test_high_pe_no_growth_blocked(self):
        """PE百分位>80% + 非成长股 → 拦截"""
        from strategy.stock_screener import _check_valuation_gate, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000002": self._make_fund_data(pe_ttm=50, pe_percentile=90)}):
            result = _check_valuation_gate("000002", growth_exempt=False)
            assert result["pass"] is False
            assert "历史高位" in result["reason"]

    def test_high_pe_with_growth_exempt(self):
        """PE百分位>80% + C/A双达标成长股 → 豁免放行（CS-07核心修复）"""
        from strategy.stock_screener import _check_valuation_gate, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000003": self._make_fund_data(pe_ttm=80, pe_percentile=92)}):
            result = _check_valuation_gate("000003", growth_exempt=True)
            assert result["pass"] is True
            assert result["growth_exempt"] is True
            assert "豁免" in result["reason"]

    def test_normal_pe_pass(self):
        """PE百分位正常（≤80%）→ 放行"""
        from strategy.stock_screener import _check_valuation_gate, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000004": self._make_fund_data(pe_ttm=30, pe_percentile=50)}):
            result = _check_valuation_gate("000004", growth_exempt=False)
            assert result["pass"] is True

    def test_no_data_pass(self):
        """无基本面数据 → 放行"""
        from strategy.stock_screener import _check_valuation_gate, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {}, clear=True):
            result = _check_valuation_gate("999999", growth_exempt=False)
            assert result["pass"] is True

    def test_boundary_pe_pct_80(self):
        """边界值: PE百分位=80% → 不拦截（>80才拦截）"""
        from strategy.stock_screener import _check_valuation_gate, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000005": self._make_fund_data(pe_ttm=40, pe_percentile=80)}):
            result = _check_valuation_gate("000005", growth_exempt=False)
            assert result["pass"] is True

    def test_boundary_pe_pct_81(self):
        """边界值: PE百分位=81% → 拦截（非成长）"""
        from strategy.stock_screener import _check_valuation_gate, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000006": self._make_fund_data(pe_ttm=40, pe_percentile=81)}):
            result = _check_valuation_gate("000006", growth_exempt=False)
            assert result["pass"] is False

    def test_boundary_pe_pct_81_growth_exempt(self):
        """边界值: PE百分位=81% + 成长豁免 → 放行"""
        from strategy.stock_screener import _check_valuation_gate, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000007": self._make_fund_data(pe_ttm=40, pe_percentile=81)}):
            result = _check_valuation_gate("000007", growth_exempt=True)
            assert result["pass"] is True
            assert result["growth_exempt"] is True

    def test_backward_compatible_default(self):
        """向后兼容: 不传growth_exempt参数时默认False（原行为不变）"""
        from strategy.stock_screener import _check_valuation_gate, FUNDAMENTAL_DATA
        with patch.dict(FUNDAMENTAL_DATA, {"000008": self._make_fund_data(pe_ttm=50, pe_percentile=90)}):
            result = _check_valuation_gate("000008")  # 不传growth_exempt
            assert result["pass"] is False
