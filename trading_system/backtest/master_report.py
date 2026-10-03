# -*- coding: utf-8 -*-
"""
Stage 13 · 总报告 HTML（full-history-backtest 新增）
=====================================================
把编排器 ctx（各 Stage 结果）+ record（存档元信息）渲染为单文件 HTML 研报：
  - 封面：七道门总览（红/黄/绿）+ 一句话结论；
  - 数据质量审计；净值曲线（策略 vs 基准）+ 回撤；月度收益热力图；
  - 参数热力图与高原判定；Walk-Forward；蒙特卡洛分布；历史情景卡片；
  - DSR/PBO；成本敏感性；分段绩效；因子分层；
  - 附录：口径、假设、已知限制（幸存者偏差/数据区间/退市股）、生成时间与 commit。

纪律：任一 Stage 缺失/skipped/fail 均如实呈现，禁止在数据缺失时输出"全部正常"。
配色遵循 A 股习惯：红涨绿跌。纯字符串拼接，不依赖第三方模板引擎。
"""

import datetime
import html
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

logger = logging.getLogger(__name__)

OUTPUT_DIR = getattr(config, "OUTPUT_DIR", os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output"))

GATE_META = [
    ("G1_data", "G1 数据质量", "无阻断级数据错误，警告项已声明"),
    ("G2_oos", "G2 样本外", "Walk-Forward 样本外平均夏普 > 0.8"),
    ("G3_dsr", "G3 DSR", "Deflated Sharpe > 0.95（PBO < 0.5）"),
    ("G4_cost", "G4 成本稳健", "成本×2 后夏普退化 < 30%"),
    ("G5_plateau", "G5 参数高原", "最优参数邻域退化 < 15%，非孤点"),
    ("G6_stress", "G6 压力存活", "五个历史情景均存活（不腰斩）"),
    ("G7_cross", "G7 双引擎一致", "总收益差 < 3%、夏普差 < 0.3"),
]

_COLOR = {"green": "#c0392b", "yellow": "#d4a017", "red": "#1e8449"}  # A股:红=好/涨,绿=差/跌
_GATE_LABEL = {"green": "通过", "yellow": "观察", "red": "不通过"}
# 门判定语义色（与涨跌色区分）：绿=通过, 黄=观察, 红=不通过
_GATE_BG = {"green": "#e8f6ef", "yellow": "#fdf6e3", "red": "#fdecec"}
_GATE_FG = {"green": "#1e8449", "yellow": "#b8860b", "red": "#c0392b"}


def _e(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def _pct(x, digits=2) -> str:
    try:
        return f"{float(x) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "N/A"


def _num(x, digits=4) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return "N/A"


def _skipped_block(d: dict, title: str) -> str:
    reason = d.get("reason") or d.get("error") or "无数据"
    status = d.get("status", "skipped")
    return (f'<p class="note">【{title}】状态: {_e(status)} — {_e(reason)}'
            f'（数据缺失/未实现，未做任何"正常"假设）</p>')


def _overall(gates: dict) -> str:
    vals = list(gates.values())
    if vals and all(v == "green" for v in vals):
        return "green"
    if any(v == "red" for v in vals):
        return "red"
    return "yellow"


def _build_equity_series(ctx: dict):
    """构造策略与基准的归一化净值序列（供 SVG 曲线）。返回 {name: [(date,val)...]} 或 {}。"""
    try:
        from backtest import master_common
        daily = master_common.equal_weight_daily_returns(ctx.get("data_dict", {}), ctx.get("codes", []))
        if daily is None or len(daily) < 5:
            return {}
        out = {}
        eq = (1.0 + daily).cumprod()
        out["策略(等权)"] = list(zip([str(i) for i in eq.index], [float(v) for v in eq.values]))
        dd = ctx.get("data_dict", {})
        if "000300" in dd:
            bdf = dd["000300"].copy()
            bdf["date"] = bdf["date"].astype(str)
            bdf = bdf.dropna(subset=["close"]).drop_duplicates(subset=["date"]).set_index("date")["close"].astype(float)
            bdf = bdf.reindex(eq.index).ffill().dropna()
            if len(bdf) > 1:
                bnorm = bdf / float(bdf.iloc[0])
                out["沪深300"] = list(zip([str(i) for i in bnorm.index], [float(v) for v in bnorm.values]))
        return out
    except Exception as e:
        logger.debug(f"净值序列构造失败: {e}")
        return {}


def _svg_line(series: dict, width=880, height=260) -> str:
    """极简多序列 SVG 折线图（无第三方依赖）。"""
    if not series:
        return '<p class="note">净值曲线：数据不足，未绘制。</p>'
    all_vals = [v for pts in series.values() for _, v in pts]
    if not all_vals:
        return '<p class="note">净值曲线：数据不足，未绘制。</p>'
    vmin, vmax = min(all_vals), max(all_vals)
    span = (vmax - vmin) or 1.0
    n_max = max(len(pts) for pts in series.values())
    colors = {"策略(等权)": "#c0392b", "沪深300": "#2c3e50"}
    pad_l, pad_b, pad_t = 40, 24, 12
    plot_w = width - pad_l - 10
    plot_h = height - pad_b - pad_t

    def xy(i, v, n):
        x = pad_l + (plot_w * (i / max(1, n - 1)))
        y = pad_t + plot_h * (1 - (v - vmin) / span)
        return x, y

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" preserveAspectRatio="none" '
             f'style="background:#fff;border:1px solid #eee;border-radius:6px">']
    # 基准线 v=1
    _, y1 = xy(0, 1.0, 2)
    if vmin <= 1.0 <= vmax:
        parts.append(f'<line x1="{pad_l}" y1="{y1:.1f}" x2="{pad_l + plot_w}" y2="{y1:.1f}" '
                     f'stroke="#bbb" stroke-dasharray="4 3" stroke-width="1"/>')
    for name, pts in series.items():
        col = colors.get(name, "#7f8c8d")
        coords = []
        n = len(pts)
        for i, (_, v) in enumerate(pts):
            x, y = xy(i, v, n)
            coords.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline fill="none" stroke="{col}" stroke-width="1.8" '
                     f'points="{" ".join(coords)}"/>')
    parts.append("</svg>")
    legend = " ".join(
        f'<span style="color:{colors.get(n, "#7f8c8d")};font-weight:600">— {_e(n)}</span>'
        for n in series.keys())
    return f'<div class="legend">{legend}</div>' + "".join(parts)


def _monthly_heatmap(ctx: dict) -> str:
    try:
        import pandas as pd
        from backtest import master_common
        daily = master_common.equal_weight_daily_returns(ctx.get("data_dict", {}), ctx.get("codes", []))
        if daily is None or len(daily) < 20:
            return '<p class="note">月度收益热力图：数据不足。</p>'
        idx = pd.to_datetime(daily.index.astype(str), errors="coerce")
        s = pd.Series(daily.values, index=idx).dropna()
        m = (1.0 + s).groupby([s.index.year, s.index.month]).prod() - 1.0
        years = sorted(set(i[0] for i in m.index))
        cells = []
        vmax = max(abs(float(v)) for v in m.values) or 0.01
        cells.append("<tr><th>年\\月</th>" + "".join(f"<th>{mm}</th>" for mm in range(1, 13)) + "</tr>")
        for y in years:
            row = [f"<td class='yh'>{y}</td>"]
            for mm in range(1, 13):
                v = m.get((y, mm))
                if v is None:
                    row.append("<td class='na'>·</td>")
                else:
                    fv = float(v)
                    intensity = min(1.0, abs(fv) / vmax)
                    # 红涨绿跌
                    bg = f"rgba(192,57,43,{0.12 + intensity * 0.6:.2f})" if fv >= 0 \
                        else f"rgba(30,132,73,{0.12 + intensity * 0.6:.2f})"
                    row.append(f"<td style='background:{bg}'>{fv * 100:.1f}</td>")
            cells.append("<tr>" + "".join(row) + "</tr>")
        return "<table class='heat'>" + "".join(cells) + "</table>"
    except Exception as e:
        logger.debug(f"月度热力图失败: {e}")
        return f'<p class="note">月度收益热力图不可用：{_e(e)}</p>'


def _sec_gate_cover(gates: dict) -> str:
    overall = _overall(gates)
    concl = {
        "green": "七道门全部通过 —— 允许进入模拟盘（衔接 paper-trading-graduation）。",
        "yellow": "存在观察项（黄）—— 可进入模拟盘但需重点观察标注的门。",
        "red": "存在不通过项（红）—— 暂不进入模拟盘，见下方各门具体问题与修复建议。",
    }[overall]
    rows = []
    for key, title, std in GATE_META:
        v = gates.get(key, "yellow")
        rows.append(
            f'<div class="gate" style="background:{_GATE_BG.get(v, "#fff")};'
            f'border-left:6px solid {_GATE_FG.get(v, "#999")}">'
            f'<div class="gate-t">{_e(title)}</div>'
            f'<div class="gate-v" style="color:{_GATE_FG.get(v, "#999")}">{_GATE_LABEL.get(v, v)}</div>'
            f'<div class="gate-s">标准：{_e(std)}</div></div>')
    badge = {"green": "#1e8449", "yellow": "#b8860b", "red": "#c0392b"}[overall]
    return (f'<section><h2>一、七道准入门总览</h2>'
            f'<div class="overall" style="border-color:{badge};color:{badge}">'
            f'总判定：{overall.upper()} — {_e(concl)}</div>'
            f'<div class="gates">{"".join(rows)}</div></section>')


def _sec_stage_status(ctx: dict, record: dict) -> str:
    names = [f"stage{i}" for i in range(0, 14)]
    chips = []
    for n in names:
        st = (ctx.get(n) or {}).get("status", "—")
        col = {"ok": "#1e8449", "skipped": "#b8860b", "fail": "#c0392b"}.get(st, "#7f8c8d")
        chips.append(f'<span class="chip" style="border-color:{col};color:{col}">'
                     f'{_e(n)}: {_e(st)}</span>')
    meta = (f'<p class="note">run_id={_e(record.get("run_id"))} · commit={_e(record.get("git_commit"))} '
            f'· quick={_e(record.get("quick"))} · {_e(record.get("time"))}</p>')
    return f'<section><h2>二、各 Stage 执行状态</h2>{meta}<div class="chips">{"".join(chips)}</div></section>'


def _sec_data_audit(ctx: dict) -> str:
    s0 = ctx.get("stage0", {})
    if s0.get("status") not in ("ok", "fail"):
        return f'<section><h2>三、数据质量审计</h2>{_skipped_block(s0, "Stage0")}</section>'
    summary = s0.get("summary", {})
    rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in summary.items())
    blocking = s0.get("blocking_errors") or []
    warnings = s0.get("warnings") or []
    bl = "".join(f"<li>{_e(x)}</li>" for x in blocking) or "<li>无阻断级错误</li>"
    wl = "".join(f"<li>{_e(x)}</li>" for x in warnings[:20]) or "<li>无警告</li>"
    extra = []
    if s0.get("survivorship_bias"):
        extra.append(f'<p class="warn">幸存者偏差：{_e(s0["survivorship_bias"])}</p>')
    if s0.get("adjustment_note"):
        extra.append(f'<p class="note">复权口径：{_e(s0["adjustment_note"])}</p>')
    if s0.get("conclusion"):
        extra.append(f'<p class="note">审计结论：{_e(s0["conclusion"])}</p>')
    return (f'<section><h2>三、数据质量审计</h2>'
            f'<table class="kv">{rows}</table>'
            f'<h3>阻断级错误</h3><ul>{bl}</ul>'
            f'<h3>警告（≤20）</h3><ul>{wl}</ul>{"".join(extra)}</section>')


def _sec_equity(ctx: dict) -> str:
    series = _build_equity_series(ctx)
    return (f'<section><h2>四、净值曲线（策略 vs 基准）与月度收益</h2>'
            f'{_svg_line(series)}<h3>月度收益热力图（%）</h3>{_monthly_heatmap(ctx)}'
            f'<p class="note">策略口径：等权买入持有日收益（详见附录）。红=上涨/正收益，绿=下跌/负收益。</p></section>')


def _sec_param(ctx: dict) -> str:
    s5 = ctx.get("stage5", {})
    if s5.get("status") != "ok":
        return f'<section><h2>五、参数网格与高原检测</h2>{_skipped_block(s5, "Stage5")}</section>'
    hm = s5.get("heatmap", {})
    hm_html = ""
    if hm and hm.get("matrix"):
        xv, yv, mat = hm.get("x_values", []), hm.get("y_values", []), hm.get("matrix", [])
        flat = [c for row in mat for c in row if c is not None]
        vmax = max(flat) if flat else 1
        vmin = min(flat) if flat else 0
        span = (vmax - vmin) or 1
        head = f"<tr><th>{_e(hm.get('y_axis'))}\\{_e(hm.get('x_axis'))}</th>" + \
            "".join(f"<th>{_e(x)}</th>" for x in xv) + "</tr>"
        body = []
        for i, y in enumerate(yv):
            cells = [f"<td class='yh'>{_e(y)}</td>"]
            for j in range(len(xv)):
                v = mat[i][j] if i < len(mat) and j < len(mat[i]) else None
                if v is None:
                    cells.append("<td class='na'>·</td>")
                else:
                    inten = (v - vmin) / span
                    cells.append(f"<td style='background:rgba(192,57,43,{0.1 + inten * 0.6:.2f})'>{v:.3f}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        hm_html = f"<table class='heat'>{head}{''.join(body)}</table>"
    verdict_map = {"plateau": "高原（稳健）", "marginal": "边缘", "isolated": "孤点（过拟合风险）",
                   "insufficient": "样本不足"}
    verdict = verdict_map.get(s5.get("verdict"), s5.get("verdict", "N/A"))
    nb = "".join(f"<li>{_e(x.get('param'))}={_e(x.get('value'))} → 夏普 {_e(x.get('sharpe'))}</li>"
                 for x in s5.get("neighbors", []))
    return (f'<section><h2>五、参数网格与高原检测</h2>'
            f'<p>最优参数：<b>{_e(s5.get("best_params"))}</b>，最优夏普 <b>{_num(s5.get("best_sharpe"))}</b>，'
            f'网格 trial 数 {_e(s5.get("n_trials"))}。</p>'
            f'<p>高原判定：<b>{_e(verdict)}</b>，邻域平均退化 {_pct(s5.get("neighbor_degradation"))}'
            f'（门限 &lt; {_pct(0.15)}，孤点 &gt; {_pct(0.30)}）。</p>'
            f'{hm_html}<h3>最优点邻域</h3><ul>{nb or "<li>无</li>"}</ul></section>')


def _sec_walkforward(ctx: dict) -> str:
    s6 = ctx.get("stage6", {})
    res = s6.get("result", {}) if s6.get("status") == "ok" else {}
    if not res:
        return f'<section><h2>六、Walk-Forward 滚动验证</h2>{_skipped_block(s6, "Stage6")}</section>'
    rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>"
                   for k, v in res.items() if not isinstance(v, (dict, list)))
    oos = res.get("oos_sharpe")
    return (f'<section><h2>六、Walk-Forward 滚动验证</h2>'
            f'<p>样本外平均夏普：<b>{_num(oos)}</b>（G2 门限 &gt; 0.8）。</p>'
            f'<table class="kv">{rows}</table></section>')


def _sec_monte(ctx: dict) -> str:
    s7 = ctx.get("stage7", {})
    if s7.get("status") not in ("ok",):
        return f'<section><h2>七、蒙特卡洛 + 历史情景 + Block Bootstrap</h2>{_skipped_block(s7, "Stage7")}</section>'
    parts = []
    mc = s7.get("monte_carlo") or {}
    if mc and "error" not in mc:
        rd = mc.get("return_distribution", {})
        dd = mc.get("max_drawdown_distribution", {})
        parts.append(
            f'<h3>蒙特卡洛（逐笔随机化）</h3><table class="kv">'
            f'<tr><td>模拟次数</td><td>{_e(mc.get("n_simulations"))}</td></tr>'
            f'<tr><td>收益分布 5%/50%/95%</td><td>{_pct(rd.get("p5"))} / {_pct(rd.get("p50"))} / {_pct(rd.get("p95"))}</td></tr>'
            f'<tr><td>最大回撤分布 50%/95%</td><td>{_pct(dd.get("p50"))} / {_pct(dd.get("p95"))}</td></tr>'
            f'<tr><td>破产概率(腰斩)</td><td>{_pct(mc.get("ruin_probability"))}</td></tr></table>')
    elif mc:
        parts.append(f'<p class="note">蒙特卡洛：{_e(mc.get("error"))}</p>')
    hist = s7.get("historical") or {}
    if hist and "error" not in hist:
        cards = []
        for name, v in hist.items():
            if not isinstance(v, dict):
                continue
            surv = v.get("survival")
            col = "#1e8449" if surv else "#c0392b"
            cards.append(
                f'<div class="card" style="border-left:5px solid {col}">'
                f'<div class="card-t">{_e(name)} <span style="color:{col}">{"存活" if surv else "未存活"}</span></div>'
                f'<div>模拟收益 {_pct(v.get("total_return"))} · 最大回撤 {_pct(v.get("max_drawdown"))}</div>'
                f'<div class="note">{_e(v.get("description"))}</div></div>')
        parts.append(f'<h3>历史情景压力（G6）</h3><div class="cards">{"".join(cards)}</div>')
    bb = s7.get("block_bootstrap") or {}
    if bb and "error" not in bb:
        rc = bb.get("return_ci", {}); dc = bb.get("max_drawdown_ci", {})
        parts.append(
            f'<h3>日度 Block Bootstrap（块长 {_e(bb.get("block_size"))}，路径 {_e(bb.get("n_paths"))}）</h3>'
            f'<table class="kv">'
            f'<tr><td>收益 90%CI (5%/50%/95%)</td><td>{_pct(rc.get("p5"))} / {_pct(rc.get("p50"))} / {_pct(rc.get("p95"))}</td></tr>'
            f'<tr><td>最大回撤 50%/95%</td><td>{_pct(dc.get("p50"))} / {_pct(dc.get("p95"))}</td></tr>'
            f'<tr><td>破产概率</td><td>{_pct(bb.get("ruin_probability"))}</td></tr></table>')
    elif bb:
        parts.append(f'<p class="note">Block Bootstrap：{_e(bb.get("error"))}</p>')
    if s7.get("reason"):
        parts.append(f'<p class="note">{_e(s7["reason"])}</p>')
    return f'<section><h2>七、蒙特卡洛 + 历史情景 + Block Bootstrap</h2>{"".join(parts) or "<p class=note>无可用结果</p>"}</section>'


def _sec_dsr(ctx: dict) -> str:
    s8 = ctx.get("stage8", {})
    res = s8.get("result", {}) if s8.get("status") == "ok" else {}
    if not res:
        return f'<section><h2>八、Deflated Sharpe / PBO</h2>{_skipped_block(s8, "Stage8")}</section>'
    rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>"
                   for k, v in res.items() if not isinstance(v, (dict, list)))
    dsr = res.get("dsr")
    return (f'<section><h2>八、Deflated Sharpe / PBO</h2>'
            f'<p>DSR：<b>{_num(dsr)}</b>（G3 门限 &gt; 0.95）。PBO：{_e(res.get("pbo", "未实现/可选"))}。</p>'
            f'<table class="kv">{rows}</table></section>')


def _sec_benchmark(ctx: dict) -> str:
    s9 = ctx.get("stage9", {})
    if s9.get("status") != "ok":
        return f'<section><h2>九、多基准对比</h2>{_skipped_block(s9, "Stage9")}</section>'
    rows = []
    for code, b in (s9.get("benchmarks") or {}).items():
        if b.get("status") != "ok":
            rows.append(f"<tr><td>{_e(code)} {_e(b.get('name'))}</td>"
                        f"<td colspan='7' class='na'>缺失：{_e(b.get('reason'))}</td></tr>")
            continue
        rows.append(
            f"<tr><td>{_e(code)} {_e(b.get('name'))}</td>"
            f"<td>{_pct(b.get('benchmark_cum_return'))}</td><td>{_pct(b.get('benchmark_annual'))}</td>"
            f"<td>{_pct(b.get('excess_return'))}</td><td>{_num(b.get('beta'), 3)}</td>"
            f"<td>{_num(b.get('alpha'), 4)}</td><td>{_num(b.get('info_ratio'), 3)}</td>"
            f"<td>{_pct(b.get('tracking_error'))}</td></tr>")
    strat = s9.get("strategy", {})
    return (f'<section><h2>九、多基准对比</h2>'
            f'<p class="note">策略累计收益 {_pct(strat.get("total_return"))}，年化 {_pct(strat.get("annual_return"))}，'
            f'夏普 {_num(strat.get("sharpe"), 3)}。可用基准 {s9.get("n_available")} 个，缺失 {_e(s9.get("missing"))}。</p>'
            f'<table class="grid"><tr><th>基准</th><th>累计</th><th>年化</th><th>超额</th>'
            f'<th>Beta</th><th>Alpha</th><th>信息比率</th><th>跟踪误差</th></tr>{"".join(rows)}</table>'
            f'<p class="note">{_e(s9.get("note"))}</p></section>')


def _sec_cost(ctx: dict) -> str:
    s10 = ctx.get("stage10", {})
    if s10.get("status") != "ok":
        return f'<section><h2>十、成本敏感性</h2>{_skipped_block(s10, "Stage10")}</section>'
    tiers = s10.get("tiers", {})
    order = ["zero", "current", "cost_x2", "slippage_x3"]
    label = {"zero": "零成本", "current": "当前成本", "cost_x2": "成本×2", "slippage_x3": "滑点×3"}
    rows = []
    for t in order:
        d = tiers.get(t, {})
        if "error" in d:
            rows.append(f"<tr><td>{label.get(t, t)}</td><td colspan='4' class='na'>{_e(d['error'])}</td></tr>")
            continue
        rows.append(f"<tr><td>{label.get(t, t)}</td><td>{_num(d.get('sharpe'), 3)}</td>"
                    f"<td>{_pct(d.get('annual'))}</td><td>{_e(d.get('n_trades'))}</td>"
                    f"<td>{_pct(d.get('win_rate'), 1)}</td></tr>")
    vmap = {"robust": "稳健", "fragile": "脆弱", "degenerate": "严重退化", "insufficient": "样本不足"}
    return (f'<section><h2>十、成本敏感性</h2>'
            f'<p>成本×2 后夏普退化 <b>{_pct(s10.get("sharpe_degradation_x2"))}</b>'
            f'（G4 门限 &lt; {_pct(s10.get("threshold", 0.30))}）→ 判定 <b>{_e(vmap.get(s10.get("verdict"), s10.get("verdict")))}</b>。</p>'
            f'<table class="grid"><tr><th>成本档</th><th>夏普</th><th>年化</th><th>交易数</th><th>胜率</th></tr>'
            f'{"".join(rows)}</table></section>')


def _sec_regime(ctx: dict) -> str:
    s11 = ctx.get("stage11", {})
    if s11.get("status") != "ok":
        return f'<section><h2>十一、分段绩效</h2>{_skipped_block(s11, "Stage11")}</section>'
    parts = []
    by_year = s11.get("by_year", {})
    if by_year:
        rows = "".join(
            f"<tr><td>{_e(y)}</td><td>{_pct(d.get('total_return'))}</td><td>{_pct(d.get('annual_return'))}</td>"
            f"<td>{_num(d.get('sharpe'), 3)}</td><td>{_pct(d.get('max_drawdown'))}</td></tr>"
            for y, d in sorted(by_year.items()) if isinstance(d, dict) and d.get("status") == "ok")
        parts.append(f'<h3>逐年</h3><table class="grid"><tr><th>年份</th><th>收益</th><th>年化</th>'
                     f'<th>夏普</th><th>最大回撤</th></tr>{rows}</table>')
    regime = s11.get("by_regime", {})
    if regime.get("status") == "ok":
        seg = regime.get("segments", {})
        rows = "".join(
            f"<tr><td>{_e(st)}</td><td>{_pct(d.get('total_return'))}</td><td>{_num(d.get('sharpe'), 3)}</td>"
            f"<td>{_pct(d.get('max_drawdown'))}</td><td>{_e(d.get('n_days'))}</td></tr>"
            for st, d in seg.items() if isinstance(d, dict) and d.get("status") == "ok")
        parts.append(f'<h3>市场环境分段（BULL/RANGE/BEAR）</h3>'
                     f'<table class="grid"><tr><th>环境</th><th>收益</th><th>夏普</th><th>最大回撤</th><th>天数</th></tr>{rows}</table>')
        bear = seg.get("BEAR", {})
        if isinstance(bear, dict) and bear.get("status") == "ok":
            parts.append(f'<p class="note">BEAR 段收益 {_pct(bear.get("total_return"))}、最大回撤 '
                         f'{_pct(bear.get("max_drawdown"))}（G6 要求 BEAR 段无大幅亏损）。</p>')
    else:
        parts.append(f'<p class="note">市场环境分段：{_e(regime.get("reason", "不可用"))}</p>')
    hw = s11.get("historical_windows", {})
    if hw.get("status") == "ok":
        rows = "".join(
            f"<tr><td>{_e(n)}</td><td>{_pct(d.get('total_return'))}</td><td>{_pct(d.get('max_drawdown'))}</td>"
            f"<td>{_pct(d.get('scenario_index_drop'))}</td></tr>"
            for n, d in (hw.get("windows") or {}).items()
            if isinstance(d, dict) and d.get("status") == "ok")
        parts.append(f'<h3>历史窗口实盘回放</h3><table class="grid"><tr><th>情景</th><th>策略实际收益</th>'
                     f'<th>最大回撤</th><th>指数跌幅</th></tr>{rows or "<tr><td colspan=4 class=na>无覆盖区间</td></tr>"}</table>'
                     f'<p class="note">{_e(hw.get("note"))}</p>')
    return f'<section><h2>十一、分段绩效</h2>{"".join(parts)}</section>'


def _sec_factor(ctx: dict) -> str:
    s12 = ctx.get("stage12", {})
    if s12.get("status") != "ok":
        return f'<section><h2>十二、因子分层回测</h2>{_skipped_block(s12, "Stage12")}</section>'
    rows = []
    for name, f in (s12.get("factors") or {}).items():
        rows.append(
            f"<tr><td>{_e(name)}</td><td>{_pct(f.get('long_short_return'))}</td>"
            f"<td>{_num(f.get('long_short_sharpe'), 3)}</td><td>{_num(f.get('ic_mean'), 4)}</td>"
            f"<td>{_num(f.get('icir'), 3)}</td><td>{_num(f.get('monotonicity_score'), 3)}</td></tr>")
    skipped = s12.get("skipped") or []
    sk = "".join(f"<li>{_e(x.get('factor'))}：{_e(x.get('reason'))}</li>" for x in skipped)
    return (f'<section><h2>十二、因子分层回测</h2>'
            f'<p class="note">{_e(s12.get("n_stocks"))} 只标的 · {s12.get("n_groups")} 组 · '
            f'前瞻 {s12.get("forward_days")} 日 · 调仓 {s12.get("rebalance_freq")} 日</p>'
            f'<table class="grid"><tr><th>因子</th><th>多空收益</th><th>多空夏普</th><th>IC均值</th>'
            f'<th>ICIR</th><th>单调性</th></tr>{"".join(rows) or "<tr><td colspan=6 class=na>无有效因子</td></tr>"}</table>'
            f'{f"<h3>跳过的因子</h3><ul>{sk}</ul>" if sk else ""}'
            f'<p class="note">{_e(s12.get("note"))}</p></section>')


def _sec_cross(ctx: dict) -> str:
    s4 = ctx.get("stage4", {})
    if s4.get("status") != "ok":
        return f'<section><h2>十三、双引擎交叉验证</h2>{_skipped_block(s4, "Stage4")}</section>'
    vec, evt = s4.get("vectorized", {}), s4.get("event_driven", {})
    return (f'<section><h2>十三、双引擎交叉验证（G7）</h2>'
            f'<table class="grid"><tr><th>口径</th><th>总收益</th><th>夏普</th><th>样本</th></tr>'
            f'<tr><td>向量化(等权)</td><td>{_pct(vec.get("total_return"))}</td><td>{_num(vec.get("sharpe"), 3)}</td>'
            f'<td>{_e(vec.get("n_days", vec.get("n_trades", "")))}</td></tr>'
            f'<tr><td>事件驱动</td><td>{_pct(evt.get("total_return"))}</td><td>{_num(evt.get("sharpe"), 3)}</td>'
            f'<td>{_e(evt.get("n_trades", ""))}</td></tr></table>'
            f'<p>总收益差 {_pct(s4.get("return_diff_abs"))}（门限 &lt; 3%），夏普差 {_num(s4.get("sharpe_diff"), 3)}'
            f'（门限 &lt; 0.3）→ {_e(s4.get("verdict"))}</p></section>')


def _sec_appendix(ctx: dict, record: dict) -> str:
    s0 = ctx.get("stage0", {})
    surv = s0.get("survivorship_bias", "股票池未含退市/摘牌标的，结果存在幸存者偏差，收益可能被高估。")
    n_codes = len(ctx.get("codes", []))
    audit_sum = s0.get("summary", {}) if isinstance(s0.get("summary"), dict) else {}
    date_range = ""
    if audit_sum.get("date_min") and audit_sum.get("date_max"):
        date_range = f'{_e(audit_sum.get("date_min"))} ~ {_e(audit_sum.get("date_max"))}'
    s3 = ctx.get("stage3") if isinstance(ctx.get("stage3"), dict) else {}
    cov = s3.get("coverage") if isinstance(s3.get("coverage"), dict) else {}
    if cov and cov.get("n_sampled") is not None:
        s3_note = (f'采样 {cov.get("n_sampled")}/{cov.get("n_total")} 只（优先 STOCK_POOL）、'
                   f'窗口 {cov.get("window_days")} 交易日（{_e(cov.get("start_date"))}~{_e(cov.get("end_date"))}）、'
                   f'关闭蒙特卡洛、不经 run() 故无邮件副作用')
    elif s3.get("status") == "skipped":
        s3_note = _e(s3.get("reason", "本次未执行"))
    else:
        s3_note = "见执行状态"
    return f'''<section><h2>十四、附录：口径、假设与已知限制</h2>
<ul class="appx">
<li><b>成交口径</b>：信号当日决策、次日开盘价成交；禁止未来函数；财报数据披露日后方可用。</li>
<li><b>成本口径</b>：佣金万2.5（最低5元）、印花税千1（卖出）、过户费、滑点、冲击成本，全部计入；Stage10 另给零成本对照。</li>
<li><b>组合口径</b>：Stage4/7-bootstrap/9/11 采用"等权买入持有日收益"作为策略序列（透明、可复现、可与指数对齐）；Stage1/5/6 为逐笔 v5/v6 口径。两者不可直接混比。</li>
<li><b>Stage3 口径</b>：进程内有界复用 run_full_backtest 事件驱动引擎（同一 BacktestEngineV2+CostConfig+趋势跟踪V2.0）；为控 30min 预算并规避 run() 邮件副作用，限定采样与窗口——{s3_note}；代表采样池表现，非全池逐笔。</li>
<li><b>G7 双引擎对齐</b>：Stage4 两侧使用"共同窗口内全程有数据"的同一标的集（≤6 只）与同一区间，消除阶梯上市/首建仓日/整数手现金拖累造成的长历史口径差；残差应回落到成本+成交时点量级（&lt;3%）。</li>
<li><b>指标复用</b>：夏普/回撤/年化/alpha-beta 全部走 backtest.metrics，未新建第二套指标。</li>
<li><b>幸存者偏差</b>：{_e(surv)}</li>
<li><b>数据区间</b>：{_e(date_range) or "见数据审计"}；参与标的 {n_codes} 只。</li>
<li><b>随机种子</b>：蒙特卡洛与 block bootstrap 固定 seed=42，结果可复现。</li>
<li><b>已知限制</b>：部分宽基指数库中缺失时已标 missing 并静默降级；退市股未纳入。</li>
<li><b>回测卫生</b>：已在本回测进程内中和实盘"策略失效检测器(SFD)"熔断状态（monkeypatch get_status→normal，仅当前进程，不触碰持久化文件、不影响实盘/调度），避免实盘 breaker 泄漏导致 Stage2/3 引擎回测零交易。</li>
<li><b>生成信息</b>：run_id={_e(record.get("run_id"))} · git commit={_e(record.get("git_commit"))} · 生成时间 {_e(datetime.datetime.now().isoformat(timespec="seconds"))}。</li>
</ul></section>'''


_CSS = """
*{box-sizing:border-box}body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
margin:0;background:#f5f6f8;color:#2c3e50;line-height:1.6}
.wrap{max-width:960px;margin:0 auto;padding:24px}
header{background:linear-gradient(135deg,#1a2a3a,#2c3e50);color:#fff;padding:32px;border-radius:10px;margin-bottom:20px}
header h1{margin:0 0 6px;font-size:26px}header .sub{opacity:.85;font-size:14px}
section{background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
h2{font-size:19px;border-bottom:2px solid #ecf0f1;padding-bottom:8px;margin-top:0}
h3{font-size:15px;color:#34495e;margin:16px 0 8px}
.overall{border:2px solid;border-radius:8px;padding:14px 16px;font-size:16px;font-weight:700;margin-bottom:16px}
.gates{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.gate{border-radius:8px;padding:12px 14px}.gate-t{font-weight:700;font-size:14px}
.gate-v{font-size:20px;font-weight:800;margin:4px 0}.gate-s{font-size:12px;opacity:.75}
.chips{display:flex;flex-wrap:wrap;gap:8px}.chip{border:1.5px solid;border-radius:14px;padding:3px 10px;font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.card{background:#fafbfc;border-radius:8px;padding:12px}.card-t{font-weight:700;margin-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
table.kv td,table.grid td,table.grid th{border:1px solid #e6e9ec;padding:6px 8px;text-align:left}
table.grid th{background:#f4f6f8}table.heat td,table.heat th{border:1px solid #fff;padding:4px 6px;text-align:center;font-size:12px}
table.heat .yh{background:#f4f6f8;font-weight:600}.na{color:#bbb;text-align:center}
.note{color:#7f8c8d;font-size:12.5px}.warn{color:#c0392b;font-size:13px;font-weight:600}
.legend{margin:6px 0;font-size:13px}.legend span{margin-right:16px}
ul.appx li{margin-bottom:6px;font-size:13px}footer{text-align:center;color:#95a5a6;font-size:12px;padding:20px}
"""


def generate_master_report(ctx: dict, record: dict) -> str:
    """生成单文件 HTML 总报告，写入 OUTPUT_DIR，返回文件路径。"""
    gates = record.get("gates") or ctx.get("stage13", {}).get("gates") or {}
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    run_id = record.get("run_id", datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    path = os.path.join(OUTPUT_DIR, f"master_backtest_report_{run_id}.html")

    sections = [
        _sec_gate_cover(gates),
        _sec_stage_status(ctx, record),
        _sec_data_audit(ctx),
        _sec_equity(ctx),
        _sec_param(ctx),
        _sec_walkforward(ctx),
        _sec_monte(ctx),
        _sec_dsr(ctx),
        _sec_benchmark(ctx),
        _sec_cost(ctx),
        _sec_regime(ctx),
        _sec_factor(ctx),
        _sec_cross(ctx),
        _sec_appendix(ctx, record),
    ]
    overall = _overall(gates)
    concl = {"green": "全部通过，允许进入模拟盘", "yellow": "存在观察项，谨慎进入模拟盘",
             "red": "存在不通过项，暂不进入模拟盘"}[overall]
    doc = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>最全历史回测总报告 · {_e(run_id)}</title><style>{_CSS}</style></head><body><div class="wrap">
<header><h1>最全历史回测流水线 · 总报告</h1>
<div class="sub">run_id {_e(run_id)} · commit {_e(record.get("git_commit"))} · quick={_e(record.get("quick"))} · {_e(record.get("time"))}</div>
<div class="sub" style="margin-top:8px;font-size:16px;font-weight:700">总判定：{overall.upper()} — {_e(concl)}</div></header>
{"".join(sections)}
<footer>本报告由 scripts/master_backtest.py 自动生成 · 数据缺失项均如实标注，未做任何"全部正常"假设</footer>
</div></body></html>'''
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    logger.info(f"[Stage13] 总报告已生成: {path}")
    return path
