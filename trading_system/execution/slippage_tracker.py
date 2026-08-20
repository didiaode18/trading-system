"""
执行质量追踪模块（滑点分析）
============================
记录每笔实际成交价 vs 信号价，统计滑点分布，校准回测参数

核心功能:
  1. 滑点记录：实际成交价 vs 信号触发价
  2. 滑点统计：平均/中位数/最大/分布
  3. 回测校准：如果实际滑点 > 回测假设 → 调整参数
  4. 流动性评估：识别高滑点股票 → 降仓或排除
  5. 执行时机分析：开盘/盘中/尾盘哪个时段滑点最小

使用方式:
    from execution.slippage_tracker import SlippageTracker
    tracker = SlippageTracker()
    tracker.record(code, signal_price, actual_price, shares, time)
    report = tracker.generate_report()
"""

import pandas as pd
import numpy as np
import logging
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


class SlippageTracker:
    """执行质量追踪器"""

    # 默认持久化路径：data/slippage_history.json
    DEFAULT_HISTORY_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "slippage_history.json"
    )

    def __init__(self, data_file: str = None, history_file: str = None):
        self.data_file = data_file or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "output", "execution_log.json"
        )
        self.history_file = history_file or self.DEFAULT_HISTORY_FILE
        # 从 history 文件加载（主要持久化）
        self.records = self.load(self.history_file)
        # 兼容旧 execution_log.json
        if not self.records:
            self.records = self._load()

    def record(self, code: str, signal_price: float, actual_price: float,
               shares: int, direction: str = "buy",
               exec_time: str = None, order_type: str = ""):
        """
        记录一笔执行
        
        参数:
            code: 股票代码
            signal_price: 信号触发价（条件单价格）
            actual_price: 实际成交价
            shares: 成交股数
            direction: "buy" / "sell"
            exec_time: 执行时间 (HH:MM)
            order_type: 条件单类型
        """
        if signal_price <= 0 or actual_price <= 0:
            return

        # 滑点计算（买入：实际>信号为正滑点；卖出：实际<信号为正滑点）
        if direction == "buy":
            slippage_pct = (actual_price - signal_price) / signal_price
        else:
            slippage_pct = (signal_price - actual_price) / signal_price

        record = {
            "code": code,
            "name": config.get_stock_name(code),
            "date": datetime.date.today().isoformat(),
            "time": exec_time or datetime.datetime.now().strftime("%H:%M"),
            "direction": direction,
            "signal_price": round(signal_price, 4),
            "actual_price": round(actual_price, 4),
            "shares": shares,
            "amount": round(shares * actual_price, 2),
            "slippage_pct": round(slippage_pct, 6),
            "slippage_amount": round(abs(actual_price - signal_price) * shares, 2),
            "order_type": order_type,
        }

        self.records.append(record)
        # 保留最近500条
        self.records = self.records[-500:]
        self._save()
        self.save(self.history_file)  # 同时持久化到 history 文件

        logger.info(f"  [执行记录] {code} {direction} {shares}股 | "
                    f"信号{signal_price:.3f} → 实际{actual_price:.3f} | "
                    f"滑点{slippage_pct:.3%}")

    def generate_report(self, lookback_days: int = 30) -> dict:
        """
        生成执行质量报告
        
        返回:
            {
                "total_trades": int,
                "avg_slippage": float,
                "median_slippage": float,
                "max_slippage": float,
                "slippage_cost": float,      # 总滑点成本
                "by_stock": dict,            # 各股票滑点
                "by_time": dict,             # 各时段滑点
                "by_direction": dict,        # 买/卖滑点
                "backtest_calibration": dict, # 回测校准建议
                "high_slippage_stocks": list, # 高滑点股票
            }
        """
        # 过滤时间范围
        cutoff = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
        recent = [r for r in self.records if r.get("date", "") >= cutoff and r.get("type") != "config_change"]

        if not recent:
            return {"total_trades": 0, "message": "无执行记录"}

        slippages = [r["slippage_pct"] for r in recent]
        costs = [r["slippage_amount"] for r in recent]

        # 基础统计
        report = {
            "total_trades": len(recent),
            "avg_slippage": round(np.mean(slippages), 6),
            "median_slippage": round(np.median(slippages), 6),
            "max_slippage": round(max(slippages), 6),
            "min_slippage": round(min(slippages), 6),
            "slippage_cost": round(sum(costs), 2),
            "pct_positive": round(sum(1 for s in slippages if s > 0) / len(slippages), 3),
        }

        # 按股票分组
        by_stock = {}
        for r in recent:
            code = r["code"]
            if code not in by_stock:
                by_stock[code] = {"slippages": [], "costs": [], "count": 0}
            by_stock[code]["slippages"].append(r["slippage_pct"])
            by_stock[code]["costs"].append(r["slippage_amount"])
            by_stock[code]["count"] += 1

        report["by_stock"] = {
            code: {
                "avg": round(np.mean(d["slippages"]), 6),
                "max": round(max(d["slippages"]), 6),
                "cost": round(sum(d["costs"]), 2),
                "count": d["count"],
            }
            for code, d in by_stock.items()
        }

        # 按时段分组
        by_time = {"open": [], "mid": [], "close": []}
        for r in recent:
            t = r.get("time", "12:00")
            if t < "10:00":
                by_time["open"].append(r["slippage_pct"])
            elif t < "14:00":
                by_time["mid"].append(r["slippage_pct"])
            else:
                by_time["close"].append(r["slippage_pct"])

        report["by_time"] = {
            k: {"avg": round(np.mean(v), 6) if v else 0, "count": len(v)}
            for k, v in by_time.items()
        }

        # 按方向
        buys = [r["slippage_pct"] for r in recent if r["direction"] == "buy"]
        sells = [r["slippage_pct"] for r in recent if r["direction"] == "sell"]
        report["by_direction"] = {
            "buy": {"avg": round(np.mean(buys), 6) if buys else 0, "count": len(buys)},
            "sell": {"avg": round(np.mean(sells), 6) if sells else 0, "count": len(sells)},
        }

        # 高滑点股票
        high_slip = [
            {"code": code, "avg_slippage": d["avg"], "count": d["count"]}
            for code, d in report["by_stock"].items()
            if d["avg"] > 0.005 and d["count"] >= 3  # 平均滑点>0.5%且至少3笔
        ]
        high_slip.sort(key=lambda x: x["avg_slippage"], reverse=True)
        report["high_slippage_stocks"] = high_slip[:5]

        # 回测校准建议
        avg_slip = report["avg_slippage"]
        backtest_slip_leader = 0.002  # 回测假设龙头0.2%
        backtest_slip_flex = 0.005    # 回测假设弹性0.5%

        calibration = {}
        if avg_slip > backtest_slip_leader:
            calibration["leader"] = {
                "current_assumption": backtest_slip_leader,
                "actual": avg_slip,
                "suggestion": f"回测滑点假设偏低，建议调整为{avg_slip:.3%}",
            }
        if avg_slip > backtest_slip_flex:
            calibration["flex"] = {
                "current_assumption": backtest_slip_flex,
                "actual": avg_slip,
                "suggestion": f"弹性股滑点超预期，建议调整为{avg_slip * 1.5:.3%}",
            }
        report["backtest_calibration"] = calibration

        return report

    def auto_record_from_signals(self, signals: list, real_prices: dict = None):
        """
        从当日信号中自动提取预期价格，记录滑点

        参数:
            signals: [(code, sig_dict), ...] 当日信号列表
            real_prices: {code: actual_price} 实际成交价格（可选）
                         若不提供，使用信号中的 close 价格作为近似
        """
        real_prices = real_prices or {}
        today_str = datetime.date.today().isoformat()
        recorded = 0

        for code, sig in signals:
            # 只记录有明确买入/卖出/加仓信号的股票
            if not (sig.get("buy_signal") or sig.get("sell_signal") or sig.get("add_position")):
                continue

            signal_price = sig.get("buy_price") or sig.get("sell_price", 0)
            if signal_price <= 0:
                continue

            direction = "buy" if sig.get("buy_signal") else "sell"

            # 实际价格：优先用真实成交价，其次用信号中的 close 价格
            actual_price = real_prices.get(code, 0)
            if actual_price <= 0:
                actual_price = sig.get("close", sig.get("current_price", 0))
            if actual_price <= 0:
                # 无任何价格可用，仅记录预期价格（滑点=0，标记为 pending）
                actual_price = signal_price

            if signal_price <= 0 or actual_price <= 0:
                continue

            if direction == "buy":
                slippage_pct = (actual_price - signal_price) / signal_price
            else:
                slippage_pct = (signal_price - actual_price) / signal_price

            record = {
                "code": code,
                "name": config.get_stock_name(code),
                "date": today_str,
                "time": sig.get("time", "15:00"),
                "direction": direction,
                "signal_price": round(signal_price, 4),
                "actual_price": round(actual_price, 4),
                "shares": sig.get("position", {}).get("shares", 0) if sig.get("position") else 0,
                "amount": round(actual_price * (sig.get("position", {}).get("shares", 0) if sig.get("position") else 0), 2),
                "slippage_pct": round(slippage_pct, 6),
                "slippage_amount": round(abs(actual_price - signal_price) * (sig.get("position", {}).get("shares", 0) if sig.get("position") else 1), 2),
                "order_type": sig.get("order_type", "auto_signal"),
                "source": "auto_signal",
                "has_real_price": code in real_prices,
            }

            self.records.append(record)
            recorded += 1

        # 保留最近500条
        self.records = self.records[-500:]
        if recorded > 0:
            self._save()
            self.save(self.history_file)

        logger.debug(f"滑点追踪: 记录{recorded}笔信号预期价格")
        return recorded

    def generate_monthly_report(self) -> dict:
        """
        生成月度滑点报告（最近30天）

        返回:
            {
                "period": str,
                "total_trades": int,
                "avg_slippage": float,
                "max_slippage": float,
                "std_slippage": float,
                "backtest_assumption": float,
                "comparison": str,
            }
        """
        cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        recent = [r for r in self.records if r.get("date", "") >= cutoff and r.get("type") != "config_change"]

        if not recent:
            return {
                "period": f"{cutoff} ~ {datetime.date.today().isoformat()}",
                "total_trades": 0,
                "message": "最近30天无滑点记录",
            }

        slippages = [r["slippage_pct"] for r in recent]
        avg_slip = float(np.mean(slippages))
        max_slip = float(max(slippages))
        median_slip = float(np.median(slippages))
        std_slip = float(np.std(slippages)) if len(slippages) > 1 else 0.0

        # 统计本月配置变更次数
        config_changes = [
            r for r in self.records
            if r.get("type") == "config_change"
            and r.get("timestamp", "")[:7] == datetime.date.today().strftime("%Y-%m")
        ]

        # 读取回测假设滑点
        backtest_assumption = getattr(config, 'BACKTEST_CONFIG', {}).get(
            'buy_slippage', 0.001
        )

        if avg_slip > backtest_assumption:
            comparison = f"实际滑点({avg_slip:.3%})高于回测假设({backtest_assumption:.3%})"
        elif avg_slip < backtest_assumption * 0.5:
            comparison = f"实际滑点({avg_slip:.3%})低于回测假设({backtest_assumption:.3%})"
        else:
            comparison = f"实际滑点({avg_slip:.3%})与回测假设({backtest_assumption:.3%})基本一致"

        report = {
            "period": f"{cutoff} ~ {datetime.date.today().isoformat()}",
            "total_trades": len(recent),
            "avg_slippage": round(avg_slip, 6),
            "median_slippage": round(median_slip, 6),
            "max_slippage": round(max_slip, 6),
            "std_slippage": round(std_slip, 6),
            "backtest_assumption": backtest_assumption,
            "comparison": comparison,
            "config_change_count": len(config_changes),
            "config_changes": [
                {"param": c["param"], "old": c["old_value"], "new": c["new_value"], "time": c["timestamp"]}
                for c in config_changes
            ],
        }

        logger.info(
            f"月度滑点报告: 记录{len(recent)}笔 | "
            f"平均{avg_slip:.3%}, 中位{median_slip:.3%}, 最大{max_slip:.3%}, 标准差{std_slip:.3%}, "
            f"回测假设{backtest_assumption:.3%}, 本月配置变更{len(config_changes)}次"
        )
        return report

    def suggest_backtest_adjustment(self) -> dict:
        """
        根据实际滑点建议是否调整回测参数

        自动调整机制:
            - 每月1日由 main.py 调用，分析最近30天滑点数据
            - 如果实际平均滑点 > 回测假设 * 1.5，建议调高
            - 如果实际平均滑点 < 回测假设 * 0.5，建议调低
            - 当 should_adjust=True 时，main.py 自动调用 apply_backtest_adjustment()
              将新值写回 config.py 的 buy_slippage 参数

        返回:
            {
                "should_adjust": bool,
                "current_assumption": float,
                "suggested_value": float,
                "reason": str,
            }
        """
        cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        recent = [r for r in self.records if r.get("date", "") >= cutoff and r.get("type") != "config_change"]

        backtest_assumption = getattr(config, 'BACKTEST_CONFIG', {}).get(
            'buy_slippage', 0.001
        )

        if not recent or len(recent) < 3:
            return {
                "should_adjust": False,
                "current_assumption": backtest_assumption,
                "suggested_value": backtest_assumption,
                "reason": "样本不足（<3笔），暂不调整",
            }

        avg_slip = float(np.mean([r["slippage_pct"] for r in recent]))

        if avg_slip > backtest_assumption * 1.5:
            suggested = round(avg_slip, 6)
            logger.info(
                f"滑点校准建议: 当前假设{backtest_assumption:.3%}, "
                f"建议调整为{suggested:.3%}"
            )
            return {
                "should_adjust": True,
                "current_assumption": backtest_assumption,
                "suggested_value": suggested,
                "reason": f"实际平均滑点({avg_slip:.3%})超过回测假设({backtest_assumption:.3%})的1.5倍",
            }
        elif avg_slip < backtest_assumption * 0.5:
            suggested = round(max(avg_slip, 0.0005), 6)  # 不低于0.05%
            logger.info(
                f"滑点校准建议: 当前假设{backtest_assumption:.3%}, "
                f"建议调整为{suggested:.3%}"
            )
            return {
                "should_adjust": True,
                "current_assumption": backtest_assumption,
                "suggested_value": suggested,
                "reason": f"实际平均滑点({avg_slip:.3%})低于回测假设({backtest_assumption:.3%})的0.5倍",
            }
        else:
            return {
                "should_adjust": False,
                "current_assumption": backtest_assumption,
                "suggested_value": backtest_assumption,
                "reason": f"实际平均滑点({avg_slip:.3%})在合理范围内",
            }

    def apply_backtest_adjustment(self, new_value: float) -> bool:
        """
        将滑点参数写回 config.py 文件，并记录变更历史

        自动调整机制:
            - 由 main.py 在 suggest_backtest_adjustment() 返回 should_adjust=True 时调用
            - 使用正则表达式定位并替换 config.py 中的 buy_slippage 参数
            - 同时写入 data/slippage_history.json 记录变更历史（type=config_change）
            - 变更记录包含：参数名、旧值、新值、时间戳、来源
            - 即使 config.py 写入失败，也会记录变更尝试到历史文件

        参数:
            new_value: 新的 buy_slippage 值

        返回:
            True 表示 config.py 写入成功，False 表示失败
        """
        import re
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config.py"
        )

        old_value = getattr(config, 'BACKTEST_CONFIG', {}).get('buy_slippage', 0.001)
        config_ok = False

        # 第一步：写入 config.py
        if not os.path.exists(config_path):
            logger.error(f"config.py 不存在: {config_path}")
        else:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    content = f.read()

                pattern = r'("buy_slippage"\s*:\s*)[\d.]+'
                if not re.search(pattern, content):
                    logger.error("config.py 中未找到 buy_slippage 参数")
                else:
                    new_content = re.sub(pattern, f'\\g<1>{new_value}', content)
                    with open(config_path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    logger.info(f"已将 config.py buy_slippage 更新为 {new_value:.6f}")
                    config_ok = True
            except Exception as e:
                logger.error(f"config.py 写入失败: {e}")

        # 第二步：无论 config.py 是否成功，都记录变更到历史文件
        change_record = {
            "type": "config_change",
            "param": "buy_slippage",
            "old_value": old_value,
            "new_value": new_value,
            "timestamp": datetime.datetime.now().isoformat(),
            "source": "auto_adjustment",
            "config_write_ok": config_ok,
        }
        self.records.append(change_record)
        self.records = self.records[-500:]
        self.save(self.history_file)

        if not config_ok:
            logger.warning("config.py 写入失败，但变更已记录到 slippage_history.json，请手动更新 config.py")

        return config_ok

    def get_stock_slippage(self, code: str) -> float:
        """获取某只股票的历史平均滑点"""
        stock_records = [r for r in self.records if r.get("code") == code and r.get("type") != "config_change"]
        if not stock_records:
            return 0.002  # 默认
        return np.mean([r["slippage_pct"] for r in stock_records])

    def export(self, path: str = None, lookback_days: int = 90) -> str:
        """
        导出滑点历史记录到 CSV 文件（供回测校准使用）

        参数:
            path: CSV 文件路径，默认 data/slippage_export.csv
            lookback_days: 回溯天数，默认90天

        返回:
            导出文件路径
        """
        if path is None:
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", "slippage_export.csv"
            )

        cutoff = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
        recent = [r for r in self.records if r.get("date", "") >= cutoff and r.get("type") != "config_change"]

        if not recent:
            logger.warning("无滑点记录可导出")
            return ""

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            df = pd.DataFrame(recent)
            # 确保列顺序一致
            cols = ["date", "time", "code", "name", "direction", "signal_price",
                    "actual_price", "shares", "amount", "slippage_pct", "slippage_amount",
                    "order_type", "source"]
            existing_cols = [c for c in cols if c in df.columns]
            df = df[existing_cols]
            df.to_csv(path, index=False, encoding="utf-8-sig")
            logger.info(f"滑点历史已导出: {path} ({len(recent)}条记录, 最近{lookback_days}天)")
            return path
        except Exception as e:
            logger.error(f"滑点历史导出失败: {e}")
            return ""

    def save(self, path: str):
        """将滑点历史记录保存到指定JSON文件（原子写，防止进程中断导致文件截断）"""
        try:
            from utils.file_io import atomic_json_write
            if not atomic_json_write(path, self.records):
                logger.warning(f"滑点历史原子写入失败({path})")
        except ImportError:
            # 降级: utils 模块不可用时回退到直接写入
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.records, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"滑点历史保存失败({path}): {e}")

    def load(self, path: str) -> list:
        """从JSON文件恢复滑点历史记录"""
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except Exception as e:
                logger.warning(f"滑点历史加载失败({path}): {e}")
        return []

    def _load(self) -> list:
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"执行日志保存失败: {e}")
