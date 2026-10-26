# -*- coding: utf-8 -*-
"""
选股可视化页
=============
CANSLIM选股结果展示 / 因子得分雷达图 / 技术形态标注
"""

import os
import sys
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from dashboard import data_loader as dl
from dashboard.theme import PLOTLY_LAYOUT, PLOTLY_LAYOUT_TALL, CATEGORY_COLORS, COLOR_UP, COLOR_DOWN, COLOR_ACCENT


def render(date_range=None):
    st.header("🎯 选股可视化")

    # 加载选股数据: 优先 stock_pool.json，降级 scan_cache.json
    stock_pool_path = os.path.join(os.path.dirname(dl.HOLDINGS_PATH), "stock_pool.json")
    pool = dl._load_json(stock_pool_path, default=[])

    # 降级到 scan_cache
    scan_data = dl.load_scan_cache()
    scan_details = scan_data.get("details", []) if isinstance(scan_data, dict) else []

    if not pool and not scan_details:
        st.info("暂无选股数据。运行CANSLIM选股后自动生成。")
        st.caption(f"数据路径: {stock_pool_path}")

        st.markdown("---")
        st.markdown("#### 💡 如何获取选股数据？")
        st.markdown("""
**方式一：运行综合报告（推荐）**
```bash
python run.py
```
综合报告会自动运行CANSLIM选股并生成 stock_pool.json

**方式二：单独运行选股引擎**
```bash
python caopan_runner.py
```

运行完成后刷新此页面即可看到结果。
""")

        # 显示当前持仓作为参考
        all_holdings = dl.load_holdings_realtime()
        active = dl.filter_active_holdings(all_holdings)
        if active:
            st.markdown("#### 📋 当前持仓（供参考）")
            pnl_data = dl.calc_holdings_pnl(active)
            rows = [{"代码": k, "名称": v["name"], "持仓": v["shares"],
                      "盈亏%": f"{v['pnl_pct']:+.1f}%"}
                     for k, v in list(pnl_data.items())[:10]]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        return

    # ============================================================
    # 1. 选股结果表格 (支持 stock_pool 和 scan_cache 两种数据源)
    # ============================================================
    st.subheader("📋 选股结果")

    if isinstance(pool, list) and pool:
        df = pd.DataFrame(pool)
        # 显示关键列
        display_cols = [c for c in ["code", "name", "total_score", "N_score",
                                     "S_score", "L_score", "CAI_score", "P_score"]
                       if c in df.columns]
        if display_cols:
            st.dataframe(df[display_cols].head(20), use_container_width=True,
                        hide_index=True)

        # 选择股票查看详情
        if "code" in df.columns and "name" in df.columns:
            options = [f"{r['code']} {r['name']}" for _, r in df.head(20).iterrows()]
            selected = st.selectbox("选择股票查看详情", options)
            if selected:
                code = selected.split()[0]
                stock_row = df[df["code"] == code].iloc[0] if code in df["code"].values else None

                if stock_row is not None:
                    col_left, col_right = st.columns(2)

                    with col_left:
                        # CANSLIM雷达图
                        score_cols = {"N": "N_score", "S": "S_score",
                                     "L": "L_score", "CAI": "CAI_score", "P": "P_score"}
                        available = {k: stock_row.get(v, 0) for k, v in score_cols.items()
                                    if v in stock_row.index and pd.notna(stock_row.get(v))}

                        if available:
                            categories = list(available.keys())
                            values = list(available.values())
                            values.append(values[0])
                            categories.append(categories[0])

                            fig = go.Figure(go.Scatterpolar(
                                r=values, theta=categories,
                                fill="toself",
                                fillcolor="rgba(24,144,255,0.15)",
                                line=dict(color=COLOR_ACCENT, width=2),
                                marker=dict(size=8),
                            ))
                            fig.update_layout({
                                **PLOTLY_LAYOUT,
                                "title": f"{code} CANSLIM雷达图 (总分: {stock_row.get('total_score', 0)})",
                                "polar": dict(
                                    radialaxis=dict(visible=True, range=[0, 25]),
                                ),
                                "height": 350,
                            })
                            st.plotly_chart(fig, use_container_width=True)

                    with col_right:
                        # K线图
                        kline = dl.load_daily_kline(code, days=120)
                        if not kline.empty:
                            fig = go.Figure(data=[go.Candlestick(
                                x=kline["date"],
                                open=kline["open"], high=kline["high"],
                                low=kline["low"], close=kline["close"],
                                increasing_line_color=COLOR_UP,
                                decreasing_line_color=COLOR_DOWN,
                            )])
                            if len(kline) >= 20:
                                ma20 = kline["close"].rolling(20).mean()
                                fig.add_trace(go.Scatter(
                                    x=kline["date"], y=ma20,
                                    mode="lines", line=dict(color="#f39c12", width=1),
                                    name="MA20"))
                            if len(kline) >= 60:
                                ma60 = kline["close"].rolling(60).mean()
                                fig.add_trace(go.Scatter(
                                    x=kline["date"], y=ma60,
                                    mode="lines", line=dict(color="#9b59b6", width=1),
                                    name="MA60"))

                            fig.update_layout({
                                **PLOTLY_LAYOUT, "height": 350,
                                "title": f"{code} K线图",
                                "xaxis_rangeslider_visible": False,
                            })
                            st.plotly_chart(fig, use_container_width=True)
                        else:
                            st.info(f"暂无{code}的K线数据")

                    # 因子得分条形图
                    st.subheader(f"📊 {code} 因子得分")
                    factor_stats = dl.load_all_factor_ic()
                    st.caption("因子IC/IR表现可前往「因子监控」面板查看详情")

    elif scan_details:
        # 使用 scan_cache 数据展示选股结果
        scan_time = scan_data.get("scan_time", "N/A")
        st.caption(f"数据来源: 选股扫描缓存 | 扫描时间: {scan_time} | 候选股: {len(scan_details)}只")

        df_scan = pd.DataFrame(scan_details)
        display_cols = [c for c in ["code", "name", "price", "change_pct",
                                     "amount", "turnover", "sector", "scan_tag"]
                        if c in df_scan.columns]
        if display_cols:
            # 重命名为中文
            rename_map = {"code": "代码", "name": "名称", "price": "现价",
                          "change_pct": "涨跌幅%", "amount": "成交额(亿)",
                          "turnover": "换手率%", "sector": "板块", "scan_tag": "标签"}
            df_display = df_scan[display_cols].rename(columns=rename_map).head(30)
            st.dataframe(df_display, use_container_width=True, hide_index=True)

        # 板块分布饼图
        if "sector" in df_scan.columns:
            sector_dist = df_scan["sector"].value_counts().head(10)
            if not sector_dist.empty:
                st.subheader("🏷️ 候选股板块分布")
                fig = go.Figure(go.Pie(
                    labels=sector_dist.index.tolist(),
                    values=sector_dist.values.tolist(),
                    hole=0.35, textinfo="label+percent",
                ))
                _lp = {k: v for k, v in PLOTLY_LAYOUT.items() if k not in ("height",)}
                fig.update_layout(**_lp, height=300, showlegend=False)
                st.plotly_chart(fig, use_container_width=True)

        # 涨跌幅分布
        if "change_pct" in df_scan.columns:
            st.subheader("📈 涨跌幅分布")
            df_sorted = df_scan.sort_values("change_pct", ascending=False).head(20)
            names = df_sorted["name"].tolist()
            changes = df_sorted["change_pct"].tolist()
            colors = [COLOR_UP if c >= 0 else COLOR_DOWN for c in changes]

            fig = go.Figure(go.Bar(
                x=names, y=changes, marker_color=colors,
                text=[f"{c:+.1f}%" for c in changes],
                textposition="outside",
            ))
            _lp = {k: v for k, v in PLOTLY_LAYOUT.items()
                   if k not in ("xaxis", "yaxis", "height")}
            fig.update_layout(
                **_lp, height=280,
                yaxis=dict(title="涨跌幅 (%)", gridcolor="#E8ECF0"),
                xaxis=dict(tickangle=30),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("选股数据格式异常")
