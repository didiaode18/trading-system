# -*- coding: utf-8 -*-
"""
100万全量回测 — 27只标的 × 6.5年完整牛熊周期
=============================================
基于系统现有回测基础设施，覆盖持仓+候选+扩展池+退市标的。

参数:
  - 初始资金: 100万元
  - 回测区间: 2020-01-02 ~ 2026-10-10（覆盖2020牛市→2021-2022熊市→2023震荡→2024Q4牛市→2025-2026震荡）
  - 标的: 27只（4持仓+6候选+16扩展池+1退市）
  - 策略: 趋势波段V2(事件驱动引擎) + V5真实环境(逐股)
  - 成本: 佣金万3(双边) + 印花税万5(卖出) + 过户费万0.1(双边) + 滑点0.1%

运行方式:
  python scripts/backtest/run_backtest_100w.py
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

# 路径设置
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.join(_project_root, 'trading_system'))
import config

# ============================================================
# 回测参数
# ============================================================
INITIAL_CAPITAL = 1_000_000  # 100万元
START_DATE = "2020-01-02"
END_DATE = "2026-10-10"
WARMUP_DATE = "2019-06-01"  # 预热期（MA60需要约120个交易日）

# 配置日志
os.makedirs(config.OUTPUT_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(config.OUTPUT_DIR,
                         f"backtest_100w_{datetime.date.today().strftime('%Y%m%d')}.log"),
            encoding='utf-8'
        )
    ]
)
logger = logging.getLogger("backtest_100w")


# ============================================================
# 一、构建回测标的宇宙
# ============================================================
def build_universe():
    """合并 STOCK_POOL + BACKTEST_EXTRA_POOL + BACKTEST_DELISTED_POOL"""
    universe = {}
    for pool in [config.STOCK_POOL, config.BACKTEST_EXTRA_POOL, config.BACKTEST_DELISTED_POOL]:
        for code, info in pool.items():
            if code not in universe:
                universe[code] = info
    logger.info(f"回测宇宙: {len(universe)} 只标的")
    return universe


# ============================================================
# 二、数据加载
# ============================================================
def load_all_data(universe):
    """从 SQLite 批量加载回测宇宙内全部历史数据"""
    if not os.path.exists(config.DB_PATH):
        logger.error(f"数据库不存在: {config.DB_PATH}")
        return {}

    conn = sqlite3.connect(config.DB_PATH)
    codes_str = ",".join(f"'{c}'" for c in universe.keys())
    query = f"""SELECT code, date, open, close, high, low, volume
                FROM daily_kline
                WHERE code IN ({codes_str})
                ORDER BY code, date ASC"""
    df_all = pd.read_sql(query, conn)
    conn.close()

    logger.info(f"数据库读取: {len(df_all)} 条记录")

    data_dict = {}
    for code, group in df_all.groupby("code"):
        df = group.copy()
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        df = df[df["date"] >= WARMUP_DATE].reset_index(drop=True)
        if len(df) > 100:
            data_dict[code] = df

    loaded = set(data_dict.keys())
    missing = set(universe.keys()) - loaded
    if missing:
        logger.warning(f"数据缺失({len(missing)}只): {missing}")
    logger.info(f"成功加载 {len(data_dict)} 只股票数据")
    return data_dict


# ============================================================
# 三、预计算技术指标
# ============================================================
def precompute_indicators(data_dict):
    """预计算全部股票的技术指标（避免回测时重复计算）"""
    from strategy.trend_strategy import compute_indicators

    logger.info("预计算技术指标...")
    precomputed = {}
    total = len(data_dict)

    for i, (code, df) in enumerate(data_dict.items(), 1):
        try:
            df_calc = compute_indicators(df.copy())
            precomputed[code] = df_calc
        except Exception:
            precomputed[code] = df
        if i % 10 == 0 or i == total:
            logger.info(f"  [{i}/{total}] 指标预计算完成")

    logger.info(f"指标预计算完成: {len(precomputed)} 只")
    return precomputed


# ============================================================
# 四、阶段1：逐股V5回测（快速诊断）
# ============================================================
def run_per_stock_v5(data_dict, universe):
    """逐股独立回测（V5真实环境引擎），快速诊断各标的基本表现"""
    from backtest_real import backtest_stock_v5, analyze_trades

    logger.info("=" * 60)
    logger.info("【阶段1】逐股V5真实环境回测")
    logger.info("=" * 60)

    per_stock = {}
    all_trades = []
    t0 = time.time()

    for code in data_dict:
        if code not in universe:
            continue
        df = data_dict[code]
        info = universe[code]
        # 截取回测区间
        df_bt = df[df["date"] >= START_DATE].copy()
        if len(df_bt) < 60:
            logger.info(f"  {code}({info.get('名称', '')}) 数据不足({len(df_bt)}天)，跳过")
            continue

        try:
            trades = backtest_stock_v5(df, code, info)
            if trades:
                per_stock[code] = {
                    "trades": trades,
                    "total_trades": len(trades),
                }
                all_trades.extend(trades)
                # 简单统计该标的胜率
                _wins = sum(1 for t in trades if t.get("return_pct", 0) > 0)
                _wr = _wins / len(trades) if trades else 0
                logger.info(
                    f"  {code}({info.get('名称', '')}): "
                    f"交易{len(trades)}笔, 胜率{_wr:.0%}"
                )
        except Exception as e:
            logger.warning(f"  {code} 回测失败: {e}")

    elapsed = time.time() - t0
    logger.info(f"逐股回测完成: {len(per_stock)}只, 耗时{elapsed:.1f}秒")

    # 汇总分析
    summary = analyze_trades(all_trades) if all_trades else {}
    return per_stock, summary, elapsed


# ============================================================
# 五、阶段2：事件驱动引擎V2（组合效应回测）
# ============================================================
def run_engine_v2(data_dict, precomputed_data, universe):
    """运行事件驱动回测引擎V2（趋势跟踪MA20/MA60 + Monte Carlo）"""
    from backtest.engine import BacktestEngineV2
    from backtest.broker import CostConfig, Order
    from strategy.trend_strategy import generate_strategy_signal
    from strategy.position import calc_first_batch

    logger.info("=" * 60)
    logger.info("【阶段2】事件驱动回测引擎V2（组合效应 + Monte Carlo）")
    logger.info("=" * 60)

    # 使用系统实际交易成本
    cost = CostConfig(
        buy_slippage=0.001,          # 买入滑点0.1%
        sell_slippage=0.001,         # 卖出滑点0.1%
        commission_rate=config.COMMISSION_RATE,   # 佣金万3
        min_commission=config.MIN_COMMISSION,     # 最低佣金5元
        stamp_tax_rate=config.STAMP_TAX_RATE,     # 印花税万5(卖出)
        transfer_fee_rate=config.TRANSFER_FEE_RATE,  # 过户费万0.1
    )

    engine = BacktestEngineV2(initial_capital=INITIAL_CAPITAL, cost_config=cost)

    # 策略函数（使用预计算数据）
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
                # 卖出信号
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
                # 买入信号
                elif signal.get("buy_signal") and not holding:
                    bar = feed.get_bar(code, date)
                    buy_price = signal.get("buy_price") or (bar["close"] if bar else 0)
                    stop_loss = signal.get("stop_loss_initial", buy_price * 0.9)
                    if buy_price > 0:
                        stock_type = universe.get(code, {}).get("类型", "龙头")
                        from utils.instrument import is_etf as _is_etf
                        batch = calc_first_batch(
                            buy_price, stop_loss, stock_type,
                            broker.initial_capital,
                            is_etf=_is_etf(code)
                        )
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

    # 使用回测宇宙内的数据
    sub_data = {c: data_dict[c] for c in data_dict if c in universe}
    logger.info(f"引擎使用 {len(sub_data)} 只标的")

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

    logger.info(f"  耗时: {elapsed:.1f}秒")
    logger.info(f"  累计收益: {report.get('total_return', 0):.2%}")
    logger.info(f"  年化收益: {report.get('annual_return', 0):.2%}")
    logger.info(f"  最大回撤: -{report.get('max_drawdown', 0):.2%}")
    logger.info(f"  夏普比率: {report.get('sharpe_ratio', 0):.2f}")
    logger.info(f"  交易笔数: {report.get('total_trades', 0)}")

    return report


# ============================================================
# 六、阶段3：Walk-Forward 滚动验证
# ============================================================
def run_walk_forward(data_dict, universe):
    """Walk-Forward 滚动窗口验证（训练120天+验证40天）"""
    try:
        from backtest.walk_forward import WalkForwardAnalyzer
    except ImportError:
        logger.warning("WalkForwardAnalyzer 导入失败，跳过阶段3")
        return None

    logger.info("=" * 60)
    logger.info("【阶段3】Walk-Forward 滚动验证")
    logger.info("=" * 60)

    # 选取有代表性的子集（持仓+候选池，最多10只）
    wf_codes = list(config.STOCK_POOL.keys())[:10]
    wf_data = {c: data_dict[c] for c in wf_codes if c in data_dict}

    if len(wf_data) < 3:
        logger.warning(f"Walk-Forward 数据不足({len(wf_data)}只)，跳过")
        return None

    try:
        analyzer = WalkForwardAnalyzer(
            train_days=120,
            test_days=40,
            max_windows=10,
        )
        result = analyzer.analyze(
            data_dict=wf_data,
            start_date=START_DATE,
            end_date=END_DATE,
            initial_capital=INITIAL_CAPITAL,
        )
        logger.info(f"  有效窗口: {result.get('valid_windows', 0)}/{result.get('total_windows', 0)}")
        logger.info(f"  OOS 夏普均值: {result.get('oos_sharpe_mean', 0):.2f}")
        logger.info(f"  OOS 夏普中位数: {result.get('oos_sharpe_median', 0):.2f}")
        return result
    except Exception as e:
        logger.warning(f"Walk-Forward 运行失败: {e}")
        return None


# ============================================================
# 七、阶段4：分段绩效（牛熊震荡）
# ============================================================
def run_regime_segments(data_dict, precomputed_data, universe):
    """不同市场环境下的分段表现"""
    try:
        from backtest.regime_segments import segmented_performance
    except ImportError:
        logger.warning("regime_segments 导入失败，跳过阶段4")
        return None

    logger.info("=" * 60)
    logger.info("【阶段4】分段绩效（牛/熊/震荡市场环境）")
    logger.info("=" * 60)

    # 使用引擎V2的报告数据
    # 此处简化：直接调用分段函数
    try:
        result = segmented_performance(
            data_dict=data_dict,
            start_date=START_DATE,
            end_date=END_DATE,
        )
        if result:
            for seg_name, seg_data in result.items():
                logger.info(
                    f"  {seg_name}: 收益{seg_data.get('return', 0):.1%}, "
                    f"夏普{seg_data.get('sharpe', 0):.2f}"
                )
        return result
    except Exception as e:
        logger.warning(f"分段绩效运行失败: {e}")
        return None


# ============================================================
# 八、生成综合报告
# ============================================================
def generate_report(v5_results, v5_summary, v2_report, wf_result, regime_result,
                    universe, elapsed_total):
    """生成文本+JSON格式的综合回测报告"""
    logger.info("=" * 60)
    logger.info("【报告】生成综合回测报告")
    logger.info("=" * 60)

    report_date = datetime.date.today().strftime("%Y-%m-%d")
    report = {
        "report_date": report_date,
        "config": {
            "initial_capital": INITIAL_CAPITAL,
            "start_date": START_DATE,
            "end_date": END_DATE,
            "universe_size": len(universe),
            "commission_rate": config.COMMISSION_RATE,
            "stamp_tax_rate": config.STAMP_TAX_RATE,
            "transfer_fee_rate": config.TRANSFER_FEE_RATE,
            "buy_slippage": 0.001,
            "sell_slippage": 0.001,
        },
        "phase1_v5": {
            "stocks_tested": len(v5_results),
            "summary": v5_summary,
        },
        "phase2_v2": {
            "total_return": v2_report.get("total_return", 0),
            "annual_return": v2_report.get("annual_return", 0),
            "max_drawdown": v2_report.get("max_drawdown", 0),
            "sharpe_ratio": v2_report.get("sharpe_ratio", 0),
            "calmar_ratio": v2_report.get("calmar_ratio", 0),
            "sortino_ratio": v2_report.get("sortino_ratio", 0),
            "total_trades": v2_report.get("total_trades", 0),
            "win_rate": v2_report.get("win_rate", 0),
            "profit_factor": v2_report.get("profit_factor", 0),
            "avg_holding_days": v2_report.get("avg_holding_days", 0),
            "alpha": v2_report.get("alpha", 0),
            "beta": v2_report.get("beta", 0),
            "monte_carlo": v2_report.get("monte_carlo", {}),
            "elapsed_sec": v2_report.get("elapsed_sec", 0),
        },
        "phase3_walk_forward": wf_result,
        "phase4_regime": regime_result,
        "elapsed_total_sec": round(elapsed_total, 1),
    }

    # 保存 JSON
    json_path = os.path.join(config.OUTPUT_DIR, f"backtest_100w_report_{report_date}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"JSON报告已保存: {json_path}")

    # 打印文本摘要
    _print_text_summary(report, universe)

    return report


def _print_text_summary(report, universe):
    """打印一页纸文本摘要"""
    cfg = report["config"]
    v2 = report["phase2_v2"]
    v5 = report["phase1_v5"]

    print("\n" + "=" * 70)
    print(f"  全量回测报告 | {report['report_date']}")
    print(f"  初始资金: ¥{cfg['initial_capital']:,.0f} | 区间: {cfg['start_date']} ~ {cfg['end_date']}")
    print(f"  标的: {cfg['universe_size']}只 | 成本: 佣金{cfg['commission_rate']*10000:.1f}‱ + 印花税{cfg['stamp_tax_rate']*10000:.1f}‱ + 滑点{cfg['buy_slippage']*100:.1f}%")
    print("=" * 70)

    print("\n【收益指标】")
    print(f"  累计收益率:    {v2['total_return']:>10.2%}")
    print(f"  年化收益率:    {v2['annual_return']:>10.2%}")
    print(f"  Alpha:         {v2['alpha']:>10.4f}")
    print(f"  Beta:          {v2['beta']:>10.4f}")

    print("\n【风险指标】")
    print(f"  最大回撤:      -{v2['max_drawdown']:>9.2%}")
    print(f"  夏普比率:      {v2['sharpe_ratio']:>10.2f}")
    print(f"  卡尔马比率:    {v2['calmar_ratio']:>10.2f}")
    print(f"  Sortino比率:   {v2['sortino_ratio']:>10.2f}")

    print("\n【交易统计】")
    print(f"  总交易笔数:    {v2['total_trades']:>10d}")
    print(f"  胜率:          {v2['win_rate']:>10.2%}")
    print(f"  盈亏比:        {v2['profit_factor']:>10.2f}")
    print(f"  平均持仓天数:  {v2['avg_holding_days']:>10.1f}")

    print("\n【逐股V5诊断】")
    print(f"  已测试标的:    {v5['stocks_tested']:>10d}")
    if v5.get("summary"):
        s = v5["summary"]
        print(f"  总交易笔数:    {s.get('total_trades', 0):>10d}")
        print(f"  综合胜率:      {s.get('win_rate', 0):>10.2%}")

    wf = report.get("phase3_walk_forward")
    if wf:
        print("\n【Walk-Forward】")
        print(f"  有效窗口:      {wf.get('valid_windows', 0):>10d}/{wf.get('total_windows', 0)}")
        print(f"  OOS夏普均值:   {wf.get('oos_sharpe_mean', 0):>10.2f}")
        print(f"  OOS夏普中位数: {wf.get('oos_sharpe_median', 0):>10.2f}")

    mc = v2.get("monte_carlo", {})
    if mc:
        print("\n【Monte Carlo (500次)】")
        print(f"  收益中位数:    {mc.get('median_return', 0):>10.2%}")
        print(f"  收益5%分位:    {mc.get('p5_return', 0):>10.2%}")
        print(f"  最大回撤95%:   -{mc.get('p95_max_drawdown', 0):>9.2%}")

    print(f"\n总耗时: {report['elapsed_total_sec']:.1f}秒")
    print("=" * 70)


# ============================================================
# 主流程
# ============================================================
def main():
    t_total = time.time()
    logger.info("=" * 60)
    logger.info("100万全量回测启动")
    logger.info(f"  初始资金: ¥{INITIAL_CAPITAL:,.0f}")
    logger.info(f"  回测区间: {START_DATE} ~ {END_DATE}")
    logger.info(f"  交易成本: 佣金{config.COMMISSION_RATE*10000:.1f}‱ + "
                f"印花税{config.STAMP_TAX_RATE*10000:.1f}‱ + "
                f"过户费{config.TRANSFER_FEE_RATE*100000:.1f}‾ + "
                f"滑点0.1%")
    logger.info("=" * 60)

    # 构建宇宙
    universe = build_universe()

    # 加载数据
    logger.info("\n>>> 数据加载...")
    data_dict = load_all_data(universe)
    if not data_dict:
        logger.error("无数据可回测，退出")
        return

    # 预计算指标
    logger.info("\n>>> 指标预计算...")
    precomputed = precompute_indicators(data_dict)

    # 阶段1: 逐股V5
    logger.info("\n>>> 阶段1: 逐股V5回测...")
    v5_results, v5_summary, v5_elapsed = run_per_stock_v5(data_dict, universe)

    # 阶段2: 事件驱动引擎V2
    logger.info("\n>>> 阶段2: 事件驱动引擎V2...")
    v2_report = run_engine_v2(data_dict, precomputed, universe)

    # 阶段3: Walk-Forward
    logger.info("\n>>> 阶段3: Walk-Forward...")
    wf_result = run_walk_forward(data_dict, universe)

    # 阶段4: 分段绩效
    logger.info("\n>>> 阶段4: 分段绩效...")
    regime_result = run_regime_segments(data_dict, precomputed, universe)

    # 生成报告
    elapsed_total = time.time() - t_total
    report = generate_report(v5_results, v5_summary, v2_report, wf_result,
                             regime_result, universe, elapsed_total)

    logger.info("\n回测完成！")
    return report


if __name__ == "__main__":
    main()
