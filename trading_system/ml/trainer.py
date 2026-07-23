"""
模型训练管线
============
- XGBoost分类器训练（含交叉验证）
- 滚动窗口训练（120天训练，预测未来5天）
- 模型持久化（joblib）
"""

import logging
import os
import numpy as np
import pandas as pd
from typing import Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 模型保存路径
MODEL_DIR = os.path.join(config.PROJECT_ROOT, "ml", "saved_models")
os.makedirs(MODEL_DIR, exist_ok=True)


class ModelTrainer:
    """
    ML模型训练器
    
    用法:
        trainer = ModelTrainer()
        model = trainer.train(X, y)
        trainer.save(model, "xgb_v1")
    """

    def __init__(self, model_type: str = "xgboost"):
        self.model_type = model_type
        self.model = None
        self.feature_importance = None

    def train(self, X: pd.DataFrame, y: pd.Series,
              cv_folds: int = 5) -> object:
        """
        训练模型（含交叉验证）
        
        参数:
            X: 特征DataFrame
            y: 标签Series (1/-1)
            cv_folds: 交叉验证折数
        
        返回:
            训练好的模型
        """
        if X.empty or y.empty:
            logger.warning("训练数据为空")
            return None

        # 将标签转为0/1（-1→0）
        y_binary = (y == 1).astype(int)

        if self.model_type == "xgboost":
            self.model = self._train_xgboost(X, y_binary, cv_folds)
        else:
            self.model = self._train_sklearn(X, y_binary, cv_folds)

        return self.model

    def _train_xgboost(self, X, y, cv_folds):
        """训练XGBoost"""
        try:
            from xgboost import XGBClassifier
            from sklearn.model_selection import cross_val_score
        except ImportError:
            logger.warning("xgboost未安装，回退到sklearn")
            return self._train_sklearn(X, y, cv_folds)

        model = XGBClassifier(
            n_estimators=100,
            max_depth=5,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=1.0,
            random_state=42,
            use_label_encoder=False,
            eval_metric="logloss",
        )

        # 交叉验证
        try:
            scores = cross_val_score(model, X, y, cv=cv_folds, scoring="accuracy")
            logger.info(f"XGBoost CV准确率: {scores.mean():.3f} ± {scores.std():.3f}")
        except Exception as e:
            logger.warning(f"交叉验证失败: {e}")

        # 全量训练
        model.fit(X, y)

        # 特征重要性
        self.feature_importance = pd.Series(
            model.feature_importances_, index=X.columns
        ).sort_values(ascending=False)

        return model

    def _train_sklearn(self, X, y, cv_folds):
        """训练sklearn模型（备选）"""
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import cross_val_score

        model = GradientBoostingClassifier(
            n_estimators=100, max_depth=5, learning_rate=0.1, random_state=42
        )

        try:
            scores = cross_val_score(model, X, y, cv=cv_folds, scoring="accuracy")
            logger.info(f"GBM CV准确率: {scores.mean():.3f} ± {scores.std():.3f}")
        except Exception:
            pass

        model.fit(X, y)
        self.feature_importance = pd.Series(
            model.feature_importances_, index=X.columns
        ).sort_values(ascending=False)

        return model

    def save(self, model, name: str):
        """保存模型"""
        try:
            import joblib
            path = os.path.join(MODEL_DIR, f"{name}.joblib")
            joblib.dump(model, path)
            logger.info(f"模型已保存: {path}")
        except ImportError:
            import pickle
            path = os.path.join(MODEL_DIR, f"{name}.pkl")
            with open(path, "wb") as f:
                pickle.dump(model, f)
            logger.info(f"模型已保存: {path}")

    def load(self, name: str) -> Optional[object]:
        """加载模型"""
        try:
            import joblib
            path = os.path.join(MODEL_DIR, f"{name}.joblib")
            if os.path.exists(path):
                return joblib.load(path)
        except ImportError:
            import pickle
            path = os.path.join(MODEL_DIR, f"{name}.pkl")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return pickle.load(f)
        return None

    def get_feature_importance(self, top_n: int = 10) -> pd.Series:
        """获取Top N特征重要性"""
        if self.feature_importance is None:
            return pd.Series(dtype=float)
        return self.feature_importance.head(top_n)
