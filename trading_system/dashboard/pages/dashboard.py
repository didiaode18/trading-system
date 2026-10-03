# -*- coding: utf-8 -*-
"""
驾驶舱总览页 V8.0
==================
Sprint 1 升级:
- 账户核心指标 + 市场情绪速览 + 策略运行状态灯
- 美股隔夜赛道涨幅
- 持仓盈亏分布 + 信号卡片 + 风控快览
"""

import os
import sys
import datetime
import logging
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dashboard import data_loader as dl
from dashboard.theme import (
    PLOTLY_LAYOUT, PLOTLY_LAYOUT_TALL, COLOR_UP, COLOR_DOWN,
    COLOR_ACCENT, COLOR_WARN, status_emoji,
)


def render(date_range=None):
    st.header("🏠 驾驶舱总览")

    # ============================================================
    # 1. KPI卡片行 (增强版: 含阈值颜色)
    # ============================================================
    all_holdings = dl.load_holdings_realtime()
    holdings = dl.filter_active_holdings(all_holdings)
    pnl_data = dl.calc_holdings_pnl(holdings)
    risk_params = dl.load_config_risk_params()

    total_value = sum(v["value"] for v in pnl_data.values())
    total_cost = sum(v["cost_total"] for v in pnl_data.values())
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    total_capital = risk_params.get("TOTAL_CAPITAL", total_value)
    available_cash = risk_params.get("AVAILABLE_CASH", total_capital - total_value)
    position_ratio = (total_value / total_capital * 100) if total_capital > 0 else 0
    max_holdings = risk_params.get("MAX_HOLDINGS", 8)

    # 市场情绪
    pulse = dl.load_market_pulse()
    pulse_temp = pulse.get("temperature", 0)
    pulse_level = pulse.get("level", "未知")
    activity = pulse.get("activity", {})

    # 盘中状态
    intraday = dl.load_intraday_state()
    alert_level = intraday.get("alert_level", "normal")

    # 第一行: 核心资产指标
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("总资产", f"¥{total_capital:,.0f}")
    c2.metric("持仓市值", f"¥{total_value:,.0f}",
              f"{position_ratio:.0f}%仓位",
              delta_color="inverse" if position_ratio > 85 else "normal")
    c3.metric("总盈亏", f"¥{total_pnl:+,.0f}", f"{total_pnl_pct:+.1f}%")
    c4.metric("可用资金", f"¥{available_cash:,.0f}",
              delta_color="inverse" if available_cash < 10000 else "normal")
    c5.metric("持仓数", f"{len(holdings)}/{max_holdings}")
    c6.metric("市场情绪", f"{pulse_temp}° {pulse_level}",
              f"涨{activity.get('up', 0)}/跌{activity.get('down', 0)}")

    st.markdown("---")

    # ============================================================
    # 2. 市场情绪速览 + 策略状态灯 (并排)
    # ============================================================
    sent_col, status_col = st.columns([3, 2])

    with sent_col:
        st.subheader("📊 市场情绪速览")
        if pulse and pulse.get("success"):
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("情绪温度", f"{pulse_temp}°",
                       "狂热" if pulse_temp > 70 else ("中性" if pulse_temp > 40 else "恐慌"))
            up_count = activity.get("up", 0)
            down_count = activity.get("down", 0)
            m2.metric("涨跌家数", f"{up_count}/{down_count}",
                       f"涨停{activity.get('limit_up', 0)}/跌停{activity.get('limit_down', 0)}")
            m3.metric("两市成交", f"{activity.get('amount', 0):,.0f}亿")
            margin = pulse.get("margin", {})
            m4.metric("融资余额", f"{margin.get('balance', 0):,.0f}亿",
                       f"{margin.get('change_pct', 0):+.2f}%")
            advice = pulse.get("advice", "")
            m5.metric("操作建议", advice[:12] + "..." if len(advice) > 12 else advice)
            st.caption(f"数据日期: {pulse.get('date', 'N/A')}")
        else:
            st.info("暂无市场情绪数据。盘中自动从行情接口获取。")

    with status_col:
        st.subheader("🚦 策略运行状态")
        # 从 intraday_state 和配置推断策略状态
        _render_strategy_status(intraday, pnl_data, risk_params)

    st.markdown("---")

    # ============================================================
    # 3. 美股隔夜赛道涨幅
    # ============================================================
    try:
        from dashboard.pages.us_sector_overnight import render_us_sector_overnight
        render_us_sector_overnight()
    except Exception as e:
        logger.warning(f"美股赛道模块加载失败: {e}")
        st.caption("美股隔夜数据暂不可用")

    st.markdown("---")

    # ============================================================
    # 4. 左栏: 持仓盈亏 + 右栏: 信号/预警/风控
    # ============================================================
    left_col, right_col = st.columns([3, 2])

    with left_col:
        st.subheader("💼 持仓盈亏分布")
        if pnl_data:
            active_pnl = {k: v for k, v in pnl_data.items()
                          if v.get("active", v["shares"] > 0)}
            if active_pnl:
                # 按盈亏排序
                sorted_items = sorted(active_pnl.items(),
                                      key=lambda x: x[1]["pnl"], reverse=True)
                names = [v["name"] for _, v in sorted_items]
                pnls = [v["pnl"] for _, v in sorted_items]
                weights = [v.get("weight", 0) * 100 for _, v in sorted_items]
                colors = [COLOR_UP if p >= 0 else COLOR_DOWN for p in pnls]

                fig = go.Figure(go.Bar(
                    x=names, y=pnls,
                    marker_color=colors,
                    text=[f"¥{p:+,.0f}" for p in pnls],
                    textposition="outside",
                    customdata=weights,
                    hovertemplate="%{x}<br>盈亏: ¥%{y:,.0f}<br>仓位: %{customdata:.1f}%<extra></extra>",
                ))
                _layout = {k: v for k, v in PLOTLY_LAYOUT.items()
                           if k not in ("xaxis", "yaxis", "height")}
                fig.update_layout(
                    **_layout, height=280,
                    yaxis=dict(title="浮动盈亏 (¥)", gridcolor="#E8ECF0"),
                    xaxis=dict(tickangle=30),
                    showlegend=False,
                )
                st.plotly_chart(fig, use_container_width=True)

                # 超阈值预警标红
                max_single = risk_params.get("MAX_SINGLE_STOCK_RATIO", 0.15) * 100
                over_limit = [(v["name"], v.get("weight", 0) * 100)
                              for _, v in sorted_items
                              if v.get("weight", 0) * 100 > max_single]
                if over_limit:
                    for name, w in over_limit:
                        st.warning(f"⚠️ {name} 仓位 {w:.1f}% 超过上限 {max_single:.0f}%")
        else:
            st.info("无持仓数据")

    with right_col:
        # 买点信号
        st.subheader("🎯 今日信号")
        buy_signals = dl.load_buy_signals()
        if buy_signals:
            recent = buy_signals[-5:] if len(buy_signals) > 5 else buy_signals
            for sig in reversed(recent):
                code = sig.get("code", "")
                name = sig.get("name", code)
                tier = sig.get("tier", "")
                r5 = sig.get("r5", None)
                r5_str = f" | 5日: {r5:+.1f}%" if r5 is not None else ""
                st.success(f"📈 **{name}** ({code}) [{tier}]{r5_str}")
        else:
            st.caption("今日无买点信号")

        # 预警摘要
        st.subheader("⚡ 最新预警")
        alert_history = dl.load_alert_history()
        if alert_history:
            recent_alerts = alert_history[-5:] if len(alert_history) > 5 else alert_history
            for alert in reversed(recent_alerts):
                if isinstance(alert, dict):
                    atime = alert.get("alert_time", alert.get("date", ""))
                    name = alert.get("name", alert.get("code", ""))
                    atype = alert.get("type", "")
                    level = alert.get("level", "info")
                    emoji = "🔴" if level == "critical" else ("🟡" if level == "warning" else "🔵")
                    st.caption(f"{emoji} {atime[:16]} | {name} {atype}")
        else:
            st.caption("暂无预警记录")

        # 风控快览
        st.markdown("---")
        st.subheader("🛡️ 风控快览")
        _render_risk_quick(pnl_data, risk_params, intraday)


def _render_strategy_status(intraday: dict, pnl_data: dict, params: dict):
    """策略运行状态灯"""
    alert_level = intraday.get("alert_level", "normal")
    gradient = intraday.get("gradient_triggered", {})
    stop_touch = intraday.get("stop_loss_first_touch", {})

    # 盘中预警等级
    level_map = {"normal": "🟢 正常", "warning": "🟡 警告",
                 "emergency": "🔴 紧急"}
    level_text = level_map.get(alert_level, f"⚪ {alert_level}")

    st.markdown(f"- 盘中预警: **{level_text}**")

    # 梯度减仓触发
    if gradient:
        names = list(gradient.keys())[:3]
        st.markdown(f"- 梯度减仓: 🟡 **{len(gradient)}只** ({', '.join(names)})")
    else:
        st.markdown("- 梯度减仓: 🟢 无触发")

    # 止损触及
    if stop_touch:
        st.markdown(f"- 止损触及: 🔴 **{len(stop_touch)}只** 首次触及止损线")
    else:
        st.markdown("- 止损触及: 🟢 未触及")

    # 持仓健康度
    if pnl_data:
        losing = sum(1 for v in pnl_data.values() if v["pnl"] < 0)
        total = len(pnl_data)
        health = "🟢 良好" if losing <= total * 0.3 else ("🟡 一般" if losing <= total * 0.6 else "🔴 较差")
        st.markdown(f"- 持仓健康: {health} (盈{total - losing}/亏{losing})")

    # 数据日期
    date_str = intraday.get("date", "")
    if date_str:
        st.caption(f"盘中状态: {date_str}")


def _render_risk_quick(pnl_data: dict, params: dict, intraday: dict):
    """风控快览小组件"""
    if not pnl_data:
        st.caption("无持仓数据")
        return

    weights = [v["weight"] for v in pnl_data.values()]
    hhi = sum(w ** 2 for w in weights) if weights else 0
    max_w = max(weights) if weights else 0
    max_limit = params.get("MAX_SINGLE_STOCK_RATIO", 0.15)
    total_cost = sum(v["cost_total"] for v in pnl_data.values())
    total_value = sum(v["value"] for v in pnl_data.values())
    pnl_pct = ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0

    # 风控指标
    st.markdown(f"- 持仓HHI: **{hhi:.3f}** {'🟢' if hhi < 0.15 else '🟡' if hhi < 0.25 else '🔴'}")
    st.markdown(f"- 最大单只: **{max_w:.0%}** (上限{max_limit:.0%}) {'🟢' if max_w <= max_limit else '🔴'}")
    st.markdown(f"- 组合盈亏: **{pnl_pct:+.1f}%** {'🟢' if pnl_pct > 0 else '🔴'}")

    # 止损状态
    stop_touch = intraday.get("stop_loss_first_touch", {})
    if stop_touch:
        st.markdown(f"- 今日止损触及: 🔴 {len(stop_touch)}只")
