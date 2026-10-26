# -*- coding: utf-8 -*-
"""
ETF→个股换仓风险追踪器 V1.0 (V28 ETF风控增强)
===============================================
追踪ETF清仓后的资金流向，防止分散仓位集中化为单一个股仓位。

核心规则：
1. ETF卖出后7个交易日内，买入个股金额 > ETF释放金额的80% → 触发预警
2. 单笔买入后单股仓位占比 > 30% → 硬否决（不论资金来源）
3. 连续3次ETF减仓 + 同方向个股加仓 → 触发"风险升级"告警

调用方：
  - capital_planner.check_buy_feasibility(): 买入前检查
  - scheduler.py 盘中卖出检测: ETF卖出后记录
"""
import json
import os
import datetime
import logging

logger = logging.getLogger(__name__)

# ===== 配置参数 =====
ETF_PROCEEDS_TRACK_WINDOW = 7       # ETF清仓后追踪窗口（交易日）
ETF_PROCEEDS_CONCENTRATION_THRESHOLD = 0.80  # 个股买入占ETF释放资金比例
MAX_SINGLE_STOCK_AFTER_ETF = 0.30   # ETF清仓后单股仓位硬上限
CONSECUTIVE_ETF_REDUCE_THRESHOLD = 3  # 连续ETF减仓次数阈值

# 状态文件路径（相对于 trading_system/ 目录）
_STATE_FILE = os.path.join("data", "etf_risk_state.json")


def _resolve_state_path() -> str:
    """解析状态文件绝对路径（兼容 scheduler 与独立运行）"""
    # 优先使用本文件相对 trading_system/ 的路径
    _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(_base, _STATE_FILE)


class ETFRiskTracker:
    """ETF换仓风险追踪器

    用法:
        tracker = ETFRiskTracker(total_capital=500000)
        # ETF卖出时记录
        tracker.record_etf_sell("510210", "综指ETF", 80000, 10000)
        # 买入个股前检查
        result = tracker.check_buy_risk("562590", "半导材料", 75000, holdings)
        if result["risk_level"] == "blocked":
            print(f"拦截: {result['reason']}")
    """

    def __init__(self, total_capital: float):
        self.total_capital = total_capital
        self.state = self._load_state()

    def _load_state(self) -> dict:
        """加载持久化状态"""
        path = _resolve_state_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"[ETFRisk] 状态文件读取失败，使用空状态: {e}")
        return {
            "etf_proceeds": [],       # [{code, name, date, amount, shares}]
            "etf_reduce_count": 0,    # 连续ETF减仓次数
            "last_etf_reduce_date": None,
            "alerts_sent": [],        # 已发送的预警记录
        }

    def _save_state(self):
        """持久化状态"""
        path = _resolve_state_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def record_etf_sell(self, code: str, name: str, amount: float, shares: int):
        """
        记录ETF卖出，追踪释放的资金

        触发时机: 任何ETF卖出成交后调用
        """
        today = datetime.date.today().isoformat()
        entry = {
            "code": code,
            "name": name,
            "date": today,
            "amount": amount,
            "shares": shares,
        }
        self.state["etf_proceeds"].append(entry)

        # 更新连续减仓计数
        if self.state["last_etf_reduce_date"]:
            last_date = datetime.date.fromisoformat(self.state["last_etf_reduce_date"])
            days_gap = (datetime.date.today() - last_date).days
            if days_gap <= 5:  # 5个自然日内算连续
                self.state["etf_reduce_count"] += 1
            else:
                self.state["etf_reduce_count"] = 1
        else:
            self.state["etf_reduce_count"] = 1
        self.state["last_etf_reduce_date"] = today

        self._save_state()
        logger.info(f"[ETFRisk] 记录ETF卖出: {code} {name} 金额{amount:,.0f}元")

        # 检查连续减仓告警
        if self.state["etf_reduce_count"] >= CONSECUTIVE_ETF_REDUCE_THRESHOLD:
            self._alert_consecutive_reduce()

    def check_buy_risk(self, buy_code: str, buy_name: str, buy_amount: float,
                       current_holdings: dict) -> dict:
        """
        检查买入操作的风险等级

        Args:
            buy_code: 买入标的代码
            buy_name: 买入标的名称
            buy_amount: 买入金额（元）
            current_holdings: 当前持仓字典 {code: {shares, buy_price, ...}}

        Returns:
            {
                "risk_level": "normal" | "warning" | "critical" | "blocked",
                "reason": str,
                "details": dict,
            }
        """
        try:
            from utils.instrument import is_etf, get_instrument_type, InstrumentType, get_etf_subtype
        except ImportError:
            return {"risk_level": "normal", "reason": "instrument模块不可用，跳过检查"}

        # 买入宽基ETF不触发（宽基本身就是分散持仓）
        _is_target_etf = is_etf(buy_code)
        if _is_target_etf:
            _target_subtype = get_etf_subtype(buy_code)
            if _target_subtype == "宽基ETF":
                return {"risk_level": "normal", "reason": "宽基ETF买入不触发换仓追踪（本身就是分散）"}
            # 行业ETF/主题ETF仍然检查集中度——单行业ETF 68%同样危险

        today = datetime.date.today()
        result = {"risk_level": "normal", "reason": "", "details": {}}

        # ===== 检查1: 单标的仓位硬上限（不论个股/行业ETF/主题ETF） =====
        current_top_value = 0.0
        for h_code, h in current_holdings.items():
            if h_code == buy_code:
                continue  # 排除即将买入的标的（避免重复计算）
            # 宽基ETF视为"分散仓位"，不计入集中度
            if is_etf(h_code) and get_etf_subtype(h_code) == "宽基ETF":
                continue
            mv = h.get("market_value", 0) or h.get("shares", 0) * h.get("buy_price", 0)
            current_top_value += mv

        projected_single_value = current_top_value + buy_amount
        total_capital = self.total_capital if self.total_capital > 0 else 1
        projected_ratio = projected_single_value / total_capital

        if projected_ratio > MAX_SINGLE_STOCK_AFTER_ETF:
            result["risk_level"] = "blocked"
            result["reason"] = (
                f"买入后单标的仓位{projected_ratio:.1%}超过ETF清仓后上限"
                f"{MAX_SINGLE_STOCK_AFTER_ETF:.0%}"
            )
            result["details"]["projected_ratio"] = projected_ratio
            return result

        # ===== 检查2: 资金来源追踪 =====
        recent_proceeds = [
            p for p in self.state["etf_proceeds"]
            if (today - datetime.date.fromisoformat(p["date"])).days
            <= ETF_PROCEEDS_TRACK_WINDOW * 2  # 自然日≈交易日*1.4
        ]
        total_recent_proceeds = sum(p["amount"] for p in recent_proceeds)

        if total_recent_proceeds > 0:
            funding_ratio = buy_amount / total_recent_proceeds
            if funding_ratio >= ETF_PROCEEDS_CONCENTRATION_THRESHOLD:
                result["risk_level"] = "critical"
                result["reason"] = (
                    f"买入金额{buy_amount:,.0f}元占近期ETF释放资金"
                    f"{total_recent_proceeds:,.0f}元的{funding_ratio:.0%}"
                    f"，分散→集中风险升级！"
                )
                result["details"] = {
                    "buy_amount": buy_amount,
                    "etf_proceeds": total_recent_proceeds,
                    "funding_ratio": funding_ratio,
                    "etf_list": [f"{p['code']} {p['name']}" for p in recent_proceeds],
                }
                return result

        # ===== 检查3: 连续减仓+加仓告警 =====
        if self.state["etf_reduce_count"] >= 2:
            result["risk_level"] = "warning"
            result["reason"] = (
                f"近期已连续{self.state['etf_reduce_count']}次ETF减仓，"
                f"现又加仓个股{buy_code} {buy_name}，风险偏好急剧上升"
            )

        return result

    def _alert_consecutive_reduce(self):
        """连续ETF减仓告警"""
        count = self.state["etf_reduce_count"]
        logger.warning(f"[ETFRisk] ⚠️ 连续ETF减仓{count}次，风险偏好上升")

    def cleanup_expired(self):
        """清理过期追踪记录（每日盘前调用一次即可）"""
        today = datetime.date.today()
        before = len(self.state["etf_proceeds"])
        self.state["etf_proceeds"] = [
            p for p in self.state["etf_proceeds"]
            if (today - datetime.date.fromisoformat(p["date"])).days
            <= ETF_PROCEEDS_TRACK_WINDOW * 2
        ]
        after = len(self.state["etf_proceeds"])
        if before != after:
            logger.info(f"[ETFRisk] 清理过期追踪记录: {before} → {after}")
            self._save_state()
