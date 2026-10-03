# -*- coding: utf-8 -*-
"""
Brinson 归因
==============
把组合相对基准的超额收益分解为三项（Brinson-Fachler 模型）：
- 配置效应 AR (Allocation): Σ (Wp_i - Wb_i) × (Rb_i - Rb_total)
- 选股效应 SR (Selection):  Σ Wb_i × (Rp_i - Rb_i)
- 交互效应 IR (Interaction): Σ (Wp_i - Wb_i) × (Rp_i - Rb_i)

数学性质: AR + SR + IR ≡ Rp_total - Rb_total（实际超额收益），
本模块内置校验，误差超过阈值(默认0.1pct)时在结果中标记 check_passed=False。

分组口径: 行业/赛道（复用 config 股票池 sector 字段/申万二级口径），
基准默认沪深300（权重与行业收益由数据层统一提供）。
数据不足（无行业映射/权重全为0）时静默降级返回 ok=False，禁止编造数据。
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 三项合计与实际超额收益的允许误差（0.1pct = 0.001）
CHECK_TOLERANCE = 0.001
# 权重合计偏离1的允许误差（组合含现金时权重和<1，容忍度放宽）
WEIGHT_TOLERANCE = 0.05


def brinson_attribution(portfolio_weights: dict,
                        benchmark_weights: dict,
                        portfolio_returns: dict,
                        benchmark_returns: dict,
                        tolerance: float = CHECK_TOLERANCE,
                        cash_return: float = 0.0) -> dict:
    """
    Brinson 三效应归因（Brinson-Fachler）

    参数:
        portfolio_weights:   {sector: 组合权重}，权重为占比(0~1)，含现金时合计可<1
        benchmark_weights:   {sector: 基准权重}（如沪深300行业权重），合计≈1
        portfolio_returns:   {sector: 组合该行业区间收益}（如周收益，小数口径）
        benchmark_returns:   {sector: 基准该行业区间收益}
        tolerance:           三项合计 vs 实际超额收益的允许误差（默认0.1pct）

    返回:
        {
            "ok": bool,                     # 数据是否充分
            "sectors": [ {sector, wp, wb, rp, rb, ar, sr, ir, total} ],
            "allocation": float,            # AR 合计
            "selection": float,             # SR 合计
            "interaction": float,           # IR 合计
            "total_excess": float,          # AR+SR+IR
            "actual_excess": float,         # Rp_total - Rb_total
            "check_error": float,           # |合计 - 实际|
            "check_passed": bool,           # 误差 < tolerance
            "wp_sum": float, "wb_sum": float,
            "message": str,                 # 降级原因（ok=False时）
        }
    """
    result = {
        "ok": False, "sectors": [], "allocation": 0.0, "selection": 0.0,
        "interaction": 0.0, "total_excess": 0.0, "actual_excess": 0.0,
        "check_error": 0.0, "check_passed": False,
        "wp_sum": 0.0, "wb_sum": 0.0, "message": "",
    }

    if not portfolio_weights or not benchmark_weights:
        result["message"] = "组合或基准行业权重缺失，Brinson归因降级"
        return result
    if not portfolio_returns or not benchmark_returns:
        result["message"] = "组合或基准行业收益缺失，Brinson归因降级"
        return result

    sectors = sorted(set(portfolio_weights) | set(benchmark_weights))
    wp_sum = sum(float(portfolio_weights.get(s, 0.0) or 0.0) for s in sectors)
    wb_sum = sum(float(benchmark_weights.get(s, 0.0) or 0.0) for s in sectors)
    if wp_sum <= 0 or wb_sum <= 0:
        result["message"] = "权重合计为0，Brinson归因降级"
        return result

    # 现金作为显式资产处理：组合未满仓(Σwp<1)时，剩余权重视为现金，
    # 基准权重0、收益=无风险利率。否则三项合计与实际超额收益会差一个
    # 现金配置项（cash drag），恒等式不成立。
    cash_w = 1.0 - wp_sum
    has_cash = cash_w > 1e-9
    if has_cash:
        sectors = sectors + ["现金"]
        portfolio_weights = dict(portfolio_weights)
        portfolio_returns = dict(portfolio_returns)
        benchmark_weights = dict(benchmark_weights)
        benchmark_returns = dict(benchmark_returns)
        portfolio_weights["现金"] = cash_w
        portfolio_returns["现金"] = cash_return
        benchmark_weights["现金"] = 0.0
        benchmark_returns["现金"] = cash_return

    # 组合/基准总收益（按权重加权；缺失收益的行业按0计）
    rp_total = sum(float(portfolio_weights.get(s, 0.0) or 0.0)
                   * float(portfolio_returns.get(s, 0.0) or 0.0) for s in sectors)
    rb_total = sum(float(benchmark_weights.get(s, 0.0) or 0.0)
                   * float(benchmark_returns.get(s, 0.0) or 0.0) for s in sectors)

    rows = []
    ar_sum = sr_sum = ir_sum = 0.0
    for s in sectors:
        wp = float(portfolio_weights.get(s, 0.0) or 0.0)
        wb = float(benchmark_weights.get(s, 0.0) or 0.0)
        rp = float(portfolio_returns.get(s, 0.0) or 0.0)
        rb = float(benchmark_returns.get(s, 0.0) or 0.0)
        # Brinson-Fachler: 配置效应扣除基准总收益，保证三项合计恒等于超额收益
        ar = (wp - wb) * (rb - rb_total)
        sr = wb * (rp - rb)
        ir = (wp - wb) * (rp - rb)
        ar_sum += ar
        sr_sum += sr
        ir_sum += ir
        rows.append({
            "sector": s,
            "wp": round(wp, 6), "wb": round(wb, 6),
            "rp": round(rp, 6), "rb": round(rb, 6),
            "ar": round(ar, 6), "sr": round(sr, 6), "ir": round(ir, 6),
            "total": round(ar + sr + ir, 6),
        })

    actual_excess = rp_total - rb_total
    total_excess = ar_sum + sr_sum + ir_sum
    check_error = abs(total_excess - actual_excess)

    # 按贡献绝对值排序，便于报告展示主要来源
    rows.sort(key=lambda r: abs(r["ar"]) + abs(r["sr"]) + abs(r["ir"]), reverse=True)

    result.update({
        "ok": True,
        "sectors": rows,
        "allocation": round(ar_sum, 6),
        "selection": round(sr_sum, 6),
        "interaction": round(ir_sum, 6),
        "total_excess": round(total_excess, 6),
        "actual_excess": round(actual_excess, 6),
        "check_error": round(check_error, 8),
        "check_passed": check_error < tolerance,
        "wp_sum": round(wp_sum, 6),
        "wb_sum": round(wb_sum, 6),
        "rp_total": round(rp_total, 6),
        "rb_total": round(rb_total, 6),
    })
    if not result["check_passed"]:
        result["message"] = (f"Brinson三项合计({total_excess:.4%})与实际超额"
                             f"({actual_excess:.4%})误差{check_error:.4%}超阈值")
        logger.warning(result["message"])
    return result


def build_sector_maps(holdings: dict,
                      weekly_changes: dict,
                      benchmark_sector_weights: Optional[dict] = None,
                      benchmark_sector_returns: Optional[dict] = None) -> tuple:
    """
    从持仓与周涨跌构建 Brinson 输入（周报接线便捷入口）

    参数:
        holdings:       {code: {"shares","current_price","赛道"/"sector",...}}
        weekly_changes: {code: 周涨跌幅(%口径，与周报一致)}
        benchmark_sector_weights: {sector: 基准权重}（缺省 None → 降级）
        benchmark_sector_returns: {sector: 基准行业周收益(小数)}（缺省 None → 降级）

    返回:
        (portfolio_weights, portfolio_returns, benchmark_weights, benchmark_returns)
        均为小数口径；基准数据缺失时返回 ({}, {}, {}, {})，调用方静默降级。
    """
    if not holdings or not weekly_changes:
        return {}, {}, {}, {}
    if not benchmark_sector_weights or not benchmark_sector_returns:
        # 无基准行业数据（数据层未提供）→ 降级，禁止用等权编造基准
        return {}, {}, {}, {}

    # 组合市值权重（按赛道聚合）
    mv_by_sector = {}
    ret_by_sector = {}
    total_mv = 0.0
    for code, h in holdings.items():
        try:
            shares = float(h.get("shares", 0) or 0)
            price = float(h.get("current_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        mv = shares * price
        if mv <= 0:
            continue
        sector = h.get("赛道") or h.get("sector") or "其他"
        chg_pct = weekly_changes.get(code)
        if chg_pct is None:
            continue
        mv_by_sector[sector] = mv_by_sector.get(sector, 0.0) + mv
        # 收益按市值加权聚合到行业（小数口径）
        ret_by_sector[sector] = ret_by_sector.get(sector, 0.0) + mv * (float(chg_pct) / 100.0)
        total_mv += mv

    if total_mv <= 0:
        return {}, {}, {}, {}

    portfolio_weights = {s: mv / total_mv for s, mv in mv_by_sector.items()}
    portfolio_returns = {s: ret_by_sector[s] / mv_by_sector[s] for s in mv_by_sector}
    return portfolio_weights, portfolio_returns, dict(benchmark_sector_weights), dict(benchmark_sector_returns)


def render_brinson_section_html(attribution: dict) -> str:
    """
    周报 Brinson 归因区块（HTML）

    数据不足(ok=False)时返回空字符串静默隐藏；校验未通过时黄框提示。
    """
    if not attribution or not attribution.get("ok"):
        return ""

    pct = lambda x: f"{x * 100:+.2f}%"
    html = ['<div class="brinson-section" style="margin:12px 0">']
    html.append('<h3 style="margin:8px 0">Brinson 归因（vs 基准）</h3>')

    if not attribution.get("check_passed", False):
        html.append(f'<div style="background:#fff3cd;border:1px solid #ffc107;'
                    f'border-radius:6px;padding:8px;margin:6px 0;font-size:13px">'
                    f'⚠️ {attribution.get("message", "三项合计与实际超额收益误差超阈值")}</div>')

    html.append('<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:8px 0">')
    for label, key, color in [
        ("配置效应 AR", "allocation", "#2980b9"),
        ("选股效应 SR", "selection", "#27ae60"),
        ("交互效应 IR", "interaction", "#8e44ad"),
        ("实际超额", "actual_excess", "#c0392b"),
    ]:
        val = attribution.get(key, 0.0)
        html.append(f'<div style="background:#f8f9fa;border-radius:8px;padding:10px;'
                    f'text-align:center"><div style="font-size:16px;font-weight:bold;'
                    f'color:{color}">{pct(val)}</div>'
                    f'<div style="font-size:11px;color:#7f8c8d;margin-top:3px">{label}</div></div>')
    html.append('</div>')

    rows = attribution.get("sectors", [])
    if rows:
        html.append('<table style="width:100%;border-collapse:collapse;font-size:13px">')
        html.append('<tr style="background:#f1f2f6"><th style="padding:6px">行业</th>'
                    '<th>组合权重</th><th>基准权重</th><th>组合收益</th><th>基准收益</th>'
                    '<th>配置</th><th>选股</th><th>交互</th></tr>')
        for r in rows[:10]:
            html.append(
                f'<tr><td style="padding:5px"><b>{r["sector"]}</b></td>'
                f'<td>{r["wp"] * 100:.1f}%</td><td>{r["wb"] * 100:.1f}%</td>'
                f'<td>{pct(r["rp"])}</td><td>{pct(r["rb"])}</td>'
                f'<td>{pct(r["ar"])}</td><td>{pct(r["sr"])}</td><td>{pct(r["ir"])}</td></tr>')
        html.append('</table>')

    html.append(f'<div style="font-size:11px;color:#95a5a6;margin-top:4px">'
                f'三项合计 {pct(attribution.get("total_excess", 0))} = 实际超额 '
                f'{pct(attribution.get("actual_excess", 0))}'
                f'（误差 {attribution.get("check_error", 0) * 100:.4f}pct）</div>')
    html.append('</div>')
    return "".join(html)
