"""
三年历史回测诊断脚本
====================
聚焦当前持仓10只 + 核心候选池，使用V5.1逐股回测引擎
回测区间: 2023-09-01 ~ 2026-09-23

输出:
  - 绩效指标: 累计收益/年化/最大回撤/夏普/Sortino/Calmar/胜率/盈亏比
  - 基准对比: vs 沪深300 超额收益 & Alpha
  - 问题诊断: 分市场环境/过拟合/止损/换手率/成本侵蚀
  - 改进建议
"""

import sys, os, time, sqlite3, datetime, warnings, json, traceback
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

START_DATE = "2023-09-01"
END_DATE = "2026-09-23"
INITIAL_CAPITAL = 1_000_000
WARMUP = "2022-10-01"

# 当前持仓10只 + 核心候选池
DIAG_STOCKS = {
    # --- 当前持仓10只 ---
    "600900": {"名称": "长江电力", "类型": "龙头", "行业": "电力"},
    "600989": {"名称": "宝丰能源", "类型": "龙头", "行业": "能源"},
    "600887": {"名称": "伊利股份", "类型": "龙头", "行业": "消费"},
    "603345": {"名称": "安井食品", "类型": "龙头", "行业": "消费"},
    "510210": {"名称": "综指ETF", "类型": "弹性", "行业": "ETF"},
    "002281": {"名称": "光迅科技", "类型": "弹性", "行业": "通信"},
    "600256": {"名称": "广汇能源", "类型": "弹性", "行业": "能源"},
    "603099": {"名称": "长白山",  "类型": "弹性", "行业": "旅游"},
    "001979": {"名称": "招商南油", "类型": "弹性", "行业": "航运"},
    "562590": {"名称": "半导材料", "类型": "弹性", "行业": "半导体"},
    # --- 核心候选池 ---
    "601360": {"名称": "三六零",   "类型": "弹性", "行业": "科技"},
    "300750": {"名称": "宁德时代", "类型": "龙头", "行业": "新能源"},
    "002594": {"名称": "比亚迪",   "类型": "龙头", "行业": "新能源"},
    "600519": {"名称": "贵州茅台", "类型": "龙头", "行业": "消费"},
    "601318": {"名称": "中国平安", "类型": "龙头", "行业": "金融"},
    "000725": {"名称": "京东方A",  "类型": "弹性", "行业": "面板"},
    "601899": {"名称": "紫金矿业", "类型": "龙头", "行业": "有色"},
    "600036": {"名称": "招商银行", "类型": "龙头", "行业": "金融"},
}

# ============================================================
# 数据加载
# ============================================================
def load_data():
    conn = sqlite3.connect(config.DB_PATH)
    query = "SELECT code, date, open, close, high, low, volume FROM daily_kline ORDER BY code, date ASC"
    df_all = pd.read_sql(query, conn)
    conn.close()
    
    data_dict = {}
    for code, group in df_all.groupby("code"):
        df = group.copy()
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        df = df[df["date"] >= WARMUP].reset_index(drop=True)
        if len(df) > 100:
            data_dict[code] = df
    
    return data_dict


# ============================================================
# 基准收益计算（沪深300买入持有）
# ============================================================
def calc_benchmark_return(data_dict):
    if "000300" not in data_dict:
        return None
    df = data_dict["000300"].copy()
    df = df[df["date"] >= START_DATE].reset_index(drop=True)
    if len(df) < 2:
        return None
    start_price = df.iloc[0]["open"]
    end_price = df.iloc[-1]["close"]
    total_ret = (end_price - start_price) / start_price
    
    # 计算每日收益率序列（用于夏普/最大回撤）
    df["daily_ret"] = df["close"].pct_change()
    df = df.dropna(subset=["daily_ret"])
    
    # 最大回撤
    cum = (1 + df["daily_ret"]).cumprod()
    peak = cum.cummax()
    dd = (cum - peak) / peak
    max_dd = dd.min()
    
    # 年化
    n_days = len(df)
    ann_ret = (1 + total_ret) ** (252 / max(n_days, 1)) - 1
    
    # 夏普
    daily_mean = df["daily_ret"].mean()
    daily_std = df["daily_ret"].std()
    sharpe = (daily_mean - 0.0003) / daily_std * np.sqrt(252) if daily_std > 0 else 0
    
    return {
        "total_return": total_ret,
        "annual_return": ann_ret,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "start_price": start_price,
        "end_price": end_price,
        "n_days": n_days,
        "daily_returns": df["daily_ret"].values,
        "cum_returns": cum.values,
        "dates": df["date"].values,
    }


# ============================================================
# 组合收益模拟（基于V5逐股交易）
# ============================================================
def simulate_portfolio(trades_all, benchmark_info, trading_dates=None):
    """基于逐股交易模拟组合日收益曲线

    FIX P2-#14: 原实现仅在 buy_date 当日一次性累加整笔收益，既未按持仓期展开，
    也未被任何调用方使用（死代码）。现重写为：
      - 优先使用传入的 trading_dates（回测区间交易日序列）把 net_profit 均摊到
        buy_date 与 sell_date 之间的实际交易日；若未提供则退化为按自然日估算，
        并统一以买入日作为记账锚点（保持向后兼容）。
      - 返回按日期升序排列的 {date: daily_return_ratio} 字典，供上层构建 equity_curve。
    """
    if not trades_all:
        return {}

    position_ratio = 0.12  # 单笔仓位占总资金比例

    # 交易日索引：若提供则按真实交易日展开
    td_index = None
    if trading_dates:
        try:
            td_sorted = sorted({str(d)[:10] for d in trading_dates if d})
            td_index = {d: i for i, d in enumerate(td_sorted)}
        except Exception:
            td_index = None

    daily_returns = {}
    for t in trades_sorted_safe(trades_all):
        bd = str(t.get("buy_date", ""))[:10]
        sd = str(t.get("sell_date", ""))[:10]
        net_pct = float(t.get("net_profit", 0)) / 100.0 * position_ratio
        if not bd:
            continue

        if td_index and bd in td_index and sd in td_index and td_index[sd] > td_index[bd]:
            span = td_sorted[td_index[bd]: td_index[sd] + 1]
            per_day = net_pct / len(span)
            for d in span:
                daily_returns[d] = daily_returns.get(d, 0.0) + per_day
        else:
            # 退化路径：按 hold_days 均摊到从 buy_date 起的自然日
            hold = max(int(t.get("hold_days", 1) or 1), 1)
            try:
                bd_dt = datetime.datetime.strptime(bd, "%Y-%m-%d")
                per_day = net_pct / hold
                for k in range(hold):
                    d = (bd_dt + datetime.timedelta(days=k)).strftime("%Y-%m-%d")
                    daily_returns[d] = daily_returns.get(d, 0.0) + per_day
            except Exception:
                daily_returns[bd] = daily_returns.get(bd, 0.0) + net_pct

    return {d: daily_returns[d] for d in sorted(daily_returns.keys())}


def trades_sorted_safe(trades_all):
    """按 buy_date 排序的健壮封装（缺失字段的样本沉底）。"""
    return sorted(trades_all, key=lambda t: (str(t.get("buy_date", "")) or "9999-99-99"))


# ============================================================
# 绩效指标计算
# ============================================================
def calc_performance_metrics(trades_all, benchmark_info):
    """计算完整绩效指标

    FIX P2-#13: 原实现用 `cumulative *= (1 + net_profit × position_ratio)` 逐笔复利，
    隐含假设同一时刻只有 1 笔交易持仓，同日多笔时资金占用超 100%，
    导致总收益/回撤/年化全面失真。现改为：
      - 调用 simulate_portfolio 将每笔交易均摊到持仓期，得到日收益序列
      - 同日多笔采用加法聚合（而非乘法），避免资金重复占用
      - 基于日收益序列重建 equity_curve，后续回撤/年化均基于此曲线
    影响范围：total_return / max_drawdown / annual_return / calmar 均会发生变化（通常向下修正）。
    验证建议：对同一批 trades 对比旧/新 equity_curve 差异，确认同日多笔时旧方法高估。
    """
    if not trades_all:
        return {}

    position_ratio = 0.12

    # ---- 以日为粒度聚合收益（同日多笔加法叠加）----
    daily_returns_map = simulate_portfolio(trades_all, benchmark_info)
    daily_dates = sorted(daily_returns_map.keys())
    daily_series = [daily_returns_map[d] for d in daily_dates]

    cumulative = 1.0
    equity_curve = [1.0]
    for r in daily_series:
        cumulative *= (1.0 + r)
        equity_curve.append(cumulative)

    total_return = cumulative - 1
    n_trades = len(trades_all)
    
    # 估算年化（基于回测区间天数）
    all_dates = [t["buy_date"] for t in trades_all]
    if all_dates:
        first_date = min(all_dates)
        last_date = max(t["sell_date"] for t in trades_all)
        try:
            d1 = datetime.datetime.strptime(first_date, "%Y-%m-%d")
            d2 = datetime.datetime.strptime(last_date, "%Y-%m-%d")
            n_calendar_days = (d2 - d1).days
        except:
            n_calendar_days = 365 * 3
    else:
        n_calendar_days = 365 * 3
    
    n_years = max(n_calendar_days / 365.25, 0.1)
    annual_return = (1 + total_return) ** (1 / n_years) - 1
    
    # 最大回撤（基于equity curve）
    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    drawdown = (eq - peak) / peak
    max_drawdown = drawdown.min()
    
    # 每笔日收益率（用于夏普/Sortino）
    # FIX P2-#13: 使用按日聚合后的真实日收益序列，避免同日多笔重复计入
    if daily_series:
        trade_returns = np.array(daily_series)
    else:
        trade_returns = np.array([0])
    
    # 夏普比率 (无风险利率 3%/年 ≈ 0.00012/日)
    rf_daily = 0.03 / 252
    mean_r = trade_returns.mean()
    std_r = trade_returns.std()
    sharpe = (mean_r - rf_daily) / std_r * np.sqrt(252) if std_r > 0 else 0
    
    # Sortino比率（只考虑下行波动）
    downside = trade_returns[trade_returns < 0]
    downside_std = downside.std() if len(downside) > 0 else 1e-8
    sortino = (mean_r - rf_daily) / downside_std * np.sqrt(252) if downside_std > 0 else 0
    
    # Calmar比率
    calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0
    
    # 胜率 & 盈亏比
    wins = [t for t in trades_all if t["net_profit"] > 0]
    losses = [t for t in trades_all if t["net_profit"] <= 0]
    win_rate = len(wins) / n_trades * 100 if n_trades > 0 else 0
    avg_win = np.mean([t["net_profit"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["net_profit"] for t in losses]) if losses else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    
    # 交易成本侵蚀
    commission_total = abs(config.COMMISSION_RATE * 2 + config.STAMP_TAX_RATE + getattr(config, 'TRANSFER_FEE_RATE', 0.00001))
    # 每笔交易成本 ≈ 0.16%
    avg_slippage = 0.003  # 平均滑点0.3%
    cost_per_trade = (commission_total + avg_slippage) * 100  # 百分比
    total_cost_pct = cost_per_trade * n_trades
    
    # 基准对比
    bm = benchmark_info or {}
    bm_total = bm.get("total_return", 0)
    excess_return = total_return - bm_total
    
    # 简单Alpha估算（年化）
    bm_annual = bm.get("annual_return", 0)
    alpha = annual_return - bm_annual
    
    return {
        "total_return": round(total_return * 100, 2),
        "annual_return": round(annual_return * 100, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "calmar_ratio": round(calmar, 2),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(np.mean([t["net_profit"] for t in trades_all]), 2),
        "total_trades": n_trades,
        "win_count": len(wins),
        "loss_count": len(losses),
        "avg_hold": round(np.mean([t["hold_days"] for t in trades_all]), 1),
        "avg_hold_win": round(np.mean([t["hold_days"] for t in wins]) if wins else 0, 1),
        "avg_hold_loss": round(np.mean([t["hold_days"] for t in losses]) if losses else 0, 1),
        "max_consec_loss": _calc_max_consec_loss(trades_all),
        "total_cost_pct": round(total_cost_pct, 2),
        "cost_per_trade_pct": round(cost_per_trade, 3),
        "benchmark_return": round(bm_total * 100, 2),
        "benchmark_annual": round(bm_annual * 100, 2),
        "excess_return": round(excess_return * 100, 2),
        "alpha": round(alpha * 100, 2),
        "n_years": round(n_years, 1),
        "cumulative_equity": equity_curve,
    }


def _calc_max_consec_loss(trades):
    max_streak = 0
    streak = 0
    for t in trades:
        if t["net_profit"] <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


# ============================================================
# 主函数
# ============================================================
def run():
    from backtest_real import backtest_stock_v5, analyze_trades, compute_indicators
    
    t0 = time.time()
    print("=" * 70)
    print("  三年历史回测诊断（V5.1逐股引擎）")
    print(f"  区间: {START_DATE} ~ {END_DATE}")
    print(f"  标的: {len(DIAG_STOCKS)}只（持仓10 + 候选8）+ 沪深300基准")
    print("=" * 70)
    
    # 1. 加载数据
    print("\n[1/5] 加载历史数据...")
    data_dict = load_data()
    print(f"  数据库共 {len(data_dict)} 只股票")
    
    # 筛选目标标的
    target_codes = list(DIAG_STOCKS.keys())
    available = [c for c in target_codes if c in data_dict]
    missing = [c for c in target_codes if c not in data_dict]
    print(f"  目标标的: {len(target_codes)}只, 可用: {len(available)}只, 缺失: {len(missing)}只")
    if missing:
        print(f"  缺失标的: {missing}")
    
    # 加载基准
    benchmark_info = calc_benchmark_return(data_dict)
    if benchmark_info:
        print(f"  沪深300基准: {benchmark_info['n_days']}个交易日, "
              f"区间收益 {benchmark_info['total_return']*100:+.2f}%")
    
    # 2. 逐股V5.1回测
    print(f"\n[2/5] 运行V5.1逐股回测...")
    all_trades = []
    stock_results = {}
    
    for i, code in enumerate(available, 1):
        df = data_dict[code]
        info = DIAG_STOCKS[code]
        info_dict = {"名称": info["名称"], "类型": info["类型"], "行业": info["行业"]}
        
        # 获取基准数据用于市场环境判定
        bm_df = data_dict.get("000300", None)
        
        try:
            trades = backtest_stock_v5(df, code, info_dict, benchmark_df=bm_df)
            stock_results[code] = {
                "info": info,
                "trades": trades,
                "trade_count": len(trades),
            }
            all_trades.extend(trades)
            status = f"{len(trades)}笔交易" if trades else "无交易"
            print(f"  [{i:2d}/{len(available)}] {code} {info['名称']:6s}: {status}")
        except Exception as e:
            print(f"  [{i:2d}/{len(available)}] {code} {info['名称']:6s}: 错误 - {e}")
            stock_results[code] = {"info": info, "trades": [], "trade_count": 0, "error": str(e)}
    
    # 按买入日期排序
    all_trades.sort(key=lambda t: t.get("buy_date", ""))
    
    print(f"\n  总交易笔数: {len(all_trades)}")
    
    # 3. 统计分析
    print(f"\n[3/5] 计算绩效指标...")
    stats = analyze_trades(all_trades) if all_trades else {"error": "无交易"}
    perf = calc_performance_metrics(all_trades, benchmark_info)
    
    if "error" not in stats:
        print(f"  胜率: {stats['win_rate']:.1f}%")
        print(f"  盈亏比: {stats['profit_factor']:.2f}")
        print(f"  每笔期望: {stats['expectancy']:+.2f}%")
        print(f"  累计收益(仓位加权): {perf['total_return']:+.2f}%")
        print(f"  年化收益: {perf['annual_return']:+.2f}%")
        print(f"  最大回撤: {perf['max_drawdown']:.2f}%")
        print(f"  夏普比率: {perf['sharpe_ratio']:.2f}")
        print(f"  Sortino: {perf['sortino_ratio']:.2f}")
        print(f"  Calmar: {perf['calmar_ratio']:.2f}")
        print(f"  vs 沪深300超额: {perf['excess_return']:+.2f}%")
        print(f"  Alpha: {perf['alpha']:+.2f}%")
    
    # 4. 分市场环境统计
    print(f"\n[4/5] 分市场环境分析...")
    regime_stats = {}
    for t in all_trades:
        rg = t.get("regime", "RANGE")
        if rg not in regime_stats:
            regime_stats[rg] = {"count": 0, "wins": 0, "total_pnl": 0, "hold_days": []}
        regime_stats[rg]["count"] += 1
        if t["net_profit"] > 0:
            regime_stats[rg]["wins"] += 1
        regime_stats[rg]["total_pnl"] += t["net_profit"]
        regime_stats[rg]["hold_days"].append(t["hold_days"])
    
    for rg in ["BULL", "RANGE", "BEAR"]:
        if rg in regime_stats:
            s = regime_stats[rg]
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            avg = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
            print(f"  {rg:5s}: {s['count']:3d}笔, 胜率{wr:.0f}%, 均盈亏{avg:+.2f}%")
    
    # 5. 生成诊断报告
    print(f"\n[5/5] 生成诊断报告...")
    report = _generate_diagnostic_report(perf, stats, stock_results, regime_stats, 
                                          all_trades, benchmark_info, available)
    
    # 保存
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    today = datetime.date.today().strftime("%Y%m%d")
    report_path = os.path.join(config.OUTPUT_DIR, f"backtest_diagnostic_{today}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  报告已保存: {report_path}")
    
    # 同时保存JSON数据
    json_path = os.path.join(config.OUTPUT_DIR, f"backtest_diagnostic_{today}.json")
    json_data = {
        "perf": {k: v for k, v in perf.items() if k != "cumulative_equity"},
        "stats": {k: v for k, v in stats.items() if k not in ["sell_stats", "stock_stats", "industry_stats"]},
        "regime_stats": {rg: {"count": s["count"], "wins": s["wins"], 
                               "win_rate": round(s["wins"]/s["count"]*100, 1) if s["count"] > 0 else 0,
                               "total_pnl": round(s["total_pnl"], 2),
                               "avg_pnl": round(s["total_pnl"]/s["count"], 2) if s["count"] > 0 else 0}
                         for rg, s in regime_stats.items()},
        "stock_summary": {code: {"name": r["info"]["名称"], "trades": r["trade_count"],
                                  "win_rate": round(len([t for t in r["trades"] if t["net_profit"]>0])/max(r["trade_count"],1)*100, 1),
                                  "total_pnl": round(sum(t["net_profit"] for t in r["trades"]), 2)}
                          for code, r in stock_results.items()},
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    print(f"  JSON数据: {json_path}")
    
    elapsed = time.time() - t0
    print(f"\n{'=' * 70}")
    print(f"  诊断完成! 耗时: {elapsed:.1f}秒")
    print(f"{'=' * 70}")
    
    return report_path, json_data


def _generate_diagnostic_report(perf, stats, stock_results, regime_stats, 
                                 all_trades, benchmark_info, available_codes):
    """生成完整HTML诊断报告"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    now = datetime.datetime.now().strftime("%H:%M")
    
    # === 个股明细 ===
    stock_rows = ""
    sorted_stocks = sorted(stock_results.items(), 
                           key=lambda x: -sum(t["net_profit"] for t in x[1]["trades"]))
    for code, r in sorted_stocks:
        trades = r["trades"]
        if not trades:
            stock_rows += f'<tr><td>{code}</td><td>{r["info"]["名称"]}</td><td>{r["info"]["类型"]}</td><td>0</td><td>-</td><td>-</td><td>-</td></tr>'
            continue
        wins = len([t for t in trades if t["net_profit"] > 0])
        wr = wins / len(trades) * 100
        total_pnl = sum(t["net_profit"] for t in trades)
        avg_hold = np.mean([t["hold_days"] for t in trades])
        color = "#e74c3c" if total_pnl > 0 else "#27ae60"
        stock_rows += f'''<tr>
            <td>{code}</td><td>{r["info"]["名称"]}</td><td>{r["info"]["类型"]}</td>
            <td>{len(trades)}</td><td>{wr:.0f}%</td>
            <td style="color:{color};font-weight:bold">{total_pnl:+.1f}%</td>
            <td>{avg_hold:.0f}天</td>
        </tr>'''
    
    # === 卖出原因分布 ===
    sell_rows = ""
    if "sell_stats" in stats:
        for st, d in sorted(stats["sell_stats"].items(), key=lambda x: -x[1]["count"]):
            wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
            avg = d["total"] / d["count"] if d["count"] > 0 else 0
            pct = d["count"] / stats.get("total", 1) * 100
            sell_rows += f'<tr><td>{st}</td><td>{d["count"]}</td><td>{pct:.1f}%</td><td>{wr:.0f}%</td><td>{avg:+.2f}%</td></tr>'
    
    # === 市场环境 ===
    regime_rows = ""
    regime_labels = {"BULL": "🟢 牛市", "RANGE": "🟡 震荡", "BEAR": "🔴 熊市"}
    for rg in ["BULL", "RANGE", "BEAR"]:
        if rg in regime_stats:
            s = regime_stats[rg]
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            avg = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
            avg_hold = np.mean(s["hold_days"]) if s["hold_days"] else 0
            pct = s["count"] / len(all_trades) * 100 if all_trades else 0
            color = "#e74c3c" if avg > 0 else "#27ae60"
            regime_rows += f'''<tr>
                <td>{regime_labels.get(rg, rg)}</td><td>{s["count"]}</td><td>{pct:.1f}%</td>
                <td>{wr:.0f}%</td><td style="color:{color};font-weight:bold">{avg:+.2f}%</td>
                <td>{avg_hold:.0f}天</td>
            </tr>'''
    
    # === 月度收益 ===
    monthly_rows = ""
    monthly_pnl = {}
    for t in all_trades:
        mk = t["sell_date"][:7]
        if mk not in monthly_pnl:
            monthly_pnl[mk] = {"count": 0, "total": 0, "wins": 0}
        monthly_pnl[mk]["count"] += 1
        monthly_pnl[mk]["total"] += t["net_profit"]
        if t["net_profit"] > 0:
            monthly_pnl[mk]["wins"] += 1
    
    for mk in sorted(monthly_pnl.keys()):
        d = monthly_pnl[mk]
        wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
        color = "#e74c3c" if d["total"] > 0 else "#27ae60"
        monthly_rows += f'<tr><td>{mk}</td><td>{d["count"]}</td><td>{wr:.0f}%</td><td style="color:{color};font-weight:bold">{d["total"]:+.1f}%</td></tr>'
    
    # === 半年度统计 ===
    period_rows = ""
    period_stats = {}
    for t in all_trades:
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
        avg = s["total_pnl"] / s["count"] if s["count"] > 0 else 0
        color = "#e74c3c" if s["total_pnl"] > 0 else "#27ae60"
        period_rows += f'<tr><td>{pk}</td><td>{s["count"]}</td><td>{wr:.0f}%</td><td style="color:{color};font-weight:bold">{s["total_pnl"]:+.1f}%</td><td>{avg:+.2f}%</td></tr>'
    
    # === 行业统计 ===
    industry_rows = ""
    if "industry_stats" in stats:
        for ind, d in sorted(stats["industry_stats"].items(), key=lambda x: -x[1]["total"]):
            wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
            color = "#e74c3c" if d["total"] > 0 else "#27ae60"
            industry_rows += f'<tr><td>{ind}</td><td>{d["count"]}</td><td>{wr:.0f}%</td><td style="color:{color};font-weight:bold">{d["total"]:+.1f}%</td></tr>'
    
    # === 诊断结论 ===
    issues = []
    suggestions = []
    
    # 诊断1: 胜率
    if perf.get("win_rate", 0) < 45:
        issues.append("⚠️ 胜率偏低（<45%），入场信号质量需提升")
        suggestions.append("提高信号质量门槛（当前65分→建议70分），增加多周期共振确认")
    elif perf.get("win_rate", 0) > 65:
        issues.append("✅ 胜率良好（>65%）")
    
    # 诊断2: 盈亏比
    if perf.get("profit_factor", 0) < 1.0:
        issues.append("❌ 盈亏比<1.0，平均亏损大于平均盈利，策略期望值为负")
        suggestions.append("收紧止损（当前ATR 5-10%→建议4-8%），同时放宽止盈让利润奔跑")
    elif perf.get("profit_factor", 0) < 1.5:
        issues.append("⚠️ 盈亏比偏低（1.0-1.5），盈利空间有限")
        suggestions.append("优化止盈策略，减少过早止盈导致的盈利截断")
    else:
        issues.append("✅ 盈亏比良好（≥1.5）")
    
    # 诊断3: 最大回撤
    if abs(perf.get("max_drawdown", 0)) > 20:
        issues.append("❌ 最大回撤超过20%，风险控制不足")
        suggestions.append("增加总仓位上限约束，单标的风险敞口从12%降至8-10%")
    elif abs(perf.get("max_drawdown", 0)) > 10:
        issues.append("⚠️ 最大回撤10-20%，需关注尾部风险")
    
    # 诊断4: 夏普比率
    if perf.get("sharpe_ratio", 0) < 0.5:
        issues.append("⚠️ 夏普比率<0.5，风险调整后收益不佳")
        suggestions.append("降低交易频率，减少无效交易对收益的侵蚀")
    elif perf.get("sharpe_ratio", 0) > 1.0:
        issues.append("✅ 夏普比率>1.0，风险调整后收益优秀")
    
    # 诊断5: 超额收益
    if perf.get("excess_return", 0) < 0:
        issues.append("❌ 策略跑输沪深300基准，Alpha为负")
        suggestions.append("考虑增加指数增强策略，或在熊市中降低仓位暴露")
    else:
        issues.append(f"✅ 跑赢沪深300基准 {perf['excess_return']:+.1f}%，Alpha={perf['alpha']:+.1f}%")
    
    # 诊断6: 交易成本
    if perf.get("total_cost_pct", 0) > 10:
        issues.append(f"⚠️ 交易成本侵蚀严重（累计{perf['total_cost_pct']:.1f}%），需降低换手率")
        suggestions.append("增加同股最小交易间隔（当前3天→建议5-7天），减少频繁进出")
    
    # 诊断7: 熊市表现
    bear_data = regime_stats.get("BEAR", {})
    if bear_data.get("count", 0) > 0:
        bear_wr = bear_data["wins"] / bear_data["count"] * 100
        bear_avg = bear_data["total_pnl"] / bear_data["count"]
        if bear_wr < 30 or bear_avg < -3:
            issues.append(f"❌ 熊市表现差（胜率{bear_wr:.0f}%，均亏{bear_avg:.1f}%）")
            suggestions.append("熊市中应大幅降低仓位或暂停交易（信号质量门槛70→80分）")
    
    # 诊断8: 连续亏损
    if perf.get("max_consec_loss", 0) > 8:
        issues.append(f"⚠️ 最大连续亏损{perf['max_consec_loss']}次，心理压力大")
        suggestions.append("优化连续亏损熔断器参数（3连亏暂停→2连亏降仓50%）")
    
    issues_html = "".join(f"<li>{iss}</li>" for iss in issues)
    suggestions_html = "".join(f"<li>{s}</li>" for s in suggestions)
    
    # === 组装HTML ===
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 15px; background: #f0f2f5; }}
.container {{ max-width: 1100px; margin: 0 auto; }}
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
.alert {{ padding: 10px 15px; border-radius: 6px; margin: 10px 0; font-size: 13px; }}
.alert-warn {{ background: #fff3cd; border-left: 4px solid #ffc107; }}
.alert-danger {{ background: #f8d7da; border-left: 4px solid #dc3545; }}
.alert-success {{ background: #d4edda; border-left: 4px solid #28a745; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📊 三年历史回测诊断报告（V5.1引擎）</h1>
        <div class="sub">回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,.0f}元 | 标的: {len(available_codes)}只 | 生成: {today} {now}</div>
    </div>
    <div class="content">

        <!-- 1. 核心绩效指标 -->
        <div class="panel" style="border: 2px solid #3498db;">
            <div class="panel-title" style="background: #ebf5fb;">📈 一、核心绩效指标</div>
            <div class="panel-body">
                <div class="grid-6">
                    <div class="metric"><div class="label">累计收益率</div><div class="value {'up' if perf.get('total_return',0)>0 else 'down'}">{perf.get('total_return',0):+.2f}%</div></div>
                    <div class="metric"><div class="label">年化收益率</div><div class="value {'up' if perf.get('annual_return',0)>0 else 'down'}">{perf.get('annual_return',0):+.2f}%</div></div>
                    <div class="metric"><div class="label">最大回撤</div><div class="value down">{perf.get('max_drawdown',0):.2f}%</div></div>
                    <div class="metric"><div class="label">夏普比率</div><div class="value">{perf.get('sharpe_ratio',0):.2f}</div></div>
                    <div class="metric"><div class="label">Sortino比率</div><div class="value">{perf.get('sortino_ratio',0):.2f}</div></div>
                    <div class="metric"><div class="label">Calmar比率</div><div class="value">{perf.get('calmar_ratio',0):.2f}</div></div>
                </div>
                <div class="grid-6" style="margin-top:10px">
                    <div class="metric"><div class="label">胜率</div><div class="value">{perf.get('win_rate',0):.1f}%</div></div>
                    <div class="metric"><div class="label">盈亏比</div><div class="value">{perf.get('profit_factor',0):.2f}</div></div>
                    <div class="metric"><div class="label">每笔期望</div><div class="value {'up' if perf.get('expectancy',0)>0 else 'down'}">{perf.get('expectancy',0):+.2f}%</div></div>
                    <div class="metric"><div class="label">总交易笔数</div><div class="value">{perf.get('total_trades',0)}</div></div>
                    <div class="metric"><div class="label">平均盈利</div><div class="value up">+{perf.get('avg_win',0):.2f}%</div></div>
                    <div class="metric"><div class="label">平均亏损</div><div class="value down">{perf.get('avg_loss',0):.2f}%</div></div>
                </div>
            </div>
        </div>

        <!-- 2. 基准对比 -->
        <div class="panel" style="border: 2px solid #e67e22;">
            <div class="panel-title" style="background: #fef9e7;">📊 二、基准对比（vs 沪深300）</div>
            <div class="panel-body">
                <div class="grid-6">
                    <div class="metric"><div class="label">策略累计收益</div><div class="value {'up' if perf.get('total_return',0)>0 else 'down'}">{perf.get('total_return',0):+.2f}%</div></div>
                    <div class="metric"><div class="label">沪深300收益</div><div class="value {'up' if perf.get('benchmark_return',0)>0 else 'down'}">{perf.get('benchmark_return',0):+.2f}%</div></div>
                    <div class="metric"><div class="label">超额收益</div><div class="value {'up' if perf.get('excess_return',0)>0 else 'down'}">{perf.get('excess_return',0):+.2f}%</div></div>
                    <div class="metric"><div class="label">年化Alpha</div><div class="value {'up' if perf.get('alpha',0)>0 else 'down'}">{perf.get('alpha',0):+.2f}%</div></div>
                    <div class="metric"><div class="label">策略年化</div><div class="value">{perf.get('annual_return',0):+.2f}%</div></div>
                    <div class="metric"><div class="label">基准年化</div><div class="value">{perf.get('benchmark_annual',0):+.2f}%</div></div>
                </div>
            </div>
        </div>

        <!-- 3. 回撤与风险 -->
        <div class="panel">
            <div class="panel-title">📉 三、回撤与风险</div>
            <div class="panel-body">
                <div class="grid">
                    <div class="metric"><div class="label">最大回撤</div><div class="value down">{perf.get('max_drawdown',0):.2f}%</div></div>
                    <div class="metric"><div class="label">最大连续亏损</div><div class="value">{perf.get('max_consec_loss',0)}次</div></div>
                    <div class="metric"><div class="label">平均持仓天数</div><div class="value">{perf.get('avg_hold',0):.0f}天</div></div>
                    <div class="metric"><div class="label">交易成本侵蚀</div><div class="value down">{perf.get('total_cost_pct',0):.1f}%</div></div>
                </div>
                <div class="grid" style="margin-top:10px">
                    <div class="metric"><div class="label">盈利单平均持仓</div><div class="value">{perf.get('avg_hold_win',0):.0f}天</div></div>
                    <div class="metric"><div class="label">亏损单平均持仓</div><div class="value">{perf.get('avg_hold_loss',0):.0f}天</div></div>
                    <div class="metric"><div class="label">单笔成本</div><div class="value">{perf.get('cost_per_trade_pct',0):.3f}%</div></div>
                    <div class="metric"><div class="label">回测年数</div><div class="value">{perf.get('n_years',0):.1f}年</div></div>
                </div>
            </div>
        </div>

        <!-- 4. 个股明细 -->
        <div class="panel">
            <div class="panel-title">📋 四、个股交易明细（按盈亏排序）</div>
            <div class="panel-body">
                <table>
                    <tr><th>代码</th><th>名称</th><th>类型</th><th>交易次数</th><th>胜率</th><th>累计盈亏</th><th>平均持仓</th></tr>
                    {stock_rows}
                </table>
            </div>
        </div>

        <!-- 5. 卖出原因分布 -->
        <div class="panel">
            <div class="panel-title">🏷️ 五、卖出原因分布</div>
            <div class="panel-body">
                <table>
                    <tr><th>卖出原因</th><th>次数</th><th>占比</th><th>胜率</th><th>平均收益</th></tr>
                    {sell_rows}
                </table>
            </div>
        </div>

        <!-- 6. 市场环境 -->
        <div class="panel" style="border: 2px solid #3498db;">
            <div class="panel-title" style="background: #ebf5fb;">🌐 六、分市场环境统计</div>
            <div class="panel-body">
                <table>
                    <tr><th>市场环境</th><th>交易笔数</th><th>占比</th><th>胜率</th><th>平均盈亏</th><th>平均持仓</th></tr>
                    {regime_rows}
                </table>
            </div>
        </div>

        <!-- 7. 半年度统计 -->
        <div class="panel" style="border: 2px solid #9b59b6;">
            <div class="panel-title" style="background: #f4ecf7;">📆 七、半年度周期统计</div>
            <div class="panel-body">
                <table>
                    <tr><th>周期</th><th>交易笔数</th><th>胜率</th><th>累计盈亏</th><th>每笔期望</th></tr>
                    {period_rows}
                </table>
            </div>
        </div>

        <!-- 8. 行业统计 -->
        <div class="panel">
            <div class="panel-title">🏭 八、行业分布</div>
            <div class="panel-body">
                <table>
                    <tr><th>行业</th><th>交易次数</th><th>胜率</th><th>累计盈亏</th></tr>
                    {industry_rows}
                </table>
            </div>
        </div>

        <!-- 9. 月度收益 -->
        <div class="panel">
            <div class="panel-title">📅 九、月度收益分布</div>
            <div class="panel-body">
                <table>
                    <tr><th>月份</th><th>交易笔数</th><th>胜率</th><th>月累计净收益</th></tr>
                    {monthly_rows}
                </table>
            </div>
        </div>

        <!-- 10. 问题诊断 -->
        <div class="panel" style="border: 2px solid #e74c3c;">
            <div class="panel-title" style="background: #fdedec;">🔍 十、问题诊断</div>
            <div class="panel-body">
                <ul style="font-size:13px;line-height:2">
                    {issues_html}
                </ul>
            </div>
        </div>

        <!-- 11. 改进建议 -->
        <div class="panel" style="border: 2px solid #27ae60;">
            <div class="panel-title" style="background: #d4edda;">💡 十一、改进建议</div>
            <div class="panel-body">
                <ol style="font-size:13px;line-height:2">
                    {suggestions_html}
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
                    <tr><td>滑点</td><td>龙头0.2% / 弹性0.5%</td><td>模拟真实成交偏差</td></tr>
                    <tr><td>T+1</td><td>✅ 启用</td><td>当日买入次日才能卖出</td></tr>
                    <tr><td>涨跌停</td><td>✅ 启用</td><td>涨停无法买入，跌停无法卖出</td></tr>
                    <tr><td>仓位</td><td>单笔12%资金</td><td>每笔交易使用总资金的12%</td></tr>
                </table>
            </div>
        </div>

        <div class="footer">
            操盘密码量化交易系统 - 三年回测诊断报告 | 仅供参考，不构成投资建议<br>
            股市有风险，投资需谨慎 | V10.0 | {today}
        </div>
    </div>
</div>
</body>
</html>'''
    
    return html


if __name__ == "__main__":
    run()
