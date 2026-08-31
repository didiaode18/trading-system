"""
交易行为长期追踪与纪律评分（V10.0 P0-③ 新增）
==============================================
持久化交易行为画像，输出纪律评分(0-100)，弥补现有 trade_behavior.py
仅有当日分析的不足。

核心逻辑:
  1. 每日盘后从 analyze_trade_behavior 结果提取关键指标
  2. 滚动30日加权平均，平滑单日波动
  3. 五维度纪律评分:
     - 换手纪律 (20分): 换手率是否可控
     - 追高纪律 (20分): 追高成本占比
     - 止损纪律 (20分): 是否严格执行止损
     - 情绪纪律 (20分): 日内回转/闪电翻转
     - 频率纪律 (20分): 交易频率是否合理
  4. 输出总分 + 各维度分 + 改进建议

接入方式:
  - scheduler.py 每日盘后自动调用 update_from_daily()
  - generate_holdings_report.py 可调用 get_profile() 展示长期画像

持久化: data/behavior_profile.json
"""

import os
import sys
import json
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 持久化文件
PROFILE_PATH = os.path.join(config.DATA_DIR, "behavior_profile.json")

# 滚动窗口
ROLLING_DAYS = 30

# 评分权重（五维度各20分，总分100）
DIMENSION_WEIGHTS = {
    "turnover": 20,      # 换手纪律
    "chase": 20,         # 追高纪律
    "stop_loss": 20,     # 止损纪律
    "emotion": 20,       # 情绪纪律
    "frequency": 20,     # 频率纪律
}

# 各维度评分阈值（低于此值扣分）
TURNOVER_IDEAL = 0.3      # 换手率<30%为理想
TURNOVER_WARNING = 0.6    # 换手率>60%开始扣分
CHASE_RATIO_IDEAL = 0.0   # 追高成本占比=0为理想
CHASE_RATIO_WARNING = 0.001  # 追高成本>0.1%开始扣分
ROUNDTRIP_IDEAL = 0       # 日内回转=0为理想
ROUNDTRIP_WARNING = 500   # 日内回转亏损>500元开始扣分
FREQUENCY_IDEAL = 3       # 日交易≤3笔为理想
FREQUENCY_WARNING = 10    # 日交易>10笔开始扣分


class BehaviorTracker:
    """交易行为长期追踪器"""

    def __init__(self):
        self.profile = {}
        self._load()

    # ============================================================
    # 持久化
    # ============================================================

    def _load(self):
        if os.path.exists(PROFILE_PATH):
            try:
                with open(PROFILE_PATH, 'r', encoding='utf-8') as f:
                    self.profile = json.load(f)
            except Exception as e:
                logger.warning(f"[行为追踪] 加载失败: {e}")
                self.profile = {}

        # 初始化默认结构
        if "daily_records" not in self.profile:
            self.profile["daily_records"] = []
        if "stop_loss_stats" not in self.profile:
            self.profile["stop_loss_stats"] = {
                "total_triggers": 0,
                "executed": 0,
                "ignored": 0,
            }

    def _save(self):
        try:
            os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
            # 裁剪旧记录（保留最近90天）
            if len(self.profile.get("daily_records", [])) > 90:
                self.profile["daily_records"] = self.profile["daily_records"][-90:]
            with open(PROFILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.profile, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.warning(f"[行为追踪] 保存失败: {e}")

    # ============================================================
    # 每日更新
    # ============================================================

    def update_from_daily(self, daily_result: dict, stop_loss_executed: bool = None):
        """从当日 analyze_trade_behavior 结果更新长期画像

        参数:
            daily_result: analyze_trade_behavior 返回的 dict
            stop_loss_executed: 当日是否执行了止损（True/False/None=无止损触发）
        """
        if not daily_result or daily_result.get("total_trades", 0) == 0:
            return

        date = daily_result.get("date", datetime.date.today().isoformat())

        # 提取关键指标
        record = {
            "date": date,
            "total_trades": daily_result.get("total_trades", 0),
            "turnover": daily_result.get("turnover", 0),
            "chase_cost": daily_result.get("chase_cost", 0),
            "panic_cost": daily_result.get("panic_cost", 0),
            "roundtrip_loss": daily_result.get("roundtrip_loss", 0),
            "quick_flip_count": daily_result.get("quick_flip", {}).get("count", 0),
            "quick_flip_loss": daily_result.get("quick_flip", {}).get("loss", 0),
            "fees": daily_result.get("fees", {}).get("total", 0),
            "total_waste": daily_result.get("total_waste", 0),
            "severity": daily_result.get("severity", "ok"),
        }

        # 去重（同日期覆盖）
        self.profile["daily_records"] = [
            r for r in self.profile["daily_records"]
            if r.get("date") != date
        ]
        self.profile["daily_records"].append(record)

        # 更新止损统计
        if stop_loss_executed is not None:
            self.profile["stop_loss_stats"]["total_triggers"] += 1
            if stop_loss_executed:
                self.profile["stop_loss_stats"]["executed"] += 1
            else:
                self.profile["stop_loss_stats"]["ignored"] += 1

        self._save()
        logger.debug(f"[行为追踪] 已更新 {date}: {record['total_trades']}笔/换手{record['turnover']:.0%}")

    # ============================================================
    # 纪律评分
    # ============================================================

    def get_discipline_score(self) -> dict:
        """计算纪律评分（0-100）

        返回:
            {
                "total_score": 75,
                "dimensions": {
                    "turnover": {"score": 16, "max": 20, "avg": 0.35},
                    "chase": {"score": 18, "max": 20, "ratio": 0.0005},
                    "stop_loss": {"score": 20, "max": 20, "rate": 1.0},
                    "emotion": {"score": 15, "max": 20, "avg_loss": 200},
                    "frequency": {"score": 12, "max": 20, "avg": 5.2},
                },
                "suggestions": ["减少日内回转操作", "控制追高冲动"],
                "period": "2026-08-01 ~ 2026-08-23",
                "trading_days": 15,
            }
        """
        records = self.profile.get("daily_records", [])
        if not records:
            return {
                "total_score": 100,  # 无交易=完美纪律
                "dimensions": {},
                "suggestions": ["暂无交易记录"],
                "period": "",
                "trading_days": 0,
            }

        # 取最近 ROLLING_DAYS 天
        recent = records[-ROLLING_DAYS:]
        n_days = len(recent)

        # 计算各维度平均值
        avg_turnover = sum(r["turnover"] for r in recent) / n_days
        avg_chase = sum(r["chase_cost"] for r in recent) / n_days
        avg_roundtrip = sum(r["roundtrip_loss"] for r in recent) / n_days
        avg_trades = sum(r["total_trades"] for r in recent) / n_days

        # 止损执行率
        sl_stats = self.profile.get("stop_loss_stats", {})
        sl_total = sl_stats.get("total_triggers", 0)
        sl_executed = sl_stats.get("executed", 0)
        sl_rate = sl_executed / sl_total if sl_total > 0 else 1.0

        # 计算各维度得分
        dimensions = {}

        # 1. 换手纪律 (20分)
        if avg_turnover <= TURNOVER_IDEAL:
            turnover_score = 20
        elif avg_turnover >= TURNOVER_WARNING:
            turnover_score = max(0, 20 - (avg_turnover - TURNOVER_WARNING) * 40)
        else:
            turnover_score = 20 - (avg_turnover - TURNOVER_IDEAL) / (TURNOVER_WARNING - TURNOVER_IDEAL) * 10
        dimensions["turnover"] = {
            "score": round(max(0, min(20, turnover_score))),
            "max": 20,
            "avg": round(avg_turnover, 4),
        }

        # 2. 追高纪律 (20分)
        # 追高成本占总买入金额比例
        total_chase = sum(r.get("chase_cost", 0) for r in recent)
        total_buy = sum(r.get("buy_amount", r.get("total_waste", 0)) for r in recent)
        chase_ratio = total_chase / total_buy if total_buy > 0 else 0
        if chase_ratio <= CHASE_RATIO_IDEAL:
            chase_score = 20
        elif chase_ratio >= CHASE_RATIO_WARNING:
            chase_score = max(0, 20 - (chase_ratio / CHASE_RATIO_WARNING) * 15)
        else:
            chase_score = 20 - (chase_ratio / CHASE_RATIO_WARNING) * 10
        dimensions["chase"] = {
            "score": round(max(0, min(20, chase_score))),
            "max": 20,
            "ratio": round(chase_ratio, 6),
        }

        # 3. 止损纪律 (20分)
        stop_loss_score = 20 * sl_rate
        dimensions["stop_loss"] = {
            "score": round(max(0, min(20, stop_loss_score))),
            "max": 20,
            "rate": round(sl_rate, 3),
            "triggers": sl_total,
        }

        # 4. 情绪纪律 (20分)
        if avg_roundtrip <= ROUNDTRIP_IDEAL:
            emotion_score = 20
        elif avg_roundtrip >= ROUNDTRIP_WARNING:
            emotion_score = max(0, 20 - (avg_roundtrip - ROUNDTRIP_WARNING) / 100)
        else:
            emotion_score = 20 - (avg_roundtrip / ROUNDTRIP_WARNING) * 10
        dimensions["emotion"] = {
            "score": round(max(0, min(20, emotion_score))),
            "max": 20,
            "avg_loss": round(avg_roundtrip, 2),
        }

        # 5. 频率纪律 (20分)
        if avg_trades <= FREQUENCY_IDEAL:
            freq_score = 20
        elif avg_trades >= FREQUENCY_WARNING:
            freq_score = max(0, 20 - (avg_trades - FREQUENCY_WARNING) * 2)
        else:
            freq_score = 20 - (avg_trades - FREQUENCY_IDEAL) / (FREQUENCY_WARNING - FREQUENCY_IDEAL) * 10
        dimensions["frequency"] = {
            "score": round(max(0, min(20, freq_score))),
            "max": 20,
            "avg": round(avg_trades, 1),
        }

        # 总分
        total_score = sum(d["score"] for d in dimensions.values())

        # 生成改进建议
        suggestions = []
        if dimensions["turnover"]["score"] < 15:
            suggestions.append(f"换手率偏高({avg_turnover:.0%})，建议减少非必要操作")
        if dimensions["chase"]["score"] < 15:
            suggestions.append("追高成本较高，建议下单前先看当日已有更低成交价")
        if dimensions["stop_loss"]["score"] < 15:
            suggestions.append(f"止损执行率偏低({sl_rate:.0%})，建议严格执行预设止损")
        if dimensions["emotion"]["score"] < 15:
            suggestions.append(f"日内回转亏损较多(日均{avg_roundtrip:.0f}元)，建议减少T+0操作")
        if dimensions["frequency"]["score"] < 15:
            suggestions.append(f"交易频率偏高(日均{avg_trades:.1f}笔)，建议每笔下单前确认是否为计划内操作")

        if not suggestions:
            suggestions.append("交易纪律良好，继续保持")

        # 时间范围
        dates = [r["date"] for r in recent]
        period = f"{min(dates)} ~ {max(dates)}" if dates else ""

        return {
            "total_score": total_score,
            "dimensions": dimensions,
            "suggestions": suggestions,
            "period": period,
            "trading_days": n_days,
        }

    # ============================================================
    # 便捷方法
    # ============================================================

    def get_profile_summary(self) -> str:
        """生成文字摘要（供报告/邮件使用）"""
        score = self.get_discipline_score()
        if score["trading_days"] == 0:
            return "📊 [交易纪律] 暂无交易记录"

        total = score["total_score"]
        if total >= 80:
            icon, level = "✅", "优秀"
        elif total >= 60:
            icon, level = "⚠️", "良好"
        elif total >= 40:
            icon, level = "⚠️", "需改进"
        else:
            icon, level = "🚨", "严重"

        lines = [
            f"{icon} [交易纪律评分] {level} ({total}/100)",
            f"   统计期: {score['period']} ({score['trading_days']}个交易日)",
        ]

        dims = score["dimensions"]
        if dims:
            lines.append("   各维度:")
            dim_names = {
                "turnover": "换手纪律",
                "chase": "追高纪律",
                "stop_loss": "止损纪律",
                "emotion": "情绪纪律",
                "frequency": "频率纪律",
            }
            for key, name in dim_names.items():
                if key in dims:
                    d = dims[key]
                    lines.append(f"     • {name}: {d['score']}/{d['max']}")

        suggestions = score.get("suggestions", [])
        if suggestions and suggestions[0] != "交易纪律良好，继续保持":
            lines.append("   改进建议:")
            for s in suggestions[:3]:
                lines.append(f"     → {s}")

        return "\n".join(lines)


# ============================================================
# 便捷函数（供 scheduler 调用）
# ============================================================

def update_from_daily(daily_result: dict, stop_loss_executed: bool = None):
    """更新当日行为画像（scheduler 盘后调用）"""
    tracker = BehaviorTracker()
    tracker.update_from_daily(daily_result, stop_loss_executed)


def get_discipline_score() -> dict:
    """获取纪律评分（报告/邮件调用）"""
    tracker = BehaviorTracker()
    return tracker.get_discipline_score()


def get_profile_summary() -> str:
    """获取纪律摘要文字（供日志/邮件使用）"""
    tracker = BehaviorTracker()
    return tracker.get_profile_summary()


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(get_profile_summary())
    print()
    score = get_discipline_score()
    print(json.dumps(score, ensure_ascii=False, indent=2))
