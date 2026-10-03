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

# FIX: 双模式导入，支持 backtest 与 trading_system.backtest 两种包路径
try:
    from .engine import BacktestEngineV2
    from .data_feed import DataFeed
    from . import master_common
    from backtest_optimizer import backtest_stock_v6
except ImportError:
    from backtest.engine import BacktestEngineV2
    from backtest.data_feed import DataFeed
    from backtest import master_common
    from backtest_optimizer import backtest_stock_v6

logger = logging.getLogger(__name__)


# ============================================================
# Walk-Forward 分析器
# ============================================================

class WalkForwardAnalyzer:
    """
    Walk-Forward 滚动窗口分析 V2.0
    
    V2.0升级（解决过拟合2886.7%问题）:
      - 训练窗口从60天扩大至120天（覆盖完整市场周期）
      - 验证窗口从20天扩大至40天（增加统计显著性）
      - 使用V5.0逐股回测替代事件驱动引擎（解决窗口内无交易问题）
      - 参数网格精简为2-3个核心参数（降低过拟合维度）
      - 过拟合公式修正（避免分母接近0导致虚高）
    
    用法:
        wf = WalkForwardAnalyzer(
            train_days=120, test_days=40,
            param_grid={"initial_stop_loss": [0.06, 0.07, 0.08]}
        )
        result = wf.run(data_dict, stock_codes)
    """

    def __init__(self, train_days: int = None, test_days: int = None,
                 param_grid: dict = None, initial_capital: float = None,
                 max_windows: int = None):
        """
        参数:
            train_days: 训练窗口天数（V6.1: 默认从 config.WF_CONFIG 读取 200天）
            test_days: 验证窗口天数（V6.1: 默认从 config.WF_CONFIG 读取 60天）
            param_grid: 参数网格 {"参数名": [值1, 值2, ...]}
            initial_capital: 初始资金
            max_windows: 最大窗口数（限制运行时间）
        """
        wf_cfg = getattr(config, 'WF_CONFIG', {})
        self.train_days = train_days or wf_cfg.get('train_days', 200)
        self.test_days = test_days or wf_cfg.get('test_days', 60)
        self.max_windows = max_windows or wf_cfg.get('max_windows', 20)
        # P1-6: 精简参数网格，仅2个核心参数×2-3值=6组合（原27组合过拟合维度太高）
        # min_signal_quality对齐V6.1新门槛65，取60/65/70
        self.param_grid = param_grid or wf_cfg.get('param_grid', {
            "initial_stop_loss": [0.06, 0.08],
            "min_signal_quality": [60, 65, 70],
        })
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
        """运行单个窗口：训练集优化 + 验证集评估（V2.0使用V5.0逐股回测）"""

        # Phase 2: 计算训练集 regime 调整——震荡/熊市自动提高信号质量门槛
        regime_adj = self._calc_regime_adj(data_dict, train_end)

        # 1. 训练集参数优化（V2.0: 使用V5.0逐股回测）
        best_params, train_report = self._optimize_on_train_v5(
            data_dict, stock_codes, train_start, train_end, regime_adj
        )

        if best_params is None:
            return None

        # 2. 验证集评估（用训练集最优参数 + 同一 regime 调整）
        test_report = self._evaluate_on_test_v5(
            data_dict, stock_codes, test_start, test_end, best_params, regime_adj
        )

        if test_report is None:
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

    def _optimize_on_train_v5(self, data_dict: dict, stock_codes: list,
                              start_date: str, end_date: str,
                              regime_adj: float = 0.0) -> tuple:
        """
        V2.0: 在训练集上用V5.0逐股回测找最优参数
        
        改进原因：事件驱动引擎在60天窗口内几乎无交易（3.5年仅1笔），
        导致训练集夏普虚高/验证集为0，过拟合程度2886.7%。
        V5.0逐股回测能产生足够交易样本（每窗口约50-100笔）。
        """
        keys = list(self.param_grid.keys())
        values = list(self.param_grid.values())
        combinations = list(product(*values))

        # V7(A3): 网格采样（避免240+组合全量遍历）
        import config as _cfg
        _max_trials = getattr(_cfg, 'WF_CONFIG', {}).get('grid_max_trials', 0)
        if not _max_trials:
            _max_trials = getattr(getattr(_cfg, 'BACKTEST_SAMPLE', {}), 'get', lambda *a: 0)('full', 'grid_max_trials')
        if not _max_trials or _max_trials <= 0:
            _max_trials = len(combinations)  # 无限制时全量遍历
        if len(combinations) > _max_trials:
            rng = np.random.default_rng(42)
            indices = rng.choice(len(combinations), _max_trials, replace=False)
            combinations = [combinations[i] for i in sorted(indices)]
            logger.debug(f"WF网格采样: {len(combinations)}/{_max_trials} 组合")

        best_score = -999
        best_params = None
        best_report = None

        for combo in combinations:
            params = dict(zip(keys, combo))
            try:
                report = self._run_v5_backtest(
                    data_dict, stock_codes, start_date, end_date, params,
                    regime_adj=regime_adj,
                )
                # V7(A3): 增强目标函数 = sharpe_proxy × sqrt(n_trades)
                # sharpe_proxy = expectancy / std(trade_returns) 的简化版
                expectancy = report.get("expectancy", 0)
                n_trades = report.get("total_trades", 0)
                win_rate = report.get("win_rate", 0) / 100.0
                # 综合评分：期望收益 × 样本量 × 胜率调整
                score = expectancy * np.sqrt(max(n_trades, 1)) * (0.5 + 0.5 * win_rate)

                if score > best_score:
                    best_score = score
                    best_params = params.copy()
                    best_report = report
            except Exception as e:
                logger.debug(f"参数组合 {params} V5.0回测失败: {e}")

        return best_params, best_report

    def _evaluate_on_test_v5(self, data_dict: dict, stock_codes: list,
                             start_date: str, end_date: str, params: dict,
                             regime_adj: float = 0.0) -> Optional[dict]:
        """V2.0: 在验证集上用V5.0逐股回测评估"""
        try:
            return self._run_v5_backtest(
                data_dict, stock_codes, start_date, end_date, params,
                regime_adj=regime_adj,
            )
        except Exception as e:
            logger.warning(f"验证集V5.0回测失败: {e}")
            return None

    def _run_v5_backtest(self, data_dict: dict, stock_codes: list,
                         start_date: str, end_date: str, params: dict,
                         regime_adj: float = 0.0) -> dict:
        """
        V2.0核心：调用backtest_real.py的V5.0逐股回测
        
        参数通过params传入，不修改config.py（遵守约束）
        Phase 2: regime_adj 为训练集 regime 检测的信号质量调整量，
        震荡/熊市时提高 min_signal_quality 门槛。
        """
        
        all_trades = []
        for code in stock_codes:
            if code not in data_dict or code == "000300":
                continue
            df = data_dict[code]
            # 截取窗口内数据（含前置预热期）
            mask = df["date"] <= end_date
            df_window = df[mask].copy()
            if len(df_window) < 80:
                continue
            
            try:
                # FIX(G2): v5/v6 记录成交时硬取 info["行业"]，而 get_stock_info 返回的
                # 是"赛道"键 → 未映射会让每笔完成的交易抛 KeyError，被下方 except
                # 吞掉，导致样本外交易恒为0、oos_sharpe=0（对齐 stage1_single 口径）。
                raw = config.get_stock_info(code) or {}
                info = {"名称": raw.get("名称", code), "类型": raw.get("类型", "龙头"),
                        "行业": raw.get("行业", raw.get("赛道", "其他"))}
                # FIX(G2-P0): 改用 v6 引擎，真正将训练集优化的 params 传入回测，
                # 而非像之前调 v5 那样 params 被静默丢弃。
                # Phase 2: regime 调整——震荡/熊市提高信号质量门槛
                adj_params = params.copy()
                if regime_adj != 0 and "min_signal_quality" in adj_params:
                    adj_params["min_signal_quality"] = min(
                        adj_params["min_signal_quality"] + regime_adj, 85)
                trades = backtest_stock_v6(df_window, code, info, adj_params)
                # 过滤日期范围内的交易
                for t in trades:
                    if t.get("sell_date", "") >= start_date and t.get("sell_date", "") <= end_date:
                        all_trades.append(t)
            except Exception:
                continue
        
        # 统计
        if not all_trades:
            return {"total_trades": 0, "win_rate": 0, "expectancy": 0,
                    "sharpe_ratio": 0, "total_return": 0, "max_drawdown": 0}
        
        # FIX(G2): v5/v6 交易记录用 net_profit（含成本，百分比），并无 profit_pct 键；
        # 旧代码读 profit_pct 恒取默认0，会把胜率/期望/夏普全算成0。改用 net_profit。
        wins = [t for t in all_trades if t.get("net_profit", 0) > 0]
        losses = [t for t in all_trades if t.get("net_profit", 0) <= 0]
        n = len(all_trades)
        win_rate = len(wins) / n * 100 if n > 0 else 0
        avg_win = np.mean([t.get("net_profit", 0) for t in wins]) if wins else 0
        avg_loss = np.mean([t.get("net_profit", 0) for t in losses]) if losses else 0
        expectancy = (win_rate/100 * avg_win + (1-win_rate/100) * avg_loss)
        
        # FIX(G2-P1): 夏普估计器升级——改用 master_common 的年化夏普代理，
        # 基于逐笔收益率 + sqrt(252/avg_hold) 年化，而非逐笔 mean/std*sqrt(n)。
        # 后者在交易笔数少时噪声极大（oos_sharpe_std=9.31 >> mean=-2.39）。
        trade_rets = master_common.trades_to_return_series(all_trades)
        avg_hold = float(np.mean([t.get("hold_days", 5) for t in all_trades])) if all_trades else 5.0
        sharpe = master_common.sharpe_from_trade_returns(trade_rets, avg_hold)
        
        returns = [t.get("net_profit", 0) for t in all_trades]
        return {
            "total_trades": n,
            "win_rate": round(win_rate, 1),
            "expectancy": round(expectancy, 3),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "sharpe_ratio": round(sharpe, 2),
            "total_return": round(sum(returns), 2),
            "max_drawdown": round(min(returns) if returns else 0, 2),
        }

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

        # V2.0: 过拟合检测（修正公式，避免分母接近0导致2886.7%虚高）
        train_sharpes = [r["train_sharpe"] for r in results]
        mean_train = np.mean(train_sharpes)
        mean_oos = np.mean(oos_sharpes)
        # 修正：分母取max(|train|, |oos|, 1.0)，避免极小分母
        denominator = max(abs(mean_train), abs(mean_oos), 1.0)
        overfit_ratio = (mean_train - mean_oos) / denominator

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
        """综合判定（V2.0: 调整阈值适配修正后的过拟合公式）"""
        if overfit_ratio > 1.0:
            return "⚠️ 严重过拟合风险：训练集表现远优于验证集"
        if overfit_ratio > 0.5:
            return "⚠️ 轻度过拟合：建议简化参数或扩大训练窗口"
        if stability < 0.5:
            return "⚠️ 参数不稳定：各窗口最优参数差异大"
        if oos_sharpe < 0:
            return "⚠️ 样本外亏损：策略泛化能力不足"
        if oos_sharpe < 0.5:
            return "⚠️ 样本外表现一般：夏普<0.5"
        if oos_sharpe > 1.5 and stability > 0.7:
            return "✅ 策略稳健：样本外表现优秀且参数稳定"
        return "✅ 策略可用：样本外表现合格"

    @staticmethod
    def _calc_regime_adj(data_dict: dict, train_end: str) -> float:
        """
        Phase 2: 基于训练集基准指数趋势计算 regime 调整量。
        
        利用 000300 基准数据的 20 日收益率判断市场状态，
        对 min_signal_quality 参数施加调整：
          - BULL (ret20 > +2%): -5（放宽门槛，趋势市多参与）
          - BEAR (ret20 < -2%): +10（收紧门槛，熊市少参与）
          - RANGE (其他): +5（震荡市适度收紧）
        
        仅使用 train_end 及之前的数据，无未来信息泄露。
        """
        bench = data_dict.get("000300")
        if bench is None or len(bench) < 30:
            return 0.0
        try:
            mask = bench["date"] <= train_end
            closes = bench.loc[mask, "close"].values
            if len(closes) < 25:
                return 0.0
            ret20 = (closes[-1] / closes[-20] - 1) if closes[-20] > 0 else 0
            if ret20 > 0.02:
                return -5.0   # BULL: 放宽
            elif ret20 < -0.02:
                return 10.0   # BEAR: 收紧
            else:
                return 5.0    # RANGE: 适度收紧
        except Exception:
            return 0.0


# ============================================================
# 便捷接口
# ============================================================

def run_walk_forward(data_dict: dict, stock_codes: list = None,
                     train_days: int = None, test_days: int = None,
                     param_grid: dict = None) -> dict:
    """
    便捷Walk-Forward接口（V6.1: 默认从 config.WF_CONFIG 读取）
    
    返回:
        Walk-Forward分析结果
    """
    wf = WalkForwardAnalyzer(train_days, test_days, param_grid)
    return wf.run(data_dict, stock_codes)


def format_walk_forward_report(result: dict, train_days: int = 200, test_days: int = 60) -> str:
    """格式化Walk-Forward报告"""
    if "error" in result:
        return f"Walk-Forward失败: {result['error']}"

    lines = [
        "=" * 60,
        "  Walk-Forward 滚动窗口分析报告",
        "=" * 60,
        f"  窗口数量:     {result['num_windows']}",
        f"  训练/验证:    {train_days}天/{test_days}天",
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
