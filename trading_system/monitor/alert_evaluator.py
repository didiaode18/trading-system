"""
盘中预警效果在线评估器（V9.3 P1-⑥ 新增）
============================================
自动统计每条盘中预警的"发出后N分钟涨跌表现"，量化预警准确率/误报率。

核心逻辑:
  1. 预警触发时 → 记录预警信息+当时价格
  2. 延迟 5/15/30 分钟后 → 回查价格，计算实际涨跌
  3. 按预警类型判定"有效"/"误报"
  4. 每日汇总: 准确率、最佳/最差预警类型

判定规则:
  - 风险类预警（急跌/止损/抛压/波动率突变）: 后续继续跌 → 有效
  - 机会类预警（止盈/回落止盈）: 后续继续涨或回落幅度可控 → 有效
  - 中性预警（振幅异常/托盘撤退）: 后续振幅扩大 → 有效

使用方式:
    from monitor.alert_evaluator import AlertEvaluator
    evaluator = AlertEvaluator()
    evaluator.record_alert(alert_dict)       # 预警触发时调用
    evaluator.verify_pending()               # 每轮/定期调用
    report = evaluator.get_daily_report()    # 每日汇总

持久化: data/alert_eval_history.json
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
EVAL_DB_PATH = os.path.join(config.DATA_DIR, "alert_eval_history.json")

# 验证时间窗口（分钟）
VERIFY_WINDOWS = [5, 15, 30]

# 风险类预警：后续继续跌 → 有效（收益率 < -0.3% 判定为有效）
_RISK_ALERT_TYPES = {
    "急跌预警", "止损预警(待确认)", "止损确认", "止损缓冲中",
    "抛压沉重", "被动跌破(大盘联动)", "波动率突变",
    "梯度减仓", "大盘急跌", "止损Buffer中",
    "深亏标的每日提醒",
}

# 机会类预警：后续继续涨 → 有效（收益率 > 0.3% 判定为有效）
_OPPORTUNITY_ALERT_TYPES = {
    "止盈提醒", "回落止盈", "V反保护", "均值回归暂缓止损",
    "止损解除(收回)", "加仓机会",
}

# 中性预警：后续绝对涨跌 > 0.5% → 有效（有显著波动即有效）
_NEUTRAL_ALERT_TYPES = {
    "振幅异常", "托盘撤退", "VWAP失守", "疑似洗盘",
    "假突破确认(洗盘)", "开盘定性", "换手率背离",
    "多信号共振",
}

# 最大保留记录数
_MAX_RECORDS = 1000


class AlertEvaluator:
    """盘中预警效果在线评估器"""

    def __init__(self):
        self.records = []       # 所有预警记录
        self._pending = []      # 待验证的预警（内存缓冲）
        self._load()

    # ============================================================
    # 持久化
    # ============================================================

    def _load(self):
        if os.path.exists(EVAL_DB_PATH):
            try:
                with open(EVAL_DB_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.records = data.get("records", [])
                # 恢复 pending（跨轮次验证）
                self._pending = data.get("pending", [])
            except Exception as e:
                logger.warning(f"[预警评估] 加载失败: {e}")
                self.records = []
                self._pending = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(EVAL_DB_PATH), exist_ok=True)
            # 裁剪旧记录
            if len(self.records) > _MAX_RECORDS:
                # 记录偏移量，调整pending索引
                offset = len(self.records) - _MAX_RECORDS
                self.records = self.records[-_MAX_RECORDS:]
                self._pending = [i - offset for i in self._pending if i >= offset]
            # V10.3: 清理pending中已验证完毕的索引（防无限增长）
            self._pending = [i for i in self._pending if i < len(self.records)]
            # V10.3: 文件大小安全检查（超过50MB时强制截断）
            if os.path.exists(EVAL_DB_PATH):
                fsize = os.path.getsize(EVAL_DB_PATH)
                if fsize > 50 * 1024 * 1024:  # 50MB
                    logger.warning(f"[预警评估] 文件过大({fsize/1024/1024:.1f}MB)，强制裁剪")
                    self.records = self.records[-100:]  # 只保留最近100条
                    self._pending = []
            with open(EVAL_DB_PATH, 'w', encoding='utf-8') as f:
                json.dump({
                    "records": self.records,
                    "pending": self._pending,
                }, f, ensure_ascii=False, indent=1, default=str)
        except Exception as e:
            logger.warning(f"[预警评估] 保存失败: {e}")

    # ============================================================
    # 记录预警
    # ============================================================

    def record_alert(self, alert: dict):
        """记录一条预警（预警触发时调用）

        参数:
            alert: intraday_monitor 生成的预警 dict
                   必须包含: type, code, name, current_price
        """
        alert_type = alert.get("type", "")
        code = alert.get("code", "")
        price = alert.get("current_price", 0)
        level = alert.get("level", "info")

        if not code or not price or price <= 0:
            return

        now = datetime.datetime.now()
        record = {
            "code": code,
            "name": alert.get("name", ""),
            "type": alert_type,
            "level": level,
            "price_at_alert": price,
            "alert_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "alert_timestamp": now.timestamp(),
            "verified_windows": {},  # {5: {"price": x, "return": y, "effective": z}, ...}
            "final_verdict": None,   # "effective" / "false_positive" / None
        }
        self.records.append(record)
        self._pending.append(len(self.records) - 1)
        self._save()
        logger.debug(f"[预警评估] 记录: {alert_type} {code} @ {price}")

    # ============================================================
    # 验证（每轮调用）
    # ============================================================

    def verify_pending(self, price_getter=None):
        """验证待验证的预警（每轮轮询后调用）

        参数:
            price_getter: callable(code) -> float 获取当前价格
                           如果为None，使用 fetch_realtime_single
        """
        if not self._pending:
            return

        if price_getter is None:
            def _default_getter(code):
                try:
                    from data.realtime import fetch_realtime_single
                    q = fetch_realtime_single(code)
                    return q.get("price", 0) if q else 0
                except Exception:
                    return 0
            price_getter = _default_getter

        now = datetime.datetime.now()
        still_pending = []

        for idx in self._pending:
            if idx >= len(self.records):
                continue
            record = self.records[idx]
            alert_ts = record.get("alert_timestamp", 0)
            elapsed_min = (now.timestamp() - alert_ts) / 60

            # 检查各时间窗口
            for window in VERIFY_WINDOWS:
                window_key = str(window)  # JSON key 必须为 string
                if window_key in record["verified_windows"]:
                    continue  # 已验证过此窗口
                if elapsed_min < window:
                    still_pending.append(idx)
                    continue  # 还没到时间

                # 获取当前价格
                current_price = price_getter(record["code"])
                if current_price <= 0:
                    still_pending.append(idx)
                    continue

                # 计算收益率
                base_price = record["price_at_alert"]
                ret_pct = (current_price - base_price) / base_price * 100

                # 判定有效性
                effective = self._judge_effective(
                    record["type"], ret_pct
                )

                record["verified_windows"][window_key] = {
                    "price": current_price,
                    "return_pct": round(ret_pct, 3),
                    "effective": effective,
                }

            # 最后一个窗口验证完后，给出最终判定
            max_window = str(max(VERIFY_WINDOWS))
            if max_window in record["verified_windows"]:
                # 用 30 分钟窗口作为最终判定依据
                record["final_verdict"] = (
                    "effective"
                    if record["verified_windows"][max_window]["effective"]
                    else "false_positive"
                )
            else:
                still_pending.append(idx)

        self._pending = still_pending
        self._save()

    @staticmethod
    def _judge_effective(alert_type: str, return_pct: float) -> bool:
        """判定预警是否有效

        参数:
            alert_type: 预警类型
            return_pct: 预警后收益率（%，正=涨，负=跌）

        规则:
            风险类: 后续跌 > 0.3% → 有效
            机会类: 后续涨 > 0.3% → 有效
            中性类: |后续涨跌| > 0.5% → 有效
        """
        if alert_type in _RISK_ALERT_TYPES:
            return return_pct < -0.3
        elif alert_type in _OPPORTUNITY_ALERT_TYPES:
            return return_pct > 0.3
        elif alert_type in _NEUTRAL_ALERT_TYPES:
            return abs(return_pct) > 0.5
        else:
            # 未知类型: 用绝对涨跌 > 0.5% 判定
            return abs(return_pct) > 0.5

    # ============================================================
    # 每日报告
    # ============================================================

    def get_daily_report(self, date: str = None) -> dict:
        """获取指定日期的预警效果评估报告

        参数:
            date: YYYY-MM-DD，默认今天

        返回:
            {"date": ..., "total_alerts": N, "accuracy": X%,
             "by_type": {...}, "best_type": ..., "worst_type": ...}
        """
        if date is None:
            date = datetime.date.today().isoformat()

        # 筛选当日记录
        today_records = [
            r for r in self.records
            if r.get("alert_time", "").startswith(date)
        ]

        if not today_records:
            return {"date": date, "total_alerts": 0, "message": "当日无预警记录"}

        # 按类型分组统计
        by_type = {}
        total_effective = 0
        total_verdict = 0

        for r in today_records:
            atype = r["type"]
            if atype not in by_type:
                by_type[atype] = {"total": 0, "effective": 0, "false_positive": 0,
                                  "avg_return_30m": 0, "returns": []}
            by_type[atype]["total"] += 1

            verdict = r.get("final_verdict")
            if verdict == "effective":
                by_type[atype]["effective"] += 1
                total_effective += 1
                total_verdict += 1
            elif verdict == "false_positive":
                by_type[atype]["false_positive"] += 1
                total_verdict += 1

            # 30分钟收益汇总
            ret_30 = r.get("verified_windows", {}).get("30", {}).get("return_pct")
            if ret_30 is not None:
                by_type[atype]["returns"].append(ret_30)

        # 计算平均收益
        for atype, stats in by_type.items():
            rets = stats.pop("returns")
            if rets:
                stats["avg_return_30m"] = round(sum(rets) / len(rets), 3)
            acc = (stats["effective"] / (stats["effective"] + stats["false_positive"])
                   if (stats["effective"] + stats["false_positive"]) > 0 else None)
            stats["accuracy"] = round(acc, 3) if acc is not None else None

        # 最佳/最差类型（至少2条样本）
        qualified = {
            t: s for t, s in by_type.items()
            if s["accuracy"] is not None and (s["effective"] + s["false_positive"]) >= 2
        }
        best_type = max(qualified, key=lambda t: qualified[t]["accuracy"]) if qualified else None
        worst_type = min(qualified, key=lambda t: qualified[t]["accuracy"]) if qualified else None

        overall_accuracy = (
            round(total_effective / total_verdict, 3)
            if total_verdict > 0 else None
        )

        return {
            "date": date,
            "total_alerts": len(today_records),
            "verified": total_verdict,
            "effective": total_effective,
            "false_positive": total_verdict - total_effective,
            "overall_accuracy": overall_accuracy,
            "by_type": by_type,
            "best_type": best_type,
            "worst_type": worst_type,
        }

    def get_summary_text(self) -> str:
        """生成文字摘要（供日志/邮件使用）"""
        report = self.get_daily_report()
        if report.get("total_alerts", 0) == 0:
            return "[预警评估] 当日无预警记录"

        lines = [
            f"📊 预警效果评估 ({report['date']})",
            f"   总预警: {report['total_alerts']}条 | "
            f"已验证: {report.get('verified', 0)}条",
        ]

        acc = report.get("overall_accuracy")
        if acc is not None:
            lines.append(
                f"   总体有效率: {acc:.1%} "
                f"({report['effective']}/{report.get('verified', 0)})"
            )
        else:
            lines.append("   总体有效率: 待验证")

        by_type = report.get("by_type", {})
        if by_type:
            lines.append("   分类型:")
            for atype, stats in sorted(
                by_type.items(), key=lambda x: x[1]["total"], reverse=True
            ):
                acc_str = (
                    f"{stats['accuracy']:.0%}" if stats["accuracy"] is not None
                    else "待验证"
                )
                lines.append(
                    f"     • {atype}: {acc_str} "
                    f"(有效{stats['effective']}/误报{stats['false_positive']}, "
                    f"30min均涨跌{stats['avg_return_30m']:+.2f}%)"
                )

        if report.get("best_type"):
            lines.append(f"   最佳类型: {report['best_type']}")
        if report.get("worst_type"):
            lines.append(f"   最差类型: {report['worst_type']}")

        return "\n".join(lines)