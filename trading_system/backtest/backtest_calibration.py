"""
回测→风控参数闭环校准模块
==========================
根据回测绩效指标、蒙特卡洛压力测试和 Deflated Sharpe 结果，
自动对比当前风控参数阈值，生成校准建议供人工审核。

使用方式:
    from backtest.backtest_calibration import BacktestCalibrator

    calibrator = BacktestCalibrator()

    # 1. 生成校准建议（回测后调用）
    result = calibrator.calibrate(
        backtest_results=performance_report,       # metrics.py 生成的绩效报告
        monte_carlo_results=mc_result,             # monte_carlo.py 输出（可选）
        deflated_sharpe=dsr_result["dsr"],         # deflated_sharpe.py 的 DSR 值（可选）
    )
    # result = {"suggestions": [...], "is_trustworthy": bool, "details": {...}}

    # 2. 保存建议到 JSON（供人工审核）
    calibrator.save_suggestions(result)
    # → trading_system/data/calibration_suggestions.json

    # 3. 人工在 JSON 中将需要应用的建议标记 "approved": true 后：
    applied = calibrator.apply_suggestions()
    # → 返回已应用的参数变更汇总（不会自动修改 config.py）

校准规则:
    1. 胜率校准：回测胜率 < 默认胜率(0.45) → 建议调低 Kelly 使用的胜率参数
    2. 回撤校准：蒙特卡洛 95% 回撤 > 最大回撤警告线(10%) → 建议降低总仓位上限
    3. 可信度校准：Deflated Sharpe < 0.50 → 标记策略不可信，阻止所有参数更新
    4. 盈亏比校准：回测盈亏比 < 2.0 → 建议提高 min_risk_reward 阈值
    5. 交易频率校准：年化交易次数 < 20 → 警告过滤过严

关键约束:
    - 校准建议是建议性的，不自动修改 config.py
    - 需要在 JSON 文件中手动设置 "approved": true 才能应用
    - Deflated Sharpe < 0.50 时强制标记不可信，阻止所有参数更新
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================
# 校准阈值常量（不修改 config.py，在模块内定义默认值）
# ============================================================
# 胜率校准阈值：低于此值说明策略命中率不足，Kelly 应下调
DEFAULT_WIN_RATE_THRESHOLD = 0.45

# 最大回撤警告线：蒙特卡洛 95% 分位回撤超过此值 → 仓位过大
MAX_DRAWDOWN_WARNING = 0.10  # 10%

# Deflated Sharpe 可信度阈值：低于此值策略不可信
DSR_TRUST_THRESHOLD = 0.50

# 盈亏比校准阈值：低于此值说明止盈止损设置不合理
MIN_PROFIT_FACTOR_THRESHOLD = 2.0

# 年化最低交易次数：低于此值说明过滤条件过严
MIN_ANNUAL_TRADES = 20

# 建议保存路径
DEFAULT_SUGGESTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "calibration_suggestions.json",
)


class BacktestCalibrator:
    """回测→风控参数闭环校准器

    将回测绩效、蒙特卡洛压力测试和 Deflated Sharpe 结果与当前风控参数对比，
    生成可供人工审核的校准建议 JSON。
    """

    def __init__(self):
        """初始化校准器，记录当前校准时间"""
        self.calibration_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("[BacktestCalibrator] 校准器初始化，时间: %s", self.calibration_time)

    # ============================================================
    # 核心校准方法
    # ============================================================

    def calibrate(
        self,
        backtest_results: dict,
        monte_carlo_results: Optional[dict] = None,
        deflated_sharpe: Optional[float] = None,
    ) -> dict:
        """运行全套校准规则，生成校准建议

        Args:
            backtest_results: metrics.py 的 generate_performance_report() 返回的绩效报告，
                              需含 win_rate / profit_factor / total_trades / trading_days 等字段。
            monte_carlo_results: monte_carlo.py 的 run() 返回结果（可选），
                                 需含 max_drawdown_distribution.p95 / ruin_probability 等字段。
            deflated_sharpe: Deflated Sharpe Ratio 值（0~1，可选），
                             来自 deflated_sharpe.py 的 evaluate()["dsr"]。

        Returns:
            {
                "suggestions": [dict, ...],   # 各规则产生的校准建议列表
                "is_trustworthy": bool,        # 策略整体是否可信（DSR 未触发且无严重问题）
                "details": {...},              # 各项指标的原始值与阈值对比详情
            }
        """
        suggestions = []
        details = {}
        is_trustworthy = True

        logger.info("[calibrate] 开始校准，回测交易天数: %s，交易笔数: %s",
                    backtest_results.get("trading_days"), backtest_results.get("total_trades"))

        # -------------------------------------------------------
        # 规则 3（优先）：可信度校准 — Deflated Sharpe < 0.50
        # 先做此检查，若不可信则后续建议全部标记为"仅供参考，不可应用"
        # -------------------------------------------------------
        dsr_triggered = False
        if deflated_sharpe is not None:
            details["deflated_sharpe"] = {
                "value": deflated_sharpe,
                "threshold": DSR_TRUST_THRESHOLD,
                "status": "pass" if deflated_sharpe >= DSR_TRUST_THRESHOLD else "fail",
            }
            if deflated_sharpe < DSR_TRUST_THRESHOLD:
                dsr_triggered = True
                is_trustworthy = False
                logger.warning(
                    "[calibrate][可信度校准] Deflated Sharpe=%.4f < %.2f，策略不可信！"
                    "所有参数更新将被阻止。",
                    deflated_sharpe, DSR_TRUST_THRESHOLD,
                )
                suggestions.append({
                    "rule": "可信度校准（Deflated Sharpe）",
                    "severity": "critical",
                    "metric": "deflated_sharpe",
                    "current_value": deflated_sharpe,
                    "threshold": DSR_TRUST_THRESHOLD,
                    "message": (
                        f"Deflated Sharpe={deflated_sharpe:.4f} < {DSR_TRUST_THRESHOLD:.2f}，"
                        f"策略大概率是数据挖掘产物，所有参数更新已阻止。"
                        f"建议：增加样本外数据验证，或减少参数搜索次数。"
                    ),
                    "suggested_action": "阻止所有参数更新，建议增加样本外验证",
                    "approved": False,
                })
            else:
                logger.info(
                    "[calibrate][可信度校准] Deflated Sharpe=%.4f >= %.2f，策略可信。",
                    deflated_sharpe, DSR_TRUST_THRESHOLD,
                )

        # -------------------------------------------------------
        # 规则 1：胜率校准
        # 回测胜率 < 默认胜率(0.45) → 建议调低 Kelly 使用的胜率参数
        # -------------------------------------------------------
        win_rate = backtest_results.get("win_rate", None)
        if win_rate is not None:
            details["win_rate"] = {
                "value": win_rate,
                "threshold": DEFAULT_WIN_RATE_THRESHOLD,
                "status": "pass" if win_rate >= DEFAULT_WIN_RATE_THRESHOLD else "fail",
            }
            if win_rate < DEFAULT_WIN_RATE_THRESHOLD:
                # Kelly 公式中使用的胜率应下调到实际回测胜率（留 10% 安全边际）
                suggested_kelly_wr = round(win_rate * 0.9, 4)
                logger.warning(
                    "[calibrate][胜率校准] 回测胜率=%.4f < %.2f，建议将 Kelly 胜率参数"
                    "从默认 %.2f 下调至 %.4f（留 10%% 安全边际）。",
                    win_rate, DEFAULT_WIN_RATE_THRESHOLD,
                    DEFAULT_WIN_RATE_THRESHOLD, suggested_kelly_wr,
                )
                suggestions.append({
                    "rule": "胜率校准（Kelly 胜率参数）",
                    "severity": "warning",
                    "metric": "win_rate",
                    "current_value": win_rate,
                    "threshold": DEFAULT_WIN_RATE_THRESHOLD,
                    "message": (
                        f"回测胜率 {win_rate:.2%} 低于默认值 {DEFAULT_WIN_RATE_THRESHOLD:.0%}。"
                        f"建议将 Kelly 计算中使用的胜率参数下调至 {suggested_kelly_wr:.2%}"
                        f"（含 10% 安全边际），避免仓位过大。"
                    ),
                    "suggested_param": "kelly_win_rate",
                    "suggested_value": suggested_kelly_wr,
                    "approved": False,
                })
            else:
                logger.info(
                    "[calibrate][胜率校准] 回测胜率=%.4f >= %.2f，无需调整。",
                    win_rate, DEFAULT_WIN_RATE_THRESHOLD,
                )

        # -------------------------------------------------------
        # 规则 2：回撤校准（依赖蒙特卡洛结果）
        # 蒙特卡洛 95% 回撤 > MAX_DRAWDOWN_WARNING(10%) → 降低总仓位上限
        # -------------------------------------------------------
        if monte_carlo_results is not None and "error" not in monte_carlo_results:
            mc_dd_p95 = (
                monte_carlo_results
                .get("max_drawdown_distribution", {})
                .get("p95", None)
            )
            ruin_prob = monte_carlo_results.get("ruin_probability", None)

            details["monte_carlo_drawdown_p95"] = {
                "value": mc_dd_p95,
                "threshold": MAX_DRAWDOWN_WARNING,
                "status": "pass" if (mc_dd_p95 is not None and mc_dd_p95 <= MAX_DRAWDOWN_WARNING) else "fail",
            }
            details["ruin_probability"] = {"value": ruin_prob}

            if mc_dd_p95 is not None and mc_dd_p95 > MAX_DRAWDOWN_WARNING:
                # 建议按比例降低总仓位上限：当前上限 × (警告线 / 实际回撤)
                current_total_limit = 0.90  # POSITION_CONFIG["max_total"] 默认值
                suggested_total_limit = round(
                    current_total_limit * (MAX_DRAWDOWN_WARNING / mc_dd_p95), 2
                )
                suggested_total_limit = max(0.30, min(suggested_total_limit, 0.90))

                logger.warning(
                    "[calibrate][回撤校准] 蒙特卡洛 95%% 回撤=%.4f > %.2f，"
                    "建议将总仓位上限从 %.2f 降至 %.2f。",
                    mc_dd_p95, MAX_DRAWDOWN_WARNING,
                    current_total_limit, suggested_total_limit,
                )
                suggestions.append({
                    "rule": "回撤校准（总仓位上限）",
                    "severity": "warning",
                    "metric": "monte_carlo_drawdown_p95",
                    "current_value": mc_dd_p95,
                    "threshold": MAX_DRAWDOWN_WARNING,
                    "message": (
                        f"蒙特卡洛 95% 置信度回撤 {mc_dd_p95:.2%} 超过警告线 "
                        f"{MAX_DRAWDOWN_WARNING:.0%}。"
                        f"建议将总仓位上限从 {current_total_limit:.0%} 降至 "
                        f"{suggested_total_limit:.0%}，控制尾部风险。"
                    ),
                    "suggested_param": "total_position_max",
                    "suggested_value": suggested_total_limit,
                    "approved": False,
                })
            else:
                logger.info(
                    "[calibrate][回撤校准] 蒙特卡洛 95%% 回撤=%s，未超过 %.2f，无需调整。",
                    f"{mc_dd_p95:.4f}" if mc_dd_p95 is not None else "N/A",
                    MAX_DRAWDOWN_WARNING,
                )

            # 破产概率独立检查（不依赖回撤是否超标）
            if ruin_prob is not None and ruin_prob > 0.05:
                logger.warning(
                    "[calibrate][回撤校准] 破产概率=%.4f > 5%%，风险极高！",
                    ruin_prob,
                )
                suggestions.append({
                    "rule": "破产风险警告",
                    "severity": "critical",
                    "metric": "ruin_probability",
                    "current_value": ruin_prob,
                    "threshold": 0.05,
                    "message": (
                        f"蒙特卡洛破产概率 {ruin_prob:.2%} 超过 5%，"
                        f"资金腰斩风险极高，建议大幅降低仓位或暂停实盘。"
                    ),
                    "suggested_action": "大幅降低仓位或暂停实盘",
                    "approved": False,
                })

        # -------------------------------------------------------
        # 规则 4：盈亏比校准
        # 回测盈亏比 < 2.0 → 建议提高 min_risk_reward 阈值
        # -------------------------------------------------------
        profit_factor = backtest_results.get("profit_factor", None)
        if profit_factor is not None:
            details["profit_factor"] = {
                "value": profit_factor,
                "threshold": MIN_PROFIT_FACTOR_THRESHOLD,
                "status": "pass" if profit_factor >= MIN_PROFIT_FACTOR_THRESHOLD else "fail",
            }
            if profit_factor < MIN_PROFIT_FACTOR_THRESHOLD:
                # 建议 min_risk_reward 提高：取当前 RISK_UNIFIED_CONFIG 值与盈亏比的较大者
                current_min_rr = 2.5  # RISK_UNIFIED_CONFIG["min_risk_reward"] 默认值
                suggested_min_rr = round(max(current_min_rr, profit_factor * 1.2), 2)
                logger.warning(
                    "[calibrate][盈亏比校准] 回测盈亏比=%.2f < %.1f，"
                    "建议将 min_risk_reward 从 %.1f 提高至 %.2f。",
                    profit_factor, MIN_PROFIT_FACTOR_THRESHOLD,
                    current_min_rr, suggested_min_rr,
                )
                suggestions.append({
                    "rule": "盈亏比校准（min_risk_reward）",
                    "severity": "warning",
                    "metric": "profit_factor",
                    "current_value": profit_factor,
                    "threshold": MIN_PROFIT_FACTOR_THRESHOLD,
                    "message": (
                        f"回测盈亏比 {profit_factor:.2f} 低于目标 {MIN_PROFIT_FACTOR_THRESHOLD:.1f}。"
                        f"建议将 RISK_UNIFIED_CONFIG['min_risk_reward'] 从 {current_min_rr:.1f} "
                        f"提高至 {suggested_min_rr:.2f}，过滤低质量交易。"
                    ),
                    "suggested_param": "min_risk_reward",
                    "suggested_value": suggested_min_rr,
                    "approved": False,
                })
            else:
                logger.info(
                    "[calibrate][盈亏比校准] 回测盈亏比=%.2f >= %.1f，无需调整。",
                    profit_factor, MIN_PROFIT_FACTOR_THRESHOLD,
                )

        # -------------------------------------------------------
        # 规则 5：交易频率校准
        # 年化交易次数 < 20 → 警告过滤过严
        # -------------------------------------------------------
        total_trades = backtest_results.get("total_trades", 0)
        trading_days = backtest_results.get("trading_days", 1)
        annual_trades = (total_trades / trading_days * 252) if trading_days > 0 else 0

        details["annual_trades"] = {
            "value": round(annual_trades, 1),
            "threshold": MIN_ANNUAL_TRADES,
            "status": "pass" if annual_trades >= MIN_ANNUAL_TRADES else "fail",
        }
        if annual_trades < MIN_ANNUAL_TRADES:
            logger.warning(
                "[calibrate][交易频率校准] 年化交易次数=%.1f < %d，过滤条件可能过严。",
                annual_trades, MIN_ANNUAL_TRADES,
            )
            suggestions.append({
                "rule": "交易频率校准（过滤条件过严警告）",
                "severity": "info",
                "metric": "annual_trades",
                "current_value": round(annual_trades, 1),
                "threshold": MIN_ANNUAL_TRADES,
                "message": (
                    f"年化交易次数仅 {annual_trades:.1f} 次（共 {total_trades} 笔，"
                    f"{trading_days} 个交易日），低于 {MIN_ANNUAL_TRADES} 次/年。"
                    f"过滤条件可能过严，建议适当放宽入场条件或增加交易品种。"
                ),
                "suggested_action": "放宽入场条件或增加交易品种",
                "approved": False,
            })
        else:
            logger.info(
                "[calibrate][交易频率校准] 年化交易次数=%.1f >= %d，正常。",
                annual_trades, MIN_ANNUAL_TRADES,
            )

        # -------------------------------------------------------
        # DSR 不可信时，将所有建议标记为不可应用
        # -------------------------------------------------------
        if dsr_triggered:
            for s in suggestions:
                if s.get("severity") != "critical":
                    s["blocked_reason"] = "Deflated Sharpe < 0.50，策略不可信，参数更新已阻止"
            logger.warning(
                "[calibrate] 因 Deflated Sharpe 不可信，%d 条建议已被阻止。",
                sum(1 for s in suggestions if "blocked_reason" in s),
            )

        logger.info(
            "[calibrate] 校准完成：共 %d 条建议，策略可信=%s",
            len(suggestions), is_trustworthy,
        )

        return {
            "suggestions": suggestions,
            "is_trustworthy": is_trustworthy,
            "details": details,
        }

    # ============================================================
    # 保存建议
    # ============================================================

    def save_suggestions(self, calibrate_result: dict, path: Optional[str] = None) -> str:
        """将校准建议保存为 JSON 文件，供人工审核

        JSON 结构中每条建议含 "approved": false 字段，
        人工审核后改为 true 方可被 apply_suggestions() 应用。

        Args:
            calibrate_result: calibrate() 的返回值
            path: 保存路径（默认 trading_system/data/calibration_suggestions.json）

        Returns:
            实际保存的文件路径
        """
        save_path = path or DEFAULT_SUGGESTIONS_PATH

        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        output = {
            "calibration_time": self.calibration_time,
            "is_trustworthy": calibrate_result.get("is_trustworthy", False),
            "total_suggestions": len(calibrate_result.get("suggestions", [])),
            "approved_count": sum(
                1 for s in calibrate_result.get("suggestions", []) if s.get("approved", False)
            ),
            "details": calibrate_result.get("details", {}),
            "suggestions": calibrate_result.get("suggestions", []),
            "review_instructions": (
                "请将需要应用的建议的 'approved' 字段改为 true，"
                "然后调用 BacktestCalibrator().apply_suggestions() 应用。\n"
                "注意：Deflated Sharpe < 0.50 时所有建议将被强制阻止，无法应用。"
            ),
        }

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        logger.info("[save_suggestions] 校准建议已保存至: %s（共 %d 条）",
                    save_path, output["total_suggestions"])
        return save_path

    # ============================================================
    # 应用已审核通过的建议
    # ============================================================

    def apply_suggestions(self, path: Optional[str] = None) -> dict:
        """读取校准建议文件，应用 approved=true 的建议

        重要：此方法不会自动修改 config.py，而是返回已应用的参数变更汇总，
        由调用方决定如何将变更落实到实际配置中。

        Args:
            path: 建议文件路径（默认 trading_system/data/calibration_suggestions.json）

        Returns:
            {
                "applied": [dict, ...],    # 已应用的建议列表
                "skipped": [dict, ...],    # 被跳过（未批准或被阻止）的建议列表
                "summary": str,            # 人类可读的应用摘要
            }
        """
        load_path = path or DEFAULT_SUGGESTIONS_PATH

        if not os.path.exists(load_path):
            logger.warning("[apply_suggestions] 建议文件不存在: %s", load_path)
            return {"applied": [], "skipped": [], "summary": "建议文件不存在，无法应用。"}

        with open(load_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        is_trustworthy = data.get("is_trustworthy", False)
        suggestions = data.get("suggestions", [])

        applied = []
        skipped = []

        for s in suggestions:
            rule_name = s.get("rule", "unknown")
            approved = s.get("approved", False)
            blocked = "blocked_reason" in s

            if blocked:
                logger.info(
                    "[apply_suggestions] 跳过（被阻止）: %s — %s",
                    rule_name, s.get("blocked_reason", ""),
                )
                skipped.append({**s, "skip_reason": "blocked_by_dsr"})
                continue

            if not approved:
                logger.info("[apply_suggestions] 跳过（未批准）: %s", rule_name)
                skipped.append({**s, "skip_reason": "not_approved"})
                continue

            if not is_trustworthy:
                logger.warning(
                    "[apply_suggestions] 策略不可信（is_trustworthy=false），"
                    "跳过所有批准的建议: %s",
                    rule_name,
                )
                skipped.append({**s, "skip_reason": "not_trustworthy"})
                continue

            # 应用该建议
            applied.append(s)
            param = s.get("suggested_param", s.get("suggested_action", "unknown"))
            value = s.get("suggested_value", "N/A")
            logger.info(
                "[apply_suggestions] 已应用: %s → %s = %s",
                rule_name, param, value,
            )

        # 生成摘要
        if applied:
            lines = [f"已应用 {len(applied)} 条校准建议："]
            for a in applied:
                param = a.get("suggested_param", a.get("suggested_action", ""))
                value = a.get("suggested_value", "")
                lines.append(f"  • {a['rule']}: {param} → {value}")
            summary = "\n".join(lines)
        else:
            summary = "无已应用的建议。请在 JSON 文件中将 'approved' 设为 true 后重试。"

        logger.info("[apply_suggestions] 应用完成：已应用 %d 条，跳过 %d 条",
                    len(applied), len(skipped))

        # 更新文件中的 applied_count
        data["approved_count"] = len(applied)
        data["applied_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(load_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return {
            "applied": applied,
            "skipped": skipped,
            "summary": summary,
        }
