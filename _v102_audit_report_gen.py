"""
V10.2 回测引擎审计报告生成器
============================
基于审计运行结果，生成结构化HTML报告
"""
import datetime
import os

today = datetime.date.today().strftime("%Y-%m-%d")

# ============================================================
# 审计发现的所有Bug
# ============================================================
BUGS = [
    {
        "id": "BUG-001",
        "severity": "MEDIUM",
        "file": "trading_system/backtest/broker.py",
        "line": "165-168, 236-249, 334-338",
        "category": "交易成本",
        "description": "过户费(transfer_fee_rate)已定义但从未使用",
        "detail": "CostConfig中定义了 transfer_fee_rate = 0.00001（万0.1），"
                  "但_calc_commission()仅计算佣金(amount*rate)，"
                  "execute_buy()和execute_sell()中total_cost均未包含过户费。"
                  "导致回测交易成本偏低。",
        "trigger": "所有买入和卖出交易",
        "impact": "每笔交易遗漏过户费约0.5元(50万元交易)，累计影响约0.001%/笔",
        "fix": "在_calc_commission()中添加transfer_fee，或新增_calc_transfer_fee()方法，"
               "在execute_buy/sell的total_cost中加入transfer_fee",
    },
    {
        "id": "BUG-002",
        "severity": "MEDIUM",
        "file": "trading_system/backtest/metrics.py",
        "line": "49-60",
        "category": "绩效指标",
        "description": "Sharpe比率在std近似0时返回极端值(8.58e+16)而非0",
        "detail": "calc_sharpe_ratio()使用 std()==0 做精确比较，"
                  "但pd.Series([0.01]*10).std()返回约5.8e-18（浮点精度非严格0），"
                  "导致条件不成立，excess_return/std得到天文数字。"
                  "该bug会导致Sharpe/Sortino等指标在低波动场景下严重失真。",
        "trigger": "日收益率序列标准差极小（如净值几乎不变）",
        "impact": "Sharpe比率虚高，绩效报告失真",
        "fix": "将 std()==0 改为 std() < 1e-12 或使用 np.isclose(std, 0, atol=1e-12)",
    },
    {
        "id": "BUG-003",
        "severity": "LOW",
        "file": "trading_system/backtest_real.py",
        "line": "51, 174-192",
        "category": "涨跌停限制",
        "description": "V5引擎涨跌停判定使用固定9.5%阈值，未区分板块",
        "detail": "LIMIT_PCT=0.095 对所有股票统一使用9.5%判定涨跌停。"
                  "但创业板(300xxx)/科创板(688xxx)涨跌停为20%，ST股为5%，主板为10%。"
                  "V2引擎(broker.py)已正确实现board_rules板块差异化。",
        "trigger": "回测创业板/科创板股票时，20%涨跌停被误判为9.5%",
        "impact": "创业板/科创板股票的涨跌停过滤过于严格，可能错误拒绝正常交易",
        "fix": "引入板块差异化涨跌停判定（参考broker.py的_board_is_limit_up/_board_is_limit_down）",
    },
    {
        "id": "BUG-004",
        "severity": "LOW",
        "file": "trading_system/backtest/engine.py",
        "line": "319",
        "category": "末日平仓",
        "description": "末日强制平仓使用固定10%跌停价，未区分板块",
        "detail": "_force_close_all()中 limit_down_price = bar['pre_close'] * 0.9，"
                  "固定使用10%跌停价。创业板/科创板应为20%，ST应为5%。",
        "trigger": "回测末日持有创业板/科创板/ST股且跌停",
        "impact": "末日平仓价格计算不准确，影响最终收益约0.1-0.5%",
        "fix": "改用board_rules.is_limit_down()替代硬编码0.9",
    },
    {
        "id": "BUG-005",
        "severity": "LOW",
        "file": "trading_system/backtest_real.py",
        "line": "391-411",
        "category": "回测边界",
        "description": "回测结束持仓平仓使用close*(1-slippage)，但收盘价不应加滑点",
        "detail": "回测最后一天仍持仓时，exec_price = last_row['close'] * (1 - slippage)。"
                  "但收盘价是确定性价格（非开盘价的不确定执行），不应额外扣减滑点。"
                  "而正常卖出信号使用T+1开盘价执行时加滑点是正确的。",
        "trigger": "回测结束日仍有持仓",
        "impact": "最终收益略微偏低（约0.2-0.5%的滑点偏差）",
        "fix": "回测结束平仓使用 close 而非 close * (1 - slippage)",
    },
    {
        "id": "BUG-006",
        "severity": "LOW",
        "file": "trading_system/backtest_real.py",
        "line": "46",
        "category": "交易成本",
        "description": "V5引擎佣金计算将过户费隐含在COMMISSION常量中，与V2引擎口径不一致",
        "detail": "V5: COMMISSION = config.COMMISSION_RATE*2 + config.STAMP_TAX_RATE ≈ 0.0016，"
                  "未单独列出过户费。V2引擎(broker.py)分别计算佣金和印花税但遗漏过户费。"
                  "两个引擎的交易成本建模口径不一致，导致回测结果不可直接对比。",
        "trigger": "V2与V5引擎回测结果对比",
        "impact": "双引擎回测结果存在系统性偏差",
        "fix": "统一两引擎的交易成本模型，均包含佣金+印花税+过户费三项",
    },
]

# ============================================================
# 审计通过项
# ============================================================
PASSED_CHECKS = [
    ("未来函数防护", "V5引擎: T日收盘后计算信号，T+1日开盘价执行(exec_price = open_arr[i+1] * (1+slippage))；V2引擎: 逐日事件驱动架构，天然防护"),
    ("T+1限制", "broker.py: frozen_shares/frozen_date机制，new_day()正确解除冻结。同日卖出正确拒绝，次日卖出正确执行"),
    ("涨跌停限制(V2)", "broker.py: _board_is_limit_up/_board_is_limit_down区分主板10%/创业板20%/ST 5%/科创板20%"),
    ("FIFO盈亏匹配", "engine.py: _calc_trade_pnl使用pop(0)先进先出匹配买卖记录"),
    ("止损逻辑", "V5引擎: 使用盘中最低价(low<=stop_price)触发止损，符合实盘条件单逻辑"),
    ("回落止盈", "V5引擎: 要求highest_since_buy > buy_price(盈利状态)才触发，避免亏损时误触发"),
    ("V10.2财报集成-配置开关", "FINANCIAL_REPORT_ENABLED=False时，compute_financial_report()正确返回bonus=0"),
    ("V10.2财报集成-无数据降级", "所有财报字段为None时，bonus=0，summary='未启用'"),
    ("V10.2财报集成-极端值防护", "roe=999/gross_margin=-50/debt_ratio=200等极端值，bonus=-2(在[-2,+4]范围内)"),
    ("V10.2财报集成-因子隔离", "启用/禁用财报分析，其他因子(N/S/L/CAI/V/W)完全不受影响，仅新增FR_财报因子"),
    ("年化收益计算", "calc_annual_return(0.5, 252) = 50.00%，公式(1+r)^(252/days)-1正确"),
    ("最大回撤计算", "calc_max_drawdown([100,110,105,90,95,100]) = 18.18%，正确识别峰值110→谷值90"),
    ("空数据边界", "calc_annual_return(0, 0) = 0，trading_days<=0防护有效"),
    ("佣金配置一致性", "V5引擎COMMISSION=0.0016与config.COMMISSION_RATE*2+config.STAMP_TAX_RATE一致"),
]

# ============================================================
# V2引擎回测结果（从审计脚本输出中提取）
# ============================================================
V2_RESULT = {
    "data_range": "2023-01-03 ~ 2026-08-20",
    "trading_days": 880,
    "stocks": 11,
    "initial_capital": "1,000,000",
    "note": "V2引擎含canslim_score()网络调用，完整回测耗时>10分钟。审计期间策略熔断器(P2-5)多次触发。",
}

# ============================================================
# 审计统计
# ============================================================
severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
for b in BUGS:
    severity_counts[b["severity"]] = severity_counts.get(b["severity"], 0) + 1

severity_colors = {
    "CRITICAL": "#FF4D4F", "HIGH": "#FA8C16",
    "MEDIUM": "#FAAD14", "LOW": "#1890FF"
}

# ============================================================
# HTML报告
# ============================================================
html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>V10.2 回测引擎审计报告</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif; margin: 0; padding: 20px; background: #f0f2f5; color: #333; }}
.container {{ max-width: 1280px; margin: 0 auto; }}
.header {{ background: linear-gradient(135deg, #1890FF 0%, #096DD9 100%); color: white; padding: 28px 36px; border-radius: 12px 12px 0 0; }}
.header h1 {{ margin: 0; font-size: 24px; font-weight: 600; }}
.header .meta {{ font-size: 13px; opacity: 0.85; margin-top: 8px; line-height: 1.8; }}
.content {{ background: white; padding: 24px 36px; border-radius: 0 0 12px 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); }}
.section {{ margin: 28px 0; }}
.section-title {{ font-size: 17px; font-weight: 600; color: #1a1a1a; margin-bottom: 16px; padding-left: 14px; border-left: 4px solid #1890FF; }}
.sub-title {{ font-size: 14px; font-weight: 600; color: #555; margin: 16px 0 8px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 10px 0; }}
th {{ background: #f0f7ff; padding: 10px 10px; text-align: left; border-bottom: 2px solid #91d5ff; font-weight: 600; color: #333; }}
td {{ padding: 9px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
tr:hover {{ background: #fafcff; }}
.severity {{ display: inline-block; padding: 3px 10px; border-radius: 4px; color: white; font-size: 11px; font-weight: 700; min-width: 60px; text-align: center; }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 12px 0; }}
.stat-box {{ background: #f8f9fa; padding: 16px; border-radius: 10px; text-align: center; border: 1px solid #eee; }}
.stat-box .label {{ font-size: 12px; color: #888; margin-bottom: 4px; }}
.stat-box .value {{ font-size: 22px; font-weight: 700; color: #333; }}
.ok {{ color: #52C41A; }}
.warn {{ color: #FAAD14; }}
.fail {{ color: #FF4D4F; }}
.info {{ color: #1890FF; }}
.check-item {{ padding: 8px 12px; margin: 4px 0; background: #f6ffed; border-left: 3px solid #52C41A; border-radius: 0 6px 6px 0; font-size: 13px; line-height: 1.6; }}
.check-item b {{ color: #333; }}
.summary-box {{ background: #f0f7ff; border: 1px solid #91d5ff; border-radius: 10px; padding: 20px; font-size: 13px; line-height: 2.0; }}
.fix-box {{ background: #fff7e6; border: 1px solid #ffd591; border-radius: 8px; padding: 14px; font-size: 12px; line-height: 1.8; margin-top: 6px; }}
.detail-text {{ color: #666; font-size: 12px; line-height: 1.6; margin-top: 4px; }}
.footer {{ text-align: center; color: #bbb; font-size: 11px; margin-top: 24px; padding-top: 16px; border-top: 1px solid #eee; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
.badge-pass {{ background: #f6ffed; color: #52C41A; border: 1px solid #b7eb8f; }}
.badge-fail {{ background: #fff1f0; color: #FF4D4F; border: 1px solid #ffa39e; }}
</style></head><body>
<div class="container">
<div class="header">
    <h1>V10.2 回测引擎全面审计报告</h1>
    <div class="meta">
        审计日期: {today} | 数据源: SQLite stock_db.db (166只股票, 2020~2026, 6.5年)<br>
        审计范围: V2引擎(engine.py+broker.py) + V5引擎(backtest_real.py) + 绩效指标(metrics.py) + V10.2财报集成<br>
        发现Bug: {len(BUGS)}个 (CRITICAL:{severity_counts['CRITICAL']}, HIGH:{severity_counts['HIGH']}, MEDIUM:{severity_counts['MEDIUM']}, LOW:{severity_counts['LOW']}) | 通过检查: {len(PASSED_CHECKS)}项
    </div>
</div>
<div class="content">

<!-- ==================== 一、总览 ==================== -->
<div class="section">
    <div class="section-title">一、审计总览</div>
    <div class="stat-grid">
        <div class="stat-box"><div class="label">审计模块</div><div class="value info">6</div></div>
        <div class="stat-box"><div class="label">检查项</div><div class="value">{len(BUGS) + len(PASSED_CHECKS)}</div></div>
        <div class="stat-box"><div class="label">通过</div><div class="value ok">{len(PASSED_CHECKS)}</div></div>
        <div class="stat-box"><div class="label">Bug</div><div class="value fail">{len(BUGS)}</div></div>
        <div class="stat-box"><div class="label">CRITICAL</div><div class="value" style="color:#FF4D4F">{severity_counts['CRITICAL']}</div></div>
        <div class="stat-box"><div class="label">MEDIUM</div><div class="value" style="color:#FAAD14">{severity_counts['MEDIUM']}</div></div>
        <div class="stat-box"><div class="label">LOW</div><div class="value" style="color:#1890FF">{severity_counts['LOW']}</div></div>
    </div>
</div>

<!-- ==================== 二、V2引擎回测 ==================== -->
<div class="section">
    <div class="section-title">二、V2引擎 (BacktestEngineV2) 回测</div>
    <div class="stat-grid">
        <div class="stat-box"><div class="label">回测区间</div><div class="value" style="font-size:14px">{V2_RESULT['data_range']}</div></div>
        <div class="stat-box"><div class="label">交易日</div><div class="value">{V2_RESULT['trading_days']}</div></div>
        <div class="stat-box"><div class="label">股票数</div><div class="value">{V2_RESULT['stocks']}</div></div>
        <div class="stat-box"><div class="label">初始资金</div><div class="value" style="font-size:16px">{V2_RESULT['initial_capital']}</div></div>
    </div>
    <div style="background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;padding:12px;font-size:13px;margin-top:8px">
        <b>注意:</b> {V2_RESULT['note']}
    </div>
    <div class="sub-title">架构验证</div>
    <div class="check-item"><b>事件驱动:</b> 逐日循环(new_day -> get_price -> update_highest -> strategy -> execute -> record)，天然无未来函数</div>
    <div class="check-item"><b>T+1机制:</b> frozen_shares/frozen_date + new_day()解除冻结，验证通过</div>
    <div class="check-item"><b>涨跌停:</b> _board_is_limit_up/_board_is_limit_down区分板块(主板10%/创业板20%/ST 5%/科创板20%)</div>
    <div class="check-item"><b>FIFO匹配:</b> _calc_trade_pnl使用pop(0)先进先出匹配</div>
</div>

<!-- ==================== 三、V5引擎回测 ==================== -->
<div class="section">
    <div class="section-title">三、V5引擎 (backtest_real.py) 回测</div>
    <div class="sub-title">核心机制验证</div>
    <div class="check-item"><b>无未来函数:</b> T日收盘后计算信号(i日)，T+1日开盘价执行(i+1日) — exec_price = open_arr[i+1] * (1+slippage)</div>
    <div class="check-item"><b>止损:</b> 使用盘中最低价(low &lt;= stop_price)触发，符合实盘条件单逻辑</div>
    <div class="check-item"><b>回落止盈:</b> 要求highest_since_buy &gt; buy_price(盈利状态)，从高点回撤超阈值触发</div>
    <div class="check-item"><b>阶梯止盈:</b> 12%卖1/3, 25%再卖1/3, 底仓用回落止盈</div>
    <div class="check-item"><b>连续亏损熔断:</b> 3连亏暂停20天, 5连亏暂停60天, 6连亏永久禁止</div>
    <div class="check-item"><b>ATR自适应止损:</b> 约束[5%,10%], BEAR×1.5/BULL×2.0</div>
    <div class="check-item"><b>市场环境预计算:</b> 4维加权评分(趋势40%+波动率20%+量能20%+动量20%)</div>
</div>

<!-- ==================== 四、Bug清单 ==================== -->
<div class="section">
    <div class="section-title">四、Bug清单 ({len(BUGS)}个)</div>
    <table>
    <tr>
        <th style="width:60px">编号</th>
        <th style="width:70px">严重度</th>
        <th style="width:180px">文件</th>
        <th style="width:60px">行号</th>
        <th>问题描述</th>
    </tr>"""

for bug in BUGS:
    color = severity_colors.get(bug["severity"], "#888")
    html += f"""
    <tr>
        <td>{bug['id']}</td>
        <td><span class="severity" style="background:{color}">{bug['severity']}</span></td>
        <td style="font-size:12px;word-break:break-all">{bug['file']}</td>
        <td style="font-size:12px">{bug['line']}</td>
        <td>
            <b>{bug['description']}</b>
            <div class="detail-text">{bug['detail']}</div>
            <div style="margin-top:4px;font-size:12px"><b>触发条件:</b> {bug['trigger']}</div>
            <div style="font-size:12px"><b>影响:</b> {bug['impact']}</div>
            <div class="fix-box"><b>修复建议:</b> {bug['fix']}</div>
        </td>
    </tr>"""

html += """
    </table>
</div>

<!-- ==================== 五、通过检查项 ==================== -->
<div class="section">
    <div class="section-title">五、通过检查项 (""" + str(len(PASSED_CHECKS)) + """项)</div>
    <table>
    <tr><th style="width:40px">#</th><th style="width:160px">检查项</th><th>验证结果</th><th style="width:60px">状态</th></tr>"""

for idx, (name, result) in enumerate(PASSED_CHECKS, 1):
    html += f"""
    <tr>
        <td>{idx}</td>
        <td><b>{name}</b></td>
        <td style="font-size:12px">{result}</td>
        <td><span class="badge badge-pass">PASS</span></td>
    </tr>"""

html += """
    </table>
</div>

<!-- ==================== 六、V10.2财报集成验证 ==================== -->
<div class="section">
    <div class="section-title">六、V10.2财报模块集成验证</div>
    <div class="stat-grid">
        <div class="stat-box"><div class="label">模块导入</div><div class="value ok">OK</div></div>
        <div class="stat-box"><div class="label">配置开关</div><div class="value ok">OK</div></div>
        <div class="stat-box"><div class="label">无数据降级</div><div class="value ok">OK</div></div>
        <div class="stat-box"><div class="label">极端值防护</div><div class="value ok">OK</div></div>
        <div class="stat-box"><div class="label">因子隔离</div><div class="value ok">OK</div></div>
    </div>
    <div style="background:#f6ffed;border:1px solid #b7eb8f;border-radius:8px;padding:14px;font-size:13px;line-height:1.8;margin-top:8px">
        <b>结论:</b> V10.2财报深度分析模块(financial_report.py)集成安全。<br>
        - 五维评分(营收趋势0-25 + 利润质量0-25 + 毛利率0-20 + ROE杜邦0-15 + 负债结构0-15 = 0-100)映射到[-2,+4]不溢出<br>
        - canslim_score()中财报代码块仅叠加FR_财报因子，不影响N/S/L/CAI/V/W等原有因子<br>
        - 配置开关(FINANCIAL_REPORT_ENABLED)可正确禁用财报分析<br>
        - 无财报数据时自动降级为bonus=0
    </div>
</div>

<!-- ==================== 七、审计结论与建议 ==================== -->
<div class="section">
    <div class="section-title">七、审计结论与修复优先级</div>
    <div class="summary-box">
        <b>整体评估:</b> 系统架构设计合理，核心逻辑（无未来函数/T+1/涨跌停/止损止盈）验证通过。
        发现6个Bug均为MEDIUM/LOW级别，无CRITICAL/HIGH级致命缺陷。<br><br>
        
        <b>修复优先级:</b><br>
        1. <span class="severity" style="background:#FAAD14">MEDIUM</span> 
           <b>BUG-001 过户费遗漏</b> — 影响交易成本准确性，建议优先修复broker.py<br>
        2. <span class="severity" style="background:#FAAD14">MEDIUM</span> 
           <b>BUG-002 Sharpe浮点精度</b> — 影响绩效报告可靠性，修改metrics.py判断条件<br>
        3. <span class="severity" style="background:#1890FF">LOW</span> 
           <b>BUG-003/004 涨跌停板块区分</b> — V5引擎和末日平仓对齐V2引擎的board_rules<br>
        4. <span class="severity" style="background:#1890FF">LOW</span> 
           <b>BUG-005/006 边界/一致性问题</b> — 回测结束平仓滑点、双引擎成本口径统一<br><br>
        
        <b>未来函数:</b> 未发现。V5引擎正确使用T日信号T+1执行；V2引擎架构级防护(逐日事件驱动)。<br>
        <b>V10.2集成:</b> 财报模块隔离性验证通过，不影响其他因子计算。<br>
        <b>因子加减分:</b> 各因子范围合理，财报模块五维评分映射到[-2,+4]有clamp防护，无溢出风险。
    </div>
</div>

</div>
<div class="footer">
    V10.2 回测引擎审计报告 | 操盘密码量化交易系统 | """ + today + """
</div>
</div></body></html>"""

# 输出报告
output_dir = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(output_dir, exist_ok=True)
report_path = os.path.join(output_dir, f"v102_backtest_audit_{today.replace('-', '')}.html")
with open(report_path, "w", encoding="utf-8") as f:
    f.write(html)
print(f"HTML审计报告已生成: {report_path}")
print(f"文件大小: {os.path.getsize(report_path):,} bytes")
