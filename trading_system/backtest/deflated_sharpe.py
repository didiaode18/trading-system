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
import config

logger = logging.getLogger(__name__)


class DeflatedSharpe:
    """Deflated Sharpe Ratio 计算器"""

    def evaluate(self, observed_sharpe: float, n_trials: int,
                 returns: np.ndarray = None, track_record_length: int = None,
                 sr_std: float = None) -> dict:
        """
        计算Deflated Sharpe Ratio
        
        参数:
            observed_sharpe: 观测到的Sharpe Ratio（年化）
            n_trials: 测试了多少组参数/策略（多重检验次数）
            returns: 收益率序列（用于计算偏度和峰度）
            track_record_length: 回测天数（默认从returns推断）
            sr_std: 年化夏普跨试验标准差（Bailey & López de Prado）。
                    默认0.5（经验值），保守压测可取1.0。
                    V10.5修复: 旧版从价格波动推导sr_std口径完全错误，
                    导致N=75时expected_max_sharpe=24.95（荒谬值）。
        
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
        # V10.5 BUG-06修复: sr_std 使用跨试验标准差（默认0.5），
        # 不再从单条收益序列的价格波动推导（口径完全错误）。
        if sr_std is None:
            sr_std = getattr(config, 'DSR_SR_STD', 0.5)

        if returns is not None:
            T = len(returns)
            skew = float(stats.skew(returns))
            kurt = float(stats.kurtosis(returns, fisher=True))  # 超额峰度
        else:
            T = track_record_length or 252
            skew = 0.0
            kurt = 0.0

        # ---- 1. 计算预期最大Sharpe（Euler-Mascheroni近似）----
        # E[max(SR)] ≈ E[max Z] × σ_SR
        # E[max Z] 是 N 个标准正态的极值期望（N=75 时约 2.43）
        if n_trials <= 1:
            expected_max_sr = 0.0
        else:
            log_n = np.log(n_trials)
            # E[max Z] 的极值期望近似
            emax_z = (
                np.sqrt(2 * log_n) -
                (np.log(np.pi) + np.log(log_n)) / (2 * np.sqrt(2 * log_n))
            )
            # V10.5: 直接乘以跨试验标准差 σ_SR（年化尺度）
            expected_max_sr = emax_z * sr_std

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

        # V10.5: Sharpe折扣率，observed≤0时haircut置1（无折扣意义）
        if observed_sharpe > 0 and expected_max_sr > 0:
            haircut = 1 - (expected_max_sr / observed_sharpe)
        else:
            haircut = 1.0
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

    def compute_pbo(self, returns_matrix, n_splits: int = None) -> dict:
        """P2-2(路线图): PBO 门面方法，转发到模块级 compute_pbo。"""
        return compute_pbo(returns_matrix, n_splits=n_splits)


# ============================================================
# P2-2(路线图): PBO — 组合对称交叉验证（CSCV）
# ============================================================
def compute_pbo(returns_matrix, n_splits: int = None) -> dict:
    """过拟合概率 PBO（Probability of Backtest Overfitting），CSCV 算法。

    López de Prado《AFML》第11章：把 T 期收益切成 S 个等长块（S 偶数），
    对每一种 "S/2 块作样本内(IS) + 其余作样本外(OS)" 的组合：
      1) 在 IS 上选出累计表现最好的配置（模拟"回测选参"）；
      2) 看该配置在 OS 上的相对排名 ω = rank/(N+1)（rank 为升序名次，1=最差）；
      3) 若 ω <= 0.5（IS 最优在 OS 落到后半段）→ 记为一次"过拟合命中"。
    PBO = 命中次数 / 组合总数。PBO>0.5 说明"回测最优"在样本外多半垫底，过拟合。

    参数:
        returns_matrix: 形如 (T, N) 的二维收益矩阵——T=时间期数(行), N=配置/trial 数(列)。
                        每列是一个参数配置在同一时间轴上的逐期收益。
        n_splits: 块数 S（偶数）。默认按 T 自适应（<=16 的最大偶数，且每块>=2 期）。

    返回:
        {status, pbo, n_splits, n_configs, n_periods, n_combinations,
         lambda_mean, interpretation}
        status ∈ {"ok", "insufficient"}
    """
    from itertools import combinations

    M = np.asarray(returns_matrix, dtype=float)
    if M.ndim != 2:
        return {"status": "insufficient", "reason": "returns_matrix 必须是二维 (T,N)",
                "pbo": None}
    T, N = M.shape
    if N < 2:
        return {"status": "insufficient", "reason": f"配置数不足(N={N}<2)，无法计算 PBO",
                "pbo": None, "n_configs": int(N)}

    # 自适应块数：<=16 的最大偶数，且保证每块>=2 期、组合数可控
    if n_splits is None:
        n_splits = min(16, T // 2)
        n_splits -= n_splits % 2  # 取偶
    n_splits = int(n_splits)
    n_splits -= n_splits % 2
    if n_splits < 4:
        return {"status": "insufficient",
                "reason": f"期数过短(T={T})，块数不足(S={n_splits}<4)",
                "pbo": None, "n_configs": int(N), "n_periods": int(T)}

    block = T // n_splits
    if block < 1:
        return {"status": "insufficient", "reason": "每块期数<1",
                "pbo": None, "n_configs": int(N)}
    use_rows = block * n_splits
    # 每块累计收益（对数近似：块内简单求和，避免 cumprod 溢出）
    Mtrim = np.nan_to_num(M[:use_rows, :], nan=0.0)
    block_perf = Mtrim.reshape(n_splits, block, N).sum(axis=1)  # (S, N)

    half = n_splits // 2
    all_idx = list(range(n_splits))
    n_hit = 0
    n_combo = 0
    lambdas = []
    for is_blocks in combinations(all_idx, half):
        os_blocks = [i for i in all_idx if i not in set(is_blocks)]
        is_perf = block_perf[list(is_blocks), :].sum(axis=0)   # (N,)
        os_perf = block_perf[os_blocks, :].sum(axis=0)         # (N,)
        is_best = int(np.argmax(is_perf))
        # OS 升序名次（1=最差, N=最好）；并列取平均名次
        order = np.argsort(np.argsort(os_perf)) + 1            # 0-based→1-based 升序名次
        rank = float(order[is_best])
        omega = rank / (N + 1.0)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        lam = float(np.log(omega / (1 - omega)))
        lambdas.append(lam)
        if omega <= 0.5:
            n_hit += 1
        n_combo += 1

    pbo = n_hit / n_combo if n_combo else None
    if pbo is None:
        return {"status": "insufficient", "reason": "无有效组合", "pbo": None}

    if pbo > 0.5:
        interp = f"✗ PBO={pbo:.2f}>0.5：回测最优配置在样本外多半垫底，存在明显过拟合"
    elif pbo > 0.3:
        interp = f"⚠️ PBO={pbo:.2f}：有一定过拟合风险，建议缩小搜索空间/加强正则"
    else:
        interp = f"✓ PBO={pbo:.2f}<0.3：回测选择在样本外较稳健，过拟合概率低"

    return {
        "status": "ok",
        "pbo": round(float(pbo), 4),
        "n_splits": n_splits,
        "n_configs": int(N),
        "n_periods": int(use_rows),
        "n_combinations": int(n_combo),
        "lambda_mean": round(float(np.mean(lambdas)), 4) if lambdas else None,
        "interpretation": interp,
    }
