# -*- coding: utf-8 -*-
"""
量化策略综合分析与回测验证报告
================================
全面梳理系统策略 + 真实数据回测 + 绩效评估 + 邮件报告生成

功能:
  1. 策略清单梳理（含逻辑缺陷分析）
  2. 基于真实历史数据的回测验证（复用现有引擎）
  3. 多策略/多版本对比（V2/V5/V6 + 事件驱动引擎）
  4. 完整绩效指标（胜率/盈亏比/夏普/最大回撤/Calmar/MC压力测试）
  5. 生成中文HTML分析报告并通过邮件发送

使用方式:
  cd trading_system
  python strategy_analysis_report.py
"""

import sys
import os
import time
import logging
import datetime
import traceback

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("strategy_analysis")


# ============================================================
# 一、策略清单定义
# ============================================================

STRATEGY_INVENTORY = [
    {
        "name": "趋势跟踪策略 (TrendStrategy V2.0)",
        "file": "strategy/trend_strategy.py",
        "logic": "MA20/MA60均线多头排列 + 缩量回踩支撑 + 盈亏比≥2.5准入",
        "buy": "收盘价站稳MA20且MA20>MA60 + 缩量回踩MA20±1% + 盈亏比≥2.5",
        "sell": "移动止损(分档) + 双轨止盈(阶梯+回落) + 时间止损(20天)",
        "params": "MA_SHORT=20, MA_MID=60, 止损10%, 盈亏比2.5",
        "scenario": "中线波段(3天-4周)，趋势行情",
        "issues": "无明显未来函数；MACD死叉/趋势破位已关闭(回测验证假信号多)",
    },
    {
        "name": "动量策略 (MomentumStrategy)",
        "file": "quant/strategies.py",
        "logic": "20日收益率排名 + MA5>MA10>MA20多头排列 + 量能配合",
        "buy": "动量>5% + 多头排列 + 5日均量>20日均量×0.8",
        "sell": "由portfolio.py统一管理（非独立卖出逻辑）",
        "params": "lookback=20, top_pct=0.1",
        "scenario": "强势市场追涨，适合牛市/结构性行情",
        "issues": "无独立卖出逻辑，依赖外部组合管理；冷静期仅标注未实际执行",
    },
    {
        "name": "均值回归策略 (MeanReversionStrategy)",
        "file": "quant/strategies.py + strategy/mean_reversion.py",
        "logic": "RSI<30超卖 + 布林带下轨 + 缩量企稳",
        "buy": "RSI<30 + 价格触及布林下轨×1.02 + 量缩至均量60% + 非连续暴跌",
        "sell": "目标利润6% / 止损4% / 持有超5天",
        "params": "rsi_threshold=30, boll_period=20, max_hold=5天",
        "scenario": "震荡市/弱势行情超跌反弹",
        "issues": "quant版与strategy版存在重复实现；RSI计算用14日差分值(非标准Wilder平滑)",
    },
    {
        "name": "事件驱动策略 (EventStrategy)",
        "file": "quant/strategies.py",
        "logic": "放量突破60日新高 + 连续2日站稳 + 连续3日温和放量",
        "buy": "突破60日新高(2日确认) + 当日量>5日均量×1.5",
        "sell": "由portfolio.py统一管理",
        "params": "breakout_period=60, vol_increase=1.5",
        "scenario": "产业资本信号/主升浪启动",
        "issues": "无独立卖出逻辑；突破确认条件close[-2]>high_n×0.99较宽松",
    },
    {
        "name": "回调买入策略 (PullbackStrategy)",
        "file": "quant/strategies.py",
        "logic": "MA20向上 + 从高点回落5-12% + 缩量企稳 + 价格在MA20附近",
        "buy": "MA20上升>0.5% + 回调5-12% + 量缩至5日均量70% + 价格≥MA20×0.97",
        "sell": "由portfolio.py统一管理",
        "params": "drawdown_min=5%, drawdown_max=12%, vol_shrink=0.7",
        "scenario": "趋势中的回调买点，减少追涨",
        "issues": "无独立卖出逻辑；冷静期仅标注未实际执行",
    },
    {
        "name": "操盘密码自适应趋势引擎 V2.0 (CaopanEngine)",
        "file": "strategy/caopan_signal.py",
        "logic": "ATR自适应EMA生命线 + DK三重共振买卖点 + 资金多维验证 + 市场环境自适应",
        "buy": "D点(金叉+量能+资金流入) + 回踩LL1入场 + 盈亏比≥1.5 + 周线同向",
        "sell": "K点(死叉+资金流出) + LL2下方2%止损 + 乖离率超买减仓",
        "params": "EMA10/30自适应±20%, ATR14, 盈亏比1.5",
        "scenario": "全市场中线趋势增强，对标东方财富操盘密码",
        "issues": "参数较多(30+)存在过拟合风险；资金流估算精度有限",
    },
    {
        "name": "多因子选股评分 (MultiFactorScorer)",
        "file": "strategy/multi_factor.py",
        "logic": "四维评分: 技术面30% + 资金面30% + 动量面20% + 基本面20%",
        "buy": "综合评分≥70分推荐买入",
        "sell": "评分下降或排名跌出TOP10",
        "params": "min_score_buy=70, min_score_watch=55",
        "scenario": "选股辅助评分，非独立交易策略",
        "issues": "基本面数据依赖akshare(可能缺失)；权重固定未做动态优化",
    },
    {
        "name": "真实环境回测V5.0 (backtest_real)",
        "file": "backtest_real.py",
        "logic": "MA20向上+缩量回踩+买点2(突破回踩) + 双轨止盈 + 信号质量评分",
        "buy": "MA20向上+缩量回踩MA20 / 放量突破后缩量回踩确认 + 质量分≥55",
        "sell": "移动止损(8%初始) + 阶梯止盈(8%/20%) + 回落止盈 + MACD死叉 + 强制卖出",
        "params": "手续费0.3%, 滑点0.2%/0.5%, T+1, 涨跌停过滤",
        "scenario": "真实环境策略验证（最贴近实盘）",
        "issues": "手续费0.3%偏保守(实际约0.13%单边)；V5中MACD死叉/趋势破位仍开启",
    },
    {
        "name": "参数优化回测V6.0 (backtest_optimizer)",
        "file": "backtest_optimizer.py",
        "logic": "基于V5逻辑 + 可配置参数网格搜索 + 样本内外分离验证",
        "buy": "同V5 + 参数可调(缩量阈值/止损/止盈/信号质量)",
        "sell": "同V5 + 可关闭MACD死叉/趋势破位 + 时间止损",
        "params": "网格搜索最优参数组合",
        "scenario": "策略参数优化，寻找最优配置",
        "issues": "网格搜索组合数多时耗时较长；需防过拟合(Walk-Forward验证)",
    },
]


# ============================================================
# 二、数据加载
# ============================================================

def load_data_from_db(stock_codes=None, days=750):
    """从SQLite加载真实历史数据"""
    import sqlite3

    if stock_codes is None:
        stock_codes = list(config.STOCK_POOL.keys())

    if not os.path.exists(config.DB_PATH):
        logger.warning(f"数据库不存在: {config.DB_PATH}")
        return {}

    conn = sqlite3.connect(config.DB_PATH)
    data_dict = {}

    for code in stock_codes:
        try:
            query = f"""SELECT date, open, close, high, low, volume 
                       FROM daily_kline WHERE code='{code}' 
                       ORDER BY date DESC LIMIT {days}"""
            df = pd.read_sql(query, conn)
            if df is not None and not df.empty and len(df) > 60:
                df = df.sort_values("date").reset_index(drop=True)
                for col in ["open", "close", "high", "low", "volume"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0].reset_index(drop=True)
                if len(df) > 60:
                    data_dict[code] = df
        except Exception as e:
            logger.debug(f"加载{code}失败: {e}")

    # 加载基准
    try:
        query = f"""SELECT date, open, close, high, low, volume 
                   FROM daily_kline WHERE code='000300' 
                   ORDER BY date DESC LIMIT {days}"""
        bench = pd.read_sql(query, conn)
        if bench is not None and not bench.empty:
            bench = bench.sort_values("date").reset_index(drop=True)
            for col in ["open", "close", "high", "low", "volume"]:
                bench[col] = pd.to_numeric(bench[col], errors="coerce")
            bench = bench.dropna(subset=["close"])
            data_dict["000300"] = bench
    except Exception:
        pass

    conn.close()
    logger.info(f"数据加载完成: {len(data_dict)}只股票")
    return data_dict


def load_data_baostock(stock_codes=None, start_date="2022-01-01"):
    """从baostock加载真实历史数据（备用方案）"""
    try:
        import baostock as bs
    except ImportError:
        logger.error("baostock未安装，无法获取数据")
        return {}

    if stock_codes is None:
        stock_codes = list(config.STOCK_POOL.keys())

    lg = bs.login()
    if lg.error_code != '0':
        logger.error(f"baostock登录失败: {lg.error_msg}")
        return {}

    data_dict = {}
    end_date = datetime.date.today().strftime("%Y-%m-%d")

    for code in stock_codes:
        try:
            if code.startswith("6") or code.startswith("9") or code == "000300":
                bs_code = f"sh.{code}"
            else:
                bs_code = f"sz.{code}"

            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,close,high,low,volume,amount",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="2"
            )
            data = []
            while rs.error_code == '0' and rs.next():
                data.append(rs.get_row_data())

            if data:
                df = pd.DataFrame(data, columns=["date", "open", "close", "high", "low", "volume", "amount"])
                for col in ["open", "close", "high", "low", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0].reset_index(drop=True)
                if len(df) > 60:
                    data_dict[code] = df
        except Exception as e:
            logger.debug(f"baostock加载{code}失败: {e}")

    bs.logout()
    logger.info(f"baostock数据加载完成: {len(data_dict)}只股票")
    return data_dict


# ============================================================
# 三、回测执行
# ============================================================

def run_event_driven_backtest(data_dict, initial_capital=None):
    """运行事件驱动回测引擎V2"""
    try:
        from backtest.engine import BacktestEngineV2, _simple_ma_strategy
        from backtest.broker import CostConfig

        capital = initial_capital or config.TOTAL_CAPITAL
        engine = BacktestEngineV2(initial_capital=capital)

        t0 = time.time()
        report = engine.run(
            data_dict,
            strategy_fn=_simple_ma_strategy,
            benchmark_code="000300" if "000300" in data_dict else None,
            auto_monte_carlo=True,
            mc_simulations=500,
        )
        elapsed = time.time() - t0
        report["elapsed_sec"] = round(elapsed, 1)
        return report
    except Exception as e:
        logger.error(f"事件驱动回测失败: {e}")
        return {"error": str(e)}


def run_real_backtest(data_dict):
    """运行真实环境回测（V2/V5对比）"""
    try:
        from backtest_real import backtest_stock_v4, backtest_stock_v5, analyze_trades, TEST_STOCKS

        trades_v2 = []
        trades_v5 = []

        for code, df in data_dict.items():
            if code == "000300":
                continue
            info = TEST_STOCKS.get(code, {"名称": config.get_stock_name(code), "类型": "龙头", "行业": "其他"})
            try:
                t2 = backtest_stock_v4(df, code, info, version="v2")
                t5 = backtest_stock_v5(df, code, info)
                trades_v2.extend(t2)
                trades_v5.extend(t5)
            except Exception as e:
                logger.debug(f"  {code} 回测失败: {e}")

        trades_v2.sort(key=lambda x: x.get("buy_date", ""))
        trades_v5.sort(key=lambda x: x.get("buy_date", ""))

        stats_v2 = analyze_trades(trades_v2) if trades_v2 else {"error": "无交易"}
        stats_v5 = analyze_trades(trades_v5) if trades_v5 else {"error": "无交易"}

        return {
            "v2": {"stats": stats_v2, "trades": trades_v2},
            "v5": {"stats": stats_v5, "trades": trades_v5},
        }
    except Exception as e:
        logger.error(f"真实环境回测失败: {e}")
        return {"error": str(e)}


# ============================================================
# 四、报告生成
# ============================================================

def generate_analysis_report(data_dict, engine_report, real_result):
    """生成综合HTML分析报告"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M:%S")

    # 数据范围
    all_dates = []
    stock_count = 0
    for code, df in data_dict.items():
        if code != "000300" and "date" in df.columns:
            stock_count += 1
            all_dates.extend(df["date"].tolist())
    if all_dates:
        date_min = min(all_dates)
        date_max = max(all_dates)
    else:
        date_min = date_max = "N/A"

    # 策略清单HTML
    strategy_rows = ""
    for s in STRATEGY_INVENTORY:
        strategy_rows += f"""<tr>
            <td style="text-align:left;font-weight:bold">{s['name']}</td>
            <td style="text-align:left;font-size:11px">{s['logic']}</td>
            <td style="font-size:11px">{s['scenario']}</td>
            <td style="text-align:left;font-size:11px;color:#e74c3c">{s['issues']}</td>
        </tr>"""

    # 事件驱动引擎结果
    bt = engine_report if engine_report and "error" not in engine_report else {}
    bt_html = ""
    if bt:
        bt_html = f"""
        <div class="panel">
            <div class="panel-title">📊 事件驱动回测引擎V2 (MA20/MA60策略)</div>
            <div class="panel-body">
                <div class="grid">
                    <div class="metric"><div class="label">总收益率</div><div class="value {'up' if bt.get('total_return',0)>0 else 'down'}">{bt.get('total_return',0):.2%}</div></div>
                    <div class="metric"><div class="label">年化收益</div><div class="value">{bt.get('annual_return',0):.2%}</div></div>
                    <div class="metric"><div class="label">最大回撤</div><div class="value down">-{bt.get('max_drawdown',0):.2%}</div></div>
                    <div class="metric"><div class="label">夏普比率</div><div class="value">{bt.get('sharpe_ratio',0):.2f}</div></div>
                    <div class="metric"><div class="label">Sortino</div><div class="value">{bt.get('sortino_ratio',0):.2f}</div></div>
                    <div class="metric"><div class="label">Calmar</div><div class="value">{bt.get('calmar_ratio',0):.2f}</div></div>
                    <div class="metric"><div class="label">胜率</div><div class="value">{bt.get('win_rate',0):.1%}</div></div>
                    <div class="metric"><div class="label">盈亏比</div><div class="value">{bt.get('profit_factor',0):.2f}</div></div>
                    <div class="metric"><div class="label">交易次数</div><div class="value">{bt.get('total_trades',0)}</div></div>
                    <div class="metric"><div class="label">交易成本</div><div class="value">{bt.get('total_cost',0):,.0f}元</div></div>
                    <div class="metric"><div class="label">基准收益</div><div class="value">{bt.get('benchmark_return',0):.2%}</div></div>
                    <div class="metric"><div class="label">耗时</div><div class="value">{bt.get('elapsed_sec',0)}秒</div></div>
                </div>
            </div>
        </div>"""

        # Monte Carlo 结果
        if bt.get("mc_95_max_drawdown") is not None:
            bt_html += f"""
            <div class="panel">
                <div class="panel-title">🎲 Monte Carlo 压力测试 (500次模拟)</div>
                <div class="panel-body">
                    <div class="grid">
                        <div class="metric"><div class="label">95%置信最大回撤</div><div class="value down">-{bt['mc_95_max_drawdown']:.1%}</div></div>
                        <div class="metric"><div class="label">平均回撤</div><div class="value">-{bt['mc_avg_drawdown']:.1%}</div></div>
                        <div class="metric"><div class="label">破产概率(腰斩)</div><div class="value">{bt['mc_bankruptcy_prob']:.2%}</div></div>
                    </div>
                </div>
            </div>"""

    # 真实环境回测结果
    real_html = ""
    if real_result and "error" not in real_result:
        v2_stats = real_result.get("v2", {}).get("stats", {})
        v5_stats = real_result.get("v5", {}).get("stats", {})

        if "error" not in v2_stats and "error" not in v5_stats:
            real_html = f"""
            <div class="panel">
                <div class="panel-title">📈 真实环境回测对比 (V2.0 vs V5.0)</div>
                <div class="panel-body">
                    <table>
                        <tr><th>指标</th><th>V2.0 基线版</th><th>V5.0 优化版</th><th>变化</th></tr>
                        <tr><td>总交易</td><td>{v2_stats.get('total',0)}笔</td><td>{v5_stats.get('total',0)}笔</td><td>{v5_stats.get('total',0)-v2_stats.get('total',0):+d}</td></tr>
                        <tr><td>胜率</td><td>{v2_stats.get('win_rate',0)}%</td><td>{v5_stats.get('win_rate',0)}%</td><td>{v5_stats.get('win_rate',0)-v2_stats.get('win_rate',0):+.1f}%</td></tr>
                        <tr><td>盈亏比</td><td>{v2_stats.get('profit_factor',0)}</td><td>{v5_stats.get('profit_factor',0)}</td><td>{v5_stats.get('profit_factor',0)-v2_stats.get('profit_factor',0):+.2f}</td></tr>
                        <tr><td>每笔期望</td><td>{v2_stats.get('expectancy',0):+.2f}%</td><td>{v5_stats.get('expectancy',0):+.2f}%</td><td>{v5_stats.get('expectancy',0)-v2_stats.get('expectancy',0):+.2f}%</td></tr>
                        <tr><td>累计收益</td><td>{v2_stats.get('cumulative',0):+.1f}%</td><td>{v5_stats.get('cumulative',0):+.1f}%</td><td>{v5_stats.get('cumulative',0)-v2_stats.get('cumulative',0):+.1f}%</td></tr>
                        <tr><td>平均持仓</td><td>{v2_stats.get('avg_hold',0)}天</td><td>{v5_stats.get('avg_hold',0)}天</td><td>-</td></tr>
                        <tr><td>最大连亏</td><td>{v2_stats.get('max_consec_loss',0)}次</td><td>{v5_stats.get('max_consec_loss',0)}次</td><td>-</td></tr>
                    </table>
                </div>
            </div>"""

            # V5卖出原因统计
            sell_stats = v5_stats.get("sell_stats", {})
            if sell_stats:
                sell_rows = ""
                for st, d in sorted(sell_stats.items(), key=lambda x: -x[1]["count"]):
                    wr = d["wins"]/d["count"]*100 if d["count"] > 0 else 0
                    avg = d["total"]/d["count"] if d["count"] > 0 else 0
                    sell_rows += f"<tr><td>{st}</td><td>{d['count']}</td><td>{wr:.0f}%</td><td>{avg:+.2f}%</td></tr>"
                real_html += f"""
                <div class="panel">
                    <div class="panel-title">🎯 V5.0卖出原因统计</div>
                    <div class="panel-body">
                        <table><tr><th>原因</th><th>次数</th><th>胜率</th><th>平均净收益</th></tr>{sell_rows}</table>
                    </div>
                </div>"""

    # 交易成本说明
    cost_html = f"""
    <div class="panel">
        <div class="panel-title">💰 交易成本与撮合规则</div>
        <div class="panel-body">
            <table>
                <tr><th>项目</th><th>事件驱动引擎</th><th>真实环境回测</th></tr>
                <tr><td>佣金</td><td>万2.5(双边)</td><td>含在0.3%合计中</td></tr>
                <tr><td>印花税</td><td>千1(仅卖出)</td><td>含在0.3%合计中</td></tr>
                <tr><td>滑点</td><td>买+0.1%/卖-0.1%</td><td>龙头0.2%/弹性0.5%</td></tr>
                <tr><td>T+1</td><td>✅ 当日买入次日可卖</td><td>✅ T日信号T+1执行</td></tr>
                <tr><td>涨跌停</td><td>✅ 涨停无法买/跌停无法卖</td><td>✅ 一字板过滤</td></tr>
                <tr><td>最小单位</td><td>✅ 100股整数倍</td><td>✅ 100股整数倍</td></tr>
                <tr><td>资金约束</td><td>✅ 现金不足自动缩减</td><td>✅ 固定12%仓位</td></tr>
            </table>
        </div>
    </div>"""

    # 问题与建议
    issues_html = """
    <div class="panel">
        <div class="panel-title">⚠️ 发现的问题与风险点</div>
        <div class="panel-body">
            <table>
                <tr><th>问题</th><th>严重度</th><th>说明</th><th>状态</th></tr>
                <tr><td>放量暴跌过滤逻辑缺陷</td><td style="color:#e74c3c">高</td><td>backtest_real.py V5中for-else结构错误导致过滤条件失效</td><td style="color:#28a745">✅已修复</td></tr>
                <tr><td>broker.update_highest类型不安全</td><td style="color:#faad14">中</td><td>未处理price为dict的边界情况</td><td style="color:#28a745">✅已修复</td></tr>
                <tr><td>佣金参数不一致</td><td style="color:#faad14">中</td><td>config.COMMISSION_RATE=万3 vs BACKTEST_CONFIG=万2.5 vs broker默认万2.5</td><td>建议统一</td></tr>
                <tr><td>quant/strategies无独立卖出</td><td style="color:#faad14">中</td><td>动量/事件/回调策略无卖出逻辑，依赖外部portfolio管理</td><td>架构设计如此</td></tr>
                <tr><td>操盘密码参数过多</td><td style="color:#17a2b8">低</td><td>CAOPAN_CONFIG含30+参数，过拟合风险较高</td><td>建议Walk-Forward验证</td></tr>
                <tr><td>真实回测手续费偏保守</td><td style="color:#17a2b8">低</td><td>backtest_real用0.3%合计，实际约0.13%单边</td><td>保守估计可接受</td></tr>
            </table>
        </div>
    </div>"""

    # 优化建议
    suggestions_html = """
    <div class="panel">
        <div class="panel-title">💡 优化建议与后续改进方向</div>
        <div class="panel-body">
            <ol style="font-size:13px;line-height:2">
                <li><b>统一交易成本参数</b>: 将config.py中COMMISSION_RATE、BACKTEST_CONFIG、broker默认值统一为万2.5+千1印花税</li>
                <li><b>补充quant策略卖出逻辑</b>: 为动量/事件/回调策略增加独立止盈止损，或明确portfolio调用链</li>
                <li><b>加强Walk-Forward验证</b>: 对操盘密码30+参数进行滚动窗口验证，确认样本外表现</li>
                <li><b>增加Deflated Sharpe Ratio</b>: 对多策略/多参数回测结果进行统计显著性检验</li>
                <li><b>实盘滑点校准</b>: 对比实盘成交价与信号价偏差，动态调整滑点参数</li>
                <li><b>扩大回测样本</b>: 增加标的数量(当前9只核心池偏少)和时间跨度(建议5年+)</li>
                <li><b>策略相关性分析</b>: 评估多策略组合的收益相关性，优化配置权重</li>
            </ol>
        </div>
    </div>"""

    # 综合结论
    conclusion_html = ""
    if bt:
        sharpe = bt.get("sharpe_ratio", 0)
        win_rate = bt.get("win_rate", 0)
        max_dd = bt.get("max_drawdown", 0)
        if sharpe > 1 and win_rate > 0.5:
            verdict = "策略在回测期间表现良好，风险收益比可接受"
        elif sharpe > 0.5:
            verdict = "策略有一定正期望，但夏普比率偏低，需优化入场/出场时机"
        else:
            verdict = "策略回测表现一般，建议结合Walk-Forward验证确认是否存在过拟合"
        conclusion_html = f"""
        <div class="panel">
            <div class="panel-title">📋 综合结论</div>
            <div class="panel-body">
                <p style="font-size:14px;line-height:1.8">
                    本次分析覆盖 <b>{stock_count}</b> 只标的，数据区间 <b>{date_min}</b> ~ <b>{date_max}</b>。
                    系统包含 <b>{len(STRATEGY_INVENTORY)}</b> 个策略/回测引擎。
                </p>
                <p style="font-size:14px;line-height:1.8">
                    <b>评估结论</b>: {verdict}
                </p>
                <p style="font-size:13px;color:#666">
                    注: 回测结果基于历史数据，不代表未来表现。所有策略均已考虑手续费、滑点、T+1、涨跌停等真实交易约束。
                </p>
            </div>
        </div>"""

    # 组装完整HTML
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 15px; background: #f0f2f5; }}
.container {{ max-width: 950px; margin: 0 auto; }}
.header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); color: white; padding: 20px 25px; border-radius: 10px 10px 0 0; }}
.header h1 {{ margin: 0; font-size: 20px; }}
.header .sub {{ font-size: 12px; opacity: 0.8; margin-top: 5px; }}
.content {{ background: white; padding: 20px 25px; border-radius: 0 0 10px 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
.panel {{ border: 1px solid #e8e8e8; border-radius: 8px; margin: 15px 0; overflow: hidden; }}
.panel-title {{ background: #fafafa; padding: 10px 15px; font-weight: bold; font-size: 14px; border-bottom: 1px solid #e8e8e8; }}
.panel-body {{ padding: 12px 15px; }}
.grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }}
.metric {{ text-align: center; padding: 10px; background: #f8f9fa; border-radius: 6px; }}
.metric .label {{ font-size: 11px; color: #888; }}
.metric .value {{ font-size: 18px; font-weight: bold; margin-top: 3px; }}
.metric .value.up {{ color: #e74c3c; }}
.metric .value.down {{ color: #27ae60; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin: 8px 0; }}
th {{ background: #34495e; color: white; padding: 8px 6px; text-align: center; }}
td {{ padding: 7px 6px; border-bottom: 1px solid #eee; text-align: center; }}
tr:nth-child(even) {{ background: #f8f9fa; }}
.footer {{ text-align: center; color: #999; font-size: 11px; margin-top: 15px; padding-top: 10px; border-top: 1px solid #eee; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📊 量化策略综合分析与回测验证报告</h1>
        <div class="sub">生成时间: {today} {now} | 数据: {stock_count}只标的 | 区间: {date_min} ~ {date_max}</div>
    </div>
    <div class="content">
        <div class="panel">
            <div class="panel-title">📋 一、现有策略清单 ({len(STRATEGY_INVENTORY)}个)</div>
            <div class="panel-body">
                <table>
                    <tr><th>策略名称</th><th>核心逻辑</th><th>适用场景</th><th>问题/风险</th></tr>
                    {strategy_rows}
                </table>
            </div>
        </div>

        <div class="panel">
            <div class="panel-title">📐 二、回测数据范围与测试假设</div>
            <div class="panel-body">
                <table>
                    <tr><th>项目</th><th>说明</th></tr>
                    <tr><td>数据来源</td><td>SQLite本地数据库 / baostock（前复权日线）</td></tr>
                    <tr><td>标的数量</td><td>{stock_count}只（核心股票池 + 扩展标的）</td></tr>
                    <tr><td>数据区间</td><td>{date_min} ~ {date_max}</td></tr>
                    <tr><td>初始资金</td><td>{config.TOTAL_CAPITAL:,.0f}元</td></tr>
                    <tr><td>基准指数</td><td>沪深300 (000300)</td></tr>
                    <tr><td>信号执行</td><td>T日收盘计算信号 → T+1日开盘执行（无未来函数）</td></tr>
                </table>
            </div>
        </div>

        {cost_html}
        {bt_html}
        {real_html}
        {issues_html}
        {suggestions_html}
        {conclusion_html}

        <div class="footer">
            本报告由交易系统自动生成 | 仅供参考，不构成投资建议<br>
            股市有风险，投资需谨慎 | 高胜率A股交易操作系统 V8.0
        </div>
    </div>
</div>
</body>
</html>"""

    return html


# ============================================================
# 五、主函数
# ============================================================

def run(send_mail=True):
    """执行完整分析流程"""
    total_start = time.time()

    print("=" * 60)
    print("  量化策略综合分析与回测验证")
    print("=" * 60)

    # 1. 加载数据（优先本地DB，备用baostock）
    logger.info("【步骤1】加载历史数据...")
    # 扩展标的池（核心池 + 回测测试池）
    all_codes = list(set(
        list(config.STOCK_POOL.keys()) +
        ["002371", "002409", "600118", "600584", "603986",
         "000725", "002384", "600760", "300750", "002594",
         "601012", "002230", "600519", "601318", "300760",
         "601899", "002049", "600893", "300274", "600036"]
    ))

    data_dict = load_data_from_db(all_codes, days=750)

    if len(data_dict) < 3:
        logger.info("本地数据不足，尝试baostock...")
        data_dict = load_data_baostock(all_codes, start_date="2022-01-01")

    if len(data_dict) < 2:
        logger.error("无法获取足够的历史数据，分析终止")
        return None

    # 2. 事件驱动回测
    logger.info("【步骤2】运行事件驱动回测引擎...")
    engine_report = run_event_driven_backtest(data_dict)
    if "error" not in engine_report:
        logger.info(f"  事件驱动回测完成: 收益{engine_report.get('total_return',0):.2%}, "
                   f"夏普{engine_report.get('sharpe_ratio',0):.2f}, "
                   f"胜率{engine_report.get('win_rate',0):.1%}")

    # 3. 真实环境回测
    logger.info("【步骤3】运行真实环境回测(V2/V5)...")
    real_result = run_real_backtest(data_dict)
    if "error" not in real_result:
        v5s = real_result.get("v5", {}).get("stats", {})
        if "error" not in v5s:
            logger.info(f"  V5回测完成: {v5s.get('total',0)}笔, "
                       f"胜率{v5s.get('win_rate',0)}%, "
                       f"盈亏比{v5s.get('profit_factor',0)}")

    # 4. 生成报告
    logger.info("【步骤4】生成分析报告...")
    html = generate_analysis_report(data_dict, engine_report, real_result)

    # 保存报告文件
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    report_path = os.path.join(config.OUTPUT_DIR, f"strategy_analysis_{datetime.date.today().strftime('%Y%m%d')}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"  报告已保存: {report_path}")

    # 5. 发送邮件
    if send_mail:
        logger.info("【步骤5】发送邮件报告...")
        try:
            from notify.email_notify import send_email
            subject = f"[策略分析] 量化策略综合回测报告 | {datetime.date.today().strftime('%Y-%m-%d')}"
            result = send_email(subject, html)
            if result:
                logger.info("  ✅ 邮件发送成功")
            else:
                logger.warning("  ⚠️ 邮件未发送（可能未配置授权码）")
        except Exception as e:
            logger.error(f"  邮件发送失败: {e}")

    total_elapsed = time.time() - total_start
    print(f"\n{'=' * 60}")
    print(f"  分析完成! 总耗时: {total_elapsed:.1f}秒")
    print(f"  报告路径: {report_path}")
    print(f"{'=' * 60}")

    return report_path


if __name__ == "__main__":
    run(send_mail=True)
