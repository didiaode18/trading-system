# -*- coding: utf-8 -*-
"""
绩效归因与交易行为诊断模块 V1.0
================================
精准定位亏损根源，支撑策略持续迭代

核心功能:
  1. 收益归因拆解:
     - 按标的: 每只股票的累计盈亏、胜率、盈亏比
     - 按赛道: 每个行业的贡献收益
     - 按月份: 月度收益曲线，识别赚钱/亏钱时间段
     - 按离场类型: 止损/止盈/被动离场的占比与盈亏
  2. 交易行为诊断（月报）:
     - 换手率、交易频次、非计划交易占比
     - 自动识别交易陋习（过度交易、追涨杀跌、越跌越补）
  3. 执行偏差报告:
     - 对比策略信号与实盘操作的差异

使用:
    from strategy.performance_attribution import PerformanceAnalyzer
    analyzer = PerformanceAnalyzer(trades_df)
    report = analyzer.full_report()
"""

import pandas as pd
import numpy as np
import datetime
import logging
from typing import Dict, List, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


class PerformanceAnalyzer:
    """
    绩效归因分析器

    参数:
        trades_df: 交易记录DataFrame，需包含列:
            date, code, name, sector, action(buy/sell), price, shares,
            amount, pnl, pnl_pct, hold_days, exit_type(stop_loss/take_profit/time_stop/trend_break)
        initial_capital: 初始资金
    """

    def __init__(self, trades_df: pd.DataFrame, initial_capital: float = 424000):
        self.trades = trades_df
        self.initial_capital = initial_capital
        self.sell_trades = trades_df[trades_df["action"] == "sell"] if not trades_df.empty else pd.DataFrame()

    # ============================================================
    # 一、核心绩效指标
    # ============================================================
    def core_metrics(self) -> dict:
        """计算核心绩效指标（含交易成本后的真实数据）"""
        if self.sell_trades.empty:
            return {"error": "无卖出交易记录"}

        st = self.sell_trades
        total_trades = len(st)
        win_trades = len(st[st["pnl"] > 0])
        lose_trades = total_trades - win_trades
        win_rate = win_trades / total_trades if total_trades > 0 else 0

        avg_win = st[st["pnl"] > 0]["pnl"].mean() if win_trades > 0 else 0
        avg_lose = abs(st[st["pnl"] < 0]["pnl"].mean()) if lose_trades > 0 else 0
        profit_factor = avg_win / avg_lose if avg_lose > 0 else 0

        total_pnl = st["pnl"].sum()
        avg_hold = st["hold_days"].mean() if "hold_days" in st.columns else 0

        # 最大连续亏损
        max_consec = 0
        consec = 0
        for _, t in st.iterrows():
            if t.get("pnl", 0) < 0:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 0

        # 数学期望
        expectancy = win_rate * avg_win - (1 - win_rate) * avg_lose

        return {
            "total_trades": total_trades,
            "win_trades": win_trades,
            "lose_trades": lose_trades,
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 2),
            "avg_win": round(avg_win, 2),
            "avg_lose": round(avg_lose, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_hold_days": round(avg_hold, 1),
            "max_consecutive_loss": max_consec,
            "expectancy": round(expectancy, 2),
            "total_return": round(total_pnl / self.initial_capital, 4),
        }

    # ============================================================
    # 二、按标的归因
    # ============================================================
    def by_stock(self) -> pd.DataFrame:
        """按标的拆分: 每只股票的累计盈亏、胜率、盈亏比"""
        if self.sell_trades.empty:
            return pd.DataFrame()

        st = self.sell_trades
        grouped = st.groupby(["code", "name"] if "name" in st.columns else "code")

        results = []
        for (code, *rest), group in grouped:
            name = rest[0] if rest else code
            trades_count = len(group)
            wins = len(group[group["pnl"] > 0])
            losses = trades_count - wins
            total_pnl = group["pnl"].sum()
            win_rate = wins / trades_count if trades_count > 0 else 0

            avg_win = group[group["pnl"] > 0]["pnl"].mean() if wins > 0 else 0
            avg_lose = abs(group[group["pnl"] < 0]["pnl"].mean()) if losses > 0 else 0
            pf = avg_win / avg_lose if avg_lose > 0 else 0

            results.append({
                "代码": code,
                "名称": name,
                "交易次数": trades_count,
                "胜率": f"{win_rate:.0%}",
                "盈亏比": round(pf, 2),
                "累计盈亏": round(total_pnl, 0),
                "贡献度": f"{total_pnl / abs(st['pnl'].sum()) * 100:.1f}%" if st["pnl"].sum() != 0 else "0%",
            })

        df = pd.DataFrame(results)
        return df.sort_values("累计盈亏", ascending=False)

    # ============================================================
    # 三、按赛道归因
    # ============================================================
    def by_sector(self) -> pd.DataFrame:
        """按赛道拆分: 每个行业的贡献收益"""
        if self.sell_trades.empty or "sector" not in self.sell_trades.columns:
            return pd.DataFrame()

        st = self.sell_trades
        grouped = st.groupby("sector")

        results = []
        for sector, group in grouped:
            trades_count = len(group)
            wins = len(group[group["pnl"] > 0])
            total_pnl = group["pnl"].sum()
            win_rate = wins / trades_count if trades_count > 0 else 0

            results.append({
                "赛道": sector,
                "交易次数": trades_count,
                "胜率": f"{win_rate:.0%}",
                "累计盈亏": round(total_pnl, 0),
                "贡献度": f"{total_pnl / abs(st['pnl'].sum()) * 100:.1f}%" if st["pnl"].sum() != 0 else "0%",
            })

        df = pd.DataFrame(results)
        return df.sort_values("累计盈亏", ascending=False)

    # ============================================================
    # 四、按月份归因
    # ============================================================
    def by_month(self) -> pd.DataFrame:
        """按月份拆分: 月度收益曲线"""
        if self.sell_trades.empty:
            return pd.DataFrame()

        st = self.sell_trades.copy()
        st["month"] = pd.to_datetime(st["date"]).dt.to_period("M")

        grouped = st.groupby("month")
        results = []
        for month, group in grouped:
            trades_count = len(group)
            wins = len(group[group["pnl"] > 0])
            total_pnl = group["pnl"].sum()
            win_rate = wins / trades_count if trades_count > 0 else 0

            results.append({
                "月份": str(month),
                "交易次数": trades_count,
                "胜率": f"{win_rate:.0%}",
                "月度盈亏": round(total_pnl, 0),
                "月收益率": f"{total_pnl / self.initial_capital:.2%}",
            })

        return pd.DataFrame(results)

    # ============================================================
    # 五、按离场类型归因
    # ============================================================
    def by_exit_type(self) -> pd.DataFrame:
        """按离场类型拆分: 止损/止盈/时间止损/趋势破位"""
        if self.sell_trades.empty or "exit_type" not in self.sell_trades.columns:
            return pd.DataFrame()

        st = self.sell_trades
        grouped = st.groupby("exit_type")

        type_names = {
            "stop_loss": "止损离场",
            "take_profit": "止盈离场",
            "time_stop": "时间止损",
            "trend_break": "趋势破位",
            "force_sell": "强制卖出",
            "drawdown_profit": "回落止盈",
        }

        results = []
        for exit_type, group in grouped:
            trades_count = len(group)
            total_pnl = group["pnl"].sum()
            avg_pnl = group["pnl"].mean()
            wins = len(group[group["pnl"] > 0])

            results.append({
                "离场类型": type_names.get(exit_type, exit_type),
                "笔数": trades_count,
                "占比": f"{trades_count / len(st):.0%}",
                "累计盈亏": round(total_pnl, 0),
                "平均盈亏": round(avg_pnl, 0),
                "胜率": f"{wins / trades_count:.0%}" if trades_count > 0 else "0%",
            })

        return pd.DataFrame(results)

    # ============================================================
    # 六、交易行为诊断（月报）
    # ============================================================
    def behavior_diagnosis(self) -> dict:
        """
        交易行为诊断: 识别交易陋习
        - 过度交易: 日均交易>3笔
        - 追涨杀跌: 买入后3天内卖出且亏损
        - 越跌越补: 同一标的浮亏时加仓
        - 持仓过短: 平均持仓<3天
        """
        if self.trades.empty:
            return {"error": "无交易记录"}

        all_trades = self.trades
        buy_trades = all_trades[all_trades["action"] == "buy"]
        sell_trades = self.sell_trades

        # 基本统计
        total_days = (pd.to_datetime(all_trades["date"]).max() -
                      pd.to_datetime(all_trades["date"]).min()).days + 1
        daily_avg_trades = len(all_trades) / max(total_days, 1)

        # 平均持仓周期
        avg_hold = sell_trades["hold_days"].mean() if "hold_days" in sell_trades.columns and not sell_trades.empty else 0

        # 诊断结论
        issues = []
        if daily_avg_trades > 3:
            issues.append(f"⚠️ 过度交易: 日均{daily_avg_trades:.1f}笔（标准≤2笔）")
        if avg_hold < 3 and avg_hold > 0:
            issues.append(f"⚠️ 持仓过短: 平均{avg_hold:.1f}天（中线标准3-20天）")

        # 短线亏损占比（持仓≤3天且亏损）
        if not sell_trades.empty and "hold_days" in sell_trades.columns:
            short_loss = sell_trades[(sell_trades["hold_days"] <= 3) & (sell_trades["pnl"] < 0)]
            short_loss_ratio = len(short_loss) / len(sell_trades) if len(sell_trades) > 0 else 0
            if short_loss_ratio > 0.3:
                issues.append(f"⚠️ 短线亏损占比{short_loss_ratio:.0%}（持仓≤3天且亏损）")

        # 非计划交易（不在股票池内的交易）
        # 这里需要外部传入allowed_pool，暂时跳过

        return {
            "total_trades": len(all_trades),
            "total_days": total_days,
            "daily_avg_trades": round(daily_avg_trades, 1),
            "avg_hold_days": round(avg_hold, 1),
            "issues": issues,
            "diagnosis": "交易行为正常" if not issues else "；".join(issues),
        }

    # ============================================================
    # 七、完整报告
    # ============================================================
    def full_report(self) -> dict:
        """生成完整绩效归因报告"""
        return {
            "core_metrics": self.core_metrics(),
            "by_stock": self.by_stock(),
            "by_sector": self.by_sector(),
            "by_month": self.by_month(),
            "by_exit_type": self.by_exit_type(),
            "behavior_diagnosis": self.behavior_diagnosis(),
        }

    def format_report(self) -> str:
        """格式化为文本报告"""
        report = self.full_report()
        lines = [
            "=" * 60,
            "  绩效归因报告",
            "=" * 60,
        ]

        # 核心指标
        m = report["core_metrics"]
        if "error" not in m:
            lines += [
                "",
                "  --- 核心指标 ---",
                f"  交易次数: {m['total_trades']} | 胜率: {m['win_rate']:.1%} | 盈亏比: {m['profit_factor']:.2f}",
                f"  累计盈亏: {m['total_pnl']:,.0f}元 | 收益率: {m['total_return']:.2%}",
                f"  平均持仓: {m['avg_hold_days']:.0f}天 | 最大连亏: {m['max_consecutive_loss']}笔",
                f"  数学期望: {m['expectancy']:,.0f}元/笔",
            ]

        # 行为诊断
        bd = report["behavior_diagnosis"]
        if "error" not in bd:
            lines += [
                "",
                "  --- 交易行为诊断 ---",
                f"  日均交易: {bd['daily_avg_trades']}笔 | 平均持仓: {bd['avg_hold_days']}天",
                f"  诊断: {bd['diagnosis']}",
            ]

        # 按标的
        df_stock = report["by_stock"]
        if not df_stock.empty:
            lines += ["", "  --- 标的归因（TOP5盈利/BOTTOM5亏损）---"]
            for _, row in df_stock.head(5).iterrows():
                lines.append(f"    {row['名称']}: {row['累计盈亏']:+,.0f}元 ({row['胜率']}胜率, {row['交易次数']}笔)")
            if len(df_stock) > 5:
                lines.append("    ...")
                for _, row in df_stock.tail(3).iterrows():
                    lines.append(f"    {row['名称']}: {row['累计盈亏']:+,.0f}元 ({row['胜率']}胜率, {row['交易次数']}笔)")

        # 按赛道
        df_sector = report["by_sector"]
        if not df_sector.empty:
            lines += ["", "  --- 赛道归因 ---"]
            for _, row in df_sector.iterrows():
                lines.append(f"    {row['赛道']}: {row['累计盈亏']:+,.0f}元 ({row['交易次数']}笔, 贡献{row['贡献度']})")

        # 按月份
        df_month = report["by_month"]
        if not df_month.empty:
            lines += ["", "  --- 月度收益 ---"]
            for _, row in df_month.iterrows():
                lines.append(f"    {row['月份']}: {row['月度盈亏']:+,.0f}元 ({row['月收益率']}, {row['交易次数']}笔)")

        lines.append("=" * 60)
        return "\n".join(lines)
