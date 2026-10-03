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
    """
    蒙特卡洛压力测试

    职责:
      通过随机模拟评估策略的尾部风险和置信区间。
      包括交易顺序随机化、极端场景模拟、最大回撤分布、破产概率。

    核心输入:
      trades: 历史交易列表 [{pnl, pnl_pct, hold_days}, ...]
      initial_capital: 初始资金

    核心输出 (run() 返回 dict):
      return_distribution: 收益率分布（5%/50%/95%分位）
      max_drawdown_distribution: 最大回撤分布
      ruin_probability: 破产概率
      confidence_interval: 收益置信区间

    原理:
      历史回测只展示了一条路径，蒙特卡洛通过重采样生成数千条可能路径，
      揭示“在95%的情况下，最大回撤不超过X%”。
    """

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

        # ---- 1. 交易顺序随机化（向量化加速）----
        n = self.n_simulations
        n_trades_local = len(returns)

        # 批量生成置换矩阵：每行是一次随机打乱
        random_keys = self.rng.random((n, n_trades_local))
        shuffled_matrix = returns[np.argsort(random_keys, axis=1)]

        # 向量化计算资金曲线 (n_simulations x n_trades)
        equity_matrix = np.cumprod(1 + shuffled_matrix, axis=1) * initial_capital

        # 向量化计算逐日最大回撤
        peak_matrix = np.maximum.accumulate(equity_matrix, axis=1)
        dd_matrix = (peak_matrix - equity_matrix) / peak_matrix
        max_drawdowns = np.max(dd_matrix, axis=1)

        # 最终收益
        final_returns = (equity_matrix[:, -1] - initial_capital) / initial_capital

        # 夏普比率（向量化）
        daily_ret = shuffled_matrix
        mean_ret = np.mean(daily_ret, axis=1)
        std_ret = np.std(daily_ret, axis=1)
        avg_hold = np.mean([t.get("hold_days", 5) for t in trades])
        ann_factor = np.sqrt(252 / max(1, avg_hold))
        sharpe_ratios = np.where(std_ret > 0, mean_ret / std_ret * ann_factor, 0)

        # 最大连续亏损（仍需逐行计算，但用 NumPy 加速）
        loss_mask = (shuffled_matrix < 0).astype(int)
        max_consecutive_losses = np.zeros(n, dtype=int)
        for idx in range(n):
            row = loss_mask[idx]
            # 用 diff 法计算最大连续 1 段
            padded = np.concatenate(([0], row, [0]))
            diffs = np.diff(padded)
            starts = np.where(diffs == 1)[0]
            ends = np.where(diffs == -1)[0]
            if len(starts) > 0:
                max_consecutive_losses[idx] = int(np.max(ends - starts))

        # ---- 2. 统计分布 ----
        final_returns = np.array(final_returns)
        max_drawdowns = np.array(max_drawdowns)
        sharpe_ratios = np.array(sharpe_ratios)
        max_consecutive_losses = np.array(max_consecutive_losses)

        # ---- 3. 破产概率（向量化：资金跌破50%）----
        ruin_matrix = np.cumprod(1 + shuffled_matrix, axis=1)
        ruin_hits = np.any(ruin_matrix < 0.5, axis=1)
        ruin_probability = float(np.mean(ruin_hits))

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


# ============================================================
# V3.2: 历史情景压力测试
# ============================================================

# A股历史极端行情情景库
HISTORICAL_SCENARIOS = {
    "2015股灾": {
        "period": ("2015-06-15", "2015-08-26"),
        "index_drop": -0.45,  # 沪深300跌幅
        "duration_days": 50,
        "description": "杠杆牛崩盘，千股跌停连续出现，流动性枟竭",
        "daily_vol": 0.04,  # 日均波动率
    },
    "2018贸易战": {
        "period": ("2018-01-29", "2018-12-28"),
        "index_drop": -0.32,
        "duration_days": 230,
        "description": "全年单边下跌，去杠杆+贸易战双杀",
        "daily_vol": 0.015,
    },
    "2020疫情": {
        "period": ("2020-01-20", "2020-03-23"),
        "index_drop": -0.16,
        "duration_days": 42,
        "description": "疫情黑天鹅，2月3日单日跌-7.9%",
        "daily_vol": 0.025,
    },
    "2022暴跌": {
        "period": ("2022-01-01", "2022-04-27"),
        "index_drop": -0.28,
        "duration_days": 80,
        "description": "俄乌+疫情封控+美联储加息三重打击",
        "daily_vol": 0.02,
    },
    "2024微盘股崩": {
        "period": ("2024-01-02", "2024-02-08"),
        "index_drop": -0.20,
        "duration_days": 28,
        "description": "微盘股流动性危机，小票连续跌停",
        "daily_vol": 0.03,
    },
}


def historical_stress_test(trades: list, initial_capital: float = None,
                           scenarios: dict = None) -> dict:
    """
    V3.2: 历史情景压力测试
    
    原理: 用历史极端行情的统计特征（日均跌幅/波动率/持续天数）
    模拟如果策略在该环境下运行，结果会如何。
    
    参数:
        trades: 历史交易记录 [{"pnl_pct": 0.05}, ...]
        initial_capital: 初始资金
        scenarios: 自定义情景（默认用HISTORICAL_SCENARIOS）
    
    返回:
        {scenario_name: {"final_equity", "max_drawdown", "survival", "detail"}}
    """
    if initial_capital is None:
        initial_capital = getattr(config, 'TOTAL_CAPITAL', 730000)
    if scenarios is None:
        scenarios = HISTORICAL_SCENARIOS
    
    if not trades:
        return {"error": "无交易记录"}
    
    returns = np.array([t.get("pnl_pct", 0) for t in trades])
    avg_win = returns[returns > 0].mean() if (returns > 0).any() else 0.02
    avg_loss = returns[returns < 0].mean() if (returns < 0).any() else -0.05
    win_rate = (returns > 0).sum() / len(returns)
    
    results = {}
    rng = np.random.default_rng(42)
    
    for name, scenario in scenarios.items():
        index_drop = scenario["index_drop"]
        duration = scenario["duration_days"]
        daily_vol = scenario["daily_vol"]
        
        # 模拟该情景下的交易表现
        # 假设: 熊市中胜率下降20%，亏损放大30%
        stress_win_rate = max(win_rate * 0.8, 0.15)
        stress_avg_loss = avg_loss * 1.3
        stress_avg_win = avg_win * 0.7  # 牛市中盈利也缩减
        
        # 模拟duration/5笔交易（平均5天一笔）
        n_trades = max(duration // 5, 5)
        equity = initial_capital
        peak = initial_capital
        max_dd = 0
        
        for _ in range(n_trades):
            if rng.random() < stress_win_rate:
                pnl = abs(rng.normal(stress_avg_win, daily_vol))
            else:
                pnl = -abs(rng.normal(abs(stress_avg_loss), daily_vol))
            equity *= (1 + pnl)
            peak = max(peak, equity)
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)
        
        survival = equity > initial_capital * 0.5  # 资金腰斩视为未存活
        
        results[name] = {
            "final_equity": round(equity, 0),
            "total_return": round((equity - initial_capital) / initial_capital, 4),
            "max_drawdown": round(max_dd, 4),
            "survival": survival,
            "simulated_trades": n_trades,
            "description": scenario["description"],
        }
    
    return results
