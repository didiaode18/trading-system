# -*- coding: utf-8 -*-
"""
P2-2: 分钟级数据与日内特征框架
================================
为日内策略提供标准化的数据获取与特征计算接口。

核心功能:
  1. MinuteDataFeed: 分钟K线获取与缓存（akshare数据源）
  2. IntradayFeatures: 日内特征计算（VWAP/日内动量/量价分布/波动率曲面）
  3. 与现有日线策略的桥接接口

使用方式:
    from data.intraday_feed import MinuteDataFeed, IntradayFeatures
    feed = MinuteDataFeed()
    df_min = feed.get_minute_data("002371", period="5")
    features = IntradayFeatures(df_min)
    vwap = features.vwap()
    momentum = features.intraday_momentum()
"""

import os
import sys
import json
import logging
import datetime
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

# 分钟数据缓存目录
MINUTE_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "minute_cache"
)


class MinuteDataFeed:
    """分钟K线数据源

    支持akshare分钟K线获取，带文件缓存（当日有效）。
    """

    def __init__(self, cache_dir: str = None):
        self.cache_dir = cache_dir or MINUTE_CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)

    def get_minute_data(self, code: str, period: str = "5") -> pd.DataFrame:
        """获取分钟K线数据

        参数:
            code: 股票代码（6位数字）
            period: K线周期，"1"/"5"/"15"/"30"/"60"

        返回:
            DataFrame with columns: [datetime, open, high, low, close, volume, amount]
            失败返回空DataFrame
        """
        # 检查缓存（当日有效）
        cached = self._load_cache(code, period)
        if cached is not None:
            return cached

        if not HAS_AKSHARE:
            logger.warning("[分钟数据] akshare未安装")
            return pd.DataFrame()

        try:
            df = ak.stock_zh_a_hist_min_em(
                symbol=code,
                period=period,
                adjust="qfq"
            )
            if df is None or df.empty:
                return pd.DataFrame()

            # 标准化列名
            col_map = {
                "时间": "datetime", "日期": "datetime",
                "开盘": "open", "最高": "high", "最低": "low",
                "收盘": "close", "成交量": "volume", "成交额": "amount",
            }
            df = df.rename(columns=col_map)

            # 确保必要列存在
            required = ["datetime", "open", "high", "low", "close", "volume"]
            for col in required:
                if col not in df.columns:
                    return pd.DataFrame()

            # 缓存
            self._save_cache(code, period, df)
            return df

        except Exception as e:
            logger.debug(f"[分钟数据] {code} {period}分钟获取失败: {e}")
            return pd.DataFrame()

    def _cache_path(self, code: str, period: str) -> str:
        today = datetime.date.today().strftime("%Y%m%d")
        return os.path.join(self.cache_dir, f"{code}_{period}min_{today}.pkl")

    def _load_cache(self, code: str, period: str) -> pd.DataFrame:
        path = self._cache_path(code, period)
        if os.path.exists(path):
            try:
                return pd.read_pickle(path)
            except Exception:
                pass
        return None

    def _save_cache(self, code: str, period: str, df: pd.DataFrame):
        path = self._cache_path(code, period)
        try:
            df.to_pickle(path)
        except Exception as e:
            logger.debug(f"[分钟数据] 缓存写入失败: {e}")


class IntradayFeatures:
    """日内特征计算器

    输入: 分钟K线DataFrame (需含 datetime/open/high/low/close/volume 列)
    输出: 各类日内特征值
    """

    def __init__(self, df_min: pd.DataFrame):
        self.df = df_min.copy() if df_min is not None and not df_min.empty else pd.DataFrame()

    @property
    def available(self) -> bool:
        return not self.df.empty and len(self.df) >= 10

    def vwap(self) -> float:
        """成交量加权平均价 (VWAP)"""
        if not self.available:
            return 0.0
        typical_price = (self.df["high"] + self.df["low"] + self.df["close"]) / 3
        cum_tp_vol = (typical_price * self.df["volume"]).sum()
        cum_vol = self.df["volume"].sum()
        return round(cum_tp_vol / cum_vol, 4) if cum_vol > 0 else 0.0

    def intraday_momentum(self) -> float:
        """日内动量: (收盘 - 开盘) / 开盘 × 100"""
        if not self.available:
            return 0.0
        open_price = self.df["open"].iloc[0]
        close_price = self.df["close"].iloc[-1]
        if open_price <= 0:
            return 0.0
        return round((close_price / open_price - 1) * 100, 3)

    def volume_profile(self, n_bins: int = 5) -> dict:
        """量价分布: 将价格范围分n_bins，返回各bin的成交量占比

        返回: {"bins": [(price_low, price_high, vol_pct), ...],
               "max_volume_price": float}  最大成交量对应的价格
        """
        if not self.available:
            return {"bins": [], "max_volume_price": 0.0}

        close = self.df["close"].values
        volume = self.df["volume"].values
        price_min, price_max = close.min(), close.max()

        if price_max <= price_min:
            return {"bins": [(price_min, price_max, 1.0)], "max_volume_price": close[volume.argmax()]}

        bin_edges = np.linspace(price_min, price_max, n_bins + 1)
        bins = []
        max_vol = 0
        max_vol_price = 0

        for i in range(n_bins):
            lo, hi = bin_edges[i], bin_edges[i + 1]
            mask = (close >= lo) & (close <= hi)
            vol_pct = volume[mask].sum() / volume.sum() if volume.sum() > 0 else 0
            bins.append((round(lo, 3), round(hi, 3), round(vol_pct, 4)))
            if volume[mask].sum() > max_vol:
                max_vol = volume[mask].sum()
                max_vol_price = (lo + hi) / 2

        return {"bins": bins, "max_volume_price": round(max_vol_price, 3)}

    def intraday_volatility(self) -> dict:
        """日内波动率特征

        返回: {
            "realized_vol": float,     # 已实现波动率(年化)
            "high_low_range": float,   # 振幅(%)
            "upper_shadow_pct": float, # 上影线占比(%)
            "lower_shadow_pct": float, # 下影线占比(%)
        }
        """
        if not self.available:
            return {"realized_vol": 0.0, "high_low_range": 0.0,
                    "upper_shadow_pct": 0.0, "lower_shadow_pct": 0.0}

        returns = self.df["close"].pct_change().dropna()
        realized_vol = returns.std() * np.sqrt(252 * 48) if len(returns) > 1 else 0  # 5min→48 bars/day

        day_open = self.df["open"].iloc[0]
        day_high = self.df["high"].max()
        day_low = self.df["low"].min()
        day_close = self.df["close"].iloc[-1]

        hl_range = (day_high - day_low) / day_open * 100 if day_open > 0 else 0
        upper_shadow = (day_high - max(day_open, day_close)) / day_open * 100 if day_open > 0 else 0
        lower_shadow = (min(day_open, day_close) - day_low) / day_open * 100 if day_open > 0 else 0

        return {
            "realized_vol": round(realized_vol, 4),
            "high_low_range": round(hl_range, 3),
            "upper_shadow_pct": round(max(0, upper_shadow), 3),
            "lower_shadow_pct": round(max(0, lower_shadow), 3),
        }

    def morning_strength(self) -> float:
        """早盘强度: 前30分钟涨跌幅(%)"""
        if not self.available:
            return 0.0
        # 取前30分钟的数据（5分钟K线约6根）
        n_bars = min(6, len(self.df))
        morning_open = self.df["open"].iloc[0]
        morning_close = self.df["close"].iloc[n_bars - 1]
        if morning_open <= 0:
            return 0.0
        return round((morning_close / morning_open - 1) * 100, 3)

    def afternoon_reversal(self) -> float:
        """午后反转: 下午涨跌幅 - 上午涨跌幅(%)"""
        if not self.available or len(self.df) < 20:
            return 0.0
        mid = len(self.df) // 2
        am_change = (self.df["close"].iloc[mid - 1] / self.df["open"].iloc[0] - 1) * 100
        pm_change = (self.df["close"].iloc[-1] / self.df["close"].iloc[mid - 1] - 1) * 100
        return round(pm_change - am_change, 3)

    def compute_all(self) -> dict:
        """计算全部日内特征，返回汇总字典"""
        return {
            "vwap": self.vwap(),
            "intraday_momentum": self.intraday_momentum(),
            "volume_profile": self.volume_profile(),
            "intraday_volatility": self.intraday_volatility(),
            "morning_strength": self.morning_strength(),
            "afternoon_reversal": self.afternoon_reversal(),
        }
