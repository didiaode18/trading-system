"""
V6.0 改进版 vs V5.2 基线版 选股对比测试
========================================
对比维度:
1. 选股数量变化（基线0只 vs 改进版N只）
2. 因子评分分布差异
3. 各筛选环节的通过率变化
4. 信号质量评估

使用方法:
    python scripts/compare_screener_versions.py
"""
import sys
import os
import json
import datetime
import traceback

# 路径设置
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'trading_system'))

# 编码设置
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np


def load_test_data():
    """加载测试数据（使用最近可用的数据库数据）"""
    from data.data_loader import init_db, load_daily_data, get_all_candidate_codes
    from strategy.trend_strategy import compute_indicators
    
    conn = init_db()
    data_dict = {}
    
    # 加载所有候选股数据
    all_codes = get_all_candidate_codes()
    print(f"[数据加载] 候选池共 {len(all_codes)} 只")
    
    loaded = 0
    failed = 0
    for code in all_codes:
        try:
            df = load_daily_data(code, conn=conn, days=120)
            if df is not None and len(df) >= 60:
                # 计算技术指标（MA20/MA60等）
                df = compute_indicators(df)
                data_dict[code] = df
                loaded += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    
    print(f"[数据加载] 成功: {loaded}只, 失败: {failed}只")
    return data_dict


def simulate_baseline_filter(df, code, market_state):
    """模拟基线版(V5.2)硬性筛选逻辑（用于对比）"""
    if len(df) < 60:
        return False, "数据不足"
    
    latest = df.iloc[-1]
    close = latest["close"]
    ma20 = latest.get("ma20", None)
    ma20_slope = latest.get("ma20_slope", None)
    
    if pd.isna(ma20) or pd.isna(ma20_slope):
        return False, "均线数据不足"
    
    # 基线版深跌防护（固定12%）
    if len(df) >= 20:
        high_20d = df["high"].iloc[-20:].max()
        drawdown = close / high_20d - 1 if high_20d > 0 else 0
        if drawdown < -0.12:  # 基线版固定12%
            return False, f"深跌防护(回撤{drawdown:.1%})"
    
    # 基线版60日跌幅（固定20%）
    if len(df) >= 61:
        chg_60d = close / df["close"].iloc[-61] - 1
        if chg_60d < -0.20:
            return False, f"60日跌幅({chg_60d:.1%})"
    
    is_weak = market_state in ("down", "weak", "neutral", "neutral_weak")
    
    if is_weak:
        # 基线版弱势评分门槛=40
        weak_score = 0
        if close > ma20:
            weak_score += 30
        elif (close - ma20) / ma20 > -0.05:
            weak_score += 20
        elif (close - ma20) / ma20 > -0.10:
            weak_score += 10
        
        if len(df) >= 25:
            ma20_slope_prev = df["ma20"].diff(3).iloc[-4] if not pd.isna(df["ma20"].diff(3).iloc[-4]) else 0
            if ma20_slope > ma20_slope_prev:
                weak_score += 20
        
        if ma20_slope > 0:
            weak_score += 15
        
        if len(df) >= 6:
            recent_5_low = df["low"].iloc[-5:].min()
            prev_5_low = df["low"].iloc[-10:-5].min() if len(df) >= 10 else recent_5_low
            if recent_5_low >= prev_5_low * 0.98:
                weak_score += 20
        
        if len(df) >= 6:
            change_5d = (close / df["close"].iloc[-6] - 1) * 100
            if change_5d > 0:
                weak_score += 15
            elif change_5d > -3:
                weak_score += 8
        
        if weak_score < 40:  # 基线版门槛40
            return False, f"弱势评分不足({weak_score}分<40)"
    
    return True, "通过"


def simulate_improved_filter(df, code, market_state):
    """模拟改进版(V6.0)硬性筛选逻辑"""
    if len(df) < 60:
        return False, "数据不足"
    
    latest = df.iloc[-1]
    close = latest["close"]
    ma20 = latest.get("ma20", None)
    ma20_slope = latest.get("ma20_slope", None)
    
    if pd.isna(ma20) or pd.isna(ma20_slope):
        return False, "均线数据不足"
    
    is_weak = market_state in ("down", "weak", "neutral", "neutral_weak")
    
    # V6.0 P0-3: 弱势市场自适应深跌阈值
    max_dd = -0.18 if is_weak else -0.12
    if len(df) >= 20:
        high_20d = df["high"].iloc[-20:].max()
        drawdown = close / high_20d - 1 if high_20d > 0 else 0
        if drawdown < max_dd:
            return False, f"深跌防护(回撤{drawdown:.1%},阈值{max_dd:.0%})"
    
    # V6.0 P0-3: 弱势市场60日跌幅自适应
    max_60d = -0.28 if is_weak else -0.20
    if len(df) >= 61:
        chg_60d = close / df["close"].iloc[-61] - 1
        if chg_60d < max_60d:
            return False, f"60日跌幅({chg_60d:.1%},阈值{max_60d:.0%})"
    
    if is_weak:
        # V6.0: 弱势评分门槛=30
        weak_score = 0
        if close > ma20:
            weak_score += 30
        elif (close - ma20) / ma20 > -0.05:
            weak_score += 20
        elif (close - ma20) / ma20 > -0.10:
            weak_score += 10
        
        if len(df) >= 25:
            ma20_slope_prev = df["ma20"].diff(3).iloc[-4] if not pd.isna(df["ma20"].diff(3).iloc[-4]) else 0
            if ma20_slope > ma20_slope_prev:
                weak_score += 20
        
        if ma20_slope > 0:
            weak_score += 15
        
        if len(df) >= 6:
            recent_5_low = df["low"].iloc[-5:].min()
            prev_5_low = df["low"].iloc[-10:-5].min() if len(df) >= 10 else recent_5_low
            if recent_5_low >= prev_5_low * 0.98:
                weak_score += 20
        
        if len(df) >= 6:
            change_5d = (close / df["close"].iloc[-6] - 1) * 100
            if change_5d > 0:
                weak_score += 15
            elif change_5d > -3:
                weak_score += 8
        
        if weak_score < 30:  # V6.0: 门槛30
            return False, f"弱势评分不足({weak_score}分<30)"
    
    return True, "通过"


def run_comparison():
    """运行对比测试"""
    print("=" * 80)
    print("V6.0 改进版 vs V5.2 基线版 选股对比测试")
    print(f"测试时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    # 加载数据
    print("\n[Step 1] 加载测试数据...")
    data_dict = load_test_data()
    
    if not data_dict:
        print("错误: 无法加载任何数据")
        return
    
    # 模拟大盘状态（使用沪深300判断）
    print("\n[Step 2] 判断大盘状态...")
    from strategy.stock_screener import check_market_direction, detect_market_regime
    market_info = check_market_direction(data_dict)
    regime_info = detect_market_regime(data_dict, market_info)
    market_state = market_info["market_state"]
    breadth = regime_info.get("breadth", 50)
    
    print(f"  大盘状态: {market_state}")
    print(f"  市场宽度: {breadth:.1f}%")
    print(f"  Regime: {regime_info.get('regime', 'unknown')}")
    print(f"  can_buy: {market_info.get('can_buy', False)}")
    print(f"  仓位限制: {market_info.get('position_limit_ratio', 0):.0%}")
    
    # 对比筛选通过率
    print("\n[Step 3] 对比硬性筛选通过率...")
    baseline_pass = []
    improved_pass = []
    baseline_fail_reasons = {}
    improved_fail_reasons = {}
    
    for code, df in data_dict.items():
        if code == "000300":
            continue
        
        # 基线版
        b_pass, b_reason = simulate_baseline_filter(df, code, market_state)
        if b_pass:
            baseline_pass.append(code)
        else:
            baseline_fail_reasons[b_reason] = baseline_fail_reasons.get(b_reason, 0) + 1
        
        # 改进版
        i_pass, i_reason = simulate_improved_filter(df, code, market_state)
        if i_pass:
            improved_pass.append(code)
        else:
            improved_fail_reasons[i_reason] = improved_fail_reasons.get(i_reason, 0) + 1
    
    total = len(data_dict) - 1  # 排除000300
    print(f"\n  总候选: {total}只")
    print(f"  基线版通过: {len(baseline_pass)}只 ({len(baseline_pass)/total*100:.1f}%)")
    print(f"  改进版通过: {len(improved_pass)}只 ({len(improved_pass)/total*100:.1f}%)")
    print(f"  增量通过: {len(improved_pass) - len(baseline_pass)}只")
    
    print("\n  基线版淘汰原因Top5:")
    for reason, count in sorted(baseline_fail_reasons.items(), key=lambda x: -x[1])[:5]:
        print(f"    {reason}: {count}只")
    
    print("\n  改进版淘汰原因Top5:")
    for reason, count in sorted(improved_fail_reasons.items(), key=lambda x: -x[1])[:5]:
        print(f"    {reason}: {count}只")
    
    # 运行完整选股引擎（改进版）
    print("\n[Step 4] 运行完整选股引擎（改进版V6.0）...")
    try:
        from strategy.stock_screener import run_stock_screener
        result = run_stock_screener(data_dict, holdings=None, min_score=None, max_stocks=None, news_risk=None)
        
        stock_pool = result.get("stock_pool", [])
        buy_count = result.get("buy_recommend_count", 0)
        watch_count = result.get("watch_only_count", 0)
        min_buy_score = result.get("min_buy_score", 0)
        
        print(f"\n  改进版输出: {len(stock_pool)}只")
        print(f"  推荐买入: {buy_count}只")
        print(f"  仅观察: {watch_count}只")
        print(f"  买入线: {min_buy_score}分")
        
        # 评分分布
        if stock_pool:
            scores = [s.get("factor_score", 0) for s in stock_pool]
            print(f"\n  评分分布:")
            print(f"    最高分: {max(scores):.1f}")
            print(f"    最低分: {min(scores):.1f}")
            print(f"    平均分: {sum(scores)/len(scores):.1f}")
            print(f"    中位数: {sorted(scores)[len(scores)//2]:.1f}")
            
            # 因子分布
            print(f"\n  因子平均分:")
            factor_sums = {}
            factor_counts = {}
            for s in stock_pool:
                for k, v in s.get("factor_detail", {}).items():
                    factor_sums[k] = factor_sums.get(k, 0) + v
                    factor_counts[k] = factor_counts.get(k, 0) + 1
            for k in sorted(factor_sums.keys()):
                avg = factor_sums[k] / factor_counts[k] if factor_counts[k] > 0 else 0
                print(f"    {k}: {avg:.1f}分")
        
        # 推荐买入详情
        if buy_count > 0:
            print(f"\n  推荐买入标的:")
            for s in stock_pool:
                if s.get("is_buy_recommend"):
                    print(f"    {s['code']} {s['name']} [{s.get('sector', '')}] "
                          f"评分{s['factor_score']}分")
    
    except Exception as e:
        print(f"  运行选股引擎失败: {e}")
        traceback.print_exc()
        result = None
    
    # 对比总结
    print("\n" + "=" * 80)
    print("对比总结")
    print("=" * 80)
    print(f"  大盘状态: {market_state} (breadth={breadth:.1f}%)")
    print(f"  基线版(V5.2): 硬筛通过{len(baseline_pass)}只, 最终输出0只(买入线35分不可达)")
    if result:
        print(f"  改进版(V6.0): 硬筛通过{len(improved_pass)}只, 最终输出{len(stock_pool)}只(买入线{min_buy_score}分)")
    print(f"\n  关键改进:")
    print(f"    P0-1 M因子柔性化: down状态允许轻仓15%（原完全禁止）")
    print(f"    P0-2 买入线下调: 弱势市35→28分 + breadth动态调整")
    print(f"    P0-3 深跌防护自适应: 弱势市12%→18%")
    print(f"    P1-1 CAI多代理: 固定10分→动量+波动+换手综合(2-18分)")
    print(f"    P1-2 估值因子V: 新增PE/PB百分位反向打分(0-5分)")
    print(f"    P1-3 IC_IR加权: IC/IC_std替代固定权重")
    print(f"    P2-1 因子正交化: 高相关因子自动降权")
    print("=" * 80)


if __name__ == "__main__":
    run_comparison()
