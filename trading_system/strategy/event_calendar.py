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
    # 限售解禁预警（扩展）
    # ============================================================

    # 冲击评级阈值（模块内常量，不修改 config.py）
    _RELEASE_HIGH_THRESHOLD = 10.0   # 解禁市值/流通市值 > 10% → 高冲击
    _RELEASE_MEDIUM_THRESHOLD = 5.0  # 5% - 10% → 中冲击
    _RELEASE_NEAR_DAYS = 7           # 7天内解禁 → 额外风险加分

    def get_restricted_release_calendar(self, days_ahead: int = 90) -> pd.DataFrame:
        """获取未来N天的限售解禁日历

        数据来源优先级:
          1. akshare stock_restricted_release_summary_em
          2. 优雅降级返回空 DataFrame

        返回 DataFrame 列: date, stock_code, stock_name, release_amount,
                           release_market_value_ratio, release_type
        """
        columns = ["date", "stock_code", "stock_name",
                    "release_amount", "release_market_value_ratio", "release_type"]
        empty_df = pd.DataFrame(columns=columns)
        if not HAS_AKSHARE:
            logger.warning("[解禁日历] akshare 不可用，返回空日历")
            return empty_df

        today = datetime.date.today()
        end_date = today + datetime.timedelta(days=days_ahead)

        # --- 方案1: akshare 限售解禁摘要接口 ---
        try:
            df = ak.stock_restricted_release_summary_em(symbol="全部")
            if df is not None and not df.empty:
                return self._parse_release_df(df, today, end_date, columns)
        except Exception as e:
            logger.warning(f"[解禁日历] stock_restricted_release_summary_em 失败: {e}")

        # --- 方案2: 尝试逐个获取（仅作为兜底，不遍历全市场） ---
        logger.warning("[解禁日历] 所有数据源均不可用，返回空日历")
        return empty_df

    def _parse_release_df(self, df: pd.DataFrame, start: datetime.date,
                          end: datetime.date, columns: list) -> pd.DataFrame:
        """解析 akshare 解禁 DataFrame 为统一格式"""
        try:
            # akshare 返回列名可能为中文，做映射
            col_map = {}
            for c in df.columns:
                cl = str(c)
                if "日期" in cl or "时间" in cl:
                    col_map[c] = "date"
                elif "代码" in cl:
                    col_map[c] = "stock_code"
                elif "名称" in cl or "简称" in cl:
                    col_map[c] = "stock_name"
                elif "数量" in cl or "解禁" in cl and "股" in cl:
                    col_map[c] = "release_amount"
                elif "占比" in cl or "比例" in cl:
                    col_map[c] = "release_market_value_ratio"
                elif "类型" in cl or "批次" in cl:
                    col_map[c] = "release_type"
            df = df.rename(columns=col_map)

            if "date" not in df.columns:
                logger.warning("[解禁日历] 无法识别日期列")
                return pd.DataFrame(columns=columns)

            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"])
            df = df[df["date"].dt.date >= start]
            df = df[df["date"].dt.date <= end]
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")

            for col in columns:
                if col not in df.columns:
                    df[col] = ""
            df = df[columns].reset_index(drop=True)
            return df
        except Exception as e:
            logger.warning(f"[解禁日历] 解析数据失败: {e}")
            return pd.DataFrame(columns=columns)

    def assess_release_impact(self, stock_code: str, release_data: dict) -> dict:
        """评估解禁冲击

        参数:
            stock_code: 股票代码
            release_data: 包含 release_date, release_market_value_ratio 等字段

        冲击评级:
            - 高冲击: 解禁市值/流通市值 > 10%
            - 中冲击: 5% - 10%
            - 低冲击: < 5%
            - 7天内解禁 → 额外风险加分
        """
        today = datetime.date.today()
        release_date_str = str(release_data.get("release_date", ""))
        try:
            release_date = datetime.date.fromisoformat(release_date_str[:10])
        except (ValueError, TypeError):
            release_date = today
        days_until = max((release_date - today).days, 0)

        ratio = float(release_data.get("release_market_value_ratio", 0) or 0)

        # 基础冲击评级
        if ratio > self._RELEASE_HIGH_THRESHOLD:
            impact_level = "high"
        elif ratio >= self._RELEASE_MEDIUM_THRESHOLD:
            impact_level = "medium"
        else:
            impact_level = "low"

        # 7天内解禁 → 风险升级
        risk_warning = False
        if days_until <= self._RELEASE_NEAR_DAYS:
            risk_warning = True
            if impact_level == "medium":
                impact_level = "high"
            elif impact_level == "low":
                impact_level = "medium"

        # 描述
        desc_map = {
            "high": f"⚠️ {stock_code} 解禁占比{ratio:.1f}%，高冲击"
                    f"{'，7天内解禁' if risk_warning else ''}，建议回避",
            "medium": f"⚡ {stock_code} 解禁占比{ratio:.1f}%，中冲击"
                      f"{'，7天内解禁' if risk_warning else ''}，谨慎",
            "low": f"{stock_code} 解禁占比{ratio:.1f}%，低冲击，可忽略",
        }

        return {
            "stock_code": stock_code,
            "release_date": release_date_str,
            "impact_level": impact_level,
            "release_ratio": ratio,
            "days_until_release": days_until,
            "risk_warning": risk_warning,
            "description": desc_map.get(impact_level, ""),
        }

    def check_stock_release_risk(self, stock_code: str) -> dict:
        """检查单只股票的解禁风险（未来90天）

        返回:
            {
                "stock_code": str,
                "has_release": bool,
                "events": [assess_result, ...],
                "max_impact": "high"/"medium"/"low"/"none",
                "block_buy": bool,
            }
        """
        cal_df = self.get_restricted_release_calendar(days_ahead=90)
        events = []
        max_impact = "none"
        block_buy = False
        impact_order = {"high": 3, "medium": 2, "low": 1, "none": 0}

        if not cal_df.empty and "stock_code" in cal_df.columns:
            stock_rows = cal_df[cal_df["stock_code"] == stock_code]
            for _, row in stock_rows.iterrows():
                release_data = row.to_dict()
                result = self.assess_release_impact(stock_code, release_data)
                events.append(result)
                if impact_order.get(result["impact_level"], 0) > impact_order.get(max_impact, 0):
                    max_impact = result["impact_level"]
                if result["impact_level"] == "high" and result["days_until_release"] <= 30:
                    block_buy = True

        return {
            "stock_code": stock_code,
            "has_release": len(events) > 0,
            "events": events,
            "max_impact": max_impact,
            "block_buy": block_buy,
        }

    def filter_restricted_stocks(self, stock_codes: list, days: int = 30,
                                 min_impact: str = "high") -> dict:
        """过滤掉即将解禁的股票（用于选股排雷）

        参数:
            stock_codes: 待筛选股票代码列表
            days: 前瞻天数（默认30天）
            min_impact: 最低排除冲击等级，"high" 或 "medium"

        返回:
            {
                "filtered_codes": list,   # 过滤后的股票列表
                "excluded": [             # 被排除的股票及原因
                    {"code": str, "reason": str}, ...
                ],
            }
        """
        cal_df = self.get_restricted_release_calendar(days_ahead=days)
        excluded = []
        filtered = []
        impact_threshold = {"high": self._RELEASE_HIGH_THRESHOLD,
                            "medium": self._RELEASE_MEDIUM_THRESHOLD}
        threshold = impact_threshold.get(min_impact, self._RELEASE_HIGH_THRESHOLD)

        for code in stock_codes:
            is_excluded = False
            if not cal_df.empty and "stock_code" in cal_df.columns:
                stock_rows = cal_df[cal_df["stock_code"] == code]
                for _, row in stock_rows.iterrows():
                    release_data = row.to_dict()
                    result = self.assess_release_impact(code, release_data)
                    ratio = result["release_ratio"]
                    if ratio >= threshold:
                        excluded.append({
                            "code": code,
                            "reason": result["description"],
                        })
                        is_excluded = True
                        break
            if not is_excluded:
                filtered.append(code)

        if excluded:
            logger.info(f"[解禁过滤] 排除{len(excluded)}只: "
                        f"{[e['code'] for e in excluded]}")

        return {
            "filtered_codes": filtered,
            "excluded": excluded,
        }

    def get_release_summary(self, days_ahead: int = 30) -> dict:
        """解禁日历摘要（供日报使用）

        返回:
            {
                "total_stocks": int,
                "high_impact": [...],
                "weekly_breakdown": {...},
                "peak_week": str,
            }
        """
        cal_df = self.get_restricted_release_calendar(days_ahead=days_ahead)
        summary = {
            "total_stocks": 0,
            "high_impact": [],
            "weekly_breakdown": {},
            "peak_week": "",
        }

        if cal_df.empty:
            return summary

        summary["total_stocks"] = len(cal_df)
        today = datetime.date.today()

        # 高冲击列表
        for _, row in cal_df.iterrows():
            release_data = row.to_dict()
            code = str(row.get("stock_code", ""))
            result = self.assess_release_impact(code, release_data)
            if result["impact_level"] == "high":
                summary["high_impact"].append(result)

        # 按周分布
        weekly = {}
        for _, row in cal_df.iterrows():
            try:
                d = datetime.date.fromisoformat(str(row["date"])[:10])
                week_offset = (d - today).days // 7
                w_start = today + datetime.timedelta(weeks=week_offset)
                w_end = today + datetime.timedelta(weeks=week_offset + 1, days=-1)
                week_label = f"第{week_offset + 1}周({w_start}~{w_end})"
                weekly[week_label] = weekly.get(week_label, 0) + 1
            except (ValueError, TypeError):
                pass
        summary["weekly_breakdown"] = weekly

        if weekly:
            summary["peak_week"] = max(weekly, key=weekly.get)

        return summary

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
        # FIX: 修复save_cache无异常保护导致写入失败时崩溃
        cache_file = os.path.join(self.cache_dir, "event_cache.json")
        data = {
            "earnings": self._earnings_cache,
            "unlock": self._unlock_cache,
            "updated": datetime.datetime.now().isoformat(),
        }
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存事件缓存失败: {e}")
