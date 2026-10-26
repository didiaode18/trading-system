# -*- coding: utf-8 -*-
"""
最全历史回测流水线 · 共享工具层
================================
为 scripts/master_backtest.py 的新增 Stage（4/7/9/10/11）提供统一的
"策略日收益序列"构造与轻量统计胶水。

设计纪律（遵守 SKILL.md 铁律）:
  - 不重写指标：夏普/回撤/年化/alpha-beta 一律复用 backtest.metrics；
  - 不重写撮合：本模块只做等权组合的日收益聚合，供多基准/分段/bootstrap 对齐口径；
  - 组合口径 = 选定标的的"等权买入持有"日收益（每日再平衡近似），
    这是一个透明、可复现、可与指数直接对齐的基准口径，
    在总报告附录中显式声明（区别于 Stage1/5/6 的逐笔 v5/v6 口径）。
"""

import logging
import os
import sys

import numpy as np
import pandas as pd
import config

try:
    from . import metrics
except ImportError:  # 兼容 trading_system.backtest 包路径
    from backtest import metrics

logger = logging.getLogger(__name__)

RF_RATE = config.MASTER_BACKTEST_CONFIG.get("risk_free_rate", 0.03) if hasattr(config, "MASTER_BACKTEST_CONFIG") else 0.03


def _close_panel(data_dict: dict, codes: list) -> pd.DataFrame:
    """构造收盘价面板 index=date(str) columns=code，仅保留 >=2 只标的的公共日期。"""
    series = {}
    for code in codes:
        df = data_dict.get(code)
        if df is None or len(df) < 2 or "close" not in df.columns or "date" not in df.columns:
            continue
        s = df.copy()
        s["date"] = s["date"].astype(str)
        s = s.dropna(subset=["close"]).drop_duplicates(subset=["date"])
        series[code] = s.set_index("date")["close"]
    if len(series) < 1:
        return pd.DataFrame()
    panel = pd.DataFrame(series).sort_index()
    return panel


def equal_weight_daily_returns(data_dict: dict, codes: list,
                               start_date: str = None, end_date: str = None) -> pd.Series:
    """等权买入持有组合的日收益序列（每日再平衡近似）。

    返回: pd.Series(index=date_str, values=daily_return)；数据不足返回空 Series。
    """
    panel = _close_panel(data_dict, codes)
    if panel.empty or len(panel) < 3:
        return pd.Series(dtype=float)
    # 前向填充停牌日（保持持仓），再计算日收益
    panel = panel.ffill()
    rets = panel.pct_change()
    # 等权：对当日有数据的标的取均值
    basket = rets.mean(axis=1)
    basket = basket.dropna()
    if start_date:
        basket = basket[basket.index >= str(start_date)]
    if end_date:
        basket = basket[basket.index <= str(end_date)]
    return basket


def equal_weight_buyhold_daily_returns(data_dict: dict, codes: list) -> pd.Series:
    """等权买入持有（一次性建仓、不再平衡）组合的日收益序列。

    与 engine_crosscheck 的事件驱动"买入持有"策略同口径：每只标的以首个
    有效交易日归一化为 1（等初始资金），组合净值 = 各标的归一化净值的均值。
    停牌日前向填充。用于双引擎交叉验证，避免"每日再平衡 vs 买入持有"的口径错配。
    """
    panel = _close_panel(data_dict, codes)
    if panel.empty or len(panel) < 3:
        return pd.Series(dtype=float)
    panel = panel.ffill()
    first_valid = panel.apply(lambda col: col.dropna().iloc[0] if col.notna().any() else np.nan)
    norm = panel.div(first_valid, axis=1)
    equity = norm.mean(axis=1)
    return equity.pct_change().dropna()


def summarize_daily_returns(daily_returns: pd.Series) -> dict:
    """把日收益序列汇总为核心指标（复用 metrics，不重复实现公式）。"""
    if daily_returns is None or len(daily_returns) < 2:
        return {"status": "insufficient", "n_days": 0 if daily_returns is None else len(daily_returns)}
    equity = (1.0 + daily_returns).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    n = len(daily_returns)
    annual = metrics.calc_annual_return(total_return, n)
    max_dd, _, _ = metrics.calc_max_drawdown(equity)
    sharpe = metrics.calc_sharpe_ratio(daily_returns, RF_RATE)
    return {
        "status": "ok",
        "n_days": n,
        "start_date": str(daily_returns.index[0]),
        "end_date": str(daily_returns.index[-1]),
        "total_return": round(total_return, 4),
        "annual_return": round(float(annual), 4),
        "max_drawdown": round(float(max_dd), 4),
        "sharpe": round(float(sharpe), 4),
    }


def trades_to_return_series(trades: list) -> np.ndarray:
    """把 v5/v6 逐笔交易转为收益率数组。

    口径: v5/v6 的 net_profit 为百分比×100（如 3.5 表示 3.5%）→ /100；
    若传入已含 pnl_pct（小数）的 MC 规范化字典，则直接取用。
    """
    if not trades:
        return np.array([])
    out = []
    for t in trades:
        if "pnl_pct" in t and "net_profit" not in t:
            out.append(float(t.get("pnl_pct", 0)))
        else:
            out.append(float(t.get("net_profit", 0)) / 100.0)
    return np.array(out)


def sharpe_from_trade_returns(trade_returns: np.ndarray, avg_hold_days: float = 5.0) -> float:
    """逐笔收益率 → 年化夏普代理（与 backtest.compare 同口径：sqrt(252/持仓天数)）。"""
    if trade_returns is None or len(trade_returns) < 2:
        return 0.0
    std = float(np.std(trade_returns))
    if std < 1e-12:
        return 0.0
    ann = np.sqrt(252.0 / max(1.0, avg_hold_days))
    return float(np.mean(trade_returns) / std * ann)


def align_returns(strat: pd.Series, bench: pd.Series):
    """按日期对齐两个日收益序列，返回 (aligned_strat, aligned_bench)。"""
    if strat is None or bench is None or len(strat) == 0 or len(bench) == 0:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    common = strat.index.intersection(bench.index)
    return strat.loc[common], bench.loc[common]


def default_position_ratio() -> float:
    """单笔仓位占比默认值 = 1 / MAX_HOLDINGS（缺省 8 → 0.125）。

    与 monte_carlo / backtest_optimizer BUG-047 的仓位折算口径对齐，
    使"主动成交口径"净值与逐笔 PnL 折算一致。
    """
    try:
        mh = int(getattr(config, "MAX_HOLDINGS", 8) or 8)
    except Exception:
        mh = 8
    return 1.0 / max(1, mh)


def active_strategy_daily_returns(trades: list, position_ratio: float = None) -> pd.Series:
    """P0-2: 从 v5/v6 逐笔交易构造"主动成交口径"日收益序列。

    口径（与 equal_weight 被动篮子严格区分，报告中显式标注）:
      - 每笔交易按其平仓日 sell_date 归集；
      - 单笔贡献 = net_profit(百分比×100)/100 × position_ratio（仓位折算，
        对齐 monte_carlo 口径，避免"满仓单票"高估）；
      - 无交易的日期收益为 0（持仓期浮盈不逐日 mark，故为"平仓实现"口径的近似）。

    返回: pd.Series(index=sell_date_str 升序, values=当日主动收益)；无交易返回空 Series。
    """
    if not trades:
        return pd.Series(dtype=float)
    if position_ratio is None:
        position_ratio = default_position_ratio()
    bucket = {}
    for t in trades:
        d = str(t.get("sell_date", "") or "")[:10]
        if not d:
            continue
        pnl = float(t.get("net_profit", 0)) / 100.0 * float(position_ratio)
        bucket[d] = bucket.get(d, 0.0) + pnl
    if not bucket:
        return pd.Series(dtype=float)
    s = pd.Series(bucket).sort_index()
    return s.astype(float)


def summarize_active_strategy(trades: list, position_ratio: float = None) -> dict:
    """P0-2: 主动成交口径绩效摘要（复用 summarize_daily_returns，不重写指标）。

    额外给出主动口径的累计收益/年化/夏普/最大回撤，供报告与被动等权并列展示，
    杜绝"封面 318% 是被动篮子却被当作策略能力"的口径混淆。
    """
    daily = active_strategy_daily_returns(trades, position_ratio)
    n_trades = len(trades) if trades else 0
    if daily is None or len(daily) < 2:
        return {"status": "insufficient", "n_trades": n_trades,
                "note": "主动成交口径：有效平仓日不足，无法构造净值"}
    summary = summarize_daily_returns(daily)
    wins = [t for t in (trades or []) if float(t.get("net_profit", 0)) > 0]
    summary.update({
        "basis": "active_trades",
        "n_trades": n_trades,
        "win_rate": round(len(wins) / n_trades * 100, 2) if n_trades else 0.0,
        "position_ratio": round(float(position_ratio if position_ratio is not None
                                        else default_position_ratio()), 4),
    })
    return summary
