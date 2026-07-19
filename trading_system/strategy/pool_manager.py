"""
股票池管理模块 V3.0
====================
管理核心操作池和观察池的动态调整

核心规则:
- 核心操作池: 固定5-7只，覆盖2-3条主线，每周日更新
- 观察池: 最多10只，等待回调到买点后再调入核心池
- 观察期: 不少于3个交易日
- 盘中绝不新增任何标的
- 核心池股票跌破MA60自动降级

使用方式:
    from strategy.pool_manager import PoolManager
    pm = PoolManager()
    pm.update_pool_weekly(data_dict, screener_result)
"""

import os
import sys
import json
import logging
import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 股票池持久化文件
POOL_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stock_pool.json")


class PoolManager:
    """股票池管理器"""

    def __init__(self):
        self.core_pool = {}      # 核心操作池 {code: {info}}
        self.watch_pool = {}     # 观察池 {code: {info, "observe_start": date}}
        self.blacklist = set()   # 黑名单
        self._load()

    def _load(self):
        """从文件加载股票池状态"""
        if os.path.exists(POOL_FILE):
            try:
                with open(POOL_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.core_pool = data.get("core_pool", {})
                self.watch_pool = data.get("watch_pool", {})
                self.blacklist = set(data.get("blacklist", []))
                logger.info(f"[股票池] 加载: 核心{len(self.core_pool)}只, 观察{len(self.watch_pool)}只")
            except Exception as e:
                logger.error(f"[股票池] 加载失败: {e}")
        else:
            # 首次运行，从config.STOCK_POOL初始化核心池
            for code, info in config.STOCK_POOL.items():
                self.core_pool[code] = {
                    "名称": info.get("名称", code),
                    "赛道": info.get("赛道", ""),
                    "类型": info.get("类型", "龙头"),
                    "added_date": datetime.date.today().strftime("%Y-%m-%d"),
                }
            logger.info(f"[股票池] 首次初始化: 核心{len(self.core_pool)}只")

    def _save(self):
        """持久化股票池状态"""
        data = {
            "core_pool": self.core_pool,
            "watch_pool": self.watch_pool,
            "blacklist": list(self.blacklist),
            "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        try:
            with open(POOL_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[股票池] 保存失败: {e}")

    def update_pool_weekly(self, data_dict: dict, screener_result: dict = None):
        """
        每周日更新股票池
        
        规则:
        1. 核心池股票跌破MA60 → 降级到观察池
        2. 观察池股票观察期满+触发买点 → 调入核心池
        3. 核心池上限7只，观察池上限10只
        4. 根据最新CANSLIM评分排序
        """
        today = datetime.date.today()
        logger.info(f"[股票池] 周度更新: {today}")

        core_max = getattr(config, 'CORE_POOL_MAX', 7)
        watch_max = getattr(config, 'WATCH_POOL_MAX', 10)
        obs_days = getattr(config, 'OBSERVATION_DAYS', 3)

        # ---- 1. 核心池降级检查 ----
        demoted = []
        for code in list(self.core_pool.keys()):
            if code in data_dict:
                df = data_dict[code]
                if len(df) >= 60 and "ma60" in df.columns:
                    latest = df.iloc[-1]
                    ma60 = latest.get("ma60", None)
                    if not pd.isna(ma60) and latest["close"] < ma60:
                        # 跌破MA60 → 降级
                        info = self.core_pool.pop(code)
                        info["observe_start"] = today.strftime("%Y-%m-%d")
                        info["demote_reason"] = "跌破MA60"
                        self.watch_pool[code] = info
                        demoted.append(code)
                        logger.info(f"  降级: {code} {info.get('名称','')} → 观察池 (跌破MA60)")

        # ---- 2. 观察池升级检查 ----
        promoted = []
        for code in list(self.watch_pool.keys()):
            info = self.watch_pool[code]
            observe_start = info.get("observe_start", today.strftime("%Y-%m-%d"))
            try:
                start_date = datetime.datetime.strptime(observe_start, "%Y-%m-%d").date()
            except:
                start_date = today
            days_observed = (today - start_date).days

            if days_observed < obs_days:
                continue  # 观察期未满

            # 检查是否触发买点（趋势恢复）
            if code in data_dict:
                df = data_dict[code]
                if len(df) >= 20 and "ma20" in df.columns:
                    latest = df.iloc[-1]
                    ma20 = latest.get("ma20", None)
                    ma20_slope = df["ma20"].diff(3).iloc[-1] if len(df) >= 23 else 0
                    if not pd.isna(ma20) and latest["close"] > ma20 and ma20_slope > 0:
                        # 趋势恢复 + 观察期满 → 可升级
                        if len(self.core_pool) < core_max:
                            self.watch_pool.pop(code)
                            info["added_date"] = today.strftime("%Y-%m-%d")
                            info.pop("observe_start", None)
                            info.pop("demote_reason", None)
                            self.core_pool[code] = info
                            promoted.append(code)
                            logger.info(f"  升级: {code} {info.get('名称','')} → 核心池 (观察{days_observed}天)")

        # ---- 3. 从选股结果补充观察池 ----
        if screener_result and screener_result.get("stock_pool"):
            for stock in screener_result["stock_pool"]:
                code = stock["code"]
                if code not in self.core_pool and code not in self.watch_pool:
                    if code not in self.blacklist and len(self.watch_pool) < watch_max:
                        self.watch_pool[code] = {
                            "名称": stock.get("name", code),
                            "赛道": stock.get("sector", ""),
                            "类型": stock.get("type", "龙头"),
                            "observe_start": today.strftime("%Y-%m-%d"),
                            "score": stock.get("factor_score", 0),
                        }
                        logger.info(f"  新增观察: {code} {stock.get('name','')} (评分{stock.get('factor_score',0)})")

        # ---- 4. 观察池清理（超过20天未升级的移除）----
        expired = []
        for code in list(self.watch_pool.keys()):
            info = self.watch_pool[code]
            observe_start = info.get("observe_start", today.strftime("%Y-%m-%d"))
            try:
                start_date = datetime.datetime.strptime(observe_start, "%Y-%m-%d").date()
            except:
                start_date = today
            if (today - start_date).days > 20:
                self.watch_pool.pop(code)
                expired.append(code)
                logger.info(f"  移除观察: {code} (超过20天未升级)")

        self._save()
        logger.info(f"[股票池] 更新完成: 核心{len(self.core_pool)}只, 观察{len(self.watch_pool)}只 | "
                    f"降级{len(demoted)}, 升级{len(promoted)}, 过期{len(expired)}")

        return {
            "core_pool": self.core_pool,
            "watch_pool": self.watch_pool,
            "demoted": demoted,
            "promoted": promoted,
            "expired": expired,
        }

    def check_observation_period(self, code: str) -> dict:
        """检查观察池股票的观察期状态"""
        if code not in self.watch_pool:
            return {"in_watch": False, "days": 0, "ready": False}

        info = self.watch_pool[code]
        observe_start = info.get("observe_start", datetime.date.today().strftime("%Y-%m-%d"))
        try:
            start_date = datetime.datetime.strptime(observe_start, "%Y-%m-%d").date()
        except:
            start_date = datetime.date.today()

        days = (datetime.date.today() - start_date).days
        obs_days = getattr(config, 'OBSERVATION_DAYS', 3)

        return {
            "in_watch": True,
            "days": days,
            "ready": days >= obs_days,
            "info": info,
        }

    def promote_to_core(self, code: str) -> bool:
        """手动将观察池股票调入核心池"""
        core_max = getattr(config, 'CORE_POOL_MAX', 7)
        if len(self.core_pool) >= core_max:
            logger.warning(f"[股票池] 核心池已满({core_max}只)，无法调入{code}")
            return False
        if code not in self.watch_pool:
            return False

        info = self.watch_pool.pop(code)
        info["added_date"] = datetime.date.today().strftime("%Y-%m-%d")
        info.pop("observe_start", None)
        self.core_pool[code] = info
        self._save()
        logger.info(f"[股票池] 手动升级: {code} → 核心池")
        return True

    def demote_from_core(self, code: str, reason: str = "手动降级") -> bool:
        """将核心池股票降级到观察池"""
        if code not in self.core_pool:
            return False

        info = self.core_pool.pop(code)
        info["observe_start"] = datetime.date.today().strftime("%Y-%m-%d")
        info["demote_reason"] = reason
        self.watch_pool[code] = info
        self._save()
        logger.info(f"[股票池] 降级: {code} → 观察池 ({reason})")
        return True

    def add_to_blacklist(self, code: str, reason: str = ""):
        """加入黑名单"""
        self.blacklist.add(code)
        # 同时从核心池和观察池移除
        self.core_pool.pop(code, None)
        self.watch_pool.pop(code, None)
        self._save()
        logger.info(f"[股票池] 黑名单: {code} ({reason})")

    def get_core_codes(self) -> list:
        """获取核心池股票代码列表"""
        return list(self.core_pool.keys())

    def get_watch_codes(self) -> list:
        """获取观察池股票代码列表"""
        return list(self.watch_pool.keys())

    def get_pool_summary(self) -> dict:
        """获取股票池摘要"""
        return {
            "core_count": len(self.core_pool),
            "watch_count": len(self.watch_pool),
            "blacklist_count": len(self.blacklist),
            "core_stocks": {code: info.get("名称", code) for code, info in self.core_pool.items()},
            "watch_stocks": {code: info.get("名称", code) for code, info in self.watch_pool.items()},
            "core_max": getattr(config, 'CORE_POOL_MAX', 7),
            "watch_max": getattr(config, 'WATCH_POOL_MAX', 10),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=" * 50)
    print("  股票池管理模块 V3.0 - 测试")
    print("=" * 50)

    pm = PoolManager()
    summary = pm.get_pool_summary()
    print(f"\n核心池: {summary['core_count']}/{summary['core_max']}只")
    for code, name in summary["core_stocks"].items():
        print(f"  {code} {name}")
    print(f"\n观察池: {summary['watch_count']}/{summary['watch_max']}只")
    for code, name in summary["watch_stocks"].items():
        print(f"  {code} {name}")
    print("\n[OK] 股票池管理模块测试通过")
