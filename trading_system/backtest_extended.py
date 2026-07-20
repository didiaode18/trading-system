"""
大规模模拟实战回测 V6.0
========================
目标: 通过大量历史数据模拟，找出策略弱点并优化

扩展测试:
- 50只股票（覆盖更多行业）
- 5年历史数据（2020-2025）
- 分年度/分行业/分市场环境统计
- 亏损交易深度分析
"""

import sys
import os
import datetime
import logging
import json
from collections import defaultdict

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from backtest_real import (
    fetch_history_data, compute_indicators, backtest_stock_v5,
    analyze_trades, COMMISSION, SLIPPAGE_LEADER, SLIPPAGE_FLEX
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# 扩展测试标的池（50只，覆盖A股主要行业龙头）
# ============================================================

EXTENDED_STOCKS = {
    # === 半导体（8只）===
    "002371": {"名称": "北方华创", "类型": "龙头", "行业": "半导体"},
    "002409": {"名称": "雅克科技", "类型": "龙头", "行业": "半导体"},
    "600584": {"名称": "长电科技", "类型": "龙头", "行业": "半导体"},
    "603986": {"名称": "兆易创新", "类型": "龙头", "行业": "半导体"},
    "002049": {"名称": "紫光国微", "类型": "龙头", "行业": "半导体"},
    "688981": {"名称": "中芯国际", "类型": "龙头", "行业": "半导体"},
    "603501": {"名称": "韦尔股份", "类型": "龙头", "行业": "半导体"},
    "688012": {"名称": "中微公司", "类型": "龙头", "行业": "半导体"},
    # === 新能源（6只）===
    "300750": {"名称": "宁德时代", "类型": "龙头", "行业": "新能源"},
    "002594": {"名称": "比亚迪", "类型": "龙头", "行业": "新能源"},
    "601012": {"名称": "隆基绿能", "类型": "龙头", "行业": "新能源"},
    "300274": {"名称": "阳光电源", "类型": "龙头", "行业": "新能源"},
    "002460": {"名称": "赣锋锂业", "类型": "龙头", "行业": "新能源"},
    "300014": {"名称": "亿纬锂能", "类型": "龙头", "行业": "新能源"},
    # === AI/科技（6只）===
    "002230": {"名称": "科大讯飞", "类型": "龙头", "行业": "AI"},
    "300033": {"名称": "同花顺", "类型": "弹性", "行业": "AI"},
    "002415": {"名称": "海康威视", "类型": "龙头", "行业": "AI"},
    "300496": {"名称": "中科创达", "类型": "弹性", "行业": "AI"},
    "688111": {"名称": "金山办公", "类型": "龙头", "行业": "AI"},
    "300124": {"名称": "汇川技术", "类型": "龙头", "行业": "AI"},
    # === 军工（5只）===
    "600118": {"名称": "中国卫星", "类型": "弹性", "行业": "军工"},
    "600760": {"名称": "中航沈飞", "类型": "弹性", "行业": "军工"},
    "600893": {"名称": "航发动力", "类型": "龙头", "行业": "军工"},
    "000768": {"名称": "中航西飞", "类型": "龙头", "行业": "军工"},
    "600150": {"名称": "中国船舶", "类型": "龙头", "行业": "军工"},
    # === 消费（5只）===
    "600519": {"名称": "贵州茅台", "类型": "龙头", "行业": "消费"},
    "000858": {"名称": "五粮液", "类型": "龙头", "行业": "消费"},
    "603288": {"名称": "海天味业", "类型": "龙头", "行业": "消费"},
    "000568": {"名称": "泸州老窖", "类型": "龙头", "行业": "消费"},
    "600887": {"名称": "伊利股份", "类型": "龙头", "行业": "消费"},
    # === 医药（5只）===
    "300760": {"名称": "迈瑞医疗", "类型": "龙头", "行业": "医药"},
    "600276": {"名称": "恒瑞医药", "类型": "龙头", "行业": "医药"},
    "300347": {"名称": "泰格医药", "类型": "龙头", "行业": "医药"},
    "000661": {"名称": "长春高新", "类型": "龙头", "行业": "医药"},
    "300122": {"名称": "智飞生物", "类型": "龙头", "行业": "医药"},
    # === 金融（5只）===
    "601318": {"名称": "中国平安", "类型": "龙头", "行业": "金融"},
    "600036": {"名称": "招商银行", "类型": "龙头", "行业": "金融"},
    "601166": {"名称": "兴业银行", "类型": "龙头", "行业": "金融"},
    "600030": {"名称": "中信证券", "类型": "龙头", "行业": "金融"},
    "601688": {"名称": "华泰证券", "类型": "龙头", "行业": "金融"},
    # === 其他行业（10只）===
    "000725": {"名称": "京东方A", "类型": "弹性", "行业": "面板"},
    "002384": {"名称": "东山精密", "类型": "弹性", "行业": "电子"},
    "601899": {"名称": "紫金矿业", "类型": "龙头", "行业": "有色"},
    "600309": {"名称": "万华化学", "类型": "龙头", "行业": "化工"},
    "601888": {"名称": "中国中免", "类型": "龙头", "行业": "消费"},
    "600900": {"名称": "长江电力", "类型": "龙头", "行业": "电力"},
    "601088": {"名称": "中国神华", "类型": "龙头", "行业": "煤炭"},
    "600031": {"名称": "三一重工", "类型": "龙头", "行业": "机械"},
    "000333": {"名称": "美的集团", "类型": "龙头", "行业": "家电"},
    "600585": {"名称": "海螺水泥", "类型": "龙头", "行业": "建材"},
}


def run_extended_backtest():
    """运行扩展回测"""
    logger.info("=" * 70)
    logger.info("  大规模模拟实战回测 V6.0")
    logger.info(f"  标的: {len(EXTENDED_STOCKS)}只 | 区间: 2020-01-01 ~ 今")
    logger.info("=" * 70)

    all_trades = []
    stock_results = {}
    failed_stocks = []

    for idx, (code, info) in enumerate(EXTENDED_STOCKS.items(), 1):
        logger.info(f"[{idx}/{len(EXTENDED_STOCKS)}] 回测: {code} {info['名称']}...")
        try:
            df = fetch_history_data(code, start_date="2020-01-01")
            if df.empty or len(df) < 120:
                logger.warning(f"  {code} 数据不足({len(df)}条)，跳过")
                failed_stocks.append(code)
                continue

            trades = backtest_stock_v5(df, code, info)
            all_trades.extend(trades)
            stock_results[code] = {
                "name": info["名称"],
                "industry": info["行业"],
                "trades": len(trades),
                "data_days": len(df)
            }
            logger.info(f"  {len(trades)}笔交易")
        except Exception as e:
            logger.error(f"  {code} 失败: {e}")
            failed_stocks.append(code)

    if not all_trades:
        logger.error("无交易记录！")
        return None

    # 按时间排序
    all_trades.sort(key=lambda x: x["buy_date"])

    # 总体统计
    stats = analyze_trades(all_trades)
    
    logger.info("\n" + "=" * 70)
    logger.info("  总体统计")
    logger.info("=" * 70)
    logger.info(f"  总交易: {stats['total']}笔")
    logger.info(f"  胜率: {stats['win_rate']}%")
    logger.info(f"  盈亏比: {stats['profit_factor']}")
    logger.info(f"  每笔期望: {stats['expectancy']:+.2f}%")
    logger.info(f"  累计收益: {stats['cumulative']:+.2f}%")
    logger.info(f"  平均持仓: {stats['avg_hold']}天")
    logger.info(f"  最大连亏: {stats['max_consec_loss']}次")

    # 深度分析
    deep_analysis = analyze_deep(all_trades)
    
    return {
        "stats": stats,
        "trades": all_trades,
        "deep_analysis": deep_analysis,
        "stock_results": stock_results,
        "failed_stocks": failed_stocks
    }


def analyze_deep(trades: list) -> dict:
    """深度分析交易数据"""
    analysis = {}
    
    # 1. 分年度统计
    yearly = defaultdict(list)
    for t in trades:
        year = t["buy_date"][:4]
        yearly[year].append(t)
    
    analysis["yearly"] = {}
    for year, ts in sorted(yearly.items()):
        s = analyze_trades(ts)
        analysis["yearly"][year] = {
            "trades": s["total"],
            "win_rate": s["win_rate"],
            "expectancy": s["expectancy"],
            "cumulative": s["cumulative"]
        }
    
    # 2. 亏损交易分析
    losing_trades = [t for t in trades if t["net_profit"] <= 0]
    analysis["losing"] = {
        "count": len(losing_trades),
        "by_sell_type": defaultdict(int),
        "by_industry": defaultdict(int),
        "avg_loss": np.mean([t["net_profit"] for t in losing_trades]) if losing_trades else 0,
        "max_loss": min([t["net_profit"] for t in trades]) if trades else 0,
        "avg_hold_days": np.mean([t["hold_days"] for t in losing_trades]) if losing_trades else 0,
    }
    for t in losing_trades:
        analysis["losing"]["by_sell_type"][t["sell_type"]] += 1
        analysis["losing"]["by_industry"][t["industry"]] += 1
    
    # 3. 盈利交易分析
    winning_trades = [t for t in trades if t["net_profit"] > 0]
    analysis["winning"] = {
        "count": len(winning_trades),
        "by_sell_type": defaultdict(int),
        "avg_win": np.mean([t["net_profit"] for t in winning_trades]) if winning_trades else 0,
        "max_win": max([t["net_profit"] for t in trades]) if trades else 0,
        "avg_hold_days": np.mean([t["hold_days"] for t in winning_trades]) if winning_trades else 0,
    }
    for t in winning_trades:
        analysis["winning"]["by_sell_type"][t["sell_type"]] += 1
    
    # 4. 持仓天数分析
    hold_buckets = {"1-5天": [], "6-10天": [], "11-20天": [], "21-30天": [], "30天+": []}
    for t in trades:
        d = t["hold_days"]
        if d <= 5:
            hold_buckets["1-5天"].append(t)
        elif d <= 10:
            hold_buckets["6-10天"].append(t)
        elif d <= 20:
            hold_buckets["11-20天"].append(t)
        elif d <= 30:
            hold_buckets["21-30天"].append(t)
        else:
            hold_buckets["30天+"].append(t)
    
    analysis["hold_days"] = {}
    for bucket, ts in hold_buckets.items():
        if ts:
            s = analyze_trades(ts)
            analysis["hold_days"][bucket] = {
                "count": len(ts),
                "win_rate": s["win_rate"],
                "expectancy": s["expectancy"]
            }
    
    # 5. 卖出原因详细分析
    analysis["sell_types"] = {}
    for st, data in stats["sell_stats"].items() if "sell_stats" in dir() else []:
        pass
    
    sell_type_trades = defaultdict(list)
    for t in trades:
        sell_type_trades[t["sell_type"]].append(t)
    
    for st, ts in sell_type_trades.items():
        s = analyze_trades(ts)
        analysis["sell_types"][st] = {
            "count": len(ts),
            "win_rate": s["win_rate"],
            "expectancy": s["expectancy"],
            "avg_profit": np.mean([t["net_profit"] for t in ts])
        }
    
    # 6. 行业分析
    analysis["industry"] = {}
    industry_trades = defaultdict(list)
    for t in trades:
        industry_trades[t["industry"]].append(t)
    
    for ind, ts in industry_trades.items():
        s = analyze_trades(ts)
        analysis["industry"][ind] = {
            "count": len(ts),
            "win_rate": s["win_rate"],
            "expectancy": s["expectancy"],
            "cumulative": s["cumulative"]
        }
    
    # 7. 连续亏损分析
    max_streak = 0
    current_streak = 0
    streak_losses = []
    for t in trades:
        if t["net_profit"] <= 0:
            current_streak += 1
            streak_losses.append(t["net_profit"])
            if current_streak > max_streak:
                max_streak = current_streak
        else:
            if current_streak >= 3:  # 记录3连亏以上的段
                pass
            current_streak = 0
            streak_losses = []
    
    analysis["streak"] = {
        "max_consecutive_loss": max_streak,
    }
    
    return analysis


def print_deep_analysis(result: dict):
    """打印深度分析结果"""
    if not result:
        return
    
    stats = result["stats"]
    deep = result["deep_analysis"]
    
    print("\n" + "=" * 70)
    print("  深度分析报告")
    print("=" * 70)
    
    # 分年度
    print("\n📅 分年度统计:")
    print(f"  {'年份':<8} {'交易':<8} {'胜率':<10} {'期望':<12} {'累计':<12}")
    print("  " + "-" * 50)
    for year, data in sorted(deep["yearly"].items()):
        print(f"  {year:<8} {data['trades']:<8} {data['win_rate']}%{'':<5} {data['expectancy']:+.2f}%{'':<6} {data['cumulative']:+.2f}%")
    
    # 亏损分析
    print("\n❌ 亏损交易分析:")
    losing = deep["losing"]
    print(f"  亏损笔数: {losing['count']}")
    print(f"  平均亏损: {losing['avg_loss']:.2f}%")
    print(f"  最大单笔亏损: {losing['max_loss']:.2f}%")
    print(f"  平均持仓天数: {losing['avg_hold_days']:.1f}天")
    print("  按卖出原因:")
    for st, cnt in sorted(losing["by_sell_type"].items(), key=lambda x: -x[1]):
        print(f"    {st}: {cnt}笔")
    print("  按行业:")
    for ind, cnt in sorted(losing["by_industry"].items(), key=lambda x: -x[1])[:5]:
        print(f"    {ind}: {cnt}笔")
    
    # 盈利分析
    print("\n✅ 盈利交易分析:")
    winning = deep["winning"]
    print(f"  盈利笔数: {winning['count']}")
    print(f"  平均盈利: {winning['avg_win']:.2f}%")
    print(f"  最大单笔盈利: {winning['max_win']:.2f}%")
    print(f"  平均持仓天数: {winning['avg_hold_days']:.1f}天")
    
    # 持仓天数
    print("\n⏱️ 持仓天数分析:")
    print(f"  {'区间':<10} {'笔数':<8} {'胜率':<10} {'期望':<12}")
    print("  " + "-" * 40)
    for bucket, data in deep["hold_days"].items():
        print(f"  {bucket:<10} {data['count']:<8} {data['win_rate']}%{'':<5} {data['expectancy']:+.2f}%")
    
    # 卖出原因
    print("\n🎯 卖出原因分析:")
    print(f"  {'原因':<12} {'笔数':<8} {'胜率':<10} {'平均收益':<12}")
    print("  " + "-" * 45)
    for st, data in sorted(deep["sell_types"].items(), key=lambda x: -x[1]["count"]):
        print(f"  {st:<12} {data['count']:<8} {data['win_rate']}%{'':<5} {data['avg_profit']:+.2f}%")
    
    # 行业
    print("\n🏭 行业分析:")
    print(f"  {'行业':<10} {'笔数':<8} {'胜率':<10} {'期望':<12} {'累计':<12}")
    print("  " + "-" * 55)
    for ind, data in sorted(deep["industry"].items(), key=lambda x: -x[1]["cumulative"]):
        print(f"  {ind:<10} {data['count']:<8} {data['win_rate']}%{'':<5} {data['expectancy']:+.2f}%{'':<6} {data['cumulative']:+.2f}%")


def save_results(result: dict):
    """保存结果到JSON"""
    if not result:
        return
    
    output_dir = os.path.join(config.PROJECT_ROOT, "output")
    os.makedirs(output_dir, exist_ok=True)
    
    today = datetime.date.today().strftime("%Y%m%d")
    
    # 保存统计数据
    stats_path = os.path.join(output_dir, f"backtest_v6_stats_{today}.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump({
            "stats": result["stats"],
            "deep_analysis": {
                k: {kk: dict(vv) if isinstance(vv, defaultdict) else vv 
                    for kk, vv in v.items()} if isinstance(v, dict) else v
                for k, v in result["deep_analysis"].items()
            },
            "stock_results": result["stock_results"],
        }, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"统计结果: {stats_path}")
    
    # 保存交易明细
    trades_path = os.path.join(output_dir, f"backtest_v6_trades_{today}.json")
    with open(trades_path, "w", encoding="utf-8") as f:
        json.dump(result["trades"], f, ensure_ascii=False, indent=2)
    logger.info(f"交易明细: {trades_path}")


if __name__ == "__main__":
    result = run_extended_backtest()
    if result:
        print_deep_analysis(result)
        save_results(result)
