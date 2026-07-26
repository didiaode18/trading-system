"""
股东户数变化 + 大宗交易监控模块 V1.0
====================================
追踪个股股东户数变化趋势（筹码集中度代理指标）和大宗交易记录（机构/游资大额买卖）。

核心功能:
  1. 股东户数变化趋势分析（筹码集中度判断）
  2. 大宗交易明细分析（机构/游资动向）
  3. 综合分析评分（供 multi_factor 选股集成）
  4. 批量分析

数据来源: akshare（东方财富）

信号判定逻辑:
  - 股东户数连续2期以上减少 → 筹码集中，看多信号
  - 股东户数单期增加超过10% → 筹码分散，看空信号
  - 大宗交易净买入且折价率<5% → 机构认可，加分
  - 大宗交易折价率>10% → 风险预警

使用方式:
    from strategy.holder_monitor import HolderMonitor
    hm = HolderMonitor()
    result = hm.analyze("000001")
    score = hm.get_holder_factor("000001")
"""

import logging
import datetime
import time
from typing import List, Dict, Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False
    logger.warning("[筹码监控] akshare 未安装，功能不可用")

# ============================================================
# 模块内配置（不修改 config.py）
# ============================================================
CFG = {
    "HOLDER_CACHE_DAYS": 90,         # 股东户数缓存天数（季报频率，长周期缓存）
    "BLOCK_TRADE_CACHE_HOURS": 12,   # 大宗交易缓存小时数
    "CONCENTRATE_THRESHOLD": 2,      # 连续减少期数判定筹码集中
    "DISPERSE_INCREASE_PCT": 10.0,   # 单期增加超此比例判定筹码分散
    "DISCOUNT_LOW": 5.0,             # 折价率低于此值 = 机构认可
    "DISCOUNT_HIGH": 10.0,           # 折价率高于此值 = 风险预警
    "BLOCK_TRADE_DAYS": 30,          # 默认大宗交易分析天数
    "BATCH_SLEEP": 0.3,              # 批量分析间隔（秒）
}


class HolderMonitor:
    """股东户数 + 大宗交易监控"""

    def __init__(self):
        # 内存缓存：股东户数按股票代码缓存（长周期）
        self._holder_cache: Dict[str, dict] = {}
        # 大宗交易按 "stock_code_date" 按日缓存
        self._block_cache: Dict[str, dict] = {}

    # ============================================================
    # 一、股东户数变化
    # ============================================================

    def get_holder_count(self, stock_code: str) -> dict:
        """
        获取股东户数变化

        返回:
            {
                "current_count": int,         # 最新股东户数
                "change_rate": float,         # 环比变化率（负数=集中）
                "consecutive_decrease": int,  # 连续减少期数
                "trend": str,                 # "concentrating"/"dispersing"/"stable"
                "history": list,              # 最近5期历史
                "success": bool
            }
        """
        cache_key = stock_code
        cached = self._get_holder_cache(cache_key)
        if cached is not None:
            return cached

        result = {
            "current_count": 0,
            "change_rate": 0.0,
            "consecutive_decrease": 0,
            "trend": "stable",
            "history": [],
            "success": False,
        }

        if not HAS_AKSHARE:
            return result

        try:
            df = ak.stock_zh_a_gdhs_detail_em(symbol=stock_code)
            if df is None or df.empty:
                logger.warning(f"[筹码监控] {stock_code} 股东户数数据为空")
                return result

            # 按截止日期升序排列（最新在最后）
            date_col = "股东户数统计截止日"
            count_col = "股东户数-本次"
            prev_col = "股东户数-上次"
            change_pct_col = "股东户数-增减比例"

            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
            df = df.sort_values(date_col).reset_index(drop=True)

            counts = df[count_col].values
            change_pcts = df[change_pct_col].values

            if len(counts) == 0:
                return result

            result["current_count"] = int(counts[-1])

            # 最新一期变化率
            if len(change_pcts) >= 1 and not pd.isna(change_pcts[-1]):
                result["change_rate"] = round(float(change_pcts[-1]), 2)
            elif len(counts) >= 2 and counts[-2] != 0:
                result["change_rate"] = round(
                    (counts[-1] - counts[-2]) / counts[-2] * 100, 2
                )

            # 连续减少期数
            consecutive_decrease = 0
            for pct in reversed(change_pcts):
                if pd.isna(pct):
                    break
                if float(pct) < 0:
                    consecutive_decrease += 1
                else:
                    break
            result["consecutive_decrease"] = consecutive_decrease

            # 趋势判定
            disperse_threshold = CFG.get("DISPERSE_INCREASE_PCT", 10.0)
            concentrate_min = CFG.get("CONCENTRATE_THRESHOLD", 2)

            if consecutive_decrease >= concentrate_min:
                result["trend"] = "concentrating"
            elif result["change_rate"] > disperse_threshold:
                result["trend"] = "dispersing"
            else:
                result["trend"] = "stable"

            # 最近5期历史
            recent = df.tail(5)
            for _, row in recent.iterrows():
                result["history"].append({
                    "date": str(row[date_col])[:10],
                    "count": int(row[count_col]),
                    "change_pct": round(float(row[change_pct_col]), 2) if not pd.isna(row[change_pct_col]) else 0.0,
                })

            result["success"] = True

        except Exception as e:
            logger.warning(f"[筹码监控] {stock_code} 股东户数获取失败: {e}")

        self._set_holder_cache(cache_key, result)
        return result

    # ============================================================
    # 二、大宗交易分析
    # ============================================================

    def get_block_trades(self, stock_code: str, days: int = 30) -> dict:
        """
        获取大宗交易分析

        返回:
            {
                "total_buy_amount": float,   # 大宗买入总额
                "total_sell_amount": float,  # 大宗卖出总额
                "avg_discount": float,       # 平均折价率（%）
                "net_flow": float,           # 净买入（买入-卖出）
                "trade_count": int,          # 交易笔数
                "trades": list,              # 交易明细列表
                "success": bool
            }
        """
        today_str = datetime.date.today().strftime("%Y%m%d")
        cache_key = f"{stock_code}_{today_str}"
        cached = self._get_block_cache(cache_key)
        if cached is not None:
            return cached

        result = {
            "total_buy_amount": 0.0,
            "total_sell_amount": 0.0,
            "avg_discount": 0.0,
            "net_flow": 0.0,
            "trade_count": 0,
            "trades": [],
            "success": False,
        }

        if not HAS_AKSHARE:
            return result

        try:
            end_date = datetime.date.today().strftime("%Y%m%d")
            start_date = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y%m%d")

            # stock_dzjy_mrmx(symbol='A股', start_date, end_date) 返回全市场大宗交易
            df = ak.stock_dzjy_mrmx(
                symbol="A股",
                start_date=start_date,
                end_date=end_date,
            )
            if df is None or df.empty:
                logger.debug(f"[筹码监控] {stock_code} 大宗交易数据为空")
                return result

            # 过滤目标股票
            code_col = "证券代码"
            df_stock = df[df[code_col] == stock_code].copy()

            if df_stock.empty:
                # 该股票近期无大宗交易
                result["success"] = True
                self._set_block_cache(cache_key, result)
                return result

            # 关键列
            discount_col = "折溢率"       # 折价率（负数=折价，正数=溢价）
            amount_col = "成交额"
            date_col = "交易日期"
            price_col = "成交价"
            close_col = "收盘价"
            buyer_col = "买方营业部"
            seller_col = "卖方营业部"
            volume_col = "成交量"

            df_stock[discount_col] = pd.to_numeric(df_stock[discount_col], errors="coerce")
            df_stock[amount_col] = pd.to_numeric(df_stock[amount_col], errors="coerce")

            trades_list = []
            total_buy = 0.0
            total_sell = 0.0
            discounts = []

            for _, row in df_stock.iterrows():
                amount = float(row[amount_col]) if not pd.isna(row[amount_col]) else 0.0
                discount = float(row[discount_col]) * 100 if not pd.isna(row[discount_col]) else 0.0

                # 判断买卖方向：
                # 买方营业部含"机构专用" → 机构买入
                # 卖方营业部含"机构专用" → 机构卖出
                # 无法区分时按成交额各半
                buyer = str(row.get(buyer_col, ""))
                seller = str(row.get(seller_col, ""))

                is_inst_buy = "机构" in buyer
                is_inst_sell = "机构" in seller

                if is_inst_buy and not is_inst_sell:
                    total_buy += amount
                elif is_inst_sell and not is_inst_buy:
                    total_sell += amount
                else:
                    # 双方都是机构 或 都不是机构 → 按折价方向推断
                    # 折价成交（discount<0）通常视为买方有利（主动买入）
                    if discount < 0:
                        total_buy += amount
                    else:
                        total_sell += amount

                discounts.append(discount)

                trades_list.append({
                    "date": str(row[date_col])[:10],
                    "price": float(row[price_col]) if not pd.isna(row[price_col]) else 0.0,
                    "close": float(row[close_col]) if not pd.isna(row[close_col]) else 0.0,
                    "discount_pct": round(discount, 2),
                    "volume": int(row[volume_col]) if not pd.isna(row[volume_col]) else 0,
                    "amount": round(amount, 2),
                    "buyer": buyer,
                    "seller": seller,
                })

            result["total_buy_amount"] = round(total_buy, 2)
            result["total_sell_amount"] = round(total_sell, 2)
            result["avg_discount"] = round(float(np.mean(discounts)), 2) if discounts else 0.0
            result["net_flow"] = round(total_buy - total_sell, 2)
            result["trade_count"] = len(trades_list)
            result["trades"] = trades_list
            result["success"] = True

        except Exception as e:
            logger.warning(f"[筹码监控] {stock_code} 大宗交易获取失败: {e}")

        self._set_block_cache(cache_key, result)
        return result

    # ============================================================
    # 三、综合分析
    # ============================================================

    def analyze(self, stock_code: str) -> dict:
        """
        综合分析

        返回:
            {
                "stock_code": str,
                "holder_trend": str,         # 筹码集中趋势
                "block_trade_signal": str,   # "bullish"/"bearish"/"neutral"
                "chip_concentration": float, # 筹码集中度评分 0-100
                "risk_warning": bool,        # 大宗折价过大风险预警
                "description": str           # 可读描述
            }
        """
        holder = self.get_holder_count(stock_code)
        block = self.get_block_trades(stock_code, days=CFG.get("BLOCK_TRADE_DAYS", 30))

        # ---------- 筹码集中度评分 (0-100) ----------
        score = 50.0  # 中性起步

        # 1) 股东户数趋势评分 (权重 60%)
        if holder.get("success"):
            trend = holder.get("trend", "stable")
            consecutive = holder.get("consecutive_decrease", 0)
            change_rate = holder.get("change_rate", 0.0)

            if trend == "concentrating":
                # 连续减少越多，分越高（最高+40）
                score += min(20 + consecutive * 10, 40)
            elif trend == "dispersing":
                # 大幅增加，扣分
                score -= min(20 + abs(change_rate) * 0.5, 35)
            else:
                # 稳定，小幅调整
                if change_rate < -5:
                    score += 8
                elif change_rate < 0:
                    score += 4

        # 2) 大宗交易评分 (权重 40%)
        block_signal = "neutral"
        risk_warning = False

        if block.get("success") and block.get("trade_count", 0) > 0:
            net_flow = block.get("net_flow", 0.0)
            avg_discount = abs(block.get("avg_discount", 0.0))
            discount_low = CFG.get("DISCOUNT_LOW", 5.0)
            discount_high = CFG.get("DISCOUNT_HIGH", 10.0)

            # 净买入 + 折价率小 → 看多
            if net_flow > 0 and avg_discount < discount_low:
                block_signal = "bullish"
                score += 15
            elif net_flow > 0:
                block_signal = "bullish"
                score += 8
            elif net_flow < 0 and avg_discount >= discount_high:
                block_signal = "bearish"
                score -= 15
            elif net_flow < 0:
                block_signal = "bearish"
                score -= 8

            # 风险预警：折价率过大
            if avg_discount >= discount_high:
                risk_warning = True

        # 限制范围
        score = max(0.0, min(100.0, score))

        # ---------- 可读描述 ----------
        description = self._build_description(stock_code, holder, block, score, risk_warning)

        return {
            "stock_code": stock_code,
            "holder_trend": holder.get("trend", "unknown"),
            "block_trade_signal": block_signal,
            "chip_concentration": round(score, 1),
            "risk_warning": risk_warning,
            "description": description,
            "holder_detail": holder,
            "block_detail": block,
        }

    # ============================================================
    # 四、批量分析
    # ============================================================

    def batch_analyze(self, stock_codes: list) -> dict:
        """
        批量分析

        返回:
            {
                "results": {code: analyze_result, ...},
                "ranked": [(code, score), ...],  # 按筹码集中度排序
                "risk_stocks": [code, ...],       # 风险预警股票
            }
        """
        results = {}
        sleep_sec = CFG.get("BATCH_SLEEP", 0.3)

        for code in stock_codes:
            try:
                results[code] = self.analyze(code)
            except Exception as e:
                logger.warning(f"[筹码监控] {code} 批量分析失败: {e}")
                results[code] = {
                    "stock_code": code,
                    "holder_trend": "unknown",
                    "block_trade_signal": "neutral",
                    "chip_concentration": 50.0,
                    "risk_warning": False,
                    "description": f"分析失败: {e}",
                }
            time.sleep(sleep_sec)

        # 按筹码集中度排序
        ranked = sorted(
            [(code, r.get("chip_concentration", 50.0)) for code, r in results.items()],
            key=lambda x: x[1],
            reverse=True,
        )

        # 风险预警股票
        risk_stocks = [code for code, r in results.items() if r.get("risk_warning", False)]

        return {
            "results": results,
            "ranked": ranked,
            "risk_stocks": risk_stocks,
        }

    # ============================================================
    # 五、筹码因子分数（供 multi_factor 集成）
    # ============================================================

    def get_holder_factor(self, stock_code: str) -> float:
        """
        返回 0-100 的筹码因子分数，供 multi_factor 选股集成。

        评分逻辑:
          - 基础分 50
          - 股东户数连续减少 → 加分（筹码集中）
          - 大宗交易净买入 → 加分
          - 大宗大幅折价 → 减分
        """
        analysis = self.analyze(stock_code)
        return analysis.get("chip_concentration", 50.0)

    # ============================================================
    # 内部方法
    # ============================================================

    def _build_description(
        self, stock_code: str, holder: dict, block: dict,
        score: float, risk_warning: bool
    ) -> str:
        """构建可读描述"""
        parts = []

        # 股东户数部分
        if holder.get("success"):
            trend = holder.get("trend", "stable")
            current = holder.get("current_count", 0)
            change = holder.get("change_rate", 0.0)
            consecutive = holder.get("consecutive_decrease", 0)

            if trend == "concentrating":
                parts.append(
                    f"股东户数{current}户，连续{consecutive}期减少，筹码持续集中"
                )
            elif trend == "dispersing":
                parts.append(
                    f"股东户数{current}户，环比增加{change:.1f}%，筹码明显分散"
                )
            else:
                direction = "减少" if change < 0 else "增加"
                parts.append(
                    f"股东户数{current}户，环比{direction}{abs(change):.1f}%，趋势平稳"
                )
        else:
            parts.append("股东户数数据获取失败")

        # 大宗交易部分
        if block.get("success") and block.get("trade_count", 0) > 0:
            net = block.get("net_flow", 0.0)
            avg_disc = block.get("avg_discount", 0.0)
            count = block.get("trade_count", 0)

            flow_dir = "净买入" if net > 0 else "净卖出"
            parts.append(
                f"近{CFG.get('BLOCK_TRADE_DAYS', 30)}日大宗交易{count}笔，"
                f"{flow_dir}{abs(net)/10000:.0f}万，平均折价{abs(avg_disc):.1f}%"
            )

            if risk_warning:
                parts.append("⚠️ 大宗折价率过大，存在抛压风险")
        else:
            parts.append("近期无大宗交易记录")

        # 综合评分
        if score >= 70:
            parts.append("筹码面偏多")
        elif score <= 30:
            parts.append("筹码面偏空")
        else:
            parts.append("筹码面中性")

        return "；".join(parts)

    # ============================================================
    # 缓存管理
    # ============================================================

    def _get_holder_cache(self, key: str) -> Optional[dict]:
        """获取股东户数缓存（长周期，按季度有效）"""
        if key not in self._holder_cache:
            return None
        cached = self._holder_cache[key]
        cached_time = cached.get("_cached_time", "")
        if not cached_time:
            return None
        try:
            cached_dt = datetime.datetime.strptime(cached_time, "%Y-%m-%d %H:%M:%S")
            delta = datetime.datetime.now() - cached_dt
            cache_days = CFG.get("HOLDER_CACHE_DAYS", 90)
            if delta.days < cache_days:
                return cached
        except (ValueError, TypeError):
            pass
        return None

    def _set_holder_cache(self, key: str, value: dict):
        """设置股东户数缓存"""
        value["_cached_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._holder_cache[key] = value

    def _get_block_cache(self, key: str) -> Optional[dict]:
        """获取大宗交易缓存（按日缓存）"""
        if key not in self._block_cache:
            return None
        cached = self._block_cache[key]
        cached_time = cached.get("_cached_time", "")
        if not cached_time:
            return None
        try:
            cached_dt = datetime.datetime.strptime(cached_time, "%Y-%m-%d %H:%M:%S")
            delta = datetime.datetime.now() - cached_dt
            cache_hours = CFG.get("BLOCK_TRADE_CACHE_HOURS", 12)
            if delta.total_seconds() < cache_hours * 3600:
                return cached
        except (ValueError, TypeError):
            pass
        return None

    def _set_block_cache(self, key: str, value: dict):
        """设置大宗交易缓存"""
        value["_cached_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._block_cache[key] = value

    def clear_cache(self):
        """清空所有缓存"""
        self._holder_cache.clear()
        self._block_cache.clear()
        logger.info("[筹码监控] 缓存已清空")


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    print("=" * 60)
    print("  股东户数 + 大宗交易监控模块 - 测试")
    print("=" * 60)

    hm = HolderMonitor()

    # 测试个股分析
    test_code = "000001"
    print(f"\n--- {test_code} 股东户数 ---")
    holder = hm.get_holder_count(test_code)
    if holder["success"]:
        print(f"  最新股东户数: {holder['current_count']}")
        print(f"  环比变化率: {holder['change_rate']:.2f}%")
        print(f"  连续减少期数: {holder['consecutive_decrease']}")
        print(f"  趋势: {holder['trend']}")

    print(f"\n--- {test_code} 大宗交易 ---")
    block = hm.get_block_trades(test_code, days=30)
    if block["success"]:
        print(f"  交易笔数: {block['trade_count']}")
        print(f"  买入总额: {block['total_buy_amount']/10000:.0f}万")
        print(f"  卖出总额: {block['total_sell_amount']/10000:.0f}万")
        print(f"  净买入: {block['net_flow']/10000:.0f}万")
        print(f"  平均折价: {block['avg_discount']:.2f}%")

    print(f"\n--- {test_code} 综合分析 ---")
    analysis = hm.analyze(test_code)
    print(f"  筹码集中趋势: {analysis['holder_trend']}")
    print(f"  大宗交易信号: {analysis['block_trade_signal']}")
    print(f"  筹码集中度评分: {analysis['chip_concentration']}")
    print(f"  风险预警: {analysis['risk_warning']}")
    print(f"  描述: {analysis['description']}")

    print(f"\n--- {test_code} 筹码因子分数 ---")
    factor = hm.get_holder_factor(test_code)
    print(f"  因子分数: {factor}")

    print("\n[OK] 股东户数 + 大宗交易监控模块测试完成")
