# -*- coding: utf-8 -*-
"""
文件 I/O 安全工具
=================
提供原子写入等文件操作工具函数，防止进程中断导致数据文件截断/损坏。

设计原则:
  - 所有 JSON 写入均先写临时文件再 os.replace 原子替换
  - 失败时清理临时文件，不留下半成品
  - 绝不抛异常到调用方（写入失败仅日志警告，返回 False）
"""

import os
import json
import logging

logger = logging.getLogger(__name__)


def atomic_json_write(path: str, data, *, encoding: str = "utf-8",
                      indent: int = 2, ensure_ascii: bool = False) -> bool:
    """
    原子写入 JSON 文件（临时文件 + os.replace）

    流程:
      1. 将 data 序列化写入 path + ".tmp"
      2. os.replace 原子替换正式文件（Windows 下同盘原子操作）
      3. 任何步骤失败 → 删除 .tmp 残留 → 记日志 → 返回 False

    参数:
        path: 目标文件路径（自动创建父目录）
        data: 可 JSON 序列化的数据
        encoding: 文件编码，默认 utf-8
        indent: JSON 缩进，默认 2
        ensure_ascii: 是否转义非 ASCII 字符，默认 False

    返回:
        True=写入成功, False=写入失败（已记日志）
    """
    tmp_path = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logger.warning(f"[原子写入] {path} 写入失败: {e}")
        # 清理临时文件残留
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False
