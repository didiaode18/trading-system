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
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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
    "consecutive_loss_today": 2,    # 连续亏损2笔 → 当日禁止开仓
    "consecutive_loss_block": 3,    # 连续亏损3笔 → 暂停3天
    "consecutive_loss_pause": 3,    # 暂停天数

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
            "total_capital": 424000,        # 总资金
        }
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                default.update(saved)
        except Exception:
            pass

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
        cooldown_days = RISK_CONFIG["cooldown_after_sell"]
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


# ============================================================
# 核心风控引擎
# ============================================================
class RiskGate:
    """
    风控硬拦截门
    所有信号必须通过此门，不通过直接淘汰

    使用方式:
        gate = RiskGate(total_capital=424000)
        result = gate.check_buy(signal, holdings, market_info)
        if not result["pass"]:
            print(f"拦截: {result['reason']}")
    """

    def __init__(self, total_capital: float = 424000, allowed_pool: set = None):
        self.cfg = RISK_CONFIG
        self.total_capital = total_capital
        self.state_mgr = RiskStateManager()
        self.state_mgr.state["total_capital"] = total_capital
        # 允许交易的股票池（核心池+观察池），不在池内的标的禁止买入
        self.allowed_pool = allowed_pool  # None=不限制, set=只允许池内标的

    # --------------------------------------------------------
    # 主入口：买入/加仓信号校验
    # --------------------------------------------------------
    def check_buy(self, signal: dict, holdings: dict,
                  market_info: dict = None) -> dict:
        """
        买入/加仓信号风控校验（硬拦截）

        参数:
            signal: {"code", "name", "price", "shares", "sector", "type",
                     "stop_loss", "target", "risk_reward"}
            holdings: {code: {"shares", "cost", "price", "sector", "type"}}
            market_info: {"index_price", "index_ma20", "index_ma60"} 指数均线

        返回:
            {"pass": bool, "reason": str, "adjusted_shares": int, "level": str}
        """
        code = signal.get("code", "")
        name = signal.get("name", code)
        price = signal.get("price", 0)
        shares = signal.get("shares", 0)
        sector = signal.get("sector", "")
        stock_type = signal.get("type", "stock")  # "etf" / "stock"
        risk_reward = signal.get("risk_reward", 0)
        amount = price * shares

        # ===== 第0关：盈亏比硬门槛 =====
        if risk_reward > 0 and risk_reward < self.cfg["min_risk_reward"]:
            return self._block(
                f"盈亏比不达标: {risk_reward:.2f} < {self.cfg['min_risk_reward']}，"
                f"拦截（宁缺毋滥）"
            )

        # ===== 第0.5关：股票池外拦截（杠绝临时起意）=====
        if self.allowed_pool is not None and code not in self.allowed_pool:
            return self._block(
                f"池外拦截: {name}({code})不在核心池/观察池中，"
                f"禁止买入（杠绝随手交易）"
            )

        # ===== 第1关：暂停期检查 =====
        paused, pause_reason = self.state_mgr.is_paused()
        if paused:
            return self._block(pause_reason)

        # ===== 第2关：冷却期检查 =====
        cooling, cool_reason = self.state_mgr.is_cooling_down(code)
        if cooling:
            return self._block(cool_reason)

        # ===== 第3关：日开仓次数限制 =====
        if self.state_mgr.state["daily_open_count"] >= self.cfg["max_daily_opens"]:
            return self._block(
                f"今日已开仓{self.state_mgr.state['daily_open_count']}笔，"
                f"达到上限{self.cfg['max_daily_opens']}笔，禁止再开"
            )

        # ===== 第4关：连续亏损熔断 =====
        consec = self.state_mgr.state["consecutive_losses"]
        if consec >= self.cfg["consecutive_loss_block"]:
            # 连续3笔 → 暂停3天
            pause_days = self.cfg["consecutive_loss_pause"]
            until = (datetime.date.today() + datetime.timedelta(days=pause_days)).isoformat()
            self.state_mgr.state["pause_until"] = until
            self.state_mgr.save()
            return self._block(
                f"连续亏损{consec}笔触发熔断，暂停开仓{pause_days}天（至{until}）"
            )
        elif consec >= self.cfg.get("consecutive_loss_today", 2):
            # 连续2笔 → 当日禁止开仓
            return self._block(
                f"连续亏损{consec}笔，当日禁止开仓（强制冷静）"
            )

        # ===== 第5关：日度亏损熔断 =====
        daily_pnl = self.state_mgr.state.get("daily_pnl", 0)
        if daily_pnl < 0 and abs(daily_pnl) / self.total_capital >= self.cfg["daily_loss_block"]:
            return self._block(
                f"单日亏损{abs(daily_pnl)/self.total_capital:.1%}≥{self.cfg['daily_loss_block']:.0%}，"
                f"当日禁止新开仓"
            )

        # ===== 第6关：周度亏损熔断 =====
        weekly_pnl = self.state_mgr.state.get("weekly_pnl", 0)
        if weekly_pnl < 0 and abs(weekly_pnl) / self.total_capital >= self.cfg["weekly_loss_force"]:
            return self._block(
                f"本周亏损{abs(weekly_pnl)/self.total_capital:.1%}≥{self.cfg['weekly_loss_force']:.0%}，"
                f"强制降仓+暂停{self.cfg['weekly_loss_pause_days']}天"
            )

        # ===== 第7关：浮亏加仓绝对拦截 =====
        if self.cfg["block_add_on_loss"] and code in holdings:
            pos = holdings[code]
            cost = pos.get("cost", 0)
            cur_price = pos.get("price", cost)
            if cost > 0 and cur_price < cost:
                loss_pct = (cur_price - cost) / cost * 100
                return self._block(
                    f"浮亏加仓拦截: {name}当前浮亏{loss_pct:.1f}%，"
                    f"绝对禁止补仓/加仓（铁律）"
                )

        # ===== 第8关：持仓数量限制 =====
        if code not in holdings and len(holdings) >= self.cfg["max_holdings"]:
            return self._block(
                f"持仓数量{len(holdings)}只已达上限{self.cfg['max_holdings']}只，"
                f"禁止新开仓"
            )

        # ===== 第9关：三级仓位硬限制 =====
        # 9a. 单只仓位
        if stock_type == "etf":
            single_max = self.cfg["etf_max_ratio"]
        else:
            single_max = self.cfg["stock_max_ratio"]

        current_amount = 0
        if code in holdings:
            pos = holdings[code]
            current_amount = pos.get("shares", 0) * pos.get("price", 0)

        new_single_ratio = (current_amount + amount) / self.total_capital
        if new_single_ratio > single_max:
            # 计算允许的最大买入量
            allowed_amount = self.total_capital * single_max - current_amount
            if allowed_amount <= 0:
                return self._block(
                    f"单只仓位超限: {name}将达{new_single_ratio:.1%} > "
                    f"上限{single_max:.0%}，拦截"
                )
            # 缩减到允许范围
            adjusted_shares = int(allowed_amount / price / 100) * 100
            if adjusted_shares < 100:
                return self._block(
                    f"单只仓位超限: {name}已达上限{single_max:.0%}，无法再买"
                )
            shares = adjusted_shares
            amount = shares * price

        # 9b. 赛道仓位
        if sector:
            sector_amount = sum(
                h.get("shares", 0) * h.get("price", 0)
                for h in holdings.values()
                if h.get("sector") == sector
            )
            new_sector_ratio = (sector_amount + amount) / self.total_capital
            if new_sector_ratio > self.cfg["sector_max_ratio"]:
                return self._block(
                    f"赛道仓位超限: {sector}将达{new_sector_ratio:.1%} > "
                    f"上限{self.cfg['sector_max_ratio']:.0%}，拦截"
                )

        # 9c. 总仓位（动态，与指数均线绑定）
        total_position = sum(
            h.get("shares", 0) * h.get("price", 0)
            for h in holdings.values()
        )
        max_total = self._get_dynamic_total_limit(market_info)
        new_total_ratio = (total_position + amount) / self.total_capital
        if new_total_ratio > max_total:
            return self._block(
                f"总仓位超限: 将达{new_total_ratio:.1%} > "
                f"动态上限{max_total:.0%}，拦截"
            )

        # ===== 第10关：现金安全垫 =====
        cash_after = self.total_capital - total_position - amount
        min_cash = self.total_capital * self.cfg["min_cash_ratio"]
        if cash_after < min_cash:
            return self._block(
                f"突破现金安全垫: 剩余{cash_after:.0f}元 < "
                f"最低保留{min_cash:.0f}元({self.cfg['min_cash_ratio']:.0%})"
            )

        # ===== 全部通过 =====
        return {
            "pass": True,
            "reason": "风控通过",
            "level": "green",
            "adjusted_shares": shares,
            "warnings": [],
        }

    # --------------------------------------------------------
    # 卖出信号校验（卖出一般不拦截，但记录状态）
    # --------------------------------------------------------
    def check_sell(self, signal: dict, holdings: dict) -> dict:
        """卖出信号处理：记录冷却期、更新连续亏损"""
        code = signal.get("code", "")
        pnl = signal.get("pnl", 0)  # 本笔盈亏

        # 记录卖出冷却
        self.state_mgr.record_sell(code)

        # 更新连续亏损计数
        if pnl < 0:
            self.state_mgr.record_loss()
        else:
            self.state_mgr.record_profit()

        return {"pass": True, "reason": "卖出放行", "level": "green"}

    # --------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------
    def _get_dynamic_total_limit(self, market_info: dict = None) -> float:
        """根据指数与MA20/MA60关系动态计算总仓位上限"""
        if not market_info:
            return self.cfg["total_above_ma20"]  # 无数据默认80%

        index_price = market_info.get("index_price", 0)
        index_ma20 = market_info.get("index_ma20", 0)
        index_ma60 = market_info.get("index_ma60", 0)

        if index_price <= 0 or index_ma20 <= 0:
            return self.cfg["total_above_ma20"]

        if index_price < index_ma60:
            return self.cfg["total_below_ma60"]   # 跌破MA60 → 30%
        elif index_price < index_ma20:
            return self.cfg["total_below_ma20"]   # 跌破MA20 → 50%
        else:
            return self.cfg["total_above_ma20"]   # MA20上方 → 80%

    def _block(self, reason: str) -> dict:
        """生成拦截结果"""
        logger.warning(f"[风控拦截] {reason}")
        return {
            "pass": False,
            "reason": reason,
            "level": "red",
            "adjusted_shares": 0,
            "warnings": [],
        }

    # --------------------------------------------------------
    # 持仓健康度巡检（模块六集成）
    # --------------------------------------------------------
    def inspect_holdings(self, holdings: dict, market_info: dict = None) -> List[dict]:
        """
        持仓风险四级巡检
        返回每只持仓的风险等级和处置建议

        等级:
          健康: 多头趋势 + 浮盈 + 止损上移 → 持有
          关注: 多头趋势 + 浮亏<10% → 持有+带止损
          预警: 空头趋势 + 浮亏<15% → 反弹减仓
          危险: 浮亏>15% / 跌破终极止损 → 无条件清仓
        """
        results = []
        for code, pos in holdings.items():
            cost = pos.get("cost", 0)
            price = pos.get("price", cost)
            name = pos.get("name", code)
            ma20 = pos.get("ma20", price)
            ma60 = pos.get("ma60", price)

            pnl_pct = (price / cost - 1) * 100 if cost > 0 else 0
            is_bullish = price > ma20 and ma20 > ma60

            # 四级分类
            if pnl_pct <= -15 or (price < ma60 and pnl_pct < -10):
                level = "危险"
                action = "无条件清仓"
                urgency = 0
            elif not is_bullish and pnl_pct < 0:
                level = "预警"
                action = "反弹减仓（设14:50条件单）"
                urgency = 1
            elif not is_bullish and pnl_pct >= 0:
                # 非多头但有浮盈，关注趋势转变
                level = "关注"
                action = "持有观察，跌破MA20即减仓"
                urgency = 2
            elif is_bullish and pnl_pct < 0:
                level = "关注"
                action = "持有+带好止损（成本×90%）"
                urgency = 2
            else:
                level = "健康"
                action = "持有，止损上移"
                urgency = 3

            # 仓位超标检查
            position_ratio = (pos.get("shares", 0) * price) / self.total_capital
            stock_type = pos.get("type", "stock")
            max_ratio = self.cfg["etf_max_ratio"] if stock_type == "etf" else self.cfg["stock_max_ratio"]
            over_limit = position_ratio > max_ratio

            reduce_shares = 0
            if over_limit:
                target_amount = self.total_capital * max_ratio
                current_amount = pos.get("shares", 0) * price
                reduce_amount = current_amount - target_amount
                reduce_shares = int(reduce_amount / price / 100) * 100

            results.append({
                "code": code,
                "name": name,
                "level": level,
                "urgency": urgency,
                "pnl_pct": round(pnl_pct, 2),
                "is_bullish": is_bullish,
                "action": action,
                "position_ratio": round(position_ratio * 100, 1),
                "over_limit": over_limit,
                "reduce_shares": reduce_shares,
                "stop_loss": round(cost * 0.9, 3) if level in ("关注", "预警") else round(price * 0.92, 3),
            })

        # 按紧急程度排序（危险在前）
        results.sort(key=lambda x: x["urgency"])
        return results


# ============================================================
# 便捷函数（供报告生成器/条件单调用）
# ============================================================
def quick_risk_check(signal: dict, holdings: dict,
                     total_capital: float = 424000,
                     market_info: dict = None) -> dict:
    """
    快速风控校验（一行调用）

    用法:
        from risk.risk_control import quick_risk_check
        result = quick_risk_check(signal, holdings)
        if not result["pass"]:
            print(f"被拦截: {result['reason']}")
    """
    gate = RiskGate(total_capital=total_capital)
    return gate.check_buy(signal, holdings, market_info)


def quick_inspect(holdings: dict, total_capital: float = 424000) -> List[dict]:
    """快速持仓巡检"""
    gate = RiskGate(total_capital=total_capital)
    return gate.inspect_holdings(holdings)
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
               market_strength: str = "normal") -> dict:
    """
    交易信号风控校验（V3.0升级版，所有信号必须先过此函数）
    
    新增检查:
    - 持仓数量硬限制: <= 7只
    - 单笔亏损达总资金2%无条件止损
    - 浮亏持仓禁止加仓
    """
    result = {
        "pass": True,
        "level": "green",
        "reasons": [],
        "adjusted_shares": trade_plan.get("shares", 0),
        "warnings": []
    }

    action = trade_plan.get("action", "buy")

    # 卖出信号不需要风控检查
    if action == "sell":
        result["reasons"].append("卖出信号，直接通过")
        return result

    # ---- 以下仅对买入/加仓进行校验 ----

    code = trade_plan.get("code", "")
    price = trade_plan.get("price", 0)
    shares = trade_plan.get("shares", 0)
    sector = trade_plan.get("sector", "")
    stock_type = trade_plan.get("stock_type", "龙头")
    amount = shares * price

    # ---- 检查-1: 满仓禁止加仓（情绪化交易防护，最高优先级）----
    full_threshold = getattr(config, 'FULL_POSITION_THRESHOLD', 0.90)
    near_full = getattr(config, 'NEAR_FULL_POSITION', 0.80)
    current_position_ratio = risk_state.get_position_ratio()
    available_cash = getattr(config, 'AVAILABLE_CASH', 0)

    # 硬性规则：仓位>=90% 或 可用资金不足 → 绝对禁止任何买入
    if current_position_ratio >= full_threshold:
        result["pass"] = False
        result["level"] = "red"
        result["reasons"].append(
            f"★满仓禁止: 当前仓位{current_position_ratio:.1%} >= {full_threshold:.0%}红线，"
            f"绝对禁止买入/加仓（情绪化交易防护）"
        )
        return result

    # 可用资金不足 → 禁止买入
    if available_cash < amount and available_cash < price * 100:
        result["pass"] = False
        result["level"] = "red"
        result["reasons"].append(
            f"资金不足: 可用{available_cash:.0f}元 < 最低买入{price*100:.0f}元，禁止买入"
        )
        return result

    # 仓位>=80% → 禁止新开仓（只允许已持仓的减仓操作）
    if current_position_ratio >= near_full and code not in risk_state.current_positions:
        result["pass"] = False
        result["level"] = "red"
        result["reasons"].append(
            f"仓位过高: 当前{current_position_ratio:.1%} >= {near_full:.0%}，"
            f"禁止新开仓（只允许减仓）"
        )
        return result

    # ---- 检查0: 持仓数量硬限制 ----
    max_holdings = getattr(config, 'MAX_HOLDINGS', 7)
    current_count = len(risk_state.current_positions)
    if action == "buy" and code not in risk_state.current_positions:
        if current_count >= max_holdings:
            result["pass"] = False
            result["level"] = "red"
            result["reasons"].append(
                f"持仓数量超限: 当前{current_count}只 >= 上限{max_holdings}只，禁止新开仓"
            )
            return result

    # ---- 检查0.5: 浮亏持仓禁止加仓 ----
    if action == "add" and code in risk_state.current_positions:
        pos = risk_state.current_positions[code]
        buy_p = pos.get("buy_price", 0)
        cur_p = pos.get("current_price", buy_p)
        if buy_p > 0 and cur_p < buy_p:
            result["pass"] = False
            result["level"] = "red"
            result["reasons"].append(
                f"铁则违反: {code}当前浮亏{(cur_p-buy_p)/buy_p:.2%}，绝对禁止加仓"
            )
            return result

    # ---- 检查1: 时间红线 ----
    now = datetime.datetime.now()
    current_time = now.strftime("%H:%M")
    if _in_no_trade_zone(current_time):
        result["pass"] = False
        result["level"] = "red"
        result["reasons"].append(f"当前时间{current_time}处于禁止交易时段")
        return result

    # ---- 检查2: 日度熔断 ----
    daily_loss_ratio = abs(risk_state.daily_pnl) / risk_state.total_capital if risk_state.daily_pnl < 0 else 0
    if daily_loss_ratio >= config.DAILY_LOSS_LIMIT_2:
        result["pass"] = False
        result["level"] = "red"
        result["reasons"].append(
            f"日度熔断L2: 当日亏损{daily_loss_ratio:.2%} >= {config.DAILY_LOSS_LIMIT_2:.0%}，禁止开新仓"
        )
        return result

    if daily_loss_ratio >= config.DAILY_LOSS_LIMIT_1:
        result["pass"] = False
        result["level"] = "red"
        result["reasons"].append(
            f"日度熔断L1: 当日亏损{daily_loss_ratio:.2%} >= {config.DAILY_LOSS_LIMIT_1:.0%}，只卖不买"
        )
        return result

    # ---- 检查3: 周度熔断 ----
    weekly_loss_ratio = abs(risk_state.weekly_pnl) / risk_state.total_capital if risk_state.weekly_pnl < 0 else 0
    if weekly_loss_ratio >= config.WEEKLY_LOSS_LIMIT:
        result["pass"] = False
        result["level"] = "red"
        result["reasons"].append(
            f"周度熔断: 本周亏损{weekly_loss_ratio:.2%} >= {config.WEEKLY_LOSS_LIMIT:.0%}，强制休息"
        )
        return result

    # ---- 检查4: 个股仓位上限 ----
    if stock_type == "弹性":
        max_ratio = config.FLEXIBLE_STOCK_MAX_RATIO
    else:
        max_ratio = config.LEADER_STOCK_MAX_RATIO
    current_stock_amount = 0
    if code in risk_state.current_positions:
        pos = risk_state.current_positions[code]
        current_stock_amount = pos.get("shares", 0) * pos.get("current_price", pos.get("buy_price", 0))

    new_total = current_stock_amount + amount
    if new_total / risk_state.total_capital > max_ratio:
        # 尝试缩减仓位
        max_amount = risk_state.total_capital * max_ratio - current_stock_amount
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
            result["pass"] = False
            result["level"] = "red"
            result["reasons"].append(f"个股仓位超限{max_ratio:.0%}且无法缩减")
            return result

    # ---- 检查5: 赛道仓位上限 ----
    if sector:
        sector_amount = risk_state.get_sector_amount(sector)
        new_sector = sector_amount + amount
        if new_sector / risk_state.total_capital > config.SECTOR_MAX_RATIO:
            result["warnings"].append(
                f"赛道仓位预警: {sector}将达到{new_sector/risk_state.total_capital:.1%}，"
                f"上限{config.SECTOR_MAX_RATIO:.0%}"
            )
            result["level"] = "yellow" if result["level"] == "green" else result["level"]

    # ---- 检查6: 总仓位上限（按行情强度）----
    max_pos_ratio = get_max_position_ratio(market_strength)
    current_pos_ratio = risk_state.get_position_ratio()
    new_pos_amount = risk_state.get_total_position_amount() + amount
    new_pos_ratio = new_pos_amount / risk_state.total_capital

    if new_pos_ratio > max_pos_ratio:
        result["warnings"].append(
            f"总仓位预警: 将达{new_pos_ratio:.1%}，"
            f"当前行情({market_strength})上限{max_pos_ratio:.0%}"
        )
        if new_pos_ratio > max_pos_ratio + 0.05:  # 超5%以上直接拒绝
            result["pass"] = False
            result["level"] = "red"
            result["reasons"].append(f"总仓位严重超限: {new_pos_ratio:.1%} > {max_pos_ratio:.0%}")
            return result

    # ---- 检查7: 现金安全垫 ----
    cash_after = risk_state.total_capital - risk_state.get_total_position_amount() - amount
    min_cash = risk_state.total_capital * config.CASH_RESERVE_RATIO
    if cash_after < min_cash:
        result["pass"] = False
        result["level"] = "red"
        result["reasons"].append(
            f"突破现金安全垫: 剩余{cash_after:.0f} < 最低保留{min_cash:.0f}({config.CASH_RESERVE_RATIO:.0%})"
        )
        return result

    # ---- 检查8: 单笔亏损控制 ----
    stop_loss = trade_plan.get("stop_loss", price * (1 - config.INITIAL_STOP_LOSS_PCT))
    max_loss = shares * (price - stop_loss)
    max_loss_ratio = max_loss / risk_state.total_capital
    if max_loss_ratio > config.MAX_SINGLE_LOSS_RATIO:
        result["warnings"].append(
            f"单笔亏损偏大: {max_loss_ratio:.2%} > {config.MAX_SINGLE_LOSS_RATIO:.0%}"
        )
        result["level"] = "yellow" if result["level"] == "green" else result["level"]

    if result["pass"]:
        result["reasons"].append("风控校验通过")

    return result


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
    if daily_loss >= config.DAILY_LOSS_LIMIT_2:
        lines.append(f"\n  [!] 日度熔断L2触发，禁止开新仓")
    elif daily_loss >= config.DAILY_LOSS_LIMIT_1:
        lines.append(f"\n  [!] 日度熔断L1触发，只卖不买")

    return "\n".join(lines)


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
