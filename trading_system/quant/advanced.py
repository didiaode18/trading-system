"""
P4 极致优化与差异化增强
========================
滚动参数寻优(Walk Forward) + 黑天鹅熔断 + 另类数据 + A股特色功能

功能:
1. Walk Forward滚动寻优: 滚动窗口回测，寻找最优参数组合
2. 黑天鹅熔断: 极端行情自动降仓/清仓
3. 另类数据: 筹码分布估算 + 板块联动 + 情绪因子
4. A股特色: 新股申购提醒 + 可转债双低策略

使用方式:
    from quant.advanced import WalkForwardOptimizer, CircuitBreaker
    optimizer = WalkForwardOptimizer(engine, factor_engine)
    best_params = optimizer.optimize(data_dict, param_grid)
"""

import logging
import itertools
import numpy as np
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================
# 一、Walk Forward 滚动参数寻优
# ============================================================

class WalkForwardOptimizer:
    """
    滚动参数寻优

    原理:
    - 将数据分为多个 [训练窗口 + 验证窗口]
    - 在训练窗口上搜索最优参数
    - 在验证窗口上评估泛化能力
    - 滚动前进，避免过拟合

    示例:
        训练: 2021-2023, 验证: 2024Q1
        训练: 2021-2024Q1, 验证: 2024Q2
        ...
    """

    def __init__(self, engine_class, factor_engine,
                 train_months: int = 12,
                 test_months: int = 3,
                 step_months: int = 3):
        """
        参数:
            engine_class: 回测引擎类
            factor_engine: 因子引擎
            train_months: 训练窗口(月)
            test_months: 验证窗口(月)
            step_months: 滚动步长(月)
        """
        self.engine_class = engine_class
        self.factor_engine = factor_engine
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months
        self.results = []

    def optimize(self, data_dict: dict, param_grid: dict,
                 start_date: str = "2021-01-01",
                 end_date: str = "2026-01-01",
                 metric: str = "sharpe") -> dict:
        """
        执行Walk Forward优化

        参数:
            data_dict: 全量数据
            param_grid: 参数搜索空间 {"param_name": [values]}
            metric: 优化目标 ("sharpe" / "calmar" / "return")

        返回:
            {"best_params": {...}, "oos_sharpe": float, "windows": [...]}
        """
        # 生成参数组合
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        all_combos = list(itertools.product(*param_values))

        logger.info(f"Walk Forward: {len(all_combos)}组参数, "
                   f"训练{self.train_months}月/验证{self.test_months}月")

        # 生成滚动窗口
        windows = self._generate_windows(start_date, end_date)
        logger.info(f"共{len(windows)}个滚动窗口")

        # 逐窗口优化
        window_results = []
        for i, (train_start, train_end, test_start, test_end) in enumerate(windows):
            logger.info(f"  窗口{i+1}: 训练[{train_start}~{train_end}] "
                       f"验证[{test_start}~{test_end}]")

            # 训练期：搜索最优参数
            best_params, best_score = self._search_best_params(
                data_dict, all_combos, param_names,
                train_start, train_end, metric
            )

            # 验证期：用最优参数跑回测
            oos_result = self._run_single_backtest(
                data_dict, best_params, test_start, test_end
            )
            oos_score = oos_result.get(metric, 0)

            window_results.append({
                "window": i + 1,
                "train": f"{train_start}~{train_end}",
                "test": f"{test_start}~{test_end}",
                "best_params": best_params,
                "is_score": round(best_score, 4),
                "oos_score": round(oos_score, 4),
            })

            logger.info(f"    IS={best_score:.4f}, OOS={oos_score:.4f}, "
                       f"params={best_params}")

        # 汇总
        avg_oos = np.mean([w["oos_score"] for w in window_results])
        final_params = self._aggregate_params(window_results)

        result = {
            "best_params": final_params,
            "avg_oos_score": round(avg_oos, 4),
            "windows": window_results,
            "metric": metric,
        }
        self.results.append(result)
        return result

    def _generate_windows(self, start_date, end_date) -> list:
        """生成滚动窗口"""
        windows = []
        current = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)

        while True:
            train_start = current.strftime("%Y-%m-%d")
            train_end = (current + pd.DateOffset(months=self.train_months)).strftime("%Y-%m-%d")
            test_start = train_end
            test_end = (current + pd.DateOffset(
                months=self.train_months + self.test_months)).strftime("%Y-%m-%d")

            if pd.Timestamp(test_end) > end:
                break

            windows.append((train_start, train_end, test_start, test_end))
            current += pd.DateOffset(months=self.step_months)

        return windows

    def _search_best_params(self, data_dict, combos, param_names,
                            start, end, metric) -> tuple:
        """在训练期搜索最优参数"""
        best_score = -999
        best_params = {}

        for combo in combos:
            params = dict(zip(param_names, combo))
            result = self._run_single_backtest(data_dict, params, start, end)
            score = result.get(metric, 0)

            if score > best_score:
                best_score = score
                best_params = params

        return best_params, best_score

    def _run_single_backtest(self, data_dict, params, start, end) -> dict:
        """运行单次回测"""
        try:
            engine = self.engine_class(
                initial_capital=1_000_000,
                max_positions=params.get("max_positions", 10),
                slippage=params.get("slippage", 0.001),
            )
            result = engine.run(
                data_dict, self.factor_engine,
                start, end,
                rebalance_days=params.get("rebalance_days", 5)
            )

            if "error" in result:
                return {"sharpe": 0, "calmar": 0, "return": 0}

            # 计算指标
            nav = result["daily_values"]["nav"].values
            returns = np.diff(nav) / nav[:-1]
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if np.std(returns) > 0 else 0
            total_return = nav[-1] / nav[0] - 1

            # 最大回撤
            peak = np.maximum.accumulate(nav)
            dd = (nav - peak) / peak
            max_dd = abs(dd.min())
            calmar = (total_return / max_dd) if max_dd > 0 else 0

            return {"sharpe": sharpe, "calmar": calmar, "return": total_return}
        except Exception as e:
            logger.debug(f"回测失败: {e}")
            return {"sharpe": 0, "calmar": 0, "return": 0}

    def _aggregate_params(self, window_results) -> dict:
        """聚合各窗口最优参数（取众数/中位数）"""
        if not window_results:
            return {}

        all_params = [w["best_params"] for w in window_results]
        aggregated = {}
        for key in all_params[0].keys():
            values = [p[key] for p in all_params]
            if isinstance(values[0], (int, float)):
                aggregated[key] = np.median(values)
            else:
                # 取众数
                from collections import Counter
                aggregated[key] = Counter(values).most_common(1)[0][0]

        return aggregated


# ============================================================
# 二、黑天鹅熔断
# ============================================================

class CircuitBreaker:
    """
    黑天鹅系统性熔断

    触发条件:
    1. 大盘单日跌幅 > 5%
    2. 持仓组合单日亏损 > 8%
    3. 连续3日亏损 > 10%
    4. 个股闪崩（持仓股单日跌停）

    熔断动作:
    - Level 1 (警告): 暂停新建仓
    - Level 2 (减仓): 减仓50%
    - Level 3 (清仓): 全部清仓
    """

    def __init__(self, market_drop_threshold: float = 0.05,
                 portfolio_drop_threshold: float = 0.08,
                 consecutive_loss_threshold: float = 0.10):
        self.market_drop_threshold = market_drop_threshold
        self.portfolio_drop_threshold = portfolio_drop_threshold
        self.consecutive_loss_threshold = consecutive_loss_threshold
        self.level = 0  # 0=正常, 1=警告, 2=减仓, 3=清仓
        self.trigger_history = []
        self.daily_returns = []

    def check(self, market_change: float, portfolio_change: float,
              date: str = None) -> dict:
        """
        每日熔断检查

        返回:
            {"level": int, "action": str, "reason": str}
        """
        self.daily_returns.append(portfolio_change)
        reasons = []
        new_level = 0

        # 条件1: 大盘暴跌
        if market_change <= -self.market_drop_threshold:
            reasons.append(f"大盘暴跌{market_change:.1%}")
            new_level = max(new_level, 2)

        # 条件2: 组合单日巨亏
        if portfolio_change <= -self.portfolio_drop_threshold:
            reasons.append(f"组合巨亏{portfolio_change:.1%}")
            new_level = max(new_level, 2)

        # 条件3: 连续亏损
        if len(self.daily_returns) >= 3:
            recent_3d = sum(self.daily_returns[-3:])
            if recent_3d <= -self.consecutive_loss_threshold:
                reasons.append(f"连续3日亏损{recent_3d:.1%}")
                new_level = max(new_level, 3)

        # 只升不降（当日）
        self.level = max(self.level, new_level)

        action_map = {0: "正常交易", 1: "暂停建仓", 2: "减仓50%", 3: "全部清仓"}
        result = {
            "level": self.level,
            "action": action_map[self.level],
            "reason": " | ".join(reasons) if reasons else "无异常",
            "date": date,
        }

        if self.level > 0:
            self.trigger_history.append(result)
            logger.critical(f"[熔断] Level {self.level}: {result['action']} - {result['reason']}")

        return result

    def reset(self):
        """重置熔断状态（新交易日）"""
        self.level = 0

    def get_position_multiplier(self) -> float:
        """根据熔断等级返回仓位乘数"""
        multipliers = {0: 1.0, 1: 0.7, 2: 0.5, 3: 0.0}
        return multipliers.get(self.level, 1.0)


# ============================================================
# 三、另类数据因子
# ============================================================

class AlternativeData:
    """
    另类数据增强（纯量价推算，无需外部数据源）

    1. 筹码分布估算: 基于成交量加权的价格分布
    2. 板块联动: 同行业股票相关性
    3. 情绪因子: 涨跌比/涨停数/换手率异常
    """

    def estimate_chip_distribution(self, df: pd.DataFrame,
                                    lookback: int = 60) -> dict:
        """
        估算筹码分布（基于成交量加权）

        返回:
            {"peak_price": x, "profit_ratio": x, "concentration": x}
        """
        if len(df) < lookback:
            return {"peak_price": 0, "profit_ratio": 0, "concentration": 0}

        recent = df.tail(lookback)
        prices = recent["close"].values
        volumes = recent["volume"].values

        # 成交量加权平均价（筹码重心）
        weighted_price = np.average(prices, weights=volumes)

        # 当前价位的获利比例
        current_price = prices[-1]
        profit_ratio = np.sum(volumes[prices <= current_price]) / np.sum(volumes)

        # 筹码集中度（标准差越小越集中）
        concentration = 1 / (np.std(prices) / np.mean(prices) + 0.01)

        return {
            "peak_price": round(weighted_price, 2),
            "profit_ratio": round(profit_ratio, 4),
            "concentration": round(concentration, 4),
        }

    def calc_sector_correlation(self, data_dict: dict,
                                 sector_map: dict,
                                 lookback: int = 20) -> dict:
        """
        计算板块联动系数

        参数:
            sector_map: {code: sector_name}

        返回:
            {sector: avg_correlation_with_others}
        """
        # 按板块分组
        sector_stocks = {}
        for code, sector in sector_map.items():
            if code in data_dict:
                sector_stocks.setdefault(sector, []).append(code)

        correlations = {}
        for sector, codes in sector_stocks.items():
            if len(codes) < 2:
                continue

            # 计算板块内股票的平均相关性
            returns_list = []
            for code in codes[:5]:  # 最多取5只
                df = data_dict[code].tail(lookback + 1)
                if len(df) > lookback:
                    ret = df["close"].pct_change().dropna().values
                    returns_list.append(ret)

            if len(returns_list) >= 2:
                corr_matrix = np.corrcoef(returns_list)
                avg_corr = np.mean(corr_matrix[np.triu_indices_from(corr_matrix, 1)])
                correlations[sector] = round(avg_corr, 4)

        return correlations

    def calc_market_sentiment(self, data_dict: dict, date: str) -> dict:
        """
        市场情绪因子

        返回:
            {"advance_ratio": x, "limit_up_count": x, "avg_turnover": x}
        """
        advances = 0
        declines = 0
        limit_ups = 0
        turnovers = []

        for code, df in data_dict.items():
            df_cut = df[df["date"] <= date]
            if len(df_cut) < 2:
                continue

            change = (df_cut["close"].iloc[-1] - df_cut["close"].iloc[-2]) / df_cut["close"].iloc[-2]

            if change > 0:
                advances += 1
            elif change < 0:
                declines += 1

            if change >= 0.095:
                limit_ups += 1

            if "turn" in df_cut.columns:
                turnovers.append(df_cut["turn"].iloc[-1])

        total = advances + declines
        advance_ratio = advances / total if total > 0 else 0.5
        avg_turnover = np.mean(turnovers) if turnovers else 0

        return {
            "advance_ratio": round(advance_ratio, 4),
            "limit_up_count": limit_ups,
            "avg_turnover": round(avg_turnover, 2),
            "sentiment_score": round(advance_ratio * 0.5 + min(limit_ups / 50, 1) * 0.3 +
                                     min(avg_turnover / 5, 1) * 0.2, 4),
        }


# ============================================================
# 四、A股特色功能
# ============================================================

class ASpecialFeatures:
    """A股特色附属功能"""

    def check_new_stock_subscription(self, holdings_value: float) -> dict:
        """
        新股申购提醒（基于持仓市值判断资格）

        沪市: 每1万元市值可申购1000股
        深市: 每5000元市值可申购500股
        """
        sh_quota = int(holdings_value / 10000) * 1000
        sz_quota = int(holdings_value / 5000) * 500

        return {
            "sh_quota": sh_quota,
            "sz_quota": sz_quota,
            "reminder": f"沪市可申购{sh_quota}股, 深市可申购{sz_quota}股",
        }

    def convertible_bond_double_low(self, bonds: list) -> list:
        """
        可转债双低策略

        双低值 = 转债价格 + 转股溢价率*100
        筛选: 双低值 < 130 且 价格 < 110

        参数:
            bonds: [{"code": x, "price": x, "premium_rate": x}, ...]

        返回:
            符合条件的转债列表（按双低值排序）
        """
        qualified = []
        for bond in bonds:
            price = bond.get("price", 200)
            premium = bond.get("premium_rate", 1)
            double_low = price + premium * 100

            if double_low < 130 and price < 110:
                qualified.append({
                    **bond,
                    "double_low": round(double_low, 2),
                })

        qualified.sort(key=lambda x: x["double_low"])
        return qualified[:10]
