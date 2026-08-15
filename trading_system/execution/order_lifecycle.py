# -*- coding: utf-8 -*-
"""
V4.2: 执行台账生命周期（成交回填 + 条件单对账）
================================================
补齐评审认定的两个P0缺口:
  1. trades_today.json 单日文件次日即被覆盖，历史成交永久丢失，
     且 trade_journal.db 的 trades 表恒为空 → M2成交率/执行归因口径失真。
     本模块每日盘后将当日已成委托回填入库（幂等去重）。
  2. 条件单只有"生成"没有"触发/未触发"终态 → G4/G6闭环缺最后一环。
     本模块盘后将当日 orders_YYYYMMDD.json 与真实成交比对，
     回写 status: 已成交 / 未触发，供报告展示与归因消费。

安全约束:
  - 全部函数异常静默降级，返回 {"ok": False, ...}，绝不阻断调度主流程
  - 只做追加/回写，不删除不修改已有成交记录
  - 去重键: (trade_date, code, action, shares, price)，重复回填幂等
"""

import json
import os
import sqlite3
import logging
import datetime

logger = logging.getLogger(__name__)

_DIRECTION_MAP = {"买入": "buy", "卖出": "sell"}


def _project_root() -> str:
    try:
        import config
        return config.PROJECT_ROOT
    except Exception:
        # config不可用时按目录结构推导（本文件位于 trading_system/execution/）
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_trades_today() -> str:
    """trades_today.json 可能在项目根或 trading_system/ 下，两处都找"""
    pr = _project_root()
    for cand in (os.path.join(os.path.dirname(pr), "trades_today.json"),
                 os.path.join(pr, "trades_today.json")):
        if os.path.exists(cand):
            return cand
    return ""


def backfill_trades_to_journal(trades_json_path: str = None,
                               db_path: str = None) -> dict:
    """将 trades_today.json 的已成委托回填 trade_journal.db 的 trades 表

    返回: {"ok": bool, "inserted": int, "skipped": int, "date": str}
    """
    stats = {"ok": False, "inserted": 0, "skipped": 0, "date": ""}
    try:
        src = trades_json_path or _find_trades_today()
        if not src or not os.path.exists(src):
            stats["error"] = "trades_today.json不存在"
            return stats

        with open(src, "r", encoding="utf-8") as f:
            payload = json.load(f)
        trade_date = payload.get("date", "")
        stats["date"] = trade_date
        trades = [t for t in payload.get("trades", [])
                  if t.get("status", "已成") == "已成"]
        if not trade_date or not trades:
            stats["ok"] = True  # 无成交也是正常状态
            return stats

        db = db_path or os.path.join(_project_root(), "data", "trade_journal.db")
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        # 表结构已存在(trades表随db预建)；缺失时兜底建表保证回填可用
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL, trade_time TEXT,
                code TEXT NOT NULL, name TEXT, action TEXT NOT NULL,
                shares INTEGER, price REAL, amount REAL,
                commission REAL DEFAULT 0, sector TEXT, strategy TEXT,
                reason TEXT, pnl REAL DEFAULT 0, pnl_pct REAL DEFAULT 0,
                hold_days INTEGER DEFAULT 0, note TEXT
            )""")

        for t in trades:
            action = _DIRECTION_MAP.get(t.get("direction", ""), "")
            if not action:
                stats["skipped"] += 1
                continue
            code = t.get("code", "")
            shares = int(t.get("qty", 0) or 0)
            price = float(t.get("price", 0) or 0)
            # 幂等去重: 同日期/代码/方向/数量/价格视为同一笔
            cur.execute(
                "SELECT 1 FROM trades WHERE trade_date=? AND code=? AND action=? "
                "AND shares=? AND price=? LIMIT 1",
                (trade_date, code, action, shares, price))
            if cur.fetchone():
                stats["skipped"] += 1
                continue
            cur.execute(
                "INSERT INTO trades (trade_date, trade_time, code, name, action, "
                "shares, price, amount, strategy, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (trade_date, t.get("time", ""), code, t.get("name", ""), action,
                 shares, price, float(t.get("amount", 0) or 0),
                 "trades_today_backfill", "V4.2每日盘后自动回填"))
            stats["inserted"] += 1

        conn.commit()
        conn.close()
        stats["ok"] = True
        logger.info(f"[执行台账] 成交回填 {trade_date}: "
                    f"新增{stats['inserted']}笔 跳过{stats['skipped']}笔")
    except Exception as e:
        stats["error"] = str(e)
        logger.warning(f"[执行台账] 成交回填失败(不影响主流程): {e}")
    return stats


def _load_trades_by_date(trade_date: str, db_path: str = None) -> list:
    """从 trade_journal.db 读取指定日期成交 [{code, action}, ...]"""
    rows = []
    try:
        db = db_path or os.path.join(_project_root(), "data", "trade_journal.db")
        if os.path.exists(db):
            conn = sqlite3.connect(db)
            cur = conn.cursor()
            cur.execute("SELECT code, action FROM trades WHERE trade_date=?",
                        (trade_date,))
            rows = [{"code": r[0], "action": r[1]} for r in cur.fetchall()]
            conn.close()
    except Exception:
        pass
    return rows


def reconcile_daily_orders(date_str: str = None, orders_dir: str = None,
                           db_path: str = None) -> dict:
    """对账当日条件单: 与真实成交比对后回写 status 字段

    参数:
        date_str: 被对账的交易日 YYYYMMDD（None=今天）；
                  对账的是该交易日生效的 orders_{date_str}.json
    返回: {"ok": bool, "total": int, "matched": int, "unmatched": int}
    """
    stats = {"ok": False, "total": 0, "matched": 0, "unmatched": 0}
    try:
        if not date_str:
            date_str = datetime.date.today().strftime("%Y%m%d")
        iso_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

        # orders文件可能在根目录output/或trading_system/output/
        pr = _project_root()
        cand_dirs = [orders_dir] if orders_dir else [
            os.path.join(pr, "output"),
            os.path.join(os.path.dirname(pr), "output"),
        ]
        orders_file = ""
        for d in cand_dirs:
            p = os.path.join(d, f"orders_{date_str}.json")
            if os.path.exists(p):
                orders_file = p
                break
        if not orders_file:
            stats["error"] = f"orders_{date_str}.json不存在"
            return stats

        with open(orders_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        orders = payload.get("orders", [])
        if not orders:
            stats["ok"] = True
            return stats

        # 先回填当日成交再对账，保证双源齐全
        backfill_trades_to_journal(db_path=db_path)
        executed = _load_trades_by_date(iso_date, db_path=db_path)
        exec_keys = {(e["code"], e["action"]) for e in executed}

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        for od in orders:
            code = od.get("证券代码") or od.get("code", "")
            action = _DIRECTION_MAP.get(od.get("方向") or od.get("direction", ""), "")
            if action and (code, action) in exec_keys:
                od["status"] = "已成交"
                stats["matched"] += 1
            else:
                od["status"] = "未触发"
                stats["unmatched"] += 1
            od["reconciled_at"] = now_str
        stats["total"] = len(orders)

        with open(orders_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        stats["ok"] = True
        logger.info(f"[执行台账] 条件单对账 {date_str}: "
                    f"共{stats['total']}条 已成交{stats['matched']} 未触发{stats['unmatched']}")
    except Exception as e:
        stats["error"] = str(e)
        logger.warning(f"[执行台账] 条件单对账失败(不影响主流程): {e}")
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    print("成交回填:", backfill_trades_to_journal())
    print("条件单对账:", reconcile_daily_orders())
