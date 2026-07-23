"""
事件日历模块
============
财报发布、限售股解禁、股权激励、回购增持等事件的风控过滤

核心功能:
  1. 财报发布日历：财报前5天不新开仓（避免业绩雷）
  2. 限售股解禁：解禁前10天不买入（抛压预期）
  3. 股权激励/回购：正面事件加分
  4. 指数成分调整：纳入/剔除效应
  5. 分红除权：除权日前提醒

数据来源:
  - AKShare: 财报披露日期、解禁日历、回购增持
  - 本地缓存: 减少API调用

使用方式:
    from strategy.event_calendar import EventCalendar
    cal = EventCalendar()
    risk = cal.check_event_risk("002415", days_ahead=5)
"""

import pandas as pd
import numpy as np
import logging
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 尝试导入akshare
try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


class EventCalendar:
    """事件日历管理器"""

    def __init__(self, cache_dir: str = None):
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "output", "event_cache"
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        self._earnings_cache = {}
        self._unlock_cache = {}
        self._load_cache()

    def check_event_risk(self, code: str, days_ahead: int = 10) -> dict:
        """
        检查个股近期事件风险
        
        参数:
            code: 股票代码
            days_ahead: 前瞻天数
        
        返回:
            {
                "has_risk": bool,
                "risk_level": "high/medium/low/none",
                "events": [event_dict, ...],
                "block_buy": bool,       # 是否阻止买入
                "suggestion": str,
            }
        """
        events = []
        block_buy = False
        risk_level = "none"

        # 1. 财报风险
        earnings_event = self._check_earnings(code, days_ahead)
        if earnings_event:
            events.append(earnings_event)
            if earnings_event["days_until"] <= 5:
                block_buy = True
                risk_level = "high"

        # 2. 解禁风险
        unlock_event = self._check_unlock(code, days_ahead)
        if unlock_event:
            events.append(unlock_event)
            if unlock_event["days_until"] <= 10:
                block_buy = True
                risk_level = "high" if unlock_event.get("ratio", 0) > 5 else "medium"

        # 3. 正面事件（回购/增持/激励）
        positive_events = self._check_positive_events(code, days_ahead)
        events.extend(positive_events)

        # 4. 分红除权
        dividend_event = self._check_dividend(code, days_ahead)
        if dividend_event:
            events.append(dividend_event)

        # 综合建议
        if block_buy:
            suggestion = f"⚠️ {code}近期有重大事件，建议暂不新开仓"
        elif risk_level == "medium":
            suggestion = f"注意: {code}近期有事件，仓位宜保守"
        elif positive_events:
            suggestion = f"利好: {code}有正面事件催化"
        else:
            suggestion = "无特殊事件"

        return {
            "has_risk": risk_level != "none",
            "risk_level": risk_level,
            "events": events,
            "block_buy": block_buy,
            "suggestion": suggestion,
        }

    def batch_check(self, codes: list, days_ahead: int = 10) -> dict:
        """批量检查事件风险"""
        results = {}
        for code in codes:
            results[code] = self.check_event_risk(code, days_ahead)
        
        blocked = [c for c, r in results.items() if r["block_buy"]]
        if blocked:
            logger.info(f"[事件日历] {len(blocked)}只股票近期有事件风险: {blocked}")
        
        return results

    def get_upcoming_events(self, codes: list, days: int = 30) -> list:
        """获取未来N天内所有事件"""
        all_events = []
        for code in codes:
            result = self.check_event_risk(code, days)
            for event in result["events"]:
                event["code"] = code
                event["name"] = config.get_stock_name(code)
                all_events.append(event)
        
        all_events.sort(key=lambda x: x.get("days_until", 999))
        return all_events

    # ============================================================
    # 事件检测
    # ============================================================

    def _check_earnings(self, code: str, days_ahead: int) -> dict:
        """检查财报发布风险"""
        today = datetime.date.today()
        
        # A股财报披露窗口
        # Q1: 4月30日前, Q2: 8月31日前, Q3: 10月31日前, Q4: 次年4月30日前
        earnings_windows = [
            (datetime.date(today.year, 4, 1), datetime.date(today.year, 4, 30), "年报/Q1"),
            (datetime.date(today.year, 7, 1), datetime.date(today.year, 8, 31), "中报"),
            (datetime.date(today.year, 10, 1), datetime.date(today.year, 10, 31), "Q3"),
        ]
        
        for start, end, report_type in earnings_windows:
            if start <= today <= end:
                # 在财报披露窗口内
                days_until_end = (end - today).days
                return {
                    "type": "earnings",
                    "type_cn": f"财报披露期({report_type})",
                    "date": end.isoformat(),
                    "days_until": min(days_until_end, days_ahead),
                    "impact": "业绩不确定性高，避免新开仓",
                    "severity": "high" if days_until_end <= 15 else "medium",
                }
        
        # 尝试从akshare获取具体披露日期
        if HAS_AKSHARE:
            try:
                specific_date = self._get_earnings_date_akshare(code)
                if specific_date:
                    days_until = (specific_date - today).days
                    if 0 <= days_until <= days_ahead:
                        return {
                            "type": "earnings",
                            "type_cn": "财报披露日",
                            "date": specific_date.isoformat(),
                            "days_until": days_until,
                            "impact": f"财报将于{specific_date}披露，前5天禁止开仓",
                            "severity": "high" if days_until <= 5 else "medium",
                        }
            except Exception:
                pass
        
        return None

    def _check_unlock(self, code: str, days_ahead: int) -> dict:
        """检查限售股解禁"""
        today = datetime.date.today()
        
        if HAS_AKSHARE:
            try:
                # 尝试获取解禁日历
                df = ak.stock_restricted_release_queue_em(symbol=code)
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        unlock_date = pd.to_datetime(row.get("解禁时间", "")).date()
                        days_until = (unlock_date - today).days
                        if 0 <= days_until <= days_ahead:
                            ratio = float(row.get("解禁占总股本比例", 0) or 0)
                            return {
                                "type": "unlock",
                                "type_cn": "限售股解禁",
                                "date": unlock_date.isoformat(),
                                "days_until": days_until,
                                "ratio": ratio,
                                "impact": f"解禁比例{ratio:.1f}%，抛压预期",
                                "severity": "high" if ratio > 5 else "medium",
                            }
            except Exception:
                pass
        
        return None

    def _check_positive_events(self, code: str, days_ahead: int) -> list:
        """检查正面事件（回购/增持/激励）"""
        events = []
        today = datetime.date.today()
        
        if not HAS_AKSHARE:
            return events
        
        try:
            # 回购
            df = ak.stock_repurchase_em()
            if df is not None and not df.empty:
                stock_rows = df[df["代码"] == code]
                for _, row in stock_rows.iterrows():
                    progress = row.get("实施进度", "")
                    if "实施中" in str(progress):
                        events.append({
                            "type": "buyback",
                            "type_cn": "回购实施中",
                            "date": today.isoformat(),
                            "days_until": 0,
                            "impact": "公司回购中，正面信号",
                            "severity": "positive",
                        })
                        break
        except Exception:
            pass
        
        return events

    def _check_dividend(self, code: str, days_ahead: int) -> dict:
        """检查分红除权"""
        # 简化：每年6-7月为分红高峰期
        today = datetime.date.today()
        if today.month in (6, 7):
            return {
                "type": "dividend_season",
                "type_cn": "分红除权季",
                "date": "",
                "days_until": 0,
                "impact": "分红除权季，注意除权日对技术信号的影响",
                "severity": "low",
            }
        return None

    def _get_earnings_date_akshare(self, code: str):
        """从akshare获取具体财报披露日期"""
        try:
            df = ak.stock_report_disclosure(symbol=code)
            if df is not None and not df.empty:
                # 取最近一次未披露的
                today = datetime.date.today()
                for _, row in df.iterrows():
                    date_str = str(row.get("预计披露时间", ""))
                    try:
                        disc_date = datetime.date.fromisoformat(date_str[:10])
                        if disc_date >= today:
                            return disc_date
                    except (ValueError, TypeError):
                        continue
        except Exception:
            pass
        return None

    # ============================================================
    # 缓存管理
    # ============================================================

    def _load_cache(self):
        """加载本地缓存"""
        cache_file = os.path.join(self.cache_dir, "event_cache.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._earnings_cache = data.get("earnings", {})
                    self._unlock_cache = data.get("unlock", {})
            except Exception:
                pass

    def save_cache(self):
        """保存缓存"""
        cache_file = os.path.join(self.cache_dir, "event_cache.json")
        data = {
            "earnings": self._earnings_cache,
            "unlock": self._unlock_cache,
            "updated": datetime.datetime.now().isoformat(),
        }
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
