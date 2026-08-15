# -*- coding: utf-8 -*-
"""
V4.1(M3): 预警闭环统计区块
==========================
把 alert_ledger 台账（预警发出→T+5前瞻收益回填）聚合结果接入综合分析报告，
让预警命中率/误报情况可见，为阈值校准提供数据支撑。

口径说明:
    - 已回填(settled): 预警发出满5个交易日后已回填前瞻收益
    - 风险预警有效性: 预警后标的下跌(fwd_return<0)视为"预警有效"
      （对止损/异动类预警成立；买入类信号解读方向相反，本区块只展示
      原始统计，不做强解读）

安全约束: 无台账/异常返回空字符串，绝不阻断报告。
"""

import logging

logger = logging.getLogger(__name__)


def render_alert_stats_html(min_total: int = 3) -> str:
    """
    生成预警闭环统计HTML区块。

    参数:
        min_total: 台账记录低于该值时不展示（样本太少无统计意义）
    """
    try:
        from notify.alert_ledger import aggregate_stats
        stats = aggregate_stats()
        if stats.get("total", 0) < min_total:
            return ""

        settled = stats.get("settled", 0)
        total = stats["total"]
        rows = (f"<div style='font-size:13px;margin-top:6px'>"
                f"台账累计预警 {total} 条 | 已回填前瞻收益 {settled} 条"
                f"（回填率{settled/total*100:.0f}%，T+5结算）</div>")

        # 按规则展示: 次数 + 预警后平均前瞻收益
        by_rule = stats.get("by_rule", {})
        if by_rule:
            parts = []
            for rule, item in sorted(by_rule.items(), key=lambda x: -x[1]["count"])[:6]:
                fr = item.get("avg_fwd_return")
                fr_txt = (f"{fr*100:+.2f}%" if isinstance(fr, (int, float)) else "未回填")
                parts.append(f"{rule}×{item['count']}（后验{fr_txt}）")
            rows += (f"<div style='font-size:12px;color:#595959;margin-top:4px'>"
                     f"按规则: {' | '.join(parts)}</div>")

        # 有效性提示: 风险预警后下跌占比（仅粗口径提示）
        try:
            from notify.alert_ledger import _resolve_path, _load_ledger
            recs = _load_ledger(_resolve_path())
            settled_recs = [r for r in recs if isinstance(r, dict)
                            and r.get("fwd_return") is not None]
            if len(settled_recs) >= min_total:
                down_cnt = sum(1 for r in settled_recs
                               if float(r["fwd_return"]) < 0)
                rows += (f"<div style='font-size:12px;color:#595959;margin-top:4px'>"
                         f"预警后5日下跌占比: {down_cnt}/{len(settled_recs)}"
                         f"（{down_cnt/len(settled_recs)*100:.0f}%，风险类预警该值越高说明预警越准）</div>")
        except Exception:
            pass

        return (f"<div style='max-width:1000px;margin:10px auto;background:#f9f0ff;"
                f"border:1px solid #d3adf7;border-radius:8px;padding:12px 18px'>"
                f"<div style='font-weight:bold;color:#531dab;font-size:14px'>"
                f"🔔 预警闭环统计</div>{rows}"
                f"<div style='font-size:11px;color:#8c8c8c;margin-top:4px'>"
                f"口径: 预警发出后T+5真实收益回填；持续低命中率的规则应考虑"
                f"收紧阈值，减少'狼来了'式打扰。</div></div>")
    except Exception as e:
        logger.warning(f"[预警统计区块] 渲染失败(不影响报告): {e}")
        return ""
