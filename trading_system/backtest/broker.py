"""
模拟撮合引擎
==============
模拟A股真实交易环境：
- 滑点模型：买入+0.1%，卖出-0.1%
- 手续费：万2.5佣金（双向）+ 千1印花税（仅卖出）
- T+1限制：当日买入次日才能卖出
- 涨跌停限制：涨停无法买入，跌停无法卖出
- 最小交易单位：100股（1手）
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 交易成本配置
# ============================================================
@dataclass
class CostConfig:
    """交易成本配置"""
    buy_slippage: float = 0.001       # 买入滑点 0.1%
    sell_slippage: float = 0.001      # 卖出滑点 0.1%
    commission_rate: float = 0.00025  # 佣金费率 万2.5
    min_commission: float = 5.0       # 最低佣金 5元
    stamp_tax_rate: float = 0.001     # 印花税 千1（仅卖出）
    transfer_fee_rate: float = 0.00001  # 过户费 十万分之一


DEFAULT_COST = CostConfig()


# ============================================================
# 持仓数据结构
# ============================================================
@dataclass
class Position:
    """单只股票持仓"""
    code: str
    shares: int = 0
    avg_cost: float = 0.0
    buy_date: str = ""
    # T+1: 当日买入的股数（次日才可卖）
    frozen_shares: int = 0
    frozen_date: str = ""
    # 历史最高价（用于回落止盈）
    highest_price: float = 0.0
    # 已实现盈亏
    realized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        """需要外部传入当前价格计算"""
        return 0.0

    def available_shares(self, current_date: str) -> int:
        """可卖股数（T+1限制）"""
        if self.frozen_date == current_date:
            return self.shares - self.frozen_shares
        return self.shares


# ============================================================
# 订单与成交
# ============================================================
@dataclass
class Order:
    """订单"""
    code: str
    direction: str  # "buy" or "sell"
    target_shares: int
    price: float  # 参考价格（信号价）
    date: str
    reason: str = ""


@dataclass
class Fill:
    """成交记录"""
    code: str
    direction: str
    shares: int
    price: float          # 实际成交价（含滑点）
    commission: float     # 佣金
    stamp_tax: float      # 印花税
    total_cost: float     # 总费用（含手续费）
    date: str
    reason: str = ""


# ============================================================
# 模拟撮合引擎
# ============================================================
class SimBroker:
    """
    模拟撮合引擎
    
    职责：
    1. 计算含滑点的实际成交价
    2. 计算手续费（佣金+印花税+过户费）
    3. 执行T+1限制检查
    4. 检查涨跌停限制
    5. 管理持仓与资金
    """

    def __init__(self, initial_capital: float, cost_config: CostConfig = None):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.cost_config = cost_config or DEFAULT_COST
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.total_commission = 0.0
        self.total_stamp_tax = 0.0

    def reset(self):
        """重置状态"""
        self.cash = self.initial_capital
        self.positions = {}
        self.fills = []
        self.total_commission = 0.0
        self.total_stamp_tax = 0.0

    # ----------------------------------------------------------
    # 价格计算
    # ----------------------------------------------------------
    def _apply_slippage(self, price: float, direction: str) -> float:
        """应用滑点"""
        if direction == "buy":
            return price * (1 + self.cost_config.buy_slippage)
        else:
            return price * (1 - self.cost_config.sell_slippage)

    def _calc_commission(self, amount: float) -> float:
        """计算佣金"""
        commission = amount * self.cost_config.commission_rate
        return max(commission, self.cost_config.min_commission)

    def _calc_stamp_tax(self, amount: float, direction: str) -> float:
        """计算印花税（仅卖出）"""
        if direction == "sell":
            return amount * self.cost_config.stamp_tax_rate
        return 0.0

    # ----------------------------------------------------------
    # 涨跌停检查
    # ----------------------------------------------------------
    @staticmethod
    def _is_limit_up(row: dict) -> bool:
        """判断是否涨停（涨幅>=9.8%视为涨停）"""
        if "pre_close" in row and row["pre_close"] > 0:
            change_pct = (row["close"] - row["pre_close"]) / row["pre_close"]
            return change_pct >= 0.098
        return False

    @staticmethod
    def _is_limit_down(row: dict) -> bool:
        """判断是否跌停"""
        if "pre_close" in row and row["pre_close"] > 0:
            change_pct = (row["close"] - row["pre_close"]) / row["pre_close"]
            return change_pct <= -0.098
        return False

    # ----------------------------------------------------------
    # 买入执行
    # ----------------------------------------------------------
    def execute_buy(self, order: Order, market_data: dict = None) -> Optional[Fill]:
        """
        执行买入订单
        
        参数:
            order: 买入订单
            market_data: 当日行情 {"open","close","high","low","volume","pre_close"}
        
        返回:
            Fill 或 None（资金不足/涨停无法买入）
        """
        # 涨停检查：涨停板无法买入
        if market_data and self._is_limit_up(market_data):
            logger.debug(f"{order.code} 涨停，无法买入")
            return None

        # 计算实际成交价（含滑点）
        exec_price = self._apply_slippage(order.price, "buy")

        # 计算可买股数（100股整数倍）
        max_shares = int(self.cash / (exec_price * (1 + self.cost_config.commission_rate)))
        max_shares = (max_shares // 100) * 100
        target = min(order.target_shares, max_shares)

        if target <= 0:
            return None

        # 计算费用
        amount = exec_price * target
        commission = self._calc_commission(amount)
        total_cost = amount + commission

        if total_cost > self.cash:
            # 再减少股数
            target = int(self.cash / (exec_price * (1 + self.cost_config.commission_rate + 0.001)))
            target = (target // 100) * 100
            if target <= 0:
                return None
            amount = exec_price * target
            commission = self._calc_commission(amount)
            total_cost = amount + commission

        # 扣款
        self.cash -= total_cost
        self.total_commission += commission

        # 更新持仓
        if order.code in self.positions:
            pos = self.positions[order.code]
            total_shares = pos.shares + target
            pos.avg_cost = (pos.avg_cost * pos.shares + exec_price * target) / total_shares
            pos.shares = total_shares
            # T+1: 新买入的冻结
            pos.frozen_shares += target
            pos.frozen_date = order.date
        else:
            self.positions[order.code] = Position(
                code=order.code,
                shares=target,
                avg_cost=exec_price,
                buy_date=order.date,
                frozen_shares=target,
                frozen_date=order.date,
                highest_price=exec_price,
            )

        fill = Fill(
            code=order.code,
            direction="buy",
            shares=target,
            price=round(exec_price, 3),
            commission=round(commission, 2),
            stamp_tax=0,
            total_cost=round(total_cost, 2),
            date=order.date,
            reason=order.reason,
        )
        self.fills.append(fill)
        return fill

    # ----------------------------------------------------------
    # 卖出执行
    # ----------------------------------------------------------
    def execute_sell(self, order: Order, market_data: dict = None) -> Optional[Fill]:
        """
        执行卖出订单
        
        参数:
            order: 卖出订单
            market_data: 当日行情
        
        返回:
            Fill 或 None（无持仓/跌停/T+1限制）
        """
        if order.code not in self.positions:
            return None

        pos = self.positions[order.code]

        # 跌停检查：跌停板无法卖出
        if market_data and self._is_limit_down(market_data):
            logger.debug(f"{order.code} 跌停，无法卖出")
            return None

        # T+1检查：可卖股数
        available = pos.available_shares(order.date)
        if available <= 0:
            logger.debug(f"{order.code} T+1限制，无可卖股")
            return None

        # 实际卖出股数
        sell_shares = min(order.target_shares, available)
        sell_shares = (sell_shares // 100) * 100
        if sell_shares <= 0:
            sell_shares = available  # 全部卖出

        # 计算实际成交价（含滑点）
        exec_price = self._apply_slippage(order.price, "sell")

        # 计算费用
        amount = exec_price * sell_shares
        commission = self._calc_commission(amount)
        stamp_tax = self._calc_stamp_tax(amount, "sell")
        total_cost = commission + stamp_tax

        # 入账
        self.cash += amount - total_cost
        self.total_commission += commission
        self.total_stamp_tax += stamp_tax

        # 计算盈亏
        pnl = (exec_price - pos.avg_cost) * sell_shares - total_cost
        pos.realized_pnl += pnl

        # 更新持仓
        pos.shares -= sell_shares
        if pos.shares <= 0:
            del self.positions[order.code]

        fill = Fill(
            code=order.code,
            direction="sell",
            shares=sell_shares,
            price=round(exec_price, 3),
            commission=round(commission, 2),
            stamp_tax=round(stamp_tax, 2),
            total_cost=round(total_cost, 2),
            date=order.date,
            reason=order.reason,
        )
        self.fills.append(fill)
        return fill

    # ----------------------------------------------------------
    # 每日结算
    # ----------------------------------------------------------
    def new_day(self, date: str):
        """新交易日开始，解除前一日的T+1冻结"""
        for pos in self.positions.values():
            if pos.frozen_date and pos.frozen_date < date:
                pos.frozen_shares = 0
                pos.frozen_date = ""

    def update_highest(self, code: str, price: float):
        """更新持仓最高价"""
        if code in self.positions:
            pos = self.positions[code]
            if price > pos.highest_price:
                pos.highest_price = price

    # ----------------------------------------------------------
    # 查询接口
    # ----------------------------------------------------------
    def get_total_value(self, price_dict: dict) -> float:
        """计算总资产"""
        total = self.cash
        for code, pos in self.positions.items():
            price = price_dict.get(code, pos.avg_cost)
            total += pos.shares * price
        return total

    def get_position(self, code: str) -> Optional[Position]:
        """获取持仓"""
        return self.positions.get(code)

    def get_holding_dict(self, code: str) -> Optional[dict]:
        """获取持仓字典格式（兼容策略接口）"""
        pos = self.positions.get(code)
        if pos is None:
            return None
        return {
            "code": code,
            "shares": pos.shares,
            "buy_price": pos.avg_cost,
            "buy_date": pos.buy_date,
            "highest_price": pos.highest_price,
        }

    @property
    def total_cost_paid(self) -> float:
        """总交易成本"""
        return self.total_commission + self.total_stamp_tax
