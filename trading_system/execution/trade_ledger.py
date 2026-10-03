# -*- coding: utf-8 -*-
"""
成交流水台账（trade_ledger）
============================
一切持仓与盈亏以券商真实成交流水为准，本地估算仅作参考。本模块用 SQLite
持久化"委托 → 部分成交 → 全部成交/撤单/拒单"的全过程，作为账实对账的唯一
权威本地镜像。

三张表:
  orders            当日及历史委托（含成交量/均价/状态回填）
  trades            逐笔成交回报（按 traded_id 去重，防止回调与查询重复入账）
  reconcile_ledger  盘后对账差异台账（同日同类型同标的去重，供告警去重）

安全约束:
  - 全部方法 try/except 静默降级，绝不抛异常影响交易主流程
  - 只追加/回填，不删除历史成交
  - 无 xtquant 也可用：所有入参均为纯 dict / 标量，便于模拟模式构造回报测试
"""

import os
import sqlite3
import logging
import datetime
import hashlib

logger = logging.getLogger(__name__)

# ---- 委托状态常量（canonical）----
STATUS_PENDING = "待成交"
STATUS_SUBMITTED = "已报"
STATUS_PARTIAL = "部分成交"
STATUS_FILLED = "已成交"
STATUS_CANCELLED = "已撤"
STATUS_REJECTED = "拒单"

# xtquant order_status 数值 → 中文原始态
XT_ORDER_STATUS = {
    48: "未报", 49: "待报", 50: "已报", 51: "已报待撤",
    52: "部成待撤", 53: "部撤", 54: "已撤", 55: "部成",
    56: "已成", 57: "废单",
}

# xtquant 原始态 → canonical 态
_STATUS_CANON = {
    "未报": STATUS_SUBMITTED, "待报": STATUS_SUBMITTED, "已报": STATUS_SUBMITTED,
    "已报待撤": STATUS_CANCELLED, "部成待撤": STATUS_PARTIAL, "部撤": STATUS_CANCELLED,
    "已撤": STATUS_CANCELLED, "部成": STATUS_PARTIAL, "已成": STATUS_FILLED,
    "废单": STATUS_REJECTED,
}

# xtquant 方向常量
XT_STOCK_BUY = 23
XT_STOCK_SELL = 24


def canonical_status(raw) -> str:
    """把 xtquant 数值状态码 / 中文原始态 归一为 canonical 状态。

    无法识别时原样返回字符串（不臆造"已成交"）。
    """
    try:
        if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
            name = XT_ORDER_STATUS.get(int(raw))
            if name:
                return _STATUS_CANON.get(name, name)
        if isinstance(raw, str):
            if raw in _STATUS_CANON:
                return _STATUS_CANON[raw]
            # 已是 canonical
            if raw in (STATUS_PENDING, STATUS_SUBMITTED, STATUS_PARTIAL,
                       STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED):
                return raw
    except Exception:
        pass
    return str(raw) if raw is not None else ""


def canonical_direction(raw) -> str:
    """把 xtquant 方向数值 / 英文 / 中文 归一为 买入/卖出。"""
    try:
        if raw in (XT_STOCK_BUY, "23", 23):
            return "买入"
        if raw in (XT_STOCK_SELL, "24", 24):
            return "卖出"
        s = str(raw).lower()
        if s in ("buy", "b", "买入"):
            return "买入"
        if s in ("sell", "s", "卖出"):
            return "卖出"
    except Exception:
        pass
    return str(raw) if raw is not None else ""


def _project_root() -> str:
    try:
        import config
        return config.PROJECT_ROOT
    except Exception:
        # 本文件位于 trading_system/execution/
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_db_path() -> str:
    try:
        import config
        if hasattr(config, "DATA_DIR"):
            return os.path.join(config.DATA_DIR, "trade_reconcile.db")
    except Exception:
        pass
    return os.path.join(_project_root(), "data", "trade_reconcile.db")


class TradeLedger:
    """成交流水台账（SQLite 持久化）。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or _default_db_path()
        self._init_schema()

    # ---- 内部：连接与建表 ----
    def _conn(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    trade_date TEXT,
                    code TEXT,
                    name TEXT,
                    direction TEXT,
                    order_price REAL DEFAULT 0,
                    order_qty INTEGER DEFAULT 0,
                    filled_qty INTEGER DEFAULT 0,
                    avg_price REAL DEFAULT 0,
                    status TEXT,
                    status_msg TEXT,
                    source TEXT,
                    updated_at TEXT
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    traded_id TEXT,
                    order_id TEXT,
                    trade_date TEXT,
                    code TEXT,
                    direction TEXT,
                    traded_price REAL DEFAULT 0,
                    traded_volume INTEGER DEFAULT 0,
                    traded_time TEXT,
                    UNIQUE(traded_id, order_id)
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reconcile_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recon_date TEXT,
                    recon_type TEXT,
                    code TEXT,
                    diff_detail TEXT,
                    diff_hash TEXT,
                    handled INTEGER DEFAULT 0,
                    created_at TEXT,
                    UNIQUE(recon_date, recon_type, diff_hash)
                )""")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[台账] 建表失败(降级): {e}")

    # ---- 委托 ----
    def record_order(self, order_id, code, direction, order_price, order_qty,
                     status=STATUS_SUBMITTED, name="", trade_date=None,
                     status_msg="", source="manual") -> bool:
        """登记/更新一笔委托（UPSERT）。order_id 为空时忽略。"""
        if not order_id:
            return False
        trade_date = trade_date or datetime.date.today().isoformat()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO orders (order_id, trade_date, code, name, direction,
                       order_price, order_qty, filled_qty, avg_price, status,
                       status_msg, source, updated_at)
                   VALUES (?,?,?,?,?,?,?,0,0,?,?,?,?)
                   ON CONFLICT(order_id) DO UPDATE SET
                       code=excluded.code, name=excluded.name,
                       direction=excluded.direction,
                       order_price=excluded.order_price,
                       order_qty=excluded.order_qty,
                       status=excluded.status,
                       status_msg=excluded.status_msg,
                       source=excluded.source,
                       updated_at=excluded.updated_at""",
                (str(order_id), trade_date, code, name,
                 canonical_direction(direction), float(order_price or 0),
                 int(order_qty or 0), canonical_status(status), status_msg,
                 source, now))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning(f"[台账] 委托登记失败 order={order_id}: {e}")
            return False

    def update_order_status(self, order_id, status, status_msg="",
                            filled_qty=None, avg_price=None) -> bool:
        """回填委托状态（不覆盖已有成交量/均价，除非显式传入）。"""
        if not order_id:
            return False
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn = self._conn()
            cur = conn.cursor()
            sets = ["status=?", "updated_at=?"]
            params = [canonical_status(status), now]
            if status_msg:
                sets.append("status_msg=?")
                params.append(status_msg)
            if filled_qty is not None:
                sets.append("filled_qty=?")
                params.append(int(filled_qty))
            if avg_price is not None:
                sets.append("avg_price=?")
                params.append(float(avg_price))
            params.append(str(order_id))
            cur.execute(f"UPDATE orders SET {', '.join(sets)} WHERE order_id=?",
                        params)
            if cur.rowcount == 0:
                # 委托尚未入账（如仅收到下单失败/撤单回报）：补建最小行，
                # 保证拒单等终态可留痕，不产生"已执行"假象。
                cur.execute(
                    """INSERT OR IGNORE INTO orders
                           (order_id, trade_date, code, name, direction,
                            order_price, order_qty, filled_qty, avg_price,
                            status, status_msg, source, updated_at)
                       VALUES (?,?,?,?,?,?,?,0,0,?,?,?,?)""",
                    (str(order_id),
                     datetime.date.today().isoformat(), "", "", "", 0.0, 0,
                     canonical_status(status), status_msg, "callback", now))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning(f"[台账] 状态回填失败 order={order_id}: {e}")
            return False

    def get_order(self, order_id) -> dict:
        """读取单笔委托，返回 dict（不存在返回 {}）。"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE order_id=?", (str(order_id),))
            row = cur.fetchone()
            conn.close()
            return dict(row) if row else {}
        except Exception as e:
            logger.warning(f"[台账] 读取委托失败 order={order_id}: {e}")
            return {}

    def get_day_orders(self, trade_date=None) -> list:
        """读取指定交易日全部委托（默认今日）。"""
        trade_date = trade_date or datetime.date.today().isoformat()
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE trade_date=? ORDER BY updated_at",
                        (trade_date,))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[台账] 读取当日委托失败: {e}")
            return []

    # ---- 成交 ----
    def record_trade(self, traded_id, order_id, code, direction, traded_price,
                     traded_volume, traded_time="", trade_date=None) -> bool:
        """登记逐笔成交回报，按 (traded_id, order_id) 去重。

        返回 True=新入账, False=重复或失败（重复视为幂等成功但不重复计数）。
        """
        if order_id is None or order_id == "":
            return False
        trade_date = trade_date or datetime.date.today().isoformat()
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT OR IGNORE INTO trades (traded_id, order_id, trade_date,
                       code, direction, traded_price, traded_volume, traded_time)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (str(traded_id), str(order_id), trade_date, code,
                 canonical_direction(direction), float(traded_price or 0),
                 int(traded_volume or 0), traded_time))
            inserted = cur.rowcount > 0
            conn.commit()
            conn.close()
            if inserted:
                # 成交入账后重算该委托的成交量与均价
                self._refresh_order_fill(order_id)
            return inserted
        except Exception as e:
            logger.warning(f"[台账] 成交入账失败 order={order_id}: {e}")
            return False

    def _refresh_order_fill(self, order_id):
        """按 trades 表重算委托的 filled_qty 与 avg_price = Σ(价×量)/Σ量。"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT SUM(traded_price*traded_volume) AS amt, "
                "SUM(traded_volume) AS vol FROM trades WHERE order_id=?",
                (str(order_id),))
            row = cur.fetchone()
            total_amt = float(row["amt"] or 0)
            total_vol = int(row["vol"] or 0)
            avg = (total_amt / total_vol) if total_vol > 0 else 0.0
            # 依据委托量推断状态（仅在非终态时升级，撤单/拒单由状态回填决定）
            cur.execute("SELECT order_qty, status FROM orders WHERE order_id=?",
                        (str(order_id),))
            orow = cur.fetchone()
            new_status = None
            if orow:
                oqty = int(orow["order_qty"] or 0)
                cur_status = orow["status"]
                if cur_status not in (STATUS_CANCELLED, STATUS_REJECTED):
                    if total_vol > 0 and oqty > 0 and total_vol >= oqty:
                        new_status = STATUS_FILLED
                    elif total_vol > 0:
                        new_status = STATUS_PARTIAL
            cur.execute("UPDATE orders SET filled_qty=?, avg_price=?, updated_at=? "
                        "WHERE order_id=?",
                        (total_vol, avg,
                         datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         str(order_id)))
            if new_status:
                cur.execute("UPDATE orders SET status=? WHERE order_id=?",
                            (new_status, str(order_id)))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[台账] 成交量重算失败 order={order_id}: {e}")

    def get_order_trades(self, order_id) -> list:
        """读取某委托的全部逐笔成交。"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades WHERE order_id=? ORDER BY id",
                        (str(order_id),))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[台账] 读取成交失败 order={order_id}: {e}")
            return []

    def get_day_trades(self, trade_date=None) -> list:
        """读取指定交易日全部成交。"""
        trade_date = trade_date or datetime.date.today().isoformat()
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades WHERE trade_date=? ORDER BY id",
                        (trade_date,))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[台账] 读取当日成交失败: {e}")
            return []

    # ---- 对账台账（差异去重）----
    def record_diff(self, recon_type, code, diff_detail, recon_date=None) -> bool:
        """记录一条对账差异，同日同类型同内容去重。

        返回 True=新差异（应告警）, False=重复（已告警，跳过）。
        """
        recon_date = recon_date or datetime.date.today().isoformat()
        diff_hash = hashlib.md5(
            f"{code}|{diff_detail}".encode("utf-8")).hexdigest()[:16]
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT OR IGNORE INTO reconcile_ledger
                       (recon_date, recon_type, code, diff_detail, diff_hash,
                        handled, created_at)
                   VALUES (?,?,?,?,?,0,?)""",
                (recon_date, recon_type, code, diff_detail, diff_hash, now))
            inserted = cur.rowcount > 0
            conn.commit()
            conn.close()
            return inserted
        except Exception as e:
            logger.warning(f"[台账] 差异记录失败: {e}")
            return False

    def get_day_diffs(self, recon_date=None) -> list:
        """读取指定日全部对账差异。"""
        recon_date = recon_date or datetime.date.today().isoformat()
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM reconcile_ledger WHERE recon_date=? ORDER BY id",
                        (recon_date,))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[台账] 读取差异失败: {e}")
            return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    _led = TradeLedger()
    print("当日委托:", _led.get_day_orders())
    print("当日成交:", _led.get_day_trades())
    print("当日差异:", _led.get_day_diffs())
