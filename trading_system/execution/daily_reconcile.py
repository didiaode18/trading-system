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
            b_frozen = float(broker_asset.get("frozen", 0) or 0)
            res = {
                "ok": True,
                "broker_total": b_total, "broker_cash": b_cash,
                "broker_market_value": b_mv, "local_net_value": round(local_net, 2),
                "diffs": [],
            }
            # FIX P1-15: 空侧判差异而非静默跳过。
            # 券商有市值而本地净值为0（holdings缺失/读取失败）→ 明确报差异，
            # 绝不因“两边都拿不到数据”而得出“账实一致”的假结论。
            if b_mv > 0 and local_net <= 0:
                res["diffs"].append({
                    "type": "asset", "code": "", "name": "市值",
                    "broker": round(b_mv, 2), "local": round(local_net, 2),
                    "detail": f"本地持仓净值为0但券商市值{b_mv:,.0f}"
                              f"（holdings缺失/读取失败，结论不可信）",
                })
            elif b_mv > 0 and local_net > 0:
                # 券商市值 vs 本地持仓市值估算
                dev = abs(b_mv - local_net) / b_mv
                if dev > self.asset_tol:
                    res["diffs"].append({
                        "type": "asset", "code": "", "name": "市值",
                        "broker": round(b_mv, 2), "local": round(local_net, 2),
                        "detail": f"持仓市值偏差 券商{b_mv:,.0f} vs 本地{local_net:,.0f}"
                                  f"（{dev*100:.2f}%）",
                    })
            # FIX P1-15: b_cash 取出从不比对 → 补券商内部一致性校验
            # 总资产 应≈ 现金 + 冻结 + 市值，偏差超阈值说明券商口径异常或字段错位。
            if b_total > 0 and (b_cash > 0 or b_mv > 0):
                implied = b_cash + b_frozen + b_mv
                dev_total = abs(b_total - implied) / b_total
                if dev_total > self.asset_tol:
                    res["diffs"].append({
                        "type": "asset", "code": "", "name": "资金一致性",
                        "broker": round(b_total, 2), "local": round(implied, 2),
                        "detail": f"券商总资产{b_total:,.0f} ≠ 现金{b_cash:,.0f}"
                                  f"+冻结{b_frozen:,.0f}+市值{b_mv:,.0f}（偏差{dev_total*100:.2f}%）",
                    })
            return res
        except Exception as e:
            logger.warning(f"[对账] 资金对账异常(降级): {e}")
            return {"ok": False, "detail": str(e)}

    # ---- 差异告警（去重）----
    def _record_and_alert(self, diffs, recon_date) -> int:
        """FIX P1-12: 先告警成功再落“已告警”标记，杜绝差异永久静默丢失。
    
        原逻辑先落库去重（立即 commit）后发送，而 send_notification 从不抛异常且不检查
        返回值 → 发送失败时差异已被标记“已告警”，次日因去重不再发。现改为：
        每条 diff 独立 try；先筛新增→告警→仅在告警成功后才 record_diff 落标记。
        """
        if not diffs:
            return 0
        # 1. 筛出近窗口首次出现的新增差异（跨日去重）
        new_diffs = []
        for d in diffs:
            try:
                if self.ledger.is_new_diff(
                        d.get("type", "position"), d.get("code", ""),
                        d.get("detail", ""), recon_date):
                    new_diffs.append(d)
            except Exception:
                new_diffs.append(d)  # 查询失败保守视为新增
        if not new_diffs:
            return 0
    
        # 2. 先告警（未启用通知时视为已处理，直接落库）
        alerted = True
        if self.notify:
            alerted = self._send_alert(new_diffs, len(new_diffs), recon_date)
    
        # 3. 仅在告警成功（或未启用通知）后才落库标记“已告警”
        if not alerted:
            logger.error(
                f"[对账] ❌ 告警发送失败，{len(new_diffs)}项差异未落标记，"
                f"将于下次对账重试（避免静默丢失）")
            return 0
    
        new_count = 0
        for d in new_diffs:
            try:
                self.ledger.record_diff(
                    recon_type=d.get("type", "position"),
                    code=d.get("code", ""),
                    diff_detail=d.get("detail", ""),
                    recon_date=recon_date)
                new_count += 1
            except Exception as e:
                logger.error(f"[对账] 单条差异落库失败(不影响其余): {e}")
        return new_count
    
    def _send_alert(self, diffs, new_count, recon_date) -> bool:
        """FIX P1-12: 发送聚合告警，检查返回 dict；企业微信/钉钉均失败时邮件兜底。
    
        返回 True=至少一个通道发送成功，False=全部失败（上层据此不落“已告警”标记）。
        """
        try:
            lines = [f"🧾 账实对账差异 {recon_date}（新增{new_count}项）"]
            for d in diffs[:15]:
                lines.append(f"· [{d.get('type')}] {d.get('code','')} {d.get('detail','')}")
            if len(diffs) > 15:
                lines.append(f"… 另有{len(diffs)-15}项，详见对账台账")
            lines.append("以券商流水为准，请人工核对后确认是否修正 holdings。")
            content = "\n".join(lines)
            title = f"[账实对账] {recon_date} 发现{new_count}项差异"
            logger.warning("[对账告警]\n" + content)
    
            # 通道1：企业微信/钉钉（检查返回 dict）
            try:
                from notify.wechat_notify import send_notification
                res = send_notification(title, content)
                if isinstance(res, dict) and any(res.values()):
                    return True
                logger.warning(f"[对账告警] 即时通道均未发送成功: {res}，尝试邮件兜底")
            except Exception as ne:
                logger.warning(f"[对账告警] 即时通道异常: {ne}，尝试邮件兜底")
    
            # 通道2：邮件兜底
            try:
                from notify.email_notify import send_email
                ok = send_email(title, content)
                if ok:
                    logger.info("[对账告警] 邮件兜底发送成功")
                    return True
                logger.error("[对账告警] 邮件兜底发送失败")
            except Exception as ee:
                logger.error(f"[对账告警] 邮件兜底异常: {ee}")
            return False
        except Exception as e:
            logger.error(f"[对账告警] 组装失败: {e}")
            return False

    # ---- 对账状态跟踪（补跑 + 连续未对账升级）----
    def _status_file(self) -> str:
        """reconcile_status.json 路径（记录已对账交易日与连续失败天数）。

        优先级：config.status_file 显式指定 > 与 holdings_path 同目录（便于测试隔离）
        > DATA_DIR。避免测试运行时污染生产对账状态。
        """
        explicit = self.config.get("status_file")
        if explicit:
            return explicit
        if self.holdings_path:
            return os.path.join(os.path.dirname(self.holdings_path),
                                "reconcile_status.json")
        try:
            import config as _c
            base = getattr(_c, "DATA_DIR", os.path.join(_project_root(), "data"))
        except Exception:
            base = os.path.join(_project_root(), "data")
        return os.path.join(base, "reconcile_status.json")

    def _load_status(self) -> dict:
        try:
            p = self._status_file()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("reconciled_days", [])
                    data.setdefault("fail_streak", 0)
                    data.setdefault("last_fail_date", "")
                    return data
        except Exception as e:
            logger.warning(f"[对账] 读取对账状态失败(降级): {e}")
        return {"reconciled_days": [], "fail_streak": 0, "last_fail_date": ""}

    def _save_status(self, status: dict):
        try:
            p = self._status_file()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception as e:
            logger.warning(f"[对账] 写入对账状态失败(降级): {e}")

    def _mark_reconciled(self, recon_date: str):
        """标记该交易日对账成功，清零连续失败计数。"""
        status = self._load_status()
        days = status.get("reconciled_days", [])
        if recon_date not in days:
            days.append(recon_date)
            # 只保留最近 60 个交易日记录，避免无限增长
            status["reconciled_days"] = sorted(days)[-60:]
        status["fail_streak"] = 0
        status["last_fail_date"] = ""
        self._save_status(status)

    def _consecutive_failures(self, recon_date: str) -> int:
        """累加连续未完成对账的交易日数（同一日多次运行不重复累加）。"""
        status = self._load_status()
        if status.get("last_fail_date") == recon_date:
            return int(status.get("fail_streak", 0))
        streak = int(status.get("fail_streak", 0)) + 1
        status["fail_streak"] = streak
        status["last_fail_date"] = recon_date
        self._save_status(status)
        return streak

    def _pending_reconcile_days(self, recon_date: str, lookback: int = 10) -> list:
        """返回最近 lookback 个交易日内尚未成功对账的日期（供补跑）。"""
        pending = []
        try:
            from utils.trading_calendar import is_trading_day
            status = self._load_status()
            done = set(status.get("reconciled_days", []))
            d = datetime.date.fromisoformat(recon_date)
            checked = 0
            while checked < lookback:
                iso = d.isoformat()
                try:
                    trading = is_trading_day(iso)
                except Exception:
                    trading = d.weekday() < 5
                if trading and iso not in done:
                    pending.append(iso)
                if trading:
                    checked += 1
                d -= datetime.timedelta(days=1)
        except Exception as e:
            logger.warning(f"[对账] 计算待补跑交易日失败(降级): {e}")
        return sorted(pending)

    def _escalate_alert(self, recon_date: str, streak: int, message: str):
        """连续 N 日未完成对账 → 邮件升级告警（区别于普通差异告警）。"""
        try:
            title = f"[账实对账] ⚠️ 连续{streak}日未完成对账"
            content = (
                f"截至 {recon_date}，盘后账实对账已连续 {streak} 个交易日未能完成。\n"
                f"最近一次原因：{message}\n"
                f"账实一致性无法确认，请尽快人工核查券商连接/数据源。"
            )
            logger.error(f"[对账升级] {content}")
            from notify.email_notify import send_email
            send_email(title, content)
        except Exception as e:
            logger.error(f"[对账升级] 邮件升级告警失败: {e}")

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

            # FIX P1-15: completed 由全部子项决定——持仓+资金均拿到权威数据
            # 且资金对账子项 ok，才算完成；消除“completed=True 与资金对账失败
            # message 并存”的自相矛盾。
            completed = bool(authoritative and asset_res.get("ok"))
            if completed:
                # 记录成功对账的交易日，用于补跑与连续未对账升级
                self._mark_reconciled(recon_date)
                if not result["message"]:
                    result["message"] = (
                        f"对账完成：持仓差异{len(pos_diffs)}项 资金差异{len(asset_diffs)}项 "
                        f"成交异常{len(order_diffs)}项 新增告警{new_diffs}项"
                        if all_diffs else "对账完成：账实一致")
            elif not result["message"]:
                result["message"] = "对账未完成：资金对账子项失败"

            result.update({
                "completed": completed,
                "has_diff": len(all_diffs) > 0,
                "positions": pos_diffs,
                "asset": asset_res,
                "orders": order_rows,
                "diffs": all_diffs,
                "new_diffs": new_diffs,
            })
            # FIX P1-15: 未完成时检查连续失败天数并升级告警；
            # 同时返回待补跑的未对账交易日列表。
            if not completed:
                streak = self._consecutive_failures(recon_date)
                result["fail_streak"] = streak
                esc_days = int(self.config.get("escalate_after_days", 3))
                if streak >= esc_days:
                    self._escalate_alert(recon_date, streak, result["message"])
            pending = self._pending_reconcile_days(recon_date)
            if pending:
                result["pending_days"] = pending
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
