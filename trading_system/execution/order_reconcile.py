# -*- coding: utf-8 -*-
"""
单笔委托对账（order_reconcile）
===============================
对单笔委托核对：委托量 vs 成交量、实际成交均价 vs 预期价、委托状态，并计算
滑点偏差。滑点超过阈值（默认买入 +0.3% / 卖出 -0.3%）标记为超标。

口径说明：
  slippage = (实际均价 - 预期价) / 预期价
    买入：slippage > +阈值 → 买贵了，超标
    卖出：slippage < -阈值 → 卖便宜了，超标

安全约束：只读台账，异常静默降级返回 {"ok": False}，绝不抛异常。
"""

import logging

from .trade_ledger import (
    TradeLedger, STATUS_FILLED, STATUS_PARTIAL, STATUS_CANCELLED, STATUS_REJECTED,
)

logger = logging.getLogger(__name__)

DEFAULT_BUY_SLIPPAGE = 0.003    # 买入 +0.3%
DEFAULT_SELL_SLIPPAGE = -0.003  # 卖出 -0.3%


def _cfg(config=None):
    """读取 RECONCILE_CONFIG 阈值，缺失时用默认值。"""
    buy_th, sell_th = DEFAULT_BUY_SLIPPAGE, DEFAULT_SELL_SLIPPAGE
    try:
        if config is None:
            import config as _c
            cfg = getattr(_c, "RECONCILE_CONFIG", {})
        else:
            cfg = config
        buy_th = float(cfg.get("buy_slippage_threshold", DEFAULT_BUY_SLIPPAGE))
        sell_th = float(cfg.get("sell_slippage_threshold", DEFAULT_SELL_SLIPPAGE))
    except Exception:
        pass
    return buy_th, sell_th


def compute_slippage(direction: str, avg_price: float, expected_price: float,
                     config=None):
    """计算滑点及是否超标。返回 (slippage, exceeded)。价格无效时返回 (None, False)。

    FIX P2-19: 新增 config 参数并传递给 _cfg，使调用方传入的滑点阈值生效
    （原永读全局 config，阈值配置形同虚设）。
    """
    try:
        if not avg_price or not expected_price or expected_price <= 0:
            return None, False
        slip = (avg_price - expected_price) / expected_price
        buy_th, sell_th = _cfg(config)
        if direction == "买入":
            exceeded = slip > buy_th
        elif direction == "卖出":
            exceeded = slip < sell_th
        else:
            exceeded = False
        return slip, exceeded
    except Exception:
        return None, False


def reconcile_order(order_id, ledger: TradeLedger = None, expected_price=None,
                    config=None) -> dict:
    """对账单笔委托。

    参数:
        order_id: 委托号
        ledger:   台账（None=用默认库）
        expected_price: 预期成交价（None=用委托价）
        config:   RECONCILE_CONFIG（None=从全局 config 读取）

    返回 dict（ok=False 表示委托不存在或异常）:
        order_id, code, direction, status, order_qty, filled_qty, unfilled_qty,
        fill_ratio, order_price, expected_price, avg_price, slippage,
        slippage_exceeded, is_complete, flags[]
    """
    res = {"ok": False, "order_id": str(order_id), "flags": []}
    try:
        ledger = ledger or TradeLedger()
        od = ledger.get_order(order_id)
        if not od:
            res["flags"].append("委托不存在")
            return res

        direction = od.get("direction", "")
        status = od.get("status", "")
        order_qty = int(od.get("order_qty", 0) or 0)
        filled_qty = int(od.get("filled_qty", 0) or 0)
        order_price = float(od.get("order_price", 0) or 0)
        avg_price = float(od.get("avg_price", 0) or 0)
        exp = float(expected_price) if expected_price else order_price

        unfilled = max(order_qty - filled_qty, 0)
        fill_ratio = (filled_qty / order_qty) if order_qty > 0 else 0.0
        slip, exceeded = compute_slippage(direction, avg_price, exp, config=config)
        # FIX P2-19: 阀值显示取实际生效配置（非写死默认值）
        _buy_th, _sell_th = _cfg(config)

        res.update({
            "ok": True,
            "code": od.get("code", ""),
            "name": od.get("name", ""),
            "direction": direction,
            "status": status,
            "order_qty": order_qty,
            "filled_qty": filled_qty,
            "unfilled_qty": unfilled,
            "fill_ratio": round(fill_ratio, 4),
            "order_price": order_price,
            "expected_price": exp,
            "avg_price": round(avg_price, 4),
            "slippage": round(slip, 6) if slip is not None else None,
            "slippage_exceeded": exceeded,
            "is_complete": status == STATUS_FILLED or (order_qty > 0 and filled_qty >= order_qty),
            "status_msg": od.get("status_msg", ""),
        })

        # 差异标记
        if status == STATUS_REJECTED:
            res["flags"].append(f"拒单: {od.get('status_msg', '')}")
        elif status == STATUS_CANCELLED:
            res["flags"].append("已撤单")
        elif status == STATUS_PARTIAL or (0 < filled_qty < order_qty):
            res["flags"].append(f"部分成交: {filled_qty}/{order_qty}股，未成{unfilled}股")
        elif filled_qty == 0 and status not in (STATUS_REJECTED, STATUS_CANCELLED):
            res["flags"].append("尚无成交回报")
        if exceeded:
            res["flags"].append(
                f"滑点超标: {slip*100:+.3f}%（{direction}阈值"
                f"{'+' if direction=='买入' else ''}"
                f"{(_buy_th if direction=='买入' else _sell_th)*100:.1f}%）")
        return res
    except Exception as e:
        logger.warning(f"[订单对账] order={order_id} 异常(降级): {e}")
        res["flags"].append(f"对账异常: {e}")
        return res


def reconcile_day(ledger: TradeLedger = None, trade_date=None, config=None) -> list:
    """对账当日全部委托，返回 reconcile_order 结果列表。"""
    try:
        ledger = ledger or TradeLedger()
        orders = ledger.get_day_orders(trade_date)
        return [reconcile_order(o["order_id"], ledger=ledger, config=config)
                for o in orders]
    except Exception as e:
        logger.warning(f"[订单对账] 当日对账异常(降级): {e}")
        return []


def render_order_reconcile_html(rows: list) -> str:
    """渲染成交对账区块（五列：委托量/成交量/委托价/实际均价/滑点）。无数据返回空串。"""
    try:
        rows = [r for r in (rows or []) if r.get("ok")]
        if not rows:
            return ""
        trs = []
        for r in rows:
            slip = r.get("slippage")
            slip_txt = f"{slip*100:+.3f}%" if slip is not None else "—"
            color = "#cf1322" if r.get("slippage_exceeded") else "#389e0d"
            flag = "；".join(r.get("flags", []))
            flag_html = (f"<div style='font-size:11px;color:#d46b08'>{flag}</div>"
                         if flag else "")
            trs.append(
                f"<tr>"
                f"<td>{r.get('code','')} {r.get('name','')}</td>"
                f"<td>{r.get('direction','')}</td>"
                f"<td>{r.get('status','')}</td>"
                f"<td style='text-align:right'>{r.get('order_qty',0)}</td>"
                f"<td style='text-align:right'>{r.get('filled_qty',0)}</td>"
                f"<td style='text-align:right'>{r.get('order_price',0):.3f}</td>"
                f"<td style='text-align:right'>{r.get('avg_price',0):.3f}</td>"
                f"<td style='text-align:right;color:{color};font-weight:bold'>{slip_txt}</td>"
                f"<td>{flag_html}</td>"
                f"</tr>")
        head = ("<tr style='background:#fafafa'>"
                "<th>标的</th><th>方向</th><th>状态</th>"
                "<th>委托量</th><th>成交量</th><th>委托价</th>"
                "<th>实际均价</th><th>滑点</th><th>标记</th></tr>")
        return (f"<div style='max-width:1000px;margin:10px auto;background:#f6ffed;"
                f"border:1px solid #b7eb8f;border-radius:8px;padding:12px 18px'>"
                f"<div style='font-weight:bold;color:#237804;font-size:14px'>"
                f"🧾 成交对账（券商流水为准）</div>"
                f"<table style='width:100%;border-collapse:collapse;font-size:12px;"
                f"margin-top:8px'>{head}{''.join(trs)}</table></div>")
    except Exception as e:
        logger.warning(f"[订单对账] 区块渲染失败(不影响报告): {e}")
        return ""
