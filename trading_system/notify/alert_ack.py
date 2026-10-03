"""
盘中预警确认(ACK)模块
=====================
实现用户对钉钉预警的"已处理"确认，确认后当日静默同标的同规则预警，
条件恶化时(紧急度升级/新规则触发)重新触发并扣减已确认数量。

存储文件: data/alert_ack_state.json
格式: {
    "date": "2026-08-17",
    "ack_records": {
        "{code}": {
            "rule": "R8-趋势级别下降",
            "level": "critical",
            "ack_time": "2026-08-17 09:45:00",
            "urgency_score": 75,
            "ack_qty": 29500,
            "total_suggested_qty": 29500
        }
    },
    "pending_tokens": {
        "{token}": {
            "code": "159611",
            "rule": "R8-趋势级别下降",
            "level": "critical",
            "urgency_score": 75,
            "expires": "2026-08-18 09:45:00"
        }
    }
}

安全约束: 全部函数 try/except 降级，绝不抛异常影响主流程
"""

import os
import json
import logging
import datetime
import re
import uuid

logger = logging.getLogger(__name__)

# 默认存储路径
try:
    import config as _config
except ImportError:
    try:
        from trading_system import config as _config
    except Exception:
        _config = None

if _config is not None and hasattr(_config, "PROJECT_ROOT"):
    _DATA_DIR = os.path.join(_config.PROJECT_ROOT, "data")
else:
    _DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')

_ACK_STATE_FILE = os.path.join(_DATA_DIR, "alert_ack_state.json")

# ============================================================
# 持久化读写
# ============================================================

def _load_state() -> dict:
    """读取状态文件，失败返回空结构；自动迁移旧格式ack_records→delivered_records"""
    try:
        if os.path.exists(_ACK_STATE_FILE):
            with open(_ACK_STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                # V26: 迁移旧格式 ack_records → delivered_records
                if "ack_records" in data and "delivered_records" not in data:
                    data["delivered_records"] = data.pop("ack_records", {})
                data.pop("pending_tokens", None)  # V26: 令牌机制已废弃
                return data
    except Exception as e:
        logger.warning(f"状态读取失败: {e}")
    return {"date": "", "delivered_records": {}}


def _save_state(state: dict) -> None:
    """写入ACK状态文件，失败仅记日志"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(_ACK_STATE_FILE)), exist_ok=True)
        with open(_ACK_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"ACK状态写入失败: {e}")


def _today_str() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


def _get_today_state() -> dict:
    """获取今日状态，非今日则重置"""
    state = _load_state()
    today = _today_str()
    if state.get("date") != today:
        # 新交易日自动重置
        state = {
            "date": today,
            "delivered_records": {},
        }
        _save_state(state)
    return state


# ============================================================
# 核心查询接口
# ============================================================

def is_delivered(code: str, rule: str, level: str, urgency_score: float) -> bool:
    """V26: 判断预警是否已发送过（自动静默）

    返回True表示该预警当日已提醒过，应被静默。
    条件恶化(urgency提升>=15分或新规则)时返回False(允许再提醒一次)。
    """
    try:
        state = _get_today_state()
        delivered = state.get("delivered_records", {})

        rec = delivered.get(code)
        if not rec:
            return False

        # 同规则已提醒过 → 静默
        if rec.get("rule") == rule:
            return not _is_worsened(rec, urgency_score, rule)

        # 不同规则 → 视为恶化，不静默
        return False
    except Exception as e:
        logger.warning(f"静默检查失败: {e}")
        return False


# V26: 向后兼容别名
is_silenced = is_delivered


def _is_worsened(delivered: dict, new_urgency: float, new_rule: str) -> bool:
    """判断条件是否恶化：紧急度提升>=15分 或 触发了新的更严重规则

    V26: 新增urgency差值阈值(15分)，避免微小波动触发重复推送
    """
    old_urgency = delivered.get("urgency_score", 0)
    old_rule = delivered.get("rule", "")

    # V26: 紧急度提升需>=15分才算恶化(避免85→88的微小波动触发再提醒)
    if new_urgency - old_urgency >= 15:
        return True

    # 新规则(不同于已提醒规则)视为恶化
    if new_rule != old_rule:
        return True

    return False


def get_ack_record(code: str) -> dict:
    """获取标的今日记录，无记录返回空dict"""
    try:
        state = _get_today_state()
        return state.get("delivered_records", {}).get(code, {})
    except Exception:
        return {}


# ============================================================
# V8.3: 跨周期方向记忆查询
# ============================================================

_BUY_KW = ("回调加仓", "加仓机会", "可以加仓", "突破加仓")
_SELL_KW = ("紧急卖出", "建议减仓", "止损", "清仓", "跌破", "暴跌", "趋势降级")


def get_last_signal_direction(code: str) -> dict:
    """V8.3: 查询该标的最近一次已推送信号的方向

    返回:
        {"direction": "buy"/"sell"/"none", "rule": str, "time": str, "urgency": float}
    """
    try:
        rec = get_ack_record(code)
        if not rec:
            return {"direction": "none", "rule": "", "time": "", "urgency": 0}
        rule = rec.get("rule", "")
        # 判断方向
        is_buy = any(k in rule for k in _BUY_KW)
        is_sell = any(k in rule for k in _SELL_KW)
        if is_sell:
            direction = "sell"
        elif is_buy:
            direction = "buy"
        else:
            direction = "none"
        return {
            "direction": direction,
            "rule": rule,
            "time": rec.get("ack_time", ""),
            "urgency": rec.get("urgency_score", 0),
        }
    except Exception:
        return {"direction": "none", "rule": "", "time": "", "urgency": 0}


# ============================================================
# 确认操作
# ============================================================

def record_delivery(code: str, rule: str, level: str, urgency_score: float,
                    suggested_qty: int = 0) -> bool:
    """V26: 记录预警已发送（自动静默，无需用户确认）

    发送成功后自动调用，登记该标的+规则当日已提醒。
    后续同标的同规则预警将被静默，除非条件恶化。
    """
    try:
        state = _get_today_state()
        delivered = state.setdefault("delivered_records", {})

        # 如果已有记录且urgency更高，不覆盖（保留最高基准用于恶化判断）
        existing = delivered.get(code)
        if existing and existing.get("urgency_score", 0) > urgency_score:
            return True  # 已有更高urgency记录，无需更新

        delivered[code] = {
            "rule": rule,
            "level": level,
            "ack_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "urgency_score": urgency_score,
            "ack_qty": suggested_qty,
            "total_suggested_qty": suggested_qty,
        }

        _save_state(state)
        logger.info(f"  [静默登记] {code} {rule} 紧急度{urgency_score} (当日已提醒)")
        return True
    except Exception as e:
        logger.warning(f"发送登记失败: {e}")
        return False


# V26: 向后兼容别名
record_ack = record_delivery


# ============================================================
# 数量扣减
# ============================================================

def parse_action_qty(action_text: str) -> int:
    """从操作建议文本中解析数量(股)

    支持格式:
        "建议减仓29500股" → 29500
        "建议卖出10000股(34%仓位)" → 10000
        "建议清仓5000股" → 5000
        "减仓1/3(约5000股)" → 5000
    """
    if not action_text:
        return 0
    try:
        # 优先匹配"XX股"格式
        m = re.search(r'(\d+)\s*股', action_text)
        if m:
            return int(m.group(1))
        # 备选：纯数字
        m = re.search(r'(\d+)', action_text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0


def calc_deducted_qty(code: str, new_suggested_qty: int) -> int:
    """计算扣减已确认数量后的剩余建议数量

    例: 首次建议减仓29500股→用户确认；后续恶化重新触发时用户又手动减了10000股，
    则新预警建议数量 = new_suggested_qty - 已确认数量(29500) 的差值。

    返回: 扣减后的剩余数量(最小为0)
    """
    try:
        ack = get_ack_record(code)
        if not ack:
            return new_suggested_qty

        ack_qty = ack.get("ack_qty", 0)
        remaining = max(0, new_suggested_qty - ack_qty)
        return remaining
    except Exception:
        return new_suggested_qty


def update_ack_qty(code: str, additional_qty: int) -> None:
    """追加确认数量(用户分次操作时累加)"""
    try:
        state = _get_today_state()
        ack = state.get("ack_records", {}).get(code)
        if ack:
            ack["ack_qty"] = ack.get("ack_qty", 0) + additional_qty
            _save_state(state)
    except Exception as e:
        logger.warning(f"ACK数量更新失败: {e}")


# ============================================================
# 令牌(Token)机制 —— 用于钉钉按钮回调 / CLI确认
# ============================================================

def generate_token(code: str, rule: str, level: str, urgency_score: float) -> str:
    """生成确认令牌，存入pending_tokens"""
    try:
        state = _get_today_state()
        tokens = state.setdefault("pending_tokens", {})

        token = uuid.uuid4().hex[:12]
        tokens[token] = {
            "code": code,
            "rule": rule,
            "level": level,
            "urgency_score": urgency_score,
            "expires": (datetime.datetime.now() + datetime.timedelta(hours=12)).strftime(
                "%Y-%m-%d %H:%M:%S"),
        }
        _save_state(state)
        return token
    except Exception as e:
        logger.warning(f"ACK令牌生成失败: {e}")
        return ""


def confirm_by_token(token: str) -> dict:
    """通过令牌确认预警

    返回: {"success": bool, "code": str, "rule": str, "msg": str}
    """
    try:
        state = _get_today_state()
        tokens = state.get("pending_tokens", {})

        info = tokens.get(token)
        if not info:
            return {"success": False, "code": "", "rule": "", "msg": "令牌不存在或已过期"}

        # 检查过期
        expires = info.get("expires", "")
        if expires and datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") > expires:
            return {"success": False, "code": info.get("code", ""), "rule": "",
                    "msg": "令牌已过期"}

        code = info["code"]
        rule = info["rule"]
        level = info["level"]
        urgency = info.get("urgency_score", 0)

        # 记录ACK
        ok = record_ack(code, rule, level, urgency)

        # FIX: 重新加载state以获取record_ack写入的最新ack_records，
        # 避免用旧的state覆盖导致ack_records丢失
        state = _load_state()
        tokens = state.get("pending_tokens", {})

        # 清除已用令牌
        tokens.pop(token, None)
        state["pending_tokens"] = tokens
        _save_state(state)

        if ok:
            return {"success": True, "code": code, "rule": rule,
                    "msg": f"✅ {code} {rule} 已确认，当日静默"}
        else:
            return {"success": False, "code": code, "rule": rule,
                    "msg": "确认记录失败"}
    except Exception as e:
        return {"success": False, "code": "", "rule": "", "msg": f"确认异常: {e}"}


def cleanup_expired_tokens() -> None:
    """清理过期令牌"""
    try:
        state = _load_state()
        tokens = state.get("pending_tokens", {})
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        expired = [t for t, info in tokens.items()
                   if info.get("expires", "") < now_str]
        for t in expired:
            tokens.pop(t, None)
        if expired:
            state["pending_tokens"] = tokens
            _save_state(state)
    except Exception:
        pass


# ============================================================
# ACK摘要(供预警消息展示)
# ============================================================

def get_ack_summary(code: str) -> str:
    """获取标的今日提醒摘要"""
    try:
        rec = get_ack_record(code)
        if not rec:
            return ""
        ack_time = rec.get("ack_time", "")
        rule = rec.get("rule", "")
        return f"已提醒 {ack_time} | {rule}"
    except Exception:
        return ""


# ============================================================
# 钉钉ActionCard格式化
# ============================================================

def build_action_card_md(alerts: list, header_title: str,
                         now_str: str, today_str: str) -> str:
    """构建钉钉ActionCard的Markdown内容(含确认按钮提示)

    返回: 完整的ActionCard text字段
    """
    level_emoji = {"critical": "🔴", "high": "🟠", "warning": "🟡", "info": "🔵"}
    cards = []
    for a in alerts[:3]:
        hi = a.get("holdings_info", {})
        _buy_p = hi.get("buy_price", 0)
        _cur_p = hi.get("current_price", 0)
        _stop_p = hi.get("stop_loss", 0)
        _shares = hi.get("shares", 0)
        _pnl = hi.get("pnl_pct", 0)
        _score = a.get("urgency_score", 0)
        _level = a.get("level", "info")
        _emoji = level_emoji.get(_level, "⚪")

        # 数量扣减
        _action = a.get("_action_text", "")
        _orig_qty = parse_action_qty(_action)
        _remaining_qty = calc_deducted_qty(a.get("code", ""), _orig_qty)
        if _remaining_qty < _orig_qty and _orig_qty > 0:
            _deduct_note = f"\n\n> ⚠️ 已确认{_orig_qty - _remaining_qty}股，剩余{_remaining_qty}股待处理"
        else:
            _deduct_note = ""

        # ACK摘要
        _ack_summary = get_ack_summary(a.get("code", ""))
        _ack_line = f"\n\n> 📋 {_ack_summary}" if _ack_summary else ""

        _card = (
            f"{_emoji} **{a.get('name', '')}({a.get('code', '')})** "
            f"紧急度{_score}\n\n"
            f"📌 {a.get('rule_name', '')} — {a.get('rule_detail', a.get('msg', ''))}\n\n"
            + (f"➕ 附加: {a.get('extra_rules', [{}])[0].get('rule_name', '')}\n\n"
               if a.get("extra_rules") else "")
            + f"🎯 **{_action}**{_deduct_note}\n\n"
            f"📊 成本{_buy_p:.2f} | 现价{_cur_p:.2f} | 止损{_stop_p:.2f} | "
            f"浮盈亏{_pnl:+.1f}% | 持仓{_shares}股"
            f"{_ack_line}"
        )
        cards.append(_card)

    n = len(alerts)
    body = (f"### {header_title}\n\n---\n\n"
            + "\n\n---\n\n".join(cards)
            + (f"\n\n---\n\n...其余{n - 3}条见预警邮件" if n > 3 else "")
            + f"\n\n> {today_str} {now_str} | 紧急度90+=立即操作 / 70-89=尽快处理")
    return body


def build_action_card_payload(alerts: list, header_title: str,
                              now_str: str, today_str: str,
                              confirm_url_base: str = "") -> dict:
    """构建钉钉ActionCard payload

    V26: 移除确认按钮（自动静默模式，无需用户确认）
    confirm_url_base参数保留但不再使用（向后兼容）

    返回: 可直接json.dumps发送的payload dict
    """
    try:
        from notify.wechat_notify import _ensure_keyword
    except ImportError:
        from trading_system.notify.wechat_notify import _ensure_keyword

    # Markdown内容
    md_text = build_action_card_md(alerts, header_title, now_str, today_str)

    # V26: 移除确认按钮提示，改为自动静默提示
    md_text += "\n\n---\n\n> \U0001f4e2 同标的同规则当日仅提醒一次，条件恶化时自动再提醒"

    # 关键词安全
    md_text = _ensure_keyword(md_text)
    title = _ensure_keyword(header_title)

    # V26: 无按钮ActionCard（singleTitle仅展示，无实际链接）
    payload = {
        "msgtype": "actionCard",
        "actionCard": {
            "title": title,
            "text": md_text,
            "singleTitle": "\U0001f4cb 查看详情",
            "singleURL": "",
        }
    }

    return payload
