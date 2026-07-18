"""
邮件通知模块
=============
通过QQ邮箱SMTP发送HTML格式的每日交易报告

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
        msg["From"] = Header(config.EMAIL_SENDER, "utf-8")
        msg["To"] = Header(receiver, "utf-8")
        msg["Subject"] = Header(subject, "utf-8")

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
