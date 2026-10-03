"""
模拟撮合引擎
==============
模拟A股真实交易环境：
- 滑点模型：买入+0.1%，卖出-0.1%
- 手续费：万2.5佣金（双向）+ 千1印花税（仅卖出）
- T+1限制：当日买入次日才能卖出
- 涨跌停限制：涨停无法买入，跌停无法卖出
- 最小交易单位：100股（1手）
- V10 新增:
  - 成交量参与率上限（默认25%）
  - 公司行动事件（停牌/退市/除权除息）
  - signal模式（零摩擦快速验证）
  - 撮合价可选 next_open / next_vwap
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from ..utils.board_rules import is_limit_up as _board_is_limit_up, is_limit_down as _board_is_limit_down
except ImportError:
    from utils.board_rules import is_limit_up as _board_is_limit_up, is_limit_down as _board_is_limit_down

logger = logging.getLogger(__name__)


# ============================================================
# V10: 撮合模式与拒单原因枚举
# ============================================================
class MatchMode(Enum):
    """撮合模式"""
    REALISTIC = "realistic"   # 真实撮合（含滑点/费用/参与率）
    SIGNAL = "signal"         # signal模式（零摩擦，信号价直接成交）


class RejectReason(Enum):
    """拒单原因枚举"""
    LIMIT_UP = "LIMIT_UP"                 # 涨停无法买入
    LIMIT_DOWN = "LIMIT_DOWN"             # 跌停无法卖出
    SUSPENDED = "SUSPENDED"               # 停牌
    DELISTED = "DELISTED"                 # 退市
    PARTICIPATION_LIMIT = "PARTICIPATION_LIMIT"  # 超参与率上限
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"      # 资金不足
    NO_POSITION = "NO_POSITION"                  # 无持仓
    T1_RESTRICTION = "T1_RESTRICTION"            # T+1限制
    ZERO_TARGET = "ZERO_TARGET"                  # 目标股数为0


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
    # V3.2: 市场冲击成本（基于订单量/日成交量比例）
    impact_cost_enabled: bool = True   # 是否启用冲击成本
    impact_cost_coeff: float = 0.1    # 冲击系数: impact = coeff * sqrt(order_value / daily_volume)
    # V10: 成交量参与率上限
    max_participation_rate: float = 0.25   # 单笔不超过日成交量25%
    participation_excess_mode: str = "partial"  # "partial"=部分成交, "reject"=整单拒绝


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
    # V4.1 FIX: 跨日加仓时，前几日买入但尚未解冻的可用股数
    # 解决 frozen_date 被覆盖导致旧冻结股错误可用的问题
    prev_day_available: int = 0
    # 历史最高价（用于回落止盈）
    highest_price: float = 0.0
    # 已实现盈亏
    realized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        """需要外部传入当前价格计算"""
        return 0.0

    def available_shares(self, current_date: str) -> int:
        """可卖股数（T+1限制）
        
        V4.1 修复: 支持跨日加仓多批次冻结
        - frozen_date == current_date: 当日有买入，仅 prev_day_available 可卖
        - frozen_date != current_date: 无当日买入，prev_day_available + frozen_shares 均可卖
        """
        if self.frozen_date == current_date:
            return self.prev_day_available
        return self.prev_day_available + self.frozen_shares


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
    transfer_fee: float = 0.0  # 过户费


@dataclass
class RejectedOrder:
    """V10: 拒单记录"""
    code: str
    direction: str
    target_shares: int
    price: float
    date: str
    reject_reason: RejectReason
    detail: str = ""
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
    6. V10: 成交量参与率上限
    7. V10: 公司行动事件（停牌/退市/除权除息）
    8. V10: signal模式（零摩擦）
    9. V10: 撮合价可选 next_open / next_vwap
    """

    def __init__(self, initial_capital: float, cost_config: CostConfig = None,
                 match_mode: MatchMode = MatchMode.REALISTIC,
                 match_price: str = "next_open"):
        """
        参数:
            initial_capital: 初始资金
            cost_config: 交易成本配置
            match_mode: 撮合模式（REALISTIC / SIGNAL）
            match_price: 撮合价来源
                "next_open" = 使用次日开盘价（V10.5默认，消除隐性前视）
                "signal_price" = 使用信号价（向后兼容，不得作为默认）
                "next_vwap" = 使用次日VWAP
        """
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.cost_config = cost_config or DEFAULT_COST
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.rejected_orders: list[RejectedOrder] = []
        self.total_commission = 0.0
        self.total_stamp_tax = 0.0
        self.total_transfer_fee = 0.0
        self.match_mode = match_mode
        self.match_price = match_price
        # V10: 公司行动注册表（延迟导入避免循环依赖）
        self._ca_registry = None
        # V10: signal vs realistic 双模式对照统计
        self._signal_fills: list[Fill] = []  # signal模式下的成交记录

    def reset(self):
        """重置状态"""
        self.cash = self.initial_capital
        self.positions = {}
        self.fills = []
        self.rejected_orders = []
        self.total_commission = 0.0
        self.total_stamp_tax = 0.0
        self.total_transfer_fee = 0.0
        self._signal_fills = []

    def set_ca_registry(self, registry):
        """V10: 设置公司行动注册表
        
        参数:
            registry: CorporateActionRegistry 实例
        """
        self._ca_registry = registry

    def _get_exec_price(self, price: float, market_data: dict = None,
                        direction: str = "buy") -> float:
        """V10: 根据 match_price 配置确定执行价
        
        参数:
            price: 信号价
            market_data: 行情数据（含 open/close/high/low/volume/amount）
            direction: buy/sell
        
        返回:
            实际执行参考价
        """
        if self.match_mode == MatchMode.SIGNAL:
            return price  # signal模式直接用信号价
        
        if self.match_price == "next_open" and market_data:
            ref = market_data.get("open", price)
            if ref > 0:
                return ref
        elif self.match_price == "next_vwap" and market_data:
            vol = market_data.get("volume", 0)
            amt = market_data.get("amount", 0)
            if vol > 0 and amt > 0:
                return amt / vol
        return price  # 默认用信号价

    # ----------------------------------------------------------
    # 价格计算
    # ----------------------------------------------------------
    def _apply_slippage(self, price: float, direction: str,
                        order_value: float = 0, daily_volume: float = 0) -> float:
        """应用滑点 + V3.2市场冲击成本
        
        冲击成本模型: impact = coeff * sqrt(order_value / daily_volume)
        73万资金买入日成交3亿的股票: impact ≈ 0.1*sqrt(100000/300000000) ≈ 0.006%
        73万资金买入日成交3000万的股票: impact ≈ 0.1*sqrt(100000/30000000) ≈ 0.018%
        """
        if direction == "buy":
            slip = self.cost_config.buy_slippage
        else:
            slip = self.cost_config.sell_slippage
        
        # V3.2: 市场冲击成本
        impact = 0.0
        if (self.cost_config.impact_cost_enabled and order_value > 0
                and daily_volume > 0):
            participation = order_value / daily_volume
            impact = self.cost_config.impact_cost_coeff * (participation ** 0.5)
            impact = min(impact, 0.005)  # 上限0.5%
        
        if direction == "buy":
            return price * (1 + slip + impact)
        else:
            return price * (1 - slip - impact)

    def _calc_commission(self, amount: float) -> float:
        """计算佣金"""
        commission = amount * self.cost_config.commission_rate
        return max(commission, self.cost_config.min_commission)

    def _calc_stamp_tax(self, amount: float, direction: str, code: str = "") -> float:
        """计算印花税（仅卖出，ETF/可转债免印花税）"""
        if direction == "sell":
            # V8.2: 统一使用instrument模块判定品种类型
            from utils.instrument import is_etf, is_bond
            if code and (is_etf(code) or is_bond(code)):
                return 0.0
            return amount * self.cost_config.stamp_tax_rate
        return 0.0

    def _calc_transfer_fee(self, amount: float) -> float:
        """计算过户费（买卖双向）"""
        return amount * self.cost_config.transfer_fee_rate

    # ----------------------------------------------------------
    # 涨跌停检查
    # ----------------------------------------------------------
    @staticmethod
    def _is_limit_up(row: dict, code: str = "", name: str = "") -> bool:
        """判断是否涨停（V2: 区分板块 — 主板10%/创业板20%/ST 5%/科创板20%）"""
        if "pre_close" in row and row["pre_close"] > 0:
            if code:
                return _board_is_limit_up(code, row["close"], row["pre_close"], name=name)
            # 无code时回退旧逻辑（兼容无code调用）
            change_pct = (row["close"] - row["pre_close"]) / row["pre_close"]
            return change_pct >= 0.098
        return False

    @staticmethod
    def _is_limit_down(row: dict, code: str = "", name: str = "") -> bool:
        """判断是否跌停（V2: 区分板块）"""
        if "pre_close" in row and row["pre_close"] > 0:
            if code:
                return _board_is_limit_down(code, row["close"], row["pre_close"], name=name)
            change_pct = (row["close"] - row["pre_close"]) / row["pre_close"]
            return change_pct <= -0.098
        return False

    # ----------------------------------------------------------
    # V10: 成交量参与率限制
    # ----------------------------------------------------------
    def _apply_participation_limit(self, target_shares: int, price: float,
                                    market_data: dict, direction: str) -> tuple[int, Optional[RejectReason]]:
        """V10: 成交量参与率上限检查
        
        单笔成交量不得超过当日成交量 × max_participation_rate
        
        返回:
            (allowed_shares, reject_reason_or_None)
            reject_reason 不为 None 表示整单拒绝
        """
        if self.match_mode == MatchMode.SIGNAL:
            return target_shares, None  # signal模式跳过参与率限制
        
        daily_volume = (market_data or {}).get("volume", 0)
        if daily_volume <= 0:
            return target_shares, None  # 无成交量数据时跳过
        
        max_shares_by_participation = int(daily_volume * self.cost_config.max_participation_rate)
        max_shares_by_participation = (max_shares_by_participation // 100) * 100
        
        if target_shares <= max_shares_by_participation:
            return target_shares, None
        
        if self.cost_config.participation_excess_mode == "reject":
            return 0, RejectReason.PARTICIPATION_LIMIT
        
        # partial: 部分成交
        allowed = max(max_shares_by_participation, 100)
        if allowed < target_shares:
            logger.debug(
                f"参与率限制: {direction} {target_shares}股 → {allowed}股 "
                f"(日成交量{daily_volume:.0f} × {self.cost_config.max_participation_rate:.0%})"
            )
        return allowed, None

    # ----------------------------------------------------------
    # V10: 公司行动检查
    # ----------------------------------------------------------
    def _check_corporate_action(self, code: str, date: str,
                                 direction: str) -> Optional[RejectReason]:
        """V10: 检查停牌/退市等公司行动事件
        
        返回:
            None=通过, RejectReason=拒单原因
        """
        if self._ca_registry is None:
            return None
        
        if self._ca_registry.is_delisted(code, date):
            logger.debug(f"{code} {date} 已退市，拒绝{direction}")
            return RejectReason.DELISTED
        
        if self._ca_registry.is_suspended(code, date):
            logger.debug(f"{code} {date} 停牌中，拒绝{direction}")
            return RejectReason.SUSPENDED
        
        return None

    # ----------------------------------------------------------
    # 买入执行
    # ----------------------------------------------------------
    def execute_buy(self, order: Order, market_data: dict = None) -> Optional[Fill]:
        """
        执行买入订单
        
        参数:
            order: 买入订单
            market_data: 当日行情 {"open","close","high","low","volume","pre_close","amount"}
        
        返回:
            Fill 或 None（资金不足/涨停无法买入/停牌/退市/参与率超限）
        """
        # V10: 公司行动检查（停牌/退市）
        ca_reject = self._check_corporate_action(order.code, order.date, "buy")
        if ca_reject is not None:
            self.rejected_orders.append(RejectedOrder(
                code=order.code, direction="buy", target_shares=order.target_shares,
                price=order.price, date=order.date, reject_reason=ca_reject,
                reason=order.reason,
            ))
            return None

        # 涨停检查：涨停板无法买入（V2: 区分板块）
        if market_data and self._is_limit_up(market_data, code=order.code,
                                              name=market_data.get("name", "")):
            logger.debug(f"{order.code} 涨停，无法买入")
            self.rejected_orders.append(RejectedOrder(
                code=order.code, direction="buy", target_shares=order.target_shares,
                price=order.price, date=order.date, reject_reason=RejectReason.LIMIT_UP,
                reason=order.reason,
            ))
            return None

        # V10: 确定执行参考价
        ref_price = self._get_exec_price(order.price, market_data, "buy")

        # V10: signal模式 - 零摩擦直接成交
        if self.match_mode == MatchMode.SIGNAL:
            return self._execute_signal_mode(order, "buy", ref_price)

        # 计算实际成交价（含滑点 + V3.2冲击成本）
        _daily_vol = (market_data or {}).get("amount",
                     (market_data or {}).get("volume", 0) * ref_price)
        _order_val = ref_price * order.target_shares
        exec_price = self._apply_slippage(ref_price, "buy",
                                          order_value=_order_val,
                                          daily_volume=_daily_vol)

        # V10: 成交量参与率限制
        target, participation_reject = self._apply_participation_limit(
            order.target_shares, ref_price, market_data, "buy")
        if participation_reject:
            self.rejected_orders.append(RejectedOrder(
                code=order.code, direction="buy", target_shares=order.target_shares,
                price=order.price, date=order.date, reject_reason=participation_reject,
                reason=order.reason,
            ))
            return None

        # 计算可买股数（100股整数倍）
        # V10.5 OPT-01: 分母一次性纳入佣金+过户费，减少二次兆底重算
        max_shares = int(self.cash / (exec_price * (1 + self.cost_config.commission_rate + self.cost_config.transfer_fee_rate)))
        max_shares = (max_shares // 100) * 100
        target = min(target, max_shares)

        if target <= 0:
            self.rejected_orders.append(RejectedOrder(
                code=order.code, direction="buy", target_shares=order.target_shares,
                price=order.price, date=order.date, reject_reason=RejectReason.INSUFFICIENT_CASH,
                reason=order.reason,
            ))
            return None

        # 计算费用
        amount = exec_price * target
        commission = self._calc_commission(amount)
        transfer_fee = self._calc_transfer_fee(amount)
        total_cost = amount + commission + transfer_fee

        if total_cost > self.cash:
            # 再减少股数
            target = int(self.cash / (exec_price * (1 + self.cost_config.commission_rate + self.cost_config.transfer_fee_rate + 0.001)))
            target = (target // 100) * 100
            if target <= 0:
                self.rejected_orders.append(RejectedOrder(
                    code=order.code, direction="buy", target_shares=order.target_shares,
                    price=order.price, date=order.date, reject_reason=RejectReason.INSUFFICIENT_CASH,
                    reason=order.reason,
                ))
                return None
            amount = exec_price * target
            commission = self._calc_commission(amount)
            transfer_fee = self._calc_transfer_fee(amount)
            total_cost = amount + commission + transfer_fee

        # 扣款
        self.cash -= total_cost
        self.total_commission += commission
        self.total_transfer_fee += transfer_fee

        # 更新持仓（V4.1 FIX: avg_cost 含买入侧费用，确保卖出 PnL 正确）
        if order.code in self.positions:
            pos = self.positions[order.code]
            total_shares = pos.shares + target
            # V4.1 FIX: 买入佣金+过户费计入持仓成本，卖出PnL自动扣除买入侧费用
            buy_side_cost = exec_price * target + commission + transfer_fee
            pos.avg_cost = (pos.avg_cost * pos.shares + buy_side_cost) / total_shares
            pos.shares = total_shares
            # V4.1 FIX: T+1 跨日加仓多批次冻结
            if pos.frozen_date == order.date:
                # 同日加仓：累加冻结，prev_day_available 不变
                pos.frozen_shares += target
            elif pos.frozen_date and pos.frozen_date < order.date:
                # 跨日加仓：旧冻结股转为可用，新买入冻结
                pos.prev_day_available += pos.frozen_shares
                pos.frozen_shares = target
                pos.frozen_date = order.date
            else:
                # 首次有冻结或日期异常：直接设置
                pos.frozen_shares = target
                pos.frozen_date = order.date
        else:
            self.positions[order.code] = Position(
                code=order.code,
                shares=target,
                avg_cost=exec_price + (commission + transfer_fee) / target,
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
            transfer_fee=round(transfer_fee, 4),
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
            Fill 或 None（无持仓/跌停/T+1限制/停牌/退市）
        """
        if order.code not in self.positions:
            self.rejected_orders.append(RejectedOrder(
                code=order.code, direction="sell", target_shares=order.target_shares,
                price=order.price, date=order.date, reject_reason=RejectReason.NO_POSITION,
                reason=order.reason,
            ))
            return None

        pos = self.positions[order.code]

        # V10: 公司行动检查（停牌/退市）
        ca_reject = self._check_corporate_action(order.code, order.date, "sell")
        if ca_reject is not None:
            self.rejected_orders.append(RejectedOrder(
                code=order.code, direction="sell", target_shares=order.target_shares,
                price=order.price, date=order.date, reject_reason=ca_reject,
                reason=order.reason,
            ))
            return None

        # 跌停检查：跌停板无法卖出（V2: 区分板块）
        if market_data and self._is_limit_down(market_data, code=order.code,
                                                name=market_data.get("name", "")):
            logger.debug(f"{order.code} 跌停，无法卖出")
            self.rejected_orders.append(RejectedOrder(
                code=order.code, direction="sell", target_shares=order.target_shares,
                price=order.price, date=order.date, reject_reason=RejectReason.LIMIT_DOWN,
                reason=order.reason,
            ))
            return None

        # T+1检查：可卖股数
        available = pos.available_shares(order.date)
        if available <= 0:
            logger.debug(f"{order.code} T+1限制，无可卖股")
            self.rejected_orders.append(RejectedOrder(
                code=order.code, direction="sell", target_shares=order.target_shares,
                price=order.price, date=order.date, reject_reason=RejectReason.T1_RESTRICTION,
                reason=order.reason,
            ))
            return None

        # 实际卖出股数
        sell_shares = min(order.target_shares, available)
        sell_shares = (sell_shares // 100) * 100
        if sell_shares <= 0:
            sell_shares = available  # 全部卖出

        # V10: 确定执行参考价
        ref_price = self._get_exec_price(order.price, market_data, "sell")

        # V10: signal模式 - 零摩擦直接成交
        if self.match_mode == MatchMode.SIGNAL:
            return self._execute_signal_mode(order, "sell", ref_price)

        # V10: 成交量参与率限制
        target, participation_reject = self._apply_participation_limit(
            sell_shares, ref_price, market_data, "sell")
        if participation_reject:
            self.rejected_orders.append(RejectedOrder(
                code=order.code, direction="sell", target_shares=sell_shares,
                price=order.price, date=order.date, reject_reason=participation_reject,
                reason=order.reason,
            ))
            return None
        sell_shares = target

        # 计算实际成交价（含滑点 + V3.2冲击成本）
        _daily_vol_sell = (market_data or {}).get("amount",
                          (market_data or {}).get("volume", 0) * ref_price)
        _order_val_sell = ref_price * sell_shares
        exec_price = self._apply_slippage(ref_price, "sell",
                                          order_value=_order_val_sell,
                                          daily_volume=_daily_vol_sell)

        # 计算费用
        amount = exec_price * sell_shares
        commission = self._calc_commission(amount)
        stamp_tax = self._calc_stamp_tax(amount, "sell", code=order.code)
        transfer_fee = self._calc_transfer_fee(amount)
        total_cost = commission + stamp_tax + transfer_fee

        # 入账
        self.cash += amount - total_cost
        self.total_commission += commission
        self.total_stamp_tax += stamp_tax
        self.total_transfer_fee += transfer_fee

        # 计算盈亏（V4.1: avg_cost 已含买入侧费用，此处无需重复扣除）
        pnl = (exec_price - pos.avg_cost) * sell_shares - total_cost
        pos.realized_pnl += pnl

        # 更新持仓（V4.1 FIX: 卖出时先扣减 prev_day_available，再扣 frozen_shares）
        if pos.frozen_date == order.date:
            # 当日有买入冻结：只能卖 prev_day_available（已在上面 checked）
            pos.prev_day_available -= sell_shares
        else:
            # 当日无买入：先卖 prev_day_available，不足部分从 frozen_shares 扣
            if sell_shares <= pos.prev_day_available:
                pos.prev_day_available -= sell_shares
            else:
                remaining = sell_shares - pos.prev_day_available
                pos.prev_day_available = 0
                pos.frozen_shares = max(0, pos.frozen_shares - remaining)
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
            transfer_fee=round(transfer_fee, 4),
        )
        self.fills.append(fill)
        return fill

    # ----------------------------------------------------------
    # V10: signal 模式执行（零摩擦）
    # ----------------------------------------------------------
    def _execute_signal_mode(self, order: Order, direction: str,
                              ref_price: float) -> Optional[Fill]:
        """V10: signal模式 - 信号价直接成交，零摩擦
        
        用于快速验证策略逻辑，不代表可成交性。
        输出标注 "signal 模式，不代表可成交性"。
        """
        if direction == "buy":
            target = (order.target_shares // 100) * 100
            if target <= 0:
                return None
            amount = ref_price * target
            # signal模式: 零费用
            fill = Fill(
                code=order.code, direction="buy", shares=target,
                price=round(ref_price, 3), commission=0, stamp_tax=0,
                total_cost=round(amount, 2), date=order.date,
                reason=order.reason, transfer_fee=0,
            )
            self.cash -= amount
            if order.code in self.positions:
                pos = self.positions[order.code]
                total_shares = pos.shares + target
                pos.avg_cost = (pos.avg_cost * pos.shares + amount) / total_shares
                pos.shares = total_shares
                if pos.frozen_date == order.date:
                    pos.frozen_shares += target
                elif pos.frozen_date and pos.frozen_date < order.date:
                    pos.prev_day_available += pos.frozen_shares
                    pos.frozen_shares = target
                    pos.frozen_date = order.date
                else:
                    pos.frozen_shares = target
                    pos.frozen_date = order.date
            else:
                self.positions[order.code] = Position(
                    code=order.code, shares=target, avg_cost=ref_price,
                    buy_date=order.date, frozen_shares=target,
                    frozen_date=order.date, highest_price=ref_price,
                )
            self.fills.append(fill)
            self._signal_fills.append(fill)
            return fill

        elif direction == "sell":
            if order.code not in self.positions:
                return None
            pos = self.positions[order.code]
            available = pos.available_shares(order.date)
            if available <= 0:
                return None
            sell_shares = min(order.target_shares, available)
            sell_shares = (sell_shares // 100) * 100
            if sell_shares <= 0:
                sell_shares = available
            amount = ref_price * sell_shares
            pnl = (ref_price - pos.avg_cost) * sell_shares
            pos.realized_pnl += pnl
            self.cash += amount
            if pos.frozen_date == order.date:
                pos.prev_day_available -= sell_shares
            else:
                if sell_shares <= pos.prev_day_available:
                    pos.prev_day_available -= sell_shares
                else:
                    remaining = sell_shares - pos.prev_day_available
                    pos.prev_day_available = 0
                    pos.frozen_shares = max(0, pos.frozen_shares - remaining)
            pos.shares -= sell_shares
            if pos.shares <= 0:
                del self.positions[order.code]
            fill = Fill(
                code=order.code, direction="sell", shares=sell_shares,
                price=round(ref_price, 3), commission=0, stamp_tax=0,
                total_cost=0, date=order.date,
                reason=order.reason, transfer_fee=0,
            )
            self.fills.append(fill)
            self._signal_fills.append(fill)
            return fill

    # ----------------------------------------------------------
    # V10: 除权除息日持仓调整
    # ----------------------------------------------------------
    def handle_ex_rights(self, code: str, date: str):
        """V10: 除权除息日调整持仓成本、股数与历史价格
        
        在 new_day() 中调用，自动检查并处理公司行动事件。
        """
        if self._ca_registry is None:
            return
        if code not in self.positions:
            return
        
        pos = self.positions[code]
        new_shares, new_cost, new_price = self._ca_registry.adjust_for_ex_rights(
            code, date, pos.shares, pos.avg_cost, pos.avg_cost  # 用成本价作为价格参考
        )
        if new_shares != pos.shares:
            pos.shares = new_shares
            pos.avg_cost = new_cost
            logger.info(
                f"[SimBroker] {code} {date} 除权调整: "
                f"股数{pos.shares}→{new_shares}, 成本{pos.avg_cost:.3f}→{new_cost:.3f}"
            )

    # ----------------------------------------------------------
    # V10: 可成交性损耗报告
    # ----------------------------------------------------------
    def get_fillability_loss(self) -> dict:
        """V10: signal模式 vs realistic模式的收益差异
        
        用于回测报告中展示"可成交性损耗"区块。
        
        返回:
            {
                "signal_total_pnl": float,
                "realistic_total_pnl": float,
                "loss_pct": float,  # 损耗百分比
                "rejected_count": int,
                "note": str,
            }
        """
        signal_pnl = sum(
            (f.price - 0) * f.shares if f.direction == "buy" else (f.price - 0) * f.shares
            for f in self._signal_fills
        )
        realistic_pnl = sum(
            (f.price - 0) * f.shares if f.direction == "buy" else (f.price - 0) * f.shares
            for f in self.fills if f not in self._signal_fills
        )
        total_cost_realistic = sum(f.commission + f.stamp_tax + f.transfer_fee for f in self.fills)
        
        loss_pct = 0.0
        if signal_pnl != 0:
            loss_pct = abs(signal_pnl - realistic_pnl) / abs(signal_pnl) * 100
        
        return {
            "signal_fill_count": len(self._signal_fills),
            "realistic_fill_count": len(self.fills),
            "rejected_count": len(self.rejected_orders),
            "total_fees_realistic": round(total_cost_realistic, 2),
            "loss_pct": round(loss_pct, 2),
            "note": "signal 模式，不代表可成交性" if self.match_mode == MatchMode.SIGNAL else "",
        }

    # ----------------------------------------------------------
    # 每日结算
    # ----------------------------------------------------------
    def new_day(self, date: str):
        """新交易日开始，解除前一日的T+1冻结
        
        V4.1 FIX: 旧冻结股转入 prev_day_available 而非直接清零
        支持跨日加仓多批次冻结场景
        
        V10: 同时处理除权除息事件
        """
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            if pos.frozen_shares > 0 and pos.frozen_date and pos.frozen_date < date:
                # 旧冻结股（前几日买入）转为可用
                pos.prev_day_available += pos.frozen_shares
                pos.frozen_shares = 0
                pos.frozen_date = ""
            # V10: 除权除息处理
            self.handle_ex_rights(code, date)

    def update_highest(self, code: str, price):
        """更新持仓最高价（兼容float和dict格式）"""
        if code in self.positions:
            # 兼容price为dict的情况（如{"close": xx}）
            if isinstance(price, dict):
                price = price.get("close", 0)
            if price and price > 0:
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
        return self.total_commission + self.total_stamp_tax + self.total_transfer_fee
