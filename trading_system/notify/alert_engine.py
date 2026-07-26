# -*- coding: utf-8 -*-
"""
智能预警系统 (Alert Engine) V2.0
================================
对标同花顺/通达信条件预警，盘中实时监控关键信号

触发条件(9大规则):
  R1. DK信号触发（金叉/死叉）
  R2. 价格突破控盘生命线
  R3. 乖离率进入超买/超卖区
  R4. 板块资金异常流出
  R5. 止损位触及
  R6. 涨停/跌停
  R7. 筹码获利盘骤变
  R8. 趋势级别下降（新增）
  R9. 均线死叉 EMA13下穿EMA34（新增）

推送渠道:
  - 邮件推送（QQ邮箱SMTP）
  - Windows桌面通知 (win10toast)
  - 控制台输出

运行方式:
  - 盘中每3分钟轮询（9:30-15:00）
  - 独立运行: python -m notify.alert_engine --loop
  - 集成到caopan_report.py --alert

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
import time
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 预警配置
ALERT_CONFIG = {
    "check_interval_min": 3,       # 检查间隔（分钟）V2.0: 5→3分钟更实时
    "trading_start": "09:30",      # 开盘时间
    "trading_end": "15:00",        # 收盘时间
    "lunch_start": "11:30",        # 午休开始
    "lunch_end": "13:00",          # 午休结束
    "deviation_overbought": 8.0,   # 超买乖离率阈值(%)
    "deviation_oversold": -8.0,    # 超卖乖离率阈值(%)
    "profit_ratio_drop": 0.10,     # 获利盘骤降阈值(10%)
    "stop_loss_pct": -0.08,        # 止损线(-8%)
    "alert_cooldown_min": 15,      # 同一标的预警冷却时间(分钟) V2.0: 30→15
    "alert_log_file": "alerts.json",  # 预警记录文件
    "trend_drop_alert": True,      # 趋势降级预警开关
}

# ============================================================
# 预警紧急度评分体系 (0-100分，分数越高越紧急)
# ============================================================
# 评分维度:
#   1. 资金损失风险(40%): 止损/跌停 > 趋势降级 > 乖离率
#   2. 时间紧迫性(30%): 盘中实时触发 > 盘后统计
#   3. 操作不可逆性(30%): 清仓信号 > 减仓信号 > 观望提示
#
# 评分参考表:
#   R6-跌停         = 95分 | 资金损失极大+不可逆+无法卖出
#   R5-触及止损线   = 90分 | 直接资金损失+必须立即操作
#   R9-均线死叉     = 85分 | 趋势反转确认+清仓信号
#   R1-DK死叉卖出  = 80分 | 卖出信号+需立即决策
#   R8-趋势降至≤2  = 75分 | 趋势恶化+需减仓/清仓
#   R8-趋势降至3   = 65分 | 趋势转弱+需警惕
#   R6-涨停         = 60分 | 利好但需决策是否止盈
#   R4-资金大幅流出 = 55分 | 主力出逃迹象
#   R7-获利盘骤降   = 50分 | 筹码松动
#   R2-跌破生命线   = 45分 | 支撑失效
#   R3-乖离率超买   = 40分 | 回调风险
#   R1-DK金叉买入  = 35分 | 买入机会(非紧急)
#   R3-乖离率超卖   = 30分 | 反弹机会(非紧急)
#   R2-站上生命线   = 25分 | 反转观察
# ============================================================


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

        # 按紧急度降序排列
        triggered.sort(key=lambda a: a.get("urgency_score", 0), reverse=True)

        # 推送
        if triggered:
            self._push_alerts(triggered)
            self._today_alerts.extend(triggered)
            self._save_history()

        return triggered

    def _check_single(self, r: dict) -> List[dict]:
        """检查单只标的的所有预警条件（V2.0: 每条预警带rule_name）"""
        alerts = []
        code = r.get("code", "")
        name = r.get("name", code)
        close = r.get("close", 0)

        # R1. DK信号触发
        dk = r.get("dk_signal")
        dk_strength = r.get("dk_strength", 0)
        dk_filtered = r.get("dk_filtered", False)
        if dk == "D" and dk_strength >= 50 and not dk_filtered:
            alerts.append({
                "type": "dk_buy",
                "rule_name": "R1-DK金叉买入信号",
                "rule_detail": f"EMA13上穿EMA34 + 三重共振确认(强度{dk_strength}分)",
                "level": "high",
                "icon": "🔴",
                "msg": f"{name} D点买入信号! 强度{dk_strength}分 | {r.get('dk_reason','')}",
                "urgency_score": 35,  # 买入机会，非紧急
            })
        elif dk == "K" and dk_strength >= 50 and not dk_filtered:
            alerts.append({
                "type": "dk_sell",
                "rule_name": "R1-DK死叉卖出信号",
                "rule_detail": f"EMA13下穿EMA34 + 空头确认(强度{dk_strength}分) | {r.get('dk_reason','')}",
                "level": "critical",
                "icon": "🟢",
                "msg": f"{name} K点卖出信号! 强度{dk_strength}分 | {r.get('dk_reason','')}",
                "urgency_score": 80,  # 卖出信号，需立即决策
            })

        # R2. 价格突破生命线
        ll_fast = r.get("ll_fast", 0)
        ll_slow = r.get("ll_slow", 0)
        if ll_fast > 0 and close > 0:
            if close < ll_fast * 0.99 and r.get("trend_level", 3) >= 4:
                alerts.append({
                    "type": "break_ll1",
                    "rule_name": "R2-跌破控盘生命线LL1",
                    "rule_detail": f"现价{close:.3f} < LL1(EMA13)={ll_fast:.3f}, 趋势{r.get('trend_level',3)}级",
                    "level": "warning",
                    "icon": "⚠️",
                    "msg": f"{name} 跌破LL1生命线! 现价{close:.3f} < LL1={ll_fast:.3f}",
                    "urgency_score": 45,  # 支撑失效
                })
            elif close > ll_fast * 1.01 and r.get("trend_level", 3) <= 2:
                alerts.append({
                    "type": "cross_ll1",
                    "rule_name": "R2-站上控盘生命线LL1",
                    "rule_detail": f"现价{close:.3f} > LL1(EMA13)={ll_fast:.3f}, 趋势可能反转",
                    "level": "info",
                    "icon": "📈",
                    "msg": f"{name} 站上LL1! 现价{close:.3f} > LL1={ll_fast:.3f} (趋势可能反转)",
                    "urgency_score": 25,  # 反转观察
                })

        # R3. 乖离率超买/超卖
        deviation = r.get("deviation_pct", 0)
        if deviation > self.cfg["deviation_overbought"]:
            alerts.append({
                "type": "overbought",
                "rule_name": "R3-乖离率超买",
                "rule_detail": f"乖离率{deviation:.1f}% > 阈值{self.cfg['deviation_overbought']}%, 股价偏离生命线过远",
                "level": "warning",
                "icon": "🔥",
                "msg": f"{name} 超买! 乖离率{deviation:.1f}% > {self.cfg['deviation_overbought']}%",
                "urgency_score": 40,  # 回调风险
            })
        elif deviation < self.cfg["deviation_oversold"]:
            alerts.append({
                "type": "oversold",
                "rule_name": "R3-乖离率超卖",
                "rule_detail": f"乖离率{deviation:.1f}% < 阈值{self.cfg['deviation_oversold']}%, 超跌反弹机会",
                "level": "info",
                "icon": "❄️",
                "msg": f"{name} 超卖! 乖离率{deviation:.1f}% < {self.cfg['deviation_oversold']}%",
                "urgency_score": 30,  # 反弹机会，非紧急
            })

        # R4. 板块资金异常流出
        fund_data = r.get("fund_data", {})
        if fund_data.get("score", 50) <= 25:
            alerts.append({
                "type": "fund_outflow",
                "rule_name": "R4-资金大幅流出",
                "rule_detail": f"资金评分{fund_data.get('score')}分(≤25分触发) | {fund_data.get('signal','')}",
                "level": "warning",
                "icon": "💸",
                "msg": f"{name} 资金大幅流出! 评分{fund_data.get('score')}分 | {fund_data.get('signal','')}",
                "urgency_score": 55,  # 主力出逃迹象
            })

        # R5. 止损位触及
        info = self.holdings.get(code, {})
        buy_price = info.get("cost", 0) or info.get("buy_price", 0)
        if buy_price > 0 and close > 0:
            pnl_pct = (close - buy_price) / buy_price
            if pnl_pct <= self.cfg["stop_loss_pct"]:
                alerts.append({
                    "type": "stop_loss",
                    "rule_name": "R5-触及止损线",
                    "rule_detail": f"浮亏{pnl_pct*100:.1f}% ≤ 止损线{self.cfg['stop_loss_pct']*100:.0f}% (成本{buy_price:.3f}→现价{close:.3f})",
                    "level": "critical",
                    "icon": "🚨",
                    "msg": f"{name} 触及止损线! 亏损{pnl_pct*100:.1f}% (成本{buy_price:.3f} 现价{close:.3f})",
                    "urgency_score": 90,  # 直接资金损失，必须立即操作
                })

        # R6. 涨停/跌停
        if close > 0:
            df = r.get("df_analyzed")
            if df is not None and len(df) >= 2:
                prev_close = df["close"].iloc[-2]
                if prev_close > 0:
                    change_pct = (close - prev_close) / prev_close * 100
                    if change_pct >= 9.8:
                        alerts.append({
                            "type": "limit_up",
                            "rule_name": "R6-涨停",
                            "rule_detail": f"涨幅{change_pct:.1f}% ≥ 9.8%",
                            "level": "info",
                            "icon": "🚀",
                            "msg": f"{name} 涨停! +{change_pct:.1f}%",
                            "urgency_score": 60,  # 利好但需决策是否止盈
                        })
                    elif change_pct <= -9.8:
                        alerts.append({
                            "type": "limit_down",
                            "rule_name": "R6-跌停",
                            "rule_detail": f"跌幅{change_pct:.1f}% ≤ -9.8%",
                            "level": "critical",
                            "icon": "💥",
                            "msg": f"{name} 跌停! {change_pct:.1f}%",
                            "urgency_score": 95,  # 资金损失极大+不可逆+无法卖出
                        })

        # R7. 筹码获利盘骤变
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
                        "rule_name": "R7-获利盘骤降",
                        "rule_detail": f"获利盘{prev_pr*100:.0f}%→{latest_pr*100:.0f}%, 降{drop*100:.0f}%(>{self.cfg['profit_ratio_drop']*100:.0f}%触发)",
                        "level": "warning",
                        "icon": "📉",
                        "msg": f"{name} 获利盘骤降{drop*100:.0f}%! ({prev_pr*100:.0f}%→{latest_pr*100:.0f}%) 主力可能出货",
                        "urgency_score": 50,  # 筹码松动
                    })

        # R8. 趋势级别下降（V2.0新增）
        if self.cfg.get("trend_drop_alert", True):
            df = r.get("df_analyzed")
            if df is not None and len(df) >= 2 and "trend_level" in df.columns:
                prev_trend = int(df["trend_level"].iloc[-2]) if not pd.isna(df["trend_level"].iloc[-2]) else 3
                curr_trend = r.get("trend_level", 3)
                if curr_trend < prev_trend:
                    trend_names = {5: "强上升", 4: "弱上升", 3: "震荡", 2: "弱下跌", 1: "强下跌"}
                    # 紧急度: 降至≤2级=75分(需清仓), 降至3级=65分(需警惕)
                    score = 75 if curr_trend <= 2 else 65
                    alerts.append({
                        "type": "trend_drop",
                        "rule_name": "R8-趋势级别下降",
                        "rule_detail": f"趋势从{prev_trend}级({trend_names.get(prev_trend,'')})降至{curr_trend}级({trend_names.get(curr_trend,'')})",
                        "level": "critical" if curr_trend <= 2 else "high",
                        "icon": "⚠️" if curr_trend >= 3 else "🚨",
                        "msg": f"{name} 趋势级别下降{prev_trend}→{curr_trend}级! {trend_names.get(prev_trend,'')}→{trend_names.get(curr_trend,'')}",
                        "urgency_score": score,
                    })

        # R9. 均线死叉 EMA13下穿EMA34（V2.0新增）
        df = r.get("df_analyzed")
        if df is not None and len(df) >= 2 and "ll_fast" in df.columns and "ll_slow" in df.columns:
            prev_fast = df["ll_fast"].iloc[-2] if not pd.isna(df["ll_fast"].iloc[-2]) else 0
            prev_slow = df["ll_slow"].iloc[-2] if not pd.isna(df["ll_slow"].iloc[-2]) else 0
            curr_fast = r.get("ll_fast", 0)
            curr_slow = r.get("ll_slow", 0)
            # 死叉: 前一日fast>=slow, 今日fast<slow
            if prev_fast >= prev_slow and curr_fast < curr_slow and curr_fast > 0:
                alerts.append({
                    "type": "ma_death_cross",
                    "rule_name": "R9-均线死叉(EMA13下穿EMA34)",
                    "rule_detail": f"EMA13={curr_fast:.3f}下穿EMA34={curr_slow:.3f} (前日EMA13={prev_fast:.3f}≥EMA34={prev_slow:.3f})",
                    "level": "critical",
                    "icon": "💥",
                    "msg": f"{name} 均线死叉! EMA13({curr_fast:.3f})下穿EMA34({curr_slow:.3f})",
                    "urgency_score": 85,  # 趋势反转确认，清仓信号
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
            level_icon = {"critical": "[!!!]", "high": "[!!]", "warning": "[!]", "info": "[i]"}.get(alert["level"], "")
            try:
                print(f"  {level_icon} [{alert['time']}] [{alert.get('rule_name','')}] {alert['msg']}")
            except UnicodeEncodeError:
                print(f"  {level_icon} [{alert['time']}] {alert.get('rule_name','')}")

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


# ============================================================
# 盘中独立预警循环 (V2.0新增)
# ============================================================

def send_alert_email(alerts: List[dict]):
    """发送预警邮件（带紧急度评分+规则名称+触发条件，按紧急度降序）"""
    from notify.email_notify import send_email

    now = datetime.datetime.now().strftime("%H:%M")
    today = datetime.date.today().strftime("%Y-%m-%d")
    critical = [a for a in alerts if a.get("level") in ("critical", "high")]
    if not critical:
        return

    # 按紧急度降序排列
    critical.sort(key=lambda a: a.get("urgency_score", 0), reverse=True)

    items = ""
    for a in critical:
        score = a.get('urgency_score', 0)
        # 紧急度颜色: >=80红色, >=60橙色, 其他黄色
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

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:15px;background:#f0f2f5;font-family:'Microsoft YaHei',Arial,sans-serif">
<div style="max-width:700px;margin:0 auto">
    <div style="background:linear-gradient(135deg,#cf1322,#ff4d4f);color:white;padding:18px 25px;border-radius:12px 12px 0 0">
        <h1 style="margin:0;font-size:20px">⚠️ 紧急预警 ({len(critical)}条)</h1>
        <div style="font-size:12px;opacity:0.8;margin-top:5px">{today} {now} | 操盘密码V3.0 | 盘中实时监控 | 按紧急度排序</div>
    </div>
    <div style="background:white;padding:20px 25px;border-radius:0 0 12px 12px;box-shadow:0 4px 15px rgba(0,0,0,0.08)">
        {items}
        <div style="text-align:center;color:#999;font-size:11px;margin-top:15px;padding-top:10px;border-top:1px solid #eee">
            请立即检查持仓，必要时手动干预 | 紧急度评分: 90+=立即操作 / 70-89=尽快处理 / 50-69=密切关注
        </div>
    </div>
</div>
</body></html>"""

    subject = f"[操盘密码] ⚠️紧急预警({len(critical)}条) | {today} {now}"
    send_email(subject, html)


def run_alert_loop(holdings: dict = None, interval_min: int = None):
    """
    盘中独立预警循环 (V2.0)

    每 N 分钟运行一次完整分析 + 预警检查，触发则即时发送邮件

    参数:
        holdings: 持仓字典
        interval_min: 检查间隔(分钟)，默认取ALERT_CONFIG
    """
    interval = interval_min or ALERT_CONFIG["check_interval_min"]

    print("=" * 55)
    print("  操盘密码 盘中实时预警 V2.0")
    print(f"  检查间隔: {interval}分钟")
    print(f"  交易时间: 09:30-11:30, 13:00-15:00")
    print(f"  预警规则: 9大规则(DK/生命线/乖离/资金/止损/涨跌停/筹码/趋势降级/均线死叉)")
    print("=" * 55)

    engine = AlertEngine(holdings=holdings)

    while True:
        if not is_trading_time():
            now_str = datetime.datetime.now().strftime("%H:%M")
            print(f"\n  [{now_str}] 非交易时间，等待中...")
            time.sleep(60)
            continue

        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"\n  [{now_str}] 执行预警检查...")

        try:
            # 获取数据并分析
            results = _fetch_and_analyze(holdings)
            if results:
                triggered = engine.check_alerts(results)
                if triggered:
                    critical = [a for a in triggered if a.get("level") in ("critical", "high")]
                    print(f"  🔔 触发 {len(triggered)} 条预警 (critical/high: {len(critical)}条)")
                    for a in triggered:
                        print(f"     {a.get('icon','')} [{a.get('rule_name','')}] {a.get('msg','')}")
                    # 即时发送邮件
                    if critical:
                        send_alert_email(critical)
                        print(f"  📧 预警邮件已发送")
                else:
                    print(f"  ✅ 无预警触发")
        except Exception as e:
            logger.error(f"预警检查异常: {e}")

        time.sleep(interval * 60)


def _fetch_and_analyze(holdings: dict = None) -> list:
    """获取数据并运行CaopanEngine分析"""
    from strategy.caopan_signal import CaopanEngine
    from data.realtime import fetch_realtime_batch

    # 加载持仓
    if not holdings:
        holdings_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "holdings.json")
        if os.path.exists(holdings_file):
            with open(holdings_file, "r", encoding="utf-8") as f:
                holdings = json.load(f)

    if not holdings:
        return []

    codes = list(holdings.keys())
    engine = CaopanEngine()
    results = []

    # 获取实时行情
    quotes = fetch_realtime_batch(codes)

    # FIX: 修复循环内每只股票重复baostock login/logout的性能问题，改为循环外一次login、循环后一次logout
    import baostock as bs
    try:
        lg = bs.login()
    except Exception as e:
        logger.warning(f"baostock login失败: {e}")

    for code in codes:
        try:
            prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"
            bs_code = f"{prefix}.{code}"
            end = datetime.date.today().strftime("%Y-%m-%d")
            start = (datetime.date.today() - datetime.timedelta(days=300)).strftime("%Y-%m-%d")
            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume,amount",
                start_date=start, end_date=end, frequency="d", adjustflag="2"
            )
            rows = []
            while rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())

            if not rows or len(rows) < 60:
                continue

            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            # 融合实时数据
            quote = quotes.get(code, {})
            if quote and quote.get("price", 0) > 0:
                today_str = datetime.date.today().strftime("%Y-%m-%d")
                if len(df) > 0 and df["date"].iloc[-1] == today_str:
                    df.iloc[-1, df.columns.get_loc("close")] = quote["price"]
                    df.iloc[-1, df.columns.get_loc("high")] = max(quote.get("high", 0), df["high"].iloc[-1])
                    df.iloc[-1, df.columns.get_loc("low")] = min(quote.get("low", 999), df["low"].iloc[-1])

            name = holdings[code].get("name", code) if isinstance(holdings[code], dict) else code
            result = engine.analyze(df, code=code, name=name)
            if "error" not in result:
                results.append(result)
        except Exception as e:
            logger.debug(f"分析{code}失败: {e}")

    try:
        bs.logout()
    except Exception:
        pass

    return results


if __name__ == "__main__":
    import argparse
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="操盘密码盘中实时预警")
    parser.add_argument("--loop", action="store_true", help="启动盘中循环监控")
    parser.add_argument("--once", action="store_true", help="单次检查")
    parser.add_argument("--interval", type=int, default=3, help="检查间隔(分钟)")
    args = parser.parse_args()

    if args.loop:
        run_alert_loop(interval_min=args.interval)
    elif args.once:
        results = _fetch_and_analyze()
        if results:
            engine = AlertEngine()
            triggered = engine.check_alerts(results)
            if triggered:
                print(f"\n触发 {len(triggered)} 条预警:")
                for a in triggered:
                    print(f"  {a.get('icon','')} [{a.get('rule_name','')}] {a.get('msg','')}")
                send_alert_email(triggered)
            else:
                print("\n无预警触发")
    else:
        run_alert_loop(interval_min=args.interval)
