# ETF 风控增强方案 V1.0

## 问题总结

系统在 ETF 交易处理上存在 12 处设计缺口，导致用户将 510210 综指ETF（分散持仓）清仓后，资金全部灌入 562590 半导材料（单一个股），仓位占比达 68.3%，系统全程无预警。

## 修复方案概览

| 优先级 | 修复项 | 涉及文件 | 改动量 |
|--------|--------|----------|--------|
| P0 | 新增 ETF→个股换仓风险拦截 | 新建 `etf_risk_tracker.py` + 改 `capital_planner.py` | ~200行 |
| P0 | ETF 止损参数接入执行路径 | 改 `intraday_monitor.py` + `config.py` | ~50行 |
| P1 | ETF 反洗盘参数差异化 | 改 `intraday_monitor.py` + `config.py` | ~40行 |
| P1 | 仓位管理区分 ETF 子类型 | 改 `portfolio_risk.py` + `config.py` | ~60行 |
| P2 | 市场环境 ETF/个股配置切换 | 改 `action_planner.py` + `config.py` | ~80行 |
| P2 | 梯度减仓阈值适配 ETF | 改 `config.py` | ~20行 |

---

## P0-1: ETF→个股换仓风险拦截（最严重）

### 设计思路

新增 `etf_risk_tracker.py` 模块，追踪"分散→集中"的风险升级：
- 当 ETF 卖出时，记录释放的资金量
- 当后续买入个股时，检查资金来源是否为近期 ETF 清仓
- 如果集中度急剧上升，触发预警并阻止买入

### 新建文件: `trading_system/strategy/etf_risk_tracker.py`

```python
"""
ETF→个股换仓风险追踪器 V1.0

追踪ETF清仓后的资金流向，防止分散仓位集中化为单一个股仓位。
核心规则：
1. ETF卖出后7个交易日内，买入个股金额 > ETF释放金额的80% → 触发预警
2. 单笔买入后单股仓位占比 > 30% → 硬否决（不论资金来源）
3. 连续3次ETF减仓 + 同方向个股加仓 → 触发"风险升级"告警
"""
import json
import os
import datetime
import logging

logger = logging.getLogger(__name__)

# ===== 配置参数 =====
ETF_PROCEEDS_TRACK_WINDOW = 7       # ETF清仓后追踪窗口（交易日）
ETF_PROCEEDS_CONCENTRATION_THRESHOLD = 0.80  # 个股买入占ETF释放资金比例
MAX_SINGLE_STOCK_AFTER_ETF = 0.30   # ETF清仓后单股仓位硬上限
CONSECUTIVE_ETF_REDUCE_THRESHOLD = 3  # 连续ETF减仓次数阈值
RISK_ESCALATION_ALERT = True

STATE_FILE = "data/etf_risk_state.json"


class ETFRiskTracker:
    """ETF换仓风险追踪器"""

    def __init__(self, total_capital: float):
        self.total_capital = total_capital
        self.state = self._load_state()

    def _load_state(self) -> dict:
        """加载持久化状态"""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "etf_proceeds": [],       # [{code, name, date, amount, shares}]
            "etf_reduce_count": 0,    # 连续ETF减仓次数
            "last_etf_reduce_date": None,
            "alerts_sent": [],        # 已发送的预警记录
        }

    def _save_state(self):
        """持久化状态"""
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def record_etf_sell(self, code: str, name: str, amount: float, shares: int):
        """
        记录ETF卖出，追踪释放的资金

        触发时机: 任何ETF卖出成交后调用
        """
        today = datetime.date.today().isoformat()
        entry = {
            "code": code,
            "name": name,
            "date": today,
            "amount": amount,
            "shares": shares,
        }
        self.state["etf_proceeds"].append(entry)

        # 更新连续减仓计数
        if self.state["last_etf_reduce_date"]:
            last_date = datetime.date.fromisoformat(self.state["last_etf_reduce_date"])
            days_gap = (datetime.date.today() - last_date).days
            if days_gap <= 5:  # 5个自然日内算连续
                self.state["etf_reduce_count"] += 1
            else:
                self.state["etf_reduce_count"] = 1
        else:
            self.state["etf_reduce_count"] = 1
        self.state["last_etf_reduce_date"] = today

        self._save_state()
        logger.info(f"[ETFRisk] 记录ETF卖出: {code} {name} 金额{amount:,.0f}元")

        # 检查连续减仓告警
        if self.state["etf_reduce_count"] >= CONSECUTIVE_ETF_REDUCE_THRESHOLD:
            self._alert_consecutive_reduce()

    def check_buy_risk(self, buy_code: str, buy_name: str, buy_amount: float,
                       current_holdings: dict) -> dict:
        """
        检查买入操作的风险等级

        Returns:
            {
                "risk_level": "normal" | "warning" | "critical" | "blocked",
                "reason": str,
                "details": dict,
            }
        """
        from utils.instrument import is_etf, get_instrument_type, InstrumentType

        # 买入ETF不触发（分散→分散是安全的）
        if is_etf(buy_code):
            return {"risk_level": "normal", "reason": "ETF买入不触发换仓追踪"}

        today = datetime.date.today()
        result = {"risk_level": "normal", "reason": "", "details": {}}

        # ===== 检查1: 单股仓位硬上限 =====
        buy_type = get_instrument_type(buy_code)
        current_stock_value = sum(
            h.get("market_value", h.get("shares", 0) * h.get("buy_price", 0))
            for h in current_holdings.values()
            if is_etf(h.get("code", "")) is False and h.get("code", "") != buy_code
        )
        projected_stock_value = current_stock_value + buy_amount
        total_stock_value = self.total_capital  # 简化：用总资金做分母
        projected_ratio = projected_stock_value / total_stock_value if total_stock_value > 0 else 0

        if projected_ratio > MAX_SINGLE_STOCK_AFTER_ETF:
            result["risk_level"] = "blocked"
            result["reason"] = (
                f"买入后个股仓位{projected_ratio:.1%}超过ETF清仓后上限"
                f"{MAX_SINGLE_STOCK_AFTER_ETF:.0%}"
            )
            result["details"]["projected_ratio"] = projected_ratio
            return result

        # ===== 检查2: 资金来源追踪 =====
        recent_proceeds = [
            p for p in self.state["etf_proceeds"]
            if (today - datetime.date.fromisoformat(p["date"])).days
            <= ETF_PROCEEDS_TRACK_WINDOW * 2  # 自然日≈交易日*1.4
        ]
        total_recent_proceeds = sum(p["amount"] for p in recent_proceeds)

        if total_recent_proceeds > 0:
            funding_ratio = buy_amount / total_recent_proceeds
            if funding_ratio >= ETF_PROCEEDS_CONCENTRATION_THRESHOLD:
                result["risk_level"] = "critical"
                result["reason"] = (
                    f"买入金额{buy_amount:,.0f}元占近期ETF释放资金"
                    f"{total_recent_proceeds:,.0f}元的{funding_ratio:.0%}"
                    f"，分散→集中风险升级！"
                )
                result["details"] = {
                    "buy_amount": buy_amount,
                    "etf_proceeds": total_recent_proceeds,
                    "funding_ratio": funding_ratio,
                    "etf_list": [f"{p['code']} {p['name']}" for p in recent_proceeds],
                }
                return result

        # ===== 检查3: 连续减仓+加仓告警 =====
        if self.state["etf_reduce_count"] >= 2:
            result["risk_level"] = "warning"
            result["reason"] = (
                f"近期已连续{self.state['etf_reduce_count']}次ETF减仓，"
                f"现又加仓个股{buy_code} {buy_name}，风险偏好急剧上升"
            )

        return result

    def _alert_consecutive_reduce(self):
        """连续ETF减仓告警"""
        count = self.state["etf_reduce_count"]
        logger.warning(f"[ETFRisk] 连续ETF减仓{count}次，风险偏好上升")

    def cleanup_expired(self):
        """清理过期追踪记录"""
        today = datetime.date.today()
        self.state["etf_proceeds"] = [
            p for p in self.state["etf_proceeds"]
            if (today - datetime.date.fromisoformat(p["date"])).days
            <= ETF_PROCEEDS_TRACK_WINDOW * 2
        ]
        self._save_state()
```

### 修改文件: `trading_system/strategy/capital_planner.py`

在 `check_buy_feasibility()` 函数中（约 line 269），增加 ETF 换仓风险检查：

```python
# 在 check_buy_feasibility() 的循环内，ETF/stock cap check 之后添加：

# ===== V28 ETF换仓风险拦截 =====
try:
    from strategy.etf_risk_tracker import ETFRiskTracker
    _etf_tracker = ETFRiskTracker(total_capital=dashboard.get("total_capital", 0))
    _etf_tracker.cleanup_expired()
    _risk_result = _etf_tracker.check_buy_risk(
        buy_code=code, buy_name=name,
        buy_amount=lv.get("buy_amount", 0) or lv.get("first_buy_amount", 0),
        current_holdings=holdings_raw,
    )
    if _risk_result["risk_level"] == "blocked":
        row["blocked"] = True
        row["block_reason"] = f"[ETF换仓拦截] {_risk_result['reason']}"
    elif _risk_result["risk_level"] in ("critical", "warning"):
        row["etf_risk_warning"] = _risk_result["reason"]
except Exception as _e:
    logger.debug(f"ETF换仓风险检查异常(不阻断): {_e}")
```

### 修改文件: `trading_system/scheduler.py`

在盘中监控循环中，当检测到 ETF 卖出时调用 `record_etf_sell()`：

```python
# 在盘中ETF卖出检测逻辑处（约 line 1800+ 的持仓变动检测），添加：
if is_etf(code) and direction == "sell":
    try:
        from strategy.etf_risk_tracker import ETFRiskTracker
        _tracker = ETFRiskTracker(total_capital=_total_capital)
        _tracker.record_etf_sell(code, name, fill_amount, fill_qty)
    except Exception:
        pass
```

---

## P0-2: ETF 止损参数接入执行路径

### 设计思路

`intraday_monitor.py` 读取 `stop_loss` 时，应根据标的是 ETF 还是股票，使用不同的默认止损比例。

### 修改文件: `trading_system/strategy/intraday_monitor.py`

**修改点 1: line ~607，止损价读取逻辑**

```python
# 原代码:
stop_loss = holding.get("stop_loss", buy_price * (1 - config.INITIAL_STOP_LOSS_PCT))

# 改为:
from utils.instrument import is_etf, get_etf_subtype
if is_etf(code):
    _subtype = get_etf_subtype(code)
    _etf_sl_pct = config.ETF_STRATEGY_PARAMS.get(_subtype, {}).get(
        "stop_loss_pct", config.INITIAL_STOP_LOSS_PCT
    )
    _default_sl_pct = _etf_sl_pct
else:
    _default_sl_pct = config.INITIAL_STOP_LOSS_PCT
stop_loss = holding.get("stop_loss", buy_price * (1 - _default_sl_pct))
```

**修改点 2: line ~3665，holdings 重载时的止损初始化**

```python
# 原代码:
pos["stop_loss"] = pos["buy_price"] * (1 - config.INITIAL_STOP_LOSS_PCT)

# 改为:
from utils.instrument import is_etf, get_etf_subtype
if is_etf(code):
    _subtype = get_etf_subtype(code)
    _sl_pct = config.ETF_STRATEGY_PARAMS.get(_subtype, {}).get(
        "stop_loss_pct", config.INITIAL_STOP_LOSS_PCT
    )
else:
    _sl_pct = config.INITIAL_STOP_LOSS_PCT
pos["stop_loss"] = pos["buy_price"] * (1 - _sl_pct)
```

---

## P1-1: ETF 反洗盘参数差异化

### 设计思路

宽基 ETF 日波动约 0.5%，行业 ETF 约 1.5%，个股约 2-5%。反洗盘参数应按波动率缩放。

### 修改文件: `trading_system/config.py`

在 `ANTI_WASH_CONFIG` 后新增 ETF 专用配置：

```python
# ===== V28 ETF反洗盘参数（按波动率缩放）=====
ETF_ANTI_WASH_CONFIG = {
    "宽基ETF": {
        "buffer_minutes": 5,          # 个股15min → 宽基5min（波动小，确认快）
        "buffer_confirm_pct": 0.005,   # 个股1% → 宽基0.5%
        "v_reversal_pct": 0.02,        # 个股5% → 宽基2%
        "volume_spike_ratio": 1.5,     # 个股2x → 宽基1.5x（ETF量变含义不同）
    },
    "行业ETF": {
        "buffer_minutes": 10,
        "buffer_confirm_pct": 0.008,
        "v_reversal_pct": 0.03,
        "volume_spike_ratio": 1.8,
    },
    "主题ETF": {
        "buffer_minutes": 12,
        "buffer_confirm_pct": 0.008,
        "v_reversal_pct": 0.04,
        "volume_spike_ratio": 1.8,
    },
}
```

### 修改文件: `trading_system/strategy/intraday_monitor.py`

在反洗盘检测逻辑处（line ~670-816），根据标的类型选择参数：

```python
# 在反洗盘参数读取处，添加ETF差异化：
from utils.instrument import is_etf, get_etf_subtype

if is_etf(code):
    _subtype = get_etf_subtype(code)
    _etf_aw = getattr(config, 'ETF_ANTI_WASH_CONFIG', {}).get(_subtype, {})
    _buffer_min = _etf_aw.get("buffer_minutes", ANTI_WASH_CONFIG["buffer_minutes"])
    _confirm_pct = _etf_aw.get("buffer_confirm_pct", ANTI_WASH_CONFIG["buffer_confirm_pct"])
    _v_rev = _etf_aw.get("v_reversal_pct", ANTI_WASH_CONFIG["v_reversal_pct"])
    _vol_spike = _etf_aw.get("volume_spike_ratio", ANTI_WASH_CONFIG["volume_spike_ratio"])
else:
    _buffer_min = ANTI_WASH_CONFIG["buffer_minutes"]
    _confirm_pct = ANTI_WASH_CONFIG["buffer_confirm_pct"]
    _v_rev = ANTI_WASH_CONFIG["v_reversal_pct"]
    _vol_spike = ANTI_WASH_CONFIG["volume_spike_ratio"]
```

---

## P1-2: 仓位管理区分 ETF 子类型

### 设计思路

宽基 ETF（日波动 0.5%）和行业 ETF（日波动 2%）风险差 4 倍，仓位上限应不同。

### 修改文件: `trading_system/config.py`

```python
# 替换现有的 MAX_SINGLE_ETF_RATIO = 0.20 为子类型差异化：
ETF_MAX_RATIO_BY_SUBTYPE = {
    "宽基ETF": 0.25,    # 低波动，可放宽
    "行业ETF": 0.15,    # 中波动，与个股持平
    "主题ETF": 0.12,    # 高波动，更严格
}
```

### 修改文件: `trading_system/utils/instrument.py`

```python
# 修改 get_max_single_position() (line ~196):
def get_max_single_position(code: str) -> float:
    """获取单标的仓位上限"""
    inst_type = get_instrument_type(code)
    if inst_type == InstrumentType.ETF:
        # V28: 按ETF子类型差异化
        from config import ETF_MAX_RATIO_BY_SUBTYPE, get_etf_type
        _subtype = get_etf_type(code)
        return ETF_MAX_RATIO_BY_SUBTYPE.get(
            _subtype,
            getattr(config, 'MAX_SINGLE_ETF_RATIO', 0.20)
        )
    elif inst_type == InstrumentType.BOND:
        return getattr(config, 'MAX_SINGLE_BOND_RATIO', 0.10)
    else:
        return getattr(config, 'MAX_SINGLE_STOCK_RATIO', 0.15)
```

### 修改文件: `trading_system/strategy/portfolio_risk.py`

在 `calc_concentration()` (line ~135) 和 `calc_dynamic_position()` (line ~389) 中，增加 ETF 分类：

```python
# calc_concentration() 中，计算权重时区分ETF/个股：
from utils.instrument import is_etf, get_max_single_position

for code, holding in self.holdings.items():
    weight = market_value / total_mv
    max_cap = get_max_single_position(code)  # 替代硬编码
    if weight > max_cap:
        alerts.append(f"{code} 仓位{weight:.1%}超限{max_cap:.0%}")
```

---

## P2-1: 市场环境 ETF/个股配置切换

### 设计思路

在 BEAR/RANGE 市场中，建议用户从个股切换到宽基 ETF 降低非系统性风险。

### 修改文件: `trading_system/config.py`

```python
# 新增：市场环境下的资产配置建议
REGIME_ETF_ALLOCATION = {
    "BULL": {
        "etf_target_ratio": 0.20,     # 牛市：ETF占比20%
        "stock_target_ratio": 0.70,
        "strategy_hint": "趋势跟踪，个股为主",
    },
    "RANGE": {
        "etf_target_ratio": 0.40,     # 震荡：ETF提升到40%
        "stock_target_ratio": 0.50,
        "strategy_hint": "高抛低吸，增加ETF配置降低选股风险",
    },
    "BEAR": {
        "etf_target_ratio": 0.60,     # 熊市：ETF为主60%
        "stock_target_ratio": 0.30,
        "strategy_hint": "防守为主，ETF替代个股降低非系统性风险",
    },
}
```

### 修改文件: `trading_system/strategy/action_planner.py`

在 `generate_action_plan()` (line ~123) 中，增加 ETF 配置建议：

```python
# 在 regime_action 生成后，添加ETF配置建议：
from config import REGIME_ETF_ALLOCATION

regime = regime_result.get("regime", "RANGE")
_etf_alloc = REGIME_ETF_ALLOCATION.get(regime, REGIME_ETF_ALLOCATION["RANGE"])

# 计算当前ETF占比
from utils.instrument import is_etf
_current_etf_value = sum(
    h.get("market_value", 0) for h in holdings.values() if is_etf(h.get("code", ""))
)
_current_etf_ratio = _current_etf_value / total_capital if total_capital > 0 else 0

# 生成ETF调仓建议
if _current_etf_ratio < _etf_alloc["etf_target_ratio"] * 0.8:
    plan["etf_recommendation"] = {
        "action": "increase",
        "current_ratio": _current_etf_ratio,
        "target_ratio": _etf_alloc["etf_target_ratio"],
        "hint": _etf_alloc["strategy_hint"],
    }
elif _current_etf_ratio > _etf_alloc["etf_target_ratio"] * 1.2:
    plan["etf_recommendation"] = {
        "action": "decrease",
        "current_ratio": _current_etf_ratio,
        "target_ratio": _etf_alloc["etf_target_ratio"],
    }
```

---

## P2-2: 梯度减仓阈值适配 ETF

### 修改文件: `trading_system/config.py`

```python
# 新增：ETF专用梯度减仓阈值（按波动率缩放）
ETF_GRADIENT_REDUCE_CONFIG = {
    "宽基ETF": {
        "levels": [
            {"loss_pct": -0.05, "reduce_ratio": 0.25},   # -5%减1/4（≈10个交易日）
            {"loss_pct": -0.08, "reduce_ratio": 0.50},   # -8%减1/2
            {"loss_pct": -0.12, "reduce_ratio": 1.0},    # -12%清仓
        ],
    },
    "行业ETF": {
        "levels": [
            {"loss_pct": -0.07, "reduce_ratio": 0.25},
            {"loss_pct": -0.10, "reduce_ratio": 0.50},
            {"loss_pct": -0.15, "reduce_ratio": 1.0},
        ],
    },
    "主题ETF": {
        "levels": [
            {"loss_pct": -0.08, "reduce_ratio": 0.25},
            {"loss_pct": -0.12, "reduce_ratio": 0.50},
            {"loss_pct": -0.15, "reduce_ratio": 1.0},
        ],
    },
}
```

---

## 实施顺序

1. **Phase 1 (P0, 1-2天):**
   - 新建 `etf_risk_tracker.py`
   - 修改 `capital_planner.py` 接入换仓拦截
   - 修改 `intraday_monitor.py` 接入 ETF 止损参数

2. **Phase 2 (P1, 1天):**
   - 新增 `ETF_ANTI_WASH_CONFIG`
   - 修改 `intraday_monitor.py` 反洗盘差异化
   - 新增 `ETF_MAX_RATIO_BY_SUBTYPE`
   - 修改 `instrument.py` 仓位上限

3. **Phase 3 (P2, 1天):**
   - 新增 `REGIME_ETF_ALLOCATION`
   - 修改 `action_planner.py` 配置建议
   - 新增 `ETF_GRADIENT_REDUCE_CONFIG`

## 测试验证

1. **换仓拦截测试:** 模拟卖出 510210 后买入 562590，验证是否触发 blocked/critical
2. **止损参数测试:** 验证宽基 ETF 止损为 12%，行业 ETF 为 8%
3. **反洗盘测试:** 验证宽基 ETF 使用 5min buffer 而非 15min
4. **仓位上限测试:** 验证宽基 ETF 上限 25%，行业 ETF 上限 15%
5. **回归测试:** 验证个股交易逻辑不受影响
