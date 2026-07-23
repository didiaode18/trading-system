"""
大单拆分执行模块（TWAP/VWAP）
==============================
将大额条件单拆分为多笔小单，降低市场冲击成本

核心功能:
  1. 大单识别：单笔 > 5万股或金额 > 50万 → 触发拆分
  2. TWAP（时间加权）：均匀分配到多个时间段
  3. VWAP（成交量加权）：按历史成交量分布分配
  4. 分批触发价：每批价格递增/递减
  5. 条件单拆分输出：生成多笔子条件单

适用场景:
  - 科创50: 196,900股 → 拆分为4~5笔
  - 任何单笔金额 > 50万的交易
  - ETF大额申赎

使用方式:
    from execution.twap import ExecutionSplitter
    splitter = ExecutionSplitter()
    sub_orders = splitter.split_order(order, df)
"""

import pandas as pd
import numpy as np
import logging
import datetime
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# 拆分阈值
SPLIT_SHARE_THRESHOLD = 50000      # 股数阈值: 5万股
SPLIT_AMOUNT_THRESHOLD = 500000    # 金额阈值: 50万元
MAX_SUB_ORDERS = 5                 # 最多拆分为5笔
MIN_SUB_SHARES = 10000             # 每笔最少1万股（ETF）/ 100股（股票）


class ExecutionSplitter:
    """大单拆分执行器"""

    def __init__(self, share_threshold: int = SPLIT_SHARE_THRESHOLD,
                 amount_threshold: float = SPLIT_AMOUNT_THRESHOLD,
                 max_splits: int = MAX_SUB_ORDERS):
        self.share_threshold = share_threshold
        self.amount_threshold = amount_threshold
        self.max_splits = max_splits

    def should_split(self, order: dict) -> bool:
        """判断是否需要拆分"""
        shares = order.get("shares", 0)
        price = order.get("trigger_price", 0)
        amount = shares * price

        # ETF阈值更低（因为单价低）
        code = order.get("code", "")
        is_etf = code.startswith("588") or code.startswith("159")
        effective_threshold = self.share_threshold if not is_etf else self.share_threshold * 2

        return shares > effective_threshold or amount > self.amount_threshold

    def split_order(self, order: dict, df: pd.DataFrame = None,
                    method: str = "auto") -> list:
        """
        拆分大额条件单
        
        参数:
            order: 原始条件单 {code, name, shares, trigger_price, order_type, ...}
            df: 日线数据（用于VWAP权重计算）
            method: "twap" / "vwap" / "auto"
        
        返回:
            [sub_order_1, sub_order_2, ...] 拆分后的子条件单列表
        """
        if not self.should_split(order):
            return [order]  # 不需要拆分

        shares = order.get("shares", 0)
        price = order.get("trigger_price", 0)
        code = order.get("code", "")
        is_etf = code.startswith("588") or code.startswith("159")
        min_shares = 10000 if is_etf else 100

        # 确定拆分笔数
        n_splits = self._determine_splits(shares, price, min_shares)

        if n_splits <= 1:
            return [order]

        # 选择拆分方法
        if method == "auto":
            method = "vwap" if df is not None and len(df) >= 5 else "twap"

        # 计算各笔权重
        if method == "vwap" and df is not None:
            weights = self._vwap_weights(n_splits, df)
        else:
            weights = self._twap_weights(n_splits)

        # 分配股数
        sub_shares = self._allocate_shares(shares, weights, min_shares)

        # 计算各笔触发价（买入递增，卖出递减）
        is_buy = order.get("order_type", "").startswith("buy")
        sub_prices = self._calc_sub_prices(price, n_splits, is_buy)

        # 生成子条件单
        sub_orders = []
        for i in range(n_splits):
            sub = order.copy()
            sub["shares"] = sub_shares[i]
            sub["trigger_price"] = sub_prices[i]
            sub["order_price"] = round(sub_prices[i] * (1.01 if is_buy else 0.99), 3)
            sub["sub_order_id"] = i + 1
            sub["total_sub_orders"] = n_splits
            sub["split_method"] = method
            sub["original_shares"] = shares
            sub["original_trigger"] = price
            sub["condition_desc"] = (
                f"[第{i+1}/{n_splits}笔] "
                f"{'买入' if is_buy else '卖出'} {sub_shares[i]}股 "
                f"@ {sub_prices[i]:.3f} "
                f"({method.upper()}拆分, 原单{shares}股@{price:.3f})"
            )
            sub["notes"] = (
                f"{order.get('notes', '')} | "
                f"TWAP拆分第{i+1}笔 | 时间窗口: {self._time_window(i, n_splits)}"
            )
            sub_orders.append(sub)

        logger.info(f"  [执行拆分] {code} {order.get('name', '')}: "
                    f"{shares}股 → {n_splits}笔 ({method.upper()})")

        return sub_orders

    def split_all_orders(self, orders: list, data_dict: dict = None) -> list:
        """
        批量拆分所有需要拆分的条件单
        
        参数:
            orders: 条件单列表
            data_dict: {code: DataFrame}
        
        返回:
            拆分后的完整条件单列表
        """
        if data_dict is None:
            data_dict = {}

        result = []
        split_count = 0

        for order in orders:
            code = order.get("code", "")
            df = data_dict.get(code)

            if self.should_split(order):
                sub_orders = self.split_order(order, df)
                result.extend(sub_orders)
                split_count += 1
            else:
                result.append(order)

        if split_count > 0:
            logger.info(f"[执行拆分] 共{split_count}笔大单被拆分，"
                        f"总计{len(result)}笔子单")

        return result

    def generate_execution_schedule(self, sub_orders: list) -> dict:
        """
        生成执行时间表（供盘中监控使用）
        
        返回:
            {
                "code": str,
                "total_shares": int,
                "schedule": [
                    {"time": "09:35", "shares": 50000, "price": 1.95, "status": "pending"},
                    ...
                ]
            }
        """
        if not sub_orders:
            return {}

        code = sub_orders[0].get("code", "")
        total_shares = sum(o.get("shares", 0) for o in sub_orders)
        n = len(sub_orders)

        schedule = []
        # A股交易时间: 9:30-11:30, 13:00-15:00
        # 避开开盘5分钟和收盘5分钟
        time_slots = self._generate_time_slots(n)

        for i, sub in enumerate(sub_orders):
            schedule.append({
                "time": time_slots[i] if i < len(time_slots) else "14:50",
                "shares": sub.get("shares", 0),
                "price": sub.get("trigger_price", 0),
                "status": "pending",
                "sub_order_id": i + 1,
            })

        return {
            "code": code,
            "name": sub_orders[0].get("name", ""),
            "total_shares": total_shares,
            "split_method": sub_orders[0].get("split_method", "twap"),
            "schedule": schedule,
        }

    # ============================================================
    # 内部方法
    # ============================================================

    def _determine_splits(self, shares: int, price: float, min_shares: int) -> int:
        """确定拆分笔数"""
        amount = shares * price

        # 基于金额
        if amount > 2000000:  # > 200万
            n = 5
        elif amount > 1000000:  # > 100万
            n = 4
        elif amount > self.amount_threshold:  # > 50万
            n = 3
        else:
            n = 2

        # 基于股数
        if shares / n < min_shares:
            n = max(2, shares // min_shares)

        return min(n, self.max_splits)

    def _twap_weights(self, n: int) -> list:
        """TWAP等权分配"""
        return [1.0 / n] * n

    def _vwap_weights(self, n: int, df: pd.DataFrame) -> list:
        """
        VWAP权重：按历史日内成交量分布
        
        A股典型日内成交量分布:
        - 9:30-10:00: 约20%（开盘活跃）
        - 10:00-11:30: 约25%
        - 13:00-14:00: 约20%
        - 14:00-15:00: 约35%（尾盘活跃）
        """
        # 典型A股日内分布
        intraday_pattern = [0.20, 0.25, 0.20, 0.35]

        if n <= len(intraday_pattern):
            # 直接取前n个并归一化
            weights = intraday_pattern[:n]
        else:
            # 均匀分配
            weights = [1.0 / n] * n

        total = sum(weights)
        return [w / total for w in weights]

    def _allocate_shares(self, total_shares: int, weights: list,
                         min_shares: int) -> list:
        """按权重分配股数（确保每笔 >= min_shares 且为100整数倍）"""
        n = len(weights)
        raw = [total_shares * w for w in weights]

        # 取整到100
        allocated = [max(min_shares, int(r / 100) * 100) for r in raw]

        # 修正总数差异（加到最后一笔）
        diff = total_shares - sum(allocated)
        allocated[-1] += int(diff / 100) * 100
        if allocated[-1] < min_shares:
            allocated[-1] = min_shares

        return allocated

    def _calc_sub_prices(self, base_price: float, n: int, is_buy: bool) -> list:
        """
        计算各笔触发价
        买入：递增（第1笔最积极，后续逐步提高）
        卖出：递减（第1笔最保守，后续逐步降低）
        """
        # 价格步进：每笔0.3%
        step = 0.003
        prices = []
        for i in range(n):
            if is_buy:
                p = base_price * (1 + step * i)
            else:
                p = base_price * (1 - step * i)
            prices.append(round(p, 3))
        return prices

    def _time_window(self, idx: int, total: int) -> str:
        """计算第idx笔的时间窗口"""
        slots = self._generate_time_slots(total)
        if idx < len(slots):
            return slots[idx]
        return "14:50"

    def _generate_time_slots(self, n: int) -> list:
        """生成n个执行时间槽"""
        # 可用时间窗口（避开开盘5分钟和收盘5分钟）
        all_slots = [
            "09:35", "09:50", "10:10", "10:30", "10:50",
            "11:10", "13:05", "13:30", "14:00", "14:30", "14:50"
        ]

        if n <= len(all_slots):
            # 均匀选取
            step = len(all_slots) / n
            return [all_slots[int(i * step)] for i in range(n)]
        else:
            return all_slots[:n]
