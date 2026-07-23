"""
因子注册表
==========
统一管理所有因子的注册、计算、查询：
- 自动发现并注册所有因子计算函数
- 统一接口：输入DataFrame → 输出因子值Series
- 支持因子分类（技术/量价/动量/质量/情绪）
- 支持因子权重动态调整
"""

import logging
import pandas as pd
import numpy as np
from typing import Callable, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class FactorMeta:
    """因子元数据"""
    name: str                    # 因子名称
    category: str                # 分类: technical/volume/momentum/quality/sentiment
    func: Callable               # 计算函数 (df) -> Series
    description: str = ""        # 描述
    direction: int = 1           # 方向: 1=越大越好, -1=越小越好
    weight: float = 1.0          # 当前权重
    ic_history: list = field(default_factory=list)  # IC历史
    enabled: bool = True         # 是否启用


class FactorRegistry:
    """
    因子注册表
    
    用法:
        registry = FactorRegistry()
        registry.register("ma20_bias", "technical", calc_ma20_bias)
        factors = registry.compute_all(df)
    """

    def __init__(self):
        self.factors: dict[str, FactorMeta] = {}
        self._auto_register()

    def register(self, name: str, category: str, func: Callable,
                 description: str = "", direction: int = 1, weight: float = 1.0):
        """注册一个因子"""
        self.factors[name] = FactorMeta(
            name=name, category=category, func=func,
            description=description, direction=direction, weight=weight,
        )

    def unregister(self, name: str):
        """注销因子"""
        if name in self.factors:
            del self.factors[name]

    def get(self, name: str) -> Optional[FactorMeta]:
        """获取因子元数据"""
        return self.factors.get(name)

    def list_by_category(self, category: str) -> list:
        """按分类列出因子"""
        return [f for f in self.factors.values() if f.category == category]

    def compute(self, name: str, df: pd.DataFrame) -> Optional[pd.Series]:
        """计算单个因子"""
        meta = self.factors.get(name)
        if meta is None or not meta.enabled:
            return None
        try:
            return meta.func(df)
        except Exception as e:
            logger.debug(f"因子 {name} 计算失败: {e}")
            return None

    def compute_all(self, df: pd.DataFrame, categories: list = None) -> pd.DataFrame:
        """
        计算所有因子
        
        参数:
            df: 含 date/open/close/high/low/volume 列的DataFrame
            categories: 限定分类（None=全部）
        
        返回:
            DataFrame，每列一个因子
        """
        results = {}
        for name, meta in self.factors.items():
            if not meta.enabled:
                continue
            if categories and meta.category not in categories:
                continue
            try:
                values = meta.func(df)
                if values is not None and len(values) == len(df):
                    results[name] = values
            except Exception as e:
                logger.debug(f"因子 {name} 计算异常: {e}")

        if not results:
            return pd.DataFrame(index=df.index)

        factor_df = pd.DataFrame(results, index=df.index)
        return factor_df

    def get_top_factors(self, n: int = 20) -> list:
        """获取IC最高的Top N因子"""
        scored = []
        for name, meta in self.factors.items():
            if meta.ic_history:
                avg_ic = np.mean(meta.ic_history[-20:])  # 最近20期
                scored.append((name, abs(avg_ic)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in scored[:n]]

    def update_weights_by_ic(self):
        """根据IC动态调整权重"""
        for meta in self.factors.values():
            if len(meta.ic_history) >= 5:
                recent_ic = np.mean(meta.ic_history[-5:])
                if abs(recent_ic) < 0.02:
                    meta.weight = 0.3  # IC衰减，降权
                elif abs(recent_ic) > 0.05:
                    meta.weight = 1.5  # IC强劲，加权
                else:
                    meta.weight = 1.0

    def _auto_register(self):
        """自动注册所有内置因子"""
        from factors.technical import TECHNICAL_FACTORS
        from factors.volume import VOLUME_FACTORS
        from factors.momentum import MOMENTUM_FACTORS

        for name, info in TECHNICAL_FACTORS.items():
            self.register(name, "technical", info["func"],
                         info.get("desc", ""), info.get("dir", 1))

        for name, info in VOLUME_FACTORS.items():
            self.register(name, "volume", info["func"],
                         info.get("desc", ""), info.get("dir", 1))

        for name, info in MOMENTUM_FACTORS.items():
            self.register(name, "momentum", info["func"],
                         info.get("desc", ""), info.get("dir", 1))

        logger.info(f"因子注册表: 共{len(self.factors)}个因子 "
                   f"(技术{len(TECHNICAL_FACTORS)} + 量价{len(VOLUME_FACTORS)} + "
                   f"动量{len(MOMENTUM_FACTORS)})")

    @property
    def count(self) -> int:
        return len(self.factors)

    @property
    def enabled_count(self) -> int:
        return sum(1 for f in self.factors.values() if f.enabled)


# ============================================================
# 便捷接口
# ============================================================

_GLOBAL_REGISTRY = None

def get_registry() -> FactorRegistry:
    """获取全局因子注册表（单例）"""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = FactorRegistry()
    return _GLOBAL_REGISTRY


def compute_all_factors(df: pd.DataFrame, categories: list = None) -> pd.DataFrame:
    """便捷接口：计算所有因子"""
    return get_registry().compute_all(df, categories)
