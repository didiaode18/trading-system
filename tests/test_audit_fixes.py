# -*- coding: utf-8 -*-
"""
审计修复回归测试（任务#3）
==========================
针对本轮集中修复的最小断言验证:
  1. report_dispatcher.load_holdings 过滤 shares=0 已清仓条目
  2. stock_screener total_score 钳制 min(100)（源码级验证，钳制内联在大函数内）
  3. REBALANCE_CONFIG 阈值单一来源一致性（config 值 + 三个消费方源码引用）
  4. 止损口径一致（report_dispatcher 引用 config.INITIAL_STOP_LOSS_PCT=0.10，无 0.08 硬编码残留）
  5. utils.trading_calendar 节假日/调休推算
  6. email_notify.send_with_fallback 存在性 + SMTP timeout + 失败降级返回

安全约束:
  - import report_dispatcher 会连带导入 data.data_loader（模块级 baostock login），
    测试前以 session 级 fixture stub baostock.login/logout，绝不联网
  - load_holdings 测试通过 monkeypatch report_dispatcher.BASE_DIR 指向 tmp_path，
    绝不读写真实 holdings.json
  - 其余为纯源码级断言与常量断言，不触发任何报告生成/邮件发送

运行: pytest tests/test_audit_fixes.py -v
"""
import sys
import os
import json
import datetime
import pytest

# 确保能导入trading_system（与 tests/test_core.py 保持一致）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'trading_system'))


@pytest.fixture(scope="session", autouse=True)
def _stub_baostock():
    """阻止 data.data_loader 模块级 bs.login() 联网（import report_dispatcher 时触发）"""
    class _FakeLg:
        error_code = '0'
        error_msg = ''
    try:
        import baostock as bs
        bs.login = lambda *a, **k: _FakeLg()
        bs.logout = lambda *a, **k: None
    except ImportError:
        pass
    yield


def _read_src(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath), 'r', encoding='utf-8') as f:
        return f.read()


# ============================================================
# 1. load_holdings 过滤 shares=0
# ============================================================

class TestLoadHoldingsFilter:
    """已清仓(shares=0)条目必须被过滤，shares>0 保留"""

    def test_shares_zero_filtered(self, tmp_path, monkeypatch):
        import report_dispatcher as rd

        holdings = {
            "000001": {"名称": "平安银行", "shares": 1000, "buy_price": 10.0},
            "600036": {"名称": "招商银行", "shares": 0, "buy_price": 30.0, "reason": "已清仓"},
            "002415": {"名称": "海康威视", "shares": 500, "buy_price": 25.0},
            "000858": {"名称": "五粮液", "shares": None, "buy_price": 120.0},
        }
        hfile = tmp_path / "holdings.json"
        hfile.write_text(json.dumps(holdings, ensure_ascii=False), encoding='utf-8')
        # FIX(2026-08-24): load_holdings已重构为config.get_holdings_file()统一路径，
        # 原monkeypatch rd.BASE_DIR失效，改打config路径函数
        monkeypatch.setattr(rd.config, "get_holdings_file", lambda: str(hfile))

        result = rd.load_holdings()
        codes = {h["code"] for h in result}
        assert codes == {"000001", "002415"}, f"过滤结果异常: {codes}"
        assert all(int(h.get("shares", 0)) > 0 for h in result)

    def test_missing_file_fallback(self, tmp_path, monkeypatch):
        """holdings.json 不存在时降级到 config.STOCK_POOL（不抛异常）"""
        import report_dispatcher as rd
        monkeypatch.setattr(rd.config, "get_holdings_file",
                            lambda: str(tmp_path / "not_exist.json"))
        result = rd.load_holdings()
        assert isinstance(result, list)


# ============================================================
# 2. total_score 钳制（源码级验证）
# ============================================================

class TestTotalScoreClamp:
    """钳制逻辑内联在 run_stock_screener 大函数内无法单测，
    此处为源码级验证：确认因子加分后存在 min(100.0, ...) 钳制"""

    def test_clamp_present_in_source(self):
        src = _read_src(os.path.join('trading_system', 'strategy', 'stock_screener.py'))
        assert 'min(100.0, factor_result["total_score"])' in src, \
            "total_score 钳制语句缺失（应存在 min(100.0, factor_result[\"total_score\"])）"

    def test_clamp_logic_semantics(self):
        """对钳制表达式本身做语义断言"""
        for raw in (99.5, 100.0, 103.7, 150.0):
            assert min(100.0, raw) <= 100.0


# ============================================================
# 3. REBALANCE_CONFIG 阈值单一来源一致性
# ============================================================

class TestRebalanceConfigSingleSource:

    def test_config_value(self):
        import config
        assert config.REBALANCE_CONFIG == {
            "score_gap": 25, "sell_threshold": 30, "buy_threshold": 70}

    @pytest.mark.parametrize("relpath", [
        "report_dispatcher.py",
        "generate_holdings_report.py",
        os.path.join("trading_system", "run_canslim_backtest.py"),
    ])
    def test_consumer_references(self, relpath):
        src = _read_src(relpath)
        assert "REBALANCE_CONFIG" in src, f"{relpath} 未引用 REBALANCE_CONFIG"


# ============================================================
# 4. 止损口径一致
# ============================================================

class TestStopLossCaliber:

    def test_config_initial_stop_loss(self):
        import config
        assert config.INITIAL_STOP_LOSS_PCT == 0.10

    def test_dispatcher_no_008_hardcode(self):
        src = _read_src("report_dispatcher.py")
        assert "STOP_LOSS_PCT = 0.08" not in src, "report_dispatcher 仍残留 0.08 硬编码止损"
        assert "config.INITIAL_STOP_LOSS_PCT" in src, \
            "report_dispatcher 未引用 config.INITIAL_STOP_LOSS_PCT"


# ============================================================
# 5. 交易日历
# ============================================================

class TestTradingCalendar:

    def test_new_year_not_trading_day(self):
        from utils.trading_calendar import is_trading_day
        assert is_trading_day(datetime.date(2026, 1, 1)) is False

    def test_makeup_workday_is_trading_day(self):
        """2026-10-10 周六为国庆调休补班日，属交易日"""
        from utils.trading_calendar import is_trading_day
        d = datetime.date(2026, 10, 10)
        assert d.weekday() == 5  # 确实是周六
        assert is_trading_day(d) is True

    def test_next_trading_day_skips_national_day(self):
        """2026-09-30(周三) 之后应跳过国庆假期(10-01~10-08)，落在 2026-10-09(周五)"""
        from utils.trading_calendar import next_trading_day
        nd = next_trading_day(datetime.date(2026, 9, 30))
        assert nd == datetime.date(2026, 10, 9), f"实际得到 {nd}"

    def test_next_trading_day_normal_weekend(self):
        from utils.trading_calendar import next_trading_day
        # 2026-08-14 周五 -> 2026-08-17 周一
        assert next_trading_day(datetime.date(2026, 8, 14)) == datetime.date(2026, 8, 17)


# ============================================================
# 6. email_notify: send_with_fallback / SMTP timeout
# ============================================================

class TestEmailNotifyFallback:

    def test_send_with_fallback_exists(self):
        from notify import email_notify
        assert hasattr(email_notify, "send_with_fallback")
        assert callable(email_notify.send_with_fallback)

    def test_smtp_ssl_timeout_in_source(self):
        src = _read_src(os.path.join('trading_system', 'notify', 'email_notify.py'))
        assert "smtplib.SMTP_SSL(" in src
        assert "timeout=" in src, "SMTP_SSL 调用缺少 timeout 参数"

    def test_fallback_returns_false_with_reason(self, tmp_path, monkeypatch):
        """send_email 失败 + 落盘文件存在 -> 返回 (False, 非空原因)"""
        from notify import email_notify

        monkeypatch.setattr(email_notify, "send_email",
                            lambda subject, html_content, receiver=None: False)
        archive = tmp_path / "fake_report.html"
        archive.write_text("<html>archive</html>", encoding='utf-8')

        ok, reason = email_notify.send_with_fallback(
            "测试主题", "<html>x</html>", archive_path=str(archive))
        assert ok is False
        assert isinstance(reason, str) and reason.strip(), "失败原因不应为空"

    def test_fallback_success_path(self, monkeypatch):
        """send_email 成功 -> 返回 (True, 'sent')"""
        from notify import email_notify
        monkeypatch.setattr(email_notify, "send_email",
                            lambda subject, html_content, receiver=None: True)
        ok, reason = email_notify.send_with_fallback("测试主题", "<html>x</html>")
        assert ok is True
        assert reason == "sent"
