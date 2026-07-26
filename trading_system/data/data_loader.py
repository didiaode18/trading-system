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
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    588000 -> sh.588000 (科创50ETF)
    159205 -> sz.159205 (创业东财ETF)
    """
    if code.startswith("sh.") or code.startswith("sz."):
        return code
    if code.startswith("6") or code.startswith("9") or code.startswith("5") or code == "000300":
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

def _validate_data_quality(conn: sqlite3.Connection, code: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    数据质量验证：过滤明显异常的数据行
    
    检测规则:
    1. 成交量异常低（< 历史均量的2%）且价格大幅变动（>5%）→ 疑似数据源错误
    2. 单日跌幅超过11%（A股主板涨跌停±10%，留1%容差）→ 疑似前复权计算错误
    3. 价格为0或负数 → 无效数据
    
    返回: 过滤后的DataFrame
    """
    if df.empty or len(df) == 0:
        return df
    
    # 获取历史平均成交量（最近20天）
    cursor = conn.cursor()
    cursor.execute("""
        SELECT volume FROM daily_kline 
        WHERE code=? AND volume > 0 
        ORDER BY date DESC LIMIT 20
    """, (code,))
    hist_volumes = [row[0] for row in cursor.fetchall()]
    avg_volume = sum(hist_volumes) / len(hist_volumes) if hist_volumes else 0
    
    valid_rows = []
    removed = []
    
    for idx, row in df.iterrows():
        vol = row.get("volume", 0) or 0
        close = row.get("close", 0) or 0
        open_price = row.get("open", 0) or 0
        
        # 规则0: 价格无效
        if close <= 0 or open_price <= 0:
            removed.append(f"{row['date']}: 价格无效(close={close})")
            continue
        
        # 规则1: 成交量异常低 + 价格大幅变动
        if avg_volume > 0 and vol > 0:
            vol_ratio = vol / avg_volume
            if vol_ratio < 0.02:  # 成交量不足均量2%
                # 检查价格变动（与前一天收盘对比）
                if valid_rows:
                    prev_close = valid_rows[-1]["close"]
                else:
                    # 从数据库取前一天收盘价
                    cursor.execute("""
                        SELECT close FROM daily_kline 
                        WHERE code=? ORDER BY date DESC LIMIT 1
                    """, (code,))
                    prev_row = cursor.fetchone()
                    prev_close = prev_row[0] if prev_row else close
                
                if prev_close > 0:
                    price_change = abs(close - prev_close) / prev_close
                    if price_change > 0.05:  # 价格变动>5%
                        removed.append(
                            f"{row['date']}: 量价异常(vol={vol:.0f}, "
                            f"均量比={vol_ratio:.3f}, 跌幅={price_change:.1%})")
                        continue
        
        # 规则2: 单日跌幅超11%（非ST/非新股）
        if valid_rows:
            prev_close = valid_rows[-1]["close"]
            if prev_close > 0:
                daily_change = (close - prev_close) / prev_close
                if daily_change < -0.11:
                    removed.append(
                        f"{row['date']}: 单日跌幅{daily_change:.1%}超限(可能前复权错误)")
                    continue
        
        valid_rows.append(row)
    
    if removed:
        logger.warning(f"[{code}] 数据质量验证: 过滤{len(removed)}条异常数据")
        for r in removed:
            logger.warning(f"  {r}")
    
    if not valid_rows:
        return pd.DataFrame()
    return pd.DataFrame(valid_rows)


def update_stock_to_db(conn: sqlite3.Connection, code: str) -> int:
    """
    增量更新单只股票到数据库（含数据质量验证 + 批量写入）
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

    # 数据质量验证：过滤异常数据
    df = _validate_data_quality(conn, code, df)
    if df.empty:
        logger.warning(f"[{code}] 所有新数据未通过质量验证，跳过更新")
        return 0

    # 批量写入（executemany替代逐行INSERT，性能提升10x+）
    cursor = conn.cursor()
    rows = [
        (code, row["date"], row["open"], row["close"], row["high"],
         row["low"], row["volume"], row.get("amount", 0))
        for _, row in df.iterrows()
    ]
    try:
        cursor.executemany("""
            INSERT OR REPLACE INTO daily_kline (code, date, open, close, high, low, volume, amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        count = len(rows)
    except Exception as e:
        logger.error(f"[{code}] 批量写入失败: {e}")
        return -1

    latest = df["date"].max()
    cursor.execute("""
        INSERT OR REPLACE INTO last_update (code, last_date) VALUES (?, ?)
    """, (code, latest))
    conn.commit()
    return count


def repair_stock_data(code: str, days_back: int = 5) -> int:
    """
    修复单只股票的近期异常数据
    
    操作:
    1. 删除最近N天的数据
    2. 重新从数据源拉取（含质量验证）
    
    参数:
        code: 股票代码
        days_back: 回退天数（默认5天）
    
    返回: 重新写入的记录数
    """
    conn = init_db()
    cursor = conn.cursor()
    
    # 计算回退日期
    cutoff = (datetime.date.today() - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    # 删除最近N天数据
    cursor.execute("DELETE FROM daily_kline WHERE code=? AND date>?", (code, cutoff))
    deleted = cursor.rowcount
    
    # 更新last_update为cutoff之前
    cursor.execute("""
        SELECT MAX(date) FROM daily_kline WHERE code=? AND date<=?
    """, (code, cutoff))
    row = cursor.fetchone()
    new_last = row[0] if row and row[0] else None
    if new_last:
        cursor.execute("INSERT OR REPLACE INTO last_update (code, last_date) VALUES (?, ?)",
                      (code, new_last))
    else:
        cursor.execute("DELETE FROM last_update WHERE code=?", (code,))
    conn.commit()
    
    logger.info(f"[{code}] 修复: 删除{deleted}条近期数据, 回退到{new_last}")
    
    # 重新拉取
    count = update_stock_to_db(conn, code)
    logger.info(f"[{code}] 修复完成: 重新写入{count}条记录")
    conn.close()
    return count


# ============================================================
# 盘后多源校验（确保收盘价准确）
# ============================================================

def validate_close_prices(codes: list = None, tolerance: float = 0.02) -> dict:
    """
    盘后收盘价交叉校验：对比DB中最新收盘价与实时行情API
    
    原理:
      盘后运行，用腾讯行情API获取当日收盘价，与DB中最新数据对比
      偏差超过tolerance(2%)的标记为可疑，尝试用akshare数据修复
    
    参数:
        codes: 要校验的股票代码列表（默认用STOCK_POOL）
        tolerance: 允许偏差比例（默认2%）
    
    返回: {code: {"status": "ok"/"mismatch"/"fixed", "db_price": x, "real_price": y}}
    """
    if codes is None:
        codes = list(config.STOCK_POOL.keys())
    
    # 获取实时行情（盘后即为收盘价）
    try:
        from data.realtime import fetch_realtime_batch
        realtime = fetch_realtime_batch(codes)
    except Exception as e:
        logger.warning(f"多源校验: 实时行情获取失败: {e}")
        return {}
    
    if not realtime:
        return {}
    
    conn = init_db()
    cursor = conn.cursor()
    results = {}
    
    for code in codes:
        if code not in realtime:
            continue
        
        real_price = realtime[code].get("price", 0)
        if real_price <= 0:
            continue
        
        # 查询DB中最新收盘价
        cursor.execute("""
            SELECT close, date FROM daily_kline 
            WHERE code=? ORDER BY date DESC LIMIT 1
        """, (code,))
        row = cursor.fetchone()
        if not row:
            continue
        
        db_price, db_date = row[0], row[1]
        deviation = abs(db_price - real_price) / real_price
        
        if deviation <= tolerance:
            results[code] = {"status": "ok", "db_price": db_price, "real_price": real_price}
        else:
            logger.warning(
                f"[{code}] 收盘价偏差{deviation:.1%}: DB={db_price:.2f}({db_date}) vs 实时={real_price:.2f}")
            
            # 尝试用akshare修复当日数据
            fixed = False
            if HAS_AKSHARE:
                try:
                    today_str = datetime.date.today().strftime("%Y-%m-%d")
                    df = fetch_stock_daily_akshare(code, start_date=today_str, end_date=today_str)
                    if not df.empty:
                        ak_close = float(df.iloc[-1]["close"])
                        ak_deviation = abs(ak_close - real_price) / real_price
                        if ak_deviation < deviation:  # akshare更接近实时价
                            cursor.execute("""
                                INSERT OR REPLACE INTO daily_kline (code, date, open, close, high, low, volume, amount)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, (code, today_str, float(df.iloc[-1]["open"]),
                                  ak_close, float(df.iloc[-1]["high"]),
                                  float(df.iloc[-1]["low"]), float(df.iloc[-1]["volume"]),
                                  float(df.iloc[-1].get("amount", 0))))
                            cursor.execute("""
                                INSERT OR REPLACE INTO last_update (code, last_date) VALUES (?, ?)
                            """, (code, today_str))
                            conn.commit()
                            fixed = True
                            logger.info(f"[{code}] 已用akshare修复: {db_price:.2f} -> {ak_close:.2f}")
                except Exception as e:
                    logger.debug(f"[{code}] akshare修复失败: {e}")
            
            results[code] = {
                "status": "fixed" if fixed else "mismatch",
                "db_price": db_price,
                "real_price": real_price,
                "deviation": f"{deviation:.1%}",
            }
    
    conn.close()
    
    # 统计
    mismatch_count = sum(1 for v in results.values() if v["status"] == "mismatch")
    fixed_count = sum(1 for v in results.values() if v["status"] == "fixed")
    if mismatch_count or fixed_count:
        logger.info(f"多源校验完成: {fixed_count}只已修复, {mismatch_count}只仍偏差")
    
    return results


def get_all_candidate_codes() -> list:
    """
    汇总所有需要拉取数据的股票代码（去重）
    来源: STOCK_POOL + SECTOR_CANDIDATES + BENCHMARK_INDEX
    """
    codes = set(config.STOCK_POOL.keys())
    # 从 SECTOR_CANDIDATES 中提取所有候选股
    sector_candidates = getattr(config, 'SECTOR_CANDIDATES', {})
    for sector_name, sector_info in sector_candidates.items():
        stocks = sector_info.get("stocks", {})
        codes.update(stocks.keys())
    # 加入基准指数
    codes.add(config.BENCHMARK_INDEX)
    # 过滤创业板(300)和科创板(688)，用户无交易权限
    codes = {c for c in codes if not c.startswith("300") and not c.startswith("688")}
    return sorted(codes)


def _update_single_akshare(code: str) -> int:
    """
    单只股票akshare更新（线程安全，用于并行拉取）
    每个线程独立连接数据库，避免SQLite并发问题
    返回: 新增记录数，失败返回-1
    """
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        last_date = get_last_update_date(conn, code)
        start_date = None
        if last_date:
            next_day = datetime.datetime.strptime(last_date, "%Y-%m-%d") + datetime.timedelta(days=1)
            start_date = next_day.strftime("%Y-%m-%d")

        end_date = datetime.date.today().strftime("%Y-%m-%d")
        if start_date and start_date > end_date:
            conn.close()
            return 0

        # 用akshare拉取
        df = fetch_stock_daily_akshare(code, start_date=start_date, end_date=end_date)
        if df.empty:
            conn.close()
            return 0

        # 数据质量验证
        df = _validate_data_quality(conn, code, df)
        if df.empty:
            conn.close()
            return 0

        # 批量写入
        cursor = conn.cursor()
        rows = [
            (code, row["date"], row["open"], row["close"], row["high"],
             row["low"], row["volume"], row.get("amount", 0))
            for _, row in df.iterrows()
        ]
        cursor.executemany("""
            INSERT OR REPLACE INTO daily_kline (code, date, open, close, high, low, volume, amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        latest = df["date"].max()
        cursor.execute("INSERT OR REPLACE INTO last_update (code, last_date) VALUES (?, ?)",
                       (code, latest))
        conn.commit()
        conn.close()
        return len(rows)
    except Exception as e:
        logger.debug(f"[{code}] akshare并行拉取失败: {e}")
        return -1


def batch_update_all(conn: sqlite3.Connection = None, full_pool: bool = True,
                     max_workers: int = 5) -> dict:
    """
    批量更新所有股票池中的日线数据（并行优化版）
    
    策略:
      Phase 1: akshare并行拉取（max_workers个线程）
      Phase 2: 失败的用baostock顺序补拉
    
    参数:
        conn: 数据库连接（仅用于baostock补拉阶段）
        full_pool: True=更新全部候选股, False=仅更新STOCK_POOL
        max_workers: 并行线程数（默认5）
    返回: {code: 新增记录数}
    """
    if conn is None:
        conn = init_db()

    if full_pool:
        codes = get_all_candidate_codes()
    else:
        codes = list(config.STOCK_POOL.keys())
        if config.BENCHMARK_INDEX not in codes:
            codes.append(config.BENCHMARK_INDEX)

    logger.info(f"数据更新范围: {len(codes)}只股票 (并行度={max_workers})")
    results = {}

    # Phase 1: akshare并行拉取
    if HAS_AKSHARE:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_update_single_akshare, code): code
                       for code in codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    results[code] = future.result()
                except Exception:
                    results[code] = -1

        success_count = sum(1 for v in results.values() if v >= 0)
        logger.info(f"  akshare并行完成: {success_count}/{len(codes)}只成功")
    else:
        # 无akshare，全部标记为失败，由baostock补拉
        for code in codes:
            results[code] = -1

    # Phase 2: baostock补拉失败的
    failed = [c for c, v in results.items() if v < 0]
    if failed and HAS_BAOSTOCK:
        logger.info(f"  baostock补拉: {len(failed)}只...")
        _bs_login()
        for code in failed:
            try:
                count = update_stock_to_db(conn, code)
                results[code] = count
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"  [{code}] baostock补拉失败: {e}")
                results[code] = -1
        _bs_logout()

    # 统计
    success = sum(1 for v in results.values() if v > 0)
    no_change = sum(1 for v in results.values() if v == 0)
    fail = sum(1 for v in results.values() if v < 0)
    logger.info(f"  更新结果: {success}只新增, {no_change}只无变化, {fail}只失败")

    return results


def load_daily_data(code: str, conn: sqlite3.Connection = None,
                    days: int = 120) -> pd.DataFrame:
    """
    从数据库加载某只股票最近N天的日线数据
    返回按日期升序排列的DataFrame
    """
    # FIX: 修复自建SQLite连接未关闭导致连接泄漏
    own_conn = False
    if conn is None:
        conn = init_db()
        own_conn = True

    try:
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
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception as e:
                logger.debug(f"关闭自建SQLite连接失败: {e}")


# ============================================================
# 资金流向数据（通过akshare获取）
# ============================================================

def init_capital_flow_table(conn: sqlite3.Connection):
    """初始化资金流向表"""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS capital_flow (
            code       TEXT    NOT NULL,
            date       TEXT    NOT NULL,
            main_net   REAL,           -- 主力净流入（万元）
            super_net  REAL,           -- 超大单净流入（万元）
            big_net    REAL,           -- 大单净流入（万元）
            mid_net    REAL,           -- 中单净流入（万元）
            small_net  REAL,           -- 小单净流入（万元）
            PRIMARY KEY (code, date)
        )
    """)
    conn.commit()


def fetch_capital_flow_akshare(code: str, days: int = 30) -> pd.DataFrame:
    """
    通过akshare获取个股资金流向数据
    
    参数:
        code: 股票代码（纯数字）
        days: 获取天数
    
    返回:
        DataFrame: date, main_net, super_net, big_net, mid_net, small_net
    """
    if not HAS_AKSHARE:
        logger.warning("akshare未安装，无法获取资金流向")
        return pd.DataFrame()

    try:
        end_date = datetime.date.today().strftime("%Y%m%d")
        start_date = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y%m%d")

        df = ak.stock_individual_fund_flow(
            stock=code,
            market="sh" if code.startswith("6") else "sz"
        )

        if df is None or df.empty:
            return pd.DataFrame()

        # 列名映射
        col_map = {
            "日期": "date",
            "主力净流入-净额": "main_net",
            "超大单净流入-净额": "super_net",
            "大单净流入-净额": "big_net",
            "中单净流入-净额": "mid_net",
            "小单净流入-净额": "small_net"
        }
        df = df.rename(columns=col_map)

        # 只保留需要的列
        needed = ["date", "main_net", "super_net", "big_net", "mid_net", "small_net"]
        available = [c for c in needed if c in df.columns]
        df = df[available].tail(days)

        # 转换数值
        for col in available:
            if col != "date":
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    except Exception as e:
        logger.warning(f"[{code}] 资金流向获取失败: {e}")
        return pd.DataFrame()


def update_capital_flow(conn: sqlite3.Connection, code: str) -> int:
    """
    更新单只股票的资金流向数据到数据库
    返回新增记录数
    """
    init_capital_flow_table(conn)

    df = fetch_capital_flow_akshare(code)
    if df.empty:
        return 0

    cursor = conn.cursor()
    count = 0
    for _, row in df.iterrows():
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO capital_flow
                (code, date, main_net, super_net, big_net, mid_net, small_net)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (code, row["date"],
                  row.get("main_net", 0), row.get("super_net", 0),
                  row.get("big_net", 0), row.get("mid_net", 0),
                  row.get("small_net", 0)))
            count += 1
        except Exception as e:
            logger.error(f"[{code}] 资金流向写入失败: {e}")

    conn.commit()
    return count


def load_capital_flow(code: str, conn: sqlite3.Connection = None,
                      days: int = 10) -> pd.DataFrame:
    """
    从数据库加载资金流向数据
    
    返回:
        DataFrame: date, main_net, super_net, big_net, mid_net, small_net
    """
    # FIX: 修复自建SQLite连接未关闭导致连接泄漏
    own_conn = False
    if conn is None:
        conn = init_db()
        own_conn = True

    try:
        init_capital_flow_table(conn)

        query = """
            SELECT date, main_net, super_net, big_net, mid_net, small_net
            FROM capital_flow
            WHERE code = ?
            ORDER BY date DESC
            LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(code, days))
        if df.empty:
            return df
        return df.sort_values("date").reset_index(drop=True)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception as e:
                logger.debug(f"关闭自建SQLite连接失败: {e}")


def get_capital_flow_signal(code: str, conn: sqlite3.Connection = None) -> dict:
    """
    获取资金流向信号
    
    返回:
        {
            "signal": str,         # "positive"/"negative"/"neutral"
            "main_net_avg": float, # 近5日主力净流入均值（万元）
            "trend": str,          # "increasing"/"decreasing"
            "reason": str
        }
    """
    result = {
        "signal": "neutral",
        "main_net_avg": 0,
        "trend": "neutral",
        "reason": "无资金流向数据"
    }

    df = load_capital_flow(code, conn, days=10)
    if df.empty:
        return result

    # 近5日主力净流入均值
    recent = df.tail(5)
    avg_main = recent["main_net"].mean()
    result["main_net_avg"] = round(avg_main, 2)

    # 信号判定
    if avg_main > 500:  # 主力净流入超500万
        result["signal"] = "positive"
        result["reason"] = f"近5日主力净流入均值{avg_main:.0f}万元，资金积极流入"
    elif avg_main < -500:  # 主力净流出超500万
        result["signal"] = "negative"
        result["reason"] = f"近5日主力净流入均值{avg_main:.0f}万元，资金持续流出"
    else:
        result["reason"] = f"近5日主力净流入均值{avg_main:.0f}万元，资金流向中性"

    # 趋势判定
    if len(df) >= 5:
        first_half = df["main_net"].iloc[:5].mean()
        second_half = df["main_net"].iloc[-5:].mean()
        if second_half > first_half:
            result["trend"] = "increasing"
        elif second_half < first_half:
            result["trend"] = "decreasing"

    return result


# ============================================================
# 数据源分层架构（盘前/盘中/盘后自动切换）
# ============================================================

import threading

# 实时行情本地缓存（避免频繁请求被限流）
_realtime_cache = {}       # {code: {"data": {...}, "ts": timestamp}}
_cache_lock = threading.Lock()
_CACHE_TTL = 30            # 缓存有效期30秒


def _get_market_phase() -> str:
    """
    判断当前市场阶段
    返回: "pre_market" / "intraday" / "post_market" / "closed"
    """
    now = datetime.datetime.now()
    weekday = now.weekday()
    if weekday >= 5:  # 周六日
        return "closed"
    t = now.hour * 100 + now.minute
    if t < 830:
        return "pre_market"
    elif t < 930:
        return "pre_market"  # 集合竞价阶段仍用昨收
    elif t <= 1500:
        return "intraday"
    else:
        return "post_market"


def validate_price(code: str, price: float, prev_close: float = 0) -> dict:
    """
    价格校验机制：检测异常数据
    
    校验规则:
      1. 价格为0或负数 → 异常
      2. 涨跌幅 > 11%（非ST/非新股）→ 异常
      3. 与昨收偏差 > 20% → 异常
    
    返回: {"valid": bool, "reason": str, "price": float}
    """
    if price <= 0:
        logger.warning(f"[价格校验] {code} 价格异常: price={price} ≤ 0 ✗")
        return {"valid": False, "reason": f"价格无效({price})", "price": 0}
    
    if prev_close > 0:
        change_pct = abs(price - prev_close) / prev_close * 100
        if change_pct > 20:
            logger.warning(f"[价格校验] {code} 价格异常: 与昨收偏差{change_pct:.1f}% > 20% ✗")
            return {"valid": False, "reason": f"与昨收偏差{change_pct:.1f}%>20%", "price": price}
        if change_pct > 11:
            logger.warning(f"[价格校验] {code} 数据可疑: 涨跌幅{change_pct:.1f}% > 11%（可能前复权错误）")
            return {"valid": True, "reason": f"涨跌幅{change_pct:.1f}%偏大", "price": price, "suspicious": True}
    
    return {"valid": True, "reason": "正常", "price": price}


def get_smart_price(code: str, conn: sqlite3.Connection = None) -> dict:
    """
    智能价格获取（数据源分层架构）
    
    策略:
      - 盘前（08:30前）：使用 baostock/DB 最新收盘价作为基准
      - 盘中（09:30-15:00）：接入实时行情源，每30秒刷新（带缓存）
      - 盘后（15:00后）：以当日收盘价为准确基准
    
    容错:
      - 单只股票获取失败不影响其他标的
      - 降级使用最近有效数据并标注"⚠️数据延迟"
    
    返回: {
        "price": float,        # 当前有效价格
        "prev_close": float,   # 昨收
        "source": str,         # 数据来源
        "phase": str,          # 市场阶段
        "stale": bool,         # 是否为降级数据
        "valid": bool,         # 价格是否通过校验
        "reason": str,         # 校验说明
    }
    """
    global _realtime_cache
    phase = _get_market_phase()
    result = {
        "price": 0, "prev_close": 0, "source": "none",
        "phase": phase, "stale": False, "valid": False, "reason": ""
    }
    
    # ---- 盘中: 优先使用实时行情（带30秒缓存）----
    if phase == "intraday":
        try:
            from data.realtime import fetch_realtime_batch
            
            # 检查缓存
            # FIX: 修复缓存写入在锁外执行导致多线程竞态
            import time as _time
            with _cache_lock:
                cached = _realtime_cache.get(code)
                if cached and (_time.time() - cached["ts"]) < _CACHE_TTL:
                    rt = cached["data"]
                else:
                    # 缓存过期，重新获取
                    rt_map = fetch_realtime_batch([code])
                    rt = rt_map.get(code, {})
                    if rt:
                        _realtime_cache[code] = {"data": rt, "ts": _time.time()}
            
            if rt and rt.get("price", 0) > 0:
                price = rt["price"]
                prev_close = rt.get("prev_close", 0)
                # 价格校验
                v = validate_price(code, price, prev_close)
                result.update({
                    "price": price, "prev_close": prev_close,
                    "source": f"realtime({rt.get('source', 'tencent')})",
                    "valid": v["valid"], "reason": v["reason"],
                })
                logger.info(f"[价格校验] {code} 实时价{price:.3f} 来源:{result['source']} ✓")
                return result
        except Exception as e:
            logger.warning(f"[{code}] 盘中实时行情获取失败: {e}，降级使用DB数据")
    
    # ---- 盘前/盘后/实时失败: 使用DB最新收盘价 ----
    own_conn = False
    try:
        # FIX: 修复自建SQLite连接未关闭导致连接泄漏
        if conn is None:
            conn = init_db()
            own_conn = True
        cursor = conn.cursor()
        cursor.execute("""
            SELECT close, date FROM daily_kline 
            WHERE code=? ORDER BY date DESC LIMIT 2
        """, (code,))
        rows = cursor.fetchall()
        
        if rows:
            price = rows[0][0]
            prev_close = rows[1][0] if len(rows) > 1 else 0
            v = validate_price(code, price, prev_close)
            
            # 盘后尝试用实时行情覆盖（获取当日收盘价）
            if phase == "post_market":
                try:
                    from data.realtime import fetch_realtime_batch
                    rt_map = fetch_realtime_batch([code])
                    rt = rt_map.get(code, {})
                    if rt and rt.get("price", 0) > 0:
                        price = rt["price"]
                        prev_close = rt.get("prev_close", prev_close)
                        v = validate_price(code, price, prev_close)
                        result["source"] = f"post_close({rt.get('source', 'tencent')})"
                except Exception:
                    pass
            
            if result["source"] == "none":
                result["source"] = "db_close"
                # 如果DB数据不是今天的，标记为延迟
                db_date = rows[0][1]
                today_str = datetime.date.today().strftime("%Y-%m-%d")
                if db_date < today_str and phase != "pre_market":
                    result["stale"] = True
                    result["reason"] = f"⚠️数据延迟(DB日期{db_date})"
                    logger.warning(f"[价格校验] {code} ⚠️数据延迟: DB最新={db_date}")
            
            result.update({
                "price": price, "prev_close": prev_close,
                "valid": v["valid"],
                "reason": result.get("reason") or v["reason"],
            })
            if result["valid"]:
                logger.info(f"[价格校验] {code} 价格{price:.3f} 来源:{result['source']} ✓")
            return result
    except Exception as e:
        logger.error(f"[{code}] DB数据获取失败: {e}")
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception as e:
                logger.debug(f"关闭自建SQLite连接失败: {e}")
    
    # ---- 最终降级: 尝试baostock直接拉取 ----
    try:
        df = fetch_stock_daily_baostock(code, 
              start_date=(datetime.date.today() - datetime.timedelta(days=10)).strftime("%Y-%m-%d"))
        if not df.empty:
            price = float(df.iloc[-1]["close"])
            prev_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else 0
            v = validate_price(code, price, prev_close)
            result.update({
                "price": price, "prev_close": prev_close,
                "source": "baostock_fallback", "stale": True,
                "valid": v["valid"], "reason": f"⚠️降级数据(baostock)",
            })
            logger.warning(f"[价格校验] {code} ⚠️降级使用baostock: {price:.3f}")
    except Exception as e:
        logger.error(f"[{code}] 所有数据源均失败: {e}")
    
    return result


def get_smart_price_batch(codes: list, conn: sqlite3.Connection = None) -> dict:
    """
    批量智能价格获取（带容错，单只失败不影响其他）
    
    返回: {code: {price, prev_close, source, phase, stale, valid, reason}}
    """
    if conn is None:
        conn = init_db()
    
    phase = _get_market_phase()
    results = {}
    
    # 盘中批量获取实时行情（一次网络请求）
    if phase == "intraday":
        try:
            from data.realtime import fetch_realtime_batch
            import time as _time
            
            # 检查哪些需要刷新
            need_fetch = []
            with _cache_lock:
                for code in codes:
                    cached = _realtime_cache.get(code)
                    if not cached or (_time.time() - cached["ts"]) >= _CACHE_TTL:
                        need_fetch.append(code)
                    else:
                        rt = cached["data"]
                        if rt.get("price", 0) > 0:
                            v = validate_price(code, rt["price"], rt.get("prev_close", 0))
                            results[code] = {
                                "price": rt["price"], "prev_close": rt.get("prev_close", 0),
                                "source": f"realtime_cache({rt.get('source', '')})",
                                "phase": phase, "stale": False,
                                "valid": v["valid"], "reason": v["reason"],
                            }
            
            if need_fetch:
                rt_map = fetch_realtime_batch(need_fetch)
                with _cache_lock:
                    for code in need_fetch:
                        rt = rt_map.get(code, {})
                        if rt and rt.get("price", 0) > 0:
                            _realtime_cache[code] = {"data": rt, "ts": _time.time()}
                            v = validate_price(code, rt["price"], rt.get("prev_close", 0))
                            results[code] = {
                                "price": rt["price"], "prev_close": rt.get("prev_close", 0),
                                "source": f"realtime({rt.get('source', 'tencent')})",
                                "phase": phase, "stale": False,
                                "valid": v["valid"], "reason": v["reason"],
                            }
                        else:
                            logger.warning(f"[{code}] 实时行情缺失，降级使用DB")
        except Exception as e:
            logger.warning(f"批量实时行情获取失败: {e}")
    
    # 未获取到的用DB补全
    missing = [c for c in codes if c not in results]
    if missing:
        for code in missing:
            results[code] = get_smart_price(code, conn)
    
    # 统计
    valid_count = sum(1 for v in results.values() if v.get("valid"))
    stale_count = sum(1 for v in results.values() if v.get("stale"))
    logger.info(f"[智能价格] {len(codes)}只: 有效{valid_count} 延迟{stale_count} 阶段:{phase}")
    
    return results


def validate_order_price(code: str, name: str, order_type: str,
                         trigger_price: float, current_price: float) -> dict:
    """
    条件单价格合理性校验
    
    规则:
      - 止损/清仓单: 触发价不得 >= 现价（否则开盘即触发）
      - 止盈单: 触发价不得 <= 现价（否则立即触发）
      - 建仓单(买入): 触发价不得 >= 现价×1.05（追高保护）
    
    返回: {"valid": bool, "adjusted_price": float, "note": str}
    """
    if current_price <= 0 or trigger_price <= 0:
        return {"valid": False, "adjusted_price": trigger_price, "note": "价格无效"}
    
    adjusted = trigger_price
    note = ""
    
    if order_type in ("止损", "清仓", "减仓", "强制减仓"):
        if trigger_price >= current_price:
            adjusted = round(current_price * 0.92, 3)
            note = f"止损价{trigger_price:.3f}≥现价{current_price:.3f}，调整为现价×92%={adjusted:.3f}"
            logger.warning(f"[价格校验] {code} {name} {note}")
        else:
            logger.info(f"[价格校验] {code} {name} 止损{trigger_price:.3f} < 现价{current_price:.3f} ✓")
    
    elif order_type in ("止盈", "移动止盈", "回落止盈"):
        if trigger_price <= current_price:
            adjusted = round(current_price * 1.05, 3)
            note = f"止盈价{trigger_price:.3f}≤现价{current_price:.3f}，调整为现价×105%={adjusted:.3f}"
            logger.warning(f"[价格校验] {code} {name} {note}")
        else:
            logger.info(f"[价格校验] {code} {name} 止盈{trigger_price:.3f} > 现价{current_price:.3f} ✓")
    
    elif order_type in ("建仓", "买入"):
        if trigger_price >= current_price * 1.05:
            adjusted = round(current_price * 1.01, 3)
            note = f"建仓价{trigger_price:.3f}追高(>现价×105%)，调整为现价×101%={adjusted:.3f}"
            logger.warning(f"[价格校验] {code} {name} {note}")
        else:
            logger.info(f"[价格校验] {code} {name} 建仓{trigger_price:.3f} 现价{current_price:.3f} ✓")
    
    return {
        "valid": note == "",
        "adjusted_price": adjusted,
        "note": note if note else "校验通过",
    }


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
