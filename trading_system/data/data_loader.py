"""
行情数据获取与增量更新模块
===========================
- 主数据源：baostock（证券宝，免费稳定）
- 备用数据源：akshare（东方财富）
- 支持每日增量更新，保存到本地 SQLite 数据库
- 失败自动重试3次，每次间隔2秒
- 股票池从 config.py 读取
"""

import sqlite3
import time
import datetime
import logging
import pandas as pd

logger = logging.getLogger(__name__)

# 导入 baostock（主数据源）
try:
    import baostock as bs
    HAS_BAOSTOCK = True
except ImportError:
    HAS_BAOSTOCK = False
    logger.warning("baostock 未安装，请运行: pip install baostock")

# 导入 akshare（备用数据源）
try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


# ============================================================
# baostock 代码转换
# ============================================================

def _to_baostock_code(code: str) -> str:
    """
    将纯数字股票代码转换为 baostock 格式
    002371 -> sz.002371
    600584 -> sh.600584
    000300 -> sh.000300 (沪深300指数)
    """
    if code.startswith("sh.") or code.startswith("sz."):
        return code
    if code.startswith("6") or code.startswith("9") or code == "000300":
        return f"sh.{code}"
    else:
        return f"sz.{code}"


# ============================================================
# 数据库操作
# ============================================================

def init_db(db_path: str = None) -> sqlite3.Connection:
    """初始化SQLite数据库，创建必要的表"""
    if db_path is None:
        db_path = config.DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_kline (
            code       TEXT    NOT NULL,
            date       TEXT    NOT NULL,
            open       REAL,
            close      REAL,
            high       REAL,
            low        REAL,
            volume     REAL,
            amount     REAL,
            PRIMARY KEY (code, date)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS last_update (
            code       TEXT PRIMARY KEY,
            last_date  TEXT
        )
    """)
    conn.commit()
    return conn


def get_last_update_date(conn: sqlite3.Connection, code: str) -> str:
    """获取某只股票最后更新日期"""
    cursor = conn.cursor()
    cursor.execute("SELECT last_date FROM last_update WHERE code=?", (code,))
    row = cursor.fetchone()
    return row[0] if row else None


# ============================================================
# baostock 数据获取（主数据源）
# ============================================================

_bs_session = {"logged_in": False}

def _bs_login():
    """确保 baostock 已登录"""
    if not _bs_session["logged_in"]:
        lg = bs.login()
        if lg.error_code != '0':
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        _bs_session["logged_in"] = True


def _bs_logout():
    """baostock 登出"""
    if _bs_session["logged_in"]:
        bs.logout()
        _bs_session["logged_in"] = False


def fetch_stock_daily_baostock(code: str, start_date: str = None,
                                end_date: str = None) -> pd.DataFrame:
    """
    通过 baostock 获取日线数据
    返回 DataFrame: date, open, close, high, low, volume
    """
    _bs_login()

    bs_code = _to_baostock_code(code)
    if start_date is None:
        start_date = "2020-01-01"
    if end_date is None:
        end_date = datetime.date.today().strftime("%Y-%m-%d")

    # baostock 日期格式 YYYY-MM-DD
    if "-" not in start_date:
        start_date = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    if "-" not in end_date:
        end_date = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,close,high,low,volume,amount",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="2"  # 前复权
    )

    if rs.error_code != '0':
        raise RuntimeError(f"baostock 查询失败 [{code}]: {rs.error_msg}")

    data = []
    while rs.error_code == '0' and rs.next():
        data.append(rs.get_row_data())

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=["date", "open", "close", "high", "low", "volume", "amount"])
    # 转换为数值类型
    for col in ["open", "close", "high", "low", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=["close"])
    return df


# ============================================================
# akshare 数据获取（备用数据源）
# ============================================================

def fetch_stock_daily_akshare(code: str, start_date: str = None,
                               end_date: str = None) -> pd.DataFrame:
    """通过 akshare 获取日线数据（备用）"""
    if not HAS_AKSHARE:
        raise RuntimeError("akshare 未安装")

    if start_date is None:
        start_date = "20200101"
    if end_date is None:
        end_date = datetime.date.today().strftime("%Y%m%d")
    # baostock 格式转 akshare 格式
    if "-" in start_date:
        start_date = start_date.replace("-", "")
    if "-" in end_date:
        end_date = end_date.replace("-", "")

    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq"
    )
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.rename(columns={
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
    })
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    cols = ["date", "open", "close", "high", "low", "volume"]
    if "amount" in df.columns:
        cols.append("amount")
    return df[cols].dropna()


# ============================================================
# 统一获取接口（自动切换数据源）
# ============================================================

def fetch_stock_daily(code: str, start_date: str = None, end_date: str = None,
                      retry_times: int = None, retry_interval: int = None) -> pd.DataFrame:
    """
    获取单只股票日线数据（带重试 + 自动切换数据源）
    优先使用 baostock，失败后尝试 akshare
    """
    if retry_times is None:
        retry_times = config.DATA_RETRY_TIMES
    if retry_interval is None:
        retry_interval = config.DATA_RETRY_INTERVAL

    last_error = None

    for attempt in range(1, retry_times + 1):
        # 先试 baostock
        if HAS_BAOSTOCK:
            try:
                df = fetch_stock_daily_baostock(code, start_date, end_date)
                if not df.empty:
                    return df
            except Exception as e:
                last_error = e
                logger.warning(f"[{code}] baostock 第{attempt}次失败: {e}")

        # 再试 akshare
        if HAS_AKSHARE:
            try:
                df = fetch_stock_daily_akshare(code, start_date, end_date)
                if not df.empty:
                    logger.info(f"[{code}] baostock失败，已切换到akshare")
                    return df
            except Exception as e:
                last_error = e
                logger.warning(f"[{code}] akshare 第{attempt}次失败: {e}")

        if attempt < retry_times:
            time.sleep(retry_interval)

    raise RuntimeError(f"[{code}] 所有数据源均失败: {last_error}")


# ============================================================
# 增量更新到数据库
# ============================================================

def update_stock_to_db(conn: sqlite3.Connection, code: str) -> int:
    """
    增量更新单只股票到数据库
    返回新增记录数
    """
    last_date = get_last_update_date(conn, code)
    start_date = None
    if last_date:
        next_day = datetime.datetime.strptime(last_date, "%Y-%m-%d") + datetime.timedelta(days=1)
        start_date = next_day.strftime("%Y-%m-%d")

    end_date = datetime.date.today().strftime("%Y-%m-%d")

    if start_date and start_date > end_date:
        return 0

    df = fetch_stock_daily(code, start_date=start_date, end_date=end_date)
    if df.empty:
        return 0

    cursor = conn.cursor()
    count = 0
    for _, row in df.iterrows():
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO daily_kline (code, date, open, close, high, low, volume, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, row["date"], row["open"], row["close"], row["high"],
                  row["low"], row["volume"], row.get("amount", 0)))
            count += 1
        except Exception as e:
            logger.error(f"[{code}] 写入失败: {e}")

    latest = df["date"].max()
    cursor.execute("""
        INSERT OR REPLACE INTO last_update (code, last_date) VALUES (?, ?)
    """, (code, latest))
    conn.commit()
    return count


def batch_update_all(conn: sqlite3.Connection = None) -> dict:
    """
    批量更新所有股票池中的日线数据
    返回: {code: 新增记录数}
    """
    if conn is None:
        conn = init_db()

    # 确保 baostock 登录
    if HAS_BAOSTOCK:
        _bs_login()

    results = {}
    codes = list(config.STOCK_POOL.keys())
    codes.append(config.BENCHMARK_INDEX)

    for code in codes:
        try:
            logger.info(f"正在更新 {code} ...")
            count = update_stock_to_db(conn, code)
            results[code] = count
            logger.info(f"[{code}] 新增 {count} 条记录")
            time.sleep(1)  # baostock 不需要太长间隔
        except Exception as e:
            logger.error(f"[{code}] 更新失败: {e}")
            results[code] = -1

    # 登出 baostock
    if HAS_BAOSTOCK:
        _bs_logout()

    return results


def load_daily_data(code: str, conn: sqlite3.Connection = None,
                    days: int = 120) -> pd.DataFrame:
    """
    从数据库加载某只股票最近N天的日线数据
    返回按日期升序排列的DataFrame
    """
    if conn is None:
        conn = init_db()

    query = """
        SELECT date, open, close, high, low, volume
        FROM daily_kline
        WHERE code = ?
        ORDER BY date DESC
        LIMIT ?
    """
    df = pd.read_sql_query(query, conn, params=(code, days))
    if df.empty:
        return df
    df = df.sort_values("date").reset_index(drop=True)
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("=" * 50)
    print("  行情数据增量更新 (baostock)")
    print("=" * 50)
    conn = init_db()
    results = batch_update_all(conn)
    for code, count in results.items():
        status = f"新增{count}条" if count >= 0 else "失败"
        print(f"  {code}: {status}")
    conn.close()
    print("\n[OK] 数据更新完成")
