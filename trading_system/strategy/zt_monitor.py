"""
涨停监测与连板分析模块
======================
功能:
  1. 涨停/炸板股票池实时监控（akshare数据源）
  2. 连板天梯分析（1板/2板/3板/4板+分布）
  3. 板块热度检测（涨停股板块聚集度）
  4. 涨停主题归因（联动 news_monitor.py 进行新闻关联）
  5. 每日涨停综合报告生成

数据源:
  - akshare stock_zt_pool_em: 当日涨停股票池
  - akshare stock_zt_pool_zbgc_em: 当日炸板股票池

使用方式:
    from strategy.zt_monitor import ZTMonitor
    monitor = ZTMonitor()
    report = monitor.get_daily_report()
"""

import os
import sys
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 尝试导入akshare
try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False
    logger.warning("涨停监控: akshare未安装，涨停监测功能不可用")


class ZTMonitor:
    """涨停/炸板股票监控、连板天梯分析和涨停主题归因"""

    def __init__(self):
        self._zt_cache = {}   # {date_str: list}
        self._zb_cache = {}   # {date_str: list}

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _default_date(date: str = None) -> str:
        """返回 YYYYMMDD 格式日期，默认当天"""
        if date:
            return date.replace("-", "")
        return datetime.datetime.now().strftime("%Y%m%d")

    @staticmethod
    def _safe_int(val, default=0):
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(val, default=0.0):
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_str(val, default=""):
        if val is None:
            return default
        return str(val).strip()

    # ------------------------------------------------------------------
    # 1. 涨停股票池
    # ------------------------------------------------------------------
    def get_zt_pool(self, date: str = None) -> list:
        """
        获取涨停股票列表

        返回: [{code, name, price, zt_time, zt_reason, consecutive_days, ...}, ...]
        """
        date_str = self._default_date(date)
        if date_str in self._zt_cache:
            return self._zt_cache[date_str]

        if not HAS_AKSHARE:
            logger.warning("涨停监控: akshare不可用，跳过涨停池获取")
            return []

        result = []
        try:
            df = ak.stock_zt_pool_em(date=date_str)
            if df is None or df.empty:
                logger.info(f"涨停监控: {date_str} 无涨停数据")
                self._zt_cache[date_str] = result
                return result

            for _, row in df.iterrows():
                item = {
                    "code": self._safe_str(row.get("代码", "")),
                    "name": self._safe_str(row.get("名称", "")),
                    "price": self._safe_float(row.get("最新价", 0)),
                    "change_pct": self._safe_float(row.get("涨跌幅", 0)),
                    "zt_time": self._safe_str(row.get("首次封板时间", "")),
                    "zt_reason": self._safe_str(row.get("涨停统计", row.get("所属行业", ""))),
                    "consecutive_days": self._safe_int(row.get("连板数", 1)),
                    "amount": self._safe_float(row.get("成交额", 0)),
                    "circulating_mv": self._safe_float(row.get("流通市值", 0)),
                    "sector": self._safe_str(row.get("所属行业", "")),
                    "last_zb_time": self._safe_str(row.get("最后封板时间", "")),
                    "zb_count": self._safe_int(row.get("炸板次数", 0)),
                    "stat": self._safe_str(row.get("涨停统计", "")),
                }
                result.append(item)

        except Exception as e:
            logger.error(f"涨停监控: {date_str} 涨停池获取失败: {e}")

        self._zt_cache[date_str] = result
        return result

    # ------------------------------------------------------------------
    # 2. 炸板股票池
    # ------------------------------------------------------------------
    def get_zb_pool(self, date: str = None) -> list:
        """
        获取炸板股票列表

        返回: [{code, name, zt_time, zb_time, current_price, ...}, ...]
        """
        date_str = self._default_date(date)
        if date_str in self._zb_cache:
            return self._zb_cache[date_str]

        if not HAS_AKSHARE:
            logger.warning("涨停监控: akshare不可用，跳过炸板池获取")
            return []

        result = []
        try:
            df = ak.stock_zt_pool_zbgc_em(date=date_str)
            if df is None or df.empty:
                logger.info(f"涨停监控: {date_str} 无炸板数据")
                self._zb_cache[date_str] = result
                return result

            for _, row in df.iterrows():
                item = {
                    "code": self._safe_str(row.get("代码", "")),
                    "name": self._safe_str(row.get("名称", "")),
                    "zt_time": self._safe_str(row.get("涨停时间", "")),
                    "zb_time": self._safe_str(row.get("打开时间", row.get("炸板时间", ""))),
                    "current_price": self._safe_float(row.get("最新价", 0)),
                    "change_pct": self._safe_float(row.get("涨跌幅", 0)),
                    "amount": self._safe_float(row.get("成交额", 0)),
                    "circulating_mv": self._safe_float(row.get("流通市值", 0)),
                    "sector": self._safe_str(row.get("所属行业", "")),
                }
                result.append(item)

        except Exception as e:
            logger.error(f"涨停监控: {date_str} 炸板池获取失败: {e}")

        self._zb_cache[date_str] = result
        return result

    # ------------------------------------------------------------------
    # 3. 连板天梯分析
    # ------------------------------------------------------------------
    def analyze_ladder(self, date: str = None) -> dict:
        """
        分析连板天梯

        返回:
            {
                ladder: {1: count, 2: count, 3: count, "4+": count},
                total_zt: int,
                total_zb: int,
                zt_rate: float,     # 封板成功率
                max_consecutive: int,
                top_stocks: [...]   # 最高连板股
            }
        """
        date_str = self._default_date(date)
        zt_pool = self.get_zt_pool(date_str)
        zb_pool = self.get_zb_pool(date_str)

        total_zt = len(zt_pool)
        total_zb = len(zb_pool)
        total = total_zt + total_zb
        zt_rate = total_zt / total if total > 0 else 0.0

        # 连板分布
        ladder = {1: 0, 2: 0, 3: 0, "4+": 0}
        max_consecutive = 0

        for stock in zt_pool:
            days = stock.get("consecutive_days", 1)
            max_consecutive = max(max_consecutive, days)
            if days >= 4:
                ladder["4+"] += 1
            elif days >= 1:
                ladder[days] += 1

        # 最高连板股（取连板数最多的前5只）
        sorted_stocks = sorted(zt_pool, key=lambda x: x.get("consecutive_days", 0), reverse=True)
        top_stocks = []
        for s in sorted_stocks[:5]:
            if s.get("consecutive_days", 0) >= 2:
                top_stocks.append({
                    "code": s["code"],
                    "name": s["name"],
                    "consecutive_days": s["consecutive_days"],
                    "sector": s.get("sector", ""),
                    "zt_time": s.get("zt_time", ""),
                })

        logger.info(
            f"涨停监控: {date_str} 涨停{total_zt}只/炸板{total_zb}只, "
            f"封板率{zt_rate:.0%}, 最高连板{max_consecutive}板"
        )

        return {
            "ladder": ladder,
            "total_zt": total_zt,
            "total_zb": total_zb,
            "zt_rate": zt_rate,
            "max_consecutive": max_consecutive,
            "top_stocks": top_stocks,
        }

    # ------------------------------------------------------------------
    # 4. 板块热度检测
    # ------------------------------------------------------------------
    def detect_sector_heat(self, date: str = None) -> list:
        """
        检测板块热度（某板块涨停股数量突增）

        返回: [{sector_name, zt_count, is_hot, change_vs_yesterday}, ...]
        """
        date_str = self._default_date(date)
        zt_pool = self.get_zt_pool(date_str)

        # 统计当日各板块涨停数
        today_map = {}
        for s in zt_pool:
            sector = s.get("sector", "其他")
            if not sector:
                sector = "其他"
            today_map[sector] = today_map.get(sector, 0) + 1

        # 获取昨日数据做对比
        yesterday_str = self._get_prev_trade_date(date_str)
        yesterday_pool = self.get_zt_pool(yesterday_str) if yesterday_str else []
        yesterday_map = {}
        for s in yesterday_pool:
            sector = s.get("sector", "其他")
            if not sector:
                sector = "其他"
            yesterday_map[sector] = yesterday_map.get(sector, 0) + 1

        hot_threshold = config.CFG.get("zt_sector_hot_threshold", 3) if hasattr(config, "CFG") else 3

        result = []
        all_sectors = set(list(today_map.keys()) + list(yesterday_map.keys()))
        for sector in all_sectors:
            today_count = today_map.get(sector, 0)
            yesterday_count = yesterday_map.get(sector, 0)
            change = today_count - yesterday_count
            is_hot = today_count >= hot_threshold

            result.append({
                "sector_name": sector,
                "zt_count": today_count,
                "is_hot": is_hot,
                "change_vs_yesterday": change,
            })

            if change != 0:
                direction = "增加" if change > 0 else "减少"
                logger.info(
                    f"板块热度: {sector}涨停{today_count}只, "
                    f"较昨日{direction}{abs(change)}只"
                )

        # 按涨停数量降序
        result.sort(key=lambda x: x["zt_count"], reverse=True)
        return result

    # ------------------------------------------------------------------
    # 5. 新闻主题归因
    # ------------------------------------------------------------------
    def analyze_with_news(self, date: str = None) -> dict:
        """
        结合新闻监控进行涨停主题归因

        尝试导入 news_monitor.py 的 NewsMonitor / 相关函数
        将涨停股与近期新闻/政策关联
        如果 NewsMonitor 不可用，优雅降级

        返回: {theme_groups: [{theme, stocks, news_count}], ...}
        """
        date_str = self._default_date(date)
        zt_pool = self.get_zt_pool(date_str)

        if not zt_pool:
            return {"theme_groups": [], "date": date_str, "news_available": False}

        # 尝试导入 news_monitor
        news_available = False
        try:
            from strategy.news_monitor import fetch_stock_news, fetch_market_alerts, analyze_sentiment
            news_available = True
        except ImportError:
            logger.info("涨停监控: news_monitor不可用，跳过主题归因")

        if not news_available:
            # 降级：仅按板块分组
            return self._fallback_theme_group(zt_pool, date_str)

        # 获取市场要闻
        market_alerts = []
        try:
            market_alerts = fetch_market_alerts(max_count=30)
        except Exception as e:
            logger.debug(f"涨停监控: 市场要闻获取失败: {e}")

        # 对涨停股逐只拉取新闻并归因
        stock_news_map = {}
        codes = [s["code"] for s in zt_pool if s.get("code")]
        for code in codes[:20]:  # 限制最多20只，避免请求过多
            try:
                news_list = fetch_stock_news(code, max_count=5)
                if news_list:
                    stock_news_map[code] = news_list
            except Exception:
                continue

        # 按板块 + 新闻关键词聚合主题
        theme_groups = self._build_theme_groups(zt_pool, stock_news_map, market_alerts)

        return {
            "theme_groups": theme_groups,
            "date": date_str,
            "news_available": True,
            "total_news_stocks": len(stock_news_map),
        }

    def _build_theme_groups(self, zt_pool: list, stock_news_map: dict,
                            market_alerts: list) -> list:
        """基于板块和新闻关键词构建主题分组"""
        # 按板块分组
        sector_stocks = {}
        for s in zt_pool:
            sector = s.get("sector", "其他") or "其他"
            sector_stocks.setdefault(sector, []).append(s)

        # 提取新闻关键词作为主题标签
        hot_keywords = set()
        for alerts in market_alerts:
            title = alerts.get("title", "")
            for kw in self._extract_keywords(title):
                hot_keywords.add(kw)

        groups = []
        for sector, stocks in sector_stocks.items():
            # 统计该板块的新闻数
            news_count = 0
            related_keywords = []
            for s in stocks:
                code = s.get("code", "")
                if code in stock_news_map:
                    news_count += len(stock_news_map[code])
                    for n in stock_news_map[code]:
                        for kw in hot_keywords:
                            if kw in n.get("title", ""):
                                related_keywords.append(kw)

            theme = sector
            if related_keywords:
                # 去重取前3个关键词补充主题
                unique_kw = list(dict.fromkeys(related_keywords))[:3]
                theme = f"{sector}({','.join(unique_kw)})"

            groups.append({
                "theme": theme,
                "stocks": [{"code": s["code"], "name": s["name"],
                            "consecutive_days": s.get("consecutive_days", 1)}
                           for s in stocks],
                "news_count": news_count,
                "zt_count": len(stocks),
            })

        groups.sort(key=lambda x: x["zt_count"], reverse=True)
        return groups

    def _fallback_theme_group(self, zt_pool: list, date_str: str) -> dict:
        """降级方案：仅按板块分组，不做新闻归因"""
        sector_stocks = {}
        for s in zt_pool:
            sector = s.get("sector", "其他") or "其他"
            sector_stocks.setdefault(sector, []).append(s)

        groups = []
        for sector, stocks in sector_stocks.items():
            groups.append({
                "theme": sector,
                "stocks": [{"code": s["code"], "name": s["name"],
                            "consecutive_days": s.get("consecutive_days", 1)}
                           for s in stocks],
                "news_count": 0,
                "zt_count": len(stocks),
            })

        groups.sort(key=lambda x: x["zt_count"], reverse=True)
        return {"theme_groups": groups, "date": date_str, "news_available": False}

    @staticmethod
    def _extract_keywords(text: str) -> list:
        """简易关键词提取：取连续中文词（2-6字）"""
        import re
        # 提取2-6字中文词组
        words = re.findall(r'[\u4e00-\u9fff]{2,6}', text)
        # 过滤常见停用词
        stop_words = {"的", "了", "和", "是", "在", "不", "有", "与", "为",
                      "或", "等", "将", "被", "从", "到", "对", "及"}
        return [w for w in words if w not in stop_words]

    @staticmethod
    def _get_prev_trade_date(date_str: str) -> str:
        """粗略获取前一自然日（不精确排除节假日，但足够做日环比参考）"""
        try:
            dt = datetime.datetime.strptime(date_str, "%Y%m%d")
            prev = dt - datetime.timedelta(days=1)
            # 简单跳过周末
            while prev.weekday() >= 5:
                prev -= datetime.timedelta(days=1)
            return prev.strftime("%Y%m%d")
        except (ValueError, TypeError):
            return ""

    # ------------------------------------------------------------------
    # 6. 每日综合报告
    # ------------------------------------------------------------------
    def get_daily_report(self, date: str = None) -> dict:
        """
        生成每日涨停综合报告（整合天梯+板块热度+主题归因）

        返回:
            {
                date: str,
                ladder: {...},
                sector_heat: [...],
                theme_analysis: {...},
                summary: str,
            }
        """
        date_str = self._default_date(date)

        ladder = self.analyze_ladder(date_str)
        sector_heat = self.detect_sector_heat(date_str)
        theme_analysis = self.analyze_with_news(date_str)

        # 生成摘要文本
        hot_sectors = [s for s in sector_heat if s.get("is_hot")]
        hot_names = "、".join(s["sector_name"] for s in hot_sectors[:5]) if hot_sectors else "无"

        summary_parts = [
            f"日期: {date_str}",
            f"涨停{ladder['total_zt']}只 / 炸板{ladder['total_zb']}只",
            f"封板成功率: {ladder['zt_rate']:.0%}",
            f"最高连板: {ladder['max_consecutive']}板",
            f"热门板块: {hot_names}",
        ]

        if ladder.get("top_stocks"):
            top_names = "、".join(
                f"{s['name']}({s['consecutive_days']}板)" for s in ladder["top_stocks"][:3]
            )
            summary_parts.append(f"连板龙头: {top_names}")

        summary = " | ".join(summary_parts)

        return {
            "date": date_str,
            "ladder": ladder,
            "sector_heat": sector_heat,
            "theme_analysis": theme_analysis,
            "summary": summary,
        }


# ============================================================
# 命令行测试入口
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 50)
    print("  涨停监测与连板分析 - 测试")
    print("=" * 50)

    if not HAS_AKSHARE:
        print("[ERROR] akshare未安装")
        sys.exit(1)

    monitor = ZTMonitor()

    # 测试涨停池
    zt = monitor.get_zt_pool()
    print(f"\n涨停池: {len(zt)}只")
    for s in zt[:5]:
        print(f"  {s['code']} {s['name']} {s.get('consecutive_days', 1)}板 "
              f"封板时间:{s.get('zt_time', '')}")

    # 测试炸板池
    zb = monitor.get_zb_pool()
    print(f"\n炸板池: {len(zb)}只")
    for s in zb[:5]:
        print(f"  {s['code']} {s['name']} 涨停:{s.get('zt_time', '')} "
              f"炸板:{s.get('zb_time', '')}")

    # 测试连板天梯
    ladder = monitor.analyze_ladder()
    print(f"\n连板天梯: {ladder['ladder']}")
    print(f"封板率: {ladder['zt_rate']:.0%}  最高连板: {ladder['max_consecutive']}板")

    # 测试板块热度
    heat = monitor.detect_sector_heat()
    print(f"\n板块热度(Top5):")
    for h in heat[:5]:
        hot_tag = " [热]" if h["is_hot"] else ""
        print(f"  {h['sector_name']}: {h['zt_count']}只{hot_tag} "
              f"(较昨日{h['change_vs_yesterday']:+d})")

    # 测试综合报告
    report = monitor.get_daily_report()
    print(f"\n综合报告摘要:\n  {report['summary']}")

    print("\n测试完成")
