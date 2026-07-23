# -*- coding: utf-8 -*-
"""
板块轮动监控系统 (Sector Rotation Monitor)
==========================================
对标同花顺/东方财富付费板块资金流功能

核心功能:
  1. 板块动量排名: 5日/20日涨幅排名，识别资金流入方向
  2. 板块资金流: 板块内个股主力净流入汇总
  3. 轮动信号: 板块从底部启动（连续N日资金流入+涨幅<阈值）
  4. 持仓板块预警: 持仓所在板块资金转出→预警

数据源:
  - 优先: akshare (stock_board_industry_name_em / stock_board_industry_hist_em)
  - 回退: baostock (用持仓个股数据计算板块动量)

使用:
    from strategy.sector_flow import SectorMonitor
    monitor = SectorMonitor()
    result = monitor.analyze(holdings_data)
"""

import pandas as pd
import numpy as np
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 板块配置
SECTOR_CONFIG = {
    "momentum_short": 5,        # 短期动量（5日涨幅）
    "momentum_long": 20,        # 长期动量（20日涨幅）
    "inflow_days": 3,           # 连续流入天数判定
    "startup_max_gain": 0.08,   # 启动信号: 涨幅<8%（还在底部）
    "top_n_sectors": 5,         # 输出TOP N板块
    "outflow_alert_days": 3,    # 连续流出N天触发预警
}

# 申万一级行业分类（持仓标的映射）
STOCK_SECTOR_MAP = {
    "002415": "电子",       # 海康威视
    "600036": "银行",       # 招商银行
    "000858": "食品饮料",   # 五粮液
    "603501": "电子",       # 韦尔股份
    "601012": "电力设备",   # 隆基绿能
    "002185": "电子",       # 华天科技
    "001309": "电子",       # 德明利
    "002558": "传媒",       # 巨人网络
    "588000": "科技ETF",    # 科创50ETF
    "159205": "金融ETF",    # 创业东财
    "688234": "电子",       # 天岳先进
}


class SectorMonitor:
    """板块轮动监控器"""

    def __init__(self, config: dict = None):
        self.cfg = {**SECTOR_CONFIG, **(config or {})}
        self._akshare_available = None

    def analyze(self, holdings_data: Dict[str, pd.DataFrame],
                holdings_info: dict = None) -> dict:
        """
        板块轮动分析

        参数:
            holdings_data: {code: DataFrame} 各标的K线数据
            holdings_info: {code: {name, shares, ...}} 持仓信息

        返回:
            板块分析结果
        """
        # 1. 按板块分组
        sector_stocks = self._group_by_sector(holdings_data)

        # 2. 计算各板块指标
        sector_metrics = {}
        for sector, stocks in sector_stocks.items():
            metrics = self._calc_sector_metrics(sector, stocks)
            if metrics:
                sector_metrics[sector] = metrics

        # 3. 排名
        ranked = self._rank_sectors(sector_metrics)

        # 4. 轮动信号
        signals = self._detect_rotation_signals(sector_metrics)

        # 5. 持仓板块预警
        alerts = self._check_holding_alerts(sector_metrics, holdings_info)

        # 6. 尝试akshare获取全市场板块数据
        market_sectors = self._try_akshare_sectors()

        return {
            "sector_metrics": sector_metrics,
            "ranked": ranked,
            "signals": signals,
            "alerts": alerts,
            "market_sectors": market_sectors,
            "top_sectors": ranked[:self.cfg["top_n_sectors"]],
            "summary": self._generate_summary(ranked, signals, alerts),
        }

    def _group_by_sector(self, holdings_data: Dict[str, pd.DataFrame]) -> Dict[str, List]:
        """按板块分组"""
        groups = {}
        for code, df in holdings_data.items():
            sector = STOCK_SECTOR_MAP.get(code, "其他")
            if sector not in groups:
                groups[sector] = []
            groups[sector].append({"code": code, "df": df})
        return groups

    def _calc_sector_metrics(self, sector: str, stocks: List[dict]) -> Optional[dict]:
        """计算单个板块的指标"""
        if not stocks:
            return None

        # 汇总板块内所有个股的收益率
        returns_5d = []
        returns_20d = []
        main_flows = []
        volumes_change = []

        for stock in stocks:
            df = stock["df"]
            if df is None or len(df) < 20:
                continue

            close = df["close"].values
            # 5日涨幅
            if len(close) >= 5:
                r5 = (close[-1] - close[-5]) / close[-5]
                returns_5d.append(r5)
            # 20日涨幅
            if len(close) >= 20:
                r20 = (close[-1] - close[-20]) / close[-20]
                returns_20d.append(r20)
            # 主力资金流（如果有）
            if "main_flow" in df.columns:
                recent_flow = df["main_flow"].tail(5).sum()
                main_flows.append(recent_flow)
            # 量能变化
            if "volume" in df.columns and len(df) >= 10:
                vol = df["volume"].values
                vol_5 = vol[-5:].mean()
                vol_20 = vol[-20:].mean() if len(vol) >= 20 else vol.mean()
                if vol_20 > 0:
                    volumes_change.append(vol_5 / vol_20 - 1)

        if not returns_5d:
            return None

        avg_r5 = np.mean(returns_5d)
        avg_r20 = np.mean(returns_20d) if returns_20d else 0
        total_flow = sum(main_flows) if main_flows else 0
        avg_vol_change = np.mean(volumes_change) if volumes_change else 0

        # 连续流入天数
        inflow_streak = self._calc_inflow_streak(stocks)

        # 板块状态判定
        status = self._determine_status(avg_r5, avg_r20, inflow_streak, avg_vol_change)

        return {
            "sector": sector,
            "stock_count": len(stocks),
            "stocks": [s["code"] for s in stocks],
            "return_5d": round(avg_r5 * 100, 2),
            "return_20d": round(avg_r20 * 100, 2),
            "main_flow_5d": round(total_flow, 0),
            "volume_change": round(avg_vol_change * 100, 1),
            "inflow_streak": inflow_streak,
            "status": status,
            "momentum_score": self._calc_momentum_score(avg_r5, avg_r20, inflow_streak, avg_vol_change),
        }

    def _calc_inflow_streak(self, stocks: List[dict]) -> int:
        """计算板块连续资金流入天数"""
        if not stocks:
            return 0

        # 取板块内所有个股的main_flow，按日汇总
        min_len = min(len(s["df"]) for s in stocks if s["df"] is not None and "main_flow" in s["df"].columns)
        if min_len == 0:
            return 0

        # 汇总最近N天的板块资金流
        streak = 0
        for day_offset in range(1, min(min_len, 30) + 1):
            day_flow = 0
            for s in stocks:
                df = s["df"]
                if df is not None and "main_flow" in df.columns:
                    day_flow += df["main_flow"].iloc[-day_offset]
            if day_flow > 0:
                streak += 1
            else:
                break

        return streak

    def _determine_status(self, r5: float, r20: float, streak: int, vol_change: float) -> str:
        """判定板块状态"""
        if streak >= self.cfg["inflow_days"] and r5 < self.cfg["startup_max_gain"]:
            return "启动"  # 资金连续流入+涨幅不大=刚启动
        elif r5 > 0.03 and streak >= 2:
            return "加速"  # 涨幅扩大+资金持续流入
        elif r5 > 0 and r20 > 0:
            return "上升"
        elif r5 < -0.03 and streak == 0:
            return "流出"  # 下跌+无资金流入
        elif r5 < 0 and r20 < 0:
            return "下跌"
        else:
            return "震荡"

    def _calc_momentum_score(self, r5: float, r20: float, streak: int, vol_change: float) -> float:
        """
        板块动量评分 (0-100)
        权重: 5日涨幅30% + 20日涨幅20% + 资金连流30% + 量能20%
        """
        score = 50.0

        # 5日涨幅 (±5% → ±15分)
        score += max(-15, min(15, r5 * 300))

        # 20日涨幅 (±10% → ±10分)
        score += max(-10, min(10, r20 * 100))

        # 资金连续流入 (每天+5分，最多+15)
        score += min(15, streak * 5)

        # 量能放大 (±50% → ±10分)
        score += max(-10, min(10, vol_change * 20))

        return max(0, min(100, score))

    def _rank_sectors(self, metrics: Dict[str, dict]) -> List[dict]:
        """按动量评分排名"""
        ranked = sorted(metrics.values(), key=lambda x: x["momentum_score"], reverse=True)
        for i, item in enumerate(ranked):
            item["rank"] = i + 1
        return ranked

    def _detect_rotation_signals(self, metrics: Dict[str, dict]) -> List[dict]:
        """检测轮动信号"""
        signals = []

        for sector, m in metrics.items():
            # 启动信号: 连续流入+涨幅小
            if m["status"] == "启动":
                signals.append({
                    "type": "startup",
                    "sector": sector,
                    "desc": f"{sector}板块启动: 资金连续{m['inflow_streak']}日流入, 5日涨幅仅{m['return_5d']:.1f}%",
                    "action": "关注该板块个股的D点信号",
                })
            # 加速信号
            elif m["status"] == "加速":
                signals.append({
                    "type": "accelerate",
                    "sector": sector,
                    "desc": f"{sector}板块加速: 5日涨{m['return_5d']:.1f}%, 资金持续流入",
                    "action": "已持有可加仓，未持有追高需谨慎",
                })
            # 流出信号
            elif m["status"] == "流出":
                signals.append({
                    "type": "outflow",
                    "sector": sector,
                    "desc": f"{sector}板块资金流出: 5日跌{abs(m['return_5d']):.1f}%, 无资金支撑",
                    "action": "该板块持仓考虑减仓",
                })

        return signals

    def _check_holding_alerts(self, metrics: Dict[str, dict],
                              holdings_info: dict = None) -> List[dict]:
        """持仓板块预警"""
        alerts = []
        if not holdings_info:
            return alerts

        for code, info in holdings_info.items():
            sector = STOCK_SECTOR_MAP.get(code, "其他")
            m = metrics.get(sector)
            if not m:
                continue

            name = info.get("name", code)

            # 板块资金流出预警
            if m["status"] in ("流出", "下跌"):
                alerts.append({
                    "level": "warning",
                    "code": code,
                    "name": name,
                    "sector": sector,
                    "desc": f"{name}所在{sector}板块资金流出(5日{m['return_5d']:+.1f}%)",
                })

            # 板块动量评分过低
            if m["momentum_score"] < 30:
                alerts.append({
                    "level": "danger",
                    "code": code,
                    "name": name,
                    "sector": sector,
                    "desc": f"{name}所在{sector}板块动量极弱({m['momentum_score']:.0f}分)",
                })

        return alerts

    def _try_akshare_sectors(self) -> Optional[List[dict]]:
        """尝试用akshare获取全市场板块数据（网络不可用时返回None）"""
        if self._akshare_available is False:
            return None

        try:
            import akshare as ak
            df = ak.stock_board_industry_name_em()
            if df is not None and not df.empty:
                self._akshare_available = True
                # 提取关键列
                sectors = []
                for _, row in df.head(20).iterrows():
                    sectors.append({
                        "name": row.get("板块名称", ""),
                        "change_pct": row.get("涨跌幅", 0),
                        "turnover": row.get("换手率", 0),
                        "up_count": row.get("上涨家数", 0),
                        "down_count": row.get("下跌家数", 0),
                    })
                return sectors
        except Exception as e:
            logger.debug(f"akshare板块数据不可用: {e}")
            self._akshare_available = False

        return None

    def _generate_summary(self, ranked: List[dict], signals: List[dict],
                          alerts: List[dict]) -> str:
        """生成文字摘要"""
        lines = []

        if ranked:
            top = ranked[0]
            bottom = ranked[-1]
            lines.append(f"最强板块: {top['sector']}(动量{top['momentum_score']:.0f}分, 5日{top['return_5d']:+.1f}%)")
            lines.append(f"最弱板块: {bottom['sector']}(动量{bottom['momentum_score']:.0f}分, 5日{bottom['return_5d']:+.1f}%)")

        startup_signals = [s for s in signals if s["type"] == "startup"]
        if startup_signals:
            names = "、".join([s["sector"] for s in startup_signals])
            lines.append(f"启动信号: {names}")

        if alerts:
            lines.append(f"持仓预警: {len(alerts)}条")

        return " | ".join(lines) if lines else "无显著轮动信号"


# === 便捷函数 ===
def sector_summary(result: dict) -> str:
    """板块分析摘要文本"""
    lines = ["📊 板块轮动分析"]

    ranked = result.get("ranked", [])
    if ranked:
        lines.append("  排名 | 板块 | 5日涨幅 | 20日涨幅 | 资金连流 | 动量分 | 状态")
        lines.append("  " + "─" * 60)
        for m in ranked:
            lines.append(
                f"  {m['rank']:>2} | {m['sector']:<6} | {m['return_5d']:>+6.2f}% | "
                f"{m['return_20d']:>+6.2f}% | {m['inflow_streak']:>2}天 | "
                f"{m['momentum_score']:>5.1f} | {m['status']}"
            )

    signals = result.get("signals", [])
    if signals:
        lines.append("\n  轮动信号:")
        for s in signals:
            icon = {"startup": "🚀", "accelerate": "⚡", "outflow": "⚠️"}.get(s["type"], "•")
            lines.append(f"    {icon} {s['desc']}")
            lines.append(f"       → {s['action']}")

    alerts = result.get("alerts", [])
    if alerts:
        lines.append("\n  持仓预警:")
        for a in alerts:
            icon = "🚨" if a["level"] == "danger" else "⚠️"
            lines.append(f"    {icon} {a['desc']}")

    return "\n".join(lines)
