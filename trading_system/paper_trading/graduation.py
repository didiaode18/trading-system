# -*- coding: utf-8 -*-
"""
毕业条件判定（graduation）
==========================
铁律：任何通过回测的策略，必须在模拟盘"毕业"后才允许进入实盘。

默认毕业条件（可在 config.PAPER_TRADING_CONFIG 覆盖）：
  1. 最少运行 20 个交易日；
  2. 夏普比率 > 1.0；
  3. 最大回撤 < 10%；
  4. 模拟盘 vs 回测净值日偏差绝对值均值 < 2%（验证可复现性）。
达标 → 建议 paper → live（仅生成建议，人工确认，绝不自动转实盘）；
不达标 → 输出失败项，保持 paper（延期）；偏差持续过大 → 建议 rejected。

绩效口径复用 backtest/metrics.py（夏普、最大回撤），不另造轮子。
"""

import os
import json
import logging
import datetime

from .tracker import PaperTracker

try:
    from ..backtest.metrics import calc_sharpe_ratio, calc_max_drawdown
except ImportError:  # pragma: no cover
    from backtest.metrics import calc_sharpe_ratio, calc_max_drawdown

logger = logging.getLogger(__name__)

# 默认阈值
DEFAULT_GRADUATION_DAYS = 20
DEFAULT_MIN_SHARPE = 1.0
DEFAULT_MAX_DRAWDOWN = 0.10
DEFAULT_NAV_DEVIATION = 0.02
DEFAULT_REJECT_DEVIATION = 0.05  # 偏差持续过大 → 建议淘汰


def _lifecycle_path(path=None) -> str:
    if path:
        return path
    try:
        import config
        return os.path.join(config.DATA_DIR, "strategy_lifecycle.json")
    except Exception:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(here, "data", "strategy_lifecycle.json")


class GraduationJudge:
    """模拟盘毕业判定器。"""

    def __init__(self, config: dict = None, tracker: PaperTracker = None):
        cfg = config
        if cfg is None:
            try:
                import config as _c
                cfg = getattr(_c, "PAPER_TRADING_CONFIG", {}) or {}
            except Exception:
                cfg = {}
        self.cfg = cfg
        self.graduation_days = int(cfg.get("graduation_days", DEFAULT_GRADUATION_DAYS))
        self.min_sharpe = float(cfg.get("min_sharpe", DEFAULT_MIN_SHARPE))
        self.max_drawdown = float(cfg.get("max_drawdown", DEFAULT_MAX_DRAWDOWN))
        self.nav_threshold = float(cfg.get("nav_deviation_threshold", DEFAULT_NAV_DEVIATION))
        self.reject_threshold = float(cfg.get("reject_deviation_threshold", DEFAULT_REJECT_DEVIATION))
        self.tracker = tracker or PaperTracker()

    def compute_metrics(self, strategy_name: str) -> dict:
        """从台账净值序列计算绩效指标（复用回测口径）。"""
        metrics = {"trading_days": 0, "sharpe": 0.0, "max_drawdown": 0.0,
                   "total_return": 0.0, "final_nav": 1.0}
        try:
            nav = self.tracker.get_nav_series(strategy_name)
            metrics["trading_days"] = int(len(nav))
            if len(nav) == 0:
                return metrics
            metrics["final_nav"] = round(float(nav.iloc[-1]), 6)
            metrics["total_return"] = round(float(nav.iloc[-1]) - 1.0, 6)
            daily_ret = nav.pct_change().dropna()
            metrics["sharpe"] = round(float(calc_sharpe_ratio(daily_ret)), 4)
            mdd, _, _ = calc_max_drawdown(nav)
            metrics["max_drawdown"] = round(float(mdd), 6)
        except Exception as e:
            logger.warning(f"[毕业判定] {strategy_name} 指标计算异常(降级): {e}")
        return metrics

    def evaluate(self, strategy_name: str, comparator_result: dict = None) -> dict:
        """毕业判定。

        参数:
            comparator_result: PaperComparator.compare 的输出（None=未做回测对比）

        返回:
            {graduated, next_state, metrics, failed_items, nav_deviation_mean,
             deviation_checked, reason}
        """
        metrics = self.compute_metrics(strategy_name)
        failed = []

        # 条件1：最少交易日
        if metrics["trading_days"] < self.graduation_days:
            failed.append(f"运行天数不足：{metrics['trading_days']} < {self.graduation_days}个交易日")
        # 条件2：夏普
        if metrics["sharpe"] <= self.min_sharpe:
            failed.append(f"夏普不达标：{metrics['sharpe']:.3f} ≤ {self.min_sharpe}")
        # 条件3：最大回撤
        if metrics["max_drawdown"] >= self.max_drawdown:
            failed.append(f"最大回撤超标：{metrics['max_drawdown']*100:.2f}% ≥ {self.max_drawdown*100:.0f}%")

        # 条件4：净值偏差（仅在有回测对比时校验）
        nav_dev = None
        deviation_checked = False
        if comparator_result and comparator_result.get("ok"):
            nav_dev = comparator_result.get("nav_deviation_mean")
            if nav_dev is not None:
                deviation_checked = True
                if nav_dev >= self.nav_threshold:
                    failed.append(
                        f"模拟盘vs回测净值偏差过大：{nav_dev*100:.2f}% ≥ {self.nav_threshold*100:.0f}%")

        # 决策 next_state
        if not failed:
            next_state = "live"
            reason = "全部毕业条件达标，建议转实盘（需人工确认）"
        elif deviation_checked and nav_dev is not None and nav_dev >= self.reject_threshold:
            next_state = "rejected"
            reason = f"净值偏差持续过大（{nav_dev*100:.2f}% ≥ {self.reject_threshold*100:.0f}%），建议淘汰"
        else:
            next_state = "paper"
            reason = "存在未达标项，保持模拟盘观察（延期）"

        return {
            "strategy": strategy_name,
            "graduated": next_state == "live",
            "next_state": next_state,
            "metrics": metrics,
            "failed_items": failed,
            "nav_deviation_mean": nav_dev,
            "deviation_checked": deviation_checked,
            "reason": reason,
        }


def suggest_lifecycle_transition(strategy_name: str, evaluation: dict,
                                 lifecycle_path: str = None) -> dict:
    """把毕业判定结果写入 strategy_lifecycle.json 的 graduation_suggestion 字段。

    仅生成建议，**绝不自动修改 state**（转 live 必须人工确认）。
    静默降级：文件缺失/异常时返回 {"ok": False}，不抛异常。
    """
    out = {"ok": False, "strategy": strategy_name,
           "suggested_state": evaluation.get("next_state")}
    try:
        path = _lifecycle_path(lifecycle_path)
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        entry = data.get(strategy_name)
        if not isinstance(entry, dict):
            entry = {"name": strategy_name, "state": "paper"}
        entry["graduation_suggestion"] = {
            "next_state": evaluation.get("next_state"),
            "graduated": evaluation.get("graduated"),
            "reason": evaluation.get("reason"),
            "failed_items": evaluation.get("failed_items", []),
            "metrics": evaluation.get("metrics", {}),
            "nav_deviation_mean": evaluation.get("nav_deviation_mean"),
            "suggested_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "仅建议，转live需人工确认；系统不自动改 state",
        }
        data[strategy_name] = entry
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        out["ok"] = True
        logger.info(f"[毕业判定] {strategy_name} 建议 → {evaluation.get('next_state')}"
                    f"（已写入建议，未改 state）")
    except Exception as e:
        logger.warning(f"[毕业判定] 写入生命周期建议失败(降级): {e}")
    return out
