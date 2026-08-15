"""
P0/P1批1纯函数单元测试（任务#23 批3）
=====================================
覆盖批1已交付的6组纯函数/安全落盘函数:
  1. risk.position_sizing.calc_add_position        加仓计算
  2. risk.risk_control.suggest_atr_threshold       ATR化档位建议
  3. factors.ic_monitor.save_ic_snapshot / settle_ic_pending   IC快照与结算
  4. notify.alert_ledger.record_sent / settle_returns / aggregate_stats  预警台账
  5. execution.execution_closure.snapshot_holdings / diff_and_confirm    执行闭环
  6. strategy.portfolio_risk_actions.build_risk_action_section           风险行动清单HTML

安全约束:
  - 全部落盘类测试使用 pytest tmp_path 临时路径，绝不读写真实 data/ output/ 业务文件
  - 严禁 import trading_system.scheduler / generate_holdings_report（模块级副作用）
  - 不依赖网络，settle类测试注入 fake load_close_fn

运行: pytest tests/test_p0p1_features.py -v
"""
import sys
import os
import json
import datetime
import pytest

# 确保能导入trading_system（与 tests/test_core.py 保持一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'trading_system'))


# ============================================================
# 1. calc_add_position 加仓计算
# ============================================================

class TestCalcAddPosition:
    """加仓股数与新止损价计算"""

    def test_normal_add_atr_stop_and_limits(self):
        """正常加仓: new_stop=max(price-2atr, price*0.90), 100整数倍, ≤现持50%"""
        from trading_system.risk.position_sizing import calc_add_position
        res = calc_add_position("002415", price=20.0, current_shares=2000,
                                capital=500000, atr=1.0)
        # 新止损: max(20-2*1, 20*0.90)=18.0
        assert res["new_stop"] == pytest.approx(18.0)
        # 风险预算: 500000*2%/2=5000股; 现持50%上限=1000 → 钳位到1000
        assert res["add_shares"] == 1000
        assert res["add_shares"] % 100 == 0
        assert res["add_shares"] <= 2000 * 0.5
        assert "风险预算" in res["method"]

    def test_atr_zero_fallback_stop(self):
        """atr=0 回退 price×0.95 止损口径"""
        from trading_system.risk.position_sizing import calc_add_position
        res = calc_add_position("600036", price=10.0, current_shares=1000,
                                capital=200000, atr=0.0)
        assert res["new_stop"] == pytest.approx(9.5)  # 10 * 0.95
        # 风险预算 200000*2%/0.5=8000, 现持50%=500, 总资25%=5000 → 500
        assert res["add_shares"] == 500
        assert res["add_shares"] % 100 == 0

    def test_invalid_input_returns_zero(self):
        """price<=0 / capital<=0 / None → add_shares=0 安全兜底"""
        from trading_system.risk.position_sizing import calc_add_position
        for kwargs in (
            {"price": 0.0, "capital": 100000},
            {"price": -5.0, "capital": 100000},
            {"price": 10.0, "capital": 0},
            {"price": 10.0, "capital": -1},
            {"price": None, "capital": 100000},
        ):
            res = calc_add_position("000001", current_shares=1000,
                                    atr=1.0, **kwargs)
            assert res["add_shares"] == 0

    def test_small_capital_insufficient_100_shares(self):
        """小额资金不足100股 → 不加仓返回0"""
        from trading_system.risk.position_sizing import calc_add_position
        # 风险预算: 10000*2%/10 = 20股 < 100 → 0
        res = calc_add_position("688234", price=100.0, current_shares=1000,
                                capital=10000, atr=10.0)
        assert res["add_shares"] == 0

    def test_kelly_low_winrate_clamps_to_zero(self):
        """低胜率凯利为负 → 钳位 add_shares=0, method标注半凯利"""
        from trading_system.risk.position_sizing import calc_add_position
        # kelly = (1.0*0.2 - 0.8)/1.0 = -0.6 → max(0, -0.3)=0
        res = calc_add_position("002415", price=20.0, current_shares=2000,
                                capital=500000, atr=1.0,
                                win_rate=0.2, pay_off_ratio=1.0)
        assert res["add_shares"] == 0
        assert "半凯利" in res["method"]


# ============================================================
# 2. suggest_atr_threshold ATR化档位
# ============================================================

class TestSuggestAtrThreshold:
    """ATR自适应档位建议"""

    def test_normal_amplify_high_vol(self):
        """高波动: max(base, k*atr/price) 放大档位"""
        from trading_system.risk.risk_control import suggest_atr_threshold
        # 1.5 * 0.8 / 10 = 0.12 > base 0.05, 未超 base*3=0.15
        assert suggest_atr_threshold(10.0, 0.8, 0.05) == pytest.approx(0.12)

    def test_low_vol_keeps_base(self):
        """低波动: 保持基础档位"""
        from trading_system.risk.risk_control import suggest_atr_threshold
        # 1.5 * 0.1 / 10 = 0.015 < 0.05
        assert suggest_atr_threshold(10.0, 0.1, 0.05) == pytest.approx(0.05)

    def test_abnormal_atr_clamped_to_3x_base(self):
        """异常ATR: 钳位不超过 base×3"""
        from trading_system.risk.risk_control import suggest_atr_threshold
        # 1.5 * 5 / 10 = 0.75 → 钳位 0.15
        assert suggest_atr_threshold(10.0, 5.0, 0.05) == pytest.approx(0.15)

    def test_zero_or_invalid_returns_base(self):
        """atr=0 / price=0 / None → 返回 base_pct 除零保护"""
        from trading_system.risk.risk_control import suggest_atr_threshold
        assert suggest_atr_threshold(10.0, 0.0, 0.05) == 0.05
        assert suggest_atr_threshold(0.0, 0.8, 0.05) == 0.05
        assert suggest_atr_threshold(None, 0.8, 0.05) == 0.05
        assert suggest_atr_threshold(10.0, None, 0.05) == 0.05


# ============================================================
# 3. IC快照落盘与结算（全部使用 tmp_path, 严禁写真实 data/）
# ============================================================

class TestIcSettlement:
    """save_ic_snapshot / settle_ic_pending"""

    def _mk_record(self, code, idx):
        return {"code": code,
                "factors": {"up": float(idx), "down": float(-idx)},
                "total_score": 80.0 + idx}

    def test_save_snapshot_overwrite_and_truncate(self, tmp_path):
        """同date覆盖 + 超25个cohort按date排序截断"""
        from trading_system.factors.ic_monitor import save_ic_snapshot
        pending = str(tmp_path / "ic_pending.json")

        # 写入26个cohort, 只保留最近25个
        for day in range(1, 27):
            date = f"2026-01-{day:02d}"
            assert save_ic_snapshot(date, [self._mk_record("C0", day)],
                                    pending_path=pending) is True
        data = json.load(open(pending, encoding='utf-8'))
        dates = [c["date"] for c in data["cohorts"]]
        assert len(dates) == 25
        assert "2026-01-01" not in dates      # 最早的被截断
        assert dates == sorted(dates)

        # 同date覆盖: 对已存在日期写入新内容, 数量不变且内容被替换
        assert save_ic_snapshot("2026-01-26",
                                [self._mk_record("C9", 99)],
                                pending_path=pending) is True
        data = json.load(open(pending, encoding='utf-8'))
        assert len(data["cohorts"]) == 25
        target = [c for c in data["cohorts"] if c["date"] == "2026-01-26"]
        assert len(target) == 1
        assert target[0]["records"][0]["code"] == "C9"

    def test_settle_keeps_not_due_cohort(self, tmp_path):
        """未到期cohort保留在pending, 返回空列表"""
        from trading_system.factors.ic_monitor import (
            save_ic_snapshot, settle_ic_pending)
        pending = str(tmp_path / "ic_pending.json")
        history = str(tmp_path / "ic_history.json")

        save_ic_snapshot("2026-07-25", [self._mk_record("C0", 1)],
                         pending_path=pending)
        settled = settle_ic_pending(
            "2026-08-01",
            lambda code: [("2026-07-25", 10.0), ("2026-08-01", 11.0)],
            forward_days=20,
            pending_path=pending, history_path=history)
        assert settled == []
        # pending保留
        data = json.load(open(pending, encoding='utf-8'))
        assert len(data["cohorts"]) == 1
        # 未结算不写history
        assert not os.path.exists(history)

    def test_settle_due_ic_sign_and_removal(self, tmp_path):
        """到期结算: 上涨/下跌序列IC符号正确, 写history(tmp), pending移除"""
        from trading_system.factors.ic_monitor import (
            save_ic_snapshot, settle_ic_pending)
        pending = str(tmp_path / "ic_pending.json")
        history = str(tmp_path / "ic_history.json")

        # 6只股票: 因子up随i递增, down随i递减; 前瞻收益随i递增
        cohort = [self._mk_record(f"C{i}", i) for i in range(6)]
        save_ic_snapshot("2026-06-01", cohort, pending_path=pending)
        # 再放一个未到期cohort, 验证结算后只移除到期的
        save_ic_snapshot("2026-07-25", [self._mk_record("X0", 1)],
                         pending_path=pending)

        def fake_load_close(code):
            idx = int(code[1:])  # C0~C5
            # 带日期元组列表: 基准日2026-06-01 → 末日，上涨序列
            return [("2026-06-01", 10.0),
                    ("2026-07-01", 10.0 * (1 + 0.01 * (idx + 1)))]

        settled = settle_ic_pending("2026-08-01", fake_load_close,
                                    forward_days=20,
                                    pending_path=pending, history_path=history)
        assert len(settled) == 1
        assert settled[0]["date"] == "2026-06-01"
        assert settled[0]["sample_size"] == 6
        assert settled[0]["factor_count"] == 2

        # history写入到临时路径, IC符号: up与收益同向>0, down反向<0
        assert os.path.exists(history)
        hist = json.load(open(history, encoding='utf-8'))
        assert hist["up"][0]["ic"] > 0
        assert hist["down"][0]["ic"] < 0

        # 已结算cohort从pending移除, 未到期保留
        remain = json.load(open(pending, encoding='utf-8'))["cohorts"]
        remain_dates = [c["date"] for c in remain]
        assert "2026-06-01" not in remain_dates
        assert "2026-07-25" in remain_dates

    def test_settle_ir_bounded_and_small_sample(self, tmp_path):
        """IR有界 |ir|<=10; 样本<5的cohort也移除避免滞留"""
        from trading_system.factors.ic_monitor import (
            save_ic_snapshot, settle_ic_pending)
        pending = str(tmp_path / "ic_pending.json")
        history = str(tmp_path / "ic_history.json")

        cohort = [self._mk_record(f"C{i}", i) for i in range(6)]
        save_ic_snapshot("2026-06-01", cohort, pending_path=pending)
        # 小样本cohort: 仅2只, 不足5只样本
        save_ic_snapshot("2026-06-02",
                         [self._mk_record("S0", 0), self._mk_record("S1", 1)],
                         pending_path=pending)

        def fake_load_close(code):
            if code.startswith("S"):
                # S cohort基准日2026-06-02，序列需覆盖该日
                return [("2026-06-02", 10.0), ("2026-07-01", 12.0)]
            idx = int(code[1:])
            return [("2026-06-01", 10.0),
                    ("2026-07-01", 10.0 * (1 + 0.01 * (idx + 1)))]

        settled = settle_ic_pending("2026-08-01", fake_load_close,
                                    forward_days=20,
                                    pending_path=pending, history_path=history)
        by_date = {s["date"]: s for s in settled}
        # 小样本cohort: factor_count=0 但仍结算移除
        assert by_date["2026-06-02"]["factor_count"] == 0
        assert by_date["2026-06-02"]["sample_size"] == 2

        # IR裁剪保护: 单条记录std=0 → ir被裁剪到[-10,10]
        hist = json.load(open(history, encoding='utf-8'))
        for fname, recs in hist.items():
            for r in recs:
                assert abs(r["ir"]) <= 10.0

        # 两个cohort均从pending移除
        remain = json.load(open(pending, encoding='utf-8'))["cohorts"]
        assert remain == []

    def test_settle_slices_by_base_date(self, tmp_path):
        """基准日切片: 序列首元素早于/晚于cohort日时, 基准取 >= cohort日的首个交易日"""
        from trading_system.factors.ic_monitor import (
            save_ic_snapshot, settle_ic_pending)
        pending = str(tmp_path / "ic_pending.json")
        history = str(tmp_path / "ic_history.json")

        cohort = [self._mk_record(f"C{i}", i) for i in range(6)]
        save_ic_snapshot("2026-06-10", cohort, pending_path=pending)

        def fake_load_close(code):
            idx = int(code[1:])
            if idx % 2 == 0:
                # 首元素早于基准日且价格迥异(100.0): 若误用closes[0]作基准,
                # 前瞻收益变为约-90%, IC符号将被反转; 正确切片应取2026-06-10收盘10.0
                return [("2026-06-01", 100.0),
                        ("2026-06-10", 10.0),
                        ("2026-07-10", 10.0 * (1 + 0.01 * (idx + 1)))]
            # 首元素晚于基准日(cohort日非交易日): 基准取首个>=2026-06-10的交易日2026-06-11
            return [("2026-06-11", 10.0),
                    ("2026-07-10", 10.0 * (1 + 0.01 * (idx + 1)))]

        settled = settle_ic_pending("2026-08-01", fake_load_close,
                                    forward_days=20,
                                    pending_path=pending, history_path=history)
        assert len(settled) == 1
        assert settled[0]["sample_size"] == 6  # 早于/晚于基准日的首元素均被正确切片
        assert settled[0]["factor_count"] == 2

        # 切片正确时前瞻收益随idx递增, up因子IC>0（若误用closes[0]=100作基准则符号反转）
        hist = json.load(open(history, encoding='utf-8'))
        assert hist["up"][0]["ic"] > 0
        assert hist["down"][0]["ic"] < 0


# ============================================================
# 4. 预警台账 alert_ledger（全部使用 tmp_path）
# ============================================================

class TestAlertLedger:
    """record_sent / settle_returns / aggregate_stats"""

    def test_record_sent_fields_complete(self, tmp_path):
        """record_sent 落盘字段完整"""
        from trading_system.notify.alert_ledger import record_sent
        ledger = str(tmp_path / "alert_stats.json")
        record_sent([{"code": "002415", "level": "high", "rule_name": "止损逼近",
                      "urgency_score": 8.5, "price": 25.6,
                      "date": "2026-08-07 09:35:00"}],
                    ledger_path=ledger)
        data = json.load(open(ledger, encoding='utf-8'))
        recs = data["records"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec["code"] == "002415"
        assert rec["level"] == "high"
        assert rec["rule_name"] == "止损逼近"
        assert rec["urgency_score"] == pytest.approx(8.5)
        assert rec["price_at_send"] == pytest.approx(25.6)
        assert rec["date"] == "2026-08-07"
        assert rec["ts"]  # 有写入时间戳

    def _mk_dated_series(self, end_price, n=6):
        """构造从今天起n个自然日的 (date, close) 元组列表（前n-1个收盘=10.0）"""
        days = [(datetime.datetime.now()
                 + datetime.timedelta(days=k)).strftime("%Y-%m-%d")
                for k in range(n)]
        return [(d, 10.0) for d in days[:-1]] + [(days[-1], end_price)]

    def test_settle_returns_backfills(self, tmp_path):
        """settle_returns 回填 fwd_return, 已回填不重复"""
        from trading_system.notify.alert_ledger import record_sent, settle_returns
        ledger = str(tmp_path / "alert_stats.json")
        record_sent([{"code": "A1", "level": "high", "rule_name": "R1",
                      "price": 10.0}], ledger_path=ledger)

        # 序列长度需 >= horizon+1; 切片后第6个收盘价11 → +10%
        n = settle_returns(lambda code: self._mk_dated_series(11.0),
                           horizon_days=5, ledger_path=ledger)
        assert n == 1
        recs = json.load(open(ledger, encoding='utf-8'))["records"]
        assert recs[0]["fwd_return"] == pytest.approx(0.1)

        # 再次调用不重复回填
        n2 = settle_returns(lambda code: self._mk_dated_series(12.0),
                            horizon_days=5, ledger_path=ledger)
        assert n2 == 0

    def test_settle_returns_slices_by_alert_date(self, tmp_path):
        """按预警日切片: 序列首元素早于/晚于预警日时, 基准取 >= 预警日的首个交易日"""
        from trading_system.notify.alert_ledger import record_sent, settle_returns
        ledger = str(tmp_path / "alert_stats.json")
        record_sent([{"code": "B1", "level": "high", "rule_name": "R1",
                      "price": 10.0, "date": "2026-08-01 09:35:00"}],
                    ledger_path=ledger)

        # 首元素早于预警日且价格迥异(99.0): 若误用closes[0]作基准则收益算错;
        # 正确切片应取2026-08-01收盘10.0, 第6个收盘11.0 → +10%
        series = (["2026-07-25", "2026-08-01", "2026-08-02", "2026-08-03",
                   "2026-08-04", "2026-08-05", "2026-08-06"])
        closes = [99.0, 10.0, 10.0, 10.0, 10.0, 10.0, 11.0]
        n = settle_returns(lambda code: list(zip(series, closes)),
                           horizon_days=5, ledger_path=ledger)
        assert n == 1
        recs = json.load(open(ledger, encoding='utf-8'))["records"]
        assert recs[0]["fwd_return"] == pytest.approx(0.1)

        # 序列全部晚于基准日时基准取首个交易日; 全部早于预警日则跳过不回填
        record_sent([{"code": "B2", "level": "high", "rule_name": "R1",
                      "price": 10.0, "date": "2026-08-01"}],
                    ledger_path=ledger)
        n2 = settle_returns(
            lambda code: [("2026-07-01", 10.0), ("2026-07-02", 12.0)]
            if code == "B2" else list(zip(series, closes)),
            horizon_days=5, ledger_path=ledger)
        assert n2 == 0  # B2序列末日仍早于预警日 → 基准找不到, 不回填

    def test_aggregate_stats_by_rule_and_level(self, tmp_path):
        """aggregate_stats 按rule/level聚合"""
        from trading_system.notify.alert_ledger import record_sent, aggregate_stats
        ledger = str(tmp_path / "alert_stats.json")
        record_sent([
            {"code": "A1", "level": "high", "rule_name": "R1", "price": 10.0},
            {"code": "A2", "level": "high", "rule_name": "R1", "price": 20.0},
            {"code": "A3", "level": "low", "rule_name": "R2", "price": 30.0},
        ], ledger_path=ledger)
        # 只回填 A1: +10%（预警日=今天, 序列从今天起覆盖）
        from trading_system.notify.alert_ledger import settle_returns
        settle_returns(lambda code: self._mk_dated_series(11.0)
                       if code == "A1" else None,
                       horizon_days=5, ledger_path=ledger)

        stats = aggregate_stats(ledger_path=ledger)
        assert stats["total"] == 3
        assert stats["settled"] == 1
        assert stats["by_rule"]["R1"]["count"] == 2
        assert stats["by_rule"]["R1"]["avg_fwd_return"] == pytest.approx(0.1)
        assert stats["by_rule"]["R2"]["count"] == 1
        assert stats["by_rule"]["R2"]["avg_fwd_return"] is None  # 未回填
        assert stats["by_level"]["high"]["count"] == 2
        assert stats["by_level"]["low"]["count"] == 1

    def test_90day_window_truncation(self, tmp_path):
        """90天外记录在下次写入时被截断移除"""
        from trading_system.notify.alert_ledger import record_sent
        ledger = str(tmp_path / "alert_stats.json")
        old_ts = (datetime.datetime.now()
                  - datetime.timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
        # 手工构造含超期记录的台账文件
        with open(ledger, 'w', encoding='utf-8') as f:
            json.dump({"records": [
                {"ts": old_ts, "date": "2025-01-01", "code": "OLD",
                 "level": "high", "rule_name": "R1",
                 "urgency_score": 1.0, "price_at_send": 5.0},
            ]}, f, ensure_ascii=False)

        record_sent([{"code": "NEW", "level": "low", "rule_name": "R2",
                      "price": 10.0}], ledger_path=ledger)
        codes = [r["code"] for r in
                 json.load(open(ledger, encoding='utf-8'))["records"]]
        assert "OLD" not in codes  # 90天外被截断
        assert "NEW" in codes


# ============================================================
# 5. 执行闭环 execution_closure（全部使用 tmp_path）
# ============================================================

class TestExecutionClosure:
    """snapshot_holdings / diff_and_confirm"""

    def test_first_snapshot_no_prev_baseline(self, tmp_path):
        """无prev快照: diff_and_confirm 首日基线 has_changes=False"""
        from trading_system.execution.execution_closure import (
            snapshot_holdings, diff_and_confirm,
            PREV_FILENAME, LATEST_FILENAME)
        d = str(tmp_path / "snap")
        holdings = {"002415": {"name": "海康", "shares": 1000}}

        # 先diff(无任何快照) → 首日基线
        res = diff_and_confirm(holdings, snapshot_dir=d)
        assert res["has_changes"] is False
        assert res["items"] == []

        # 首次快照后仍只有latest没有prev → 依旧基线
        path = snapshot_holdings(holdings, snapshot_dir=d)
        assert path and os.path.exists(path)
        assert os.path.exists(os.path.join(d, LATEST_FILENAME))
        assert not os.path.exists(os.path.join(d, PREV_FILENAME))
        res2 = diff_and_confirm(holdings, snapshot_dir=d)
        assert res2["has_changes"] is False

    def test_five_action_types_detected(self, tmp_path):
        """增/减/消失/新增/止损价变更 五类action识别"""
        from trading_system.execution.execution_closure import (
            snapshot_holdings, diff_and_confirm)
        d = str(tmp_path / "snap")
        prev_h = {
            "A": {"name": "股A", "shares": 1000, "stop_loss": 9.0},
            "B": {"name": "股B", "shares": 1000},
            "C": {"name": "股C", "shares": 500},
            "E": {"name": "股E", "shares": 800, "stop_loss": 9.0},
        }
        curr_h = {
            "A": {"name": "股A", "shares": 1500, "stop_loss": 9.0},  # 加仓
            "B": {"name": "股B", "shares": 600},                     # 减仓
            # C 消失 → 卖出
            "D": {"name": "股D", "shares": 200},                     # 新增
            "E": {"name": "股E", "shares": 800, "stop_loss": 9.5},   # 止损价变更
        }
        snapshot_holdings(prev_h, snapshot_dir=d)   # 建立 latest
        snapshot_holdings(curr_h, snapshot_dir=d)   # latest→prev, 写新latest

        res = diff_and_confirm(curr_h, snapshot_dir=d)
        assert res["has_changes"] is True
        actions = {(it["code"], it["action"]) for it in res["items"]}
        assert actions == {
            ("A", "加仓"), ("B", "减仓"), ("C", "卖出"),
            ("D", "新增"), ("E", "止损价变更"),
        }
        assert res["html"] and res["text"]  # 有确认清单输出

    def test_max_two_snapshot_files(self, tmp_path):
        """快照轮转: 目录中最多保留2份快照文件"""
        from trading_system.execution.execution_closure import snapshot_holdings
        d = str(tmp_path / "snap")
        for i in range(4):
            snapshot_holdings({f"C{i}": {"shares": 100 * (i + 1)}},
                              snapshot_dir=d)
        snap_files = [f for f in os.listdir(d)
                      if f.startswith("holdings_snapshot_")]
        assert len(snap_files) == 2
        assert set(snap_files) == {"holdings_snapshot_prev.json",
                                   "holdings_snapshot_latest.json"}


# ============================================================
# 6. build_risk_action_section 风险行动清单HTML
# ============================================================

class TestBuildRiskActionSection:
    """组合风险行动清单HTML生成"""

    def _full_report(self):
        return {
            "risk_score": 72.5,
            "overall_level": "high",
            "scan_time": "2026-08-07 10:00",
            "correlation": {"avg_correlation": 0.65},
            "concentration": {
                "stock_hhi": 0.32,
                "alerts": [{"level": "warning", "type": "单票超限",
                            "detail": "002415占比28%", "suggestion": "减仓"}],
            },
            "var": {"var_pct": 0.05, "risk_level": "high"},
            "max_drawdown": {"max_drawdown": 0.12, "current_drawdown": 0.04,
                             "risk_level": "low"},
            "rebalance": {
                "actions": [{"action": "减仓", "code": "002415",
                             "name": "海康威视", "shares": 100,
                             "amount": 3000, "reason": "集中度超限"}],
                "urgency": "high",
                "rebalance_cost": 30,
            },
        }

    def test_normal_report_html_contains_key_sections(self):
        """正常full_report → HTML含关键文案/代码/动态仓位"""
        from trading_system.strategy.portfolio_risk_actions import (
            build_risk_action_section)
        html = build_risk_action_section(
            self._full_report(),
            dynamic_pos={"method": "atr_volatility", "shares": 300,
                         "amount": 6000, "position_ratio": 0.08,
                         "atr_pct": 0.03, "risk_amount": 500})
        assert isinstance(html, str)
        assert "组合风险行动清单" in html
        assert "集中度超限清单" in html
        assert "全局仓位缩放建议" in html      # VaR high触发
        assert "再平衡建议" in html
        assert "002415" in html
        assert "海康威视" in html
        assert "动态仓位建议" in html
        assert "72.5" in html

    def test_none_report_degrades_gracefully(self):
        """full_report=None 降级为'数据不足'段落, 不抛异常"""
        from trading_system.strategy.portfolio_risk_actions import (
            build_risk_action_section)
        html = build_risk_action_section(None)
        assert isinstance(html, str) and html
        assert "数据不足" in html

    def test_empty_or_malformed_report_degrades(self):
        """空dict/缺字段输入降级不抛异常"""
        from trading_system.strategy.portfolio_risk_actions import (
            build_risk_action_section)
        assert "数据不足" in build_risk_action_section({})
        # 缺字段的极简report: 不抛异常, 输出包含清单标题
        html = build_risk_action_section({"risk_score": None})
        assert isinstance(html, str)
        assert "组合风险行动清单" in html
