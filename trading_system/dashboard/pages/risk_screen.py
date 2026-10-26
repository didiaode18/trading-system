# -*- coding: utf-8 -*-
"""
风控实时大屏
=============
12道风控关卡通过/拦截状态 / 组合风险指标 / 预警历史时间线
"""

import os
import sys
import datetime
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from dashboard import data_loader as dl
from dashboard.theme import (
    PLOTLY_LAYOUT, COLOR_UP, COLOR_DOWN, COLOR_WARN, COLOR_ACCENT,
    status_emoji, GATE_COLORS,
)


# 12道关卡定义
GATE_DEFS = [
    ("关卡-2", "黑天鹅检测", "极端行情检测"),
    ("关卡-1", "盘中风险标记", "盘中异常检测"),
    ("关卡0", "满仓禁止加仓", "情绪化交易防护"),
    ("关卡1", "暂停期检查", "连亏后暂停交易"),
    ("关卡2", "冷却期检查", "卖出后冷却期"),
    ("关卡3", "赛道集中度", "单一赛道上限40%"),
    ("关卡4", "单只集中度", "单只上限15%"),
    ("关卡5", "止损检查", "个股止损线"),
    ("关卡6", "连亏暂停", "连续亏损熔断"),
    ("关卡7", "策略失效", "胜率过低检测"),
    ("关卡8", "信号衰减", "因子IC衰减"),
    ("关卡9", "反冲动锁", "单日开仓次数限制"),
    ("关卡10", "时间窗口", "交易时间检查"),
    ("关卡11", "组合回撤", "组合级回撤熔断"),
    ("关卡12", "赛道HHI", "赛道集中度HHI"),
]


def render(date_range=None):
    st.header("🛡️ 风控实时大屏")

    # ============================================================
    # 1. 顶部状态灯
    # ============================================================
    risk_state = dl.load_risk_state()
    all_holdings = dl.load_holdings_realtime()
    holdings = dl.filter_active_holdings(all_holdings)
    pnl_data = dl.calc_holdings_pnl(holdings)

    total_value = sum(v["value"] for v in pnl_data.values())
    total_cost = sum(v["cost_total"] for v in pnl_data.values())
    total_pnl = total_value - total_cost

    try:
        import config
        total_capital = getattr(config, 'TOTAL_CAPITAL', total_value)
        position_ratio = (total_value / total_capital) if total_capital > 0 else 0
    except Exception:
        total_capital = total_value
        position_ratio = 1.0

    level = risk_state.get("current_level", "normal")
    win_rate = risk_state.get("win_rate", 0)
    consec = risk_state.get("consec_losses", 0)
    expectancy = risk_state.get("expectancy", 0)

    # F3修复: 动态计算风控等级，而非仅读取静态文件
    max_weight = max((v.get("weight", 0) for v in pnl_data.values()), default=0)
    pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    dynamic_level = "normal"
    level_reasons = []
    if position_ratio > 0.95 or max_weight > 0.30:
        dynamic_level = "critical"
        level_reasons.append(f"仓位{position_ratio:.0%}/单只{max_weight:.0%}严重超限")
    elif position_ratio > 0.85 or max_weight > 0.15:
        dynamic_level = "warning"
        level_reasons.append(f"仓位{position_ratio:.0%}/单只{max_weight:.0%}超限")
    if consec >= 3:
        dynamic_level = "critical"
        level_reasons.append(f"连亏{consec}笔触发熔断")
    elif consec >= 2 and dynamic_level != "critical":
        dynamic_level = "warning"
        level_reasons.append(f"连亏{consec}笔")
    if pnl_pct < -8 and dynamic_level != "critical":
        dynamic_level = "critical"
        level_reasons.append(f"组合回撤{pnl_pct:.1f}%")
    elif pnl_pct < -5 and dynamic_level not in ("critical",):
        dynamic_level = max(dynamic_level, "warning", key=lambda x: ["normal","warning","critical"].index(x))
        level_reasons.append(f"组合回撤{pnl_pct:.1f}%")
    # 取静态和动态中更严重的
    level_order = {"normal": 0, "warning": 1, "critical": 2}
    if level_order.get(dynamic_level, 0) > level_order.get(level, 0):
        level = dynamic_level

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("风控等级", f"{status_emoji(level)} {level}")
    c2.metric("总仓位", f"{position_ratio:.0%}",
              f"上限80%")
    c3.metric("连续亏损", f"{consec}笔",
              f"熔断:3笔")
    c4.metric("滚动胜率", f"{win_rate:.0%}")
    c5.metric("交易期望", f"{expectancy:.2%}")
    c6.metric("日盈亏", f"¥{total_pnl:+,.0f}")

    st.markdown("---")

    # ============================================================
    # 2. 关卡状态矩阵
    # ============================================================
    st.subheader("📋 关卡状态矩阵")

    # 从持仓数据推断关卡状态
    gate_status = []
    for gate_id, gate_name, gate_desc in GATE_DEFS:
        status = "pass"
        detail = ""

        if "赛道" in gate_name and holdings:
            # 简单检查赛道集中度
            max_weight = max((v.get("weight", 0) for v in pnl_data.values()), default=0)
            if max_weight > 0.15:
                status = "warn"
                detail = f"最大单只{max_weight:.0%}"
            else:
                detail = f"最大单只{max_weight:.0%}"
        elif "单只" in gate_name and holdings:
            max_weight = max((v.get("weight", 0) for v in pnl_data.values()), default=0)
            if max_weight > 0.15:
                status = "block"
                detail = f"超限: {max_weight:.0%}"
            else:
                detail = f"最大{max_weight:.0%}"
        elif "连亏" in gate_name:
            detail = f"当前{consec}笔"
            if consec >= 3:
                status = "block"
            elif consec >= 2:
                status = "warn"
        elif "策略失效" in gate_name:
            detail = f"胜率{win_rate:.0%}"
            if win_rate < 0.3:
                status = "block"
            elif win_rate < 0.4:
                status = "warn"
        elif "满仓" in gate_name:
            detail = f"仓位{position_ratio:.0%}"
            if position_ratio > 0.9:
                status = "block"
        elif "组合回撤" in gate_name:
            pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            detail = f"回撤{pnl_pct:.1f}%"
            if pnl_pct < -8:
                status = "block"
            elif pnl_pct < -5:
                status = "warn"
        else:
            detail = "正常"

        gate_status.append({
            "关卡": gate_id,
            "名称": gate_name,
            "说明": gate_desc,
            "状态": f"{status_emoji(status)} {status.upper()}",
            "详情": detail,
            "_status": status,
        })

    # 渲染为表格
    display_df = pd.DataFrame(gate_status)[["关卡", "名称", "说明", "状态", "详情"]]
    st.dataframe(display_df, use_container_width=True, hide_index=True, height=500)

    # 统计
    pass_count = sum(1 for g in gate_status if g["_status"] == "pass")
    warn_count = sum(1 for g in gate_status if g["_status"] == "warn")
    block_count = sum(1 for g in gate_status if g["_status"] == "block")
    st.caption(f"通过: {pass_count} | 警告: {warn_count} | 拦截: {block_count}")

    st.markdown("---")

    # ============================================================
    # 3. 组合风险指标
    # ============================================================
    st.subheader("📊 组合风险指标")

    rc1, rc2, rc3, rc4 = st.columns(4)

    # 持仓集中度 (HHI)
    if pnl_data:
        weights = [v["weight"] for v in pnl_data.values()]
        hhi = sum(w**2 for w in weights) if weights else 0
    else:
        hhi = 0

    # 最大单只仓位
    max_weight = max((v.get("weight", 0) for v in pnl_data.values()), default=0)

    # 浮亏最大的持仓
    if pnl_data:
        worst = min(pnl_data.values(), key=lambda x: x["pnl_pct"])
        worst_name = f"{worst['name']} {worst['pnl_pct']:.1f}%"
    else:
        worst_name = "无"

    rc1.metric("持仓HHI", f"{hhi:.3f}", "越低越分散")
    rc2.metric("最大单只仓位", f"{max_weight:.0%}", "上限15%")
    rc3.metric("最大浮亏", worst_name)
    try:
        import config
        max_hold = getattr(config, 'MAX_HOLDINGS', 7)
    except Exception:
        max_hold = 7
    rc4.metric("持仓数", f"{len(holdings)}/{max_hold}")

    st.markdown("---")

    # ============================================================
    # 4. 风控可视化图表
    # ============================================================
    st.subheader("📊 风险分布")

    chart_left, chart_right = st.columns(2)

    with chart_left:
        # 仓位分布饼图
        if pnl_data:
            top_holdings = sorted(pnl_data.items(),
                                  key=lambda x: x[1]["weight"], reverse=True)[:8]
            fig_pie = go.Figure(go.Pie(
                labels=[v[1]["name"] for v in top_holdings],
                values=[v[1]["weight"] * 100 for v in top_holdings],
                hole=0.4,
                marker=dict(colors=[COLOR_ACCENT, COLOR_UP, COLOR_DOWN,
                                    COLOR_WARN, "#8B5CF6", "#06B6D4",
                                    "#F59E0B", "#6B7280"]),
                textinfo="label+percent",
                textfont=dict(size=10),
            ))
            _lp = {k: v for k, v in PLOTLY_LAYOUT.items()
                   if k not in ("height",)}
            fig_pie.update_layout(**_lp, height=280, title="持仓仓位分布")
            st.plotly_chart(fig_pie, use_container_width=True)

    with chart_right:
        # 个股盈亏条形图
        if pnl_data:
            sorted_pnl = sorted(pnl_data.items(),
                                key=lambda x: x[1]["pnl"], reverse=True)
            names = [v["name"] for _, v in sorted_pnl[:10]]
            pnls = [v["pnl"] for _, v in sorted_pnl[:10]]
            bar_colors = [COLOR_UP if p >= 0 else COLOR_DOWN for p in pnls]

            fig_bar = go.Figure(go.Bar(
                x=names, y=pnls, marker_color=bar_colors,
                text=[f"¥{p:+,.0f}" for p in pnls],
                textposition="outside",
            ))
            _lp2 = {k: v for k, v in PLOTLY_LAYOUT.items()
                    if k not in ("xaxis", "yaxis", "height")}
            fig_bar.update_layout(
                **_lp2, height=280, title="个股浮动盈亏",
                xaxis=dict(tickangle=30),
                yaxis=dict(title="盈亏 (¥)", gridcolor="#E8ECF0"),
                showlegend=False,
            )
            st.plotly_chart(fig_bar, use_container_width=True)

    st.markdown("---")

    # ============================================================
    # 5. 预警历史时间线
    # ============================================================
    st.subheader("📜 预警历史")

    alert_history = dl.load_alert_history()
    if alert_history:
        recent = alert_history[-20:] if len(alert_history) > 20 else alert_history

        # 预警统计图
        levels_count = {"critical": 0, "warning": 0, "info": 0}
        for alert in recent:
            if isinstance(alert, dict):
                lv = alert.get("level", "info")
                levels_count[lv] = levels_count.get(lv, 0) + 1

        ac1, ac2, ac3 = st.columns(3)
        ac1.metric("🔴 严重", levels_count.get("critical", 0))
        ac2.metric("🟡 警告", levels_count.get("warning", 0))
        ac3.metric("🔵 信息", levels_count.get("info", 0))

        st.markdown("---")

        # 预警时间线
        for alert in reversed(recent):
            if isinstance(alert, dict):
                date = alert.get("date", "")
                lv = alert.get("level", "info")
                msg = alert.get("message", alert.get("type", ""))
                emoji = "🔴" if lv == "critical" else ("🟡" if lv == "warning" else "🔵")
                st.caption(f"{emoji} **{date}** - {msg}")
    else:
        st.info("暂无预警历史记录。盘中预警触发后自动记录。")

    st.markdown("---")

    # ============================================================
    # 6. 熔断状态面板 (Sprint 1)
    # ============================================================
    st.subheader("🚨 熔断状态")

    risk_cfg = dl.load_config_risk_params()
    total_cost = sum(v["cost_total"] for v in pnl_data.values())
    total_val = sum(v["value"] for v in pnl_data.values())
    day_pnl_pct = ((total_val - total_cost) / total_cost * 100) if total_cost > 0 else 0

    daily_l1 = risk_cfg.get("DAILY_LOSS_LIMIT_1", 0.02) * 100
    daily_l2 = risk_cfg.get("DAILY_LOSS_LIMIT_2", 0.03) * 100
    weekly_limit = risk_cfg.get("WEEKLY_LOSS_LIMIT", 0.08) * 100

    fb1, fb2, fb3 = st.columns(3)
    with fb1:
        daily_progress = min(abs(day_pnl_pct) / daily_l1 * 100, 100) if daily_l1 > 0 else 0
        st.markdown(f"**日度熔断L1** (停止买入)")
        st.markdown(f"阈值: {daily_l1:.1f}% | 当前: {day_pnl_pct:+.2f}%")
        st.progress(min(int(daily_progress), 100))
    with fb2:
        daily2_progress = min(abs(day_pnl_pct) / daily_l2 * 100, 100) if daily_l2 > 0 else 0
        st.markdown(f"**日度熔断L2** (清弱势仓)")
        st.markdown(f"阈值: {daily_l2:.1f}% | 当前: {day_pnl_pct:+.2f}%")
        st.progress(min(int(daily2_progress), 100))
    with fb3:
        st.markdown(f"**周度熔断** (降至3成)")
        st.markdown(f"阈值: {weekly_limit:.1f}%")
        st.progress(0)

    # 连亏监控
    st.markdown("---")
    st.subheader("📉 连续亏损监控")

    consec = risk_state.get("consec_losses", 0)
    consec_pause = risk_cfg.get("consec_loss_pause", 3)
    consec_halt = risk_cfg.get("consec_loss_halt", 5)

    lc1, lc2, lc3 = st.columns(3)
    lc1.metric("当前连亏", f"{consec}笔")
    lc2.metric("暂停阈值", f"{consec_pause}笔")
    lc3.metric("熔断阈值", f"{consec_halt}笔")

    if consec >= consec_halt:
        st.error(f"🔴 **已触发交易熔断**！连续亏损{consec}笔 >= {consec_halt}笔，暂停所有交易")
    elif consec >= consec_pause:
        st.warning(f"🟡 **已触发暂停**！连续亏损{consec}笔 >= {consec_pause}笔，暂停新开仓")
    elif consec > 0:
        st.info(f"🟢 当前连亏{consec}笔，距暂停还差{consec_pause - consec}笔")
    else:
        st.success("✅ 无连续亏损")

    st.markdown("---")

    # ============================================================
    # 7. 风控参数联动 (Sprint 1: config.py 参数展示)
    # ============================================================
    with st.expander("📋 风控参数配置 (来自 config.py)"):
        param_rows = [
            {"参数": "最大持仓数", "值": risk_cfg.get("MAX_HOLDINGS", "N/A"), "说明": "超过禁止买入"},
            {"参数": "单只股票上限", "值": f"{risk_cfg.get('MAX_SINGLE_STOCK_RATIO', 0.15):.0%}", "说明": "个股仓位硬限制"},
            {"参数": "单只ETF上限", "值": f"{risk_cfg.get('MAX_SINGLE_ETF_RATIO', 0.20):.0%}", "说明": "ETF仓位硬限制"},
            {"参数": "赛道上限", "值": f"{risk_cfg.get('SECTOR_MAX_RATIO', 0.40):.0%}", "说明": "单一赛道集中度"},
            {"参数": "满仓阈值", "值": f"{risk_cfg.get('FULL_POSITION_THRESHOLD', 0.90):.0%}", "说明": "超过绝对禁止买入"},
            {"参数": "初始止损", "值": f"{risk_cfg.get('INITIAL_STOP_LOSS_PCT', 0.12):.0%}", "说明": "买入价下方止损幅度"},
            {"参数": "日度L1", "值": f"{risk_cfg.get('DAILY_LOSS_LIMIT_1', 0.02):.1%}", "说明": "停止买入"},
            {"参数": "日度L2", "值": f"{risk_cfg.get('DAILY_LOSS_LIMIT_2', 0.03):.1%}", "说明": "清弱势仓"},
            {"参数": "周度熔断", "值": f"{risk_cfg.get('WEEKLY_LOSS_LIMIT', 0.08):.0%}", "说明": "降至3成仓"},
        ]
        st.dataframe(pd.DataFrame(param_rows), use_container_width=True, hide_index=True)
