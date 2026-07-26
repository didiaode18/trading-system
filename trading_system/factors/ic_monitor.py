"""
IC监控模块
==========
因子有效性监控：
- IC（信息系数）: 因子值与未来收益的秩相关
- IR（信息比率）: IC均值/IC标准差
- 衰减预警: 连续N天IC<阈值 → 自动降权
- 分层回测: 按因子值分5组验证单调性
"""

import os
import json
import logging
import pandas as pd
import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)

# 默认持久化路径
DEFAULT_IC_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'ic_history.json'
)


class ICMonitor:
    """
    因子IC监控器
    
    用法:
        monitor = ICMonitor()
        ic = monitor.calc_ic(factor_values, forward_returns)
        monitor.update(factor_name, ic)
        if monitor.is_decaying(factor_name):
            print(f"{factor_name} IC衰减，建议降权")
    """

    def __init__(self, decay_threshold: float = 0.02, decay_days: int = 5,
                 history_path: str = None):
        """
        参数:
            decay_threshold: IC衰减阈值（|IC|<此值视为无效）
            decay_days: 连续多少天低于阈值触发预警
            history_path: IC历史JSON持久化路径（None=默认路径）
        """
        self.decay_threshold = decay_threshold
        self.decay_days = decay_days
        self.history_path = history_path or DEFAULT_IC_HISTORY_PATH
        # ic_records: {factor_name: [{"date": "2026-07-24", "ic": 0.05, "ir": 1.2}, ...]}
        self.ic_records: dict[str, list] = {}
        # 初始化时自动加载历史数据
        self.load()

    def calc_ic(self, factor_values: pd.Series, forward_returns: pd.Series) -> float:
        """
        计算单期IC（Spearman秩相关）
        
        参数:
            factor_values: 因子值（截面数据，多只股票同一时点）
            forward_returns: 未来N日收益率
        
        返回:
            IC值 (-1 ~ 1)
        """
        # 去除NaN
        valid = factor_values.notna() & forward_returns.notna()
        if valid.sum() < 5:
            return 0.0

        f = factor_values[valid]
        r = forward_returns[valid]

        # Spearman秩相关
        ic, _ = stats.spearmanr(f, r)
        return ic if not np.isnan(ic) else 0.0

    def calc_ic_series(self, factor_df: pd.DataFrame, returns_df: pd.DataFrame,
                       factor_name: str) -> pd.Series:
        """
        计算时间序列IC（逐日截面IC）
        
        参数:
            factor_df: 因子面板数据 (index=date, columns=stocks)
            returns_df: 收益率面板数据
            factor_name: 因子名
        
        返回:
            IC时间序列
        """
        ic_series = []
        dates = factor_df.index.intersection(returns_df.index)

        for date in dates:
            f = factor_df.loc[date]
            r = returns_df.loc[date]
            ic = self.calc_ic(f, r)
            ic_series.append({"date": date, "ic": ic})

        return pd.DataFrame(ic_series).set_index("date")["ic"]

    def update(self, factor_name: str, ic_value: float, date: str = None):
        """更新因子IC记录并自动持久化
        
        参数:
            factor_name: 因子名称
            ic_value: 当期IC值
            date: 日期字符串(YYYY-MM-DD)，默认当天
        """
        if factor_name not in self.ic_records:
            self.ic_records[factor_name] = []
        if date is None:
            import datetime
            date = datetime.date.today().strftime('%Y-%m-%d')
        # 计算当前IR（滚动）
        records = self.ic_records[factor_name]
        all_ics = [r['ic'] if isinstance(r, dict) else r for r in records] + [ic_value]
        ir = self._calc_ir(all_ics)
        self.ic_records[factor_name].append({
            'date': date, 'ic': round(ic_value, 6), 'ir': round(ir, 4)
        })
        # 自动持久化
        self.save()

    @staticmethod
    def _calc_ir(ic_list: list) -> float:
        """计算IR = IC均值/IC标准差"""
        if len(ic_list) < 2:
            return 0.0
        arr = np.array(ic_list, dtype=float)
        std = arr.std()
        return float(arr.mean() / std) if std > 0 else 0.0

    def get_ic_stats(self, factor_name: str) -> dict:
        """获取因子IC统计"""
        records = self.ic_records.get(factor_name, [])
        if not records:
            return {"ic_mean": 0, "ic_std": 0, "ir": 0, "ic_positive_ratio": 0}

        arr = np.array([r['ic'] if isinstance(r, dict) else r for r in records])
        ic_mean = arr.mean()
        ic_std = arr.std()
        ir = ic_mean / ic_std if ic_std > 0 else 0
        positive_ratio = (arr > 0).sum() / len(arr)

        return {
            "ic_mean": round(ic_mean, 4),
            "ic_std": round(ic_std, 4),
            "ir": round(ir, 4),
            "ic_positive_ratio": round(positive_ratio, 4),
            "sample_size": len(records),
        }

    def is_decaying(self, factor_name: str) -> bool:
        """判断因子是否IC衰减（连续N天|IC|<阈值）"""
        records = self.ic_records.get(factor_name, [])
        if len(records) < self.decay_days:
            return False
        recent = records[-self.decay_days:]
        return all(
            abs(r['ic'] if isinstance(r, dict) else r) < self.decay_threshold
            for r in recent
        )

    def is_strong(self, factor_name: str, threshold: float = 0.05) -> bool:
        """判断因子是否IC强劲（连续N天|IC|>阈值）"""
        records = self.ic_records.get(factor_name, [])
        if len(records) < self.decay_days:
            return False
        recent = records[-self.decay_days:]
        return all(
            abs(r['ic'] if isinstance(r, dict) else r) > threshold
            for r in recent
        )

    def get_decaying_factors(self) -> list:
        """获取所有衰减因子"""
        return [name for name in self.ic_records if self.is_decaying(name)]

    def get_strong_factors(self, threshold: float = 0.05) -> list:
        """获取所有强劲因子"""
        return [name for name in self.ic_records if self.is_strong(name, threshold)]

    def rank_factors(self) -> list:
        """按IR排序所有因子"""
        ranked = []
        for name in self.ic_records:
            stats_dict = self.get_ic_stats(name)
            ranked.append((name, stats_dict["ir"], stats_dict["ic_mean"]))
        ranked.sort(key=lambda x: abs(x[1]), reverse=True)
        return ranked

    # ============================================================
    # 持久化
    # ============================================================

    def save(self, path: str = None):
        """将IC历史序列化为JSON文件"""
        save_path = path or self.history_path
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(self.ic_records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"IC历史保存失败({save_path}): {e}")

    def load(self, path: str = None):
        """从JSON文件恢复IC历史"""
        load_path = path or self.history_path
        try:
            if os.path.exists(load_path):
                with open(load_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.ic_records = data
                    logger.info(f"IC历史已加载: {len(data)}个因子, "
                               f"共{sum(len(v) for v in data.values())}条记录")
                else:
                    logger.warning(f"IC历史文件格式异常，已忽略")
        except Exception as e:
            logger.warning(f"IC历史加载失败({load_path}): {e}")
            self.ic_records = {}

    def layer_backtest(self, factor_values: pd.Series, forward_returns: pd.Series,
                       n_layers: int = 5) -> dict:
        """
        分层回测：按因子值分N组，验证单调性
        
        返回:
            {"layer_returns": [各层平均收益], "monotonicity": 单调性评分}
        """
        valid = factor_values.notna() & forward_returns.notna()
        if valid.sum() < n_layers * 3:
            return {"layer_returns": [], "monotonicity": 0}

        f = factor_values[valid]
        r = forward_returns[valid]

        # 分层
        labels = pd.qcut(f, n_layers, labels=False, duplicates="drop")
        layer_returns = []
        for i in range(n_layers):
            mask = labels == i
            if mask.sum() > 0:
                layer_returns.append(r[mask].mean())
            else:
                layer_returns.append(0)

        # 单调性：相邻层收益差的方向一致性
        diffs = np.diff(layer_returns)
        if len(diffs) > 0:
            positive_ratio = (diffs > 0).sum() / len(diffs)
            monotonicity = max(positive_ratio, 1 - positive_ratio)  # 0.5~1
        else:
            monotonicity = 0.5

        return {
            "layer_returns": [round(r, 4) for r in layer_returns],
            "monotonicity": round(monotonicity, 4),
            "top_minus_bottom": round(layer_returns[-1] - layer_returns[0], 4),
        }
