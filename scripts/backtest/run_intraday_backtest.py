# -*- coding: utf-8 -*-
"""
分钟级盘中预警回测验证（V9.3 P1-⑧ 新增）
==========================================
基于5分钟K线数据，精确验证盘中预警的时效性:
  1. 急跌预警: 5分钟跌>3% → 后续30/60分钟是否继续下跌
  2. 波动率突变: 短期RV突破长期均值+2σ → 后续波动持续性
  3. 放量暴跌: 5分钟量>均量2倍+跌>2% → 后续走势
  4. 梯度减仓: 浮亏-5%/-8%/-10% → 分钟级确认时间

与日线回测的关键差异:
  - 日线: "今日跌3%" → 无法区分开盘跌还是尾盘跌
  - 分钟: "10:35分跌3%" → 可精确度量后续5/15/30分钟走势

数据源: baostock 5分钟K线（免费，最近约2个月数据）
使用方式:
    python run_intraday_backtest.py
"""

import sys, os, datetime, logging, json, warnings
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_system'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("intraday_bt")

# ============================================================
# 一、分钟K线数据获取（baostock）
# ============================================================

def fetch_minute_data_baostock(code: str, freq: str = "5",
                                start_date: str = None,
                                end_date: str = None) -> pd.DataFrame:
    """通过baostock获取分钟K线

    参数:
        code: 股票代码（纯数字）
        freq: "5"/"15"/"30"/"60"
        start_date/end_date: "YYYY-MM-DD"

    返回:
        DataFrame: datetime, open, close, high, low, volume, amount
    """
    try:
        import baostock as bs
    except ImportError:
        logger.error("baostock未安装: pip install baostock")
        return pd.DataFrame()

    # baostock代码格式
    if code.startswith("6") or code.startswith("9") or code == "000300":
        bs_code = f"sh.{code}"
    else:
        bs_code = f"sz.{code}"

    if end_date is None:
        end_date = datetime.date.today().isoformat()
    if start_date is None:
        start_date = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()

    try:
        lg = bs.login()
        if lg.error_code != '0':
            logger.warning(f"baostock登录失败: {lg.error_msg}")
            return pd.DataFrame()

        fields = "date,time,open,high,low,close,volume,amount"
        rs = bs.query_history_k_data_plus(
            bs_code, fields,
            start_date=start_date, end_date=end_date,
            frequency=freq, adjustflag="2"  # 前复权
        )

        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(rs.get_row_data())

        bs.logout()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=rs.fields)
        for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            df[c] = pd.to_numeric(df[c], errors='coerce')

        # 构造datetime
        df['datetime'] = pd.to_datetime(df['date'] + ' ' + df['time'])
        df = df.sort_values('datetime').reset_index(drop=True)
        return df

    except Exception as e:
        logger.warning(f"baostock分钟数据获取失败({code}): {e}")
        try:
            bs.logout()
        except Exception:
            pass
        return pd.DataFrame()


# ============================================================
# 二、分钟级预警模拟
# ============================================================

def simulate_intraday_alerts_minute(df: pd.DataFrame, code: str,
                                     buy_price: float = None) -> list:
    """在5分钟K线上模拟盘中预警

    参数:
        df: 5分钟K线 DataFrame
        code: 股票代码
        buy_price: 买入价（用于梯度减仓/止盈模拟），None则用第一根K线收盘价

    返回:
        list of dict: [{type, bar_idx, datetime, price, ...}, ...]
    """
    if df.empty or len(df) < 30:
        return []

    alerts = []
    if buy_price is None:
        buy_price = df.iloc[0]['close']

    highest_since_buy = buy_price
    triggered = set()

    for i in range(5, len(df)):
        row = df.iloc[i]
        dt = row['datetime']
        close = row['close']
        high = row['high']
        low = row['low']
        volume = row['volume']

        highest_since_buy = max(highest_since_buy, high)

        # ---- 1. 急跌预警: 5分钟跌幅>3% ----
        if i >= 1:
            prev_close = df.iloc[i-1]['close']
            if prev_close > 0:
                pct_5m = (close / prev_close - 1) * 100
                if pct_5m < -3.0:
                    key = f"rapid_{dt.date()}"
                    if key not in triggered:
                        triggered.add(key)
                        alerts.append({
                            "type": "急跌预警(5min)",
                            "bar_idx": i,
                            "datetime": str(dt),
                            "price": close,
                            "pct_5m": round(pct_5m, 2),
                        })

        # ---- 2. 波动率突变: 短期RV > 长期均值+2σ ----
        if i >= 20:
            returns = df['close'].pct_change().dropna()
            short_rv = returns.iloc[max(0,i-5):i+1].std()
            long_rv_mean = returns.iloc[max(0,i-20):i+1].mean()
            long_rv_std = returns.iloc[max(0,i-20):i+1].std()
            if long_rv_std > 0 and short_rv > long_rv_mean + 2 * long_rv_std:
                key = f"vol_regime_{dt.date()}"
                if key not in triggered:
                    triggered.add(key)
                    alerts.append({
                        "type": "波动率突变",
                        "bar_idx": i,
                        "datetime": str(dt),
                        "price": close,
                        "short_rv": round(short_rv * 100, 4),
                        "long_rv_mean": round(long_rv_mean * 100, 4),
                    })

        # ---- 3. 放量暴跌: 5分钟量>均量2倍+跌>2% ----
        if i >= 20:
            vol_ma = df['volume'].iloc[max(0,i-20):i].mean()
            prev_close = df.iloc[i-1]['close']
            if prev_close > 0 and vol_ma > 0:
                pct_5m = (close / prev_close - 1) * 100
                if pct_5m < -2.0 and volume > vol_ma * 2.0:
                    key = f"vol_crash_{dt.date()}"
                    if key not in triggered:
                        triggered.add(key)
                        alerts.append({
                            "type": "放量暴跌(5min)",
                            "bar_idx": i,
                            "datetime": str(dt),
                            "price": close,
                            "pct_5m": round(pct_5m, 2),
                            "vol_ratio": round(volume / vol_ma, 1),
                        })

        # ---- 4. 梯度减仓: 浮亏-5%/-8%/-10% ----
        loss_pct = (close / buy_price - 1)
        for level, threshold in [(-5, -0.05), (-8, -0.08), (-10, -0.10)]:
            if loss_pct <= threshold:
                key = f"gradient_{level}"
                if key not in triggered:
                    triggered.add(key)
                    alerts.append({
                        "type": f"梯度减仓({level}%)",
                        "bar_idx": i,
                        "datetime": str(dt),
                        "price": close,
                        "loss_pct": round(loss_pct * 100, 2),
                    })

    return alerts


def compute_forward_returns(df: pd.DataFrame, bar_idx: int,
                             windows: list = None) -> dict:
    """计算预警后N根K线的收益

    参数:
        df: 5分钟K线
        bar_idx: 预警触发的K线索引
        windows: [5, 15, 30] → 对应5根/15根/30根5分钟K线

    返回:
        {"fwd_5bar": float, "fwd_15bar": float, "fwd_30bar": float,
         "min_return": float, "max_return": float}
    """
    if windows is None:
        windows = [5, 15, 30]  # 5根(25min), 15根(75min), 30根(150min)

    base_price = df.iloc[bar_idx]['close']
    if base_price <= 0:
        return {}

    result = {}
    min_ret = 0
    max_ret = 0

    for w in windows:
        target_idx = bar_idx + w
        if target_idx < len(df):
            target_price = df.iloc[target_idx]['close']
            ret = (target_price / base_price - 1) * 100
            result[f"fwd_{w}bar"] = round(ret, 3)
            # 窗口内极值
            window_data = df.iloc[bar_idx:target_idx+1]
            if len(window_data) > 0:
                window_high = window_data['high'].max()
                window_low = window_data['low'].min()
                max_ret = max(max_ret, (window_high / base_price - 1) * 100)
                min_ret = min(min_ret, (window_low / base_price - 1) * 100)
        else:
            result[f"fwd_{w}bar"] = None

    result["min_return"] = round(min_ret, 3)
    result["max_return"] = round(max_ret, 3)
    return result


# ============================================================
# 三、统计分析与报告
# ============================================================

def analyze_minute_alerts(all_alerts: list, df_map: dict) -> dict:
    """分钟级预警统计分析

    参数:
        all_alerts: [(code, alert_dict, forward_returns), ...]
        df_map: {code: DataFrame} 5分钟K线
    """
    by_type = {}

    for code, alert, fwd in all_alerts:
        atype = alert["type"]
        if atype not in by_type:
            by_type[atype] = {"alerts": [], "forwards": []}
        by_type[atype]["alerts"].append(alert)
        by_type[atype]["forwards"].append(fwd)

    summary = {}
    for atype, data in by_type.items():
        n = len(data["alerts"])
        # 风险预警: 后续下跌=有效; 机会预警: 后续上涨=有效
        is_risk = any(kw in atype for kw in ["急跌", "波动率", "放量暴跌", "梯度减仓"])

        fwd_vals = [f.get("fwd_30bar") for f in data["forwards"]
                    if f.get("fwd_30bar") is not None]
        if not fwd_vals:
            summary[atype] = {"count": n, "avg_fwd_30bar": None, "valid_rate": None}
            continue

        avg_fwd = round(np.mean(fwd_vals), 3)
        if is_risk:
            valid = sum(1 for f in fwd_vals if f < -0.3)
        else:
            valid = sum(1 for f in fwd_vals if f > 0.3)

        # 各窗口统计
        fwd_5 = [f.get("fwd_5bar") for f in data["forwards"] if f.get("fwd_5bar") is not None]
        fwd_15 = [f.get("fwd_15bar") for f in data["forwards"] if f.get("fwd_15bar") is not None]

        summary[atype] = {
            "count": n,
            "avg_fwd_5bar": round(np.mean(fwd_5), 3) if fwd_5 else None,
            "avg_fwd_15bar": round(np.mean(fwd_15), 3) if fwd_15 else None,
            "avg_fwd_30bar": avg_fwd,
            "valid_rate": round(valid / len(fwd_vals) * 100, 1) if fwd_vals else None,
            "min_avg": round(np.mean([f.get("min_return", 0) for f in data["forwards"]]), 3),
            "max_avg": round(np.mean([f.get("max_return", 0) for f in data["forwards"]]), 3),
        }

    return summary


def generate_intraday_report(summary: dict, total_stocks: int,
                              data_period: str) -> str:
    """生成分钟级回测报告"""
    lines = []
    lines.append("=" * 72)
    lines.append("  操盘密码系统 V9.3 — 分钟级盘中预警回测验证报告")
    lines.append("=" * 72)
    lines.append(f"  数据频率: 5分钟K线")
    lines.append(f"  数据周期: {data_period}")
    lines.append(f"  回测标的: {total_stocks}只")
    lines.append("")

    lines.append("━" * 72)
    lines.append("  分钟级预警有效性统计")
    lines.append("━" * 72)
    lines.append(f"  {'预警类型':<22} {'触发':>6} {'5bar均收益':>10} "
                 f"{'15bar均收益':>11} {'30bar均收益':>11} {'有效率':>8}")
    lines.append("  " + "-" * 70)

    for atype, stats in sorted(summary.items()):
        cnt = stats["count"]
        a5 = stats.get("avg_fwd_5bar")
        a15 = stats.get("avg_fwd_15bar")
        a30 = stats.get("avg_fwd_30bar")
        vr = stats.get("valid_rate")

        a5_s = f"{a5:>+9.3f}%" if a5 is not None else f"{'N/A':>10}"
        a15_s = f"{a15:>+10.3f}%" if a15 is not None else f"{'N/A':>11}"
        a30_s = f"{a30:>+10.3f}%" if a30 is not None else f"{'N/A':>11}"
        vr_s = f"{vr:>7.1f}%" if vr is not None else f"{'N/A':>8}"

        lines.append(f"  {atype:<22} {cnt:>5}次 {a5_s} {a15_s} {a30_s} {vr_s}")

    lines.append("")
    lines.append("  ─── 说明 ───")
    lines.append("  • 5bar = 25分钟(5根×5分钟), 15bar = 75分钟, 30bar = 150分钟")
    lines.append("  • 风险预警有效率 = 后续继续下跌(>0.3%)的比例")
    lines.append("  • 有效率>60%说明预警有实际预测价值")
    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


# ============================================================
# 四、主函数
# ============================================================

def main():
    # 从持仓中选取回测标的（最多10只，节省时间）
    holdings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'trading_system', 'holdings.json')
    if os.path.exists(holdings_path):
        with open(holdings_path, 'r', encoding='utf-8') as f:
            holdings = json.load(f)
        codes = list(holdings.keys())[:10]
    else:
        codes = ["600584", "002415", "000725", "601318", "000002"]

    logger.info(f"分钟级回测标的: {codes}")

    all_alerts = []
    df_map = {}
    data_periods = []

    for code in codes:
        logger.info(f"获取 {code} 5分钟K线...")
        df = fetch_minute_data_baostock(code, freq="5")
        if df.empty or len(df) < 50:
            logger.warning(f"  {code}: 数据不足({len(df)}根)，跳过")
            continue

        df_map[code] = df
        data_periods.append(f"{df.iloc[0]['datetime']}~{df.iloc[-1]['datetime']}")
        logger.info(f"  {code}: {len(df)}根K线, "
                    f"{df.iloc[0]['datetime']} ~ {df.iloc[-1]['datetime']}")

        # 模拟预警
        alerts = simulate_intraday_alerts_minute(df, code)
        logger.info(f"  {code}: 触发{len(alerts)}条预警")

        # 计算前瞻收益
        for alert in alerts:
            fwd = compute_forward_returns(df, alert["bar_idx"])
            all_alerts.append((code, alert, fwd))

    if not all_alerts:
        logger.warning("无预警触发，无法生成报告")
        return

    # 统计分析
    summary = analyze_minute_alerts(all_alerts, df_map)

    # 生成报告
    period_str = "; ".join(set(
        p.split("~")[0][:10] for p in data_periods
    )) + " ~ " + "; ".join(set(
        p.split("~")[1][:10] if "~" in p else "" for p in data_periods
    ))
    report = generate_intraday_report(summary, len(df_map), period_str)
    print("\n" + report)

    # 保存
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, 'intraday_backtest_v93.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    # JSON结果
    json_path = os.path.join(out_dir, 'intraday_backtest_v93.json')
    def _to_native(obj):
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

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(_to_native({
            "summary": summary,
            "total_alerts": len(all_alerts),
            "stocks_tested": len(df_map),
            "data_period": period_str,
        }), f, ensure_ascii=False, indent=2)

    logger.info(f"报告: {report_path}")
    logger.info(f"JSON: {json_path}")


if __name__ == "__main__":
    main()
