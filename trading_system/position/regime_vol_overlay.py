# -*- coding: utf-8 -*-
"""
仓位叠加层 · 波动率目标 + regime 联动 + 组合级回撤熔断（P2-1 路线图）
=====================================================================
在推荐/执行输出层"叠加"一个总仓位缩放建议，**不改撮合、不新建引擎**：
  1. 波动率目标仓位：复用 `position/vol_target.py` 的 `VolTargetManager`；
  2. regime 联动：复用 `strategy/regime_playbook.py` 的 `position_limits()["max_total"]`
     （BEAR/RANGE 自动收缩总仓位上限，且 playbook 已与风控引擎取更严值）；
  3. 组合级回撤熔断：基于主动策略净值曲线的峰谷回撤，分级降仓/全平（纯函数，可单测）。

红线（遵守 SKILL 铁律）：
  - 最终总仓位目标 **永不超过 regime 上限**，风控硬约束只增不减；
  - 任何异常降级为"不叠加"（scale=1、目标=regime 上限），绝不因叠加层报错阻断主流程。

用法：
    from position.regime_vol_overlay import RegimeVolOverlay
    ov = RegimeVolOverlay()
    res = ov.total_position_target(data_dict, holdings, regime_result, equity_curve)
    # res["final_total_target"] ∈ [0, regime_max_total]
"""

import logging
import os
import sys

import numpy as np
import config

logger = logging.getLogger(__name__)


def drawdown_breaker_scale(equity_curve, cfg: dict = None) -> dict:
    """组合级回撤熔断（纯函数，可独立单测）。

    参数:
        equity_curve: 净值序列（list / np.ndarray / pandas.Series），起点通常为 1.0。
        cfg: 熔断配置（默认取 config.POSITION_OVERLAY_CONFIG["circuit_breaker"]）。
             {enabled, warn_drawdown, warn_scale, trip_drawdown, trip_scale, cooldown_days}

    返回:
        {scale, drawdown, level, tripped, reason}
        level ∈ {"normal", "warn", "trip", "disabled", "insufficient"}
        scale ∈ [0, 1]：作用于总仓位目标的乘子（1=不降仓，0=全平）。
    """
    if cfg is None:
        cfg = (getattr(config, "POSITION_OVERLAY_CONFIG", {}) or {}).get("circuit_breaker", {}) or {}
    if not cfg.get("enabled", True):
        return {"scale": 1.0, "drawdown": None, "level": "disabled",
                "tripped": False, "reason": "熔断未启用"}

    try:
        arr = np.asarray(list(equity_curve) if equity_curve is not None else [], dtype=float)
    except Exception:
        arr = np.array([])
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return {"scale": 1.0, "drawdown": None, "level": "insufficient",
                "tripped": False, "reason": "净值样本不足，熔断不触发"}

    peak = float(np.max(arr))
    cur = float(arr[-1])
    dd = 0.0 if peak <= 0 else max(0.0, (peak - cur) / peak)

    trip_dd = float(cfg.get("trip_drawdown", 0.18))
    warn_dd = float(cfg.get("warn_drawdown", 0.10))
    trip_scale = float(cfg.get("trip_scale", 0.0))
    warn_scale = float(cfg.get("warn_scale", 0.6))

    if dd >= trip_dd:
        return {"scale": trip_scale, "drawdown": round(dd, 4), "level": "trip",
                "tripped": True, "cooldown_days": int(cfg.get("cooldown_days", 5)),
                "reason": f"回撤 {dd:.1%} ≥ 熔断阈值 {trip_dd:.0%}，全平并进入冷静期"}
    if dd >= warn_dd:
        return {"scale": warn_scale, "drawdown": round(dd, 4), "level": "warn",
                "tripped": False,
                "reason": f"回撤 {dd:.1%} ≥ 预警阈值 {warn_dd:.0%}，降仓至 {warn_scale:.0%}"}
    return {"scale": 1.0, "drawdown": round(dd, 4), "level": "normal",
            "tripped": False, "reason": f"回撤 {dd:.1%} 在阈值内，维持仓位"}


class RegimeVolOverlay:
    """波动率目标 + regime 联动 + 回撤熔断的总仓位叠加层。"""

    def __init__(self, cfg: dict = None):
        self.cfg = dict(getattr(config, "POSITION_OVERLAY_CONFIG", {}) or {})
        if cfg:
            self.cfg.update(cfg)
        self.enabled = bool(self.cfg.get("enabled", True))
        self._vtm = None

    def _vol_manager(self):
        """惰性构造 VolTargetManager（复用现有模块，不重写波动率估计）。"""
        if self._vtm is None:
            from position.vol_target import VolTargetManager
            self._vtm = VolTargetManager(
                target_vol=float(self.cfg.get("target_vol", 0.15)),
                lookback=int(self.cfg.get("vol_lookback", 20)),
                max_scale=float(self.cfg.get("max_scale", 1.3)),
                min_scale=float(self.cfg.get("min_scale", 0.3)),
            )
        return self._vtm

    def _regime_cap(self, regime_result: dict) -> float:
        """regime 联动的总仓位上限（复用 RegimePlaybook，已与风控引擎取更严）。"""
        try:
            from strategy.regime_playbook import RegimePlaybook
            pb = RegimePlaybook(regime_result)
            return float(pb.position_limits().get("max_total", 1.0)), pb.state
        except Exception as e:
            logger.debug(f"[Overlay] regime 上限读取失败，降级 1.0: {e}")
            return 1.0, "UNKNOWN"

    def total_position_target(self, data_dict: dict, holdings: dict = None,
                              regime_result: dict = None, equity_curve=None) -> dict:
        """计算叠加后的总仓位目标。

        返回:
            {enabled, regime_state, regime_max_total, vol_scale, breaker,
             final_total_target, recommendation}
            final_total_target ∈ [0, regime_max_total]（红线：永不超过 regime 上限）。
        """
        holdings = holdings or {}
        regime_result = regime_result or {}
        cap, state = self._regime_cap(regime_result)
        if not self.cfg.get("regime_cap_enabled", True):
            cap = 1.0  # 仅关闭 regime 联动时才放开上限（默认开启）

        if not self.enabled:
            return {"enabled": False, "regime_state": state, "regime_max_total": round(cap, 4),
                    "vol_scale": 1.0, "breaker": {"scale": 1.0, "level": "disabled"},
                    "final_total_target": round(cap, 4),
                    "recommendation": "叠加层未启用，总仓位目标=regime 上限"}

        # 1. 波动率目标缩放
        vol_scale, vol_detail = 1.0, {}
        try:
            vol_detail = self._vol_manager().calc_position_scale(
                data_dict or {}, holdings, market_info=None)
            vol_scale = float(vol_detail.get("scale", 1.0))
        except Exception as e:
            logger.debug(f"[Overlay] 波动率缩放失败，降级 1.0: {e}")

        # 2. 组合级回撤熔断
        breaker = drawdown_breaker_scale(equity_curve, self.cfg.get("circuit_breaker"))
        breaker_scale = float(breaker.get("scale", 1.0))

        # 3. 合成：regime 上限 × 波动率缩放 × 熔断缩放，并硬性 cap 到 regime 上限
        raw_target = cap * vol_scale * breaker_scale
        final_target = max(0.0, min(cap, raw_target))

        rec = (f"regime={state} 上限{cap:.0%}｜波动率缩放{vol_scale:.2f}"
               f"（{vol_detail.get('vol_regime', 'NA')}）｜熔断{breaker.get('level')}"
               f"（回撤{('%.1f%%' % (breaker.get('drawdown') * 100)) if isinstance(breaker.get('drawdown'), (int, float)) else 'NA'}）"
               f"→ 总仓位目标 {final_target:.0%}")

        return {
            "enabled": True,
            "regime_state": state,
            "regime_max_total": round(cap, 4),
            "vol_scale": round(vol_scale, 4),
            "vol_regime": vol_detail.get("vol_regime"),
            "market_vol": vol_detail.get("market_vol"),
            "breaker": breaker,
            "final_total_target": round(final_target, 4),
            "recommendation": rec,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== RegimeVolOverlay 自检 ===")
    ov = RegimeVolOverlay()
    # 熔断纯函数自检
    eq_ok = list(np.linspace(1.0, 1.2, 60))
    eq_warn = list(np.concatenate([np.linspace(1.0, 1.2, 40), np.linspace(1.2, 1.06, 20)]))
    eq_trip = list(np.concatenate([np.linspace(1.0, 1.2, 40), np.linspace(1.2, 0.95, 20)]))
    for name, eq in (("normal", eq_ok), ("warn", eq_warn), ("trip", eq_trip)):
        b = drawdown_breaker_scale(eq)
        print(f"  {name:7s} dd={b['drawdown']} level={b['level']} scale={b['scale']}")
    r = ov.total_position_target({}, {}, {"state": "RANGE"}, eq_trip)
    print("  RANGE+trip 总仓位目标:", r["final_total_target"], "|", r["recommendation"])
