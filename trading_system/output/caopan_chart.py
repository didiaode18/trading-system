# -*- coding: utf-8 -*-
"""
操盘密码可视化图表模块 V2.0
==========================
生成超越付费软件的HTML分析图表

图表包含:
  1. K线图（红涨绿跌）
  2. 双控盘生命线（黄色LL1 + 紫色LL2 自适应虚线）
  3. DK买卖点标记（D=红色向上箭头, K=绿色向下箭头, 假信号灰色×）
  4. 主力资金柱状图（黄=净买入, 蓝=净卖出）
  5. 散户资金柱状图（绿=净买入, 紫=净卖出）
  6. 支撑/压力位标注 + 乖离率指引 + 市场环境 + 盈亏比

输出: HTML文件（基于ECharts），可直接浏览器打开
"""

import json
import os
import datetime
import pandas as pd
import numpy as np
from typing import Optional


def generate_caopan_chart(result: dict, output_path: str = None,
                          show_days: int = 120) -> str:
    """
    生成操盘密码分析图表HTML

    参数:
        result: CaopanEngine.analyze()的返回结果
        output_path: 输出文件路径（None则不保存）
        show_days: 显示最近N天数据

    返回:
        HTML字符串
    """
    if "error" in result:
        return f"<html><body><h2>分析失败: {result['error']}</h2></body></html>"

    df = result.get("df_analyzed")
    if df is None or df.empty:
        return "<html><body><h2>无数据</h2></body></html>"

    code = result.get("code", "")
    name = result.get("name", code)
    trend_desc = result.get("trend_desc", "")
    action = result.get("action_suggestion", {})

    # 取最近N天
    df_show = df.tail(show_days).copy()

    # 准备数据
    dates = df_show["date"].tolist() if "date" in df_show.columns else list(range(len(df_show)))
    # K线数据: [open, close, low, high]
    kline_data = df_show[["open", "close", "low", "high"]].values.tolist()
    # 生命线
    ll_fast = [round(v, 3) if not pd.isna(v) else None for v in df_show["ll_fast"]]
    ll_slow = [round(v, 3) if not pd.isna(v) else None for v in df_show["ll_slow"]]

    # DK信号标记（仅显示未过滤的有效信号）
    d_points = []
    k_points = []
    for i, (_, row) in enumerate(df_show.iterrows()):
        dk = row.get("dk_signal")
        strength = row.get("dk_strength", 0)
        filtered = row.get("dk_filtered", False)
        grade = row.get("dk_grade", "")
        if dk == "D" and strength >= 50 and not filtered:
            d_points.append({"coord": [i, row["low"] * 0.99], "strength": int(strength), "value": f"D({int(strength)})"})
        elif dk == "K" and strength >= 50 and not filtered:
            k_points.append({"coord": [i, row["high"] * 1.01], "strength": int(strength), "value": f"K({int(strength)})"})

    # 主力资金流
    main_flow = [round(v / 10000, 2) if not pd.isna(v) else 0 for v in df_show["main_flow"]]
    # 散户资金流
    retail_flow = [round(v / 10000, 2) if not pd.isna(v) else 0 for v in df_show["retail_flow"]]

    # 成交量
    volumes = df_show["volume"].tolist()

    # 生成HTML
    html = _build_echarts_html(
        code=code, name=name, trend_desc=trend_desc, action=action,
        dates=dates, kline_data=kline_data,
        ll_fast=ll_fast, ll_slow=ll_slow,
        d_points=d_points, k_points=k_points,
        main_flow=main_flow, retail_flow=retail_flow,
        volumes=volumes, result=result,
    )

    # 保存文件
    if output_path:
        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

    return html


def _build_chip_info_row(result: dict) -> str:
    """生成筹码分布信息行HTML"""
    chip = result.get("chip")
    if not chip:
        return ""
    pr = chip.get("profit_ratio", 0)
    conc = chip.get("concentration", 0)
    ctrl = chip.get("control_level", {})
    pattern = chip.get("pattern", {})
    cc = chip.get("cost_center", 0)
    sup = chip.get("support", 0)
    res = chip.get("resistance", 0)
    # 控盘度颜色
    ctrl_score = ctrl.get("score", 0)
    ctrl_color = "#e53935" if ctrl_score >= 70 else "#ff9800" if ctrl_score >= 45 else "#aaa"
    # 获利盘颜色
    pr_color = "#e53935" if pr > 0.8 else "#ff9800" if pr > 0.5 else "#4caf50"
    return f"""<div class="info-row">
    <div class="info-item"><span class="info-label">📊筹码获利盘:</span><span class="info-value" style="color:{pr_color}">{pr*100:.1f}%</span></div>
    <div class="info-item"><span class="info-label">集中度:</span><span class="info-value">{conc*100:.1f}%</span></div>
    <div class="info-item"><span class="info-label">成本重心:</span><span class="info-value">{cc:.3f}</span></div>
    <div class="info-item"><span class="info-label">控盘度:</span><span class="info-value" style="color:{ctrl_color}">{ctrl.get('level','-')}({ctrl_score}分)</span></div>
    <div class="info-item"><span class="info-label">筹码形态:</span><span class="info-value">{pattern.get('name','-')}</span></div>
    <div class="info-item"><span class="info-label">筹码支撑:</span><span class="info-value" style="color:#e53935">{sup:.3f}</span></div>
    <div class="info-item"><span class="info-label">筹码压力:</span><span class="info-value" style="color:#4caf50">{res:.3f}</span></div>
</div>"""


def _build_echarts_html(code, name, trend_desc, action,
                         dates, kline_data, ll_fast, ll_slow,
                         d_points, k_points, main_flow, retail_flow,
                         volumes, result) -> str:
    """构建ECharts HTML页面"""

    # 趋势颜色（V2.0 5级趋势）
    trend_level = result.get("trend_level", 3)
    trend_color = {5: "#e53935", 4: "#ff7043", 3: "#ff9800", 2: "#66bb6a", 1: "#4caf50"}.get(trend_level, "#333")
    action_type = action.get("type", "hold")
    action_color = {
        "buy": "#e53935", "add": "#ff5722", "hold": "#2196f3",
        "reduce": "#ff9800", "clear": "#4caf50", "watch": "#9e9e9e"
    }.get(action_type, "#333")

    # 中文翻译映射
    env_cn = {"trend": "趋势市", "oscillation": "震荡市", "transition": "转换期"}.get(
        result.get('market_env', {}).get('mode', ''), result.get('market_env', {}).get('mode', ''))
    pattern_cn = {"mild_build": "温和建仓", "surge": "放量拉升", "fake": "对倒骗线", "normal": "正常"}.get(
        result.get('fund_pattern', 'normal'), result.get('fund_pattern', 'normal'))
    deviation_cn = {"超买减仓": "超买减仓", "偏高减仓": "偏高减仓", "正常持有": "正常持有", "超卖观察": "超卖观察"}.get(
        result.get('deviation_action', ''), result.get('deviation_action', ''))

    # D点标记数据（增强：更大标记+分数标签）
    d_mark = json.dumps([{
        "coord": [dp["coord"][0], dp["coord"][1]],
        "symbol": "triangle",
        "symbolSize": 18,
        "symbolRotate": 0,
        "itemStyle": {"color": "#e53935", "borderColor": "#fff", "borderWidth": 1},
        "label": {"show": True, "formatter": f"D({dp.get('strength', '')})", "position": "bottom",
                  "color": "#e53935", "fontWeight": "bold", "fontSize": 13}
    } for dp in d_points], ensure_ascii=False)

    k_mark = json.dumps([{
        "coord": [kp["coord"][0], kp["coord"][1]],
        "symbol": "triangle",
        "symbolSize": 18,
        "symbolRotate": 180,
        "itemStyle": {"color": "#4caf50", "borderColor": "#fff", "borderWidth": 1},
        "label": {"show": True, "formatter": f"K({kp.get('strength', '')})", "position": "top",
                  "color": "#4caf50", "fontWeight": "bold", "fontSize": 13}
    } for kp in k_points], ensure_ascii=False)

    # 资金流柱状图颜色
    main_colors = ["#f5a623" if v > 0 else "#2196f3" for v in main_flow]
    retail_colors = ["#4caf50" if v > 0 else "#9c27b0" for v in retail_flow]

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>操盘密码分析 - {name}({code})</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; margin: 0; padding: 15px; background: #1a1a2e; color: #eee; }}
.header {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 20px; background: #16213e; border-radius: 8px; margin-bottom: 10px; }}
.header h1 {{ font-size: 18px; margin: 0; color: #fff; }}
.trend-badge {{ padding: 4px 12px; border-radius: 4px; font-weight: bold; font-size: 14px; }}
.action-box {{ padding: 10px 20px; background: #0f3460; border-radius: 8px; margin-bottom: 10px; border-left: 4px solid {action_color}; }}
.info-row {{ display: flex; gap: 20px; padding: 8px 20px; background: #16213e; border-radius: 8px; margin-bottom: 10px; font-size: 13px; flex-wrap: wrap; }}
.info-item {{ display: flex; align-items: center; gap: 5px; }}
.info-label {{ color: #aaa; }}
.info-value {{ font-weight: bold; }}
#chart {{ width: 100%; height: 700px; }}
.legend {{ display: flex; gap: 15px; padding: 5px 20px; font-size: 12px; color: #aaa; }}
.legend-item {{ display: flex; align-items: center; gap: 4px; }}
.legend-dot {{ width: 12px; height: 3px; border-radius: 2px; }}
</style>
</head><body>

<div class="header">
    <h1>📊 操盘密码分析 | {name}({code})</h1>
    <span class="trend-badge" style="background:{trend_color};color:#fff">{trend_desc}</span>
</div>

<div class="action-box">
    <b style="color:{action_color}">🎯 操作建议: {action.get('desc', '观望')}</b>
    <span style="color:#aaa;font-size:12px;margin-left:10px">{action.get('detail', '')}</span>
</div>

<div class="info-row">
    <div class="info-item"><span class="info-label">收盘价:</span><span class="info-value">{result.get('close', 0):.3f}</span></div>
    <div class="info-item"><span class="info-label">LL1快线(黄):</span><span class="info-value" style="color:#f5a623">{result.get('ll_fast', 0):.3f} {result.get('ll_fast_direction', '')}</span></div>
    <div class="info-item"><span class="info-label">LL2慢线(紫):</span><span class="info-value" style="color:#9c27b0">{result.get('ll_slow', 0):.3f} {result.get('ll_slow_direction', '')}</span></div>
    <div class="info-item"><span class="info-label">乖离率:</span><span class="info-value">{result.get('deviation_pct', 0):.2f}% ({result.get('deviation_action', '')})</span></div>
    <div class="info-item"><span class="info-label">DK信号:</span><span class="info-value" style="color:{'#e53935' if result.get('dk_signal')=='D' else '#4caf50' if result.get('dk_signal')=='K' else '#aaa'}">{result.get('dk_signal') or '无'} ({result.get('dk_strength', 0)}分/{result.get('dk_grade', '-')})</span></div>
    <div class="info-item"><span class="info-label">主力连续流入:</span><span class="info-value">{result.get('main_flow_streak', 0)}天</span></div>
    <div class="info-item"><span class="info-label">盈亏比:</span><span class="info-value" style="color:{'#4caf50' if result.get('risk_reward',{}).get('passed') else '#f44336'}">{result.get('risk_reward',{}).get('risk_reward_1',0):.1f}:1</span></div>
    <div class="info-item"><span class="info-label">市场环境:</span><span class="info-value" style="color:{'#e53935' if result.get('market_env',{}).get('mode')=='trend' else '#ff9800'}">{env_cn}</span></div>
    <div class="info-item"><span class="info-label">周线:</span><span class="info-value">{result.get('weekly_trend',{}).get('desc','')}</span></div>
    <div class="info-item"><span class="info-label">资金模式:</span><span class="info-value" style="color:{'#e53935' if pattern_cn=='对倒骗线' else '#4caf50' if pattern_cn=='温和建仓' else '#eee'}">{pattern_cn}</span></div>
    <div class="info-item"><span class="info-label">支撑(LL2):</span><span class="info-value" style="color:#e53935">{result.get('support_price', 0):.3f}</span></div>
    <div class="info-item"><span class="info-label">压力(LL1):</span><span class="info-value" style="color:#4caf50">{result.get('resistance_price', 0):.3f}</span></div>
</div>

{_build_chip_info_row(result)}

<div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#f5a623"></div>LL1快线(自适应EMA10)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#9c27b0"></div>LL2慢线(自适应EMA30)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#e53935"></div>D点(三重确认)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#4caf50"></div>K点(三重确认)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#f5a623"></div>主力净买入</div>
    <div class="legend-item"><div class="legend-dot" style="background:#2196f3"></div>主力净卖出</div>
    <div class="legend-item"><div class="legend-dot" style="background:#4caf50"></div>散户净买入</div>
    <div class="legend-item"><div class="legend-dot" style="background:#9c27b0"></div>散户净卖出</div>
</div>

<div id="chart"></div>

<script>
var chart = echarts.init(document.getElementById('chart'), 'dark');
var dates = {json.dumps(dates, ensure_ascii=False)};
var klineData = {json.dumps(kline_data)};
var llFast = {json.dumps(ll_fast)};
var llSlow = {json.dumps(ll_slow)};
var mainFlow = {json.dumps(main_flow)};
var retailFlow = {json.dumps(retail_flow)};
var volumes = {json.dumps(volumes)};

var option = {{
    backgroundColor: '#1a1a2e',
    animation: false,
    tooltip: {{
        trigger: 'axis',
        axisPointer: {{ type: 'cross' }},
        backgroundColor: 'rgba(0,0,0,0.8)',
        textStyle: {{ fontSize: 11 }}
    }},
    axisPointer: {{ link: [{{xAxisIndex: 'all'}}] }},
    grid: [
        {{ left: '8%', right: '3%', top: '3%', height: '42%' }},
        {{ left: '8%', right: '3%', top: '50%', height: '18%' }},
        {{ left: '8%', right: '3%', top: '72%', height: '18%' }}
    ],
    xAxis: [
        {{ type: 'category', data: dates, gridIndex: 0, axisLabel: {{show: false}}, axisLine: {{lineStyle: {{color: '#444'}}}} }},
        {{ type: 'category', data: dates, gridIndex: 1, axisLabel: {{show: false}}, axisLine: {{lineStyle: {{color: '#444'}}}} }},
        {{ type: 'category', data: dates, gridIndex: 2, axisLabel: {{fontSize: 9, color: '#888'}}, axisLine: {{lineStyle: {{color: '#444'}}}} }}
    ],
    yAxis: [
        {{ scale: true, gridIndex: 0, splitLine: {{lineStyle: {{color: '#333'}}}}, axisLabel: {{color: '#aaa'}} }},
        {{ scale: true, gridIndex: 1, splitLine: {{show: false}}, axisLabel: {{fontSize: 9, color: '#888', formatter: function(v){{return (v/10000).toFixed(0)+'万'}}}} }},
        {{ scale: true, gridIndex: 2, splitLine: {{show: false}}, axisLabel: {{fontSize: 9, color: '#888', formatter: function(v){{return (v/10000).toFixed(0)+'万'}}}} }}
    ],
    dataZoom: [
        {{ type: 'inside', xAxisIndex: [0, 1, 2], start: 60, end: 100 }},
        {{ type: 'slider', xAxisIndex: [0, 1, 2], bottom: '2%', height: 15, start: 60, end: 100 }}
    ],
    series: [
        // K线
        {{
            name: 'K线',
            type: 'candlestick',
            data: klineData,
            xAxisIndex: 0, yAxisIndex: 0,
            itemStyle: {{
                color: '#e53935', color0: '#4caf50',
                borderColor: '#e53935', borderColor0: '#4caf50'
            }},
            markPoint: {{
                data: {d_mark}.concat({k_mark}),
                animation: false
            }}
        }},
        // 生命线快线（黄色虚线）
        {{
            name: '生命线快(EMA13)',
            type: 'line',
            data: llFast,
            xAxisIndex: 0, yAxisIndex: 0,
            smooth: true,
            symbol: 'none',
            lineStyle: {{ color: '#f5a623', width: 1.5, type: 'dashed' }}
        }},
        // 生命线慢线（紫色虚线）
        {{
            name: '生命线慢(EMA34)',
            type: 'line',
            data: llSlow,
            xAxisIndex: 0, yAxisIndex: 0,
            smooth: true,
            symbol: 'none',
            lineStyle: {{ color: '#9c27b0', width: 1.5, type: 'dashed' }}
        }},
        // 主力资金流（机构监控）
        {{
            name: '机构监控',
            type: 'bar',
            data: mainFlow.map(function(v, i) {{
                return {{ value: v, itemStyle: {{ color: v > 0 ? '#f5a623' : '#2196f3' }} }};
            }}),
            xAxisIndex: 1, yAxisIndex: 1
        }},
        // 散户资金流（散户监控）
        {{
            name: '散户监控',
            type: 'bar',
            data: retailFlow.map(function(v, i) {{
                return {{ value: v, itemStyle: {{ color: v > 0 ? '#4caf50' : '#9c27b0' }} }};
            }}),
            xAxisIndex: 2, yAxisIndex: 2
        }}
    ]
}};

chart.setOption(option);
window.addEventListener('resize', function() {{ chart.resize(); }});
</script>

<div style="text-align:center;color:#666;font-size:11px;margin-top:10px">
    操盘密码自适应趋势策略引擎 V2.0 | {name}({code}) | {trend_desc} | 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}
    <br>DK信号: {result.get('dk_signal') or '无'} | 原因: {result.get('dk_reason', '')} | 主力连续流入: {result.get('main_flow_streak', 0)}天 | 盈亏比: {result.get('risk_reward',{}).get('risk_reward_1',0):.1f}:1
</div>
</body></html>"""

    return html


def generate_batch_charts(results: list, output_dir: str) -> list:
    """
    批量生成图表

    参数:
        results: [CaopanEngine.analyze()结果列表]
        output_dir: 输出目录

    返回:
        [生成的文件路径列表]
    """
    os.makedirs(output_dir, exist_ok=True)
    paths = []
    for r in results:
        if "error" in r:
            continue
        code = r.get("code", "unknown")
        name = r.get("name", code)
        filename = f"caopan_{code}_{datetime.date.today().strftime('%Y%m%d')}.html"
        filepath = os.path.join(output_dir, filename)
        generate_caopan_chart(r, output_path=filepath)
        paths.append(filepath)
    return paths
