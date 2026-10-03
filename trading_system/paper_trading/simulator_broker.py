# -*- coding: utf-8 -*-
"""
模拟券商（simulator_broker）
============================
对 backtest/broker.py 的 SimBroker 做**薄封装**：撮合、滑点、佣金、印花税、
过户费、T+1、涨跌停、参与率全部委托给回测同一套代码，绝不复制第二套费率逻辑，
保证"模拟成交的滑点/费率与 backtest/broker.py 完全一致"。

只额外提供模拟盘所需的命名空间隔离与查询便利，不改变任何计费口径。
"""

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from ..backtest.broker import SimBroker, Order, CostConfig, MatchMode
except ImportError:  # pragma: no cover - 顶层包路径兼容
    from backtest.broker import SimBroker, Order, CostConfig, MatchMode

logger = logging.getLogger(__name__)


class SimulatorBroker:
    """回测 SimBroker 的薄封装（模拟盘专用）。

    所有属性/方法默认透传内部 SimBroker；仅 execute_buy/execute_sell 在成交后
    追加一次成交回填通知（供 tracker 记录），计费逻辑仍完全走 SimBroker。
    """

    def __init__(self, initial_capital: float, cost_config: CostConfig = None,
                 match_mode: MatchMode = MatchMode.REALISTIC,
                 match_price: str = "signal_price", on_fill=None):
        """
        参数:
            initial_capital: 初始资金
            cost_config:     交易成本配置（None=回测默认，费率与回测一致）
            match_mode:      撮合模式（默认 REALISTIC，与实盘一致）
            match_price:     撮合价来源
            on_fill:         成交回调 fn(fill)，供模拟盘 tracker 落库
        """
        self._broker = SimBroker(
            initial_capital=initial_capital,
            cost_config=cost_config,
            match_mode=match_mode,
            match_price=match_price,
        )
        self._on_fill = on_fill

    # ---- 成交（委托内部 broker，追加落库回调）----
    def execute_buy(self, order, market_data=None):
        fill = self._broker.execute_buy(order, market_data)
        if fill is not None and self._on_fill:
            try:
                self._on_fill(fill)
            except Exception as e:
                logger.debug(f"[模拟券商] on_fill 回调异常(降级): {e}")
        return fill

    def execute_sell(self, order, market_data=None):
        fill = self._broker.execute_sell(order, market_data)
        if fill is not None and self._on_fill:
            try:
                self._on_fill(fill)
            except Exception as e:
                logger.debug(f"[模拟券商] on_fill 回调异常(降级): {e}")
        return fill

    # ---- 透传内部 SimBroker 的全部属性与方法 ----
    def __getattr__(self, name):
        # 仅在实例字典未命中时触发（self._broker 已在 __init__ 设置）
        return getattr(self.__dict__["_broker"], name)

    @property
    def inner(self) -> SimBroker:
        """暴露内部 SimBroker，供需要直接访问的场景（对账/校验）。"""
        return self._broker
