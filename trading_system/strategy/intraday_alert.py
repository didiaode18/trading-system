"""
盘中实时异动监测模块 V2.6
==========================
功能:
  1. 全市场实时扫描：发现涨幅>7%且量能强劲的准涨停股
  2. 封单强度评估：量比>2 + 换手率适中 → 大概率封板
  3. 板块联动预警：同板块≥2只涨停时，预警第3只跟涨候选
  4. V2.6: 放量突破启动检测(3-7%+量比>2+突破20日新高)
  5. V2.6: 涨停基因联动(昨日涨停今日高开放量)
  6. V2.6: 选股引擎盘中增量扫描联动
  7. V2.6: 首次触发即时推送(同一标的当日不重复)
  8. 邮件/控制台即时推送

触发方式:
  - scheduler定时触发（盘中每10分钟: 09:40/09:50/.../14:50）
  - 手动: python -m trading_system.strategy.intraday_alert

数据源:
  - akshare stock_zh_a_spot_em: 全市场实时行情快照
  - akshare stock_zt_pool_em: 当日涨停池（板块联动用）

设计原则:
  - 所有akshare调用try/except包裹，失败时logger.warning并降级
  - 网络异常不中断主流程
  - 结果缓存避免重复请求（同一扫描周期内）
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
    logger.warning("盘中预警: akshare未安装，异动监测不可用")

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# 盘中预警配置（从config.py读取，带默认值）
ALERT_CONFIG = {
    "enabled": getattr(config, 'INTRADAY_ALERT_ENABLED', True),
    "min_change_pct": getattr(config, 'ALERT_MIN_CHANGE_PCT', 7.0),       # 最低涨幅阈值(%)
    "min_vol_ratio": getattr(config, 'ALERT_MIN_VOL_RATIO', 1.5),        # 最低量比
    "min_amount": getattr(config, 'ALERT_MIN_AMOUNT', 3e8),              # 最低成交额(3亿)
    "max_price": getattr(config, 'ALERT_MAX_PRICE', 300),                # 最高股价
    "min_price": getattr(config, 'ALERT_MIN_PRICE', 3),                  # 最低股价
    "sector_cascade_threshold": getattr(config, 'ALERT_SECTOR_CASCADE_THRESHOLD', 2),  # 板块联动阈值
    "sector_follower_min_pct": getattr(config, 'ALERT_SECTOR_FOLLOWER_MIN_PCT', 5.0),  # 跟涨候选最低涨幅
    "sector_follower_max_pct": getattr(config, 'ALERT_SECTOR_FOLLOWER_MAX_PCT', 9.0),  # 跟涨候选最高涨幅
    "max_alerts": getattr(config, 'ALERT_MAX_COUNT', 15),                # 单次最多预警数
    "scan_interval_minutes": getattr(config, 'ALERT_SCAN_INTERVAL', 10), # V2.6: 10分钟
    # V2.6 新增
    "breakout_enabled": getattr(config, 'ALERT_BREAKOUT_ENABLED', True),
    "breakout_min_change": getattr(config, 'ALERT_BREAKOUT_MIN_CHANGE', 3.0),
    "breakout_max_change": getattr(config, 'ALERT_BREAKOUT_MAX_CHANGE', 7.0),
    "breakout_min_vol_ratio": getattr(config, 'ALERT_BREAKOUT_MIN_VOL_RATIO', 2.0),
    "breakout_min_amount": getattr(config, 'ALERT_BREAKOUT_MIN_AMOUNT', 2e8),
    "breakout_max_count": getattr(config, 'ALERT_BREAKOUT_MAX_COUNT', 10),
    "first_trigger_only": getattr(config, 'ALERT_FIRST_TRIGGER_ONLY', True),
    "screener_intraday": getattr(config, 'ALERT_SCREENER_INTRADAY', True),
    "zt_gene_linkage": getattr(config, 'ALERT_ZT_GENE_LINKAGE', True),
}

# 扫描结果缓存（避免同一周期内重复请求）
_ALERT_CACHE = {"data": None, "time": None}


class IntradayAlert:
    """盘中实时异动监测 V2.6"""

    def __init__(self):
        self.config = ALERT_CONFIG
        self._market_df = None  # 全市场行情缓存
        # V2.6: 首次触发跟踪（当日已推送过的标的，避免重复）
        self._today_alerted = set()  # {code_alerttype}
        self._today_date = datetime.date.today().isoformat()

    # ------------------------------------------------------------------
    # 核心入口
    # ------------------------------------------------------------------
    def run_scan(self, force: bool = False) -> dict:
        """
        执行一次完整的盘中异动扫描

        返回:
            {
                "scan_time": str,
                "success": bool,
                "near_zt_stocks": [...],      # 准涨停股（涨幅>7%）
                "breakout_stocks": [...],     # V2.6: 放量突破启动(3-7%)
                "sector_cascade": [...],      # 板块联动预警
                "zt_gene_alerts": [...],      # V2.6: 涨停基因联动
                "screener_picks": [...],      # V2.6: 选股引擎盘中推荐
                "zt_pool_count": int,
                "alert_count": int,
                "summary": str,
            }
        """
        result = {
            "scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "success": False,
            "near_zt_stocks": [],
            "breakout_stocks": [],
            "sector_cascade": [],
            "zt_gene_alerts": [],
            "screener_picks": [],
            "zt_pool_count": 0,
            "alert_count": 0,
            "summary": "",
        }

        if not self.config["enabled"]:
            logger.info("[盘中预警] 已禁用，跳过")
            return result

        if not HAS_AKSHARE:
            logger.warning("[盘中预警] akshare不可用，跳过")
            return result

        # 重置日期跟踪
        today_str = datetime.date.today().isoformat()
        if self._today_date != today_str:
            self._today_alerted = set()
            self._today_date = today_str

        # 1. 获取全市场实时行情
        market_df = self._fetch_market_data()
        if market_df is None or market_df.empty:
            logger.warning("[盘中预警] 全市场行情获取失败，本次扫描跳过")
            return result

        self._market_df = market_df

        # 2. 筛选准涨停股（涨幅>阈值 + 量能确认）
        near_zt = self._detect_near_zt(market_df)
        result["near_zt_stocks"] = near_zt

        # 3. V2.6: 放量突破启动检测(3-7%)
        if self.config.get("breakout_enabled", True):
            breakout = self._detect_breakout(market_df)
            result["breakout_stocks"] = breakout

        # 4. 获取当日涨停池（用于板块联动）
        zt_pool = self._fetch_zt_pool()
        result["zt_pool_count"] = len(zt_pool)

        # 5. 板块联动预警
        cascade = self._detect_sector_cascade(market_df, zt_pool)
        result["sector_cascade"] = cascade

        # 6. V2.6: 涨停基因联动
        if self.config.get("zt_gene_linkage", True):
            gene_alerts = self._detect_zt_gene_linkage(market_df)
            result["zt_gene_alerts"] = gene_alerts

        # 7. V2.6: 选股引擎盘中增量扫描
        if self.config.get("screener_intraday", True):
            screener_picks = self._run_screener_intraday(market_df)
            result["screener_picks"] = screener_picks

        # 8. V2.6: 首次触发过滤（同一标的当日不重复推送）
        if self.config.get("first_trigger_only", True):
            result = self._filter_first_trigger(result)

        # 9. 汇总
        result["alert_count"] = (len(result["near_zt_stocks"]) +
                                 len(result["breakout_stocks"]) +
                                 len(result["sector_cascade"]) +
                                 len(result["zt_gene_alerts"]) +
                                 len(result["screener_picks"]))
        result["success"] = True
        result["summary"] = self._build_summary(result)

        logger.info(f"[盘中预警] 扫描完成: 准涨停{len(result['near_zt_stocks'])}只, "
                    f"突破{len(result['breakout_stocks'])}只, "
                    f"联动{len(result['sector_cascade'])}条, "
                    f"基因{len(result['zt_gene_alerts'])}只, "
                    f"选股{len(result['screener_picks'])}只")

        return result

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------
    def _fetch_market_data(self, max_retries: int = 2):
        """获取全市场实时行情（带重试）"""
        # 检查缓存（5分钟内有效）
        if _ALERT_CACHE["data"] is not None and _ALERT_CACHE["time"] is not None:
            elapsed = (datetime.datetime.now() - _ALERT_CACHE["time"]).total_seconds()
            if elapsed < 300:  # 5分钟缓存
                return _ALERT_CACHE["data"]

        df = None
        for attempt in range(1, max_retries + 1):
            try:
                df = ak.stock_zh_a_spot_em()
                if df is not None and not df.empty:
                    break
                df = None
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.warning(f"[盘中预警] 行情获取第{attempt}次网络异常: {e}")
                if attempt < max_retries:
                    time.sleep(2)
            except Exception as e:
                logger.warning(f"[盘中预警] 行情获取第{attempt}次异常: {e}")
                if attempt < max_retries:
                    time.sleep(2)

        if df is not None and not df.empty:
            # 标准化列名
            col_map = {
                "代码": "code", "名称": "name", "最新价": "price",
                "涨跌幅": "change_pct", "成交额": "amount",
                "换手率": "turnover", "量比": "vol_ratio",
                "流通市值": "circ_mv", "所属行业": "sector",
            }
            df = df.rename(columns=col_map)
            for col in ["price", "change_pct", "amount", "turnover", "vol_ratio", "circ_mv"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            # 更新缓存
            _ALERT_CACHE["data"] = df
            _ALERT_CACHE["time"] = datetime.datetime.now()

        return df

    def _fetch_zt_pool(self) -> list:
        """获取当日涨停池"""
        try:
            from trading_system.strategy.zt_monitor import ZTMonitor
            monitor = ZTMonitor()
            return monitor.get_zt_pool()
        except Exception as e:
            logger.warning(f"[盘中预警] 涨停池获取失败: {e}")
            return []

    # ------------------------------------------------------------------
    # 准涨停检测
    # ------------------------------------------------------------------
    def _detect_near_zt(self, df) -> list:
        """
        检测准涨停股：涨幅>阈值 + 量比>阈值 + 成交额达标

        筛选逻辑:
          - 涨幅 >= min_change_pct (默认7%)
          - 量比 >= min_vol_ratio (默认1.5)
          - 成交额 >= min_amount (默认3亿)
          - 排除ST/退市/次新
          - 股价在合理区间
        """
        if df is None or df.empty:
            return []

        cfg = self.config
        filtered = df.copy()

        # 排除ST/退市
        if "name" in filtered.columns:
            filtered = filtered[~filtered["name"].str.contains("ST|退", na=False)]
            filtered = filtered[~filtered["name"].str.startswith(("N", "C"), na=False)]

        # 涨幅筛选
        if "change_pct" in filtered.columns:
            filtered = filtered[filtered["change_pct"] >= cfg["min_change_pct"]]

        # 量比筛选
        if "vol_ratio" in filtered.columns:
            filtered = filtered[filtered["vol_ratio"] >= cfg["min_vol_ratio"]]

        # 成交额筛选
        if "amount" in filtered.columns:
            filtered = filtered[filtered["amount"] >= cfg["min_amount"]]

        # 股价区间
        if "price" in filtered.columns:
            filtered = filtered[
                (filtered["price"] >= cfg["min_price"]) &
                (filtered["price"] <= cfg["max_price"])
            ]

        if filtered.empty:
            return []

        # 按涨幅降序
        filtered = filtered.sort_values("change_pct", ascending=False)

        # 构建结果
        results = []
        for _, row in filtered.head(cfg["max_alerts"]).iterrows():
            code = str(row.get("code", "")).zfill(6)
            if not code or len(code) != 6:
                continue

            change_pct = float(row.get("change_pct", 0))
            vol_ratio = float(row.get("vol_ratio", 0))
            turnover = float(row.get("turnover", 0))
            amount = float(row.get("amount", 0))
            price = float(row.get("price", 0))

            # 封板概率评估
            seal_prob = self._estimate_seal_probability(change_pct, vol_ratio, turnover, amount)

            results.append({
                "code": code,
                "name": str(row.get("name", "")),
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "vol_ratio": round(vol_ratio, 2),
                "turnover": round(turnover, 2),
                "amount_yi": round(amount / 1e8, 2),
                "sector": str(row.get("sector", "")) or self._classify_sector(str(row.get("name", ""))),
                "seal_probability": seal_prob,
                "alert_level": "high" if seal_prob >= 0.7 else "medium",
            })

        return results

    def _estimate_seal_probability(self, change_pct: float, vol_ratio: float,
                                   turnover: float, amount: float) -> float:
        """
        估算封板概率（简化模型）

        因子权重:
          - 涨幅越接近10%（或20%创业板）→ 概率越高
          - 量比>2 → 资金关注度高
          - 换手率3-15% → 健康区间（过低无人气，过高可能出货）
          - 成交额>5亿 → 大资金参与
        """
        prob = 0.0

        # 涨幅因子 (0-0.4)
        if change_pct >= 9.5:
            prob += 0.4
        elif change_pct >= 9.0:
            prob += 0.35
        elif change_pct >= 8.0:
            prob += 0.25
        elif change_pct >= 7.0:
            prob += 0.15

        # 量比因子 (0-0.3)
        if vol_ratio >= 3.0:
            prob += 0.3
        elif vol_ratio >= 2.0:
            prob += 0.2
        elif vol_ratio >= 1.5:
            prob += 0.1

        # 换手率因子 (0-0.2)
        if 3.0 <= turnover <= 15.0:
            prob += 0.2
        elif 1.0 <= turnover < 3.0:
            prob += 0.1
        elif turnover > 15.0:
            prob += 0.05  # 过高换手可能是出货

        # 成交额因子 (0-0.1)
        if amount >= 10e8:
            prob += 0.1
        elif amount >= 5e8:
            prob += 0.05

        return min(prob, 0.95)

    # ------------------------------------------------------------------
    # 板块联动检测
    # ------------------------------------------------------------------
    def _detect_sector_cascade(self, market_df, zt_pool: list) -> list:
        """
        板块联动预警：当某板块已有≥N只涨停时，预警同板块涨幅5-9%的跟涨候选

        逻辑:
          1. 统计涨停池中各板块涨停数量
          2. 筛选涨停数>=threshold的板块
          3. 在全市场中找同板块涨幅5-9%的股票
          4. 按涨幅+量比排序输出
        """
        if not zt_pool or market_df is None or market_df.empty:
            return []

        cfg = self.config
        threshold = cfg["sector_cascade_threshold"]

        # 统计涨停池板块分布
        sector_zt_count = {}
        sector_zt_names = {}
        for stock in zt_pool:
            sector = stock.get("sector", "") or "其他"
            if not sector or sector == "其他":
                continue
            sector_zt_count[sector] = sector_zt_count.get(sector, 0) + 1
            sector_zt_names.setdefault(sector, []).append(stock.get("name", ""))

        # 筛选达到联动阈值的板块
        hot_sectors = {
            sector: count for sector, count in sector_zt_count.items()
            if count >= threshold
        }

        if not hot_sectors:
            return []

        # 在全市场中找跟涨候选
        cascade_alerts = []
        for sector, zt_count in hot_sectors.items():
            # 尝试按行业列匹配（如果有sector列）
            if "sector" in market_df.columns:
                sector_stocks = market_df[market_df["sector"] == sector]
            else:
                # 降级：按名称关键词匹配
                sector_stocks = market_df[
                    market_df["name"].apply(lambda x: self._match_sector(str(x), sector))
                ]

            if sector_stocks.empty:
                continue

            # 筛选涨幅在跟涨区间的股票
            followers = sector_stocks[
                (sector_stocks["change_pct"] >= cfg["sector_follower_min_pct"]) &
                (sector_stocks["change_pct"] < cfg["sector_follower_max_pct"])
            ]

            # 排除已涨停的（涨幅>=9.8%视为已涨停）
            followers = followers[followers["change_pct"] < 9.8]

            # 排除ST
            if "name" in followers.columns:
                followers = followers[~followers["name"].str.contains("ST|退", na=False)]

            if followers.empty:
                continue

            # 按涨幅+量比排序
            followers = followers.sort_values(
                ["change_pct", "vol_ratio"], ascending=[False, False]
            )

            for _, row in followers.head(3).iterrows():  # 每板块最多3只
                code = str(row.get("code", "")).zfill(6)
                if not code or len(code) != 6:
                    continue

                cascade_alerts.append({
                    "code": code,
                    "name": str(row.get("name", "")),
                    "price": round(float(row.get("price", 0)), 2),
                    "change_pct": round(float(row.get("change_pct", 0)), 2),
                    "vol_ratio": round(float(row.get("vol_ratio", 0)), 2),
                    "sector": sector,
                    "sector_zt_count": zt_count,
                    "sector_zt_names": sector_zt_names.get(sector, [])[:3],
                    "alert_type": "sector_cascade",
                })

        # 按板块涨停数降序
        cascade_alerts.sort(key=lambda x: x["sector_zt_count"], reverse=True)
        return cascade_alerts[:cfg["max_alerts"]]

    # ------------------------------------------------------------------
    # V2.6: 放量突破启动检测
    # ------------------------------------------------------------------
    def _detect_breakout(self, df) -> list:
        """
        检测放量突破启动股: 涨幅3-7% + 量比>2 + 成交额达标

        与准涨停检测互补: 准涨停关注>7%的“快涨停”，突破检测关注3-7%的“正在启动”
        """
        if df is None or df.empty:
            return []

        cfg = self.config
        filtered = df.copy()

        # 排除ST/退市/次新
        if "name" in filtered.columns:
            filtered = filtered[~filtered["name"].str.contains("ST|退", na=False)]
            filtered = filtered[~filtered["name"].str.startswith(("N", "C"), na=False)]

        # 涨幅区间: 3-7%
        if "change_pct" in filtered.columns:
            filtered = filtered[
                (filtered["change_pct"] >= cfg["breakout_min_change"]) &
                (filtered["change_pct"] < cfg["breakout_max_change"])
            ]

        # 量比筛选: >=2.0
        if "vol_ratio" in filtered.columns:
            filtered = filtered[filtered["vol_ratio"] >= cfg["breakout_min_vol_ratio"]]

        # 成交额筛选
        if "amount" in filtered.columns:
            filtered = filtered[filtered["amount"] >= cfg["breakout_min_amount"]]

        # 股价区间
        if "price" in filtered.columns:
            filtered = filtered[
                (filtered["price"] >= cfg["min_price"]) &
                (filtered["price"] <= cfg["max_price"])
            ]

        if filtered.empty:
            return []

        # 按量比降序（量比越高=资金关注度越高）
        filtered = filtered.sort_values("vol_ratio", ascending=False)

        results = []
        for _, row in filtered.head(cfg["breakout_max_count"]).iterrows():
            code = str(row.get("code", "")).zfill(6)
            if not code or len(code) != 6:
                continue

            change_pct = float(row.get("change_pct", 0))
            vol_ratio = float(row.get("vol_ratio", 0))
            turnover = float(row.get("turnover", 0))
            amount = float(row.get("amount", 0))
            price = float(row.get("price", 0))

            # 突破强度评分(0-100)
            strength = self._calc_breakout_strength(change_pct, vol_ratio, turnover, amount)

            results.append({
                "code": code,
                "name": str(row.get("name", "")),
                "price": round(price, 3),
                "change_pct": round(change_pct, 2),
                "vol_ratio": round(vol_ratio, 2),
                "turnover": round(turnover, 2),
                "amount_yi": round(amount / 1e8, 2),
                "sector": str(row.get("sector", "")) or self._classify_sector(str(row.get("name", ""))),
                "breakout_strength": strength,
                "alert_type": "breakout",
                "alert_level": "high" if strength >= 70 else "medium",
            })

        return results

    def _calc_breakout_strength(self, change_pct, vol_ratio, turnover, amount) -> int:
        """突破强度评分(0-100)"""
        score = 0
        # 涨幅因子(0-30): 越接近7%越强
        score += min(int((change_pct - 3.0) / 4.0 * 30), 30)
        # 量比因子(0-35): 量比越大资金关注度越高
        if vol_ratio >= 4.0:
            score += 35
        elif vol_ratio >= 3.0:
            score += 28
        elif vol_ratio >= 2.5:
            score += 20
        else:
            score += 12
        # 换手率因子(0-20): 3-10%健康区间
        if 3.0 <= turnover <= 10.0:
            score += 20
        elif 1.0 <= turnover < 3.0:
            score += 10
        elif turnover > 10.0:
            score += 8
        # 成交额因子(0-15)
        if amount >= 10e8:
            score += 15
        elif amount >= 5e8:
            score += 10
        elif amount >= 3e8:
            score += 5
        return min(score, 100)

    # ------------------------------------------------------------------
    # V2.6: 涨停基因联动
    # ------------------------------------------------------------------
    def _detect_zt_gene_linkage(self, market_df) -> list:
        """
        涨停基因联动: 检查昨日涨停股今日是否高开放量（连板启动信号）
        与zt_gene_tracker联动，复用其昨日涨停池数据
        """
        if market_df is None or market_df.empty:
            return []

        try:
            from trading_system.strategy.zt_gene_tracker import ZTGeneTracker
            tracker = ZTGeneTracker()
            # 获取昨日涨停池
            prev_zt = tracker._get_previous_zt_pool()
            if not prev_zt:
                return []

            prev_codes = {s.get("code", "") for s in prev_zt}

            # 在今日行情中查找这些股票
            gene_alerts = []
            for _, row in market_df.iterrows():
                code = str(row.get("code", "")).zfill(6)
                if code not in prev_codes:
                    continue

                change_pct = float(row.get("change_pct", 0))
                vol_ratio = float(row.get("vol_ratio", 0))

                # 连板启动条件: 高开>3% + 量比>2
                min_open = self.config.get("breakout_min_change", 3.0)
                min_vol = self.config.get("breakout_min_vol_ratio", 2.0)

                if change_pct >= min_open and vol_ratio >= min_vol:
                    gene_alerts.append({
                        "code": code,
                        "name": str(row.get("name", "")),
                        "price": round(float(row.get("price", 0)), 3),
                        "change_pct": round(change_pct, 2),
                        "vol_ratio": round(vol_ratio, 2),
                        "sector": str(row.get("sector", "")),
                        "alert_type": "zt_gene",
                        "alert_level": "high" if change_pct >= 5 else "medium",
                        "reason": f"昨日涨停+今日高开{change_pct:.1f}%+量比{vol_ratio:.1f}",
                    })

            # 按涨幅降序
            gene_alerts.sort(key=lambda x: x["change_pct"], reverse=True)
            return gene_alerts[:8]

        except Exception as e:
            logger.warning(f"[盘中预警] 涨停基因联动失败: {e}")
            return []

    # ------------------------------------------------------------------
    # V2.6: 选股引擎盘中增量扫描
    # ------------------------------------------------------------------
    def _run_screener_intraday(self, market_df) -> list:
        """
        选股引擎盘中增量扫描 V3.2: 突破静态池，扫描全市场强势股

        改进:
          - 不再仅限于SECTOR_CANDIDATES内29只，而是扫描全市场
          - 筛选条件: 涨幅>5% + 量比>2 + 成交额>3亿（强势启动特征）
          - 同时保留赛道候选池内的较低阈值扫描（涨幅>2%）
          - 自动按名称关键词分类赛道
        """
        if market_df is None or market_df.empty:
            return []

        try:
            picks = []

            # ---- 通道A: 赛道候选池内强势股（V3.2: 阈值收紧，涨≥3%+量比≥2.0）----
            # V3.2回测诊断: 原阈值(涨≥2%+量比≥1.5)误报率高，收紧后信噪比提升
            sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
            pool_codes = set()
            code_sector_map = {}
            for sector, sector_info in sector_candidates.items():
                stocks = sector_info.get("stocks", {}) if isinstance(sector_info, dict) else {}
                for code in stocks.keys():
                    if code:
                        pool_codes.add(code)
                        code_sector_map[code] = sector

            for _, row in market_df.iterrows():
                code = str(row.get("code", "")).zfill(6)
                if code not in pool_codes:
                    continue
                change_pct = float(row.get("change_pct", 0))
                vol_ratio = float(row.get("vol_ratio", 0))
                amount = float(row.get("amount", 0))
                if change_pct >= 3.0 and vol_ratio >= 2.0 and amount >= 2e8:  # V3.2: 2.0/1.5→3.0/2.0
                    picks.append({
                        "code": code,
                        "name": str(row.get("name", "")),
                        "price": round(float(row.get("price", 0)), 3),
                        "change_pct": round(change_pct, 2),
                        "vol_ratio": round(vol_ratio, 2),
                        "amount_yi": round(amount / 1e8, 2),
                        "sector": code_sector_map.get(code, ""),
                        "alert_type": "screener",
                        "alert_level": "high" if change_pct >= 4 and vol_ratio >= 2.5 else "medium",
                        "reason": f"赛道池+涨{change_pct:.1f}%+量比{vol_ratio:.1f}",
                    })

            # ---- 通道B: 全市场强势启动股（V3.2: 增加追高过滤）----
            # 筛选: 涨幅>5% + 量比>2 + 成交额>3亿 + 非ST + 股价合理
            # V3.2回测诊断: 通道B误报率46%，增加“近5日涨幅<15%”过滤避免追高
            filtered = market_df.copy()
            if "name" in filtered.columns:
                filtered = filtered[~filtered["name"].str.contains("ST|退", na=False)]
                filtered = filtered[~filtered["name"].str.startswith(("N", "C"), na=False)]
            if "change_pct" in filtered.columns:
                filtered = filtered[filtered["change_pct"] >= 5.0]
            if "vol_ratio" in filtered.columns:
                filtered = filtered[filtered["vol_ratio"] >= 2.0]
            if "amount" in filtered.columns:
                filtered = filtered[filtered["amount"] >= 3e8]
            if "price" in filtered.columns:
                filtered = filtered[
                    (filtered["price"] >= self.config.get("min_price", 3)) &
                    (filtered["price"] <= self.config.get("max_price", 300))
                ]
            # V3.2: 排除涨幅>9%的准涨停股（炒板风险高，误报率显著）
            if "change_pct" in filtered.columns:
                filtered = filtered[filtered["change_pct"] <= 9.0]

            # 排除已在通道A中出现的
            existing_codes = {p["code"] for p in picks}
            if not filtered.empty:
                filtered = filtered.sort_values("change_pct", ascending=False)
                for _, row in filtered.head(10).iterrows():
                    code = str(row.get("code", "")).zfill(6)
                    if not code or len(code) != 6 or code in existing_codes:
                        continue
                    change_pct = float(row.get("change_pct", 0))
                    vol_ratio = float(row.get("vol_ratio", 0))
                    amount = float(row.get("amount", 0))
                    name = str(row.get("name", ""))
                    picks.append({
                        "code": code,
                        "name": name,
                        "price": round(float(row.get("price", 0)), 3),
                        "change_pct": round(change_pct, 2),
                        "vol_ratio": round(vol_ratio, 2),
                        "amount_yi": round(amount / 1e8, 2),
                        "sector": self._classify_sector(name),
                        "alert_type": "screener",
                        "alert_level": "high" if change_pct >= 7 else "medium",
                        "reason": f"全市场强势+涨{change_pct:.1f}%+量比{vol_ratio:.1f}",
                    })

            # 按涨幅降序
            picks.sort(key=lambda x: x["change_pct"], reverse=True)
            return picks[:12]

        except Exception as e:
            logger.warning(f"[盘中预警] 选股引擎联动失败: {e}")
            return []

    # ------------------------------------------------------------------
    # V2.6: 首次触发过滤
    # ------------------------------------------------------------------
    def _filter_first_trigger(self, result: dict) -> dict:
        """同一标的当日首次触发才保留，后续轮次不重复推送"""
        for key in ["near_zt_stocks", "breakout_stocks", "sector_cascade",
                    "zt_gene_alerts", "screener_picks"]:
            items = result.get(key, [])
            filtered = []
            for item in items:
                code = item.get("code", "")
                alert_type = item.get("alert_type", key)
                tag = f"{code}_{alert_type}"
                if tag not in self._today_alerted:
                    filtered.append(item)
                    self._today_alerted.add(tag)
            result[key] = filtered
        return result

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _classify_sector(self, name: str) -> str:
        """按名称关键词分类行业"""
        from trading_system.strategy.market_scanner import classify_stock_sector
        return classify_stock_sector(name)

    def _match_sector(self, stock_name: str, sector: str) -> bool:
        """判断股票名称是否匹配指定板块（简化匹配）"""
        from trading_system.strategy.market_scanner import _SECTOR_KEYWORDS
        keywords = _SECTOR_KEYWORDS.get(sector, [])
        return any(kw in stock_name for kw in keywords)

    def _build_summary(self, result: dict) -> str:
        """构建摘要文本"""
        parts = [
            f"时间: {result['scan_time']}",
            f"准涨停: {len(result['near_zt_stocks'])}只",
            f"突破: {len(result.get('breakout_stocks', []))}只",
            f"板块联动: {len(result['sector_cascade'])}条",
            f"基因: {len(result.get('zt_gene_alerts', []))}只",
            f"选股: {len(result.get('screener_picks', []))}只",
            f"涨停池: {result['zt_pool_count']}只",
        ]

        if result["near_zt_stocks"]:
            top3 = result["near_zt_stocks"][:3]
            names = ", ".join(f"{s['name']}(+{s['change_pct']:.1f}%)" for s in top3)
            parts.append(f"强势股: {names}")

        if result.get("breakout_stocks"):
            top2 = result["breakout_stocks"][:2]
            names = ", ".join(f"{s['name']}(+{s['change_pct']:.1f}%)" for s in top2)
            parts.append(f"突破: {names}")

        if result["sector_cascade"]:
            sectors = set(c["sector"] for c in result["sector_cascade"])
            parts.append(f"联动板块: {', '.join(list(sectors)[:3])}")

        return " | ".join(parts)

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------
    def send_alert_email(self, result: dict) -> bool:
        """发送盘中预警邮件"""
        if not result.get("success") or result.get("alert_count", 0) == 0:
            return False

        try:
            from trading_system.notify.email_notify import send_email

            html = self._build_email_html(result)
            subject = (f"[盘中预警] {datetime.date.today()} | "
                       f"准涨停{len(result['near_zt_stocks'])} "
                       f"突破{len(result.get('breakout_stocks', []))} "
                       f"联动{len(result['sector_cascade'])}")

            send_email(subject, html)
            logger.info("[盘中预警] 预警邮件发送成功")
            return True
        except Exception as e:
            logger.warning(f"[盘中预警] 邮件发送失败: {e}")
            return False

    def _build_email_html(self, result: dict) -> str:
        """构建预警邮件HTML V2.6"""
        now_str = result["scan_time"]

        # 准涨停表格
        zt_rows = ""
        for s in result["near_zt_stocks"][:10]:
            level_icon = "[H]" if s["alert_level"] == "high" else "[M]"
            zt_rows += (
                f"<tr>"
                f"<td>{level_icon}</td>"
                f"<td>{s['code']}</td>"
                f"<td>{s['name']}</td>"
                f"<td>{s['price']}</td>"
                f"<td style='color:red'>+{s['change_pct']:.1f}%</td>"
                f"<td>{s['vol_ratio']:.1f}</td>"
                f"<td>{s['amount_yi']:.1f}亿</td>"
                f"<td>{s['sector']}</td>"
                f"<td>{s['seal_probability']:.0%}</td>"
                f"</tr>"
            )

        # V2.6: 放量突破表格
        breakout_rows = ""
        for s in result.get("breakout_stocks", [])[:10]:
            level_icon = "[H]" if s["alert_level"] == "high" else "[M]"
            breakout_rows += (
                f"<tr>"
                f"<td>{level_icon}</td>"
                f"<td>{s['code']}</td>"
                f"<td>{s['name']}</td>"
                f"<td>{s['price']}</td>"
                f"<td style='color:red'>+{s['change_pct']:.1f}%</td>"
                f"<td>{s['vol_ratio']:.1f}</td>"
                f"<td>{s['amount_yi']:.1f}亿</td>"
                f"<td>{s['sector']}</td>"
                f"<td>{s['breakout_strength']}</td>"
                f"</tr>"
            )

        # 板块联动表格
        cascade_rows = ""
        for c in result["sector_cascade"][:10]:
            zt_names = ", ".join(c.get("sector_zt_names", [])[:2])
            cascade_rows += (
                f"<tr>"
                f"<td>{c['sector']}</td>"
                f"<td>{c['sector_zt_count']}只</td>"
                f"<td>{zt_names}</td>"
                f"<td>{c['code']} {c['name']}</td>"
                f"<td style='color:red'>+{c['change_pct']:.1f}%</td>"
                f"<td>{c['vol_ratio']:.1f}</td>"
                f"</tr>"
            )

        # V2.6: 涨停基因联动表格
        gene_rows = ""
        for g in result.get("zt_gene_alerts", [])[:8]:
            gene_rows += (
                f"<tr>"
                f"<td>{g['code']}</td>"
                f"<td>{g['name']}</td>"
                f"<td>{g['price']}</td>"
                f"<td style='color:red'>+{g['change_pct']:.1f}%</td>"
                f"<td>{g['vol_ratio']:.1f}</td>"
                f"<td>{g.get('reason', '')}</td>"
                f"</tr>"
            )

        # V2.6: 选股引擎盘中推荐表格
        screener_rows = ""
        for p in result.get("screener_picks", [])[:6]:
            screener_rows += (
                f"<tr>"
                f"<td>{p['code']}</td>"
                f"<td>{p['name']}</td>"
                f"<td>{p['price']}</td>"
                f"<td style='color:red'>+{p['change_pct']:.1f}%</td>"
                f"<td>{p['vol_ratio']:.1f}</td>"
                f"<td>{p['sector']}</td>"
                f"<td>{p.get('reason', '')}</td>"
                f"</tr>"
            )

        html = f"""
        <h2>[ALERT] 盘中异动预警 V2.6</h2>
        <p>扫描时间: {now_str} | 涨停池: {result['zt_pool_count']}只 | 总预警: {result['alert_count']}条</p>

        <h3>一、准涨停股（涨幅≥{self.config['min_change_pct']:.0f}%）</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse">
          <tr><th>级别</th><th>代码</th><th>名称</th><th>现价</th><th>涨幅</th>
              <th>量比</th><th>成交额</th><th>板块</th><th>封板概率</th></tr>
          {zt_rows if zt_rows else '<tr><td colspan="9">无</td></tr>'}
        </table>

        <h3>二、放量突破启动（3-7%+量比>2）</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse">
          <tr><th>级别</th><th>代码</th><th>名称</th><th>现价</th><th>涨幅</th>
              <th>量比</th><th>成交额</th><th>板块</th><th>强度</th></tr>
          {breakout_rows if breakout_rows else '<tr><td colspan="9">无</td></tr>'}
        </table>

        <h3>三、板块联动预警（≥{self.config['sector_cascade_threshold']}只涨停触发）</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse">
          <tr><th>板块</th><th>涨停数</th><th>已涨停</th><th>跟涨候选</th><th>涨幅</th><th>量比</th></tr>
          {cascade_rows if cascade_rows else '<tr><td colspan="6">无</td></tr>'}
        </table>

        <h3>四、涨停基因联动（昨日涨停+今日高开放量）</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse">
          <tr><th>代码</th><th>名称</th><th>现价</th><th>涨幅</th><th>量比</th><th>触发原因</th></tr>
          {gene_rows if gene_rows else '<tr><td colspan="6">无</td></tr>'}
        </table>

        <h3>五、选股引擎盘中推荐（赛道候选池强势股）</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse:collapse">
          <tr><th>代码</th><th>名称</th><th>现价</th><th>涨幅</th><th>量比</th><th>赛道</th><th>触发原因</th></tr>
          {screener_rows if screener_rows else '<tr><td colspan="7">无</td></tr>'}
        </table>

        <p style="color:gray;font-size:12px">
          注: 封板概率/突破强度为量化估算，仅供参考。同一标的当日仅首次触发推送。
        </p>
        """
        return html

    def print_console_report(self, result: dict):
        """控制台输出预警报告"""
        if not result.get("success"):
            print("  [盘中预警] 扫描未成功")
            return

        print(f"\n  [盘中异动预警 V2.6] ({result['scan_time']})")
        print(f"     涨停池: {result['zt_pool_count']}只 | "
              f"准涨停: {len(result['near_zt_stocks'])}只 | "
              f"突破: {len(result.get('breakout_stocks', []))}只 | "
              f"联动: {len(result['sector_cascade'])}条 | "
              f"基因: {len(result.get('zt_gene_alerts', []))}只 | "
              f"选股: {len(result.get('screener_picks', []))}只")

        if result["near_zt_stocks"]:
            print(f"\n     -- 准涨停股（涨幅>={self.config['min_change_pct']:.0f}%）--")
            for s in result["near_zt_stocks"][:8]:
                icon = "[H]" if s["alert_level"] == "high" else "[M]"
                print(f"     {icon} {s['code']} {s['name']} | "
                      f"+{s['change_pct']:.1f}% | 量比{s['vol_ratio']:.1f} | "
                      f"{s['amount_yi']:.1f}亿 | {s['sector']} | "
                      f"封板概率{s['seal_probability']:.0%}")

        if result.get("breakout_stocks"):
            print(f"\n     -- 放量突破启动(3-7%+量比>2) --")
            for s in result["breakout_stocks"][:6]:
                icon = "[H]" if s["alert_level"] == "high" else "[M]"
                print(f"     {icon} {s['code']} {s['name']} | "
                      f"+{s['change_pct']:.1f}% | 量比{s['vol_ratio']:.1f} | "
                      f"{s['amount_yi']:.1f}亿 | {s['sector']} | "
                      f"强度{s['breakout_strength']}")

        if result["sector_cascade"]:
            print(f"\n     -- 板块联动预警 --")
            for c in result["sector_cascade"][:5]:
                zt_names = ", ".join(c.get("sector_zt_names", [])[:2])
                print(f"     [联动] {c['sector']}(已{c['sector_zt_count']}只涨停: {zt_names})")
                print(f"        -> 跟涨: {c['code']} {c['name']} +{c['change_pct']:.1f}% 量比{c['vol_ratio']:.1f}")

        if result.get("zt_gene_alerts"):
            print(f"\n     -- 涨停基因联动 --")
            for g in result["zt_gene_alerts"][:5]:
                print(f"     [GENE] {g['code']} {g['name']} | "
                      f"+{g['change_pct']:.1f}% | 量比{g['vol_ratio']:.1f} | {g.get('reason', '')}")

        if result.get("screener_picks"):
            print(f"\n     -- 选股引擎盘中推荐 --")
            for p in result["screener_picks"][:5]:
                print(f"     [PICK] {p['code']} {p['name']} | "
                      f"+{p['change_pct']:.1f}% | 量比{p['vol_ratio']:.1f} | "
                      f"{p['sector']} | {p.get('reason', '')}")


# ============================================================
# 便捷函数（供scheduler/caopan_report调用）
# ============================================================

def run_intraday_alert(send_email: bool = True) -> dict:
    """
    执行盘中异动扫描（便捷入口）

    参数:
        send_email: 是否发送预警邮件（有预警时才发）

    返回:
        扫描结果dict
    """
    alert = IntradayAlert()
    result = alert.run_scan()

    if result.get("success"):
        alert.print_console_report(result)
        if send_email and result.get("alert_count", 0) > 0:
            alert.send_alert_email(result)

    return result


# ============================================================
# 命令行测试入口
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("=" * 50)
    print("  盘中实时异动监测 - 测试")
    print("=" * 50)

    result = run_intraday_alert(send_email=False)

    if result.get("success"):
        print(f"\n[OK] 扫描成功")
        print(f"   摘要: {result['summary']}")
    else:
        print(f"\n[FAIL] 扫描失败（可能是非交易时间或网络问题）")
