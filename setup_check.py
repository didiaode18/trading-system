# -*- coding: utf-8 -*-
"""
操盘密码交易系统 - 环境自检脚本
==================================
用法:
    python setup_check.py              # 完整自检（不发测试邮件）
    python setup_check.py --email      # 附加SMTP登录测试（需要网络）
    python setup_check.py --strict     # 严格模式：任何警告也视为失败

输出: 通过/未通过清单 + 修复建议。退出码 0=全部通过, 1=存在失败项。
"""

import os
import sys
import json
import sqlite3
import argparse
import io

# Windows控制台GBK编码修复
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TS_DIR = os.path.join(SCRIPT_DIR, "trading_system")
sys.path.insert(0, TS_DIR)

PASS = "✓"
FAIL = "✗"
WARN = "!"

results = []  # (status, item, detail)


def record(ok, item, detail="", warn_only=False):
    status = PASS if ok else (WARN if warn_only else FAIL)
    results.append((status, item, detail))
    print(f"  [{status}] {item}" + (f" — {detail}" if detail else ""))
    return ok


def check_python():
    print("\n[1] Python环境")
    v = sys.version_info
    ok = (v.major == 3 and 9 <= v.minor <= 11)
    record(ok, f"Python版本 {v.major}.{v.minor}.{v.micro}",
           "要求 3.9 ~ 3.11" if not ok else "")


def check_dependencies():
    print("\n[2] 核心依赖包")
    core = ["pandas", "numpy", "scipy", "sklearn", "baostock", "akshare",
            "schedule", "openpyxl", "matplotlib", "joblib"]
    missing = []
    for dep in core:
        try:
            __import__(dep)
            record(True, dep)
        except ImportError:
            record(False, dep, "未安装，运行 pip install -r requirements.txt")
            missing.append(dep)
    print("\n[2b] 可选依赖包（缺失不影响核心运行）")
    optional = {"xgboost": "ML增强", "lightgbm": "ML增强",
                "win10toast": "Windows桌面通知", "streamlit": "仪表盘",
                "pyecharts": "交互式图表"}
    for dep, desc in optional.items():
        try:
            __import__(dep)
            record(True, f"{dep} ({desc})")
        except ImportError:
            record(False, f"{dep} ({desc})", "未安装（可选，不影响核心运行）", warn_only=True)
    return not missing


def check_config():
    print("\n[3] 配置文件")
    record(os.path.exists(os.path.join(TS_DIR, "config.py")), "trading_system/config.py")
    local_cfg = os.path.join(TS_DIR, "config_local.py")
    env_file = os.path.join(SCRIPT_DIR, ".env")
    has_local = os.path.exists(local_cfg)
    record(has_local or os.path.exists(env_file), "敏感配置(config_local.py/.env)",
           "未找到，请复制 config_local.example.py 为 config_local.py 并填写授权码"
           if not (has_local or os.path.exists(env_file)) else "")

    # 加载config并检查关键项
    try:
        import config
        record(True, "config.py可正常加载", f"配置版本 {config.get_config_version()}")
        # 授权码检查
        has_auth = bool(getattr(config, "EMAIL_AUTH_CODE", ""))
        record(has_auth, "EMAIL_AUTH_CODE已配置",
               "为空，邮件通知将不可用（填写config_local.py或设置环境变量）" if not has_auth else "",
               warn_only=not has_auth)
        record(bool(getattr(config, "EMAIL_SENDER", "")), "EMAIL_SENDER已配置")
        record(getattr(config, "TOTAL_CAPITAL", 0) > 0, "TOTAL_CAPITAL有效",
               f"{config.TOTAL_CAPITAL:,.0f}元")
        return config
    except Exception as e:
        record(False, "config.py可正常加载", str(e))
        return None


def check_directories():
    print("\n[4] 目录结构")
    for d in ("data", "logs", "output", "strategy", "risk", "notify"):
        p = os.path.join(TS_DIR, d)
        ok = os.path.isdir(p)
        record(ok, f"trading_system/{d}/", "" if ok else "缺失，将自动创建")
        if not ok:
            try:
                os.makedirs(p, exist_ok=True)
                print(f"      已自动创建: {p}")
            except Exception:
                pass


def check_database(config):
    print("\n[5] 数据库")
    if config is None:
        record(False, "数据库检查", "config加载失败，跳过")
        return
    db_path = getattr(config, "DB_PATH", os.path.join(TS_DIR, "data", "stock_db.db"))
    if not os.path.exists(db_path):
        record(True, f"数据库 {os.path.basename(db_path)}",
               "不存在（首次运行 python setup.py 自动初始化）", warn_only=True)
        return
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT code), COUNT(*) FROM daily_kline")
        n_code, n_row = cur.fetchone()
        conn.close()
        record(True, "数据库可读", f"{n_code}只股票/{n_row}条K线")
    except Exception as e:
        record(False, "数据库可读", str(e))
    # 交易日志库（可选）
    journal = os.path.join(TS_DIR, "data", "trade_journal.db")
    if os.path.exists(journal):
        try:
            conn = sqlite3.connect(journal)
            conn.execute("SELECT name FROM sqlite_master LIMIT 1")
            conn.close()
            record(True, "trade_journal.db可读")
        except Exception as e:
            record(False, "trade_journal.db可读", str(e))


def check_holdings(config):
    print("\n[6] 持仓文件（双份同步机制）")
    if config is None:
        record(False, "持仓检查", "config加载失败，跳过")
        return
    try:
        hf = config.get_holdings_file()
        if os.path.exists(hf):
            with open(hf, "r", encoding="utf-8") as f:
                h = json.load(f)
            record(isinstance(h, dict), "holdings.json格式正确", f"{len(h)}只持仓: {hf}")
        else:
            record(True, "holdings.json", "不存在（空仓模式，首次运行时创建）", warn_only=True)
    except Exception as e:
        record(False, "holdings.json", str(e))


def check_email(config, do_test=False):
    print("\n[7] 邮箱配置")
    if config is None or not getattr(config, "EMAIL_AUTH_CODE", ""):
        record(True, "SMTP登录测试", "跳过（未配置授权码）", warn_only=True)
        return
    if not do_test:
        record(True, "SMTP配置存在", "使用 --email 参数可实测登录")
        return
    try:
        import smtplib
        server = smtplib.SMTP_SSL(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT, timeout=10)
        server.login(config.EMAIL_SENDER, config.EMAIL_AUTH_CODE)
        server.quit()
        record(True, "SMTP登录测试", "授权码有效")
    except Exception as e:
        record(False, "SMTP登录测试", f"{e}（检查授权码是否过期）")


def check_package_import():
    print("\n[8] 包导入")
    try:
        # 从项目根验证包导入
        if SCRIPT_DIR not in sys.path:
            sys.path.insert(0, SCRIPT_DIR)
        import trading_system  # noqa
        record(True, "import trading_system")
        from trading_system.risk.risk_control import UnifiedRiskEngine  # noqa
        record(True, "风控引擎 UnifiedRiskEngine")
        from trading_system.strategy import trend_strategy  # noqa
        record(True, "核心策略 trend_strategy")
    except Exception as e:
        record(False, "import trading_system", str(e))


def main():
    parser = argparse.ArgumentParser(description="环境自检")
    parser.add_argument("--email", action="store_true", help="实测SMTP登录")
    parser.add_argument("--strict", action="store_true", help="警告也视为失败")
    args = parser.parse_args()

    print("=" * 55)
    print("  操盘密码交易系统 - 环境自检")
    print(f"  项目根目录: {SCRIPT_DIR}")
    print("=" * 55)

    check_python()
    check_dependencies()
    config = check_config()
    check_directories()
    check_database(config)
    check_holdings(config)
    check_email(config, do_test=args.email)
    check_package_import()

    n_pass = sum(1 for s, _, _ in results if s == PASS)
    n_warn = sum(1 for s, _, _ in results if s == WARN)
    n_fail = sum(1 for s, _, _ in results if s == FAIL)

    print("\n" + "=" * 55)
    print(f"  汇总: 通过 {n_pass} | 警告 {n_warn} | 失败 {n_fail}")
    if n_fail == 0 and (n_warn == 0 or not args.strict):
        print("  结果: 环境就绪 ✓")
        print("=" * 55)
        sys.exit(0)
    else:
        print("  结果: 存在问题，请按上方 ✗ 项逐一修复")
        print("=" * 55)
        sys.exit(1)


if __name__ == "__main__":
    main()
