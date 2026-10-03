# -*- coding: utf-8 -*-
"""
市场情绪逆向指标模块 V10.1
============================
利用市场情绪极端值作为诱多/诱空的辅助判断

核心指标:
  1. 涨停板数量动量: 今日涨停数 vs 5日均值 → 骤增后骤减=情绪拐点
  2. 板块轮动速度: 近5日TOP3板块变化率 → 全部更换=无主线(诱多环境)
  3. 涨跌停比: 涨停数/(涨停数+跌停数) → >90%后突然<60%=情绪退潮
  4. 融资余额拐点(P2): 融资余额周变化率 → 连续增加后首周减少=杠杆撤退

设计原则:
  - 所有数据从akshare获取，获取失败时降级为中性(50分)
  - 逆向逻辑: 极端乐观→看空, 极端悲观→看多
  - 输出统一格式供consensus.py消费

使用:
    from strategy.market_sentiment import MarketSentiment
    ms = MarketSentiment()
    result = ms.analyze()
    # result = {"sentiment_score": 0-100, "components": {...}, "extreme": str}
"""

import logging
import os
import json
import datetime

import numpy as np

logger = logging.getLogger(__name__)

# 情绪缓存文件(每日更新一次即可)
_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "market_sentiment_cache.json"
)


class MarketSentiment:
    """
    市场情绪逆向指标分析器

    职责:
      通过换手率、融资余额、波动率等指标量化市场情绪，
      识别情绪极值点（过热/过冷），提供逆向操作信号。
    """

    def __init__(self):
        self._cache = None

    def analyze(self) -> dict:
        """综合分析市场情绪

        返回:
            {
                "sentiment_score": 0-100,  # >65偏乐观, <35偏悲观, >80极端乐观, <20极端悲观
                "components": {
                    "limit_up_momentum": {...},
                    "sector_rotation_speed": {...},
                    "limit_ratio": {...},
                    "margin_turning": {...},
                },
                "extreme": str,  # "极端乐观"/"极端悲观"/"中性"
                "detail": str,
            }
        """
        result = {
            "sentiment_score": 50,
            "components": {},
            "extreme": "中性",
            "detail": "",
        }

        # I2修复: 加载缓存一次，存入 self._cache 供子方法复用，避免多磁盘IO
        self._cache = self._load_cache()
        if self._cache and self._cache.get("date") == datetime.date.today().isoformat():
            return self._cache.get("result", result)

        details = []
        scores = []

        # 1. 涨停板数量动量
        limit_up_result = self._calc_limit_up_momentum()
        result["components"]["limit_up_momentum"] = limit_up_result
        if limit_up_result.get("score") is not None:
            scores.append(limit_up_result["score"])
            details.append(limit_up_result.get("detail", ""))

        # 2. 板块轮动速度
        rotation_result = self._calc_sector_rotation_speed()
        result["components"]["sector_rotation_speed"] = rotation_result
        if rotation_result.get("score") is not None:
            scores.append(rotation_result["score"])
            details.append(rotation_result.get("detail", ""))

        # 3. 涨跌停比
        ratio_result = self._calc_limit_ratio()
        result["components"]["limit_ratio"] = ratio_result
        if ratio_result.get("score") is not None:
            scores.append(ratio_result["score"])
            details.append(ratio_result.get("detail", ""))

        # 4. 融资余额拐点(P2)
        margin_result = self._calc_margin_turning()
        result["components"]["margin_turning"] = margin_result
        if margin_result.get("score") is not None:
            scores.append(margin_result["score"])
            details.append(margin_result.get("detail", ""))

        # 综合评分(等权平均后映射到0-100)
        if scores:
            avg_score = np.mean(scores)  # -1到+1
            # 映射到0-100: 0=极端悲观, 50=中性, 100=极端乐观
            sentiment_score = int(50 + avg_score * 50)
            sentiment_score = max(0, min(100, sentiment_score))
        else:
            sentiment_score = 50  # 无数据时中性

        result["sentiment_score"] = sentiment_score

        # 极端值判定
        if sentiment_score >= 80:
            result["extreme"] = "极端乐观"
        elif sentiment_score <= 20:
            result["extreme"] = "极端悲观"
        elif sentiment_score >= 65:
            result["extreme"] = "偏乐观"
        elif sentiment_score <= 35:
            result["extreme"] = "偏悲观"

        result["detail"] = " | ".join([d for d in details if d])

        # 写入缓存
        self._save_cache(result)

        return result

    # ============================================================
    # 1. 涨停板数量动量
    # ============================================================
    def _calc_limit_up_momentum(self) -> dict:
        """涨停板数量动量: 今日涨停数 vs 5日均值

        骤增(>2倍)后骤减(<0.5倍): 情绪拐点
        """
        try:
            import akshare as ak
            today = datetime.date.today().isoformat().replace("-", "")

            # 获取涨停股数据
            df = ak.stock_zt_pool_em(date=today)
            today_count = len(df) if df is not None else 0

            # 获取近5日涨停数(简化: 用缓存历史)
            # I2修复: 复用 self._cache 而非重新加载磁盘
            cache = self._cache or {}
            history = cache.get("limit_up_history", [])
            history.append(today_count)
            history = history[-5:]

            if len(history) < 3:
                return {"score": None, "detail": "涨停数据积累不足"}

            avg_5d = np.mean(history[:-1]) if len(history) > 1 else today_count
            if avg_5d <= 0:
                return {"score": 0, "detail": f"今日涨停{today_count}只"}

            ratio = today_count / avg_5d

            # 逆向评分: 涨停数骤增→悲观(过热), 骤减→乐观(恐慌释放)
            if ratio > 2.0:
                score = -0.6  # 过热→逆向看空
                detail = f"涨停{today_count}只(5日均值{avg_5d:.0f}的{ratio:.1f}倍), 情绪过热"
            elif ratio > 1.5:
                score = -0.3
                detail = f"涨停{today_count}只(偏多), 市场偏热"
            elif ratio < 0.3:
                score = 0.6  # 恐慌释放→逆向看多
                detail = f"涨停仅{today_count}只(5日均值{avg_5d:.0f}的{ratio:.1f}倍), 恐慌释放"
            elif ratio < 0.5:
                score = 0.3
                detail = f"涨停{today_count}只(偏少), 市场偏冷"
            else:
                score = 0
                detail = f"涨停{today_count}只(正常)"

            # 更新缓存历史
            self._update_cache_field("limit_up_history", history)

            return {"score": score, "detail": detail, "today_count": today_count, "ratio": round(ratio, 2)}

        except Exception as e:
            logger.debug(f"涨停板数据获取失败: {e}")
            return {"score": None, "detail": f"涨停数据获取失败: {e}"}

    # ============================================================
    # 2. 板块轮动速度
    # ============================================================
    def _calc_sector_rotation_speed(self) -> dict:
        """板块轮动速度: 近5日TOP3板块变化率

        全部更换=无持续主线(诱多环境)
        稳定+新板块加入=主线明确(健康)
        """
        try:
            import akshare as ak

            # 获取板块涨幅排名
            df = ak.stock_board_industry_name_em()
            if df is None or df.empty:
                return {"score": None, "detail": "板块数据获取失败"}

            # 取涨幅TOP3板块名称
            top3 = set(df.head(3)["板块名称"].tolist()) if "板块名称" in df.columns else set()

            # I2修复: 复用 self._cache 而非重新加载磁盘
            cache = self._cache or {}
            prev_top3 = set(cache.get("prev_top3_sectors", []))

            if not prev_top3 or not top3:
                self._update_cache_field("prev_top3_sectors", list(top3))
                return {"score": None, "detail": "板块数据积累不足"}

            # 计算变化率: Jaccard距离
            union = prev_top3 | top3
            intersection = prev_top3 & top3
            if union:
                change_rate = 1 - len(intersection) / len(union)
            else:
                change_rate = 0

            # 逆向评分: 轮动过快→悲观(无主线), 稳定→乐观
            if change_rate > 0.8:
                score = -0.5  # TOP3全部更换
                detail = f"板块轮动极快(变化率{change_rate:.0%}), 无持续主线, 诱多环境"
            elif change_rate > 0.5:
                score = -0.2
                detail = f"板块轮动偏快(变化率{change_rate:.0%}), 主线不明确"
            elif change_rate < 0.2:
                score = 0.3
                detail = f"板块轮动稳定(变化率{change_rate:.0%}), 主线明确"
            else:
                score = 0
                detail = f"板块轮动正常(变化率{change_rate:.0%})"

            self._update_cache_field("prev_top3_sectors", list(top3))

            return {"score": score, "detail": detail, "change_rate": round(change_rate, 2),
                    "top3": list(top3)}

        except Exception as e:
            logger.debug(f"板块数据获取失败: {e}")
            return {"score": None, "detail": f"板块数据获取失败: {e}"}

    # ============================================================
    # 3. 涨跌停比
    # ============================================================
    def _calc_limit_ratio(self) -> dict:
        """涨跌停比: 涨停数/(涨停数+跌停数)

        >90%后突然<60%: 情绪退潮
        """
        try:
            import akshare as ak
            today = datetime.date.today().isoformat().replace("-", "")

            # 涨停数
            zt_df = ak.stock_zt_pool_em(date=today)
            zt_count = len(zt_df) if zt_df is not None else 0

            # 跌停数
            dt_df = ak.stock_dt_pool_em(date=today)
            dt_count = len(dt_df) if dt_df is not None else 0

            total = zt_count + dt_count
            if total == 0:
                return {"score": None, "detail": "无涨跌停数据"}

            ratio = zt_count / total

            # 逆向评分
            if ratio > 0.9:
                score = -0.5  # 极端乐观
                detail = f"涨跌停比{ratio:.0%}(涨停{zt_count}/跌停{dt_count}), 极端乐观"
            elif ratio > 0.7:
                score = -0.2
                detail = f"涨跌停比{ratio:.0%}, 市场偏热"
            elif ratio < 0.1:
                score = 0.5  # 极端悲观→逆向看多
                detail = f"涨跌停比{ratio:.0%}(涨停{zt_count}/跌停{dt_count}), 极端悲观"
            elif ratio < 0.3:
                score = 0.2
                detail = f"涨跌停比{ratio:.0%}, 市场偏冷"
            else:
                score = 0
                detail = f"涨跌停比{ratio:.0%}(正常)"

            return {"score": score, "detail": detail, "ratio": round(ratio, 3),
                    "zt_count": zt_count, "dt_count": dt_count}

        except Exception as e:
            logger.debug(f"涨跌停数据获取失败: {e}")
            return {"score": None, "detail": f"涨跌停数据获取失败: {e}"}

    # ============================================================
    # 4. 融资余额拐点(P2)
    # ============================================================
    def _calc_margin_turning(self) -> dict:
        """融资余额拐点: 融资余额周变化率

        连续3周增加后首周减少: 杠杆资金撤退
        """
        try:
            import akshare as ak

            # 获取融资余额数据
            df = ak.stock_margin_sse(start_date="20260101")
            if df is None or df.empty:
                return {"score": None, "detail": "融资数据获取失败"}

            # 取最近几周数据
            if "融资余额" in df.columns:
                margin = df["融资余额"].values
            else:
                return {"score": None, "detail": "融资数据格式异常"}

            if len(margin) < 10:
                return {"score": None, "detail": "融资数据不足"}

            # 周频采样(每5个交易日)
            weekly_margin = [margin[i] for i in range(0, len(margin), 5)]
            if len(weekly_margin) < 4:
                return {"score": None, "detail": "融资周频数据不足"}

            # 计算周变化
            changes = []
            for i in range(1, len(weekly_margin)):
                prev = weekly_margin[i - 1]
                curr = weekly_margin[i]
                if prev > 0:
                    changes.append((curr - prev) / prev)

            if len(changes) < 3:
                return {"score": None, "detail": "融资变化数据不足"}

            recent_3 = changes[-3:]
            latest = changes[-1]

            # 连续3周增加后首周减少 → 拐点
            if all(c > 0 for c in recent_3[:-1]) and latest < 0:
                score = -0.4  # 杠杆撤退→看空
                detail = f"融资余额拐点: 连续{len(recent_3)-1}周增加后本周减少{latest*100:.2f}%, 杠杆撤退"
            elif all(c < 0 for c in recent_3[:-1]) and latest > 0:
                score = 0.3  # 连续减少后回升→杠杆回流
                detail = f"融资余额回升: 连续减少后本周增加{latest*100:.2f}%, 杠杆回流"
            elif latest > 0.005:
                score = -0.1
                detail = f"融资余额增加{latest*100:.2f}%, 杠杆加仓中"
            elif latest < -0.005:
                score = 0.1
                detail = f"融资余额减少{latest*100:.2f}%, 杠杆减仓中"
            else:
                score = 0
                detail = "融资余额平稳"

            return {"score": score, "detail": detail, "latest_change": round(latest * 100, 3)}

        except Exception as e:
            logger.debug(f"融资数据获取失败: {e}")
            return {"score": None, "detail": f"融资数据获取失败: {e}"}

    # ============================================================
    # 缓存管理
    # ============================================================
    def _load_cache(self) -> dict:
        """加载缓存"""
        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_cache(self, result: dict):
        """保存缓存"""
        try:
            cache = self._load_cache()
            cache["date"] = datetime.date.today().isoformat()
            cache["result"] = result
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"情绪缓存写入失败: {e}")

    def _update_cache_field(self, key: str, value):
        """更新缓存中的某个字段"""
        try:
            cache = self._load_cache()
            cache[key] = value
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


# 便捷函数
_default_sentiment = None


def get_market_sentiment() -> dict:
    """获取市场情绪分析结果(供consensus调用)"""
    global _default_sentiment
    if _default_sentiment is None:
        _default_sentiment = MarketSentiment()
    return _default_sentiment.analyze()
