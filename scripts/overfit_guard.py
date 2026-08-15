# -*- coding: utf-8 -*-
"""
V4.0(G10): 过拟合治理守卫（参数预算审计 + 样本外验证纪律检查）
================================================================
背景: 系统策略参数(阈值/权重/档位)散落在 config.py 与策略模块中，
参数越多、调参次数越多，回测结论越可能是过拟合产物。本工具提供
两道治理关卡（只审计告警，不修改任何策略逻辑）:

  1. 参数预算审计:
     - 清点 config.py 中各策略配置字典的可调参数数量
     - 扫描策略/回测脚本的数值魔法阈值密度
     - 总参数数超过预算(PARAM_BUDGET)时给出告警

  2. 样本外验证纪律检查:
     - 回测入口是否使用 walk-forward(样本外分段验证)
     - 是否存在 deflated Sharpe(多重检验校正)工具可用
     - 输出纪律清单(新增参数须过样本外验证等铁律)

用法:
    python scripts/overfit_guard.py            # 控制台输出审计报告
    python scripts/overfit_guard.py --save     # 同时落盘 output/overfit_guard_YYYYMMDD.txt
"""

import os
import re
import sys
import json
import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "trading_system"))

# ---- 治理常量（本文件独有，不进config，避免"治理参数也是参数"的悖论）----
PARAM_BUDGET = 80          # 全系统可调参数预算上限
MAGIC_DENSITY_WARN = 15    # 单文件数值阈值超过该个数 → 魔法数字密度告警
OOS_REQUIRED_FILES = [     # 必须引用样本外验证工具的回测入口
    "trading_system/run_full_backtest.py",
]

# config.py 中视为"策略可调参数"的字典名
CONFIG_DICTS = [
    "RISK_CONFIG", "SIZING_CONFIG", "PULLBACK_ADD_CONFIG",
    "JOURNAL_CONFIG", "CANSLIM_CONFIG", "MONITOR_CONFIG",
]

# 策略数值阈值扫描范围（相对PROJECT_ROOT）
SCAN_DIRS = ["trading_system/strategy", "trading_system/risk",
             "trading_system/position", "trading_system/factors"]

# 数值魔法阈值模式: 与比较运算符相邻的数字（如 > 0.15、* 0.95、>= 2e8）
_MAGIC_RE = re.compile(
    r"(?<=[<>=+\-*/\s])\d+\.?\d*(?:e[+-]?\d+)?(?=[\s)<>=,;])")


def count_config_params() -> dict:
    """清点config.py策略配置字典中的可调参数数量"""
    counts = {}
    try:
        import config
        for name in CONFIG_DICTS:
            d = getattr(config, name, None)
            if isinstance(d, dict):
                counts[name] = len(d)
    except Exception as e:
        counts["_error"] = str(e)
    return counts


def scan_magic_thresholds() -> list:
    """扫描策略模块数值阈值密度，返回超密度文件清单"""
    hot_files = []
    for rel_dir in SCAN_DIRS:
        abs_dir = os.path.join(PROJECT_ROOT, rel_dir.replace("/", os.sep))
        if not os.path.isdir(abs_dir):
            continue
        for fn in os.listdir(abs_dir):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(abs_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    src = f.read()
            except Exception:
                continue
            # 粗略剔除注释行，降低误报
            code_lines = [l for l in src.splitlines()
                          if not l.strip().startswith("#")]
            hits = len(_MAGIC_RE.findall("\n".join(code_lines)))
            if hits > MAGIC_DENSITY_WARN:
                hot_files.append({"file": os.path.join(rel_dir, fn),
                                  "threshold_count": hits})
    hot_files.sort(key=lambda x: -x["threshold_count"])
    return hot_files


def check_oos_discipline() -> dict:
    """样本外验证纪律检查"""
    result = {
        "walk_forward_available": False,
        "deflated_sharpe_available": False,
        "backtest_entries_with_oos": [],
        "backtest_entries_without_oos": [],
    }
    try:
        from backtest.walk_forward import WalkForwardAnalyzer  # noqa: F401
        result["walk_forward_available"] = True
    except Exception:
        pass
    try:
        from backtest.deflated_sharpe import DeflatedSharpe  # noqa: F401
        result["deflated_sharpe_available"] = True
    except Exception:
        pass
    for rel in OOS_REQUIRED_FILES:
        path = os.path.join(PROJECT_ROOT, rel.replace("/", os.sep))
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
        except Exception:
            continue
        if "walk_forward" in src or "WalkForward" in src:
            result["backtest_entries_with_oos"].append(rel)
        else:
            result["backtest_entries_without_oos"].append(rel)
    return result


DISCIPLINE_RULES = [
    "1. 新增任何可调参数，必须先在样本外区间(walk-forward后30%数据)验证有效性",
    "2. 同一组回测数据重复调参超过5次，结论须用Deflated Sharpe校正后再采信",
    "3. 参数总数超过预算(%d)时，新增参数必须先删除/合并一个旧参数" % PARAM_BUDGET,
    "4. 回测收益率与实盘cohort胜率(G2)偏离>50%时，冻结该参数组并复核",
    "5. 禁止针对单一异常交易日的特例参数(如某只股票的专属阈值)",
]


def run_audit(save: bool = False) -> dict:
    """执行完整审计，返回报告dict；save=True时落盘output/"""
    now = datetime.datetime.now()
    cfg_counts = count_config_params()
    config_total = sum(v for k, v in cfg_counts.items() if isinstance(v, int))
    magic_files = scan_magic_thresholds()
    magic_total = sum(f["threshold_count"] for f in magic_files)
    oos = check_oos_discipline()

    over_budget = config_total > PARAM_BUDGET
    oos_gap = (not oos["walk_forward_available"]) or oos["backtest_entries_without_oos"]

    lines = []
    lines.append("=" * 62)
    lines.append(f"  过拟合治理审计报告 | {now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 62)
    lines.append("")
    lines.append(f"[1] 参数预算审计 (预算: {PARAM_BUDGET})")
    for name, cnt in cfg_counts.items():
        lines.append(f"    {name}: {cnt}个参数")
    flag = "⚠️ 超预算" if over_budget else "✅ 在预算内"
    lines.append(f"    config策略参数合计: {config_total} | {flag}")
    lines.append(f"    策略模块数值阈值热点文件(>{MAGIC_DENSITY_WARN}个): {len(magic_files)}个")
    for f in magic_files[:5]:
        lines.append(f"      - {f['file']}: {f['threshold_count']}个阈值")
    lines.append("")
    lines.append("[2] 样本外验证纪律检查")
    lines.append(f"    walk-forward工具: {'✅ 可用' if oos['walk_forward_available'] else '❌ 不可用'}")
    lines.append(f"    deflated Sharpe工具: {'✅ 可用' if oos['deflated_sharpe_available'] else '❌ 不可用'}")
    for rel in oos["backtest_entries_with_oos"]:
        lines.append(f"    ✅ {rel}: 已接入样本外验证")
    for rel in oos["backtest_entries_without_oos"]:
        lines.append(f"    ❌ {rel}: 未接入walk-forward，回测结论过拟合风险高")
    lines.append("")
    lines.append("[3] 过拟合治理铁律")
    for r in DISCIPLINE_RULES:
        lines.append(f"    {r}")
    lines.append("")
    verdict = "⚠️ 存在过拟合治理缺口" if (over_budget or oos_gap) else "✅ 治理状态良好"
    lines.append(f"审计结论: {verdict}")

    report_text = "\n".join(lines)
    print(report_text)

    report = {
        "generated_at": now.isoformat(),
        "config_param_counts": cfg_counts,
        "config_total": config_total,
        "param_budget": PARAM_BUDGET,
        "over_budget": over_budget,
        "magic_threshold_files": magic_files[:10],
        "oos": oos,
        "verdict": verdict,
    }

    if save:
        out_dir = os.path.join(PROJECT_ROOT, "trading_system", "output")
        os.makedirs(out_dir, exist_ok=True)
        txt_path = os.path.join(out_dir, f"overfit_guard_{now.strftime('%Y%m%d')}.txt")
        json_path = os.path.join(out_dir, f"overfit_guard_{now.strftime('%Y%m%d')}.json")
        try:
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(report_text)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"\n[保存] {txt_path}")
        except Exception as e:
            print(f"\n[保存失败] {e}")
    return report


if __name__ == "__main__":
    run_audit(save="--save" in sys.argv)
