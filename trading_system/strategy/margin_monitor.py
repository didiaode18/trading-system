"""
融资融券监控模块 V1.0
====================
监控个股及市场融资融券数据，分析杠杆资金趋势，提供做空预警与轧空风险提示。

核心功能:
  1. 个股融资融券历史数据获取（支持上交所 / 深交所）
  2. 融资净买入连续天数 & 余额趋势分析
  3. 融券异常增加预警（做空信号）
  4. 融资余额拐点检测（杠杆资金入场信号）
  5. 轧空风险检测（融券高 + 股价上涨）
  6. 融资融券因子评分（供 multi_factor 集成）

数据来源: akshare（上交所 / 深交所官方披露）

使用方式:
    from strategy.margin_monitor import MarginMonitor
    mm = MarginMonitor()
    signal = mm.calc_margin_signal("000001", days=30)
    factor = mm.get_margin_factor("000001")
"""

import os
import sys
import json
import logging
import datetime
from typing import Optional

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
    logger.warning("akshare 未安装，融资融券数据获取不可用")

# ---------------------------------------------------------------------------
# 缓存路径
# ---------------------------------------------------------------------------
CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
MARGIN_CACHE_FILE = os.path.join(CACHE_DIR, "margin_monitor_cache.json")

# ---------------------------------------------------------------------------
# 模块内默认参数（不修改 config.py）
# ---------------------------------------------------------------------------
CFG = getattr(config, "CFG", {}) if isinstance(getattr(config, "CFG", None), dict) else {}

DEFAULT_ANALYSIS_DAYS = CFG.get("margin_default_days", 30)
SHORT_ANOMALY_THRESHOLD = CFG.get("margin_short_anomaly_threshold", 0.5)  # 50%
SHORT_SQUEEZE_PRICE_PCT = CFG.get("margin_short_squeeze_price_pct", 0.10)  # 10%
NET_BUY_MIN_DAYS = CFG.get("margin_net_buy_min_days", 3)  # 连续净买入最少天数
CACHE_TTL_SECONDS = CFG.get("margin_cache_ttl_seconds", 3600 * 6)  # 6小时缓存


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    """加载本地 JSON 缓存文件"""
    if not os.path.exists(MARGIN_CACHE_FILE):
        return {}
    try:
        with open(MARGIN_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"融资融券缓存读取失败: {e}")
        return {}


def _save_cache(cache: dict) -> None:
    """将缓存写回本地 JSON 文件"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(MARGIN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug(f"融资融券缓存写入失败: {e}")


def _is_cache_fresh(cache: dict, key: str) -> bool:
    """检查缓存条目是否在 TTL 内"""
    ts = cache.get(key, {}).get("timestamp")
    if ts is None:
        return False
    return (datetime.datetime.now().timestamp() - ts) < CACHE_TTL_SECONDS


def _normalize_stock_code(code: str) -> str:
    """去除前缀，保留6位数字代码"""
    code = str(code).strip()
    for prefix in ("sh", "sz", "SH", "SZ", "bj", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix):]
    return code.zfill(6)


def _get_exchange(code: str) -> str:
    """根据代码判断所属交易所: 'sse' / 'szse'"""
    code = _normalize_stock_code(code)
    if code.startswith("6") or code.startswith("5"):
        return "sse"
    return "szse"


# ---------------------------------------------------------------------------
# MarginMonitor 核心类
# ---------------------------------------------------------------------------

class MarginMonitor:
    """融资融券数据监控器"""

    def __init__(self):
        if not HAS_AKSHARE:
            logger.error("akshare 不可用，MarginMonitor 功能受限")

    # ------------------------------------------------------------------
    # 内部：获取单日全市场融资融券明细（带缓存）
    # ------------------------------------------------------------------
    def _fetch_daily_detail(self, date_str: str) -> Optional[pd.DataFrame]:
        """
        获取指定日期全市场融资融券明细（合并上交所+深交所）

        :param date_str: YYYYMMDD 格式日期字符串
        :return: 统一列名后的 DataFrame，失败返回 None
        """
        cache = _load_cache()
        cache_key = f"daily_{date_str}"

        if _is_cache_fresh(cache, cache_key):
            try:
                return pd.DataFrame(cache[cache_key]["data"])
            except Exception:
                pass

        frames = []

        # 深交所
        try:
            df_sz = ak.stock_margin_detail_szse(date=date_str)
            if df_sz is not None and not df_sz.empty:
                df_sz = df_sz.rename(columns={
                    "证券代码": "code",
                    "证券简称": "name",
                    "融资买入额": "margin_buy_amount",
                    "融资余额": "margin_balance",
                    "融券卖出量": "short_sell_volume",
                    "融券余量": "short_volume",
                    "融券余额": "short_balance",
                    "融资融券余额": "total_balance",
                })
                df_sz["exchange"] = "szse"
                df_sz["date"] = date_str
                frames.append(df_sz)
        except Exception as e:
            logger.debug(f"深交所融资融券明细获取失败({date_str}): {e}")

        # 上交所
        try:
            df_sh = ak.stock_margin_detail_sse(date=date_str)
            if df_sh is not None and not df_sh.empty:
                df_sh = df_sh.rename(columns={
                    "标的证券代码": "code",
                    "标的证券简称": "name",
                    "融资余额": "margin_balance",
                    "融资买入额": "margin_buy_amount",
                    "融资偿还额": "margin_repay_amount",
                    "融券余量": "short_volume",
                    "融券卖出量": "short_sell_volume",
                    "融券偿还量": "short_repay_volume",
                })
                df_sh["exchange"] = "sse"
                df_sh["date"] = date_str
                frames.append(df_sh)
        except Exception as e:
            logger.debug(f"上交所融资融券明细获取失败({date_str}): {e}")

        if not frames:
            logger.warning(f"融资融券: {date_str} 两所数据均获取失败")
            return None

        combined = pd.concat(frames, ignore_index=True)
        # 统一数值列，防止字符串类型
        numeric_cols = [
            "margin_buy_amount", "margin_balance", "margin_repay_amount",
            "short_sell_volume", "short_volume", "short_balance",
            "total_balance", "short_repay_volume",
        ]
        for col in numeric_cols:
            if col in combined.columns:
                combined[col] = pd.to_numeric(combined[col], errors="coerce").fillna(0)

        # 上交所无 short_balance，用 short_volume * 估算收盘价 近似替代（无精确价格时用0）
        if "short_balance" not in combined.columns:
            combined["short_balance"] = 0.0

        # 写缓存
        try:
            cache[cache_key] = {
                "timestamp": datetime.datetime.now().timestamp(),
                "data": combined.to_dict(orient="records"),
            }
            _save_cache(cache)
        except Exception:
            pass

        return combined

    # ------------------------------------------------------------------
    # 内部：获取指定交易日列表
    # ------------------------------------------------------------------
    @staticmethod
    def _trading_dates(n: int, end_date: Optional[datetime.date] = None) -> list:
        """
        生成最近 n 个自然日对应的日期字符串列表（倒序，最新在前）。
        实际交易日过滤在数据获取时自动完成（非交易日 API 返回空）。
        """
        if end_date is None:
            end_date = datetime.date.today()
        dates = []
        for i in range(n * 2):  # 扩大窗口以覆盖非交易日
            d = end_date - datetime.timedelta(days=i)
            if d.weekday() < 5:  # 排除周末
                dates.append(d.strftime("%Y%m%d"))
            if len(dates) >= n:
                break
        return dates

    # ------------------------------------------------------------------
    # 公开接口 1: 获取个股融资融券历史数据
    # ------------------------------------------------------------------
    def get_margin_data(self, stock_code: str, days: int = 30) -> pd.DataFrame:
        """
        获取个股融资融券历史数据

        :param stock_code: 6位股票代码（如 '000001', '600519'）
        :param days: 获取最近多少个交易日的数据
        :return: DataFrame，包含 date, margin_balance, margin_buy_amount,
                 short_volume, short_sell_volume, short_balance 等列；
                 若获取失败返回空 DataFrame
        """
        stock_code = _normalize_stock_code(stock_code)
        date_list = self._trading_dates(days)

        rows = []
        for date_str in date_list:
            df = self._fetch_daily_detail(date_str)
            if df is None or df.empty:
                continue
            stock_rows = df[df["code"] == stock_code]
            if stock_rows.empty:
                continue
            row = stock_rows.iloc[0].to_dict()
            row["date"] = date_str
            rows.append(row)

        if not rows:
            logger.info(f"融资融券: {stock_code} 最近{days}天无数据")
            return pd.DataFrame()

        result = pd.DataFrame(rows)
        # 按日期升序排列（最旧在前）
        result = result.sort_values("date").reset_index(drop=True)
        logger.info(f"融资融券: {stock_code} 获取到{len(result)}天数据")
        return result

    # ------------------------------------------------------------------
    # 公开接口 2: 计算融资融券信号
    # ------------------------------------------------------------------
    def calc_margin_signal(self, stock_code: str, days: int = 30) -> dict:
        """
        计算融资融券综合信号

        :param stock_code: 6位股票代码
        :param days: 分析天数
        :return: 信号字典
        """
        stock_code = _normalize_stock_code(stock_code)
        default_result = {
            "net_buy_days": 0,
            "balance_trend": "neutral",
            "balance_turning": False,
            "short_selling_anomaly": False,
            "signal": "neutral",
            "confidence": 0.0,
            "description": "数据不足，无法判定",
        }

        df = self.get_margin_data(stock_code, days=days)
        if df.empty or len(df) < 5:
            return default_result

        # --- 融资净买入连续天数 ---
        # 净买入 = 当日融资余额 - 前日融资余额（余额增加代表净买入）
        if "margin_balance" in df.columns and len(df) >= 2:
            df["net_buy"] = df["margin_balance"].diff()
        else:
            df["net_buy"] = 0

        # 从最新一天向前数连续净买入天数
        net_buy_days = 0
        for val in df["net_buy"].iloc[::-1]:
            if val > 0:
                net_buy_days += 1
            else:
                break

        # --- 余额趋势 ---
        recent = df["margin_balance"].tail(10)
        if len(recent) >= 5:
            first_half = recent.iloc[: len(recent) // 2].mean()
            second_half = recent.iloc[len(recent) // 2 :].mean()
            if second_half > first_half * 1.02:
                balance_trend = "increasing"
            elif second_half < first_half * 0.98:
                balance_trend = "decreasing"
            else:
                balance_trend = "neutral"
        else:
            balance_trend = "neutral"

        # --- 余额拐点（从降转升）---
        balance_turning = False
        if len(df) >= 7:
            # 前段连续下降 + 最近3天回升
            prev_segment = df["margin_balance"].iloc[-7:-3]
            last_segment = df["margin_balance"].iloc[-3:]
            prev_declining = all(
                prev_segment.iloc[i] >= prev_segment.iloc[i + 1]
                for i in range(len(prev_segment) - 1)
            ) if len(prev_segment) >= 2 else False
            last_rising = all(
                last_segment.iloc[i] <= last_segment.iloc[i + 1]
                for i in range(len(last_segment) - 1)
            ) if len(last_segment) >= 2 else False
            balance_turning = prev_declining and last_rising

        # --- 融券异常增加 ---
        short_selling_anomaly = False
        short_anomaly_pct = 0.0
        if "short_balance" in df.columns and len(df) >= 20:
            short_ma20 = df["short_balance"].tail(20).mean()
            short_recent5 = df["short_balance"].tail(5).mean()
            if short_ma20 > 0:
                short_anomaly_pct = (short_recent5 - short_ma20) / short_ma20
                if short_anomaly_pct > SHORT_ANOMALY_THRESHOLD:
                    short_selling_anomaly = True
                    logger.warning(
                        f"融券预警: {stock_code} 融券余额异常增加{short_anomaly_pct:.1%}"
                    )

        # --- 综合信号判定 ---
        signal = "neutral"
        confidence = 0.3
        desc_parts = []

        # P1优化: 融资余额与价格背离检测
        price_margin_divergence = "none"
        try:
            price_pct_5d = self._get_price_change_pct(stock_code, days=5)
            if len(df) >= 5:
                mb_start = df["margin_balance"].iloc[-5]
                mb_end = df["margin_balance"].iloc[-1]
                margin_chg_pct = (mb_end - mb_start) / mb_start if mb_start > 0 else 0
                # 顶背离: 价格上涨但融资余额下降
                if price_pct_5d > 0.02 and margin_chg_pct < -0.01:
                    price_margin_divergence = "top"
                    desc_parts.append(f"⚠️融资余额与价格顶背离(价格+{price_pct_5d:.1%}但融资{margin_chg_pct:.1%})")
                # 底背离: 价格下跌但融资余额上升
                elif price_pct_5d < -0.02 and margin_chg_pct > 0.01:
                    price_margin_divergence = "bottom"
                    desc_parts.append(f"融资余额与价格底背离(价格{price_pct_5d:.1%}但融资+{margin_chg_pct:.1%}，杠杆资金逆势布局)")
        except Exception:
            pass

        # P1优化: 融资余额急降阈值预警
        balance_drop_alert = False
        if len(df) >= 5:
            mb_5d_ago = df["margin_balance"].iloc[-5]
            mb_now = df["margin_balance"].iloc[-1]
            if mb_5d_ago > 0:
                drop_pct = (mb_5d_ago - mb_now) / mb_5d_ago
                if drop_pct > 0.05:  # 5日融资余额降幅超5%
                    balance_drop_alert = True
                    desc_parts.append(f"⚠️融资余额5日急降{drop_pct:.1%}，杠杆资金撤离")

        if net_buy_days >= NET_BUY_MIN_DAYS and balance_trend == "increasing":
            signal = "bullish"
            confidence = min(0.9, 0.5 + net_buy_days * 0.05)
            desc_parts.append(f"融资净买入连续{net_buy_days}天，余额趋势向上")

        if balance_turning:
            if signal == "neutral":
                signal = "bullish"
                confidence = max(confidence, 0.55)
            desc_parts.append("融资余额出现拐点（从降转升，杠杆资金入场）")

        if short_selling_anomaly:
            signal = "bearish"
            confidence = max(confidence, min(0.85, 0.5 + short_anomaly_pct * 0.3))
            desc_parts.append(f"融券余额异常增加{short_anomaly_pct:.1%}（做空预警）")

        # 背离和急降也影响信号
        if price_margin_divergence == "top" or balance_drop_alert:
            if signal != "bearish":
                signal = "cautious"
            confidence = max(confidence, 0.6)
        elif price_margin_divergence == "bottom":
            if signal == "neutral":
                signal = "bullish"
            confidence = max(confidence, 0.55)

        if not desc_parts:
            desc_parts.append("融资融券数据无明显信号")

        description = "；".join(desc_parts)

        logger.info(
            f"融资融券: {stock_code} 融资净买入{net_buy_days}天, "
            f"余额趋势{balance_trend}, 信号={signal}({confidence:.2f})"
        )

        return {
            "net_buy_days": net_buy_days,
            "balance_trend": balance_trend,
            "balance_turning": balance_turning,
            "short_selling_anomaly": short_selling_anomaly,
            "price_margin_divergence": price_margin_divergence,
            "balance_drop_alert": balance_drop_alert,
            "signal": signal,
            "confidence": round(confidence, 3),
            "description": description,
        }

    # ------------------------------------------------------------------
    # 公开接口 3: 轧空风险检测
    # ------------------------------------------------------------------
    def detect_short_squeeze_risk(self, stock_code: str) -> dict:
        """
        检测轧空风险（融券余额高 + 股价上涨）

        :param stock_code: 6位股票代码
        :return: 风险字典
        """
        stock_code = _normalize_stock_code(stock_code)
        default_result = {
            "risk_level": "unknown",
            "short_balance_high": False,
            "price_rising": False,
            "description": "数据不足",
        }

        df = self.get_margin_data(stock_code, days=30)
        if df.empty or len(df) < 10:
            return default_result

        # 融券余额是否偏高（相对自身20日均值）
        short_balance_high = False
        if "short_balance" in df.columns and len(df) >= 20:
            short_ma20 = df["short_balance"].tail(20).mean()
            short_latest = df["short_balance"].iloc[-1]
            if short_ma20 > 0 and short_latest > short_ma20 * 1.5:
                short_balance_high = True

        # 股价近5天涨幅（使用融资余额变化近似，若有价格数据更佳）
        # 尝试从 baostock 获取真实价格
        price_rising = False
        price_pct = 0.0
        try:
            price_pct = self._get_price_change_pct(stock_code, days=5)
            if price_pct > SHORT_SQUEEZE_PRICE_PCT:
                price_rising = True
        except Exception as e:
            logger.debug(f"股价获取失败({stock_code}): {e}")
            # 降级：用融资余额变化近似
            if len(df) >= 5:
                mb_start = df["margin_balance"].iloc[-6] if len(df) >= 6 else df["margin_balance"].iloc[0]
                mb_end = df["margin_balance"].iloc[-1]
                if mb_start > 0:
                    price_pct = (mb_end - mb_start) / mb_start
                    if price_pct > SHORT_SQUEEZE_PRICE_PCT:
                        price_rising = True

        risk_level = "low"
        desc_parts = []
        if short_balance_high and price_rising:
            risk_level = "high"
            desc_parts.append(
                f"融券余额偏高 + 近5天涨幅{price_pct:.1%}，存在轧空风险"
            )
            logger.warning(
                f"轧空预警: {stock_code} 融券余额高+股价涨{price_pct:.1%}"
            )
        elif short_balance_high:
            risk_level = "medium"
            desc_parts.append("融券余额偏高，需关注股价走势")
        elif price_rising:
            risk_level = "low"
            desc_parts.append(f"股价近5天涨{price_pct:.1%}，融券余额正常")
        else:
            desc_parts.append("无轧空风险")

        return {
            "risk_level": risk_level,
            "short_balance_high": short_balance_high,
            "price_rising": price_rising,
            "price_change_pct": round(price_pct, 4),
            "description": "；".join(desc_parts),
        }

    # ------------------------------------------------------------------
    # 公开接口 4: 批量分析
    # ------------------------------------------------------------------
    def batch_analyze(self, stock_codes: list) -> dict:
        """
        批量分析多只股票的融资融券信号

        :param stock_codes: 股票代码列表
        :return: {stock_code: signal_dict} 字典
        """
        results = {}
        total = len(stock_codes)
        for idx, code in enumerate(stock_codes, 1):
            code = _normalize_stock_code(code)
            try:
                logger.info(f"批量分析进度: {idx}/{total} - {code}")
                signal = self.calc_margin_signal(code)
                squeeze = self.detect_short_squeeze_risk(code)
                results[code] = {
                    "signal": signal,
                    "short_squeeze": squeeze,
                    "factor": self._signal_to_factor(signal, squeeze),
                }
            except Exception as e:
                logger.error(f"批量分析失败({code}): {e}")
                results[code] = {
                    "signal": {"signal": "neutral", "confidence": 0, "description": f"分析失败: {e}"},
                    "short_squeeze": {"risk_level": "unknown"},
                    "factor": 50.0,
                }
        return results

    # ------------------------------------------------------------------
    # 公开接口 5: 融资融券因子评分
    # ------------------------------------------------------------------
    def get_margin_factor(self, stock_code: str) -> float:
        """
        返回 0-100 的融资融券因子分数，供 multi_factor 集成

        评分逻辑:
          - 融资连续净买入 → 高分（看多确认）
          - 融券异常增加 → 低分（做空预警）
          - 余额拐点 → 加分
          - 无数据 → 50（中性）

        :param stock_code: 6位股票代码
        :return: 0-100 的因子分数
        """
        stock_code = _normalize_stock_code(stock_code)
        try:
            signal = self.calc_margin_signal(stock_code)
            squeeze = self.detect_short_squeeze_risk(stock_code)
            return self._signal_to_factor(signal, squeeze)
        except Exception as e:
            logger.error(f"融资融券因子计算失败({stock_code}): {e}")
            return 50.0

    # ------------------------------------------------------------------
    # 内部：信号 → 因子分数转换
    # ------------------------------------------------------------------
    @staticmethod
    def _signal_to_factor(signal: dict, squeeze: dict) -> float:
        """将信号字典转换为 0-100 因子分数"""
        base_score = 50.0

        # 融资净买入加分
        net_buy_days = signal.get("net_buy_days", 0)
        if net_buy_days >= NET_BUY_MIN_DAYS:
            base_score += min(25, net_buy_days * 4)

        # 余额趋势调整
        trend = signal.get("balance_trend", "neutral")
        if trend == "increasing":
            base_score += 10
        elif trend == "decreasing":
            base_score -= 10

        # 余额拐点加分
        if signal.get("balance_turning"):
            base_score += 8

        # 融券异常减分
        if signal.get("short_selling_anomaly"):
            base_score -= 20

        # 轧空风险（高风险时略减分，因为不确定性大）
        risk_level = squeeze.get("risk_level", "low")
        if risk_level == "high":
            base_score -= 5

        # 置信度加权
        confidence = signal.get("confidence", 0.3)
        # 高置信度让分数更极端，低置信度向50靠拢
        base_score = 50 + (base_score - 50) * max(0.3, confidence)

        return round(max(0.0, min(100.0, base_score)), 2)

    # ------------------------------------------------------------------
    # 内部：获取股价涨跌幅
    # ------------------------------------------------------------------
    @staticmethod
    def _get_price_change_pct(stock_code: str, days: int = 5) -> float:
        """
        通过 akshare 获取个股近 N 天涨跌幅

        :param stock_code: 6位股票代码
        :param days: 天数
        :return: 涨跌幅（如 0.12 代表 12%）
        """
        try:
            import akshare as ak

            end_date = datetime.date.today().strftime("%Y%m%d")
            start_date = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y%m%d")

            # 使用东方财富日K线接口
            df = ak.stock_zh_a_hist(
                symbol=stock_code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            )
            if df is None or df.empty or len(df) < 2:
                return 0.0

            close_col = "收盘" if "收盘" in df.columns else "close"
            recent = df[close_col].tail(days + 1)
            if len(recent) < 2:
                return 0.0

            start_price = recent.iloc[0]
            end_price = recent.iloc[-1]
            if start_price <= 0:
                return 0.0
            return (end_price - start_price) / start_price

        except Exception as e:
            logger.debug(f"股价获取失败({stock_code}): {e}")
            return 0.0
