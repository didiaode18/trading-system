# -*- coding: utf-8 -*-
"""
最全历史回测流水线 · 统一编排器
================================
一键串联 13 个 Stage 与七道准入门，输出总报告与结果存档。

用法:
    python scripts/master_backtest.py                 # 全量流水线
    python scripts/master_backtest.py --quick         # 小样本快速验证(<1分钟)
    python scripts/master_backtest.py --stages 0,1,2  # 只跑指定 Stage
    python scripts/master_backtest.py --baseline <run_id>  # 与基线对比

说明:
    - 已存在的回测组件(Stage 1/2/3/6/7/8)直接接线复用；
    - 新增模块(Stage 0/4/5高原/9/10/11/12/13)若尚未实现，
      自动标记 skipped 并继续，不阻断流水线；
    - 具体实现规格见 .qoder/skills/full-history-backtest/SKILL.md。
"""
import sys
import os
import json
import time
import argparse
import datetime
import traceback
import subprocess

# ---- 路径：scripts/ 的上一级是仓库根，源码在 trading_system/ ----
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TS_DIR = os.path.join(ROOT_DIR, "trading_system")
sys.path.insert(0, TS_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
# 仓库根入路：部分现有模块/策略用 `import trading_system.xxx` 绝对包路径，
# 仅把 trading_system/ 入路会导致 No module named 'trading_system'。
sys.path.insert(0, ROOT_DIR)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import config  # noqa: E402

OUTPUT_DIR = getattr(config, "OUTPUT_DIR", os.path.join(ROOT_DIR, "output"))
ARCHIVE_PATH = os.path.join(OUTPUT_DIR, "backtest_runs.jsonl")
SEED = 42


# ============================================================
# 数据加载（复用现有 SQLite 口径）
# ============================================================
def load_data_dict(quick=False):
    """从 SQLite 加载全部日K，返回 {code: DataFrame} 与交易日列表。"""
    import sqlite3
    import pandas as pd
    db_path = getattr(config, "DB_PATH", None)
    if not db_path or not os.path.exists(db_path):
        print(f"[FAIL] 数据库不存在: {db_path}，请先运行数据下载。")
        return {}
    conn = sqlite3.connect(db_path)
    q = ("SELECT code, date, open, close, high, low, volume, amount "
         "FROM daily_kline ORDER BY code, date")
    df_all = pd.read_sql(q, conn)
    conn.close()
    data_dict = {}
    for code, grp in df_all.groupby("code"):
        df = grp.copy()
        for c in ["open", "close", "high", "low", "volume", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        if len(df) > 100:
            data_dict[code] = df
    if quick:
        # quick：优先保留 STOCK_POOL 标的（保证与 main 的 codes 交集非空），
        # 再补足到 12 只；并缩短区间。否则 DB 前 12 只(000001..)与股票池(600989..)
        # 无交集，会导致 codes 为空、Stage4/5/9/10/11 全部空跑。
        pool = list(getattr(config, "STOCK_POOL", {}).keys())
        keep = [c for c in pool if c in data_dict][:12]
        for c in data_dict.keys():
            if len(keep) >= 12:
                break
            if c not in keep:
                keep.append(c)
        if "000300" in data_dict and "000300" not in keep:
            keep.append("000300")
        data_dict = {k: data_dict[k].tail(400) for k in keep}
    print(f"[OK] 加载 {len(data_dict)} 个标的")
    return data_dict


# ============================================================
# Stage 实现（每个返回 dict，至少含 status: ok/skipped/fail）
# ============================================================
def stage0_data_audit(ctx):
    """数据质量审计（新增 backtest/data_audit.py）。"""
    try:
        from backtest.data_audit import audit_dataset
        return audit_dataset(ctx["data_dict"], ctx.get("codes"))
    except ImportError:
        return {"status": "skipped", "reason": "backtest/data_audit.py 待实现（见 SKILL.md Stage 0）"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage1_single(ctx):
    """单标的回测，复用 backtest_real.backtest_stock_v5。"""
    try:
        from backtest_real import backtest_stock_v5
        per_stock, all_trades = {}, []
        codes = ctx["codes"]
        for code in codes:
            df = ctx["data_dict"].get(code)
            if df is None or len(df) < 60:
                continue
            raw = config.get_stock_info(code) or {}
            # v5 需 info["行业"]，而 get_stock_info 返回"赛道" → 映射
            info = {"名称": raw.get("名称", code), "类型": raw.get("类型", "龙头"),
                    "行业": raw.get("行业", raw.get("赛道", "其他"))}
            trades = backtest_stock_v5(df.copy(), code, info)
            per_stock[code] = len(trades)
            all_trades.extend(trades)
        # Phase 2: 逐股交易统计——诊断哪些标的产生交易、策略覆盖是否充分
        active = {c: n for c, n in per_stock.items() if n > 0}
        inactive = [c for c, n in per_stock.items() if n == 0]
        # P0-2(路线图): 主动成交口径净值摘要——与封面被动等权篮子并列，杜绝口径混淆。
        active_summary = {}
        try:
            from backtest import master_common
            active_summary = master_common.summarize_active_strategy(all_trades)
        except Exception as _e:
            active_summary = {"status": "fail", "error": str(_e)}
        return {"status": "ok", "n_stocks": len(per_stock),
                "n_trades": len(all_trades), "trades": all_trades,
                "per_stock": per_stock,
                "active_stocks": len(active),
                "inactive_stocks": len(inactive),
                "active_detail": active,
                "inactive_codes": inactive,
                "active_strategy": active_summary}
    except Exception as e:
        return {"status": "fail", "error": str(e), "tb": traceback.format_exc()}


def stage2_equal_weight(ctx):
    """等权组合回测，复用 backtest/engine.py 的 BacktestEngineV2。

    口径（对齐 SKILL：默认 10 只等权）：默认策略 default_strategy 每日对全历史
    重算指标，耗时 O(天数×标的数×历史长度)；为控制在全量 30min 预算内，
    限定 ≤10 只标的与回测窗口（quick 更短），窗口限定已在报告附录声明。
    """
    try:
        from backtest.engine import BacktestEngineV2
        dd = ctx["data_dict"]
        pool = [c for c in ctx.get("codes", []) if c in dd]
        if not pool:
            pool = [c for c in dd.keys() if c != "000300"]
        codes2 = pool[:10]
        tail_n = 200 if ctx["quick"] else 500
        sub = {c: dd[c].tail(tail_n) for c in codes2}
        engine = BacktestEngineV2(initial_capital=getattr(config, "TOTAL_CAPITAL", 730000))
        report = engine.run(sub, auto_monte_carlo=False)
        out = {"status": "ok" if "error" not in report else "fail",
               "n_stocks": len(sub), "window_days": tail_n,
               "report": _jsonable(report)}
        if "error" in report:
            out["error"] = report["error"]
        return out
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage3_full_pool(ctx):
    """全量股票池回测（进程内有界复用 run_full_backtest 引擎逻辑）。

    口径（对齐 SKILL：复用而非重写、控 30min 预算、无副作用）：
      - 不再起子进程跑 run_full_backtest.run()（全池 178 只×3年×MC 达小时级，
        且 run() 步骤7 含 send_email 邮件副作用）；
      - 改为进程内 import run_full_backtest，复用其 load_all_data /
        precompute_indicators / run_engine_backtest（同一 BacktestEngineV2 +
        CostConfig + 趋势跟踪V2.0 事件驱动策略）；
      - 限定采样标的数(优先 STOCK_POOL)与回测窗口 window_days，关闭蒙特卡洛省时；
      - 采样与窗口口径在 coverage 中如实声明，并在报告附录标注（不做“全池”夸大）。
    """
    if ctx["quick"]:
        return {"status": "skipped",
                "reason": "quick 模式跳过全池引擎回测（耗时过长），仅全量运行执行"}
    cfg = getattr(config, "MASTER_BACKTEST_CONFIG", {})
    s3cfg = cfg.get("stage3", {}) or {}
    max_codes = int(s3cfg.get("max_codes", 10))
    window_days = int(s3cfg.get("window_days", 200))
    try:
        import run_full_backtest as rfb
        all_data = rfb.load_all_data()
        n_total = len(all_data)
        if n_total == 0:
            return {"status": "fail", "error": "run_full_backtest.load_all_data 返回空"}
        # 采样：优先回测宇宙（STOCK_POOL + BACKTEST_EXTRA_POOL），再按 DB 顺序补足
        pool_codes = list(getattr(config, "STOCK_POOL", {}).keys())
        pool_codes += [c for c in getattr(config, "BACKTEST_EXTRA_POOL", {}).keys()
                       if c not in pool_codes]
        pool = [c for c in pool_codes if c in all_data]
        sampled = pool[:max_codes]
        for c in all_data.keys():
            if len(sampled) >= max_codes:
                break
            if c not in sampled:
                sampled.append(c)
        sub = {c: all_data[c] for c in sampled}
        # 限定窗口：取采样标的公共交易日尾部 window_days 根，临时覆盖 rfb 全局 START/END
        orig_start, orig_end = rfb.START_DATE, rfb.END_DATE
        try:
            date_sets = [set(sub[c]["date"].astype(str).str[:10]) for c in sub]
            common = sorted(set.intersection(*date_sets)) if date_sets else []
            win = common[-window_days:] if len(common) > window_days else common
            if win:
                rfb.START_DATE, rfb.END_DATE = win[0], win[-1]
            pre = rfb.precompute_indicators(sub)
            report = rfb.run_engine_backtest(sub, pre, auto_monte_carlo=False)
        finally:
            rfb.START_DATE, rfb.END_DATE = orig_start, orig_end
        ok = bool(report) and "error" not in report
        out = {
            "status": "ok" if ok else "fail",
            "mode": "in-process-bounded",
            "coverage": {
                "n_sampled": len(sub), "n_total": n_total, "window_days": window_days,
                "start_date": report.get("start_date") if report else None,
                "end_date": report.get("end_date") if report else None,
            },
            "total_return": report.get("total_return") if report else None,
            "sharpe_ratio": report.get("sharpe_ratio") if report else None,
            "total_trades": report.get("total_trades") if report else None,
            "elapsed_sec": report.get("elapsed_sec") if report else None,
        }
        if report and "error" in report:
            out["error"] = report["error"]
        return out
    except Exception as e:
        return {"status": "fail", "error": str(e), "tb": traceback.format_exc()}


def stage4_engine_crosscheck(ctx):
    """双引擎交叉验证（新增）。"""
    try:
        from backtest.engine_crosscheck import cross_validate
        return cross_validate(ctx["data_dict"], ctx["codes"])
    except ImportError:
        return {"status": "skipped", "reason": "backtest/engine_crosscheck.py 待实现（Stage 4）"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage5_param_grid(ctx):
    """参数网格 + 高原检测（复用 backtest_optimizer，高原判定新增）。"""
    try:
        from backtest.param_plateau import run_grid_with_plateau
        return run_grid_with_plateau(ctx["data_dict"], ctx["codes"], quick=ctx["quick"])
    except ImportError:
        return {"status": "skipped", "reason": "backtest/param_plateau.py 待实现（Stage 5）"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage6_walk_forward(ctx):
    """Walk-Forward，复用 walk_forward.py。"""
    try:
        from backtest.walk_forward import WalkForwardAnalyzer
        wf = WalkForwardAnalyzer(max_windows=3 if ctx["quick"] else None)
        result = wf.run(ctx["data_dict"], ctx["codes"])
        return {"status": "ok" if "error" not in result else "fail",
                "result": _jsonable(result)}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage7_monte_carlo(ctx):
    """蒙特卡洛 + 历史情景 + 日度 block bootstrap，复用 monte_carlo.py / block_bootstrap.py。

    口径: v5 逐笔 net_profit 为百分比×100（如 3.5 表示 3.5%）→ 归一化为 pnl_pct=小数。
    block bootstrap 用等权买入持有日收益序列，与逐笔随机化交叉印证。
    """
    out = {"status": "ok"}
    trades = ctx.get("stage1", {}).get("trades", [])
    capital = getattr(config, "TOTAL_CAPITAL", 730000)
    if len(trades) >= 5:
        try:
            from backtest.monte_carlo import MonteCarloStressTest, historical_stress_test
            norm = [{"pnl_pct": float(t.get("net_profit", 0)) / 100.0,
                     "hold_days": t.get("hold_days", 5)} for t in trades]
            mc = MonteCarloStressTest(n_simulations=200 if ctx["quick"] else 1000, seed=SEED)
            out["monte_carlo"] = _jsonable(mc.run(norm, capital))
            out["historical"] = _jsonable(historical_stress_test(norm, capital))
        except Exception as e:
            out["monte_carlo"] = {"error": str(e)}
    else:
        out["reason"] = "交易样本不足 5 笔，跳过逐笔蒙特卡洛"
    # 日度 block bootstrap（独立于逐笔样本量，用等权日收益）
    try:
        from backtest.block_bootstrap import block_bootstrap_daily
        from backtest.master_common import equal_weight_daily_returns
        daily = equal_weight_daily_returns(ctx["data_dict"], ctx["codes"])
        if len(daily) >= 30:
            out["block_bootstrap"] = _jsonable(block_bootstrap_daily(
                daily, block_size=5, n_paths=500 if ctx["quick"] else 1000, seed=SEED))
    except Exception as e:
        out["block_bootstrap"] = {"error": str(e)}
    if "monte_carlo" not in out and "block_bootstrap" not in out:
        return {"status": "skipped", "reason": out.get("reason", "无可用样本")}
    return out


def stage8_dsr(ctx):
    """Deflated Sharpe，复用 deflated_sharpe.py；PBO 可选。

    以 Stage5 真实 trial 数为多重检验次数，observed_sharpe 取网格最优夏普（缺失
    时回退等权组合日收益年化夏普）。传入真实日收益序列以计算 skew/kurt，提升
    标准误估计精度（deflated_sharpe.py 已加 sr_std 保护，cap 到 10.0）。
    """
    grid = ctx.get("stage5", {})
    sharpes = grid.get("all_sharpes", []) if grid.get("status") == "ok" else []
    try:
        from backtest.deflated_sharpe import DeflatedSharpe
        from backtest import master_common
        daily = master_common.equal_weight_daily_returns(ctx["data_dict"], ctx["codes"])
        trec = int(len(daily)) if daily is not None and len(daily) > 0 else 252
        grid_best = grid.get("best_sharpe") if grid.get("status") == "ok" else None
        if grid_best is None:
            grid_best = master_common.summarize_daily_returns(daily).get("sharpe", 0.0)
        grid_best = float(grid_best)
        gates_cfg = getattr(config, "MASTER_BACKTEST_CONFIG", {}).get("gates", {})
        # P2-2(路线图): n_trials 真实化——除 Stage5 网格 trial 数外，可取实验台账累计值作下限。
        #   台账文件 output/experiment_ledger.json {"cumulative_trials": int} 存在则取 max；
        #   不编造，缺省仍用真实网格 trial 数。
        n_trials = max(len(sharpes), 1)
        try:
            _led = os.path.join(OUTPUT_DIR, "experiment_ledger.json")
            if os.path.exists(_led):
                with open(_led, "r", encoding="utf-8") as _lf:
                    _cum = int(json.load(_lf).get("cumulative_trials", 0) or 0)
                if _cum > n_trials:
                    n_trials = _cum
        except Exception:
            pass
        _n_trials_floor = gates_cfg.get("G3_dsr_n_trials_floor", 0)
        if isinstance(_n_trials_floor, int) and _n_trials_floor > n_trials:
            n_trials = _n_trials_floor
        # P0-2(路线图): observed_sharpe 优先用 Stage6 Walk-Forward 样本外夏普（pooled 优先），
        #   而非 Stage5 样本内网格最优。旧口径用样本内 2.03 使 DSR=0.9988 误导性 PASS；
        #   OOS 为负/None 时如实反映（不为通过而回退到样本内）。
        use_oos = gates_cfg.get("G3_dsr_observed_from_oos", False)
        wf = ctx.get("stage6", {}).get("result", {}) if ctx.get("stage6", {}).get("status") == "ok" else {}
        oos_used = wf.get("oos_sharpe_pooled")
        if not isinstance(oos_used, (int, float)):
            oos_used = wf.get("oos_sharpe")
        observed, obs_basis = grid_best, "in_sample_grid"
        if use_oos and isinstance(oos_used, (int, float)):
            observed, obs_basis = float(oos_used), "oos_walkforward"
        # FIX(G3-P1): 传入真实日收益序列，让 DSR 计算使用实际 skew/kurt。
        result = DeflatedSharpe().evaluate(float(observed), n_trials, returns=daily,
                                           track_record_length=trec)
        result["observed_basis"] = obs_basis
        result["in_sample_sharpe"] = round(grid_best, 4)
        result["oos_sharpe_used"] = round(float(oos_used), 4) if isinstance(oos_used, (int, float)) else None
        # P2-2(路线图): 携带 Stage5 网格上算出的 PBO(CSCV)，供 G3 与报告展示。
        result["pbo"] = _jsonable(grid.get("pbo", {})) if isinstance(grid.get("pbo"), dict) else None
        note = ("observed_sharpe 取自 Stage6 OOS(pooled)夏普——诚实反映泛化能力"
                if obs_basis == "oos_walkforward" else
                "OOS 夏普不可用，回退样本内网格夏普（已在 observed_basis 标注，G3 需谨慎解读）")
        return {"status": "ok", "result": _jsonable(result), "note": note}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage9_benchmarks(ctx):
    """多基准对比（新增）。"""
    try:
        from backtest.multi_benchmark import compare_benchmarks
        return compare_benchmarks(ctx["data_dict"], ctx["codes"], quick=ctx["quick"])
    except ImportError:
        return {"status": "skipped", "reason": "backtest/multi_benchmark.py 待实现（Stage 9）"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage10_cost_sensitivity(ctx):
    """成本敏感性（新增）。"""
    try:
        from backtest.cost_sensitivity import run_cost_ladder
        return run_cost_ladder(ctx["data_dict"], ctx["codes"], quick=ctx["quick"])
    except ImportError:
        return {"status": "skipped", "reason": "backtest/cost_sensitivity.py 待实现（Stage 10）"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage11_regime_segments(ctx):
    """分段绩效（新增）。"""
    try:
        from backtest.regime_segments import segmented_performance
        return segmented_performance(ctx["data_dict"], ctx["codes"])
    except ImportError:
        return {"status": "skipped", "reason": "backtest/regime_segments.py 待实现（Stage 11）"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage12_factor_layers(ctx):
    """因子分层（复用 factors 体系）。"""
    try:
        from backtest.factor_layers import run_factor_layering
        return run_factor_layering(ctx["data_dict"])
    except ImportError:
        return {"status": "skipped", "reason": "backtest/factor_layers.py 待实现（Stage 12）"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage13_archive_report(ctx):
    """结果存档 + 基线 diff + 总报告 HTML。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    record = {
        "run_id": run_id,
        "git_commit": _git_commit(),
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "quick": ctx["quick"],
        "gates": evaluate_gates(ctx),
        "stage_status": {k: v.get("status") for k, v in ctx.items() if k.startswith("stage")},
    }
    with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(_jsonable(record), ensure_ascii=False) + "\n")

    report_path = None
    try:
        from backtest.master_report import generate_master_report
        report_path = generate_master_report(ctx, record)
    except ImportError:
        print("[i] backtest/master_report.py 待实现（Stage 13 总报告）")
    return {"status": "ok", "run_id": run_id,
            "archive": ARCHIVE_PATH, "report": report_path, "gates": record["gates"]}


def stage14_minute_simulation(ctx):
    """分钟级执行仿真（新增 Stage14）。

    当前状态: 分钟数据表已废弃，统一由 intraday_feed.py 的 pkl 缓存管理。
    若近 3-5 年分钟数据可用，标定滑点/参与率/竞价滑点；
    否则标记 skipped 并在报告中注明。
    """
    try:
        from data.intraday_feed import IntradayFeed
        # 检查是否有分钟数据
        sample_code = ctx["codes"][0] if ctx["codes"] else None
        if sample_code is None:
            return {"status": "skipped", "reason": "无样本标的"}
        feed = IntradayFeed()
        has_data = hasattr(feed, 'has_minute_data') and feed.has_minute_data(sample_code)
        if not has_data:
            return {"status": "skipped",
                    "reason": "分钟数据不可用（intraday_feed pkl缓存为空），"
                              "滑点/冲击参数使用默认值（拍脑袋口径）"}
        return {"status": "ok", "note": "分钟级执行仿真已接入"}
    except ImportError:
        return {"status": "skipped",
                "reason": "intraday_feed 不可用，滑点/冲击参数使用默认值"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage15_capacity_scan(ctx):
    """资金规模与容量扫描（新增 Stage15）。

    资金档位: {50万, 500万, 2000万, 1亿}，逐档跑同一策略，
    记录成交率、平均滑点、年化收益随资金的衰减曲线。
    """
    try:
        from backtest.engine import BacktestEngineV2
        from backtest.broker import CostConfig
        dd = ctx["data_dict"]
        pool = [c for c in ctx.get("codes", []) if c in dd][:5]
        if not pool:
            return {"status": "skipped", "reason": "无有效标的"}
        sub = {c: dd[c].tail(200) for c in pool}
        capital_grid = [500000, 5000000, 20000000, 100000000]
        scan_results = []
        for capital in capital_grid:
            engine = BacktestEngineV2(initial_capital=capital)
            report = engine.run(sub, auto_monte_carlo=False)
            fillability = engine.broker.get_fillability_loss() if engine.broker else {}
            scan_results.append({
                "资金": capital,
                "总收益": report.get("total_return", 0),
                "年化收益": report.get("annual_return", 0),
                "拒单数": fillability.get("rejected_count", 0),
            })
        # 容量上限判定: 年化收益较50万基准衰减>30%
        base_return = scan_results[0]["年化收益"] if scan_results else 0
        capacity_limit = None
        for r in scan_results[1:]:
            if base_return > 0 and (base_return - r["年化收益"]) / base_return > 0.30:
                capacity_limit = r["资金"]
                break
        return {
            "status": "ok",
            "capacity_scan": scan_results,
            "capacity_limit": capacity_limit,
        }
    except Exception as e:
        return {"status": "fail", "error": str(e)}


def stage16_red_team_probes(ctx):
    """★ 对抗性红队探针套件（新增 Stage16，强制）。

    P-01~P-08 全绿才允许进模拟盘。任一判红 → 不允许。
    """
    try:
        from backtest.red_team_probes import run_all_probes
        result = run_all_probes(
            data_dict=ctx["data_dict"],
            codes=ctx["codes"],
        )
        return {
            "status": "ok" if result.get("overall") == "绿" else "fail",
            "result": _jsonable(result),
            "G8_probe": result.get("G8_probe", "FAIL"),
        }
    except ImportError:
        return {"status": "skipped", "reason": "backtest/red_team_probes.py 待实现"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


STAGES = [
    ("stage0", stage0_data_audit),
    ("stage1", stage1_single),
    ("stage2", stage2_equal_weight),
    ("stage3", stage3_full_pool),
    ("stage4", stage4_engine_crosscheck),
    ("stage5", stage5_param_grid),
    ("stage6", stage6_walk_forward),
    ("stage7", stage7_monte_carlo),
    ("stage8", stage8_dsr),
    ("stage9", stage9_benchmarks),
    ("stage10", stage10_cost_sensitivity),
    ("stage11", stage11_regime_segments),
    ("stage12", stage12_factor_layers),
    ("stage13", stage13_archive_report),
    ("stage14", stage14_minute_simulation),
    ("stage15", stage15_capacity_scan),
    ("stage16", stage16_red_team_probes),
]


# ============================================================
# 七道准入门
# ============================================================
def evaluate_gates(ctx):
    """返回七道门的 green/yellow/red 判定。新增 Stage 未实现时为 yellow。"""
    gates = {}

    gates_cfg = getattr(config, "MASTER_BACKTEST_CONFIG", {}).get("gates", {})

    s0 = ctx.get("stage0", {})
    gates["G1_data"] = "green" if s0.get("status") == "ok" and not s0.get("blocking_errors") \
        else ("red" if s0.get("blocking_errors") else "yellow")

    wf = ctx.get("stage6", {}).get("result", {})
    # P0-2(路线图): G2 优先用 pooled OOS 夏普（汇总有效窗逐笔），pooled 不可用时回退均值。
    oos = wf.get("oos_sharpe_pooled") if gates_cfg.get("G2_use_pooled", False) else wf.get("oos_sharpe")
    if not isinstance(oos, (int, float)):
        oos = wf.get("oos_sharpe")
    g2_min = gates_cfg.get("G2_oos_sharpe_min", 0.8)
    gates["G2_oos"] = "green" if isinstance(oos, (int, float)) and oos > g2_min \
        else ("red" if isinstance(oos, (int, float)) else "yellow")

    # P0-2(路线图): 新增 G2b——OOS 夏普中位数 > 阈值 且 有效窗占比 >= 阈值（泛化稳健性双保险）。
    med = wf.get("oos_sharpe_median")
    vr = wf.get("valid_ratio")
    med_min = gates_cfg.get("G2b_oos_median_min", 0.0)
    vr_min = gates_cfg.get("G2b_valid_ratio_min", 0.6)
    if isinstance(med, (int, float)) and isinstance(vr, (int, float)):
        gates["G2b_oos_robust"] = "green" if (med > med_min and vr >= vr_min) else "red"
    else:
        gates["G2b_oos_robust"] = "yellow"

    dsr = ctx.get("stage8", {}).get("result", {}).get("dsr")
    gates["G3_dsr"] = "green" if isinstance(dsr, (int, float)) and dsr > 0.95 \
        else ("red" if isinstance(dsr, (int, float)) else "yellow")
    # P2-2(路线图): G3 叠加 PBO 约束——DSR 绿但 PBO>G3_pbo_max 时降为 red（过拟合）；
    #   PBO 不可用时不改变 DSR 判定（不编造，仅如实附加）。
    _s8 = ctx.get("stage8", {}).get("result", {})
    _pbo = (_s8.get("pbo") or {}).get("pbo") if isinstance(_s8.get("pbo"), dict) else None
    _pbo_max = gates_cfg.get("G3_pbo_max", 0.5)
    if gates["G3_dsr"] == "green" and isinstance(_pbo, (int, float)) and _pbo > _pbo_max:
        gates["G3_dsr"] = "red"

    cost = ctx.get("stage10", {})
    deg = cost.get("sharpe_degradation_x2")
    gates["G4_cost"] = "green" if isinstance(deg, (int, float)) and deg < 0.30 \
        else ("red" if isinstance(deg, (int, float)) else "yellow")

    plateau = ctx.get("stage5", {})
    pdeg = plateau.get("neighbor_degradation")
    gates["G5_plateau"] = "green" if isinstance(pdeg, (int, float)) and pdeg < 0.15 \
        else ("red" if isinstance(pdeg, (int, float)) and pdeg > 0.30 else "yellow")

    hist = ctx.get("stage7", {}).get("historical", {})
    if isinstance(hist, dict) and hist and "error" not in hist:
        all_survive = all(v.get("survival", False) for v in hist.values() if isinstance(v, dict))
        gates["G6_stress"] = "green" if all_survive else "red"
    else:
        gates["G6_stress"] = "yellow"

    cross = ctx.get("stage4", {})
    ret_diff = cross.get("return_diff_abs")
    gates["G7_cross"] = "green" if isinstance(ret_diff, (int, float)) and ret_diff < 0.03 \
        else ("red" if isinstance(ret_diff, (int, float)) else "yellow")

    # G8: 红队探针（新增）
    probes = ctx.get("stage16", {})
    g8 = probes.get("G8_probe")
    gates["G8_probe"] = "green" if g8 == "PASS" \
        else ("red" if g8 == "FAIL" else "yellow")

    return gates


# ============================================================
# 工具函数
# ============================================================
def _jsonable(obj):
    """把 DataFrame/ndarray 等转为可 JSON 序列化结构（DataFrame 仅取摘要）。"""
    import numpy as np
    import pandas as pd
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()
                if k not in ("daily_values", "trades", "windows")}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, pd.DataFrame):
        return {"_dataframe": True, "rows": len(obj),
                "columns": list(obj.columns)}
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer, np.floating)):
        return obj.item()
    if isinstance(obj, float) and (obj != obj):  # NaN
        return None
    return obj


def _git_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                           text=True, cwd=ROOT_DIR, timeout=10)
        return r.stdout.strip()[:12] if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _neutralize_live_breaker():
    """中和实盘“策略失效检测器(SFD)”对回测的污染（仅当前进程，不碰持久化/实盘）。

    背景：strategy/position.py 的 calc_first_batch 会读 risk_control
    StrategyFailureDetector.get_status() 的持久化状态；当实盘 trade_history
    达阈值且期望为负时返回 breaker → 仓位×0，使 Stage2/3 引擎回测零交易（空洞），
    掩盖策略真实表现。回测须基于历史数据独立评估，不继承实盘熔断状态。
    仅在本解释器 monkeypatch，不触碰持久化文件、不影响实盘/调度进程。
    """
    try:
        from risk.risk_control import StrategyFailureDetector
        StrategyFailureDetector.get_status = lambda self: {
            "level": "normal", "position_scale": 1.0, "allow_buy": True,
            "reason": "backtest-neutralized",
        }
        return True
    except Exception:
        return False


# ============================================================
# 主流程
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="最全历史回测流水线")
    parser.add_argument("--quick", action="store_true", help="小样本快速验证")
    parser.add_argument("--stages", type=str, default="", help="只跑指定 Stage，如 0,1,2")
    parser.add_argument("--baseline", type=str, default="", help="对比基线 run_id")
    args = parser.parse_args()

    selected = None
    if args.stages:
        selected = {f"stage{int(x)}" for x in args.stages.split(",") if x.strip() != ""}

    print("=" * 64)
    print(f"  最全历史回测流水线  quick={args.quick}")
    print("=" * 64)
    # 回测卫生：中和实盘熔断状态，避免 SFD breaker 污染引擎回测（仅本进程）
    if _neutralize_live_breaker():
        print("[i] 已中和实盘策略失效检测器(SFD)熔断状态（仅本回测进程，不影响实盘/调度）")
    data_dict = load_data_dict(quick=args.quick)
    if not data_dict:
        return
    # Phase 2: 回测宇宙 = STOCK_POOL + BACKTEST_EXTRA_POOL（扩展标的覆盖）
    # V11.2 P2-1: + BACKTEST_DELISTED_POOL（退市/ST摘牌标的，有数据则纳入以缓解幸存者偏差）
    _pool_codes = list(getattr(config, "STOCK_POOL", {}).keys())
    for _extra_name in ("BACKTEST_EXTRA_POOL", "BACKTEST_DELISTED_POOL"):
        for _c in getattr(config, _extra_name, {}):
            if _c not in _pool_codes:
                _pool_codes.append(_c)
    codes = [c for c in _pool_codes if c in data_dict and c != "000300"]
    if args.quick:
        codes = codes[:8]

    ctx = {"data_dict": data_dict, "codes": codes, "quick": args.quick}
    t0 = time.time()
    for name, fn in STAGES:
        if selected and name not in selected:
            continue
        ts = time.time()
        print(f"\n>>> {name} ...")
        try:
            result = fn(ctx)
        except Exception as e:  # 兜底：任何 Stage 异常不阻断
            result = {"status": "fail", "error": str(e), "tb": traceback.format_exc()}
        ctx[name] = result
        print(f"    [{result.get('status', '?')}] {time.time() - ts:.1f}s "
              f"{result.get('reason', result.get('error', ''))}")

    gates = ctx.get("stage13", {}).get("gates") or evaluate_gates(ctx)
    print("\n" + "=" * 64)
    print("  八道准入门 (G1-G7 + G8红队探针)")
    for g, v in gates.items():
        mark = {"green": "PASS", "yellow": "WARN", "red": "FAIL"}[v]
        print(f"    {g:12s} {mark}")
    overall = "green" if all(v == "green" for v in gates.values()) \
        else ("red" if any(v == "red" for v in gates.values()) else "yellow")
    print(f"\n  总判定: {overall.upper()}  "
          f"({'允许进入模拟盘' if overall == 'green' else '暂不进入模拟盘，见报告'})")
    print(f"  总耗时: {time.time() - t0:.1f}s")
    print("=" * 64)


if __name__ == "__main__":
    main()
