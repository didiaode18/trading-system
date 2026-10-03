# -*- coding: utf-8 -*-
"""
回测分析中心
=============
回测结果对比 / 策略绩效排行 / 风险指标分析
"""

import os
import sys
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dashboard import data_loader as dl
from dashboard.theme import (
    PLOTLY_LAYOUT, PLOTLY_LAYOUT_TALL, COLOR_UP, COLOR_DOWN, COLOR_ACCENT,
)


def render(date_range=None):
    st.header("📈 回测分析中心")

    # 加载回测结果
    output_dir = dl.OUTPUT_DIR
    backtest_files = []
    if os.path.exists(output_dir):
        backtest_files = [f for f in os.listdir(output_dir)
                         if f.startswith("backtest_") and f.endswith(".json")]

    if not backtest_files:
        st.info("暂无回测结果。运行回测后自动生成。")
        st.caption(f"输出目录: {output_dir}")
        return

    # 文件选择
    selected_file = st.selectbox("选择回测报告", backtest_files)
    if not selected_file:
        return

    # 加载回测数据
    bt_path = os.path.join(output_dir, selected_file)
    bt_data = dl._load_json(bt_path)
    if not bt_data:
        st.error("回测数据加载失败（可能是编码问题）")
        return

    # 判断数据格式
    has_stats = "stats" in bt_data
    has_metrics = "metrics" in bt_data

    if not has_stats and not has_metrics:
        st.warning("回测文件格式不匹配，无法解析")
        st.json(bt_data)
        return

    # ============================================================
    # 1. 核心指标卡片
    # ============================================================
    st.subheader("核心绩效指标")

    if has_stats:
        stats = bt_data["stats"]
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            total = stats.get("total", 0)
            st.metric("总交易次数", total)
        with col2:
            # win_rate 可能是百分比(63.1)或小数(0.631)
            win_rate = stats.get("win_rate", 0)
            if win_rate > 1:
                st.metric("胜率", f"{win_rate:.1f}%")
            else:
                st.metric("胜率", f"{win_rate:.1%}")
        with col3:
            pf = stats.get("profit_factor", 0)
            st.metric("盈亏比", f"{pf:.2f}")
        with col4:
            exp = stats.get("expectancy", 0)
            st.metric("交易期望", f"¥{exp:,.0f}")
        with col5:
            cum = stats.get("cumulative", 0)
            # 统一用万/千/元显示，避免"亿"级误解
            if abs(cum) >= 1e8:
                st.metric("累计盈亏", f"¥{cum/1e8:,.2f}亿")
            elif abs(cum) >= 1e4:
                st.metric("累计盈亏", f"¥{cum/1e4:,.1f}万")
            else:
                st.metric("累计盈亏", f"¥{cum:,.0f}")

        # 详细指标
        with st.expander("查看详细统计"):
            dc1, dc2, dc3, dc4 = st.columns(4)
            dc1.metric("盈利次数", stats.get("win_count", 0))
            dc2.metric("亏损次数", stats.get("loss_count", 0))
            dc3.metric("平均盈利", f"¥{stats.get('avg_win', 0):,.0f}")
            dc4.metric("平均亏损", f"¥{stats.get('avg_loss', 0):,.0f}")

            dc5, dc6, dc7, dc8 = st.columns(4)
            dc5.metric("最大单笔盈利", f"¥{stats.get('max_win', 0):,.0f}")
            dc6.metric("最大单笔亏损", f"¥{stats.get('max_loss', 0):,.0f}")
            dc7.metric("平均持仓天数", f"{stats.get('avg_hold', 0):.1f}")
            dc8.metric("最大连亏", f"{stats.get('max_consec_loss', 0)}笔")

    elif has_metrics:
        metrics = bt_data["metrics"]
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric("总收益率", f"{metrics.get('total_return', 0):.2%}")
        with col2:
            st.metric("年化收益", f"{metrics.get('annual_return', 0):.2%}")
        with col3:
            st.metric("夏普比率", f"{metrics.get('sharpe_ratio', 0):.2f}")
        with col4:
            st.metric("最大回撤", f"{metrics.get('max_drawdown', 0):.2%}")
        with col5:
            st.metric("胜率", f"{metrics.get('win_rate', 0):.1%}")

    # ============================================================
    # 2. 净值曲线（如果有）
    # ============================================================
    equity_curve = bt_data.get("equity_curve", [])
    if equity_curve:
        st.subheader("净值曲线")
        df_equity = pd.DataFrame(equity_curve)

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df_equity.get("date", []),
            y=df_equity.get("nav", []),
            mode="lines", name="策略净值",
            line=dict(color=COLOR_ACCENT, width=2),
        ))
        if "benchmark" in df_equity.columns:
            fig.add_trace(go.Scatter(
                x=df_equity.get("date", []),
                y=df_equity.get("benchmark", []),
                mode="lines", name="基准",
                line=dict(color="#95a5a6", width=1, dash="dash"),
            ))
        fig.update_layout(
            **PLOTLY_LAYOUT_TALL, title="策略净值走势",
            xaxis_title="日期", yaxis_title="净值", hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True)

    # ============================================================
    # 3. 个股表现
    # ============================================================
    stock_results = bt_data.get("stock_results", [])
    if stock_results:
        st.subheader("个股表现")

        # 兼容 dict 和 list 两种格式
        if isinstance(stock_results, dict):
            df_stocks = pd.DataFrame([
                {"code": k, **v} for k, v in stock_results.items()
            ])
        else:
            df_stocks = pd.DataFrame(stock_results)

        # 个股交易次数柱状图
        if "trades" in df_stocks.columns:
            name_col = "name" if "name" in df_stocks.columns else "code"
            df_sorted = df_stocks.sort_values("trades", ascending=False).head(20)
            names = df_sorted[name_col].tolist()
            trades = df_sorted["trades"].tolist()

            fig = go.Figure(go.Bar(
                x=names, y=trades,
                marker_color=COLOR_ACCENT,
                text=[f"{t}次" for t in trades],
                textposition="outside",
            ))
            _layout = {k: v for k, v in PLOTLY_LAYOUT.items()
                       if k not in ("xaxis", "yaxis", "height")}
            fig.update_layout(
                **_layout, height=300,
                title="个股交易次数排行",
                yaxis=dict(title="交易次数", gridcolor="#E8ECF0"),
                xaxis=dict(tickangle=30),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)
        elif "pnl" in df_stocks.columns:
            name_col = "name" if "name" in df_stocks.columns else "code"
            df_sorted = df_stocks.sort_values("pnl", ascending=False).head(20)
            names = df_sorted[name_col].tolist()
            pnls = df_sorted["pnl"].tolist()
            bar_colors = [COLOR_UP if p >= 0 else COLOR_DOWN for p in pnls]

            fig = go.Figure(go.Bar(
                x=names, y=pnls,
                marker_color=bar_colors,
                text=[f"¥{p:+,.0f}" for p in pnls],
                textposition="outside",
            ))
            _layout = {k: v for k, v in PLOTLY_LAYOUT.items()
                       if k not in ("xaxis", "yaxis", "height")}
            fig.update_layout(
                **_layout, height=300,
                title="个股盈亏排行",
                yaxis=dict(title="盈亏 (¥)", gridcolor="#E8ECF0"),
                xaxis=dict(tickangle=30),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)

        # 明细表格
        display_cols = [c for c in ["code", "name", "industry", "trades",
                                     "pnl", "pnl_pct", "win_rate"]
                        if c in df_stocks.columns]
        if display_cols:
            st.dataframe(df_stocks[display_cols].head(20),
                        use_container_width=True, hide_index=True)

    # ============================================================
    # 4. 行业统计
    # ============================================================
    if has_stats:
        industry_stats = stats.get("industry_stats", {})
        if industry_stats:
            st.subheader("行业统计")
            df_ind = pd.DataFrame([
                {"行业": k, **v} for k, v in industry_stats.items()
            ])
            st.dataframe(df_ind, use_container_width=True, hide_index=True)

    # ============================================================
    # 5. 跳转到策略生命周期
    # ============================================================
    st.markdown("---")
    if st.button("🔄 查看策略生命周期", use_container_width=True):
        st.session_state["jump_page"] = "strategy_lifecycle"
        st.rerun()
