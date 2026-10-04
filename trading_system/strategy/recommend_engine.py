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
import json
# FIX: 清理死代码无用 import（datetime/numpy 均 grep 确认本文件零使用）
import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# ============================================================
# 配置参数
# ============================================================
# FIX: 修复TOTAL_CAPITAL硬编码与config不一致导致仓位计算错误
TOTAL_CAPITAL = getattr(config, 'TOTAL_CAPITAL', 722000)   # 从 config 读取
MAX_SINGLE_RISK = getattr(config, 'MAX_SINGLE_RISK', 0.02)  # 单笔最大风险
STOP_LOSS_PCT = getattr(config, 'INITIAL_STOP_LOSS_PCT', getattr(config, 'STOP_LOSS_PCT', 0.08))
MIN_RISK_REWARD = getattr(config, 'MIN_RISK_REWARD_RATIO', getattr(config, 'MIN_RISK_REWARD', 2.0))
MIN_AVG_AMOUNT = 3e8         # 日均成交额最低3亿
MIN_MARKET_CAP = 100e8       # 最低总市值100亿（近似用成交额替代）
MAX_SECTOR_RATIO = 0.25      # 单赛道最大仓位25%
# FIX P1(2026-08-07): 单只仓位上限收口config（原硬编码15%与scheduler/报告/kelly口径不一）
MAX_SINGLE_RATIO = getattr(config, 'MAX_SINGLE_STOCK_RATIO', 0.15)


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
    # V9.0(P0-3): 改为20日相对强弱，消除与L2赛道层60日涨幅的重复计算
    if len(df) >= 20:
        change_20 = (close / df["close"].iloc[-20] - 1) * 100
        if change_20 > 10:
            score += 15
            signals.append(f"20日涨幅{change_20:.0f}%(强势)")
        elif change_20 > 5:
            score += 10
            signals.append(f"20日涨幅{change_20:.0f}%")
        elif change_20 > 0:
            score += 5
        elif change_20 > -3:
            signals.append(f"20日微跌{change_20:.0f}%")

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
    # P2优化: 买点区间从0.5%扩展到2%，避免过窄导致无法成交
    buy_low = round(first_support * 0.995, 2)
    buy_high = round(price * 1.02, 2)  # 不超过当前价+2%（给追入留合理空间）

    # 判断买点类型（V2增强: 回踩不破MA20确认 + 放量突破连续2日站稳）
    vol_ma20 = latest.get("vol_ma20", 0)
    volume = latest.get("volume", 0)
    vol_ratio = volume / vol_ma20 if not pd.isna(vol_ma20) and vol_ma20 > 0 else 1.0

    # 缩量回踩MA20买点: 增加"回踩不破MA20"确认
    # 条件: 当日最低 ≤ MA20×1.01 且 收盘 > MA20 且 缩量
    day_low = latest.get("low", price)
    if not pd.isna(ma20) and ma20 > 0:
        touch_ma20 = day_low <= ma20 * 1.01  # 最低触及MA20附近
        hold_ma20 = price > ma20              # 收盘站稳MA20
        shrink_vol = vol_ratio < 0.7          # 缩量
        if touch_ma20 and hold_ma20 and shrink_vol:
            entry_type = "★缩量回踩MA20不破（核心买点）"
        elif abs(price - ma20) / ma20 < 0.02 and vol_ratio < 0.7:
            entry_type = "缩量接近MA20（观察买点）"
        else:
            entry_type = None
    else:
        entry_type = None

    # 放量突破买点: 突破后需连续2日站稳突破位（避免假突破）
    if entry_type is None and not pd.isna(ma20) and price > ma20 and vol_ratio > 1.5:
        # 检查前一日是否也站稳突破位
        if len(df) >= 2:
            prev_close = df["close"].iloc[-2]
            prev_ma20 = df["close"].rolling(20).mean().iloc[-2] if len(df) >= 21 else ma20
            if not pd.isna(prev_ma20) and prev_close > prev_ma20:
                entry_type = "放量突破连续2日站稳（追入买点）"
                buy_low = round(price * 0.99, 2)
                buy_high = round(price * 1.01, 2)
            else:
                entry_type = "放量突破(待确认站稳)"
                buy_low = round(price * 0.99, 2)
                buy_high = round(price * 1.01, 2)
        else:
            entry_type = "放量突破（追入买点）"
            buy_low = round(price * 0.99, 2)
            buy_high = round(price * 1.01, 2)

    if entry_type is None:
        if first_support < price * 0.97:
            entry_type = f"等待回踩{supports[0][0] if supports else '支撑位'}"
        else:
            entry_type = "当前价附近可建仓"

    # ---- 止损价 ----
    # P2优化: 止损与config统一，不再硬编码5%上限
    # FIX P0(2026-08-07): 原实现将fixed_stop放进max()，止损距离恒≤10%，
    # "给ATR止损留余量(至12%)"从未生效且距离限制行为死代码。
    # 改为双向钳制: ATR/支撑止损取较高者作技术止损，
    # 距离上限=STOP_LOSS_PCT+2%(不无限远)，距离下限=STOP_LOSS_PCT(不过度收紧防频繁止损)
    atr_stop = price - 2 * atr
    support_stop = first_support * 0.98  # 支撑位下方2%
    technical_stop = max(atr_stop, support_stop)
    stop_floor = price * (1 - STOP_LOSS_PCT - 0.02)   # 距离上限12% → 止损价下限
    stop_ceiling = price * (1 - STOP_LOSS_PCT)        # 距离下限10% → 止损价上限
    stop_loss = round(min(max(technical_stop, stop_floor), stop_ceiling), 2)

    # ---- 目标价 ----
    target_1 = round(first_resistance, 2)
    target_2 = round(second_resistance, 2)
    # 确保目标价有意义
    if target_1 <= price * 1.03:
        target_1 = round(price * 1.10, 2)
    if target_2 <= target_1 * 1.03:
        target_2 = round(price * 1.20, 2)

    # ---- 盈亏比（硬门槛: <2.0直接不推荐，标注风险等级） ----
    potential_loss = price - stop_loss
    potential_gain_1 = target_1 - price
    risk_reward = potential_gain_1 / potential_loss if potential_loss > 0 else 0

    # 风险等级标注
    if risk_reward >= 2.5:
        risk_level = "低风险(盈亏比≥2.5)"
    elif risk_reward >= 2.0:
        risk_level = "中风险(盈亏比2.0~2.5)"
    else:
        risk_level = "高风险(盈亏比<2.0，不推荐)"

    # ---- 仓位计算（单笔风险≤总资金2%）----
    max_risk_amount = TOTAL_CAPITAL * MAX_SINGLE_RISK
    if potential_loss > 0:
        max_shares_by_risk = int(max_risk_amount / potential_loss / 100) * 100
    else:
        max_shares_by_risk = 0

    # 单只仓位上限（config.MAX_SINGLE_STOCK_RATIO统一配置）
    max_amount_by_position = TOTAL_CAPITAL * MAX_SINGLE_RATIO
    max_shares_by_position = int(max_amount_by_position / price / 100) * 100

    buy_shares = min(max_shares_by_risk, max_shares_by_position)
    buy_shares = max(buy_shares, 100)  # 最少100股
    buy_amount = buy_shares * price
    position_pct = buy_amount / TOTAL_CAPITAL * 100

    # ---- 评分 ----
    score = 0
    if risk_reward >= 2.5:
        score += 40
    elif risk_reward >= MIN_RISK_REWARD:  # 2.0
        score += 28
    elif risk_reward >= 1.5:
        score += 10

    if "★缩量回踩MA20不破" in entry_type:
        score += 30
    elif "缩量回踩MA20" in entry_type or "缩量接近MA20" in entry_type:
        score += 22
    elif "放量突破连续2日站稳" in entry_type:
        score += 25
    elif "放量突破" in entry_type:
        score += 18
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
    # 修复BUG-055: 统一使用get避免KeyError
    rsi_val = latest.get("rsi", 50)
    if not pd.isna(rsi_val) and rsi_val > 75:
        risk_notes.append("RSI超买，短期回调风险")
    if vol_ratio > 2.5:
        risk_notes.append("成交量异常放大，注意主力出货")
    if dist_to_support > 8:
        risk_notes.append(f"距支撑位较远({dist_to_support:.1f}%)，追高风险")
    if not risk_notes:
        risk_notes.append("暂无重大风险信号")

    # ---- V9.0(P0-2): 动量衰减惩罚 ----
    # 检测短期涨幅是否过度集中在近期（追高信号）
    momentum_decay_penalty = 0
    if len(df) >= 20:
        close_arr = df["close"].values
        chg_5d = (close_arr[-1] / close_arr[-6] - 1) * 100 if len(close_arr) >= 6 else 0
        chg_20d = (close_arr[-1] / close_arr[-21] - 1) * 100 if len(close_arr) >= 21 else 0
        if abs(chg_20d) > 3:
            recent_ratio = chg_5d / chg_20d
            if chg_20d > 0 and recent_ratio > 0.6 and chg_5d > 5:
                momentum_decay_penalty = 10
            elif chg_20d > 0 and recent_ratio > 0.45 and chg_5d > 3:
                momentum_decay_penalty = 5

    result.update({
        "score": min(100, score),
        "reason": entry_type,
        "buy_low": buy_low,
        "buy_high": buy_high,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "risk_reward": round(risk_reward, 2),
        "risk_level": risk_level,
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
        "momentum_decay_penalty": momentum_decay_penalty,
    })
    return result


# ============================================================
# 综合推荐引擎：五层联合评估
# ============================================================
def generate_trading_plan(code: str, name: str, sector: str, stock_type: str,
                          df: pd.DataFrame, realtime_price: float = 0,
                          realtime_change: float = 0, fund_data: dict = None,
                          _playbook=None) -> dict:
    """
    对单只股票运行完整五层分析，生成交易计划

    参数:
        _playbook: V10 RegimePlaybook实例，提供regime-aware权重和门槛。None时使用默认硬编码权重。

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

    # V10: 综合评分（regime-aware加权）
    # 有playbook时用regime动态权重，无playbook时保持原有硬编码权重（向后兼容）
    if _playbook is not None:
        _pw = _playbook.score_weights()
        _w_l2 = _pw.get("L2_赛道", 0.25)
        _w_l3 = _pw.get("L3_基本面", 0.15)
        _w_l4 = _pw.get("L4_趋势", 0.35)
        _w_l5 = _pw.get("L5_买点", 0.15)
        _w_md = _pw.get("momentum_decay", 0.10)
    else:
        _w_l2, _w_l3, _w_l4, _w_l5, _w_md = 0.25, 0.15, 0.35, 0.15, 0.10

    total_score = (
        l2["score"] * _w_l2 +
        l3["score"] * _w_l3 +
        l4["score"] * _w_l4 +
        l5["score"] * _w_l5 +
        (100 - l5.get("momentum_decay_penalty", 0) * 10) * _w_md
    )

    # V10: 通过条件（regime-aware门槛）
    # 有playbook时用regime动态门槛，无playbook时保持原有硬编码门槛
    if _playbook is not None:
        _pass_threshold = _playbook.pass_threshold()
        _core_threshold = _playbook.core_pool_threshold()
    else:
        _pass_threshold = 50
        _core_threshold = 55

    is_bearish = l4["score"] < 20  # 趋势分极低 = 空头排列
    passed_a = (total_score >= _core_threshold and
                l5["risk_reward"] >= 2.5 and
                l4["score"] >= 50 and
                not is_bearish)
    passed_b = (total_score >= _pass_threshold and
                l5["risk_reward"] >= MIN_RISK_REWARD and  # 2.0
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
        "momentum_decay": l5.get("momentum_decay_penalty", 0),
    }


# ============================================================
# 批量推荐：扫描候选池，输出三级股票池
# ============================================================
def run_recommendation(candidate_data: list, top_n: int = 8, held_sectors: list = None,
                       regime_result: dict = None) -> dict:
    """
    批量运行推荐引擎 V3（regime-aware + 三级池分级 + 操盘密码DK信号加分）

    三级池:
      核心池: 全部筛选通过 + 盈亏比≥2.5 + 买点已到，可直接建仓，数量≤8
      观察池: 基本面趋势达标，但买点未到/估值偏高，等待回调，数量≤15
      排除池: 空头趋势、踩雷、基本面差，永久禁止开仓

    参数:
        candidate_data: [{"code", "name", "sector", "type", "df", "realtime_price", "realtime_change", "fund_data"}]
        top_n: 核心池上限
        held_sectors: 当前持仓赛道列表，同赛道推荐扣分（避免集中度风险）
        regime_result: V10 regime检测结果，用于动态调整选股权重/门槛/板块偏好

    返回:
        {"recommended": [...], "watchlist": [...], "excluded": [...], "rejected_count", "total_scanned"}
    """
    # V10: 加载RegimePlaybook（regime-aware选股）
    _playbook = None
    try:
        from strategy.regime_playbook import RegimePlaybook
        _playbook = RegimePlaybook(regime_result)
        logger.info(f"[推荐引擎] V10 regime={_playbook.state}, 门槛={_playbook.pass_threshold()}分, "
                    f"模式={_playbook.action_mode_cn()}")
    except Exception as _e:
        logger.debug(f"[推荐引擎] V10 playbook加载失败，使用默认权重: {_e}")
    # 加载操盘密码引擎（DK信号加分）
    try:
        from strategy.caopan_signal import CaopanEngine
        caopan_engine = CaopanEngine()
        caopan_available = True
    except Exception:
        caopan_available = False

    # V9.1(P0-2): 板块景气加成 —— 读取sector_rotation_cache，强势板块加分/弱势减分
    _sector_boost = {}  # {sector_name: (boost_value, status_text)}
    try:
        import json as _json
        _rot_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'sector_rotation_cache.json'
        )
        if os.path.exists(_rot_path):
            with open(_rot_path, 'r', encoding='utf-8') as _rf:
                _rot_data = _json.load(_rf)
            # 校验新鲜度（24小时内有效）
            _rot_updated = _rot_data.get('updated', '')
            _rot_valid = True
            if _rot_updated:
                try:
                    import datetime as _dt
                    _rot_ts = _dt.datetime.fromisoformat(str(_rot_updated))
                    if (_dt.datetime.now() - _rot_ts).total_seconds() > 24 * 3600:
                        _rot_valid = False
                except Exception:
                    pass
            if _rot_valid:
                _strong = set(_rot_data.get('strong', []))
                _weak = set(_rot_data.get('weak', []))
                _scores = _rot_data.get('sector_scores', {})
                # 构建板块加成映射
                for _sec in _strong:
                    _sc = _scores.get(_sec, 60)
                    _sector_boost[_sec] = (+8, f'强势+{_sc:.0f}分')
                for _sec in _weak:
                    _sc = _scores.get(_sec, 30)
                    _sector_boost[_sec] = (-8, f'弱势{_sc:.0f}分')
                # 中间板块（非strong非weak但评分较高）也给小幅加成
                for _sec, _sc in _scores.items():
                    if _sec not in _sector_boost:
                        if _sc >= 70:
                            _sector_boost[_sec] = (+4, f'偏强{_sc:.0f}分')
                        elif _sc <= 35:
                            _sector_boost[_sec] = (-4, f'偏弱{_sc:.0f}分')
    except Exception:
        pass  # 板块数据不可用时不加成

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
            _playbook=_playbook,  # V10: regime-aware权重/门槛
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

        # P0优化: 赛道去重——同赛道已有持仓→评分-15（降低集中度风险）
        if held_sectors and item.get("sector") in held_sectors:
            plan["total_score"] = max(0, plan.get("total_score", 0) - 15)
            plan["sector_overlap_penalty"] = True

        # V9.1(P0-2): 板块景气加成/减分
        _item_sector = item.get("sector", "")
        _boost_info = _sector_boost.get(_item_sector)
        if _boost_info:
            _boost_val, _boost_text = _boost_info
            plan["total_score"] = max(0, min(100, plan.get("total_score", 0) + _boost_val))
            plan["sector_boost"] = _boost_val
            plan["sector_status"] = f"{_item_sector}·{_boost_text}"
        else:
            plan["sector_status"] = _item_sector or "未知"

        # V10: BEAR模式非防御板块评分惩罚
        if _playbook is not None:
            _sec_pref = _playbook.sector_preference()
            if _sec_pref.get("mode") == "defensive":
                _penalty = _sec_pref.get("non_defensive_penalty", 1.0)
                if _penalty < 1.0 and not _playbook.is_defensive_sector(_item_sector):
                    plan["total_score"] = max(0, plan.get("total_score", 0) * _penalty)
                    plan["bear_non_defensive_penalty"] = True
                    plan["sector_status"] += "·⚠️非防御板块"

        results.append(plan)

    # 三级分类（V10: 观察池门槛随regime调整）
    _watch_threshold = 45  # 默认观察池门槛
    if _playbook is not None:
        if _playbook.state == "BEAR":
            _watch_threshold = 55  # 熊市提高观察池门槛（减少噪音）
        elif _playbook.state == "BULL":
            _watch_threshold = 40  # 牛市降低观察池门槛（更多候选）
    # 核心池: 通过全部筛选 + 盈亏比≥2.5 + 多头趋势
    core_pool = []
    # 观察池: 综合分≥_watch_threshold 但买点未到/盈亏比不足
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
        elif r["total_score"] >= _watch_threshold:
            # 观察池: 基本面趋势达标，买点未到
            r["watch_reason"] = f"综合分{r['total_score']}，买点未到/估值偏高"
            watch_pool.append(r)
        else:
            # 排除池
            excluded_pool.append(r)

    # 排序
    core_pool.sort(key=lambda x: x["total_score"], reverse=True)
    watch_pool.sort(key=lambda x: x["total_score"], reverse=True)

    result = {
        "recommended": core_pool[:top_n],       # 核心池 ≤8只
        "watchlist": watch_pool[:15],           # 观察池 ≤15只
        "excluded": excluded_pool,              # 排除池
        "rejected_count": len(excluded_pool),
        "total_scanned": len(results),
        # V10: regime信息传递给下游（报告/条件单）
        "regime_state": _playbook.state if _playbook else "RANGE",
        "regime_action_mode": _playbook.action_mode_cn() if _playbook else "",
    }

    # V9.0(P1-3): 保存推荐历史，用于信号衰减追踪
    try:
        _save_recommend_history(result)
    except Exception as _e:
        logger.debug(f"推荐历史保存失败(不影响主流程): {_e}")

    return result


# ============================================================
# 选股结果与条件单联动
# ============================================================
def generate_entry_orders(recommendation_result: dict) -> list:
    """
    根据选股推荐结果自动生成"建仓条件单"
    
    规则:
      - 只对核心池(recommended)中评分≥55的标的生成建仓单
      - 触发价 = 推荐买入价(buy_low)
      - 有效期 = 3个交易日
      - 条件单中注明选股评分、推荐理由、建议仓位、风险等级
    
    返回: [条件单 dict, ...]
    """
    orders = []
    recommended = recommendation_result.get("recommended", [])
    
    for item in recommended:
        if item.get("total_score", 0) < 55:
            continue
        plan = item.get("plan")
        if not plan:
            continue
        
        code = item.get("code", "")
        name = item.get("name", "")
        score = item.get("total_score", 0)
        grade = item.get("grade", "C")
        reasons = item.get("reasons", [])
        
        buy_price = plan.get("buy_low", 0)
        buy_shares = plan.get("buy_shares", 0)
        position_pct = plan.get("position_pct", 0)
        risk_reward = plan.get("risk_reward", 0)
        risk_level = plan.get("risk_level", "")
        entry_type = plan.get("entry_type", "")
        stop_loss = plan.get("stop_loss", 0)
        target_1 = plan.get("target_1", 0)
        
        if buy_price <= 0 or buy_shares <= 0:
            continue
        
        # 盈亏比硬门槛: <2.0不生成建仓单
        if risk_reward < 2.0:
            logger.info(f"[选股联动] {code} {name} 盈亏比{risk_reward:.1f}<2.0，跳过建仓单")
            continue
        
        reason_text = "、".join(reasons[:2]) if reasons else entry_type
        
        order = {
            "类型": f"建仓条件单({grade}级推荐)",
            "优先级": "★★建议" if grade == "A" else "★可选",
            "证券代码": code,
            "证券名称": name,
            "方向": "买入",
            "触发价": buy_price,
            "触发时间": "盘中实时",
            "数量": buy_shares,
            "有效期": "3个交易日",
            "说明": (
                f"选股评分{score:.0f}分 | {reason_text} | "
                f"建议仓位{position_pct:.1f}% | 盈亏比{risk_reward:.1f} | {risk_level} | "
                f"止损{stop_loss:.2f} 目标{target_1:.2f}"
            ),
            "选股评分": score,
            "推荐理由": reason_text,
            "风险等级": risk_level,
        }
        orders.append(order)
        logger.info(f"[选股联动] {code} {name} 建仓单: 触发价{buy_price:.2f} {buy_shares}股 评分{score:.0f}")
    
    return orders


# ============================================================
# V9.0(P1-3): 选股信号衰减追踪
# ============================================================
_RECOMMEND_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'recommend_history.json'
)


def _save_recommend_history(result: dict):
    """保存每次推荐结果到历史文件，用于T+1/T+3/T+5衰减追踪"""
    import datetime
    history_file = _RECOMMEND_HISTORY_PATH
    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception:
            history = []
    today = datetime.date.today().isoformat()
    # 记录核心池和观察池的评分快照
    snapshot = {"date": today, "core": [], "watch": []}
    for r in result.get("recommended", []):
        snapshot["core"].append({
            "code": r.get("code", ""),
            "name": r.get("name", ""),
            "score": r.get("total_score", 0),
            "grade": r.get("grade", ""),
            "momentum_decay": r.get("momentum_decay", 0),
            "layers": {
                "L2": r.get("layers", {}).get("L2_赛道", {}).get("score", 0),
                "L4": r.get("layers", {}).get("L4_趋势", {}).get("score", 0),
            },
        })
    for r in result.get("watchlist", []):
        snapshot["watch"].append({
            "code": r.get("code", ""),
            "name": r.get("name", ""),
            "score": r.get("total_score", 0),
        })
    history.append(snapshot)
    # 只保留最近30天的记录
    history = history[-30:]
    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_recommendation_decay_stats() -> dict:
    """
    读取推荐历史，统计信号衰减情况。
    返回: {
        "total_recommendations": int,
        "avg_decay_3d": float,  # 3日平均衰减
        "worst_decay_codes": [...],  # 衰减最严重的标的
    }
    """
    history_file = _RECOMMEND_HISTORY_PATH
    if not os.path.exists(history_file):
        return {"total_recommendations": 0, "avg_decay_3d": 0, "worst_decay_codes": []}
    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            history = json.load(f)
        if not history or len(history) < 2:
            return {"total_recommendations": len(history), "avg_decay_3d": 0, "worst_decay_codes": []}
        # 统计每个code的评分变化
        code_scores = {}  # {code: [(date, score), ...]}
        for snap in history:
            for r in snap.get("core", []) + snap.get("watch", []):
                code = r["code"]
                if code not in code_scores:
                    code_scores[code] = []
                code_scores[code].append((snap["date"], r["score"]))
        # 计算衰减
        decays = []
        for code, entries in code_scores.items():
            if len(entries) >= 2:
                first_score = entries[0][1]
                last_score = entries[-1][1]
                decay = last_score - first_score
                decays.append({
                    "code": code,
                    "first_score": first_score,
                    "last_score": last_score,
                    "decay": round(decay, 1),
                    "days": len(entries),
                })
        decays.sort(key=lambda x: x["decay"])
        avg_decay = sum(d["decay"] for d in decays) / len(decays) if decays else 0
        return {
            "total_recommendations": len(history),
            "unique_codes": len(code_scores),
            "avg_decay_3d": round(avg_decay, 1),
            "worst_decay_codes": decays[:5],
        }
    except Exception as e:
        logger.debug(f"推荐衰减统计失败: {e}")
        return {"total_recommendations": 0, "avg_decay_3d": 0, "worst_decay_codes": []}
