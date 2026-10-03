"""
执行模块
========
大单拆分、滑点追踪、执行质量分析、动态滑点模型、算法交易

子模块:
- slippage_tracker.py: 实际滑点追踪与分析
- slippage_model.py: 动态滑点预测模型（按流动性分层）
- order_lifecycle.py: 订单生命周期管理
- behavior_tracker.py: 交易行为分析
- algo_trading.py: V8.1 算法交易执行引擎(TWAP/VWAP拆单)
- trade_ledger.py: 成交流水台账（SQLite，委托→成交全过程留痕）
- fill_callback.py: XtQuant 成交/委托回报回调 + 主动流水查询兜底
- order_reconcile.py: 单笔委托对账（委托量/成交量/均价/滑点）
- daily_reconcile.py: 盘后账实对账（券商持仓/资金 vs 本地 holdings）
"""

from .slippage_model import DynamicSlippageModel
from .algo_trading import AlgoOrderSplitter, AlgoType
from .trade_ledger import TradeLedger
from .fill_callback import FillCallback
from .daily_reconcile import DailyReconciler

__all__ = [
    "DynamicSlippageModel", "AlgoOrderSplitter", "AlgoType",
    "TradeLedger", "FillCallback", "DailyReconciler",
]
