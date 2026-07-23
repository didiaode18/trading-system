"""
Deflated Sharpe Ratio (DSR)
============================
多重检验校正，防止数据挖掘偏差（López de Prado《AFML》第12章）

核心问题:
  如果你测试了100组参数，选出Sharpe最高的那组，
  这个Sharpe很可能是过拟合的产物（多重比较问题）。
  DSR通过统计学校正，回答"这个Sharpe是否显著优于随机？"

公式:
  DSR = P[SR* > E[max(SR)]]
  其中 E[max(SR)] ≈ sqrt(2*log(N)) × std(SR) （N=测试次数）
  
  DSR > 0.95: 策略大概率有效
  DSR < 0.50: 策略可能是数据挖掘产物

使用方式:
    from backtest.deflated_sharpe import DeflatedSharpe
    dsr = DeflatedSharpe()
    result = dsr.evaluate(observed_sharpe, n_trials, returns_series)
"""

import numpy as np
from scipy import stats
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class DeflatedSharpe:
    """Deflated Sharpe Ratio 计算器"""

    def evaluate(self, observed_sharpe: float, n_trials: int,
                 returns: np.ndarray = None, track_record_length: int = None) -> dict:
        """
        计算Deflated Sharpe Ratio
        
        参数:
            observed_sharpe: 观测到的Sharpe Ratio（年化）
            n_trials: 测试了多少组参数/策略（多重检验次数）
            returns: 收益率序列（用于计算偏度和峰度）
            track_record_length: 回测天数（默认从returns推断）
        
        返回:
            {
                "observed_sharpe": float,
                "expected_max_sharpe": float,  # 随机策略预期最大Sharpe
                "dsr": float,                  # Deflated Sharpe Ratio (0~1)
                "p_value": float,              # 原假设p值
                "is_significant": bool,        # 是否显著（DSR>0.95）
                "haircut": float,              # Sharpe折扣率
                "interpretation": str,
            }
        """
        if returns is not None:
            T = len(returns)
            skew = float(stats.skew(returns))
            kurt = float(stats.kurtosis(returns, fisher=True))  # 超额峰度
            sr_std = np.std(returns) / np.mean(returns) if np.mean(returns) != 0 else 1
        else:
            T = track_record_length or 252
            skew = 0.0
            kurt = 0.0
            sr_std = 1.0

        # ---- 1. 计算预期最大Sharpe（Euler-Mascheroni近似）----
        # E[max(SR)] ≈ sqrt(2*log(N)) - (log(π) + log(log(N))) / (2*sqrt(2*log(N)))
        if n_trials <= 1:
            expected_max_sr = 0.0
        else:
            log_n = np.log(n_trials)
            expected_max_sr = (
                np.sqrt(2 * log_n) -
                (np.log(np.pi) + np.log(log_n)) / (2 * np.sqrt(2 * log_n))
            )
            # 调整为年化（假设日度Sharpe × sqrt(252)）
            expected_max_sr *= sr_std  # 缩放到与observed_sharpe同尺度

        # ---- 2. Sharpe的标准误 ----
        # SE(SR) = sqrt((1 + 0.5*SR^2 - skew*SR + (kurt/4)*SR^2) / T)
        sr = observed_sharpe / np.sqrt(252)  # 转为日度
        se_sr = np.sqrt(
            (1 - skew * sr + (kurt / 4) * sr ** 2) / T
        ) if T > 0 else 1.0

        # ---- 3. DSR = Φ((SR - E[max(SR)]) / SE(SR)) ----
        if se_sr > 0:
            test_stat = (sr - expected_max_sr / np.sqrt(252)) / se_sr
            dsr = float(stats.norm.cdf(test_stat))
        else:
            dsr = 0.5

        # p值（原假设：SR <= E[max(SR)]）
        p_value = 1 - dsr

        # Sharpe折扣率
        haircut = 1 - (expected_max_sr / observed_sharpe) if observed_sharpe > 0 else 0
        haircut = max(0, min(1, haircut))

        # 显著性判定
        is_significant = dsr > 0.95

        # 解读
        interpretation = self._interpret(dsr, observed_sharpe, expected_max_sr,
                                         n_trials, haircut, is_significant)

        return {
            "observed_sharpe": round(observed_sharpe, 4),
            "expected_max_sharpe": round(float(expected_max_sr), 4),
            "dsr": round(dsr, 4),
            "p_value": round(p_value, 4),
            "is_significant": is_significant,
            "haircut": round(haircut, 4),
            "n_trials": n_trials,
            "track_record_days": T,
            "skewness": round(skew, 4),
            "kurtosis": round(kurt, 4),
            "interpretation": interpretation,
        }

    def evaluate_multiple(self, sharpe_ratios: list, returns: np.ndarray = None) -> dict:
        """
        评估多组回测结果（自动推断n_trials）
        
        参数:
            sharpe_ratios: 所有测试过的Sharpe列表
            returns: 最佳策略的收益率序列
        
        返回:
            评估结果（n_trials = len(sharpe_ratios)）
        """
        if not sharpe_ratios:
            return {"error": "无Sharpe数据"}

        n_trials = len(sharpe_ratios)
        best_sharpe = max(sharpe_ratios)

        result = self.evaluate(best_sharpe, n_trials, returns)
        result["all_sharpes"] = sorted(sharpe_ratios, reverse=True)
        result["best_rank"] = 1
        result["selection_bias_warning"] = (
            f"从{n_trials}组中选出最佳Sharpe={best_sharpe:.2f}，"
            f"存在选择偏差风险"
        ) if n_trials > 5 else ""

        return result

    def _interpret(self, dsr, observed_sr, expected_sr, n_trials,
                   haircut, is_significant) -> str:
        """生成解读"""
        if is_significant:
            return (
                f"✓ DSR={dsr:.2f}>0.95，策略Sharpe({observed_sr:.2f})显著优于"
                f"{n_trials}次随机测试的预期最大值({expected_sr:.2f})，"
                f"策略大概率有效"
            )
        elif dsr > 0.80:
            return (
                f"△ DSR={dsr:.2f}，策略可能有效但证据不够强，"
                f"建议增加样本外验证"
            )
        elif dsr > 0.50:
            return (
                f"⚠️ DSR={dsr:.2f}，策略Sharpe({observed_sr:.2f})与随机水平"
                f"({expected_sr:.2f})接近，可能是过拟合，"
                f"Sharpe折扣率{haircut:.0%}"
            )
        else:
            return (
                f"✗ DSR={dsr:.2f}<0.50，策略大概率是数据挖掘产物，"
                f"在{n_trials}次测试中选出最佳只是运气，"
                f"不建议实盘使用"
            )
