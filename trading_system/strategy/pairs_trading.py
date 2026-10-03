"""
配对交易策略（Pairs Trading）
=============================
基于协整关系的均值回归策略，与趋势策略低相关，震荡市互补。

核心流程:
  1. 配对筛选: 同行业/同赛道股票对，相关性 > 0.8
  2. 协整检验: Engle-Granger 两步法 (ADF检验残差平稳性)
  3. 价差计算: spread = price_A - hedge_ratio * price_B
  4. 信号生成: z-score > 2 做空价差, z-score < -2 做多价差
  5. 平仓规则: |z-score| < 0.5 平仓
  6. 止损规则: |z-score| > 4 强制止损

适用场景:
  - 半导体 ETF vs 芯片 ETF
  - 券商 ETF vs 保险 ETF
  - 同行业龙头 vs 二线标的

使用方式:
    from strategy.pairs_trading import PairsTradingEngine
    engine = PairsTradingEngine()
    pairs = engine.find_pairs(["512480", "159995"], lookback=120)
    signal = engine.generate_signal("512480", "159995")
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional, Tuple, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PairSignal:
    """配对交易信号"""
    code_a: str
    code_b: str
    hedge_ratio: float          # 对冲比率
    spread: float               # 当前价差
    spread_zscore: float        # 价差z-score
    signal: str                 # "long_spread" / "short_spread" / "close" / "none"
    entry_zscore: float = 0.0   # 入场时z-score
    pnl_pct: float = 0.0        # 当前盈亏
    confidence: float = 0.0     # 协整检验p-value


@dataclass
class PairInfo:
    """配对信息"""
    code_a: str
    code_b: str
    sector: str = ""
    correlation: float = 0.0
    coint_pvalue: float = 1.0   # 协整检验p值
    hedge_ratio: float = 1.0
    half_life: float = 0.0      # 均值回归半衰期(天)
    spread_mean: float = 0.0
    spread_std: float = 0.0
    n_observations: int = 0


class PairsTradingEngine:
    """
    配对交易引擎

    参数:
        corr_threshold: 相关性阈值 (默认0.8)
        coint_pvalue_threshold: 协整p值阈值 (默认0.05)
        entry_zscore: 入场z-score阈值 (默认2.0)
        exit_zscore: 平仓z-score阈值 (默认0.5)
        stop_zscore: 止损z-score阈值 (默认4.0)
        lookback: 回看天数 (默认120)
    """

    def __init__(self, corr_threshold: float = 0.8,
                 coint_pvalue_threshold: float = 0.05,
                 entry_zscore: float = 2.0,
                 exit_zscore: float = 0.5,
                 stop_zscore: float = 4.0,
                 lookback: int = 120):
        self.corr_threshold = corr_threshold
        self.coint_pvalue_threshold = coint_pvalue_threshold
        self.entry_zscore = entry_zscore
        self.exit_zscore = exit_zscore
        self.stop_zscore = stop_zscore
        self.lookback = lookback
        self._active_pairs: dict = {}  # {(code_a, code_b): PairInfo}

    def find_pairs(self, codes: list, price_data: dict = None,
                   lookback: int = None) -> List[PairInfo]:
        """
        在候选股票池中寻找协整配对

        参数:
            codes: 股票代码列表
            price_data: {code: DataFrame(close)} 价格数据
                       如果为None则从data_loader加载
            lookback: 回看天数

        返回:
            通过协整检验的配对列表
        """
        lookback = lookback or self.lookback
        pairs = []

        # 加载价格数据
        prices = {}
        for code in codes:
            if price_data and code in price_data:
                prices[code] = price_data[code]
            else:
                try:
                    from data.data_loader import DataLoader
                    dl = DataLoader()
                    df = dl.fetch_stock_daily(code, days=lookback + 30)
                    if df is not None and len(df) >= lookback:
                        prices[code] = df["close"].tail(lookback)
                except Exception as e:
                    logger.debug(f"加载 {code} 价格失败: {e}")

        if len(prices) < 2:
            logger.warning(f"可用价格数据不足: {len(prices)}只")
            return pairs

        # 构建价格矩阵
        price_df = pd.DataFrame(prices)
        valid_codes = price_df.dropna(axis=1).columns.tolist()

        if len(valid_codes) < 2:
            return pairs

        # 两两检验
        for i in range(len(valid_codes)):
            for j in range(i + 1, len(valid_codes)):
                code_a = valid_codes[i]
                code_b = valid_codes[j]
                try:
                    pair = self._test_pair(code_a, code_b,
                                           price_df[code_a], price_df[code_b])
                    if pair is not None:
                        pairs.append(pair)
                except Exception as e:
                    logger.debug(f"配对检验失败 {code_a}-{code_b}: {e}")

        # 按p值排序 (越显著越好)
        pairs.sort(key=lambda p: p.coint_pvalue)
        logger.info(f"配对筛选完成: {len(valid_codes)}只候选 → {len(pairs)}个有效配对")
        return pairs

    def _test_pair(self, code_a: str, code_b: str,
                   price_a: pd.Series, price_b: pd.Series) -> Optional[PairInfo]:
        """检验单个配对"""
        # 对齐
        common = price_a.dropna().index.intersection(price_b.dropna().index)
        if len(common) < 60:
            return None

        pa = price_a.loc[common].values
        pb = price_b.loc[common].values

        # Step 1: 相关性
        corr = np.corrcoef(pa, pb)[0, 1]
        if abs(corr) < self.corr_threshold:
            return None

        # Step 2: 协整检验 (Engle-Granger)
        coint_ok, p_value, hedge_ratio = self._cointegration_test(pa, pb)
        if not coint_ok:
            return None

        # Step 3: 半衰期
        spread = pa - hedge_ratio * pb
        half_life = self._calc_half_life(spread)

        pair = PairInfo(
            code_a=code_a,
            code_b=code_b,
            correlation=float(corr),
            coint_pvalue=float(p_value),
            hedge_ratio=float(hedge_ratio),
            half_life=float(half_life),
            spread_mean=float(np.mean(spread)),
            spread_std=float(np.std(spread)),
            n_observations=len(common),
        )
        return pair

    def _cointegration_test(self, pa: np.ndarray, pb: np.ndarray) -> Tuple[bool, float, float]:
        """
        Engle-Granger 协整检验

        Step 1: OLS回归 pa = alpha + beta * pb + residual
        Step 2: ADF检验 residual 是否平稳
        """
        try:
            from scipy.stats import linregress
            slope, intercept, _, _, _ = linregress(pb, pa)
            residual = pa - (intercept + slope * pb)

            # ADF检验 (简化版: 用残差的一阶差分回归)
            adf_stat = self._adf_statistic(residual)

            # 近似p值 (MacKinnon 1994 临界值近似)
            # 5%临界值约 -2.86, 1%约 -3.43
            if adf_stat < -3.43:
                p_value = 0.01
            elif adf_stat < -2.86:
                p_value = 0.05
            elif adf_stat < -2.57:
                p_value = 0.10
            else:
                p_value = 0.5

            coint_ok = p_value <= self.coint_pvalue_threshold
            return coint_ok, p_value, slope

        except Exception as e:
            logger.debug(f"协整检验失败: {e}")
            return False, 1.0, 1.0

    def _adf_statistic(self, series: np.ndarray) -> float:
        """简化版 ADF 统计量"""
        if len(series) < 10:
            return 0.0
        dy = np.diff(series)
        y_lag = series[:-1]
        # 简单回归: dy = rho * y_{t-1} + error
        x = y_lag[:-1]  # y_{t-1}
        y = dy[1:]       # dy_t
        if len(x) < 5 or np.std(x) < 1e-12:
            return 0.0
        # OLS
        n = len(x)
        rho = np.dot(x, y) / np.dot(x, x)
        residuals = y - rho * x
        se = np.sqrt(np.sum(residuals ** 2) / (n - 1) / np.dot(x, x)) if np.dot(x, x) > 0 else 1e-12
        t_stat = rho / se if se > 1e-12 else 0.0
        return t_stat

    def _calc_half_life(self, spread: np.ndarray) -> float:
        """计算均值回归半衰期"""
        if len(spread) < 10:
            return 0.0
        spread_lag = spread[:-1]
        spread_diff = np.diff(spread)
        mean_spread = np.mean(spread)
        x = spread_lag - mean_spread
        y = spread_diff
        if np.std(x) < 1e-12:
            return 0.0
        theta = np.dot(x, y) / np.dot(x, x)
        if theta >= 0:
            return float("inf")  # 不回归
        half_life = -np.log(2) / theta
        return max(0, half_life)

    def generate_signal(self, code_a: str, code_b: str,
                        price_a: float = None, price_b: float = None,
                        pair_info: PairInfo = None) -> Optional[PairSignal]:
        """
        生成配对交易信号

        参数:
            code_a, code_b: 配对代码
            price_a, price_b: 当前价格 (None则从data_loader获取)
            pair_info: 配对信息 (None则使用缓存)
        """
        if pair_info is None:
            pair_info = self._active_pairs.get((code_a, code_b))
            if pair_info is None:
                logger.warning(f"配对 {code_a}-{code_b} 未注册")
                return None

        # 获取当前价格
        if price_a is None or price_b is None:
            try:
                from data.realtime import fetch_realtime_batch
                rt = fetch_realtime_batch([code_a, code_b])
                if price_a is None:
                    price_a = rt.get(code_a, {}).get("price", 0)
                if price_b is None:
                    price_b = rt.get(code_b, {}).get("price", 0)
            except Exception:
                return None

        if price_a <= 0 or price_b <= 0:
            return None

        # 计算价差 z-score
        spread = price_a - pair_info.hedge_ratio * price_b
        if pair_info.spread_std > 1e-12:
            zscore = (spread - pair_info.spread_mean) / pair_info.spread_std
        else:
            zscore = 0.0

        # 信号判定
        if abs(zscore) >= self.stop_zscore:
            signal = "close"  # 止损
        elif zscore >= self.entry_zscore:
            signal = "short_spread"  # 价差偏高, 做空A做多B
        elif zscore <= -self.entry_zscore:
            signal = "long_spread"   # 价差偏低, 做多A做空B
        elif abs(zscore) <= self.exit_zscore:
            signal = "close"         # 价差回归, 平仓
        else:
            signal = "none"          # 观望

        return PairSignal(
            code_a=code_a,
            code_b=code_b,
            hedge_ratio=pair_info.hedge_ratio,
            spread=round(spread, 4),
            spread_zscore=round(zscore, 3),
            signal=signal,
            confidence=1 - pair_info.coint_pvalue,
        )

    def register_pair(self, pair_info: PairInfo):
        """注册一个配对到活跃列表"""
        key = (pair_info.code_a, pair_info.code_b)
        self._active_pairs[key] = pair_info
        logger.info(f"配对注册: {pair_info.code_a}-{pair_info.code_b} "
                    f"corr={pair_info.correlation:.3f} p={pair_info.coint_pvalue:.4f} "
                    f"半衰期={pair_info.half_life:.0f}天")

    def get_active_pairs(self) -> dict:
        """获取所有活跃配对"""
        return self._active_pairs

    def summary(self) -> str:
        """输出配对状态摘要"""
        if not self._active_pairs:
            return "无活跃配对"
        lines = ["=" * 60, "  配对交易状态", "=" * 60]
        for (ca, cb), info in self._active_pairs.items():
            sig = self.generate_signal(ca, cb)
            sig_str = sig.signal if sig else "N/A"
            z_str = f"{sig.spread_zscore:+.2f}" if sig else "N/A"
            lines.append(f"  {ca}-{cb}: corr={info.correlation:.2f} p={info.coint_pvalue:.3f} "
                        f"z={z_str} signal={sig_str}")
        lines.append("=" * 60)
        return "\n".join(lines)
