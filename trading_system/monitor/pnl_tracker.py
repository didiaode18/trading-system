"""
实盘P&L实时追踪模块（V3.2新增）
================================
独立模块实时计算每只持仓的浮动盈亏/当日盈亏/组合总收益

核心功能:
  1. 逐只持仓浮盈/浮亏计算
  2. 当日盈亏（vs昨日收盘）
  3. 组合总收益率/总盈亏金额
  4. 持久化到SQLite（支持历史查询）
  5. 供盘中决策报告和风控模块调用

使用方式:
    from monitor.pnl_tracker import PnLTracker
    tracker = PnLTracker()
    snapshot = tracker.calc_portfolio_pnl(holdings, realtime_prices)
    tracker.save_snapshot(snapshot)
"""

import os
import sys
import json
import sqlite3
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# P&L数据库路径
PNL_DB_PATH = os.path.join(config.DATA_DIR, "pnl_history.db")


class PnLTracker:
    """实盘P&L追踪器"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or PNL_DB_PATH
        self._init_db()

    def _init_db(self):
        """初始化P&L历史表"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pnl_snapshot (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    date        TEXT NOT NULL,
                    time        TEXT NOT NULL,
                    total_value REAL,
                    total_cost  REAL,
                    total_pnl   REAL,
                    pnl_pct     REAL,
                    day_pnl     REAL,
                    holdings_count INTEGER,
                    detail_json TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[PnL] 数据库初始化失败: {e}")

    def calc_portfolio_pnl(self, holdings: dict,
                           realtime_prices: dict = None) -> dict:
        """
        计算组合P&L快照
        
        参数:
            holdings: {code: {shares, buy_price/cost, price, name, ...}}
            realtime_prices: {code: current_price}（可选，覆盖holdings中的price）
        
        返回:
            {
                "date": str,
                "time": str,
                "total_value": float,      # 总市值
                "total_cost": float,       # 总成本
                "total_pnl": float,        # 总浮盈/亏（元）
                "pnl_pct": float,          # 总收益率
                "day_pnl": float,          # 当日盈亏（元）
                "holdings_count": int,
                "positions": [{code, name, shares, cost, price, pnl, pnl_pct, weight}],
            }
        """
        now = datetime.datetime.now()
        positions = []
        total_value = 0
        total_cost = 0
        day_pnl = 0

        for code, pos in holdings.items():
            shares = pos.get("shares", 0)
            cost = pos.get("cost", pos.get("buy_price", 0))
            # 优先使用实时价格
            price = (realtime_prices or {}).get(code, pos.get("price", cost))
            prev_close = pos.get("prev_close", pos.get("yesterday_close", price))
            name = pos.get("name", config.get_stock_name(code))

            if shares <= 0 or cost <= 0:
                continue

            market_value = shares * price
            cost_value = shares * cost
            pnl = market_value - cost_value
            pnl_pct = (price - cost) / cost if cost > 0 else 0
            # 当日盈亏
            day_change = (price - prev_close) * shares if prev_close > 0 else 0

            total_value += market_value
            total_cost += cost_value
            day_pnl += day_change

            positions.append({
                "code": code,
                "name": name,
                "shares": shares,
                "cost": round(cost, 3),
                "price": round(price, 3),
                "market_value": round(market_value, 0),
                "pnl": round(pnl, 0),
                "pnl_pct": round(pnl_pct, 4),
                "day_pnl": round(day_change, 0),
                "weight": 0,  # 后面计算
            })

        # 计算权重
        if total_value > 0:
            for p in positions:
                p["weight"] = round(p["market_value"] / total_value, 4)

        # 按浮盈排序（最弱的在前）
        positions.sort(key=lambda x: x["pnl_pct"])

        total_pnl = total_value - total_cost
        pnl_pct = total_pnl / total_cost if total_cost > 0 else 0

        return {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "total_value": round(total_value, 0),
            "total_cost": round(total_cost, 0),
            "total_pnl": round(total_pnl, 0),
            "pnl_pct": round(pnl_pct, 4),
            "day_pnl": round(day_pnl, 0),
            "holdings_count": len(positions),
            "positions": positions,
        }

    def save_snapshot(self, snapshot: dict):
        """持久化P&L快照到SQLite"""
        try:
            conn = sqlite3.connect(self.db_path)
            detail = json.dumps(snapshot.get("positions", []),
                               ensure_ascii=False)
            conn.execute("""
                INSERT INTO pnl_snapshot
                (date, time, total_value, total_cost, total_pnl,
                 pnl_pct, day_pnl, holdings_count, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot["date"], snapshot["time"],
                snapshot["total_value"], snapshot["total_cost"],
                snapshot["total_pnl"], snapshot["pnl_pct"],
                snapshot["day_pnl"], snapshot["holdings_count"],
                detail,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[PnL] 快照保存失败: {e}")

    def get_history(self, days: int = 30) -> list:
        """获取最近N天的P&L历史"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("""
                SELECT date, time, total_value, total_pnl, pnl_pct, day_pnl
                FROM pnl_snapshot
                ORDER BY date DESC, time DESC
                LIMIT ?
            """, (days * 16,))  # 每天最多16条（15分钟一次）
            rows = cursor.fetchall()
            conn.close()
            return [
                {"date": r[0], "time": r[1], "total_value": r[2],
                 "total_pnl": r[3], "pnl_pct": r[4], "day_pnl": r[5]}
                for r in rows
            ]
        except Exception:
            return []

    def get_today_summary(self) -> dict:
        """获取今日P&L摘要（供报告使用）"""
        today = datetime.date.today().isoformat()
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("""
                SELECT total_value, total_pnl, pnl_pct, day_pnl, holdings_count
                FROM pnl_snapshot
                WHERE date = ?
                ORDER BY time DESC LIMIT 1
            """, (today,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "total_value": row[0], "total_pnl": row[1],
                    "pnl_pct": row[2], "day_pnl": row[3],
                    "holdings_count": row[4],
                }
        except Exception:
            pass
        return {}
