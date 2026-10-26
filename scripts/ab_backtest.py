# -*- coding: utf-8 -*-
"""
ab_backtest.py — 隔离 A/B 回测归因工具（P0-2 路线图）
=====================================================
封装"固定种子(SEED=42)/固定池/固定区间 + 单开关 + 对比"的 A/B 流程，
输出 OOS 夏普（均值/pooled/中位数）、年化、回撤、overfit_ratio、有效窗占比的差异表。

设计纪律（遵守 SKILL 铁律 / 路线图"每改动独立 A/B 归因"）：
  - 复用 master_backtest 的数据加载与回测宇宙构建、复用 walk_forward 引擎，不新建第二套引擎；
  - baseline 用当前 config 原样运行；variant 仅施加 --set 指定的开关（进程内、可回退）；
  - 无 --set 时 baseline == variant → 差异应 ≈ 0（自证可复现）；
  - 只读、不联网、不发邮件、不写持仓；结果落 output/ab_report_<ts>.json。

用法:
    # 自证可复现（无开关，差异应为 0）
    python scripts/ab_backtest.py --quick

    # 对比 P0-3 正则化前后
    python scripts/ab_backtest.py --quick --set WF_CONFIG.regularization_lambda=0.15

    # 对比 P1-2 RANGE 调优开关（置空 REGIME_PLAYBOOK_TUNING 需代码级，见下）
    python scripts/ab_backtest.py --set WF_CONFIG.min_train_trades=0 --set WF_CONFIG.train_param_cap_steps=0
"""
import sys
import os
import json
import copy
import time
import argparse
import datetime

# ---- 路径：scripts/ 的上一级是仓库根，源码在 trading_system/ ----
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TS_DIR = os.path.join(ROOT_DIR, "trading_system")
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
for p in (TS_DIR, SCRIPTS_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np  # noqa: E402
import config  # noqa: E402

SEED = 42
OUTPUT_DIR = getattr(config, "OUTPUT_DIR", os.path.join(ROOT_DIR, "output"))

# 参与 A/B 对比的 Walk-Forward 汇总指标
METRIC_KEYS = [
    "oos_sharpe", "oos_sharpe_pooled", "oos_sharpe_median",
    "valid_ratio", "overfit_ratio", "oos_return", "oos_win_rate",
    "oos_max_dd", "stability_score", "num_valid_windows", "num_windows",
]


def _log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        print(str(msg).encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def _coerce(v: str):
    """把命令行字符串值转为合适的 Python 类型（bool/int/float/str/JSON）。"""
    s = str(v).strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if s.startswith(("{", "[")):
        try:
            return json.loads(s)
        except Exception:
            return s
    return s


def apply_overrides(overrides: dict) -> dict:
    """把 {"WF_CONFIG.regularization_lambda": 0.15} 施加到 config（进程内），返回旧值备份。

    仅支持"config 属性(字典) + 嵌套键路径"形式；顶层标量属性亦支持。
    返回 backup: {attr_name: 原属性对象深拷贝}，供 run 后恢复。
    """
    backup = {}
    for path, raw in overrides.items():
        parts = path.split(".")
        attr = parts[0]
        if not hasattr(config, attr):
            _log(f"[warn] config 无属性 {attr}，跳过 {path}")
            continue
        if attr not in backup:
            backup[attr] = copy.deepcopy(getattr(config, attr))
        obj = getattr(config, attr)
        val = _coerce(raw)
        if len(parts) == 1:
            setattr(config, attr, val)
            continue
        cur = obj
        for k in parts[1:-1]:
            if not isinstance(cur, dict) or k not in cur:
                cur = None
                break
            cur = cur[k]
        if isinstance(cur, dict):
            cur[parts[-1]] = val
        else:
            _log(f"[warn] 无法定位嵌套路径 {path}，跳过")
    return backup


def restore_overrides(backup: dict):
    """恢复 apply_overrides 修改前的 config 属性。"""
    for attr, val in backup.items():
        try:
            setattr(config, attr, val)
        except Exception:
            pass


def run_wf(data_dict: dict, codes: list, quick: bool, max_windows: int = None) -> dict:
    """固定种子运行 Walk-Forward，返回汇总指标 dict。"""
    from backtest.walk_forward import WalkForwardAnalyzer
    np.random.seed(SEED)
    wf = WalkForwardAnalyzer(max_windows=(max_windows or (3 if quick else None)))
    res = wf.run(data_dict, codes)
    if not isinstance(res, dict) or "error" in res:
        return {"_error": (res or {}).get("error", "WF 运行失败") if isinstance(res, dict) else "WF 运行失败"}
    return {k: res.get(k) for k in METRIC_KEYS}


def _fmt(v):
    if isinstance(v, (int, float)):
        return f"{v:.4f}"
    return "N/A"


def build_diff(base: dict, var: dict) -> list:
    rows = []
    for k in METRIC_KEYS:
        b, v = base.get(k), var.get(k)
        d = None
        if isinstance(b, (int, float)) and isinstance(v, (int, float)):
            d = round(v - b, 6)
        rows.append({"metric": k, "baseline": b, "variant": v, "delta": d})
    return rows


def print_table(rows: list, overrides: dict):
    _log("\n" + "=" * 68)
    _log("  A/B 回测归因（固定 SEED=42 / 同池 / 同区间）")
    _log("  开关(variant): " + (json.dumps(overrides, ensure_ascii=False) if overrides else "无（自证可复现）"))
    _log("=" * 68)
    _log(f"  {'指标':<22}{'baseline':>14}{'variant':>14}{'Δ':>12}")
    _log("  " + "-" * 62)
    for r in rows:
        _log(f"  {r['metric']:<22}{_fmt(r['baseline']):>14}{_fmt(r['variant']):>14}{_fmt(r['delta']):>12}")
    _log("=" * 68)


def main():
    parser = argparse.ArgumentParser(description="隔离 A/B 回测归因工具")
    parser.add_argument("--quick", action="store_true", help="小样本快速验证")
    parser.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                        help="variant 施加的开关，如 WF_CONFIG.regularization_lambda=0.15（可多次）")
    parser.add_argument("--windows", type=int, default=0, help="限制 WF 最大窗口数（加速）")
    parser.add_argument("--out", type=str, default="", help="结果 JSON 输出路径")
    args = parser.parse_args()

    overrides = {}
    for kv in args.set:
        if "=" not in kv:
            _log(f"[warn] 忽略非法 --set（缺 =）: {kv}")
            continue
        k, v = kv.split("=", 1)
        overrides[k.strip()] = v.strip()

    import master_backtest as mb
    # 回测卫生：中和实盘熔断状态（与 master_backtest 同口径）
    try:
        mb._neutralize_live_breaker()
    except Exception:
        pass

    _log("=== ab_backtest 开始（只读/离线/不发邮件） ===")
    data_dict = mb.load_data_dict(quick=args.quick)
    if not data_dict:
        _log("[FAIL] 数据加载失败，无法进行 A/B。")
        return 1

    # 回测宇宙 = STOCK_POOL + BACKTEST_EXTRA_POOL + BACKTEST_DELISTED_POOL（与 master_backtest 同口径）
    pool_codes = list(getattr(config, "STOCK_POOL", {}).keys())
    for extra in ("BACKTEST_EXTRA_POOL", "BACKTEST_DELISTED_POOL"):
        for c in getattr(config, extra, {}):
            if c not in pool_codes:
                pool_codes.append(c)
    codes = [c for c in pool_codes if c in data_dict and c != "000300"]
    if args.quick:
        codes = codes[:8]
    _log(f"[OK] 回测宇宙 {len(codes)} 只，quick={args.quick}")

    # ---- baseline：当前 config 原样 ----
    t0 = time.time()
    _log("\n>>> 运行 baseline（当前 config）...")
    base = run_wf(data_dict, codes, args.quick, args.windows or None)
    _log(f"    baseline 完成 {time.time() - t0:.1f}s")

    # ---- variant：施加开关 ----
    backup = {}
    t1 = time.time()
    if overrides:
        _log(">>> 施加开关，运行 variant...")
        backup = apply_overrides(overrides)
        try:
            var = run_wf(data_dict, codes, args.quick, args.windows or None)
        finally:
            restore_overrides(backup)
        _log(f"    variant 完成 {time.time() - t1:.1f}s（config 已恢复）")
    else:
        _log(">>> 无开关，variant = baseline 再跑一次（自证可复现）...")
        var = run_wf(data_dict, codes, args.quick, args.windows or None)
        _log(f"    variant 完成 {time.time() - t1:.1f}s")

    rows = build_diff(base, var)
    print_table(rows, overrides)

    # 自证：无开关时核心 OOS 指标差异应为 0
    if not overrides:
        max_abs = max([abs(r["delta"]) for r in rows
                       if isinstance(r["delta"], (int, float)) and r["metric"] != "num_windows"] or [0.0])
        _log(f"\n  [自证可复现] 无开关最大绝对差异 = {max_abs:.6f} "
             f"({'✓ 通过' if max_abs < 1e-9 else '⚠ 存在非确定性，请排查随机源'})")

    result = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "quick": args.quick,
        "n_codes": len(codes),
        "overrides": overrides,
        "seed": SEED,
        "baseline": base,
        "variant": var,
        "diff": rows,
    }
    out_path = args.out or os.path.join(
        OUTPUT_DIR, f"ab_report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        _log(f"\n结果已写入: {out_path}")
    except Exception as e:
        _log(f"[warn] 结果写入失败: {e}")
    _log("=== ab_backtest 结束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
