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
from datetime import datetime, timedelta
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
        self.is_degraded = False  # 整体模型是否降级
        self._load_history()

    # FIX: 修复准确率判定与三分类模型不匹配的问题，使用predicted_class替代prob>0.5
    @staticmethod
    def _is_correct(p: dict) -> bool:
        """判断单条预测是否正确（兼容三分类predicted_class和二分类prob）"""
        actual = p.get("actual_return", 0)
        if "predicted_class" in p:
            predicted = p["predicted_class"]
            # 三分类: predicted_class ∈ {-1, 0, 1}
            if predicted == 1:
                return actual > 0
            elif predicted == -1:
                return actual < 0
            else:
                return actual == 0 or (abs(actual) < 0.01)
        # 向后兼容: 旧记录无predicted_class，退化为二值判定
        return (p.get("prob", 0.5) > 0.5) == (actual > 0)

    def record_prediction(self, code: str, date: str, prob: float,
                          actual_return: float = None,
                          predicted_class: int = None):
        """记录一次预测

        参数:
            predicted_class: 三分类预测类别 {-1: 下跌, 0: 震荡, 1: 上涨}，
                             为None时从prob推导（向后兼容）
        """
        if predicted_class is None:
            # 向后兼容：从概率推导粗略类别
            if prob >= 0.5:
                predicted_class = 1
            elif prob < 0.3:
                predicted_class = -1
            else:
                predicted_class = 0
        self.predictions.append({
            "code": code,
            "date": date,
            "prob": prob,
            "predicted_class": predicted_class,
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
        # 每次验证后检查模型是否需要降级
        self._auto_disable_check()

    def get_recent_accuracy(self) -> float:
        """计算最近N天的预测准确率"""
        verified = [p for p in self.predictions if p["verified"]]
        if len(verified) < 5:
            return 0.5  # 样本不足，返回中性值

        recent = verified[-self.window_days * 3:]  # 取足够多的样本
        correct = 0
        total = 0
        for p in recent:
            if self._is_correct(p):
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
        correct = sum(1 for p in recent if self._is_correct(p))
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
        """加载历史记录（兼容旧版列表格式和新版字典格式）"""
        if os.path.exists(MONITOR_FILE):
            try:
                with open(MONITOR_FILE, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # 新格式: {'predictions': [...], 'is_degraded': bool}
                    self.is_degraded = data.get('is_degraded', False)
                    items = data.get('predictions', [])
                else:
                    # 旧格式: [...]
                    items = data
                for item in items[-200:]:
                    self.predictions.append(item)
            except Exception:
                pass

    def _auto_disable_check(self):
        """连续10天准确率<55%时标记降级，<45%时立即降级"""
        verified = [p for p in self.predictions if p.get('verified')]
        if len(verified) < 10:
            return
        recent_10 = list(verified)[-10:]
        correct = sum(
            1 for p in recent_10 if self._is_correct(p)
        )
        accuracy = correct / len(recent_10)
        if accuracy < 0.45:
            if not self.is_degraded:
                self.is_degraded = True
                logger.warning(f"ML模型准确率 {accuracy:.1%} < 45%，已自动降级禁用")
        elif accuracy < 0.55:
            if not self.is_degraded:
                self.is_degraded = True
                logger.warning(f"连续10天准确率 {accuracy:.1%} < 55%，标记降级预警")
        else:
            if self.is_degraded:
                self.is_degraded = False
                logger.info(f"ML模型准确率恢复至 {accuracy:.1%}，解除降级")

    def get_accuracy_report(self, days: int = 30) -> dict:
        """最近 N 天的准确率统计报告。

        Returns:
            dict: {
                'total': int,           # 已验证预测总数
                'correct': int,         # 正确预测数
                'accuracy': float,      # 准确率 0~1
                'recent_trend': list,   # 最近7天每日准确率
                'is_degraded': bool,    # 是否处于低精度状态
            }
        """
        cutoff = datetime.now() - timedelta(days=days)
        verified = []
        for p in self.predictions:
            if p.get('verified'):
                try:
                    pred_date = datetime.strptime(p['date'], '%Y-%m-%d')
                    if pred_date >= cutoff:
                        verified.append(p)
                except (ValueError, KeyError):
                    continue

        total = len(verified)
        if total == 0:
            return {
                'total': 0, 'correct': 0, 'accuracy': 0.0,
                'recent_trend': [],
                'is_degraded': self.is_degraded,
            }

        correct = sum(
            1 for p in verified if self._is_correct(p)
        )

        # 最近7天每日准确率
        recent_trend = []
        for i in range(7):
            day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            day_preds = [p for p in verified if p.get('date') == day]
            if day_preds:
                dc = sum(
                    1 for p in day_preds if self._is_correct(p)
                )
                recent_trend.append({
                    'date': day,
                    'accuracy': dc / len(day_preds),
                    'count': len(day_preds),
                })

        return {
            'total': total,
            'correct': correct,
            'accuracy': correct / total if total > 0 else 0.0,
            'recent_trend': recent_trend,
            'is_degraded': self.is_degraded,
        }

    def save(self):
        """保存监控日志"""
        os.makedirs(os.path.dirname(MONITOR_FILE), exist_ok=True)
        with open(MONITOR_FILE, "w") as f:
            json.dump({
                'predictions': list(self.predictions),
                'is_degraded': self.is_degraded,
            }, f, ensure_ascii=False)
