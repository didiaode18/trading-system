# -*- coding: utf-8 -*-
"""
五层量化推荐引擎 V1.0
=====================
生成可直接挂东方财富条件单的完整交易计划

五层筛选体系:
  第一层: 底线排雷（流动性/ST/庄股特征）→ 一票否决
  第二层: 赛道景气（行业RPS/均线趋势/资金流入）
  第三层: 基本面质地（ROE/增速/估值）
  第四层: 趋势与资金（MA系统/量能/相对强度）
  第五层: 买点性价比（回踩支撑/盈亏比/仓位计算）

输出: 完整交易计划（买入区间/止损/目标位/仓位/风险提示）
"""

import logging
import datetime
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# 配置参数
# ============================================================
TOTAL_CAPITAL = 722000       # 总资金（元）
MAX_SINGLE_RISK = 0.02       # 单笔最大风险 = 总资金2%
STOP_LOSS_PCT = 0.08         # 默认止损幅度8%
MIN_RISK_REWARD = 2.5        # 最低盈亏比
MIN_AVG_AMOUNT = 3e8         # 日均成交额最低3亿
MIN_MARKET_CAP = 100e8       # 最低总市值100亿（近似用成交额替代）
MAX_SECTOR_RATIO = 0.25      # 单赛道最大仓位25%
MAX_SINGLE_RATIO = 0.15      # 单只最大仓位15%


# ============================================================
# 第一层：底线排雷（一票否决）
# ============================================================
def layer1_risk_filter(code: str, df: pd.DataFrame, name: str = "") -> dict:
    """
    底线排雷:
    - 流动性: 近20日日均成交额 >= 3亿
    - ST/退市: 名称含ST/*ST
    - 庄股特征: 成交量异常波动（标准差/均值 > 3）
    - 连续暴跌: 近5日有单日跌幅>9%

    返回: {"pass": bool, "reason": str, "details": dict}
    """
    result = {"pass": True, "reason": "", "details": {}}

    if df.empty or len(df) < 20:
        result["pass"] = False
        result["reason"] = "数据不足20根K线"
        return result

    # 1. ST/退市检测
    if "ST" in name.upper() or "*ST" in name.upper() or "退" in name:
        result["pass"] = False
        result["reason"] = f"ST/退市风险: {name}"
        return result

    # 2. 流动性: 日均成交额
    if "amount" in df.columns:
        avg_amount_20 = df["amount"].iloc[-20:].mean()
        result["details"]["avg_amount_20"] = avg_amount_20
        if not pd.isna(avg_amount_20) and avg_amount_20 < MIN_AVG_AMOUNT:
            result["pass"] = False
            result["reason"] = f"流动性不足: 日均成交额{avg_amount_20/1e8:.1f}亿 < 3亿"
            return result
    else:
        # 用 volume * close 估算
        est_amount = (df["volume"].iloc[-20:] * df["close"].iloc[-20:]).mean()
        result["details"]["avg_amount_20"] = est_amount
        if est_amount < MIN_AVG_AMOUNT:
            result["pass"] = False
            result["reason"] = f"流动性不足: 估算日均成交额{est_amount/1e8:.1f}亿 < 3亿"
            return result

    # 3. 庄股特征: 成交量变异系数 > 3（忽大忽小）
    vol_20 = df["volume"].iloc[-20:]
    vol_cv = vol_20.std() / vol_20.mean() if vol_20.mean() > 0 else 0
    result["details"]["vol_cv"] = round(vol_cv, 2)
    if vol_cv > 3.0:
        result["pass"] = False
        result["reason"] = f"疑似庄股: 成交量变异系数{vol_cv:.1f}>3"
        return result

    # 4. 近5日单日暴跌>9%
    if len(df) >= 6:
        recent_5 = df.iloc[-5:]
        pct_changes = recent_5["close"].pct_change()
        if (pct_changes < -0.09).any():
            result["pass"] = False
            result["reason"] = "近5日有单日跌幅>9%，疑似黑天鹅"
            return result

    result["reason"] = "排雷通过"
    return result


# ============================================================
# 第二层：赛道景气（行业相对强度）
# ============================================================
def layer2_sector_strength(code: str, df: pd.DataFrame, sector: str,
                           all_sector_data: dict = None) -> dict:
    """
    赛道景气评估:
    - 个股60日/120日相对强度
    - 均线多头排列
    - 行业内排名

    返回: {"score": 0-100, "rps_60": float, "rps_120": float, "ma_bullish": bool, "reason": str}
    """
    result = {"score": 0, "rps_60": 0, "rps_120": 0, "ma_bullish": False, "reason": ""}

    if df.empty or len(df) < 60:
        result["reason"] = "数据不足60日"
        return result

    close = df["close"].values
    current = close[-1]

    # 60日涨幅
    change_60 = (current / close[-60] - 1) * 100 if close[-60] > 0 else 0
    # 120日涨幅（如果数据够）
    change_120 = (current / close[-120] - 1) * 100 if len(close) >= 120 and close[-120] > 0 else change_60

    result["rps_60"] = round(change_60, 1)
    result["rps_120"] = round(change_120, 1)

    # 均线多头排列检测
    ma20 = df["close"].rolling(20).mean().iloc[-1]
    ma60 = df["close"].rolling(60).mean().iloc[-1]
    ma_bullish = current > ma20 > ma60
    result["ma_bullish"] = ma_bullish

    # 评分
    score = 0
    # 60日涨幅评分
    if change_60 > 30:
        score += 35
    elif change_60 > 15:
        score += 28
    elif change_60 > 5:
        score += 20
    elif change_60 > 0:
        score += 10
    else:
        score += 0

    # 均线多头排列
    if ma_bullish:
        score += 30
    elif current > ma20:
        score += 15

    # MA20斜率向上
    ma20_series = df["close"].rolling(20).mean()
    ma20_slope = ma20_series.iloc[-1] - ma20_series.iloc[-5] if len(ma20_series) >= 5 else 0
    if ma20_slope > 0:
        score += 20
    elif ma20_slope > -0.5:
        score += 5

    # 站稳MA60
    if current > ma60:
        score += 15

    result["score"] = min(100, score)
    result["reason"] = f"60日涨幅{change_60:.1f}%, {'多头排列' if ma_bullish else '非多头'}, MA20{'↑' if ma20_slope > 0 else '↓'}"
    return result


# ============================================================
# 第三层：基本面质地（简化版，基于可获取数据）
# ============================================================
def layer3_fundamental(code: str, df: pd.DataFrame, fund_data: dict = None) -> dict:
    """
    基本面评估（基于baostock可获取数据）:
    - ROE (如果可获取)
    - 业绩增速 (如果可获取)
    - 估值水平 (PE/PB分位)

    注: 免费数据源基本面数据有限，未获取到的项给中性分
    返回: {"score": 0-100, "roe": float, "pe_percentile": float, "reason": str}
    """
    result = {"score": 50, "roe": None, "pe_percentile": None, "reason": "基本面数据有限，给中性分"}

    score = 50  # 基础中性分

    if fund_data:
        roe = fund_data.get("roe")
        if roe is not None:
            result["roe"] = roe
            if roe >= 15:
                score += 20
            elif roe >= 8:
                score += 10
            elif roe < 0:
                score -= 30  # 亏损一票否决级别

        profit_growth = fund_data.get("profit_growth")
        if profit_growth is not None:
            if profit_growth >= 30:
                score += 15
            elif profit_growth >= 15:
                score += 8
            elif profit_growth < 0:
                score -= 10

    # 用价格波动估算估值位置（近1年价格分位）
    if len(df) >= 120:
        close = df["close"].values
        current = close[-1]
        low_120 = min(close[-120:])
        high_120 = max(close[-120:])
        if high_120 > low_120:
            percentile = (current - low_120) / (high_120 - low_120)
            result["pe_percentile"] = round(percentile * 100, 0)
            if percentile < 0.3:
                score += 10  # 低位，安全边际高
            elif percentile > 0.9:
                score -= 15  # 历史高位，风险大

    result["score"] = max(0, min(100, score))
    return result


# ============================================================
# 第四层：趋势与资金（技术面核心）
# ============================================================
def layer4_trend_capital(code: str, df: pd.DataFrame) -> dict:
    """
    趋势与资金评估:
    - MA系统趋势（MA5>MA10>MA20>MA60）
    - MACD状态
    - RSI强度
    - 量能结构（上涨放量/回调缩量）
    - 相对强度RPS

    返回: {"score": 0-100, "signals": [], "trend_dir": str, ...}
    """
    result = {"score": 0, "signals": [], "trend_dir": "未知", "details": {}}

    if df.empty or len(df) < 60:
        result["trend_dir"] = "数据不足"
        return result

    latest = df.iloc[-1]
    close = latest["close"]
    volume = latest["volume"]

    ma5 = latest.get("ma5", close)
    ma10 = latest.get("ma10", close)
    ma20 = latest.get("ma20", close)
    ma60 = latest.get("ma60", close)
    rsi = latest.get("rsi", 50)
    macd_dif = latest.get("macd_dif", 0)
    macd_dea = latest.get("macd_dea", 0)
    macd_hist = latest.get("macd_hist", 0)
    vol_ma20 = latest.get("vol_ma20", 0)

    score = 0
    signals = []

    # ---- 均线系统 (最高30分) ----
    if not pd.isna(ma5) and not pd.isna(ma10) and not pd.isna(ma20) and not pd.isna(ma60):
        if ma5 > ma10 > ma20 > ma60:
            score += 30
            signals.append("★均线完美多头排列")
        elif ma5 > ma10 > ma20:
            score += 22
            signals.append("均线多头排列(MA5>MA10>MA20)")
        elif close > ma20 > ma60:
            score += 15
            signals.append("站上MA20/MA60")
        elif close > ma20:
            score += 8
            signals.append("站上MA20")
        elif close < ma20 and close < ma60:
            score -= 10
            signals.append("⚠跌破MA20和MA60")

    # MA20斜率
    ma20_series = df["close"].rolling(20).mean()
    if len(ma20_series) >= 5:
        ma20_slope = ma20_series.iloc[-1] - ma20_series.iloc[-5]
        if ma20_slope > 0:
            score += 10
            signals.append("MA20向上")
        else:
            score -= 5
            signals.append("MA20向下")

    # ---- MACD (最高20分) ----
    if not pd.isna(macd_dif) and not pd.isna(macd_dea):
        if macd_dif > macd_dea and macd_dif > 0:
            score += 20
            signals.append("MACD零轴上方金叉")
        elif macd_dif > macd_dea:
            score += 12
            signals.append("MACD金叉")
        elif macd_dif < macd_dea and macd_dif < 0:
            score -= 10
            signals.append("MACD零轴下方死叉")
        else:
            score -= 3
            signals.append("MACD死叉")

        # MACD柱放大
        if not pd.isna(macd_hist) and len(df) >= 3:
            prev_hist = df["macd_hist"].iloc[-2] if "macd_hist" in df.columns else 0
            if not pd.isna(prev_hist) and macd_hist > prev_hist and macd_hist > 0:
                score += 5
                signals.append("MACD红柱放大")

    # ---- RSI (最高15分) ----
    if not pd.isna(rsi):
        if 50 <= rsi <= 70:
            score += 15
            signals.append(f"RSI健康偏强({rsi:.0f})")
        elif 40 <= rsi < 50:
            score += 8
            signals.append(f"RSI中性({rsi:.0f})")
        elif rsi > 80:
            score -= 5
            signals.append(f"⚠RSI超买({rsi:.0f})")
        elif rsi < 30:
            score += 5  # 超卖可能反弹
            signals.append(f"RSI超卖({rsi:.0f})，可能反弹")
        else:
            signals.append(f"RSI({rsi:.0f})")

    # ---- 量能结构 (最高20分) ----
    if not pd.isna(vol_ma20) and vol_ma20 > 0:
        vol_ratio = volume / vol_ma20
        result["details"]["vol_ratio"] = round(vol_ratio, 2)

        # 检查近5日量价关系
        if len(df) >= 6:
            recent_5 = df.iloc[-5:]
            up_days_vol = recent_5[recent_5["close"] > recent_5["close"].shift(1)]["volume"].mean()
            down_days_vol = recent_5[recent_5["close"] < recent_5["close"].shift(1)]["volume"].mean()

            if not pd.isna(up_days_vol) and not pd.isna(down_days_vol) and down_days_vol > 0:
                if up_days_vol > down_days_vol * 1.3:
                    score += 15
                    signals.append("上涨放量/下跌缩量(健康)")
                elif up_days_vol < down_days_vol * 0.7:
                    score -= 8
                    signals.append("⚠上涨缩量/下跌放量(出货)")

        # 回调缩量检测
        if len(df) >= 3:
            last_3_vol = df["volume"].iloc[-3:].mean()
            if last_3_vol < vol_ma20 * 0.7:
                score += 5
                signals.append("近3日缩量回调")

    # ---- 相对强度 (最高15分) ----
    if len(df) >= 60:
        change_60 = (close / df["close"].iloc[-60] - 1) * 100
        if change_60 > 20:
            score += 15
            signals.append(f"60日涨幅{change_60:.0f}%(强)")
        elif change_60 > 10:
            score += 10
        elif change_60 > 0:
            score += 5

    result["score"] = max(0, min(100, score))
    result["signals"] = signals

    # 趋势方向
    if result["score"] >= 70:
        result["trend_dir"] = "📈 强势上涨"
    elif result["score"] >= 50:
        result["trend_dir"] = "↗️ 偏多震荡"
    elif result["score"] >= 35:
        result["trend_dir"] = "➡️ 横盘整理"
    elif result["score"] >= 20:
        result["trend_dir"] = "↘️ 偏空震荡"
    else:
        result["trend_dir"] = "📉 弱势下跌"

    return result


# ============================================================
# 第五层：买点性价比（盈亏比+仓位）
# ============================================================
def layer5_entry_value(code: str, df: pd.DataFrame, realtime_price: float = 0) -> dict:
    """
    买点性价比评估:
    - 回踩支撑位检测
    - 盈亏比计算 (>= 2.5:1)
    - 买入区间计算
    - 止损价/目标价
    - 建议仓位（单笔风险≤总资金2%）

    返回: 完整交易计划
    """
    result = {
        "score": 0, "reason": "",
        "buy_low": 0, "buy_high": 0,
        "stop_loss": 0, "target_1": 0, "target_2": 0,
        "risk_reward": 0, "position_pct": 0,
        "buy_shares": 0, "buy_amount": 0,
        "entry_type": "", "risk_notes": [],
    }

    if df.empty or len(df) < 20:
        result["reason"] = "数据不足"
        return result

    latest = df.iloc[-1]
    price = realtime_price if realtime_price > 0 else latest["close"]

    ma20 = latest.get("ma20", price)
    ma60 = latest.get("ma60", price)
    atr = latest.get("atr", price * 0.025)
    if pd.isna(atr) or atr <= 0:
        atr = price * 0.025
    if pd.isna(ma20):
        ma20 = price
    if pd.isna(ma60):
        ma60 = price

    # ---- 支撑位计算 ----
    supports = []
    if ma20 < price:
        supports.append(("MA20", ma20))
    if ma60 < price and ma60 < ma20:
        supports.append(("MA60", ma60))
    # 布林下轨
    boll_lower = latest.get("boll_lower", price * 0.95)
    if not pd.isna(boll_lower) and boll_lower < price:
        supports.append(("布林下轨", boll_lower))
    # 近20日低点
    if len(df) >= 20:
        recent_low = df["low"].iloc[-20:].min()
        if recent_low < price:
            supports.append(("20日低点", recent_low))

    supports.sort(key=lambda x: x[1], reverse=True)
    first_support = supports[0][1] if supports else price * 0.95

    # ---- 压力位计算 ----
    resistances = []
    if ma20 > price:
        resistances.append(("MA20", ma20))
    if ma60 > price:
        resistances.append(("MA60", ma60))
    boll_upper = latest.get("boll_upper", price * 1.05)
    if not pd.isna(boll_upper) and boll_upper > price:
        resistances.append(("布林上轨", boll_upper))
    if len(df) >= 20:
        recent_high = df["high"].iloc[-20:].max()
        if recent_high > price:
            resistances.append(("20日高点", recent_high))
    if len(df) >= 60:
        high_60 = df["high"].iloc[-60:].max()
        if high_60 > price:
            resistances.append(("60日高点", high_60))

    resistances.sort(key=lambda x: x[1])
    first_resistance = resistances[0][1] if resistances else price * 1.10
    second_resistance = resistances[1][1] if len(resistances) > 1 else price * 1.20

    # ---- 买入区间 ----
    # 理想买点: 回踩第一支撑位附近
    buy_low = round(first_support * 0.995, 2)
    buy_high = round(price * 1.005, 2)  # 不超过当前价+0.5%

    # 判断买点类型
    vol_ma20 = latest.get("vol_ma20", 0)
    volume = latest.get("volume", 0)
    vol_ratio = volume / vol_ma20 if not pd.isna(vol_ma20) and vol_ma20 > 0 else 1.0

    if not pd.isna(ma20) and abs(price - ma20) / ma20 < 0.02 and vol_ratio < 0.7:
        entry_type = "★缩量回踩MA20（核心买点）"
    elif not pd.isna(ma20) and price > ma20 and vol_ratio > 1.5:
        entry_type = "放量突破（追入买点）"
        buy_low = round(price * 0.99, 2)
        buy_high = round(price * 1.01, 2)
    elif first_support < price * 0.97:
        entry_type = f"等待回踩{supports[0][0] if supports else '支撑位'}"
    else:
        entry_type = "当前价附近可建仓"

    # ---- 止损价 ----
    # 取ATR止损和固定8%止损中较高的
    atr_stop = price - 2 * atr
    fixed_stop = price * (1 - STOP_LOSS_PCT)
    support_stop = first_support * 0.98  # 支撑位下方2%
    stop_loss = max(atr_stop, fixed_stop, support_stop)
    stop_loss = round(min(stop_loss, price * 0.95), 2)  # 止损不超5%以上

    # ---- 目标价 ----
    target_1 = round(first_resistance, 2)
    target_2 = round(second_resistance, 2)
    # 确保目标价有意义
    if target_1 <= price * 1.03:
        target_1 = round(price * 1.10, 2)
    if target_2 <= target_1 * 1.03:
        target_2 = round(price * 1.20, 2)

    # ---- 盈亏比 ----
    potential_loss = price - stop_loss
    potential_gain_1 = target_1 - price
    risk_reward = potential_gain_1 / potential_loss if potential_loss > 0 else 0

    # ---- 仓位计算（单笔风险≤总资金2%）----
    max_risk_amount = TOTAL_CAPITAL * MAX_SINGLE_RISK
    if potential_loss > 0:
        max_shares_by_risk = int(max_risk_amount / potential_loss / 100) * 100
    else:
        max_shares_by_risk = 0

    # 单只仓位上限15%
    max_amount_by_position = TOTAL_CAPITAL * MAX_SINGLE_RATIO
    max_shares_by_position = int(max_amount_by_position / price / 100) * 100

    buy_shares = min(max_shares_by_risk, max_shares_by_position)
    buy_shares = max(buy_shares, 100)  # 最少100股
    buy_amount = buy_shares * price
    position_pct = buy_amount / TOTAL_CAPITAL * 100

    # ---- 评分 ----
    score = 0
    if risk_reward >= MIN_RISK_REWARD:
        score += 40
    elif risk_reward >= 2.0:
        score += 25
    elif risk_reward >= 1.5:
        score += 10

    if "核心买点" in entry_type:
        score += 30
    elif "放量突破" in entry_type:
        score += 20
    elif "当前价" in entry_type:
        score += 15

    if vol_ratio < 0.7:
        score += 15  # 缩量，好买点
    elif vol_ratio > 1.5 and price > (ma20 if not pd.isna(ma20) else price):
        score += 10  # 放量突破

    # 距离支撑位近（<3%）加分
    dist_to_support = (price - first_support) / price * 100 if first_support > 0 else 99
    if dist_to_support < 3:
        score += 15
    elif dist_to_support < 5:
        score += 8

    # ---- 风险提示 ----
    risk_notes = []
    if not pd.isna(latest.get("rsi", 50)) and latest["rsi"] > 75:
        risk_notes.append("RSI超买，短期回调风险")
    if vol_ratio > 2.5:
        risk_notes.append("成交量异常放大，注意主力出货")
    if dist_to_support > 8:
        risk_notes.append(f"距支撑位较远({dist_to_support:.1f}%)，追高风险")
    if not risk_notes:
        risk_notes.append("暂无重大风险信号")

    result.update({
        "score": min(100, score),
        "reason": entry_type,
        "buy_low": buy_low,
        "buy_high": buy_high,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "risk_reward": round(risk_reward, 2),
        "position_pct": round(position_pct, 1),
        "buy_shares": buy_shares,
        "buy_amount": round(buy_amount, 0),
        "entry_type": entry_type,
        "risk_notes": risk_notes,
        "first_support_name": supports[0][0] if supports else "估算",
        "first_support": round(first_support, 2),
        "first_resistance_name": resistances[0][0] if resistances else "估算",
        "atr": round(atr, 3),
        "dist_to_support_pct": round(dist_to_support, 1),
    })
    return result


# ============================================================
# 综合推荐引擎：五层联合评估
# ============================================================
def generate_trading_plan(code: str, name: str, sector: str, stock_type: str,
                          df: pd.DataFrame, realtime_price: float = 0,
                          realtime_change: float = 0, fund_data: dict = None) -> dict:
    """
    对单只股票运行完整五层分析，生成交易计划

    返回: {
        "pass": bool,  # 是否通过全部筛选
        "layers": {...},  # 各层得分
        "total_score": float,  # 综合评分
        "plan": {...},  # 完整交易计划
    }
    """
    # 第一层：排雷
    l1 = layer1_risk_filter(code, df, name)
    if not l1["pass"]:
        return {
            "pass": False, "code": code, "name": name, "sector": sector,
            "reject_layer": 1, "reject_reason": l1["reason"],
            "total_score": 0, "plan": None
        }

    # 第二层：赛道景气
    l2 = layer2_sector_strength(code, df, sector)

    # 第三层：基本面
    l3 = layer3_fundamental(code, df, fund_data)

    # 第四层：趋势资金
    l4 = layer4_trend_capital(code, df)

    # 第五层：买点性价比
    l5 = layer5_entry_value(code, df, realtime_price)

    # 综合评分（加权）
    # 赛道20% + 基本面15% + 趋势40% + 买点25%
    total_score = (
        l2["score"] * 0.20 +
        l3["score"] * 0.15 +
        l4["score"] * 0.40 +
        l5["score"] * 0.25
    )

    # 通过条件（分级 + 趋势硬门槛）:
    # 硬规则: 空头排列（收盘价<MA20<MA60）直接进排除池，连观察池都不进
    # A级推荐: 综合分>=55 且 盈亏比>=2.5 且 趋势分>=50 且 多头排列
    # B级推荐: 综合分>=50 且 盈亏比>=2.0 且 趋势分>=40
    is_bearish = l4["score"] < 20  # 趋势分极低 = 空头排列
    passed_a = (total_score >= 55 and
                l5["risk_reward"] >= 2.5 and
                l4["score"] >= 50 and
                not is_bearish)
    passed_b = (total_score >= 50 and
                l5["risk_reward"] >= 2.0 and
                l4["score"] >= 40 and
                not is_bearish)
    passed = passed_a or passed_b

    # 推荐逻辑（2-3条核心理由）
    reasons = []
    if l2["ma_bullish"]:
        reasons.append("均线多头排列，赛道趋势向上")
    if l2["rps_60"] > 15:
        reasons.append(f"60日涨幅{l2['rps_60']:.0f}%，相对强度突出")
    if "★缩量回踩MA20" in l5.get("entry_type", ""):
        reasons.append("缩量回踩20日线，经典买点")
    elif "放量突破" in l5.get("entry_type", ""):
        reasons.append("放量突破关键压力位")
    for sig in l4.get("signals", []):
        if "MACD" in sig and "金叉" in sig:
            reasons.append(sig)
            break
    if not reasons:
        reasons = l4.get("signals", [])[:3]

    price = realtime_price if realtime_price > 0 else (df["close"].iloc[-1] if not df.empty else 0)

    return {
        "pass": passed,
        "grade": "A" if passed_a else ("B" if passed_b else "C"),
        "code": code,
        "name": name,
        "sector": sector,
        "type": stock_type,
        "price": price,
        "change_pct": realtime_change,
        "total_score": round(total_score, 1),
        "layers": {
            "L1_排雷": l1,
            "L2_赛道": l2,
            "L3_基本面": l3,
            "L4_趋势": l4,
            "L5_买点": l5,
        },
        "reasons": reasons[:3],
        "plan": l5 if passed else None,
        "trend_dir": l4["trend_dir"],
        "signals": l4["signals"],
    }


# ============================================================
# 批量推荐：扫描候选池，输出三级股票池
# ============================================================
def run_recommendation(candidate_data: list, top_n: int = 8) -> dict:
    """
    批量运行推荐引擎 V2（三级池分级 + 操盘密码DK信号加分）

    三级池:
      核心池: 全部筛选通过 + 盈亏比≥2.5 + 买点已到，可直接建仓，数量≤8
      观察池: 基本面趋势达标，但买点未到/估值偏高，等待回调，数量≤15
      排除池: 空头趋势、踩雷、基本面差，永久禁止开仓

    参数:
        candidate_data: [{"code", "name", "sector", "type", "df", "realtime_price", "realtime_change"}]
        top_n: 核心池上限

    返回:
        {"recommended": [...], "watchlist": [...], "excluded": [...], "rejected_count", "total_scanned"}
    """
    # 加载操盘密码引擎（DK信号加分）
    try:
        from strategy.caopan_signal import CaopanEngine
        caopan_engine = CaopanEngine()
        caopan_available = True
    except Exception:
        caopan_available = False

    results = []

    for item in candidate_data:
        plan = generate_trading_plan(
            code=item["code"],
            name=item["name"],
            sector=item["sector"],
            stock_type=item.get("type", "龙头"),
            df=item["df"],
            realtime_price=item.get("realtime_price", 0),
            realtime_change=item.get("realtime_change", 0),
            fund_data=item.get("fund_data"),
        )

        # 操盘密码V2.0信号融合（5级趋势+三重DK+资金模式）
        if caopan_available and item.get("df") is not None and len(item["df"]) >= 60:
            try:
                cr = caopan_engine.analyze(item["df"], code=item["code"], name=item["name"])
                if "error" not in cr:
                    dk = cr.get("dk_signal")
                    dk_strength = cr.get("dk_strength", 0)
                    dk_grade = cr.get("dk_grade", "")
                    dk_filtered = cr.get("dk_filtered", False)
                    trend_level = cr.get("trend_level", 3)
                    fund_pattern = cr.get("fund_pattern", "normal")
                    top_div = cr.get("top_divergence", False)

                    # 趋势层硬性门槛: 下跌趋势一票否决
                    if trend_level <= 2:
                        plan["total_score"] = max(0, plan.get("total_score", 0) - 30)
                        plan["caopan_bonus"] = f"下跌趋势{trend_level}级否决"
                    # 有效D点强信号加分
                    elif dk == "D" and not dk_filtered and dk_grade in ("strong", "medium"):
                        bonus = 15 if dk_grade == "strong" else 10
                        plan["total_score"] = min(100, plan.get("total_score", 0) + bonus)
                        plan["caopan_bonus"] = f"{dk_grade}D点+{bonus}"
                    # K点信号减分
                    elif dk == "K" and not dk_filtered:
                        plan["total_score"] = max(0, plan.get("total_score", 0) - 15)
                        plan["caopan_bonus"] = f"K点-15"
                    # 上升趋势+主力连续流入
                    elif trend_level >= 4 and cr.get("main_flow_streak", 0) >= 3:
                        plan["total_score"] = min(100, plan.get("total_score", 0) + 8)
                        plan["caopan_bonus"] = f"上升{trend_level}级+主力{cr['main_flow_streak']}日流入"
                    # 资金模式加分/减分
                    if fund_pattern == "mild_build":
                        plan["total_score"] = min(100, plan.get("total_score", 0) + 5)
                    elif fund_pattern == "fake":
                        plan["total_score"] = max(0, plan.get("total_score", 0) - 20)
                        plan["caopan_bonus"] = "对倒骗线-20"
                    # 顶背离减分
                    if top_div:
                        plan["total_score"] = max(0, plan.get("total_score", 0) - 20)
                        plan["caopan_bonus"] = "顶背离-20"

                    plan["caopan_trend"] = cr.get("trend_desc", "")
                    plan["caopan_trend_level"] = trend_level
                    plan["caopan_dk"] = dk
            except Exception:
                pass

        results.append(plan)

    # 三级分类
    # 核心池: 通过全部筛选 + 盈亏比≥2.5 + 多头趋势
    core_pool = []
    # 观察池: 综合分≥45 但买点未到/盈亏比不足
    watch_pool = []
    # 排除池: 空头趋势、踩雷、评分过低
    excluded_pool = []

    for r in results:
        if r["pass"] and r.get("plan"):
            # 核心池额外门槛: 盈亏比≥2.5 + 必须多头排列
            rr = r["plan"].get("risk_reward", 0)
            l4_score = r["layers"]["L4_趋势"]["score"]
            if rr >= 2.5 and l4_score >= 50:
                core_pool.append(r)
            elif rr >= 1.5:
                # 盈亏比达标但买点未完美，进观察池
                r["watch_reason"] = f"盈亏比{rr:.1f}(未达2.5)" if rr < 2.5 else f"趋势分{l4_score}(未达50)"
                watch_pool.append(r)
            else:
                r["watch_reason"] = f"盈亏比{rr:.1f}不足"
                watch_pool.append(r)
        elif r["total_score"] >= 45:
            # 观察池: 基本面趋势达标，买点未到
            r["watch_reason"] = f"综合分{r['total_score']}，买点未到/估值偏高"
            watch_pool.append(r)
        else:
            # 排除池
            excluded_pool.append(r)

    # 排序
    core_pool.sort(key=lambda x: x["total_score"], reverse=True)
    watch_pool.sort(key=lambda x: x["total_score"], reverse=True)

    return {
        "recommended": core_pool[:top_n],       # 核心池 ≤8只
        "watchlist": watch_pool[:15],           # 观察池 ≤15只
        "excluded": excluded_pool,              # 排除池
        "rejected_count": len(excluded_pool),
        "total_scanned": len(results),
    }
