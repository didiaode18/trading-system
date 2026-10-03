# -*- coding: utf-8 -*-
"""
pytest 全局配置
===============
统一 sys.path 设置，确保所有测试文件能正确导入 trading_system 包及根目录脚本。
"""
import sys
import os

# 项目根目录 (d:\workspace\trading-system)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# trading_system 包目录
_PACKAGE_DIR = os.path.join(_PROJECT_ROOT, "trading_system")

# 确保导入路径（所有测试文件不再需要各自 sys.path.insert）
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)
