"""
模型训练管线
============
- XGBoost分类器训练（含时序交叉验证）
- 滚动窗口训练（120天训练，预测未来5天）
- 模型持久化（joblib）

【修复说明】
- 使用 TimeSeriesSplit 替代 KFold，避免金融时序未来数据泄露
- 使用 f1_macro 替代 accuracy，适配涨跌不平衡的三分类场景(1/0/-1)
- 添加类别权重 + 时间衰减权重 处理类别不平衡和样本时效性
- 训练后输出 classification_report 详细评估
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
              cv_folds: int = 5,
              temporal_weights: Optional[np.ndarray] = None) -> object:
        """
        训练模型（含时序交叉验证）
        
        参数:
            X: 特征DataFrame
            y: 标签Series (1/0/-1 三分类: 涨/震荡/跌)
            cv_folds: 交叉验证折数
            temporal_weights: 时间衰减权重数组，由 prepare_dataset 计算
                              近期样本权重更高，远期样本权重更低
        
        返回:
            训练好的模型
        """
        if X.empty or y.empty:
            logger.warning("训练数据为空")
            return None

        # 【修复】保留三分类标签(1/0/-1)，不再转为二分类
        # 震荡期(label=0)是重要的"不操作"信号，模型需要学习
        y_train = y.copy()

        if self.model_type == "xgboost":
            self.model = self._train_xgboost(X, y_train, cv_folds, temporal_weights)
        else:
            self.model = self._train_sklearn(X, y_train, cv_folds, temporal_weights)

        return self.model

    def _compute_combined_weights(self, y, temporal_weights):
        """
        【新增】计算类别平衡权重 × 时间衰减权重的组合权重
        
        金融ML中两类样本不平衡问题需要同时处理：
        1. 类别不平衡：涨/跌/震荡样本数量差异大 → class_weight
        2. 样本时效性：近期样本比远期样本更有预测价值 → temporal_weight
        
        最终权重 = class_weight × temporal_weight（逐元素相乘）
        """
        # 类别权重：样本少的类别权重大
        class_counts = y.value_counts()
        max_count = class_counts.max()
        class_weight_map = {cls: max_count / cnt for cls, cnt in class_counts.items()}
        class_weights = y.map(class_weight_map).values.astype(float)

        # 时间权重：默认为全1（无衰减）
        if temporal_weights is not None and len(temporal_weights) == len(y):
            time_weights = np.asarray(temporal_weights, dtype=float)
        else:
            time_weights = np.ones(len(y), dtype=float)

        # 组合权重 = 类别权重 × 时间权重
        combined = class_weights * time_weights
        return combined

    def _train_xgboost(self, X, y, cv_folds, temporal_weights=None):
        """训练XGBoost"""
        try:
            from xgboost import XGBClassifier
            from sklearn.model_selection import cross_val_score, TimeSeriesSplit
        except ImportError:
            logger.warning("xgboost未安装，回退到sklearn")
            return self._train_sklearn(X, y, cv_folds, temporal_weights)

        n_classes = len(y.unique())
        # 计算组合权重（类别平衡 × 时间衰减）
        sample_weights = self._compute_combined_weights(y, temporal_weights)

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
            eval_metric="mlogloss" if n_classes > 2 else "logloss",
        )

        # 【修复】使用 TimeSeriesSplit 替代 KFold，防止未来数据泄露
        # 金融时序必须用时序分割：只用过去数据预测未来，不能反过来
        tscv = TimeSeriesSplit(n_splits=cv_folds)

        # 【修复】使用 f1_macro 替代 accuracy，对不平衡三分类更合理
        try:
            scores = cross_val_score(model, X, y, cv=tscv, scoring="f1_macro")
            logger.info(f"XGBoost CV F1-macro: {scores.mean():.3f} ± {scores.std():.3f}")
        except Exception as e:
            logger.warning(f"交叉验证失败: {e}")

        # 全量训练（带组合权重：类别平衡 × 时间衰减）
        model.fit(X, y, sample_weight=sample_weights)

        # 特征重要性
        self.feature_importance = pd.Series(
            model.feature_importances_, index=X.columns
        ).sort_values(ascending=False)

        # 【增强】输出详细分类评估报告
        self._log_classification_report(model, X, y)

        return model

    def _train_sklearn(self, X, y, cv_folds, temporal_weights=None):
        """训练sklearn模型（备选）"""
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import cross_val_score, TimeSeriesSplit

        # 计算组合权重（类别平衡 × 时间衰减）
        sample_weights = self._compute_combined_weights(y, temporal_weights)

        model = GradientBoostingClassifier(
            n_estimators=100, max_depth=5, learning_rate=0.1, random_state=42
        )

        # 【修复】使用 TimeSeriesSplit + f1_macro
        tscv = TimeSeriesSplit(n_splits=cv_folds)

        try:
            scores = cross_val_score(model, X, y, cv=tscv, scoring="f1_macro")
            logger.info(f"GBM CV F1-macro: {scores.mean():.3f} ± {scores.std():.3f}")
        except Exception:
            pass

        # 带组合权重的全量训练
        model.fit(X, y, sample_weight=sample_weights)
        self.feature_importance = pd.Series(
            model.feature_importances_, index=X.columns
        ).sort_values(ascending=False)

        # 【增强】输出详细分类评估报告
        self._log_classification_report(model, X, y)

        return model

    def _log_classification_report(self, model, X, y):
        """
        【新增】输出详细的分类评估报告
        
        对三分类(1=涨, 0=震荡, -1=跌)输出 precision/recall/f1，
        帮助诊断模型对各类别的学习效果。
        """
        try:
            from sklearn.metrics import classification_report
            y_pred = model.predict(X)
            labels_present = sorted(y.unique())
            label_name_map = {1: "涨(1)", 0: "震荡(0)", -1: "跌(-1)"}
            target_names = [label_name_map.get(lbl, str(lbl)) for lbl in labels_present]

            report = classification_report(
                y, y_pred,
                labels=labels_present,
                target_names=target_names,
                zero_division=0
            )
            logger.info(f"分类报告:\n{report}")
        except Exception as e:
            logger.warning(f"生成分类报告失败: {e}")

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
