"""
交易系统Web仪表盘（V3.2新增）
==============================
基于Streamlit的本地可视化面板

功能:
  1. 持仓总览（市值/浮盈亏/仓位占比热力图）
  2. 收益曲线（组合/基准对比）
  3. 风控状态（17道关卡/策略失效/熔断状态）
  4. 信号历史（最近选股/买卖信号）
  5. 策略生命周期状态
  6. 大盘状态+推荐策略

启动方式:
  streamlit run trading_system/dashboard/app.py
  或: python -m streamlit run trading_system/dashboard/app.py
"""

import os
import sys
import json
import datetime

# 确保能导入trading_system
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    import streamlit as st
    import pandas as pd
    import numpy as np
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

import config

# 数据路径
DATA_DIR = config.DATA_DIR
HOLDINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             "holdings.json")
LIFECYCLE_PATH = os.path.join(DATA_DIR, "strategy_lifecycle.json")
IC_HISTORY_PATH = os.path.join(DATA_DIR, "ic_history.json")
PNL_DB_PATH = os.path.join(DATA_DIR, "pnl_history.db")


def load_holdings() -> dict:
    """加载持仓数据"""
    if os.path.exists(HOLDINGS_PATH):
        with open(HOLDINGS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def load_lifecycle() -> dict:
    """加载策略生命周期"""
    if os.path.exists(LIFECYCLE_PATH):
        with open(LIFECYCLE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def load_ic_history() -> dict:
    """加载IC历史"""
    if os.path.exists(IC_HISTORY_PATH):
        with open(IC_HISTORY_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def main():
    if not HAS_STREAMLIT:
        print("错误: 需要安装streamlit")
        print("  pip install streamlit")
        return

    st.set_page_config(
        page_title="量化交易系统 V3.2",
        page_icon="📈",
        layout="wide",
    )

    st.title("📈 量化交易系统仪表盘 V3.2")
    st.caption(f"数据时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # 侧边栏导航
    page = st.sidebar.selectbox("导航", [
        "📊 持仓总览",
        "📈 收益追踪",
        "🛡️ 风控状态",
        "🎯 信号历史",
        "🔄 策略管理",
        "🌐 大盘状态",
    ])

    if page == "📊 持仓总览":
        render_holdings()
    elif page == "📈 收益追踪":
        render_pnl()
    elif page == "🛡️ 风控状态":
        render_risk()
    elif page == "🎯 信号历史":
        render_signals()
    elif page == "🔄 策略管理":
        render_lifecycle()
    elif page == "🌐 大盘状态":
        render_market()


def render_holdings():
    """持仓总览页"""
    st.header("📊 持仓总览")
    holdings = load_holdings()

    if not holdings:
        st.warning("无持仓数据")
        return

    # 持仓表格
    rows = []
    total_value = 0
    total_cost = 0
    for code, pos in holdings.items():
        shares = pos.get("shares", 0)
        cost = pos.get("cost", pos.get("buy_price", 0))
        price = pos.get("price", cost)
        value = shares * price
        cost_total = shares * cost
        pnl = value - cost_total
        pnl_pct = (pnl / cost_total * 100) if cost_total > 0 else 0
        total_value += value
        total_cost += cost_total
        rows.append({
            "代码": code,
            "名称": pos.get("name", code),
            "股数": shares,
            "成本": f"{cost:.2f}",
            "现价": f"{price:.2f}",
            "市值": f"{value:,.0f}",
            "盈亏": f"{pnl:+,.0f}",
            "盈亏%": f"{pnl_pct:+.1f}%",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)

    # 汇总指标
    col1, col2, col3, col4 = st.columns(4)
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    col1.metric("总市值", f"¥{total_value:,.0f}")
    col2.metric("总成本", f"¥{total_cost:,.0f}")
    col3.metric("总盈亏", f"¥{total_pnl:+,.0f}", f"{total_pnl_pct:+.1f}%")
    col4.metric("持仓数", f"{len(holdings)}只")


def render_pnl():
    """收益追踪页"""
    st.header("📈 收益追踪")

    if not os.path.exists(PNL_DB_PATH):
        st.info("暂无历史P&L数据，运行实盘追踪后自动生成")
        return

    try:
        import sqlite3
        conn = sqlite3.connect(PNL_DB_PATH)
        df = pd.read_sql("SELECT * FROM daily_pnl ORDER BY date DESC LIMIT 60", conn)
        conn.close()

        if df.empty:
            st.info("暂无数据")
            return

        # 收益曲线
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        st.line_chart(df.set_index("date")[["total_pnl_pct"]])

        # 最近数据
        st.dataframe(df.tail(10), use_container_width=True)
    except Exception as e:
        st.error(f"读取P&L数据失败: {e}")


def render_risk():
    """风控状态页"""
    st.header("🛡️ 风控状态")

    # 策略失效检测器状态
    risk_state_path = os.path.join(DATA_DIR, "risk_state.json")
    if os.path.exists(risk_state_path):
        with open(risk_state_path, 'r', encoding='utf-8') as f:
            risk_state = json.load(f)

        level = risk_state.get("current_level", "normal")
        level_colors = {"normal": "🟢", "degrade": "🟡", "pause": "🟠", "breaker": "🔴"}
        st.metric("策略失效等级", f"{level_colors.get(level, '⚪')} {level}")

        col1, col2, col3 = st.columns(3)
        col1.metric("滚动胜率", f"{risk_state.get('win_rate', 0):.0%}")
        col2.metric("连亏笔数", risk_state.get("consec_losses", 0))
        col3.metric("交易期望", f"{risk_state.get('expectancy', 0):.2%}")
    else:
        st.info("暂无风控状态数据")

    # 风控关卡说明
    st.subheader("风控关卡列表")
    gates = [
        "关卡-2: 黑天鹅检测", "关卡-1: 盘中风险标记", "第1关: 年度回撤",
        "第2关: 大盘方向", "第3关: 仓位上限", "第4关: 单只集中度",
        "第5关: 止损检查", "第6关: 连亏暂停", "第7关: 策略失效",
        "第8关: 信号衰减", "第9关: 反冲动锁", "第10关: 时间窗口",
        "第10.5关: Kelly仓位", "第11关: 组合回撤", "第12关: 赛道集中度",
    ]
    for g in gates:
        st.text(f"  ✅ {g}")


def render_signals():
    """信号历史页"""
    st.header("🎯 信号历史")

    # 读取最近选股结果
    stock_pool_path = os.path.join(os.path.dirname(HOLDINGS_PATH), "stock_pool.json")
    if os.path.exists(stock_pool_path):
        with open(stock_pool_path, 'r', encoding='utf-8') as f:
            pool = json.load(f)
        if pool:
            st.subheader("当前股票池")
            df = pd.DataFrame(pool[:10])
            st.dataframe(df, use_container_width=True)
    else:
        st.info("暂无选股数据")


def render_lifecycle():
    """策略管理页"""
    st.header("🔄 策略生命周期")
    lifecycle = load_lifecycle()

    if not lifecycle:
        st.info("暂无策略注册，运行 init_builtin_strategies() 初始化")
        return

    state_order = ["live", "paper", "backtest", "research", "retired", "rejected"]
    state_cn = {"live": "🟢 实盘", "paper": "🔵 模拟", "backtest": "🟡 回测",
                "research": "⚪ 研发", "retired": "⚫ 退役", "rejected": "🔴 否决"}

    for state in state_order:
        strategies = {k: v for k, v in lifecycle.items() if v.get("state") == state}
        if strategies:
            st.subheader(f"{state_cn.get(state, state)} ({len(strategies)}个)")
            for name, info in strategies.items():
                m = info.get("metrics", {})
                with st.expander(f"{name} - {info.get('description', '')}"):
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("夏普", f"{m.get('sharpe', 0):.2f}")
                    col2.metric("胜率", f"{m.get('win_rate', 0):.0%}")
                    col3.metric("回撤", f"{m.get('max_drawdown', 0):.0%}")
                    col4.metric("交易数", m.get("total_trades", 0))
                    st.text(f"状态变更: {info.get('state_changed_at', 'N/A')}")


def render_market():
    """大盘状态页"""
    st.header("🌐 大盘状态")

    # 尝试读取缓存的大盘状态
    market_cache_path = os.path.join(DATA_DIR, "market_state_cache.json")
    if os.path.exists(market_cache_path):
        with open(market_cache_path, 'r', encoding='utf-8') as f:
            market = json.load(f)

        state = market.get("state", "RANGE")
        state_emoji = {"BULL": "🐂", "RANGE": "⚖️", "BEAR": "🐻"}
        st.metric("市场状态", f"{state_emoji.get(state, '❓')} {market.get('state_cn', state)}")
        st.metric("置信度", f"{market.get('confidence', 0):.0%}")

        if "recommended_strategies" in market:
            st.subheader("推荐策略组合")
            for name, weight in market["recommended_strategies"].items():
                st.progress(weight, text=f"{name}: {weight:.0%}")
    else:
        st.info("暂无大盘状态缓存，运行scheduler后自动生成")

    # IC监控
    st.subheader("因子IC监控")
    ic_data = load_ic_history()
    if ic_data:
        for factor, records in ic_data.items():
            if records:
                recent = records[-5:] if len(records) >= 5 else records
                ics = [r['ic'] if isinstance(r, dict) else r for r in recent]
                avg_ic = sum(ics) / len(ics)
                status = "🟢" if avg_ic > 0.02 else ("🔴" if avg_ic < -0.02 else "🟡")
                st.text(f"  {status} {factor}: IC={avg_ic:.4f} (近{len(ics)}期)")
    else:
        st.info("暂无IC历史数据")


if __name__ == "__main__":
    main()
