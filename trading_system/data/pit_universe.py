"""
PIT（Point-in-Time）可交易宇宙服务
====================================
在任意历史日 T，还原"当时真实可交易的全部 A 股"：
已上市、未退市、非停牌，并可配置是否纳入 ST / 创业板(300) / 科创板(688) / 北交所。

核心接口:
    get_pit_universe(date, 含ST=False, 允许板块=("主板","创业板","科创板"), 排除停牌=True)

数据依赖:
    - stock_basic 表（全市场基础信息，含 ipo_date / out_date）
    - stock_status_history 表（ST 时变状态，可选）
    - daily_kline 表（停牌检测: volume=0）

数据采集:
    fetch_all_stock_basic() 从 baostock 批量拉取全市场信息并落库。

使用:
    from data.pit_universe import PITUniverse
    pit = PITUniverse()
    codes = pit.get_pit_universe("2024-06-01")
"""

import os
import sys
import sqlite3
import datetime
import logging
import time

import pandas as pd  # noqa: F401 (预留供后续扩展)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.data_loader import init_db

logger = logging.getLogger(__name__)

# baostock
try:
    import baostock as bs
    HAS_BAOSTOCK = True
except ImportError:
    HAS_BAOSTOCK = False


def _to_bs_code(code: str) -> str:
    """纯数字 -> baostock格式"""
    if "." in code:
        return code
    if code.startswith(("6", "9", "5")) or code == "000300":
        return f"sh.{code}"
    return f"sz.{code}"


def _from_bs_code(bs_code: str) -> str:
    """baostock格式 -> 纯数字"""
    return bs_code.split(".")[-1] if "." in bs_code else bs_code


def _infer_board(code: str) -> str:
    """根据代码推断板块"""
    if code.startswith("60"):
        return "主板"
    elif code.startswith("00"):
        return "主板"
    elif code.startswith("300"):
        return "创业板"
    elif code.startswith("688"):
        return "科创板"
    elif code.startswith(("4", "8")):
        return "北交所"
    return "其他"


class PITUniverse:
    """PIT 可交易宇宙服务"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = init_db(self.db_path)
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ============================================================
    # A-1: 全市场基础信息采集（baostock → stock_basic 表）
    # ============================================================

    def fetch_all_stock_basic(self) -> int:
        """
        从 baostock 批量拉取全市场股票基础信息并写入 stock_basic 表。

        返回: 写入/更新的记录数
        """
        if not HAS_BAOSTOCK:
            logger.error("baostock 未安装，无法采集全市场基础信息")
            return 0

        lg = bs.login()
        if lg.error_code != '0':
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return 0

        try:
            return self._do_fetch_stock_basic()
        finally:
            bs.logout()

    def _do_fetch_stock_basic(self) -> int:
        """执行采集逻辑"""
        # 回退到最近交易日（周末/节假日 query_all_stock 返回空）
        target_date = datetime.date.today()
        for _ in range(7):
            rs_test = bs.query_all_stock(day=target_date.strftime("%Y-%m-%d"))
            if rs_test.error_code == '0' and rs_test.next():
                break  # 找到有数据的交易日
            target_date -= datetime.timedelta(days=1)
        today = target_date.strftime("%Y-%m-%d")
        logger.info(f"采集日期: {today} (回退至最近交易日)")

        # 1. 获取当前日期的全市场列表
        rs = bs.query_all_stock(day=today)
        if rs.error_code != '0':
            logger.error(f"获取全A股列表失败: {rs.error_msg}")
            return 0

        rows = []
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            bs_code = row[0]
            pure_code = _from_bs_code(bs_code)
            # 只要A股（6/0/3/688）
            if pure_code.startswith(("6", "0", "3")):
                name = row[2] if len(row) > 2 else ""
                rows.append({
                    "code": pure_code,
                    "name": name,
                    "board": _infer_board(pure_code),
                    "status": "1",  # trade_status
                })

        logger.info(f"baostock 全市场列表: {len(rows)}只")

        # 2. 逐只获取 IPO/退市日期 + 行业信息（分批，避免请求过快）
        count = 0
        conn = self.conn
        cursor = conn.cursor()

        for i, row in enumerate(rows):
            code = row["code"]
            bs_code = _to_bs_code(code)

            try:
                # 获取股票基础信息（含 ipoDate）
                rs2 = bs.query_stock_basic(code=bs_code)
                ipo_date = ""
                out_date = ""
                status = "上市"

                if rs2.error_code == '0' and rs2.next():
                    data = rs2.get_row_data()
                    # query_stock_basic 返回: code, code_name, ipoDate, outDate, ...
                    if len(data) >= 4:
                        ipo_date = data[2] if data[2] else ""
                        out_date = data[3] if data[3] else ""
                        status = "退市" if out_date else "上市"

                # 写入 stock_basic
                cursor.execute("""
                    INSERT OR REPLACE INTO stock_basic
                    (code, name, board, ipo_date, out_date, status, update_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (code, row["name"], row["board"],
                      ipo_date, out_date, status, today))
                count += 1

            except Exception as e:
                logger.debug(f"  [{code}] 采集异常: {e}")

            # 进度报告 + 限速
            if (i + 1) % 200 == 0:
                conn.commit()
                logger.info(f"  采集进度: {i+1}/{len(rows)}, 已写入{count}")
                time.sleep(0.3)

        # 3. 补充行业信息
        try:
            rs_ind = bs.query_stock_industry()
            if rs_ind.error_code == '0':
                while rs_ind.next():
                    ind_data = rs_ind.get_row_data()
                    if len(ind_data) >= 4:
                        ind_code = _from_bs_code(ind_data[1])
                        ind_name = ind_data[3] if len(ind_data) > 3 else ""
                        cursor.execute("""
                            UPDATE stock_basic SET industry_sw=? WHERE code=?
                        """, (ind_name, ind_code))
        except Exception as e:
            logger.debug(f"行业信息采集异常: {e}")

        conn.commit()
        logger.info(f"stock_basic 采集完成: {count}只")
        return count

    # ============================================================
    # A-3: PIT 可交易宇宙查询
    # ============================================================

    def get_pit_universe(self, date: str,
                         含ST: bool = False,
                         允许板块: tuple = ("主板", "创业板"),
                         排除停牌: bool = True) -> list:
        """
        返回 date 当日 point-in-time 可交易代码列表。

        条件:
          - ipo_date <= date（已上市）
          - out_date > date OR out_date IS NULL（未退市）
          - 按配置过滤板块
          - 可选排除 ST（基于 stock_status_history 或名称推断）
          - 可选排除停牌（当日 volume=0）

        参数:
            date: 查询日期 YYYY-MM-DD
            含ST: 是否包含 ST 股票（默认排除）
            允许板块: 允许的板块列表
            排除停牌: 是否排除当日停牌股票

        返回:
            list[str]: 可交易代码列表
        """
        conn = self.conn
        cursor = conn.cursor()

        # 1. 基础过滤: 已上市 + 未退市 + 板块
        placeholders = ",".join(["?"] * len(允许板块))
        query = f"""
            SELECT code FROM stock_basic
            WHERE ipo_date <= ?
              AND (out_date > ? OR out_date IS NULL OR out_date = '')
              AND board IN ({placeholders})
        """
        params = [date, date] + list(允许板块)

        try:
            cursor.execute(query, params)
            candidates = [row[0] for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            # stock_basic 表可能为空（未执行采集），降级为空列表
            logger.warning("stock_basic 表为空或不存在，请先运行 fetch_all_stock_basic()")
            return []

        if not candidates:
            return []

        # 2. ST 过滤
        if not 含ST:
            candidates = self._filter_st(candidates, date)

        # 3. 停牌过滤
        if 排除停牌:
            candidates = self._filter_suspended(candidates, date)

        logger.info(f"PIT宇宙({date}): {len(candidates)}只 "
                    f"(板块={允许板块}, 含ST={含ST}, 排除停牌={排除停牌})")
        return candidates

    def _filter_st(self, codes: list, date: str) -> list:
        """排除 ST 股票（优先查 stock_status_history，降级用名称推断）"""
        conn = self.conn
        cursor = conn.cursor()

        # 尝试从 stock_status_history 查
        try:
            cursor.execute("""
                SELECT DISTINCT code FROM stock_status_history
                WHERE date = ? AND status_tag IN ('ST', '*ST')
            """, (date,))
            st_from_history = {row[0] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            st_from_history = set()

        # 降级: 从 stock_basic.name 推断
        filtered = []
        for code in codes:
            if code in st_from_history:
                continue
            cursor.execute("SELECT name FROM stock_basic WHERE code=?", (code,))
            row = cursor.fetchone()
            name = row[0] if row else ""
            if "ST" in name.upper():
                continue
            filtered.append(code)

        return filtered

    def _filter_suspended(self, codes: list, date: str) -> list:
        """排除当日停牌股票（volume=0 或无数据）"""
        conn = self.conn
        cursor = conn.cursor()

        # 批量查询当日有成交的股票
        placeholders = ",".join(["?"] * len(codes))
        try:
            cursor.execute(f"""
                SELECT DISTINCT code FROM daily_kline
                WHERE code IN ({placeholders})
                  AND date = ?
                  AND volume > 0
            """, codes + [date])
            active = {row[0] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            return codes

        # 如果 daily_kline 中没有当日数据，可能是非交易日或数据缺失，
        # 保守策略：保留所有候选（不做停牌过滤）
        if not active:
            return codes

        return [c for c in codes if c in active]

    # ============================================================
    # 便捷方法
    # ============================================================

    def get_stock_count(self, date: str = None) -> int:
        """获取 stock_basic 表中的总股票数"""
        conn = self.conn
        cursor = conn.cursor()
        try:
            if date:
                cursor.execute("""
                    SELECT COUNT(*) FROM stock_basic
                    WHERE ipo_date <= ? AND (out_date > ? OR out_date IS NULL OR out_date = '')
                """, (date, date))
            else:
                cursor.execute("SELECT COUNT(*) FROM stock_basic")
            return cursor.fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    def get_stock_info(self, code: str) -> dict:
        """获取单只股票基础信息"""
        conn = self.conn
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT * FROM stock_basic WHERE code=?", (code,))
            row = cursor.fetchone()
            if row:
                cols = ["code", "name", "board", "ipo_date", "out_date",
                        "status", "industry_sw", "industry_em", "update_date"]
                return dict(zip(cols, row))
        except sqlite3.OperationalError:
            pass
        return {}
