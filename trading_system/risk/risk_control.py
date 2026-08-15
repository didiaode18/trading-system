# -*- coding: utf-8 -*-
"""
风控硬拦截层 V2.0
================
所有买卖指令必须先过风控，违规直接拦截，从代码层面管住手。

核心升级（对比V1）:
  - 三级仓位从"warning"升级为"硬拦截"
  - 总仓位与指数MA20/MA60动态绑定
  - 账户亏损熔断（日/周/连续笔数）
  - 浮亏加仓绝对拦截
  - 风控优先级高于策略逻辑，不通过直接淘汰

架构位置:
  信号生成 → 【风控硬拦截层】→ 通过 → 输出条件单/推荐
                              → 不通过 → 拦截 + 输出原因
"""

import datetime
import json
import os
import logging
# FIX: 清理 RiskGate 死代码，同步移除无引用的 Dict 导入
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# 延迟导入ATR止损函数（避免循环导入）
_adaptive_stop_imported = False
_calc_adaptive_stop_loss = None
_get_atr_value = None


def _ensure_adaptive_stop_import():
    """延迟导入ATR自适应止损函数"""
    global _adaptive_stop_imported, _calc_adaptive_stop_loss, _get_atr_value
    if not _adaptive_stop_imported:
        try:
            from strategy.position import calc_adaptive_stop_loss, _get_atr_value as _get_atr
            _calc_adaptive_stop_loss = calc_adaptive_stop_loss
            _get_atr_value = _get_atr
            _adaptive_stop_imported = True
        except ImportError:
            logger.debug("ATR自适应止损模块导入失败，将使用固定百分比止损")
            _adaptive_stop_imported = True  # 避免重复尝试

# ============================================================
# 风控参数（可配置，集中管理）
# ============================================================
RISK_CONFIG = {
    # --- 三级仓位硬限制 ---
    "etf_max_ratio": 0.20,          # 单只ETF仓位上限20%
    "stock_max_ratio": 0.15,        # 单只个股仓位上限15%
    "sector_max_ratio": 0.40,       # 单一赛道仓位上限40%
    "min_cash_ratio": 0.10,         # 最低现金保留10%

    # --- 总仓位动态上限（与指数均线绑定）---
    "total_above_ma20": 0.80,       # 指数在MA20上方 → 总仓位≤80%
    "total_below_ma20": 0.50,       # 指数跌破MA20 → 总仓位≤50%
    "total_below_ma60": 0.30,       # 指数跌破MA60 → 总仓位≤30%

    # --- 账户亏损熔断 ---
    "daily_loss_block": 0.03,       # 单日亏损≥3% → 当日禁止新开仓
    "weekly_loss_force": 0.08,      # 单周亏损≥8% → 强制降到30%以下
    "weekly_loss_pause_days": 3,    # 周熔断后暂停开仓天数
    "consecutive_loss_today": 2,    # 连续亏损2笔 → 暂停2天（V3.2回测优化: 原"当日禁止"升级为暂停2天）
    "consecutive_loss_block": 3,    # 连续亏损3笔 → 暂停5天+仓位×0.5恢复
    "consecutive_loss_pause": 5,    # 暂停天数（V3.2: 从3天升级为5天）
    "consecutive_loss_2_pause": 2,  # V3.2新增: 连亏2笔暂停天数

    # --- 浮亏加仓拦截 ---
    "block_add_on_loss": True,      # 浮亏>0时绝对禁止加仓

    # --- 交易行为管控 ---
    "max_daily_opens": 2,           # 单日开仓次数上限
    "cooldown_after_sell": 3,       # 卖出后N天内禁止重新买入
    "max_holdings": 7,              # 持仓数量硬限制

    # --- 盈亏比准入 ---
    "min_risk_reward": 2.5,         # 最低盈亏比（不达标拦截）
}

# 状态文件路径（记录熔断/冷却状态）
STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "output", "risk_state.json")


# ============================================================
# 风控状态管理
# ============================================================
class RiskStateManager:
    """
    风控状态管理器
    持久化记录: 连续亏损、熔断状态、冷却期、日交易计数
    """

    def __init__(self):
        self.state = self._load_state()

    def _load_state(self) -> dict:
        """加载持久化状态"""
        default = {
            "consecutive_losses": 0,        # 连续亏损笔数
            "pause_until": "",              # 暂停开仓截止日期
            "daily_open_count": 0,          # 今日开仓次数
            "daily_pnl": 0.0,              # 当日已实现盈亏
            "weekly_pnl": 0.0,             # 本周已实现盈亏
            "last_trade_date": "",          # 上次交易日期
            "sell_cooldown": {},            # {code: "解禁日期"} 卖出后冷却
            "weekly_force_reduce": False,   # 周熔断强制减仓标记
            "total_capital": getattr(config, 'TOTAL_CAPITAL', 424000),  # 总资金（FIX P3: 从 config 读取）
            "intraday_risk_flags": {},      # 盘中风险标记 {flag: {"time": ISO时间, "details": dict}}
            # FIX: 修复四级熔断(日/周/月/年)pnl数据源缺失问题 —— 总资产快照差分法的持久化字段
            "daily_start_total": 0.0,       # 当日起始总资产快照（daily_pnl = 当前总资产 - 本值）
            "weekly_start_total": 0.0,      # 本周起始总资产快照（weekly_pnl = 当前总资产 - 本值）
            "monthly_start_capital": getattr(config, 'TOTAL_CAPITAL', 424000),  # 月度回撤基准（首次快照时自动重置）
            "annual_start_capital": getattr(config, 'TOTAL_CAPITAL', 424000),   # 年度回撤基准（首次快照时自动重置）
            "snapshot_date": "",            # 日快照基准日期 yyyy-mm-dd
            "snapshot_week": "",            # 周快照基准 ISO周 yyyy-Www
            "snapshot_month": "",           # 月快照基准 yyyy-mm
            "snapshot_year": "",            # 年快照基准 yyyy
        }
        # FIX: 修复 _load_state 裸except吞没状态损坏异常的问题
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                default.update(saved)
        except Exception as e:
            logger.warning(f"风控状态文件损坏，使用默认状态: {e}")

        # 日期重置逻辑
        today = datetime.date.today().isoformat()
        if default["last_trade_date"] != today:
            default["daily_open_count"] = 0
            default["daily_pnl"] = 0.0
            default["last_trade_date"] = today

        return default

    def save(self):
        """持久化状态"""
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def record_loss(self):
        """记录一笔亏损"""
        self.state["consecutive_losses"] += 1
        self.save()

    def record_profit(self):
        """记录一笔盈利（重置连续亏损）"""
        self.state["consecutive_losses"] = 0
        self.save()

    def record_sell(self, code: str):
        """记录卖出（启动冷却期）"""
        unified_cfg = getattr(config, 'RISK_UNIFIED_CONFIG', RISK_CONFIG)
        cooldown_days = unified_cfg.get("cool_down_days", RISK_CONFIG.get("cooldown_after_sell", 3))
        unlock = (datetime.date.today() + datetime.timedelta(days=cooldown_days)).isoformat()
        self.state["sell_cooldown"][code] = unlock
        self.save()

    def record_open(self):
        """记录一次开仓"""
        self.state["daily_open_count"] += 1
        self.save()

    def is_paused(self) -> Tuple[bool, str]:
        """检查是否在暂停期"""
        pause_until = self.state.get("pause_until", "")
        if pause_until and datetime.date.today().isoformat() <= pause_until:
            return True, f"暂停开仓中（至{pause_until}）"
        return False, ""

    def is_cooling_down(self, code: str) -> Tuple[bool, str]:
        """检查某只标的是否在冷却期"""
        unlock = self.state.get("sell_cooldown", {}).get(code, "")
        if unlock and datetime.date.today().isoformat() < unlock:
            return True, f"{code}冷却期中（{unlock}解禁）"
        return False, ""

    # FIX P1: 渐进加仓恢复规则（深亏后禁止一次性满仓）
    # 3档恢复路径: 30% → 45% → 60% → 解除
    RECOVERY_TIERS = [
        {"max_position": 0.30, "condition": "深亏未恢复(浮亏>15%)"},
        {"max_position": 0.45, "condition": "本月已实现盈亏≥0"},
        {"max_position": 0.60, "condition": "连续2周盈利"},
        {"max_position": 1.00, "condition": "完全恢复"},
    ]

    def get_recovery_position_cap(self) -> Tuple[float, str]:
        """
        获取当前恢复阶段允许的仓位上限

        返回: (max_position_ratio, description)
        规则:
          - 总浮亏>15%: 最高30%仓位（当前状态）
          - 本月已实现盈亏≥0: 最高45%
          - 连续2周盈利: 最高60%
          - 否则: 不限制
        """
        total_capital = self.state.get("total_capital", 424000)
        monthly_start = self.state.get("monthly_start_capital", total_capital)

        # 计算当前回撤深度
        if monthly_start > 0:
            drawdown_from_month = (monthly_start - total_capital) / monthly_start
        else:
            drawdown_from_month = 0

        # Tier 0: 深亏未恢复
        if drawdown_from_month > 0.15 or total_capital < monthly_start * 0.85:
            return 0.30, f"深亏恢复期(回撤{drawdown_from_month*100:.1f}%), 仓位上饨30%"

        # Tier 1: 本月已实现盈亏≥0
        monthly_pnl = self.state.get("monthly_pnl", 0)
        weekly_pnl = self.state.get("weekly_pnl", 0)
        consecutive_profit_weeks = self.state.get("consecutive_profit_weeks", 0)

        if consecutive_profit_weeks >= 2:
            return 0.60, f"连续{consecutive_profit_weeks}周盈利, 仓位上饨60%"
        elif monthly_pnl >= 0 and drawdown_from_month <= 0.15:
            return 0.45, "本月盈亏持平, 仓位上饨45%"

        # 默认不限制
        return 1.00, "无恢复限制"

    def get_market_position_cap(self) -> Tuple[float, str]:
        """
        读取MarketRegimeDetector缓存，返回市场状态对应的仓位上限
        FIX P1: MarketRegime→执行层联动

        返回: (max_position_ratio, description)
        """
        regime_file = os.path.join(os.path.dirname(STATE_FILE), "market_regime_state.json")
        try:
            if os.path.exists(regime_file):
                with open(regime_file, "r", encoding="utf-8") as f:
                    regime = json.load(f)
                state = regime.get("state", "RANGE")
                max_pos = regime.get("position_advice", {}).get("max_position", 0.60)
                detail = regime.get("detail", "")
                # 缓存超过2天则不可信
                detect_time = regime.get("detect_time", "")
                if detect_time:
                    from datetime import datetime as _dt
                    try:
                        detect_dt = _dt.strptime(detect_time, "%Y-%m-%d %H:%M")
                        if (_dt.now() - detect_dt).days > 2:
                            return 1.00, "市场状态缓存过期(>2天)"
                    except ValueError:
                        pass
                return max_pos, f"市场{state}, 仓位上饨{max_pos:.0%}"
        except Exception as e:
            logger.debug(f"market_regime_state读取失败: {e}")
        return 1.00, "无市场状态限制"


# ============================================================
# 策略失效检测器（P0级风控增强）
# ============================================================

class StrategyFailureDetector:
    """
    策略失效检测器 V1.0
    
    基于滚动窗口胜率/期望收益的三级熔断机制：
      - 降级（DEGRADE）: 滚动20笔胜率 < 40% → 仓位减半
      - 暂停（PAUSE）:  滚动20笔胜率 < 30% → 禁止买入
      - 熔断（BREAKER）: 滚动20笔期望 < 0% → 全面暂停
    
    设计原理：
      回测显示最大连续亏损17次，说明策略在特定市场环境下会持续失效。
      与其等到连续亏损熔断（3笔暂停），不如提前通过滚动胜率检测策略退化。
    
    使用方式:
        detector = StrategyFailureDetector()
        detector.record_trade(profit_pct=2.5)  # 记录每笔交易
        status = detector.get_status()  # 获取当前状态
    """
    
    # 三级阈值（不修改config.py，使用类内默认值）
    WINDOW_SIZE = 20          # 滚动窗口大小
    DEGRADE_WIN_RATE = 40     # 胜率<40% → 降级（仓位减半）
    PAUSE_WIN_RATE = 30       # 胜率<30% → 暂停买入
    BREAKER_EXPECTANCY = 0    # 期望<0% → 全面熔断
    RECOVERY_TRADES = 5       # 恢复后前5笔仓位减半（试探性恢复）
    AUTO_RECOVERY_DAYS = 5    # V3.2: 暂停/熔断超过5天自动降级为degrade
    CONSEC_LOSS_BREAKER = 5   # V3.2: 连续5笔亏损直接熔断
    
    def __init__(self):
        self.state = self._load_state()
    
    def _load_state(self) -> dict:
        """加载持久化状态"""
        default = {
            "trade_history": [],       # 最近N笔交易收益 [{"pnl_pct": float, "date": str}]
            "current_level": "normal", # normal / degrade / pause / breaker
            "trades_since_recovery": 0, # 恢复后的交易计数
            "last_level_change": "",    # 上次状态变更日期
        }
        state_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "output", "strategy_failure_state.json")
        try:
            if os.path.exists(state_file):
                with open(state_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                default.update(saved)
        except Exception as e:
            logger.warning(f"策略失效状态文件损坏: {e}")
        default["_state_file"] = state_file
        return default
    
    def _save_state(self):
        """持久化状态"""
        state_file = self.state.get("_state_file", "")
        if not state_file:
            return
        save_data = {k: v for k, v in self.state.items() if not k.startswith("_")}
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)
    
    def record_trade(self, profit_pct: float, date: str = ""):
        """
        记录一笔交易结果，更新策略状态
        
        参数:
            profit_pct: 盈亏百分比（正=盈利，负=亏损）
            date: 交易日期
        """
        history = self.state["trade_history"]
        history.append({"pnl_pct": profit_pct, "date": date or datetime.date.today().isoformat()})
        
        # 保留最近WINDOW_SIZE笔
        if len(history) > self.WINDOW_SIZE:
            history = history[-self.WINDOW_SIZE:]
            self.state["trade_history"] = history
        
        # 更新状态
        self._update_level()
        self._save_state()
    
    def _update_level(self):
        """根据滚动窗口统计更新策略状态"""
        history = self.state["trade_history"]
        if len(history) < self.WINDOW_SIZE:
            # V3.2: 即使样本不足，连续5笔亏损也直接熔断
            if len(history) >= self.CONSEC_LOSS_BREAKER:
                recent = [t["pnl_pct"] for t in history[-self.CONSEC_LOSS_BREAKER:]]
                if all(p < 0 for p in recent):
                    if self.state["current_level"] != "breaker":
                        self.state["current_level"] = "breaker"
                        self.state["last_level_change"] = datetime.date.today().isoformat()
                        logger.warning(
                            f"[策略失效检测] 连续{self.CONSEC_LOSS_BREAKER}笔亏损，直接熔断"
                        )
            return
        
        pnls = [t["pnl_pct"] for t in history]
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100
        expectancy = sum(pnls) / len(pnls)
        
        old_level = self.state["current_level"]
        
        # V3.2: 连续5笔亏损加速熔断
        recent_5 = pnls[-self.CONSEC_LOSS_BREAKER:]
        all_loss_5 = all(p < 0 for p in recent_5)
        
        # 三级判定（从严到宽）
        if expectancy < self.BREAKER_EXPECTANCY or all_loss_5:
            new_level = "breaker"
        elif win_rate < self.PAUSE_WIN_RATE:
            new_level = "pause"
        elif win_rate < self.DEGRADE_WIN_RATE:
            new_level = "degrade"
        else:
            new_level = "normal"
        
        if new_level != old_level:
            self.state["current_level"] = new_level
            self.state["last_level_change"] = datetime.date.today().isoformat()
            if new_level == "normal" and old_level != "normal":
                # 恢复时设置试探期
                self.state["trades_since_recovery"] = 0
            logger.warning(
                f"[策略失效检测] 状态变更: {old_level} → {new_level} | "
                f"滚动{len(pnls)}笔胜率={win_rate:.1f}% 期望={expectancy:+.2f}%"
            )
    
    def get_status(self) -> dict:
        """
        获取当前策略状态
        
        返回:
            {
                "level": str,           # normal/degrade/pause/breaker
                "win_rate": float,      # 滚动胜率
                "expectancy": float,    # 滚动期望
                "position_scale": float,# 仓位缩放因子
                "allow_buy": bool,      # 是否允许买入
                "message": str,         # 描述信息
            }
        """
        history = self.state["trade_history"]
        level = self.state["current_level"]
        
        if len(history) < self.WINDOW_SIZE:
            return {
                "level": "normal", "win_rate": 0, "expectancy": 0,
                "position_scale": 1.0, "allow_buy": True,
                "message": f"样本积累中({len(history)}/{self.WINDOW_SIZE}笔)",
            }
        
        pnls = [t["pnl_pct"] for t in history]
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100
        expectancy = sum(pnls) / len(pnls)
        
        # 试探性恢复期仓位减半
        recovery_scale = 1.0
        if self.state.get("trades_since_recovery", 99) < self.RECOVERY_TRADES:
            recovery_scale = 0.5
        
        level_map = {
            "normal": {"scale": 1.0, "allow_buy": True, "msg": "策略正常运行"},
            "degrade": {"scale": 0.5, "allow_buy": True, "msg": f"策略降级: 胜率{win_rate:.0f}%<40%, 仓位减半"},
            "pause": {"scale": 0.0, "allow_buy": False, "msg": f"策略暂停: 胜率{win_rate:.0f}%<30%, 禁止买入"},
            "breaker": {"scale": 0.0, "allow_buy": False, "msg": f"策略熔断: 期望{expectancy:+.2f}%<0%, 全面暂停"},
        }
        info = level_map.get(level, level_map["normal"])
        
        return {
            "level": level,
            "win_rate": round(win_rate, 1),
            "expectancy": round(expectancy, 3),
            "position_scale": info["scale"] * recovery_scale,
            "allow_buy": info["allow_buy"],
            "message": info["msg"],
        }
    
    def check_buy_allowed(self) -> tuple:
        """
        检查是否允许买入（供UnifiedRiskEngine调用）
        
        V3.2增强: 暂停/熔断超过5天自动降级为degrade（允许半仓试探）
        
        返回: (allowed: bool, reason: str, position_scale: float)
        """
        # V3.2: 时间自动恢复 - 暂停/熔断超过5天降级为degrade
        self._try_auto_recovery()
        
        status = self.get_status()
        if not status["allow_buy"]:
            return False, f"[策略失效] {status['message']}", 0.0
        return True, "", status["position_scale"]

    def _try_auto_recovery(self):
        """V3.2: 暂停/熔断超过N天自动降级为degrade（避免永久锁死）"""
        level = self.state.get("current_level", "normal")
        if level not in ("pause", "breaker"):
            return
        last_change = self.state.get("last_level_change", "")
        if not last_change:
            return
        try:
            change_date = datetime.date.fromisoformat(last_change)
            days_elapsed = (datetime.date.today() - change_date).days
            if days_elapsed >= self.AUTO_RECOVERY_DAYS:
                self.state["current_level"] = "degrade"
                self.state["trades_since_recovery"] = 0
                self.state["last_level_change"] = datetime.date.today().isoformat()
                self._save_state()
                logger.info(
                    f"[策略失效检测] 自动恢复: {level}→degrade "
                    f"(已暂停{days_elapsed}天，允许半仓试探)"
                )
        except Exception:
            pass


# ============================================================
# P2: 动态熔断增强器
# ============================================================

class DynamicFuseEnhancer:
    """
    动态熔断增强器 V1.0
    
    增强现有固定阈值熔断机制，新增:
      1. 波动率自适应: 高波动时收紧熔断阈值，低波动时略微放宽
      2. 严重度分级暂停: 暂停天数与亏损严重度成正比
      3. 恢复渐进: 熔断解除后不立即恢复满仓，而是逐步放大
    
    设计原理:
      回测显示最大连续亏损13次，但固定3天暂停可能不足以覆盖
      持续熊市。动态熔断根据市场环境和亏损程度自动调整保护力度。
    """
    
    def __init__(self):
        self._fuse_history = []  # 熔断历史记录
        self._recovery_step = 1.0  # 恢复比例 (0.5=半仓恢复, 1.0=完全恢复)
        self._last_fuse_date = ""
    
    def get_volatility_adjustment(self, market_info: dict = None) -> dict:
        """
        根据市场波动率调整熔断参数
        
        参数:
            market_info: {"index_pct_change": float, "vix": float, "atr_pct": float}
        
        返回: 调整后的熔断参数
        """
        # 默认调整系数 = 1.0（不调整）
        vol_factor = 1.0
        
        if market_info:
            # 指数单日跌幅>2% → 高波动，收紧20%
            idx_chg = market_info.get("index_pct_change", 0)
            if idx_chg < -2:
                vol_factor = 0.8  # 收紧: 阈值*0.8
            elif idx_chg < -1:
                vol_factor = 0.9
            elif idx_chg > 1:
                vol_factor = 1.1  # 放宽: 阈值*1.1
            
            # ATR波动率调整
            atr_pct = market_info.get("atr_pct", 0)
            if atr_pct > 3:  # 指数ATR%>3 → 极端波动
                vol_factor *= 0.85
        
        # 计算调整后的阈值
        base_cfg = RISK_CONFIG
        adjusted = {
            "daily_loss_block": base_cfg["daily_loss_block"] * vol_factor,
            "weekly_loss_force": base_cfg["weekly_loss_force"] * vol_factor,
            "consecutive_loss_today": base_cfg["consecutive_loss_today"],
            "consecutive_loss_block": base_cfg["consecutive_loss_block"],
            "vol_factor": vol_factor,
        }
        return adjusted
    
    def calc_pause_days(self, consec_losses: int, daily_loss_pct: float = 0) -> int:
        """
        P2: 严重度分级暂停天数
        
        规则:
          - 3笔连续亏损: 暂停3天（基础）
          - 4笔: 5天
          - 5笔+: 7天
          - 单日亏损>4%: 额外+2天
        """
        base_pause = RISK_CONFIG.get("consecutive_loss_pause", 3)
        
        # 根据连续亏损笔数递增
        if consec_losses >= 5:
            pause = 7
        elif consec_losses >= 4:
            pause = 5
        else:
            pause = base_pause
        
        # 单日亏损严重度加成
        if daily_loss_pct > 4:
            pause += 2
        elif daily_loss_pct > 3:
            pause += 1
        
        return pause
    
    def get_recovery_scale(self) -> float:
        """
        P2: 熔断解除后的恢复比例
        
        规则:
          - 熔断解除后第1笔: 仓位*0.5（半仓试探）
          - 第1笔盈利: 恢复1.0
          - 第1笔亏损: 继续保持0.5
        """
        return self._recovery_step
    
    def on_fuse_triggered(self, reason: str, consec_losses: int):
        """熔断触发时调用"""
        self._fuse_history.append({
            "date": datetime.date.today().isoformat(),
            "reason": reason,
            "consec_losses": consec_losses,
        })
        self._recovery_step = 0.5  # 熔断后恢复时半仓开始
        self._last_fuse_date = datetime.date.today().isoformat()
    
    def on_recovery_trade(self, is_profit: bool):
        """熔断解除后的恢复交易结果"""
        if is_profit:
            self._recovery_step = 1.0  # 盈利后完全恢复
        # 亏损则继续保持0.5


# FIX: 清理 RiskGate 死代码（V2.0已废弃类，grep验证全库零调用，由 UnifiedRiskEngine 取代）

# ============================================================
# 便捷函数（供报告生成器/条件单调用）
# ============================================================
def quick_risk_check(signal: dict, holdings: dict,
                     total_capital: float = None,
                     market_info: dict = None) -> dict:
    """
    快速风控校验（一行调用）

    用法:
        from risk.risk_control import quick_risk_check
        result = quick_risk_check(signal, holdings)
        if not result["pass"]:
            print(f"被拦截: {result['reason']}")
    """
    engine = UnifiedRiskEngine(total_capital=total_capital)
    return engine.check_buy(signal, holdings, market_info)


def quick_inspect(holdings: dict, total_capital: float = None) -> List[dict]:
    """快速持仓巡检"""
    engine = UnifiedRiskEngine(total_capital=total_capital)
    return engine.inspect_holdings(holdings)


# ============================================================
# V4.0 (G4): 条件单下单前风控硬校验（pre-trade check）
# ============================================================

def pre_trade_check_orders(orders: list, positions: dict,
                           total_capital: float, cfg: dict = None) -> list:
    """
    V4.0(G4): 对已生成的条件单做下单前硬校验（纯函数、无状态副作用，可安全重复调用）

    依据 config 三级仓位硬限制对"买入"条件单做事前拦截，
    避免条件单成交后突破仓位约束（卖出/止损类条件单永不拦截）。

    检查项:
      1. 总仓位 ≥ 80%（NEAR_FULL_POSITION）→ 禁止任何新开仓
      2. 成交后单票仓位超限（个股15%/ETF20%）
      3. 成交后单一赛道仓位超限（40%）
      4. 成交后现金比例低于最低保留（10%）

    参数:
        orders: 条件单dict列表（需含 "方向"/"证券代码"/"证券名称"/"触发价"/"数量"）
        positions: {code: {"market_value": float, "sector": str, "is_etf": bool}} 持仓快照
        total_capital: 总资金
        cfg: 可选风控参数覆盖，默认用 RISK_CONFIG

    返回:
        被拦截订单列表 [{"code","name","reason"}]；被拦截的order原地标记
        blocked=True + block_reason，调用方据此从执行JSON中剔除并展示警示
    """
    cfg = cfg or RISK_CONFIG
    blocked_list = []
    if total_capital <= 0:
        return blocked_list

    total_mv = sum(p.get("market_value", 0) for p in positions.values())
    near_full = getattr(config, 'NEAR_FULL_POSITION', 0.80)
    min_cash = cfg.get("min_cash_ratio", 0.10)

    for order in orders:
        if order.get("方向") != "买入":
            continue
        code = order.get("证券代码", "")
        name = order.get("证券名称", code)
        amount = float(order.get("触发价", 0) or 0) * float(order.get("数量", 0) or 0)
        if amount <= 0:
            continue

        reasons = []
        # 关卡1: 总仓位近满仓 → 只允许减仓不允许新开仓
        if total_mv / total_capital >= near_full:
            reasons.append(f"总仓位已达{total_mv / total_capital:.0%}≥{near_full:.0%}，禁止新开仓")
        # 关卡2: 单票仓位上限
        pos = positions.get(code, {})
        is_etf = bool(pos.get("is_etf"))
        single_max = cfg.get("etf_max_ratio", 0.20) if is_etf else cfg.get("stock_max_ratio", 0.15)
        after_ratio = (pos.get("market_value", 0) + amount) / total_capital
        if after_ratio > single_max + 1e-9:
            kind = "ETF" if is_etf else "个股"
            reasons.append(f"成交后{kind}单票仓位{after_ratio:.1%}超上限{single_max:.0%}")
        # 关卡3: 赛道仓位上限
        sector = pos.get("sector") or ""
        if sector:
            sector_mv = sum(p.get("market_value", 0) for p in positions.values()
                            if p.get("sector") == sector)
            sector_max = cfg.get("sector_max_ratio", 0.40)
            if (sector_mv + amount) / total_capital > sector_max + 1e-9:
                reasons.append(f"成交后[{sector}]赛道仓位超上限{sector_max:.0%}")
        # 关卡4: 最低现金保留
        cash_after = total_capital - total_mv - amount
        if cash_after < min_cash * total_capital:
            reasons.append(f"成交后现金比例低于{min_cash:.0%}最低保留")

        if reasons:
            reason_txt = "；".join(reasons)
            order["blocked"] = True
            order["block_reason"] = reason_txt
            blocked_list.append({"code": code, "name": name, "reason": reason_txt})
            logger.warning(f"[pre-trade] ⛔ {code} {name} 买入单被风控拦截: {reason_txt}")

    return blocked_list


# ============================================================
# 逆势加仓（回调加仓）风控检查 V1.0（2026-08-07）
# ============================================================
def check_pullback_add_risk(code: str, holdings: dict, df,
                            market_state: str = "neutral",
                            market_drop_pct: float = 0.0) -> dict:
    """
    逆势加仓风控检查（持仓股回调加仓专用）

    核心逻辑:
      对已持仓的强势股，判断当前回调是否属于"正常回调"而非"趋势反转"，
      通过则返回允许加仓的数量和止损价。

    硬否决条件（满足任一即拒绝）:
      1. 大盘跌>1.5%（系统性风险）
      2. 非持仓股
      3. 浮亏>8%（深套锁）
      4. 回调深度>12%（趋势可能反转）
      5. 回调深度<3%（噪音，未进入加仓区间）
      6. 未缩量企稳（量>均量60%）
      7. 跌破关键支撑超3%（破位）
      8. 单票仓位>30%（集中度超限）
      9. MA20在MA60下方（中期趋势向下）
      10. MA20斜率向下（均线拐头）

    参数:
        code: 股票代码
        holdings: 持仓字典 {code: {shares, buy_price, ...}}
        df: 日K线DataFrame（需含 close/high/volume/ma20/ma60/ma20_slope 列）
        market_state: 大盘状态 ("up"/"neutral"/"weak"/"down")
        market_drop_pct: 大盘当日涨跌幅 (小数，如 -0.015)

    返回:
        {"pass": bool, "reason": str, "add_shares": int,
         "stop_loss": float, "pullback_pct": float,
         "signal_type": str}  # "pullback_ma20" / "oversold_rebound"
    """
    import config
    cfg = getattr(config, 'PULLBACK_ADD_CONFIG', {})
    if not cfg.get("enabled", True):
        return {"pass": False, "reason": "回调加仓功能未启用", "add_shares": 0}

    # ---- 1. 大盘过滤 ----
    if market_state not in cfg.get("market_state_allow", {"up", "neutral"}):
        return {"pass": False, "reason": f"大盘状态[{market_state}]不允许逆势加仓", "add_shares": 0}
    if market_drop_pct <= cfg.get("market_drop_forbidden", -0.015):
        return {"pass": False, "reason": f"大盘跌{market_drop_pct:.1%}，系统性风险锁", "add_shares": 0}

    # ---- 2. 持仓检查 ----
    if code not in holdings:
        return {"pass": False, "reason": "非持仓股，不适用回调加仓", "add_shares": 0}
    pos = holdings[code]
    shares = pos.get("shares", 0)
    buy_price = pos.get("buy_price", 0) or pos.get("cost", 0)
    if shares <= 0 or buy_price <= 0:
        return {"pass": False, "reason": "持仓数据异常", "add_shares": 0}

    # ---- 3. 数据完整性 ----
    if df is None or len(df) < 25:
        return {"pass": False, "reason": "K线数据不足", "add_shares": 0}

    latest = df.iloc[-1]
    current_price = latest["close"]
    pnl_pct = current_price / buy_price - 1 if buy_price > 0 else 0

    # ---- 4. 浮亏>8%禁止（深套锁）----
    if pnl_pct < -0.08:
        return {"pass": False, "reason": f"浮亏{pnl_pct:.1%}>8%，深套锁生效", "add_shares": 0}

    # ---- 5. 趋势前置条件 ----
    ma20 = latest.get("ma20", 0)
    ma60 = latest.get("ma60", 0)
    ma20_slope = latest.get("ma20_slope", 0)
    if cfg.get("require_ma20_above_ma60", True) and ma20 > 0 and ma60 > 0:
        if ma20 < ma60:
            return {"pass": False, "reason": f"MA20({ma20:.2f})<MA60({ma60:.2f})，中期趋势向下", "add_shares": 0}
    if cfg.get("require_ma20_slope_up", True) and ma20_slope is not None:
        if ma20_slope <= 0:
            return {"pass": False, "reason": f"MA20斜率{ma20_slope:.3f}<=0，均线拐头", "add_shares": 0}

    # ---- 6. 回调深度检查 ----
    high_since_buy = df["high"].iloc[-20:].max() if len(df) >= 20 else df["high"].max()
    pullback_pct = current_price / high_since_buy - 1 if high_since_buy > 0 else 0
    min_pb = cfg.get("min_pullback_pct", -0.03)
    max_pb = cfg.get("max_pullback_pct", -0.12)
    if pullback_pct > min_pb:
        return {"pass": False, "reason": f"回调{pullback_pct:.1%}未达加仓区间(>{abs(min_pb):.0%})", "add_shares": 0}
    if pullback_pct < max_pb:
        return {"pass": False, "reason": f"回调{pullback_pct:.1%}过深(<{abs(max_pb):.0%})，趋势可能反转", "add_shares": 0}

    # ---- 7. 缩量企稳确认 ----
    vol = latest.get("volume", 0)
    vol_ma20 = df["volume"].rolling(20).mean().iloc[-1] if len(df) >= 20 else 0
    vol_shrink_threshold = cfg.get("pullback_vol_shrink", 0.60)
    if vol_ma20 > 0 and vol > vol_ma20 * vol_shrink_threshold:
        return {"pass": False, "reason": f"量比{vol/vol_ma20:.0%}>={vol_shrink_threshold:.0%}，未缩量企稳", "add_shares": 0}

    # ---- 8. 关键支撑检查 ----
    support_col = cfg.get("support_ma", "ma20")
    support_val = latest.get(support_col, 0)
    support_tol = cfg.get("support_tolerance", 0.01)
    max_below = cfg.get("max_below_support_pct", -0.03)
    if support_val > 0:
        below_pct = current_price / support_val - 1
        if below_pct < max_below:
            return {"pass": False, "reason": f"跌破{support_col}支撑{below_pct:.1%}>({-max_below:.0%})，破位", "add_shares": 0}

    # ---- 9. 仓位上限 ----
    current_weight = pos.get("weight", 0)
    if current_weight <= 0:
        # 尝试估算
        total_mv = sum(h.get("shares", 0) * h.get("current_price", 0) for h in holdings.values())
        if total_mv > 0:
            current_weight = (shares * current_price) / total_mv
    max_weight = cfg.get("max_single_weight", 0.30)
    if current_weight > max_weight:
        return {"pass": False, "reason": f"仓位{current_weight:.0%}>{max_weight:.0%}，集中度超限", "add_shares": 0}

    # ---- 10. 计算加仓量 ----
    add_pct = cfg.get("max_add_pct_first", 0.50)
    add_shares = int(shares * add_pct / 100) * 100
    add_shares = max(add_shares, 100)

    # 止损价
    stop_loss_pct = cfg.get("stop_loss_pullback", -0.03)
    if support_val > 0:
        stop_loss = support_val * (1 + stop_loss_pct)
    else:
        stop_loss = current_price * (1 + stop_loss_pct)

    # 判定信号类型
    signal_type = "pullback_ma20"
    rsi = latest.get("rsi", 50)
    boll_lower = latest.get("boll_lower", 0)
    if rsi < 30 and boll_lower > 0 and current_price <= boll_lower * 1.01:
        signal_type = "oversold_rebound"
        stop_loss_pct_os = cfg.get("stop_loss_oversold", -0.05)
        stop_loss = current_price * (1 + stop_loss_pct_os)

    return {
        "pass": True,
        "reason": f"回调{pullback_pct:.1%}企稳，缩量触及{support_col}支撑，允许逆势加仓",
        "add_shares": add_shares,
        "stop_loss": round(stop_loss, 2),
        "pullback_pct": round(pullback_pct, 4),
        "signal_type": signal_type,
    }


# ============================================================
# FIX: 统一止损价口径 —— 权威源盘后写回 holdings.json
# ============================================================
def sync_authoritative_stop_loss(holdings_file: str = None) -> dict:
    """
    FIX: 修复6套止损计算并存且无统一写回，导致同一持仓在盘中执行、
    盘后报告、信号判断中看到不同止损线且互相矛盾的问题。

    指定 strategy.trend_strategy.compute_trailing_stop（信号侧在用的
    阶梯式移动止损）为唯一权威源，重算每只持仓止损价并写回
    holdings.json 的 stop_loss 字段。

    遵循止损线 Ratchet 原则：新值严格大于现有 stop_loss 才写入
    （只升不降，除非持仓重建）。
    写回采用临时文件+os.replace 原子替换；任何失败均降级跳过，
    不影响巡检报告。

    参数:
        holdings_file: 持仓文件路径（默认 config.get_holdings_file()，
                       测试可注入临时副本）
    返回:
        {"updated": n, "skipped": n, "failed": bool, "details": [(code, old, new)]}
    """
    result = {"updated": 0, "skipped": 0, "failed": False, "details": []}
    try:
        import config
        if holdings_file is None:
            holdings_file = config.get_holdings_file()
        if not os.path.exists(holdings_file):
            return result
        with open(holdings_file, "r", encoding="utf-8") as f:
            holdings = json.load(f)
        if not isinstance(holdings, dict):
            return result

        # 延迟导入权威源（避免循环导入）
        from strategy.trend_strategy import compute_trailing_stop

        changed = False
        for code, info in holdings.items():
            if not isinstance(info, dict) or info.get("shares", 0) <= 0:
                result["skipped"] += 1
                continue
            buy_price = info.get("buy_price", 0) or info.get("cost", 0)
            current_price = info.get("current_price", 0) or info.get("price", 0)
            # 成本价异常（摊薄成本/除权前旧价）跳过，与 config.load_holdings 校验口径一致
            if buy_price <= 0 or current_price <= 0 or buy_price > current_price * 5:
                result["skipped"] += 1
                continue
            try:
                new_stop = compute_trailing_stop(buy_price, current_price)
            except Exception:
                result["skipped"] += 1
                continue
            old_stop = info.get("stop_loss", 0) or 0
            # Ratchet: 仅当新值严格大于旧值才写回（止损只升不降）
            if new_stop > old_stop:
                info["stop_loss"] = new_stop
                changed = True
                result["updated"] += 1
                result["details"].append((code, old_stop, new_stop))
            else:
                result["skipped"] += 1

        if changed:
            tmp_file = holdings_file + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(holdings, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, holdings_file)
            logger.info(
                f"[止损统一] 权威止损写回 {result['updated']} 只 → {holdings_file}"
            )
    except Exception as e:
        # FIX: 写回失败降级跳过，不影响巡检报告
        result["failed"] = True
        logger.warning(f"[止损统一] 写回失败已降级: {e}")
    return result


# ============================================================
# 统一风控引擎（合并 V2.0 RiskGate + V3.0 risk_check）
# ============================================================

class UnifiedRiskEngine:
    """
    统一风控引擎 U0 —— 系统中唯一有效的风控引擎

    整合 RiskGate(V2.0) 与 risk_check(V3.0) 两套系统的优点：
      - 从 RiskGate 继承：状态持久化（JSON文件）、10级关卡检查链
      - 从 risk_check 继承：函数式调用接口、与 main.py 的兼容性
      - 统一参数源：全部从 config.RISK_UNIFIED_CONFIG 读取

    使用方式（两种接口均可）:
        # 接口 1：兼容 risk_check()
        engine = UnifiedRiskEngine(total_capital=737834)
        result = engine.check(trade_plan, risk_state, market_strength)

        # 接口 2：兼容 RiskGate
        result = engine.check_buy(signal, holdings, market_info)
    """

    _instance = None  # 单例，保证状态一致

    def __new__(cls, total_capital: float = None, allowed_pool: set = None):
        # 单例模式：确保全局只有一个引擎实例（状态持久化一致）
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, total_capital: float = None, allowed_pool: set = None):
        if self._initialized:
            # 允许更新资金和股票池
            if total_capital is not None:
                self.total_capital = total_capital
                self.state_mgr.state["total_capital"] = total_capital
            if allowed_pool is not None:
                self.allowed_pool = allowed_pool
            return
        self._initialized = True

        self.cfg = getattr(config, 'RISK_UNIFIED_CONFIG', {})
        self.total_capital = total_capital or getattr(config, 'TOTAL_CAPITAL', 424000)
        self.state_mgr = RiskStateManager()
        self.state_mgr.state["total_capital"] = self.total_capital
        self.allowed_pool = allowed_pool  # None=不限制, set=只允许池内标的
        # P0: 策略失效检测器（滚动20笔胜率三级熔断）
        self.failure_detector = StrategyFailureDetector()
        # P2: 动态熔断增强器（波动率自适应+严重度分级+恢复渐进）
        self.fuse_enhancer = DynamicFuseEnhancer()
        # 加载盘中风险标记并执行盘后自动过期
        self._load_intraday_flags()
        self._expire_intraday_flags()

    @classmethod
    def reset_instance(cls):
        """重置单例（仅用于测试）"""
        cls._instance = None

    # --------------------------------------------------------
    # 接口 1：兼容 risk_check() 的调用方式
    # --------------------------------------------------------
    def check(self, trade_plan: dict, risk_state,
              market_strength: str = "normal",
              vol_scale: float = 1.0) -> dict:
        """
        统一风控校验入口（兼容 risk_check 签名）

        参数:
            trade_plan: {"code", "action", "price", "shares", "sector",
                         "stock_type", "stop_loss"}
            risk_state: RiskState 实例（含 current_positions, daily_pnl 等）
            market_strength: "strong" / "normal" / "weak"
            vol_scale: 波动率目标缩放因子（>1允许加仓，<1需减仓），默认1.0不影响

        返回:
            {"pass": bool, "level": str, "reasons": list,
             "adjusted_shares": int, "warnings": list}
        """
        try:
            return self._do_check(trade_plan, risk_state, market_strength, vol_scale=vol_scale)
        except Exception as e:
            logger.exception(f"[UnifiedRiskEngine] 引擎异常: {e}")
            return {
                "pass": False,
                "level": "red",
                "reasons": [f"ENGINE_ERROR: 风控引擎异常，暂停交易 ({e})"],
                "adjusted_shares": 0,
                "warnings": []
            }

    def _do_check(self, trade_plan: dict, risk_state,
                  market_strength: str = "normal",
                  vol_scale: float = 1.0) -> dict:
        """内部校验逻辑（异常由外层 check() 捕获）"""
        cfg = self.cfg
        result = {
            "pass": True,
            "level": "green",
            "reasons": [],
            "adjusted_shares": trade_plan.get("shares", 0),
            "warnings": []
        }

        action = trade_plan.get("action", "buy")
        # FIX: 修复check()卖出路径不更新风控状态，冷却期和连续亏损计数失效
        if action == "sell":
            code = trade_plan.get("code", "")
            pnl = trade_plan.get("pnl", 0)
            self.state_mgr.record_sell(code)
            if pnl < 0:
                self.state_mgr.record_loss()
            else:
                self.state_mgr.record_profit()
            result["reasons"].append("卖出信号，直接通过")
            return result

        # 同步 RiskState 数据到状态管理器
        self._sync_risk_state(risk_state)

        # ===== 关卡-2: 黑天鹅/极端行情检测（V3.2新增，最高优先级）=====
        # 指数单日跌>3% / 千股跌停 / 连续暴跌 → 全面禁止开仓
        # FIX P0: market_info未定义导致每次买入校验抛NameError
        market_info = trade_plan.get("market_info") or {}
        black_swan = self._check_black_swan(market_info)
        if black_swan:
            return black_swan

        # ===== 关卡-1: 盘中风险标记检查（最高优先级）=====
        self._expire_intraday_flags()  # 每次检查前先清理过期标记
        intraday_block = self._check_intraday_risk_flags()
        if intraday_block:
            return intraday_block

        # ===== 关卡-0.5: 策略失效检测（P0级增强）=====
        # 滚动20笔胜率<30%禁止买入，期望<0%全面熔断
        sf_allowed, sf_reason, sf_scale = self.failure_detector.check_buy_allowed()
        if not sf_allowed:
            return self._block_result(sf_reason)

        code = trade_plan.get("code", "")
        price = trade_plan.get("price", 0)
        shares = trade_plan.get("shares", 0)

        # FIX P1: 策略降级时强制缩放仓位（degrade=0.5, 恢复期=0.5）
        if sf_scale < 1.0 and shares > 0:
            scaled = max(100, int(shares * sf_scale // 100) * 100)
            result["warnings"].append(
                f"[策略失效缩放] scale={sf_scale:.1f}, {shares}→{scaled}股"
            )
            shares = scaled
            trade_plan = {**trade_plan, "shares": scaled}
            result["adjusted_shares"] = scaled
        sector = trade_plan.get("sector", "")
        stock_type = trade_plan.get("stock_type", "龙头")
        amount = shares * price
        positions = risk_state.current_positions if hasattr(risk_state, 'current_positions') else {}
        # FIX: 修复 total_capital 取自内存 RiskState(静态config值)导致月/年度回撤基准与当前值同源恒差0的问题
        # 改为从快照/持仓动态计算，config.TOTAL_CAPITAL 仅作兜底
        total_capital = self.total_capital

        # ===== 关卡0: 满仓禁止加仓（情绪化交易防护，最高优先级）=====
        full_threshold = cfg.get("full_position_threshold",
                                  getattr(config, 'FULL_POSITION_THRESHOLD', 0.90))
        near_full = cfg.get("near_full_position",
                            getattr(config, 'NEAR_FULL_POSITION', 0.80))
        current_position_ratio = self._calc_position_ratio(positions, total_capital)
        available_cash = getattr(config, 'AVAILABLE_CASH', 0)

        if current_position_ratio >= full_threshold:
            return self._block_result(
                f"★满仓禁止: 当前仓位{current_position_ratio:.1%} >= {full_threshold:.0%}红线，"
                f"绝对禁止买入/加仓（情绪化交易防护）"
            )

        if available_cash < amount and available_cash < price * 100:
            return self._block_result(
                f"资金不足: 可用{available_cash:.0f}元 < 最低买入{price*100:.0f}元，禁止买入"
            )

        if current_position_ratio >= near_full and code not in positions:
            return self._block_result(
                f"仓位过高: 当前{current_position_ratio:.1%} >= {near_full:.0%}，"
                f"禁止新开仓（只允许减仓）"
            )

        # ===== 关卡0.8: 渐进加仓恢复约束（FIX P1）=====
        # 深亏后禁止一次性满仓，必须分档恢复
        recovery_cap, recovery_desc = self.state_mgr.get_recovery_position_cap()
        if recovery_cap < 1.0:
            # 买入后的预期仓位
            new_position_ratio = (current_position_ratio * total_capital + amount) / total_capital
            if new_position_ratio > recovery_cap:
                # 计算允许的最大买入金额
                allowed_amount = max(0, recovery_cap * total_capital - current_position_ratio * total_capital)
                allowed_shares = int(allowed_amount / price / 100) * 100 if price > 0 else 0
                if allowed_shares < 100:
                    return self._block_result(
                        f"[渐进加仓] {recovery_desc}，"
                        f"当前仓位{current_position_ratio:.1%}已达上饨{recovery_cap:.0%}，禁止买入"
                    )
                # 缩股到允许范围
                shares = min(shares, allowed_shares)
                trade_plan = {**trade_plan, "shares": shares}
                amount = shares * price
                result["adjusted_shares"] = shares
                result["warnings"].append(
                    f"[渐进加仓] {recovery_desc}，缩股至{shares}股"
                )

        # ===== 关卡0.9: MarketRegime动态仓位上限（FIX P1）=====
        # BEAR→30% / RANGE→60% / BULL→90%
        market_cap, market_desc = self.state_mgr.get_market_position_cap()
        if market_cap < 1.0:
            current_ratio_after = (current_position_ratio * total_capital + amount) / total_capital
            if current_ratio_after > market_cap:
                allowed_amt = max(0, market_cap * total_capital - current_position_ratio * total_capital)
                mkt_shares = int(allowed_amt / price / 100) * 100 if price > 0 else 0
                if mkt_shares < 100:
                    return self._block_result(
                        f"[MarketRegime] {market_desc}，"
                        f"当前仓位{current_position_ratio:.1%}已达上饨，禁止买入"
                    )
                shares = min(shares, mkt_shares)
                trade_plan = {**trade_plan, "shares": shares}
                amount = shares * price
                result["adjusted_shares"] = shares
                result["warnings"].append(f"[MarketRegime] {market_desc}，缩股至{shares}")

        # ===== 关卡0.5: 股票池外拦截 =====
        if self.allowed_pool is not None and code not in self.allowed_pool:
            return self._block_result(
                f"池外拦截: {code}不在核心池/观察池中，禁止买入"
            )

        # ===== 关卡1: 暂停期检查（状态持久化）=====
        paused, pause_reason = self.state_mgr.is_paused()
        if paused:
            return self._block_result(pause_reason)

        # ===== 关卡2: 冷却期检查（状态持久化）=====
        cooling, cool_reason = self.state_mgr.is_cooling_down(code)
        if cooling:
            return self._block_result(cool_reason)

        # ===== 关卡3: 日开仓次数限制 =====
        max_daily = cfg.get("max_daily_opens", 2)
        if self.state_mgr.state["daily_open_count"] >= max_daily:
            return self._block_result(
                f"今日已开仓{self.state_mgr.state['daily_open_count']}笔，"
                f"达到上限{max_daily}笔，禁止再开"
            )

        # ===== 关卡4: 连续亏损熔断（状态持久化）=====
        consec = self.state_mgr.state["consecutive_losses"]
        # FIX: 修复误用"暂停天数"键(consecutive_loss_pause=5)作为连亏笔数阈值，导致熔断延迟到5笔才触发的问题
        consec_block = cfg.get("consecutive_loss_block", 3)
        consec_today = cfg.get("consecutive_loss_today", 2)
        if consec >= consec_block:
            pause_days = cfg.get("consecutive_loss_pause", 3)
            until = (datetime.date.today() + datetime.timedelta(days=pause_days)).isoformat()
            self.state_mgr.state["pause_until"] = until
            self.state_mgr.save()
            return self._block_result(
                f"连续亏损{consec}笔触发熔断，暂停开仓{pause_days}天（至{until}）"
            )
        elif consec >= consec_today:
            return self._block_result(
                f"连续亏损{consec}笔，当日禁止开仓（强制冷静）"
            )

        # ===== 关卡5: 时间红线 =====
        now = datetime.datetime.now()
        current_time = now.strftime("%H:%M")
        if _in_no_trade_zone(current_time):
            return self._block_result(f"当前时间{current_time}处于禁止交易时段")

        # ===== 关卡6: 日度亏损熔断 =====
        # FIX: 修复从内存 RiskState(新进程初值0)读取 daily_pnl 导致日度熔断永不触发的问题
        # 改为读取快照差分法持久化的当日盈亏
        daily_pnl = self.state_mgr.state.get("daily_pnl", 0)
        daily_loss_ratio = abs(daily_pnl) / total_capital if daily_pnl < 0 else 0
        daily_l2 = cfg.get("daily_loss_limit_l2",
                            getattr(config, 'DAILY_LOSS_LIMIT_2', 0.03))
        daily_l1 = cfg.get("daily_loss_limit",
                            getattr(config, 'DAILY_LOSS_LIMIT_1', 0.02))
        if daily_loss_ratio >= daily_l2:
            return self._block_result(
                f"日度熔断L2: 当日亏损{daily_loss_ratio:.2%} >= {daily_l2:.0%}，禁止开新仓"
            )
        if daily_loss_ratio >= daily_l1:
            return self._block_result(
                f"日度熔断L1: 当日亏损{daily_loss_ratio:.2%} >= {daily_l1:.0%}，只卖不买"
            )

        # ===== 关卡7: 周度亏损熔断 =====
        # FIX: 修复从内存 RiskState(新进程初值0)读取 weekly_pnl 导致周度熔断永不触发的问题
        weekly_pnl = self.state_mgr.state.get("weekly_pnl", 0)
        weekly_loss_ratio = abs(weekly_pnl) / total_capital if weekly_pnl < 0 else 0
        weekly_limit = cfg.get("weekly_loss_limit",
                                getattr(config, 'WEEKLY_LOSS_LIMIT', 0.08))
        if weekly_loss_ratio >= weekly_limit:
            return self._block_result(
                f"周度熔断: 本周亏损{weekly_loss_ratio:.2%} >= {weekly_limit:.0%}，强制休息"
            )

        # ===== 关卡8: 持仓数量硬限制 =====
        max_holdings = cfg.get("max_holdings",
                                getattr(config, 'MAX_HOLDINGS', 7))
        if action == "buy" and code not in positions:
            if len(positions) >= max_holdings:
                return self._block_result(
                    f"持仓数量超限: 当前{len(positions)}只 >= 上限{max_holdings}只，禁止新开仓"
                )

        # ===== 关卡9: 浮亏加仓绝对拦截 =====
        if action == "add" and code in positions:
            pos = positions[code]
            buy_p = pos.get("buy_price", 0)
            cur_p = pos.get("current_price", buy_p)
            if buy_p > 0 and cur_p < buy_p:
                return self._block_result(
                    f"铁则违反: {code}当前浮亏{(cur_p-buy_p)/buy_p:.2%}，绝对禁止加仓"
                )
        # 兼容 RiskGate 的浮亏检查（买入已持仓标的也拦截）
        if cfg.get("block_add_on_loss", True) and code in positions and action == "buy":
            pos = positions[code]
            cost = pos.get("buy_price", pos.get("cost", 0))
            cur_price = pos.get("current_price", pos.get("price", cost))
            if cost > 0 and cur_price < cost:
                loss_pct = (cur_price - cost) / cost * 100
                return self._block_result(
                    f"浮亏加仓拦截: {code}当前浮亏{loss_pct:.1f}%，绝对禁止补仓/加仓（铁律）"
                )

        # ===== 关卡10: 个股仓位上限 =====
        if stock_type == "弹性":
            max_ratio = cfg.get("flexible_max_ratio",
                                getattr(config, 'FLEXIBLE_STOCK_MAX_RATIO', 0.08))
        else:
            max_ratio = cfg.get("stock_max_ratio",
                                getattr(config, 'LEADER_STOCK_MAX_RATIO', 0.15))
        current_stock_amount = 0
        if code in positions:
            pos = positions[code]
            current_stock_amount = pos.get("shares", 0) * pos.get(
                "current_price", pos.get("buy_price", 0))
        new_total = current_stock_amount + amount
        if new_total / total_capital > max_ratio:
            max_amount = total_capital * max_ratio - current_stock_amount
            if max_amount > 0 and price > 0:
                adjusted = int(max_amount / price)
                adjusted = (adjusted // 100) * 100
                if adjusted < shares:
                    result["adjusted_shares"] = adjusted
                    result["warnings"].append(
                        f"仓位超限: 原计划{shares}股 -> 调整为{adjusted}股 (上限{max_ratio:.0%})"
                    )
                    shares = adjusted
                    amount = shares * price
            else:
                return self._block_result(f"个股仓位超限{max_ratio:.0%}且无法缩减")

        # ===== 关卡11: 赛道仓位上限 =====
        if sector:
            sector_max = cfg.get("sector_max_ratio",
                                  getattr(config, 'SECTOR_MAX_RATIO', 0.40))
            sector_amount = self._calc_sector_amount(positions, sector)
            new_sector = sector_amount + amount
            if new_sector / total_capital > sector_max:
                result["warnings"].append(
                    f"赛道仓位预警: {sector}将达到{new_sector/total_capital:.1%}，"
                    f"上限{sector_max:.0%}"
                )
                result["level"] = "yellow" if result["level"] == "green" else result["level"]

        # ===== 关卡12: 总仓位上限（按行情强度 × 波动率缩放 × 盘中风险）=====
        max_pos_ratio = get_max_position_ratio(market_strength) * vol_scale
        # 盘中系统性风险强制降低仓位上限
        intraday_limit = self._get_intraday_position_limit()
        if intraday_limit is not None and intraday_limit < max_pos_ratio:
            logger.warning(f"[盘中风控] 系统性风险强制仓位上限: {max_pos_ratio:.0%} -> {intraday_limit:.0%}")
            max_pos_ratio = intraday_limit
        current_pos_amount = self._calc_total_position_amount(positions)
        new_pos_amount = current_pos_amount + amount
        new_pos_ratio = new_pos_amount / total_capital
        if new_pos_ratio > max_pos_ratio:
            result["warnings"].append(
                f"总仓位预警: 将达{new_pos_ratio:.1%}，"
                f"当前行情({market_strength})上限{max_pos_ratio:.0%}"
            )
            if new_pos_ratio > max_pos_ratio + 0.05:
                return self._block_result(
                    f"总仓位严重超限: {new_pos_ratio:.1%} > {max_pos_ratio:.0%}"
                )

        # ===== 关卡13: 现金安全垫 =====
        min_cash_ratio = cfg.get("min_cash_ratio",
                                  getattr(config, 'CASH_RESERVE_RATIO', 0.10))
        cash_after = total_capital - current_pos_amount - amount
        min_cash = total_capital * min_cash_ratio
        if cash_after < min_cash:
            return self._block_result(
                f"突破现金安全垫: 剩余{cash_after:.0f} < 最低保留{min_cash:.0f}({min_cash_ratio:.0%})"
            )

        # ===== 关卡14: 单笔亏损控制 =====
        stop_loss = trade_plan.get("stop_loss",
                                    price * (1 - cfg.get("initial_stop_loss_pct",
                                               getattr(config, 'INITIAL_STOP_LOSS_PCT', 0.10))))

        # ATR自适应止损计算（V8.3新增）
        adaptive_result = self._try_adaptive_stop_loss(
            code, price, trade_plan.get("volatility_regime", "normal")
        )
        if adaptive_result:
            stop_loss = adaptive_result["stop_price"]
            result["adaptive_stop"] = adaptive_result
            logger.info(
                f"[ATR自适应止损] {code}: 止损={adaptive_result['stop_price']:.2f} "
                f"({adaptive_result['stop_pct']:.2%}), ATR={adaptive_result['atr_used']:.4f}"
            )

        max_loss = shares * (price - stop_loss)
        max_loss_ratio = max_loss / total_capital
        max_single_loss = cfg.get("max_single_loss_ratio",
                                    getattr(config, 'MAX_SINGLE_LOSS_RATIO', 0.02))
        if max_loss_ratio > max_single_loss:
            # FIX P1: 从警告升级为强制缩股（单笔风险≤2%本金硬约束）
            safe_shares = int(total_capital * max_single_loss / (price - stop_loss)) if price > stop_loss else shares
            safe_shares = max(100, (safe_shares // 100) * 100)
            result["warnings"].append(
                f"单笔风险{max_loss_ratio:.2%}>{max_single_loss:.0%}，"
                f"强制缩股: {shares}→{safe_shares}"
            )
            result["adjusted_shares"] = safe_shares
            result["level"] = "yellow" if result["level"] == "green" else result["level"]

        # ===== 全部通过 =====
        if result["pass"]:
            result["reasons"].append("风控校验通过")
        return result

    # --------------------------------------------------------
    # 接口 2：兼容 RiskGate 的调用方式
    # --------------------------------------------------------
    def check_buy(self, signal: dict, holdings: dict,
                  market_info: dict = None) -> dict:
        """
        RiskGate 兼容接口：买入/加仓信号风控校验

        参数:
            signal: {"code", "name", "price", "shares", "sector", "type",
                     "stop_loss", "target", "risk_reward"}
            holdings: {code: {"shares", "cost", "price", "sector", "type"}}
            market_info: {"index_price", "index_ma20", "index_ma60"}

        返回:
            {"pass": bool, "reason": str, "adjusted_shares": int, "level": str}
        """
        try:
            return self._do_check_buy(signal, holdings, market_info)
        except Exception as e:
            logger.exception(f"[UnifiedRiskEngine] 引擎异常: {e}")
            return self._block(f"ENGINE_ERROR: 风控引擎异常，暂停交易 ({e})")

    def _do_check_buy(self, signal: dict, holdings: dict,
                      market_info: dict = None) -> dict:
        """RiskGate 兼容接口的内部实现"""
        # FIX: 修复 RiskGate 兼容接口日/周/月/年熔断 pnl 数据源缺失(恒为0或静态值)导致永不触发的问题
        self.refresh_pnl_snapshot(holdings)
        # FIX: 修复 total_capital 为0时除零崩溃的风险
        if self.total_capital <= 0:
            return self._block("系统错误: 总资金配置异常")
        cfg = self.cfg
        code = signal.get("code", "")
        name = signal.get("name", code)
        price = signal.get("price", 0)
        shares = signal.get("shares", 0)
        sector = signal.get("sector", "")
        stock_type = signal.get("type", "stock")
        risk_reward = signal.get("risk_reward", 0)
        amount = price * shares

        # 第-1关：盘中风险标记检查（最高优先级）
        self._expire_intraday_flags()  # 每次检查前先清理过期标记
        intraday_block = self._check_intraday_risk_flags_for_buy()
        if intraday_block:
            return intraday_block

        # 第0关：盈亏比硬门槛
        min_rr = cfg.get("min_risk_reward", 2.5)
        if risk_reward > 0 and risk_reward < min_rr:
            return self._block(
                f"盈亏比不达标: {risk_reward:.2f} < {min_rr}，拦截（宁缺毋滥）"
            )

        # 第0.5关：股票池外拦截
        if self.allowed_pool is not None and code not in self.allowed_pool:
            return self._block(
                f"池外拦截: {name}({code})不在核心池/观察池中，禁止买入"
            )

        # 第1关：暂停期检查
        paused, pause_reason = self.state_mgr.is_paused()
        if paused:
            return self._block(pause_reason)

        # 第2关：冷却期检查
        cooling, cool_reason = self.state_mgr.is_cooling_down(code)
        if cooling:
            return self._block(cool_reason)

        # 第3关：日开仓次数限制
        max_daily = cfg.get("max_daily_opens", 2)
        if self.state_mgr.state["daily_open_count"] >= max_daily:
            return self._block(
                f"今日已开仓{self.state_mgr.state['daily_open_count']}笔，"
                f"达到上限{max_daily}笔，禁止再开"
            )

        # 第4关：连续亏损熔断
        # P2: 动态熔断增强 - 暂停天数与严重度成正比
        consec = self.state_mgr.state["consecutive_losses"]
        # FIX: 修复误用"暂停天数"键(consecutive_loss_pause=5)作为连亏笔数阈值，导致熔断延迟到5笔才触发的问题
        consec_block = cfg.get("consecutive_loss_block", 3)
        consec_today = cfg.get("consecutive_loss_today", 2)
        if consec >= consec_block:
            # P2: 使用动态熔断增强器计算暂停天数
            daily_pnl = self.state_mgr.state.get("daily_pnl", 0)
            daily_loss_pct = abs(daily_pnl) / self.total_capital * 100 if daily_pnl < 0 else 0
            pause_days = self.fuse_enhancer.calc_pause_days(consec, daily_loss_pct)
            until = (datetime.date.today() + datetime.timedelta(days=pause_days)).isoformat()
            self.state_mgr.state["pause_until"] = until
            self.state_mgr.save()
            # P2: 记录熔断触发
            self.fuse_enhancer.on_fuse_triggered(f"连续亏损{consec}笔", consec)
            return self._block(
                f"连续亏损{consec}笔触发熔断，暂停开仓{pause_days}天（至{until}）"
            )
        elif consec >= consec_today:
            # V3.2: 连续2笔 → 暂停2天（原"当日禁止"升级）
            pause_days_2 = cfg.get("consecutive_loss_2_pause", 2)
            until = (datetime.date.today() + datetime.timedelta(days=pause_days_2)).isoformat()
            self.state_mgr.state["pause_until"] = until
            self.state_mgr.save()
            return self._block(
                f"连续亏损{consec}笔，暂停开仓{pause_days_2}天（至{until}，强制冷静）"
            )

        # 第5关：日度亏损熔断
        daily_pnl = self.state_mgr.state.get("daily_pnl", 0)
        daily_limit = cfg.get("daily_loss_limit", 0.02)
        if daily_pnl < 0 and abs(daily_pnl) / self.total_capital >= daily_limit:
            return self._block(
                f"单日亏损{abs(daily_pnl)/self.total_capital:.1%}≥{daily_limit:.0%}，"
                f"当日禁止新开仓"
            )

        # 第6关：周度亏损熔断
        weekly_pnl = self.state_mgr.state.get("weekly_pnl", 0)
        weekly_limit = cfg.get("weekly_loss_limit", 0.08)
        if weekly_pnl < 0 and abs(weekly_pnl) / self.total_capital >= weekly_limit:
            return self._block(
                f"本周亏损{abs(weekly_pnl)/self.total_capital:.1%}≥{weekly_limit:.0%}，"
                f"强制降仓+暂停{cfg.get('weekly_loss_pause_days', 3)}天"
            )

        # 第6.5关：月度回撤硬限制（V3.2: 从5%收紧至4%）
        # FIX P0: 从110万亏到73万(-33.6%)过程中无任何月度熔断，新增此关卡
        # V3.2回测诊断: 2026年-50.4%亏损证明5%仍过松，收紧至4%
        monthly_start = self.state_mgr.state.get("monthly_start_capital", self.total_capital)
        if monthly_start > 0:
            monthly_dd = (monthly_start - self.total_capital) / monthly_start
            monthly_dd_limit = cfg.get("monthly_drawdown_limit", 0.04)  # V3.2: 0.05→0.04
            if monthly_dd >= monthly_dd_limit:
                return self._block(
                    f"月度回撤{monthly_dd*100:.1f}%≥{monthly_dd_limit*100:.0f}%，"
                    f"本月禁止新开仓（防守优先）"
                )

        # 第6.6关：年度回撤硬限制（V3.2新增）
        # 回测诊断: 2026年最大回撤58.4%，无任何年度熔断触发，新增15%硬限制
        annual_start = self.state_mgr.state.get("annual_start_capital", self.total_capital)
        if annual_start > 0:
            annual_dd = (annual_start - self.total_capital) / annual_start
            annual_dd_limit = cfg.get("annual_drawdown_limit", 0.15)
            if annual_dd >= annual_dd_limit:
                # 年度回撤≥15%: 全面暂停，需手动重置
                self.state_mgr.state["annual_halt"] = True
                self.state_mgr.save()
                return self._block(
                    f"⚠️年度回撤{annual_dd*100:.1f}%≥{annual_dd_limit*100:.0f}%，"
                    f"全面暂停交易（需手动重置annual_halt后方可恢复）"
                )

        # 第7关：浮亏加仓绝对拦截
        if cfg.get("block_add_on_loss", True) and code in holdings:
            pos = holdings[code]
            cost = pos.get("cost", 0)
            cur_price = pos.get("price", cost)
            if cost > 0 and cur_price < cost:
                loss_pct = (cur_price - cost) / cost * 100
                return self._block(
                    f"浮亏加仓拦截: {name}当前浮亏{loss_pct:.1f}%，"
                    f"绝对禁止补仓/加仓（铁律）"
                )

        # 第8关：持仓数量限制
        max_holdings = cfg.get("max_holdings", 7)
        if code not in holdings and len(holdings) >= max_holdings:
            return self._block(
                f"持仓数量{len(holdings)}只已达上限{max_holdings}只，禁止新开仓"
            )

        # 第9关：三级仓位硬限制
        if stock_type == "etf":
            single_max = cfg.get("etf_max_ratio", 0.20)
        else:
            single_max = cfg.get("stock_max_ratio", 0.15)

        current_amount = 0
        if code in holdings:
            pos = holdings[code]
            current_amount = pos.get("shares", 0) * pos.get("price", 0)
        new_single_ratio = (current_amount + amount) / self.total_capital
        if new_single_ratio > single_max:
            allowed_amount = self.total_capital * single_max - current_amount
            if allowed_amount <= 0:
                return self._block(
                    f"单只仓位超限: {name}将达{new_single_ratio:.1%} > "
                    f"上限{single_max:.0%}，拦截"
                )
            adjusted_shares = int(allowed_amount / price / 100) * 100
            if adjusted_shares < 100:
                return self._block(
                    f"单只仓位超限: {name}已达上限{single_max:.0%}，无法再买"
                )
            shares = adjusted_shares
            amount = shares * price

        # 9b. 赛道仓位
        if sector:
            sector_max = cfg.get("sector_max_ratio", 0.40)
            sector_amount = sum(
                h.get("shares", 0) * h.get("price", 0)
                for h in holdings.values()
                if h.get("sector") == sector
            )
            new_sector_ratio = (sector_amount + amount) / self.total_capital
            if new_sector_ratio > sector_max:
                return self._block(
                    f"赛道仓位超限: {sector}将达{new_sector_ratio:.1%} > "
                    f"上限{sector_max:.0%}，拦截"
                )

        # 9c. 总仓位（动态，与指数均线绑定 + 盘中风险联动）
        total_position = sum(
            h.get("shares", 0) * h.get("price", 0)
            for h in holdings.values()
        )
        max_total = self._get_dynamic_total_limit(market_info)
        # 盘中系统性风险强制降低仓位上限
        intraday_limit = self._get_intraday_position_limit()
        if intraday_limit is not None and intraday_limit < max_total:
            logger.warning(f"[盘中风控] 系统性风险强制仓位上限: {max_total:.0%} -> {intraday_limit:.0%}")
            max_total = intraday_limit
        new_total_ratio = (total_position + amount) / self.total_capital
        if new_total_ratio > max_total:
            return self._block(
                f"总仓位超限: 将达{new_total_ratio:.1%} > "
                f"动态上限{max_total:.0%}，拦截"
            )

        # 第10关：现金安全垫
        min_cash_ratio = cfg.get("min_cash_ratio", 0.10)
        cash_after = self.total_capital - total_position - amount
        min_cash = self.total_capital * min_cash_ratio
        if cash_after < min_cash:
            return self._block(
                f"突破现金安全垫: 剩余{cash_after:.0f}元 < "
                f"最低保留{min_cash:.0f}元({min_cash_ratio:.0%})"
            )

        # 第10.5关：Kelly动态仓位调整（V3.2新增）
        # 基于该标的历史胜率/盈亏比动态缩减仓位（只减不增）
        kelly_shares = self._kelly_adjust_shares(code, shares, price, signal)
        if kelly_shares < shares:
            logger.info(
                f"[Kelly仓位] {code}: {shares}→{kelly_shares}股 "
                f"(基于历史胜率动态缩减)"
            )
            shares = kelly_shares
            amount = shares * price

        # ATR自适应止损计算（V8.3新增）
        adaptive_result = self._try_adaptive_stop_loss(
            code, price, signal.get("volatility_regime", "normal")
        )

        # 第11关：组合层面浮亏强制减仓（V3.2新增）
        # 回测诊断: 2026年-50.4%亏损主因是组合集中度过高+无组合级止损
        portfolio_pnl_pct = self._calc_portfolio_pnl(holdings)
        portfolio_dd_limit = cfg.get("portfolio_drawdown_limit", -0.10)
        if portfolio_pnl_pct < portfolio_dd_limit:
            return self._block(
                f"组合浮亏{portfolio_pnl_pct:.1%}超过{portfolio_dd_limit:.0%}，"
                f"禁止新开仓（应先减仓最弱标的）"
            )

        # 第12关：持仓相关性检查（V3.2新增）
        # 避免7只持仓全部集中在同一赛道，系统性风险无法通过个股止损解决
        if code not in holdings and len(holdings) >= 3:
            corr_warning = self._check_correlation(code, holdings)
            if corr_warning:
                return self._block(corr_warning)

        # 全部通过
        pass_result = {
            "pass": True,
            "reason": "风控通过",
            "level": "green",
            "adjusted_shares": shares,
            "warnings": [],
        }
        if adaptive_result:
            pass_result["adaptive_stop"] = adaptive_result
            logger.info(
                f"[ATR自适应止损] {code}: 止损={adaptive_result['stop_price']:.2f} "
                f"({adaptive_result['stop_pct']:.2%}), ATR={adaptive_result['atr_used']:.4f}"
            )
        return pass_result

    # --------------------------------------------------------
    # 卖出信号校验
    # --------------------------------------------------------
    def check_sell(self, signal: dict, holdings: dict) -> dict:
        """卖出信号处理：记录冷却期、更新连续亏损、更新策略失效检测"""
        code = signal.get("code", "")
        pnl = signal.get("pnl", 0)
        self.state_mgr.record_sell(code)
        if pnl < 0:
            self.state_mgr.record_loss()
        else:
            self.state_mgr.record_profit()
        
        # P0: 记录交易结果到策略失效检测器
        pnl_pct = signal.get("pnl_pct", 0)
        if pnl_pct == 0 and pnl != 0:
            # 如果没有pnl_pct字段，尝试从持仓计算
            pos = holdings.get(code, {})
            cost = pos.get("cost", pos.get("buy_price", 0))
            if cost > 0:
                shares = pos.get("shares", 0)
                pnl_pct = (pnl / (cost * shares) * 100) if shares > 0 else 0
        if pnl_pct != 0:
            self.failure_detector.record_trade(pnl_pct)
        
        # P2: 熔断恢复期交易跟踪
        self.fuse_enhancer.on_recovery_trade(pnl >= 0)
        
        return {"pass": True, "reason": "卖出放行", "level": "green"}

    # --------------------------------------------------------
    # 持仓健康度巡检（兼容 RiskGate.inspect_holdings）
    # --------------------------------------------------------
    def inspect_holdings(self, holdings: dict,
                         market_info: dict = None) -> List[dict]:
        """持仓风险四级巡检（与 RiskGate 完全兼容）"""
        # FIX: 统一止损口径 —— 巡检前用权威源(trend_strategy.compute_trailing_stop)
        # 重算并写回 holdings.stop_loss（Ratchet只升不降），内部已降级兜底
        sync_authoritative_stop_loss()
        cfg = self.cfg
        results = []
        for code, pos in holdings.items():
            cost = pos.get("cost", pos.get("buy_price", 0))
            price = pos.get("price", pos.get("current_price", cost))
            name = pos.get("name", code)
            ma20 = pos.get("ma20", price)
            ma60 = pos.get("ma60", price)
            pnl_pct = (price / cost - 1) * 100 if cost > 0 else 0
            is_bullish = price > ma20 and ma20 > ma60

            if pnl_pct <= -15 or (price < ma60 and pnl_pct < -10):
                level, action, urgency = "危险", "无条件清仓", 0
            elif not is_bullish and pnl_pct < 0:
                level, action, urgency = "预警", "反弹减仓（设14:50条件单）", 1
            elif not is_bullish and pnl_pct >= 0:
                level, action, urgency = "关注", "持有观察，跌破MA20即减仓", 2
            elif is_bullish and pnl_pct < 0:
                level, action, urgency = "关注", "持有+带好止损（成本×90%）", 2
            else:
                level, action, urgency = "健康", "持有，止损上移", 3

            position_ratio = (pos.get("shares", 0) * price) / self.total_capital
            stock_type = pos.get("type", "stock")
            max_ratio = cfg.get("etf_max_ratio", 0.20) if stock_type == "etf" \
                else cfg.get("stock_max_ratio", 0.15)
            over_limit = position_ratio > max_ratio

            reduce_shares = 0
            if over_limit:
                target_amount = self.total_capital * max_ratio
                current_amount = pos.get("shares", 0) * price
                reduce_amount = current_amount - target_amount
                reduce_shares = int(reduce_amount / price / 100) * 100

            results.append({
                "code": code, "name": name, "level": level,
                "urgency": urgency, "pnl_pct": round(pnl_pct, 2),
                "is_bullish": is_bullish, "action": action,
                "position_ratio": round(position_ratio * 100, 1),
                "over_limit": over_limit, "reduce_shares": reduce_shares,
                # 仅巡检参考，执行以 holdings.stop_loss 为准（权威值已由 sync_authoritative_stop_loss 写回）
                "stop_loss": round(cost * 0.9, 3) if level in ("关注", "预警")
                             else round(max(cost * 0.9, price * 0.92), 3),  # FIX P2: Ratchet原则
            })
        results.sort(key=lambda x: x["urgency"])
        return results

    # --------------------------------------------------------
    # 内部辅助方法
    # --------------------------------------------------------
    def _sync_risk_state(self, risk_state):
        """同步盈亏数据到持久化状态管理器

        FIX: 修复从内存 RiskState 同步 daily_pnl/weekly_pnl(新进程初值恒0)且
        total_capital 为静态 config 值，导致日/周/月/年四级熔断全部失效的问题。
        改为基于持仓实际市值的"总资产快照差分法"计算并持久化。
        """
        positions = getattr(risk_state, 'current_positions', None) or {}
        self.refresh_pnl_snapshot(positions)

    # --------------------------------------------------------
    # FIX: pnl 快照差分法（四级熔断数据源修复）
    # --------------------------------------------------------
    def _calc_current_total_assets(self, positions: dict = None) -> float:
        """动态计算当前总资产 = 持仓市值 + 可用资金

        口径说明:
          - 持仓市值: 优先用传入持仓，否则读取 config.get_holdings_file()，
            价格取 current_price/price/buy_price/cost 中首个可用值
          - 可用资金: config.AVAILABLE_CASH（手动与券商同步的静态值）
          - 返回 0 表示数据不可用（由调用方兜底降级）
        """
        try:
            if not positions:
                holdings_file = config.get_holdings_file()
                if os.path.exists(holdings_file):
                    with open(holdings_file, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    positions = {k: v for k, v in raw.items()
                                 if isinstance(v, dict)}
            market_value = 0.0
            for pos in (positions or {}).values():
                shares = pos.get("shares", 0) or 0
                price = (pos.get("current_price") or pos.get("price")
                         or pos.get("buy_price") or pos.get("cost") or 0)
                market_value += shares * price
            if market_value <= 0:
                return 0.0
            cash = getattr(config, 'AVAILABLE_CASH', 0) or 0
            return market_value + cash
        except Exception as e:
            logger.warning(f"[PnL快照] 总资产计算失败，降级为不熔断: {e}")
            return 0.0

    def refresh_pnl_snapshot(self, positions: dict = None):
        """刷新日/周/月/年总资产快照并计算 daily_pnl / weekly_pnl

        FIX: 修复四级熔断数据源缺失问题（日/周pnl内存初值0、月度基准与
        total_capital同源恒差0、annual_start_capital全库无写入方）。

        口径（总资产快照差分法，实现成本最低且数据可靠）:
          - 总资产 = 持仓市值 + config.AVAILABLE_CASH（见 _calc_current_total_assets）
          - daily_pnl  = 当前总资产 - 当日起始快照（每交易日首次风控检查时重置）
          - weekly_pnl = 当前总资产 - 本周起始快照（ISO周变化即周一重置）
          - monthly_start_capital / annual_start_capital 同理按月/年首个使用日重置
          - 已实现盈亏: trades_today.json 无逐笔成本字段无法可靠计算，故统一用
            总资产差分口径（卖出兑现的盈亏已体现在现金端，随 AVAILABLE_CASH 同步）
          - 所有重置通过比较持久化日期自动完成，不依赖 scheduler
          - 任何读写失败降级为不熔断（保留旧值），绝不阻断风控主流程
        """
        try:
            state = self.state_mgr.state
            current_total = self._calc_current_total_assets(positions)
            if current_total <= 0:
                # 数据不可用：降级为不熔断，保留既有值
                logger.warning("[PnL快照] 持仓/资金数据不可用，跳过快照刷新（降级为不熔断）")
                return

            today = datetime.date.today()
            iso = today.isocalendar()
            week_key = f"{iso[0]}-W{iso[1]:02d}"
            month_key = today.strftime("%Y-%m")
            year_key = str(today.year)

            # 快照重置（比较持久化日期，首次使用自动初始化）
            if state.get("snapshot_date") != today.isoformat():
                state["snapshot_date"] = today.isoformat()
                state["daily_start_total"] = current_total
            if state.get("snapshot_week") != week_key:
                state["snapshot_week"] = week_key
                state["weekly_start_total"] = current_total
            if state.get("snapshot_month") != month_key:
                state["snapshot_month"] = month_key
                state["monthly_start_capital"] = current_total
            if state.get("snapshot_year") != year_key:
                state["snapshot_year"] = year_key
                state["annual_start_capital"] = current_total

            # 盈亏 = 当前总资产 - 周期起始快照
            daily_start = state.get("daily_start_total", current_total) or current_total
            weekly_start = state.get("weekly_start_total", current_total) or current_total
            state["daily_pnl"] = current_total - daily_start
            state["weekly_pnl"] = current_total - weekly_start

            # FIX: 修复 total_capital 为静态 config.TOTAL_CAPITAL 导致月度回撤恒差0的问题
            # 动态总资产口径，config.TOTAL_CAPITAL 仅作兜底
            self.total_capital = current_total
            state["total_capital"] = current_total

            self.state_mgr.save()
        except Exception as e:
            logger.warning(f"[PnL快照] 刷新失败，降级为不熔断: {e}")

    def _calc_position_ratio(self, positions: dict, total_capital: float) -> float:
        if total_capital <= 0:
            return 0
        return self._calc_total_position_amount(positions) / total_capital

    @staticmethod
    def _calc_total_position_amount(positions: dict) -> float:
        total = 0
        for pos in positions.values():
            total += pos.get("shares", 0) * pos.get(
                "current_price", pos.get("buy_price", pos.get("price", 0)))
        return total

    @staticmethod
    def _calc_sector_amount(positions: dict, sector: str) -> float:
        total = 0
        for pos in positions.values():
            if pos.get("sector") == sector:
                total += pos.get("shares", 0) * pos.get(
                    "current_price", pos.get("buy_price", pos.get("price", 0)))
        return total

    def _get_dynamic_total_limit(self, market_info: dict = None) -> float:
        cfg = self.cfg
        if not market_info:
            return cfg.get("total_above_ma20", 0.80)
        index_price = market_info.get("index_price", 0)
        index_ma20 = market_info.get("index_ma20", 0)
        index_ma60 = market_info.get("index_ma60", 0)
        if index_price <= 0 or index_ma20 <= 0:
            return cfg.get("total_above_ma20", 0.80)
        if index_price < index_ma60:
            return cfg.get("total_below_ma60", 0.30)
        elif index_price < index_ma20:
            return cfg.get("total_below_ma20", 0.50)
        else:
            return cfg.get("total_above_ma20", 0.80)

    # --------------------------------------------------------
    # ATR自适应止损辅助方法
    # --------------------------------------------------------
    def _calc_portfolio_pnl(self, holdings: dict) -> float:
        """V3.2: 计算组合整体浮盈/浮亏百分比"""
        total_cost = 0
        total_value = 0
        for code, pos in holdings.items():
            shares = pos.get("shares", 0)
            cost = pos.get("cost", pos.get("buy_price", 0))
            price = pos.get("price", cost)
            if shares > 0 and cost > 0:
                total_cost += shares * cost
                total_value += shares * price
        if total_cost <= 0:
            return 0.0
        return (total_value - total_cost) / total_cost

    def _check_black_swan(self, market_info: dict) -> Optional[dict]:
        """V3.2: 黑天鹅/极端行情检测（关卡-2，最高优先级）
        
        触发条件（满足任一即全面禁止开仓）:
        1. 指数单日跌幅 > 3%
        2. 指数连续3日累计跌幅 > 5%
        3. 市场状态为'crash'或'panic'
        
        设计原理:
        回测诊断2026年-50.4%亏损主因是系统性暴跌时未及时降仓。
        年度回撤15%硬限制是滞后的，黑天鹅检测是前瞻的。
        """
        if not market_info:
            return None
        
        # 条件1: 指数单日跌幅>3%
        index_change = market_info.get("index_change_pct",
                     market_info.get("change_pct", 0))
        if isinstance(index_change, (int, float)) and index_change < -3.0:
            return self._block(
                f"⚠️黑天鹅拦截: 指数单日暴跌{index_change:.1f}%，"
                f"全面禁止开仓（等待市场企稳）"
            )
        
        # 条件2: 连续3日累计跌幅>5%
        index_3d_change = market_info.get("index_3d_change_pct", 0)
        if isinstance(index_3d_change, (int, float)) and index_3d_change < -5.0:
            return self._block(
                f"⚠️黑天鹅拦截: 指数连续3日累计跌{index_3d_change:.1f}%，"
                f"全面禁止开仓（系统性风险）"
            )
        
        # 条件3: 市场状态为极端
        regime = market_info.get("regime", market_info.get("market_state", ""))
        if regime in ("crash", "panic", "extreme_fear"):
            return self._block(
                f"⚠️黑天鹅拦截: 市场状态={regime}，"
                f"全面禁止开仓（极端恐慌）"
            )
        
        return None

    def _check_correlation(self, new_code: str, holdings: dict) -> str:
        """V3.2: 检查新标的与现有持仓的相关性
        
        简化实现: 基于赛道/行业判断（无需实时数据）
        如果新标的与已有持仓同赛道占比>50%，拒绝买入
        """
        try:
            new_sector = self._get_code_sector(new_code)
            if not new_sector:
                return ""  # 无法判断赛道，放行
            
            same_sector_count = 0
            total_count = len(holdings)
            for code in holdings:
                if self._get_code_sector(code) == new_sector:
                    same_sector_count += 1
            
            # 同赛道占比>50% → 拒绝（避免过度集中）
            if total_count > 0 and (same_sector_count + 1) / (total_count + 1) > 0.5:
                return (
                    f"赛道集中度拦截: {new_code}与{same_sector_count}只持仓同属"
                    f"[{new_sector}]赛道，买入后占比"
                    f"{(same_sector_count+1)/(total_count+1):.0%}>50%"
                )
        except Exception:
            pass
        return ""

    def _get_code_sector(self, code: str) -> str:
        """获取标的所属赛道（从SECTOR_CANDIDATES查找）"""
        sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
        for sector_name, sector_info in sector_candidates.items():
            stocks = sector_info.get("stocks", {}) if isinstance(sector_info, dict) else {}
            if code in stocks:
                return sector_name
        return ""

    def _kelly_adjust_shares(self, code: str, shares: int, price: float,
                             signal: dict) -> int:
        """V3.2: Kelly公式动态仓位调整（只减不增）
        
        逻辑:
        - 从策略失效检测器获取滚动胜率/盈亏比
        - 半Kelly仓位 vs 当前请求仓位，取较小值
        - 最低不低于100股（1手）
        """
        try:
            from position.kelly import half_kelly_position
            
            # 从策略失效检测器获取滚动统计
            history = self.failure_detector.state.get("trade_history", [])
            if len(history) < 5:
                return shares  # 样本不足，不调整
            
            pnls = [t["pnl_pct"] for t in history]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            
            if not wins or not losses:
                return shares
            
            win_rate = len(wins) / len(pnls)
            avg_win = sum(wins) / len(wins)
            avg_loss = abs(sum(losses) / len(losses))
            profit_factor = avg_win / avg_loss if avg_loss > 0 else 1.0
            
            # 半Kelly仓位比例
            kelly_ratio = half_kelly_position(win_rate, profit_factor)
            kelly_amount = self.total_capital * kelly_ratio
            kelly_shares = int(kelly_amount / price / 100) * 100
            
            # 只减不增，最低100股
            return max(min(shares, kelly_shares), 100)
        except Exception:
            return shares

    def _try_adaptive_stop_loss(self, code: str, price: float,
                                volatility_regime: str = "normal") -> Optional[dict]:
        """
        尝试计算ATR自适应止损价。
        成功返回止损结果 dict，ATR数据不可用时返回 None（fallback到固定百分比）。
        """
        _ensure_adaptive_stop_import()
        if _get_atr_value is None or _calc_adaptive_stop_loss is None:
            return None

        try:
            atr = _get_atr_value(code, price)
            if atr <= 0:
                logger.debug(f"[ATR止损] {code}: ATR数据不可用({atr}), fallback到固定百分比止损")
                return None

            result = _calc_adaptive_stop_loss(price, atr, volatility_regime)

            # 验证止损价合理性
            cfg = self.cfg
            max_stop_pct = cfg.get('max_stop_loss_pct', 0.15)
            stop_distance = (price - result["stop_price"]) / price if price > 0 else 0
            if stop_distance > max_stop_pct:
                logger.warning(
                    f"[ATR止损] {code}: 止损距离{stop_distance:.2%}超过上限{max_stop_pct:.0%}，"
                    f"截断到固定止损"
                )
                return None

            return result
        except Exception as e:
            logger.warning(f"[ATR止损] {code}: 自适应止损计算异常({e}), fallback到固定百分比")
            return None

    # --------------------------------------------------------
    # 盘中风险联动方法
    # --------------------------------------------------------
    def set_intraday_risk_flag(self, flag: str, details: dict):
        """
        设置盘中风险标记（由 IntradayMonitor 联动调用）

        参数:
            flag: 风险标记名称，如 "systemic_risk", "intraday_pause"
            details: 风险详情 dict
        """
        try:
            flags = self.state_mgr.state.setdefault("intraday_risk_flags", {})
            flags[flag] = {
                "time": datetime.datetime.now().isoformat(),
                "details": details,
            }
            self.state_mgr.save()
            logger.critical(f"[盘中风控] 设置风险标记: {flag}, 详情: {details}")
        except Exception as e:
            logger.error(f"[盘中风控] 设置标记失败({flag}): {e}")

    def clear_intraday_risk_flag(self, flag: str):
        """
        清除盘中风险标记（风险解除时调用）

        参数:
            flag: 要清除的风险标记名称
        """
        try:
            flags = self.state_mgr.state.get("intraday_risk_flags", {})
            if flag in flags:
                del flags[flag]
                self.state_mgr.save()
                logger.info(f"[盘中风控] 清除风险标记: {flag}")
        except Exception as e:
            logger.error(f"[盘中风控] 清除标记失败({flag}): {e}")

    def _load_intraday_flags(self):
        """从持久化状态加载盘中风险标记"""
        flags = self.state_mgr.state.get("intraday_risk_flags", {})
        if flags:
            logger.info(f"[盘中风控] 从持久化状态加载 {len(flags)} 个风险标记: {list(flags.keys())}")

    def _expire_intraday_flags(self):
        """
        盘中风险标记自动过期机制：
        当日15:30后自动清除所有盘中风险标记（盘后自动解除）
        """
        try:
            flags = self.state_mgr.state.get("intraday_risk_flags", {})
            if not flags:
                return
            now = datetime.datetime.now()
            expire_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
            if now >= expire_time:
                expired_flags = list(flags.keys())
                self.state_mgr.state["intraday_risk_flags"] = {}
                self.state_mgr.save()
                logger.info(f"[盘中风控] 盘后自动过期清除标记: {expired_flags}")
        except Exception as e:
            logger.error(f"[盘中风控] 过期清理失败: {e}")

    def _check_intraday_risk_flags(self) -> Optional[dict]:
        """
        检查盘中风险标记（用于 _do_check 接口）
        - systemic_risk → 总仓位上限强制降至30%
        - intraday_pause → 拒绝所有新买入
        """
        flags = self.state_mgr.state.get("intraday_risk_flags", {})
        if not flags:
            return None

        if "intraday_pause" in flags:
            pause_info = flags["intraday_pause"]
            return self._block_result(
                f"盘中暂停: {pause_info.get('details', {}).get('reason', '盘中急跌暂停')}，"
                f"所有新买入被拒绝（{pause_info.get('time', '')}触发）"
            )

        if "systemic_risk" in flags:
            risk_info = flags["systemic_risk"]
            pct = risk_info.get("details", {}).get("pct_down", 0)
            logger.warning(f"[盘中风控] 系统性风险标记生效，总仓位上限强制30% (触发时间: {risk_info.get('time', '')})")
            # 系统性风险不直接拦截，而是降低仓位上限（在关卡12中生效）
            # 此处仅输出警告，不返回拦截结果
        return None

    def _check_intraday_risk_flags_for_buy(self) -> Optional[dict]:
        """
        检查盘中风险标记（用于 _do_check_buy / RiskGate 兼容接口）
        - intraday_pause → 拒绝所有新买入
        - systemic_risk → 总仓位上限强制降至30%（在仓位检查关卡中生效）
        """
        flags = self.state_mgr.state.get("intraday_risk_flags", {})
        if not flags:
            return None

        if "intraday_pause" in flags:
            pause_info = flags["intraday_pause"]
            return self._block(
                f"盘中暂停: {pause_info.get('details', {}).get('reason', '盘中急跌暂停')}，"
                f"所有新买入被拒绝（{pause_info.get('time', '')}触发）"
            )
        return None

    def _get_intraday_position_limit(self) -> Optional[float]:
        """获取盘中风险标记强制的仓位上限（None=无限制）"""
        flags = self.state_mgr.state.get("intraday_risk_flags", {})
        if "systemic_risk" in flags:
            return 0.30  # 系统性风险 → 总仓位上限30%
        return None

    @staticmethod
    def _block(reason: str) -> dict:
        """RiskGate 格式拦截结果"""
        logger.warning(f"[风控拦截] {reason}")
        return {
            "pass": False, "reason": reason, "level": "red",
            "adjusted_shares": 0, "warnings": [],
        }

    @staticmethod
    def _block_result(reason: str) -> dict:
        """risk_check 格式拦截结果"""
        logger.warning(f"[风控拦截] {reason}")
        return {
            "pass": False, "level": "red", "reasons": [reason],
            "adjusted_shares": 0, "warnings": [],
        }


# 全局单例引用（延迟初始化）
_unified_engine: Optional[UnifiedRiskEngine] = None


def _get_unified_engine() -> UnifiedRiskEngine:
    """获取全局 UnifiedRiskEngine 单例"""
    global _unified_engine
    if _unified_engine is None:
        _unified_engine = UnifiedRiskEngine(
            total_capital=getattr(config, 'TOTAL_CAPITAL', 424000)
        )
    return _unified_engine


"""
风控熔断校验模块
==================
所有交易信号必须先过风控才能输出，从程序层面杜绝情绪化操作

核心规则:
- 单只标的仓位 <= 总资金15%（龙头）/ 8%（弹性）
- 单一赛道仓位 <= 总资金40%
- 弱势行情总仓位 <= 30%
- 单日亏损>=2%: 当日禁止开新仓
- 单日亏损>=3%: 清非主线弱势仓，总仓位<=60%
- 单周亏损>=8%: 全仓降至3成以下，强制休息1周
- 任何时刻保留>=10%现金安全垫
"""

import datetime
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class RiskState:
    """风控状态记录（每日更新）"""

    def __init__(self):
        self.total_capital = config.TOTAL_CAPITAL
        self.current_positions = {}   # {code: {"shares": int, "buy_price": float, "sector": str, ...}}
        self.daily_pnl = 0.0          # 当日已实现盈亏
        self.weekly_pnl = 0.0         # 本周已实现盈亏
        self.today = datetime.date.today()
        self.trade_log = []           # 当日交易记录

    def update_positions(self, positions: dict):
        """更新当前持仓"""
        self.current_positions = positions

    def record_trade(self, code: str, action: str, pnl: float = 0):
        """记录一笔交易"""
        self.trade_log.append({
            "code": code,
            "action": action,
            "pnl": pnl,
            "time": datetime.datetime.now().strftime("%H:%M:%S")
        })
        self.daily_pnl += pnl
        self.weekly_pnl += pnl

    def get_total_position_amount(self) -> float:
        """计算当前总持仓金额"""
        total = 0
        for code, pos in self.current_positions.items():
            total += pos.get("shares", 0) * pos.get("current_price", pos.get("buy_price", 0))
        return total

    def get_sector_amount(self, sector: str) -> float:
        """计算某赛道当前占用金额"""
        total = 0
        for code, pos in self.current_positions.items():
            if pos.get("sector") == sector:
                total += pos.get("shares", 0) * pos.get("current_price", pos.get("buy_price", 0))
        return total

    def get_position_ratio(self) -> float:
        """当前总仓位占比"""
        if self.total_capital <= 0:
            return 0
        return self.get_total_position_amount() / self.total_capital


# ============================================================
# 一、行情强度判定
# ============================================================

def judge_market_strength(benchmark_df) -> str:
    """
    判定当前行情强度（基于基准指数）
    
    规则:
    - 强势：指数站稳20日线，20日均线向上，主线放量上攻
    - 弱势：指数跌破60日线，60日均线向下
    - 震荡：其他情况
    
    参数:
        benchmark_df: 基准指数日线数据（需含close, ma20, ma60等）
    
    返回: "strong" / "normal" / "weak"
    """
    import pandas as pd
    if benchmark_df is None or len(benchmark_df) < 60:
        return "normal"  # 数据不足默认震荡

    latest = benchmark_df.iloc[-1]
    close = latest["close"]

    # 计算均线
    ma20 = benchmark_df["close"].rolling(20).mean().iloc[-1]
    ma60 = benchmark_df["close"].rolling(60).mean().iloc[-1]
    ma20_slope = benchmark_df["close"].rolling(20).mean().diff(3).iloc[-1]
    ma60_slope = benchmark_df["close"].rolling(60).mean().diff(3).iloc[-1]

    # 弱势：跌破60日线且60日线向下
    if close < ma60 and ma60_slope < 0:
        return "weak"
    # 强势：站稳20日线且20日线向上
    if close > ma20 and ma20_slope > 0:
        return "strong"
    return "normal"


def get_max_position_ratio(market_strength: str) -> float:
    """根据行情强度获取最大仓位比例"""
    if market_strength == "strong":
        return config.MARKET_STRONG_MAX
    elif market_strength == "weak":
        return config.MARKET_WEAK_MAX
    else:
        return config.MARKET_NORMAL_MAX


# ============================================================
# 二、风控校验主函数
# ============================================================

def risk_check(trade_plan: dict, risk_state: RiskState,
               market_strength: str = "normal",
               vol_scale: float = 1.0) -> dict:
    """
    交易信号风控校验（向后兼容包装器）
    
    实际已委托给 UnifiedRiskEngine.check() 执行。
    保留此函数仅为与 main.py 的导入兼容。
    
    参数:
        vol_scale: 波动率目标缩放因子（>1允许加仓，<1需减仓），默认1.0不影响
    """
    engine = _get_unified_engine()
    # 同步资金信息
    engine.total_capital = getattr(risk_state, 'total_capital', config.TOTAL_CAPITAL)
    return engine.check(trade_plan, risk_state, market_strength, vol_scale=vol_scale)


def _in_no_trade_zone(current_time: str) -> bool:
    """判断是否在禁止交易时段"""
    morning_start, morning_end = config.NO_TRADE_MORNING
    afternoon_start, afternoon_end = config.NO_TRADE_AFTERNOON

    if morning_start <= current_time <= morning_end:
        return True
    if afternoon_start <= current_time <= afternoon_end:
        return True
    return False


# ============================================================
# 三、每日风控摘要
# ============================================================

def daily_risk_summary(risk_state: RiskState, market_strength: str) -> str:
    """生成每日风控摘要报告"""
    total_pos = risk_state.get_total_position_amount()
    pos_ratio = total_pos / risk_state.total_capital if risk_state.total_capital > 0 else 0
    cash = risk_state.total_capital - total_pos
    cash_ratio = cash / risk_state.total_capital if risk_state.total_capital > 0 else 0
    max_pos = get_max_position_ratio(market_strength)

    lines = [
        "=" * 50,
        "  每日风控摘要",
        "=" * 50,
        f"  总资金:     {risk_state.total_capital:>12,.0f} 元",
        f"  持仓市值:   {total_pos:>12,.0f} 元 ({pos_ratio:.1%})",
        f"  现金余额:   {cash:>12,.0f} 元 ({cash_ratio:.1%})",
        f"  行情强度:   {market_strength}",
        f"  仓位上限:   {max_pos:.0%}",
        f"  当日盈亏:   {risk_state.daily_pnl:>12,.0f} 元",
        f"  本周盈亏:   {risk_state.weekly_pnl:>12,.0f} 元",
        "",
        "  持仓明细:",
    ]

    for code, pos in risk_state.current_positions.items():
        name = config.get_stock_name(code)
        shares = pos.get("shares", 0)
        buy_p = pos.get("buy_price", 0)
        cur_p = pos.get("current_price", buy_p)
        pnl_pct = (cur_p - buy_p) / buy_p if buy_p > 0 else 0
        amount = shares * cur_p
        lines.append(f"    {code} {name}: {shares}股, 成本{buy_p:.2f}, "
                     f"现价{cur_p:.2f}, 浮盈{pnl_pct:.2%}, 市值{amount:,.0f}")

    # 风控状态
    daily_loss = abs(risk_state.daily_pnl) / risk_state.total_capital if risk_state.daily_pnl < 0 else 0
    unified_cfg = getattr(config, 'RISK_UNIFIED_CONFIG', {})
    daily_l2 = unified_cfg.get("daily_loss_limit_l2", getattr(config, 'DAILY_LOSS_LIMIT_2', 0.03))
    daily_l1 = unified_cfg.get("daily_loss_limit", getattr(config, 'DAILY_LOSS_LIMIT_1', 0.02))
    if daily_loss >= daily_l2:
        lines.append(f"\n  [!] 日度熔断L2触发，禁止开新仓")
    elif daily_loss >= daily_l1:
        lines.append(f"\n  [!] 日度熔断L1触发，只卖不买")

    return "\n".join(lines)


def suggest_atr_threshold(price, atr, base_pct, k=1.5):
    """
    返回 ATR 化的档位建议（百分比，与 base_pct 同口径的小数形式）

    用途: 基于个股实际波动率给出止损/触发档位下限建议，
    口径与 _try_adaptive_stop_loss / calc_adaptive_stop_loss 中
    止损百分比的使用方式一致（如 0.05 表示 5%，ATR 为绝对值元）。

    计算: max(base_pct, k * atr / price)，即高波动股自动放宽档位，
    低波动股保持基础档位不变；并钳位不超过 base_pct×3（防异常ATR）。

    参数:
        price: 当前价格(元)
        atr: ATR绝对值(元)
        base_pct: 基础档位(小数形式, 如 0.05 表示 5%)
        k: ATR倍数, 默认1.5

    返回:
        float, 建议档位(小数形式, 与 base_pct 一致);
        price<=0 或 atr<=0 时返回 base_pct(除零保护)
    """
    if price is None or atr is None or price <= 0 or atr <= 0:
        return base_pct
    suggested = max(base_pct, k * atr / price)
    # 上限钳位: 不超过基础档位3倍, 防异常ATR导致档位失控
    return float(min(suggested, base_pct * 3))


if __name__ == "__main__":
    print("=" * 50)
    print("  风控熔断模块 - 测试")
    print("=" * 50)

    state = RiskState()
    state.total_capital = 800_000

    # 模拟一个买入计划
    plan = {
        "code": "002049",
        "action": "buy",
        "price": 200.0,
        "shares": 1000,
        "sector": "半导体",
        "stock_type": "龙头",
        "stop_loss": 180.0
    }

    result = risk_check(plan, state, market_strength="normal")
    print(f"\n交易计划: 买入{plan['code']} {plan['shares']}股 @ {plan['price']}")
    print(f"风控结果: {'[PASS]' if result['pass'] else '[FAIL]'} ({result['level']})")
    for r in result["reasons"]:
        print(f"  - {r}")
    for w in result["warnings"]:
        print(f"  ! {w}")
    if result["adjusted_shares"] != plan["shares"]:
        print(f"  调整: {plan['shares']} -> {result['adjusted_shares']}股")

    print("\n" + daily_risk_summary(state, "normal"))
    print("\n[OK] 风控模块测试通过")
