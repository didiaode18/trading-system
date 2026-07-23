"""
历史数据馈送模块
================
为回测引擎提供按日期逐日推送的行情数据：
- 从SQLite数据库或DataFrame字典加载历史数据
- 按交易日逐日推送，模拟真实行情到达
- 支持多股票并行馈送
- 自动计算前收盘价（用于涨跌停判断）
"""

import logging
import pandas as pd
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)


class DataFeed:
    """
    历史数据馈送器
    
    用法:
        feed = DataFeed(data_dict, start_date, end_date)
        for date in feed.trading_dates:
            bar = feed.get_bar(code, date)  # 获取当日K线
            history = feed.get_history(code, date, lookback=60)  # 获取历史
    """

    def __init__(self, data_dict: dict, start_date: str = None, end_date: str = None):
        """
        参数:
            data_dict: {code: DataFrame} 历史日线数据
                       DataFrame列: date, open, close, high, low, volume
            start_date: 回测开始日期 (YYYY-MM-DD)
            end_date: 回测结束日期
        """
        self.data_dict = {}
        self.trading_dates: list[str] = []
        self._date_index: dict[str, dict] = {}  # {code: {date: row_dict}}

        self._prepare_data(data_dict, start_date, end_date)

    def _prepare_data(self, data_dict: dict, start_date: str, end_date: str):
        """预处理数据：排序、建索引、计算前收盘价"""
        all_dates = set()

        for code, df in data_dict.items():
            if df is None or df.empty:
                continue

            # 确保列名统一
            df = df.copy()
            df.columns = [c.lower() for c in df.columns]

            # 确保date列为字符串
            if "date" in df.columns:
                df["date"] = df["date"].astype(str).str[:10]

            # 排序
            df = df.sort_values("date").reset_index(drop=True)

            # 计算前收盘价
            if "pre_close" not in df.columns:
                df["pre_close"] = df["close"].shift(1)
                df["pre_close"] = df["pre_close"].fillna(df["close"])

            # 计算涨跌幅
            if "change_pct" not in df.columns:
                df["change_pct"] = (df["close"] - df["pre_close"]) / df["pre_close"]
                df["change_pct"] = df["change_pct"].fillna(0)

            self.data_dict[code] = df

            # 建立日期索引
            self._date_index[code] = {}
            for idx, row in df.iterrows():
                d = row["date"]
                self._date_index[code][d] = row.to_dict()
                all_dates.add(d)

        # 确定交易日序列
        self.trading_dates = sorted(all_dates)
        if start_date:
            self.trading_dates = [d for d in self.trading_dates if d >= start_date]
        if end_date:
            self.trading_dates = [d for d in self.trading_dates if d <= end_date]

        logger.info(f"DataFeed: {len(self.data_dict)}只股票, "
                   f"{len(self.trading_dates)}个交易日 "
                   f"({self.trading_dates[0] if self.trading_dates else 'N/A'} ~ "
                   f"{self.trading_dates[-1] if self.trading_dates else 'N/A'})")

    def get_bar(self, code: str, date: str) -> Optional[dict]:
        """
        获取某只股票某日的K线数据
        
        返回:
            {"date","open","close","high","low","volume","pre_close","change_pct"}
            或 None（该日无数据）
        """
        if code not in self._date_index:
            return None
        return self._date_index[code].get(date)

    def get_history(self, code: str, date: str, lookback: int = 60) -> Optional[pd.DataFrame]:
        """
        获取截止到某日的历史数据（含当日）
        
        参数:
            code: 股票代码
            date: 截止日期
            lookback: 回看天数
        
        返回:
            DataFrame 或 None
        """
        if code not in self.data_dict:
            return None

        df = self.data_dict[code]
        mask = df["date"] <= date
        history = df[mask].tail(lookback).copy()

        if history.empty:
            return None
        return history

    def get_full_history(self, code: str, date: str) -> Optional[pd.DataFrame]:
        """获取截止到某日的全部历史数据"""
        if code not in self.data_dict:
            return None
        df = self.data_dict[code]
        return df[df["date"] <= date].copy()

    def get_price_dict(self, date: str) -> dict:
        """获取某日所有股票的收盘价字典 {code: close_price}"""
        prices = {}
        for code in self.data_dict:
            bar = self.get_bar(code, date)
            if bar:
                prices[code] = bar["close"]
        return prices

    def get_codes_with_data(self, date: str) -> list:
        """获取某日有数据的股票代码列表"""
        codes = []
        for code in self.data_dict:
            if date in self._date_index.get(code, {}):
                codes.append(code)
        return codes

    @property
    def stock_codes(self) -> list:
        """所有股票代码"""
        return list(self.data_dict.keys())

    @property
    def date_range(self) -> tuple:
        """日期范围"""
        if not self.trading_dates:
            return ("", "")
        return (self.trading_dates[0], self.trading_dates[-1])

    def get_benchmark_data(self, benchmark_code: str = "000300") -> Optional[pd.DataFrame]:
        """获取基准指数数据（如果包含在data_dict中）"""
        return self.data_dict.get(benchmark_code)


def load_data_from_db(stock_codes: list, start_date: str, end_date: str,
                      db_path: str = None) -> dict:
    """
    从SQLite数据库加载历史数据
    
    参数:
        stock_codes: 股票代码列表
        start_date: 开始日期
        end_date: 结束日期
        db_path: 数据库路径
    
    返回:
        {code: DataFrame}
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config
    from data.data_loader import load_daily_data

    if db_path is None:
        db_path = config.DB_PATH

    data_dict = {}
    for code in stock_codes:
        try:
            df = load_daily_data(code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                data_dict[code] = df
        except Exception as e:
            logger.warning(f"加载 {code} 数据失败: {e}")

    return data_dict
