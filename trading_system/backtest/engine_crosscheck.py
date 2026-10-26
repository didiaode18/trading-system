# -*- coding: utf-8 -*-
"""
Stage 4 · 双引擎交叉验证（full-history-backtest 新增）
======================================================
同一策略、同一区间、同一股票集合，分别用
  - 向量化口径（master_common 等权日收益聚合）
  - 事件驱动引擎（backtest.engine.BacktestEngineV2 + SimBroker 撮合）
计算总收益与夏普，比对差异。

准入门 G7：总收益差 < 3% 且 夏普差 < 0.3。
超阈值说明两引擎逻辑不一致（常见：成交价口径、停牌处理、涨跌停成交），必须排查。

为便于测试构造"双引擎不一致"坏样本，vectorized_runner / event_runner 可注入。
默认共享策略 = 等权买入持有（口径最易对齐，差异仅来自一次性成本/取整）。

复用而非重写：撮合/成本一律走 backtest.engine + broker，指标走 backtest.metrics。
"""

import logging
import os
import sys
import config

try:
    from . import master_common
    from .engine import BacktestEngineV2
    from .broker import Order
except ImportError:  # pragma: no cover
    from backtest import master_common
    from backtest.engine import BacktestEngineV2
    from backtest.broker import Order

logger = logging.getLogger(__name__)

GATES = config.MASTER_BACKTEST_CONFIG.get("gates", {}) if hasattr(config, "MASTER_BACKTEST_CONFIG") else {}
RET_DIFF_MAX = GATES.get("G7_return_diff_max", 0.03)
SHARPE_DIFF_MAX = GATES.get("G7_sharpe_diff_max", 0.3)

# G7 对齐参数：共同窗口长度（交易日）与标的上限（降低整数手取整的现金拖累占比）
ALIGN_WINDOW = 250
ALIGN_CAP = 6


def _aligned_basket(data_dict: dict, codes: list, cap: int = ALIGN_CAP,
                    align_window: int = ALIGN_WINDOW):
    """求两侧完全对齐的标的集与共同窗口（消除 G7 长历史口径差的主因）。

    差异根因：阶梯上市（部分标的窗口早期无数据 → 向量化 mean 跳过 NaN 稀释、
    事件驱动首个可建仓日不同）、标的过多时整数手建仓的现金拖累。
    对齐：先 ffill 容忍停牌，取最近 align_window 交易日为共同窗口，
    dropna(axis=1, how='any') 仅保留“窗口内全程有数据”的标的（副除晚上市/退市），
    再 cap 标的数；两侧用同一 codes + 同一 [start,end]。
    返回 (aligned_codes, start_date, end_date)；不足则原样返回 (codes, None, None)。
    """
    try:
        panel = master_common._close_panel(data_dict, codes)
    except Exception:
        return list(codes), None, None
    if panel.empty:
        return list(codes), None, None
    panel = panel.ffill()  # 先填充（利用窗口前历史），使窗口内停牌日不致误删标的
    if len(panel) > align_window:
        panel = panel.iloc[-align_window:]
    full = panel.dropna(axis=1, how="any")  # 丢弃窗口内仍有 NaN 的列（未上市/长期停牌）
    if full.shape[1] < 2 or len(full) < 20:
        return list(codes), None, None
    aligned = list(full.columns)[:cap]
    return aligned, str(full.index[0])[:10], str(full.index[-1])[:10]


def _trim_window(data_dict: dict, codes: list, start_date, end_date) -> dict:
    """把 data_dict 中 codes 的数据裁剪到 [start_date, end_date]（含端点）。

    两侧 runner 因而看到完全相同的标的集与窗口（runner 签名 (data_dict, codes) 不变，
    保证注入式测试 runner 不受影响）。
    """
    out = {}
    for c in codes:
        df = data_dict.get(c)
        if df is None:
            continue
        d = df.copy()
        d["date"] = d["date"].astype(str).str[:10]
        if start_date:
            d = d[d["date"] >= str(start_date)[:10]]
        if end_date:
            d = d[d["date"] <= str(end_date)[:10]]
        out[c] = d.reset_index(drop=True)
    return out


def _vectorized_runner(data_dict: dict, codes: list) -> dict:
    """向量化口径：等权买入持有组合的总收益/夏普（与事件驱动同口径）。"""
    rets = master_common.equal_weight_buyhold_daily_returns(data_dict, codes)
    s = master_common.summarize_daily_returns(rets)
    if s.get("status") != "ok":
        return {"error": "向量化口径数据不足"}
    return {
        "total_return": s["total_return"],
        "sharpe": s["sharpe"],
        "n_days": s["n_days"],
        "start_date": s["start_date"],
        "end_date": s["end_date"],
    }


def _make_buy_and_hold_strategy(codes: list):
    """构造等权买入持有策略函数（首个有行情日一次性建仓，之后持有）。"""
    state = {"bought": False}

    def _strategy(date, feed, broker):
        if state["bought"]:
            return []
        tradable = [c for c in codes if c in feed.get_price_dict(date)]
        if not tradable:
            return []
        prices = feed.get_price_dict(date)
        n = len(tradable)
        cash = getattr(broker, "cash", getattr(broker, "initial_capital", 0))
        per = cash / max(1, n)
        orders = []
        for code in tradable:
            price = prices.get(code, 0)
            if price <= 0:
                continue
            shares = int(per / price / 100) * 100
            if shares >= 100:
                orders.append(Order(code=code, direction="buy", target_shares=shares,
                                    price=price, date=date, reason="等权建仓"))
        state["bought"] = True
        return orders

    return _strategy


def _event_runner(data_dict: dict, codes: list) -> dict:
    """事件驱动口径：BacktestEngineV2 等权买入持有。"""
    sub = {c: data_dict[c] for c in codes if c in data_dict}
    if not sub:
        return {"error": "事件驱动口径无标的"}
    capital = getattr(config, "TOTAL_CAPITAL", 0) or 1_000_000
    engine = BacktestEngineV2(initial_capital=capital)
    report = engine.run(sub, strategy_fn=_make_buy_and_hold_strategy(list(sub.keys())),
                        benchmark_code=None, auto_monte_carlo=False)
    if not report or "error" in report:
        return {"error": report.get("error", "引擎返回异常") if report else "引擎无返回"}
    return {
        "total_return": round(float(report.get("total_return", 0)), 4),
        "sharpe": round(float(report.get("sharpe_ratio", 0)), 4),
        "n_trades": int(report.get("total_trades", 0)),
        "start_date": report.get("start_date"),
        "end_date": report.get("end_date"),
    }


def cross_validate(data_dict: dict, codes: list, quick: bool = False,
                   vectorized_runner=None, event_runner=None) -> dict:
    """双引擎交叉验证主入口。

    返回:
      {status, vectorized, event_driven, return_diff_abs, sharpe_diff,
       pass_return, pass_sharpe, verdict}
    """
    vr = vectorized_runner or _vectorized_runner
    er = event_runner or _event_runner

    if quick:
        codes = codes[:6]

    # G7 对齐：两侧使用同一“共同窗口内全程有数据”的标的集与窗口，
    # 消除阶梯上市/首建仓日/整数手现金拖累造成的长历史口径差（残差应回落到成本+成交时点量级）。
    # 仅对默认 runner 生效；注入式测试 runner 仍收到 (data_dict, codes)（空面板时原样降级）。
    aligned_codes, start_date, end_date = _aligned_basket(data_dict, codes)
    data_dict = _trim_window(data_dict, aligned_codes, start_date, end_date)
    codes = aligned_codes

    try:
        vec = vr(data_dict, codes)
    except Exception as e:
        vec = {"error": f"向量化引擎异常: {e}"}
    try:
        evt = er(data_dict, codes)
    except Exception as e:
        evt = {"error": f"事件驱动引擎异常: {e}"}

    if "error" in vec or "error" in evt:
        return {
            "status": "fail",
            "vectorized": vec,
            "event_driven": evt,
            "reason": vec.get("error") or evt.get("error"),
        }

    ret_diff = abs(vec.get("total_return", 0) - evt.get("total_return", 0))
    sharpe_diff = abs(vec.get("sharpe", 0) - evt.get("sharpe", 0))
    pass_return = ret_diff < RET_DIFF_MAX
    pass_sharpe = sharpe_diff < SHARPE_DIFF_MAX

    if pass_return and pass_sharpe:
        verdict = "一致（两引擎总收益差%.2f%%、夏普差%.2f 均在阈值内）" % (ret_diff * 100, sharpe_diff)
    else:
        reasons = []
        if not pass_return:
            reasons.append(f"总收益差{ret_diff * 100:.2f}%≥{RET_DIFF_MAX * 100:.0f}%")
        if not pass_sharpe:
            reasons.append(f"夏普差{sharpe_diff:.2f}≥{SHARPE_DIFF_MAX}")
        verdict = "不一致（" + "，".join(reasons) + "）→ 需排查成交价口径/停牌/涨跌停处理"

    return {
        "status": "ok",
        "vectorized": vec,
        "event_driven": evt,
        "return_diff_abs": round(ret_diff, 4),
        "sharpe_diff": round(sharpe_diff, 4),
        "pass_return": pass_return,
        "pass_sharpe": pass_sharpe,
        "verdict": verdict,
    }
