# -*- coding: utf-8 -*-
"""
模拟盘持仓/净值跟踪（tracker）
==============================
逐日记录每个 paper 策略的总市值、净值、仓位、现金与当日成交，写入现有
SQLite（trading_system/data/paper_trading.db），表名 paper_pnl / paper_trades，
不新建散落文件。以策略名做命名空间隔离，支持多策略并行。

安全约束：全部方法 try/except 静默降级，绝不抛异常影响调度主流程。
"""

import os
import sqlite3
import logging
import datetime

logger = logging.getLogger(__name__)


def _default_db_path() -> str:
    try:
        import config
        if hasattr(config, "DATA_DIR"):
            return os.path.join(config.DATA_DIR, "paper_trading.db")
    except Exception:
        pass
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "data", "paper_trading.db")


class PaperTracker:
    """模拟盘每日净值与成交流水跟踪（SQLite）。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or _default_db_path()
        self._init_schema()

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
                CREATE TABLE IF NOT EXISTS paper_pnl (
                    strategy TEXT NOT NULL,
                    date TEXT NOT NULL,
                    total_value REAL DEFAULT 0,
                    nav REAL DEFAULT 0,
                    cash REAL DEFAULT 0,
                    position_value REAL DEFAULT 0,
                    position_ratio REAL DEFAULT 0,
                    trades_count INTEGER DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (strategy, date)
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    date TEXT NOT NULL,
                    code TEXT,
                    action TEXT,
                    shares INTEGER DEFAULT 0,
                    price REAL DEFAULT 0,
                    cost REAL DEFAULT 0,
                    commission REAL DEFAULT 0,
                    stamp_tax REAL DEFAULT 0,
                    reason TEXT
                )""")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[模拟盘台账] 建表失败(降级): {e}")

    # ---- 写入 ----
    def record_daily(self, strategy, date, total_value, nav, cash,
                     position_value, position_ratio, trades_count=0) -> bool:
        """记录/更新某策略某交易日的净值快照（按 (strategy,date) UPSERT）。"""
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO paper_pnl (strategy, date, total_value, nav, cash,
                       position_value, position_ratio, trades_count, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(strategy, date) DO UPDATE SET
                       total_value=excluded.total_value, nav=excluded.nav,
                       cash=excluded.cash, position_value=excluded.position_value,
                       position_ratio=excluded.position_ratio,
                       trades_count=excluded.trades_count,
                       updated_at=excluded.updated_at""",
                (strategy, date, float(total_value), float(nav), float(cash),
                 float(position_value), float(position_ratio), int(trades_count), now))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning(f"[模拟盘台账] 净值记录失败 {strategy} {date}: {e}")
            return False

    def record_trade(self, strategy, date, code, action, shares, price,
                     cost=0.0, commission=0.0, stamp_tax=0.0, reason="") -> bool:
        """记录一笔模拟成交。"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO paper_trades (strategy, date, code, action, shares,
                       price, cost, commission, stamp_tax, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (strategy, date, code, action, int(shares), float(price),
                 float(cost), float(commission), float(stamp_tax), reason))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning(f"[模拟盘台账] 成交记录失败 {strategy} {date} {code}: {e}")
            return False

    def clear(self, strategy) -> None:
        """清空某策略的模拟盘记录（重置实例时用）。"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM paper_pnl WHERE strategy=?", (strategy,))
            cur.execute("DELETE FROM paper_trades WHERE strategy=?", (strategy,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[模拟盘台账] 清空失败 {strategy}: {e}")

    # ---- 读取 ----
    def get_daily_rows(self, strategy) -> list:
        """按日期升序返回净值快照列表。"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM paper_pnl WHERE strategy=? ORDER BY date",
                        (strategy,))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[模拟盘台账] 读取净值失败 {strategy}: {e}")
            return []

    def get_nav_series(self, strategy):
        """返回净值 pandas.Series（index=date, value=nav），无数据返回空 Series。"""
        import pandas as pd
        rows = self.get_daily_rows(strategy)
        if not rows:
            return pd.Series(dtype=float)
        return pd.Series({r["date"]: r["nav"] for r in rows}, dtype=float)

    def get_total_value_series(self, strategy):
        """返回总市值 pandas.Series（index=date）。"""
        import pandas as pd
        rows = self.get_daily_rows(strategy)
        if not rows:
            return pd.Series(dtype=float)
        return pd.Series({r["date"]: r["total_value"] for r in rows}, dtype=float)

    def get_trades(self, strategy, date=None) -> list:
        """返回成交流水（可按日期过滤），按 id 升序。"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            if date:
                cur.execute("SELECT * FROM paper_trades WHERE strategy=? AND date=? "
                            "ORDER BY id", (strategy, date))
            else:
                cur.execute("SELECT * FROM paper_trades WHERE strategy=? ORDER BY id",
                            (strategy,))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[模拟盘台账] 读取成交失败 {strategy}: {e}")
            return []

    def last_date(self, strategy):
        """返回已记录的最新交易日（无则 None），用于增量推进断点续跑。"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("SELECT MAX(date) AS d FROM paper_pnl WHERE strategy=?",
                        (strategy,))
            row = cur.fetchone()
            conn.close()
            return row["d"] if row else None
        except Exception as e:
            logger.warning(f"[模拟盘台账] 读取最新日期失败 {strategy}: {e}")
            return None

    def recorded_dates(self, strategy) -> set:
        """返回已记录的交易日集合（幂等去重用）。"""
        return {r["date"] for r in self.get_daily_rows(strategy)}

    def trading_days(self, strategy) -> int:
        """已运行的交易日数。"""
        return len(self.get_daily_rows(strategy))

    def list_strategies(self) -> list:
        """列出所有有净值记录的策略名。"""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT strategy FROM paper_pnl ORDER BY strategy")
            rows = [r["strategy"] for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[模拟盘台账] 列出策略失败: {e}")
            return []
