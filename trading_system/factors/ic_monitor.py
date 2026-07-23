"""
IC监控模块
==========
因子有效性监控：
- IC（信息系数）: 因子值与未来收益的秩相关
- IR（信息比率）: IC均值/IC标准差
- 衰减预警: 连续N天IC<阈值 → 自动降权
- 分层回测: 按因子值分5组验证单调性
"""

import logging
import pandas as pd
import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)


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

    def __init__(self, decay_threshold: float = 0.02, decay_days: int = 5):
        """
        参数:
            decay_threshold: IC衰减阈值（|IC|<此值视为无效）
            decay_days: 连续多少天低于阈值触发预警
        """
        self.decay_threshold = decay_threshold
        self.decay_days = decay_days
        self.ic_records: dict[str, list] = {}  # {factor_name: [ic_values]}

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

    def update(self, factor_name: str, ic_value: float):
        """更新因子IC记录"""
        if factor_name not in self.ic_records:
            self.ic_records[factor_name] = []
        self.ic_records[factor_name].append(ic_value)

    def get_ic_stats(self, factor_name: str) -> dict:
        """获取因子IC统计"""
        records = self.ic_records.get(factor_name, [])
        if not records:
            return {"ic_mean": 0, "ic_std": 0, "ir": 0, "ic_positive_ratio": 0}

        arr = np.array(records)
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
        """判断因子是否IC衰减"""
        records = self.ic_records.get(factor_name, [])
        if len(records) < self.decay_days:
            return False
        recent = records[-self.decay_days:]
        return all(abs(ic) < self.decay_threshold for ic in recent)

    def get_decaying_factors(self) -> list:
        """获取所有衰减因子"""
        return [name for name in self.ic_records if self.is_decaying(name)]

    def rank_factors(self) -> list:
        """按IR排序所有因子"""
        ranked = []
        for name in self.ic_records:
            stats_dict = self.get_ic_stats(name)
            ranked.append((name, stats_dict["ir"], stats_dict["ic_mean"]))
        ranked.sort(key=lambda x: abs(x[1]), reverse=True)
        return ranked

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
