# -*- coding: utf-8 -*-
"""因子图表组件"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from ..theme import PLOTLY_LAYOUT, PLOTLY_LAYOUT_TALL, CATEGORY_COLORS, COLOR_UP, COLOR_DOWN


def render_ic_scatter(factor_stats: dict) -> go.Figure:
    """
    IC/IR散点图（气泡大小=IC胜率，颜色=分类）

    参数:
        factor_stats: {name: {ic_mean, ir, ic_win_rate, category, ...}}
    """
    if not factor_stats:
        fig = go.Figure(layout={**PLOTLY_LAYOUT_TALL, "title": "暂无因子IC数据"})
        return fig

    names = list(factor_stats.keys())
    ic_means = [factor_stats[n]["ic_mean"] for n in names]
    irs = [factor_stats[n]["ir"] for n in names]
    win_rates = [factor_stats[n].get("ic_win_rate", 50) for n in names]
    categories = [factor_stats[n].get("category", "unknown") for n in names]

    fig = go.Figure()

    unique_cats = sorted(set(categories))
    for cat in unique_cats:
        mask = [c == cat for c in categories]
        cat_names = [n for n, m in zip(names, mask) if m]
        cat_ic = [ic for ic, m in zip(ic_means, mask) if m]
        cat_ir = [ir for ir, m in zip(irs, mask) if m]
        cat_wr = [wr for wr, m in zip(win_rates, mask) if m]

        fig.add_trace(go.Scatter(
            x=cat_ic, y=cat_ir,
            mode="markers+text",
            name=cat,
            marker=dict(
                size=[max(8, wr / 5) for wr in cat_wr],
                color=CATEGORY_COLORS.get(cat, "#888"),
                opacity=0.7,
                line=dict(width=1, color="white"),
            ),
            text=cat_names,
            textposition="top center",
            textfont=dict(size=9),
            hovertemplate=(
                "<b>%{text}</b><br>"
                "IC: %{x:.4f} | IR: %{y:.2f}"
                f"<extra>{cat}</extra>"
            ),
        ))

    fig.add_hline(y=0.3, line_dash="dash", line_color="green", opacity=0.3,
                  annotation_text="IR=0.3")
    fig.add_hline(y=-0.3, line_dash="dash", line_color="red", opacity=0.3)
    fig.add_vline(x=0, line_dash="solid", line_color="gray", opacity=0.2)

    fig.update_layout(
        **{k: v for k, v in PLOTLY_LAYOUT_TALL.items() if k != "legend"},
        title="因子IC/IR散点图",
        xaxis_title="IC均值",
        yaxis_title="IR (信息比率)",
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
    )
    return fig


def render_ic_bar(factor_name: str, ic_records: list) -> go.Figure:
    """IC时序柱状图"""
    if not ic_records:
        fig = go.Figure(layout={**PLOTLY_LAYOUT, "title": f"{factor_name}: 暂无IC数据"})
        return fig

    dates = [r["date"] if isinstance(r, dict) else "" for r in ic_records]
    ics = [r["ic"] if isinstance(r, dict) else r for r in ic_records]
    colors = [COLOR_UP if v >= 0 else COLOR_DOWN for v in ics]

    fig = go.Figure()
    fig.add_trace(go.Bar(x=dates, y=ics, marker_color=colors, opacity=0.7, name="IC"))

    if len(ics) >= 5:
        ma5 = pd.Series(ics).rolling(5, min_periods=1).mean().tolist()
        fig.add_trace(go.Scatter(
            x=dates, y=ma5, mode="lines",
            line=dict(color="#f39c12", width=2), name="IC MA5"))

    fig.add_hline(y=0.02, line_dash="dash", line_color="green", opacity=0.4)
    fig.add_hline(y=-0.02, line_dash="dash", line_color="red", opacity=0.4)

    fig.update_layout(
        **PLOTLY_LAYOUT,
        title=f"{factor_name} - IC时序",
        xaxis_title="日期", yaxis_title="IC值",
    )
    return fig


def render_ic_heatmap(correlation_df: pd.DataFrame) -> go.Figure:
    """因子相关性热力图"""
    if correlation_df.empty:
        fig = go.Figure(layout={**PLOTLY_LAYOUT, "title": "暂无相关性数据"})
        return fig

    fig = go.Figure(data=go.Heatmap(
        z=correlation_df.values,
        x=correlation_df.columns.tolist(),
        y=correlation_df.index.tolist(),
        colorscale="RdBu_r", zmid=0,
        text=np.round(correlation_df.values, 2),
        texttemplate="%{text}", textfont=dict(size=8),
        hovertemplate="X: %{x}<br>Y: %{y}<br>r: %{z:.3f}<extra></extra>",
    ))
    fig.update_layout(
        **PLOTLY_LAYOUT_TALL,
        title="因子相关性热力图",
        xaxis=dict(tickangle=45, tickfont=dict(size=9)),
        yaxis=dict(tickfont=dict(size=9)),
    )
    return fig
