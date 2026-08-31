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
        self._emergency_since = None  # V10.3: emergency开始时间（防锁定）
        self._emergency_suppressed_until = None  # V10.3 FIX: 降级后抑制窗口（防乒乓回升）
        self._code_last_alert_time = {}  # V10.3: {code: datetime} 标的级全局冷却

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

        # V9.1: 大盘门控 — 延迟预警队列与状态跟踪
        self._pending_alerts_queue = []  # [(alert, alert_key)] 门控延迟的非紧急预警
        self._prev_gate_state = "pass"   # 上轮门控状态（用于检测 delay→pass 转换）
        self._queued_alert_keys = set()  # 被门控延迟的预警key（防止重复入队）

        # V9.3: warning级预警邮件冷却与升级追踪
        self._warning_email_cooldown = {}   # {code: datetime} warning级邮件冷却追踪
        self._warning_count_window = {}     # {code: [(datetime, type)]} 滑动窗口（升级判定用）

        # V9.3: 波动率突变检测冷却（每标的15分钟最多触发1次）
        self._vol_alert_cooldown = {}       # {code: datetime} 波动率预警冷却

        # V9.3: 多因子联合评分冷却（每标的30分钟最多触发1次）
        self._fusion_alert_cooldown = {}    # {code: datetime} 联合评分预警冷却

        # V9.3: 预警效果在线评估器（P1-⑥）
        self._alert_evaluator = None  # 懒初始化，避免非交易时段无谓导入

        # V10.0 P0-①: 预警参数自优化闭环（加载校准参数）
        self._calibration = self._load_calibration_params()

        # V9.2: 跨日状态跟踪（进程不重启时检测日期变化自动清理日内状态）
        self._state_date = datetime.date.today().isoformat()

        # V9.2: 持仓热重载跟踪（检测 holdings.json 变更后自动刷新监控列表）
        self._holdings_file = config.get_holdings_file()
        self._holdings_mtime = 0.0
        self._reload_check_counter = 0  # 每N轮检查一次文件变更（降低IO）

        # FIX: 从持久化文件加载当日状态（盘中重启不重复报警/不重置缓冲计时）
        self._load_state()

    # ============================================================
    # V10.0 P0-①: 预警参数自优化闭环（加载校准参数）
    # ============================================================

    def _load_calibration_params(self) -> dict:
        """加载预警校准参数（仅启动时调用一次，失败降级为空 dict）"""
        try:
            from strategy.alert_auto_calibrate import load_calibration
            cal = load_calibration()
            if cal:
                mode = "观察" if cal.get("observation_mode") else "生效"
                logger.info(f"[预警校准] 已加载（{mode}模式）| "
                           f"阈值因子{len(cal.get('threshold_factors', {}))}种 | "
                           f"更新于{cal.get('last_updated', '未知')}")
            return cal
        except Exception as e:
            logger.debug(f"[预警校准] 加载失败（降级为空）: {e}")
            return {}

    # ============================================================
    # 状态持久化（FIX: 参照alert_cooldown.json模式，失败仅debug不阻断）
    # ============================================================

    def _load_state(self):
        """FIX: 加载当日盘中监控状态（跨日自动重置为空状态，失败降级不阻断）
        V9.2: 补充加载 day_lows / stop_touch_prices / _queued_alert_keys 防止盘中重启丢失
        """
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
            # V9.2: 补充加载日内状态字段
            self.day_lows = {c: float(v) for c, v in data.get("day_lows", {}).items() if v > 0}
            self.stop_touch_prices = {c: float(v) for c, v in data.get("stop_touch_prices", {}).items() if v > 0}
            self._queued_alert_keys = set(data.get("queued_alert_keys", []))
            self._state_date = data.get("date", self._state_date)
            logger.info(f"[盘中监控] 已恢复当日状态: 已发预警{len(self.alerts_sent)}条, "
                        f"缓冲计时{len(self.stop_loss_first_touch)}只, "
                        f"日内低点{len(self.day_lows)}只, 门控队列{len(self._queued_alert_keys)}条")
        except Exception as e:
            logger.debug(f"[盘中监控] 状态加载失败，使用空状态: {e}")

    def _save_state(self):
        """FIX: 当日状态落盘（原子写 + 补全日内状态字段，失败仅debug不阻断监控）
        V9.2: 补充持久化 day_lows / stop_touch_prices / _queued_alert_keys
        """
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
                # V9.2: 补充持久化日内状态字段
                "day_lows": dict(self.day_lows),
                "stop_touch_prices": dict(self.stop_touch_prices),
                "queued_alert_keys": list(self._queued_alert_keys),
            }
            try:
                from utils.file_io import atomic_json_write
                atomic_json_write(_INTRADAY_STATE_FILE, data, indent=None)
            except ImportError:
                os.makedirs(os.path.dirname(_INTRADAY_STATE_FILE), exist_ok=True)
                with open(_INTRADAY_STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[盘中监控] 状态持久化失败: {e}")

    def _daily_reset_if_new_day(self):
        """V9.2: 检测日期变化，自动清理所有日内增长的 dict/set，防止内存泄漏

        解决的问题:
          - 进程跨日运行时 alerts_sent / price_history / day_lows 等不释放
          - 内存持续膨胀（数天/数周不重启时）
          - 旧日状态干扰新日逻辑（如 day_lows 残留昨日值）
        """
        today = datetime.date.today().isoformat()
        if self._state_date == today:
            return  # 同日，无需清理

        logger.info(f"[盘中监控] 检测到跨日({self._state_date}→{today})，清理日内状态")

        # 清理所有日内增长的 dict/set
        self.alerts_sent.clear()
        self.price_history.clear()
        self.day_lows.clear()
        self.day_volumes.clear()
        self.stop_loss_first_touch.clear()
        self.stop_touch_prices.clear()
        self.vwap_below_count.clear()
        self.prev_vwap_above.clear()
        self.prev_bid1_vol.clear()
        self.closing_alerts_sent.clear()
        self.day_phase_label.clear()
        self.opening_prices.clear()
        self._queued_alert_keys.clear()
        self._pending_alerts_queue.clear()
        self._atr_obs_logged.clear()
        self.gradient_triggered.clear()

        # 重置标记
        self.opening_phase_done = False
        self._sector_switch_logged = False
        self._prev_gate_state = "pass"
        self._state_date = today
        self._atr_cache = {"date": today, "values": {}}

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

            # V9.2: 持仓热重载（每10轮≈5分钟检查一次holdings.json变更）
            self._reload_check_counter += 1
            if self._reload_check_counter >= 10:
                self._reload_holdings_if_changed()
                self._reload_check_counter = 0

            try:
                self._check_all()
            except Exception as e:
                logger.error(f"[盘中监控] 检查异常: {e}")

            # V9.3: 预警效果评估 — 每轮验证待验证的历史预警
            if self._alert_evaluator is not None:
                try:
                    self._alert_evaluator.verify_pending()
                except Exception:
                    pass

            # V4.0(G8): 每轮扫描后写心跳文件，watchdog据此检测监控静默失效
            self._write_heartbeat("active")

            time.sleep(self.poll_interval)

    def _reload_holdings_if_changed(self):
        """V9.2: 检测 holdings.json 文件变更，自动刷新监控列表

        解决的问题:
          - 盘中更新持仓后（如新增3只标的），监控线程仍用启动时的旧数据
          - 导致新标的完全无盘中监控覆盖（止损/VWAP/抛压等全部缺失）

        策略:
          - 比较文件 mtime，变更时重新加载
          - 新增标的: 加入监控（补充必要字段）
          - 已清仓标的(shares=0): 从监控列表移除
          - 存续标的: 更新 stop_loss/buy_price 等关键字段
        """
        try:
            if not os.path.exists(self._holdings_file):
                return
            mtime = os.path.getmtime(self._holdings_file)
            if mtime <= self._holdings_mtime:
                return  # 文件未变更

            logger.info(f"[盘中监控] 检测到 holdings.json 变更，重新加载持仓...")
            with open(self._holdings_file, "r", encoding="utf-8") as f:
                new_holdings = json.load(f)

            # 过滤已清仓标的
            new_holdings = {c: v for c, v in new_holdings.items()
                           if isinstance(v, dict) and v.get("shares", 0) > 0}

            old_codes = set(self.holdings.keys())
            new_codes = set(new_holdings.keys())
            added = new_codes - old_codes
            removed = old_codes - new_codes

            # 补充必要字段
            for code, pos in new_holdings.items():
                pos["name"] = config.get_stock_name(code)
                if "stop_loss" not in pos:
                    pos["stop_loss"] = pos.get("buy_price", 0) * (1 - config.INITIAL_STOP_LOSS_PCT)

            # 更新持仓
            self.holdings = new_holdings
            self._holdings_mtime = mtime

            # 清理已移除标的的日内状态
            for code in removed:
                self.day_lows.pop(code, None)
                self.day_volumes.pop(code, None)
                self.stop_touch_prices.pop(code, None)
                self.price_history.pop(code, None)
                self.last_prices.pop(code, None)

            if added or removed:
                logger.info(f"[盘中监控] 持仓刷新完成: 新增{len(added)}只{list(added)}, "
                           f"移除{len(removed)}只{list(removed)}, 当前监控{len(self.holdings)}只")
            else:
                logger.info(f"[盘中监控] 持仓字段更新（标的不变），当前监控{len(self.holdings)}只")

        except Exception as e:
            logger.warning(f"[盘中监控] 持仓热重载失败(不影响当前监控): {e}")

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

        # V9.2: 跨日自动清理日内状态（进程不重启时防止内存泄漏）
        self._daily_reset_if_new_day()

        # 获取实时行情
        quotes = self._get_realtime_quotes(list(self.holdings.keys()))
        if not quotes:
            return alerts

        # 获取大盘行情
        market_quote = self._get_index_quote()

        # ---- V9.1: 大盘门控（方案4）----
        # 大盘急跌/极端下跌时压制或延迟个股预警，避免"情绪冰点"发出不可靠信号
        gate_state = self._market_gate_check(market_quote)
        if gate_state == "suppress":
            logger.info("[大盘门控] 大盘极端下跌中，压制个股预警（避免情绪极值误报）")
            return alerts
        elif gate_state == "delay":
            # 大盘急跌中: warning/critical/emergency 风控信号立即发送，
            # 仅 info 级预警入队列延迟等待企稳后补发
            pass  # 在 _send_alert 中按 level 分流
        
        # V9.2: 门控/均值回归周期性存活日志（每20轮≈10分钟输出一次，便于确认功能存活）
        if not hasattr(self, '_gate_log_counter'):
            self._gate_log_counter = 0
        self._gate_log_counter += 1
        if self._gate_log_counter >= 20:
            mkt_chg = market_quote.get("change_pct", 0) if market_quote else 0
            logger.info(f"[门控状态] {gate_state} | 大盘{mkt_chg:+.2f}% | "
                       f"延迟队列{len(self._pending_alerts_queue)}条 | "
                       f"监控{len(self.holdings)}只 | 已发预警{len(self.alerts_sent)}条")
            self._gate_log_counter = 0

        # V9.1: 门控从 delay→pass 转换时，补发延迟队列中的预警
        if gate_state == "pass" and self._prev_gate_state == "delay" and self._pending_alerts_queue:
            self._dispatch_pending_alerts(quotes, today)
        self._prev_gate_state = gate_state

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
            # V9.2: 防止 0/负值 写入导致后续除零或反弹计算失效
            if day_low > 0 and (code not in self.day_lows or day_low < self.day_lows[code]):
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
                            alert["_alert_key"] = alert_key
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
                            alert["_alert_key"] = alert_key
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
                            alert["_alert_key"] = alert_key
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
                                alert["_alert_key"] = alert_key
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
                        alert["_alert_key"] = alert_key
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
                    # V9.1: 均值回归过滤器（方案1）
                    # 价格极端偏离VWAP时，止损信号可靠性极低，暂缓确认等待回归
                    is_mr_zone, z_score = self._is_mean_reversion_zone(code, current_price, quote)
                    if is_mr_zone and z_score < -2.0:
                        mr_key = f"{today}_{code}_mr_defer"
                        if mr_key not in self.alerts_sent:
                            loss_pct = round((current_price / buy_price - 1) * 100, 2)
                            alert = {
                                "level": "info",
                                "type": "均值回归暂缓止损",
                                "code": code,
                                "name": name,
                                "current_price": current_price,
                                "z_score": round(z_score, 2),
                                "message": f"💡 {name}({code}) 偏离VWAP达{z_score:.1f}倍ATR | "
                                          f"处于均值回归区域，止损信号暂缓 | "
                                          f"浮亏{loss_pct:.1f}% | 等待价格回归后再评估",
                                "time": now.strftime("%H:%M:%S"),
                            }
                            alerts.append(alert)
                            alert["_alert_key"] = mr_key
                            self.alerts_sent.add(mr_key)
                            self._send_alert(alert)
                        continue  # 本轮不确认止损，等待回归

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
                            alert["_alert_key"] = alert_key
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
                            alert["_alert_key"] = alert_key
                            self.alerts_sent.add(alert_key)
                            self._send_alert(alert)
                self.stop_loss_first_touch.pop(code, None)
                self.stop_touch_prices.pop(code, None)

            # ---- 检查2: 急跌预警（V9.3: 自适应阈值）----
            change_pct = quote.get("change_pct", 0)
            _rapid_drop_threshold = self._get_adaptive_rapid_drop_threshold(market_quote)
            if change_pct and change_pct < _rapid_drop_threshold:
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
                    alert["_alert_key"] = alert_key
                    self.alerts_sent.add(alert_key)
                    self._send_alert(alert)

            # ---- 检查3: 止盈位到达（V6.2: 阈值提高，回测10%/20%提醒过早）----
            if self.PROFIT_TARGET_ALERT and buy_price > 0:
                profit_pct = (current_price / buy_price - 1)
                highest = holding.get("highest", buy_price)
                drawdown_from_high = (current_price - highest) / highest if highest > 0 else 0

                # V6.2: 止盈提醒阈值从config读取（15%/25%，回测超半数提醒后继续上涨）
                _profit_levels = getattr(config, 'PROFIT_ALERT_LEVELS', [0.15, 0.25])

                # 第一止盈位: 浮盈≥15%（V6.2: 10%→15%）
                if len(_profit_levels) >= 1 and profit_pct >= _profit_levels[0]:
                    alert_key = f"{today}_{code}_profit_10"
                    if alert_key not in self.alerts_sent:
                        # FIX: 修复100股持仓减1/3取整为0的问题（建议卖出量不超持有股数）
                        sell_1_3 = min(shares, max(100, int(shares / 3 / 100) * 100)) if shares >= 100 else shares
                        alert = {
                            # V9.2 FIX: level从"info"提升为"high"，确保通过钉钉分级路由(ALERT_DINGTALK_MIN_LEVEL=high)
                            "level": "high",
                            "type": "止盈提醒",
                            "code": code,
                            "name": name,
                            "current_price": current_price,
                            "profit_pct": round(profit_pct * 100, 2),
                            "message": f"🎯 {name}({code}) 浮盈{profit_pct*100:.1f}%达第一止盈位({int(_profit_levels[0]*100)}%)！"
                                      f"建议卖出{sell_1_3}股(1/3)锁定利润",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                        }
                        alerts.append(alert)
                        alert["_alert_key"] = alert_key
                        self.alerts_sent.add(alert_key)
                        self._send_alert(alert)

                # 第二止盈位: 浮盈≥25%（V6.2: 20%→25%）
                if len(_profit_levels) >= 2 and profit_pct >= _profit_levels[1]:
                    alert_key = f"{today}_{code}_profit_20"
                    if alert_key not in self.alerts_sent:
                        # FIX: 修复100股持仓减1/3取整为0的问题（建议卖出量不超持有股数）
                        sell_1_3 = min(shares, max(100, int(shares / 3 / 100) * 100)) if shares >= 100 else shares
                        alert = {
                            # V9.2 FIX: level从"warning"提升为"high"，确保通过钉钉分级路由(ALERT_DINGTALK_MIN_LEVEL=high)
                            "level": "high",
                            "type": "止盈提醒",
                            "code": code,
                            "name": name,
                            "current_price": current_price,
                            "profit_pct": round(profit_pct * 100, 2),
                            "message": f"🎯 {name}({code}) 浮盈{profit_pct*100:.1f}%达第二止盈位({int(_profit_levels[1]*100)}%)！"
                                      f"建议再卖{sell_1_3}股(1/3)，已锁定大部分利润",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                        }
                        alerts.append(alert)
                        alert["_alert_key"] = alert_key
                        self.alerts_sent.add(alert_key)
                        self._send_alert(alert)

                # 回落止盈: V6.2提高门槛（浮盈>6%且从最高点回落超过阈值）
                _min_profit_dd = getattr(config, 'DRAWDOWN_PROFIT_MIN_PROFIT', 0.06)
                if profit_pct >= _min_profit_dd:
                    from config import DRAWDOWN_STOP
                    stock_type = holding.get("stock_type", "龙头稳健")
                    dd_threshold = DRAWDOWN_STOP.get(stock_type, DRAWDOWN_STOP.get("龙头稳健", 0.07))
                    if drawdown_from_high <= -dd_threshold:
                        alert_key = f"{today}_{code}_drawdown_profit"
                        if alert_key not in self.alerts_sent:
                            # FIX: 修复100股持仓减半取整为0的问题（建议卖出量不超持有股数）
                            sell_half = min(shares, max(100, int(shares * 0.5 / 100) * 100)) if shares >= 100 else shares
                            alert = {
                                # V9.2 FIX: level从"warning"提升为"high"，确保通过钉钉分级路由(ALERT_DINGTALK_MIN_LEVEL=high)
                                "level": "high",
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
                            alert["_alert_key"] = alert_key
                            self.alerts_sent.add(alert_key)
                            self._send_alert(alert)

            # ---- 检查4: 振幅异常（V6.2: 结合趋势位置过滤）----
            amplitude = quote.get("amplitude", 0)
            _amp_trend_filter = getattr(config, 'AMPLITUDE_TREND_FILTER', True)
            # V6.2: 仅当股价<MA20时预警（高位振幅可能是拉升，不预警）
            _ma20 = holding.get("ma20", 0)
            _below_ma20 = (_ma20 > 0 and current_price < _ma20) if _amp_trend_filter else True
            if amplitude and amplitude > self.AMPLITUDE_ALERT_PCT * 100 and _below_ma20:
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
                    alert["_alert_key"] = alert_key
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

        # ---- 检查8.6: V9.3 波动率突变检测（P0级新增）----
        # V10.3: 默认禁用（无实际参考价值），通过 VOLATILITY_REGIME_CONFIG.enabled 控制
        _vol_cfg = getattr(config, 'VOLATILITY_REGIME_CONFIG', {})
        if _vol_cfg.get('enabled', False):
            for code in list(self.holdings.keys()):
                if code == "000300":
                    continue
                name = self.holdings[code].get("name", code)
                vol_alerts = self._check_volatility_regime(code, name, today)
                alerts.extend(vol_alerts)

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
                    alert["_alert_key"] = alert_key
                    self.alerts_sent.add(alert_key)
                    self._send_alert(alert)

        # ---- V9.3: 多因子联合评分（贝叶斯融合近似）----
        # 同一标的同一轮触发≥2个不同类型warning → 信号共振 → 升级为critical
        fusion_alerts = self._fuse_alerts(alerts, today)
        alerts.extend(fusion_alerts)

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
        """发送预警通知

        V9.3: 恢复钉钉推送（critical/emergency立即推送，warning复用邮件判定逻辑）。
        通知策略: 钉钉(秒达) + 邮件(存档) 双通道冗余。
        scheduler统一链路仍保留作为补发兜底（含30min冷却+同标的合并）。

        V9.1: 大盘门控 — delay状态下非 critical 预警入队列延迟发送。
        """
        message = alert.get("message", "")
        level = alert.get("level", "info")

        # V9.1: 大盘门控 — delay状态下仅 info 级预警入队列延迟
        # FIX(2026-08-19): 原 level!="critical" 误将 warning(梯度减仓-5%)
        # 也延迟发送，导致风控预警被门控吞掉。修正为仅 info 级延迟，
        # warning/critical/emergency 均为风控信号，必须穿透门控立即发送。
        if (self._prev_gate_state == "delay" and level == "info"
                and not message.startswith("[延迟补发]")):
            alert_key = alert.get("_alert_key", "")
            if alert_key and alert_key not in self._queued_alert_keys:
                self._pending_alerts_queue.append((alert, alert_key))
                self._queued_alert_keys.add(alert_key)
                logger.info(f"[大盘门控] 大盘急跌中，{alert.get('type','')}预警延迟: "
                           f"{alert.get('name','')}({alert.get('code','')})")
            return  # 不实际发送

        logger.warning(f"[盘中预警-{level.upper()}] {message}")

        # V10.3: 标的级全局冷却（同一标的多类型预警合并，防轰炸）
        # critical/emergency不受限制（风控信号不可延迟）
        code = alert.get("code", "")
        if code and level not in ("critical", "emergency"):
            esc_cfg = getattr(config, 'INTRADAY_ESCALATION_CONFIG', {})
            global_cd_min = esc_cfg.get("code_global_cooldown_min", 15)
            last_time = self._code_last_alert_time.get(code)
            if last_time is not None:
                elapsed_min = (datetime.datetime.now() - last_time).total_seconds() / 60
                if elapsed_min < global_cd_min:
                    logger.info(f"[标的冷却] {code} {alert.get('type','')} 距上次预警仅{elapsed_min:.0f}分钟(<{global_cd_min}min)，合并跳过")
                    return
        # 记录本次预警时间（延迟到实际发送判定后，避免仅日志预警占用冷却槽位）
        _code_cooldown_armed = False

        # 企业微信推送
        if config.WECHAT_WORK_WEBHOOK:
            self._send_wechat(message, level)
            _code_cooldown_armed = True

        # V9.3: warning级通知判定（钉钉+邮件共用，只调用一次避免副作用重复）
        # _should_send_warning_email 有副作用（滑动窗口记录），必须缓存结果
        _warning_notify = False
        if level in ("warning", "emergency"):
            _warning_notify = self._should_send_warning_email(alert)

        # V9.3: 恢复钉钉推送（双通道冗余 — 钉钉秒达 + 邮件存档）
        # critical/emergency: 立即推送（风控信号不可延迟）
        # warning: 复用邮件判定逻辑，仅应发邮件的warning同步推钉钉
        # info: 不发钉钉（避免噪音）
        if config.DINGTALK_WEBHOOK:
            if level in ("critical", "emergency"):
                self._send_dingtalk(message, level)
                _code_cooldown_armed = True
            elif _warning_notify:
                self._send_dingtalk(message, level)
                _code_cooldown_armed = True

        # 邮件推送
        if config.EMAIL_SENDER and config.EMAIL_AUTH_CODE:
            if level == "critical":
                # critical 无冷却，立即发送
                self._send_email_alert(alert)
                _code_cooldown_armed = True
            elif _warning_notify:
                # 复用上方已判定的 _warning_notify 结果，避免重复调用
                self._send_warning_email(alert)
                _code_cooldown_armed = True

        # FIX(2026-08-24): 仅在实际发出通知后才占用标的冷却槽位——
        # 原实现无条件记录，导致仅日志类预警（如VWAP失守）占用槽位后，
        # 15分钟内同标的的必发风控预警（急跌/梯度减仓）被误拦截（实测复现）
        if code and _code_cooldown_armed:
            self._code_last_alert_time[code] = datetime.datetime.now()

        # 回调函数
        for callback in self.alert_callbacks:
            try:
                callback(alert)
            except Exception:
                pass

        # V9.3: 预警效果在线评估记录（warning+级别）
        if level in ("warning", "critical", "emergency"):
            try:
                if self._alert_evaluator is None:
                    from monitor.alert_evaluator import AlertEvaluator
                    self._alert_evaluator = AlertEvaluator()
                self._alert_evaluator.record_alert(alert)
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
            # V10.3: 关键词安全模式 —— 消息体不含DINGTALK_KEYWORD时自动加前缀，
            # 否则钉钉服务端拒收(errcode 310000)导致风控信号丢失（与wechat_notify一致）
            content = f"[交易系统预警] {message}"
            _kw = getattr(config, "DINGTALK_KEYWORD", "")
            if _kw and _kw not in content:
                content = f"[{_kw}] {content}"
            data = json.dumps({
                "msgtype": "text",
                "text": {"content": content}
            }).encode("utf-8")
            req = urllib.request.Request(
                config.DINGTALK_WEBHOOK,
                data=data,
                headers={"Content-Type": "application/json"}
            )
            # V10.3: 检查响应体errcode（钉钉返回HTTP200但errcode非0=拒收）
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_body = json.loads(resp.read().decode("utf-8"))
            if resp_body.get("errcode", 0) != 0:
                logger.warning(f"[盘中监控] 钉钉推送被拒: errcode={resp_body.get('errcode')} "
                               f"errmsg={resp_body.get('errmsg', '')[:60]}")
        except Exception as e:
            logger.warning(f"[盘中监控] 钉钉推送失败: {e}")

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

    # ============================================================
    # V9.3: warning级预警邮件发送（含冷却+升级机制）
    # ============================================================

    # 必须发送 warning 邮件的预警类型
    _WARNING_EMAIL_TYPES = {
        "急跌预警",        # 急跌≥3%（含5%+严重档）
        "梯度减仓",        # 任意档位
        "深亏标的每日提醒",  # 深亏每日汇总
        # V10.3: 波动率突变移出必发集（用户反馈无实际参考价值，且单日产生86条预警/40+封邮件）
    }
    # 条件性发送：已满足触发条件即发送
    _WARNING_CONDITIONAL_TYPES = {
        "抛压沉重",           # 外盘<40% 且 跌幅>3%
        "被动跌破(大盘联动)",  # 被动跌破
    }
    # 仅日志不邮件的类型
    _WARNING_LOG_ONLY_TYPES = {
        "托盘撤退", "VWAP失守", "振幅异常",
        "疑似洗盘", "V反保护", "假突破确认(洗盘)",
        "均值回归暂缓止损", "止损Buffer中", "止损解除(收回)",
        "开盘定性",
        "波动率突变",      # V10.3: 降为仅日志（即使重新启用检测也不发邮件/钉钉）
    }

    def _should_send_warning_email(self, alert: dict) -> bool:
        """V9.3: 判断 warning 级预警是否应发送邮件（含冷却+升级机制）

        规则:
          - 仅日志类型（托盘撤退/VWAP首次跌破等）不发邮件
          - 必发类型（急跌≥3%/梯度减仓/深亏）立即发送
          - 条件类型（抛压沉重/被动跌破）立即发送
          - 冷却: 同标的30分钟（抛压/VWAP 60分钟）只发一次
          - 升级: 30分钟内同标的累计≥3条warning → 升级为critical放行
        """
        code = alert.get("code", "")
        alert_type = alert.get("type", "")
        now = datetime.datetime.now()

        # ① 仅日志类型不发邮件
        if alert_type in self._WARNING_LOG_ONLY_TYPES:
            return False

        # ② 冷却检查（warning 默认 30 分钟，抛压/被动跌破 60 分钟）
        cooldown_min = 60 if alert_type in ("抛压沉重", "被动跌破(大盘联动)") else 30
        # V10.0 P0-①: 应用校准冷却因子
        if self._calibration and not self._calibration.get("observation_mode", True):
            cd_factor = self._calibration.get("cooldown_factors", {}).get(alert_type, 1.0)
            cooldown_min = int(cooldown_min * cd_factor)
        last_send = self._warning_email_cooldown.get(code)
        if last_send and (now - last_send).total_seconds() < cooldown_min * 60:
            # 冷却期内：记录到滑动窗口用于升级判定
            self._warning_count_window.setdefault(code, []).append(
                (now, alert_type)
            )
            # 升级判定：30分钟内同标的 ≥3 条 warning → 升级 critical
            window = self._warning_count_window.get(code, [])
            recent = [t for t in window
                      if (now - t[0]).total_seconds() < 30 * 60]
            if len(recent) >= 3:
                logger.warning(f"[预警升级] {code} 30分钟内{len(recent)}条warning"
                              f"→升级为critical")
                self._warning_count_window[code] = []  # 重置
                return True  # 升级后放行
            return False  # 冷却中，不发送

        # ③ 按类型判定
        if alert_type in self._WARNING_EMAIL_TYPES:
            return True
        if alert_type in self._WARNING_CONDITIONAL_TYPES:
            return True  # 已满足触发条件

        # ④ 未知类型默认不发（安全侧）
        return False

    def _send_warning_email(self, alert: dict):
        """V9.3: warning 级预警邮件发送（含冷却登记 + 滑动窗口清理）"""
        code = alert.get("code", "")
        now = datetime.datetime.now()
        try:
            from notify.email_notify import send_email
            name = alert.get("name", "")
            alert_type = alert.get("type", "")
            message = alert.get("message", "")

            # 汇总滑动窗口中的历史 warning（如有）
            window = self._warning_count_window.get(code, [])
            recent = [t for t in window if (now - t[0]).total_seconds() < 60 * 60]
            history_lines = ""
            if recent:
                history_lines = ("<p style='color:#FF8C00;font-size:13px'>"
                                 "📋 近1小时预警记录:</p><ul>")
                for t, typ in recent:
                    history_lines += f"<li>{t.strftime('%H:%M')} {typ}</li>"
                history_lines += "</ul>"

            subject = (f"[盘中预警] {alert_type} - "
                       f"{name}({code}) {now.strftime('%H:%M')}")
            html = f"""
            <div style="font-family:Microsoft YaHei;padding:20px">
                <h2 style="color:#FF8C00">⚠️ {alert_type}</h2>
                <p style="font-size:16px">{message}</p>
                {history_lines}
                <table style="border-collapse:collapse;margin:15px 0">
                    <tr><td style="padding:5px 15px;border:1px solid #eee"><b>股票</b></td>
                        <td style="padding:5px 15px;border:1px solid #eee">{name} ({code})</td></tr>
                    <tr><td style="padding:5px 15px;border:1px solid #eee"><b>时间</b></td>
                        <td style="padding:5px 15px;border:1px solid #eee">{now.strftime('%H:%M:%S')}</td></tr>
                </table>
                <p style="color:#999;font-size:12px">此邮件由盘中监控系统自动发送 | warning级预警</p>
            </div>"""
            send_email(subject, html)
            # 登记冷却
            self._warning_email_cooldown[code] = now
            # 清理滑动窗口
            self._warning_count_window[code] = []
            logger.info(f"[warning邮件] 已发送: {name}({code}) {alert_type}")
        except Exception as e:
            logger.debug(f"[warning邮件] 发送失败: {e}")

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
                alert["_alert_key"] = dl_key
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
            # FIX(2026-08-24): 暴跌减仓同样受cum_reduce封顶——同日三档梯度已全仓时不再重复发单，
            # 否则条件单合计超持仓（实测：6000股生成9000股卖单）
            reduce_shares = min(reduce_shares, shares - cum_reduce)
            if reduce_shares <= 0:
                logger.info(f"[梯度减仓] {code} 暴跌减仓跳过：梯度已减仓{cum_reduce}股，无可减仓位")
            else:
                cum_reduce += reduce_shares
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
    # V9.3: 自适应阈值引擎（P1-⑦ 波动率 regime 条件）
    # ============================================================

    def _get_adaptive_rapid_drop_threshold(self, market_quote: dict) -> float:
        """V9.3: 根据大盘波动率 regime 动态调整急跌阈值

        原理: 高波动环境下频繁触发急跌预警会产生大量误报，
              低波动环境下收紧阈值可提前捕捉风险。

        规则:
          - 高波动（大盘振幅>2% 或 跌幅>2%）: 放宽到 -4.0%
          - 低波动（大盘振幅<0.5% 且 跌幅<0.5%）: 收紧到 -2.5%
          - 正常: 保持 -3.0%

        返回: 急跌阈值（负数，如 -3.0 表示跌3%）
        """
        if not market_quote:
            return self.RAPID_DROP_PCT * 100  # 默认 -3.0

        mkt_amplitude = abs(market_quote.get("change_pct", 0))
        mkt_change = market_quote.get("change_pct", 0)

        # 高波动 regime: 大盘大跌或振幅极大
        if mkt_change < -2.0 or mkt_amplitude > 2.0:
            base_threshold = -4.0
        # 低波动 regime: 大盘平稳
        elif mkt_amplitude < 0.5 and abs(mkt_change) < 0.5:
            base_threshold = -2.5
        # 正常波动
        else:
            base_threshold = self.RAPID_DROP_PCT * 100  # -3.0

        # V10.0 P0-①: 应用校准因子（仅观察期外生效）
        if self._calibration and not self._calibration.get("observation_mode", True):
            factor = self._calibration.get("threshold_factors", {}).get("急跌预警", 1.0)
            base_threshold *= factor

        return base_threshold

    # ============================================================
    # V9.1: 大盘门控机制（方案4: 大盘急跌时延迟/压制个股预警）
    # ============================================================

    def _market_gate_check(self, market_quote: dict) -> str:
        """大盘门控: 根据大盘状态决定个股预警的发送策略

        返回:
            "pass"     — 正常发送所有预警
            "delay"    — 大盘急跌中，非 critical 预警入队列延迟
            "suppress" — 大盘极端下跌，完全压制个股预警（此时信号不可靠）

        设计逻辑:
          大盘急跌(-1%~-2.5%)时，个股普遍跟跌，此时发出的止损/减仓预警
          在大盘企稳后往往迅速失效（价格反弹），导致"卖在最低点"。
          通过门控延迟，等大盘企稳后再补发，显著提高预警有效性。
        """
        if not market_quote:
            return "pass"

        mkt_chg = market_quote.get("change_pct", 0)

        # 大盘极端下跌(<-2.5%): 完全压制
        # 此时所有个股预警都不可靠，且用户操作也大概率是错误的
        if mkt_chg <= -2.5:
            new_state = "suppress"
        # 大盘明显下跌(-1%~-2.5%): 延迟非紧急预警
        elif mkt_chg < -1.0:
            new_state = "delay"
        else:
            new_state = "pass"

        # V9.2: 门控状态转换日志（便于事后复盘门控行为）
        # FIX(2026-08-19): 不再在此处更新 _prev_gate_state —— 由 _check_all
        # 统一维护，否则 delay→pass 转换检测永远为 False（延迟队列永远不补发）
        if new_state != self._prev_gate_state:
            logger.info(f"[大盘门控] 状态转换: {self._prev_gate_state}→{new_state} "
                        f"(大盘涨跌{mkt_chg:+.2f}%)")

        return new_state

    # ============================================================
    # V9.1: 均值回归过滤器（方案1: ATR+VWAP Z-Score 检测极端偏离）
    # ============================================================

    def _is_mean_reversion_zone(self, code: str, current_price: float,
                                 quote: dict) -> tuple:
        """判断当前价格是否处于均值回归高概率区域

        使用 VWAP + ATR 计算标准化偏离度(Z-Score):
          Z = (价格 - VWAP) / ATR
          |Z| > 2 意味着价格偏离日内均价超过2倍真实波幅，回归概率极高

        返回:
            (is_zone: bool, z_score: float)
            is_zone=True 且 z_score<-2 → 极端低估，止损信号不可靠
            is_zone=True 且 z_score>2  → 极端高估，止盈信号可加强
        """
        vwap = quote.get("vwap", 0)
        if vwap <= 0 or current_price <= 0:
            return False, 0.0

        atr = self._get_atr_cached(code)
        if not atr or atr <= 0:
            return False, 0.0

        z_score = (current_price - vwap) / atr
        return (abs(z_score) > 2.0), z_score

    # ============================================================
    # V9.1: 延迟预警补发（大盘企稳后释放门控期间积压的预警）
    # ============================================================

    def _dispatch_pending_alerts(self, quotes: dict, today: str):
        """大盘企稳后补发延迟队列中的预警

        仅在 _market_gate_check 从 "delay" 转回 "pass" 时调用。
        补发前验证标的仍在持仓中且行情可用，过期预警直接丢弃。
        """
        if not self._pending_alerts_queue:
            return

        dispatched = 0
        for alert, alert_key in self._pending_alerts_queue:
            code = alert.get("code", "")
            if code not in quotes:
                continue
            # 已被正式流程确认/发送的跳过（缓冲期内价格回升等场景）
            if alert_key in self.alerts_sent and alert_key not in self._queued_alert_keys:
                continue

            # 补发时追加"延迟"标注，让用户知道这是之前积压的信号
            alert["message"] = f"[延迟补发] {alert.get('message', '')}"
            self.alerts_sent.add(alert_key)
            self._queued_alert_keys.discard(alert_key)
            self._send_alert(alert)
            dispatched += 1

        logger.info(f"[大盘门控] 大盘企稳，补发{dispatched}条延迟预警")
        self._pending_alerts_queue.clear()

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

            # 紧急: 跌>5% 或 触及止损（新恶化跌>5%不受降级抑制窗口限制）
            if change_pct < emerg_triggers.get("holding_drop_pct", -5.0):
                is_emergency = True
            if stop_loss > 0 and current_price > 0 and current_price <= stop_loss:
                if emerg_triggers.get("stop_loss_touched", True):
                    # FIX(2026-08-24): 降级抑制窗口内，仅止损价停滞不再回升emergency（防乒乓），
                    # 仅跌>5%/大盘暴跌等新恶化能突破抑制（风控不丢）
                    if (self._emergency_suppressed_until is not None
                            and datetime.datetime.now() < self._emergency_suppressed_until):
                        is_warning = True  # 降为warning级关注，不停止监控
                    else:
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
            # V10.3 FIX: 从状态文件恢复alert_level=emergency时_emergency_since为None，
            # 会导致变频降级保护永远不触发，此处补设起始时间
            if self.alert_level != "emergency" or self._emergency_since is None:
                self._emergency_since = datetime.datetime.now()
            self.alert_level = "emergency"
            self._clear_count = 0
            self._emergency_suppressed_until = None  # FIX: 新恶化突破抑制，清除窗口
        elif is_warning:
            self.alert_level = "warning"
            self._clear_count = 0
            self._emergency_since = None
        else:
            self._clear_count += 1
            downgrade_n = esc_cfg.get("downgrade_after_clear", 3)
            if self._clear_count >= downgrade_n:
                self.alert_level = "normal"
            self._emergency_since = None

        # V10.3: 紧急级别最大持续时长保护（防止因止损价持续低于而锁定emergency）
        if self.alert_level == "emergency" and self._emergency_since is not None:
            max_dur = esc_cfg.get("emergency_max_duration_min", 20)
            elapsed = (datetime.datetime.now() - self._emergency_since).total_seconds() / 60
            if elapsed > max_dur:
                # 检查是否有新的恶化（价格进一步下跌>2%）
                has_new_deterioration = False
                for code, holding in self.holdings.items():
                    if code not in quotes:
                        continue
                    quote = quotes[code]
                    change_pct = quote.get("change_pct", 0)
                    if change_pct < esc_cfg.get("emergency_triggers", {}).get("holding_drop_pct", -5.0):
                        has_new_deterioration = True
                        break
                if not has_new_deterioration:
                    logger.info(f"[变频降级] emergency持续{elapsed:.0f}分钟且无新恶化，自动降为warning")
                    self.alert_level = "warning"
                    # FIX(2026-08-24): 设抑制窗口——否则下轮扫描止损价仍低于又会升回emergency，
                    # 降级只生效一个周期（实测乒乓效应）。窗口内仅跌>5%/大盘暴跌可突破。
                    _suppress_min = esc_cfg.get("emergency_suppress_after_downgrade_min", 30)
                    self._emergency_suppressed_until = datetime.datetime.now() + \
                        datetime.timedelta(minutes=_suppress_min)
                    self._emergency_since = None

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

        # V9.2: 开盘15分钟静默期（09:30-09:45 VWAP不稳定，信号不可靠）
        _now = datetime.datetime.now()
        _open_time = _now.replace(hour=9, minute=30, second=0, microsecond=0)
        _quiet_end = _open_time + datetime.timedelta(
            minutes=self.vwap_cfg.get("quiet_minutes", 15))
        if _now < _quiet_end:
            return alerts  # 开盘静默期内不触发VWAP信号

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
                alert["_alert_key"] = alert_key
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
                    alert["_alert_key"] = alert_key
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
                alert["_alert_key"] = alert_key
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
                alert["_alert_key"] = alert_key
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
                alert["_alert_key"] = alert_key
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
                alert["_alert_key"] = alert_key
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
                alert["_alert_key"] = alert_key
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
                        alert["_alert_key"] = alert_key
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
                        alert["_alert_key"] = alert_key
                        self.alerts_sent.add(alert_key)
                        self._send_alert(alert)

        return alerts

    # ============================================================
    # V9.3: 波动率突变检测（基于分时价格序列的实现波动率 RV）
    # ============================================================

    def _check_volatility_regime(self, code: str, name: str, today: str) -> list:
        """V9.3: 波动率突变检测

        原理: 用 price_history 中的分时价格序列计算短期/长期实现波动率( RV)，
        当短期RV突破长期均值+2σ时，说明市场从“低波”切换到“高波”regime，
        需立即预警。

        参数:
          - 短期窗口: 20个价格点（10秒轮询≈3-7分钟）
          - 长期窗口: 60个价格点（10秒轮询≈10-20分钟）
          - 触发阈值: 短期RV > 长期均值 + 2倍标准差
          - 冷却: 同标的15分钟
        """
        alerts = []
        prices_raw = self.price_history.get(code, [])
        if len(prices_raw) < 30:  # 至少需要30个数据点（≈5分钟）
            return alerts

        # 冷却检查
        now = datetime.datetime.now()
        last_vol_alert = self._vol_alert_cooldown.get(code)
        if last_vol_alert and (now - last_vol_alert).total_seconds() < 15 * 60:
            return alerts

        # 提取价格序列
        prices = [p for _, p in prices_raw[-120:]]  # 取最近120个点
        if len(prices) < 30:
            return alerts

        # 计算收益率序列
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1])
        if len(returns) < 20:
            return alerts

        # 短期RV: 最近20个收益率的标准差 × √252（年化）
        import math
        short_window = min(20, len(returns))
        short_returns = returns[-short_window:]
        short_mean = sum(short_returns) / len(short_returns)
        short_var = sum((r - short_mean) ** 2 for r in short_returns) / max(len(short_returns) - 1, 1)
        short_rv = math.sqrt(short_var) * math.sqrt(252 * 24 * 6)  # 年化（10秒频率≈每天24*6*60=8640个）

        # 长期RV均值和标准差: 用滑动窗口计算
        long_window = min(60, len(returns))
        if long_window < 40:
            return alerts

        # 将长期数据分成多个20点的滑动窗口，计算每个窗口的RV
        rv_samples = []
        step = 5  # 滑动步长
        for start in range(0, long_window - short_window + 1, step):
            window_returns = returns[-(long_window) + start: -(long_window) + start + short_window]
            if len(window_returns) < short_window:
                continue
            w_mean = sum(window_returns) / len(window_returns)
            w_var = sum((r - w_mean) ** 2 for r in window_returns) / max(len(window_returns) - 1, 1)
            rv_samples.append(math.sqrt(w_var))

        if len(rv_samples) < 3:
            return alerts

        # 长期RV均值和标准差
        rv_mean = sum(rv_samples) / len(rv_samples)
        rv_var = sum((r - rv_mean) ** 2 for r in rv_samples) / max(len(rv_samples) - 1, 1)
        rv_std = math.sqrt(rv_var)

        # 短期RV（未年化）与长期比较
        short_rv_raw = math.sqrt(short_var)
        threshold = rv_mean + 2 * rv_std

        if short_rv_raw > threshold and rv_std > 0:
            # 波动率突变！
            z_score = (short_rv_raw - rv_mean) / rv_std if rv_std > 0 else 0
            current_price = prices[-1]
            alert_key = f"{today}_vol_regime_{code}_{int(now.timestamp() / 900)}"
            if alert_key not in self.alerts_sent:
                alert = {
                    "level": "warning",
                    "type": "波动率突变",
                    "code": code,
                    "name": name,
                    "current_price": current_price,
                    "message": (f"📊 {name}({code}) 波动率突变！"
                               f"短期RV={short_rv_raw*100:.2f}% > "
                               f"长期均值+2σ={threshold*100:.2f}% "
                               f"(Z={z_score:.1f})，"
                               f"市场可能进入高波regime，注意风控"),
                    "time": now.strftime("%H:%M:%S"),
                }
                alerts.append(alert)
                alert["_alert_key"] = alert_key
                self.alerts_sent.add(alert_key)
                self._vol_alert_cooldown[code] = now
                self._send_alert(alert)

        return alerts

    # ============================================================
    # V9.3: 多因子联合评分（贝叶斯融合近似）
    # ============================================================

    def _fuse_alerts(self, alerts: list, today: str) -> list:
        """V9.3: 多因子联合评分 — 同一标的同一轮触发多个warning时升级为critical

        原理: 当多个独立信号同时触发同一标的时，联合后验概率显著高于单一信号。
        近似贝叶斯融合: 2个独立warning联合 → P(风险) ≈ 1-(1-p1)*(1-p2) >> p1

        规则:
          - 同一标的同一轮 ≥2个不同类型 warning → 联合评分+15/个
          - ≥3个不同类型 → 联合评分+30
          - 联合评分 ≥30 → 生成 critical 级“多信号共振”预警
          - 已有 critical 的标的不重复升级
          - 冷却: 同标的30分钟
        """
        fusion_alerts = []
        now = datetime.datetime.now()

        # 按标的分组本轮 warning 级预警（排除已critical的）
        code_warnings = {}  # {code: [alert_types]}
        code_has_critical = set()
        for a in alerts:
            code = a.get("code", "")
            level = a.get("level", "info")
            alert_type = a.get("type", "")
            if not code or not alert_type:
                continue
            if level in ("critical", "emergency"):
                code_has_critical.add(code)
            elif level == "warning":
                code_warnings.setdefault(code, []).append(alert_type)

        for code, types in code_warnings.items():
            # 已有 critical 的标的不重复升级
            if code in code_has_critical:
                continue

            # 去重统计不同类型数量
            unique_types = list(set(types))
            n_types = len(unique_types)

            if n_types < 2:
                continue

            # 计算联合评分
            if n_types >= 3:
                score = 30 + (n_types - 3) * 10  # 3个=30, 4个=40, ...
            else:
                score = 15  # 2个=15

            if score < 30:
                continue  # 不足30分不升级

            # 冷却检查
            last_fusion = self._fusion_alert_cooldown.get(code)
            if last_fusion and (now - last_fusion).total_seconds() < 30 * 60:
                continue

            # 获取标的名称
            name = self.holdings.get(code, {}).get("name", code)

            # 生成“多信号共振”预警
            alert_key = f"{today}_fusion_{code}_{int(now.timestamp() / 1800)}"
            if alert_key not in self.alerts_sent:
                type_str = "/".join(unique_types[:4])  # 最多显示4个
                alert = {
                    "level": "critical",
                    "type": "多信号共振",
                    "code": code,
                    "name": name,
                    "message": (f"🚨 {name}({code}) 多信号共振！"
                               f"本轮触发{n_types}类warning[{type_str}]，"
                               f"联合评分{score}分，风险极高，建议立即评估减仓"),
                    "time": now.strftime("%H:%M:%S"),
                }
                fusion_alerts.append(alert)
                alert["_alert_key"] = alert_key
                self.alerts_sent.add(alert_key)
                self._fusion_alert_cooldown[code] = now
                self._send_alert(alert)

        return fusion_alerts

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
                    alert["_alert_key"] = alert_key
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
