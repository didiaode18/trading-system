"""
分层回测引擎（Decile / Quintile Backtest）
==========================================
因子有效性检验的黄金标准：
- 按因子值分 10 组（或 5 组），等权持有，定期调仓
- 输出每组年化收益、累积收益、多空收益
- 收益单调性检验（Top 组 > Bottom 组）
- IC / IR / t 值等核心统计指标

使用方式:
    from backtest.decile_backtest import DecileBacktest
    bt = DecileBacktest(factor_df, forward_ret, n_groups=10)
    result = bt.run()
    bt.print_summary()

输入格式:
    factor_df: DataFrame, index=date, columns=stock_codes, values=因子值
    forward_ret: DataFrame, index=date, columns=stock_codes, values=未来N日收益率
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DecileGroupResult:
    """单组回测结果"""
    group: int                    # 组号 (1=最低, 10=最高)
    cum_return: float = 0.0      # 累计收益
    annual_return: float = 0.0   # 年化收益
    max_drawdown: float = 0.0    # 最大回撤
    sharpe: float = 0.0          # 夏普比率
    win_rate: float = 0.0        # 胜率
    avg_turnover: float = 0.0    # 平均换手率
    n_rebalance: int = 0         # 调仓次数
    equity_curve: list = field(default_factory=list)  # 净值曲线


@dataclass
class DecileBacktestResult:
    """分层回测完整结果"""
    n_groups: int = 10
    rebalance_freq: int = 5       # 调仓频率（交易日）
    groups: list = field(default_factory=list)  # List[DecileGroupResult]
    # 多空组合
    long_short_return: float = 0.0
    long_short_sharpe: float = 0.0
    long_short_equity: list = field(default_factory=list)
    # 统计检验
    ic_mean: float = 0.0
    ic_std: float = 0.0
    icir: float = 0.0
    t_stat: float = 0.0
    monotonicity_score: float = 0.0  # 单调性得分 (0-1)
    # 每期截面IC
    ic_series: list = field(default_factory=list)
    # 元信息
    n_stocks: int = 0
    n_dates: int = 0
    start_date: str = ""
    end_date: str = ""


class DecileBacktest:
    """
    分层回测引擎

    参数:
        factor_df: 因子值面板 (index=date, columns=codes)
        forward_ret: 前瞻收益面板 (index=date, columns=codes)
        n_groups: 分组数 (默认10)
        rebalance_freq: 调仓频率 (默认5个交易日)
        min_stocks_per_group: 每组最少股票数 (默认5)
    """

    def __init__(self, factor_df: pd.DataFrame, forward_ret: pd.DataFrame,
                 n_groups: int = 10, rebalance_freq: int = 5,
                 min_stocks_per_group: int = 5):
        self.factor_df = factor_df
        self.forward_ret = forward_ret
        self.n_groups = n_groups
        self.rebalance_freq = rebalance_freq
        self.min_stocks_per_group = min_stocks_per_group
        self.result = None

    def run(self) -> DecileBacktestResult:
        """执行分层回测"""
        # 对齐日期
        common_dates = self.factor_df.index.intersection(self.forward_ret.index)
        common_dates = common_dates.sort_values()
        if len(common_dates) < self.rebalance_freq * 2:
            logger.warning(f"可用日期不足: {len(common_dates)}天, 需至少{self.rebalance_freq * 2}天")
            return DecileBacktestResult()

        factor = self.factor_df.loc[common_dates]
        fwd_ret = self.forward_ret.loc[common_dates]

        # 调仓日序列
        rebalance_dates = common_dates[::self.rebalance_freq]
        if len(rebalance_dates) < 3:
            logger.warning(f"调仓次数不足: {len(rebalance_dates)}次")
            return DecileBacktestResult()

        # 逐调仓日分组
        group_returns = {g: [] for g in range(1, self.n_groups + 1)}
        ic_list = []

        for i, rb_date in enumerate(rebalance_dates[:-1]):
            # 截面因子值
            f截面 = factor.loc[rb_date].dropna()
            # 下一调仓期收益
            next_date = rebalance_dates[i + 1]
            r截面 = fwd_ret.loc[next_date]
            # 对齐
            common = f截面.index.intersection(r截面.dropna().index)
            if len(common) < self.n_groups * self.min_stocks_per_group:
                continue

            f_val = f截面[common]
            r_val = r截面[common]

            # 分组
            try:
                groups = pd.qcut(f_val, q=self.n_groups, labels=False, duplicates="drop")
            except ValueError:
                continue

            # 每组等权平均收益
            actual_groups = groups.nunique()
            for g in range(actual_groups):
                mask = groups == g
                if mask.sum() >= self.min_stocks_per_group:
                    group_returns[g + 1].append(r_val[mask].mean())

            # 截面IC (Rank IC - Spearman)
            from scipy.stats import spearmanr
            try:
                ic, _ = spearmanr(f_val.values, r_val.values)
                if not np.isnan(ic):
                    ic_list.append(ic)
            except Exception:
                pass

        # 汇总各组结果
        result = DecileBacktestResult(
            n_groups=self.n_groups,
            rebalance_freq=self.rebalance_freq,
            n_stocks=len(factor.columns),
            n_dates=len(common_dates),
            start_date=str(common_dates[0])[:10],
            end_date=str(common_dates[-1])[:10],
        )

        equity_global = []
        for g in range(1, self.n_groups + 1):
            rets = group_returns.get(g, [])
            gr = DecileGroupResult(group=g)
            if len(rets) < 2:
                result.groups.append(gr)
                continue

            rets = np.array(rets)
            gr.cum_return = float(np.prod(1 + rets) - 1)
            gr.n_rebalance = len(rets)
            gr.annual_return = float((1 + gr.cum_return) ** (252 / (len(rets) * self.rebalance_freq)) - 1)
            gr.win_rate = float((rets > 0).mean())
            gr.avg_turnover = 0.0  # 简化：等权组合换手需完整持仓记录

            # 净值曲线
            equity = np.cumprod(1 + rets)
            gr.equity_curve = equity.tolist()

            # 最大回撤
            peak = np.maximum.accumulate(equity)
            dd = (peak - equity) / peak
            gr.max_drawdown = float(dd.max())

            # 夏普
            if rets.std() > 1e-12:
                gr.sharpe = float(rets.mean() / rets.std() * np.sqrt(252 / self.rebalance_freq))
            else:
                gr.sharpe = 0.0

            result.groups.append(gr)

        # 多空组合
        long_rets = np.array(group_returns.get(self.n_groups, []))
        short_rets = np.array(group_returns.get(1, []))
        min_len = min(len(long_rets), len(short_rets))
        if min_len > 1:
            ls_rets = long_rets[:min_len] - short_rets[:min_len]
            result.long_short_return = float(np.prod(1 + ls_rets) - 1)
            if ls_rets.std() > 1e-12:
                result.long_short_sharpe = float(
                    ls_rets.mean() / ls_rets.std() * np.sqrt(252 / self.rebalance_freq)
                )
            result.long_short_equity = np.cumprod(1 + ls_rets).tolist()

        # IC统计
        if ic_list:
            ic_arr = np.array(ic_list)
            result.ic_series = ic_arr.tolist()
            result.ic_mean = float(ic_arr.mean())
            result.ic_std = float(ic_arr.std()) if len(ic_arr) > 1 else 1e-12
            result.icir = result.ic_mean / result.ic_std if result.ic_std > 1e-12 else 0.0
            result.t_stat = float(ic_arr.mean() / (ic_arr.std() / np.sqrt(len(ic_arr)))) if len(ic_arr) > 1 else 0.0

        # 单调性检验：检查各组年化收益是否从低到高单调递增
        annual_returns = [g.annual_return for g in result.groups if g.n_rebalance > 0]
        if len(annual_returns) >= 3:
            # 计算相邻组递增比例
            increases = sum(1 for i in range(1, len(annual_returns))
                          if annual_returns[i] > annual_returns[i - 1])
            result.monotonicity_score = increases / (len(annual_returns) - 1)
        else:
            result.monotonicity_score = 0.0

        self.result = result
        return result

    def print_summary(self):
        """打印分层回测摘要"""
        if self.result is None:
            print("请先调用 run()")
            return

        r = self.result
        print("=" * 70)
        print(f"  分层回测报告 ({r.n_groups}组 · {r.rebalance_freq}日调仓)")
        print("=" * 70)
        print(f"  区间: {r.start_date} ~ {r.end_date} | {r.n_dates}天 | {r.n_stocks}只股票")
        print(f"  IC均值: {r.ic_mean:+.4f} | IC_IR: {r.icir:.3f} | t值: {r.t_stat:.2f}")
        print(f"  多空收益: {r.long_short_return:+.2%} | 多空Sharpe: {r.long_short_sharpe:.3f}")
        print(f"  单调性: {r.monotonicity_score:.1%}")
        print("-" * 70)
        print(f"  {'组号':>4s}  {'累计收益':>10s}  {'年化收益':>10s}  {'最大回撤':>10s}  {'夏普':>8s}  {'胜率':>8s}  {'调仓':>4s}")
        print("-" * 70)

        for g in r.groups:
            if g.n_rebalance == 0:
                print(f"  {g.group:>4d}  {'N/A':>10s}  {'N/A':>10s}  {'N/A':>10s}  {'N/A':>8s}  {'N/A':>8s}  {'0':>4s}")
            else:
                print(f"  {g.group:>4d}  {g.cum_return:>+10.2%}  {g.annual_return:>+10.2%}  "
                      f"{g.max_drawdown:>10.2%}  {g.sharpe:>8.3f}  {g.win_rate:>8.1%}  {g.n_rebalance:>4d}")

        print("=" * 70)

        # 判断因子有效性
        if r.monotonicity_score >= 0.7 and abs(r.t_stat) >= 2.0:
            print("  结论: 因子有效 — 单调性良好且IC显著")
        elif r.monotonicity_score >= 0.5:
            print("  结论: 因子弱有效 — 单调性一般，建议结合IC_IR综合判断")
        else:
            print("  结论: 因子无效 — 收益不单调，不建议使用")
        print("=" * 70)

    def to_dataframe(self) -> pd.DataFrame:
        """输出各组结果为DataFrame"""
        if self.result is None:
            return pd.DataFrame()
        rows = []
        for g in self.result.groups:
            rows.append({
                "group": g.group,
                "cum_return": g.cum_return,
                "annual_return": g.annual_return,
                "max_drawdown": g.max_drawdown,
                "sharpe": g.sharpe,
                "win_rate": g.win_rate,
                "n_rebalance": g.n_rebalance,
            })
        return pd.DataFrame(rows)
