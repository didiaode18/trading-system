"""
ML推理接口
==========
- 加载训练好的模型
- 对单只股票实时预测涨跌概率
- 集成规则：传统信号 + ML概率>0.65 才确认
"""

import logging
import pandas as pd
import numpy as np
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)


class MLPredictor:
    """
    ML预测器
    
    用法:
        predictor = MLPredictor()
        predictor.load_model("xgb_v1")
        prob = predictor.predict(df)  # 返回上涨概率
        confirmed = predictor.confirm_signal(df, traditional_signal="buy")
    """

    def __init__(self, confirm_threshold: float = 0.65):
        """
        参数:
            confirm_threshold: ML确认阈值（概率>此值才确认信号）
        """
        self.confirm_threshold = confirm_threshold
        self.model = None
        self.model_name = ""

    def load_model(self, name: str) -> bool:
        """加载模型"""
        from ml.trainer import ModelTrainer
        trainer = ModelTrainer()
        self.model = trainer.load(name)
        if self.model is not None:
            self.model_name = name
            logger.info(f"ML模型已加载: {name}")
            return True
        logger.warning(f"ML模型未找到: {name}")
        return False

    def predict(self, df: pd.DataFrame) -> Optional[float]:
        """
        预测上涨概率
        
        参数:
            df: 单只股票的历史数据
        
        返回:
            上涨概率 (0~1)，None表示无法预测
        """
        if self.model is None:
            return None

        from ml.features import build_features
        features = build_features(df)

        if features.empty:
            return None

        # 取最后一行作为当前特征
        current = features.iloc[[-1]]

        try:
            prob = self.model.predict_proba(current)[0]
            # prob[1] = 上涨概率
            return float(prob[1]) if len(prob) > 1 else float(prob[0])
        except Exception as e:
            logger.debug(f"ML预测失败: {e}")
            return None

    def confirm_signal(self, df: pd.DataFrame, traditional_signal: str) -> dict:
        """
        ML确认/否决传统信号
        
        参数:
            df: 股票历史数据
            traditional_signal: "buy" / "sell" / "hold"
        
        返回:
            {
                "confirmed": bool,      # 是否确认
                "ml_prob": float,       # ML上涨概率
                "action": str,          # 最终建议
                "reason": str,          # 原因
            }
        """
        if self.model is None:
            # 无模型时直接通过
            return {
                "confirmed": True,
                "ml_prob": 0.5,
                "action": traditional_signal,
                "reason": "ML模型未加载，直接通过",
            }

        prob = self.predict(df)
        if prob is None:
            return {
                "confirmed": True,
                "ml_prob": 0.5,
                "action": traditional_signal,
                "reason": "ML预测失败，直接通过",
            }

        # 集成规则
        if traditional_signal == "buy":
            if prob >= self.confirm_threshold:
                return {
                    "confirmed": True, "ml_prob": prob,
                    "action": "buy",
                    "reason": f"ML确认买入(概率{prob:.1%})",
                }
            else:
                return {
                    "confirmed": False, "ml_prob": prob,
                    "action": "hold",
                    "reason": f"ML否决买入(概率{prob:.1%}<{self.confirm_threshold:.0%})",
                }

        elif traditional_signal == "sell":
            if prob <= (1 - self.confirm_threshold):
                return {
                    "confirmed": True, "ml_prob": prob,
                    "action": "sell",
                    "reason": f"ML确认卖出(概率{prob:.1%})",
                }
            else:
                return {
                    "confirmed": False, "ml_prob": prob,
                    "action": "hold",
                    "reason": f"ML否决卖出(概率{prob:.1%}，仍有上涨可能)",
                }

        # hold信号不需要ML确认
        return {
            "confirmed": True, "ml_prob": prob,
            "action": "hold",
            "reason": f"观望(ML概率{prob:.1%})",
        }

    @property
    def is_ready(self) -> bool:
        return self.model is not None
