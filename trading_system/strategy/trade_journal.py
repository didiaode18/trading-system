"""
交易日志与绩效归因模块 V1.0
============================
记录每笔交易，追踪策略绩效，回答"我亏在哪、赚在哪"

核心功能:
  1. 交易日志记录（买入/卖出/加仓/减仓，含理由）
  2. 绩效指标计算（胜率、盈亏比、夏普比率、最大回撤）
  3. 基准对比（vs 沪深300，计算Alpha）
  4. 绩效归因（按行业/策略/时间维度拆解盈亏来源）
  5. 资金曲线追踪（每日净值记录）
  6. 周度/月度绩效报告

数据存储: SQLite (trade_journal表) + JSON备份

使用方式:
    from strategy.trade_journal import TradeJournal
    journal = TradeJournal()
    journal.record_buy("002371", "北方华创", 200, 807.5, "趋势突破", "半导体")
    journal.record_sell("002371", "北方华创", 200, 850.0, "止盈")
    report = journal.performance_report()
"""

import os
import sys
import json
import sqlite3
import logging
import datetime
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 数据库路径
JOURNAL_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "trade_journal.db"
)


class TradeJournal:
    """交易日志管理器"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or JOURNAL_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 交易记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                trade_time TEXT,
                code TEXT NOT NULL,
                name TEXT,
                action TEXT NOT NULL,
                shares INTEGER,
                price REAL,
                amount REAL,
                commission REAL DEFAULT 0,
                sector TEXT,
                strategy TEXT,
                reason TEXT,
                pnl REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                hold_days INTEGER DEFAULT 0,
                note TEXT
            )
        """)

        # 每日净值表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_nav (
                date TEXT PRIMARY KEY,
                total_value REAL,
                cash REAL,
                market_value REAL,
                daily_pnl REAL,
                daily_return REAL,
                benchmark_return REAL,
                drawdown REAL
            )
        """)

        conn.commit()
        conn.close()

    # ============================================================
    # 一、交易记录
    # ============================================================

    def record_buy(self, code: str, name: str, shares: int, price: float,
                   reason: str = "", sector: str = "", strategy: str = "trend",
                   note: str = "") -> int:
        """记录买入交易"""
        amount = shares * price
        commission = amount * 0.0015  # 佣金约0.15%

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trades (trade_date, trade_time, code, name, action, shares, price,
                              amount, commission, sector, strategy, reason, note)
            VALUES (?, ?, ?, ?, 'buy', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.date.today().isoformat(),
            datetime.datetime.now().strftime("%H:%M:%S"),
            code, name, shares, price, amount, commission,
            sector, strategy, reason, note
        ))
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"[交易日志] 买入 {code} {name} {shares}股 @ {price:.2f} | {reason}")
        return trade_id

    def record_sell(self, code: str, name: str, shares: int, price: float,
                    reason: str = "", sector: str = "", strategy: str = "trend",
                    buy_price: float = 0, buy_date: str = "", note: str = "") -> int:
        """记录卖出交易（自动计算盈亏）"""
        amount = shares * price
        commission = amount * 0.0015  # 佣金+印花税约0.15%

        # 计算盈亏
        pnl = 0
        pnl_pct = 0
        hold_days = 0
        if buy_price > 0:
            pnl = (price - buy_price) * shares - commission
            pnl_pct = (price / buy_price - 1) * 100
        if buy_date:
            try:
                bd = datetime.date.fromisoformat(buy_date)
                hold_days = (datetime.date.today() - bd).days
            except Exception:
                pass

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trades (trade_date, trade_time, code, name, action, shares, price,
                              amount, commission, sector, strategy, reason, pnl, pnl_pct,
                              hold_days, note)
            VALUES (?, ?, ?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.date.today().isoformat(),
            datetime.datetime.now().strftime("%H:%M:%S"),
            code, name, shares, price, amount, commission,
            sector, strategy, reason, pnl, pnl_pct, hold_days, note
        ))
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"[交易日志] 卖出 {code} {name} {shares}股 @ {price:.2f} | "
                   f"盈亏{pnl:+,.0f}元({pnl_pct:+.1f}%) | {reason}")
        return trade_id

    def record_daily_nav(self, total_value: float, cash: float,
                         benchmark_close: float = None, prev_benchmark: float = None):
        """记录每日净值"""
        market_value = total_value - cash
        today = datetime.date.today().isoformat()

        # 获取前一日数据计算日收益
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT total_value, benchmark_return FROM daily_nav ORDER BY date DESC LIMIT 1")
        prev = cursor.fetchone()

        daily_pnl = 0
        daily_return = 0
        benchmark_return = 0
        if prev and prev[0] > 0:
            daily_pnl = total_value - prev[0]
            daily_return = daily_pnl / prev[0]

        if benchmark_close and prev_benchmark and prev_benchmark > 0:
            benchmark_return = (benchmark_close / prev_benchmark - 1)

        # 计算回撤
        cursor.execute("SELECT MAX(total_value) FROM daily_nav")
        peak = cursor.fetchone()[0]
        peak = max(peak or 0, total_value)
        drawdown = (total_value - peak) / peak if peak > 0 else 0

        cursor.execute("""
            INSERT OR REPLACE INTO daily_nav (date, total_value, cash, market_value,
                                             daily_pnl, daily_return, benchmark_return, drawdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (today, total_value, cash, market_value, daily_pnl, daily_return,
              benchmark_return, drawdown))

        conn.commit()
        conn.close()

    # ============================================================
    # 二、绩效指标计算
    # ============================================================

    def performance_report(self, days: int = 90) -> dict:
        """
        计算核心绩效指标
        
        返回:
            {
                "total_trades": int,
                "win_rate": float,          # 胜率(%)
                "profit_loss_ratio": float, # 盈亏比
                "total_pnl": float,         # 总盈亏
                "avg_profit": float,        # 平均盈利
                "avg_loss": float,          # 平均亏损
                "max_consecutive_loss": int, # 最大连续亏损
                "sharpe_ratio": float,      # 夏普比率(年化)
                "max_drawdown": float,      # 最大回撤
                "alpha": float,             # 超额收益
                "avg_hold_days": float,     # 平均持有天数
                "by_sector": {...},         # 按行业归因
                "by_strategy": {...},       # 按策略归因
            }
        """
        conn = sqlite3.connect(self.db_path)

        # 获取所有卖出交易（有盈亏的）
        sells_df = pd.read_sql_query(
            f"SELECT * FROM trades WHERE action='sell' AND pnl != 0 "
            f"AND trade_date >= date('now', '-{days} days')",
            conn
        )

        # 获取净值曲线
        nav_df = pd.read_sql_query(
            f"SELECT * FROM daily_nav WHERE date >= date('now', '-{days} days') ORDER BY date",
            conn
        )
        conn.close()

        if sells_df.empty:
            return {
                "total_trades": 0, "win_rate": 0, "profit_loss_ratio": 0,
                "total_pnl": 0, "detail": "无交易记录", "by_sector": {}, "by_strategy": {}
            }

        # 基础统计
        total_trades = len(sells_df)
        wins = sells_df[sells_df["pnl"] > 0]
        losses = sells_df[sells_df["pnl"] <= 0]
        win_count = len(wins)
        loss_count = len(losses)

        win_rate = win_count / total_trades * 100 if total_trades > 0 else 0
        total_pnl = sells_df["pnl"].sum()
        avg_profit = wins["pnl"].mean() if not wins.empty else 0
        avg_loss = abs(losses["pnl"].mean()) if not losses.empty else 0
        profit_loss_ratio = avg_profit / avg_loss if avg_loss > 0 else float('inf')

        # 最大连续亏损
        max_consec_loss = self._calc_max_consecutive(sells_df["pnl"].values)

        # 平均持有天数
        avg_hold_days = sells_df["hold_days"].mean() if "hold_days" in sells_df.columns else 0

        # 夏普比率（基于日收益率）
        sharpe = 0
        if not nav_df.empty and len(nav_df) > 5:
            returns = nav_df["daily_return"].dropna().values
            if len(returns) > 1 and np.std(returns) > 0:
                sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)

        # 最大回撤
        max_dd = 0
        if not nav_df.empty:
            max_dd = nav_df["drawdown"].min() if "drawdown" in nav_df.columns else 0

        # Alpha（超额收益）
        alpha = 0
        if not nav_df.empty and "benchmark_return" in nav_df.columns:
            cum_strategy = nav_df["daily_return"].sum()
            cum_benchmark = nav_df["benchmark_return"].sum()
            alpha = cum_strategy - cum_benchmark

        # 按行业归因
        by_sector = {}
        if "sector" in sells_df.columns:
            for sector, group in sells_df.groupby("sector"):
                if sector:
                    by_sector[sector] = {
                        "trades": len(group),
                        "pnl": round(group["pnl"].sum(), 2),
                        "win_rate": round(len(group[group["pnl"] > 0]) / len(group) * 100, 1),
                    }

        # 按策略归因
        by_strategy = {}
        if "strategy" in sells_df.columns:
            for strat, group in sells_df.groupby("strategy"):
                if strat:
                    by_strategy[strat] = {
                        "trades": len(group),
                        "pnl": round(group["pnl"].sum(), 2),
                        "win_rate": round(len(group[group["pnl"] > 0]) / len(group) * 100, 1),
                    }

        return {
            "total_trades": total_trades,
            "win_rate": round(win_rate, 1),
            "profit_loss_ratio": round(profit_loss_ratio, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_profit": round(avg_profit, 2),
            "avg_loss": round(avg_loss, 2),
            "max_consecutive_loss": max_consec_loss,
            "sharpe_ratio": round(sharpe, 3),
            "max_drawdown": round(max_dd, 4),
            "alpha": round(alpha, 4),
            "avg_hold_days": round(avg_hold_days, 1),
            "win_count": win_count,
            "loss_count": loss_count,
            "by_sector": by_sector,
            "by_strategy": by_strategy,
            "period_days": days,
            "detail": (f"近{days}天: {total_trades}笔 | 胜率{win_rate:.1f}% | "
                      f"盈亏比{profit_loss_ratio:.2f} | 总盈亏{total_pnl:+,.0f}元 | "
                      f"夏普{sharpe:.2f} | 最大回撤{max_dd:.2%}")
        }

    # ============================================================
    # 三、绩效归因分析
    # ============================================================

    def attribution_analysis(self) -> dict:
        """
        多维度绩效归因
        
        维度:
        - 行业归因: 哪个行业贡献最多利润/亏损
        - 策略归因: 趋势/均值回归/事件驱动哪个更有效
        - 时间归因: 周几/月份效应
        - 持有期归因: 短线vs中线哪个更好
        """
        conn = sqlite3.connect(self.db_path)
        sells_df = pd.read_sql_query("SELECT * FROM trades WHERE action='sell' AND pnl != 0", conn)
        conn.close()

        if sells_df.empty:
            return {"detail": "无交易记录"}

        # 时间维度
        sells_df["weekday"] = pd.to_datetime(sells_df["trade_date"]).dt.dayofweek
        by_weekday = {}
        weekday_names = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五"}
        for wd, group in sells_df.groupby("weekday"):
            by_weekday[weekday_names.get(wd, f"周{wd}")] = {
                "trades": len(group),
                "pnl": round(group["pnl"].sum(), 2),
                "win_rate": round(len(group[group["pnl"] > 0]) / len(group) * 100, 1),
            }

        # 持有期维度
        if "hold_days" in sells_df.columns:
            sells_df["hold_type"] = sells_df["hold_days"].apply(
                lambda x: "短线(≤5天)" if x <= 5 else ("中线(6-20天)" if x <= 20 else "长线(>20天)")
            )
            by_hold = {}
            for ht, group in sells_df.groupby("hold_type"):
                by_hold[ht] = {
                    "trades": len(group),
                    "pnl": round(group["pnl"].sum(), 2),
                    "win_rate": round(len(group[group["pnl"] > 0]) / len(group) * 100, 1),
                    "avg_pnl": round(group["pnl"].mean(), 2),
                }
        else:
            by_hold = {}

        return {
            "by_weekday": by_weekday,
            "by_hold_period": by_hold,
            "detail": "归因分析完成"
        }

    # ============================================================
    # 四、获取最近交易
    # ============================================================

    def get_recent_trades(self, limit: int = 20) -> list:
        """获取最近N笔交易"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT trade_date, code, name, action, shares, price, pnl, pnl_pct, reason, strategy
            FROM trades ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()

        trades = []
        for row in rows:
            trades.append({
                "date": row[0], "code": row[1], "name": row[2],
                "action": row[3], "shares": row[4], "price": row[5],
                "pnl": row[6], "pnl_pct": row[7], "reason": row[8], "strategy": row[9]
            })
        return trades

    # ============================================================
    # 辅助方法
    # ============================================================

    def _calc_max_consecutive(self, pnl_array) -> int:
        """计算最大连续亏损次数"""
        max_consec = 0
        current = 0
        for pnl in pnl_array:
            if pnl <= 0:
                current += 1
                max_consec = max(max_consec, current)
            else:
                current = 0
        return max_consec


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 50)
    print("  交易日志模块 - 测试")
    print("=" * 50)

    journal = TradeJournal()

    # 模拟交易记录
    journal.record_buy("002371", "北方华创", 200, 807.5, "趋势突破买入", "半导体设备", "trend")
    journal.record_sell("002371", "北方华创", 200, 750.0, "止损卖出", "半导体设备", "trend",
                       buy_price=807.5, buy_date="2025-06-01")

    journal.record_buy("600584", "长电科技", 1200, 97.5, "回踩MA20买入", "半导体封测", "trend")
    journal.record_sell("600584", "长电科技", 600, 103.0, "止盈减半", "半导体封测", "trend",
                       buy_price=97.5, buy_date="2025-05-15")

    # 绩效报告
    report = journal.performance_report()
    print(f"\n{report['detail']}")
    print(f"  胜率: {report['win_rate']}%")
    print(f"  盈亏比: {report['profit_loss_ratio']}")
    print(f"  总盈亏: {report['total_pnl']:+,.0f}元")

    print("\n[OK] 交易日志模块测试完成")
