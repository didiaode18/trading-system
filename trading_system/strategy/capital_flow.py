"""
资金流向分析模块 V1.0
======================
整合北向资金、主力资金、行业资金流数据，辅助选股决策

核心功能:
  1. 北向资金（沪股通/深股通）每日净流入追踪
  2. 个股主力资金净流入排名
  3. 行业板块资金流向（发现资金聚集行业）
  4. 龙虎榜机构席位分析
  5. 资金流综合评分（供选股引擎调用）
  6. 四级资金流分析（超大单/大单/中单/小单）
  7. 资金流模式识别（吸筹/出货/洗盘）
  8. 增强版资金流评分

数据来源: akshare（东方财富）

使用方式:
    from strategy.capital_flow import CapitalFlowAnalyzer
    cfa = CapitalFlowAnalyzer()
    report = cfa.full_analysis()
"""

import os
import sys
import json
import logging
import datetime
from collections import defaultdict

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

# 缓存
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
FLOW_CACHE_FILE = os.path.join(CACHE_DIR, "capital_flow_cache.json")


class CapitalFlowAnalyzer:
    """资金流向分析器"""

    def __init__(self):
        self.today = datetime.date.today().strftime("%Y-%m-%d")
        self.cache = self._load_cache()

    # ============================================================
    # 一、北向资金分析
    # ============================================================

    def get_northbound_flow(self) -> dict:
        """
        获取北向资金（沪股通+深股通）近期流向
        
        返回:
            {
                "today_net_inflow": float,    # 今日净流入(亿)
                "5d_net_inflow": float,       # 5日累计净流入
                "20d_net_inflow": float,      # 20日累计净流入
                "trend": str,                 # "inflow"/"outflow"/"neutral"
                "consecutive_inflow_days": int, # 连续净流入天数
                "signal": str,                # 信号描述
                "success": bool
            }
        """
        cached = self._get_cache("northbound")
        if cached:
            return cached

        result = {
            "today_net_inflow": 0,
            "5d_net_inflow": 0,
            "20d_net_inflow": 0,
            "trend": "neutral",
            "consecutive_inflow_days": 0,
            "signal": "数据获取失败",
            "success": False
        }

        if not HAS_AKSHARE:
            return result

        try:
            # 获取北向资金历史数据（兼容不同版本akshare API）
            df = None
            api_names = [
                lambda: ak.stock_hsgt_north_net_flow_in_em(symbol="北上"),
                lambda: ak.stock_hsgt_north_net_flow_in_em(indicator="北上"),
                lambda: ak.stock_em_hsgt_north_net_flow_in(indicator="北上"),
            ]
            for api_fn in api_names:
                try:
                    df = api_fn()
                    if df is not None and not df.empty:
                        break
                except (AttributeError, TypeError):
                    continue

            if df is None or df.empty:
                return result

            # 标准化列名
            if "当日净流入" in df.columns:
                flow_col = "当日净流入"
            elif "value" in df.columns:
                flow_col = "value"
            else:
                # 尝试找数值列
                numeric_cols = df.select_dtypes(include=[np.number]).columns
                if len(numeric_cols) > 0:
                    flow_col = numeric_cols[-1]
                else:
                    return result

            flows = df[flow_col].dropna().values

            if len(flows) >= 1:
                result["today_net_inflow"] = round(float(flows[-1]), 2)
            if len(flows) >= 5:
                result["5d_net_inflow"] = round(float(flows[-5:].sum()), 2)
            if len(flows) >= 20:
                result["20d_net_inflow"] = round(float(flows[-20:].sum()), 2)

            # 连续净流入天数
            consecutive = 0
            for f in reversed(flows):
                if f > 0:
                    consecutive += 1
                else:
                    break
            result["consecutive_inflow_days"] = consecutive

            # 趋势判断
            if result["5d_net_inflow"] > 50:  # 5日净流入>50亿
                result["trend"] = "strong_inflow"
                result["signal"] = f"北向资金强势流入(5日+{result['5d_net_inflow']:.0f}亿)，利好"
            elif result["5d_net_inflow"] > 0:
                result["trend"] = "inflow"
                result["signal"] = f"北向资金小幅流入(5日+{result['5d_net_inflow']:.0f}亿)"
            elif result["5d_net_inflow"] > -50:
                result["trend"] = "outflow"
                result["signal"] = f"北向资金小幅流出(5日{result['5d_net_inflow']:.0f}亿)"
            else:
                result["trend"] = "strong_outflow"
                result["signal"] = f"北向资金大幅流出(5日{result['5d_net_inflow']:.0f}亿)，警惕"

            result["success"] = True

        except Exception as e:
            logger.warning(f"[资金流] 北向资金获取失败: {e}")

        self._set_cache("northbound", result)
        return result

    # ============================================================
    # 二、行业资金流向
    # ============================================================

    def get_sector_flow(self) -> dict:
        """
        获取行业板块资金流向排名
        
        返回:
            {
                "top_inflow": [{"sector", "net_inflow", "change_pct"}],  # 资金流入前5
                "top_outflow": [...],   # 资金流出前5
                "hot_sectors": [str],   # 热门行业
                "success": bool
            }
        """
        cached = self._get_cache("sector_flow")
        if cached:
            return cached

        result = {
            "top_inflow": [],
            "top_outflow": [],
            "hot_sectors": [],
            "success": False
        }

        if not HAS_AKSHARE:
            return result

        try:
            df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
            if df is None or df.empty:
                return result

            # 查找净流入列
            flow_col = None
            for col in df.columns:
                if "净流入" in col and "净额" in col:
                    flow_col = col
                    break
            if flow_col is None:
                for col in df.columns:
                    if "净流入" in col:
                        flow_col = col
                        break

            name_col = None
            for col in df.columns:
                if "名称" in col or "行业" in col:
                    name_col = col
                    break

            if flow_col is None or name_col is None:
                return result

            df[flow_col] = pd.to_numeric(df[flow_col], errors="coerce")
            df = df.dropna(subset=[flow_col])
            df = df.sort_values(flow_col, ascending=False)

            # Top5流入
            for _, row in df.head(5).iterrows():
                result["top_inflow"].append({
                    "sector": row[name_col],
                    "net_inflow": round(float(row[flow_col]) / 1e8, 2),  # 转亿
                })

            # Top5流出
            for _, row in df.tail(5).iterrows():
                result["top_outflow"].append({
                    "sector": row[name_col],
                    "net_inflow": round(float(row[flow_col]) / 1e8, 2),
                })

            # 热门行业
            result["hot_sectors"] = [item["sector"] for item in result["top_inflow"][:3]]
            result["success"] = True

        except Exception as e:
            logger.warning(f"[资金流] 行业资金流向获取失败: {e}")

        self._set_cache("sector_flow", result)
        return result

    # ============================================================
    # 三、个股主力资金
    # ============================================================

    def get_stock_main_flow(self, code: str) -> dict:
        """
        获取个股主力资金流向
        
        返回:
            {
                "code": str,
                "today_main_net": float,   # 今日主力净流入(万)
                "5d_main_net": float,      # 5日主力净流入(万)
                "signal": str,             # "strong_inflow"/"inflow"/"outflow"/"strong_outflow"
                "success": bool
            }
        """
        cache_key = f"stock_flow_{code}"
        cached = self._get_cache(cache_key)
        if cached:
            return cached

        result = {
            "code": code,
            "today_main_net": 0,
            "5d_main_net": 0,
            "signal": "neutral",
            "success": False
        }

        if not HAS_AKSHARE:
            return result

        try:
            market = "sh" if code.startswith("6") else "sz"
            df = ak.stock_individual_fund_flow(stock=code, market=market)
            if df is None or df.empty:
                return result

            # 找主力净流入列
            flow_col = None
            for col in df.columns:
                if "主力净流入" in col and "净额" in col:
                    flow_col = col
                    break
            if flow_col is None:
                for col in df.columns:
                    if "主力" in col and "净" in col:
                        flow_col = col
                        break

            if flow_col is None:
                return result

            df[flow_col] = pd.to_numeric(df[flow_col], errors="coerce")
            flows = df[flow_col].dropna().values

            if len(flows) >= 1:
                result["today_main_net"] = round(float(flows[-1]) / 1e4, 2)  # 转万
            if len(flows) >= 5:
                result["5d_main_net"] = round(float(flows[-5:].sum()) / 1e4, 2)

            # 信号
            if result["5d_main_net"] > 5000:  # >5000万
                result["signal"] = "strong_inflow"
            elif result["5d_main_net"] > 0:
                result["signal"] = "inflow"
            elif result["5d_main_net"] > -5000:
                result["signal"] = "outflow"
            else:
                result["signal"] = "strong_outflow"

            result["success"] = True

        except Exception as e:
            logger.debug(f"[资金流] {code}主力资金获取失败: {e}")

        self._set_cache(cache_key, result)
        return result

    # ============================================================
    # 四、资金流综合评分
    # ============================================================

    def calc_flow_score(self, code: str) -> dict:
        """
        计算个股资金流综合评分(0-100)
        
        维度:
        - 北向资金大环境 (0-30分)
        - 行业资金热度 (0-30分)
        - 个股主力动向 (0-40分)
        """
        north = self.get_northbound_flow()
        stock_flow = self.get_stock_main_flow(code)
        sector_flow = self.get_sector_flow()

        # 北向大环境 (0-30)
        north_score = 15  # 中性
        if north.get("success"):
            if north["trend"] == "strong_inflow":
                north_score = 30
            elif north["trend"] == "inflow":
                north_score = 22
            elif north["trend"] == "outflow":
                north_score = 8
            elif north["trend"] == "strong_outflow":
                north_score = 2

        # 行业热度 (0-30)
        sector_score = 15
        stock_sector = self._get_stock_sector(code)
        if sector_flow.get("success") and stock_sector:
            hot = sector_flow.get("hot_sectors", [])
            # 模糊匹配行业
            for h in hot:
                if stock_sector in h or h in stock_sector:
                    sector_score = 28
                    break
            # 检查是否在流出行业
            for item in sector_flow.get("top_outflow", []):
                if stock_sector in item.get("sector", "") or item.get("sector", "") in stock_sector:
                    sector_score = 5
                    break

        # 个股主力 (0-40)
        stock_score = 20
        if stock_flow.get("success"):
            if stock_flow["signal"] == "strong_inflow":
                stock_score = 40
            elif stock_flow["signal"] == "inflow":
                stock_score = 30
            elif stock_flow["signal"] == "outflow":
                stock_score = 10
            elif stock_flow["signal"] == "strong_outflow":
                stock_score = 2

        total = north_score + sector_score + stock_score

        return {
            "code": code,
            "total_score": total,
            "north_score": north_score,
            "sector_score": sector_score,
            "stock_score": stock_score,
            "north_trend": north.get("trend", "unknown"),
            "stock_signal": stock_flow.get("signal", "unknown"),
            "detail": f"资金评分{total}/100 | 北向{north_score} 行业{sector_score} 主力{stock_score}"
        }

    # ============================================================
    # 四点五、四级资金流分析（增量增强）
    # ============================================================

    def _fetch_multi_level_flow_df(self, code: str, days: int = 10):
        """获取个股四级资金流原始DataFrame，失败返回None"""
        if not HAS_AKSHARE:
            return None
        try:
            market = "sh" if code.startswith("6") else "sz"
            df = ak.stock_individual_fund_flow(stock=code, market=market)
            if df is None or df.empty:
                return None
            # 取最近 days 行
            df = df.tail(days).reset_index(drop=True)
            return df
        except Exception as e:
            logger.debug(f"[资金流] {code} 四级资金流获取失败: {e}")
            return None

    @staticmethod
    def _find_flow_col(df: pd.DataFrame, keywords: list) -> str:
        """在DataFrame列中按关键字顺序匹配，返回列名或None"""
        for col in df.columns:
            for kw in keywords:
                if kw in col:
                    return col
        return None

    @staticmethod
    def _calc_flow_trend(values: np.ndarray) -> str:
        """根据近几日数值判断趋势"""
        if len(values) < 2:
            return "neutral"
        recent = values[-3:] if len(values) >= 3 else values
        if all(recent[i] <= recent[i + 1] for i in range(len(recent) - 1)) and recent[-1] > 0:
            return "increasing"
        if all(recent[i] >= recent[i + 1] for i in range(len(recent) - 1)) and recent[-1] < 0:
            return "decreasing"
        avg = float(np.mean(recent))
        if avg > 0:
            return "net_inflow"
        elif avg < 0:
            return "net_outflow"
        return "neutral"

    def analyze_multi_level_flow(self, stock_code: str, days: int = 10) -> dict:
        """
        四级资金流分析（超大单/大单/中单/小单）

        返回:
            {
                "super_large": {"net_inflow": float, "trend": str},  # 超大单
                "large": {"net_inflow": float, "trend": str},        # 大单
                "medium": {"net_inflow": float, "trend": str},       # 中单
                "small": {"net_inflow": float, "trend": str},        # 小单
                "main_force_direction": str,  # "buying"/"selling"/"neutral"
                "visualization_data": {...},  # 柱状图数据（黄/蓝/绿/紫）
                "success": bool
            }
        """
        empty_result = {
            "super_large": {"net_inflow": 0, "trend": "neutral"},
            "large": {"net_inflow": 0, "trend": "neutral"},
            "medium": {"net_inflow": 0, "trend": "neutral"},
            "small": {"net_inflow": 0, "trend": "neutral"},
            "main_force_direction": "neutral",
            "visualization_data": {"dates": [], "bars": []},
            "success": False,
        }

        df = self._fetch_multi_level_flow_df(stock_code, days)
        if df is None:
            return empty_result

        # 匹配各等级列（akshare 返回列名如 "超大单净流入-净额" 等）
        col_map = {
            "super_large": self._find_flow_col(df, ["超大单"]),
            "large": self._find_flow_col(df, ["大单"]),
            "medium": self._find_flow_col(df, ["中单"]),
            "small": self._find_flow_col(df, ["小单"]),
        }

        # 至少匹配到两个等级才有效
        matched = {k: v for k, v in col_map.items() if v is not None}
        if len(matched) < 2:
            return empty_result

        # 日期列
        date_col = self._find_flow_col(df, ["日期", "date", "时间"])
        if date_col is not None:
            dates = [str(v) for v in df[date_col].tolist()]
        else:
            dates = [str(i) for i in range(len(df))]

        result = {"success": True}
        bars = []
        main_net_total = 0.0

        for level, col_name in matched.items():
            df[col_name] = pd.to_numeric(df[col_name], errors="coerce")
            vals = df[col_name].dropna().values
            net = round(float(vals[-1]), 2) if len(vals) >= 1 else 0.0
            trend = self._calc_flow_trend(vals)
            result[level] = {"net_inflow": net, "trend": trend}

            # 主力 = 超大单 + 大单，用于方向判断
            if level in ("super_large", "large"):
                tail_n = min(3, len(vals))
                main_net_total += float(np.sum(vals[-tail_n:])) if tail_n > 0 else 0

        # 主力方向
        if main_net_total > 0:
            result["main_force_direction"] = "buying"
        elif main_net_total < 0:
            result["main_force_direction"] = "selling"
        else:
            result["main_force_direction"] = "neutral"

        # 补充未匹配到的等级默认值
        for level in ("super_large", "large", "medium", "small"):
            if level not in result:
                result[level] = {"net_inflow": 0, "trend": "neutral"}

        # 构建可视化数据
        for i, date_str in enumerate(dates):
            # 主力（超大单+大单）净买入/卖出
            main_val = 0.0
            for lv in ("super_large", "large"):
                cn = col_map.get(lv)
                if cn and i < len(df):
                    v = pd.to_numeric(df[cn].iloc[i], errors="coerce")
                    if not pd.isna(v):
                        main_val += float(v)
            if main_val >= 0:
                bars.append({
                    "date": date_str, "type": "main_buy",
                    "value": round(main_val, 2), "color": "yellow",
                })
            else:
                bars.append({
                    "date": date_str, "type": "main_sell",
                    "value": round(main_val, 2), "color": "blue",
                })

            # 散户（中单+小单）
            retail_val = 0.0
            for lv in ("medium", "small"):
                cn = col_map.get(lv)
                if cn and i < len(df):
                    v = pd.to_numeric(df[cn].iloc[i], errors="coerce")
                    if not pd.isna(v):
                        retail_val += float(v)
            if retail_val >= 0:
                bars.append({
                    "date": date_str, "type": "retail_buy",
                    "value": round(retail_val, 2), "color": "green",
                })
            else:
                bars.append({
                    "date": date_str, "type": "retail_sell",
                    "value": round(retail_val, 2), "color": "purple",
                })

        result["visualization_data"] = {"dates": dates, "bars": bars}
        return result

    def detect_flow_pattern(self, stock_code: str, days: int = 10) -> dict:
        """
        资金流模式识别

        识别模式:
        - accumulation: 连续吸筹（主力连续净买入3天+但股价不涨或微涨）
        - distribution: 集中出货（主力连续净卖出3天+但股价不跌或微跌）
        - washout: 洗盘模式（大单卖出+超大单买入=对倒洗盘）
        - neutral: 无明显模式

        返回:
            {"pattern": str, "confidence": float, "days": int, "description": str}
        """
        empty_pattern = {
            "pattern": "neutral", "confidence": 0.0,
            "days": 0, "description": "数据不足，无法识别模式",
        }

        df = self._fetch_multi_level_flow_df(stock_code, days)
        if df is None:
            return empty_pattern

        # 主力净流入列
        main_col = self._find_flow_col(df, ["主力净流入"])
        super_col = self._find_flow_col(df, ["超大单"])
        large_col = self._find_flow_col(df, ["大单"])

        if main_col is None:
            return empty_pattern

        df[main_col] = pd.to_numeric(df[main_col], errors="coerce")
        main_vals = df[main_col].dropna().values

        if len(main_vals) < 3:
            return empty_pattern

        # 尝试获取涨跌幅列用于辅助判断
        chg_col = self._find_flow_col(df, ["涨跌幅", "涨幅", "change"])
        if chg_col is not None:
            df[chg_col] = pd.to_numeric(df[chg_col], errors="coerce")
            chg_vals = df[chg_col].dropna().values
        else:
            chg_vals = np.array([])

        # --- 检测连续吸筹 accumulation ---
        consec_buy = 0
        for v in reversed(main_vals):
            if v > 0:
                consec_buy += 1
            else:
                break
        if consec_buy >= 3:
            price_flat = True
            if len(chg_vals) >= consec_buy:
                recent_chg = chg_vals[-consec_buy:]
                avg_chg = float(np.mean(recent_chg))
                if avg_chg > 3.0:  # 日均涨幅>3%不算吸筹
                    price_flat = False
            if price_flat:
                confidence = min(0.5 + consec_buy * 0.1, 0.95)
                return {
                    "pattern": "accumulation",
                    "confidence": round(confidence, 2),
                    "days": consec_buy,
                    "description": f"主力连续{consec_buy}日净买入但股价未明显上涨，疑似吸筹",
                }

        # --- 检测集中出货 distribution ---
        consec_sell = 0
        for v in reversed(main_vals):
            if v < 0:
                consec_sell += 1
            else:
                break
        if consec_sell >= 3:
            price_flat = True
            if len(chg_vals) >= consec_sell:
                recent_chg = chg_vals[-consec_sell:]
                avg_chg = float(np.mean(recent_chg))
                if avg_chg < -3.0:
                    price_flat = False
            if price_flat:
                confidence = min(0.5 + consec_sell * 0.1, 0.95)
                return {
                    "pattern": "distribution",
                    "confidence": round(confidence, 2),
                    "days": consec_sell,
                    "description": f"主力连续{consec_sell}日净卖出但股价未明显下跌，疑似出货",
                }

        # --- 检测洗盘 washout ---
        if super_col is not None and large_col is not None:
            df[super_col] = pd.to_numeric(df[super_col], errors="coerce")
            df[large_col] = pd.to_numeric(df[large_col], errors="coerce")
            super_vals = df[super_col].dropna().values
            large_vals = df[large_col].dropna().values
            min_len = min(len(super_vals), len(large_vals), 3)
            if min_len >= 2:
                recent_super = super_vals[-min_len:]
                recent_large = large_vals[-min_len:]
                # 超大单净买入 & 大单净卖出 = 对倒洗盘
                super_buy_days = sum(1 for v in recent_super if v > 0)
                large_sell_days = sum(1 for v in recent_large if v < 0)
                if super_buy_days >= 2 and large_sell_days >= 2:
                    confidence = min(0.4 + min(super_buy_days, large_sell_days) * 0.15, 0.85)
                    return {
                        "pattern": "washout",
                        "confidence": round(confidence, 2),
                        "days": min_len,
                        "description": f"近{min_len}日超大单买入+大单卖出，疑似对倒洗盘",
                    }

        return {"pattern": "neutral", "confidence": 0.0, "days": 0, "description": "近期资金流无明显模式"}

    def get_enhanced_flow_score(self, stock_code: str) -> float:
        """
        增强版资金流评分（0-100）

        综合四级资金流 + 模式识别 + 原有综合评分给出评分:
        - 四级资金流分析 (权重 35%)
        - 模式识别 (权重 15%)
        - 现有主力评分 get_stock_main_flow (权重 15%)
        - 原有综合评分 calc_flow_score (权重 35%)
        """
        base_score = 50.0  # 中性起步

        # 1) 四级资金流分析
        multi = self.analyze_multi_level_flow(stock_code, days=10)
        if multi.get("success"):
            # 超大单权重最高
            weights = {
                "super_large": 0.35, "large": 0.25,
                "medium": 0.10, "small": 0.05,
            }
            flow_score = 0.0
            for level, weight in weights.items():
                net = multi.get(level, {}).get("net_inflow", 0)
                # 归一化: net>0 → +分, net<0 → -分, 以1亿为满分参考
                normalized = max(min(net / 1e8, 1.0), -1.0)
                flow_score += normalized * weight
            # flow_score 范围 [-0.75, 0.75], 映射到 [-30, 30]
            base_score += flow_score * 25

            # 主力方向加分/减分
            direction = multi.get("main_force_direction", "neutral")
            if direction == "buying":
                base_score += 3
            elif direction == "selling":
                base_score -= 3

        # 2) 模式识别
        pattern_info = self.detect_flow_pattern(stock_code, days=10)
        pattern = pattern_info.get("pattern", "neutral")
        confidence = pattern_info.get("confidence", 0)
        if pattern == "accumulation":
            base_score += 10 * confidence  # 吸筹利好
        elif pattern == "distribution":
            base_score -= 10 * confidence  # 出货利空
        elif pattern == "washout":
            base_score += 3 * confidence   # 洗盘偏中性略偏多

        # 3) 现有主力评分作为补充
        existing = self.get_stock_main_flow(stock_code)
        if existing.get("success"):
            sig = existing.get("signal", "neutral")
            sig_bonus = {
                "strong_inflow": 6, "inflow": 3,
                "outflow": -3, "strong_outflow": -6,
            }
            base_score += sig_bonus.get(sig, 0)

        # 4) 原有综合评分 calc_flow_score 作为基础参考
        calc_result = self.calc_flow_score(stock_code)
        if calc_result.get("total_score") is not None:
            # calc_flow_score 返回 0-100, 映射到 [-25, 25] 作为调整
            calc_adj = (calc_result["total_score"] - 50) * 0.4
            base_score += calc_adj

        # 限制范围 0-100
        return round(max(0.0, min(100.0, base_score)), 1)

    # ============================================================
    # 五、完整分析报告
    # ============================================================

    def full_analysis(self, codes: list = None) -> dict:
        """
        生成完整资金流向分析报告
        
        参数:
            codes: 需要分析个股资金流的代码列表（默认用STOCK_POOL）
        """
        if codes is None:
            codes = list(config.STOCK_POOL.keys())

        logger.info("[资金流] 开始全面资金流向分析...")

        north = self.get_northbound_flow()
        sector = self.get_sector_flow()

        # 个股资金流
        stock_flows = {}
        for code in codes[:15]:  # 限制数量避免请求过多
            flow = self.get_stock_main_flow(code)
            if flow.get("success"):
                stock_flows[code] = flow

        # 资金流入排名
        inflow_rank = sorted(
            stock_flows.items(),
            key=lambda x: x[1].get("5d_main_net", 0),
            reverse=True
        )

        report = {
            "northbound": north,
            "sector_flow": sector,
            "stock_flows": stock_flows,
            "inflow_rank": [(code, flow) for code, flow in inflow_rank[:5]],
            "outflow_rank": [(code, flow) for code, flow in inflow_rank[-3:]],
            "scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        # 日志输出
        if north.get("success"):
            logger.info(f"  北向资金: {north['signal']}")
        if sector.get("success"):
            logger.info(f"  热门行业: {', '.join(sector['hot_sectors'])}")
        if inflow_rank:
            logger.info(f"  主力流入TOP3:")
            for code, flow in inflow_rank[:3]:
                name = self._get_stock_name(code)
                logger.info(f"    {code} {name}: 5日主力净流入{flow['5d_main_net']:,.0f}万")

        self._flush_cache()
        return report

    # ============================================================
    # 缓存管理
    # ============================================================

    def _load_cache(self) -> dict:
        if os.path.exists(FLOW_CACHE_FILE):
            try:
                with open(FLOW_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 只保留当日缓存
                if data.get("_date") == self.today:
                    return data
            except Exception:
                pass
        return {"_date": self.today}

    def _get_cache(self, key: str):
        if key in self.cache:
            return self.cache[key]
        return None

    def _set_cache(self, key: str, value):
        self.cache[key] = value

    def _flush_cache(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            self.cache["_date"] = self.today
            with open(FLOW_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, default=str)
        except Exception as e:
            logger.debug(f"[资金流] 缓存写入失败: {e}")

    # ============================================================
    # 辅助方法
    # ============================================================

    def _get_stock_name(self, code: str) -> str:
        if code in config.STOCK_POOL:
            return config.STOCK_POOL[code].get("名称", code)
        for sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).values():
            if code in sector_info.get("stocks", {}):
                return sector_info["stocks"][code].get("名称", code)
        return code

    def _get_stock_sector(self, code: str) -> str:
        if code in config.STOCK_POOL:
            return config.STOCK_POOL[code].get("赛道", "")
        for sector_name, sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).items():
            if code in sector_info.get("stocks", {}):
                return sector_name
        return ""


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 50)
    print("  资金流向分析 - 测试")
    print("=" * 50)

    cfa = CapitalFlowAnalyzer()

    # 北向资金
    north = cfa.get_northbound_flow()
    if north["success"]:
        print(f"\n北向资金: {north['signal']}")
        print(f"  今日: {north['today_net_inflow']:.1f}亿")
        print(f"  5日: {north['5d_net_inflow']:.1f}亿")
        print(f"  连续流入: {north['consecutive_inflow_days']}天")

    # 行业资金
    sector = cfa.get_sector_flow()
    if sector["success"]:
        print(f"\n热门行业: {', '.join(sector['hot_sectors'])}")
        print("流入TOP3:")
        for item in sector["top_inflow"][:3]:
            print(f"  {item['sector']}: +{item['net_inflow']:.1f}亿")

    print("\n[OK] 资金流向模块测试完成")
