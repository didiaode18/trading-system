# -*- coding: utf-8 -*-
"""
操盘密码报告工具 V2.0 (CLI手动模式)
========================
整合P0-P5全部模块，按「4+1」体系输出分析报告

❗ 重要说明 (V2.0):
  本文件已降级为CLI手动报告生成工具，不再作为常驻调度器。
  定时任务已统一由 trading_system/scheduler.py 负责。
  请勿将本文件与 scheduler.py 同时作为调度器运行，避免重复发送邮件。

报告体系:
  1. [盘前] 盘前作战计划 - 板块方向+操作清单+关键价位+仓位建议
  2. [盘后] 盘后深度复盘 - 九大板块全量分析(趋势/DK/资金/筹码/板块/多因子/仓位/预警/持仓)
  3. [周报] 周策略报告 - 本周绩效+板块轮动+仓位再平衡+下周计划
  4. [选股] CANSLIM选股报告 - 三层候选池+实时融合+涨停复盘

运行(CLI手动模式):
  python caopan_report.py --morning      # 立即生成盘前报告
  python caopan_report.py --evening      # 立即生成盘后报告
  python caopan_report.py --weekly       # 立即生成周报
  python caopan_report.py --screener     # 运行选股引擎
  python caopan_report.py --alert        # 启动盘中实时预警循环(每3分钟)
"""

import sys
import os
import io
import json
import datetime
import logging
import argparse

# Windows编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADING_SYSTEM_DIR = os.path.join(BASE_DIR, "trading_system")
sys.path.insert(0, TRADING_SYSTEM_DIR)
sys.path.insert(0, BASE_DIR)

import config
from strategy.caopan_signal import CaopanEngine
from strategy.market_regime import MarketRegimeDetector
from strategy.chip_distribution import ChipAnalyzer, chip_summary
from strategy.sector_flow import SectorMonitor, sector_summary
from strategy.multi_factor import MultiFactorScorer, factor_summary
from risk.position_sizing import PositionSizer, position_summary
from notify.alert_engine import AlertEngine
from notify.email_notify import send_email
from output.report_email import (
    build_morning_email, build_evening_email,
    build_orders_email, build_weekly_email
)
from output.report_charts import (
    generate_kline_chart, generate_fund_flow_chart,
    generate_position_pie, generate_sector_bar
)
from strategy.stock_screener import run_stock_screener, send_screener_email
from strategy.capital_flow import CapitalFlowAnalyzer
from strategy.market_scanner import scan_market_hot_stocks, merge_scan_results_to_pool
from strategy.pool_manager import PoolManager
from data.realtime import fetch_realtime_batch

# 新数据模块（可选导入）
try:
    from trading_system.strategy.lhb_analyzer import LHBAnalyzer
    HAS_LHB = True
except:
    HAS_LHB = False

try:
    from trading_system.strategy.margin_monitor import MarginMonitor
    HAS_MARGIN = True
except:
    HAS_MARGIN = False

try:
    from trading_system.strategy.zt_monitor import ZTMonitor
    HAS_ZT = True
except:
    HAS_ZT = False

try:
    from trading_system.strategy.holder_monitor import HolderMonitor
    HAS_HOLDER = True
except:
    HAS_HOLDER = False

try:
    from trading_system.strategy.event_calendar import EventCalendar
    HAS_CALENDAR = True
except:
    HAS_CALENDAR = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(TRADING_SYSTEM_DIR, "output", "reports")


# ============================================================
# 数据获取（V2.0 性能优化：共享会话 + 批量拉取）
# ============================================================

_bs_logged_in = False  # baostock会话状态标记


def _ensure_bs_login():
    """确保baostock已登录（复用会话，避免每只股票重复login/logout）"""
    global _bs_logged_in
    if not _bs_logged_in:
        import baostock as bs
        bs.login()
        _bs_logged_in = True


def _bs_logout():
    """报告全部完成后统一登出"""
    global _bs_logged_in
    if _bs_logged_in:
        import baostock as bs
        try:
            bs.logout()
        except Exception:
            pass
        _bs_logged_in = False


def fetch_stock_data(code: str, days: int = 500):
    """获取K线数据（复用baostock会话，不再每次login/logout）"""
    try:
        import baostock as bs
        import pandas as pd
        _ensure_bs_login()
        prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"
        bs_code = f"{prefix}.{code}"
        end = datetime.date.today().strftime("%Y-%m-%d")
        start = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="d", adjustflag="2"
        )
        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.tail(days).reset_index(drop=True)
    except Exception as e:
        logger.error(f"获取{code}数据失败: {e}")
        return None


def fetch_batch_data(codes: list, days: int = 500) -> dict:
    """批量获取多只股票K线数据（单次登录，顺序查询，最后统一登出）
    
    性能优化：避免N只股票做N次login/logout（原每次约2-3秒）
    """
    import pandas as pd
    _ensure_bs_login()
    import baostock as bs
    
    result = {}
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y-%m-%d")
    
    for code in codes:
        try:
            prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"
            bs_code = f"{prefix}.{code}"
            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume,amount",
                start_date=start, end_date=end, frequency="d", adjustflag="2"
            )
            rows = []
            while rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                continue
            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            result[code] = df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.error(f"批量获取{code}失败: {e}")
    
    return result


def _merge_realtime_row(df, quote: dict):
    """将实时行情融合到baostock历史DataFrame最后一行
    
    逻辑（复用caopan_realtime_report.py的merge_realtime模式）:
      - 如果最后一行已是今日 → 更新close/high/low/volume
      - 否则 → 追加今日新行
    """
    import pandas as pd
    if not quote or quote.get("price", 0) <= 0:
        return df
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    price = quote["price"]
    high = quote.get("high", price)
    low = quote.get("low", price)
    volume = quote.get("volume", 0) * 100  # 手→股

    if len(df) > 0 and df["date"].iloc[-1] == today_str:
        # 更新当日数据
        df = df.copy()
        df.iloc[-1, df.columns.get_loc("close")] = price
        df.iloc[-1, df.columns.get_loc("high")] = max(high, df["high"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")] = min(low, df["low"].iloc[-1])
        if volume > 0:
            df.iloc[-1, df.columns.get_loc("volume")] = volume
    else:
        # 追加当日数据
        prev_close = df["close"].iloc[-1] if len(df) > 0 else price
        new_row = {
            "date": today_str,
            "open": quote.get("open", prev_close),
            "high": high,
            "low": low,
            "close": price,
            "volume": volume,
            "amount": quote.get("amount", 0) * 10000,
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    return df


def load_holdings() -> dict:
    """加载持仓"""
    NAME_MAP = {
        "588000": "科创50", "002415": "海康威视", "603501": "豪威集团",
        "002409": "雅克科技", "002185": "华天科技", "600036": "招商银行",
        "159205": "创业东财", "600276": "恒瑞医药", "603993": "洛阳钼业",
    }
    holdings_file = os.path.join(BASE_DIR, "holdings.json")
    if os.path.exists(holdings_file):
        with open(holdings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        for code in data:
            if "name" not in data[code]:
                data[code]["name"] = NAME_MAP.get(code, code)
        return data
    return {code: {"name": name} for code, name in NAME_MAP.items()}


def run_full_analysis(holdings: dict) -> list:
    """运行完整分析（V2.0: 批量拉取数据，避免重复登录）"""
    engine = CaopanEngine()
    results = []

    # 大盘状态检测
    market_regime = None
    try:
        from data.data_loader import load_daily_data, init_db
        conn = init_db()
        benchmark_df = load_daily_data(config.BENCHMARK_INDEX, conn, days=120)
        if not benchmark_df.empty and len(benchmark_df) >= 60:
            detector = MarketRegimeDetector()
            market_regime = detector.detect(benchmark_df)
        conn.close()
    except Exception:
        pass

    # 批量获取所有持仓股票数据（单次登录）
    codes = list(holdings.keys())
    print(f"  批量获取{len(codes)}只股票数据...")
    data_cache = fetch_batch_data(codes, days=500)
    print(f"  成功获取{len(data_cache)}/{len(codes)}只")

    for code, info in holdings.items():
        name = info.get("name", code)
        df = data_cache.get(code)
        if df is None or len(df) < 60:
            continue
        result = engine.analyze(df, code=code, name=name, market_regime=market_regime)
        if "error" not in result:
            results.append(result)
    return results


# ============================================================
# 报告1: 盘前作战计划 (08:30)
# ============================================================

def generate_morning_brief(results: list, holdings: dict) -> str:
    """
    盘前作战计划 - 精简版，30秒内看完
    核心: 今天干什么 + 关键价位 + 仓位建议
    """
    now = datetime.datetime.now()
    lines = []
    lines.append(f"{'═' * 50}")
    lines.append(f"  📋 盘前作战计划 | {now.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"{'═' * 50}")

    # 1. 市场环境（一句话）
    envs = [r.get("market_env", {}).get("mode", "") for r in results]
    trend_count = envs.count("trend")
    osc_count = envs.count("oscillation")
    if trend_count > osc_count:
        env_desc = "趋势市（信号有效，顺势操作）"
    elif osc_count > trend_count:
        env_desc = "震荡市（信号减弱，高抛低吸）"
    else:
        env_desc = "转换期（谨慎操作，控制仓位）"
    lines.append(f"\n  🌍 市场环境: {env_desc}")

    # 2. 板块方向（P1）
    try:
        holdings_data = {r.get("code", ""): r.get("df_analyzed") for r in results if r.get("df_analyzed") is not None}
        monitor = SectorMonitor()
        sector_result = monitor.analyze(holdings_data, holdings)
        ranked = sector_result.get("ranked", [])
        if ranked:
            top = ranked[0]
            bottom = ranked[-1]
            lines.append(f"  📊 板块方向: 最强={top['sector']}({top['return_5d']:+.1f}%) | 最弱={bottom['sector']}({bottom['return_5d']:+.1f}%)")
            signals = sector_result.get("signals", [])
            for s in signals[:2]:
                lines.append(f"     {s['desc']}")
    except Exception:
        pass

    # 3. 今日操作清单（最重要）
    lines.append(f"\n  ━━ 今日操作 ━━")
    for r in results:
        action = r.get("action_suggestion", {})
        urgency = action.get("urgency", "normal")
        if urgency in ("critical", "high"):
            icon = "🚨" if urgency == "critical" else "⚡"
            lines.append(f"  {icon} {r['name']}: {action.get('desc','')} | {action.get('detail','')}")

    # 4. 关键价位（支撑/压力/止损）
    lines.append(f"\n  ━━ 关键价位 ━━")
    for r in results:
        close = r.get("close", 0)
        support = r.get("support_price", 0)
        resistance = r.get("resistance_price", 0)
        chip = r.get("chip", {})
        chip_support = chip.get("support", 0) if chip else 0
        trend = r.get("trend_desc", "")
        lines.append(f"  {r['name']:<8} 现价{close:.2f} | 支撑{max(support, chip_support):.2f} | 压力{resistance:.2f} | {trend}")

    # 5. 仓位建议（P5精简版）
    try:
        sizer = PositionSizer(total_capital=config.TOTAL_CAPITAL)
        plan = sizer.calc_positions(results, holdings)
        risk = plan.get("portfolio_risk", {})
        lines.append(f"\n  💼 仓位: 风险{risk.get('risk_level','-')} | 预估回撤{risk.get('max_drawdown_est',0):.1f}% | 配置{plan.get('total_allocated',0)/10000:.1f}万/{plan.get('total_capital',0)/10000:.1f}万")
        rebalance = plan.get("rebalance", [])
        if rebalance:
            lines.append(f"  📝 再平衡:")
            for rb in rebalance[:5]:
                lines.append(f"     {rb['action']} {rb['name']} {rb['shares']}股 ({rb['amount']/10000:.1f}万)")
    except Exception:
        pass

    lines.append(f"\n{'═' * 50}")
    return "\n".join(lines)


# ============================================================
# 报告2: 盘后深度复盘 (15:30)
# ============================================================

def generate_evening_report(results: list, holdings: dict,
                           sector_result=None, scored=None, plan=None) -> str:
    """
    盘后深度复盘 - 九大板块全量分析
    V2.0: 支持传入已计算结果，避免重复计算
    """
    now = datetime.datetime.now()
    lines = []
    lines.append(f"{'═' * 60}")
    lines.append(f"  📊 操盘密码 V9.0 盘后深度复盘")
    lines.append(f"  {now.strftime('%Y-%m-%d %H:%M')} | {len(results)}只标的")
    lines.append(f"{'═' * 60}")

    # 一、趋势分布
    lines.append(f"\n  ━━ 一、趋势分布 ━━")
    trend_names = {5: "强上升", 4: "弱上升", 3: "震荡", 2: "弱下跌", 1: "强下跌"}
    for lv in [5, 4, 3, 2, 1]:
        stocks = [r for r in results if r.get("trend_level") == lv]
        if stocks:
            names = "、".join([r["name"] for r in stocks])
            lines.append(f"     {trend_names[lv]}({lv}级): {names}")

    # 二、DK信号
    lines.append(f"\n  ━━ 二、DK信号 ━━")
    for r in results:
        dk = r.get("dk_signal") or "无"
        strength = r.get("dk_strength", 0)
        filtered = "[过滤]" if r.get("dk_filtered") else ""
        lines.append(f"     {r['name']:<8} DK={dk}({strength}分){filtered} | {r.get('dk_reason','')}")

    # 三、资金动向
    lines.append(f"\n  ━━ 三、资金动向 ━━")
    for r in results:
        fd = r.get("fund_data", {})
        streak = r.get("main_flow_streak", 0)
        pattern = r.get("fund_pattern", "normal")
        pattern_cn = {"mild_build": "温和建仓", "surge": "放量拉升", "fake": "对倒骗线", "normal": "-"}.get(pattern, "-")
        lines.append(f"     {r['name']:<8} 主力连流{streak}天 | {pattern_cn} | 资金评分{fd.get('score',50)} | {fd.get('signal','-')}")

    # 四、筹码分布
    lines.append(f"\n  ━━ 四、筹码分布 ━━")
    for r in results:
        chip = r.get("chip")
        if not chip:
            continue
        pr = chip.get("profit_ratio", 0)
        conc = chip.get("concentration", 0)
        ctrl = chip.get("control_level", {})
        pattern = chip.get("pattern", {})
        lines.append(f"     {r['name']:<8} 获利{pr*100:.0f}% | 集中{conc*100:.1f}% | {ctrl.get('level','-')}({ctrl.get('score',0)}分) | {pattern.get('name','-')}")

    # 五、板块轮动
    lines.append(f"\n  ━━ 五、板块轮动 ━━")
    try:
        if sector_result is None:
            holdings_data = {r.get("code", ""): r.get("df_analyzed") for r in results if r.get("df_analyzed") is not None}
            monitor = SectorMonitor()
            sector_result = monitor.analyze(holdings_data, holdings)
        lines.append(sector_summary(sector_result))
    except Exception as e:
        lines.append(f"     异常: {e}")

    # 六、多因子评分
    lines.append(f"\n  ━━ 六、多因子评分 ━━")
    try:
        if scored is None:
            scorer = MultiFactorScorer()
            scored = scorer.score_all(results)
        lines.append(factor_summary(scored))
    except Exception as e:
        lines.append(f"     异常: {e}")

    # 七、仓位管理
    lines.append(f"\n  ━━ 七、仓位管理 ━━")
    try:
        if plan is None:
            sizer = PositionSizer(total_capital=config.TOTAL_CAPITAL)
            plan = sizer.calc_positions(results, holdings)
        lines.append(position_summary(plan))
    except Exception as e:
        lines.append(f"     异常: {e}")

    # 八、风险提示
    lines.append(f"\n  ━━ 八、风险提示 ━━")
    for r in results:
        if r.get("trend_level", 3) <= 2:
            lines.append(f"     🚨 {r['name']}: 下跌趋势({r.get('trend_level')}级)")
        if r.get("fund_pattern") == "fake":
            lines.append(f"     ⚠️ {r['name']}: 对倒骗线")
        if r.get("top_divergence"):
            lines.append(f"     ⚠️ {r['name']}: 顶背离")

    # 九、持仓盈亏
    lines.append(f"\n  ━━ 九、持仓盈亏 ━━")
    total_cost = 0
    total_value = 0
    for r in results:
        code = r.get("code", "")
        info = holdings.get(code, {})
        shares = info.get("shares", 0)
        cost = info.get("cost", 0) or info.get("buy_price", 0)
        close = r.get("close", 0)
        if shares and close > 0:
            cost_val = shares * cost
            mkt_val = shares * close
            # 成本<=0表示已完全回本，盈亏比例无意义，显示绝对收益
            if cost > 0:
                pnl_pct = (mkt_val - cost_val) / cost_val * 100
                pnl_str = f"{pnl_pct:+.1f}%"
            else:
                pnl_pct = 0
                pnl_str = f"+{mkt_val - cost_val:,.0f}元(已回本)"
            total_cost += max(cost_val, 0)
            total_value += mkt_val
            icon = "📈" if (mkt_val - cost_val) > 0 else "📉"
            lines.append(f"     {icon} {r['name']:<8} {pnl_str} | 市值{mkt_val/10000:.1f}万")
    if total_cost > 0:
        total_pnl = (total_value - total_cost) / total_cost * 100
        lines.append(f"     {'─'*40}")
        lines.append(f"     总盈亏: {total_pnl:+.1f}% | 市值{total_value/10000:.1f}万")

    # 十、V9.0盘口综合（K线形态 + 主力阶段 + 缺口分析）
    lines.append(f"\n  ━━ 十、V9.0盘口综合 ━━")
    for r in results:
        df = r.get("df_analyzed")
        if df is None or len(df) < 20:
            continue
        name = r.get("name", "")
        tags = []
        # K线形态
        last = df.iloc[-1]
        prev = df.iloc[-2]
        o, h, l, c = last["open"], last["high"], last["low"], last["close"]
        body = abs(c - o)
        full_range = h - l
        if full_range > 0:
            body_ratio = body / full_range
            if body_ratio < 0.1:
                tags.append("K线:十字星")
            elif (min(o, c) - l) > body * 2 and (h - max(o, c)) < body * 0.3:
                tags.append("K线:锤子线")
            elif c > o and prev["close"] < prev["open"] and body > abs(prev["close"] - prev["open"]) * 1.2:
                tags.append("K线:看涨吞没")
            elif c < o and prev["close"] > prev["open"] and body > abs(prev["close"] - prev["open"]) * 1.2:
                tags.append("K线:看跌吞没")
            elif body_ratio > 0.7:
                tags.append(f"K线:{'大阳线' if c > o else '大阴线'}")
        # 主力阶段
        recent = df.tail(20)
        price_chg = (recent["close"].iloc[-1] - recent["close"].iloc[0]) / recent["close"].iloc[0]
        vol_h1 = recent["volume"].iloc[:10].mean()
        vol_h2 = recent["volume"].iloc[10:].mean()
        vol_r = vol_h2 / vol_h1 if vol_h1 > 0 else 1.0
        if price_chg > 0.05 and vol_r > 1.3:
            tags.append("主力:拉升期")
        elif price_chg > 0.03 and vol_r < 0.8:
            tags.append("主力:控盘期")
        elif abs(price_chg) < 0.05 and vol_r < 0.9:
            tags.append("主力:吸筹期")
        elif price_chg < -0.05 and vol_r > 1.2:
            tags.append("主力:出货期")
        elif price_chg < -0.03 and vol_r < 0.8:
            tags.append("主力:洗盘期")
        # 缺口
        gap_type = last.get("gap_type", "none") if hasattr(last, 'get') else "none"
        gap_up = last.get("gap_up", False) if hasattr(last, 'get') else False
        gap_down = last.get("gap_down", False) if hasattr(last, 'get') else False
        if gap_up or gap_down:
            gap_label = {"breakaway": "突破缺口", "exhaustion": "衰竭缺口", "common": "普通缺口"}.get(gap_type, "缺口")
            tags.append(f"缺口{'↑' if gap_up else '↓'}:{gap_label}")
        if tags:
            lines.append(f"     {name:<8} {' | '.join(tags)}")
    if not any(r.get("df_analyzed") is not None and len(r.get("df_analyzed", [])) >= 20 for r in results):
        lines.append("     (数据不足，跳过)")

    lines.append(f"\n{'═' * 60}")
    return "\n".join(lines)


# ============================================================
# 报告3: 周策略报告 (周六 10:00)
# ============================================================

def generate_weekly_report(results: list, holdings: dict) -> str:
    """
    周策略报告 - 中期波段节奏把控
    核心: 本周绩效 + 板块轮动趋势 + 仓位再平衡 + 下周计划
    """
    now = datetime.datetime.now()
    lines = []
    lines.append(f"{'═' * 55}")
    lines.append(f"  📅 周策略报告 | {now.strftime('%Y-%m-%d')} (第{now.isocalendar()[1]}周)")
    lines.append(f"{'═' * 55}")

    # 1. 本周持仓表现
    lines.append(f"\n  ━━ 本周持仓表现 ━━")
    winners = []
    losers = []
    for r in results:
        code = r.get("code", "")
        info = holdings.get(code, {})
        shares = info.get("shares", 0)
        cost = info.get("cost", 0) or info.get("buy_price", 0)
        close = r.get("close", 0)
        if shares and close > 0:
            # 成本<=0表示已完全回本，盈亏比例无意义
            if cost > 0:
                pnl = (close - cost) / cost * 100
            else:
                pnl = 100.0  # 已回本视为正收益
            if pnl > 0:
                winners.append((r["name"], pnl))
            else:
                losers.append((r["name"], pnl))

    winners.sort(key=lambda x: x[1], reverse=True)
    losers.sort(key=lambda x: x[1])
    for name, pnl in winners:
        lines.append(f"     📈 {name}: {pnl:+.1f}%")
    for name, pnl in losers:
        lines.append(f"     📉 {name}: {pnl:+.1f}%")

    # 2. 板块轮动趋势
    lines.append(f"\n  ━━ 板块轮动趋势 ━━")
    try:
        holdings_data = {r.get("code", ""): r.get("df_analyzed") for r in results if r.get("df_analyzed") is not None}
        monitor = SectorMonitor()
        sector_result = monitor.analyze(holdings_data, holdings)
        ranked = sector_result.get("ranked", [])
        for m in ranked:
            status_icon = {"启动": "🚀", "加速": "⚡", "上升": "📈", "震荡": "➡️", "流出": "📉", "下跌": "⬇️"}.get(m["status"], "")
            lines.append(f"     {status_icon} {m['sector']:<6} 动量{m['momentum_score']:.0f}分 | 5日{m['return_5d']:+.1f}% | {m['status']}")
    except Exception:
        pass

    # 3. 仓位再平衡建议
    lines.append(f"\n  ━━ 仓位再平衡 ━━")
    try:
        sizer = PositionSizer(total_capital=config.TOTAL_CAPITAL)
        plan = sizer.calc_positions(results, holdings)
        rebalance = plan.get("rebalance", [])
        if rebalance:
            for rb in rebalance:
                icon = "🔴" if rb["action"] in ("买入", "加仓") else "🟢"
                lines.append(f"     {icon} {rb['name']} {rb['action']} {rb['shares']}股 | {rb['reason']}")
        else:
            lines.append(f"     ✅ 当前仓位合理，无需调整")
        risk = plan.get("portfolio_risk", {})
        lines.append(f"     风险: {risk.get('risk_level','-')} | 回撤预估{risk.get('max_drawdown_est',0):.1f}%")
    except Exception:
        pass

    # 4. 下周策略
    lines.append(f"\n  ━━ 下周策略 ━━")
    # 基于趋势和信号给出策略
    strong = [r for r in results if r.get("trend_level", 3) >= 4]
    weak = [r for r in results if r.get("trend_level", 3) <= 2]
    if strong:
        names = "、".join([r["name"] for r in strong])
        lines.append(f"     持有: {names} (上升趋势，持股待涨)")
    if weak:
        names = "、".join([r["name"] for r in weak])
        lines.append(f"     回避: {names} (下跌趋势，不抄底)")

    # DK信号前瞻
    d_signals = [r for r in results if r.get("dk_signal") == "D" and not r.get("dk_filtered")]
    if d_signals:
        names = "、".join([r["name"] for r in d_signals])
        lines.append(f"     关注: {names} (D点信号，等待回踩确认)")

    lines.append(f"\n{'═' * 55}")
    return "\n".join(lines)


# ============================================================
# 调度器
# ============================================================

def run_morning():
    """盘前作战计划（08:30）- 1封邮件"""
    print("\n" + "=" * 50)
    print("  生成盘前作战计划...")
    holdings = load_holdings()
    results = run_full_analysis(holdings)
    if results:
        # 文本报告（本地存档）
        report = generate_morning_brief(results, holdings)
        print(report)
        _save_report("morning", report)

        # 板块数据
        sector_result = _get_sector_result(results, holdings)
        # 仓位计划
        plan = _get_position_plan(results, holdings)
        # 图表
        charts = {"position_pie": generate_position_pie(holdings, results)}

        # 涨停数据
        zt_data = None
        if HAS_ZT:
            try:
                zt_data = ZTMonitor().get_daily_report()
            except Exception:
                pass

        # 解禁数据
        release_data = None
        if HAS_CALENDAR:
            try:
                release_data = EventCalendar().get_release_summary(days_ahead=30)
            except Exception:
                pass

        # 发送美观HTML邮件
        today = datetime.date.today().strftime("%Y-%m-%d")
        html = build_morning_email(results, holdings, sector_result, plan, charts,
                                   zt_data=zt_data, release_data=release_data)
        _send_html_email(f"[操盘密码] 📋盘前作战计划 | {today}", html)
    return results


def run_screener():
    """运行选股引擎并发送选股报告邮件
    
    V2.2: 三层候选池架构
      第1层: SECTOR_CANDIDATES 静态配置池（29只，8大赛道）
      第2层: PoolManager 观察池（动态维护，最多10只）
      第3层: scan_market_hot_stocks 全市场动态扫描（最多15只）
    """
    print("\n" + "=" * 50)
    print("  运行CANSLIM选股引擎...")
    print("=" * 50)
    holdings = load_holdings()

    # ---- 第1层: 静态配置池 ----
    sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
    all_codes = set()
    for sector_name, sector_info in sector_candidates.items():
        stocks = sector_info.get("stocks", {})
        all_codes.update(stocks.keys())
    static_count = len(all_codes)

    # ---- 第2层: PoolManager观察池 ----
    pm = PoolManager()
    watch_codes = pm.get_watch_codes()
    pool_new = 0
    for code in watch_codes:
        if code not in all_codes:
            all_codes.add(code)
            pool_new += 1
    if pool_new > 0:
        print(f"  观察池补充: +{pool_new}只 (PoolManager)")

    # ---- 第3层: 全市场动态扫描 ----
    scan_new = 0
    try:
        scan_result = scan_market_hot_stocks(total_max=15)
        if scan_result.get("success"):
            new_codes = merge_scan_results_to_pool(scan_result, all_codes)
            # 最多追加15只动态发现股（控制baostock拉取耗时）
            for code in new_codes[:15]:
                all_codes.add(code)
                scan_new += 1
            if scan_new > 0:
                top3 = [f"{d['code']} {d['name']}({d['change_pct']:+.1f}%)"
                        for d in scan_result['details'][:3]]
                cache_tag = "(缓存)" if scan_result.get("from_cache") else ""
                print(f"  动态扫描补充{cache_tag}: +{scan_new}只 | 强势股: {', '.join(top3)}")
        else:
            print(f"  动态扫描: 未成功（非盘中/网络异常），使用已有候选池")
    except Exception as e:
        logger.warning(f"  动态扫描异常: {e}，继续使用已有候选池")

    # 加入指数数据
    all_codes.add("000300")
    print(f"  候选股票池: {len(all_codes)}只 "
          f"(静态{static_count} + 观察池{pool_new} + 动态{scan_new} + 指数1)")

    # V2.0性能优化：批量拉取（单次登录）
    raw_data = fetch_batch_data(list(all_codes), days=300)

    # V2.1: 融合盘中实时行情（腾讯行情API），确保选股基于最新市场状态
    realtime_codes = [c for c in all_codes if c != "000300"]  # 指数不需要实时融合
    quotes = fetch_realtime_batch(realtime_codes)
    merged_count = 0
    if quotes:
        for code, df in raw_data.items():
            if df is None or df.empty:
                continue
            quote = quotes.get(code)
            if quote and quote.get("price", 0) > 0:
                raw_data[code] = _merge_realtime_row(df, quote)
                merged_count += 1
        print(f"  实时行情融合: {merged_count}/{len(quotes)}只 (腾讯行情API)")
    else:
        print(f"  实时行情: 未获取到（非盘中时间或网络异常），使用历史数据")

    data_dict = {}
    for code, df in raw_data.items():
        if df is not None and len(df) >= 60:
            # 计算均线
            df["ma5"] = df["close"].rolling(5).mean()
            df["ma10"] = df["close"].rolling(10).mean()
            df["ma20"] = df["close"].rolling(20).mean()
            df["ma60"] = df["close"].rolling(60).mean()
            df["ma20_slope"] = df["ma20"].diff(3)
            data_dict[code] = df

    print(f"  有效数据: {len(data_dict)}只")
    if len(data_dict) < 5:
        print("  ❌ 数据不足，无法运行选股")
        return None

    # 运行选股引擎
    result = run_stock_screener(data_dict, holdings)
    buy_count = result.get('buy_recommend_count', 0)
    watch_count = result.get('watch_only_count', 0)
    min_buy = result.get('min_buy_score', 50)
    print(f"\n  ✅ 选股完成: {result['qualified_count']}只输出 / {result['total_candidates']}只候选")
    print(f"     ★推荐买入: {buy_count}只 | 仅观察: {watch_count}只 | 买入线: {min_buy}分")
    if result["stock_pool"]:
        for i, s in enumerate(result["stock_pool"], 1):
            tag = "★推荐" if s.get('is_buy_recommend') else "观察"
            if s.get('is_buy_recommend'):
                print(f"     {i:2d}. [{tag}] {s['code']} {s['name']} | 评分{s['factor_score']} | "
                      f"买点{s['moderate_buy']} | 止损{s['stop_loss']}(-{s['stop_loss_pct']}%)")
            else:
                print(f"     {i:2d}. [{tag}] {s['code']} {s['name']} | 评分{s['factor_score']} | "
                      f"{s.get('watch_reason', '')}")

    # V2.2: 选股后同步更新PoolManager观察池（将入选股加入观察池）
    try:
        pool_result = pm.update_pool_weekly(data_dict, screener_result=result)
        if pool_result.get("promoted") or pool_result.get("demoted"):
            print(f"  股票池更新: 升级{len(pool_result['promoted'])}只, "
                  f"降级{len(pool_result['demoted'])}只, "
                  f"过期{len(pool_result['expired'])}只")
    except Exception as e:
        logger.warning(f"  PoolManager更新异常(不影响选股): {e}")

    # V2.4: 涨停复盘集成
    zt_report = None
    if getattr(config, 'ZT_MONITOR_ENABLED', True):
        try:
            from trading_system.strategy.zt_monitor import ZTMonitor
            zt_monitor = ZTMonitor()
            zt_report = zt_monitor.get_daily_report()
            if zt_report and zt_report.get("ladder", {}).get("total_zt", 0) > 0:
                ladder = zt_report["ladder"]
                print(f"\n  📈 涨停复盘: 涨停{ladder['total_zt']}只 / 炸板{ladder['total_zb']}只 | "
                      f"封板率{ladder['zt_rate']:.0%} | 最高{ladder['max_consecutive']}板")
                # 板块热度Top3
                heat_top3 = zt_report.get("sector_heat", [])[:3]
                if heat_top3:
                    heat_str = ", ".join(f"{h['sector_name']}({h['zt_count']}只)" for h in heat_top3)
                    print(f"     热门板块: {heat_str}")
                # 连板龙头
                if ladder.get("top_stocks"):
                    top_str = ", ".join(f"{s['name']}({s['consecutive_days']}板)" for s in ladder["top_stocks"][:3])
                    print(f"     连板龙头: {top_str}")
                # 与候选池交集
                zt_codes = set(s["code"] for s in zt_monitor.get_zt_pool())
                pool_codes = set(s["code"] for s in result.get("stock_pool", []))
                overlap = zt_codes & pool_codes
                if overlap:
                    print(f"     ★涨停股已在观察池: {', '.join(overlap)}")
                result["zt_report"] = zt_report
            else:
                print(f"  涨停复盘: 当日无涨停数据（非交易日或数据未更新）")
        except Exception as e:
            logger.warning(f"  涨停复盘异常(不影响选股): {e}")

    # V2.5: 涨停基因跟踪集成
    zt_gene_result = None
    if getattr(config, 'ZT_GENE_ENABLED', True):
        try:
            from trading_system.strategy.zt_gene_tracker import ZTGeneTracker
            gene_tracker = ZTGeneTracker()
            zt_gene_result = gene_tracker.track_lianban_candidates()
            if zt_gene_result and zt_gene_result.get("success"):
                candidates = zt_gene_result.get("candidates", [])
                continued = zt_gene_result.get("continued_zt", [])
                if candidates or continued:
                    print(f"\n  [涨停基因] 昨日涨停{zt_gene_result['prev_zt_count']}只 | "
                          f"连板候选{len(candidates)}只 | 已连板{len(continued)}只")
                    if continued:
                        cont_str = ", ".join(f"{s['name']}({s['consecutive_days']}板)" for s in continued[:3])
                        print(f"     已连板: {cont_str}")
                    if candidates:
                        cand_str = ", ".join(f"{s['name']}(高开{s.get('open_pct', 0):.1f}%)" for s in candidates[:3])
                        print(f"     连板候选: {cand_str}")
                    result["zt_gene"] = zt_gene_result
            else:
                print(f"  涨停基因: 无有效跟踪数据")
        except Exception as e:
            logger.warning(f"  涨停基因异常(不影响选股): {e}")

    # V3.2: 短线动量筛选通道（与CANSLIM并行，增量输出）
    try:
        from trading_system.strategy.stock_screener import run_momentum_screener
        # 复用涨停池数据（如已获取）
        _zt_pool_for_momentum = None
        if zt_report and zt_report.get("ladder", {}).get("stocks"):
            _zt_pool_for_momentum = zt_report["ladder"]["stocks"]
        momentum_result = run_momentum_screener(
            market_df=None,  # 自动获取全市场实时行情
            zt_pool=_zt_pool_for_momentum,
            # FIX: holdings已是{code: info}字典，无需再转换（原误当列表迭代导致string indices异常）
            holdings=holdings if holdings else None,
        )
        if momentum_result.get("success") and momentum_result.get("picks"):
            result["momentum_picks"] = momentum_result["picks"]
            print(f"\n  ⚡ 短线动量筛选: {momentum_result['summary']}")
            for i, p in enumerate(momentum_result["picks"][:5], 1):
                yizi_tag = " [一字板-不可买]" if p.get("is_yizi") else ""
                print(f"     {i}. {p['code']} {p['name']} | 涨{p['change_pct']:+.1f}% | "
                      f"量比{p['vol_ratio']:.1f} | 动量{p['momentum_score']}分 | "
                      f"{p['sector']}{yizi_tag}")
        else:
            print(f"  短线动量: {momentum_result.get('summary', '无结果')}")
    except Exception as e:
        logger.warning(f"  短线动量筛选异常(不影响CANSLIM): {e}")

    # 发送选股报告邮件
    success = send_screener_email(result)
    if success:
        print(f"  📧 选股报告邮件已发送")
    else:
        print(f"  ⚠️ 选股报告邮件发送失败")
    return result


def run_evening():
    """盘后深度复盘（15:30）- 拆分2封邮件：复盘+条件单"""
    print("\n" + "=" * 50)
    print("  生成盘后深度复盘...")
    holdings = load_holdings()
    results = run_full_analysis(holdings)
    if results:
        # V2.0: 先计算一次，文本报告和HTML邮件共用
        sector_result = _get_sector_result(results, holdings)
        scored = _get_factor_scores(results)
        plan = _get_position_plan(results, holdings)

        # 文本报告（本地存档）
        report = generate_evening_report(results, holdings,
                                         sector_result=sector_result,
                                         scored=scored, plan=plan)
        print(report)
        _save_report("evening", report)
        charts = {
            "fund_flow": generate_fund_flow_chart(results),
            "position_pie": generate_position_pie(holdings, results),
            "sector_bar": generate_sector_bar(sector_result) if sector_result else "",
        }

        today = datetime.date.today().strftime("%Y-%m-%d")

        # === 新增数据收集 ===
        lhb_data = {}
        if HAS_LHB:
            try:
                analyzer = LHBAnalyzer()
                for stock in holdings:
                    lhb_data[stock] = analyzer.analyze(stock, days=10)
                logger.info(f"龙虎榜数据: {len(lhb_data)}只股票")
            except Exception as e:
                logger.warning(f"龙虎榜数据获取失败: {e}")

        margin_data = {}
        if HAS_MARGIN:
            try:
                monitor = MarginMonitor()
                for stock in holdings:
                    margin_data[stock] = monitor.calc_margin_signal(stock, days=10)
                logger.info(f"融资融券数据: {len(margin_data)}只股票")
            except Exception as e:
                logger.warning(f"融资融券数据获取失败: {e}")

        zt_data = None
        if HAS_ZT:
            try:
                zt_monitor = ZTMonitor()
                zt_data = zt_monitor.get_daily_report()
                logger.info(f"涨停数据: {zt_data.get('summary', '')}")
            except Exception as e:
                logger.warning(f"涨停数据获取失败: {e}")

        # flow_data — 从 CapitalFlowAnalyzer 获取
        flow_data = {}
        try:
            cf = CapitalFlowAnalyzer()
            for stock in holdings:
                flow_data[stock] = cf.analyze_multi_level_flow(stock, days=5)
            logger.info(f"资金流数据: {len(flow_data)}只股票")
        except Exception as e:
            logger.warning(f"资金流数据获取失败: {e}")

        heatmap_html = None
        try:
            sector = SectorMonitor()
            heatmap_html = sector.render_treemap_html()
        except Exception as e:
            logger.warning(f"板块热力图获取失败: {e}")

        release_data = None
        if HAS_CALENDAR:
            try:
                calendar = EventCalendar()
                release_data = calendar.get_release_summary(days_ahead=30)
                logger.info(f"解禁数据: {release_data.get('total_stocks', 0)}只即将解禁")
            except Exception as e:
                logger.warning(f"解禁数据获取失败: {e}")

        holder_data = {}
        if HAS_HOLDER:
            try:
                holder = HolderMonitor()
                for stock in holdings:
                    holder_data[stock] = holder.analyze(stock)
                logger.info(f"股东户数数据: {len(holder_data)}只股票")
            except Exception as e:
                logger.warning(f"股东户数数据获取失败: {e}")

        # 第1封：盘后深度复盘（含图表）
        html1 = build_evening_email(
            results, holdings, sector_result, scored, plan, charts,
            flow_data=flow_data if flow_data else None,
            lhb_data=lhb_data if lhb_data else None,
            margin_data=margin_data if margin_data else None,
            zt_data=zt_data,
            heatmap_html=heatmap_html,
            release_data=release_data,
            holder_data=holder_data if holder_data else None
        )
        _send_html_email(f"[操盘密码] 📊盘后深度复盘 | {today}", html1)

        # V2.0: 条件单已统一由 scheduler.py 19:00 发送，本处不再重复发送
        # html2 = build_orders_email(results, holdings, plan, release_data=release_data)
        # _send_html_email(f"[操盘密码] 📋条件单操作计划 | {today}", html2)

        # 预警检查
        alert_engine = AlertEngine(holdings=holdings)
        triggered = alert_engine.check_alerts(results)
        if triggered:
            print(f"\n  🔔 预警触发 {len(triggered)} 条")
            critical = [a for a in triggered if a.get("level") in ("critical", "high")]
            if critical:
                alert_text = "\n".join([f"  {a.get('icon','')} {a.get('name','')}: {a.get('message','')}" for a in critical])
                _send_html_email(
                    f"[操盘密码] ⚠️紧急预警({len(critical)}条) | {today}",
                    _build_alert_html(critical)
                )
    return results


def run_weekly():
    """周策略报告（周六 10:00）- 1封邮件"""
    print("\n" + "=" * 50)
    print("  生成周策略报告...")
    holdings = load_holdings()
    results = run_full_analysis(holdings)
    if results:
        report = generate_weekly_report(results, holdings)
        print(report)
        _save_report("weekly", report)

        sector_result = _get_sector_result(results, holdings)
        plan = _get_position_plan(results, holdings)
        charts = {"position_pie": generate_position_pie(holdings, results)}

        # 龙虎榜周数据（30天）
        lhb_data = {}
        if HAS_LHB:
            try:
                analyzer = LHBAnalyzer()
                for stock in holdings:
                    lhb_data[stock] = analyzer.analyze(stock, days=30)
                logger.info(f"龙虎榜周数据: {len(lhb_data)}只股票")
            except Exception as e:
                logger.warning(f"龙虎榜周数据获取失败: {e}")

        # 融资融券周数据（30天）
        margin_data = {}
        if HAS_MARGIN:
            try:
                monitor = MarginMonitor()
                for stock in holdings:
                    margin_data[stock] = monitor.calc_margin_signal(stock, days=30)
                logger.info(f"融资融券周数据: {len(margin_data)}只股票")
            except Exception as e:
                logger.warning(f"融资融券周数据获取失败: {e}")

        today = datetime.date.today().strftime("%Y-%m-%d")
        html = build_weekly_email(
            results, holdings, sector_result, plan, charts,
            lhb_data=lhb_data if lhb_data else None,
            margin_data=margin_data if margin_data else None
        )
        _send_html_email(f"[操盘密码] 📅周策略报告 | {today}", html)
    return results


def _save_report(report_type: str, content: str):
    """保存报告到文件"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    date_str = datetime.date.today().strftime("%Y%m%d")
    path = os.path.join(OUTPUT_DIR, f"{report_type}_{date_str}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"报告已保存: {path}")


# ============================================================
# 邮件发送（V2.0 美观HTML + 拆分多封）
# ============================================================

def _send_html_email(subject: str, html: str):
    """发送HTML邮件"""
    success = send_email(subject, html)
    if success:
        logger.info(f"📧 邮件已发送: {subject}")
    else:
        logger.warning(f"📧 邮件发送失败: {subject}")
    return success


def _get_sector_result(results: list, holdings: dict):
    """获取板块轮动数据"""
    try:
        holdings_data = {r.get("code", ""): r.get("df_analyzed") for r in results if r.get("df_analyzed") is not None}
        monitor = SectorMonitor()
        return monitor.analyze(holdings_data, holdings)
    except Exception:
        return None


def _get_factor_scores(results: list):
    """获取多因子评分"""
    try:
        scorer = MultiFactorScorer()
        return scorer.score_all(results)
    except Exception:
        return None


def _get_position_plan(results: list, holdings: dict):
    """获取仓位管理计划"""
    try:
        sizer = PositionSizer(total_capital=config.TOTAL_CAPITAL)
        return sizer.calc_positions(results, holdings)
    except Exception:
        return None


def _build_alert_html(alerts: list) -> str:
    """构建紧急预警HTML邮件（V2.0: 带紧急度评分+规则名称+触发条件，按紧急度降序）"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M")
    # 按紧急度降序排列
    alerts_sorted = sorted(alerts, key=lambda a: a.get("urgency_score", 0), reverse=True)
    items = ""
    for a in alerts_sorted:
        score = a.get('urgency_score', 0)
        score_color = "#cf1322" if score >= 80 else "#d46b08" if score >= 60 else "#faad14"
        items += f"""
        <div style="border:1px solid #ffccc7;border-left:4px solid #ff4d4f;border-radius:8px;padding:12px 16px;margin:10px 0;background:#fff1f0">
            <div style="display:flex;justify-content:space-between;align-items:center">
                <span style="font-weight:700;font-size:14px;color:#cf1322">{a.get('icon','🚨')} {a.get('name','')} ({a.get('code','')})</span>
                <span style="background:{score_color};color:white;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:bold">紧急度: {score}/100</span>
            </div>
            <div style="font-size:13px;color:#333;margin-top:6px">{a.get('msg','')}</div>
            <div style="margin-top:8px;padding:6px 10px;background:#fff7e6;border:1px solid #ffd591;border-radius:4px;font-size:12px">
                <b style="color:#d46b08">触发规则:</b> <span style="color:#333">{a.get('rule_name','')}</span><br>
                <b style="color:#d46b08">触发条件:</b> <span style="color:#666">{a.get('rule_detail','')}</span>
            </div>
            <div style="font-size:11px;color:#999;margin-top:6px">{a.get('level','')} | {a.get('time', now)}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:15px;background:#f0f2f5;font-family:'Microsoft YaHei',Arial,sans-serif">
<div style="max-width:700px;margin:0 auto">
    <div style="background:linear-gradient(135deg,#cf1322,#ff4d4f);color:white;padding:18px 25px;border-radius:12px 12px 0 0">
        <h1 style="margin:0;font-size:20px">⚠️ 紧急预警 ({len(alerts)}条)</h1>
        <div style="font-size:12px;opacity:0.8;margin-top:5px">{today} {now} | 操盘密码V9.0 | 按紧急度排序</div>
    </div>
    <div style="background:white;padding:20px 25px;border-radius:0 0 12px 12px;box-shadow:0 4px 15px rgba(0,0,0,0.08)">
        {items}
        <div style="text-align:center;color:#999;font-size:11px;margin-top:15px;padding-top:10px;border-top:1px solid #eee">
            请立即检查持仓，必要时手动干预 | 紧急度: 90+=立即操作 / 70-89=尽快处理 / 50-69=密切关注
        </div>
    </div>
</div>
</body></html>"""


def start_scheduler():
    """DEPRECATED: 调度功能已统一由 trading_system/scheduler.py 负责
    
    本函数保留仅为兼容，不再注册任何定时任务。
    请使用:
      python trading_system/scheduler.py  # 启动主调度器
      python caopan_report.py --morning   # CLI手动生成报告
    """
    print("=" * 50)
    print("  ⚠️  本调度器已废弃 (V2.0)")
    print("  定时任务已统一由 trading_system/scheduler.py 负责")
    print("  如需手动生成报告，请使用:")
    print("    python caopan_report.py --morning")
    print("    python caopan_report.py --evening")
    print("    python caopan_report.py --weekly")
    print("    python caopan_report.py --screener")
    print("=" * 50)
    print("\n正在启动主调度器 scheduler.py ...")
    # 转发到主调度器
    import subprocess
    scheduler_path = os.path.join(TRADING_SYSTEM_DIR, "scheduler.py")
    subprocess.run([sys.executable, scheduler_path], cwd=TRADING_SYSTEM_DIR)


def install_tasks():
    """安装Windows定时任务"""
    import subprocess
    python_exe = sys.executable
    script = os.path.abspath(__file__)

    tasks = [
        ("CaopanReport_Morning", "08:30", "--morning", "盘前作战计划"),
        ("CaopanReport_Evening", "15:30", "--evening", "盘后深度复盘"),
    ]

    print("安装操盘密码报告定时任务:")
    for name, time_str, arg, desc in tasks:
        cmd = f'schtasks /create /tn "{name}" /tr "\\"{python_exe}\\" \\"{script}\\" {arg}" /sc daily /st {time_str} /f'
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            status = "OK" if r.returncode == 0 else "FAIL"
            print(f"  [{status}] {desc} | {time_str}")
        except Exception as e:
            print(f"  [ERROR] {desc}: {e}")

    # 周报（周六）
    cmd = f'schtasks /create /tn "CaopanReport_Weekly" /tr "\\"{python_exe}\\" \\"{script}\\" --weekly" /sc weekly /d SAT /st 10:00 /f'
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        status = "OK" if r.returncode == 0 else "FAIL"
        print(f"  [{status}] 周策略报告 | 周六 10:00")
    except Exception as e:
        print(f"  [ERROR] 周报: {e}")


# ============================================================
# 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="操盘密码报告调度器")
    parser.add_argument("--morning", action="store_true", help="生成盘前作战计划")
    parser.add_argument("--evening", action="store_true", help="生成盘后深度复盘")
    parser.add_argument("--weekly", action="store_true", help="生成周策略报告")
    parser.add_argument("--screener", action="store_true", help="运行选股引擎并发送报告")
    parser.add_argument("--alert", action="store_true", help="启动盘中实时预警循环(每3分钟)")
    parser.add_argument("--install", action="store_true", help="安装Windows定时任务")
    args = parser.parse_args()

    if args.install:
        install_tasks()
    elif args.alert:
        from notify.alert_engine import run_alert_loop
        holdings = load_holdings()
        run_alert_loop(holdings=holdings)
    elif args.morning:
        run_morning()
    elif args.evening:
        run_evening()
    elif args.screener:
        run_screener()
    elif args.weekly:
        run_weekly()
    else:
        start_scheduler()

    # 统一登出baostock会话
    _bs_logout()


if __name__ == "__main__":
    main()
