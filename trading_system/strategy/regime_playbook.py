# -*- coding: utf-8 -*-
"""
市场状态策略手册 (Regime Playbook) V10.0
=========================================
根据 MarketRegime 检测结果（BULL/BEAR/RANGE）提供统一的策略配置接口，
消除各模块各自判断regime的碎片化，实现"一套规则、全局生效"。

核心职责:
  1. 选股权重: 不同regime下五层评分的权重不同
  2. 仓位限制: 总仓位/单只仓位的上下限
  3. 板块偏好: 进攻型/均衡型/防御型
  4. 风控规则: 止损幅度、移动止盈、追涨许可
  5. 行动模式: 持仓加仓/高抛低吸/减仓防守
  6. 通过门槛: 推荐池/核心池的最低评分

使用方式:
    from strategy.regime_playbook import RegimePlaybook
    playbook = RegimePlaybook(regime_result)
    weights = playbook.score_weights()
    limits = playbook.position_limits()

设计原则:
  - 所有参数集中管理，禁止在recommend_engine/daily_orders中硬编码regime逻辑
  - regime_result加载失败时默认RANGE（最保守的中间态）
  - 任何异常不抛出，降级到默认配置
"""

import logging
import json
import os
import copy
import datetime

logger = logging.getLogger(__name__)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（返回新字典）：字典逐键递归，非字典直接覆盖。

    仅用于 RegimePlaybook 调优覆盖，保证未覆盖的键沿用基线值。
    """
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class RegimePlaybook:
    """
    市场状态策略手册

    职责:
      根据 MarketRegime 检测结果（BULL/BEAR/RANGE）提供统一的策略配置接口，
      消除各模块各自判断regime的碎片化，实现“一套规则、全局生效”。

    核心配置维度（每种状态完整定义）:
      1. score_weights: 选股五层评分权重（趋势/动量/基本面/赛道/买点）
      2. position_limits: 总仓位/单只仓位上下限
      3. sector_preference: 板块偏好（进攻/均衡/防御）
      4. risk_rules: 风控规则（止损/移动止盈/追涨许可）
      5. action_mode: 行动模式（持仓加仓/高抛低吸/减仓防守）
      6. pass_threshold: 推荐池/核心池最低评分

    关键依赖:
      - regime_result: MarketRegimeDetector.detect() 返回
      - UnifiedRiskEngine: position_limits() 取更严格值（P1-G）

    设计原则:
      - 所有参数集中管理，禁止在recommend_engine/daily_orders中硬编码regime逻辑
      - regime_result加载失败时默认RANGE（最保守的中间态）
    """

    # ============================================================
    # 三种市场状态的完整策略配置
    # ============================================================
    CONFIGS = {
        "BULL": {
            # 选股五层权重: 趋势+动量优先
            "score_weights": {
                "L2_赛道": 0.20,
                "L3_基本面": 0.10,
                "L4_趋势": 0.45,
                "L5_买点": 0.15,
                # V9.0动量衰减项并入L5
                "momentum_decay": 0.10,
            },
            # 仓位限制
            "position_limits": {
                "max_total": 0.90,   # 总仓位上限90%
                "min_total": 0.70,   # 总仓位下限70%（牛市保持仓位）
                "max_single": 0.20,  # 单只上限20%
            },
            # 板块偏好: 进攻型
            "sector_preference": {
                "mode": "aggressive",           # 进攻模式
                "prefer_momentum_top3": True,    # 优先板块动量TOP3
                "avoid_defensive": False,        # 不回避防御板块
                "defensive_sectors": [],         # 无防御板块要求
                "non_defensive_penalty": 1.0,    # 非防御板块无惩罚
            },
            # 风控规则
            "risk_rules": {
                "stop_loss_pct": 0.08,    # 止损8%（标准）
                "trailing_stop": True,     # 启用移动止盈
                "trailing_trigger": 0.15,  # 浮盈15%启动移动止盈
                "trailing_drawdown": 0.05, # 从高点回落5%止盈
                "allow_chase": True,       # 允许追强势股
                "allow_add_position": True,  # 允许加仓
            },
            # 行动模式
            "action_mode": "hold_and_add",  # 持仓+加仓
            "action_mode_cn": "牛市进攻 — 持股待涨，回踩加仓",

            # 通过门槛
            "pass_threshold": 50,           # 推荐池最低分
            "core_pool_threshold": 55,      # 核心池最低分

            # 纪律锁附加条目
            "discipline_extra": [
                "✅ 牛市策略: 持股为主，回踩MA20加仓",
                "✅ 牛市策略: 止损上移保护利润",
                "✅ 牛市策略: 集中配置强势板块龙头",
            ],

            # 报告标题
            "report_title": "📈 牛市进攻模式",
            "report_summary": "趋势向上，重仓持有龙头，回踩是加仓良机",
        },

        "RANGE": {
            "score_weights": {
                "L2_赛道": 0.25,
                "L3_基本面": 0.15,
                "L4_趋势": 0.35,
                "L5_买点": 0.15,
                "momentum_decay": 0.10,  # 与当前硬编码一致
            },
            "position_limits": {
                "max_total": 0.60,
                "min_total": 0.30,
                "max_single": 0.15,
            },
            "sector_preference": {
                "mode": "balanced",
                "prefer_momentum_top3": False,
                "avoid_defensive": False,
                "defensive_sectors": [],
                "non_defensive_penalty": 1.0,
            },
            "risk_rules": {
                "stop_loss_pct": 0.06,
                "trailing_stop": False,
                "trailing_trigger": 0.15,
                "trailing_drawdown": 0.05,
                "allow_chase": False,
                "allow_add_position": False,
            },
            "action_mode": "swing_trade",
            "action_mode_cn": "震荡平衡 — 高抛低吸，控制仓位",

            "pass_threshold": 55,
            "core_pool_threshold": 60,

            "discipline_extra": [
                "✅ 震荡策略: 半仓操作，高抛低吸",
                "✅ 震荡策略: 布林带上轨减仓、下轨加仓",
                "🚫 震荡策略: 禁止追涨，只接受缩量回踩买点",
            ],

            "report_title": "⚖️ 震荡平衡模式",
            "report_summary": "方向不明，半仓操作，关注板块轮动机会",
        },

        "BEAR": {
            "score_weights": {
                "L2_赛道": 0.15,
                "L3_基本面": 0.25,   # 熊市基本面质量更重要
                "L4_趋势": 0.25,
                "L5_买点": 0.35,     # 熊市买点性价比最重要
                "momentum_decay": 0.00,
            },
            "position_limits": {
                "max_total": 0.30,
                "min_total": 0.00,
                "max_single": 0.10,
            },
            "sector_preference": {
                "mode": "defensive",
                "prefer_momentum_top3": False,
                "avoid_defensive": True,  # 回避非防御板块
                "defensive_sectors": ["大消费", "公用事业", "医药生物", "医药", "银行", "食品饮料"],
                "non_defensive_penalty": 0.80,  # 非防御板块评分打8折
            },
            "risk_rules": {
                "stop_loss_pct": 0.05,    # 止损5%（极紧）
                "trailing_stop": False,
                "trailing_trigger": 0.10,
                "trailing_drawdown": 0.03,
                "allow_chase": False,
                "allow_add_position": False,  # 禁止加仓
            },
            "action_mode": "reduce_and_defend",
            "action_mode_cn": "熊市防守 — 减仓至3成以下，只保留防御仓",

            "pass_threshold": 65,   # 熊市通过门槛大幅提高
            "core_pool_threshold": 70,

            "discipline_extra": [
                "🚫 熊市模式: 禁止抄底非防御板块",
                "🚫 熊市模式: 单只仓位不超过10%",
                "🚫 熊市模式: 总仓位不超过30%",
                "🚫 熊市模式: 禁止加仓，只减不加",
                "✅ 熊市策略: 只保留防御板块核心仓，现金为王",
            ],

            "report_title": "📉 熊市防守模式",
            "report_summary": "趋势向下，大幅减仓至3成以下，只保留防御板块",
        },
    }

    # 默认配置（regime不可用时使用RANGE）
    DEFAULT_STATE = "RANGE"

    def __init__(self, regime_result: dict = None):
        """
        参数:
            regime_result: MarketRegimeDetector.detect() 或 detect_with_ensemble() 的返回
                          None或异常时降级为RANGE
        """
        self._raw = regime_result or {}
        self.state = self._resolve_state()
        # P1-2(路线图): 先取基线配置（深拷贝，禁止污染类级 CONFIGS），
        #   再按 config.REGIME_PLAYBOOK_TUNING 对当前状态做增量覆盖（仅覆盖出现的键）。
        #   将调优块置空即回退基线，便于 A/B 归因。
        base = self.CONFIGS.get(self.state, self.CONFIGS[self.DEFAULT_STATE])
        self.config = copy.deepcopy(base)
        self._apply_tuning()
        self.confidence = self._raw.get("confidence", 0.5)
        self.detail = self._raw.get("detail", "")

        logger.info(f"[Playbook] 市场状态={self.state}, 置信度={self.confidence:.0%}, "
                    f"行动模式={self.config['action_mode_cn']}")

    def _apply_tuning(self):
        """应用 config.REGIME_PLAYBOOK_TUNING 中当前状态的增量覆盖（仅浅层字典递归合并）。

        任何异常不抛出，降级为基线配置（遵循"任何异常不抛出"设计原则）。
        """
        try:
            import config as _cfg
            tuning = getattr(_cfg, "REGIME_PLAYBOOK_TUNING", {}) or {}
            override = tuning.get(self.state)
            if not isinstance(override, dict):
                return
            self.config = _deep_merge(self.config, override)
            self.config["_tuned"] = True
        except Exception as e:
            logger.debug(f"[Playbook] 调优覆盖失败，降级基线: {e}")

    def _resolve_state(self) -> str:
        """从regime_result中提取状态，异常时降级为RANGE"""
        try:
            state = self._raw.get("state", "")
            if state in self.CONFIGS:
                return state
            # 兼容小写regime字段
            regime = self._raw.get("regime", "")
            if regime:
                state_upper = regime.upper()
                if state_upper in self.CONFIGS:
                    return state_upper
        except Exception:
            pass
        return self.DEFAULT_STATE

    # ============================================================
    # 公共接口
    # ============================================================

    def score_weights(self) -> dict:
        """获取当前regime的选股评分权重"""
        return self.config["score_weights"]

    def position_limits(self) -> dict:
        """获取当前regime的仓位限制
        FIX P1-G: 与UnifiedRiskEngine仓位限制对齐，取更严格的一方
        返回: {"max_total": float, "min_total": float, "max_single": float}
        """
        base = self.config["position_limits"].copy()
        # P1-G / FIX P1-02: 尝试读取风控引擎的仓位限制，取更严格的一方。
        # 旧实现用宽泛 `except: pass` 吞掉了 get_position_limits() 不存在的
        # AttributeError → “与风控引擎取更严值”从未生效。现将导入异常与
        # 调用异常分开：导入失败（ImportError）静默降级；调用异常记日志不静默吞。
        try:
            from risk.risk_control import UnifiedRiskEngine
        except ImportError:
            return base  # 风控模块不可用，降级用 playbook 默认值
        try:
            _engine = UnifiedRiskEngine()
            _risk_limits = _engine.get_position_limits()
            if _risk_limits:
                # 取更严格的限制
                _risk_max_total = _risk_limits.get("max_total", base["max_total"])
                _risk_max_single = _risk_limits.get("max_single", base["max_single"])
                if _risk_max_total < base["max_total"]:
                    base["max_total"] = _risk_max_total
                    base["_risk_constrained"] = True
                if _risk_max_single < base["max_single"]:
                    base["max_single"] = _risk_max_single
                    base["_risk_constrained"] = True
        except Exception as e:
            logger.warning(f"[Playbook] 读取风控仓位限制失败，降级用 playbook 默认值: {e}")
        return base

    def sector_preference(self) -> dict:
        """获取当前regime的板块偏好配置"""
        return self.config["sector_preference"]

    def risk_rules(self) -> dict:
        """获取当前regime的风控规则"""
        return self.config["risk_rules"]

    def action_mode(self) -> str:
        """获取当前行动模式: hold_and_add / swing_trade / reduce_and_defend"""
        return self.config["action_mode"]

    def action_mode_cn(self) -> str:
        """获取行动模式中文描述"""
        return self.config["action_mode_cn"]

    def pass_threshold(self) -> int:
        """推荐池最低通过分数"""
        return self.config["pass_threshold"]

    def core_pool_threshold(self) -> int:
        """核心池最低分数"""
        return self.config["core_pool_threshold"]

    def defensive_sectors(self) -> list:
        """获取防御板块列表（BEAR模式有效）"""
        return self.config["sector_preference"].get("defensive_sectors", [])

    def is_defensive_sector(self, sector_name: str) -> bool:
        """判断给定板块是否为防御板块"""
        if not sector_name:
            return False
        defensive = self.defensive_sectors()
        # 模糊匹配: "医药生物" 匹配 "医药"
        for ds in defensive:
            if ds in sector_name or sector_name in ds:
                return True
        return False

    def should_reduce_position(self, current_position_pct: float) -> bool:
        """判断是否需要减仓"""
        max_total = self.position_limits()["max_total"]
        return current_position_pct > max_total + 0.05  # 超目标5%以上才触发

    def allow_chase(self) -> bool:
        """是否允许追涨"""
        return self.config["risk_rules"].get("allow_chase", False)

    def allow_add_position(self) -> bool:
        """是否允许加仓"""
        return self.config["risk_rules"].get("allow_add_position", False)

    def trailing_stop_enabled(self) -> bool:
        """是否启用移动止盈"""
        return self.config["risk_rules"].get("trailing_stop", False)

    def mean_reversion(self) -> dict:
        """P1-2(路线图): 获取均值回归子逻辑配置（仅 RANGE 调优时非空）。

        返回 {enabled, bollinger_window, bollinger_std, upper_reduce_pct,
              lower_add_pct, prefer_reversal_factor}；未启用时 enabled=False。
        供下游高抛低吸子逻辑复用（布林带上下轨），不新建引擎。
        """
        mr = self.config.get("mean_reversion", {})
        if not isinstance(mr, dict):
            return {"enabled": False}
        return mr

    def discipline_extra(self) -> list:
        """获取额外纪律锁条目"""
        return self.config.get("discipline_extra", [])

    def report_title(self) -> str:
        """获取报告标题"""
        return self.config.get("report_title", "⚖️ 震荡模式")

    def report_summary(self) -> str:
        """获取报告摘要"""
        return self.config.get("report_summary", "")

    # ============================================================
    # 综合行动摘要（给报告/邮件使用）
    # ============================================================

    def generate_regime_action_summary(self) -> str:
        """生成regime行动摘要文本"""
        state = self.state
        conf = self.confidence
        mode_cn = self.action_mode_cn()
        limits = self.position_limits()
        rules = self.risk_rules()

        summary_parts = [
            f"【{mode_cn}】",
            f"置信度{conf:.0%}",
            f"仓位目标{limits['min_total']:.0%}-{limits['max_total']:.0%}",
            f"止损{rules['stop_loss_pct']:.0%}",
        ]

        if state == "BEAR":
            defensive = ", ".join(self.defensive_sectors()[:4])
            summary_parts.append(f"仅防御板块({defensive})")
        elif state == "BULL":
            summary_parts.append("集中强势板块龙头")

        return " | ".join(summary_parts)

    # ============================================================
    # 序列化（供报告/日志使用）
    # ============================================================

    def to_dict(self) -> dict:
        """导出为字典（供JSON序列化）"""
        return {
            "state": self.state,
            "confidence": self.confidence,
            "action_mode": self.action_mode(),
            "action_mode_cn": self.action_mode_cn(),
            "position_limits": self.position_limits(),
            "risk_rules": self.risk_rules(),
            "pass_threshold": self.pass_threshold(),
            "core_pool_threshold": self.core_pool_threshold(),
            "defensive_sectors": self.defensive_sectors(),
            "detail": self.detail,
            "summary": self.generate_regime_action_summary(),
        }


# ============================================================
# 门面函数: 快速创建playbook（带缓存）
# ============================================================
_PLAYBOOK_CACHE = {"date": None, "playbook": None}


def get_regime_playbook(regime_result: dict = None) -> RegimePlaybook:
    """获取当日regime playbook（进程级缓存）

    参数:
        regime_result: regime检测结果，None时自动从detect_market_regime获取

    返回:
        RegimePlaybook实例，任何异常降级为RANGE
    """
    today = datetime.date.today().isoformat()

    # 缓存命中
    if _PLAYBOOK_CACHE["date"] == today and _PLAYBOOK_CACHE["playbook"] is not None:
        if regime_result is None:
            return _PLAYBOOK_CACHE["playbook"]

    # 自动加载regime
    if regime_result is None:
        try:
            from strategy.market_regime import detect_market_regime
            regime_result = detect_market_regime()
        except Exception as e:
            logger.warning(f"[Playbook] regime自动加载失败，降级RANGE: {e}")
            regime_result = {}

    try:
        playbook = RegimePlaybook(regime_result)
    except Exception as e:
        logger.warning(f"[Playbook] 创建失败，降级RANGE: {e}")
        playbook = RegimePlaybook({})

    # 写入缓存（仅自动加载时）
    if regime_result:
        _PLAYBOOK_CACHE["date"] = today
        _PLAYBOOK_CACHE["playbook"] = playbook

    return playbook


# ============================================================
# 独立测试
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("=" * 50)
    print("  RegimePlaybook 测试")
    print("=" * 50)

    # 测试三种状态
    for state in ["BULL", "RANGE", "BEAR"]:
        pb = RegimePlaybook({"state": state, "confidence": 0.75})
        print(f"\n--- {state} ---")
        print(f"  行动模式: {pb.action_mode_cn()}")
        print(f"  仓位限制: {pb.position_limits()}")
        print(f"  止损幅度: {pb.risk_rules()['stop_loss_pct']:.0%}")
        print(f"  通过门槛: {pb.pass_threshold()}分")
        print(f"  防御板块: {pb.defensive_sectors()}")
        print(f"  摘要: {pb.generate_regime_action_summary()}")

    # 测试降级
    pb_default = RegimePlaybook(None)
    print(f"\n--- 降级测试(None) ---")
    print(f"  状态: {pb_default.state} (应为RANGE)")

    pb_empty = RegimePlaybook({})
    print(f"\n--- 降级测试({{}}) ---")
    print(f"  状态: {pb_empty.state} (应为RANGE)")

    print("\n[OK] RegimePlaybook 测试完成")
