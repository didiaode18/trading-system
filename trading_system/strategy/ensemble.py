"""
多策略投票器/集成决策引擎（V3.2新增）
======================================
多策略并行运行，加权投票产生最终交易信号

核心功能:
  1. 多策略信号聚合（加权投票/多数决/一票否决）
  2. 信号冲突检测与仲裁
  3. 策略权重动态调整（基于近期表现）
  4. 与lifecycle集成：仅使用live状态的策略
  5. 输出最终决策+置信度+各策略明细

投票模式:
  - weighted: 加权投票（默认，按策略权重×信号强度）
  - majority: 多数决（>50%策略同意才执行）
  - veto: 一票否决（任何策略强烈反对则否决）

使用方式:
    from strategy.ensemble import EnsembleEngine
    engine = EnsembleEngine()
    decision = engine.decide(code, market_data, signals_dict)
    # decision = {"action": "buy", "confidence": 0.72, "detail": ...}
"""

import os
import sys
import json
import logging
import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 信号方向常量
SIGNAL_BUY = 1
SIGNAL_HOLD = 0
SIGNAL_SELL = -1


class EnsembleEngine:
    """多策略集成决策引擎"""

    # 默认策略权重（可通过performance动态调整）
    DEFAULT_WEIGHTS = {
        "CANSLIM_V3.2": 0.30,
        "操盘密码DK": 0.25,
        "均值回归": 0.20,
        "事件驱动": 0.10,
        "板块轮动": 0.15,
    }

    # 决策阈值
    BUY_THRESHOLD = 0.30     # 加权得分>0.30才买入
    SELL_THRESHOLD = -0.25   # 加权得分<-0.25才卖出
    STRONG_VETO = -0.80      # 单策略强烈反对阈值

    def __init__(self, mode: str = "weighted"):
        """
        参数:
            mode: 投票模式 "weighted"/"majority"/"veto"
        """
        self.mode = mode
        self.weights = dict(self.DEFAULT_WEIGHTS)
        self.decision_history = []  # 最近决策记录
        self._load_weights()

    def _load_weights(self):
        """从持久化文件加载动态权重"""
        weight_path = os.path.join(config.DATA_DIR, "ensemble_weights.json")
        if os.path.exists(weight_path):
            try:
                with open(weight_path, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                self.weights.update(saved.get("weights", {}))
            except Exception:
                pass

    def _save_weights(self):
        """保存动态权重"""
        weight_path = os.path.join(config.DATA_DIR, "ensemble_weights.json")
        try:
            os.makedirs(os.path.dirname(weight_path), exist_ok=True)
            with open(weight_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "weights": self.weights,
                    "updated_at": datetime.datetime.now().isoformat(),
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ============================================================
    # 核心决策
    # ============================================================

    def decide(self, code: str, signals: dict, market_state: str = "RANGE",
               extra_context: dict = None) -> dict:
        """
        多策略投票决策
        
        参数:
            code: 股票代码
            signals: {strategy_name: {"direction": 1/0/-1, "strength": 0-1, "reason": str}}
            market_state: 大盘状态 "BULL"/"RANGE"/"BEAR"
            extra_context: 额外上下文（持仓信息等）
        
        返回:
            {
                "action": "buy"/"sell"/"hold",
                "confidence": float (0-1),
                "score": float (-1 to 1),
                "votes": {strategy: vote_detail},
                "conflicts": [str],  # 冲突描述
                "veto": bool,  # 是否被一票否决
                "detail": str,
            }
        """
        if not signals:
            return self._hold_result("无策略信号")

        # 1. 收集有效投票
        votes = {}
        for name, sig in signals.items():
            direction = sig.get("direction", SIGNAL_HOLD)
            strength = sig.get("strength", 0.5)
            weight = self.weights.get(name, 0.1)
            votes[name] = {
                "direction": direction,
                "strength": strength,
                "weight": weight,
                "weighted_vote": direction * strength * weight,
                "reason": sig.get("reason", ""),
            }

        # 2. 计算加权得分
        total_weight = sum(v["weight"] for v in votes.values())
        if total_weight <= 0:
            return self._hold_result("策略权重为0")

        weighted_score = sum(v["weighted_vote"] for v in votes.values()) / total_weight

        # 3. 检测冲突
        conflicts = self._detect_conflicts(votes)

        # 4. 一票否决检查
        veto = False
        veto_reason = ""
        if self.mode == "veto" or market_state == "BEAR":
            for name, v in votes.items():
                if v["direction"] == SIGNAL_SELL and v["strength"] >= 0.8:
                    veto = True
                    veto_reason = f"{name}强烈反对(强度{v['strength']:.0%})"
                    break

        # 5. 多数决模式
        if self.mode == "majority":
            buy_count = sum(1 for v in votes.values() if v["direction"] == SIGNAL_BUY)
            sell_count = sum(1 for v in votes.values() if v["direction"] == SIGNAL_SELL)
            total = len(votes)
            if buy_count > total * 0.5:
                weighted_score = max(weighted_score, self.BUY_THRESHOLD)
            elif sell_count > total * 0.5:
                weighted_score = min(weighted_score, self.SELL_THRESHOLD)

        # 6. 大盘状态修正
        if market_state == "BEAR":
            weighted_score *= 0.6  # 熊市削弱买入信号
        elif market_state == "BULL":
            weighted_score *= 1.1  # 牛市增强买入信号

        # 7. 最终决策
        if veto:
            action = "hold"
            confidence = 0.9
            detail = f"一票否决: {veto_reason}"
        elif weighted_score >= self.BUY_THRESHOLD:
            action = "buy"
            confidence = min(0.95, 0.5 + weighted_score * 0.5)
            detail = f"加权得分{weighted_score:+.3f}≥买入阈值{self.BUY_THRESHOLD}"
        elif weighted_score <= self.SELL_THRESHOLD:
            action = "sell"
            confidence = min(0.95, 0.5 + abs(weighted_score) * 0.5)
            detail = f"加权得分{weighted_score:+.3f}≤卖出阈值{self.SELL_THRESHOLD}"
        else:
            action = "hold"
            confidence = 0.6
            detail = f"加权得分{weighted_score:+.3f}在观望区间"

        result = {
            "code": code,
            "action": action,
            "confidence": round(confidence, 3),
            "score": round(weighted_score, 4),
            "votes": votes,
            "conflicts": conflicts,
            "veto": veto,
            "veto_reason": veto_reason,
            "market_state": market_state,
            "detail": detail,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 记录决策历史（保留最近100条）
        self.decision_history.append(result)
        if len(self.decision_history) > 100:
            self.decision_history = self.decision_history[-100:]

        logger.info(f"[集成决策] {code}: {action}(置信{confidence:.0%}) {detail}")
        return result

    # ============================================================
    # 冲突检测
    # ============================================================

    def _detect_conflicts(self, votes: dict) -> list:
        """检测策略间信号冲突"""
        conflicts = []
        buy_strategies = [n for n, v in votes.items() if v["direction"] == SIGNAL_BUY]
        sell_strategies = [n for n, v in votes.items() if v["direction"] == SIGNAL_SELL]

        if buy_strategies and sell_strategies:
            conflicts.append(
                f"信号冲突: {'/'.join(buy_strategies)}看多 vs {'/'.join(sell_strategies)}看空"
            )

        # 检测强信号冲突（两个高权重策略方向相反）
        for n1, v1 in votes.items():
            for n2, v2 in votes.items():
                if n1 >= n2:
                    continue
                if (v1["direction"] * v2["direction"] < 0
                        and v1["strength"] > 0.7 and v2["strength"] > 0.7
                        and v1["weight"] > 0.15 and v2["weight"] > 0.15):
                    conflicts.append(
                        f"强冲突: {n1}(强度{v1['strength']:.0%}) vs {n2}(强度{v2['strength']:.0%})"
                    )
        return conflicts

    # ============================================================
    # 权重动态调整
    # ============================================================

    def update_weights_by_performance(self, performance: dict):
        """根据近期表现动态调整策略权重
        
        参数:
            performance: {strategy_name: {"sharpe": float, "win_rate": float, "recent_pnl": float}}
        """
        for name, perf in performance.items():
            if name not in self.weights:
                continue
            sharpe = perf.get("sharpe", 0)
            win_rate = perf.get("win_rate", 0.5)

            # 表现好→加权，表现差→降权
            if sharpe > 1.0 and win_rate > 0.5:
                self.weights[name] = min(0.40, self.weights[name] * 1.2)
            elif sharpe < 0 or win_rate < 0.35:
                self.weights[name] = max(0.05, self.weights[name] * 0.7)

        # 归一化
        total = sum(self.weights.values())
        if total > 0:
            self.weights = {k: round(v / total, 3) for k, v in self.weights.items()}

        self._save_weights()
        logger.info(f"[集成决策] 权重更新: {self.weights}")

    # ============================================================
    # 辅助
    # ============================================================

    def _hold_result(self, reason: str) -> dict:
        """生成hold结果"""
        return {
            "action": "hold",
            "confidence": 0.5,
            "score": 0.0,
            "votes": {},
            "conflicts": [],
            "veto": False,
            "veto_reason": "",
            "detail": reason,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def get_status(self) -> dict:
        """获取引擎状态"""
        return {
            "mode": self.mode,
            "weights": self.weights,
            "recent_decisions": len(self.decision_history),
            "last_decision": self.decision_history[-1] if self.decision_history else None,
        }
