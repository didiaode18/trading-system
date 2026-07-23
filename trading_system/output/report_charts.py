# -*- coding: utf-8 -*-
"""
报告图表生成器
==============
生成K线图、资金流向图、仓位饼图，输出base64嵌入邮件
"""

import os
import io
import base64
import logging
import datetime
import numpy as np

logger = logging.getLogger(__name__)

# 图表输出目录
CHART_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "charts")


def _ensure_matplotlib():
    """确保matplotlib可用且使用非交互后端"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # 中文字体
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial"]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def generate_kline_chart(df, code: str, name: str, ma_lines=None) -> str:
    """
    生成K线+均线图，返回base64字符串
    
    参数:
        df: DataFrame，需含 date/open/high/low/close/volume 列
        code: 股票代码
        name: 股票名称
        ma_lines: 均线列表，默认 [5, 10, 20, 60]
    
    返回: base64编码的PNG图片字符串，失败返回空字符串
    """
    try:
        plt = _ensure_matplotlib()
        import matplotlib.dates as mdates
        from matplotlib.patches import Rectangle

        if ma_lines is None:
            ma_lines = [5, 10, 20, 60]

        # 取最近60个交易日
        plot_df = df.tail(60).copy().reset_index(drop=True)
        if len(plot_df) < 10:
            return ""

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6),
                                        gridspec_kw={"height_ratios": [3, 1]},
                                        sharex=True)
        fig.patch.set_facecolor("#1a1a2e")
        ax1.set_facecolor("#1a1a2e")
        ax2.set_facecolor("#1a1a2e")

        dates = range(len(plot_df))
        opens = plot_df["open"].values
        closes = plot_df["close"].values
        highs = plot_df["high"].values
        lows = plot_df["low"].values
        volumes = plot_df["volume"].values

        # K线
        for i in range(len(plot_df)):
            color = "#ef5350" if closes[i] >= opens[i] else "#26a69a"
            # 实体
            body_bottom = min(opens[i], closes[i])
            body_height = abs(closes[i] - opens[i])
            if body_height < 0.001:
                body_height = 0.001
            rect = Rectangle((i - 0.3, body_bottom), 0.6, body_height,
                            facecolor=color, edgecolor=color, linewidth=0.5)
            ax1.add_patch(rect)
            # 影线
            ax1.plot([i, i], [lows[i], body_bottom], color=color, linewidth=0.8)
            ax1.plot([i, i], [body_bottom + body_height, highs[i]], color=color, linewidth=0.8)

        # 均线
        ma_colors = {"5": "#ffeb3b", "10": "#ff9800", "20": "#2196f3", "60": "#e91e63"}
        for ma in ma_lines:
            if len(plot_df) >= ma:
                ma_vals = plot_df["close"].rolling(ma).mean().values
                valid = ~np.isnan(ma_vals)
                if valid.any():
                    ax1.plot(dates[valid], ma_vals[valid],
                            color=ma_colors.get(str(ma), "#ffffff"),
                            linewidth=1.2, label=f"MA{ma}", alpha=0.9)

        ax1.set_title(f"{name}({code}) 日K线", color="white", fontsize=14, fontweight="bold")
        ax1.legend(loc="upper left", fontsize=9, facecolor="#1a1a2e",
                  labelcolor="white", edgecolor="#333")
        ax1.set_xlim(-1, len(plot_df))
        ax1.tick_params(colors="white", labelsize=9)
        ax1.spines["top"].set_visible(False)
        ax1.spines["right"].set_visible(False)
        ax1.spines["bottom"].set_color("#333")
        ax1.spines["left"].set_color("#333")
        ax1.grid(True, alpha=0.15, color="white")

        # 成交量
        vol_colors = ["#ef5350" if closes[i] >= opens[i] else "#26a69a" for i in range(len(plot_df))]
        ax2.bar(dates, volumes, color=vol_colors, alpha=0.7, width=0.6)
        ax2.set_ylabel("成交量", color="white", fontsize=9)
        ax2.tick_params(colors="white", labelsize=8)
        ax2.spines["top"].set_visible(False)
        ax2.spines["right"].set_visible(False)
        ax2.spines["bottom"].set_color("#333")
        ax2.spines["left"].set_color("#333")
        ax2.grid(True, alpha=0.15, color="white")

        # X轴日期标签
        tick_step = max(1, len(plot_df) // 8)
        tick_positions = list(range(0, len(plot_df), tick_step))
        if "date" in plot_df.columns:
            tick_labels = [str(plot_df["date"].iloc[i])[:10] for i in tick_positions]
        else:
            tick_labels = [str(i) for i in tick_positions]
        ax2.set_xticks(tick_positions)
        ax2.set_xticklabels(tick_labels, rotation=30, fontsize=8, color="white")

        plt.tight_layout()

        # 转base64
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight",
                   facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        return f"data:image/png;base64,{b64}"

    except Exception as e:
        logger.warning(f"K线图生成失败({code}): {e}")
        return ""


def generate_fund_flow_chart(results: list) -> str:
    """
    生成资金流向柱状图（主力净流入/流出）
    
    参数:
        results: 分析结果列表
    
    返回: base64 PNG
    """
    try:
        plt = _ensure_matplotlib()

        names = []
        scores = []
        for r in results:
            fd = r.get("fund_data", {})
            names.append(r.get("name", r.get("code", "")))
            scores.append(fd.get("score", 50) - 50)  # 转为-50~+50

        if not names:
            return ""

        fig, ax = plt.subplots(figsize=(10, 4))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")

        colors = ["#ef5350" if s >= 0 else "#26a69a" for s in scores]
        bars = ax.barh(names, scores, color=colors, alpha=0.85, height=0.6)

        ax.axvline(x=0, color="white", linewidth=0.8, alpha=0.5)
        ax.set_title("资金评分分布（>0看多 / <0看空）", color="white", fontsize=12, fontweight="bold")
        ax.set_xlabel("资金评分偏移", color="white", fontsize=9)
        ax.tick_params(colors="white", labelsize=10)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["bottom"].set_color("#333")
        ax.spines["left"].set_color("#333")
        ax.grid(True, axis="x", alpha=0.15, color="white")

        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight",
                   facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}"

    except Exception as e:
        logger.warning(f"资金流向图生成失败: {e}")
        return ""


def generate_position_pie(holdings: dict, results: list) -> str:
    """
    生成仓位分布饼图
    
    参数:
        holdings: 持仓字典
        results: 分析结果列表
    
    返回: base64 PNG
    """
    try:
        plt = _ensure_matplotlib()

        labels = []
        sizes = []
        for r in results:
            code = r.get("code", "")
            info = holdings.get(code, {})
            shares = info.get("shares", 0)
            close = r.get("close", 0)
            if shares and close > 0:
                labels.append(r.get("name", code))
                sizes.append(shares * close)

        if not labels or sum(sizes) == 0:
            return ""

        # 添加现金
        total_capital = getattr(config, "TOTAL_CAPITAL", 750000)
        cash = total_capital - sum(sizes)
        if cash > 0:
            labels.append("现金")
            sizes.append(cash)

        fig, ax = plt.subplots(figsize=(6, 6))
        fig.patch.set_facecolor("#1a1a2e")

        colors = ["#ef5350", "#ff9800", "#ffeb3b", "#4caf50", "#2196f3",
                  "#9c27b0", "#e91e63", "#00bcd4", "#8bc34a", "#607d8b", "#795548"]
        explode = [0.03] * len(labels)

        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, autopct="%1.1f%%",
            colors=colors[:len(labels)], explode=explode,
            textprops={"color": "white", "fontsize": 10},
            pctdistance=0.8
        )
        for t in autotexts:
            t.set_fontsize(9)
            t.set_color("white")

        ax.set_title("仓位分布", color="white", fontsize=13, fontweight="bold")

        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight",
                   facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}"

    except Exception as e:
        logger.warning(f"仓位饼图生成失败: {e}")
        return ""


def generate_sector_bar(sector_result: dict) -> str:
    """
    生成板块动量柱状图
    """
    try:
        plt = _ensure_matplotlib()

        ranked = sector_result.get("ranked", [])
        if not ranked:
            return ""

        names = [m["sector"] for m in ranked]
        scores = [m["momentum_score"] for m in ranked]

        fig, ax = plt.subplots(figsize=(8, 4))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")

        colors = []
        for s in scores:
            if s >= 70:
                colors.append("#ef5350")
            elif s >= 50:
                colors.append("#ff9800")
            elif s >= 30:
                colors.append("#ffeb3b")
            else:
                colors.append("#26a69a")

        bars = ax.barh(names[::-1], scores[::-1], color=colors[::-1], alpha=0.85, height=0.6)
        ax.set_title("板块动量评分", color="white", fontsize=12, fontweight="bold")
        ax.set_xlim(0, 100)
        ax.tick_params(colors="white", labelsize=10)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["bottom"].set_color("#333")
        ax.spines["left"].set_color("#333")
        ax.grid(True, axis="x", alpha=0.15, color="white")

        # 数值标签
        for bar, score in zip(bars, scores[::-1]):
            ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2,
                   f"{score:.0f}", va="center", color="white", fontsize=9)

        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight",
                   facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}"

    except Exception as e:
        logger.warning(f"板块图生成失败: {e}")
        return ""


# 需要在模块级别导入config（用于position_pie）
try:
    import config
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config
