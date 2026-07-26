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
import sqlite3
import threading
from typing import Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from position.kelly import half_kelly_position
from strategy.trade_journal import JOURNAL_DB_PATH

logger = logging.getLogger(__name__)

# ============================================================
# Kelly + ATR 仓位约束（V8.2）
# ============================================================

# 同一运行周期内的历史胜率缓存 {stock_code: (win_rate, profit_factor)}
_win_rate_cache: dict = {}
# FIX: 修复模块级缓存无锁保护的线程安全问题
_cache_lock = threading.Lock()

# Kelly 默认参数（无历史数据时使用）
DEFAULT_WIN_RATE = 0.45
DEFAULT_PROFIT_FACTOR = 2.0

# ATR 波动率阈值
ATR_HIGH_VOL_THRESHOLD = 0.03   # ATR/price > 3% 视为高波动
ATR_LOW_VOL_THRESHOLD = 0.015   # ATR/price < 1.5% 视为低波动
ATR_HIGH_VOL_PENALTY = 0.80     # 高波动减仓20%
ATR_LOW_VOL_BONUS = 1.0         # 低波动不额外调整（2%风险预算法在低波动时已自然产生更大仓位）


def _get_historical_win_rate(stock_code: str) -> Tuple[float, float]:
    """
    查询 trade_journal.db 获取该股票历史交易的胜率和盈亏比

    最少需要5笔已完成交易（action='sell' 且 pnl!=0），否则返回默认值。
    同一运行周期内使用缓存，不重复查询。

    返回:
        (win_rate, profit_factor)
    """
    if stock_code in _win_rate_cache:
        with _cache_lock:
            return _win_rate_cache[stock_code]

    # FIX: 修复SQLite连接在异常路径下未关闭导致连接泄漏
    conn = None
    try:
        conn = sqlite3.connect(JOURNAL_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pnl FROM trades WHERE code=? AND action='sell' AND pnl != 0",
            (stock_code,)
        )
        rows = cursor.fetchall()
    except Exception as e:
        logger.warning(f"查询trade_journal.db失败({stock_code}): {e}")
        return (DEFAULT_WIN_RATE, DEFAULT_PROFIT_FACTOR)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if len(rows) < 5:
        logger.debug(f"{stock_code} 历史交易仅{len(rows)}笔(<5)，使用默认参数")
        return (DEFAULT_WIN_RATE, DEFAULT_PROFIT_FACTOR)

    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    if not wins or not losses:
        return (DEFAULT_WIN_RATE, DEFAULT_PROFIT_FACTOR)

    win_rate = len(wins) / len(pnls)
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    profit_factor = avg_win / avg_loss if avg_loss > 0 else DEFAULT_PROFIT_FACTOR

    result = (round(win_rate, 4), round(profit_factor, 4))
    with _cache_lock:
        _win_rate_cache[stock_code] = result
    logger.debug(f"{stock_code} 历史胜率={win_rate:.2%}, 盈亏比={profit_factor:.2f} (共{len(pnls)}笔)")
    return result


def _calc_atr_ratio(stock_code: str, buy_price: float) -> float:
    """
    从行情数据计算20日ATR，返回 ATR/price 比值。
    如果无法获取数据则返回 -1（表示跳过ATR调整）。
    """
    try:
        from data.data_loader import get_stock_data
        df = get_stock_data(stock_code, days=60)
        if df is None or len(df) < 20:
            return -1.0

        df = df.tail(21)  # 需要21天算20日ATR
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values

        tr_list = []
        for i in range(1, len(close)):
            tr = max(high[i] - low[i],
                     abs(high[i] - close[i - 1]),
                     abs(low[i] - close[i - 1]))
            tr_list.append(tr)

        atr20 = sum(tr_list[-20:]) / 20
        ratio = atr20 / buy_price if buy_price > 0 else -1.0
        return round(ratio, 6)
    except Exception as e:
        logger.debug(f"ATR计算失败({stock_code}): {e}")
        return -1.0


def _atr_adjustment(atr_ratio: float) -> float:
    """
    根据 ATR/price 比值返回仓位调整乘数。
    高波动(>3%) → 0.80（减仓20%）
    低波动(<1.5%) → 1.0（不调整，2%风险预算法在低波动时已自然产生更大仓位）
    其他 → 1.0
    """
    if atr_ratio < 0:
        return 1.0
    if atr_ratio > ATR_HIGH_VOL_THRESHOLD:
        return ATR_HIGH_VOL_PENALTY
    if atr_ratio < ATR_LOW_VOL_THRESHOLD:
        return ATR_LOW_VOL_BONUS
    return 1.0


def _get_atr_value(stock_code: str, price: float) -> float:
    """
    从行情数据计算20日ATR，返回ATR绝对值（元）。
    复用_calc_atr_ratio的数据获取逻辑。
    如果无法获取数据则返回 -1.0。
    """
    try:
        from data.data_loader import get_stock_data
        df = get_stock_data(stock_code, days=60)
        if df is None or len(df) < 20:
            return -1.0

        df = df.tail(21)  # 需要21天算20日ATR
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values

        tr_list = []
        for i in range(1, len(close)):
            tr = max(high[i] - low[i],
                     abs(high[i] - close[i - 1]),
                     abs(low[i] - close[i - 1]))
            tr_list.append(tr)

        atr20 = sum(tr_list[-20:]) / 20
        return round(atr20, 4)
    except Exception as e:
        logger.debug(f"ATR绝对值计算失败({stock_code}): {e}")
        return -1.0


def calc_adaptive_stop_loss(price: float, atr: float,
                            volatility_regime: str = "normal") -> dict:
    """
    ATR自适应止损计算（V8.3新增）

    根据个股波动率（ATR）动态计算止损价，替代固定百分比止损。
    高波动股自动放宽止损距离，低波动股自动收紧止损距离。

    参数:
        price: 当前价格（或买入价）
        atr: 20日ATR绝对值（元）
        volatility_regime: 波动率环境 "high" / "normal" / "low"

    返回:
        {
            "stop_price": float,   # 止损价
            "stop_pct": float,     # 止损幅度（如0.08表示8%）
            "atr_used": float,     # 使用的ATR值
            "method": "adaptive"   # 止损方法标识
        }
    """
    unified_cfg = getattr(config, 'RISK_UNIFIED_CONFIG', {})
    atr_multiplier = unified_cfg.get('atr_stop_multiplier', 2.0)
    min_stop_pct = unified_cfg.get('min_stop_loss_pct', 0.05)
    max_stop_pct = unified_cfg.get('max_stop_loss_pct', 0.15)

    # 基础止损 = price - ATR * 倍数
    base_stop = price - atr * atr_multiplier

    # 保底止损（确保止损不会太远，对应低波动股）
    floor_stop = price * (1 - min_stop_pct)

    # 根据波动率环境调整最大止损百分比（在计算ceiling之前调整，使高波动允许更远止损）
    if volatility_regime == "high":
        effective_max_pct = max_stop_pct * 1.10  # 如15% → 16.5%，允许更远止损
    elif volatility_regime == "low":
        effective_max_pct = max_stop_pct * 0.90  # 如15% → 13.5%，更紧止损
    else:
        effective_max_pct = max_stop_pct

    # 极限止损（确保止损不会太近，对应高波动股）
    ceiling_stop = price * (1 - effective_max_pct)

    # 最终止损 = max(基础止损, 保底止损)，且 min(结果, 极限止损)
    final_stop = max(base_stop, floor_stop)
    final_stop = min(final_stop, ceiling_stop)

    stop_pct = (price - final_stop) / price if price > 0 else 0

    result = {
        "stop_price": round(final_stop, 3),
        "stop_pct": round(stop_pct, 4),
        "atr_used": round(atr, 4),
        "method": "adaptive"
    }

    logger.debug(
        f"ATR自适应止损: 价格={price:.2f}, ATR={atr:.4f}, "
        f"基础止损={base_stop:.2f}, 保底={floor_stop:.2f}, 极限={ceiling_stop:.2f}, "
        f"波动率={volatility_regime}, 最终止损={final_stop:.2f}({stop_pct:.2%})"
    )

    return result


def calc_trailing_stop(current_price: float, highest_since_buy: float,
                       atr: float, profit_pct: float) -> dict:
    """
    ATR连续移动止损计算（V8.3新增）

    将移动止损从固定档位改为基于浮盈百分比的连续函数，
    结合ATR动态调整止损距离，浮盈越多止损越紧。

    参数:
        current_price: 当前价格
        highest_since_buy: 买入以来的最高价
        atr: 20日ATR绝对值（元）
        profit_pct: 浮盈百分比（如0.10表示10%）

    返回:
        {
            "trailing_stop": float,    # 移动止损价
            "lock_profit_pct": float,  # 已锁定利润百分比
            "method": "atr_trailing"   # 止损方法标识
        }
    """
    unified_cfg = getattr(config, 'RISK_UNIFIED_CONFIG', {})
    trailing_mult = unified_cfg.get('trailing_atr_multiplier', 1.5)
    fixed_stop_pct = unified_cfg.get('initial_stop_loss_pct',
                                      getattr(config, 'INITIAL_STOP_LOSS_PCT', 0.10))

    # 反推成本价（所有分支都需要）
    cost_price = current_price / (1 + profit_pct) if profit_pct > -0.99 else current_price * 0.9

    if profit_pct < 0.03:
        # 浮盈 < 3%：止损 = 成本价（保本）
        trailing_stop = cost_price
        lock_pct = 0.0

    elif profit_pct < 0.15:
        # 浮盈 3%-15%：止损 = highest * (1 - ATR * trailing_mult / price)
        atr_ratio = atr / current_price if current_price > 0 else 0.05
        trailing_stop = highest_since_buy * (1 - atr_ratio * trailing_mult)
        lock_pct = max(0, (trailing_stop / cost_price - 1))

    elif profit_pct < 0.30:
        # 浮盈 15%-30%：止损 = highest * (1 - ATR * 1.0 / price)（更紧）
        atr_ratio = atr / current_price if current_price > 0 else 0.05
        trailing_stop = highest_since_buy * (1 - atr_ratio * 1.0)
        lock_pct = max(0, (trailing_stop / cost_price - 1))

    else:
        # 浮盈 > 30%：止损 = highest * (1 - ATR * 0.7 / price)（最紧）
        atr_ratio = atr / current_price if current_price > 0 else 0.05
        trailing_stop = highest_since_buy * (1 - atr_ratio * 0.7)
        lock_pct = max(0, (trailing_stop / cost_price - 1))

    # 终极兜底：固定百分比止损（防止ATR异常时止损太远）
    fixed_stop = cost_price * (1 - fixed_stop_pct)
    trailing_stop = max(trailing_stop, fixed_stop)

    # 确保止损不超过当前价
    trailing_stop = min(trailing_stop, current_price * 0.99)

    lock_pct = max(0, (trailing_stop / cost_price - 1)) if cost_price > 0 else 0.0

    result = {
        "trailing_stop": round(trailing_stop, 3),
        "lock_profit_pct": round(lock_pct, 4),
        "method": "atr_trailing"
    }

    logger.debug(
        f"ATR移动止损: 现价={current_price:.2f}, 最高={highest_since_buy:.2f}, "
        f"ATR={atr:.4f}, 浮盈={profit_pct:.2%}, "
        f"移动止损={trailing_stop:.2f}, 锁定利润={lock_pct:.2%}, 固定兜底={fixed_stop:.2f}"
    )

    return result


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
                     current_sector_amount: float = 0,
                     is_etf: bool = False,
                     stock_code: str = None) -> dict:
    """
    计算第一批试仓（40%计划仓位）
    
    V8.1新增（基于1322笔真实委托验证）:
    - 单笔最低2万元（62.8%小单<1万被手续费吃掉）
    - ETF单只最大20%（科创50占53%严重超限）
    - 每日最多3笔买入（日均18笔→巨亏）
    
    V8.2新增 Kelly + ATR 双约束:
    - Kelly约束: 取min(风险预算仓位, 半Kelly仓位)
    - ATR波动率调整: 高波动减仓20%，低波动不额外调整（2%风险预算法已自然放大仓位）
    
    参数:
        buy_price: 买入价
        stop_loss_price: 止损价
        stock_type: "龙头" 或 "弹性"
        total_capital: 总资金
        current_sector_amount: 该赛道当前已占用金额
        is_etf: 是否为ETF基金
        stock_code: 股票代码（用于Kelly+ATR约束，可选）
    
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
    discipline = getattr(config, 'DISCIPLINE_CONFIG', {})
    if is_etf:
        # V8.1: ETF单只最大20%（防止科创50占53%的情况）
        max_ratio = discipline.get('max_single_etf_ratio', 0.20)
    elif stock_type == "弹性":
        max_ratio = config.FLEXIBLE_STOCK_MAX_RATIO
    else:
        max_ratio = config.LEADER_STOCK_MAX_RATIO

    # 方法1：按风控（2%最大亏损）反算最大股数
    risk_shares = calc_max_shares_by_risk(buy_price, stop_loss_price, total_capital)
    risk_amount = risk_shares * buy_price
    risk_pct = risk_amount / total_capital if total_capital > 0 else 0

    # 方法2：按仓位上限计算
    max_amount = total_capital * max_ratio
    max_shares = calc_position_by_ratio(buy_price, max_ratio, total_capital)

    # 取两者较小值
    final_shares = min(risk_shares, max_shares)

    # ---- V8.2 Kelly约束 ----
    half_kelly_pct = 1.0  # 默认不约束（相当于100%）
    kelly_shares = final_shares  # 默认等于风险预算股数
    if stock_code:
        win_rate, profit_factor = _get_historical_win_rate(stock_code)
        half_kelly_pct = half_kelly_position(win_rate, profit_factor, max_ratio * 2)
        kelly_amount = total_capital * half_kelly_pct
        kelly_shares = int(kelly_amount / buy_price)
        kelly_shares = (kelly_shares // 100) * 100
        # Kelly只能使仓位更小
        final_shares = min(final_shares, kelly_shares)

    # ---- V8.2 ATR波动率调整 ----
    atr_adj = 1.0
    atr_ratio_val = -1.0
    if stock_code:
        atr_ratio_val = _calc_atr_ratio(stock_code, buy_price)
        atr_adj = _atr_adjustment(atr_ratio_val)
        if atr_adj != 1.0:
            adj_shares = int(final_shares * atr_adj)
            adj_shares = (adj_shares // 100) * 100
            # ATR调整只能使仓位更小
            final_shares = min(final_shares, adj_shares)

    final_amount = final_shares * buy_price

    # 仓位计算日志（V8.2）
    logger.debug(
        f"仓位计算[{stock_code or 'N/A'}]: "
        f"风险预算={risk_pct:.2%}, 半Kelly={half_kelly_pct:.2%}, "
        f"ATR比={atr_ratio_val:.4f}, ATR调整={atr_adj:.2f}, "
        f"最终股数={final_shares}, 最终金额={final_amount:.0f}"
    )

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

    # V8.1: 最小交易金额检查（单笔不低于2万，避免小单碎片化）
    min_trade = discipline.get('min_trade_amount', 20000)
    pass_min_amount = first_amount >= min_trade
    if not pass_min_amount and first_shares > 0:
        # 尝试提升到最低金额
        min_shares = int(min_trade / buy_price / 100) * 100
        if min_shares > first_shares and min_shares <= final_shares:
            first_shares = min_shares
            first_amount = first_shares * buy_price
            max_loss = first_shares * (buy_price - stop_loss_price)
            max_loss_ratio = max_loss / total_capital if total_capital > 0 else 0
            pass_min_amount = first_amount >= min_trade

    pass_risk = pass_sector and pass_cash and pass_min_amount and (max_loss_ratio <= config.MAX_SINGLE_LOSS_RATIO)
    risk_msgs = []
    if not pass_sector:
        risk_msgs.append(f"赛道仓位超限: {sector_total:.0f} > {sector_limit:.0f}")
    if not pass_cash:
        risk_msgs.append(f"突破现金安全垫: 需{first_amount:.0f}, 可用{total_capital-cash_reserve:.0f}")
    if not pass_min_amount:
        risk_msgs.append(f"低于最低交易额: {first_amount:.0f} < {min_trade:.0f}元(小单无意义)")
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
