"""
隔夜外盘联动前瞻性模块
======================
获取美股/港股/A50期货隔夜表现，计算对A股各行业的联动影响，
为综合分析报告和CANSLIM选股提供"次日开盘预判"前瞻维度。

数据源优先级:
  1. akshare Sina接口（稳定，免费，无需token）
  2. akshare 东方财富接口（实时性好，但易被反爬）
  3. 降级: 返回 available=False，不阻塞报告

使用方式:
    from trading_system.strategy.overnight_linkage import OvernightLinkage
    ol = OvernightLinkage()
    data = ol.fetch_global_indices()
    # data["sector_impacts"]["半导体"] -> {"score": +3, "direction": "利好", ...}
"""

import logging
import datetime
import time
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

try:
    from trading_system import config
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config


# ============================================================
# 一、行业 → 外盘指数映射（联动系数0-1）
# ============================================================

SECTOR_FOREIGN_MAP = {
    "半导体":     {"indices": ["SOX", "NASDAQ"], "coeff": 0.8},
    "AI数字经济": {"indices": ["NASDAQ", "SPX"],  "coeff": 0.6},
    "AI视觉":    {"indices": ["NASDAQ", "SPX"],  "coeff": 0.6},
    "新能源":     {"indices": ["NASDAQ"],          "coeff": 0.4},
    "医药医疗":   {"indices": ["SPX"],             "coeff": 0.3},
    "大消费":     {"indices": ["SPX"],             "coeff": 0.2},
    "大金融":     {"indices": ["SPX", "DJI"],      "coeff": 0.4},
    "有色资源":   {"indices": ["DJI"],             "coeff": 0.5},
    "军工航天":   {"indices": ["SPX"],             "coeff": 0.2},
    "指数ETF":    {"indices": ["SPX", "NASDAQ"],   "coeff": 0.3},
}

# 外盘指数代码 → 中文名
INDEX_NAMES = {
    "DJI": "道琼斯",
    "NASDAQ": "纳斯达克",
    "SPX": "标普500",
    "SOX": "费城半导体",
    "HSI": "恒生指数",
    "HSTECH": "恒生科技",
}


# ============================================================
# 二、评分规则
# ============================================================

def _change_to_base_score(change_pct: float) -> int:
    """外盘涨跌幅 → 基础分（-5 ~ +5）"""
    if change_pct > 3:
        return 5
    elif change_pct > 2:
        return 4
    elif change_pct > 1:
        return 2
    elif change_pct >= -1:
        return 0
    elif change_pct >= -2:
        return -2
    elif change_pct >= -3:
        return -4
    else:
        return -5


# ============================================================
# 三、核心类
# ============================================================

class OvernightLinkage:
    """隔夜外盘联动分析器"""

    def __init__(self):
        self._data: Optional[dict] = None
        self._enabled = getattr(config, "OVERNIGHT_LINKAGE_ENABLED", True)
        self._threshold = getattr(config, "OVERNIGHT_SIGNIFICANT_THRESHOLD", 2.0)
        self._max_bonus = getattr(config, "OVERNIGHT_MAX_BONUS", 3)

    # ----------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------
    def fetch_global_indices(self) -> dict:
        """
        获取全球主要指数隔夜表现，计算行业联动影响

        返回:
            {
                "available": bool,
                "fetch_time": str,
                "indices": {name: {"change_pct": float, "close": float, "date": str}},
                "sector_impacts": {sector: {"score": float, "direction": str, "reason": str}},
                "condition_hints": [str, ...],
                "overall_bias": str,
            }
        """
        empty = {
            "available": False,
            "fetch_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "indices": {},
            "sector_impacts": {},
            "condition_hints": [],
            "overall_bias": "中性",
        }

        if not self._enabled:
            logger.info("[外盘联动] 已禁用")
            return empty

        if not HAS_AKSHARE:
            logger.warning("[外盘联动] akshare不可用")
            return empty

        # 获取各指数数据
        indices = {}
        self._fetch_us_indices(indices)
        self._fetch_sox_index(indices)
        self._fetch_hk_indices(indices)

        if not indices:
            logger.warning("[外盘联动] 所有外盘数据获取失败，降级跳过")
            return empty

        # 计算行业影响
        sector_impacts = self._calc_all_sector_impacts(indices)

        # 生成条件单提示
        condition_hints = self._generate_hints(indices, sector_impacts)

        # 综合偏向
        overall_bias = self._calc_overall_bias(indices)

        self._data = {
            "available": True,
            "fetch_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "indices": indices,
            "sector_impacts": sector_impacts,
            "condition_hints": condition_hints,
            "overall_bias": overall_bias,
        }
        logger.info(f"[外盘联动] 获取成功: {len(indices)}个指数 | 综合偏向: {overall_bias}")
        return self._data

    # ----------------------------------------------------------
    # 数据获取（Sina源为主，稳定免费）
    # ----------------------------------------------------------
    def _fetch_us_indices(self, indices: dict):
        """获取美股三大指数（Sina日线，取最近2日计算涨跌幅）"""
        us_symbols = {
            "DJI": ".DJI",
            "NASDAQ": ".IXIC",
            "SPX": ".INX",
        }
        for key, symbol in us_symbols.items():
            try:
                df = ak.index_us_stock_sina(symbol=symbol)
                if df is not None and len(df) >= 2:
                    close_today = float(df["close"].iloc[-1])
                    close_prev = float(df["close"].iloc[-2])
                    change_pct = (close_today / close_prev - 1) * 100
                    date_str = str(df["date"].iloc[-1])
                    indices[key] = {
                        "change_pct": round(change_pct, 2),
                        "close": close_today,
                        "date": date_str,
                        "name": INDEX_NAMES.get(key, key),
                    }
            except Exception as e:
                logger.debug(f"[外盘联动] 美股{key}获取失败: {e}")

    def _fetch_sox_index(self, indices: dict):
        """获取费城半导体指数SOX"""
        try:
            df = ak.macro_global_sox_index()
            if df is not None and len(df) >= 1:
                latest = df.iloc[-1]
                change_pct = float(latest.get("涨跌幅", 0))
                close_val = float(latest.get("最新值", 0))
                date_str = str(latest.get("日期", ""))
                indices["SOX"] = {
                    "change_pct": round(change_pct, 2),
                    "close": close_val,
                    "date": date_str,
                    "name": "费城半导体",
                }
        except Exception as e:
            logger.debug(f"[外盘联动] SOX获取失败: {e}")

    def _fetch_hk_indices(self, indices: dict):
        """获取港股指数（恒生/恒生科技）"""
        hk_symbols = {"HSI": "HSI", "HSTECH": "HSTECH"}
        for key, symbol in hk_symbols.items():
            try:
                df = ak.stock_hk_index_daily_sina(symbol=symbol)
                if df is not None and len(df) >= 2:
                    close_today = float(df["close"].iloc[-1])
                    close_prev = float(df["close"].iloc[-2])
                    change_pct = (close_today / close_prev - 1) * 100
                    date_str = str(df["date"].iloc[-1])
                    indices[key] = {
                        "change_pct": round(change_pct, 2),
                        "close": close_today,
                        "date": date_str,
                        "name": INDEX_NAMES.get(key, key),
                    }
            except Exception as e:
                logger.debug(f"[外盘联动] 港股{key}获取失败: {e}")

    # ----------------------------------------------------------
    # 行业联动计算
    # ----------------------------------------------------------
    def _calc_all_sector_impacts(self, indices: dict) -> dict:
        """计算所有行业的联动影响分"""
        impacts = {}
        for sector, mapping in SECTOR_FOREIGN_MAP.items():
            score, reason = self._calc_single_sector(indices, mapping)
            direction = "利好" if score > 0 else "利空" if score < 0 else "中性"
            impacts[sector] = {
                "score": round(score, 1),
                "direction": direction,
                "reason": reason,
            }
        return impacts

    def _calc_single_sector(self, indices: dict, mapping: dict) -> tuple:
        """计算单个行业的联动分 = 基础分 × 联动系数"""
        coeff = mapping["coeff"]
        target_indices = mapping["indices"]

        # 取该行业映射的所有外盘指数中，绝对涨跌幅最大的作为主驱动
        best_change = 0
        best_name = ""
        for idx_key in target_indices:
            if idx_key in indices:
                chg = indices[idx_key]["change_pct"]
                if abs(chg) > abs(best_change):
                    best_change = chg
                    best_name = indices[idx_key]["name"]

        if best_change == 0:
            return 0, "无有效外盘数据"

        base_score = _change_to_base_score(best_change)
        final_score = base_score * coeff
        reason = f"{best_name}{best_change:+.1f}%, 联动系数{coeff}"
        return final_score, reason

    # ----------------------------------------------------------
    # 条件单提示生成
    # ----------------------------------------------------------
    def _generate_hints(self, indices: dict, sector_impacts: dict) -> list:
        """当外盘出现显著异动时，生成条件单调整建议"""
        hints = []
        threshold = self._threshold

        # 找出显著异动的外盘指数
        for idx_key, idx_data in indices.items():
            chg = idx_data["change_pct"]
            name = idx_data["name"]
            if abs(chg) >= threshold:
                if chg > 0:
                    hints.append(
                        f"⚡ 隔夜{name}{chg:+.1f}%，相关板块次日大概率高开，"
                        f"建议止损单上移/暂缓卖出"
                    )
                else:
                    hints.append(
                        f"⚡ 隔夜{name}{chg:+.1f}%，相关板块承压，"
                        f"建议关注开盘后15分钟确认方向再操作"
                    )

        # 行业级提示（仅对显著影响的行业）
        for sector, impact in sector_impacts.items():
            if abs(impact["score"]) >= 3:
                if impact["score"] > 0:
                    hints.append(f"📈 {sector}: {impact['reason']} → 次日偏多")
                else:
                    hints.append(f"📉 {sector}: {impact['reason']} → 次日偏空")

        return hints

    # ----------------------------------------------------------
    # 综合偏向
    # ----------------------------------------------------------
    def _calc_overall_bias(self, indices: dict) -> str:
        """根据主要指数加权判断综合偏向"""
        # 权重: 纳斯达克40%, 标普30%, 道琼斯15%, 恒生科技15%
        weights = {"NASDAQ": 0.4, "SPX": 0.3, "DJI": 0.15, "HSTECH": 0.15}
        weighted_sum = 0
        total_weight = 0
        for key, weight in weights.items():
            if key in indices:
                weighted_sum += indices[key]["change_pct"] * weight
                total_weight += weight

        if total_weight == 0:
            return "中性"

        avg_change = weighted_sum / total_weight
        if avg_change > 1:
            return "偏多"
        elif avg_change < -1:
            return "偏空"
        else:
            return "中性"

    # ----------------------------------------------------------
    # 便捷方法
    # ----------------------------------------------------------
    def get_sector_score(self, sector: str) -> float:
        """获取指定行业的外盘联动分（供CANSLIM P因子调用）"""
        if self._data is None or not self._data.get("available"):
            return 0
        impact = self._data["sector_impacts"].get(sector, {})
        score = impact.get("score", 0)
        # 只加不减，上限为config配置
        return min(max(score, 0), self._max_bonus)

    def get_condition_hint_for_sector(self, sector: str) -> Optional[str]:
        """获取指定行业的条件单提示（供综合报告调用）"""
        if self._data is None or not self._data.get("available"):
            return None
        impact = self._data["sector_impacts"].get(sector, {})
        if abs(impact.get("score", 0)) >= 3:
            direction = impact["direction"]
            reason = impact["reason"]
            if direction == "利好":
                return f"⚡ 隔夜外盘{reason}，{sector}次日大概率高开，建议止损单上移/暂缓卖出"
            else:
                return f"⚡ 隔夜外盘{reason}，{sector}承压，建议关注开盘后15分钟确认方向再操作"
        return None

    def get_summary_html(self) -> str:
        """生成外盘前瞻HTML板块（供综合报告直接嵌入）"""
        if self._data is None or not self._data.get("available"):
            return ""

        indices = self._data["indices"]
        bias = self._data["overall_bias"]
        fetch_time = self._data["fetch_time"]

        # 指数行情表
        idx_html = ""
        for key in ["DJI", "NASDAQ", "SPX", "SOX", "HSI", "HSTECH"]:
            if key in indices:
                d = indices[key]
                color = "#e74c3c" if d["change_pct"] > 0 else "#27ae60" if d["change_pct"] < 0 else "#666"
                arrow = "▲" if d["change_pct"] > 0 else "▼" if d["change_pct"] < 0 else "—"
                idx_html += (
                    f'<span style="display:inline-block;margin:4px 12px;padding:4px 10px;'
                    f'background:#f8f9fa;border-radius:4px;font-size:13px">'
                    f'{d["name"]} <b style="color:{color}">{arrow}{d["change_pct"]:+.2f}%</b>'
                    f'</span>'
                )

        # 行业影响表
        sector_html = ""
        impacts = self._data["sector_impacts"]
        for sector, impact in sorted(impacts.items(), key=lambda x: abs(x[1]["score"]), reverse=True):
            if abs(impact["score"]) >= 1:
                color = "#e74c3c" if impact["score"] > 0 else "#27ae60"
                sector_html += (
                    f'<span style="display:inline-block;margin:3px 8px;padding:3px 8px;'
                    f'background:#fff3e0;border-radius:3px;font-size:12px">'
                    f'{sector} <b style="color:{color}">{impact["score"]:+.1f}</b>'
                    f'({impact["reason"]})</span>'
                )

        # 条件单提示
        hints_html = ""
        for hint in self._data["condition_hints"]:
            hints_html += f'<div style="margin:4px 0;font-size:13px">{hint}</div>'

        bias_color = "#e74c3c" if bias == "偏多" else "#27ae60" if bias == "偏空" else "#666"

        html = f"""
<div style="background:#1a237e;color:#fff;
     border-radius:8px;padding:16px 20px;margin:15px 0">
  <h3 style="margin:0 0 10px;color:#fff;font-size:16px">🌍 隔夜外盘前瞻（次日开盘预判）</h3>
  <div style="margin-bottom:10px">{idx_html}</div>
  <div style="font-size:14px;margin:8px 0">
    综合预判: <b style="color:#ffd54f;font-size:15px">{bias}</b>
    <span style="color:#aaa;font-size:11px;margin-left:10px">数据时间: {fetch_time}</span>
  </div>
  {"<div style='margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.2)'>" + sector_html + "</div>" if sector_html else ""}
  {"<div style='margin-top:8px;padding:8px;background:rgba(255,255,255,0.1);border-radius:4px'>" + hints_html + "</div>" if hints_html else ""}
</div>
"""
        return html
