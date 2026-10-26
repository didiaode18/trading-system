# -*- coding: utf-8 -*-
"""
全量历史回测运行器 · 100万初始资金
==================================
用户要求：初始资金 100 万、全量股票池、完整区间、真实交易规则与成本。

本脚本仅在【当前进程内】覆盖 config.TOTAL_CAPITAL = 1_000_000，
不修改 config_local.py（实盘券商同步值 439,135.22 保持不变）。

用法:
    python scripts/run_master_1m.py            # 全量流水线
    python scripts/run_master_1m.py --quick    # 小样本快速验证
"""
import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TS_DIR = os.path.join(ROOT_DIR, "trading_system")
sys.path.insert(0, TS_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
sys.path.insert(0, ROOT_DIR)

# ★ 关键：在导入任何回测模块之前覆盖资金口径
import config  # noqa: E402
config.TOTAL_CAPITAL = 1_000_000
config.AVAILABLE_CASH = 1_000_000
print(f"[i] 已覆盖回测初始资金: TOTAL_CAPITAL = {config.TOTAL_CAPITAL:,.0f} 元 (仅本进程)")

# 延迟导入 master_backtest，确保其内部 getattr(config,'TOTAL_CAPITAL') 读到 100万
import master_backtest  # noqa: E402

if __name__ == "__main__":
    master_backtest.main()
