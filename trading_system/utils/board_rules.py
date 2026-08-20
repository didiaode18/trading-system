# -*- coding: utf-8 -*-
"""
板块涨跌停规则（统一判定函数）
================================
A股不同板块涨跌停幅度：
  - 主板（60xxxx/000xxx/001xxx/002xxx）: ±10%
  - 创业板（300xxx/301xxx）: ±20%
  - 科创板（688xxx）: ±20%
  - ST股（名称含 ST）: ±5%
  - 北交所（8xxxxx/4xxxxx）: ±30%

使用:
    from utils.board_rules import get_limit_pct, is_limit_up, is_limit_down
    pct = get_limit_pct("300750", "宁德时代")   # → 0.20
    up  = is_limit_up("600584", 11.0, 10.0)     # → True (主板+10%)
"""
import logging

logger = logging.getLogger(__name__)

# 默认容差（收盘价可能因四舍五入差1分钱）
_DEFAULT_TOL = 0.002


def get_limit_pct(code: str, name: str = "") -> float:
    """返回该标的涨跌停幅度（小数形式，如0.10=10%）

    参数:
        code: 6位股票代码
        name: 股票名称（用于ST判定，可选）
    """
    code = str(code).zfill(6)
    name_upper = (name or "").upper()

    # ST股: 名称含 ST / *ST → 5%
    if "ST" in name_upper:
        return 0.05

    # 创业板 300xxx / 301xxx → 20%
    if code.startswith("300") or code.startswith("301"):
        return 0.20

    # 科创板 688xxx → 20%
    if code.startswith("688"):
        return 0.20

    # 北交所 8xxxxx / 4xxxxx → 30%
    if code.startswith("8") or code.startswith("4"):
        return 0.30

    # 主板（60xxxx / 000xxx / 001xxx / 002xxx / 003xxx）→ 10%
    return 0.10


def is_limit_up(code: str, price: float, pre_close: float,
                name: str = "", tol: float = _DEFAULT_TOL) -> bool:
    """判断是否涨停

    参数:
        code: 6位股票代码
        price: 当前价（通常为收盘价）
        pre_close: 昨收价
        name: 股票名称（可选，用于ST判定）
        tol: 容差（默认0.2%，处理四舍五入）

    返回:
        True=涨停
    """
    if pre_close <= 0:
        return False
    limit_pct = get_limit_pct(code, name)
    change_pct = (price - pre_close) / pre_close
    return change_pct >= limit_pct - tol


def is_limit_down(code: str, price: float, pre_close: float,
                  name: str = "", tol: float = _DEFAULT_TOL) -> bool:
    """判断是否跌停"""
    if pre_close <= 0:
        return False
    limit_pct = get_limit_pct(code, name)
    change_pct = (price - pre_close) / pre_close
    return change_pct <= -(limit_pct - tol)
