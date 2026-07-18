"""Python 环境测试脚本"""
import sys
import platform
import os

print("=" * 50)
print("  Python 环境测试")
print("=" * 50)
print(f"Python 版本:  {platform.python_version()}")
print(f"Python 路径:  {sys.executable}")
print(f"操作系统:     {platform.system()} {platform.release()}")
print(f"架构:         {platform.machine()}")
print(f"pip 版本:     ", end="")

import pip
print(pip.__version__)

# 测试基本功能
print("\n--- 功能测试 ---")
print(f"1 + 1 = {1 + 1}")
print(f"列表推导: {[x**2 for x in range(5)]}")
print(f"f-string:  Hello, Python {sys.version_info.major}.{sys.version_info.minor}!")

import json
data = {"name": "Python", "version": sys.version_info.major}
print(f"JSON 序列化: {json.dumps(data, ensure_ascii=False)}")

import datetime
print(f"当前时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

print("\n[OK] 所有测试通过！Python 环境搭建成功！")
