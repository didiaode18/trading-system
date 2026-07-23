"""
回测报告生成模块
================
生成HTML格式的可视化回测报告：
- 净值曲线图（含基准对比）
- 回撤曲线
- 月度收益热力图
- 交易明细表
- 核心指标仪表盘
"""

import os
import logging
import datetime
import pandas as pd
import numpy as np

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def generate_html_report(report: dict, output_path: str = None) -> str:
    """
    生成HTML回测报告
    
    参数:
        report: 回测绩效报告字典
        output_path: 输出文件路径（默认output/backtest_report_日期.html）
    
    返回:
        报告文件路径
    """
    if "error" in report:
        return f"回测失败: {report['error']}"

    if output_path is None:
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        date_str = datetime.date.today().strftime("%Y%m%d")
        output_path = os.path.join(config.OUTPUT_DIR, f"backtest_report_{date_str}.html")

    # 准备数据
    daily_df = report.get("daily_values", pd.DataFrame())
    trades_df = report.get("trades", pd.DataFrame())
    monthly = report.get("monthly_returns", pd.DataFrame())

    # 净值曲线数据
    dates_json = daily_df["date"].tolist() if not daily_df.empty else []
    values_json = [round(v, 2) for v in daily_df["total_value"].tolist()] if not daily_df.empty else []

    # 回撤曲线
    if not daily_df.empty:
        equity = daily_df["total_value"]
        cummax = equity.cummax()
        drawdown = ((cummax - equity) / cummax * 100).tolist()
        drawdown = [round(d, 2) for d in drawdown]
    else:
        drawdown = []

    html = _build_html(report, dates_json, values_json, drawdown, trades_df, monthly)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"回测报告已生成: {output_path}")
    return output_path


def _build_html(report: dict, dates: list, values: list, drawdown: list,
                trades_df: pd.DataFrame, monthly: pd.DataFrame) -> str:
    """构建HTML内容"""

    # 交易明细表格
    trades_html = ""
    if not trades_df.empty and "pnl" in trades_df.columns:
        rows = ""
        for _, t in trades_df.tail(50).iterrows():
            pnl = t.get("pnl", 0)
            color = "#e74c3c" if pnl > 0 else "#27ae60" if pnl < 0 else "#333"
            name = config.get_stock_name(str(t.get("code", "")))
            rows += f"""<tr>
                <td>{t.get('buy_date', '')}</td>
                <td>{t.get('sell_date', t.get('date', ''))}</td>
                <td>{t.get('code', '')} {name}</td>
                <td>{t.get('buy_price', 0):.2f}</td>
                <td>{t.get('sell_price', t.get('price', 0)):.2f}</td>
                <td>{t.get('shares', 0)}</td>
                <td style="color:{color};font-weight:bold">{pnl:+,.0f}</td>
                <td style="color:{color}">{t.get('pnl_pct', 0):.2%}</td>
                <td>{t.get('hold_days', 0)}天</td>
            </tr>"""
        trades_html = f"""
        <h3>交易明细（最近50笔）</h3>
        <table class="trades-table">
            <tr><th>买入日</th><th>卖出日</th><th>股票</th><th>买入价</th>
                <th>卖出价</th><th>股数</th><th>盈亏</th><th>收益率</th><th>持仓</th></tr>
            {rows}
        </table>"""

    # 月度收益
    monthly_html = ""
    if not monthly.empty:
        rows = ""
        for _, m in monthly.iterrows():
            ret = m["return_pct"]
            color = "#e74c3c" if ret > 0 else "#27ae60" if ret < 0 else "#333"
            rows += f"<td style='color:{color}'>{ret:.1%}</td>"
        monthly_html = f"<div class='monthly'><h3>月度收益</h3><table class='monthly-table'><tr>{rows}</tr></table></div>"

    # 基准信息
    benchmark_html = ""
    if "benchmark_return" in report:
        benchmark_html = f"""
        <div class="metric-card">
            <div class="metric-label">基准收益</div>
            <div class="metric-value">{report['benchmark_return']:.2%}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">超额收益</div>
            <div class="metric-value" style="color:{'#e74c3c' if report.get('excess_return', 0) > 0 else '#27ae60'}">{report.get('excess_return', 0):.2%}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Alpha</div>
            <div class="metric-value">{report.get('alpha', 0):.2%}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Beta</div>
            <div class="metric-value">{report.get('beta', 0):.2f}</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>量化回测报告 - {report.get('start_date', '')} ~ {report.get('end_date', '')}</title>
<style>
body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; margin: 20px; background: #f5f5f5; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
h3 {{ color: #34495e; margin-top: 30px; }}
.metrics {{ display: flex; flex-wrap: wrap; gap: 15px; margin: 20px 0; }}
.metric-card {{ background: white; border-radius: 8px; padding: 15px 20px; min-width: 150px;
               box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
.metric-label {{ font-size: 12px; color: #7f8c8d; margin-bottom: 5px; }}
.metric-value {{ font-size: 24px; font-weight: bold; color: #2c3e50; }}
.chart-container {{ background: white; border-radius: 8px; padding: 20px; margin: 20px 0;
                   box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
canvas {{ width: 100% !important; height: 300px !important; }}
.trades-table {{ width: 100%; border-collapse: collapse; background: white; font-size: 13px; }}
.trades-table th {{ background: #3498db; color: white; padding: 8px; text-align: left; }}
.trades-table td {{ padding: 6px 8px; border-bottom: 1px solid #eee; }}
.trades-table tr:hover {{ background: #f8f9fa; }}
.monthly-table td {{ padding: 8px 12px; border: 1px solid #eee; text-align: center; }}
.footer {{ text-align: center; color: #95a5a6; margin-top: 40px; font-size: 12px; }}
</style>
</head>
<body>
<div class="container">
<h1>📊 量化回测绩效报告</h1>
<p>回测区间: {report.get('start_date', '')} ~ {report.get('end_date', '')} 
   ({report.get('trading_days', 0)}个交易日) | 
   初始资金: {report.get('initial_capital', 0):,.0f}元</p>

<div class="metrics">
    <div class="metric-card">
        <div class="metric-label">总收益率</div>
        <div class="metric-value" style="color:{'#e74c3c' if report.get('total_return', 0) > 0 else '#27ae60'}">{report.get('total_return', 0):.2%}</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">年化收益</div>
        <div class="metric-value">{report.get('annual_return', 0):.2%}</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">最大回撤</div>
        <div class="metric-value" style="color:#e74c3c">-{report.get('max_drawdown', 0):.2%}</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">夏普比率</div>
        <div class="metric-value">{report.get('sharpe_ratio', 0):.2f}</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">Calmar比率</div>
        <div class="metric-value">{report.get('calmar_ratio', 0):.2f}</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">胜率</div>
        <div class="metric-value">{report.get('win_rate', 0):.1%}</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">盈亏比</div>
        <div class="metric-value">{report.get('profit_factor', 0):.2f}</div>
    </div>
    <div class="metric-card">
        <div class="metric-label">交易次数</div>
        <div class="metric-value">{report.get('total_trades', 0)}</div>
    </div>
    {benchmark_html}
</div>

<div class="chart-container">
    <h3>净值曲线</h3>
    <canvas id="equityChart"></canvas>
</div>

<div class="chart-container">
    <h3>回撤曲线</h3>
    <canvas id="drawdownChart"></canvas>
</div>

{monthly_html}
{trades_html}

<div class="footer">
    <p>交易成本: 佣金{report.get('total_commission', 0):,.0f}元 + 
       印花税{report.get('total_stamp_tax', 0):,.0f}元 = 
       合计{report.get('total_cost', 0):,.0f}元</p>
    <p>生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 
       高胜率A股交易操作系统 V7.1</p>
</div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
const dates = {dates};
const values = {values};
const drawdown = {drawdown};

// 净值曲线
new Chart(document.getElementById('equityChart'), {{
    type: 'line',
    data: {{
        labels: dates,
        datasets: [{{
            label: '策略净值',
            data: values,
            borderColor: '#3498db',
            borderWidth: 1.5,
            pointRadius: 0,
            fill: false,
        }}]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
            x: {{ display: true, ticks: {{ maxTicksLimit: 12 }} }},
            y: {{ display: true }}
        }}
    }}
}});

// 回撤曲线
new Chart(document.getElementById('drawdownChart'), {{
    type: 'line',
    data: {{
        labels: dates,
        datasets: [{{
            label: '回撤(%)',
            data: drawdown.map(d => -d),
            borderColor: '#e74c3c',
            borderWidth: 1,
            pointRadius: 0,
            fill: true,
            backgroundColor: 'rgba(231,76,60,0.1)',
        }}]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
            x: {{ display: true, ticks: {{ maxTicksLimit: 12 }} }},
            y: {{ display: true }}
        }}
    }}
}});
</script>
</body>
</html>"""

    return html
