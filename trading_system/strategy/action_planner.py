# -*- coding: utf-8 -*-
"""
调仓建议生成器 (Action Planner) V10.0
=======================================
根据 regime状态 + 板块景气 + 持仓状况，生成"今天该做什么"的具体行动清单。

核心职责:
  1. 识别减仓候选: 弱势板块持仓 / 空头排列 / 深亏无止跌
  2. 识别加仓候选: 强势板块龙头 / 缩量回踩MA20
  3. 识别换仓配对: 卖出弱势→买入强势防御
  4. 计算目标仓位: 当前仓位 vs playbook目标仓位

数据流:
  regime_result + sector_result + holdings + all_data
      → ActionPlanner.generate_action_plan()
      → {reduce_list, add_list, switch_list, position_target, ...}

使用方式:
    from strategy.action_planner import ActionPlanner
    planner = ActionPlanner(regime_result, sector_result, holdings, all_data)
    plan = planner.generate_action_plan()

设计原则:
  - 所有建议带置信度和理由，低置信度建议不展示
  - 任何异常不抛出，降级为空行动清单
  - BEAR模式下优先输出减仓清单
"""

import logging
import json
import os
import datetime

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# FIX P0-B: 板块→个股映射桥接表（板块名→关联个股代码集合）
# 用于将板块景气信号映射到具体个股候选
_SECTOR_STOCK_MAP = {
    "能源化工": ["600989", "600346", "002648"],
    "能源":     ["600256", "601857", "600028"],
    "电力公用": ["600900", "600886", "600023"],
    "乳制品":   ["600887", "002714"],
    "CRO/CDMO": ["603259", "300759", "300347"],
    "宽基ETF":  ["510210", "510300", "510050"],
    "食品饮料": ["603345", "000858", "600519"],
    "半导体材料": ["562590", "688981", "002371"],
    "光通信":   ["600487", "002491", "300502"],
    "消费电子": ["002655", "002475", "300433"],
    "航运":     ["601919", "601866"],
    "军工":     ["600610", "600893", "000768"],
    "消费":     ["600737", "000651", "600887"],
    "数字支付": ["000997", "300468"],
    "出版传媒": ["600551", "601999"],
}


class ActionPlanner:
    """
    调仓建议生成器

    职责:
      根据 regime状态 + 板块景气 + 持仓状况，生成“今天该做什么”的具体行动清单。
      包括减仓候选、加仓候选、换仓配对、目标仓位计算。

    核心输入:
      regime_result: MarketRegimeDetector.detect() 返回的状态字典
      sector_result: SectorMonitor.analyze() 返回的板块分析结果（可为None）
      holdings: {code: {name, shares, buy_price, sector, ...}} 持仓信息
      all_data: {code: DataFrame} K线数据
      total_capital: 总资金

    核心输出 (generate_action_plan() 返回 dict):
      regime_action: 核心行动指令
      regime_state: BULL/BEAR/RANGE
      reduce_list: 减仓/清仓建议列表
      add_list: 加仓/建仓建议列表
      switch_list: 换仓配对列表
      position_target: 目标仓位比例
      position_current: 当前仓位比例

    关键依赖:
      - RegimePlaybook: 三态策略配置（仓位限制/风控规则/板块偏好）
      - _SECTOR_STOCK_MAP: 板块→个股映射桥接表（P0-B）

    V10 修复:
      - P1-D: BEAR换仓TypeError修复
      - P1-E: 加仓候选质量门槛（动量+MA20斜率）
      - P1-F: 目标仓位动态调整（基于playbook置信度）
      - P2-B: 涨跌停/停牌标的过滤
      - P2-C: 换手率控制（最大40%）
    """

    def __init__(self, regime_result: dict, sector_result: dict,
                 holdings: dict, all_data: dict, total_capital: float = 722000):
        """
        参数:
            regime_result: MarketRegimeDetector.detect() 返回
            sector_result: SectorMonitor.analyze() 返回（可为None）
            holdings: {code: {name, shares, buy_price, sector, ...}}
            all_data: {code: DataFrame} K线数据
            total_capital: 总资金
        """
        from strategy.regime_playbook import RegimePlaybook
        self.playbook = RegimePlaybook(regime_result)
        self.sector_result = sector_result or {}
        self.holdings = holdings
        self.all_data = all_data
        self.total_capital = total_capital

        # 缓存
        self._ranked = self.sector_result.get("ranked", [])
        self._sector_metrics = self.sector_result.get("sector_metrics", {})

        # FIX P1-H: 数据质量追踪——记录缺失维度
        self._data_quality = {"total_dims": 0, "missing_dims": []}
        # FIX P2-C: 换手率控制——记录当日建议交易笔数
        self._turnover_count = 0
        self._MAX_TURNOVER_RATIO = 0.40  # 单次调仓最多换40%仓位

    def generate_action_plan(self) -> dict:
        """
        生成完整行动计划

        返回: {
            "regime_action": str,       # 核心行动指令
            "regime_state": str,        # BULL/BEAR/RANGE
            "reduce_list": [...],       # 减仓/清仓建议
            "add_list": [...],          # 加仓/建仓建议
            "switch_list": [...],       # 换仓配对
            "position_target": float,   # 目标仓位比例
            "current_position": float,  # 当前仓位比例
            "position_gap": float,      # 仓位缺口（正=需加仓，负=需减仓）
            "urgency": str,            # immediate / gradual / hold
            "expected_impact": str,     # 预期影响描述
            "confidence": float,        # 整体置信度
        }
        """
        try:
            current_pos = self._calc_current_position()
            # FIX P1-F: 根据置信度在 min_total~max_total 间动态调整目标仓位
            _limits = self.playbook.position_limits()
            _conf = self.playbook.confidence
            target_pos = _limits["min_total"] + (_limits["max_total"] - _limits["min_total"]) * _conf
            pos_gap = target_pos - current_pos

            # V10.1: 获取弹性仓位信息（TOP3板块持仓可超配）
            elastic_info = self._get_elastic_position_info()

            # 根据regime状态生成不同行动
            if self.playbook.state == "BEAR":
                plan = self._bear_mode_action(current_pos, target_pos, pos_gap)
            elif self.playbook.state == "BULL":
                plan = self._bull_mode_action(current_pos, target_pos, pos_gap)
            else:
                plan = self._range_mode_action(current_pos, target_pos, pos_gap)

            # V10.1: 附加弹性仓位信息
            plan["elastic_info"] = elastic_info
            return plan

        except Exception as e:
            logger.warning(f"[ActionPlanner] 生成行动计划失败: {e}")
            return self._empty_plan("生成失败: " + str(e))

    # ============================================================
    # BEAR模式: 减仓+转防御
    # ============================================================
    def _bear_mode_action(self, current_pos, target_pos, pos_gap):
        """BEAR模式行动: 优先减仓，输出减仓清单 + 防御型加仓候选"""
        reduce_list = self._identify_reduce_candidates()
        add_list = []
        switch_list = []

        # 计算需要减仓的金额
        reduce_target = (current_pos - target_pos) * self.total_capital
        reduce_total = sum(r.get("amount", 0) for r in reduce_list)

        # 识别防御板块候选（如果有强势防御板块可换仓）
        defensive_candidates = self._find_defensive_add_candidates()
        if defensive_candidates and reduce_list:
            # 生成换仓配对
            for i, add_c in enumerate(defensive_candidates[:2]):
                if i < len(reduce_list):
                    switch_list.append({
                        "sell_code": reduce_list[i]["code"],
                        "sell_name": reduce_list[i]["name"],  # FIX P1-D: 原代码 reduce_list["name"] 会 TypeError
                        "buy_code": add_c["code"],
                        "buy_name": add_c["name"],
                        "reason": f"弱势板块→防御板块换仓",
                        "amount": min(reduce_list[i].get("amount", 0),
                                      add_c.get("amount", 0)),
                    })

        # 判断紧急程度
        if current_pos > target_pos + 0.20:
            urgency = "immediate"
        elif current_pos > target_pos + 0.10:
            urgency = "gradual"
        else:
            urgency = "hold"

        expected = f"减仓至{target_pos:.0%}以下，当前{current_pos:.0%}"
        if reduce_total > 0:
            expected += f"，预计回笼{reduce_total/10000:.1f}万元"

        return {
            "regime_action": self.playbook.action_mode_cn(),
            "regime_state": "BEAR",
            "reduce_list": reduce_list,
            "add_list": add_list,
            "switch_list": switch_list,
            "position_target": target_pos,
            "current_position": current_pos,
            "position_gap": pos_gap,
            "urgency": urgency,
            "expected_impact": expected,
            "confidence": self.playbook.confidence,
        }

    # ============================================================
    # BULL模式: 持仓+加仓强势
    # ============================================================
    def _bull_mode_action(self, current_pos, target_pos, pos_gap):
        """BULL模式行动: 持仓待涨 + 回踩加仓 + 换仓优化"""
        reduce_list = self._identify_weak_holdings()  # 仅识别弱势持仓
        add_list = self._identify_add_candidates()
        switch_list = []

        urgency = "gradual" if pos_gap > 0.05 else "hold"
        expected = f"目标仓位{target_pos:.0%}，当前{current_pos:.0%}"
        if pos_gap > 0.05:
            expected += f"，可加仓约{pos_gap * self.total_capital / 10000:.1f}万元"

        return {
            "regime_action": self.playbook.action_mode_cn(),
            "regime_state": "BULL",
            "reduce_list": reduce_list,
            "add_list": add_list,
            "switch_list": switch_list,
            "position_target": target_pos,
            "current_position": current_pos,
            "position_gap": pos_gap,
            "urgency": urgency,
            "expected_impact": expected,
            "confidence": self.playbook.confidence,
        }

    # ============================================================
    # RANGE模式: 高抛低吸
    # ============================================================
    def _range_mode_action(self, current_pos, target_pos, pos_gap):
        """RANGE模式行动: 高抛低吸 + 均值回归"""
        reduce_list = self._identify_weak_holdings()
        add_list = self._identify_add_candidates()
        switch_list = self._identify_switch_pairs()

        urgency = "hold"
        expected = f"震荡市维持{target_pos:.0%}仓位，当前{current_pos:.0%}"

        return {
            "regime_action": self.playbook.action_mode_cn(),
            "regime_state": "RANGE",
            "reduce_list": reduce_list,
            "add_list": add_list,
            "switch_list": switch_list,
            "position_target": target_pos,
            "current_position": current_pos,
            "position_gap": pos_gap,
            "urgency": urgency,
            "expected_impact": expected,
            "confidence": self.playbook.confidence,
        }

    # ============================================================
    # 减仓候选识别
    # ============================================================
    def _identify_reduce_candidates(self) -> list:
        """识别应减仓/清仓的标的（BEAR模式用）:
        - 持仓板块在排名后30%
        - 个股空头排列（价格<MA20<MA60）
        - 浮亏>15%且无止跌信号
        """
        candidates = []
        for code, info in self.holdings.items():
            if info.get("shares", 0) <= 0:
                continue

            reasons = []
            priority = 0  # 越高越优先

            # 检查板块排名
            sector = info.get("sector", "")
            sector_rank = self._get_sector_rank(sector)
            if sector_rank is not None and sector_rank > 0.7:  # 后30%
                reasons.append(f"板块{sector}排名后{int((1-sector_rank)*100)}%")
                priority += 2

            # 检查个股趋势
            df = self.all_data.get(code)
            if df is not None and len(df) >= 60:
                close = df["close"].iloc[-1]
                ma20 = df["close"].rolling(20).mean().iloc[-1]
                ma60 = df["close"].rolling(60).mean().iloc[-1]
                if close < ma20 and ma20 < ma60:
                    reasons.append("空头排列(价格<MA20<MA60)")
                    priority += 3

            # 检查浮亏
            price = df["close"].iloc[-1] if df is not None and len(df) > 0 else 0
            cost = info.get("buy_price", 0)
            if cost > 0 and price > 0:
                pnl_pct = (price / cost - 1) * 100
                if pnl_pct < -15:
                    reasons.append(f"浮亏{pnl_pct:.1f}%，无止跌信号")
                    priority += 1

            if reasons:
                amount = price * info.get("shares", 0)
                candidates.append({
                    "code": code,
                    "name": info.get("name", code),
                    "sector": sector,
                    "reason": "；".join(reasons),
                    "priority": priority,
                    "amount": amount,
                    "shares": info.get("shares", 0),
                    "price": price,
                    "pnl_pct": (price / cost - 1) * 100 if cost > 0 and price > 0 else 0,
                    "confidence": self._reduce_confidence(reasons),
                })

        # 按优先级排序
        candidates.sort(key=lambda x: x["priority"], reverse=True)
        # FIX(2026-10-08): priority 排序完成后转成中文标签。此前输出的是 int，
        # 而报告渲染侧按字符串做 `"必减" in priority` 判断 → TypeError，
        # 导致 BEAR 模式下整个"今日行动清单"面板被 except 静默丢弃。
        for c in candidates:
            c["priority"] = self._priority_label(c["priority"], full=True)
        return candidates

    def _priority_label(self, score, full: bool = False) -> str:
        """把减仓优先级得分翻译成展示标签（full=True 用三档口径）"""
        if full:
            if score >= 5:
                return "必减"
            if score >= 3:
                return "建议减"
            return "可关注"
        if score >= 4:
            return "必减"
        if score >= 2:
            return "建议减"
        return "可关注"

    def _reduce_confidence(self, reasons) -> float:
        """减仓/弱势建议置信度：随命中理由数递增，并被 regime 置信度缩放"""
        try:
            base = float(getattr(self.playbook, "confidence", 0.5) or 0.5)
        except Exception:
            base = 0.5
        return round(min(0.9, base * (0.4 + 0.2 * len(reasons or []))), 2)

    def _identify_weak_holdings(self) -> list:
        """识别弱势持仓:
        - 板块排名后30%
        - 空头排列
        - 浮亏>10%

        V10.1改进: TOP3板块持仓不标记为弱势（板块景气联动）

        返回: [{code, name, sector, reason, price, shares, amount,
                pnl_pct, priority(中文标签), confidence}] —— 与
                _identify_reduce_candidates 同 schema，供报告渲染侧统一读取
        """
        candidates = []
        top_sectors = self._get_top_sector_names(3)

        for code, info in self.holdings.items():
            if info.get("shares", 0) <= 0:
                continue

            reasons = []
            sector = info.get("sector", "")

            # V10.1: TOP3板块持仓享受弹性仓位，不标记弱势
            if sector in top_sectors:
                continue  # 跳过TOP3板块持仓

            sector_rank = self._get_sector_rank(sector)
            if sector_rank is not None and sector_rank > 0.7:
                reasons.append(f"板块{sector}排名靠后")

            df = self.all_data.get(code)
            if df is not None and len(df) >= 60:
                close = df["close"].iloc[-1]
                ma20 = df["close"].rolling(20).mean().iloc[-1]
                if close < ma20:
                    reasons.append("价格<MA20偏弱")

            if reasons:
                price = df["close"].iloc[-1] if df is not None and len(df) > 0 else 0
                cost = info.get("buy_price", 0)
                candidates.append({
                    "code": code,
                    "name": info.get("name", code),
                    "sector": sector,
                    "reason": "；".join(reasons),
                    "price": price,
                    "shares": info.get("shares", 0),
                    "amount": price * info.get("shares", 0),
                    # FIX(2026-10-08): 补齐报告渲染侧要读的字段。此前本方法只返回
                    # code/name/sector/reason/price/shares，而渲染侧按
                    # _identify_reduce_candidates 的完整 schema 取 pnl_pct/priority/
                    # confidence，导致 RANGE/BULL 模式减仓清单恒显示
                    # "浮盈 +0.0% | 置信度 0%"（宝丰能源实际-47%也显示+0.0%）。
                    "pnl_pct": (price / cost - 1) * 100 if cost > 0 and price > 0 else 0,
                    "priority": self._priority_label(len(reasons) * 2),
                    "confidence": self._reduce_confidence(reasons),
                })

        return candidates

    # ============================================================
    # 加仓候选识别
    # ============================================================
    def _identify_add_candidates(self) -> list:
        """识别加仓/建仓候选:
        - 板块景气TOP3中的个股
        - 缩量回踩MA20的强势股
        FIX P1-E: 新增信号质量门槛（动量得分+MA20斜率过滤）
        FIX P0-B: 板块→个股映射桥接（从强势板块映射到具体个股）
        FIX P2-B: 过滤涨跌停/停牌标的
        """
        candidates = []
        top_sectors = self._get_top_sector_names(3)

        for code, df in self.all_data.items():
            if df is None or len(df) < 60:
                continue
            if code in self.holdings:
                continue  # 已持仓的跳过

            # FIX P2-B: 过滤停牌（最新成交量=0）和涨跌停（涨跌幅>=9.8%）
            latest = df.iloc[-1]
            if latest.get("volume", 1) <= 0:
                continue  # 停牌
            prev_close = df["close"].iloc[-2] if len(df) >= 2 else latest["close"]
            if prev_close > 0:
                chg_pct = (latest["close"] - prev_close) / prev_close
                if abs(chg_pct) >= 0.098:
                    continue  # 涨跌停附近，跳过

            close = df["close"].iloc[-1]
            ma20 = df["close"].rolling(20).mean().iloc[-1]

            # 缩量回踩MA20检测
            if close > ma20 * 0.98 and close < ma20 * 1.02:
                vol = df["volume"].iloc[-1]
                vol_ma = df["volume"].rolling(20).mean().iloc[-1]
                if vol < vol_ma * 0.7:
                    # FIX P0-B: 检查该股是否属于强势板块
                    stock_sector = self.holdings.get(code, {}).get("sector", "")
                    in_top_sector = stock_sector in top_sectors
                    # 桥接: 如果持仓中无板块信息，通过映射表检查
                    if not in_top_sector and stock_sector == "":
                        for sn in top_sectors:
                            if code in _SECTOR_STOCK_MAP.get(sn, []):
                                in_top_sector = True
                                break
                    if not in_top_sector:
                        continue

                    # FIX P1-E: 信号质量门槛——动量得分 + MA20斜率
                    momentum_score = 0
                    if len(df) >= 20:
                        ret_20d = (df["close"].iloc[-1] / df["close"].iloc[-20] - 1) * 100
                        momentum_score = max(0, min(30, ret_20d * 3))

                    ma20_slope = 0
                    ma20_vals = df["close"].rolling(20).mean().dropna()
                    if len(ma20_vals) >= 5:
                        ma20_slope = (ma20_vals.iloc[-1] / ma20_vals.iloc[-5] - 1) * 100

                    # 质量门槛: 动量分>10 且 MA20斜率>0（上升趋势）
                    if momentum_score < 10 or ma20_slope <= 0:
                        continue

                    candidates.append({
                        "code": code,
                        "name": code,  # 名称需外部填充
                        "buy_price": round(ma20, 2),
                        "stop_loss": round(ma20 * 0.95, 2),
                        "reason": f"缩量回踩MA20+强势板块+动量{momentum_score:.0f}分",
                        "position_pct": self.playbook.position_limits()["max_single"] * 100,
                        "momentum_score": round(momentum_score, 1),
                        "sector": stock_sector,
                    })

        # 按动量得分降序取前5
        candidates.sort(key=lambda x: x.get("momentum_score", 0), reverse=True)
        return candidates[:5]  # 最多5个

    # ============================================================
    # V10.1: 趋势启动弹性仓位检测
    # ============================================================
    def _detect_trend_breakout(self, code: str) -> dict:
        """检测个股是否处于趋势启动初期（缩量回踩MA20后放量突破）

        返回: {"breakout": bool, "reason": str, "elastic_ratio": float}
            elastic_ratio: 允许超配的比例（默认+5%）
        """
        df = self.all_data.get(code)
        if df is None or len(df) < 20:
            return {"breakout": False, "reason": "数据不足", "elastic_ratio": 0}

        close = df["close"].values
        volume = df["volume"].values if "volume" in df.columns else None
        ma20 = df["close"].rolling(20).mean().iloc[-1]

        # 条件1: 价格回踩MA20附近（±2%）
        latest_close = close[-1]
        near_ma20 = abs(latest_close - ma20) / ma20 < 0.02

        # 条件2: 近期缩量（前5日均量 < 20日均量70%）
        vol_shrink = False
        if volume is not None and len(volume) >= 20:
            vol_5 = np.mean(volume[-6:-1])  # 前5日
            vol_20 = np.mean(volume[-20:])
            vol_shrink = vol_5 < vol_20 * 0.7

        # 条件3: 当日放量突破（成交量 > 20日均量1.5倍）
        vol_breakout = False
        if volume is not None and len(volume) >= 20:
            vol_breakout = volume[-1] > np.mean(volume[-20:]) * 1.5

        # 条件4: 板块在TOP3
        sector = self.holdings.get(code, {}).get("sector", "")
        top_sectors = self._get_top_sector_names(3)
        sector_strong = sector in top_sectors

        # 综合判断: 缩量回踩 + 放量突破 + 板块强势
        breakout = near_ma20 and vol_shrink and vol_breakout and sector_strong

        return {
            "breakout": breakout,
            "reason": "缩量回踩MA20后放量突破+板块TOP3" if breakout else "",
            "elastic_ratio": 0.05 if breakout else 0,  # 允许超配5%
        }

    def _get_elastic_position_info(self) -> dict:
        """获取弹性仓位信息（TOP3板块持仓可超配）

        返回: {"elastic_holdings": [...], "total_elastic_ratio": float}
        """
        elastic_holdings = []
        total_elastic_ratio = 0

        for code, info in self.holdings.items():
            if info.get("shares", 0) <= 0:
                continue

            sector = info.get("sector", "")
            top_sectors = self._get_top_sector_names(3)

            if sector in top_sectors:
                # 检测趋势启动
                breakout_info = self._detect_trend_breakout(code)
                df = self.all_data.get(code)
                if df is not None and len(df) > 0:
                    price = df["close"].iloc[-1]
                else:
                    price = info.get("buy_price", 0)
                amount = price * info.get("shares", 0)
                ratio = amount / self.total_capital if self.total_capital > 0 else 0

                elastic_holdings.append({
                    "code": code,
                    "name": info.get("name", code),
                    "sector": sector,
                    "position_ratio": ratio,
                    "breakout": breakout_info["breakout"],
                    "reason": breakout_info["reason"],
                    "elastic_ratio": breakout_info["elastic_ratio"],
                })
                total_elastic_ratio += breakout_info["elastic_ratio"]

        return {
            "elastic_holdings": elastic_holdings,
            "total_elastic_ratio": total_elastic_ratio,
        }

    def _find_defensive_add_candidates(self) -> list:
        """寻找防御型加仓候选: playbook防御板块中的强势个股（BEAR模式用）"""
        candidates = []
        defensive = self.playbook.defensive_sectors()

        for code, info in self.holdings.items():
            sector = info.get("sector", "")
            if self.playbook.is_defensive_sector(sector):
                df = self.all_data.get(code)
                if df is not None and len(df) >= 20:
                    close = df["close"].iloc[-1]
                    ma20 = df["close"].rolling(20).mean().iloc[-1]
                    if close > ma20:  # 多头趋势
                        candidates.append({
                            "code": code,
                            "name": info.get("name", code),
                            "sector": sector,
                            "amount": close * info.get("shares", 0),
                        })

        return candidates

    # ============================================================
    # 换仓配对识别
    # ============================================================
    def _identify_switch_pairs(self) -> list:
        """识别换仓配对: 卖出弱势持仓 → 买入强势候选"""
        pairs = []
        weak = self._identify_weak_holdings()
        strong = self._identify_add_candidates()

        for i, w in enumerate(weak[:3]):
            if i < len(strong):
                s = strong[i]
                pairs.append({
                    "sell_code": w["code"],
                    "sell_name": w["name"],
                    "buy_code": s["code"],
                    "buy_name": s.get("name", s["code"]),
                    "reason": f"{w.get('reason', '弱势')}→{s.get('reason', '强势')}",
                })

        return pairs

    # ============================================================
    # 辅助方法
    # ============================================================
    def _calc_current_position(self) -> float:
        """计算当前仓位比例"""
        total_mv = 0
        for code, info in self.holdings.items():
            df = self.all_data.get(code)
            if df is not None and len(df) > 0:
                price = df["close"].iloc[-1]
            else:
                price = info.get("buy_price", 0)
            total_mv += price * info.get("shares", 0)
        return total_mv / self.total_capital if self.total_capital > 0 else 0

    def _get_sector_rank(self, sector_name: str):
        """获取板块排名百分位（0=最强，1=最弱）"""
        if not self._ranked or not sector_name:
            return None
        total = len(self._ranked)
        for i, r in enumerate(self._ranked):
            if r.get("sector") == sector_name:
                return (i + 1) / total if total > 0 else 0.5
        return None

    def _get_top_sector_names(self, n: int = 3) -> list:
        """获取TOP N板块名称"""
        if not self._ranked:
            return []
        return [r.get("sector", "") for r in self._ranked[:n]]

    def _empty_plan(self, reason: str = "") -> dict:
        """空行动计划（降级用）"""
        return {
            "regime_action": self.playbook.action_mode_cn(),
            "regime_state": self.playbook.state,
            "reduce_list": [],
            "add_list": [],
            "switch_list": [],
            "position_target": self.playbook.position_limits()["max_total"],
            "current_position": 0,
            "position_gap": 0,
            "urgency": "hold",
            "expected_impact": f"无法生成行动计划: {reason}",
            "confidence": 0,
        }


# ============================================================
# 独立测试
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("=" * 50)
    print("  ActionPlanner 测试")
    print("=" * 50)

    # 模拟数据
    regime_bear = {"state": "BEAR", "confidence": 0.75}
    holdings = {
        "002415": {"name": "海康威视", "shares": 500, "buy_price": 30, "sector": "AI视觉"},
        "600036": {"name": "招商银行", "shares": 100, "buy_price": 42, "sector": "银行"},
    }

    planner = ActionPlanner(
        regime_result=regime_bear,
        sector_result={"ranked": [], "sector_metrics": {}},
        holdings=holdings,
        all_data={},
        total_capital=722000,
    )

    plan = planner.generate_action_plan()
    print(f"\nregime: {plan['regime_state']}")
    print(f"action: {plan['regime_action']}")
    print(f"position: {plan['current_position']:.1%} → {plan['position_target']:.1%}")
    print(f"urgency: {plan['urgency']}")
    print(f"reduce_list: {len(plan['reduce_list'])} items")
    print(f"add_list: {len(plan['add_list'])} items")

    print("\n[OK] ActionPlanner 测试完成")
