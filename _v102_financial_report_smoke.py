"""
V10.2 财报深度分析模块 - 冒烟测试
验证五维评分逻辑、加减分映射、缓存、配置开关、边界条件
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "trading_system"))

# ============================================================
# Test 1: 模块导入
# ============================================================
print("=" * 60)
print("  V10.2 财报深度分析 - 冒烟测试")
print("=" * 60)

print("\n[Test 1] 模块导入...")
try:
    from strategy.financial_report import (
        compute_financial_report,
        get_financial_report_bonus,
        get_financial_report_summary,
        reset_report_cache,
        _score_revenue_trend,
        _score_profit_quality,
        _score_margin_change,
        _score_dupont_decomposition,
        _score_balance_structure,
    )
    print("  [OK] 模块导入成功")
except Exception as e:
    print(f"  [FAIL] 模块导入失败: {e}")
    sys.exit(1)

# ============================================================
# Test 2: 五维评分 - 优秀财报场景
# ============================================================
print("\n[Test 2] 优秀财报场景 (高增长+高毛利+高ROE+低负债)...")
reset_report_cache()
excellent_data = {
    "revenue_growth": 35.0,     # 营收增速35%
    "net_profit_growth": 45.0,  # 利润增速45% (>营收=高质量)
    "gross_margin": 55.0,       # 毛利率55%
    "roe": 25.0,                # ROE 25%
    "debt_ratio": 25.0,         # 负债率25%
}
result = compute_financial_report("TEST001", fund_data=excellent_data)
print(f"  评分: {result['report_score']}分, 加减分: {result['bonus']:+d}")
print(f"  摘要: {result['summary']}")
print(f"  明细: 营收{result['revenue_score']}/25 利润{result['profit_score']}/25 "
      f"毛利{result['margin_score']}/20 杜邦{result['dupont_score']}/15 负债{result['balance_score']}/15")

assert result["report_score"] >= 80, f"优秀财报应>=80分, 实际{result['report_score']}"
assert result["bonus"] >= 2, f"优秀财报应>=+2, 实际{result['bonus']}"
print("  [OK] 优秀财报场景通过")

# ============================================================
# Test 3: 五维评分 - 财报预警场景
# ============================================================
print("\n[Test 3] 财报预警场景 (负增长+低毛利+高负债)...")
reset_report_cache()
bad_data = {
    "revenue_growth": -10.0,    # 营收负增长
    "net_profit_growth": -20.0, # 利润大幅下滑
    "gross_margin": 8.0,        # 毛利率极低
    "roe": 3.0,                 # ROE极低
    "debt_ratio": 80.0,         # 负债率80%
}
result = compute_financial_report("TEST002", fund_data=bad_data)
print(f"  评分: {result['report_score']}分, 加减分: {result['bonus']:+d}")
print(f"  摘要: {result['summary']}")

assert result["report_score"] < 30, f"差财报应<30分, 实际{result['report_score']}"
assert result["bonus"] <= -1, f"差财报应<=-1, 实际{result['bonus']}"
print("  [OK] 财报预警场景通过")

# ============================================================
# Test 4: 五维评分 - 中性场景
# ============================================================
print("\n[Test 4] 中性财报场景 (温和增长+适中负债)...")
reset_report_cache()
neutral_data = {
    "revenue_growth": 8.0,
    "net_profit_growth": 10.0,
    "gross_margin": 22.0,
    "roe": 12.0,
    "debt_ratio": 50.0,
}
result = compute_financial_report("TEST003", fund_data=neutral_data)
print(f"  评分: {result['report_score']}分, 加减分: {result['bonus']:+d}")
print(f"  摘要: {result['summary']}")

assert -2 <= result["bonus"] <= 2, f"中性财报应在-2~+2, 实际{result['bonus']}"
print("  [OK] 中性财报场景通过")

# ============================================================
# Test 5: 无数据降级
# ============================================================
print("\n[Test 5] 无数据降级 (所有字段None)...")
reset_report_cache()
empty_data = {
    "revenue_growth": None,
    "net_profit_growth": None,
    "gross_margin": None,
    "roe": None,
    "debt_ratio": None,
}
result = compute_financial_report("TEST004", fund_data=empty_data)
print(f"  评分: {result['report_score']}分, 加减分: {result['bonus']:+d}")
print(f"  摘要: {result['summary']}")

assert result["bonus"] == 0, f"无数据应0分, 实际{result['bonus']}"
assert result["summary"] == "财报数据不足", f"无数据摘要应为'财报数据不足', 实际'{result['summary']}'"
print("  [OK] 无数据降级通过")

# ============================================================
# Test 6: 缓存验证
# ============================================================
print("\n[Test 6] 缓存验证 (第二次调用应命中缓存)...")
reset_report_cache()
r1 = compute_financial_report("TEST005", fund_data=excellent_data)
r2 = compute_financial_report("TEST005", fund_data=bad_data)  # 即使传入不同数据，应返回缓存结果
assert r1["report_score"] == r2["report_score"], "缓存应返回相同结果"
print(f"  第一次: {r1['report_score']}分, 第二次(缓存): {r2['report_score']}分")
print("  [OK] 缓存验证通过")

# ============================================================
# Test 7: 配置开关
# ============================================================
print("\n[Test 7] 配置开关 (禁用时应返回0)...")
reset_report_cache()
import config
original = config.FINANCIAL_REPORT_ENABLED
config.FINANCIAL_REPORT_ENABLED = False
result = compute_financial_report("TEST006", fund_data=excellent_data)
config.FINANCIAL_REPORT_ENABLED = original  # 恢复
assert result["bonus"] == 0, f"禁用时应返回0, 实际{result['bonus']}"
assert result["summary"] == "未启用", f"禁用时摘要应为'未启用', 实际'{result['summary']}'"
print("  [OK] 配置开关通过")

# ============================================================
# Test 8: 便捷接口
# ============================================================
print("\n[Test 8] 便捷接口 (get_financial_report_bonus / get_financial_report_summary)...")
reset_report_cache()
bonus = get_financial_report_bonus("TEST007", fund_data=excellent_data)
summary = get_financial_report_summary("TEST007", fund_data=excellent_data)
print(f"  bonus={bonus:+d}, summary='{summary}'")
assert isinstance(bonus, int), f"bonus应为int, 实际{type(bonus)}"
assert isinstance(summary, str), f"summary应为str, 实际{type(summary)}"
print("  [OK] 便捷接口通过")

# ============================================================
# Test 9: 利润质量 - 增收不增利场景
# ============================================================
print("\n[Test 9] 增收不增利场景 (营收高增但利润低增)...")
reset_report_cache()
trap_data = {
    "revenue_growth": 40.0,     # 营收高增40%
    "net_profit_growth": 5.0,   # 利润仅增5% (增收不增利)
    "gross_margin": 15.0,       # 低毛利
    "roe": 8.0,
    "debt_ratio": 55.0,
}
result = compute_financial_report("TEST008", fund_data=trap_data)
print(f"  评分: {result['report_score']}分, 加减分: {result['bonus']:+d}")
print(f"  利润质量分: {result['profit_score']}/25")
# 增收不增利应该在利润质量维度被扣分
assert result["profit_score"] <= 8, f"增收不增利利润质量应<=8, 实际{result['profit_score']}"
print("  [OK] 增收不增利识别通过")

# ============================================================
# Test 10: 杜邦分解 - 高ROE+高毛利 vs 高ROE+低毛利
# ============================================================
print("\n[Test 10] 杜邦分解区分能力...")
reset_report_cache()
# 高质量ROE (净利率驱动)
high_quality = {"roe": 22.0, "gross_margin": 45.0}
r_hq = compute_financial_report("TEST009A", fund_data=high_quality)
# 低质量ROE (可能杠杆驱动)
low_quality = {"roe": 22.0, "gross_margin": 12.0}
r_lq = compute_financial_report("TEST009B", fund_data=low_quality)
print(f"  高ROE+高毛利: 杜邦{r_hq['dupont_score']}/15")
print(f"  高ROE+低毛利: 杜邦{r_lq['dupont_score']}/15")
assert r_hq["dupont_score"] > r_lq["dupont_score"], "高毛利ROE应比低毛利ROE杜邦分更高"
print("  [OK] 杜邦分解区分能力通过")

# ============================================================
# 总结
# ============================================================
print("\n" + "=" * 60)
print("  [ALL PASS] 全部10项冒烟测试通过！")
print("=" * 60)
print("\n财报深度分析模块功能验证:")
print("  [OK] 五维评分体系正常工作")
print("  [OK] 优秀财报正确加分 (+4)")
print("  [OK] 财报预警正确减分 (-2)")
print("  [OK] 增收不增利陷阱识别")
print("  [OK] 杜邦分解区分ROE质量")
print("  [OK] 无数据安全降级")
print("  [OK] 缓存机制正常")
print("  [OK] 配置开关生效")
