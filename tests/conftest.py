# -*- coding: utf-8 -*-
"""
pytest 全局配置
===============
统一 sys.path 设置，确保所有测试文件能正确导入 trading_system 包及根目录脚本。
"""
import sys
import os

import pytest

# 项目根目录 (d:\workspace\trading-system)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# trading_system 包目录
_PACKAGE_DIR = os.path.join(_PROJECT_ROOT, "trading_system")

# 确保导入路径（所有测试文件不再需要各自 sys.path.insert）
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)

# FIX(P0-12): 测试环境关闭风控引擎默认股票池强制 ——
# 生产环境 UnifiedRiskEngine 会从 config_local.STOCK_POOL/stock_pool.json 自动构建
# 允许池并拦截池外标的；单测使用合成代码（000001/999999等），需全局关闭。
# 池逻辑专项测试（test_pool_restriction）通过显式传 allowed_pool 覆盖。
try:
    import config as _config  # noqa: E402
    _config.ENFORCE_ALLOWED_POOL = False
except Exception:
    pass


# FIX(P1-21): fill_callback 接通 trades_today.json 写入方后，成交回调类单测
# 会向仓库根的生产 trades_today.json 追写合成成交，污染真实运行时数据。
# 全局 autouse fixture 将写入路径重定向到 tmp，保证测试隔离。
@pytest.fixture(autouse=True)
def _isolate_trades_today(tmp_path, monkeypatch):
    try:
        from execution import fill_callback as _fc
        monkeypatch.setattr(
            _fc, "_trades_today_path",
            lambda: str(tmp_path / "trades_today.json"))
    except Exception:
        pass
    yield


# FIX(G12): 数据源不可达时，@pytest.mark.network 用例应 skip 而非 fail——
# 离线/受限环境下 run_screener 等实时数据依赖用例会因数据源全挂而误判为失败。
# 以真实 HTTP 探测行情端点（非仅 socket 连接）判定数据源是否可用，结果缓存。
_NET_OK = None


def _network_available() -> bool:
    """轻量探测行情数据源是否可达（真实 HTTP GET，结果进程级缓存）。"""
    global _NET_OK
    if _NET_OK is not None:
        return _NET_OK
    import urllib.request
    _probe_urls = [
        "http://qt.gtimg.cn/q=sh000001",
        "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,2,qfq",
    ]
    ok = False
    for _u in _probe_urls:
        try:
            req = urllib.request.Request(_u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.read(1):
                    ok = True
                    break
        except Exception:
            continue
    _NET_OK = ok
    return ok


def pytest_collection_modifyitems(config, items):
    """数据源不可达时，为所有 network 标记用例添加 skip。"""
    if _network_available():
        return
    _skip_net = pytest.mark.skip(reason="网络/数据源不可达，跳过 network 标记用例")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(_skip_net)
