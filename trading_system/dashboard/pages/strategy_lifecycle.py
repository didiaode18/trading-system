# -*- coding: utf-8 -*-
"""
策略生命周期看板
================
策略阶段状态 / 参数演化 / 性能趋势
"""

import os
import sys
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from dashboard import data_loader as dl
from dashboard.theme import (
    PLOTLY_LAYOUT, PLOTLY_LAYOUT_TALL, COLOR_UP, COLOR_DOWN, COLOR_ACCENT,
    COLOR_WARN, status_emoji,
)

# 策略阶段定义
STAGES = {
    "research": ("🔬 研究阶段", "因子挖掘/策略构思"),
    "backtest": ("📊 回测验证", "历史数据验证"),
    "paper_trading": ("📝 模拟交易", "实时模拟验证"),
    "live": ("🚀 实盘运行", "真实资金交易"),
    "monitoring": ("👁️ 监控中", "性能监控/风控"),
    "deprecated": ("⚰️ 已废弃", "策略下线"),
}


def render(date_range=None):
    st.header("🔄 策略生命周期")

    # 加载生命周期数据
    lifecycle = dl.load_lifecycle()
    
    if not lifecycle:
        st.info("暂无策略生命周期数据")
        st.caption("策略上线后自动记录生命周期状态")

        st.markdown("---")
        st.markdown("#### 💡 策略生命周期是什么？")
        st.markdown("""
策略生命周期跟踪每个交易策略从研究到废弃的完整过程：

| 阶段 | 说明 |
|------|------|
| 🔬 研究阶段 | 因子挖掘、策略构思、初步验证 |
| 📊 回测验证 | 历史数据回测、参数优化 |
| 📝 模拟交易 | 实时模拟盘验证、风控测试 |
| 🚀 实盘运行 | 真实资金交易、绩效监控 |
| 👁️ 监控中 | 性能下降监控、策略失效检测 |
| ⚰️ 已废弃 | IC衰减、策略下线 |
""")

        # ============================================================
        # Sprint 2: 绩效仪表盘 (从买点信号和预测历史统计)
        # ============================================================
        st.markdown("---")
        st.subheader("📊 策略绩效仪表盘")

        # 买点信号统计
        buy_signals = dl.load_buy_signals()
        if buy_signals:
            settled = [s for s in buy_signals if s.get("settled", False)]
            if settled:
                wins = sum(1 for s in settled if (s.get("r5") or 0) > 0)
                total = len(settled)
                win_rate = wins / total if total > 0 else 0
                r5_values = [(s.get("r5") or 0) for s in settled]
                avg_r5 = sum(r5_values) / total if total > 0 else 0

                pc1, pc2, pc3, pc4 = st.columns(4)
                pc1.metric("买点信号总数", len(buy_signals))
                pc2.metric("已结算", total)
                pc3.metric("5日胜率", f"{win_rate:.1%}")
                pc4.metric("平均5日收益", f"{avg_r5:+.2f}%")

                # 按档位统计
                tiers = {}
                for s in settled:
                    t = s.get("tier", "未知")
                    if t not in tiers:
                        tiers[t] = {"total": 0, "wins": 0, "r5_sum": 0}
                    tiers[t]["total"] += 1
                    r5_val = s.get("r5") or 0
                    if r5_val > 0:
                        tiers[t]["wins"] += 1
                    tiers[t]["r5_sum"] += r5_val

                if tiers:
                    st.markdown("**按档位统计:**")
                    tier_rows = []
                    for t, d in tiers.items():
                        wr = d["wins"] / d["total"] if d["total"] > 0 else 0
                        avg = d["r5_sum"] / d["total"] if d["total"] > 0 else 0
                        tier_rows.append({"档位": t, "信号数": d["total"],
                                           "胜率": f"{wr:.1%}", "平均5日收益": f"{avg:+.2f}%"})
                    st.dataframe(pd.DataFrame(tier_rows), use_container_width=True, hide_index=True)
            else:
                st.info(f"共{len(buy_signals)}条买点信号，均未结算")
        else:
            st.caption("暂无买点信号数据")

        # 预测历史统计
        predictions = dl.load_prediction_history()
        if predictions:
            st.markdown("---")
            st.markdown("#### 🔮 预测命中率统计")
            verified = [p for p in predictions if p.get("verified", False)]
            if verified:
                correct = sum(1 for p in verified if p.get("is_correct", False))
                hit_rate = correct / len(verified) if verified else 0
                avg_conf = sum((p.get("confidence") or 0) for p in predictions) / len(predictions)

                pm1, pm2, pm3 = st.columns(3)
                pm1.metric("总预测数", len(predictions))
                pm2.metric("已验证", len(verified))
                pm3.metric("命中率", f"{hit_rate:.1%}")
            else:
                st.caption(f"共{len(predictions)}条预测，均未验证")

        # 已部署策略模块
        st.markdown("---")
        st.markdown("#### 📁 已部署策略模块")
        strategy_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "strategy"
        )
        if os.path.exists(strategy_dir):
            strategy_files = sorted([
                f for f in os.listdir(strategy_dir)
                if f.endswith(".py") and not f.startswith("__")
            ])
            if strategy_files:
                rows = [{"策略文件": f, "状态": "📁 已部署"}
                        for f in strategy_files[:15]]
                st.dataframe(pd.DataFrame(rows), use_container_width=True,
                            hide_index=True)
                st.caption(f"共 {len(strategy_files)} 个策略模块")
        return

    # ============================================================
    # 1. 策略列表
    # ============================================================
    strategies = lifecycle.get("strategies", [])
    
    if not strategies:
        st.warning("无策略记录")
        return

    # 策略选择
    strategy_names = [s.get("name", f"策略{i}") for i, s in enumerate(strategies)]
    selected = st.selectbox("选择策略", strategy_names)
    
    if not selected:
        return

    strategy_idx = strategy_names.index(selected)
    strategy = strategies[strategy_idx]

    # ============================================================
    # 2. 当前阶段展示
    # ============================================================
    st.subheader("当前阶段")
    
    current_stage = strategy.get("stage", "research")
    stage_info = STAGES.get(current_stage, ("❓ 未知", "状态未知"))
    
    col1, col2, col3 = st.columns([2, 1, 1])
    
    with col1:
        st.markdown(f"### {stage_info[0]}")
        st.caption(stage_info[1])
    
    with col2:
        updated_at = strategy.get("updated_at", "N/A")
        st.metric("最后更新", updated_at[:10] if len(updated_at) >= 10 else updated_at)
    
    with col3:
        version = strategy.get("version", "v1.0")
        st.metric("版本", version)

    # ============================================================
    # 3. 生命周期时间线
    # ============================================================
    st.subheader("生命周期时间线")
    
    history = strategy.get("history", [])
    if history:
        df_history = pd.DataFrame(history)
        
        fig = go.Figure()
        
        # 绘制阶段变化
        for i, row in df_history.iterrows():
            stage = row.get("stage", "")
            stage_label = STAGES.get(stage, (stage, ""))[0]
            
            fig.add_trace(go.Scatter(
                x=[row.get("date", "")],
                y=[i],
                mode="markers+text",
                marker=dict(size=15, color=COLOR_ACCENT),
                text=[stage_label],
                textposition="top center",
                name=stage_label,
            ))
        
        fig.update_layout(
            **PLOTLY_LAYOUT_TALL,
            title="策略阶段演化",
            xaxis_title="日期",
            yaxis=dict(visible=False),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("无历史记录")

    # ============================================================
    # 4. 参数配置
    # ============================================================
    st.subheader("参数配置")
    
    params = strategy.get("params", {})
    if params:
        st.json(params)
    else:
        st.info("无参数配置")

    # ============================================================
    # 5. 性能指标
    # ============================================================
    st.subheader("性能指标")
    
    performance = strategy.get("performance", {})
    if performance:
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            total_return = performance.get("total_return", 0)
            st.metric("总收益", f"{total_return:.2%}")
        
        with col2:
            sharpe = performance.get("sharpe_ratio", 0)
            st.metric("夏普比率", f"{sharpe:.2f}")
        
        with col3:
            max_dd = performance.get("max_drawdown", 0)
            st.metric("最大回撤", f"{max_dd:.2%}")
        
        with col4:
            win_rate = performance.get("win_rate", 0)
            st.metric("胜率", f"{win_rate:.1%}")
    else:
        st.info("无性能数据")

    # ============================================================
    # 6. 备注/日志
    # ============================================================
    st.subheader("备注/日志")
    
    notes = strategy.get("notes", [])
    if notes:
        for note in notes[-10:]:  # 最近10条
            st.markdown(f"- {note}")
    else:
        st.info("无备注")

    # ============================================================
    # 7. 跳转到回测分析
    # ============================================================
    st.markdown("---")
    if st.button("📈 查看回测详情", use_container_width=True):
        st.session_state["jump_page"] = "backtest_analysis"
        st.rerun()
