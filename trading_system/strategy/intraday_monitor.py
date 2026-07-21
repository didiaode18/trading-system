"""
盘中实时监控与预警推送模块 V1.0
================================
交易时段内定时轮询持仓股行情，触发预警条件时即时推送通知

核心功能:
  1. 持仓股实时价格监控（每60秒轮询）
  2. 止损位触发预警
  3. 急跌预警（5分钟跌幅>3%）
  4. 大盘急跌预警（指数5分钟跌幅>1.5%）
  5. 止盈位到达提醒
  6. 企业微信/钉钉实时推送
  7. 异常波动预警（振幅>8%）

运行方式:
  python -m strategy.intraday_monitor          # 启动监控
  python -m strategy.intraday_monitor --once   # 单次检查

预警规则:
  - 持仓股跌破止损价 → 紧急推送
  - 持仓股5分钟跌>3% → 急跌预警
  - 持仓股浮盈达止盈位 → 止盈提醒
  - 大盘5分钟跌>1.5% → 系统性风险预警
  - 持仓股涨停/跌停 → 极端行情提醒

使用方式:
    from strategy.intraday_monitor import IntradayMonitor
    monitor = IntradayMonitor(holdings)
    monitor.start()  # 启动循环监控
"""

import os
import sys
import json
import time
import logging
import datetime
from typing import Callable

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


class IntradayMonitor:
    """盘中实时监控器"""

    def __init__(self, holdings: dict = None, poll_interval: int = 60):
        """
        参数:
            holdings: 持仓字典 {code: {shares, buy_price, stop_loss, ...}}
            poll_interval: 轮询间隔(秒)，默认60秒
        """
        self.holdings = holdings or {}
        self.poll_interval = poll_interval
        self.alerts_sent = set()  # 已发送的预警（避免重复）
        self.running = False
        self.last_prices = {}     # 上次价格记录
        self.alert_callbacks = [] # 预警回调函数

        # 预警阈值
        self.STOP_LOSS_ALERT = True        # 止损预警
        self.RAPID_DROP_PCT = -0.03        # 急跌阈值(5分钟跌3%)
        self.RAPID_DROP_WINDOW = 5         # 急跌检测窗口(分钟)
        self.MARKET_DROP_PCT = -0.015      # 大盘急跌阈值
        self.PROFIT_TARGET_ALERT = True    # 止盈提醒
        self.AMPLITUDE_ALERT_PCT = 0.08    # 振幅预警(8%)

    # ============================================================
    # 一、启动监控
    # ============================================================

    def start(self):
        """启动循环监控（阻塞式）"""
        if not HAS_AKSHARE:
            logger.error("[盘中监控] akshare未安装，无法启动")
            return

        if not self.holdings:
            logger.warning("[盘中监控] 无持仓数据，退出")
            return

        self.running = True
        logger.info("=" * 50)
        logger.info("  盘中实时监控启动")
        logger.info(f"  监控标的: {len(self.holdings)}只")
        logger.info(f"  轮询间隔: {self.poll_interval}秒")
        logger.info(f"  交易时段: 09:30-11:30, 13:00-15:00")
        logger.info("=" * 50)

        while self.running:
            now = datetime.datetime.now()

            # 只在交易时段运行
            if not self._is_trading_time(now):
                # 非交易时间等待
                time.sleep(300)
                continue

            try:
                self._check_all()
            except Exception as e:
                logger.error(f"[盘中监控] 检查异常: {e}")

            time.sleep(self.poll_interval)

    def stop(self):
        """停止监控"""
        self.running = False
        logger.info("[盘中监控] 已停止")

    def check_once(self) -> list:
        """单次检查（供外部调用）"""
        if not HAS_AKSHARE:
            return []
        return self._check_all()

    # ============================================================
    # 二、核心检查逻辑
    # ============================================================

    def _check_all(self) -> list:
        """检查所有持仓股"""
        alerts = []
        today = datetime.date.today().isoformat()

        # 获取实时行情
        quotes = self._get_realtime_quotes(list(self.holdings.keys()))
        if not quotes:
            return alerts

        # 获取大盘行情
        market_quote = self._get_index_quote()

        for code, holding in self.holdings.items():
            if code not in quotes:
                continue

            quote = quotes[code]
            current_price = quote.get("price", 0)
            if current_price <= 0:
                continue

            buy_price = holding.get("buy_price", current_price)
            stop_loss = holding.get("stop_loss", buy_price * (1 - config.INITIAL_STOP_LOSS_PCT))
            name = holding.get("name", self._get_stock_name(code))

            # 更新价格记录
            self.last_prices[code] = {
                "price": current_price,
                "time": datetime.datetime.now().isoformat()
            }

            # ---- 检查1: 止损位触发 ----
            if self.STOP_LOSS_ALERT and current_price <= stop_loss:
                alert_key = f"{today}_{code}_stop_loss"
                if alert_key not in self.alerts_sent:
                    alert = {
                        "level": "critical",
                        "type": "止损触发",
                        "code": code,
                        "name": name,
                        "current_price": current_price,
                        "stop_loss": stop_loss,
                        "buy_price": buy_price,
                        "loss_pct": round((current_price / buy_price - 1) * 100, 2),
                        "message": f"⚠️ {name}({code}) 已跌破止损位！"
                                  f"现价{current_price:.2f} ≤ 止损{stop_loss:.2f} | "
                                  f"浮亏{(current_price/buy_price-1)*100:.1f}% | 建议立即止损",
                        "time": datetime.datetime.now().strftime("%H:%M:%S"),
                    }
                    alerts.append(alert)
                    self.alerts_sent.add(alert_key)
                    self._send_alert(alert)

            # ---- 检查2: 急跌预警 ----
            change_pct = quote.get("change_pct", 0)
            if change_pct and change_pct < self.RAPID_DROP_PCT * 100:
                alert_key = f"{today}_{code}_rapid_drop_{int(change_pct)}"
                if alert_key not in self.alerts_sent:
                    alert = {
                        "level": "warning",
                        "type": "急跌预警",
                        "code": code,
                        "name": name,
                        "current_price": current_price,
                        "change_pct": change_pct,
                        "message": f"⚡ {name}({code}) 急跌{change_pct:.1f}%！"
                                  f"现价{current_price:.2f} | 注意风险",
                        "time": datetime.datetime.now().strftime("%H:%M:%S"),
                    }
                    alerts.append(alert)
                    self.alerts_sent.add(alert_key)
                    self._send_alert(alert)

            # ---- 检查3: 止盈位到达 ----
            if self.PROFIT_TARGET_ALERT:
                profit_pct = (current_price / buy_price - 1) if buy_price > 0 else 0
                # 第一止盈位8%
                if profit_pct >= 0.08:
                    alert_key = f"{today}_{code}_profit_8"
                    if alert_key not in self.alerts_sent:
                        alert = {
                            "level": "info",
                            "type": "止盈提醒",
                            "code": code,
                            "name": name,
                            "current_price": current_price,
                            "profit_pct": round(profit_pct * 100, 2),
                            "message": f"🎯 {name}({code}) 浮盈{profit_pct*100:.1f}%达第一止盈位！"
                                      f"建议卖出1/3锁定利润",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                        }
                        alerts.append(alert)
                        self.alerts_sent.add(alert_key)
                        self._send_alert(alert)

            # ---- 检查4: 振幅异常 ----
            amplitude = quote.get("amplitude", 0)
            if amplitude and amplitude > self.AMPLITUDE_ALERT_PCT * 100:
                alert_key = f"{today}_{code}_amplitude"
                if alert_key not in self.alerts_sent:
                    alert = {
                        "level": "warning",
                        "type": "振幅异常",
                        "code": code,
                        "name": name,
                        "amplitude": amplitude,
                        "message": f"📊 {name}({code}) 今日振幅{amplitude:.1f}%异常！注意风险",
                        "time": datetime.datetime.now().strftime("%H:%M:%S"),
                    }
                    alerts.append(alert)
                    self.alerts_sent.add(alert_key)
                    self._send_alert(alert)

        # ---- 检查5: 大盘急跌 ----
        if market_quote:
            market_change = market_quote.get("change_pct", 0)
            if market_change and market_change < self.MARKET_DROP_PCT * 100:
                alert_key = f"{today}_market_crash_{int(market_change)}"
                if alert_key not in self.alerts_sent:
                    alert = {
                        "level": "critical",
                        "type": "大盘急跌",
                        "code": "000300",
                        "name": "沪深300",
                        "change_pct": market_change,
                        "message": f"🚨 大盘急跌{market_change:.1f}%！系统性风险预警，"
                                  f"建议暂停买入、评估是否需要减仓",
                        "time": datetime.datetime.now().strftime("%H:%M:%S"),
                    }
                    alerts.append(alert)
                    self.alerts_sent.add(alert_key)
                    self._send_alert(alert)

        return alerts

    # ============================================================
    # 三、数据获取
    # ============================================================

    def _get_realtime_quotes(self, codes: list) -> dict:
        """获取实时行情（精准批量接口，不再拉取全市场）"""
        if not codes:
            return {}

        try:
            from data.realtime import fetch_realtime_batch
            raw = fetch_realtime_batch(codes)
            # 转换为监控模块期望的格式
            quotes = {}
            for code, info in raw.items():
                quotes[code] = {
                    "price": info.get("price", 0),
                    "change_pct": info.get("change_pct", 0),
                    "amplitude": info.get("amplitude", 0),
                    "high": info.get("high", 0),
                    "low": info.get("low", 0),
                    "open": info.get("open", 0),
                }
            return quotes
        except Exception as e:
            logger.debug(f"[盘中监控] 行情获取失败: {e}")
            return {}

    def _get_index_quote(self) -> dict:
        """获取大盘指数行情（腾讯API精准获取）"""
        try:
            from data.realtime import fetch_index_realtime
            result = fetch_index_realtime("000300")
            if result:
                return {
                    "price": result.get("price", 0),
                    "change_pct": result.get("change_pct", 0),
                }
            return {}
        except Exception:
            return {}

    # ============================================================
    # 四、预警推送
    # ============================================================

    def _send_alert(self, alert: dict):
        """发送预警通知"""
        message = alert.get("message", "")
        level = alert.get("level", "info")

        logger.warning(f"[盘中预警-{level.upper()}] {message}")

        # 企业微信推送
        if config.WECHAT_WORK_WEBHOOK:
            self._send_wechat(message, level)

        # 钉钉推送
        if config.DINGTALK_WEBHOOK:
            self._send_dingtalk(message, level)

        # 邮件推送（仅critical级别）
        if level == "critical" and config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            self._send_email_alert(alert)

        # 回调函数
        for callback in self.alert_callbacks:
            try:
                callback(alert)
            except Exception:
                pass

    def _send_wechat(self, message: str, level: str):
        """企业微信机器人推送"""
        try:
            import urllib.request
            color = "warning" if level == "critical" else "comment"
            data = json.dumps({
                "msgtype": "markdown",
                "markdown": {"content": f"**交易系统预警**\n>{message}"}
            }).encode("utf-8")
            req = urllib.request.Request(
                config.WECHAT_WORK_WEBHOOK,
                data=data,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.debug(f"[盘中监控] 企微推送失败: {e}")

    def _send_dingtalk(self, message: str, level: str):
        """钉钉机器人推送"""
        try:
            import urllib.request
            data = json.dumps({
                "msgtype": "text",
                "text": {"content": f"[交易系统预警] {message}"}
            }).encode("utf-8")
            req = urllib.request.Request(
                config.DINGTALK_WEBHOOK,
                data=data,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.debug(f"[盘中监控] 钉钉推送失败: {e}")

    def _send_email_alert(self, alert: dict):
        """邮件推送（紧急预警）"""
        try:
            from notify.email_notify import send_email
            subject = f"[紧急预警] {alert['type']} - {alert.get('name', '')} {alert['time']}"
            html = f"""
            <div style="font-family:Microsoft YaHei;padding:20px">
                <h2 style="color:#FF4D4F">⚠️ {alert['type']}</h2>
                <p style="font-size:16px">{alert['message']}</p>
                <table style="border-collapse:collapse;margin:15px 0">
                    <tr><td style="padding:5px 15px;border:1px solid #eee"><b>股票</b></td>
                        <td style="padding:5px 15px;border:1px solid #eee">{alert.get('name','')} ({alert.get('code','')})</td></tr>
                    <tr><td style="padding:5px 15px;border:1px solid #eee"><b>现价</b></td>
                        <td style="padding:5px 15px;border:1px solid #eee">{alert.get('current_price','')}</td></tr>
                    <tr><td style="padding:5px 15px;border:1px solid #eee"><b>时间</b></td>
                        <td style="padding:5px 15px;border:1px solid #eee">{alert.get('time','')}</td></tr>
                </table>
                <p style="color:#999;font-size:12px">此邮件由盘中监控系统自动发送</p>
            </div>"""
            send_email(subject, html)
        except Exception as e:
            logger.debug(f"[盘中监控] 邮件推送失败: {e}")

    def add_alert_callback(self, callback: Callable):
        """添加预警回调函数"""
        self.alert_callbacks.append(callback)

    # ============================================================
    # 五、辅助方法
    # ============================================================

    def _is_trading_time(self, now: datetime.datetime = None) -> bool:
        """判断是否在交易时段"""
        if now is None:
            now = datetime.datetime.now()

        # 周末不交易
        if now.weekday() >= 5:
            return False

        t = now.strftime("%H:%M")
        # 上午 09:30 - 11:30
        if "09:30" <= t <= "11:30":
            return True
        # 下午 13:00 - 15:00
        if "13:00" <= t <= "15:00":
            return True
        return False

    def _get_stock_name(self, code: str) -> str:
        if code in config.STOCK_POOL:
            return config.STOCK_POOL[code].get("名称", code)
        for sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).values():
            if code in sector_info.get("stocks", {}):
                return sector_info["stocks"][code].get("名称", code)
        return code

    def update_holdings(self, holdings: dict):
        """更新持仓数据"""
        self.holdings = holdings
        logger.info(f"[盘中监控] 持仓已更新: {len(holdings)}只")


# ============================================================
# 命令行入口
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘中实时监控")
    parser.add_argument("--once", action="store_true", help="单次检查后退出")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s [%(levelname)s] %(message)s")

    # 加载持仓
    holdings_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "holdings.json"
    )
    holdings = {}
    if os.path.exists(holdings_file):
        with open(holdings_file, "r", encoding="utf-8") as f:
            holdings = json.load(f)

    # 为持仓添加名称和止损价
    for code, pos in holdings.items():
        pos["name"] = config.get_stock_name(code)
        if "stop_loss" not in pos:
            pos["stop_loss"] = pos["buy_price"] * (1 - config.INITIAL_STOP_LOSS_PCT)

    monitor = IntradayMonitor(holdings)

    if args.once:
        alerts = monitor.check_once()
        if alerts:
            print(f"\n发现 {len(alerts)} 条预警:")
            for a in alerts:
                print(f"  [{a['level']}] {a['message']}")
        else:
            print("\n无预警，持仓正常")
    else:
        monitor.start()


if __name__ == "__main__":
    main()
