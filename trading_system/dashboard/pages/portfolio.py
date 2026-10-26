# -*- coding: utf-8 -*-
"""
持仓管理页 V8.0
================
Sprint 1 升级:
- 持仓列表增强 (盈亏比例/仓位占比/止损距离进度条)
- 条件单可视化 (止损线/止盈线距离标注)
- 赛道结构饼图 (按赛道聚合)
- 超阈值预警标红
数据统一走 config.load_holdings()，实时计算不缓存。
"""

import os
import sys
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from dashboard import data_loader as dl
from dashboard.theme import (
    PLOTLY_LAYOUT, COLOR_UP, COLOR_DOWN, COLOR_ACCENT, COLOR_WARN,
)


def render(date_range=None):
    st.header("💼 持仓管理")

    # ============================================================
    # 1. 实时加载持仓 + 风控参数
    # ============================================================
    all_holdings = dl.load_holdings_realtime()

    if not all_holdings:
        st.warning("⚠️ 无持仓数据")
        st.info("请确认 `holdings.json` 是否存在，或通过 `update_holdings.py` 同步券商持仓。")
        return

    holdings = dl.filter_active_holdings(all_holdings)
    pnl_data = dl.calc_holdings_pnl(holdings)
    risk_params = dl.load_config_risk_params()
    intraday = dl.load_intraday_state()

    total_value = sum(v["value"] for v in pnl_data.values())
    total_cost = sum(v["cost_total"] for v in pnl_data.values())
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    total_capital = risk_params.get("TOTAL_CAPITAL", total_value)
    available_cash = risk_params.get("AVAILABLE_CASH", total_capital - total_value)
    max_stock_ratio = risk_params.get("MAX_SINGLE_STOCK_RATIO", 0.15)
    max_ETF_ratio = risk_params.get("MAX_SINGLE_ETF_RATIO", 0.20)
    sector_max = risk_params.get("SECTOR_MAX_RATIO", 0.40)
    max_holdings = risk_params.get("MAX_HOLDINGS", 8)

    # KPI
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("总市值", f"¥{total_value:,.0f}")
    c2.metric("总盈亏", f"¥{total_pnl:+,.0f}", f"{total_pnl_pct:+.1f}%")
    c3.metric("持仓数", f"{len(holdings)}/{max_holdings}")
    c4.metric("仓位比", f"{total_value/total_capital:.0%}" if total_capital > 0 else "N/A")
    c5.metric("可用资金", f"¥{available_cash:,.0f}")

    # 已清仓摘要
    inactive_count = len(all_holdings) - len(holdings)
    if inactive_count > 0:
        with st.expander(f"📦 已清仓标的 ({inactive_count}只)"):
            inactive = {k: v for k, v in all_holdings.items()
                        if k not in holdings}
            for code, pos in inactive.items():
                st.caption(f"{pos.get('name', code)} ({code}): "
                           f"盈亏 ¥{pos.get('pnl', 0):+,.0f}")

    st.markdown("---")

    # ============================================================
    # 2. 持仓详情表 (增强: 止损距离 + 条件单进度条)
    # ============================================================
    st.subheader("📋 持仓详情")

    rows = []
    for code, d in pnl_data.items():
        pos = holdings.get(code, {})
        stop_loss = d.get("stop_loss") or pos.get("stop_loss", d["cost"] * 0.88)
        sl_distance = (d["price"] - stop_loss) / d["price"] * 100 if d["price"] > 0 else 0
        is_etf = len(code) == 6 and code.startswith(("5", "1"))
        ratio_limit = max_ETF_ratio if is_etf else max_stock_ratio

        # 超阈值标记
        over_limit = d["weight"] > ratio_limit
        near_stop = sl_distance < 3

        row = {
            "代码": code,
            "名称": d["name"],
            "股数": d["shares"],
            "成本": f"{d['cost']:.3f}",
            "现价": f"{d['price']:.2f}",
            "市值": f"¥{d['value']:,.0f}",
            "盈亏": f"¥{d['pnl']:+,.0f}",
            "盈亏%": f"{d['pnl_pct']:+.1f}%",
            "仓位": f"{d['weight']:.1%}",
            "止损价": f"{stop_loss:.2f}",
            "距止损": f"{sl_distance:.1f}%",
        }
        if over_limit:
            row["状态"] = "🔴超限"
        elif near_stop:
            row["状态"] = "🟡近止损"
        else:
            row["状态"] = "🟢正常"
        rows.append(row)

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ============================================================
    # 2.5 条件单可视化: 止损距离进度条
    # ============================================================
    st.subheader("🎯 条件单状态")

    gradient_triggered = intraday.get("gradient_triggered", {})
    stop_touch = intraday.get("stop_loss_first_touch", {})

    cond_rows = []
    for code, d in pnl_data.items():
        pos = holdings.get(code, {})
        stop_loss = d.get("stop_loss") or pos.get("stop_loss", d["cost"] * 0.88)
        sl_pct = (d["price"] - stop_loss) / d["price"] * 100 if d["price"] > 0 else 100
        cost_pct = d["pnl_pct"]

        # 止损进度: 0%=已触, 100%=远离
        sl_progress = min(max(sl_pct / 15 * 100, 0), 100)  # 15%为满量程

        status = "🟢 安全"
        if code in stop_touch:
            status = "🔴 已触及止损"
        elif code in gradient_triggered:
            levels = gradient_triggered[code]
            if "emergency" in levels:
                status = "🔴 紧急减仓"
            elif "critical" in levels:
                status = "🟠 梯度减仓"
            elif "warning" in levels:
                status = "🟡 预警"
        elif sl_pct < 3:
            status = "🟡 接近止损"

        cond_rows.append({
            "标的": f"{d['name']}({code})",
            "现价": f"{d['price']:.2f}",
            "止损价": f"{stop_loss:.2f}",
            "距止损%": f"{sl_pct:.1f}%",
            "浮盈%": f"{cost_pct:+.1f}%",
            "状态": status,
            "_progress": sl_progress,
        })

    if cond_rows:
        cond_df = pd.DataFrame(cond_rows)
        # 用 st.dataframe 展示（去掉内部列）
        st.dataframe(cond_df[[c for c in cond_df.columns if not c.startswith("_")]],
                     use_container_width=True, hide_index=True)

    st.markdown("---")

    # ============================================================
    # 3. 仓位分布饼图 + 赛道饼图
    # ============================================================
    left_col, mid_col, right_col = st.columns(3)

    with left_col:
        st.subheader("📊 个股仓位")
        sorted_by_weight = sorted(pnl_data.items(),
                                   key=lambda x: x[1]["weight"], reverse=True)
        names = [v["name"] for _, v in sorted_by_weight]
        values = [v["weight"] * 100 for _, v in sorted_by_weight]
        pie_colors = [COLOR_UP if v["pnl"] >= 0 else COLOR_DOWN
                      for _, v in sorted_by_weight]

        fig = go.Figure(go.Pie(
            labels=names, values=values, hole=0.35,
            marker=dict(colors=pie_colors),
            textinfo="label+percent",
            textfont=dict(size=9),
        ))
        _lp = {k: v for k, v in PLOTLY_LAYOUT.items() if k not in ("height",)}
        fig.update_layout(**_lp, height=300, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with mid_col:
        st.subheader("🏷️ 赛道分布")
        # 按赛道聚合
        sector_weights = {}
        for code, d in pnl_data.items():
            sector = d.get("sector", "未分类") or "未分类"
            sector_weights[sector] = sector_weights.get(sector, 0) + d["weight"]

        if sector_weights:
            sorted_sectors = sorted(sector_weights.items(),
                                     key=lambda x: x[1], reverse=True)
            s_names = [s[0] for s in sorted_sectors]
            s_values = [s[1] * 100 for s in sorted_sectors]

            fig = go.Figure(go.Pie(
                labels=s_names, values=s_values, hole=0.35,
                textinfo="label+percent",
                textfont=dict(size=9),
            ))
            _lp = {k: v for k, v in PLOTLY_LAYOUT.items() if k not in ("height",)}
            fig.update_layout(**_lp, height=300, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

            # 赛道超限检查
            for s_name, s_weight in sorted_sectors:
                if s_weight > sector_max:
                    st.warning(f"⚠️ 赛道「{s_name}」仓位 {s_weight:.0%} 超限 (上限{sector_max:.0%})")

    with right_col:
        st.subheader("📈 个股盈亏")
        codes = list(pnl_data.keys())
        names = [pnl_data[c]["name"] for c in codes]
        pnls = [pnl_data[c]["pnl"] for c in codes]
        bar_colors = [COLOR_UP if p >= 0 else COLOR_DOWN for p in pnls]

        fig = go.Figure(go.Bar(
            x=names, y=pnls,
            marker_color=bar_colors,
            text=[f"¥{p:+,.0f}" for p in pnls],
            textposition="outside",
        ))
        _lp = {k: v for k, v in PLOTLY_LAYOUT.items()
               if k not in ("xaxis", "yaxis", "height")}
        fig.update_layout(
            **_lp, height=300,
            yaxis=dict(title="盈亏(¥)", gridcolor="#E8ECF0"),
            xaxis=dict(tickangle=30),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)

    # ============================================================
    # 4. 盈亏归因瀑布图
    # ============================================================
    st.markdown("---")
    st.subheader("📊 盈亏归因")

    winners = sum(d["pnl"] for d in pnl_data.values() if d["pnl"] > 0)
    losers = sum(d["pnl"] for d in pnl_data.values() if d["pnl"] < 0)

    wc1, wc2, wc3 = st.columns(3)
    wc1.metric("盈利贡献", f"¥{winners:+,.0f}")
    wc2.metric("亏损拖累", f"¥{losers:+,.0f}")
    wc3.metric("净盈亏", f"¥{total_pnl:+,.0f}")

    fig = go.Figure(go.Waterfall(
        x=["盈利股", "亏损股", "净盈亏"],
        y=[winners, losers, total_pnl],
        measure=["relative", "relative", "total"],
        connector={"line": {"color": "#D0D5DD"}},
        increasing={"marker": {"color": COLOR_UP}},
        decreasing={"marker": {"color": COLOR_DOWN}},
        totals={"marker": {"color": COLOR_ACCENT}},
        text=[f"¥{winners:+,.0f}", f"¥{losers:+,.0f}", f"¥{total_pnl:+,.0f}"],
        textposition="outside",
    ))
    _lp = {k: v for k, v in PLOTLY_LAYOUT.items() if k not in ("height",)}
    fig.update_layout(**_lp, height=250, showlegend=False, title="盈亏归因瀑布")
    st.plotly_chart(fig, use_container_width=True)

    # ============================================================
    # 5. 风险提示 (增强: 多维度)
    # ============================================================
    st.markdown("---")
    st.subheader("⚠️ 风险提示")

    has_risk = False

    # 距止损<5%的标的
    for code, d in pnl_data.items():
        pos = holdings.get(code, {})
        stop_loss = pos.get("stop_loss", d["cost"] * 0.88)
        sl_dist = (d["price"] - stop_loss) / d["price"] * 100 if d["price"] > 0 else 100
        if sl_dist < 5:
            has_risk = True
            emoji = "🔴" if sl_dist < 3 else "🟡"
            st.warning(f"{emoji} **{d['name']}**({code}): 距止损仅{sl_dist:.1f}%, 浮亏{d['pnl_pct']:.1f}%")

    # 超阈值标的
    for code, d in pnl_data.items():
        is_etf = len(code) == 6 and code.startswith(("5", "1"))
        limit = max_ETF_ratio if is_etf else max_stock_ratio
        if d["weight"] > limit:
            has_risk = True
            st.error(f"🔴 **{d['name']}**({code}): 仓位{d['weight']:.0%}超过上限{limit:.0%}")

    # 梯度减仓/止损触及
    if gradient_triggered:
        has_risk = True
        for code, levels in gradient_triggered.items():
            name = pnl_data.get(code, {}).get("name", code)
            st.warning(f"🟡 **{name}**({code}): 已触发梯度减仓 [{', '.join(levels)}]")

    if stop_touch:
        has_risk = True
        for code in stop_touch:
            name = pnl_data.get(code, {}).get("name", code)
            st.error(f"🔴 **{name}**({code}): 今日首次触及止损线")

    if not has_risk:
        st.success("✅ 所有持仓风控状态正常，无紧急风险")
