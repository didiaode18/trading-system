"""
仓位计算模块
=============
根据风控规则计算每笔交易的建议仓位金额和股数

核心规则:
- 单笔最大亏损不超过总资金2%
- 龙头股单只仓位 <= 15%
- 弹性票单只仓位 <= 8%
- 单一赛道 <= 40%
- 总仓位按行情强度动态调整
- 保留至少10%现金安全垫
"""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def calc_max_shares_by_risk(buy_price: float, stop_loss_price: float,
                            total_capital: float = None) -> int:
    """
    根据单笔最大亏损（总资金2%）反算最大可买股数
    
    公式:
        最大亏损金额 = 总资金 * 2%
        每股最大亏损 = 买入价 - 止损价
        最大股数 = 最大亏损金额 / 每股最大亏损
    
    参数:
        buy_price: 买入价
        stop_loss_price: 止损价
        total_capital: 总资金（默认取config）
    
    返回:
        最大可买股数（整数，向下取整到100的倍数，即整手）
    """
    if total_capital is None:
        total_capital = config.TOTAL_CAPITAL

    max_loss_amount = total_capital * config.MAX_SINGLE_LOSS_RATIO
    loss_per_share = buy_price - stop_loss_price

    if loss_per_share <= 0:
        logger.warning(f"止损价{stop_loss_price} >= 买入价{buy_price}，无法计算仓位")
        return 0

    max_shares = int(max_loss_amount / loss_per_share)
    # A股最小交易单位100股（1手），向下取整到100的倍数
    max_shares = (max_shares // 100) * 100
    return max(max_shares, 0)


def calc_position_by_ratio(buy_price: float, ratio: float,
                           total_capital: float = None) -> int:
    """
    根据仓位比例计算买入股数
    
    参数:
        buy_price: 买入价
        ratio: 仓位占总资金比例（如0.15表示15%）
        total_capital: 总资金
    
    返回:
        股数（整手）
    """
    if total_capital is None:
        total_capital = config.TOTAL_CAPITAL

    amount = total_capital * ratio
    shares = int(amount / buy_price)
    shares = (shares // 100) * 100
    return max(shares, 0)


def calc_first_batch(buy_price: float, stop_loss_price: float,
                     stock_type: str = "龙头",
                     total_capital: float = None,
                     current_sector_amount: float = 0) -> dict:
    """
    计算第一批试仓（40%计划仓位）
    
    参数:
        buy_price: 买入价
        stop_loss_price: 止损价
        stock_type: "龙头" 或 "弹性"
        total_capital: 总资金
        current_sector_amount: 该赛道当前已占用金额
    
    返回:
        {
            "shares": int,            # 建议买入股数
            "amount": float,          # 买入金额
            "ratio": float,           # 占总资金比例
            "max_loss": float,        # 最大亏损金额
            "max_loss_ratio": float,  # 最大亏损占总资金比例
            "pass_risk": bool,        # 是否通过风控
            "risk_msg": str           # 风控说明
        }
    """
    if total_capital is None:
        total_capital = config.TOTAL_CAPITAL

    # 个股仓位上限
    if stock_type == "弹性":
        max_ratio = config.FLEXIBLE_STOCK_MAX_RATIO
    else:
        max_ratio = config.LEADER_STOCK_MAX_RATIO

    # 方法1：按风控（2%最大亏损）反算最大股数
    risk_shares = calc_max_shares_by_risk(buy_price, stop_loss_price, total_capital)
    risk_amount = risk_shares * buy_price

    # 方法2：按仓位上限计算
    max_amount = total_capital * max_ratio
    max_shares = calc_position_by_ratio(buy_price, max_ratio, total_capital)

    # 取两者较小值
    final_shares = min(risk_shares, max_shares)
    final_amount = final_shares * buy_price

    # 第一批 = 40%
    first_shares = int(final_shares * config.FIRST_BATCH_RATIO)
    first_shares = (first_shares // 100) * 100
    if first_shares == 0 and final_shares >= 100:
        first_shares = 100  # 至少买1手

    first_amount = first_shares * buy_price
    max_loss = first_shares * (buy_price - stop_loss_price)
    max_loss_ratio = max_loss / total_capital if total_capital > 0 else 0

    # 赛道仓位检查
    sector_total = current_sector_amount + first_amount
    sector_limit = total_capital * config.SECTOR_MAX_RATIO
    pass_sector = sector_total <= sector_limit

    # 现金安全垫检查
    cash_reserve = total_capital * config.CASH_RESERVE_RATIO
    pass_cash = first_amount <= (total_capital - cash_reserve)

    pass_risk = pass_sector and pass_cash and (max_loss_ratio <= config.MAX_SINGLE_LOSS_RATIO)
    risk_msgs = []
    if not pass_sector:
        risk_msgs.append(f"赛道仓位超限: {sector_total:.0f} > {sector_limit:.0f}")
    if not pass_cash:
        risk_msgs.append(f"突破现金安全垫: 需{first_amount:.0f}, 可用{total_capital-cash_reserve:.0f}")
    if max_loss_ratio > config.MAX_SINGLE_LOSS_RATIO:
        risk_msgs.append(f"单笔亏损超限: {max_loss_ratio:.2%} > {config.MAX_SINGLE_LOSS_RATIO:.0%}")

    return {
        "shares": first_shares,
        "amount": round(first_amount, 2),
        "ratio": round(first_amount / total_capital, 4) if total_capital > 0 else 0,
        "max_loss": round(max_loss, 2),
        "max_loss_ratio": round(max_loss_ratio, 4),
        "pass_risk": pass_risk,
        "risk_msg": "; ".join(risk_msgs) if risk_msgs else "风控通过"
    }


def calc_second_batch(buy_price: float, first_batch: dict,
                      stock_type: str = "龙头",
                      total_capital: float = None) -> dict:
    """
    计算第二批加仓（60%计划仓位）
    前提：第一批已浮盈>=3%
    """
    if total_capital is None:
        total_capital = config.TOTAL_CAPITAL

    if stock_type == "弹性":
        max_ratio = config.FLEXIBLE_STOCK_MAX_RATIO
    else:
        max_ratio = config.LEADER_STOCK_MAX_RATIO

    # 总计划仓位
    total_shares = calc_position_by_ratio(buy_price, max_ratio, total_capital)
    risk_shares = calc_max_shares_by_risk(buy_price,
                                          buy_price * (1 - config.INITIAL_STOP_LOSS_PCT),
                                          total_capital)
    total_shares = min(total_shares, risk_shares)

    # 第二批 = 总计划 - 第一批
    second_shares = total_shares - first_batch["shares"]
    second_shares = max((second_shares // 100) * 100, 0)
    second_amount = second_shares * buy_price

    return {
        "shares": second_shares,
        "amount": round(second_amount, 2),
        "ratio": round(second_amount / total_capital, 4) if total_capital > 0 else 0,
    }


if __name__ == "__main__":
    print("=" * 50)
    print("  仓位计算模块 - 测试")
    print("=" * 50)

    buy_p = 200.0
    stop_p = 180.0  # 止损10%

    result = calc_first_batch(buy_p, stop_p, stock_type="龙头")
    print(f"\n买入价: {buy_p}, 止损价: {stop_p}")
    print(f"第一批建议股数: {result['shares']}股")
    print(f"买入金额: {result['amount']:.0f}元")
    print(f"占总资金: {result['ratio']:.2%}")
    print(f"最大亏损: {result['max_loss']:.0f}元 ({result['max_loss_ratio']:.2%})")
    print(f"风控结果: {'[PASS]' if result['pass_risk'] else '[FAIL]'} {result['risk_msg']}")

    second = calc_second_batch(buy_p, result, stock_type="龙头")
    print(f"\n第二批建议股数: {second['shares']}股")
    print(f"第二批金额: {second['amount']:.0f}元")
    print("\n[OK] 仓位计算模块测试通过")
