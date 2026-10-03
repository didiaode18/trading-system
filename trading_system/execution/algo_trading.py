# -*- coding: utf-8 -*-
"""
算法交易执行引擎 V8.1
=====================
为大资金ETF交易提供TWAP/VWAP拆单逻辑，降低冲击成本。

核心功能:
  1. TWAP (Time Weighted Average Price): 时间均匀拆单
  2. VWAP (Volume Weighted Average Price): 按历史成交量分布拆单
  3. 参与率控制: 单笔不超过日成交量的指定比例

使用:
    from execution.algo_trading import AlgoOrderSplitter, AlgoType

    splitter = AlgoOrderSplitter()
    child_orders = splitter.split(
        code="562590", direction="buy", total_shares=100000,
        ref_price=0.94, algo=AlgoType.VWAP,
        daily_volume=5000000, duration_minutes=30,
    )
"""

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class AlgoType(Enum):
    """算法交易策略类型"""
    TWAP = "twap"       # 时间加权均匀拆单
    VWAP = "vwap"       # 成交量加权拆单
    IMMEDIATE = "immediate"  # 立即执行（不拆单）


@dataclass
class ChildOrder:
    """子单"""
    sequence: int           # 子单序号
    shares: int             # 股数（100的整数倍）
    price_limit: float      # 限价（参考价的±滑点）
    time_offset_min: int    # 距开始的偏移（分钟）
    algo_type: str          # 算法类型


@dataclass
class AlgoResult:
    """拆单结果"""
    code: str
    direction: str
    total_shares: int
    algo_type: str
    child_orders: List[ChildOrder] = field(default_factory=list)
    estimated_duration_min: int = 0
    participation_rate: float = 0.0  # 预估参与率（占总成交量比例）
    estimated_impact_cost: float = 0.0  # 预估冲击成本（%）


class AlgoOrderSplitter:
    """算法交易拆单器

    核心逻辑:
      - 当订单金额 < 阈值（默认50万）或参与率 < 5% 时，不拆单（IMMEDIATE）
      - 当参与率 5%-15% 时，TWAP拆为3-5笔
      - 当参与率 > 15% 时，VWAP拆为5-10笔（需要历史成交量分布）
    """

    # 配置参数
    MIN_SPLIT_AMOUNT = 500_000   # 最小拆单金额（50万元以下不拆）
    MIN_PARTICIPATION_SPLIT = 0.05  # 参与率>5%才开始拆单
    MAX_PARTICIPATION_SINGLE = 0.10  # 单笔不超过日成交量10%
    MAX_CHILD_ORDERS = 10        # 最多拆为10笔
    MIN_CHILD_ORDERS = 3         # 最少拆为3笔
    SLIPPAGE_PER_ORDER = 0.001   # 每笔预估滑点0.1%

    def split(
        self,
        code: str,
        direction: str,
        total_shares: int,
        ref_price: float,
        algo: AlgoType = AlgoType.VWAP,
        daily_volume: int = 0,
        duration_minutes: int = 30,
        volume_profile: Optional[List[float]] = None,
    ) -> AlgoResult:
        """
        将大单拆分为多个子单

        参数:
            code: 证券代码
            direction: "buy" 或 "sell"
            total_shares: 总股数
            ref_price: 参考价格
            algo: 算法类型（TWAP/VWAP/IMMEDIATE）
            daily_volume: 日成交量（股）
            duration_minutes: 执行时长（分钟），默认30
            volume_profile: 成交量分布（归一化权重列表），None时使用均匀分布

        返回:
            AlgoResult: 拆单结果
        """
        order_value = total_shares * ref_price

        # 判断是否需要拆单
        if algo == AlgoType.IMMEDIATE or order_value < self.MIN_SPLIT_AMOUNT:
            return self._immediate_order(code, direction, total_shares, ref_price)

        if daily_volume <= 0:
            logger.warning(f"[算法交易] {code} 无日成交量数据，降级为不拆单")
            return self._immediate_order(code, direction, total_shares, ref_price)

        # 计算参与率
        participation = total_shares / daily_volume
        if participation < self.MIN_PARTICIPATION_SPLIT:
            return self._immediate_order(code, direction, total_shares, ref_price)

        # 确定拆分数
        n_orders = self._calc_n_orders(participation)

        # 选择算法
        if algo == AlgoType.TWAP:
            child_orders = self._twap_split(
                code, direction, total_shares, ref_price,
                n_orders, duration_minutes
            )
        elif algo == AlgoType.VWAP:
            child_orders = self._vwap_split(
                code, direction, total_shares, ref_price,
                n_orders, duration_minutes, volume_profile
            )
        else:
            return self._immediate_order(code, direction, total_shares, ref_price)

        # 预估冲击成本: impact = coeff * sqrt(participation)
        impact_coeff = 0.1
        estimated_impact = impact_coeff * math.sqrt(participation)

        result = AlgoResult(
            code=code,
            direction=direction,
            total_shares=total_shares,
            algo_type=algo.value,
            child_orders=child_orders,
            estimated_duration_min=duration_minutes,
            participation_rate=round(participation, 4),
            estimated_impact_cost=round(estimated_impact, 4),
        )

        logger.info(
            f"[算法交易] {code} {direction} {total_shares}股 "
            f"→ {algo.value.upper()}拆为{n_orders}笔, "
            f"参与率{participation:.1%}, 预估冲击{estimated_impact:.2%}"
        )

        return result

    def _immediate_order(self, code, direction, total_shares, ref_price) -> AlgoResult:
        """不拆单，立即执行"""
        child = ChildOrder(
            sequence=1,
            shares=total_shares,
            price_limit=round(ref_price * (1 + self.SLIPPAGE_PER_ORDER if direction == "buy"
                                           else 1 - self.SLIPPAGE_PER_ORDER), 4),
            time_offset_min=0,
            algo_type="immediate",
        )
        return AlgoResult(
            code=code, direction=direction,
            total_shares=total_shares, algo_type="immediate",
            child_orders=[child],
            participation_rate=0.0,
            estimated_impact_cost=0.0,
        )

    def _calc_n_orders(self, participation: float) -> int:
        """根据参与率确定拆分数"""
        if participation < 0.10:
            return self.MIN_CHILD_ORDERS
        elif participation < 0.25:
            return 5
        elif participation < 0.50:
            return 7
        else:
            return self.MAX_CHILD_ORDERS

    def _twap_split(self, code, direction, total_shares, ref_price,
                    n_orders, duration_minutes) -> List[ChildOrder]:
        """TWAP: 时间均匀拆单"""
        # 按100股整数倍分配
        base_shares = (total_shares // n_orders // 100) * 100
        remainder = total_shares - base_shares * n_orders

        orders = []
        time_step = duration_minutes // n_orders if n_orders > 1 else 0

        for i in range(n_orders):
            shares = base_shares + (100 if i < remainder // 100 else 0)
            if shares <= 0:
                continue

            slip = self.SLIPPAGE_PER_ORDER
            price_limit = round(ref_price * (1 + slip if direction == "buy"
                                             else 1 - slip), 4)

            orders.append(ChildOrder(
                sequence=i + 1,
                shares=shares,
                price_limit=price_limit,
                time_offset_min=i * time_step,
                algo_type="twap",
            ))

        return orders

    def _vwap_split(self, code, direction, total_shares, ref_price,
                    n_orders, duration_minutes,
                    volume_profile: Optional[List[float]] = None) -> List[ChildOrder]:
        """VWAP: 按成交量分布拆单

        A股日内成交量分布特征: U型（开盘和收盘量大，中午量小）
        近似模型: [1.5, 1.2, 0.8, 0.7, 0.8, 1.0, 1.3, 1.7] (8段)
        """
        if volume_profile is None:
            # 使用A股典型U型日内成交量分布
            volume_profile = self._default_intraday_profile(n_orders)

        # 归一化
        total_weight = sum(volume_profile[:n_orders])
        if total_weight <= 0:
            return self._twap_split(code, direction, total_shares, ref_price,
                                    n_orders, duration_minutes)

        weights = [w / total_weight for w in volume_profile[:n_orders]]

        # 按权重分配股数（100股整数倍）
        raw_shares = [w * total_shares for w in weights]
        int_shares = [int(s // 100) * 100 for s in raw_shares]

        # 余数分配给权重最大的时段
        remainder = total_shares - sum(int_shares)
        if remainder > 0:
            max_idx = weights.index(max(weights))
            int_shares[max_idx] += int(remainder // 100) * 100

        time_step = duration_minutes // n_orders if n_orders > 1 else 0
        orders = []

        for i in range(n_orders):
            shares = int_shares[i]
            if shares <= 0:
                continue

            # VWAP限价：根据时段微调滑点（开盘/收盘时段滑点更大）
            slip = self.SLIPPAGE_PER_ORDER * (0.8 + 0.4 * weights[i] / max(weights))
            price_limit = round(ref_price * (1 + slip if direction == "buy"
                                             else 1 - slip), 4)

            orders.append(ChildOrder(
                sequence=i + 1,
                shares=shares,
                price_limit=price_limit,
                time_offset_min=i * time_step,
                algo_type="vwap",
            ))

        return orders

    @staticmethod
    def _default_intraday_profile(n_slots: int) -> List[float]:
        """生成A股典型U型日内成交量分布（归一化到n_slots个时段）"""
        # 标准A股U型曲线（30分钟粒度，8段覆盖4小时交易时段）
        _standard = [1.5, 1.2, 0.8, 0.7, 0.8, 1.0, 1.3, 1.7]

        if n_slots <= len(_standard):
            # 合并时段
            step = len(_standard) / n_slots
            profile = []
            for i in range(n_slots):
                start = int(i * step)
                end = int((i + 1) * step)
                profile.append(sum(_standard[start:end]) / max(1, end - start))
            return profile
        else:
            # 扩展时段（线性插值）
            x_old = np.linspace(0, 1, len(_standard))
            x_new = np.linspace(0, 1, n_slots)
            return list(np.interp(x_new, x_old, _standard))

    def should_use_algo(self, code: str, total_shares: int,
                        ref_price: float, daily_volume: int) -> bool:
        """判断是否需要使用算法交易

        条件:
          - ETF品种
          - 订单金额 > 50万
          - 参与率 > 5%
        """
        from utils.instrument import is_etf
        if not is_etf(code):
            return False
        order_value = total_shares * ref_price
        if order_value < self.MIN_SPLIT_AMOUNT:
            return False
        if daily_volume <= 0:
            return False
        participation = total_shares / daily_volume
        return participation >= self.MIN_PARTICIPATION_SPLIT
