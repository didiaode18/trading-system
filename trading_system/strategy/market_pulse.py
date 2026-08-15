# -*- coding: utf-8 -*-
"""
市场情绪与资金面面板（Market Pulse）
====================================
为综合分析报告与选股报告提供统一的"市场全局研判"面板数据。
对应框架模块一（市场全局研判）的增量能力：
  - 涨跌家数 / 涨停跌停 / 急涨急跌（市场宽度）
  - 两市成交额（量能水位，乐咕→腾讯实时→akshare指数→baostock四级兜底）
  - 沪市两融余额（杠杆资金水位）
  - 综合情绪三档（狂热/中性/恐慌）+ 逆向提示

FIX(2026-08-12): 北向资金因子整体移除 —— 交易所自2024-08-19起停止披露
北向逐日净流入，任何数据源都无法获取，面板长期显示"-"只会误导判断。

设计约束:
  1. 全数据源 try/except 降级，任一源失败只标记 degraded，绝不抛异常；
  2. 当日缓存（market_pulse_cache.json），重复调用不重复联网；
  3. 不修改 config.py，阈值集中在本模块 PULSE_CFG。

使用方式:
    from strategy.market_pulse import get_market_pulse, render_pulse_html
    data = get_market_pulse()
    html += render_pulse_html(data)
"""

import os
import json
import logging
import datetime

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

# 缓存文件（与既有数据缓存同目录）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TS_DIR = os.path.dirname(_THIS_DIR)
PULSE_CACHE_FILE = os.path.join(_TS_DIR, "data", "market_pulse_cache.json")

# 模块内默认参数（不修改 config.py）
PULSE_CFG = {
    "amount_hot": 15000,        # 两市成交额(亿) ≥ 此值视为放量
    "amount_cold": 7000,        # 两市成交额(亿) ≤ 此值视为缩量冰点
    "amount_stale_days": 4,     # 日K兜底源允许的交易日滞后上限(超过视为陈旧弃用)
    "limit_up_hot": 80,         # 涨停家数 ≥ 此值视为情绪火热
    "limit_down_panic": 30,     # 跌停家数 ≥ 此值视为恐慌
    "fever_threshold": 70,      # 情绪分 ≥ 70 → 狂热
    "panic_threshold": 40,      # 情绪分 < 40 → 恐慌
}

_LEVEL_META = {
    "狂热": {"color": "#e74c3c", "icon": "🔥"},
    "中性": {"color": "#f57c00", "icon": "⚖️"},
    "恐慌": {"color": "#2e7d32", "icon": "🧊"},
    "未知": {"color": "#95a5a6", "icon": "❔"},
}


# ============================================================
# 一、数据源获取（各自独立降级）
# ============================================================

def _fetch_amount_tencent() -> float:
    """
    两市成交额备用源: 腾讯行情(上证综指+深证综指成交额, 单位亿)。
    乐咕接口不再提供成交额字段时的兜底；行情时间戳非当日则视为陈旧不用。
    """
    try:
        import requests
        r = requests.get("https://qt.gtimg.cn/q=sh000001,sz399106", timeout=8)
        r.encoding = "gbk"
        today = datetime.date.today().strftime("%Y%m%d")
        total, got = 0.0, False
        for line in r.text.split(";"):
            if "v_sh000001" not in line and "v_sz399106" not in line:
                continue
            fields = line.split("~")
            if len(fields) < 36:
                continue
            if str(fields[30])[:8] != today:  # 时间戳新鲜度校验
                continue
            seg = str(fields[35]).split("/")  # "现价/成交量/成交额(元)"
            if len(seg) >= 3 and seg[2]:
                total += float(seg[2]) / 1e8
                got = True
        return round(total, 0) if got and total > 0 else None
    except Exception as e:
        logger.debug(f"[市场脉搏] 腾讯成交额获取失败: {e}")
        return None


def _fetch_amount_index() -> float:
    """
    FIX(2026-08-12): 两市成交额第三兜底源 —— akshare指数日线成交额
    (上证综指000001 + 深证成指399001, 单位亿)。取当日或最近交易日，
    乐咕与腾讯双源全挂时保证量能水位不断供。任一指数失败即弃用。
    """
    if not HAS_AKSHARE:
        return None
    try:
        end = datetime.date.today().strftime("%Y%m%d")
        start = (datetime.date.today() - datetime.timedelta(days=10)).strftime("%Y%m%d")
        total = 0.0
        for sym in ("000001", "399001"):
            df = ak.index_zh_a_hist(symbol=sym, period="daily",
                                    start_date=start, end_date=end)
            if df is None or df.empty:
                return None
            amt_col = None
            for c in df.columns:
                if "成交额" in str(c):
                    amt_col = c
                    break
            if amt_col is None:
                return None
            v = float(df[amt_col].iloc[-1])
            if v > 1e10:  # 以元为单位归一为亿
                v /= 1e8
            total += v
        return round(total, 0) if total > 0 else None
    except Exception as e:
        logger.debug(f"[市场脉搏] 指数成交额兜底获取失败: {e}")
        return None


def _fetch_amount_baostock():
    """
    FIX(2026-08-12): 两市成交额第四兜底源 —— baostock指数日线
    (项目既有依赖，update_holdings.py已用)。腾讯日K接口只返回成交量无
    成交额，故选用baostock的amount字段(元)。取最近已完成交易日：
    ①盘前/集合竞价阶段实时源成交额为0，此源保证不断供；
    ②东财接口被断连(index_zh_a_hist失败)时此源独立可用。
    滞后超过 amount_stale_days 个自然日则视为陈旧弃用。

    返回: (成交额亿 | None, 数据交易日ISO | None)
    """
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            return None, None
        try:
            end = datetime.date.today()
            start = end - datetime.timedelta(days=12)
            total, last_date = 0.0, None
            for code in ("sh.000001", "sz.399001"):
                rs = bs.query_history_k_data_plus(
                    code, "date,amount",
                    start_date=start.isoformat(), end_date=end.isoformat(),
                    frequency="d")
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if not rows or not rows[-1][1]:
                    return None, None
                d = datetime.datetime.strptime(rows[-1][0], "%Y-%m-%d").date()
                if last_date is None:
                    last_date = d
                elif d != last_date:
                    return None, None  # 两指数最新交易日不一致，数据不可靠
                total += float(rows[-1][1]) / 1e8
            if last_date is None or total <= 0:
                return None, None
            if (end - last_date).days > PULSE_CFG["amount_stale_days"]:
                logger.debug(f"[市场脉搏] baostock成交额陈旧({last_date})，弃用")
                return None, None
            return round(total, 0), last_date.isoformat()
        finally:
            bs.logout()
    except Exception as e:
        logger.debug(f"[市场脉搏] baostock成交额获取失败: {e}")
        return None, None


def _fetch_market_activity() -> dict:
    """
    乐咕市场活跃度: 涨跌家数/涨停跌停/两市成交额
    返回: {"success": bool, "up": int, "down": int, "limit_up": int,
           "limit_down": int, "amount": float(亿), ...}

    新鲜度校验(V5修复): 乐咕偶发返回前一交易日快照，若"统计日期"非当日
    则整源判失败，杜绝陈旧涨跌家数被当作当日数据缓存并推高情绪分。
    """
    out = {"success": False, "up": None, "down": None, "limit_up": None,
           "limit_down": None, "amount": None}
    if not HAS_AKSHARE:
        return out
    try:
        df = ak.stock_market_activity_legu()
        if df is None or df.empty:
            return out
        kv = {}
        # 兼容两种列名结构: (item, value) 或 (项目, 值)
        cols = list(df.columns)
        if len(cols) >= 2:
            for _, row in df.iterrows():
                kv[str(row[cols[0]])] = row[cols[1]]

        # 新鲜度校验: 统计日期必须为当日
        stat_date = str(kv.get("统计日期", ""))[:10]
        if stat_date and stat_date != datetime.date.today().isoformat():
            logger.debug(f"[市场脉搏] 乐咕数据陈旧(统计日期{stat_date})，弃用")
            return out

        def _to_int(key):
            try:
                v = kv.get(key)
                return int(float(v)) if v is not None else None
            except (ValueError, TypeError):
                return None

        def _to_amount(key):
            """两市成交额单位兼容: 元 / 亿元 均归一化为亿"""
            try:
                v = kv.get(key)
                if v is None:
                    return None
                v = float(str(v).replace(",", ""))
                if v > 1e10:      # 以元为单位
                    v = v / 1e8
                return round(v, 0)
            except (ValueError, TypeError):
                return None

        out["up"] = _to_int("上涨")
        out["down"] = _to_int("下跌")
        out["limit_up"] = _to_int("涨停")
        out["limit_down"] = _to_int("跌停")
        out["up_sharp"] = _to_int("急速上涨")
        out["down_sharp"] = _to_int("急速下跌")
        out["amount"] = _to_amount("两市成交额")
        out["success"] = (out["up"] is not None) or (out["limit_up"] is not None)
    except Exception as e:
        logger.debug(f"[市场脉搏] 市场活跃度获取失败: {e}")
    return out


def _fetch_margin_market() -> dict:
    """沪市融资融券余额水位（市场级杠杆资金，取上交所汇总，轻量）"""
    out = {"success": False, "balance": None, "change_pct": None}
    if not HAS_AKSHARE:
        return out
    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=15)
        df = ak.stock_margin_sse(
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"))
        if df is None or df.empty:
            return out
        # 列名兼容: 融资余额 / 融资余额(元)
        bal_col = None
        for c in df.columns:
            if "融资余额" in str(c):
                bal_col = c
                break
        if bal_col is None:
            return out
        bals = df[bal_col].astype(float).values
        last = bals[-1]
        # 单位归一为亿元
        if last > 1e10:
            bals = bals / 1e8
        out["balance"] = round(float(bals[-1]), 0)
        if len(bals) >= 2 and bals[-2] > 0:
            out["change_pct"] = round((bals[-1] / bals[-2] - 1) * 100, 2)
        out["success"] = True
    except Exception as e:
        logger.debug(f"[市场脉搏] 两融余额获取失败: {e}")
    return out


# ============================================================
# 二、情绪评分与三档定性
# ============================================================

def _calc_sentiment(act: dict, mg: dict) -> dict:
    """
    按可用数据源加权计算 0-100 情绪分（缺失源自动重新归一权重）。
    成分: 涨跌家数(30) 涨停强度(25) 量能水位(20) 两融趋势(10)
    FIX(2026-08-12): 北向成分(原权重15)已移除(2024-08起停止披露)。

    V5.1修复(低置信护栏): 市场宽度成分(涨跌家数/涨停/成交额, 合计权重75)
    全部缺失时，仅剩两融等边缘成分，归一化会把单一成分(如两融日环比
    +0.9%)放大成96分误判"狂热"。此时不能定性情绪，返回 level=未知 +
    low_confidence 标记，面板如实展示"数据不足"，杜绝误导性高热/低温结论。
    """
    weights, scores, notes = [], [], []
    has_width = False  # 是否含市场宽度成分(涨跌家数/涨停/成交额)

    # 1. 涨跌家数
    if act.get("up") is not None and act.get("down") is not None:
        total = act["up"] + act["down"]
        ratio = act["up"] / total if total > 0 else 0.5
        weights.append(30)
        scores.append(ratio * 100)
        has_width = True

    # 2. 涨停强度（涨停多+跌停少为高分）
    if act.get("limit_up") is not None:
        lu = act["limit_up"]
        ld = act.get("limit_down") or 0
        s = min(1.0, lu / PULSE_CFG["limit_up_hot"]) * 100
        if ld >= PULSE_CFG["limit_down_panic"]:
            s = max(0.0, s - 30)  # 大面积跌停显著拉低情绪
        weights.append(25)
        scores.append(s)
        has_width = True

    # 3. 量能水位
    # FIX(2026-08-12): 前日兜底成交额不代表当日情绪，仅展示不计分；
    # 否则涨跌家数全缺时会被放大成"狂热100分"误判(绕过V5.1护栏)
    if act.get("amount") is not None and (act.get("amount_date") or "")[:10] in ("", datetime.date.today().isoformat()):
        amt = act["amount"]
        lo, hi = PULSE_CFG["amount_cold"], PULSE_CFG["amount_hot"]
        s = max(0.0, min(1.0, (amt - lo) / (hi - lo))) * 100 if hi > lo else 50
        weights.append(20)
        scores.append(s)
        has_width = True

    # 4. 两融日环比趋势
    if mg.get("success") and mg.get("change_pct") is not None:
        chg = mg["change_pct"]
        # ±1% 映射到 0-100
        s = max(0.0, min(1.0, (chg + 1) / 2)) * 100
        weights.append(10)
        scores.append(s)

    if not weights:
        return {"temperature": None, "level": "未知", "components": 0}

    # V5.1护栏: 市场宽度成分全缺 → 仅凭两融不足以定性情绪
    if not has_width:
        return {"temperature": None, "level": "未知",
                "components": len(weights), "low_confidence": True}

    temperature = int(sum(w * s for w, s in zip(weights, scores)) / sum(weights))
    if temperature >= PULSE_CFG["fever_threshold"]:
        level = "狂热"
    elif temperature < PULSE_CFG["panic_threshold"]:
        level = "恐慌"
    else:
        level = "中性"
    return {"temperature": temperature, "level": level,
            "components": len(weights)}


def _build_advice(level: str, act: dict) -> str:
    """三档情绪对应的操作基调 + 逆向提示"""
    if level == "狂热":
        tips = ["情绪偏热，警惕热门赛道拥挤踩踏，追高需谨慎"]
        if act.get("limit_down") and act["limit_down"] >= 15:
            tips.append("涨停多但跌停亦不少，分化加剧")
        return "；".join(tips)
    if level == "恐慌":
        tips = ["情绪冰点区，历史上多为逆向布局窗口，但需等待止跌信号再动手"]
        return "；".join(tips)
    if level == "中性":
        return "情绪中性，按既定策略与仓位纪律执行，无需额外加减仓"
    return "市场情绪数据不足，按中性对待"


# ============================================================
# 三、对外主入口
# ============================================================

def _load_cache() -> dict:
    try:
        if os.path.exists(PULSE_CACHE_FILE):
            with open(PULSE_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cache(cache: dict):
    try:
        os.makedirs(os.path.dirname(PULSE_CACHE_FILE), exist_ok=True)
        with open(PULSE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.debug(f"[市场脉搏] 缓存写入失败: {e}")


def get_market_pulse(force: bool = False) -> dict:
    """
    获取市场情绪与资金面面板数据（当日缓存，全源降级）

    返回:
        {
            "success": bool, "date": "YYYY-MM-DD",
            "degraded": [str],          # 降级数据源说明（报告汇总展示）
            "temperature": 0-100 | None,
            "level": "狂热/中性/恐慌/未知",
            "advice": str,
            "activity": {...}, "margin": {...},
        }
    """
    today = datetime.date.today().isoformat()
    cache = _load_cache()
    cached = cache.get("latest", {})
    if (not force) and cached.get("date") == today and cached.get("success"):
        # FIX(2026-08-12): 缓存中成交额若为前日值(盘前baostock兜底产生)，
        # 09:40后允许重新拉取，争取实时源给出当日真实量能
        _amt_d = (cached.get("activity") or {}).get("amount_date")
        _amt_stale = _amt_d is not None and _amt_d != today
        if not _amt_stale or datetime.datetime.now().strftime("%H%M") < "0940":
            return cached

    degraded = []

    act = _fetch_market_activity()
    # 成交额兜底链: 乐咕 → 腾讯实时指数 → akshare指数日线 → baostock指数日线
    # FIX(2026-08-12): 新增第三/四级兜底，盘前时段与东财断连时量能不断供
    if act.get("amount") is None:
        _v = _fetch_amount_tencent()
        if _v is not None:
            act["amount"] = _v
    if act.get("amount") is None:
        _v = _fetch_amount_index()
        if _v is not None:
            act["amount"] = _v
    if act.get("amount") is None:
        _v, _vdate = _fetch_amount_baostock()
        if _v is not None:
            act["amount"] = _v
            act["amount_date"] = _vdate  # 可能为前一交易日，渲染/缓存据此标注
    if not act.get("success"):
        degraded.append("市场活跃度(涨跌家数)获取失败")
    if act.get("amount") is None:
        degraded.append("两市成交额获取失败")

    mg = _fetch_margin_market()
    if not mg.get("success"):
        degraded.append("两融余额获取失败")

    senti = _calc_sentiment(act, mg)
    level = senti["level"]
    result = {
        "success": senti["components"] > 0,
        "date": today,
        "degraded": degraded,
        "temperature": senti["temperature"],
        "level": level,
        "advice": _build_advice(level, act),
        "activity": act,
        "margin": mg,
    }

    # 成交额历史（保留10日，供量能环比）
    # FIX(2026-08-12): 前日兜底值不记入当日历史，避免环比基准被污染
    history = cache.get("amount_history", [])
    if act.get("amount") is not None and (act.get("amount_date") or today) == today:
        if not history or history[-1].get("date") != today:
            history.append({"date": today, "amount": act["amount"]})
            history = history[-10:]
            cache["amount_history"] = history
    if len(history) >= 2 and history[-1]["date"] == today:
        prev_amt = history[-2].get("amount") or 0
        if prev_amt > 0:
            result["amount_change_pct"] = round(
                (act["amount"] / prev_amt - 1) * 100, 1)

    cache["latest"] = result
    _save_cache(cache)
    return result


# ============================================================
# 四、HTML渲染（综合报告完整面板 / 选股报告紧凑条）
# ============================================================

def _fmt(val, suffix="", nd=0):
    """安全格式化，None显示为'-'（降级禁止空白）"""
    if val is None:
        return "-"
    try:
        return f"{float(val):,.{nd}f}{suffix}"
    except (ValueError, TypeError):
        return "-"


def render_pulse_html(data: dict, compact: bool = False) -> str:
    """
    渲染市场情绪与资金面面板HTML。
    compact=True 输出单行紧凑条（选股报告头部），False 输出完整卡片组。
    渲染层 key 与 get_market_pulse 返回结构完全匹配，任何异常返回降级提示块。
    """
    try:
        if not data or not data.get("success"):
            return ('<div class="alert alert-info">🌡️ 市场情绪面板: '
                    '数据暂不可用，按中性环境对待</div>')

        meta = _LEVEL_META.get(data.get("level", "未知"), _LEVEL_META["未知"])
        act = data.get("activity", {}) or {}
        mg = data.get("margin", {}) or {}
        temp = data.get("temperature")
        temp_txt = f"{temp}分" if temp is not None else "-"
        advice = data.get("advice", "")

        up, down = act.get("up"), act.get("down")
        ratio_txt = "-"
        if up is not None and down is not None and (up + down) > 0:
            ratio_txt = f"{up / (up + down) * 100:.0f}%"

        amt_chg = data.get("amount_change_pct")
        amt_chg_txt = "-"
        if amt_chg is not None:
            arrow = "↑" if amt_chg >= 0 else "↓"
            amt_chg_txt = f"{arrow}{abs(amt_chg):.0f}%"

        # FIX(2026-08-12): 成交额为前日兜底值时标注，避免误读为当日量能
        _amt_lag = act.get("amount_date") not in (None, data.get("date"))
        _amt_suffix = "(前日)" if _amt_lag else ""

        if compact:
            return (
                f'<div class="alert" style="background:#f0f5ff;'
                f'border-left:4px solid {meta["color"]}">'
                f'🌡️ 市场情绪 <b style="color:{meta["color"]}">'
                f'{meta["icon"]}{data.get("level", "未知")}</b>({temp_txt}) | '
                f'涨跌 {up if up is not None else "-"}/'
                f'{down if down is not None else "-"} | '
                f'涨停 {act.get("limit_up") if act.get("limit_up") is not None else "-"} | '
                f'两市 {_fmt(act.get("amount"), "亿")}{_amt_suffix}'
                f'{" | " + advice if advice else ""}'
                f'</div>')

        mg_txt = _fmt(mg.get("balance"), "亿")
        if mg.get("change_pct") is not None:
            mg_txt += f"({'+' if mg['change_pct'] >= 0 else ''}{mg['change_pct']}%)"

        degraded_note = ""
        if data.get("degraded"):
            degraded_note = (
                '<div style="font-size:11px;color:#999;margin-top:6px">'
                f'⚠️ 数据源降级: {"；".join(data["degraded"])}'
                f'（缺失成分不计入情绪分，结论可靠性降低）</div>')

        return f"""
<h2>🌡️ 市场情绪与资金面</h2>
<div class="alert" style="background:#f0f5ff;border-left:4px solid {meta['color']}">
    <b style="color:{meta['color']};font-size:14px">{meta['icon']} 情绪{data.get('level', '未知')}（{temp_txt}）</b>
    <span style="margin-left:10px">{advice}</span>
</div>
<table class="metric-table"><tr>
    <td class="metric-cell"><div class="metric-value">{up if up is not None else '-'}<span style="font-size:11px;color:#999"> / {down if down is not None else '-'}</span></div><div class="metric-label">涨/跌家数（上涨占比{ratio_txt}）</div></td>
    <td class="metric-cell"><div class="metric-value">{act.get('limit_up') if act.get('limit_up') is not None else '-'}<span style="font-size:11px;color:#999"> / {act.get('limit_down') if act.get('limit_down') is not None else '-'}</span></div><div class="metric-label">涨停/跌停</div></td>
    <td class="metric-cell"><div class="metric-value">{_fmt(act.get('amount'), '')}</div><div class="metric-label">两市成交额(亿){_amt_suffix} 环比{amt_chg_txt}</div></td>
</tr></table>
<div class="alert alert-info">💹 杠杆资金: 沪市融资余额 {mg_txt}（存量博弈看成交额与换手）</div>
{degraded_note}
"""
    except Exception as e:
        logger.debug(f"[市场脉搏] 渲染失败: {e}")
        return ('<div class="alert alert-info">🌡️ 市场情绪面板: '
                '数据暂不可用，按中性环境对待</div>')
