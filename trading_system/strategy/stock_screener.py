"""
盘后选股引擎 V2.0 — CANSLIM成长趋势选股体系
================================================
融合全球经典CANSLIM + A股实战适配

三步选股流程:
  第一步：大盘方向判断（M因子）→ 决定是否操作
  第二步：CANSLIM多因子打分 → 筛选核心股票池
  第三步：计算买点/止损/分批建仓方案

CANSLIM量化对应（A股适配版）:
  C（当期业绩）：单季度扣非净利润同比增速＞25%    → 基本面配置
  A（年度业绩）：近3年净利润复合增速＞20%          → 基本面配置
  N（新事物）：股价创近半年新高 + 突破形态         → 技术面打分
  S（供给需求）：量价配合、缩量回踩、放量突破       → 技术面打分
  L（领涨龙头）：行业内涨幅领先、RPS排名靠前       → 技术面打分
  I（机构认同）：北向资金/机构持仓                  → 基本面配置
  M（大盘方向）：大盘处于上升趋势                   → 指数趋势判断

核心买点:
  1. 缩量回踩20日均线：成交量较20日均量萎缩30%以上，价格回踩MA20不跌破
  2. 放量突破新高：成交量放大50%以上，股价创近60日新高

分批建仓:
  - 第一批 40% 试仓（买点附近）
  - 浮盈≥3% 再加第二批 60%（确认趋势）

止损策略:
  - 初始止损：买入价 × 90%（10%止损）
  - 浮盈后上移移动止损（保护利润）

使用方式:
    from strategy.stock_screener import run_stock_screener
    result = run_stock_screener(data_dict, holdings)
"""

import os
import sys
import logging
import datetime
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategy.position import calc_first_batch

logger = logging.getLogger(__name__)

# ============================================================
# 基本面数据配置（CANSLIM的C/A/I因子）
# ============================================================
# 格式: {"股票代码": {"eps_growth_q": xx, "eps_growth_3y": xx, "has_institution": bool}}
# eps_growth_q: 单季度扣非净利润同比增速(%)
# eps_growth_3y: 近3年净利润复合增速(%)
# has_institution: 是否有机构持仓/北向加仓
# 未配置的个股使用默认中性值
FUNDAMENTAL_DATA = {
    # 示例（请根据实际财报数据更新）:
    # "002371": {"eps_growth_q": 35, "eps_growth_3y": 28, "has_institution": True},
    # "603986": {"eps_growth_q": 20, "eps_growth_3y": 15, "has_institution": True},
}


# ============================================================
# 一、大盘方向判断（M因子）
# ============================================================

def check_market_direction(data_dict: dict) -> dict:
    """
    M因子：判断大盘方向，决定是否适合做多
    
    判断标准（000300沪深300）:
    - 收盘价 > MA20 > MA60 → 上升趋势（可操作）
    - 收盘价 > MA20 但 MA20 < MA60 → 震荡（谨慎操作）
    - 收盘价 < MA20 → 下降趋势（不建议买入）
    
    返回:
        {
            "market_state": "up" / "neutral" / "down",
            "can_buy": bool,
            "position_limit_ratio": float,  # 建议仓位上限
            "detail": str
        }
    """
    index_df = data_dict.get("000300")
    if index_df is None or len(index_df) < 60:
        logger.warning("[M因子] 无沪深300数据，默认中性")
        return {
            "market_state": "neutral",
            "can_buy": True,
            "position_limit_ratio": 0.5,
            "detail": "无指数数据，默认半仓操作"
        }

    latest = index_df.iloc[-1]
    close = latest["close"]
    ma20 = latest.get("ma20", 0)
    ma60 = latest.get("ma60", 0)

    if pd.isna(ma20) or pd.isna(ma60):
        return {
            "market_state": "neutral",
            "can_buy": True,
            "position_limit_ratio": 0.5,
            "detail": "均线数据不足，默认半仓操作"
        }

    # 20日涨跌幅
    change_20d = (close / index_df["close"].iloc[-20] - 1) * 100 if len(index_df) >= 20 else 0
    # MA20斜率（近5日）
    ma20_slope = (ma20 - index_df["ma20"].iloc[-6]) / index_df["ma20"].iloc[-6] * 100 if len(index_df) >= 26 else 0

    if close > ma20 > ma60 and ma20_slope > 0:
        state = "up"
        can_buy = True
        limit = 1.0
        detail = f"上升趋势（沪深300在MA20/MA60上方，MA20斜率{ma20_slope:+.2f}%）→ 满仓操作"
    elif close > ma20:
        state = "neutral"
        can_buy = True
        limit = 0.5
        detail = f"震荡偏强（沪深300在MA20上方但MA20<MA60）→ 半仓操作"
    elif close > ma60:
        state = "neutral"
        can_buy = True
        limit = 0.3
        detail = f"震荡偏弱（沪深300在MA60上方但跌破MA20）→ 轻仓操作"
    else:
        state = "down"
        can_buy = False
        limit = 0.0
        detail = f"下降趋势（沪深300跌破MA20和MA60）→ 不建议买入"

    logger.info(f"[M因子] 大盘状态: {state} | {detail}")

    return {
        "market_state": state,
        "can_buy": can_buy,
        "position_limit_ratio": limit,
        "detail": detail,
        "index_close": round(close, 2),
        "index_ma20": round(ma20, 2),
        "index_ma60": round(ma60, 2),
        "change_20d": round(change_20d, 2),
    }


# ============================================================
# 一'、个股硬性筛选（6个硬性标准，不通过直接跳过）
# ============================================================

def hard_filter(df: pd.DataFrame, code: str, market_state: str = "up") -> dict:
    """
    个股硬性筛选（支持强势/弱势两种模式）
    
    强势模式（6个硬性标准）:
    1. 趋势合格: 股价站稳MA20 + MA20向上
    2. 流动性充足: 日均成交额 >= 8亿
    3. 股性稳定: 近30日单日振幅>10%的天数 <= 3天
    4. 无放量暴跌: 近5日无单日跌幅>8%且放量
    5. 不在黑名单: 非下降通道
    6. 回调不创新低
    
    弱势模式（放宽条件，选相对强势股）:
    1. 不要求MA20向上，改为"MA20跌幅收窄"或"近5日企稳"
    2. 不要求收盘价在MA20上方，改为"距MA20偏离度最小"
    3. 保留流动性、暴跌、下降通道等底线筛选
    
    返回:
        {"pass": bool, "reason": str, "details": dict, "weak_score": float}
    """
    result = {"pass": True, "reason": "", "details": {}, "weak_score": 0}
    is_weak_market = market_state in ("down", "weak", "neutral")
    
    if len(df) < 60:
        result["pass"] = False
        result["reason"] = "数据不足60日"
        return result
    
    latest = df.iloc[-1]
    close = latest["close"]
    
    # ---- 1. 趋势判定 ----
    ma20 = latest.get("ma20", None)
    ma60 = latest.get("ma60", None)
    ma20_slope = latest.get("ma20_slope", None)
    
    if pd.isna(ma20) or pd.isna(ma20_slope):
        result["pass"] = False
        result["reason"] = "均线数据不足"
        return result
    
    if is_weak_market:
        # === 弱势行情宽松模式 ===
        weak_score = 0
        
        # 计算MA20斜率变化（跌幅是否收窄）
        if len(df) >= 25:
            ma20_slope_prev = df["ma20"].diff(3).iloc[-4] if not pd.isna(df["ma20"].diff(3).iloc[-4]) else 0
            slope_improving = ma20_slope > ma20_slope_prev  # 斜率在改善
        else:
            slope_improving = False
        
        # 近5日企稳判定：连续2日不创新低
        if len(df) >= 6:
            recent_5_low = df["low"].iloc[-5:].min()
            prev_5_low = df["low"].iloc[-10:-5].min() if len(df) >= 10 else recent_5_low
            is_stabilizing = recent_5_low >= prev_5_low * 0.98
        else:
            is_stabilizing = False
        
        # 距MA20偏离度（越小越好）
        dist_to_ma20 = (close - ma20) / ma20 if ma20 > 0 else -1
        
        # 弱势模式评分
        if close > ma20:
            weak_score += 30  # 仍在MA20上方，很强
        elif dist_to_ma20 > -0.05:
            weak_score += 20  # 距MA20不超过5%
        elif dist_to_ma20 > -0.10:
            weak_score += 10  # 距MA20不超过10%
        
        if slope_improving:
            weak_score += 20  # MA20跌幅收窄
        if ma20_slope > 0:
            weak_score += 15  # MA20仍然向上
        if is_stabilizing:
            weak_score += 20  # 近5日企稳
        
        # 近5日涨跌幅（相对强度）
        if len(df) >= 6:
            change_5d = (close / df["close"].iloc[-6] - 1) * 100
            if change_5d > 0:
                weak_score += 15
            elif change_5d > -3:
                weak_score += 8
        
        result["weak_score"] = weak_score
        
        # 弱势模式底线：不能是明确下降通道
        if not pd.isna(ma60):
            ma60_slope = df["ma60"].diff(5).iloc[-1] if len(df) >= 65 else 0
            if not pd.isna(ma60_slope) and ma20_slope < 0 and ma60_slope < 0 and close < ma60:
                # MA20/MA60全部向下且股价在MA60下方 → 明确下降通道，即使弱势也不选
                if dist_to_ma20 < -0.15:  # 偏离MA20超过15%，太弱
                    result["pass"] = False
                    result["reason"] = f"明确下降通道且偏离MA20达{dist_to_ma20:.1%}"
                    return result
        
        # 弱势模式通过条件：weak_score >= 25
        if weak_score < 25:
            result["pass"] = False
            result["reason"] = f"弱势评分{weak_score}分不足25分，相对强度太弱"
            return result
        
        result["reason"] = f"弱势模式通过(评分{weak_score})"
    else:
        # === 强势行情严格模式（原逻辑）===
        # MA20必须向上
        if ma20_slope <= 0:
            result["pass"] = False
            result["reason"] = f"MA20向下(斜率{ma20_slope:.4f})，趋势不合格"
            return result
        
        # 收盘价必须在MA20上方
        if close < ma20:
            result["pass"] = False
            result["reason"] = f"收盘价{close:.2f}跌破MA20({ma20:.2f})"
            return result
        
        # MA60不能明确向下
        if not pd.isna(ma60):
            ma60_slope = df["ma60"].diff(5).iloc[-1] if len(df) >= 65 else 0
            if not pd.isna(ma60_slope) and ma60_slope < 0 and close < ma60:
                result["pass"] = False
                result["reason"] = f"MA60向下且股价在MA60下方，中期趋势走坏"
                return result
        
        result["reason"] = "硬性筛选通过"
    
    # ---- 2. 流动性充足: 日均成交额 >= 8亿 ----
    min_amount = getattr(config, 'MIN_DAILY_AMOUNT', 8e8)
    if "amount" in df.columns:
        avg_amount_20d = df["amount"].iloc[-20:].mean()
        result["details"]["avg_amount"] = avg_amount_20d
        if not pd.isna(avg_amount_20d) and avg_amount_20d < min_amount:
            result["pass"] = False
            result["reason"] = f"日均成交额{avg_amount_20d/1e8:.1f}亿 < {min_amount/1e8:.0f}亿，流动性不足"
            return result
    
    # ---- 3. 股性稳定: 近30日振幅>10%的天数 <= 3 ----
    max_amp_days = getattr(config, 'MAX_HIGH_AMPLITUDE_DAYS', 3)
    if len(df) >= 30:
        recent_30 = df.iloc[-30:]
        amplitude = (recent_30["high"] - recent_30["low"]) / recent_30["close"].shift(1)
        high_amp_count = (amplitude > 0.10).sum()
        result["details"]["high_amp_days"] = int(high_amp_count)
        if high_amp_count > max_amp_days:
            result["pass"] = False
            result["reason"] = f"近30日振幅>10%的天数={high_amp_count} > {max_amp_days}，量化控盘风险"
            return result
    
    # ---- 4. 无放量暴跌: 近5日无单日跌幅>8%且放量 ----
    crash_threshold = getattr(config, 'CRASH_THRESHOLD', -0.08)
    crash_vol_ratio = getattr(config, 'CRASH_VOLUME_RATIO', 2.0)
    if len(df) >= 6:
        vol_ma20 = df["volume"].iloc[-20:].mean() if len(df) >= 20 else df["volume"].mean()
        for i in range(-5, 0):
            idx = len(df) + i
            if idx < 1:
                continue
            day_change = (df["close"].iloc[idx] / df["close"].iloc[idx-1] - 1)
            day_vol = df["volume"].iloc[idx]
            if day_change <= crash_threshold and not pd.isna(vol_ma20) and vol_ma20 > 0:
                if day_vol > vol_ma20 * crash_vol_ratio:
                    result["pass"] = False
                    result["reason"] = f"近5日有放量暴跌(跌{day_change:.2%}且量>{crash_vol_ratio}倍)，资金出逃"
                    return result
    
    # ---- 5. 不在下降通道（强势模式严格检查）----
    if not is_weak_market and not pd.isna(ma60) and not pd.isna(ma20_slope):
        ma60_slope = df["ma60"].diff(5).iloc[-1] if len(df) >= 65 else 0
        if not pd.isna(ma60_slope) and ma20_slope < 0 and ma60_slope < 0:
            result["pass"] = False
            result["reason"] = "MA20/MA60全部向下，明确下降通道"
            return result
    
    return result


# ============================================================
# 二、赛道筛选（第一步）
# ============================================================

def filter_strong_sectors(data_dict: dict, lookback: int = 20) -> dict:
    """
    筛选强势赛道（V3.0升级版）
    
    评分维度:
    - 近20日涨跌幅（30%）
    - 近5日加速度（20%）
    - 均线位置（30%）：赛道内站上MA20的股票占比
    - MA60上方占比（20%）：板块内站稳MA60的股票占比>60%才算有效主线
    
    新增判定标准:
    - 板块内股票站稳60日均线的占比 > 60% 才算有效主线
    - 板块20日均线拐头向上作为必要条件
    - 弱势赛道直接排除（MA20/MA60全部向下）
    """
    sector_data = defaultdict(lambda: {"changes": [], "recent_changes": [], "early_changes": [], 
                                        "ma20_count": 0, "ma60_count": 0, "total": 0,
                                        "ma20_slopes": []})

    for code, df in data_dict.items():
        if code == "000300":
            continue
        # 过滤创业板(300)和科创板(688)
        if code.startswith("300") or code.startswith("688"):
            continue
        # 过滤ETF基金(588/159开头)，不参与个股筛选
        if code.startswith("588") or code.startswith("159"):
            continue
        info = config.get_stock_info(code)
        sector = info.get("赛道", "其他")
        if len(df) < lookback + 5:
            continue

        sector_data[sector]["total"] += 1

        change_20d = (df["close"].iloc[-1] / df["close"].iloc[-lookback] - 1) * 100
        sector_data[sector]["changes"].append(change_20d)

        change_5d = (df["close"].iloc[-1] / df["close"].iloc[-5] - 1) * 100
        change_15d = (df["close"].iloc[-5] / df["close"].iloc[-lookback] - 1) * 100 if df["close"].iloc[-lookback] > 0 else 0
        sector_data[sector]["recent_changes"].append(change_5d)
        sector_data[sector]["early_changes"].append(change_15d)

        if "ma20" in df.columns:
            ma20 = df["ma20"].iloc[-1]
            if not pd.isna(ma20) and df["close"].iloc[-1] > ma20:
                sector_data[sector]["ma20_count"] += 1
            # MA20斜率
            if len(df) >= 23:
                slope = df["ma20"].iloc[-1] - df["ma20"].iloc[-4]
                if not pd.isna(slope):
                    sector_data[sector]["ma20_slopes"].append(slope)

        # MA60上方占比
        if "ma60" in df.columns and len(df) >= 60:
            ma60 = df["ma60"].iloc[-1]
            if not pd.isna(ma60) and df["close"].iloc[-1] > ma60:
                sector_data[sector]["ma60_count"] += 1

    sectors = []
    ma60_ratio_threshold = getattr(config, 'SECTOR_MA60_ABOVE_RATIO', 0.60)
    
    for sector, data in sector_data.items():
        if data["total"] == 0:
            continue

        avg_change = np.mean(data["changes"])
        avg_recent = np.mean(data["recent_changes"])
        avg_early = np.mean(data["early_changes"])
        ma20_ratio = data["ma20_count"] / data["total"]
        ma60_ratio = data["ma60_count"] / data["total"]
        
        # MA20斜率（板块整体趋势方向）
        avg_ma20_slope = np.mean(data["ma20_slopes"]) if data["ma20_slopes"] else 0

        acceleration = avg_recent / 5 / (avg_early / 15 + 0.01) if avg_early != 0 else 1.0

        change_score = max(0, min(100, (avg_change + 15) / 30 * 100))
        accel_score = max(0, min(100, acceleration * 50))
        ma20_score = ma20_ratio * 100
        ma60_score = ma60_ratio * 100

        # 综合评分（新增MA60占比权重）
        total_score = change_score * 0.30 + accel_score * 0.20 + ma20_score * 0.30 + ma60_score * 0.20

        # 判定赛道状态
        is_valid = ma60_ratio >= ma60_ratio_threshold and avg_ma20_slope > 0
        is_weak = ma20_ratio < 0.3 and ma60_ratio < 0.3  # MA20/MA60占比都低 = 弱势

        sectors.append({
            "sector": sector,
            "score": round(total_score, 1),
            "change_20d": round(avg_change, 2),
            "acceleration": round(acceleration, 2),
            "ma20_ratio": round(ma20_ratio, 2),
            "ma60_ratio": round(ma60_ratio, 2),
            "ma20_slope": round(avg_ma20_slope, 4),
            "is_valid": is_valid,
            "stock_count": data["total"]
        })

    sectors.sort(key=lambda x: x["score"], reverse=True)
    strong = [s["sector"] for s in sectors if s["score"] >= 60 and s["is_valid"]]
    weak = [s["sector"] for s in sectors if s["score"] < 40 or not s["is_valid"] and s["ma60_ratio"] < 0.3]

    return {"sectors": sectors, "strong": strong, "weak": weak}


# ============================================================
# 三、CANSLIM多因子打分（第二步）
# ============================================================

def canslim_score(df: pd.DataFrame, code: str, all_dfs: dict = None) -> dict:
    """
    CANSLIM量化打分（0-100分）
    
    技术面可量化部分:
    - N因子（新事物/新高）20分：股价创近60日新高、突破形态
    - S因子（供给需求/量价）20分：缩量回踩MA20、放量突破
    - L因子（领涨龙头）20分：RPS相对强弱、行业内涨幅排名
    - C/A/I因子（基本面）20分：从FUNDAMENTAL_DATA读取
    - M因子（大盘方向）20分：从check_market_direction传入
    
    买入信号加权:
    - 缩量回踩20日均线（经典买点）→ 额外+10分
    - 放量突破60日新高（启动信号）→ 额外+10分
    """
    if len(df) < 60:
        return {"total_score": 0, "factors": {}, "signals": [], "reason": "数据不足"}

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    current_price = latest["close"]
    factors = {}
    signals = []

    # ---- N因子：新事物/新高（20分）----
    n_score = 0
    high_60d = df["high"].iloc[-60:].max()
    high_120d = df["high"].iloc[-120:].max() if len(df) >= 120 else high_60d

    # 股价创近60日新高
    if current_price >= high_60d * 0.98:  # 接近或创新高
        n_score += 12
        if current_price >= high_60d:
            signals.append("创60日新高")
    # 股价创近120日新高（半年新高更有价值）
    if len(df) >= 120 and current_price >= high_120d * 0.98:
        n_score += 8
        if current_price >= high_120d:
            signals.append("创半年新高")

    # 距离新高的位置（越近越好）
    dist_from_high = (current_price - high_60d) / high_60d * 100
    if -5 <= dist_from_high <= 0:
        n_score += 5  # 距新高5%以内
    elif dist_from_high > 0:
        n_score += 8  # 已突破新高

    factors["N_新事物"] = min(n_score, 20)

    # ---- S因子：供给需求/量价配合（20分）----
    s_score = 0
    vol = latest["volume"]
    vol_ma20 = df["volume"].iloc[-20:].mean()
    vol_ratio = vol / vol_ma20 if vol_ma20 > 0 else 1

    # ★ 核心买点：缩量回踩20日均线
    if "ma20" in df.columns and not pd.isna(latest.get("ma20", None)):
        dist_to_ma20 = (current_price - latest["ma20"]) / latest["ma20"]

        # 缩量：成交量较20日均量萎缩30%以上
        is_shrink = vol_ratio < 0.7
        # 回踩MA20：价格在MA20附近（-3%到+2%）
        is_pullback = -0.03 <= dist_to_ma20 <= 0.02
        # 不跌破：收盘价仍在MA20上方或仅微破
        is_holding = dist_to_ma20 >= -0.03

        if is_shrink and is_pullback and is_holding:
            s_score += 20  # 完美缩量回踩MA20
            signals.append("★缩量回踩MA20")
        elif is_pullback and is_holding:
            s_score += 12  # 回踩MA20但未缩量
            signals.append("回踩MA20")
        elif is_shrink and -0.05 <= dist_to_ma20 <= 0.05:
            s_score += 8   # 缩量但在MA20附近稍远

    # 放量突破（另一个核心买点）
    if vol_ratio > 1.5 and current_price > prev["close"]:
        s_score += 10
        signals.append("放量上涨")
    elif vol_ratio > 1.2 and current_price > prev["close"]:
        s_score += 5

    # 下跌缩量（健康的量价关系）
    if current_price < prev["close"] and vol_ratio < 0.7:
        s_score += 5
        signals.append("下跌缩量")

    factors["S_供需"] = min(s_score, 20)

    # ---- L因子：领涨龙头/相对强弱（20分）----
    l_score = 0

    # RPS（Relative Price Strength）：近60日涨幅在所有候选股中的排名
    change_60d = (current_price / df["close"].iloc[-60] - 1) * 100 if len(df) >= 60 else 0
    change_20d = (current_price / df["close"].iloc[-20] - 1) * 100 if len(df) >= 20 else 0

    # 计算RPS排名（需要all_dfs）
    rps_rank = 0.5  # 默认中位
    if all_dfs:
        all_changes = []
        for c, d in all_dfs.items():
            if c == "000300" or len(d) < 60:
                continue
            c60 = (d["close"].iloc[-1] / d["close"].iloc[-60] - 1) * 100
            all_changes.append(c60)
        if all_changes:
            rank = sum(1 for x in all_changes if x <= change_60d)
            rps_rank = rank / len(all_changes)

    if rps_rank >= 0.8:
        l_score += 12  # 前20%强势股
        signals.append(f"RPS前{int(rps_rank*100)}%")
    elif rps_rank >= 0.6:
        l_score += 8   # 前40%
    elif rps_rank >= 0.4:
        l_score += 4

    # 60日涨幅
    if change_60d > 30:
        l_score += 8
    elif change_60d > 15:
        l_score += 5
    elif change_60d > 5:
        l_score += 3

    # 20日涨幅（短期动量）
    if 3 < change_20d < 20:  # 温和上涨最佳
        l_score += 5
    elif change_20d > 20:
        l_score += 3  # 涨太多可能过热

    factors["L_龙头"] = min(l_score, 20)

    # ---- C/A/I因子：基本面（20分）----
    cai_score = 0
    fund_data = FUNDAMENTAL_DATA.get(code, {})

    # C因子：单季度业绩增速
    eps_q = fund_data.get("eps_growth_q", None)
    if eps_q is not None:
        if eps_q > 50:
            cai_score += 8
        elif eps_q > 25:
            cai_score += 5
        elif eps_q > 0:
            cai_score += 2
    else:
        cai_score += 3  # 无数据给中性分

    # A因子：3年复合增速
    eps_3y = fund_data.get("eps_growth_3y", None)
    if eps_3y is not None:
        if eps_3y > 30:
            cai_score += 7
        elif eps_3y > 20:
            cai_score += 5
        elif eps_3y > 10:
            cai_score += 2
    else:
        cai_score += 3

    # I因子：机构认同
    has_inst = fund_data.get("has_institution", None)
    if has_inst is True:
        cai_score += 5
    elif has_inst is False:
        cai_score -= 2
    else:
        cai_score += 2

    factors["CAI_基本面"] = max(0, min(cai_score, 20))

    # ---- 综合评分 ----
    total = factors["N_新事物"] + factors["S_供需"] + factors["L_龙头"] + factors["CAI_基本面"]

    # ---- 前瞻性预测加分（0-20分）----
    prediction = predict_forward(df, code)
    factors["P_前瞻"] = prediction["score"]
    signals.extend(prediction["signals"])
    total += prediction["score"]

    return {
        "total_score": round(total, 1),
        "factors": {k: round(v, 1) for k, v in factors.items()},
        "signals": signals,
        "reason": _canslim_reason(factors, signals),
        "rps_rank": round(rps_rank * 100, 1),
        "change_60d": round(change_60d, 2),
        "prediction": prediction,
    }


def _canslim_reason(factors: dict, signals: list) -> str:
    """生成简要说明"""
    parts = []
    if signals:
        parts.extend(signals[:3])
    if factors.get("N_新事物", 0) >= 15:
        parts.append("新高")
    if factors.get("S_供需", 0) >= 15:
        parts.append("量价佳")
    if factors.get("L_龙头", 0) >= 15:
        parts.append("龙头强")
    if factors.get("CAI_基本面", 0) >= 14:
        parts.append("业绩好")
    if factors.get("P_前瞻", 0) >= 12:
        parts.append("★前瞻强")
    return " | ".join(parts) if parts else "一般"


# ============================================================
# 三’、前瞻性预测因子（P因子）
# ============================================================

def predict_forward(df: pd.DataFrame, code: str) -> dict:
    """
    前瞻性预测因子（0-20分）
    
    核心逻辑：不仅看过去发生了什么，更要预判明天/本周可能发生什么
    
    四个维度:
    1. 动量延续性（6分）: 近5日趋势是否具备延续性
    2. 板块轮动预判（5分）: 板块是否处于启动初期（而非尾声）
    3. 突破预判（5分）: 是否接近关键压力位，即将突破
    4. 量能蓄积（4分）: 近期量能是否显示主力吸筹迹象
    """
    if len(df) < 30:
        return {"score": 0, "signals": [], "detail": "数据不足"}
    
    score = 0
    signals = []
    latest = df.iloc[-1]
    current_price = latest["close"]
    
    # ---- 1. 动量延续性（6分）----
    # 近5日每日涨跌幅
    if len(df) >= 6:
        recent_5d_changes = []
        for i in range(-5, 0):
            chg = (df["close"].iloc[i] / df["close"].iloc[i-1] - 1) * 100
            recent_5d_changes.append(chg)
        
        # 连续上涨天数
        up_days = sum(1 for c in recent_5d_changes if c > 0)
        # 动量加速度：近3日平均涨幅 vs 前2日平均涨幅
        avg_recent_3 = np.mean(recent_5d_changes[-3:])
        avg_early_2 = np.mean(recent_5d_changes[:2])
        
        if up_days >= 4 and avg_recent_3 > avg_early_2 > 0:
            score += 6  # 连续上涨且加速，明日延续概率高
            signals.append("动量加速↑")
        elif up_days >= 3 and avg_recent_3 > 0:
            score += 4  # 多数天上涨，趋势延续
            signals.append("动量延续")
        elif up_days >= 3 and avg_recent_3 < avg_early_2:
            score += 2  # 上涨但减速，注意
    
    # ---- 2. 板块轮动预判（5分）----
    # 判断板块是否处于启动初期（近5日涨幅 > 近20日涨幅的均值）
    if len(df) >= 20:
        change_5d = (current_price / df["close"].iloc[-6] - 1) * 100
        change_20d = (current_price / df["close"].iloc[-21] - 1) * 100
        
        # 近5日贡献了20日涨幅的大部分 → 板块刚启动
        if change_20d > 0 and change_5d > 0:
            contribution = change_5d / change_20d if change_20d != 0 else 0
            if contribution > 0.7 and change_5d > 3:
                score += 5  # 近5日贡献70%涨幅，板块刚启动
                signals.append("板块启动期")
            elif contribution > 0.5 and change_5d > 2:
                score += 3  # 板块加速中
                signals.append("板块加速")
        elif change_5d > 3 and change_20d < 5:
            score += 4  # 20日横盘后突然启动
            signals.append("横盘突破启动")
    
    # ---- 3. 突破预判（5分）----
    # 股价接近关键压力位，即将突破
    if len(df) >= 60:
        high_20d = df["high"].iloc[-20:].max()
        high_60d = df["high"].iloc[-60:].max()
        
        # 距离20日新高的距离
        dist_to_20d_high = (high_20d - current_price) / current_price * 100
        # 距离60日新高的距离
        dist_to_60d_high = (high_60d - current_price) / current_price * 100
        
        if 0 < dist_to_20d_high <= 2:
            score += 5  # 距20日新高仅2%，明日可能突破
            signals.append("即将突码20日新高")
        elif 0 < dist_to_60d_high <= 3:
            score += 4  # 距60日新高3%以内
            signals.append("逼近60日新高")
        elif 0 < dist_to_20d_high <= 5:
            score += 2  # 接近前高
        
        # 布林带收窄（波动率降低 → 即将选择方向）
        if "boll_upper" in df.columns and "boll_lower" in df.columns:
            boll_width = (latest.get("boll_upper", 0) - latest.get("boll_lower", 0)) / current_price * 100
            if boll_width < 8 and current_price > latest.get("ma20", 0):
                score += 2  # 布林收窄+价格在MA20上方 → 向上突破概率大
                signals.append("布林收窄待突破")
    
    # ---- 4. 量能蓄积（4分）----
    # 近期量能显示主力吸筹迹象
    if len(df) >= 10:
        vol_5d = df["volume"].iloc[-5:].mean()
        vol_20d = df["volume"].iloc[-20:].mean()
        vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 1
        
        # 近5日量能温和放大（1.2-2倍）且价格上涨 → 主力吸筹
        price_up_5d = current_price > df["close"].iloc[-6]
        
        if 1.2 <= vol_ratio <= 2.0 and price_up_5d:
            score += 4  # 量增价涨，主力进场
            signals.append("量能蓄积↑")
        elif 1.1 <= vol_ratio <= 1.5 and price_up_5d:
            score += 2  # 温和放量
        
        # 下跌缩量 + 上涨放量（健康的量价关系）
        up_vol = df[df["close"] > df["close"].shift(1)]["volume"].iloc[-5:].mean() if len(df) > 5 else 0
        down_vol = df[df["close"] < df["close"].shift(1)]["volume"].iloc[-5:].mean() if len(df) > 5 else 0
        if up_vol > 0 and down_vol > 0 and up_vol > down_vol * 1.3:
            score += 2  # 涨时量大、跌时量小，主力控盘
            signals.append("主力控盘")
    
    return {
        "score": min(score, 20),
        "signals": signals,
        "detail": f"动量+轮动+突破+量能 综合预判"
    }


# ============================================================
# 四、买点计算（第三步）— 分批建仓 + 移动止损
# ============================================================

def calculate_buy_plan(df: pd.DataFrame, code: str, factor_result: dict,
                       market_info: dict = None) -> dict:
    """
    计算次日买点价格、止损价、分批建仓方案
    
    买点策略:
    - 缩量回踩买点：MA20附近（核心买点）
    - 激进买点：现价+0.5%
    - 稳健买点：MA5/MA10附近
    - 保守买点：MA20附近
    
    分批建仓:
    - 第一批 40%：在买点附近建仓
    - 第二批 60%：浮盈≥3%后加仓
    
    止损策略:
    - 初始止损：买入价×90%（10%止损）
    - ATR止损与技术支撑止损取较高者
    - 浮盈后上移移动止损
    """
    latest = df.iloc[-1]
    current_price = latest["close"]
    stock_info = config.get_stock_info(code)
    stock_type = stock_info.get("类型", "龙头")

    # ATR
    atr = latest.get("atr", 0)
    if pd.isna(atr) or atr <= 0:
        if len(df) >= 14:
            high_low = df["high"].iloc[-14:] - df["low"].iloc[-14:]
            atr = high_low.mean()
        else:
            atr = current_price * 0.025

    # 均线
    ma5 = latest.get("ma5", current_price)
    ma10 = latest.get("ma10", current_price)
    ma20 = latest.get("ma20", current_price)
    ma60 = latest.get("ma60", current_price)

    # ---- 买点价格 ----
    # 激进买点：根据信号类型决定
    # - 突破/动量类信号：现价+0.5%（追入）
    # - 回踩/缩量类信号：现价（直接买入，不应高于现价）
    signals_list = factor_result.get("signals", [])
    is_pullback_signal = any(
        s for s in signals_list
        if "回踩" in s or "缩量" in s or "超卖" in s or "均值回归" in s
    )
    if is_pullback_signal:
        aggressive_buy = round(current_price, 2)  # 回踩信号：激进买点=现价
    else:
        aggressive_buy = round(current_price * 1.005, 2)  # 突破/动量：现价+0.5%

    # 稳健买点：MA5和MA10的较高者附近
    if not pd.isna(ma5) and not pd.isna(ma10):
        moderate_buy = round(max(ma5, ma10) * 1.005, 2)
    else:
        moderate_buy = round(current_price * 0.99, 2)

    # 保守买点：MA20附近（缩量回踩的理想买点）
    if not pd.isna(ma20):
        conservative_buy = round(ma20 * 1.005, 2)
    else:
        conservative_buy = round(current_price * 0.97, 2)

    # ---- 止损价 ----
    # 1. 10%固定止损（底线）
    fixed_stop = round(current_price * 0.90, 2)

    # 2. ATR自适应止损
    atr_multiplier = 2.0 if stock_type == "龙头" else 2.5
    atr_stop = round(current_price - atr_multiplier * atr, 2)

    # 3. 技术支撑止损
    support_stop = 0
    if not pd.isna(ma20) and ma20 < current_price:
        support_stop = round(ma20 * 0.99, 2)
    if not pd.isna(ma60) and ma60 < current_price:
        ma60_stop = round(ma60 * 0.99, 2)
        if ma60_stop > support_stop:
            support_stop = ma60_stop

    # 最终止损：取ATR/技术支撑/固定止损中较高的，但不能高于现价
    candidates_stop = [s for s in [atr_stop, support_stop, fixed_stop] if s > 0]
    final_stop = max(candidates_stop) if candidates_stop else fixed_stop
    final_stop = min(final_stop, round(current_price * 0.95, 2))  # 最多5%以内

    # ---- 分批建仓方案 ----
    # 使用可用资金（而非总资金）计算实际可买股数
    available_cash = getattr(config, 'AVAILABLE_CASH', config.TOTAL_CAPITAL * 0.5)
    total_capital = config.TOTAL_CAPITAL
    # 大盘仓位限制
    market_limit = market_info.get("position_limit_ratio", 1.0) if market_info else 1.0
    # 实际可用 = min(可用资金, 总资金*仓位限制)
    effective_capital = min(available_cash, total_capital * market_limit)

    # 第一批40%试仓
    first_ratio = 0.4
    first_max_amount = effective_capital * first_ratio
    first_shares = int(first_max_amount / moderate_buy / 100) * 100
    if first_shares == 0:
        first_shares = 100
    first_amount = first_shares * moderate_buy

    # 第二批60%加仓（浮盈≥3%后）
    add_price = round(moderate_buy * 1.03, 2)  # 浮盈3%的加仓触发价
    second_ratio = 0.6
    second_max_amount = effective_capital * second_ratio
    second_shares = int(second_max_amount / add_price / 100) * 100
    if second_shares == 0:
        second_shares = 100
    second_amount = second_shares * add_price

    # 总仓位
    total_shares = first_shares + second_shares
    total_amount = first_amount + second_amount

    # 最大亏损（以止损价计算）
    max_loss_first = first_shares * (moderate_buy - final_stop)
    max_loss_second = second_shares * (add_price - final_stop)
    max_loss_total = max_loss_first + max_loss_second
    max_loss_pct = max_loss_total / total_capital * 100

    # 风控检查
    pass_risk = max_loss_pct < 3.0

    return {
        "code": code,
        "name": stock_info.get("名称", config.get_stock_name(code)),
        "sector": stock_info.get("赛道", ""),
        "type": stock_type,
        "current_price": round(current_price, 2),
        # 买点
        "aggressive_buy": aggressive_buy,
        "moderate_buy": moderate_buy,
        "conservative_buy": conservative_buy,
        # 止损
        "stop_loss": final_stop,
        "stop_loss_pct": round((current_price - final_stop) / current_price * 100, 1),
        "atr": round(atr, 2),
        # 分批建仓
        "first_shares": first_shares,
        "first_amount": round(first_amount, 0),
        "first_ratio_pct": first_ratio * 100,
        "add_price": add_price,
        "second_shares": second_shares,
        "second_amount": round(second_amount, 0),
        "second_ratio_pct": second_ratio * 100,
        "total_shares": total_shares,
        "total_amount": round(total_amount, 0),
        # 风控
        "max_loss": round(max_loss_total, 0),
        "max_loss_pct": round(max_loss_pct, 2),
        "pass_risk": pass_risk,
        "risk_msg": "" if pass_risk else f"最大亏损{max_loss_pct:.1f}%超限",
        # 因子
        "factor_score": factor_result["total_score"],
        "factor_detail": factor_result["factors"],
        "factor_reason": factor_result["reason"],
        "signals": factor_result.get("signals", []),
        "rps_rank": factor_result.get("rps_rank", 0),
    }


# ============================================================
# 五、行业配额动态分配
# ============================================================

def allocate_sector_quotas(sector_result: dict, total_max: int = 10) -> dict:
    """
    根据行业强弱动态分配选股名额
    
    规则:
    - 基础配额: 按config.SECTOR_CANDIDATES中的weight分配
    - 动态调整: 强势赛道+1名额，弱势赛道-1名额
    - 保底: 每个赛道至少1个名额（如果该赛道有候选股）
    
    参数:
        sector_result: filter_strong_sectors()的返回结果
        total_max: 总入选上限
    
    返回:
        {"半导体": 4, "军工航天": 2, ...} 各行业配额
    """
    sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
    if not sector_candidates:
        return {}
    
    strong_sectors = set(sector_result.get("strong", []))
    weak_sectors = set(sector_result.get("weak", []))
    
    quotas = {}
    for sector_name, sector_info in sector_candidates.items():
        weight = sector_info.get("weight", 0.1)
        # 基础配额 = 总上限 × 权重
        base_quota = max(1, round(total_max * weight))
        
        # 动态调整: 强势+1, 弱势-1
        # 将行业名称与赛道评分中的赛道名匹配（模糊匹配）
        is_strong = any(s in sector_name or sector_name in s for s in strong_sectors)
        is_weak = any(s in sector_name or sector_name in s for s in weak_sectors)
        
        if is_strong:
            base_quota += 1
        elif is_weak:
            base_quota = max(1, base_quota - 1)
        
        quotas[sector_name] = base_quota
    
    # 确保总配额不超过上限
    total_allocated = sum(quotas.values())
    if total_allocated > total_max:
        # 按比例缩减
        scale = total_max / total_allocated
        quotas = {k: max(1, round(v * scale)) for k, v in quotas.items()}
    
    return quotas


# ============================================================
# 六、主流程：运行选股引擎（全赛道版）
# ============================================================

def run_stock_screener(data_dict: dict, holdings: dict = None,
                       min_score: float = None, max_stocks: int = None,
                       news_risk: dict = None) -> dict:
    """
    运行完整选股流程（全赛道版 + 弱势行情支持）
    
    改进:
    - 支持全行业选股，不局限于半导体
    - 根据行业强弱动态分配名额
    - 弱势行情输出“观察池”（相对强势股）
    - 结合持仓行业集中度调整配额
    - 新增持仓诊断输出
    - 新闻风险过滤（level>=2排除候选）
    
    参数:
        data_dict: {code: DataFrame} 股票数据（含技术指标）
        holdings: 当前持仓
        min_score: 最低入选分数
        max_stocks: 最多入选股票数
        news_risk: 新闻风险扫描结果（仅用于过滤，不产生信号）
    """
    # 从配置读取参数
    screener_cfg = getattr(config, 'SCREENER_CONFIG', {})
    if min_score is None:
        min_score = screener_cfg.get("min_score", 45)
    if max_stocks is None:
        max_stocks = screener_cfg.get("total_max", 10)
    max_per_sector = screener_cfg.get("max_stocks_per_sector", 3)
    
    scan_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info(f"[选股引擎V3] 开始运行（全赛道+弱势模式），候选股票 {len(data_dict)} 只")

    # M因子：大盘方向判断
    logger.info("[选股引擎V3] M因子: 大盘方向判断...")
    market_info = check_market_direction(data_dict)
    market_state = market_info["market_state"]
    logger.info(f"  大盘状态: {market_state} | 可买入: {market_info['can_buy']}")

    # 第一步：赛道筛选
    logger.info("[选股引擎V3] Step 1: 赛道筛选...")
    sector_result = filter_strong_sectors(data_dict)
    logger.info(f"  强势赛道: {sector_result['strong']}")
    logger.info(f"  弱势赛道: {sector_result['weak']}")
    
    # 行业配额动态分配（结合持仓集中度调整）
    sector_quotas = allocate_sector_quotas(sector_result, max_stocks)
    # 根据持仓行业集中度降低已重仓行业配额
    if holdings:
        holdings_sector_amount = defaultdict(float)
        for code, pos in holdings.items():
            sector = pos.get("sector", "")
            shares = pos.get("shares", 0)
            price = pos.get("current_price", pos.get("buy_price", 0))
            holdings_sector_amount[sector] += shares * price
        total_capital = config.TOTAL_CAPITAL
        for sector_name, amount in holdings_sector_amount.items():
            ratio = amount / total_capital if total_capital > 0 else 0
            if ratio > 0.15:  # 单行业持仓超15%，降低配额
                for q_sector in sector_quotas:
                    if any(s in q_sector or q_sector in s for s in [sector_name]):
                        sector_quotas[q_sector] = max(1, sector_quotas[q_sector] - 1)
                        logger.info(f"  持仓集中调整: {q_sector}配额-1（已持仓{ratio:.1%}）")
    logger.info(f"  行业配额: {sector_quotas}")

    # 第二步：硬性筛选 + CANSLIM多因子打分
    logger.info("[选股引擎V3] Step 2: 硬性筛选 + CANSLIM多因子打分...")
    candidates = []
    watch_list = []  # 观察池（弱势行情下相对强势但未达买入标准的）
    filtered_count = 0
    
    for code, df in data_dict.items():
        if code == "000300":
            continue
        # 过滤创业板(300)和科创板(688)，用户无交易权限
        if code.startswith("300") or code.startswith("688"):
            continue
        # 过滤ETF基金(588/159开头)，不参与个股筛选
        if code.startswith("588") or code.startswith("159"):
            continue
        # 新闻风险过滤（level>=2 排除候选，仅做选股过滤不产生信号）
        if news_risk and getattr(config, 'NEWS_FILTER_IN_SCREENER', False):
            nr = news_risk.get(code, {})
            if nr.get("level", 0) >= 2:
                top_alert = nr.get("alerts", [{}])[0]
                logger.info(f"  {code} {nr.get('name', '')}: [新闻过滤] "
                           f"{top_alert.get('title', '')[:30]}")
                filtered_count += 1
                continue
        # 从STOCK_POOL或SECTOR_CANDIDATES中查找股票信息（统一查找）
        stock_info = config.get_stock_info(code)
        
        # 确定该股票属于哪个行业
        sector_name = _find_stock_sector(code, stock_info)

        # ---- 硬性筛选（传入market_state）----
        hf_result = hard_filter(df, code, market_state)
        if not hf_result["pass"]:
            filtered_count += 1
            # 弱势行情下，将评分较高的失败股放入观察池
            if market_state in ("down", "weak", "neutral") and hf_result.get("weak_score", 0) >= 15:
                watch_list.append({
                    "code": code,
                    "name": stock_info.get("名称", code),
                    "sector": stock_info.get("赛道", sector_name),
                    "sector_group": sector_name,
                    "weak_score": hf_result["weak_score"],
                    "reason": hf_result["reason"],
                    "current_price": round(df["close"].iloc[-1], 2),
                })
            logger.info(f"  {code} {stock_info.get('名称', '')}: [筛选不通过] {hf_result['reason']}")
            continue

        factor_result = canslim_score(df, code, data_dict)
        candidates.append({
            "code": code,
            "sector": stock_info.get("赛道", "其他"),
            "sector_group": sector_name,
            "score": factor_result["total_score"],
            "factors": factor_result["factors"],
            "signals": factor_result.get("signals", []),
            "reason": factor_result["reason"],
            "rps_rank": factor_result.get("rps_rank", 0),
            "weak_score": hf_result.get("weak_score", 0),
            "df": df
        })
        logger.info(f"  {code} {stock_info.get('名称', '')}: {factor_result['total_score']}分 "
                    f"[{sector_name}] "
                    f"(N={factor_result['factors'].get('N_新事物',0)} "
                    f"S={factor_result['factors'].get('S_供需',0)} "
                    f"L={factor_result['factors'].get('L_龙头',0)} "
                    f"CAI={factor_result['factors'].get('CAI_基本面',0)}) "
                    f"| {factor_result['reason']}")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    # 观察池按weak_score排序
    watch_list.sort(key=lambda x: x["weak_score"], reverse=True)
    watch_list = watch_list[:5]  # 最多5只

    # 第三步：按行业配额选股 + 计算买点
    logger.info("[选股引擎V3] Step 3: 行业均衡选股 + 计算买点...")
    stock_pool = []
    sector_selected_count = defaultdict(int)
    
    # 弱势行情下降低最低分数要求
    effective_min_score = min_score if market_state == "up" else max(30, min_score - 15)
    
    for cand in candidates:
        if len(stock_pool) >= max_stocks:
            break
        if cand["score"] < effective_min_score:
            continue
        # 跳过已持仓股票
        if holdings and cand["code"] in holdings:
            continue
        
        # 行业配额检查
        sector_group = cand["sector_group"]
        quota = sector_quotas.get(sector_group, max_per_sector)
        if sector_selected_count[sector_group] >= quota:
            logger.info(f"  跳过 {cand['code']}: {sector_group}配额已满({quota})")
            continue
        if sector_selected_count[sector_group] >= max_per_sector:
            continue

        buy_plan = calculate_buy_plan(cand["df"], cand["code"], {
            "total_score": cand["score"],
            "factors": cand["factors"],
            "signals": cand["signals"],
            "reason": cand["reason"],
            "rps_rank": cand["rps_rank"],
        }, market_info)
        buy_plan["sector_group"] = sector_group
        buy_plan["is_watch"] = not market_info["can_buy"]  # 大盘下跌时标记为观察
        stock_pool.append(buy_plan)
        sector_selected_count[sector_group] += 1
        status = "观察" if not market_info["can_buy"] else "入选"
        logger.info(f"  {status}: {buy_plan['code']} {buy_plan['name']} [{sector_group}] | "
                    f"评分{buy_plan['factor_score']} | 买点{buy_plan['moderate_buy']} | "
                    f"止损{buy_plan['stop_loss']}(-{buy_plan['stop_loss_pct']}%) | "
                    f"首批{buy_plan['first_shares']}股+加仓{buy_plan['second_shares']}股")

    # 行业分布统计
    logger.info(f"[选股引擎V3] 完成: {len(stock_pool)}只入选 / {len(candidates)}只候选 / {len(watch_list)}只观察")
    logger.info(f"  行业分布: {dict(sector_selected_count)}")

    # 持仓诊断
    holdings_diagnosis = diagnose_holdings(holdings, data_dict) if holdings else []

    return {
        "market_info": market_info,
        "sector_analysis": sector_result,
        "sector_quotas": sector_quotas,
        "stock_pool": stock_pool,
        "watch_list": watch_list,
        "holdings_diagnosis": holdings_diagnosis,
        "scan_time": scan_time,
        "total_candidates": len(candidates),
        "qualified_count": len(stock_pool),
        "sector_distribution": dict(sector_selected_count),
    }


def _find_stock_sector(code: str, stock_info: dict) -> str:
    """
    查找股票所属行业分组（从SECTOR_CANDIDATES中查找）
    如果找不到，返回stock_info中的赛道名
    """
    sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
    for sector_name, sector_info in sector_candidates.items():
        if code in sector_info.get("stocks", {}):
            return sector_name
    # 未找到，用原始赛道名
    return stock_info.get("赛道", "其他")


def _find_stock_info_from_candidates(code: str) -> dict:
    """
    从SECTOR_CANDIDATES中查找股票信息
    返回: {"名称": xxx, "赛道": xxx, "类型": xxx}
    """
    sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
    for sector_name, sector_info in sector_candidates.items():
        stocks = sector_info.get("stocks", {})
        if code in stocks:
            info = stocks[code]
            return {
                "名称": info.get("名称", code),
                "赛道": info.get("细分", sector_name),
                "类型": info.get("类型", "龙头"),
            }
    return {"名称": code, "赛道": "其他", "类型": "弹性"}


# ============================================================
# 持仓诊断模块
# ============================================================

def diagnose_holdings(holdings: dict, data_dict: dict) -> list:
    """
    对当前持仓进行诊断，给出持有/减仓/止损建议
    
    诊断维度:
    1. 浮盈浮亏状态
    2. 趋势是否破位（跌破MA20/MA60）
    3. 止损位距离
    4. 行业集中度风险
    
    返回: [{"code", "name", "action", "reason", "profit_pct", ...}]
    """
    if not holdings:
        return []
    
    results = []
    total_capital = config.TOTAL_CAPITAL
    
    for code, pos in holdings.items():
        shares = pos.get("shares", 0)
        buy_price = pos.get("buy_price", 0)
        sector = pos.get("sector", "")
        stock_type = pos.get("stock_type", "龙头")
        
        # 获取当前价格
        df = data_dict.get(code)
        if df is not None and not df.empty:
            current_price = df["close"].iloc[-1]
        else:
            current_price = pos.get("current_price", buy_price)
        
        profit_pct = (current_price - buy_price) / buy_price if buy_price > 0 else 0
        market_value = shares * current_price
        position_ratio = market_value / total_capital if total_capital > 0 else 0
        
        diagnosis = {
            "code": code,
            "name": config.get_stock_name(code),
            "sector": sector,
            "shares": shares,
            "buy_price": round(buy_price, 3),
            "current_price": round(current_price, 2),
            "profit_pct": round(profit_pct * 100, 2),
            "market_value": round(market_value, 0),
            "position_ratio": round(position_ratio * 100, 2),
            "action": "持有",
            "reason": "",
            "stop_loss_price": 0,
        }
        
        # 计算止损位
        from strategy.trend_strategy import compute_trailing_stop
        stop_loss = compute_trailing_stop(buy_price, current_price)
        diagnosis["stop_loss_price"] = stop_loss
        
        # 诊断逻辑
        if df is not None and not df.empty and len(df) >= 20:
            ma20 = df["close"].rolling(20).mean().iloc[-1]
            ma60 = df["close"].rolling(60).mean().iloc[-1] if len(df) >= 60 else None
            
            # 浮亏超过10% + 跌破MA20 → 建议减仓
            if profit_pct < -0.10 and current_price < ma20:
                diagnosis["action"] = "减仓"
                diagnosis["reason"] = f"浮亏{profit_pct:.1%}且跌破MA20({ma20:.2f})，趋势走坏，建议减仓50%止损"
            # 浮亏超过15% → 强烈建议止损
            elif profit_pct < -0.15:
                diagnosis["action"] = "止损"
                diagnosis["reason"] = f"浮亏{profit_pct:.1%}超过15%红线，建议无条件止损离场"
            # 跌破MA60 → 中期趋势走坏
            elif ma60 and current_price < ma60 and profit_pct < 0:
                diagnosis["action"] = "减仓"
                diagnosis["reason"] = f"跌破MA60({ma60:.2f})且浮亏，中期趋势走坏，建议减仓"
            # 浮盈状态
            elif profit_pct > 0:
                if current_price < ma20:
                    diagnosis["action"] = "止盈减仓"
                    diagnosis["reason"] = f"浮盈{profit_pct:.1%}但跌破MA20，建议止盈减仓1/3"
                else:
                    diagnosis["action"] = "持有"
                    diagnosis["reason"] = f"浮盈{profit_pct:.1%}，趋势正常，继续持有，止损上移至{stop_loss:.2f}"
            else:
                diagnosis["action"] = "观望"
                diagnosis["reason"] = f"浮亏{profit_pct:.1%}，尚未触发止损，观望等待，止损位{stop_loss:.2f}"
        else:
            if profit_pct < -0.15:
                diagnosis["action"] = "止损"
                diagnosis["reason"] = f"浮亏{profit_pct:.1%}超过15%，建议止损"
            else:
                diagnosis["action"] = "观望"
                diagnosis["reason"] = f"浮亏{profit_pct:.1%}，数据不足无法判断趋势"
        
        results.append(diagnosis)
    
    # 按浮亏程度排序（亏损最多的排前面）
    results.sort(key=lambda x: x["profit_pct"])
    return results


# ============================================================
# 六、生成选股报告HTML
# ============================================================

def generate_screener_report_html(result: dict) -> str:
    """生成选股结果HTML报告"""
    market = result["market_info"]
    sector = result["sector_analysis"]
    pool = result["stock_pool"]
    scan_time = result["scan_time"]

    next_day = datetime.date.today() + datetime.timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += datetime.timedelta(days=1)
    next_trade_day = next_day.strftime("%Y-%m-%d")

    # 大盘状态颜色
    market_color = {"up": "#52C41A", "neutral": "#FA8C16", "down": "#FF4D4F"}.get(market["market_state"], "#888")
    market_text = {"up": "上升趋势", "neutral": "震荡", "down": "下降趋势"}.get(market["market_state"], "未知")

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 20px; background: #f0f2f5; }}
    .container {{ max-width: 1050px; margin: 0 auto; }}
    .header {{ background: linear-gradient(135deg, #1890FF, #096DD9); color: white; padding: 20px 30px; border-radius: 12px 12px 0 0; }}
    .header h1 {{ margin: 0; font-size: 22px; }}
    .header .subtitle {{ font-size: 13px; opacity: 0.9; margin-top: 5px; }}
    .content {{ background: white; padding: 20px 30px; border-radius: 0 0 12px 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }}
    .section {{ margin: 20px 0; }}
    .section-title {{ font-size: 16px; font-weight: bold; color: #333; margin-bottom: 12px; padding-left: 12px; border-left: 4px solid #1890FF; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ background: #fafafa; padding: 10px 8px; text-align: center; border-bottom: 2px solid #e8e8e8; font-weight: bold; color: #333; }}
    td {{ padding: 10px 8px; text-align: center; border-bottom: 1px solid #f0f0f0; }}
    tr:hover {{ background: #fafafa; }}
    .score-high {{ color: #52C41A; font-weight: bold; }}
    .score-mid {{ color: #FA8C16; font-weight: bold; }}
    .score-low {{ color: #FF4D4F; font-weight: bold; }}
    .price {{ color: #FF4D4F; font-weight: bold; }}
    .buy-price {{ color: #52C41A; font-weight: bold; }}
    .stop-price {{ color: #FF4D4F; }}
    .sector-tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; background: #E6F7FF; color: #1890FF; }}
    .sector-strong {{ background: #F6FFED; color: #52C41A; }}
    .sector-weak {{ background: #FFF1F0; color: #FF4D4F; }}
    .factor-bar {{ display: inline-block; height: 6px; border-radius: 3px; }}
    .bar-n {{ background: #722ED1; }}
    .bar-s {{ background: #1890FF; }}
    .bar-l {{ background: #52C41A; }}
    .bar-cai {{ background: #FA8C16; }}
    .stats {{ display: flex; gap: 15px; margin: 15px 0; flex-wrap: wrap; }}
    .stat-box {{ background: #f5f5f5; padding: 10px 20px; border-radius: 8px; text-align: center; }}
    .stat-box .label {{ font-size: 12px; color: #888; }}
    .stat-box .value {{ font-size: 20px; font-weight: bold; color: #333; }}
    .guide {{ background: #E6F7FF; border: 1px solid #91D5FF; border-radius: 8px; padding: 15px; margin: 15px 0; font-size: 13px; }}
    .guide h3 {{ margin: 0 0 8px; color: #096DD9; font-size: 14px; }}
    .guide-warn {{ background: #FFF7E6; border: 1px solid #FFD591; border-radius: 8px; padding: 15px; margin: 15px 0; font-size: 13px; }}
    .guide-warn h3 {{ margin: 0 0 8px; color: #D46B08; font-size: 14px; }}
    .signal-tag {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; background: #F9F0FF; color: #722ED1; margin: 1px; }}
    .signal-star {{ background: #FFF1F0; color: #FF4D4F; font-weight: bold; }}
    .footer {{ text-align: center; color: #bbb; font-size: 11px; margin-top: 20px; padding-top: 15px; border-top: 1px solid #eee; }}
    .note {{ font-size: 11px; color: #999; margin-top: 4px; }}
    .batch-box {{ display: inline-block; background: #f0f5ff; border: 1px solid #adc6ff; border-radius: 4px; padding: 3px 8px; margin: 2px; font-size: 11px; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>CANSLIM核心股票池 + 条件单设置表</h1>
        <div class="subtitle">适用日期: {next_trade_day} | 扫描时间: {scan_time} | 候选{result['total_candidates']}只 → 入选{result['qualified_count']}只</div>
    </div>
    <div class="content">

        <!-- M因子：大盘状态 -->
        <div class="stats">
            <div class="stat-box" style="border-left:4px solid {market_color}">
                <div class="label">大盘状态</div>
                <div class="value" style="color:{market_color}">{market_text}</div>
                <div class="note">{market.get('detail', '')}</div>
            </div>
            <div class="stat-box"><div class="label">强势赛道</div><div class="value" style="color:#52C41A">{len(sector['strong'])}</div></div>
            <div class="stat-box"><div class="label">弱势赛道</div><div class="value" style="color:#FF4D4F">{len(sector['weak'])}</div></div>
            <div class="stat-box"><div class="label">入选股票</div><div class="value" style="color:#1890FF">{result['qualified_count']}</div></div>
            <div class="stat-box"><div class="label">建议仓位</div><div class="value" style="color:{market_color}">{market.get('position_limit_ratio', 1)*100:.0f}%</div></div>
        </div>
"""

    # 大盘下跌警告
    if market["market_state"] == "down":
        html += f"""
        <div class="guide-warn">
            <h3>⚠ 大盘风险警告</h3>
            <p>当前大盘处于<b>下降趋势</b>（沪深300: {market.get('index_close', 'N/A')}），<b>不建议新开仓位</b>。</p>
            <p>建议：持仓股设好止损保护，空仓等待大盘企稳再操作。逆势操作是亏损的最大来源。</p>
        </div>
"""

    # 赛道排名表
    html += """
        <div class="section">
            <div class="section-title">一、赛道强弱排名</div>
            <table>
                <tr><th>排名</th><th>赛道</th><th>综合评分</th><th>20日涨跌</th><th>加速度</th><th>MA20占比</th><th>股票数</th><th>状态</th></tr>
"""
    for i, s in enumerate(sector["sectors"], 1):
        status_class = "sector-strong" if s["sector"] in sector["strong"] else ("sector-weak" if s["sector"] in sector["weak"] else "")
        status_text = "强势" if s["sector"] in sector["strong"] else ("弱势" if s["sector"] in sector["weak"] else "中性")
        score_class = "score-high" if s["score"] >= 60 else ("score-mid" if s["score"] >= 40 else "score-low")
        html += f"""
                <tr>
                    <td>{i}</td>
                    <td><span class="sector-tag {status_class}">{s['sector']}</span></td>
                    <td class="{score_class}">{s['score']}</td>
                    <td class="{'price' if s['change_20d'] > 0 else 'stop-price'}">{s['change_20d']:+.1f}%</td>
                    <td>{s['acceleration']:.2f}</td>
                    <td>{s['ma20_ratio']:.0%}</td>
                    <td>{s['stock_count']}</td>
                    <td>{status_text}</td>
                </tr>
"""
    html += """
            </table>
        </div>
"""

    # 核心股票池 + 条件单
    if pool:
        html += """
        <div class="section">
            <div class="section-title">二、核心股票池 + 分批建仓方案</div>
            <table>
                <tr>
                    <th rowspan="2">代码</th><th rowspan="2">名称</th><th rowspan="2">赛道</th>
                    <th rowspan="2">CANSLIM<br>评分</th><th rowspan="2">买入信号</th>
                    <th rowspan="2">现价</th><th colspan="3">买点价格</th>
                    <th rowspan="2">止损价<br>(跌幅)</th>
                    <th colspan="2">第一批(40%)</th><th colspan="2">第二批(60%)</th>
                    <th rowspan="2">最大亏损</th>
                </tr>
                <tr>
                    <th>激进</th><th>稳健</th><th>保守</th>
                    <th>股数</th><th>金额</th>
                    <th>加仓价(+3%)</th><th>股数</th>
                </tr>
"""
        for stock in pool:
            score = stock["factor_score"]
            score_class = "score-high" if score >= 70 else ("score-mid" if score >= 55 else "score-low")
            factors = stock["factor_detail"]

            # 信号标签
            signal_html = ""
            for sig in stock.get("signals", []):
                css = "signal-tag signal-star" if "★" in sig else "signal-tag"
                signal_html += f'<span class="{css}">{sig}</span>'

            risk_tag = '<span style="color:#52C41A">通过</span>' if stock["pass_risk"] else '<span style="color:#FF4D4F">未通过</span>'

            html += f"""
                <tr>
                    <td>{stock['code']}</td>
                    <td style="font-weight:bold">{stock['name']}</td>
                    <td><span class="sector-tag">{stock['sector']}</span></td>
                    <td class="{score_class}">{score}</td>
                    <td style="text-align:left">{signal_html if signal_html else '<span class="note">-</span>'}</td>
                    <td class="price">{stock['current_price']:.2f}</td>
                    <td class="buy-price">{stock['aggressive_buy']:.2f}</td>
                    <td class="buy-price">{stock['moderate_buy']:.2f}</td>
                    <td class="buy-price">{stock['conservative_buy']:.2f}</td>
                    <td class="stop-price">{stock['stop_loss']:.2f}<br><span class="note">(-{stock['stop_loss_pct']}%)</span></td>
                    <td><span class="batch-box">{stock['first_shares']}股<br>{stock['first_amount']:,.0f}元</span></td>
                    <td><span class="batch-box">{stock['add_price']:.2f}<br>+3%触发</span></td>
                    <td><span class="batch-box">{stock['second_shares']}股<br>{stock['second_amount']:,.0f}元</span></td>
                    <td>{stock['max_loss']:,.0f}元<br>{risk_tag}</td>
                </tr>
"""
        html += """
            </table>
        </div>
"""
    else:
        reason_text = "大盘处于下降趋势，不建议买入" if market["market_state"] == "down" else "当前无符合条件的股票入选"
        html += f"""
        <div class="section">
            <div class="section-title">二、核心股票池</div>
            <p style="text-align:center;color:#999;padding:30px">{reason_text}，建议观望等待</p>
        </div>
"""

    # 操作指南
    html += f"""
        <div class="guide">
            <h3>CANSLIM选股体系 + 分批建仓操作指南</h3>
            <ol style="margin:5px 0;padding-left:20px;line-height:1.8">
                <li><b>★缩量回踩20日均线</b>：成交量较20日均量萎缩30%以上，价格回踩MA20不跌破 → <span style="color:#FF4D4F">核心买点</span></li>
                <li><b>分批建仓</b>：第一批40%试仓（买点附近），浮盈≥3%再加第二批60%（确认趋势）</li>
                <li><b>同步设置止损</b>：买入即挂10%初始止损，浮盈后上移移动止损保护利润</li>
                <li><b>激进买点</b>：现价+0.5%，适合强势突破股直接追入</li>
                <li><b>稳健买点</b>：MA5/MA10附近，等待短期回踩挂单（推荐）</li>
                <li><b>保守买点</b>：MA20附近，等待深度回调挂单</li>
                <li>在东方财富APP中设置<b>定价买入</b>条件单，触发价=买点，委托价=触发价×1.01</li>
                <li>同时设置<b>定价卖出</b>条件单作为止损保护（止损价=买入价×90%）</li>
            </ol>
        </div>

        <div class="guide" style="background:#F9F0FF;border-color:#D3ADF7">
            <h3 style="color:#531DAB">CANSLIM因子说明</h3>
            <table style="font-size:12px;margin:5px 0">
                <tr><td style="width:120px"><b style="color:#722ED1">N 新事物(20分)</b></td><td>股价创近60日/半年新高，突破形态</td></tr>
                <tr><td><b style="color:#1890FF">S 供需(20分)</b></td><td>缩量回踩MA20、放量突破、量价配合</td></tr>
                <tr><td><b style="color:#52C41A">L 龙头(20分)</b></td><td>RPS相对强弱排名、行业涨幅领先</td></tr>
                <tr><td><b style="color:#FA8C16">CAI 基本面(20分)</b></td><td>业绩增速(C)、年度增长(A)、机构认同(I)</td></tr>
            </table>
            <p class="note">注：CAI因子需在config.py的FUNDAMENTAL_DATA中配置各股财报数据，未配置则取中性分</p>
        </div>

        <div class="footer">
            本报告由CANSLIM选股引擎V2自动生成 | 仅供参考，不构成投资建议<br>
            股市有风险，投资需谨慎 | 总资金: """ + f"{config.TOTAL_CAPITAL:,.0f}" + """元
        </div>
    </div>
</div>
</body>
</html>"""

    return html


# ============================================================
# 七、发送选股邮件
# ============================================================

def send_screener_email(result: dict) -> bool:
    """生成并发送选股报告邮件"""
    from notify.email_notify import send_email

    next_day = datetime.date.today() + datetime.timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += datetime.timedelta(days=1)
    next_trade_day = next_day.strftime("%Y-%m-%d")

    market_text = {"up": "可操作", "neutral": "震荡", "down": "风险"}.get(
        result["market_info"]["market_state"], "")

    subject = f"[CANSLIM选股] {next_trade_day} | {market_text} | {result['qualified_count']}只入选"
    html_content = generate_screener_report_html(result)

    return send_email(subject, html_content)


# ============================================================
# 测试入口
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=" * 50)
    print("  CANSLIM选股引擎 V2.0 - 测试")
    print("=" * 50)
    print("\n请通过 main.py 运行完整流程")
    print("\n[OK] 选股引擎V2模块加载成功")
