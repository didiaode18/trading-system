"""
Walk-Forward 滚动窗口分析
==========================
防止策略过拟合的核心工具：
- 将数据分为多个"训练窗口+验证窗口"
- 在每个训练窗口上优化参数
- 在紧随的验证窗口上评估表现
- 汇总所有验证窗口的表现 = 样本外真实表现

流程:
  |---训练60天---|--验证20天--|
       |---训练60天---|--验证20天--|
            |---训练60天---|--验证20天--|
"""

import logging
import pandas as pd
import numpy as np
from itertools import product
from typing import Callable, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from backtest.engine import BacktestEngineV2
from backtest.data_feed import DataFeed

logger = logging.getLogger(__name__)


# ============================================================
# Walk-Forward 分析器
# ============================================================

class WalkForwardAnalyzer:
    """
    Walk-Forward 滚动窗口分析
    
    用法:
        wf = WalkForwardAnalyzer(
            train_days=60, test_days=20,
            param_grid={"MA_SHORT": [15, 20, 25]}
        )
        result = wf.run(data_dict, stock_codes)
    """

    def __init__(self, train_days: int = 60, test_days: int = 20,
                 param_grid: dict = None, initial_capital: float = None,
                 max_windows: int = None):
        """
        参数:
            train_days: 训练窗口天数
            test_days: 验证窗口天数
            param_grid: 参数网格 {"参数名": [值1, 值2, ...]}
            initial_capital: 初始资金
            max_windows: 最大窗口数（限制运行时间）
        """
        self.train_days = train_days
        self.test_days = test_days
        self.max_windows = max_windows
        self.param_grid = param_grid or {
            "MA_SHORT": [15, 20, 25],
            "VOLUME_SHRINK_RATIO": [0.25, 0.30, 0.35],
            "INITIAL_STOP_LOSS_PCT": [0.08, 0.10, 0.12],
        }
        self.initial_capital = initial_capital or config.TOTAL_CAPITAL

    def run(self, data_dict: dict, stock_codes: list = None) -> dict:
        """
        执行Walk-Forward分析
        
        返回:
            {
                "windows": [...],  # 每个窗口的结果
                "oos_sharpe": float,  # 样本外平均夏普
                "oos_return": float,  # 样本外平均收益
                "best_params_history": [...],  # 每个窗口最优参数
                "stability_score": float,  # 参数稳定性评分
            }
        """
        if stock_codes is None:
            stock_codes = list(config.STOCK_POOL.keys())

        # 获取所有交易日
        feed = DataFeed(data_dict)
        all_dates = feed.trading_dates

        if len(all_dates) < self.train_days + self.test_days:
            return {"error": f"数据不足，需要至少{self.train_days + self.test_days}个交易日"}

        # 生成滚动窗口
        windows = self._generate_windows(all_dates)
        logger.info(f"Walk-Forward: {len(windows)}个窗口, "
                   f"训练{self.train_days}天 + 验证{self.test_days}天")

        # 逐窗口分析
        results = []
        for i, (train_start, train_end, test_start, test_end) in enumerate(windows):
            logger.info(f"  窗口{i+1}/{len(windows)}: "
                       f"训练[{train_start}~{train_end}] 验证[{test_start}~{test_end}]")

            window_result = self._run_window(
                data_dict, stock_codes,
                train_start, train_end, test_start, test_end
            )
            if window_result:
                results.append(window_result)

        if not results:
            return {"error": "所有窗口均失败"}

        # 汇总
        return self._summarize(results)

    def _generate_windows(self, all_dates: list) -> list:
        """生成滚动窗口 [(train_start, train_end, test_start, test_end), ...]"""
        windows = []
        total = len(all_dates)
        step = self.test_days  # 每次前进一个验证窗口

        start = 0
        while start + self.train_days + self.test_days <= total:
            train_start = all_dates[start]
            train_end = all_dates[start + self.train_days - 1]
            test_start = all_dates[start + self.train_days]
            test_end_idx = min(start + self.train_days + self.test_days - 1, total - 1)
            test_end = all_dates[test_end_idx]

            windows.append((train_start, train_end, test_start, test_end))
            start += step

            # 限制最大窗口数
            if self.max_windows and len(windows) >= self.max_windows:
                break

        return windows

    def _run_window(self, data_dict: dict, stock_codes: list,
                    train_start: str, train_end: str,
                    test_start: str, test_end: str) -> Optional[dict]:
        """运行单个窗口：训练集优化 + 验证集评估"""

        # 1. 训练集参数优化
        best_params, train_report = self._optimize_on_train(
            data_dict, stock_codes, train_start, train_end
        )

        if best_params is None:
            return None

        # 2. 验证集评估（用训练集最优参数）
        test_report = self._evaluate_on_test(
            data_dict, stock_codes, test_start, test_end, best_params
        )

        if test_report is None or "error" in test_report:
            return None

        return {
            "train_period": (train_start, train_end),
            "test_period": (test_start, test_end),
            "best_params": best_params,
            "train_sharpe": train_report.get("sharpe_ratio", 0),
            "test_sharpe": test_report.get("sharpe_ratio", 0),
            "test_return": test_report.get("total_return", 0),
            "test_win_rate": test_report.get("win_rate", 0),
            "test_max_dd": test_report.get("max_drawdown", 0),
        }

    def _optimize_on_train(self, data_dict: dict, stock_codes: list,
                           start_date: str, end_date: str) -> tuple:
        """在训练集上找最优参数"""
        keys = list(self.param_grid.keys())
        values = list(self.param_grid.values())
        combinations = list(product(*values))

        best_sharpe = -999
        best_params = None
        best_report = None

        for combo in combinations:
            params = dict(zip(keys, combo))

            # 临时修改config
            original = {}
            for k, v in params.items():
                if hasattr(config, k):
                    original[k] = getattr(config, k)
                    setattr(config, k, v)

            # 回测
            try:
                engine = BacktestEngineV2(self.initial_capital)
                report = engine.run(data_dict, start_date=start_date, end_date=end_date)
                sharpe = report.get("sharpe_ratio", -999)

                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = params.copy()
                    best_report = report
            except Exception as e:
                logger.debug(f"参数组合 {params} 回测失败: {e}")
            finally:
                # 恢复参数
                for k, v in original.items():
                    setattr(config, k, v)

        return best_params, best_report

    def _evaluate_on_test(self, data_dict: dict, stock_codes: list,
                          start_date: str, end_date: str, params: dict) -> Optional[dict]:
        """在验证集上评估"""
        # 应用参数
        original = {}
        for k, v in params.items():
            if hasattr(config, k):
                original[k] = getattr(config, k)
                setattr(config, k, v)

        try:
            engine = BacktestEngineV2(self.initial_capital)
            report = engine.run(data_dict, start_date=start_date, end_date=end_date)
            return report
        except Exception as e:
            logger.warning(f"验证集回测失败: {e}")
            return None
        finally:
            for k, v in original.items():
                setattr(config, k, v)

    def _summarize(self, results: list) -> dict:
        """汇总所有窗口结果"""
        oos_sharpes = [r["test_sharpe"] for r in results]
        oos_returns = [r["test_return"] for r in results]
        oos_win_rates = [r["test_win_rate"] for r in results]
        oos_max_dds = [r["test_max_dd"] for r in results]

        # 参数稳定性：最优参数在各窗口的一致性
        param_keys = list(results[0]["best_params"].keys())
        stability_scores = []
        for key in param_keys:
            values = [r["best_params"][key] for r in results]
            # 众数占比
            from collections import Counter
            most_common = Counter(values).most_common(1)[0]
            stability_scores.append(most_common[1] / len(values))

        avg_stability = np.mean(stability_scores) if stability_scores else 0

        # 过拟合检测：训练集夏普 vs 验证集夏普
        train_sharpes = [r["train_sharpe"] for r in results]
        overfit_ratio = (np.mean(train_sharpes) - np.mean(oos_sharpes)) / max(abs(np.mean(train_sharpes)), 0.01)

        return {
            "windows": results,
            "num_windows": len(results),
            "oos_sharpe": round(np.mean(oos_sharpes), 2),
            "oos_sharpe_std": round(np.std(oos_sharpes), 2),
            "oos_return": round(np.mean(oos_returns), 4),
            "oos_win_rate": round(np.mean(oos_win_rates), 4),
            "oos_max_dd": round(np.mean(oos_max_dds), 4),
            "best_params_history": [r["best_params"] for r in results],
            "stability_score": round(avg_stability, 2),
            "overfit_ratio": round(overfit_ratio, 4),
            "verdict": self._judge(overfit_ratio, avg_stability, np.mean(oos_sharpes)),
        }

    @staticmethod
    def _judge(overfit_ratio: float, stability: float, oos_sharpe: float) -> str:
        """综合判定"""
        if overfit_ratio > 0.5:
            return "⚠️ 严重过拟合风险：训练集表现远优于验证集"
        if stability < 0.5:
            return "⚠️ 参数不稳定：各窗口最优参数差异大"
        if oos_sharpe < 0.5:
            return "⚠️ 样本外表现一般：夏普<0.5"
        if oos_sharpe > 1.5 and stability > 0.7:
            return "✅ 策略稳健：样本外表现优秀且参数稳定"
        return "✅ 策略可用：样本外表现合格"


# ============================================================
# 便捷接口
# ============================================================

def run_walk_forward(data_dict: dict, stock_codes: list = None,
                     train_days: int = 60, test_days: int = 20,
                     param_grid: dict = None) -> dict:
    """
    便捷Walk-Forward接口
    
    返回:
        Walk-Forward分析结果
    """
    wf = WalkForwardAnalyzer(train_days, test_days, param_grid)
    return wf.run(data_dict, stock_codes)


def format_walk_forward_report(result: dict) -> str:
    """格式化Walk-Forward报告"""
    if "error" in result:
        return f"Walk-Forward失败: {result['error']}"

    lines = [
        "=" * 60,
        "  Walk-Forward 滚动窗口分析报告",
        "=" * 60,
        f"  窗口数量:     {result['num_windows']}",
        f"  训练/验证:    60天/20天",
        "",
        "  ─── 样本外表现 ───",
        f"  平均夏普:     {result['oos_sharpe']:.2f} ± {result['oos_sharpe_std']:.2f}",
        f"  平均收益:     {result['oos_return']:.2%}",
        f"  平均胜率:     {result['oos_win_rate']:.1%}",
        f"  平均回撤:     {result['oos_max_dd']:.2%}",
        "",
        "  ─── 稳健性评估 ───",
        f"  参数稳定性:   {result['stability_score']:.0%}",
        f"  过拟合程度:   {result['overfit_ratio']:.1%}",
        f"  综合判定:     {result['verdict']}",
        "",
        "  ─── 各窗口详情 ───",
    ]

    for i, w in enumerate(result["windows"]):
        lines.append(
            f"  窗口{i+1}: 验证[{w['test_period'][0]}~{w['test_period'][1]}] "
            f"夏普={w['test_sharpe']:.2f} 收益={w['test_return']:.2%} "
            f"参数={w['best_params']}"
        )

    lines.append("=" * 60)
    return "\n".join(lines)
