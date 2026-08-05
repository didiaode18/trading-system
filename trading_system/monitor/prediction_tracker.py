"""
预测验证闭环追踪器（V3.2新增）
================================
核心功能: 记录所有预测 → N天后自动验证 → 统计准确率 → 反馈给决策

覆盖模块:
  1. 趋势预测 (trend_forecast.py) 的方向/置信度
  2. 多模块共识 (consensus.py) 的方向判断
  3. CANSLIM选股评分的区分度
  4. ML预测（如果启用）

验证逻辑:
  - 记录预测当天的方向/评分/置信度
  - 5个交易日后回填实际涨跌幅
  - 按置信度分桶统计准确率（校准曲线）
  - 输出周报: 各模块准确率趋势

使用方式:
    from monitor.prediction_tracker import PredictionTracker
    tracker = PredictionTracker()
    tracker.record_forecast(code, direction, confidence, score)
    tracker.verify_all()  # 每日盘后调用
    report = tracker.get_accuracy_report()

持久化: data/prediction_history.json
"""

import os
import sys
import json
import logging
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 预测历史文件
PREDICTION_DB_PATH = os.path.join(config.DATA_DIR, "prediction_history.json")

# 验证等待天数（交易日）
VERIFY_WAIT_DAYS = 5


class PredictionTracker:
    """预测验证闭环追踪器"""

    def __init__(self):
        self.records = []  # 所有预测记录
        self._load()

    # ============================================================
    # 持久化
    # ============================================================

    def _load(self):
        if os.path.exists(PREDICTION_DB_PATH):
            try:
                with open(PREDICTION_DB_PATH, 'r', encoding='utf-8') as f:
                    self.records = json.load(f)
            except Exception as e:
                logger.warning(f"[预测追踪] 加载失败: {e}")
                self.records = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(PREDICTION_DB_PATH), exist_ok=True)
            # 只保留最近500条
            if len(self.records) > 500:
                self.records = self.records[-500:]
            with open(PREDICTION_DB_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.records, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.error(f"[预测追踪] 保存失败: {e}")

    # ============================================================
    # 记录预测
    # ============================================================

    def record_forecast(self, code: str, direction: str, confidence: int,
                        composite_score: float, source: str = "trend_forecast",
                        extra: dict = None):
        """记录一条趋势预测
        
        参数:
            code: 股票代码
            direction: "看多"/"看空"/"中性"/"偏多"/"偏空"
            confidence: 置信度(0-100)
            composite_score: 综合评分(0-100)
            source: 来源模块
            extra: 额外信息
        """
        record = {
            "code": code,
            "date": datetime.date.today().isoformat(),
            "source": source,
            "direction": direction,
            "confidence": confidence,
            "score": composite_score,
            "verified": False,
            "actual_return_5d": None,
            "actual_direction": None,
            "is_correct": None,
        }
        if extra:
            record["extra"] = extra
        self.records.append(record)
        self._save()

    def record_consensus(self, code: str, direction: str, confidence: int,
                         score: float, conflict: bool):
        """记录共识模块预测"""
        self.record_forecast(
            code=code,
            direction=direction,
            confidence=confidence,
            composite_score=score,
            source="consensus",
            extra={"conflict": conflict},
        )

    def record_screener(self, code: str, total_score: float, factors: dict):
        """记录选股评分（用于区分度验证）"""
        self.record_forecast(
            code=code,
            direction="看多" if total_score >= 65 else ("看空" if total_score < 30 else "中性"),
            confidence=int(min(95, max(30, total_score))),
            composite_score=total_score,
            source="screener",
            extra={"factors": {k: round(v, 1) for k, v in factors.items()}},
        )

    # ============================================================
    # 验证（每日盘后调用）
    # ============================================================

    def verify_all(self, price_data: dict = None):
        """验证所有到期预测
        
        参数:
            price_data: {code: DataFrame} 最新行情数据
                       如果为None，尝试从数据库加载
        """
        today = datetime.date.today()
        verified_count = 0

        for record in self.records:
            if record["verified"]:
                continue

            # 检查是否到期（5个交易日≈7自然日）
            pred_date = datetime.date.fromisoformat(record["date"])
            days_elapsed = (today - pred_date).days
            if days_elapsed < VERIFY_WAIT_DAYS + 2:  # +2容错周末
                continue

            # 获取实际收益
            code = record["code"]
            actual_return = self._get_actual_return(code, record["date"], price_data)
            if actual_return is None:
                continue

            # 回填验证
            record["actual_return_5d"] = round(actual_return, 4)
            record["actual_direction"] = "看多" if actual_return > 0.01 else ("看空" if actual_return < -0.01 else "中性")
            record["is_correct"] = self._judge_correct(record["direction"], record["actual_direction"])
            record["verified"] = True
            verified_count += 1

        if verified_count > 0:
            self._save()
            logger.info(f"[预测追踪] 本次验证{verified_count}条预测")

    def _get_actual_return(self, code: str, pred_date_str: str,
                           price_data: dict = None) -> float:
        """获取预测日之后5天的实际收益率"""
        try:
            if price_data and code in price_data:
                df = price_data[code]
            else:
                from data.data_loader import load_daily_data
                df = load_daily_data(code, days=30)

            if df is None or df.empty:
                return None

            # 找到预测日之后的数据
            dates = df["date"].astype(str).tolist()
            try:
                idx = dates.index(pred_date_str)
            except ValueError:
                # 预测日可能不在数据中，找最近的
                available = [d for d in dates if d >= pred_date_str]
                if not available:
                    return None
                idx = dates.index(available[0])

            # 取预测日后第5天的收盘价
            target_idx = min(idx + VERIFY_WAIT_DAYS, len(df) - 1)
            if target_idx <= idx:
                return None

            pred_close = df["close"].iloc[idx]
            actual_close = df["close"].iloc[target_idx]
            if pred_close <= 0:
                return None
            return (actual_close - pred_close) / pred_close

        except Exception as e:
            logger.debug(f"[预测追踪] 获取{code}实际收益失败: {e}")
            return None

    @staticmethod
    def _judge_correct(predicted_dir: str, actual_dir: str) -> bool:
        """判断预测是否正确"""
        # 宽松判定：方向一致即正确（看多/偏多都算多）
        pred_bull = "多" in predicted_dir
        pred_bear = "空" in predicted_dir
        actual_bull = "多" in actual_dir
        actual_bear = "空" in actual_dir

        if pred_bull and actual_bull:
            return True
        if pred_bear and actual_bear:
            return True
        if "中性" in predicted_dir:
            return True  # 中性预测不判错
        return False

    # ============================================================
    # 统计报告
    # ============================================================

    def get_accuracy_report(self) -> dict:
        """生成预测准确率报告"""
        verified = [r for r in self.records if r["verified"]]
        if not verified:
            return {"total": 0, "message": "暂无已验证预测"}

        # 按来源分组统计
        by_source = {}
        for r in verified:
            src = r.get("source", "unknown")
            if src not in by_source:
                by_source[src] = {"total": 0, "correct": 0}
            by_source[src]["total"] += 1
            if r["is_correct"]:
                by_source[src]["correct"] += 1

        source_stats = {}
        for src, stats in by_source.items():
            acc = stats["correct"] / stats["total"] if stats["total"] > 0 else 0
            source_stats[src] = {
                "total": stats["total"],
                "correct": stats["correct"],
                "accuracy": round(acc, 3),
            }

        # 按置信度分桶（校准曲线）
        confidence_buckets = {
            "high(>=75)": {"total": 0, "correct": 0},
            "medium(50-74)": {"total": 0, "correct": 0},
            "low(<50)": {"total": 0, "correct": 0},
        }
        for r in verified:
            conf = r.get("confidence", 50)
            if conf >= 75:
                bucket = "high(>=75)"
            elif conf >= 50:
                bucket = "medium(50-74)"
            else:
                bucket = "low(<50)"
            confidence_buckets[bucket]["total"] += 1
            if r["is_correct"]:
                confidence_buckets[bucket]["correct"] += 1

        calibration = {}
        for bucket, stats in confidence_buckets.items():
            if stats["total"] > 0:
                calibration[bucket] = {
                    "total": stats["total"],
                    "accuracy": round(stats["correct"] / stats["total"], 3),
                }

        # 总体
        total_correct = sum(1 for r in verified if r["is_correct"])
        overall_acc = total_correct / len(verified)

        return {
            "total_verified": len(verified),
            "overall_accuracy": round(overall_acc, 3),
            "by_source": source_stats,
            "calibration": calibration,
            "latest_update": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def get_summary_text(self) -> str:
        """生成文字摘要（供邮件/日志使用）"""
        report = self.get_accuracy_report()
        if report.get("total_verified", 0) == 0:
            return "[预测追踪] 暂无已验证数据，继续积累中..."

        lines = [
            f"📊 预测验证报告 (共{report['total_verified']}条已验证)",
            f"   总体准确率: {report['overall_accuracy']:.1%}",
        ]
        for src, stats in report.get("by_source", {}).items():
            lines.append(f"   • {src}: {stats['accuracy']:.1%} ({stats['correct']}/{stats['total']})")

        cal = report.get("calibration", {})
        if cal:
            lines.append("   置信度校准:")
            for bucket, data in cal.items():
                lines.append(f"     {bucket}: 实际准确率{data['accuracy']:.1%} (样本{data['total']})")

        return "\n".join(lines)
