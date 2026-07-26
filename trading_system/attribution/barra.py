"""
Barra风格因子归因
=================
将组合收益分解为风格因子暴露 + 选股Alpha

核心功能:
  1. 风格因子构建（市场/规模/价值/动量/质量/波动率）
  2. 组合因子暴露计算
  3. 收益归因分解（因子收益 vs 选股Alpha）
  4. Alpha稳定性评估
  5. 因子拥挤度检测

原理（Barra风险模型简化版）:
  R_portfolio = β_market × R_market + β_size × R_size + β_value × R_value
                + β_momentum × R_momentum + β_quality × R_quality + Alpha + ε

  如果收益主要来自β_market（市场涨），说明选股能力弱
  如果Alpha > 0 且稳定，说明有真正的选股能力

使用方式:
    from attribution.barra import BarraAttribution
    barra = BarraAttribution()
    result = barra.attribute(portfolio_returns, factor_returns)
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class BarraAttribution:
    """Barra风格因子归因"""

    FACTOR_NAMES = ["market", "size", "value", "momentum", "quality", "volatility"]
    FACTOR_CN = {
        "market": "市场Beta",
        "size": "规模因子",
        "value": "价值因子",
        "momentum": "动量因子",
        "quality": "质量因子",
        "volatility": "低波因子",
    }

    def __init__(self, lookback: int = 60):
        self.lookback = lookback

    def attribute(self, portfolio_returns: pd.Series, factor_returns: pd.DataFrame,
                  holdings: dict = None, data_dict: dict = None) -> dict:
        """
        执行因子归因
        
        参数:
            portfolio_returns: 组合日收益率序列
            factor_returns: 因子日收益率 DataFrame (columns=因子名)
            holdings: 当前持仓（用于计算暴露）
            data_dict: {code: DataFrame}（用于计算因子暴露）
        
        返回:
            {
                "factor_exposures": dict,    # 各因子暴露（Beta）
                "factor_contribution": dict, # 各因子收益贡献
                "alpha": float,              # 选股Alpha（年化）
                "alpha_tstat": float,        # Alpha显著性
                "r_squared": float,          # 模型拟合度
                "residual_vol": float,       # 残差波动率（特质风险）
                "interpretation": str,       # 解读
            }
        """
        if portfolio_returns is None or len(portfolio_returns) < 20:
            return {"error": "收益率数据不足"}

        # 对齐数据
        if factor_returns is not None and not factor_returns.empty:
            common_idx = portfolio_returns.index.intersection(factor_returns.index)
            y = portfolio_returns.loc[common_idx].values
            X = factor_returns.loc[common_idx].values
            factor_names = factor_returns.columns.tolist()
        else:
            # 无外部因子数据，用持仓数据构建简化因子
            y = portfolio_returns.values
            X, factor_names = self._build_factors_from_holdings(holdings, data_dict, len(y))
            if X is None:
                return {"error": "无法构建因子数据"}

        n = len(y)
        if n < 20:
            return {"error": f"有效样本不足({n}<20)"}

        # ---- OLS回归: y = Xβ + α + ε ----
        # 加入截距项（Alpha）
        X_with_const = np.column_stack([np.ones(n), X])

        try:
            # β = (X'X)^-1 X'y
            beta = np.linalg.lstsq(X_with_const, y, rcond=None)[0]
        except np.linalg.LinAlgError:
            return {"error": "回归计算失败（多重共线性）"}

        alpha_daily = beta[0]  # 日度Alpha
        factor_betas = beta[1:]  # 因子暴露

        # 拟合值与残差
        y_hat = X_with_const @ beta
        residuals = y - y_hat
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        # Alpha统计显著性
        n_params = X_with_const.shape[1]
        mse = ss_res / max(1, n - n_params)
        try:
            var_beta = mse * np.linalg.inv(X_with_const.T @ X_with_const)
            alpha_se = np.sqrt(var_beta[0, 0])
            alpha_tstat = alpha_daily / alpha_se if alpha_se > 0 else 0
        except np.linalg.LinAlgError:
            alpha_tstat = 0

        # 年化
        alpha_annual = alpha_daily * 252
        residual_vol_annual = np.std(residuals) * np.sqrt(252)

        # 因子收益贡献
        factor_contribution = {}
        for i, name in enumerate(factor_names):
            # 贡献 = 暴露 × 因子平均收益
            factor_mean = np.mean(X[:, i]) * 252
            contribution = factor_betas[i] * factor_mean
            factor_contribution[name] = {
                "exposure": round(float(factor_betas[i]), 4),
                "factor_return_annual": round(float(factor_mean), 4),
                "contribution_annual": round(float(contribution), 4),
                "cn": self.FACTOR_CN.get(name, name),
            }

        # 总因子贡献
        total_factor_contrib = sum(
            v["contribution_annual"] for v in factor_contribution.values()
        )

        # ---- 解读 ----
        interpretation = self._interpret(
            alpha_annual, alpha_tstat, r_squared,
            factor_contribution, total_factor_contrib
        )

        return {
            "factor_exposures": {
                name: round(float(factor_betas[i]), 4)
                for i, name in enumerate(factor_names)
            },
            "factor_contribution": factor_contribution,
            "alpha_annual": round(float(alpha_annual), 4),
            "alpha_daily": round(float(alpha_daily), 6),
            "alpha_tstat": round(float(alpha_tstat), 3),
            "alpha_significant": abs(alpha_tstat) > 2.0,
            "r_squared": round(float(r_squared), 4),
            "residual_vol_annual": round(float(residual_vol_annual), 4),
            "total_factor_contribution": round(float(total_factor_contrib), 4),
            "portfolio_return_annual": round(float(np.mean(y) * 252), 4),
            "interpretation": interpretation,
            "sample_size": n,
        }

    def _build_factors_from_holdings(self, holdings: dict, data_dict: dict,
                                     n_days: int):
        """从持仓数据构建完整6因子模型（市场/规模/动量/波动率/价值/质量）"""
        if not data_dict or "000300" not in data_dict:
            return None, []

        # 市场因子：沪深300收益率
        benchmark = data_dict["000300"]
        if len(benchmark) < n_days + 1:
            return None, []

        market_ret = benchmark["close"].pct_change().tail(n_days).values

        X_columns = [market_ret]
        factor_names = ["market"]
        factor_status = {"market": True}

        # 提取持仓股行情数据（排除基准指数）
        stock_codes = [c for c in (holdings or {}).keys() if c in data_dict and c != "000300"]
        if not stock_codes:
            # 无持仓股数据，仅返回市场因子（向后兼容）
            logger.info("Barra因子构建: 市场✓ 规模~ 动量~ 波动率~ 价值~ 质量~ (无持仓数据)")
            return market_ret.reshape(-1, 1), ["market"]

        stock_dfs = {c: data_dict[c] for c in stock_codes if len(data_dict[c]) >= max(n_days + 1, 21)}
        if not stock_dfs:
            logger.info("Barra因子构建: 市场✓ 规模~ 动量~ 波动率~ 价值~ 质量~ (持仓数据不足)")
            return market_ret.reshape(-1, 1), ["market"]

        # 计算等权持仓日收益率
        stock_returns = self._equal_weight_returns(stock_dfs, n_days)

        # ---- 规模因子 (SMB): 用20日平均成交额作为市值代理 ----
        try:
            size_proxy = {}
            for code, df in stock_dfs.items():
                if "amount" in df.columns and len(df) >= 20:
                    avg_amount = df["amount"].tail(20).mean()
                    if avg_amount > 0:
                        size_proxy[code] = avg_amount
            if len(size_proxy) >= 3:
                size_factor = self._make_long_short_factor(size_proxy, stock_returns, n_days)
                if size_factor is not None:
                    X_columns.append(size_factor)
                    factor_names.append("size")
                    factor_status["size"] = True
        except Exception as e:
            logger.debug(f"规模因子构建失败: {e}")

        # ---- 动量因子 (MOM): 20日收益率 ----
        try:
            mom_proxy = {}
            for code, df in stock_dfs.items():
                close = df["close"].values
                if len(close) >= 20 and close[-20] > 0:
                    mom_proxy[code] = (close[-1] / close[-20]) - 1
            if len(mom_proxy) >= 3:
                mom_factor = self._make_long_short_factor(mom_proxy, stock_returns, n_days)
                if mom_factor is not None:
                    X_columns.append(mom_factor)
                    factor_names.append("momentum")
                    factor_status["momentum"] = True
        except Exception as e:
            logger.debug(f"动量因子构建失败: {e}")

        # ---- 波动率因子 (VOL): 20日收益率标准差（年化） ----
        try:
            vol_proxy = {}
            for code, df in stock_dfs.items():
                close = df["close"]
                if len(close) >= 21:
                    daily_ret = close.pct_change().tail(20)
                    vol_proxy[code] = daily_ret.std() * np.sqrt(252)
            if len(vol_proxy) >= 3:
                vol_factor = self._make_long_short_factor(vol_proxy, stock_returns, n_days)
                if vol_factor is not None:
                    X_columns.append(vol_factor)
                    factor_names.append("volatility")
                    factor_status["volatility"] = True
        except Exception as e:
            logger.debug(f"波动率因子构建失败: {e}")

        # ---- 价值因子 (VAL): PE倒数（高价值=低PE） ----
        pe_available = False
        fundamental_data = None  # 延迟获取，只调用一次
        try:
            fundamental_data = self._get_fundamental_proxies(stock_codes)
            val_proxy = {}
            for code in stock_codes:
                fin = fundamental_data.get(code, {})
                pe = fin.get("pe_ttm")
                if pe is not None and pe > 0:
                    val_proxy[code] = 1.0 / pe  # 高值=低PE=高价值
            # PE不可用时用PB倒数替代
            if len(val_proxy) < 3:
                val_proxy = {}
                for code in stock_codes:
                    fin = fundamental_data.get(code, {})
                    pb = fin.get("pb")
                    if pb is not None and pb > 0:
                        val_proxy[code] = 1.0 / pb
                if val_proxy:
                    pe_available = False  # 标记为代理
            else:
                pe_available = True
            if len(val_proxy) >= 3:
                val_factor = self._make_long_short_factor(val_proxy, stock_returns, n_days)
                if val_factor is not None:
                    X_columns.append(val_factor)
                    factor_names.append("value")
                    factor_status["value"] = True
        except Exception as e:
            logger.debug(f"价值因子构建失败: {e}")

        # ---- 质量因子 (QUAL): ROE（ROE不可用时用毛利率代理） ----
        roe_available = False
        qual_proxy_source = "none"
        try:
            if fundamental_data is None:
                fundamental_data = self._get_fundamental_proxies(stock_codes)
            qual_proxy = {}
            for code in stock_codes:
                fin = fundamental_data.get(code, {})
                roe = fin.get("roe")
                if roe is not None:
                    qual_proxy[code] = roe
            if len(qual_proxy) >= 3:
                roe_available = True
                qual_proxy_source = "ROE"
            else:
                # 用毛利率代理
                qual_proxy = {}
                for code in stock_codes:
                    fin = fundamental_data.get(code, {})
                    gm = fin.get("gross_margin")
                    if gm is not None:
                        qual_proxy[code] = gm
                if qual_proxy:
                    qual_proxy_source = "毛利率"
            if len(qual_proxy) >= 3:
                qual_factor = self._make_long_short_factor(qual_proxy, stock_returns, n_days)
                if qual_factor is not None:
                    X_columns.append(qual_factor)
                    factor_names.append("quality")
                    factor_status["quality"] = True
        except Exception as e:
            logger.debug(f"质量因子构建失败: {e}")

        # ---- 因子构建日志 ----
        status_parts = []
        for fn in self.FACTOR_NAMES:
            if factor_status.get(fn):
                status_parts.append(f"{self.FACTOR_CN[fn]}✓")
            else:
                status_parts.append(f"{self.FACTOR_CN[fn]}~")
        if pe_available:
            status_parts.append("价值(PE)")
        elif "value" in factor_names:
            status_parts.append("价值(PB代理)")
        if roe_available:
            status_parts.append("质量(ROE)")
        elif "quality" in factor_names:
            status_parts.append(f"质量({qual_proxy_source}代理)")
        logger.info(f"Barra因子构建: {' '.join(status_parts)}")

        # 组合因子矩阵
        X = np.column_stack(X_columns)
        return X, factor_names

    def _make_long_short_factor(self, proxy: dict, stock_returns: dict,
                                n_days: int) -> np.ndarray:
        """
        根据代理指标中位数分组，计算多空因子收益率

        参数:
            proxy: {code: 因子代理值}
            stock_returns: {code: np.array(日收益率)}
            n_days: 输出长度

        返回:
            np.array(n_days,) 因子日收益率，失败返回None
        """
        if len(proxy) < 3:
            return None

        codes = [c for c in proxy.keys() if c in stock_returns]
        if len(codes) < 3:
            return None

        values = np.array([proxy[c] for c in codes])
        median_val = np.median(values)

        high_group = [c for c, v in zip(codes, values) if v >= median_val]
        low_group = [c for c, v in zip(codes, values) if v < median_val]

        if not high_group or not low_group:
            return None

        # 对齐长度
        min_len = min(min(len(stock_returns[c]) for c in high_group),
                      min(len(stock_returns[c]) for c in low_group),
                      n_days)
        if min_len < n_days:
            return None

        high_ret = np.mean([stock_returns[c][-n_days:] for c in high_group], axis=0)
        low_ret = np.mean([stock_returns[c][-n_days:] for c in low_group], axis=0)
        return high_ret - low_ret

    def _equal_weight_returns(self, stock_dfs: dict, n_days: int) -> dict:
        """计算各股票日收益率（等权）"""
        returns = {}
        for code, df in stock_dfs.items():
            close = df["close"]
            if len(close) >= n_days + 1:
                ret = close.pct_change().tail(n_days).values
                if len(ret) == n_days:
                    returns[code] = ret
        return returns

    def _get_fundamental_proxies(self, codes: list) -> dict:
        """获取基本面代理数据（PE/PB/ROE/毛利率）"""
        result = {}
        try:
            from strategy.fundamental import FundamentalAnalyzer
            fa = FundamentalAnalyzer()
            for code in codes:
                try:
                    fin = fa.get_financial_indicators(code)
                    result[code] = fin
                except Exception:
                    result[code] = {}
        except ImportError:
            logger.debug("基本面模块不可用，价值/质量因子使用代理指标")
        except Exception as e:
            logger.debug(f"基本面数据获取异常: {e}")
        return result

    def _interpret(self, alpha, tstat, r2, contributions, total_contrib) -> str:
        """生成归因解读（支持6因子）"""
        parts = []

        # Alpha评估
        if alpha > 0.05 and tstat > 2:
            parts.append(f"★选股Alpha显著为正(年化{alpha:.1%}, t={tstat:.1f})，具备真实选股能力")
        elif alpha > 0 and tstat > 1.5:
            parts.append(f"选股Alpha为正(年化{alpha:.1%})但显著性一般(t={tstat:.1f})")
        elif alpha < 0:
            parts.append(f"⚠️选股Alpha为负(年化{alpha:.1%})，选股在拖累收益")
        else:
            parts.append(f"选股Alpha接近零(年化{alpha:.1%})，收益主要来自因子暴露")

        # 市场Beta
        market_contrib = contributions.get("market", {}).get("contribution_annual", 0)
        if abs(market_contrib) > abs(total_contrib) * 0.7:
            parts.append("收益主要来自市场Beta（跟着大盘涨），非选股功劳")

        # 风格因子贡献分析
        style_factors = {
            "size": "规模", "momentum": "动量", "value": "价值",
            "quality": "质量", "volatility": "低波",
        }
        top_style = None
        top_style_contrib = 0
        for factor_key, cn_name in style_factors.items():
            c = contributions.get(factor_key, {}).get("contribution_annual", 0)
            if abs(c) > top_style_contrib:
                top_style_contrib = abs(c)
                top_style = (factor_key, cn_name, c)
        if top_style and top_style_contrib > abs(total_contrib) * 0.1:
            _, cn_name, c = top_style
            direction = "正贡献" if c > 0 else "负拖累"
            parts.append(f"{cn_name}因子{direction}(年化{c:.1%})")

        # 因子覆盖度
        active_factors = sum(1 for k in style_factors if k in contributions
                             and abs(contributions[k].get("contribution_annual", 0)) > 0.001)
        if active_factors >= 3:
            parts.append(f"{active_factors}个风格因子有效参与归因")

        # 拟合度
        if r2 > 0.8:
            parts.append(f"模型拟合度高(R²={r2:.2f})，收益可被因子解释")
        elif r2 < 0.4:
            parts.append(f"模型拟合度低(R²={r2:.2f})，收益来源复杂或数据不足")

        return " | ".join(parts)
