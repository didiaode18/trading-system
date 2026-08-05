"""
涨停基因跟踪模块 V1.0
======================
功能:
  1. 获取昨日/近期涨停股池
  2. 跟踪这些股票今日的表现（竞价强度、开盘涨幅、量能）
  3. 识别连板候选：高开>3% + 量比>2 → 连板概率35-45%
  4. 输出"今日连板候选"列表，供盘中重点关注

触发方式:
  - scheduler定时触发（每日9:26竞价结束后）
  - 手动: python -m trading_system.strategy.zt_gene_tracker
  - 集成到caopan_report.py选股报告

数据源:
  - akshare stock_zt_pool_previous_em: 昨日涨停股池
  - akshare stock_zh_a_spot_em: 今日实时行情（竞价/开盘后）
  - 腾讯行情API: 备用实时数据

设计原则:
  - 所有akshare调用try/except包裹，失败时logger.warning并降级
  - 网络异常不中断主流程
  - 与zt_monitor.py共享涨停池数据
"""

import os
import sys
import logging
import datetime
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False
    logger.warning("涨停基因: akshare未安装，跟踪功能不可用")

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# 涨停基因配置（从config.py读取，带默认值）
GENE_CONFIG = {
    "enabled": getattr(config, 'ZT_GENE_ENABLED', True),
    "min_open_pct": getattr(config, 'ZT_GENE_MIN_OPEN_PCT', 3.0),      # 连板候选最低高开幅度(%)
    "min_vol_ratio": getattr(config, 'ZT_GENE_MIN_VOL_RATIO', 2.0),    # 连板候选最低量比
    "track_days": getattr(config, 'ZT_GENE_TRACK_DAYS', 3),            # 跟踪最近N天涨停股
    "max_candidates": 10,                                               # 最多输出候选数
}


class ZTGeneTracker:
    """涨停基因跟踪：识别连板候选"""

    def __init__(self):
        self.config = GENE_CONFIG
        self._prev_zt_cache = {}  # {date_str: list}

    # ------------------------------------------------------------------
    # 核心入口
    # ------------------------------------------------------------------
    def track_lianban_candidates(self, date: str = None) -> dict:
        """
        跟踪连板候选

        流程:
          1. 获取昨日涨停池
          2. 获取这些股票今日实时行情
          3. 筛选高开>阈值 + 量比>阈值的连板候选
          4. 按连板概率排序输出

        返回:
            {
                "date": str,
                "success": bool,
                "prev_zt_count": int,        # 昨日涨停总数
                "candidates": [...],         # 连板候选列表
                "continued_zt": [...],       # 已确认连板的股票
                "failed_stocks": [...],      # 断板股票（高开低走）
                "summary": str,
            }
        """
        result = {
            "date": date or datetime.date.today().strftime("%Y-%m-%d"),
            "success": False,
            "prev_zt_count": 0,
            "candidates": [],
            "continued_zt": [],
            "failed_stocks": [],
            "summary": "",
        }

        if not self.config["enabled"]:
            logger.info("[涨停基因] 已禁用，跳过")
            return result

        if not HAS_AKSHARE:
            logger.warning("[涨停基因] akshare不可用，跳过")
            return result

        # 1. 获取昨日涨停池
        prev_zt = self._get_previous_zt_pool(date)
        result["prev_zt_count"] = len(prev_zt)

        if not prev_zt:
            logger.info("[涨停基因] 昨日无涨停数据，跳过")
            result["summary"] = "昨日无涨停数据"
            return result

        logger.info(f"[涨停基因] 昨日涨停{len(prev_zt)}只，开始跟踪今日表现...")

        # 2. 获取今日实时行情
        today_quotes = self._get_today_quotes([s["code"] for s in prev_zt])

        if not today_quotes:
            logger.warning("[涨停基因] 今日行情获取失败，降级为仅输出昨日涨停列表")
            # 降级：仅输出昨日涨停列表
            result["candidates"] = [{
                "code": s["code"],
                "name": s["name"],
                "prev_zt_time": s.get("zt_time", ""),
                "consecutive_days": s.get("consecutive_days", 1),
                "sector": s.get("sector", ""),
                "status": "pending",  # 待确认
            } for s in prev_zt[:self.config["max_candidates"]]]
            result["success"] = True
            result["summary"] = f"昨日涨停{len(prev_zt)}只（行情未获取，仅列表）"
            return result

        # 3. 分析每只昨日涨停股的今日表现
        candidates = []
        continued = []
        failed = []

        for stock in prev_zt:
            code = stock["code"]
            quote = today_quotes.get(code)

            if not quote:
                continue

            change_pct = quote.get("change_pct", 0)
            vol_ratio = quote.get("vol_ratio", 0)
            open_pct = quote.get("open_pct", 0)  # 开盘涨幅
            price = quote.get("price", 0)

            # 判断状态
            prev_days = stock.get("consecutive_days", 1)

            if change_pct >= 9.5:
                # 已涨停 → 确认连板
                continued.append({
                    "code": code,
                    "name": stock["name"],
                    "price": price,
                    "change_pct": change_pct,
                    "consecutive_days": prev_days + 1,
                    "sector": stock.get("sector", ""),
                    "status": "continued",
                })
            elif open_pct >= self.config["min_open_pct"] and vol_ratio >= self.config["min_vol_ratio"]:
                # 高开+放量 → 连板候选
                lianban_prob = self._estimate_lianban_probability(
                    open_pct, vol_ratio, change_pct, prev_days
                )
                candidates.append({
                    "code": code,
                    "name": stock["name"],
                    "price": price,
                    "change_pct": change_pct,
                    "open_pct": open_pct,
                    "vol_ratio": vol_ratio,
                    "prev_consecutive": prev_days,
                    "sector": stock.get("sector", ""),
                    "lianban_probability": lianban_prob,
                    "status": "candidate",
                })
            elif change_pct < 0:
                # 低开或翻绿 → 断板
                failed.append({
                    "code": code,
                    "name": stock["name"],
                    "price": price,
                    "change_pct": change_pct,
                    "prev_consecutive": prev_days,
                    "sector": stock.get("sector", ""),
                    "status": "failed",
                })

        # 按连板概率排序
        candidates.sort(key=lambda x: x.get("lianban_probability", 0), reverse=True)
        result["candidates"] = candidates[:self.config["max_candidates"]]
        result["continued_zt"] = continued
        result["failed_stocks"] = failed[:5]  # 最多显示5只断板
        result["success"] = True
        result["summary"] = self._build_summary(result)

        logger.info(f"[涨停基因] 跟踪完成: 连板候选{len(candidates)}只, "
                    f"已连板{len(continued)}只, 断板{len(failed)}只")

        return result

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------
    def _get_previous_zt_pool(self, date: str = None) -> list:
        """获取昨日涨停池"""
        # 计算昨日日期
        if date:
            target_date = datetime.datetime.strptime(date, "%Y-%m-%d").date()
        else:
            target_date = datetime.date.today()

        # 回溯找上一个交易日
        prev_date = target_date - datetime.timedelta(days=1)
        while prev_date.weekday() >= 5:  # 跳过周末
            prev_date -= datetime.timedelta(days=1)

        prev_date_str = prev_date.strftime("%Y%m%d")

        # 检查缓存
        if prev_date_str in self._prev_zt_cache:
            return self._prev_zt_cache[prev_date_str]

        # 尝试akshare获取
        result = []
        try:
            df = ak.stock_zt_pool_em(date=prev_date_str)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    result.append({
                        "code": str(row.get("代码", "")).zfill(6),
                        "name": str(row.get("名称", "")),
                        "zt_time": str(row.get("首次封板时间", "")),
                        "consecutive_days": int(row.get("连板数", 1)) if pd.notna(row.get("连板数")) else 1,
                        "sector": str(row.get("所属行业", "")),
                        "amount": float(row.get("成交额", 0)) if pd.notna(row.get("成交额")) else 0,
                    })
                logger.info(f"[涨停基因] 获取{prev_date_str}涨停池: {len(result)}只")
        except Exception as e:
            logger.warning(f"[涨停基因] 昨日涨停池获取失败: {e}")

        self._prev_zt_cache[prev_date_str] = result
        return result

    def _get_today_quotes(self, codes: list) -> dict:
        """
        获取今日实时行情（用于跟踪昨日涨停股）

        返回: {code: {price, change_pct, open_pct, vol_ratio, ...}}
        """
        if not codes:
            return {}

        result = {}

        # 方法1: akshare全市场快照（筛选目标股票）
        try:
            df = ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                col_map = {
                    "代码": "code", "名称": "name", "最新价": "price",
                    "涨跌幅": "change_pct", "今开": "open", "昨收": "prev_close",
                    "量比": "vol_ratio", "成交额": "amount",
                }
                df = df.rename(columns=col_map)
                for col in ["price", "change_pct", "open", "prev_close", "vol_ratio", "amount"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")

                # 筛选目标股票
                target_df = df[df["code"].isin(codes)]

                for _, row in target_df.iterrows():
                    code = str(row.get("code", "")).zfill(6)
                    price = float(row.get("price", 0)) if pd.notna(row.get("price")) else 0
                    prev_close = float(row.get("prev_close", 0)) if pd.notna(row.get("prev_close")) else 0
                    open_price = float(row.get("open", 0)) if pd.notna(row.get("open")) else 0

                    # 计算开盘涨幅
                    open_pct = 0
                    if prev_close > 0 and open_price > 0:
                        open_pct = (open_price - prev_close) / prev_close * 100

                    result[code] = {
                        "price": price,
                        "change_pct": float(row.get("change_pct", 0)) if pd.notna(row.get("change_pct")) else 0,
                        "open_pct": open_pct,
                        "vol_ratio": float(row.get("vol_ratio", 0)) if pd.notna(row.get("vol_ratio")) else 0,
                        "amount": float(row.get("amount", 0)) if pd.notna(row.get("amount")) else 0,
                    }

                logger.info(f"[涨停基因] 获取今日行情: {len(result)}/{len(codes)}只")
                return result
        except Exception as e:
            logger.warning(f"[涨停基因] akshare行情获取失败: {e}")

        # 方法2: 腾讯行情API（备用）
        # FIX: 修复错误引用不存在的fetch_realtime_quotes，改用data.realtime.fetch_realtime_tencent
        try:
            try:
                from data.realtime import fetch_realtime_tencent
            except ImportError:
                from trading_system.data.realtime import fetch_realtime_tencent
            quotes = fetch_realtime_tencent(codes)
            if quotes:
                for code, q in quotes.items():
                    prev_close = q.get("prev_close", 0)
                    open_price = q.get("open", 0)
                    open_pct = 0
                    if prev_close > 0 and open_price > 0:
                        open_pct = (open_price - prev_close) / prev_close * 100

                    result[code] = {
                        "price": q.get("price", 0),
                        "change_pct": q.get("change_pct", 0),
                        "open_pct": open_pct,
                        "vol_ratio": q.get("vol_ratio", 0),
                        "amount": q.get("amount", 0),
                    }
                logger.info(f"[涨停基因] 腾讯API获取行情: {len(result)}/{len(codes)}只")
        except Exception as e:
            logger.warning(f"[涨停基因] 腾讯API行情获取失败: {e}")

        return result

    # ------------------------------------------------------------------
    # 连板概率估算
    # ------------------------------------------------------------------
    def _estimate_lianban_probability(self, open_pct: float, vol_ratio: float,
                                      change_pct: float, prev_days: int) -> float:
        """
        估算连板概率（简化模型）

        因子:
          - 高开幅度: >5%概率更高
          - 量比: >3说明资金抢筹
          - 当前涨幅: 越接近涨停概率越高
          - 昨日连板数: 2板以上延续性更强
        """
        prob = 0.0

        # 高开因子 (0-0.3)
        if open_pct >= 7:
            prob += 0.3
        elif open_pct >= 5:
            prob += 0.25
        elif open_pct >= 3:
            prob += 0.15

        # 量比因子 (0-0.25)
        if vol_ratio >= 4:
            prob += 0.25
        elif vol_ratio >= 3:
            prob += 0.2
        elif vol_ratio >= 2:
            prob += 0.1

        # 当前涨幅因子 (0-0.3)
        if change_pct >= 9:
            prob += 0.3
        elif change_pct >= 7:
            prob += 0.2
        elif change_pct >= 5:
            prob += 0.1

        # 连板基础因子 (0-0.15)
        if prev_days >= 3:
            prob += 0.15
        elif prev_days >= 2:
            prob += 0.1
        else:
            prob += 0.05

        return min(prob, 0.90)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _build_summary(self, result: dict) -> str:
        """构建摘要"""
        parts = [
            f"昨日涨停: {result['prev_zt_count']}只",
            f"连板候选: {len(result['candidates'])}只",
            f"已连板: {len(result['continued_zt'])}只",
            f"断板: {len(result['failed_stocks'])}只",
        ]

        if result["candidates"]:
            top = result["candidates"][0]
            parts.append(f"最强候选: {top['name']}(高开{top.get('open_pct', 0):.1f}%)")

        return " | ".join(parts)

    def print_console_report(self, result: dict):
        """控制台输出"""
        if not result.get("success"):
            print("  [涨停基因] 跟踪未成功")
            return

        print(f"\n  [涨停基因跟踪] {result['date']}")
        print(f"     昨日涨停: {result['prev_zt_count']}只 | "
              f"连板候选: {len(result['candidates'])}只 | "
              f"已连板: {len(result['continued_zt'])}只")

        if result["continued_zt"]:
            print(f"\n     -- 已确认连板 --")
            for s in result["continued_zt"][:5]:
                print(f"     [连板] {s['code']} {s['name']} | "
                      f"{s['consecutive_days']}板 | {s['sector']}")

        if result["candidates"]:
            print(f"\n     -- 连板候选（高开>={self.config['min_open_pct']:.0f}% + 量比>={self.config['min_vol_ratio']:.0f}）--")
            for s in result["candidates"][:8]:
                # 处理降级模式（可能没有change_pct字段）
                change_pct = s.get('change_pct', 0)
                open_pct = s.get('open_pct', 0)
                vol_ratio = s.get('vol_ratio', 0)
                prob = s.get('lianban_probability', 0)
                if s.get('status') == 'pending':
                    # 降级模式：仅显示基本信息
                    print(f"     [候选] {s['code']} {s['name']} | "
                          f"昨{s.get('prev_consecutive', s.get('consecutive_days', 1))}板 | {s.get('sector', '')}")
                else:
                    print(f"     [候选] {s['code']} {s['name']} | "
                          f"高开{open_pct:.1f}% | 量比{vol_ratio:.1f} | "
                          f"当前+{change_pct:.1f}% | 概率{prob:.0%}")

        if result["failed_stocks"]:
            print(f"\n     -- 断板警示 --")
            for s in result["failed_stocks"][:3]:
                print(f"     [断板] {s['code']} {s['name']} | "
                      f"昨{s['prev_consecutive']}板 | 今日{s['change_pct']:.1f}%")


# ============================================================
# 便捷函数
# ============================================================

def run_zt_gene_tracking(date: str = None) -> dict:
    """
    执行涨停基因跟踪（便捷入口）

    参数:
        date: 指定日期（YYYY-MM-DD），默认今天

    返回:
        跟踪结果dict
    """
    tracker = ZTGeneTracker()
    result = tracker.track_lianban_candidates(date)

    if result.get("success"):
        tracker.print_console_report(result)

    return result


# ============================================================
# 命令行测试入口
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("=" * 50)
    print("  涨停基因跟踪 - 测试")
    print("=" * 50)

    result = run_zt_gene_tracking()

    if result.get("success"):
        print(f"\n[OK] 跟踪成功")
        print(f"   摘要: {result['summary']}")
    else:
        print(f"\n[FAIL] 跟踪失败（可能是非交易日或数据未更新）")
