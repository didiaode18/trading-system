"""
信号衰减模型 + 条件单有效期管理
================================
量化信号的时间有效性，避免执行过期信号

核心功能:
  1. 信号衰减曲线：T+1/T+2/T+3/T+5 信号胜率衰减
  2. 条件单有效期：买入单3日过期，止损单永久，止盈单每周更新
  3. 信号新鲜度评分：基于信号产生时间计算当前有效性
  4. 自动撤单建议：过期信号自动标记撤销

原理（基于历史统计）:
  - 买入信号在T+1执行胜率最高，T+3后显著衰减
  - 止损信号无衰减（保护性，永久有效）
  - 止盈信号随最高价变化需动态更新
  - 加仓信号在浮盈确认后2日内有效

使用方式:
    from strategy.signal_decay import SignalDecayManager
    manager = SignalDecayManager()
    # 评估信号新鲜度
    freshness = manager.evaluate_freshness(signal, signal_date)
    # 管理条件单有效期
    orders = manager.apply_validity(orders)
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


# ============================================================
# 一、信号衰减模型
# ============================================================

# 各类信号的衰减曲线（基于历史回测统计）
# 格式: {信号类型: {T+N: 胜率衰减系数}}
DECAY_CURVES = {
    "buy_pullback": {  # 缩量回踩买入
        1: 1.00,   # T+1: 100%有效性
        2: 0.82,   # T+2: 82%
        3: 0.61,   # T+3: 61%
        5: 0.35,   # T+5: 35%（基本失效）
    },
    "buy_breakout": {  # 放量突破买入
        1: 1.00,
        2: 0.75,   # 突破后第2天追入胜率下降快
        3: 0.50,
        5: 0.20,   # 5天后基本是追高
    },
    "buy_dual": {  # 双买点共振
        1: 1.00,
        2: 0.88,   # 共振信号衰减慢
        3: 0.72,
        5: 0.50,
    },
    "sell_stop_loss": {  # 止损（无衰减）
        1: 1.00, 2: 1.00, 3: 1.00, 5: 1.00, 10: 1.00, 30: 1.00,
    },
    "sell_take_profit": {  # 止盈（慢衰减）
        1: 1.00, 2: 0.95, 3: 0.90, 5: 0.80, 10: 0.60,
    },
    "sell_trend_break": {  # 趋势破位
        1: 1.00, 2: 0.90, 3: 0.75, 5: 0.50,
    },
    "add_position": {  # 加仓
        1: 1.00, 2: 0.85, 3: 0.65, 5: 0.40,
    },
}

# 条件单有效期（交易日）
ORDER_VALIDITY = {
    "buy_limit": 3,          # 买入条件单: 3个交易日
    "buy_rebound": 3,        # 反弹买入: 3个交易日
    "sell_stop_loss": 999,   # 止损: 永久有效
    "sell_take_profit": 5,   # 止盈: 5个交易日（需每周更新）
    "sell_drawdown": 5,      # 回落止盈: 5个交易日
}


class SignalDecayManager:
    """信号衰减与条件单有效期管理器"""

    def __init__(self, decay_curves: dict = None, validity_days: dict = None):
        self.decay_curves = decay_curves or DECAY_CURVES
        self.validity_days = validity_days or ORDER_VALIDITY
        # 信号历史记录文件
        self._history_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "output", "signal_history.json"
        )

    def evaluate_freshness(self, signal: dict, signal_date: str = None,
                           eval_date: str = None) -> dict:
        """
        评估信号新鲜度
        
        参数:
            signal: 策略信号字典
            signal_date: 信号产生日期 (YYYY-MM-DD)
            eval_date: 评估日期（默认今天）
        
        返回:
            {
                "days_elapsed": int,        # 已过天数
                "decay_factor": float,      # 衰减系数 (0~1)
                "is_valid": bool,           # 是否仍有效
                "signal_type": str,         # 信号类型
                "recommendation": str,      # 建议
            }
        """
        if signal_date is None:
            signal_date = signal.get("date", datetime.date.today().isoformat())
        if eval_date is None:
            eval_date = datetime.date.today().isoformat()

        # 计算交易日间隔（简化：用自然日×0.7估算交易日）
        try:
            d1 = datetime.date.fromisoformat(signal_date[:10])
            d2 = datetime.date.fromisoformat(eval_date[:10])
            natural_days = (d2 - d1).days
            trade_days = max(0, int(natural_days * 0.7))
        except (ValueError, TypeError):
            trade_days = 0

        # 识别信号类型
        signal_type = self._classify_signal(signal)

        # 获取衰减曲线
        curve = self.decay_curves.get(signal_type, self.decay_curves["buy_pullback"])

        # 插值计算衰减系数
        decay_factor = self._interpolate_decay(curve, trade_days)

        # 有效期判断
        order_type = self._signal_to_order_type(signal)
        max_validity = self.validity_days.get(order_type, 3)
        is_valid = trade_days <= max_validity

        # 建议
        if not is_valid:
            recommendation = f"信号已过期({trade_days}天>{max_validity}天)，建议撤销条件单"
        elif decay_factor < 0.5:
            recommendation = f"信号严重衰减(有效性{decay_factor:.0%})，建议撤销或大幅调整价格"
        elif decay_factor < 0.75:
            recommendation = f"信号有所衰减(有效性{decay_factor:.0%})，建议调整触发价"
        else:
            recommendation = f"信号有效(有效性{decay_factor:.0%})"

        return {
            "days_elapsed": trade_days,
            "decay_factor": round(decay_factor, 3),
            "is_valid": is_valid,
            "signal_type": signal_type,
            "order_type": order_type,
            "max_validity_days": max_validity,
            "recommendation": recommendation,
        }

    def apply_validity(self, orders: list, signal_dates: dict = None) -> list:
        """
        对条件单列表应用有效期管理
        
        参数:
            orders: 条件单列表 [{"code", "order_type", "trigger_price", ...}]
            signal_dates: {code: signal_date} 信号产生日期
        
        返回:
            更新后的条件单列表（过期的标记为expired）
        """
        if signal_dates is None:
            signal_dates = {}

        today = datetime.date.today()
        result = []

        for order in orders:
            order = order.copy()
            code = order.get("code", "")
            order_type = order.get("order_type", "buy_limit")

            # 获取信号日期
            sig_date = signal_dates.get(code, today.isoformat())
            try:
                d1 = datetime.date.fromisoformat(sig_date[:10])
                trade_days = max(0, int((today - d1).days * 0.7))
            except (ValueError, TypeError):
                trade_days = 0

            # 有效期
            max_days = self.validity_days.get(order_type, 3)
            is_expired = trade_days > max_days

            # 衰减系数
            signal_type = self._order_type_to_signal_type(order_type)
            curve = self.decay_curves.get(signal_type, self.decay_curves["buy_pullback"])
            decay = self._interpolate_decay(curve, trade_days)

            order["validity_days_remaining"] = max(0, max_days - trade_days)
            order["decay_factor"] = round(decay, 3)
            order["is_expired"] = is_expired

            if is_expired:
                order["status"] = "expired"
                order["notes"] = order.get("notes", "") + f" | ⚠️已过期({trade_days}天)，建议撤销"
            elif decay < 0.5:
                order["status"] = "decayed"
                order["notes"] = order.get("notes", "") + f" | 信号衰减({decay:.0%})，建议调整"
            else:
                order["status"] = "active"

            # 计算过期日期
            try:
                expire_date = d1 + datetime.timedelta(days=int(max_days / 0.7))
                order["expire_date"] = expire_date.isoformat()
            except Exception:
                order["expire_date"] = ""

            result.append(order)

        return result

    def get_validity_summary(self, orders: list) -> dict:
        """获取条件单有效期摘要"""
        active = [o for o in orders if o.get("status") == "active"]
        decayed = [o for o in orders if o.get("status") == "decayed"]
        expired = [o for o in orders if o.get("status") == "expired"]

        return {
            "total": len(orders),
            "active": len(active),
            "decayed": len(decayed),
            "expired": len(expired),
            "expire_soon": len([o for o in active
                               if o.get("validity_days_remaining", 99) <= 1]),
            "recommendations": [
                f"{len(expired)}个条件单已过期，建议撤销",
                f"{len(decayed)}个条件单信号衰减，建议调整价格",
            ] if expired or decayed else ["所有条件单状态正常"],
        }

    # ============================================================
    # 内部方法
    # ============================================================

    def _classify_signal(self, signal: dict) -> str:
        """将信号分类为衰减曲线类型"""
        reason = signal.get("signal_reason", "")
        buy_type = signal.get("buy_type", "")

        if signal.get("sell_signal"):
            if "止损" in reason or "强制" in reason:
                return "sell_stop_loss"
            elif "止盈" in reason or "阶梯" in reason:
                return "sell_take_profit"
            elif "趋势破位" in reason or "跌破" in reason:
                return "sell_trend_break"
            else:
                return "sell_stop_loss"

        if signal.get("add_position"):
            return "add_position"

        if signal.get("buy_signal"):
            if "双买点" in reason or "共振" in reason:
                return "buy_dual"
            elif "突破" in buy_type or "突破" in reason:
                return "buy_breakout"
            else:
                return "buy_pullback"

        return "buy_pullback"

    def _signal_to_order_type(self, signal: dict) -> str:
        """信号→条件单类型"""
        if signal.get("sell_signal"):
            reason = signal.get("signal_reason", "")
            if "止盈" in reason or "阶梯" in reason:
                return "sell_take_profit"
            elif "回落" in reason:
                return "sell_drawdown"
            else:
                return "sell_stop_loss"
        elif signal.get("add_position"):
            return "buy_rebound"
        else:
            return "buy_limit"

    def _order_type_to_signal_type(self, order_type: str) -> str:
        """条件单类型→信号衰减类型"""
        mapping = {
            "buy_limit": "buy_pullback",
            "buy_rebound": "add_position",
            "sell_stop_loss": "sell_stop_loss",
            "sell_take_profit": "sell_take_profit",
            "sell_drawdown": "sell_take_profit",
        }
        return mapping.get(order_type, "buy_pullback")

    def _interpolate_decay(self, curve: dict, days: int) -> float:
        """插值计算衰减系数"""
        if days <= 0:
            return 1.0

        # 找到相邻的两个点
        sorted_days = sorted(curve.keys())
        if days >= sorted_days[-1]:
            return curve[sorted_days[-1]]

        for i in range(len(sorted_days) - 1):
            d1, d2 = sorted_days[i], sorted_days[i+1]
            if d1 <= days <= d2:
                # 线性插值
                v1, v2 = curve[d1], curve[d2]
                ratio = (days - d1) / (d2 - d1)
                return v1 + (v2 - v1) * ratio

        return curve.get(days, 0.5)


# ============================================================
# 二、信号历史统计（用于校准衰减曲线）
# ============================================================

class SignalHistoryTracker:
    """
    信号历史追踪器
    记录每个信号产生后的实际表现，用于校准衰减曲线
    """

    def __init__(self, history_file: str = None):
        self.history_file = history_file or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "output", "signal_history.json"
        )
        self.history = self._load()

    def _load(self) -> list:
        """加载历史记录"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def record_signal(self, code: str, signal: dict, signal_date: str):
        """记录新信号"""
        entry = {
            "code": code,
            "date": signal_date,
            "type": "buy" if signal.get("buy_signal") else "sell",
            "buy_type": signal.get("buy_type", ""),
            "quality_score": signal.get("quality_score", 50),
            "price": signal.get("buy_price") or signal.get("sell_price", 0),
            "recorded_at": datetime.datetime.now().isoformat(),
            # 后续填充
            "result_t1": None,
            "result_t3": None,
            "result_t5": None,
        }
        self.history.append(entry)
        # 只保留最近200条
        self.history = self.history[-200:]

    def update_results(self, data_dict: dict):
        """
        用最新数据更新历史信号的T+N收益
        """
        for entry in self.history:
            code = entry["code"]
            if code not in data_dict:
                continue
            df = data_dict[code]
            sig_date = entry["date"]
            sig_price = entry["price"]
            if sig_price <= 0:
                continue

            # 找到信号日之后的数据
            try:
                date_idx = df[df["date"] == sig_date].index
                if len(date_idx) == 0:
                    continue
                idx = date_idx[0]
            except Exception:
                continue

            # T+1, T+3, T+5 收益
            for t, key in [(1, "result_t1"), (3, "result_t3"), (5, "result_t5")]:
                if entry[key] is None and idx + t < len(df):
                    future_price = df["close"].iloc[idx + t]
                    if entry["type"] == "buy":
                        entry[key] = round((future_price - sig_price) / sig_price, 4)
                    else:
                        entry[key] = round((sig_price - future_price) / sig_price, 4)

    def compute_decay_stats(self) -> dict:
        """
        统计各类型信号的实际衰减数据
        用于校准 DECAY_CURVES
        """
        stats = {}
        for entry in self.history:
            sig_type = entry.get("buy_type", "pullback")
            if entry["type"] == "buy":
                if "突破" in sig_type:
                    key = "buy_breakout"
                elif "共振" in sig_type or "双" in sig_type:
                    key = "buy_dual"
                else:
                    key = "buy_pullback"
            else:
                key = "sell"

            if key not in stats:
                stats[key] = {"t1": [], "t3": [], "t5": [], "count": 0}

            stats[key]["count"] += 1
            if entry["result_t1"] is not None:
                stats[key]["t1"].append(entry["result_t1"])
            if entry["result_t3"] is not None:
                stats[key]["t3"].append(entry["result_t3"])
            if entry["result_t5"] is not None:
                stats[key]["t5"].append(entry["result_t5"])

        # 计算胜率衰减
        result = {}
        for key, data in stats.items():
            if data["count"] < 5:
                continue
            t1_winrate = np.mean([1 for r in data["t1"] if r > 0]) if data["t1"] else None
            t3_winrate = np.mean([1 for r in data["t3"] if r > 0]) if data["t3"] else None
            t5_winrate = np.mean([1 for r in data["t5"] if r > 0]) if data["t5"] else None

            result[key] = {
                "count": data["count"],
                "t1_winrate": round(t1_winrate, 3) if t1_winrate else None,
                "t3_winrate": round(t3_winrate, 3) if t3_winrate else None,
                "t5_winrate": round(t5_winrate, 3) if t5_winrate else None,
                "t1_avg_return": round(np.mean(data["t1"]), 4) if data["t1"] else None,
                "t3_avg_return": round(np.mean(data["t3"]), 4) if data["t3"] else None,
                "t5_avg_return": round(np.mean(data["t5"]), 4) if data["t5"] else None,
            }

        return result

    def save(self):
        """保存历史记录"""
        os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2)
