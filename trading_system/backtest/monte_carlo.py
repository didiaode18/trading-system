"""
蒙特卡洛压力测试
================
通过随机模拟评估策略的尾部风险和置信区间

核心功能:
  1. 交易顺序随机化：打乱历史交易顺序1000次，计算置信区间
  2. 极端场景模拟：连续止损、单日跌停、流动性枯竭
  3. 最大回撤分布：95%/99%置信度下的最大回撤
  4. 破产概率：资金曲线跌破安全线的概率
  5. 收益置信区间：年化收益的5%/50%/95%分位

原理:
  历史回测只展示了一条路径，但交易顺序是随机的。
  蒙特卡洛通过重采样生成数千条可能路径，
  揭示"在95%的情况下，最大回撤不超过X%"。

使用方式:
    from backtest.monte_carlo import MonteCarloStressTest
    mc = MonteCarloStressTest(n_simulations=1000)
    result = mc.run(trades, initial_capital)
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class MonteCarloStressTest:
    """蒙特卡洛压力测试"""

    def __init__(self, n_simulations: int = 1000, seed: int = 42):
        """
        参数:
            n_simulations: 模拟次数
            seed: 随机种子（可复现）
        """
        self.n_simulations = n_simulations
        self.rng = np.random.default_rng(seed)

    def run(self, trades: list, initial_capital: float = None) -> dict:
        """
        运行蒙特卡洛模拟
        
        参数:
            trades: 历史交易列表 [{"pnl": float, "pnl_pct": float, "hold_days": int}, ...]
            initial_capital: 初始资金
        
        返回:
            {
                "n_simulations": int,
                "n_trades": int,
                "return_distribution": {
                    "mean": float,
                    "p5": float,      # 5%分位（悲观）
                    "p50": float,     # 中位数
                    "p95": float,     # 95%分位（乐观）
                },
                "max_drawdown_distribution": {
                    "mean": float,
                    "p5": float,
                    "p50": float,
                    "p95": float,     # 95%情况下最大回撤不超过此值
                },
                "ruin_probability": float,   # 破产概率
                "consecutive_loss": {
                    "max_expected": int,     # 预期最大连亏次数
                    "p95": int,
                },
                "sharpe_distribution": {
                    "mean": float,
                    "p5": float,
                    "p95": float,
                },
                "stress_scenarios": dict,    # 极端场景
                "confidence_statement": str, # 结论性陈述
            }
        """
        if initial_capital is None:
            initial_capital = config.TOTAL_CAPITAL

        if not trades or len(trades) < 5:
            return {"error": "交易记录不足（至少需要5笔）"}

        # 提取收益率序列
        returns = np.array([t.get("pnl_pct", 0) for t in trades])
        n_trades = len(returns)

        # ---- 1. 交易顺序随机化 ----
        final_returns = []
        max_drawdowns = []
        sharpe_ratios = []
        max_consecutive_losses = []

        for _ in range(self.n_simulations):
            # 随机打乱交易顺序
            shuffled = self.rng.choice(returns, size=n_trades, replace=True)
            
            # 模拟资金曲线
            equity = initial_capital
            peak = equity
            max_dd = 0
            consecutive_loss = 0
            max_consec_loss = 0
            daily_returns = []

            for r in shuffled:
                equity *= (1 + r)
                daily_returns.append(r)

                # 最大回撤
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd

                # 连续亏损
                if r < 0:
                    consecutive_loss += 1
                    max_consec_loss = max(max_consec_loss, consecutive_loss)
                else:
                    consecutive_loss = 0

            final_returns.append((equity - initial_capital) / initial_capital)
            max_drawdowns.append(max_dd)
            max_consecutive_losses.append(max_consec_loss)

            # 夏普比率（简化）
            if np.std(daily_returns) > 0:
                sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252 / max(1, np.mean([t.get("hold_days", 5) for t in trades])))
            else:
                sharpe = 0
            sharpe_ratios.append(sharpe)

        # ---- 2. 统计分布 ----
        final_returns = np.array(final_returns)
        max_drawdowns = np.array(max_drawdowns)
        sharpe_ratios = np.array(sharpe_ratios)
        max_consecutive_losses = np.array(max_consecutive_losses)

        # ---- 3. 破产概率（资金跌破50%）----
        ruin_count = 0
        for _ in range(self.n_simulations):
            shuffled = self.rng.choice(returns, size=n_trades, replace=True)
            equity = initial_capital
            for r in shuffled:
                equity *= (1 + r)
                if equity < initial_capital * 0.5:
                    ruin_count += 1
                    break
        ruin_probability = ruin_count / self.n_simulations

        # ---- 4. 极端场景 ----
        stress = self._run_stress_scenarios(returns, initial_capital)

        # ---- 5. 结论 ----
        dd_95 = np.percentile(max_drawdowns, 95)
        ret_5 = np.percentile(final_returns, 5)
        ret_95 = np.percentile(final_returns, 95)

        confidence_statement = (
            f"基于{n_trades}笔历史交易的{self.n_simulations}次蒙特卡洛模拟:\n"
            f"  • 95%置信度下最大回撤不超过 {dd_95:.1%}\n"
            f"  • 收益区间: [{ret_5:.1%}, {ret_95:.1%}] (5%~95%分位)\n"
            f"  • 破产概率(资金腰斩): {ruin_probability:.2%}\n"
            f"  • 预期最大连亏: {int(np.percentile(max_consecutive_losses, 50))}笔 "
            f"(95%分位: {int(np.percentile(max_consecutive_losses, 95))}笔)"
        )

        return {
            "n_simulations": self.n_simulations,
            "n_trades": n_trades,
            "return_distribution": {
                "mean": round(float(np.mean(final_returns)), 4),
                "p5": round(float(np.percentile(final_returns, 5)), 4),
                "p50": round(float(np.percentile(final_returns, 50)), 4),
                "p95": round(float(np.percentile(final_returns, 95)), 4),
            },
            "max_drawdown_distribution": {
                "mean": round(float(np.mean(max_drawdowns)), 4),
                "p5": round(float(np.percentile(max_drawdowns, 5)), 4),
                "p50": round(float(np.percentile(max_drawdowns, 50)), 4),
                "p95": round(float(np.percentile(max_drawdowns, 95)), 4),
            },
            "ruin_probability": round(ruin_probability, 4),
            "consecutive_loss": {
                "max_expected": int(np.percentile(max_consecutive_losses, 50)),
                "p95": int(np.percentile(max_consecutive_losses, 95)),
            },
            "sharpe_distribution": {
                "mean": round(float(np.mean(sharpe_ratios)), 3),
                "p5": round(float(np.percentile(sharpe_ratios, 5)), 3),
                "p95": round(float(np.percentile(sharpe_ratios, 95)), 3),
            },
            "stress_scenarios": stress,
            "confidence_statement": confidence_statement,
        }

    def _run_stress_scenarios(self, returns: np.ndarray,
                              initial_capital: float) -> dict:
        """极端场景压力测试"""
        scenarios = {}

        # 场景1: 连续N笔止损
        avg_loss = np.mean(returns[returns < 0]) if np.any(returns < 0) else -0.05
        for n_consecutive in [3, 5, 7]:
            equity = initial_capital
            for _ in range(n_consecutive):
                equity *= (1 + avg_loss)
            scenarios[f"连续{n_consecutive}笔止损"] = {
                "final_equity": round(equity, 2),
                "loss_pct": round((equity - initial_capital) / initial_capital, 4),
                "description": f"连续{n_consecutive}笔平均亏损{avg_loss:.1%}",
            }

        # 场景2: 单日跌停无法卖出
        worst_day = np.min(returns) if len(returns) > 0 else -0.10
        scenarios["单日跌停"] = {
            "final_equity": round(initial_capital * (1 + worst_day), 2),
            "loss_pct": round(worst_day, 4),
            "description": f"单日最大亏损{worst_day:.1%}（跌停无法止损）",
        }

        # 场景3: 最差10%交易连续出现
        worst_10pct = np.percentile(returns, 10)
        equity = initial_capital
        for _ in range(5):
            equity *= (1 + worst_10pct)
        scenarios["极端连亏"] = {
            "final_equity": round(equity, 2),
            "loss_pct": round((equity - initial_capital) / initial_capital, 4),
            "description": f"连续5笔最差10%交易(每笔{worst_10pct:.1%})",
        }

        return scenarios
