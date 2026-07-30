# -*- coding: utf-8 -*-
"""
集合竞价轨迹采集与形态识别模块 V1.0（V9.0 P2-1）
================================================
9:15-9:25期间每30秒采样竞价价格/量，记录轨迹并识别形态

形态识别:
  - 先高后低(试盘诱多): 9:15-9:20高开→9:20后回落
  - 先低后高(真实需求): 9:15低开→9:25持续上移
  - 持续上移(抢筹): 价格单调递增+量加速
  - 平稳(无主力): 波动<0.5%

运行方式:
  - scheduler.py 09:15启动后台线程
  - 09:25自动结束并输出形态判定

使用:
    from strategy.auction_monitor import AuctionTracker
    tracker = AuctionTracker(codes)
    tracker.start()  # 阻塞式采集到09:25
    result = tracker.get_result()
"""

import os
import sys
import time
import logging
import datetime
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)
CFG = getattr(config, 'AUCTION_TRACK_CONFIG', {})


class AuctionTracker:
    """集合竞价轨迹采集器"""

    def __init__(self, codes: list = None, holdings: dict = None):
        """
        参数:
            codes: 要监控的股票代码列表
            holdings: 持仓字典（用于获取名称）
        """
        self.codes = codes or []
        self.holdings = holdings or {}
        self.interval = CFG.get("sample_interval_sec", 30)
        self.samples = {code: [] for code in self.codes}  # {code: [(time, price, volume)]}
        self.result = {}

    def start(self):
        """启动采集（阻塞到09:25）"""
        if not self.codes:
            logger.warning("[竞价轨迹] 无监控标的")
            return

        start_time = CFG.get("start_time", "09:15")
        end_time = CFG.get("end_time", "09:25")

        logger.info(f"[竞价轨迹] 启动采集: {len(self.codes)}只, "
                    f"间隔{self.interval}秒, {start_time}-{end_time}")

        while True:
            now = datetime.datetime.now()
            current_time = now.strftime("%H:%M")

            # 超过结束时间，停止
            if current_time >= end_time:
                break

            # 未到开始时间，等待
            if current_time < start_time:
                time.sleep(5)
                continue

            # 采样
            self._take_sample()
            time.sleep(self.interval)

        # 最终采样
        self._take_sample()
        # 分析形态
        self._analyze_patterns()
        logger.info(f"[竞价轨迹] 采集完成, {len(self.result)}只已分析")

    def _take_sample(self):
        """执行一次采样"""
        try:
            from data.realtime import fetch_realtime_batch
            quotes = fetch_realtime_batch(self.codes)
            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            for code in self.codes:
                if code in quotes:
                    q = quotes[code]
                    price = q.get("price", 0)
                    volume = q.get("volume", 0)
                    if price > 0:
                        self.samples[code].append((now_str, price, volume))
        except Exception as e:
            logger.debug(f"[竞价轨迹] 采样异常: {e}")

    def _analyze_patterns(self):
        """分析竞价轨迹形态"""
        fake_drop = CFG.get("fake_high_open_drop", 0.02)
        real_rise = CFG.get("real_demand_rise", 0.01)
        rush_accel = CFG.get("rush_buy_acceleration", 0.5)

        for code, samples in self.samples.items():
            if len(samples) < 3:
                self.result[code] = {
                    "pattern": "insufficient_data",
                    "label": "数据不足",
                    "detail": f"仅{len(samples)}个采样点",
                }
                continue

            prices = [s[1] for s in samples]
            volumes = [s[2] for s in samples]
            name = self.holdings.get(code, {}).get("name", code)

            # 获取昨收（从第一次采样的prev_close或从realtime）
            try:
                from data.realtime import fetch_realtime_single
                q = fetch_realtime_single(code)
                prev_close = q.get("prev_close", prices[0])
            except Exception:
                prev_close = prices[0]

            if prev_close <= 0:
                prev_close = prices[0]

            # 价格轨迹分析
            first_price = prices[0]
            last_price = prices[-1]
            max_price = max(prices)
            min_price = min(prices)

            # 高开幅度
            first_gap = (first_price - prev_close) / prev_close
            final_gap = (last_price - prev_close) / prev_close

            # 轨迹方向
            mid_idx = len(prices) // 2
            first_half_trend = (prices[mid_idx] - prices[0]) / prices[0]
            second_half_trend = (prices[-1] - prices[mid_idx]) / prices[mid_idx]

            # 量能加速
            if len(volumes) >= 4:
                first_vol = volumes[mid_idx] - volumes[0]
                second_vol = volumes[-1] - volumes[mid_idx]
                vol_acceleration = (second_vol - first_vol) / max(first_vol, 1)
            else:
                vol_acceleration = 0

            # === 形态判定 ===
            pattern = "neutral"
            label = "平稳"
            detail = ""

            # 先高后低(试盘诱多): 前半段高开→后半段回落
            if first_gap > fake_drop and second_half_trend < -fake_drop / 2:
                pattern = "fake_high_open"
                label = "试盘诱多"
                detail = (f"高开{first_gap*100:.1f}%后回落至{final_gap*100:.1f}% | "
                         f"9:15-9:20推高→9:20后撤退，勿追高")

            # 先低后高(真实需求): 前半段低→后半段持续上移
            elif first_half_trend < 0 and second_half_trend > real_rise:
                pattern = "real_demand"
                label = "真实需求"
                detail = (f"先抑后扬: 后半段上移{second_half_trend*100:.1f}% | "
                         f"不可撤单阶段资金涌入，真实买入意愿")

            # 持续上移(抢筹): 价格单调递增+量加速
            elif all(prices[i] <= prices[i + 1] for i in range(len(prices) - 2)):
                if vol_acceleration > rush_accel:
                    pattern = "rush_buy"
                    label = "竞价抢筹"
                    detail = (f"价格单调上移+量加速{vol_acceleration*100:.0f}% | "
                             f"多资金抢筹，开盘大概率继续上攻")
                else:
                    pattern = "steady_rise"
                    label = "稳步上移"
                    detail = f"竞价期间稳步上移{final_gap*100:.1f}%"

            # 高开稳定
            elif final_gap > fake_drop and abs(second_half_trend) < 0.005:
                pattern = "stable_high"
                label = "高开稳定"
                detail = f"高开{final_gap*100:.1f}%且稳定，关注开盘后量能确认"

            # 低开
            elif final_gap < -real_rise:
                pattern = "low_open"
                label = "低开"
                detail = f"竞价低开{final_gap*100:.1f}%，注意开盘后走势"

            else:
                detail = f"竞价波动正常(高开{final_gap*100:.1f}%)"

            self.result[code] = {
                "pattern": pattern,
                "label": label,
                "detail": detail,
                "name": name,
                "code": code,
                "final_gap_pct": round(final_gap * 100, 2),
                "samples_count": len(samples),
                "vol_acceleration": round(vol_acceleration, 2),
            }

    def get_result(self) -> dict:
        """获取分析结果"""
        return self.result

    def get_alerts(self) -> list:
        """生成预警列表（供邮件/日志使用）"""
        alerts = []
        for code, r in self.result.items():
            pattern = r.get("pattern", "")
            if pattern in ("fake_high_open", "rush_buy", "low_open"):
                level = "warning" if pattern == "fake_high_open" else "info"
                if pattern == "low_open" and r.get("final_gap_pct", 0) < -3:
                    level = "critical"
                alerts.append({
                    "level": level,
                    "code": code,
                    "name": r.get("name", code),
                    "message": f"[竞价轨迹] {r.get('name', code)}({code}) {r['label']}: {r['detail']}",
                })
        return alerts


# ============================================================
# 便捷接口（供scheduler调用）
# ============================================================

def run_auction_tracking() -> dict:
    """
    执行竞价轨迹采集（scheduler 09:15调用）

    返回: {code: {pattern, label, detail, ...}}
    """
    import json

    # 加载持仓
    holdings_file = config.get_holdings_file()
    holdings = {}
    if os.path.exists(holdings_file):
        with open(holdings_file, "r", encoding="utf-8") as f:
            holdings = json.load(f)

    if not holdings:
        return {}

    codes = list(holdings.keys())
    tracker = AuctionTracker(codes=codes, holdings=holdings)
    tracker.start()

    result = tracker.get_result()
    alerts = tracker.get_alerts()

    # 输出日志
    for a in alerts:
        logger.warning(f"  {a['message']}")

    return result
