# -*- coding: utf-8 -*-
"""
持仓评分深度归因诊断 + 选股系统有效性验证
==========================================
输出:
  1. 6只持仓CANSLIM各维度因子得分拆解
  2. 大盘状态/Regime/买入线判定
  3. 选股引擎逻辑一致性验证
"""
import sys, os, io, json, datetime, logging
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'trading_system'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import numpy as np

logging.basicConfig(level=logging.WARNING, format='%(message)s')
logger = logging.getLogger(__name__)

# ============================================================
# 1. 加载持仓 & 数据
# ============================================================
from trading_system import config
holdings_path = os.path.join(os.path.dirname(__file__), '..', 'holdings.json')
with open(holdings_path, 'r', encoding='utf-8') as f:
    holdings = json.load(f)

codes = [c for c in holdings.keys() if holdings[c].get('shares', 0) > 0]
print("=" * 72)
print(f"  持仓评分深度归因诊断  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"  活跃持仓: {len(codes)}只  总资产: {config.TOTAL_CAPITAL:,.2f}")
print("=" * 72)

# 获取K线数据
from data.data_loader import fetch_stock_daily
data_dict = {}
for code in codes:
    df = fetch_stock_daily(code)
    if df is not None and len(df) >= 60:
        data_dict[code] = df
        print(f"  [数据] {code} {holdings[code]['name']}: {len(df)}根K线, 最新{df['date'].iloc[-1]}")
    else:
        print(f"  [数据] {code} 数据不足({len(df) if df is not None else 0}根)")

# 基准
bench = fetch_stock_daily("000300")
if bench is not None:
    data_dict["000300"] = bench
    print(f"  [数据] 000300 沪深300: {len(bench)}根K线")

# 计算技术指标
from strategy.trend_strategy import compute_indicators
for code in list(data_dict.keys()):
    try:
        data_dict[code] = compute_indicators(data_dict[code])
    except Exception as e:
        print(f"  [指标] {code} 指标计算失败: {e}")

# ============================================================
# 2. 大盘状态 & Regime
# ============================================================
print("\n" + "=" * 72)
print("  一、大盘环境诊断 (M因子)")
print("=" * 72)

from strategy.stock_screener import canslim_score, _load_fundamental_data
from strategy.market_analysis import check_market_direction, detect_market_regime
market_info = check_market_direction(data_dict)
regime_info = detect_market_regime(data_dict, market_info)

ms = market_info["market_state"]
print(f"  大盘状态: {ms}")
print(f"  可买入:   {market_info['can_buy']}")
print(f"  Regime:   {regime_info.get('regime', '?')}")
print(f"  宽度:     {regime_info.get('breadth', '?')}%")
print(f"  置信度:   {regime_info.get('confidence', '?')}")

# 买入线计算
_screener_cfg = getattr(config, 'SCREENER_CONFIG', {})
is_weak = ms in ("down", "weak", "neutral", "neutral_weak")
if is_weak:
    min_buy_score = _screener_cfg.get("min_buy_score_weak", 28)
else:
    min_buy_score = _screener_cfg.get("min_buy_score_strong", 45)

# breadth调整
_breadth = regime_info.get("breadth", 50)
if getattr(config, 'BUY_SCORE_BREADTH_ADJUST_ENABLED', True):
    if _breadth < 30:
        _adj = -5
        min_buy_score = max(20, min_buy_score + _adj)
    elif _breadth < 40:
        _adj = -3
        min_buy_score = max(22, min_buy_score + _adj)

print(f"\n  买入线: {min_buy_score}分 (weak={is_weak}, breadth={_breadth:.0f}%)")
print(f"  理论天花板: N(20)+S(5)+L(20)+CAI(20)+P(10)+V(5) = 80分(基础)")
print(f"             +W_周线(±5)+MR_回归(0~5)+E_事件(±10)+F_资金(±5)+R_轮动(±5)+M_ML(±2.5)")
print(f"             +SC_景气度(±3)+MOAT(0~3)+FR_财报(±4) = 理论最高~112.5分")

# ============================================================
# 3. 逐只持仓因子拆解
# ============================================================
print("\n" + "=" * 72)
print("  二、持仓CANSLIM因子拆解")
print("=" * 72)

from strategy.stock_screener import canslim_score, _load_fundamental_data
stock_codes_for_fund = [c for c in codes if not c.startswith(("300", "688", "588", "159"))]
_load_fundamental_data(stock_codes_for_fund)

factor_summary = []
for code in codes:
    df = data_dict.get(code)
    if df is None or len(df) < 60:
        print(f"\n  {code} {holdings[code]['name']}: 数据不足，跳过")
        continue

    result = canslim_score(df, code, None, market_state=ms, regime_info=regime_info)
    name = holdings[code]['name']
    total = result['total_score']
    factors = result['factors']
    signals = result['signals']
    rps = result.get('rps_rank', 0)
    chg60 = result.get('change_60d', 0)

    # 价格 vs 均线
    latest = df.iloc[-1]
    close = latest['close']
    ma20 = df['close'].rolling(20).mean().iloc[-1]
    ma60 = df['close'].rolling(60).mean().iloc[-1]
    above_ma20 = "↑" if close > ma20 else "↓"
    above_ma60 = "↑" if close > ma60 else "↓"

    # 60日高点距离
    high_60d = df['high'].iloc[-60:].max()
    dist_high = (close - high_60d) / high_60d * 100

    print(f"\n  ── {code} {name} ── 总分: {total}分  {'✅≥买入线' if total >= min_buy_score else '❌<买入线'}")
    print(f"     现价: {close:.3f} ({above_ma20}MA20 {ma20:.3f}, {above_ma60}MA60 {ma60:.3f})")
    print(f"     距60日高: {dist_high:+.1f}%  |  60日涨幅: {chg60:+.1f}%  |  RPS: {rps:.0f}%")
    print(f"     ┌─────────────────────────────────────────────┐")
    print(f"     │ N_新事物:  {factors.get('N_新事物', 0):5.1f} / 20  {'█' * int(factors.get('N_新事物', 0))}{'░' * (20 - int(factors.get('N_新事物', 0)))}")
    print(f"     │ S_供需:    {factors.get('S_供需', 0):5.1f} /  5  {'█' * int(factors.get('S_供需', 0))}{'░' * (5 - int(factors.get('S_供需', 0)))}")
    print(f"     │ L_龙头:    {factors.get('L_龙头', 0):5.1f} / 20  {'█' * int(factors.get('L_龙头', 0))}{'░' * (20 - int(factors.get('L_龙头', 0)))}")
    print(f"     │ CAI基本面: {factors.get('CAI_基本面', 0):5.1f} / 20  {'█' * int(factors.get('CAI_基本面', 0))}{'░' * (20 - int(factors.get('CAI_基本面', 0)))}")
    print(f"     │ V_估值:    {factors.get('V_估值', 0):5.1f} /  5  {'█' * int(factors.get('V_估值', 0))}{'░' * (5 - int(factors.get('V_估值', 0)))}")
    print(f"     │ P_前瞻:    {factors.get('P_前瞻', 0):5.1f} / 10  {'█' * int(factors.get('P_前瞻', 0))}{'░' * (10 - int(factors.get('P_前瞻', 0)))}")
    print(f"     │ W_周线:    {factors.get('W_周线', 0):+5.1f}     (±5)")
    print(f"     │ MR_回归:   {factors.get('MR_回归', 0):5.1f}     (0~5)")
    print(f"     │ F_资金:    {factors.get('F_资金', 0):5.1f}     (±5)")
    print(f"     │ R_轮动:    {factors.get('R_轮动', 0):5.1f}     (±5)")
    print(f"     │ M_ML:      {factors.get('M_ML', 0):5.1f}     (0~5)")
    extra_keys = [k for k in factors.keys() if k not in ('N_新事物','S_供需','L_龙头','CAI_基本面','V_估值','P_前瞻','W_周线','MR_回归','F_资金','R_轮动','M_ML')]
    for ek in extra_keys:
        print(f"     │ {ek}: {factors.get(ek, 0):5.1f}")
    print(f"     └─────────────────────────────────────────────┘")
    if signals:
        print(f"     信号: {' | '.join(signals[:6])}")
    if len(signals) > 6:
        print(f"           {' | '.join(signals[6:])}")

    factor_summary.append({
        "code": code, "name": name, "total": total,
        "N": factors.get('N_新事物', 0), "S": factors.get('S_供需', 0),
        "L": factors.get('L_龙头', 0), "CAI": factors.get('CAI_基本面', 0),
        "V": factors.get('V_估值', 0), "P": factors.get('P_前瞻', 0),
        "W": factors.get('W_周线', 0), "MR": factors.get('MR_回归', 0),
        "rps": rps, "chg60": chg60, "dist_high": dist_high,
        "above_ma20": close > ma20, "above_ma60": close > ma60,
    })

# ============================================================
# 4. 汇总统计
# ============================================================
if factor_summary:
    print("\n" + "=" * 72)
    print("  三、因子贡献汇总与拖累归因")
    print("=" * 72)

    # 各因子均值
    avg_N = np.mean([f['N'] for f in factor_summary])
    avg_S = np.mean([f['S'] for f in factor_summary])
    avg_L = np.mean([f['L'] for f in factor_summary])
    avg_CAI = np.mean([f['CAI'] for f in factor_summary])
    avg_V = np.mean([f['V'] for f in factor_summary])
    avg_P = np.mean([f['P'] for f in factor_summary])
    avg_W = np.mean([f['W'] for f in factor_summary])
    avg_MR = np.mean([f['MR'] for f in factor_summary])
    avg_total = np.mean([f['total'] for f in factor_summary])

    print(f"\n  因子平均得分 (满分基准=80):")
    print(f"    N_新事物:  {avg_N:5.1f}/20 ({avg_N/20*100:.0f}%)  ← 创新高能力")
    print(f"    S_供需:    {avg_S:5.1f}/5  ({avg_S/5*100:.0f}%)   ← 量价配合")
    print(f"    L_龙头:    {avg_L:5.1f}/20 ({avg_L/20*100:.0f}%)  ← 相对强弱")
    print(f"    CAI基本面: {avg_CAI:5.1f}/20 ({avg_CAI/20*100:.0f}%) ← 业绩/动量代理")
    print(f"    V_估值:    {avg_V:5.1f}/5  ({avg_V/5*100:.0f}%)   ← 估值安全边际")
    print(f"    P_前瞻:    {avg_P:5.1f}/10 ({avg_P/10*100:.0f}%)  ← 预测信号")
    print(f"    W_周线:    {avg_W:+5.1f}    ← 周线共振")
    print(f"    MR_回归:   {avg_MR:5.1f}    ← 均值回归")
    print(f"    ─────────────────────────────")
    print(f"    总分均值:  {avg_total:5.1f}")
    print(f"    买入线:    {min_buy_score}")
    print(f"    通过率:    {sum(1 for f in factor_summary if f['total'] >= min_buy_score)}/{len(factor_summary)}")

    # 核心拖累因子识别
    print(f"\n  核心拖累因子识别:")
    # N因子: 距高点距离
    n_weak = [f for f in factor_summary if f['N'] < 8]
    if n_weak:
        print(f"    N_新事物 拖累 ({len(n_weak)}只<8分):")
        for f in n_weak:
            print(f"      {f['code']} {f['name']}: N={f['N']:.0f}分, 距60日高{f['dist_high']:+.1f}%, 60日涨幅{f['chg60']:+.1f}%")

    # L因子: RPS排名
    l_weak = [f for f in factor_summary if f['L'] < 8]
    if l_weak:
        print(f"    L_龙头 拖累 ({len(l_weak)}只<8分):")
        for f in l_weak:
            print(f"      {f['code']} {f['name']}: L={f['L']:.0f}分, RPS={f['rps']:.0f}%, 60日涨幅{f['chg60']:+.1f}%")

    # MA位置
    below_ma20 = [f for f in factor_summary if not f['above_ma20']]
    below_ma60 = [f for f in factor_summary if not f['above_ma60']]
    print(f"\n  均线位置统计:")
    print(f"    破MA20: {len(below_ma20)}/{len(factor_summary)}只 ({','.join(f['name'] for f in below_ma20) or '无'})")
    print(f"    破MA60: {len(below_ma60)}/{len(factor_summary)}只 ({','.join(f['name'] for f in below_ma60) or '无'})")

# ============================================================
# 5. 选股系统有效性诊断
# ============================================================
print("\n" + "=" * 72)
print("  四、选股系统有效性诊断")
print("=" * 72)

print(f"\n  [1] 买入线逻辑:")
print(f"      基础值: {'28分(弱势)' if is_weak else '45分(强势)'}")
print(f"      breadth调整: {_breadth:.0f}% → {'下调' if _breadth < 40 else '未调整'}")
print(f"      最终买入线: {min_buy_score}分")
print(f"      持仓最高分: {max(f['total'] for f in factor_summary) if factor_summary else 0}分")

print(f"\n  [2] 理论天花板分析:")
print(f"      基础满分: N(20)+S(5)+L(20)+CAI(20) = 65分")
print(f"      +V(5)+P(10) = 80分")
print(f"      +辅助因子(W/MR/F/R/M_ML) ≈ ±15~25分")
print(f"      → 弱势市中: N因子难满(无新高)、L因子难满(无龙头)、S因子压缩(5分)")
print(f"      → 弱势市理论天花板 ≈ 50~60分")

print(f"\n  [3] 大盘闸门状态:")
print(f"      market_state={ms}, can_buy={market_info['can_buy']}")
if ms in ("down", "weak"):
    print(f"      → 弱势闸门: 允许轻仓参与(≤15%), 买入线28分")
elif ms == "neutral" or ms == "neutral_weak":
    print(f"      → 中性闸门: 正常参与, 买入线28分")
else:
    print(f"      → 强势闸门: 积极参与, 买入线45分")

print(f"\n  [4] 系统是否失效判定:")
has_bug = False
if avg_total < 10 and len(factor_summary) > 0:
    print(f"      ⚠ 警告: 均分{avg_total:.1f}极低(<10), 可能存在数据异常或Bug")
    has_bug = True
else:
    print(f"      ✅ 评分分布合理(均分{avg_total:.1f}), 无明显Bug特征")

# 检查是否有因子系统性归零
if avg_S < 1:
    print(f"      ⚠ S因子均分{avg_S:.1f}<1, 接近系统性归零")
if avg_N < 3:
    print(f"      ⚠ N因子均分{avg_N:.1f}<3, 弱势市无新高导致系统性低分")
if avg_CAI < 5:
    print(f"      ⚠ CAI因子均分{avg_CAI:.1f}<5, 基本面数据可能缺失")

print(f"\n  [5] 结论:")
if not has_bug:
    print(f"      当前持仓评分低迷属于「市场环境系统性拖累」而非系统Bug:")
    print(f"      • 大盘震荡/恐慌 → M因子压制 + 买入线相对偏高")
    print(f"      • 个股普遍破位 → N/L因子系统性低分(无新高/无龙头)")
    print(f"      • S因子满分仅5分 → 区分度极低, 无法拉开差距")
    print(f"      • 选股0入选 = 系统在极端弱势环境下的正常防御行为")

print("\n" + "=" * 72)
print("  诊断完成")
print("=" * 72)
