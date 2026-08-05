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
import json
import sys
import logging
import datetime
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
# FIX: 移除未使用的calc_first_batch导入（买点计划由本文件calculate_buy_plan自行计算）

logger = logging.getLogger(__name__)

# ============================================================
# 基本面数据配置（CANSLIM的C/A/I因子）
# ============================================================
# 格式: {"股票代码": {"eps_growth_q": xx, "eps_growth_3y": xx, "has_institution": bool}}
# eps_growth_q: 单季度扣非净利润同比增速(%)
# eps_growth_3y: 近3年净利润复合增速(%)
# has_institution: 是否有机构持仓/北向加仓
# 未配置的个股使用默认中性值
FUNDAMENTAL_DATA = {}

# V2.3-P2: 全市场RPS排名缓存（每次运行加载一次）
_MARKET_RPS_CACHE = None  # [float] 全市场60日涨幅列表

# V3.0: 隔夜外盘联动缓存（每次运行加载一次）
_OVERNIGHT_CACHE = None

def _get_overnight_data() -> dict:
    """获取隔夜外盘数据（缓存，避免每只股票重复请求）"""
    global _OVERNIGHT_CACHE
    if _OVERNIGHT_CACHE is not None:
        return _OVERNIGHT_CACHE
    try:
        from trading_system.strategy.overnight_linkage import OvernightLinkage
        ol = OvernightLinkage()
        _OVERNIGHT_CACHE = ol.fetch_global_indices()
    except Exception:
        _OVERNIGHT_CACHE = {"available": False}
    return _OVERNIGHT_CACHE

def _get_stock_sector(code: str) -> str:
    """从SECTOR_CANDIDATES反查股票所属赛道"""
    for sector_name, sector_info in config.SECTOR_CANDIDATES.items():
        if code in sector_info.get("stocks", {}):
            return sector_name
    return ""


def _load_market_rps() -> list:
    """
    加载全市场60日涨幅数据（用于RPS排名）
    
    数据源: akshare stock_zh_a_spot_em() 的 '60日涨跌幅' 字段
    失败时返回空列表，降级为候选池内排名
    """
    global _MARKET_RPS_CACHE
    if _MARKET_RPS_CACHE is not None:
        return _MARKET_RPS_CACHE
    
    _MARKET_RPS_CACHE = []
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty and "60日涨跌幅" in df.columns:
            changes = pd.to_numeric(df["60日涨跌幅"], errors="coerce").dropna().tolist()
            _MARKET_RPS_CACHE = changes
            logger.info(f"[选股引擎V3] 全市场RPS加载成功: {len(changes)}只 (用于L因子排名)")
        else:
            logger.info("[选股引擎V3] 全市场RPS: 无60日涨跌幅字段，降级为池内排名")
    except Exception as e:
        logger.info(f"[选股引擎V3] 全市场RPS加载失败({e})，降级为池内排名")
    
    return _MARKET_RPS_CACHE


# V2.4: 资金异动数据缓存（龙虎榜+主力资金流，按日缓存）
_FUND_FLOW_CACHE = {"date": None, "lhb": {}, "flow": {}}


def _load_fund_flow_data(stock_codes: list) -> dict:
    """
    V2.4: 批量拉取候选池内股票的龙虎榜/资金流数据
    返回: {code: {"bonus": int, "signals": [str]}}
    网络失败时静默降级（不加分、不报错）
    """
    global _FUND_FLOW_CACHE
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    
    if _FUND_FLOW_CACHE["date"] == today_str and (_FUND_FLOW_CACHE["lhb"] or _FUND_FLOW_CACHE["flow"]):
        logger.info("[选股引擎V3] 资金数据使用当日缓存")
    else:
        _FUND_FLOW_CACHE = {"date": today_str, "lhb": {}, "flow": {}}
        # 1. 龙虎榜
        try:
            import akshare as ak
            df_lhb = ak.stock_lhb_detail_em(
                start_date=today_str.replace("-", ""),
                end_date=today_str.replace("-", "")
            )
            if df_lhb is not None and not df_lhb.empty:
                code_col = "代码" if "代码" in df_lhb.columns else df_lhb.columns[0]
                buy_col = "买入额" if "买入额" in df_lhb.columns else None
                sell_col = "卖出额" if "卖出额" in df_lhb.columns else None
                if buy_col and sell_col:
                    for _, row in df_lhb.iterrows():
                        code = str(row.get(code_col, "")).zfill(6)
                        net = float(row.get(buy_col, 0)) - float(row.get(sell_col, 0))
                        if code in stock_codes:
                            _FUND_FLOW_CACHE["lhb"][code] = _FUND_FLOW_CACHE["lhb"].get(code, 0) + net
                logger.info(f"[选股引擎V3] 龙虎榜: {len(_FUND_FLOW_CACHE['lhb'])}只有记录")
        except Exception as e:
            logger.warning(f"[选股引擎V3] 龙虎榜获取失败(不影响主流程): {e}")
        # 2. 主力资金流
        try:
            import akshare as ak
            consecutive_days = getattr(config, 'FUND_FLOW_CONSECUTIVE_DAYS', 3)
            flow_count = 0
            for code in stock_codes[:30]:
                try:
                    mkt = "sh" if code.startswith("6") else "sz"
                    df_flow = ak.stock_individual_fund_flow(stock=code, market=mkt)
                    if df_flow is not None and not df_flow.empty and len(df_flow) >= consecutive_days:
                        recent = df_flow.tail(consecutive_days)
                        main_col = "主力净流入-净额" if "主力净流入-净额" in recent.columns else None
                        if main_col and all(pd.to_numeric(recent[main_col], errors="coerce") > 0):
                            _FUND_FLOW_CACHE["flow"][code] = consecutive_days
                            flow_count += 1
                except Exception:
                    continue
            if flow_count > 0:
                logger.info(f"[选股引擎V3] 资金流: {flow_count}只主力连续{consecutive_days}日净流入")
        except Exception as e:
            logger.warning(f"[选股引擎V3] 资金流获取失败(不影响主流程): {e}")
    
    # 组装结果
    lhb_threshold = getattr(config, 'LHB_NET_BUY_THRESHOLD', 5000e4)
    bonus_max = getattr(config, 'FUND_FLOW_BONUS_MAX', 5)
    result = {}
    for code in stock_codes:
        bonus = 0
        signals = []
        if _FUND_FLOW_CACHE["lhb"].get(code, 0) > lhb_threshold:
            bonus += 3
            signals.append("龙虎榜净买入")
        if _FUND_FLOW_CACHE["flow"].get(code, 0) > 0:
            bonus += 2
            signals.append("主力连续流入")
        bonus = min(bonus, bonus_max)
        if bonus > 0:
            result[code] = {"bonus": bonus, "signals": signals}
    return result


def _load_fundamental_data(stock_codes: list) -> dict:
    """
    通过 FundamentalAnalyzer 批量获取基本面数据，填充 FUNDAMENTAL_DATA
    
    参数:
        stock_codes: 需要获取基本面数据的股票代码列表
    返回:
        {code: {"eps_growth_q": xx, "eps_growth_3y": xx, "has_institution": bool}}
    """
    global FUNDAMENTAL_DATA
    try:
        from strategy.fundamental import FundamentalAnalyzer
    except ImportError as e:
        logger.warning(f"[基本面] FundamentalAnalyzer导入失败，CAI因子使用中性默认值: {e}")
        return {}

    fa = FundamentalAnalyzer()
    loaded_count = 0
    failed_count = 0

    for code in stock_codes:
        try:
            data = fa.get_canslim_fundamental(code)
            if data and (data.get("eps_growth_q", 0) != 0
                         or data.get("eps_growth_3y", 0) != 0
                         or data.get("has_institution") is True):
                FUNDAMENTAL_DATA[code] = data
                loaded_count += 1
            else:
                failed_count += 1
        except Exception as e:
            logger.debug(f"[基本面] {code}数据获取异常: {e}")
            failed_count += 1

    logger.info(f"[基本面] 数据加载完成: 成功{loaded_count}只 / 失败{failed_count}只 / 共{len(stock_codes)}只")
    if loaded_count > 0:
        logger.info(f"[基本面] 已填充FUNDAMENTAL_DATA，CAI因子将基于真实基本面数据计算")
    else:
        logger.warning(f"[基本面] 未能获取任何有效基本面数据，CAI因子将使用中性默认值")

    return FUNDAMENTAL_DATA


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
    # FIX P2: 科创板/创业板20%涨跌幅板下，10%振幅属常态，上限放宽至MAX_HIGH_AMPLITUDE_DAYS_20CM(5)
    max_amp_days = getattr(config, 'MAX_HIGH_AMPLITUDE_DAYS', 3)
    if str(code).startswith(("300", "301", "688")):
        max_amp_days = getattr(config, 'MAX_HIGH_AMPLITUDE_DAYS_20CM', max_amp_days)
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
    # FIX P2: 强/弱阈值改为从 SCREENER_CONFIG 读取（保留默认值，不在config.py新增键）
    _strong_th = getattr(config, 'SCREENER_CONFIG', {}).get("sector_strong_score", 60)
    _weak_th = getattr(config, 'SCREENER_CONFIG', {}).get("sector_weak_score", 40)
    strong = [s["sector"] for s in sectors if s["score"] >= _strong_th and s["is_valid"]]
    weak = [s["sector"] for s in sectors if (s["score"] < _weak_th) or (not s["is_valid"] and s["ma60_ratio"] < 0.3)]

    # V3.2: 缓存赛道评分供行业轮动加分使用
    try:
        _rot_cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       'data', 'sector_rotation_cache.json')
        _rot_scores = {s["sector"]: s["score"] for s in sectors}
        with open(_rot_cache_path, 'w', encoding='utf-8') as _rf:
            json.dump({"sector_scores": _rot_scores, "strong": strong, "weak": weak,
                       "updated": datetime.datetime.now().isoformat()}, _rf, ensure_ascii=False)
    except Exception:
        pass

    return {"sectors": sectors, "strong": strong, "weak": weak}


# ============================================================
# 三、CANSLIM多因子打分（第二步）
# ============================================================

def canslim_score(df: pd.DataFrame, code: str, all_dfs: dict = None, market_state: str = "up") -> dict:
    """
    CANSLIM量化打分（0-100分）
    
    技术面可量化部分:
    - N因子（新事物/新高）20分：股价创近60日新高、突破形态
    - S因子（供给需求/量价）V3.2: 满分从20降至10，放量突破升权/缩量回踩降权
    - L因子（领涨龙头）20分：RPS相对强弱、行业内涨幅排名
    - C/A/I因子（基本面）20分：从FUNDAMENTAL_DATA读取
    - M因子（大盘方向）20分：从check_market_direction传入
    
    买入信号加权:
    - 缩量回踩20日均线（经典买点）→ 额外+10分
    - 放量突破60日新高（启动信号）→ 额外+10分
    
    V2.7: 新增market_state参数，震荡市N因子条件放宽
    """
    if len(df) < 60:
        return {"total_score": 0, "factors": {}, "signals": [], "reason": "数据不足"}

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    current_price = latest["close"]
    factors = {}
    signals = []

    # ---- N因子：新事物/新高（20分）----
    # V2.7: 震荡市适配 - 弱势/震荡市放宽新高条件，避免N因子系统性归零
    is_weak_market = market_state in ("down", "weak", "neutral")
    n_score = 0
    high_60d = df["high"].iloc[-60:].max()
    high_120d = df["high"].iloc[-120:].max() if len(df) >= 120 else high_60d
    high_20d = df["high"].iloc[-20:].max() if len(df) >= 20 else high_60d

    if is_weak_market:
        # === V2.7 震荡市宽松模式 ===
        # 创60日新高（满分条件不变）
        if current_price >= high_60d * 0.98:
            n_score += 12
            if current_price >= high_60d:
                signals.append("创60日新高")
        # 创120日新高
        if len(df) >= 120 and current_price >= high_120d * 0.98:
            n_score += 8
            if current_price >= high_120d:
                signals.append("创半年新高")
        # V2.7新增: 震荡市下"距20日高点<3%"也可得部分分（强势市中无此加分）
        if n_score == 0 and len(df) >= 20:
            dist_20d_high = (current_price - high_20d) / high_20d * 100
            if dist_20d_high >= -3:  # 距20日高点差距<3%
                n_score += 8
                signals.append("接近20日高点")
            elif dist_20d_high >= -5:  # 距20日高点差距<5%
                n_score += 5
                signals.append("逼近20日高点")
        # 距离60日新高的位置
        dist_from_high = (current_price - high_60d) / high_60d * 100
        if -5 <= dist_from_high <= 0:
            n_score += 5
        elif dist_from_high > 0:
            n_score += 8
        # V2.7: 震荡市N因子上限仍为20分（不应惩罚真创新高的股票）
        factors["N_新事物"] = min(n_score, 20)
    else:
        # === 强势市原始逻辑（不变）===
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

    # ---- S因子：供给需求/量价配合（V3.2: 满分从20降至10）----
    # V3.2回测诊断: S因子IC=-0.28%(整体), 牛市-1.43%, 占总分20%权重无效
    # 修复: ①满分压缩至10 ②"缩量回踩"降权(牛市反向) ③"放量突破"升权
    s_score = 0
    vol = latest["volume"]
    vol_ma20 = df["volume"].iloc[-20:].mean()
    vol_ratio = vol / vol_ma20 if vol_ma20 > 0 else 1

    # ★ 放量突破（V3.2升权: 回测显示放量突破是唯一正向信号）
    high_60d_for_breakout = df["high"].iloc[-60:].max() if len(df) >= 60 else current_price
    is_near_high = current_price >= high_60d_for_breakout * 0.98
    if vol_ratio > 1.5 and current_price > prev["close"] and is_near_high:
        s_score += 7   # 放量突破60日新高（核心买点，V3.2: 从10升至7/10满分）
        signals.append("★放量突破新高")
        logger.info(f"  [买点] {code} 放量突破: vol_ratio={vol_ratio:.2f} price={current_price:.2f} high60={high_60d_for_breakout:.2f}")
    elif vol_ratio > 1.5 and current_price > prev["close"]:
        s_score += 4   # 放量上涨但未突破新高
        signals.append("放量上涨")
    elif vol_ratio > 1.2 and current_price > prev["close"]:
        s_score += 2   # 温和放量

    # 缩量回踩MA20（V3.2降权: 牛市中“缩量回踩”常意味无人问津而非蓄势）
    if "ma20" in df.columns and not pd.isna(latest.get("ma20", None)):
        ma20_val = latest["ma20"]
        dist_to_ma20 = (current_price - ma20_val) / ma20_val
        day_low = latest.get("low", current_price)
        is_shrink = vol_ratio < 0.7
        touch_ma20 = day_low <= ma20_val * 1.01
        hold_above_ma20 = current_price > ma20_val
        is_pullback = -0.03 <= dist_to_ma20 <= 0.02

        if is_shrink and touch_ma20 and hold_above_ma20:
            s_score += 3   # V3.2: 从20降至3（缩量回踩在牛市反向，仅保留微弱加分）
            signals.append("缩量回踩MA20")
        elif is_shrink and is_pullback and hold_above_ma20:
            s_score += 2   # V3.2: 从16降至2
        elif is_pullback and hold_above_ma20:
            s_score += 1   # V3.2: 从12降至1

    # 下跌缩量（健康的量价关系，保留微弱加分）
    if current_price < prev["close"] and vol_ratio < 0.7:
        s_score += 1
        signals.append("下跌缩量")

    factors["S_供需"] = min(s_score, 10)  # V3.2: 上限从20压缩到10
    # V2.8回测优化: 弱势市场下S因子打折
    if is_weak_market and factors["S_供需"] > 5:
        factors["S_供需"] = int(factors["S_供需"] * 0.7)

    # V3.2: 换手率修正（知识库规则: 3-10%健康, >15%过度换手扣分）
    try:
        _turnover = float(latest.get("turnover", 0) or 0)
        if _turnover > 15:
            factors["S_供需"] = max(0, factors["S_供需"] - 3)
            signals.append(f"换手率{_turnover:.0f}%过高-3")
        elif 3 <= _turnover <= 10:
            factors["S_供需"] = min(10, factors["S_供需"] + 1)
    except (TypeError, ValueError):
        pass

    # ---- L因子：领涨龙头/相对强弱（20分）----
    l_score = 0

    # RPS（Relative Price Strength）：近60日涨幅在所有候选股中的排名
    change_60d = (current_price / df["close"].iloc[-60] - 1) * 100 if len(df) >= 60 else 0
    change_20d = (current_price / df["close"].iloc[-20] - 1) * 100 if len(df) >= 20 else 0

    # 计算RPS排名
    # V2.3-P2: 优先使用全市场排名，失败时降级为候选池内排名
    rps_rank = 0.5  # 默认中位
    market_changes = _load_market_rps()
    if market_changes and len(market_changes) > 100:
        # 全市场排名（5000+只）
        rank = sum(1 for x in market_changes if x <= change_60d)
        rps_rank = rank / len(market_changes)
    elif all_dfs:
        # 降级: 候选池内排名
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
    # V2.7: 无数据时给中性基础分8分，避免系统性偏低（V2.3"不给分"规则废止）
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

    # A因子：3年复合增速
    eps_3y = fund_data.get("eps_growth_3y", None)
    if eps_3y is not None:
        if eps_3y > 30:
            cai_score += 7
        elif eps_3y > 20:
            cai_score += 5
        elif eps_3y > 10:
            cai_score += 2

    # I因子：机构认同
    has_inst = fund_data.get("has_institution", None)
    if has_inst is True:
        cai_score += 5
    elif has_inst is False:
        cai_score -= 2

    # V2.7: 无有效基本面数据时给中性基础分（V3.2: 从固定8分改为动量代理4-12分）
    # V3.2回测诊断: 固定8分导致80%+股票无区分度，改用“20日动量”作为基本面代理
    has_positive_signal = (
        (eps_q is not None and eps_q > 0) or
        (eps_3y is not None and eps_3y > 0) or
        (has_inst is True)
    )
    if not has_positive_signal:
        # V3.2: 动量代理 - 20日涨幅>10%给10分, 5-10%给8分, 0-5%给6分, <0%给4分
        _proxy_chg = (current_price / df["close"].iloc[-21] - 1) * 100 if len(df) >= 21 else 0
        if _proxy_chg > 10:
            cai_score = 10
        elif _proxy_chg > 5:
            cai_score = 8
        elif _proxy_chg > 0:
            cai_score = 6
        else:
            cai_score = 4
        signals.append(f"CAI动量代理({_proxy_chg:+.0f}%)")

    factors["CAI_基本面"] = max(0, min(cai_score, 20))

    # ---- 综合评分（V3.2: IC动态降权）----
    # 读取IC历史，对持续负IC的因子自动降权
    _ic_weights = _get_ic_factor_weights()
    _n_w = _ic_weights.get("N_新事物", 1.0)
    _s_w = _ic_weights.get("S_供需", 1.0)
    _l_w = _ic_weights.get("L_龙头", 1.0)
    _cai_w = _ic_weights.get("CAI_基本面", 1.0)
    _p_w = _ic_weights.get("P_前瞻", 1.0)
    total = (factors["N_新事物"] * _n_w + factors["S_供需"] * _s_w +
             factors["L_龙头"] * _l_w + factors["CAI_基本面"] * _cai_w)

    # ---- 前瞻性预测加分（0-10分）----
    # V2.8回测优化: P因子从0-20缩减为0-10（回测显示P因子高分组20d=+1.38% vs 低分组=+2.09%，差=-0.72%，负相关）
    # 原因: 动量加速/板块启动信号在A股常标记顶部而非底部，降低权重减少追高风险
    prediction = predict_forward(df, code)
    factors["P_前瞻"] = min(prediction["score"], 10)  # 上限从20压缩到10
    signals.extend(prediction["signals"])

    # ---- V3.0: 外盘前瞻加分（0-3分，叠加到P因子）----
    # 约束: 只加不减 | P因子上限仍为10 | 下跌市无效（系统性风险优先）
    if market_state != "down":
        _on_data = _get_overnight_data()
        if _on_data.get("available"):
            _sector = _get_stock_sector(code)
            if _sector:
                _impact = _on_data.get("sector_impacts", {}).get(_sector, {})
                _foreign_bonus = min(max(_impact.get("score", 0), 0), config.OVERNIGHT_MAX_BONUS)
                if _foreign_bonus > 0:
                    factors["P_前瞻"] = min(factors["P_前瞻"] + _foreign_bonus, 10)
                    signals.append(f"外盘利好+{_foreign_bonus:.0f}({_impact.get('reason', '')})")

    total += factors["P_前瞻"] * _p_w

    # ---- V2.3-P1: 周线共振验证（V3.2增强: 多时间框架确认）----
    # 日线MA20 + 周线MA10(MA50) + 月线MA5(MA100) 三重共振
    weekly_bonus = 0
    if len(df) >= 55:
        ma50 = df["close"].rolling(50).mean()
        ma50_now = ma50.iloc[-1]
        ma50_5d_ago = ma50.iloc[-6] if len(ma50) >= 6 else ma50_now
        if not pd.isna(ma50_now) and not pd.isna(ma50_5d_ago):
            weekly_up = ma50_now > ma50_5d_ago and current_price > ma50_now
            weekly_down = ma50_now < ma50_5d_ago and current_price < ma50_now
            
            if weekly_up:
                weekly_bonus = 5
                signals.append("周线共振↑")
            elif weekly_down:
                weekly_bonus = -5
                signals.append("周线偏弱↓")
            
            # V3.2: 月线确认（MA100向上 = 月线趋势）
            if len(df) >= 105:
                ma100 = df["close"].rolling(100).mean()
                ma100_now = ma100.iloc[-1]
                ma100_10d_ago = ma100.iloc[-11] if len(ma100) >= 11 else ma100_now
                if not pd.isna(ma100_now) and not pd.isna(ma100_10d_ago):
                    monthly_up = ma100_now > ma100_10d_ago and current_price > ma100_now
                    monthly_down = ma100_now < ma100_10d_ago and current_price < ma100_now
                    
                    # 三重共振: 日线MA20上 + 周线上 + 月线上 → 额外+3
                    ma20_val = df["close"].rolling(20).mean().iloc[-1]
                    daily_up = not pd.isna(ma20_val) and current_price > ma20_val
                    
                    if weekly_up and monthly_up and daily_up:
                        weekly_bonus += 3  # 三重共振加分
                        signals.append("★三重共振(日+周+月)")
                    elif weekly_down and monthly_down:
                        weekly_bonus -= 2  # 周月双弱额外减分
                        signals.append("周月双弱↓")
    factors["W_周线"] = weekly_bonus
    total += weekly_bonus

    # ---- V3.2: 均值回归加分（弱势市场专用）----
    # 回测诊断: 熊市/震荡市中趋势策略失效，均值回归有效
    # 条件: RSI<30 + 触及布林带下轨 + 缩量企稳 → +5分
    mr_bonus = 0
    if is_weak_market and len(df) >= 20:
        try:
            _rsi = df["close"].diff().apply(lambda x: max(x, 0)).rolling(14).mean() / \
                   (df["close"].diff().abs().rolling(14).mean() + 1e-10) * 100
            _rsi_now = _rsi.iloc[-1] if not pd.isna(_rsi.iloc[-1]) else 50
            _bb_mid = df["close"].rolling(20).mean().iloc[-1]
            _bb_std = df["close"].rolling(20).std().iloc[-1]
            _bb_lower = _bb_mid - 2 * _bb_std if not pd.isna(_bb_std) else _bb_mid
            _vol_ma5 = df["volume"].rolling(5).mean().iloc[-1]
            _vol_now = df["volume"].iloc[-1]
            
            _mr_conditions = 0
            if _rsi_now < 30:
                _mr_conditions += 1
            if current_price <= _bb_lower * 1.02:
                _mr_conditions += 1
            if _vol_now < _vol_ma5 * 0.7:  # 缩量企稳
                _mr_conditions += 1
            
            if _mr_conditions >= 2:
                mr_bonus = 5
                signals.append(f"均值回归+5(RSI={_rsi_now:.0f},触下轨)")
            elif _mr_conditions == 1 and _rsi_now < 25:
                mr_bonus = 3
                signals.append(f"超卖反弹+3(RSI={_rsi_now:.0f})")
        except Exception:
            pass
    factors["MR_回归"] = mr_bonus
    total += mr_bonus

    # ---- V3.2: 事件日历调整（财报/解禁/回购）----
    # 财报前5天减分，解禁前10天减分，回购/股权激励加分
    event_adj = 0
    try:
        from strategy.event_calendar import EventCalendar
        _evt_cal = EventCalendar()
        _evt_risk = _evt_cal.check_event_risk(code, days_ahead=10)
        if _evt_risk.get("block_buy"):
            event_adj = -10  # 高风险事件（财报/解禁）
            signals.append(f"事件风险-10({_evt_risk.get('suggestion', '')})")
        elif _evt_risk.get("risk_level") == "medium":
            event_adj = -5
            signals.append("事件风险-5(中等级)")
        # 正面事件加分（回购/股权激励）
        for evt in _evt_risk.get("events", []):
            if evt.get("type") in ("buyback", "equity_incentive"):
                event_adj += 3
                signals.append(f"正面事件+3({evt.get('type')})")
                break
    except Exception:
        pass
    factors["E_事件"] = event_adj
    total += event_adj

    # ---- V3.2: 北向资金/主力资金流加分 ----
    # 北向资金连续流入+个股主力净流入 → 聪明钱信号
    flow_bonus = 0
    try:
        flow_bonus = _get_capital_flow_bonus(code)
        if flow_bonus > 0:
            signals.append(f"资金流+{flow_bonus}(北向/主力)")
        elif flow_bonus < 0:
            signals.append(f"资金流{flow_bonus}(流出)")
    except Exception:
        pass
    factors["F_资金"] = flow_bonus
    total += flow_bonus

    # ---- V3.2: 行业轮动加分/减分 ----
    # 强势赛道+3，弱势赛道-3，基于sector_rotation模块
    sector_rot_adj = 0
    try:
        sector_rot_adj = _get_sector_rotation_adj(code)
        if sector_rot_adj > 0:
            signals.append(f"强势赛道+{sector_rot_adj}")
        elif sector_rot_adj < 0:
            signals.append(f"弱势赛道{sector_rot_adj}")
    except Exception:
        pass
    factors["R_轮动"] = sector_rot_adj
    total += sector_rot_adj

    return {
        "total_score": round(total, 1),
        "factors": {k: round(v, 1) for k, v in factors.items()},
        "signals": signals,
        "reason": _canslim_reason(factors, signals),
        "rps_rank": round(rps_rank * 100, 1),
        "change_60d": round(change_60d, 2),
        "prediction": prediction,
    }


# ============================================================
# IC动态降权辅助（V3.2新增）
# ============================================================
_IC_WEIGHTS_CACHE = None
_IC_WEIGHTS_CACHE_TIME = 0

def _get_ic_factor_weights() -> dict:
    """读取IC历史，返回各CANSLIM因子的权重乘数
    
    规则:
    - IC持续为负(5天<-0.02) → 权重0.5（已降权但保留微弱信号）
    - IC衰减(5天|IC|<0.02) → 权重0.7
    - 正常 → 权重1.0
    
    缓存: 每小时刷新一次（避免每只股票重复读取JSON）
    """
    global _IC_WEIGHTS_CACHE, _IC_WEIGHTS_CACHE_TIME
    import time
    now = time.time()
    if _IC_WEIGHTS_CACHE is not None and (now - _IC_WEIGHTS_CACHE_TIME) < 3600:
        return _IC_WEIGHTS_CACHE
    
    weights = {"N_新事物": 1.0, "S_供需": 1.0, "L_龙头": 1.0, "CAI_基本面": 1.0, "P_前瞻": 1.0}
    try:
        ic_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'data', 'ic_history.json')
        if os.path.exists(ic_path):
            with open(ic_path, 'r', encoding='utf-8') as f:
                ic_data = json.load(f)
            for factor_key, display_name in [("N", "N_新事物"), ("S", "S_供需"),
                                             ("L", "L_龙头"), ("CAI", "CAI_基本面"),
                                             ("P", "P_前瞻")]:
                records = ic_data.get(factor_key, ic_data.get(display_name, []))
                if len(records) >= 5:
                    recent_ics = [r['ic'] if isinstance(r, dict) else r for r in records[-5:]]
                    avg_ic = sum(recent_ics) / len(recent_ics)
                    if avg_ic < -0.02:
                        weights[display_name] = 0.5  # IC持续为负 → 半权
                    elif all(abs(ic) < 0.02 for ic in recent_ics):
                        weights[display_name] = 0.7  # IC衰减 → 7折
    except Exception:
        pass  # 读取失败不影响正常评分
    
    _IC_WEIGHTS_CACHE = weights
    _IC_WEIGHTS_CACHE_TIME = now
    return weights


def record_canslim_ic(all_scores: list, data_dict: dict, forward_days: int = 20):
    """V3.2: 记录CANSLIM五因子IC到ic_history.json
    
    在每日选股完成后调用，计算各因子得分与forward_days后收益的Rank IC。
    键名为 "N", "S", "L", "CAI", "P"，与_get_ic_factor_weights()读取逻辑匹配。
    
    参数:
        all_scores: [{"code": str, "factors": {"N_新事物": float, ...}, "total_score": float}]
        data_dict: {code: DataFrame} 用于计算forward return
        forward_days: 前看收益天数
    """
    try:
        import scipy.stats as stats
    except ImportError:
        # 无scipy时用简化版rank相关
        stats = None

    if not all_scores or len(all_scores) < 5:
        return  # 样本不足

    # 计算每只股票的forward return
    factor_returns = []  # [(factor_dict, forward_return)]
    for item in all_scores:
        code = item.get("code", "")
        factors = item.get("factors", {})
        df = data_dict.get(code)
        if df is None or len(df) < forward_days + 1:
            continue
        # 用最新收盘价 vs forward_days前的收盘价
        current_close = df["close"].iloc[-1]
        past_close = df["close"].iloc[-(forward_days + 1)]
        if past_close <= 0:
            continue
        fwd_return = (current_close - past_close) / past_close
        factor_returns.append((factors, fwd_return))

    if len(factor_returns) < 5:
        return

    # 计算各因子的Rank IC
    factor_keys = [("N_新事物", "N"), ("S_供需", "S"), ("L_龙头", "L"),
                   ("CAI_基本面", "CAI"), ("P_前瞻", "P")]
    
    returns = [fr[1] for fr in factor_returns]
    today_str = datetime.date.today().isoformat()

    # 读取现有ic_history
    ic_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'data', 'ic_history.json')
    ic_data = {}
    if os.path.exists(ic_path):
        try:
            with open(ic_path, 'r', encoding='utf-8') as f:
                ic_data = json.load(f)
        except Exception:
            ic_data = {}

    for display_name, key in factor_keys:
        factor_values = [fr[0].get(display_name, 0) for fr in factor_returns]
        # 计算Rank IC (Spearman相关)
        if stats:
            ic, _ = stats.spearmanr(factor_values, returns)
        else:
            # 简化版: Pearson相关
            n = len(factor_values)
            mean_f = sum(factor_values) / n
            mean_r = sum(returns) / n
            cov = sum((f - mean_f) * (r - mean_r) for f, r in zip(factor_values, returns)) / n
            std_f = (sum((f - mean_f) ** 2 for f in factor_values) / n) ** 0.5
            std_r = (sum((r - mean_r) ** 2 for r in returns) / n) ** 0.5
            ic = cov / (std_f * std_r) if std_f > 0 and std_r > 0 else 0

        if ic != ic:  # NaN check
            ic = 0.0

        # 追加到ic_history
        if key not in ic_data:
            ic_data[key] = []
        # 避免同一天重复记录
        existing_dates = [r.get("date") for r in ic_data[key] if isinstance(r, dict)]
        if today_str not in existing_dates:
            ic_data[key].append({"date": today_str, "ic": round(float(ic), 6)})
            # 保留最近60条
            if len(ic_data[key]) > 60:
                ic_data[key] = ic_data[key][-60:]

    # 写回
    try:
        with open(ic_path, 'w', encoding='utf-8') as f:
            json.dump(ic_data, f, ensure_ascii=False, indent=1)
        logger.info(f"[IC记录] CANSLIM五因子IC已记录 ({today_str})")
    except Exception as e:
        logger.error(f"[IC记录] 写入失败: {e}")


# ============================================================
# 北向资金/主力资金流加分（V3.2新增）
# ============================================================
_FLOW_BONUS_CACHE = None
_FLOW_BONUS_CACHE_TIME = 0

def _get_capital_flow_bonus(code: str) -> int:
    """V3.2: 获取资金流加分（北向+主力）
    
    规则:
    - 北向资金5日净流入>50亿 + 个股主力连续3日净流入 → +3
    - 北向资金5日净流入>20亿 → +2
    - 北向资金5日净流出>30亿 → -2
    - 个股主力连续3日净流出 → -1
    
    缓存: 每小时刷新一次
    """
    global _FLOW_BONUS_CACHE, _FLOW_BONUS_CACHE_TIME
    import time
    now = time.time()
    
    # 加载北向资金大环境（缓存）
    if _FLOW_BONUS_CACHE is None or (now - _FLOW_BONUS_CACHE_TIME) > 3600:
        try:
            from strategy.capital_flow import CapitalFlowAnalyzer
            analyzer = CapitalFlowAnalyzer()
            nb = analyzer.get_northbound_flow()
            _FLOW_BONUS_CACHE = nb
            _FLOW_BONUS_CACHE_TIME = now
        except Exception:
            _FLOW_BONUS_CACHE = {}
            _FLOW_BONUS_CACHE_TIME = now
    
    nb_data = _FLOW_BONUS_CACHE or {}
    bonus = 0
    
    # 北向资金大环境
    nb_5d = nb_data.get("5d_net_inflow", 0)  # 单位: 亿
    if nb_5d > 50:
        bonus += 2
    elif nb_5d > 20:
        bonus += 1
    elif nb_5d < -30:
        bonus -= 2
    
    # 个股主力资金流（从缓存读取）
    try:
        from data.data_loader import load_capital_flow_cache
        flow_cache = load_capital_flow_cache()
        stock_flow = flow_cache.get(code, {})
        consec_in = stock_flow.get("consecutive_inflow_days", 0)
        consec_out = stock_flow.get("consecutive_outflow_days", 0)
        if consec_in >= 3:
            bonus += 1
        elif consec_out >= 3:
            bonus -= 1
    except Exception:
        pass
    
    return max(min(bonus, 3), -2)  # 范围[-2, +3]


# ============================================================
# 行业轮动加分（V3.2新增）
# ============================================================
_SECTOR_ROT_CACHE = None
_SECTOR_ROT_CACHE_TIME = 0

def _get_sector_rotation_adj(code: str) -> int:
    """V3.2: 获取行业轮动加分/减分
    
    规则:
    - 强势赛道(score>=60) → +3
    - 较强赛道(score>=50) → +1
    - 弱势赛道(score<35) → -3
    - 较弱赛道(score<45) → -1
    
    缓存: 每小时刷新
    """
    global _SECTOR_ROT_CACHE, _SECTOR_ROT_CACHE_TIME
    import time
    now = time.time()
    
    if _SECTOR_ROT_CACHE is None or (now - _SECTOR_ROT_CACHE_TIME) > 3600:
        try:
            from strategy.sector_rotation import analyze_sector_rotation
            # 用当前已有的data_dict缓存（如果有的话）
            # 简化版：直接从filter_strong_sectors的结果读取
            rot_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    'data', 'sector_rotation_cache.json')
            if os.path.exists(rot_path):
                with open(rot_path, 'r', encoding='utf-8') as f:
                    rot_data = json.load(f)
                _SECTOR_ROT_CACHE = rot_data.get("sector_scores", {})
            else:
                _SECTOR_ROT_CACHE = {}
        except Exception:
            _SECTOR_ROT_CACHE = {}
        _SECTOR_ROT_CACHE_TIME = now
    
    if not _SECTOR_ROT_CACHE:
        return 0
    
    # 查找股票所属赛道
    sector = _get_stock_sector(code)
    if not sector or sector == "其他":
        return 0
    
    score = _SECTOR_ROT_CACHE.get(sector, 50)  # 默认中性50
    if score >= 60:
        return 3
    elif score >= 50:
        return 1
    elif score < 35:
        return -3
    elif score < 45:
        return -1
    return 0


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
    if factors.get("P_前瞻", 0) >= 8:
        parts.append("★前瞻强")
    return " | ".join(parts) if parts else "一般"


# ============================================================
# 三’、前瞻性预测因子（P因子）
# ============================================================

def predict_forward(df: pd.DataFrame, code: str) -> dict:
    """
    前瞻性预测因子（0-10分，V2.8回测优化后上限从20压缩到10）
    
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
    
    # ---- 1. 动量延续性（V3.2: 从6分降至3分，回测显示追涨信号在熊市反向）----
    # 近5日每日涨跌幅
    if len(df) >= 6:
        recent_5d_changes = []
        for i in range(-5, 0):
            chg = (df["close"].iloc[i] / df["close"].iloc[i-1] - 1) * 100
            recent_5d_changes.append(chg)
        
        up_days = sum(1 for c in recent_5d_changes if c > 0)
        avg_recent_3 = np.mean(recent_5d_changes[-3:])
        avg_early_2 = np.mean(recent_5d_changes[:2])
        
        if up_days >= 4 and avg_recent_3 > avg_early_2 > 0:
            score += 3  # V3.2: 从6降至3（连续上涨且加速，但追涨风险高）
            signals.append("动量加速↑")
        elif up_days >= 3 and avg_recent_3 > 0:
            score += 2  # V3.2: 从4降至2
            signals.append("动量延续")
        elif up_days >= 3 and avg_recent_3 < avg_early_2:
            score += 1  # V3.2: 从2降至1
    
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
            # FIX: 修复“即将突码”笔误，应为“即将突破”
            signals.append("即将突破20日新高")
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
    
    # V2.8回测优化: 均值回归惩罚（防止追高）
    # 回测显示: 20日涨幅>20%的标的后续20日平均回报显著低于市场均值
    if len(df) >= 21:
        change_20d = (current_price / df["close"].iloc[-21] - 1) * 100
        if change_20d > 25:
            score -= 5  # 20日涨庅>25%，过热惩罚
            signals.append("短期过热(-5)")
        elif change_20d > 15:
            score -= 2  # 轻度惩罚
    
    return {
        "score": max(0, min(score, 10)),  # V2.8: 上限从20压缩到10（回测验证P因子负相关）
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
    # FIX: 除零防护（数据异常时current_price可能为0）
    if moderate_buy <= 0:
        moderate_buy = round(current_price * 0.99, 2) if current_price > 0 else 1.0

    # 保守买点：MA20附近（缩量回踩的理想买点）
    if not pd.isna(ma20):
        conservative_buy = round(ma20 * 1.005, 2)
    else:
        conservative_buy = round(current_price * 0.97, 2)

    # ---- 止损价 ----
    # 1. 10%固定止损（底线）
    # FIX P2: 止损基准从current_price改为moderate_buy（与文档"买入价×90%"一致）
    fixed_stop = round(moderate_buy * 0.90, 2)

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
    # FIX: 止损上限统一使用config.INITIAL_STOP_LOSS_PCT（10%），与generate_holdings_report一致
    _max_stop_pct = getattr(config, 'INITIAL_STOP_LOSS_PCT', 0.10)
    final_stop = min(final_stop, round(moderate_buy * (1 - _max_stop_pct), 2))  # 最多10%以内
    # 止损价保护：确保止损价 < 现价（参考report_email.py保护逻辑）
    if final_stop >= current_price:
        final_stop = round(current_price * 0.92, 2)
        logger.warning(f"  [止损保护] {code} 止损价异常，强制调整为现价×92%={final_stop}")
    stop_loss_pct = round((current_price - final_stop) / current_price * 100, 1)
    logger.info(f"  [止损] {code} 最终止损={final_stop}(-{stop_loss_pct}%) | ATR止损={atr_stop} 支撑止损={support_stop} 固定止损={fixed_stop}")

    # 风险等级标注（基于止损距离）
    if stop_loss_pct <= 5:
        risk_level = "低风险(止损≤5%)"
    elif stop_loss_pct <= 8:
        risk_level = "中风险(止损5-8%)"
    else:
        risk_level = "高风险(止损>8%)"

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
        "stop_loss_pct": stop_loss_pct,
        "risk_level": risk_level,
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

def _build_coarse_sector_map() -> dict:
    """
    从 SECTOR_CANDIDATES 自动推导 细粒度赛道(细分) -> 粗粒度候选key 的映射。

    背景: filter_strong_sectors() 按 get_stock_info 的"赛道"(即细分字段)分组，
    输出细粒度赛道名（调味品/AI视觉/保险/军工航空...）；而 SECTOR_CANDIDATES 用
    粗粒度key（大消费/AI数字经济/大金融/军工航天...）。两者粒度不一致会导致
    子串模糊匹配失效（如"大消费"无法匹配"调味品"），使强势配额加分静默失效。
    本映射直接从配置推导，无需硬编码、与配置自动同步。
    """
    mapping = {}
    for sector_name, sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).items():
        mapping[sector_name] = sector_name  # 粗粒度key映射到自身
        for _code, stock in sector_info.get("stocks", {}).items():
            fine = stock.get("细分", sector_name)
            mapping[fine] = sector_name
    return mapping


def _build_code_to_coarse_map() -> dict:
    """股票代码 -> 粗粒度候选赛道key 映射（用于持仓集中度按统一赛道口径聚合）"""
    mapping = {}
    for sector_name, sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).items():
        for _code in sector_info.get("stocks", {}):
            mapping[_code] = sector_name
    return mapping


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
    
    # FIX P0/P1: 通过 细分->粗粒度 映射归一化强弱赛道，替代子串模糊匹配
    # 使 大消费/大金融/军工航天/AI数字经济 能正确继承其子赛道（调味品/保险/军工航空/AI视觉）的强弱
    coarse_map = _build_coarse_sector_map()
    strong_candidates = {coarse_map.get(s, s) for s in sector_result.get("strong", [])}
    weak_candidates = {coarse_map.get(s, s) for s in sector_result.get("weak", [])}
    
    quotas = {}
    for sector_name, sector_info in sector_candidates.items():
        weight = sector_info.get("weight", 0.1)
        # 基础配额 = 总上限 × 权重
        base_quota = max(1, round(total_max * weight))
        
        # 动态调整: 强势+1, 弱势-1（强势优先，保持原 elif 优先级）
        if sector_name in strong_candidates:
            base_quota += 1
        elif sector_name in weak_candidates:
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

    # 基本面数据加载：通过FundamentalAnalyzer获取真实PE/PB/ROE/增速数据
    logger.info("[选股引擎V3] 加载基本面数据（CAI因子）...")
    stock_codes = [code for code in data_dict.keys()
                   if code != "000300"
                   and not code.startswith("300")
                   and not code.startswith("688")
                   and not code.startswith("588")
                   and not code.startswith("159")]
    _load_fundamental_data(stock_codes)

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
        # FIX P0: 按 代码->粗粒度赛道 权威映射聚合持仓市值，
        # 替代 holdings sector 字符串模糊匹配（holdings.json用细粒度名如AI视觉/保险金融，
        # 与 sector_quotas 粗粒度key无法匹配，导致集中度调整此前完全失效）
        code_to_coarse = _build_code_to_coarse_map()
        coarse_map = _build_coarse_sector_map()
        holdings_sector_amount = defaultdict(float)
        for code, pos in holdings.items():
            # 优先用代码映射（权威），回退到sector字符串归一化
            coarse = code_to_coarse.get(code) or coarse_map.get(pos.get("sector", ""), pos.get("sector", ""))
            if not coarse:
                continue
            shares = pos.get("shares", 0)
            price = pos.get("current_price", pos.get("buy_price", 0))
            holdings_sector_amount[coarse] += shares * price
        total_capital = config.TOTAL_CAPITAL
        for sector_name, amount in holdings_sector_amount.items():
            ratio = amount / total_capital if total_capital > 0 else 0
            if ratio > 0.15 and sector_name in sector_quotas:  # 单行业持仓超15%，降低配额
                sector_quotas[sector_name] = max(1, sector_quotas[sector_name] - 1)
                logger.info(f"  持仓集中调整: {sector_name}配额-1（已持仓{ratio:.1%}）")
    logger.info(f"  行业配额: {sector_quotas}")

    # 第二步：硬性筛选 + CANSLIM多因子打分
    logger.info("[选股引擎V3] Step 2: 硬性筛选 + CANSLIM多因子打分...")
    candidates = []
    watch_list = []  # 观察池（弱势行情下相对强势但未达买入标准的）
    filtered_count = 0

    # V2.4: 加载资金异动数据（龙虎榜+主力资金流，失败时静默降级）
    fund_flow_data = _load_fund_flow_data([c for c in data_dict.keys() if c != "000300"])

    # V2.3-P4: 加载冷却期数据（近期被降级/止损的股票不重复选入）
    # V2.7: 冷却期从2天缩短为1天（避免唯一达标股被排除）
    cool_down_days = getattr(config, 'SCREENER_CONFIG', {}).get("cooldown_days", 1)
    _cooling_codes = set()
    try:
        from strategy.pool_manager import PoolManager
        _pm = PoolManager()
        for wcode, winfo in _pm.watch_pool.items():
            if winfo.get("demote_reason"):  # 被降级的股票
                observe_start = winfo.get("observe_start", "")
                try:
                    demote_date = datetime.datetime.strptime(observe_start, "%Y-%m-%d").date()
                    if (datetime.date.today() - demote_date).days <= cool_down_days:
                        _cooling_codes.add(wcode)
                except (ValueError, TypeError):
                    pass
        # 黑名单也加入冷却
        _cooling_codes.update(_pm.blacklist)
        if _cooling_codes:
            logger.info(f"  冷却期过滤: {len(_cooling_codes)}只近期止损/降级股不参与选股")
    except Exception:
        pass  # PoolManager加载失败不影响核心流程
    
    for code, df in data_dict.items():
        if code == "000300":
            continue
        # 过滤创业板(300)和科创板(688)，用户无交易权限
        if code.startswith("300") or code.startswith("688"):
            continue
        # 过滤ETF基金(588/159开头)，不参与个股筛选
        if code.startswith("588") or code.startswith("159"):
            continue
        # V2.3: 退市/停牌黑名单硬过滤
        if code in getattr(config, 'DELISTED_STOCKS', set()):
            logger.info(f"  {code}: [退市黑名单] 已退市/不可交易，跳过")
            filtered_count += 1
            continue
        # V2.7: 冷却期优化 - 不完全排除，而是评分后标注"达标但冷却中"
        is_cooling = code in _cooling_codes
        if is_cooling:
            logger.info(f"  {code}: [冷却期] 近期止损/降级，{cool_down_days}天内不推荐买入（仍参与评分）")
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

        factor_result = canslim_score(df, code, data_dict, market_state=market_state)
        
        # V2.4: 资金异动加分（S因子补充，最多+5分）
        ff_info = fund_flow_data.get(code)
        if ff_info and ff_info["bonus"] > 0:
            factor_result["total_score"] += ff_info["bonus"]
            factor_result["factors"]["资金异动"] = ff_info["bonus"]
            factor_result["signals"].extend(ff_info["signals"])
        
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
            "is_cooling": is_cooling,  # V2.7: 冷却期标记
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

    # 第三步：V2.4 固定输出前10只 + 买入推荐分界线
    logger.info("[选股引擎V3] Step 3: 固定输出前10 + 买入推荐分界线...")
    stock_pool = []
    sector_selected_count = defaultdict(int)
    
    # V2.7: 买入推荐分界线动态化（根据market_state选择不同阈值）
    _screener_cfg = getattr(config, 'SCREENER_CONFIG', {})
    if market_state in ("down", "weak", "neutral"):
        min_buy_score = _screener_cfg.get("min_buy_score_weak", 35)
    else:
        min_buy_score = _screener_cfg.get("min_buy_score_strong", 50)
    logger.info(f"  买入线: {min_buy_score}分 (market_state={market_state})")
    
    # V2.4: 取消min_score硬性截断，按评分降序固定取前10只
    for cand in candidates:
        if len(stock_pool) >= max_stocks:
            break
        # 跳过已持仓股票
        if holdings and cand["code"] in holdings:
            continue
        
        # 行业配额检查（仅对"推荐买入"级别生效，观察级不受配额限制）
        sector_group = cand["sector_group"]
        is_buy_recommend = cand["score"] >= min_buy_score
        # V2.7: 冷却期股票评分达标但不推荐买入，降级为观察并标注
        if is_buy_recommend and cand.get("is_cooling", False):
            is_buy_recommend = False
            cand["cooling_note"] = f"评分{cand['score']}达标但冷却期中(近期止损/降级)"
            logger.info(f"  [冷却] {cand['code']} 评分{cand['score']}>=买入线{min_buy_score}，但冷却期中，降级为观察")
        if is_buy_recommend:
            quota = sector_quotas.get(sector_group, max_per_sector)
            if sector_selected_count[sector_group] >= quota:
                # 配额已满，降级为观察
                is_buy_recommend = False
        
        if is_buy_recommend:
            # 推荐买入：生成完整买点计划
            buy_plan = calculate_buy_plan(cand["df"], cand["code"], {
                "total_score": cand["score"],
                "factors": cand["factors"],
                "signals": cand["signals"],
                "reason": cand["reason"],
                "rps_rank": cand["rps_rank"],
            }, market_info)
            buy_plan["sector_group"] = sector_group
            buy_plan["is_buy_recommend"] = True
            buy_plan["is_watch"] = not market_info["can_buy"]
            stock_pool.append(buy_plan)
            sector_selected_count[sector_group] += 1
            status = "观察" if not market_info["can_buy"] else "★推荐"
            logger.info(f"  {status}: {buy_plan['code']} {buy_plan['name']} [{sector_group}] | "
                        f"评分{buy_plan['factor_score']} | 买点{buy_plan['moderate_buy']} | "
                        f"止损{buy_plan['stop_loss']}(-{buy_plan['stop_loss_pct']}%)")
        else:
            # 仅观察：不生成买点计划，仅展示评分和趋势状态
            stock_info = config.get_stock_info(cand["code"])
            watch_item = {
                "code": cand["code"],
                "name": stock_info.get("名称", cand["code"]),
                "sector": cand["sector"],
                "sector_group": sector_group,
                "factor_score": cand["score"],
                "factor_detail": cand["factors"],
                "signals": cand["signals"],
                "factor_reason": cand["reason"],
                "rps_rank": cand["rps_rank"],
                "current_price": round(cand["df"]["close"].iloc[-1], 2),
                "is_buy_recommend": False,
                "is_watch": True,
                "watch_reason": cand.get("cooling_note", f"评分{cand['score']}<买入线{min_buy_score}"),
                # 买点计划字段置空（邮件模板兼容）
                "aggressive_buy": 0, "moderate_buy": 0, "conservative_buy": 0,
                "stop_loss": 0, "stop_loss_pct": 0, "risk_level": "-",
                "first_shares": 0, "first_amount": 0, "add_price": 0,
                "second_shares": 0, "second_amount": 0, "max_loss": 0,
                "pass_risk": False, "atr": 0,
            }
            stock_pool.append(watch_item)
            logger.info(f"  观察: {cand['code']} {watch_item['name']} [{sector_group}] | "
                        f"评分{cand['score']} | 未达买入线{min_buy_score} | {cand['reason']}")

    # 统计
    buy_count = sum(1 for s in stock_pool if s.get("is_buy_recommend"))
    watch_count = len(stock_pool) - buy_count
    logger.info(f"[选股引擎V3] 完成: {len(stock_pool)}只输出 / {len(candidates)}只候选 | "
                f"推荐买入{buy_count}只 + 观察{watch_count}只")
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
        "buy_recommend_count": buy_count,
        "watch_only_count": watch_count,
        "min_buy_score": min_buy_score,
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
# V3.2: 短线动量筛选通道（与CANSLIM并行，不修改现有逻辑）
# ============================================================

def run_momentum_screener(market_df=None, zt_pool: list = None,
                          holdings: dict = None) -> dict:
    """
    短线动量筛选通道 V3.2

    与CANSLIM中线体系并行，专注于:
      - 连板股/强势启动股的快速识别
      - 跳过基本面(CAI)和“振幅>10%天数”等中线约束
      - 仅用量价+连板+赛道热度评分

    A股约束:
      - 一字板(开盘即涨停)标记为"不可买入"
      - 创业板/科创板20%涨跌停差异
      - T+1: 标注"今日买入明日方可卖出"
      - 100股整数倍仓位建议

    参数:
        market_df: 全市场实时行情DataFrame (akshare stock_zh_a_spot_em)
        zt_pool: 当日涨停池列表 (zt_monitor.get_zt_pool())
        holdings: 当前持仓

    返回:
        {"success": bool, "picks": [...], "summary": str}
    """
    result = {"success": False, "picks": [], "summary": ""}

    if market_df is None or market_df.empty:
        # 尝试获取全市场行情
        try:
            import akshare as ak
            market_df = ak.stock_zh_a_spot_em()
            if market_df is not None and not market_df.empty:
                col_map = {
                    "代码": "code", "名称": "name", "最新价": "price",
                    "涨跌幅": "change_pct", "成交额": "amount",
                    "换手率": "turnover", "量比": "vol_ratio",
                    "今开": "open", "昨收": "prev_close",
                }
                market_df = market_df.rename(columns=col_map)
                for col in ["price", "change_pct", "amount", "turnover", "vol_ratio", "open", "prev_close"]:
                    if col in market_df.columns:
                        market_df[col] = pd.to_numeric(market_df[col], errors="coerce")
        except Exception as e:
            logger.warning(f"[动量筛选] 行情获取失败: {e}")
            result["summary"] = "行情获取失败"
            return result

    if market_df is None or market_df.empty:
        result["summary"] = "无行情数据"
        return result

    # 构建涨停池代码集（用于连板加分）
    zt_codes = set()
    zt_consecutive = {}  # {code: 连板数}
    if zt_pool:
        for s in zt_pool:
            code = s.get("code", "")
            zt_codes.add(code)
            zt_consecutive[code] = s.get("consecutive_days", 1)

    # 筛选条件: 涨幅>5% + 量比>1.5 + 成交额>2亿 + 非ST
    df = market_df.copy()
    if "name" in df.columns:
        df = df[~df["name"].str.contains("ST|退", na=False)]
        df = df[~df["name"].str.startswith(("N", "C"), na=False)]
    if "change_pct" in df.columns:
        df = df[df["change_pct"] >= 5.0]
    if "vol_ratio" in df.columns:
        df = df[df["vol_ratio"] >= 1.5]
    if "amount" in df.columns:
        df = df[df["amount"] >= 2e8]
    if "price" in df.columns:
        df = df[(df["price"] >= 3) & (df["price"] <= 500)]

    if df.empty:
        result["success"] = True
        result["summary"] = "无符合条件的强势股"
        return result

    # 动量评分(0-100)
    picks = []
    for _, row in df.head(30).iterrows():
        code = str(row.get("code", "")).zfill(6)
        if not code or len(code) != 6:
            continue
        # 排除创业板/科创板（用户无权限）
        if code.startswith("300") or code.startswith("688"):
            continue

        name = str(row.get("name", ""))
        price = float(row.get("price", 0))
        change_pct = float(row.get("change_pct", 0))
        vol_ratio = float(row.get("vol_ratio", 0))
        turnover = float(row.get("turnover", 0))
        amount = float(row.get("amount", 0))
        open_price = float(row.get("open", 0))
        prev_close = float(row.get("prev_close", 0))

        # 一字板检测: 开盘价=涨停价
        is_yizi = False
        if prev_close > 0 and open_price > 0:
            limit_pct = 0.20 if code.startswith(("300", "688")) else 0.10
            limit_price = prev_close * (1 + limit_pct)
            if open_price >= limit_price * 0.998:
                is_yizi = True

        # 动量评分
        score = 0
        # 涨幅因子(0-30)
        score += min(int(change_pct / 10 * 30), 30)
        # 量比因子(0-25)
        if vol_ratio >= 4:
            score += 25
        elif vol_ratio >= 3:
            score += 20
        elif vol_ratio >= 2:
            score += 15
        else:
            score += 8
        # 换手率因子(0-20): 3-15%健康区间
        if 3 <= turnover <= 15:
            score += 20
        elif 1 <= turnover < 3:
            score += 10
        elif turnover > 15:
            score += 5
        # 连板加分(0-15)
        consec = zt_consecutive.get(code, 0)
        if consec >= 3:
            score += 15
        elif consec >= 2:
            score += 10
        elif code in zt_codes:
            score += 5
        # 成交额加分(0-10)
        if amount >= 10e8:
            score += 10
        elif amount >= 5e8:
            score += 5

        # 100股整数倍仓位建议
        first_shares = 100
        first_amount = first_shares * price

        # 赛道分类
        try:
            from strategy.market_scanner import classify_stock_sector
            sector = classify_stock_sector(name)
        except Exception:
            sector = "其他"

        picks.append({
            "code": code,
            "name": name,
            "price": round(price, 2),
            "change_pct": round(change_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "turnover": round(turnover, 2),
            "amount_yi": round(amount / 1e8, 2),
            "sector": sector,
            "momentum_score": min(score, 100),
            "consecutive_days": consec,
            "is_yizi": is_yizi,
            "buyable": not is_yizi,  # 一字板不可买
            "first_shares": first_shares,
            "first_amount": round(first_amount, 0),
            "t1_note": "T+1: 今日买入明日方可卖出",
            "alert_level": "high" if score >= 70 and not is_yizi else "medium",
        })

    # 按动量评分降序
    picks.sort(key=lambda x: x["momentum_score"], reverse=True)
    result["picks"] = picks[:10]
    result["success"] = True

    buyable_count = sum(1 for p in result["picks"] if p["buyable"])
    result["summary"] = (f"短线动量筛选: {len(result['picks'])}只强势股"
                         f"(可买{buyable_count}只/一字板{len(result['picks'])-buyable_count}只)")
    logger.info(f"[动量筛选V3.2] {result['summary']}")

    return result


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
        stop_loss = compute_trailing_stop(buy_price, current_price) if buy_price > 0 else 0
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

    # V2.4: 分两板块展示——“推荐买入”和“观察跟踪”
    buy_stocks = [s for s in pool if s.get("is_buy_recommend")]
    watch_stocks = [s for s in pool if not s.get("is_buy_recommend")]
    min_buy_score = result.get("min_buy_score", 50)

    # 因子明细条生成器（复用）
    def _factor_bar(factors):
        bar = ""
        items = [("N", factors.get("N_新事物", 0), 20, "#722ED1"),
                 ("S", factors.get("S_供需", 0), 20, "#1890FF"),
                 ("L", factors.get("L_龙头", 0), 20, "#52C41A"),
                 ("CAI", factors.get("CAI_基本面", 0), 20, "#FA8C16"),
                 ("P", factors.get("P_前瞻", 0), 10, "#13C2C2"),  # V2.8: 满分从20压缩到10
                 ("W", factors.get("W_周线", 0), 5, "#EB2F96")]
        for fn, fv, fm, fc in items:
            pct = max(0, min(fv / fm * 100, 100)) if fm > 0 else 0
            bar += (f'<span style="display:inline-block;margin:1px 3px;font-size:10px">'
                    f'<b style="color:{fc}">{fn}</b>={fv:.0f} '
                    f'<span style="display:inline-block;width:30px;height:5px;background:#eee;border-radius:2px;vertical-align:middle">'
                    f'<span style="display:block;width:{pct:.0f}%;height:100%;background:{fc};border-radius:2px"></span>'
                    f'</span></span>')
        # FIX P2: 资金异动bonus纳入因子展示（解决手动验算总分不一致问题）
        ff_bonus = factors.get("资金异动", 0)
        if ff_bonus > 0:
            bar += f'<span style="display:inline-block;margin:1px 3px;font-size:10px"><b style="color:#F5222D">资金</b>=+{ff_bonus:.0f}</span>'
        return bar

    # === 板块A: 推荐买入 ===
    html += f"""
        <div class="section">
            <div class="section-title">二、推荐买入（评分≥{min_buy_score}分，附买点计划）</div>
"""
    if buy_stocks:
        html += """
            <table>
                <tr>
                    <th>排名</th><th>代码</th><th>名称</th><th>赛道</th>
                    <th>CANSLIM<br>评分</th><th>买入信号</th>
                    <th>现价</th><th>买点(稳健)</th>
                    <th>止损价</th><th>风险</th><th>仓位</th>
                </tr>
"""
        for i, stock in enumerate(buy_stocks, 1):
            score = stock["factor_score"]
            score_class = "score-high" if score >= 70 else ("score-mid" if score >= 55 else "score-low")
            signal_html = "".join(f'<span class="signal-tag{" signal-star" if "★" in sig else ""}">{sig}</span>' for sig in stock.get("signals", []))
            html += f"""
                <tr>
                    <td>{i}</td>
                    <td>{stock['code']}</td>
                    <td style="font-weight:bold">{stock['name']}</td>
                    <td><span class="sector-tag">{stock['sector']}</span></td>
                    <td class="{score_class}">{score}</td>
                    <td style="text-align:left">{signal_html or '-'}</td>
                    <td class="price">{stock['current_price']:.2f}</td>
                    <td class="buy-price">{stock['moderate_buy']:.2f}</td>
                    <td class="stop-price">{stock['stop_loss']:.2f}(-{stock['stop_loss_pct']}%)</td>
                    <td>{stock.get('risk_level', '-')}</td>
                    <td><span class="batch-box">{stock['first_shares']}股 {stock['first_amount']:,.0f}元</span></td>
                </tr>
                <tr>
                    <td colspan="11" style="padding:3px 8px;background:#fafafa;text-align:left;border-bottom:1px solid #e8e8e8">
                        <span style="font-size:11px;color:#666">因子: </span>{_factor_bar(stock['factor_detail'])}
                        <span style="font-size:10px;color:#999;margin-left:6px">RPS前{stock.get('rps_rank',0):.0f}% | {stock.get('factor_reason','')}</span>
                    </td>
                </tr>
                <tr>
                    <td colspan="11" style="padding:4px 8px;background:#eef6ff;text-align:left;border-bottom:2px solid #e8e8e8;font-size:11px">
                        <b style="color:#1565c0">[ADD] 加仓条件:</b>
                        <span style="color:#1565c0;font-weight:bold"> 触发价 {stock.get('add_price', 0):.2f}元</span>(浮盈≥3%且放量)
                        | 加仓 <b>{stock.get('second_shares', 0)}</b>股 / {stock.get('second_amount', 0):,.0f}元
                        | 加仓后总仓位占比 <b>{(stock.get('total_amount', 0) / max(config.TOTAL_CAPITAL, 1) * 100):.1f}%</b>
                        | 前置: DK D点确认 + 量比>1.5 + 板块共振
                    </td>
                </tr>
"""
        html += "</table>"
    else:
        html += f'<p style="text-align:center;color:#999;padding:20px">本期无符合买入条件的标的（评分均<{min_buy_score}分），以下仅供跟踪观察</p>'
    html += "</div>"

    # === 板块B: 观察跟踪 ===
    html += f"""
        <div class="section">
            <div class="section-title">三、观察跟踪（评分<{min_buy_score}分，不建议买入）</div>
            <table>
                <tr><th>排名</th><th>代码</th><th>名称</th><th>赛道</th><th>评分</th><th>现价</th><th>因子明细</th><th>观察原因</th></tr>
"""
    for i, stock in enumerate(watch_stocks, len(buy_stocks) + 1):
        score = stock["factor_score"]
        score_class = "score-mid" if score >= 40 else "score-low"
        html += f"""
                <tr>
                    <td>{i}</td>
                    <td>{stock['code']}</td>
                    <td>{stock['name']}</td>
                    <td><span class="sector-tag">{stock['sector']}</span></td>
                    <td class="{score_class}">{score}</td>
                    <td>{stock.get('current_price', 0):.2f}</td>
                    <td style="text-align:left">{_factor_bar(stock.get('factor_detail', {}))}</td>
                    <td style="font-size:11px;color:#999">{stock.get('watch_reason', stock.get('factor_reason', ''))}</td>
                </tr>
"""
    html += """
            </table>
            <p class="note">观察股票暂不建议买入，等待评分突破买入线后再介入</p>
        </div>
"""

    # V2.4: 涨停复盘板块
    zt_report = result.get("zt_report")
    if zt_report and zt_report.get("ladder", {}).get("total_zt", 0) > 0:
        ladder = zt_report["ladder"]
        sector_heat = zt_report.get("sector_heat", [])[:3]
        html += f"""
        <div class="section">
            <div class="section-title">四、涨停复盘（{zt_report.get('date', '')}）</div>
            <table>
                <tr><th>指标</th><th>数值</th></tr>
                <tr><td>涨停总数</td><td><b>{ladder['total_zt']}</b>只</td></tr>
                <tr><td>炸板数</td><td>{ladder['total_zb']}只</td></tr>
                <tr><td>封板成功率</td><td>{ladder['zt_rate']:.0%}</td></tr>
                <tr><td>最高连板</td><td><b>{ladder['max_consecutive']}</b>板</td></tr>
                <tr><td>连板分布</td><td>1板:{ladder['ladder'].get(1,0)} | 2板:{ladder['ladder'].get(2,0)} | 3板:{ladder['ladder'].get(3,0)} | 4+板:{ladder['ladder'].get('4+',0)}</td></tr>
            </table>
"""
        # 板块热度Top3
        if sector_heat:
            html += '<p style="margin:8px 0 4px;font-size:12px;color:#666"><b>板块热度Top3:</b> '
            html += " | ".join(f"{h['sector_name']}({h['zt_count']}只涨停)" for h in sector_heat)
            html += '</p>'
        # 连板龙头
        if ladder.get("top_stocks"):
            html += '<p style="margin:4px 0;font-size:12px;color:#666"><b>连板龙头:</b> '
            html += "、".join(f"{s['name']}({s['consecutive_days']}板/{s.get('sector','')})" for s in ladder["top_stocks"][:5])
            html += '</p>'
        # 次日关注提示
        lianban_stocks = [s for s in ladder.get("top_stocks", []) if s.get("consecutive_days", 0) >= 2]
        if lianban_stocks:
            html += '<p style="margin:4px 0;font-size:11px;color:#FA8C16">★ 次日关注: '
            html += "、".join(f"{s['name']}({s['consecutive_days']}板)" for s in lianban_stocks[:3])
            html += ' — 关注延续性</p>'
        html += "</div>"

    # 操作指南
    html += f"""
        <div class="guide">
            <h3>CANSLIM选股体系 + 分批建仓操作指南</h3>
            <ol style="margin:5px 0;padding-left:20px;line-height:1.8">
                <li><b>★缩量回踩20日均线</b>：成交量较20日均量萎缩30%以上，价格回踩MA20不跌破 → <span style="color:#FF4D4F">核心买点</span></li>
                <li><b>分批建仓</b>：第一批40%试仓（买点附近），浮盈≥3%再加第二批60%（确认趋势）</li>
                <li><b>同步设置止损</b>：买入即挂止损（初始止损=买入价×{1-getattr(config, 'INITIAL_STOP_LOSS_PCT', 0.10):.0%}），浮盈后上移移动止损保护利润</li>
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
            <p class="note">注：V2.8回测优化: P前瞻因子满分从20压缩到10(回测显示动量追高负相关)，新增均值回归惩罚(20日涨庅>25%扣5分)。CAI因子无有效数据给8/20中性分。</p>
        </div>
"""

    # 市场环境简评（由report_dispatcher传入）
    market_commentary = result.get("_market_commentary", "")
    if market_commentary:
        commentary_color = {"up": "#F6FFED", "neutral": "#FFF7E6", "down": "#FFF1F0"}.get(market["market_state"], "#f5f5f5")
        commentary_border = {"up": "#B7EB8F", "neutral": "#FFD591", "down": "#FFA39E"}.get(market["market_state"], "#e8e8e8")
        html += f"""
        <div class="guide" style="background:{commentary_color};border-color:{commentary_border}">
            <h3>📊 当前市场环境简评</h3>
            <p style="font-size:13px;line-height:1.8;margin:5px 0">{market_commentary}</p>
        </div>
"""

    html += f"""
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
