"""
组合风险管理模块 V1.0
======================
基于现代投资组合理论(MPT)，提供:
  1. 持仓相关性矩阵分析（Pearson相关系数）
  2. 行业/个股集中度预警（HHI指数）
  3. VaR/CVaR风险度量（历史模拟法）
  4. 最大回撤追踪与账户级风控
  5. 基于ATR的动态仓位计算（波动率倒数法）
  6. 自动再平衡建议（目标配比 vs 当前配比）

使用方式:
    from strategy.portfolio_risk import PortfolioRiskManager
    prm = PortfolioRiskManager(data_dict, holdings)
    report = prm.full_risk_report()
"""

import os
import sys
import logging
import datetime
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class PortfolioRiskManager:
    """组合风险管理器"""

    def __init__(self, data_dict: dict, holdings: dict):
        """
        参数:
            data_dict: {code: DataFrame} 含日线数据（需有close列）
            holdings: {code: {shares, buy_price, sector, ...}}
        """
        self.data_dict = data_dict
        self.holdings = holdings
        self.total_capital = config.TOTAL_CAPITAL
        self.available_cash = getattr(config, 'AVAILABLE_CASH', self.total_capital * 0.3)

    # ============================================================
    # 一、相关性分析
    # ============================================================

    def calc_correlation_matrix(self, lookback: int = 60) -> dict:
        """
        计算持仓股票间的收益率相关性矩阵
        
        返回:
            {
                "matrix": DataFrame (相关系数矩阵),
                "high_corr_pairs": [(code1, code2, corr), ...],  # 高相关对
                "avg_correlation": float,
                "diversification_score": float,  # 分散化得分(0-100)
                "risk_level": str
            }
        """
        # 收集持仓股票的日收益率序列
        returns_dict = {}
        holding_codes = list(self.holdings.keys())

        for code in holding_codes:
            if code in self.data_dict and len(self.data_dict[code]) >= lookback:
                df = self.data_dict[code]
                closes = df["close"].tail(lookback + 1).values
                daily_returns = np.diff(closes) / closes[:-1]
                returns_dict[code] = daily_returns

        if len(returns_dict) < 2:
            return {
                "matrix": pd.DataFrame(),
                "high_corr_pairs": [],
                "avg_correlation": 0,
                "diversification_score": 100,
                "risk_level": "low",
                "detail": "持仓不足2只，无法计算相关性"
            }

        # 对齐长度
        min_len = min(len(v) for v in returns_dict.values())
        returns_df = pd.DataFrame({
            code: ret[-min_len:] for code, ret in returns_dict.items()
        })

        # 计算相关系数矩阵
        corr_matrix = returns_df.corr()

        # 找出高相关对 (>0.7)
        high_corr_pairs = []
        codes = list(corr_matrix.columns)
        for i in range(len(codes)):
            for j in range(i + 1, len(codes)):
                corr_val = corr_matrix.iloc[i, j]
                if corr_val > 0.7:
                    name_i = self._get_stock_name(codes[i])
                    name_j = self._get_stock_name(codes[j])
                    high_corr_pairs.append((codes[i], codes[j], round(corr_val, 3),
                                           name_i, name_j))

        # 平均相关性
        upper_tri = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(bool))
        avg_corr = upper_tri.stack().mean() if not upper_tri.stack().empty else 0

        # 分散化得分: 相关性越低分越高
        # 100分 = 完全不相关(0), 0分 = 完全正相关(1)
        diversification_score = max(0, min(100, (1 - avg_corr) * 100))

        # 风险等级
        if avg_corr > 0.8:
            risk_level = "critical"
        elif avg_corr > 0.6:
            risk_level = "high"
        elif avg_corr > 0.4:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "matrix": corr_matrix,
            "high_corr_pairs": high_corr_pairs,
            "avg_correlation": round(avg_corr, 3),
            "diversification_score": round(diversification_score, 1),
            "risk_level": risk_level,
            "detail": f"平均相关系数{avg_corr:.3f}，分散化得分{diversification_score:.0f}/100"
        }

    # ============================================================
    # 二、集中度分析（HHI指数）
    # ============================================================

    def calc_concentration(self) -> dict:
        """
        计算持仓集中度（赫芬达尔-赫希曼指数 HHI）
        
        HHI = sum(各持仓市值占比^2)
        - HHI < 0.15: 分散（良好）
        - 0.15 <= HHI < 0.25: 适度集中
        - HHI >= 0.25: 高度集中（危险）
        
        同时计算行业集中度
        """
        # 个股集中度
        position_values = {}
        sector_values = defaultdict(float)

        for code, holding in self.holdings.items():
            shares = holding.get("shares", 0)
            price = holding.get("current_price", holding.get("buy_price", 0))
            value = shares * price
            position_values[code] = value
            sector = holding.get("sector", "其他")
            sector_values[sector] += value

        total_value = sum(position_values.values())
        if total_value <= 0:
            return {"stock_hhi": 0, "sector_hhi": 0, "risk_level": "low",
                    "detail": "无持仓", "alerts": []}

        # 个股HHI
        stock_weights = [v / total_value for v in position_values.values()]
        stock_hhi = sum(w ** 2 for w in stock_weights)

        # 行业HHI
        sector_weights = [v / total_value for v in sector_values.values()]
        sector_hhi = sum(w ** 2 for w in sector_weights)

        # 最大单股占比
        max_stock_code = max(position_values, key=position_values.get)
        max_stock_ratio = position_values[max_stock_code] / total_value

        # 最大行业占比
        max_sector = max(sector_values, key=sector_values.get)
        max_sector_ratio = sector_values[max_sector] / total_value

        # 预警
        alerts = []
        if stock_hhi >= 0.40:
            alerts.append({
                "level": "critical",
                "type": "个股极度集中",
                "detail": f"HHI={stock_hhi:.3f}，最大单股{self._get_stock_name(max_stock_code)}占{max_stock_ratio:.1%}",
                "suggestion": "单股占比过高，建议分散至3-5只"
            })
        elif stock_hhi >= 0.25:
            alerts.append({
                "level": "warning",
                "type": "个股集中度偏高",
                "detail": f"HHI={stock_hhi:.3f}，建议均衡配置",
                "suggestion": "适当分散，降低单股风险"
            })

        if sector_hhi >= 0.50:
            alerts.append({
                "level": "critical",
                "type": "行业极度集中",
                "detail": f"行业HHI={sector_hhi:.3f}，{max_sector}占{max_sector_ratio:.1%}",
                "suggestion": f"严重偏重{max_sector}，建议配置2-3个不相关行业"
            })
        elif sector_hhi >= 0.30:
            alerts.append({
                "level": "warning",
                "type": "行业集中度偏高",
                "detail": f"行业HHI={sector_hhi:.3f}，{max_sector}占{max_sector_ratio:.1%}",
                "suggestion": "建议增加其他行业配置"
            })

        # 风险等级
        combined_hhi = (stock_hhi + sector_hhi) / 2
        if combined_hhi >= 0.40:
            risk_level = "critical"
        elif combined_hhi >= 0.25:
            risk_level = "high"
        elif combined_hhi >= 0.15:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "stock_hhi": round(stock_hhi, 4),
            "sector_hhi": round(sector_hhi, 4),
            "max_stock": {"code": max_stock_code, "name": self._get_stock_name(max_stock_code),
                         "ratio": round(max_stock_ratio, 4)},
            "max_sector": {"name": max_sector, "ratio": round(max_sector_ratio, 4)},
            "sector_distribution": {k: round(v / total_value, 4) for k, v in sector_values.items()},
            "risk_level": risk_level,
            "alerts": alerts,
            "detail": f"个股HHI={stock_hhi:.3f} | 行业HHI={sector_hhi:.3f}"
        }

    # ============================================================
    # 三、VaR / CVaR 风险度量
    # ============================================================

    def calc_var(self, confidence: float = 0.95, lookback: int = 60) -> dict:
        """
        历史模拟法计算组合VaR和CVaR
        
        VaR: 在confidence置信度下，未来1天最大可能亏损
        CVaR: 超过VaR的平均亏损（尾部风险）
        
        返回:
            {
                "var_95": float,       # 95% VaR (金额)
                "var_99": float,       # 99% VaR (金额)
                "cvar_95": float,      # 95% CVaR
                "var_pct": float,      # VaR占总资金比例
                "daily_volatility": float,  # 组合日波动率
                "annual_volatility": float, # 年化波动率
                "risk_level": str
            }
        """
        # 计算组合日收益率
        portfolio_returns = self._calc_portfolio_returns(lookback)

        if portfolio_returns is None or len(portfolio_returns) < 20:
            return {
                "var_95": 0, "var_99": 0, "cvar_95": 0,
                "var_pct": 0, "daily_volatility": 0, "annual_volatility": 0,
                "risk_level": "unknown", "detail": "数据不足"
            }

        # 组合总市值
        total_market_value = sum(
            h.get("shares", 0) * h.get("current_price", h.get("buy_price", 0))
            for h in self.holdings.values()
        )

        # 排序收益率（从小到大）
        sorted_returns = np.sort(portfolio_returns)

        # VaR计算
        var_95_idx = int((1 - 0.95) * len(sorted_returns))
        var_99_idx = int((1 - 0.99) * len(sorted_returns))

        var_95_return = sorted_returns[var_95_idx]
        var_99_return = sorted_returns[max(0, var_99_idx)]

        # CVaR (Expected Shortfall)
        cvar_95_return = sorted_returns[:var_95_idx + 1].mean() if var_95_idx > 0 else var_95_return

        # 转换为金额
        var_95_amount = abs(var_95_return) * total_market_value
        var_99_amount = abs(var_99_return) * total_market_value
        cvar_95_amount = abs(cvar_95_return) * total_market_value

        # 波动率
        daily_vol = np.std(portfolio_returns)
        annual_vol = daily_vol * np.sqrt(252)

        # VaR占总资金比例
        var_pct = var_95_amount / self.total_capital

        # 风险等级
        if var_pct > 0.05:
            risk_level = "critical"
        elif var_pct > 0.03:
            risk_level = "high"
        elif var_pct > 0.02:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "var_95": round(var_95_amount, 2),
            "var_99": round(var_99_amount, 2),
            "cvar_95": round(cvar_95_amount, 2),
            "var_pct": round(var_pct, 4),
            "daily_volatility": round(daily_vol, 4),
            "annual_volatility": round(annual_vol, 4),
            "risk_level": risk_level,
            "detail": f"95%VaR={var_95_amount:,.0f}元({var_pct:.2%}) | 年化波动率{annual_vol:.1%}"
        }

    # ============================================================
    # 四、最大回撤追踪
    # ============================================================

    def calc_max_drawdown(self, lookback: int = 120) -> dict:
        """
        计算组合近期最大回撤
        
        返回:
            {
                "max_drawdown": float,     # 最大回撤比例
                "max_dd_amount": float,    # 最大回撤金额
                "peak_date": str,          # 高点日期
                "trough_date": str,        # 低点日期
                "current_drawdown": float, # 当前回撤
                "recovery_days": int,      # 恢复天数(-1=未恢复)
                "risk_level": str
            }
        """
        portfolio_returns = self._calc_portfolio_returns(lookback)
        if portfolio_returns is None or len(portfolio_returns) < 10:
            return {"max_drawdown": 0, "risk_level": "unknown", "detail": "数据不足"}

        # 构建净值曲线
        nav = np.cumprod(1 + portfolio_returns)
        nav = np.insert(nav, 0, 1.0)  # 起始净值1

        # 计算回撤序列
        peak = np.maximum.accumulate(nav)
        drawdown = (nav - peak) / peak

        max_dd = abs(drawdown.min())
        max_dd_idx = np.argmin(drawdown)

        # 找峰值点
        peak_idx = np.argmax(nav[:max_dd_idx + 1]) if max_dd_idx > 0 else 0

        # 当前回撤
        current_dd = abs(drawdown[-1])

        # 是否恢复
        recovery_days = -1
        if max_dd_idx < len(nav) - 1:
            for i in range(max_dd_idx, len(nav)):
                if nav[i] >= nav[peak_idx]:
                    recovery_days = i - max_dd_idx
                    break

        # 风险等级
        if max_dd > 0.20:
            risk_level = "critical"
        elif max_dd > 0.10:
            risk_level = "high"
        elif max_dd > 0.05:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "max_drawdown": round(max_dd, 4),
            "max_dd_amount": round(max_dd * self.total_capital, 2),
            "current_drawdown": round(current_dd, 4),
            "recovery_days": recovery_days,
            "risk_level": risk_level,
            "detail": f"最大回撤{max_dd:.2%} | 当前回撤{current_dd:.2%}"
        }

    # ============================================================
    # 五、动态仓位计算（波动率倒数法 / ATR法）
    # ============================================================

    def calc_dynamic_position(self, code: str, price: float,
                              stop_loss_pct: float = None) -> dict:
        """
        基于ATR的动态仓位计算
        
        原理: 波动越大的股票，分配越少的仓位，使每笔交易的"风险金额"相等
        
        公式: shares = (总资金 × 单笔风险比例) / (ATR × 乘数)
        
        参数:
            code: 股票代码
            price: 当前价格
            stop_loss_pct: 止损比例(默认使用config中的初始止损)
        
        返回:
            {
                "shares": int,           # 建议股数(100整数)
                "amount": float,         # 投入金额
                "position_ratio": float, # 仓位占比
                "atr": float,            # 当前ATR
                "atr_pct": float,        # ATR占价格比例
                "risk_amount": float,    # 风险金额
                "method": str
            }
        """
        if stop_loss_pct is None:
            stop_loss_pct = config.INITIAL_STOP_LOSS_PCT

        # 获取ATR
        atr = self._get_atr(code)
        if atr <= 0 or price <= 0:
            # 无ATR数据，使用固定比例法
            risk_amount = self.total_capital * config.MAX_SINGLE_LOSS_RATIO
            shares = int(risk_amount / (price * stop_loss_pct))
            shares = (shares // 100) * 100
            return {
                "shares": shares,
                "amount": shares * price,
                "position_ratio": shares * price / self.total_capital,
                "atr": 0,
                "atr_pct": 0,
                "risk_amount": shares * price * stop_loss_pct,
                "method": "fixed_ratio"
            }

        # ATR法: 风险金额 / (ATR * 2) = 股数
        # 2倍ATR约等于正常止损距离
        risk_amount = self.total_capital * config.MAX_SINGLE_LOSS_RATIO  # 单笔风险2%
        stop_distance = max(atr * 2, price * stop_loss_pct)  # 取ATR*2和固定止损的较大值
        shares = int(risk_amount / stop_distance)
        shares = (shares // 100) * 100

        # 仓位上限约束
        stock_type = self._get_stock_type(code)
        max_ratio = config.LEADER_STOCK_MAX_RATIO if stock_type == "龙头" else config.FLEXIBLE_STOCK_MAX_RATIO
        max_shares = int(self.total_capital * max_ratio / price / 100) * 100
        shares = min(shares, max_shares)

        # 可用资金约束
        max_affordable = int(self.available_cash * 0.9 / price / 100) * 100
        shares = min(shares, max_affordable)

        amount = shares * price
        actual_risk = shares * stop_distance

        return {
            "shares": shares,
            "amount": round(amount, 2),
            "position_ratio": round(amount / self.total_capital, 4),
            "atr": round(atr, 3),
            "atr_pct": round(atr / price, 4),
            "risk_amount": round(actual_risk, 2),
            "stop_distance": round(stop_distance, 3),
            "method": "atr_volatility"
        }

    # ============================================================
    # 六、自动再平衡建议
    # ============================================================

    def calc_rebalance_plan(self, target_allocation: dict = None) -> dict:
        """
        生成自动再平衡方案
        
        默认目标配比（可根据行情调整）:
        - 单行业不超过30%
        - 单股不超过15%
        - 现金不低于15%
        - 至少覆盖3个行业
        
        参数:
            target_allocation: {"行业名": 目标比例} 自定义目标
        
        返回:
            {
                "actions": [{"action": "减仓/加仓/清仓", "code", "name", "shares", "amount"}],
                "current_allocation": {...},
                "target_allocation": {...},
                "rebalance_cost": float,  # 预估交易成本
                "urgency": str
            }
        """
        if target_allocation is None:
            target_allocation = self._default_target_allocation()

        # 当前配置
        current_alloc = {}
        position_details = {}
        total_market_value = 0

        for code, holding in self.holdings.items():
            shares = holding.get("shares", 0)
            price = holding.get("current_price", holding.get("buy_price", 0))
            value = shares * price
            sector = holding.get("sector", "其他")
            total_market_value += value

            if sector not in current_alloc:
                current_alloc[sector] = 0
            current_alloc[sector] += value

            position_details[code] = {
                "name": self._get_stock_name(code),
                "sector": sector,
                "shares": shares,
                "price": price,
                "value": value,
                "ratio": 0  # 后面计算
            }

        # 计算比例
        total_with_cash = total_market_value + self.available_cash
        for code in position_details:
            position_details[code]["ratio"] = position_details[code]["value"] / total_with_cash

        current_alloc_ratio = {k: v / total_with_cash for k, v in current_alloc.items()}
        current_alloc_ratio["现金"] = self.available_cash / total_with_cash

        # 生成调仓动作
        actions = []
        for code, detail in position_details.items():
            sector = detail["sector"]
            target_ratio = target_allocation.get(sector, 0.10)  # 默认10%
            current_ratio = detail["ratio"]

            # 单股目标 = 行业目标 / 行业内股票数（简化为行业目标的60%）
            stock_target = target_ratio * 0.6
            diff = current_ratio - stock_target

            if diff > 0.05:  # 超配5%以上 → 减仓
                sell_value = diff * total_with_cash
                sell_shares = int(sell_value / detail["price"] / 100) * 100
                if sell_shares >= 100:
                    actions.append({
                        "action": "减仓",
                        "code": code,
                        "name": detail["name"],
                        "sector": sector,
                        "shares": sell_shares,
                        "amount": round(sell_shares * detail["price"], 0),
                        "reason": f"当前{current_ratio:.1%}超目标{stock_target:.1%}",
                        "priority": 1 if diff > 0.10 else 2
                    })
            elif diff < -0.05 and current_ratio < stock_target:  # 低配 → 可加仓
                buy_value = abs(diff) * total_with_cash
                buy_shares = int(buy_value / detail["price"] / 100) * 100
                if buy_shares >= 100 and self.available_cash > buy_shares * detail["price"]:
                    actions.append({
                        "action": "加仓",
                        "code": code,
                        "name": detail["name"],
                        "sector": sector,
                        "shares": buy_shares,
                        "amount": round(buy_shares * detail["price"], 0),
                        "reason": f"当前{current_ratio:.1%}低于目标{stock_target:.1%}",
                        "priority": 3
                    })

        # 按优先级排序
        actions.sort(key=lambda x: x["priority"])

        # 预估交易成本（0.3%）
        total_trade_amount = sum(a["amount"] for a in actions)
        rebalance_cost = total_trade_amount * 0.003

        # 紧急程度
        critical_count = sum(1 for a in actions if a["priority"] == 1)
        if critical_count > 0:
            urgency = "high"
        elif len(actions) > 0:
            urgency = "medium"
        else:
            urgency = "low"

        return {
            "actions": actions,
            "current_allocation": {k: round(v, 4) for k, v in current_alloc_ratio.items()},
            "target_allocation": target_allocation,
            "rebalance_cost": round(rebalance_cost, 2),
            "total_trade_amount": round(total_trade_amount, 2),
            "urgency": urgency,
            "detail": f"{len(actions)}项调仓 | 交易额{total_trade_amount:,.0f}元 | 成本{rebalance_cost:,.0f}元"
        }

    # ============================================================
    # 七、综合风险报告
    # ============================================================

    def full_risk_report(self) -> dict:
        """生成完整的组合风险报告"""
        logger.info("[组合风控] 开始全面风险评估...")

        corr_result = self.calc_correlation_matrix()
        conc_result = self.calc_concentration()
        var_result = self.calc_var()
        dd_result = self.calc_max_drawdown()
        rebalance = self.calc_rebalance_plan()

        # 综合风险评分 (0-100, 越高风险越大)
        risk_score = 0
        # 相关性贡献 (0-25)
        risk_score += min(25, corr_result["avg_correlation"] * 25)
        # 集中度贡献 (0-25)
        risk_score += min(25, conc_result["stock_hhi"] * 50)
        # VaR贡献 (0-25)
        risk_score += min(25, var_result["var_pct"] * 500)
        # 回撤贡献 (0-25)
        risk_score += min(25, dd_result.get("max_drawdown", 0) * 100)

        risk_score = round(min(100, risk_score), 1)

        if risk_score >= 70:
            overall_level = "critical"
        elif risk_score >= 50:
            overall_level = "high"
        elif risk_score >= 30:
            overall_level = "medium"
        else:
            overall_level = "low"

        # 汇总所有预警
        all_alerts = []
        all_alerts.extend(conc_result.get("alerts", []))
        if corr_result["risk_level"] in ("critical", "high"):
            all_alerts.append({
                "level": corr_result["risk_level"],
                "type": "相关性过高",
                "detail": corr_result["detail"],
                "suggestion": "持仓高度相关，同涨同跌风险大，建议配置低相关行业"
            })
        if var_result["risk_level"] in ("critical", "high"):
            all_alerts.append({
                "level": var_result["risk_level"],
                "type": "VaR风险过大",
                "detail": var_result["detail"],
                "suggestion": "单日最大可能亏损过高，建议降低总仓位或分散配置"
            })
        if dd_result.get("risk_level") in ("critical", "high"):
            all_alerts.append({
                "level": dd_result["risk_level"],
                "type": "回撤过大",
                "detail": dd_result["detail"],
                "suggestion": "组合回撤已超警戒线，建议减仓控制风险"
            })

        report = {
            "risk_score": risk_score,
            "overall_level": overall_level,
            "correlation": corr_result,
            "concentration": conc_result,
            "var": var_result,
            "max_drawdown": dd_result,
            "rebalance": rebalance,
            "alerts": all_alerts,
            "scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        logger.info(f"[组合风控] 风险评分: {risk_score}/100 ({overall_level})")
        logger.info(f"  相关性: {corr_result['detail']}")
        logger.info(f"  集中度: {conc_result['detail']}")
        logger.info(f"  VaR: {var_result['detail']}")
        logger.info(f"  回撤: {dd_result.get('detail', 'N/A')}")
        logger.info(f"  再平衡: {rebalance['detail']}")
        logger.info(f"  预警数: {len(all_alerts)}项")

        return report

    # ============================================================
    # 内部辅助方法
    # ============================================================

    def _calc_portfolio_returns(self, lookback: int = 60) -> np.ndarray:
        """计算组合日收益率序列（按市值加权）"""
        returns_list = []
        weights = []

        total_value = sum(
            h.get("shares", 0) * h.get("current_price", h.get("buy_price", 0))
            for h in self.holdings.values()
        )
        if total_value <= 0:
            return None

        for code, holding in self.holdings.items():
            if code not in self.data_dict or len(self.data_dict[code]) < lookback:
                continue
            df = self.data_dict[code]
            closes = df["close"].tail(lookback + 1).values
            daily_ret = np.diff(closes) / closes[:-1]
            returns_list.append(daily_ret)

            value = holding.get("shares", 0) * holding.get("current_price", holding.get("buy_price", 0))
            weights.append(value / total_value)

        if not returns_list:
            return None

        # 对齐长度
        min_len = min(len(r) for r in returns_list)
        returns_array = np.array([r[-min_len:] for r in returns_list])
        weights = np.array(weights[:len(returns_array)])
        weights = weights / weights.sum()  # 归一化

        # 加权组合收益率
        portfolio_returns = np.dot(weights, returns_array)
        return portfolio_returns

    def _get_atr(self, code: str) -> float:
        """获取股票的ATR值"""
        if code not in self.data_dict:
            return 0
        df = self.data_dict[code]
        if "atr" in df.columns and not df["atr"].empty:
            return float(df["atr"].iloc[-1])
        # 手动计算ATR
        if len(df) < 15:
            return 0
        high_low = df["high"] - df["low"]
        high_close = abs(df["high"] - df["close"].shift(1))
        low_close = abs(df["low"] - df["close"].shift(1))
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return float(tr.rolling(14).mean().iloc[-1])

    def _get_stock_name(self, code: str) -> str:
        """获取股票名称"""
        if code in config.STOCK_POOL:
            return config.STOCK_POOL[code].get("名称", code)
        # 从SECTOR_CANDIDATES查找
        for sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).values():
            stocks = sector_info.get("stocks", {})
            if code in stocks:
                return stocks[code].get("名称", code)
        return code

    def _get_stock_type(self, code: str) -> str:
        """获取股票类型"""
        if code in config.STOCK_POOL:
            return config.STOCK_POOL[code].get("类型", "龙头")
        for sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).values():
            stocks = sector_info.get("stocks", {})
            if code in stocks:
                return stocks[code].get("类型", "龙头")
        return "龙头"

    def _default_target_allocation(self) -> dict:
        """默认目标行业配比"""
        # 基于SECTOR_CANDIDATES的weight配置
        targets = {}
        for sector_name, sector_info in getattr(config, 'SECTOR_CANDIDATES', {}).items():
            targets[sector_name] = sector_info.get("weight", 0.10)
        return targets


# ============================================================
# 邮件报告生成
# ============================================================

def generate_risk_report_html(report: dict) -> str:
    """生成组合风险HTML报告"""
    score = report["risk_score"]
    level = report["overall_level"]
    corr = report["correlation"]
    conc = report["concentration"]
    var = report["var"]
    dd = report["max_drawdown"]
    rebalance = report["rebalance"]
    alerts = report["alerts"]

    level_colors = {"critical": "#FF4D4F", "high": "#FA8C16", "medium": "#FAAD14", "low": "#52C41A"}
    level_text = {"critical": "危险", "high": "偏高", "medium": "中等", "low": "良好"}
    color = level_colors.get(level, "#333")

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:"Microsoft YaHei",Arial,sans-serif;margin:0;padding:20px;background:#f5f5f5}}
.container{{max-width:900px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:25px 30px;border-radius:12px 12px 0 0}}
.header h1{{margin:0;font-size:20px}}
.content{{background:#fff;padding:25px 30px;border-radius:0 0 12px 12px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
.score-box{{text-align:center;padding:20px;margin:15px 0;background:#f9f9f9;border-radius:12px}}
.score-num{{font-size:48px;font-weight:bold;color:{color}}}
.score-label{{font-size:14px;color:#666;margin-top:5px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin:15px 0}}
.card{{background:#fafafa;border-radius:8px;padding:15px;border-left:4px solid #1890FF}}
.card h3{{margin:0 0 8px;font-size:14px;color:#333}}
.card .value{{font-size:18px;font-weight:bold;color:#333}}
.card .sub{{font-size:12px;color:#888;margin-top:4px}}
.alert-item{{padding:10px 14px;margin:6px 0;border-radius:6px;font-size:13px}}
.alert-critical{{background:#FFF1F0;border-left:3px solid #FF4D4F}}
.alert-warning{{background:#FFF7E6;border-left:3px solid #FA8C16}}
.action-item{{padding:10px 14px;margin:6px 0;background:#F0F5FF;border-radius:6px;font-size:13px}}
.footer{{text-align:center;color:#bbb;font-size:11px;margin-top:20px;padding-top:15px;border-top:1px solid #eee}}
</style></head><body><div class="container">
<div class="header"><h1>组合风险管理报告</h1>
<div style="font-size:12px;opacity:.8;margin-top:5px">{report['scan_time']} | 总资金{config.TOTAL_CAPITAL:,.0f}元</div></div>
<div class="content">
<div class="score-box"><div class="score-num">{score}</div>
<div class="score-label">综合风险评分（0-100）| 等级: <b style="color:{color}">{level_text.get(level,'')}</b></div></div>

<div class="grid">
<div class="card"><h3>相关性分析</h3><div class="value">{corr['avg_correlation']:.3f}</div>
<div class="sub">平均相关系数 | 分散化得分{corr['diversification_score']:.0f}/100</div></div>
<div class="card"><h3>集中度(HHI)</h3><div class="value">{conc['stock_hhi']:.3f}</div>
<div class="sub">个股HHI | 行业HHI={conc['sector_hhi']:.3f}</div></div>
<div class="card"><h3>VaR风险</h3><div class="value">{var['var_95']:,.0f}元</div>
<div class="sub">95%置信度日VaR | 占比{var['var_pct']:.2%}</div></div>
<div class="card"><h3>最大回撤</h3><div class="value">{dd.get('max_drawdown',0):.2%}</div>
<div class="sub">当前回撤{dd.get('current_drawdown',0):.2%}</div></div>
</div>
"""

    # 高相关对
    if corr["high_corr_pairs"]:
        html += '<div style="margin:15px 0"><b>高相关持仓对（同涨同跌风险）:</b><br>'
        for pair in corr["high_corr_pairs"]:
            html += f'<span style="color:#FF4D4F;font-size:13px">⚠ {pair[3]}({pair[0]}) ↔ {pair[4]}({pair[1]}) 相关系数{pair[2]:.3f}</span><br>'
        html += '</div>'

    # 预警
    if alerts:
        html += '<div style="margin:15px 0"><b>风险预警:</b>'
        for a in alerts:
            css = "alert-critical" if a["level"] == "critical" else "alert-warning"
            html += f'<div class="alert-item {css}"><b>{a["type"]}</b>: {a["detail"]}<br><span style="color:#1890FF">→ {a["suggestion"]}</span></div>'
        html += '</div>'

    # 再平衡
    if rebalance["actions"]:
        html += '<div style="margin:15px 0"><b>再平衡建议:</b>'
        for a in rebalance["actions"]:
            html += f'<div class="action-item"><b>[{a["action"]}]</b> {a["name"]}({a["code"]}) {a["shares"]}股 ≈{a["amount"]:,.0f}元 | {a["reason"]}</div>'
        html += f'<div style="font-size:12px;color:#888;margin-top:8px">预估交易成本: {rebalance["rebalance_cost"]:,.0f}元</div></div>'

    html += f"""<div class="footer">组合风险管理模块自动生成 | 仅供参考<br>年化波动率{var['annual_volatility']:.1%} | CVaR(95%)={var['cvar_95']:,.0f}元</div>
</div></div></body></html>"""

    return html


def send_risk_report_email(report: dict) -> bool:
    """发送组合风险报告邮件"""
    from notify.email_notify import send_email
    level_text = {"critical": "危险", "high": "偏高", "medium": "中等", "low": "良好"}
    subject = (f"[组合风控] 风险评分{report['risk_score']}/100 "
               f"({level_text.get(report['overall_level'], '')}) | "
               f"{len(report['alerts'])}项预警")
    html_content = generate_risk_report_html(report)
    return send_email(subject, html_content)
