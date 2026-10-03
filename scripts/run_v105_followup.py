"""
运行 PIT 全市场基础数据采集 + PIT回测 + Alpha归因
====================================================
V10.5 后续操作一键执行脚本
"""
import os
import sys
import json
import logging
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'trading_system'))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("run_v105_followup")


def step1_fetch_stock_basic():
    """步骤1: 采集全市场基础信息到 stock_basic 表"""
    print("\n" + "=" * 60)
    print("  步骤1: 采集全市场基础信息 (stock_basic)")
    print("=" * 60)

    from data.pit_universe import PITUniverse
    pit = PITUniverse()

    # 先检查当前状态
    count_before = pit.get_stock_count()
    print(f"  当前 stock_basic 记录数: {count_before}")

    if count_before > 100:
        print(f"  已有 {count_before} 条记录，跳过采集（如需刷新请手动执行）")
        pit.close()
        return True

    # 执行采集
    count = pit.fetch_all_stock_basic()
    print(f"  采集完成: 写入 {count} 条记录")

    # 验证
    count_after = pit.get_stock_count()
    print(f"  验证: stock_basic 共 {count_after} 只股票")

    # 抽样检查
    sample_codes = ["600519", "000001", "002594", "300750", "600036"]
    for code in sample_codes:
        info = pit.get_stock_info(code)
        if info:
            print(f"    {code} {info.get('name','')}: "
                  f"IPO={info.get('ipo_date','')}, "
                  f"板块={info.get('board','')}, "
                  f"行业={info.get('industry_sw','')}")

    pit.close()
    return count > 0


def step2_pit_backtest():
    """步骤2: 运行 PIT 动态选股回测"""
    print("\n" + "=" * 60)
    print("  步骤2: PIT 全市场动态选股回测")
    print("=" * 60)

    from strategy.dynamic_selector import run_pit_backtest_demo

    result = run_pit_backtest_demo(
        start_date="2024-01-01",
        end_date="2026-09-30",
        rebalance_freq_days=5,
        选股数量=10
    )

    if result.get("执行状态") == "成功":
        r = result.get("结果", {})
        perf = r.get("绩效", {})
        print(f"\n  === PIT 回测结果 ===")
        print(f"  回测名称: {r.get('回测名称', '')}")
        print(f"  区间: {r.get('区间', {})}")
        print(f"  宇宙口径: {r.get('宇宙口径', '')}")
        print(f"  累计收益: {perf.get('累计收益百分比', 0):+.2f}%")
        print(f"  年化收益: {perf.get('年化收益百分比', 0):+.2f}%")
        print(f"  年化夏普: {perf.get('年化夏普', 0):.2f}")
        print(f"  最大回撤: -{perf.get('最大回撤百分比', 0):.2f}%")
        print(f"  Calmar:   {perf.get('Calmar', 0):.2f}")
        print(f"  胜率:     {perf.get('胜率百分比', 0):.1f}%")
        print(f"  累计成本: {perf.get('累计成本元', 0):,.0f}元")
        print(f"  数据缺口: {r.get('数据缺口声明', [])}")

        # 保存结果
        output_path = os.path.join(
            os.path.dirname(__file__), '..', 'output',
            f"pit_backtest_{datetime.date.today().strftime('%Y%m%d')}.json"
        )
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        # 移除每日净值（太大）以节省空间
        result_save = json.loads(json.dumps(result, default=str))
        if "结果" in result_save and "每日净值" in result_save["结果"]:
            result_save["结果"]["每日净值"] = result_save["结果"]["每日净值"][:5] + ["..."] + result_save["结果"]["每日净值"][-5:]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result_save, f, ensure_ascii=False, indent=2)
        print(f"\n  结果已保存: {output_path}")
    else:
        print(f"\n  PIT 回测未成功: {result.get('已知限制', [])}")
        print("  提示: 需先完成步骤1采集全市场数据")

    return result


def step3_alpha_purity():
    """步骤3: 运行 Alpha 纯度归因"""
    print("\n" + "=" * 60)
    print("  步骤3: Alpha 纯度归因")
    print("=" * 60)

    from attribution.style_factors import build_style_factor_returns
    from attribution.alpha_purity import analyze_alpha_purity
    from data.data_loader import init_db
    import pandas as pd
    import sqlite3

    conn = init_db()

    # 1. 加载沪深300基准 + 部分股票数据
    bench_df = pd.read_sql_query("""
        SELECT date, open, close, high, low, volume, amount
        FROM daily_kline WHERE code='000300' AND date>='2024-01-01' AND date<='2026-09-30'
        ORDER BY date
    """, conn)

    if bench_df.empty:
        print("  沪深300数据缺失，跳过Alpha归因")
        conn.close()
        return None

    data_dict = {"000300": bench_df}

    # 加载部分股票数据（取前50只流动性好的）
    codes_df = pd.read_sql_query("""
        SELECT code, AVG(amount) as avg_amt FROM daily_kline
        WHERE date>='2025-01-01' AND date<='2026-09-30'
        GROUP BY code ORDER BY avg_amt DESC LIMIT 50
    """, conn)

    for _, row in codes_df.iterrows():
        code = row["code"]
        df = pd.read_sql_query("""
            SELECT date, open, close, high, low, volume, amount
            FROM daily_kline WHERE code=? AND date>='2024-01-01' AND date<='2026-09-30'
            ORDER BY date
        """, conn, params=(code,))
        if len(df) >= 60:
            data_dict[code] = df

    conn.close()
    print(f"  数据加载: {len(data_dict)}只股票")

    # 2. 构造风格因子
    factor_returns = build_style_factor_returns(data_dict, "2024-01-01", "2026-09-30")
    print(f"  风格因子: {list(factor_returns.keys())}")

    # 3. 构造一个模拟组合净值（用等权持有前10只的简化收益）
    # 真实应用中应来自 PIT 回测的每日净值
    stock_codes = [c for c in data_dict.keys() if c != "000300"][:10]
    daily_dates = bench_df["date"].tolist()

    # 计算等权组合日收益
    portfolio_daily = []
    total_value = 1_000_000.0
    for date in daily_dates:
        day_ret = 0
        count = 0
        for code in stock_codes:
            df = data_dict[code]
            idx = df[df["date"] <= date]
            if len(idx) >= 2:
                ret = (idx.iloc[-1]["close"] - idx.iloc[-2]["close"]) / idx.iloc[-2]["close"]
                day_ret += ret
                count += 1
        if count > 0:
            total_value *= (1 + day_ret / count)
        portfolio_daily.append({"日期": date, "总市值": round(total_value, 2)})

    # 4. 运行 Alpha 归因
    result = analyze_alpha_purity(
        组合每日净值=portfolio_daily,
        因子日收益=factor_returns,
        无风险日收益=None,
        使用NeweyWest=True
    )

    if result.get("执行状态") == "成功":
        r = result.get("结果", {})
        reg = r.get("回归结果", {})
        decomp = r.get("收益分解", {})
        print(f"\n  === Alpha 纯度归因结果 ===")
        print(f"  归因区间: {r.get('归因区间', {})}")
        print(f"  年化Alpha: {reg.get('年化Alpha百分比', 0):+.2f}%")
        print(f"  Alpha t值: {reg.get('Alpha的t值', 0):.2f}")
        print(f"  Alpha显著: {'是' if reg.get('Alpha是否显著') else '否'}")
        print(f"  R方:       {reg.get('R方', 0):.4f}")
        print(f"\n  收益分解:")
        print(f"    市场贡献: {decomp.get('市场贡献百分比', 0):+.2f}%")
        print(f"    风格贡献: {decomp.get('风格贡献百分比', 0):+.2f}%")
        print(f"    残差Alpha: {decomp.get('残差Alpha百分比', 0):+.2f}%")
        print(f"    合计年化:  {decomp.get('合计年化收益百分比', 0):+.2f}%")
        print(f"\n  因子暴露:")
        for fe in r.get("因子暴露与贡献", []):
            print(f"    {fe['因子']}: β={fe['Beta']:.4f}, "
                  f"年化贡献={fe['年化贡献百分比']:+.2f}%, t={fe['t值']:.2f}")
        print(f"\n  结论: {r.get('结论', '')}")
        print(f"  口径声明: {r.get('口径声明', [])}")

        # 保存结果
        output_path = os.path.join(
            os.path.dirname(__file__), '..', 'output',
            f"alpha_purity_{datetime.date.today().strftime('%Y%m%d')}.json"
        )
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(json.loads(json.dumps(result, default=str)), f, ensure_ascii=False, indent=2)
        print(f"\n  结果已保存: {output_path}")
    else:
        print(f"\n  Alpha归因未成功: {result.get('已知限制', [])}")

    return result


if __name__ == "__main__":
    print("=" * 60)
    print("  V10.5 后续操作执行脚本")
    print(f"  时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 步骤1: 采集全市场数据
    step1_ok = step1_fetch_stock_basic()

    # 步骤2: PIT 回测
    step2_result = step2_pit_backtest()

    # 步骤3: Alpha 归因
    step3_result = step3_alpha_purity()

    print("\n" + "=" * 60)
    print("  全部后续操作执行完毕")
    print("=" * 60)
