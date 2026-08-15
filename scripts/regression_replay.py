# -*- coding: utf-8 -*-
"""
regression_replay.py — 回归回放核验脚本（任务#3）
==================================================
只读、不联网、不发邮件。对交易系统数据资产做五维回归核验，
结果输出到 output/regression_report.json（并在 stdout 打印摘要）。

  R1 数据可用性矩阵   stock_db.db 日线覆盖 / last_update 停滞 / 分钟线缺失提示
  R2 买点信号结算核验 buy_signal_history.json 已结算记录用日线独立重算 T+1/3/5
  R3 成本对账         trades_today.json + slippage_history.json 分布 vs 回测 SLIPPAGE
  R4 报告一致性       holdings_snapshots 快照 vs output/holdings_analysis_*.html 清单
  R5 调度时序         scheduler_*.log 关键行扫描（发送成功/失败/超时 计数）

健壮性约定: 任何子项失败记入结果 errors 段继续执行，绝不中断；
           所有 DB/文件读取均 try/except 包裹。
"""
import os
import re
import sys
import json
import glob
import sqlite3
import datetime
import statistics

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "trading_system", "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
RESULT_PATH = os.path.join(OUTPUT_DIR, "regression_report.json")

TODAY = datetime.date.today()


def _log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        print(msg.encode('utf-8', 'replace').decode('utf-8', 'replace'), flush=True)


# ============================================================
# R1 数据可用性矩阵
# ============================================================

def r1_data_availability(errors):
    res = {"db_path": os.path.join(DATA_DIR, "stock_db.db")}
    conn = None
    try:
        conn = sqlite3.connect(res["db_path"])
        cur = conn.cursor()

        # daily_kline
        row = cur.execute(
            "SELECT COUNT(*), COUNT(DISTINCT code), MIN(date), MAX(date) FROM daily_kline"
        ).fetchone()
        res["daily_kline"] = {
            "rows": row[0], "codes": row[1],
            "date_min": row[2], "date_max": row[3],
        }
        per_code = cur.execute(
            "SELECT code, COUNT(*), MIN(date), MAX(date) FROM daily_kline "
            "GROUP BY code ORDER BY code"
        ).fetchall()
        res["daily_kline"]["per_code"] = [
            {"code": c, "rows": n, "first": f, "last": l} for c, n, f, l in per_code
        ]

        # last_update 停滞检查（基准取全库最大日期，避免周末/假期误判）
        try:
            tables = {r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "last_update" in tables:
                rows = cur.execute("SELECT code, last_date FROM last_update").fetchall()
                ref = res["daily_kline"]["date_max"]
                ref_dt = datetime.datetime.strptime(ref, "%Y-%m-%d").date()
                stale = []
                for code, last_date in rows:
                    try:
                        ld = datetime.datetime.strptime(str(last_date), "%Y-%m-%d").date()
                        gap = (ref_dt - ld).days
                        if gap > 30:
                            stale.append({"code": code, "last_date": last_date, "gap_days": gap})
                    except Exception:
                        stale.append({"code": code, "last_date": last_date, "gap_days": None,
                                      "note": "日期解析失败"})
                res["last_update"] = {
                    "total": len(rows), "reference_date": ref,
                    "stale_over_30d": stale, "stale_count": len(stale),
                }
            else:
                res["last_update"] = {"exists": False}
        except Exception as e:
            errors.append(f"R1 last_update检查失败: {e}")

        # minute_kline
        try:
            mcnt = cur.execute("SELECT COUNT(*) FROM minute_kline").fetchone()[0]
            res["minute_kline"] = {"rows": mcnt}
            if mcnt == 0:
                res["minute_kline"]["note"] = (
                    "分钟线表为空：环境闸门/分钟级回测类验证暂不可行，"
                    "需先接入分钟线数据源")
        except Exception as e:
            res["minute_kline"] = {"error": str(e)}
            errors.append(f"R1 minute_kline统计失败: {e}")
    except Exception as e:
        errors.append(f"R1 stock_db.db读取失败: {e}")
        res["fatal"] = str(e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return res


# ============================================================
# R2 买点信号结算核验
# ============================================================

def r2_signal_settlement(errors):
    res = {"tolerance_pp": 0.5}
    hist_path = os.path.join(DATA_DIR, "buy_signal_history.json")
    conn = None
    try:
        with open(hist_path, "r", encoding="utf-8") as f:
            hist = json.load(f)
        signals = hist.get("signals", []) if isinstance(hist, dict) else hist
        res["total_signals"] = len(signals)

        settled = [s for s in signals
                   if s.get("settled") or s.get("r5") is not None]
        res["settled_count"] = len(settled)
        if not settled:
            res["note"] = "无已结算样本，无法核验（如实记录）"
            return res

        conn = sqlite3.connect(os.path.join(DATA_DIR, "stock_db.db"))
        cur = conn.cursor()
        detail, n_ok, n_bad, n_skip = [], 0, 0, 0
        for s in settled:
            code, sig_date = s.get("code"), s.get("date")
            base_price = s.get("tier_price") or s.get("price")
            try:
                rows = cur.execute(
                    "SELECT date, close FROM daily_kline WHERE code=? AND date>=? "
                    "ORDER BY date LIMIT 10", (code, sig_date)).fetchall()
                if len(rows) < 6 or not base_price:
                    n_skip += 1
                    detail.append({"code": code, "date": sig_date,
                                   "status": "skip", "reason": "日线样本不足或无基准价"})
                    continue
                # rows[0] 应为信号日当日（若当日有K线），否则首个>=信号日的交易日
                closes = [r[1] for r in rows]
                recalc = {}
                for hor, key in ((1, "r1"), (3, "r3"), (5, "r5")):
                    rec = s.get(key)
                    if rec is None or hor >= len(closes):
                        continue
                    calc = (closes[hor] / base_price - 1.0) * 100.0
                    recalc[key] = {"recorded": round(rec, 4),
                                   "recalculated": round(calc, 4),
                                   "diff_pp": round(abs(calc - rec), 4),
                                   "match": abs(calc - rec) <= 0.5}
                all_match = all(v["match"] for v in recalc.values()) if recalc else False
                status = "match" if all_match else "mismatch"
                if all_match:
                    n_ok += 1
                else:
                    n_bad += 1
                detail.append({"code": code, "date": sig_date, "status": status,
                               "horizons": recalc})
            except Exception as e:
                n_skip += 1
                detail.append({"code": code, "date": sig_date,
                               "status": "skip", "reason": str(e)})
        res.update({"matched": n_ok, "mismatched": n_bad, "skipped": n_skip,
                    "detail": detail,
                    "caliber": "基准价=tier_price(到价提醒档位价)，T+n收益按信号日后第n个交易日收盘重算"})
    except FileNotFoundError:
        res["note"] = "buy_signal_history.json 不存在"
        errors.append("R2 buy_signal_history.json不存在")
    except Exception as e:
        errors.append(f"R2 核验失败: {e}")
        res["fatal"] = str(e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return res


# ============================================================
# R3 成本对账
# ============================================================

def r3_cost_reconciliation(errors):
    res = {}
    # 实盘成交样本
    trades_path = os.path.join(BASE_DIR, "trades_today.json")
    try:
        with open(trades_path, "r", encoding="utf-8") as f:
            tt = json.load(f)
        trades = tt.get("trades", []) if isinstance(tt, dict) else []
        res["trades_today"] = {
            "exists": True, "date": tt.get("date") if isinstance(tt, dict) else None,
            "trade_count": len(trades),
            "note": "仅单日样本，统计意义有限",
        }
    except FileNotFoundError:
        res["trades_today"] = {"exists": False}
    except Exception as e:
        res["trades_today"] = {"exists": True, "error": str(e)}
        errors.append(f"R3 trades_today.json解析失败: {e}")

    # 滑点历史分布
    slip_path = os.path.join(DATA_DIR, "slippage_history.json")
    try:
        with open(slip_path, "r", encoding="utf-8") as f:
            slips = json.load(f)
        pcts = [float(x.get("slippage_pct", 0.0)) for x in slips
                if isinstance(x, dict) and x.get("slippage_pct") is not None]
        real_trades = [x for x in slips
                       if isinstance(x, dict) and x.get("has_real_price")]
        res["slippage_history"] = {
            "records": len(slips),
            "with_real_price": len(real_trades),
        }
        if pcts:
            pcts_sorted = sorted(pcts)
            p90_idx = max(0, int(round(0.9 * (len(pcts_sorted) - 1))))
            res["slippage_history"].update({
                "mean_pct": round(statistics.mean(pcts), 4),
                "median_pct": round(statistics.median(pcts), 4),
                "p90_pct": round(pcts_sorted[p90_idx], 4),
            })
        else:
            res["slippage_history"]["note"] = "无有效滑点样本"
    except FileNotFoundError:
        res["slippage_history"] = {"exists": False}
        errors.append("R3 slippage_history.json不存在")
    except Exception as e:
        res["slippage_history"] = {"error": str(e)}
        errors.append(f"R3 slippage_history.json解析失败: {e}")

    # 回测成本参数（源码级提取，避免import回测脚本的模块级副作用）
    try:
        bt_path = os.path.join(BASE_DIR, "trading_system", "run_canslim_backtest.py")
        with open(bt_path, "r", encoding="utf-8") as f:
            src = f.read()
        bt_cost = {}
        for name in ("SLIPPAGE", "COMMISSION_RATE", "STAMP_TAX"):
            m = re.search(rf"^{name}\s*=\s*([0-9.]+)", src, re.MULTILINE)
            if m:
                bt_cost[name] = float(m.group(1))
        res["backtest_cost_params"] = bt_cost

        slip = res.get("slippage_history", {})
        if bt_cost.get("SLIPPAGE") is not None and "mean_pct" in slip:
            diff = abs(slip["mean_pct"] / 100.0 - bt_cost["SLIPPAGE"])
            res["cost_diff"] = {
                "backtest_slippage": bt_cost["SLIPPAGE"],
                "real_mean_slippage": round(slip["mean_pct"] / 100.0, 6),
                "abs_diff": round(diff, 6),
                "note": "实盘滑点均值与回测假设的差异（绝对值，小数口径）",
            }
    except Exception as e:
        errors.append(f"R3 回测成本参数提取失败: {e}")
    return res


# ============================================================
# R4 报告一致性
# ============================================================

def r4_report_consistency(errors):
    res = {}
    snap_dir = os.path.join(DATA_DIR, "holdings_snapshots")
    snaps = {}
    try:
        for fn in sorted(glob.glob(os.path.join(snap_dir, "*.json"))):
            try:
                with open(fn, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict):
                    continue
                held = {c: v for c, v in d.items()
                        if isinstance(v, dict) and int(v.get("shares", 0) or 0) > 0}
                total_pnl = sum(
                    (float(v.get("current_price", 0) or 0) -
                     float(v.get("buy_price", 0) or 0)) * int(v.get("shares", 0) or 0)
                    for v in held.values())
                m = re.search(r"(\d{8})", os.path.basename(fn))
                snaps[os.path.basename(fn)] = {
                    "snapshot_date": m.group(1) if m else None,
                    "total_codes": len(d), "held_codes": len(held),
                    "held_code_list": sorted(held.keys()),
                    "total_pnl_held": round(total_pnl, 2),
                }
            except Exception as e:
                errors.append(f"R4 快照解析失败 {fn}: {e}")
        res["snapshots"] = snaps

        # 两日快照间持仓代码 diff（shares>0 口径）
        keys = sorted(snaps.keys())
        if len(keys) >= 2:
            a, b = snaps[keys[-2]], snaps[keys[-1]]
            ca, cb = set(a["held_code_list"]), set(b["held_code_list"])
            res["snapshot_diff"] = {
                "from": keys[-2], "to": keys[-1],
                "added": sorted(cb - ca), "removed": sorted(ca - cb),
                "unchanged": sorted(ca & cb),
            }
    except FileNotFoundError:
        res["snapshots"] = {}
        errors.append("R4 holdings_snapshots目录不存在")
    except Exception as e:
        errors.append(f"R4 快照读取失败: {e}")

    # 与 holdings_analysis_*.html 归档做文件名级清单核对（不解析HTML数值）
    # 归档目录实际位于 trading_system/output（config.PROJECT_ROOT/output），
    # 兼容扫描根目录 output/
    try:
        archive_dirs = [os.path.join(BASE_DIR, "trading_system", "output"), OUTPUT_DIR]
        htmls = sorted({os.path.basename(p)
                        for d in archive_dirs if os.path.isdir(d)
                        for p in glob.glob(os.path.join(d, "holdings_analysis_*.html"))})
        res["html_archives"] = htmls
        snap_dates = {v["snapshot_date"] for v in snaps.values() if v["snapshot_date"]}
        html_dates = set()
        for h in htmls:
            m = re.search(r"(\d{8})", h)
            if m:
                html_dates.add(m.group(1))
        res["cross_check"] = {
            "snapshot_dates": sorted(snap_dates),
            "html_dates": sorted(html_dates),
            "snapshot_without_html": sorted(snap_dates - html_dates),
            "html_without_snapshot": sorted(html_dates - snap_dates),
        }
    except Exception as e:
        errors.append(f"R4 HTML归档清单核对失败: {e}")
    return res


# ============================================================
# R5 调度时序
# ============================================================

def r5_scheduler_timeline(errors):
    res = {"logs": []}
    log_dir = os.path.join(BASE_DIR, "trading_system", "logs")
    keywords = ("发送成功", "发送失败", "超时")
    try:
        files = sorted(glob.glob(os.path.join(log_dir, "scheduler_*.log")))
        for fp in files:
            fn = os.path.basename(fp)
            m = re.search(r"(\d{8})", fn)
            entry = {"file": fn, "date": m.group(1) if m else None}
            try:
                counts = {kw: 0 for kw in keywords}
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        for kw in keywords:
                            if kw in line:
                                counts[kw] += 1
                entry.update(counts)
            except Exception as e:
                entry["error"] = str(e)
                errors.append(f"R5 日志扫描失败 {fn}: {e}")
            res["logs"].append(entry)
        res["log_count"] = len(files)
        if files:
            dates = [re.search(r"(\d{8})", os.path.basename(f)).group(1)
                     for f in files if re.search(r"(\d{8})", os.path.basename(f))]
            if dates:
                res["date_range"] = [min(dates), max(dates)]
    except Exception as e:
        errors.append(f"R5 日志目录读取失败: {e}")
    return res


# ============================================================
# 主流程
# ============================================================

def main():
    errors = []
    result = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "read-only / offline / no-email",
        "errors": errors,
    }

    _log("=== regression_replay 开始（只读/离线/不发邮件） ===")
    for name, fn in (("R1_data_availability", r1_data_availability),
                     ("R2_signal_settlement", r2_signal_settlement),
                     ("R3_cost_reconciliation", r3_cost_reconciliation),
                     ("R4_report_consistency", r4_report_consistency),
                     ("R5_scheduler_timeline", r5_scheduler_timeline)):
        _log(f"[{name}] 执行中...")
        try:
            result[name] = fn(errors)
            _log(f"[{name}] 完成")
        except Exception as e:  # 双保险：任何子项异常不中断
            errors.append(f"{name} 未预期异常: {e}")
            result[name] = {"fatal": str(e)}
            _log(f"[{name}] 异常已记录: {e}")

    result["data_gaps"] = [
        "成交样本仅 trades_today.json 单日，无法评估多日成交质量",
        "持仓快照仅 2 份（holdings_snapshots），diff 统计意义有限",
        "无退市股历史数据，退市黑名单机制无法回放验证",
        "minute_kline 分钟线为空，环境闸门分钟级回测不可行",
        "调度日志起始 2026-07-20，更早时序无从核验",
    ]

    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(RESULT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        _log(f"\n结果已写入: {RESULT_PATH}")
    except Exception as e:
        _log(f"结果写入失败: {e}")
        return 1

    # ---- 摘要 ----
    _log("\n=== 关键结论摘要 ===")
    r1 = result.get("R1_data_availability", {})
    dk = r1.get("daily_kline", {})
    _log(f"R1: 日线 {dk.get('rows')} 行 / {dk.get('codes')} 标的, "
         f"{dk.get('date_min')} ~ {dk.get('date_max')}; "
         f"last_update停滞>30天: {r1.get('last_update', {}).get('stale_count', 'N/A')} 只; "
         f"minute_kline: {r1.get('minute_kline', {}).get('rows', 'N/A')} 行")
    r2 = result.get("R2_signal_settlement", {})
    _log(f"R2: 信号总数 {r2.get('total_signals', 0)}, 已结算 {r2.get('settled_count', 0)}, "
         f"一致 {r2.get('matched', 0)} / 不一致 {r2.get('mismatched', 0)} / 跳过 {r2.get('skipped', 0)}")
    r3 = result.get("R3_cost_reconciliation", {})
    sh = r3.get("slippage_history", {})
    _log(f"R3: 滑点样本 {sh.get('records', 0)} 条(真实价 {sh.get('with_real_price', 0)}), "
         f"均值 {sh.get('mean_pct', 'N/A')}% / 中位 {sh.get('median_pct', 'N/A')}% / "
         f"P90 {sh.get('p90_pct', 'N/A')}%; 回测SLIPPAGE="
         f"{r3.get('backtest_cost_params', {}).get('SLIPPAGE', 'N/A')}")
    r4 = result.get("R4_report_consistency", {})
    _log(f"R4: 快照 {len(r4.get('snapshots', {}))} 份, HTML归档 {len(r4.get('html_archives', []))} 份; "
         f"diff: {json.dumps(r4.get('snapshot_diff', {}), ensure_ascii=False)}")
    r5 = result.get("R5_scheduler_timeline", {})
    _log(f"R5: 调度日志 {r5.get('log_count', 0)} 份, 范围 {r5.get('date_range', 'N/A')}")
    _log(f"errors: {len(errors)} 条" + (f" -> {errors}" if errors else ""))
    _log("=== regression_replay 结束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
