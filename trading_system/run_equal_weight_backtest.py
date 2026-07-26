# -*- coding: utf-8 -*-
"""
等权分散投资回测验证
====================
基于V5.1回测引擎（backtest_stock_v5），验证策略在10只代表性个股上的独立表现。

回测方案:
  - 总资金: 100万元
  - 分配方式: 等权分配，每只股票10万元
  - 标的: 从STOCK_POOL/SECTOR_CANDIDATES中按行业分散选取10只
  - 区间: 2023-01-01 ~ 2026-07-25
  - 策略: V5.1（含市场环境自适应、ATR止损、急涨过滤等最新优化）
  - 成本: 佣金万2.5 + 印花税千1 + 滑点0.1%

输出:
  - 每只股票独立统计（交易笔数/胜率/盈亏比/累计收益/最大单笔/连续亏损）
  - 10只股票收益排名
  - 100万组合整体表现（加权收益/最大回撤/沪深300对比）
  - HTML报告 + 邮件发送
"""

import sys
import os
import time
import logging
import datetime
import sqlite3
import traceback
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(config.OUTPUT_DIR, f"equal_weight_backtest_{datetime.date.today().strftime('%Y%m%d')}.log"),
            encoding='utf-8'
        )
    ]
)
logger = logging.getLogger("equal_weight_backtest")

# ============================================================
# 回测参数
# ============================================================
TOTAL_CAPITAL_PORTFOLIO = 1_000_000   # 组合总资金100万
CAPITAL_PER_STOCK = 100_000           # 每只股票分配10万
START_DATE = "2023-01-01"
END_DATE = "2026-07-25"

# 10只标的：按行业分散选取（覆盖8个不同行业）
SELECTED_STOCKS = {
    "002371": {"名称": "北方华创", "赛道": "半导体设备", "类型": "龙头"},
    "002415": {"名称": "海康威视", "赛道": "AI视觉",     "类型": "龙头"},
    "600036": {"名称": "招商银行", "赛道": "银行",       "类型": "龙头"},
    "600276": {"名称": "恒瑞医药", "赛道": "创新药",     "类型": "龙头"},
    "002594": {"名称": "比亚迪",   "赛道": "新能源车",   "类型": "龙头"},
    "600519": {"名称": "贵州茅台", "赛道": "白酒",       "类型": "龙头"},
    "601899": {"名称": "紫金矿业", "赛道": "黄金铜矿",   "类型": "龙头"},
    "600760": {"名称": "中航沈飞", "赛道": "军工航空",   "类型": "龙头"},
    "002230": {"名称": "科大讯飞", "赛道": "AI应用",     "类型": "龙头"},
    "601012": {"名称": "隆基绿能", "赛道": "光伏",       "类型": "龙头"},
}


# ============================================================
# 一、数据加载
# ============================================================

def load_stock_data(codes: list) -> dict:
    """从SQLite批量加载股票历史数据（性能优化: 单次查询替代逐条SQL）"""
    if not os.path.exists(config.DB_PATH):
        logger.error(f"数据库不存在: {config.DB_PATH}")
        return {}

    conn = sqlite3.connect(config.DB_PATH)
    warmup_date = "2022-10-01"

    # 批量查询所有股票+沉深300（单次SQL替代N次查询）
    all_codes = list(codes) + ["000300"]
    placeholders = ",".join(["?"] * len(all_codes))
    query = f"""SELECT code, date, open, close, high, low, volume 
               FROM daily_kline WHERE code IN ({placeholders}) ORDER BY code, date ASC"""
    df_all = pd.read_sql(query, conn, params=all_codes)
    conn.close()

    # 按股票分组处理
    data_dict = {}
    for code, group in df_all.groupby("code"):
        df = group.copy()
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        if code == "000300":
            data_dict["000300"] = df
            logger.info(f"  000300 (沉深300): {len(df)}条数据")
        else:
            df = df[df["date"] >= warmup_date].reset_index(drop=True)
            if len(df) > 100:
                data_dict[code] = df
                logger.info(f"  {code} ({SELECTED_STOCKS.get(code, {}).get('名称', code)}): {len(df)}条数据")

    return data_dict


# ============================================================
# 二、逐股回测
# ============================================================

def run_per_stock_backtest(data_dict: dict) -> dict:
    """
    对每只股票独立运行V5.1回测
    
    关键: 通过临时调整backtest_real.TOTAL_CAPITAL使每笔仓位=10万
    原始公式: initial_shares = TOTAL_CAPITAL * 0.12 / price
    目标: TOTAL_CAPITAL * 0.12 = 100,000 → TOTAL_CAPITAL = 833,333
    """
    import backtest_real
    from backtest_real import backtest_stock_v5, analyze_trades

    # 临时修改TOTAL_CAPITAL使每笔仓位≈10万（不修改源文件，仅运行时调整）
    original_capital = backtest_real.TOTAL_CAPITAL
    backtest_real.TOTAL_CAPITAL = CAPITAL_PER_STOCK / 0.12  # ≈833,333

    benchmark_df = data_dict.get("000300", None)
    results = {}

    stock_codes = [c for c in SELECTED_STOCKS.keys() if c in data_dict]
    logger.info(f"  开始逐股回测: {len(stock_codes)}只, 每只分配{CAPITAL_PER_STOCK:,.0f}元")

    for i, code in enumerate(stock_codes, 1):
        df = data_dict[code]
        info = SELECTED_STOCKS[code]
        name = info["名称"]

        try:
            # backtest_stock_v5需要info包含"行业"键
            info_dict = {"名称": info["名称"], "类型": info["类型"], "行业": info["赛道"]}
            trades = backtest_stock_v5(df, code, info_dict, benchmark_df=benchmark_df)
            stats = analyze_trades(trades) if trades else {"error": "无交易"}

            # 计算基于10万本金的累计收益
            total_pnl_pct = sum(t["net_profit"] for t in trades) if trades else 0
            # 每笔交易使用约10万，net_profit是百分比，转为绝对金额
            total_pnl_amount = sum(t["net_profit"] / 100 * CAPITAL_PER_STOCK for t in trades) if trades else 0
            final_capital = CAPITAL_PER_STOCK + total_pnl_amount

            # 最大单笔盈利/亏损
            if trades:
                max_win = max(t["net_profit"] for t in trades)
                max_loss = min(t["net_profit"] for t in trades)
            else:
                max_win = max_loss = 0

            # 最大连续亏损
            max_consec_loss = 0
            current_consec = 0
            for t in trades:
                if t["net_profit"] < 0:
                    current_consec += 1
                    max_consec_loss = max(max_consec_loss, current_consec)
                else:
                    current_consec = 0

            results[code] = {
                "name": name,
                "sector": info["赛道"],
                "trades": trades,
                "stats": stats,
                "trade_count": len(trades),
                "win_rate": stats.get("win_rate", 0),
                "profit_factor": stats.get("profit_factor", 0),
                "total_pnl_pct": total_pnl_pct,
                "total_pnl_amount": total_pnl_amount,
                "final_capital": final_capital,
                "return_pct": (final_capital - CAPITAL_PER_STOCK) / CAPITAL_PER_STOCK * 100,
                "max_win": max_win,
                "max_loss": max_loss,
                "max_consec_loss": max_consec_loss,
                "avg_hold_days": stats.get("avg_hold", 0),
            }

            logger.info(f"    [{i}/{len(stock_codes)}] {name}: {len(trades)}笔, "
                       f"胜率{results[code]['win_rate']}%, "
                       f"收益{results[code]['return_pct']:+.1f}%, "
                       f"10万→{final_capital/10000:.2f}万")

        except Exception as e:
            logger.error(f"    [{i}/{len(stock_codes)}] {name} 回测失败: {e}")
            traceback.print_exc()
            results[code] = {"name": name, "sector": info["赛道"], "error": str(e)}

    # 恢复原始TOTAL_CAPITAL
    backtest_real.TOTAL_CAPITAL = original_capital

    return results


# ============================================================
# 三、组合表现计算
# ============================================================

def calc_portfolio_performance(results: dict, data_dict: dict) -> dict:
    """计算100万组合的整体表现"""

    # 汇总所有交易
    all_trades = []
    for code, r in results.items():
        if "error" not in r and r.get("trades"):
            all_trades.extend(r["trades"])

    if not all_trades:
        return {"error": "无交易"}

    # 组合总收益
    total_pnl = sum(r.get("total_pnl_amount", 0) for r in results.values() if "error" not in r)
    portfolio_return = total_pnl / TOTAL_CAPITAL_PORTFOLIO * 100

    # 组合胜率/盈亏比
    wins = [t for t in all_trades if t["net_profit"] > 0]
    losses = [t for t in all_trades if t["net_profit"] <= 0]
    win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
    avg_win = np.mean([t["net_profit"] for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t["net_profit"] for t in losses])) if losses else 1
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

    # 组合最大回撤（基于月度收益曲线近似）
    # 按卖出日期排序，累计PnL构建权益曲线
    all_trades_sorted = sorted(all_trades, key=lambda x: x.get("sell_date", ""))
    equity = TOTAL_CAPITAL_PORTFOLIO
    peak = equity
    max_drawdown = 0
    equity_curve = []

    for t in all_trades_sorted:
        pnl_amount = t["net_profit"] / 100 * CAPITAL_PER_STOCK
        equity += pnl_amount
        peak = max(peak, equity)
        dd = (peak - equity) / peak
        max_drawdown = max(max_drawdown, dd)
        equity_curve.append({"date": t["sell_date"], "equity": equity})

    # 沪深300基准收益
    benchmark_return = 0
    if "000300" in data_dict:
        df_300 = data_dict["000300"]
        df_period = df_300[(df_300["date"] >= START_DATE) & (df_300["date"] <= END_DATE)]
        if len(df_period) > 1:
            start_price = df_period.iloc[0]["close"]
            end_price = df_period.iloc[-1]["close"]
            benchmark_return = (end_price - start_price) / start_price * 100

    return {
        "total_pnl": total_pnl,
        "portfolio_return": portfolio_return,
        "final_equity": TOTAL_CAPITAL_PORTFOLIO + total_pnl,
        "total_trades": len(all_trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": -abs(avg_loss),
        "max_drawdown": max_drawdown * 100,
        "benchmark_return": benchmark_return,
        "excess_return": portfolio_return - benchmark_return,
        "equity_curve": equity_curve,
    }


# ============================================================
# 四、HTML报告生成
# ============================================================

def generate_report(results: dict, portfolio: dict, data_dict: dict) -> str:
    """生成等权分散投资回测HTML报告"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M:%S")

    # === 每只股票明细行 ===
    stock_rows = ""
    # 按收益率排序
    sorted_stocks = sorted(
        [(code, r) for code, r in results.items() if "error" not in r],
        key=lambda x: -x[1].get("return_pct", 0)
    )

    for rank, (code, r) in enumerate(sorted_stocks, 1):
        color = "#e74c3c" if r["return_pct"] > 0 else "#27ae60"
        stock_rows += f"""<tr>
            <td>{rank}</td>
            <td>{code}</td>
            <td><b>{r['name']}</b></td>
            <td>{r['sector']}</td>
            <td>{r['trade_count']}</td>
            <td>{r['win_rate']:.0f}%</td>
            <td>{r['profit_factor']:.2f}</td>
            <td style="color:{color};font-weight:bold">{r['return_pct']:+.1f}%</td>
            <td>{r['final_capital']/10000:.2f}万</td>
            <td style="color:#e74c3c">{r['max_win']:+.1f}%</td>
            <td style="color:#27ae60">{r['max_loss']:.1f}%</td>
            <td>{r['max_consec_loss']}</td>
            <td>{r['avg_hold_days']:.0f}天</td>
        </tr>"""

    # === 具体数字说明 ===
    example_lines = ""
    for code, r in sorted_stocks[:5]:
        example_lines += f"<li><b>{r['name']}</b>：10万→{r['final_capital']/10000:.2f}万，收益率{r['return_pct']:+.1f}%，胜率{r['win_rate']:.0f}%，{r['trade_count']}笔交易</li>\n"

    # === 月度组合收益 ===
    monthly_rows = ""
    all_trades = []
    for code, r in results.items():
        if "error" not in r and r.get("trades"):
            all_trades.extend(r["trades"])

    if all_trades:
        monthly_pnl = {}
        for t in all_trades:
            mk = t["sell_date"][:7]
            if mk not in monthly_pnl:
                monthly_pnl[mk] = {"count": 0, "total": 0, "wins": 0}
            monthly_pnl[mk]["count"] += 1
            monthly_pnl[mk]["total"] += t["net_profit"] / 100 * CAPITAL_PER_STOCK
            if t["net_profit"] > 0:
                monthly_pnl[mk]["wins"] += 1

        for mk in sorted(monthly_pnl.keys()):
            d = monthly_pnl[mk]
            wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
            color = "#e74c3c" if d["total"] > 0 else "#27ae60"
            monthly_rows += f"""<tr>
                <td>{mk}</td><td>{d['count']}</td><td>{wr:.0f}%</td>
                <td style="color:{color};font-weight:bold">{d['total']:+,.0f}元</td>
            </tr>"""

    # === 卖出原因分布 ===
    sell_reason_rows = ""
    if all_trades:
        sell_stats = {}
        for t in all_trades:
            st = t.get("sell_type", "未知")
            if st not in sell_stats:
                sell_stats[st] = {"count": 0, "wins": 0, "total": 0}
            sell_stats[st]["count"] += 1
            sell_stats[st]["total"] += t["net_profit"]
            if t["net_profit"] > 0:
                sell_stats[st]["wins"] += 1

        for st, d in sorted(sell_stats.items(), key=lambda x: -x[1]["count"]):
            wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
            avg = d["total"] / d["count"] if d["count"] > 0 else 0
            sell_reason_rows += f"""<tr>
                <td>{st}</td><td>{d['count']}</td><td>{wr:.0f}%</td><td>{avg:+.2f}%</td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 15px; background: #f0f2f5; }}
.container {{ max-width: 1100px; margin: 0 auto; }}
.header {{ background: linear-gradient(135deg, #1a5276, #154360); color: white; padding: 20px 25px; border-radius: 10px 10px 0 0; }}
.header h1 {{ margin: 0; font-size: 20px; }}
.header .sub {{ font-size: 12px; opacity: 0.8; margin-top: 5px; }}
.content {{ background: white; padding: 20px 25px; border-radius: 0 0 10px 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
.panel {{ border: 1px solid #e8e8e8; border-radius: 8px; margin: 15px 0; overflow: hidden; }}
.panel-title {{ background: #fafafa; padding: 10px 15px; font-weight: bold; font-size: 14px; border-bottom: 1px solid #e8e8e8; }}
.panel-body {{ padding: 12px 15px; }}
.grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }}
.grid-6 {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }}
.metric {{ text-align: center; padding: 10px; background: #f8f9fa; border-radius: 6px; }}
.metric .label {{ font-size: 11px; color: #888; }}
.metric .value {{ font-size: 18px; font-weight: bold; margin-top: 3px; }}
.metric .value.up {{ color: #e74c3c; }}
.metric .value.down {{ color: #27ae60; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin: 8px 0; }}
th {{ background: #2c3e50; color: white; padding: 8px 5px; text-align: center; }}
td {{ padding: 7px 5px; border-bottom: 1px solid #eee; text-align: center; }}
tr:nth-child(even) {{ background: #f8f9fa; }}
.footer {{ text-align: center; color: #999; font-size: 11px; margin-top: 15px; padding-top: 10px; border-top: 1px solid #eee; }}
.highlight {{ background: #fef9e7; border: 2px solid #f39c12; border-radius: 8px; padding: 15px; margin: 15px 0; }}
.highlight h3 {{ margin: 0 0 10px; color: #d35400; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>💼 等权分散投资回测验证报告（V5.1策略）</h1>
        <div class="sub">回测区间: {START_DATE} ~ {END_DATE} | 总资金: {TOTAL_CAPITAL_PORTFOLIO:,.0f}元 | 每只: {CAPITAL_PER_STOCK:,.0f}元 × 10只 | 生成: {today} {now}</div>
    </div>
    <div class="content">

        <!-- 1. 组合整体表现 -->
        <div class="panel" style="border: 2px solid #27ae60;">
            <div class="panel-title" style="background: #d4edda;">📊 一、100万组合整体表现</div>
            <div class="panel-body">
                <div class="grid-6">
                    <div class="metric"><div class="label">组合累计收益</div><div class="value {'up' if portfolio.get('portfolio_return',0)>0 else 'down'}">{portfolio.get('portfolio_return',0):+.1f}%</div></div>
                    <div class="metric"><div class="label">最终权益</div><div class="value">{portfolio.get('final_equity',0)/10000:.1f}万</div></div>
                    <div class="metric"><div class="label">组合最大回撤</div><div class="value down">-{portfolio.get('max_drawdown',0):.1f}%</div></div>
                    <div class="metric"><div class="label">总交易笔数</div><div class="value">{portfolio.get('total_trades',0)}</div></div>
                    <div class="metric"><div class="label">组合胜率</div><div class="value">{portfolio.get('win_rate',0):.1f}%</div></div>
                    <div class="metric"><div class="label">盈亏比</div><div class="value">{portfolio.get('profit_factor',0):.2f}</div></div>
                </div>
                <div class="grid" style="margin-top:10px">
                    <div class="metric"><div class="label">沪深300收益</div><div class="value">{portfolio.get('benchmark_return',0):+.1f}%</div></div>
                    <div class="metric"><div class="label">超额收益</div><div class="value {'up' if portfolio.get('excess_return',0)>0 else 'down'}">{portfolio.get('excess_return',0):+.1f}%</div></div>
                    <div class="metric"><div class="label">平均盈利</div><div class="value up">+{portfolio.get('avg_win',0):.2f}%</div></div>
                    <div class="metric"><div class="label">平均亏损</div><div class="value down">{portfolio.get('avg_loss',0):.2f}%</div></div>
                </div>
            </div>
        </div>

        <!-- 2. 具体数字说明 -->
        <div class="highlight">
            <h3>📌 核心结论（具体数字）</h3>
            <ul style="font-size:14px;line-height:2;margin:0;padding-left:20px">
                {example_lines}
            </ul>
            <p style="font-size:13px;margin-top:10px;color:#555">
                <b>组合总结:</b> 100万等权投入10只行业龙头，{START_DATE}~{END_DATE}期间
                累计盈亏<b>{portfolio.get('total_pnl',0):+,.0f}元</b>，
                收益率<b>{portfolio.get('portfolio_return',0):+.1f}%</b>，
                同期沪深300收益{portfolio.get('benchmark_return',0):+.1f}%，
                超额收益{portfolio.get('excess_return',0):+.1f}%。
            </p>
        </div>

        <!-- 3. 10只股票收益排名 -->
        <div class="panel">
            <div class="panel-title">🏆 二、10只股票收益排名（从高到低）</div>
            <div class="panel-body" style="overflow-x:auto">
                <table>
                    <tr>
                        <th>排名</th><th>代码</th><th>名称</th><th>行业</th>
                        <th>交易数</th><th>胜率</th><th>盈亏比</th>
                        <th>收益率</th><th>10万→</th>
                        <th>最大盈利</th><th>最大亏损</th><th>连亏</th><th>平均持仓</th>
                    </tr>
                    {stock_rows}
                </table>
            </div>
        </div>

        <!-- 4. 月度组合收益 -->
        <div class="panel">
            <div class="panel-title">📅 三、月度组合收益分布</div>
            <div class="panel-body">
                <table>
                    <tr><th>月份</th><th>交易笔数</th><th>胜率</th><th>月度盈亏(元)</th></tr>
                    {monthly_rows}
                </table>
            </div>
        </div>

        <!-- 5. 卖出原因分布 -->
        <div class="panel">
            <div class="panel-title">🏷️ 四、卖出原因分布</div>
            <div class="panel-body">
                <table>
                    <tr><th>卖出原因</th><th>次数</th><th>胜率</th><th>平均收益</th></tr>
                    {sell_reason_rows}
                </table>
            </div>
        </div>

        <!-- 6. 回测说明 -->
        <div class="panel">
            <div class="panel-title">📋 五、回测配置说明</div>
            <div class="panel-body">
                <table>
                    <tr><th>项目</th><th>配置</th><th>说明</th></tr>
                    <tr><td>策略版本</td><td>V5.1</td><td>含市场环境自适应、ATR止损、急涨过滤</td></tr>
                    <tr><td>总资金</td><td>100万元</td><td>等权分配10只，每只10万</td></tr>
                    <tr><td>佣金</td><td>万2.5 (双边)</td><td>买卖各收0.025%</td></tr>
                    <tr><td>印花税</td><td>千1 (仅卖出)</td><td>卖出时收取0.1%</td></tr>
                    <tr><td>滑点</td><td>龙头0.2%</td><td>模拟真实成交偏差</td></tr>
                    <tr><td>T+1</td><td>✅ 启用</td><td>当日买入次日才能卖出</td></tr>
                    <tr><td>涨跌停</td><td>✅ 启用</td><td>涨停无法买入，跌停无法卖出</td></tr>
                    <tr><td>市场环境</td><td>✅ 自适应</td><td>BEAR收紧/ BULL放宽</td></tr>
                    <tr><td>止损方式</td><td>ATR自适应</td><td>2×ATR，约束[4%,10%]</td></tr>
                </table>
            </div>
        </div>

        <div class="footer">
            本报告由交易系统自动生成 | 仅供参考，不构成投资建议<br>
            股市有风险，投资需谨慎 | 等权分散投资回测 V5.1 | {today}
        </div>
    </div>
</div>
</body>
</html>"""

    return html


# ============================================================
# 五、主函数
# ============================================================

def run():
    """执行等权分散投资回测"""
    total_start = time.time()

    print("=" * 60)
    print("  等权分散投资回测验证（10只行业龙头 × 10万/只）")
    print(f"  区间: {START_DATE} ~ {END_DATE}")
    print(f"  策略: V5.1（市场环境自适应 + ATR止损）")
    print("=" * 60)

    # 1. 加载数据
    logger.info("【步骤1】加载历史数据...")
    codes = list(SELECTED_STOCKS.keys()) + ["000300"]
    data_dict = load_stock_data(codes)
    if len(data_dict) < 5:
        logger.error("数据不足，回测终止")
        return None

    # 2. 逐股回测
    logger.info("【步骤2】运行V5.1逐股回测...")
    results = run_per_stock_backtest(data_dict)

    # 3. 组合表现
    logger.info("【步骤3】计算组合整体表现...")
    portfolio = calc_portfolio_performance(results, data_dict)
    if "error" not in portfolio:
        logger.info(f"  组合收益: {portfolio['portfolio_return']:+.1f}%, "
                   f"最大回撤: -{portfolio['max_drawdown']:.1f}%, "
                   f"沪深300: {portfolio['benchmark_return']:+.1f}%")

    # 4. 生成报告
    logger.info("【步骤4】生成HTML报告...")
    html = generate_report(results, portfolio, data_dict)

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    report_path = os.path.join(config.OUTPUT_DIR, f"equal_weight_backtest_{datetime.date.today().strftime('%Y%m%d')}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"  报告已保存: {report_path}")

    # 5. 发送邮件
    logger.info("【步骤5】发送邮件...")
    try:
        from notify.email_notify import send_email
        subject = f"[等权回测] 10只行业龙头×10万 V5.1策略验证 | {START_DATE}~{END_DATE}"
        result = send_email(subject, html)
        if result:
            logger.info("  ✅ 邮件发送成功")
        else:
            logger.warning("  ⚠️ 邮件发送失败")
    except Exception as e:
        logger.error(f"  邮件发送异常: {e}")

    total_elapsed = time.time() - total_start
    print(f"\n{'=' * 60}")
    print(f"  回测完成! 总耗时: {total_elapsed:.1f}秒")
    print(f"  报告: {report_path}")
    print(f"{'=' * 60}")

    return report_path


if __name__ == "__main__":
    run()
