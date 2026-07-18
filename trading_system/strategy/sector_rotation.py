"""
板块轮动分析模块
==================
分析各赛道的强弱变化，识别资金流入/流出赛道
在选股和交易时优先选择处于上升周期的赛道

核心功能:
- 计算各赛道近N日涨跌幅排名
- 识别赛道趋势（上升/下降/震荡）
- 赛道强度评分
- 给出赛道配置建议

使用方式:
    from strategy.sector_rotation import analyze_sector_rotation
    result = analyze_sector_rotation(data_dict, holdings)
"""

import pandas as pd
import numpy as np
import logging
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def analyze_sector_rotation(data_dict: dict,
                            holdings: dict = None,
                            lookback_days: int = 20) -> dict:
    """
    板块轮动分析
    
    参数:
        data_dict: {code: DataFrame} 所有股票的日线数据
        holdings: {code: holding_info} 当前持仓
        lookback_days: 回看天数（默认20日）
    
    返回:
        {
            "sector_ranking": [(sector, score, change_pct), ...],  # 赛道排名
            "strong_sectors": [str],    # 强势赛道
            "weak_sectors": [str],      # 弱势赛道
            "rotation_signal": str,     # "accumulating"/"distributing"/"neutral"
            "recommendation": str,      # 配置建议
            "sector_details": {sector: dict}  # 各赛道详细数据
        }
    """
    if not data_dict:
        return {
            "sector_ranking": [],
            "strong_sectors": [],
            "weak_sectors": [],
            "rotation_signal": "neutral",
            "recommendation": "数据不足，无法分析",
            "sector_details": {}
        }

    # 收集各赛道的股票数据
    sector_stocks = defaultdict(list)
    for code, df in data_dict.items():
        stock_info = config.STOCK_POOL.get(code, {})
        sector = stock_info.get("赛道", "其他")
        if len(df) >= lookback_days:
            sector_stocks[sector].append(df)

    sector_details = {}
    sector_scores = {}

    for sector, dfs in sector_stocks.items():
        # 计算赛道整体涨跌幅（等权平均）
        changes = []
        volumes = []
        ma20_up_count = 0
        total_count = len(dfs)

        for df in dfs:
            if len(df) < 2:
                continue
            # 区间涨跌幅
            change_pct = (df["close"].iloc[-1] - df["close"].iloc[-lookback_days]) / df["close"].iloc[-lookback_days]
            changes.append(change_pct)

            # 成交量变化
            recent_vol = df["volume"].iloc[-5:].mean()
            prev_vol = df["volume"].iloc[-lookback_days:-5].mean() if len(df) > lookback_days else recent_vol
            if prev_vol > 0:
                volumes.append(recent_vol / prev_vol)
            else:
                volumes.append(1.0)

            # MA20向上个股数
            if "ma20_slope" in df.columns:
                latest = df.iloc[-1]
                if not pd.isna(latest.get("ma20_slope", np.nan)) and latest["ma20_slope"] > 0:
                    ma20_up_count += 1

        if not changes:
            continue

        avg_change = np.mean(changes)
        avg_vol_ratio = np.mean(volumes)
        ma20_up_ratio = ma20_up_count / total_count if total_count > 0 else 0

        # 赛道综合评分
        # 涨跌幅贡献40% + 量能贡献30% + 均线贡献30%
        change_score = np.clip(avg_change * 5, -1, 1)  # 涨跌幅映射到[-1,1]
        vol_score = np.clip((avg_vol_ratio - 1) * 2, -1, 1)  # 量能变化映射
        ma_score = ma20_up_ratio * 2 - 1  # 均线向上比例映射到[-1,1]

        total_score = change_score * 0.4 + vol_score * 0.3 + ma_score * 0.3

        # 赛道趋势判定
        if total_score > 0.2:
            trend = "up"
        elif total_score < -0.2:
            trend = "down"
        else:
            trend = "neutral"

        sector_details[sector] = {
            "avg_change_pct": avg_change,
            "avg_vol_ratio": avg_vol_ratio,
            "ma20_up_ratio": ma20_up_ratio,
            "total_score": total_score,
            "trend": trend,
            "stock_count": total_count
        }
        sector_scores[sector] = total_score

    # 赛道排名
    sector_ranking = sorted(
        [(s, d["total_score"], d["avg_change_pct"]) for s, d in sector_details.items()],
        key=lambda x: x[1],
        reverse=True
    )

    # 强势/弱势赛道
    strong_sectors = [s for s, d in sector_details.items() if d["trend"] == "up"]
    weak_sectors = [s for s, d in sector_details.items() if d["trend"] == "down"]

    # 轮动信号判定
    if len(strong_sectors) >= len(sector_details) * 0.6:
        rotation_signal = "accumulating"
        recommendation = "多数赛道走强，市场处于上升周期，可适当提高仓位"
    elif len(weak_sectors) >= len(sector_details) * 0.6:
        rotation_signal = "distributing"
        recommendation = "多数赛道走弱，市场处于下降周期，建议降低仓位，防守为主"
    elif strong_sectors and weak_sectors:
        rotation_signal = "rotating"
        recommendation = f"赛道分化，资金从[{', '.join(weak_sectors)}]流向[{', '.join(strong_sectors)}]，建议调仓换股"
    else:
        rotation_signal = "neutral"
        recommendation = "赛道整体震荡，维持现有仓位，等待方向明确"

    return {
        "sector_ranking": sector_ranking,
        "strong_sectors": strong_sectors,
        "weak_sectors": weak_sectors,
        "rotation_signal": rotation_signal,
        "recommendation": recommendation,
        "sector_details": sector_details
    }


def get_sector_recommendation(code: str,
                              rotation_result: dict) -> dict:
    """
    根据板块轮动结果，给出单只股票的赛道建议
    
    参数:
        code: 股票代码
        rotation_result: analyze_sector_rotation() 的返回结果
    
    返回:
        {
            "sector": str,
            "sector_trend": str,
            "position_adjust": float,  # 赛道维度的仓位调整系数
            "recommendation": str
        }
    """
    stock_info = config.STOCK_POOL.get(code, {})
    sector = stock_info.get("赛道", "其他")
    details = rotation_result.get("sector_details", {}).get(sector, {})

    trend = details.get("trend", "neutral")
    score = details.get("total_score", 0)

    if trend == "up":
        position_adjust = 1.1  # 强势赛道，仓位上浮10%
        rec = f"赛道[{sector}]处于上升周期，可正常建仓"
    elif trend == "down":
        position_adjust = 0.6  # 弱势赛道，仓位下调40%
        rec = f"赛道[{sector}]处于下降周期，建议观望或轻仓"
    else:
        position_adjust = 0.9  # 震荡赛道，仓位微调
        rec = f"赛道[{sector}]震荡整理，谨慎建仓"

    return {
        "sector": sector,
        "sector_trend": trend,
        "position_adjust": position_adjust,
        "recommendation": rec
    }


def format_sector_report(rotation_result: dict) -> str:
    """格式化板块轮动报告"""
    lines = [
        "=" * 50,
        "  板块轮动分析",
        "=" * 50,
        f"  轮动信号: {rotation_result['rotation_signal']}",
        f"  配置建议: {rotation_result['recommendation']}",
        "",
        "  赛道排名（由强到弱）:"
    ]

    for i, (sector, score, change) in enumerate(rotation_result["sector_ranking"], 1):
        trend_icon = {"up": "▲", "down": "▼", "neutral": "—"}
        details = rotation_result["sector_details"].get(sector, {})
        trend = details.get("trend", "neutral")
        icon = trend_icon.get(trend, "—")
        lines.append(f"    {i}. {sector}: 评分{score:+.2f} {icon} | 涨跌幅{change:.2%}")

    if rotation_result["strong_sectors"]:
        lines.append(f"\n  强势赛道: {', '.join(rotation_result['strong_sectors'])}")
    if rotation_result["weak_sectors"]:
        lines.append(f"  弱势赛道: {', '.join(rotation_result['weak_sectors'])}")

    return "\n".join(lines)


if __name__ == "__main__":
    print("=" * 50)
    print("  板块轮动分析 - 测试")
    print("=" * 50)

    # 生成模拟数据
    np.random.seed(42)
    data_dict = {}
    for code, info in config.STOCK_POOL.items():
        dates = pd.date_range("2025-01-01", periods=80, freq="B")
        base = 50 + np.random.random() * 100
        prices = [base]
        for i in range(1, 80):
            change = np.random.normal(0.1, 0.5)
            prices.append(max(prices[-1] + change, 10))

        df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": [p * 0.998 for p in prices],
            "close": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "volume": np.random.randint(500000, 2000000, 80).astype(float),
        })
        from strategy.trend_strategy import compute_indicators
        df = compute_indicators(df)
        data_dict[code] = df

    result = analyze_sector_rotation(data_dict)
    print(format_sector_report(result))

    # 单股建议
    for code in list(config.STOCK_POOL.keys())[:3]:
        rec = get_sector_recommendation(code, result)
        print(f"\n  {code} {config.STOCK_POOL[code]['名称']}: {rec['recommendation']}")

    print("\n[OK] 板块轮动模块测试通过")
