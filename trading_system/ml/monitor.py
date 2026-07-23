"""
模型漂移监控
============
- 跟踪预测准确率
- 连续10天<55%触发重训练预警
- 记录预测日志
"""

import logging
import json
import os
import datetime
from collections import deque

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

MONITOR_FILE = os.path.join(config.PROJECT_ROOT, "ml", "monitor_log.json")


class ModelMonitor:
    """
    模型漂移监控器
    
    用法:
        monitor = ModelMonitor()
        monitor.record_prediction(code, date, prob, actual_return)
        if monitor.needs_retrain():
            print("模型需要重训练！")
    """

    def __init__(self, accuracy_threshold: float = 0.55, window_days: int = 10):
        self.accuracy_threshold = accuracy_threshold
        self.window_days = window_days
        self.predictions: deque = deque(maxlen=200)
        self._load_history()

    def record_prediction(self, code: str, date: str, prob: float,
                          actual_return: float = None):
        """记录一次预测"""
        self.predictions.append({
            "code": code,
            "date": date,
            "prob": prob,
            "actual_return": actual_return,
            "verified": actual_return is not None,
        })

    def verify_prediction(self, code: str, date: str, actual_return: float):
        """回填实际收益，验证预测"""
        for p in self.predictions:
            if p["code"] == code and p["date"] == date and not p["verified"]:
                p["actual_return"] = actual_return
                p["verified"] = True
                break

    def get_recent_accuracy(self) -> float:
        """计算最近N天的预测准确率"""
        verified = [p for p in self.predictions if p["verified"]]
        if len(verified) < 5:
            return 0.5  # 样本不足，返回中性值

        recent = verified[-self.window_days * 3:]  # 取足够多的样本
        correct = 0
        total = 0
        for p in recent:
            predicted_up = p["prob"] > 0.5
            actual_up = p["actual_return"] > 0
            if predicted_up == actual_up:
                correct += 1
            total += 1

        return correct / total if total > 0 else 0.5

    def needs_retrain(self) -> bool:
        """判断是否需要重训练"""
        verified = [p for p in self.predictions if p["verified"]]
        if len(verified) < self.window_days:
            return False

        # 检查最近window_days天的准确率
        recent = verified[-self.window_days:]
        correct = sum(1 for p in recent
                     if (p["prob"] > 0.5) == (p["actual_return"] > 0))
        accuracy = correct / len(recent)

        if accuracy < self.accuracy_threshold:
            logger.warning(f"模型漂移预警: 最近{self.window_days}天准确率"
                         f"{accuracy:.1%} < {self.accuracy_threshold:.0%}")
            return True
        return False

    def get_stats(self) -> dict:
        """获取监控统计"""
        verified = [p for p in self.predictions if p["verified"]]
        total = len(self.predictions)
        verified_count = len(verified)
        accuracy = self.get_recent_accuracy()

        return {
            "total_predictions": total,
            "verified_count": verified_count,
            "recent_accuracy": round(accuracy, 4),
            "needs_retrain": self.needs_retrain(),
            "threshold": self.accuracy_threshold,
        }

    def _load_history(self):
        """加载历史记录"""
        if os.path.exists(MONITOR_FILE):
            try:
                with open(MONITOR_FILE, "r") as f:
                    data = json.load(f)
                for item in data[-200:]:
                    self.predictions.append(item)
            except Exception:
                pass

    def save(self):
        """保存监控日志"""
        os.makedirs(os.path.dirname(MONITOR_FILE), exist_ok=True)
        with open(MONITOR_FILE, "w") as f:
            json.dump(list(self.predictions), f, ensure_ascii=False)
