# -*- coding: utf-8 -*-
"""
买点提醒 + 盘中预警 历史回测验证
=================================
基于数据库2023-08~2026-08近三年日线数据，验证:
  1. 买点提醒信号准确性（命中率/假信号率/前瞻收益）
  2. 盘中预警有效性（梯度减仓/急跌/止盈/振幅）

局限性说明:
  - 日线数据无法完整模拟分时级别盘中预警（如5分钟急跌、VWAP、盘口）
  - 买点三档价位需从选股报告生成，此处用MA20/ATR近似重构
  - 大盘闸门用上证指数实际涨跌判定
"""

import sys, os, sqlite3, warnings, datetime, logging, json
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_system'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("alert_bt")

START_DATE = "2023-08-01"
END_DATE = "2026-08-13"
BENCHMARK = "000300"  # 沪深300

# ============================================================
# 一、数据加载与指标预计算
# ============================================================

def load_data():
    conn = sqlite3.connect(config.DB_PATH)
    df = pd.read_sql(
        "SELECT code,date,open,close,high,low,volume FROM daily_kline "
        "WHERE date>=? ORDER BY code,date", conn, params=(START_DATE,))
    conn.close()
    for c in ['open','close','high','low','volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.dropna(subset=['close'])

def compute_indicators(df):
    """向量化指标计算"""
    df = df.copy()
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma60'] = df['close'].rolling(60).mean()
    df['vol_ma20'] = df['volume'].rolling(20).mean()
    df['ma20_slope'] = df['ma20'].diff(3)
    df['pct_change'] = df['close'].pct_change()
    # ATR14
    h, l, pc = df['high'].values, df['low'].values, np.roll(df['close'].values, 1)
    pc[0] = df['close'].values[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    df['atr14'] = pd.Series(tr, index=df.index).rolling(14).mean()
    # MACD
    e12 = df['close'].ewm(span=12, adjust=False).mean()
    e26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd_dif'] = e12 - e26
    df['macd_dea'] = df['macd_dif'].ewm(span=9, adjust=False).mean()
    # RSI
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta).where(delta < 0, 0.0).rolling(14).mean()
    df['rsi'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    return df

def compute_market_regime(benchmark_df):
    """大盘市场环境: BULL/BEAR/RANGE"""
    bm = benchmark_df.copy()
    bm['ma20'] = bm['close'].rolling(20).mean()
    bm['ma60'] = bm['close'].rolling(60).mean()
    regime = {}
    for _, r in bm.iterrows():
        d = str(r['date'])
        if pd.isna(r['ma20']) or pd.isna(r['ma60']):
            regime[d] = "RANGE"
        elif r['close'] > r['ma20'] and r['ma20'] > r['ma60']:
            regime[d] = "BULL"
        elif r['close'] < r['ma60'] and r['ma20'] < r['ma60']:
            regime[d] = "BEAR"
        else:
            regime[d] = "RANGE"
    return regime

# ============================================================
# 二、买点提醒回测（模拟V5信号生成 + 三档买点触发）
# ============================================================

def simulate_buy_point_signals(data_dict, regime_map, use_v62=False):
    """
    模拟买点信号生成:
    - 用V5引擎的买点逻辑(MA20缩量回踩/放量突破回踩)近似重构买点触发
    - 三档买点: 激进=MA20*1.01, 稳健=MA20*0.99, 保守=MA20*0.97
    - 统计触发后T+1/3/5前瞻收益
    use_v62: True=启用V6.2改进(MA60下行过滤+放量确认+熊市门槛75)
    """
    results = []
    for code, df in data_dict.items():
        if code == BENCHMARK or len(df) < 80:
            continue
        df = df.reset_index(drop=True)
        n = len(df)
        for i in range(60, n):
            row = df.iloc[i]
            date = str(row['date'])
            if date > END_DATE or date < START_DATE:
                continue
            close, low, vol = row['close'], row['low'], row['volume']
            ma20, ma20_s = row['ma20'], row['ma20_slope']
            vol_ma = row['vol_ma20']
            if pd.isna(ma20) or pd.isna(ma20_s) or pd.isna(vol_ma) or vol_ma == 0:
                continue
            regime = regime_map.get(date, "RANGE")
            # 信号质量评分（简化版）
            signal_quality = 50
            # 买点1: 缩量回踩MA20
            bp1 = (vol < vol_ma * 0.70 and low <= ma20 * 1.01 and ma20_s > 0 and close >= ma20)
            if bp1:
                signal_quality += 10
            # MA60支撑
            ma60 = row['ma60']
            if not pd.isna(ma60) and abs(low - ma60) / ma60 < 0.02:
                signal_quality += 15
            # MACD金叉
            if not pd.isna(row['macd_dif']) and not pd.isna(row['macd_dea']):
                if not pd.isna(df.iloc[i-1]['macd_dif']) and not pd.isna(df.iloc[i-1]['macd_dea']):
                    if df.iloc[i-1]['macd_dif'] <= df.iloc[i-1]['macd_dea'] and row['macd_dif'] > row['macd_dea']:
                        signal_quality += 10
            # RSI超卖
            if not pd.isna(row['rsi']) and row['rsi'] < 30:
                signal_quality += 10
            # 均线多头
            if not pd.isna(ma60) and ma20 > ma60:
                signal_quality += 5
            signal_quality = min(100, signal_quality)
            # V6.2改进: 熊市门槛75(原70)
            min_q = 75 if regime == "BEAR" else 65 if use_v62 else (70 if regime == "BEAR" else 65)
            if not bp1 or signal_quality < min_q:
                continue
            # V6.2改进: MA60下行过滤（下降趋势中回踩大概率破位）
            if use_v62 and not pd.isna(ma60):
                ma60_slope_5 = ma60 - df.iloc[max(0, i-5)]['ma60'] if not pd.isna(df.iloc[max(0, i-5)]['ma60']) else 0
                if ma60_slope_5 < -ma60 * 0.005:  # MA60日斜率<-0.5%视为下行
                    continue
            # V6.2改进: 次日放量阳线确认（回踩后1日内需出现放量阳线）
            if use_v62:
                _confirmed = False
                for k in range(1, min(3, n - i)):
                    nxt = df.iloc[i + k]
                    if nxt['close'] > nxt['open'] and nxt['volume'] > vol_ma * 0.8:
                        _confirmed = True
                        break
                if not _confirmed:
                    continue
            # 急涨过滤
            if i >= 5:
                surge5 = (close - df.iloc[i-5]['close']) / df.iloc[i-5]['close'] if df.iloc[i-5]['close'] > 0 else 0
                if surge5 > 0.25:
                    continue
            # 三档买点
            aggressive = round(ma20 * 1.01, 2)
            moderate = round(ma20 * 0.99, 2)
            conservative = round(ma20 * 0.97, 2)
            stop_loss = round(ma20 * 0.90, 2)
            # 前瞻收益计算
            fwd = {}
            for nd, key in [(1, 'r1'), (3, 'r3'), (5, 'r5')]:
                if i + nd < n:
                    fwd_close = df.iloc[i + nd]['close']
                    fwd[key] = round((fwd_close / close - 1) * 100, 2)
                else:
                    fwd[key] = None
            # 5日内最高涨幅(命中率)
            max_5d = 0
            min_5d = 0
            for j in range(1, min(6, n - i)):
                chg = (df.iloc[i+j]['high'] / close - 1) * 100
                max_5d = max(max_5d, chg)
                low_chg = (df.iloc[i+j]['low'] / close - 1) * 100
                min_5d = min(min_5d, low_chg)
            results.append({
                'code': code, 'date': date, 'close': close,
                'ma20': round(ma20, 2), 'regime': regime,
                'signal_quality': signal_quality,
                'aggressive': aggressive, 'moderate': moderate,
                'conservative': conservative, 'stop_loss': stop_loss,
                'max_5d_pct': round(max_5d, 2), 'min_5d_pct': round(min_5d, 2),
                **fwd,
            })
    return results

# ============================================================
# 三、盘中预警回测（日线近似）
# ============================================================

def simulate_intraday_alerts(data_dict, regime_map, use_v62=False):
    """
    用日线数据近似模拟盘中预警:
    1. 梯度减仓: 浮亏-5%/-8%/-10%触发
    2. 急跌预警: 日跌幅<-3%
    3. 止盈提醒: 浮盈>=15%/25% (V6.2: 原10%/20%)
    4. 振幅异常: 日振幅>8%且股价<MA20 (V6.2: 加趋势过滤)
    5. 放量暴跌: 跌>5%且量>均量2倍
    6. 趋势破位: 连续3日收盘<MA60且MA60斜率<-0.5%且放量 (V6.2增强)
    7. 回落止盈: 浮盈>=6%且回撤>=阈值 (V6.2: 原5%→6%)
    """
    alerts = {'gradient_5': [], 'gradient_8': [], 'gradient_10': [],
              'rapid_drop': [], 'profit_10': [], 'profit_20': [],
              'amplitude': [], 'vol_crash': [], 'trend_break': [],
              'drawdown_profit': []}
    # 模拟持仓: 每只股票假设在信号首次出现前5日以收盘价买入
    for code, df in data_dict.items():
        if code == BENCHMARK or len(df) < 80:
            continue
        df = df.reset_index(drop=True)
        n = len(df)
        # 模拟持仓: 用前60日MA20买点触发后持有
        in_position = False
        buy_price = 0
        highest = 0
        triggered = set()  # 已触发的预警类型
        triggered_rd = set()  # 急跌预警日期去重
        for i in range(65, n):
            row = df.iloc[i]
            date = str(row['date'])
            if date > END_DATE:
                break
            close, low, high = row['close'], row['low'], row['high']
            pct = row['pct_change'] if not pd.isna(row['pct_change']) else 0
            vol = row['volume']
            vol_ma = row['vol_ma20']
            ma20, ma60 = row['ma20'], row['ma60']
            ma20_s = row['ma20_slope']
            # 模拟入场: MA20缩量回踩买入
            if not in_position:
                if (not pd.isna(ma20) and not pd.isna(ma20_s) and ma20_s > 0
                    and close >= ma20 and not pd.isna(vol_ma) and vol_ma > 0
                    and vol < vol_ma * 0.70 and low <= ma20 * 1.01):
                    buy_price = close
                    highest = high
                    in_position = True
                continue
            highest = max(highest, high)
            # 止损出局
            if close < buy_price * 0.90:
                in_position = False
                continue
            amplitude = (high - low) / low * 100 if low > 0 else 0
            loss_pct = (close / buy_price - 1)
            profit_pct = loss_pct
            # 1. 梯度减仓
            if loss_pct <= -0.05 and 'L1' not in triggered:
                triggered.add('L1')
                alerts['gradient_5'].append({'code': code, 'date': date, 'loss': round(loss_pct*100,2), 'fwd5': _fwd(df, i, 5)})
            if loss_pct <= -0.08 and 'L2' not in triggered:
                triggered.add('L2')
                alerts['gradient_8'].append({'code': code, 'date': date, 'loss': round(loss_pct*100,2), 'fwd5': _fwd(df, i, 5)})
            if loss_pct <= -0.10 and 'L3' not in triggered:
                triggered.add('L3')
                alerts['gradient_10'].append({'code': code, 'date': date, 'loss': round(loss_pct*100,2), 'fwd5': _fwd(df, i, 5)})
            # 2. 急跌预警
            if pct < -3.0 and date not in triggered_rd:
                triggered_rd.add(date)
                alerts['rapid_drop'].append({'code': code, 'date': date, 'pct': round(pct,2), 'fwd5': _fwd(df, i, 5)})
            # 3. 止盈提醒 (V6.2: 10%/20% → 15%/25%)
            _p1_th = 0.15 if use_v62 else 0.10
            _p2_th = 0.25 if use_v62 else 0.20
            if profit_pct >= _p1_th and 'P1' not in triggered:
                triggered.add('P1')
                alerts['profit_10'].append({'code': code, 'date': date, 'profit': round(profit_pct*100,2), 'fwd5': _fwd(df, i, 5)})
            if profit_pct >= _p2_th and 'P2' not in triggered:
                triggered.add('P2')
                alerts['profit_20'].append({'code': code, 'date': date, 'profit': round(profit_pct*100,2), 'fwd5': _fwd(df, i, 5)})
            # 4. 振幅异常 (V6.2: 加趋势过滤，仅股价<MA20时预警)
            _amp_ok = True
            if use_v62 and not pd.isna(ma20):
                _amp_ok = close < ma20  # 仅弱势时预警
            if amplitude > 8.0 and _amp_ok and 'AMP' not in triggered:
                triggered.add('AMP')
                alerts['amplitude'].append({'code': code, 'date': date, 'amp': round(amplitude,2), 'fwd5': _fwd(df, i, 5)})
            # 5. 放量暴跌
            if (pct < -5.0 and not pd.isna(vol_ma) and vol_ma > 0
                and vol > vol_ma * 2.0 and 'VC' not in triggered):
                triggered.add('VC')
                alerts['vol_crash'].append({'code': code, 'date': date, 'pct': round(pct,2), 'fwd5': _fwd(df, i, 5)})
            # 6. 趋势破位 (V6.2: 增加连续破位+斜率阈值+放量确认)
            if not pd.isna(ma60) and close < ma60 and i >= 5:
                ma60_slope = ma60 - df.iloc[max(0,i-5)]['ma60'] if not pd.isna(df.iloc[max(0,i-5)]['ma60']) else 0
                if use_v62:
                    # V6.2增强: MA60斜率<-0.5% + 连续3日<MA60 + 放量确认
                    _slope_ok = ma60_slope < -ma60 * 0.005
                    _consec = all(
                        j < n and df.iloc[j]['close'] < (df.iloc[j]['ma60'] if not pd.isna(df.iloc[j]['ma60']) else float('inf'))
                        for j in range(max(0, i-2), i+1)
                    )
                    _vol_ok = not pd.isna(vol_ma) and vol_ma > 0 and vol > vol_ma * 1.3
                    if _slope_ok and _consec and _vol_ok and 'TB' not in triggered:
                        triggered.add('TB')
                        alerts['trend_break'].append({'code': code, 'date': date, 'close': close, 'ma60': round(ma60,2), 'fwd5': _fwd(df, i, 5)})
                else:
                    if ma60_slope < 0 and 'TB' not in triggered:
                        triggered.add('TB')
                        alerts['trend_break'].append({'code': code, 'date': date, 'close': close, 'ma60': round(ma60,2), 'fwd5': _fwd(df, i, 5)})
            # 7. 回落止盈 (V6.2: 浮盈门槛5%→6%)
            _dd_min_profit = 0.06 if use_v62 else 0.05
            if profit_pct >= _dd_min_profit and highest > buy_price:
                dd = (highest - low) / highest
                if dd >= 0.06 and 'DD' not in triggered:
                    triggered.add('DD')
                    alerts['drawdown_profit'].append({'code': code, 'date': date, 'profit': round(profit_pct*100,2), 'dd': round(dd*100,2), 'fwd5': _fwd(df, i, 5)})
    return alerts

def _fwd(df, i, n):
    """计算未来N日收益"""
    if i + n < len(df):
        return round((df.iloc[i+n]['close'] / df.iloc[i]['close'] - 1) * 100, 2)
    return None

# ============================================================
# 四、统计分析与报告
# ============================================================

def analyze_buy_signals(signals):
    """买点信号统计分析"""
    if not signals:
        return {"total": 0, "hit_3pct": 0, "hit_rate": 0.0, "false_sig": 0, "false_rate": 0.0}
    df = pd.DataFrame(signals)
    total = len(df)
    # 命中率: 5日内最高涨幅>3%
    hit_3pct = (df['max_5d_pct'] > 3.0).sum()
    # 假信号率: 5日内最低跌幅>5%
    false_sig = (df['min_5d_pct'] < -5.0).sum()
    # 各档前瞻收益
    stats = {"total": total, "hit_3pct": int(hit_3pct), "hit_rate": round(hit_3pct/total*100, 1),
             "false_sig": int(false_sig), "false_rate": round(false_sig/total*100, 1)}
    for key in ['r1', 'r3', 'r5']:
        vals = df[key].dropna()
        if len(vals) > 0:
            stats[key] = {"n": len(vals), "avg": round(vals.mean(), 2),
                          "win_rate": round((vals > 0).sum() / len(vals) * 100, 1),
                          "median": round(vals.median(), 2)}
    # 按市场环境分组
    for regime in ['BULL', 'BEAR', 'RANGE']:
        sub = df[df['regime'] == regime]
        if len(sub) == 0:
            continue
        r_stats = {"n": len(sub), "hit_rate": round((sub['max_5d_pct'] > 3.0).sum() / len(sub) * 100, 1)}
        for key in ['r1', 'r3', 'r5']:
            vals = sub[key].dropna()
            if len(vals) > 0:
                r_stats[key] = {"avg": round(vals.mean(), 2), "win_rate": round((vals > 0).sum() / len(vals) * 100, 1)}
        stats[f"regime_{regime}"] = r_stats
    return stats

def analyze_alerts(alerts):
    """预警统计分析"""
    summary = {}
    for alert_type, items in alerts.items():
        if not items:
            summary[alert_type] = {"count": 0}
            continue
        fwds = [x['fwd5'] for x in items if x.get('fwd5') is not None]
        # 有效性: 预警后5日继续下跌(对风险预警)或继续上涨(对止盈预警)
        is_risk = alert_type in ['gradient_5','gradient_8','gradient_10','rapid_drop','vol_crash','trend_break']
        if is_risk:
            # 风险预警: 后续5日收益<0说明预警准确
            valid = sum(1 for f in fwds if f < 0)
        else:
            # 止盈/回落: 后续5日收益>0说明应该继续持有(预警过早)
            valid = sum(1 for f in fwds if f > 0)
        avg_fwd = round(np.mean(fwds), 2) if fwds else 0
        summary[alert_type] = {
            "count": len(items),
            "avg_fwd5": avg_fwd,
            "valid_rate": round(valid / len(fwds) * 100, 1) if fwds else 0,
            "fwd_positive": sum(1 for f in fwds if f > 0),
            "fwd_negative": sum(1 for f in fwds if f <= 0),
        }
    return summary

def generate_report(buy_stats, alert_stats, signals):
    """生成结构化报告"""
    lines = []
    lines.append("=" * 70)
    lines.append("  操盘密码系统 — 买点提醒 + 盘中预警 历史回测验证报告")
    lines.append("=" * 70)
    lines.append(f"  回测区间: {START_DATE} ~ {END_DATE}")
    lines.append(f"  标的范围: 数据库全量(161只)")
    lines.append(f"  数据频率: 日线(盘中预警为近似模拟)")
    lines.append("")

    # === 买点提醒 ===
    lines.append("━" * 70)
    lines.append("  一、实时买点提醒功能验证")
    lines.append("━" * 70)
    lines.append(f"  信号触发总数: {buy_stats['total']}")
    if buy_stats['total'] > 0:
        lines.append(f"  5日内命中(涨>3%): {buy_stats['hit_3pct']}次  命中率: {buy_stats['hit_rate']}%")
        lines.append(f"  5日内假信号(跌>5%): {buy_stats['false_sig']}次  假信号率: {buy_stats['false_rate']}%")
        lines.append("")
        lines.append("  ─── 前瞻收益统计 ───")
        for key, label in [('r1','T+1'), ('r3','T+3'), ('r5','T+5')]:
            if key in buy_stats:
                s = buy_stats[key]
                lines.append(f"  {label}: {s['n']}条  平均{s['avg']:+.2f}%  胜率{s['win_rate']}%  中位数{s['median']:+.2f}%")
        lines.append("")
        lines.append("  ─── 按市场环境分组 ───")
        for regime, label in [('BULL','牛市'), ('RANGE','震荡'), ('BEAR','熊市')]:
            k = f"regime_{regime}"
            if k in buy_stats:
                s = buy_stats[k]
                r1 = s.get('r1', {}).get('avg', 0)
                r1_wr = s.get('r1', {}).get('win_rate', 0)
                r5 = s.get('r5', {}).get('avg', 0)
                r5_wr = s.get('r5', {}).get('win_rate', 0)
                lines.append(f"  {label}({regime}): {s['n']}次  命中率{s['hit_rate']}%  "
                           f"T+1均{r1:+.2f}%(胜率{r1_wr}%)  T+5均{r5:+.2f}%(胜率{r5_wr}%)")

    lines.append("")
    # === 盘中预警 ===
    lines.append("━" * 70)
    lines.append("  二、实时盘中预警功能验证")
    lines.append("━" * 70)
    alert_labels = {
        'gradient_5': '浮亏梯度减仓(-5%)', 'gradient_8': '浮亏梯度减仓(-8%)',
        'gradient_10': '浮亏梯度减仓(-10%)', 'rapid_drop': '急跌预警(日跌>3%)',
        'profit_10': '止盈提醒(浮盈≥10%)', 'profit_20': '止盈提醒(浮盈≥20%)',
        'amplitude': '振幅异常(>8%)', 'vol_crash': '放量暴跌(跌>5%+量2x)',
        'trend_break': '趋势破位(<MA60且下行)', 'drawdown_profit': '回落止盈(盈>5%回撤>6%)',
    }
    lines.append(f"  {'预警类型':<25} {'触发':>6} {'5日均收益':>10} {'有效率':>8} {'有效/无效':>10}")
    lines.append("  " + "-" * 65)
    for atype, label in alert_labels.items():
        s = alert_stats.get(atype, {"count": 0})
        cnt = s.get('count', 0)
        avg = s.get('avg_fwd5', 0)
        vr = s.get('valid_rate', 0)
        pos = s.get('fwd_positive', 0)
        neg = s.get('fwd_negative', 0)
        lines.append(f"  {label:<25} {cnt:>5}次 {avg:>+9.2f}% {vr:>7.1f}% {pos:>4}/{neg:<4}")

    lines.append("")
    lines.append("━" * 70)
    lines.append("  三、关键发现与改进建议")
    lines.append("━" * 70)

    # 买点分析
    if buy_stats['total'] > 0:
        hit = buy_stats['hit_rate']
        false = buy_stats['false_rate']
        if hit < 40:
            lines.append(f"  [P0] 买点命中率偏低({hit}%)，建议提高信号质量门槛或增加确认指标")
        if false > 20:
            lines.append(f"  [P0] 假信号率偏高({false}%)，建议加强下跌趋势过滤")
        r5 = buy_stats.get('r5', {})
        if r5.get('avg', 0) < 0:
            lines.append(f"  [P1] T+5平均收益为负({r5['avg']:+.2f}%)，买点整体偏左侧，建议增加右侧确认")
        # 市场环境差异
        bull_s = buy_stats.get('regime_BULL', {})
        bear_s = buy_stats.get('regime_BEAR', {})
        if bull_s and bear_s:
            b_hit = bull_s.get('hit_rate', 0)
            be_hit = bear_s.get('hit_rate', 0)
            if b_hit - be_hit > 15:
                lines.append(f"  [P1] 牛熊命中率差异大(牛{b_hit}% vs 熊{be_hit}%)，熊市应加强闸门拦截")

    # 预警分析
    g5 = alert_stats.get('gradient_5', {})
    if g5.get('count', 0) > 0:
        vr = g5.get('valid_rate', 0)
        if vr < 50:
            lines.append(f"  [P1] 梯度减仓-5%预警有效率仅{vr}%，超半数触发后5日反弹，阈值可能过紧")
    rd = alert_stats.get('rapid_drop', {})
    if rd.get('count', 0) > 0:
        vr = rd.get('valid_rate', 0)
        avg = rd.get('avg_fwd5', 0)
        lines.append(f"  [P2] 急跌预警{rd['count']}次，有效率{vr}%，5日均收益{avg:+.2f}%")
    tb = alert_stats.get('trend_break', {})
    if tb.get('count', 0) > 0:
        vr = tb.get('valid_rate', 0)
        lines.append(f"  [P1] 趋势破位预警{tb['count']}次，有效率{vr}%，{'有效' if vr > 60 else '需优化'}")

    lines.append("")
    lines.append("  ─── 局限性说明 ───")
    lines.append("  1. 日线数据无法模拟分时级预警(5分钟急跌/VWAP/盘口托盘)，实际有效率可能不同")
    lines.append("  2. 买点三档价位用MA20近似，实际由选股报告CANSLIM模型生成，存在差异")
    lines.append("  3. 大盘闸门用收盘价判定，实际盘中用实时涨跌幅，存在时点差异")
    lines.append("  4. 持仓模拟简化(MA20回踩买入)，实际持仓由用户手动管理")
    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)

# ============================================================
# 五、主函数
# ============================================================

def main():
    logger.info("加载数据...")
    df_all = load_data()
    logger.info(f"数据: {len(df_all)}行, {df_all['code'].nunique()}只股票")

    # 计算指标
    logger.info("计算指标...")
    data_dict = {}
    benchmark_df = None
    for code, grp in df_all.groupby('code'):
        g = grp.copy().reset_index(drop=True)
        g = compute_indicators(g)
        data_dict[code] = g
        if code == BENCHMARK:
            benchmark_df = g

    # 市场环境
    regime_map = compute_market_regime(benchmark_df) if benchmark_df is not None else {}
    regime_counts = pd.Series(list(regime_map.values())).value_counts()
    logger.info(f"市场环境分布: {dict(regime_counts)}")

    # ===== V6.1 基线回测 =====
    logger.info("=== V6.1 基线回测 ===")
    signals_old = simulate_buy_point_signals(data_dict, regime_map, use_v62=False)
    buy_stats_old = analyze_buy_signals(signals_old)
    alerts_old = simulate_intraday_alerts(data_dict, regime_map, use_v62=False)
    alert_stats_old = analyze_alerts(alerts_old)
    logger.info(f"V6.1 买点信号: {len(signals_old)}次")

    # ===== V6.2 改进后回测 =====
    logger.info("=== V6.2 改进后回测 ===")
    signals_new = simulate_buy_point_signals(data_dict, regime_map, use_v62=True)
    buy_stats_new = analyze_buy_signals(signals_new)
    alerts_new = simulate_intraday_alerts(data_dict, regime_map, use_v62=True)
    alert_stats_new = analyze_alerts(alerts_new)
    logger.info(f"V6.2 买点信号: {len(signals_new)}次")

    # ===== 生成对比报告 =====
    report = generate_comparison_report(
        buy_stats_old, buy_stats_new,
        alert_stats_old, alert_stats_new,
        signals_old, signals_new,
        regime_counts
    )
    print("\n" + report)

    # 保存结果
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, 'alert_backtest_v62_comparison.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    def _to_native(obj):
        """递归转换numpy类型为Python原生类型"""
        if isinstance(obj, dict):
            return {k: _to_native(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [_to_native(v) for v in obj]
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    json_result = _to_native({
        "v61": {
            "buy_stats": buy_stats_old,
            "alert_stats": alert_stats_old,
            "signal_count": len(signals_old),
        },
        "v62": {
            "buy_stats": buy_stats_new,
            "alert_stats": alert_stats_new,
            "signal_count": len(signals_new),
        },
        "regime_distribution": dict(regime_counts),
        "backtest_period": f"{START_DATE} ~ {END_DATE}",
    })
    json_path = os.path.join(out_dir, 'alert_backtest_v62_comparison.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(json_result, f, ensure_ascii=False, indent=2)
    logger.info(f"报告: {report_path}")
    logger.info(f"JSON: {json_path}")


def generate_comparison_report(bs_old, bs_new, as_old, as_new, sig_old, sig_new, regime_counts):
    """生成V6.1 vs V6.2对比报告"""
    lines = []
    lines.append("=" * 78)
    lines.append("  操盘密码系统 V6.2 — 买点提醒+盘中预警 改进效果对比报告")
    lines.append("=" * 78)
    lines.append(f"  回测区间: {START_DATE} ~ {END_DATE}  |  标的: 161只  |  环境: 震荡{regime_counts.get('RANGE',0)}/"
                 f"牛{regime_counts.get('BULL',0)}/熊{regime_counts.get('BEAR',0)}")
    lines.append("")

    # === 买点提醒对比 ===
    lines.append("━" * 78)
    lines.append("  一、买点提醒功能 V6.1 vs V6.2 对比")
    lines.append("━" * 78)
    lines.append(f"  {'指标':<20} {'V6.1(基线)':>14} {'V6.2(改进)':>14} {'变化':>12}")
    lines.append("  " + "-" * 62)

    def _pct_change(old, new):
        if old == 0: return "N/A"
        d = new - old
        return f"{d:+.1f}%" if abs(d) < 100 else f"{d:+.0f}%"

    def _num_change(old, new):
        d = new - old
        return f"{d:+d}"

    o_hr = bs_old.get('hit_rate', 0)
    n_hr = bs_new.get('hit_rate', 0)
    o_fr = bs_old.get('false_rate', 0)
    n_fr = bs_new.get('false_rate', 0)
    lines.append(f"  {'信号触发总数':<20} {bs_old['total']:>13}次 {bs_new['total']:>13}次 {_num_change(bs_old['total'], bs_new['total']):>10}次")
    lines.append(f"  {'5日命中率(涨>3%)':<20} {o_hr:>12.1f}% {n_hr:>12.1f}% {_pct_change(o_hr, n_hr):>11}")
    lines.append(f"  {'5日假信号率(跌>5%)':<20} {o_fr:>12.1f}% {n_fr:>12.1f}% {_pct_change(o_fr, n_fr):>11}")
    lines.append("")

    lines.append("  ─── 前瞻收益对比 ───")
    for key, label in [('r1','T+1'), ('r3','T+3'), ('r5','T+5')]:
        o = bs_old.get(key, {})
        n = bs_new.get(key, {})
        if o and n:
            lines.append(f"  {label}: V6.1 均{o.get('avg',0):+.2f}%(胜率{o.get('win_rate',0)}%)  →  "
                         f"V6.2 均{n.get('avg',0):+.2f}%(胜率{n.get('win_rate',0)}%)")
    lines.append("")

    lines.append("  ─── 按市场环境对比 ───")
    for regime, label in [('BULL','牛市'), ('RANGE','震荡'), ('BEAR','熊市')]:
        ko = f"regime_{regime}"
        o = bs_old.get(ko, {})
        n = bs_new.get(ko, {})
        if o and n:
            lines.append(f"  {label}: V6.1 {o['n']}次/命中{o['hit_rate']}%  →  V6.2 {n['n']}次/命中{n['hit_rate']}%")
    lines.append("")

    # === 盘中预警对比 ===
    lines.append("━" * 78)
    lines.append("  二、盘中预警功能 V6.1 vs V6.2 对比")
    lines.append("━" * 78)
    alert_labels = {
        'gradient_5': '浮亏梯度减仓(-5%)', 'gradient_8': '浮亏梯度减仓(-8%)',
        'gradient_10': '浮亏梯度减仓(-10%)', 'rapid_drop': '急跌预警(日跌>3%)',
        'profit_10': '止盈提醒(第1档)', 'profit_20': '止盈提醒(第2档)',
        'amplitude': '振幅异常(>8%)', 'vol_crash': '放量暴跌(跌>5%+量2x)',
        'trend_break': '趋势破位(<MA60下行)', 'drawdown_profit': '回落止盈(盈+回撤)',
    }
    lines.append(f"  {'预警类型':<22} {'V6.1触发':>8} {'有效率':>7} {'V6.2触发':>8} {'有效率':>7} {'变化':>10}")
    lines.append("  " + "-" * 66)
    for atype, label in alert_labels.items():
        o = as_old.get(atype, {"count": 0})
        n = as_new.get(atype, {"count": 0})
        o_cnt = o.get('count', 0)
        n_cnt = n.get('count', 0)
        o_vr = o.get('valid_rate', 0)
        n_vr = n.get('valid_rate', 0)
        vr_diff = f"{n_vr - o_vr:+.1f}%" if o_cnt > 0 and n_cnt > 0 else ("新增" if o_cnt == 0 and n_cnt > 0 else ("消失" if o_cnt > 0 and n_cnt == 0 else "-"))
        lines.append(f"  {label:<22} {o_cnt:>7}次 {o_vr:>6.1f}% {n_cnt:>7}次 {n_vr:>6.1f}% {vr_diff:>9}")
    lines.append("")

    # === 改进措施说明 ===
    lines.append("━" * 78)
    lines.append("  三、V6.2改进措施清单")
    lines.append("━" * 78)
    lines.append("  [P0-1] 买点假信号过滤: MA60下行不买 + 回踩后需放量阳线确认")
    lines.append("  [P0-2] 趋势破位增强: MA60斜率<-0.5% + 连续3日<MA60 + 放量1.3x确认")
    lines.append("  [P1-1] 熊市闸门收紧: L2闸门-1.5%→-1.0% + 熊市质量门槛70→75")
    lines.append("  [P1-2] 止盈提醒推迟: 第1档10%→15%, 第2档20%→25%")
    lines.append("  [P1-3] 回落止盈门槛: 浮盈5%→6%")
    lines.append("  [P2-1] 振幅趋势过滤: 仅当股价<MA20时预警振幅异常")
    lines.append("")

    # === 综合评价 ===
    lines.append("━" * 78)
    lines.append("  四、综合评价")
    lines.append("━" * 78)
    if bs_new['total'] > 0:
        hit_imp = bs_new.get('hit_rate', 0) - bs_old.get('hit_rate', 0)
        false_imp = bs_old.get('false_rate', 0) - bs_new.get('false_rate', 0)
        lines.append(f"  买点命中率: {bs_old.get('hit_rate',0)}% → {bs_new.get('hit_rate',0)}% ({hit_imp:+.1f}pp)")
        lines.append(f"  假信号率:   {bs_old.get('false_rate',0)}% → {bs_new.get('false_rate',0)}% ({false_imp:+.1f}pp)")
        sig_reduce = (1 - bs_new['total'] / max(bs_old['total'], 1)) * 100
        lines.append(f"  信号量缩减: {bs_old['total']}→{bs_new['total']} ({sig_reduce:.1f}%)，质量换数量")
    tb_o = as_old.get('trend_break', {})
    tb_n = as_new.get('trend_break', {})
    if tb_o.get('count', 0) > 0 and tb_n.get('count', 0) > 0:
        lines.append(f"  趋势破位有效率: {tb_o['valid_rate']}% → {tb_n['valid_rate']}%")
    lines.append("")
    lines.append("  ─── 局限性说明 ───")
    lines.append("  1. 日线数据无法模拟分时级预警(5分钟急跌/VWAP/盘口托盘)")
    lines.append("  2. 买点三档价位用MA20近似，实际由CANSLIM模型生成")
    lines.append("  3. 大盘闸门用收盘价判定，实际用实时涨跌幅")
    lines.append("  4. 持仓模拟简化(MA20回踩买入)，实际由用户手动管理")
    lines.append("")
    lines.append("=" * 78)
    return "\n".join(lines)

if __name__ == "__main__":
    main()
