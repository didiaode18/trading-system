# -*- coding: utf-8 -*-
"""
条件单生成器
============
生成条件单文本报告和 Excel 条件单文件

接口:
  generate_condition_sheet(signals) -> str  (Excel 文件路径)
  generate_simple_report(signals) -> str    (文本简报)
"""

import os
import logging
import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)


def generate_simple_report(signals):
    """
    生成文本简报（打印到控制台）

    参数:
        signals: [(code, signal_dict), ...]

    返回: 文本字符串
    """
    if not signals:
        return "  无交易信号"

    buy_signals = [(c, s) for c, s in signals if s.get("buy_signal")]
    sell_signals = [(c, s) for c, s in signals if s.get("sell_signal")]
    add_signals = [(c, s) for c, s in signals if s.get("add_position")]

    lines = []
    lines.append("=" * 55)
    lines.append(f"  交易信号摘要 ({datetime.date.today()})")
    lines.append("=" * 55)

    if buy_signals:
        lines.append(f"\n  【买入信号】{len(buy_signals)}只")
        for code, sig in buy_signals:
            name = config.get_stock_name(code)
            price = sig.get("buy_price", 0)
            stop = sig.get("stop_loss_initial", price * 0.9)
            shares = sig.get("position", {}).get("shares", 0)
            reason = sig.get("signal_reason", "")
            risk_pass = sig.get("position", {}).get("pass_risk", False)
            risk_tag = "[风控PASS]" if risk_pass else "[风控FAIL]"
            lines.append(f"    {code} {name}: 买入@{price:.2f} {shares}股 止损@{stop:.2f} {risk_tag}")
            if reason:
                lines.append(f"      原因: {reason}")

    if sell_signals:
        lines.append(f"\n  【卖出信号】{len(sell_signals)}只")
        for code, sig in sell_signals:
            name = config.get_stock_name(code)
            price = sig.get("sell_price", 0)
            reason = sig.get("signal_reason", "")
            lines.append(f"    {code} {name}: 卖出@{price:.2f}")
            if reason:
                lines.append(f"      原因: {reason}")

    if add_signals:
        lines.append(f"\n  【加仓信号】{len(add_signals)}只")
        for code, sig in add_signals:
            name = config.get_stock_name(code)
            reason = sig.get("signal_reason", "")
            lines.append(f"    {code} {name}: 加仓")
            if reason:
                lines.append(f"      原因: {reason}")

    if not buy_signals and not sell_signals and not add_signals:
        lines.append("\n  无明确交易信号，继续观望")

    lines.append("\n" + "=" * 55)
    return "\n".join(lines)


def generate_condition_sheet(signals, **kwargs):
    """
    生成条件单文件（文本格式，兼容 Excel 导入）

    参数:
        signals: [(code, signal_dict), ...]

    返回: 文件路径字符串，失败返回 None
    """
    if not signals:
        return None

    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(output_dir, exist_ok=True)
    today = datetime.date.today().strftime("%Y%m%d")
    file_path = os.path.join(output_dir, f"condition_sheet_{today}.txt")

    try:
        lines = []
        lines.append(f"# 条件单 {today}")
        lines.append(f"# 生成时间: {datetime.datetime.now().strftime('%H:%M:%S')}")
        lines.append("#")
        lines.append("# 格式: 代码|名称|操作|价格|股数|止损|原因")
        lines.append("")

        for code, sig in signals:
            name = config.get_stock_name(code)
            # FIX(P0-19): 双重守卫——风控拒绝(risk_rejected/pass_risk=False)或 shares<=0 的信号
            # 不得输出为"买入"行（main.py 已将 FAIL 降级为 buy_signal=False，此处再防御一道）。
            if sig.get("buy_signal") and not sig.get("risk_rejected") and sig.get("pass_risk", True) is not False:
                price = sig.get("buy_price", 0)
                shares = sig.get("position", {}).get("shares", 0)
                if shares <= 0:
                    continue
                stop = sig.get("stop_loss_initial", price * 0.9)
                reason = sig.get("signal_reason", "").replace("|", "/")
                lines.append(f"{code}|{name}|买入|{price:.2f}|{shares}|{stop:.2f}|{reason}")
            if sig.get("sell_signal"):
                price = sig.get("sell_price", 0)
                shares = sig.get("sell_shares", 0)
                reason = sig.get("signal_reason", "").replace("|", "/")
                lines.append(f"{code}|{name}|卖出|{price:.2f}|{shares}|0.00|{reason}")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"条件单已保存: {file_path}")
        return file_path
    except Exception as e:
        logger.error(f"条件单生成失败: {e}")
        return None
