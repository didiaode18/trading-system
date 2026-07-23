# -*- coding: utf-8 -*-
"""
操盘密码报告调度器 V1.0
========================
整合P0-P5全部模块，按「4+1」体系定时输出分析报告

报告体系:
  1. [08:30] 盘前作战计划 - 板块方向+操作清单+关键价位+仓位建议
  2. [15:30] 盘后深度复盘 - 九大板块全量分析(趋势/DK/资金/筹码/板块/多因子/仓位/预警/持仓)
  3. [19:00] 条件单 - 东方财富智能条件单+止损止盈价位
  4. [周六 10:00] 周策略报告 - 本周绩效+板块轮动+仓位再平衡+下周计划
  5. [盘中实时] 紧急预警 - 仅critical/high级别(止损/DK强信号/跌停)

运行:
  python caopan_report.py                # 启动调度器
  python caopan_report.py --morning      # 立即生成盘前报告
  python caopan_report.py --evening      # 立即生成盘后报告
  python caopan_report.py --weekly       # 立即生成周报
  python caopan_report.py --install      # 安装Windows定时任务
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(TRADING_SYSTEM_DIR, "output", "reports")


# ============================================================
# 数据获取
# ============================================================

def fetch_stock_data(code: str, days: int = 500):
    """获取K线数据"""
    try:
        import baostock as bs
        lg = bs.login()
        prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"
        bs_code = f"{prefix}.{code}"
        end = datetime.date.today().strftime("%Y-%m-%d")
        start = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="d", adjustflag="2"
        )
        import pandas as pd
        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.tail(days).reset_index(drop=True)
    except Exception as e:
        logger.error(f"获取{code}数据失败: {e}")
        return None


def load_holdings() -> dict:
    """加载持仓"""
    NAME_MAP = {
        "588000": "科创50", "603501": "豪威集团", "002558": "巨人网络",
        "159205": "创业东财", "002185": "华天科技",
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
    """运行完整分析"""
    engine = CaopanEngine()
    results = []
    for code, info in holdings.items():
        name = info.get("name", code)
        df = fetch_stock_data(code, days=500)
        if df is None or len(df) < 60:
            continue
        result = engine.analyze(df, code=code, name=name)
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
        sizer = PositionSizer(total_capital=750000)
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

def generate_evening_report(results: list, holdings: dict) -> str:
    """
    盘后深度复盘 - 九大板块全量分析
    直接调用caopan_runner的完整流程
    """
    now = datetime.datetime.now()
    lines = []
    lines.append(f"{'═' * 60}")
    lines.append(f"  📊 操盘密码 V3.0 盘后深度复盘")
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
        holdings_data = {r.get("code", ""): r.get("df_analyzed") for r in results if r.get("df_analyzed") is not None}
        monitor = SectorMonitor()
        sector_result = monitor.analyze(holdings_data, holdings)
        lines.append(sector_summary(sector_result))
    except Exception as e:
        lines.append(f"     异常: {e}")

    # 六、多因子评分
    lines.append(f"\n  ━━ 六、多因子评分 ━━")
    try:
        scorer = MultiFactorScorer()
        scored = scorer.score_all(results)
        lines.append(factor_summary(scored))
    except Exception as e:
        lines.append(f"     异常: {e}")

    # 七、仓位管理
    lines.append(f"\n  ━━ 七、仓位管理 ━━")
    try:
        sizer = PositionSizer(total_capital=750000)
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
        sizer = PositionSizer(total_capital=750000)
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

        # 发送美观HTML邮件
        today = datetime.date.today().strftime("%Y-%m-%d")
        html = build_morning_email(results, holdings, sector_result, plan, charts)
        _send_html_email(f"[操盘密码] 📋盘前作战计划 | {today}", html)
    return results


def run_evening():
    """盘后深度复盘（15:30）- 拆分2封邮件：复盘+条件单"""
    print("\n" + "=" * 50)
    print("  生成盘后深度复盘...")
    holdings = load_holdings()
    results = run_full_analysis(holdings)
    if results:
        # 文本报告（本地存档）
        report = generate_evening_report(results, holdings)
        print(report)
        _save_report("evening", report)

        # 准备数据
        sector_result = _get_sector_result(results, holdings)
        scored = _get_factor_scores(results)
        plan = _get_position_plan(results, holdings)
        charts = {
            "fund_flow": generate_fund_flow_chart(results),
            "position_pie": generate_position_pie(holdings, results),
            "sector_bar": generate_sector_bar(sector_result) if sector_result else "",
        }

        today = datetime.date.today().strftime("%Y-%m-%d")

        # 第1封：盘后深度复盘（含图表）
        html1 = build_evening_email(results, holdings, sector_result, scored, plan, charts)
        _send_html_email(f"[操盘密码] 📊盘后深度复盘 | {today}", html1)

        # 第2封：条件单操作计划（持仓+条件单+风控+纪律锁）
        html2 = build_orders_email(results, holdings, plan)
        _send_html_email(f"[操盘密码] 📋条件单操作计划 | {today}", html2)

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

        today = datetime.date.today().strftime("%Y-%m-%d")
        html = build_weekly_email(results, holdings, sector_result, plan, charts)
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
        sizer = PositionSizer(total_capital=750000)
        return sizer.calc_positions(results, holdings)
    except Exception:
        return None


def _build_alert_html(alerts: list) -> str:
    """构建紧急预警HTML邮件"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M")
    items = ""
    for a in alerts:
        items += f"""
        <div style="border:1px solid #ffccc7;border-left:4px solid #ff4d4f;border-radius:8px;padding:12px 16px;margin:10px 0;background:#fff1f0">
            <div style="font-weight:700;font-size:14px;color:#cf1322">{a.get('icon','🚨')} {a.get('name','')} ({a.get('code','')})</div>
            <div style="font-size:13px;color:#333;margin-top:6px">{a.get('message','')}</div>
            <div style="font-size:11px;color:#999;margin-top:4px">{a.get('level','')} | {now}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:15px;background:#f0f2f5;font-family:'Microsoft YaHei',Arial,sans-serif">
<div style="max-width:700px;margin:0 auto">
    <div style="background:linear-gradient(135deg,#cf1322,#ff4d4f);color:white;padding:18px 25px;border-radius:12px 12px 0 0">
        <h1 style="margin:0;font-size:20px">⚠️ 紧急预警 ({len(alerts)}条)</h1>
        <div style="font-size:12px;opacity:0.8;margin-top:5px">{today} {now} | 操盘密码V3.0</div>
    </div>
    <div style="background:white;padding:20px 25px;border-radius:0 0 12px 12px;box-shadow:0 4px 15px rgba(0,0,0,0.08)">
        {items}
        <div style="text-align:center;color:#999;font-size:11px;margin-top:15px;padding-top:10px;border-top:1px solid #eee">
            请立即检查持仓，必要时手动干预
        </div>
    </div>
</div>
</body></html>"""


def start_scheduler():
    """启动定时调度"""
    try:
        import schedule
        import time
    except ImportError:
        print("请安装schedule: pip install schedule")
        return

    print("=" * 50)
    print("  操盘密码报告调度器 V1.0")
    print(f"  启动: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)
    print("  08:30 盘前作战计划")
    print("  15:30 盘后深度复盘")
    print("  周六 10:00 周策略报告")
    print("  盘中: 智能预警(随盘后报告)")
    print("=" * 50)

    schedule.every().day.at("08:30").do(run_morning)
    schedule.every().day.at("15:30").do(run_evening)
    schedule.every().saturday.at("10:00").do(run_weekly)

    while True:
        schedule.run_pending()
        time.sleep(60)


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
    parser.add_argument("--install", action="store_true", help="安装Windows定时任务")
    args = parser.parse_args()

    if args.install:
        install_tasks()
    elif args.morning:
        run_morning()
    elif args.evening:
        run_evening()
    elif args.weekly:
        run_weekly()
    else:
        start_scheduler()


if __name__ == "__main__":
    main()
