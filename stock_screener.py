"""
全市场选股程序
================
基于「高胜率A股交易操作系统V2.0」选股规则，从全A股市场筛选符合条件的标的

选股规则（来自V2.0文档）:
  1. 排除ST/*ST/退市股
  2. 流动性：日均成交额 >= 8亿，市值 >= 150亿
  3. 趋势：股价站稳20日均线，20日均线向上
  4. 中期趋势：股价在60日均线上方
  5. 股性稳定：近30日单日振幅>10%的天数 <= 3天
  6. 排除高位股：不处于明确下降通道

使用方法:
  python stock_screener.py                  # 全市场扫描（耗时较长）
  python stock_screener.py --sector 半导体   # 按赛道关键词筛选
  python stock_screener.py --top 20         # 只输出前20只
"""

import os
import sys
import time
import datetime
import logging
import argparse

# 将trading_system加入路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "trading_system"))

import baostock as bs
import pandas as pd
import numpy as np

import config as cfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ============================================================
# 一、获取全市场股票列表
# ============================================================

def get_all_stocks() -> pd.DataFrame:
    """获取全部A股股票列表"""
    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"baostock登录失败: {lg.error_msg}")

    # 使用 query_stock_basic 获取股票基本信息
    rs = bs.query_stock_basic()
    data = []
    while rs.error_code == '0' and rs.next():
        data.append(rs.get_row_data())

    bs.logout()

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=rs.fields)
    # type=1 为股票，status=1 为上市
    df = df[(df["type"] == "1") & (df["status"] == "1")]
    # 只保留A股代码：sh.6xxxxx / sz.00xxxx / sz.30xxxx
    df = df[df["code"].str.match(r"^(sh\.6|sz\.0|sz\.3)", na=False)]
    # 排除ST股
    df = df[~df["code_name"].str.contains("ST|\\*ST|退", na=False)]

    return df.reset_index(drop=True)


# ============================================================
# 二、单只股票筛选
# ============================================================

def quick_pre_screen(code: str, end_date: str = None,
                     min_amount: float = 8e8) -> bool:
    """
    快速预筛选（60日数据），淘汰明显不符合的股票
    通过预筛选的股票才会进入详细分析
    返回: True=通过预筛选, False=淘汰
    """
    if end_date is None:
        end_date = datetime.date.today().strftime("%Y-%m-%d")
    start_date = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")

    try:
        rs = bs.query_history_k_data_plus(
            code, "date,close,volume,amount",
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="2"
        )
    except Exception:
        return False

    if rs.error_code != '0':
        return False

    data = []
    while rs.error_code == '0' and rs.next():
        data.append(rs.get_row_data())

    if len(data) < 30:
        return False

    df = pd.DataFrame(data, columns=rs.fields)
    for col in ["close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=["close"])

    if len(df) < 30:
        return False

    # 快速检查1：流动性
    avg_amount = df["amount"].tail(20).mean()
    if avg_amount < min_amount:
        return False

    # 快速检查2：股价在MA20上方
    ma20 = df["close"].rolling(20).mean()
    if pd.isna(ma20.iloc[-1]) or df["close"].iloc[-1] < ma20.iloc[-1]:
        return False

    # 快速检查3：MA20向上
    if ma20.diff(3).iloc[-1] <= 0:
        return False

    return True


def screen_single_stock(code: str, name: str,
                        end_date: str = None,
                        min_amount: float = 8e8,
                        min_market_cap: float = 150e8) -> dict:
    """
    筛选单只股票是否符合V2.0选股标准

    参数:
        code: baostock格式代码 (sh.600584)
        name: 股票名称
        end_date: 截止日期 YYYY-MM-DD
        min_amount: 最低日均成交额
        min_market_cap: 最低市值

    返回:
        {
            "pass": bool,
            "code": str,
            "name": str,
            "close": float,
            "ma20": float,
            "ma60": float,
            "avg_amount": float,
            "score": float,         # 综合评分
            "reasons": [str]        # 通过/淘汰原因
        } 或 None（数据不足时）
    """
    if end_date is None:
        end_date = datetime.date.today().strftime("%Y-%m-%d")
    start_date = (datetime.date.today() - datetime.timedelta(days=180)).strftime("%Y-%m-%d")

    try:
        rs = bs.query_history_k_data_plus(
            code,
            "date,open,close,high,low,volume,amount,turn",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2"
        )
    except Exception:
        return None

    if rs.error_code != '0':
        return None

    data = []
    while rs.error_code == '0' and rs.next():
        data.append(rs.get_row_data())

    if len(data) < 60:
        return None

    df = pd.DataFrame(data, columns=rs.fields)
    for col in ["open", "close", "high", "low", "volume", "amount", "turn"]:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=["close"])

    if len(df) < 60:
        return None

    reasons = []
    score = 0
    latest = df.iloc[-1]
    close = latest["close"]

    # ---- 规则1：流动性检查（近20日日均成交额）----
    avg_amount = df["amount"].tail(20).mean()
    if avg_amount < min_amount:
        return None  # 成交额不足，直接淘汰（不输出原因，减少噪音）

    score += 10  # 流动性达标

    # ---- 规则2：趋势检查 - 股价站稳20日均线 ----
    ma20 = df["close"].rolling(20).mean()
    if pd.isna(ma20.iloc[-1]):
        return None
    if close < ma20.iloc[-1]:
        return None  # 股价在20日线下方，淘汰

    score += 20
    reasons.append(f"站稳MA20({ma20.iloc[-1]:.2f})")

    # ---- 规则3：20日均线向上 ----
    ma20_slope = ma20.diff(5).iloc[-1]
    if ma20_slope <= 0:
        return None  # 20日线未向上，淘汰

    score += 20
    reasons.append("MA20向上")

    # ---- 规则4：股价在60日均线上方 ----
    ma60 = df["close"].rolling(60).mean()
    if pd.isna(ma60.iloc[-1]):
        return None
    if close < ma60.iloc[-1]:
        return None  # 股价在60日线下方，淘汰

    score += 15
    reasons.append(f"站稳MA60({ma60.iloc[-1]:.2f})")

    # ---- 规则5：60日均线向上（中期趋势）----
    ma60_slope = ma60.diff(5).iloc[-1]
    if ma60_slope > 0:
        score += 10
        reasons.append("MA60向上")

    # ---- 规则6：股性稳定（近30日振幅>10%天数<=3）----
    recent = df.tail(30)
    daily_range = (recent["high"] - recent["low"]) / recent["close"].shift(1)
    big_move_days = (daily_range > 0.10).sum()
    if big_move_days > 3:
        score -= 5
        reasons.append(f"股性偏活跃({big_move_days}天振幅>10%)")
    else:
        score += 10
        reasons.append(f"股性稳定({big_move_days}天振幅>10%)")

    # ---- 规则7：近20日涨幅不过大（排除短期暴涨股）----
    if len(df) >= 20:
        pct_20d = (close / df["close"].iloc[-20] - 1) * 100
        if pct_20d > 30:
            score -= 10
            reasons.append(f"20日涨幅过大({pct_20d:.1f}%)")
        elif pct_20d > 0:
            score += 5

    # ---- 规则8：量能配合（近期成交量温和放大）----
    vol_recent = df["volume"].tail(5).mean()
    vol_ma20 = df["volume"].tail(20).mean()
    if vol_recent > vol_ma20 * 0.8:
        score += 5
        reasons.append("量能配合")

    return {
        "pass": True,
        "code": code,
        "name": name,
        "close": round(close, 2),
        "ma20": round(ma20.iloc[-1], 2),
        "ma60": round(ma60.iloc[-1], 2),
        "avg_amount_yi": round(avg_amount / 1e8, 2),
        "score": score,
        "reasons": "; ".join(reasons)
    }


# ============================================================
# 三、批量扫描
# ============================================================

def run_screener(sector_keyword: str = None, top_n: int = None,
                 min_amount: float = 8e8) -> pd.DataFrame:
    """
    执行全市场选股

    参数:
        sector_keyword: 赛道关键词过滤（如"半导体"、"芯片"）
        top_n: 只输出前N只
        min_amount: 最低日均成交额（元）

    返回:
        符合条件的股票DataFrame
    """
    print("=" * 60)
    print("  全市场选股 - 高胜率A股交易操作系统V2.0")
    print(f"  扫描日期: {datetime.date.today().strftime('%Y-%m-%d')}")
    print(f"  最低成交额: {min_amount/1e8:.0f}亿")
    if sector_keyword:
        print(f"  赛道关键词: {sector_keyword}")
    print("=" * 60)

    # 获取全部股票
    print("\n[1/3] 获取A股股票列表...")
    all_stocks = get_all_stocks()
    print(f"  共 {len(all_stocks)} 只A股（已排除ST/北交所）")

    if sector_keyword:
        all_stocks = all_stocks[all_stocks["code_name"].str.contains(sector_keyword, na=False)]
        print(f"  关键词过滤后: {len(all_stocks)} 只")

    # 登录baostock（长连接）
    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"baostock登录失败: {lg.error_msg}")

    # 第一阶段：快速预筛选（90日数据，检查流动性+MA20趋势）
    total = len(all_stocks)
    print(f"\n[2/4] 第一阶段：快速预筛选（共{total}只）...")
    pre_passed = []
    start_time = time.time()

    for idx, row in all_stocks.iterrows():
        code = row["code"]
        name = row["code_name"]

        if quick_pre_screen(code, min_amount=min_amount):
            pre_passed.append((code, name))

        if (idx + 1) % 500 == 0:
            elapsed = time.time() - start_time
            print(f"  进度: {idx+1}/{total} ({(idx+1)/total:.0%}) "
                  f"预通过:{len(pre_passed)} 耗时:{elapsed/60:.1f}分钟")
            sys.stdout.flush()

    print(f"  预筛选完成: {total}只 -> {len(pre_passed)}只通过 ({len(pre_passed)/total:.1%})")
    sys.stdout.flush()

    if not pre_passed:
        print("  无股票通过预筛选")
        bs.logout()
        return pd.DataFrame()

    # 第二阶段：详细分析（180日数据，完整V2.0规则）
    print(f"\n[3/4] 第二阶段：详细分析（{len(pre_passed)}只）...")
    results = []
    passed = 0

    for code, name in pre_passed:
        result = screen_single_stock(code, name, min_amount=min_amount)
        if result and result["pass"]:
            results.append(result)
            passed += 1
            print(f"  [{passed}] {code} {name} 评分:{result['score']} "
                  f"现价:{result['close']} MA20:{result['ma20']}")
            sys.stdout.flush()

    bs.logout()

    elapsed = time.time() - start_time
    print(f"\n  筛选完成: 预筛选{total}只, 详细通过{passed}只, 耗时{elapsed/60:.1f}分钟")

    # 排序输出
    print(f"\n[4/4] 生成选股结果...")
    if not results:
        print("  未找到符合条件的股票")
        return pd.DataFrame()

    df_result = pd.DataFrame(results)
    df_result = df_result.sort_values("score", ascending=False)

    if top_n:
        df_result = df_result.head(top_n)

    # 打印结果
    print("\n" + "=" * 60)
    print(f"  选股结果（共{len(df_result)}只，按评分排序）")
    print("=" * 60)
    print(f"{'排名':>4} {'代码':<10} {'名称':<10} {'现价':>8} {'MA20':>8} {'MA60':>8} "
          f"{'日均亿':>6} {'评分':>4} 条件")
    print("-" * 90)
    for i, (_, row) in enumerate(df_result.iterrows(), 1):
        print(f"{i:>4} {row['code']:<10} {row['name']:<10} {row['close']:>8.2f} "
              f"{row['ma20']:>8.2f} {row['ma60']:>8.2f} {row['avg_amount_yi']:>6.1f} "
              f"{row['score']:>4} {row['reasons']}")

    # 保存结果
    output_file = os.path.join(PROJECT_ROOT, "trading_system", "output",
                               f"选股结果_{datetime.date.today().strftime('%Y%m%d')}.csv")
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    df_result.to_csv(output_file, index=False, encoding="utf-8-sig")
    print(f"\n  结果已保存: {output_file}")

    return df_result


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="全市场选股程序")
    parser.add_argument("--sector", type=str, default=None,
                        help="赛道关键词过滤（如：半导体、芯片、新能源）")
    parser.add_argument("--top", type=int, default=None,
                        help="只输出前N只")
    parser.add_argument("--amount", type=float, default=8,
                        help="最低日均成交额（亿元），默认8亿")
    args = parser.parse_args()

    run_screener(
        sector_keyword=args.sector,
        top_n=args.top,
        min_amount=args.amount * 1e8
    )
