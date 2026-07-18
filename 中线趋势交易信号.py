"""
中线趋势交易信号扫描器
========================
基于「高胜率A股交易操作系统V2.0」规则
扫描股票池，输出所有中线趋势买卖信号

使用方法:
    python 中线趋势交易信号.py              # 更新数据并扫描
    python 中线趋势交易信号.py --no-update  # 使用已有数据扫描
"""

import os
import sys
import datetime
import logging

# 将trading_system加入路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "trading_system"))

import config
from data.data_loader import init_db, batch_update_all, load_daily_data
from strategy.trend_strategy import (
    compute_indicators, is_trend_up, check_buy_signal,
    check_sell_signal, generate_strategy_signal,
    compute_trailing_stop, scan_all_stocks
)
from strategy.position import calc_first_batch

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def run_scan(skip_update: bool = False):
    """执行全量扫描"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    print("=" * 60)
    print(f"  中线趋势交易信号扫描 - {today}")
    print(f"  股票池: {len(config.STOCK_POOL)}只 | 总资金: {config.TOTAL_CAPITAL:,.0f}元")
    print("=" * 60)

    # 更新数据
    conn = init_db()
    if not skip_update:
        print("\n[1/3] 更新行情数据...")
        results = batch_update_all(conn)
        for code, count in results.items():
            status = f"新增{count}条" if count > 0 else ("失败" if count < 0 else "无新数据")
            print(f"  {code}: {status}")
    else:
        print("\n[1/3] 跳过数据更新")

    # 加载数据并计算指标
    print("\n[2/3] 计算趋势指标...")
    data_dict = {}
    for code in config.STOCK_POOL:
        df = load_daily_data(code, conn, days=120)
        if not df.empty and len(df) >= config.MA_SHORT:
            df = compute_indicators(df)
            data_dict[code] = df
            latest = df.iloc[-1]
            trend = "UP" if is_trend_up(df) else "DOWN"
            print(f"  {code} {config.STOCK_POOL[code]['名称']}: "
                  f"收盘{latest['close']:.2f} MA20={latest['ma20']:.2f} "
                  f"趋势={trend}")
        else:
            print(f"  {code}: 数据不足")

    # 扫描信号
    print("\n[3/3] 扫描交易信号...")
    signals = scan_all_stocks(data_dict)

    # 输出结果
    print("\n" + "=" * 60)
    print("  扫描结果")
    print("=" * 60)

    buy_list = [(c, s) for c, s in signals if s.get("buy_signal")]
    sell_list = [(c, s) for c, s in signals if s.get("sell_signal")]
    add_list = [(c, s) for c, s in signals if s.get("add_position")]
    watch_list = [(c, s) for c, s in signals
                  if not any([s.get("buy_signal"), s.get("sell_signal"), s.get("add_position")])]

    if sell_list:
        print("\n>>> [卖出信号] (优先处理)")
        for code, sig in sell_list:
            name = config.STOCK_POOL.get(code, {}).get("名称", code)
            print(f"  {code} {name}: 卖出价 {sig.get('sell_price', '-')}")
            print(f"    原因: {sig.get('signal_reason', '')}")

    if buy_list:
        print("\n>>> [买入信号]")
        for code, sig in buy_list:
            name = config.STOCK_POOL.get(code, {}).get("名称", code)
            stock_type = config.STOCK_POOL.get(code, {}).get("类型", "龙头")
            buy_p = sig["buy_price"]
            stop_p = sig.get("stop_loss_initial", buy_p * 0.9)

            # 计算仓位
            batch = calc_first_batch(buy_p, stop_p, stock_type, config.TOTAL_CAPITAL)
            print(f"  {code} {name}: 买入价 {buy_p:.2f} | 止损 {stop_p:.2f}")
            print(f"    第一批: {batch['shares']}股 ({batch['amount']:,.0f}元)")
            print(f"    风控: {'[PASS]' if batch['pass_risk'] else '[FAIL]'} {batch['risk_msg']}")
            print(f"    原因: {sig.get('signal_reason', '')}")

    if add_list:
        print("\n>>> [加仓信号]")
        for code, sig in add_list:
            name = config.STOCK_POOL.get(code, {}).get("名称", code)
            print(f"  {code} {name}: {sig.get('signal_reason', '')}")

    if watch_list:
        print("\n>>> [观望] (无信号)")
        for code, sig in watch_list:
            name = config.STOCK_POOL.get(code, {}).get("名称", code)
            print(f"  {code} {name}: {sig.get('signal_reason', '')}")

    # 汇总
    print("\n" + "-" * 60)
    print(f"  汇总: 买入{len(buy_list)}只 | 卖出{len(sell_list)}只 | "
          f"加仓{len(add_list)}只 | 观望{len(watch_list)}只")
    print("-" * 60)

    conn.close()
    return signals


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="中线趋势交易信号扫描")
    parser.add_argument("--no-update", action="store_true", help="跳过数据更新")
    args = parser.parse_args()
    run_scan(skip_update=args.no_update)
