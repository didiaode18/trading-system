"""
预警发送台账模块（批1-B公共模块）
==================================
记录每次成功发送的预警，回填T+N后验收益，按规则/级别聚合统计，
用于评估预警系统命中率与事后校准。

落盘文件: output/alert_stats.json（config.OUTPUT_DIR）
记录字段: {"ts","date","code","level","rule_name","urgency_score","price_at_send"}
后验回填: fwd_return 字段（T+horizon_days 涨跌幅）

窗口约束: 仅保留最近90天记录（按 ts 过滤截断）
安全约束: 全部函数 try/except 降级，绝不抛异常影响主流程
"""

import os
import json
import logging
import datetime

logger = logging.getLogger(__name__)

# 默认台账路径: config.OUTPUT_DIR/alert_stats.json
try:
    import config as _config
except ImportError:  # pragma: no cover - 包内导入兼容
    try:
        from trading_system import config as _config
    except Exception:
        _config = None

if _config is not None and hasattr(_config, "OUTPUT_DIR"):
    DEFAULT_LEDGER_PATH = os.path.join(_config.OUTPUT_DIR, "alert_stats.json")
else:
    DEFAULT_LEDGER_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'output', 'alert_stats.json'
    )

# 台账保留窗口（天）
LEDGER_WINDOW_DAYS = 90


def _resolve_path(ledger_path: str = None) -> str:
    return ledger_path or DEFAULT_LEDGER_PATH


def _load_ledger(path: str) -> list:
    """读取台账记录列表，任何异常返回空列表"""
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("records"), list):
                return data["records"]
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning(f"预警台账读取失败({path}): {e}")
    return []


def _save_ledger(records: list, path: str) -> None:
    """写入台账（90天窗口截断），任何异常仅记日志"""
    try:
        # 按ts过滤最近90天
        cutoff = datetime.datetime.now() - datetime.timedelta(days=LEDGER_WINDOW_DAYS)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        kept = []
        for r in records:
            if not isinstance(r, dict):
                continue
            ts = r.get("ts")
            if not ts or str(ts) >= cutoff_str:
                kept.append(r)

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({"records": kept,
                       "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"预警台账写入失败({path}): {e}")


def record_sent(alerts: list, ledger_path: str = None) -> None:
    """
    记录一批发送成功的预警到台账

    参数:
        alerts: 发送成功的预警dict列表，期望含 code/level/rule_name/urgency_score/price/close/date 等字段
        ledger_path: 台账路径（None=默认 output/alert_stats.json）

    记录字段: {"ts","date","code","level","rule_name","urgency_score","price_at_send"}
    price_at_send 取 alert.get("price") 或 get("close")，缺失则 None
    绝不抛异常。
    """
    try:
        if not alerts:
            return
        path = _resolve_path(ledger_path)
        records = _load_ledger(path)

        now = datetime.datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        today = now.strftime("%Y-%m-%d")

        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            price = alert.get("price")
            if price is None:
                price = alert.get("close")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None

            urgency = alert.get("urgency_score")
            try:
                urgency = float(urgency) if urgency is not None else None
            except (TypeError, ValueError):
                urgency = None

            date_str = alert.get("date") or today
            records.append({
                "ts": ts,
                "date": str(date_str)[:10],
                "code": alert.get("code", ""),
                "level": alert.get("level", ""),
                "rule_name": alert.get("rule_name", alert.get("rule", "")),
                "urgency_score": urgency,
                "price_at_send": price,
            })

        _save_ledger(records, path)
    except Exception as e:
        logger.warning(f"预警台账记录失败: {e}")


def settle_returns(load_close_fn, horizon_days: int = 5, ledger_path: str = None) -> int:
    """
    回填T+horizon后验收益

    对 price_at_send 非空且未回填 fwd_return 的记录，用 load_close_fn(code) 获取
    带日期的收盘价序列，按记录的 date（预警日）切片取基准价：基准=序列中
    首个 >= 预警日的交易日收盘（无精确匹配取最近后一个交易日；基准找不到
    则跳过），切片后取第 horizon_days 个收盘价计算涨跌幅写入 fwd_return。
    切片后序列不足 horizon_days+1 视为未到期，跳过。

    参数:
        load_close_fn: 注入函数 load_close_fn(code) -> [(date_str, close), ...]
                       升序带日期元组列表（date_str为YYYY-MM-DD），拿不到返回None
        horizon_days: 后验窗口（默认5）
        ledger_path: 台账路径（None=默认）

    返回: 本次回填条数（异常返回0，绝不抛异常）
    """
    try:
        if not callable(load_close_fn):
            return 0
        path = _resolve_path(ledger_path)
        records = _load_ledger(path)
        if not records:
            return 0

        settled_count = 0
        changed = False
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if rec.get("fwd_return") is not None:
                continue
            price = rec.get("price_at_send")
            code = rec.get("code")
            if price is None or not code:
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            try:
                series = load_close_fn(code)
            except Exception:
                series = None
            if series is None:
                continue

            # 契约: [(date_str, close), ...] 升序带日期元组列表
            try:
                pairs = [(str(d)[:10], float(c)) for d, c in series]
            except Exception:
                continue
            if not pairs:
                continue
            pairs.sort(key=lambda p: p[0])

            # 按预警日切片: 首个 >= 记录date 的交易日为基准
            # （无精确匹配取最近后一个交易日；基准找不到则跳过）
            rec_date = str(rec.get("date") or "")[:10]
            if rec_date:
                base_idx = next((i for i, (d, _c) in enumerate(pairs)
                                 if d >= rec_date), None)
                if base_idx is None:
                    continue
            else:
                base_idx = 0  # 无预警日时退化为全序列首日
            sliced = pairs[base_idx:]

            # 需要 horizon_days+1 个收盘价（首日=基准日）才算到期
            if len(sliced) < horizon_days + 1:
                continue

            base_close = sliced[0][1]
            target = sliced[horizon_days][1]
            if base_close <= 0 or target <= 0:
                continue
            rec["fwd_return"] = round((target - base_close) / base_close, 6)
            settled_count += 1
            changed = True

        if changed:
            _save_ledger(records, path)
        return settled_count
    except Exception as e:
        logger.warning(f"预警台账回填失败: {e}")
        return 0


def aggregate_stats(ledger_path: str = None) -> dict:
    """
    按rule/level聚合台账统计

    返回:
        {
            "total": int,                      # 记录总数
            "settled": int,                    # 已回填条数
            "by_rule": {rule_name: {"count": n, "avg_fwd_return": x}},
            "by_level": {level: {"count": n, "avg_fwd_return": x}},
        }
    avg_fwd_return 仅统计已回填记录；异常时返回空结构，绝不抛异常。
    """
    empty = {"total": 0, "settled": 0, "by_rule": {}, "by_level": {}}
    try:
        path = _resolve_path(ledger_path)
        records = _load_ledger(path)
        if not records:
            return empty

        by_rule: dict = {}
        by_level: dict = {}

        def _agg(bucket: dict, key: str, rec: dict):
            key = key or "(未知)"
            item = bucket.setdefault(key, {"count": 0, "_sum": 0.0, "_n": 0})
            item["count"] += 1
            fr = rec.get("fwd_return")
            if fr is not None:
                try:
                    item["_sum"] += float(fr)
                    item["_n"] += 1
                except (TypeError, ValueError):
                    pass

        for rec in records:
            if not isinstance(rec, dict):
                continue
            _agg(by_rule, rec.get("rule_name"), rec)
            _agg(by_level, rec.get("level"), rec)

        def _finalize(bucket: dict) -> dict:
            out = {}
            for k, item in bucket.items():
                n = item["_n"]
                out[k] = {
                    "count": item["count"],
                    "avg_fwd_return": round(item["_sum"] / n, 6) if n > 0 else None,
                }
            return out

        settled = sum(1 for r in records
                      if isinstance(r, dict) and r.get("fwd_return") is not None)
        return {
            "total": len(records),
            "settled": settled,
            "by_rule": _finalize(by_rule),
            "by_level": _finalize(by_level),
        }
    except Exception as e:
        logger.warning(f"预警台账聚合失败: {e}")
        return empty
