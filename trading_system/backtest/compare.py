# NOTE: 仅供 validate_v8.py/tests 引用，未接入生产链路
"""
策略对比框架（V3.2新增）
========================
同一历史区间并行运行多策略，输出对比表

核心功能:
  1. 多策略同条件PK（收益/回撤/胜率/夏普/换手）
  2. 分年度/分市场环境对比
  3. 信号重叠度分析（两策略同时触发比例）
  4. 最优策略推荐（基于夏普×胜率综合评分）

使用方式:
    from backtest.compare import StrategyComparator
    comp = StrategyComparator()
    result = comp.compare([
        {"name": "CANSLIM_V3.2", "trades": trades_a},
        {"name": "均值回归", "trades": trades_b},
    ])
"""

import numpy as np
import logging

logger = logging.getLogger(__name__)


class StrategyComparator:
    """策略对比器"""

    def compare(self, strategies: list, initial_capital: float = 730000,
                risk_free_rate: float = 0.02) -> dict:
        """
        对比多个策略的表现
        
        参数:
            strategies: [{"name": str, "trades": [{"pnl_pct", "date"}]}, ...]
            initial_capital: 初始资金
            risk_free_rate: 无风险利率（年化）
        
        返回:
            {
                "ranking": [按综合评分排序],
                "details": {name: {metrics}},
                "recommendation": str,
            }
        """
        details = {}
        
        for strat in strategies:
            name = strat["name"]
            trades = strat.get("trades", [])
            if not trades:
                details[name] = {"error": "无交易记录"}
                continue
            
            metrics = self._calc_metrics(trades, initial_capital, risk_free_rate)
            details[name] = metrics
        
        # 综合评分排名: 夏普×0.4 + 胜率×0.3 + (1-最大回撤)×0.3
        ranking = []
        for name, m in details.items():
            if "error" in m:
                continue
            score = (
                m.get("sharpe", 0) * 0.4 +
                m.get("win_rate", 0) * 0.3 +
                (1 - m.get("max_drawdown", 1)) * 0.3
            )
            ranking.append({"name": name, "score": round(score, 4), **m})
        
        ranking.sort(key=lambda x: x["score"], reverse=True)
        
        recommendation = ""
        if ranking:
            best = ranking[0]
            recommendation = (
                f"推荐策略: {best['name']} "
                f"(夏普{best.get('sharpe', 0):.2f}, "
                f"胜率{best.get('win_rate', 0):.1%}, "
                f"回撤{best.get('max_drawdown', 0):.1%})"
            )
        
        return {
            "ranking": ranking,
            "details": details,
            "recommendation": recommendation,
            "n_strategies": len(strategies),
        }

    def _calc_metrics(self, trades: list, initial_capital: float,
                      risk_free_rate: float) -> dict:
        """计算单策略的完整指标"""
        pnls = np.array([t.get("pnl_pct", 0) for t in trades])
        n = len(pnls)
        
        if n == 0:
            return {"error": "无交易"}
        
        # 基础统计
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        win_rate = len(wins) / n
        avg_win = wins.mean() if len(wins) > 0 else 0
        avg_loss = abs(losses.mean()) if len(losses) > 0 else 0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else 0
        
        # 收益曲线
        equity = initial_capital
        peak = initial_capital
        max_dd = 0
        equity_curve = [initial_capital]
        for p in pnls:
            equity *= (1 + p)
            equity_curve.append(equity)
            peak = max(peak, equity)
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)
        
        total_return = (equity - initial_capital) / initial_capital
        
        # 夏普比率（假设每笔交易间隔5天）
        if pnls.std() > 0:
            sharpe_per_trade = pnls.mean() / pnls.std()
            annualize_factor = np.sqrt(252 / 5)  # 年化
            sharpe = sharpe_per_trade * annualize_factor
        else:
            sharpe = 0
        
        # 最大连续亏损
        max_consec_loss = 0
        current_streak = 0
        for p in pnls:
            if p < 0:
                current_streak += 1
                max_consec_loss = max(max_consec_loss, current_streak)
            else:
                current_streak = 0
        
        # 期望值
        expectancy = pnls.mean()
        
        return {
            "total_trades": n,
            "total_return": round(total_return, 4),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(-avg_loss, 4),
            "expectancy": round(expectancy, 4),
            "sharpe": round(sharpe, 4),
            "max_drawdown": round(max_dd, 4),
            "max_consecutive_loss": max_consec_loss,
            "final_equity": round(equity, 0),
        }

    def signal_overlap(self, trades_a: list, trades_b: list) -> dict:
        """
        分析两策略信号重叠度
        
        返回:
            {"overlap_ratio": float, "a_only": int, "b_only": int, "both": int}
        """
        dates_a = set(t.get("date", "") for t in trades_a if t.get("date"))
        dates_b = set(t.get("date", "") for t in trades_b if t.get("date"))
        
        both = dates_a & dates_b
        a_only = dates_a - dates_b
        b_only = dates_b - dates_a
        total = len(dates_a | dates_b)
        
        return {
            "overlap_ratio": round(len(both) / total, 4) if total > 0 else 0,
            "a_only": len(a_only),
            "b_only": len(b_only),
            "both": len(both),
            "total_unique": total,
        }
