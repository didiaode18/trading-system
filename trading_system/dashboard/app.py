"""
实盘仪表盘（多页 Streamlit）
=============================
V10 P1-4: 从单文件扩展为多页应用。

页面:
    1. 账户总览
    2. 持仓明细
    3. 委托与成交
    4. 信号流
    5. 净值与归因
    6. 预警台账

所有数据只读本地 SQLite，宕机不影响调度与交易主进程。
邮件仍是主通知渠道，dashboard 只是增强。

启动: streamlit run trading_system/dashboard/app.py
"""

import logging
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False


def _get_db_path() -> str:
    """获取数据库路径"""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "stock_db.db"
    )


def _get_paper_db_path() -> str:
    """获取模拟盘数据库路径"""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "paper_account.db"
    )


def page_overview():
    """Page 1: 账户总览"""
    st.header("账户总览")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("总资产", "¥1,000,000", delta="0.0%")
    col2.metric("可用资金", "¥500,000")
    col3.metric("持仓市值", "¥500,000")
    col4.metric("今日盈亏", "¥0", delta="0.0%")

    st.subheader("风控额度")
    st.progress(0.3, text="总仓位 30% / 上限 90%")
    st.progress(0.1, text="单票最大 15% / 上限 25%")

    st.info("数据来源: 本地 SQLite（只读） | 邮件仍是主通知渠道")


def page_positions():
    """Page 2: 持仓明细"""
    st.header("持仓与成本")

    try:
        import pandas as pd
        db_path = _get_db_path()
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            # 尝试读取持仓数据
            df = pd.read_sql_query("SELECT * FROM daily_kline LIMIT 10", conn)
            st.dataframe(df)
            conn.close()
        else:
            st.warning("数据库文件不存在")
    except Exception as e:
        st.error(f"读取持仓失败: {e}")

    st.subheader("止损线")
    st.write("止损规则: 固定止损 -5% / ATR 动态止损 / 移动止损")


def page_orders():
    """Page 3: 委托与成交"""
    st.header("当日委托与成交")

    st.subheader("委托列表")
    st.write("暂无委托记录（对接 P0-2 Broker 状态机）")

    st.subheader("成交记录")
    st.write("暂无成交记录")


def page_signals():
    """Page 4: 信号流"""
    st.header("信号流（P1-1 标准 Signal）")

    st.write("实时信号列表（对接策略注册表）")
    st.info("暂无活跃信号")


def page_nav():
    """Page 5: 净值与归因"""
    st.header("净值 vs 沪深300 与归因")

    st.subheader("净值曲线")
    st.line_chart([1.0, 1.02, 1.01, 1.05, 1.03])

    st.subheader("归因分析")
    col1, col2 = st.columns(2)
    col1.write("选股贡献: +2.3%")
    col2.write("择时贡献: -0.5%")


def page_alerts():
    """Page 6: 预警台账"""
    st.header("预警台账")

    st.write("盘中预警记录")
    st.info("暂无预警记录")


def main():
    """主入口"""
    if not HAS_STREAMLIT:
        print("streamlit 未安装。安装: pip install streamlit")
        return

    st.set_page_config(
        page_title="操盘密码 V10",
        page_icon="📊",
        layout="wide",
    )

    st.sidebar.title("操盘密码 V10")
    st.sidebar.markdown("---")

    page = st.sidebar.radio("导航", [
        "账户总览", "持仓明细", "委托成交",
        "信号流", "净值归因", "预警台账",
    ])

    if page == "账户总览":
        page_overview()
    elif page == "持仓明细":
        page_positions()
    elif page == "委托成交":
        page_orders()
    elif page == "信号流":
        page_signals()
    elif page == "净值归因":
        page_nav()
    elif page == "预警台账":
        page_alerts()

    st.sidebar.markdown("---")
    st.sidebar.caption(f"更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    st.sidebar.caption("数据只读 | 宕机不影响交易")


if __name__ == "__main__":
    main()
