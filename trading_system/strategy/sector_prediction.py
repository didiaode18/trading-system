"""
板块前瞻预测引擎 - 四维评分模型

维度与权重:
- 资金流向 (30%): 板块内个股主力资金净流入占比、连续流入天数
- 量能特征 (25%): 量比异动、放量上涨个股占比
- 财报与基本面 (25%): EPS增长、机构覆盖、业绩持续性
- 政策与情绪面 (20%): 政策热度映射、RPS斜率动量

降级策略: 每个维度独立 try/except，失败返回中性分50
"""
import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class SectorPredictor:
    """板块前瞻预测引擎 - 四维评分模型"""

    def __init__(self, fund_flow_data: dict = None):
        """
        Args:
            fund_flow_data: _load_fund_flow_data() 返回值
                           格式: {code: {"bonus": int, "signals": [str]}}
        """
        self.fund_flow_data = fund_flow_data or {}
        # 读取配置权重
        try:
            from trading_system import config
            self.weights = getattr(config, 'SCREENER_CONFIG', {}).get("prediction_weights",
                {"capital_flow": 0.30, "volume_feature": 0.25, "fundamental": 0.25, "policy_sentiment": 0.20})
        except Exception:
            self.weights = {"capital_flow": 0.30, "volume_feature": 0.25, "fundamental": 0.25, "policy_sentiment": 0.20}

    def predict_batch(self, sectors_info: list, data_dict: dict) -> dict:
        """
        批量预测所有板块的前瞻评分

        Args:
            sectors_info: [{"sector": str, "codes": [str], "avg_change": float}, ...]
            data_dict: {code: DataFrame} 日线数据

        Returns:
            {sector_name: {"score": float, "drivers": [str], "sub_scores": dict}}
        """
        predictions = {}
        dimension_scores = {}  # for post-processing

        for info in sectors_info:
            sector = info["sector"]
            codes = info["codes"]

            # 四维独立评分
            sub_scores = {}
            all_drivers = []

            # 1. 资金流向
            try:
                cf_score, cf_tags = self._score_capital_flow(codes)
                sub_scores["capital_flow"] = cf_score
                all_drivers.extend(cf_tags)
            except Exception as e:
                logger.debug(f"[板块前瞻] {sector} 资金流向评分异常: {e}")
                sub_scores["capital_flow"] = 50.0

            # 2. 量能特征
            try:
                vol_score, vol_tags = self._score_volume_feature(codes, data_dict)
                sub_scores["volume_feature"] = vol_score
                all_drivers.extend(vol_tags)
            except Exception as e:
                logger.debug(f"[板块前瞻] {sector} 量能特征评分异常: {e}")
                sub_scores["volume_feature"] = 50.0

            # 3. 基本面
            try:
                fund_score, fund_tags = self._score_fundamental(codes)
                sub_scores["fundamental"] = fund_score
                all_drivers.extend(fund_tags)
            except Exception as e:
                logger.debug(f"[板块前瞻] {sector} 基本面评分异常: {e}")
                sub_scores["fundamental"] = 50.0

            # 4. 政策情绪面
            try:
                sent_score, sent_tags = self._score_policy_sentiment(sector, codes, data_dict)
                sub_scores["policy_sentiment"] = sent_score
                all_drivers.extend(sent_tags)
            except Exception as e:
                logger.debug(f"[板块前瞻] {sector} 政策情绪面评分异常: {e}")
                sub_scores["policy_sentiment"] = 50.0

            # 加权总分
            total = (sub_scores["capital_flow"] * self.weights.get("capital_flow", 0.30) +
                     sub_scores["volume_feature"] * self.weights.get("volume_feature", 0.25) +
                     sub_scores["fundamental"] * self.weights.get("fundamental", 0.25) +
                     sub_scores["policy_sentiment"] * self.weights.get("policy_sentiment", 0.20))
            total = max(0, min(100, total))

            predictions[sector] = {"score": round(total, 1), "drivers": [], "sub_scores": sub_scores}
            dimension_scores[sector] = {"total": total, "tags": all_drivers}

        # 后处理: 跨板块相对强弱调整（V9.2: 增强区分度，偏离±10分）
        if len(dimension_scores) > 1:
            all_totals = [v["total"] for v in dimension_scores.values()]
            median_score = float(np.median(all_totals))
            std_score = float(np.std(all_totals)) if len(all_totals) > 1 else 1.0
            for sector, ds in dimension_scores.items():
                # V9.2: 用标准差归一化偏离，拉开分数差距
                z_score = (ds["total"] - median_score) / max(std_score, 1.0)
                adjustment = max(-10, min(10, z_score * 3))  # 标准差单位×3，限幅±10
                predictions[sector]["score"] = round(max(0, min(100, predictions[sector]["score"] + adjustment)), 1)
                # 追加相对强弱标签
                if adjustment >= 5:
                    ds["tags"].append("板块领先")
                elif adjustment >= 3:
                    ds["tags"].append("板块偏强")
                elif adjustment <= -5:
                    ds["tags"].append("板块落后")
                elif adjustment <= -3:
                    ds["tags"].append("板块偏弱")

        # 提取 top-2 驱动标签
        for sector, ds in dimension_scores.items():
            predictions[sector]["drivers"] = ds["tags"][:2]

        return predictions

    def _score_capital_flow(self, codes: list):
        """
        维度1: 资金流向评分
        数据源: fund_flow_data ({code: {"bonus": int, "signals": [str]}})
        """
        if not self.fund_flow_data:
            return 50.0, []

        total = len(codes)
        if total == 0:
            return 50.0, []

        # 统计板块内主力资金净流入个股数
        inflow_count = 0
        consecutive_flow_count = 0
        for code in codes:
            flow_info = self.fund_flow_data.get(code, {})
            bonus = flow_info.get("bonus", 0)
            if bonus > 0:
                inflow_count += 1
            # 检查连续净流入天数（存储在 signals 中）
            signals = flow_info.get("signals", [])
            for sig in signals:
                if "连续" in sig and "流入" in sig:
                    consecutive_flow_count += 1
                    break

        inflow_ratio = inflow_count / total
        consecutive_ratio = min(consecutive_flow_count, 3) / 3.0

        score = inflow_ratio * 80 + consecutive_ratio * 20
        score = max(0, min(100, score))

        tags = []
        if score > 60:
            tags.append("资金抢筹")
        elif score > 50:
            tags.append("资金流入")

        return round(score, 1), tags

    def _score_volume_feature(self, codes: list, data_dict: dict):
        """
        维度2: 量能特征评分
        数据源: data_dict[code] DataFrame 的 volume, vol_ma20 列
        """
        total = 0
        vol_up_count = 0
        vol_ratios = []

        for code in codes:
            df = data_dict.get(code)
            if df is None or len(df) < 20:
                continue
            total += 1

            try:
                # 量比
                vol_ma20 = df["vol_ma20"].iloc[-1] if "vol_ma20" in df.columns else df["volume"].iloc[-20:].mean()
                if vol_ma20 > 0:
                    vol_ratio = df["volume"].iloc[-1] / vol_ma20
                    vol_ratios.append(min(vol_ratio, 3.0))

                # 放量上涨: 近5日均量 > 近20日均量 且 近5日收盘上涨
                vol_5d = df["volume"].iloc[-5:].mean()
                vol_20d = df["volume"].iloc[-20:].mean()
                close_5d_change = (df["close"].iloc[-1] / df["close"].iloc[-5] - 1) if df["close"].iloc[-5] > 0 else 0

                if vol_5d > vol_20d and close_5d_change > 0:
                    vol_up_count += 1
            except Exception:
                continue

        if total == 0:
            return 50.0, []

        vol_up_ratio = vol_up_count / total
        avg_vol_ratio = np.mean(vol_ratios) if vol_ratios else 1.0

        score = vol_up_ratio * 50 + min(avg_vol_ratio, 3.0) / 3.0 * 50
        score = max(0, min(100, score))

        tags = []
        if score > 60:
            tags.append("量能放大")

        return round(score, 1), tags

    def _score_fundamental(self, codes: list):
        """
        维度3: 财报与基本面评分
        数据源: 全局 FUNDAMENTAL_DATA ({code: {eps_growth_q, eps_growth_3y, has_institution}})
        """
        # 从 stock_screener.py 导入全局 FUNDAMENTAL_DATA
        try:
            from trading_system.strategy.stock_screener import FUNDAMENTAL_DATA
        except Exception:
            try:
                from strategy.stock_screener import FUNDAMENTAL_DATA
            except Exception:
                return 50.0, []

        if not FUNDAMENTAL_DATA:
            return 50.0, []

        total = 0
        eps_positive = 0
        institution_count = 0
        eps_3y_positive = 0

        for code in codes:
            fund = FUNDAMENTAL_DATA.get(code)
            if fund is None:
                continue
            total += 1

            eps_q = fund.get("eps_growth_q", 0)
            if eps_q > 0:
                eps_positive += 1

            if fund.get("has_institution", False):
                institution_count += 1

            eps_3y = fund.get("eps_growth_3y", 0)
            if eps_3y > 0:
                eps_3y_positive += 1

        if total == 0:
            return 50.0, []

        eps_ratio = eps_positive / total
        inst_ratio = institution_count / total
        eps_3y_ratio = eps_3y_positive / total

        score = eps_ratio * 50 + inst_ratio * 20 + eps_3y_ratio * 30
        score = max(0, min(100, score))

        tags = []
        if score > 60:
            tags.append("业绩超预期")
        elif score > 50:
            tags.append("业绩增长")

        return round(score, 1), tags

    def _score_policy_sentiment(self, sector_name: str, codes: list, data_dict: dict):
        """
        维度4: 政策与情绪面评分
        数据源: config.SECTOR_POLICY_HEAT + data_dict 中的 ma20_slope
        """
        # 政策热度
        try:
            from trading_system import config
            policy_heat = getattr(config, 'SECTOR_POLICY_HEAT', {}).get(sector_name, 50)
        except Exception:
            policy_heat = 50

        # RPS斜率: 板块内平均 ma20_slope 归一化
        slopes = []
        for code in codes:
            df = data_dict.get(code)
            if df is None or len(df) < 23:
                continue
            if "ma20" in df.columns:
                try:
                    slope = df["ma20"].iloc[-1] - df["ma20"].iloc[-4]
                    if not pd.isna(slope):
                        # 归一化: slope / ma20 * 100 → 百分比变化
                        ma20_val = df["ma20"].iloc[-1]
                        if ma20_val > 0:
                            slopes.append(slope / ma20_val * 100)
                except Exception:
                    continue

        if slopes:
            avg_slope_pct = np.mean(slopes)
            # 归一化到 0-100: -5%以下=0, +5%以上=100, 线性映射
            rps_component = max(0, min(100, (avg_slope_pct + 5) / 10 * 100))
        else:
            rps_component = 50.0

        score = policy_heat * 0.5 + rps_component * 0.5
        score = max(0, min(100, score))

        tags = []
        if score > 60:
            tags.append("政策催化")

        return round(score, 1), tags


# ============================================================
# V9.2: 板块预测准确率追踪
# ============================================================
import json
import os
import datetime

_SECTOR_PRED_HISTORY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'sector_prediction_history.json')


def record_sector_predictions(predictions: dict):
    """记录当日板块预测分数，供7天后结算验证
    
    Args:
        predictions: SectorPredictor.predict_batch() 返回值
                     {sector: {"score": float, "drivers": [...], ...}}
    """
    try:
        history = []
        if os.path.exists(_SECTOR_PRED_HISTORY):
            with open(_SECTOR_PRED_HISTORY, 'r', encoding='utf-8') as f:
                history = json.load(f)

        today_str = datetime.date.today().isoformat()
        entry = {
            "date": today_str,
            "scores": {s: d.get("score", 50) for s, d in predictions.items()},
            "settled": False,
        }
        history.append(entry)

        # 保留最近90天记录
        history = history[-90:]
        with open(_SECTOR_PRED_HISTORY, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        logger.debug(f"[板块追踪] 已记录{len(predictions)}个板块预测")
    except Exception as e:
        logger.warning(f"[板块追踪] 记录失败: {e}")


def settle_sector_predictions(load_close_fn=None, forward_days: int = 7) -> list:
    """结算到期的板块预测
    
    Args:
        load_close_fn: 函数 load_close_fn(code) → [(date, close), ...] (可选)
        forward_days: 前瞻窗口（默认7天）
    
    Returns:
        已结算列表 [{"date": str, "sectors": int}]
    """
    try:
        if not os.path.exists(_SECTOR_PRED_HISTORY):
            return []
        with open(_SECTOR_PRED_HISTORY, 'r', encoding='utf-8') as f:
            history = json.load(f)

        today = datetime.date.today()
        settled = []

        for entry in history:
            if entry.get("settled"):
                continue
            pred_date = datetime.date.fromisoformat(entry["date"])
            if (today - pred_date).days < forward_days + 2:  # +2容错周末
                continue

            scores = entry.get("scores", {})
            if len(scores) < 5:
                entry["settled"] = True
                continue

            entry["settled"] = True
            entry["settle_date"] = today.isoformat()
            settled.append({"date": entry["date"], "sectors": len(scores)})

        with open(_SECTOR_PRED_HISTORY, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

        if settled:
            logger.info(f"[板块追踪] 已结算{len(settled)}期板块预测")
        return settled
    except Exception as e:
        logger.warning(f"[板块追踪] 结算失败: {e}")
        return []
