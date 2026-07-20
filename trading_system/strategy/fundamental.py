"""
基本面数据自动获取模块 V1.0
============================
自动从akshare获取财报/估值/资金流数据，填充CANSLIM的C/A/I因子

核心功能:
  1. 获取个股财务指标（ROE、净利润增速、营收增速、PE、PB）
  2. 获取估值分位数（当前PE在历史中的百分位）
  3. 获取北向资金/主力资金流向
  4. 生成基本面评分（供选股引擎使用）
  5. 缓存机制（避免频繁请求，每日更新一次）

使用方式:
    from strategy.fundamental import FundamentalAnalyzer
    fa = FundamentalAnalyzer()
    score = fa.get_fundamental_score("002371")
    batch = fa.batch_update_fundamentals(["002371", "600584"])
"""

import os
import sys
import json
import logging
import datetime
from pathlib import Path

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

# 缓存文件路径
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
FUNDAMENTAL_CACHE_FILE = os.path.join(CACHE_DIR, "fundamental_cache.json")


class FundamentalAnalyzer:
    """基本面分析器"""

    def __init__(self):
        self.cache = self._load_cache()
        self.today = datetime.date.today().strftime("%Y-%m-%d")

    # ============================================================
    # 一、财务指标获取
    # ============================================================

    def get_financial_indicators(self, code: str) -> dict:
        """
        获取个股核心财务指标
        
        返回:
            {
                "code": str,
                "pe_ttm": float,          # 滚动市盈率
                "pb": float,              # 市净率
                "roe": float,             # ROE(%)
                "net_profit_growth": float, # 净利润同比增速(%)
                "revenue_growth": float,   # 营收同比增速(%)
                "gross_margin": float,     # 毛利率(%)
                "debt_ratio": float,       # 资产负债率(%)
                "pe_percentile": float,    # PE历史分位数(0-100)
                "update_date": str
            }
        """
        # 检查缓存
        cached = self._get_from_cache(code, "financial")
        if cached:
            return cached

        result = {
            "code": code,
            "pe_ttm": None,
            "pb": None,
            "roe": None,
            "net_profit_growth": None,
            "revenue_growth": None,
            "gross_margin": None,
            "debt_ratio": None,
            "pe_percentile": None,
            "update_date": self.today
        }

        if not HAS_AKSHARE:
            logger.warning(f"[基本面] akshare未安装，无法获取{code}财务数据")
            return result

        try:
            # 方法1: 通过个股指标获取PE/PB
            self._fetch_valuation(code, result)
        except Exception as e:
            logger.debug(f"[基本面] {code}估值获取失败: {e}")

        try:
            # 方法2: 通过财务分析指标获取ROE/增速
            self._fetch_financial_analysis(code, result)
        except Exception as e:
            logger.debug(f"[基本面] {code}财务指标获取失败: {e}")

        # 存入缓存
        self._save_to_cache(code, "financial", result)
        return result

    def _fetch_valuation(self, code: str, result: dict):
        """获取估值数据（PE/PB/分位数）"""
        try:
            # 使用akshare获取个股估值指标
            symbol = self._to_akshare_symbol(code)
            df = ak.stock_a_lg_indicator(symbol=code)
            if df is not None and not df.empty:
                latest = df.iloc[-1]
                if "pe_ttm" in df.columns:
                    result["pe_ttm"] = float(latest["pe_ttm"]) if pd.notna(latest["pe_ttm"]) else None
                if "pb" in df.columns:
                    result["pb"] = float(latest["pb"]) if pd.notna(latest["pb"]) else None

                # PE分位数（近3年）
                if "pe_ttm" in df.columns and len(df) > 60:
                    pe_series = df["pe_ttm"].dropna().tail(750)  # 约3年
                    if len(pe_series) > 0 and result["pe_ttm"]:
                        result["pe_percentile"] = round(
                            (pe_series < result["pe_ttm"]).sum() / len(pe_series) * 100, 1
                        )
        except Exception as e:
            logger.debug(f"[基本面] {code}估值接口异常: {e}")

    def _fetch_financial_analysis(self, code: str, result: dict):
        """获取财务分析指标"""
        try:
            symbol = self._to_akshare_symbol(code)
            df = ak.stock_financial_analysis_indicator(symbol=symbol, start_year="2023")
            if df is not None and not df.empty:
                latest = df.iloc[-1]
                # 映射列名（akshare返回中文列名）
                col_map = {
                    "净资产收益率(%)": "roe",
                    "主营业务收入增长率(%)": "revenue_growth",
                    "净利润增长率(%)": "net_profit_growth",
                    "销售毛利率(%)": "gross_margin",
                    "资产负债率(%)": "debt_ratio",
                }
                for cn_col, en_key in col_map.items():
                    if cn_col in df.columns:
                        val = latest[cn_col]
                        if pd.notna(val):
                            result[en_key] = round(float(val), 2)
        except Exception as e:
            logger.debug(f"[基本面] {code}财务分析接口异常: {e}")

    # ============================================================
    # 二、资金流向获取
    # ============================================================

    def get_capital_flow(self, code: str) -> dict:
        """
        获取个股资金流向（主力/北向）
        
        返回:
            {
                "code": str,
                "main_net_inflow": float,    # 主力净流入(万元)
                "main_net_inflow_5d": float, # 5日主力净流入
                "north_holding_change": float, # 北向持股变化(股)
                "signal": str,               # "inflow"/"outflow"/"neutral"
                "update_date": str
            }
        """
        cached = self._get_from_cache(code, "capital_flow")
        if cached:
            return cached

        result = {
            "code": code,
            "main_net_inflow": None,
            "main_net_inflow_5d": None,
            "north_holding_change": None,
            "signal": "neutral",
            "update_date": self.today
        }

        if not HAS_AKSHARE:
            return result

        try:
            # 主力资金流
            symbol = self._to_akshare_symbol(code)
            df = ak.stock_individual_fund_flow(stock=code, market=self._get_market(code))
            if df is not None and not df.empty:
                latest = df.iloc[-1]
                # 查找主力净流入列
                for col in df.columns:
                    if "主力净流入" in col and "净额" in col:
                        val = latest[col]
                        if pd.notna(val):
                            result["main_net_inflow"] = round(float(val) / 1e4, 2)  # 转为万元
                        break

                # 5日累计
                if len(df) >= 5:
                    for col in df.columns:
                        if "主力净流入" in col and "净额" in col:
                            result["main_net_inflow_5d"] = round(
                                df[col].tail(5).sum() / 1e4, 2
                            )
                            break

                # 判断信号
                if result["main_net_inflow"] and result["main_net_inflow"] > 0:
                    result["signal"] = "inflow"
                elif result["main_net_inflow"] and result["main_net_inflow"] < 0:
                    result["signal"] = "outflow"
        except Exception as e:
            logger.debug(f"[基本面] {code}资金流向获取失败: {e}")

        self._save_to_cache(code, "capital_flow", result)
        return result

    # ============================================================
    # 三、基本面综合评分
    # ============================================================

    def get_fundamental_score(self, code: str) -> dict:
        """
        计算基本面综合评分（0-100分）
        
        评分维度:
          - C因子(业绩增速): 0-30分
          - A因子(年度增长): 0-20分
          - I因子(机构/资金): 0-20分
          - 估值合理性: 0-15分
          - 财务健康度: 0-15分
        
        返回:
            {
                "code": str,
                "total_score": float,
                "c_score": float,   # 业绩增速分
                "a_score": float,   # 年度增长分
                "i_score": float,   # 机构认同分
                "v_score": float,   # 估值分
                "h_score": float,   # 健康度分
                "grade": str,       # A/B/C/D
                "detail": str
            }
        """
        fin = self.get_financial_indicators(code)
        flow = self.get_capital_flow(code)

        # C因子: 当期业绩增速 (0-30分)
        c_score = 0
        npg = fin.get("net_profit_growth")
        if npg is not None:
            if npg >= 50:
                c_score = 30
            elif npg >= 30:
                c_score = 25
            elif npg >= 20:
                c_score = 20
            elif npg >= 10:
                c_score = 12
            elif npg >= 0:
                c_score = 6
            else:
                c_score = 0
        else:
            c_score = 10  # 无数据给中性分

        # A因子: 年度增长稳定性 (0-20分)
        a_score = 0
        rg = fin.get("revenue_growth")
        if rg is not None and npg is not None:
            if rg >= 20 and npg >= 20:
                a_score = 20
            elif rg >= 10 and npg >= 10:
                a_score = 14
            elif rg >= 0 and npg >= 0:
                a_score = 8
            else:
                a_score = 2
        else:
            a_score = 8  # 中性

        # I因子: 机构/资金认同 (0-20分)
        i_score = 0
        if flow.get("signal") == "inflow":
            i_score += 12
            if flow.get("main_net_inflow_5d") and flow["main_net_inflow_5d"] > 0:
                i_score += 8
        elif flow.get("signal") == "outflow":
            i_score += 2
        else:
            i_score += 6  # 中性

        # V因子: 估值合理性 (0-15分)
        v_score = 0
        pe_pct = fin.get("pe_percentile")
        if pe_pct is not None:
            if pe_pct <= 30:
                v_score = 15  # 低估
            elif pe_pct <= 50:
                v_score = 12
            elif pe_pct <= 70:
                v_score = 8
            elif pe_pct <= 90:
                v_score = 4
            else:
                v_score = 1  # 高估
        else:
            pe = fin.get("pe_ttm")
            if pe is not None:
                if 0 < pe <= 20:
                    v_score = 13
                elif pe <= 40:
                    v_score = 10
                elif pe <= 80:
                    v_score = 6
                else:
                    v_score = 2
            else:
                v_score = 7  # 中性

        # H因子: 财务健康度 (0-15分)
        h_score = 0
        roe = fin.get("roe")
        debt = fin.get("debt_ratio")
        if roe is not None:
            if roe >= 20:
                h_score += 8
            elif roe >= 12:
                h_score += 6
            elif roe >= 6:
                h_score += 3
            else:
                h_score += 1
        else:
            h_score += 4

        if debt is not None:
            if debt <= 40:
                h_score += 7
            elif debt <= 60:
                h_score += 5
            elif debt <= 75:
                h_score += 2
            else:
                h_score += 0
        else:
            h_score += 3

        total = c_score + a_score + i_score + v_score + h_score

        # 评级
        if total >= 75:
            grade = "A"
        elif total >= 55:
            grade = "B"
        elif total >= 35:
            grade = "C"
        else:
            grade = "D"

        return {
            "code": code,
            "total_score": round(total, 1),
            "c_score": c_score,
            "a_score": a_score,
            "i_score": i_score,
            "v_score": v_score,
            "h_score": h_score,
            "grade": grade,
            "financial": fin,
            "capital_flow": flow,
            "detail": f"基本面{grade}级({total:.0f}分) | C={c_score} A={a_score} I={i_score} V={v_score} H={h_score}"
        }

    # ============================================================
    # 四、批量更新
    # ============================================================

    def batch_update_fundamentals(self, codes: list) -> dict:
        """
        批量获取基本面数据
        
        返回: {code: fundamental_score_dict}
        """
        results = {}
        total = len(codes)
        for i, code in enumerate(codes):
            try:
                score = self.get_fundamental_score(code)
                results[code] = score
                if (i + 1) % 5 == 0:
                    logger.info(f"[基本面] 进度: {i+1}/{total}")
            except Exception as e:
                logger.warning(f"[基本面] {code}获取失败: {e}")
                results[code] = {"code": code, "total_score": 0, "grade": "N/A", "detail": "获取失败"}

        # 保存缓存
        self._flush_cache()
        logger.info(f"[基本面] 批量更新完成: {len(results)}/{total}只")
        return results

    def get_canslim_fundamental(self, code: str) -> dict:
        """
        输出兼容选股引擎FUNDAMENTAL_DATA格式的数据
        
        返回: {"eps_growth_q": xx, "eps_growth_3y": xx, "has_institution": bool}
        """
        fin = self.get_financial_indicators(code)
        flow = self.get_capital_flow(code)

        return {
            "eps_growth_q": fin.get("net_profit_growth", 0) or 0,
            "eps_growth_3y": fin.get("revenue_growth", 0) or 0,  # 近似
            "has_institution": flow.get("signal") == "inflow",
        }

    # ============================================================
    # 五、缓存管理
    # ============================================================

    def _load_cache(self) -> dict:
        """加载缓存"""
        if os.path.exists(FUNDAMENTAL_CACHE_FILE):
            try:
                with open(FUNDAMENTAL_CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _get_from_cache(self, code: str, data_type: str) -> dict:
        """从缓存获取（当日有效）"""
        key = f"{code}_{data_type}"
        if key in self.cache:
            cached = self.cache[key]
            if cached.get("update_date") == self.today:
                return cached
        return None

    def _save_to_cache(self, code: str, data_type: str, data: dict):
        """保存到缓存"""
        key = f"{code}_{data_type}"
        self.cache[key] = data

    def _flush_cache(self):
        """写入缓存文件"""
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            with open(FUNDAMENTAL_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[基本面] 缓存写入失败: {e}")

    # ============================================================
    # 辅助方法
    # ============================================================

    def _to_akshare_symbol(self, code: str) -> str:
        """转换为akshare格式的代码"""
        # akshare大部分接口直接用6位数字
        return code

    def _get_market(self, code: str) -> str:
        """判断市场（sh/sz）"""
        if code.startswith("6") or code.startswith("9"):
            return "sh"
        return "sz"


# ============================================================
# 独立运行测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 50)
    print("  基本面数据模块 - 测试")
    print("=" * 50)

    fa = FundamentalAnalyzer()

    test_codes = ["002371", "600584", "603618"]
    for code in test_codes:
        print(f"\n--- {code} ---")
        score = fa.get_fundamental_score(code)
        print(f"  评分: {score['total_score']}分 ({score['grade']}级)")
        print(f"  {score['detail']}")
        fin = score.get("financial", {})
        print(f"  PE={fin.get('pe_ttm')} PB={fin.get('pb')} ROE={fin.get('roe')}%")
        print(f"  净利润增速={fin.get('net_profit_growth')}% 营收增速={fin.get('revenue_growth')}%")

    print("\n[OK] 基本面模块测试完成")
