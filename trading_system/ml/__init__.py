"""
机器学习信号增强模块
====================
用ML模型对传统信号做二次确认，降低假信号率：
- XGBoost分类：预测未来5日涨跌
- 集成规则：传统信号 + ML概率>0.65 才出最终信号
- 漂移监控：准确率连续10天<55%触发重训练

约束: ML仅做"确认/否决"，不独立产生交易信号
"""

from ml.predictor import MLPredictor
from ml.monitor import ModelMonitor

__all__ = ["MLPredictor", "ModelMonitor"]
