"""
高胜率A股交易操作系统 V9.0 - 主程序入口
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
import time
import argparse
import datetime
import logging
import json
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
import pandas as pd

# 确保项目根目录在sys.path中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import config
from data.data_loader import init_db, batch_update_all, load_daily_data, get_all_candidate_codes
# FIX: 清理死代码无用 import（generate_strategy_signal / notify_risk_alert /
# send_daily_report / send_risk_alert / send_eastmoney_orders_email /
# send_screener_email / send_portfolio_email / send_risk_report_email /
# send_forecast_email 均 grep 确认本文件零使用）
from strategy.trend_strategy import scan_all_stocks, compute_indicators
from strategy.position import calc_first_batch
from risk.risk_control import (RiskState, risk_check, judge_market_strength,
                                get_max_position_ratio, daily_risk_summary)
from notify.wechat_notify import (notify_buy_signal, notify_sell_signal,
                                   notify_daily_summary)
from output.condition_sheet import generate_condition_sheet, generate_simple_report
from strategy.stock_screener import run_stock_screener, hard_filter
from strategy.portfolio_analyzer import analyze_portfolio
from strategy.market_scanner import scan_market_hot_stocks, merge_scan_results_to_pool
# V3.0 新增模块
from strategy.portfolio_risk import PortfolioRiskManager
from strategy.fundamental import FundamentalAnalyzer
from strategy.mean_reversion import MeanReversionStrategy
from strategy.multi_timeframe import MultiTimeframeAnalyzer
from strategy.capital_flow import CapitalFlowAnalyzer
from strategy.market_regime import MarketRegimeDetector
from strategy.trade_journal import TradeJournal
from strategy.trend_forecast import TrendForecaster
# V7.1 新增模块
from strategy.anti_manipulation import AntiManipulationAnalyzer
from strategy.consensus import batch_consensus

# ============================================================
# 日志配置
# ============================================================
os.makedirs(config.LOG_DIR, exist_ok=True)
log_file = os.path.join(config.LOG_DIR,
                        f"trading_{datetime.date.today().strftime('%Y%m%d')}.log")
# FIX: 被调度器拉起时root logger已有handler，logging.basicConfig会被静默忽略，
# 导致trading_*.log长期为0字节。此时显式挂载专用FileHandler保证该日志正常写入。
if logging.root.handlers:
    _trading_fh = logging.FileHandler(log_file, encoding="utf-8")
    _trading_fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.root.addHandler(_trading_fh)
else:
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


# FIX: 修复 _auto_bootstrap() 在模块级执行导致任何import触发数据下载的问题
# 将自动初始化逻辑移入显式函数，仅在主动运行时调用
def maybe_auto_bootstrap():
    """仅在数据库为空时执行自动初始化（避免import时触发下载）"""
    if _is_db_empty():
        _auto_bootstrap()


# ============================================================
# 持仓数据加载（从本地JSON文件读取）
# ============================================================
HOLDINGS_FILE = config.get_holdings_file()

# 大盘状态检测结果（模块级变量，供CaopanEngine等模块读取）
_current_market_regime = None

# FIX: 修复 _prev_vol_scale 模块级变量进程重启后丢失的问题，持久化到JSON文件
_VOL_SCALE_STATE_FILE = os.path.join(config.DATA_DIR, "vol_scale_state.json")


def _load_vol_scale_state() -> float:
    """从持久化文件加载波动率缩放状态"""
    try:
        if os.path.exists(_VOL_SCALE_STATE_FILE):
            with open(_VOL_SCALE_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            val = float(data.get("prev_vol_scale", 1.0))
            print(f"DEBUG: _load_vol_scale_state from {_VOL_SCALE_STATE_FILE} = {val}")
            return val
    except Exception as e:
        print(f"DEBUG: _load_vol_scale_state exception: {e}")
    print(f"DEBUG: _load_vol_scale_state default 1.0 (file={_VOL_SCALE_STATE_FILE}, exists={os.path.exists(_VOL_SCALE_STATE_FILE)})")
    return 1.0


def _save_vol_scale_state(value: float):
    """持久化波动率缩放状态到JSON文件"""
    try:
        os.makedirs(os.path.dirname(_VOL_SCALE_STATE_FILE), exist_ok=True)
        with open(_VOL_SCALE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"prev_vol_scale": value, "updated": datetime.datetime.now().isoformat()}, f)
        print(f"DEBUG: _save_vol_scale_state {value} to {_VOL_SCALE_STATE_FILE}")
    except Exception as e:
        logger.warning(f"  波动率缩放状态持久化失败: {e}")
        print(f"DEBUG: _save_vol_scale_state FAILED: {e}")


_prev_vol_scale = _load_vol_scale_state()

def load_holdings() -> dict:
    """
    加载当前持仓数据（统一使用 config.get_holdings_file 解析路径）
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
    """保存持仓数据（原子写 + 异常降级，防止进程中断导致 holdings.json 截断）"""
    global HOLDINGS_FILE
    HOLDINGS_FILE = config.get_holdings_file()
    try:
        from utils.file_io import atomic_json_write
        if not atomic_json_write(HOLDINGS_FILE, holdings):
            logger.error(f"[持仓保存] {HOLDINGS_FILE} 原子写入失败，请检查磁盘空间")
    except ImportError:
        # 降级: utils 模块不可用时回退到直接写入
        try:
            os.makedirs(os.path.dirname(HOLDINGS_FILE), exist_ok=True)
            with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(holdings, f, ensure_ascii=False, indent=2)
        except Exception as e2:
            logger.error(f"[持仓保存] 降级写入也失败: {e2}")


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


def get_current_market_regime() -> dict:
    """获取当前大盘状态检测结果（供CaopanEngine等模块调用）"""
    return _current_market_regime


# ============================================================
# Step9 动态扫描扩面：两阶段候选拉取（批2-S7）
# ============================================================

def _expand_scan_candidates(new_codes: list, scan_result: dict,
                            data_dict: dict, conn) -> int:
    """两阶段处理扫描新增候选（阶段A内存粗筛 + 阶段B预算制日线拉取）

    阶段A（零成本）: 利用扫描快照 details 字段过滤指数/ETF/ST/零成交等无效标的
    阶段B（预算制）: ThreadPoolExecutor(max_workers=5) 拉取日线，worker 内
        sleep(0.3) 节流；总预算60秒，超时即截断；失败率>30% 自动降级（本批上限50只）。
    拉取成功者经 compute_indicators + hard_filter 二次筛选后合并进 data_dict。

    返回: 实际入库的股票数量。任何阶段异常向上抛出，由调用方降级为原有行为。
    """
    details_map = {str(d.get("code", "")): d for d in scan_result.get("details", [])}

    # ---- 阶段A: 零成本内存粗筛 ----
    # 排除口径与 stock_screener.filter_strong_sectors 一致：
    # 指数(000300)/创业板(300)/科创板(688)/ETF(588/159)
    _EXCLUDE_PREFIX = ("300", "688", "588", "159")
    coarse = []
    for code in new_codes:
        if code == "000300" or code.startswith(_EXCLUDE_PREFIX):
            continue
        detail = details_map.get(code, {})
        name = str(detail.get("name", ""))
        if "ST" in name.upper() or "退" in name:
            continue
        amount = detail.get("amount")  # details中amount单位为亿
        if amount is None or pd.isna(amount) or float(amount) <= 0:
            continue  # 成交额为0/无效，直接排除
        coarse.append(code)

    if not coarse:
        logger.info("扫描扩面: 粗筛0只→日线成功0只(失败0只, 耗时0.0s)→hard_filter通过0只")
        return 0

    # ---- 阶段B: 日线拉取（线程池5并发 + 0.3s节流 + 60s总预算）----
    t0 = time.time()

    def _fetch_one(code):
        time.sleep(0.3)  # 拉取前节流，避免触发数据源限流
        # 跨线程连接规避：主线程的 SQLite conn 默认禁止跨线程使用
        # （未设 check_same_thread=False，worker 内共用会抛 ProgrammingError），
        # 故传 None 让 load_daily_data 在 worker 线程内自建独立连接并在内部关闭
        return load_daily_data(code, None, days=120)

    budget_sec = 60
    ok_dfs = {}
    fail_count = 0
    truncated = False
    with ThreadPoolExecutor(max_workers=5) as ex:
        fut_map = {ex.submit(_fetch_one, c): c for c in coarse}
        try:
            for fut in as_completed(fut_map, timeout=budget_sec):
                code = fut_map[fut]
                try:
                    df_new = fut.result()
                    if df_new is not None and not df_new.empty and len(df_new) >= config.MA_SHORT:
                        ok_dfs[code] = df_new
                    else:
                        fail_count += 1
                except Exception:
                    fail_count += 1  # 单只失败静默跳过，计入失败数
        except FuturesTimeoutError:
            truncated = True
            logger.warning(f"扫描扩面: 日线拉取超过{budget_sec}s预算，截断剩余"
                           f"{len(fut_map) - len(ok_dfs) - fail_count}只")
    elapsed = time.time() - t0

    # 失败率>30% → 自动降级：本批处理上限降回50只
    attempted = len(ok_dfs) + fail_count
    fail_rate = (fail_count / attempted) if attempted else 0.0
    batch_cap = None
    if fail_rate > 0.3:
        batch_cap = 50
        logger.warning(f"扫描扩面: 日线拉取失败率{fail_rate:.0%}(>{30}%)，本批处理上限降级为{batch_cap}只")

    # ---- 二次筛选并合并进 data_dict ----
    pass_count = 0
    for code, df_new in ok_dfs.items():
        if batch_cap is not None and pass_count >= batch_cap:
            break
        df_ind = compute_indicators(df_new)
        try:
            # hard_filter 适合单只调用（需带均线指标的df）；market_state 默认"up"，
            # 此处M因子尚未计算，与选股引擎内部二次判定的强势模式口径保持一致
            hf = hard_filter(df_ind, code)
        except Exception:
            hf = {"pass": True}  # hard_filter异常时沿用旧行为：直接入库
        if hf.get("pass"):
            data_dict[code] = df_ind
            pass_count += 1

    logger.info(f"扫描扩面: 粗筛{len(coarse)}只→日线成功{len(ok_dfs)}只"
                f"(失败{fail_count}只, 耗时{elapsed:.1f}s)→hard_filter通过{pass_count}只"
                + ("（预算截断）" if truncated else ""))
    return pass_count


# ============================================================
# 主流程
# ============================================================

def run_daily_pipeline(skip_update: bool = False, report_only: bool = False):
    """
    执行每日交易分析流程
    """
    start_time = datetime.datetime.now()
    logger.info("=" * 60)
    logger.info(f"  高胜率A股交易操作系统 V9.0")
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
    global _current_market_regime
    market_regime_result = None
    if getattr(config, 'STRATEGY_CONFIG', {}).get('market_regime', {}).get('enabled', True):
        logger.info("[Step 2.5] 大盘状态智能识别...")
        try:
            detector = MarketRegimeDetector()
            if not benchmark_df.empty and len(benchmark_df) >= 60:
                benchmark_with_indicators = compute_indicators(benchmark_df)
                market_regime_result = detector.detect(benchmark_with_indicators)
                _current_market_regime = market_regime_result  # 存入模块级变量，供CaopanEngine读取
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

    # ---- Step 5.1.5: 提前执行趋势预测+资金流分析（供共识计算使用）----
    logger.info("[Step 5.1.5] 提前执行趋势预测与资金流分析...")
    early_forecast_results = []
    capital_flow_results = {}
    try:
        # 提前执行趋势预测（全候选池，确保共识计算有真实数据）
        forecaster = TrendForecaster()
        for code, df in data_dict.items():
            holding = holdings.get(code)
            try:
                fr = forecaster.analyze_stock(code, df, holding)
                if fr.get("valid"):
                    early_forecast_results.append(fr)
            except Exception as e:
                logger.debug(f"  {code} 预测分析异常: {e}")
        logger.info(f"  趋势预测: {len(early_forecast_results)}/{len(data_dict)}只有效")

        # 提前执行资金流分析（持仓+候选池）
        try:
            cfa = CapitalFlowAnalyzer()
            flow_codes = list(holdings.keys()) + list(config.STOCK_POOL.keys())[:10]
            for code in set(flow_codes):
                try:
                    flow_result = cfa.calc_flow_score(code)
                    capital_flow_results[code] = flow_result
                except Exception as e:
                    logger.debug(f"  {code} 资金流分析异常: {e}")
            logger.info(f"  资金流分析: {len(capital_flow_results)}只有效")
        except Exception as e:
            logger.warning(f"  资金流分析整体异常: {e}")
    except Exception as e:
        logger.warning(f"  提前分析异常(不影响主流程): {e}")

    # ---- Step 5.2: 多空共识计算（V7.1新增，V2.0升级四维度）----
    logger.info("[Step 5.2] 多空共识计算...")
    consensus_results = {}
    try:
        consensus_results = batch_consensus(
            data_dict, holdings, signals,
            forecast_results=early_forecast_results,
            manipulation_results=manipulation_results,
            capital_flow_results=capital_flow_results
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
        # 输出各维度数据验证
        for code, cr in list(consensus_results.items())[:3]:
            comps = cr.get("components", {})
            fc = comps.get("forecast", {})
            cf = comps.get("capital_flow", {})
            logger.info(f"  {code} 共识: {cr['direction']}({cr['score']:+.0f}) | "
                       f"预测{fc.get('total_score', 'N/A')}分 资金{cf.get('total_score', 'N/A')}分")
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

    # ---- Step 5.6: 波动率目标仓位缩放（在风控前计算，供风控使用）----
    global _prev_vol_scale
    effective_vol_scale = 1.0
    _vol_info_for_digest = {}
    try:
        from position.vol_target import VolTargetManager
        vtm = VolTargetManager(target_vol=0.15)
        vol_result = vtm.calc_position_scale(
            data_dict, holdings,
            market_info={"market_state": market_strength}
        )
        scale = vol_result["scale"]
        # 上升限速：每天最多提升10%；下降不限速（风控收紧立即生效）
        if scale > _prev_vol_scale:
            effective_vol_scale = min(scale, _prev_vol_scale * 1.10)
        else:
            effective_vol_scale = scale
        original_max = get_max_position_ratio(market_strength)
        logger.info(f"  [波动率目标] regime={vol_result['vol_regime']} | "
                    f"缩放={scale:.2f} | {vol_result['recommendation']}")
        logger.info(f"  波动率目标缩放: 原始scale={scale:.2f}, 限速后={effective_vol_scale:.2f}, "
                    f"总仓位上限调整至{original_max * effective_vol_scale:.0%}")
        _vol_info_for_digest = vol_result
    except Exception as e:
        logger.warning(f"  波动率目标异常(不影响主流程，scale=1.0): {e}")
        effective_vol_scale = 1.0

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
            risk_result = risk_check(plan, risk_state, market_strength,
                                      vol_scale=effective_vol_scale)
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

    # ---- Step 6.5: V9.0 智能增强层 ----
    logger.info("[Step 6.5] V9.0智能增强（Meta-Label + 事件日历 + 筹码 + 波动率）...")

    # 6.5a: Meta-Labeling 信号二级过滤
    try:
        from strategy.meta_label import apply_meta_label
        # 构建板块信息映射
        sector_info_map = {}
        try:
            from strategy.stock_screener import screen_strong_sectors
            sector_result = screen_strong_sectors(data_dict)
            for s in sector_result.get("sectors", []):
                for code_s in data_dict:
                    info = config.get_stock_info(code_s)
                    if info.get("赛道", "") == s["sector"]:
                        sector_info_map[code_s] = s
        except Exception:
            pass

        # 使用大盘状态检测器的真实置信度（而非硬编码0.6）
        market_confidence = 0.5
        if market_regime_result and market_regime_result.get("confidence"):
            market_confidence = market_regime_result["confidence"]
        filtered_signals, meta_results = apply_meta_label(
            filtered_signals, data_dict,
            market_info={"market_state": market_strength, "confidence": market_confidence},
            sector_info_map=sector_info_map
        )
        logger.info(f"  [Meta-Label] 过滤后保留{len(filtered_signals)}个信号")
    except Exception as e:
        logger.warning(f"  Meta-Label异常(不影响主流程): {e}")

    # 6.5b: 事件日历风控
    try:
        from strategy.event_calendar import EventCalendar
        event_cal = EventCalendar()
        buy_codes = [c for c, s in filtered_signals if s.get("buy_signal")]
        if buy_codes:
            event_risks = event_cal.batch_check(buy_codes, days_ahead=10)
            for code, risk in event_risks.items():
                if risk["block_buy"]:
                    # 降级为观察
                    for i, (c, s) in enumerate(filtered_signals):
                        if c == code and s.get("buy_signal"):
                            s["buy_signal"] = False
                            s["signal_reason"] += f" [事件风控: {risk['suggestion']}]"
                            logger.info(f"  [事件日历] {code} 买入被阻止: {risk['suggestion']}")
                            break
    except Exception as e:
        logger.warning(f"  事件日历异常(不影响主流程): {e}")

    # 6.5c: 筹码分布分析（写入信号供条件单参考）
    try:
        from strategy.chip_distribution import ChipAnalyzer
        chip_analyzer = ChipAnalyzer()
        for code, sig in filtered_signals:
            if code in data_dict and len(data_dict[code]) >= 30:
                chip = chip_analyzer.analyze(data_dict[code])
                sig["chip_score"] = chip["chip_score"]
                sig["chip_signals"] = chip["signals"]
                sig["trapped_ratio"] = chip["trapped_ratio"]
                # 套牢盘>70%的买入信号降级
                if sig.get("buy_signal") and chip["trapped_ratio"] > 0.70:
                    sig["signal_reason"] += f" [筹码预警: 套牢盘{chip['trapped_ratio']:.0%}]"
    except Exception as e:
        logger.warning(f"  筹码分析异常(不影响主流程): {e}")

    # 6.5d: 波动率目标仓位缩放（已在Step 5.6提前计算并应用于风控，此处仅写入信号）
    if _vol_info_for_digest:
        for code, sig in filtered_signals:
            sig["vol_scale"] = _vol_info_for_digest.get("scale", 1.0)

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

    # ---- Step 8: 发送通知 + 收集综合日报数据 ----
    logger.info("[Step 8] 发送通知 + 收集综合日报数据...")

    # 初始化综合日报数据收集器（中线波段版）
    digest_data = {
        "holdings": holdings,
        "data_dict": data_dict,
        "signals": filtered_signals,
        "holdings_count": list(holdings.keys()),
        "market": {
            "market_state": market_strength,
            "strength": market_regime_result.get("state", "") if market_regime_result else "",
            "suggested_position": max_pos,
        },
        "vol_info": _vol_info_for_digest,
    }

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

    # 条件单邮件不再在15:30发送（统一由scheduler 19:00发送）
    # 但仍执行信号衰减计算（供综合日报展示）
    try:
        from strategy.signal_decay import SignalDecayManager
        decay_mgr = SignalDecayManager()
        expired_count = 0
        for code, sig in filtered_signals:
            freshness = decay_mgr.evaluate_freshness(sig)
            sig["signal_freshness"] = freshness["decay_factor"]
            sig["signal_valid"] = freshness["is_valid"]
            if not freshness["is_valid"]:
                expired_count += 1
        digest_data["v9_summary"] = digest_data.get("v9_summary", {})
        digest_data["v9_summary"]["expired_signals"] = expired_count
    except Exception:
        pass
    
    # ---- Step 8.5: 信号历史记录 + 衰减曲线定期校准 ----
    try:
        from strategy.signal_decay import SignalHistoryTracker
        tracker = SignalHistoryTracker()
    
        # 用最新行情数据回填历史信号的 T+N 收益
        tracker.update_results(data_dict)
    
        # 记录当日所有信号（买入 + 卖出 + 加仓）
        today_str = datetime.date.today().isoformat()
        recorded = 0
        for code, sig in filtered_signals:
            if sig.get("buy_signal") or sig.get("sell_signal") or sig.get("add_position"):
                tracker.record_signal(code, sig, today_str)
                recorded += 1
        logger.info(f"  [信号历史] 记录{recorded}个信号，累计{len(tracker.history)}条历史")
    
        # 每周校准：周五盘后执行衰减曲线校准
        is_friday = datetime.date.today().weekday() == 4
        if is_friday:
            logger.info("  [信号衰减校准] 周五盘后，启动衰减曲线校准...")
            stats = tracker.compute_decay_stats()
            if stats:
                update_summary = tracker.update_decay_curves(stats)
                for sig_type, info in update_summary.items():
                    if info["updated"]:
                        logger.info(
                            f"    {sig_type}: 已校准 "
                            f"(样本{info['sample_count']}, "
                            f"T1胜率{info['ci']['t1']['wr']:.0%}, "
                            f"T3胜率{info['ci']['t3']['wr']:.0%}, "
                            f"T5胜率{info['ci']['t5']['wr']:.0%})"
                        )
                    else:
                        logger.info(
                            f"    {sig_type}: 未校准 - {info['reason']}"
                        )
            else:
                # 统计各类型样本量，输出冷启动提示
                all_stats = {}
                for entry in tracker.history:
                    key = entry.get("signal_type", "unknown")
                    all_stats[key] = all_stats.get(key, 0) + 1
                for signal_type, count in all_stats.items():
                    logger.info(
                        f"    信号衰减校准: {signal_type} "
                        f"样本量{count}/30, 暂使用默认衰减曲线"
                    )
        else:
            # 非周五，输出各类型当前样本积累进度
            all_stats = {}
            for entry in tracker.history:
                key = entry.get("signal_type", "unknown")
                all_stats[key] = all_stats.get(key, 0) + 1
            progress_parts = [f"{k}:{v}/30" for k, v in all_stats.items()]
            if progress_parts:
                logger.info(f"  [信号衰减] 样本积累: {', '.join(progress_parts)} (周五校准)")
    except Exception as e:
        logger.warning(f"  信号历史/衰减校准异常(不影响核心流程): {e}")

    # ---- Step 8.6: 滑点追踪自动化（每日记录 + 月度报告）----
    try:
        from execution.slippage_tracker import SlippageTracker
        slippage_tracker = SlippageTracker()
        
        # 每日自动记录信号的预期价格
        recorded_count = slippage_tracker.auto_record_from_signals(filtered_signals)
        if recorded_count > 0:
            logger.info(f"  [滑点追踪] 记录{recorded_count}笔信号预期价格")
        
        # 每月1日生成月度滑点报告 + 回测参数校准建议
        if datetime.date.today().day == 1:
            logger.info("  [滑点追踪] 月度滑点报告生成...")
            monthly_report = slippage_tracker.generate_monthly_report()
            total = monthly_report.get("total_trades", 0)
            logger.info(f"    本月滑点记录: {total}笔")
            if total > 0:
                logger.info(f"    {monthly_report['comparison']}")
                logger.info(f"    统计详情: 平均{monthly_report['avg_slippage']:.3%}, "
                           f"中位{monthly_report['median_slippage']:.3%}, "
                           f"最大{monthly_report['max_slippage']:.3%}, "
                           f"标准差{monthly_report['std_slippage']:.3%}")
                # 输出配置变更历史
                change_count = monthly_report.get('config_change_count', 0)
                if change_count > 0:
                    logger.info(f"    本月配置变更{change_count}次:")
                    for ch in monthly_report.get('config_changes', []):
                        logger.info(f"      {ch['param']}: {ch['old']:.6f} → {ch['new']:.6f} ({ch['time'][:10]})")
                else:
                    logger.info(f"    本月配置变更: 0次")
                
                # 检查是否需要调整回测参数
                adjustment = slippage_tracker.suggest_backtest_adjustment()
                if adjustment["should_adjust"]:
                    logger.warning(f"    建议调整回测滑点: {adjustment['reason']}")
                    logger.info(f"    当前: {adjustment['current_assumption']:.3%} → 建议: {adjustment['suggested_value']:.3%}")
                    # 自动写入 config.py
                    if slippage_tracker.apply_backtest_adjustment(adjustment['suggested_value']):
                        logger.info(f"    ✓ buy_slippage 已自动更新为 {adjustment['suggested_value']:.6f}")
                    else:
                        logger.warning(f"    ✗ buy_slippage 自动更新失败，请手动修改 config.py")
    except Exception as e:
        logger.warning(f"  滑点追踪异常(不影响核心流程): {e}")

    # ---- Step 9: 盘后选股 + 全市场扫描 + 持仓诊断 ----
    logger.info("[Step 9] 盘后选股引擎（全赛道+弱势模式）...")
    try:
        # 全市场动态扫描，发现强势股补充候选池
        logger.info("  [市场扫描] 尝试全市场动态扫描...")
        try:
            scan_result = scan_market_hot_stocks(total_max=getattr(config, "SCAN_TOTAL_MAX", 100))
            if scan_result["success"]:
                new_codes = merge_scan_results_to_pool(scan_result, set(data_dict.keys()))
                # 两阶段扩面处理全部新增候选（内存粗筛 → 预算制日线拉取 → hard_filter）
                added_count = 0
                try:
                    added_count = _expand_scan_candidates(new_codes, scan_result, data_dict, conn)
                except Exception as exp_e:
                    # 两阶段扩面异常 → 回退原有行为：最多拉取10只直接入库
                    logger.warning(f"  扫描扩面两阶段处理异常，回退原有逻辑: {exp_e}")
                    for code in new_codes[:10]:  # 最多追加10只
                        try:
                            df_new = load_daily_data(code, conn, days=120)
                            if not df_new.empty and len(df_new) >= config.MA_SHORT:
                                df_new = compute_indicators(df_new)
                                data_dict[code] = df_new
                                added_count += 1
                        except Exception:
                            pass
                logger.info(f"  动态扫描完成，新增{len(new_codes)}只候选股（实际入库{added_count}只）")
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
        
        # 收集选股结果到综合日报（不再单独发邮件）
        digest_data["screener"] = screener_result
        logger.info(f"  选股完成: {screener_result['qualified_count']}只入选, "
                   f"{len(screener_result.get('watch_list', []))}只观察")
    except Exception as e:
        logger.error(f"  选股引擎异常: {e}")

    # ---- Step 10: 仓位管理与资金优化分析（V7.1: 引用共识）----
    logger.info("[Step 10] 仓位管理分析...")
    try:
        portfolio_result = analyze_portfolio(holdings, data_dict, consensus_results)
        # 收集到综合日报
        digest_data["portfolio"] = portfolio_result.get("summary", {})
        digest_data["portfolio"]["risk_alerts"] = portfolio_result.get("risk_alerts", [])
        digest_data["portfolio"]["optimization"] = portfolio_result.get("optimization", {})
        logger.info(f"  仓位分析完成 ({len(portfolio_result['risk_alerts'])}项风险预警)")
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
        # 收集到综合日报
        digest_data["risk_report"] = risk_report
    except Exception as e:
        logger.error(f"  组合风控分析异常: {e}")

    # ---- Step 12: 基本面+资金流分析（V3.0新增）----
    logger.info("[Step 12] 基本面与资金流分析...")
    step12_flow_report = None  # FIX: 保存Step12资金流结果供Step15复用，避免重复调用CapitalFlowAnalyzer
    try:
        # 资金流向分析
        cfa = CapitalFlowAnalyzer()
        flow_report = cfa.full_analysis(list(holdings.keys()) + list(config.STOCK_POOL.keys())[:5])
        step12_flow_report = flow_report  # 保存供后续Step复用
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

    # ---- Step 14: 持仓趋势预测分析（复用Step5.1.5已计算结果）----
    if getattr(config, 'FORECAST_ENABLED', True):
        logger.info("[Step 14] 持仓趋势预测分析...")
        try:
            # 复用提前计算的预测结果（避免重复计算）
            forecast_results = early_forecast_results if early_forecast_results else []
            if forecast_results:
                # 输出摘要
                for r in forecast_results:
                    score = r["composite"]["total_score"]
                    logger.info(f"  {r['code']} {r['name']}: {score:.0f}分 [{r['composite']['rating']}] "
                               f"| {r['advice']['action']} | 时间: {r['advice']['timing']['best_time']}")
                # 收集到综合日报
                digest_data["forecast"] = forecast_results
            else:
                logger.info("  无有效预测数据")
        except Exception as e:
            logger.error(f"  趋势预测分析异常: {e}")

    # ---- Step 15: 发送盘后综合日报（合并原6封邮件为1封）----
    logger.info("[Step 15] 发送盘后综合日报...")
    if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
        try:
            # FIX: 修复 CapitalFlowAnalyzer 重复调用，复用Step12已计算的资金流结果
            # Step15不再创建新的CapitalFlowAnalyzer，仅复用Step12结果
            try:
                if step12_flow_report is not None:
                    flow_report = step12_flow_report
                    logger.info("  复用Step12资金流分析结果")
                    if flow_report.get('northbound', {}).get('success'):
                        digest_data["market"]["northbound"] = flow_report['northbound']
                    if flow_report.get('sector_flow', {}).get('success'):
                        digest_data["market"]["hot_sectors"] = flow_report['sector_flow'].get('hot_sectors', [])
                else:
                    logger.info("  Step12资金流结果不可用，跳过Step15资金流补充")
            except Exception:
                pass

            # 补充V9.0摘要
            v9 = digest_data.get("v9_summary", {})
            # Meta-Label统计
            meta_stats = {"execute": 0, "observe": 0, "reject": 0}
            for code, sig in filtered_signals:
                meta = sig.get("meta_label", {})
                action = meta.get("action", "")
                if action in meta_stats:
                    meta_stats[action] += 1
            if any(meta_stats.values()):
                v9["meta_label"] = meta_stats
            # 波动率
            try:
                vol_info = digest_data.get("vol_info", {})
                if vol_info:
                    v9["vol_scale"] = vol_info.get("scale")
                    v9["vol_regime"] = vol_info.get("vol_regime", "")
                    v9["recommendation"] = vol_info.get("recommendation", "")
            except Exception:
                pass
            digest_data["v9_summary"] = v9

            from output.daily_digest import send_daily_digest
            digest_ok = send_daily_digest(digest_data)
            if digest_ok:
                logger.info("  盘后综合日报发送成功")
            else:
                logger.warning("  盘后综合日报发送失败")
        except Exception as e:
            logger.error(f"  综合日报发送异常: {e}")
    else:
        logger.info("  邮箱未配置，跳过综合日报发送")

    # ---- 更新波动率缩放限速状态（供下次运行使用）----
    _prev_vol_scale = effective_vol_scale
    _save_vol_scale_state(_prev_vol_scale)  # FIX: 持久化波动率缩放状态到JSON文件

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
    parser = argparse.ArgumentParser(description="高胜率A股交易操作系统 V9.0")
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
    maybe_auto_bootstrap()  # FIX: 将自动初始化移入__main__块，避免import时触发数据下载
    main()
