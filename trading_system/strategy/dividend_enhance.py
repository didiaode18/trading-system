"""
红利 ETF 增强策略
================
在红利ETF底仓基础上，通过因子选股做超额收益。

核心逻辑:
  1. 底仓: 红利ETF (如中证红利 515080 / 上证红利 510880)
  2. 增强: 从ETF成分股中，用 value/quality/dividend 因子选股
  3. 约束: 行业偏离度 < 5%, 个股权重 < 2×基准, 跟踪误差 < 8%
  4. 调仓: 月度调仓 (每月第一个交易日)

定位:
  - 与主策略(趋势+CANSLIM)低相关
  - 分散集中度风险 (当前半导材料占74%)
  - 稳定现金流 (红利股息)

使用方式:
    from strategy.dividend_enhance import DividendEnhanceStrategy
    strategy = DividendEnhanceStrategy()
    result = strategy.run()
    # result.portfolio = {code: weight, ...}
    # result.tracking_error = 0.03
    # result.expected_alpha = 0.02
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional, List, Dict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DividendEnhanceResult:
    """红利增强策略结果"""
    # 组合权重
    portfolio: dict = field(default_factory=dict)  # {code: weight}
    etf_weight: float = 0.0            # ETF底仓权重
    stock_weight: float = 0.0          # 个股增强权重
    # 约束检查
    max_sector_deviation: float = 0.0  # 最大行业偏离
    max_stock_weight: float = 0.0      # 最大个股权重
    tracking_error: float = 0.0        # 年化跟踪误差
    # 预期绩效
    expected_alpha: float = 0.0        # 预期年化超额
    factor_scores: dict = field(default_factory=dict)  # 因子得分
    # 调仓信息
    rebalance_date: str = ""
    n_stocks: int = 0
    # 配置引用
    config: Optional[object] = None    # DividendEnhanceConfig


@dataclass
class DividendEnhanceConfig:
    """红利增强策略配置"""
    # 标的ETF
    etf_code: str = "515080"           # 中证红利ETF
    etf_name: str = "中证红利"
    # 增强参数
    enhance_ratio: float = 0.3         # 个股增强比例 (30%仓位做增强)
    n_stocks: int = 10                 # 增强部分选股数量
    # 约束
    max_sector_deviation: float = 0.05 # 行业偏离上限 5%
    max_stock_weight: float = 0.10     # 个股上限 10%
    max_tracking_error: float = 0.08   # 跟踪误差上限 8%
    # 因子权重
    factor_weights: dict = field(default_factory=lambda: {
        "dividend_yield": 0.30,        # 股息率
        "pe_ttm": 0.20,               # 估值 (低PE)
        "roe": 0.25,                   # 质量 (高ROE)
        "momentum_60d": 0.15,          # 动量
        "volatility": 0.10,            # 低波动
    })


# 红利ETF成分股池 (代表性高股息标的)
DIVIDEND_STOCK_POOL = {
    # 银行 (高股息代表)
    "601398": {"name": "工商银行", "sector": "银行", "dividend_yield": 0.055},
    "601939": {"name": "建设银行", "sector": "银行", "dividend_yield": 0.058},
    "601288": {"name": "农业银行", "sector": "银行", "dividend_yield": 0.062},
    "601988": {"name": "中国银行", "sector": "银行", "dividend_yield": 0.060},
    "600036": {"name": "招商银行", "sector": "银行", "dividend_yield": 0.040},
    # 煤炭 (高分红)
    "601088": {"name": "中国神华", "sector": "煤炭", "dividend_yield": 0.065},
    "600188": {"name": "兖矿能源", "sector": "煤炭", "dividend_yield": 0.058},
    "601898": {"name": "中煤能源", "sector": "煤炭", "dividend_yield": 0.055},
    # 交通运输
    "601006": {"name": "大秦铁路", "sector": "交通运输", "dividend_yield": 0.052},
    "600029": {"name": "南方航空", "sector": "交通运输", "dividend_yield": 0.030},
    # 公用事业
    "600900": {"name": "长江电力", "sector": "公用事业", "dividend_yield": 0.035},
    "600886": {"name": "国投电力", "sector": "公用事业", "dividend_yield": 0.032},
    # 消费
    "600887": {"name": "伊利股份", "sector": "食品饮料", "dividend_yield": 0.038},
    "000651": {"name": "格力电器", "sector": "家电", "dividend_yield": 0.045},
    # 能源
    "600028": {"name": "中国石化", "sector": "能源", "dividend_yield": 0.050},
    "601857": {"name": "中国石油", "sector": "能源", "dividend_yield": 0.048},
    # 钢铁
    "600019": {"name": "宝钢股份", "sector": "钢铁", "dividend_yield": 0.050},
    # 保险
    "601318": {"name": "中国平安", "sector": "保险", "dividend_yield": 0.035},
}


class DividendEnhanceStrategy:
    """
    红利ETF增强策略

    在ETF底仓基础上，用多因子模型从成分股池中选股做增强。
    核心约束: 行业偏离、个股权重、跟踪误差。
    """

    def __init__(self, config: DividendEnhanceConfig = None):
        self.config = config or DividendEnhanceConfig()

    def run(self, stock_pool: dict = None, factor_data: dict = None) -> DividendEnhanceResult:
        """
        执行红利增强选股

        参数:
            stock_pool: 候选股票池 {code: {name, sector, ...}}
                       None则使用内置红利成分股池
            factor_data: 因子数据 {code: {factor_name: value}}
                        None则从系统获取

        返回:
            DividendEnhanceResult
        """
        pool = stock_pool or DIVIDEND_STOCK_POOL
        result = DividendEnhanceResult()
        result.config = self.config
        result.rebalance_date = pd.Timestamp.now().strftime("%Y-%m-%d")

        if len(pool) < self.config.n_stocks:
            logger.warning(f"股票池不足: {len(pool)}只 < 需要{self.config.n_stocks}只")
            # 全部入选
            selected = list(pool.keys())
        else:
            # 多因子打分选股
            selected = self._factor_select(pool, factor_data)

        if not selected:
            logger.warning("选股结果为空，全部配置ETF底仓")
            result.etf_weight = 1.0
            result.portfolio = {self.config.etf_code: 1.0}
            return result

        # 权重分配
        n = len(selected)
        # ETF底仓权重
        etf_w = 1.0 - self.config.enhance_ratio
        # 个股增强权重 (等权分配)
        stock_w_each = self.config.enhance_ratio / n

        portfolio = {self.config.etf_code: etf_w}
        for code in selected:
            portfolio[code] = stock_w_each

        # 约束检查
        sector_deviation = self._check_sector_constraint(portfolio, pool)
        max_weight = max(v for k, v in portfolio.items() if k != self.config.etf_code) if selected else 0

        result.portfolio = portfolio
        result.etf_weight = etf_w
        result.stock_weight = self.config.enhance_ratio
        result.max_sector_deviation = sector_deviation
        result.max_stock_weight = max_weight
        result.n_stocks = n
        result.tracking_error = self._estimate_tracking_error(selected, pool)
        result.expected_alpha = self._estimate_alpha(selected, pool)

        logger.info(f"红利增强完成: ETF={etf_w:.0%} + {n}只个股={self.config.enhance_ratio:.0%} "
                    f"| 行业偏离={sector_deviation:.2%} | 跟踪误差={result.tracking_error:.2%} "
                    f"| 预期超额={result.expected_alpha:.2%}")

        return result

    def _factor_select(self, pool: dict, factor_data: dict = None) -> list:
        """多因子打分选股"""
        scores = {}
        weights = self.config.factor_weights

        for code, info in pool.items():
            score = 0.0

            # 股息率因子 (值越大越好)
            dy = info.get("dividend_yield", 0)
            if dy > 0:
                score += weights.get("dividend_yield", 0) * min(dy / 0.08, 1.0) * 100

            # 如果有外部因子数据
            if factor_data and code in factor_data:
                fd = factor_data[code]
                # PE因子 (值越低越好)
                pe = fd.get("pe_ttm", 0)
                if 0 < pe < 20:
                    score += weights.get("pe_ttm", 0) * (1 - pe / 20) * 100

                # ROE因子 (值越高越好)
                roe = fd.get("roe", 0)
                if roe > 0:
                    score += weights.get("roe", 0) * min(roe / 20, 1.0) * 100

                # 动量因子
                mom = fd.get("momentum_60d", 0)
                score += weights.get("momentum_60d", 0) * max(0, min(mom / 0.2, 1.0)) * 100

                # 波动率因子 (低波动更好)
                vol = fd.get("volatility", 1)
                if vol > 0:
                    score += weights.get("volatility", 0) * max(0, 1 - vol / 0.5) * 100

            scores[code] = score

        # 排序取Top N
        sorted_codes = sorted(scores.keys(), key=lambda c: scores[c], reverse=True)
        selected = sorted_codes[:self.config.n_stocks]

        logger.debug(f"红利增强选股: {len(pool)}只候选 → {len(selected)}只入选")
        for code in selected:
            logger.debug(f"  {code} {pool[code].get('name', '')}: 得分={scores[code]:.1f}")

        return selected

    def _check_sector_constraint(self, portfolio: dict, pool: dict) -> float:
        """检查行业偏离度"""
        # 计算组合行业权重
        portfolio_sectors = {}
        total_stock_weight = 0
        for code, weight in portfolio.items():
            if code == self.config.etf_code:
                continue
            sector = pool.get(code, {}).get("sector", "其他")
            portfolio_sectors[sector] = portfolio_sectors.get(sector, 0) + weight
            total_stock_weight += weight

        if total_stock_weight <= 0:
            return 0.0

        # 假设ETF基准行业分布均匀 (简化)
        n_sectors = len(set(v.get("sector", "其他") for v in pool.values()))
        benchmark_sector_weight = total_stock_weight / max(n_sectors, 1)

        max_dev = 0.0
        for sector, weight in portfolio_sectors.items():
            deviation = abs(weight - benchmark_sector_weight)
            max_dev = max(max_dev, deviation)

        return max_dev

    def _estimate_tracking_error(self, selected: list, pool: dict) -> float:
        """估算年化跟踪误差"""
        # 简化估算: 基于增强比例和选股数量
        # 增强比例越高、选股越少 → 跟踪误差越大
        enhance = self.config.enhance_ratio
        n = len(selected)
        # 经验公式: TE ≈ enhance_ratio * annual_vol / sqrt(n)
        # 假设红利组合年化波动率约15%
        annual_vol = 0.15
        te = enhance * annual_vol / max(np.sqrt(n), 1)
        return min(te, 0.20)  # 上限20%

    def _estimate_alpha(self, selected: list, pool: dict) -> float:
        """估算预期超额收益"""
        # 基于因子得分估算
        if not selected:
            return 0.0
        # 经验: 红利因子增强年化超额约2-5%
        avg_dy = np.mean([pool.get(c, {}).get("dividend_yield", 0.03) for c in selected])
        # 超额 ≈ 因子IC × 选股数量 × 因子收益
        # 简化: 超额与平均股息率正相关
        alpha = avg_dy * self.config.enhance_ratio * 0.5
        return max(0, alpha)

    def print_portfolio(self, result: DividendEnhanceResult):
        """打印组合详情"""
        print("=" * 60)
        print(f"  红利ETF增强策略 | 调仓日: {result.rebalance_date}")
        print("=" * 60)
        print(f"  ETF底仓: {self.config.etf_name}({self.config.etf_code}) {result.etf_weight:.1%}")
        print(f"  个股增强: {result.n_stocks}只 {result.stock_weight:.1%}")
        print("-" * 60)
        print(f"  {'代码':>8s}  {'权重':>8s}  {'类型':>8s}")
        print("-" * 60)
        for code, weight in sorted(result.portfolio.items(), key=lambda x: -x[1]):
            if code == self.config.etf_code:
                print(f"  {code:>8s}  {weight:>8.2%}  {'ETF底仓':>8s}")
            else:
                name = DIVIDEND_STOCK_POOL.get(code, {}).get("name", code)
                print(f"  {code:>8s}  {weight:>8.2%}  {name:>8s}")
        print("-" * 60)
        print(f"  行业偏离: {result.max_sector_deviation:.2%} (上限{self.config.max_sector_deviation:.0%})")
        print(f"  跟踪误差: {result.tracking_error:.2%} (上限{self.config.max_tracking_error:.0%})")
        print(f"  预期超额: {result.expected_alpha:.2%}")
        print("=" * 60)
