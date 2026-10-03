"""
TA-Lib 技术指标封装层
=====================
统一封装 TA-Lib 高效C底层计算的技术指标，补充现有因子库缺失的指标。
所有函数签名与现有因子库一致: (df: DataFrame) -> Series

设计原则:
  - 不替换现有手写指标（避免回归风险）
  - 仅做增量补充: ADX, OBV, Williams%R, MFI, SAR 等
  - TA-Lib 不可用时自动降级为 pandas 手写实现
  - 注册到 FactorRegistry 供全系统使用

安装: pip install TA-Lib  (需预编译二进制, Windows 从 .whl 安装)
"""

import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)

# TA-Lib 可用性检测
_TALIB_AVAILABLE = False
try:
    import talib
    _TALIB_AVAILABLE = True
    logger.debug("TA-Lib 已加载, 使用C底层加速")
except ImportError:
    logger.info("TA-Lib 未安装, 使用 pandas 降级实现 (pip install TA-Lib 可启用加速)")


def _require_high_low_close(df):
    """校验 DataFrame 必须包含 high/low/close 列"""
    for col in ("high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"DataFrame 缺少必要列: {col}")


# ============================================================
# ADX — 平均趋向指标 (Average Directional Index)
# ============================================================
def adx_14(df, period: int = 14) -> pd.Series:
    """
    ADX趋势强度指标
    ADX > 25: 趋势明确 (适合趋势策略)
    ADX < 20: 震荡市 (适合均值回归)
    """
    _require_high_low_close(df)
    if _TALIB_AVAILABLE:
        return pd.Series(
            talib.ADX(df["high"].values, df["low"].values, df["close"].values, timeperiod=period),
            index=df.index, name=f"adx_{period}"
        )
    # pandas 降级
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    # 当 +DM > -DM 时 -DM=0, 反之亦然
    mask = plus_dm > minus_dm
    minus_dm[mask] = 0
    plus_dm[~mask] = 0
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.rolling(period).mean()
    return adx.rename(f"adx_{period}")


# ============================================================
# OBV — 能量潮指标 (On Balance Volume)
# ============================================================
def obv(df) -> pd.Series:
    """
    OBV能量潮: 量价配合验证
    OBV 上升 + 价格上升 = 趋势确认
    OBV 下降 + 价格上升 = 顶背离 (看跌)
    """
    if "volume" not in df.columns:
        raise ValueError("DataFrame 缺少 volume 列")
    if _TALIB_AVAILABLE:
        return pd.Series(
            talib.OBV(df["close"].values, df["volume"].values.astype(float)),
            index=df.index, name="obv"
        )
    close = df["close"]
    vol = df["volume"].astype(float)
    direction = np.sign(close.diff())
    direction.iloc[0] = 0
    obv_vals = (vol * direction).cumsum()
    return obv_vals.rename("obv")


# ============================================================
# Williams %R — 威廉指标
# ============================================================
def williams_r(df, period: int = 14) -> pd.Series:
    """
    Williams %R: 超买超卖指标
    > -20: 超买区
    < -80: 超卖区
    与RSI互补，对极端价格更敏感
    """
    _require_high_low_close(df)
    if _TALIB_AVAILABLE:
        return pd.Series(
            talib.WILLR(df["high"].values, df["low"].values, df["close"].values, timeperiod=period),
            index=df.index, name=f"williams_r_{period}"
        )
    high = df["high"]
    low = df["low"]
    close = df["close"]
    hh = high.rolling(period).max()
    ll = low.rolling(period).min()
    wr = -100 * (hh - close) / (hh - ll)
    return wr.rename(f"williams_r_{period}")


# ============================================================
# MFI — 资金流量指标 (Money Flow Index)
# ============================================================
def mfi(df, period: int = 14) -> pd.Series:
    """
    MFI资金流量: 量价结合的RSI
    > 80: 超买 (可能见顶)
    < 20: 超卖 (可能见底)
    比普通RSI多了成交量权重
    """
    _require_high_low_close(df)
    if "volume" not in df.columns:
        raise ValueError("DataFrame 缺少 volume 列")
    if _TALIB_AVAILABLE:
        return pd.Series(
            talib.MFI(df["high"].values, df["low"].values, df["close"].values,
                      df["volume"].values.astype(float), timeperiod=period),
            index=df.index, name=f"mfi_{period}"
        )
    high, low, close = df["high"], df["low"], df["close"]
    vol = df["volume"].astype(float)
    tp = (high + low + close) / 3
    mf = tp * vol
    pos_mf = pd.Series(np.where(tp > tp.shift(1), mf, 0), index=df.index)
    neg_mf = pd.Series(np.where(tp < tp.shift(1), mf, 0), index=df.index)
    ratio = pos_mf.rolling(period).sum() / neg_mf.rolling(period).sum()
    mfi_val = 100 - 100 / (1 + ratio)
    return mfi_val.rename(f"mfi_{period}")


# ============================================================
# SAR — 抛物线转向 (Parabolic SAR)
# ============================================================
def sar(df, acceleration: float = 0.02, maximum: float = 0.2) -> pd.Series:
    """
    Parabolic SAR: 趋势跟踪止损指标
    SAR < close: 多头趋势 (持有)
    SAR > close: 空头趋势 (离场)
    可作为移动止损的参考
    """
    _require_high_low_close(df)
    if _TALIB_AVAILABLE:
        return pd.Series(
            talib.SAR(df["high"].values, df["low"].values,
                      acceleration=acceleration, maximum=maximum),
            index=df.index, name="sar"
        )
    # pandas 降级 (简化版)
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    n = len(close)
    sar_vals = np.full(n, np.nan)
    if n < 2:
        return pd.Series(sar_vals, index=df.index, name="sar")
    # 初始趋势: 价格上涨为多头
    is_long = close[1] > close[0]
    sar_vals[0] = low[0] if is_long else high[0]
    af = acceleration
    ep = high[0] if is_long else low[0]
    for i in range(1, n):
        prev_sar = sar_vals[i - 1]
        if is_long:
            sar_vals[i] = prev_sar + af * (ep - prev_sar)
            sar_vals[i] = min(sar_vals[i], low[i - 1])
            if i >= 2:
                sar_vals[i] = min(sar_vals[i], low[i - 2])
            if low[i] < sar_vals[i]:
                is_long = False
                sar_vals[i] = ep
                ep = low[i]
                af = acceleration
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + acceleration, maximum)
        else:
            sar_vals[i] = prev_sar + af * (ep - prev_sar)
            sar_vals[i] = max(sar_vals[i], high[i - 1])
            if i >= 2:
                sar_vals[i] = max(sar_vals[i], high[i - 2])
            if high[i] > sar_vals[i]:
                is_long = True
                sar_vals[i] = ep
                ep = high[i]
                af = acceleration
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + acceleration, maximum)
    return pd.Series(sar_vals, index=df.index, name="sar")


# ============================================================
# AROON — 阿隆指标 (趋势强度+方向)
# ============================================================
def aroon(df, period: int = 25) -> pd.DataFrame:
    """
    Aroon Up/Down: 趋势强度和方向
    Aroon Up > 70 且 Aroon Down < 30: 强上升趋势
    Aroon Down > 70 且 Aroon Up < 30: 强下降趋势
    """
    _require_high_low_close(df)
    high = df["high"]
    low = df["low"]
    # 距离最高/最低点的天数
    high_idx = high.rolling(period + 1).apply(lambda x: x.argmax(), raw=True)
    low_idx = low.rolling(period + 1).apply(lambda x: x.argmin(), raw=True)
    aroon_up = 100 * (period - high_idx) / period
    aroon_down = 100 * (period - low_idx) / period
    result = pd.DataFrame({
        f"aroon_up_{period}": aroon_up,
        f"aroon_down_{period}": aroon_down,
    }, index=df.index)
    return result


# ============================================================
# 因子注册: 将所有 TA-Lib 封装指标注册到 FactorRegistry
# ============================================================
def register_talib_factors(registry):
    """
    将 TA-Lib 封装指标批量注册到因子注册表

    使用方式:
        from factors.registry import FactorRegistry
        from factors.ta_lib_wrapper import register_talib_factors
        reg = FactorRegistry()
        register_talib_factors(reg)
    """
    factors_to_register = [
        # (name, category, func, description, direction)
        ("adx_14", "technical", adx_14, "ADX趋势强度(14日)", 1),
        ("obv", "volume", obv, "OBV能量潮", 1),
        ("williams_r_14", "technical", williams_r, "Williams%R超买超卖(14日)", -1),
        ("mfi_14", "volume", mfi, "MFI资金流量(14日)", 1),
        ("sar", "technical", sar, "Parabolic SAR抛物线转向", -1),
    ]
    for name, cat, func, desc, direction in factors_to_register:
        try:
            registry.register(name, cat, func, description=desc, direction=direction)
            logger.debug(f"TA-Lib因子已注册: {name} ({cat})")
        except Exception as e:
            logger.warning(f"TA-Lib因子注册失败 {name}: {e}")

    logger.info(f"TA-Lib封装层注册完成 ({len(factors_to_register)}个指标, "
                f"TA-Lib可用: {_TALIB_AVAILABLE})")
