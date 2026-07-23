"""
P3 实盘运行监控 + 每日自动复盘
================================
数据接口/策略进程/交易通道状态监控 + 每日复盘报告生成

功能:
1. 系统健康监控: 数据源/策略/交易通道状态
2. 每日自动复盘: 当日交易汇总 + 持仓盈亏 + 信号回顾
3. 异常告警: 数据中断/策略异常/通道断连

使用方式:
    from quant.live_monitor import LiveMonitor
    monitor = LiveMonitor()
    status = monitor.check_system_health()
    report = monitor.generate_daily_review(holdings, trades, signals)
"""

import logging
import datetime
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class LiveMonitor:
    """实盘运行监控器"""

    def __init__(self, data_source: str = "baostock",
                 alert_channels: list = None):
        self.data_source = data_source
        self.alert_channels = alert_channels or ["log"]
        self.health_history = []
        self.last_data_time = None
        self.last_signal_time = None

    # ============================================================
    # 一、系统健康监控
    # ============================================================

    def check_system_health(self) -> dict:
        """
        检查系统各组件健康状态

        返回:
            {
                "data_source": {"status": "ok/error", "latency_ms": x},
                "strategy": {"status": "ok/error", "last_run": str},
                "trade_channel": {"status": "ok/error"},
                "overall": "healthy/degraded/critical",
            }
        """
        health = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_source": self._check_data_source(),
            "strategy": self._check_strategy(),
            "trade_channel": self._check_trade_channel(),
        }

        # 综合判断
        statuses = [v["status"] for v in health.values() if isinstance(v, dict)]
        if all(s == "ok" for s in statuses):
            health["overall"] = "healthy"
        elif any(s == "error" for s in statuses):
            health["overall"] = "critical"
        else:
            health["overall"] = "degraded"

        self.health_history.append(health)

        if health["overall"] != "healthy":
            self._send_alert("SYSTEM", f"系统状态: {health['overall']}")

        return health

    def _check_data_source(self) -> dict:
        """检查数据源连通性"""
        try:
            start = datetime.datetime.now()
            # 尝试获取最新数据（简化检查）
            import baostock as bs
            lg = bs.login()
            if lg.error_code == '0':
                bs.logout()
                latency = (datetime.datetime.now() - start).total_seconds() * 1000
                self.last_data_time = datetime.datetime.now()
                return {"status": "ok", "latency_ms": round(latency, 0)}
            else:
                return {"status": "error", "message": lg.error_msg}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _check_strategy(self) -> dict:
        """检查策略运行状态"""
        if self.last_signal_time is None:
            return {"status": "warning", "message": "尚未运行"}

        hours_since = (datetime.datetime.now() - self.last_signal_time).total_seconds() / 3600
        if hours_since > 24:
            return {"status": "warning", "message": f"超过{hours_since:.0f}小时未更新"}
        return {"status": "ok", "last_run": self.last_signal_time.strftime("%H:%M")}

    def _check_trade_channel(self) -> dict:
        """检查交易通道（模拟：始终OK）"""
        return {"status": "ok", "message": "模拟通道"}

    # ============================================================
    # 二、每日自动复盘报告
    # ============================================================

    def generate_daily_review(self, holdings: dict, trades: list,
                              signals: list, market_data: dict = None) -> str:
        """
        生成每日复盘报告

        参数:
            holdings: 当前持仓 {code: {shares, buy_price, current_price, pnl_pct}}
            trades: 当日交易记录
            signals: 当日信号
            market_data: 大盘数据

        返回:
            格式化复盘报告文本
        """
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"{'='*50}",
            f"  每日复盘报告 | {now}",
            f"{'='*50}",
            "",
        ]

        # 1. 大盘概况
        if market_data:
            idx_change = market_data.get("index_change", 0)
            lines.append(f"【大盘】涨跌: {idx_change:+.2f}%")
            lines.append("")

        # 2. 当日交易
        lines.append(f"【当日交易】共{len(trades)}笔")
        for t in trades[:10]:
            action = "买入" if t.get("action") == "buy" else "卖出"
            pnl_str = f" 盈亏{t.get('pnl_pct', 0):+.1%}" if "pnl_pct" in t else ""
            lines.append(f"  {action} {t.get('code', '?')} x{t.get('shares', 0)} "
                        f"@{t.get('price', 0):.2f}{pnl_str}")
        if not trades:
            lines.append("  无交易")
        lines.append("")

        # 3. 持仓盈亏
        lines.append(f"【持仓概况】共{len(holdings)}只")
        total_pnl = 0
        for code, pos in list(holdings.items())[:10]:
            pnl_pct = pos.get("pnl_pct", 0)
            total_pnl += pnl_pct
            status = "盈" if pnl_pct > 0 else "亏"
            lines.append(f"  {code}: {status}{abs(pnl_pct):.1%} "
                        f"(成本{pos.get('buy_price', 0):.2f})")
        if holdings:
            avg_pnl = total_pnl / len(holdings)
            lines.append(f"  平均盈亏: {avg_pnl:+.2%}")
        lines.append("")

        # 4. 信号回顾
        lines.append(f"【今日信号】共{len(signals)}个")
        for s in signals[:5]:
            lines.append(f"  {s.get('code', '?')}: {s.get('reason', '')}")
        lines.append("")

        # 5. 系统状态
        health = self.check_system_health()
        lines.append(f"【系统状态】{health['overall']}")
        lines.append(f"{'='*50}")

        report = "\n".join(lines)
        logger.info(f"复盘报告已生成 ({len(lines)}行)")
        return report

    # ============================================================
    # 三、异常告警
    # ============================================================

    def _send_alert(self, alert_type: str, message: str):
        """发送告警"""
        alert_msg = f"[{alert_type}] {message}"
        logger.warning(alert_msg)

        # 可扩展: 邮件/微信/钉钉
        for channel in self.alert_channels:
            if channel == "email":
                self._send_email_alert(alert_msg)
            elif channel == "wechat":
                self._send_wechat_alert(alert_msg)

    def _send_email_alert(self, message: str):
        """邮件告警（预留接口）"""
        pass

    def _send_wechat_alert(self, message: str):
        """微信告警（预留接口）"""
        pass

    # ============================================================
    # 四、运行统计
    # ============================================================

    def get_uptime_stats(self) -> dict:
        """获取运行统计"""
        if not self.health_history:
            return {"total_checks": 0, "healthy_ratio": 0}

        total = len(self.health_history)
        healthy = sum(1 for h in self.health_history if h.get("overall") == "healthy")

        return {
            "total_checks": total,
            "healthy_ratio": round(healthy / total, 2) if total > 0 else 0,
            "last_check": self.health_history[-1].get("timestamp", ""),
        }
