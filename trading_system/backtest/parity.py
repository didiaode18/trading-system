"""
回测/模拟 Parity 对账
======================
V10 P2-2: 同一历史日，paper 回放与 backtest 逐笔对账。

偏差来源分类统计: 行情时点/撮合价/费用。
持续收敛。NautilusTrader "research-to-live parity" 理念的 Python 平价实现。
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ParityDiff:
    """对账差异"""
    date: str
    code: str
    direction: str
    backtest_shares: int = 0
    paper_shares: int = 0
    backtest_price: float = 0.0
    paper_price: float = 0.0
    price_diff_pct: float = 0.0
    fee_diff: float = 0.0
    source: str = ""  # 偏差来源分类


class ParityReconciler:
    """Parity 对账器

    用法:
        reconciler = ParityReconciler()
        diffs = reconciler.reconcile(backtest_broker, paper_broker, date="2026-09-09")
        report = reconciler.report(diffs)
    """

    def reconcile(self, backtest_fills: list, paper_fills: list,
                  date: str = None) -> list[ParityDiff]:
        """逐笔对账

        参数:
            backtest_fills: 回测成交列表
            paper_fills: 模拟盘成交列表
            date: 对账日期

        返回:
            差异列表
        """
        diffs = []

        # 按 (code, direction, date) 配对
        bt_map = {}
        for f in backtest_fills:
            key = (f.get("code", ""), f.get("direction", ""), f.get("date", date))
            bt_map[key] = f

        pp_map = {}
        for f in paper_fills:
            key = (f.get("code", ""), f.get("direction", ""), f.get("date", date))
            pp_map[key] = f

        all_keys = set(bt_map.keys()) | set(pp_map.keys())
        for key in all_keys:
            bt = bt_map.get(key, {})
            pp = pp_map.get(key, {})

            diff = ParityDiff(
                date=key[2] or date or "",
                code=key[0],
                direction=key[1],
                backtest_shares=bt.get("shares", bt.get("qty", 0)),
                paper_shares=pp.get("shares", pp.get("qty", 0)),
                backtest_price=bt.get("price", 0),
                paper_price=pp.get("price", 0),
            )

            # 计算价格偏差
            if diff.backtest_price > 0 and diff.paper_price > 0:
                diff.price_diff_pct = (
                    abs(diff.backtest_price - diff.paper_price)
                    / diff.backtest_price * 100
                )

            # 计算费用偏差
            bt_fees = bt.get("commission", 0) + bt.get("stamp_tax", 0) + bt.get("transfer_fee", 0)
            pp_fees = pp.get("commission", 0) + pp.get("stamp_tax", 0) + pp.get("transfer_fee", 0)
            diff.fee_diff = abs(bt_fees - pp_fees)

            # 分类偏差来源
            if diff.price_diff_pct > 0.01:
                diff.source = "撮合价差异"
            elif diff.backtest_shares != diff.paper_shares:
                diff.source = "参与率/股数差异"
            elif diff.fee_diff > 0.01:
                diff.source = "费用差异"

            if diff.source:
                diffs.append(diff)

        return diffs

    def report(self, diffs: list[ParityDiff]) -> dict:
        """生成对账报告

        返回:
            {
                "total_diffs": int,
                "by_source": {source: count},
                "avg_price_diff_pct": float,
                "total_fee_diff": float,
            }
        """
        by_source = {}
        total_price_diff = 0
        total_fee_diff = 0

        for d in diffs:
            by_source[d.source] = by_source.get(d.source, 0) + 1
            total_price_diff += d.price_diff_pct
            total_fee_diff += d.fee_diff

        n = len(diffs) or 1
        return {
            "total_diffs": len(diffs),
            "by_source": by_source,
            "avg_price_diff_pct": round(total_price_diff / n, 4),
            "total_fee_diff": round(total_fee_diff, 2),
        }
