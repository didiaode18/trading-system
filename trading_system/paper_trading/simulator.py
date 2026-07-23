"""
模拟盘交易引擎
==============
- 模拟执行策略信号（不实际下单）
- 每日记录净值
- 毕业条件判定：20天夏普>1.0 且 最大回撤<10%
"""

import json
import os
import logging
import datetime
import numpy as np
import pandas as pd

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

PAPER_STATE_FILE = os.path.join(config.PROJECT_ROOT, "paper_trading", "paper_state.json")


class PaperTradingSimulator:
    """
    模拟盘交易引擎
    
    用法:
        sim = PaperTradingSimulator(initial_capital=1000000)
        sim.execute_signal("buy", "002371", price=350, shares=200)
        sim.daily_update(price_dict)
        if sim.is_graduated():
            print("模拟盘毕业，可转实盘！")
    """

    def __init__(self, initial_capital: float = None,
                 graduation_days: int = 20,
                 min_sharpe: float = 1.0,
                 max_drawdown: float = 0.10):
        self.initial_capital = initial_capital or config.TOTAL_CAPITAL
        self.cash = self.initial_capital
        self.positions: dict = {}  # {code: {shares, buy_price, buy_date}}
        self.daily_values: list = []
        self.graduation_days = graduation_days
        self.min_sharpe = min_sharpe
        self.max_drawdown = max_drawdown
        self.start_date = None
        self._load_state()

    def execute_signal(self, action: str, code: str, price: float,
                       shares: int = 0, date: str = None):
        """执行模拟信号"""
        if date is None:
            date = datetime.date.today().strftime("%Y-%m-%d")

        if action == "buy" and shares > 0:
            cost = price * shares
            if cost <= self.cash:
                self.cash -= cost
                if code in self.positions:
                    pos = self.positions[code]
                    total = pos["shares"] + shares
                    pos["buy_price"] = (pos["buy_price"] * pos["shares"] + price * shares) / total
                    pos["shares"] = total
                else:
                    self.positions[code] = {
                        "shares": shares, "buy_price": price, "buy_date": date
                    }
                logger.info(f"[模拟盘] 买入 {code} {shares}股@{price:.2f}")

        elif action == "sell" and code in self.positions:
            pos = self.positions[code]
            sell_shares = min(shares, pos["shares"]) if shares > 0 else pos["shares"]
            revenue = price * sell_shares
            self.cash += revenue
            pnl = (price - pos["buy_price"]) * sell_shares
            pos["shares"] -= sell_shares
            if pos["shares"] <= 0:
                del self.positions[code]
            logger.info(f"[模拟盘] 卖出 {code} {sell_shares}股@{price:.2f}, 盈亏{pnl:+,.0f}")

    def daily_update(self, price_dict: dict, date: str = None):
        """每日更新净值"""
        if date is None:
            date = datetime.date.today().strftime("%Y-%m-%d")
        if self.start_date is None:
            self.start_date = date

        total = self.cash
        for code, pos in self.positions.items():
            price = price_dict.get(code, pos["buy_price"])
            total += pos["shares"] * price

        self.daily_values.append({"date": date, "value": total})
        self._save_state()

    def get_performance(self) -> dict:
        """获取模拟盘绩效"""
        if len(self.daily_values) < 2:
            return {"days": len(self.daily_values), "sharpe": 0, "max_dd": 0}

        values = pd.Series([d["value"] for d in self.daily_values])
        returns = values.pct_change().dropna()

        # 夏普
        if returns.std() > 0:
            sharpe = (returns.mean() - 0.03/252) / returns.std() * np.sqrt(252)
        else:
            sharpe = 0

        # 最大回撤
        cummax = values.cummax()
        dd = (cummax - values) / cummax
        max_dd = dd.max()

        total_return = (values.iloc[-1] - self.initial_capital) / self.initial_capital

        return {
            "days": len(self.daily_values),
            "total_return": round(total_return, 4),
            "sharpe": round(sharpe, 2),
            "max_drawdown": round(max_dd, 4),
            "current_value": round(values.iloc[-1], 0),
        }

    def is_graduated(self) -> bool:
        """判断是否达到毕业条件"""
        perf = self.get_performance()
        if perf["days"] < self.graduation_days:
            return False
        return perf["sharpe"] >= self.min_sharpe and perf["max_drawdown"] <= self.max_drawdown

    def graduation_report(self) -> str:
        """毕业评估报告"""
        perf = self.get_performance()
        graduated = self.is_graduated()
        status = "✅ 已毕业" if graduated else "⏳ 未达标"

        return (
            f"模拟盘评估 [{status}]\n"
            f"  运行天数: {perf['days']}/{self.graduation_days}\n"
            f"  总收益:   {perf['total_return']:.2%}\n"
            f"  夏普比率: {perf['sharpe']:.2f} (要求>{self.min_sharpe})\n"
            f"  最大回撤: {perf['max_drawdown']:.2%} (要求<{self.max_drawdown:.0%})\n"
        )

    def reset(self):
        """重置模拟盘"""
        self.cash = self.initial_capital
        self.positions = {}
        self.daily_values = []
        self.start_date = None
        self._save_state()

    def _load_state(self):
        if os.path.exists(PAPER_STATE_FILE):
            try:
                with open(PAPER_STATE_FILE, "r") as f:
                    state = json.load(f)
                self.cash = state.get("cash", self.initial_capital)
                self.positions = state.get("positions", {})
                self.daily_values = state.get("daily_values", [])
                self.start_date = state.get("start_date")
            except Exception:
                pass

    def _save_state(self):
        os.makedirs(os.path.dirname(PAPER_STATE_FILE), exist_ok=True)
        state = {
            "cash": self.cash,
            "positions": self.positions,
            "daily_values": self.daily_values[-100:],  # 只保留最近100天
            "start_date": self.start_date,
        }
        with open(PAPER_STATE_FILE, "w") as f:
            json.dump(state, f)
