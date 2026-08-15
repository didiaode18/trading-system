# -*- coding: utf-8 -*-
"""
P2-3: 行业轮动策略框架
========================
与趋势跟踪策略低相关的行业轮动策略。

核心逻辑:
  1. 计算各行业ETF/指数的相对强度(RPS)
  2. 按动量排名选择Top-N行业
  3. 定期再平衡（周频/双周频）
  4. 行业ETF作为标的，降低个股特异性风险

与趋势策略的相关性:
  - 趋势策略: 个股MA20/MA60多头 + 回踩买点 → 高相关(同质化)
  - 轮动策略: 行业动量排名 + 定期再平衡 → 低相关(跨行业切换)

使用方式:
    from strategy.sector_rotation_strategy import SectorRotationStrategy
    strategy = SectorRotationStrategy()
    signals = strategy.generate_signals(data_dict, holdings)
"""

import os
import sys
import logging
import datetime
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 行业ETF映射表（覆盖主要赛道）
SECTOR_ETF_MAP = {
    "芯片半导体": {"etf": "159599", "name": "芯片指数"},
    "新能源": {"etf": "516160", "name": "新能源ETF"},
    "医药生物": {"etf": "512010", "name": "医药ETF"},
    "消费": {"etf": "159928", "name": "消费ETF"},
    "金融": {"etf": "510230", "name": "金融ETF"},
    "军工": {"etf": "512660", "name": "军工ETF"},
    "光伏": {"etf": "515790", "name": "光伏ETF"},
    "白酒": {"etf": "512690", "name": "酒ETF"},
    "有色金属": {"etf": "512400", "name": "有色ETF"},
    "人工智能": {"etf": "515070", "name": "人工智能"},
}

# 轮动参数
LOOKBACK_PERIOD = 20       # 动量回看周期（交易日）
REBALANCE_PERIOD = 10      # 再平衡周期（交易日）
TOP_N_SECTORS = 3          # 选择Top N个行业
MIN_MOMENTUM = 0.0         # 最低动量阈值(%)，低于此值不选入


class SectorRotationStrategy:
    """行业轮动策略

    基于行业ETF动量排名的轮动策略，与个股趋势策略低相关。
    """

    def __init__(self, lookback: int = LOOKBACK_PERIOD,
                 rebalance_period: int = REBALANCE_PERIOD,
                 top_n: int = TOP_N_SECTORS):
        self.lookback = lookback
        self.rebalance_period = rebalance_period
        self.top_n = top_n

    def compute_sector_momentum(self, data_dict: dict) -> list:
        """计算各行业ETF的动量排名

        参数:
            data_dict: {code: DataFrame} 包含ETF的日线数据

        返回:
            [{"sector": str, "etf": str, "momentum": float, "rps_rank": float,
              "trend": str, "vol_ratio": float}, ...]
            按momentum降序排列
        """
        results = []
        for sector, info in SECTOR_ETF_MAP.items():
            etf_code = info["etf"]
            df = data_dict.get(etf_code)
            if df is None or len(df) < self.lookback + 5:
                continue

            close = df["close"]
            # 动量: lookback期涨跌幅
            momentum = (close.iloc[-1] / close.iloc[-self.lookback] - 1) * 100

            # 趋势: MA5 vs MA20
            ma5 = close.rolling(5).mean().iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1]
            if pd.isna(ma5) or pd.isna(ma20):
                trend = "unknown"
            elif ma5 > ma20:
                trend = "up"
            else:
                trend = "down"

            # 量比: 近5日均量 / 近20日均量
            vol = df["volume"]
            vol_5 = vol.tail(5).mean()
            vol_20 = vol.tail(20).mean()
            vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1.0

            results.append({
                "sector": sector,
                "etf": etf_code,
                "name": info["name"],
                "momentum": round(momentum, 2),
                "trend": trend,
                "vol_ratio": round(vol_ratio, 2),
                "close": round(close.iloc[-1], 4),
            })

        # 按动量降序排列
        results.sort(key=lambda x: x["momentum"], reverse=True)

        # 计算RPS排名百分位
        if results:
            momentums = [r["momentum"] for r in results]
            for r in results:
                rank_pct = sum(1 for m in momentums if m <= r["momentum"]) / len(momentums) * 100
                r["rps_rank"] = round(rank_pct, 1)

        return results

    def generate_signals(self, data_dict: dict, holdings: dict = None) -> dict:
        """生成轮动信号

        返回:
            {
                "date": str,
                "rebalance_due": bool,     # 是否需要再平衡
                "top_sectors": [...],       # Top N行业
                "bottom_sectors": [...],    # 末位行业（应卖出）
                "signals": [...],           # 具体交易信号
                "detail": str,
            }
        """
        holdings = holdings or {}
        momentum_rank = self.compute_sector_momentum(data_dict)

        if not momentum_rank:
            return {"date": "", "rebalance_due": False, "top_sectors": [],
                    "bottom_sectors": [], "signals": [], "detail": "无行业数据"}

        # 判断是否再平衡日（简单实现：每rebalance_period个交易日执行一次）
        today = datetime.date.today()
        day_of_year = today.timetuple().tm_yday
        rebalance_due = (day_of_year % self.rebalance_period) < 2  # 2天窗口

        # Top N行业
        top_sectors = [s for s in momentum_rank[:self.top_n]
                       if s["momentum"] >= MIN_MOMENTUM and s["trend"] == "up"]

        # 末位行业（动量为负且趋势向下）
        bottom_sectors = [s for s in momentum_rank
                          if s["momentum"] < 0 and s["trend"] == "down"]

        # 生成交易信号
        signals = []
        # 买入信号: Top N中未持有的行业ETF
        for s in top_sectors:
            etf_code = s["etf"]
            if etf_code not in holdings:
                signals.append({
                    "code": etf_code,
                    "name": s["name"],
                    "action": "buy",
                    "sector": s["sector"],
                    "momentum": s["momentum"],
                    "reason": f"行业动量Top{self.top_n}({s['momentum']:+.1f}%)，趋势向上",
                })

        # 卖出信号: 持仓中属于末位行业的ETF
        for s in bottom_sectors:
            etf_code = s["etf"]
            if etf_code in holdings:
                signals.append({
                    "code": etf_code,
                    "name": s["name"],
                    "action": "sell",
                    "sector": s["sector"],
                    "momentum": s["momentum"],
                    "reason": f"行业动量末位({s['momentum']:+.1f}%)，趋势向下",
                })

        rank_parts = []
        for s in momentum_rank[:5]:
            rank_parts.append(f"{s['sector']}({s['momentum']:+.1f}%)")
        rank_str = " > ".join(rank_parts)
        top_str = ", ".join(s["sector"] for s in top_sectors) or "无"
        rebal_str = "是" if rebalance_due else "否"
        detail = f"轮动排名: {rank_str} | Top{self.top_n}选入: {top_str} | 再平衡: {rebal_str}"

        return {
            "date": today.isoformat(),
            "rebalance_due": rebalance_due,
            "top_sectors": top_sectors,
            "bottom_sectors": bottom_sectors,
            "signals": signals,
            "momentum_rank": momentum_rank,
            "detail": detail,
        }

    def format_report(self, signals_result: dict) -> str:
        """格式化轮动信号报告"""
        lines = ["## 行业轮动策略信号", ""]
        lines.append(f"**日期**: {signals_result.get('date', '')}")
        lines.append(f"**再平衡**: {'是' if signals_result.get('rebalance_due') else '否'}")
        lines.append("")

        # 动量排名
        rank = signals_result.get("momentum_rank", [])
        if rank:
            lines.append("### 行业动量排名")
            for i, s in enumerate(rank, 1):
                trend_icon = "↑" if s["trend"] == "up" else ("↓" if s["trend"] == "down" else "→")
                lines.append(f"{i}. **{s['sector']}**({s['name']}) "
                           f"动量{s['momentum']:+.1f}% {trend_icon} "
                           f"量比{s['vol_ratio']:.1f}")
            lines.append("")

        # 交易信号
        signals = signals_result.get("signals", [])
        if signals:
            lines.append("### 交易信号")
            for sig in signals:
                icon = "买入" if sig["action"] == "buy" else "卖出"
                lines.append(f"- [{icon}] {sig['name']}({sig['code']}) - {sig['reason']}")
        else:
            lines.append("*本期无交易信号*")

        return "\n".join(lines)
