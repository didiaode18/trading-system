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
