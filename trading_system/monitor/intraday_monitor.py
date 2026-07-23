"""
盘中实时信号监控模块
====================
盘中每5分钟检查条件单状态，提供预警和异常检测

核心功能:
  1. 条件单触发预警：距离触发价 < 1% 时推送预警
  2. 急跌保护：盘中急跌 > 3% 暂停条件单（反洗盘）
  3. 尾盘确认：14:50 检查所有条件单状态
  4. 异常检测：量价异常、涨跌停预警
  5. 执行进度：大单拆分后各笔执行状态追踪

使用方式:
    from monitor.intraday_monitor import IntradayMonitor
    monitor = IntradayMonitor(orders, holdings)
    alerts = monitor.check_alerts(realtime_data)
"""

import pandas as pd
import numpy as np
import logging
import datetime
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class IntradayMonitor:
    """盘中实时监控器"""

    def __init__(self, orders: list = None, holdings: dict = None,
                 alert_threshold: float = 0.01, crash_threshold: float = -0.03):
        """
        参数:
            orders: 当日条件单列表
            holdings: 当前持仓
            alert_threshold: 触发预警的距离阈值（1%）
            crash_threshold: 急跌保护阈值（-3%）
        """
        self.orders = orders or []
        self.holdings = holdings or {}
        self.alert_threshold = alert_threshold
        self.crash_threshold = crash_threshold
        self._alert_history = []
        self._paused_orders = set()  # 被暂停的条件单

    def check_alerts(self, realtime_data: dict) -> list:
        """
        检查所有条件单的实时状态
        
        参数:
            realtime_data: {code: {price, prev_close, open, high, low, volume, ...}}
        
        返回:
            [alert_dict, ...] 预警列表
        """
        alerts = []
        now = datetime.datetime.now()
        current_time = now.strftime("%H:%M")

        for order in self.orders:
            code = order.get("code", "")
            if code not in realtime_data:
                continue
            if code in self._paused_orders:
                continue

            rt = realtime_data[code]
            current_price = rt.get("price", 0)
            prev_close = rt.get("prev_close", 0)
            trigger_price = order.get("trigger_price", 0)

            if current_price <= 0 or trigger_price <= 0:
                continue

            # ---- 1. 触发预警 ----
            distance = abs(current_price - trigger_price) / trigger_price
            if distance < self.alert_threshold:
                alert = {
                    "type": "trigger_warning",
                    "level": "warning",
                    "code": code,
                    "name": order.get("name", ""),
                    "message": (
                        f"⚡ {order.get('name', '')}({code}) 即将触发条件单! "
                        f"当前{current_price:.3f} 距触发价{trigger_price:.3f} "
                        f"仅{distance:.2%}"
                    ),
                    "time": current_time,
                    "order": order,
                }
                alerts.append(alert)

            # ---- 2. 急跌保护 ----
            if prev_close > 0:
                change_pct = (current_price - prev_close) / prev_close
                if change_pct <= self.crash_threshold:
                    # 检查是否系统性风险
                    is_systemic = self._check_systemic_risk(realtime_data)
                    
                    if not is_systemic:
                        # 非系统性急跌 → 暂停条件单（可能是洗盘）
                        self._paused_orders.add(code)
                        alert = {
                            "type": "crash_protection",
                            "level": "critical",
                            "code": code,
                            "name": order.get("name", ""),
                            "message": (
                                f"🛡️ {order.get('name', '')}({code}) 盘中急跌{change_pct:.1%}，"
                                f"非系统性风险，条件单已暂停（反洗盘保护）"
                            ),
                            "time": current_time,
                            "action": "pause_order",
                        }
                        alerts.append(alert)
                    else:
                        # 系统性风险 → 保留条件单
                        alert = {
                            "type": "systemic_risk",
                            "level": "critical",
                            "code": code,
                            "name": order.get("name", ""),
                            "message": (
                                f"🚨 {order.get('name', '')}({code}) 急跌{change_pct:.1%}，"
                                f"检测到系统性风险，条件单保持激活"
                            ),
                            "time": current_time,
                            "action": "keep_active",
                        }
                        alerts.append(alert)

            # ---- 3. 涨跌停预警 ----
            if prev_close > 0:
                change_pct = (current_price - prev_close) / prev_close
                if change_pct > 0.095:
                    alerts.append({
                        "type": "limit_up",
                        "level": "info",
                        "code": code,
                        "name": order.get("name", ""),
                        "message": f"🔴 {order.get('name', '')}({code}) 涨停! 买入条件单可能无法成交",
                        "time": current_time,
                    })
                elif change_pct < -0.095:
                    alerts.append({
                        "type": "limit_down",
                        "level": "critical",
                        "code": code,
                        "name": order.get("name", ""),
                        "message": f"🟢 {order.get('name', '')}({code}) 跌停! 卖出条件单可能无法成交",
                        "time": current_time,
                    })

        self._alert_history.extend(alerts)
        return alerts

    def end_of_day_check(self, realtime_data: dict) -> dict:
        """
        尾盘确认（14:50执行）
        
        返回:
            {
                "summary": str,
                "triggered": list,      # 已触发
                "near_trigger": list,   # 接近触发
                "far_from_trigger": list,  # 远离触发
                "paused": list,         # 被暂停
                "recommendations": list,
            }
        """
        triggered = []
        near_trigger = []
        far_from_trigger = []
        recommendations = []

        for order in self.orders:
            code = order.get("code", "")
            if code not in realtime_data:
                continue

            rt = realtime_data[code]
            current_price = rt.get("price", 0)
            trigger_price = order.get("trigger_price", 0)

            if current_price <= 0 or trigger_price <= 0:
                continue

            distance = (current_price - trigger_price) / trigger_price
            is_buy = order.get("order_type", "").startswith("buy")

            # 判断是否已触发
            if is_buy and current_price <= trigger_price:
                triggered.append(order)
            elif not is_buy and current_price >= trigger_price:
                triggered.append(order)
            elif abs(distance) < 0.02:
                near_trigger.append(order)
            else:
                far_from_trigger.append(order)

        # 生成建议
        if far_from_trigger:
            recommendations.append(
                f"{len(far_from_trigger)}笔条件单远离触发价，评估是否撤销或调整"
            )
        if self._paused_orders:
            recommendations.append(
                f"{len(self._paused_orders)}笔条件单被暂停（急跌保护），盘后评估是否恢复"
            )

        summary = (
            f"尾盘确认: 已触发{len(triggered)}笔 | "
            f"接近触发{len(near_trigger)}笔 | "
            f"远离{len(far_from_trigger)}笔 | "
            f"暂停{len(self._paused_orders)}笔"
        )

        return {
            "summary": summary,
            "triggered": triggered,
            "near_trigger": near_trigger,
            "far_from_trigger": far_from_trigger,
            "paused": list(self._paused_orders),
            "recommendations": recommendations,
        }

    def resume_paused(self, code: str = None):
        """恢复被暂停的条件单"""
        if code:
            self._paused_orders.discard(code)
        else:
            self._paused_orders.clear()

    def get_status(self) -> dict:
        """获取监控器状态"""
        return {
            "total_orders": len(self.orders),
            "paused_orders": len(self._paused_orders),
            "total_alerts": len(self._alert_history),
            "recent_alerts": self._alert_history[-5:],
        }

    # ============================================================
    # 内部方法
    # ============================================================

    def _check_systemic_risk(self, realtime_data: dict) -> bool:
        """
        检测是否系统性风险（大盘普跌）
        如果超过60%的股票跌幅>2%，判定为系统性风险
        """
        if not realtime_data:
            return False

        down_count = 0
        total = 0
        for code, rt in realtime_data.items():
            prev_close = rt.get("prev_close", 0)
            price = rt.get("price", 0)
            if prev_close > 0 and price > 0:
                total += 1
                change = (price - prev_close) / prev_close
                if change < -0.02:
                    down_count += 1

        if total == 0:
            return False

        return down_count / total > 0.6
