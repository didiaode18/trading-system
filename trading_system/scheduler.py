"""
定时任务调度模块 V3.0 (报告重构版)
==================
每个交易日自动运行交易分析流程并发送邮件报告

运行方式:
  python scheduler.py              # 启动调度器（前台运行）
  python scheduler.py --install    # 安装为Windows任务计划（开机自启）
  python scheduler.py --uninstall  # 卸载Windows任务计划

调度时间:
  - 每个交易日 08:30 盘前作战计划（大盘展望+操作清单+持仓快览+条件单提醒）
  - 每个交易日 15:30 盘后分析（数据更新+信号扫描+综合日报）
  - 每个交易日 19:00 条件单（东方财富智能条件单，提前挂单）
  - 每周五 16:00 周度回顾（绩效+归因+压力测试+下周建议）
  - 非交易日（周末/法定节假日）自动跳过

邮件报告列表("3+1"体系):
  1. [盘前作战计划] 日期 | N项操作
  2. [盘后综合日报] 日期 | 买N/卖N | 风险N分
  3. [条件单] 东方财富智能条件单 - 日期（提前挂单）
  4. [周度回顾] 日期 | 周收益+x.x%
  + [紧急预警] 盘中实时预警（保留）
"""

import os
import sys
import datetime
import logging
import argparse
import subprocess
import time as _time

# 确保项目根目录在sys.path中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
# FIX P1-1: 将trading-system根目录也加入sys.path，支持子模块中 "from trading_system.xxx" 的绝对导入
# （intraday_alert.py等使用 trading_system.strategy.xxx 路径，缺少此配置导致盘中异动预警持续报错）
_PROJECT_BASE = os.path.dirname(PROJECT_ROOT)
if _PROJECT_BASE not in sys.path:
    sys.path.insert(0, _PROJECT_BASE)

import config

try:
    import schedule
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False

logger = logging.getLogger("scheduler")

# ============================================================
# P0: 心跳 + 单实例锁（防止调度器静默失效）
# ============================================================

HEARTBEAT_FILE = os.path.join(PROJECT_ROOT, "output", ".scheduler_heartbeat")
PID_FILE = os.path.join(PROJECT_ROOT, "output", ".scheduler.pid")
HEARTBEAT_STALE_SECONDS = 320  # 心跳超过320秒视为死亡（需>夜间sleep(300)，避免误判）


def _write_heartbeat():
    """写入心跳文件（当前时间戳 + PID）"""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(f"{datetime.datetime.now().isoformat()}|{os.getpid()}")
    except Exception:
        pass


def is_scheduler_alive() -> bool:
    """检查调度器主循环是否存活（心跳文件是否在3分钟内更新）

    用途: Windows任务计划CLI模式执行前调用，若主循环存活则跳过，避免双重执行。
    """
    try:
        if not os.path.exists(HEARTBEAT_FILE):
            return False
        with open(HEARTBEAT_FILE, "r") as f:
            content = f.read().strip()
        ts_str = content.split("|")[0]
        last_beat = datetime.datetime.fromisoformat(ts_str)
        elapsed = (datetime.datetime.now() - last_beat).total_seconds()
        return elapsed < HEARTBEAT_STALE_SECONDS
    except Exception:
        return False


def _acquire_lock() -> bool:
    """获取单实例锁（PID文件）。若已有存活实例则返回False。"""
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            # 检查进程是否存活
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, old_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                # 进程存活，再检查心跳
                if is_scheduler_alive():
                    return False  # 已有存活实例
        # 写入当前PID
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True  # 异常时不阻塞启动


def _release_lock():
    """释放单实例锁"""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


# ============================================================
# 一、交易日判断
# ============================================================

# FIX: 填入2026年A股法定节假日和调休日，修复节假日判断失效
# 注意：具体日期以国务院年度公告为准，当前为合理预估值
HOLIDAYS = {
    # 元旦
    "2026-01-01", "2026-01-02",
    # 春节（预估，以国务院公告为准）
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22",
    # 清明节
    "2026-04-05", "2026-04-06", "2026-04-07",
    # 劳动节
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午节
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋节+国庆节
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
}

# 周末调休上班日（需每年手动更新）
WORKDAYS = {
    # 调休上班日（周末补班）
    "2026-02-14", "2026-02-28",  # 春节前后调休
    "2026-04-26",  # 劳动节调休
    "2026-10-10",  # 国庆调休
}


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

        # ---- ML预测 + 记录（不影响主流程）----
        try:
            _run_daily_ml_prediction(signals)
        except Exception as e:
            logger.error(f"[{today}] ML预测异常(不影响主流程): {e}")

        # ---- ML预测回填验证（不影响主流程）----
        try:
            _run_daily_prediction_verification()
        except Exception as e:
            logger.error(f"[{today}] ML验证异常(不影响主流程): {e}")

        # ---- IC因子监控 + 自适应权重（不影响主流程）----
        try:
            _run_daily_ic_monitoring()
        except Exception as e:
            logger.error(f"[{today}] IC监控异常(不影响主流程): {e}")

        # ---- V3.2: 预测验证闭环（不影响主流程）----
        try:
            _run_prediction_verification()
        except Exception as e:
            logger.error(f"[{today}] 预测验证异常(不影响主流程): {e}")

        # ---- V3.2: CANSLIM五因子IC记录（不影响主流程）----
        try:
            _run_canslim_ic_recording(signals)
        except Exception as e:
            logger.error(f"[{today}] CANSLIM IC记录异常(不影响主流程): {e}")

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


def run_holdings_report_task():
    """综合分析报告（16:15运行）—— 调用 generate_holdings_report.py 生成技术面+条件单报告

    与盘后综合日报(15:30 main.py Step15 [盘后日报])的区别:
      - [盘后日报]: 15步流程综合摘要(信号/ML/IC/资金/风控/展望)
      - [综合分析报告]: 逐只持仓深度技术诊断(评分/趋势/条件单/龙虎榜/融资融券/资金流)
    两者内容互补，不重复。
    """
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过综合分析报告")
        return

    logger.info(f"[{today}] 综合分析报告（16:15）...")

    try:
        # generate_holdings_report.py 是纯脚本（无函数入口），通过subprocess调用
        PROJECT_BASE = os.path.dirname(PROJECT_ROOT)  # trading-system根目录
        script_path = os.path.join(PROJECT_BASE, "generate_holdings_report.py")

        if not os.path.exists(script_path):
            logger.error(f"[{today}] 脚本不存在: {script_path}")
            return

        result = subprocess.run(
            [sys.executable, "-X", "utf8", script_path],
            cwd=PROJECT_BASE,
            capture_output=True,
            text=True,
            timeout=300,  # 5分钟超时
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode == 0:
            # 检查是否发送成功
            if "✅ 发送成功" in (result.stdout or ""):
                logger.info(f"[{today}] 综合分析报告发送成功")
            else:
                logger.warning(f"[{today}] 综合分析报告执行完成但未确认发送")
            # 打印最后几行输出
            output_lines = (result.stdout or "").strip().split("\n")
            for line in output_lines[-3:]:
                logger.info(f"  {line}")
        else:
            logger.error(f"[{today}] 综合分析报告失败(rc={result.returncode})")
            if result.stderr:
                logger.error(f"  错误: {result.stderr[-300:]}")

    except subprocess.TimeoutExpired:
        logger.error(f"[{today}] 综合分析报告超时(>300秒)")
    except Exception as e:
        logger.error(f"[{today}] 综合分析报告异常: {e}", exc_info=True)

    # FIX B2: 盘后仓位超限强制减仓单生成（解决“风控只拦新买不强制减仓”的P0缺陷）
    try:
        _generate_overlimit_reduce_orders()
    except Exception as e:
        logger.warning(f"[{today}] 仓位超限减仓单生成失败: {e}")
    
    # FIX P1: MarketRegime状态缓存（供风控引擎读取动态仓位上限）
    try:
        _cache_market_regime_state()
    except Exception as e:
        logger.warning(f"[{today}] MarketRegime缓存失败: {e}")


def _cache_market_regime_state():
    """FIX P1: 检测大盘状态并缓存到JSON，供UnifiedRiskEngine读取动态仓位上限

    输出: trading_system/output/market_regime_state.json
    消费方: risk_control.py RiskStateManager.get_market_position_cap()
    """
    import json as _json
    try:
        from strategy.market_regime import MarketRegimeDetector
        from data.data_loader import init_db, load_daily_data

        detector = MarketRegimeDetector()

        # 加载沪深300日线作为基准
        # FIX P1-2: data_loader无DataLoader类，改用模块级函数
        benchmark_df = None
        try:
            _conn = init_db()
            benchmark_df = load_daily_data("000300", _conn, days=120)
            if benchmark_df is None or benchmark_df.empty or len(benchmark_df) < 60:
                benchmark_df = load_daily_data("sh000300", _conn, days=120)
            _conn.close()
        except Exception:
            pass

        if benchmark_df is None or len(benchmark_df) < 60:
            logger.info("  [MarketRegime] 基准数据不足，跳过缓存")
            return

        result = detector.detect(benchmark_df)

        # FIX: detector返回的dict中可能含numpy类型(bool_/int64/float64)，
        # 标准json.dump无法序列化导致"Object of type bool_ is not JSON serializable"，
        # 递归转换为原生Python类型。
        def _to_native(obj):
            if isinstance(obj, dict):
                return {k: _to_native(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_to_native(v) for v in obj]
            if hasattr(obj, "item"):  # numpy标量(bool_/int64/float64等)
                try:
                    return obj.item()
                except (ValueError, AttributeError):
                    return str(obj)
            return obj

        result = _to_native(result)

        # 写入缓存文件
        output_dir = os.path.join(PROJECT_ROOT, "output")
        os.makedirs(output_dir, exist_ok=True)
        cache_file = os.path.join(output_dir, "market_regime_state.json")
        with open(cache_file, "w", encoding="utf-8") as f:
            _json.dump(result, f, ensure_ascii=False, indent=2)

        logger.info(f"  [MarketRegime] {result.get('detail', '')} → 已缓存")

    except Exception as e:
        logger.warning(f"  [MarketRegime] 检测失败: {e}")


def _generate_overlimit_reduce_orders():
    """FIX B2: 检查持仓集中度，对超限标的自动生成次日减仓执行单

    规则:
      - 个股 > 15% → 生成减仓至15%的卖出单
      - ETF > 20% → 生成减仓至20%的卖出单
      - 总仓位 > 90% → 全部持仓标记预警（不自动卖，仅日志）
    输出: 追加到次日orders JSON，QMT执行器次日开盘执行
    """
    import json as _json

    holdings_file = config.get_holdings_file()
    if not os.path.exists(holdings_file):
        return
    with open(holdings_file, "r", encoding="utf-8") as f:
        holdings = _json.load(f)

    total_capital = getattr(config, 'TOTAL_CAPITAL', 1000000)
    if total_capital <= 0:
        return

    MAX_STOCK_RATIO = 0.15
    MAX_ETF_RATIO = 0.20
    reduce_orders = []

    for code, h in holdings.items():
        shares = h.get("shares", 0)
        buy_price = h.get("buy_price", 0)
        if shares <= 0 or buy_price <= 0:
            continue
        market_value = shares * buy_price  # 用成本价估算（保守）
        ratio = market_value / total_capital

        is_etf = code.startswith("5") or code.startswith("15")
        max_ratio = MAX_ETF_RATIO if is_etf else MAX_STOCK_RATIO

        if ratio > max_ratio:
            # 计算需减仓股数（减到上限，取整百）
            target_value = total_capital * max_ratio
            excess_value = market_value - target_value
            reduce_shares = int(excess_value / buy_price / 100) * 100
            if reduce_shares < 100:
                continue
            # 触发价: 用成本价×98%作为保护性触发（次日开盘若低于此价则执行）
            trigger_price = round(buy_price * 0.98, 2)
            reduce_orders.append({
                "order_id": f"REDUCE_{code}",
                "证券代码": code,
                "证券名称": h.get("name", code),
                "方向": "卖出",
                "触发价": trigger_price,
                "数量": reduce_shares,
                "类型": "定价卖出",
                "有效期": "1个交易日",
                "触发时间": "09:30",
                "优先级": "★★★必挂",
                "说明": f"[仓位超限强制减仓] 当前占比{ratio*100:.1f}% > 上限{max_ratio*100:.0f}%, 减仓{reduce_shares}股至{max_ratio*100:.0f}%",
            })
            logger.warning(f"  [仓位超限] {h.get('name', code)} 占比{ratio*100:.1f}% > {max_ratio*100:.0f}% → 次日减仓{reduce_shares}股")

    if not reduce_orders:
        return

    # 写入次日orders文件
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    date_str = tomorrow.strftime("%Y%m%d")
    output_dir = config.OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"orders_{date_str}.json")

    # 追加到已有文件
    existing_orders = []
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing_data = _json.load(f)
            existing_orders = existing_data.get("orders", [])
        except Exception:
            pass

    orders_json = {
        "date": tomorrow.strftime("%Y-%m-%d"),
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "overlimit_auto_reduce",
        "total_capital": total_capital,
        "max_daily_trades": 10,
        "orders": existing_orders + reduce_orders,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        _json.dump(orders_json, f, ensure_ascii=False, indent=2)
    logger.info(f"  [仓位超限] 强制减仓单已生成: {len(reduce_orders)}条 → {json_path}")


# ============================================================
# V9.0 P0-3: 集合竞价分析（09:25运行）
# ============================================================

def run_auction_analysis():
    """V9.0 集合竞价分析（09:25运行）

    竞价结束后获取持仓股开盘价/竞价量，判定:
      - 高开>3% + 量能不足 → 警惕出货
      - 低开<-2% + 放量 → 恐慌抛售预警
      - 高开>5% → 紧急预警
      - 低开<-3% → 紧急预警(开盘即亏损扩大)
    """
    today = datetime.date.today()
    if not is_trading_day(today):
        return

    auction_cfg = getattr(config, 'AUCTION_CONFIG', {})
    if not auction_cfg.get("enabled", True):
        return

    logger.info(f"[{today}] V9.0 集合竞价分析...")

    try:
        import json as _json
        from data.realtime import fetch_realtime_batch

        # 加载持仓
        holdings_file = config.get_holdings_file()
        holdings = {}
        if os.path.exists(holdings_file):
            with open(holdings_file, "r", encoding="utf-8") as f:
                holdings = _json.load(f)
        if not holdings:
            return

        codes = list(holdings.keys())
        quotes = fetch_realtime_batch(codes)
        if not quotes:
            logger.warning(f"[{today}] 竞价分析: 行情获取失败")
            return

        alerts = []
        high_warn = auction_cfg.get("high_open_warn_pct", 0.03)
        high_extreme = auction_cfg.get("high_open_extreme_pct", 0.05)
        low_warn = auction_cfg.get("low_open_warn_pct", -0.02)
        low_extreme = auction_cfg.get("low_open_extreme_pct", -0.03)
        vol_surge = auction_cfg.get("volume_surge_ratio", 3.0)
        vol_weak = auction_cfg.get("volume_weak_ratio", 0.5)

        for code, quote in quotes.items():
            if code not in holdings:
                continue
            holding = holdings[code]
            name = holding.get("name", quote.get("name", code))
            open_price = quote.get("open", 0)
            prev_close = quote.get("prev_close", 0)
            volume = quote.get("volume", 0)  # 竞价量(手)
            # FIX: avg_volume fallback - holdings中无此字段时从 volume 自身推断
            avg_volume = holding.get("avg_volume", 0)

            if open_price <= 0 or prev_close <= 0:
                continue

            open_gap = (open_price - prev_close) / prev_close
            vol_ratio = volume / avg_volume if avg_volume > 0 else 1.0

            # 高开 extreme
            if open_gap >= high_extreme:
                alerts.append({
                    "level": "critical",
                    "code": code, "name": name,
                    "message": f"🚨 {name}({code}) 竞价高开+{open_gap*100:.1f}%! "
                              f"极端高开，警惕出货/利好兑现 | 建议开盘减仓",
                })
            # 高开 + 量能不足
            elif open_gap >= high_warn and vol_ratio < vol_weak:
                alerts.append({
                    "level": "warning",
                    "code": code, "name": name,
                    "message": f"⚠️ {name}({code}) 高开+{open_gap*100:.1f}%但量能不足"
                              f"(量比{vol_ratio:.1f}) | 高开无力，警惕回落",
                })
            # 高开 + 放量（利好）
            elif open_gap >= high_warn and vol_ratio > vol_surge:
                alerts.append({
                    "level": "info",
                    "code": code, "name": name,
                    "message": f"🟢 {name}({code}) 高开+{open_gap*100:.1f}% + 放量"
                              f"(量比{vol_ratio:.1f}) | 主力有备而来，关注开盘后确认",
                })
            # 低开 extreme
            elif open_gap <= low_extreme:
                loss_pct = (open_price / holding.get("buy_price", open_price) - 1) * 100
                alerts.append({
                    "level": "critical",
                    "code": code, "name": name,
                    "message": f"🚨 {name}({code}) 竞价低开{open_gap*100:.1f}%! "
                              f"开盘即亏损扩大(浮盈{loss_pct:.1f}%) | 建议开盘立即评估止损",
                })
            # 低开 + 放量
            elif open_gap <= low_warn:
                alerts.append({
                    "level": "warning",
                    "code": code, "name": name,
                    "message": f"📉 {name}({code}) 竞价低开{open_gap*100:.1f}%"
                              f"{' + 放量' if vol_ratio > vol_surge else ''} | "
                              f"恐慌情绪，注意开盘后走势",
                })

        # 发送预警
        if alerts:
            logger.info(f"[{today}] 竞价分析: {len(alerts)}条预警")
            for a in alerts:
                logger.warning(f"  [竞价-{a['level']}] {a['message']}")

            # 发邮件（仅critical或配置允许）
            critical_alerts = [a for a in alerts if a["level"] == "critical"]
            if critical_alerts and auction_cfg.get("alert_email", True):
                try:
                    from notify.email_notify import send_email
                    subject = f"[竞价预警] {today} | {len(critical_alerts)}只持仓异常"
                    html = "<div style='font-family:Microsoft YaHei;padding:20px'>"
                    html += "<h2 style='color:#FF4D4F'>🚨 集合竞价预警</h2>"
                    for a in alerts:
                        color = "#FF4D4F" if a["level"] == "critical" else "#FA8C16"
                        html += f"<p style='color:{color};font-size:14px'>{a['message']}</p>"
                    html += "</div>"
                    send_email(subject, html)
                except Exception as e:
                    logger.error(f"竞价预警邮件发送失败: {e}")
        else:
            logger.info(f"[{today}] 竞价分析: 持仓股开盘正常，无异常")

    except Exception as e:
        logger.error(f"[{today}] 竞价分析异常: {e}", exc_info=True)


# ============================================================
# V9.0 P2-1: 竞价轨迹采集（09:15启动后台线程）
# ============================================================

def run_auction_tracking_task():
    """V9.0 竞价轨迹采集（09:15启动）

    在后台线程中运行，09:15-09:25每30秒采样一次竞价价格/量，
    识别轨迹形态（诱多/真实需求/抢筹），结果写入日志。
    """
    import threading

    today = datetime.date.today()
    if not is_trading_day(today):
        return

    track_cfg = getattr(config, 'AUCTION_TRACK_CONFIG', {})
    if not track_cfg.get("enabled", True):
        return

    def _tracking_worker():
        try:
            from strategy.auction_monitor import run_auction_tracking
            logger.info(f"[{today}] 竞价轨迹采集启动 (09:15-09:25)...")
            result = run_auction_tracking()
            if result:
                logger.info(f"[{today}] 竞价轨迹采集完成: {len(result)}只股票")
                for code, info in result.items():
                    pattern = info.get("pattern", "unknown")
                    label = info.get("label", "")
                    logger.info(f"  {code}: {pattern} ({label})")
            else:
                logger.info(f"[{today}] 竞价轨迹采集: 无持仓或无数据")
        except Exception as e:
            logger.error(f"[{today}] 竞价轨迹采集异常: {e}", exc_info=True)

    t = threading.Thread(target=_tracking_worker, name="AuctionTracking", daemon=True)
    t.start()
    logger.info(f"[{today}] 竞价轨迹采集线程已启动")


def run_morning_screener():
    """竞价后选股报告（09:25运行）—— 复用report_dispatcher.run_canslim()统一入口"""
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过竞价选股")
        return

    logger.info(f"[{today}] 竞价后选股（09:25）...")

    try:
        # 统一入口：手动触发和定时触发调用同一函数，避免逻辑分叉
        PROJECT_BASE = os.path.dirname(PROJECT_ROOT)  # trading-system根目录
        if PROJECT_BASE not in sys.path:
            sys.path.insert(0, PROJECT_BASE)
        from report_dispatcher import run_canslim
        result = run_canslim()
        if result:
            logger.info(f"[{today}] 竞价选股报告发送成功 ({result['qualified_count']}只入选)")
        else:
            logger.warning(f"[{today}] 竞价选股未返回结果")
    except Exception as e:
        logger.error(f"[{today}] 竞价选股失败: {e}", exc_info=True)


def run_intraday_scan_task():
    """盘中动态扫描缓存预热（09:45/10:30运行）—— 刷新全市场强势股扫描并落盘缓存

    V3.0-FIX P1: 选股报告09:25盘前运行时无当日盘中涨幅数据，且动态扫描网络失败时
    仅能用静态池；盘中预热两次扫描后，后续选股/手动触发可用盘中真实数据降级兑底。
    仅更新缓存，不发送邮件。
    """
    today = datetime.date.today()
    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过盘中扫描")
        return
    try:
        from strategy.market_scanner import scan_market_hot_stocks
        result = scan_market_hot_stocks(total_max=15)
        if result.get("success"):
            logger.info(f"[{today}] 盘中扫描缓存已更新: {len(result['codes'])}只强势股")
        else:
            logger.warning(f"[{today}] 盘中扫描未成功（沿用现有缓存）")
    except Exception as e:
        logger.error(f"[{today}] 盘中扫描异常: {e}", exc_info=True)


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

        # V8.1: 买入推荐数量受纪律配置约束（验证: 日均18笔→巨亏）
        discipline = getattr(config, 'DISCIPLINE_CONFIG', {})
        MAX_BUY_RECOMMEND = discipline.get('max_daily_buys', 3)  # 最多推荐3只买入
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
        # FIX: 校验发送返回值。原逻辑无论邮件是否发送成功（如19:00 DNS解析失败
        # getaddrinfo failed导致3次重试全失败）都记录"发送成功"，造成通知失败不可见。
        _email_ok = send_eastmoney_orders_email(signals, holdings, data_dict, news_risk=news_risk)
        if _email_ok:
            logger.info(f"[{today}] 盘前条件单提醒发送成功"
                        f"（持仓{len(holdings)}只 + 买入推荐，共{len(signals)}条信号）")
        else:
            logger.error(f"[{today}] 盘前条件单提醒邮件发送失败（订单文件已生成，请手动补发）")

        # FIX: 同步生成QMT执行JSON，消除定时条件单与QMT执行器的断链
        try:
            from output.eastmoney_orders import _save_qmt_orders_json
            _save_qmt_orders_json(signals, holdings, data_dict)
        except Exception as e:
            logger.warning(f"  QMT JSON生成失败(不影响邮件): {e}")

        conn.close()
    except Exception as e:
        logger.error(f"[{today}] 盘前提醒失败: {e}", exc_info=True)


def run_forecast_morning():
    """盘前作战计划（08:30运行）—— 合并原盘前预测+操作清单+持仓快览+条件单提醒"""
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过盘前作战计划")
        return

    logger.info(f"[{today}] 盘前作战计划（08:30）...")

    try:
        from data.data_loader import init_db, load_daily_data
        from strategy.trend_strategy import compute_indicators
        from risk.risk_control import judge_market_strength, get_max_position_ratio
        from output.morning_brief import send_morning_brief, prepare_morning_brief_data
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

        # 更新持仓现价
        for code, pos in holdings.items():
            if code in data_dict and not data_dict[code].empty:
                pos["current_price"] = data_dict[code].iloc[-1]["close"]

        # 判断市场强度
        benchmark_df = load_daily_data(config.BENCHMARK_INDEX, conn, days=120)
        if not benchmark_df.empty:
            market_strength = judge_market_strength(benchmark_df)
        else:
            market_strength = "normal"
        max_pos = get_max_position_ratio(market_strength)

        # 准备数据并发送
        brief_data = prepare_morning_brief_data(
            holdings, data_dict,
            market_strength=market_strength,
            max_pos=max_pos
        )

        if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            send_morning_brief(brief_data)
            logger.info(f"[{today}] 盘前作战计划发送成功 ({len(holdings)}只持仓)")

        conn.close()
    except Exception as e:
        logger.error(f"[{today}] 盘前作战计划失败: {e}", exc_info=True)


def run_forecast_afternoon():
    """DEPRECATED: 盘后趋势预测已合并到盘后综合日报，本函数保留仅为CLI兼容"""
    logger.info("[DEPRECATED] 盘后预测已合并到盘后综合日报(15:30)，无需独立运行")
    return


def run_weekly_portfolio():
    """每周六周策略报告（10:00运行）—— 合并原仓位分析+V9.0模块"""
    today = datetime.date.today()
    if today.weekday() != 5:  # 只周六运行
        return

    logger.info(f"[{today}] 周度回顾...")
    try:
        from data.data_loader import init_db, load_daily_data
        from strategy.trend_strategy import compute_indicators
        from output.weekly_review import send_weekly_review, prepare_weekly_data
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

        # 准备周度数据并发送
        weekly_data = prepare_weekly_data(holdings, data_dict)
        if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            send_weekly_review(weekly_data)
            logger.info(f"[{today}] 周度回顾发送成功")

        conn.close()
    except Exception as e:
        logger.error(f"[{today}] 周度回顾失败: {e}", exc_info=True)


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
# 二-2、ML自动化（训练/预测/验证/预警）
# ============================================================

def _run_weekly_ml_training():
    """[周度] ML模型训练 - 每周六自动训练全市场模型"""
    today = datetime.date.today()
    logger.info(f"[{today}] 🤖 开始周度ML模型训练...")
    try:
        from data.data_loader import init_db, load_daily_data
        from ml.trainer import ModelTrainer
        from ml.features import prepare_dataset

        conn = init_db()
        pool = set(config.STOCK_POOL.keys())
        # 补充行业候选池
        sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
        for sector_info in sector_candidates.values():
            pool.update(sector_info.get('stocks', {}).keys())

        logger.info(f"ML训练: 股票池{len(pool)}只")

        # 获取最近300个交易日数据
        stock_data = {}
        for stock_code in pool:
            try:
                df = load_daily_data(stock_code, conn, days=300)
                if df is not None and not df.empty and len(df) > 60:
                    stock_data[stock_code] = df
            except Exception as e:
                logger.debug(f"ML训练跳过{stock_code}: {e}")

        conn.close()

        if len(stock_data) < 50:
            msg = f"可用股票不足({len(stock_data)}只)，跳过ML训练"
            logger.warning(msg)
            return

        # 准备数据集（特征工程 + 标签 + 时间衰减权重）
        X, y, temporal_weights = prepare_dataset(stock_data)

        if X.empty or y.empty:
            logger.warning("特征数据为空，跳过训练")
            return

        logger.info(f"训练数据: {len(X)}样本, {X.shape[1]}特征, "
                   f"标签分布: {dict(y.value_counts())}")

        # 执行训练
        trainer = ModelTrainer(model_type='xgboost')
        model = trainer.train(X, y, cv_folds=5, temporal_weights=temporal_weights)

        if model is None:
            logger.warning("ML训练返回空模型")
            return

        # 保存模型
        trainer.save(model, 'xgb_daily')

        # 获取交叉验证分数
        f1_mean = 0.0
        f1_std = 0.0
        try:
            from sklearn.model_selection import cross_val_score, TimeSeriesSplit
            tscv = TimeSeriesSplit(n_splits=5)
            scores = cross_val_score(model, X, y, cv=tscv, scoring='f1_macro')
            f1_mean = scores.mean()
            f1_std = scores.std()
        except Exception:
            pass

        logger.info(
            f"ML训练完成: {len(X)}样本, F1={f1_mean:.3f}±{f1_std:.3f}, "
            f"模型已保存为xgb_daily"
        )

        # 发送训练成功通知
        try:
            from notify.email_notify import send_email
            send_email(
                f"[ML训练] 周度模型训练完成 - {today}",
                f"<p>训练时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>"
                f"<p>股票池: {len(stock_data)}只</p>"
                f"<p>训练样本: {len(X)}条</p>"
                f"<p>特征维度: {X.shape[1]}</p>"
                f"<p>F1分数: {f1_mean:.3f}±{f1_std:.3f}</p>"
                f"<p>模型已保存: xgb_daily</p>"
            )
        except Exception as e:
            logger.warning(f"发送训练通知失败: {e}")

    except Exception as e:
        import traceback
        logger.error(f"ML训练异常: {e}", exc_info=True)
        try:
            from notify.email_notify import send_email
            send_email(
                f"[ML训练] 训练失败 - {today}",
                f"<p>错误: {str(e)}</p><pre>{traceback.format_exc()}</pre>"
            )
        except Exception:
            pass


def _run_daily_ml_prediction(signals=None):
    """[每日盘后] ML预测+记录 - 对候选股进行ML预测并记录到监控
    
    Args:
        signals: run_daily_pipeline返回的 (code, signal_dict) 列表
    """
    today = datetime.date.today()
    today_str = today.strftime('%Y-%m-%d')
    logger.info(f"[{today}] 🧠 ML盘后预测...")
    try:
        from ml.predictor import MLPredictor
        from ml.monitor import ModelMonitor
        from data.data_loader import init_db, load_daily_data
        from strategy.trend_strategy import compute_indicators

        predictor = MLPredictor()
        # 尝试加载已训练的模型
        if not predictor.load_model('xgb_daily'):
            logger.info("ML预测: 无可用模型，跳过")
            return

        monitor = ModelMonitor()

        # 收集需要预测的股票（信号股+持仓股+股票池）
        codes_to_predict = set()
        if signals:
            for code, sig in signals:
                codes_to_predict.add(code)
        # 补充持仓
        holdings_file = config.get_holdings_file()
        if os.path.exists(holdings_file):
            import json
            with open(holdings_file, 'r', encoding='utf-8') as f:
                holdings = json.load(f)
            codes_to_predict.update(holdings.keys())
        # 补充股票池
        codes_to_predict.update(config.STOCK_POOL.keys())

        if not codes_to_predict:
            logger.info("ML预测: 无候选股")
            return

        # 加载数据
        conn = init_db()
        prediction_count = 0
        for code in codes_to_predict:
            try:
                df = load_daily_data(code, conn, days=120)
                if df.empty or len(df) < 30:
                    continue
                df = compute_indicators(df)
                prob = predictor.predict(df)
                if prob is not None:
                    monitor.record_prediction(code, today_str, prob)
                    prediction_count += 1
            except Exception as e:
                logger.debug(f"ML预测跳过{code}: {e}")

        conn.close()
        monitor.save()
        logger.info(f"ML预测完成: {prediction_count}/{len(codes_to_predict)}只已预测并记录")

    except Exception as e:
        logger.error(f"ML盘后预测异常: {e}", exc_info=True)


def _run_daily_prediction_verification():
    """[每日盘后] 回填验证5个交易日前的ML预测"""
    today = datetime.date.today()
    logger.info(f"[{today}] 🔍 ML预测验证回填...")
    try:
        from ml.monitor import ModelMonitor
        from data.data_loader import init_db, load_daily_data

        monitor = ModelMonitor()

        # 找出5个交易日前的预测
        # 回溯7个自然日，取其中5个交易日的预测
        target_dates = set()
        d = today
        trading_days_found = 0
        for _ in range(14):  # 最多回溯14个自然日
            d = d - datetime.timedelta(days=1)
            if is_trading_day(d):
                trading_days_found += 1
                if trading_days_found == 5:
                    target_dates.add(d.strftime('%Y-%m-%d'))
                    break

        if not target_dates:
            logger.info("ML验证: 未找到5个交易日前的日期")
            return

        # 找出这些日期的未验证预测
        target_preds = [
            p for p in monitor.predictions
            if p.get('date') in target_dates and not p.get('verified')
        ]

        if not target_preds:
            logger.info(f"ML验证: {target_dates}无待验证预测")
            return

        # 加载数据计算实际收益
        conn = init_db()
        verified_count = 0
        for pred in target_preds:
            code = pred['code']
            pred_date = pred['date']
            try:
                # 加载pred_date之后5个交易日的数据
                df = load_daily_data(code, conn, days=30)
                if df.empty:
                    continue
                # 找到预测日之后的数据
                pred_dt = datetime.datetime.strptime(pred_date, '%Y-%m-%d')
                future = df[df.index > pred_dt]
                if len(future) < 5:
                    continue
                # 5日后收益
                actual_return = (future.iloc[4]['close'] - future.iloc[0]['open']) / future.iloc[0]['open']
                monitor.verify_prediction(code, pred_date, actual_return)
                verified_count += 1
            except Exception as e:
                logger.debug(f"ML验证跳过{code}@{pred_date}: {e}")

        conn.close()
        monitor.save()
        logger.info(f"ML验证完成: 回填验证{verified_count}条")

        # 检查是否需要发送准确率预警
        _check_ml_accuracy_alert(monitor)

    except Exception as e:
        logger.error(f"ML预测验证异常: {e}", exc_info=True)


def _check_ml_accuracy_alert(monitor=None):
    """检查ML准确率，低于阈值时发送预警邮件"""
    try:
        if monitor is None:
            from ml.monitor import ModelMonitor
            monitor = ModelMonitor()

        report = monitor.get_accuracy_report(days=30)
        accuracy = report.get('accuracy', 0.5)
        total = report.get('total', 0)

        if total < 20:
            return  # 样本不足，不预警

        # 连续10天 < 55% 发送预警
        if report.get('is_degraded'):
            try:
                from notify.email_notify import send_email
                trend = report.get('recent_trend', [])
                trend_str = '<br>'.join(
                    f"{t['date']}: {t['accuracy']:.1%} ({t['count']}条)"
                    for t in trend
                )
                send_email(
                    f"[ML预警] 模型准确率异常 - {datetime.date.today()}",
                    f"<p>最近30天准确率: {accuracy:.1%} ({report['correct']}/{total})</p>"
                    f"<p>状态: {'已自动降级' if accuracy < 0.45 else '降级预警'}</p>"
                    f"<p>最近7天趋势:</p><p>{trend_str}</p>"
                    f"<p>建议: 检查市场状态并考虑重新训练模型</p>"
                )
                logger.warning(f"ML准确率预警已发送: {accuracy:.1%}")
            except Exception as e:
                logger.warning(f"发送ML预警邮件失败: {e}")

    except Exception as e:
        logger.error(f"ML准确率检查异常: {e}")


# ============================================================
# 二-3、IC监控与自适应权重
# ============================================================

# 全局IC监控摘要（供日报使用）
_daily_ic_report = {}


def _run_daily_ic_monitoring():
    """[每日盘后] IC监控：计算截面IC、检测衰减、自适应权重调整"""
    global _daily_ic_report
    today = datetime.date.today()
    today_str = today.strftime('%Y-%m-%d')
    logger.info(f"[{today}] 📊 IC因子监控开始...")

    try:
        from factors.ic_monitor import ICMonitor
        from factors.registry import get_registry
        from data.data_loader import init_db, load_daily_data, get_all_candidate_codes
        from strategy.trend_strategy import compute_indicators

        # 1. 初始化ICMonitor（自动加载历史）
        monitor = ICMonitor()
        registry = get_registry()

        # 2. 加载全市场候选股数据（50+只）
        conn = init_db()
        all_codes = get_all_candidate_codes()
        # 排除基准指数
        benchmark = getattr(config, 'BENCHMARK_INDEX', '')
        stock_codes = [c for c in all_codes if c != benchmark]
        logger.info(f"  IC计算: 加载{len(stock_codes)}只候选股...")

        # 加载数据并计算因子
        stock_data = {}  # {code: df}
        for code in stock_codes:
            try:
                df = load_daily_data(code, conn, days=120)
                if df is not None and not df.empty and len(df) >= 30:
                    df = compute_indicators(df)
                    stock_data[code] = df
            except Exception:
                pass
        conn.close()

        if len(stock_data) < 10:
            logger.warning(f"IC监控: 可用股票不足({len(stock_data)}只)，跳过")
            _daily_ic_report = {"status": "skipped", "reason": "样本不足"}
            return

        logger.info(f"  IC计算: {len(stock_data)}只股票数据就绪")

        # 3. 计算每个注册因子的截面IC
        #    截面IC: 同一时间点，各股票的因子值 vs 未来5日收益的相关系数
        #    这里用最近一日作为当期，近5日收益用倒数第6到倒数第1的涨幅
        computed_count = 0
        for factor_name, factor_meta in registry.factors.items():
            if not factor_meta.enabled:
                continue
            try:
                # 构建截面数据：每只股票的因子值 + 未来收益
                factor_vals = {}
                forward_returns = {}
                for code, df in stock_data.items():
                    try:
                        fv = factor_meta.func(df)
                        if fv is not None and len(fv) == len(df):
                            # 当期因子值（最后一行）
                            factor_vals[code] = fv.iloc[-1]
                            # 未来5日收益近似：用倒数5日涨幅
                            if len(df) >= 6:
                                ret = (df['close'].iloc[-1] - df['close'].iloc[-6]) / df['close'].iloc[-6]
                                forward_returns[code] = ret
                    except Exception:
                        pass

                if len(factor_vals) < 10:
                    continue

                # 对齐并计算截面IC
                common_codes = set(factor_vals.keys()) & set(forward_returns.keys())
                if len(common_codes) < 10:
                    continue

                import pandas as pd
                f_series = pd.Series({c: factor_vals[c] for c in common_codes})
                r_series = pd.Series({c: forward_returns[c] for c in common_codes})
                ic = monitor.calc_ic(f_series, r_series)
                monitor.update(factor_name, ic, date=today_str)
                computed_count += 1
            except Exception as e:
                logger.debug(f"IC计算跳过{factor_name}: {e}")

        # 4. 检测衰减和强劲因子
        decaying_factors = monitor.get_decaying_factors()
        strong_factors = monitor.get_strong_factors(threshold=0.05)

        logger.info(f"IC监控: {computed_count}个因子已计算, "
                   f"{len(decaying_factors)}个衰减, {len(strong_factors)}个强劲")

        # 衰减因子详情日志
        for fname in decaying_factors:
            records = monitor.ic_records.get(fname, [])
            low_days = sum(
                1 for r in records[-5:]
                if abs(r['ic'] if isinstance(r, dict) else r) < 0.02
            )
            meta = registry.get(fname)
            new_weight = (meta.weight * 0.5) if meta else 0
            logger.warning(f"因子{fname}连续{low_days}天|IC|<0.02, "
                          f"权重将降至{new_weight:.2f}")

        # 强劲因子日志
        for fname in strong_factors:
            meta = registry.get(fname)
            new_weight = (meta.weight * 1.2) if meta else 0
            logger.info(f"因子{fname}连续5天|IC|>0.05, 权重将加至{new_weight:.2f}")

        # 5. 自适应权重调整
        weight_result = registry.update_weights_by_ic(ic_monitor=monitor)
        n_decay = len(weight_result.get("decaying", []))
        n_strong = len(weight_result.get("strong", []))
        if n_decay > 0 or n_strong > 0:
            logger.info(f"权重调整: {n_decay}个降权, {n_strong}个加权")

        # 6. 构建IC报告摘要（供日报使用）
        _daily_ic_report = _get_ic_report(monitor, weight_result, computed_count)

        # 7. 持久化已由monitor.update自动完成
        logger.info(f"[{today}] IC监控完成")

    except Exception as e:
        logger.error(f"IC监控异常: {e}", exc_info=True)
        _daily_ic_report = {"status": "error", "error": str(e)}


def _get_ic_report(monitor=None, weight_result=None, computed_count=0) -> dict:
    """生成IC监控摘要供日报使用
    
    返回:
        dict: {status, n_factors, decaying, strong, weight_adjustments, top_factors}
    """
    try:
        if monitor is None:
            from factors.ic_monitor import ICMonitor
            monitor = ICMonitor()

        decaying = monitor.get_decaying_factors()
        strong = monitor.get_strong_factors(threshold=0.05)
        ranked = monitor.rank_factors()

        report = {
            "status": "ok",
            "n_factors": computed_count,
            "n_decaying": len(decaying),
            "n_strong": len(strong),
            "decaying_factors": decaying,
            "strong_factors": strong,
            "top_factors": ranked[:5],  # IR最高的5个因子
            "weight_adjustments": weight_result or {},
        }
        return report

    except Exception as e:
        return {"status": "error", "error": str(e)}


# ============================================================
# V3.2: 预测验证闭环 + CANSLIM IC记录
# ============================================================

def _run_prediction_verification():
    """[每日盘后] V3.2: 验证到期的预测记录，统计准确率"""
    today = datetime.date.today()
    logger.info(f"[{today}] 🔮 预测验证闭环...")
    try:
        from monitor.prediction_tracker import PredictionTracker
        tracker = PredictionTracker()
        tracker.verify_all()  # 自动验证到期预测
        summary = tracker.get_summary_text()
        logger.info(f"[{today}] {summary}")
    except Exception as e:
        logger.error(f"预测验证失败: {e}")


def _run_canslim_ic_recording(signals=None):
    """[每日盘后] V3.2: 记录CANSLIM五因子IC（供IC降权机制使用）"""
    today = datetime.date.today()
    logger.info(f"[{today}] 📝 CANSLIM五因子IC记录...")
    try:
        from strategy.stock_screener import record_canslim_ic, canslim_score
        from data.data_loader import init_db, load_daily_data, get_all_candidate_codes
        from strategy.trend_strategy import compute_indicators

        conn = init_db()
        all_codes = get_all_candidate_codes()
        stock_codes = [c for c in all_codes if not c.startswith("300") and not c.startswith("688")]

        # 加载数据并计算评分
        data_dict = {}
        all_scores = []
        for code in stock_codes[:30]:  # 取前30只
            try:
                df = load_daily_data(code, conn, days=120)
                if df is not None and not df.empty and len(df) >= 60:
                    df = compute_indicators(df)
                    data_dict[code] = df
                    result = canslim_score(df, code)
                    if result.get("total_score", 0) > 0:
                        all_scores.append({
                            "code": code,
                            "factors": result.get("factors", {}),
                            "total_score": result.get("total_score", 0),
                        })
            except Exception:
                pass
        conn.close()

        if len(all_scores) >= 5:
            record_canslim_ic(all_scores, data_dict)
            logger.info(f"[{today}] CANSLIM IC记录完成 ({len(all_scores)}只样本)")
        else:
            logger.info(f"[{today}] 样本不足({len(all_scores)}只)，跳过IC记录")

    except Exception as e:
        logger.error(f"CANSLIM IC记录失败: {e}")


# ============================================================
# 二-4、月度Walk-Forward验证
# ============================================================

def _run_monthly_walk_forward():
    """[月度] Walk-Forward滚动窗口验证 - 每月1日自动运行，防止策略过拟合"""
    today = datetime.date.today()
    if today.day != 1 and not getattr(_run_monthly_walk_forward, '__skip_day_check__', False):
        return
    # FIX: 每月1日若逢周末/节假日则跳过
    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过Walk-Forward验证")
        return

    logger.info(f"[{today}] 🔄 开始月度Walk-Forward验证...")
    start_time = datetime.datetime.now()

    # FIX P0: 每月1日重置月度回撤基准（与 risk_control.py 第6.5关联动）
    try:
        from risk.risk_control import RiskStateManager
        _state_mgr = RiskStateManager()
        _state_mgr.state["monthly_start_capital"] = getattr(config, 'TOTAL_CAPITAL', 731455)
        _state_mgr.save()
        logger.info(f"  [月度重置] monthly_start_capital = {config.TOTAL_CAPITAL:,.2f}")
    except Exception as e:
        logger.warning(f"  月度回撤基准重置失败: {e}")

    try:
        from data.data_loader import init_db, load_daily_data
        from strategy.trend_strategy import compute_indicators
        from backtest.walk_forward import WalkForwardAnalyzer, format_walk_forward_report
        import numpy as np

        conn = init_db()

        # 1. 加载全量股票历史数据（股票池+行业候选池，300天）
        pool = set(config.STOCK_POOL.keys())
        sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
        for sector_info in sector_candidates.values():
            pool.update(sector_info.get('stocks', {}).keys())

        logger.info(f"  Walk-Forward: 加载全量{len(pool)}只数据（300日历史）...")
        data_dict = {}
        for code in pool:
            try:
                df = load_daily_data(code, conn, days=300)
                if df is not None and not df.empty and len(df) >= 80:
                    df = compute_indicators(df)
                    data_dict[code] = df
            except Exception as e:
                logger.debug(f"  Walk-Forward跳过{code}: {e}")

        conn.close()

        if len(data_dict) < 50:
            msg = f"Walk-Forward: 可用股票不足({len(data_dict)}只<50)，跳过"
            logger.warning(msg)
            return

        # 检查数据长度：至少需要 train_window + test_window = 80 天
        min_required = 80
        valid_data = {k: v for k, v in data_dict.items() if len(v) >= min_required}
        if len(valid_data) < 50:
            msg = (f"Walk-Forward: 历史数据不足{min_required}天的股票过多"
                   f"（仅{len(valid_data)}只满足，需≥50只），跳过")
            logger.warning(msg)
            return

        logger.info(f"  Walk-Forward: {len(valid_data)}只股票数据就绪（≥{min_required}天）")

        # 2. 实例化并运行Walk-Forward分析（全量股票：股票池+行业候选池）
        wf = WalkForwardAnalyzer(
            train_days=60,
            test_days=20,
            max_windows=6,  # 限制窗口数，避免运行时间过长
        )
        stock_codes = list(valid_data.keys())
        logger.info(f"  Walk-Forward: 对{len(stock_codes)}只做滚动窗口分析（可能耗时较长）...")
        result = wf.run(valid_data, stock_codes)

        if "error" in result:
            logger.error(f"  Walk-Forward分析失败: {result['error']}")
            return

        elapsed = (datetime.datetime.now() - start_time).total_seconds()
        logger.info(f"  Walk-Forward: 分析完成，耗时{elapsed:.0f}秒")

        # 3. 样本内外表现比较与预警
        # 计算样本内平均Sharpe（从各窗口的train_sharpe取均值）
        train_sharpes = [w["train_sharpe"] for w in result["windows"]]
        train_win_rates = [w.get("test_win_rate", 0) for w in result["windows"]]  # 近似
        avg_in_sample_sharpe = float(np.mean(train_sharpes)) if train_sharpes else 0
        avg_out_sample_sharpe = result.get("oos_sharpe", 0)
        avg_out_sample_win_rate = result.get("oos_win_rate", 0)

        # 预警判断
        alerts = []
        if avg_in_sample_sharpe > 0 and avg_out_sample_sharpe < avg_in_sample_sharpe * 0.5:
            alerts.append(
                f"⚠️ 样本外Sharpe({avg_out_sample_sharpe:.2f})"
                f" < 样本内Sharpe({avg_in_sample_sharpe:.2f}) × 0.5\n"
                f"   → 触发参数重优化预警：策略可能过拟合"
            )

        # 用窗口内train胜率近似样本内胜率
        avg_in_sample_wr = float(np.mean(train_win_rates)) if train_win_rates else 0
        if avg_in_sample_wr > 0 and avg_out_sample_win_rate < avg_in_sample_wr * 0.7:
            alerts.append(
                f"⚠️ 样本外胜率({avg_out_sample_win_rate:.1%})"
                f" < 样本内胜率({avg_in_sample_wr:.1%}) × 0.7\n"
                f"   → 触发参数重优化预警：胜率衰减明显"
            )

        # 4. 生成报告文本
        report_text = format_walk_forward_report(result)
        logger.info(f"\n{report_text}")

        # 预警详情日志
        if alerts:
            for alert in alerts:
                logger.warning(f"  Walk-Forward预警: {alert}")

        # 5. 发送邮件通知
        try:
            from notify.email_notify import send_email

            # 构建HTML邮件内容
            alert_html = ""
            if alerts:
                alert_items = "".join(f"<li>{a}</li>" for a in alerts)
                alert_html = (
                    f"<div style='background:#fff3cd;padding:10px;margin:10px 0;'>"
                    f"<strong>🚨 参数重优化预警</strong>"
                    f"<ul>{alert_items}</ul></div>"
                )

            # 各窗口详情
            window_rows = ""
            for i, w in enumerate(result["windows"]):
                window_rows += (
                    f"<tr>"
                    f"<td>{i+1}</td>"
                    f"<td>{w['test_period'][0]}~{w['test_period'][1]}</td>"
                    f"<td>{w['test_sharpe']:.2f}</td>"
                    f"<td>{w['test_return']:.2%}</td>"
                    f"<td>{str(w['best_params'])}</td>"
                    f"</tr>"
                )

            html_body = f"""
            <h2>Walk-Forward 滚动窗口分析报告</h2>
            <p>运行时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} | 耗时: {elapsed:.0f}秒</p>
            <p>股票数: {len(valid_data)}只 | 窗口数: {result['num_windows']} | 训练/验证: 60天/20天</p>
            {alert_html}
            <h3>样本外表现</h3>
            <table border="1" cellpadding="5" cellspacing="0">
              <tr><th>指标</th><th>数值</th></tr>
              <tr><td>平均夏普</td><td>{result['oos_sharpe']:.2f} ± {result['oos_sharpe_std']:.2f}</td></tr>
              <tr><td>平均收益</td><td>{result['oos_return']:.2%}</td></tr>
              <tr><td>平均胜率</td><td>{result['oos_win_rate']:.1%}</td></tr>
              <tr><td>平均回撤</td><td>{result['oos_max_dd']:.2%}</td></tr>
            </table>
            <h3>稳健性评估</h3>
            <table border="1" cellpadding="5" cellspacing="0">
              <tr><th>指标</th><th>数值</th></tr>
              <tr><td>参数稳定性</td><td>{result['stability_score']:.0%}</td></tr>
              <tr><td>过拟合程度</td><td>{result['overfit_ratio']:.1%}</td></tr>
              <tr><td>综合判定</td><td>{result['verdict']}</td></tr>
            </table>
            <h3>各窗口详情</h3>
            <table border="1" cellpadding="5" cellspacing="0">
              <tr><th>窗口</th><th>验证期</th><th>夏普</th><th>收益</th><th>最优参数</th></tr>
              {window_rows}
            </table>
            """

            subject_prefix = "🚨[Walk-Forward预警]" if alerts else "[Walk-Forward]"
            send_email(
                f"{subject_prefix} 月度策略验证 - {today}",
                html_body
            )
            logger.info(f"  Walk-Forward: 邮件通知发送成功")
        except Exception as e:
            logger.warning(f"  Walk-Forward: 邮件发送失败: {e}")

        logger.info(f"[{today}] Walk-Forward验证完成")

    except Exception as e:
        import traceback
        logger.error(f"Walk-Forward验证异常: {e}", exc_info=True)
        try:
            from notify.email_notify import send_email
            send_email(
                f"[Walk-Forward] 验证失败 - {today}",
                f"<p>错误: {str(e)}</p><pre>{traceback.format_exc()}</pre>"
            )
        except Exception:
            pass


def _force_walk_forward():
    """CLI手动触发Walk-Forward（忽略日期限制）"""
    import types
    today = datetime.date.today()
    logger.info(f"[{today}] 🔄 手动触发Walk-Forward验证...")
    # 临时将today.day替换为1以绕过日期检查
    original_day = today.day
    # 直接调用内部逻辑：将day检查绕过
    _run_monthly_walk_forward.__skip_day_check__ = True
    _run_monthly_walk_forward()
    _run_monthly_walk_forward.__skip_day_check__ = False


# ============================================================
# 三、盘中监控（后台线程）
# ============================================================

_monitor_thread = None
_monitor_instance = None

# V4.0: 统一盘中预警冷却记录 {stock_code: last_send_time}
_ALERT_COOLDOWN_MINUTES = 30  # 同一股票30分钟内不重复发送

# FIX P2-7: 冷却字典JSON持久化（启动加载/写入即保存/自动清理过期键，重启不重复推送）
_ALERT_COOLDOWN_FILE = os.path.join(PROJECT_ROOT, "data", "alert_cooldown.json")


def _load_alert_cooldown() -> dict:
    """FIX P2-7: 加载冷却记录（自动清理过期键，失败降级为空字典）"""
    data = {}
    try:
        if os.path.exists(_ALERT_COOLDOWN_FILE):
            import json as _json
            with open(_ALERT_COOLDOWN_FILE, "r", encoding="utf-8") as f:
                raw = _json.load(f)
            now = datetime.datetime.now()
            for code, ts in raw.items():
                try:
                    t = datetime.datetime.fromisoformat(ts)
                    if (now - t).total_seconds() < _ALERT_COOLDOWN_MINUTES * 60:
                        data[code] = t
                except Exception:
                    continue
    except Exception:
        data = {}
    return data


def _save_alert_cooldown():
    """FIX P2-7: 冷却记录落盘（先清理过期键，失败不阻断主流程）"""
    try:
        import json as _json
        now = datetime.datetime.now()
        live = {c: t.isoformat() for c, t in _alert_cooldown.items()
                if (now - t).total_seconds() < _ALERT_COOLDOWN_MINUTES * 60}
        os.makedirs(os.path.dirname(_ALERT_COOLDOWN_FILE), exist_ok=True)
        with open(_ALERT_COOLDOWN_FILE, "w", encoding="utf-8") as f:
            _json.dump(live, f, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"冷却持久化失败: {e}")


_alert_cooldown = _load_alert_cooldown()


# FIX P2-8: AlertEngine模块级单例复用（原每轮新建实例导致引擎15分钟冷却失效）
_alert_engine_singleton = None


def _get_alert_engine(holdings_data: dict):
    """FIX P2-8: 懒加载全局单例，每轮仅刷新holdings引用"""
    global _alert_engine_singleton
    try:
        from notify.alert_engine import AlertEngine
        if _alert_engine_singleton is None:
            _alert_engine_singleton = AlertEngine(holdings=holdings_data)
        else:
            _alert_engine_singleton.holdings = holdings_data
        return _alert_engine_singleton
    except Exception as e:
        logger.warning(f"AlertEngine单例初始化失败: {e}")
        return None


def _generate_intraday_stop_orders(alerts: list, holdings_data: dict):
    """FIX B1: 将止损类盘中预警自动转化为QMT执行JSON

    仅对以下规则触发的预警生成卖出执行单:
      - 止损线跌破 / 生命线跌破 / 单日暴跌 / 趋势降级
    生成的JSON追加到当日orders文件，QMT执行器(09:25启动)会读取并执行。
    """
    import json as _json

    STOP_RULES = ("止损", "跌破", "暴跌", "趋势降级", "清仓", "stop_loss")
    stop_alerts = [
        a for a in alerts
        if any(kw in a.get("rule_name", "") or kw in a.get("msg", "") for kw in STOP_RULES)
        and a.get("code") in holdings_data
        and holdings_data[a["code"]].get("shares", 0) > 0
    ]
    if not stop_alerts:
        return

    today = datetime.date.today()
    date_str = today.strftime("%Y%m%d")
    output_dir = config.OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"orders_{date_str}.json")

    # 读取已有orders文件（避免覆盖盘前生成的条件单）
    existing_orders = []
    existing_codes = set()
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing_data = _json.load(f)
            existing_orders = existing_data.get("orders", [])
            existing_codes = {o.get("证券代码", "") for o in existing_orders if o.get("方向") == "卖出"}
        except Exception:
            pass

    new_orders = []
    for a in stop_alerts:
        code = a["code"]
        if code in existing_codes:
            continue  # 已有卖出单，不重复生成
        h = holdings_data[code]
        shares = h.get("shares", 0)
        stop_loss = h.get("stop_loss", 0)
        name = h.get("name", a.get("name", code))
        # 触发价: 使用holdings中预设的stop_loss，若无则用现价×97%
        trigger_price = stop_loss if stop_loss > 0 else 0
        new_orders.append({
            "order_id": f"ALERT_{date_str}_{code}",
            "证券代码": code,
            "证券名称": name,
            "方向": "卖出",
            "触发价": trigger_price,
            "数量": shares,
            "类型": "定价卖出",
            "有效期": "1个交易日",
            "触发时间": "盘中实时",
            "优先级": "★★★必挂",
            "说明": f"[盘中预警自动止损] {a.get('rule_name', '')}: {a.get('msg', '')}",
        })
        existing_codes.add(code)

    if not new_orders:
        return

    orders_json = {
        "date": today.strftime("%Y-%m-%d"),
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "intraday_alert_auto_stop",
        "total_capital": getattr(config, 'TOTAL_CAPITAL', 1000000),
        "max_daily_trades": 10,
        "orders": existing_orders + new_orders,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        _json.dump(orders_json, f, ensure_ascii=False, indent=2)
    logger.info(f"  [统一预警] 🔴 自动止损执行JSON已生成: {len(new_orders)}条 → {json_path}")


# V8.0: 分级变频门控（P0-2）
_last_intraday_alert_time = None  # 上次实际执行时间


def _gated_intraday_alert():
    """V8.0 分级变频门控：根据监控级别决定是否实际执行统一预警扫描

    级别对应频率:
      - normal:    10分钟/次
      - warning:    3分钟/次
      - emergency:  1分钟/次
    """
    global _last_intraday_alert_time

    esc_cfg = getattr(config, 'INTRADAY_ESCALATION_CONFIG', {})
    normal_interval = esc_cfg.get('normal_interval_min', 10)
    warning_interval = esc_cfg.get('warning_interval_min', 3)
    emergency_interval = esc_cfg.get('emergency_interval_min', 1)

    # 获取当前监控级别（从IntradayMonitor实例读取）
    current_level = "normal"
    if _monitor_instance and hasattr(_monitor_instance, 'get_alert_level'):
        current_level = _monitor_instance.get_alert_level()

    # 根据级别确定当前应执行的间隔
    interval_map = {
        "normal": normal_interval,
        "warning": warning_interval,
        "emergency": emergency_interval,
    }
    required_interval = interval_map.get(current_level, normal_interval)

    # 检查距离上次执行是否已足够
    now = datetime.datetime.now()
    if _last_intraday_alert_time is not None:
        elapsed_min = (now - _last_intraday_alert_time).total_seconds() / 60
        if elapsed_min < required_interval:
            return  # 未到间隔，跳过

    # 执行实际扫描
    _last_intraday_alert_time = now
    if current_level != "normal":
        logger.info(f"[变频监控] 级别={current_level}, 间隔={required_interval}min, 执行扫描")
    run_unified_intraday_alert()


def run_unified_intraday_alert():
    """V4.0 统一盘中预警（合并原异动预警+决策报告+深度预警，每10分钟一次）

    设计原则:
      - 三源合一: 异动预警(intraday_alert) + 决策报告(intraday_decision) + 深度预警(alert_engine)
      - 去重冷却: 同一股票30分钟内只发一次邮件
      - 仅紧急发送: urgency_score>=70 或 level=critical/high 才触发邮件
      - 决策报告不再独立发邮件，仅记录日志供盘后回溯
    """
    today = datetime.date.today()

    if not is_trading_day(today):
        return

    now = datetime.datetime.now()
    if now.hour < 9 or (now.hour == 9 and now.minute < 30) or now.hour >= 15:
        return
    # 午休跳过
    if now.hour == 12 or (now.hour == 11 and now.minute > 30):
        return

    logger.info(f"[{today}] 统一盘中预警扫描 ({now.strftime('%H:%M')})...")

    # ---- 源1: 持仓深度预警（alert_engine 11大规则）----
    urgent_alerts = []
    try:
        import json as _json
        from notify.alert_engine import AlertEngine, send_alert_email, _fetch_and_analyze
        holdings_data = {}
        try:
            with open(config.HOLDINGS_FILE, "r", encoding="utf-8") as f:
                holdings_data = _json.load(f)
        except Exception:
            pass

        # FIX: 将股票池(core_pool+watch_pool)纳入盘中监控，消除选股结果与盘中预警的盲区
        try:
            pool_file = os.path.join(config.PROJECT_ROOT, "stock_pool.json")
            if os.path.exists(pool_file):
                with open(pool_file, "r", encoding="utf-8") as pf:
                    pool_data = _json.load(pf)
                for pool_key in ("core_pool", "watch_pool"):
                    for code, info in pool_data.get(pool_key, {}).items():
                        if code not in holdings_data:
                            # 股票池标的以零仓位加入监控（触发DK/均线/涨跌停等技术规则）
                            holdings_data[code] = {
                                "name": info.get("名称", code),
                                "shares": 0,
                                "buy_price": 0,
                                "stop_loss": 0,
                                "sector": info.get("赛道", ""),
                            }
        except Exception:
            pass

        if holdings_data:
            engine = _get_alert_engine(holdings_data)  # FIX P2-8: 单例复用
            results = _fetch_and_analyze(holdings_data) if engine else None
            if engine and results:
                triggered = engine.check_alerts(results)
                if triggered:
                    # FIX P2-5: warning级且评分>=50的预警（如R17止盈）也纳入邮件链路
                    critical = [a for a in triggered
                                if a.get("level") in ("critical", "high")
                                or (a.get("level") == "warning" and a.get("urgency_score", 0) >= 50)]
                    logger.info(f"  [深度预警] 触发{len(triggered)}条(入邮件链路: {len(critical)}条)")
                    urgent_alerts.extend(critical)
    except Exception as e:
        logger.warning(f"  [深度预警] 异常: {e}")

    # ---- 源2: 盘中异动预警（准涨停/突破/联动）----
    try:
        from strategy.intraday_alert import run_intraday_alert
        result = run_intraday_alert(send_email=False)  # V4.0: 不再独立发邮件
        if result.get("success"):
            n_zt = len(result.get('near_zt_stocks', []))
            n_break = len(result.get('breakout_stocks', []))
            n_cascade = len(result.get('sector_cascade', []))
            n_gene = len(result.get('zt_gene_alerts', []))
            if n_zt + n_break + n_cascade + n_gene > 0:
                logger.info(f"  [异动预警] 准涨停{n_zt} 突破{n_break} 联动{n_cascade} 基因{n_gene}")
            # 将准涨停/突破转化为urgent_alerts格式（仅持仓相关的）
            for stock in result.get('near_zt_stocks', []):
                urgent_alerts.append({
                    "level": "high", "name": stock.get("name", ""),
                    "code": stock.get("code", ""), "urgency_score": 75,
                    "msg": f"准涨停: {stock.get('reason', '')}",
                    "rule_name": "盘中异动-准涨停", "icon": "🚀",
                })
            # FIX P2-6: 板块联动达阈值时构造warning级预警入urgent_alerts（原仅记日志）
            for cas in result.get('sector_cascade', []):
                urgent_alerts.append({
                    "level": "warning", "name": cas.get("name", ""),
                    "code": cas.get("code", ""), "urgency_score": 50,
                    "msg": (f"板块联动: {cas.get('sector', '')}已有{cas.get('sector_zt_count', 0)}只涨停, "
                            f"跟涨候选{cas.get('name', '')} +{cas.get('change_pct', 0):.1f}% "
                            f"(量比{cas.get('vol_ratio', 0):.1f})"),
                    "rule_name": "盘中异动-板块联动", "icon": "🔗",
                })
    except Exception as e:
        logger.warning(f"  [异动预警] 异常: {e}")

    # ---- 源3: 盘中决策报告（仅日志，不发邮件）----
    try:
        from strategy.intraday_decision import run_intraday_decision
        result = run_intraday_decision(send_email_flag=False)  # V4.0: 不再独立发邮件
        if result.get("success"):
            decisions = result.get("decisions", [])
            danger = sum(1 for d in decisions if d.get("score", 0) <= -3)
            locked = sum(1 for d in decisions if d.get("add_locked"))
            if danger > 0 or locked > 0:
                logger.info(f"  [决策报告] {len(decisions)}只标的, 风险{danger}只, 加仓锁{locked}只")
            # 将紧急卖出建议转化为urgent_alerts
            for d in decisions:
                if d.get("score", 0) <= -3:
                    urgent_alerts.append({
                        "level": "high", "name": d.get("name", ""),
                        "code": d.get("code", ""), "urgency_score": 72,
                        "msg": f"紧急卖出: {d.get('action', '')}",
                        "rule_name": "盘中决策-紧急卖出", "icon": "🔴",
                    })
    except Exception as e:
        logger.warning(f"  [决策报告] 异常: {e}")

    # ---- 源4: 炸板池增量扫描（V9.1新增，P0资金安全）----
    # 解决缺口: 原系统无实时炸板检测，持仓股从涨停回落无法及时通知
    # 覆盖范围: 全市场炸板池（东财接口），持仓股critical/候选池high/其他warning
    try:
        from strategy.zt_monitor import ZTMonitor
        _zb_monitor = ZTMonitor()
        new_zb_stocks = _zb_monitor.get_zb_pool_incremental()
        if new_zb_stocks:
            # 构建持仓代码集 + 候选池代码集（用于分级）
            holdings_codes = set(holdings_data.keys()) if holdings_data else set()
            candidate_codes = set()
            sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
            code_sector_map = {}  # code -> 粗粒度赛道
            for sector_name, sector_info in sector_candidates.items():
                for stk_code in sector_info.get("stocks", {}).keys():
                    candidate_codes.add(stk_code)
                    code_sector_map[stk_code] = sector_name

            for zb in new_zb_stocks:
                code = zb.get("code", "")
                name = zb.get("name", "")
                change_pct = zb.get("change_pct", 0)
                zt_time = zb.get("zt_time", "")
                zb_time = zb.get("zb_time", "")
                cur_price = zb.get("current_price", 0)
                sector_raw = zb.get("sector", "")

                # 赛道归一化: 优先用config粗粒度口径，其次用东财行业
                sector_display = code_sector_map.get(code, sector_raw or "其他")

                # 分级: 持仓股critical(95) > 候选池high(80) > 全市场warning(65)
                if code in holdings_codes:
                    level, score = "critical", 95
                elif code in candidate_codes:
                    level, score = "high", 80
                else:
                    level, score = "warning", 65

                # 仅warning且非持仓/候选的不进入邮件（降噪）
                if level == "warning" and score < 70:
                    logger.info(f"  [炸板池] 全市场炸板(非持仓/候选): {name}({code}) 涨幅{change_pct:.1f}%")
                    continue

                msg = (f"炸板回落: 曾{zt_time}封板→{zb_time}开板 | "
                       f"当前涨幅{change_pct:+.1f}% | 赛道:{sector_display}")

                urgent_alerts.append({
                    "level": level,
                    "name": name,
                    "code": code,
                    "urgency_score": score,
                    "msg": msg,
                    "rule_name": "炸板池增量-涨停开板",
                    "rule_detail": f"封板{zt_time}→开板{zb_time}, 现价{cur_price}, 涨幅{change_pct:+.1f}%",
                    "icon": "💥",
                    "type": "zb_alert",
                    "sector": sector_display,
                    "time": now.strftime("%H:%M"),
                })

            n_zb_alerts = sum(1 for a in urgent_alerts if a.get("type") == "zb_alert")
            if n_zb_alerts > 0:
                logger.info(f"  [炸板池] 🚨 新增炸板预警{n_zb_alerts}条")
    except Exception as e:
        logger.warning(f"  [炸板池] 异常: {e}")

    # ---- FIX P2-10: 系统性风险标记接通（大盘跌>2.5%→风控systemic_risk标记，恢复时清除）----
    try:
        from data.realtime import fetch_index_realtime
        _idx_sh = fetch_index_realtime("000001")
        _mkt_chg = _idx_sh.get("change_pct", 0)
        if _mkt_chg:
            _esc_cfg = getattr(config, 'INTRADAY_ESCALATION_CONFIG', {})
            _emerg_drop = _esc_cfg.get("emergency_triggers", {}).get("market_drop_pct", -2.5)
            from risk.risk_control import UnifiedRiskEngine
            _risk_eng = UnifiedRiskEngine()
            if _mkt_chg <= _emerg_drop:
                _risk_eng.set_intraday_risk_flag("systemic_risk", {
                    "source": "run_unified_intraday_alert",
                    "market_change_pct": _mkt_chg,
                })
            elif _mkt_chg > _emerg_drop + 0.5:
                # 恢复口径: 回升到跌2%以内清除标记（滞回0.5%防抖动）
                _risk_eng.clear_intraday_risk_flag("systemic_risk")
    except Exception as e:
        logger.warning(f"  [系统性风险联动] 异常: {e}")

    # ---- 去重 + 冷却过滤 ----
    if not urgent_alerts:
        return

    # V3.0: 为所有预警附加holdings_info（供邮件过滤shares>0）
    for a in urgent_alerts:
        if "holdings_info" not in a:
            code = a.get("code", "")
            info = holdings_data.get(code, {})
            buy_price = info.get("buy_price", 0) or info.get("cost", 0)
            cur_price = info.get("current_price", 0)
            a["holdings_info"] = {
                "buy_price": buy_price,
                "current_price": cur_price,
                "stop_loss": info.get("stop_loss", 0),
                "shares": info.get("shares", 0),
                "pnl_pct": round((cur_price - buy_price) / buy_price * 100, 1) if buy_price > 0 and cur_price > 0 else 0,
            }

    # 按(code)去重 — V3.0同标的只保留最高紧急度
    by_code = {}
    for a in urgent_alerts:
        code = a.get("code", "")
        if code not in by_code or a.get("urgency_score", 0) > by_code[code].get("urgency_score", 0):
            by_code[code] = a
    deduped = list(by_code.values())

    # 30分钟冷却过滤
    filtered = []
    for a in deduped:
        code = a.get("code", "")
        last_time = _alert_cooldown.get(code)
        if last_time and (now - last_time).total_seconds() < _ALERT_COOLDOWN_MINUTES * 60:
            continue  # 冷却中，跳过
        filtered.append(a)

    if not filtered:
        logger.info(f"  [统一预警] 全部处于冷却期，跳过发送")
        return

    # 按紧急度排序
    filtered.sort(key=lambda a: a.get("urgency_score", 0), reverse=True)

    # 发送统一预警邮件
    try:
        from notify.alert_engine import send_alert_email
        send_alert_email(filtered)
        # 更新冷却记录
        for a in filtered:
            _alert_cooldown[a.get("code", "")] = now
        _save_alert_cooldown()  # FIX P2-7: 冷却记录落盘，重启不重复推送
        logger.info(f"  [统一预警] 📧 邮件已发送({len(filtered)}条)")
    except Exception as e:
        logger.warning(f"  [统一预警] 邮件发送失败: {e}")

    # FIX B1: 止损类预警自动生成QMT执行JSON，消除"预警发了但止损没执行"的断链
    try:
        _generate_intraday_stop_orders(filtered, holdings_data)
    except Exception as e:
        logger.warning(f"  [统一预警] 止损执行JSON生成失败(不影响邮件): {e}")


# V4.0: 保留原函数名作为兼容别名（CLI --run-intraday-alert/--run-intraday-decision 仍可用）
def run_intraday_alert_task():
    """兼容入口 → 转发到统一预警"""
    run_unified_intraday_alert()


def run_intraday_decision_task():
    """兼容入口 → 转发到统一预警"""
    run_unified_intraday_alert()


def run_alert_engine_task():
    """兼容入口 → 转发到统一预警"""
    run_unified_intraday_alert()


def run_zt_gene_task():
    """涨停基因跟踪（09:26竞价结束后运行）"""
    today = datetime.date.today()

    if not is_trading_day(today):
        logger.info(f"[{today}] 非交易日，跳过涨停基因跟踪")
        return

    logger.info(f"[{today}] 涨停基因跟踪（竞价后）...")

    try:
        from strategy.zt_gene_tracker import run_zt_gene_tracking
        result = run_zt_gene_tracking()
        if result.get("success"):
            logger.info(f"[{today}] 涨停基因跟踪完成: "
                       f"连板候选{len(result.get('candidates', []))}只, "
                       f"已连板{len(result.get('continued_zt', []))}只")
        else:
            logger.warning(f"[{today}] 涨停基因跟踪未成功")
    except Exception as e:
        logger.error(f"[{today}] 涨停基因跟踪异常: {e}", exc_info=True)


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

    # P0: 单实例锁，防止多进程并发
    if not _acquire_lock():
        logger.warning("已有调度器实例在运行（心跳正常），本次启动跳过")
        return

    logger.info("=" * 50)
    logger.info("  交易系统定时调度器 V3.1 (守护增强版)")
    logger.info(f"  启动时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"  PID: {os.getpid()}")
    logger.info("=" * 50)

    # 每个交易日 08:30 盘前作战计划（合并原盘前预测+操作清单+持仓快览）
    schedule.every().day.at("08:30").do(run_forecast_morning)

    # V2.5: 每个交易日 09:26 涨停基因跟踪（竞价结束后）
    schedule.every().day.at("09:26").do(run_zt_gene_task)

    # V9.0: 每个交易日 09:25 集合竞价分析（持仓股高开/低开预警）
    # FIX: 注册顺序提前于选股报告，竞价分析对时效性更敏感
    schedule.every().day.at("09:25").do(run_auction_analysis)

    # V3.1: 每个交易日 09:25 竞价后选股报告（修复: 原来只在CLI模式可用，未注册到调度器）
    schedule.every().day.at("09:25").do(run_morning_screener)

    # V3.0-FIX P1: 盘中动态扫描缓存预热（09:45/10:30各一次，仅刷缓存不发邮件）
    schedule.every().day.at("09:45").do(run_intraday_scan_task)
    schedule.every().day.at("10:30").do(run_intraday_scan_task)

    # V9.0 P2-1: 每个交易日 09:15 竞价轨迹采集（后台线程，09:15-09:25多点采样）
    schedule.every().day.at("09:15").do(run_auction_tracking_task)

    # 每个交易日 15:30 盘后分析（数据更新+信号+综合日报）
    schedule.every().day.at("15:30").do(run_daily_task)

    # V4.1: 每个交易日 16:15 综合分析报告（技术面深度诊断+条件单，与盘后日报互补）
    schedule.every().day.at("16:15").do(run_holdings_report_task)

    # 每个交易日 19:00 条件单（统一发送，方便提前挂单）
    schedule.every().day.at("19:00").do(run_morning_reminder)

    # 每周六 10:00 周策略报告
    schedule.every().saturday.at("10:00").do(run_weekly_portfolio)

    # 每周六 11:00 ML模型训练（周策略报告之后）
    schedule.every().saturday.at("11:00").do(_run_weekly_ml_training)

    # 每周日 10:00 股票池更新提醒
    schedule.every().sunday.at("10:00").do(run_weekly_task)

    # FIX: Walk-Forward 虽每日注册，但 _run_monthly_walk_forward() 内部已有 today.day==1 检查，
    # 非每月1日时自动跳过，因此实际仅在每月1日执行。每日注册是为了避免错过（如节假日后第一天）。
    # 每月1日 09:00 Walk-Forward滚动窗口验证（避开其他任务）
    schedule.every().day.at("09:00").do(_run_monthly_walk_forward)

    logger.info("调度器已启动，等待执行...")
    logger.info(f"  盘前作战计划: 每个交易日 08:30")
    logger.info(f"  竞价选股报告: 每个交易日 09:25")
    logger.info(f"  涨停基因跟踪: 每个交易日 09:26")
    logger.info(f"  盘后综合日报: 每个交易日 15:30")
    logger.info(f"  综合分析报告: 每个交易日 16:15")
    logger.info(f"  条件单: 每个交易日 19:00")
    logger.info(f"  周策略报告: 每周六 10:00")
    logger.info(f"  ML模型训练: 每周六 11:00")
    logger.info(f"  周日提醒: 每周日 10:00")
    logger.info(f"  Walk-Forward验证: 每月1日 09:00")
    logger.info(f"  盘中监控: 每个交易日 09:30-15:00")

    # 盘中监控（后台线程）
    schedule.every().day.at("09:30").do(start_intraday_monitor)
    schedule.every().day.at("15:01").do(stop_intraday_monitor)

    # V8.0: 统一盘中预警（分级变频: 正常10分钟/预警3分钟/紧急1分钟）
    # 每分钟注册一次，内部通过 _should_run_intraday_alert() 门控决定是否实际执行
    for hour in range(9, 15):
        for minute in range(0, 60):
            # 跳过09:30之前和11:30-13:00午休
            if hour == 9 and minute < 35:
                continue
            if hour == 11 and minute > 30:
                continue
            if hour == 12:
                continue
            if hour == 14 and minute > 55:
                continue
            time_str = f"{hour:02d}:{minute:02d}"
            schedule.every().day.at(time_str).do(_gated_intraday_alert)

    logger.info(f"  统一盘中预警: 每个交易日 09:35-14:55 分级变频(正常10min/预警3min/紧急1min)")

    # FIX P2-9: 盘中启动补跑 — 若调度器在盘中时段启动，立即启动盘中监控（不等待次日09:30）
    try:
        _now_startup = datetime.datetime.now()
        _startup_hm = (_now_startup.hour, _now_startup.minute)
        if is_trading_day(datetime.date.today()) and (9, 30) <= _startup_hm and _now_startup.hour < 15:
            logger.info("[盘中监控] 检测到盘中时段启动，立即补跑启动盘中监控")
            start_intraday_monitor()
    except Exception as e:
        logger.warning(f"[盘中监控] 补跑启动失败(不影响主循环): {e}")

    # P0: 启动时立即写入心跳
    _write_heartbeat()

    try:
        while True:
            schedule.run_pending()
            # P0: 每次循环写入心跳（供外部监控和CLI去重检查）
            _write_heartbeat()

            # 非交易时段降低检查频率
            now = datetime.datetime.now()
            if now.hour >= 20 or (now.hour < 8):
                _time.sleep(300)
            else:
                _time.sleep(60)
    except KeyboardInterrupt:
        logger.info("调度器收到中断信号，正常退出")
    except Exception as e:
        logger.error(f"调度器主循环异常: {e}", exc_info=True)
    finally:
        _release_lock()
        logger.info("调度器已停止")


# ============================================================
# P0 自愈: 心跳失联告警 + 自动拉起主循环
# ============================================================

SELF_HEAL_STALE_SECONDS = 900  # 心跳超过15分钟未更新视为失联


def run_self_heal():
    """P0 调度器自愈: 心跳失联 → 发失联告警邮件 + 拉起主调度循环

    由Windows任务 TradingSystem_SelfHeal(09:31) 触发。
    主循环拉起后盘中监控/统一盘中预警等盘中任务一并恢复（主循环是唯一载体）。
    start_scheduler内部_acquire_lock单实例锁防止重复拉起。
    """
    try:
        if not is_trading_day(datetime.date.today()):
            logger.info("[自愈] 非交易日，跳过")
            return

        # 第1步: 心跳检测
        last_beat = None
        alive = False
        try:
            if os.path.exists(HEARTBEAT_FILE):
                with open(HEARTBEAT_FILE, "r") as f:
                    ts_str = f.read().strip().split("|")[0]
                last_beat = datetime.datetime.fromisoformat(ts_str)
                alive = (datetime.datetime.now() - last_beat).total_seconds() <= SELF_HEAL_STALE_SECONDS
        except Exception:
            alive = False

        if alive:
            logger.info("[自愈] 心跳正常，无需处理")
            return

        last_str = last_beat.strftime("%Y-%m-%d %H:%M:%S") if last_beat else "无心跳记录"
        logger.warning(f"[自愈] 检测到心跳失联(最后: {last_str})，启动告警与拉起")

        # 第2步: 失联告警邮件
        try:
            from notify.email_notify import send_email
            html = (
                "<html><body style='font-family:Microsoft YaHei,sans-serif;padding:20px'>"
                "<h2 style='color:#cf1322'>⚠️ 调度器失联告警</h2>"
                f"<p>最后心跳时间: <b>{last_str}</b></p>"
                f"<p>检测时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                f"（失联阈值{SELF_HEAL_STALE_SECONDS // 60}分钟）</p>"
                "<p>主调度循环可能已停止，盘中预警/定时报告将不会触发。</p>"
                "<p>系统正在尝试自动拉起调度器主循环，若反复收到本邮件请人工检查。</p>"
                "</body></html>"
            )
            send_email(f"[操盘密码] ⚠️调度器失联告警 {datetime.date.today()} | 最后心跳{last_str}", html)
        except Exception as e:
            logger.warning(f"[自愈] 失联告警邮件发送失败: {e}")

        # 第3步: 拉起主调度循环（python scheduler.py无参数→start_scheduler，内部单实例锁防重复）
        try:
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__)],
                cwd=PROJECT_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            logger.info("[自愈] 已发起拉起: python scheduler.py (主循环)")
        except Exception as e:
            logger.warning(f"[自愈] 拉起主循环失败: {e}")
    except Exception as e:
        logger.warning(f"[自愈] 异常(不影响其他任务): {e}")


# ============================================================
# 四、Windows任务计划程序（每个报告独立任务）
# ============================================================

# 所有定时任务定义: (任务名, 时间, 命令行参数, 说明)
SCHEDULED_TASKS = [
    ("TradingSystem_MorningReminder", "19:00", "--run-morning-reminder", "条件单(晚间挂单)"),
    ("TradingSystem_ForecastAM",      "08:30", "--run-forecast-am",      "盘前作战计划"),
    ("TradingSystem_AuctionTrack",    "09:15", "--run-auction-track",    "V9.0竞价轨迹采集"),
    ("TradingSystem_AuctionAnalysis", "09:25", "--run-auction-analysis", "V9.0集合竞价分析"),
    ("TradingSystem_Screener",        "09:25", "--run-screener",         "竞价后选股报告"),
    ("TradingSystem_Daily",           "15:30", "--run-once",             "盘后完整分析"),
    ("TradingSystem_HoldingsReport",  "16:15", "--run-holdings-report",  "综合分析报告(技术面+条件单)"),
    ("TradingSystem_Weekly",          "10:00", "--run-weekly",           "周六周策略报告"),
    ("TradingSystem_MLTraining",      "11:00", "--run-ml-training",      "周六ML模型训练"),
    ("TradingSystem_WalkForward",     "09:00", "--run-walk-forward",     "月度Walk-Forward验证(每月1日)"),
    ("TradingSystem_SelfHeal",        "09:31", "--self-heal",            "P0调度器自愈(心跳检测+自动拉起)"),
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
    parser.add_argument("--run-ml-training", action="store_true",
                        help="运行 ML模型训练")
    parser.add_argument("--run-walk-forward", action="store_true",
                        help="运行 Walk-Forward滚动窗口验证")
    parser.add_argument("--run-intraday-alert", action="store_true",
                        help="运行盘中异动预警扫描")
    parser.add_argument("--run-intraday-decision", action="store_true",
                        help="运行盘中实时决策报告")
    parser.add_argument("--run-zt-gene", action="store_true",
                        help="运行涨停基因跟踪")
    parser.add_argument("--run-holdings-report", action="store_true",
                        help="运行综合分析报告(技术面+条件单)")
    parser.add_argument("--run-auction-track", action="store_true",
                        help="运行V9.0竞价轨迹采集")
    parser.add_argument("--run-auction-analysis", action="store_true",
                        help="运行V9.0集合竞价分析")
    parser.add_argument("--self-heal", action="store_true",
                        help="P0调度器自愈: 心跳失联检测+告警邮件+拉起主循环")
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
    else:
        # P0: 调度器自愈任务（自身检测心跳，不走心跳去重逻辑）
        if getattr(args, 'self_heal', False):
            run_self_heal()
            return

        # P1: CLI一次性任务执行前检查主循环是否存活，避免双重执行
        _ONE_SHOT_TASKS = {
            'run_once': run_daily_task,
            'run_forecast_am': run_forecast_morning,
            'run_forecast_pm': run_forecast_afternoon,
            'run_morning_reminder': run_morning_reminder,
            'run_screener': run_morning_screener,
            'run_weekly': run_weekly_portfolio,
            'run_ml_training': _run_weekly_ml_training,
            'run_intraday_alert': run_intraday_alert_task,
            'run_intraday_decision': run_intraday_decision_task,
            'run_zt_gene': run_zt_gene_task,
            'run_holdings_report': run_holdings_report_task,
            'run_auction_track': run_auction_tracking_task,
            'run_auction_analysis': run_auction_analysis,
        }
        dispatched = False
        for arg_name, func in _ONE_SHOT_TASKS.items():
            if getattr(args, arg_name, False):
                dispatched = True
                # P1: 心跳去重 —— 主循环存活时跳过（它会在同一时间触发同一任务）
                if is_scheduler_alive():
                    logger.info(f"[P1去重] 调度器主循环存活，跳过CLI任务: --{arg_name.replace('_', '-')}")
                else:
                    logger.info(f"[CLI执行] 主循环未运行，执行: --{arg_name.replace('_', '-')}")
                    func()
                break

        if not dispatched:
            # FIX: Walk-Forward CLI入口改为走带日期校验的入口。
            # 原逻辑调用_force_walk_forward()强制绕过today.day==1检查，
            # 而Windows任务TradingSystem_WalkForward每日09:00触发，
            # 导致非1号交易日也执行月度资金重置(monthly_start_capital)，月度收益统计失真。
            if args.run_walk_forward:
                if is_scheduler_alive():
                    logger.info("[P1去重] 调度器主循环存活，跳过CLI任务: --run-walk-forward")
                else:
                    _run_monthly_walk_forward()
            else:
                start_scheduler()


if __name__ == "__main__":
    main()
