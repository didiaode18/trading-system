"""
定时任务调度模块 V2.0
==================
每个交易日自动运行交易分析流程并发送邮件报告

运行方式:
  python scheduler.py              # 启动调度器（前台运行）
  python scheduler.py --install    # 安装为Windows任务计划（开机自启）
  python scheduler.py --uninstall  # 卸载Windows任务计划

调度时间（建议）:
  - 每个交易日 08:30 盘前趋势预测（基于前日收盘数据，规划当日操作）
  - 每个交易日 09:25 竞价后选股报告
  - 每个交易日 15:30 盘后分析（数据更新+信号扫描+条件单+选股+仓位分析）
  - 每个交易日 15:35 盘后趋势预测（更新数据后，预测次日走势）
  - 每周五 16:00 周度仓位分析（单独发送）
  - 非交易日（周末/法定节假日）自动跳过

邮件报告列表:
  1. [持仓趋势预测] 日期 | N只持仓分析（盘前+盘后各一封）
  2. [条件单] 东方财富智能条件单 - 日期（提前挂单）
  3. [CANSLIM选股] 日期 | 行业分布 | N只入选
  4. [仓位分析] 资金优化方案 | N项风险预警
  5. [交易系统] 每日报告 日期
"""

import os
import sys
import datetime
import logging
import argparse
import subprocess

# 确保项目根目录在sys.path中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import config

try:
    import schedule
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False

logger = logging.getLogger("scheduler")


# ============================================================
# 一、交易日判断
# ============================================================

# 中国法定节假日（需每年手动更新，或使用第三方库如chinese_calendar）
# 格式: "YYYY-MM-DD"
HOLIDAYS = set()

# 周末调休上班日（需每年手动更新）
WORKDAYS = set()


def is_trading_day(date: datetime.date = None) -> bool:
    """
    判断是否为交易日
    
    规则:
    - 周末默认非交易日（除非在WORKDAYS中）
    - 法定节假日非交易日（在HOLIDAYS中）
    - 其他日期默认为交易日
    """
    if date is None:
        date = datetime.date.today()

    # 调休上班日
    if date.strftime("%Y-%m-%d") in WORKDAYS:
        return True

    # 法定节假日
    if date.strftime("%Y-%m-%d") in HOLIDAYS:
        return False

    # 周末
    if date.weekday() >= 5:  # 5=周六, 6=周日
        return False

    return True


def next_trading_day(date: datetime.date = None) -> datetime.date:
    """获取下一个交易日"""
    if date is None:
        date = datetime.date.today()

    next_day = date + datetime.timedelta(days=1)
    while not is_trading_day(next_day):
        next_day += datetime.timedelta(days=1)
    return next_day


# ============================================================
# 二、任务执行
# ============================================================

def run_daily_task():
    """执行每日盘后分析任务（15:30运行）"""
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过")
        return

    logger.info(f"[{today}] 开始执行盘后分析...")

    try:
        # 调用main.py的完整流程
        from main import run_daily_pipeline
        signals = run_daily_pipeline(skip_update=False, report_only=False)

        buy_count = sum(1 for _, s in signals if s.get("buy_signal"))
        sell_count = sum(1 for _, s in signals if s.get("sell_signal"))

        logger.info(f"[{today}] 盘后分析完成: {buy_count}个买入信号, {sell_count}个卖出信号")

    except Exception as e:
        logger.error(f"[{today}] 任务执行失败: {e}", exc_info=True)

        # 发送错误通知邮件
        try:
            from notify.email_notify import send_email
            send_email(
                f"[交易系统] 运行异常 - {today}",
                f"<p>每日交易分析任务执行失败:</p><pre>{str(e)}</pre>"
            )
        except Exception:
            pass


def run_morning_screener():
    """竞价后选股报告（09:25运行）—— 集合竞价结束后发送前瞻性选股，盘中可操作"""
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过竞价选股")
        return

    logger.info(f"[{today}] 竞价后选股（09:25）...")

    try:
        from data.data_loader import init_db, load_daily_data
        from strategy.trend_strategy import compute_indicators
        from strategy.stock_screener import run_stock_screener, send_screener_email
        import json

        conn = init_db()
        # 加载数据（用前一日收盘数据 + 技术指标）
        data_dict = {}
        # 加载股票池 + 行业候选池的所有股票
        all_codes = set(config.STOCK_POOL.keys())
        sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
        for sector_info in sector_candidates.values():
            all_codes.update(sector_info.get("stocks", {}).keys())
        all_codes.add(config.BENCHMARK_INDEX)  # 沉深300指数

        for code in all_codes:
            df = load_daily_data(code, conn, days=120)
            if not df.empty and len(df) >= config.MA_SHORT:
                df = compute_indicators(df)
                data_dict[code] = df

        # 加载持仓
        holdings_file = config.get_holdings_file()
        holdings = {}
        if os.path.exists(holdings_file):
            with open(holdings_file, "r", encoding="utf-8") as f:
                holdings = json.load(f)

        # 运行选股引擎（前瞻性模式）
        result = run_stock_screener(data_dict, holdings)

        # 发送选股邮件
        if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            send_screener_email(result)
            logger.info(f"[{today}] 竞价选股报告发送成功 ({result['qualified_count']}只入选)")

        conn.close()
    except Exception as e:
        logger.error(f"[{today}] 竞价选股失败: {e}", exc_info=True)


def run_morning_reminder():
    """盘前条件单提醒（19:00运行）—— 发送完整条件单邮件（持仓止损+选股买入）"""
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过盘前提醒")
        return

    logger.info(f"[{today}] 盘前条件单提醒...")

    try:
        from data.data_loader import init_db, load_daily_data, get_all_candidate_codes
        from strategy.trend_strategy import compute_indicators, scan_all_stocks
        from output.eastmoney_orders import send_eastmoney_orders_email
        import json

        conn = init_db()

        # 加载持仓
        holdings_file = config.get_holdings_file()
        holdings = {}
        if os.path.exists(holdings_file):
            with open(holdings_file, "r", encoding="utf-8") as f:
                holdings = json.load(f)

        # 加载数据：全部候选池 + 所有持仓（确保选股引擎和持仓都有数据）
        data_dict = {}
        all_codes = set(get_all_candidate_codes()) | set(holdings.keys())
        for code in all_codes:
            df = load_daily_data(code, conn, days=120)
            if not df.empty and len(df) >= config.MA_SHORT:
                df = compute_indicators(df)
                data_dict[code] = df
        logger.info(f"  加载数据: {len(data_dict)}只")

        # 更新持仓现价
        for code, pos in holdings.items():
            if code in data_dict and not data_dict[code].empty:
                pos["current_price"] = data_dict[code].iloc[-1]["close"]

        # 扫描持仓信号（止损/止盈）
        signals = scan_all_stocks(data_dict, holdings)

        # 确保每只持仓都有信号条目
        signal_codes = set(code for code, _ in signals)
        for code in holdings:
            if code not in signal_codes:
                signals.append((code, {
                    "buy_signal": False,
                    "sell_signal": False,
                    "add_position": False,
                    "stop_loss_initial": holdings[code].get("buy_price", 0) * 0.90,
                    "stop_loss_current": holdings[code].get("buy_price", 0) * 0.90,
                    "signal_reason": "持仓观望（无明确信号，生成默认止损单）"
                }))

        # 为所有非持仓候选股评分，只推荐评分最高的5只买入
        MAX_BUY_RECOMMEND = 5  # 最多推荐5只买入
        buy_candidates = []  # [(score, code, new_sig), ...]
        signals_dict = {code: sig for code, sig in signals}

        for code, df in data_dict.items():
            if code in holdings or code == config.BENCHMARK_INDEX:
                continue
            if df.empty or len(df) < 20:
                continue

            latest = df.iloc[-1]
            close = latest["close"]
            stock_info = config.get_stock_info(code)
            stock_type = stock_info.get("类型", "龙头")

            # 计算买入价：取MA20和近10日低点中更接近现价的支撑位
            ma20 = latest.get("ma20", close * 0.97)
            low_10d = df["low"].iloc[-10:].min()
            support = max(ma20, low_10d) if ma20 < close else low_10d
            buy_price = round(min(support, close * 0.99), 2)
            if buy_price <= 0:
                continue

            # V6.0止损：买入价下方10%（龙头）或12%（弹性）
            stop_pct = 0.10 if stock_type == "龙头" else 0.12
            stop_loss = round(buy_price * (1 - stop_pct), 2)

            # 计算首批买入股数
            try:
                from strategy.position import calc_first_batch
                batch = calc_first_batch(buy_price, stop_loss, stock_type, config.TOTAL_CAPITAL)
                shares = batch["shares"] if batch["pass_risk"] else 0
            except Exception:
                buy_amount = config.TOTAL_CAPITAL * 0.05
                shares = int(buy_amount / buy_price / 100) * 100
            if shares <= 0:
                shares = 100

            # CANSLIM评分
            score = 0
            try:
                from strategy.stock_screener import canslim_score
                factor_result = canslim_score(df, code, data_dict)
                score = factor_result.get("total_score", 0)
            except Exception:
                ma5 = latest.get("ma5", close)
                ma10 = latest.get("ma10", close)
                if ma5 > ma10 > ma20:
                    score = 60
                elif ma5 > ma20:
                    score = 45
                else:
                    score = 30

            new_sig = {
                "buy_signal": True,
                "sell_signal": False,
                "add_position": False,
                "buy_price": buy_price,
                "stop_loss_initial": stop_loss,
                "signal_reason": f"技术支撑买入 | 评分{score} | "
                                 f"MA20={ma20:.2f} | 10日低={low_10d:.2f} | "
                                 f"止损{stop_loss:.2f}(-{stop_pct:.0%})",
                "quality_score": score,
            }
            buy_candidates.append((score, code, new_sig))

        # 按评分排序，只取前5只
        buy_candidates.sort(key=lambda x: x[0], reverse=True)
        buy_count = 0
        for score, code, new_sig in buy_candidates[:MAX_BUY_RECOMMEND]:
            if code in signals_dict:
                old_sig = signals_dict[code]
                if not old_sig.get("buy_signal") and not old_sig.get("sell_signal"):
                    old_sig.update(new_sig)
                    buy_count += 1
            else:
                signals.append((code, new_sig))
                signals_dict[code] = new_sig
                buy_count += 1

        logger.info(f"  候选{len(buy_candidates)}只，推荐前{buy_count}只买入条件单")

        # 新闻/政策风险扫描（仅预警，不影响信号）
        news_risk = {}
        if getattr(config, 'NEWS_MONITOR_ENABLED', False):
            try:
                from strategy.news_monitor import scan_news_risk
                scan_codes = list(holdings.keys()) + list(data_dict.keys())
                news_risk = scan_news_risk(scan_codes, holdings)
                alert_count = sum(1 for v in news_risk.values() if v["level"] >= 2)
                if alert_count > 0:
                    logger.info(f"  新闻风险预警: {alert_count}只")
            except Exception as e:
                logger.warning(f"  新闻扫描异常(不影响主流程): {e}")

        # 发送条件单邮件（附带新闻预警）
        send_eastmoney_orders_email(signals, holdings, data_dict, news_risk=news_risk)
        logger.info(f"[{today}] 盘前条件单提醒发送成功"
                   f"（持仓{len(holdings)}只 + 买入推荐，共{len(signals)}条信号）")

        conn.close()
    except Exception as e:
        logger.error(f"[{today}] 盘前提醒失败: {e}", exc_info=True)


def run_forecast_morning():
    """盘前趋势预测（08:30运行）—— 基于前日收盘数据，生成当日操作计划"""
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过盘前趋势预测")
        return

    logger.info(f"[{today}] 盘前趋势预测分析（08:30）...")

    try:
        from data.data_loader import init_db, load_daily_data
        from strategy.trend_strategy import compute_indicators
        from strategy.trend_forecast import TrendForecaster, send_forecast_email
        import json

        conn = init_db()
        # 加载持仓
        holdings_file = config.get_holdings_file()
        holdings = {}
        if os.path.exists(holdings_file):
            with open(holdings_file, "r", encoding="utf-8") as f:
                holdings = json.load(f)

        if not holdings:
            logger.info("  无持仓，跳过")
            conn.close()
            return

        # 加载数据
        data_dict = {}
        for code in holdings:
            df = load_daily_data(code, conn, days=120)
            if not df.empty and len(df) >= 20:
                df = compute_indicators(df)
                data_dict[code] = df

        # 执行预测分析
        forecaster = TrendForecaster()
        results = forecaster.batch_analyze(data_dict, holdings)

        # 发送邮件
        if results and config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            send_forecast_email(results)
            logger.info(f"[{today}] 盘前趋势预测发送成功 ({len(results)}只)")

        conn.close()
    except Exception as e:
        logger.error(f"[{today}] 盘前趋势预测失败: {e}", exc_info=True)


def run_forecast_afternoon():
    """盘后趋势预测（15:35运行）—— 收盘后更新数据，生成次日趋势预测"""
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳开盘后趋势预测")
        return

    logger.info(f"[{today}] 盘后趋势预测分析（15:35）...")

    try:
        from data.data_loader import init_db, batch_update_all, load_daily_data
        from strategy.trend_strategy import compute_indicators
        from strategy.trend_forecast import TrendForecaster, send_forecast_email
        import json

        conn = init_db()

        # 先更新数据（获取当日收盘数据）
        batch_update_all(conn, full_pool=False)

        # 加载持仓
        holdings_file = config.get_holdings_file()
        holdings = {}
        if os.path.exists(holdings_file):
            with open(holdings_file, "r", encoding="utf-8") as f:
                holdings = json.load(f)

        if not holdings:
            logger.info("  无持仓，跳过")
            conn.close()
            return

        # 加载数据
        data_dict = {}
        for code in holdings:
            df = load_daily_data(code, conn, days=120)
            if not df.empty and len(df) >= 20:
                df = compute_indicators(df)
                data_dict[code] = df

        # 执行预测分析
        forecaster = TrendForecaster()
        results = forecaster.batch_analyze(data_dict, holdings)

        # 发送邮件
        if results and config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            send_forecast_email(results)
            logger.info(f"[{today}] 盘后趋势预测发送成功 ({len(results)}只)")

        conn.close()
    except Exception as e:
        logger.error(f"[{today}] 盘后趋势预测失败: {e}", exc_info=True)


def run_weekly_portfolio():
    """每周五盘后仓位分析（16:00运行）"""
    today = datetime.date.today()
    if today.weekday() != 4:  # 只周五运行
        return

    logger.info(f"[{today}] 周度仓位分析...")
    try:
        from data.data_loader import init_db, load_daily_data
        from strategy.trend_strategy import compute_indicators
        from strategy.portfolio_analyzer import analyze_portfolio, send_portfolio_email
        import json

        conn = init_db()
        data_dict = {}
        for code in config.STOCK_POOL:
            df = load_daily_data(code, conn, days=120)
            if not df.empty and len(df) >= config.MA_SHORT:
                df = compute_indicators(df)
                data_dict[code] = df

        holdings_file = config.get_holdings_file()
        holdings = {}
        if os.path.exists(holdings_file):
            with open(holdings_file, "r", encoding="utf-8") as f:
                holdings = json.load(f)
        for code, pos in holdings.items():
            if code in data_dict and not data_dict[code].empty:
                pos["current_price"] = data_dict[code].iloc[-1]["close"]

        result = analyze_portfolio(holdings, data_dict)
        send_portfolio_email(result)
        logger.info(f"[{today}] 周度仓位分析发送成功")

        conn.close()
    except Exception as e:
        logger.error(f"[{today}] 周度仓位分析失败: {e}", exc_info=True)


def run_weekly_task():
    """每周日执行股票池更新提醒"""
    today = datetime.date.today()
    if today.weekday() == 6:  # 周日
        logger.info(f"[{today}] 周日提醒：请检查并更新股票池")
        try:
            from notify.email_notify import send_email
            send_email(
                f"[交易系统] 周日提醒 - {today}",
                "<p>今天是周日，请检查并更新下周的股票池(config.py)。</p>"
                "<p>同时建议运行回测验证策略参数。</p>"
            )
        except Exception:
            pass


# ============================================================
# 三、盘中监控（后台线程）
# ============================================================

_monitor_thread = None
_monitor_instance = None


def start_intraday_monitor():
    """启动盘中监控（后台线程）"""
    global _monitor_thread, _monitor_instance
    import threading

    today = datetime.date.today()
    if not is_trading_day(today):
        return

    if _monitor_thread and _monitor_thread.is_alive():
        logger.info("[盘中监控] 已在运行中")
        return

    # 加载持仓
    holdings_file = config.get_holdings_file()
    holdings = {}
    if os.path.exists(holdings_file):
        import json
        with open(holdings_file, "r", encoding="utf-8") as f:
            holdings = json.load(f)

    if not holdings:
        logger.info("[盘中监控] 无持仓，跳过监控")
        return

    # 补充必要字段
    for code, pos in holdings.items():
        pos["name"] = config.get_stock_name(code)
        if "stop_loss" not in pos:
            pos["stop_loss"] = pos["buy_price"] * (1 - config.INITIAL_STOP_LOSS_PCT)

    monitor_cfg = getattr(config, 'MONITOR_CONFIG', {})
    poll_interval = monitor_cfg.get('poll_interval', 30)

    from strategy.intraday_monitor import IntradayMonitor
    _monitor_instance = IntradayMonitor(holdings, poll_interval=poll_interval)

    def _run_monitor():
        logger.info(f"[盘中监控] 后台线程启动 (监控{len(holdings)}只, 间隔{poll_interval}秒)")
        _monitor_instance.start()
        logger.info("[盘中监控] 后台线程结束")

    _monitor_thread = threading.Thread(target=_run_monitor, daemon=True, name="IntradayMonitor")
    _monitor_thread.start()
    logger.info(f"[盘中监控] 已启动 (09:30-15:00, {len(holdings)}只持仓)")


def stop_intraday_monitor():
    """停止盘中监控"""
    global _monitor_instance
    if _monitor_instance:
        _monitor_instance.stop()
        _monitor_instance = None
        logger.info("[盘中监控] 已停止")


# ============================================================
# 四、调度器
# ============================================================

def start_scheduler():
    """启动调度器"""
    if not HAS_SCHEDULE:
        logger.error("schedule库未安装，请运行: pip install schedule")
        logger.info("或使用Windows任务计划程序手动配置")
        return

    logger.info("=" * 50)
    logger.info("  交易系统定时调度器 V2.0")
    logger.info(f"  启动时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 50)

    # 每个交易日 08:30 盘前趋势预测（基于前日收盘数据，规划当日操作）
    schedule.every().day.at("08:30").do(run_forecast_morning)

    # 每个交易日 15:35 盘后趋势预测（更新数据后，预测次日走势）
    schedule.every().day.at("15:35").do(run_forecast_afternoon)

    # 每个交易日 15:30 盘后分析（数据更新+信号+条件单）
    schedule.every().day.at("15:30").do(run_daily_task)

    # 每个交易日 09:25 竞价后选股（前瞻性选股报告，盘中可操作）
    schedule.every().day.at("09:25").do(run_morning_screener)

    # 每个交易日 19:00 盘前条件单提醒（前一天晚上发送，方便提前挂单）
    schedule.every().day.at("19:00").do(run_morning_reminder)

    # 每周五 16:00 周度仓位分析
    schedule.every().friday.at("16:00").do(run_weekly_portfolio)

    # 每周日 10:00 股票池更新提醒
    schedule.every().sunday.at("10:00").do(run_weekly_task)

    logger.info("调度器已启动，等待执行...")
    logger.info(f"  盘前趋势预测: 每个交易日 08:30")
    logger.info(f"  盘后趋势预测: 每个交易日 15:35")
    logger.info(f"  盘后分析: 每个交易日 15:30")
    logger.info(f"  竞价选股: 每个交易日 09:25")
    logger.info(f"  盘前提醒: 每个交易日 08:00")
    logger.info(f"  仓位分析: 每周五 16:00")
    logger.info(f"  周日提醒: 每周日 10:00")
    logger.info(f"  盘中监控: 每个交易日 09:30-15:00 (每30秒)")

    # 盘中监控（后台线程）
    schedule.every().day.at("09:30").do(start_intraday_monitor)
    schedule.every().day.at("15:01").do(stop_intraday_monitor)

    while True:
        schedule.run_pending()

        # 非交易时段降低检查频率
        now = datetime.datetime.now()
        if now.hour >= 16 or (now.hour < 8):
            import time
            time.sleep(300)
        else:
            import time
            time.sleep(60)


# ============================================================
# 四、Windows任务计划程序（每个报告独立任务）
# ============================================================

# 所有定时任务定义: (任务名, 时间, 命令行参数, 说明)
SCHEDULED_TASKS = [
    ("TradingSystem_MorningReminder", "19:00", "--run-morning-reminder", "盘前条件单提醒(前晚)"),
    ("TradingSystem_ForecastAM",      "08:30", "--run-forecast-am",      "盘前趋势预测"),
    ("TradingSystem_Screener",        "09:25", "--run-screener",         "竞价后选股报告"),
    ("TradingSystem_Daily",           "15:30", "--run-once",             "盘后完整分析"),
    ("TradingSystem_ForecastPM",      "15:35", "--run-forecast-pm",      "盘后趋势预测"),
    ("TradingSystem_Weekly",          "16:00", "--run-weekly",           "周五仓位分析"),
]


def install_windows_task():
    """安装所有Windows任务计划（每个报告一个独立任务）"""
    python_exe = sys.executable
    script_path = os.path.abspath(__file__)

    print("=" * 60)
    print("  安装交易系统定时任务")
    print("=" * 60)

    success_count = 0
    for task_name, time_str, arg, desc in SCHEDULED_TASKS:
        cmd = (
            f'schtasks /create /tn "{task_name}" '
            f'/tr "\\"{python_exe}\\" \\"{script_path}\\" {arg}" '
            f'/sc daily /st {time_str} '
            f'/f'
        )
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"  [OK] {desc} | 每天 {time_str} | {task_name}")
                success_count += 1
            else:
                print(f"  [FAIL] {desc}: {result.stderr.strip()}")
        except Exception as e:
            print(f"  [ERROR] {desc}: {e}")

    print(f"\n安装完成: {success_count}/{len(SCHEDULED_TASKS)} 个任务")
    if success_count < len(SCHEDULED_TASKS):
        print("提示: 部分任务安装失败，请以管理员身份运行")
    print("\n注意: 周末任务会自动跳过（脚本内部判断交易日）")


def uninstall_windows_task():
    """卸载所有Windows任务计划"""
    print("=" * 60)
    print("  卸载交易系统定时任务")
    print("=" * 60)

    for task_name, _, _, desc in SCHEDULED_TASKS:
        cmd = f'schtasks /delete /tn "{task_name}" /f'
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"  [OK] 已卸载: {desc} ({task_name})")
            else:
                print(f"  [跳过] {desc}: 任务不存在")
        except Exception as e:
            print(f"  [ERROR] {desc}: {e}")

    print("\n卸载完成")


# ============================================================
# 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="交易系统定时调度器")
    parser.add_argument("--install", action="store_true",
                        help="安装所有Windows定时任务")
    parser.add_argument("--uninstall", action="store_true",
                        help="卸载所有Windows定时任务")
    parser.add_argument("--run-once", action="store_true",
                        help="立即运行盘后完整分析")
    parser.add_argument("--run-forecast-am", action="store_true",
                        help="运行盘前趋势预测")
    parser.add_argument("--run-forecast-pm", action="store_true",
                        help="运行盘后趋势预测")
    parser.add_argument("--run-morning-reminder", action="store_true",
                        help="运行盘前条件单提醒")
    parser.add_argument("--run-screener", action="store_true",
                        help="运行竞价后选股报告")
    parser.add_argument("--run-weekly", action="store_true",
                        help="运行周度仓位分析")
    args = parser.parse_args()

    # 配置日志
    log_dir = config.LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"scheduler_{datetime.date.today().strftime('%Y%m%d')}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

    if args.install:
        install_windows_task()
    elif args.uninstall:
        uninstall_windows_task()
    elif args.run_once:
        run_daily_task()
    elif args.run_forecast_am:
        run_forecast_morning()
    elif args.run_forecast_pm:
        run_forecast_afternoon()
    elif args.run_morning_reminder:
        run_morning_reminder()
    elif args.run_screener:
        run_morning_screener()
    elif args.run_weekly:
        run_weekly_portfolio()
    else:
        start_scheduler()


if __name__ == "__main__":
    main()
