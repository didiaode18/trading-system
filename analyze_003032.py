# -*- coding: utf-8 -*-
"""单只股票全面进场可行性分析 - 调用系统所有分析模块"""
import sys, os, io, json, datetime, warnings, traceback
warnings.filterwarnings('ignore')
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "trading_system"))
sys.path.insert(0, BASE_DIR)

import numpy as np
import pandas as pd
import config
from data.realtime import fetch_realtime_batch
from data.data_loader import fetch_stock_daily_baostock, _bs_logout

CODE = "003032"
NAME = "传智教育"
SECTOR = "教育"
STOCK_TYPE = "龙头"

print(f"\n{'='*70}")
print(f"  传智教育(003032) 全面进场可行性分析")
print(f"  分析时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'='*70}")

# ============================================================
# 0. 获取数据
# ============================================================
print("\n[数据] 获取历史K线...")
df = fetch_stock_daily_baostock(CODE, start_date="2024-01-01")
if df is None or df.empty:
    print("ERROR: 无法获取历史数据"); sys.exit(1)
print(f"  K线数据: {len(df)}根 ({df['date'].iloc[0]} ~ {df['date'].iloc[-1]})")

print("[数据] 获取实时行情...")
rt = fetch_realtime_batch([CODE])
quote = rt.get(CODE, {})
rt_price = quote.get("price", 0)
rt_change = quote.get("change_pct", 0)
rt_turnover = quote.get("turnover", 0)
rt_volume_ratio = quote.get("volume_ratio", 0)
rt_amount = quote.get("amount", 0)
print(f"  实时价: {rt_price} | 涨跌: {rt_change}% | 换手率: {rt_turnover}% | 量比: {rt_volume_ratio}")

# 计算技术指标
print("[数据] 计算技术指标...")
for col in ['open','close','high','low','volume']:
    df[col] = pd.to_numeric(df[col], errors='coerce')
df['ma5'] = df['close'].rolling(5).mean()
df['ma10'] = df['close'].rolling(10).mean()
df['ma20'] = df['close'].rolling(20).mean()
df['ma60'] = df['close'].rolling(60).mean()
df['ema12'] = df['close'].ewm(span=12).mean()
df['ema26'] = df['close'].ewm(span=26).mean()
df['dif'] = df['ema12'] - df['ema26']
df['dea'] = df['dif'].ewm(span=9).mean()
df['macd'] = (df['dif'] - df['dea']) * 2
# RSI
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
df['rsi14'] = 100 - (100 / (1 + gain / loss.replace(0, 1)))
# KDJ
low9 = df['low'].rolling(9).min()
high9 = df['high'].rolling(9).max()
rsv = (df['close'] - low9) / (high9 - low9).replace(0, 1) * 100
df['k'] = rsv.ewm(alpha=1/3, adjust=False).mean()
df['d'] = df['k'].ewm(alpha=1/3, adjust=False).mean()
df['j'] = 3 * df['k'] - 2 * df['d']
# 布林带
df['boll_mid'] = df['close'].rolling(20).mean()
df['boll_std'] = df['close'].rolling(20).std()
df['boll_up'] = df['boll_mid'] + 2 * df['boll_std']
df['boll_dn'] = df['boll_mid'] - 2 * df['boll_std']
# ATR
tr = pd.concat([df['high']-df['low'], (df['high']-df['close'].shift()).abs(), (df['low']-df['close'].shift()).abs()], axis=1).max(axis=1)
df['atr14'] = tr.rolling(14).mean()
# 量比(5日)
df['vol_ma5'] = df['volume'].rolling(5).mean()

latest = df.iloc[-1]
prev = df.iloc[-2]

# ============================================================
# 1. 基本面分析
# ============================================================
print(f"\n{'─'*70}")
print("【一、基本面分析】(fundamental.py)")
print(f"{'─'*70}")
try:
    from strategy.fundamental import FundamentalAnalyzer
    fa = FundamentalAnalyzer()
    fund = fa.get_financial_indicators(CODE)
    print(f"  PE_TTM:        {fund.get('pe_ttm', 'N/A')}")
    print(f"  PB:            {fund.get('pb', 'N/A')}")
    print(f"  ROE(%):        {fund.get('roe', 'N/A')}")
    print(f"  净利润增速(%): {fund.get('net_profit_growth', 'N/A')}")
    print(f"  营收增速(%):   {fund.get('revenue_growth', 'N/A')}")
    print(f"  毛利率(%):     {fund.get('gross_margin', 'N/A')}")
    print(f"  资产负债率(%): {fund.get('debt_ratio', 'N/A')}")
    print(f"  PE历史分位:    {fund.get('pe_percentile', 'N/A')}")
    print(f"  数据日期:      {fund.get('update_date', 'N/A')}")
except Exception as e:
    fund = {}
    print(f"  [WARN] 基本面获取失败: {e}")

# ============================================================
# 2. 技术面分析 (caopan_signal.py)
# ============================================================
print(f"\n{'─'*70}")
print("【二、技术面分析】(caopan_signal.py)")
print(f"{'─'*70}")
try:
    from strategy.caopan_signal import CaopanEngine
    engine = CaopanEngine()
    caopan = engine.analyze(df, code=CODE, name=NAME)
    print(f"  DK信号:       {caopan.get('dk_signal', 'N/A')}")
    print(f"  趋势方向:     {caopan.get('trend_direction', 'N/A')}")
    print(f"  趋势强度:     {caopan.get('trend_strength', 'N/A')}")
    print(f"  MACD顶背离:   {caopan.get('top_divergence', False)}")
    print(f"  MACD底背离:   {caopan.get('bottom_divergence', False)}")
    print(f"  资金流模式:   {caopan.get('fund_pattern', 'N/A')}")
    print(f"  综合评分:     {caopan.get('composite_score', 'N/A')}")
    print(f"  控盘度:       {caopan.get('control_degree', 'N/A')}")
    signals_cp = caopan.get('signals', [])
    if signals_cp:
        print(f"  信号列表:     {signals_cp[:5]}")
except Exception as e:
    caopan = {}
    print(f"  [WARN] 操盘信号分析失败: {e}")
    traceback.print_exc()

# 手动技术指标展示
print(f"\n  --- 手动技术指标 (截至 {latest['date']}) ---")
print(f"  收盘价:  {latest['close']:.3f}")
print(f"  MA5:     {latest['ma5']:.3f} | MA10: {latest['ma10']:.3f} | MA20: {latest['ma20']:.3f} | MA60: {latest['ma60']:.3f}")
ma_state = "多头排列" if latest['ma5'] > latest['ma10'] > latest['ma20'] > latest['ma60'] else \
           "空头排列" if latest['ma5'] < latest['ma10'] < latest['ma20'] < latest['ma60'] else "交叉/震荡"
print(f"  均线状态: {ma_state}")
print(f"  MACD:    DIF={latest['dif']:.4f} | DEA={latest['dea']:.4f} | 柱={latest['macd']:.4f}")
macd_cross = "金叉" if latest['dif'] > latest['dea'] and prev['dif'] <= prev['dea'] else \
             "死叉" if latest['dif'] < latest['dea'] and prev['dif'] >= prev['dea'] else \
             "DIF>DEA(多)" if latest['dif'] > latest['dea'] else "DIF<DEA(空)"
print(f"  MACD状态: {macd_cross}")
print(f"  KDJ:     K={latest['k']:.1f} | D={latest['d']:.1f} | J={latest['j']:.1f}", end="")
if latest['j'] > 100: print(" ← 超买!")
elif latest['j'] < 0: print(" ← 超卖!")
else: print(" (正常区间)")
print(f"  RSI14:   {latest['rsi14']:.1f}", end="")
if latest['rsi14'] > 70: print(" ← 超买区")
elif latest['rsi14'] < 30: print(" ← 超卖区")
else: print(" (中性)")
boll_pos = (latest['close'] - latest['boll_dn']) / (latest['boll_up'] - latest['boll_dn']) * 100 if (latest['boll_up'] - latest['boll_dn']) > 0 else 50
print(f"  布林带:  上={latest['boll_up']:.3f} | 中={latest['boll_mid']:.3f} | 下={latest['boll_dn']:.3f} | 位置={boll_pos:.0f}%")

# ============================================================
# 3. 量价指标
# ============================================================
print(f"\n{'─'*70}")
print("【三、量价指标】(realtime.py)")
print(f"{'─'*70}")
print(f"  实时量比:     {rt_volume_ratio}")
print(f"  换手率:       {rt_turnover}%", end="")
if rt_turnover and 3 <= rt_turnover <= 10: print(" ✓ 健康区间(3-10%)")
elif rt_turnover and rt_turnover > 15: print(" ⚠ 过度换手(>15%)")
elif rt_turnover and rt_turnover < 3: print(" (偏低<3%)")
else: print("")
print(f"  成交额:       {rt_amount/10000:.0f}万" if rt_amount else "  成交额: N/A")
# 近5日量能趋势
vol_5 = df['volume'].iloc[-5:].values
vol_trend = "递增" if all(vol_5[i] <= vol_5[i+1] for i in range(4)) else \
            "递减" if all(vol_5[i] >= vol_5[i+1] for i in range(4)) else "波动"
print(f"  近5日量能:    {vol_trend} | 日均量={np.mean(vol_5):.0f}")
print(f"  最新成交量:   {latest['volume']:.0f} | 5日均量: {latest['vol_ma5']:.0f} | 量比(日): {latest['volume']/latest['vol_ma5']:.2f}" if latest['vol_ma5'] > 0 else "")

# ============================================================
# 4. 资金面分析
# ============================================================
print(f"\n{'─'*70}")
print("【四、资金面分析】(capital_flow.py)")
print(f"{'─'*70}")
try:
    from strategy.capital_flow import CapitalFlowAnalyzer
    cfa = CapitalFlowAnalyzer()
    flow_data = cfa.analyze_flow_combined(CODE, days=10)
    if isinstance(flow_data, tuple):
        flow_summary, flow_detail = flow_data[0], flow_data[1] if len(flow_data) > 1 else {}
    else:
        flow_summary = flow_data
        flow_detail = {}
    if isinstance(flow_summary, dict):
        print(f"  主力净流入(今日): {flow_summary.get('main_net_inflow', flow_summary.get('net_inflow', 'N/A'))}")
        print(f"  资金流方向:       {flow_summary.get('direction', flow_summary.get('flow_direction', 'N/A'))}")
        for k, v in list(flow_summary.items())[:8]:
            if k not in ('main_net_inflow', 'net_inflow', 'direction', 'flow_direction'):
                print(f"    {k}: {v}")
    else:
        print(f"  资金流数据: {str(flow_summary)[:200]}")
except Exception as e:
    print(f"  [WARN] 资金面分析失败: {e}")

# 资金流模式识别
try:
    pattern = cfa.detect_flow_pattern(CODE, days=10)
    if pattern:
        print(f"  资金流模式:   {pattern.get('pattern', pattern.get('type', 'N/A'))}")
        print(f"  模式置信度:   {pattern.get('confidence', 'N/A')}")
        print(f"  模式描述:     {pattern.get('description', pattern.get('desc', ''))}")
except Exception as e:
    print(f"  [WARN] 模式识别失败: {e}")

# ============================================================
# 5. 趋势与动量
# ============================================================
print(f"\n{'─'*70}")
print("【五、趋势与动量】(trend_forecast.py)")
print(f"{'─'*70}")
try:
    from strategy.trend_forecast import TrendForecaster
    tf = TrendForecaster()
    trend = tf.analyze_stock(CODE, df)
    if trend.get("valid"):
        print(f"  趋势方向:     {trend.get('trend_direction', trend.get('direction', 'N/A'))}")
        print(f"  趋势级别:     {trend.get('trend_level', trend.get('level', 'N/A'))}")
        print(f"  综合评分:     {trend.get('composite_score', trend.get('score', 'N/A'))}")
        print(f"  置信度:       {trend.get('confidence', 'N/A')}")
        print(f"  预测方向:     {trend.get('predicted_direction', trend.get('forecast', 'N/A'))}")
        # 支撑压力
        sr = trend.get("support_resistance", trend.get("levels", {}))
        if sr:
            print(f"  支撑位:       {sr.get('support', sr.get('support_1', 'N/A'))}")
            print(f"  压力位:       {sr.get('resistance', sr.get('resistance_1', 'N/A'))}")
        # 波动率
        vol_info = trend.get("volatility", {})
        if vol_info:
            print(f"  波动率:       {vol_info.get('atr', vol_info.get('volatility', 'N/A'))}")
    else:
        print(f"  趋势分析无效: {trend.get('error', 'unknown')}")
except Exception as e:
    trend = {}
    print(f"  [WARN] 趋势预测失败: {e}")
    traceback.print_exc()

# RPS相对强度(60日涨幅排名)
try:
    ret_60d = (latest['close'] - df['close'].iloc[-60]) / df['close'].iloc[-60] * 100 if len(df) >= 60 else 0
    ret_20d = (latest['close'] - df['close'].iloc[-20]) / df['close'].iloc[-20] * 100 if len(df) >= 20 else 0
    print(f"  60日涨幅:     {ret_60d:.1f}%")
    print(f"  20日涨幅:     {ret_20d:.1f}%")
except: pass

# ============================================================
# 6. CANSLIM五因子评分
# ============================================================
print(f"\n{'─'*70}")
print("【六、CANSLIM五因子评分】(stock_screener.py)")
print(f"{'─'*70}")
try:
    from strategy.stock_screener import canslim_score
    canslim = canslim_score(df, CODE, market_state="neutral")
    print(f"  总分:         {canslim.get('total_score', 0)}/100")
    factors = canslim.get('factors', {})
    for k, v in factors.items():
        print(f"    {k}: {v}")
    cs_signals = canslim.get('signals', [])
    if cs_signals:
        print(f"  信号:         {cs_signals}")
except Exception as e:
    canslim = {}
    print(f"  [WARN] CANSLIM评分失败: {e}")
    traceback.print_exc()

# ============================================================
# 7. 五层选股模型
# ============================================================
print(f"\n{'─'*70}")
print("【七、五层选股模型】(recommend_engine.py)")
print(f"{'─'*70}")
try:
    from strategy.recommend_engine import generate_trading_plan
    plan_result = generate_trading_plan(
        code=CODE, name=NAME, sector=SECTOR, stock_type=STOCK_TYPE,
        df=df, realtime_price=rt_price, realtime_change=rt_change,
        fund_data=fund if fund else None
    )
    print(f"  通过筛选:     {'是' if plan_result.get('pass') else '否'}")
    print(f"  推荐等级:     {plan_result.get('grade', 'N/A')}")
    print(f"  综合评分:     {plan_result.get('total_score', 0)}")
    print(f"  趋势方向:     {plan_result.get('trend_dir', 'N/A')}")
    
    layers = plan_result.get('layers', {})
    for lname, ldata in layers.items():
        if isinstance(ldata, dict):
            score = ldata.get('score', 'N/A')
            passed = ldata.get('pass', '')
            extra = ""
            if lname == "L5_买点":
                extra = f" | 盈亏比={ldata.get('risk_reward', 'N/A')}"
                extra += f" | 买点={ldata.get('entry_price', ldata.get('buy_price', 'N/A'))}"
                extra += f" | 止损={ldata.get('stop_loss', 'N/A')}"
                extra += f" | 目标={ldata.get('target', ldata.get('target_1', 'N/A'))}"
            print(f"    {lname}: 得分={score}{extra}")
    
    reasons = plan_result.get('reasons', [])
    if reasons:
        print(f"  推荐理由:     {reasons}")
    signals_re = plan_result.get('signals', [])
    if signals_re:
        print(f"  技术信号:     {signals_re}")
    
    # 交易计划详情
    plan_detail = plan_result.get('plan')
    if plan_detail and isinstance(plan_detail, dict):
        print(f"\n  --- 交易计划 ---")
        print(f"  买入区间:     {plan_detail.get('entry_price', plan_detail.get('buy_range', 'N/A'))}")
        print(f"  止损位:       {plan_detail.get('stop_loss', 'N/A')}")
        print(f"  目标位1:      {plan_detail.get('target_1', plan_detail.get('target', 'N/A'))}")
        print(f"  目标位2:      {plan_detail.get('target_2', 'N/A')}")
        print(f"  盈亏比:       {plan_detail.get('risk_reward', 'N/A')}")
        print(f"  买点类型:     {plan_detail.get('entry_type', 'N/A')}")
except Exception as e:
    plan_result = {}
    print(f"  [WARN] 五层模型分析失败: {e}")
    traceback.print_exc()

# ============================================================
# 8. 综合结论
# ============================================================
print(f"\n{'='*70}")
print("【综合评定结论】")
print(f"{'='*70}")

# 计算仓位建议(基于总资金681973, 2%风险)
total_capital = 681973.04
risk_pct = 0.02
risk_amount = total_capital * risk_pct
if plan_result and plan_result.get('plan'):
    pd_ = plan_result['plan']
    entry = pd_.get('entry_price', rt_price)
    sl = pd_.get('stop_loss', 0)
    if isinstance(entry, (list, tuple)):
        entry = entry[0]
    if entry and sl and entry > sl:
        risk_per_share = entry - sl
        max_shares = int(risk_amount / risk_per_share / 100) * 100  # 100股整数
        position_value = max_shares * entry
        position_pct = position_value / total_capital * 100
        print(f"  风险预算:     {risk_amount:.0f}元 (总资金{total_capital:.0f}×2%)")
        print(f"  每股风险:     {risk_per_share:.3f}元")
        print(f"  建议仓位:     {max_shares}股 ({position_value:.0f}元, 占比{position_pct:.1f}%)")

print(f"\n  数据获取时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'='*70}")

# 清理
try:
    _bs_logout()
except: pass
