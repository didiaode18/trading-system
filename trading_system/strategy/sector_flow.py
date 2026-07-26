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

import os
import pandas as pd
import numpy as np
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# pyecharts 可选依赖
try:
    from pyecharts import options as opts
    from pyecharts.charts import TreeMap, Bar
    HAS_PYECHARTS = True
except ImportError:
    HAS_PYECHARTS = False

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


   # ============================================================
    # 热力图 / 火苗图可视化（新增方法）
    # ============================================================

    def _collect_sector_flow_data(self, data: dict = None) -> List[dict]:
        """收集板块资金流向数据，供热力图/火苗图使用

        优先使用传入 data，否则尝试 akshare 获取全市场板块，
        最后回退到已有 analyze 结果。

        返回: [{name, value, change_pct, net_inflow}, ...]
        """
        if data and isinstance(data, list) and len(data) > 0:
            return data

        # 尝试 akshare 获取全市场板块
        market_sectors = self._try_akshare_sectors()
        if market_sectors:
            result = []
            for s in market_sectors:
                result.append({
                    "name": s.get("name", ""),
                    "value": max(s.get("turnover", 1), 1),  # 换手率作为面积
                    "change_pct": float(s.get("change_pct", 0)),
                    "net_inflow": 0,  # akshare 板块概览无净流入，用涨跌幅代替方向
                })
            if result:
                return result

        # 回退：返回空列表
        return []

    def _collect_stock_flame_data(self) -> List[dict]:
        """收集个股资金流数据，供火苗图使用

        调用 CapitalFlowAnalyzer 获取持仓池个股四级资金流，
        不重复造轮子。

        返回: [{name, value, change_pct, net_inflow}, ...]
        """
        try:
            from strategy.capital_flow import CapitalFlowAnalyzer
            import config
            cfa = CapitalFlowAnalyzer()
            codes = list(getattr(config, "STOCK_POOL", {}).keys())[:20]
            if not codes:
                # 回退到 STOCK_SECTOR_MAP
                codes = list(STOCK_SECTOR_MAP.keys())

            items = []
            for code in codes:
                flow = cfa.get_stock_main_flow(code)
                if not flow.get("success"):
                    continue
                name = STOCK_SECTOR_MAP.get(code, code)
                net_5d = flow.get("5d_main_net", 0)  # 万元
                # 用绝对值做面积，符号做颜色
                items.append({
                    "name": f"{name}({code})",
                    "value": max(abs(net_5d), 100),  # 面积
                    "change_pct": 0,
                    "net_inflow": net_5d,  # 正=流入 负=流出
                })
            return items
        except Exception as e:
            logger.debug(f"[板块热力图] 个股资金流获取失败: {e}")
            return []

    # ---------- 颜色工具 ----------

    @staticmethod
    def _change_to_color_rgb(change_pct: float) -> str:
        """A股配色：红涨绿跌，颜色深浅表示幅度"""
        if change_pct >= 3:
            return "#CC0000"
        elif change_pct >= 1:
            return "#FF3333"
        elif change_pct >= 0:
            return "#FF9999"
        elif change_pct >= -1:
            return "#99FF99"
        elif change_pct >= -3:
            return "#33CC33"
        else:
            return "#006600"

    @staticmethod
    def _flow_to_color_rgb(net_inflow: float) -> str:
        """资金流向配色：红=流入，绿=流出，深浅表示力度"""
        if net_inflow >= 5000:  # >5000万
            return "#CC0000"
        elif net_inflow >= 1000:
            return "#FF3333"
        elif net_inflow >= 0:
            return "#FF9999"
        elif net_inflow >= -1000:
            return "#99FF99"
        elif net_inflow >= -5000:
            return "#33CC33"
        else:
            return "#006600"

    @staticmethod
    def _format_amount(val: float) -> str:
        """金额格式化（万元→亿元自动转换）"""
        abs_val = abs(val)
        if abs_val >= 10000:
            return f"{val / 10000:+.1f}亿"
        return f"{val:+.0f}万"

    # ---------- 1. render_heatmap 板块资金热力图 ----------

    def render_heatmap(self, data: dict = None, output_path: str = None) -> str:
        """生成板块资金流向热力图（pyecharts TreeMap）

        色块大小 = 换手率或成交额
        色块颜色 = 涨跌幅方向（红涨绿跌，A股配色习惯）
        色块标签 = 板块名称 + 涨跌幅

        Args:
            data: 板块资金流数据列表，如为None则自动获取最新数据
            output_path: HTML输出路径，默认为 output/sector_heatmap.html

        Returns:
            HTML文件路径
        """
        items = self._collect_sector_flow_data(data)
        if not items:
            logger.warning("[板块热力图] 无板块数据，无法生成热力图")
            return ""

        if output_path is None:
            output_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"
            )
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, "sector_heatmap.html")

        if not HAS_PYECHARTS:
            return self._render_heatmap_text(items, output_path)

        try:
            tree_data = []
            for item in items:
                change = item.get("change_pct", 0)
                tree_data.append({
                    "name": item["name"],
                    "value": max(item.get("value", 1), 1),
                    "itemStyle": {"color": self._change_to_color_rgb(change)},
                    "label": {
                        "show": True,
                        "formatter": (
                            f"{item['name']}\n"
                            f"{'🔥' if change >= 1 else '❄️' if change <= -1 else ''}"
                            f"{change:+.2f}%"
                        ),
                    },
                })

            chart = (
                TreeMap()
                .add(
                    series_name="板块资金流向",
                    data=tree_data,
                    leaf_depth=1,
                    levels=[
                        opts.TreeMapLevelsOpts(
                            item_style_opts=opts.ItemStyleOpts(
                                border_color="#555", border_width=1, gap_width=1
                            )
                        )
                    ],
                )
                .set_global_opts(
                    title_opts=opts.TitleOpts(title="板块资金流向热力图"),
                    legend_opts=opts.LegendOpts(is_show=False),
                )
            )
            chart.render(output_path)
            logger.info(f"[板块热力图] 已生成: {output_path}")
            return output_path
        except Exception as e:
            logger.warning(f"[板块热力图] pyecharts渲染失败，降级文本输出: {e}")
            return self._render_heatmap_text(items, output_path)

    def _render_heatmap_text(self, items: List[dict], output_path: str) -> str:
        """降级方案：生成纯文本热力表格"""
        items_sorted = sorted(items, key=lambda x: x.get("change_pct", 0), reverse=True)
        lines = [
            "板块资金流向热力图（文本版）",
            "=" * 50,
            f"{'板块':<10} {'涨跌幅':>8} {'热度条':<20}",
            "-" * 50,
        ]
        for item in items_sorted:
            change = item.get("change_pct", 0)
            bar_len = int(min(abs(change) * 3, 20))
            if change >= 0:
                bar = "🔴" * bar_len + "⚪" * (20 - bar_len)
            else:
                bar = "🟢" * bar_len + "⚪" * (20 - bar_len)
            lines.append(f"{item['name']:<10} {change:>+7.2f}% {bar}")

        text = "\n".join(lines)
        # 保存为 .txt
        txt_path = output_path.replace(".html", ".txt")
        try:
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
            logger.info(f"[板块热力图] 文本版已生成: {txt_path}")
        except Exception:
            pass
        return txt_path

    # ---------- 2. render_treemap_html 可嵌入邮件HTML ----------

    def render_treemap_html(self, data: dict = None) -> str:
        """生成可嵌入邮件的HTML字符串（不保存文件）

        使用 pyecharts render_embed() 获取精简HTML，
        不可用时降级为HTML表格。

        Returns:
            HTML字符串，可直接嵌入邮件正文
        """
        items = self._collect_sector_flow_data(data)
        if not items:
            return "<p>暂无板块资金流向数据</p>"

        if not HAS_PYECHARTS:
            return self._render_treemap_html_fallback(items)

        try:
            tree_data = []
            for item in items:
                change = item.get("change_pct", 0)
                tree_data.append({
                    "name": item["name"],
                    "value": max(item.get("value", 1), 1),
                    "itemStyle": {"color": self._change_to_color_rgb(change)},
                    "label": {
                        "show": True,
                        "formatter": (
                            f"{item['name']}\n"
                            f"{'🔥' if change >= 1 else '❄️' if change <= -1 else ''}"
                            f"{change:+.2f}%"
                        ),
                    },
                })

            chart = (
                TreeMap(init_opts=opts.InitOpts(width="800px", height="500px"))
                .add(
                    series_name="板块资金流向",
                    data=tree_data,
                    leaf_depth=1,
                    levels=[
                        opts.TreeMapLevelsOpts(
                            item_style_opts=opts.ItemStyleOpts(
                                border_color="#555", border_width=1, gap_width=1
                            )
                        )
                    ],
                )
                .set_global_opts(
                    title_opts=opts.TitleOpts(title="板块资金流向热力图"),
                    legend_opts=opts.LegendOpts(is_show=False),
                )
            )
            return chart.render_embed()
        except Exception as e:
            logger.warning(f"[板块热力图] render_embed失败，降级表格: {e}")
            return self._render_treemap_html_fallback(items)

    @staticmethod
    def _render_treemap_html_fallback(items: List[dict]) -> str:
        """降级方案：生成HTML表格代替TreeMap"""
        items_sorted = sorted(items, key=lambda x: x.get("change_pct", 0), reverse=True)
        rows = []
        for item in items_sorted:
            change = item.get("change_pct", 0)
            color = "#CC0000" if change >= 0 else "#006600"
            rows.append(
                f"<tr><td>{item['name']}</td>"
                f"<td style='color:{color};font-weight:bold'>{change:+.2f}%</td></tr>"
            )
        return (
            "<div style='font-family:Arial,sans-serif;padding:10px'>"
            "<h3>板块资金流向</h3>"
            "<table style='border-collapse:collapse;width:100%'>"
            "<tr style='background:#f0f0f0'><th style='padding:6px;border:1px solid #ddd'>板块</th>"
            "<th style='padding:6px;border:1px solid #ddd'>涨跌幅</th></tr>"
            + "".join(rows)
            + "</table></div>"
        )

    # ---------- 3. get_sector_rotation_signal 板块轮动信号 ----------

    def get_sector_rotation_signal(self) -> dict:
        """板块轮动信号

        分析板块资金流向变化，识别资金从哪些板块流出、流入哪些板块。

        Returns:
            {
                inflow_sectors: [{name, amount, days}],
                outflow_sectors: [{name, amount, days}],
                rotation_direction: str  # "科技→消费" 等
            }
        """
        result = {
            "inflow_sectors": [],
            "outflow_sectors": [],
            "rotation_direction": "无明显轮动",
        }

        # 尝试从 akshare 获取板块数据
        market_sectors = self._try_akshare_sectors()
        if market_sectors:
            inflow = []
            outflow = []
            for s in market_sectors:
                change = float(s.get("change_pct", 0))
                name = s.get("name", "")
                if change > 1.0:
                    inflow.append({"name": name, "amount": change, "days": 1})
                elif change < -1.0:
                    outflow.append({"name": name, "amount": change, "days": 1})

            inflow.sort(key=lambda x: x["amount"], reverse=True)
            outflow.sort(key=lambda x: x["amount"])

            result["inflow_sectors"] = inflow[:5]
            result["outflow_sectors"] = outflow[:5]

            if inflow and outflow:
                top_in = inflow[0]["name"]
                top_out = outflow[0]["name"]
                result["rotation_direction"] = f"{top_out}→{top_in}"

            return result

        # 回退：使用已有 analyze 结果（需外部先调用 analyze）
        # 没有数据时返回默认空结果
        return result

    # ---------- 4. render_flame_map 个股资金火苗图 ----------

    def render_flame_map(self, data: dict = None, output_path: str = None) -> str:
        """生成个股资金火苗图（pyecharts Bar 柱状图）

        柱状图横轴 = 股票名称，柱子高度 = 5日净流入金额
        红色 = 流入，绿色 = 流出
        🔥 = 大单持续流入（>=1000万），❄️ = 持续流出（<=-1000万）

        Args:
            data: 个股资金流数据列表，如为None则自动获取
            output_path: HTML输出路径

        Returns:
            HTML文件路径
        """
        items = self._collect_stock_flame_data() if data is None else (
            data if isinstance(data, list) else []
        )
        if not items:
            logger.warning("[资金火苗图] 无个股资金流数据，无法生成火苗图")
            return ""

        if output_path is None:
            output_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"
            )
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, "stock_flame_map.html")

        if not HAS_PYECHARTS:
            return self._render_flame_map_text(items, output_path)

        try:
            # 按净流入金额降序排列
            items_sorted = sorted(items, key=lambda x: x.get("net_inflow", 0), reverse=True)
            names = [it["name"] for it in items_sorted]
            values = [it.get("net_inflow", 0) for it in items_sorted]

            chart = (
                Bar(init_opts=opts.InitOpts(width="900px", height="500px"))
                .add_xaxis(names)
                .add_yaxis(
                    "5日净流入(万)",
                    values,
                    itemstyle_opts=opts.ItemStyleOpts(
                        color=opts.utils.JsCode(
                            "function(params){"
                            "  var v=params.value;"
                            "  if(v>=5000) return '#CC0000';"
                            "  if(v>=1000) return '#FF3333';"
                            "  if(v>=0) return '#FF9999';"
                            "  if(v>=-1000) return '#99FF99';"
                            "  if(v>=-5000) return '#33CC33';"
                            "  return '#006600';"
                            "}"
                        )
                    ),
                    label_opts=opts.LabelOpts(
                        position="top",
                        formatter=opts.utils.JsCode(
                            "function(params){"
                            "  var v=params.value;"
                            "  var icon=v>=1000?'🔥':v<=-1000?'❄️':'';"
                            "  var abs=Math.abs(v);"
                            "  var s=abs>=10000?(v/10000).toFixed(1)+'亿':v.toFixed(0)+'万';"
                            "  return icon+s;"
                            "}"
                        ),
                    ),
                )
                .set_global_opts(
                    title_opts=opts.TitleOpts(title="个股资金火苗图"),
                    xaxis_opts=opts.AxisOpts(
                        axislabel_opts=opts.LabelOpts(rotate=30, font_size=10),
                    ),
                    yaxis_opts=opts.AxisOpts(name="净流入(万)"),
                    legend_opts=opts.LegendOpts(is_show=False),
                    tooltip_opts=opts.TooltipOpts(trigger="axis"),
                )
            )
            chart.render(output_path)
            logger.info(f"[资金火苗图] 已生成: {output_path}")
            return output_path
        except Exception as e:
            logger.warning(f"[资金火苗图] pyecharts渲染失败，降级文本输出: {e}")
            return self._render_flame_map_text(items, output_path)

    def _render_flame_map_text(self, items: List[dict], output_path: str) -> str:
        """降级方案：生成纯文本火苗表格"""
        items_sorted = sorted(items, key=lambda x: x.get("net_inflow", 0), reverse=True)
        lines = [
            "个股资金火苗图（文本版）",
            "=" * 55,
            f"{'个股':<16} {'5日净流入':>10} {'力度条':<20}",
            "-" * 55,
        ]
        for item in items_sorted:
            net = item.get("net_inflow", 0)
            bar_len = int(min(abs(net) / 500, 20))
            if net >= 0:
                bar = "🔴" * bar_len + "⚪" * (20 - bar_len)
            else:
                bar = "🟢" * bar_len + "⚪" * (20 - bar_len)
            lines.append(f"{item['name']:<16} {self._format_amount(net):>10} {bar}")

        text = "\n".join(lines)
        txt_path = output_path.replace(".html", ".txt")
        try:
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
            logger.info(f"[资金火苗图] 文本版已生成: {txt_path}")
        except Exception:
            pass
        return txt_path

    # ---------- 5. render_flame_map_html 可嵌入邮件HTML ----------

    def render_flame_map_html(self, data: dict = None) -> str:
        """生成可嵌入邮件的个股资金火苗图HTML字符串（不保存文件）

        使用 pyecharts Bar render_embed() 获取精简HTML，
        不可用时降级为HTML表格。

        Returns:
            HTML字符串，可直接嵌入邮件正文
        """
        items = self._collect_stock_flame_data() if data is None else (
            data if isinstance(data, list) else []
        )
        if not items:
            return "<p>暂无个股资金流数据</p>"

        if not HAS_PYECHARTS:
            return self._render_flame_html_fallback(items)

        try:
            items_sorted = sorted(items, key=lambda x: x.get("net_inflow", 0), reverse=True)
            names = [it["name"] for it in items_sorted]
            values = [it.get("net_inflow", 0) for it in items_sorted]

            chart = (
                Bar(init_opts=opts.InitOpts(width="900px", height="500px"))
                .add_xaxis(names)
                .add_yaxis(
                    "5日净流入(万)",
                    values,
                    itemstyle_opts=opts.ItemStyleOpts(
                        color=opts.utils.JsCode(
                            "function(params){"
                            "  var v=params.value;"
                            "  if(v>=5000) return '#CC0000';"
                            "  if(v>=1000) return '#FF3333';"
                            "  if(v>=0) return '#FF9999';"
                            "  if(v>=-1000) return '#99FF99';"
                            "  if(v>=-5000) return '#33CC33';"
                            "  return '#006600';"
                            "}"
                        )
                    ),
                    label_opts=opts.LabelOpts(
                        position="top",
                        formatter=opts.utils.JsCode(
                            "function(params){"
                            "  var v=params.value;"
                            "  var icon=v>=1000?'🔥':v<=-1000?'❄️':'';"
                            "  var abs=Math.abs(v);"
                            "  var s=abs>=10000?(v/10000).toFixed(1)+'亿':v.toFixed(0)+'万';"
                            "  return icon+s;"
                            "}"
                        ),
                    ),
                )
                .set_global_opts(
                    title_opts=opts.TitleOpts(title="个股资金火苗图"),
                    xaxis_opts=opts.AxisOpts(
                        axislabel_opts=opts.LabelOpts(rotate=30, font_size=10),
                    ),
                    yaxis_opts=opts.AxisOpts(name="净流入(万)"),
                    legend_opts=opts.LegendOpts(is_show=False),
                    tooltip_opts=opts.TooltipOpts(trigger="axis"),
                )
            )
            return chart.render_embed()
        except Exception as e:
            logger.warning(f"[资金火苗图] render_embed失败，降级表格: {e}")
            return self._render_flame_html_fallback(items)

    @staticmethod
    def _render_flame_html_fallback(items: List[dict]) -> str:
        """降级方案：生成HTML表格代替火苗图"""
        items_sorted = sorted(items, key=lambda x: x.get("net_inflow", 0), reverse=True)
        rows = []
        for item in items_sorted:
            net = item.get("net_inflow", 0)
            color = "#CC0000" if net >= 0 else "#006600"
            icon = "🔥" if net >= 1000 else "❄️" if net <= -1000 else ""
            abs_val = abs(net)
            amount_str = f"{net / 10000:+.1f}亿" if abs_val >= 10000 else f"{net:+.0f}万"
            rows.append(
                f"<tr><td>{item['name']}</td>"
                f"<td style='color:{color};font-weight:bold'>{icon}{amount_str}</td></tr>"
            )
        return (
            "<div style='font-family:Arial,sans-serif;padding:10px'>"
            "<h3>个股资金火苗图</h3>"
            "<table style='border-collapse:collapse;width:100%'>"
            "<tr style='background:#f0f0f0'><th style='padding:6px;border:1px solid #ddd'>个股</th>"
            "<th style='padding:6px;border:1px solid #ddd'>5日净流入</th></tr>"
            + "".join(rows)
            + "</table></div>"
        )


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
