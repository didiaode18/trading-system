"""
交易记录与归因
==============
记录每笔完整交易（买入→持有→卖出），计算MFE/MAE
"""

import json
import os
import logging
import datetime
import pandas as pd
import numpy as np

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

TRADE_LOG_FILE = os.path.join(config.PROJECT_ROOT, "attribution", "trade_history.json")


class TradeLog:
    """
    交易记录器
    
    记录每笔交易的完整生命周期：
    - 买入价、卖出价、持仓天数
    - MFE（最大浮盈）、MAE（最大浮亏）
    - 盈亏归因
    """

    def __init__(self):
        self.trades: list = []
        self._load()

    def record_buy(self, code: str, price: float, shares: int, date: str,
                   reason: str = ""):
        """记录买入"""
        self.trades.append({
            "code": code,
            "buy_price": price,
            "buy_shares": shares,
            "buy_date": date,
            "sell_price": None,
            "sell_date": None,
            "pnl": None,
            "pnl_pct": None,
            "hold_days": None,
            "mfe": 0,  # 最大浮盈
            "mae": 0,  # 最大浮亏
            "reason": reason,
            "status": "open",
        })

    def record_sell(self, code: str, price: float, date: str, reason: str = ""):
        """记录卖出（匹配最近的买入）"""
        for trade in reversed(self.trades):
            if trade["code"] == code and trade["status"] == "open":
                trade["sell_price"] = price
                trade["sell_date"] = date
                trade["pnl"] = (price - trade["buy_price"]) * trade["buy_shares"]
                trade["pnl_pct"] = (price - trade["buy_price"]) / trade["buy_price"]
                trade["hold_days"] = (
                    datetime.datetime.strptime(date, "%Y-%m-%d") -
                    datetime.datetime.strptime(trade["buy_date"], "%Y-%m-%d")
                ).days
                trade["status"] = "closed"
                trade["sell_reason"] = reason
                break

    def update_extremes(self, code: str, current_price: float):
        """更新持仓期间的MFE/MAE"""
        for trade in reversed(self.trades):
            if trade["code"] == code and trade["status"] == "open":
                pnl_pct = (current_price - trade["buy_price"]) / trade["buy_price"]
                trade["mfe"] = max(trade["mfe"], pnl_pct)
                trade["mae"] = min(trade["mae"], pnl_pct)
                break

    def get_closed_trades(self) -> list:
        """获取已平仓交易"""
        return [t for t in self.trades if t["status"] == "closed"]

    def get_open_trades(self) -> list:
        """获取持仓中交易"""
        return [t for t in self.trades if t["status"] == "open"]

    def get_stats(self) -> dict:
        """统计汇总"""
        closed = self.get_closed_trades()
        if not closed:
            return {"total": 0}

        pnls = [t["pnl_pct"] for t in closed if t["pnl_pct"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        mfes = [t["mfe"] for t in closed]
        maes = [t["mae"] for t in closed]

        return {
            "total": len(closed),
            "win_rate": len(wins) / len(closed) if closed else 0,
            "avg_pnl": np.mean(pnls) if pnls else 0,
            "avg_win": np.mean(wins) if wins else 0,
            "avg_loss": np.mean(losses) if losses else 0,
            "avg_mfe": np.mean(mfes) if mfes else 0,
            "avg_mae": np.mean(maes) if maes else 0,
            "mfe_capture": (np.mean(pnls) / np.mean(mfes)) if mfes and np.mean(mfes) > 0 else 0,
            "avg_hold_days": np.mean([t["hold_days"] for t in closed if t["hold_days"]]),
        }

    def to_dataframe(self) -> pd.DataFrame:
        """转为DataFrame"""
        return pd.DataFrame(self.get_closed_trades())

    def _load(self):
        if os.path.exists(TRADE_LOG_FILE):
            try:
                with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
                    self.trades = json.load(f)
            except Exception:
                self.trades = []

    def save(self):
        os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)
        with open(TRADE_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.trades, f, ensure_ascii=False, indent=2)
