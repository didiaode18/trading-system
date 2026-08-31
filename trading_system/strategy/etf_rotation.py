"""
ETF行业轮动监控（V10.1 Top2 新增）
=================================
跟踪申万一级行业ETF的5日/20日动量，输出行业轮动排名。
与现有 filter_strong_sectors() 互补：
  - filter_strong_sectors: 基于个股数据，计算板块内个股涨幅/MA20占比
  - etf_rotation: 基于ETF价格数据，独立验证板块轮动方向

核心逻辑:
  1. 维护行业ETF映射表（代码→行业名）
  2. 获取各ETF近60日价格数据
  3. 计算5日/20日动量 + 加速度（5日涨幅 vs 20日涨幅/4）
  4. 综合排名，输出最强/最弱行业

使用方式:
    from strategy.etf_rotation import ETFRotationMonitor
    monitor = ETFRotationMonitor()
    result = monitor.run()
    # result = {
    #   "rankings": [{"sector": "半导体", "etf_code": "512480", "chg_5d": 2.3, "chg_20d": -1.5, "momentum": 3.8, "accel": 3.3}, ...],
    #   "strong": ["半导体", "新能源"],
    #   "weak": ["房地产", "银行"],
    #   "available": True
    # }

数据依赖:
  - data_loader.fetch_stock_daily() 获取ETF日线数据
  - ETF代码通过 baostock 获取（sh.512480 等格式）
"""

import os
import sys
import json
import logging
import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# ============================================================
# 行业ETF映射表（申万一级行业 → 代表性ETF）
# ============================================================
# 选取原则: 规模大、流动性好、跟踪误差小的行业ETF
SECTOR_ETF_MAP = {
    # 科技
    "半导体":    {"code": "512480", "exchange": "sh"},
    "电子":      {"code": "512720", "exchange": "sh"},
    "计算机":    {"code": "512760", "exchange": "sh"},
    "通信":      {"code": "515880", "exchange": "sh"},
    "传媒":      {"code": "512980", "exchange": "sh"},
    # 消费
    "食品饮料":  {"code": "515170", "exchange": "sh"},
    "医药生物":  {"code": "512010", "exchange": "sh"},
    "家电":      {"code": "159996", "exchange": "sz"},
    # 金融
    "银行":      {"code": "512800", "exchange": "sh"},
    "非银金融":  {"code": "512070", "exchange": "sh"},
    # 周期
    "有色金属":  {"code": "512400", "exchange": "sh"},
    "钢铁":      {"code": "515210", "exchange": "sh"},
    "煤炭":      {"code": "515220", "exchange": "sh"},
    "化工":      {"code": "516020", "exchange": "sh"},
    "建筑材料":  {"code": "516820", "exchange": "sh"},
    # 制造
    "新能源":    {"code": "516160", "exchange": "sh"},
    "汽车":      {"code": "516110", "exchange": "sh"},
    "国防军工":  {"code": "512660", "exchange": "sh"},
    "机械设备":  {"code": "516950", "exchange": "sh"},
    # 其他
    "房地产":    {"code": "512200", "exchange": "sh"},
    "交通运输":  {"code": "516530", "exchange": "sh"},
    "公用事业":  {"code": "159839", "exchange": "sz"},
    "农林牧渔":  {"code": "159825", "exchange": "sz"},
}

# 缓存文件
CACHE_FILE = os.path.join(config.DATA_DIR, "etf_rotation_cache.json")
CACHE_TTL_HOURS = 12  # 缓存有效期12小时


class ETFRotationMonitor:
    """ETF行业轮动监控器"""

    def __init__(self):
        self._data_loader = None

    def _get_loader(self):
        """惰性加载 data_loader"""
        if self._data_loader is None:
            try:
                from data.data_loader import fetch_stock_daily
                self._data_loader = fetch_stock_daily
            except ImportError:
                logger.warning("[ETF轮动] data_loader 导入失败")
        return self._data_loader

    def _load_cache(self) -> dict:
        """加载缓存"""
        if not os.path.exists(CACHE_FILE):
            return {}
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            # 检查缓存时效
            cache_time = cache.get("update_time", "")
            if cache_time:
                cache_dt = datetime.datetime.strptime(cache_time, "%Y-%m-%d %H:%M:%S")
                if (datetime.datetime.now() - cache_dt).total_seconds() < CACHE_TTL_HOURS * 3600:
                    return cache
        except Exception as e:
            logger.debug(f"[ETF轮动] 缓存加载失败: {e}")
        return {}

    def _save_cache(self, result: dict):
        """保存缓存"""
        try:
            result["update_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[ETF轮动] 缓存保存失败: {e}")

    def _fetch_etf_data(self, etf_code: str, exchange: str) -> pd.DataFrame:
        """获取ETF日线数据"""
        loader = self._get_loader()
        if loader is None:
            return pd.DataFrame()

        full_code = f"{exchange}.{etf_code}"
        end_date = datetime.datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.datetime.now() - datetime.timedelta(days=120)).strftime("%Y-%m-%d")

        try:
            df = loader(etf_code, start_date=start_date, end_date=end_date)
            if df is not None and len(df) >= 20:
                return df
        except Exception as e:
            logger.debug(f"[ETF轮动] {etf_code} 数据获取失败: {e}")
        return pd.DataFrame()

    def _calc_momentum(self, df: pd.DataFrame) -> dict:
        """计算动量指标"""
        if len(df) < 20:
            return {}

        close = df["close"].values
        latest = close[-1]

        # 5日涨幅
        if len(close) >= 6:
            chg_5d = (latest / close[-6] - 1) * 100
        else:
            chg_5d = 0.0

        # 20日涨幅
        chg_20d = (latest / close[-21] - 1) * 100 if len(close) >= 21 else 0.0

        # 加速度: 5日涨幅 vs 20日涨幅/4（线性外推）
        accel = chg_5d - chg_20d / 4

        # 综合动量分: 5日涨幅×0.6 + 20日涨幅×0.3 + 加速度×0.1
        momentum = chg_5d * 0.6 + chg_20d * 0.3 + accel * 0.1

        return {
            "chg_5d": round(chg_5d, 2),
            "chg_20d": round(chg_20d, 2),
            "accel": round(accel, 2),
            "momentum": round(momentum, 2),
        }

    def run(self, use_cache: bool = True) -> dict:
        """
        执行ETF行业轮动分析

        参数:
            use_cache: 是否使用缓存（默认True，12小时内复用）

        返回:
            {
                "rankings": [{"sector": str, "etf_code": str, "chg_5d": float, ...}, ...],
                "strong": [str],    # 最强3个行业
                "weak": [str],      # 最弱3个行业
                "available": bool,
                "update_time": str
            }
        """
        # 检查缓存
        if use_cache:
            cache = self._load_cache()
            if cache.get("available", False):
                logger.info("[ETF轮动] 使用缓存数据")
                return cache

        logger.info("[ETF轮动] 开始扫描行业ETF动量...")
        rankings = []
        failed = 0

        for sector, info in SECTOR_ETF_MAP.items():
            etf_code = info["code"]
            exchange = info["exchange"]

            df = self._fetch_etf_data(etf_code, exchange)
            if df.empty:
                failed += 1
                continue

            momentum = self._calc_momentum(df)
            if not momentum:
                failed += 1
                continue

            rankings.append({
                "sector": sector,
                "etf_code": etf_code,
                **momentum,
            })

        # 按动量排名
        rankings.sort(key=lambda x: x["momentum"], reverse=True)

        # 最强/最弱行业
        strong = [r["sector"] for r in rankings[:3]] if len(rankings) >= 3 else []
        weak = [r["sector"] for r in rankings[-3:]] if len(rankings) >= 3 else []
        weak.reverse()  # 最弱的排前面

        result = {
            "rankings": rankings,
            "strong": strong,
            "weak": weak,
            "available": len(rankings) >= 5,  # 至少5个行业有数据
            "total_etfs": len(SECTOR_ETF_MAP),
            "success_count": len(rankings),
            "failed_count": failed,
        }

        # 保存缓存
        if result["available"]:
            self._save_cache(result)

        logger.info(f"[ETF轮动] 扫描完成: {len(rankings)}/{len(SECTOR_ETF_MAP)}个行业有数据")
        if strong:
            logger.info(f"[ETF轮动] 最强行业: {strong}")
        if weak:
            logger.info(f"[ETF轮动] 最弱行业: {weak}")

        return result

    def get_rotation_signal(self, sector_name: str) -> str:
        """
        获取特定行业的轮动信号

        参数:
            sector_name: 行业名称

        返回:
            "strong_up" / "up" / "neutral" / "down" / "weak_down" / "unknown"
        """
        result = self.run()
        if not result.get("available"):
            return "unknown"

        rankings = result.get("rankings", [])
        total = len(rankings)
        if total == 0:
            return "unknown"

        # 找到目标行业排名位置
        for i, r in enumerate(rankings):
            if r["sector"] == sector_name:
                rank_pct = (i + 1) / total
                if rank_pct <= 0.15:
                    return "strong_up"
                elif rank_pct <= 0.35:
                    return "up"
                elif rank_pct >= 0.85:
                    return "weak_down"
                elif rank_pct >= 0.65:
                    return "down"
                else:
                    return "neutral"

        return "unknown"


# ============================================================
# 便捷函数（供外部直接调用）
# ============================================================

def run_etf_rotation(use_cache: bool = True) -> dict:
    """执行ETF行业轮动分析（便捷入口）"""
    monitor = ETFRotationMonitor()
    return monitor.run(use_cache=use_cache)


def get_etf_rotation_summary() -> str:
    """获取ETF轮动摘要文本（供报告嵌入）"""
    result = run_etf_rotation()
    if not result.get("available"):
        return "ETF轮动数据暂不可用"

    strong = result.get("strong", [])
    weak = result.get("weak", [])
    top3 = result.get("rankings", [])[:3]
    bot3 = result.get("rankings", [])[-3:]

    lines = []
    if strong:
        lines.append(f"强势行业: {', '.join(strong)}")
    if weak:
        lines.append(f"弱势行业: {', '.join(weak)}")
    if top3:
        top_detail = " | ".join(f"{r['sector']}({r['momentum']:+.1f})" for r in top3)
        lines.append(f"动量Top3: {top_detail}")

    return "; ".join(lines) if lines else "ETF轮动暂无显著信号"
