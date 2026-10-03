# -*- coding: utf-8 -*-
"""
可视化平台统一主题 V2.0（浅色金融专业主题）
============================================
Plotly图表主题 + Streamlit自定义样式
设计风格: 参考东方财富/同花顺专业交易终端浅色方案
"""

import plotly.graph_objects as go
import plotly.express as px


# ============================================================
# Plotly 金融主题（浅色背景 + 红涨绿跌 A股惯例）
# ============================================================

PLOTLY_LAYOUT = dict(
    template="plotly_white",
    paper_bgcolor="#FFFFFF",
    plot_bgcolor="#FAFBFC",
    font=dict(color="#333333", size=12, family="Microsoft YaHei, PingFang SC, sans-serif"),
    margin=dict(l=40, r=20, t=40, b=40),
    height=350,
    xaxis=dict(
        gridcolor="#E8ECF0",
        zerolinecolor="#E8ECF0",
        linecolor="#D0D5DD",
    ),
    yaxis=dict(
        gridcolor="#E8ECF0",
        zerolinecolor="#E8ECF0",
        linecolor="#D0D5DD",
    ),
    legend=dict(
        font=dict(color="#555555", size=11),
        bgcolor="rgba(255,255,255,0.85)",
        bordercolor="#E0E4E8",
        borderwidth=1,
    ),
)

PLOTLY_LAYOUT_TALL = {**PLOTLY_LAYOUT, "height": 500}

# 颜色常量（A股: 红涨绿跌）
COLOR_UP = "#FF4D4F"         # 红=涨
COLOR_DOWN = "#52C41A"       # 绿=跌
COLOR_NEUTRAL = "#8C8C8C"    # 灰=平
COLOR_ACCENT = "#1890FF"     # 蓝=强调（Ant Design蓝）
COLOR_WARN = "#FAAD14"       # 橙=警告
COLOR_DANGER = "#FF4D4F"     # 红=危险
COLOR_SUCCESS = "#52C41A"    # 绿=成功

# 因子分类颜色映射（适配浅色背景，饱和度适中）
CATEGORY_COLORS = {
    "technical": "#1890FF",
    "volume": "#722ED1",
    "momentum": "#FA8C16",
    "quant": "#13C2C2",
    "smart_money": "#FF4D4F",
    "reversal": "#52C41A",
    "vol_dist": "#FADB14",
    "volatility": "#8C8C8C",
    "tail_volume": "#EB2F96",
    "quality": "#08979C",
    "value": "#FA8C16",
    "growth": "#389E0D",
    "liquidity": "#5D7092",
    "dividend": "#597EF7",
}

# 风控关卡状态颜色
GATE_COLORS = {
    "pass": "#52C41A",
    "warn": "#FAAD14",
    "block": "#FF4D4F",
    "inactive": "#BFBFBF",
}


def make_plotly_fig(**kwargs) -> go.Figure:
    """创建带统一主题的Plotly图表"""
    layout = {**PLOTLY_LAYOUT, **kwargs}
    return go.Figure(layout=layout)


def pnl_color(val: float) -> str:
    """盈亏值返回CSS颜色"""
    if val > 0:
        return COLOR_UP
    elif val < 0:
        return COLOR_DOWN
    return COLOR_NEUTRAL


def status_emoji(status: str) -> str:
    """状态文本转emoji"""
    mapping = {
        "pass": "🟢", "green": "🟢", "normal": "🟢", "live": "🟢",
        "warn": "🟡", "yellow": "🟡", "degrade": "🟡", "paper": "🔵",
        "block": "🔴", "red": "🔴", "breaker": "🔴", "rejected": "🔴", "critical": "🔴",
        "backtest": "🟡", "research": "⚪", "retired": "⚫",
    }
    return mapping.get(status.lower(), "⚪")


# ============================================================
# Streamlit 自定义CSS（浅色金融专业主题）
# ============================================================

CUSTOM_CSS = """
<style>
/* ========================================
   全局基础
   ======================================== */
body {
    background-color: #F0F2F5;
    color: #333333;
    font-family: "Microsoft YaHei", "PingFang SC", -apple-system, sans-serif;
}

/* 主内容区背景 */
.stApp {
    background-color: #F0F2F5;
}

/* ========================================
   指标卡 (Metric Cards)
   ======================================== */
[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #E8ECF0;
    border-radius: 10px;
    padding: 14px 18px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
    transition: box-shadow 0.2s ease;
}
[data-testid="stMetric"]:hover {
    box-shadow: 0 3px 8px rgba(0, 0, 0, 0.1);
}
[data-testid="stMetricLabel"] {
    color: #666666 !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    letter-spacing: 0.02em;
}
[data-testid="stMetricValue"] {
    color: #1A1A1A !important;
    font-weight: 700 !important;
    font-size: 1.45rem !important;
    letter-spacing: -0.01em;
}
/* 指标卡 delta 着色 */
[data-testid="stMetricValue"] [data-testid="stMarkdownContainer"] {
    font-size: 0.85rem !important;
}

/* ========================================
   侧边栏导航
   ======================================== */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #FFFFFF 0%, #F7F9FC 100%);
    border-right: 1px solid #E8ECF0;
}
[data-testid="stSidebar"] .stRadio label {
    color: #333333 !important;
    font-size: 0.95rem !important;
    font-weight: 500;
    padding: 4px 0;
}
[data-testid="stSidebar"] .stRadio label:hover {
    color: #1890FF !important;
}
/* 侧边栏标题 */
[data-testid="stSidebar"] h1 {
    color: #1A1A1A !important;
    font-size: 1.3rem !important;
    border-bottom: none !important;
}
[data-testid="stSidebar"] .stCaption {
    color: #999999 !important;
}

/* ========================================
   标题层级
   ======================================== */
h1 {
    color: #1A1A1A !important;
    border-bottom: 3px solid #1890FF;
    padding-bottom: 10px;
    margin-bottom: 20px;
    font-weight: 700;
    font-size: 1.75rem;
}
h2 {
    color: #1A1A1A !important;
    font-weight: 600;
    font-size: 1.35rem;
    margin-top: 8px;
    margin-bottom: 12px;
}
h3 {
    color: #555555 !important;
    font-weight: 600;
    font-size: 1.1rem;
}

/* ========================================
   正文文本
   ======================================== */
[data-testid="stMarkdown"] {
    color: #333333 !important;
}
[data-testid="stCaption"] {
    color: #888888 !important;
    font-size: 0.85rem !important;
}

/* ========================================
   表格样式
   ======================================== */
[data-testid="stDataFrame"] {
    border-radius: 8px;
    border: 1px solid #E0E4E8;
    overflow: hidden;
}
/* 表头加深 */
[data-testid="stDataFrame"] th {
    background-color: #F5F7FA !important;
    color: #333333 !important;
    font-weight: 600 !important;
    border-bottom: 2px solid #D0D5DD !important;
}

/* ========================================
   信息/警告/错误框
   ======================================== */
[data-testid="stAlert"] {
    border-radius: 8px;
    border: 1px solid #E8ECF0;
}

/* ========================================
   标签页
   ======================================== */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    background: #F0F2F5;
    border-radius: 8px;
    padding: 4px;
}
.stTabs [data-baseweb="tab"] {
    padding: 8px 18px;
    border-radius: 6px;
    background: transparent;
    color: #666666;
    font-weight: 500;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] {
    background: #FFFFFF;
    color: #1890FF;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}

/* ========================================
   展开器
   ======================================== */
.streamlit-expanderHeader {
    color: #555555 !important;
    font-weight: 500 !important;
}
.streamlit-expanderHeader:hover {
    color: #1890FF !important;
}

/* ========================================
   按钮样式
   ======================================== */
.stButton > button {
    border-radius: 6px;
    border: 1px solid #D0D5DD;
    background: #FFFFFF;
    color: #333333;
    font-weight: 500;
    transition: all 0.2s;
}
.stButton > button:hover {
    border-color: #1890FF;
    color: #1890FF;
    box-shadow: 0 2px 4px rgba(24,144,255,0.15);
}

/* ========================================
   分割线
   ======================================== */
hr {
    border-color: #E0E4E8 !important;
    margin: 16px 0;
}

/* ========================================
   进度条 / Spinner
   ======================================== */
.stProgress > div > div {
    background-color: #1890FF;
}
</style>
"""
