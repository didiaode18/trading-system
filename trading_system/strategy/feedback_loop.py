# -*- coding: utf-8 -*-
"""
绩效归因反馈闭环 (Feedback Loop) V10.0
=======================================
追踪每次推荐的实际盈亏，按regime/板块分组统计，形成反馈闭环。

核心职责:
  1. 结算历史推荐的T+N收益（T+1/T+3/T+5）
  2. 按regime状态分组统计胜率
  3. 按板块分组统计胜率
  4. 止损触发率 + MFE捕获率
  5. 参数自适应建议（观察模式，不自动执行）

数据流:
  recommend_history.json + K线数据
      → FeedbackLoop.settle_recommendations()
      → {regime_stats, sector_stats, overall_stats}

使用方式:
    from strategy.feedback_loop import FeedbackLoop
    fb = FeedbackLoop()
    report = fb.generate_feedback_report()

设计原则:
  - <30条记录时不输出反馈（数据不足）
  - 任何异常不抛出，降级为空报告
  - auto_adjust仅建议，不自动执行
"""

import logging
import json
import os
import datetime

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# 推荐历史文件路径
_RECOMMEND_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'recommend_history.json'
)

# 反馈结果保存路径
_FEEDBACK_RESULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'feedback_result.json'
)

# 最小样本数（低于此数不输出反馈）
_MIN_SAMPLE_SIZE = 5


class FeedbackLoop:
    """
    绩效归因反馈闭环

    职责:
      追踪每次推荐的实际盈亏，按regime/板块分组统计，形成反馈闭环。
      包括 T+1/T+3/T+5 收益结算、regime分组胜率、板块分组胜率、
      止损触发率、MFE捕获率、参数自适应建议。

    核心输入:
      - data/recommend_history.json: 推荐历史记录
      - baostock K线数据: 用于计算T+N收益

    核心输出 (generate_feedback_report() 返回 dict):
      regime_stats: 按regime分组的胜率/平均收益
      sector_stats: 按板块分组的胜率/平均收益
      overall_stats: 总体统计（胜率/平均收益/MFE/MAE）

    设计原则:
      - <5条记录时不输出反馈（数据不足）
      - 任何异常不抛出，降级为空报告
      - auto_adjust仅建议，不自动执行
    """

    def __init__(self):
        self.history = self._load_history()
        self.feedback = self._load_feedback()

    def _load_history(self) -> list:
        """加载推荐历史"""
        try:
            if os.path.exists(_RECOMMEND_HISTORY_PATH):
                with open(_RECOMMEND_HISTORY_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as _e:
            logger.warning(f"加载推荐历史失败: {_e}")
        return []

    def _load_feedback(self) -> list:
        """加载已结算的反馈结果"""
        try:
            if os.path.exists(_FEEDBACK_RESULT_PATH):
                with open(_FEEDBACK_RESULT_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_feedback(self):
        """保存反馈结果"""
        try:
            os.makedirs(os.path.dirname(_FEEDBACK_RESULT_PATH), exist_ok=True)
            with open(_FEEDBACK_RESULT_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.feedback, f, ensure_ascii=False, indent=2)
        except Exception as _e:
            logger.warning(f"保存反馈结果失败: {_e}")

    def settle_recommendations(self, days_forward: int = 5) -> dict:
        """
        结算历史推荐的T+N收益

        参数:
            days_forward: 结算天数（默认5天）

        返回:
            {
                "settled_count": int,
                "regime_stats": {regime: {win_rate, avg_return, count}},
                "sector_stats": {sector: {win_rate, avg_return, count}},
                "overall_stats": {win_rate, avg_return, avg_mfe, mfe_capture, ...},
                "details": [...]
            }
        """
        from data.data_loader import fetch_stock_daily_baostock

        if not self.history:
            return {"settled_count": 0, "regime_stats": {}, "sector_stats": {},
                    "overall_stats": {}, "details": []}

        # 已结算的日期集合（幂等）
        _settled_dates = {fb.get("recommend_date") for fb in self.feedback
                          if fb.get("settled_days") >= days_forward}

        details = []
        settled_count = 0

        for snapshot in self.history:
            rec_date = snapshot.get("date", "")
            if rec_date in _settled_dates:
                continue  # 已结算

            # 获取推荐日之后的K线数据计算T+N收益
            try:
                rec_dt = datetime.datetime.strptime(rec_date, "%Y-%m-%d").date()
                # 只结算至少有days_forward+2天历史的推荐
                if (datetime.date.today() - rec_dt).days < days_forward + 2:
                    continue
            except (ValueError, TypeError):
                continue

            # 加载该推荐日对应的regime状态
            _regime_state = self._get_regime_at_date(rec_date)

            for item in snapshot.get("core", []) + snapshot.get("watch", []):
                code = item.get("code", "")
                name = item.get("name", "")
                score = item.get("score", 0)
                if not code:
                    continue

                # 获取K线数据
                try:
                    start = rec_date
                    end = (rec_dt + datetime.timedelta(days=days_forward + 5)).strftime("%Y-%m-%d")
                    df = fetch_stock_daily_baostock(code, start_date=start, end_date=end)
                    if df is None or df.empty or len(df) < 2:
                        continue

                    # 推荐日收盘价（基准）
                    base_price = df["close"].iloc[0]
                    if base_price <= 0:
                        continue

                    # 计算T+N收益率
                    returns = {}
                    for n in [1, 3, 5]:
                        if len(df) > n:
                            returns[f"T+{n}"] = round(
                                (df["close"].iloc[n] / base_price - 1) * 100, 2)

                    # MFE（最大浮盈）
                    if len(df) > days_forward:
                        _high_n = df["high"].iloc[:days_forward + 1].max()
                        mfe = round((_high_n / base_price - 1) * 100, 2)
                    else:
                        mfe = 0

                    # MAE（最大浮亏）
                    if len(df) > days_forward:
                        _low_n = df["low"].iloc[:days_forward + 1].min()
                        mae = round((_low_n / base_price - 1) * 100, 2)
                    else:
                        mae = 0

                    # 止损触发（假设6%止损）
                    stop_triggered = mae <= -6.0

                    _sector = item.get("sector", "")
                    _pool = "core" if item in snapshot.get("core", []) else "watch"

                    detail = {
                        "recommend_date": rec_date,
                        "code": code,
                        "name": name,
                        "score": score,
                        "pool": _pool,
                        "sector": _sector,
                        "regime": _regime_state,
                        "returns": returns,
                        "mfe": mfe,
                        "mae": mae,
                        "stop_triggered": stop_triggered,
                        "settled_days": days_forward,
                    }
                    details.append(detail)
                    settled_count += 1

                except Exception as _e:
                    logger.debug(f"结算{code}失败: {_e}")
                    continue

        # 保存新结算结果
        self.feedback.extend(details)
        self._save_feedback()

        # 分组统计
        regime_stats = self._group_stats(details, "regime")
        sector_stats = self._group_stats(details, "sector")
        overall_stats = self._overall_stats(details)

        return {
            "settled_count": settled_count,
            "regime_stats": regime_stats,
            "sector_stats": sector_stats,
            "overall_stats": overall_stats,
            "details": details,
        }

    def generate_feedback_report(self) -> dict:
        """
        生成完整反馈报告

        返回:
            {
                "available": bool,
                "total_records": int,
                "regime_stats": {...},
                "sector_stats": {...},
                "overall_stats": {...},
                "summary": str,
                "adjustments": [...],
            }
        """
        if not self.feedback or len(self.feedback) < _MIN_SAMPLE_SIZE:
            return {
                "available": False,
                "total_records": len(self.feedback) if self.feedback else 0,
                "summary": f"反馈数据不足（{len(self.feedback) if self.feedback else 0}条，需≥{_MIN_SAMPLE_SIZE}条）",
                "adjustments": [],
            }

        regime_stats = self._group_stats(self.feedback, "regime")
        sector_stats = self._group_stats(self.feedback, "sector")
        overall_stats = self._overall_stats(self.feedback)

        # 生成摘要文本
        summary_lines = [f"📊 绩效反馈报告（{len(self.feedback)}条推荐记录）"]
        summary_lines.append(f"  整体胜率: {overall_stats.get('win_rate', 0):.0%} | "
                             f"平均收益: {overall_stats.get('avg_return', 0):+.2f}% | "
                             f"MFE捕获: {overall_stats.get('mfe_capture', 0):.0%}")

        for regime, stats in regime_stats.items():
            summary_lines.append(f"  {regime}: 胜率{stats.get('win_rate', 0):.0%} "
                                 f"({stats.get('count', 0)}条) "
                                 f"平均{stats.get('avg_return', 0):+.2f}%")

        # 参数调整建议
        adjustments = self.auto_adjust_parameters()

        return {
            "available": True,
            "total_records": len(self.feedback),
            "regime_stats": regime_stats,
            "sector_stats": sector_stats,
            "overall_stats": overall_stats,
            "summary": "\n".join(summary_lines),
            "adjustments": adjustments,
        }

    def auto_adjust_parameters(self) -> list:
        """
        根据反馈结果建议参数微调（观察模式，不自动执行）

        返回:
            [{"parameter": str, "current": float, "suggested": float, "reason": str}]
        """
        adjustments = []
        if len(self.feedback) < _MIN_SAMPLE_SIZE * 2:
            return adjustments

        regime_stats = self._group_stats(self.feedback, "regime")

        # 1. BEAR模式胜率过低 → 建议提高pass_threshold
        bear_stats = regime_stats.get("BEAR", {})
        if bear_stats.get("count", 0) >= _MIN_SAMPLE_SIZE:
            bear_wr = bear_stats.get("win_rate", 0)
            if bear_wr < 0.3:
                adjustments.append({
                    "parameter": "BEAR.pass_threshold",
                    "current": 65,
                    "suggested": 70,
                    "reason": f"BEAR模式胜率仅{bear_wr:.0%}，建议提高选股门槛至70分",
                })
            elif bear_wr > 0.6:
                adjustments.append({
                    "parameter": "BEAR.pass_threshold",
                    "current": 65,
                    "suggested": 60,
                    "reason": f"BEAR模式胜率{bear_wr:.0%}良好，可适当降低门槛",
                })

        # 2. BULL模式MFE捕获率低 → 建议启用trailing_stop
        bull_stats = regime_stats.get("BULL", {})
        if bull_stats.get("count", 0) >= _MIN_SAMPLE_SIZE:
            mfe_cap = bull_stats.get("mfe_capture", 0)
            if mfe_cap < 0.4:
                adjustments.append({
                    "parameter": "BULL.risk_rules.trailing_stop",
                    "current": True,
                    "suggested": True,
                    "reason": f"BULL模式MFE捕获率{mfe_cap:.0%}偏低，建议检查移动止盈是否有效执行",
                })

        # 3. 止损触发率过高 → 建议放宽止损幅度
        stop_triggered = [fb for fb in self.feedback if fb.get("stop_triggered")]
        stop_rate = len(stop_triggered) / len(self.feedback) if self.feedback else 0
        if stop_rate > 0.5:
            adjustments.append({
                "parameter": "RANGE.risk_rules.stop_loss_pct",
                "current": 0.06,
                "suggested": 0.07,
                "reason": f"止损触发率{stop_rate:.0%}过高(>50%)，建议适当放宽止损幅度",
            })

        # 4. 板块胜率分析 → 建议调整防御板块列表
        sector_stats = self._group_stats(self.feedback, "sector")
        weak_sectors = [s for s, st in sector_stats.items()
                        if st.get("count", 0) >= 3 and st.get("win_rate", 0) < 0.25]
        if weak_sectors:
            adjustments.append({
                "parameter": "BEAR.defensive_sectors",
                "current": ["大消费", "公用事业", "医药生物", "银行"],
                "suggested": "保持现状",
                "reason": f"低胜率板块: {', '.join(weak_sectors)}，建议检查是否应从防御列表移除",
            })

        return adjustments

    def _get_regime_at_date(self, date_str: str) -> str:
        """获取某日的regime状态（从regime_history.json读取）"""
        try:
            _regime_history_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'regime_history.json'
            )
            if os.path.exists(_regime_history_path):
                with open(_regime_history_path, 'r', encoding='utf-8') as f:
                    records = json.load(f)
                for rec in reversed(records):
                    if rec.get("date", "") == date_str:
                        return rec.get("state", "RANGE")
        except Exception:
            pass
        return "UNKNOWN"

    def _group_stats(self, details: list, group_key: str) -> dict:
        """按指定维度分组统计"""
        if not details:
            return {}

        groups = {}
        for d in details:
            key = d.get(group_key, "UNKNOWN")
            if not key:
                key = "UNKNOWN"
            if key not in groups:
                groups[key] = []
            groups[key].append(d)

        stats = {}
        for key, items in groups.items():
            # 使用T+5收益（优先），其次T+3，最后T+1
            returns = []
            for item in items:
                _ret = item.get("returns", {})
                r = _ret.get("T+5", _ret.get("T+3", _ret.get("T+1", 0)))
                returns.append(r)

            wins = [r for r in returns if r > 0]
            mfes = [item.get("mfe", 0) for item in items]

            stats[key] = {
                "count": len(items),
                "win_rate": len(wins) / len(items) if items else 0,
                "avg_return": np.mean(returns) if returns else 0,
                "avg_mfe": np.mean(mfes) if mfes else 0,
                "mfe_capture": (np.mean(returns) / np.mean(mfes))
                    if mfes and np.mean(mfes) > 0 else 0,
            }

        return stats

    def _overall_stats(self, details: list) -> dict:
        """整体统计"""
        if not details:
            return {}

        returns = []
        for d in details:
            _ret = d.get("returns", {})
            r = _ret.get("T+5", _ret.get("T+3", _ret.get("T+1", 0)))
            returns.append(r)

        wins = [r for r in returns if r > 0]
        mfes = [d.get("mfe", 0) for d in details]
        maes = [d.get("mae", 0) for d in details]
        stops = sum(1 for d in details if d.get("stop_triggered"))

        return {
            "count": len(details),
            "win_rate": len(wins) / len(details) if details else 0,
            "avg_return": np.mean(returns) if returns else 0,
            "avg_win": np.mean(wins) if wins else 0,
            "avg_loss": np.mean([r for r in returns if r <= 0]) if returns else 0,
            "avg_mfe": np.mean(mfes) if mfes else 0,
            "avg_mae": np.mean(maes) if maes else 0,
            "mfe_capture": (np.mean(returns) / np.mean(mfes))
                if mfes and np.mean(mfes) > 0 else 0,
            "stop_trigger_rate": stops / len(details) if details else 0,
        }


# ============================================================
# 便捷函数（供scheduler调用）
# ============================================================
def run_daily_feedback_settle() -> dict:
    """
    每日结算入口（供scheduler调用）

    返回:
        {"settled_count": int, "report_available": bool, "summary": str}
    """
    try:
        fb = FeedbackLoop()
        settle_result = fb.settle_recommendations(days_forward=5)
        settled_count = settle_result.get("settled_count", 0)

        report = fb.generate_feedback_report()
        summary = report.get("summary", "")

        if settled_count > 0:
            logger.info(f"[FeedbackLoop] 结算{settled_count}条推荐记录")

        return {
            "settled_count": settled_count,
            "report_available": report.get("available", False),
            "summary": summary,
            "adjustments": report.get("adjustments", []),
        }
    except Exception as _e:
        logger.error(f"[FeedbackLoop] 每日结算异常: {_e}")
        return {"settled_count": 0, "report_available": False, "summary": "", "adjustments": []}
