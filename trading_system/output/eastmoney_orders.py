"""
东方财富智能条件单生成模块（V3 - 双轨止盈版）
===============================================
根据策略信号，生成东方财富APP可直接填写的智能条件单
提前一天盘前发送到邮箱，开盘即可照着填单

V3 改进:
- 双轨止盈体系: 第一轨阶梯止盈(8%卖1/3, 20%再卖1/3) + 第二轨回落止盈(底仓)
- 止损升级: 龙头8%/弹性10% + 移动止损只上移不下移 + 仅收盘价触发
- 强制卖出: 单日放量大跌>8%无条件离场
- 时间红线: 醒目标注禁止开仓时段
- 信号质量评分: 条件单按优先级排序

东方财富条件单类型对应:
- 定价买入: 价格跌到目标价自动买入
- 反弹买入: 从最低点反弹一定幅度后买入
- 定价卖出: 价格跌到止损价自动卖出
- 回落卖出: 从最高点回落一定幅度自动卖出

使用方式:
    from output.eastmoney_orders import generate_eastmoney_orders
    html = generate_eastmoney_orders(signals, holdings, data_dict)
"""

import os
import datetime
import logging

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

try:
    import pandas as pd
except ImportError:
    pd = None


# ============================================================
# 一、条件单类型映射
# ============================================================

# 东方财富条件单类型
ORDER_TYPES = {
    "buy_limit": "定价买入",        # 价格<=目标价时买入
    "buy_rebound": "反弹买入",      # 从最低点反弹N%后买入
    "sell_stop_loss": "定价卖出",   # 价格<=止损价时卖出
    "sell_profit_stop": "止盈止损", # 达到止盈或止损时卖出
    "sell_drawdown": "回落卖出",    # 从最高点回落N%时卖出
    "sell_open_board": "开板卖出",  # 涨停开板时卖出
}


def _calculate_smart_stop_loss(code: str, holding: dict, sig: dict, data_df=None) -> dict:
    """
    智能止损价计算（V2）
    综合 ATR自适应 + 技术支撑位 + 移动止损，取最优值
    
    返回:
        {
            "stop_loss": float,      # 最终止损价
            "method": str,           # 计算方法说明
            "atr_stop": float,       # ATR止损价
            "support_stop": float,   # 技术支撑止损价
            "trailing_stop": float,  # 移动止损价
            "is_breached": bool,     # 是否已跌破止损
            "breach_pct": float,     # 跌破幅度
            "recommendation": str    # 操作建议
        }
    """
    buy_price = holding.get("buy_price", 0)
    current_price = holding.get("current_price", buy_price)
    stock_type = holding.get("stock_type", "龙头")
    
    result = {
        "stop_loss": 0,
        "method": "",
        "atr_stop": 0,
        "support_stop": 0,
        "trailing_stop": 0,
        "is_breached": False,
        "breach_pct": 0,
        "recommendation": ""
    }
    
    if buy_price <= 0 or current_price <= 0:
        return result
    
    # ---- 1. ATR自适应止损 ----
    # 用 2.5倍 ATR 作为止损幅度（覆盖95%的正常波动）
    # 龙头股用2倍ATR（波动小），弹性股用3倍ATR（波动大）
    atr_multiplier = 2.0 if stock_type == "龙头" else 3.0
    
    atr_value = 0
    if data_df is not None and "atr" in data_df.columns:
        atr_value = data_df["atr"].iloc[-1] if len(data_df) > 0 else 0
    
    if atr_value and atr_value > 0:
        atr_stop = round(current_price - atr_multiplier * atr_value, 2)
    else:
        # 没有ATR数据时，用日内振幅估算（近5日平均振幅的2倍）
        if data_df is not None and len(data_df) >= 5:
            recent_highs = data_df["high"].iloc[-5:]
            recent_lows = data_df["low"].iloc[-5:]
            avg_range = ((recent_highs - recent_lows) / recent_lows * 100).mean()
            estimated_atr = current_price * avg_range / 100
            atr_stop = round(current_price - atr_multiplier * estimated_atr, 2)
        else:
            # 最后兜底：用固定百分比（龙头8%，弹性12%）
            fallback_pct = 0.08 if stock_type == "龙头" else 0.12
            atr_stop = round(current_price * (1 - fallback_pct), 2)
    
    result["atr_stop"] = atr_stop
    
    # ---- 2. 技术支撑位止损 ----
    # 取 MA20、MA60、布林下轨 中最近的一个作为支撑止损
    support_levels = []
    
    if data_df is not None:
        latest = data_df.iloc[-1] if len(data_df) > 0 else {}
        
        # MA20 支撑
        if "ma20" in data_df.columns and not pd.isna(latest.get("ma20", None)):
            ma20 = latest["ma20"]
            if ma20 < current_price:  # 只在现价上方时才有意义
                support_levels.append(("MA20", round(ma20 * 0.99, 2)))  # MA20下方1%作为缓冲
        
        # MA60 支撑
        if "ma60" in data_df.columns and not pd.isna(latest.get("ma60", None)):
            ma60 = latest["ma60"]
            if ma60 < current_price:
                support_levels.append(("MA60", round(ma60 * 0.99, 2)))
        
        # 布林带下轨支撑
        if "boll_lower" in data_df.columns and not pd.isna(latest.get("boll_lower", None)):
            boll_lower = latest["boll_lower"]
            if boll_lower < current_price:
                support_levels.append(("布林下轨", round(boll_lower * 0.99, 2)))
    
    # 取最高的支撑位（最接近现价的）作为技术支撑止损
    if support_levels:
        support_name, support_stop = max(support_levels, key=lambda x: x[1])
        result["support_stop"] = support_stop
        result["support_method"] = support_name
    else:
        result["support_stop"] = 0
        result["support_method"] = "无"
    
    # ---- 3. 移动止损（已有浮盈时）----
    trailing_stop = sig.get("stop_loss_current") or sig.get("stop_loss_initial")
    if not trailing_stop:
        trailing_stop = round(buy_price * 0.90, 2)
    result["trailing_stop"] = round(trailing_stop, 2)
    
    # ---- 4. 综合决策：取三者中最高的（最保护投资者的）----
    # 但前提是不能高于现价（否则挂单会立刻成交）
    candidates = []
    if atr_stop > 0 and atr_stop < current_price:
        candidates.append(("ATR自适应", atr_stop))
    if result["support_stop"] > 0 and result["support_stop"] < current_price:
        candidates.append((f"技术支撑({result['support_method']})", result["support_stop"]))
    if trailing_stop > 0 and trailing_stop < current_price:
        candidates.append(("移动止损", trailing_stop))
    
    if candidates:
        # 取最高的止损价（最保守，最保护利润）
        best_method, best_stop = max(candidates, key=lambda x: x[1])
        result["stop_loss"] = best_stop
        result["method"] = best_method
    else:
        # 所有止损价都高于现价，说明已经深度套牢
        # 用现价下方3%作为止损（避免立刻成交）
        result["stop_loss"] = round(current_price * 0.97, 2)
        result["method"] = "现价下方3%（已深度套牢）"
    
    # ---- 5. 检测是否已跌破止损 ----
    if current_price < result["trailing_stop"]:
        result["is_breached"] = True
        result["breach_pct"] = round((current_price - result["trailing_stop"]) / result["trailing_stop"] * 100, 2)
        result["recommendation"] = f"⚠️ 已跌破移动止损线{result['trailing_stop']:.2f}，建议开盘择机卖出"
    elif current_price < atr_stop:
        result["is_breached"] = True
        result["breach_pct"] = round((current_price - atr_stop) / atr_stop * 100, 2)
        result["recommendation"] = f"️ 已跌破ATR止损线{atr_stop:.2f}，关注能否企稳"
    else:
        result["recommendation"] = f"止损位{result['stop_loss']:.2f}，距现价{round((current_price - result['stop_loss']) / current_price * 100, 1)}%"
    
    return result


def _calculate_realistic_take_profit(code: str, holding: dict, data_df=None) -> list:
    """
    计算合理的止盈目标价（V2）
    结合近期前高、布林上轨、整数关口，避免不切实际的目标
    
    返回:
        [(price, sell_pct, reason), ...]
    """
    buy_price = holding.get("buy_price", 0)
    current_price = holding.get("current_price", buy_price)
    
    if buy_price <= 0:
        return []
    
    targets = []
    
    # ---- 1. 近期前高作为第一目标 ----
    if data_df is not None and len(data_df) >= 20:
        recent_high = data_df["high"].iloc[-20:].max()  # 近20日最高
        if recent_high > current_price:
            targets.append((round(recent_high, 2), 0.33, f"近20日前高{recent_high:.2f}"))
    
    # ---- 2. 布林上轨作为第二目标 ----
    if data_df is not None:
        latest = data_df.iloc[-1] if len(data_df) > 0 else {}
        if "boll_upper" in data_df.columns and not pd.isna(latest.get("boll_upper", None)):
            boll_upper = latest["boll_upper"]
            if boll_upper > current_price:
                targets.append((round(boll_upper, 2), 0.33, f"布林上轨{boll_upper:.2f}"))
    
    # ---- 3. 成本价上方合理涨幅作为保底目标 ----
    # 第一档：回本或微盈（如果当前亏损）
    if current_price < buy_price:
        breakeven = round(buy_price * 1.01, 2)  # 回本+1%
        targets.insert(0, (breakeven, 0.50, f"回本目标{breakeven:.2f}"))
    
    # 如果已有目标不足3档，补充固定涨幅目标
    default_targets = [
        (round(buy_price * 1.08, 2), 1/3, "成本+8%"),
        (round(buy_price * 1.15, 2), 1/3, "成本+15%"),
        (round(buy_price * 1.25, 2), 1/3, "成本+25%"),
    ]
    
    for price, pct, reason in default_targets:
        if price > current_price and price not in [t[0] for t in targets]:
            targets.append((price, pct, reason))
        if len(targets) >= 3:
            break
    
    # 去重并排序
    seen = set()
    unique_targets = []
    for price, pct, reason in sorted(targets, key=lambda x: x[0]):
        if price not in seen and price > current_price:
            seen.add(price)
            unique_targets.append((price, pct, reason))
    
    return unique_targets[:3]


def _get_drawdown_pct(stock_type: str, sector: str = "") -> float:
    """根据股票类型/赛道获取回落止盈幅度"""
    drawdown_map = getattr(config, 'DRAWDOWN_STOP', {})
    # 龙头 → 5%, 赛道(半导体/存储/光模块) → 4%, 弹性 → 3%
    if stock_type == "龙头":
        return drawdown_map.get("龙头稳健", 0.05)
    # 判断是否属于成长赛道
    growth_keywords = ["半导体", "存储", "光模块", "AI", "芯片", "封测", "材料"]
    if any(kw in sector for kw in growth_keywords):
        return drawdown_map.get("成长赛道", 0.04)
    return drawdown_map.get("高弹性", 0.03)


def _generate_holding_orders(code: str, sig: dict, holding: dict, data_df=None) -> list:
    """
    为每个持仓生成完整的条件单组合（V3.0 双轨止盈版）
    
    双轨止盈体系:
    - 第一轨（阶梯止盈）: 浮盈8%→卖1/3, 浮盈20%→再卖1/3
    - 第二轨（回落止盈）: 剩余1/3底仓→高点回落5%/4%/3%卖出
    
    止损体系:
    - 初始止损: 龙头8% / 弹性10%
    - 移动止损: 每日盘后更新（只上移不下移）
    - 仅收盘价触发，盘中跳水不割
    
    参数:
        code: 股票代码
        sig: 策略信号
        holding: 持仓信息
        data_df: 技术指标DataFrame（可选，用于ATR/支撑位计算）
    """
    orders = []
    stock_info = config.get_stock_info(code)
    name = stock_info.get("名称", code)
    stock_type = stock_info.get("类型", "龙头")
    sector = stock_info.get("赛道", "")

    shares = holding.get("shares", 0)
    buy_price = holding.get("buy_price", 0)
    current_price = holding.get("current_price", holding.get("buy_price", 0))
    highest_price = holding.get("highest_price", current_price)

    if shares <= 0 or buy_price <= 0:
        return orders

    # 当前浮盈
    profit_pct = (current_price - buy_price) / buy_price

    # ---- 智能止损计算 ----
    stop_info = _calculate_smart_stop_loss(code, holding, sig, data_df)
    stop_loss = stop_info["stop_loss"]

    # ---- 1. 止损条件单（定价卖出）- 最高优先级 ----
    # 初始止损: 龙头8% / 弹性10%
    initial_stop_pct = getattr(config, 'INITIAL_STOP_LOSS_LOW', 0.08) if stock_type == "龙头" else getattr(config, 'INITIAL_STOP_LOSS_PCT', 0.10)
    initial_stop_price = round(buy_price * (1 - initial_stop_pct), 2)
    # 取移动止损和初始止损中更高的（只上移不下移）
    final_stop = max(stop_loss, initial_stop_price) if stop_loss > 0 else initial_stop_price
    # 止损不能高于现价（否则立刻成交）
    if final_stop >= current_price:
        final_stop = round(current_price * 0.97, 2)

    stop_label = "定价卖出（止损）"
    stop_notes = (f"{stop_info['method']} | 成本{buy_price:.2f} | 浮盈{profit_pct:.1%} | "
                  f"仅收盘价触发，盘中跳水不割")
    
    if stop_info["is_breached"]:
        stop_label = "定价卖出（⚠️已破止损）"
        stop_notes = f"{stop_info['recommendation']} | 跌破{stop_info['breach_pct']:.1%} | 建议开盘择机卖出"
    
    orders.append({
        "order_type": "sell_stop_loss",
        "order_type_cn": stop_label,
        "code": code,
        "name": name,
        "trigger_price": final_stop,
        "order_price": round(final_stop * 0.99, 2),
        "shares": shares,
        "condition_desc": f"收盘价 <= {final_stop:.2f} 时，卖出全部 {shares} 股（仅收盘价触发）",
        "priority": 1,
        "notes": stop_notes,
        "category": "stop_loss"
    })

    # ---- 2. 第一轨：阶梯止盈条件单 ----
    ladder_levels = getattr(config, 'LADDER_SELL_LEVELS', [(0.08, 1/3), (0.20, 1/3)])
    for i, (threshold, sell_ratio) in enumerate(ladder_levels, 1):
        target_price = round(buy_price * (1 + threshold), 2)
        # 只有目标价高于现价才有意义
        if target_price <= current_price:
            continue  # 已达到或超过该档，跳过（可能已执行）
        sell_shares = max(100, int(shares * sell_ratio / 100) * 100)
        profit_amount = sell_shares * (target_price - buy_price)
        orders.append({
            "order_type": "sell_stop_loss",
            "order_type_cn": f"定价卖出（阶梯止盈第{i}档）",
            "code": code,
            "name": name,
            "trigger_price": target_price,
            "order_price": round(target_price * 0.99, 2),
            "shares": sell_shares,
            "condition_desc": f"收盘价 >= {target_price:.2f} 时，卖出 {sell_shares} 股（浮盈+{threshold:.0%}，卖{sell_ratio:.0%}仓位）",
            "priority": 3,
            "notes": f"阶梯止盈第{i}档 | 成本+{threshold:.0%} | 卖出{sell_shares}股 | 预计盈利{profit_amount:,.0f}元",
            "category": "take_profit"
        })

    # ---- 3. 第二轨：回落止盈条件单（底仓保护）----
    drawdown_pct = _get_drawdown_pct(stock_type, sector)
    # 回落止盈基于持仓期间最高价
    drawdown_trigger = round(highest_price * (1 - drawdown_pct), 2)
    # 底仓 = 总仓位 - 已阶梯卖出的部分（简化为1/3底仓）
    base_shares = max(100, int(shares / 3 / 100) * 100)
    
    # 回落止盈只在有浮盈时设置
    if profit_pct > 0.02 and drawdown_trigger > buy_price:
        type_label = "龙头5%" if stock_type == "龙头" else ("赛道4%" if drawdown_pct == 0.04 else "弹性3%")
        orders.append({
            "order_type": "sell_drawdown",
            "order_type_cn": "回落卖出（底仓止盈）",
            "code": code,
            "name": name,
            "trigger_price": drawdown_trigger,
            "order_price": round(drawdown_trigger * 0.99, 2),
            "shares": base_shares,
            "condition_desc": f"从最高{highest_price:.2f}回落{drawdown_pct:.0%}（<= {drawdown_trigger:.2f}）时，卖出底仓 {base_shares} 股",
            "priority": 2,
            "notes": (f"第二轨回落止盈 | 类型:{type_label} | 最高价{highest_price:.2f} | "
                       f"回落{drawdown_pct:.0%}触发 | 锁定利润{(drawdown_trigger - buy_price) * base_shares:,.0f}元"),
            "category": "drawdown"
        })

    # ---- 4. 如果有明确卖出信号，更新止损单 ----
    if sig.get("sell_signal"):
        sell_price = sig.get("sell_price", final_stop)
        sell_type = sig.get("sell_type", "")
        reason = sig.get("signal_reason", "")

        for order in orders:
            if order["category"] == "stop_loss":
                if sell_price < current_price:
                    order["trigger_price"] = round(sell_price, 2)
                    order["order_price"] = round(sell_price * 0.99, 2)
                    order["condition_desc"] = f"收盘价 <= {sell_price:.2f} 时，卖出全部 {shares} 股（仅收盘价触发）"

                if "强制卖出" in reason or sell_type == "force_sell":
                    order["order_type_cn"] = "定价卖出（⚠️强制卖出）"
                    order["notes"] = f"🚨 强制卖出！放量大跌 | {reason}"
                elif "止损" in reason or sell_type == "stop_loss":
                    order["order_type_cn"] = "定价卖出（紧急止损）"
                    order["notes"] = f"⚠️ 紧急止损！{reason}"
                elif "趋势破位" in reason or sell_type == "trend_break":
                    order["order_type_cn"] = "定价卖出（趋势破位）"
                    order["notes"] = f"⚠️ 趋势破位卖出 | {reason}"
                elif "MACD死叉" in reason or sell_type == "macd_death_cross":
                    order["order_type_cn"] = "定价卖出（MACD死叉）"
                    order["notes"] = f"MACD死叉卖出 | {reason}"
                elif "RSI" in reason:
                    order["order_type_cn"] = "定价卖出（RSI超买回落）"
                    order["notes"] = f"RSI超买回落 | {reason}"
                else:
                    order["notes"] = f"卖出信号 | {reason}"
                order["priority"] = 1
                break

    return orders


def ratio_to_pct(price: float, base: float) -> float:
    """计算价格相对成本的涨幅百分比"""
    if base <= 0:
        return 0
    return (price - base) / base * 100


def _map_signal_to_order(code: str, sig: dict, holding: dict = None, data_df=None) -> list:
    """
    将策略信号映射为东方财富条件单
    
    参数:
        code: 股票代码
        sig: 策略信号字典
        holding: 当前持仓信息
        data_df: 技术指标DataFrame
    
    返回:
        [order_dict, ...] 条件单列表
    """
    orders = []
    stock_info = config.get_stock_info(code)
    name = stock_info.get("名称", code)
    stock_type = stock_info.get("类型", "龙头")

    # ---- 有持仓：生成完整的止损+止盈+回落条件单组合 ----
    if holding and holding.get("shares", 0) > 0:
        orders.extend(_generate_holding_orders(code, sig, holding, data_df))

    # ---- 买入信号：生成定价买入条件单 ----
    if sig.get("buy_signal") and sig.get("buy_price"):
        buy_price = sig["buy_price"]
        stop_loss = sig.get("stop_loss_initial", buy_price * 0.9)

        from strategy.position import calc_first_batch
        batch = calc_first_batch(buy_price, stop_loss, stock_type, config.TOTAL_CAPITAL)
        shares = batch["shares"] if batch["pass_risk"] else 0

        if shares > 0:
            trigger_price = round(buy_price, 2)
            orders.append({
                "order_type": "buy_limit",
                "order_type_cn": "定价买入",
                "code": code,
                "name": name,
                "trigger_price": trigger_price,
                "order_price": round(trigger_price * 1.01, 2),
                "shares": shares,
                "condition_desc": f"股价 <= {trigger_price:.2f} 时，以市价买入 {shares} 股（约{shares * trigger_price:,.0f}元）",
                "priority": 2,
                "notes": sig.get("signal_reason", ""),
                "category": "buy"
            })

    # ---- 加仓信号：反弹买入 ----
    if sig.get("add_position") and holding:
        add_price = holding.get("current_price", holding.get("buy_price", 0))
        shares = holding.get("shares", 0)

        if add_price > 0 and shares > 0:
            from strategy.position import calc_second_batch
            first_batch = {"shares": shares}
            second = calc_second_batch(add_price, first_batch, stock_type, config.TOTAL_CAPITAL)
            add_shares = second["shares"]

            if add_shares > 0:
                trigger = round(add_price, 2)
                orders.append({
                    "order_type": "buy_rebound",
                    "order_type_cn": "反弹买入（加仓）",
                    "code": code,
                    "name": name,
                    "trigger_price": trigger,
                    "order_price": round(trigger * 1.01, 2),
                    "shares": add_shares,
                    "condition_desc": f"股价站稳 {trigger:.2f} 后反弹，买入 {add_shares} 股（第二批加仓）",
                    "priority": 3,
                    "notes": sig.get("signal_reason", ""),
                    "category": "add_position"
                })

    return orders


# ============================================================
# 二、生成东方财富条件单HTML报告
# ============================================================

def generate_eastmoney_orders(signals: list, holdings: dict = None, data_dict: dict = None) -> str:
    """
    生成东方财富智能条件单HTML报告
    按股票分组展示，每只股票显示完整的止损+止盈+回落条件单组合
    
    参数:
        signals: [(code, signal_dict), ...]
        holdings: {code: holding_info}
        data_dict: {code: DataFrame} 技术指标数据（用于ATR/支撑位计算）
    """
    if holdings is None:
        holdings = {}
    if data_dict is None:
        data_dict = {}

    # 收集所有条件单
    all_orders = []
    for code, sig in signals:
        holding = holdings.get(code)
        data_df = data_dict.get(code)
        orders = _map_signal_to_order(code, sig, holding, data_df)
        all_orders.extend(orders)

    # 按股票分组
    from collections import OrderedDict
    stock_orders = OrderedDict()
    for order in all_orders:
        key = f"{order['code']} {order['name']}"
        if key not in stock_orders:
            stock_orders[key] = []
        stock_orders[key].append(order)

    # 统计
    buy_orders = [o for o in all_orders if o["order_type"].startswith("buy")]
    sell_orders = [o for o in all_orders if o["order_type"].startswith("sell")]
    urgent_orders = [o for o in sell_orders if o["priority"] == 1]

    # 下一个交易日
    next_day = datetime.date.today() + datetime.timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += datetime.timedelta(days=1)
    next_trade_day = next_day.strftime("%Y-%m-%d")

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 20px; background: #f0f2f5; }}
    .container {{ max-width: 960px; margin: 0 auto; }}
    .header {{ background: linear-gradient(135deg, #FF6B35, #F7931E); color: white; padding: 20px 30px; border-radius: 12px 12px 0 0; }}
    .header h1 {{ margin: 0; font-size: 22px; }}
    .header .subtitle {{ font-size: 13px; opacity: 0.9; margin-top: 5px; }}
    .content {{ background: white; padding: 20px 30px; border-radius: 0 0 12px 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }}
    .section-title {{ font-size: 16px; font-weight: bold; color: #333; margin: 20px 0 10px; padding-left: 12px; border-left: 4px solid #FF6B35; }}
    .stock-group {{ border: 1px solid #e8e8e8; border-radius: 10px; margin: 12px 0; overflow: hidden; }}
    .stock-group.urgent {{ border-color: #FF4D4F; }}
    .stock-header {{ background: #fafafa; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #f0f0f0; }}
    .stock-group.urgent .stock-header {{ background: #FFF1F0; }}
    .stock-name {{ font-size: 16px; font-weight: bold; color: #333; }}
    .stock-info {{ font-size: 12px; color: #888; }}
    .stock-info .pnl {{ font-weight: bold; }}
    .stock-info .pnl.loss {{ color: #52C41A; }}
    .stock-info .pnl.profit {{ color: #FF4D4F; }}
    .order-row {{ display: flex; align-items: center; padding: 10px 16px; border-bottom: 1px solid #f5f5f5; font-size: 13px; }}
    .order-row:last-child {{ border-bottom: none; }}
    .order-row.urgent {{ background: #FFF1F0; }}
    .order-row.profit {{ background: #F6FFED; }}
    .order-row.drawdown {{ background: #FFF7E6; }}
    .order-row.buy-row {{ background: #E6F7FF; }}
    .order-tag {{ min-width: 90px; font-size: 12px; padding: 3px 8px; border-radius: 4px; text-align: center; font-weight: bold; }}
    .order-tag.stop-loss {{ background: #FF4D4F; color: white; }}
    .order-tag.take-profit {{ background: #52C41A; color: white; }}
    .order-tag.drawdown-tag {{ background: #FA8C16; color: white; }}
    .order-tag.buy-tag {{ background: #1890FF; color: white; }}
    .order-prices {{ flex: 1; display: flex; gap: 20px; align-items: center; }}
    .price-item {{ display: flex; flex-direction: column; }}
    .price-item .label {{ font-size: 11px; color: #999; }}
    .price-item .value {{ font-size: 14px; font-weight: bold; }}
    .price-item .value.red {{ color: #FF4D4F; }}
    .price-item .value.green {{ color: #52C41A; }}
    .order-shares {{ min-width: 80px; text-align: right; font-size: 13px; color: #333; }}
    .order-notes {{ font-size: 11px; color: #999; margin-top: 2px; }}
    .stats {{ display: flex; gap: 15px; margin: 15px 0; flex-wrap: wrap; }}
    .stat-box {{ background: #f5f5f5; padding: 10px 20px; border-radius: 8px; text-align: center; }}
    .stat-box .label {{ font-size: 12px; color: #888; }}
    .stat-box .value {{ font-size: 20px; font-weight: bold; color: #333; }}
    .guide {{ background: #FFFBE6; border: 1px solid #FFE58F; border-radius: 8px; padding: 15px; margin: 15px 0; font-size: 13px; }}
    .guide h3 {{ margin: 0 0 8px; color: #D48806; font-size: 14px; }}
    .guide ol {{ margin: 5px 0; padding-left: 20px; }}
    .guide li {{ margin: 4px 0; color: #666; }}
    .footer {{ text-align: center; color: #bbb; font-size: 11px; margin-top: 20px; padding-top: 15px; border-top: 1px solid #eee; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; color: white; }}
    .badge-urgent {{ background: #FF4D4F; }}
    .badge-normal {{ background: #1890FF; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>东方财富智能条件单（双轨止盈版）</h1>
        <div class="subtitle">适用日期: {next_trade_day}（提前挂单） | 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} | 持仓 {len(holdings)} 只</div>
    </div>
    <div class="content">
"""

    # 统计
    html += f"""
        <div class="stats">
            <div class="stat-box"><div class="label">紧急止损</div><div class="value" style="color:#FF4D4F">{len(urgent_orders)}</div></div>
            <div class="stat-box"><div class="label">止盈/回落</div><div class="value" style="color:#FA8C16">{len(sell_orders) - len(urgent_orders)}</div></div>
            <div class="stat-box"><div class="label">买入</div><div class="value" style="color:#1890FF">{len(buy_orders)}</div></div>
            <div class="stat-box"><div class="label">总条件单</div><div class="value">{len(all_orders)}</div></div>
        </div>
"""

    # 时间红线警告
    no_trade_morning = getattr(config, 'NO_TRADE_MORNING', ("09:30", "10:00"))
    no_trade_afternoon = getattr(config, 'NO_TRADE_AFTERNOON', ("14:30", "15:00"))
    no_new_after = getattr(config, 'NO_NEW_AFTER', "13:30")
    html += f"""
        <div style="background:#FFF1F0;border:2px solid #FF4D4F;border-radius:8px;padding:12px 16px;margin:15px 0;">
            <div style="font-size:14px;font-weight:bold;color:#FF4D4F;margin-bottom:6px">⛔ 交易时间红线（严禁违反）</div>
            <div style="font-size:13px;color:#333;line-height:1.8">
                • <b>{no_trade_morning[0]}-{no_trade_morning[1]}</b> 禁止新开仓（开盘半小时不动）<br>
                • <b>{no_trade_afternoon[0]}-{no_trade_afternoon[1]}</b> 禁止新开仓（收盘半小时不动）<br>
                • <b>{no_new_after}后</b> 禁止新开计划外标的<br>
                • 止损单仅看<b>收盘价</b>，盘中跳水不割肉
            </div>
        </div>
"""

    # 操作指南
    html += """
        <div class="guide">
            <h3>东方财富APP操作步骤（双轨止盈版）</h3>
            <ol>
                <li>打开东方财富APP → 交易 → <b>智能条件单</b></li>
                <li>按从上到下顺序设置，<span style="color:#FF4D4F;font-weight:bold">红色=紧急止损</span>优先</li>
                <li><b>止损单</b>：触发价=止损价，委托价=触发价×0.99，仅收盘价触发</li>
                <li><b>阶梯止盈</b>：第1档成本+8%卖1/3，第2档成本+20%再卖1/3</li>
                <li><b>回落卖出</b>：底仓1/3，龙头回落5%/赛道4%/弹性3%触发</li>
                <li><b>定价买入</b>：触发价=目标价，委托价=触发价×1.01</li>
                <li>每只股票设置完止损+阶梯止盈+回落卖出后再设下一只</li>
            </ol>
        </div>
"""

    # 按股票分组展示
    for stock_key, orders in stock_orders.items():
        # 判断是否有紧急止损
        has_urgent = any(o["priority"] == 1 for o in orders)
        group_class = "urgent" if has_urgent else ""

        # 获取持仓信息
        code = orders[0]["code"]
        holding = holdings.get(code, {})
        buy_price = holding.get("buy_price", 0)
        current_price = holding.get("current_price", buy_price)
        shares = holding.get("shares", 0)
        pnl_pct = (current_price - buy_price) / buy_price * 100 if buy_price > 0 else 0
        pnl_class = "profit" if pnl_pct >= 0 else "loss"
        pnl_sign = "+" if pnl_pct >= 0 else ""

        html += f"""
        <div class="stock-group {group_class}">
            <div class="stock-header">
                <div>
                    <span class="stock-name">{stock_key}</span>
                    {f'<span class="badge badge-urgent">紧急</span>' if has_urgent else ''}
                </div>
                <div class="stock-info">
                    持仓{shares}股 | 成本{buy_price:.2f} | 现价{current_price:.2f} |
                    <span class="pnl {pnl_class}">{pnl_sign}{pnl_pct:.1f}%</span>
                </div>
            </div>
"""

        # 排序：止损(1) > 回落(2) > 止盈(3) > 买入
        order_sort = {"stop_loss": 0, "drawdown": 1, "take_profit": 2, "buy": 3, "add_position": 4}
        orders.sort(key=lambda x: (order_sort.get(x.get("category", ""), 9), x["priority"]))

        for order in orders:
            cat = order.get("category", "")
            if cat == "stop_loss":
                row_class = "urgent"
                tag_class = "stop-loss"
            elif cat == "take_profit":
                row_class = "profit"
                tag_class = "take-profit"
            elif cat == "drawdown":
                row_class = "drawdown"
                tag_class = "drawdown-tag"
            elif cat in ("buy", "add_position"):
                row_class = "buy-row"
                tag_class = "buy-tag"
            else:
                row_class = ""
                tag_class = ""

            price_color = "red" if order["order_type"].startswith("sell") else "green"

            html += f"""
            <div class="order-row {row_class}">
                <div class="order-tag {tag_class}">{order['order_type_cn']}</div>
                <div class="order-prices">
                    <div class="price-item">
                        <span class="label">触发价</span>
                        <span class="value {price_color}">{order['trigger_price']:.2f}</span>
                    </div>
                    <div class="price-item">
                        <span class="label">委托价</span>
                        <span class="value {price_color}">{order['order_price']:.2f}</span>
                    </div>
                    <div class="price-item" style="flex:1">
                        <span class="label">说明</span>
                        <span class="value" style="font-size:12px;color:#555">{order['condition_desc']}</span>
                    </div>
                </div>
                <div class="order-shares">
                    {order['shares']}股<br>
                    <span style="font-size:11px;color:#999">{order['shares'] * order['trigger_price']:,.0f}元</span>
                </div>
            </div>
            <div style="padding:0 16px 8px;font-size:11px;color:#bbb;border-bottom:1px solid #f5f5f5">{order['notes']}</div>
"""

        html += "        </div>\n"

    # 底部说明
    html += f"""
        <div class="footer">
            本报告由交易系统自动生成 | 条件单仅供参考，请根据实际情况调整<br>
            股市有风险，投资需谨慎 | 总资金: {config.TOTAL_CAPITAL:,.0f}元
        </div>
    </div>
</div>
</body>
</html>"""

    return html


# ============================================================
# 三、发送条件单邮件
# ============================================================

def send_eastmoney_orders_email(signals: list, holdings: dict = None, data_dict: dict = None) -> bool:
    """
    生成并发送东方财富条件单邮件
    
    参数:
        signals: 策略信号列表
        holdings: 持仓数据
        data_dict: 技术指标数据
    
    返回: 是否发送成功
    """
    from notify.email_notify import send_email

    next_day = datetime.date.today() + datetime.timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += datetime.timedelta(days=1)
    next_trade_day = next_day.strftime("%Y-%m-%d")

    subject = f"[条件单] 东方财富智能条件单 - {next_trade_day}（提前挂单）"
    html_content = generate_eastmoney_orders(signals, holdings, data_dict)

    return send_email(subject, html_content)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=" * 50)
    print("  东方财富条件单模块 - 测试")
    print("=" * 50)

    mock_signals = [
        ("002371", {
            "buy_signal": False, "sell_signal": True,
            "sell_price": 697.12, "sell_type": "stop_loss",
            "stop_loss_current": 697.12,
            "signal_reason": "触发止损: 收盘677.00 <= 止损线697.12, 浮盈=-12.60%"
        }),
        ("002409", {
            "buy_signal": False, "sell_signal": True,
            "sell_price": 174.20, "sell_type": "stop_loss",
            "stop_loss_current": 174.20,
            "signal_reason": "触发止损: 收盘145.00 <= 止损线174.20, 浮盈=-25.09%"
        }),
        ("600118", {
            "buy_signal": False, "sell_signal": True,
            "sell_price": 62.35, "sell_type": "trend_break",
            "stop_loss_current": 59.57,
            "signal_reason": "趋势破位: 收盘跌破60日线且均线拐头向下"
        }),
        ("600584", {
            "buy_signal": False, "sell_signal": True,
            "sell_price": 88.02, "sell_type": "stop_loss",
            "stop_loss_current": 88.02,
            "signal_reason": "触发止损: 收盘85.49 <= 止损线88.02, 浮盈=-12.59%"
        }),
        ("002384", {
            "buy_signal": False, "sell_signal": True,
            "sell_price": 241.92, "sell_type": "macd_death_cross",
            "stop_loss_current": 213.49,
            "signal_reason": "MACD死叉: DIF下穿DEA, 浮盈1.99%"
        }),
        ("600760", {
            "buy_signal": False, "sell_signal": True,
            "sell_price": 40.21, "sell_type": "trend_break",
            "stop_loss_current": 36.51,
            "signal_reason": "趋势破位: 收盘跌破60日线且均线拐头向下"
        }),
        ("603986", {
            "buy_signal": False, "sell_signal": False,
            "stop_loss_initial": 436.73, "stop_loss_current": 436.73,
            "add_position": False, "signal_reason": "持仓观望: 浮盈-4.56%"
        }),
        ("000725", {
            "buy_signal": False, "sell_signal": False,
            "stop_loss_initial": 5.47, "stop_loss_current": 5.47,
            "add_position": False, "signal_reason": "持仓观望: 浮盈-0.08%"
        }),
    ]

    mock_holdings = {
        "002371": {"shares": 300, "buy_price": 774.57, "current_price": 677.0, "highest_price": 774.57, "sector": "半导体设备", "stock_type": "龙头"},
        "002409": {"shares": 900, "buy_price": 193.56, "current_price": 145.0, "highest_price": 193.56, "sector": "半导体材料", "stock_type": "龙头"},
        "600118": {"shares": 1700, "buy_price": 66.19, "current_price": 62.35, "highest_price": 66.19, "sector": "卫星导航", "stock_type": "弹性"},
        "600584": {"shares": 1200, "buy_price": 97.80, "current_price": 85.49, "highest_price": 97.80, "sector": "半导体封测", "stock_type": "龙头"},
        "002384": {"shares": 200, "buy_price": 237.21, "current_price": 241.92, "highest_price": 246.43, "sector": "精密制造", "stock_type": "弹性"},
        "600760": {"shares": 600, "buy_price": 40.57, "current_price": 40.21, "highest_price": 40.57, "sector": "军工航空", "stock_type": "弹性"},
        "603986": {"shares": 200, "buy_price": 485.26, "current_price": 463.15, "highest_price": 485.26, "sector": "存储芯片", "stock_type": "龙头"},
        "000725": {"shares": 8600, "buy_price": 6.08, "current_price": 6.07, "highest_price": 6.08, "sector": "面板显示", "stock_type": "弹性"},
    }

    html = generate_eastmoney_orders(mock_signals, mock_holdings)

    # 输出到文件预览
    output_path = os.path.join(config.OUTPUT_DIR, f"东方财富条件单_{datetime.date.today().strftime('%Y-%m-%d')}.html")
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML已生成: {output_path}")

    # 统计
    all_orders = []
    for code, sig in mock_signals:
        holding = mock_holdings.get(code)
        orders = _map_signal_to_order(code, sig, holding)
        all_orders.extend(orders)

    buy_count = sum(1 for o in all_orders if o["order_type"].startswith("buy"))
    sell_count = sum(1 for o in all_orders if o["order_type"].startswith("sell"))
    print(f"卖出条件单: {sell_count}个")
    print(f"买入条件单: {buy_count}个")
    print(f"总计: {len(all_orders)}个条件单")

    print("\n[OK] 东方财富条件单模块测试完成")
