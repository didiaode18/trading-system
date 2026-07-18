"""
高胜率A股交易操作系统 V2.0 - 主程序入口
=========================================
每日一键运行流程:
  1. 增量更新所有股票池日线数据
  2. 判定当前行情强度（强势/震荡/弱势）
  3. 扫描所有股票池，生成买卖信号
  4. 所有信号过风控校验
  5. 输出条件单Excel + 文本报告
  6. 发送钉钉/企微通知（如已配置）

使用方式:
  python main.py              # 完整运行（更新数据+生成信号+输出Excel）
  python main.py --no-update  # 跳过数据更新（使用已有数据）
  python main.py --report     # 仅输出文本报告（不生成Excel）
"""

import os
import sys
import argparse
import datetime
import logging
import json

# 确保项目根目录在sys.path中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import config
from data.data_loader import init_db, batch_update_all, load_daily_data
from strategy.trend_strategy import generate_strategy_signal, scan_all_stocks, compute_indicators
from strategy.position import calc_first_batch
from risk.risk_control import (RiskState, risk_check, judge_market_strength,
                                get_max_position_ratio, daily_risk_summary)
from notify.wechat_notify import (notify_buy_signal, notify_sell_signal,
                                   notify_risk_alert, notify_daily_summary)
from notify.email_notify import send_daily_report, send_risk_alert
from output.condition_sheet import generate_condition_sheet, generate_simple_report

# ============================================================
# 日志配置
# ============================================================
os.makedirs(config.LOG_DIR, exist_ok=True)
log_file = os.path.join(config.LOG_DIR,
                        f"trading_{datetime.date.today().strftime('%Y%m%d')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("main")


# ============================================================
# 持仓数据加载（从本地JSON文件读取）
# ============================================================
HOLDINGS_FILE = os.path.join(os.path.dirname(PROJECT_ROOT), "holdings.json")

def load_holdings() -> dict:
    """
    加载当前持仓数据
    持仓文件格式 holdings.json:
    {
        "002049": {
            "shares": 400,
            "buy_price": 195.0,
            "highest_price": 210.0,
            "first_batch_done": true,
            "sector": "半导体",
            "stock_type": "龙头"
        }
    }
    """
    if not os.path.exists(HOLDINGS_FILE):
        logger.info("未找到holdings.json，默认空仓")
        return {}
    try:
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            holdings = json.load(f)
        logger.info(f"加载持仓: {len(holdings)}只")
        return holdings
    except Exception as e:
        logger.error(f"加载持仓失败: {e}")
        return {}


def save_holdings(holdings: dict):
    """保存持仓数据"""
    with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)


# ============================================================
# 主流程
# ============================================================

def run_daily_pipeline(skip_update: bool = False, report_only: bool = False):
    """
    执行每日交易分析流程
    """
    start_time = datetime.datetime.now()
    logger.info("=" * 60)
    logger.info(f"  高胜率A股交易操作系统 V2.0")
    logger.info(f"  运行时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # ---- Step 1: 更新数据 ----
    conn = None
    if not skip_update:
        logger.info("[Step 1] 增量更新行情数据...")
        try:
            conn = init_db()
            results = batch_update_all(conn)
            success = sum(1 for v in results.values() if v >= 0)
            total = len(results)
            logger.info(f"  数据更新完成: {success}/{total} 只成功")
            for code, count in results.items():
                if count > 0:
                    logger.info(f"    {code}: 新增 {count} 条")
                elif count < 0:
                    logger.warning(f"    {code}: 更新失败")
        except Exception as e:
            logger.error(f"  数据更新异常: {e}")
    else:
        logger.info("[Step 1] 跳过数据更新（--no-update）")
        conn = init_db()

    # ---- Step 2: 加载数据并判定行情强度 ----
    logger.info("[Step 2] 加载数据，判定行情强度...")
    data_dict = {}
    for code in config.STOCK_POOL:
        df = load_daily_data(code, conn, days=120)
        if not df.empty and len(df) >= config.MA_SHORT:
            df = compute_indicators(df)
            data_dict[code] = df
        else:
            logger.warning(f"  {code}: 数据不足，跳过")

    # 加载基准指数判定行情
    benchmark_df = load_daily_data(config.BENCHMARK_INDEX, conn, days=120)
    if not benchmark_df.empty:
        market_strength = judge_market_strength(benchmark_df)
    else:
        market_strength = "normal"
        logger.warning("  基准指数数据不足，默认判定为震荡行情")

    max_pos = get_max_position_ratio(market_strength)
    logger.info(f"  行情强度: {market_strength} | 仓位上限: {max_pos:.0%}")

    # ---- Step 3: 加载持仓 ----
    logger.info("[Step 3] 加载当前持仓...")
    holdings = load_holdings()

    # 更新持仓中的当前价格
    for code, pos in holdings.items():
        if code in data_dict and not data_dict[code].empty:
            pos["current_price"] = data_dict[code].iloc[-1]["close"]

    # ---- Step 4: 风控状态初始化 ----
    logger.info("[Step 4] 初始化风控状态...")
    risk_state = RiskState()
    risk_state.total_capital = config.TOTAL_CAPITAL
    risk_state.update_positions(holdings)

    # ---- Step 5: 扫描所有股票，生成信号 ----
    logger.info("[Step 5] 扫描股票池，生成交易信号...")
    signals = scan_all_stocks(data_dict, holdings)

    buy_count = sum(1 for _, s in signals if s.get("buy_signal"))
    sell_count = sum(1 for _, s in signals if s.get("sell_signal"))
    add_count = sum(1 for _, s in signals if s.get("add_position"))
    logger.info(f"  信号统计: 买入{buy_count}只, 卖出{sell_count}只, 加仓{add_count}只")

    # ---- Step 6: 信号过风控 ----
    logger.info("[Step 6] 信号风控校验...")
    filtered_signals = []
    for code, sig in signals:
        stock_info = config.STOCK_POOL.get(code, {})
        if sig.get("buy_signal"):
            # 计算仓位
            buy_p = sig["buy_price"]
            stop_p = sig.get("stop_loss_initial", buy_p * 0.9)
            batch = calc_first_batch(buy_p, stop_p,
                                     stock_info.get("类型", "龙头"),
                                     config.TOTAL_CAPITAL)
            # 风控校验
            plan = {
                "code": code,
                "action": "buy",
                "price": buy_p,
                "shares": batch["shares"],
                "sector": stock_info.get("赛道", ""),
                "stock_type": stock_info.get("类型", "龙头"),
                "stop_loss": stop_p
            }
            risk_result = risk_check(plan, risk_state, market_strength)
            if risk_result["pass"]:
                sig["position"] = batch
                sig["risk_level"] = risk_result["level"]
                filtered_signals.append((code, sig))
                logger.info(f"  {code} 买入: [PASS] {batch['shares']}股 @ {buy_p}")
            else:
                logger.warning(f"  {code} 买入: [FAIL] {risk_result['reasons']}")
                sig["signal_reason"] += f" [风控拒绝: {risk_result['reasons']}]"
                filtered_signals.append((code, sig))  # 仍然保留，但标记为风控拒绝
        else:
            filtered_signals.append((code, sig))

    # ---- Step 7: 输出报告 ----
    logger.info("[Step 7] 生成报告...")

    # 文本报告
    text_report = generate_simple_report(filtered_signals)
    print("\n" + text_report)

    # 风控摘要
    risk_summary = daily_risk_summary(risk_state, market_strength)
    print("\n" + risk_summary)

    # Excel条件单
    if not report_only:
        try:
            excel_path = generate_condition_sheet(filtered_signals)
            if excel_path:
                logger.info(f"  条件单Excel: {excel_path}")
                print(f"\n  >>> 条件单已生成: {excel_path}")
        except Exception as e:
            logger.error(f"  Excel生成失败: {e}")

    # ---- Step 8: 发送通知 ----
    logger.info("[Step 8] 发送通知...")
    if config.DINGTALK_WEBHOOK or config.WECHAT_WORK_WEBHOOK:
        for code, sig in filtered_signals:
            name = config.STOCK_POOL.get(code, {}).get("名称", code)
            if sig.get("buy_signal") and sig.get("position", {}).get("pass_risk"):
                notify_buy_signal(code, name, sig["buy_price"],
                                  sig.get("stop_loss_initial", 0),
                                  sig["position"]["shares"])
            if sig.get("sell_signal"):
                notify_sell_signal(code, name, sig.get("sell_price", 0),
                                   "stop_loss", sig.get("signal_reason", ""))
        # 发送摘要
        notify_daily_summary(text_report[:1500])
    else:
        logger.info("  钉钉/企微通知渠道未配置，跳过")

    # 发送邮件报告
    if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
        try:
            email_ok = send_daily_report(text_report, risk_summary, filtered_signals)
            if email_ok:
                logger.info("  邮件报告发送成功")
            else:
                logger.warning("  邮件报告发送失败")
        except Exception as e:
            logger.error(f"  邮件发送异常: {e}")
    else:
        logger.info("  邮箱未配置（EMAIL_SENDER或EMAIL_AUTH_CODE为空），跳过邮件发送")

    # ---- 完成 ----
    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    logger.info(f"\n[DONE] 全流程完成，耗时 {elapsed:.1f} 秒")
    logger.info(f"  日志文件: {log_file}")

    if conn:
        conn.close()

    return filtered_signals


# ============================================================
# 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="高胜率A股交易操作系统 V2.0")
    parser.add_argument("--no-update", action="store_true",
                        help="跳过数据更新，使用已有数据")
    parser.add_argument("--report", action="store_true",
                        help="仅输出文本报告，不生成Excel")
    args = parser.parse_args()

    try:
        run_daily_pipeline(skip_update=args.no_update, report_only=args.report)
    except KeyboardInterrupt:
        print("\n[中断] 用户取消")
    except Exception as e:
        logger.exception(f"[ERROR] 运行异常: {e}")
        raise


if __name__ == "__main__":
    main()
