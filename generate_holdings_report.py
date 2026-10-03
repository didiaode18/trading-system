# -*- coding: utf-8 -*-
"""
向后兼容垫片 — P1-5 已将实际逻辑迁移至 output/generate_holdings_report.py
=========================================================================
本文件保留仅为向后兼容（run.py / scheduler.py 等旧路径引用），
新代码请直接使用:  python output/generate_holdings_report.py
"""
import subprocess
import sys
import os

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "output", "generate_holdings_report.py")

if __name__ == "__main__":
    # 透传命令行参数，直接执行新版本
    sys.exit(subprocess.call([sys.executable, _SCRIPT] + sys.argv[1:]))
