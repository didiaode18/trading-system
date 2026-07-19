"""
邮件通知模块 V3.0
=================
通过QQ邮箱SMTP发送HTML格式的每日交易报告

V3.0新增:
- 综合日报: 大盘状态+持仓盈亏+信号质量+条件单+风控仪表盘
- 信号质量评分显示
- 双轨止盈条件单展示
- 时间红线标注

使用场景:
- 每日盘前/盘后自动发送交易信号摘要
- 风控熔断时紧急邮件通知
- 回测报告发送

配置方式:
  在 config.py 中设置:
  - EMAIL_SENDER: 发件人QQ邮箱
  - EMAIL_AUTH_CODE: QQ邮箱授权码（非QQ密码）
  - EMAIL_RECEIVER: 收件人邮箱
  - EMAIL_SMTP_HOST: smtp.qq.com
  - EMAIL_SMTP_PORT: 465
"""

import smtplib
import logging
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ============================================================
# 一、邮件发送核心
# ============================================================

def send_email(subject: str, html_content: str,
               receiver: str = None) -> bool:
    """
    发送HTML格式邮件
    
    参数:
        subject: 邮件主题
        html_content: HTML格式邮件正文
        receiver: 收件人（默认取config）
    
    返回: 是否发送成功
    """
    if not config.EMAIL_SENDER or not config.EMAIL_AUTH_CODE:
        logger.warning("邮箱未配置（EMAIL_SENDER或EMAIL_AUTH_CODE为空），跳过发送")
        logger.info(f"邮件内容预览: [{subject}] {html_content[:200]}...")
        return False

    if receiver is None:
        receiver = config.EMAIL_RECEIVER

    try:
        # 构建邮件
        msg = MIMEMultipart("alternative")
        msg["From"] = config.EMAIL_SENDER
        msg["To"] = receiver
        msg["Subject"] = subject

        # HTML正文
        html_part = MIMEText(html_content, "html", "utf-8")
        msg.attach(html_part)

        # 发送
        smtp = smtplib.SMTP_SSL(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT)
        smtp.login(config.EMAIL_SENDER, config.EMAIL_AUTH_CODE)
        smtp.sendmail(config.EMAIL_SENDER, [receiver], msg.as_string())
        smtp.quit()

        logger.info(f"邮件发送成功: {subject} -> {receiver}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"邮箱认证失败: {e}（请检查EMAIL_SENDER和EMAIL_AUTH_CODE是否正确）")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"SMTP错误: {e}")
        return False
    except Exception as e:
        logger.error(f"邮件发送异常: {e}")
        return False


# ============================================================
# 二、HTML报告模板
# ============================================================

def _build_html_report(title: str, sections: list,
                       footer: str = "") -> str:
    """
    构建标准HTML报告
    
    参数:
        title: 报告标题
        sections: 段落列表 [{"heading": str, "content": str, "type": "table"/"text"/"alert"}, ...]
        footer: 底部说明文字
    
    返回: HTML字符串
    """
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M:%S")

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
    .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    h1 {{ color: #1a1a2e; border-bottom: 3px solid #4472C4; padding-bottom: 10px; font-size: 22px; }}
    h2 {{ color: #333; font-size: 16px; margin-top: 20px; border-left: 4px solid #4472C4; padding-left: 10px; }}
    .meta {{ color: #888; font-size: 12px; margin-bottom: 20px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
    th {{ background: #4472C4; color: white; padding: 8px 12px; text-align: center; font-size: 13px; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #eee; text-align: center; font-size: 13px; }}
    tr:nth-child(even) {{ background: #f8f9fa; }}
    .buy {{ background: #E2EFDA !important; }}
    .sell {{ background: #FCE4D6 !important; }}
    .alert {{ background: #FFF3CD; border: 1px solid #FFC107; padding: 12px; border-radius: 4px; margin: 10px 0; }}
    .alert-danger {{ background: #F8D7DA; border-color: #F5C6CB; }}
    .alert-success {{ background: #D4EDDA; border-color: #C3E6CB; }}
    .text-red {{ color: #DC3545; font-weight: bold; }}
    .text-green {{ color: #28A745; font-weight: bold; }}
    .text-orange {{ color: #FD7E14; font-weight: bold; }}
    .footer {{ color: #999; font-size: 11px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 10px; }}
    .stat-box {{ display: inline-block; background: #f0f4ff; padding: 10px 20px; margin: 5px; border-radius: 6px; text-align: center; }}
    .stat-box .label {{ font-size: 12px; color: #666; }}
    .stat-box .value {{ font-size: 20px; font-weight: bold; color: #333; }}
</style>
</head>
<body>
<div class="container">
    <h1>{title}</h1>
    <div class="meta">报告日期: {today} | 生成时间: {now}</div>
"""

    for section in sections:
        heading = section.get("heading", "")
        content = section.get("content", "")
        stype = section.get("type", "text")

        if heading:
            html += f"    <h2>{heading}</h2>\n"

        if stype == "alert":
            alert_class = section.get("alert_class", "")
            html += f'    <div class="alert {alert_class}">{content}</div>\n'
        elif stype == "table":
            html += f"    {content}\n"
        else:
            html += f"    <p>{content}</p>\n"

    if footer:
        html += f'    <div class="footer">{footer}</div>\n'

    html += """
</div>
</body>
</html>"""
    return html


# ============================================================
# 三、预设报告模板
# ============================================================

def send_daily_report(text_report: str, risk_summary: str,
                      signals: list = None) -> bool:
    """
    发送每日交易报告邮件
    
    参数:
        text_report: 文本格式交易信号报告
        risk_summary: 风控摘要
        signals: 信号列表（用于生成HTML表格）
    
    返回: 是否发送成功
    """
    today = datetime.date.today().strftime("%Y-%m-%d")
    subject = f"[交易系统] 每日报告 {today}"

    sections = []

    # 交易信号摘要
    if signals:
        buy_count = sum(1 for _, s in signals if s.get("buy_signal"))
        sell_count = sum(1 for _, s in signals if s.get("sell_signal"))
        add_count = sum(1 for _, s in signals if s.get("add_position"))

        sections.append({
            "heading": "信号概览",
            "content": f"""
<div>
    <div class="stat-box"><div class="label">买入信号</div><div class="value text-green">{buy_count}</div></div>
    <div class="stat-box"><div class="label">卖出信号</div><div class="value text-red">{sell_count}</div></div>
    <div class="stat-box"><div class="label">加仓信号</div><div class="value text-orange">{add_count}</div></div>
    <div class="stat-box"><div class="label">观望</div><div class="value">{len(signals) - buy_count - sell_count - add_count}</div></div>
</div>""",
            "type": "text"
        })

        # 信号明细表格
        table_html = '<table><tr><th>代码</th><th>名称</th><th>信号</th><th>价格</th><th>止损</th><th>说明</th></tr>'
        for code, sig in signals:
            name = config.STOCK_POOL.get(code, {}).get("名称", code)
            if sig.get("sell_signal"):
                row_class = "sell"
                signal_type = '<span class="text-red">卖出</span>'
                price = sig.get("sell_price", "-")
            elif sig.get("buy_signal"):
                row_class = "buy"
                signal_type = '<span class="text-green">买入</span>'
                price = sig.get("buy_price", "-")
            elif sig.get("add_position"):
                row_class = ""
                signal_type = '<span class="text-orange">加仓</span>'
                price = "-"
            else:
                row_class = ""
                signal_type = "观望"
                price = "-"

            stop_loss = sig.get("stop_loss_initial", sig.get("stop_loss_current", "-"))
            reason = sig.get("signal_reason", "")[:60]

            table_html += f'<tr class="{row_class}"><td>{code}</td><td>{name}</td><td>{signal_type}</td><td>{price}</td><td>{stop_loss}</td><td>{reason}</td></tr>'
        table_html += '</table>'

        sections.append({
            "heading": "信号明细",
            "content": table_html,
            "type": "table"
        })

    # 风控摘要
    if risk_summary:
        # 检查是否有熔断警告
        is_alert = "熔断" in risk_summary or "[!]" in risk_summary
        sections.append({
            "heading": "风控摘要",
            "content": risk_summary.replace("\n", "<br>"),
            "type": "alert",
            "alert_class": "alert-danger" if is_alert else "alert-success"
        })

    # 纯文本版本（作为补充）
    if text_report:
        sections.append({
            "heading": "完整报告",
            "content": text_report.replace("\n", "<br>"),
            "type": "text"
        })

    html = _build_html_report(
        title=f"每日交易报告 - {today}",
        sections=sections,
        footer="本报告由交易系统自动生成，仅供参考，不构成投资建议。股市有风险，投资需谨慎。"
    )

    return send_email(subject, html)


def send_risk_alert(message: str, level: str = "warning") -> bool:
    """发送风控预警邮件"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    subject = f"[风控预警] {today} - {'紧急' if level == 'critical' else '警告'}"

    alert_class = "alert-danger" if level == "critical" else ""
    sections = [{
        "heading": "风控预警",
        "content": message.replace("\n", "<br>"),
        "type": "alert",
        "alert_class": alert_class
    }]

    html = _build_html_report(
        title=f"风控预警 - {today}",
        sections=sections,
        footer="请及时查看交易系统进行风控处理。"
    )

    return send_email(subject, html)


def send_backtest_report(report_data: dict) -> bool:
    """
    发送回测报告邮件
    
    参数:
        report_data: 回测结果字典
            {
                "total_return": float,
                "annual_return": float,
                "max_drawdown": float,
                "sharpe_ratio": float,
                "win_rate": float,
                "profit_factor": float,
                "trade_count": int,
                "details": str
            }
    """
    today = datetime.date.today().strftime("%Y-%m-%d")
    subject = f"[回测报告] 策略回测结果 {today}"

    sections = [{
        "heading": "回测核心指标",
        "content": f"""
<div>
    <div class="stat-box"><div class="label">总收益率</div><div class="value {'text-green' if report_data.get('total_return', 0) > 0 else 'text-red'}">{report_data.get('total_return', 0):.2%}</div></div>
    <div class="stat-box"><div class="label">年化收益率</div><div class="value">{report_data.get('annual_return', 0):.2%}</div></div>
    <div class="stat-box"><div class="label">最大回撤</div><div class="value text-red">{report_data.get('max_drawdown', 0):.2%}</div></div>
    <div class="stat-box"><div class="label">夏普比率</div><div class="value">{report_data.get('sharpe_ratio', 0):.2f}</div></div>
    <div class="stat-box"><div class="label">胜率</div><div class="value">{report_data.get('win_rate', 0):.1%}</div></div>
    <div class="stat-box"><div class="label">盈亏比</div><div class="value">{report_data.get('profit_factor', 0):.2f}</div></div>
    <div class="stat-box"><div class="label">交易次数</div><div class="value">{report_data.get('trade_count', 0)}</div></div>
</div>""",
        "type": "text"
    }]

    if report_data.get("details"):
        sections.append({
            "heading": "交易明细",
            "content": report_data["details"].replace("\n", "<br>"),
            "type": "text"
        })

    html = _build_html_report(
        title="策略回测报告",
        sections=sections,
        footer="回测结果基于历史数据，不代表未来表现。"
    )

    return send_email(subject, html)


# ============================================================
# 四、综合日报 V3.0
# ============================================================

def send_comprehensive_daily_report(
    market_status: dict,
    holdings: dict,
    signals: list,
    risk_status: dict,
    weekly_stats: dict = None
) -> bool:
    """
    发送综合日报（V3.0升级版）
    
    参数:
        market_status: 大盘状态 {"index_trend": str, "strength": str, "suggested_position": float}
        holdings: 持仓 {code: {"name", "shares", "buy_price", "current_price", "stop_loss", "highest"}}
        signals: 信号列表 [(code, signal_dict), ...]
        risk_status: 风控状态 {"total_position", "cash_ratio", "sector_concentration", "circuit_breaker"}
        weekly_stats: 周度统计 {"win_rate", "profit_factor", "max_drawdown"} (周五发送)
    """
    today = datetime.date.today().strftime("%Y-%m-%d")
    weekday = datetime.date.today().weekday()  # 0=周一, 4=周五
    subject = f"[综合日报] 交易系统日报 {today}"

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 15px; background: #f0f2f5; }}
    .container {{ max-width: 900px; margin: 0 auto; }}
    .header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); color: white; padding: 18px 25px; border-radius: 10px 10px 0 0; }}
    .header h1 {{ margin: 0; font-size: 20px; }}
    .header .sub {{ font-size: 12px; opacity: 0.8; margin-top: 5px; }}
    .content {{ background: white; padding: 20px 25px; border-radius: 0 0 10px 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
    .panel {{ border: 1px solid #e8e8e8; border-radius: 8px; margin: 15px 0; overflow: hidden; }}
    .panel-title {{ background: #fafafa; padding: 10px 15px; font-weight: bold; font-size: 14px; border-bottom: 1px solid #e8e8e8; }}
    .panel-body {{ padding: 12px 15px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }}
    .metric {{ text-align: center; padding: 10px; background: #f8f9fa; border-radius: 6px; }}
    .metric .label {{ font-size: 11px; color: #888; }}
    .metric .value {{ font-size: 18px; font-weight: bold; margin-top: 3px; }}
    .metric .value.up {{ color: #e74c3c; }}
    .metric .value.down {{ color: #27ae60; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th {{ background: #34495e; color: white; padding: 8px 6px; text-align: center; }}
    td {{ padding: 7px 6px; border-bottom: 1px solid #eee; text-align: center; }}
    .buy-row {{ background: #e8f5e9; }}
    .sell-row {{ background: #ffebee; }}
    .alert {{ padding: 10px 15px; border-radius: 6px; margin: 10px 0; font-size: 13px; }}
    .alert-danger {{ background: #ffebee; border-left: 4px solid #e74c3c; }}
    .time-redline {{ background: #fff1f0; border: 2px solid #ff4d4f; border-radius: 8px; padding: 12px; margin: 15px 0; }}
    .time-redline h4 {{ margin: 0 0 8px; color: #ff4d4f; font-size: 13px; }}
    .time-redline p {{ margin: 3px 0; font-size: 12px; color: #333; }}
    .quality-badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; color: white; }}
    .q-high {{ background: #52c41a; }}
    .q-mid {{ background: #faad14; }}
    .q-low {{ background: #ff4d4f; }}
    .footer {{ text-align: center; color: #999; font-size: 11px; margin-top: 15px; padding-top: 10px; border-top: 1px solid #eee; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📊 交易系统综合日报</h1>
        <div class="sub">日期: {today} | 生成: {datetime.datetime.now().strftime('%H:%M:%S')} | 股票池: {len(config.STOCK_POOL)}只</div>
    </div>
    <div class="content">
"""

    # ---- 1. 大盘状态面板 ----
    strength = market_status.get("strength", "normal")
    strength_map = {"strong": ("强势", "#e74c3c"), "normal": ("震荡", "#faad14"), "weak": ("弱势", "#27ae60")}
    strength_text, strength_color = strength_map.get(strength, ("未知", "#999"))
    suggested_pos = market_status.get("suggested_position", 0.5) * 100

    html += f"""
        <div class="panel">
            <div class="panel-title">🌐 大盘状态</div>
            <div class="panel-body">
                <div class="grid">
                    <div class="metric"><div class="label">沪深300趋势</div><div class="value">{market_status.get('index_trend', '未知')}</div></div>
                    <div class="metric"><div class="label">行情强度</div><div class="value" style="color:{strength_color}">{strength_text}</div></div>
                    <div class="metric"><div class="label">建议仓位</div><div class="value">{suggested_pos:.0f}%</div></div>
                    <div class="metric"><div class="label">操作建议</div><div class="value" style="font-size:13px">{market_status.get('advice', '观望')}</div></div>
                </div>
            </div>
        </div>
"""

    # ---- 2. 持仓盈亏面板 ----
    if holdings:
        html += """
        <div class="panel">
            <div class="panel-title">💼 持仓盈亏</div>
            <div class="panel-body">
                <table>
                    <tr><th>代码</th><th>名称</th><th>持仓</th><th>成本</th><th>现价</th><th>浮盈</th><th>止损价</th><th>距止损</th></tr>
"""
        for code, h in holdings.items():
            name = h.get("name", code)
            shares = h.get("shares", 0)
            buy_price = h.get("buy_price", 0)
            current = h.get("current_price", buy_price)
            stop_loss = h.get("stop_loss", 0)
            pnl_pct = (current - buy_price) / buy_price * 100 if buy_price > 0 else 0
            dist_stop = (current - stop_loss) / current * 100 if current > 0 and stop_loss > 0 else 0
            pnl_class = "up" if pnl_pct >= 0 else "down"
            html += f"""<tr><td>{code}</td><td>{name}</td><td>{shares}</td><td>{buy_price:.2f}</td>
            <td>{current:.2f}</td><td class="{pnl_class}" style="font-weight:bold">{pnl_pct:+.1f}%</td>
            <td>{stop_loss:.2f}</td><td>{dist_stop:.1f}%</td></tr>"""
        html += "</table></div></div>"

    # ---- 3. 今日信号面板 ----
    if signals:
        buy_signals = [(c, s) for c, s in signals if s.get("buy_signal")]
        sell_signals = [(c, s) for c, s in signals if s.get("sell_signal")]
        add_signals = [(c, s) for c, s in signals if s.get("add_position")]

        html += f"""
        <div class="panel">
            <div class="panel-title">📡 今日信号 (买入:{len(buy_signals)} | 卖出:{len(sell_signals)} | 加仓:{len(add_signals)})</div>
            <div class="panel-body">
                <table>
                    <tr><th>代码</th><th>名称</th><th>信号</th><th>价格</th><th>质量分</th><th>说明</th></tr>
"""
        for code, sig in signals:
            name = config.STOCK_POOL.get(code, {}).get("名称", code)
            quality = sig.get("quality_score", 0)
            q_class = "q-high" if quality >= 70 else ("q-mid" if quality >= 55 else "q-low")
            
            if sig.get("sell_signal"):
                row_class = "sell-row"
                signal_type = '<span style="color:#e74c3c;font-weight:bold">卖出</span>'
                price = f"{sig.get('sell_price', 0):.2f}"
            elif sig.get("buy_signal"):
                row_class = "buy-row"
                signal_type = '<span style="color:#27ae60;font-weight:bold">买入</span>'
                price = f"{sig.get('buy_price', 0):.2f}"
            elif sig.get("add_position"):
                row_class = ""
                signal_type = '<span style="color:#faad14;font-weight:bold">加仓</span>'
                price = "-"
            else:
                row_class = ""
                signal_type = "观望"
                price = "-"

            reason = sig.get("signal_reason", "")[:50]
            q_badge = f'<span class="quality-badge {q_class}">{quality}</span>' if quality > 0 else "-"
            html += f'<tr class="{row_class}"><td>{code}</td><td>{name}</td><td>{signal_type}</td><td>{price}</td><td>{q_badge}</td><td style="text-align:left;font-size:11px">{reason}</td></tr>'
        html += "</table></div></div>"

    # ---- 4. 风控仪表盘 ----
    breaker_text = '触发' if risk_status.get('circuit_breaker') else '正常'
    breaker_color = '#e74c3c' if risk_status.get('circuit_breaker') else '#27ae60'
    html += f"""
        <div class="panel">
            <div class="panel-title">🛡️ 风控仪表盘</div>
            <div class="panel-body">
                <div class="grid">
                    <div class="metric"><div class="label">总仓位</div><div class="value">{risk_status.get('total_position', 0)*100:.0f}%</div></div>
                    <div class="metric"><div class="label">现金比例</div><div class="value">{risk_status.get('cash_ratio', 0)*100:.0f}%</div></div>
                    <div class="metric"><div class="label">赛道集中</div><div class="value">{risk_status.get('sector_concentration', 0)*100:.0f}%</div></div>
                    <div class="metric"><div class="label">熔断状态</div><div class="value" style="color:{breaker_color}">{breaker_text}</div></div>
                </div>
"""
    if risk_status.get("circuit_breaker"):
        html += f'<div class="alert alert-danger">⚠️ 风控熔断已触发：{risk_status.get("breaker_reason", "")}</div>'
    html += "</div></div>"

    # ---- 5. 时间红线 ----
    no_trade_morning = getattr(config, 'NO_TRADE_MORNING', ("09:30", "10:00"))
    no_trade_afternoon = getattr(config, 'NO_TRADE_AFTERNOON', ("14:30", "15:00"))
    no_new_after = getattr(config, 'NO_NEW_AFTER', "13:30")
    html += f"""
        <div class="time-redline">
            <h4>⛔ 交易时间红线</h4>
            <p>• <b>{no_trade_morning[0]}-{no_trade_morning[1]}</b> 禁止新开仓（开盘半小时不动）</p>
            <p>• <b>{no_trade_afternoon[0]}-{no_trade_afternoon[1]}</b> 禁止新开仓（收盘半小时不动）</p>
            <p>• <b>{no_new_after}后</b> 禁止新开计划外标的</p>
            <p>• 止损单仅看<b>收盘价</b>，盘中跳水不割肉</p>
        </div>
"""

    # ---- 6. 周度统计（周五发送）----
    if weekday == 4 and weekly_stats:
        html += f"""
        <div class="panel">
            <div class="panel-title">📈 本周统计</div>
            <div class="panel-body">
                <div class="grid">
                    <div class="metric"><div class="label">本周胜率</div><div class="value">{weekly_stats.get('win_rate', 0):.1f}%</div></div>
                    <div class="metric"><div class="label">盈亏比</div><div class="value">{weekly_stats.get('profit_factor', 0):.2f}</div></div>
                    <div class="metric"><div class="label">最大回撤</div><div class="value down">{weekly_stats.get('max_drawdown', 0):.1f}%</div></div>
                    <div class="metric"><div class="label">交易笔数</div><div class="value">{weekly_stats.get('trade_count', 0)}</div></div>
                </div>
            </div>
        </div>
"""

    html += f"""
        <div class="footer">
            本报告由交易系统自动生成 | 仅供参考，不构成投资建议<br>
            股市有风险，投资需谨慎 | 总资金: {config.TOTAL_CAPITAL:,.0f}元
        </div>
    </div>
</div>
</body>
</html>"""

    return send_email(subject, html)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=" * 50)
    print("  邮件通知模块 - 测试")
    print("=" * 50)

    # 模拟发送每日报告
    mock_signals = [
        ("002371", {
            "buy_signal": True, "sell_signal": False, "add_position": False,
            "buy_price": 774.5, "stop_loss_initial": 697.0,
            "signal_reason": "缩量回踩20日线: 量比=0.62, MA20=770.3"
        }),
        ("600584", {
            "buy_signal": False, "sell_signal": True, "add_position": False,
            "sell_price": 38.5, "signal_reason": "回落止盈: 最高42->收盘38.5"
        }),
    ]

    mock_report = """
============================================================
  每日交易信号报告 - 2026-07-18
  总资金: 759,965元 | 股票池: 8只
============================================================

>>> 买入信号:
  002371 北方华创: 买入价 774.50 | 止损 697.00 | 缩量回踩20日线

>>> 卖出信号:
  600584 长电科技: 卖出价 38.50 | 回落止盈
"""

    mock_risk = """
==================================================
  每日风控摘要
==================================================
  总资金:     759,965 元
  持仓市值:   300,000 元 (39.5%)
  现金余额:   459,965 元 (60.5%)
  行情强度:   normal
  仓位上限:   80%
"""

    result = send_daily_report(mock_report, mock_risk, mock_signals)
    print(f"\n发送结果: {'[成功]' if result else '[失败/未配置]'}")
    print("\n[OK] 邮件模块测试完成")
