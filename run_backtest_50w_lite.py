# -*- coding: utf-8 -*-
"""
50万散户实战模拟回测（精简版）
==============================
跳过极慢的V2全量事件驱动引擎（已知3.5年仅1笔交易），
聚焦运行: V5真实环境回测 + Walk-Forward + 样本外验证 + V2快速抽样

参数: 50万 | 2023-01-01 ~ 2026-08-13 | 160只股票
"""

import sys
import os
import time
import json
import logging
import datetime
import sqlite3
import warnings
import traceback

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_system'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

INITIAL_CAPITAL = 500_000
START_DATE = "2023-01-01"
END_DATE = "2026-08-13"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest_50w")


# ============================================================
# 数据加载
# ============================================================
def load_all_data():
    conn = sqlite3.connect(config.DB_PATH)
    query = "SELECT code, date, open, close, high, low, volume FROM daily_kline ORDER BY code, date ASC"
    df_all = pd.read_sql(query, conn)
    conn.close()
    logger.info(f"读取: {len(df_all)} 条记录")

    data_dict = {}
    for code, group in df_all.groupby("code"):
        df = group.copy()
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        df = df[df["date"] >= "2022-10-01"].reset_index(drop=True)
        if len(df) > 100:
            data_dict[code] = df
    logger.info(f"加载 {len(data_dict)} 只股票")
    return data_dict


# ============================================================
# V5 真实环境回测
# ============================================================
def run_v5_backtest(data_dict):
    from backtest_real import backtest_stock_v4, backtest_stock_v5, analyze_trades

    logger.info("=" * 60)
    logger.info("【阶段1】V5真实环境回测（缩量回踩+突破回踩+信号质量评分）")
    logger.info("=" * 60)

    trades_v2, trades_v5 = [], []
    stock_codes = [c for c in data_dict.keys() if c != "000300"]
    total = len(stock_codes)

    for i, code in enumerate(stock_codes, 1):
        df = data_dict[code]
        info = config.get_stock_info(code)
        info_dict = {"名称": info.get("名称", code), "类型": info.get("类型", "龙头"), "行业": info.get("赛道", "其他")}
        try:
            t2 = backtest_stock_v4(df, code, info_dict, version="v2")
            t5 = backtest_stock_v5(df, code, info_dict)
            trades_v2.extend(t2)
            trades_v5.extend(t5)
        except Exception:
            pass
        if i % 20 == 0 or i == total:
            logger.info(f"  进度 [{i}/{total}]")

    trades_v2.sort(key=lambda x: x.get("buy_date", ""))
    trades_v5.sort(key=lambda x: x.get("buy_date", ""))

    stats_v2 = analyze_trades(trades_v2) if trades_v2 else {"error": "无交易"}
    stats_v5 = analyze_trades(trades_v5) if trades_v5 else {"error": "无交易"}

    logger.info(f"  V2: {len(trades_v2)}笔")
    if "error" not in stats_v2:
        logger.info(f"    胜率{stats_v2.get('win_rate',0)}%, 盈亏比{stats_v2.get('profit_factor',0)}, 期望{stats_v2.get('expectancy',0):+.2f}%")
    logger.info(f"  V5: {len(trades_v5)}笔")
    if "error" not in stats_v5:
        logger.info(f"    胜率{stats_v5.get('win_rate',0)}%, 盈亏比{stats_v5.get('profit_factor',0)}, 期望{stats_v5.get('expectancy',0):+.2f}%")

    return {
        "v2": {"stats": stats_v2, "trades": trades_v2},
        "v5": {"stats": stats_v5, "trades": trades_v5},
    }


# ============================================================
# V2 快速抽样回测（仅20只代表性股票）
# ============================================================
def run_v2_sampled(data_dict):
    from backtest.engine import BacktestEngineV2
    from backtest.broker import CostConfig, Order
    from strategy.trend_strategy import compute_indicators, generate_strategy_signal
    from strategy.position import calc_first_batch

    logger.info("=" * 60)
    logger.info("【阶段2】V2事件驱动引擎（抽样20只代表性股票）")
    logger.info("=" * 60)

    # 选取20只代表性股票（含持仓+大盘股）
    sample_codes = ["002371", "600519", "300750", "002594", "601318",
                    "600760", "000725", "300274", "601899", "600036",
                    "002230", "600893", "002049", "603986", "002409",
                    "600118", "600584", "002384", "300760", "601012"]
    sample_codes = [c for c in sample_codes if c in data_dict and c != "000300"][:20]

    # 预计算指标
    precomputed = {}
    for code in sample_codes:
        try:
            precomputed[code] = compute_indicators(data_dict[code].copy())
        except Exception:
            precomputed[code] = data_dict[code]

    def sample_strategy(date, feed, broker):
        orders = []
        for code in feed.stock_codes:
            if code not in precomputed:
                continue
            df_full = precomputed[code]
            df = df_full[df_full["date"] <= date].copy()
            if len(df) < config.MA_MID:
                continue
            holding = broker.get_holding_dict(code)
            try:
                signal = generate_strategy_signal(df, holding)
                if signal.get("sell_signal") and holding:
                    bar = feed.get_bar(code, date)
                    sp = signal.get("sell_price") or (bar["close"] if bar else 0)
                    if sp > 0:
                        orders.append(Order(code=code, direction="sell", target_shares=holding["shares"],
                                            price=sp, date=date, reason=signal.get("signal_reason", "策略卖出")))
                elif signal.get("buy_signal") and not holding:
                    bar = feed.get_bar(code, date)
                    bp = signal.get("buy_price") or (bar["close"] if bar else 0)
                    sl = signal.get("stop_loss_initial", bp * 0.9)
                    if bp > 0:
                        st = config.get_stock_info(code).get("类型", "龙头")
                        batch = calc_first_batch(bp, sl, st, broker.initial_capital)
                        if batch.get("pass_risk") and batch.get("shares", 0) > 0:
                            orders.append(Order(code=code, direction="buy", target_shares=batch["shares"],
                                                price=bp, date=date, reason=signal.get("signal_reason", "策略买入")))
            except Exception:
                pass
        return orders

    cost = CostConfig(buy_slippage=0.001, sell_slippage=0.001, commission_rate=0.00025,
                      min_commission=5.0, stamp_tax_rate=0.001)
    engine = BacktestEngineV2(initial_capital=INITIAL_CAPITAL, cost_config=cost)
    sub_data = {c: data_dict[c] for c in sample_codes}

    t0 = time.time()
    report = engine.run(sub_data, strategy_fn=sample_strategy,
                        start_date=START_DATE, end_date=END_DATE,
                        benchmark_code="000300" if "000300" in data_dict else None,
                        auto_monte_carlo=False)
    elapsed = time.time() - t0
    report["elapsed_sec"] = round(elapsed, 1)
    report["sample_stocks"] = sample_codes

    logger.info(f"  耗时: {elapsed:.1f}秒")
    logger.info(f"  累计收益: {report.get('total_return', 0):.2%}")
    logger.info(f"  年化收益: {report.get('annual_return', 0):.2%}")
    logger.info(f"  最大回撤: -{report.get('max_drawdown', 0):.2%}")
    logger.info(f"  夏普比率: {report.get('sharpe_ratio', 0):.2f}")
    logger.info(f"  交易笔数: {report.get('total_trades', 0)}")

    return report


# ============================================================
# Walk-Forward
# ============================================================
def run_walk_forward(data_dict):
    logger.info("=" * 60)
    logger.info("【阶段3】Walk-Forward 防过拟合验证")
    logger.info("=" * 60)
    try:
        from backtest.walk_forward import WalkForwardAnalyzer
        stock_codes = [c for c in data_dict.keys() if c != "000300"][:20]
        sub_data = {c: data_dict[c] for c in stock_codes}
        if "000300" in data_dict:
            sub_data["000300"] = data_dict["000300"]

        wf = WalkForwardAnalyzer(
            train_days=120, test_days=40,
            param_grid={"initial_stop_loss": [0.06, 0.07, 0.08],
                        "min_signal_quality": [55, 60, 65],
                        "drawdown_leader": [0.05, 0.06, 0.07]},
            initial_capital=INITIAL_CAPITAL, max_windows=15)

        t0 = time.time()
        result = wf.run(sub_data, stock_codes)
        result["elapsed_sec"] = round(time.time() - t0, 1)
        logger.info(f"  窗口数: {result.get('num_windows', 0)}")
        logger.info(f"  样本外夏普: {result.get('oos_sharpe', 0):.2f}")
        logger.info(f"  过拟合程度: {result.get('overfit_ratio', 0):.1%}")
        return result
    except Exception as e:
        logger.warning(f"  Walk-Forward失败: {e}")
        traceback.print_exc()
        return {"error": str(e)}


# ============================================================
# 样本外验证
# ============================================================
def run_holdout(data_dict):
    logger.info("=" * 60)
    logger.info("【阶段4】独立样本外验证（Holdout 120天）")
    logger.info("=" * 60)
    try:
        from backtest_real import backtest_stock_v5, analyze_trades
        holdout_days = 120
        stock_codes = [c for c in data_dict.keys() if c != "000300"]
        holdout_trades = []
        for code in stock_codes:
            df = data_dict[code]
            if len(df) <= holdout_days + 80:
                continue
            df_h = df.iloc[-(holdout_days + 80):].reset_index(drop=True)
            info = config.get_stock_info(code)
            info_dict = {"名称": info.get("名称", code), "类型": info.get("类型", "龙头"), "行业": info.get("赛道", "其他")}
            try:
                trades = backtest_stock_v5(df_h, code, info_dict)
                cutoff = df.iloc[-holdout_days]["date"]
                trades = [t for t in trades if t.get("buy_date", "") >= cutoff]
                holdout_trades.extend(trades)
            except Exception:
                pass
        if not holdout_trades:
            return {"error": "样本外无交易"}
        stats = analyze_trades(holdout_trades)
        hs = data_dict[stock_codes[0]].iloc[-holdout_days]["date"]
        he = data_dict[stock_codes[0]].iloc[-1]["date"]
        logger.info(f"  区间: {hs} ~ {he}, 交易: {len(holdout_trades)}笔")
        logger.info(f"  胜率: {stats.get('win_rate', 0)}%, 盈亏比: {stats.get('profit_factor', 0)}")
        return {"stats": stats, "trades_count": len(holdout_trades),
                "period": f"{hs} ~ {he}", "holdout_days": holdout_days}
    except Exception as e:
        logger.warning(f"  样本外验证失败: {e}")
        return {"error": str(e)}


# ============================================================
# 打印报告
# ============================================================
def print_report(v2_sample, real_result, wf_result, holdout_result):
    v5_stats = real_result.get("v5", {}).get("stats", {})
    v2_stats = real_result.get("v2", {}).get("stats", {})
    v5_trades = real_result.get("v5", {}).get("trades", [])
    v2_trades = real_result.get("v2", {}).get("trades", [])

    print("\n" + "=" * 70)
    print("  操盘密码交易系统 — 50万散户实战模拟回测验证报告")
    print(f"  回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,}元")
    print("=" * 70)

    # === V2 抽样引擎 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 一、V2事件驱动引擎（抽样20只代表性股票）                         │")
    print("└─────────────────────────────────────────────────────────────────┘")
    if v2_sample and "error" not in v2_sample:
        print(f"  累计收益率:   {v2_sample.get('total_return', 0):>10.2%}")
        print(f"  年化收益率:   {v2_sample.get('annual_return', 0):>10.2%}")
        print(f"  最大回撤:     {-v2_sample.get('max_drawdown', 0):>10.2%}")
        print(f"  夏普比率:     {v2_sample.get('sharpe_ratio', 0):>10.2f}")
        print(f"  Sortino比率:  {v2_sample.get('sortino_ratio', 0):>10.2f}")
        print(f"  Calmar比率:   {v2_sample.get('calmar_ratio', 0):>10.2f}")
        print(f"  交易笔数:     {v2_sample.get('total_trades', 0):>10d}")
        print(f"  基准收益:     {v2_sample.get('benchmark_return', 0):>10.2%}")
        print(f"  超额收益:     {v2_sample.get('excess_return', 0):>10.2%}")
        print(f"  交易成本:     {v2_sample.get('total_cost', 0):>10,.0f}元")
    else:
        print("  引擎回测失败或无交易")

    # === V5 全量 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 二、V5真实环境回测（160只全量，缩量回踩+突破+信号质量评分）       │")
    print("└─────────────────────────────────────────────────────────────────┘")
    if "error" not in v5_stats:
        print(f"  总交易笔数:   {v5_stats.get('total', 0):>10d}")
        print(f"  总胜率:       {v5_stats.get('win_rate', 0):>10.1f}%")
        print(f"  盈亏比:       {v5_stats.get('profit_factor', 0):>10.2f}")
        print(f"  每笔期望:     {v5_stats.get('expectancy', 0):>+10.2f}%")
        print(f"  平均盈利:     +{v5_stats.get('avg_win', 0):>9.2f}%")
        print(f"  平均亏损:     {v5_stats.get('avg_loss', 0):>10.2f}%")
        print(f"  最大连续亏损: {v5_stats.get('max_consec_loss', 0):>10d}次")
        print(f"  平均持仓天数: {v5_stats.get('avg_hold', 0):>10.0f}天")
        print(f"  盈利单持仓:   {v5_stats.get('avg_hold_win', 0):>10.0f}天")
        print(f"  亏损单持仓:   {v5_stats.get('avg_hold_loss', 0):>10.0f}天")
    else:
        print(f"  V5回测失败: {v5_stats}")

    # V2逐股对比
    if "error" not in v2_stats:
        print(f"\n  V2简易版对比（全量160只）:")
        print(f"    交易: {v2_stats.get('total',0)}笔, 胜率: {v2_stats.get('win_rate',0):.1f}%, "
              f"盈亏比: {v2_stats.get('profit_factor',0):.2f}, 期望: {v2_stats.get('expectancy',0):+.2f}%")

    # === 分市场环境 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 三、分市场环境表现（V5）                                         │")
    print("└─────────────────────────────────────────────────────────────────┘")
    regime_stats = {}
    for t in v5_trades:
        rg = t.get("regime", "RANGE")
        if rg not in regime_stats:
            regime_stats[rg] = {"count": 0, "wins": 0, "total_pnl": 0, "hold_days": []}
        regime_stats[rg]["count"] += 1
        if t["net_profit"] > 0:
            regime_stats[rg]["wins"] += 1
        regime_stats[rg]["total_pnl"] += t["net_profit"]
        regime_stats[rg]["hold_days"].append(t.get("hold_days", 0))

    labels = {"BULL": "🟢 牛市", "RANGE": "🟡 震荡", "BEAR": "🔴 熊市"}
    for rg in ["BULL", "RANGE", "BEAR"]:
        if rg in regime_stats:
            s = regime_stats[rg]
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            avg = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
            ah = np.mean(s["hold_days"]) if s["hold_days"] else 0
            print(f"  {labels.get(rg, rg):10s}: {s['count']:>4d}笔, 胜率{wr:>5.1f}%, 平均盈亏{avg:>+.2f}%, 持仓{ah:.0f}天")

    # === 卖出原因 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 四、卖出原因分布（V5）                                           │")
    print("└─────────────────────────────────────────────────────────────────┘")
    sell_stats = {}
    for t in v5_trades:
        r = t.get("sell_reason", "未知")
        if r not in sell_stats:
            sell_stats[r] = {"count": 0, "wins": 0, "total": 0}
        sell_stats[r]["count"] += 1
        if t["net_profit"] > 0:
            sell_stats[r]["wins"] += 1
        sell_stats[r]["total"] += t["net_profit"]
    for r, d in sorted(sell_stats.items(), key=lambda x: -x[1]["count"]):
        wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
        avg = d["total"] / d["count"] if d["count"] > 0 else 0
        pct = d["count"] / len(v5_trades) * 100 if v5_trades else 0
        print(f"  {r:20s}: {d['count']:>4d}笔({pct:>4.1f}%), 胜率{wr:>5.1f}%, 平均{avg:>+.2f}%")

    # === Walk-Forward ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 五、Walk-Forward 防过拟合验证                                    │")
    print("└─────────────────────────────────────────────────────────────────┘")
    if wf_result and "error" not in wf_result:
        print(f"  验证窗口数:   {wf_result.get('num_windows', 0)}")
        print(f"  样本外夏普:   {wf_result.get('oos_sharpe', 0):.2f}")
        print(f"  参数稳定性:   {wf_result.get('stability_score', 0):.0%}")
        print(f"  过拟合程度:   {wf_result.get('overfit_ratio', 0):.1%}")
        print(f"  判定:         {wf_result.get('verdict', 'N/A')}")
    else:
        print(f"  失败: {wf_result.get('error', '未知')}")

    # === Holdout ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 六、独立样本外验证（Holdout 120天）                               │")
    print("└─────────────────────────────────────────────────────────────────┘")
    if holdout_result and "error" not in holdout_result:
        hs = holdout_result.get("stats", {})
        full_wr = v5_stats.get('win_rate', 0)
        hold_wr = hs.get('win_rate', 0)
        full_pf = v5_stats.get('profit_factor', 0)
        hold_pf = hs.get('profit_factor', 0)
        print(f"  验证区间:     {holdout_result.get('period', 'N/A')}")
        print(f"  样本外交易:   {holdout_result.get('trades_count', 0)}笔")
        print(f"  样本外胜率:   {hold_wr:.1f}% (全样本: {full_wr:.1f}%, 衰减: {hold_wr-full_wr:+.1f}%)")
        print(f"  样本外盈亏比: {hold_pf:.2f} (全样本: {full_pf:.2f}, 衰减: {hold_pf-full_pf:+.2f})")
        print(f"  样本外每笔:   {hs.get('expectancy', 0):+.2f}% (全样本: {v5_stats.get('expectancy', 0):+.2f}%)")
        if hold_wr >= full_wr * 0.85 and hold_pf >= full_pf * 0.7:
            print(f"  泛化判定:     ✅ 泛化良好")
        elif hold_wr >= full_wr * 0.7:
            print(f"  泛化判定:     ⚠️ 轻度过拟合")
        else:
            print(f"  泛化判定:     ❌ 过拟合风险高")
    else:
        print(f"  失败: {holdout_result.get('error', '未知')}")

    # === 半年度 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 七、半年度表现（V5）                                             │")
    print("└─────────────────────────────────────────────────────────────────┘")
    hy = {}
    for t in v5_trades:
        bd = t.get("buy_date", "")
        if len(bd) >= 7:
            pk = f"{bd[:4]}{'H1' if int(bd[5:7]) <= 6 else 'H2'}"
            if pk not in hy:
                hy[pk] = {"count": 0, "wins": 0, "total_pnl": 0}
            hy[pk]["count"] += 1
            if t["net_profit"] > 0:
                hy[pk]["wins"] += 1
            hy[pk]["total_pnl"] += t["net_profit"]
    for pk in sorted(hy.keys()):
        s = hy[pk]
        wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
        avg = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
        print(f"  {pk}: {s['count']:>4d}笔, 胜率{wr:>5.1f}%, 累计{s['total_pnl']:>+.1f}%, 每笔{avg:>+.2f}%")

    # === 标的排名 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 八、标的盈亏排名（V5 Top10/Bottom10）                             │")
    print("└─────────────────────────────────────────────────────────────────┘")
    ss = {}
    for t in v5_trades:
        key = f"{t['code']}|{t.get('name', t['code'])}"
        if key not in ss:
            ss[key] = {"count": 0, "wins": 0, "total_pnl": 0}
        ss[key]["count"] += 1
        if t["net_profit"] > 0:
            ss[key]["wins"] += 1
        ss[key]["total_pnl"] += t["net_profit"]
    ranked = sorted(ss.items(), key=lambda x: -x[1]["total_pnl"])
    print("  Top10 盈利:")
    for key, s in ranked[:10]:
        code, name = key.split("|")
        wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
        print(f"    {code} {name:8s}: {s['count']:>3d}笔, 胜率{wr:>5.1f}%, 累计{s['total_pnl']:>+.1f}%")
    print("  Top10 亏损:")
    for key, s in ranked[-10:]:
        code, name = key.split("|")
        wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
        print(f"    {code} {name:8s}: {s['count']:>3d}笔, 胜率{wr:>5.1f}%, 累计{s['total_pnl']:>+.1f}%")

    print("\n" + "=" * 70)


# ============================================================
# 主函数
# ============================================================
def main():
    total_start = time.time()
    print("=" * 70)
    print("  50万散户实战模拟回测验证（精简版）")
    print(f"  区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,}元")
    print("=" * 70)

    logger.info("【步骤1】加载数据...")
    data_dict = load_all_data()
    if len(data_dict) < 5:
        logger.error("数据不足"); return

    # V5 全量回测
    real_result = run_v5_backtest(data_dict)

    # V2 抽样回测
    v2_sample = run_v2_sampled(data_dict)

    # Walk-Forward
    wf_result = run_walk_forward(data_dict)

    # Holdout
    holdout_result = run_holdout(data_dict)

    # 打印报告
    print_report(v2_sample, real_result, wf_result, holdout_result)

    # 保存结果
    results_path = os.path.join(config.OUTPUT_DIR, f"backtest_50w_{datetime.date.today().strftime('%Y%m%d')}.json")
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    serializable = {"config": {"initial_capital": INITIAL_CAPITAL, "start_date": START_DATE,
                               "end_date": END_DATE, "stock_count": len(data_dict)}}

    # V2 sample
    if v2_sample and "error" not in v2_sample:
        serializable["engine_v2_sample"] = {k: v for k, v in v2_sample.items()
                                            if isinstance(v, (int, float, str, bool, type(None)))}
    # V5
    v5s = real_result.get("v5", {}).get("stats", {})
    if "error" not in v5s:
        serializable["v5"] = {k: v for k, v in v5s.items() if isinstance(v, (int, float, str, bool, type(None), list, dict))}
    v2s = real_result.get("v2", {}).get("stats", {})
    if "error" not in v2s:
        serializable["v2_full"] = {k: v for k, v in v2s.items() if isinstance(v, (int, float, str, bool, type(None), list, dict))}
    # WF
    if wf_result and "error" not in wf_result:
        serializable["walk_forward"] = {k: v for k, v in wf_result.items()
                                        if isinstance(v, (int, float, str, bool, type(None), list, dict))}
    # Holdout
    if holdout_result and "error" not in holdout_result:
        hs = holdout_result.get("stats", {})
        serializable["holdout"] = {"trades_count": holdout_result.get("trades_count", 0),
                                   "period": holdout_result.get("period", ""),
                                   "stats": {k: v for k, v in hs.items() if isinstance(v, (int, float, str, bool, type(None), list, dict))}}

    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"结果保存: {results_path}")

    elapsed = time.time() - total_start
    print(f"\n  总耗时: {elapsed:.1f}秒")
    return serializable


if __name__ == "__main__":
    main()
