"""
金字塔加仓机制
==============
趋势延续中系统性加仓，让利润奔跑（海龟法则核心）

核心规则:
  - 浮盈 > 5% 且趋势加速 → 加仓第3批（20%）
  - 浮盈 > 10% 且板块共振 → 加仓第4批（10%）
  - 每次加仓后止损上移至新成本价（保本止损）
  - 最多加仓3次，总仓位不超上限
  - 加仓量递减（金字塔形）：40% → 30% → 20% → 10%

与现有分批建仓的关系:
  现有: 第一批40% + 第二批60%（浮盈3%确认）
  新增: 第三批20%（浮盈5%+加速）+ 第四批10%（浮盈10%+共振）
  
  总仓位 = 40% + 60% + 20% + 10% = 130%（相对初始计划）
  但受个股仓位上限约束（龙头15%/弹性8%）

使用方式:
    from position.pyramid import PyramidManager
    pm = PyramidManager()
    result = pm.check_pyramid_add(code, holding, df, sector_info)
"""

import pandas as pd
import numpy as np
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


# 金字塔加仓配置
PYRAMID_LEVELS = [
    # (最低浮盈, 加仓比例, 需要趋势加速, 需要板块共振, 描述)
    (0.05, 0.20, True,  False, "第3批: 浮盈5%+趋势加速"),
    (0.10, 0.10, False, True,  "第4批: 浮盈10%+板块共振"),
    (0.20, 0.05, True,  True,  "第5批: 浮盈20%+加速+共振（极端强势）"),
]

MAX_PYRAMID_ADDS = 3  # 最多加仓3次


class PyramidManager:
    """金字塔加仓管理器"""

    def __init__(self, levels: list = None, max_adds: int = MAX_PYRAMID_ADDS):
        self.levels = levels or PYRAMID_LEVELS
        self.max_adds = max_adds

    def check_pyramid_add(self, code: str, holding: dict, df: pd.DataFrame,
                          sector_info: dict = None, market_info: dict = None) -> dict:
        """
        检查是否满足金字塔加仓条件
        
        参数:
            code: 股票代码
            holding: 持仓信息 {shares, buy_price, current_price, pyramid_adds, ...}
            df: 日线数据
            sector_info: 板块信息
            market_info: 大盘状态
        
        返回:
            {
                "can_add": bool,
                "level": int,              # 加仓层级 (0=不加)
                "add_shares": int,         # 建议加仓股数
                "add_amount": float,       # 加仓金额
                "new_stop_loss": float,    # 加仓后新止损
                "reason": str,
                "conditions_met": list,    # 已满足条件
                "conditions_missing": list, # 未满足条件
            }
        """
        if sector_info is None:
            sector_info = {}
        if market_info is None:
            market_info = {}

        buy_price = holding.get("buy_price", 0)
        current_price = holding.get("current_price", buy_price)
        shares = holding.get("shares", 0)
        pyramid_adds = holding.get("pyramid_adds", 0)  # 已加仓次数

        if buy_price <= 0 or current_price <= 0 or shares <= 0:
            return self._no_add("持仓数据无效")

        profit_pct = (current_price - buy_price) / buy_price

        # 已达最大加仓次数
        if pyramid_adds >= self.max_adds:
            return self._no_add(f"已达最大加仓次数({self.max_adds}次)")

        # 大盘弱势不加仓
        if market_info.get("market_state") == "down":
            return self._no_add("大盘弱势，禁止金字塔加仓")

        # 检查各层级条件
        conditions_met = []
        conditions_missing = []
        target_level = None

        for i, (min_profit, add_ratio, need_accel, need_sector, desc) in enumerate(self.levels):
            if i < pyramid_adds:
                continue  # 已经过的层级

            # 浮盈条件
            if profit_pct < min_profit:
                conditions_missing.append(f"浮盈{profit_pct:.1%} < {min_profit:.0%}")
                break  # 后续层级更高，不用检查

            conditions_met.append(f"浮盈{profit_pct:.1%} >= {min_profit:.0%}")

            # 趋势加速条件
            if need_accel:
                is_accel = self._check_acceleration(df)
                if is_accel:
                    conditions_met.append("趋势加速确认")
                else:
                    conditions_missing.append("趋势未加速（近3日涨幅 <= 前3日）")
                    break

            # 板块共振条件
            if need_sector:
                sector_strong = sector_info.get("score", 50) >= 60
                if sector_strong:
                    conditions_met.append(f"板块强势(评分{sector_info.get('score', 0)})")
                else:
                    conditions_missing.append(f"板块不够强(评分{sector_info.get('score', 0)}<60)")
                    break

            target_level = i + 1
            target_ratio = add_ratio
            target_desc = desc
            break

        if target_level is None:
            return self._no_add(
                "未满足加仓条件",
                conditions_met=conditions_met,
                conditions_missing=conditions_missing
            )

        # 计算加仓股数
        stock_info = config.get_stock_info(code)
        stock_type = stock_info.get("类型", "龙头")
        max_ratio = config.LEADER_STOCK_MAX_RATIO if stock_type == "龙头" else config.FLEXIBLE_STOCK_MAX_RATIO

        # 基于初始计划仓位计算加仓量
        initial_amount = shares * buy_price  # 近似初始仓位
        add_amount = initial_amount * target_ratio
        add_shares = int(add_amount / current_price / 100) * 100

        # 检查加仓后是否超过仓位上限
        current_amount = shares * current_price
        new_total = current_amount + add_shares * current_price
        if new_total / config.TOTAL_CAPITAL > max_ratio:
            # 缩减到上限
            max_add_amount = config.TOTAL_CAPITAL * max_ratio - current_amount
            add_shares = int(max_add_amount / current_price / 100) * 100
            add_amount = add_shares * current_price
            if add_shares <= 0:
                return self._no_add(f"加仓后超过仓位上限{max_ratio:.0%}")

        # 加仓后新止损（保本止损：新成本价）
        new_total_shares = shares + add_shares
        new_cost = (shares * buy_price + add_shares * current_price) / new_total_shares
        new_stop_loss = round(new_cost * 1.01, 3)  # 成本+1%（保本+手续费）

        return {
            "can_add": True,
            "level": target_level,
            "add_shares": add_shares,
            "add_amount": round(add_amount, 2),
            "add_price": round(current_price, 3),
            "new_stop_loss": new_stop_loss,
            "new_total_shares": new_total_shares,
            "new_cost": round(new_cost, 3),
            "reason": f"★金字塔加仓{target_desc} | 加{add_shares}股@{current_price:.2f}",
            "conditions_met": conditions_met,
            "conditions_missing": conditions_missing,
            "profit_pct": round(profit_pct, 4),
        }

    def generate_pyramid_order(self, code: str, pyramid_result: dict,
                               holding: dict) -> dict:
        """
        生成金字塔加仓条件单
        
        参数:
            code: 股票代码
            pyramid_result: check_pyramid_add()的返回值
            holding: 持仓信息
        
        返回:
            条件单字典
        """
        if not pyramid_result.get("can_add"):
            return None

        stock_info = config.get_stock_info(code)
        name = stock_info.get("名称", code)
        add_price = pyramid_result["add_price"]
        add_shares = pyramid_result["add_shares"]

        return {
            "order_type": "buy_rebound",
            "order_type_cn": f"金字塔加仓（第{pyramid_result['level']}批）",
            "code": code,
            "name": name,
            "trigger_price": round(add_price * 1.005, 3),  # 略高于现价确认
            "order_price": round(add_price * 1.01, 3),
            "shares": add_shares,
            "condition_desc": (
                f"股价站稳 {add_price * 1.005:.3f} 后，"
                f"买入 {add_shares} 股（金字塔第{pyramid_result['level']}批）"
            ),
            "priority": 3,
            "notes": (
                f"金字塔加仓 | 浮盈{pyramid_result['profit_pct']:.1%} | "
                f"加仓后止损上移至{pyramid_result['new_stop_loss']:.3f} | "
                f"新成本{pyramid_result['new_cost']:.3f}"
            ),
            "category": "pyramid_add",
            "new_stop_loss": pyramid_result["new_stop_loss"],
        }

    # ============================================================
    # 内部方法
    # ============================================================

    def _check_acceleration(self, df: pd.DataFrame) -> bool:
        """
        检查趋势加速
        条件：近3日平均涨幅 > 前3日平均涨幅（动量加速）
        """
        if df is None or len(df) < 7:
            return False

        closes = df["close"].tail(7).values
        # 近3日涨幅
        recent_3 = (closes[-1] / closes[-4] - 1) / 3
        # 前3日涨幅
        prev_3 = (closes[-4] / closes[-7] - 1) / 3

        return recent_3 > prev_3 and recent_3 > 0

    def _no_add(self, reason: str, conditions_met: list = None,
                conditions_missing: list = None) -> dict:
        """不加仓的返回"""
        return {
            "can_add": False,
            "level": 0,
            "add_shares": 0,
            "add_amount": 0,
            "add_price": 0,
            "new_stop_loss": 0,
            "reason": reason,
            "conditions_met": conditions_met or [],
            "conditions_missing": conditions_missing or [],
        }
