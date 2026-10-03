# -*- coding: utf-8 -*-
"""
美股隔夜赛道涨幅
================
展示美股昨晚各板块涨跌幅，映射到A股持仓赛道，辅助盘前决策。

数据来源: akshare (美股板块ETF) + 本地缓存降级
缓存: trading_system/data/us_sector_cache.json (当日有效)
"""

import os
import sys
import json
import datetime
import logging

import pandas as pd
import numpy as np
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dashboard.theme import COLOR_UP, COLOR_DOWN, COLOR_ACCENT, PLOTLY_LAYOUT

logger = logging.getLogger(__name__)

# ============================================================
# 美股板块 → A股赛道映射
# ============================================================
SECTOR_MAPPING = {
    "半导体": {
        "us_etf": "SOXX",
        "us_name": "iShares Semiconductor",
        "a_sectors": ["半导体材料"],
        "a_stocks": ["562590 半导材料"],
        "icon": "🔬",
    },
    "科技": {
        "us_etf": "XLK",
        "us_name": "Technology Select",
        "a_sectors": ["软件", "消费电子", "金融科技"],
        "a_stocks": ["000158 常山北明", "002655 共达电声"],
        "icon": "💻",
    },
    "金融": {
        "us_etf": "XLF",
        "us_name": "Financial Select",
        "a_sectors": ["金融科技"],
        "a_stocks": ["000997 新大陆"],
        "icon": "🏦",
    },
    "能源": {
        "us_etf": "XLE",
        "us_name": "Energy Select",
        "a_sectors": ["能源", "能源化工"],
        "a_stocks": ["600256 广汇能源", "600989 宝丰能源"],
        "icon": "⛽",
    },
    "医药": {
        "us_etf": "XLV",
        "us_name": "Health Care Select",
        "a_sectors": ["医药"],
        "a_stocks": ["603259 XD药明康"],
        "icon": "💊",
    },
    "工业": {
        "us_etf": "XLI",
        "us_name": "Industrial Select",
        "a_sectors": ["军工", "电力设备"],
        "a_stocks": ["600760 中航沈飞", "002498 汉缆股份"],
        "icon": "🏭",
    },
    "航运": {
        "us_etf": "ARKB",
        "us_name": "ARK Blockchain",
        "a_sectors": ["航运"],
        "a_stocks": ["601919 中远海控"],
        "icon": "🚢",
    },
    "消费": {
        "us_etf": "XLP",
        "us_name": "Consumer Staples",
        "a_sectors": ["消费"],
        "a_stocks": [],
        "icon": "🛒",
    },
    "通信": {
        "us_etf": "XLC",
        "us_name": "Communication Svcs",
        "a_sectors": ["光通信"],
        "a_stocks": [],
        "icon": "📡",
    },
    "材料": {
        "us_etf": "XLB",
        "us_name": "Materials Select",
        "a_sectors": ["有色金属"],
        "a_stocks": ["512400 有色ETF"],
        "icon": "🧱",
    },
    "公用事业": {
        "us_etf": "XLU",
        "us_name": "Utilities Select",
        "a_sectors": [],
        "a_stocks": [],
        "icon": "⚡",
    },
    "地产": {
        "us_etf": "XLRE",
        "us_name": "Real Estate Select",
        "a_sectors": [],
        "a_stocks": [],
        "icon": "🏠",
    },
}

CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "us_sector_cache.json"
)


# ============================================================
# 数据获取
# ============================================================

def _fetch_us_sector_data() -> dict:
    """
    获取美股板块ETF昨日涨跌幅

    返回: {sector_name: {"change_pct": float, "close": float, "date": str}}
    """
    # 1. 检查缓存（当日有效）
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("date") == today_str:
                return cache["data"]
        except Exception:
            pass

    # 2. 尝试 akshare 获取
    data = _fetch_via_akshare()
    if data:
        _save_cache(today_str, data)
        return data

    # 3. 降级: 返回空
    return {}


def _fetch_via_akshare() -> dict:
    """通过 akshare 获取美股板块数据"""
    try:
        import akshare as ak

        result = {}
        # 获取各板块ETF最新行情
        etf_list = {name: info["us_etf"] for name, info in SECTOR_MAPPING.items()}

        for sector_name, ticker in etf_list.items():
            try:
                # 使用 akshare 获取美股ETF历史数据
                df = ak.stock_us_daily(symbol=ticker, adjust="qfq")
                if df is not None and len(df) >= 2:
                    df = df.sort_values("date").tail(5)
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]
                    close = float(latest["close"])
                    prev_close = float(prev["close"])
                    change_pct = (close - prev_close) / prev_close * 100
                    result[sector_name] = {
                        "change_pct": round(change_pct, 2),
                        "close": round(close, 2),
                        "date": str(latest["date"])[:10],
                    }
            except Exception as e:
                logger.debug(f"akshare获取{ticker}失败: {e}")
                continue

        return result if result else {}

    except ImportError:
        logger.warning("akshare未安装，无法获取美股板块数据")
        return {}
    except Exception as e:
        logger.warning(f"akshare获取美股板块数据异常: {e}")
        return {}


def _save_cache(date_str: str, data: dict):
    """缓存当日数据"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": date_str, "data": data}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug(f"缓存写入失败: {e}")


# ============================================================
# 可视化渲染
# ============================================================

def render_us_sector_overnight():
    """渲染美股隔夜赛道涨幅模块"""
    st.subheader("🌙 美股隔夜赛道涨幅")

    with st.spinner("正在获取美股板块数据..."):
        sector_data = _fetch_us_sector_data()

    if not sector_data:
        st.info("暂无美股隔夜数据。可能原因：非交易日 / 网络异常 / akshare接口变动。"
                "\n\n数据恢复后自动展示。缓存文件: `data/us_sector_cache.json`")
        return

    # 按涨跌幅排序
    sorted_sectors = sorted(sector_data.items(), key=lambda x: x[1]["change_pct"], reverse=True)
    trade_date = sector_data.get(sorted_sectors[0][0], {}).get("date", "未知")

    st.caption(f"数据日期: {trade_date} (美东时间) | 来源: akshare")

    # ---- 柱状图 ----
    names = [f"{SECTOR_MAPPING.get(n, {}).get('icon', '📊')} {n}" for n, _ in sorted_sectors]
    changes = [v["change_pct"] for _, v in sorted_sectors]
    colors = [COLOR_UP if c >= 0 else COLOR_DOWN for c in changes]

    fig = go.Figure(go.Bar(
        x=names,
        y=changes,
        marker_color=colors,
        text=[f"{c:+.2f}%" for c in changes],
        textposition="outside",
        textfont=dict(size=11, color="#333333"),
        hovertemplate="%{x}<br>涨跌幅: %{y:+.2f}%<extra></extra>",
    ))

    # 过滤PLOTLY_LAYOUT中会被显式覆盖的键，避免重复参数
    _skip = {"legend", "xaxis", "yaxis", "height", "margin", "title", "showlegend"}
    layout = {k: v for k, v in PLOTLY_LAYOUT.items() if k not in _skip}
    fig.update_layout(
        **layout,
        height=320,
        title=f"美股板块涨跌幅 ({trade_date})",
        xaxis=dict(gridcolor="#E8ECF0", zerolinecolor="#D0D5DD"),
        yaxis=dict(title="涨跌幅 (%)", gridcolor="#E8ECF0", zerolinecolor="#999999", zerolinewidth=1),
        showlegend=False,
        margin=dict(l=40, r=20, t=50, b=60),
    )
    st.plotly_chart(fig, use_container_width=True)

    # ---- 赛道映射表 ----
    st.markdown("#### 🔗 美股→A股赛道映射")

    mapping_rows = []
    for sector_name, d in sorted_sectors:
        info = SECTOR_MAPPING.get(sector_name, {})
        a_sectors = ", ".join(info.get("a_sectors", [])) or "-"
        a_stocks = ", ".join(info.get("a_stocks", [])) or "-"
        change = d["change_pct"]
        emoji = "🔴" if change > 0 else ("🟢" if change < 0 else "⚪")
        mapping_rows.append({
            "美股板块": f"{info.get('icon', '')} {sector_name}",
            "涨跌幅": f"{emoji} {change:+.2f}%",
            "ETF": info.get("us_etf", ""),
            "映射A股赛道": a_sectors,
            "关联持仓": a_stocks,
        })

    mapping_df = pd.DataFrame(mapping_rows)
    st.dataframe(mapping_df, use_container_width=True, hide_index=True)

    # ---- 盘前提示 ----
    _render_trading_hints(sorted_sectors, sector_data)


def _render_trading_hints(sorted_sectors: list, sector_data: dict):
    """基于美股隔夜数据生成盘前提示"""
    st.markdown("#### 💡 盘前提示")

    big_movers_up = [(n, d) for n, d in sorted_sectors if d["change_pct"] > 1.5]
    big_movers_down = [(n, d) for n, d in sorted_sectors if d["change_pct"] < -1.5]

    hints = []
    for name, d in big_movers_up:
        info = SECTOR_MAPPING.get(name, {})
        a_stocks = info.get("a_stocks", [])
        if a_stocks:
            hints.append(
                f"🔴 **{name}** 大涨 {d['change_pct']:+.2f}% → "
                f"利好A股: {', '.join(a_stocks)}"
            )

    for name, d in big_movers_down:
        info = SECTOR_MAPPING.get(name, {})
        a_stocks = info.get("a_stocks", [])
        if a_stocks:
            hints.append(
                f"🟢 **{name}** 大跌 {d['change_pct']:+.2f}% → "
                f"利空A股: {', '.join(a_stocks)}"
            )

    if hints:
        for hint in hints:
            st.markdown(f"- {hint}")
    else:
        st.info("美股隔夜波动均<1.5%，对A股持仓影响有限。")
