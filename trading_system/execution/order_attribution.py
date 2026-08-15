# -*- coding: utf-8 -*-
"""
V4.1(M2): 条件单执行归因
========================
统计近N日生成的条件单(orders_*.json)与实际成交(trade_journal.db)的匹配情况，
回答"条件单体系是否真实执行到位"——G4/G6闭环的最后一块拼图。

口径:
    - 已成交: 条件单日期当天，成交记录中存在同标的、同方向的成交
    - 成交数据双源: trades_today.json（真实成交，带date字段可跨天）
      + trade_journal.db的trades表（若已回填）
    - 未成交: 无匹配成交（触发价未达到/未挂单/撤单）
    - ★★★必挂未成交单单独计数（多为风控强制减仓单，漏挂风险高）

安全约束: 全部异常静默降级，返回空统计/空字符串，绝不阻断报告。
"""

import os
import json
import glob
import sqlite3
import datetime
import logging

logger = logging.getLogger(__name__)

_DIRECTION_MAP = {"卖出": "sell", "买入": "buy"}


def _find_orders_dirs() -> list:
    """条件单JSON可能落在根目录output/或trading_system/output/，两处都扫"""
    dirs = []
    try:
        import config
        pr = config.PROJECT_ROOT
        for cand in (os.path.join(pr, "output"),
                     os.path.join(os.path.dirname(pr), "output"),
                     os.path.join(os.path.dirname(pr), "trading_system", "output")):
            if os.path.isdir(cand) and cand not in dirs:
                dirs.append(cand)
    except Exception:
        pass
    return dirs


def _load_trades_between(start: str, end: str) -> list:
    """读取成交记录（双源: trades_today.json + trade_journal.db）

    返回 [{date, code, action}, ...]，action为buy/sell
    """
    rows = []
    # 源1: trades_today.json（真实成交落盘，带date字段）
    try:
        import config
        pr = config.PROJECT_ROOT
        for cand in (os.path.join(os.path.dirname(pr), "trades_today.json"),
                     os.path.join(pr, "trades_today.json")):
            if os.path.exists(cand):
                with open(cand, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                d_str = payload.get("date", "")
                if start <= d_str <= end:
                    for t in payload.get("trades", []):
                        action = _DIRECTION_MAP.get(t.get("direction", ""))
                        if action and t.get("status", "").startswith("已成"):
                            rows.append({"date": d_str, "code": t.get("code", ""),
                                         "action": action})
                break
    except Exception as e:
        logger.warning(f"[条件单归因] trades_today.json读取失败: {e}")
    # 源2: trade_journal.db（若已回填历史成交）
    try:
        import config
        db_path = os.path.join(config.PROJECT_ROOT, "data", "trade_journal.db")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT trade_date, code, action FROM trades
                WHERE trade_date >= ? AND trade_date <= ?
            """, (start, end))
            rows.extend({"date": r[0], "code": r[1], "action": r[2]}
                        for r in cur.fetchall())
            conn.close()
    except Exception as e:
        logger.warning(f"[条件单归因] 交易日志读取失败: {e}")
    return rows


def collect_order_execution_stats(lookback_days: int = 7) -> dict:
    """
    统计近N日条件单执行情况

    返回:
        {"batches": int, "total_orders": int, "filled": int, "unfilled": int,
         "fill_rate": float(小数),
         "by_direction": {"卖出": {"total": n, "filled": n, "rate": float}, ...},
         "unfilled_by_type": {"定价卖出": n, ...},
         "unfilled_high_priority": int,
         "date_range": [start, end],
         "available": bool}
    """
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=int(lookback_days))).strftime("%Y%m%d")
    start_iso = (today - datetime.timedelta(days=int(lookback_days))).isoformat()
    end_iso = today.isoformat()

    stats = {"batches": 0, "total_orders": 0, "filled": 0, "unfilled": 0,
             "fill_rate": None, "by_direction": {}, "unfilled_by_type": {},
             "unfilled_high_priority": 0, "date_range": [start_iso, end_iso],
             "available": False}
    try:
        # 收集条件单
        orders = []  # [{date, code, name, direction, type, priority}, ...]
        for d in _find_orders_dirs():
            for fp in glob.glob(os.path.join(d, "orders_*.json")):
                fname = os.path.basename(fp)
                ds = fname.replace("orders_", "").replace(".json", "")
                if not (ds.isdigit() and len(ds) == 8 and ds >= start):
                    continue
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                    for o in payload.get("orders", []):
                        orders.append({
                            "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}",
                            "code": o.get("证券代码") or o.get("code") or "",
                            "name": o.get("证券名称") or o.get("name") or "",
                            "direction": o.get("方向") or o.get("direction") or "",
                            "type": o.get("类型") or o.get("type") or "",
                            "priority": o.get("优先级") or "",
                        })
                except Exception:
                    continue
        if not orders:
            return stats
        stats["batches"] = len({o["date"] for o in orders})
        stats["total_orders"] = len(orders)

        # 成交索引: (date, code, action) -> True
        trades = _load_trades_between(start_iso, end_iso)
        filled_keys = {(t["date"], t["code"], t["action"]) for t in trades}

        for o in orders:
            action = _DIRECTION_MAP.get(o["direction"])
            is_filled = action is not None and (o["date"], o["code"], action) in filled_keys
            d_stat = stats["by_direction"].setdefault(
                o["direction"] or "未知", {"total": 0, "filled": 0, "rate": None})
            d_stat["total"] += 1
            if is_filled:
                stats["filled"] += 1
                d_stat["filled"] += 1
            else:
                stats["unfilled"] += 1
                stats["unfilled_by_type"][o["type"] or "未知"] = \
                    stats["unfilled_by_type"].get(o["type"] or "未知", 0) + 1
                if "★★★" in o["priority"]:
                    stats["unfilled_high_priority"] += 1

        stats["fill_rate"] = round(stats["filled"] / stats["total_orders"], 4)
        for d_stat in stats["by_direction"].values():
            if d_stat["total"] > 0:
                d_stat["rate"] = round(d_stat["filled"] / d_stat["total"], 4)
        stats["available"] = True
        return stats
    except Exception as e:
        logger.warning(f"[条件单归因] 统计异常(不影响报告): {e}")
        return stats


def render_order_attribution_html(stats: dict, lookback_days: int = 7) -> str:
    """生成HTML区块；数据不可用时返回空字符串"""
    try:
        if not isinstance(stats, dict) or not stats.get("available"):
            return ""
        rate = stats["fill_rate"] * 100
        if rate >= 80:
            bg, border, color = "#f6ffed", "#b7eb8f", "#389e0d"
        elif rate >= 50:
            bg, border, color = "#fff7e6", "#ffd591", "#d46b08"
        else:
            bg, border, color = "#fff1f0", "#ffa39e", "#cf1322"
        lines = (f"<div style='font-size:13px;margin-top:6px'>"
                 f"近{lookback_days}日: {stats['batches']}批条件单共{stats['total_orders']}条 | "
                 f"成交{stats['filled']}条 | 未成交{stats['unfilled']}条 | "
                 f"成交率<b>{rate:.0f}%</b></div>")
        d_parts = []
        for d_name, d_stat in stats["by_direction"].items():
            if d_stat.get("rate") is not None:
                d_parts.append(f"{d_name} {d_stat['filled']}/{d_stat['total']}"
                               f"({d_stat['rate']*100:.0f}%)")
        if d_parts:
            lines += (f"<div style='font-size:12px;color:#595959;margin-top:4px'>"
                      f"分方向: {' | '.join(d_parts)}</div>")
        warn = ""
        if stats["unfilled_high_priority"] > 0:
            warn = (f"<div style='font-size:12px;color:#cf1322;margin-top:4px'>"
                    f"⚠️ {stats['unfilled_high_priority']}条★★★必挂单未成交"
                    f"（多为风控强制减仓单），请检查是否漏挂或被撤单</div>")
        elif stats["unfilled"] > 0:
            top = sorted(stats["unfilled_by_type"].items(), key=lambda x: -x[1])[:3]
            warn = (f"<div style='font-size:12px;color:#8c8c8c;margin-top:4px'>"
                    f"未成交构成: {'、'.join(f'{k}×{v}' for k, v in top)}"
                    f"（触发价未达到/未挂单，属正常情况居多）</div>")
        return (f"<div style='max-width:1000px;margin:10px auto;background:{bg};"
                f"border:1px solid {border};border-radius:8px;padding:12px 18px'>"
                f"<div style='font-weight:bold;color:{color};font-size:14px'>"
                f"📋 条件单执行归因（近{lookback_days}日）</div>{lines}{warn}"
                f"<div style='font-size:11px;color:#8c8c8c;margin-top:4px'>"
                f"口径: 条件单当日存在同标的同方向真实成交(trades_today/交易日志)即计为已成交；"
                f"若成交未录入系统则会被计为未成交；本区块只展示不干预。</div></div>")
    except Exception:
        return ""
