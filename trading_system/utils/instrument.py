# -*- coding: utf-8 -*-
"""
统一品种类型系统 V1.0
=====================
消除全系统散落的 startswith('5')/startswith('1') 判定，
提供单一事实源的品种类型查询。

品种分类:
  STOCK     - A股个股（沪深主板/创业板/科创板）
  ETF       - 交易型开放式指数基金
  BOND      - 可转债
  INDEX     - 指数（沪深300/中证500等）
  FUND      - 其他基金
  UNKNOWN   - 未识别

使用:
    from utils.instrument import InstrumentType, get_instrument_type, is_etf, is_stock

    if is_etf(code):
        ...
    inst_type = get_instrument_type(code)
    if inst_type == InstrumentType.ETF:
        ...
"""

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class InstrumentType(Enum):
    """品种类型枚举"""
    STOCK = "stock"
    ETF = "etf"
    BOND = "bond"       # 可转债
    INDEX = "index"
    FUND = "fund"
    UNKNOWN = "unknown"


# ============================================================
# ETF 代码前缀规则（精确匹配，避免与可转债冲突）
# ============================================================
# 上海ETF: 51xxxx, 56xxxx, 58xxxx
# 深圳ETF: 159xxx
# 注意: 11xxxx/12xxxx 是可转债，不是ETF！
ETF_PREFIXES = (
    "510", "511", "512", "513", "515",  # 上海ETF（行业/跨境/主题）
    "560", "562", "563", "568",          # 上海ETF（新材料/半导体等）
    "588",                                # 上海ETF（科创板系列）
    "159",                                # 深圳ETF（创业板/主题系列）
)

# 可转债前缀（用于排除）
BOND_PREFIXES = (
    "113", "118",  # 上海可转债
    "123", "127", "128",  # 深圳可转债
)

# 指数代码
INDEX_CODES = {
    "000300",  # 沪深300
    "000905",  # 中证500
    "000852",  # 中证1000
    "399001",  # 深证成指
    "399006",  # 创业板指
    "399673",  # 创业板50
    "000016",  # 上证50
    "000688",  # 科创50
}


def get_instrument_type(code: str) -> InstrumentType:
    """根据代码判断品种类型（单一事实源）

    参数:
        code: 6位证券代码

    返回:
        InstrumentType 枚举值
    """
    if not code or len(code) < 3:
        return InstrumentType.UNKNOWN

    # 指数
    if code in INDEX_CODES:
        return InstrumentType.INDEX

    # 可转债（优先于ETF判定，避免12xxxx被误判）
    for prefix in BOND_PREFIXES:
        if code.startswith(prefix):
            return InstrumentType.BOND

    # ETF
    for prefix in ETF_PREFIXES:
        if code.startswith(prefix):
            return InstrumentType.ETF

    # 股票
    if code.startswith(("000", "001", "002", "003")):
        return InstrumentType.STOCK  # 深圳主板/中小板
    if code.startswith(("300", "301")):
        return InstrumentType.STOCK  # 创业板
    if code.startswith(("600", "601", "603", "605")):
        return InstrumentType.STOCK  # 上海主板
    if code.startswith("688"):
        return InstrumentType.STOCK  # 科创板
    # BUG-038修复: 北交所 8xx/4xx/920xxx 识别为股票
    if code.startswith(("8", "4", "920")):
        return InstrumentType.STOCK  # 北交所

    # 其他以5开头但不是ETF的（可能是封闭式基金等）
    if code.startswith("5"):
        return InstrumentType.FUND

    # 其他以1开头但不是ETF/可转债的
    if code.startswith("1"):
        return InstrumentType.FUND

    return InstrumentType.UNKNOWN


def is_etf(code: str) -> bool:
    """判断是否为ETF品种（快捷方法）"""
    return get_instrument_type(code) == InstrumentType.ETF


def is_stock(code: str) -> bool:
    """判断是否为个股（快捷方法）"""
    return get_instrument_type(code) == InstrumentType.STOCK


def is_bond(code: str) -> bool:
    """判断是否为可转债（快捷方法）"""
    return get_instrument_type(code) == InstrumentType.BOND


# ============================================================
# T+0 / T+1 回转交易判定（FIX P2-17）
# ============================================================
# A股 T+0 品种（当日买入可当日卖出）：
#   可转债(BOND)、跨境/QDII ETF(513)、货币/债券 ETF(511)、商品/黄金 ETF(518)
# T+1 品种：个股、普通股票型 ETF(510/512/515/560/588/159 等)
# 注意：511/513/518 虽以 5 开头，但属 T+0，需先于通用 ETF 判定。
T0_PREFIXES = ("511", "513", "518")


def is_t0(code: str) -> bool:
    """判断品种是否 T+0（当日可回转交易）

    T+0: 可转债 / 跨境 ETF(513) / 货币、债券 ETF(511) / 商品 ETF(518)
    T+1: 个股、普通股票型 ETF

    回测/模拟盘据此决定是否施加 T+1 冻结；原实现统一 T+1，
    对 T+0 品种偏保守失真。
    """
    if not code:
        return False
    if is_bond(code):
        return True
    for prefix in T0_PREFIXES:
        if code.startswith(prefix):
            return True
    return False


# ============================================================
# 品种属性查询（费率、仓位限制等）
# ============================================================

def get_stamp_tax_rate(code: str) -> float:
    """获取品种的印花税税率

    ETF/基金免印花税，仅个股卖出收取
    """
    if is_etf(code) or is_bond(code):
        return 0.0
    # 个股: 从config读取
    try:
        import config
        return getattr(config, 'STAMP_TAX_RATE', 0.001)
    except ImportError:
        return 0.001


def get_commission_rate(code: str) -> float:
    """获取品种的佣金费率"""
    try:
        import config
        return getattr(config, 'COMMISSION_RATE', 0.0003)
    except ImportError:
        return 0.0003


def get_max_single_position(code: str) -> float:
    """获取品种的单只最大仓位上限

    V28: ETF按子类型差异化——宽基25%/行业15%/主题12%
    个股: 15%
    可转债: 10% (T+0但波动大)
    """
    inst_type = get_instrument_type(code)
    try:
        import config
        if inst_type == InstrumentType.ETF:
            # V28: 按ETF子类型差异化仓位上限
            _subtype = get_etf_subtype(code)
            _by_subtype = getattr(config, 'ETF_MAX_RATIO_BY_SUBTYPE', {})
            if _subtype and _subtype in _by_subtype:
                return _by_subtype[_subtype]
            return getattr(config, 'MAX_SINGLE_ETF_RATIO', 0.20)
        elif inst_type == InstrumentType.BOND:
            return getattr(config, 'MAX_SINGLE_BOND_RATIO', 0.10)
        else:
            return getattr(config, 'MAX_SINGLE_STOCK_RATIO', 0.15)
    except ImportError:
        return 0.15


def get_etf_subtype(code: str) -> str:
    """获取ETF子类型（行业/宽基/主题）

    兼容config.get_etf_type()，优先使用config中的分类规则
    """
    try:
        import config
        if hasattr(config, 'get_etf_type'):
            return config.get_etf_type(code)
    except ImportError:
        pass
    return ""
