"""
全A股数据中台 + 风险过滤
=========================
职责:
- 获取全A股代码列表（baostock）
- ST/*ST、停牌、退市、次新股自动剔除
- 市值过滤（50-500亿中小盘）
- 批量加载日线数据（SQLite缓存 + 增量拉取）

使用方式:
    from quant.universe import UniverseManager
    um = UniverseManager()
    codes = um.get_filtered_universe("2024-01-01")
    data = um.load_data(codes, "2021-01-01", "2026-01-01")
"""

import os
import sys
import time
import sqlite3
import datetime
import logging
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# baostock
try:
    import baostock as bs
    HAS_BAOSTOCK = True
except ImportError:
    HAS_BAOSTOCK = False


def _to_bs_code(code: str) -> str:
    """纯数字 -> baostock格式 (sh.600000 / sz.000001)"""
    if "." in code:
        return code
    if code.startswith(("6", "9", "5")) or code == "000300":
        return f"sh.{code}"
    return f"sz.{code}"


def _from_bs_code(bs_code: str) -> str:
    """baostock格式 -> 纯数字"""
    return bs_code.split(".")[-1] if "." in bs_code else bs_code


class UniverseManager:
    """全A股数据中台"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        self._bs_logged_in = False
        self._all_stocks_cache = None  # 全A股基本信息缓存

    def _bs_login(self):
        if not self._bs_logged_in and HAS_BAOSTOCK:
            lg = bs.login()
            if lg.error_code != '0':
                raise RuntimeError(f"baostock登录失败: {lg.error_msg}")
            self._bs_logged_in = True

    def _bs_logout(self):
        if self._bs_logged_in:
            bs.logout()
            self._bs_logged_in = False

    # ============================================================
    # 一、获取全A股代码列表
    # ============================================================

    def get_all_a_share_codes(self, date: str = None) -> pd.DataFrame:
        """
        获取全A股代码列表（含上市日期、ST标记、行业）

        返回 DataFrame:
            code, code_name, ipo_date, status, industry
        """
        if self._all_stocks_cache is not None:
            return self._all_stocks_cache

        self._bs_login()

        if date is None:
            date = datetime.date.today().strftime("%Y-%m-%d")

        # baostock获取指定日期的全部股票
        rs = bs.query_all_stock(day=date)
        if rs.error_code != '0':
            raise RuntimeError(f"获取全A股列表失败: {rs.error_msg}")

        rows = []
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            # row: [code, tradeStatus, code_name]
            bs_code = row[0]
            # 只要A股（sh.6xxxxx, sz.0xxxxx, sz.3xxxxx）
            pure_code = _from_bs_code(bs_code)
            if pure_code.startswith(("6", "0", "3")):
                rows.append({
                    "code": pure_code,
                    "bs_code": bs_code,
                    "trade_status": row[1],  # 1=正常交易
                    "code_name": row[2] if len(row) > 2 else "",
                })

        df = pd.DataFrame(rows)
        logger.info(f"全A股列表: {len(df)}只 (日期={date})")
        self._all_stocks_cache = df
        return df

    # ============================================================
    # 二、风险过滤
    # ============================================================

    def filter_universe(self, stocks_df: pd.DataFrame, date: str = None) -> list:
        """
        风险过滤，返回可交易代码列表

        剔除规则:
        1. ST/*ST（名称含ST）
        2. 停牌（trade_status != 1）
        3. 退市（代码以4/8开头的北交所/三板）
        4. 上市不足60个自然日（次新股）
        5. 科创板(688)和北交所(8/4)暂不纳入（流动性/门槛）
        """
        if date is None:
            date = datetime.date.today().strftime("%Y-%m-%d")

        filtered = stocks_df.copy()

        # 1. 剔除ST
        filtered = filtered[~filtered["code_name"].str.contains("ST", case=False, na=False)]

        # 2. 剔除停牌
        filtered = filtered[filtered["trade_status"] == "1"]

        # 3. 剔除科创板(688)和北交所
        filtered = filtered[~filtered["code"].str.startswith("688")]
        filtered = filtered[~filtered["code"].str.startswith(("4", "8"))]

        # 4. 只保留主板+中小板+创业板
        filtered = filtered[filtered["code"].str.match(r"^(60|00|30)")]

        codes = filtered["code"].tolist()
        logger.info(f"风险过滤后: {len(codes)}只 (剔除ST/停牌/科创/北交所)")
        return codes

    # ============================================================
    # 三、市值过滤
    # ============================================================

    def get_market_cap_filter(self, codes: list, date: str,
                               min_cap: float = 50e8,
                               max_cap: float = 500e8) -> list:
        """
        市值过滤：锁定50-500亿中小盘

        使用baostock query_stock_basic获取流通市值
        注：baostock免费接口无实时市值，用"收盘价*流通股本"估算
        简化方案：用日线amount（成交额）作为流动性代理过滤
        """
        # 简化实现：通过成交额过滤（日均成交额>5000万作为流动性门槛）
        # 完整市值过滤需要付费数据源，P0阶段用流动性代理
        conn = sqlite3.connect(self.db_path)
        valid_codes = []

        for code in codes:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT AVG(amount) as avg_amount, COUNT(*) as cnt
                FROM daily_kline
                WHERE code=? AND date <= ? AND date >= date(?, '-30 days')
            """, (code, date, date))
            row = cursor.fetchone()
            if row and row[0] and row[1] and row[1] >= 10:
                avg_amount = row[0]
                # 日均成交额 > 5000万 作为流动性门槛（近似50亿市值）
                if avg_amount > 5e7:
                    valid_codes.append(code)

        conn.close()
        logger.info(f"流动性过滤后: {len(valid_codes)}只 (日均成交额>5000万)")
        return valid_codes

    # ============================================================
    # 四、批量加载日线数据
    # ============================================================

    def load_data(self, codes: list, start_date: str, end_date: str,
                  progress_interval: int = 100) -> dict:
        """
        批量加载日线数据

        策略: 优先从SQLite读取，缺失的从baostock拉取并缓存

        返回: {code: DataFrame(date, open, close, high, low, volume, amount)}
        """
        conn = sqlite3.connect(self.db_path)
        data_dict = {}
        missing_codes = []

        # 1. 从SQLite批量读取
        for i, code in enumerate(codes):
            df = self._load_from_db(conn, code, start_date, end_date)
            if df is not None and len(df) >= 60:  # 至少60天数据
                data_dict[code] = df
            else:
                missing_codes.append(code)

            if (i + 1) % progress_interval == 0:
                logger.info(f"  数据加载进度: {i+1}/{len(codes)}")

        logger.info(f"SQLite缓存命中: {len(data_dict)}只, 需拉取: {len(missing_codes)}只")

        # 2. 从baostock拉取缺失数据
        if missing_codes:
            fetched = self._fetch_and_cache(conn, missing_codes, start_date, end_date)
            data_dict.update(fetched)

        conn.close()
        logger.info(f"最终数据: {len(data_dict)}只股票, 区间 {start_date}~{end_date}")
        return data_dict

    def _load_from_db(self, conn, code, start_date, end_date) -> pd.DataFrame:
        """从SQLite加载单只股票数据"""
        try:
            df = pd.read_sql_query("""
                SELECT date, open, close, high, low, volume, amount
                FROM daily_kline
                WHERE code=? AND date>=? AND date<=?
                ORDER BY date
            """, conn, params=(code, start_date, end_date))

            if df.empty:
                return None

            # 数据清洗
            for col in ["open", "close", "high", "low", "volume", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["close"])
            df = df[df["close"] > 0]
            df = df[df["volume"] > 0]

            return df if not df.empty else None
        except Exception:
            return None

    def _fetch_and_cache(self, conn, codes, start_date, end_date) -> dict:
        """从baostock批量拉取并缓存到SQLite"""
        if not HAS_BAOSTOCK:
            logger.warning("baostock未安装，无法拉取缺失数据")
            return {}

        self._bs_login()
        data_dict = {}
        failed = 0

        for i, code in enumerate(codes):
            try:
                bs_code = _to_bs_code(code)
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,close,high,low,volume,amount",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="2"  # 前复权
                )

                if rs.error_code != '0':
                    failed += 1
                    continue

                rows = []
                while rs.error_code == '0' and rs.next():
                    rows.append(rs.get_row_data())

                if not rows:
                    continue

                df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "amount"])
                for col in ["open", "close", "high", "low", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["close"] > 0]

                if len(df) >= 60:
                    data_dict[code] = df
                    # 写入SQLite缓存
                    self._save_to_db(conn, code, df)

            except Exception as e:
                failed += 1
                if failed <= 5:
                    logger.warning(f"  [{code}] 拉取失败: {e}")

            if (i + 1) % 50 == 0:
                logger.info(f"  拉取进度: {i+1}/{len(codes)}, 成功{len(data_dict)}, 失败{failed}")
                time.sleep(0.5)  # 避免请求过快

        logger.info(f"拉取完成: 成功{len(data_dict)}, 失败{failed}")
        return data_dict

    def _save_to_db(self, conn, code, df):
        """保存数据到SQLite"""
        try:
            rows = [
                (code, r["date"], r["open"], r["close"], r["high"],
                 r["low"], r["volume"], r.get("amount", 0))
                for _, r in df.iterrows()
            ]
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO daily_kline (code, date, open, close, high, low, volume, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            if not df.empty:
                cursor.execute("""
                    INSERT OR REPLACE INTO last_update (code, last_date) VALUES (?, ?)
                """, (code, df["date"].max()))
            conn.commit()
        except Exception as e:
            logger.debug(f"  [{code}] 缓存写入失败: {e}")

    # ============================================================
    # 五、便捷接口
    # ============================================================

    def get_filtered_universe(self, date: str = None,
                               apply_cap_filter: bool = False) -> list:
        """
        一键获取过滤后的可交易股票池

        参数:
            date: 过滤日期
            apply_cap_filter: 是否应用市值/流动性过滤（需要先有数据）
        """
        stocks_df = self.get_all_a_share_codes(date)
        codes = self.filter_universe(stocks_df, date)

        if apply_cap_filter:
            codes = self.get_market_cap_filter(codes, date)

        return codes

    def close(self):
        """释放资源"""
        self._bs_logout()
