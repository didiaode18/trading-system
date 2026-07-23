"""
P0 一键回测入口
================
一键运行5年多因子选股回测

使用方式:
    cd trading_system
    python -m quant.run_backtest
    python -m quant.run_backtest --start 2021-01-01 --end 2026-01-01
    python -m quant.run_backtest --capital 1000000 --top 10 --rebalance 5

验收标准:
    - 一键运行无报错
    - 输出净值曲线CSV + 核心指标
    - 无未来函数（信号T日生成，T+1开盘执行）
    - T+1严格执行
    - 涨跌停模拟
    - 手续费+滑点正确扣除
"""

import os
import sys
import argparse
import logging
import datetime

# 确保项目根目录在path中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import config
from quant.universe import UniverseManager
from quant.factors import FactorEngine
from quant.engine import QuantBacktestEngine
from quant.risk_manager import RiskManager
from quant.performance import calc_performance, print_report, export_nav_curve

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("run_backtest")


def main():
    parser = argparse.ArgumentParser(description="P0 多因子选股回测")
    parser.add_argument("--start", default="2021-01-01", help="回测开始日期")
    parser.add_argument("--end", default="2026-01-01", help="回测结束日期")
    parser.add_argument("--capital", type=float, default=1_000_000, help="初始资金")
    parser.add_argument("--top", type=int, default=10, help="持仓数量")
    parser.add_argument("--rebalance", type=int, default=5, help="调仓间隔(交易日)")
    parser.add_argument("--output", default=None, help="净值曲线输出路径")
    parser.add_argument("--max-stocks", type=int, default=500, help="最大加载股票数(控制内存)")
    parser.add_argument("--no-risk", action="store_true", help="禁用P1风控(对比用)")
    parser.add_argument("--stop-pct", type=float, default=0.10, help="固定止损比例")
    parser.add_argument("--trailing-pct", type=float, default=0.08, help="移动止盈回落比例")
    args = parser.parse_args()

    print()
    print("=" * 65)
    print("  P0 多因子选股回测系统")
    print("=" * 65)
    print(f"  回测区间: {args.start} ~ {args.end}")
    print(f"  初始资金: {args.capital:,.0f} 元")
    print(f"  持仓数量: TOP {args.top}")
    print(f"  调仓频率: 每 {args.rebalance} 个交易日")
    print(f"  交易成本: 佣金万2.5 + 印花税千1 + 滑点0.1%")
    if not args.no_risk:
        print(f"  P1风控: 止损-{args.stop_pct:.0%} + ATR动态 + 移动止盈{args.trailing_pct:.0%} + 大盘择时")
    else:
        print(f"  P1风控: 已禁用(对比模式)")
    print("=" * 65)
    print()

    # ============================================================
    # Step 1: 获取股票池
    # ============================================================
    logger.info("[Step 1] 获取全A股股票池 + 风险过滤...")
    um = UniverseManager()

    try:
        # 获取全A股列表
        stocks_df = um.get_all_a_share_codes(args.end)
        # 风险过滤
        codes = um.filter_universe(stocks_df, args.end)
        logger.info(f"  过滤后股票池: {len(codes)}只")

        # 限制数量（控制内存和运行时间）
        if len(codes) > args.max_stocks:
            # 随机抽样（实际应用中应按市值/流动性排序取前N）
            import random
            random.seed(42)
            codes = random.sample(codes, args.max_stocks)
            logger.info(f"  抽样限制: {args.max_stocks}只")

    except Exception as e:
        logger.error(f"  获取股票池失败: {e}")
        logger.info("  降级方案: 使用本地已有数据...")
        codes = _get_local_codes()

    # ============================================================
    # Step 2: 加载历史数据
    # ============================================================
    logger.info(f"[Step 2] 加载历史数据 ({args.start} ~ {args.end})...")

    try:
        data_dict = um.load_data(codes, args.start, args.end)
    except Exception as e:
        logger.error(f"  数据加载失败: {e}")
        data_dict = {}

    if not data_dict:
        logger.error("  无有效数据，回测终止")
        um.close()
        return

    logger.info(f"  有效数据: {len(data_dict)}只股票")

    # ============================================================
    # Step 3: 初始化引擎
    # ============================================================
    logger.info("[Step 3] 初始化因子引擎 + 回测引擎...")

    factor_engine = FactorEngine()

    # P1风控管理器
    risk_manager = None
    if not args.no_risk:
        risk_manager = RiskManager(
            fixed_stop_pct=args.stop_pct,
            atr_multiplier=2.0,
            trailing_stop_pct=args.trailing_pct,
            max_single_position=0.15,
            use_market_timing=True,
            dynamic_slippage=True,
        )
        logger.info(f"  P1风控已启用: 止损{args.stop_pct:.0%}, ATR*2, 移动止盈{args.trailing_pct:.0%}")

    backtest_engine = QuantBacktestEngine(
        initial_capital=args.capital,
        commission=0.00025,    # 万2.5
        stamp_tax=0.001,       # 千1
        slippage=0.001,        # 0.1%
        max_positions=args.top,
        risk_manager=risk_manager,
    )

    # ============================================================
    # Step 4: 运行回测
    # ============================================================
    logger.info("[Step 4] 运行回测...")
    start_time = datetime.datetime.now()

    result = backtest_engine.run(
        data_dict=data_dict,
        factor_engine=factor_engine,
        start_date=args.start,
        end_date=args.end,
        rebalance_days=args.rebalance,
        benchmark_data=None,  # 用持仓股票数据代替基准
    )

    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    logger.info(f"  回测完成，耗时 {elapsed:.1f}秒")

    if "error" in result:
        logger.error(f"  回测失败: {result['error']}")
        um.close()
        return

    # ============================================================
    # Step 5: 绩效分析 + 输出
    # ============================================================
    logger.info("[Step 5] 绩效分析...")

    perf = calc_performance(result["daily_values"], result["trades"])
    print_report(perf, result)

    # 导出净值曲线
    output_path = args.output
    if output_path is None:
        output_dir = os.path.join(PROJECT_ROOT, "output")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "nav_curve_p0.csv")

    export_nav_curve(result["daily_values"], output_path)

    # 导出交易记录
    trades_path = output_path.replace("nav_curve", "trades")
    if not result["trades"].empty:
        result["trades"].to_csv(trades_path, index=False, encoding="utf-8-sig")
        logger.info(f"交易记录已导出: {trades_path}")

    # 清理
    um.close()

    print()
    print(f"  [完成] 净值曲线: {output_path}")
    print(f"  [完成] 交易记录: {trades_path}")
    print()


def _get_local_codes() -> list:
    """降级方案：从本地SQLite获取已有数据的股票代码"""
    import sqlite3
    try:
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT code FROM daily_kline")
        codes = [row[0] for row in cursor.fetchall()]
        conn.close()
        logger.info(f"  本地已有数据: {len(codes)}只")
        return codes
    except Exception:
        return []


if __name__ == "__main__":
    main()
