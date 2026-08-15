"""
组合风险行动清单HTML生成模块（批1-B公共模块）
==============================================
纯函数：消费 portfolio_risk.py 的 full_risk_report() / calc_rebalance_plan() /
calc_dynamic_position() 返回结构，生成可直接插入邮件报告的HTML段落。

输入结构参考（只读字段，不import portfolio_risk，保持解耦）:
- full_risk_report: {risk_score, overall_level, correlation, concentration,
                     var, max_drawdown, rebalance, alerts, scan_time}
- calc_rebalance_plan: {actions:[{action,code,name,shares,amount,reason,priority}],
                        urgency, rebalance_cost, total_trade_amount, detail}
- calc_dynamic_position: {shares, amount, position_ratio, atr, atr_pct,
                          risk_amount, method}

安全约束: 输入为None/空/缺字段时优雅降级（显示"数据不足"），绝不抛异常。
"""

import logging

logger = logging.getLogger(__name__)

# 风险等级 → 配色（内联样式，简单色块）
_LEVEL_COLORS = {
    "critical": "#cf222e",
    "high": "#bc4c00",
    "warning": "#bf8700",
    "medium": "#bf8700",
    "low": "#1a7f37",
    "unknown": "#57606a",
}

_LEVEL_CN = {
    "critical": "严重",
    "high": "高",
    "warning": "警告",
    "medium": "中",
    "low": "低",
    "unknown": "未知",
}


def _badge(level: str) -> str:
    color = _LEVEL_COLORS.get(level, "#57606a")
    text = _LEVEL_CN.get(level, level or "未知")
    return (f"<span style='color:#fff;background:{color};padding:2px 10px;"
            f"border-radius:3px;font-size:12px;font-weight:bold;'>{text}</span>")


def _cell(text: str, bold: bool = False, color: str = None) -> str:
    style = "padding:6px 10px;border-bottom:1px solid #e5e7eb;"
    if bold:
        style += "font-weight:bold;"
    if color:
        style += f"color:{color};"
    return f"<td style='{style}'>{text}</td>"


def _insufficient(title: str = "组合风险行动清单") -> str:
    return (
        "<div style='border:1px solid #d0d7de;border-radius:6px;padding:12px 16px;"
        "margin:12px 0;background:#f6f8fa;'>"
        f"<div style='font-weight:bold;font-size:15px;margin-bottom:6px;'>{title}</div>"
        "<div style='color:#57606a;font-size:13px;'>数据不足</div></div>"
    )


def build_risk_action_section(full_report: dict, rebalance: dict = None,
                              dynamic_pos: dict = None) -> str:
    """
    生成组合风险行动清单HTML段落

    参数:
        full_report: portfolio_risk.full_risk_report() 返回结构（可为None）
        rebalance:   calc_rebalance_plan() 返回结构（None时尝试用full_report["rebalance"]）
        dynamic_pos: calc_dynamic_position() 返回结构（可选，单股ATR仓位建议）

    返回: HTML字符串。输入异常/为空时返回"数据不足"降级段落，绝不抛异常。
    """
    try:
        if not isinstance(full_report, dict) or not full_report:
            return _insufficient()

        parts = []

        # ============================================================
        # 一、风险评分总览行
        # ============================================================
        risk_score = full_report.get("risk_score")
        overall_level = full_report.get("overall_level", "unknown")
        scan_time = full_report.get("scan_time", "")

        corr = full_report.get("correlation") or {}
        conc = full_report.get("concentration") or {}
        var = full_report.get("var") or {}
        dd = full_report.get("max_drawdown") or {}

        def _fmt(v, pattern="{:.2f}", na="N/A"):
            try:
                if v is None:
                    return na
                return pattern.format(float(v))
            except (TypeError, ValueError):
                return na

        score_color = ("#cf222e" if (isinstance(risk_score, (int, float)) and risk_score >= 70)
                       else "#bc4c00" if (isinstance(risk_score, (int, float)) and risk_score >= 50)
                       else "#bf8700" if (isinstance(risk_score, (int, float)) and risk_score >= 30)
                       else "#1a7f37")
        score_text = _fmt(risk_score, "{:.1f}") if risk_score is not None else "N/A"

        overview_cells = [
            f"风险评分 <b style='color:{score_color};font-size:16px;'>{score_text}/100</b>",
            f"综合等级 {_badge(overall_level)}",
            f"平均相关性 <b>{_fmt(corr.get('avg_correlation'), '{:.3f}')}</b>",
            f"个股HHI <b>{_fmt(conc.get('stock_hhi'), '{:.3f}')}</b>",
            f"VaR占比 <b>{_fmt(var.get('var_pct'), '{:.2%}')}</b>",
            f"最大回撤 <b>{_fmt(dd.get('max_drawdown'), '{:.2%}')}</b>",
        ]
        parts.append(
            "<div style='border:1px solid #d0d7de;border-radius:6px;padding:12px 16px;"
            "margin:12px 0;background:#f6f8fa;'>"
            "<div style='font-weight:bold;font-size:15px;margin-bottom:8px;'>"
            "📊 组合风险行动清单"
            f"<span style='color:#57606a;font-size:12px;font-weight:normal;'>"
            f"{'　|　' + scan_time if scan_time else ''}</span></div>"
            "<div style='font-size:13px;color:#24292f;line-height:1.8;'>"
            + "　｜　".join(overview_cells) +
            "</div></div>"
        )

        # ============================================================
        # 二、集中度超限清单
        # ============================================================
        conc_alerts = conc.get("alerts") or []
        if conc_alerts:
            rows = []
            for a in conc_alerts:
                if not isinstance(a, dict):
                    continue
                rows.append(
                    "<tr>"
                    + _cell(_badge(a.get("level", "warning")))
                    + _cell(a.get("type", "集中度超限"), bold=True)
                    + _cell(a.get("detail", ""))
                    + _cell(a.get("suggestion", ""))
                    + "</tr>"
                )
            if rows:
                parts.append(
                    "<div style='margin:10px 0;'>"
                    "<div style='font-weight:bold;font-size:14px;margin-bottom:6px;'>集中度超限清单</div>"
                    "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
                    "<tr style='background:#f6f8fa;'>"
                    "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>等级</th>"
                    "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>类型</th>"
                    "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>详情</th>"
                    "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>建议</th>"
                    "</tr>" + "".join(rows) + "</table></div>"
                )

        # ============================================================
        # 三、回撤/VaR超限 → 全局仓位缩放建议
        # ============================================================
        scale_suggestions = []
        var_level = var.get("risk_level")
        if var_level in ("critical", "high"):
            ratio = var.get("var_pct")
            # critical: 缩放至60%；high: 缩放至75%
            target = 0.60 if var_level == "critical" else 0.75
            scale_suggestions.append(
                f"VaR风险{_LEVEL_CN.get(var_level, var_level)}"
                f"（VaR占比{_fmt(ratio, '{:.2%}')}），建议全局仓位缩放至"
                f"<b style='color:#cf222e;'>{target:.0%}</b>以下"
            )
        dd_level = dd.get("risk_level")
        if dd_level in ("critical", "high"):
            mdd = dd.get("max_drawdown")
            target = 0.50 if dd_level == "critical" else 0.70
            scale_suggestions.append(
                f"回撤{_LEVEL_CN.get(dd_level, dd_level)}"
                f"（最大回撤{_fmt(mdd, '{:.2%}')}，当前回撤"
                f"{_fmt(dd.get('current_drawdown'), '{:.2%}')}），"
                f"建议全局仓位缩放至<b style='color:#cf222e;'>{target:.0%}</b>以下"
            )

        if scale_suggestions:
            parts.append(
                "<div style='border-left:4px solid #cf222e;background:#fff8f8;"
                "padding:10px 14px;margin:10px 0;'>"
                "<div style='font-weight:bold;color:#cf222e;font-size:14px;"
                "margin-bottom:6px;'>⚠ 全局仓位缩放建议</div>"
                "<ul style='margin:4px 0;padding-left:20px;font-size:13px;'>"
                + "".join(f"<li style='margin:3px 0;'>{s}</li>" for s in scale_suggestions)
                + "</ul></div>"
            )

        # ============================================================
        # 四、再平衡 actions 表格
        # ============================================================
        if not isinstance(rebalance, dict) or not rebalance:
            rebalance = full_report.get("rebalance")
        actions = []
        if isinstance(rebalance, dict):
            actions = rebalance.get("actions") or []

        parts.append(
            "<div style='margin:10px 0;'>"
            "<div style='font-weight:bold;font-size:14px;margin-bottom:6px;'>再平衡建议</div>"
        )
        if actions:
            urgency = rebalance.get("urgency", "")
            urgency_cn = {"high": "紧急", "medium": "中等", "low": "常规"}.get(urgency, urgency or "常规")
            rows = []
            action_colors = {"减仓": "#bf8700", "加仓": "#0969da", "清仓": "#cf222e"}
            for a in actions:
                if not isinstance(a, dict):
                    continue
                act = a.get("action", "")
                color = action_colors.get(act, "#57606a")
                try:
                    amount_txt = f"{float(a.get('amount', 0)):,.0f}"
                except (TypeError, ValueError):
                    amount_txt = str(a.get("amount", ""))
                rows.append(
                    "<tr>"
                    + _cell(f"<span style='color:#fff;background:{color};padding:2px 8px;"
                            f"border-radius:3px;font-size:12px;'>{act}</span>")
                    + _cell(a.get("code", ""))
                    + _cell(a.get("name", ""))
                    + _cell(a.get("shares", ""))
                    + _cell(amount_txt + "元")
                    + _cell(a.get("reason", ""))
                    + "</tr>"
                )
            try:
                cost_txt = f"{float(rebalance.get('rebalance_cost', 0)):,.0f}元"
            except (TypeError, ValueError):
                cost_txt = "N/A"
            parts.append(
                "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
                "<tr style='background:#f6f8fa;'>"
                "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>动作</th>"
                "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>代码</th>"
                "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>名称</th>"
                "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>股数</th>"
                "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>金额</th>"
                "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>原因</th>"
                "</tr>" + "".join(rows) + "</table>"
                f"<div style='color:#57606a;font-size:12px;margin-top:4px;'>"
                f"紧急程度: {urgency_cn}　|　预估交易成本: {cost_txt}</div>"
            )
        else:
            parts.append(
                "<div style='color:#1a7f37;font-size:13px;padding:6px 10px;"
                "background:#f0f9f4;border-radius:4px;display:inline-block;'>✔ 无需再平衡</div>"
            )
        parts.append("</div>")

        # ============================================================
        # 五、动态仓位建议（可选，单股ATR法）
        # ============================================================
        if isinstance(dynamic_pos, dict) and dynamic_pos:
            try:
                method_cn = {"atr_volatility": "ATR波动率法",
                             "fixed_ratio": "固定比例法"}.get(
                    dynamic_pos.get("method", ""), dynamic_pos.get("method", ""))
                pos_ratio = dynamic_pos.get("position_ratio")
                parts.append(
                    "<div style='border:1px dashed #d0d7de;border-radius:6px;"
                    "padding:8px 14px;margin:10px 0;font-size:13px;color:#24292f;'>"
                    f"💡 动态仓位建议（{method_cn}）：建议股数 "
                    f"<b>{dynamic_pos.get('shares', 'N/A')}</b> 股，金额 "
                    f"<b>{_fmt(dynamic_pos.get('amount'), '{:,.0f}')}元</b>"
                    f"（占比{_fmt(pos_ratio, '{:.1%}')}），"
                    f"ATR波动 {_fmt(dynamic_pos.get('atr_pct'), '{:.2%}')}，"
                    f"风险金额 {_fmt(dynamic_pos.get('risk_amount'), '{:,.0f}')}元"
                    "</div>"
                )
            except Exception:
                pass

        return "".join(parts)
    except Exception as e:
        logger.warning(f"风险行动清单生成失败: {e}")
        try:
            return _insufficient()
        except Exception:
            return ""
