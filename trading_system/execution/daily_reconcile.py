# -*- coding: utf-8 -*-
"""
盘后账实对账（daily_reconcile）
===============================
交易日 15:30 后执行，以券商数据为唯一权威，逐项核对：
  1. 持仓对账：query_stock_positions 的 代码/数量/可用量/成本 vs holdings.json
  2. 资金对账：query_stock_asset 的 总资产/可用/市值 vs 本地净值估算
  3. 成交对账：当日每笔委托给出 委托量/成交量/委托价/实际均价/滑点 五列
差异写入 reconcile_ledger（同日去重）并通过现有 notify 通道告警一次。

铁律与降级：
  - 拿不到券商权威数据（模拟模式/查询失败）时 completed=False，明确"对账未完成"，
    绝不输出"全部一致"的假结论。
  - 自动修正仅生成建议，是否改 holdings 由人工确认（初期不自动写回）。
"""

import os
import json
import logging
import datetime

from .trade_ledger import TradeLedger
from .order_reconcile import reconcile_day, render_order_reconcile_html

logger = logging.getLogger(__name__)


def _project_root() -> str:
    try:
        import config
        return config.PROJECT_ROOT
    except Exception:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm_code(code) -> str:
    """统一去除交易所后缀：588000.SH → 588000。"""
    return str(code or "").split(".")[0].strip()


def _find_holdings(path=None) -> str:
    """holdings.json 可能在项目根或 trading_system/ 下，两处都找。"""
    if path and os.path.exists(path):
        return path
    pr = _project_root()
    for cand in (os.path.join(os.path.dirname(pr), "holdings.json"),
                 os.path.join(pr, "holdings.json")):
        if os.path.exists(cand):
            return cand
    return ""


def _cfg(config=None) -> dict:
    try:
        if config is None:
            import config as _c
            return getattr(_c, "RECONCILE_CONFIG", {}) or {}
        return config or {}
    except Exception:
        return {}


class DailyReconciler:
    """盘后账实对账器。"""

    def __init__(self, trader=None, ledger: TradeLedger = None,
                 holdings_path: str = None, notify: bool = True, config=None):
        self.trader = trader
        self.ledger = ledger or TradeLedger()
        self.holdings_path = holdings_path
        self.notify = notify
        self.config = _cfg(config)
        self.asset_tol = float(self.config.get("asset_tolerance_pct", 0.005))

    # ---- 本地数据 ----
    def _load_local_holdings(self) -> dict:
        """读取 holdings.json → {code: {shares, buy_price, current_price, market_value}}。"""
        result = {}
        try:
            path = _find_holdings(self.holdings_path)
            if not path:
                return result
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for code, info in data.items():
                    if not isinstance(info, dict):
                        continue
                    result[_norm_code(code)] = {
                        "name": info.get("name", ""),
                        "shares": int(info.get("shares", 0) or 0),
                        "buy_price": float(info.get("buy_price", 0) or 0),
                        "current_price": float(info.get("current_price", 0) or 0),
                        "market_value": float(info.get("market_value", 0) or 0),
                    }
        except Exception as e:
            logger.warning(f"[对账] 读取本地持仓失败(降级): {e}")
        return result

    def _local_net_value(self, holdings: dict) -> float:
        """本地净值估算：Σ 市值（缺失市值时用 shares×current_price）。"""
        total = 0.0
        for h in holdings.values():
            mv = h.get("market_value", 0) or 0
            if mv <= 0:
                mv = h.get("shares", 0) * h.get("current_price", 0)
            total += mv
        return total

    # ---- 券商数据 ----
    def _pull_broker(self):
        """从 trader 拉取券商持仓/资金；模拟模式或失败返回 (None, None, False)。"""
        if self.trader is None:
            return None, None, False
        try:
            authoritative = bool(getattr(self.trader, "has_xtquant", False)
                                 and getattr(self.trader, "connected", False))
            if not authoritative:
                return None, None, False
            positions = self.trader.get_positions() or []
            asset = self.trader.get_account_info() or {}
            return positions, asset, True
        except Exception as e:
            logger.warning(f"[对账] 拉取券商数据失败: {e}")
            return None, None, False

    # ---- 持仓对账 ----
    def _reconcile_positions(self, broker_positions, local_holdings) -> list:
        """逐行比对券商持仓与本地 holdings，返回差异列表。"""
        diffs = []
        broker_map = {}
        for p in (broker_positions or []):
            code = _norm_code(p.get("code", ""))
            if not code:
                continue
            broker_map[code] = {
                "volume": int(p.get("volume", 0) or 0),
                "available": int(p.get("available", 0) or 0),
                "cost": float(p.get("cost", 0) or 0),
            }
        all_codes = set(broker_map) | set(local_holdings)
        for code in sorted(all_codes):
            b = broker_map.get(code)
            l = local_holdings.get(code)
            b_vol = b["volume"] if b else 0
            l_vol = l["shares"] if l else 0
            if b_vol != l_vol:
                name = (l or {}).get("name", "")
                if b is None:
                    detail = f"本地持仓{name} {l_vol}股，券商无此持仓"
                elif l is None:
                    detail = f"券商持仓 {b_vol}股，本地 holdings 缺失"
                else:
                    detail = f"数量不一致 券商{b_vol}股 vs 本地{l_vol}股（差{b_vol-l_vol:+d}）"
                diffs.append({
                    "type": "position", "code": code, "name": name,
                    "broker": b_vol, "local": l_vol, "detail": detail,
                })
            # 成本偏差（仅双方都持仓时）
            if b and l and b["cost"] > 0 and l["buy_price"] > 0:
                cost_diff = abs(b["cost"] - l["buy_price"]) / l["buy_price"]
                if cost_diff > self.asset_tol:
                    diffs.append({
                        "type": "position_cost", "code": code,
                        "name": (l or {}).get("name", ""),
                        "broker": round(b["cost"], 4), "local": round(l["buy_price"], 4),
                        "detail": f"成本价偏差 券商{b['cost']:.3f} vs 本地{l['buy_price']:.3f}"
                                  f"（{cost_diff*100:.2f}%）",
                    })
        return diffs

    # ---- 资金对账 ----
    def _reconcile_asset(self, broker_asset, local_net) -> dict:
        """比对券商总资产/市值 vs 本地净值估算。"""
        res = {"ok": False}
        try:
            if not broker_asset:
                return {"ok": False, "detail": "券商资金不可用"}
            b_total = float(broker_asset.get("total_asset", 0) or 0)
            b_mv = float(broker_asset.get("market_value", 0) or 0)
            b_cash = float(broker_asset.get("cash", 0) or 0)
            res = {
                "ok": True,
                "broker_total": b_total, "broker_cash": b_cash,
                "broker_market_value": b_mv, "local_net_value": round(local_net, 2),
                "diffs": [],
            }
            # 券商市值 vs 本地持仓市值估算
            if b_mv > 0 and local_net > 0:
                dev = abs(b_mv - local_net) / b_mv
                if dev > self.asset_tol:
                    res["diffs"].append({
                        "type": "asset", "code": "", "name": "市值",
                        "broker": round(b_mv, 2), "local": round(local_net, 2),
                        "detail": f"持仓市值偏差 券商{b_mv:,.0f} vs 本地{local_net:,.0f}"
                                  f"（{dev*100:.2f}%）",
                    })
            return res
        except Exception as e:
            logger.warning(f"[对账] 资金对账异常(降级): {e}")
            return {"ok": False, "detail": str(e)}

    # ---- 差异告警（去重）----
    def _record_and_alert(self, diffs, recon_date) -> int:
        """写台账去重，返回新增差异数；有新增才聚合告警一次。"""
        new_count = 0
        try:
            for d in diffs:
                is_new = self.ledger.record_diff(
                    recon_type=d.get("type", "position"),
                    code=d.get("code", ""),
                    diff_detail=d.get("detail", ""),
                    recon_date=recon_date)
                if is_new:
                    new_count += 1
            if new_count > 0 and self.notify:
                self._send_alert(diffs, new_count, recon_date)
        except Exception as e:
            logger.warning(f"[对账] 差异告警异常(降级): {e}")
        return new_count

    def _send_alert(self, diffs, new_count, recon_date):
        """通过现有 notify 通道发送聚合告警（复用企业微信/邮件，不新增通道）。"""
        try:
            lines = [f"🧾 账实对账差异 {recon_date}（新增{new_count}项）"]
            for d in diffs[:15]:
                lines.append(f"· [{d.get('type')}] {d.get('code','')} {d.get('detail','')}")
            if len(diffs) > 15:
                lines.append(f"… 另有{len(diffs)-15}项，详见对账台账")
            lines.append("以券商流水为准，请人工核对后确认是否修正 holdings。")
            content = "\n".join(lines)
            logger.warning("[对账告警]\n" + content)
            try:
                from notify.wechat_notify import send_notification
                send_notification(f"[账实对账] {recon_date} 发现{new_count}项差异", content)
            except Exception:
                try:
                    import sys
                    sys.path.insert(0, _project_root())
                    from notify.wechat_notify import send_notification
                    send_notification(f"[账实对账] {recon_date} 发现{new_count}项差异", content)
                except Exception as ne:
                    logger.debug(f"[对账告警] notify 不可用(降级): {ne}")
        except Exception as e:
            logger.debug(f"[对账告警] 组装失败(降级): {e}")

    # ---- 主流程 ----
    def run(self, broker_positions=None, broker_asset=None, trade_date=None) -> dict:
        """执行盘后账实对账。

        参数 broker_positions/broker_asset 可显式注入（测试或外部已拉取的权威数据）；
        未注入时从 trader 拉取。拿不到权威数据 → completed=False。

        返回:
            {completed, has_diff, positions:[diff], asset:{...}, orders:[...],
             diffs:[...], new_diffs:int, recon_date, message}
        """
        recon_date = trade_date or datetime.date.today().isoformat()
        result = {
            "completed": False, "has_diff": False, "positions": [],
            "asset": {}, "orders": [], "diffs": [], "new_diffs": 0,
            "recon_date": recon_date, "message": "",
        }
        try:
            injected = broker_positions is not None or broker_asset is not None
            if injected:
                positions = broker_positions or []
                asset = broker_asset or {}
                authoritative = True
            else:
                positions, asset, authoritative = self._pull_broker()

            if not authoritative:
                result["message"] = "对账未完成：无法获取券商权威数据（模拟模式/未连接/查询失败）"
                logger.warning(f"[对账] {result['message']}")
                return result

            local_holdings = self._load_local_holdings()
            local_net = self._local_net_value(local_holdings)

            # 1. 持仓对账
            pos_diffs = self._reconcile_positions(positions, local_holdings)
            # 2. 资金对账
            asset_res = self._reconcile_asset(asset, local_net)
            asset_diffs = asset_res.get("diffs", []) if asset_res.get("ok") else []
            if not asset_res.get("ok"):
                result["message"] = f"对账未完成：资金对账失败（{asset_res.get('detail','')}）"
            # 3. 成交对账（读台账，已由回调/流水查询回填）
            order_rows = reconcile_day(self.ledger, recon_date, config=self.config)
            order_diffs = []
            for r in order_rows:
                if r.get("slippage_exceeded") or r.get("flags"):
                    order_diffs.append({
                        "type": "order", "code": r.get("code", ""),
                        "name": r.get("name", ""),
                        "detail": f"{r.get('direction','')} {r.get('filled_qty',0)}/"
                                  f"{r.get('order_qty',0)}股 均价{r.get('avg_price',0):.3f}"
                                  f"{'；'.join(r.get('flags', []))}",
                    })

            all_diffs = pos_diffs + asset_diffs + order_diffs
            new_diffs = self._record_and_alert(all_diffs, recon_date)

            result.update({
                "completed": True,
                "has_diff": len(all_diffs) > 0,
                "positions": pos_diffs,
                "asset": asset_res,
                "orders": order_rows,
                "diffs": all_diffs,
                "new_diffs": new_diffs,
            })
            if not result["message"]:
                result["message"] = (
                    f"对账完成：持仓差异{len(pos_diffs)}项 资金差异{len(asset_diffs)}项 "
                    f"成交异常{len(order_diffs)}项 新增告警{new_diffs}项"
                    if all_diffs else "对账完成：账实一致")
            logger.info(f"[对账] {result['message']}")
            return result
        except Exception as e:
            result["message"] = f"对账未完成：异常 {e}"
            logger.warning(f"[对账] 执行异常(降级): {e}")
            return result


def render_reconcile_section_html(recon: dict) -> str:
    """渲染盘后对账区块（持仓/资金差异 + 成交五列表）。未完成时明确标注。"""
    try:
        if not recon:
            return ""
        if not recon.get("completed"):
            return (f"<div style='max-width:1000px;margin:10px auto;background:#fff2f0;"
                    f"border:1px solid #ffccc7;border-radius:8px;padding:12px 18px'>"
                    f"<div style='font-weight:bold;color:#cf1322;font-size:14px'>"
                    f"⚠️ 账实对账未完成</div>"
                    f"<div style='font-size:12px;margin-top:6px'>"
                    f"{recon.get('message','无法获取券商数据')}。"
                    f"结论不可信，禁止据此判断账实一致。</div></div>")

        parts = []
        pos = recon.get("positions", [])
        asset = recon.get("asset", {})
        asset_diffs = asset.get("diffs", []) if isinstance(asset, dict) else []
        if pos or asset_diffs:
            rows = []
            for d in pos + asset_diffs:
                rows.append(f"<tr><td>{d.get('code','')}</td><td>{d.get('type','')}</td>"
                            f"<td style='text-align:right'>{d.get('broker','')}</td>"
                            f"<td style='text-align:right'>{d.get('local','')}</td>"
                            f"<td>{d.get('detail','')}</td></tr>")
            head = ("<tr style='background:#fafafa'><th>标的</th><th>类型</th>"
                    "<th>券商</th><th>本地</th><th>差异</th></tr>")
            parts.append(f"<table style='width:100%;border-collapse:collapse;"
                         f"font-size:12px;margin-top:6px'>{head}{''.join(rows)}</table>")
        else:
            parts.append("<div style='font-size:12px;color:#389e0d;margin-top:6px'>"
                         "✅ 持仓与资金账实一致</div>")

        # 成交五列表
        order_html = render_order_reconcile_html(recon.get("orders", []))
        bg = "#fff2f0" if recon.get("has_diff") else "#f6ffed"
        border = "#ffccc7" if recon.get("has_diff") else "#b7eb8f"
        title_color = "#cf1322" if recon.get("has_diff") else "#237804"
        title = ("⚠️ 账实对账（发现差异，以券商为准）" if recon.get("has_diff")
                 else "🧾 账实对账（一致）")
        return (f"<div style='max-width:1000px;margin:10px auto;background:{bg};"
                f"border:1px solid {border};border-radius:8px;padding:12px 18px'>"
                f"<div style='font-weight:bold;color:{title_color};font-size:14px'>{title}</div>"
                f"<div style='font-size:12px;color:#595959;margin-top:4px'>"
                f"{recon.get('message','')}</div>{''.join(parts)}{order_html}</div>")
    except Exception as e:
        logger.warning(f"[对账] 区块渲染失败(不影响报告): {e}")
        return ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    rec = DailyReconciler(notify=False).run()
    print(rec.get("message"))
