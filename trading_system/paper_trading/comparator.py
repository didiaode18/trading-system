# -*- coding: utf-8 -*-
"""
模拟盘 vs 回测偏差分析（comparator）
====================================
对比同一策略在相同日期区间的模拟盘实际表现与回测预期，输出：
  - 净值偏差（日偏差绝对值均值/最大值）
  - 信号一致率、漏信号/多信号清单
  - 成交滑点偏差
偏差超阈值在报告中标红，供毕业判定消费（验证实盘可复现性）。

口径：净值日偏差 = (paper_nav - backtest_nav) / backtest_nav，取绝对值。
安全约束：只读比对，异常静默降级返回 {"ok": False}。
"""

import logging

logger = logging.getLogger(__name__)

DEFAULT_NAV_DEVIATION = 0.02  # 净值日偏差绝对值均值阈值 2%


def _to_dict(series) -> dict:
    """把 pd.Series / dict 统一成 {date: value(float)}。"""
    out = {}
    try:
        if series is None:
            return out
        if isinstance(series, dict):
            items = series.items()
        else:  # pandas.Series
            items = series.to_dict().items()
        for k, v in items:
            try:
                out[str(k)] = float(v)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _signal_keys(signals) -> set:
    """把信号列表归一为 {(date, code, direction)} 集合。

    支持元素为 dict（含 date/code/direction|action）或三元组。
    """
    keys = set()
    try:
        for s in (signals or []):
            if isinstance(s, dict):
                d = str(s.get("date", ""))
                c = str(s.get("code", ""))
                dirn = str(s.get("direction", s.get("action", "")))
                keys.add((d, c, dirn))
            elif isinstance(s, (tuple, list)) and len(s) >= 3:
                keys.add((str(s[0]), str(s[1]), str(s[2])))
    except Exception:
        pass
    return keys


class PaperComparator:
    """模拟盘与回测偏差比对器。"""

    def __init__(self, config: dict = None):
        cfg = config
        if cfg is None:
            try:
                import config as _c
                cfg = getattr(_c, "PAPER_TRADING_CONFIG", {}) or {}
            except Exception:
                cfg = {}
        self.nav_threshold = float(cfg.get("nav_deviation_threshold", DEFAULT_NAV_DEVIATION))

    def compare(self, paper_nav, backtest_nav, paper_signals=None,
                backtest_signals=None, paper_fills=None, backtest_fills=None) -> dict:
        """比对模拟盘与回测。

        参数:
            paper_nav / backtest_nav: {date: nav} 或 pd.Series
            paper_signals / backtest_signals: 信号列表（dict 或 (date,code,direction)）
            paper_fills / backtest_fills: 成交列表（dict 含 date/code/direction/price）

        返回 dict:
            ok, common_days, nav_deviation_mean, nav_deviation_max,
            nav_deviation_series, signal_consistency, missing_signals,
            extra_signals, slippage_deviation, has_deviation
        """
        res = {"ok": False, "common_days": 0, "nav_deviation_mean": None,
               "nav_deviation_max": None, "nav_deviation_series": {},
               "signal_consistency": None, "missing_signals": [],
               "extra_signals": [], "slippage_deviation": None,
               "has_deviation": False}
        try:
            p = _to_dict(paper_nav)
            b = _to_dict(backtest_nav)
            common = sorted(set(p) & set(b))
            res["common_days"] = len(common)

            # 1. 净值偏差
            devs = {}
            for d in common:
                bv = b[d]
                if bv and bv != 0:
                    devs[d] = (p[d] - bv) / bv
                else:
                    devs[d] = 0.0
            res["nav_deviation_series"] = {d: round(v, 6) for d, v in devs.items()}
            if devs:
                abs_devs = [abs(v) for v in devs.values()]
                res["nav_deviation_mean"] = round(sum(abs_devs) / len(abs_devs), 6)
                res["nav_deviation_max"] = round(max(abs_devs), 6)
                res["has_deviation"] = res["nav_deviation_mean"] > self.nav_threshold

            # 2. 信号一致率
            if paper_signals is not None or backtest_signals is not None:
                pk = _signal_keys(paper_signals)
                bk = _signal_keys(backtest_signals)
                union = pk | bk
                res["missing_signals"] = sorted(bk - pk)  # 回测有、模拟盘漏
                res["extra_signals"] = sorted(pk - bk)    # 模拟盘多、回测无
                if union:
                    res["signal_consistency"] = round(len(pk & bk) / len(union), 4)
                else:
                    res["signal_consistency"] = 1.0

            # 3. 成交滑点偏差（匹配 date/code/direction 的均价差）
            if paper_fills and backtest_fills:
                res["slippage_deviation"] = self._slippage_deviation(
                    paper_fills, backtest_fills)

            res["ok"] = True
        except Exception as e:
            logger.warning(f"[模拟盘偏差] 比对异常(降级): {e}")
            res["error"] = str(e)
        return res

    @staticmethod
    def _slippage_deviation(paper_fills, backtest_fills):
        """匹配成交的平均价格相对偏差（绝对值均值）。"""
        try:
            def _index(fills):
                idx = {}
                for f in fills:
                    if isinstance(f, dict):
                        key = (str(f.get("date", "")), str(f.get("code", "")),
                               str(f.get("direction", f.get("action", ""))))
                        price = float(f.get("price", 0) or 0)
                    else:
                        key = (str(getattr(f, "date", "")), str(getattr(f, "code", "")),
                               str(getattr(f, "direction", "")))
                        price = float(getattr(f, "price", 0) or 0)
                    idx.setdefault(key, []).append(price)
                return idx

            pi = _index(paper_fills)
            bi = _index(backtest_fills)
            devs = []
            for key, prices in pi.items():
                if key in bi:
                    p_avg = sum(prices) / len(prices)
                    b_prices = bi[key]
                    b_avg = sum(b_prices) / len(b_prices)
                    if b_avg:
                        devs.append(abs((p_avg - b_avg) / b_avg))
            if devs:
                return round(sum(devs) / len(devs), 6)
        except Exception as e:
            logger.debug(f"[模拟盘偏差] 滑点偏差计算异常: {e}")
        return None
