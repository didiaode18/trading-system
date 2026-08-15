"""动量通道离线逻辑验证（合成行情，不联网）"""
import sys
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "trading_system"))
import pandas as pd  # noqa: E402
from strategy.stock_screener import run_momentum_screener  # noqa: E402

md = pd.DataFrame([
    # 缩量三连板（量比0.4，应被涨停池豁免）
    {"code": "600001", "name": "三连板缩量", "price": 12.1, "change_pct": 10.0,
     "amount": 8e8, "turnover": 5.0, "vol_ratio": 0.4, "open": 11.1, "prev_close": 11.0},
    # 一字板（开盘=涨停价，不可买）
    {"code": "600002", "name": "一字板", "price": 11.0, "change_pct": 10.0,
     "amount": 3e8, "turnover": 0.5, "vol_ratio": 0.1, "open": 11.0, "prev_close": 10.0},
    # 普通放量强势股
    {"code": "600003", "name": "普通放量强势", "price": 10.6, "change_pct": 6.0,
     "amount": 9e8, "turnover": 8.0, "vol_ratio": 2.5, "open": 10.1, "prev_close": 10.0},
    # 缩量但非涨停池（应被量比门槛过滤）
    {"code": "600004", "name": "缩量非涨停", "price": 10.7, "change_pct": 7.0,
     "amount": 9e8, "turnover": 4.0, "vol_ratio": 0.8, "open": 10.1, "prev_close": 10.0},
    # 十连板妖股（高换手）
    {"code": "600005", "name": "十连板妖股", "price": 22.0, "change_pct": 10.0,
     "amount": 12e8, "turnover": 20.0, "vol_ratio": 3.0, "open": 20.1, "prev_close": 20.0},
])
zt_pool = [
    {"code": "600001", "name": "三连板缩量", "consecutive_days": 3},
    {"code": "600002", "name": "一字板", "consecutive_days": 2},
    {"code": "600005", "name": "十连板妖股", "consecutive_days": 10},
]

r = run_momentum_screener(market_df=md, zt_pool=zt_pool)
print("summary:", r["summary"])
for p in r["picks"]:
    tags = "/".join(p["risk_tags"])
    print(f"{p['code']} {p['name']} | score={p['momentum_score']} | "
          f"consec={p['consecutive_days']} | tags={tags} | "
          f"{'可买' if p['buyable'] else '不可买'}")

codes = [p["code"] for p in r["picks"]]
assert "600001" in codes, "缩量三连板应被豁免进入"
assert "600004" not in codes, "非涨停缩量股应被量比门槛过滤"
_yz = r["picks"][codes.index("600002")]
assert not _yz["buyable"], "一字板不可买"
assert "一字板·不可买" in _yz["risk_tags"], "一字板风险标签缺失"
_lb3 = r["picks"][codes.index("600001")]
assert "3板接力" in _lb3["risk_tags"], "3板接力标签缺失"
_yg = r["picks"][codes.index("600005")]
assert any("妖股" in t for t in _yg["risk_tags"]), "妖股标签缺失"
assert "高波动短线" in _yg["risk_tags"], "高换手应标 高波动短线"
# 排序: 妖股(连板10+高成交)分最高排最前
assert codes[0] == "600005", f"动量评分排序异常: {codes}"
print("ALL ASSERTIONS PASSED")
