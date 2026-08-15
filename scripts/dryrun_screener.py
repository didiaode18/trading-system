"""
选股引擎 dry-run 验证脚本（V3.5，2026-08-06）
================================================
运行完整选股流程（CANSLIM主通道 + 短线动量通道），但：
  - 不发送任何邮件（send_email 被拦截，HTML 存档到 output/）
  - 不触发真实交易
  - 不更新 PoolManager 观察池（纯观察模式）

用法: python scripts/dryrun_screener.py
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "trading_system"))
os.chdir(ROOT)

# ---- 1) 拦截邮件发送 ----
import notify.email_notify as _ne


def _fake_send_email(subject, html, *a, **k):
    print(f"\n[DRY-RUN] 邮件已拦截不发送: {subject} (HTML {len(html)} 字符)")
    out = os.path.join(ROOT, "output", "dryrun_screener_report.html")
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[DRY-RUN] HTML报告已存档: {out}")
    except Exception as e:
        print(f"[DRY-RUN] HTML存档失败: {e}")
    return True


_ne.send_email = _fake_send_email

import caopan_report  # noqa: E402
caopan_report.send_email = _fake_send_email  # 顶层绑定也替换

# ---- 2) 禁用PoolManager池更新副作用（纯观察）----
from trading_system.strategy.pool_manager import PoolManager  # noqa: E402
PoolManager.update_pool_weekly = lambda self, *a, **k: {
    "promoted": [], "demoted": [], "expired": []}

# ---- 3) 运行选股引擎 ----
result = caopan_report.run_screener()
if not result:
    print("[DRY-RUN] 选股无结果")
    sys.exit(1)

# ---- 4) 分类输出摘要 ----
print("\n" + "=" * 64)
print("  [DRY-RUN] 选股结果分类摘要")
print("=" * 64)
mi = result["market_info"]
print(f"大盘状态: {mi['market_state']} | 可买入: {mi['can_buy']} | "
      f"买入线: {result.get('min_buy_score')}分")

buys = [s for s in result["stock_pool"] if s.get("is_buy_recommend")]
watches = [s for s in result["stock_pool"] if not s.get("is_buy_recommend")]

print(f"\n[a] CANSLIM 中线推荐买入（{len(buys)}只）:")
for s in buys:
    print(f"   ★ {s['code']} {s['name']} [{s['sector_group']}] 评分{s['factor_score']} | "
          f"买点{s['moderate_buy']} | 止损{s['stop_loss']}")
    print(f"      理由: {s.get('factor_reason', '')}")

mp = result.get("momentum_picks", [])
print(f"\n[b] 短线连板/涨停动量推荐（{len(mp)}只）:")
for p in mp:
    tags = "/".join(p.get("risk_tags", []))
    nb = " [不可买]" if not p.get("buyable") else ""
    print(f"   ⚡ {p['code']} {p['name']} 涨{p['change_pct']:+.1f}% | "
          f"连板{p['consecutive_days']} | 动量{p['momentum_score']}分 | {tags}{nb}")

print(f"\n[c] 仅观察不推荐买入（{len(watches)}只）:")
for s in watches:
    print(f"   ○ {s['code']} {s['name']} [{s['sector_group']}] 评分{s['factor_score']} | "
          f"{s.get('watch_reason', '')}")

if result.get("holdings_risk_watch"):
    print("\n[!] 持仓风控处置（浮亏超8%，禁止加仓/再买入）:")
    for h in result["holdings_risk_watch"]:
        print(f"   ⚠ {h['code']} {h['name']} 浮亏{h['loss_pct']}% — {h['note']}")

print("\n[DRY-RUN] 完成，未发送邮件、未触发交易")
