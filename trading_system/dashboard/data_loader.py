# -*- coding: utf-8 -*-
"""
统一数据加载层
===============
所有页面通过此模块加载数据，内置缓存+错误处理+优雅降级。
持仓数据统一走 config.load_holdings()，确保与 update_holdings.py 一致。
"""

import os
import sys
import json
import sqlite3
import datetime
import logging
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config

logger = logging.getLogger(__name__)

# 路径常量
DATA_DIR = config.DATA_DIR
HOLDINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "holdings.json")
LIFECYCLE_PATH = os.path.join(DATA_DIR, "strategy_lifecycle.json")
IC_HISTORY_PATH = os.path.join(DATA_DIR, "ic_history.json")
IC_PENDING_PATH = os.path.join(DATA_DIR, "ic_pending.json")
PNL_DB_PATH = os.path.join(DATA_DIR, "pnl_history.db")
RISK_STATE_PATH = os.path.join(DATA_DIR, "risk_state.json")
MARKET_CACHE_PATH = os.path.join(DATA_DIR, "market_pulse_cache.json")
ALERT_HISTORY_PATH = os.path.join(DATA_DIR, "alert_eval_history.json")
INTRADAY_STATE_PATH = os.path.join(DATA_DIR, "intraday_state.json")
BUY_SIGNAL_PATH = os.path.join(DATA_DIR, "buy_signal_history.json")
SCAN_CACHE_PATH = os.path.join(DATA_DIR, "scan_cache.json")
PREDICTION_PATH = os.path.join(DATA_DIR, "prediction_history.json")
COHORT_PERF_PATH = os.path.join(DATA_DIR, "cohort_performance.json")
OUTPUT_DIR = config.OUTPUT_DIR


# ============================================================
# 持仓数据（核心：统一走 config.load_holdings）
# ============================================================

def load_holdings_realtime() -> dict:
    """
    实时加载持仓数据（每次调用都重新读取文件，不缓存）

    返回:
        {code: {name, shares, buy_price, price, ...}}
        空dict表示无持仓或文件不存在
    """
    try:
        return config.load_holdings(validated=True)
    except Exception as e:
        logger.warning(f"[数据层] 持仓加载失败: {e}")
        return {}


def calc_holdings_pnl(holdings: dict) -> dict:
    """
    实时计算持仓盈亏（不缓存，每次重算）

    返回:
        {code: {value, cost, pnl, pnl_pct, weight, ...}}
    """
    result = {}
    total_value = 0
    for code, pos in holdings.items():
        shares = pos.get("shares", 0)
        cost = pos.get("buy_price", pos.get("cost", 0))
        # 价格字段优先级: current_price > price > last_price > cost
        price = pos.get("current_price",
                        pos.get("price",
                                pos.get("last_price", cost)))
        value = shares * price
        cost_total = shares * cost
        pnl = value - cost_total
        pnl_pct = (pnl / cost_total * 100) if cost_total > 0 else 0
        total_value += value
        result[code] = {
            "name": pos.get("name", code),
            "shares": shares,
            "cost": cost,
            "price": price,
            "value": value,
            "cost_total": cost_total,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "sector": pos.get("sector", ""),
            "stop_loss": pos.get("stop_loss", 0),
            "active": shares > 0,
        }
    # 计算仓位占比（仅基于活跃持仓）
    if total_value > 0:
        for code in result:
            result[code]["weight"] = result[code]["value"] / total_value
    return result


def filter_active_holdings(holdings: dict) -> dict:
    """过滤出活跃持仓（shares > 0 且 buy_price > 0）"""
    return {code: pos for code, pos in holdings.items()
            if pos.get("shares", 0) > 0 and pos.get("buy_price", 0) > 0}


# ============================================================
# JSON文件加载（带错误处理）
# ============================================================

def _load_json(path: str, default=None):
    """安全加载JSON文件"""
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[数据层] JSON加载失败({path}): {e}")
        return default


def load_lifecycle() -> dict:
    return _load_json(LIFECYCLE_PATH)


def load_ic_history() -> dict:
    return _load_json(IC_HISTORY_PATH)


def load_ic_pending() -> dict:
    return _load_json(IC_PENDING_PATH)


def load_risk_state() -> dict:
    return _load_json(RISK_STATE_PATH)


def load_market_state() -> dict:
    return _load_json(MARKET_CACHE_PATH)


def load_alert_history() -> list:
    data = _load_json(ALERT_HISTORY_PATH, default={})
    # alert_eval_history.json 格式: {"records": [...], "pending": [...]}
    if isinstance(data, dict):
        return data.get("records", [])
    if isinstance(data, list):
        return data
    return []


def load_intraday_state() -> dict:
    return _load_json(INTRADAY_STATE_PATH)


def load_buy_signals() -> list:
    data = _load_json(BUY_SIGNAL_PATH, default={})
    # buy_signal_history.json 格式: {"signals": [...], "updated": "..."}
    if isinstance(data, dict):
        return data.get("signals", [])
    if isinstance(data, list):
        return data
    return []


def load_cohort_performance() -> list:
    return _load_json(COHORT_PERF_PATH, default=[])


def load_market_pulse() -> dict:
    """加载市场情绪数据 (market_pulse_cache.json)"""
    data = _load_json(MARKET_CACHE_PATH)
    if isinstance(data, dict) and "latest" in data:
        return data["latest"]
    return data


def load_scan_cache() -> dict:
    """加载选股缓存数据 (scan_cache.json)"""
    data = _load_json(SCAN_CACHE_PATH)
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def load_prediction_history() -> list:
    """加载预测历史 (prediction_history.json)"""
    data = _load_json(PREDICTION_PATH, default=[])
    if isinstance(data, list):
        return data
    return []


def load_config_risk_params() -> dict:
    """从 config.py 读取风控参数"""
    params = {}
    try:
        params["MAX_HOLDINGS"] = getattr(config, "MAX_HOLDINGS", 8)
        params["MAX_SINGLE_STOCK_RATIO"] = getattr(config, "MAX_SINGLE_STOCK_RATIO", 0.15)
        params["MAX_SINGLE_ETF_RATIO"] = getattr(config, "MAX_SINGLE_ETF_RATIO", 0.20)
        params["SECTOR_MAX_RATIO"] = getattr(config, "SECTOR_MAX_RATIO", 0.40)
        params["FULL_POSITION_THRESHOLD"] = getattr(config, "FULL_POSITION_THRESHOLD", 0.90)
        params["NEAR_FULL_POSITION"] = getattr(config, "NEAR_FULL_POSITION", 0.80)
        params["INITIAL_STOP_LOSS_PCT"] = getattr(config, "INITIAL_STOP_LOSS_PCT", 0.12)
        params["DAILY_LOSS_LIMIT_1"] = getattr(config, "DAILY_LOSS_LIMIT_1", 0.02)
        params["DAILY_LOSS_LIMIT_2"] = getattr(config, "DAILY_LOSS_LIMIT_2", 0.03)
        params["WEEKLY_LOSS_LIMIT"] = getattr(config, "WEEKLY_LOSS_LIMIT", 0.08)
        params["TOTAL_CAPITAL"] = getattr(config, "TOTAL_CAPITAL", 0)
        params["AVAILABLE_CASH"] = getattr(config, "AVAILABLE_CASH", 0)
        # 统一风控配置
        risk_unified = getattr(config, "RISK_UNIFIED_CONFIG", {})
        if risk_unified:
            params["unified_max_holdings"] = risk_unified.get("max_holdings", 7)
            params["consec_loss_pause"] = risk_unified.get("consec_loss_pause", 3)
            params["consec_loss_halt"] = risk_unified.get("consec_loss_halt", 5)
    except Exception as e:
        logger.warning(f"[数据层] 风控参数加载失败: {e}")
    return params


# ============================================================
# SQLite数据加载
# ============================================================

def load_pnl_history(days: int = 60) -> pd.DataFrame:
    """加载历史P&L数据"""
    if not os.path.exists(PNL_DB_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(PNL_DB_PATH)
        df = pd.read_sql(
            f"SELECT * FROM daily_pnl ORDER BY date DESC LIMIT {days}", conn)
        conn.close()
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
        return df
    except Exception as e:
        logger.warning(f"[数据层] P&L加载失败: {e}")
        return pd.DataFrame()


def load_daily_kline(code: str, days: int = 250) -> pd.DataFrame:
    """加载个股日K线"""
    db_path = config.DB_PATH
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(db_path)
        df = pd.read_sql(
            f"SELECT * FROM daily_kline WHERE code=? ORDER BY date DESC LIMIT {days}",
            conn, params=(code,))
        conn.close()
        if not df.empty:
            df = df.sort_values("date")
        return df
    except Exception as e:
        logger.warning(f"[数据层] K线加载失败({code}): {e}")
        return pd.DataFrame()


def load_fundamental_data() -> pd.DataFrame:
    """加载基本面数据"""
    db_path = config.DB_PATH
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(db_path)
        df = pd.read_sql("SELECT * FROM fundamental ORDER BY code", conn)
        conn.close()
        return df
    except Exception as e:
        logger.warning(f"[数据层] 基本面数据加载失败: {e}")
        return pd.DataFrame()


# ============================================================
# 因子数据加载
# ============================================================

def load_factor_validation(phase: int = None) -> dict:
    """加载因子验证结果"""
    results = {}
    for p in ([phase] if phase else [1, 2]):
        path = os.path.join(OUTPUT_DIR, f"phase{p}_factor_validation.json")
        data = _load_json(path)
        if data:
            results.update(data)
    return results


def load_all_factor_ic() -> dict:
    """
    加载所有因子的IC统计（从ic_history.json解析）

    返回:
        {factor_name: {ic_mean, ic_std, ir, ic_win_rate, n_periods, status}}
    """
    ic_data = load_ic_history()
    result = {}
    for name, records in ic_data.items():
        if not records:
            continue
        ics = [r['ic'] if isinstance(r, dict) else r for r in records]
        ics = [v for v in ics if v is not None and not (isinstance(v, float) and np.isnan(v))]
        if len(ics) < 2:
            continue
        arr = np.array(ics, dtype=float)
        ic_mean = float(arr.mean())
        ic_std = float(arr.std())
        ir = ic_mean / ic_std if ic_std > 0 else 0
        win_rate = float((arr > 0).sum()) / len(arr)
        t_stat = ic_mean / (ic_std / np.sqrt(len(arr))) if ic_std > 0 else 0

        # 判断状态
        if abs(ir) > 0.3:
            status = "pass"
        elif abs(ir) > 0.15:
            status = "obs"
        else:
            status = "weak"

        result[name] = {
            "ic_mean": round(ic_mean, 4),
            "ic_std": round(ic_std, 4),
            "ir": round(ir, 4),
            "ic_win_rate": round(win_rate * 100, 1),
            "t_stat": round(t_stat, 2),
            "n_periods": len(ics),
            "status": status,
            "records": records[-30:],  # 最近30条IC记录
        }
    return result


# ============================================================
# 数据库统计（数据中心用）
# ============================================================

def load_data_stats() -> dict:
    """加载各数据表的统计信息"""
    db_path = config.DB_PATH
    stats = {}
    if not os.path.exists(db_path):
        return stats
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        tables = {
            "daily_kline": ("code", "date"),
            "minute_kline": ("code", "datetime"),
            "fundamental": ("code", "report_date"),
            "stock_industry": ("code", None),
        }
        for table, (code_col, date_col) in tables.items():
            try:
                c.execute(f"SELECT COUNT(DISTINCT {code_col}) FROM {table}")
                count = c.fetchone()[0]
                stats[table] = {"codes": count}
                if date_col:
                    c.execute(f"SELECT MAX({date_col}) FROM {table}")
                    stats[table]["latest"] = c.fetchone()[0]
            except Exception:
                stats[table] = {"codes": 0, "latest": None}

        # 换手率统计
        try:
            c.execute("SELECT COUNT(*) FROM daily_kline WHERE turnover IS NOT NULL")
            stats["turnover_rows"] = c.fetchone()[0]
        except Exception:
            stats["turnover_rows"] = 0

        conn.close()
    except Exception as e:
        logger.warning(f"[数据层] 统计查询失败: {e}")
    return stats
