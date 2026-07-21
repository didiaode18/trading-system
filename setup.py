"""
高胜率A股交易操作系统 - 一键初始化脚本
======================================
用法:
  python setup.py           # 首次运行：安装依赖 + 初始化数据库 + 拉取历史数据
  python setup.py --check   # 检查环境是否就绪
  python setup.py --reset   # 重置数据库（重新拉取全部数据）

迁移到新电脑时:
  1. 拷贝整个项目文件夹
  2. 运行 python setup.py
  3. 编辑 trading_system/holdings.json 填入持仓
  4. 运行 python -m trading_system.main
"""

import os
import sys
import time
import json
import sqlite3
import argparse
import subprocess

# 项目路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADING_SYSTEM_DIR = os.path.join(SCRIPT_DIR, "trading_system")
REQUIREMENTS_FILE = os.path.join(SCRIPT_DIR, "requirements.txt")

sys.path.insert(0, TRADING_SYSTEM_DIR)


def check_python_version():
    """检查Python版本"""
    v = sys.version_info
    print(f"[1/7] Python版本: {v.major}.{v.minor}.{v.micro}", end=" ")
    if v.major >= 3 and v.minor >= 10:
        print("✓")
        return True
    else:
        print("✗ (需要 >= 3.10)")
        return False


def install_dependencies():
    """安装Python依赖"""
    print("[2/7] 安装依赖包...", end=" ")
    if not os.path.exists(REQUIREMENTS_FILE):
        print("✗ (requirements.txt不存在)")
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE, "-q"],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            print("✓")
            return True
        else:
            print(f"✗\n  {result.stderr[:200]}")
            return False
    except Exception as e:
        print(f"✗ ({e})")
        return False


def create_directories():
    """创建必要目录"""
    print("[3/7] 创建目录结构...", end=" ")
    dirs = [
        os.path.join(TRADING_SYSTEM_DIR, "data"),
        os.path.join(TRADING_SYSTEM_DIR, "logs"),
        os.path.join(TRADING_SYSTEM_DIR, "output"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    print("✓")
    return True


def init_database(reset=False):
    """初始化SQLite数据库"""
    import config
    db_path = config.DB_PATH

    if reset and os.path.exists(db_path):
        os.remove(db_path)
        print(f"[4/7] 已重置数据库: {db_path}")
    elif os.path.exists(db_path):
        print(f"[4/7] 数据库已存在: {db_path} ✓")
        return True

    print("[4/7] 初始化数据库...", end=" ")
    try:
        from data.data_loader import init_db
        conn = init_db()
        conn.close()
        print("✓")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def fetch_history_data():
    """拉取全部候选股历史数据"""
    print("[5/7] 拉取历史行情数据（首次约需3-5分钟）...")
    try:
        from data.data_loader import init_db, batch_update_all, get_all_candidate_codes
        conn = init_db()
        codes = get_all_candidate_codes()
        print(f"  候选池: {len(codes)}只股票")

        start = time.time()
        results = batch_update_all(conn, full_pool=True)
        elapsed = time.time() - start

        success = sum(1 for v in results.values() if v > 0)
        failed = sum(1 for v in results.values() if v < 0)
        print(f"  完成: {success}只成功, {failed}只失败, 耗时{elapsed:.1f}秒")
        conn.close()
        return True
    except Exception as e:
        print(f"  ✗ 数据拉取异常: {e}")
        return False


def validate_holdings():
    """验证holdings.json格式"""
    print("[6/7] 验证持仓文件...", end=" ")
    import config
    holdings_file = config.get_holdings_file()

    if not os.path.exists(holdings_file):
        print(f"未找到（将在首次运行时创建）")
        return True

    try:
        with open(holdings_file, "r", encoding="utf-8") as f:
            holdings = json.load(f)
        if not isinstance(holdings, dict):
            print("✗ (格式错误：应为字典)")
            return False
        # 验证每只持仓的必要字段
        for code, pos in holdings.items():
            if "buy_price" not in pos or "shares" not in pos:
                print(f"✗ ({code}缺少buy_price或shares)")
                return False
        print(f"✓ ({len(holdings)}只持仓)")
        return True
    except json.JSONDecodeError as e:
        print(f"✗ (JSON解析失败: {e})")
        return False
    except Exception as e:
        print(f"✗ ({e})")
        return False


def quick_test():
    """快速功能测试"""
    print("[7/7] 快速功能测试...", end=" ")
    try:
        import config
        from data.data_loader import init_db, load_daily_data
        from strategy.trend_strategy import compute_indicators

        conn = init_db()
        # 测试加载一只股票
        test_code = list(config.STOCK_POOL.keys())[0]
        df = load_daily_data(test_code, conn, days=60)
        conn.close()

        if df.empty:
            print(f"✗ (无法加载{test_code}数据)")
            return False

        df = compute_indicators(df)
        if "ma5" not in df.columns:
            print("✗ (指标计算异常)")
            return False

        print(f"✓ (加载{test_code}: {len(df)}条, 指标正常)")
        return True
    except Exception as e:
        print(f"✗ ({e})")
        return False


def bootstrap_data():
    """供main.py调用的自动初始化入口"""
    print("=" * 50)
    print("  检测到首次运行，自动初始化...")
    print("=" * 50)
    create_directories()
    init_database()
    fetch_history_data()
    print("  初始化完成！\n")


def check_environment():
    """检查环境是否就绪"""
    print("\n===== 环境检查 =====")
    ok = True
    ok &= check_python_version()

    # 检查关键依赖
    print("[依赖检查]")
    deps = ["baostock", "akshare", "pandas", "numpy", "openpyxl", "schedule"]
    for dep in deps:
        try:
            __import__(dep)
            print(f"  {dep}: ✓")
        except ImportError:
            print(f"  {dep}: ✗ (未安装)")
            ok = False

    # 检查数据库
    import config
    print(f"\n[数据库] {config.DB_PATH}")
    if os.path.exists(config.DB_PATH):
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT code) FROM daily_kline")
        count = cursor.fetchone()[0]
        conn.close()
        print(f"  状态: ✓ ({count}只股票有数据)")
    else:
        print("  状态: ✗ (不存在，运行 python setup.py 初始化)")
        ok = False

    # 检查持仓文件
    holdings_file = config.get_holdings_file()
    print(f"\n[持仓文件] {holdings_file}")
    if os.path.exists(holdings_file):
        print("  状态: ✓")
    else:
        print("  状态: 未找到（空仓模式）")

    print(f"\n{'=' * 50}")
    print(f"  结果: {'环境就绪 ✓' if ok else '需要初始化 ✗'}")
    print(f"{'=' * 50}\n")
    return ok


def main():
    parser = argparse.ArgumentParser(description="交易系统初始化")
    parser.add_argument("--check", action="store_true", help="检查环境是否就绪")
    parser.add_argument("--reset", action="store_true", help="重置数据库并重新拉取")
    args = parser.parse_args()

    if args.check:
        check_environment()
        return

    print("=" * 50)
    print("  高胜率A股交易操作系统 - 初始化向导")
    print("=" * 50 + "\n")

    # Step 1: Python版本
    if not check_python_version():
        sys.exit(1)

    # Step 2: 安装依赖
    if not install_dependencies():
        print("  提示: 可手动运行 pip install -r requirements.txt")

    # Step 3: 创建目录
    create_directories()

    # Step 4: 初始化数据库
    init_database(reset=args.reset)

    # Step 5: 拉取历史数据
    fetch_history_data()

    # Step 6: 验证持仓
    validate_holdings()

    # Step 7: 快速测试
    quick_test()

    print("\n" + "=" * 50)
    print("  初始化完成！")
    print("  运行方式:")
    print("    python -m trading_system.main           # 盘后分析")
    print("    python -m trading_system.main --monitor  # 盘中监控")
    print("    python -m trading_system.scheduler       # 定时调度")
    print("=" * 50)


if __name__ == "__main__":
    main()
