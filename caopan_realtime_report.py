# -*- coding: utf-8 -*-
"""
操盘密码实时图表报告 V1.0
==========================
仿东方财富操盘密码界面风格，生成深色主题K线图表邮件

功能:
  - 5只持仓标的独立图表（K线+控盘生命线+DK买卖点+主力/散户监控）
  - 盘中每15分钟自动刷新（--loop模式）
  - 图表以PNG图片内嵌HTML邮件发送

图表风格（对标东方财富付费版）:
  - 深色背景 #1a1a2e
  - K线: 红涨(#e53935) 绿跌(#4caf50)
  - 控盘生命线: 黄色EMA13虚线 + 紫色EMA34虚线
  - DK买卖点: D=红色向上三角, K=绿色向下三角
  - 机构监控: 黄色=主力净买入, 蓝色=主力净卖出
  - 散户监控: 绿色=散户净买入, 紫色=散户净卖出

运行:
  python caopan_realtime_report.py          # 单次生成并发送
  python caopan_realtime_report.py --loop   # 盘中每15分钟循环
"""

import sys
import os
import io
import base64
import datetime
import logging
import argparse
import time

# Windows编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADING_SYSTEM_DIR = os.path.join(BASE_DIR, "trading_system")
sys.path.insert(0, TRADING_SYSTEM_DIR)
sys.path.insert(0, BASE_DIR)

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # 无GUI后端
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

# 配置中文字体
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False  # 负号正常显示

import config
from strategy.caopan_signal import CaopanEngine
from data.realtime import fetch_realtime_batch
from notify.email_notify import send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# 持仓标的配置
# ============================================================
HOLDINGS = {
    "588000": "科创50",
    "002415": "海康威视",
    "603501": "豪威集团",
    "002409": "雅克科技",
    "002185": "华天科技",
    "600036": "招商银行",
    "159205": "创业东财",
    "600276": "恒瑞医药",
    "603993": "洛阳钼业",
}

# 东方财富配色方案（对标截图精确色值）
COLORS = {
    "bg": "#0d0d1a",         # 主背景（近纯黑，对标东财深色底）
    "panel": "#141428",       # 面板背景
    "grid": "#1e1e3a",        # 网格线（极淡）
    "axis": "#3a3a5c",        # 坐标轴
    "text": "#999999",        # 普通文字
    "text_bright": "#ffffff", # 高亮文字
    "up": "#ff4444",          # 阳线/涨 (红) - 东财红
    "up_border": "#ff4444",   # 阳线边框
    "down": "#00cc66",        # 阴线/跌 (绿) - 东财绿
    "down_border": "#00cc66", # 阴线边框
    "ll_fast": "#ff9900",     # EMA13 橙黄色（东财原版色）
    "ll_slow": "#cc44ff",     # EMA34 亮紫色（东财原版色）
    "main_buy": "#ff9900",    # 主力净买入 橙黄
    "main_sell": "#4488ff",   # 主力净卖出 蓝
    "retail_buy": "#00cc66",  # 散户净买入 绿
    "retail_sell": "#cc44ff", # 散户净卖出 紫
    "d_point": "#ff6600",     # D点标记（橙色）
    "k_point": "#00dd77",     # K点标记（绿色）
    "badge_bg": "#ff6600",    # DK点徽章背景
    "zero_line": "#555577",   # 零轴线
}


# ============================================================
# 一、数据获取层
# ============================================================

def fetch_kline_data(code: str, days: int = 150) -> pd.DataFrame:
    """baostock获取日K线数据"""
    try:
        import baostock as bs
        lg = bs.login()
        prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"
        bs_code = f"{prefix}.{code}"
        end = datetime.date.today().strftime("%Y-%m-%d")
        start = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume,amount",
            start_date=start, end_date=end, frequency="d", adjustflag="2"
        )
        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.tail(days).reset_index(drop=True)
    except Exception as e:
        logger.error(f"获取{code}数据失败: {e}")
        return None


def merge_realtime(df: pd.DataFrame, quote: dict) -> pd.DataFrame:
    """盘中用实时价更新当日K线"""
    if not quote or quote.get("price", 0) <= 0:
        return df
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    price = quote["price"]
    high = quote.get("high", price)
    low = quote.get("low", price)
    volume = quote.get("volume", 0) * 100  # 手→股

    if len(df) > 0 and df["date"].iloc[-1] == today_str:
        # 更新当日数据
        df.iloc[-1, df.columns.get_loc("close")] = price
        df.iloc[-1, df.columns.get_loc("high")] = max(high, df["high"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")] = min(low, df["low"].iloc[-1])
        if volume > 0:
            df.iloc[-1, df.columns.get_loc("volume")] = volume
    else:
        # 追加当日数据
        prev_close = df["close"].iloc[-1] if len(df) > 0 else price
        new_row = {
            "date": today_str,
            "open": quote.get("open", prev_close),
            "high": high,
            "low": low,
            "close": price,
            "volume": volume,
            "amount": quote.get("amount", 0) * 10000,
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    return df


# ============================================================
# 二、图表生成层 (matplotlib 仿东方财富操盘密码 V2.0)
# ============================================================

def generate_stock_chart(result: dict, quote: dict, show_days: int = 60) -> str:
    """
    生成单只标的的操盘密码图表PNG，返回base64字符串
    视觉对标东方财富操盘密码付费版界面
    """
    df = result.get("df_analyzed")
    if df is None or df.empty:
        return ""

    code = result.get("code", "")
    name = result.get("name", code)
    close = result.get("close", 0)
    trend_level = result.get("trend_level", 3)
    trend_desc = result.get("trend_desc", "")
    dk_signal = result.get("dk_signal") or "无"
    dk_strength = result.get("dk_strength", 0)
    change_pct = quote.get("change_pct", 0) if quote else 0
    ll1_val = result.get("ll_fast", 0)
    ll2_val = result.get("ll_slow", 0)
    ll1_dir = result.get("ll_fast_direction", "")
    ll2_dir = result.get("ll_slow_direction", "")

    df_show = df.tail(show_days).copy().reset_index(drop=True)
    n = len(df_show)

    # 创建图表 (14x9英寸, 110dpi = 1540x990 高清)
    fig = plt.figure(figsize=(14, 9), dpi=110, facecolor=COLORS["bg"])

    # 布局: 标题区(8%) + K线区(52%) + 机构监控(17%) + 散户监控(17%)
    ax_k = fig.add_axes([0.07, 0.37, 0.90, 0.50], facecolor=COLORS["bg"])
    ax_main = fig.add_axes([0.07, 0.195, 0.90, 0.145], facecolor=COLORS["bg"])
    ax_retail = fig.add_axes([0.07, 0.03, 0.90, 0.145], facecolor=COLORS["bg"])

    x = np.arange(n)
    bar_width = 0.6  # K线实体宽度

    # ---- K线图（阳线红色空心 / 阴线绿色实心）----
    for i in range(n):
        row = df_show.iloc[i]
        o, c, h, l = row["open"], row["close"], row["high"], row["low"]
        is_up = c >= o
        color = COLORS["up"] if is_up else COLORS["down"]
        # 影线（细线）
        ax_k.plot([i, i], [l, h], color=color, linewidth=0.9, solid_capstyle='round')
        # 实体
        body_bottom = min(o, c)
        body_height = abs(c - o)
        if body_height < (df_show["high"].max() - df_show["low"].min()) * 0.002:
            body_height = (df_show["high"].max() - df_show["low"].min()) * 0.002
        if is_up:
            # 阳线: 红色空心（边框红，填充背景色）
            rect = mpatches.FancyBboxPatch(
                (i - bar_width/2, body_bottom), bar_width, body_height,
                boxstyle="square,pad=0", facecolor=COLORS["bg"],
                edgecolor=color, linewidth=1.2
            )
        else:
            # 阴线: 绿色实心
            rect = mpatches.FancyBboxPatch(
                (i - bar_width/2, body_bottom), bar_width, body_height,
                boxstyle="square,pad=0", facecolor=color,
                edgecolor=color, linewidth=0.8
            )
        ax_k.add_patch(rect)

    # ---- 控盘生命线（平滑曲线，对标东财原版）----
    if "ll_fast" in df_show.columns:
        ll_fast = df_show["ll_fast"].values
        valid_mask = ~np.isnan(ll_fast)
        ax_k.plot(x[valid_mask], ll_fast[valid_mask], color=COLORS["ll_fast"],
                  linewidth=1.8, linestyle=(0, (6, 3)), alpha=0.95,
                  label=f'LL1:{ll1_val:.2f}{ll1_dir}', zorder=3)
    if "ll_slow" in df_show.columns:
        ll_slow = df_show["ll_slow"].values
        valid_mask = ~np.isnan(ll_slow)
        ax_k.plot(x[valid_mask], ll_slow[valid_mask], color=COLORS["ll_slow"],
                  linewidth=1.8, linestyle=(0, (6, 3)), alpha=0.95,
                  label=f'LL2:{ll2_val:.2f}{ll2_dir}', zorder=3)

    # ---- DK买卖点标记（仿东财: 字母D/K + 彩色圆点）----
    if "dk_signal" in df_show.columns:
        for i in range(n):
            row = df_show.iloc[i]
            dk = row.get("dk_signal")
            strength = row.get("dk_strength", 0)
            filtered = row.get("dk_filtered", False)
            if dk == "D" and strength >= 50 and not filtered:
                y_pos = row["low"] * 0.98
                ax_k.scatter(i, y_pos, marker='^', s=160,
                           color=COLORS["d_point"], zorder=6, edgecolors='white', linewidths=0.8)
                ax_k.annotate(f"D", (i, y_pos), fontsize=9, color='white',
                            ha='center', va='center', fontweight='bold', zorder=7)
                ax_k.annotate(f"{int(strength)}", (i, row["low"] * 0.965),
                            fontsize=7, color=COLORS["d_point"], ha='center', fontweight='bold')
            elif dk == "K" and strength >= 50 and not filtered:
                y_pos = row["high"] * 1.02
                ax_k.scatter(i, y_pos, marker='v', s=160,
                           color=COLORS["k_point"], zorder=6, edgecolors='white', linewidths=0.8)
                ax_k.annotate(f"K", (i, y_pos), fontsize=9, color='white',
                            ha='center', va='center', fontweight='bold', zorder=7)
                ax_k.annotate(f"{int(strength)}", (i, row["high"] * 1.035),
                            fontsize=7, color=COLORS["k_point"], ha='center', fontweight='bold')

    # ---- K线区域样式设置 ----
    price_range = df_show["high"].max() - df_show["low"].min()
    ax_k.set_xlim(-1, n)
    ax_k.set_ylim(df_show["low"].min() - price_range * 0.05, df_show["high"].max() + price_range * 0.08)
    ax_k.grid(True, color=COLORS["grid"], linewidth=0.4, alpha=0.6)
    ax_k.tick_params(colors=COLORS["text"], labelsize=8, length=3)
    for spine in ax_k.spines.values():
        spine.set_color(COLORS["axis"])
        spine.set_linewidth(0.5)
    ax_k.set_xticks([])
    # Y轴价格刻度（右侧）
    ax_k.yaxis.set_label_position("right")
    ax_k.yaxis.tick_right()
    ax_k.tick_params(axis='y', colors=COLORS["text"], labelsize=8)

    # 标题栏（仿东财顶部信息栏）
    price_color = COLORS["up"] if change_pct >= 0 else COLORS["down"]
    title_text = f"{name} ({code})   "
    ax_k.set_title(title_text, color=COLORS["text_bright"], fontsize=13, fontweight='bold',
                   loc='left', pad=12, fontfamily='Microsoft YaHei')
    # 副标题: 价格+涨跌+趋势+DK
    dk_badge = f"DK={dk_signal}({dk_strength})" if dk_signal != "无" else ""
    subtitle = f"  {close:.2f}  {change_pct:+.2f}%   {trend_desc}   {dk_badge}"
    fig.text(0.07, 0.885, subtitle, color=price_color, fontsize=10,
             fontfamily='Microsoft YaHei', fontweight='bold')

    # DK点徽章（仿东财右上角橙色标签）
    if dk_signal != "无":
        badge_color = COLORS["d_point"] if dk_signal == "D" else COLORS["k_point"]
        fig.patches.append(mpatches.FancyBboxPatch(
            (0.88, 0.875), 0.06, 0.025, boxstyle="round,pad=0.005",
            facecolor=badge_color, edgecolor='none',
            transform=fig.transFigure, zorder=10
        ))
        fig.text(0.91, 0.887, f"DK{dk_signal}", color='white', fontsize=9,
                ha='center', va='center', fontweight='bold', fontfamily='Microsoft YaHei')

    # X轴日期标签
    tick_step = max(1, n // 8)
    tick_positions = list(range(0, n, tick_step))
    tick_labels = [df_show["date"].iloc[i][-5:] if i < n else "" for i in tick_positions]

    # ---- 机构监控（主力资金流）----
    if "main_flow" in df_show.columns:
        main_flow = df_show["main_flow"].fillna(0).values / 10000  # 万元
        bar_colors = [COLORS["main_buy"] if v > 0 else COLORS["main_sell"] for v in main_flow]
        ax_main.bar(x, main_flow, color=bar_colors, width=0.7, alpha=0.9)
        latest_jg = main_flow[-1] if n > 0 else 0
        ax_main.set_title(f"机构监控  JG: {latest_jg:.1f}", color=COLORS["ll_fast"],
                         fontsize=9, loc='left', fontfamily='Microsoft YaHei', pad=3)
    ax_main.axhline(y=0, color=COLORS["zero_line"], linewidth=0.8, linestyle='-')
    ax_main.set_xlim(-1, n)
    ax_main.grid(True, axis='y', color=COLORS["grid"], linewidth=0.3, alpha=0.4)
    ax_main.tick_params(colors=COLORS["text"], labelsize=7, length=2)
    for spine in ax_main.spines.values():
        spine.set_color(COLORS["axis"])
        spine.set_linewidth(0.5)
    ax_main.set_xticks([])

    # ---- 散户监控（散户资金流）----
    if "retail_flow" in df_show.columns:
        retail_flow = df_show["retail_flow"].fillna(0).values / 10000
        bar_colors = [COLORS["retail_buy"] if v > 0 else COLORS["retail_sell"] for v in retail_flow]
        ax_retail.bar(x, retail_flow, color=bar_colors, width=0.7, alpha=0.9)
        latest_sh = retail_flow[-1] if n > 0 else 0
        ax_retail.set_title(f"散户监控  SH: {latest_sh:.1f}", color=COLORS["retail_buy"],
                           fontsize=9, loc='left', fontfamily='Microsoft YaHei', pad=3)
    ax_retail.axhline(y=0, color=COLORS["zero_line"], linewidth=0.8, linestyle='-')
    ax_retail.set_xlim(-1, n)
    ax_retail.grid(True, axis='y', color=COLORS["grid"], linewidth=0.3, alpha=0.4)
    ax_retail.tick_params(colors=COLORS["text"], labelsize=7, length=2)
    for spine in ax_retail.spines.values():
        spine.set_color(COLORS["axis"])
        spine.set_linewidth(0.5)
    ax_retail.set_xticks(tick_positions)
    ax_retail.set_xticklabels(tick_labels, color=COLORS["text"], fontsize=7)

    # 图例（简洁，右上角）
    legend_elements = [
        Line2D([0], [0], color=COLORS["ll_fast"], linestyle=(0, (6, 3)), linewidth=1.8, label=f'LL1(EMA13) {ll1_val:.2f}{ll1_dir}'),
        Line2D([0], [0], color=COLORS["ll_slow"], linestyle=(0, (6, 3)), linewidth=1.8, label=f'LL2(EMA34) {ll2_val:.2f}{ll2_dir}'),
        Line2D([0], [0], marker='^', color='w', markerfacecolor=COLORS["d_point"], markersize=9, label='D点(买)'),
        Line2D([0], [0], marker='v', color='w', markerfacecolor=COLORS["k_point"], markersize=9, label='K点(卖)'),
    ]
    ax_k.legend(handles=legend_elements, loc='upper left', fontsize=7.5,
                facecolor=COLORS["panel"], edgecolor=COLORS["axis"],
                labelcolor=COLORS["text"], framealpha=0.85)

    # 水印（仿东财半透明）
    fig.text(0.5, 0.5, "操盘密码", fontsize=48, color='white', alpha=0.025,
             ha='center', va='center', fontfamily='Microsoft YaHei', rotation=0)

    # 输出为base64
    buf = io.BytesIO()
    fig.savefig(buf, format='png', facecolor=COLORS["bg"], bbox_inches='tight', pad_inches=0.08)
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode('utf-8')
    return b64


# ============================================================
# 三、邮件组装层
# ============================================================

def build_report_email(results: list, charts_b64: dict, quotes: dict) -> str:
    """组装HTML邮件（图表base64内嵌 + 图表阅读说明）"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    is_trading = _is_trading_time()
    status_text = "盘中实时" if is_trading else "盘后收盘"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:15px;background:#0d1117;font-family:'Microsoft YaHei',Arial,sans-serif">
<div style="max-width:1100px;margin:0 auto">
    <div style="background:linear-gradient(135deg,#0d0d1a,#141428);padding:18px 25px;border-radius:12px 12px 0 0;border-bottom:2px solid #ff9900">
        <h1 style="margin:0;font-size:20px;color:#fff">操盘密码 实时图表报告</h1>
        <div style="font-size:12px;color:#aaa;margin-top:5px">{now} | {status_text} | 5只持仓标的 | 仿东方财富操盘密码风格</div>
    </div>
    <!-- 图表阅读说明模块 -->
    <div style="background:#141428;padding:14px 20px;border-bottom:1px solid #2a2a4a">
        <div style="font-size:13px;font-weight:bold;color:#ff9900;margin-bottom:8px">📖 图表阅读说明（新手必看）</div>
        <table style="width:100%;font-size:11px;color:#ccc;border-collapse:collapse">
            <tr>
                <td style="padding:3px 8px"><span style="color:#ff9900;font-weight:bold">━ ━</span> 橙色虚线 = EMA13短期生命线</td>
                <td style="padding:3px 8px"><span style="color:#cc44ff;font-weight:bold">━ ━</span> 紫色虚线 = EMA34中期生命线</td>
            </tr>
            <tr>
                <td style="padding:3px 8px"><span style="color:#ff6600;font-weight:bold">▲ D</span> = 买入信号（多方共振确认）</td>
                <td style="padding:3px 8px"><span style="color:#00dd77;font-weight:bold">▼ K</span> = 卖出信号（空方共振确认）</td>
            </tr>
            <tr>
                <td style="padding:3px 8px"><span style="color:#ff4444">□</span> 红色空心K线 = 当日上涨（阳线）</td>
                <td style="padding:3px 8px"><span style="color:#00cc66">■</span> 绿色实心K线 = 当日下跌（阴线）</td>
            </tr>
            <tr>
                <td style="padding:3px 8px"><span style="color:#ff9900">█</span> 机构监控橙柱 = 主力净买入</td>
                <td style="padding:3px 8px"><span style="color:#4488ff">█</span> 机构监控蓝柱 = 主力净卖出</td>
            </tr>
            <tr>
                <td style="padding:3px 8px"><span style="color:#00cc66">█</span> 散户监控绿柱 = 散户净买入</td>
                <td style="padding:3px 8px"><span style="color:#cc44ff">█</span> 散户监控紫柱 = 散户净卖出</td>
            </tr>
        </table>
        <div style="margin-top:8px;padding:8px 12px;background:#1a1a30;border-radius:6px;border-left:3px solid #ff9900;font-size:11px;color:#ddd;line-height:1.8">
            <b style="color:#ff9900">核心口诀：</b><br>
            ① 两线向上 + D点出现 = 持股/加仓参考（主力与趋势共振）<br>
            ② K点 + 趋势降级 = 减仓/离场参考（信号与趋势双确认）<br>
            ③ 机构蓝柱连续放大 + 散户绿柱放大 = 主力出货警报（反向指标）
        </div>
    </div>
    <div style="background:#0d0d1a;padding:15px;border-radius:0 0 12px 12px">
"""

    for r in results:
        code = r.get("code", "")
        name = r.get("name", code)
        b64 = charts_b64.get(code, "")
        quote = quotes.get(code, {})

        # 信号摘要
        dk = r.get("dk_signal") or "无"
        dk_strength = r.get("dk_strength", 0)
        trend_desc = r.get("trend_desc", "")
        trend_level = r.get("trend_level", 3)
        action = r.get("action_suggestion", {})
        main_streak = r.get("main_flow_streak", 0)
        close = r.get("close", 0)
        change_pct = quote.get("change_pct", 0)

        # 颜色
        trend_color = {5: "#ff4444", 4: "#ff7043", 3: "#ff9900", 2: "#66bb6a", 1: "#00cc66"}.get(trend_level, "#888")
        price_color = "#ff4444" if change_pct >= 0 else "#00cc66"
        dk_color = "#ff6600" if dk == "D" else "#00dd77" if dk == "K" else "#888"

        html += f"""
    <div style="margin-bottom:20px;border:1px solid #2a2a4a;border-radius:8px;overflow:hidden">
        <div style="background:#141428;padding:10px 15px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">
            <span style="font-size:15px;font-weight:bold;color:#fff">{name} ({code})</span>
            <span style="color:{price_color};font-weight:bold">{close:.3f} ({change_pct:+.2f}%)</span>
            <span style="color:{trend_color};font-size:12px;padding:2px 8px;border:1px solid {trend_color};border-radius:4px">{trend_desc}</span>
            <span style="color:{dk_color};font-size:12px;font-weight:bold">DK={dk}({dk_strength}分)</span>
            <span style="color:#aaa;font-size:11px">主力连流{main_streak}天</span>
        </div>
"""
        if b64:
            html += f'        <img src="data:image/png;base64,{b64}" style="width:100%;display:block" />\n'

        html += f"""
        <div style="background:#1a1a30;padding:8px 15px;font-size:12px;color:#ddd">
            操作建议: <b style="color:#ff9900">{action.get('desc', '观望')}</b>
            <span style="color:#999;margin-left:10px">{action.get('detail', '')}</span>
        </div>
    </div>
"""

    html += f"""
        <div style="text-align:center;color:#666;font-size:11px;padding:10px;border-top:1px solid #333;margin-top:10px">
            操盘密码自适应趋势策略引擎 V2.0 | 数据来源: baostock+腾讯行情 | {now}<br>
            仅供参考，不构成投资建议 | 股市有风险，投资需谨慎
        </div>
    </div>
</div>
</body></html>"""
    return html


# ============================================================
# 四、主流程
# ============================================================

def run_once():
    """单次生成并发送"""
    print("\n" + "=" * 55)
    print("  操盘密码实时图表报告 V1.0")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    codes = list(HOLDINGS.keys())

    # 1. 获取实时行情
    print("\n  [1/4] 获取实时行情...")
    quotes = fetch_realtime_batch(codes)
    print(f"        获取到 {len(quotes)}/{len(codes)} 只实时数据")

    # 2. 获取K线数据 + 运行分析
    print("  [2/4] 获取K线数据并运行操盘密码分析...")
    engine = CaopanEngine()
    results = []
    for code in codes:
        name = HOLDINGS[code]
        df = fetch_kline_data(code, days=150)
        if df is None or len(df) < 60:
            print(f"        {code} {name}: 数据不足，跳过")
            continue
        # 融合实时数据
        quote = quotes.get(code, {})
        df = merge_realtime(df, quote)
        # 运行分析
        result = engine.analyze(df, code=code, name=name)
        if "error" not in result:
            results.append(result)
            dk = result.get("dk_signal") or "无"
            print(f"        {code} {name}: {result.get('trend_desc','')} | DK={dk}({result.get('dk_strength',0)}分)")
        else:
            print(f"        {code} {name}: 分析失败 - {result['error']}")

    if not results:
        print("  无有效分析结果，退出")
        return

    # 3. 生成图表
    print("  [3/4] 生成操盘密码图表...")
    charts_b64 = {}
    for r in results:
        code = r.get("code", "")
        name = r.get("name", code)
        quote = quotes.get(code, {})
        b64 = generate_stock_chart(r, quote, show_days=60)
        if b64:
            charts_b64[code] = b64
            print(f"        {name}: 图表生成成功 ({len(b64)//1024}KB)")
        else:
            print(f"        {name}: 图表生成失败")

    # 4. 发送邮件
    print("  [4/4] 组装并发送邮件...")
    html = build_report_email(results, charts_b64, quotes)
    is_trading = _is_trading_time()
    status = "盘中" if is_trading else "盘后"
    now_str = datetime.datetime.now().strftime("%H:%M")
    subject = f"[操盘密码] 实时图表报告({status}) | {now_str} | {len(results)}只标的"

    success = send_email(subject, html)
    if success:
        print(f"\n  邮件发送成功 -> 563646039@qq.com")
    else:
        print(f"\n  邮件发送失败!")

    # 同时保存本地HTML
    output_dir = os.path.join(TRADING_SYSTEM_DIR, "output", "reports")
    os.makedirs(output_dir, exist_ok=True)
    local_path = os.path.join(output_dir, f"caopan_chart_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.html")
    with open(local_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  本地存档: {local_path}")
    print("=" * 55)


def run_loop(interval: int = 15):
    """盘中每N分钟循环"""
    print("=" * 55)
    print("  操盘密码实时图表报告 - 循环模式")
    print(f"  刷新间隔: {interval}分钟")
    print(f"  交易时间: 09:30-11:30, 13:00-15:00")
    print("=" * 55)

    while True:
        if _is_trading_time():
            run_once()
        else:
            now = datetime.datetime.now().strftime("%H:%M")
            print(f"\n  [{now}] 非交易时间，等待中...")
        time.sleep(interval * 60)


def _is_trading_time() -> bool:
    """判断当前是否为交易时间"""
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.strftime("%H:%M")
    return ("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00")


# ============================================================
# 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="操盘密码实时图表报告")
    parser.add_argument("--loop", action="store_true", help="盘中每15分钟循环刷新")
    parser.add_argument("--interval", type=int, default=15, help="刷新间隔(分钟)")
    args = parser.parse_args()

    if args.loop:
        run_loop(args.interval)
    else:
        run_once()


if __name__ == "__main__":
    main()
