"""
高胜率A股交易操作系统 V3.0 - 主程序入口
=========================================
每日一键运行流程:
  1. 增量更新所有股票池日线数据
  2. 大盘状态智能识别（多特征融合）
  3. 扫描所有股票池，生成买卖信号（趋势+均值回归+多周期共振）
  4. 所有信号过风控校验
  5. 组合风险管理（相关性/HHI/VaR/再平衡）
  6. 基本面自动分析（PE/ROE/资金流）
  7. 输出条件单Excel + 文本报告
  8. 发送通知（邮件/企微/钉钉）
  9. 交易日志记录 + 绩效归因

使用方式:
  python main.py              # 完整运行
  python main.py --no-update  # 跳过数据更新
  python main.py --report     # 仅输出文本报告
  python main.py --monitor    # 启动盘中监控模式
"""

import os
import sys
import argparse
import datetime
import logging
import json
import pandas as pd

# 确保项目根目录在sys.path中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import config
from data.data_loader import init_db, batch_update_all, load_daily_data, get_all_candidate_codes
from strategy.trend_strategy import generate_strategy_signal, scan_all_stocks, compute_indicators
from strategy.position import calc_first_batch
from risk.risk_control import (RiskState, risk_check, judge_market_strength,
                                get_max_position_ratio, daily_risk_summary)
from notify.wechat_notify import (notify_buy_signal, notify_sell_signal,
                                   notify_risk_alert, notify_daily_summary)
from notify.email_notify import send_daily_report, send_risk_alert
from output.condition_sheet import generate_condition_sheet, generate_simple_report
from output.eastmoney_orders import send_eastmoney_orders_email
from strategy.stock_screener import run_stock_screener, send_screener_email
from strategy.portfolio_analyzer import analyze_portfolio, send_portfolio_email
from strategy.market_scanner import scan_market_hot_stocks, merge_scan_results_to_pool
# V3.0 新增模块
from strategy.portfolio_risk import PortfolioRiskManager, send_risk_report_email
from strategy.fundamental import FundamentalAnalyzer
from strategy.mean_reversion import MeanReversionStrategy
from strategy.multi_timeframe import MultiTimeframeAnalyzer
from strategy.capital_flow import CapitalFlowAnalyzer
from strategy.market_regime import MarketRegimeDetector
from strategy.trade_journal import TradeJournal
from strategy.trend_forecast import TrendForecaster, send_forecast_email
# V7.1 新增模块
from strategy.anti_manipulation import AntiManipulationAnalyzer
from strategy.consensus import batch_consensus

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
# 首次运行自动检测（可迁移性）
# ============================================================
def _is_db_empty() -> bool:
    """检查数据库是否为空（无任何股票数据）"""
    if not os.path.exists(config.DB_PATH):
        return True
    try:
        import sqlite3
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM daily_kline")
        count = cursor.fetchone()[0]
        conn.close()
        return count == 0
    except Exception:
        return True


def _auto_bootstrap():
    """首次运行自动初始化：创建目录 + 拉取历史数据"""
    logger.info("检测到首次运行，自动初始化数据...")
    logger.info("  拉取历史行情数据（约需3-5分钟）...")
    try:
        conn = init_db()
        results = batch_update_all(conn, full_pool=True)
        success = sum(1 for v in results.values() if v > 0)
        logger.info(f"  初始化完成: {success}/{len(results)}只股票数据就绪")
        conn.close()
    except Exception as e:
        logger.error(f"  自动初始化失败: {e}")
        logger.info("  请手动运行: python setup.py")


if _is_db_empty():
    _auto_bootstrap()


# ============================================================
# 持仓数据加载（从本地JSON文件读取）
# ============================================================
HOLDINGS_FILE = config.get_holdings_file()

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
    global HOLDINGS_FILE
    HOLDINGS_FILE = config.get_holdings_file()
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
    global HOLDINGS_FILE
    HOLDINGS_FILE = config.get_holdings_file()
    with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)


# ============================================================
# 指标计算缓存（避免同一运行周期内重复计算）
# ============================================================
_indicator_cache = {}  # {code: (last_date, df)}

def compute_indicators_cached(code: str, df: pd.DataFrame) -> pd.DataFrame:
    """带缓存的指标计算：同一(code, last_date)不重复计算"""
    if df.empty:
        return df
    last_date = df.iloc[-1]["date"] if "date" in df.columns else ""
    cache_key = code
    if cache_key in _indicator_cache:
        cached_date, cached_df = _indicator_cache[cache_key]
        if cached_date == last_date and len(cached_df) == len(df):
            return cached_df
    result = compute_indicators(df)
    _indicator_cache[cache_key] = (last_date, result)
    return result


# ============================================================
# 主流程
# ============================================================

def run_daily_pipeline(skip_update: bool = False, report_only: bool = False):
    """
    执行每日交易分析流程
    """
    start_time = datetime.datetime.now()
    logger.info("=" * 60)
    logger.info(f"  高胜率A股交易操作系统 V3.0")
    logger.info(f"  运行时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # ---- Step 1: 更新数据（全候选池）----
    conn = None
    if not skip_update:
        logger.info("[Step 1] 增量更新行情数据（全候选池）...")
        try:
            conn = init_db()
            results = batch_update_all(conn, full_pool=True)
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

        # 盘后多源校验：确保收盘价准确
        try:
            from data.data_loader import validate_close_prices
            validation = validate_close_prices(list(config.STOCK_POOL.keys()))
            mismatch = sum(1 for v in validation.values() if v["status"] == "mismatch")
            fixed = sum(1 for v in validation.values() if v["status"] == "fixed")
            if mismatch or fixed:
                logger.info(f"  多源校验: {fixed}只已修复, {mismatch}只仍偏差")
        except Exception as e:
            logger.debug(f"  多源校验跳过: {e}")
    else:
        logger.info("[Step 1] 跳过数据更新（--no-update）")
        conn = init_db()

    # ---- Step 2: 加载数据并判定行情强度（全候选池）----
    logger.info("[Step 2] 加载数据，判定行情强度...")
    data_dict = {}
    # 加载所有候选股数据（STOCK_POOL + SECTOR_CANDIDATES）
    all_codes = get_all_candidate_codes()
    for code in all_codes:
        df = load_daily_data(code, conn, days=120)
        if not df.empty and len(df) >= config.MA_SHORT:
            df = compute_indicators_cached(code, df)
            data_dict[code] = df
    logger.info(f"  成功加载 {len(data_dict)} 只股票数据（候选池共{len(all_codes)}只）")

    # 加载基准指数判定行情
    benchmark_df = load_daily_data(config.BENCHMARK_INDEX, conn, days=120)
    if not benchmark_df.empty:
        market_strength = judge_market_strength(benchmark_df)
    else:
        market_strength = "normal"
        logger.warning("  基准指数数据不足，默认判定为震荡行情")

    max_pos = get_max_position_ratio(market_strength)
    logger.info(f"  行情强度: {market_strength} | 仓位上限: {max_pos:.0%}")

    # ---- Step 2.5: 大盘状态智能识别（V3.0新增）----
    market_regime_result = None
    if getattr(config, 'STRATEGY_CONFIG', {}).get('market_regime', {}).get('enabled', True):
        logger.info("[Step 2.5] 大盘状态智能识别...")
        try:
            detector = MarketRegimeDetector()
            if not benchmark_df.empty and len(benchmark_df) >= 60:
                benchmark_with_indicators = compute_indicators(benchmark_df)
                market_regime_result = detector.detect(benchmark_with_indicators)
                logger.info(f"  {market_regime_result['detail']}")
                # 用智能识别结果覆盖简单判断
                regime_state = market_regime_result["state"]
                if regime_state == "BULL":
                    market_strength = "strong"
                elif regime_state == "BEAR":
                    market_strength = "weak"
                else:
                    market_strength = "normal"
                max_pos = get_max_position_ratio(market_strength)
                # 输出策略建议
                advice = detector.get_strategy_advice(market_regime_result)
                logger.info(f"  主策略: {advice['primary_strategy']}")
                logger.info(f"  仓位范围: {advice['position_range'][0]:.0%}-{advice['position_range'][1]:.0%}")
            else:
                logger.info("  基准数据不足，跳过大盘状态识别")
        except Exception as e:
            logger.warning(f"  大盘状态识别异常: {e}")

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

    # ---- Step 4.5: 新闻/政策风险扫描（仅预警，不产生信号）----
    news_risk = {}
    if getattr(config, 'NEWS_MONITOR_ENABLED', False):
        logger.info("[Step 4.5] 新闻/政策风险扫描...")
        try:
            from strategy.news_monitor import scan_news_risk
            scan_codes = list(holdings.keys()) + list(data_dict.keys())
            news_risk = scan_news_risk(scan_codes, holdings)
            alert_count = sum(1 for v in news_risk.values() if v["level"] >= 2)
            logger.info(f"  扫描{len(scan_codes)}只, 风险预警{alert_count}只")
        except Exception as e:
            logger.warning(f"  新闻扫描异常(不影响主流程): {e}")

    # ---- Step 5: 扫描所有股票，生成信号 ----
    logger.info("[Step 5] 扫描股票池，生成交易信号...")
    signals = scan_all_stocks(data_dict, holdings)

    buy_count = sum(1 for _, s in signals if s.get("buy_signal"))
    sell_count = sum(1 for _, s in signals if s.get("sell_signal"))
    add_count = sum(1 for _, s in signals if s.get("add_position"))
    wash_warnings = sum(1 for _, s in signals if s.get("wash_trading_warning"))
    logger.info(f"  信号统计: 买入{buy_count}只, 卖出{sell_count}只, 加仓{add_count}只, 洗盘预警{wash_warnings}只")

    # ---- Step 5.1: 反主力操控分析（V7.1新增）----
    logger.info("[Step 5.1] 反主力操控分析...")
    manipulation_results = {}
    try:
        manip_analyzer = AntiManipulationAnalyzer()
        # 只对持仓股和重点股分析
        manip_codes = list(holdings.keys()) + [code for code, _ in signals[:10]]
        for code in set(manip_codes):
            if code in data_dict and not data_dict[code].empty:
                holding = holdings.get(code)
                manipulation_results[code] = manip_analyzer.analyze(code, data_dict[code], holding)
        
        # 统计洗盘/诱多/诱空
        wash_count = sum(1 for r in manipulation_results.values() if r.get("wash_trading"))
        bull_trap_count = sum(1 for r in manipulation_results.values() if r.get("bull_trap"))
        bear_trap_count = sum(1 for r in manipulation_results.values() if r.get("bear_trap"))
        logger.info(f"  分析{len(manipulation_results)}只 | 疑似洗盘:{wash_count} 诱多:{bull_trap_count} 诱空:{bear_trap_count}")
        
        # 将主力评分添加到信号中
        for code, sig in signals:
            if code in manipulation_results:
                sig["manipulation_score"] = manipulation_results[code].get("manipulation_score", 50)
                sig["manipulation_detail"] = manipulation_results[code].get("detail", "")
    except Exception as e:
        logger.warning(f"  反主力分析异常: {e}")

    # ---- Step 5.2: 多空共识计算（V7.1新增）----
    logger.info("[Step 5.2] 多空共识计算...")
    consensus_results = {}
    try:
        consensus_results = batch_consensus(
            data_dict, holdings, signals,
            forecast_results=None,  # 预测结果在Step14后更新
            manipulation_results=manipulation_results
        )
        # 将共识结果添加到信号中
        for code, sig in signals:
            if code in consensus_results:
                sig["consensus"] = consensus_results[code]
        
        # 统计共识方向
        bullish = sum(1 for r in consensus_results.values() if "多" in r.get("direction", ""))
        bearish = sum(1 for r in consensus_results.values() if "空" in r.get("direction", ""))
        conflict = sum(1 for r in consensus_results.values() if r.get("conflict"))
        logger.info(f"  共识统计: 看多{bullish} 看空{bearish} 分歧{conflict}")
    except Exception as e:
        logger.warning(f"  共识计算异常: {e}")

    # ---- Step 5.5: 多策略引擎（V3.0新增）----
    mr_signals = []
    mtf_results = []
    strategy_cfg = getattr(config, 'STRATEGY_CONFIG', {})

    # 多周期共振分析
    if strategy_cfg.get('multi_timeframe', {}).get('enabled', True):
        logger.info("[Step 5.5a] 多周期共振分析...")
        try:
            mtf = MultiTimeframeAnalyzer()
            mtf_results = mtf.batch_analyze(data_dict, holdings)
            strong_resonance = [r for r in mtf_results if r["resonance_score"] >= 4 and not r.get("in_holdings")]
            if strong_resonance:
                logger.info(f"  强共振(≥4分): {len(strong_resonance)}只")
                for r in strong_resonance[:5]:
                    logger.info(f"    {r['code']} {r['name']}: {r['detail']}")
        except Exception as e:
            logger.warning(f"  多周期分析异常: {e}")

    # 均值回归策略（弱势/震荡市启用）
    if strategy_cfg.get('mean_reversion', {}).get('enabled', True) and market_strength != "strong":
        logger.info("[Step 5.5b] 均值回归策略扫描...")
        try:
            mr = MeanReversionStrategy()
            mr_signals = mr.scan_reversion_signals(data_dict, market_strength, holdings)
            if mr_signals:
                logger.info(f"  发现{len(mr_signals)}个反弹信号:")
                for sig in mr_signals[:3]:
                    logger.info(f"    {sig['code']} {sig['name']}: "
                               f"强度{sig['signal_strength']}/5 | {sig['reason']}")
        except Exception as e:
            logger.warning(f"  均值回归扫描异常: {e}")

    # ---- Step 6: 信号过风控 ----
    logger.info("[Step 6] 信号风控校验...")
    filtered_signals = []
    for code, sig in signals:
        stock_info = config.get_stock_info(code)
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
            name = config.get_stock_name(code)
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

        # 发送东方财富条件单邮件
        try:
            order_ok = send_eastmoney_orders_email(filtered_signals, holdings, data_dict,
                                                    news_risk=news_risk)
            if order_ok:
                logger.info("  东方财富条件单邮件发送成功")
            else:
                logger.warning("  东方财富条件单邮件发送失败")
        except Exception as e:
            logger.error(f"  条件单邮件发送异常: {e}")
    else:
        logger.info("  邮箱未配置（EMAIL_SENDER或EMAIL_AUTH_CODE为空），跳过邮件发送")

    # ---- Step 9: 盘后选股 + 全市场扫描 + 持仓诊断 ----
    logger.info("[Step 9] 盘后选股引擎（全赛道+弱势模式）...")
    try:
        # 全市场动态扫描，发现强势股补充候选池
        logger.info("  [市场扫描] 尝试全市场动态扫描...")
        try:
            scan_result = scan_market_hot_stocks(total_max=15)
            if scan_result["success"]:
                new_codes = merge_scan_results_to_pool(scan_result, set(data_dict.keys()))
                # 拉取新发现股票的历史数据
                for code in new_codes[:10]:  # 最多追加10只
                    try:
                        df_new = load_daily_data(code, conn, days=120)
                        if not df_new.empty and len(df_new) >= config.MA_SHORT:
                            df_new = compute_indicators(df_new)
                            data_dict[code] = df_new
                    except Exception:
                        pass
                logger.info(f"  动态扫描完成，新增{len(new_codes)}只候选股")
            else:
                logger.info("  全市场扫描未成功（可能非交易时间），使用已有候选池")
        except Exception as e:
            logger.warning(f"  市场扫描异常: {e}，继续使用已有候选池")

        # 运行选股引擎（传入全部data_dict + 持仓 + 新闻风险）
        screener_result = run_stock_screener(data_dict, holdings, news_risk=news_risk)
        
        # 输出持仓诊断结果
        if screener_result.get("holdings_diagnosis"):
            logger.info("  === 持仓诊断 ===")
            for diag in screener_result["holdings_diagnosis"]:
                logger.info(f"  {diag['code']} {diag['name']}: "
                           f"[{diag['action']}] 浮盈{diag['profit_pct']:+.1f}% | "
                           f"{diag['reason']}")
        
        # 输出观察池
        if screener_result.get("watch_list"):
            logger.info(f"  === 观察池（{len(screener_result['watch_list'])}只）===")
            for w in screener_result["watch_list"]:
                logger.info(f"  {w['code']} {w['name']} [{w['sector']}]: "
                           f"弱势评分{w['weak_score']} | {w['reason']}")
        
        if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            try:
                screener_ok = send_screener_email(screener_result)
                if screener_ok:
                    logger.info(f"  选股报告邮件发送成功 ({screener_result['qualified_count']}只入选, "
                               f"{len(screener_result.get('watch_list', []))}只观察)")
                else:
                    logger.warning("  选股报告邮件发送失败")
            except Exception as e:
                logger.error(f"  选股邮件发送异常: {e}")
        else:
            logger.info("  邮箱未配置，跳过选股邮件发送")
    except Exception as e:
        logger.error(f"  选股引擎异常: {e}")

    # ---- Step 10: 仓位管理与资金优化分析（V7.1: 引用共识）----
    logger.info("[Step 10] 仓位管理分析...")
    try:
        portfolio_result = analyze_portfolio(holdings, data_dict, consensus_results)
        if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            try:
                portfolio_ok = send_portfolio_email(portfolio_result)
                if portfolio_ok:
                    logger.info(f"  仓位分析邮件发送成功 ({len(portfolio_result['risk_alerts'])}项风险预警)")
                else:
                    logger.warning("  仓位分析邮件发送失败")
            except Exception as e:
                logger.error(f"  仓位分析邮件发送异常: {e}")
        else:
            logger.info("  邮箱未配置，跳过仓位分析邮件发送")
    except Exception as e:
        logger.error(f"  仓位分析异常: {e}")

    # ---- Step 11: 组合风险管理（V3.0新增）----
    logger.info("[Step 11] 组合风险管理（相关性/HHI/VaR/再平衡）...")
    try:
        prm = PortfolioRiskManager(data_dict, holdings)
        risk_report = prm.full_risk_report()
        logger.info(f"  风险评分: {risk_report['risk_score']}/100 ({risk_report['overall_level']})")
        if risk_report['alerts']:
            for alert in risk_report['alerts'][:3]:
                logger.warning(f"  [{alert['level']}] {alert['type']}: {alert['detail']}")
        if risk_report['rebalance']['actions']:
            logger.info(f"  再平衡建议: {len(risk_report['rebalance']['actions'])}项")
            for act in risk_report['rebalance']['actions'][:3]:
                logger.info(f"    [{act['action']}] {act['name']} {act['shares']}股 | {act['reason']}")
        # 发送风控报告邮件
        if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            try:
                send_risk_report_email(risk_report)
                logger.info("  组合风控报告邮件发送成功")
            except Exception as e:
                logger.warning(f"  风控报告邮件发送失败: {e}")
    except Exception as e:
        logger.error(f"  组合风控分析异常: {e}")

    # ---- Step 12: 基本面+资金流分析（V3.0新增）----
    logger.info("[Step 12] 基本面与资金流分析...")
    try:
        # 资金流向分析
        cfa = CapitalFlowAnalyzer()
        flow_report = cfa.full_analysis(list(holdings.keys()) + list(config.STOCK_POOL.keys())[:5])
        if flow_report.get('northbound', {}).get('success'):
            logger.info(f"  北向资金: {flow_report['northbound']['signal']}")
        if flow_report.get('sector_flow', {}).get('success'):
            logger.info(f"  热门行业: {', '.join(flow_report['sector_flow']['hot_sectors'])}")

        # 基本面分析（只对持仓+重点股）
        fa = FundamentalAnalyzer()
        key_codes = list(holdings.keys()) + list(config.STOCK_POOL.keys())[:5]
        fund_scores = fa.batch_update_fundamentals(key_codes[:10])
        if fund_scores:
            logger.info("  基本面评分:")
            for code, score in sorted(fund_scores.items(),
                                      key=lambda x: x[1].get('total_score', 0), reverse=True)[:5]:
                name = config.get_stock_name(code)
                logger.info(f"    {code} {name}: {score.get('detail', 'N/A')}")
    except Exception as e:
        logger.warning(f"  基本面/资金流分析异常: {e}")

    # ---- Step 13: 交易日志+绩效归因（V3.0新增）----
    logger.info("[Step 13] 交易日志与绩效归因...")
    try:
        journal = TradeJournal()
        # 记录每日净值
        total_mv = sum(
            pos.get("shares", 0) * pos.get("current_price", pos.get("buy_price", 0))
            for pos in holdings.values()
        )
        total_value = config.TOTAL_CAPITAL
        cash = total_value - total_mv
        journal.record_daily_nav(total_value, cash)

        # 绩效报告
        perf = journal.performance_report(
            days=getattr(config, 'JOURNAL_CONFIG', {}).get('performance_days', 90)
        )
        if perf.get('total_trades', 0) > 0:
            logger.info(f"  {perf['detail']}")
        else:
            logger.info("  暂无历史交易记录，将自动记录后续信号")
    except Exception as e:
        logger.warning(f"  交易日志异常: {e}")

    # ---- Step 14: 持仓趋势预测分析（V3.1新增）----
    if getattr(config, 'FORECAST_ENABLED', True):
        logger.info("[Step 14] 持仓趋势预测分析...")
        try:
            forecaster = TrendForecaster()
            forecast_results = forecaster.batch_analyze(data_dict, holdings)
            if forecast_results:
                # 输出摘要
                for r in forecast_results:
                    score = r["composite"]["total_score"]
                    logger.info(f"  {r['code']} {r['name']}: {score:.0f}分 [{r['composite']['rating']}] "
                               f"| {r['advice']['action']} | 时间: {r['advice']['timing']['best_time']}")
                # 发送邮件
                if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
                    forecast_ok = send_forecast_email(forecast_results)
                    if forecast_ok:
                        logger.info(f"  趋势预测邮件发送成功 ({len(forecast_results)}只)")
                    else:
                        logger.warning("  趋势预测邮件发送失败")
            else:
                logger.info("  无有效持仓数据，跳过预测")
        except Exception as e:
            logger.error(f"  趋势预测分析异常: {e}")

    # ---- 完成 ----
    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    logger.info(f"\n[DONE] V3.0全流程完成，耗时 {elapsed:.1f} 秒")
    logger.info(f"  日志文件: {log_file}")

    if conn:
        conn.close()

    return filtered_signals


# ============================================================
# 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="高胜率A股交易操作系统 V3.0")
    parser.add_argument("--no-update", action="store_true",
                        help="跳过数据更新，使用已有数据")
    parser.add_argument("--report", action="store_true",
                        help="仅输出文本报告，不生成Excel")
    parser.add_argument("--monitor", action="store_true",
                        help="启动盘中实时监控模式")
    args = parser.parse_args()

    try:
        if args.monitor:
            # 盘中监控模式
            from strategy.intraday_monitor import IntradayMonitor
            holdings = load_holdings()
            for code, pos in holdings.items():
                pos["name"] = config.get_stock_name(code)
                if "stop_loss" not in pos:
                    pos["stop_loss"] = pos["buy_price"] * (1 - config.INITIAL_STOP_LOSS_PCT)
            monitor_cfg = getattr(config, 'MONITOR_CONFIG', {})
            monitor = IntradayMonitor(holdings, poll_interval=monitor_cfg.get('poll_interval', 60))
            logger.info("启动盘中监控模式 (Ctrl+C退出)...")
            monitor.start()
        else:
            run_daily_pipeline(skip_update=args.no_update, report_only=args.report)
    except KeyboardInterrupt:
        print("\n[中断] 用户取消")
    except Exception as e:
        logger.exception(f"[ERROR] 运行异常: {e}")
        raise


if __name__ == "__main__":
    main()
