"""
P1 风控管理模块
================
个股级风控 + 仓位约束 + 大盘择时 + 动态滑点

功能:
1. 个股级风控: 固定比例止损 + ATR动态止损 + 移动止盈（只升不降）
2. 仓位分散约束: 单票仓位上限15% + 单行业上限30%
3. 大盘择时: 牛熊震荡三态识别 -> 动态总仓位(100%/60%/30%)
4. 动态滑点: 按个股流动性分级（高/中/低流动性对应不同滑点）

使用方式:
    from quant.risk_manager import RiskManager
    rm = RiskManager()
    # 每日检查止损
    sells = rm.check_stop_loss(positions, date_index, date)
    # 大盘择时
    max_pos = rm.get_market_position_limit(benchmark_data, date)
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class RiskManager:
    """
    P1 风控管理器

    止损规则（按优先级）:
    1. 固定止损: 亏损超过固定比例（默认-10%）
    2. ATR动态止损: 买入价 - N*ATR（适应波动率）
    3. 移动止盈: 从最高价回落超过阈值（只升不降）

    仓位约束:
    - 单票仓位上限: 15%（默认）
    - 最大持仓数: 10（由engine控制）

    大盘择时:
    - 牛市（MA20>MA60且价格在MA20上方）: 满仓100%
    - 震荡（MA20>MA60但价格<MA20，或MA20<MA60但价格>MA20）: 60%
    - 熊市（MA20<MA60且价格<MA20）: 30%
    """

    def __init__(self,
                 fixed_stop_pct: float = 0.10,
                 atr_multiplier: float = 2.0,
                 atr_period: int = 14,
                 trailing_stop_pct: float = 0.08,
                 max_single_position: float = 0.15,
                 use_market_timing: bool = True,
                 dynamic_slippage: bool = True):
        """
        参数:
            fixed_stop_pct: 固定止损比例（亏损10%止损）
            atr_multiplier: ATR止损倍数（买入价 - 2*ATR）
            atr_period: ATR计算周期
            trailing_stop_pct: 移动止盈回落比例（从高点回落8%止盈）
            max_single_position: 单票最大仓位比例
            use_market_timing: 是否启用大盘择时
            dynamic_slippage: 是否启用动态滑点
        """
        self.fixed_stop_pct = fixed_stop_pct
        self.atr_multiplier = atr_multiplier
        self.atr_period = atr_period
        self.trailing_stop_pct = trailing_stop_pct
        self.max_single_position = max_single_position
        self.use_market_timing = use_market_timing
        self.dynamic_slippage = dynamic_slippage

        # 移动止损记录 {code: highest_price_since_buy}
        self.trailing_highs = {}

    def reset(self):
        """重置状态"""
        self.trailing_highs = {}

    # ============================================================
    # 一、个股级风控（每日检查）
    # ============================================================

    def check_stop_loss(self, positions: dict, date_index: dict,
                        date: str, data_dict: dict = None) -> list:
        """
        每日盘后检查所有持仓的止损条件

        参数:
            positions: 当前持仓 {code: {shares, buy_price, buy_date, cost}}
            date_index: {code: {date: row}}
            date: 当前日期
            data_dict: 完整数据（用于计算ATR）

        返回:
            需要卖出的 [(code, reason), ...]
        """
        sells = []

        for code, pos in positions.items():
            code_data = date_index.get(code, {})
            row = code_data.get(date)
            if row is None:
                continue

            close = row["close"]
            buy_price = pos["buy_price"]
            pnl_pct = (close - buy_price) / buy_price

            # 更新移动止盈高点
            if code not in self.trailing_highs:
                self.trailing_highs[code] = close
            else:
                self.trailing_highs[code] = max(self.trailing_highs[code], close)

            highest = self.trailing_highs[code]

            # 规则1: 固定止损（亏损超过阈值）
            if pnl_pct <= -self.fixed_stop_pct:
                sells.append((code, f"固定止损({pnl_pct:.1%})"))
                continue

            # 规则2: ATR动态止损
            atr_stop = self._calc_atr_stop(code, buy_price, data_dict, date)
            if atr_stop > 0 and close <= atr_stop:
                sells.append((code, f"ATR止损(价格{close:.2f}<=止损{atr_stop:.2f})"))
                continue

            # 规则3: 移动止盈（从高点回落超过阈值，且当前有浮盈）
            if highest > buy_price:  # 有浮盈才启动移动止盈
                drawdown_from_high = (highest - close) / highest
                if drawdown_from_high >= self.trailing_stop_pct:
                    sells.append((code, f"移动止盈(从{highest:.2f}回落{drawdown_from_high:.1%})"))
                    continue

        return sells

    def _calc_atr_stop(self, code: str, buy_price: float,
                       data_dict: dict, date: str) -> float:
        """计算ATR动态止损价"""
        if data_dict is None:
            return 0

        df = data_dict.get(code)
        if df is None:
            return 0

        # 截取到当前日期的数据
        df_cut = df[df["date"] <= date]
        if len(df_cut) < self.atr_period + 1:
            return 0

        # 计算ATR
        recent = df_cut.tail(self.atr_period + 1)
        highs = recent["high"].values
        lows = recent["low"].values
        closes = recent["close"].values

        tr_list = []
        for i in range(1, len(recent)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            tr_list.append(tr)

        atr = np.mean(tr_list) if tr_list else 0
        if atr <= 0:
            return 0

        # ATR止损价 = 买入价 - N*ATR
        atr_stop = buy_price - self.atr_multiplier * atr
        return max(atr_stop, buy_price * 0.80)  # 不低于固定止损

    # ============================================================
    # 二、仓位约束
    # ============================================================

    def calc_position_size(self, code: str, price: float,
                           total_value: float, current_positions: dict) -> int:
        """
        计算建仓股数（受仓位约束限制）

        参数:
            code: 股票代码
            price: 买入价格
            total_value: 当前总资产
            current_positions: 当前持仓

        返回:
            建议买入股数（100的整数倍）
        """
        # 单票最大金额
        max_amount = total_value * self.max_single_position

        # 等权分配（不超过单票上限）
        equal_amount = total_value / 10  # 假设10只持仓
        target_amount = min(max_amount, equal_amount)

        shares = int(target_amount / price)
        shares = (shares // 100) * 100
        return max(shares, 0)

    # ============================================================
    # 三、大盘择时
    # ============================================================

    def get_market_position_limit(self, benchmark_data: dict,
                                   date: str) -> float:
        """
        大盘择时：根据基准指数判断牛熊震荡，返回建议最大仓位

        参数:
            benchmark_data: 基准指数数据 {date: row} 或 DataFrame
            date: 当前日期

        返回:
            最大仓位比例 (0.3 ~ 1.0)
        """
        if not self.use_market_timing:
            return 1.0

        if benchmark_data is None:
            return 0.7  # 无数据时保守

        # 获取基准指数的MA20和MA60
        if isinstance(benchmark_data, dict):
            # date_index格式
            dates_before = sorted([d for d in benchmark_data.keys() if d <= date])
            if len(dates_before) < 60:
                return 0.7

            closes = [benchmark_data[d]["close"] for d in dates_before[-60:]]
        elif isinstance(benchmark_data, pd.DataFrame):
            df_cut = benchmark_data[benchmark_data["date"] <= date]
            if len(df_cut) < 60:
                return 0.7
            closes = df_cut["close"].values[-60:]
        else:
            return 0.7

        current_price = closes[-1]
        ma20 = np.mean(closes[-20:])
        ma60 = np.mean(closes[-60:])

        # 三态判断
        if ma20 > ma60 and current_price > ma20:
            # 牛市：满仓
            return 1.0
        elif ma20 < ma60 and current_price < ma20:
            # 熊市：低仓位
            return 0.3
        else:
            # 震荡：中等仓位
            return 0.6

    # ============================================================
    # 四、动态滑点
    # ============================================================

    def get_slippage(self, code: str, date_index: dict, date: str,
                     base_slippage: float = 0.001) -> float:
        """
        按流动性分级返回动态滑点

        高流动性（日均成交额>5亿）: 0.05%
        中流动性（1-5亿）: 0.1%
        低流动性（<1亿）: 0.2%

        参数:
            code: 股票代码
            date_index: 日期索引
            date: 当前日期
            base_slippage: 基础滑点

        返回:
            实际滑点比例
        """
        if not self.dynamic_slippage:
            return base_slippage

        code_data = date_index.get(code, {})
        # 取近5天的成交额
        dates_before = sorted([d for d in code_data.keys() if d <= date])[-5:]
        if not dates_before:
            return base_slippage * 2  # 无数据用高滑点

        amounts = []
        for d in dates_before:
            row = code_data[d]
            amt = row.get("amount", 0)
            if amt and amt > 0:
                amounts.append(amt)

        if not amounts:
            return base_slippage * 2

        avg_amount = np.mean(amounts)

        # 分级
        if avg_amount > 5e8:      # >5亿：高流动性
            return base_slippage * 0.5
        elif avg_amount > 1e8:    # 1-5亿：中流动性
            return base_slippage
        else:                     # <1亿：低流动性
            return base_slippage * 2

    # ============================================================
    # 五、综合风控检查（供engine调用）
    # ============================================================

    def daily_risk_check(self, positions: dict, date_index: dict,
                         date: str, data_dict: dict = None,
                         benchmark_data=None) -> dict:
        """
        每日综合风控检查

        返回:
            {
                "stop_sells": [(code, reason), ...],  # 需要止损的
                "position_limit": float,              # 当前仓位上限
                "market_state": str,                  # 市场状态
            }
        """
        # 1. 止损检查
        stop_sells = self.check_stop_loss(positions, date_index, date, data_dict)

        # 2. 大盘择时
        position_limit = self.get_market_position_limit(benchmark_data, date)

        # 3. 市场状态描述
        if position_limit >= 0.9:
            market_state = "牛市"
        elif position_limit >= 0.5:
            market_state = "震荡"
        else:
            market_state = "熊市"

        return {
            "stop_sells": stop_sells,
            "position_limit": position_limit,
            "market_state": market_state,
        }

    # ============================================================
    # 六、交易纪律约束（P0 行为纠偏）
    # ============================================================

    def check_trade_frequency(self, today_trades: int,
                              max_daily: int = 5) -> dict:
        """
        单日最大交易笔数限制

        参数:
            today_trades: 今日已成交笔数
            max_daily: 每日最大允许笔数（默认5笔）

        返回:
            {
                "allowed": bool,       # 是否还允许交易
                "remaining": int,      # 剩余可用笔数
                "today_trades": int,   # 今日已交易
                "max_daily": int,      # 上限
                "level": str,          # 警告级别: normal/warning/danger
            }
        """
        remaining = max(0, max_daily - today_trades)
        if today_trades >= max_daily:
            level = "danger"
        elif today_trades >= max_daily * 0.6:
            level = "warning"
        else:
            level = "normal"

        return {
            "allowed": today_trades < max_daily,
            "remaining": remaining,
            "today_trades": today_trades,
            "max_daily": max_daily,
            "level": level,
        }

    def check_cooldown(self, code: str, last_sell_date: str,
                       current_date: str, cooldown_days: int = 3) -> dict:
        """
        同标的冷却期检查（卖出后N个自然日内禁止再买入）

        参数:
            code: 股票代码
            last_sell_date: 上次卖出日期 (YYYY-MM-DD)
            current_date: 当前日期 (YYYY-MM-DD)
            cooldown_days: 冷却天数（默认3天）

        返回:
            {
                "in_cooldown": bool,    # 是否在冷却期内
                "remaining_days": int,  # 剩余冷却天数
                "code": str,
            }
        """
        try:
            from datetime import datetime
            sell_dt = datetime.strptime(last_sell_date, "%Y-%m-%d")
            cur_dt = datetime.strptime(current_date, "%Y-%m-%d")
            elapsed = (cur_dt - sell_dt).days
            in_cooldown = elapsed < cooldown_days
            return {
                "in_cooldown": in_cooldown,
                "remaining_days": max(0, cooldown_days - elapsed),
                "code": code,
            }
        except Exception:
            return {"in_cooldown": False, "remaining_days": 0, "code": code}

    def check_min_holding_days(self, buy_date: str, current_date: str,
                               min_days: int = 3,
                               is_stop_loss: bool = False) -> dict:
        """
        最小持仓天数检查（未达标禁止主动卖出，止损除外）

        参数:
            buy_date: 买入日期 (YYYY-MM-DD)
            current_date: 当前日期
            min_days: 最小持仓天数（默认3天）
            is_stop_loss: 是否为止损卖出（止损不受限制）

        返回:
            {
                "can_sell": bool,        # 是否允许卖出
                "holding_days": int,     # 已持仓天数
                "min_days": int,         # 最小要求
                "reason": str,           # 说明
            }
        """
        # 止损不受最小持仓限制
        if is_stop_loss:
            return {"can_sell": True, "holding_days": 0,
                    "min_days": min_days, "reason": "止损不受限"}

        try:
            from datetime import datetime
            buy_dt = datetime.strptime(buy_date, "%Y-%m-%d")
            cur_dt = datetime.strptime(current_date, "%Y-%m-%d")
            holding_days = (cur_dt - buy_dt).days
            can_sell = holding_days >= min_days
            reason = (f"持仓{holding_days}天，未达最小{min_days}天" if not can_sell
                      else f"持仓{holding_days}天，满足最小持仓要求")
            return {
                "can_sell": can_sell,
                "holding_days": holding_days,
                "min_days": min_days,
                "reason": reason,
            }
        except Exception:
            return {"can_sell": True, "holding_days": 0,
                    "min_days": min_days, "reason": "日期解析失败"}

    def check_sector_concentration(self, positions: dict,
                                    sector_map: dict = None,
                                    max_sector_pct: float = 0.30) -> dict:
        """
        行业分散度检查（单行业持仓不超过总资产的30%）

        参数:
            positions: {code: {shares, buy_price/current_price, sector}}
            sector_map: {code: sector_name} 备选行业映射
            max_sector_pct: 单行业最大占比（默认30%）

        返回:
            {
                "pass": bool,              # 是否通过
                "sector_exposure": dict,   # {sector: pct}
                "violations": [str],       # 超限行业列表
                "max_sector": str,         # 占比最高的行业
                "max_pct": float,          # 最高占比
            }
        """
        if sector_map is None:
            sector_map = {}

        # 计算各行业市值
        sector_values = {}
        total_value = 0

        for code, pos in positions.items():
            price = pos.get("current_price", pos.get("buy_price", 0))
            shares = pos.get("shares", 0)
            value = price * shares
            total_value += value

            sector = pos.get("sector", sector_map.get(code, "未知"))
            sector_values[sector] = sector_values.get(sector, 0) + value

        if total_value <= 0:
            return {"pass": True, "sector_exposure": {},
                    "violations": [], "max_sector": "", "max_pct": 0}

        # 计算占比
        sector_pct = {s: v / total_value for s, v in sector_values.items()}
        violations = [f"{s}({p:.0%})" for s, p in sector_pct.items()
                      if p > max_sector_pct]

        max_sector = max(sector_pct, key=sector_pct.get) if sector_pct else ""
        max_pct = sector_pct.get(max_sector, 0)

        return {
            "pass": len(violations) == 0,
            "sector_exposure": sector_pct,
            "violations": violations,
            "max_sector": max_sector,
            "max_pct": max_pct,
        }

    def get_discipline_report(self, today_trades: int,
                              positions: dict,
                              trade_history: list = None,
                              current_date: str = None,
                              sector_map: dict = None) -> dict:
        """
        综合交易纪律报告（供日报/晨报调用）

        参数:
            today_trades: 今日已交易笔数
            positions: 当前持仓
            trade_history: 近期交易记录 [{code, direction, date, ...}]
            current_date: 当前日期
            sector_map: 行业映射

        返回:
            完整纪律检查结果
        """
        import datetime as dt
        if current_date is None:
            current_date = dt.date.today().strftime("%Y-%m-%d")

        report = {}

        # 1. 交易频率
        report["frequency"] = self.check_trade_frequency(today_trades)

        # 2. 行业集中度
        report["sector"] = self.check_sector_concentration(
            positions, sector_map)

        # 3. 冷却期检查（基于trade_history）
        cooldown_list = []
        if trade_history:
            sell_records = [t for t in trade_history
                           if t.get("direction") == "sell"]
            for t in sell_records:
                cd = self.check_cooldown(
                    t.get("code", ""), t.get("date", ""),
                    current_date)
                if cd["in_cooldown"]:
                    cooldown_list.append(cd)
        report["cooldown_list"] = cooldown_list

        # 4. 最小持仓检查
        min_hold_alerts = []
        for code, pos in positions.items():
            buy_date = pos.get("buy_date", "")
            if buy_date:
                chk = self.check_min_holding_days(buy_date, current_date)
                if not chk["can_sell"]:
                    chk["code"] = code
                    min_hold_alerts.append(chk)
        report["min_hold_alerts"] = min_hold_alerts

        return report
