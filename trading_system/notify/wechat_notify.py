"""
消息通知模块
=============
支持钉钉机器人、企业微信机器人发送预警消息

使用场景:
- 盘中价格触及买点、止损位、止盈位时自动发提醒
- 每日盘前发送当日条件单摘要
- 风控熔断时紧急通知
"""

import json
import logging
import datetime
import urllib.request
import urllib.error

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ============================================================
# 一、钉钉机器人通知
# ============================================================

def send_dingtalk(title: str, content: str, webhook_url: str = None) -> bool:
    """
    发送钉钉机器人消息（Markdown格式）
    
    参数:
        title: 消息标题
        content: Markdown格式消息内容
        webhook_url: 钉钉机器人Webhook地址（默认取config）
    
    返回: 是否发送成功
    """
    if webhook_url is None:
        webhook_url = config.DINGTALK_WEBHOOK
    if not webhook_url:
        logger.warning("钉钉Webhook未配置，跳过发送")
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": content
        }
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errcode") == 0:
                logger.info(f"钉钉通知发送成功: {title}")
                return True
            else:
                logger.error(f"钉钉通知失败: {result}")
                return False
    except Exception as e:
        logger.error(f"钉钉通知异常: {e}")
        return False


# ============================================================
# 二、企业微信机器人通知
# ============================================================

def send_wechat_work(title: str, content: str, webhook_url: str = None) -> bool:
    """
    发送企业微信机器人消息（Markdown格式）
    
    参数:
        title: 消息标题
        content: Markdown格式消息内容
        webhook_url: 企业微信Webhook地址（默认取config）
    
    返回: 是否发送成功
    """
    if webhook_url is None:
        webhook_url = config.WECHAT_WORK_WEBHOOK
    if not webhook_url:
        logger.warning("企业微信Webhook未配置，跳过发送")
        return False

    # 企业微信Markdown内容限制2048字节
    md_content = f"## {title}\n\n{content}"
    if len(md_content.encode("utf-8")) > 2000:
        md_content = md_content[:600] + "\n\n...(内容过长已截断)"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": md_content
        }
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errcode") == 0:
                logger.info(f"企微通知发送成功: {title}")
                return True
            else:
                logger.error(f"企微通知失败: {result}")
                return False
    except Exception as e:
        logger.error(f"企微通知异常: {e}")
        return False


# ============================================================
# 三、统一发送接口
# ============================================================

def send_notification(title: str, content: str) -> dict:
    """
    统一通知接口，同时发送到所有已配置的渠道
    
    返回: {"dingtalk": bool, "wechat": bool}
    """
    results = {"dingtalk": False, "wechat": False}

    if config.DINGTALK_WEBHOOK:
        results["dingtalk"] = send_dingtalk(title, content)
    if config.WECHAT_WORK_WEBHOOK:
        results["wechat"] = send_wechat_work(title, content)

    if not any(results.values()):
        logger.info(f"通知内容: [{title}] {content[:100]}...")

    return results


# ============================================================
# 四、预设通知模板
# ============================================================

def notify_buy_signal(code: str, name: str, buy_price: float,
                      stop_loss: float, shares: int):
    """发送买入信号通知"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    title = f"[买入信号] {name}({code})"
    content = (
        f"**日期**: {today}\n\n"
        f"**标的**: {name}({code})\n\n"
        f"**买入价**: {buy_price:.2f}\n\n"
        f"**止损价**: {stop_loss:.2f} (跌幅{(buy_price-stop_loss)/buy_price:.1%})\n\n"
        f"**建议股数**: {shares}股\n\n"
        f"**建议金额**: {shares * buy_price:,.0f}元\n\n"
        f"> 请设置条件单，到价自动买入"
    )
    send_notification(title, content)


def notify_sell_signal(code: str, name: str, sell_price: float,
                       sell_type: str, reason: str):
    """发送卖出信号通知"""
    type_map = {
        "stop_loss": "止损",
        "drawdown_profit": "回落止盈",
        "trend_break": "趋势破位"
    }
    type_cn = type_map.get(sell_type, sell_type)
    today = datetime.date.today().strftime("%Y-%m-%d")
    title = f"[{type_cn}信号] {name}({code})"
    content = (
        f"**日期**: {today}\n\n"
        f"**标的**: {name}({code})\n\n"
        f"**卖出价**: {sell_price:.2f}\n\n"
        f"**信号类型**: {type_cn}\n\n"
        f"**原因**: {reason}\n\n"
        f"> 请尽快设置卖出条件单"
    )
    send_notification(title, content)


def notify_risk_alert(message: str, level: str = "warning"):
    """发送风控预警通知"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    emoji = "[!]" if level == "critical" else "[!]"
    title = f"{emoji} 风控预警"
    content = (
        f"**日期**: {today}\n\n"
        f"**级别**: {level}\n\n"
        f"**详情**: {message}"
    )
    send_notification(title, content)


def notify_daily_summary(summary_text: str):
    """发送每日盘前摘要"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    title = f"[盘前摘要] {today}"
    send_notification(title, summary_text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=" * 50)
    print("  通知模块 - 测试")
    print("=" * 50)
    print("\n（未配置Webhook，仅打印内容）\n")

    # 模拟发送
    notify_buy_signal("002049", "紫光国微", 200.0, 180.0, 400)
    notify_sell_signal("002049", "紫光国微", 215.0, "drawdown_profit",
                       "高点225回落4%触发")
    notify_risk_alert("当日亏损已达1.8%，接近熔断线", "warning")
    print("\n[OK] 通知模块测试完成")
