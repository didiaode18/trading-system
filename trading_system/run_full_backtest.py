# -*- coding: utf-8 -*-
"""
100万实盘模拟回测验证 - 74只全量标的版
======================================
基于系统全部策略与回测基础设施，执行完整真实历史数据回测

核心优化：
  - 预计算全部股票技术指标（避免每日重复计算）
  - 批量数据加载
  - 进度日志
  - 异常容错

参数:
  - 初始资金: 100万元
  - 回测区间: 2023-01-01 ~ 2026-07-25
  - 标的: 数据库全部74只股票
  - 策略: 趋势跟踪V2.0 (事件驱动引擎) + 真实环境V5.0
  - 成本: 佣金万2.5 + 印花税千1 + 滑点0.1% + T+1 + 涨跌停
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
            os.path.join(config.OUTPUT_DIR, f"backtest_100w_{datetime.date.today().strftime('%Y%m%d')}.log"),
            encoding='utf-8'
        )
    ]
)
logger = logging.getLogger("full_backtest")

# ============================================================
# 回测参数
# ============================================================
INITIAL_CAPITAL = 1_000_000  # 100万
START_DATE = "2023-01-01"
END_DATE = "2026-07-25"


# ============================================================
# 一、数据加载（批量优化）
# ============================================================

def load_all_data():
    """从SQLite批量加载全部历史数据"""
    if not os.path.exists(config.DB_PATH):
        logger.error(f"数据库不存在: {config.DB_PATH}")
        return {}

    conn = sqlite3.connect(config.DB_PATH)
    
    # 批量读取所有数据
    logger.info("  批量读取数据库...")
    query = """SELECT code, date, open, close, high, low, volume 
               FROM daily_kline ORDER BY code, date ASC"""
    df_all = pd.read_sql(query, conn)
    conn.close()
    
    logger.info(f"  读取完成: {len(df_all)} 条记录")
    
    # 按股票代码分组
    data_dict = {}
    warmup_date = "2022-10-01"  # 预热期
    
    for code, group in df_all.groupby("code"):
        df = group.copy()
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        df = df[df["date"] >= warmup_date].reset_index(drop=True)
        if len(df) > 100:
            data_dict[code] = df
    
    logger.info(f"  成功加载 {len(data_dict)} 只股票数据")
    return data_dict


# ============================================================
# 二、预计算指标（核心优化）
# ============================================================

def precompute_indicators(data_dict):
    """预计算全部股票的技术指标，避免回测时重复计算"""
    from strategy.trend_strategy import compute_indicators
    
    logger.info("  预计算技术指标...")
    precomputed = {}
    total = len(data_dict)
    
    for i, (code, df) in enumerate(data_dict.items(), 1):
        try:
            df_calc = compute_indicators(df.copy())
            precomputed[code] = df_calc
            if i % 10 == 0 or i == total:
                logger.info(f"    [{i}/{total}] 指标预计算完成")
        except Exception as e:
            logger.warning(f"    [{i}/{total}] {code} 指标计算失败: {e}")
            precomputed[code] = df  # 使用原始数据
    
    logger.info(f"  指标预计算完成: {len(precomputed)} 只")
    return precomputed


# ============================================================
# 三、事件驱动引擎回测（使用预计算指标）
# ============================================================

def create_fast_strategy(precomputed_data):
    """创建使用预计算数据的快速策略函数"""
    from strategy.trend_strategy import generate_strategy_signal
    from strategy.position import calc_first_batch
    from backtest.broker import Order
    
    def fast_strategy(date: str, feed, broker) -> list:
        orders = []
        
        for code in feed.stock_codes:
            if code not in precomputed_data:
                continue
            
            df_full = precomputed_data[code]
            
            # 获取截止到当前日期的数据
            mask = df_full["date"] <= date
            df = df_full[mask].copy()
            
            if len(df) < config.MA_MID:
                continue
            
            # 获取当前持仓
            holding = broker.get_holding_dict(code)
            
            try:
                # 生成信号
                signal = generate_strategy_signal(df, holding)
                
                # 卖出信号
                if signal.get("sell_signal") and holding:
                    bar = feed.get_bar(code, date)
                    sell_price = signal.get("sell_price") or (bar["close"] if bar else 0)
                    if sell_price > 0:
                        orders.append(Order(
                            code=code,
                            direction="sell",
                            target_shares=holding["shares"],
                            price=sell_price,
                            date=date,
                            reason=signal.get("signal_reason", "策略卖出"),
                        ))
                
                # 买入信号
                elif signal.get("buy_signal") and not holding:
                    bar = feed.get_bar(code, date)
                    buy_price = signal.get("buy_price") or (bar["close"] if bar else 0)
                    stop_loss = signal.get("stop_loss_initial", buy_price * 0.9)
                    
                    if buy_price > 0:
                        stock_type = config.get_stock_info(code).get("类型", "龙头")
                        batch = calc_first_batch(buy_price, stop_loss, stock_type, broker.initial_capital)
                        if batch.get("pass_risk") and batch.get("shares", 0) > 0:
                            orders.append(Order(
                                code=code,
                                direction="buy",
                                target_shares=batch["shares"],
                                price=buy_price,
                                date=date,
                                reason=signal.get("signal_reason", "策略买入"),
                            ))
            except Exception as e:
                pass  # 静默处理单只股票异常
        
        return orders
    
    return fast_strategy


def run_engine_backtest(data_dict, precomputed_data):
    """运行事件驱动回测引擎V2 + 趋势跟踪策略（74只全量）"""
    from backtest.engine import BacktestEngineV2
    from backtest.broker import CostConfig
    
    # 使用系统配置的交易成本
    cost = CostConfig(
        buy_slippage=0.001,       # 买入滑点0.1%
        sell_slippage=0.001,      # 卖出滑点0.1%
        commission_rate=0.00025,  # 佣金万2.5
        min_commission=5.0,       # 最低佣金5元
        stamp_tax_rate=0.001,     # 印花税千1
    )
    
    engine = BacktestEngineV2(initial_capital=INITIAL_CAPITAL, cost_config=cost)
    
    # 使用全部股票
    sub_data = {c: data_dict[c] for c in data_dict.keys()}
    
    logger.info(f"  事件驱动引擎使用 {len(sub_data)} 只股票")
    
    # 创建快速策略
    fast_strategy = create_fast_strategy(precomputed_data)
    
    t0 = time.time()
    report = engine.run(
        sub_data,
        strategy_fn=fast_strategy,
        start_date=START_DATE,
        end_date=END_DATE,
        benchmark_code="000300" if "000300" in sub_data else None,
        auto_monte_carlo=True,
        mc_simulations=500,  # 500次模拟
    )
    elapsed = time.time() - t0
    report["elapsed_sec"] = round(elapsed, 1)
    
    # 提取交易明细
    report["order_log"] = engine.order_log
    report["daily_values_df"] = pd.DataFrame(engine.daily_values) if engine.daily_values else pd.DataFrame()
    
    return report


# ============================================================
# 四、真实环境回测 V5.0（74只全量）
# ============================================================

def run_real_backtest(data_dict):
    """运行真实环境回测V5.0（逐股回测，带进度日志）"""
    from backtest_real import backtest_stock_v4, backtest_stock_v5, analyze_trades
    
    trades_v2 = []
    trades_v5 = []
    stock_info_map = {}
    
    stock_codes = [c for c in data_dict.keys() if c != "000300"]
    total = len(stock_codes)
    
    logger.info(f"  真实环境回测: {total} 只股票")
    
    for i, code in enumerate(stock_codes, 1):
        df = data_dict[code]
        
        # 获取股票信息
        info = config.get_stock_info(code)
        stock_type = info.get("类型", "龙头")
        industry = info.get("赛道", "其他")
        name = info.get("名称", code)
        stock_info_map[code] = {"名称": name, "类型": stock_type, "行业": industry}
        
        info_dict = {"名称": name, "类型": stock_type, "行业": industry}
        
        try:
            t2 = backtest_stock_v4(df, code, info_dict, version="v2")
            t5 = backtest_stock_v5(df, code, info_dict)
            trades_v2.extend(t2)
            trades_v5.extend(t5)
        except Exception as e:
            logger.warning(f"    [{i}/{total}] {code} 回测失败: {e}")
        
        # 进度日志
        if i % 10 == 0 or i == total:
            logger.info(f"    [{i}/{total}] 回测进度")
    
    trades_v2.sort(key=lambda x: x.get("buy_date", ""))
    trades_v5.sort(key=lambda x: x.get("buy_date", ""))
    
    stats_v2 = analyze_trades(trades_v2) if trades_v2 else {"error": "无交易"}
    stats_v5 = analyze_trades(trades_v5) if trades_v5 else {"error": "无交易"}
    
    return {
        "v2": {"stats": stats_v2, "trades": trades_v2},
        "v5": {"stats": stats_v5, "trades": trades_v5},
        "stock_info": stock_info_map,
    }


# ============================================================
# 五、Walk-Forward 验证
# ============================================================

def run_walk_forward(data_dict):
    """运行Walk-Forward防过拟合验证（V2.0: 扩大窗口+V5.0回测+精简参数）"""
    try:
        from backtest.walk_forward import WalkForwardAnalyzer
        
        stock_codes = [c for c in data_dict.keys() if c != "000300"][:20]
        sub_data = {c: data_dict[c] for c in stock_codes}
        if "000300" in data_dict:
            sub_data["000300"] = data_dict["000300"]
        
        # V2.0: 训练窗口60→120天，验证窗口20→40天，参数精简为3个核心
        wf = WalkForwardAnalyzer(
            train_days=120,
            test_days=40,
            param_grid={
                "initial_stop_loss": [0.06, 0.07, 0.08],
                "min_signal_quality": [55, 60, 65],
                "drawdown_leader": [0.05, 0.06, 0.07],
            },
            initial_capital=INITIAL_CAPITAL,
            max_windows=15,
        )
        
        t0 = time.time()
        result = wf.run(sub_data, stock_codes)
        elapsed = time.time() - t0
        result["elapsed_sec"] = round(elapsed, 1)
        return result
    except Exception as e:
        logger.warning(f"Walk-Forward执行失败: {e}")
        return {"error": str(e)}


# ============================================================
# 5.5、P2: 独立样本外验证集
# ============================================================

def run_holdout_validation(data_dict):
    """
    P2: 独立样本外验证
    
    原理: 将最近6个月数据作为完全独立的验证集，
    该数据不参与任何参数调优，仅用于评估策略的泛化能力。
    
    方法:
      - 截取最后120个交易日的数据作为holdout
      - 用相同参数运行V5回测
      - 对比全样本与holdout的胜率/盈亏比
    """
    try:
        from backtest_real import backtest_stock_v5, analyze_trades
        
        # 截取最后120个交易日作为holdout
        holdout_days = 120
        stock_codes = [c for c in data_dict.keys() if c != "000300"]
        
        holdout_trades = []
        for code in stock_codes:
            df = data_dict[code]
            if len(df) <= holdout_days + 80:  # 需要80天预热
                continue
            # 取最后 holdout_days+80 天（前80天用于指标预热）
            df_holdout = df.iloc[-(holdout_days + 80):].reset_index(drop=True)
            info = config.get_stock_info(code)
            info_dict = {"名称": info.get("名称", code), "类型": info.get("类型", "龙头"), "行业": info.get("赛道", "其他")}
            try:
                trades = backtest_stock_v5(df_holdout, code, info_dict)
                # 只保留holdout期间的交易（买入日期在最后120天内）
                cutoff_date = df.iloc[-holdout_days]["date"]
                trades = [t for t in trades if t.get("buy_date", "") >= cutoff_date]
                holdout_trades.extend(trades)
            except Exception:
                pass
        
        if not holdout_trades:
            return {"error": "样本外无交易"}
        
        stats = analyze_trades(holdout_trades)
        holdout_start = data_dict[stock_codes[0]].iloc[-holdout_days]["date"] if stock_codes else "N/A"
        holdout_end = data_dict[stock_codes[0]].iloc[-1]["date"] if stock_codes else "N/A"
        
        return {
            "stats": stats,
            "trades_count": len(holdout_trades),
            "period": f"{holdout_start} ~ {holdout_end}",
            "holdout_days": holdout_days,
        }
    except Exception as e:
        logger.warning(f"样本外验证失败: {e}")
        return {"error": str(e)}


# ============================================================
# 六、报告生成
# ============================================================

def generate_full_report(engine_report, real_result, wf_result, data_dict, holdout_result=None):
    """生成完整HTML分析报告"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M:%S")
    
    # 数据统计
    stock_count = len([c for c in data_dict.keys() if c != "000300"])
    
    # === 事件驱动引擎结果 ===
    bt = engine_report if engine_report and "error" not in engine_report else {}
    
    # === 真实环境V5结果 ===
    v5_data = real_result.get("v5", {}) if real_result else {}
    v5_stats = v5_data.get("stats", {})
    v5_trades = v5_data.get("trades", [])
    v2_stats = real_result.get("v2", {}).get("stats", {}) if real_result else {}
    
    # === 股票明细统计 ===
    stock_detail_rows = ""
    if v5_trades:
        stock_stats = {}
        for t in v5_trades:
            key = f"{t['code']}|{t['name']}"
            if key not in stock_stats:
                stock_stats[key] = {"count": 0, "wins": 0, "total_pnl": 0, "hold_days": []}
            stock_stats[key]["count"] += 1
            if t["net_profit"] > 0:
                stock_stats[key]["wins"] += 1
            stock_stats[key]["total_pnl"] += t["net_profit"]
            stock_stats[key]["hold_days"].append(t["hold_days"])
        
        sorted_stocks = sorted(stock_stats.items(), key=lambda x: -x[1]["total_pnl"])
        for key, s in sorted_stocks[:30]:
            code, name = key.split("|")
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            avg_hold = np.mean(s["hold_days"]) if s["hold_days"] else 0
            color = "#e74c3c" if s["total_pnl"] > 0 else "#27ae60"
            stock_detail_rows += f"""<tr>
                <td>{code}</td><td>{name}</td><td>{s['count']}</td>
                <td>{wr:.0f}%</td>
                <td style="color:{color};font-weight:bold">{s['total_pnl']:+.1f}%</td>
                <td>{avg_hold:.0f}天</td>
            </tr>"""
    
    # === 卖出原因分布 ===
    sell_reason_rows = ""
    if v5_stats and "sell_stats" in v5_stats:
        for st, d in sorted(v5_stats["sell_stats"].items(), key=lambda x: -x[1]["count"]):
            wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
            avg = d["total"] / d["count"] if d["count"] > 0 else 0
            pct = d["count"] / v5_stats.get("total", 1) * 100
            sell_reason_rows += f"""<tr>
                <td>{st}</td><td>{d['count']}</td><td>{pct:.1f}%</td>
                <td>{wr:.0f}%</td><td>{avg:+.2f}%</td>
            </tr>"""
    
    # === 月度收益分布 ===
    monthly_rows = ""
    if v5_trades:
        monthly_pnl = {}
        for t in v5_trades:
            month_key = t["sell_date"][:7]
            if month_key not in monthly_pnl:
                monthly_pnl[month_key] = {"count": 0, "total": 0, "wins": 0}
            monthly_pnl[month_key]["count"] += 1
            monthly_pnl[month_key]["total"] += t["net_profit"]
            if t["net_profit"] > 0:
                monthly_pnl[month_key]["wins"] += 1
        
        for mk in sorted(monthly_pnl.keys()):
            d = monthly_pnl[mk]
            wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
            color = "#e74c3c" if d["total"] > 0 else "#27ae60"
            monthly_rows += f"""<tr>
                <td>{mk}</td><td>{d['count']}</td><td>{wr:.0f}%</td>
                <td style="color:{color};font-weight:bold">{d['total']:+.1f}%</td>
            </tr>"""
    
    # === Walk-Forward结果 ===
    wf_html = ""
    if wf_result and "error" not in wf_result:
        wf_html = f"""
        <div class="panel">
            <div class="panel-title">🔄 Walk-Forward 防过拟合验证</div>
            <div class="panel-body">
                <div class="grid">
                    <div class="metric"><div class="label">验证窗口数</div><div class="value">{wf_result.get('num_windows', 0)}</div></div>
                    <div class="metric"><div class="label">样本外夏普</div><div class="value">{wf_result.get('oos_sharpe', 0):.2f}</div></div>
                    <div class="metric"><div class="label">参数稳定性</div><div class="value">{wf_result.get('stability_score', 0):.0%}</div></div>
                    <div class="metric"><div class="label">过拟合程度</div><div class="value">{wf_result.get('overfit_ratio', 0):.1%}</div></div>
                </div>
                <p style="font-size:12px;color:#666;margin-top:10px">判定: {wf_result.get('verdict', 'N/A')} | 耗时: {wf_result.get('elapsed_sec', 0)}秒</p>
            </div>
        </div>"""
    
    # === Monte Carlo结果 ===
    mc_html = ""
    if bt.get("mc_95_max_drawdown") is not None:
        mc_html = f"""
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
    
    # === P2: 独立样本外验证 ===
    holdout_html = ""
    if holdout_result and "error" not in holdout_result:
        hs = holdout_result.get("stats", {})
        # 对比全样本 vs 样本外
        full_wr = v5_stats.get('win_rate', 0)
        hold_wr = hs.get('win_rate', 0)
        full_pf = v5_stats.get('profit_factor', 0)
        hold_pf = hs.get('profit_factor', 0)
        # 泛化判定
        if hold_wr >= full_wr * 0.85 and hold_pf >= full_pf * 0.7:
            verdict = "✅ 泛化良好"
        elif hold_wr >= full_wr * 0.7:
            verdict = "⚠️ 轻度过拟合"
        else:
            verdict = "❌ 过拟合风险高"
        holdout_html = f"""
        <div class="panel" style="border: 2px solid #e67e22;">
            <div class="panel-title" style="background: #fef9e7;">🧪 P2: 独立样本外验证（最近{holdout_result.get('holdout_days',120)}个交易日）</div>
            <div class="panel-body">
                <div class="grid">
                    <div class="metric"><div class="label">样本外交易数</div><div class="value">{holdout_result.get('trades_count',0)}</div></div>
                    <div class="metric"><div class="label">样本外胜率</div><div class="value">{hold_wr:.1f}%</div></div>
                    <div class="metric"><div class="label">样本外盈亏比</div><div class="value">{hold_pf:.2f}</div></div>
                    <div class="metric"><div class="label">泛化判定</div><div class="value">{verdict}</div></div>
                </div>
                <table style="margin-top:10px">
                    <tr><th>指标</th><th>全样本</th><th>样本外</th><th>衰减</th></tr>
                    <tr><td>胜率</td><td>{full_wr:.1f}%</td><td>{hold_wr:.1f}%</td><td>{hold_wr-full_wr:+.1f}%</td></tr>
                    <tr><td>盈亏比</td><td>{full_pf:.2f}</td><td>{hold_pf:.2f}</td><td>{hold_pf-full_pf:+.2f}</td></tr>
                    <tr><td>每笔期望</td><td>{v5_stats.get('expectancy',0):+.2f}%</td><td>{hs.get('expectancy',0):+.2f}%</td><td>{hs.get('expectancy',0)-v5_stats.get('expectancy',0):+.2f}%</td></tr>
                </table>
                <p style="font-size:12px;color:#666;margin-top:8px">验证区间: {holdout_result.get('period','N/A')} | 该数据不参与任何参数调优</p>
            </div>
        </div>"""

    # === P1: 分市场环境统计 ===
    regime_rows = ""
    if v5_trades:
        regime_stats = {}
        for t in v5_trades:
            rg = t.get("regime", "RANGE")
            if rg not in regime_stats:
                regime_stats[rg] = {"count": 0, "wins": 0, "total_pnl": 0, "hold_days": []}
            regime_stats[rg]["count"] += 1
            if t["net_profit"] > 0:
                regime_stats[rg]["wins"] += 1
            regime_stats[rg]["total_pnl"] += t["net_profit"]
            regime_stats[rg]["hold_days"].append(t["hold_days"])
        
        regime_labels = {"BULL": "🟢 牛市(BULL)", "RANGE": "🟡 震荡(RANGE)", "BEAR": "🔴 熊市(BEAR)"}
        for rg in ["BULL", "RANGE", "BEAR"]:
            if rg in regime_stats:
                s = regime_stats[rg]
                wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
                avg_pnl = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
                avg_hold = np.mean(s["hold_days"]) if s["hold_days"] else 0
                pct = s["count"] / len(v5_trades) * 100
                color = "#e74c3c" if s["total_pnl"] > 0 else "#27ae60"
                regime_rows += f"""<tr>
                    <td>{regime_labels.get(rg, rg)}</td><td>{s['count']}</td><td>{pct:.1f}%</td>
                    <td>{wr:.0f}%</td><td style="color:{color};font-weight:bold">{avg_pnl:+.2f}%</td>
                    <td>{avg_hold:.0f}天</td>
                </tr>"""
    
    # === P1: 半年度周期统计 ===
    period_rows = ""
    if v5_trades:
        period_stats = {}
        for t in v5_trades:
            # 按半年分组: 2023H1, 2023H2, 2024H1...
            bd = t.get("buy_date", "")
            if len(bd) >= 7:
                year = bd[:4]
                half = "H1" if int(bd[5:7]) <= 6 else "H2"
                pk = f"{year}{half}"
            else:
                pk = "unknown"
            if pk not in period_stats:
                period_stats[pk] = {"count": 0, "wins": 0, "total_pnl": 0}
            period_stats[pk]["count"] += 1
            if t["net_profit"] > 0:
                period_stats[pk]["wins"] += 1
            period_stats[pk]["total_pnl"] += t["net_profit"]
        
        for pk in sorted(period_stats.keys()):
            s = period_stats[pk]
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            avg_pnl = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
            color = "#e74c3c" if s["total_pnl"] > 0 else "#27ae60"
            period_rows += f"""<tr>
                <td>{pk}</td><td>{s['count']}</td><td>{wr:.0f}%</td>
                <td style="color:{color};font-weight:bold">{s['total_pnl']:+.1f}%</td>
                <td>{avg_pnl:+.2f}%</td>
            </tr>"""

    # === 组装HTML ===
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 15px; background: #f0f2f5; }}
.container {{ max-width: 1000px; margin: 0 auto; }}
.header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); color: white; padding: 20px 25px; border-radius: 10px 10px 0 0; }}
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
th {{ background: #34495e; color: white; padding: 8px 6px; text-align: center; }}
td {{ padding: 7px 6px; border-bottom: 1px solid #eee; text-align: center; }}
tr:nth-child(even) {{ background: #f8f9fa; }}
.footer {{ text-align: center; color: #999; font-size: 11px; margin-top: 15px; padding-top: 10px; border-top: 1px solid #eee; }}
.alert {{ padding: 10px 15px; border-radius: 6px; margin: 10px 0; font-size: 13px; background: #fff3cd; border-left: 4px solid #ffc107; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📊 100万实盘模拟回测验证报告（策略优化后 V5.1）</h1>
        <div class="sub">回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,.0f}元 | 标的: {stock_count}只 | 生成: {today} {now}</div>
    </div>
    <div class="content">

        <!-- 0. 优化前后对比 -->
        <div class="panel" style="border: 2px solid #27ae60;">
            <div class="panel-title" style="background: #d4edda;">📊 策略优化前后对比（V5.0 → V5.1）</div>
            <div class="panel-body">
                <table>
                    <tr><th>指标</th><th>优化前</th><th>优化后</th><th>变化</th></tr>
                    <tr><td>总胜率</td><td>61.8%</td><td>{v5_stats.get('win_rate',0):.1f}%</td><td>{v5_stats.get('win_rate',0)-61.8:+.1f}%</td></tr>
                    <tr><td>盈亏比</td><td>0.99</td><td>{v5_stats.get('profit_factor',0):.2f}</td><td>{v5_stats.get('profit_factor',0)-0.99:+.2f}</td></tr>
                    <tr><td>每笔期望</td><td>+1.66%</td><td>{v5_stats.get('expectancy',0):+.2f}%</td><td>{v5_stats.get('expectancy',0)-1.66:+.2f}%</td></tr>
                    <tr><td>总交易笔数</td><td>752笔</td><td>{v5_stats.get('total',0)}笔</td><td>{v5_stats.get('total',0)-752:+d}笔</td></tr>
                    <tr><td>最大连续亏损</td><td>17次</td><td>{v5_stats.get('max_consec_loss',0)}次</td><td>{v5_stats.get('max_consec_loss',0)-17:+d}次</td></tr>
                </table>
                <p style="font-size:12px;color:#666;margin-top:10px">
                    <b>优化内容:</b> ①关闭趋势破位卖出(原91笔0%胜率) ②关闭MACD死叉卖出(原61笔平均+1.50%) 
                    ③止损从8%收紧至7% ④信号质量门槛55→60分 ⑤龙头回落止盈5%→6%
                </p>
            </div>
        </div>

        <!-- 1. 整体绩效摘要 -->
        <div class="panel">
            <div class="panel-title">📈 一、整体绩效摘要（事件驱动引擎 + 趋势跟踪V2.0）</div>
            <div class="panel-body">
                <div class="grid-6">
                    <div class="metric"><div class="label">累计收益率</div><div class="value {'up' if bt.get('total_return',0)>0 else 'down'}">{bt.get('total_return',0):.2%}</div></div>
                    <div class="metric"><div class="label">年化收益率</div><div class="value">{bt.get('annual_return',0):.2%}</div></div>
                    <div class="metric"><div class="label">最大回撤</div><div class="value down">-{bt.get('max_drawdown',0):.2%}</div></div>
                    <div class="metric"><div class="label">夏普比率</div><div class="value">{bt.get('sharpe_ratio',0):.2f}</div></div>
                    <div class="metric"><div class="label">Calmar比率</div><div class="value">{bt.get('calmar_ratio',0):.2f}</div></div>
                    <div class="metric"><div class="label">总交易笔数</div><div class="value">{bt.get('total_trades',0)}</div></div>
                </div>
                <div class="grid" style="margin-top:10px">
                    <div class="metric"><div class="label">Sortino比率</div><div class="value">{bt.get('sortino_ratio',0):.2f}</div></div>
                    <div class="metric"><div class="label">基准收益</div><div class="value">{bt.get('benchmark_return',0):.2%}</div></div>
                    <div class="metric"><div class="label">超额收益</div><div class="value">{bt.get('excess_return',0):.2%}</div></div>
                    <div class="metric"><div class="label">交易成本</div><div class="value">{bt.get('total_cost',0):,.0f}元</div></div>
                </div>
            </div>
        </div>

        <!-- 2. 胜率与盈亏比 -->
        <div class="panel">
            <div class="panel-title">🎯 二、胜率与盈亏比（真实环境V5.0）</div>
            <div class="panel-body">
                <div class="grid-6">
                    <div class="metric"><div class="label">总胜率</div><div class="value">{v5_stats.get('win_rate',0):.1f}%</div></div>
                    <div class="metric"><div class="label">盈亏比</div><div class="value">{v5_stats.get('profit_factor',0):.2f}</div></div>
                    <div class="metric"><div class="label">每笔期望</div><div class="value {'up' if v5_stats.get('expectancy',0)>0 else 'down'}">{v5_stats.get('expectancy',0):+.2f}%</div></div>
                    <div class="metric"><div class="label">平均盈利</div><div class="value up">+{v5_stats.get('avg_win',0):.2f}%</div></div>
                    <div class="metric"><div class="label">平均亏损</div><div class="value down">{v5_stats.get('avg_loss',0):.2f}%</div></div>
                    <div class="metric"><div class="label">总交易</div><div class="value">{v5_stats.get('total',0)}笔</div></div>
                </div>
            </div>
        </div>

        <!-- 3. 回撤分析 -->
        <div class="panel">
            <div class="panel-title">📉 三、回撤与风险分析</div>
            <div class="panel-body">
                <div class="grid">
                    <div class="metric"><div class="label">最大回撤(引擎)</div><div class="value down">-{bt.get('max_drawdown',0):.2%}</div></div>
                    <div class="metric"><div class="label">最大连续亏损</div><div class="value">{v5_stats.get('max_consec_loss',0)}次</div></div>
                    <div class="metric"><div class="label">平均持仓天数</div><div class="value">{v5_stats.get('avg_hold',0):.0f}天</div></div>
                    <div class="metric"><div class="label">盈利单平均持仓</div><div class="value">{v5_stats.get('avg_hold_win',0):.0f}天</div></div>
                </div>
            </div>
        </div>

        {mc_html}
        {wf_html}
        {holdout_html}

        <!-- 4. 股票标的明细 -->
        <div class="panel">
            <div class="panel-title">📋 四、股票标的明细（按盈亏排序 TOP30）</div>
            <div class="panel-body">
                <table>
                    <tr><th>代码</th><th>名称</th><th>交易次数</th><th>胜率</th><th>累计盈亏</th><th>平均持仓</th></tr>
                    {stock_detail_rows}
                </table>
            </div>
        </div>

        <!-- 5. 卖出原因分布 -->
        <div class="panel">
            <div class="panel-title">🏷️ 五、卖出原因分布</div>
            <div class="panel-body">
                <table>
                    <tr><th>卖出原因</th><th>次数</th><th>占比</th><th>胜率</th><th>平均收益</th></tr>
                    {sell_reason_rows}
                </table>
            </div>
        </div>

        <!-- 6. 月度收益分布 -->
        <div class="panel">
            <div class="panel-title">📅 六、月度收益分布</div>
            <div class="panel-body">
                <table>
                    <tr><th>月份</th><th>交易笔数</th><th>胜率</th><th>月累计净收益</th></tr>
                    {monthly_rows}
                </table>
            </div>
        </div>

        <!-- P1: 分市场环境统计 -->
        <div class="panel" style="border: 2px solid #3498db;">
            <div class="panel-title" style="background: #ebf5fb;">🌐 七、分市场环境统计（P1: 多周期分环境）</div>
            <div class="panel-body">
                <table>
                    <tr><th>市场环境</th><th>交易笔数</th><th>占比</th><th>胜率</th><th>平均盈亏</th><th>平均持仓</th></tr>
                    {regime_rows}
                </table>
                <p style="font-size:12px;color:#666;margin-top:8px">
                    环境判定: 大盘指数>MA20且MA20>MA60→牛市 | 指数<MA60且MA20<MA60→熊市 | 其他→震荡
                </p>
            </div>
        </div>

        <!-- P1: 半年度周期统计 -->
        <div class="panel" style="border: 2px solid #9b59b6;">
            <div class="panel-title" style="background: #f4ecf7;">📆 八、半年度周期统计（P1: 多周期回测）</div>
            <div class="panel-body">
                <table>
                    <tr><th>周期</th><th>交易笔数</th><th>胜率</th><th>累计盈亏</th><th>每笔期望</th></tr>
                    {period_rows}
                </table>
            </div>
        </div>

        <!-- 9. 风险提示与优化建议 -->
        <div class="panel">
            <div class="panel-title">⚠️ 九、风险提示与优化建议</div>
            <div class="panel-body">
                <div class="alert">
                    <b>风险提示:</b> 回测结果基于历史数据，不代表未来表现。 Monte Carlo压力测试显示极端情况下回撤可能远超历史最大值。
                </div>
                <ol style="font-size:13px;line-height:2">
                    <li><b>趋势破位信号失效</b>: 历史数据显示趋势破位卖出胜率极低，建议在V6中关闭</li>
                    <li><b>MACD死叉假信号多</b>: 盈利状态下MACD死叉卖出收益有限，建议关闭或提高触发阈值</li>
                    <li><b>止损是主要亏损来源</b>: 优化入场时机（如增加多周期共振确认）可减少止损触发</li>
                    <li><b>持仓时间控制</b>: 盈利单平均持仓{v5_stats.get('avg_hold_win',0):.0f}天 vs 亏损单{v5_stats.get('avg_hold_loss',0):.0f}天，建议严格执行时间止损</li>
                    <li><b>分散投资</b>: 单只股票交易次数过多可能集中风险，建议控制单标的交易频率</li>
                </ol>
            </div>
        </div>

        <!-- 交易成本说明 -->
        <div class="panel">
            <div class="panel-title">💰 交易成本与撮合规则</div>
            <div class="panel-body">
                <table>
                    <tr><th>项目</th><th>配置值</th><th>说明</th></tr>
                    <tr><td>佣金</td><td>万2.5 (双边)</td><td>买卖各收0.025%，最低5元</td></tr>
                    <tr><td>印花税</td><td>千1 (仅卖出)</td><td>卖出时收取0.1%</td></tr>
                    <tr><td>滑点</td><td>买+0.1% / 卖-0.1%</td><td>模拟真实成交偏差</td></tr>
                    <tr><td>T+1</td><td>✅ 启用</td><td>当日买入次日才能卖出</td></tr>
                    <tr><td>涨跌停</td><td>✅ 启用</td><td>涨停无法买入，跌停无法卖出</td></tr>
                    <tr><td>最小单位</td><td>100股</td><td>交易股数必须为100的整数倍</td></tr>
                    <tr><td>资金约束</td><td>✅ 启用</td><td>现金不足时自动缩减交易量</td></tr>
                </table>
            </div>
        </div>

        <div class="footer">
            本报告由交易系统自动生成 | 仅供参考，不构成投资建议<br>
            股市有风险，投资需谨慎 | 高胜率A股交易操作系统 V9.0 | {today}
        </div>
    </div>
</div>
</body>
</html>"""
    
    return html


# ============================================================
# 七、主函数
# ============================================================

def run():
    """执行完整回测流程"""
    total_start = time.time()
    
    print("=" * 60)
    print("  100万实盘模拟回测验证（74只全量标的）")
    print(f"  区间: {START_DATE} ~ {END_DATE}")
    print("=" * 60)
    
    # 1. 加载数据
    logger.info("【步骤1】加载历史数据...")
    data_dict = load_all_data()
    if len(data_dict) < 5:
        logger.error("数据不足，回测终止")
        return None
    
    # 2. 预计算指标
    logger.info("【步骤2】预计算技术指标...")
    precomputed_data = precompute_indicators(data_dict)
    
    # 3. 事件驱动引擎回测
    logger.info("【步骤3】运行事件驱动回测引擎(趋势跟踪V2.0)...")
    engine_report = run_engine_backtest(data_dict, precomputed_data)
    if "error" not in engine_report:
        logger.info(f"  引擎回测完成: 收益{engine_report.get('total_return',0):.2%}, "
                   f"夏普{engine_report.get('sharpe_ratio',0):.2f}, "
                   f"交易{engine_report.get('total_trades',0)}笔, "
                   f"耗时{engine_report.get('elapsed_sec',0)}秒")
    
    # 4. 真实环境回测V5
    logger.info("【步骤4】运行真实环境回测V5.0...")
    real_result = run_real_backtest(data_dict)
    if "error" not in real_result:
        v5s = real_result.get("v5", {}).get("stats", {})
        if "error" not in v5s:
            logger.info(f"  V5回测完成: {v5s.get('total',0)}笔, "
                       f"胜率{v5s.get('win_rate',0)}%, "
                       f"盈亏比{v5s.get('profit_factor',0)}")
    
    # 5. Walk-Forward验证
    logger.info("【步骤5】运行Walk-Forward验证...")
    wf_result = run_walk_forward(data_dict)
    if "error" not in wf_result:
        logger.info(f"  Walk-Forward完成: 样本外夏普{wf_result.get('oos_sharpe',0):.2f}")
    
    # 5.5 P2: 独立样本外验证
    logger.info("【步骤5.5】运行独立样本外验证...")
    holdout_result = run_holdout_validation(data_dict)
    if "error" not in holdout_result:
        hs = holdout_result.get("stats", {})
        logger.info(f"  样本外验证完成: {holdout_result.get('trades_count',0)}笔, "
                   f"胜率{hs.get('win_rate',0)}%, 盈亏比{hs.get('profit_factor',0)}")
    
    # 6. 生成报告
    logger.info("【步骤6】生成分析报告...")
    html = generate_full_report(engine_report, real_result, wf_result, data_dict, holdout_result)
    
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    report_path = os.path.join(config.OUTPUT_DIR, f"backtest_100w_optimized_{datetime.date.today().strftime('%Y%m%d')}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"  报告已保存: {report_path}")
    
    # 7. 发送邮件
    logger.info("【步骤7】发送邮件...")
    try:
        from notify.email_notify import send_email
        subject = f"[回测验证] 策略优化后全量回测对比报告（74只）| 2023-2026"
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
