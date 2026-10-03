# -*- coding: utf-8 -*-
"""
聪明钱因子（Smart Money Factor）
================================
核心逻辑: 捕捉机构资金隐秘建仓，大单净流入的边际变化

构造:
1. big_money_ratio = (super_net + big_net) / total_amount
2. smart_money = MA(5) - MA(20) of big_money_ratio
3. 方向: 正向（聪明钱流入越多越好）

预期: IR≈2.7, 月度胜率82%
数据源: capital_flow 表 (main_net/super_net/big_net)
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_smart_money_flow(capital_flow_df: pd.DataFrame,
                          amount_series: pd.Series = None,
                          window_short: int = 5,
                          window_long: int = 20) -> pd.Series:
    """
    计算聪明钱因子（时序版本，单只股票）

    参数:
        capital_flow_df: 资金流向DataFrame，含 date/main_net/super_net/big_net 列
        amount_series: 成交额序列（用于归一化），None则不做归一化
        window_short: 短期均线窗口
        window_long: 长期均线窗口

    返回:
        聪明钱因子值序列（与输入等长）
    """
    if capital_flow_df is None or capital_flow_df.empty:
        return pd.Series(dtype=float)

    # 大单+超大单净流入
    if "super_net" in capital_flow_df.columns and "big_net" in capital_flow_df.columns:
        big_flow = capital_flow_df["super_net"].fillna(0) + capital_flow_df["big_net"].fillna(0)
    elif "main_net" in capital_flow_df.columns:
        big_flow = capital_flow_df["main_net"].fillna(0)
    else:
        return pd.Series(dtype=float)

    # 归一化（如有成交额）
    if amount_series is not None and len(amount_series) == len(big_flow):
        amount = amount_series.replace(0, np.nan)
        ratio = big_flow / amount
    else:
        ratio = big_flow

    # 边际变化: 短期均线 - 长期均线
    ma_short = ratio.rolling(window_short, min_periods=1).mean()
    ma_long = ratio.rolling(window_long, min_periods=1).mean()
    smart_money = ma_short - ma_long

    return smart_money


def calc_smart_money_cross_section(codes: list,
                                   capital_flow_dict: dict,
                                   amount_dict: dict = None) -> pd.Series:
    """
    计算截面聪明钱因子（多只股票同一时点）

    参数:
        codes: 股票代码列表
        capital_flow_dict: {code: capital_flow_df}
        amount_dict: {code: amount_series}（可选）

    返回:
        pd.Series, index=code, value=聪明钱因子值
    """
    result = {}
    for code in codes:
        cf = capital_flow_dict.get(code)
        if cf is None or cf.empty:
            continue
        amt = amount_dict.get(code) if amount_dict else None
        try:
            sm = calc_smart_money_flow(cf, amt)
            if len(sm) > 0 and not np.isnan(sm.iloc[-1]):
                result[code] = sm.iloc[-1]
        except Exception as e:
            logger.debug(f"[聪明钱] {code}计算失败: {e}")

    return pd.Series(result)


# ============================================================
# FactorRegistry 标准接口（返回与df等长的Series）
# ============================================================

def calc_smf_proxy(df: pd.DataFrame) -> pd.Series:
    """
    聪明钱因子的量价代理版本（无需capital_flow数据时用）

    用大单成交占比的边际变化近似聪明钱行为:
    - 放量上涨日 = 机构买入信号
    - 缩量下跌日 = 机构未卖出信号
    - 两者差值 = 聪明钱净买入代理

    参数:
        df: 含 close/volume 列的DataFrame

    返回:
        pd.Series（与df等长）
    """
    close = df["close"].values
    volume = df["volume"].values
    n = len(close)
    result = np.full(n, np.nan)

    if n < 20:
        return pd.Series(result, index=df.index)

    for i in range(19, n):
        # 放量上涨: 价涨且量>均量
        price_up = close[i] > close[i - 1]
        vol_ratio = volume[i] / np.mean(volume[max(0, i-19):i]) if np.mean(volume[max(0, i-19):i]) > 0 else 1

        # 缩量下跌: 价跌且量<均量
        price_down = close[i] < close[i - 1]
        vol_shrink = volume[i] < np.mean(volume[max(0, i-19):i])

        # 聪明钱代理: 放量上涨得分 - 缩量下跌得分
        buy_signal = 1.0 if (price_up and vol_ratio > 1.2) else 0.0
        hold_signal = 0.5 if (price_down and vol_shrink) else 0.0

        result[i] = buy_signal - hold_signal

    # 平滑
    series = pd.Series(result, index=df.index)
    return series.rolling(5, min_periods=1).mean()


# 因子注册字典
SMART_MONEY_FACTORS = {
    "smf_proxy": {
        "func": calc_smf_proxy,
        "desc": "聪明钱因子(量价代理)",
        "dir": 1,
    },
}
