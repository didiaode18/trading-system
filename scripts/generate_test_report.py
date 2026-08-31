# -*- coding: utf-8 -*-
"""
测试结果报告生成器
================
汇总L1~L5各层测试结果, 生成HTML报告 + 控制台摘要
复用: backtest/report.py Chart.js模板风格

运行: python scripts/generate_test_report.py
"""
import sys, os, time, json, datetime, subprocess, re
import numpy as np

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# 1. 收集各层测试结果
# ============================================================
def collect_l1_results():
    """运行L1单元测试并收集结果"""
    print("[1/4] 运行L1单元测试...")
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "tests/test_deep_quant.py", "-v", "--tb=no"],
            capture_output=True, text=True, cwd=ROOT_DIR, timeout=120, encoding='utf-8', errors='replace'
        )
        output = result.stdout + result.stderr
        # 解析pytest输出 - 使用摘要行 "50 passed" 或 "3 passed, 2 failed"
        passed_match = re.search(r'(\d+) passed', output)
        failed_match = re.search(r'(\d+) failed', output)
        passed = int(passed_match.group(1)) if passed_match else 0
        failed = int(failed_match.group(1)) if failed_match else 0
        total = passed + failed
        elapsed_match = re.search(r'in (\d+\.\d+)s', output)
        elapsed = float(elapsed_match.group(1)) if elapsed_match else 0
        return {
            "passed": passed, "failed": failed, "total": total,
            "pass_rate": passed / max(1, total) * 100,
            "elapsed": elapsed, "status": "PASS" if failed == 0 else "FAIL",
            "detail": f"{passed}/{total} 通过 ({passed/max(1,total)*100:.1f}%)"
        }
    except Exception as e:
        return {"passed": 0, "failed": 0, "total": 0, "pass_rate": 0,
                "elapsed": 0, "status": "ERROR", "detail": str(e)}

def collect_l3_results():
    """加载L3策略回测结果"""
    print("[2/4] 加载L3策略回测结果...")
    path = os.path.join(OUTPUT_DIR, "strategy_backtest_result.json")
    if not os.path.exists(path):
        return {"status": "SKIP", "detail": "未运行 (请先执行 run_strategy_backtest.py)", "strategies": []}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        strategies = []
        for name, metrics in data.items():
            strategies.append({
                "name": name,
                "trade_count": metrics.get("trade_count", 0),
                "win_rate": metrics.get("win_rate", 0),
                "avg_pnl": metrics.get("avg_pnl", 0),
                "total_return": metrics.get("total_return", 0),
                "profit_factor": metrics.get("profit_factor", 0),
            })
        return {"status": "PASS", "detail": f"{len(strategies)}个策略完成", "strategies": strategies}
    except Exception as e:
        return {"status": "ERROR", "detail": str(e), "strategies": []}

def collect_l5_results():
    """加载L5压力测试结果"""
    print("[3/4] 加载L5压力测试结果...")
    path = os.path.join(OUTPUT_DIR, "stress_test_result.json")
    if not os.path.exists(path):
        return {"status": "SKIP", "detail": "未运行 (请先执行 run_stress_test.py)"}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        summary = data.get("summary", {})
        return {
            "status": "PASS" if summary.get("overall") == "PASS" else "WARN",
            "detail": f"综合: {summary.get('overall', '?')}",
            "ruin_probability": summary.get("ruin_probability", 0),
            "historical_pass": summary.get("historical_pass_rate", "?"),
            "max_streak": summary.get("max_streak", 0),
            "degradation_pass": summary.get("degradation_pass_rate", "?"),
        }
    except Exception as e:
        return {"status": "ERROR", "detail": str(e)}

def collect_v52_v60_results():
    """加载V5.2 vs V6.0对比结果"""
    print("[4/4] 加载V5.2 vs V6.0对比结果...")
    path = os.path.join(OUTPUT_DIR, "v52_v60_backtest_detail.json")
    if not os.path.exists(path):
        return {"status": "SKIP", "detail": "未运行"}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {"status": "PASS", "detail": "对比完成", "data": data}
    except Exception as e:
        return {"status": "ERROR", "detail": str(e)}

# ============================================================
# 2. 控制台摘要
# ============================================================
def print_console_summary(l1, l3, l5, v52v60, total_time):
    """打印控制台摘要"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    print(f"\n{'='*70}")
    print(f"  量化系统深度测试报告  {today}")
    print(f"{'='*70}")
    print(f"  L1 单元测试:  {l1['detail']:<30} [{l1['status']}]")
    print(f"  L3 策略回测:  {l3['detail']:<30} [{l3['status']}]")
    print(f"  L5 压力测试:  {l5['detail']:<30} [{l5['status']}]")
    print(f"  V52vsV60:     {v52v60['detail']:<30} [{v52v60['status']}]")
    print(f"{'='*70}")
    mins = int(total_time // 60)
    secs = int(total_time % 60)
    print(f"  总耗时: {mins}分{secs}秒")
    output_file = f"output/test_report_{today.replace('-', '')}.html"
    print(f"  详细报告: {output_file}")
    print(f"{'='*70}")

# ============================================================
# 3. HTML报告
# ============================================================
def generate_html_report(l1, l3, l5, v52v60, total_time):
    """生成HTML报告"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    today_compact = today.replace('-', '')

    # 策略对比表格
    strategy_rows = ""
    if l3.get("strategies"):
        for s in l3["strategies"]:
            ret = s.get("total_return", 0) or 0
            color = "#27ae60" if ret > 0 else "#e74c3c"
            strategy_rows += f"""<tr>
                <td>{s['name']}</td>
                <td>{s.get('trade_count', 0)}</td>
                <td>{s.get('win_rate', 0):.1f}%</td>
                <td>{s.get('avg_pnl', 0):+.2f}%</td>
                <td style='color:{color};font-weight:bold'>{ret*100:+.1f}%</td>
                <td>{s.get('profit_factor', 0):.2f}</td>
            </tr>"""

    # V5.2 vs V6.0 对比
    v52_section = ""
    if v52v60.get("status") == "PASS" and v52v60.get("data"):
        d = v52v60["data"]
        v52s = d.get("v52", {}).get("stats", {})
        v60s = d.get("v60", {}).get("stats", {})
        v52_section = f"""
        <h2>V5.2 vs V6.0 对比</h2>
        <table>
        <tr><th>指标</th><th>V5.2</th><th>V6.0</th><th>变化</th></tr>
        <tr><td>信号数</td><td>{v52s.get('count',0)}</td><td>{v60s.get('count',0)}</td>
            <td style='color:#3498db'>{v60s.get('count',0)-v52s.get('count',0):+d}</td></tr>
        <tr><td>20日胜率</td><td>{v52s.get('wr_20d',0):.1f}%</td><td>{v60s.get('wr_20d',0):.1f}%</td>
            <td>{v60s.get('wr_20d',0)-v52s.get('wr_20d',0):+.1f}pp</td></tr>
        <tr><td>20日均收益</td><td>{v52s.get('avg_20d',0):+.2f}%</td><td>{v60s.get('avg_20d',0):+.2f}%</td>
            <td>{v60s.get('avg_20d',0)-v52s.get('avg_20d',0):+.2f}pp</td></tr>
        <tr><td>弱势信号数</td><td>{v52s.get('weak_signals',0)}</td><td>{v60s.get('weak_signals',0)}</td>
            <td style='color:#27ae60'>{v60s.get('weak_signals',0)-v52s.get('weak_signals',0):+d}</td></tr>
        <tr><td>CAI IC</td><td>{v52s.get('factor_ics',{}).get('CAI',0):+.4f}</td>
            <td>{v60s.get('factor_ics',{}).get('CAI',0):+.4f}</td>
            <td style='color:#27ae60'>+{v60s.get('factor_ics',{}).get('CAI',0)-v52s.get('factor_ics',{}).get('CAI',0):.4f}</td></tr>
        </table>"""

    # 压力测试结果
    stress_section = ""
    if l5.get("status") in ("PASS", "WARN"):
        stress_section = f"""
        <h2>L5 压力测试</h2>
        <div class='cards'>
            <div class='card'><div class='v'>{l5.get('ruin_probability',0):.2%}</div><div class='l'>破产概率</div></div>
            <div class='card'><div class='v'>{l5.get('historical_pass','?')}</div><div class='l'>历史场景通过</div></div>
            <div class='card'><div class='v'>{l5.get('max_streak',0)}笔</div><div class='l'>最长连亏</div></div>
            <div class='card'><div class='v'>{l5.get('degradation_pass','?')}</div><div class='l'>降级测试通过</div></div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>量化系统深度测试报告 {today}</title>
<style>
body{{font-family:'Microsoft YaHei',sans-serif;padding:20px;background:#f5f5f5}}
.container{{max-width:1100px;margin:0 auto}}
h1{{color:#2c3e50;border-bottom:3px solid #3498db;padding-bottom:10px}}
h2{{color:#34495e;margin-top:25px}}
table{{width:100%;border-collapse:collapse;margin:12px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
th{{background:#34495e;color:#fff;padding:10px 8px;font-size:13px}}
td{{padding:8px;text-align:center;border-bottom:1px solid #ecf0f1;font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:15px 0}}
.card{{background:#fff;border-radius:8px;padding:15px;text-align:center;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
.card .v{{font-size:20px;font-weight:bold}}.card .l{{font-size:11px;color:#7f8c8d;margin-top:4px}}
.pass{{color:#27ae60;font-weight:bold}}.fail{{color:#e74c3c;font-weight:bold}}.warn{{color:#f39c12;font-weight:bold}}.skip{{color:#95a5a6}}
.summary{{background:#e8f5e9;padding:12px;border-radius:6px;margin:15px 0;border-left:4px solid #4caf50}}
.note{{background:#fff3cd;padding:12px;border-radius:6px;margin:15px 0;border-left:4px solid #ffc107}}
</style></head><body><div class='container'>
<h1>量化系统深度测试报告</h1>
<p>生成日期: {today} | 总耗时: {total_time/60:.1f}分钟</p>

<h2>测试总览</h2>
<div class='cards'>
    <div class='card'><div class='v {l1["status"].lower()}'>{l1["status"]}</div><div class='l'>L1 单元测试<br>{l1["detail"]}</div></div>
    <div class='card'><div class='v {l3["status"].lower()}'>{l3["status"]}</div><div class='l'>L3 策略回测<br>{l3["detail"]}</div></div>
    <div class='card'><div class='v {l5["status"].lower()}'>{l5["status"]}</div><div class='l'>L5 压力测试<br>{l5["detail"]}</div></div>
    <div class='card'><div class='v {v52v60["status"].lower()}'>{v52v60["status"]}</div><div class='l'>V5.2 vs V6.0<br>{v52v60["detail"]}</div></div>
</div>

<h2>L1 单元测试详情</h2>
<p>通过: {l1.get('passed',0)}/{l1.get('total',0)} ({l1.get('pass_rate',0):.1f}%) | 耗时: {l1.get('elapsed',0):.1f}s</p>

<h2>L3 策略横向对比</h2>
{f'<table><tr><th>策略</th><th>交易笔数</th><th>胜率</th><th>平均收益</th><th>累计收益</th><th>盈亏比</th></tr>{strategy_rows}</table>' if strategy_rows else '<p class="note">策略回测未运行</p>'}

{v52_section}
{stress_section}

<div class='summary'>
<b>测试结论:</b><br>
• L1单元测试: {l1['detail']}<br>
• L3策略回测: {l3['detail']}<br>
• L5压力测试: {l5['detail']}<br>
• V5.2 vs V6.0: {v52v60['detail']}
</div>

</div></body></html>"""

    output_path = os.path.join(OUTPUT_DIR, f"test_report_{today_compact}.html")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n[OK] HTML报告: {output_path}")
    return output_path

# ============================================================
# 4. 主函数
# ============================================================
def main():
    t0 = time.time()
    print("=" * 70)
    print("  量化系统深度测试 - 报告生成")
    print("=" * 70)

    # 收集各层结果
    l1 = collect_l1_results()
    l3 = collect_l3_results()
    l5 = collect_l5_results()
    v52v60 = collect_v52_v60_results()

    total_time = time.time() - t0

    # 控制台摘要
    print_console_summary(l1, l3, l5, v52v60, total_time)

    # HTML报告
    html_path = generate_html_report(l1, l3, l5, v52v60, total_time)

    print(f"\n完成! 总耗时: {total_time:.1f}秒")

if __name__ == "__main__":
    main()
