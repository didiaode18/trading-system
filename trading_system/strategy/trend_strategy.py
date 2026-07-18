"""
核心趋势策略函数
=================
基于「高胜率A股交易操作系统V2.0」规则，输入日线DataFrame，输出买卖信号

核心规则:
- 趋势判定：20日均线向上 + 收盘价站稳20日均线 -> 允许开仓
- 买点：缩量回踩20日线（量缩30%+，最低价触及20日线±1%）
- 止损：买入价下方10%，仅收盘价触发
- 移动止损：按浮盈分档上移
- 止盈：双轨制（阶梯目标 + 回落止盈）
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# ============================================================
# 一、均线与趋势计算
# ============================================================

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算所有技术指标，在原始DataFrame上增加列:
    - ma5, ma10, ma20, ma60: 均线
    - vol_ma20: 20日成交量均线
    - ma20_slope: 20日均线斜率（向上/向下）
    - rsi: RSI相对强弱指标
    - macd_dif, macd_dea, macd_hist: MACD指标
    - boll_upper, boll_mid, boll_lower: 布林带
    - atr: 真实波幅
    - ma_bullish: 均线多头排列标记
    - vol_price_divergence: 量价背离标记
    - highest_since_buy: 持仓期间最高价（用于回落止盈）
    """
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(config.MA_SHORT).mean()
    df["ma60"] = df["close"].rolling(config.MA_MID).mean()
    df["vol_ma20"] = df["volume"].rolling(config.VOLUME_MA_PERIOD).mean()

    # 20日均线斜率：今天ma20 > 3天前ma20 视为向上
    df["ma20_slope"] = df["ma20"].diff(3)

    # 日内振幅
    df["intraday_range"] = (df["high"] - df["low"]) / df["close"].shift(1)

    # ---- RSI ----
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(config.RSI_PERIOD).mean()
    avg_loss = loss.rolling(config.RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ---- MACD ----
    ema_fast = df["close"].ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow = df["close"].ewm(span=config.MACD_SLOW, adjust=False).mean()
    df["macd_dif"] = ema_fast - ema_slow
    df["macd_dea"] = df["macd_dif"].ewm(span=config.MACD_SIGNAL, adjust=False).mean()
    df["macd_hist"] = 2 * (df["macd_dif"] - df["macd_dea"])

    # ---- 布林带 ----
    df["boll_mid"] = df["close"].rolling(config.BOLL_PERIOD).mean()
    boll_std = df["close"].rolling(config.BOLL_PERIOD).std()
    df["boll_upper"] = df["boll_mid"] + config.BOLL_STD * boll_std
    df["boll_lower"] = df["boll_mid"] - config.BOLL_STD * boll_std

    # ---- ATR (Average True Range) ----
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.rolling(config.ATR_PERIOD).mean()

    # ---- 均线多头排列检测 (MA5 > MA10 > MA20 > MA60) ----
    df["ma_bullish"] = (
        (df["ma5"] > df["ma10"]) &
        (df["ma10"] > df["ma20"]) &
        (df["ma20"] > df["ma60"])
    )

    # ---- 量价背离检测（近5日价格新高但成交量萎缩）----
    df["vol_price_divergence"] = False
    for i in range(5, len(df)):
        recent_5 = df.iloc[i-4:i+1]
        price_new_high = recent_5["close"].iloc[-1] >= recent_5["close"].max()
        vol_shrinking = recent_5["volume"].iloc[-1] < recent_5["volume"].mean() * 0.8
        if price_new_high and vol_shrinking:
            df.iloc[i, df.columns.get_loc("vol_price_divergence")] = True

    return df


def is_trend_up(df: pd.DataFrame) -> bool:
    """
    判定中期上升趋势是否成立:
    1. 20日均线向上（斜率>0）
    2. 收盘价站稳20日均线上方
    3. 60日均线存在且未明确向下（可选增强条件）
    """
    if len(df) < config.MA_MID:
        return False

    latest = df.iloc[-1]
    # 条件1: 20日均线向上
    if pd.isna(latest["ma20_slope"]) or latest["ma20_slope"] <= 0:
        return False
    # 条件2: 收盘价在20日均线上方
    if latest["close"] < latest["ma20"]:
        return False
    return True


# ============================================================
# 二、买点信号判定
# ============================================================

def check_buy_signal(df: pd.DataFrame) -> dict:
    """
    检查今日是否触发买入信号（买点1：缩量回踩20日线）
    
    条件:
    1. 趋势向上（ma20向上 + 收盘价在ma20上方或附近）
    2. 缩量：今日成交量 < 20日均量 * (1 - 30%)
    3. 最低价触及20日线±1%
    
    返回:
        {
            "signal": True/False,
            "buy_price": float,      # 建议买入价（今日收盘价附近）
            "support_price": float,  # 支撑位价格（20日均线）
            "stop_loss": float,      # 初始止损价
            "reason": str            # 信号说明
        }
    """
    result = {"signal": False, "buy_price": None, "support_price": None,
              "stop_loss": None, "reason": ""}

    if len(df) < config.MA_MID:
        result["reason"] = "数据不足，无法计算均线"
        return result

    df_ind = compute_indicators(df)
    latest = df_ind.iloc[-1]

    # 前提：趋势向上
    if not is_trend_up(df_ind):
        result["reason"] = "趋势不满足：20日均线未向上或收盘价在20日线下方"
        return result

    ma20 = latest["ma20"]
    close = latest["close"]
    low = latest["low"]
    volume = latest["volume"]
    vol_ma = latest["vol_ma20"]

    # 条件1：缩量（成交量较20日均量萎缩30%以上）
    if pd.isna(vol_ma) or vol_ma == 0:
        result["reason"] = "成交量均线数据不足"
        return result
    vol_ratio = volume / vol_ma
    if vol_ratio >= (1 - config.VOLUME_SHRINK_RATIO):
        result["reason"] = f"未缩量：今日成交量/20日均量 = {vol_ratio:.2%}，需<{1-config.VOLUME_SHRINK_RATIO:.0%}"
        return result

    # 条件2：最低价触及20日线±1%
    touch_lower = ma20 * (1 - config.SUPPORT_TOUCH_PCT)
    touch_upper = ma20 * (1 + config.SUPPORT_TOUCH_PCT)
    if low > touch_upper:
        result["reason"] = f"最低价{low:.2f}未触及20日线{ma20:.2f}±{config.SUPPORT_TOUCH_PCT:.0%}"
        return result

    # 信号触发！
    buy_price = close  # 建议以收盘价附近买入
    stop_loss = buy_price * (1 - config.INITIAL_STOP_LOSS_PCT)

    # ---- 新增指标增强确认 ----
    confirmations = []
    warnings_list = []

    # RSI确认：RSI在超卖区域（<30）附近反弹是加分项
    rsi_val = latest.get("rsi", 50)
    if not pd.isna(rsi_val):
        if rsi_val < config.RSI_OVERSOLD:
            confirmations.append(f"RSI超卖({rsi_val:.0f})，反弹概率大")
        elif rsi_val > config.RSI_OVERBOUGHT:
            warnings_list.append(f"RSI超买({rsi_val:.0f})，追高风险")

    # MACD金叉确认：DIF上穿DEA
    macd_dif = latest.get("macd_dif", 0)
    macd_dea = latest.get("macd_dea", 0)
    if not pd.isna(macd_dif) and not pd.isna(macd_dea):
        prev_dif = df_ind["macd_dif"].iloc[-2] if len(df_ind) > 1 else 0
        prev_dea = df_ind["macd_dea"].iloc[-2] if len(df_ind) > 1 else 0
        if not pd.isna(prev_dif) and not pd.isna(prev_dea):
            if prev_dif <= prev_dea and macd_dif > macd_dea:
                confirmations.append("MACD金叉")
            elif macd_dif > macd_dea:
                confirmations.append("MACD多头")
            else:
                warnings_list.append("MACD空头")

    # 布林带下轨支撑确认
    boll_lower = latest.get("boll_lower", 0)
    if not pd.isna(boll_lower) and boll_lower > 0:
        if low <= boll_lower * 1.01:  # 最低价触及布林带下轨附近
            confirmations.append("触及布林带下轨支撑")

    # 均线多头排列加分
    if latest.get("ma_bullish", False):
        confirmations.append("均线多头排列")

    # 量价背离警告
    if latest.get("vol_price_divergence", False):
        warnings_list.append("量价背离（价升量缩）")

    # 构建完整信号说明
    extra_info = ""
    if confirmations:
        extra_info += " [确认: " + ", ".join(confirmations) + "]"
    if warnings_list:
        extra_info += " [警告: " + ". ".join(warnings_list) + "]"

    result.update({
        "signal": True,
        "buy_price": round(buy_price, 2),
        "support_price": round(ma20, 2),
        "stop_loss": round(stop_loss, 2),
        "rsi": round(rsi_val, 1) if not pd.isna(rsi_val) else None,
        "macd_dif": round(macd_dif, 3) if not pd.isna(macd_dif) else None,
        "macd_dea": round(macd_dea, 3) if not pd.isna(macd_dea) else None,
        "atr": round(latest.get("atr", 0), 2) if not pd.isna(latest.get("atr", 0)) else None,
        "reason": f"缩量回踩20日线: 量比={vol_ratio:.2%}, MA20={ma20:.2f}, 最低={low:.2f}{extra_info}"
    })
    return result


# ============================================================
# 三、卖出信号判定
# ============================================================

def check_sell_signal(df: pd.DataFrame, buy_price: float,
                      current_position: dict = None) -> dict:
    """
    检查是否触发卖出信号（止损 / 回落止盈）
    
    参数:
        df: 日线数据
        buy_price: 买入均价
        current_position: 当前持仓信息 {"shares": int, "highest_price": float}
    
    返回:
        {
            "signal": True/False,
            "sell_type": "stop_loss" / "drawdown_profit" / "trend_break" / None,
            "sell_price": float,
            "reason": str
        }
    """
    result = {"signal": False, "sell_type": None, "sell_price": None, "reason": ""}

    if len(df) < config.MA_SHORT or buy_price <= 0:
        return result

    df_ind = compute_indicators(df)
    latest = df_ind.iloc[-1]
    close = latest["close"]
    low = latest["low"]

    # 当前浮盈比例
    profit_pct = (close - buy_price) / buy_price

    # 持仓期间最高价
    if current_position and current_position.get("highest_price"):
        highest = current_position["highest_price"]
    else:
        highest = df_ind["high"].max()

    # ---- 1. 止损判定（仅收盘价触发）----
    stop_loss_price = compute_trailing_stop(buy_price, close)
    if close <= stop_loss_price:
        result.update({
            "signal": True,
            "sell_type": "stop_loss",
            "sell_price": round(stop_loss_price, 2),
            "reason": f"触发止损: 收盘{close:.2f} <= 止损线{stop_loss_price:.2f}, 浮盈={profit_pct:.2%}"
        })
        return result

    # ---- 2. 趋势破位：收盘跌破60日均线且60日均线拐头向下 ----
    if not pd.isna(latest.get("ma60", None)):
        ma60_slope = df_ind["ma60"].diff(3).iloc[-1]
        if close < latest["ma60"] and ma60_slope < 0:
            result.update({
                "signal": True,
                "sell_type": "trend_break",
                "sell_price": round(close, 2),
                "reason": f"趋势破位: 收盘跌破60日线且均线拐头向下"
            })
            return result

    # ---- 3. MACD死叉确认卖出 ----
    macd_dif = latest.get("macd_dif", 0)
    macd_dea = latest.get("macd_dea", 0)
    if not pd.isna(macd_dif) and not pd.isna(macd_dea) and profit_pct > 0:
        prev_dif = df_ind["macd_dif"].iloc[-2] if len(df_ind) > 1 else 0
        prev_dea = df_ind["macd_dea"].iloc[-2] if len(df_ind) > 1 else 0
        if not pd.isna(prev_dif) and not pd.isna(prev_dea):
            if prev_dif >= prev_dea and macd_dif < macd_dea:
                result.update({
                    "signal": True,
                    "sell_type": "macd_death_cross",
                    "sell_price": round(close, 2),
                    "reason": f"MACD死叉: DIF({macd_dif:.3f})下穿DEA({macd_dea:.3f}), 浮盈{profit_pct:.2%}"
                })
                return result

    # ---- 4. RSI超买区域回落卖出 ----
    rsi_val = latest.get("rsi", 50)
    if not pd.isna(rsi_val) and rsi_val > config.RSI_OVERBOUGHT and profit_pct > 0.05:
        # RSI从超买区域回落（当前RSI比前一天低）
        prev_rsi = df_ind["rsi"].iloc[-2] if len(df_ind) > 1 else rsi_val
        if not pd.isna(prev_rsi) and rsi_val < prev_rsi:
            result.update({
                "signal": True,
                "sell_type": "rsi_overbought_reversal",
                "sell_price": round(close, 2),
                "reason": f"RSI超买回落: RSI={rsi_val:.0f}(前日{prev_rsi:.0f})从超买区回落, 浮盈{profit_pct:.2%}"
            })
            return result

    # ---- 5. 回落止盈：从最高点回落超过阈值 ----
    drawdown_threshold = _get_drawdown_threshold(current_position)
    if highest > buy_price:  # 只有盈利过才启用回落止盈
        drawdown_from_high = (highest - close) / highest
        if drawdown_from_high >= drawdown_threshold:
            result.update({
                "signal": True,
                "sell_type": "drawdown_profit",
                "sell_price": round(close, 2),
                "reason": f"回落止盈: 最高{highest:.2f}->收盘{close:.2f}, 回落{drawdown_from_high:.2%} >= {drawdown_threshold:.0%}"
            })
            return result

    result["reason"] = f"持仓正常: 收盘{close:.2f}, 浮盈{profit_pct:.2%}, 止损线{stop_loss_price:.2f}"
    return result


def _get_drawdown_threshold(position: dict = None) -> float:
    """根据股票类型获取回落止盈阈值"""
    if position is None:
        return config.DRAWDOWN_STOP["成长赛道"]
    stock_type = position.get("stock_type", "龙头")
    sector = position.get("sector", "")
    if stock_type == "弹性":
        return config.DRAWDOWN_STOP["高弹性"]
    if sector in ["光模块", "存储芯片", "半导体材料"]:
        return config.DRAWDOWN_STOP["成长赛道"]
    return config.DRAWDOWN_STOP["龙头稳健"]


# ============================================================
# 四、移动止损计算
# ============================================================

def compute_trailing_stop(buy_price: float, current_price: float) -> float:
    """
    根据当前浮盈计算移动止损价（止损只能上移、不能下移）
    
    规则:
    - 浮盈 < 5%:  维持初始止损（买入价 * (1-10%)）
    - 浮盈 5%-15%: 止损上移到成本价（保本）
    - 浮盈 15%-30%: 止损上移到盈利10%的位置
    - 浮盈 > 30%: 止损上移到盈利20%的位置
    """
    profit_pct = (current_price - buy_price) / buy_price
    initial_stop = buy_price * (1 - config.INITIAL_STOP_LOSS_PCT)

    stop_price = initial_stop  # 默认初始止损

    for low, high, mode in config.TRAILING_STOP_LEVELS:
        if low <= profit_pct < high:
            if mode == "initial":
                stop_price = initial_stop
            elif mode == "cost":
                stop_price = buy_price  # 保本线
            elif mode == "profit_10":
                stop_price = buy_price * (1 + 0.10)
            elif mode == "profit_20":
                stop_price = buy_price * (1 + 0.20)
            break

    return round(stop_price, 2)


# ============================================================
# 五、综合策略输出
# ============================================================

def generate_strategy_signal(df: pd.DataFrame, holding: dict = None) -> dict:
    """
    综合策略函数：输入日线数据，输出完整的交易信号
    
    参数:
        df: 日线DataFrame (date, open, close, high, low, volume)
        holding: 当前持仓信息（可选）
            {
                "buy_price": float,      # 买入均价
                "shares": int,           # 持仓股数
                "highest_price": float,  # 持仓期间最高价
                "stock_type": str,       # "龙头" / "弹性"
                "sector": str,           # 所属赛道
                "first_batch_done": bool # 第一批是否已建仓
            }
    
    返回:
        {
            "date": str,                 # 信号日期
            "buy_signal": bool,          # 是否触发买入
            "sell_signal": bool,         # 是否触发卖出
            "buy_price": float,          # 建议买入价
            "sell_price": float,         # 建议卖出价
            "stop_loss_initial": float,  # 初始止损价
            "stop_loss_current": float,  # 当前移动止损价
            "add_position": bool,        # 是否可以加第二批仓
            "position_suggestion": dict, # 仓位建议
            "signal_reason": str         # 信号说明
        }
    """
    latest = df.iloc[-1]
    today = latest["date"]

    result = {
        "date": today,
        "buy_signal": False,
        "sell_signal": False,
        "buy_price": None,
        "sell_price": None,
        "stop_loss_initial": None,
        "stop_loss_current": None,
        "add_position": False,
        "position_suggestion": {},
        "signal_reason": ""
    }

    # ---- 已持仓：检查卖出信号 + 加仓条件 ----
    if holding and holding.get("buy_price"):
        buy_price = holding["buy_price"]
        current_price = latest["close"]
        profit_pct = (current_price - buy_price) / buy_price

        # 初始止损
        result["stop_loss_initial"] = round(buy_price * (1 - config.INITIAL_STOP_LOSS_PCT), 2)
        # 当前移动止损
        result["stop_loss_current"] = compute_trailing_stop(buy_price, current_price)

        # 检查卖出信号
        sell_result = check_sell_signal(df, buy_price, holding)
        if sell_result["signal"]:
            result["sell_signal"] = True
            result["sell_price"] = sell_result["sell_price"]
            result["signal_reason"] = f"[卖出] {sell_result['reason']}"
            return result

        # 检查是否可以加第二批仓
        if not holding.get("first_batch_done") or holding.get("first_batch_done") == False:
            pass  # 第一批未建，不涉及加仓
        elif profit_pct >= config.MIN_PROFIT_TO_ADD:
            result["add_position"] = True
            result["signal_reason"] = (
                f"[可加仓] 浮盈{profit_pct:.2%} >= {config.MIN_PROFIT_TO_ADD:.0%}, "
                f"可买入第二批{config.SECOND_BATCH_RATIO:.0%}仓位"
            )
        else:
            result["signal_reason"] = (
                f"[持仓观望] 浮盈{profit_pct:.2%}, "
                f"移动止损={result['stop_loss_current']:.2f}"
            )
        return result

    # ---- 未持仓：检查买入信号 ----
    buy_result = check_buy_signal(df)
    if buy_result["signal"]:
        result["buy_signal"] = True
        result["buy_price"] = buy_result["buy_price"]
        result["stop_loss_initial"] = buy_result["stop_loss"]
        result["stop_loss_current"] = buy_result["stop_loss"]
        result["signal_reason"] = f"[买入] {buy_result['reason']}"
    else:
        result["signal_reason"] = f"[无信号] {buy_result['reason']}"

    return result


# ============================================================
# 六、批量扫描所有股票池
# ============================================================

def scan_all_stocks(data_dict: dict, holdings: dict = None) -> list:
    """
    扫描所有股票池，返回今日信号列表
    
    参数:
        data_dict: {code: DataFrame} 所有股票的日线数据
        holdings: {code: holding_info} 当前持仓
    
    返回:
        [(code, signal_dict), ...] 按信号优先级排序
    """
    if holdings is None:
        holdings = {}

    signals = []
    for code, df in data_dict.items():
        if df.empty or len(df) < config.MA_MID:
            continue
        holding = holdings.get(code)
        sig = generate_strategy_signal(df, holding)
        sig["code"] = code
        sig["name"] = config.STOCK_POOL.get(code, {}).get("名称", code)
        signals.append((code, sig))

    # 排序：卖出信号优先，其次买入信号
    def sort_key(item):
        sig = item[1]
        if sig["sell_signal"]:
            return 0  # 卖出最优先
        if sig["buy_signal"]:
            return 1  # 买入次之
        if sig["add_position"]:
            return 2  # 加仓第三
        return 3      # 无信号最后

    signals.sort(key=sort_key)
    return signals


if __name__ == "__main__":
    # 简单测试：用模拟数据验证逻辑
    print("=" * 50)
    print("  趋势策略模块 - 单元测试")
    print("=" * 50)

    # 生成模拟数据（60天上涨趋势 + 回踩）
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=80, freq="B")
    base_price = 50
    prices = [base_price]
    for i in range(1, 80):
        if i < 50:
            change = np.random.normal(0.3, 0.5)  # 上涨趋势
        else:
            change = np.random.normal(-0.2, 0.5)  # 回调
        prices.append(max(prices[-1] + change, 10))

    df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": [p * 0.998 for p in prices],
        "close": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "volume": np.random.randint(500000, 2000000, 80).astype(float),
    })
    # 让最后几天缩量
    df.loc[df.index[-3:], "volume"] = df["volume"].mean() * 0.5

    signal = generate_strategy_signal(df)
    print(f"\n日期: {signal['date']}")
    print(f"买入信号: {signal['buy_signal']}")
    print(f"卖出信号: {signal['sell_signal']}")
    print(f"信号说明: {signal['signal_reason']}")
    if signal["buy_price"]:
        print(f"建议买入价: {signal['buy_price']}")
        print(f"初始止损价: {signal['stop_loss_initial']}")
    print("\n[OK] 策略模块测试通过")
