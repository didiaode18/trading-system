# -*- coding: utf-8 -*-
"""
数据中心 V8.0
=============
Sprint 3 升级:
- 数据链路状态 (各数据源连接/新鲜度)
- 任务调度状态 (心跳/最近执行)
- 异常日志摘要 (最近错误/警告)
- 数据库统计 + 数据质量检查
"""

import os
import sys
import re
import datetime
import glob
import streamlit as st
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dashboard import data_loader as dl
from dashboard.theme import PLOTLY_LAYOUT


def render(date_range=None):
    st.header("🗄️ 数据中心 & 系统健康")

    # ============================================================
    # 1. 数据链路状态 (Sprint 3)
    # ============================================================
    st.subheader("🔗 数据链路状态")

    data_dir = dl.DATA_DIR
    today_str = datetime.date.today().strftime("%Y%m%d")

    # 检查关键数据文件新鲜度
    freshness_checks = [
        ("持仓数据", os.path.join(os.path.dirname(dl.HOLDINGS_PATH), "holdings.json")),
        ("市场情绪", os.path.join(data_dir, "market_pulse_cache.json")),
        ("风控状态", os.path.join(data_dir, "risk_state.json")),
        ("盘中状态", os.path.join(data_dir, "intraday_state.json")),
        ("买点信号", os.path.join(data_dir, "buy_signal_history.json")),
        ("IC历史", os.path.join(data_dir, "ic_history.json")),
        ("选股缓存", os.path.join(data_dir, "scan_cache.json")),
        ("美股赛道", os.path.join(data_dir, "us_sector_cache.json")),
    ]

    link_rows = []
    for name, path in freshness_checks:
        if os.path.exists(path):
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
            age_hours = (datetime.datetime.now() - mtime).total_seconds() / 3600
            size = os.path.getsize(path)
            if age_hours < 2:
                status = "🟢 新鲜"
            elif age_hours < 24:
                status = "🟡 正常"
            elif age_hours < 72:
                status = "🟠 陈旧"
            else:
                status = "🔴 过期"
            link_rows.append({
                "数据源": name,
                "状态": status,
                "最后更新": mtime.strftime("%m-%d %H:%M"),
                "距今": f"{age_hours:.1f}h",
                "大小": f"{size/1024:.1f}KB",
            })
        else:
            link_rows.append({
                "数据源": name,
                "状态": "⚫ 不存在",
                "最后更新": "-",
                "距今": "-",
                "大小": "-",
            })

    st.dataframe(pd.DataFrame(link_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ============================================================
    # 2. 任务调度状态 (Sprint 3)
    # ============================================================
    st.subheader("⏰ 任务调度状态")

    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    scheduler_log = os.path.join(logs_dir, f"scheduler_{today_str}.log")

    if os.path.exists(scheduler_log):
        # 解析最近的调度任务执行记录
        recent_tasks = _parse_scheduler_log(scheduler_log)
        if recent_tasks:
            task_df = pd.DataFrame(recent_tasks).head(20)
            st.dataframe(task_df, use_container_width=True, hide_index=True)
        else:
            st.info("今日调度日志中无任务执行记录")

        # 日志文件大小
        log_size = os.path.getsize(scheduler_log)
        st.caption(f"调度日志: scheduler_{today_str}.log ({log_size/1024:.0f}KB)")
    else:
        st.warning(f"今日调度日志不存在: scheduler_{today_str}.log")
        # 检查最近的日志
        log_files = sorted(glob.glob(os.path.join(logs_dir, "scheduler_*.log")),
                           key=os.path.getmtime, reverse=True)[:3]
        if log_files:
            st.caption(f"最近日志: {', '.join(os.path.basename(f) for f in log_files)}")

    st.markdown("---")

    # ============================================================
    # 3. 异常日志摘要 (Sprint 3)
    # ============================================================
    st.subheader("🚨 异常日志摘要")

    if os.path.exists(scheduler_log):
        errors, warnings = _extract_errors_warnings(scheduler_log)

        ec1, ec2 = st.columns(2)
        ec1.metric("错误 (ERROR)", len(errors))
        ec2.metric("警告 (WARNING)", len(warnings))

        if errors:
            st.markdown("**最近错误:**")
            for err in errors[-5:]:
                st.error(f"`{err}`")
        else:
            st.success("✅ 今日无ERROR级别日志")

        if warnings:
            with st.expander(f"查看最近警告 ({len(warnings)}条)"):
                for warn in warnings[-10:]:
                    st.warning(f"`{warn}`")
    else:
        st.info("无今日调度日志")

    st.markdown("---")

    # ============================================================
    # 4. 数据库统计
    # ============================================================
    st.subheader("📊 数据库统计")

    stats = dl.load_data_stats()
    if stats:
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            kline_stats = stats.get("daily_kline", {})
            st.metric("日K线标的数", kline_stats.get("codes", 0))
            st.caption(f"最新: {kline_stats.get('latest', 'N/A')}")
        with col2:
            fundamental_stats = stats.get("fundamental", {})
            st.metric("基本面标的数", fundamental_stats.get("codes", 0))
            st.caption(f"最新: {fundamental_stats.get('latest', 'N/A')}")
        with col3:
            industry_stats = stats.get("stock_industry", {})
            st.metric("行业分类数", industry_stats.get("codes", 0))
        with col4:
            turnover_rows = stats.get("turnover_rows", 0)
            st.metric("换手率记录", f"{turnover_rows:,}")

        # 各表记录数
        db_path = os.path.join(dl.DATA_DIR, "stock_db.db")
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            st.metric("数据库大小", f"{db_size / 1024 / 1024:.1f} MB")

            table_data = []
            for table_name, table_stats in stats.items():
                if table_name != "turnover_rows":
                    table_data.append({
                        "表名": table_name,
                        "标的数": table_stats.get("codes", 0),
                        "最新日期": table_stats.get("latest", "N/A"),
                    })
            if table_data:
                st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)
    else:
        st.warning("无法获取数据库统计信息")

    st.markdown("---")

    # ============================================================
    # 5. 数据质量检查
    # ============================================================
    st.subheader("🔍 数据质量检查")

    files_to_check = {
        "holdings.json": dl.HOLDINGS_PATH,
        "ic_history.json": dl.IC_HISTORY_PATH,
        "strategy_lifecycle.json": dl.LIFECYCLE_PATH,
    }

    col1, col2, col3 = st.columns(3)
    for i, (name, path) in enumerate(files_to_check.items()):
        col = [col1, col2, col3][i]
        with col:
            if os.path.exists(path):
                size = os.path.getsize(path)
                if size > 0:
                    st.success(f"✅ {name}")
                else:
                    st.warning(f"🟡 {name} (空文件)")
                st.caption(f"大小: {size:,} bytes")
            else:
                st.error(f"❌ {name}")
                st.caption("文件不存在")

    # 数据更新建议
    recommendations = []
    if stats:
        fundamental_codes = stats.get("fundamental", {}).get("codes", 0)
        if fundamental_codes < 100:
            recommendations.append("⚠️ 基本面数据覆盖率较低")
        industry_codes = stats.get("stock_industry", {}).get("codes", 0)
        if industry_codes < 50:
            recommendations.append("⚠️ 行业分类数据不足")
        turnover_rows = stats.get("turnover_rows", 0)
        if turnover_rows < 10000:
            recommendations.append("⚠️ 换手率数据较少")

    if recommendations:
        for rec in recommendations:
            st.markdown(rec)
    else:
        st.success("✅ 数据状态良好")

    # ============================================================
    # 6. 数据路径参考
    # ============================================================
    st.subheader("📂 数据路径参考")

    path_rows = [
        {"用途": "数据目录", "路径": dl.DATA_DIR.replace(os.sep, "/")},
        {"用途": "持仓文件", "路径": dl.HOLDINGS_PATH.replace(os.sep, "/")},
        {"用途": "IC历史", "路径": dl.IC_HISTORY_PATH.replace(os.sep, "/")},
        {"用途": "策略生命周期", "路径": dl.LIFECYCLE_PATH.replace(os.sep, "/")},
        {"用途": "数据库", "路径": os.path.join(dl.DATA_DIR, "stock_db.db").replace(os.sep, "/")},
        {"用途": "日志目录", "路径": logs_dir.replace(os.sep, "/")},
    ]
    st.table(pd.DataFrame(path_rows))


def _parse_scheduler_log(log_path: str) -> list:
    """解析调度器日志，提取最近的任务执行记录"""
    tasks = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        # 从后往前找最近的任务执行记录
        task_pattern = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?(?:执行|开始|完成|任务|task|run)',
                                   re.IGNORECASE)
        recent_lines = lines[-500:] if len(lines) > 500 else lines

        for line in recent_lines:
            m = task_pattern.search(line)
            if m:
                time_str = m.group(1)
                # 提取任务名（简化处理）
                task_name = line.strip()[:80]
                tasks.append({
                    "时间": time_str,
                    "任务": task_name,
                })

        # 去重并取最近20条
        seen = set()
        unique_tasks = []
        for t in reversed(tasks):
            key = t["时间"] + t["任务"][:30]
            if key not in seen:
                seen.add(key)
                unique_tasks.append(t)
            if len(unique_tasks) >= 20:
                break
        return list(reversed(unique_tasks))
    except Exception:
        return []


def _extract_errors_warnings(log_path: str) -> tuple:
    """从日志文件中提取ERROR和WARNING行"""
    errors = []
    warnings = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "ERROR" in line:
                    errors.append(line.strip()[:120])
                elif "WARNING" in line:
                    warnings.append(line.strip()[:120])
    except Exception:
        pass
    return errors, warnings
