# -*- coding: utf-8 -*-
"""
50万散户实战模拟回测验证
========================
基于系统全部回测基础设施，以散户视角执行完整回测

参数:
  - 初始资金: 50万元
  - 回测区间: 2023-01-01 ~ 2026-08-13（覆盖牛/熊/震荡）
  - 标的: 数据库全部161只股票
  - 策略: V2事件驱动(趋势跟踪) + V5真实环境(缩量回踩+突破) + Walk-Forward
  - 成本: 佣金万2.5 + 印花税千1 + 滑点0.1% + T+1 + 涨跌停
"""

import sys
import os
import time
import json
import logging
import datetime
import sqlite3
import traceback
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_system'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

# ============================================================
# 回测参数（50万散户配置）
# ============================================================
INITIAL_CAPITAL = 500_000   # 50万元
START_DATE = "2023-01-01"
END_DATE = "2026-08-13"     # 数据库最新日期

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backtest_50w")

# 收集所有结果的字典
all_results = {}


def load_all_data():
    """从SQLite批量加载全部历史数据"""
    if not os.path.exists(config.DB_PATH):
        logger.error(f"数据库不存在: {config.DB_PATH}")
        return {}

    conn = sqlite3.connect(config.DB_PATH)
    query = """SELECT code, date, open, close, high, low, volume 
               FROM daily_kline ORDER BY code, date ASC"""
    df_all = pd.read_sql(query, conn)
    conn.close()

    logger.info(f"  读取完成: {len(df_all)} 条记录")

    data_dict = {}
    warmup_date = "2022-10-01"

    for code, group in df_all.groupby("code"):
        df = group.copy()
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        df = df[df["date"] >= warmup_date].reset_index(drop=True)
        if len(df) > 100:
            data_dict[code] = df

    logger.info(f"  成功加载 {len(data_dict)} 只股票数据")
    return data_dict


def precompute_indicators(data_dict):
    """预计算全部股票的技术指标"""
    from strategy.trend_strategy import compute_indicators

    logger.info("  预计算技术指标...")
    precomputed = {}
    total = len(data_dict)

    for i, (code, df) in enumerate(data_dict.items(), 1):
        try:
            df_calc = compute_indicators(df.copy())
            precomputed[code] = df_calc
        except Exception:
            precomputed[code] = df

        if i % 20 == 0 or i == total:
            logger.info(f"    [{i}/{total}] 指标预计算完成")

    logger.info(f"  指标预计算完成: {len(precomputed)} 只")
    return precomputed


def run_engine_v2(data_dict, precomputed_data):
    """运行事件驱动回测引擎V2"""
    from backtest.engine import BacktestEngineV2
    from backtest.broker import CostConfig, Order
    from strategy.trend_strategy import generate_strategy_signal
    from strategy.position import calc_first_batch

    logger.info("=" * 60)
    logger.info("【阶段1】事件驱动回测引擎V2（趋势跟踪MA20/MA60）")
    logger.info("=" * 60)

    cost = CostConfig(
        buy_slippage=0.001,
        sell_slippage=0.001,
        commission_rate=0.00025,
        min_commission=5.0,
        stamp_tax_rate=0.001,
    )

    engine = BacktestEngineV2(initial_capital=INITIAL_CAPITAL, cost_config=cost)

    # 创建快速策略
    def fast_strategy(date, feed, broker):
        orders = []
        for code in feed.stock_codes:
            if code not in precomputed_data:
                continue
            df_full = precomputed_data[code]
            mask = df_full["date"] <= date
            df = df_full[mask].copy()
            if len(df) < config.MA_MID:
                continue
            holding = broker.get_holding_dict(code)
            try:
                signal = generate_strategy_signal(df, holding)
                if signal.get("sell_signal") and holding:
                    bar = feed.get_bar(code, date)
                    sell_price = signal.get("sell_price") or (bar["close"] if bar else 0)
                    if sell_price > 0:
                        orders.append(Order(
                            code=code, direction="sell",
                            target_shares=holding["shares"],
                            price=sell_price, date=date,
                            reason=signal.get("signal_reason", "策略卖出"),
                        ))
                elif signal.get("buy_signal") and not holding:
                    bar = feed.get_bar(code, date)
                    buy_price = signal.get("buy_price") or (bar["close"] if bar else 0)
                    stop_loss = signal.get("stop_loss_initial", buy_price * 0.9)
                    if buy_price > 0:
                        stock_type = config.get_stock_info(code).get("类型", "龙头")
                        batch = calc_first_batch(buy_price, stop_loss, stock_type, broker.initial_capital)
                        if batch.get("pass_risk") and batch.get("shares", 0) > 0:
                            orders.append(Order(
                                code=code, direction="buy",
                                target_shares=batch["shares"],
                                price=buy_price, date=date,
                                reason=signal.get("signal_reason", "策略买入"),
                            ))
            except Exception:
                pass
        return orders

    sub_data = {c: data_dict[c] for c in data_dict.keys()}
    logger.info(f"  引擎使用 {len(sub_data)} 只股票")

    t0 = time.time()
    report = engine.run(
        sub_data,
        strategy_fn=fast_strategy,
        start_date=START_DATE,
        end_date=END_DATE,
        benchmark_code="000300" if "000300" in sub_data else None,
        auto_monte_carlo=True,
        mc_simulations=500,
    )
    elapsed = time.time() - t0

    report["elapsed_sec"] = round(elapsed, 1)
    report["order_log"] = engine.order_log

    # 打印关键指标
    logger.info(f"  耗时: {elapsed:.1f}秒")
    logger.info(f"  累计收益: {report.get('total_return', 0):.2%}")
    logger.info(f"  年化收益: {report.get('annual_return', 0):.2%}")
    logger.info(f"  最大回撤: -{report.get('max_drawdown', 0):.2%}")
    logger.info(f"  夏普比率: {report.get('sharpe_ratio', 0):.2f}")
    logger.info(f"  交易笔数: {report.get('total_trades', 0)}")

    return report


def run_v5_backtest(data_dict):
    """运行真实环境回测V5"""
    from backtest_real import backtest_stock_v4, backtest_stock_v5, analyze_trades

    logger.info("=" * 60)
    logger.info("【阶段2】真实环境回测V5.0（缩量回踩+突破回踩+信号质量评分）")
    logger.info("=" * 60)

    trades_v2 = []
    trades_v5 = []
    stock_info_map = {}

    stock_codes = [c for c in data_dict.keys() if c != "000300"]
    total = len(stock_codes)
    logger.info(f"  回测标的: {total} 只股票")

    for i, code in enumerate(stock_codes, 1):
        df = data_dict[code]
        info = config.get_stock_info(code)
        stock_type = info.get("类型", "龙头")
        industry = info.get("赛道", "其他")
        name = info.get("名称", code)
        stock_info_map[code] = {"名称": name, "类型": stock_type, "行业": industry}

        info_dict = {"名称": name, "类型": stock_type, "行业": industry}

        try:
            t2 = backtest_stock_v4(df, code, info_dict, version="v2")
            t5 = backtest_stock_v5(df, code, info_dict)
            trades_v2.extend(t2)
            trades_v5.extend(t5)
        except Exception as e:
            if i <= 5 or i % 20 == 0:
                logger.warning(f"    [{i}/{total}] {code} 回测失败: {e}")

        if i % 20 == 0 or i == total:
            logger.info(f"    [{i}/{total}] 回测进度")

    trades_v2.sort(key=lambda x: x.get("buy_date", ""))
    trades_v5.sort(key=lambda x: x.get("buy_date", ""))

    stats_v2 = analyze_trades(trades_v2) if trades_v2 else {"error": "无交易"}
    stats_v5 = analyze_trades(trades_v5) if trades_v5 else {"error": "无交易"}

    logger.info(f"  V2交易: {len(trades_v2)}笔")
    if "error" not in stats_v2:
        logger.info(f"  V2胜率: {stats_v2.get('win_rate', 0)}%, 盈亏比: {stats_v2.get('profit_factor', 0)}")
    logger.info(f"  V5交易: {len(trades_v5)}笔")
    if "error" not in stats_v5:
        logger.info(f"  V5胜率: {stats_v5.get('win_rate', 0)}%, 盈亏比: {stats_v5.get('profit_factor', 0)}")
        logger.info(f"  V5每笔期望: {stats_v5.get('expectancy', 0):+.2f}%")

    return {
        "v2": {"stats": stats_v2, "trades": trades_v2},
        "v5": {"stats": stats_v5, "trades": trades_v5},
        "stock_info": stock_info_map,
    }


def run_walk_forward(data_dict):
    """运行Walk-Forward防过拟合验证"""
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
            train_days=120,
            test_days=40,
            param_grid={
                "initial_stop_loss": [0.06, 0.07, 0.08],
                "min_signal_quality": [55, 60, 65],
                "drawdown_leader": [0.05, 0.06, 0.07],
            },
            initial_capital=INITIAL_CAPITAL,
            max_windows=15,
        )

        t0 = time.time()
        result = wf.run(sub_data, stock_codes)
        elapsed = time.time() - t0
        result["elapsed_sec"] = round(elapsed, 1)

        logger.info(f"  验证窗口数: {result.get('num_windows', 0)}")
        logger.info(f"  样本外夏普: {result.get('oos_sharpe', 0):.2f}")
        logger.info(f"  过拟合程度: {result.get('overfit_ratio', 0):.1%}")
        logger.info(f"  耗时: {elapsed:.1f}秒")

        return result
    except Exception as e:
        logger.warning(f"  Walk-Forward执行失败: {e}")
        traceback.print_exc()
        return {"error": str(e)}


def run_holdout_validation(data_dict):
    """独立样本外验证（最近120个交易日）"""
    logger.info("=" * 60)
    logger.info("【阶段4】独立样本外验证（Holdout）")
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
            df_holdout = df.iloc[-(holdout_days + 80):].reset_index(drop=True)
            info = config.get_stock_info(code)
            info_dict = {"名称": info.get("名称", code), "类型": info.get("类型", "龙头"), "行业": info.get("赛道", "其他")}
            try:
                trades = backtest_stock_v5(df_holdout, code, info_dict)
                cutoff_date = df.iloc[-holdout_days]["date"]
                trades = [t for t in trades if t.get("buy_date", "") >= cutoff_date]
                holdout_trades.extend(trades)
            except Exception:
                pass

        if not holdout_trades:
            return {"error": "样本外无交易"}

        stats = analyze_trades(holdout_trades)
        holdout_start = data_dict[stock_codes[0]].iloc[-holdout_days]["date"]
        holdout_end = data_dict[stock_codes[0]].iloc[-1]["date"]

        logger.info(f"  样本外区间: {holdout_start} ~ {holdout_end}")
        logger.info(f"  交易笔数: {len(holdout_trades)}")
        logger.info(f"  胜率: {stats.get('win_rate', 0)}%")
        logger.info(f"  盈亏比: {stats.get('profit_factor', 0)}")

        return {
            "stats": stats,
            "trades_count": len(holdout_trades),
            "period": f"{holdout_start} ~ {holdout_end}",
            "holdout_days": holdout_days,
        }
    except Exception as e:
        logger.warning(f"  样本外验证失败: {e}")
        return {"error": str(e)}


def collect_trade_details(real_result):
    """收集交易明细统计"""
    v5_trades = real_result.get("v5", {}).get("trades", [])
    v5_stats = real_result.get("v5", {}).get("stats", {})

    if not v5_trades:
        return {}

    # 按卖出原因分组统计
    sell_reason_stats = {}
    for t in v5_trades:
        reason = t.get("sell_reason", "未知")
        if reason not in sell_reason_stats:
            sell_reason_stats[reason] = {"count": 0, "wins": 0, "total_pnl": 0}
        sell_reason_stats[reason]["count"] += 1
        if t["net_profit"] > 0:
            sell_reason_stats[reason]["wins"] += 1
        sell_reason_stats[reason]["total_pnl"] += t["net_profit"]

    # 按市场环境分组
    regime_stats = {}
    for t in v5_trades:
        rg = t.get("regime", "RANGE")
        if rg not in regime_stats:
            regime_stats[rg] = {"count": 0, "wins": 0, "total_pnl": 0}
        regime_stats[rg]["count"] += 1
        if t["net_profit"] > 0:
            regime_stats[rg]["wins"] += 1
        regime_stats[rg]["total_pnl"] += t["net_profit"]

    # 按半年度分组
    half_year_stats = {}
    for t in v5_trades:
        bd = t.get("buy_date", "")
        if len(bd) >= 7:
            year = bd[:4]
            half = "H1" if int(bd[5:7]) <= 6 else "H2"
            pk = f"{year}{half}"
        else:
            continue
        if pk not in half_year_stats:
            half_year_stats[pk] = {"count": 0, "wins": 0, "total_pnl": 0}
        half_year_stats[pk]["count"] += 1
        if t["net_profit"] > 0:
            half_year_stats[pk]["wins"] += 1
        half_year_stats[pk]["total_pnl"] += t["net_profit"]

    # 股票排名
    stock_stats = {}
    for t in v5_trades:
        key = f"{t['code']}|{t.get('name', t['code'])}"
        if key not in stock_stats:
            stock_stats[key] = {"count": 0, "wins": 0, "total_pnl": 0, "hold_days": []}
        stock_stats[key]["count"] += 1
        if t["net_profit"] > 0:
            stock_stats[key]["wins"] += 1
        stock_stats[key]["total_pnl"] += t["net_profit"]
        stock_stats[key]["hold_days"].append(t.get("hold_days", 0))

    return {
        "sell_reasons": sell_reason_stats,
        "regime_stats": regime_stats,
        "half_year_stats": half_year_stats,
        "stock_ranking": stock_stats,
    }


def print_summary_report(engine_report, real_result, wf_result, holdout_result, trade_details):
    """打印文本摘要报告"""
    v5_stats = real_result.get("v5", {}).get("stats", {})
    v2_stats = real_result.get("v2", {}).get("stats", {})
    v5_trades = real_result.get("v5", {}).get("trades", [])

    print("\n")
    print("=" * 70)
    print("  操盘密码交易系统 — 50万散户实战模拟回测验证报告")
    print(f"  回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,}元")
    print("=" * 70)

    # === 1. 事件驱动引擎V2 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 一、事件驱动回测引擎V2（趋势跟踪MA20/MA60）                    │")
    print("└─────────────────────────────────────────────────────────────────┘")
    if engine_report and "error" not in engine_report:
        print(f"  累计收益率:   {engine_report.get('total_return', 0):>10.2%}")
        print(f"  年化收益率:   {engine_report.get('annual_return', 0):>10.2%}")
        print(f"  最大回撤:     {-engine_report.get('max_drawdown', 0):>10.2%}")
        print(f"  夏普比率:     {engine_report.get('sharpe_ratio', 0):>10.2f}")
        print(f"  Sortino比率:  {engine_report.get('sortino_ratio', 0):>10.2f}")
        print(f"  Calmar比率:   {engine_report.get('calmar_ratio', 0):>10.2f}")
        print(f"  交易笔数:     {engine_report.get('total_trades', 0):>10d}")
        print(f"  基准收益:     {engine_report.get('benchmark_return', 0):>10.2%}")
        print(f"  超额收益:     {engine_report.get('excess_return', 0):>10.2%}")
        print(f"  交易成本:     {engine_report.get('total_cost', 0):>10,.0f}元")
        # Monte Carlo
        if engine_report.get("mc_95_max_drawdown") is not None:
            print(f"\n  Monte Carlo (500次):")
            print(f"    95%置信最大回撤: -{engine_report['mc_95_max_drawdown']:.1%}")
            print(f"    平均回撤:        -{engine_report['mc_avg_drawdown']:.1%}")
            print(f"    破产概率(腰斩):  {engine_report['mc_bankruptcy_prob']:.2%}")
    else:
        print("  引擎回测失败或无交易")

    # === 2. 真实环境V5 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 二、真实环境回测V5.0（缩量回踩+突破回踩+信号质量评分）          │")
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

    # V2对比
    if "error" not in v2_stats:
        print(f"\n  V2简易版对比:")
        print(f"    交易笔数: {v2_stats.get('total', 0)}, 胜率: {v2_stats.get('win_rate', 0):.1f}%, "
              f"盈亏比: {v2_stats.get('profit_factor', 0):.2f}, 每笔期望: {v2_stats.get('expectancy', 0):+.2f}%")

    # === 3. 分市场环境 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 三、分市场环境表现                                              │")
    print("└─────────────────────────────────────────────────────────────────┘")
    regime_stats = trade_details.get("regime_stats", {})
    regime_labels = {"BULL": "牛市", "RANGE": "震荡", "BEAR": "熊市"}
    for rg in ["BULL", "RANGE", "BEAR"]:
        if rg in regime_stats:
            s = regime_stats[rg]
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            avg = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
            print(f"  {regime_labels.get(rg, rg):6s}: {s['count']:>4d}笔, 胜率{wr:>5.1f}%, 平均盈亏{avg:>+.2f}%")

    # === 4. Walk-Forward ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 四、Walk-Forward 防过拟合验证                                    │")
    print("└─────────────────────────────────────────────────────────────────┘")
    if wf_result and "error" not in wf_result:
        print(f"  验证窗口数:   {wf_result.get('num_windows', 0)}")
        print(f"  样本外夏普:   {wf_result.get('oos_sharpe', 0):.2f}")
        print(f"  参数稳定性:   {wf_result.get('stability_score', 0):.0%}")
        print(f"  过拟合程度:   {wf_result.get('overfit_ratio', 0):.1%}")
        print(f"  判定:         {wf_result.get('verdict', 'N/A')}")
    else:
        print(f"  Walk-Forward失败: {wf_result.get('error', '未知')}")

    # === 5. 样本外验证 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 五、独立样本外验证（Holdout）                                    │")
    print("└─────────────────────────────────────────────────────────────────┘")
    if holdout_result and "error" not in holdout_result:
        hs = holdout_result.get("stats", {})
        full_wr = v5_stats.get('win_rate', 0)
        hold_wr = hs.get('win_rate', 0)
        full_pf = v5_stats.get('profit_factor', 0)
        hold_pf = hs.get('profit_factor', 0)
        print(f"  验证区间:     {holdout_result.get('period', 'N/A')}")
        print(f"  样本外交易:   {holdout_result.get('trades_count', 0)}笔")
        print(f"  样本外胜率:   {hold_wr:.1f}% (全样本: {full_wr:.1f}%)")
        print(f"  样本外盈亏比: {hold_pf:.2f} (全样本: {full_pf:.2f})")
        print(f"  样本外每笔:   {hs.get('expectancy', 0):+.2f}% (全样本: {v5_stats.get('expectancy', 0):+.2f}%)")
        # 泛化判定
        if hold_wr >= full_wr * 0.85 and hold_pf >= full_pf * 0.7:
            print(f"  泛化判定:     ✅ 泛化良好")
        elif hold_wr >= full_wr * 0.7:
            print(f"  泛化判定:     ⚠️ 轻度过拟合")
        else:
            print(f"  泛化判定:     ❌ 过拟合风险高")
    else:
        print(f"  样本外验证失败: {holdout_result.get('error', '未知')}")

    # === 6. 卖出原因分布 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 六、卖出原因分布（V5）                                           │")
    print("└─────────────────────────────────────────────────────────────────┘")
    sell_reasons = trade_details.get("sell_reasons", {})
    if sell_reasons:
        total_trades = sum(s["count"] for s in sell_reasons.values())
        for reason, s in sorted(sell_reasons.items(), key=lambda x: -x[1]["count"]):
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            avg = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
            pct = s["count"] / total_trades * 100
            print(f"  {reason:20s}: {s['count']:>4d}笔({pct:>4.1f}%), 胜率{wr:>5.1f}%, 平均{avg:>+.2f}%")

    # === 7. 半年度表现 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 七、半年度表现（V5）                                             │")
    print("└─────────────────────────────────────────────────────────────────┘")
    hy_stats = trade_details.get("half_year_stats", {})
    for pk in sorted(hy_stats.keys()):
        s = hy_stats[pk]
        wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
        avg = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
        print(f"  {pk}: {s['count']:>4d}笔, 胜率{wr:>5.1f}%, 累计{s['total_pnl']:>+.1f}%, 每笔{avg:>+.2f}%")

    # === 8. Top10 盈利 / Top10 亏损标的 ===
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│ 八、标的盈亏排名（V5）                                           │")
    print("└─────────────────────────────────────────────────────────────────┘")
    stock_ranking = trade_details.get("stock_ranking", {})
    if stock_ranking:
        sorted_stocks = sorted(stock_ranking.items(), key=lambda x: -x[1]["total_pnl"])
        print("  Top10 盈利标的:")
        for key, s in sorted_stocks[:10]:
            code, name = key.split("|")
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            print(f"    {code} {name:8s}: {s['count']:>3d}笔, 胜率{wr:>5.1f}%, 累计{s['total_pnl']:>+.1f}%")
        print("  Top10 亏损标的:")
        for key, s in sorted_stocks[-10:]:
            code, name = key.split("|")
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            print(f"    {code} {name:8s}: {s['count']:>3d}笔, 胜率{wr:>5.1f}%, 累计{s['total_pnl']:>+.1f}%")

    print("\n" + "=" * 70)


def main():
    """执行完整回测流程"""
    total_start = time.time()

    print("=" * 70)
    print("  50万散户实战模拟回测验证")
    print(f"  区间: {START_DATE} ~ {END_DATE}")
    print(f"  初始资金: {INITIAL_CAPITAL:,}元")
    print("=" * 70)

    # 1. 加载数据
    logger.info("【步骤1】加载历史数据...")
    data_dict = load_all_data()
    if len(data_dict) < 5:
        logger.error("数据不足，回测终止")
        return

    # 2. 预计算指标
    logger.info("【步骤2】预计算技术指标...")
    precomputed_data = precompute_indicators(data_dict)

    # 3. 事件驱动引擎V2
    engine_report = run_engine_v2(data_dict, precomputed_data)

    # 4. 真实环境回测V5
    real_result = run_v5_backtest(data_dict)

    # 5. Walk-Forward
    wf_result = run_walk_forward(data_dict)

    # 6. 样本外验证
    holdout_result = run_holdout_validation(data_dict)

    # 7. 收集交易明细
    trade_details = collect_trade_details(real_result)

    # 8. 打印摘要
    print_summary_report(engine_report, real_result, wf_result, holdout_result, trade_details)

    # 9. 保存结果到JSON（供后续分析）
    results_path = os.path.join(config.OUTPUT_DIR, f"backtest_50w_results_{datetime.date.today().strftime('%Y%m%d')}.json")
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    # 序列化结果（排除不可序列化的对象）
    serializable_results = {
        "config": {
            "initial_capital": INITIAL_CAPITAL,
            "start_date": START_DATE,
            "end_date": END_DATE,
            "stock_count": len(data_dict),
        },
        "engine_v2": {},
        "v5": {},
        "walk_forward": {},
        "holdout": {},
        "trade_details": {},
    }

    # Engine V2
    if engine_report and "error" not in engine_report:
        serializable_results["engine_v2"] = {
            k: v for k, v in engine_report.items()
            if k not in ("order_log", "daily_values_df") and isinstance(v, (int, float, str, bool, type(None)))
        }

    # V5
    v5_stats = real_result.get("v5", {}).get("stats", {})
    if "error" not in v5_stats:
        serializable_results["v5"] = {
            k: v for k, v in v5_stats.items()
            if isinstance(v, (int, float, str, bool, type(None), list, dict))
        }

    # Walk-Forward
    if wf_result and "error" not in wf_result:
        serializable_results["walk_forward"] = {
            k: v for k, v in wf_result.items()
            if isinstance(v, (int, float, str, bool, type(None), list, dict))
        }

    # Holdout
    if holdout_result and "error" not in holdout_result:
        hs = holdout_result.get("stats", {})
        serializable_results["holdout"] = {
            "trades_count": holdout_result.get("trades_count", 0),
            "period": holdout_result.get("period", ""),
            "stats": {k: v for k, v in hs.items() if isinstance(v, (int, float, str, bool, type(None), list, dict))},
        }

    # Trade details
    for key, val in trade_details.items():
        serializable_results["trade_details"][key] = {}
        for sub_key, sub_val in val.items():
            if isinstance(sub_val, (int, float, str, bool, type(None))):
                serializable_results["trade_details"][key][sub_key] = sub_val
            elif isinstance(sub_val, dict):
                serializable_results["trade_details"][key][sub_key] = {
                    k: {kk: vv for kk, vv in v.items() if isinstance(vv, (int, float, str, bool, type(None), list))}
                    if isinstance(v, dict) else v
                    for k, v in sub_val.items()
                }

    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(serializable_results, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"  结果已保存: {results_path}")

    total_elapsed = time.time() - total_start
    print(f"\n  总耗时: {total_elapsed:.1f}秒")
    print(f"  结果文件: {results_path}")

    return serializable_results


if __name__ == "__main__":
    main()
