"""
执行质量追踪模块（滑点分析）
============================
记录每笔实际成交价 vs 信号价，统计滑点分布，校准回测参数

核心功能:
  1. 滑点记录：实际成交价 vs 信号触发价
  2. 滑点统计：平均/中位数/最大/分布
  3. 回测校准：如果实际滑点 > 回测假设 → 调整参数
  4. 流动性评估：识别高滑点股票 → 降仓或排除
  5. 执行时机分析：开盘/盘中/尾盘哪个时段滑点最小

使用方式:
    from execution.slippage_tracker import SlippageTracker
    tracker = SlippageTracker()
    tracker.record(code, signal_price, actual_price, shares, time)
    report = tracker.generate_report()
"""

import pandas as pd
import numpy as np
import logging
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class SlippageTracker:
    """执行质量追踪器"""

    def __init__(self, data_file: str = None):
        self.data_file = data_file or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "output", "execution_log.json"
        )
        self.records = self._load()

    def record(self, code: str, signal_price: float, actual_price: float,
               shares: int, direction: str = "buy",
               exec_time: str = None, order_type: str = ""):
        """
        记录一笔执行
        
        参数:
            code: 股票代码
            signal_price: 信号触发价（条件单价格）
            actual_price: 实际成交价
            shares: 成交股数
            direction: "buy" / "sell"
            exec_time: 执行时间 (HH:MM)
            order_type: 条件单类型
        """
        if signal_price <= 0 or actual_price <= 0:
            return

        # 滑点计算（买入：实际>信号为正滑点；卖出：实际<信号为正滑点）
        if direction == "buy":
            slippage_pct = (actual_price - signal_price) / signal_price
        else:
            slippage_pct = (signal_price - actual_price) / signal_price

        record = {
            "code": code,
            "name": config.get_stock_name(code),
            "date": datetime.date.today().isoformat(),
            "time": exec_time or datetime.datetime.now().strftime("%H:%M"),
            "direction": direction,
            "signal_price": round(signal_price, 4),
            "actual_price": round(actual_price, 4),
            "shares": shares,
            "amount": round(shares * actual_price, 2),
            "slippage_pct": round(slippage_pct, 6),
            "slippage_amount": round(abs(actual_price - signal_price) * shares, 2),
            "order_type": order_type,
        }

        self.records.append(record)
        # 保留最近500条
        self.records = self.records[-500:]
        self._save()

        logger.info(f"  [执行记录] {code} {direction} {shares}股 | "
                    f"信号{signal_price:.3f} → 实际{actual_price:.3f} | "
                    f"滑点{slippage_pct:.3%}")

    def generate_report(self, lookback_days: int = 30) -> dict:
        """
        生成执行质量报告
        
        返回:
            {
                "total_trades": int,
                "avg_slippage": float,
                "median_slippage": float,
                "max_slippage": float,
                "slippage_cost": float,      # 总滑点成本
                "by_stock": dict,            # 各股票滑点
                "by_time": dict,             # 各时段滑点
                "by_direction": dict,        # 买/卖滑点
                "backtest_calibration": dict, # 回测校准建议
                "high_slippage_stocks": list, # 高滑点股票
            }
        """
        # 过滤时间范围
        cutoff = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
        recent = [r for r in self.records if r["date"] >= cutoff]

        if not recent:
            return {"total_trades": 0, "message": "无执行记录"}

        slippages = [r["slippage_pct"] for r in recent]
        costs = [r["slippage_amount"] for r in recent]

        # 基础统计
        report = {
            "total_trades": len(recent),
            "avg_slippage": round(np.mean(slippages), 6),
            "median_slippage": round(np.median(slippages), 6),
            "max_slippage": round(max(slippages), 6),
            "min_slippage": round(min(slippages), 6),
            "slippage_cost": round(sum(costs), 2),
            "pct_positive": round(sum(1 for s in slippages if s > 0) / len(slippages), 3),
        }

        # 按股票分组
        by_stock = {}
        for r in recent:
            code = r["code"]
            if code not in by_stock:
                by_stock[code] = {"slippages": [], "costs": [], "count": 0}
            by_stock[code]["slippages"].append(r["slippage_pct"])
            by_stock[code]["costs"].append(r["slippage_amount"])
            by_stock[code]["count"] += 1

        report["by_stock"] = {
            code: {
                "avg": round(np.mean(d["slippages"]), 6),
                "max": round(max(d["slippages"]), 6),
                "cost": round(sum(d["costs"]), 2),
                "count": d["count"],
            }
            for code, d in by_stock.items()
        }

        # 按时段分组
        by_time = {"open": [], "mid": [], "close": []}
        for r in recent:
            t = r.get("time", "12:00")
            if t < "10:00":
                by_time["open"].append(r["slippage_pct"])
            elif t < "14:00":
                by_time["mid"].append(r["slippage_pct"])
            else:
                by_time["close"].append(r["slippage_pct"])

        report["by_time"] = {
            k: {"avg": round(np.mean(v), 6) if v else 0, "count": len(v)}
            for k, v in by_time.items()
        }

        # 按方向
        buys = [r["slippage_pct"] for r in recent if r["direction"] == "buy"]
        sells = [r["slippage_pct"] for r in recent if r["direction"] == "sell"]
        report["by_direction"] = {
            "buy": {"avg": round(np.mean(buys), 6) if buys else 0, "count": len(buys)},
            "sell": {"avg": round(np.mean(sells), 6) if sells else 0, "count": len(sells)},
        }

        # 高滑点股票
        high_slip = [
            {"code": code, "avg_slippage": d["avg"], "count": d["count"]}
            for code, d in report["by_stock"].items()
            if d["avg"] > 0.005 and d["count"] >= 3  # 平均滑点>0.5%且至少3笔
        ]
        high_slip.sort(key=lambda x: x["avg_slippage"], reverse=True)
        report["high_slippage_stocks"] = high_slip[:5]

        # 回测校准建议
        avg_slip = report["avg_slippage"]
        backtest_slip_leader = 0.002  # 回测假设龙头0.2%
        backtest_slip_flex = 0.005    # 回测假设弹性0.5%

        calibration = {}
        if avg_slip > backtest_slip_leader:
            calibration["leader"] = {
                "current_assumption": backtest_slip_leader,
                "actual": avg_slip,
                "suggestion": f"回测滑点假设偏低，建议调整为{avg_slip:.3%}",
            }
        if avg_slip > backtest_slip_flex:
            calibration["flex"] = {
                "current_assumption": backtest_slip_flex,
                "actual": avg_slip,
                "suggestion": f"弹性股滑点超预期，建议调整为{avg_slip * 1.5:.3%}",
            }
        report["backtest_calibration"] = calibration

        return report

    def get_stock_slippage(self, code: str) -> float:
        """获取某只股票的历史平均滑点"""
        stock_records = [r for r in self.records if r["code"] == code]
        if not stock_records:
            return 0.002  # 默认
        return np.mean([r["slippage_pct"] for r in stock_records])

    def _load(self) -> list:
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.records, f, ensure_ascii=False, indent=2)
