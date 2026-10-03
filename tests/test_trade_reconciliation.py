# -*- coding: utf-8 -*-
"""
成交回报 + 账实对账 测试（trade-reconciliation）
================================================
覆盖 Skill 验收清单的五类核心用例：
  1. 全部成交：均价回填、状态已成交、持仓不被多记
  2. 部分成交：识别真实成交量/均价，未成部分标记
  3. 拒单：清除执行假象，记录原因
  4. 回调丢失后主动流水补全（含去重幂等）
  5. 滑点超标：买入 +0.3% 阈值标红
外加：盘后账实对账差异 + 告警去重、模拟模式对账未完成、区块渲染。
全部在模拟数据下运行，不依赖 xtquant 真实客户端。
"""

import os
import json
import pytest

from execution.trade_ledger import (
    TradeLedger, canonical_status, canonical_direction,
    STATUS_FILLED, STATUS_PARTIAL, STATUS_REJECTED,
)
from execution.fill_callback import FillCallback
from execution.order_reconcile import (
    reconcile_order, compute_slippage, render_order_reconcile_html,
)
from execution.daily_reconcile import (
    DailyReconciler, render_reconcile_section_html,
)


@pytest.fixture
def ledger(tmp_path):
    return TradeLedger(db_path=str(tmp_path / "recon_test.db"))


@pytest.fixture
def cb(ledger):
    return FillCallback(ledger=ledger)


class _SimTrader:
    """模拟模式 trader 桩：无 xtquant，无法提供权威数据。"""
    has_xtquant = False
    connected = True

    def get_positions(self):
        return []

    def get_account_info(self):
        return {}


# ---- 状态/方向归一 ----
def test_canonical_status_and_direction():
    assert canonical_status(56) == STATUS_FILLED
    assert canonical_status(55) == STATUS_PARTIAL
    assert canonical_status(57) == STATUS_REJECTED
    assert canonical_status("已成") == STATUS_FILLED
    assert canonical_direction(23) == "买入"
    assert canonical_direction(24) == "卖出"
    assert canonical_direction("buy") == "买入"


# ---- 用例1：全部成交 ----
def test_full_fill(cb, ledger):
    cb.on_stock_order({"order_id": "O1", "stock_code": "600989", "order_type": 23,
                       "price": 10.0, "order_volume": 1000, "order_status": 50})
    cb.on_stock_trade({"traded_id": "T1", "order_id": "O1", "stock_code": "600989",
                       "order_type": 23, "traded_price": 10.05, "traded_volume": 1000})
    od = ledger.get_order("O1")
    assert od["status"] == STATUS_FILLED
    assert od["filled_qty"] == 1000
    assert abs(od["avg_price"] - 10.05) < 1e-6

    recon = reconcile_order("O1", ledger=ledger)
    assert recon["ok"] is True
    assert recon["is_complete"] is True
    assert recon["fill_ratio"] == 1.0
    assert recon["unfilled_qty"] == 0
    # 成交回报重复推送不会多记成交量（traded_id 去重）
    cb.on_stock_trade({"traded_id": "T1", "order_id": "O1", "stock_code": "600989",
                       "order_type": 23, "traded_price": 10.05, "traded_volume": 1000})
    assert ledger.get_order("O1")["filled_qty"] == 1000


def test_full_fill_multi_leg_avg_price(cb, ledger):
    """分笔成交：均价 = Σ(价×量)/Σ量。"""
    cb.on_stock_order({"order_id": "OM", "stock_code": "000001", "order_type": 23,
                       "price": 10.0, "order_volume": 1000, "order_status": 50})
    cb.on_stock_trade({"traded_id": "M1", "order_id": "OM", "traded_price": 10.0,
                       "traded_volume": 600, "order_type": 23})
    cb.on_stock_trade({"traded_id": "M2", "order_id": "OM", "traded_price": 10.2,
                       "traded_volume": 400, "order_type": 23})
    od = ledger.get_order("OM")
    assert od["filled_qty"] == 1000
    expected = (10.0 * 600 + 10.2 * 400) / 1000
    assert abs(od["avg_price"] - expected) < 1e-6
    assert od["status"] == STATUS_FILLED


# ---- 用例2：部分成交 ----
def test_partial_fill(cb, ledger):
    cb.on_stock_order({"order_id": "O2", "stock_code": "600887", "order_type": 23,
                       "price": 20.0, "order_volume": 1000, "order_status": 50})
    cb.on_stock_trade({"traded_id": "T2", "order_id": "O2", "traded_price": 20.0,
                       "traded_volume": 400, "order_type": 23})
    od = ledger.get_order("O2")
    assert od["status"] == STATUS_PARTIAL
    assert od["filled_qty"] == 400

    recon = reconcile_order("O2", ledger=ledger)
    assert recon["unfilled_qty"] == 600
    assert recon["fill_ratio"] == 0.4
    assert recon["is_complete"] is False
    assert any("部分成交" in f for f in recon["flags"])


# ---- 用例3：拒单 ----
def test_rejected_order(cb, ledger):
    cb.on_stock_order({"order_id": "O3", "stock_code": "002281", "order_type": 23,
                       "price": 100.0, "order_volume": 100, "order_status": 57,
                       "status_msg": "资金不足"})
    od = ledger.get_order("O3")
    assert od["status"] == STATUS_REJECTED
    assert od["filled_qty"] == 0
    assert "O3" in cb.rejected_orders

    recon = reconcile_order("O3", ledger=ledger)
    assert recon["is_complete"] is False
    assert any("拒单" in f for f in recon["flags"])


def test_order_error_marks_rejected(cb, ledger):
    cb.on_order_error({"order_id": "OE", "error_msg": "涨停无法成交"})
    assert ledger.get_order("OE")["status"] == STATUS_REJECTED
    assert "OE" in cb.rejected_orders


# ---- 用例4：回调丢失后流水补全 ----
def test_sync_backfill_after_callback_lost(ledger):
    cb2 = FillCallback(ledger=ledger)
    # 断线期间成交，回调丢失 → 主动流水查询补全
    n_orders = cb2.sync_orders([{
        "order_id": "O4", "stock_code": "000001", "order_type": 24,
        "price": 12.0, "order_volume": 500, "order_status": 56,
        "traded_volume": 500, "traded_price": 12.0,
    }])
    n_trades = cb2.sync_trades([{
        "traded_id": "T4", "order_id": "O4", "stock_code": "000001",
        "order_type": 24, "traded_price": 12.0, "traded_volume": 500,
    }])
    assert n_orders == 1
    assert n_trades == 1
    od = ledger.get_order("O4")
    assert od["status"] == STATUS_FILLED
    assert od["filled_qty"] == 500
    assert abs(od["avg_price"] - 12.0) < 1e-6

    # 幂等：重复拉取同一流水不重复入账
    assert cb2.sync_trades([{
        "traded_id": "T4", "order_id": "O4", "stock_code": "000001",
        "order_type": 24, "traded_price": 12.0, "traded_volume": 500,
    }]) == 0
    assert ledger.get_order("O4")["filled_qty"] == 500


def test_sync_from_trader_sim_not_completed(ledger):
    cb = FillCallback(ledger=ledger)
    res = cb.sync_from_trader(_SimTrader())
    assert res["completed"] is False


def test_disconnected_flag(cb):
    cb.on_disconnected()
    assert cb.disconnected is True
    cb.on_connected()
    assert cb.disconnected is False


# ---- 用例5：滑点超标 ----
def test_buy_slippage_exceeded(cb, ledger):
    cb.on_stock_order({"order_id": "O5", "stock_code": "600256", "order_type": 23,
                       "price": 10.0, "order_volume": 1000, "order_status": 50})
    cb.on_stock_trade({"traded_id": "T5", "order_id": "O5", "traded_price": 10.05,
                       "traded_volume": 1000, "order_type": 23})
    recon = reconcile_order("O5", ledger=ledger)
    assert recon["slippage"] == pytest.approx(0.005, abs=1e-6)
    assert recon["slippage_exceeded"] is True
    assert any("滑点超标" in f for f in recon["flags"])


def test_sell_slippage_exceeded(cb, ledger):
    cb.on_stock_order({"order_id": "O6", "stock_code": "600256", "order_type": 24,
                       "price": 10.0, "order_volume": 1000, "order_status": 50})
    cb.on_stock_trade({"traded_id": "T6", "order_id": "O6", "traded_price": 9.9,
                       "traded_volume": 1000, "order_type": 24})
    recon = reconcile_order("O6", ledger=ledger)
    assert recon["slippage"] == pytest.approx(-0.01, abs=1e-6)
    assert recon["slippage_exceeded"] is True


def test_slippage_within_threshold_not_exceeded():
    slip, exceeded = compute_slippage("买入", 10.01, 10.0)  # +0.1% < +0.3%
    assert exceeded is False
    slip, exceeded = compute_slippage("卖出", 9.99, 10.0)    # -0.1% > -0.3%
    assert exceeded is False


# ---- 盘后账实对账 ----
def _write_holdings(tmp_path, shares=3000):
    path = tmp_path / "holdings.json"
    path.write_text(json.dumps({
        "600989": {"name": "宝丰能源", "shares": shares, "buy_price": 24.618,
                   "current_price": 22.58, "market_value": shares * 22.58},
    }, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_daily_reconcile_position_diff_and_dedup(ledger, tmp_path, monkeypatch):
    hpath = _write_holdings(tmp_path, shares=3000)  # 本地3000，券商3600 → 差异
    recon = DailyReconciler(ledger=ledger, holdings_path=hpath, notify=True,
                            config={"asset_tolerance_pct": 0.005})
    alerts = []
    monkeypatch.setattr(recon, "_send_alert",
                        lambda diffs, n, d: alerts.append((diffs, n, d)))

    broker_positions = [{"code": "600989", "volume": 3600, "available": 3600,
                         "cost": 24.618, "market_value": 81288}]
    broker_asset = {"total_asset": 710000, "cash": 200000,
                    "market_value": 81288, "frozen": 0}

    res = recon.run(broker_positions=broker_positions, broker_asset=broker_asset,
                    trade_date="2026-09-25")
    assert res["completed"] is True
    assert res["has_diff"] is True
    assert any(d["code"] == "600989" and d["type"] == "position"
               for d in res["positions"])
    assert len(alerts) == 1  # 触发一次告警

    # 同日重复对账：差异去重，不再重复告警
    res2 = recon.run(broker_positions=broker_positions, broker_asset=broker_asset,
                     trade_date="2026-09-25")
    assert res2["new_diffs"] == 0
    assert len(alerts) == 1  # 去重生效，告警未再次发送


def test_daily_reconcile_sim_not_completed(ledger, tmp_path):
    """拿不到券商权威数据 → 明确未完成，绝不假报一致。"""
    hpath = _write_holdings(tmp_path)
    recon = DailyReconciler(trader=_SimTrader(), ledger=ledger,
                            holdings_path=hpath, notify=False)
    res = recon.run(trade_date="2026-09-25")
    assert res["completed"] is False
    assert res["has_diff"] is False
    assert "未完成" in res["message"]


def test_daily_reconcile_consistent(ledger, tmp_path):
    hpath = _write_holdings(tmp_path, shares=3600)
    recon = DailyReconciler(ledger=ledger, holdings_path=hpath, notify=False,
                            config={"asset_tolerance_pct": 0.01})
    broker_positions = [{"code": "600989", "volume": 3600, "available": 3600,
                         "cost": 24.618, "market_value": 81288}]
    broker_asset = {"total_asset": 710000, "cash": 200000,
                    "market_value": 81288, "frozen": 0}
    res = recon.run(broker_positions=broker_positions, broker_asset=broker_asset,
                    trade_date="2026-09-25")
    assert res["completed"] is True
    assert res["positions"] == []


# ---- 区块渲染 ----
def test_render_sections(ledger, tmp_path):
    hpath = _write_holdings(tmp_path, shares=3000)
    recon = DailyReconciler(ledger=ledger, holdings_path=hpath, notify=False)
    res = recon.run(broker_positions=[{"code": "600989", "volume": 3600,
                                       "available": 3600, "cost": 24.618,
                                       "market_value": 81288}],
                    broker_asset={"total_asset": 710000, "cash": 200000,
                                  "market_value": 81288, "frozen": 0},
                    trade_date="2026-09-25")
    html = render_reconcile_section_html(res)
    assert "账实对账" in html
    assert "600989" in html

    incomplete = {"completed": False, "message": "对账未完成：无券商数据"}
    html2 = render_reconcile_section_html(incomplete)
    assert "对账未完成" in html2


def test_render_order_reconcile_html(cb, ledger):
    cb.on_stock_order({"order_id": "OH", "stock_code": "600989", "order_type": 23,
                       "price": 10.0, "order_volume": 1000, "order_status": 50})
    cb.on_stock_trade({"traded_id": "TH", "order_id": "OH", "traded_price": 10.05,
                       "traded_volume": 1000, "order_type": 23})
    from execution.order_reconcile import reconcile_day
    rows = reconcile_day(ledger)
    html = render_order_reconcile_html(rows)
    assert "成交对账" in html
    assert "滑点" in html
