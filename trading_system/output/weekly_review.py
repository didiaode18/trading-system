"""
周度回顾报告 V2.0 - 中线波段版
=================================
每周五16:00发送，回答核心问题：这周赚了多少？策略有效吗？

7大板块:
1. 本周绩效 - 收益率/回撤/胜率
2. 中线波段指标 - 持仓天数/波段完成率/时间止损
3. 盈亏归因 - Barra风格因子分解
4. 信号质量追踪 - 准确率/滑点
5. 压力测试 - 蒙特卡洛模拟
6. 组合健康度 - 风格暴露/Deflated Sharpe
7. 下周操作建议 - 仓位/关注/风险事件

使用方式:
    from output.weekly_review import send_weekly_review
    send_weekly_review(weekly_data)
"""

import os
import sys
import datetime
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ============================================================
# 一、HTML样式
# ============================================================

_CSS = """
body { font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 10px; background: #f0f2f5; }
.container { max-width: 880px; margin: 0 auto; background: #fff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.08); overflow: hidden; }
.header { background: linear-gradient(135deg, #1b5e20, #2e7d32); color: #fff; padding: 20px 24px; }
.header h1 { margin: 0; font-size: 20px; font-weight: 600; }
.header .meta { font-size: 12px; opacity: 0.85; margin-top: 6px; }
.section { padding: 16px 24px; border-bottom: 1px solid #f0f0f0; }
.section:last-child { border-bottom: none; }
.section-title { font-size: 15px; font-weight: 600; color: #1b5e20; margin: 0 0 12px 0; padding-left: 10px; border-left: 3px solid #4caf50; }
.metric-grid { display: flex; flex-wrap: wrap; gap: 10px; margin: 10px 0; }
.metric-card { flex: 1; min-width: 110px; background: #e8f5e9; border-radius: 8px; padding: 12px; text-align: center; }
.metric-card .label { font-size: 11px; color: #666; margin-bottom: 4px; }
.metric-card .value { font-size: 16px; font-weight: 700; color: #333; }
.text-red { color: #d32f2f !important; }
.text-green { color: #2e7d32 !important; }
.text-orange { color: #e65100 !important; }
table { width: 100%; border-collapse: collapse; font-size: 12px; margin: 8px 0; }
th { background: #e8f5e9; color: #1b5e20; padding: 8px 6px; text-align: center; font-weight: 600; }
td { padding: 7px 6px; border-bottom: 1px solid #f5f5f5; text-align: center; }
.bar-container { background: #e0e0e0; border-radius: 4px; height: 16px; position: relative; margin: 2px 0; }
.bar-fill { height: 100%; border-radius: 4px; }
.bar-positive { background: #ef5350; }
.bar-negative { background: #66bb6a; }
.alert-item { padding: 8px 12px; margin: 4px 0; border-radius: 6px; font-size: 12px; background: #f5f5f5; border-left: 3px solid #4caf50; }
.footer { padding: 12px 24px; background: #fafafa; font-size: 11px; color: #999; text-align: center; }
"""


# ============================================================
# 二、板块生成
# ============================================================

def _section_performance(data: dict) -> str:
    """板块1: 本周绩效"""
    perf = data.get("performance", {})
    if not perf:
        return ""

    html = '<div class="section"><h2 class="section-title">一、本周绩效</h2>'
    html += '<div class="metric-grid">'

    # 周收益率
    weekly_return = perf.get("weekly_return", 0)
    cls = "text-red" if weekly_return > 0 else "text-green" if weekly_return < 0 else ""
    html += f'<div class="metric-card"><div class="label">周收益率</div><div class="value {cls}">{weekly_return:+.2f}%</div></div>'

    # 基准收益
    benchmark = perf.get("benchmark_return", 0)
    html += f'<div class="metric-card"><div class="label">基准(沪深300)</div><div class="value">{benchmark:+.2f}%</div></div>'

    # 超额收益
    excess = weekly_return - benchmark
    cls = "text-red" if excess > 0 else "text-green"
    html += f'<div class="metric-card"><div class="label">超额收益</div><div class="value {cls}">{excess:+.2f}%</div></div>'

    # 最大回撤
    max_dd = perf.get("max_drawdown", 0)
    html += f'<div class="metric-card"><div class="label">最大回撤</div><div class="value text-green">-{max_dd:.2f}%</div></div>'

    # 胜率
    win_rate = perf.get("win_rate", 0)
    html += f'<div class="metric-card"><div class="label">胜率</div><div class="value">{win_rate:.0f}%</div></div>'

    # 交易笔数
    trades = perf.get("trade_count", 0)
    html += f'<div class="metric-card"><div class="label">交易笔数</div><div class="value">{trades}</div></div>'

    html += '</div></div>'
    return html


def _section_wave_metrics(data: dict) -> str:
    """板块2: 中线波段指标"""
    wave = data.get("wave_metrics", {})
    if not wave:
        return ""

    html = '<div class="section"><h2 class="section-title">二、中线波段指标</h2>'
    html += '<div class="metric-grid">'

    avg_days = wave.get("avg_holding_days", 0)
    html += f'<div class="metric-card"><div class="label">平均持仓天数</div><div class="value">{avg_days:.0f}天</div></div>'

    completion = wave.get("wave_completion_rate", 0)
    html += f'<div class="metric-card"><div class="label">波段完成率</div><div class="value">{completion:.0f}%</div></div>'

    time_stops = wave.get("time_stop_count", 0)
    html += f'<div class="metric-card"><div class="label">时间止损触发</div><div class="value">{time_stops}次</div></div>'

    overdue = wave.get("overdue_count", 0)
    html += f'<div class="metric-card"><div class="label">持仓>30天未达标</div><div class="value text-orange">{overdue}只</div></div>'

    html += '</div>'

    # 超期持仓列表
    overdue_list = wave.get("overdue_stocks", [])
    if overdue_list:
        html += '<p style="font-size:12px;margin-top:8px"><b>持仓超30天未达目标:</b></p>'
        for item in overdue_list[:5]:
            html += f'<div class="alert-item" style="border-left-color:#ff9800">{item}</div>'

    html += '</div>'
    return html


def _section_attribution(data: dict) -> str:
    """板块3: 盈亏归因（Barra风格因子）"""
    attribution = data.get("attribution", {})
    if not attribution:
        return ""

    html = '<div class="section"><h2 class="section-title">三、盈亏归因（Barra风格因子）</h2>'

    factors = attribution.get("factors", {})
    if factors:
        html += '<table><tr><th>因子</th><th>贡献(元)</th><th>占比</th><th>方向</th></tr>'
        total_pnl = attribution.get("total_pnl", 1)
        for factor_name, contribution in sorted(factors.items(), key=lambda x: abs(x[1]), reverse=True):
            pct = contribution / abs(total_pnl) * 100 if total_pnl != 0 else 0
            direction = "正贡献" if contribution > 0 else "负贡献"
            cls = "text-red" if contribution > 0 else "text-green"
            html += f'<tr><td>{factor_name}</td><td class="{cls}">{contribution:+,.0f}</td>'
            html += f'<td>{pct:+.1f}%</td><td>{direction}</td></tr>'
        html += '</table>'

    # Alpha显著性
    alpha_p = attribution.get("alpha_pvalue")
    if alpha_p is not None:
        sig_text = "显著" if alpha_p < 0.05 else "不显著"
        html += f'<p style="font-size:12px">个股Alpha: p={alpha_p:.3f} ({sig_text})</p>'

    html += '</div>'
    return html


def _section_signal_quality(data: dict) -> str:
    """板块4: 信号质量追踪"""
    quality = data.get("signal_quality", {})
    if not quality:
        return ""

    html = '<div class="section"><h2 class="section-title">四、信号质量追踪</h2>'
    html += '<div class="metric-grid">'

    # 信号统计
    buy_signals = quality.get("buy_signals", 0)
    sell_signals = quality.get("sell_signals", 0)
    html += f'<div class="metric-card"><div class="label">本周信号</div><div class="value">买{buy_signals}/卖{sell_signals}</div></div>'

    # 准确率
    buy_accuracy = quality.get("buy_accuracy", 0)
    sell_accuracy = quality.get("sell_accuracy", 0)
    html += f'<div class="metric-card"><div class="label">买入准确率</div><div class="value">{buy_accuracy:.0f}%</div></div>'
    html += f'<div class="metric-card"><div class="label">卖出准确率</div><div class="value">{sell_accuracy:.0f}%</div></div>'

    # 滑点
    buy_slippage = quality.get("avg_buy_slippage", 0)
    sell_slippage = quality.get("avg_sell_slippage", 0)
    html += f'<div class="metric-card"><div class="label">平均滑点</div><div class="value">买{buy_slippage:+.2f}%/卖{sell_slippage:+.2f}%</div></div>'

    html += '</div>'

    # Meta-Label有效性
    meta_effectiveness = quality.get("meta_effectiveness", {})
    if meta_effectiveness:
        exec_return = meta_effectiveness.get("execute_return", 0)
        reject_return = meta_effectiveness.get("reject_return", 0)
        html += f'<p style="font-size:12px">Meta-Label有效性: 执行组收益 {exec_return:+.2f}% vs 拒绝组 {reject_return:+.2f}%</p>'

    html += '</div>'
    return html


def _section_stress_test(data: dict) -> str:
    """板块5: 压力测试（蒙特卡洛）"""
    stress = data.get("stress_test", {})
    if not stress:
        return ""

    html = '<div class="section"><h2 class="section-title">五、压力测试（蒙特卡洛1000次）</h2>'
    html += '<div class="metric-grid">'

    # 95%回撤
    dd_95 = stress.get("max_drawdown_p95", 0)
    html += f'<div class="metric-card"><div class="label">95%最大回撤</div><div class="value text-green">-{dd_95:.1f}%</div></div>'

    # 连续亏损
    max_consec_loss = stress.get("max_consecutive_loss", 0)
    html += f'<div class="metric-card"><div class="label">最长连续亏损</div><div class="value">{max_consec_loss}笔</div></div>'

    # 破产概率
    ruin_prob = stress.get("ruin_probability", 0)
    html += f'<div class="metric-card"><div class="label">破产概率</div><div class="value">{ruin_prob:.2f}%</div></div>'

    # 中位收益
    median_return = stress.get("median_return", 0)
    html += f'<div class="metric-card"><div class="label">中位收益</div><div class="value">{median_return:+.1f}%</div></div>'

    html += '</div></div>'
    return html


def _section_health(data: dict) -> str:
    """板块6: 组合健康度"""
    health = data.get("health", {})
    if not health:
        return ""

    html = '<div class="section"><h2 class="section-title">六、组合健康度</h2>'

    # Deflated Sharpe
    dsr = health.get("deflated_sharpe")
    if dsr is not None:
        dsr_text = "策略有效" if dsr > 0.95 else "可能过拟合" if dsr < 0.5 else "待观察"
        dsr_cls = "text-green" if dsr > 0.95 else "text-orange" if dsr < 0.5 else ""
        html += f'<p style="font-size:13px">Deflated Sharpe: <b class="{dsr_cls}">{dsr:.2f}</b> ({dsr_text})</p>'

    # 风格暴露
    style = health.get("style_exposure", "")
    if style:
        html += f'<p style="font-size:12px">风格暴露: {style}</p>'

    # 事件提醒
    events = health.get("upcoming_events", [])
    if events:
        html += '<p style="font-size:12px;margin-top:8px"><b>下周关注:</b></p>'
        for event in events[:4]:
            html += f'<div class="alert-item">{event}</div>'

    html += '</div>'
    return html


def _section_next_week(data: dict) -> str:
    """板块7: 下周操作建议"""
    advice = data.get("next_week_advice", {})
    if not advice:
        return ""

    html = '<div class="section"><h2 class="section-title">七、下周操作建议</h2>'

    # 仓位建议
    position_advice = advice.get("position", "")
    if position_advice:
        html += f'<p style="font-size:13px"><b>仓位:</b> {position_advice}</p>'

    # 重点关注
    focus = advice.get("focus_stocks", [])
    if focus:
        html += '<p style="font-size:12px"><b>重点关注:</b></p>'
        for f_item in focus[:3]:
            html += f'<div class="alert-item">{f_item}</div>'

    # 风险事件
    risks = advice.get("risk_events", [])
    if risks:
        html += '<p style="font-size:12px;margin-top:6px"><b>风险事件:</b></p>'
        for r in risks[:3]:
            html += f'<div class="alert-item" style="border-left-color:#ff9800">{r}</div>'

    html += '</div>'
    return html


# ============================================================
# 三、主构建 + 发送
# ============================================================

def build_weekly_review_html(weekly_data: dict) -> str:
    """
    构建周度回顾HTML

    参数:
        weekly_data: {
            "performance": {...},      # 本周绩效
            "attribution": {...},      # 盈亏归因
            "signal_quality": {...},   # 信号质量
            "stress_test": {...},      # 压力测试
            "health": {...},           # 组合健康度
            "next_week_advice": {...}, # 下周建议
        }
    """
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M")

    # 计算本周范围
    week_start = datetime.date.today() - datetime.timedelta(days=4)  # 周一
    week_range = f"{week_start.strftime('%m/%d')} - {today[5:]}"

    perf = weekly_data.get("performance", {})
    weekly_return = perf.get("weekly_return", 0)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_CSS}</style></head>
<body><div class="container">
<div class="header">
    <h1>周度回顾 | {week_range}</h1>
    <div class="meta">生成时间: {today} {now} | 周收益: {weekly_return:+.2f}%</div>
</div>
"""

    html += _section_performance(weekly_data)
    html += _section_wave_metrics(weekly_data)
    html += _section_attribution(weekly_data)
    html += _section_signal_quality(weekly_data)
    html += _section_stress_test(weekly_data)
    html += _section_health(weekly_data)
    html += _section_next_week(weekly_data)

    html += f"""
<div class="footer">
    本报告由交易系统V9.0自动生成 | 仅供参考，不构成投资建议<br>
    股市有风险，投资需谨慎 | 总资金: {config.TOTAL_CAPITAL:,.0f}元
</div>
</div></body></html>"""

    return html


def send_weekly_review(weekly_data: dict) -> bool:
    """构建并发送周度回顾"""
    from notify.email_notify import send_email

    today = datetime.date.today().strftime("%Y-%m-%d")
    perf = weekly_data.get("performance", {})
    weekly_return = perf.get("weekly_return", 0)

    subject = f"[周度回顾] {today} | 周收益{weekly_return:+.2f}%"

    html = build_weekly_review_html(weekly_data)
    return send_email(subject, html)


def prepare_weekly_data(holdings: dict, data_dict: dict) -> dict:
    """
    准备周度回顾数据（供scheduler调用）
    自动调用V9.0模块收集数据
    """
    weekly_data = {}

    # 1. 本周绩效（从交易日志获取）
    try:
        from strategy.trade_journal import TradeJournal
        journal = TradeJournal()
        perf = journal.performance_report(days=5)
        weekly_data["performance"] = {
            "weekly_return": perf.get("total_return_pct", 0),
            "benchmark_return": 0,  # TODO: 从指数数据计算
            "max_drawdown": perf.get("max_drawdown_pct", 0),
            "win_rate": perf.get("win_rate_pct", 0),
            "trade_count": perf.get("total_trades", 0),
        }
    except Exception as e:
        logger.debug(f"  绩效数据获取失败: {e}")
        weekly_data["performance"] = {"weekly_return": 0, "trade_count": 0}

    # 2. 盈亏归因（Barra）
    try:
        from attribution.barra import BarraAttribution
        barra = BarraAttribution()
        # 需要收益率序列，暂用简化版
        weekly_data["attribution"] = {}
    except Exception:
        weekly_data["attribution"] = {}

    # 3. 信号质量
    try:
        from execution.slippage_tracker import SlippageTracker
        tracker = SlippageTracker()
        stats = tracker.get_statistics()
        weekly_data["signal_quality"] = {
            "buy_signals": stats.get("total_buy_signals", 0),
            "sell_signals": stats.get("total_sell_signals", 0),
            "buy_accuracy": stats.get("buy_accuracy", 0),
            "sell_accuracy": stats.get("sell_accuracy", 0),
            "avg_buy_slippage": stats.get("avg_buy_slippage", 0),
            "avg_sell_slippage": stats.get("avg_sell_slippage", 0),
        }
    except Exception:
        weekly_data["signal_quality"] = {}

    # 4. 压力测试（蒙特卡洛）
    try:
        from backtest.monte_carlo import MonteCarloStressTest
        mc = MonteCarloStressTest()
        # 需要交易记录，暂用简化版
        weekly_data["stress_test"] = {}
    except Exception:
        weekly_data["stress_test"] = {}

    # 5. 组合健康度
    try:
        from backtest.deflated_sharpe import DeflatedSharpe
        ds = DeflatedSharpe()
        weekly_data["health"] = {
            "deflated_sharpe": None,  # 需要完整回测数据
            "style_exposure": "大盘成长偏重",
            "upcoming_events": [],
        }
    except Exception:
        weekly_data["health"] = {}

    # 6. 下周建议
    weekly_data["next_week_advice"] = {
        "position": "维持当前仓位，关注止损位",
        "focus_stocks": [],
        "risk_events": [],
    }

    # 7. 中线波段指标
    try:
        holding_days_list = []
        overdue_stocks = []
        wave_completed = 0
        total_positions = len(holdings)

        for code, pos in holdings.items():
            buy_date_str = pos.get("buy_date", "")
            buy_price = pos.get("buy_price", 0)
            try:
                buy_date = datetime.datetime.strptime(buy_date_str, "%Y-%m-%d").date()
                days = max(1, int((datetime.date.today() - buy_date).days / 1.4))
            except Exception:
                days = 0
            holding_days_list.append(days)

            # 检查是否达到目标价(+10%)
            df = data_dict.get(code)
            if df is not None and not df.empty:
                close = df.iloc[-1]["close"]
                target1 = buy_price * 1.10
                if close >= target1:
                    wave_completed += 1

                # 持仓超30天未达标
                pnl_pct = (close - buy_price) / buy_price * 100 if buy_price > 0 else 0
                if days > 30 and pnl_pct < 10:
                    name = config.get_stock_name(code)
                    overdue_stocks.append(f"{code} {name}: 持仓{days}天，浮盈{pnl_pct:.1f}%")

        avg_days = sum(holding_days_list) / len(holding_days_list) if holding_days_list else 0
        completion_rate = (wave_completed / total_positions * 100) if total_positions > 0 else 0

        weekly_data["wave_metrics"] = {
            "avg_holding_days": avg_days,
            "wave_completion_rate": completion_rate,
            "time_stop_count": 0,  # TODO: 从交易日志统计
            "overdue_count": len(overdue_stocks),
            "overdue_stocks": overdue_stocks,
        }
    except Exception as e:
        logger.debug(f"  波段指标计算失败: {e}")
        weekly_data["wave_metrics"] = {}

    # 事件日历检查
    try:
        from strategy.event_calendar import EventCalendar
        event_cal = EventCalendar()
        for code in list(holdings.keys())[:5]:
            risk = event_cal.check_stock(code, days_ahead=7)
            if risk.get("events"):
                name = config.get_stock_name(code)
                for ev in risk["events"][:1]:
                    weekly_data["next_week_advice"]["risk_events"].append(
                        f"{code} {name}: {ev.get('type', '')} ({ev.get('date', '')})"
                    )
    except Exception:
        pass

    return weekly_data
