# -*- coding: utf-8 -*-
"""KPI指标卡组件"""
import streamlit as st


def render_kpi_row(metrics: list):
    """
    渲染一行KPI指标卡

    参数:
        metrics: [(label, value, delta), ...] 最多6个
    """
    cols = st.columns(min(len(metrics), 6))
    for i, (label, value, delta) in enumerate(metrics):
        if i < len(cols):
            cols[i].metric(label, value, delta)


def render_pnl_metric(label: str, value: float, fmt: str = "+,.0f",
                      suffix: str = "¥"):
    """渲染单个盈亏指标卡（自动着色）"""
    delta_str = f"{suffix}{value:{fmt}}" if suffix else f"{value:{fmt}}"
    st.metric(label, f"{suffix}{value:{fmt}}", delta_str)
