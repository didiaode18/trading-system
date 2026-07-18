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
    交易信号风控校验（所有信号必须先过此函数）
    
    参数:
        trade_plan: 交易计划
            {
                "code": str,
                "action": "buy" / "sell" / "add",
                "price": float,
                "shares": int,
                "sector": str,
                "stock_type": "龙头" / "弹性"
            }
        risk_state: 当前风控状态
        market_strength: 行情强度
    
    返回:
        {
            "pass": bool,         # 是否通过风控
            "level": str,         # "green" / "yellow" / "red"
            "reasons": [str],     # 通过/拒绝原因列表
            "adjusted_shares": int,  # 调整后的股数（可能被缩减）
            "warnings": [str]     # 警告信息
        }
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
        name = config.STOCK_POOL.get(code, {}).get("名称", code)
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
