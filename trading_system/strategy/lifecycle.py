"""
策略生命周期状态机（V3.2新增）
================================
管理策略从研发到退役的完整生命周期

状态流转:
  research → backtest → paper → live → retired
                ↓                    ↑
              rejected ←── degraded ─┘

核心功能:
  1. 策略注册+状态管理（JSON持久化）
  2. 自动晋升条件（回测达标→模拟→实盘）
  3. 自动降级条件（实盘连亏→退役）
  4. 策略健康度评分（夏普/胜率/回撤/运行天数）
  5. 与scheduler集成：每日自动评估所有策略状态

使用方式:
    from strategy.lifecycle import StrategyLifecycleManager
    mgr = StrategyLifecycleManager()
    mgr.register("CANSLIM_V3.2", author="system", description="五因子选股")
    mgr.promote("CANSLIM_V3.2")  # 晋升到下一状态
    mgr.evaluate_all()  # 每日自动评估
"""

import os
import sys
import json
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 策略状态定义
STATES = ["research", "backtest", "paper", "live", "retired", "rejected"]
STATE_CN = {
    "research": "研发中",
    "backtest": "回测验证",
    "paper": "模拟盘",
    "live": "实盘运行",
    "retired": "已退役",
    "rejected": "已否决",
}

# 晋升路径
PROMOTION_PATH = {
    "research": "backtest",
    "backtest": "paper",
    "paper": "live",
}

# 降级路径
DEMOTION_PATH = {
    "live": "paper",
    "paper": "backtest",
    "backtest": "rejected",
}

# 生命周期数据文件
LIFECYCLE_DB_PATH = os.path.join(config.DATA_DIR, "strategy_lifecycle.json")


class StrategyLifecycleManager:
    """策略生命周期管理器"""

    # 晋升条件阈值
    PROMOTE_CRITERIA = {
        "backtest_to_paper": {
            "min_sharpe": 0.8,
            "min_win_rate": 0.40,
            "max_drawdown": -0.25,
            "min_trades": 20,
        },
        "paper_to_live": {
            "min_days": 20,          # 模拟盘至少运行20天
            "min_sharpe": 0.5,
            "min_win_rate": 0.38,
            "max_drawdown": -0.20,
        },
    }

    # 降级条件阈值
    DEMOTE_CRITERIA = {
        "live_to_paper": {
            "consec_losses": 5,       # 连续5笔亏损
            "rolling_win_rate": 0.30, # 滚动20笔胜率<30%
            "max_drawdown": -0.20,    # 实盘回撤>20%
        },
        "paper_to_backtest": {
            "consec_losses": 7,
            "rolling_win_rate": 0.25,
        },
    }

    def __init__(self):
        self.strategies = {}
        self._load()

    # ============================================================
    # 持久化
    # ============================================================

    def _load(self):
        """从JSON加载策略状态"""
        if os.path.exists(LIFECYCLE_DB_PATH):
            try:
                with open(LIFECYCLE_DB_PATH, 'r', encoding='utf-8') as f:
                    self.strategies = json.load(f)
            except Exception as e:
                logger.warning(f"[生命周期] 加载失败: {e}")
                self.strategies = {}

    def _save(self):
        """持久化到JSON"""
        try:
            os.makedirs(os.path.dirname(LIFECYCLE_DB_PATH), exist_ok=True)
            with open(LIFECYCLE_DB_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.strategies, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[生命周期] 保存失败: {e}")

    # ============================================================
    # 策略注册与管理
    # ============================================================

    def register(self, name: str, author: str = "system",
                 description: str = "", strategy_type: str = "momentum") -> dict:
        """注册新策略"""
        if name in self.strategies:
            logger.info(f"[生命周期] 策略已存在: {name}")
            return self.strategies[name]

        entry = {
            "name": name,
            "author": author,
            "description": description,
            "type": strategy_type,
            "state": "research",
            "created_at": datetime.date.today().isoformat(),
            "state_changed_at": datetime.date.today().isoformat(),
            "history": [{"state": "research", "date": datetime.date.today().isoformat(), "reason": "初始注册"}],
            "metrics": {
                "sharpe": 0.0,
                "win_rate": 0.0,
                "max_drawdown": 0.0,
                "total_trades": 0,
                "consec_losses": 0,
                "days_in_state": 0,
            },
            "config": {},  # 策略特有参数
            "enabled": True,
        }
        self.strategies[name] = entry
        self._save()
        logger.info(f"[生命周期] 注册策略: {name} (state=research)")
        return entry

    def get_state(self, name: str) -> str:
        """获取策略当前状态"""
        if name not in self.strategies:
            return "unknown"
        return self.strategies[name]["state"]

    def get_active_strategies(self) -> list:
        """获取所有实盘运行的策略"""
        return [
            name for name, info in self.strategies.items()
            if info["state"] == "live" and info.get("enabled", True)
        ]

    def get_strategies_by_state(self, state: str) -> list:
        """按状态筛选策略"""
        return [name for name, info in self.strategies.items() if info["state"] == state]

    # ============================================================
    # 晋升/降级
    # ============================================================

    def promote(self, name: str, reason: str = "") -> bool:
        """晋升策略到下一状态"""
        if name not in self.strategies:
            logger.warning(f"[生命周期] 策略不存在: {name}")
            return False

        current = self.strategies[name]["state"]
        next_state = PROMOTION_PATH.get(current)
        if not next_state:
            logger.info(f"[生命周期] {name} 已处于最高状态({current})")
            return False

        self.strategies[name]["state"] = next_state
        self.strategies[name]["state_changed_at"] = datetime.date.today().isoformat()
        self.strategies[name]["history"].append({
            "state": next_state,
            "date": datetime.date.today().isoformat(),
            "reason": reason or f"从{STATE_CN[current]}晋升",
        })
        self._save()
        logger.info(f"[生命周期] {name}: {STATE_CN[current]} → {STATE_CN[next_state]}")
        return True

    def demote(self, name: str, reason: str = "") -> bool:
        """降级策略"""
        if name not in self.strategies:
            return False

        current = self.strategies[name]["state"]
        prev_state = DEMOTION_PATH.get(current)
        if not prev_state:
            # 已经是最低状态，直接退役
            prev_state = "retired"

        self.strategies[name]["state"] = prev_state
        self.strategies[name]["state_changed_at"] = datetime.date.today().isoformat()
        self.strategies[name]["history"].append({
            "state": prev_state,
            "date": datetime.date.today().isoformat(),
            "reason": reason or f"从{STATE_CN[current]}降级",
        })
        self._save()
        logger.warning(f"[生命周期] {name}: {STATE_CN[current]} → {STATE_CN[prev_state]} ({reason})")
        return True

    def retire(self, name: str, reason: str = "") -> bool:
        """退役策略"""
        if name not in self.strategies:
            return False
        self.strategies[name]["state"] = "retired"
        self.strategies[name]["enabled"] = False
        self.strategies[name]["state_changed_at"] = datetime.date.today().isoformat()
        self.strategies[name]["history"].append({
            "state": "retired",
            "date": datetime.date.today().isoformat(),
            "reason": reason or "手动退役",
        })
        self._save()
        logger.info(f"[生命周期] {name}: 已退役 ({reason})")
        return True

    # ============================================================
    # 指标更新
    # ============================================================

    def update_metrics(self, name: str, metrics: dict):
        """更新策略绩效指标"""
        if name not in self.strategies:
            return
        self.strategies[name]["metrics"].update(metrics)
        # 计算在当前状态的天数
        changed = self.strategies[name].get("state_changed_at", "")
        if changed:
            try:
                days = (datetime.date.today() - datetime.date.fromisoformat(changed)).days
                self.strategies[name]["metrics"]["days_in_state"] = days
            except Exception:
                pass
        self._save()

    def record_trade(self, name: str, pnl_pct: float):
        """记录一笔交易结果"""
        if name not in self.strategies:
            return
        m = self.strategies[name]["metrics"]
        m["total_trades"] = m.get("total_trades", 0) + 1
        if pnl_pct < 0:
            m["consec_losses"] = m.get("consec_losses", 0) + 1
        else:
            m["consec_losses"] = 0
        self._save()

    # ============================================================
    # 自动评估（每日运行）
    # ============================================================

    def evaluate_all(self) -> dict:
        """评估所有策略，自动晋升/降级
        
        返回: {"promoted": [...], "demoted": [...], "unchanged": [...]}
        """
        result = {"promoted": [], "demoted": [], "unchanged": []}

        for name, info in list(self.strategies.items()):
            state = info["state"]
            metrics = info.get("metrics", {})

            if state in ("retired", "rejected", "research"):
                result["unchanged"].append(name)
                continue

            # 检查降级条件
            if self._should_demote(state, metrics):
                reason = self._get_demote_reason(state, metrics)
                self.demote(name, reason)
                result["demoted"].append({"name": name, "reason": reason})
                continue

            # 检查晋升条件
            if self._should_promote(state, metrics):
                reason = f"指标达标，自动晋升"
                self.promote(name, reason)
                result["promoted"].append({"name": name, "reason": reason})
                continue

            result["unchanged"].append(name)

        return result

    def _should_promote(self, state: str, metrics: dict) -> bool:
        """判断是否满足晋升条件"""
        if state == "backtest":
            criteria = self.PROMOTE_CRITERIA["backtest_to_paper"]
            return (
                metrics.get("sharpe", 0) >= criteria["min_sharpe"]
                and metrics.get("win_rate", 0) >= criteria["min_win_rate"]
                and metrics.get("max_drawdown", -1) >= criteria["max_drawdown"]
                and metrics.get("total_trades", 0) >= criteria["min_trades"]
            )
        elif state == "paper":
            criteria = self.PROMOTE_CRITERIA["paper_to_live"]
            return (
                metrics.get("days_in_state", 0) >= criteria["min_days"]
                and metrics.get("sharpe", 0) >= criteria["min_sharpe"]
                and metrics.get("win_rate", 0) >= criteria["min_win_rate"]
                and metrics.get("max_drawdown", -1) >= criteria["max_drawdown"]
            )
        return False

    def _should_demote(self, state: str, metrics: dict) -> bool:
        """判断是否满足降级条件"""
        if state == "live":
            criteria = self.DEMOTE_CRITERIA["live_to_paper"]
            return (
                metrics.get("consec_losses", 0) >= criteria["consec_losses"]
                or metrics.get("win_rate", 1) < criteria["rolling_win_rate"]
                or metrics.get("max_drawdown", 0) < criteria["max_drawdown"]
            )
        elif state == "paper":
            criteria = self.DEMOTE_CRITERIA["paper_to_backtest"]
            return (
                metrics.get("consec_losses", 0) >= criteria["consec_losses"]
                or metrics.get("win_rate", 1) < criteria["rolling_win_rate"]
            )
        return False

    def _get_demote_reason(self, state: str, metrics: dict) -> str:
        """生成降级原因描述"""
        reasons = []
        if metrics.get("consec_losses", 0) >= 5:
            reasons.append(f"连续{metrics['consec_losses']}笔亏损")
        if metrics.get("win_rate", 1) < 0.30:
            reasons.append(f"胜率{metrics['win_rate']:.0%}过低")
        if metrics.get("max_drawdown", 0) < -0.20:
            reasons.append(f"回撤{metrics['max_drawdown']:.0%}超限")
        return "；".join(reasons) if reasons else "综合指标不达标"

    # ============================================================
    # 报告
    # ============================================================

    def get_summary(self) -> str:
        """生成策略生命周期摘要"""
        lines = ["=" * 50, "策略生命周期状态", "=" * 50]
        for state in ["live", "paper", "backtest", "research", "retired", "rejected"]:
            strategies = self.get_strategies_by_state(state)
            if strategies:
                lines.append(f"\n【{STATE_CN[state]}】({len(strategies)}个)")
                for name in strategies:
                    info = self.strategies[name]
                    m = info.get("metrics", {})
                    lines.append(
                        f"  • {name}: 夏普{m.get('sharpe', 0):.2f} "
                        f"胜率{m.get('win_rate', 0):.0%} "
                        f"回撤{m.get('max_drawdown', 0):.0%} "
                        f"交易{m.get('total_trades', 0)}笔"
                    )
        return "\n".join(lines)


# ============================================================
# 初始化：注册系统内置策略
# ============================================================

def init_builtin_strategies():
    """注册系统内置策略（首次运行时调用）"""
    mgr = StrategyLifecycleManager()

    builtin = [
        ("CANSLIM_V3.2", "五因子选股+IC动态降权", "momentum"),
        ("均值回归", "RSI超卖+布林下轨+缩量反弹", "mean_reversion"),
        ("操盘密码DK", "DK信号+四阶段+VWAP", "trend"),
        ("事件驱动", "财报/回购/定增事件信号", "event"),
        ("板块轮动", "行业动量+资金流驱动轮动", "rotation"),
    ]

    for name, desc, stype in builtin:
        if name not in mgr.strategies:
            mgr.register(name, author="system", description=desc, strategy_type=stype)
            # 内置策略直接设为live（已有历史验证）
            mgr.strategies[name]["state"] = "live"
            mgr.strategies[name]["state_changed_at"] = datetime.date.today().isoformat()

    mgr._save()
    return mgr
