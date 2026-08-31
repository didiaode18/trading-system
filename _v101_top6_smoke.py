"""V10.1 Top6: 护城河代理指标冒烟测试"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_system.strategy.moat import (
    _score_roe_stability, _score_margin_trend, _score_market_position,
    compute_moat_score, get_moat_bonus, reset_moat_cache
)

# === 测试1: ROE稳定性评分 ===
print("=" * 50)
print("Test 1: ROE Stability Scoring")
print("=" * 50)

# 极稳定 ROE (std < 3%)
score = _score_roe_stability([15.0, 15.5, 16.0, 15.8, 15.2])
assert score == 40, f"Expected 40, got {score}"
print(f"  Stable ROE (std~0.4): score={score} PASS")

# 中等稳定 (std 3-5%)
score = _score_roe_stability([10.0, 15.0, 20.0, 12.0, 18.0])
assert score in (20, 30), f"Expected 20 or 30, got {score}"
print(f"  Medium ROE (std~4): score={score} PASS")

# 高波动 (std > 8%)
score = _score_roe_stability([5.0, 25.0, 3.0, 30.0, 2.0])
assert score == 10, f"Expected 10, got {score}"
print(f"  Volatile ROE (std~13): score={score} PASS")

# 数据不足
score = _score_roe_stability([15.0])
assert score == 10
print(f"  Insufficient data: score={score} PASS")

print("ALL ROE TESTS PASS\n")

# === 测试2: 毛利率趋势评分 ===
print("=" * 50)
print("Test 2: Margin Trend Scoring")
print("=" * 50)

# 上升趋势
score = _score_margin_trend([35.0, 36.0, 37.0, 38.0, 39.0, 40.0])
assert score == 35, f"Expected 35, got {score}"
print(f"  Rising margin: score={score} PASS")

# 平稳
score = _score_margin_trend([40.0, 39.5, 40.5, 39.0, 40.0, 40.5])
assert score == 25, f"Expected 25, got {score}"
print(f"  Stable margin: score={score} PASS")

# 下降趋势
score = _score_margin_trend([45.0, 43.0, 41.0, 39.0, 37.0, 35.0])
assert score == 10, f"Expected 10, got {score}"
print(f"  Declining margin: score={score} PASS")

print("ALL MARGIN TESTS PASS\n")

# === 测试3: 行业地位评分 ===
print("=" * 50)
print("Test 3: Market Position Scoring")
print("=" * 50)

score = _score_market_position(0.10)
assert score == 25
print(f"  Top 10%: score={score} PASS")

score = _score_market_position(0.30)
assert score == 18
print(f"  Top 30%: score={score} PASS")

score = _score_market_position(0.50)
assert score == 10
print(f"  Middle 50%: score={score} PASS")

score = _score_market_position(0.80)
assert score == 5
print(f"  Bottom 20%: score={score} PASS")

print("ALL POSITION TESTS PASS\n")

# === 测试4: 综合评分 ===
print("=" * 50)
print("Test 4: Comprehensive Moat Score")
print("=" * 50)

reset_moat_cache()
# 强护城河: ROE稳定 + 毛利率上升 + 行业龙头
fund_strong = {
    "roe_history": [18.0, 18.5, 19.0, 18.8, 18.2],
    "gross_margin_history": [40.0, 41.0, 42.0, 43.0, 44.0, 45.0],
}
result = compute_moat_score("STRONG", fund_data=fund_strong, rps_rank=0.10)
print(f"  Strong moat: score={result['moat_score']} bonus={result['bonus']}")
assert result['bonus'] >= 2, f"Expected bonus>=2, got {result['bonus']}"
print(f"  PASS")

# 弱护城河: ROE波动 + 毛利率下降 + 行业落后
fund_weak = {
    "roe_history": [5.0, 25.0, 3.0, 30.0],
    "gross_margin_history": [45.0, 40.0, 35.0, 30.0],
}
result = compute_moat_score("WEAK", fund_data=fund_weak, rps_rank=0.80)
print(f"  Weak moat: score={result['moat_score']} bonus={result['bonus']}")
assert result['bonus'] == 0, f"Expected bonus=0, got {result['bonus']}"
print(f"  PASS")

# 无数据
result = compute_moat_score("NODATA", fund_data={}, rps_rank=None)
print(f"  No data: score={result['moat_score']} bonus={result['bonus']}")
assert result['bonus'] == 0
print(f"  PASS")

print("ALL COMPREHENSIVE TESTS PASS\n")

# === 测试5: get_moat_bonus便捷接口 ===
print("=" * 50)
print("Test 5: get_moat_bonus Interface")
print("=" * 50)

reset_moat_cache()
bonus = get_moat_bonus("TEST1", fund_data=fund_strong, rps_rank=0.10)
assert bonus >= 2
print(f"  Strong moat bonus: {bonus} PASS")

bonus = get_moat_bonus("TEST2", fund_data=fund_weak, rps_rank=0.80)
assert bonus == 0
print(f"  Weak moat bonus: {bonus} PASS")

print("ALL INTERFACE TESTS PASS\n")

print("=" * 50)
print("=== ALL 5 SMOKE TESTS PASSED ===")
print("=" * 50)
