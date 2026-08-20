"""
CANSLIM选股买点盘中到价提醒模块 V1.2（L1降级标记 + 信号结算登记）
====================================
将选股报告"推荐买入"板块的三档买点持久化，盘中实时比价、到价自动推送。

链路:
  选股报告生成(send_screener_email) → save_buy_alert_levels() 落盘买点
  盘中统一预警循环(scheduler, 每10分钟) → check_buy_point_alerts() 比价推送

V1.1升级(2026-08-11 交易复盘驱动):
  - 大盘环境四档闸门: L0正常/L1走弱(仅池内龙头且减半提示)/
    L2走跌(≥1.5%暂停全部推送)/L3系统性风险(≥2.5%禁止买入)
  - 个股日内暴跌作废: 标的当日跌幅≤-3%即作废其买点并推送作废通知(防接飞刀)
  - 推送卡片升级为7要素执行指令: 建议股数/分批确认条件/止损价/
    单股仓位上限/当日禁止再加仓/提醒有效期
  - 持仓数≥MAX_HOLDINGS时降级为"仅提示不建议买入"
  - 禁加仓冷却清单(buy_block.json): 卖出预警/手工可登记，冷却期内屏蔽买点
  - 单轮限推MAX_PUSH_PER_CYCLE只，超出顺延下轮，防批量诱导下单

V1.2升级(2026-08-11 L1闸门回测驱动):
  - L1走弱不再硬拦截: 非池内龙头标的仍推送，但卡片加"⚠️L1走弱降级"标记，
    建议放弃，坚持执行者仅限首仓（闸门触发时刻敏感，硬切断易错杀）
  - 触发结算登记(buy_signal_history.json): 每次买点触发自动落盘，
    盘后任务settle_signal_history()结算T+1/3/5前瞻收益，
    积累真实样本供闸门策略定期复审（胜率/平均收益）

V1.3修复(2026-08-13 实盘缺陷修复):
  - H1: 作废登记后立即落盘去重记录，杜绝"只有作废无触发"轮次的重复推送
  - H2: 仅推送成功后登记去重；失败记失败计数允许下轮重试，连续≥3次记WARNING
  - 低价补触发: 日内最低价击穿且现价未偏离(≤+1%)时补触发，防轮询漏报
  - 指数获取失败时闸门默认L1降级标记推送(原L0放行过松)
  - JSON原子写(临时文件+os.replace)，损坏读取记WARNING
  - 行情新鲜度: 东财备用源识别疑似停牌(零量且价=昨收)跳过，卡片标注备用源
  - check_buy_point_alerts 新增 skip_codes 参数(与本轮风险预警双轨互斥预留)
  - 作废通知不占用单轮限推配额；signal_history登记expires_at，卡片展示具体过期时刻
  - 卡片补充可用资金快照提示

设计原则:
  - 独立预警类型: 有自己的每日去重记录(buy_point_alert_sent.json)，
    不占用持仓风险预警的alert_cooldown.json冷却字典
  - 同日同标的同档位只推送一次；一轮同时击穿多档时只推最深一档（防轰炸）
  - 非交易时段不触发；行情获取失败/文件缺失/任何异常均静默跳过，
    绝不中断调用方主循环
"""

import os
import sys
import json
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
LEVELS_FILE = os.path.join(_DATA_DIR, "buy_alert_levels.json")   # 买点价位持久化
SENT_FILE = os.path.join(_DATA_DIR, "buy_point_alert_sent.json")  # 当日推送去重记录
BLOCK_FILE = os.path.join(_DATA_DIR, "buy_block.json")            # 禁加仓冷却清单
HISTORY_FILE = os.path.join(_DATA_DIR, "buy_signal_history.json")  # V1.2触发结算登记

# 三档买点: (字段名, 档位名)
TIERS = (
    ("aggressive_buy", "激进"),
    ("moderate_buy", "稳健"),
    ("conservative_buy", "保守"),
)

# V1.1 盘中闸门参数（模块级常量，不在config.py新增配置项）
# 阈值与 intraday_decision.DECISION_CONFIG 的 market_risk_pct/market_crash_pct 口径对齐
GATE_L1_WEAK = -0.5        # 上证/沪深300较差者 ≤-0.5% → L1走弱(买点降级)
GATE_L2_DOWN = -1.0        # V6.2: ≤-1.0% → L2整体走跌(原-1.5%收紧，熊市命中率仅33.2%需更早拦截)
GATE_L3_CRASH = -2.5       # ≤-2.5% → L3系统性风险(禁止买入)
INTRA_DAY_CRASH_PCT = -3.0  # 个股当日跌幅≤-3% → 当日作废其买点(对齐config.REJECT_DROP_PCT)
ALERT_VALID_MINUTES = 15    # 买点提醒有效窗口（分钟），过期作废不得追价
MAX_PUSH_PER_CYCLE = 3      # 单轮最多推送标的数，超出顺延下轮（作废通知不占配额）
LOW_FILL_BUFFER_PCT = 1.0   # 低价补触发容忍度(%): 日内low击穿档位且现价≤档位价×(1+该%)时补触发
MAX_PUSH_RETRY_FAILS = 3    # 同一档位推送连续失败达该次数记WARNING（仍允许继续重试）


def _enabled() -> bool:
    return bool(getattr(config, "BUY_POINT_ALERT_ENABLED", True))


def _load_json(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            logger.warning(f"[买点提醒] JSON结构异常(非dict)，按空处理: {path}")
    except Exception as e:
        if os.path.exists(path):
            logger.warning(f"[买点提醒] JSON读取失败(按空处理，不影响主链路): {path} | {e}")
    return {}


def _save_json(path: str, data: dict):
    """原子写: 先写同目录临时文件再os.replace替换，避免半截文件污染正式数据"""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[买点提醒] JSON落盘失败: {path} | {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _next_trading_day(d: datetime.date) -> datetime.date:
    """FIX: 改用公共交易日历（含节假日/调休），异常时降级到原跳周末逻辑"""
    try:
        from utils.trading_calendar import next_trading_day
        return next_trading_day(d)
    except Exception:
        nd = d + datetime.timedelta(days=1)
        while nd.weekday() >= 5:
            nd += datetime.timedelta(days=1)
        return nd


# ============================================================
# 一、写侧：选股报告生成后持久化买点
# ============================================================

def save_buy_alert_levels(result: dict) -> int:
    """从选股结果中提取推荐买入标的的三档买点并落盘

    仅收录 is_buy_recommend=True 且 is_watch=False（大盘允许买入）的标的。
    无推荐标的时写入空levels（清空昨日遗留），不产生任何监控任务。
    返回写入条数；任何异常返回0且不影响调用方。
    """
    try:
        if not _enabled():
            return 0
        levels = []
        for item in (result or {}).get("stock_pool", []):
            if not item.get("is_buy_recommend") or item.get("is_watch"):
                continue
            try:
                moderate = float(item.get("moderate_buy") or 0)
                if moderate <= 0:
                    continue
                levels.append({
                    "code": str(item.get("code", "")),
                    "name": item.get("name", ""),
                    "sector_group": item.get("sector_group", ""),
                    "factor_score": item.get("factor_score", 0),
                    "aggressive_buy": float(item.get("aggressive_buy") or 0),
                    "moderate_buy": moderate,
                    "conservative_buy": float(item.get("conservative_buy") or 0),
                    "stop_loss": float(item.get("stop_loss") or 0),
                    "first_shares": int(item.get("first_shares") or 0),
                    "first_amount": float(item.get("first_amount") or 0),
                    "base_price": float(item.get("current_price") or 0),
                })
            except (TypeError, ValueError):
                continue

        # 有效日判定: 交易日15:00前生成→当日盘中生效；收盘后/非交易日生成→下一交易日生效
        now = datetime.datetime.now()
        today = now.date()
        # FIX(review): weekday()<5改用公共交易日历(含节假日/调休)，修复调休交易日
        # (如周六补班)买点提醒失效一天的问题；日历异常时降级到原周末判定
        try:
            from utils.trading_calendar import is_trading_day as _is_trading_day
            _today_is_trading = _is_trading_day(today)
        except Exception:
            _today_is_trading = today.weekday() < 5
        if _today_is_trading and now.hour < 15:
            valid_date = today
        else:
            valid_date = _next_trading_day(today)

        _save_json(LEVELS_FILE, {
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "valid_date": valid_date.strftime("%Y-%m-%d"),
            "levels": levels,
        })
        if levels:
            logger.info(f"[买点提醒] 已持久化{len(levels)}只推荐买点 (生效日{valid_date})")
        else:
            logger.info("[买点提醒] 本期无推荐买入标的，监控任务清空")
        return len(levels)
    except Exception as e:
        logger.warning(f"[买点提醒] 买点持久化失败(不影响报告): {e}")
        return 0


def merge_intraday_picks(candidates: list) -> int:
    """V4.4 P1: 盘中发现的强势股合并入买点监控（回踩型买点）

    背景: 选股是09:25一次性决策（基于昨日数据），盘中突发拉升的强势股
    永远得不到买点覆盖。本函数接收异动扫描(screener_picks/breakouts)与
    扫描预热的发现结果，为合格标的追加"回踩型买点"——三档均低于现价，
    只有回踩到位才触发提醒，纪律上不追高；落盘后盘中快路径自动监控。

    去重与限制: levels生效日非当日/大盘闸门L2+不追加；已在levels、已持仓、
    科创/创业/北交所/ETF前缀跳过；每日追加与总数上限均可配。
    返回本次新增条数；异常返回0且不影响调用方。
    """
    try:
        if not _enabled() or not candidates:
            return 0
        if not getattr(config, "INTRADAY_PICK_TO_LEVELS_ENABLED", True):
            return 0
        if not os.path.exists(LEVELS_FILE):
            return 0
        data = _load_json(LEVELS_FILE)
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        if str(data.get("valid_date", "")) != today_str:
            return 0  # 过期文件不混写（09:25选股会重建）

        levels = list(data.get("levels", []) or [])
        held = set(_load_held_shares().keys())
        _max_per_day = int(getattr(config, "INTRADAY_PICK_MAX_PER_DAY", 3))
        _max_levels = int(getattr(config, "INTRADAY_PICK_MAX_LEVELS", 12))
        _min_chg = float(getattr(config, "INTRADAY_PICK_MIN_CHANGE", 3.0))
        _max_chg = float(getattr(config, "INTRADAY_PICK_MAX_CHANGE", 8.5))
        _added_today = sum(1 for lv in levels if lv.get("source") == "盘中增量")

        # 先做零成本去重/资格过滤，剩余有新候选才拉指数闸门（避免每轮无谓调用）
        pending = []
        _existing = {str(lv.get("code", "")).zfill(6) for lv in levels}
        for cand in candidates or []:
            code = str(cand.get("code", "")).zfill(6)
            if len(code) != 6 or not code.startswith(("0", "6")):
                continue  # 仅沪深主板（创业/科创/北交所/ETF由下方前缀再拦一道）
            if code.startswith(("300", "688", "588", "159", "8", "4")):
                continue
            if code in _existing or code in held:
                continue
            if code in {p[0] for p in pending}:
                continue
            try:
                price = float(cand.get("price") or 0)
                chg = float(cand.get("change_pct") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or not (_min_chg <= chg <= _max_chg):
                continue
            try:
                vol_ratio = float(cand.get("vol_ratio") or 0)
            except (TypeError, ValueError):
                vol_ratio = 0.0
            if vol_ratio and vol_ratio < 1.2:
                continue
            pending.append((code, cand, price))
        if not pending:
            return 0

        if _added_today >= _max_per_day or len(levels) >= _max_levels:
            return 0
        gate_level = _market_gate()[0]
        if gate_level >= 2:
            logger.info(f"[盘中增量买点] 大盘闸门L{gate_level}，不追加新标的")
            return 0

        added = 0
        for code, cand, price in pending:
            if _added_today + added >= _max_per_day or len(levels) >= _max_levels:
                break
            _first_shares = max(100, int(20000 // price // 100) * 100)
            levels.append({
                "code": code,
                "name": cand.get("name", ""),
                "sector_group": cand.get("sector", cand.get("sector_group", "")),
                "factor_score": 0,
                "aggressive_buy": round(price * 0.99, 2),
                "moderate_buy": round(price * 0.97, 2),
                "conservative_buy": round(price * 0.95, 2),
                "stop_loss": round(price * 0.90, 2),
                "first_shares": _first_shares,
                "first_amount": 20000.0,
                "base_price": price,
                "source": "盘中增量",
            })
            added += 1

        if added:
            _save_json(LEVELS_FILE, {
                "generated_at": data.get("generated_at", ""),
                "valid_date": today_str,
                "levels": levels,
            })
            _names = [f"{lv.get('name')}({lv.get('code')})"
                      for lv in levels[-added:]]
            logger.info(f"[盘中增量买点] 追加{added}只回踩买点: {', '.join(_names)}")
        return added
    except Exception as e:
        logger.warning(f"[盘中增量买点] 合并异常(不阻断): {e}")
        return 0


# ============================================================
# 一'、V1.1 盘中闸门: 大盘环境/禁加仓冷却/持仓数
# ============================================================


def check_levels_validity() -> dict:
    """V4.4: 买点数据当日有效性体检（供scheduler 09:40静默失效告警）

    背景: levels的valid_date若非当日，盘中比价会静默全跳过且无任何告警，
    用户全天收不到买点提醒而不自知。本函数把该静默态转为显式告警。

    返回: {"ok": bool, "reason": str, "levels_count": int,
           "valid_date": str, "generated_at": str}
    """
    info = {"ok": False, "reason": "", "levels_count": 0,
            "valid_date": "", "generated_at": ""}
    try:
        if not _enabled():
            info["ok"] = True  # 功能关闭不属于异常
            info["reason"] = "买点到价提醒开关关闭"
            return info
        if not os.path.exists(LEVELS_FILE):
            info["reason"] = "buy_alert_levels.json不存在（选股报告未生成过买点）"
            return info
        data = _load_json(LEVELS_FILE)
        info["valid_date"] = str(data.get("valid_date", ""))
        info["generated_at"] = str(data.get("generated_at", ""))
        info["levels_count"] = len(data.get("levels", []) or [])
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        if info["valid_date"] == today_str:
            info["ok"] = True
        elif info["levels_count"] == 0:
            # 无买点监控任务，失效与否不影响推送
            info["ok"] = True
            info["reason"] = "本期无推荐买点，无需盘中监控"
        else:
            info["reason"] = (f"levels生效日为{info['valid_date']}而非今日{today_str}，"
                              f"盘中{info['levels_count']}只买点将全部静默跳过")
    except Exception as e:
        info["reason"] = f"有效性检查异常: {e}"
    return info


def _market_gate() -> tuple:
    """大盘环境四档闸门（上证/沪深300取较差者）

    返回 (level, label, emoji, pct_sh, pct_300):
      L0正常(>-0.5%) 正常执行 | L1走弱 买点降级仅池内龙头
      L2整体走跌(≤-1.5%) 暂停全部推送 | L3系统性风险(≤-2.5%) 禁止买入
    行情异常时降级为L0并记日志，不误杀也不阻断调用方。
    """
    try:
        from data.realtime import fetch_index_realtime
        sh = fetch_index_realtime("000001") or {}
        hs = fetch_index_realtime("000300") or {}
        if not sh and not hs:
            raise RuntimeError("指数行情返回为空")
        if not sh or not hs:
            # FIX(2026-08-14): 单源缺失也降级。原仅双源全空才降级，单源失败时
            # float({}.get("change_pct") or 0)=0，另一源为正即L0放行(fail-open)；
            # 现任一源缺失即按L1走弱降级，用可得值填充展示
            _pct_sh = float(sh.get("change_pct") or 0) if sh else 0.0
            _pct_300 = float(hs.get("change_pct") or 0) if hs else 0.0
            logger.warning(f"[买点提醒] 指数行情单源缺失"
                           f"(上证{'OK' if sh else '缺失'}/沪深300{'OK' if hs else '缺失'})，"
                           f"闸门按L1走弱降级处理")
            return 1, "走弱(指数行情缺失)", "🟡", _pct_sh, _pct_300
        pct_sh = float(sh.get("change_pct") or 0)
        pct_300 = float(hs.get("change_pct") or 0)
    except Exception as e:
        # V1.3: 指数缺失不再默认L0放行，改为L1降级标记推送（卡片横幅提示），
        # 宁可降级提示也不在环境未知时按正常档位诱导买入
        logger.warning(f"[买点提醒] 大盘指数获取失败，闸门按L1走弱降级处理: {e}")
        return 1, "走弱(指数行情缺失)", "🟡", 0.0, 0.0

    worst = min(pct_sh, pct_300)
    if worst <= GATE_L3_CRASH:
        return 3, "系统性风险", "🔴", pct_sh, pct_300
    if worst <= GATE_L2_DOWN:
        return 2, "大盘整体走跌", "🟠", pct_sh, pct_300
    if worst <= GATE_L1_WEAK:
        return 1, "大盘走弱", "🟡", pct_sh, pct_300
    return 0, "正常", "🟢", pct_sh, pct_300


def _load_buy_block(today_str: str) -> set:
    """读取当日仍有效的禁加仓冷却清单，返回被屏蔽的代码集合"""
    data = _load_json(BLOCK_FILE)
    blocked = set()
    for code, info in (data.get("blocked") or {}).items():
        until = str((info or {}).get("until") or "")
        if until and until >= today_str:
            blocked.add(str(code))
    return blocked


def add_buy_block(code: str, name: str = "", reason: str = "", days: int = 1) -> bool:
    """登记禁加仓冷却（供卖出预警/盘后复盘/手工调用）

    days=1 当日屏蔽；days=3 约3个交易日冷却（简单跳周末）。
    任何异常静默失败，不影响调用方。
    """
    try:
        today = datetime.date.today()
        until = today
        for _ in range(max(days, 1) - 1):
            until = _next_trading_day(until)
        data = _load_json(BLOCK_FILE)
        blocked = data.setdefault("blocked", {})
        blocked[str(code)] = {
            "name": name, "reason": reason,
            "added": today.strftime("%Y-%m-%d"),
            "until": until.strftime("%Y-%m-%d"),
        }
        # 顺手清理已过期条目，防文件无限膨胀
        today_str = today.strftime("%Y-%m-%d")
        data["blocked"] = {k: v for k, v in blocked.items()
                           if str((v or {}).get("until") or "") >= today_str}
        _save_json(BLOCK_FILE, data)
        logger.info(f"[买点提醒] 已登记禁加仓冷却: {code} {name} 至{until} ({reason})")
        return True
    except Exception as e:
        logger.warning(f"[买点提醒] 禁加仓冷却登记失败: {e}")
        return False


def _load_held_shares() -> dict:
    """加载当前持仓 {code: shares}，口径与 intraday_decision.load_holdings 一致"""
    try:
        holdings_file = getattr(config, "HOLDINGS_FILE", None)
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not holdings_file or not os.path.exists(holdings_file):
            holdings_file = os.path.join(base_dir, "holdings.json")
        if not os.path.exists(holdings_file):
            return {}
        with open(holdings_file, "r", encoding="utf-8") as f:
            all_holdings = json.load(f)
        return {str(code): info.get("shares", 0)
                for code, info in all_holdings.items()
                if info.get("shares", 0) > 0}
    except Exception:
        return {}


# ============================================================
# 二、读侧：盘中到价检测
# ============================================================

def _in_trading_hours(now: datetime.datetime) -> bool:
    """交易时段门: 09:30-11:30 / 13:00-15:00，周末直接排除"""
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= t <= 11 * 60 + 30) or (13 * 60 <= t <= 15 * 60)


def _quote_is_fresh(quote: dict, today_str: str) -> bool:
    """行情新鲜度校验

    FIX(2026-08-14): 疑似停牌/陈旧报价检测(零量且现价==昨收)提到函数开头
    对所有源统一执行——腾讯源盘中停牌时time仍刷新，原仅备用源分支检查会漏拦。
    腾讯源: time字段(YYYYMMDDHHMMSS)前8位须为当日，
    兜底排除节假日/停牌的陈旧报价误触发。
    东财等备用源(time无日期字段): 通过停牌检测后放行（卡片标注备用源）。
    """
    # 疑似停牌/陈旧报价: 零成交量且现价==昨收（全源统一拦截）
    try:
        volume = float(quote.get("volume", 1) or 0)
        price = float(quote.get("price", 0) or 0)
        prev = float(quote.get("prev_close", 0) or 0)
        if volume == 0 and prev > 0 and abs(price - prev) < 1e-9:
            return False
    except (TypeError, ValueError):
        pass
    qtime = str(quote.get("time", ""))
    if len(qtime) >= 8 and qtime[:8].isdigit():
        return qtime[:8] == today_str.replace("-", "")
    return True


def _max_position_ratio(code: str) -> float:
    """单只仓位上限比例: ETF→20%，池内龙头→15%，其余弹性标的→8%"""
    if str(code).startswith("5") or str(code).startswith("1"):
        return float(getattr(config, "MAX_SINGLE_ETF_RATIO", 0.20))
    pool_info = getattr(config, "STOCK_POOL", {}).get(str(code), {})
    if pool_info.get("类型") == "龙头":
        return float(getattr(config, "LEADER_STOCK_MAX_RATIO", 0.15))
    return float(getattr(config, "FLEXIBLE_STOCK_MAX_RATIO", 0.08))


def _build_push_text(lv: dict, tier_label: str, tier_price: float,
                     price: float, now: datetime.datetime,
                     env_str: str = "", held_shares: int = 0,
                     over_limit: bool = False,
                     l1_degraded: bool = False,
                     low_fill: bool = False,
                     index_missing: bool = False,
                     alt_source: bool = False) -> tuple:
    """构造推送标题与Markdown正文：7要素完整执行指令卡（一屏内看完）

    要素: ①建议买入股数(仅此数量) ②分批安排与二批确认条件 ③止损价
          ④单股仓位上限(股数化) ⑤当日是否允许继续加仓 ⑥提醒有效期 ⑦环境标记
    l1_degraded: L1走弱且非池内龙头 → 卡片头部加降级标记横幅(建议放弃)
    low_fill: 日内低价回补触发 → 触发文案注明
    index_missing: 指数行情缺失按走弱降级 → 卡片横幅注明
    alt_source: 东财等备用行情源 → 触发文案标注
    """
    code, name = lv.get("code", ""), lv.get("name", lv.get("code", ""))
    sector = lv.get("sector_group", "")
    stop = lv.get("stop_loss", 0)
    stop_str = f"{stop:.2f}" if stop > 0 else "-"
    shares = lv.get("first_shares", 0)
    amount = lv.get("first_amount", 0)
    
    # 总仓位约束：若可用现金不足，压缩首仓股数（避免卡片①与④矛盾）
    available_cash = float(getattr(config, "AVAILABLE_CASH", 0) or 0)
    if available_cash > 0 and amount > available_cash:
        shares = int(available_cash / price / 100) * 100
        amount = shares * price
    
    pos_str = f"{shares}股 ≈ {amount / 1e4:.1f}万" if shares > 0 else "见报告"

    tiers_str = " / ".join(
        f"{label}{float(lv.get(field) or 0):.2f}"
        for field, label in TIERS if float(lv.get(field) or 0) > 0
    )

    # 执行参数: 分批/加仓确认/仓位上限（均取config现有参数，缺失时用安全默认值）
    second_ratio = (1 - float(getattr(config, "FIRST_BATCH_RATIO", 0.40))) * 100
    add_profit = float(getattr(config, "MIN_PROFIT_TO_ADD", 0.03)) * 100
    max_ratio = _max_position_ratio(code)
    capital = float(getattr(config, "TOTAL_CAPITAL", 0)
                    or getattr(config, "INITIAL_CAPITAL", 0) or 0)
    if capital > 0 and price > 0:
        # 总仓位约束：取 min(单股上限金额, 可用现金) —— 避免满仓时仍按理论上限下指令
        available_cash = float(getattr(config, "AVAILABLE_CASH", 0) or 0)
        max_by_ratio = capital * max_ratio
        max_by_cash = available_cash if available_cash > 0 else max_by_ratio
        max_amount = min(max_by_ratio, max_by_cash)
        max_shares = int(max_amount / price / 100) * 100
        constraint_note = ""
        if max_by_cash < max_by_ratio and available_cash > 0:
            constraint_note = f"(受总仓位约束，可用现金{available_cash/1e4:.1f}万)"
        cap_str = (f"{max_ratio * 100:.0f}% → 封顶{max_shares}股{constraint_note}"
                   if max_shares > 0 else f"{max_ratio * 100:.0f}%(不足100股，不得新开)")
    else:
        cap_str = f"总资产的{max_ratio * 100:.0f}%"

    # 当日加仓许可: 已持仓需浮盈达标才许加，未持仓首仓后当日禁止再买
    if held_shares > 0:
        add_str = (f"⚠️ 已持{held_shares}股: 加仓需浮盈≥{add_profit:.0f}%"
                   f"或次日回踩买点不破确认")
    else:
        add_str = f"❌ 首仓成交后当日禁止再买入(第二批需浮盈≥{add_profit:.0f}%)"

    max_hold = getattr(config, "MAX_HOLDINGS", 7)
    over_str = (f"⛔ **持仓数已达上限({max_hold}只)，本条仅提示，不建议新开仓**\n\n"
                if over_limit else "")
    degrade_str = ("⚠️ **L1走弱降级**: 大盘走弱且本标的非池内龙头，"
                   "**建议放弃本次买入**；坚持执行仅限首仓，禁止追价/当日补仓\n\n"
                   if l1_degraded else "")
    index_miss_str = ("⚠️ **指数行情缺失，按走弱降级处理**：环境未确认，"
                      "仅限首仓谨慎执行\n\n" if index_missing else "")

    # 可用资金快照提示（同步快照，仅供参考，非下单依据）
    cash_str = ""
    try:
        cash_val = float(getattr(config, "AVAILABLE_CASH", 0) or 0)
        if cash_val > 0:
            cash_str = f"**可用资金**: {cash_val / 1e4:.2f}万（同步快照，仅供参考）\n\n"
    except (TypeError, ValueError):
        pass

    trigger_note = ("（日内低价回补触发）" if low_fill else "")
    source_note = ("（备用行情源）" if alt_source else "")
    expire_at = now + datetime.timedelta(minutes=ALERT_VALID_MINUTES)

    title = f"[操盘密码·买点到价] {name}({code}) {tier_label}买点"
    # V9.2: 标题区分持仓加仓 vs 候选新建仓，提升信息辨识度
    if held_shares > 0:
        title += f" [持仓加仓·已持{held_shares}股]"
    elif over_limit:
        title += " [候选新建仓]"
    
    # V5.0: 钉钉卡片美化 —— 分区布局+分隔线+醒目标题
    # 顶部横幅（L1降级/持仓超限）
    banner = ""
    if over_limit:
        banner += f"### ⛔ 持仓已满({max_hold}只)，仅提示不建议\n\n---\n\n"
    if l1_degraded:
        banner += f"### ⚠️ L1走弱降级，建议放弃本次买入\n\n---\n\n"
    if index_missing:
        banner += f"### ⚠️ 指数行情缺失，按走弱降级处理\n\n---\n\n"
    
    # 主体卡片
    content = (
        f"{banner}"
        f"### 🎯 {name} ({code})\n\n"
        f"**赛道**: {sector} | **评分**: {lv.get('factor_score', '-')} | **环境**: {env_str or '🟢 正常'}\n\n"
        f"---\n\n"
        f"#### 📊 触发详情\n\n"
        f"**现价**: {price:.2f} ≤ **{tier_label}买点**: {tier_price:.2f}{trigger_note}{source_note}\n\n"
        f"**三档买点**: {tiers_str}\n\n"
        f"---\n\n"
        f"#### 🎯 执行指令\n\n"
        f"① **首仓**: 仅买{pos_str}，一次买足\n\n"
        f"② **加仓**: 第二批(约{second_ratio:.0f}%)需浮盈≥{add_profit:.0f}%或次日确认\n\n"
        f"③ **止损**: {stop_str}，跌破无条件执行\n\n"
        f"④ **上限**: {cap_str}\n\n"
        f"⑤ **纪律**: {add_str}\n\n"
    )
    
    # 可用资金（如有）
    if cash_str:
        content += f"---\n\n💰 {cash_str}\n\n"
    
    # 底部有效期
    content += (
        f"---\n\n"
        f"> ⏰ {now.strftime('%H:%M')} 有效期至 {expire_at.strftime('%H:%M')}"
        f"({ALERT_VALID_MINUTES}分钟)，过期作废，禁止追价"
    )
    return title, content


def _push_invalidate_notice(lv: dict, change_pct: float, price: float,
                            now: datetime.datetime, env_str: str):
    """标的日内暴跌时推送买点作废通知（当日仅推一次，由去重记录控制）"""
    code, name = lv.get("code", ""), lv.get("name", lv.get("code", ""))
    title = f"[操盘密码·买点作废] {name}({code}) 日内跌{abs(change_pct):.1f}%"
    content = (
        f"**标的**: {name}({code})\n\n"
        f"**市场环境**: {env_str}\n\n"
        f"**状态**: 现价{price:.2f}，日内跌幅{change_pct:.1f}%"
        f"(触及{INTRA_DAY_CRASH_PCT:.0f}%作废线)\n\n"
        f"**处理**: 该标的今日买点全部作废，不接飞刀，待次日重新评估\n\n"
        f"> ⏰{now.strftime('%H:%M')} 纪律优先: 暴跌日不买，急跌不接"
    )
    return _push_alert(title, content)


def _push_alert(title: str, content: str, alert_meta: dict = None):
    """推送: V5.0分级路由 + V6.0 ACK确认按钮

    V4.4: 钉钉+邮件双发（邮件SMTP延迟5-75秒，对盘中快速决策无意义）
    V5.0: BUY_POINT_ALERT_DINGTALK_ONLY=True时仅钉钉，延迟从~20秒降到<1秒；
          钉钉webhook未配置时自动降级邮件（安全网）；
          DINGTALK_ONLY=False时回退旧双发模式。
    V6.0: ACK开启时，钉钉推送升级为ActionCard(含"✅ 已处理"确认按钮)，
          用户确认后当日同标的同档位买点静默；ActionCard失败回退Markdown。

    alert_meta: 可选，{"code", "name", "tier", "tier_price", "price", ...}
                用于构建ActionCard确认按钮和令牌。
    """
    channel_ok = False
    _dingtalk_only = getattr(config, "BUY_POINT_ALERT_DINGTALK_ONLY", True)

    # ---- 钉钉/企微推送 ----
    try:
        # V6.0: ACK开启 + 有alert_meta → ActionCard(含确认按钮)
        _ack_ok = False
        if alert_meta and getattr(config, "ALERT_ACK_ENABLED", True):
            try:
                from notify.alert_ack import build_action_card_payload
                from notify.wechat_notify import send_dingtalk_action_card
                _confirm_base = getattr(config, "ALERT_ACK_CALLBACK_URL",
                                        "http://192.168.88.101:9876/ack/")
                _now_str = datetime.datetime.now().strftime("%H:%M")
                _today_str2 = datetime.date.today().strftime("%Y-%m-%d")
                # 构造单条预警的alerts列表(复用ACK模块的ActionCard构建)
                _bp_alerts = [{
                    "code": alert_meta.get("code", ""),
                    "name": alert_meta.get("name", ""),
                    "level": "high",
                    "urgency_score": 80,
                    "rule_name": f"买点到价-{alert_meta.get('tier', '')}",
                    "rule_detail": content[:120],
                    "msg": "",
                    "icon": "",
                    "holdings_info": {},
                    "_action_text": f"建议买入{alert_meta.get('tier', '')}买点",
                    "extra_rules": [],
                }]
                _header = f"[股票] 🎯 买点到价提醒 ({alert_meta.get('name', '')})"
                _payload = build_action_card_payload(
                    _bp_alerts, _header, _now_str, _today_str2, _confirm_base)
                _ack_ok = send_dingtalk_action_card(_payload)
                if _ack_ok:
                    channel_ok = True
                    logger.info(f"[买点提醒] ActionCard发送成功(含ACK按钮): "
                                f"{alert_meta.get('code', '')} "
                                f"{alert_meta.get('tier', '')}")
            except Exception as _ack_e:
                logger.debug(f"[买点提醒] ActionCard失败，回退Markdown: {_ack_e}")

        if not _ack_ok:
            # 回退: 普通Markdown格式
            from notify.wechat_notify import send_notification
            res = send_notification(title, content)
            if any(res.values()):
                channel_ok = True
    except Exception as e:
        logger.debug(f"买点提醒钉钉/企微推送异常: {e}")

    # ---- 邮件通道 ----
    if _dingtalk_only:
        # P0钉钉only模式: 仅当钉钉未配置(全部失败)时降级邮件
        if not channel_ok:
            try:
                from notify.email_notify import send_email
                html = (f"<h3>{title}</h3>"
                        f"<pre style='font-family:Consolas,monospace;font-size:13px'>"
                        f"{content.replace('**', '').replace('> ', '')}</pre>")
                ok = send_email(title, html)
                if ok:
                    logger.info(f"[买点提醒] 钉钉失败→邮件降级成功: {title}")
                channel_ok = channel_ok or bool(ok)
            except Exception as e:
                logger.warning(f"[买点提醒] 邮件降级也失败: {e}")
    else:
        # 旧模式: 钉钉+邮件双发（非兜底）
        if getattr(config, "BUY_POINT_ALERT_EMAIL_FALLBACK", True):
            try:
                from notify.email_notify import send_email
                html = (f"<h3>{title}</h3>"
                        f"<pre style='font-family:Consolas,monospace;font-size:13px'>"
                        f"{content.replace('**', '').replace('> ', '')}</pre>")
                ok = send_email(title, html)
                if ok:
                    logger.info(f"[买点提醒] 邮件双发成功: {title}")
                channel_ok = channel_ok or bool(ok)
            except Exception as e:
                logger.warning(f"[买点提醒] 邮件双发失败: {e}")
    return channel_ok


def _sent_entry_labels(entry) -> set:
    """解析当日去重条目已成功推送的标签集合（含"作废"标记）

    兼容旧格式(字符串列表)与新格式(dict: pushed/invalidated/failed)。
    注: 失败计数(failed)不算已推送，允许下轮重试。
    """
    if isinstance(entry, list):
        return {str(x) for x in entry}
    if isinstance(entry, dict):
        labels = {str(x) for x in (entry.get("pushed") or [])}
        if entry.get("invalidated"):
            labels.add("作废")
        return labels
    return set()


def _to_sent_dict(entry) -> dict:
    """归一化去重条目为新格式 dict（旧列表格式自动转换）"""
    labels = _sent_entry_labels(entry)
    failed = {}
    if isinstance(entry, dict):
        for k, v in (entry.get("failed") or {}).items():
            try:
                failed[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
    return {
        "pushed": sorted(l for l in labels if l != "作废"),
        "invalidated": "作废" in labels,
        "failed": failed,
    }


def check_buy_point_alerts(skip_codes=None) -> list:
    """盘中到价检测主入口（由scheduler统一预警循环调用）V1.3

    参数:
      skip_codes: 可选集合，含标的代码；命中标的跳过并记日志
                  （与本轮持仓风险预警双轨互斥，由scheduler传入）

    流程: 时段/开关/文件门 → 大盘环境闸门(L2/L3暂停) →
          禁加仓冷却/持仓超限预检 → 拉实时行情 →
          逐标的暴跌作废过滤/L1降级标记 → 比对三档买点(含日内低价补触发) →
          命中且当日未推送成功过 → 推送7要素执行指令卡，仅成功后登记去重与结算
    返回本轮触发的预警列表（调试用）；任何异常静默返回[]。
    """
    try:
        if not _enabled():
            return []
        now = datetime.datetime.now()
        if not _in_trading_hours(now):
            return []

        data = _load_json(LEVELS_FILE)
        today_str = now.strftime("%Y-%m-%d")
        # FIX(2026-08-14): 逐条 isinstance 防御过滤畸形记录，
        # 原单条非dict会使整轮列表推导抛异常中止（与单标的异常隔离对齐）
        if data.get("valid_date") == today_str:
            levels = [lv for lv in (data.get("levels") or [])
                      if isinstance(lv, dict) and lv.get("code")]
        else:
            levels = []
        if not levels:
            return []

        codes = [lv["code"] for lv in levels]
        if not codes:
            return []

        # ---- 大盘环境闸门: L2整体走跌/L3系统性风险 → 暂停全部买点推送 ----
        gate_level, gate_label, gate_emoji, pct_sh, pct_300 = _market_gate()
        if gate_level >= 2:
            logger.info(f"[买点提醒] {gate_emoji} 环境闸门: 上证{pct_sh:+.2f}%/"
                        f"沪深300{pct_300:+.2f}% → {gate_label}，暂停全部买点推送")
            return []

        # ---- 持仓超限与禁加仓冷却预检 ----
        held_map = _load_held_shares()
        over_limit = len(held_map) >= getattr(config, "MAX_HOLDINGS", 7)
        blocked = _load_buy_block(today_str)

        from data.realtime import fetch_realtime_batch
        quotes = fetch_realtime_batch(codes)
        if not quotes:
            return []  # 行情获取失败，静默跳过

        sent = _load_json(SENT_FILE)
        if sent.get("date") != today_str:
            sent = {"date": today_str, "sent": {}}
        sent_map = sent.setdefault("sent", {})

        # 双轨互斥: 与本轮持仓风险预警重叠的标的跳过（scheduler后续任务传入）
        skip_set = {str(c) for c in (skip_codes or set())}
        index_missing = (gate_level == 1 and "指数行情缺失" in gate_label)

        env_str = f"{gate_emoji} {gate_label}(上证{pct_sh:+.2f}%)"
        pool = getattr(config, "STOCK_POOL", {})
        triggered = []
        pushed_count = 0
        sent_dirty = False
        for lv in levels:
            try:
                if pushed_count >= MAX_PUSH_PER_CYCLE:
                    break  # 单轮限推，剩余标的顺延下一轮
                code = lv.get("code", "")
                if code in skip_set:
                    logger.info(f"[买点提醒] {code} 与本轮风险预警互斥跳过")
                    continue
                if code in blocked:
                    logger.debug(f"[买点提醒] {code} 处于禁加仓冷却期，本轮跳过")
                    continue
                # L1走弱降级标记(V1.2方案B): 非池内龙头仍推送但卡片加降级横幅，
                # 由用户决策是否执行（硬拦截在闸门阈值边界易错杀，样本待积累后复审）
                l1_degraded = (gate_level == 1
                               and pool.get(code, {}).get("类型") != "龙头")
                quote = quotes.get(code)
                if not quote:
                    continue
                price = float(quote.get("price") or 0)
                if price <= 0:
                    continue
                if not _quote_is_fresh(quote, today_str):
                    logger.info(f"[买点提醒] {code} 行情非新鲜"
                                f"(陈旧报价/疑似停牌)，本轮跳过")
                    continue
                alt_source = quote.get("source") == "eastmoney"
                day_low = float(quote.get("low") or 0)  # 当日最低价(低价补触发用)

                # ---- 日内暴跌作废: 跌幅触及阈值 → 当日买点全部作废(防接飞刀) ----
                change_pct = float(quote.get("change_pct") or 0)
                if change_pct <= INTRA_DAY_CRASH_PCT:
                    entry = _to_sent_dict(sent_map.get(code))
                    if not entry["invalidated"]:
                        # FIX(2026-08-14) H2式一致性: 先推送，成功才置 invalidated 并落盘；
                        # 失败记 entry["failed"]["作废"] 计数允许下轮重试（原先登记后推送，
                        # 推送失败则作废通知永久丢失）。一旦 invalidated=True 不再重推。
                        try:
                            _push_ok = bool(_push_invalidate_notice(
                                lv, change_pct, price, now, env_str))
                        except Exception as _pe:
                            logger.debug(f"[买点提醒] {code} 作废通知推送异常: {_pe}")
                            _push_ok = False
                        if _push_ok:
                            entry["invalidated"] = True
                            entry["failed"].pop("作废", None)
                            sent_map[code] = entry
                            _save_json(SENT_FILE, sent)
                            sent_dirty = True
                            logger.info(f"  [买点提醒] ⛔ {code} {lv.get('name', '')} "
                                        f"日内跌{change_pct:.1f}%触及作废线，买点已作废")
                        else:
                            fails = int(entry["failed"].get("作废", 0) or 0) + 1
                            entry["failed"]["作废"] = fails
                            sent_map[code] = entry
                            sent_dirty = True
                            if fails >= MAX_PUSH_RETRY_FAILS:
                                logger.warning(f"[买点提醒] {code} 作废通知推送累计失败"
                                               f"{fails}次，渠道可能不可用，仍将逐轮重试")
                            logger.info(f"  [买点提醒] ⛔ {code} 触及作废线但作废通知推送失败"
                                        f"(累计{fails}次)，未登记作废，下轮重试")
                    continue

                # 有效档位按价格降序（激进通常最高，动态排序不依赖假设）
                tiers = sorted(
                    ((float(lv.get(f) or 0), label) for f, label in TIERS),
                    key=lambda x: -x[0])
                entry = _to_sent_dict(sent_map.get(code))
                if entry["invalidated"]:
                    continue  # 当日已作废(日内暴跌)，不再推送任何档位买点
                pushed = set(entry["pushed"])  # 仅成功推送过的档位才去重，失败可重试
                # 已击穿且当日未推送成功的档位:
                # ① 现价直接击穿; ② 低价补触发: 日内low击穿且现价未偏离档位价>1%
                #    (缓解10分钟轮询间隙击穿又弹回的漏报; low为当日最低价语义)
                low_cap = 1 + LOW_FILL_BUFFER_PCT / 100.0
                reached = []
                for p, label in tiers:
                    if p <= 0 or label in pushed:
                        continue
                    if price <= p:
                        reached.append((p, label, False))
                    elif day_low > 0 and day_low <= p and price <= p * low_cap:
                        reached.append((p, label, True))
                if not reached:
                    continue

                # 一轮击穿多档时只推最深一档（价格最低），成功后全部登记防后续轰炸
                hit_price, hit_label, low_fill = reached[-1]

                title, content = _build_push_text(
                    lv, hit_label, hit_price, price, now,
                    env_str=env_str,
                    held_shares=int(held_map.get(code, 0)),
                    over_limit=over_limit and code not in held_map,
                    l1_degraded=l1_degraded,
                    low_fill=low_fill,
                    index_missing=index_missing,
                    alt_source=alt_source)
                alert = {
                    "type": "buy_point_alert",
                    "code": code,
                    "name": lv.get("name", code),
                    "tier": hit_label,
                    "tier_price": hit_price,
                    "price": price,
                    "market_env": gate_label,
                    "degraded": l1_degraded,
                    "trigger_type": "low_fill" if low_fill else "direct",
                    "expires_at": (now + datetime.timedelta(
                        minutes=ALERT_VALID_MINUTES)).isoformat(timespec="seconds"),
                    "date": today_str,
                    "time": now.strftime("%H:%M"),
                    # V9.2: 区分持仓加仓 vs 候选新建仓
                    "is_holding": code in held_map,
                    "held_shares": int(held_map.get(code, 0)),
                }
                # V6.0: ACK静默检查 —— 用户已确认的同标的同档位买点当日不再推送
                try:
                    from notify.alert_ack import is_silenced as _bp_silenced
                    if _bp_silenced(code, f"买点到价-{hit_label}", "high", 80):
                        logger.info(f"  [买点提醒] ACK静默: {code} {hit_label}档 "
                                    f"(用户已确认，当日不再推送)")
                        continue
                except Exception as _bp_e:
                    logger.debug(f"[买点提醒] ACK静默检查异常(不阻断): {_bp_e}")
                # H2修复: 先推送，仅成功后才登记去重；失败记失败计数允许下轮重试
                alert["pushed"] = _push_alert(title, content, alert_meta=alert)
                triggered.append(alert)
                if alert["pushed"]:
                    entry["pushed"] = sorted(
                        set(entry["pushed"]) | {label for _, label, _ in reached})
                    entry["failed"].pop(hit_label, None)
                    sent_map[code] = entry
                    sent_dirty = True
                    pushed_count += 1
                    _record_signal(alert)  # V1.2: 结算登记(异常不影响推送主链路)
                else:
                    fails = int(entry["failed"].get(hit_label, 0) or 0) + 1
                    entry["failed"][hit_label] = fails
                    sent_map[code] = entry
                    sent_dirty = True
                    if fails >= MAX_PUSH_RETRY_FAILS:
                        logger.warning(f"[买点提醒] {code} {hit_label}档推送累计失败"
                                       f"{fails}次，渠道可能不可用，仍将逐轮重试")
                logger.info(f"  [买点提醒] 🎯 {code} {lv.get('name', '')} "
                            f"现价{price:.2f}≤{hit_label}买点{hit_price:.2f}"
                            f"{('[低价补触发]' if low_fill else '')} "
                            f"环境{gate_label}{'+L1降级' if l1_degraded else ''} "
                            f"推送{'成功' if alert['pushed'] else '失败(下轮重试)'}")
            except Exception as e:
                logger.debug(f"买点提醒单标的检测异常({lv.get('code', '?')}): {e}")
                continue

        if triggered or sent_dirty:
            _save_json(SENT_FILE, sent)
        return triggered
    except Exception as e:
        logger.warning(f"[买点提醒] 检测异常(已静默): {e}")
        return []


def _record_signal(alert: dict):
    """V1.2触发结算登记: 买点触发记录落盘buy_signal_history.json

    同日同标的同档位仅登记一次；盘后由settle_signal_history()结算前瞻收益。
    任何异常静默降级，不影响推送主链路。
    """
    try:
        data = _load_json(HISTORY_FILE)
        signals = data.setdefault("signals", [])
        key = (alert.get("date"), alert.get("code"), alert.get("tier"))
        if any((s.get("date"), s.get("code"), s.get("tier")) == key
               for s in signals):
            return
        signals.append({
            "date": alert.get("date"), "time": alert.get("time"),
            "code": alert.get("code"), "name": alert.get("name"),
            "tier": alert.get("tier"), "tier_price": alert.get("tier_price"),
            "price": alert.get("price"), "market_env": alert.get("market_env"),
            "degraded": bool(alert.get("degraded")),
            "trigger_type": alert.get("trigger_type", "direct"),
            "expires_at": alert.get("expires_at"),
            # V9.2: 持仓标记（用于后续统计持仓加仓 vs 候选新建仓的信号质量差异）
            "is_holding": bool(alert.get("is_holding", False)),
            "held_shares": int(alert.get("held_shares", 0)),
            "r1": None, "r3": None, "r5": None, "settled": False,
        })
        data["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_json(HISTORY_FILE, data)
        logger.debug(f"[买点提醒] 信号已登记结算队列: {alert.get('code')} "
                     f"{alert.get('date')} {alert.get('tier')}")
    except Exception as e:
        logger.debug(f"[买点提醒] 信号登记失败(不影响推送): {e}")


def _fetch_daily_closes_tencent(code: str, count: int = 30) -> list:
    """腾讯日K轻量拉取: 返回最近count个交易日 (date, close) 升序列表

    仅结算用途（网络异常/结构异常均返回[]，由调用方降级到baostock/akshare）。
    """
    try:
        import urllib.request
        c = str(code)
        prefix = "sh" if c.startswith(("5", "6", "9")) else "sz"
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={prefix}{c},day,,,{count},qfq")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
        d = json.loads(raw)
        node = d["data"][f"{prefix}{c}"]
        bars = node.get("qfqday") or node.get("day") or []
        out = []
        for b in bars:
            out.append((str(b[0])[:10], float(b[2])))  # [date, open, close, ...]
        return out
    except Exception:
        return []


def settle_signal_history() -> dict:
    """V1.2盘后结算: 为买点触发记录补算T+1/3/5前瞻收益（交易日收盘价口径）

    由scheduler盘后任务(15:30)每日调用。日线源优先级: 腾讯日K(轻量稳定) →
    data_loader.fetch_stock_daily(baostock/akshare)降级兑底。
    各档独立结算: 未来第N个交易日收盘可得即计入；三档全齐或触发日超12个
    自然日（数据缺失不再重试）标记settled。返回统计dict(样本数/胜率/均收益)，
    供闸门策略定期复审；任何异常静默返回{}。
    """
    try:
        data = _load_json(HISTORY_FILE)
        signals = data.get("signals", [])
        if not signals:
            return {}

        from data.data_loader import fetch_stock_daily
        today = datetime.date.today()
        df_cache = {}
        tx_cache = {}
        changed = False
        _settle_ok = _settle_timeout = 0
        for s in signals:
            if s.get("settled"):
                continue
            code, tdate = s.get("code"), s.get("date")
            if not code or not tdate:
                continue
            try:
                trig_d = datetime.datetime.strptime(tdate, "%Y-%m-%d").date()
                # V9.2 FIX: 超12自然日直接标记过期（不再依赖数据源，防止网络故障导致永久未结算）
                if (today - trig_d).days > 12:
                    s["settled"] = True
                    s["settle_status"] = "expired"
                    changed = True
                    _settle_ok += 1
                    continue
                # ---- 日线源: 腾讯优先，失败降级baostock/akshare ----
                dates = closes = None
                if code not in tx_cache:
                    tx_cache[code] = _fetch_daily_closes_tencent(code, 30)
                tx_bars = tx_cache[code]
                if tx_bars and any(d == tdate for d, _ in tx_bars):
                    dates = [d for d, _ in tx_bars]
                    closes = [c for _, c in tx_bars]
                else:
                    if code not in df_cache:
                        df_cache[code] = fetch_stock_daily(
                            code, start_date=tdate,
                            end_date=today.strftime("%Y-%m-%d"))
                    df = df_cache[code]
                    if df is None or len(df) == 0:
                        _settle_timeout += 1
                        logger.info(f"[买点提醒] {code} {tdate} 数据源暂不可得，等待下次结算")
                        continue
                    dates = ([str(d)[:10] for d in df["date"].tolist()]
                             if "date" in df.columns
                             else [str(d)[:10] for d in df.index.tolist()])
                    closes = df["close"].astype(float).tolist()
                if tdate not in dates:
                    continue  # 触发日收盘尚不可得(停牌/当日未收盘)
                i = dates.index(tdate)
                trig_close = closes[i]
                if trig_close <= 0:
                    continue
                for n, key in ((1, "r1"), (3, "r3"), (5, "r5")):
                    if s.get(key) is None and i + n < len(closes):
                        s[key] = round((closes[i + n] / trig_close - 1) * 100, 2)
                        changed = True
                done = all(s.get(k) is not None for k in ("r1", "r3", "r5"))
                if done:
                    s["settled"] = True
                    s["settle_status"] = "complete"
                    changed = True
                    _settle_ok += 1
            except Exception as e:
                logger.warning(f"[买点提醒] 单标的结算异常({code}): {e}")
                continue

        if changed:
            data["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _save_json(HISTORY_FILE, data)

        # 汇总统计（各档仅统计已有数据的样本）
        stats = {"total": len(signals)}
        for n, key in ((1, "r1"), (3, "r3"), (5, "r5")):
            vals = [s[key] for s in signals if s.get(key) is not None]
            if vals:
                stats[key] = {
                    "n": len(vals),
                    "win_rate": round(sum(1 for v in vals if v > 0)
                                      / len(vals) * 100, 1),
                    "avg_ret": round(sum(vals) / len(vals), 2),
                }
        summary = " | ".join(
            f"T+{n}: {stats[k]['n']}条 胜率{stats[k]['win_rate']}% "
            f"均{stats[k]['avg_ret']:+.2f}%"
            for n, k in ((1, "r1"), (3, "r3"), (5, "r5")) if k in stats)
        logger.info(f"[买点提醒] 信号结算完成: 累计{stats['total']}条"
                    f"{(' | ' + summary) if summary else '(前瞻数据暂不可得)'}"
                    f" | 本次成功{_settle_ok}条 数据暂缺{_settle_timeout}条")
        return stats
    except Exception as e:
        logger.warning(f"[买点提醒] 信号结算失败(静默跳过): {e}")
        return {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 50)
    print("  买点提醒模块 - 手动检测")
    print("=" * 50)
    data = _load_json(LEVELS_FILE)
    print(f"买点文件: {LEVELS_FILE}")
    print(f"生效日: {data.get('valid_date')} | 标的数: {len(data.get('levels', []))}")
    for lv in data.get("levels", []):
        print(f"  {lv['code']} {lv.get('name', '')} | 激进{lv['aggressive_buy']} "
              f"/ 稳健{lv['moderate_buy']} / 保守{lv['conservative_buy']} | 止损{lv['stop_loss']}")
    hits = check_buy_point_alerts()
    print(f"\n本轮触发: {len(hits)}条")
    for a in hits:
        print(f"  {a['code']} {a['name']} {a['tier']}买点{a['tier_price']} 现价{a['price']}")
