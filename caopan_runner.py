#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
操盘密码分析运行器 V1.0
======================
对标东方财富付费软件"操盘密码"，一键生成分析图表+信号+回测

用法:
  python caopan_runner.py              # 分析所有持仓，生成图表
  python caopan_runner.py --backtest   # 3年回测+绩效报告
  python caopan_runner.py --scan       # 全市场批量扫描推荐
  python caopan_runner.py --optimize   # 参数网格优化
"""

import sys
import os
import io
import json
import datetime
import logging

# Windows编码修复
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADING_SYSTEM_DIR = os.path.join(BASE_DIR, "trading_system")
sys.path.insert(0, TRADING_SYSTEM_DIR)
sys.path.insert(0, BASE_DIR)

import config
from strategy.caopan_signal import CaopanEngine
from strategy.sector_flow import SectorMonitor, sector_summary
from strategy.multi_factor import MultiFactorScorer, factor_summary
from risk.position_sizing import PositionSizer, position_summary
from notify.alert_engine import AlertEngine
from output.caopan_chart import generate_caopan_chart, generate_batch_charts

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(TRADING_SYSTEM_DIR, "output", "caopan")


def load_holdings() -> dict:
    """加载持仓配置"""
    # 名称映射表
    NAME_MAP = {
        "588000": "科创50", "603501": "豪威集团", "002558": "巨人网络",
        "159205": "创业东财", "002185": "华天科技",
    }
    holdings_path = os.path.join(BASE_DIR, "holdings.json")
    if os.path.exists(holdings_path):
        with open(holdings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 补充name字段
        for code, info in data.items():
            if "name" not in info:
                info["name"] = NAME_MAP.get(code, info.get("sector", code))
        return data
    return {}


def fetch_stock_data(code: str, days: int = 500) -> "pd.DataFrame":
    """获取股票历史K线数据"""
    import baostock as bs
    import pandas as pd

    # 转换代码格式
    if "." not in code:
        if code.startswith("6") or code.startswith("5"):
            code = f"sh.{code}"
        else:
            code = f"sz.{code}"

    lg = bs.login()
    end_date = datetime.date.today().strftime("%Y-%m-%d")
    start_date = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

    rs = bs.query_history_k_data_plus(
        code,
        "date,code,open,high,low,close,volume,amount",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="2"  # 前复权
    )

    data_list = []
    while rs.error_code == '0' and rs.next():
        data_list.append(rs.get_row_data())

    bs.logout()

    if not data_list:
        return None

    df = pd.DataFrame(data_list, columns=rs.fields)
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df.reset_index(drop=True)
    return df


def run_analyze():
    """分析所有持仓，生成操盘密码图表"""
    print("\n" + "=" * 60)
    print("  📊 操盘密码自适应趋势策略引擎 V2.0")
    print("  自适应生命线 + 三重共振DK + 多维资金 + 盈亏比门槛 + 周线共振")
    print("=" * 60)

    holdings = load_holdings()
    if not holdings:
        print("\n⚠️ 未找到holdings.json，使用默认持仓列表")
        # 从config获取默认持仓
        holdings = {
            "588000": {"name": "科创50ETF", "shares": 170100, "cost": 1.063},
            "603501": {"name": "韦尔股份", "shares": 200, "cost": 131.35},
            "688234": {"name": "天岳先进", "shares": 500, "cost": 72.80},
            "002185": {"name": "华天科技", "shares": 2000, "cost": 12.50},
            "000858": {"name": "五粮液", "shares": 500, "cost": 148.60},
        }

    engine = CaopanEngine()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    results = []
    print(f"\n📡 正在获取数据并分析 {len(holdings)} 只标的...\n")

    for code, info in holdings.items():
        name = info.get("name", code)
        print(f"  分析 {name}({code})...", end=" ")

        try:
            df = fetch_stock_data(code, days=500)
            if df is None or len(df) < 60:
                print("❌ 数据不足")
                continue

            result = engine.analyze(df, code=code, name=name)
            if "error" in result:
                print(f"❌ {result['error']}")
                continue

            # 生成图表
            chart_path = os.path.join(OUTPUT_DIR, f"caopan_{code}_{datetime.date.today().strftime('%Y%m%d')}.html")
            generate_caopan_chart(result, output_path=chart_path)

            # 输出摘要
            dk = result.get("dk_signal") or "无"
            trend = result.get("trend_desc", "")
            grade = result.get("dk_grade", "")
            filtered = result.get("dk_filtered", False)
            action = result.get("action_suggestion", {})
            rr = result.get("risk_reward", {})
            env = result.get("market_env", {})
            filter_mark = "[已过滤]" if filtered else ""
            print(f"✅ {trend} | DK={dk}({result.get('dk_strength',0)}分/{grade}){filter_mark} | 盈亏比{rr.get('risk_reward_1',0):.1f}:1 | {action.get('desc','')}")

            results.append(result)

        except Exception as e:
            print(f"❌ 异常: {e}")

    # 输出汇总
    print(f"\n{'─' * 60}")
    print(f"  分析完成: {len(results)}/{len(holdings)} 只成功")
    print(f"  图表目录: {OUTPUT_DIR}")

    # 信号汇总（仅显示未过滤的有效信号）
    d_signals = [r for r in results if r.get("dk_signal") == "D" and r.get("dk_strength", 0) >= 50 and not r.get("dk_filtered", False)]
    k_signals = [r for r in results if r.get("dk_signal") == "K" and r.get("dk_strength", 0) >= 50 and not r.get("dk_filtered", False)]
    filtered_signals = [r for r in results if r.get("dk_signal") and r.get("dk_filtered", False)]
    if d_signals:
        print(f"\n  🔴 D点买入信号({len(d_signals)}只, 三重确认通过):")
        for r in d_signals:
            rr = r.get("risk_reward", {})
            print(f"     {r['name']}({r['code']}) {r['dk_grade']}信号 {r['dk_strength']}分 | 盈亏比{rr.get('risk_reward_1',0):.1f}:1 | {r['dk_reason']}")
    if k_signals:
        print(f"\n  🟢 K点卖出信号({len(k_signals)}只, 三重确认通过):")
        for r in k_signals:
            print(f"     {r['name']}({r['code']}) {r['dk_grade']}信号 {r['dk_strength']}分 | {r['dk_reason']}")
    if filtered_signals:
        print(f"\n  ⛔ 已过滤信号({len(filtered_signals)}只, 未通过三重确认/盈亏比/周线):")
        for r in filtered_signals:
            print(f"     {r['name']}({r['code']}) {r.get('dk_grade','')} | {r.get('dk_reason','')}")

    # 操作建议汇总
    print(f"\n  📋 操作建议:")
    for r in results:
        action = r.get("action_suggestion", {})
        urgency_icon = {"critical": "🚨", "high": "⚡", "warning": "⚠️", "normal": "  "}.get(action.get("urgency"), "  ")
        print(f"     {urgency_icon} {r['name']}: {action.get('desc', '观望')} - {action.get('detail', '')}")

    # ═══ 最终分析报告 ═══
    _print_final_report(results, holdings)

    # ═══ 智能预警检查 ═══
    alert_engine = AlertEngine(holdings=holdings)
    triggered = alert_engine.check_alerts(results)
    if triggered:
        print(f"\n  🔔 智能预警触发 {len(triggered)} 条:")
    else:
        print(f"\n  ✅ 无预警触发")

    print(f"\n{'=' * 60}\n")
    return results


def _print_final_report(results: list, holdings: dict):
    """输出最终综合分析报告"""
    if not results:
        return

    print(f"\n{'═' * 60}")
    print(f"  📊 操盘密码 V2.0 最终分析报告")
    print(f"  生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═' * 60}")

    # 1. 趋势分布
    trend_dist = {5: [], 4: [], 3: [], 2: [], 1: []}
    for r in results:
        lv = r.get("trend_level", 3)
        trend_dist[lv].append(r)

    print(f"\n  ━━ 一、趋势分布 ━━")
    trend_names = {5: "强上升(5级)", 4: "弱上升(4级)", 3: "震荡(3级)", 2: "弱下跌(2级)", 1: "强下跌(1级)"}
    for lv in [5, 4, 3, 2, 1]:
        stocks = trend_dist[lv]
        if stocks:
            names_str = "、".join([f"{r['name']}" for r in stocks])
            icon = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🟢", 1: "🟢"}.get(lv, "")
            print(f"     {icon} {trend_names[lv]}: {len(stocks)}只 → {names_str}")

    # 2. 核心信号
    print(f"\n  ━━ 二、DK信号与资金 ━━")
    for r in results:
        dk = r.get("dk_signal") or "无"
        strength = r.get("dk_strength", 0)
        grade = r.get("dk_grade", "-")
        streak = r.get("main_flow_streak", 0)
        pattern_cn = {"mild_build": "温和建仓", "surge": "放量拉升", "fake": "对倒骗线", "normal": "-"}.get(
            r.get("fund_pattern", "normal"), "-")
        env_cn = {"trend": "趋势市", "oscillation": "震荡市", "transition": "转换期"}.get(
            r.get("market_env", {}).get("mode", ""), "-")
        rr = r.get("risk_reward", {})
        rr_val = rr.get("risk_reward_1", 0)
        rr_pass = "✅" if rr.get("passed") else "❌"
        print(f"     {r['name']:<8} DK={dk}({strength}分/{grade}) | 主力连流{streak}天 | "
              f"资金:{pattern_cn} | 环境:{env_cn} | 盈亏比{rr_val:.1f}:1{rr_pass}")

    # 3. 操作优先级
    print(f"\n  ━━ 三、操作优先级排序 ━━")
    # 按紧急程度排序
    priority_map = {"critical": 0, "high": 1, "warning": 2, "normal": 3}
    sorted_results = sorted(results, key=lambda r: priority_map.get(r.get("action_suggestion", {}).get("urgency", "normal"), 3))
    for i, r in enumerate(sorted_results, 1):
        action = r.get("action_suggestion", {})
        urgency = action.get("urgency", "normal")
        icon = {"critical": "🚨", "high": "⚡", "warning": "⚠️", "normal": "✅"}.get(urgency, "  ")
        close = r.get("close", 0)
        support = r.get("support_price", 0)
        resistance = r.get("resistance_price", 0)
        print(f"     {i}. {icon} {r['name']}({r['code']}) | 现价{close:.2f} | "
              f"支撑{support:.2f} 压力{resistance:.2f}")
        print(f"        → {action.get('desc', '观望')}: {action.get('detail', '')}")

    # 4. 风险提示
    print(f"\n  ━━ 四、风险提示 ━━")
    risk_items = []
    for r in results:
        # 下跌趋势持仓
        if r.get("trend_level", 3) <= 2:
            risk_items.append(f"🚨 {r['name']}: 下跌趋势({r.get('trend_level')}级)，建议清仓止损")
        # 对倒骗线
        if r.get("fund_pattern") == "fake":
            risk_items.append(f"⚠️ {r['name']}: 检测到对倒骗线行为，主力出货嫌疑")
        # 顶背离
        if r.get("fund_divergence") == "top":
            risk_items.append(f"⚠️ {r['name']}: 顶背离信号，价格新高但主力流出")
        # 超买
        if r.get("deviation_pct", 0) > 10:
            risk_items.append(f"⚠️ {r['name']}: 乖离率{r.get('deviation_pct',0):.1f}%超买，注意回调")
    if risk_items:
        for item in risk_items:
            print(f"     {item}")
    else:
        print(f"     ✅ 当前无重大风险信号")

    # 5. 筹码分布
    print(f"\n  ━━ 五、筹码分布 ━━")
    for r in results:
        chip = r.get("chip")
        if not chip:
            continue
        pr = chip.get("profit_ratio", 0)
        conc = chip.get("concentration", 0)
        ctrl = chip.get("control_level", {})
        pattern = chip.get("pattern", {})
        peaks = chip.get("peaks", [])
        peaks_str = " | ".join([f"{p['price']:.2f}" for p in peaks[:2]]) if peaks else "-"
        print(f"     {r['name']:<8} 获利{pr*100:.0f}% | 集中{conc*100:.1f}% | "
              f"{ctrl.get('level','-')}({ctrl.get('score',0)}分) | "
              f"{pattern.get('name','-')} | 峰:{peaks_str}")

    # 6. 板块轮动
    print(f"\n  ━━ 六、板块轮动 ━━")
    try:
        holdings_data = {r.get("code", ""): r.get("df_analyzed") for r in results if r.get("df_analyzed") is not None}
        monitor = SectorMonitor()
        sector_result = monitor.analyze(holdings_data, holdings)
        print(sector_summary(sector_result))
    except Exception as e:
        print(f"     板块分析异常: {e}")

    # 7. 多因子评分
    print(f"\n  ━━ 七、多因子评分 ━━")
    try:
        scorer = MultiFactorScorer()
        scored = scorer.score_all(results)
        print(factor_summary(scored))
    except Exception as e:
        print(f"     多因子评分异常: {e}")

    # 8. 仓位管理
    print(f"\n  ━━ 八、仓位管理 ━━")
    try:
        sizer = PositionSizer(total_capital=750000)
        plan = sizer.calc_positions(results, holdings)
        print(position_summary(plan))
    except Exception as e:
        print(f"     仓位计算异常: {e}")

    # 9. 持仓市值估算
    print(f"\n  ━━ 九、持仓概览 ━━")
    total_cost = 0
    total_value = 0
    for r in results:
        code = r.get("code", "")
        info = holdings.get(code, {})
        shares = info.get("shares", 0)
        cost = info.get("cost", 0) or info.get("buy_price", 0)
        close = r.get("close", 0)
        if shares and cost:
            cost_val = shares * cost
            mkt_val = shares * close
            pnl_pct = (mkt_val - cost_val) / cost_val * 100 if cost_val > 0 else 0
            total_cost += cost_val
            total_value += mkt_val
            pnl_icon = "📈" if pnl_pct > 0 else "📉"
            print(f"     {pnl_icon} {r['name']:<8} {shares}股 | 成本{cost:.3f} 现价{close:.3f} | "
                  f"盈亏{pnl_pct:+.1f}% | 市值{mkt_val/10000:.1f}万")
    if total_cost > 0:
        total_pnl = (total_value - total_cost) / total_cost * 100
        print(f"     {'─'*50}")
        print(f"     总成本: {total_cost/10000:.1f}万 | 总市值: {total_value/10000:.1f}万 | 总盈亏: {total_pnl:+.1f}%")

    print(f"\n{'═' * 60}")


def run_backtest():
    """3年回测 + 绩效报告"""
    print("\n" + "=" * 60)
    print("  📈 操盘密码策略回测 (近3年)")
    print("  DK信号 + 控盘生命线 | 含真实成本(佣金万3+印花税千1+滑点千1)")
    print("=" * 60)

    holdings = load_holdings()
    if not holdings:
        holdings = {
            "588000": {"name": "科创50ETF"},
            "603501": {"name": "韦尔股份"},
            "002185": {"name": "华天科技"},
            "000858": {"name": "五粮液"},
        }

    engine = CaopanEngine()
    all_results = []

    print(f"\n📡 获取3年数据并回测...\n")

    for code, info in holdings.items():
        name = info.get("name", code)
        print(f"  回测 {name}({code})...", end=" ")

        try:
            df = fetch_stock_data(code, days=1200)  # ~3年
            if df is None or len(df) < 120:
                print("❌ 数据不足")
                continue

            bt = engine.backtest(df, code=code)
            if "error" in bt:
                print(f"❌ {bt['error']}")
                continue

            all_results.append({**bt, "name": name})
            pass_mark = "✅达标" if bt["all_pass"] else "❌未达标"
            print(f"✅ 交易{bt['total_trades']}次 | 胜率{bt['win_rate']:.0%} | 盈亏比{bt['profit_factor']:.1f} | 年化{bt['annual_return']:.1%} | 回撤{bt['max_drawdown']:.1%} | {pass_mark}")

        except Exception as e:
            print(f"❌ 异常: {e}")

    # 输出完整报告
    if all_results:
        print(f"\n{'═' * 60}")
        print(f"  📊 回测绩效汇总报告")
        print(f"{'═' * 60}")
        print(f"  {'标的':<10} {'交易':>4} {'胜率':>6} {'盈亏比':>6} {'年化':>8} {'回撤':>6} {'夏普':>5} {'达标':>4}")
        print(f"  {'─'*56}")

        for bt in all_results:
            name = bt.get("name", bt["code"])[:8]
            mark = "✅" if bt["all_pass"] else "❌"
            print(f"  {name:<10} {bt['total_trades']:>4} {bt['win_rate']:>6.0%} {bt['profit_factor']:>6.1f} "
                  f"{bt['annual_return']:>7.1%} {bt['max_drawdown']:>6.1%} {bt['sharpe_ratio']:>5.1f} {mark:>4}")

        # 达标验证
        print(f"\n  验证标准: 年化≥20% | 回撤≤20% | 盈亏比≥2 | 胜率≥35%")
        passed = [bt for bt in all_results if bt["all_pass"]]
        print(f"  达标: {len(passed)}/{len(all_results)} 只")

        # 信号类型拆分
        print(f"\n  📋 信号类型拆分:")
        for bt in all_results:
            trades = bt.get("trades", [])
            # 配对买卖交易
            buy_trades = [t for t in trades if t["action"] == "buy"]
            sell_trades = [t for t in trades if t["action"] == "sell"]
            # D点买入后的卖出结果
            d_buys = [t for t in buy_trades if "D点" in t.get("reason", "")]
            # 卖出原因拆分
            k_sells = [t for t in sell_trades if "K点" in t.get("reason", "")]
            stop_sells = [t for t in sell_trades if "跌破" in t.get("reason", "")]
            profit_sells = [t for t in sell_trades if "止盈" in t.get("reason", "")]
            # D点买入后最终盈利的比例
            d_win_count = len([t for t in sell_trades if t.get("pnl", 0) > 0])
            total_sells = len(sell_trades)
            print(f"    {bt.get('name', bt['code'])}: D点买入{len(d_buys)}次 | "
                  f"卖出: K点{len(k_sells)}次/止损{len(stop_sells)}次/止盈{len(profit_sells)}次 | "
                  f"整体胜率{d_win_count}/{total_sells}={d_win_count/max(total_sells,1):.0%}")

    print(f"\n{'=' * 60}\n")
    return all_results


def run_scan():
    """全市场批量扫描"""
    print("\n" + "=" * 60)
    print("  🔍 操盘密码全市场扫描")
    print("  筛选: 上升趋势 + D点信号 + 主力连续流入 + 偏离≤8%")
    print("=" * 60)

    # 获取候选股票池
    scan_pool = _get_scan_pool()
    if not scan_pool:
        print("\n❌ 无法获取扫描股票池")
        return []

    print(f"\n📡 扫描 {len(scan_pool)} 只标的...\n")

    engine = CaopanEngine()
    data_dict = {}
    names = {}
    count = 0

    for code, name in scan_pool.items():
        try:
            df = fetch_stock_data(code, days=300)
            if df is not None and len(df) >= 60:
                data_dict[code] = df
                names[code] = name
                count += 1
                if count % 20 == 0:
                    print(f"  已加载 {count}/{len(scan_pool)} ...")
        except Exception:
            continue

    print(f"\n  数据加载完成: {count}只有效")
    print(f"  正在计算信号...")

    # 严格筛选
    filtered = engine.scan_filter(data_dict, names)

    # 输出结果
    print(f"\n{'═' * 60}")
    print(f"  🎯 扫描结果: {len(filtered)} 只标的满足全部条件")
    print(f"{'═' * 60}")

    if filtered:
        print(f"\n  {'排名':<4} {'代码':<8} {'名称':<10} {'趋势':<6} {'DK强度':>6} {'主力连流':>6} {'偏离%':>6} {'建议'}")
        print(f"  {'─'*70}")
        for i, r in enumerate(filtered[:20], 1):
            action = r.get("action_suggestion", {})
            print(f"  {i:<4} {r['code']:<8} {r['name']:<10} {r['trend_desc']:<6} "
                  f"{r['dk_strength']:>6} {r['main_flow_streak']:>5}天 {r['deviation_pct']:>5.1f}% {action.get('desc','')}")

        # 生成图表
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        paths = generate_batch_charts(filtered[:10], OUTPUT_DIR)
        print(f"\n  📊 已生成 {len(paths)} 张分析图表 → {OUTPUT_DIR}")
    else:
        print(f"\n  当前无满足全部条件的标的（市场可能处于弱势期）")

        # 放宽条件输出次优
        all_results = engine.scan_batch(data_dict, names)
        up_trend = [r for r in all_results if r["trend_type"] == "up"]
        if up_trend:
            print(f"\n  📋 次优推荐（上升趋势，{len(up_trend)}只）:")
            for r in up_trend[:10]:
                dk = r.get("dk_signal") or "无"
                print(f"    {r['name']}({r['code']}) DK={dk}({r.get('dk_strength',0)}) 偏离{r['deviation_pct']:.1f}% 主力{r['main_flow_streak']}日")

    print(f"\n{'=' * 60}\n")
    return filtered


def run_optimize():
    """参数网格优化"""
    print("\n" + "=" * 60)
    print("  ⚙️ 操盘密码参数优化（网格搜索）")
    print("=" * 60)

    holdings = load_holdings()
    if not holdings:
        print("❌ 无持仓数据")
        return

    # 取第一只持仓做优化
    code = list(holdings.keys())[0]
    name = holdings[code].get("name", code)
    print(f"\n  优化标的: {name}({code})")
    print(f"  搜索空间: {config.CAOPAN_CONFIG['optimize_grid']}")

    df = fetch_stock_data(code, days=1200)
    if df is None:
        print("❌ 数据获取失败")
        return

    engine = CaopanEngine()
    results = engine.optimize_params(df, code=code)

    if results:
        print(f"\n  共测试 {len(results)} 组有效参数")
        print(f"\n  {'排名':<4} {'快线':>4} {'慢线':>4} {'偏离%':>6} {'夏普':>6} {'年化':>8} {'回撤':>6} {'胜率':>6} {'盈亏比':>6} {'达标':>4}")
        print(f"  {'─'*65}")
        for i, r in enumerate(results[:10], 1):
            p = r["params"]
            mark = "✅" if r["all_pass"] else ""
            print(f"  {i:<4} {p.get('life_line_fast',''):>4} {p.get('life_line_slow',''):>4} "
                  f"{p.get('deviation_high_pct',0)*100:>5.0f}% {r['sharpe']:>6.2f} "
                  f"{r['annual_return']:>7.1%} {r['max_drawdown']:>6.1%} "
                  f"{r['win_rate']:>5.0%} {r['profit_factor']:>6.1f} {mark:>4}")

        best = results[0]
        print(f"\n  🏆 最优参数: {best['params']}")
        print(f"     夏普={best['sharpe']:.2f} 年化={best['annual_return']:.1%} 回撤={best['max_drawdown']:.1%}")
    else:
        print("  ❌ 无有效结果（可能交易次数不足）")

    print(f"\n{'=' * 60}\n")


def _get_scan_pool() -> dict:
    """获取扫描股票池"""
    # 优先从recommend_engine获取候选池
    try:
        from strategy.recommend_engine import RecommendEngine
        rec = RecommendEngine()
        pool = rec.get_candidate_pool()
        if pool:
            return {item["code"]: item["name"] for item in pool[:100]}
    except Exception:
        pass

    # 备选: 从config获取赛道龙头
    try:
        pool = {}
        sectors = getattr(config, "SECTOR_CONFIG", {})
        for sector_name, sector_info in sectors.items():
            stocks = sector_info.get("stocks", [])
            for s in stocks[:5]:
                if isinstance(s, dict):
                    pool[s.get("code", "")] = s.get("name", "")
                elif isinstance(s, str):
                    pool[s] = s
        if pool:
            return pool
    except Exception:
        pass

    # 最终备选: 沪深300成分股
    try:
        import baostock as bs
        lg = bs.login()
        rs = bs.query_hs300_stocks()
        pool = {}
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            code = row[1].replace("sh.", "").replace("sz.", "")
            pool[code] = row[2]
        bs.logout()
        return dict(list(pool.items())[:100])  # 取前100只
    except Exception as e:
        logger.warning(f"获取扫描池失败: {e}")
        return {}


def run_orders():
    """批量条件单生成（基于V2.0信号+动态止损）"""
    print("\n" + "=" * 60)
    print("  📝 操盘密码批量条件单生成 V2.0")
    print("  动态止损(沿LL2上移) + 超买止盈 + K点清仓")
    print("=" * 60)

    holdings = load_holdings()
    if not holdings:
        print("❌ 无持仓数据")
        return

    engine = CaopanEngine()
    orders = []

    print(f"\n📡 分析{len(holdings)}只持仓并生成条件单...\n")

    for code, info in holdings.items():
        name = info.get("name", code)
        try:
            df = fetch_stock_data(code, days=300)
            if df is None or len(df) < 60:
                continue

            result = engine.analyze(df, code=code, name=name)
            if "error" in result:
                continue

            close = result["close"]
            ll2 = result.get("support_price", close * 0.95)
            trend_level = result.get("trend_level", 3)
            rr = result.get("risk_reward", {})
            action = result.get("action_suggestion", {})

            # 动态止损: LL2下方3%
            stop_loss = round(ll2 * (1 - config.CAOPAN_CONFIG["stop_loss_below_ll2"]), 3)
            # 超买止盈: 乖离>10%的价位
            overbought_price = round(result.get("ll_fast", close) * (1 + config.CAOPAN_CONFIG["deviation_overbought"]), 3)
            # 目标位
            target_1 = rr.get("target_1", close * 1.1)
            target_2 = rr.get("target_2", close * 1.2)

            order = {
                "code": code, "name": name, "close": close,
                "trend": result.get("trend_desc", ""),
                "trend_level": trend_level,
                "stop_loss": stop_loss,
                "overbought_price": overbought_price,
                "target_1": target_1, "target_2": target_2,
                "action": action.get("desc", ""),
                "dk_signal": result.get("dk_signal"),
                "deviation_pct": result.get("deviation_pct", 0),
            }
            orders.append(order)

            print(f"  {name}({code}) | 趋势{trend_level}级 | 止损{stop_loss} | 超买{overbought_price} | {action.get('desc','')}")

        except Exception as e:
            print(f"  {name}({code}) ❌ {e}")

    # 输出条件单表格
    if orders:
        print(f"\n{'═' * 70}")
        print(f"  📋 条件单参数表（可直接复制到东方财富）")
        print(f"{'═' * 70}")
        print(f"  {'标的':<10} {'最新价':>7} {'止损价':>7} {'超买止盈':>8} {'目标1':>7} {'目标2':>7} {'趋势':>6} {'建议'}")
        print(f"  {'─'*66}")
        for o in orders:
            print(f"  {o['name']:<10} {o['close']:>7.3f} {o['stop_loss']:>7.3f} {o['overbought_price']:>8.3f} "
                  f"{o['target_1']:>7.3f} {o['target_2']:>7.3f} {o['trend']:>6} {o['action']}")

        # 导出JSON
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        orders_path = os.path.join(OUTPUT_DIR, f"caopan_orders_{datetime.date.today().strftime('%Y%m%d')}.json")
        with open(orders_path, "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=2)
        print(f"\n  📁 条件单已导出: {orders_path}")

    print(f"\n{'=' * 60}\n")


def main():
    args = sys.argv[1:]

    if "--backtest" in args:
        run_backtest()
    elif "--scan" in args:
        run_scan()
    elif "--optimize" in args:
        run_optimize()
    elif "--orders" in args:
        run_orders()
    elif "-h" in args or "--help" in args:
        print(__doc__)
    else:
        run_analyze()


if __name__ == "__main__":
    main()
