# -*- coding: utf-8 -*-
"""一次性清洗 ic_history.json（2026-08-07，第二轮扩展）

- 备份到 output/ic_history_backup_YYYYMMDD.json
- 删除测试污染键 test_factor / weak_factor
- 删除 IR 溢出记录（|ir| > 10，存在 -8.6e15 量级脏数据）
- 删除旧口径CANSLIM记录（N/S/L/CAI/P 缺ir字段，为2026-08-07前伪前瞻实现残留，
  新延迟结算记录必含ir字段）
- 删除伪前瞻污染记录（|ic| > 0.99，同期相关而非预测力，如return_5d IC=1.0）
- 同因子同日重复记录去重（保留最后一条）
- 保持 {factor: [records]} 结构写回原文件

用法（工作区根目录）: python scripts/clean_ic_history.py
"""
import json
import os
import shutil
import sys
from datetime import datetime

# 工作区根目录（scripts/ 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IC_HISTORY_PATH = os.path.join(ROOT, "trading_system", "data", "ic_history.json")
OUTPUT_DIR = os.path.join(ROOT, "output")

# 测试污染的因子键
POLLUTED_KEYS = ("test_factor", "weak_factor")
# IR 溢出阈值：|ir| 超过该值视为脏记录
IR_LIMIT = 10.0
# 旧口径CANSLIM因子（缺ir字段的记录为伪前瞻实现残留，新延迟结算记录必含ir）
CANSLIM_FACTORS = ("N", "S", "L", "CAI", "P")
# 伪前瞻污染阈值：|ic| 超过该值视为同期相关而非预测力（如return_5d IC=1.0）
PSEUDO_FORWARD_IC_LIMIT = 0.99


def main():
    if not os.path.exists(IC_HISTORY_PATH):
        print(f"[错误] 未找到 {IC_HISTORY_PATH}")
        sys.exit(1)

    # 1. 读取原始数据
    with open(IC_HISTORY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"原始因子数: {len(data)}")

    # 2. 备份到 output/ 目录（当天日期后缀）
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    backup_path = os.path.join(OUTPUT_DIR, f"ic_history_backup_{today}.json")
    shutil.copy2(IC_HISTORY_PATH, backup_path)
    print(f"备份路径: {backup_path}")

    # 3. 删除测试污染键
    removed_keys = {}
    for key in POLLUTED_KEYS:
        if key in data:
            removed_keys[key] = len(data.pop(key))

    # 4. 裁剪各因子的脏记录（IR溢出/旧口径缺ir/伪前瞻IC/同日重复）
    stats = {}
    cleaned = {}
    for factor, records in data.items():
        kept = []
        seen_dates = {}  # date -> kept中的下标，用于同日去重（保留最后一条）
        dropped_ir = dropped_legacy = dropped_pseudo = dropped_dup = 0
        for rec in records:
            ir = rec.get("ir")
            if ir is not None and abs(ir) > IR_LIMIT:
                dropped_ir += 1  # IR 溢出，丢弃该条记录
                continue
            # 旧口径CANSLIM记录：缺ir字段，与新延迟结算记录同键混合会污染5日均IC
            if factor in CANSLIM_FACTORS and ir is None:
                dropped_legacy += 1
                continue
            # 伪前瞻污染：|IC|>0.99（衡量的是与历史收益的同期相关而非预测力）
            ic = rec.get("ic")
            if ic is not None and abs(ic) > PSEUDO_FORWARD_IC_LIMIT:
                dropped_pseudo += 1
                continue
            # 同因子同日重复：用后写入的覆盖先写入的
            d = rec.get("date")
            if d and d in seen_dates:
                kept[seen_dates[d]] = rec
                dropped_dup += 1
                continue
            if d:
                seen_dates[d] = len(kept)
            kept.append(rec)
        if dropped_ir or dropped_legacy or dropped_pseudo or dropped_dup:
            stats[factor] = {"ir溢出": dropped_ir, "旧口径缺ir": dropped_legacy,
                             "伪前瞻": dropped_pseudo, "同日重复": dropped_dup}
        cleaned[factor] = kept

    # 5. 写回原文件
    with open(IC_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)

    # 6. 打印清洗统计
    print("\n===== 清洗统计 =====")
    for key, n in removed_keys.items():
        print(f"删除污染键 {key}: {n} 条")
    if not removed_keys:
        print("未发现污染键 test_factor/weak_factor")
    for factor, s in stats.items():
        parts = [f"{k}删{n}条" for k, n in s.items() if n]
        print(f"因子 {factor}: " + ", ".join(parts))
    if not stats:
        print("未发现脏记录")
    print(f"清洗后因子数: {len(cleaned)}")
    print(f"已写回: {IC_HISTORY_PATH}")


if __name__ == "__main__":
    main()
