# -*- coding: utf-8 -*-
"""
V4.1(P3): 滑点执行归因区块（综合分析报告版）
==============================================
G11已把滑点归因接入周报；本模块将摘要版接入每日综合分析报告，
并把 suggest_backtest_adjustment 的校准建议以"待确认"形式展示。

风控约束: 只展示建议，绝不调用 apply_backtest_adjustment 自动写回config。
"""

import logging

logger = logging.getLogger(__name__)


def render_slippage_section_html(lookback_days: int = 7) -> str:
    """
    生成滑点归因HTML区块。无记录/异常返回空字符串。
    """
    try:
        from execution.slippage_tracker import SlippageTracker
        tracker = SlippageTracker()
        slip = tracker.generate_report(lookback_days=lookback_days)
        if slip.get("total_trades", 0) <= 0:
            return ""

        bd = slip.get("by_direction", {})
        buy_avg = bd.get("buy", {}).get("avg", 0)
        sell_avg = bd.get("sell", {}).get("avg", 0)
        rows = (f"<div style='font-size:13px;margin-top:6px'>"
                f"近{lookback_days}日 {slip['total_trades']}笔成交 | "
                f"平均滑点 <b>{slip['avg_slippage']*100:+.3f}%</b> | "
                f"滑点成本 {slip['slippage_cost']:,.0f}元 | "
                f"买{buy_avg*100:+.3f}% / 卖{sell_avg*100:+.3f}%</div>")

        # 高滑点标的警示
        high = slip.get("high_slippage_stocks", [])
        if high:
            _hs_txt = "、".join(
                f"{h['code']}(均{h['avg_slippage']*100:.2f}%/{h['count']}笔)"
                for h in high[:3])
            rows += (f"<div style='font-size:12px;color:#d46b08;margin-top:4px'>"
                     f"⚠️ 高滑点标的: {_hs_txt}，建议改限价分批执行</div>")

        # 回测校准建议（只展示为"待确认"，绝不自动应用）
        try:
            adj = tracker.suggest_backtest_adjustment()
            if adj.get("should_adjust"):
                rows += (f"<div style='font-size:12px;color:#cf1322;margin-top:4px'>"
                         f"📐 回测滑点校准建议(待人工确认，未自动生效): "
                         f"当前假设{adj['current_assumption']:.3%} → "
                         f"建议{adj['suggested_value']:.3%} | {adj['reason']}</div>")
            else:
                rows += (f"<div style='font-size:11px;color:#8c8c8c;margin-top:4px'>"
                         f"📐 回测滑点校准: {adj.get('reason', '在合理范围内')}</div>")
        except Exception:
            pass

        return (f"<div style='max-width:1000px;margin:10px auto;background:#f0f5ff;"
                f"border:1px solid #adc6ff;border-radius:8px;padding:12px 18px'>"
                f"<div style='font-weight:bold;color:#1d39c4;font-size:14px'>"
                f"💸 滑点执行归因（近{lookback_days}日）</div>{rows}"
                f"<div style='font-size:11px;color:#8c8c8c;margin-top:4px'>"
                f"口径: 信号价 vs 实际成交价的偏差统计；校准建议需人工确认后"
                f"才会写入回测参数，系统不会自动修改。</div></div>")
    except Exception as e:
        logger.warning(f"[滑点区块] 渲染失败(不影响报告): {e}")
        return ""
