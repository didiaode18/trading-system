"""
龙虎榜深度分析模块 (LHB Analyzer)
===================================
用途：追踪个股龙虎榜上榜记录，分析机构净买入趋势，评估游资席位活跃度，
     并输出可直接集成至多因子模型的综合评分因子。

数据源：akshare 东方财富龙虎榜接口
- stock_lhb_detail_em       ：个股龙虎榜汇总（买卖前5席位）
- stock_lhb_jgmmtj_em       ：机构买卖每日统计
- stock_lhb_stock_detail_em ：个股当日席位明细（买/卖）
- stock_lhb_stock_detail_date_em：个股有龙虎榜数据的日期列表

作者：quant-team
"""

import logging
import datetime
from functools import lru_cache
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

from trading_system import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块内默认参数（不修改 config.py）
# ---------------------------------------------------------------------------
CFG = getattr(config, "CFG", {}) if isinstance(getattr(config, "CFG", None), dict) else {}

DEFAULT_DAYS = CFG.get("lhb_default_days", 30)
# 机构净买入连续天数阈值（超过该值视为看多确认）
INST_BUY_DAYS_THRESHOLD = CFG.get("lhb_inst_buy_days_threshold", 3)
# 游资主导判断：机构席位占比低于此阈值视为游资主导
HOT_MONEY_DOMINATED_RATIO = CFG.get("lhb_hot_money_dominated_ratio", 0.3)
# 短线风险上榜次数阈值
RISK_LHB_COUNT_THRESHOLD = CFG.get("lhb_risk_count_threshold", 5)


# ---------------------------------------------------------------------------
# 内存缓存（当日有效，避免重复请求）
# ---------------------------------------------------------------------------
_cache: Dict[str, object] = {}
_cache_date: Optional[str] = None


def _today_str() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _date_str(days_ago: int = 0) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days_ago)).strftime("%Y%m%d")


def _clear_cache_if_stale():
    """若日期已非今日，清空缓存"""
    global _cache, _cache_date
    today = _today_str()
    if _cache_date != today:
        _cache.clear()
        _cache_date = today


# ---------------------------------------------------------------------------
# 已知机构席位名称关键词
# ---------------------------------------------------------------------------
INSTITUTION_KEYWORDS = ["机构专用", "沪股通专用", "深股通专用", "北向资金"]


def _is_institution_seat(seat_name: str) -> bool:
    """判断席位是否为机构/北向资金"""
    return any(kw in seat_name for kw in INSTITUTION_KEYWORDS)


# ---------------------------------------------------------------------------
# 数据获取层（带缓存 + 异常降级）
# ---------------------------------------------------------------------------

def fetch_lhb_detail(start_date: str, end_date: str) -> pd.DataFrame:
    """获取日期区间内全部龙虎榜汇总记录"""
    _clear_cache_if_stale()
    key = f"detail_{start_date}_{end_date}"
    if key in _cache:
        return _cache[key]

    try:
        import akshare as ak
        df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
        _cache[key] = df
        return df
    except Exception as e:
        logger.error(f"龙虎榜明细获取失败 ({start_date}~{end_date}): {e}")
        return pd.DataFrame()


def fetch_lhb_jgmm(start_date: str, end_date: str) -> pd.DataFrame:
    """获取机构买卖每日统计"""
    _clear_cache_if_stale()
    key = f"jgmm_{start_date}_{end_date}"
    if key in _cache:
        return _cache[key]

    try:
        import akshare as ak
        df = ak.stock_lhb_jgmmtj_em(start_date=start_date, end_date=end_date)
        _cache[key] = df
        return df
    except Exception as e:
        logger.error(f"龙虎榜机构统计获取失败 ({start_date}~{end_date}): {e}")
        return pd.DataFrame()


def fetch_stock_lhb_dates(symbol: str) -> pd.DataFrame:
    """获取个股有龙虎榜数据的日期列表"""
    _clear_cache_if_stale()
    key = f"dates_{symbol}"
    if key in _cache:
        return _cache[key]

    try:
        import akshare as ak
        df = ak.stock_lhb_stock_detail_date_em(symbol=symbol)
        _cache[key] = df
        return df
    except Exception as e:
        logger.error(f"龙虎榜日期列表获取失败 ({symbol}): {e}")
        return pd.DataFrame()


def fetch_stock_seat_detail(symbol: str, date: str, flag: str = "买入") -> pd.DataFrame:
    """获取个股某日席位明细（买入/卖出）"""
    _clear_cache_if_stale()
    key = f"seat_{symbol}_{date}_{flag}"
    if key in _cache:
        return _cache[key]

    try:
        import akshare as ak
        df = ak.stock_lhb_stock_detail_em(symbol=symbol, date=date, flag=flag)
        _cache[key] = df
        return df
    except Exception as e:
        logger.error(f"龙虎榜席位明细获取失败 ({symbol}, {date}, {flag}): {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# 核心分析类
# ---------------------------------------------------------------------------

class LHBAnalyzer:
    """龙虎榜深度分析器"""

    # ------------------------------------------------------------------
    def get_lhb_records(self, stock_code: str, days: int = DEFAULT_DAYS) -> dict:
        """
        获取近 N 天龙虎榜上榜记录。

        返回:
            {records: list[dict], total_count: int, dates: list[str]}
        """
        end_date = _today_str()
        start_date = _date_str(days)

        df = fetch_lhb_detail(start_date, end_date)
        if df.empty:
            logger.info(f"龙虎榜分析: {stock_code} 近{days}天无上榜记录（数据为空）")
            return {"records": [], "total_count": 0, "dates": []}

        # 过滤目标股票（代码列可能为字符串，统一处理）
        df["代码"] = df["代码"].astype(str).str.zfill(6)
        mask = df["代码"] == stock_code.zfill(6)
        stock_df = df[mask].copy()

        records = stock_df.to_dict(orient="records")
        dates = []
        if "上榜日" in stock_df.columns:
            dates = stock_df["上榜日"].astype(str).tolist()

        logger.info(
            f"龙虎榜分析: {stock_code} 近{days}天上榜{len(records)}次"
        )
        return {"records": records, "total_count": len(records), "dates": dates}

    # ------------------------------------------------------------------
    def calc_institution_flow(self, stock_code: str, days: int = DEFAULT_DAYS) -> dict:
        """
        计算机构净买入趋势。

        返回:
            {net_buy_amount: float, buy_days: int, sell_days: int,
             trend: "increasing"/"decreasing"/"neutral", daily_data: list[dict]}
        """
        end_date = _today_str()
        start_date = _date_str(days)

        df = fetch_lhb_jgmm(start_date, end_date)
        if df.empty:
            logger.info(f"龙虎榜分析: {stock_code} 机构统计数据为空")
            return {
                "net_buy_amount": 0.0,
                "buy_days": 0,
                "sell_days": 0,
                "trend": "neutral",
                "daily_data": [],
            }

        df["代码"] = df["代码"].astype(str).str.zfill(6)
        mask = df["代码"] == stock_code.zfill(6)
        stock_df = df[mask].copy()

        if stock_df.empty:
            logger.info(f"龙虎榜分析: {stock_code} 近{days}天无机构交易记录")
            return {
                "net_buy_amount": 0.0,
                "buy_days": 0,
                "sell_days": 0,
                "trend": "neutral",
                "daily_data": [],
            }

        # 机构买入净额列
        net_col = "机构买入净额" if "机构买入净额" in stock_df.columns else None
        if net_col is None:
            # fallback：用买入总额-卖出总额
            if "机构买入总额" in stock_df.columns and "机构卖出总额" in stock_df.columns:
                stock_df["_net"] = (
                    stock_df["机构买入总额"].fillna(0) - stock_df["机构卖出总额"].fillna(0)
                )
                net_col = "_net"
            else:
                net_col = None

        daily_data = []
        net_series = []
        if net_col:
            # 按上榜日期排序
            if "上榜日期" in stock_df.columns:
                stock_df = stock_df.sort_values("上榜日期").reset_index(drop=True)

            for _, row in stock_df.iterrows():
                net_val = float(row.get(net_col, 0) or 0)
                date_val = str(row.get("上榜日期", ""))
                net_series.append(net_val)
                daily_data.append({
                    "date": date_val,
                    "net_buy": net_val,
                    "buy_amount": float(row.get("机构买入总额", 0) or 0),
                    "sell_amount": float(row.get("机构卖出总额", 0) or 0),
                    "buy_inst_count": int(row.get("买方机构数", 0) or 0),
                    "sell_inst_count": int(row.get("卖方机构数", 0) or 0),
                })

        net_buy_amount = sum(net_series) if net_series else 0.0
        buy_days = sum(1 for v in net_series if v > 0)
        sell_days = sum(1 for v in net_series if v < 0)

        # 趋势判断：用线性回归斜率
        trend = "neutral"
        if len(net_series) >= 2:
            x = np.arange(len(net_series))
            slope = np.polyfit(x, net_series, 1)[0]
            if slope > 0:
                trend = "increasing"
            elif slope < 0:
                trend = "decreasing"

        amount_wan = net_buy_amount / 10000
        logger.info(
            f"龙虎榜分析: {stock_code} 近{days}天机构净买入{amount_wan:.2f}万, "
            f"趋势={trend}, 买入天数={buy_days}, 卖出天数={sell_days}"
        )
        return {
            "net_buy_amount": net_buy_amount,
            "buy_days": buy_days,
            "sell_days": sell_days,
            "trend": trend,
            "daily_data": daily_data,
        }

    # ------------------------------------------------------------------
    def calc_hot_money_activity(self, stock_code: str, days: int = DEFAULT_DAYS) -> dict:
        """
        评估游资席位活跃度。

        返回:
            {activity_score: 0-100, top_seats: list[dict],
             is_hot_money_dominated: bool}
        """
        code = stock_code.zfill(6)

        # 获取近 N 天上榜日期
        dates_df = fetch_stock_lhb_dates(code)
        if dates_df.empty:
            return {"activity_score": 0, "top_seats": [], "is_hot_money_dominated": False}

        # 过滤近 N 天
        cutoff = datetime.date.today() - datetime.timedelta(days=days)
        if "交易日" not in dates_df.columns:
            return {"activity_score": 0, "top_seats": [], "is_hot_money_dominated": False}

        dates_df["交易日"] = pd.to_datetime(dates_df["交易日"])
        recent = dates_df[dates_df["交易日"] >= pd.Timestamp(cutoff)]
        lhb_dates = recent["交易日"].dt.strftime("%Y%m%d").tolist()

        if not lhb_dates:
            return {"activity_score": 0, "top_seats": [], "is_hot_money_dominated": False}

        # 汇总席位数据
        seat_stats: Dict[str, dict] = {}
        total_inst_amount = 0.0
        total_all_amount = 0.0

        for date_str in lhb_dates:
            for flag in ("买入", "卖出"):
                seat_df = fetch_stock_seat_detail(code, date_str, flag)
                if seat_df.empty:
                    continue
                for _, row in seat_df.iterrows():
                    seat_name = str(row.get("交易营业部名称", "") or "")
                    amount = float(row.get("买入金额", 0) or 0) + float(row.get("卖出金额", 0) or 0)
                    if not seat_name:
                        continue
                    if seat_name not in seat_stats:
                        seat_stats[seat_name] = {"seat": seat_name, "amount": 0.0, "count": 0, "is_institution": False}
                    seat_stats[seat_name]["amount"] += amount
                    seat_stats[seat_name]["count"] += 1
                    seat_stats[seat_name]["is_institution"] = _is_institution_seat(seat_name)
                    total_all_amount += amount
                    if _is_institution_seat(seat_name):
                        total_inst_amount += amount

        # 排序取前10席位
        top_seats = sorted(seat_stats.values(), key=lambda x: x["amount"], reverse=True)[:10]

        # 机构占比
        inst_ratio = total_inst_amount / total_all_amount if total_all_amount > 0 else 0.0
        is_hot_money_dominated = inst_ratio < HOT_MONEY_DOMINATED_RATIO

        # 活跃度评分（0-100）
        # 维度：上榜次数（40%）+ 席位多样性（30%）+ 游资主导程度（30%）
        count_score = min(len(lhb_dates) / RISK_LHB_COUNT_THRESHOLD, 1.0) * 40
        seat_diversity = min(len(seat_stats) / 20.0, 1.0) * 30
        hot_money_ratio_score = (1 - inst_ratio) * 30
        activity_score = round(count_score + seat_diversity + hot_money_ratio_score, 2)
        activity_score = min(max(activity_score, 0), 100)

        if is_hot_money_dominated:
            logger.warning(
                f"龙虎榜预警: {stock_code} 游资活跃度{activity_score}分, "
                f"机构占比仅{inst_ratio:.1%}, 短线风险较高"
            )

        return {
            "activity_score": activity_score,
            "top_seats": top_seats,
            "is_hot_money_dominated": is_hot_money_dominated,
        }

    # ------------------------------------------------------------------
    def analyze(self, stock_code: str, days: int = DEFAULT_DAYS) -> dict:
        """
        综合分析（整合上榜次数、机构趋势、游资活跃度）。

        返回:
            {lhb_count: int, institution_trend: str,
             hot_money_score: float, risk_warning: bool,
             signal: "bullish"/"bearish"/"neutral",
             description: str}
        """
        records = self.get_lhb_records(stock_code, days)
        inst_flow = self.calc_institution_flow(stock_code, days)
        hot_money = self.calc_hot_money_activity(stock_code, days)

        lhb_count = records["total_count"]
        institution_trend = inst_flow["trend"]
        hot_money_score = hot_money["activity_score"]
        is_hot_money_dominated = hot_money["is_hot_money_dominated"]

        # ---- 信号判定逻辑 ----
        signal = "neutral"
        risk_warning = False
        reasons = []

        # 看多条件：机构净买入趋势上升 且 连续买入天数 >= 阈值
        if institution_trend == "increasing" and inst_flow["buy_days"] >= INST_BUY_DAYS_THRESHOLD:
            signal = "bullish"
            reasons.append(
                f"机构连续净买入{inst_flow['buy_days']}天，趋势上行"
            )

        # 看空/风险预警条件：游资主导且上榜频繁
        if is_hot_money_dominated and lhb_count >= RISK_LHB_COUNT_THRESHOLD:
            risk_warning = True
            if signal != "bullish":
                signal = "bearish"
            reasons.append(
                f"游资主导（活跃度{hot_money_score}分），近{days}天上榜{lhb_count}次，短线风险较高"
            )

        # 机构持续卖出
        if institution_trend == "decreasing" and inst_flow["sell_days"] >= INST_BUY_DAYS_THRESHOLD:
            if signal != "bullish":
                signal = "bearish"
            reasons.append(
                f"机构连续净卖出{inst_flow['sell_days']}天，趋势下行"
            )

        if not reasons:
            reasons.append("无明显异动信号")

        description = "; ".join(reasons)
        net_wan = inst_flow["net_buy_amount"] / 10000

        logger.info(
            f"龙虎榜分析: {stock_code} 近{days}天上榜{lhb_count}次, "
            f"机构净买入{net_wan:.2f}万, 信号={signal}, 风险预警={risk_warning}"
        )

        return {
            "lhb_count": lhb_count,
            "institution_trend": institution_trend,
            "hot_money_score": hot_money_score,
            "risk_warning": risk_warning,
            "signal": signal,
            "description": description,
        }

    # ------------------------------------------------------------------
    def batch_analyze(self, stock_codes: List[str], days: int = DEFAULT_DAYS) -> dict:
        """
        批量分析多只股票。

        返回:
            {code: analysis_result_dict, ...}
        """
        results = {}
        for code in stock_codes:
            try:
                results[code] = self.analyze(code, days)
            except Exception as e:
                logger.error(f"龙虎榜批量分析失败 ({code}): {e}")
                results[code] = {
                    "lhb_count": 0,
                    "institution_trend": "neutral",
                    "hot_money_score": 0.0,
                    "risk_warning": False,
                    "signal": "neutral",
                    "description": f"分析异常: {e}",
                }
        return results


# ---------------------------------------------------------------------------
# 集成接口：供 multi_factor.py 直接调用
# ---------------------------------------------------------------------------

def get_lhb_factor(stock_code: str, days: int = DEFAULT_DAYS) -> float:
    """
    计算龙虎榜综合因子分数（0-100），可直接用于多因子模型。

    评分规则：
    - 基础分：50（中性）
    - 机构净买入趋势上升 → +15
    - 机构连续净买入 >= 3 天 → +15（看多确认加分）
    - 游资主导且上榜频繁 → -20（短线风险扣分）
    - 机构持续卖出 → -15
    - 上榜次数适中（1~3次） → +5（市场关注度加分）
    """
    analyzer = LHBAnalyzer()
    try:
        result = analyzer.analyze(stock_code, days)
    except Exception as e:
        logger.error(f"龙虎榜因子计算失败 ({stock_code}): {e}")
        return 50.0

    score = 50.0

    # 机构趋势加分/扣分
    if result["institution_trend"] == "increasing":
        score += 15
    elif result["institution_trend"] == "decreasing":
        score -= 15

    # 机构连续净买入确认（通过 analyze 中 buy_days 判断）
    inst_flow = analyzer.calc_institution_flow(stock_code, days)
    if inst_flow["buy_days"] >= INST_BUY_DAYS_THRESHOLD and inst_flow["trend"] == "increasing":
        score += 15

    if inst_flow["sell_days"] >= INST_BUY_DAYS_THRESHOLD and inst_flow["trend"] == "decreasing":
        score -= 15

    # 游资主导 + 频繁上榜 → 扣分
    if result["risk_warning"]:
        score -= 20

    # 上榜次数适中加分
    if 1 <= result["lhb_count"] <= 3:
        score += 5

    # 归一化到 [0, 100]
    score = min(max(score, 0), 100)

    logger.info(
        f"龙虎榜因子: {stock_code} 得分={score:.1f}, "
        f"信号={result['signal']}, 上榜{result['lhb_count']}次"
    )
    return round(score, 2)


# ---------------------------------------------------------------------------
# 便捷入口（可直接运行测试）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    analyzer = LHBAnalyzer()
    # 示例：分析平安银行
    test_code = "000001"
    print(f"\n=== 龙虎榜分析: {test_code} ===")
    result = analyzer.analyze(test_code, days=30)
    for k, v in result.items():
        print(f"  {k}: {v}")
    print(f"\n  龙虎榜因子得分: {get_lhb_factor(test_code, days=30)}")
