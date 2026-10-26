# -*- coding: utf-8 -*-
"""
register_delisted_pool.py — 退市/摘牌标的池扫描与登记助手（P1-3 路线图）
=======================================================================
消除幸存者偏差的第一步：从**本地 DB 真实数据**中识别"数据在回测区间内提前终止"
的标的（退市/被吸收合并/长期停牌的强候选），生成可直接粘贴进
`config.BACKTEST_DELISTED_POOL` 的登记片段。

铁律（遵守"不编造数据"）：
  - 只依据 DB 中 daily_kline 的真实 last_date 与全库最大日期的间隔做证据判定，
    绝不臆断退市原因；名称/赛道取自 config.get_stock_info（仓库权威来源），
    缺失则以代码占位，并在片段中标注"名称待人工确认"；
  - 本脚本**只读、只打印**，不自动改写 config.py；是否登记由人工复核后决定；
  - 间隔阈值默认 365 天（远大于普通停牌），低于阈值的近期停只列为"观察"不登记。

用法:
    python scripts/register_delisted_pool.py                 # 扫描并打印候选与片段
    python scripts/register_delisted_pool.py --min-gap 730   # 更严阈值（2年）
    python scripts/register_delisted_pool.py --json out.json # 同时导出结构化结果
"""
import sys
import os
import json
import sqlite3
import argparse
import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TS_DIR = os.path.join(ROOT_DIR, "trading_system")
for p in (TS_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import config  # noqa: E402


def _log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        print(str(msg).encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def live_universe_codes() -> set:
    """当前存活回测宇宙（STOCK_POOL + BACKTEST_EXTRA_POOL），退市池应与之互斥。"""
    codes = set(getattr(config, "STOCK_POOL", {}).keys())
    codes |= set(getattr(config, "BACKTEST_EXTRA_POOL", {}).keys())
    return codes


def scan_db(min_gap_days: int = 365, min_rows: int = 60) -> dict:
    """扫描 DB，按 last_date 与全库最大日期的间隔识别退市候选（真实数据）。"""
    db_path = getattr(config, "DB_PATH", None)
    if not db_path or not os.path.exists(db_path):
        return {"error": f"数据库不存在: {db_path}"}
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        gmax = cur.execute("SELECT MAX(date) FROM daily_kline").fetchone()[0]
        rows = cur.execute(
            "SELECT code, COUNT(*), MIN(date), MAX(date), "
            "SUM(CASE WHEN volume>0 THEN 1 ELSE 0 END) "
            "FROM daily_kline GROUP BY code"
        ).fetchall()
    finally:
        conn.close()
    if not gmax or not rows:
        return {"error": "daily_kline 无数据"}
    gm = datetime.date.fromisoformat(str(gmax)[:10])
    live = live_universe_codes()

    candidates, watch, insufficient = [], [], []
    for row in rows:
        code, n, first, last = row[0], row[1], row[2], row[3]
        n_tradable = int(row[4]) if len(row) > 4 and row[4] is not None else int(n)
        if code in live:
            continue  # 已在存活宇宙，非退市候选
        try:
            gap = (gm - datetime.date.fromisoformat(str(last)[:10])).days
        except Exception:
            continue
        info = {}
        try:
            info = config.get_stock_info(code) or {}
        except Exception:
            info = {}
        name = info.get("名称")
        name_confirmed = bool(name and name != code)
        entry = {
            "code": code,
            "name": name if name_confirmed else code,
            "name_confirmed": name_confirmed,
            "sector": info.get("赛道", info.get("行业", "已退市")),
            "type": info.get("类型", "退市"),
            "rows": int(n),
            "tradable_rows": n_tradable,   # volume>0 的可交易日数（load_data_dict 实际可用口径）
            "first_date": str(first)[:10],
            "last_date": str(last)[:10],
            "gap_days": int(gap),
        }
        # 可加载性：load_data_dict 要求 volume>0 过滤后 len>100，否则无法进回测
        if n_tradable <= min_rows:
            if gap >= min_gap_days:
                insufficient.append(entry)  # 强退市候选但数据不足，如实单列
            continue
        if gap >= min_gap_days:
            candidates.append(entry)
        elif gap >= 90:
            watch.append(entry)  # 近期停只，退市状态未确认，仅观察不登记
    candidates.sort(key=lambda x: -x["gap_days"])
    watch.sort(key=lambda x: -x["gap_days"])
    insufficient.sort(key=lambda x: -x["gap_days"])
    return {
        "global_max_date": str(gmax),
        "total_codes": len(rows),
        "live_universe": len(live),
        "min_gap_days": min_gap_days,
        "candidates": candidates,
        "watch": watch,
        "insufficient": insufficient,
    }


def emit_snippet(candidates: list) -> str:
    """生成可粘贴进 config.BACKTEST_DELISTED_POOL 的登记片段。"""
    if not candidates:
        return "BACKTEST_DELISTED_POOL = {\n    # 未发现满足阈值的退市候选（DB 中无提前终止标的）\n}"
    lines = ["BACKTEST_DELISTED_POOL = {"]
    lines.append("    # 由 scripts/register_delisted_pool.py 基于 DB 真实 last_date 扫描生成；")
    lines.append("    # 证据=gap_days(距全库最大交易日的间隔)。名称如标注'待确认'请人工复核后再登记。")
    for c in candidates:
        nm = c["name"] if c["name_confirmed"] else f'{c["code"]}(名称待确认)'
        lines.append(
            f'    "{c["code"]}": {{"名称": "{nm}", "赛道": "{c["sector"]}", '
            f'"类型": "退市", "delisted": True, '
            f'"evidence_last_date": "{c["last_date"]}", "evidence_gap_days": {c["gap_days"]} }},')
    lines.append("}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="退市/摘牌标的池扫描与登记助手")
    ap.add_argument("--min-gap", type=int, default=365, help="退市候选最小间隔天数（默认365）")
    ap.add_argument("--min-rows", type=int, default=100, help="最少可交易日数（volume>0，默认100，对齐 load_data_dict）")
    ap.add_argument("--json", type=str, default="", help="导出结构化结果 JSON 路径")
    args = ap.parse_args()

    _log("=== register_delisted_pool 扫描（只读/离线） ===")
    res = scan_db(min_gap_days=args.min_gap, min_rows=args.min_rows)
    if "error" in res:
        _log(f"[FAIL] {res['error']}")
        return 1

    _log(f"全库最大交易日: {res['global_max_date']}｜标的总数: {res['total_codes']}｜"
         f"存活宇宙: {res['live_universe']}")
    cands = res["candidates"]
    watch = res["watch"]
    _log(f"\n退市强候选（gap≥{args.min_gap}天，不在存活宇宙）: {len(cands)} 只")
    for c in cands:
        flag = "" if c["name_confirmed"] else " [名称待确认]"
        _log(f"  {c['code']} {c['name']}{flag} | {c['sector']} | "
             f"{c['first_date']}~{c['last_date']} | gap={c['gap_days']}d | rows={c['rows']}")
    _log(f"\n近期停只观察（90≤gap<{args.min_gap}天，退市状态未确认，不登记）: {len(watch)} 只")
    for c in watch[:10]:
        _log(f"  {c['code']} {c['name']} | last={c['last_date']} | gap={c['gap_days']}d")
    insuff = res.get("insufficient", [])
    _log(f"\n强退市候选但可交易数据不足（tradable≤{args.min_rows}天，无法进回测，不登记）: {len(insuff)} 只")
    for c in insuff[:10]:
        _log(f"  {c['code']} {c['name']} | last={c['last_date']} | gap={c['gap_days']}d | tradable={c['tradable_rows']}")

    _log("\n=== 可粘贴进 config.BACKTEST_DELISTED_POOL 的片段 ===")
    _log(emit_snippet(cands))

    if args.json:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
            _log(f"\n结构化结果已写入: {args.json}")
        except Exception as e:
            _log(f"[warn] JSON 导出失败: {e}")
    _log("\n[提示] 本脚本不自动改写 config.py；请人工复核候选后手动登记，再重跑 master_backtest。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
