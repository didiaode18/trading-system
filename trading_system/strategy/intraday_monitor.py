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

# FIX: 修复盘中监控实例状态全内存、进程重启导致重复邮件/重复条件单/缓冲计时重置的问题
# 状态持久化文件（按交易日key，跨日自动重置，参照alert_cooldown.json模式）
_INTRADAY_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "intraday_state.json"
)


class IntradayMonitor:
    """盘中实时监控器（V8.0: 梯度减仓+智能缓冲+变频监控增强版）"""

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

        # V7.2: 反洗盘保护配置（增强版）
        anti_wash = getattr(config, 'ANTI_WASH_CONFIG', {})
        self.BUFFER_MINUTES = anti_wash.get("buffer_minutes", 10)        # 触及止损后等待分钟
        self.BUFFER_CONFIRM_PCT = anti_wash.get("buffer_confirm_pct", 0.01)  # 确认阈值
        self.V_REVERSAL_PCT = anti_wash.get("v_reversal_pct", 0.03)    # V反保护阈值(3%→降低，更敏感)
        self.VOLUME_SPIKE_RATIO = anti_wash.get("volume_spike_ratio", 2.0)  # 放量判定
        self.VOLUME_SPIKE_EXTRA = anti_wash.get("volume_spike_extra", 0.03)  # 放量额外放宽
        self.SOFT_STOP_MODE = anti_wash.get("soft_stop_mode", True)    # 软止损模式
        # V7.2新增
        self.FALSE_BREAK_RECOVER_PCT = anti_wash.get("false_break_recover_pct", 0.005)  # 假突破收回阈值(0.5%)
        self.PASSIVE_DECLINE_MARKET = anti_wash.get("passive_decline_market", -1.0)  # 大盘跌>1%视为被动
        self.VOLUME_DECAY_RATIO = anti_wash.get("volume_decay_ratio", 0.6)  # 量能衰减判定

        # V8.0: 梯度减仓配置
        self.gradient_cfg = getattr(config, 'GRADIENT_REDUCE_CONFIG', {})
        self.gradient_triggered = {}  # {code: set()} 已触发的梯度级别

        # 批2-S3: ATR自适应档位观察缓存（日线14日ATR，每日每标的最多算一次，严禁轮询内拉数据）
        self._atr_cache = {"date": None, "values": {}}  # {"date": "YYYY-MM-DD", "values": {code: atr|None}}
        self._atr_obs_logged = set()        # {f"{date}_{code}_{level}"} ATR档位观察日志去重，避免刷屏
        self._sector_switch_logged = False  # 板块涨跌幅开关状态日志标记（仅记录一次，勿刷屏）

        # V8.0: 智能缓冲配置
        self.smart_buffer_cfg = getattr(config, 'SMART_BUFFER_CONFIG', {})

        # V8.0: 监控级别状态（供scheduler变频使用）
        self.alert_level = "normal"  # normal / warning / emergency
        self._clear_count = 0        # 连续无异常计数

        # V7.2: 状态跟踪
        self.stop_loss_first_touch = {}  # {code: datetime} 首次触及止损时间
        self.day_lows = {}               # {code: float} 当日最低价
        self.day_volumes = {}            # {code: float} 当日成交量
        self.stop_touch_prices = {}      # V7.2: {code: float} 触及止损时的价格（用于假突破检测）

        # V9.0: 看盘策略增强状态
        self.vwap_cfg = getattr(config, 'VWAP_CONFIG', {})
        self.orderbook_cfg = getattr(config, 'ORDERBOOK_CONFIG', {})
        self.phase_cfg = getattr(config, 'PHASE_CONFIG', {})
        self.vol_ratio_cfg = getattr(config, 'VOL_RATIO_CONFIG', {})
        self.vwap_below_count = {}       # {code: int} 连续低于VWAP计数
        self.prev_vwap_above = {}        # {code: bool} 上轮是否在VWAP上方
        self.prev_bid1_vol = {}          # {code: float} 上轮买一量（托盘撤退检测）
        self.opening_phase_done = False   # 开盘30分钟定性是否完成
        self.opening_prices = {}         # {code: [prices]} 开盘阶段价格记录
        self.closing_alerts_sent = set() # 尾盘预警已发送
        self.day_phase_label = {}        # {code: str} 当日定性标签
        self.price_history = {}          # {code: [(time, price)]} 分时价格序列(P2-3用)

        # FIX: 从持久化文件加载当日状态（盘中重启不重复报警/不重置缓冲计时）
        self._load_state()

    # ============================================================
    # 状态持久化（FIX: 参照alert_cooldown.json模式，失败仅debug不阻断）
    # ============================================================

    def _load_state(self):
        """FIX: 加载当日盘中监控状态（跨日自动重置为空状态，失败降级不阻断）"""
        try:
            if not os.path.exists(_INTRADAY_STATE_FILE):
                return
            with open(_INTRADAY_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") != datetime.date.today().isoformat():
                return  # 跨日自动清理：非当日状态不加载，下次保存时覆盖
            self.alerts_sent = set(data.get("alerts_sent", []))
            self.gradient_triggered = {
                c: set(v) for c, v in data.get("gradient_triggered", {}).items()
            }
            self.alert_level = data.get("alert_level", "normal")
            self._clear_count = int(data.get("clear_count", 0))
            for c, t in data.get("stop_loss_first_touch", {}).items():
                try:
                    self.stop_loss_first_touch[c] = datetime.datetime.fromisoformat(t)
                except (TypeError, ValueError):
                    continue
            self.opening_phase_done = bool(data.get("opening_phase_done", False))
            self.closing_alerts_sent = set(data.get("closing_alerts_sent", []))
            logger.info(f"[盘中监控] 已恢复当日状态: 已发预警{len(self.alerts_sent)}条, "
                        f"缓冲计时{len(self.stop_loss_first_touch)}只")
        except Exception as e:
            logger.debug(f"[盘中监控] 状态加载失败，使用空状态: {e}")

    def _save_state(self):
        """FIX: 当日状态落盘（按交易日key，失败仅debug不阻断监控）"""
        try:
            data = {
                "date": datetime.date.today().isoformat(),
                "alerts_sent": list(self.alerts_sent),
                "gradient_triggered": {c: list(v) for c, v in self.gradient_triggered.items()},
                "alert_level": self.alert_level,
                "clear_count": self._clear_count,
                "stop_loss_first_touch": {
                    c: t.isoformat() for c, t in self.stop_loss_first_touch.items()
                },
                "opening_phase_done": self.opening_phase_done,
                "closing_alerts_sent": list(self.closing_alerts_sent),
            }
            os.makedirs(os.path.dirname(_INTRADAY_STATE_FILE), exist_ok=True)
            with open(_INTRADAY_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[盘中监控] 状态持久化失败: {e}")

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
                # V4.0(G8): 非交易时段也写心跳（状态idle），供watchdog区分"线程存活但非盘中"与"线程已死"
                self._write_heartbeat("idle")
                # 非交易时间等待
                time.sleep(300)
                continue

            try:
                self._check_all()
            except Exception as e:
                logger.error(f"[盘中监控] 检查异常: {e}")

            # V4.0(G8): 每轮扫描后写心跳文件，watchdog据此检测监控静默失效
            self._write_heartbeat("active")

            time.sleep(self.poll_interval)

    def _write_heartbeat(self, status: str):
        """V4.0(G8): 写入监控心跳文件（时间戳|PID|状态），失败不影响主流程"""
        try:
            hb_path = os.path.join(config.PROJECT_ROOT, "output", ".monitor_heartbeat")
            os.makedirs(os.path.dirname(hb_path), exist_ok=True)
            with open(hb_path, "w") as f:
                f.write(f"{datetime.datetime.now().isoformat()}|{os.getpid()}|{status}")
        except Exception:
            pass

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
            # FIX P1-1: shares原仅在止损确认深分支赋值，止盈分支引用会NameError，循环顶部统一补齐
            shares = holding.get("shares", 0)

            # 更新价格记录
            self.last_prices[code] = {
                "price": current_price,
                "time": datetime.datetime.now().isoformat()
            }

            # V7.1: 更新当日最低价（用于V反保护）
            day_low = quote.get("low", current_price)
            if code not in self.day_lows or day_low < self.day_lows[code]:
                self.day_lows[code] = day_low

            # ---- 检查1: 止损位触发（V7.2: 反洗盘保护增强版）----
            if self.STOP_LOSS_ALERT and current_price <= stop_loss:
                now = datetime.datetime.now()

                # V7.2 保护1: 放量急跌检测（主力洗盘概率大）
                volume = quote.get("volume", 0)
                avg_volume = holding.get("avg_volume", 0)
                is_volume_spike = (avg_volume > 0 and volume > avg_volume * self.VOLUME_SPIKE_RATIO)

                if is_volume_spike:
                    # 放量急跌: 放宽止损线
                    adjusted_stop = stop_loss * (1 - self.VOLUME_SPIKE_EXTRA)
                    if current_price > adjusted_stop:
                        alert_key = f"{today}_{code}_wash_warning"
                        if alert_key not in self.alerts_sent:
                            alert = {
                                "level": "warning",
                                "type": "疑似洗盘",
                                "code": code,
                                "name": name,
                                "current_price": current_price,
                                "stop_loss": stop_loss,
                                "message": f"⚡ {name}({code}) 放量急跌触及止损，疑似主力洗盘 | "
                                          f"量比{volume/avg_volume:.1f}倍 | 已放宽止损至{adjusted_stop:.2f} | 建议观望",
                                "time": now.strftime("%H:%M:%S"),
                            }
                            alerts.append(alert)
                            self.alerts_sent.add(alert_key)
                            self._send_alert(alert)
                        continue  # 不触发止损

                # V7.2 保护2: V型反转保护（阈值降低到3%更敏感）
                if code in self.day_lows and self.day_lows[code] > 0:
                    rebound_pct = (current_price - self.day_lows[code]) / self.day_lows[code]
                    if rebound_pct > self.V_REVERSAL_PCT:
                        alert_key = f"{today}_{code}_v_reversal"
                        if alert_key not in self.alerts_sent:
                            alert = {
                                "level": "info",
                                "type": "V反保护",
                                "code": code,
                                "name": name,
                                "current_price": current_price,
                                "message": f"🔄 {name}({code}) 从日内低点{self.day_lows[code]:.2f}反弹{rebound_pct:.1%} | "
                                          f"V反保护启动，暂停止损触发",
                                "time": now.strftime("%H:%M:%S"),
                            }
                            alerts.append(alert)
                            self.alerts_sent.add(alert_key)
                            self._send_alert(alert)
                        # 重置首次触及时间
                        self.stop_loss_first_touch.pop(code, None)
                        continue  # 不触发止损

                # V7.2 保护3: 假突破检测（触及止损后快速收回）
                if code in self.stop_touch_prices:
                    touch_price = self.stop_touch_prices[code]
                    # 当前价已收回止损上方 → 假突破确认
                    if current_price > stop_loss * (1 + self.FALSE_BREAK_RECOVER_PCT):
                        alert_key = f"{today}_{code}_false_break"
                        if alert_key not in self.alerts_sent:
                            alert = {
                                "level": "info",
                                "type": "假突破确认(洗盘)",
                                "code": code,
                                "name": name,
                                "current_price": current_price,
                                "stop_loss": stop_loss,
                                "message": f"🛡️ {name}({code}) 止损假突破! "
                                          f"触及{touch_price:.2f}后已收回{current_price:.2f}"
                                          f"(>止损{stop_loss:.2f}+0.5%) | 洗盘特征，不执行止损",
                                "time": now.strftime("%H:%M:%S"),
                            }
                            alerts.append(alert)
                            self.alerts_sent.add(alert_key)
                            self._send_alert(alert)
                        self.stop_loss_first_touch.pop(code, None)
                        self.stop_touch_prices.pop(code, None)
                        continue  # 不触发止损

                # V7.2 保护4: 被动跌破检测（大盘/板块整体回调带动）
                if market_quote:
                    market_chg = market_quote.get("change_pct", 0)
                    if market_chg < self.PASSIVE_DECLINE_MARKET:
                        # 大盘跌>1%，个股被动跌破止损
                        stock_chg = quote.get("change_pct", 0)
                        relative_strength = stock_chg - market_chg
                        if relative_strength > -1.0:  # 个股跌幅不超过大盘1%
                            alert_key = f"{today}_{code}_passive_decline"
                            if alert_key not in self.alerts_sent:
                                alert = {
                                    "level": "warning",
                                    "type": "被动跌破(大盘联动)",
                                    "code": code,
                                    "name": name,
                                    "current_price": current_price,
                                    "stop_loss": stop_loss,
                                    "message": f"📉 {name}({code}) 被动跌破止损 | "
                                              f"大盘{market_chg:.1f}%，个股相对强度{relative_strength:+.1f}% | "
                                              f"非个股自身问题，延长缓冲期观察",
                                    "time": now.strftime("%H:%M:%S"),
                                }
                                alerts.append(alert)
                                self.alerts_sent.add(alert_key)
                                self._send_alert(alert)
                            # 被动跌破: 延长缓冲期×1.5
                            if code not in self.stop_loss_first_touch:
                                self.stop_loss_first_touch[code] = now
                                self.stop_touch_prices[code] = current_price
                            continue  # 不立即触发，进入缓冲

                # V8.0 保护5: 智能缓冲确认机制（P0-3: 真跌3分钟/洗盘15分钟）
                if code not in self.stop_loss_first_touch:
                    # 首次触及: 记录时间，发出预警
                    self.stop_loss_first_touch[code] = now
                    self.stop_touch_prices[code] = current_price
                    # V8.0: 动态缓冲时间
                    dynamic_buffer = self._get_dynamic_buffer_minutes(
                        code, quote, holding, market_quote
                    )
                    alert_key = f"{today}_{code}_stop_pending"
                    if alert_key not in self.alerts_sent:
                        scenario = self._classify_decline_scenario(
                            code, quote, holding, market_quote
                        )
                        scenario_label = {"true_decline": "真跌", "wash_trading": "洗盘", "unknown": "待判定"}
                        alert = {
                            "level": "warning",
                            "type": "止损预警(待确认)",
                            "code": code,
                            "name": name,
                            "current_price": current_price,
                            "stop_loss": stop_loss,
                            "buy_price": buy_price,
                            "loss_pct": round((current_price / buy_price - 1) * 100, 2),
                            "message": f"⚠️ {name}({code}) 触及止损位！"
                                      f"现价{current_price:.2f} ≤ 止损{stop_loss:.2f} | "
                                      f"场景判定:{scenario_label.get(scenario, scenario)} | "
                                      f"等待{dynamic_buffer:.0f}分钟确认 | "
                                      f"{'软止损模式:仅预警' if self.SOFT_STOP_MODE else '硬止损模式'}",
                            "time": now.strftime("%H:%M:%S"),
                        }
                        alerts.append(alert)
                        self.alerts_sent.add(alert_key)
                        self._send_alert(alert)
                    continue  # 等待缓冲期

                # 检查缓冲期是否到期（V8.0: 动态缓冲）
                first_touch = self.stop_loss_first_touch[code]
                elapsed_minutes = (now - first_touch).total_seconds() / 60
                dynamic_buffer = self._get_dynamic_buffer_minutes(
                    code, quote, holding, market_quote
                )

                if elapsed_minutes >= dynamic_buffer:
                    # 缓冲期到: 确认止损
                    confirm_price = stop_loss * (1 - self.BUFFER_CONFIRM_PCT)
                    if current_price <= confirm_price:
                        alert_key = f"{today}_{code}_stop_confirmed"
                        if alert_key not in self.alerts_sent:
                            shares = holding.get("shares", 0)
                            alert = {
                                "level": "critical",
                                "type": "止损确认",
                                "code": code,
                                "name": name,
                                "current_price": current_price,
                                "stop_loss": stop_loss,
                                "buy_price": buy_price,
                                # 批2-S3: 结构化标记（纯增量字段，供下游执行层识别止损类信号）
                                "is_stop_signal": True,
                                "stop_price": stop_loss,
                                "loss_pct": round((current_price / buy_price - 1) * 100, 2),
                                "buffer_minutes": round(elapsed_minutes, 1),
                                "message": f"🚨 {name}({code}) 止损确认！"
                                          f"现价{current_price:.2f} 持续低于止损{stop_loss:.2f} "
                                          f"超过{elapsed_minutes:.0f}分钟 | "
                                          f"浮亏{(current_price/buy_price-1)*100:.1f}% | "
                                          f"建议卖出{shares}股清仓 | "
                                          f"条件单已生成，请立即在APP执行",
                                "time": now.strftime("%H:%M:%S"),
                            }
                            alerts.append(alert)
                            self.alerts_sent.add(alert_key)
                            self._send_alert(alert)
                            # V8.0 P1-3: 止损确认时自动生成卖出条件单+紧急邮件
                            self._generate_reduce_condition_order(
                                code, name, current_price, shares,
                                f"盘中止损确认(持续{elapsed_minutes:.0f}分钟低于止损线)"
                            )
                            self._send_stop_loss_email(alert)
                    else:
                        # 价格回升，重置缓冲
                        self.stop_loss_first_touch.pop(code, None)
                        self.stop_touch_prices.pop(code, None)
                else:
                    # 缓冲期中: 发出进度提示
                    remaining = dynamic_buffer - elapsed_minutes
                    alert_key = f"{today}_{code}_stop_buffering_{int(elapsed_minutes // 5)}"
                    if alert_key not in self.alerts_sent:
                        alert = {
                            "level": "info",
                            "type": "止损缓冲中",
                            "code": code,
                            "name": name,
                            "current_price": current_price,
                            "message": f"⏳ {name}({code}) 止损缓冲中，还需{remaining:.0f}分钟确认 | "
                                      f"现价{current_price:.2f}",
                            "time": now.strftime("%H:%M:%S"),
                        }
                        alerts.append(alert)
                        self.alerts_sent.add(alert_key)
            else:
                # 价格回升到止损线上方: 重置缓冲状态
                if code in self.stop_loss_first_touch:
                    # V7.2: 价格收回止损上方，确认假突破
                    if code in self.stop_touch_prices:
                        alert_key = f"{today}_{code}_recovered"
                        if alert_key not in self.alerts_sent:
                            alert = {
                                "level": "info",
                                "type": "止损解除(收回)",
                                "code": code,
                                "name": name,
                                "current_price": current_price,
                                "stop_loss": stop_loss,
                                "message": f"✅ {name}({code}) 已收回止损上方! "
                                          f"现价{current_price:.2f} > 止损{stop_loss:.2f} | 止损解除",
                                "time": datetime.datetime.now().strftime("%H:%M:%S"),
                            }
                            alerts.append(alert)
                            self.alerts_sent.add(alert_key)
                            self._send_alert(alert)
                self.stop_loss_first_touch.pop(code, None)
                self.stop_touch_prices.pop(code, None)

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

            # ---- 检查3: 止盈位到达（V3.3: 阶梯止盈+回落止盈）----
            if self.PROFIT_TARGET_ALERT and buy_price > 0:
                profit_pct = (current_price / buy_price - 1)
                highest = holding.get("highest", buy_price)
                drawdown_from_high = (current_price - highest) / highest if highest > 0 else 0

                # 第一止盈位: 浮盈≥10%（V6.0: 8%→10%）
                if profit_pct >= 0.10:
                    alert_key = f"{today}_{code}_profit_10"
                    if alert_key not in self.alerts_sent:
                        # FIX: 修复100股持仓减1/3取整为0的问题（建议卖出量不超持有股数）
                        sell_1_3 = min(shares, max(100, int(shares / 3 / 100) * 100)) if shares >= 100 else shares
                        alert = {
                            "level": "info",
                            "type": "止盈提醒",
                            "code": code,
                            "name": name,
                            "current_price": current_price,
                            "profit_pct": round(profit_pct * 100, 2),
                            "message": f"🎯 {name}({code}) 浮盈{profit_pct*100:.1f}%达第一止盈位(10%)！"
                                      f"建议卖出{sell_1_3}股(1/3)锁定利润",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                        }
                        alerts.append(alert)
                        self.alerts_sent.add(alert_key)
                        self._send_alert(alert)

                # 第二止盈位: 浮盈≥20%
                if profit_pct >= 0.20:
                    alert_key = f"{today}_{code}_profit_20"
                    if alert_key not in self.alerts_sent:
                        # FIX: 修复100股持仓减1/3取整为0的问题（建议卖出量不超持有股数）
                        sell_1_3 = min(shares, max(100, int(shares / 3 / 100) * 100)) if shares >= 100 else shares
                        alert = {
                            "level": "warning",
                            "type": "止盈提醒",
                            "code": code,
                            "name": name,
                            "current_price": current_price,
                            "profit_pct": round(profit_pct * 100, 2),
                            "message": f"🎯 {name}({code}) 浮盈{profit_pct*100:.1f}%达第二止盈位(20%)！"
                                      f"建议再卖{sell_1_3}股(1/3)，已锁定大部分利润",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                        }
                        alerts.append(alert)
                        self.alerts_sent.add(alert_key)
                        self._send_alert(alert)

                # 回落止盈: 浮盈>5%且从最高点回落超过阈值
                if profit_pct >= 0.05:
                    from config import DRAWDOWN_STOP
                    stock_type = holding.get("stock_type", "龙头稳健")
                    dd_threshold = DRAWDOWN_STOP.get(stock_type, DRAWDOWN_STOP.get("龙头稳健", 0.07))
                    if drawdown_from_high <= -dd_threshold:
                        alert_key = f"{today}_{code}_drawdown_profit"
                        if alert_key not in self.alerts_sent:
                            # FIX: 修复100股持仓减半取整为0的问题（建议卖出量不超持有股数）
                            sell_half = min(shares, max(100, int(shares * 0.5 / 100) * 100)) if shares >= 100 else shares
                            alert = {
                                "level": "warning",
                                "type": "回落止盈",
                                "code": code,
                                "name": name,
                                "current_price": current_price,
                                "profit_pct": round(profit_pct * 100, 2),
                                "drawdown_pct": round(drawdown_from_high * 100, 2),
                                "message": f" {name}({code}) 利润回落! 浮盈{profit_pct*100:.1f}%"
                                          f"但从最高{highest:.2f}回落{drawdown_from_high*100:.1f}%"
                                          f"(>{dd_threshold*100:.0f}%)，建议卖出{sell_half}股(50%)保住利润",
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

            # ---- 检查5: V8.0梯度减仓（P0-1）----
            gradient_alerts = self._check_gradient_reduction(code, holding, current_price, quote)
            alerts.extend(gradient_alerts)

            # ---- 检查6: V9.0 VWAP均价线监控（P0-1）----
            vwap_alerts = self._check_vwap(code, name, current_price, quote, today)
            alerts.extend(vwap_alerts)

            # ---- 检查7: V9.0 盘口强弱监控（P0-2）----
            ob_alerts = self._check_orderbook(code, name, current_price, quote, holding, today)
            alerts.extend(ob_alerts)

            # ---- 检查8: V9.0 量比异动（P1-1）----
            vr_alerts = self._check_volume_ratio(code, name, current_price, quote, holding, today)
            alerts.extend(vr_alerts)

            # ---- 检查8.5: V3.2 换手率异常+量价背离（知识库规则落地）----
            to_alerts = self._check_turnover_divergence(code, name, current_price, quote, holding, today)
            alerts.extend(to_alerts)

            # ---- V9.0: 记录分时价格序列（P2-3用）----
            if code not in self.price_history:
                self.price_history[code] = []
            self.price_history[code].append(
                (datetime.datetime.now().strftime("%H:%M:%S"), current_price)
            )

        # ---- 检查9: V9.0 开盘30分钟定性（P0-4）----
        phase_alerts = self._check_opening_phase(quotes, today)
        alerts.extend(phase_alerts)

        # ---- 检查10: V9.0 尾盘异动检测（P0-4）----
        closing_alerts = self._check_closing_phase(quotes, today)
        alerts.extend(closing_alerts)

        # ---- 检查11: V9.0 盘口快照变化追踪（P2-2）----
        for code, quote in quotes.items():
            if code == "000300":
                continue
            name = quote.get("name", code)
            snapshot_alerts = self.check_orderbook_snapshot_change(code, name, quote, today)
            alerts.extend(snapshot_alerts)

        # ---- 检查12: V9.0 分时形态识别（P2-3）----
        for code, quote in quotes.items():
            if code == "000300":
                continue
            name = quote.get("name", code)
            pattern_alerts = self.detect_intraday_pattern(code, name, today)
            alerts.extend(pattern_alerts)

        # ---- 检查13: 大盘急跌 ----
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

        # ---- V8.0: 更新监控级别（供scheduler变频使用）----
        self._update_alert_level(quotes, market_quote)

        # FIX: 每轮检查后状态落盘（盘中重启不重复报警/不重置缓冲计时）
        self._save_state()

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
                    "prev_close": info.get("prev_close", 0),
                    # FIX: 修复放量急跌检测永远False的问题，添加缺失的volume字段
                    "volume": info.get("volume", 0),
                    "amount": info.get("amount", 0),
                    "turnover": info.get("turnover", 0),  # V3.2: 换手率(%)
                    # V9.0: 看盘增强字段
                    "vwap": info.get("vwap", 0),
                    "outer_vol": info.get("outer_vol", 0),
                    "inner_vol": info.get("inner_vol", 0),
                    "bid1_price": info.get("bid1_price", 0),
                    "bid1_vol": info.get("bid1_vol", 0),
                    "ask1_price": info.get("ask1_price", 0),
                    "ask1_vol": info.get("ask1_vol", 0),
                    "total_bid_vol": info.get("total_bid_vol", 0),
                    "total_ask_vol": info.get("total_ask_vol", 0),
                    "order_ratio": info.get("order_ratio", 0),
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

    def _send_stop_loss_email(self, alert: dict):
        """V8.0 P1-3: 止损确认紧急邮件（不等收盘，盘中直接发送）"""
        try:
            if not (config.EMAIL_SENDER and config.EMAIL_AUTH_CODE):
                return
            from notify.email_notify import send_email
            name = alert.get('name', '')
            code = alert.get('code', '')
            subject = f"[紧急止损] {name}({code}) 盘中止损确认 {alert.get('time', '')}"
            html = f"""
            <div style="font-family:Microsoft YaHei;padding:20px">
                <h2 style="color:#FF4D4F">🚨 盘中止损确认 - 请立即执行</h2>
                <p style="font-size:16px;color:#333">{alert.get('message', '')}</p>
                <table style="border-collapse:collapse;margin:15px 0;width:100%">
                    <tr><td style="padding:8px 15px;border:1px solid #eee;background:#f5f5f5"><b>股票</b></td>
                        <td style="padding:8px 15px;border:1px solid #eee">{name} ({code})</td></tr>
                    <tr><td style="padding:8px 15px;border:1px solid #eee;background:#f5f5f5"><b>现价</b></td>
                        <td style="padding:8px 15px;border:1px solid #eee;color:#FF4D4F;font-weight:bold">{alert.get('current_price', '')}</td></tr>
                    <tr><td style="padding:8px 15px;border:1px solid #eee;background:#f5f5f5"><b>止损线</b></td>
                        <td style="padding:8px 15px;border:1px solid #eee">{alert.get('stop_loss', '')}</td></tr>
                    <tr><td style="padding:8px 15px;border:1px solid #eee;background:#f5f5f5"><b>浮亏</b></td>
                        <td style="padding:8px 15px;border:1px solid #eee;color:#FF4D4F">{alert.get('loss_pct', '')}%</td></tr>
                    <tr><td style="padding:8px 15px;border:1px solid #eee;background:#f5f5f5"><b>时间</b></td>
                        <td style="padding:8px 15px;border:1px solid #eee">{alert.get('time', '')}</td></tr>
                </table>
                <p style="color:#FF4D4F;font-size:14px;font-weight:bold">
                    ❗ 条件单已生成到 output/gradient_reduce_今日.json，请立即打开东方财富APP挂单执行！
                </p>
                <p style="color:#999;font-size:12px">此邮件由盘中监控系统自动发送（V8.0实时止损）</p>
            </div>"""
            send_email(subject, html)
            logger.info(f"[止损邮件] 已发送: {name}({code})")
        except Exception as e:
            logger.error(f"[止损邮件] 发送失败: {e}")

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
    # 六、V8.0 梯度减仓预警（P0-1）
    # ============================================================

    def _check_gradient_reduction(self, code: str, holding: dict,
                                   current_price: float, quote: dict) -> list:
        """
        检查浮亏梯度减仓（V8.0 P0-1）

        规则:
          浮亏-5% → 预警+减仓1/3条件单
          浮亏-8% → 紧急预警+减仓1/2条件单
          浮亏-10% → 清仓预警+全部卖出条件单
          盘中放量跌>5% → 不等收盘，立即减仓1/2
        """
        alerts = []
        if not self.gradient_cfg:
            return alerts

        buy_price = holding.get("buy_price", 0)
        if buy_price <= 0 or current_price <= 0:
            return alerts

        name = holding.get("name", code)
        shares = holding.get("shares", 0)
        loss_pct = (current_price - buy_price) / buy_price
        today = datetime.date.today().isoformat()

        # 初始化触发记录
        if code not in self.gradient_triggered:
            self.gradient_triggered[code] = set()

        # FIX(2026-08-12): 深亏模式 —— 浮亏远超清仓预警线(默认-30%)时成本已无止损纪律意义，
        # 三档齐发会每天重复"清仓建议+3张超量条件单"轰炸；合并为每日1条汇总提醒且不生成条件单，
        # 可在holdings.json对该标的设 "ack_deep_loss": true 完全豁免提醒
        change_pct = quote.get("change_pct", 0) or 0
        deep_cfg = self.gradient_cfg.get("deep_loss", {})
        if (deep_cfg.get("enabled", True) and loss_pct <= deep_cfg.get("threshold", -0.30)
                and not holding.get("ack_deep_loss")):
            dl_key = f"{today}_{code}_deep_loss_digest"
            if dl_key not in self.alerts_sent:
                self.alerts_sent.add(dl_key)
                # 标记全部档位已触发，防止降级开关变动后当日再走正常三档路径
                self.gradient_triggered[code].update(
                    lv["level"] for lv in self.gradient_cfg.get("levels", []))
                alert = {
                    "level": "warning",
                    "type": "深亏标的每日提醒",
                    "code": code,
                    "name": name,
                    "current_price": current_price,
                    "buy_price": buy_price,
                    "loss_pct": round(loss_pct * 100, 2),
                    "message": f"🕳️ {name}({code}) 深亏{loss_pct*100:.1f}% "
                              f"(成本{buy_price:.2f} 现价{current_price:.2f}) | "
                              f"今日{change_pct:+.1f}% | "
                              f"深亏模式: 已远超清仓预警线，不再重复发清仓建议/条件单，"
                              f"每日仅提醒1次，请结合当日走势自主决策处置",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self._send_alert(alert)
            return alerts

        # 批2-S3: ATR自适应档位观察模式（每日每标的仅从本地日线算一次ATR，查缓存为主）
        atr_log_only = getattr(config, "ATR_ADAPTIVE_STOP_LOG_ONLY", True)
        atr_val = self._get_atr_cached(code)

        # FIX(2026-08-12): 累计减仓股数 —— 同日多档齐发时各档均按全仓计算，
        # 三张条件单合计会超持仓量（如6000股生成11000股卖单），需累计封顶
        cum_reduce = 0

        # 梯度级别检查
        for level_cfg in self.gradient_cfg.get("levels", []):
            threshold = level_cfg["loss_pct"]
            level_name = level_cfg["level"]
            reduce_ratio = level_cfg["reduce_ratio"]
            label = level_cfg["label"]

            # ATR档位建议值（小数形式，如 0.05=5%；算不到ATR时保持固定档不变）
            atr_suggest = None
            if atr_val is not None:
                try:
                    from risk.risk_control import suggest_atr_threshold
                    atr_suggest = suggest_atr_threshold(current_price, atr_val, abs(threshold))
                except Exception as e:
                    logger.debug(f"[ATR档位] {code} 建议值计算失败: {e}")

            if atr_log_only:
                # 观察模式（默认）: 判断逻辑完全不变，仅记录固定档与ATR建议的对比；
                # 只在ATR建议值与固定档不同且当日未记录过时输出一行，避免刷屏
                if (atr_suggest is not None and
                        abs(atr_suggest - abs(threshold)) > 1e-9):
                    obs_key = f"{today}_{code}_{level_name}"
                    if obs_key not in self._atr_obs_logged:
                        self._atr_obs_logged.add(obs_key)
                        logger.info(
                            f"ATR档位观察: {code} 固定档{threshold*100:.1f}% "
                            f"vs ATR建议-{atr_suggest*100:.1f}%（观察模式，判断逻辑不变）"
                        )
            else:
                # 观察期结束后启用: 档位生效值取更宽者（保持负号语义），本次默认行为不变
                if atr_suggest is not None:
                    threshold = min(threshold, -atr_suggest)

            if loss_pct <= threshold and level_name not in self.gradient_triggered[code]:
                self.gradient_triggered[code].add(level_name)
                reduce_shares = int(shares * reduce_ratio / 100) * 100  # 整百股
                if reduce_shares < 100:
                    reduce_shares = min(shares, 100)
                # FIX(2026-08-12): 按剩余可减股数封顶，保证同日多档条件单合计不超持仓
                reduce_shares = min(reduce_shares, shares - cum_reduce)
                if reduce_shares <= 0:
                    continue
                cum_reduce += reduce_shares

                alert = {
                    "level": level_name,
                    "type": f"梯度减仓-{label}",
                    "code": code,
                    "name": name,
                    "current_price": current_price,
                    "buy_price": buy_price,
                    "loss_pct": round(loss_pct * 100, 2),
                    "reduce_shares": reduce_shares,
                    "reduce_ratio": round(reduce_ratio * 100),
                    # 批2-S3: 结构化标记；预警上下文无现成止损价，取现价×0.97作为参考价
                    "is_stop_signal": True,
                    "stop_price": round(current_price * 0.97, 2),
                    "message": f"🚨 {name}({code}) {label}！"
                              f"现价{current_price:.2f} 成本{buy_price:.2f} "
                              f"浮亏{loss_pct*100:.1f}% | 今日{change_pct:+.1f}% | "
                              f"建议卖出{reduce_shares}股({reduce_ratio*100:.0f}%仓位) | "
                              f"请立即在东方财富APP挂单执行",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(f"{today}_{code}_gradient_{level_name}")
                self._send_alert(alert)

                # 自动生成条件单
                if self.gradient_cfg.get("generate_condition_order", True):
                    self._generate_reduce_condition_order(
                        code, name, current_price, reduce_shares, label
                    )

        # 盘中放量暴跌检查（不等收盘）
        crash_pct = self.gradient_cfg.get("intraday_crash_pct", -0.05)
        crash_vol = self.gradient_cfg.get("intraday_crash_vol_ratio", 2.0)
        change_pct = quote.get("change_pct", 0)
        volume = quote.get("volume", 0)
        avg_volume = holding.get("avg_volume", 0)

        crash_key = f"{today}_{code}_intraday_crash"
        if (change_pct and change_pct < crash_pct * 100 and
                avg_volume > 0 and volume > avg_volume * crash_vol and
                crash_key not in self.alerts_sent):
            reduce_shares = int(shares * 0.5 / 100) * 100
            if reduce_shares < 100:
                reduce_shares = min(shares, 100)
            alert = {
                "level": "emergency",
                "type": "盘中放量暴跌-紧急减仓",
                "code": code,
                "name": name,
                "current_price": current_price,
                "change_pct": change_pct,
                "reduce_shares": reduce_shares,
                # 批2-S3: 结构化标记；预警上下文无现成止损价，取现价×0.97作为参考价
                "is_stop_signal": True,
                "stop_price": round(current_price * 0.97, 2),
                "message": f"⚡ {name}({code}) 盘中放量暴跌{change_pct:.1f}%！"
                          f"量比{volume/avg_volume:.1f}倍 | "
                          f"不等收盘，建议立即减仓{reduce_shares}股(50%)",
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
            }
            alerts.append(alert)
            self.alerts_sent.add(crash_key)
            self._send_alert(alert)
            if self.gradient_cfg.get("generate_condition_order", True):
                self._generate_reduce_condition_order(
                    code, name, current_price, reduce_shares, "盘中放量暴跌紧急减仓"
                )

        return alerts

    def _get_atr_cached(self, code: str):
        """
        批2-S3: 获取标的日线14日ATR（实例级缓存，每日每标的最多从本地SQLite算一次）

        缓存结构: {"date": "YYYY-MM-DD", "values": {code: atr|None}}
        跨日自动失效；算不到（数据不足/异常）记 None 且当日不再重试；绝不抛异常。
        """
        try:
            today = datetime.date.today().isoformat()
            if self._atr_cache.get("date") != today:
                self._atr_cache = {"date": today, "values": {}}
            values = self._atr_cache["values"]
            if code in values:
                return values[code]
            atr = None
            try:
                from data.data_loader import load_daily_data  # 本地SQLite读，无网络请求
                df = load_daily_data(code, days=30)
                # shift(1)+rolling(14) 至少需要15根日线
                if df is not None and len(df) >= 15:
                    high_low = df["high"] - df["low"]
                    high_close = (df["high"] - df["close"].shift(1)).abs()
                    low_close = (df["low"] - df["close"].shift(1)).abs()
                    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                    last = tr.rolling(14).mean().iloc[-1]
                    if pd.notna(last) and last > 0:
                        atr = float(last)
            except Exception as e:
                logger.debug(f"[ATR缓存] {code} 日线ATR计算失败: {e}")
                atr = None
            values[code] = atr
            return atr
        except Exception:
            return None

    def _generate_reduce_condition_order(self, code: str, name: str,
                                          price: float, shares: int, reason: str):
        """生成东方财富条件单（减仓卖出）"""
        try:
            output_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                self.gradient_cfg.get("order_output_dir", "output")
            )
            os.makedirs(output_dir, exist_ok=True)
            today = datetime.date.today().isoformat()
            order_file = os.path.join(output_dir, f"gradient_reduce_{today}.json")

            # 读取已有订单
            orders = []
            if os.path.exists(order_file):
                with open(order_file, "r", encoding="utf-8") as f:
                    orders = json.load(f)

            # 避免重复
            for o in orders:
                if o.get("证券代码") == code and o.get("数量") == shares:
                    return

            # 卖出价: 现价下方0.5%确保成交
            sell_price = round(price * 0.995, 2)
            orders.append({
                "证券代码": code,
                "证券名称": name,
                "方向": "卖出",
                "触发价": sell_price,
                "数量": shares,
                "类型": "定价卖出",
                "有效期": "1个交易日",
                "触发时间": "盘中实时",
                "优先级": "★★★必挂",
                "说明": f"[梯度减仓自动触发] {reason}",
                "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

            with open(order_file, "w", encoding="utf-8") as f:
                json.dump(orders, f, ensure_ascii=False, indent=2)
            logger.info(f"[梯度减仓] 条件单已生成: {name}({code}) 卖出{shares}股@{sell_price} → {order_file}")
        except Exception as e:
            logger.error(f"[梯度减仓] 条件单生成失败: {e}")

    # ============================================================
    # 七、V8.0 智能缓冲分场景（P0-3）
    # ============================================================

    def _classify_decline_scenario(self, code: str, quote: dict,
                                    holding: dict, market_quote: dict) -> str:
        """
        判定下跌场景: "true_decline" / "wash_trading" / "unknown"

        真跌特征: 缩量+均线空头+大盘弱+板块联动+连续多日
        洗盘特征: 放量急跌+快速收回+大盘稳+板块未联动
        """
        cfg = self.smart_buffer_cfg
        if not cfg:
            return "unknown"

        # --- 真跌条件计数 ---
        true_score = 0
        volume = quote.get("volume", 0)
        avg_volume = holding.get("avg_volume", 0)
        change_pct = quote.get("change_pct", 0)

        # 1. 缩量下跌
        if avg_volume > 0 and volume < avg_volume * 0.7:
            true_score += 1
        # 2. 均线空头（用价格与止损线关系近似判断）
        buy_price = holding.get("buy_price", 0)
        current_price = quote.get("price", 0)
        if buy_price > 0 and current_price < buy_price * 0.95:
            true_score += 1
        # 3. 大盘走弱
        if market_quote:
            mkt_chg = market_quote.get("change_pct", 0)
            if mkt_chg < -0.5:
                true_score += 1
        # 4. 板块联动（批2-S3: 开关开启时用东财板块真实涨跌幅模糊匹配赛道，
        #    未命中/失败/开关关闭一律回退大盘近似）
        sector_chg = self._get_sector_change_pct(code, holding)
        if sector_chg is not None:
            if sector_chg < -1.0:
                true_score += 1
        elif market_quote and market_quote.get("change_pct", 0) < -1.0:
            # 回退: 大盘跌>1%，大概率板块联动
            true_score += 1
        # 5. 连续多日下跌（用浮亏深度近似）
        if buy_price > 0 and current_price > 0:
            loss_pct = (current_price - buy_price) / buy_price
            if loss_pct < -0.05:  # 浮亏>5%说明不是今天才开始跌
                true_score += 1
        # V9.0: 6. 盘口抛压重（外盘占比<35%）
        outer = quote.get("outer_vol", 0)
        inner = quote.get("inner_vol", 0)
        if outer + inner > 0:
            outer_ratio = outer / (outer + inner)
            if outer_ratio < 0.35:
                true_score += 1
        # V9.0: 7. VWAP压制（价格持续低于均价线）
        vwap = quote.get("vwap", 0)
        if vwap > 0 and current_price < vwap * 0.99:
            true_score += 1

        min_true = cfg.get("true_decline", {}).get("min_conditions", 3)
        if true_score >= min_true:
            return "true_decline"

        # --- 洗盘条件计数 ---
        wash_score = 0
        # 1. 放量急跌
        if avg_volume > 0 and volume > avg_volume * 2.0:
            wash_score += 1
        # 2. 快速收回（用当日最低价与当前价比较）
        day_low = self.day_lows.get(code, current_price)
        if day_low > 0 and current_price > day_low * 1.03:
            wash_score += 1
        # 3. 大盘稳定
        if market_quote:
            mkt_chg = market_quote.get("change_pct", 0)
            if abs(mkt_chg) < 0.5:
                wash_score += 1
        # 4. 板块未联动（简化: 大盘不跌则板块未联动）
        if market_quote and market_quote.get("change_pct", 0) > -0.3:
            wash_score += 1
        # V9.0: 5. 盘口资金承接（外盘占比>60%）
        if outer + inner > 0:
            outer_r = outer / (outer + inner)
            if outer_r > 0.60:
                wash_score += 1
        # V9.0: 6. 委比偏多（买盘挂单占优）
        order_ratio = quote.get("order_ratio", 0)
        if order_ratio > 0.2:
            wash_score += 1

        min_wash = cfg.get("wash_trading", {}).get("min_conditions", 2)
        if wash_score >= min_wash:
            return "wash_trading"

        return "unknown"

    def _get_sector_change_pct(self, code: str, holding: dict):
        """
        批2-S3: 获取标的所属板块当日涨跌幅（SECTOR_CHANGE_PCT_ENABLED 开关控制）

        返回:
            float: 板块涨跌幅百分比（东财真实值）
            None:  开关关闭/未命中/失败，调用方回退大盘近似
        绝不抛异常。
        """
        try:
            enabled = getattr(config, "SECTOR_CHANGE_PCT_ENABLED", False)
            if not enabled:
                # 默认关闭: 完全维持现状，仅首次记录一次开关状态（勿刷屏）
                if not self._sector_switch_logged:
                    self._sector_switch_logged = True
                    logger.debug("[板块联动] SECTOR_CHANGE_PCT_ENABLED=关闭，维持大盘近似判定")
                return None

            # 赛道名: 优先持仓自带sector，其次STOCK_POOL的赛道字段
            sector_name = holding.get("sector", "") or ""
            if not sector_name:
                sector_name = getattr(config, "STOCK_POOL", {}).get(code, {}).get("赛道", "")
            if not sector_name:
                return None

            from data.realtime import fetch_sector_changes_cached  # O(1)模块级缓存
            board_map = fetch_sector_changes_cached()
            if not board_map:
                return None

            # 模糊匹配: 赛道名与东财板块名互相包含即命中
            for board_name, pct in board_map.items():
                if not board_name or pct is None:
                    continue
                if board_name in sector_name or sector_name in board_name:
                    try:
                        return float(pct)
                    except (TypeError, ValueError):
                        continue
            return None
        except Exception as e:
            logger.debug(f"[板块联动] {code} 板块涨跌幅获取失败，回退大盘近似: {e}")
            return None

    def _get_dynamic_buffer_minutes(self, code: str, quote: dict,
                                     holding: dict, market_quote: dict) -> float:
        """根据场景判定返回动态缓冲时间"""
        scenario = self._classify_decline_scenario(code, quote, holding, market_quote)
        cfg = self.smart_buffer_cfg

        if scenario == "true_decline":
            buf = cfg.get("true_decline", {}).get("buffer_minutes", 3)
            logger.info(f"[智能缓冲] {code} 判定为真跌，缓冲缩短为{buf}分钟")
            return buf
        elif scenario == "wash_trading":
            buf = cfg.get("wash_trading", {}).get("buffer_minutes", 15)
            return buf
        else:
            return cfg.get("default_buffer_minutes", 8)

    # ============================================================
    # 八、V8.0 监控级别状态管理（P0-2 供scheduler变频使用）
    # ============================================================

    def _update_alert_level(self, quotes: dict, market_quote: dict):
        """
        根据当前行情更新监控级别（normal/warning/emergency）
        scheduler读取此状态决定下次扫描间隔
        """
        esc_cfg = getattr(config, 'INTRADAY_ESCALATION_CONFIG', {})
        if not esc_cfg:
            return

        warn_triggers = esc_cfg.get("warning_triggers", {})
        emerg_triggers = esc_cfg.get("emergency_triggers", {})

        # 检查紧急条件
        is_emergency = False
        is_warning = False

        # 大盘检查
        if market_quote:
            mkt_chg = market_quote.get("change_pct", 0)
            if mkt_chg < emerg_triggers.get("market_drop_pct", -2.5):
                is_emergency = True
            elif mkt_chg < warn_triggers.get("market_drop_pct", -1.5):
                is_warning = True

        # 持仓股检查
        for code, holding in self.holdings.items():
            if code not in quotes:
                continue
            quote = quotes[code]
            change_pct = quote.get("change_pct", 0)
            current_price = quote.get("price", 0)
            stop_loss = holding.get("stop_loss", 0)

            # 紧急: 跌>5% 或 触及止损
            if change_pct < emerg_triggers.get("holding_drop_pct", -5.0):
                is_emergency = True
            if stop_loss > 0 and current_price > 0 and current_price <= stop_loss:
                if emerg_triggers.get("stop_loss_touched", True):
                    is_emergency = True

            # 预警: 跌>3% 或 距止损<2%
            if change_pct < warn_triggers.get("holding_drop_pct", -3.0):
                is_warning = True
            if stop_loss > 0 and current_price > 0:
                distance = (current_price - stop_loss) / current_price
                if distance < warn_triggers.get("approaching_stop_loss", 0.02):
                    is_warning = True

        # 更新状态
        if is_emergency:
            self.alert_level = "emergency"
            self._clear_count = 0
        elif is_warning:
            self.alert_level = "warning"
            self._clear_count = 0
        else:
            self._clear_count += 1
            downgrade_n = esc_cfg.get("downgrade_after_clear", 3)
            if self._clear_count >= downgrade_n:
                self.alert_level = "normal"

    def get_alert_level(self) -> str:
        """获取当前监控级别（供scheduler读取）"""
        return self.alert_level

    # ============================================================
    # 九、V9.0 VWAP均价线监控（P0-1）
    # ============================================================

    def _check_vwap(self, code: str, name: str, current_price: float,
                    quote: dict, today: str) -> list:
        """检查VWAP均价线支撑/压力（多空分水岭）"""
        alerts = []
        if not self.vwap_cfg.get("enabled", True):
            return alerts

        vwap = quote.get("vwap", 0)
        if vwap <= 0 or current_price <= 0:
            return alerts

        deviation = (current_price - vwap) / vwap
        bearish_pct = self.vwap_cfg.get("bearish_deviation_pct", -0.015)
        bullish_pct = self.vwap_cfg.get("bullish_deviation_pct", 0.03)
        persist_n = self.vwap_cfg.get("bearish_persist_cycles", 3)

        # 跟踪VWAP上下方状态
        is_above = current_price >= vwap
        was_above = self.prev_vwap_above.get(code, True)

        # 信号1: 从 VWAP上方跌破到下方（均价线失守）
        if was_above and not is_above and self.vwap_cfg.get("vwap_break_alert", True):
            alert_key = f"{today}_{code}_vwap_break"
            if alert_key not in self.alerts_sent:
                # FIX(2026-08-12): ETF无主力多空博弈，VWAP失守的"空头控盘"语义弱，
                # 降级为info避免与个股告警混淆（沪市51/56/58，深市15/16/18开头）
                is_etf = code.startswith(("15", "16", "18", "51", "56", "58"))
                alert = {
                    "level": "info" if is_etf else "warning",
                    "type": "VWAP失守",
                    "code": code,
                    "name": name,
                    "current_price": current_price,
                    "vwap": round(vwap, 2),
                    "deviation_pct": round(deviation * 100, 2),
                    "message": f"📉 {name}({code}) 跌破均价线! "
                              f"现价{current_price:.2f} < VWAP{vwap:.2f} | "
                              f"偏离{deviation*100:.1f}% | 空头控盘信号",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)
                self._send_alert(alert)

        # 信号2: 持续低于VWAP超过N个周期（空头控盘确认）
        if deviation < bearish_pct:
            self.vwap_below_count[code] = self.vwap_below_count.get(code, 0) + 1
            if self.vwap_below_count[code] == persist_n:
                alert_key = f"{today}_{code}_vwap_bearish"
                if alert_key not in self.alerts_sent:
                    alert = {
                        "level": "warning",
                        "type": "VWAP空头控盘",
                        "code": code,
                        "name": name,
                        "current_price": current_price,
                        "vwap": round(vwap, 2),
                        "persist_cycles": persist_n,
                        "message": f"⚠️ {name}({code}) 持续低于均价线{persist_n}个周期! "
                                  f"VWAP{vwap:.2f}压制 | 偏离{deviation*100:.1f}% | "
                                  f"确认走弱，建议减仓",
                        "time": datetime.datetime.now().strftime("%H:%M:%S"),
                    }
                    alerts.append(alert)
                    self.alerts_sent.add(alert_key)
                    self._send_alert(alert)
        else:
            self.vwap_below_count[code] = 0  # 重置

        # 信号3: 超买偏离（价格远高于VWAP，回归概率大）
        if deviation > bullish_pct:
            alert_key = f"{today}_{code}_vwap_overbought"
            if alert_key not in self.alerts_sent:
                alert = {
                    "level": "info",
                    "type": "VWAP超买偏离",
                    "code": code,
                    "name": name,
                    "deviation_pct": round(deviation * 100, 2),
                    "message": f"📈 {name}({code}) 偏离均价线+{deviation*100:.1f}%，"
                              f"短期超买，注意回归风险",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)

        self.prev_vwap_above[code] = is_above
        return alerts

    # ============================================================
    # 十、V9.0 盘口强弱监控（P0-2）
    # ============================================================

    def _check_orderbook(self, code: str, name: str, current_price: float,
                         quote: dict, holding: dict, today: str) -> list:
        """检查盘口强弱（内外盘比+委比+挂单异动）"""
        alerts = []
        if not self.orderbook_cfg.get("enabled", True):
            return alerts

        outer = quote.get("outer_vol", 0)
        inner = quote.get("inner_vol", 0)
        total_vol = outer + inner
        if total_vol <= 0:
            return alerts

        outer_ratio = outer / total_vol  # 外盘占比 [0~1]
        change_pct = quote.get("change_pct", 0)
        order_ratio = quote.get("order_ratio", 0)  # 委比 [-1~1]
        bid1_vol = quote.get("bid1_vol", 0)
        ask1_vol = quote.get("ask1_vol", 0)

        heavy_sell = self.orderbook_cfg.get("heavy_sell_outer_ratio", 0.35)
        strong_buy = self.orderbook_cfg.get("strong_buy_outer_ratio", 0.65)
        ask_pressure = self.orderbook_cfg.get("ask_pressure_ratio", 5.0)
        bid_withdraw = self.orderbook_cfg.get("bid_withdraw_pct", 0.20)

        # 信号1: 抛压沉重（外盘占比低 + 跌幅大）→ 真跌确认
        if outer_ratio < heavy_sell and change_pct < -2.0:
            alert_key = f"{today}_{code}_heavy_sell"
            if alert_key not in self.alerts_sent:
                alert = {
                    "level": "warning",
                    "type": "抛压沉重(真跌)",
                    "code": code,
                    "name": name,
                    "outer_ratio": round(outer_ratio * 100, 1),
                    "change_pct": change_pct,
                    "message": f"🔴 {name}({code}) 抛压沉重! "
                              f"外盘仅{outer_ratio*100:.0f}% + 跌{change_pct:.1f}% | "
                              f"主动卖出占主导，真跌确认",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)
                self._send_alert(alert)

        # 信号2: 资金承接（外盘高 + 价格不跌）→ 洗盘概率
        elif outer_ratio > strong_buy and change_pct > -1.0:
            alert_key = f"{today}_{code}_strong_buy"
            if alert_key not in self.alerts_sent:
                alert = {
                    "level": "info",
                    "type": "资金承接(洗盘)",
                    "code": code,
                    "name": name,
                    "outer_ratio": round(outer_ratio * 100, 1),
                    "message": f"🟢 {name}({code}) 资金承接良好! "
                              f"外盘{outer_ratio*100:.0f}% + 涨跌{change_pct:+.1f}% | "
                              f"主动买入占主导，洗盘概率大",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)

        # 信号3: 压盘吸筹（卖一巨单 + 价格不跌）
        if bid1_vol > 0 and ask1_vol > bid1_vol * ask_pressure and abs(change_pct) < 1.0:
            alert_key = f"{today}_{code}_ask_pressure"
            if alert_key not in self.alerts_sent:
                alert = {
                    "level": "info",
                    "type": "压盘吸筹",
                    "code": code,
                    "name": name,
                    "message": f"🛡️ {name}({code}) 卖一压单{ask1_vol:.0f}手"
                              f"(买一{bid1_vol:.0f}手的{ask1_vol/max(bid1_vol,1):.1f}倍) | "
                              f"价格不跌，疑似压盘吸筹",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)

        # 信号4: 托盘撤退（买一量突然消失）
        prev_bid1 = self.prev_bid1_vol.get(code, 0)
        if prev_bid1 > 100 and bid1_vol < prev_bid1 * bid_withdraw and change_pct < -0.5:
            alert_key = f"{today}_{code}_bid_withdraw"
            if alert_key not in self.alerts_sent:
                alert = {
                    "level": "warning",
                    "type": "托盘撤退",
                    "code": code,
                    "name": name,
                    "message": f"⚠️ {name}({code}) 买一托盘撤退! "
                              f"买一量{prev_bid1:.0f}→{bid1_vol:.0f}手"
                              f"(减少{(1-bid1_vol/prev_bid1)*100:.0f}%) | 注意下跌加速",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)
                self._send_alert(alert)

        self.prev_bid1_vol[code] = bid1_vol
        return alerts

    # ============================================================
    # 十一、V9.0 量比异动监控（P1-1）
    # ============================================================

    def _check_volume_ratio(self, code: str, name: str, current_price: float,
                            quote: dict, holding: dict, today: str) -> list:
        """检查量比异动（放量滞涨/底部放量）"""
        alerts = []
        if not self.vol_ratio_cfg.get("enabled", True):
            return alerts

        volume = quote.get("volume", 0)  # 当日累计成交量(手)
        avg_volume = holding.get("avg_volume", 0)  # FIX: 更正注释口径: 近20日日均量(手)，由update_holdings.py写入
        change_pct = quote.get("change_pct", 0)
        buy_price = holding.get("buy_price", 0)

        if avg_volume <= 0 or volume <= 0:
            return alerts

        # 计算量比: 当前分钟均量 / 历史分钟均量
        now = datetime.datetime.now()
        market_open = now.replace(hour=9, minute=30, second=0)
        mid_close = now.replace(hour=11, minute=30, second=0)
        mid_open = now.replace(hour=13, minute=0, second=0)

        # 计算已经过的交易分钟数
        if now <= mid_close:
            elapsed_min = max((now - market_open).total_seconds() / 60, 1)
        elif now < mid_open:
            elapsed_min = 120  # 上午2小时
        else:
            elapsed_min = 120 + max((now - mid_open).total_seconds() / 60, 1)
        elapsed_min = min(elapsed_min, 240)

        current_per_min = volume / elapsed_min
        hist_per_min = avg_volume / 240.0
        vol_ratio = current_per_min / hist_per_min if hist_per_min > 0 else 1.0

        surge = self.vol_ratio_cfg.get("surge_threshold", 3.0)
        stagnation = self.vol_ratio_cfg.get("stagnation_change_pct", 1.0)
        high_profit = self.vol_ratio_cfg.get("high_level_profit_pct", 5.0)

        if vol_ratio < surge:
            return alerts

        # 放量滞涨（出货嫌疑）
        profit_pct = ((current_price / buy_price - 1) * 100) if buy_price > 0 else 0
        if change_pct < stagnation and profit_pct > high_profit:
            alert_key = f"{today}_{code}_vol_stagnation"
            if alert_key not in self.alerts_sent:
                alert = {
                    "level": "warning",
                    "type": "放量滞涨(出货)",
                    "code": code,
                    "name": name,
                    "vol_ratio": round(vol_ratio, 1),
                    "change_pct": change_pct,
                    "profit_pct": round(profit_pct, 1),
                    "message": f"🚨 {name}({code}) 放量滞涨! "
                              f"量比{vol_ratio:.1f} + 涨幅仅{change_pct:.1f}% | "
                              f"浮盈{profit_pct:.1f}% | 出货嫌疑，建议减仓",
                    "time": now.strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)
                self._send_alert(alert)

        # 放量下跌（加速下杀）
        elif change_pct < -2.0:
            alert_key = f"{today}_{code}_vol_dump"
            if alert_key not in self.alerts_sent:
                alert = {
                    "level": "warning",
                    "type": "放量下杀",
                    "code": code,
                    "name": name,
                    "vol_ratio": round(vol_ratio, 1),
                    "change_pct": change_pct,
                    "message": f"🔴 {name}({code}) 放量下杀! "
                              f"量比{vol_ratio:.1f} + 跌{change_pct:.1f}% | "
                              f"恐慌性抛售，注意风险",
                    "time": now.strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)
                self._send_alert(alert)

        return alerts

    # ============================================================
    # V3.2: 换手率异常 + 量价背离检测（知识库规则落地）
    # ============================================================

    def _check_turnover_divergence(self, code: str, name: str, current_price: float,
                                   quote: dict, holding: dict, today: str) -> list:
        """检查换手率异常和量价背离
        
        知识库规则:
          - 换手率>15%: 过度换手，可能是主力出货（给低分0.05）
          - 换手率3-10%: 健康活跃区间
          - 涨+缩量: 上涨乏力，警惕回调
          - 跌+缩量: 卖压减小，可能见底
        """
        alerts = []
        turnover = quote.get("turnover", 0)  # 换手率(%)
        change_pct = quote.get("change_pct", 0)
        volume = quote.get("volume", 0)
        avg_volume = holding.get("avg_volume", 0)
        buy_price = holding.get("buy_price", 0)
        now = datetime.datetime.now()

        # ---- 规则1: 换手率异常高（>15% = 主力出货风险）----
        if turnover >= 15.0:
            alert_key = f"{today}_{code}_high_turnover"
            if alert_key not in self.alerts_sent:
                profit_pct = ((current_price / buy_price - 1) * 100) if buy_price > 0 else 0
                # 高位+高换手 = 出货概率极大
                if profit_pct > 10:
                    level = "critical"
                    msg = (f"🚨 {name}({code}) 换手率{turnover:.1f}%异常偏高! "
                           f"浮盈{profit_pct:.1f}%+过度换手 | "
                           f"主力出货概率极大，建议立即减仓")
                else:
                    level = "warning"
                    msg = (f"⚠️ {name}({code}) 换手率{turnover:.1f}%异常偏高! "
                           f"涨跌{change_pct:+.1f}% | "
                           f"筹码剧烈换手，注意主力动向")
                alert = {
                    "level": level,
                    "type": "换手率异常",
                    "code": code,
                    "name": name,
                    "turnover": turnover,
                    "change_pct": change_pct,
                    "message": msg,
                    "time": now.strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)
                self._send_alert(alert)

        # ---- 规则2: 量价背离（涨+缩量 = 上涨乏力）----
        if avg_volume > 0 and volume > 0:
            # 计算当前量比（简化版）
            elapsed_min = self._calc_elapsed_minutes(now)
            if elapsed_min > 30:  # 开盘30分钟后才检测
                current_per_min = volume / elapsed_min
                hist_per_min = avg_volume / 240.0
                vol_ratio = current_per_min / hist_per_min if hist_per_min > 0 else 1.0

                # 涨>3% 但量比<0.7 = 缩量上涨（背离）
                if change_pct > 3.0 and vol_ratio < 0.7:
                    alert_key = f"{today}_{code}_vol_diverge_up"
                    if alert_key not in self.alerts_sent:
                        alert = {
                            "level": "warning",
                            "type": "量价背离(缩量涨)",
                            "code": code,
                            "name": name,
                            "change_pct": change_pct,
                            "vol_ratio": round(vol_ratio, 2),
                            "message": f"⚠️ {name}({code}) 缩量上涨背离! "
                                      f"涨{change_pct:.1f}%但量比仅{vol_ratio:.2f} | "
                                      f"上涨乏力，警惕回调",
                            "time": now.strftime("%H:%M:%S"),
                        }
                        alerts.append(alert)
                        self.alerts_sent.add(alert_key)
                        self._send_alert(alert)

                # 跌>3% 但量比<0.5 = 缩量下跌（卖压衰竭，可能见底）
                elif change_pct < -3.0 and vol_ratio < 0.5:
                    alert_key = f"{today}_{code}_vol_diverge_down"
                    if alert_key not in self.alerts_sent:
                        alert = {
                            "level": "info",
                            "type": "缩量下跌(见底信号)",
                            "code": code,
                            "name": name,
                            "change_pct": change_pct,
                            "vol_ratio": round(vol_ratio, 2),
                            "message": f"💡 {name}({code}) 缩量下跌! "
                                      f"跌{change_pct:.1f}%但量比仅{vol_ratio:.2f} | "
                                      f"卖压衰竭，可能接近底部",
                            "time": now.strftime("%H:%M:%S"),
                        }
                        alerts.append(alert)
                        self.alerts_sent.add(alert_key)
                        self._send_alert(alert)

        return alerts

    @staticmethod
    def _calc_elapsed_minutes(now: datetime.datetime) -> float:
        """计算已经过的交易分钟数"""
        market_open = now.replace(hour=9, minute=30, second=0)
        mid_close = now.replace(hour=11, minute=30, second=0)
        mid_open = now.replace(hour=13, minute=0, second=0)
        if now <= mid_close:
            return max((now - market_open).total_seconds() / 60, 1)
        elif now < mid_open:
            return 120.0
        else:
            return 120 + max((now - mid_open).total_seconds() / 60, 1)

    # ============================================================
    # 十二、V9.0 开盘30分钟定性（P0-4）
    # ============================================================

    def _check_opening_phase(self, quotes: dict, today: str) -> list:
        """开盘30分钟定性：10:00时判断当日多空基调"""
        alerts = []
        if not self.phase_cfg.get("enabled", True):
            return alerts
        if self.opening_phase_done:
            return alerts

        now = datetime.datetime.now()
        end_time = self.phase_cfg.get("opening_end_time", "10:00")
        end_h, end_m = map(int, end_time.split(":"))
        if now.hour < end_h or (now.hour == end_h and now.minute < end_m):
            return alerts  # 未到10:00，不执行

        # 到达10:00，执行定性
        self.opening_phase_done = True
        high_open_low = self.phase_cfg.get("high_open_low_walk_pct", 0.01)
        low_open_high = self.phase_cfg.get("low_open_high_walk_pct", -0.01)

        for code, quote in quotes.items():
            if code not in self.holdings:
                continue
            open_price = quote.get("open", 0)
            prev_close = quote.get("prev_close", 0)
            current_price = quote.get("price", 0)
            name = self.holdings[code].get("name", code)

            if open_price <= 0 or prev_close <= 0 or current_price <= 0:
                continue

            open_gap = (open_price - prev_close) / prev_close  # 高开/低开幅度
            current_vs_open = (current_price - open_price) / open_price  # 开盘后走势

            # 高开低走（出货信号）
            if open_gap > high_open_low and current_vs_open < 0:
                label = "高开低走(出货)"
                alert_key = f"{today}_{code}_opening_bearish"
                if alert_key not in self.alerts_sent:
                    alert = {
                        "level": "warning",
                        "type": "开盘定性:高开低走",
                        "code": code,
                        "name": name,
                        "open_gap_pct": round(open_gap * 100, 2),
                        "message": f"📉 {name}({code}) 开盘定性: 高开低走! "
                                  f"高开{open_gap*100:.1f}%后转跌{current_vs_open*100:.1f}% | "
                                  f"出货信号，建议减仓",
                        "time": now.strftime("%H:%M:%S"),
                    }
                    alerts.append(alert)
                    self.alerts_sent.add(alert_key)
                    self._send_alert(alert)

            # 低开高走（吸筹信号）
            elif open_gap < low_open_high and current_vs_open > 0:
                label = "低开高走(吸筹)"
                alert_key = f"{today}_{code}_opening_bullish"
                if alert_key not in self.alerts_sent:
                    alert = {
                        "level": "info",
                        "type": "开盘定性:低开高走",
                        "code": code,
                        "name": name,
                        "message": f"🟢 {name}({code}) 开盘定性: 低开高走! "
                                  f"低开{open_gap*100:.1f}%后反弹+{current_vs_open*100:.1f}% | "
                                  f"吸筹信号，可持有观察",
                        "time": now.strftime("%H:%M:%S"),
                    }
                    alerts.append(alert)
                    self.alerts_sent.add(alert_key)
            else:
                label = "正常"

            self.day_phase_label[code] = label

        return alerts

    # ============================================================
    # 十三、V9.0 尾盘异动检测（P0-4）
    # ============================================================

    def _check_closing_phase(self, quotes: dict, today: str) -> list:
        """尾盘异动: 14:45后检测急拉/急跌"""
        alerts = []
        if not self.phase_cfg.get("enabled", True):
            return alerts

        now = datetime.datetime.now()
        start_time = self.phase_cfg.get("closing_start_time", "14:45")
        start_h, start_m = map(int, start_time.split(":"))
        if now.hour < start_h or (now.hour == start_h and now.minute < start_m):
            return alerts  # 未到尾盘时间

        surge_pct = self.phase_cfg.get("closing_surge_pct", 1.5)
        plunge_pct = self.phase_cfg.get("closing_plunge_pct", -1.5)

        for code, quote in quotes.items():
            if code not in self.holdings:
                continue
            current_price = quote.get("price", 0)
            name = self.holdings[code].get("name", code)
            if current_price <= 0:
                continue

            # 用分时价格序列计算近5分钟涨跌
            history = self.price_history.get(code, [])
            if len(history) < 5:
                continue

            # 取5分钟前的价格（轮询间隔60秒，5个点≈5分钟）
            price_5min_ago = history[-5][1]
            if price_5min_ago <= 0:
                continue

            change_5min = (current_price - price_5min_ago) / price_5min_ago * 100

            # 尾盘急拉
            if change_5min > surge_pct:
                alert_key = f"{today}_{code}_closing_surge"
                if alert_key not in self.closing_alerts_sent:
                    alert = {
                        "level": "info",
                        "type": "尾盘急拉",
                        "code": code,
                        "name": name,
                        "change_5min": round(change_5min, 2),
                        "message": f"📈 {name}({code}) 尾盘5分钟急拉+{change_5min:.1f}%! "
                                  f"次日高开概率大，关注是否追涨",
                        "time": now.strftime("%H:%M:%S"),
                    }
                    alerts.append(alert)
                    self.closing_alerts_sent.add(alert_key)
                    self._send_alert(alert)

            # 尾盘急跌
            elif change_5min < plunge_pct:
                alert_key = f"{today}_{code}_closing_plunge"
                if alert_key not in self.closing_alerts_sent:
                    alert = {
                        "level": "warning",
                        "type": "尾盘跳水",
                        "code": code,
                        "name": name,
                        "change_5min": round(change_5min, 2),
                        "message": f"🚨 {name}({code}) 尾盘5分钟跳水{change_5min:.1f}%! "
                                  f"次日风险预警，关注是否有利空",
                        "time": now.strftime("%H:%M:%S"),
                    }
                    alerts.append(alert)
                    self.closing_alerts_sent.add(alert_key)
                    self._send_alert(alert)

        return alerts

    # ============================================================
    # 十四、V9.0 盘口快照变化追踪（P2-2）
    # ============================================================

    def check_orderbook_snapshot_change(self, code: str, name: str,
                                         quote: dict, today: str) -> list:
        """盘口快照变化追踪: 检测买一/卖一挂单量突变

        与P0-2的委比监控互补:
          - P0-2: 静态判断当前委比/外盘占比
          - P2-2: 动态跟踪相邻两轮的变化率
        """
        alerts = []
        pattern_cfg = getattr(config, 'INTRADAY_PATTERN_CONFIG', {})
        if not pattern_cfg.get("enabled", True):
            return alerts

        bid1_vol = quote.get("bid1_vol", 0)
        ask1_vol = quote.get("ask1_vol", 0)
        change_pct = quote.get("change_pct", 0)

        # 获取上轮快照
        prev_bid1 = self.prev_bid1_vol.get(code, 0)

        # 卖一持续增加（压盘）
        if not hasattr(self, '_prev_ask1_vol'):
            self._prev_ask1_vol = {}
        prev_ask1 = self._prev_ask1_vol.get(code, 0)

        if prev_ask1 > 0 and ask1_vol > prev_ask1 * 1.5 and abs(change_pct) < 1.0:
            alert_key = f"{today}_{code}_ask_building"
            if alert_key not in self.alerts_sent:
                alert = {
                    "level": "info",
                    "type": "卖压增加(压盘)",
                    "code": code,
                    "name": name,
                    "message": f"🛡️ {name}({code}) 卖一挂单增加"
                              f"({prev_ask1:.0f}→{ask1_vol:.0f}手) | "
                              f"价格不动，疑似压盘吸筹",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                self.alerts_sent.add(alert_key)

        self._prev_ask1_vol[code] = ask1_vol
        return alerts

    # ============================================================
    # 十五、V9.0 分时形态识别（P2-3: M头/阶梯/V反）
    # ============================================================

    def detect_intraday_pattern(self, code: str, name: str, today: str) -> list:
        """基于日内价格序列识别分时形态

        形态:
          - M头(双顶): 两次高点差<0.3% + 跌破颈线
          - 阶梯上攻: 3级台阶，每级上涨>0.5%
          - V反: 急跌>3%后5分钟反弹>2%
        """
        alerts = []
        pattern_cfg = getattr(config, 'INTRADAY_PATTERN_CONFIG', {})
        if not pattern_cfg.get("enabled", True):
            return alerts

        history = self.price_history.get(code, [])
        min_points = pattern_cfg.get("min_data_points", 30)
        if len(history) < min_points:
            return alerts

        prices = [p[1] for p in history]

        # --- M头检测 ---
        m_alert = self._detect_m_head(code, name, prices, pattern_cfg, today)
        if m_alert:
            alerts.append(m_alert)

        # --- 阶梯上攻检测 ---
        stair_alert = self._detect_staircase(code, name, prices, pattern_cfg, today)
        if stair_alert:
            alerts.append(stair_alert)

        # --- V反检测 ---
        v_alert = self._detect_v_reversal(code, name, prices, pattern_cfg, today)
        if v_alert:
            alerts.append(v_alert)

        return alerts

    def _detect_m_head(self, code: str, name: str, prices: list,
                       cfg: dict, today: str) -> dict:
        """M头(双顶)检测: 两个高点差<0.3% + 跌破颈线"""
        peak_diff_pct = cfg.get("m_head_peak_diff_pct", 0.003)
        neckline_break = cfg.get("m_head_neckline_break", -0.005)

        if len(prices) < 20:
            return None

        # 简化: 将价格序列分为前半/后半，各找最高点
        mid = len(prices) // 2
        first_half = prices[:mid]
        second_half = prices[mid:]

        peak1 = max(first_half)
        peak2 = max(second_half)

        # 两峰接近
        if peak1 <= 0:
            return None
        diff = abs(peak1 - peak2) / peak1
        if diff > peak_diff_pct:
            return None

        # 颈线 = 两峰之间的最低点
        valley = min(prices[mid - 5:mid + 5]) if mid > 5 else min(prices)
        current = prices[-1]

        # 跌破颈线
        if current < valley * (1 + neckline_break):
            alert_key = f"{today}_{code}_m_head"
            if alert_key not in self.alerts_sent:
                self.alerts_sent.add(alert_key)
                return {
                    "level": "warning",
                    "type": "分时M头",
                    "code": code,
                    "name": name,
                    "message": f"📉 {name}({code}) 分时M头形成! "
                              f"双顶{peak1:.2f}/{peak2:.2f}(差{diff*100:.2f}%) | "
                              f"已跌破颈线{valley:.2f} | 建议减仓",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
        return None

    def _detect_staircase(self, code: str, name: str, prices: list,
                          cfg: dict, today: str) -> dict:
        """阶梯上攻: 3级台阶，每级上涨>0.5%"""
        min_steps = cfg.get("stair_min_steps", 3)
        min_rise = cfg.get("stair_min_rise_pct", 0.005)

        if len(prices) < 30:
            return None

        # 简化算法: 将序列分段，检测每段是否合阶梯特征
        segment_size = len(prices) // (min_steps + 1)
        if segment_size < 5:
            return None

        steps = 0
        prev_level = prices[0]
        for i in range(1, min_steps + 2):
            seg_start = i * segment_size
            seg_end = min((i + 1) * segment_size, len(prices))
            if seg_start >= len(prices):
                break
            seg_avg = sum(prices[seg_start:seg_end]) / max(len(prices[seg_start:seg_end]), 1)
            rise = (seg_avg - prev_level) / prev_level if prev_level > 0 else 0
            if rise > min_rise:
                steps += 1
                prev_level = seg_avg

        if steps >= min_steps:
            alert_key = f"{today}_{code}_staircase"
            if alert_key not in self.alerts_sent:
                self.alerts_sent.add(alert_key)
                total_rise = (prices[-1] - prices[0]) / prices[0] * 100
                return {
                    "level": "info",
                    "type": "阶梯上攻",
                    "code": code,
                    "name": name,
                    "message": f"📈 {name}({code}) 分时阶梯上攻! "
                              f"{steps}级台阶，累计+{total_rise:.1f}% | "
                              f"主力有序推升，持有",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
        return None

    def _detect_v_reversal(self, code: str, name: str, prices: list,
                           cfg: dict, today: str) -> dict:
        """V反: 急跌>3%后5分钟反弹>2%"""
        drop_pct = cfg.get("v_reversal_drop_pct", -0.03)
        bounce_pct = cfg.get("v_reversal_bounce_pct", 0.02)

        if len(prices) < 10:
            return None

        # 找近10个点内的最低点
        recent = prices[-10:]
        min_price = min(recent)
        min_idx = recent.index(min_price)

        # 最低点之前的最高点
        before_min = recent[:min_idx] if min_idx > 0 else [prices[-10]]
        local_high = max(before_min) if before_min else prices[-10]

        # 下跌幅度
        drop = (min_price - local_high) / local_high if local_high > 0 else 0
        if drop > drop_pct:  # drop是负数，drop_pct也是负数
            return None

        # 反弹幅度（从最低点到当前）
        current = prices[-1]
        bounce = (current - min_price) / min_price if min_price > 0 else 0

        if bounce > bounce_pct:
            alert_key = f"{today}_{code}_v_reversal_intraday"
            if alert_key not in self.alerts_sent:
                self.alerts_sent.add(alert_key)
                return {
                    "level": "info",
                    "type": "分时V反",
                    "code": code,
                    "name": name,
                    "message": f"🔄 {name}({code}) 分时V型反转! "
                              f"急跌{drop*100:.1f}%后反弹+{bounce*100:.1f}% | "
                              f"短期止跌，观察持续性",
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
        return None


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
