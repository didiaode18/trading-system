# -*- coding: utf-8 -*-
"""
成交/委托回报回调（fill_callback）
==================================
通过 xtquant 的 register_callback 注册本对象，接收券商推送的委托状态变化与
逐笔成交回报，全程写入 trade_ledger；并提供主动流水查询兜底，防止回调丢失
（断线、启动前已成交）导致本地记账失真。

铁律：place_order 返回订单号 ≠ 成交。真实成交量/均价一律以成交回报或券商
流水为准。

安全约束：
  - 回调在 xtquant 线程触发，全部方法 try/except 静默降级，绝不抛异常
  - 字段解析同时兼容 xtquant 对象（getattr）与纯 dict（模拟测试）
  - 断线只置标记，恢复后由 sync_from_trader 主动补全，不臆造成交
"""

import os
import json
import logging
import datetime

from .trade_ledger import (
    TradeLedger, canonical_status, canonical_direction,
    STATUS_SUBMITTED, STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED,
)

logger = logging.getLogger(__name__)


def _repo_root() -> str:
    """仓库根（trades_today.json 位于项目根，非 trading_system/）。"""
    # 本文件位于 trading_system/execution/ → 上溯两级到仓库根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _trades_today_path() -> str:
    """trades_today.json 写入路径（仓库根，与 order_lifecycle 读取位置一致）。"""
    root = _repo_root()
    cand = os.path.join(root, "trades_today.json")
    if os.path.exists(cand):
        return cand
    inner = os.path.join(root, "trading_system", "trades_today.json")
    if os.path.exists(inner):
        return inner
    return cand


def _norm_time(raw) -> str:
    """traded_time 归一为 HH:MM:SS：兼容 epoch 秒(int/数字串) 与已是时间串。"""
    try:
        s = str(raw or "").strip()
        if not s:
            return datetime.datetime.now().strftime("%H:%M:%S")
        if s.isdigit():
            return datetime.datetime.fromtimestamp(int(s)).strftime("%H:%M:%S")
        if ":" in s:
            return s.split(" ")[-1][:8]
        return s[:8]
    except Exception:
        return datetime.datetime.now().strftime("%H:%M:%S")


def _field(obj, *names, default=None):
    """从 xtquant 对象或 dict 中按候选名取第一个存在的字段。"""
    for n in names:
        try:
            if isinstance(obj, dict):
                if n in obj and obj[n] is not None:
                    return obj[n]
            else:
                if hasattr(obj, n):
                    v = getattr(obj, n)
                    if v is not None:
                        return v
        except Exception:
            continue
    return default


class FillCallback:
    """XtQuant 交易回调对象（on_stock_order / on_stock_trade / on_disconnected）。

    继承 xtquant XtQuantTraderCallback 由其动态决定：真实模式下 register_callback
    只要求鸭子类型（具备同名方法），无需强依赖基类，故此处不继承，便于无 xtquant 测试。
    """

    def __init__(self, ledger: TradeLedger = None):
        self.ledger = ledger or TradeLedger()
        self.disconnected = False
        self.order_events = 0    # 收到的委托回报计数
        self.trade_events = 0    # 收到的成交回报计数
        self.rejected_orders = []  # 拒单/废单 order_id 列表（供上层清除执行标志）

    # ---- FIX P1-21: trades_today.json 写入方 ----
    def _append_trades_today(self, code, name, direction, qty, price,
                             traded_time):
        """将一笔真实成交追写入 trades_today.json（order_lifecycle 回填数据源）。

        以前全库零写入方 → 文件过期2个月 → executed 恒空 → 条件单被写死
        "未触发"。现由成交回报/流水回填接通。全程 try/except 降级，绝不阻断主流程。
        去重键 (time, code, direction, qty, price)，原子写，自动重算 summary。
        仅真实回报触发（on_stock_trade / sync_trades），不污染模拟。
        """
        try:
            if not code or not qty or not price:
                return
            path = _trades_today_path()
            today = datetime.date.today().isoformat()
            t_time = _norm_time(traded_time)
            payload = {"date": today, "trades": [], "summary": {}}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        old = json.load(f)
                    if isinstance(old, dict):
                        # 跨日重置：旧文件日期非今日则开新一天的 trades
                        if old.get("date") == today and isinstance(old.get("trades"), list):
                            payload["trades"] = old["trades"]
                except Exception:
                    pass  # 旧文件损坏则以空重建
            # 去重：同 (time, code, direction, qty, price) 不重复追写
            for t in payload["trades"]:
                if (str(t.get("time")) == t_time and str(t.get("code")) == str(code)
                        and t.get("direction") == direction
                        and int(t.get("qty", 0) or 0) == int(qty)
                        and abs(float(t.get("price", 0) or 0) - float(price)) < 1e-6):
                    return
            payload["trades"].append({
                "time": t_time, "code": str(code), "name": name or "",
                "direction": direction, "qty": int(qty),
                "price": round(float(price), 4),
                "amount": round(float(price) * int(qty), 2), "status": "已成",
            })
            payload["trades"].sort(key=lambda x: str(x.get("time", "")), reverse=True)
            payload["summary"] = self._summarize(payload["trades"])
            # 原子写
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"[回调] 追写 trades_today.json 失败(降级): {e}")

    @staticmethod
    def _summarize(trades: list) -> dict:
        """重算 trades_today.json 的 summary 块。"""
        buy_amt = sum(float(t.get("amount", 0) or 0) for t in trades
                      if t.get("direction") == "买入")
        sell_amt = sum(float(t.get("amount", 0) or 0) for t in trades
                       if t.get("direction") == "卖出")
        buy_cnt = sum(1 for t in trades if t.get("direction") == "买入")
        sell_cnt = sum(1 for t in trades if t.get("direction") == "卖出")
        return {
            "total_trades": len(trades),
            "buy_count": buy_cnt, "sell_count": sell_cnt,
            "buy_amount": round(buy_amt, 2), "sell_amount": round(sell_amt, 2),
            "net_flow": round(buy_amt - sell_amt, 2),
        }

    # ---- 委托状态回报 ----
    def on_stock_order(self, order):
        """委托状态变化：已报/部分成交/已成交/已撤/拒单，全程留痕。"""
        try:
            self.order_events += 1
            order_id = _field(order, "order_id", "orderId")
            code = _field(order, "stock_code", "code", default="")
            direction = canonical_direction(
                _field(order, "order_type", "direction", "offset_flag", default=""))
            price = float(_field(order, "price", "order_price", default=0) or 0)
            qty = int(_field(order, "order_volume", "volume", "qty", default=0) or 0)
            raw_status = _field(order, "order_status", "status", default="")
            status = canonical_status(raw_status)
            status_msg = str(_field(order, "status_msg", "msg", default="") or "")
            traded_volume = int(_field(order, "traded_volume", default=0) or 0)
            traded_price = float(_field(order, "traded_price", default=0) or 0)

            if not order_id:
                logger.debug("[回调] 委托回报缺 order_id，忽略")
                return

            self.ledger.record_order(
                order_id=order_id, code=code, direction=direction,
                order_price=price, order_qty=qty, status=status or STATUS_SUBMITTED,
                status_msg=status_msg, source="callback")

            # 委托回报自带的成交量/均价（部分券商推送），仅在有效时回填
            fill_qty = traded_volume if traded_volume > 0 else None
            fill_avg = traded_price if traded_price > 0 else None
            self.ledger.update_order_status(
                order_id, status or STATUS_SUBMITTED, status_msg=status_msg,
                filled_qty=fill_qty, avg_price=fill_avg)

            if status == STATUS_REJECTED:
                self.rejected_orders.append(str(order_id))
                logger.warning(f"[回调] 🚫 拒单/废单 order={order_id} {code} | {status_msg}")
            elif status in (STATUS_CANCELLED,):
                logger.info(f"[回调] 已撤 order={order_id} {code}")
            elif status == STATUS_FILLED:
                logger.info(f"[回调] 已成 order={order_id} {code} "
                            f"{traded_volume}股 @ {traded_price}")
        except Exception as e:
            logger.warning(f"[回调] on_stock_order 处理异常(降级): {e}")

    # ---- 逐笔成交回报 ----
    def on_stock_trade(self, trade):
        """逐笔成交：累积成交量，台账自动重算实际均价 Σ(价×量)/Σ量。"""
        try:
            self.trade_events += 1
            traded_id = _field(trade, "traded_id", "tradeId", "trade_id", default="")
            order_id = _field(trade, "order_id", "orderId")
            code = _field(trade, "stock_code", "code", default="")
            direction = canonical_direction(
                _field(trade, "order_type", "direction", "offset_flag", default=""))
            price = float(_field(trade, "traded_price", "price", default=0) or 0)
            volume = int(_field(trade, "traded_volume", "volume", "qty", default=0) or 0)
            traded_time = str(_field(trade, "traded_time", "time", default="") or "")

            if order_id is None or order_id == "":
                logger.debug("[回调] 成交回报缺 order_id，忽略")
                return

            inserted = self.ledger.record_trade(
                traded_id=traded_id, order_id=order_id, code=code,
                direction=direction, traded_price=price, traded_volume=volume,
                traded_time=traded_time)
            if inserted:
                logger.info(f"[回调] 💰 成交 order={order_id} {code} "
                            f"{direction} {volume}股 @ {price}")
                # FIX P1-21: 接通 trades_today.json 写入（供盘后回填/对账消费）
                name = _field(trade, "stock_name", "name", default="")
                self._append_trades_today(code, name, direction, volume,
                                          price, traded_time)
        except Exception as e:
            logger.warning(f"[回调] on_stock_trade 处理异常(降级): {e}")

    # ---- 断线 ----
    def on_disconnected(self):
        """断线标记：恢复后需主动拉流水补全，不得假设期间无成交。"""
        self.disconnected = True
        logger.warning("[回调] ⚠️ 与QMT断线，成交回报可能丢失，恢复后将主动补全流水")

    def on_connected(self):
        """连接恢复：清除断线标记（补全由 sync_from_trader 触发）。"""
        if self.disconnected:
            logger.info("[回调] 连接恢复，建议执行 sync_from_trader 补全流水")
        self.disconnected = False

    # 兼容 xtquant 其它回调（空实现，避免 AttributeError）
    def on_stock_asset(self, asset):
        pass

    def on_stock_position(self, position):
        pass

    def on_cancel_error(self, cancel_error):
        """FIX P1-13: 撤单【失败】回报——委托仍活，绝不能写成 CANCELLED（活单记死单）。
        仅在 status_msg 追加 cancel_failed 标记，保持台账原有状态。"""
        try:
            order_id = _field(cancel_error, "order_id")
            msg = _field(cancel_error, "error_msg", "msg", default="")
            if order_id:
                row = self.ledger.get_order(order_id)
                cur_status = row.get("status") if row else None
                # 不改状态，只留痕撤单失败
                self.ledger.update_order_status(
                    order_id, cur_status or STATUS_SUBMITTED,
                    status_msg=f"cancel_failed:{msg}")
                logger.warning(
                    f"[回调] ⚠️ 撤单失败 order={order_id} | {msg}（保持原状态{cur_status}）")
        except Exception as e:
            logger.debug(f"[回调] on_cancel_error 处理异常(降级): {e}")

    def on_order_error(self, order_error):
        """下单失败回报：标记拒单，供上层清除执行标志。"""
        try:
            order_id = _field(order_error, "order_id")
            msg = _field(order_error, "error_msg", "msg", default="")
            if order_id:
                self.ledger.update_order_status(order_id, STATUS_REJECTED,
                                                status_msg=str(msg))
                self.rejected_orders.append(str(order_id))
                logger.warning(f"[回调] 🚫 下单失败 order={order_id} | {msg}")
        except Exception:
            pass

    # ---- 主动流水查询兜底 ----
    def sync_orders(self, order_list) -> int:
        """以券商委托流水为权威回填订单（回调丢失兜底）。返回新增/更新条数。"""
        n = 0
        try:
            for od in (order_list or []):
                order_id = _field(od, "order_id", "orderId")
                if not order_id:
                    continue
                code = _field(od, "stock_code", "code", default="")
                direction = canonical_direction(
                    _field(od, "order_type", "direction", default=""))
                price = float(_field(od, "price", "order_price", default=0) or 0)
                qty = int(_field(od, "order_volume", "volume", default=0) or 0)
                status = canonical_status(
                    _field(od, "order_status", "status", default=""))
                traded_volume = int(_field(od, "traded_volume", default=0) or 0)
                traded_price = float(_field(od, "traded_price", default=0) or 0)
                self.ledger.record_order(
                    order_id=order_id, code=code, direction=direction,
                    order_price=price, order_qty=qty,
                    status=status or STATUS_SUBMITTED, source="query")
                self.ledger.update_order_status(
                    order_id, status or STATUS_SUBMITTED,
                    filled_qty=traded_volume if traded_volume > 0 else None,
                    avg_price=traded_price if traded_price > 0 else None)
                n += 1
        except Exception as e:
            logger.warning(f"[回调] 委托流水回填异常(降级): {e}")
        return n

    def sync_trades(self, trade_list) -> int:
        """以券商成交流水为权威回填成交（按 traded_id 去重）。返回新增条数。"""
        n = 0
        try:
            for td in (trade_list or []):
                order_id = _field(td, "order_id", "orderId")
                if order_id is None or order_id == "":
                    continue
                code = _field(td, "stock_code", "code", default="")
                direction = canonical_direction(
                    _field(td, "order_type", "direction", default=""))
                price = float(_field(td, "traded_price", "price", default=0) or 0)
                volume = int(_field(td, "traded_volume", "volume", default=0) or 0)
                traded_time = str(_field(td, "traded_time", "time", default="") or "")
                inserted = self.ledger.record_trade(
                    traded_id=_field(td, "traded_id", "trade_id", default=""),
                    order_id=order_id, code=code, direction=direction,
                    traded_price=price, traded_volume=volume,
                    traded_time=traded_time)
                if inserted:
                    n += 1
                    # FIX P1-21: 流水回填的成交同步追写 trades_today.json
                    name = _field(td, "stock_name", "name", default="")
                    self._append_trades_today(code, name, direction, volume,
                                              price, traded_time)
        except Exception as e:
            logger.warning(f"[回调] 成交流水回填异常(降级): {e}")
        return n

    def sync_from_trader(self, trader) -> dict:
        """连接后/盘后调用：从 QMTTrader 拉取当日全量委托与成交补全台账。

        真实模式经 trader.trader.query_stock_orders/query_stock_trades；
        模拟模式（has_xtquant=False）安全返回 completed=False，禁止假报"已补全"。
        """
        result = {"completed": False, "orders": 0, "trades": 0}
        try:
            inner = getattr(trader, "trader", None)
            account = getattr(trader, "account", None)
            if not getattr(trader, "has_xtquant", False) or inner is None:
                logger.info("[回调] 模拟模式/未连接，跳过主动流水查询(对账未完成)")
                return result
            orders = inner.query_stock_orders(account) or []
            trades = inner.query_stock_trades(account) or []
            result["orders"] = self.sync_orders(orders)
            result["trades"] = self.sync_trades(trades)
            result["completed"] = True
            self.disconnected = False
            logger.info(f"[回调] 流水补全: 委托{result['orders']}条 成交{result['trades']}笔")
        except Exception as e:
            logger.warning(f"[回调] 主动流水查询失败(对账未完成): {e}")
        return result
