# -*- coding: utf-8 -*-
"""
模拟盘报告区块（report）
========================
把模拟盘净值进度与毕业判定嵌入现有盘后/周报，静默降级：无数据或数据不足时
自动隐藏并注明原因，禁止编造数据。开关关闭时不渲染。
"""

import logging

from .tracker import PaperTracker
from .graduation import GraduationJudge

logger = logging.getLogger(__name__)


def _paper_enabled(config=None) -> bool:
    try:
        cfg = config
        if cfg is None:
            import config as _c
            cfg = getattr(_c, "PAPER_TRADING_CONFIG", {}) or {}
        return bool(cfg.get("enabled", False))
    except Exception:
        return False


def render_paper_section_html(strategy_name: str = None, comparator_result: dict = None,
                              tracker: PaperTracker = None, judge: GraduationJudge = None,
                              config: dict = None) -> str:
    """渲染模拟盘报告区块（HTML）。

    - 开关关闭 / 无任何 paper 数据 → 返回空串（静默隐藏）；
    - 数据不足毕业天数 → 显示进度并注明"数据不足，暂不判定"。
    """
    try:
        if not _paper_enabled(config):
            return ""
        tracker = tracker or PaperTracker()
        judge = judge or GraduationJudge(config=config, tracker=tracker)

        names = [strategy_name] if strategy_name else tracker.list_strategies()
        names = [n for n in names if n]
        blocks = []
        for name in names:
            days = tracker.trading_days(name)
            if days <= 0:
                continue
            eval_res = judge.evaluate(
                name, comparator_result if name == strategy_name else None)
            m = eval_res.get("metrics", {})
            need = judge.graduation_days
            pct = min(100, int(days / need * 100)) if need else 0
            state = eval_res.get("next_state")
            state_color = {"live": "#389e0d", "paper": "#d46b08",
                           "rejected": "#cf1322"}.get(state, "#595959")
            state_txt = {"live": "✅ 可毕业转实盘(待人工确认)",
                         "paper": "⏳ 模拟盘观察中(延期)",
                         "rejected": "❌ 建议淘汰"}.get(state, state)

            rows = (
                f"<div style='font-size:13px;margin-top:6px'>"
                f"期末净值 <b>{m.get('final_nav',1):.4f}</b>"
                f"（收益 {m.get('total_return',0)*100:+.2f}%） | "
                f"夏普 <b>{m.get('sharpe',0):.3f}</b> | "
                f"最大回撤 <b>{m.get('max_drawdown',0)*100:.2f}%</b> | "
                f"运行 {days}/{need} 交易日</div>"
                f"<div style='background:#f0f0f0;border-radius:4px;height:8px;"
                f"margin-top:4px;overflow:hidden'>"
                f"<div style='width:{pct}%;background:#1890ff;height:8px'></div></div>")

            if days < need:
                rows += (f"<div style='font-size:12px;color:#8c8c8c;margin-top:4px'>"
                         f"数据不足（{days}<{need}交易日），暂不做毕业判定</div>")
            else:
                rows += (f"<div style='font-size:13px;margin-top:6px;color:{state_color};"
                         f"font-weight:bold'>{state_txt}</div>")
                if eval_res.get("failed_items"):
                    items = "".join(f"<li>{x}</li>" for x in eval_res["failed_items"])
                    rows += (f"<ul style='font-size:12px;color:#d46b08;margin:4px 0 "
                             f"0 18px'>{items}</ul>")
                dev = eval_res.get("nav_deviation_mean")
                if dev is not None:
                    rows += (f"<div style='font-size:12px;color:#595959;margin-top:4px'>"
                             f"模拟盘vs回测净值偏差均值 {dev*100:.2f}%</div>")

            blocks.append(
                f"<div style='border-top:1px dashed #d9d9d9;padding:6px 0'>"
                f"<div style='font-weight:bold;color:#1d39c4'>{name}</div>{rows}</div>")

        if not blocks:
            return ""
        return (f"<div style='max-width:1000px;margin:10px auto;background:#f9f0ff;"
                f"border:1px solid #d3adf7;border-radius:8px;padding:12px 18px'>"
                f"<div style='font-weight:bold;color:#531dab;font-size:14px'>"
                f"🧪 模拟盘毕业进度</div>{''.join(blocks)}"
                f"<div style='font-size:11px;color:#8c8c8c;margin-top:6px'>"
                f"口径：模拟盘净值/夏普/回撤由回测同一套撮合与费率引擎产出；"
                f"毕业转实盘仅生成建议，需人工确认。</div></div>")
    except Exception as e:
        logger.warning(f"[模拟盘区块] 渲染失败(不影响报告): {e}")
        return ""
