# -*- coding: utf-8 -*-
"""
智能预警系统 (Alert Engine)
============================
对标同花顺/通达信条件预警，盘中实时监控关键信号

触发条件:
  1. DK信号触发（金叉/死叉）
  2. 价格突破控盘生命线
  3. 乖离率进入超买/超卖区
  4. 板块资金异常流出
  5. 止损位触及
  6. 涨停/跌停
  7. 筹码获利盘骤变

推送渠道:
  - Windows桌面通知 (win10toast)
  - 控制台输出
  - 邮件（已有notify模块）

运行方式:
  - 盘中每5分钟轮询（9:30-15:00）
  - 集成到scheduler.py

使用:
    from notify.alert_engine import AlertEngine
    engine = AlertEngine()
    engine.check_alerts(holdings_data)
"""

import os
import sys
import json
import logging
import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 预警配置
ALERT_CONFIG = {
    "check_interval_min": 5,       # 检查间隔（分钟）
    "trading_start": "09:30",      # 开盘时间
    "trading_end": "15:00",        # 收盘时间
    "lunch_start": "11:30",        # 午休开始
    "lunch_end": "13:00",          # 午休结束
    "deviation_overbought": 8.0,   # 超买乖离率阈值(%)
    "deviation_oversold": -8.0,    # 超卖乖离率阈值(%)
    "profit_ratio_drop": 0.10,     # 获利盘骤降阈值(10%)
    "stop_loss_pct": -0.08,        # 止损线(-8%)
    "alert_cooldown_min": 30,      # 同一标的预警冷却时间(分钟)
    "alert_log_file": "alerts.json",  # 预警记录文件
}


class AlertEngine:
    """智能预警引擎"""

    def __init__(self, config: dict = None, holdings: dict = None):
        self.cfg = {**ALERT_CONFIG, **(config or {})}
        self.holdings = holdings or {}
        self._alert_history = {}  # {code: last_alert_time}
        self._today_alerts = []   # 今日所有预警
        self._load_history()

    def check_alerts(self, results: List[dict]) -> List[dict]:
        """
        检查所有标的的预警条件

        参数:
            results: CaopanEngine.analyze()的结果列表

        返回:
            触发的预警列表
        """
        triggered = []

        for r in results:
            code = r.get("code", "")
            name = r.get("name", code)

            # 冷却检查
            if self._in_cooldown(code):
                continue

            alerts = self._check_single(r)
            for alert in alerts:
                alert["code"] = code
                alert["name"] = name
                alert["time"] = datetime.datetime.now().strftime("%H:%M:%S")
                triggered.append(alert)

            if alerts:
                self._alert_history[code] = datetime.datetime.now()

        # 推送
        if triggered:
            self._push_alerts(triggered)
            self._today_alerts.extend(triggered)
            self._save_history()

        return triggered

    def _check_single(self, r: dict) -> List[dict]:
        """检查单只标的的所有预警条件"""
        alerts = []
        code = r.get("code", "")
        name = r.get("name", code)
        close = r.get("close", 0)

        # 1. DK信号触发
        dk = r.get("dk_signal")
        dk_strength = r.get("dk_strength", 0)
        dk_filtered = r.get("dk_filtered", False)
        if dk == "D" and dk_strength >= 50 and not dk_filtered:
            alerts.append({
                "type": "dk_buy",
                "level": "high",
                "icon": "🔴",
                "msg": f"{name} D点买入信号! 强度{dk_strength}分 | {r.get('dk_reason','')}",
            })
        elif dk == "K" and dk_strength >= 50 and not dk_filtered:
            alerts.append({
                "type": "dk_sell",
                "level": "high",
                "icon": "🟢",
                "msg": f"{name} K点卖出信号! 强度{dk_strength}分 | {r.get('dk_reason','')}",
            })

        # 2. 价格突破生命线
        ll_fast = r.get("ll_fast", 0)
        ll_slow = r.get("ll_slow", 0)
        if ll_fast > 0 and close > 0:
            # 跌破LL1
            if close < ll_fast * 0.99 and r.get("trend_level", 3) >= 4:
                alerts.append({
                    "type": "break_ll1",
                    "level": "warning",
                    "icon": "⚠️",
                    "msg": f"{name} 跌破LL1生命线! 现价{close:.3f} < LL1={ll_fast:.3f}",
                })
            # 站上LL1（下跌趋势中）
            elif close > ll_fast * 1.01 and r.get("trend_level", 3) <= 2:
                alerts.append({
                    "type": "cross_ll1",
                    "level": "info",
                    "icon": "📈",
                    "msg": f"{name} 站上LL1! 现价{close:.3f} > LL1={ll_fast:.3f} (趋势可能反转)",
                })

        # 3. 乖离率超买/超卖
        deviation = r.get("deviation_pct", 0)
        if deviation > self.cfg["deviation_overbought"]:
            alerts.append({
                "type": "overbought",
                "level": "warning",
                "icon": "🔥",
                "msg": f"{name} 超买! 乖离率{deviation:.1f}% > {self.cfg['deviation_overbought']}%",
            })
        elif deviation < self.cfg["deviation_oversold"]:
            alerts.append({
                "type": "oversold",
                "level": "info",
                "icon": "❄️",
                "msg": f"{name} 超卖! 乖离率{deviation:.1f}% < {self.cfg['deviation_oversold']}%",
            })

        # 4. 止损位触及
        info = self.holdings.get(code, {})
        buy_price = info.get("cost", 0) or info.get("buy_price", 0)
        if buy_price > 0 and close > 0:
            pnl_pct = (close - buy_price) / buy_price
            if pnl_pct <= self.cfg["stop_loss_pct"]:
                alerts.append({
                    "type": "stop_loss",
                    "level": "critical",
                    "icon": "🚨",
                    "msg": f"{name} 触及止损线! 亏损{pnl_pct*100:.1f}% (成本{buy_price:.3f} 现价{close:.3f})",
                })

        # 5. 涨停/跌停
        if close > 0:
            df = r.get("df_analyzed")
            if df is not None and len(df) >= 2:
                prev_close = df["close"].iloc[-2]
                if prev_close > 0:
                    change_pct = (close - prev_close) / prev_close * 100
                    if change_pct >= 9.8:
                        alerts.append({
                            "type": "limit_up",
                            "level": "info",
                            "icon": "🚀",
                            "msg": f"{name} 涨停! +{change_pct:.1f}%",
                        })
                    elif change_pct <= -9.8:
                        alerts.append({
                            "type": "limit_down",
                            "level": "critical",
                            "icon": "💥",
                            "msg": f"{name} 跌停! {change_pct:.1f}%",
                        })

        # 6. 筹码获利盘骤变
        chip = r.get("chip")
        if chip:
            history = chip.get("profit_history", [])
            if len(history) >= 2:
                latest_pr = history[-1].get("profit_ratio", 0)
                prev_pr = history[-2].get("profit_ratio", 0)
                drop = prev_pr - latest_pr
                if drop > self.cfg["profit_ratio_drop"]:
                    alerts.append({
                        "type": "chip_panic",
                        "level": "warning",
                        "icon": "📉",
                        "msg": f"{name} 获利盘骤降{drop*100:.0f}%! ({prev_pr*100:.0f}%→{latest_pr*100:.0f}%) 主力可能出货",
                    })

        # 7. 板块资金异常（从fund_data）
        fund_data = r.get("fund_data", {})
        if fund_data.get("score", 50) <= 25:
            alerts.append({
                "type": "fund_outflow",
                "level": "warning",
                "icon": "💸",
                "msg": f"{name} 资金大幅流出! 评分{fund_data.get('score')}分 | {fund_data.get('signal','')}",
            })

        return alerts

    def _in_cooldown(self, code: str) -> bool:
        """检查是否在冷却期内"""
        last_time = self._alert_history.get(code)
        if last_time is None:
            return False
        elapsed = (datetime.datetime.now() - last_time).total_seconds() / 60
        return elapsed < self.cfg["alert_cooldown_min"]

    def _push_alerts(self, alerts: List[dict]):
        """推送预警"""
        for alert in alerts:
            # 控制台输出
            level_icon = {"critical": "🚨", "high": "⚡", "warning": "⚠️", "info": "ℹ️"}.get(alert["level"], "")
            print(f"  {level_icon} [{alert['time']}] {alert['msg']}")

        # Windows桌面通知
        self._windows_notify(alerts)

    def _windows_notify(self, alerts: List[dict]):
        """Windows桌面通知"""
        if sys.platform != "win32":
            return

        try:
            # 尝试使用win10toast
            from win10toast import ToastNotifier
            toaster = ToastNotifier()
            # 只推送critical和high级别
            important = [a for a in alerts if a["level"] in ("critical", "high")]
            if important:
                title = f"操盘密码预警 ({len(important)}条)"
                body = "\n".join([a["msg"] for a in important[:3]])
                toaster.show_toast(title, body, duration=10, threaded=True)
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"桌面通知失败: {e}")

    def _load_history(self):
        """加载预警历史"""
        log_path = self._get_log_path()
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._today_alerts = data.get("today", [])
            except Exception:
                pass

    def _save_history(self):
        """保存预警记录"""
        log_path = self._get_log_path()
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump({
                    "date": datetime.date.today().isoformat(),
                    "today": self._today_alerts[-50:],  # 保留最近50条
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"保存预警记录失败: {e}")

    def _get_log_path(self) -> str:
        """获取日志文件路径"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, "output", self.cfg["alert_log_file"])

    def get_today_summary(self) -> str:
        """获取今日预警摘要"""
        if not self._today_alerts:
            return "今日无预警触发"

        lines = [f"📢 今日预警 ({len(self._today_alerts)}条):"]
        for a in self._today_alerts[-10:]:
            lines.append(f"  {a.get('icon','')} [{a.get('time','')}] {a.get('msg','')}")
        return "\n".join(lines)


def is_trading_time() -> bool:
    """判断当前是否为交易时间"""
    now = datetime.datetime.now()
    # 周末不交易
    if now.weekday() >= 5:
        return False

    current = now.strftime("%H:%M")
    cfg = ALERT_CONFIG

    # 上午盘
    if cfg["trading_start"] <= current <= cfg["lunch_start"]:
        return True
    # 下午盘
    if cfg["lunch_end"] <= current <= cfg["trading_end"]:
        return True

    return False
