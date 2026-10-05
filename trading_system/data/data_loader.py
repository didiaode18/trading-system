"""
行情数据获取与增量更新模块
===========================
- 主数据源：baostock（证券宝，免费稳定）
- 备用数据源：akshare（东方财富）
- 第三数据源：腾讯日K API（web.ifzq.gtimg.cn）
- 兜底：本地 SQLite 数据库
- 支持每日增量更新，保存到本地 SQLite 数据库
- 失败自动重试3次，每次间隔2秒
- 股票池从 config.py 读取
"""

import sqlite3
import time
import datetime
import logging
import json as _json
import urllib.request
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
    # BUG-038修复: 北交所(8xx/4xx/920xxx)不支持baostock，返回bj.前缀供调用方识别
    if code.startswith(("8", "4")) or code.startswith("920"):
        return f"bj.{code}"
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
    # V3.2: 分钟级K线表（支持5min/15min/60min）
    # V10.5: 此表已废弃 — 分钟数据统一由 intraday_feed.py 的 pkl 缓存管理，
    #        保留表定义仅为向后兼容，新代码不应再读写此表。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS minute_kline (
            code       TEXT    NOT NULL,
            datetime   TEXT    NOT NULL,
            freq       TEXT    NOT NULL DEFAULT '5',
            open       REAL,
            close      REAL,
            high       REAL,
            low        REAL,
            volume     REAL,
            amount     REAL,
            PRIMARY KEY (code, datetime, freq)
        )
    """)
    # V7.0 Phase0: 基本面数据表（因子体系建设）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fundamental (
            code              TEXT NOT NULL,
            report_date       TEXT NOT NULL,
            update_date       TEXT NOT NULL,
            pe_ttm            REAL,
            pb                REAL,
            roe               REAL,
            gross_margin      REAL,
            debt_ratio        REAL,
            revenue_growth    REAL,
            net_profit_growth REAL,
            eps_growth_q      REAL,
            eps_growth_3y     REAL,
            pe_percentile     REAL,
            dividend_yield    REAL,
            total_mcap        REAL,
            circ_mcap         REAL,
            PRIMARY KEY (code, report_date)
        )
    """)
    # V7.0 Phase0: 行业分类表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stock_industry (
            code          TEXT PRIMARY KEY,
            industry_sw   TEXT,
            industry_em   TEXT,
            update_date   TEXT
        )
    """)
    # V7.0 Phase0: daily_kline 换手率字段迁移
    try:
        cursor.execute("ALTER TABLE daily_kline ADD COLUMN turnover REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 字段已存在
    # V10.5: 添加二级索引（加速按code查询，避免全表扫描）
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily_kline(code, date DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_last_update_code ON last_update(code)")
    except sqlite3.OperationalError:
        pass  # 索引已存在
    # V10.5 改造A: PIT全市场股票基础信息表（含上市/退市日期，消除幸存者偏差）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stock_basic (
            code          TEXT PRIMARY KEY,
            name          TEXT,
            board         TEXT,
            ipo_date      TEXT,
            out_date      TEXT,
            status        TEXT,
            industry_sw   TEXT,
            industry_em   TEXT,
            update_date   TEXT
        )
    """)
    # V10.5 改造A: ST戴帽/摘帽时变状态表（PIT化）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stock_status_history (
            code        TEXT,
            date        TEXT,
            status_tag  TEXT,
            PRIMARY KEY (code, date)
        )
    """)
    # V11 回测升级: PIT数据层五张表（根治 BUG-001/011，支撑 Stage0-16）
    # 1. 不复权日线（根治 BUG-001 前复权增量假跳空）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_kline_raw (
            代码 TEXT NOT NULL,
            日期 TEXT NOT NULL,
            开盘 REAL, 最高 REAL, 最低 REAL, 收盘 REAL,
            成交量 REAL, 成交额 REAL, 换手率 REAL,
            是否停牌 INTEGER DEFAULT 0,
            涨停价 REAL, 跌停价 REAL,
            前收盘 REAL,
            数据源 TEXT,
            数据版本 TEXT,
            PRIMARY KEY (代码, 日期)
        )
    """)
    # 2. 复权因子表（独立，运行时复权；根治前复权增量假跳空）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS adjust_factor (
            代码 TEXT NOT NULL,
            除权日 TEXT NOT NULL,
            复权因子 REAL NOT NULL,
            每股分红 REAL, 送股比例 REAL, 转增比例 REAL, 配股比例 REAL,
            披露日 TEXT,
            PRIMARY KEY (代码, 除权日)
        )
    """)
    # 3. PIT股票池（根治 BUG-011 幸存者/后视）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS universe_pit (
            日期 TEXT NOT NULL,
            代码 TEXT NOT NULL,
            名称当日 TEXT,
            板块 TEXT,
            上市日 TEXT, 退市日 TEXT,
            是否ST当日 INTEGER,
            交易状态 TEXT,
            PRIMARY KEY (日期, 代码)
        )
    """)
    # 4. PIT财报（关键：披露日 announcement_date）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fundamental_pit (
            代码 TEXT NOT NULL,
            报告期 TEXT NOT NULL,
            披露日 TEXT NOT NULL,
            营收 REAL, 归母净利 REAL, 扣非 REAL,
            经营现金流 REAL, 净资产 REAL,
            PE_TTM REAL, PB REAL, 总市值 REAL, 流通市值 REAL,
            PRIMARY KEY (代码, 报告期)
        )
    """)
    # 5. 数据版本（复现性）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS data_version (
            版本 TEXT PRIMARY KEY,
            生成时间 TEXT,
            git_commit TEXT,
            来源清单 TEXT,
            说明 TEXT
        )
    """)
    # V11: 索引
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kline_raw_code_date ON daily_kline_raw(代码, 日期 DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_adjust_code ON adjust_factor(代码, 除权日 DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_universe_pit_date ON universe_pit(日期)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fund_pit_code ON fundamental_pit(代码, 披露日)")
    except sqlite3.OperationalError:
        pass
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
    """确保 baostock 已登录（含断线重连机制）"""
    if not _bs_session["logged_in"]:
        lg = bs.login()
        if lg.error_code != '0':
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        _bs_session["logged_in"] = True


def _bs_reconnect():
    """FIX: 断线重连 - 长时间运行时baostock可能断开连接，需重置会话状态"""
    try:
        bs.logout()
    except Exception:
        pass
    _bs_session["logged_in"] = False
    _bs_login()


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

    # FIX: 会话断开时自动重连重试一次（解决调度器长时间运行后连接失效问题）
    if rs.error_code != '0' and 'login' in str(rs.error_msg).lower():
        _bs_reconnect()
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,close,high,low,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2"
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
# 腾讯日K API（第三数据源）
# ============================================================

def fetch_stock_daily_tencent(code: str, start_date: str = None,
                               end_date: str = None) -> pd.DataFrame:
    """
    腾讯日K线 API（第三数据源）
    接口: http://web.ifzq.gtimg.cn/appstock/app/fqkline/get
    返回前复权日K，最多返回约320根K线
    """
    # 腾讯格式转换: 600xxx/51xxxx -> sh, 00xxxx/30xxxx/00xxxx -> sz
    if code.startswith(('6', '5', '9')):
        tc_code = f"sh{code}"
    else:
        tc_code = f"sz{code}"
    
    url = (f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={tc_code},day,,,320,qfq")
    
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Referer", "http://finance.qq.com")
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
        
        data = _json.loads(content)
        # 解析腾讯日K格式: data -> data -> {tc_code} -> day/qfqday -> [[date, open, close, high, low, volume], ...]
        stock_data = data.get("data", {}).get(tc_code, {})
        klines = stock_data.get("qfqday") or stock_data.get("day") or []
        
        if not klines:
            return pd.DataFrame()
        
        df = pd.DataFrame(klines)
        # 腾讯返回5-7列: [date, open, close, high, low, volume?, amount?]
        col_map = {0: "date", 1: "open", 2: "close", 3: "high", 4: "low", 5: "volume", 6: "amount"}
        df = df.rename(columns=col_map)
        if "volume" not in df.columns:
            df["volume"] = 0
        if "amount" not in df.columns:
            df["amount"] = 0.0
        
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=["close"])
        
        # 日期过滤
        if start_date:
            sd = start_date.replace("-", "")[:8]
            df = df[df["date"] >= f"{sd[:4]}-{sd[4:6]}-{sd[6:8]}"]
        if end_date:
            ed = end_date.replace("-", "")[:8]
            df = df[df["date"] <= f"{ed[:4]}-{ed[4:6]}-{ed[6:8]}"]
        
        return df.reset_index(drop=True)
    except Exception as e:
        raise RuntimeError(f"腾讯日K失败[{code}]: {e}")


# ============================================================
# 统一获取接口（自动切换数据源）
# ============================================================

def fetch_stock_daily(code: str, start_date: str = None, end_date: str = None,
                      retry_times: int = None, retry_interval: int = None) -> pd.DataFrame:
    """
    获取单只股票日线数据（带重试 + 自动切换数据源）
    优先使用 baostock，失败后尝试 akshare，再失败尝试腾讯日K，最后回退到本地数据库
    V6.0 P2-4: 新增本地数据库兜底，避免网络故障时完全无数据
    V6.1(2026-08-27): 新增腾讯日K API作为第三数据源
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

        # 再试腾讯日K API（第三数据源）
        try:
            df = fetch_stock_daily_tencent(code, start_date, end_date)
            if not df.empty:
                logger.info(f"[{code}] baostock/akshare失败，已切换到腾讯日K")
                return df
        except Exception as e:
            last_error = e
            logger.warning(f"[{code}] 腾讯日K 第{attempt}次失败: {e}")

        if attempt < retry_times:
            time.sleep(retry_interval)

    # V6.0 P2-4: 本地数据库兜底 —— 网络数据源全部失败时尝试从 stock_db.db 读取
    try:
        from data.data_loader import load_daily_data
        df = load_daily_data(code, days=120)
        if df is not None and not df.empty and len(df) >= 30:
            logger.warning(f"[{code}] 网络数据源均失败，已回退到本地数据库({len(df)}条)")
            return df
    except Exception as db_err:
        logger.debug(f"[{code}] 本地数据库兜底也失败: {db_err}")

    raise RuntimeError(f"[{code}] 所有数据源(含本地DB)均失败: {last_error}")


# ============================================================
# 增量更新到数据库
# ============================================================

def _validate_data_quality(conn: sqlite3.Connection, code: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    数据质量验证：过滤明显异常的数据行（向量化实现）
    
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
    
    original_len = len(df)
    mask_valid = pd.Series(True, index=df.index)
    removed = []
    
    # 规则0: 价格无效（向量化）
    close = pd.to_numeric(df['close'], errors='coerce')
    open_p = pd.to_numeric(df['open'], errors='coerce')
    bad_price = (close <= 0) | (open_p <= 0) | close.isna() | open_p.isna()
    if bad_price.any():
        for idx in df.index[bad_price]:
            removed.append(f"{df.loc[idx, 'date']}: 价格无效(close={df.loc[idx, 'close']})")
        mask_valid &= ~bad_price
    
    # 规则2: 单日跌幅超11%（向量化，用pct_change）
    if mask_valid.sum() >= 2:
        pct = close.pct_change()
        extreme_drop = pct < -0.11
        extreme_drop &= mask_valid  # 只看当前仍有效的行
        if extreme_drop.any():
            for idx in df.index[extreme_drop]:
                removed.append(
                    f"{df.loc[idx, 'date']}: 单日跌幅{pct.loc[idx]:.1%}超限(可能前复权错误)")
            mask_valid &= ~extreme_drop
    
    # 规则1: 成交量异常低 + 价格大幅变动（向量化）
    if avg_volume > 0:
        vol = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
        vol_ratio = vol / avg_volume
        prev_close = close.shift(1)
        price_change = ((close - prev_close) / prev_close).abs()
        # 量<均量2% 且 价格变动>5%
        vol_price_anomaly = (vol_ratio < 0.02) & (vol > 0) & (price_change > 0.05)
        vol_price_anomaly &= mask_valid
        if vol_price_anomaly.any():
            for idx in df.index[vol_price_anomaly]:
                removed.append(
                    f"{df.loc[idx, 'date']}: 量价异常(vol={vol.loc[idx]:.0f}, "
                    f"均量比={vol_ratio.loc[idx]:.3f}, 跌幅={price_change.loc[idx]:.1%})")
            mask_valid &= ~vol_price_anomaly
    
    if removed:
        logger.warning(f"[{code}] 数据质量验证: 过滤{len(removed)}条异常数据")
        for r in removed:
            logger.warning(f"  {r}")
    
    result = df.loc[mask_valid].reset_index(drop=True)
    return result


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
    try:
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
        return count
    finally:
        conn.close()


# ============================================================
# 盘后多源校验（确保收盘价准确）
# ============================================================

def _get_tiered_tolerance(price: float) -> float:
    """V10.4: 分段容差计算 —— 低价股容差窄、高价股容差宽

    原理: 低价股2%容差绝对值太小(如3元×2%=0.06元)容易误报，
    高价股2%容差绝对值太大(如200元×2%=4元)容易漏报。
    分段策略: ≤5元→1%, 5-50元→2%, >50元→0.5%
    """
    if price <= 0:
        return 0.02
    if price <= 5:
        return 0.01   # 低价股: 1%容差
    elif price <= 50:
        return 0.02   # 中价股: 2%容差
    else:
        return 0.005  # 高价股: 0.5%容差


def validate_close_prices(codes: list = None, tolerance: float = None) -> dict:
    """
    盘后收盘价交叉校验：对比DB中最新收盘价与实时行情API
    
    原理:
      盘后运行，用腾讯行情API获取当日收盘价，与DB中最新数据对比
      V10.4: 偏差阈值改为分段容差（低价股1%/中价股2%/高价股0.5%）
      偏差超限的标记为可疑，尝试用akshare数据修复
    
    参数:
        codes: 要校验的股票代码列表（默认用STOCK_POOL）
        tolerance: 允许偏差比例（None=使用分段容差，默认向后兼容）
    
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
        return {"_error": f"实时行情获取失败: {e}"}
    
    if not realtime:
        logger.warning("多源校验: 实时行情返回空结果，可能全部数据源失败")
        return {"_error": "实时行情返回空结果"}
    
    conn = init_db()
    try:
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

            # V10.4: 分段容差（若未指定固定tolerance）
            effective_tolerance = tolerance if tolerance is not None else _get_tiered_tolerance(real_price)

            if deviation <= effective_tolerance:
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

        # 统计
        mismatch_count = sum(1 for v in results.values() if v["status"] == "mismatch")
        fixed_count = sum(1 for v in results.values() if v["status"] == "fixed")
        if mismatch_count or fixed_count:
            logger.info(f"多源校验完成: {fixed_count}只已修复, {mismatch_count}只仍偏差")

        return results
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_all_candidate_codes() -> list:
    """
    汇总所有需要拉取数据的股票代码（去重）
    来源: STOCK_POOL + SECTOR_CANDIDATES + BENCHMARK_INDEX
    V10.5: 自动过滤数据陈旧股票（>180天未更新），减少无效拉取
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

    # V10.5: 过滤陈旧数据（last_update超过180天未更新的股票）
    try:
        conn = init_db()
        cursor = conn.cursor()
        cutoff = (datetime.date.today() - datetime.timedelta(days=180)).strftime("%Y-%m-%d")
        cursor.execute("SELECT code FROM last_update WHERE last_date >= ?", (cutoff,))
        fresh_codes = {row[0] for row in cursor.fetchall()}
        conn.close()
        # 保留: 在fresh_codes中的 或 无last_update记录的（新股）
        stale_removed = codes - fresh_codes - {config.BENCHMARK_INDEX}
        if stale_removed:
            logger.debug(f"[代码过滤] 过滤{len(stale_removed)}只陈旧股票(>180天未更新)")
        codes = codes & (fresh_codes | {config.BENCHMARK_INDEX})
    except Exception as e:
        logger.debug(f"[代码过滤] 陈旧检查失败，保留全部: {e}")

    return sorted(codes)


def check_data_freshness(max_stale_days: int = 3) -> dict:
    """
    检查K线数据新鲜度
    返回: {
        "ok": bool,           # 数据是否新鲜
        "latest_date": str,   # 最新数据日期
        "stale_days": int,    # 过时自然日数
        "stale_codes": list,  # 过时标的列表
        "total_codes": int,   # 总标的数
    }
    """
    try:
        conn = init_db()
        cursor = conn.cursor()
        # 获取所有有last_update记录的标的
        cursor.execute("SELECT code, last_date FROM last_update ORDER BY last_date DESC")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return {"ok": False, "latest_date": None, "stale_days": 999,
                    "stale_codes": [], "total_codes": 0}
        
        today = datetime.date.today()
        latest_overall = None
        stale_codes = []
        
        for code, last_date_str in rows:
            last_date = datetime.datetime.strptime(last_date_str, "%Y-%m-%d").date()
            days_stale = (today - last_date).days
            if days_stale > max_stale_days:
                stale_codes.append((code, last_date_str, days_stale))
            if latest_overall is None or last_date > latest_overall:
                latest_overall = last_date
        
        stale_days = (today - latest_overall).days if latest_overall else 999
        return {
            "ok": len(stale_codes) == 0,
            "latest_date": latest_overall.strftime("%Y-%m-%d") if latest_overall else None,
            "stale_days": stale_days,
            "stale_codes": stale_codes[:20],  # 最多返回20只
            "total_codes": len(rows),
        }
    except Exception as e:
        logger.error(f"数据新鲜度检查异常: {e}")
        return {"ok": False, "latest_date": None, "stale_days": 999,
                "stale_codes": [], "total_codes": 0}


def _update_single_akshare(code: str) -> int:
    """
    单只股票akshare更新（线程安全，用于并行拉取）
    每个线程独立连接数据库，避免SQLite并发问题
    返回: 新增记录数，失败返回-1
    """
    conn = None
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        last_date = get_last_update_date(conn, code)
        start_date = None
        if last_date:
            next_day = datetime.datetime.strptime(last_date, "%Y-%m-%d") + datetime.timedelta(days=1)
            start_date = next_day.strftime("%Y-%m-%d")

        end_date = datetime.date.today().strftime("%Y-%m-%d")
        if start_date and start_date > end_date:
            return 0

        # 用akshare拉取
        df = fetch_stock_daily_akshare(code, start_date=start_date, end_date=end_date)
        if df.empty:
            return 0

        # 数据质量验证
        df = _validate_data_quality(conn, code, df)
        if df.empty:
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
        return len(rows)
    except Exception as e:
        logger.debug(f"[{code}] akshare并行拉取失败: {e}")
        return -1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def batch_update_all(conn: sqlite3.Connection = None, full_pool: bool = True,
                     max_workers: int = None) -> dict:
    """
    批量更新所有股票池中的日线数据（并行优化版）
    
    策略:
      Phase 1: akshare并行拉取（max_workers个线程）
      Phase 2: 失败的用baostock顺序补拉
    
    参数:
        conn: 数据库连接（仅用于baostock补拉阶段）
        full_pool: True=更新全部候选股, False=仅更新STOCK_POOL
        max_workers: 并行线程数（默认=min(10, 候选股数)）
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

    # P1: 自适应并行度，默认 min(10, 候选股数)
    if max_workers is None:
        max_workers = min(getattr(config, 'DATA_FETCH_WORKERS', 10), len(codes))

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


# ============================================================
# 内存缓存层（减少重复SQLite查询）
# ============================================================
import functools

@functools.lru_cache(maxsize=256)
def _load_daily_data_cached(code: str, days: int, _db_mtime: float) -> pd.DataFrame:
    """
    load_daily_data 的内存缓存层
    
    参数:
        _db_mtime: 数据库文件修改时间戳，用于缓存失效触发
        code: 股票代码
        days: 加载天数
    
    返回: DataFrame (date升序)
    """
    conn = init_db()
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
        df = validate_dataframe(df, code)
        return df
    finally:
        try:
            conn.close()
        except Exception:
            pass


_DB_MTIME_CACHE = {"value": 0, "ts": 0}


def _get_db_mtime() -> float:
    """获取数据库文件修改时间（V10.5: 10秒内缓存，减少os.stat调用）"""
    import time as _time
    now = _time.time()
    if (now - _DB_MTIME_CACHE["ts"]) < 10:
        return _DB_MTIME_CACHE["value"]
    try:
        mtime = os.path.getmtime(config.DB_PATH)
        _DB_MTIME_CACHE["value"] = mtime
        _DB_MTIME_CACHE["ts"] = now
        return mtime
    except OSError:
        return 0


def load_daily_data(code: str, conn: sqlite3.Connection = None,
                    days: int = 120) -> pd.DataFrame:
    """
    从数据库加载某只股票最近N天的日线数据（带进程内缓存）
    
    缓存策略:
      - 同一进程内对 (code, days) 的查询结果缓存
      - 数据库文件被更新（mtime变化）时自动失效
      - 传入自定义conn时绕过缓存（保证调用方控制的数据新鲜度）
    
    返回按日期升序排列的DataFrame
    """
    # 自定义conn时走原路径（调用方可能刚写入数据）
    if conn is not None:
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
        return validate_dataframe(df, code)
    
    # 默认路径：使用缓存
    return _load_daily_data_cached(code, days, _get_db_mtime())


def batch_load_daily_data(codes: list, days: int = 120,
                          conn: sqlite3.Connection = None,
                          max_workers: int = 8) -> dict:
    """
    批量并行加载多只股票的日线数据（线程池加速）

    参数:
        codes: 股票代码列表
        days: 加载天数
        conn: 数据库连接（可选）
        max_workers: 并行线程数

    返回: {code: DataFrame}
    """
    data_dict = {}

    def _load_one(code):
        try:
            df = load_daily_data(code, conn=conn, days=days)
            return code, df
        except Exception as e:
            logger.warning(f"[{code}] 数据加载失败: {e}")
            return code, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_load_one, code) for code in codes]
        for future in as_completed(futures):
            code, df = future.result()
            if not df.empty:
                data_dict[code] = df

    logger.info(f"批量加载数据: {len(data_dict)}/{len(codes)}只成功")
    return data_dict


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
    更新单只股票的资金流向数据到数据库（executemany批量写入）
    返回新增记录数
    """
    init_capital_flow_table(conn)

    df = fetch_capital_flow_akshare(code)
    if df.empty:
        return 0

    cursor = conn.cursor()
    rows = [
        (code, row["date"],
         row.get("main_net", 0), row.get("super_net", 0),
         row.get("big_net", 0), row.get("mid_net", 0),
         row.get("small_net", 0))
        for _, row in df.iterrows()
    ]
    try:
        cursor.executemany("""
            INSERT OR REPLACE INTO capital_flow
            (code, date, main_net, super_net, big_net, mid_net, small_net)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
        return len(rows)
    except Exception as e:
        logger.error(f"[{code}] 资金流向批量写入失败: {e}")
        return 0


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


# V10.4: 静态节假日表（非交易日列表，格式 "MM-DD"）
# 每年初需更新，包含元旦/春节/清明/劳动节/端午/中秋/国庆等
# 2026年A股休市日（预估，实际以交易所公告为准）
_HOLIDAYS_2026 = {
    "01-01", "01-02",  # 元旦
    "02-16", "02-17", "02-18", "02-19", "02-20", "02-21", "02-22",  # 春节
    "04-04", "04-05", "04-06",  # 清明
    "05-01", "05-02", "05-03", "05-04", "05-05",  # 劳动节
    "05-31", "06-01", "06-02",  # 端午
    "09-25", "09-26", "09-27",  # 中秋
    "10-01", "10-02", "10-03", "10-04", "10-05", "10-06", "10-07",  # 国庆
}


def _is_trading_day(dt: datetime.datetime = None) -> bool:
    """V10.4: 判断是否为交易日（排除周末+法定节假日）"""
    if dt is None:
        dt = datetime.datetime.now()
    if dt.weekday() >= 5:
        return False
    mmdd = dt.strftime("%m-%d")
    # 根据年份选择节假日表（目前仅2026，其他年份默认视为交易日）
    year_holidays = {
        2026: _HOLIDAYS_2026,
    }.get(dt.year, set())
    if mmdd in year_holidays:
        return False
    return True


def _get_market_phase() -> str:
    """
    判断当前市场阶段
    V10.4增强:
      - 新增 "auction" 阶段（09:25-09:30集合竞价，可获取开盘价）
      - 新增交易日历感知（法定节假日返回 "closed"）
    返回: "pre_market" / "auction" / "intraday" / "post_market" / "closed"
    """
    now = datetime.datetime.now()

    # V10.4: 交易日历感知（非交易日直接返回closed）
    if not _is_trading_day(now):
        return "closed"

    t = now.hour * 100 + now.minute
    if t < 830:
        return "pre_market"
    elif t < 925:
        return "pre_market"  # 08:30-09:25 盘前准备
    elif t < 930:
        return "auction"     # V10.4: 09:25-09:30 集合竞价，可获取开盘价
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
    
    # ---- 盘中/集合竞价: 优先使用实时行情（带30秒缓存）----
    # V10.4: auction阶段也尝试获取实时价（开盘价）
    if phase in ("intraday", "auction"):
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
    own_conn = False
    if conn is None:
        conn = init_db()
        own_conn = True

    try:
        return _get_smart_price_batch_inner(codes, conn)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def _get_smart_price_batch_inner(codes: list, conn: sqlite3.Connection) -> dict:
    """get_smart_price_batch 内部实现（conn由调用方管理）"""
    
    phase = _get_market_phase()
    results = {}
    
    # 盘中/集合竞价批量获取实时行情（一次网络请求）
    # V10.4: auction阶段也尝试获取
    if phase in ("intraday", "auction"):
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


# ============================================================
# V3.2: 数据质量自动监控
# ============================================================

class DataQualityReport:
    """数据质量检测报告"""
    def __init__(self):
        self.issues = []  # [(level, code, message)]
    
    def add(self, level: str, code: str, msg: str):
        self.issues.append((level, code, msg))
        if level == "ERROR":
            logger.error(f"[数据质量] {code}: {msg}")
        elif level == "WARN":
            logger.warning(f"[数据质量] {code}: {msg}")
    
    @property
    def has_errors(self) -> bool:
        return any(lv == "ERROR" for lv, _, _ in self.issues)
    
    @property
    def summary(self) -> str:
        errs = sum(1 for lv, _, _ in self.issues if lv == "ERROR")
        warns = sum(1 for lv, _, _ in self.issues if lv == "WARN")
        return f"{errs}个错误, {warns}个警告"


def validate_dataframe(df: pd.DataFrame, code: str,
                       report: DataQualityReport = None) -> pd.DataFrame:
    """
    V3.2: 数据质量自动校验（每次load后自动调用）
    
    检查项:
    1. 空值/NaN检测
    2. 价格为0或负数
    3. 成交量为0（停牌未标记）
    4. OHLC逻辑关系 (high>=low, high>=open/close)
    5. 复权异常（单日涨跌幅>20%非ST）
    6. 日期连续性（缺失交易日）
    
    返回: 清洗后的DataFrame（严重问题行被移除）
    """
    if report is None:
        report = DataQualityReport()
    
    if df is None or df.empty:
        report.add("ERROR", code, "数据为空")
        return df
    
    original_len = len(df)
    
    # 1. 空值检测
    null_cols = df[['open', 'close', 'high', 'low']].isnull().any(axis=1)
    if null_cols.sum() > 0:
        report.add("ERROR", code, f"价格空值{null_cols.sum()}行，已移除")
        df = df[~null_cols].reset_index(drop=True)
    
    # 2. 价格<=0
    bad_price = (df['close'] <= 0) | (df['open'] <= 0)
    if bad_price.sum() > 0:
        report.add("ERROR", code, f"价格<=0共{bad_price.sum()}行，已移除")
        df = df[~bad_price].reset_index(drop=True)
    
    # 3. 成交量为0（可能停牌）
    if 'volume' in df.columns:
        zero_vol = df['volume'] == 0
        if zero_vol.sum() > 3:  # 超过3天量为0才警告
            report.add("WARN", code, f"成交量为0共{zero_vol.sum()}天，疑似停牌")
        # V10.5: 连续≥5天volume=0标记为停牌，因子计算时排除这些行
        if zero_vol.sum() >= 5:
            # 标记连续停牌段（volume=0且持续≥5天）
            is_suspended = zero_vol
            report.add("WARN", code,
                       f"停牌标记: {is_suspended.sum()}天volume=0，因子计算时请注意")
    
    # 4. OHLC逻辑关系
    if len(df) > 0:
        bad_ohlc = (df['high'] < df['low']) | (df['high'] < df['open']) | (df['high'] < df['close'])
        if bad_ohlc.sum() > 0:
            report.add("ERROR", code, f"OHLC逻辑异常{bad_ohlc.sum()}行(high<low)，已移除")
            df = df[~bad_ohlc].reset_index(drop=True)
    
    # 5. 复权异常（单日涨跌幅>20%）
    if len(df) >= 2:
        pct_change = df['close'].pct_change().abs()
        extreme = pct_change > 0.20
        if extreme.sum() > 0:
            # V8.2: 统一使用instrument模块判定ETF
            from utils.instrument import is_etf as _is_etf
            is_etf = _is_etf(code)
            if not is_etf:
                report.add("WARN", code,
                           f"单日涨跌幅>20%共{extreme.sum()}天，疑似复权错误")
    
    # 6. 日期连续性（缺失超过3天警告）
    if len(df) >= 5 and 'date' in df.columns:
        try:
            dates = pd.to_datetime(df['date'])
            gaps = dates.diff().dt.days
            large_gaps = gaps[gaps > 5]  # 超过5天无数据（排除周末）
            if len(large_gaps) > 0:
                report.add("WARN", code,
                           f"数据缺口{len(large_gaps)}处(最长{large_gaps.max():.0f}天)")
        except Exception:
            pass
    
    if len(df) < original_len:
        logger.info(f"[数据质量] {code}: 清洗{original_len - len(df)}行异常数据")
    
    return df


# ============================================================
# V3.2: 分钟级K线数据获取与加载
# ============================================================

def fetch_minute_data(code: str, freq: str = "5", days: int = 5,
                      conn: sqlite3.Connection = None) -> pd.DataFrame:
    """
    V3.2: 获取分钟级K线数据并存储到SQLite
    
    数据源: akshare(东方财富分钟线) 或 腐讯实时API
    
    参数:
        code: 股票代码
        freq: 频率 "5"/"15"/"60"
        days: 获取最近N天
        conn: 数据库连接
    
    返回:
        DataFrame: datetime, open, close, high, low, volume, amount
    """
    own_conn = False
    if conn is None:
        conn = init_db()
        own_conn = True
    
    df = pd.DataFrame()
    try:
        if HAS_AKSHARE:
            # akshare分钟线（东方财富）
            period_map = {"5": "5", "15": "15", "60": "60"}
            period = period_map.get(freq, "5")
            raw = ak.stock_zh_a_minute(
                symbol=_to_baostock_code(code).replace(".", ""),
                period=period,
                adjust="qfq"
            )
            if raw is not None and not raw.empty:
                df = raw.rename(columns={
                    "day": "datetime", "open": "open", "close": "close",
                    "high": "high", "low": "low", "volume": "volume"
                })
                if "amount" not in df.columns:
                    df["amount"] = df["volume"] * df["close"]
                df["code"] = code
                df["freq"] = freq
                # 只保留最近N天
                if len(df) > 0:
                    df["datetime"] = pd.to_datetime(df["datetime"])
                    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
                    df = df[df["datetime"] >= cutoff]
    except Exception as e:
        logger.warning(f"[分钟线] {code} akshare获取失败: {e}")
    
    # 存储到SQLite
    if not df.empty:
        try:
            save_df = df[["code", "datetime", "freq", "open", "close",
                          "high", "low", "volume", "amount"]].copy()
            save_df["datetime"] = save_df["datetime"].astype(str)
            save_df.to_sql("minute_kline", conn, if_exists="append",
                          index=False, method="multi")
            # 去重（保留最新）
            conn.execute("""
                DELETE FROM minute_kline WHERE rowid NOT IN (
                    SELECT MAX(rowid) FROM minute_kline
                    GROUP BY code, datetime, freq
                )
            """)
            conn.commit()
            logger.info(f"[分钟线] {code} {freq}min: 存储{len(save_df)}条")
        except Exception as e:
            logger.warning(f"[分钟线] {code} 存储失败: {e}")
    
    if own_conn:
        conn.close()
    return df


def load_minute_data(code: str, freq: str = "5", bars: int = 240,
                     conn: sqlite3.Connection = None) -> pd.DataFrame:
    """
    V3.2: 从SQLite加载分钟级K线
    
    参数:
        code: 股票代码
        freq: 频率 "5"/"15"/"60"
        bars: 加载最近N根K线
        conn: 数据库连接
    
    返回:
        DataFrame: datetime, open, close, high, low, volume, amount (升序)
    """
    own_conn = False
    if conn is None:
        conn = init_db()
        own_conn = True
    
    try:
        query = """
            SELECT datetime, open, close, high, low, volume, amount
            FROM minute_kline
            WHERE code = ? AND freq = ?
            ORDER BY datetime DESC
            LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=(code, freq, bars))
        if df.empty:
            return df
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# V3.2: 数据管道故障告警+自动重试
# ============================================================

def retry_with_fallback(max_retries: int = 3, delay: float = 2.0,
                        fallback_fn=None):
    """
    V3.2: 数据获取重试装饰器
    
    失败时自动重试N次，每次间隔delay秒。
    全部失败后调用fallback_fn（如返回缓存数据）并发送告警。
    
    用法:
        @retry_with_fallback(max_retries=3, fallback_fn=_load_from_cache)
        def fetch_data(code):
            ...
    """
    def decorator(func):
        import functools
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(max_retries):
                try:
                    result = func(*args, **kwargs)
                    if result is not None:
                        return result
                    # 返回None视为失败
                    last_err = Exception("返回None")
                except Exception as e:
                    last_err = e
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"[数据管道] {func.__name__} 第{attempt+1}次失败: {e}, "
                            f"{delay}秒后重试..."
                        )
                        time.sleep(delay)
            
            # 全部失败
            logger.error(
                f"[数据管道] {func.__name__} {max_retries}次重试均失败: {last_err}"
            )
            # 尝试降级
            if fallback_fn is not None:
                try:
                    logger.info(f"[数据管道] 尝试降级方案: {fallback_fn.__name__}")
                    return fallback_fn(*args, **kwargs)
                except Exception as fb_err:
                    logger.error(f"[数据管道] 降级也失败: {fb_err}")
            return None
        return wrapper
    return decorator


# ============================================================
# V7.0 Phase0: 基本面数据 + 行业分类 加载/存储
# ============================================================

def load_fundamental(code: str = None, conn: sqlite3.Connection = None) -> pd.DataFrame:
    """
    加载基本面数据

    参数:
        code: 股票代码，None=加载全部
        conn: 数据库连接，None=自动创建

    返回:
        DataFrame，含 pe_ttm/pb/roe/gross_margin/debt_ratio 等字段
    """
    _close = False
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH)
        _close = True
    try:
        if code:
            query = "SELECT * FROM fundamental WHERE code=? ORDER BY report_date DESC"
            df = pd.read_sql(query, conn, params=(code,))
        else:
            query = "SELECT * FROM fundamental ORDER BY code, report_date DESC"
            df = pd.read_sql(query, conn)
        return df
    except Exception as e:
        logger.warning(f"[基本面] 加载失败: {e}")
        return pd.DataFrame()
    finally:
        if _close:
            conn.close()


def save_fundamental_batch(data_dict: dict, conn: sqlite3.Connection = None) -> int:
    """
    批量保存基本面数据

    参数:
        data_dict: {code: {pe_ttm, pb, roe, ...}} 字典
        conn: 数据库连接

    返回:
        成功保存的记录数
    """
    _close = False
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH)
        _close = True
    try:
        today = datetime.date.today().strftime("%Y-%m-%d")
        cursor = conn.cursor()
        count = 0
        for code, data in data_dict.items():
            report_date = data.get("report_date", today)
            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO fundamental
                    (code, report_date, update_date,
                     pe_ttm, pb, roe, gross_margin, debt_ratio,
                     revenue_growth, net_profit_growth, eps_growth_q, eps_growth_3y,
                     pe_percentile, dividend_yield, total_mcap, circ_mcap)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    code, report_date, today,
                    data.get("pe_ttm"), data.get("pb"), data.get("roe"),
                    data.get("gross_margin"), data.get("debt_ratio"),
                    data.get("revenue_growth"), data.get("net_profit_growth"),
                    data.get("eps_growth_q"), data.get("eps_growth_3y"),
                    data.get("pe_percentile"), data.get("dividend_yield"),
                    data.get("total_mcap"), data.get("circ_mcap"),
                ))
                count += 1
            except Exception as e:
                logger.debug(f"[基本面] {code}保存失败: {e}")
        conn.commit()
        logger.info(f"[基本面] 保存{count}条记录")
        return count
    finally:
        if _close:
            conn.close()


def load_industry_map(conn: sqlite3.Connection = None) -> dict:
    """
    加载行业分类映射

    返回:
        {code: industry_name} 字典，优先申万行业，其次东财行业
    """
    _close = False
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH)
        _close = True
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, industry_sw, industry_em FROM stock_industry")
        result = {}
        for row in cursor.fetchall():
            code = row[0]
            # 优先申万行业，其次东财
            industry = row[1] or row[2] or "未知"
            result[code] = industry
        return result
    except Exception as e:
        logger.warning(f"[行业] 加载失败: {e}")
        return {}
    finally:
        if _close:
            conn.close()


def save_industry_map(industry_dict: dict, source: str = "sw",
                      conn: sqlite3.Connection = None) -> int:
    """
    保存行业分类映射

    参数:
        industry_dict: {code: industry_name}
        source: 行业分类来源 (sw=申万, em=东财)

    返回:
        保存记录数
    """
    _close = False
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH)
        _close = True
    try:
        today = datetime.date.today().strftime("%Y-%m-%d")
        cursor = conn.cursor()
        col = "industry_sw" if source == "sw" else "industry_em"
        count = 0
        for code, industry in industry_dict.items():
            try:
                cursor.execute(f"""
                    INSERT OR REPLACE INTO stock_industry (code, {col}, update_date)
                    VALUES (?, ?, ?)
                """, (code, industry, today))
                count += 1
            except Exception as e:
                logger.debug(f"[行业] {code}保存失败: {e}")
        conn.commit()
        logger.info(f"[行业] 保存{count}条行业分类({source})")
        return count
    finally:
        if _close:
            conn.close()


def fetch_industry_from_baostock(codes: list) -> dict:
    """
    从baostock获取行业分类

    参数:
        codes: 股票代码列表

    返回:
        {code: industry_name}
    """
    if not HAS_BAOSTOCK:
        logger.warning("[行业] baostock未安装，无法获取行业分类")
        return {}

    _bs_login()
    result = {}
    try:
        rs = bs.query_stock_industry()
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            # baostock格式: [code, code_name, industry, industryClassify]
            if len(row) >= 3:
                bs_code = row[0]
                industry = row[2]
                # 转换为纯数字代码
                pure_code = bs_code.split(".")[-1] if "." in bs_code else bs_code
                if industry and industry != "":
                    result[pure_code] = industry
    except Exception as e:
        logger.warning(f"[行业] baostock获取行业失败: {e}")

    logger.info(f"[行业] 从baostock获取到{len(result)}条行业分类")
    return result


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
