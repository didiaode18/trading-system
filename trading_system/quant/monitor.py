"""
P2 策略有效性监控
==================
因子IC/IR值跟踪、收益衰减预警、风格漂移检测

功能:
1. 因子IC监控: 每日计算因子值与未来收益的相关性
2. 收益衰减预警: 策略近20日收益 vs 历史均值，低于阈值告警
3. 风格漂移检测: 持仓行业/市值分布偏移检测

使用方式:
    from quant.monitor import StrategyMonitor
    monitor = StrategyMonitor()
    ic = monitor.calc_factor_ic(factor_values, forward_returns)
    alert = monitor.check_decay(strategy_returns)
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)


class StrategyMonitor:
    """策略有效性监控器"""

    def __init__(self, ic_threshold: float = 0.03,
                 decay_threshold: float = -0.05,
                 drift_threshold: float = 0.3):
        """
        参数:
            ic_threshold: IC绝对值低于此值则因子失效
            decay_threshold: 近20日收益低于此值则衰减预警
            drift_threshold: 风格漂移超过此比例则告警
        """
        self.ic_threshold = ic_threshold
        self.decay_threshold = decay_threshold
        self.drift_threshold = drift_threshold
        self.ic_history = []  # [(date, factor_name, ic), ...]
        self.alerts = []

    # ============================================================
    # 一、因子IC监控
    # ============================================================

    def calc_factor_ic(self, factor_values: dict, forward_returns: dict,
                       date: str = None) -> dict:
        """
        计算因子IC（Information Coefficient）

        参数:
            factor_values: {code: factor_score}
            forward_returns: {code: forward_5d_return}

        返回:
            {"ic": float, "rank_ic": float, "date": str}
        """
        # 取交集
        common_codes = set(factor_values.keys()) & set(forward_returns.keys())
        if len(common_codes) < 10:
            return {"ic": 0, "rank_ic": 0, "date": date}

        fv = np.array([factor_values[c] for c in common_codes])
        fr = np.array([forward_returns[c] for c in common_codes])

        # Pearson IC
        if np.std(fv) > 0 and np.std(fr) > 0:
            ic = np.corrcoef(fv, fr)[0, 1]
        else:
            ic = 0

        # Rank IC (Spearman)
        fv_rank = pd.Series(fv).rank().values
        fr_rank = pd.Series(fr).rank().values
        if np.std(fv_rank) > 0 and np.std(fr_rank) > 0:
            rank_ic = np.corrcoef(fv_rank, fr_rank)[0, 1]
        else:
            rank_ic = 0

        result = {"ic": round(ic, 4), "rank_ic": round(rank_ic, 4), "date": date}
        self.ic_history.append(result)

        # 衰减检测：近20日IC均值
        if len(self.ic_history) >= 20:
            recent_ic = np.mean([h["ic"] for h in self.ic_history[-20:]])
            if abs(recent_ic) < self.ic_threshold:
                self._add_alert("IC_DECAY", f"因子IC衰减: 近20日均值={recent_ic:.4f}")

        return result

    def get_ic_summary(self, lookback: int = 60) -> dict:
        """获取IC汇总统计"""
        if not self.ic_history:
            return {"mean_ic": 0, "ic_ir": 0, "positive_ratio": 0}

        recent = self.ic_history[-lookback:]
        ics = [h["ic"] for h in recent]

        mean_ic = np.mean(ics)
        std_ic = np.std(ics) if len(ics) > 1 else 1
        ic_ir = mean_ic / std_ic if std_ic > 0 else 0
        positive_ratio = sum(1 for ic in ics if ic > 0) / len(ics)

        return {
            "mean_ic": round(mean_ic, 4),
            "ic_ir": round(ic_ir, 4),
            "positive_ratio": round(positive_ratio, 2),
            "sample_size": len(recent),
        }

    # ============================================================
    # 二、收益衰减预警
    # ============================================================

    def check_decay(self, strategy_returns: list, strategy_name: str = "") -> dict:
        """
        检测策略收益衰减

        参数:
            strategy_returns: 近期每日收益率列表
            strategy_name: 策略名称

        返回:
            {"is_decay": bool, "recent_avg": float, "historical_avg": float}
        """
        if len(strategy_returns) < 40:
            return {"is_decay": False, "recent_avg": 0, "historical_avg": 0}

        recent_20 = np.mean(strategy_returns[-20:])
        historical = np.mean(strategy_returns[:-20])

        is_decay = recent_20 < self.decay_threshold and recent_20 < historical * 0.5

        if is_decay:
            self._add_alert("RETURN_DECAY",
                          f"{strategy_name}收益衰减: 近20日={recent_20:.2%}, 历史={historical:.2%}")

        return {
            "is_decay": is_decay,
            "recent_avg": round(recent_20, 4),
            "historical_avg": round(historical, 4),
        }

    # ============================================================
    # 三、风格漂移检测
    # ============================================================

    def check_style_drift(self, current_holdings: dict,
                          historical_holdings: dict) -> dict:
        """
        检测持仓风格漂移

        参数:
            current_holdings: {code: {"sector": x, "market_cap": x}}
            historical_holdings: 历史持仓分布

        返回:
            {"is_drift": bool, "sector_drift": float, "cap_drift": float}
        """
        if not current_holdings or not historical_holdings:
            return {"is_drift": False, "sector_drift": 0, "cap_drift": 0}

        # 行业分布偏移
        curr_sectors = self._calc_sector_dist(current_holdings)
        hist_sectors = self._calc_sector_dist(historical_holdings)
        sector_drift = self._distribution_distance(curr_sectors, hist_sectors)

        # 市值分布偏移
        curr_caps = self._calc_cap_dist(current_holdings)
        hist_caps = self._calc_cap_dist(historical_holdings)
        cap_drift = self._distribution_distance(curr_caps, hist_caps)

        is_drift = sector_drift > self.drift_threshold or cap_drift > self.drift_threshold

        if is_drift:
            self._add_alert("STYLE_DRIFT",
                          f"风格漂移: 行业偏移={sector_drift:.2f}, 市值偏移={cap_drift:.2f}")

        return {
            "is_drift": is_drift,
            "sector_drift": round(sector_drift, 4),
            "cap_drift": round(cap_drift, 4),
        }

    # ============================================================
    # 四、告警管理
    # ============================================================

    def _add_alert(self, alert_type: str, message: str):
        alert = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "type": alert_type,
            "message": message,
        }
        self.alerts.append(alert)
        logger.warning(f"[{alert_type}] {message}")

    def get_alerts(self, clear: bool = False) -> list:
        alerts = self.alerts.copy()
        if clear:
            self.alerts = []
        return alerts

    # ============================================================
    # 辅助函数
    # ============================================================

    def _calc_sector_dist(self, holdings: dict) -> dict:
        sectors = {}
        for code, info in holdings.items():
            sector = info.get("sector", "unknown")
            sectors[sector] = sectors.get(sector, 0) + 1
        total = sum(sectors.values())
        return {k: v / total for k, v in sectors.items()} if total > 0 else {}

    def _calc_cap_dist(self, holdings: dict) -> dict:
        caps = {"small": 0, "mid": 0, "large": 0}
        for code, info in holdings.items():
            cap = info.get("market_cap", 0)
            if cap < 100e8:
                caps["small"] += 1
            elif cap < 500e8:
                caps["mid"] += 1
            else:
                caps["large"] += 1
        total = sum(caps.values())
        return {k: v / total for k, v in caps.items()} if total > 0 else {}

    def _distribution_distance(self, dist1: dict, dist2: dict) -> float:
        """计算两个分布的JS散度"""
        all_keys = set(list(dist1.keys()) + list(dist2.keys()))
        if not all_keys:
            return 0

        p = np.array([dist1.get(k, 0) for k in all_keys])
        q = np.array([dist2.get(k, 0) for k in all_keys])

        # 避免除零
        p = p + 1e-10
        q = q + 1e-10
        p = p / p.sum()
        q = q / q.sum()

        m = (p + q) / 2
        kl_pm = np.sum(p * np.log(p / m))
        kl_qm = np.sum(q * np.log(q / m))
        js_div = (kl_pm + kl_qm) / 2

        return float(js_div)
