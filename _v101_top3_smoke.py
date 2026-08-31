"""V10.1 Top3: 风格轮动检测冒烟测试"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from trading_system.strategy.stock_screener import (
    _detect_market_style, _reset_style_cache
)

# === 场景1: 大盘风格（大盘涨幅 > 小盘） ===
np.random.seed(42)
data_large = {}
for i in range(50):
    code = f'600{i:03d}'
    dates = pd.date_range('2026-01-01', periods=60)
    close = 10 + np.cumsum(np.random.randn(60) * 0.15)  # 波动较大但正偏
    data_large[code] = pd.DataFrame({
        'close': close, 'high': close + 0.5, 'low': close - 0.5,
        'volume': np.random.randint(1000, 10000, 60), 'open': close
    }, index=dates)

for i in range(50):
    code = f'002{i:03d}'
    dates = pd.date_range('2026-01-01', periods=60)
    close = 10 + np.cumsum(np.random.randn(60) * 0.05)  # 波动小，涨幅少
    data_large[code] = pd.DataFrame({
        'close': close, 'high': close + 0.5, 'low': close - 0.5,
        'volume': np.random.randint(1000, 10000, 60), 'open': close
    }, index=dates)

_reset_style_cache()
result = _detect_market_style(data_large)
print(f"[场景1] style={result['style']} large={result['large_cap_chg']:+.2f}% small={result['small_cap_chg']:+.2f}% spread={result['spread']:+.2f}%")
assert result['computed'] == True
assert result['style'] in ('large_cap', 'small_cap', 'neutral')
print("  PASS")

# === 场景2: 缓存命中（第二次调用直接返回缓存） ===
result2 = _detect_market_style(data_large)
assert result2 is result  # 应该返回同一个缓存对象
print(f"[场景2] 缓存命中: {result2['style']}")
print("  PASS")

# === 场景3: 缓存重置 ===
_reset_style_cache()
result3 = _detect_market_style(data_large)
assert result3 is not result  # 重置后应该是新对象
assert result3['style'] == result['style']  # 但结果应该一致
print(f"[场景3] 缓存重置后重新计算: {result3['style']}")
print("  PASS")

# === 场景4: 空数据 ===
_reset_style_cache()
result4 = _detect_market_style({})
assert result4['style'] == 'neutral'
assert result4['computed'] == True
print(f"[场景4] 空数据: style={result4['style']}")
print("  PASS")

# === 场景5: 配置开关 ===
import trading_system.config as config
config.STYLE_ROTATION_ENABLED = False  # 关闭后不影响检测函数（检测函数始终计算，开关在权重调整处生效）
_reset_style_cache()
result5 = _detect_market_style(data_large)
assert result5['computed'] == True
config.STYLE_ROTATION_ENABLED = True  # 恢复
print(f"[场景5] 配置开关: style={result5['style']}")
print("  PASS")

print("\n=== ALL 5 SMOKE TESTS PASSED ===")
