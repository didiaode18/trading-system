# -*- coding: utf-8 -*-
"""
因子监控面板
=============
71因子IC/IR趋势 / 因子相关性热力图 / 因子衰减预警

三个子标签:
  A. IC/IR排行榜 + 散点图
  B. 因子详情（IC时序 + 分层回测）
  C. 衰减预警面板
"""

import os
import sys
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from dashboard import data_loader as dl
from dashboard.theme import (
    PLOTLY_LAYOUT, PLOTLY_LAYOUT_TALL, CATEGORY_COLORS,
    COLOR_UP, COLOR_DOWN, COLOR_WARN,
)
from dashboard.components.factor_charts import (
    render_ic_scatter, render_ic_bar, render_ic_heatmap,
)


def _get_factor_category(name: str) -> str:
    """从因子名推断分类"""
    prefixes = {
        "technical": ["ma_", "macd", "rsi", "bb_", "atr", "kdj", "cci", "ema"],
        "volume": ["obv", "vwap", "vol_ratio", "mfi", "cmf", "ad_line"],
        "momentum": ["mom_", "ret_", "rs_", "accel", "breakout", "pullback"],
        "quant": ["qe_"],
        "smart_money": ["smf_"],
        "reversal": ["improved_reversal"],
        "vol_dist": ["vol_dist_"],
        "volatility": ["low_volatility"],
        "tail_volume": ["tail_volume"],
        "quality": ["quality_"],
        "value": ["value_"],
        "growth": ["growth_"],
        "liquidity": ["liquidity_"],
        "dividend": ["dividend_"],
    }
    for cat, pfx_list in prefixes.items():
        for pfx in pfx_list:
            if name.startswith(pfx):
                return cat
    return "other"


def render(date_range=None):
    st.header("📊 因子监控面板")

    # 加载因子IC统计
    factor_stats = dl.load_all_factor_ic()
    validation = dl.load_factor_validation()

    # 合并验证结果到stats
    for name, vdata in validation.items():
        if name not in factor_stats:
            factor_stats[name] = {
                "ic_mean": vdata.get("ic_mean", 0),
                "ic_std": vdata.get("ic_std", 0),
                "ir": vdata.get("ir", 0),
                "ic_win_rate": vdata.get("ic_win_rate", 0),
                "t_stat": vdata.get("t_stat", 0),
                "n_periods": vdata.get("n_periods", 0),
                "status": "pass" if vdata.get("pass") else "obs",
                "records": [],
            }

    # 补充分类信息
    for name in factor_stats:
        factor_stats[name]["category"] = _get_factor_category(name)

    # 标签页
    tab_a, tab_b, tab_c, tab_d = st.tabs(["📊 IC/IR排行榜", "📈 因子详情", "📉 IC累积曲线", "⚠️ 衰减预警"])

    # ============================================================
    # Tab A: IC/IR排行榜 + 散点图
    # ============================================================
    with tab_a:
        # 分类筛选
        all_cats = sorted(set(v.get("category", "other")
                              for v in factor_stats.values()))
        selected_cat = st.multiselect("筛选分类", ["全部"] + all_cats,
                                      default=["全部"])

        filtered = factor_stats
        if "全部" not in selected_cat:
            filtered = {k: v for k, v in factor_stats.items()
                       if v.get("category") in selected_cat}

        # 散点图
        fig = render_ic_scatter(filtered)
        st.plotly_chart(fig, use_container_width=True)

        # 排行表格
        if filtered:
            rows = []
            for name, s in sorted(filtered.items(),
                                  key=lambda x: abs(x[1]["ir"]), reverse=True):
                status_emoji = {"pass": "🟢", "obs": "🟡", "weak": "⚪"}.get(
                    s.get("status", ""), "⚪")
                rows.append({
                    "因子": name,
                    "分类": s.get("category", ""),
                    "IC均值": s["ic_mean"],
                    "IC标准差": s.get("ic_std", 0),
                    "IR": s["ir"],
                    "IC胜率%": s.get("ic_win_rate", 0),
                    "t值": s.get("t_stat", 0),
                    "期数": s.get("n_periods", 0),
                    "状态": f"{status_emoji} {s.get('status', '').upper()}",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True,
                        hide_index=True, height=400)

        st.caption(f"共 {len(factor_stats)} 个因子 | "
                  f"🟢PASS: {sum(1 for v in factor_stats.values() if v.get('status')=='pass')} | "
                  f"🟡OBS: {sum(1 for v in factor_stats.values() if v.get('status')=='obs')}")

    # ============================================================
    # Tab B: 因子详情（IC时序）
    # ============================================================
    with tab_b:
        if not factor_stats:
            st.info("暂无因子IC数据。运行因子验证脚本后数据将自动加载。")
        else:
            selected = st.selectbox("选择因子", list(factor_stats.keys()), index=0)
            if selected and selected in factor_stats:
                stats = factor_stats[selected]

                # 指标卡
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("IC均值", f"{stats['ic_mean']:.4f}")
                c2.metric("IR", f"{stats['ir']:.2f}")
                c3.metric("IC胜率", f"{stats.get('ic_win_rate', 0):.1f}%")
                c4.metric("t值", f"{stats.get('t_stat', 0):.2f}")
                c5.metric("期数", stats.get("n_periods", 0))

                # IC时序图
                records = stats.get("records", [])
                fig = render_ic_bar(selected, records)
                st.plotly_chart(fig, use_container_width=True)

                # 状态判定
                if stats.get("status") == "pass":
                    st.success(f"✅ {selected} 通过IR>0.3门槛，因子有效")
                elif stats.get("status") == "obs":
                    st.warning(f"🟡 {selected} 处于观察模式（0.15<IR<0.3），需更多数据验证")
                else:
                    st.info(f"⚪ {selected} IC较弱（IR<0.15），建议观察或调整")

    # ============================================================
    # Tab C: IC累积曲线 (Sprint 2)
    # ============================================================
    with tab_c:
        st.subheader("📉 因子IC累积曲线")
        st.caption("展示各因子近期IC值的累积走势，用于判断因子稳定性")

        if not factor_stats:
            st.info("暂无因子IC数据")
        else:
            # 选择因子查看IC累积曲线
            factor_names = sorted(factor_stats.keys())
            selected_factors = st.multiselect(
                "选择因子 (最多5个)", factor_names,
                default=factor_names[:3] if len(factor_names) >= 3 else factor_names
            )

            if selected_factors:
                fig = go.Figure()
                for fname in selected_factors[:5]:
                    records = factor_stats[fname].get("records", [])
                    if records:
                        dates = [r.get("date", "") if isinstance(r, dict) else "" for r in records]
                        ics = [r.get("ic", 0) if isinstance(r, dict) else 0 for r in records]
                        # 累积IC
                        cum_ic = []
                        running = 0
                        for ic in ics:
                            running += ic
                            cum_ic.append(running)
                        fig.add_trace(go.Scatter(
                            x=dates, y=cum_ic,
                            mode="lines", name=fname,
                            line=dict(width=2),
                        ))

                if fig.data:
                    _layout = {k: v for k, v in PLOTLY_LAYOUT_TALL.items()
                               if k not in ("xaxis", "yaxis", "height", "legend")}
                    fig.update_layout(
                        **_layout, height=400,
                        title="因子IC累积走势对比",
                        xaxis=dict(title="日期"),
                        yaxis=dict(title="累积IC", gridcolor="#E8ECF0"),
                        legend=dict(orientation="h", y=1.02, xanchor="center", x=0.5),
                    )
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("所选因子无IC记录数据")

            # 因子有效性排行条形图
            st.markdown("---")
            st.markdown("#### 因子有效性排行 (按IR绝对值)")
            sorted_factors = sorted(factor_stats.items(),
                                     key=lambda x: abs(x[1]["ir"]), reverse=True)[:15]
            fnames = [f[0] for f in sorted_factors]
            irs = [f[1]["ir"] for f in sorted_factors]
            bar_colors = [COLOR_UP if ir > 0.3 else (COLOR_WARN if ir > 0.15 else "#999999")
                          for ir in irs]

            fig2 = go.Figure(go.Bar(
                x=fnames, y=irs,
                marker_color=bar_colors,
                text=[f"{ir:.2f}" for ir in irs],
                textposition="outside",
            ))
            _layout2 = {k: v for k, v in PLOTLY_LAYOUT.items()
                        if k not in ("xaxis", "yaxis", "height")}
            fig2.update_layout(
                **_layout2, height=300,
                yaxis=dict(title="IR值", gridcolor="#E8ECF0",
                           zerolinecolor="#999999", zerolinewidth=1),
                xaxis=dict(tickangle=45, tickfont=dict(size=9)),
                showlegend=False,
            )
            # 添加参考线
            fig2.add_hline(y=0.3, line_dash="dash", line_color="green",
                           annotation_text="PASS线 0.3", opacity=0.5)
            fig2.add_hline(y=0.15, line_dash="dash", line_color="orange",
                           annotation_text="OBS线 0.15", opacity=0.5)
            st.plotly_chart(fig2, use_container_width=True)

    # ============================================================
    # Tab D: 衰减预警
    # ============================================================
    with tab_d:
        st.subheader("⚠️ 因子衰减预警")
        st.caption("衰减标准: 连续5期 |IC| < 0.02")

        decaying = {k: v for k, v in factor_stats.items()
                   if v.get("status") == "weak" and v.get("n_periods", 0) >= 5}
        negative = {k: v for k, v in factor_stats.items()
                   if v["ir"] < -0.2}
        strong = {k: v for k, v in factor_stats.items()
                 if v["ir"] > 0.3}

        if strong:
            st.subheader(f"🟢 强劲因子 ({len(strong)}个)")
            for name, s in sorted(strong.items(), key=lambda x: x[1]["ir"], reverse=True):
                st.success(f"**{name}**: IR={s['ir']:.2f}, IC={s['ic_mean']:.4f}")

        if decaying:
            st.subheader(f"🟡 衰减因子 ({len(decaying)}个)")
            for name, s in decaying.items():
                st.warning(f"**{name}**: IC均值={s['ic_mean']:.4f}, "
                          f"当前权重可能已被降权")
        else:
            if factor_stats:
                st.info("✅ 当前无衰减因子")

        if negative:
            st.subheader(f"🔴 反向因子 ({len(negative)}个)")
            st.caption("IC持续为负 → 因子有效但方向相反，可反向使用")
            for name, s in negative.items():
                st.error(f"**{name}**: IR={s['ir']:.2f} (负向有效)")
        else:
            if factor_stats:
                st.info("✅ 当前无反向因子")

        if not factor_stats:
            st.info("暂无IC历史数据。运行 `_validate_phase1.py` 或 `_validate_phase2.py` 后自动生成。")
