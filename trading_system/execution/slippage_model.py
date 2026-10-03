"""
动态滑点与冲击成本模型
======================
替代回测引擎的固定滑点假设，按个股流动性动态计算真实交易成本。

核心公式:
  slippage = base_slip + impact_cost
  base_slip = f(日均成交额, 股价)  — 流动性分层
  impact_cost = coeff * sqrt(order_value / daily_turnover)  — 市场冲击

分层标准 (基于A股实证):
  大盘股 (日均成交>5亿): 单边 0.05%-0.10%
  中盘股 (日均成交1-5亿): 单边 0.10%-0.20%
  小盘股 (日均成交<1亿): 单边 0.30%-0.50%
  微盘股 (日均成交<2000万): 单边 0.50%-1.00%

使用方式:
    from execution.slippage_model import DynamicSlippageModel
    model = DynamicSlippageModel()
    slip = model.estimate(price=10.0, shares=10000, daily_amount=3e8)
    # slip = 0.0008 (0.08%)

接入点:
    1. backtest/broker.py CostConfig → 替换固定滑点
    2. execution/slippage_tracker.py → 增加预测模式
    3. scripts/run_backtest_50w.py → 回测时使用动态滑点
"""

import numpy as np
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SlippageEstimate:
    """滑点估算结果"""
    total_slippage: float      # 总滑点 (小数, 如0.001=0.1%)
    base_slippage: float       # 基础滑点 (流动性分层)
    impact_cost: float         # 冲击成本 (订单量/成交量)
    liquidity_tier: str        # 流动性分层 (large/mid/small/micro)
    daily_amount: float        # 日均成交额
    order_value: float         # 订单金额
    participation_rate: float  # 参与率 (订单/日成交量)


class DynamicSlippageModel:
    """
    动态滑点模型

    参数:
        use_adaptive_base: 是否按流动性自适应基础滑点 (默认True)
        impact_coeff: 冲击成本系数 (默认0.1, 基于A股实证)
        max_slippage: 滑点上限 (默认1%, 防止极端值)
        lookback_days: 日均成交额回溯天数 (默认20)
    """

    # 流动性分层阈值 (日均成交额, 单位: 元)
    TIER_THRESHOLDS = {
        "large": 5e8,     # >5亿
        "mid":   1e8,     # 1-5亿
        "small": 2e7,     # 2000万-1亿
        "micro": 0,       # <2000万
    }

    # 各分层基础滑点范围 (单边, 小数)
    TIER_BASE_SLIPPAGE = {
        "large": (0.0005, 0.0010),   # 0.05%-0.10%
        "mid":   (0.0010, 0.0020),   # 0.10%-0.20%
        "small": (0.0030, 0.0050),   # 0.30%-0.50%
        "micro": (0.0050, 0.0100),   # 0.50%-1.00%
    }

    def __init__(self, use_adaptive_base: bool = True,
                 impact_coeff: float = 0.1,
                 max_slippage: float = 0.01,
                 lookback_days: int = 20):
        self.use_adaptive_base = use_adaptive_base
        self.impact_coeff = impact_coeff
        self.max_slippage = max_slippage
        self.lookback_days = lookback_days

    def classify_liquidity(self, daily_amount: float) -> str:
        """按日均成交额分类流动性层级"""
        if daily_amount >= self.TIER_THRESHOLDS["large"]:
            return "large"
        elif daily_amount >= self.TIER_THRESHOLDS["mid"]:
            return "mid"
        elif daily_amount >= self.TIER_THRESHOLDS["small"]:
            return "small"
        else:
            return "micro"

    def _base_slippage(self, tier: str, price: float) -> float:
        """
        基础滑点: 在分层范围内按股价插值

        高价股 (>50元) 滑点偏向下限 (价差大, 相对比例小)
        低价股 (<10元) 滑点偏向上限 (tick 粒度粗)
        """
        lo, hi = self.TIER_BASE_SLIPPAGE[tier]
        if price <= 0:
            return hi
        # 线性插值: 价格越低, 滑点越大
        if price >= 50:
            weight = 0.0
        elif price <= 5:
            weight = 1.0
        else:
            weight = (50 - price) / 45.0
        return lo + weight * (hi - lo)

    def estimate(self, price: float, shares: int,
                 daily_amount: float = 0, daily_volume: float = 0,
                 direction: str = "buy") -> SlippageEstimate:
        """
        估算单笔交易滑点

        参数:
            price: 委托价格
            shares: 委托股数
            daily_amount: 近20日日均成交额 (元)
            daily_volume: 近20日日均成交量 (股) — 可选, 有则优先用
            direction: "buy" 或 "sell"

        返回:
            SlippageEstimate
        """
        order_value = price * shares

        # 日均成交额: 优先用 daily_volume * price
        if daily_volume > 0 and daily_amount <= 0:
            daily_amount = daily_volume * price

        # 流动性分层
        tier = self.classify_liquidity(daily_amount) if daily_amount > 0 else "mid"

        # 基础滑点
        base = self._base_slippage(tier, price) if self.use_adaptive_base else 0.001

        # 冲击成本: coeff * sqrt(participation_rate)
        impact = 0.0
        participation = 0.0
        if daily_amount > 0 and order_value > 0:
            daily_value = daily_amount
            participation = order_value / daily_value
            impact = self.impact_coeff * np.sqrt(participation)
            impact = min(impact, 0.005)  # 冲击上限 0.5%

        total = min(base + impact, self.max_slippage)

        return SlippageEstimate(
            total_slippage=round(total, 6),
            base_slippage=round(base, 6),
            impact_cost=round(impact, 6),
            liquidity_tier=tier,
            daily_amount=daily_amount,
            order_value=order_value,
            participation_rate=round(participation, 6),
        )

    def estimate_adjusted_price(self, price: float, shares: int,
                                daily_amount: float = 0,
                                direction: str = "buy") -> float:
        """
        返回含滑点的调整价格

        买入: 价格上浮 (实际成交价更高)
        卖出: 价格下浮 (实际成交价更低)
        """
        est = self.estimate(price, shares, daily_amount=daily_amount, direction=direction)
        if direction == "buy":
            return price * (1 + est.total_slippage)
        else:
            return price * (1 - est.total_slippage)

    def batch_estimate(self, orders: list, market_data: dict) -> list:
        """
        批量估算滑点

        参数:
            orders: [{"code": "000001", "price": 10.0, "shares": 1000, "direction": "buy"}, ...]
            market_data: {"000001": {"amount": 3e8, "volume": 30000000}, ...}

        返回:
            [SlippageEstimate, ...]
        """
        results = []
        for order in orders:
            code = order.get("code", "")
            md = market_data.get(code, {})
            daily_amount = md.get("amount", 0)
            daily_volume = md.get("volume", 0)
            est = self.estimate(
                price=order.get("price", 0),
                shares=order.get("shares", 0),
                daily_amount=daily_amount,
                daily_volume=daily_volume,
                direction=order.get("direction", "buy"),
            )
            results.append(est)
        return results


def create_dynamic_cost_config(dynamic_slippage: bool = True, **kwargs):
    """
    创建带动态滑点的 CostConfig

    用于替换回测引擎的固定滑点配置:
        from execution.slippage_model import create_dynamic_cost_config
        cost = create_dynamic_cost_config()
        broker = SimBroker(cash=1000000, cost_config=cost)
    """
    try:
        from backtest.broker import CostConfig
    except ImportError:
        from trading_system.backtest.broker import CostConfig

    if dynamic_slippage:
        # 基础滑点设为0, 由 DynamicSlippageModel 动态计算
        return CostConfig(
            buy_slippage=0.0,       # 基础滑点由模型覆盖
            sell_slippage=0.0,
            impact_cost_enabled=True,
            impact_cost_coeff=kwargs.get("impact_coeff", 0.1),
            **{k: v for k, v in kwargs.items()
               if k in ("commission_rate", "min_commission", "stamp_tax_rate", "transfer_fee_rate")}
        )
    else:
        return CostConfig(**{k: v for k, v in kwargs.items()
                            if k in ("buy_slippage", "sell_slippage", "commission_rate",
                                     "min_commission", "stamp_tax_rate", "transfer_fee_rate",
                                     "impact_cost_enabled", "impact_cost_coeff")})
