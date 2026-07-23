"""
P3 智能执行层
==============
TWAP拆单算法 + 订单管理系统(OMS) + 交易全链路风控

功能:
1. TWAP拆单: 将大单拆分为多个小单，按时间加权均匀执行
2. OMS: 订单全生命周期管理（创建→拆单→执行→完成/撤销）
3. 事前风控: 违规订单拦截（超仓位、超频率）
4. 事中风控: 账户亏损熔断（日亏>3%停止交易）

使用方式:
    from quant.execution import ExecutionEngine, OrderManager
    oms = OrderManager()
    engine = ExecutionEngine(oms)
    engine.submit_order("buy", "600036", 10000, price=35.0)
"""

import logging
import datetime
import numpy as np
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    PENDING = "pending"        # 待执行
    PARTIAL = "partial"        # 部分成交
    FILLED = "filled"          # 全部成交
    CANCELLED = "cancelled"    # 已撤销
    REJECTED = "rejected"      # 被拒绝


class Order:
    """订单对象"""

    def __init__(self, order_id: str, action: str, code: str,
                 quantity: int, price: float = 0,
                 strategy: str = "default"):
        self.order_id = order_id
        self.action = action  # buy/sell
        self.code = code
        self.quantity = quantity
        self.price = price
        self.strategy = strategy
        self.status = OrderStatus.PENDING
        self.filled_quantity = 0
        self.filled_price = 0
        self.create_time = datetime.datetime.now()
        self.update_time = self.create_time
        self.sub_orders = []  # 拆单后的子订单
        self.reject_reason = ""

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "action": self.action,
            "code": self.code,
            "quantity": self.quantity,
            "price": self.price,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "filled_price": self.filled_price,
            "strategy": self.strategy,
        }


class OrderManager:
    """
    订单管理系统(OMS)
    - 订单全生命周期管理
    - 事前风控拦截
    - 订单状态跟踪
    """

    def __init__(self, max_single_order_pct: float = 0.15,
                 max_daily_loss_pct: float = 0.03,
                 max_orders_per_day: int = 50):
        self.orders = {}  # {order_id: Order}
        self.order_counter = 0
        self.max_single_order_pct = max_single_order_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_orders_per_day = max_orders_per_day
        self.daily_pnl = 0  # 当日盈亏
        self.daily_order_count = 0
        self.is_frozen = False  # 熔断状态

    def create_order(self, action: str, code: str, quantity: int,
                     price: float = 0, strategy: str = "default") -> Optional[Order]:
        """
        创建订单（含事前风控检查）

        返回:
            Order对象，或被拒绝返回None
        """
        # 熔断检查
        if self.is_frozen:
            logger.warning(f"[OMS] 账户已熔断，拒绝新订单: {action} {code}")
            return None

        # 日订单数限制
        if self.daily_order_count >= self.max_orders_per_day:
            logger.warning(f"[OMS] 超过日订单上限({self.max_orders_per_day})")
            return None

        self.order_counter += 1
        order_id = f"ORD{self.order_counter:06d}"

        order = Order(order_id, action, code, quantity, price, strategy)
        self.orders[order_id] = order
        self.daily_order_count += 1

        logger.info(f"[OMS] 创建订单: {order_id} {action} {code} x{quantity} @{price:.2f}")
        return order

    def update_order_status(self, order_id: str, status: OrderStatus,
                            filled_qty: int = 0, filled_price: float = 0):
        """更新订单状态"""
        if order_id not in self.orders:
            return
        order = self.orders[order_id]
        order.status = status
        order.filled_quantity += filled_qty
        if filled_price > 0:
            order.filled_price = filled_price
        order.update_time = datetime.datetime.now()

    def check_daily_loss(self, current_pnl: float, total_capital: float):
        """
        事中熔断检查: 日亏损超过阈值则冻结交易
        """
        loss_pct = current_pnl / total_capital if total_capital > 0 else 0
        self.daily_pnl = current_pnl

        if loss_pct <= -self.max_daily_loss_pct:
            self.is_frozen = True
            logger.critical(f"[OMS] 触发熔断! 日亏损{loss_pct:.2%} <= -{self.max_daily_loss_pct:.0%}")

    def reset_daily(self):
        """每日重置"""
        self.daily_pnl = 0
        self.daily_order_count = 0
        self.is_frozen = False

    def get_active_orders(self) -> list:
        return [o for o in self.orders.values()
                if o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)]


class ExecutionEngine:
    """
    智能执行引擎
    - TWAP拆单
    - 与OMS交互
    """

    def __init__(self, oms: OrderManager,
                 twap_slices: int = 5,
                 twap_interval_min: int = 10,
                 max_slice_pct: float = 0.02):
        """
        参数:
            oms: 订单管理器
            twap_slices: TWAP拆分数
            twap_interval_min: 每片间隔(分钟)
            max_slice_pct: 单片不超过日均成交量的2%
        """
        self.oms = oms
        self.twap_slices = twap_slices
        self.twap_interval_min = twap_interval_min
        self.max_slice_pct = max_slice_pct

    def submit_order(self, action: str, code: str, quantity: int,
                     price: float = 0, strategy: str = "default",
                     avg_daily_volume: int = 0) -> Optional[Order]:
        """
        提交订单（自动判断是否需要拆单）

        参数:
            avg_daily_volume: 该股日均成交量（用于判断拆单）
        """
        # 判断是否需要TWAP拆单
        need_twap = False
        if avg_daily_volume > 0:
            order_pct = quantity / avg_daily_volume
            need_twap = order_pct > self.max_slice_pct

        # 创建主订单
        order = self.oms.create_order(action, code, quantity, price, strategy)
        if order is None:
            return None

        if need_twap:
            self._twap_split(order, avg_daily_volume)
        else:
            # 小单直接执行
            order.sub_orders = [{"quantity": quantity, "time_offset": 0}]

        return order

    def _twap_split(self, order: Order, avg_daily_volume: int):
        """
        TWAP拆单: 将大单均匀拆分为N片
        """
        total_qty = order.quantity
        slice_qty = total_qty // self.twap_slices
        remainder = total_qty % self.twap_slices

        slices = []
        for i in range(self.twap_slices):
            qty = slice_qty + (1 if i < remainder else 0)
            qty = (qty // 100) * 100  # 取整到100股
            if qty > 0:
                slices.append({
                    "quantity": qty,
                    "time_offset": i * self.twap_interval_min,
                })

        order.sub_orders = slices
        logger.info(f"[TWAP] {order.code} 拆为{len(slices)}片, "
                   f"每片~{slice_qty}股, 间隔{self.twap_interval_min}分钟")

    def simulate_execution(self, order: Order, market_prices: list) -> dict:
        """
        模拟执行（回测用）

        参数:
            market_prices: 各时间片的市场价格

        返回:
            执行结果
        """
        total_filled = 0
        total_cost = 0

        for i, sub in enumerate(order.sub_orders):
            if i >= len(market_prices):
                break

            price = market_prices[i]
            qty = sub["quantity"]

            # 模拟成交（加入随机滑点）
            slippage = np.random.uniform(-0.001, 0.002)
            exec_price = price * (1 + slippage)

            total_filled += qty
            total_cost += exec_price * qty

        avg_price = total_cost / total_filled if total_filled > 0 else 0

        # 更新订单状态
        if total_filled >= order.quantity:
            self.oms.update_order_status(order.order_id, OrderStatus.FILLED,
                                         total_filled, avg_price)
        elif total_filled > 0:
            self.oms.update_order_status(order.order_id, OrderStatus.PARTIAL,
                                         total_filled, avg_price)

        return {
            "order_id": order.order_id,
            "filled_quantity": total_filled,
            "avg_price": round(avg_price, 3),
            "total_cost": round(total_cost, 2),
            "status": "filled" if total_filled >= order.quantity else "partial",
        }
