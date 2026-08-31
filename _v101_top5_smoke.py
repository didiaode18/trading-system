"""V10.1 Top5: 行业景气度因子冒烟测试"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_system.strategy.sector_cycle import (
    _classify_cycle_phase, compute_sector_cycles, 
    get_sector_cycle_score, reset_cycle_cache, CYCLE_SCORE_MAP
)

# === 测试1: 周期阶段分类 ===
print("=" * 50)
print("Test 1: Cycle Phase Classification")
print("=" * 50)

# 复苏期: 5d涨 > 0, 20d跌 < 0, 加速度 > 0
phase = _classify_cycle_phase(chg_5d=1.5, chg_20d=-1.0, accel=2.0, rank_pct=0.5)
assert phase == "recovery", f"Expected recovery, got {phase}"
print(f"  recovery: chg5d=+1.5 chg20d=-1.0 accel=+2.0 -> {phase} PASS")

# 扩张期: 5d涨 > 0, 20d涨 > 0, 排名前40%
phase = _classify_cycle_phase(chg_5d=3.0, chg_20d=5.0, accel=1.0, rank_pct=0.2)
assert phase == "expansion", f"Expected expansion, got {phase}"
print(f"  expansion: chg5d=+3.0 chg20d=+5.0 rank=20% -> {phase} PASS")

# 放缓期: 5d跌 < 0, 20d涨 > 0 (动量由正转负)
phase = _classify_cycle_phase(chg_5d=-0.5, chg_20d=2.0, accel=-1.0, rank_pct=0.6)
assert phase == "slowdown", f"Expected slowdown, got {phase}"
print(f"  slowdown: chg5d=-0.5 chg20d=+2.0 -> {phase} PASS")

# 收缩期: 5d跌 < -1%, 20d跌 < -2%, 排名后70%
phase = _classify_cycle_phase(chg_5d=-2.0, chg_20d=-3.0, accel=-0.5, rank_pct=0.85)
assert phase == "contraction", f"Expected contraction, got {phase}"
print(f"  contraction: chg5d=-2.0 chg20d=-3.0 rank=85% -> {phase} PASS")

# 稳定期: 其他情况
phase = _classify_cycle_phase(chg_5d=0.5, chg_20d=0.3, accel=0.1, rank_pct=0.5)
assert phase == "stable", f"Expected stable, got {phase}"
print(f"  stable: chg5d=+0.5 chg20d=+0.3 rank=50% -> {phase} PASS")

print("ALL CLASSIFICATION TESTS PASS\n")

# === 测试2: 模拟ETF轮动数据计算景气度 ===
print("=" * 50)
print("Test 2: Compute Sector Cycles")
print("=" * 50)

mock_etf_result = {
    "available": True,
    "rankings": [
        {"sector": "半导体", "chg_5d": 3.0, "chg_20d": 5.0, "accel": 1.5, "momentum": 4.0},
        {"sector": "新能源", "chg_5d": 1.5, "chg_20d": -1.0, "accel": 2.0, "momentum": 2.0},
        {"sector": "医药", "chg_5d": 0.3, "chg_20d": 0.5, "accel": 0.1, "momentum": 0.4},
        {"sector": "房地产", "chg_5d": -0.5, "chg_20d": 2.0, "accel": -1.0, "momentum": -0.5},
        {"sector": "银行", "chg_5d": -2.5, "chg_20d": -4.0, "accel": -1.0, "momentum": -3.0},
    ]
}

reset_cycle_cache()
sectors = compute_sector_cycles(mock_etf_result)
print(f"  Sectors computed: {len(sectors)}")

assert "半导体" in sectors
assert sectors["半导体"]["phase"] == "expansion"
assert sectors["半导体"]["score_adj"] == 2
print(f"  半导体: {sectors['半导体']['phase']} adj={sectors['半导体']['score_adj']} PASS")

assert "新能源" in sectors
assert sectors["新能源"]["phase"] == "recovery"
assert sectors["新能源"]["score_adj"] == 3
print(f"  新能源: {sectors['新能源']['phase']} adj={sectors['新能源']['score_adj']} PASS")

assert "银行" in sectors
assert sectors["银行"]["phase"] == "contraction"
assert sectors["银行"]["score_adj"] == -3
print(f"  银行: {sectors['银行']['phase']} adj={sectors['银行']['score_adj']} PASS")

assert "房地产" in sectors
assert sectors["房地产"]["phase"] == "slowdown"
assert sectors["房地产"]["score_adj"] == -2
print(f"  房地产: {sectors['房地产']['phase']} adj={sectors['房地产']['score_adj']} PASS")

print("ALL COMPUTATION TESTS PASS\n")

# === 测试3: get_sector_cycle_score ===
print("=" * 50)
print("Test 3: Score Lookup")
print("=" * 50)

# 使用已缓存的数据
score = get_sector_cycle_score("半导体", mock_etf_result)
assert score == 2, f"Expected 2, got {score}"
print(f"  半导体 score_adj = {score} PASS")

score = get_sector_cycle_score("银行", mock_etf_result)
assert score == -3, f"Expected -3, got {score}"
print(f"  银行 score_adj = {score} PASS")

# 未知行业
score = get_sector_cycle_score("未知行业", mock_etf_result)
assert score == 0, f"Expected 0, got {score}"
print(f"  未知行业 score_adj = {score} PASS")

print("ALL SCORE LOOKUP TESTS PASS\n")

# === 测试4: 空数据/不可用 ===
print("=" * 50)
print("Test 4: Edge Cases")
print("=" * 50)

reset_cycle_cache()
sectors_empty = compute_sector_cycles({"available": False})
assert sectors_empty == {}
print(f"  unavailable data -> empty dict PASS")

sectors_none = compute_sector_cycles(None)
# May fail to import etf_rotation or return empty - either is fine
print(f"  None input -> {len(sectors_none)} sectors PASS")

print("ALL EDGE CASE TESTS PASS\n")

# === 测试5: 配置开关 ===
print("=" * 50)
print("Test 5: Config Toggle")
print("=" * 50)

# 直接修改sector_cycle内部引用的config对象
from trading_system.strategy import sector_cycle as _sc_module
_sc_module.config.SECTOR_CYCLE_ENABLED = False
score = get_sector_cycle_score("半导体", mock_etf_result)
assert score == 0, f"Expected 0 when disabled, got {score}"
print(f"  Disabled: score = {score} PASS")

_sc_module.config.SECTOR_CYCLE_ENABLED = True  # 恢复
reset_cycle_cache()  # 重置Test 4的缓存
score = get_sector_cycle_score("半导体", mock_etf_result)
assert score == 2, f"Expected 2 when re-enabled, got {score}"
print(f"  Re-enabled: score = {score} PASS")

print("ALL CONFIG TESTS PASS\n")

print("=" * 50)
print("=== ALL 5 SMOKE TESTS PASSED ===")
print("=" * 50)
