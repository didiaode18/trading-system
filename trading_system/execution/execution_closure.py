"""
持仓执行闭环模块（批1-B公共模块）
==================================
通过"上一交易日快照 vs 当日holdings"对比，自动识别疑似已在券商端执行的交易
（新增/卖出/加仓/减仓/止损价变更），生成邮件确认清单，实现执行闭环校验。

快照文件（仅保留两份）:
    output/holdings_snapshot_prev.json    # 上一份快照
    output/holdings_snapshot_latest.json  # 最新快照

holdings 结构（与 trading_system/holdings.json 一致）:
    {code: {"name", "shares", "buy_price"/"cost", "stop_loss", ...}}

安全约束: 全部函数 try/except 降级，prev 不存在时视为首日基线（has_changes=False）
"""

import os
import json
import logging

logger = logging.getLogger(__name__)

# 默认快照目录: config.OUTPUT_DIR
try:
    import config as _config
except ImportError:  # pragma: no cover - 包内导入兼容
    try:
        from trading_system import config as _config
    except Exception:
        _config = None

if _config is not None and hasattr(_config, "OUTPUT_DIR"):
    DEFAULT_SNAPSHOT_DIR = _config.OUTPUT_DIR
else:
    DEFAULT_SNAPSHOT_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'output'
    )

PREV_FILENAME = "holdings_snapshot_prev.json"
LATEST_FILENAME = "holdings_snapshot_latest.json"


def _resolve_dir(snapshot_dir: str = None) -> str:
    return snapshot_dir or DEFAULT_SNAPSHOT_DIR


def _get_cost(holding: dict):
    """兼容 cost / buy_price 两种成本字段"""
    if not isinstance(holding, dict):
        return None
    v = holding.get("cost")
    if v is None:
        v = holding.get("buy_price")
    return v


def _load_holdings_file(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"持仓快照读取失败({path}): {e}")
    return {}


def snapshot_holdings(holdings: dict, snapshot_dir: str = None) -> str:
    """
    保存当前holdings快照：先把 latest 改名为 prev，再写新 latest（仅保留这两份）

    参数:
        holdings: 当前持仓dict {code: {...}}
        snapshot_dir: 快照目录（None=默认 output/）

    返回: 新 latest 快照文件路径（异常时返回空串，不抛异常）
    """
    try:
        d = _resolve_dir(snapshot_dir)
        os.makedirs(d, exist_ok=True)
        latest_path = os.path.join(d, LATEST_FILENAME)
        prev_path = os.path.join(d, PREV_FILENAME)

        # latest -> prev（覆盖旧prev）
        if os.path.exists(latest_path):
            try:
                if os.path.exists(prev_path):
                    os.remove(prev_path)
                os.replace(latest_path, prev_path)
            except Exception as e:
                logger.warning(f"快照轮转失败: {e}")

        with open(latest_path, 'w', encoding='utf-8') as f:
            json.dump(holdings if isinstance(holdings, dict) else {},
                      f, ensure_ascii=False, indent=2)
        return latest_path
    except Exception as e:
        logger.warning(f"持仓快照保存失败: {e}")
        return ""


def diff_and_confirm(holdings: dict, snapshot_dir: str = None) -> dict:
    """
    对比当前 holdings 与 prev 快照，输出疑似已执行交易确认清单

    参数:
        holdings: 当前持仓dict
        snapshot_dir: 快照目录（None=默认 output/）

    返回:
        {
            "has_changes": bool,
            "items": [{"code","name","action": "新增/卖出/加仓/减仓/止损价变更","detail": str}],
            "html": str,   # 可直接插入邮件的确认清单HTML段落（无变化时为空串）
            "text": str,   # 纯文本版本（无变化时为空串）
        }
    prev 不存在 → has_changes=False 并提示首日基线；绝不抛异常。
    """
    result = {"has_changes": False, "items": [], "html": "", "text": ""}
    try:
        if not isinstance(holdings, dict):
            holdings = {}
        d = _resolve_dir(snapshot_dir)
        prev_path = os.path.join(d, PREV_FILENAME)

        if not os.path.exists(prev_path):
            result["text"] = "首日基线：尚无上一交易日持仓快照，本次仅建立基线。"
            return result

        prev = _load_holdings_file(prev_path)

        items = []
        prev_codes = set(prev.keys())
        curr_codes = set(holdings.keys())

        # 新增
        for code in sorted(curr_codes - prev_codes):
            h = holdings.get(code) or {}
            items.append({
                "code": code,
                "name": h.get("name", code),
                "action": "新增",
                "detail": f"新增持仓 {h.get('shares', 0)}股",
            })

        # 卖出
        for code in sorted(prev_codes - curr_codes):
            h = prev.get(code) or {}
            items.append({
                "code": code,
                "name": h.get("name", code),
                "action": "卖出",
                "detail": f"持仓已全部卖出(原{h.get('shares', 0)}股)",
            })

        # 存续持仓对比
        for code in sorted(prev_codes & curr_codes):
            ph = prev.get(code) or {}
            ch = holdings.get(code) or {}
            name = ch.get("name") or ph.get("name") or code

            ps, cs = ph.get("shares", 0), ch.get("shares", 0)
            if cs > ps:
                items.append({
                    "code": code, "name": name, "action": "加仓",
                    "detail": f"股数 {ps} → {cs} (+{cs - ps})",
                })
            elif cs < ps:
                items.append({
                    "code": code, "name": name, "action": "减仓",
                    "detail": f"股数 {ps} → {cs} (-{ps - cs})",
                })

            psl, csl = ph.get("stop_loss"), ch.get("stop_loss")
            try:
                sl_changed = (psl is not None and csl is not None
                              and abs(float(psl) - float(csl)) > 1e-9)
            except (TypeError, ValueError):
                sl_changed = False
            if sl_changed:
                items.append({
                    "code": code, "name": name, "action": "止损价变更",
                    "detail": f"止损价 {psl} → {csl}",
                })

        if not items:
            return result

        result["has_changes"] = True
        result["items"] = items

        # ---------- 纯文本清单 ----------
        lines = ["疑似已执行交易，请确认："]
        for it in items:
            lines.append(f"  [{it['action']}] {it['code']} {it['name']}：{it['detail']}")
        lines.append("（系统检测到持仓文件与上一交易日快照存在差异，"
                     "若非本人操作请及时核查账户）")
        result["text"] = "\n".join(lines)

        # ---------- HTML清单（内联样式，可直接插入邮件） ----------
        action_colors = {
            "新增": "#1a7f37", "加仓": "#0969da",
            "减仓": "#bf8700", "卖出": "#cf222e", "止损价变更": "#8250df",
        }
        rows = []
        for it in items:
            color = action_colors.get(it["action"], "#57606a")
            rows.append(
                "<tr>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #e5e7eb;'>"
                f"<span style='color:#fff;background:{color};padding:2px 8px;"
                f"border-radius:3px;font-size:12px;'>{it['action']}</span></td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #e5e7eb;"
                f"font-family:Consolas,monospace;'>{it['code']}</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #e5e7eb;'>{it['name']}</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #e5e7eb;'>{it['detail']}</td>"
                "</tr>"
            )
        html = (
            "<div style='border-left:4px solid #cf222e;padding:10px 14px;"
            "background:#fff8f8;margin:12px 0;'>"
            "<div style='font-weight:bold;color:#cf222e;font-size:15px;"
            "margin-bottom:8px;'>⚠ 疑似已执行交易，请确认</div>"
            "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
            "<tr style='background:#f6f8fa;'>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>动作</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>代码</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>名称</th>"
            "<th style='padding:6px 10px;text-align:left;border-bottom:2px solid #d0d7de;'>详情</th>"
            "</tr>"
            + "".join(rows) +
            "</table>"
            "<div style='color:#57606a;font-size:12px;margin-top:6px;'>"
            "系统检测到持仓文件与上一交易日快照存在差异，若非本人操作请及时核查账户"
            "</div></div>"
        )
        result["html"] = html
        return result
    except Exception as e:
        logger.warning(f"持仓对比失败: {e}")
        return result
