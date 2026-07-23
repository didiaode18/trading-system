# -*- coding: utf-8 -*-
"""
仓位管理引擎 (Position Sizing Engine)
======================================
解决"买多少"问题，避免重仓亏损/轻仓踏空

策略:
  1. 凯利公式: f* = (bp-q)/b
     - b=盈亏比, p=胜率(回测得出), q=1-p
  2. 风险预算: 单笔最大亏损 ≤ 总资金2%
     - 仓位 = (总资金×2%) / (入场价-止损价)
  3. 相关性调整: 同板块持仓不超过3只
  4. 波动率调整: ATR高的标的仓位减半
  5. 信号强度调整: DK分数越高仓位越大

输出:
  - 每只标的建议仓位（股数+金额+占比）
  - 组合风险度（0-100%）
  - 再平衡建议

使用:
    from risk.position_sizing import PositionSizer
    sizer = PositionSizer(total_capital=750000)
    plan = sizer.calc_positions(results, holdings)
"""

import numpy as np
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SIZING_CONFIG = {
    "total_capital": 750000,       # 总资金(元)
    "max_risk_per_trade": 0.02,    # 单笔最大风险(2%)
    "max_position_pct": 0.25,      # 单只最大仓位(25%)
    "min_position_pct": 0.05,      # 单只最小仓位(5%)
    "max_same_sector": 3,          # 同板块最多持仓数
    "max_total_positions": 6,      # 最大持仓数
    "kelly_fraction": 0.5,         # 凯利公式使用半凯利(保守)
    "default_win_rate": 0.45,      # 默认胜率(无回测数据时)
    "default_profit_factor": 1.5,  # 默认盈亏比
    "atr_high_threshold": 0.04,    # ATR>4%视为高波动
    "volatility_reduce": 0.5,      # 高波动仓位缩减比例
}

# 板块映射
STOCK_SECTOR = {
    "002415": "电子", "600036": "银行", "000858": "食品饮料",
    "603501": "电子", "601012": "电力设备", "002185": "电子",
    "001309": "电子", "002558": "传媒", "588000": "科技ETF",
    "159205": "金融ETF", "688234": "电子",
}


class PositionSizer:
    """仓位管理器"""

    def __init__(self, total_capital: float = None, config: dict = None):
        self.cfg = {**SIZING_CONFIG, **(config or {})}
        if total_capital:
            self.cfg["total_capital"] = total_capital

    def calc_positions(self, results: List[dict], holdings: dict = None) -> dict:
        """
        计算所有标的的建议仓位

        参数:
            results: CaopanEngine.analyze()结果列表
            holdings: 当前持仓 {code: {shares, cost, ...}}

        返回:
            仓位计划
        """
        capital = self.cfg["total_capital"]
        holdings = holdings or {}

        # 1. 筛选可操作标的（排除下跌趋势）
        candidates = []
        for r in results:
            trend = r.get("trend_level", 3)
            if trend >= 3:  # 只做震荡及以上
                candidates.append(r)

        # 2. 按多因子评分排序（如果有）
        candidates.sort(key=lambda r: self._get_priority_score(r), reverse=True)

        # 3. 板块限制
        candidates = self._apply_sector_limit(candidates)

        # 4. 取TOP N
        candidates = candidates[:self.cfg["max_total_positions"]]

        # 5. 计算每只仓位
        positions = []
        total_allocated = 0

        for r in candidates:
            pos = self._calc_single_position(r, capital, holdings)
            if pos["suggested_shares"] > 0:
                positions.append(pos)
                total_allocated += pos["suggested_amount"]

        # 6. 组合风险评估
        portfolio_risk = self._assess_portfolio_risk(positions, capital)

        # 7. 再平衡建议
        rebalance = self._calc_rebalance(positions, holdings, capital)

        return {
            "total_capital": capital,
            "positions": positions,
            "total_allocated": round(total_allocated, 0),
            "cash_remaining": round(capital - total_allocated, 0),
            "portfolio_risk": portfolio_risk,
            "rebalance": rebalance,
            "summary": self._generate_summary(positions, capital, portfolio_risk),
        }

    def _calc_single_position(self, r: dict, capital: float, holdings: dict) -> dict:
        """计算单只标的的建议仓位"""
        code = r.get("code", "")
        name = r.get("name", code)
        close = r.get("close", 0)
        atr = r.get("atr", 0)

        if close <= 0:
            return {"code": code, "name": name, "suggested_shares": 0, "suggested_amount": 0}

        # 止损价
        support = r.get("support_price", close * 0.95)
        stop_loss = support * 0.98  # 支撑位下方2%
        risk_per_share = close - stop_loss

        if risk_per_share <= 0:
            risk_per_share = atr * 1.5 if atr > 0 else close * 0.05

        # === 方法1: 风险预算法 ===
        max_loss = capital * self.cfg["max_risk_per_trade"]
        shares_by_risk = int(max_loss / risk_per_share)

        # === 方法2: 凯利公式 ===
        rr = r.get("risk_reward", {})
        profit_factor = rr.get("risk_reward_1", self.cfg["default_profit_factor"])
        win_rate = self.cfg["default_win_rate"]

        # 根据趋势调整胜率
        trend = r.get("trend_level", 3)
        if trend >= 5:
            win_rate = 0.55
        elif trend >= 4:
            win_rate = 0.50
        elif trend <= 2:
            win_rate = 0.30

        b = max(profit_factor, 1.0)
        p = win_rate
        q = 1 - p
        kelly = (b * p - q) / b if b > 0 else 0
        kelly = max(0, kelly * self.cfg["kelly_fraction"])  # 半凯利
        shares_by_kelly = int(capital * kelly / close)

        # === 取两者较小值 ===
        suggested_shares = min(shares_by_risk, shares_by_kelly)

        # === 波动率调整 ===
        if atr > 0 and close > 0:
            atr_pct = atr / close
            if atr_pct > self.cfg["atr_high_threshold"]:
                suggested_shares = int(suggested_shares * self.cfg["volatility_reduce"])

        # === 信号强度调整 ===
        dk_strength = r.get("dk_strength", 0)
        dk_signal = r.get("dk_signal")
        if dk_signal == "D" and dk_strength >= 70:
            suggested_shares = int(suggested_shares * 1.2)  # 强D点加仓20%
        elif dk_signal == "K":
            suggested_shares = int(suggested_shares * 0.5)  # K点减半

        # === 仓位上下限 ===
        max_shares = int(capital * self.cfg["max_position_pct"] / close)
        min_shares = int(capital * self.cfg["min_position_pct"] / close)
        suggested_shares = max(0, min(suggested_shares, max_shares))

        # 如果低于最小仓位，设为0（不值得开仓）
        if suggested_shares < min_shares * 0.5:
            suggested_shares = 0

        # A股100股整数倍
        suggested_shares = (suggested_shares // 100) * 100

        suggested_amount = suggested_shares * close
        position_pct = suggested_amount / capital * 100 if capital > 0 else 0

        # 当前持仓对比
        current = holdings.get(code, {})
        current_shares = current.get("shares", 0)
        action = "持有"
        if current_shares == 0 and suggested_shares > 0:
            action = "建仓"
        elif current_shares > 0 and suggested_shares == 0:
            action = "清仓"
        elif suggested_shares > current_shares * 1.2:
            action = "加仓"
        elif suggested_shares < current_shares * 0.8:
            action = "减仓"

        return {
            "code": code,
            "name": name,
            "close": close,
            "stop_loss": round(stop_loss, 3),
            "risk_per_share": round(risk_per_share, 3),
            "suggested_shares": suggested_shares,
            "suggested_amount": round(suggested_amount, 0),
            "position_pct": round(position_pct, 1),
            "kelly_pct": round(kelly * 100, 1),
            "current_shares": current_shares,
            "action": action,
            "trend_level": trend,
            "dk_signal": dk_signal,
        }

    def _apply_sector_limit(self, candidates: List[dict]) -> List[dict]:
        """板块限制: 同板块最多N只"""
        sector_count = {}
        filtered = []
        for r in candidates:
            code = r.get("code", "")
            sector = STOCK_SECTOR.get(code, "其他")
            count = sector_count.get(sector, 0)
            if count < self.cfg["max_same_sector"]:
                filtered.append(r)
                sector_count[sector] = count + 1
        return filtered

    def _get_priority_score(self, r: dict) -> float:
        """获取优先级分数"""
        score = r.get("trend_level", 3) * 20
        dk = r.get("dk_signal")
        if dk == "D" and not r.get("dk_filtered"):
            score += r.get("dk_strength", 0) * 0.3
        chip = r.get("chip")
        if chip:
            score += chip.get("control_level", {}).get("score", 0) * 0.2
        return score

    def _assess_portfolio_risk(self, positions: List[dict], capital: float) -> dict:
        """评估组合风险"""
        if not positions:
            return {"risk_level": "空仓", "risk_score": 0, "max_drawdown_est": 0}

        total_amount = sum(p["suggested_amount"] for p in positions)
        invested_pct = total_amount / capital * 100 if capital > 0 else 0

        # 加权ATR风险
        weighted_risk = 0
        for p in positions:
            if p["suggested_amount"] > 0:
                risk_pct = p["risk_per_share"] / p["close"] * 100 if p["close"] > 0 else 5
                weight = p["suggested_amount"] / total_amount if total_amount > 0 else 0
                weighted_risk += risk_pct * weight

        # 最大预估回撤
        max_dd = invested_pct * weighted_risk / 100

        if max_dd > 10:
            risk_level = "高风险"
        elif max_dd > 5:
            risk_level = "中风险"
        elif max_dd > 2:
            risk_level = "低风险"
        else:
            risk_level = "极低风险"

        return {
            "risk_level": risk_level,
            "risk_score": round(max_dd * 10, 1),
            "invested_pct": round(invested_pct, 1),
            "max_drawdown_est": round(max_dd, 2),
            "position_count": len(positions),
        }

    def _calc_rebalance(self, positions: List[dict], holdings: dict, capital: float) -> List[dict]:
        """计算再平衡建议"""
        rebalance = []
        for p in positions:
            code = p["code"]
            current = p["current_shares"]
            target = p["suggested_shares"]
            diff = target - current

            if abs(diff) >= 100:  # 至少100股才调整
                action = "买入" if diff > 0 else "卖出"
                amount = abs(diff) * p["close"]
                rebalance.append({
                    "code": code,
                    "name": p["name"],
                    "action": action,
                    "shares": abs(diff),
                    "amount": round(amount, 0),
                    "reason": f"目标{target}股 vs 当前{current}股",
                })

        # 检查需要清仓的（在holdings中但不在positions中）
        position_codes = {p["code"] for p in positions}
        for code, info in holdings.items():
            if code not in position_codes and info.get("shares", 0) > 0:
                rebalance.append({
                    "code": code,
                    "name": info.get("name", code),
                    "action": "清仓",
                    "shares": info.get("shares", 0),
                    "amount": round(info.get("shares", 0) * info.get("buy_price", 0), 0),
                    "reason": "不满足持仓条件(趋势<3级)",
                })

        return rebalance

    def _generate_summary(self, positions: List[dict], capital: float, risk: dict) -> str:
        """生成摘要"""
        lines = []
        total = sum(p["suggested_amount"] for p in positions)
        lines.append(f"总资金: {capital/10000:.1f}万 | 配置: {total/10000:.1f}万 | 现金: {(capital-total)/10000:.1f}万")
        lines.append(f"风险等级: {risk['risk_level']} | 预估最大回撤: {risk['max_drawdown_est']:.1f}%")
        return " | ".join(lines)


def position_summary(plan: dict) -> str:
    """仓位计划摘要文本"""
    lines = ["📊 仓位管理计划"]
    lines.append(f"  {plan['summary']}")
    lines.append("")
    lines.append(f"  {'标的':<10} {'操作':<4} {'目标股数':<8} {'金额(万)':<8} {'占比':<6} {'止损':<8} {'凯利%':<6}")
    lines.append("  " + "─" * 60)

    for p in plan["positions"]:
        lines.append(
            f"  {p['name']:<10} {p['action']:<4} {p['suggested_shares']:<8} "
            f"{p['suggested_amount']/10000:<8.1f} {p['position_pct']:<5.1f}% "
            f"{p['stop_loss']:<8.3f} {p['kelly_pct']:<5.1f}%"
        )

    rebalance = plan.get("rebalance", [])
    if rebalance:
        lines.append(f"\n  再平衡操作:")
        for rb in rebalance:
            icon = "🔴" if rb["action"] in ("买入", "加仓") else "🟢"
            lines.append(f"    {icon} {rb['name']} {rb['action']} {rb['shares']}股 ({rb['amount']/10000:.1f}万) | {rb['reason']}")

    return "\n".join(lines)
