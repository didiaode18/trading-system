# -*- coding: utf-8 -*-
"""
回本计划模块（任务#1新增）
==========================
基于当前持仓市值与回本缺口金额，计算:
  1. 组合所需总收益率（缺口/总市值）
  2. 逐只持仓的目标价与所需涨幅（按市值权重等比分摊组合收益率）
  3. 可行性评估（目标价 vs 趋势预测压力位，超压力位15%以上标记换股）
  4. 多期限档位（3/6/12月）的月化要求与激进程度评级

纯函数实现，不依赖网络，可独立单元测试。
"""

import os
import json
import datetime


def _empty_plan(total_value, target_gap):
    """构造空计划结构（缺口<=0或市值<=0时返回，不抛异常）"""
    return {
        "available": False,
        "target_gap": float(target_gap or 0.0),
        "total_value": float(total_value or 0.0),
        "required_return_pct": 0.0,
        "stock_targets": [],
        "horizons": [],
    }


def _rate_monthly_rating(monthly_pct):
    """月化收益率评级: >4%激进, >2.5%偏积极, 否则可执行"""
    if monthly_pct > 4.0:
        return "激进"
    if monthly_pct > 2.5:
        return "偏积极"
    return "可执行"


def build_recovery_plan(holdings, total_value, target_gap,
                        months=(3, 6, 12), forecast_results=None):
    """
    构建回本计划

    参数:
        holdings: list[dict]，每项含 code/name/shares/price（最新价）/sector，
                  模块内部按 shares×price 计算市值权重（缺失时回退"市值"键）
        total_value: 账户总市值（元），用于计算组合所需总收益率
        target_gap: 回本缺口金额（元），<=0 时返回空计划
        months: 回本期限档位（月），默认(3,6,12)
        forecast_results: 趋势预测结果 {code: {"levels": {"first_resistance": x}}}，可选

    返回:
        dict:
          - available: 计划是否有效
          - required_return_pct: 组合所需总收益率（小数，如0.598=59.8%）
          - stock_targets: 每只持仓的 target_price/required_gain_pct/need_swap
            （目标价=现价×(1+组合收益率)，等比分摊；目标价超压力位15%标记换股）
          - horizons: 每档期限的月化要求与评级
    """
    if target_gap is None or target_gap <= 0 or total_value is None or total_value <= 0:
        return _empty_plan(total_value, target_gap)

    required_return = target_gap / total_value  # 组合所需总收益率（小数）
    forecast_results = forecast_results or {}

    # 内部计算市值权重
    items = []
    for h in (holdings or []):
        shares = float(h.get("shares", 0) or 0)
        price = float(h.get("price", 0) or 0)
        mv = shares * price
        if mv <= 0:
            mv = float(h.get("市值", 0) or 0)
        items.append({"raw": h, "mv": mv})
    total_mv = sum(i["mv"] for i in items)

    stock_targets = []
    for item in items:
        h = item["raw"]
        mv = item["mv"]
        price = float(h.get("price", 0) or 0)
        weight = (mv / total_mv) if total_mv > 0 else 0.0
        # 等比分摊组合收益率: 每只所需涨幅 = 组合所需收益率
        required_gain_pct = required_return * 100.0
        target_price = round(price * (1 + required_return), 2) if price > 0 else 0.0

        # 可行性: 与趋势预测第一压力位比较，超压力位15%以上 → 建议换股
        fr = forecast_results.get(h.get("code", ""), {}) or {}
        levels = fr.get("levels", {}) if isinstance(fr, dict) else {}
        resistance = float(levels.get("first_resistance", 0) or 0)
        need_swap = bool(resistance > 0 and target_price > resistance * 1.15)

        stock_targets.append({
            "code": h.get("code", ""),
            "name": h.get("name", ""),
            "sector": h.get("sector", ""),
            "price": price,
            "weight": round(weight, 4),
            "target_price": target_price,
            "required_gain_pct": round(required_gain_pct, 2),
            "resistance": resistance,
            "need_swap": need_swap,
            "feasibility": "超压力位15%以上，建议换股" if need_swap else "压力位内可达",
        })

    # 期限档位: 月化要求 = (1+总收益)^(1/m) - 1
    horizons = []
    for m in (months or ()):
        if m <= 0:
            continue
        monthly_pct = ((1 + required_return) ** (1.0 / m) - 1) * 100.0
        horizons.append({
            "months": m,
            "monthly_required_pct": round(monthly_pct, 2),
            "rating": _rate_monthly_rating(monthly_pct),
        })

    return {
        "available": True,
        "target_gap": float(target_gap),
        "total_value": float(total_value),
        "required_return_pct": required_return,
        "stock_targets": stock_targets,
        "horizons": horizons,
    }


def track_progress(total_value, target_gap, progress_file):
    """
    记录当日回本进度快照到JSON数组文件（同日覆盖）

    参数:
        total_value: 当前账户总市值（元）
        target_gap: 回本缺口金额（元）
        progress_file: 进度文件路径（JSON数组），目录不存在时自动创建

    返回:
        当日快照dict；任何IO/解析异常返回None（不抛出）
    """
    try:
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        if target_gap and target_gap > 0:
            denom = (total_value or 0) + target_gap
            progress_pct = round((total_value or 0) / denom * 100.0, 2) if denom > 0 else 0.0
        else:
            progress_pct = 100.0

        history = []
        if os.path.exists(progress_file):
            with open(progress_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                history = loaded

        snapshot = {
            "date": today_str,
            "total_value": round(float(total_value or 0), 2),
            "gap": round(float(target_gap or 0), 2),
            "progress_pct": progress_pct,
        }
        # 同日覆盖
        history = [s for s in history if s.get("date") != today_str]
        history.append(snapshot)

        dir_path = os.path.dirname(progress_file)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        return snapshot
    except Exception:
        return None
