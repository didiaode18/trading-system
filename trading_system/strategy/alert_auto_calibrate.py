"""
预警效果→参数自优化闭环（V10.0 P0-① 新增）
==============================================
消费 AlertEvaluator 的评估结果，按预警类型计算阈值/冷却调整建议，
写入 data/alert_calibration.json，供 IntradayMonitor 加载使用。

核心逻辑:
  1. 读取 alert_eval_history.json 中已验证的预警记录
  2. 按预警类型分组统计准确率（rolling window）
  3. 高准确率类型 → 收紧阈值（乘数<1）+ 缩短冷却（乘数<1）
  4. 低准确率类型 → 放宽阈值（乘数>1）+ 延长冷却（乘数>1）
  5. 安全护栏: 调整幅度不超过 ±30%
  6. 观察模式: 前 N 天仅记录不生效，防止冷启动过拟合

接入方式:
  - scheduler.py 每周六 11:30 自动调用 calibrate()
  - intraday_monitor.py 启动时加载 data/alert_calibration.json

持久化: data/alert_calibration.json
"""

import os
import sys
import json
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 文件路径
EVAL_DB_PATH = os.path.join(config.DATA_DIR, "alert_eval_history.json")
CALIBRATION_PATH = os.path.join(config.DATA_DIR, "alert_calibration.json")

# 安全护栏参数
MAX_ADJUSTMENT = 0.30       # 单次调整幅度上限 ±30%
MIN_SAMPLES = 5             # 最少样本数（低于此数不调整）
MIN_ACCURACY_THRESHOLD = 0.3  # 准确率低于此值才放宽（高于此值可收紧）
OBSERVATION_DAYS = 14       # 观察期天数（冷启动保护）

# 目标准确率区间（校准目标）
TARGET_ACCURACY_LOW = 0.55   # 低于此 → 放宽
TARGET_ACCURACY_HIGH = 0.75  # 高于此 → 收紧

# 风险类预警类型（阈值放宽方向 = 更负 = 乘数>1）
RISK_ALERT_TYPES = {
    "急跌预警", "止损预警(待确认)", "止损确认", "止损缓冲中",
    "抛压沉重", "被动跌破(大盘联动)", "波动率突变",
    "梯度减仓", "大盘急跌", "止损Buffer中",
    "深亏标的每日提醒",
}

# 机会类预警类型
OPPORTUNITY_ALERT_TYPES = {
    "止盈提醒", "回落止盈", "V反保护", "均值回归暂缓止损",
    "止损解除(收回)", "加仓机会",
}

# 中性预警类型
NEUTRAL_ALERT_TYPES = {
    "振幅异常", "托盘撤退", "VWAP失守", "疑似洗盘",
    "假突破确认(洗盘)", "开盘定性", "换手率背离",
    "多信号共振",
}


class AlertCalibrator:
    """预警参数自优化校准器"""

    def __init__(self):
        self.calibration = {}  # 当前校准参数

    # ============================================================
    # 主入口：执行校准
    # ============================================================

    def calibrate(self) -> dict:
        """执行一次校准（每周调用）

        返回:
            校准结果 dict，包含 by_type 统计和 adjustment 建议
        """
        records = self._load_eval_records()
        if not records:
            logger.info("[预警校准] 无评估记录，跳过校准")
            return {"status": "skip", "reason": "no_records"}

        # 按类型分组统计
        by_type = self._compute_by_type_accuracy(records)
        if not by_type:
            logger.info("[预警校准] 无已验证记录，跳过校准")
            return {"status": "skip", "reason": "no_verified"}

        # 计算调整因子
        adjustments = self._compute_adjustments(by_type)

        # 加载现有校准（用于平滑过渡）
        existing = self._load_calibration()
        existing_factors = existing.get("threshold_factors", {})

        # 应用安全护栏 + 平滑
        final_factors = {}
        for atype, adj in adjustments.items():
            old_factor = existing_factors.get(atype, 1.0)
            new_factor = self._apply_guard(old_factor, adj["threshold_factor"])
            cooldown_factor = self._apply_guard(
                existing.get("cooldown_factors", {}).get(atype, 1.0),
                adj["cooldown_factor"]
            )
            final_factors[atype] = {
                "threshold_factor": round(new_factor, 3),
                "cooldown_factor": round(cooldown_factor, 3),
                "accuracy": adj["accuracy"],
                "samples": adj["samples"],
                "direction": adj["direction"],
            }

        # 判断是否在观察期
        is_observation = self._is_observation_period(existing)

        # 写入校准文件
        result = {
            "last_updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_records": len(records),
            "verified_records": sum(a["samples"] for a in adjustments.values()),
            "observation_mode": is_observation,
            "threshold_factors": {
                k: v["threshold_factor"] for k, v in final_factors.items()
            },
            "cooldown_factors": {
                k: v["cooldown_factor"] for k, v in final_factors.items()
            },
            "by_type": final_factors,
            "history": self._append_history(existing, final_factors),
        }

        self._save_calibration(result)

        mode_str = "观察模式" if is_observation else "生效模式"
        logger.info(
            f"[预警校准] 完成（{mode_str}）| "
            f"已验证{result['verified_records']}条 | "
            f"调整{len(final_factors)}种类型"
        )
        for atype, info in final_factors.items():
            logger.info(
                f"  {atype}: 准确率{info['accuracy']:.0%}({info['samples']}条) "
                f"→ 阈值×{info['threshold_factor']:.2f} 冷却×{info['cooldown_factor']:.2f} "
                f"[{info['direction']}]"
            )

        return result

    # ============================================================
    # 内部方法
    # ============================================================

    def _load_eval_records(self) -> list:
        """加载 AlertEvaluator 的评估记录"""
        if not os.path.exists(EVAL_DB_PATH):
            return []
        try:
            with open(EVAL_DB_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get("records", [])
        except Exception as e:
            logger.warning(f"[预警校准] 加载评估记录失败: {e}")
            return []

    def _compute_by_type_accuracy(self, records: list) -> dict:
        """按预警类型分组统计准确率

        返回: {alert_type: {"effective": N, "false_positive": N,
                            "accuracy": float, "samples": int}}
        """
        by_type = {}
        for r in records:
            verdict = r.get("final_verdict")
            if verdict not in ("effective", "false_positive"):
                continue  # 未验证的不计入

            atype = r.get("type", "未知")
            if atype not in by_type:
                by_type[atype] = {"effective": 0, "false_positive": 0}

            by_type[atype][verdict] += 1

        # 计算准确率
        result = {}
        for atype, stats in by_type.items():
            total = stats["effective"] + stats["false_positive"]
            if total < MIN_SAMPLES:
                continue  # 样本不足，不纳入校准
            accuracy = stats["effective"] / total
            result[atype] = {
                "effective": stats["effective"],
                "false_positive": stats["false_positive"],
                "accuracy": round(accuracy, 3),
                "samples": total,
            }
        return result

    def _compute_adjustments(self, by_type: dict) -> dict:
        """根据准确率计算调整因子

        规则:
          - 准确率 > TARGET_ACCURACY_HIGH → 收紧（阈值乘数<1，冷却乘数<1）
          - 准确率 < TARGET_ACCURACY_LOW → 放宽（阈值乘数>1，冷却乘数>1）
          - 中间区间 → 不调整（乘数=1）
          - 调整幅度与偏离目标的程度成正比

        返回: {alert_type: {"threshold_factor": float, "cooldown_factor": float,
                            "accuracy": float, "samples": int, "direction": str}}
        """
        adjustments = {}

        for atype, stats in by_type.items():
            acc = stats["accuracy"]

            if acc >= TARGET_ACCURACY_HIGH:
                # 高准确率 → 收紧
                # 偏离越大，收紧越多；最大收紧到 0.7（阈值×0.7）
                overshoot = (acc - TARGET_ACCURACY_HIGH) / (1.0 - TARGET_ACCURACY_HIGH)
                threshold_factor = 1.0 - overshoot * 0.3  # 最多收紧30%
                cooldown_factor = 1.0 - overshoot * 0.2   # 冷却最多缩短20%
                direction = "收紧"

            elif acc < TARGET_ACCURACY_LOW:
                # 低准确率 → 放宽
                # 偏离越大，放宽越多；最大放宽到 1.3（阈值×1.3）
                undershoot = (TARGET_ACCURACY_LOW - acc) / TARGET_ACCURACY_LOW
                threshold_factor = 1.0 + undershoot * 0.3  # 最多放宽30%
                cooldown_factor = 1.0 + undershoot * 0.3   # 冷却最多延长30%
                direction = "放宽"

            else:
                # 中间区间 → 不调整
                threshold_factor = 1.0
                cooldown_factor = 1.0
                direction = "保持"

            adjustments[atype] = {
                "threshold_factor": round(threshold_factor, 3),
                "cooldown_factor": round(cooldown_factor, 3),
                "accuracy": acc,
                "samples": stats["samples"],
                "direction": direction,
            }

        return adjustments

    def _apply_guard(self, old_factor: float, new_factor: float) -> float:
        """安全护栏: 单次调整幅度不超过 ±MAX_ADJUSTMENT

        同时确保因子在 [0.5, 1.5] 绝对范围内
        """
        delta = new_factor - old_factor
        clamped_delta = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, delta))
        result = old_factor + clamped_delta
        return max(0.5, min(1.5, result))

    def _is_observation_period(self, existing: dict) -> bool:
        """判断是否在观察期（前 OBSERVATION_DAYS 天）"""
        updated = existing.get("last_updated", "")
        if not updated:
            return True  # 首次校准，进入观察期
        try:
            first_date = datetime.datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
            days_since = (datetime.datetime.now() - first_date).days
            return days_since < OBSERVATION_DAYS
        except (ValueError, TypeError):
            return True

    def _append_history(self, existing: dict, new_factors: dict) -> list:
        """追加校准历史（保留最近 12 次）"""
        history = existing.get("history", [])
        entry = {
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "factors": {
                k: {
                    "threshold_factor": v["threshold_factor"],
                    "cooldown_factor": v["cooldown_factor"],
                    "accuracy": v["accuracy"],
                }
                for k, v in new_factors.items()
            }
        }
        history.append(entry)
        return history[-12:]  # 保留最近12次

    # ============================================================
    # 持久化
    # ============================================================

    def _load_calibration(self) -> dict:
        """加载现有校准参数"""
        if not os.path.exists(CALIBRATION_PATH):
            return {}
        try:
            with open(CALIBRATION_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[预警校准] 加载校准文件失败: {e}")
            return {}

    def _save_calibration(self, data: dict):
        """保存校准参数"""
        try:
            os.makedirs(os.path.dirname(CALIBRATION_PATH), exist_ok=True)
            with open(CALIBRATION_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.warning(f"[预警校准] 保存校准文件失败: {e}")


# ============================================================
# 便捷函数（供 scheduler 调用）
# ============================================================

def calibrate() -> dict:
    """执行预警参数校准（scheduler 每周调用）"""
    calibrator = AlertCalibrator()
    return calibrator.calibrate()


def load_calibration() -> dict:
    """加载校准参数（IntradayMonitor 启动时调用）

    返回:
        {"threshold_factors": {type: factor},
         "cooldown_factors": {type: factor},
         "observation_mode": bool}
        如果文件不存在或加载失败，返回空 dict
    """
    if not os.path.exists(CALIBRATION_PATH):
        return {}
    try:
        with open(CALIBRATION_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {
            "threshold_factors": data.get("threshold_factors", {}),
            "cooldown_factors": data.get("cooldown_factors", {}),
            "observation_mode": data.get("observation_mode", True),
            "last_updated": data.get("last_updated", ""),
        }
    except Exception as e:
        logger.warning(f"[预警校准] 加载校准参数失败: {e}")
        return {}


# ============================================================
# CLI 入口（手动执行校准）
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )
    result = calibrate()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
