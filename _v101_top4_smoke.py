"""V10.1 Top4: 结构性行情检测冒烟测试"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from trading_system.strategy.stock_screener import _detect_structural_market

# === 场景1: 结构性行情（指数横盘 + 高分化 + mixed regime） ===
np.random.seed(42)
data_struct = {}
# 指数: 横盘（5日涨跌<3%）
dates = pd.date_range('2026-01-01', periods=60)
idx_close = 100 * np.cumprod(1 + np.random.normal(0, 0.002, 60))
data_struct['000300'] = pd.DataFrame({
    'close': idx_close, 'high': idx_close * 1.01, 'low': idx_close * 0.99,
    'volume': np.random.randint(1000, 10000, 60), 'open': idx_close
}, index=dates)

# 强势板块: 大涨
for i in range(15):
    code = f'600{i:03d}'
    close = 10 * np.cumprod(1 + np.random.normal(0.01, 0.01, 60))
    data_struct[code] = pd.DataFrame({
        'close': close, 'high': close * 1.01, 'low': close * 0.99,
        'volume': np.random.randint(1000, 10000, 60), 'open': close
    }, index=dates)

# 弱势板块: 大跌
for i in range(15):
    code = f'002{i:03d}'
    close = 10 * np.cumprod(1 + np.random.normal(-0.008, 0.01, 60))
    data_struct[code] = pd.DataFrame({
        'close': close, 'high': close * 1.01, 'low': close * 0.99,
        'volume': np.random.randint(1000, 10000, 60), 'open': close
    }, index=dates)

regime_mixed = {"regime": "mixed", "breadth": 50}
result = _detect_structural_market(data_struct, regime_mixed)
print(f"[场景1] structural={result['structural']} dispersion={result['dispersion']:.2f} pos_adj={result['position_adj']:.2f}")
print(f"  detail: {result['detail']}")
assert result['structural'] == True, f"Expected structural=True, got {result['structural']}"
assert result['position_adj'] < 1.0, f"Expected pos_adj<1.0, got {result['position_adj']}"
print("  PASS")

# === 场景2: 趋势行情（指数大涨，不应判定为结构性） ===
data_trend = {}
idx_close2 = 100 * np.cumprod(1 + np.random.normal(0.01, 0.005, 60))
data_trend['000300'] = pd.DataFrame({
    'close': idx_close2, 'high': idx_close2 * 1.01, 'low': idx_close2 * 0.99,
    'volume': np.random.randint(1000, 10000, 60), 'open': idx_close2
}, index=dates)
for i in range(30):
    code = f'600{i:03d}'
    close = 10 * np.cumprod(1 + np.random.normal(0.008, 0.005, 60))
    data_trend[code] = pd.DataFrame({
        'close': close, 'high': close * 1.01, 'low': close * 0.99,
        'volume': np.random.randint(1000, 10000, 60), 'open': close
    }, index=dates)

regime_trend = {"regime": "trend_following", "breadth": 70}
result2 = _detect_structural_market(data_trend, regime_trend)
print(f"[场景2] structural={result2['structural']} dispersion={result2['dispersion']:.2f}")
assert result2['structural'] == False, "Trend market should NOT be structural"
assert result2['position_adj'] == 1.0
print("  PASS")

# === 场景3: 数据不足 ===
result3 = _detect_structural_market({}, {"regime": "mixed", "breadth": 50})
print(f"[场景3] structural={result3['structural']} (empty data)")
assert result3['structural'] == False
print("  PASS")

# === 场景4: 低分化（所有股票涨跌一致） ===
data_low = {}
idx_close3 = 100 * np.cumprod(1 + np.random.normal(0, 0.001, 60))
data_low['000300'] = pd.DataFrame({
    'close': idx_close3, 'high': idx_close3 * 1.01, 'low': idx_close3 * 0.99,
    'volume': np.random.randint(1000, 10000, 60), 'open': idx_close3
}, index=dates)
for i in range(30):
    code = f'600{i:03d}'
    # 所有股票涨跌几乎一致（低分化）
    close = 10 * np.cumprod(1 + np.random.normal(0.001, 0.001, 60))
    data_low[code] = pd.DataFrame({
        'close': close, 'high': close * 1.01, 'low': close * 0.99,
        'volume': np.random.randint(1000, 10000, 60), 'open': close
    }, index=dates)

regime_mixed2 = {"regime": "mixed", "breadth": 50}
result4 = _detect_structural_market(data_low, regime_mixed2)
print(f"[场景4] structural={result4['structural']} dispersion={result4['dispersion']:.2f}")
assert result4['structural'] == False, "Low dispersion should NOT be structural"
print("  PASS")

# === 场景5: 非mixed regime不应判定为结构性 ===
result5 = _detect_structural_market(data_struct, {"regime": "trend_following", "breadth": 65})
print(f"[场景5] structural={result5['structural']} (trend regime, even with high dispersion)")
assert result5['structural'] == False, "Non-mixed regime should NOT be structural"
print("  PASS")

print("\n=== ALL 5 SMOKE TESTS PASSED ===")
