"""
量化智能面板 - 报告增强引擎
============================
将P0-P4量化能力注入分析报告，打造"量化决策终端"

核心理念: 机构有速度和资金，我们有纪律和适应性。
报告要回答5个机构不会告诉你的问题:
1. 我们的优势还在吗？(因子IC/IR)
2. 现在是什么环境？(牛熊震荡三态)
3. 主力在干什么？(筹码分布+量价)
4. 信号有多可靠？(多策略共识度)
5. 组合风险可控吗？(相关性+集中度)

使用方式:
    from quant.report_enhancer import QuantReportEnhancer
    enhancer = QuantReportEnhancer()
    panel_data = enhancer.compute_panel(holdings, data_dict, date)
    # panel_data 传给报告构建函数
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)


class QuantReportEnhancer:
    """量化报告增强器"""

    def __init__(self):
        self.ic_history = []  # 因子IC历史
        self.sentiment_history = []  # 情绪历史

    def compute_panel(self, holdings: dict, data_dict: dict,
                      date: str = None, signals: list = None) -> dict:
        """
        计算完整的量化智能面板数据

        返回:
            {
                "factor_health": {...},      # 因子有效性
                "market_regime": {...},      # 市场环境
                "chip_analysis": {...},      # 筹码分析
                "signal_confidence": {...},  # 信号置信度
                "portfolio_risk": {...},     # 组合风险
                "risk_status": {...},        # 风控状态
                "overall_score": float,      # 综合评分(0-100)
            }
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        panel = {
            "factor_health": self._calc_factor_health(data_dict, date),
            "market_regime": self._calc_market_regime(data_dict, date),
            "chip_analysis": self._calc_chip_analysis(holdings, data_dict),
            "signal_confidence": self._calc_signal_confidence(holdings, data_dict, signals),
            "portfolio_risk": self._calc_portfolio_risk(holdings, data_dict),
            "risk_status": self._calc_risk_status(holdings, data_dict),
        }

        # 综合评分
        panel["overall_score"] = self._calc_overall_score(panel)

        return panel

    # ============================================================
    # 一、因子有效性 (我们的优势还在吗？)
    # ============================================================

    def _calc_factor_health(self, data_dict: dict, date: str) -> dict:
        """
        计算因子有效性指标
        - 动量因子IC: 过去20日涨幅 vs 未来5日涨幅的相关性
        - 趋势因子IC: 均线多头排列 vs 未来收益
        - 综合判断: 因子是否失效
        """
        if not data_dict:
            return {"status": "无数据", "momentum_ic": 0, "trend_ic": 0, "verdict": "未知"}

        # 收集所有股票的因子值和前瞻收益
        momentum_values = []
        forward_returns = []
        trend_values = []

        for code, df in data_dict.items():
            df_cut = df[df["date"] <= date]
            if len(df_cut) < 30:
                continue

            close = df_cut["close"].values

            # 动量因子: 20日收益率
            if len(close) > 20:
                momentum = (close[-1] - close[-21]) / close[-21]
                momentum_values.append(momentum)

            # 趋势因子: MA5>MA10>MA20 得分
            if len(close) > 20:
                ma5 = np.mean(close[-5:])
                ma10 = np.mean(close[-10:])
                ma20 = np.mean(close[-20:])
                trend_score = int(ma5 > ma10) + int(ma10 > ma20) + int(close[-1] > ma20)
                trend_values.append(trend_score)

            # 前瞻收益: 未来5日（如果有数据）
            df_future = df[df["date"] > date]
            if len(df_future) >= 5:
                future_ret = (df_future["close"].iloc[4] - close[-1]) / close[-1]
                forward_returns.append(future_ret)
            else:
                forward_returns.append(0)

        # 计算IC
        n = min(len(momentum_values), len(forward_returns))
        if n < 10:
            return {"status": "样本不足", "momentum_ic": 0, "trend_ic": 0, "verdict": "数据不足"}

        mom_ic = np.corrcoef(momentum_values[:n], forward_returns[:n])[0, 1] if n > 2 else 0
        trend_ic = np.corrcoef(trend_values[:n], forward_returns[:n])[0, 1] if n > 2 else 0

        # 判断
        avg_ic = (abs(mom_ic) + abs(trend_ic)) / 2
        if avg_ic > 0.1:
            verdict = "因子强势有效"
            status = "excellent"
        elif avg_ic > 0.05:
            verdict = "因子正常有效"
            status = "good"
        elif avg_ic > 0.02:
            verdict = "因子效力减弱"
            status = "warning"
        else:
            verdict = "因子可能失效，谨慎操作"
            status = "danger"

        self.ic_history.append({"date": date, "mom_ic": mom_ic, "trend_ic": trend_ic})

        return {
            "status": status,
            "momentum_ic": round(mom_ic, 4),
            "trend_ic": round(trend_ic, 4),
            "avg_ic": round(avg_ic, 4),
            "verdict": verdict,
        }

    # ============================================================
    # 二、市场环境 (现在是什么环境？)
    # ============================================================

    def _calc_market_regime(self, data_dict: dict, date: str) -> dict:
        """
        牛熊震荡三态判断 + 市场情绪

        判断逻辑:
        - 取所有股票的中位数表现作为"大盘代理"
        - MA20>MA60 且 价格>MA20 = 牛市
        - MA20<MA60 且 价格<MA20 = 熊市
        - 其他 = 震荡
        """
        if not data_dict:
            return {"regime": "unknown", "sentiment": 50, "position_limit": 0.7}

        # 收集市场宽度数据
        above_ma20_count = 0
        total_count = 0
        daily_changes = []
        limit_up_count = 0

        for code, df in data_dict.items():
            df_cut = df[df["date"] <= date]
            if len(df_cut) < 60:
                continue

            close = df_cut["close"].values
            total_count += 1

            ma20 = np.mean(close[-20:])
            ma60 = np.mean(close[-60:])

            if close[-1] > ma20:
                above_ma20_count += 1

            # 日涨跌幅
            if len(close) > 1:
                change = (close[-1] - close[-2]) / close[-2]
                daily_changes.append(change)
                if change >= 0.095:
                    limit_up_count += 1

        if total_count == 0:
            return {"regime": "unknown", "sentiment": 50, "position_limit": 0.7}

        # 市场宽度
        breadth = above_ma20_count / total_count  # 站上MA20的比例

        # 情绪指标
        avg_change = np.mean(daily_changes) if daily_changes else 0
        advance_ratio = sum(1 for c in daily_changes if c > 0) / len(daily_changes) if daily_changes else 0.5

        # 三态判断
        if breadth > 0.6 and avg_change > 0:
            regime = "牛市"
            position_limit = 1.0
            regime_color = "#2e7d32"
        elif breadth < 0.35 and avg_change < 0:
            regime = "熊市"
            position_limit = 0.3
            regime_color = "#d32f2f"
        else:
            regime = "震荡"
            position_limit = 0.6
            regime_color = "#f57c00"

        # 综合情绪分(0-100)
        sentiment = int(
            breadth * 40 +
            advance_ratio * 30 +
            min(limit_up_count / 20, 1) * 20 +
            (1 if avg_change > 0 else 0) * 10
        )

        self.sentiment_history.append({"date": date, "sentiment": sentiment, "regime": regime})

        return {
            "regime": regime,
            "regime_color": regime_color,
            "sentiment": sentiment,
            "breadth": round(breadth, 2),
            "advance_ratio": round(advance_ratio, 2),
            "limit_up_count": limit_up_count,
            "position_limit": position_limit,
            "advice": f"{regime}环境，建议仓位{position_limit:.0%}" +
                     ("，积极进攻" if regime == "牛市" else "，防守为主" if regime == "熊市" else "，灵活应对"),
        }

    # ============================================================
    # 三、筹码分析 (主力在干什么？)
    # ============================================================

    def _calc_chip_analysis(self, holdings: dict, data_dict: dict) -> dict:
        """
        持仓筹码分布分析
        - 获利盘比例: 当前价上方有多少套牢盘
        - 筹码集中度: 筹码是分散还是集中
        - 主力成本估算: 成交量加权均价
        """
        results = []

        for code, pos in holdings.items():
            df = data_dict.get(code)
            if df is None or len(df) < 30:
                continue

            close = df["close"].values
            volume = df["volume"].values
            current_price = close[-1]

            # 近60日筹码分布
            lookback = min(60, len(close))
            prices = close[-lookback:]
            volumes = volume[-lookback:]

            # 获利盘比例
            profit_ratio = np.sum(volumes[prices <= current_price]) / np.sum(volumes) if np.sum(volumes) > 0 else 0.5

            # 筹码集中度 (变异系数越小越集中)
            cv = np.std(prices) / np.mean(prices) if np.mean(prices) > 0 else 1
            concentration = max(0, 1 - cv * 5)  # 归一化到0-1

            # 主力成本估算 (VWAP)
            vwap = np.average(prices, weights=volumes) if np.sum(volumes) > 0 else current_price

            # 当前价vs主力成本
            vs_main_cost = (current_price - vwap) / vwap * 100

            results.append({
                "code": code,
                "profit_ratio": round(profit_ratio, 2),
                "concentration": round(concentration, 2),
                "vwap": round(vwap, 2),
                "vs_main_cost": round(vs_main_cost, 1),
                "interpretation": self._interpret_chip(profit_ratio, vs_main_cost),
            })

        # 汇总
        avg_profit = np.mean([r["profit_ratio"] for r in results]) if results else 0.5
        avg_vs_cost = np.mean([r["vs_main_cost"] for r in results]) if results else 0

        return {
            "holdings_detail": results,
            "avg_profit_ratio": round(avg_profit, 2),
            "avg_vs_main_cost": round(avg_vs_cost, 1),
            "summary": self._summarize_chips(avg_profit, avg_vs_cost),
        }

    def _interpret_chip(self, profit_ratio: float, vs_cost: float) -> str:
        """解读单只股票筹码状态"""
        if profit_ratio > 0.8 and vs_cost > 5:
            return "获利盘多，注意抛压"
        elif profit_ratio < 0.3 and vs_cost < -5:
            return "套牢盘重，反弹有压力"
        elif abs(vs_cost) < 3:
            return "接近主力成本，关注方向"
        elif vs_cost > 0:
            return "在主力成本上方，相对安全"
        else:
            return "在主力成本下方，谨慎"

    def _summarize_chips(self, avg_profit: float, avg_vs_cost: float) -> str:
        if avg_profit > 0.7:
            return "持仓整体获利盘较多，注意高位抛压"
        elif avg_profit < 0.4:
            return "持仓套牢盘较重，反弹可能遇阻"
        else:
            return "筹码分布中性，关注量价配合"

    # ============================================================
    # 四、信号置信度 (信号有多可靠？)
    # ============================================================

    def _calc_signal_confidence(self, holdings: dict, data_dict: dict,
                                 signals: list = None) -> dict:
        """
        多策略共识度分析
        - 单策略信号: 置信度低(30%)
        - 双策略共振: 置信度中(60%)
        - 三策略共振: 置信度高(90%)
        """
        if not signals:
            return {"avg_confidence": 0, "high_confidence_count": 0, "detail": []}

        detail = []
        for code, sig in signals if isinstance(signals, list) else []:
            if not isinstance(sig, dict):
                continue

            # 计算共振策略数
            strategy_count = 0
            if sig.get("momentum_signal"):
                strategy_count += 1
            if sig.get("trend_signal"):
                strategy_count += 1
            if sig.get("volume_signal"):
                strategy_count += 1

            # 置信度
            confidence = min(0.3 * strategy_count + 0.1, 0.95)

            detail.append({
                "code": code,
                "strategy_count": strategy_count,
                "confidence": round(confidence, 2),
                "level": "高" if confidence > 0.7 else "中" if confidence > 0.4 else "低",
            })

        avg_conf = np.mean([d["confidence"] for d in detail]) if detail else 0
        high_conf = sum(1 for d in detail if d["confidence"] > 0.7)

        return {
            "avg_confidence": round(avg_conf, 2),
            "high_confidence_count": high_conf,
            "total_signals": len(detail),
            "detail": detail[:5],  # 只显示前5个
            "advice": "多策略共振信号优先" if high_conf > 0 else "当前无高置信信号，耐心等待",
        }

    # ============================================================
    # 五、组合风险 (风险可控吗？)
    # ============================================================

    def _calc_portfolio_risk(self, holdings: dict, data_dict: dict) -> dict:
        """
        组合级风险指标
        - 持仓相关性: 持仓间平均相关系数
        - 行业集中度: 最大行业占比
        - 组合波动率: 加权波动率
        - 最大回撤: 近20日组合回撤
        """
        if len(holdings) < 2:
            return {"correlation": 0, "concentration": 1, "volatility": 0, "max_dd": 0}

        # 收集收益率序列
        returns_dict = {}
        for code in holdings:
            df = data_dict.get(code)
            if df is not None and len(df) > 20:
                ret = df["close"].pct_change().dropna().values[-20:]
                if len(ret) >= 15:
                    returns_dict[code] = ret

        if len(returns_dict) < 2:
            return {"correlation": 0, "concentration": 1, "volatility": 0, "max_dd": 0}

        # 相关性矩阵
        codes = list(returns_dict.keys())
        n = min(len(r) for r in returns_dict.values())
        returns_matrix = np.array([returns_dict[c][-n:] for c in codes])
        corr_matrix = np.corrcoef(returns_matrix)

        # 平均相关性（排除对角线）
        mask = ~np.eye(len(codes), dtype=bool)
        avg_corr = np.mean(corr_matrix[mask]) if mask.any() else 0

        # 组合波动率（等权简化）
        weights = np.ones(len(codes)) / len(codes)
        port_var = weights @ corr_matrix @ weights * np.mean([np.var(r) for r in returns_dict.values()])
        port_vol = np.sqrt(port_var) * np.sqrt(252)  # 年化

        # 近20日最大回撤
        port_returns = np.mean(returns_matrix, axis=0)
        nav = np.cumprod(1 + port_returns)
        peak = np.maximum.accumulate(nav)
        dd = (nav - peak) / peak
        max_dd = abs(dd.min()) if len(dd) > 0 else 0

        # 风险评级
        if avg_corr > 0.7:
            risk_level = "高"
            risk_advice = "持仓高度相关，分散化不足"
        elif avg_corr > 0.4:
            risk_level = "中"
            risk_advice = "相关性适中"
        else:
            risk_level = "低"
            risk_advice = "分散化良好"

        return {
            "correlation": round(avg_corr, 2),
            "volatility": round(port_vol, 4),
            "max_dd_20d": round(max_dd, 4),
            "risk_level": risk_level,
            "risk_advice": risk_advice,
            "position_count": len(holdings),
        }

    # ============================================================
    # 六、风控状态 (止损/止盈/择时)
    # ============================================================

    def _calc_risk_status(self, holdings: dict, data_dict: dict) -> dict:
        """
        每只持仓的风控状态
        - ATR动态止损价
        - 移动止盈状态（距高点回落%）
        - 时间止损预警
        """
        details = []

        for code, pos in holdings.items():
            df = data_dict.get(code)
            if df is None or len(df) < 20:
                continue

            close = df["close"].values
            buy_price = pos.get("buy_price", 0)
            buy_date = pos.get("buy_date", "")

            # ATR计算
            highs = df["high"].values[-15:]
            lows = df["low"].values[-15:]
            closes = df["close"].values[-15:]
            tr_list = []
            for i in range(1, len(highs)):
                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                tr_list.append(tr)
            atr = np.mean(tr_list) if tr_list else 0

            # ATR止损价
            atr_stop = buy_price - 2 * atr if atr > 0 else buy_price * 0.9

            # 移动止盈
            highest = pos.get("highest_price", np.max(close[-20:]))
            drawdown_from_high = (highest - close[-1]) / highest if highest > 0 else 0

            # 浮盈
            pnl_pct = (close[-1] - buy_price) / buy_price if buy_price > 0 else 0

            # 持仓天数
            try:
                hold_days = (datetime.now() - datetime.strptime(buy_date, "%Y-%m-%d")).days
            except:
                hold_days = 0

            # 风控状态判断
            if close[-1] <= atr_stop:
                status = "触发止损"
                status_color = "#d32f2f"
            elif drawdown_from_high > 0.08 and pnl_pct > 0:
                status = "移动止盈预警"
                status_color = "#e65100"
            elif hold_days > 25 and pnl_pct < 0.03:
                status = "时间止损预警"
                status_color = "#f57c00"
            else:
                status = "正常"
                status_color = "#2e7d32"

            details.append({
                "code": code,
                "atr_stop": round(atr_stop, 2),
                "trailing_dd": round(drawdown_from_high, 4),
                "pnl_pct": round(pnl_pct, 4),
                "hold_days": hold_days,
                "status": status,
                "status_color": status_color,
            })

        # 汇总
        alert_count = sum(1 for d in details if d["status"] != "正常")

        return {
            "details": details,
            "alert_count": alert_count,
            "total_positions": len(details),
            "summary": f"{alert_count}只触发风控预警" if alert_count > 0 else "全部持仓风控正常",
        }

    # ============================================================
    # 七、综合评分
    # ============================================================

    def _calc_overall_score(self, panel: dict) -> float:
        """
        综合评分(0-100):
        - 因子有效性: 25分
        - 市场环境: 25分
        - 筹码健康: 20分
        - 组合风险: 15分
        - 风控状态: 15分
        """
        score = 0

        # 因子有效性 (25分)
        fh = panel.get("factor_health", {})
        ic = fh.get("avg_ic", 0)
        score += min(ic * 200, 25)  # IC=0.125 -> 25分

        # 市场环境 (25分)
        mr = panel.get("market_regime", {})
        sentiment = mr.get("sentiment", 50)
        score += sentiment * 0.25

        # 筹码健康 (20分)
        ca = panel.get("chip_analysis", {})
        profit_ratio = ca.get("avg_profit_ratio", 0.5)
        # 获利盘50-70%最健康
        if 0.5 <= profit_ratio <= 0.7:
            score += 20
        elif 0.4 <= profit_ratio <= 0.8:
            score += 15
        else:
            score += 8

        # 组合风险 (15分)
        pr = panel.get("portfolio_risk", {})
        corr = pr.get("correlation", 0.5)
        score += max(0, 15 * (1 - corr))  # 相关性越低分越高

        # 风控状态 (15分)
        rs = panel.get("risk_status", {})
        alerts = rs.get("alert_count", 0)
        total = rs.get("total_positions", 1)
        score += 15 * (1 - alerts / max(total, 1))

        return round(min(score, 100), 1)
