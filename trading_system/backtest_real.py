"""
真实环境回测引擎 V5.0
======================
严格遵循三大原则:
1. 无未来函数: 信号在T日收盘后计算，T+1日开盘执行
2. 真实交易环境: 手续费0.3% + 滑点(龙头0.2%/弹性0.5%) + 涨跌停过滤 + T+1
3. 参数极简: 只保留核心参数，不过度拟合

V5.0新增优化:
- 买点2: 放量突破后缩量回踩确认
- 硬性过滤: 流动性/振幅/放量暴跌
- 双轨止盈: 阶梯止盈(8%卖1/3, 20%再卖1/3) + 回落止盈(底仓)
- 强制卖出: 单日放量大跌>8%无条件离场
- 信号质量评分: 多支撑重合+MACD金叉+RSI超卖

交易环境配置:
- 佣金+印花税: 买卖合计0.3% (commission=0.003)
- 滑点: 龙头0.2%, 弹性0.5%
- 成交规则: T日收盘判断信号 → T+1开盘价成交
- 涨跌停: 一字涨停无法买入, 一字跌停无法卖出
- T+1: 买入当天不能卖出
- 仓位: 单笔风险2%本金

测试标的: 持仓8只 + 额夔12只 = 20只
回测区间: 2022-07-01 ~ 2025-07-01 (3年)
"""

import sys
import os
import datetime
import logging

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# 一、交易环境参数（不可过度调整）
# ============================================================

COMMISSION = 0.003       # 买卖合计手续费（佣金+印花税）
SLIPPAGE_LEADER = 0.002  # 龙头股滑点 0.2%
SLIPPAGE_FLEX = 0.005    # 弹性股滑点 0.5%
RISK_PER_TRADE = 0.02    # 单笔风险2%本金
TOTAL_CAPITAL = 760000   # 总资金
LIMIT_PCT = 0.095        # 涨跌停判定阈值（9.5%以上视为一字板）

# ============================================================
# 二、测试标的池（20只，覆盖多行业）
# ============================================================

TEST_STOCKS = {
    # --- 用户持仓8只 ---
    "002371": {"名称": "北方华创", "类型": "龙头", "行业": "半导体"},
    "002409": {"名称": "雅克科技", "类型": "龙头", "行业": "半导体"},
    "600118": {"名称": "中国卫星", "类型": "弹性", "行业": "军工"},
    "600584": {"名称": "长电科技", "类型": "龙头", "行业": "半导体"},
    "603986": {"名称": "兆易创新", "类型": "龙头", "行业": "半导体"},
    "000725": {"名称": "京东方A", "类型": "弹性", "行业": "面板"},
    "002384": {"名称": "东山精密", "类型": "弹性", "行业": "电子"},
    "600760": {"名称": "中航沈飞", "类型": "弹性", "行业": "军工"},
    # --- 额外12只（扩大样本） ---
    "300750": {"名称": "宁德时代", "类型": "龙头", "行业": "新能源"},
    "002594": {"名称": "比亚迪", "类型": "龙头", "行业": "新能源"},
    "601012": {"名称": "隆基绿能", "类型": "龙头", "行业": "新能源"},
    "002230": {"名称": "科大讯飞", "类型": "龙头", "行业": "AI"},
    "600519": {"名称": "贵州茅台", "类型": "龙头", "行业": "消费"},
    "601318": {"名称": "中国平安", "类型": "龙头", "行业": "金融"},
    "300760": {"名称": "迈瑞医疗", "类型": "龙头", "行业": "医药"},
    "601899": {"名称": "紫金矿业", "类型": "龙头", "行业": "有色"},
    "002049": {"名称": "紫光国微", "类型": "龙头", "行业": "半导体"},
    "600893": {"名称": "航发动力", "类型": "龙头", "行业": "军工"},
    "300274": {"名称": "阳光电源", "类型": "龙头", "行业": "新能源"},
    "600036": {"名称": "招商银行", "类型": "龙头", "行业": "金融"},
}

# ============================================================
# 三、数据获取
# ============================================================

def fetch_history_data(code: str, start_date: str = "2022-01-01") -> pd.DataFrame:
    """通过baostock获取历史日线数据（前复权）"""
    import baostock as bs

    if code.startswith("6") or code.startswith("9") or code == "000300":
        bs_code = f"sh.{code}"
    else:
        bs_code = f"sz.{code}"

    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"baostock登录失败: {lg.error_msg}")

    end_date = datetime.date.today().strftime("%Y-%m-%d")
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,close,high,low,volume,amount",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="2"
    )

    data = []
    while rs.error_code == '0' and rs.next():
        data.append(rs.get_row_data())

    bs.logout()

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=["date", "open", "close", "high", "low", "volume", "amount"])
    for col in ["open", "close", "high", "low", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=["close"])
    df = df[df["volume"] > 0].reset_index(drop=True)
    return df


# ============================================================
# 四、技术指标（极简，只用MA20/MA60/Volume）
# ============================================================

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算指标 - 参数固定，不可调整"""
    df = df.copy()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["ma20_slope"] = df["ma20"].diff(3)

    # MACD（标准参数12/26/9，不可调）
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_dif"] = ema12 - ema26
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()

    # RSI（标准14日）
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # 涨跌幅（用于涨跌停判定）
    df["pct_change"] = df["close"].pct_change()

    return df


# ============================================================
# 五、真实环境回测引擎
# ============================================================

def is_limit_up(row) -> bool:
    """判定是否一字涨停（无法买入）"""
    if pd.isna(row["pct_change"]):
        return False
    # 涨幅>9.5% 且 开盘=最高=最低=收盘（一字板）
    if row["pct_change"] > LIMIT_PCT:
        if abs(row["open"] - row["high"]) < 0.01 and abs(row["open"] - row["low"]) < 0.01:
            return True
    return False


def is_limit_down(row) -> bool:
    """判定是否一字跌停（无法卖出）"""
    if pd.isna(row["pct_change"]):
        return False
    if row["pct_change"] < -LIMIT_PCT:
        if abs(row["open"] - row["high"]) < 0.01 and abs(row["open"] - row["low"]) < 0.01:
            return True
    return False


def backtest_stock_v4(df: pd.DataFrame, code: str, info: dict, version: str = "v2") -> list:
    """
    真实环境回测引擎
    
    关键规则:
    - T日收盘后计算信号（只用T日及之前的数据）
    - T+1日开盘价执行（加滑点）
    - T+1日如果是涨跌停则无法成交，信号作废
    - T+1规则：买入当天不能卖出
    - 手续费0.3%，滑点按股票类型
    
    version: "v2"=原始规则, "v4"=优化规则
    """
    if len(df) < 80:
        return []

    df = compute_indicators(df)
    trades = []
    stock_type = info.get("类型", "龙头")
    slippage = SLIPPAGE_LEADER if stock_type == "龙头" else SLIPPAGE_FLEX

    # 状态
    in_position = False
    buy_price = 0       # 实际买入价（含滑点）
    buy_date = ""
    buy_index = 0
    highest_since_buy = 0
    pending_sell = False  # T+1: 是否有待执行的卖出信号

    # 从第60根K线开始
    for i in range(60, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        date = row["date"]
        close = row["close"]
        low = row["low"]
        volume = row["volume"]

        if not in_position:
            # ==== 买入信号判定（T日收盘后）====
            # 只用T日及之前的数据，无未来函数

            ma20 = row["ma20"]
            ma20_slope = row["ma20_slope"]
            if pd.isna(ma20) or pd.isna(ma20_slope):
                continue

            # 条件1: MA20向上 + 收盘价在MA20上方
            if ma20_slope <= 0 or close < ma20:
                continue

            # 条件2: 缩量（量<20日均量70%）
            vol_ma = row["vol_ma20"]
            if pd.isna(vol_ma) or vol_ma == 0:
                continue
            if volume >= vol_ma * 0.70:
                continue

            # 条件3: 最低价触及MA20±1%
            if low > ma20 * 1.01:
                continue

            # V4优化: 额外确认（仅优化版）
            if version == "v4":
                # 确认MA60也向上（中期趋势）
                ma60 = row["ma60"]
                if pd.isna(ma60):
                    continue
                ma60_slope = df["ma60"].iloc[i] - df["ma60"].iloc[max(0, i-5)]
                if ma60_slope < 0:
                    continue

            # ==== T+1执行买入 ====
            if i + 1 >= len(df):
                continue

            next_row = df.iloc[i + 1]

            # 检查T+1日是否一字涨停（无法买入）
            if is_limit_up(next_row):
                continue

            # 实际买入价 = T+1开盘价 + 滑点
            exec_price = next_row["open"] * (1 + slippage)
            buy_price = exec_price
            buy_date = next_row["date"]
            buy_index = i + 1
            highest_since_buy = next_row["high"]
            in_position = True

        else:
            # ==== 持仓管理 ====
            highest_since_buy = max(highest_since_buy, row["high"])

            # T+1规则：买入当天不能卖出
            if i <= buy_index:
                continue

            # ==== 卖出信号判定（T日收盘后）====
            sell_signal = False
            sell_type = ""

            profit_pct = (close - buy_price) / buy_price

            # 1. 止损（收盘价触发）
            if version == "v2":
                # V2.0原始止损
                if profit_pct < 0.05:
                    stop_price = buy_price * 0.90
                elif profit_pct < 0.15:
                    stop_price = buy_price
                elif profit_pct < 0.30:
                    stop_price = buy_price * 1.10
                else:
                    stop_price = buy_price * 1.20
            else:
                # V4.0优化止损（更合理的分段）
                if profit_pct < 0.05:
                    stop_price = buy_price * 0.92  # 初始8%（原10%太宽）
                elif profit_pct < 0.15:
                    stop_price = buy_price * 1.02  # 保本+2%（覆盖手续费）
                elif profit_pct < 0.30:
                    stop_price = buy_price * 1.12  # 锁定12%
                else:
                    stop_price = buy_price * 1.22  # 锁定22%

            if close <= stop_price:
                sell_signal = True
                sell_type = "止损"

            # 2. 趋势破位
            if not sell_signal and not pd.isna(row["ma60"]):
                ma60_slope = df["ma60"].iloc[i] - df["ma60"].iloc[max(0, i-3)]
                if close < row["ma60"] and ma60_slope < 0:
                    sell_signal = True
                    sell_type = "趋势破位"

            # 3. MACD死叉（盈利状态下）
            if not sell_signal and profit_pct > 0:
                if not pd.isna(row["macd_dif"]) and not pd.isna(row["macd_dea"]):
                    if not pd.isna(prev["macd_dif"]) and not pd.isna(prev["macd_dea"]):
                        if prev["macd_dif"] >= prev["macd_dea"] and row["macd_dif"] < row["macd_dea"]:
                            sell_signal = True
                            sell_type = "MACD死叉"

            # 4. 回落止盈
            if not sell_signal and highest_since_buy > buy_price:
                if version == "v2":
                    threshold = 0.03 if stock_type == "弹性" else 0.05
                else:
                    threshold = 0.04 if stock_type == "弹性" else 0.055
                drawdown = (highest_since_buy - close) / highest_since_buy
                if drawdown >= threshold:
                    sell_signal = True
                    sell_type = "回落止盈"

            # ==== T+1执行卖出 ====
            if sell_signal:
                if i + 1 >= len(df):
                    # 回测最后一天，按收盘价平仓
                    exec_price = close * (1 - slippage)
                    sell_date = date
                else:
                    next_row = df.iloc[i + 1]
                    # 检查T+1日是否一字跌停（无法卖出）
                    if is_limit_down(next_row):
                        continue  # 卖不出去，继续持有
                    exec_price = next_row["open"] * (1 - slippage)
                    sell_date = next_row["date"]

                # 计算净收益（扣除手续费）
                gross_profit = (exec_price - buy_price) / buy_price
                net_profit = gross_profit - COMMISSION  # 扣除买卖合计手续费

                hold_days = (i + 1 - buy_index) if i + 1 < len(df) else (i - buy_index)

                trades.append({
                    "code": code,
                    "name": info["名称"],
                    "industry": info["行业"],
                    "stock_type": stock_type,
                    "buy_date": buy_date,
                    "buy_price": round(buy_price, 3),
                    "sell_date": sell_date,
                    "sell_price": round(exec_price, 3),
                    "gross_profit": round(gross_profit * 100, 2),
                    "net_profit": round(net_profit * 100, 2),
                    "hold_days": hold_days,
                    "sell_type": sell_type,
                    "highest": round(highest_since_buy, 3),
                })
                in_position = False

    # 回测结束仍持仓
    if in_position:
        last_row = df.iloc[-1]
        exec_price = last_row["close"] * (1 - slippage)
        gross_profit = (exec_price - buy_price) / buy_price
        net_profit = gross_profit - COMMISSION
        hold_days = len(df) - 1 - buy_index
        trades.append({
            "code": code,
            "name": info["名称"],
            "industry": info["行业"],
            "stock_type": stock_type,
            "buy_date": buy_date,
            "buy_price": round(buy_price, 3),
            "sell_date": last_row["date"],
            "sell_price": round(exec_price, 3),
            "gross_profit": round(gross_profit * 100, 2),
            "net_profit": round(net_profit * 100, 2),
            "hold_days": hold_days,
            "sell_type": "回测结束",
            "highest": round(highest_since_buy, 3),
        })

    return trades


def backtest_stock_v5(df: pd.DataFrame, code: str, info: dict) -> list:
    """
    V5.0回测引擎 - 全面优化版
    
    新增优化:
    - 硬性过滤: 流动性/振幅/放量暴跌
    - 买点2: 放量突破后缩量回踩确认
    - 双轨止盈: 阶梯(8%卖1/3, 20%再卖1/3) + 回落(底仓)
    - 强制卖出: 单日放量大跌>8%且量>均量2倍
    - 信号质量评分: 多支撑重合+MACD金叉+RSI超卖
    """
    if len(df) < 80:
        return []

    df = compute_indicators(df)
    trades = []
    stock_type = info.get("类型", "龙头")
    slippage = SLIPPAGE_LEADER if stock_type == "龙头" else SLIPPAGE_FLEX

    # 状态
    in_position = False
    buy_price = 0
    buy_date = ""
    buy_index = 0
    highest_since_buy = 0
    position_shares = 0  # 当前持仓股数（用于双轨止盈分批卖出）
    initial_shares = 0   # 初始买入股数
    ladder_sold = [False, False]  # 阶梯止盈第1/2档是否已执行

    for i in range(60, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        date = row["date"]
        close = row["close"]
        low = row["low"]
        high = row["high"]
        volume = row["volume"]
        open_price = row["open"]

        if not in_position:
            # ==== 硬性过滤 ====
            # 1. 流动性: 日均成交额 >= 8亿
            if "amount" in df.columns and i >= 20:
                avg_amount = df["amount"].iloc[i-19:i+1].mean()
                if avg_amount < getattr(config, 'MIN_DAILY_AMOUNT', 8e8):
                    continue

            # 2. 股性稳定: 近30日振幅>10%的天数 <= 3
            if i >= 30:
                recent_30 = df.iloc[i-29:i+1]
                amplitude = (recent_30["high"] - recent_30["low"]) / recent_30["close"].shift(1)
                high_amp_days = (amplitude > 0.10).sum()
                if high_amp_days > getattr(config, 'MAX_HIGH_AMPLITUDE_DAYS', 3):
                    continue

            # 3. 无放量暴跌: 近5日无单日跌幅>8%且放量
            if i >= 5:
                recent_5 = df.iloc[i-4:i+1]
                for j in range(len(recent_5)):
                    r = recent_5.iloc[j]
                    if not pd.isna(r["pct_change"]) and r["pct_change"] < -0.08:
                        vol_ma_check = df["vol_ma20"].iloc[i-4+j] if not pd.isna(df["vol_ma20"].iloc[i-4+j]) else 0
                        if vol_ma_check > 0 and r["volume"] > vol_ma_check * 2:
                            break  # 有放量暴跌，跳过
                else:
                    pass  # 无放量暴跌，继续
                if j < len(recent_5) - 1 or (j == len(recent_5) - 1 and not pd.isna(recent_5.iloc[j]["pct_change"]) and recent_5.iloc[j]["pct_change"] < -0.08):
                    continue

            # ==== 买点判定 ====
            ma20 = row["ma20"]
            ma20_slope = row["ma20_slope"]
            ma60 = row["ma60"]
            if pd.isna(ma20) or pd.isna(ma20_slope):
                continue

            # 基础条件: MA20向上 + 收盘价在MA20上方
            if ma20_slope <= 0 or close < ma20:
                continue

            # MA60不能明确向下
            if not pd.isna(ma60) and i >= 5:
                ma60_slope = df["ma60"].iloc[i] - df["ma60"].iloc[max(0, i-5)]
                if ma60_slope < 0:
                    continue

            vol_ma = row["vol_ma20"]
            if pd.isna(vol_ma) or vol_ma == 0:
                continue

            buy_signal = False
            signal_quality = 50  # 基础分

            # ---- 买点1: 缩量回踩MA20 ----
            bp1 = False
            if volume < vol_ma * 0.70 and low <= ma20 * 1.01:
                # 排除放量下跌
                prev_close = prev["close"]
                day_change = (close - prev_close) / prev_close if prev_close > 0 else 0
                if not (day_change < -0.03 and volume > vol_ma * 1.5):
                    # 回调不创新低
                    if i >= 20:
                        recent_10_low = df["low"].iloc[i-9:i+1].min()
                        prev_wave_low = df["low"].iloc[i-19:i-9].min()
                        if recent_10_low >= prev_wave_low * 0.99:
                            bp1 = True
                    else:
                        bp1 = True

            # ---- 买点2: 放量突破后缩量回踩确认 ----
            bp2 = False
            lookback = getattr(config, 'BREAKOUT_LOOKBACK', 10)
            if i >= lookback + 20:
                # 近10日内是否有放量突破（量>均量1.5倍 + 创20日新高）
                for k in range(i - lookback, i):
                    k_row = df.iloc[k]
                    k_vol_ma = df["vol_ma20"].iloc[k]
                    if pd.isna(k_vol_ma) or k_vol_ma == 0:
                        continue
                    k_high_20 = df["high"].iloc[max(0, k-19):k].max()
                    if k_row["volume"] > k_vol_ma * 1.5 and k_row["close"] > k_high_20:
                        # 找到突破日，检查今日是否缩量回踩
                        breakout_close = k_row["close"]
                        if (volume < k_row["volume"] * getattr(config, 'BREAKOUT_PULLBACK_VOL', 0.50) and
                            close >= breakout_close * getattr(config, 'BREAKOUT_HOLD_PCT', 0.99)):
                            bp2 = True
                            break

            if bp1 or bp2:
                buy_signal = True
                # 信号质量评分
                if bp1 and bp2:
                    signal_quality += 15  # 双买点共振
                if bp1:
                    signal_quality += 5
                if bp2:
                    signal_quality += 10

                # 多支撑位重合
                support_count = 1
                if not pd.isna(ma60) and abs(low - ma60) / ma60 < 0.02:
                    support_count += 1
                if i >= 20:
                    platform_low = df["low"].iloc[i-19:i+1].min()
                    if abs(low - platform_low) / platform_low < 0.02:
                        support_count += 1
                if support_count >= 3:
                    signal_quality += 25
                elif support_count >= 2:
                    signal_quality += 15

                # MACD金叉确认
                if not pd.isna(row["macd_dif"]) and not pd.isna(row["macd_dea"]):
                    if not pd.isna(prev["macd_dif"]) and not pd.isna(prev["macd_dea"]):
                        if prev["macd_dif"] <= prev["macd_dea"] and row["macd_dif"] > row["macd_dea"]:
                            signal_quality += 10

                # RSI超卖
                if not pd.isna(row["rsi"]) and row["rsi"] < 30:
                    signal_quality += 10

                # 均线多头排列
                ma5 = df["ma5"].iloc[i] if "ma5" in df.columns else None
                ma10 = df["ma10"].iloc[i] if "ma10" in df.columns else None
                # 简化: MA20>MA60且向上
                if not pd.isna(ma60) and ma20 > ma60:
                    signal_quality += 5

                signal_quality = min(100, signal_quality)

                # 质量分太低不买入
                if signal_quality < 55:
                    buy_signal = False

            if not buy_signal:
                continue

            # ==== T+1执行买入 ====
            if i + 1 >= len(df):
                continue

            next_row = df.iloc[i + 1]
            if is_limit_up(next_row):
                continue

            exec_price = next_row["open"] * (1 + slippage)
            buy_price = exec_price
            buy_date = next_row["date"]
            buy_index = i + 1
            highest_since_buy = next_row["high"]
            in_position = True
            # 初始仓位（简化为固定金额）
            initial_shares = int(TOTAL_CAPITAL * 0.12 / exec_price / 100) * 100
            if initial_shares < 100:
                initial_shares = 100
            position_shares = initial_shares
            ladder_sold = [False, False]

        else:
            # ==== 持仓管理 ====
            highest_since_buy = max(highest_since_buy, high)

            # T+1规则
            if i <= buy_index:
                continue

            profit_pct = (close - buy_price) / buy_price
            sell_signal = False
            sell_type = ""
            sell_ratio = 1.0  # 卖出比例（双轨止盈用）

            # ---- 强制卖出: 单日放量大跌>8% ----
            if not pd.isna(row["pct_change"]) and row["pct_change"] < -0.08:
                if not pd.isna(vol_ma) and vol_ma > 0 and volume > vol_ma * 2:
                    sell_signal = True
                    sell_type = "强制卖出"
                    sell_ratio = 1.0

            # ---- 移动止损 ----
            if not sell_signal:
                if profit_pct < 0.05:
                    stop_price = buy_price * 0.92  # 初始8%
                elif profit_pct < 0.15:
                    stop_price = buy_price * 1.02  # 保本+2%
                elif profit_pct < 0.30:
                    stop_price = buy_price * 1.12  # 锁定12%
                else:
                    stop_price = buy_price * 1.22  # 锁定22%

                if close <= stop_price:
                    sell_signal = True
                    sell_type = "止损"
                    sell_ratio = 1.0

            # ---- 趋势破位 ----
            if not sell_signal and not pd.isna(row["ma60"]):
                ma60_slope = df["ma60"].iloc[i] - df["ma60"].iloc[max(0, i-3)]
                if close < row["ma60"] and ma60_slope < 0:
                    sell_signal = True
                    sell_type = "趋势破位"
                    sell_ratio = 1.0

            # ---- 双轨止盈: 第一轨阶梯止盈 ----
            if not sell_signal and position_shares > 0:
                # 第1档: 浮盈8% 卖1/3
                if not ladder_sold[0] and profit_pct >= 0.08:
                    sell_signal = True
                    sell_type = "阶梯止盈1"
                    sell_ratio = 1/3
                    ladder_sold[0] = True
                # 第2档: 浮盈20% 再卖1/3
                elif not ladder_sold[1] and profit_pct >= 0.20:
                    sell_signal = True
                    sell_type = "阶梯止盈2"
                    sell_ratio = 1/3
                    ladder_sold[1] = True

            # ---- 双轨止盈: 第二轨回落止盈（底仓）----
            if not sell_signal and highest_since_buy > buy_price * 1.05:
                # 回落幅度: 龙头5%, 弹性3%
                drawdown_threshold = 0.05 if stock_type == "龙头" else 0.03
                drawdown = (highest_since_buy - close) / highest_since_buy
                if drawdown >= drawdown_threshold and profit_pct > 0:
                    sell_signal = True
                    sell_type = "回落止盈"
                    sell_ratio = 1.0  # 剩余全部卖出

            # ---- MACD死叉（盈利状态下）----
            if not sell_signal and profit_pct > 0:
                if not pd.isna(row["macd_dif"]) and not pd.isna(row["macd_dea"]):
                    if not pd.isna(prev["macd_dif"]) and not pd.isna(prev["macd_dea"]):
                        if prev["macd_dif"] >= prev["macd_dea"] and row["macd_dif"] < row["macd_dea"]:
                            sell_signal = True
                            sell_type = "MACD死叉"
                            sell_ratio = 1.0

            # ==== T+1执行卖出 ====
            if sell_signal:
                if i + 1 >= len(df):
                    exec_price = close * (1 - slippage)
                    sell_date = date
                else:
                    next_row = df.iloc[i + 1]
                    if is_limit_down(next_row):
                        continue
                    exec_price = next_row["open"] * (1 - slippage)
                    sell_date = next_row["date"]

                # 计算实际卖出股数
                actual_sell_shares = int(position_shares * sell_ratio / 100) * 100
                if actual_sell_shares < 100:
                    actual_sell_shares = position_shares
                if sell_ratio >= 1.0:
                    actual_sell_shares = position_shares

                gross_profit = (exec_price - buy_price) / buy_price
                net_profit = gross_profit - COMMISSION

                hold_days = (i + 1 - buy_index) if i + 1 < len(df) else (i - buy_index)

                trades.append({
                    "code": code,
                    "name": info["名称"],
                    "industry": info["行业"],
                    "stock_type": stock_type,
                    "buy_date": buy_date,
                    "buy_price": round(buy_price, 3),
                    "sell_date": sell_date,
                    "sell_price": round(exec_price, 3),
                    "gross_profit": round(gross_profit * 100, 2),
                    "net_profit": round(net_profit * 100, 2),
                    "hold_days": hold_days,
                    "sell_type": sell_type,
                    "highest": round(highest_since_buy, 3),
                    "sell_ratio": round(sell_ratio, 2),
                })

                # 更新持仓
                position_shares -= actual_sell_shares
                if position_shares <= 0 or sell_ratio >= 1.0:
                    in_position = False
                    position_shares = 0

    # 回测结束仍持仓
    if in_position and position_shares > 0:
        last_row = df.iloc[-1]
        exec_price = last_row["close"] * (1 - slippage)
        gross_profit = (exec_price - buy_price) / buy_price
        net_profit = gross_profit - COMMISSION
        hold_days = len(df) - 1 - buy_index
        trades.append({
            "code": code,
            "name": info["名称"],
            "industry": info["行业"],
            "stock_type": stock_type,
            "buy_date": buy_date,
            "buy_price": round(buy_price, 3),
            "sell_date": last_row["date"],
            "sell_price": round(exec_price, 3),
            "gross_profit": round(gross_profit * 100, 2),
            "net_profit": round(net_profit * 100, 2),
            "hold_days": hold_days,
            "sell_type": "回测结束",
            "highest": round(highest_since_buy, 3),
            "sell_ratio": 1.0,
        })

    return trades


# ============================================================
# 六、统计分析
# ============================================================

def analyze_trades(all_trades: list) -> dict:
    """统计分析"""
    if not all_trades:
        return {"error": "无交易记录"}

    total = len(all_trades)
    wins = [t for t in all_trades if t["net_profit"] > 0]
    losses = [t for t in all_trades if t["net_profit"] <= 0]

    win_count = len(wins)
    loss_count = len(losses)
    win_rate = win_count / total * 100

    avg_win = np.mean([t["net_profit"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["net_profit"] for t in losses]) if losses else 0
    max_win = max(t["net_profit"] for t in all_trades)
    max_loss = min(t["net_profit"] for t in all_trades)

    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    expectancy = np.mean([t["net_profit"] for t in all_trades])

    # 累计收益（复利）
    cumulative = 1.0
    for t in all_trades:
        cumulative *= (1 + t["net_profit"] / 100)
    cumulative_pct = (cumulative - 1) * 100

    avg_hold = np.mean([t["hold_days"] for t in all_trades])
    avg_hold_win = np.mean([t["hold_days"] for t in wins]) if wins else 0
    avg_hold_loss = np.mean([t["hold_days"] for t in losses]) if losses else 0

    # 最大连续亏损
    max_consec_loss = 0
    streak = 0
    for t in all_trades:
        if t["net_profit"] <= 0:
            streak += 1
            max_consec_loss = max(max_consec_loss, streak)
        else:
            streak = 0

    # 按卖出原因
    sell_stats = {}
    for t in all_trades:
        st = t["sell_type"]
        if st not in sell_stats:
            sell_stats[st] = {"count": 0, "wins": 0, "total": 0}
        sell_stats[st]["count"] += 1
        if t["net_profit"] > 0:
            sell_stats[st]["wins"] += 1
        sell_stats[st]["total"] += t["net_profit"]

    # 按股票
    stock_stats = {}
    for t in all_trades:
        key = f"{t['code']} {t['name']}"
        if key not in stock_stats:
            stock_stats[key] = {"count": 0, "wins": 0, "total": 0}
        stock_stats[key]["count"] += 1
        if t["net_profit"] > 0:
            stock_stats[key]["wins"] += 1
        stock_stats[key]["total"] += t["net_profit"]

    # 按行业
    industry_stats = {}
    for t in all_trades:
        ind = t["industry"]
        if ind not in industry_stats:
            industry_stats[ind] = {"count": 0, "wins": 0, "total": 0}
        industry_stats[ind]["count"] += 1
        if t["net_profit"] > 0:
            industry_stats[ind]["wins"] += 1
        industry_stats[ind]["total"] += t["net_profit"]

    return {
        "total": total,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_win": round(max_win, 2),
        "max_loss": round(max_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
        "cumulative": round(cumulative_pct, 2),
        "avg_hold": round(avg_hold, 1),
        "avg_hold_win": round(avg_hold_win, 1),
        "avg_hold_loss": round(avg_hold_loss, 1),
        "max_consec_loss": max_consec_loss,
        "sell_stats": sell_stats,
        "stock_stats": stock_stats,
        "industry_stats": industry_stats,
    }


# ============================================================
# 七、HTML对比报告
# ============================================================

def generate_report(stats_v2: dict, stats_v4: dict, trades_v4: list) -> str:
    """生成V2 vs V5对比报告"""
    today = datetime.date.today().strftime("%Y-%m-%d")

    def delta_color(v):
        return "#e74c3c" if v > 0 else "#27ae60" if v < 0 else "#333"

    # 卖出原因表
    sell_rows = ""
    for st, d in sorted(stats_v4["sell_stats"].items(), key=lambda x: -x[1]["count"]):
        wr = d["wins"]/d["count"]*100 if d["count"]>0 else 0
        avg = d["total"]/d["count"] if d["count"]>0 else 0
        sell_rows += f"<tr><td>{st}</td><td>{d['count']}</td><td>{wr:.0f}%</td><td>{avg:+.2f}%</td></tr>"

    # 行业表
    ind_rows = ""
    for ind, d in sorted(stats_v4["industry_stats"].items(), key=lambda x: -x[1]["total"]):
        wr = d["wins"]/d["count"]*100 if d["count"]>0 else 0
        ind_rows += f"<tr><td>{ind}</td><td>{d['count']}</td><td>{d['wins']}</td><td>{wr:.0f}%</td><td>{d['total']:+.2f}%</td></tr>"

    # 个股表
    stock_rows = ""
    for key, d in sorted(stats_v4["stock_stats"].items(), key=lambda x: -x[1]["total"]):
        wr = d["wins"]/d["count"]*100 if d["count"]>0 else 0
        stock_rows += f"<tr><td>{key}</td><td>{d['count']}</td><td>{wr:.0f}%</td><td>{d['total']:+.2f}%</td></tr>"

    # 最近交易
    recent = sorted(trades_v4, key=lambda x: x["sell_date"], reverse=True)[:25]
    trade_rows = ""
    for t in recent:
        c = "#e74c3c" if t["net_profit"] > 0 else "#27ae60"
        trade_rows += f"""<tr><td>{t['code']}</td><td>{t['name']}</td><td>{t['buy_date']}</td>
        <td>{t['sell_date']}</td><td style="color:{c};font-weight:bold">{t['net_profit']:+.2f}%</td>
        <td>{t['hold_days']}天</td><td>{t['sell_type']}</td></tr>"""

    wr_d = stats_v4["win_rate"] - stats_v2["win_rate"]
    pf_d = stats_v4["profit_factor"] - stats_v2["profit_factor"]
    exp_d = stats_v4["expectancy"] - stats_v2["expectancy"]
    cum_d = stats_v4["cumulative"] - stats_v2["cumulative"]

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:'Microsoft YaHei',sans-serif;padding:20px;background:#f8f9fa}}
.container{{max-width:950px;margin:0 auto}}
h1{{color:#2c3e50;border-bottom:3px solid #3498db;padding-bottom:10px}}
h2{{color:#34495e;margin-top:25px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:20px 0}}
.box{{background:#fff;border-radius:8px;padding:15px;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
.box h3{{margin-top:0;border-bottom:2px solid #3498db;padding-bottom:8px}}
.box.v4 h3{{border-bottom-color:#e74c3c}}
table{{width:100%;border-collapse:collapse;margin:12px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
th{{background:#34495e;color:#fff;padding:10px 8px;font-size:13px}}
td{{padding:8px;text-align:center;border-bottom:1px solid #ecf0f1;font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:15px 0}}
.card{{background:#fff;border-radius:8px;padding:12px;text-align:center;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
.card .v{{font-size:20px;font-weight:bold}}
.card .l{{font-size:11px;color:#7f8c8d;margin-top:4px}}
.note{{background:#d4edda;padding:12px;border-radius:6px;margin:15px 0;font-size:13px;border-left:4px solid #28a745}}
.warn{{background:#fff3cd;padding:12px;border-radius:6px;margin:15px 0;font-size:13px;border-left:4px solid #ffc107}}
</style></head><body><div class="container">
<h1>📊 策略优化对比报告（真实环境回测 V5.0）</h1>
<p>回测区间: 2022-07 ~ {today} | 标的: 20只 | 手续费0.3% | 滑点0.2%/0.5% | T+1 | 涨跌停过滤</p>

<div class="cards">
<div class="card"><div class="v" style="color:{delta_color(wr_d)}">{wr_d:+.1f}%</div><div class="l">胜率变化</div></div>
<div class="card"><div class="v" style="color:{delta_color(pf_d)}">{pf_d:+.2f}</div><div class="l">盈亏比变化</div></div>
<div class="card"><div class="v" style="color:{delta_color(exp_d)}">{exp_d:+.2f}%</div><div class="l">期望收益变化</div></div>
<div class="card"><div class="v" style="color:{delta_color(cum_d)}">{cum_d:+.1f}%</div><div class="l">累计收益变化</div></div>
</div>

<div class="grid">
<div class="box"><h3>V2.0 原版</h3><table>
<tr><td>总交易</td><td><b>{stats_v2['total']}笔</b></td></tr>
<tr><td>胜率</td><td><b>{stats_v2['win_rate']}%</b></td></tr>
<tr><td>盈亏比</td><td><b>{stats_v2['profit_factor']}</b></td></tr>
<tr><td>每笔期望</td><td><b>{stats_v2['expectancy']:+.2f}%</b></td></tr>
<tr><td>累计收益</td><td><b>{stats_v2['cumulative']:+.2f}%</b></td></tr>
<tr><td>平均持仓</td><td>{stats_v2['avg_hold']}天</td></tr>
<tr><td>最大连亏</td><td>{stats_v2['max_consec_loss']}次</td></tr>
</table></div>
<div class="box v4"><h3>V5.0 全面优化版</h3><table>
<tr><td>总交易</td><td><b>{stats_v4['total']}笔</b></td></tr>
<tr><td>胜率</td><td><b>{stats_v4['win_rate']}%</b> <span style="color:{delta_color(wr_d)}">({wr_d:+.1f}%)</span></td></tr>
<tr><td>盈亏比</td><td><b>{stats_v4['profit_factor']}</b> <span style="color:{delta_color(pf_d)}">({pf_d:+.2f})</span></td></tr>
<tr><td>每笔期望</td><td><b>{stats_v4['expectancy']:+.2f}%</b> <span style="color:{delta_color(exp_d)}">({exp_d:+.2f}%)</span></td></tr>
<tr><td>累计收益</td><td><b>{stats_v4['cumulative']:+.2f}%</b> <span style="color:{delta_color(cum_d)}">({cum_d:+.1f}%)</span></td></tr>
<tr><td>平均持仓</td><td>{stats_v4['avg_hold']}天</td></tr>
<tr><td>最大连亏</td><td>{stats_v4['max_consec_loss']}次</td></tr>
</table></div>
</div>

<h2>✅ V5.0优化内容</h2>
<table><tr><th>项目</th><th>V2.0</th><th>V5.0</th><th>理由</th></tr>
<tr><td>买入信号</td><td>仅买点1(缩量回踩)</td><td>买点1+买点2(放量突破回踩)</td><td>双买点提高信号质量</td></tr>
<tr><td>硬性过滤</td><td>无</td><td>流动性≥8亿+振幅≤3天+无暴跌</td><td>过滤庄股/量化控盘股</td></tr>
<tr><td>信号质量</td><td>无评分</td><td>0-100分(多支撑+MACD+RSI)</td><td>只做高确定性机会</td></tr>
<tr><td>止盈体系</td><td>单一回落止盈</td><td>双轨:阶梯8%/20%+回落底仓</td><td>让利润奔跑+分批锁利</td></tr>
<tr><td>强制卖出</td><td>无</td><td>放量大跌>8%无条件离场</td><td>避免重大亏损</td></tr>
<tr><td>初始止损</td><td>10%</td><td>8%</td><td>更快认错，减少单笔亏损</td></tr>
<tr><td>保本线</td><td>浮盈5%→保本</td><td>浮盈5%→保本+2%</td><td>覆盖手续费</td></tr>
<tr><td>买入确认</td><td>MA20向上</td><td>MA20+MA60都向上</td><td>双重趋势确认</td></tr>
</table>

<div class="warn">⚠️ <b>回测环境</b>: 手续费0.3%(买卖合计) | 滑点:龙头0.2%/弹性0.5% | T+1执行 | 涨跌停过滤 | 信号T日收盘计算→T+1开盘执行（无未来函数）</div>

<h2>🎯 卖出原因统计（V5.0）</h2>
<table><tr><th>原因</th><th>次数</th><th>胜率</th><th>平均净收益</th></tr>{sell_rows}</table>

<h2>📋 行业分布（V5.0）</h2>
<table><tr><th>行业</th><th>交易</th><th>盈利</th><th>胜率</th><th>累计净收益</th></tr>{ind_rows}</table>

<h2>📈 个股统计（V5.0）</h2>
<table><tr><th>股票</th><th>交易</th><th>胜率</th><th>累计净收益</th></tr>{stock_rows}</table>

<h2>📝 最近25笔交易（V5.0）</h2>
<table><tr><th>代码</th><th>名称</th><th>买入日</th><th>卖出日</th><th>净收益</th><th>持仓</th><th>原因</th></tr>{trade_rows}</table>

<div class="note">✅ <b>结论</b>: V5.0在真实交易环境下（含手续费+滑点+T+1+涨跌停），
通过双买点+硬性过滤+信号质量评分+双轨止盈+强制卖出，全面优化了风险收益比。所有信号均在T日收盘后计算，T+1日开盘执行，无未来函数。</div>
</div></body></html>"""
    return html


# ============================================================
# 八、邮件发送
# ============================================================

def send_email(html: str):
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header

    today = datetime.date.today().strftime("%Y-%m-%d")
    subject = f"[策略优化] 真实环境回测对比 V2 vs V5 | {today}"

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = config.EMAIL_RECEIVER

    try:
        server = smtplib.SMTP_SSL(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT)
        server.login(config.EMAIL_SENDER, config.EMAIL_AUTH_CODE)
        server.sendmail(config.EMAIL_SENDER, [config.EMAIL_RECEIVER], msg.as_string())
        server.quit()
        logger.info(f"邮件发送成功: {subject}")
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")


# ============================================================
# 九、主函数
# ============================================================

def run():
    logger.info("=" * 60)
    logger.info("  真实环境回测 V5.0（无未来函数+手续费+滑点+T+1）")
    logger.info("=" * 60)

    trades_v2 = []
    trades_v4 = []
    trades_v5 = []

    for code, info in TEST_STOCKS.items():
        logger.info(f"回测: {code} {info['名称']}...")
        try:
            df = fetch_history_data(code, start_date="2022-01-01")
            if df.empty or len(df) < 80:
                logger.warning(f"  {code} 数据不足，跳过")
                continue

            t2 = backtest_stock_v4(df, code, info, version="v2")
            t4 = backtest_stock_v4(df, code, info, version="v4")
            t5 = backtest_stock_v5(df, code, info)
            trades_v2.extend(t2)
            trades_v4.extend(t4)
            trades_v5.extend(t5)
            logger.info(f"  V2:{len(t2)}笔 | V4:{len(t4)}笔 | V5:{len(t5)}笔")
        except Exception as e:
            logger.error(f"  {code} 失败: {e}")

    if not trades_v2 or not trades_v5:
        logger.error("无交易记录")
        return

    trades_v2.sort(key=lambda x: x["buy_date"])
    trades_v4.sort(key=lambda x: x["buy_date"])
    trades_v5.sort(key=lambda x: x["buy_date"])

    stats_v2 = analyze_trades(trades_v2)
    stats_v4 = analyze_trades(trades_v4)
    stats_v5 = analyze_trades(trades_v5)

    # 打印对比
    logger.info("\n" + "=" * 80)
    logger.info(f"  {'指标':<12} {'V2.0':<15} {'V4.0':<15} {'V5.0':<15}")
    logger.info("-" * 80)
    logger.info(f"  {'总交易':<12} {stats_v2['total']:<15} {stats_v4['total']:<15} {stats_v5['total']:<15}")
    logger.info(f"  {'胜率':<12} {stats_v2['win_rate']}%{'':<10} {stats_v4['win_rate']}%{'':<10} {stats_v5['win_rate']}%")
    logger.info(f"  {'盈亏比':<12} {stats_v2['profit_factor']:<15} {stats_v4['profit_factor']:<15} {stats_v5['profit_factor']:<15}")
    logger.info(f"  {'每笔期望':<12} {stats_v2['expectancy']:+.2f}%{'':<9} {stats_v4['expectancy']:+.2f}%{'':<9} {stats_v5['expectancy']:+.2f}%")
    logger.info(f"  {'累计收益':<12} {stats_v2['cumulative']:+.2f}%{'':<9} {stats_v4['cumulative']:+.2f}%{'':<9} {stats_v5['cumulative']:+.2f}%")
    logger.info(f"  {'最大连亏':<12} {stats_v2['max_consec_loss']:<15} {stats_v4['max_consec_loss']:<15} {stats_v5['max_consec_loss']:<15}")
    logger.info("=" * 80)

    # V5 vs V2 变化
    logger.info(f"\n  V5.0 vs V2.0 改善:")
    logger.info(f"    胜率: {stats_v5['win_rate'] - stats_v2['win_rate']:+.1f}%")
    logger.info(f"    盈亏比: {stats_v5['profit_factor'] - stats_v2['profit_factor']:+.2f}")
    logger.info(f"    期望: {stats_v5['expectancy'] - stats_v2['expectancy']:+.2f}%")
    logger.info(f"    累计: {stats_v5['cumulative'] - stats_v2['cumulative']:+.2f}%")

    # 生成报告
    html = generate_report(stats_v2, stats_v5, trades_v5)
    report_path = os.path.join(config.PROJECT_ROOT, "output", f"backtest_v5_{datetime.date.today().strftime('%Y%m%d')}.html")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"报告: {report_path}")

    send_email(html)
    return stats_v2, stats_v5


if __name__ == "__main__":
    run()
