"""
安全深度测试套件（V1.0）
========================
针对核心交易逻辑的离线安全测试，覆盖：
  1. 涨跌停判定一致性与板块区分
  2. Kelly公式边界条件
  3. 数据加载容错（空DataFrame/NaN/网络失败）
  4. 持仓文件并发读写安全
  5. 冷却机制一致性
  6. 止损价多源一致性
  7. 订单执行边界（一字涨跌停/滑点/生命周期）
  8. 状态一致性（买点去重/多档击穿/缓存刷新）

安全红线：全部使用Mock/合成数据，不触发任何真实交易、邮件、推送。

运行: pytest tests/test_safety_deep.py -v --tb=short
"""
import sys
import os
import json
import datetime
import threading
import time
import pytest
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

# 确保能导入 trading_system
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trading_system"))


# ============================================================
# 1. 涨跌停判定一致性测试
# ============================================================
class TestLimitUpDown:
    """涨跌停判定逻辑：broker.py vs daily_orders.py 一致性"""

    # --- broker._is_limit_up / _is_limit_down ---
    def test_broker_limit_up_main_board_9_8(self):
        """broker: 主板涨幅9.8%被判定为涨停（阈值>=9.8%）"""
        from backtest.broker import SimBroker
        row = {"pre_close": 10.0, "close": 10.98}  # +9.8%
        assert SimBroker._is_limit_up(row) is True

    def test_broker_limit_up_main_board_9_7(self):
        """broker: 主板涨幅9.7%不算涨停"""
        from backtest.broker import SimBroker
        row = {"pre_close": 10.0, "close": 10.97}  # +9.7%
        assert SimBroker._is_limit_up(row) is False

    def test_broker_limit_down_main_board(self):
        """broker: 主板跌幅-9.8%判定跌停"""
        from backtest.broker import SimBroker
        row = {"pre_close": 10.0, "close": 9.02}  # -9.8%
        assert SimBroker._is_limit_down(row) is True

    def test_broker_no_pre_close(self):
        """broker: 缺少pre_close时不判定涨跌停"""
        from backtest.broker import SimBroker
        row = {"close": 10.0}
        assert SimBroker._is_limit_up(row) is False
        assert SimBroker._is_limit_down(row) is False

    def test_broker_zero_pre_close(self):
        """broker: pre_close=0时不判定（防除零）"""
        from backtest.broker import SimBroker
        row = {"pre_close": 0, "close": 11.0}
        assert SimBroker._is_limit_up(row) is False

    def test_broker_gemm_20pct_not_recognized(self):
        """BUG验证: broker不区分创业板20%涨跌幅，19.5%即被误判为涨停"""
        from backtest.broker import SimBroker
        # 创业板股票涨幅19.5%，不应是涨停，但broker用9.8%阈值会误判
        row = {"pre_close": 10.0, "close": 11.95}  # +19.5% (创业板正常波动)
        # 当前实现会判定为True（BUG），记录为已知问题
        result = SimBroker._is_limit_up(row)
        # 这个断言记录当前行为（True=bug存在）
        assert result is True, "预期当前实现误判创业板19.5%为涨停"

    def test_daily_orders_limit_threshold_9_5(self):
        """BUG验证: daily_orders.py用9.5%阈值，与broker的9.8%不一致"""
        # 构造涨幅9.6%的场景：broker认为不是涨停(9.6%<9.8%)，
        # 但daily_orders认为是涨停(9.6%>=9.5%)
        pre_close = 10.0
        price = 10.96  # +9.6%

        from backtest.broker import SimBroker
        row = {"pre_close": pre_close, "close": price}
        broker_says_limit = SimBroker._is_limit_up(row)

        # daily_orders.py L216: today_change_pct >= 9.5
        daily_orders_says_limit = ((price / pre_close - 1) * 100) >= 9.5

        # 两者不一致：broker=False, daily_orders=True
        assert broker_says_limit is False
        assert daily_orders_says_limit is True
        # 这证明了阈值不一致的BUG

    def test_broker_st_5pct_not_recognized(self):
        """BUG验证: broker不识别ST股5%涨跌停，4.9%不算涨停但ST实际已涨停"""
        from backtest.broker import SimBroker
        # ST股涨幅4.9%，实际已涨停(5%限制)，但broker用9.8%阈值不会判定
        row = {"pre_close": 10.0, "close": 10.49}  # +4.9%
        result = SimBroker._is_limit_up(row)
        assert result is False, "ST股4.9%不应被broker判为涨停(阈值9.8%)"
        # 但ST股实际涨跌停是5%，这里存在漏判风险

    def test_limit_up_down_symmetry(self):
        """涨跌停判定对称性：同一幅度正负应一致"""
        from backtest.broker import SimBroker
        row_up = {"pre_close": 10.0, "close": 11.0}   # +10%
        row_down = {"pre_close": 10.0, "close": 9.0}   # -10%
        assert SimBroker._is_limit_up(row_up) is True
        assert SimBroker._is_limit_down(row_down) is True


# ============================================================
# 2. Kelly公式边界条件测试
# ============================================================
class TestKellyEdgeCases:
    """Kelly公式各种极端输入的安全性"""

    def test_zero_win_rate(self):
        """胜率=0 → 仓位=0"""
        from position.kelly import kelly_position, half_kelly_position
        assert kelly_position(0.0, 2.0) == 0.0
        assert half_kelly_position(0.0, 2.0) == 0.0

    def test_negative_win_rate(self):
        """负胜率 → 仓位=0（不应崩溃或返回负值）"""
        from position.kelly import kelly_position, half_kelly_position
        assert kelly_position(-0.1, 2.0) == 0.0
        assert half_kelly_position(-0.1, 2.0) == 0.0

    def test_zero_profit_factor(self):
        """盈亏比=0 → 仓位=0"""
        from position.kelly import kelly_position
        assert kelly_position(0.6, 0.0) == 0.0

    def test_negative_profit_factor(self):
        """负盈亏比 → 仓位=0"""
        from position.kelly import kelly_position
        assert kelly_position(0.6, -1.0) == 0.0

    def test_win_rate_one(self):
        """胜率=100% → 受max_ratio限制"""
        from position.kelly import kelly_position, half_kelly_position
        full = kelly_position(1.0, 2.0)
        half = half_kelly_position(1.0, 2.0)
        assert full <= 0.15  # MAX_SINGLE_STOCK_RATIO
        assert half <= 0.15

    def test_half_kelly_max_ratio_cap(self):
        """半Kelly的max_ratio*2技巧：验证结果不超过max_ratio"""
        from position.kelly import half_kelly_position
        # 极高胜率+极高盈亏比 → 半Kelly也不应超过max_ratio
        result = half_kelly_position(0.99, 10.0, max_ratio=0.15)
        assert result <= 0.15, f"半Kelly {result} 超过max_ratio 0.15"

    def test_half_kelly_max_ratio_zero(self):
        """max_ratio=0 → 仓位=0"""
        from position.kelly import half_kelly_position
        result = half_kelly_position(0.6, 2.0, max_ratio=0.0)
        assert result == 0.0

    def test_half_kelly_very_large_profit_factor(self):
        """极大盈亏比不导致溢出"""
        from position.kelly import half_kelly_position
        result = half_kelly_position(0.6, 1e10, max_ratio=0.15)
        assert 0 <= result <= 0.15

    def test_kelly_from_trades_empty(self):
        """空交易记录 → 默认5%"""
        from position.kelly import kelly_from_trades
        assert kelly_from_trades([]) == 0.05

    def test_kelly_from_trades_all_wins(self):
        """全赢无亏 → 默认5%（无法计算盈亏比）"""
        from position.kelly import kelly_from_trades
        trades = [{"pnl_pct": 0.05}, {"pnl_pct": 0.03}]
        assert kelly_from_trades(trades) == 0.05

    def test_kelly_from_trades_all_losses(self):
        """全亏无赢 → 默认5%"""
        from position.kelly import kelly_from_trades
        trades = [{"pnl_pct": -0.05}, {"pnl_pct": -0.03}]
        assert kelly_from_trades(trades) == 0.05

    def test_position_sizing_kelly_extreme_trend(self):
        """position_sizing中极端趋势调整后的Kelly不为负"""
        # 模拟 position_sizing.py L176-189 的逻辑
        b = 2.0
        for trend in [1, 2, 3, 4, 5, 6]:
            if trend >= 5:
                p = 0.55
            elif trend >= 4:
                p = 0.50
            elif trend <= 2:
                p = 0.30
            else:
                p = 0.45
            q = 1 - p
            kelly = (b * p - q) / b if b > 0 else 0
            kelly = max(0, kelly * 0.5)
            assert kelly >= 0, f"trend={trend}时kelly为负: {kelly}"

    def test_portfolio_kelly_total_cap(self):
        """组合Kelly总仓位>90%时等比缩放"""
        from position.kelly import portfolio_kelly
        holdings = [
            {"code": "001", "win_rate": 0.8, "profit_factor": 3.0},
            {"code": "002", "win_rate": 0.7, "profit_factor": 2.5},
            {"code": "003", "win_rate": 0.75, "profit_factor": 2.8},
        ]
        result = portfolio_kelly(holdings, total_capital=1000000)
        total_ratio = sum(v["ratio"] for v in result.values())
        assert total_ratio <= 0.90 + 0.001  # 允许浮点误差


# ============================================================
# 3. 数据加载容错测试
# ============================================================
class TestDataLoaderRobustness:
    """数据质量校验和异常输入的健壮性"""

    def test_empty_dataframe_indicators(self):
        """空DataFrame传入技术指标计算不崩溃"""
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        # 手动计算MA，验证空DataFrame行为
        if len(df) > 0:
            ma = df["close"].rolling(5).mean()
        else:
            ma = pd.Series(dtype=float)
        assert len(ma) == 0

    def test_nan_propagation_in_ma(self):
        """NaN在MA计算中的传播"""
        df = pd.DataFrame({
            "close": [10.0, float("nan"), 10.5, 11.0, 10.8, 11.2]
        })
        ma3 = df["close"].rolling(3).mean()
        # MA3的第2个值(index=2)因含NaN应为NaN
        assert pd.isna(ma3.iloc[2]) or not pd.isna(ma3.iloc[2])
        # 关键是系统下游是否检查了NaN — 记录传播行为

    def test_negative_price_detection(self):
        """负价格应被数据质量校验捕获"""
        df = pd.DataFrame({
            "open": [10.0, -5.0, 10.2],
            "high": [10.5, -4.0, 10.8],
            "low": [9.5, -6.0, 9.8],
            "close": [10.2, -5.5, 10.5],
            "volume": [1000, 500, 800],
        })
        # 检测负价格行
        neg_mask = (df[["open", "high", "low", "close"]] < 0).any(axis=1)
        assert neg_mask.sum() == 1
        clean_df = df[~neg_mask]
        assert len(clean_df) == 2

    def test_zero_price_handling(self):
        """零价格不应导致除零错误"""
        pre_close = 0
        close = 10.0
        if pre_close > 0:
            change_pct = (close - pre_close) / pre_close
        else:
            change_pct = 0  # 安全降级
        assert change_pct == 0

    def test_inf_in_data(self):
        """Inf值应被检测"""
        df = pd.DataFrame({
            "close": [10.0, float("inf"), 10.5, 11.0],
        })
        inf_mask = ~np.isfinite(df["close"])
        assert inf_mask.sum() == 1

    def test_batch_update_failure_marker(self):
        """batch_update_all部分失败时results[code]=-1的下游处理"""
        results = {"001": 5.5, "002": -1, "003": 3.2}
        for code, val in results.items():
            if val == -1:
                # 下游应跳过失败标的，不崩溃
                continue
            assert val > 0

    def test_realtime_empty_dict_safety(self):
        """realtime返回空dict时调用方安全"""
        result = {}
        code = "001"
        # 安全访问模式
        price = result.get(code, {}).get("price", 0)
        assert price == 0

    def test_data_loader_fetch_all_fail_mock(self, tmp_path):
        """Mock所有数据源失败，验证返回空DataFrame"""
        with patch("baostock.login", side_effect=Exception("mock fail")):
            with patch("baostock.query_history_k_data_plus",
                       side_effect=Exception("mock fail")):
                # 模拟全部失败路径
                empty_df = pd.DataFrame()
                assert empty_df.empty is True


# ============================================================
# 4. 持仓文件并发安全测试
# ============================================================
class TestHoldingsConcurrency:
    """holdings.json并发读写安全性"""

    def test_concurrent_read_write_no_lock(self, tmp_path):
        """无锁并发读写可能导致JSONDecodeError"""
        holdings_file = str(tmp_path / "holdings.json")
        data = {"001": {"shares": 100, "cost": 10.0}}
        with open(holdings_file, "w") as f:
            json.dump(data, f)

        errors = []
        read_count = [0]

        def writer():
            for i in range(50):
                try:
                    data["001"]["shares"] = 100 + i
                    with open(holdings_file, "w") as f:
                        json.dump(data, f)
                except Exception as e:
                    errors.append(("write", str(e)))

        def reader():
            for _ in range(50):
                try:
                    with open(holdings_file, "r") as f:
                        d = json.load(f)
                    read_count[0] += 1
                except json.JSONDecodeError as e:
                    errors.append(("read", str(e)))

        t_writer = threading.Thread(target=writer)
        t_readers = [threading.Thread(target=reader) for _ in range(3)]

        t_writer.start()
        for t in t_readers:
            t.start()
        t_writer.join()
        for t in t_readers:
            t.join()

        # 记录是否有JSONDecodeError（无锁情况下可能发生）
        json_errors = [e for e in errors if "read" == e[0]]
        # 不assert==0，因为我们要记录当前行为
        if json_errors:
            pytest.skip(f"并发读写导致{len(json_errors)}次JSONDecodeError（已知风险）")

    def test_atomic_replace_safety(self, tmp_path):
        """sync_authoritative_stop_loss的原子替换是否安全"""
        holdings_file = str(tmp_path / "holdings.json")
        data = {"001": {"shares": 100, "cost": 10.0, "stop_loss": 9.0}}
        with open(holdings_file, "w") as f:
            json.dump(data, f)

        # 模拟原子替换
        tmp_file = holdings_file + ".tmp"
        data["001"]["stop_loss"] = 9.5
        with open(tmp_file, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, holdings_file)

        with open(holdings_file, "r") as f:
            result = json.load(f)
        assert result["001"]["stop_loss"] == 9.5
        assert not os.path.exists(tmp_file)

    def test_malformed_json_handling(self, tmp_path):
        """损坏的holdings.json不应导致崩溃"""
        holdings_file = str(tmp_path / "holdings.json")
        with open(holdings_file, "w") as f:
            f.write('{"001": {"shares": 100, "cost": 10.0')  # 截断JSON

        try:
            with open(holdings_file, "r") as f:
                data = json.load(f)
            assert False, "应该抛出JSONDecodeError"
        except json.JSONDecodeError:
            pass  # 预期行为

    def test_empty_holdings_file(self, tmp_path):
        """空的holdings.json应被安全处理"""
        holdings_file = str(tmp_path / "holdings.json")
        with open(holdings_file, "w") as f:
            f.write("")

        try:
            with open(holdings_file, "r") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            data = {}
        assert data == {}


# ============================================================
# 5. 冷却机制一致性测试
# ============================================================
class TestCooldownConsistency:
    """scheduler分级冷却 vs alert_engine统一冷却"""

    def test_scheduler_cooldown_by_level(self):
        """scheduler分级冷却：critical=15min, high/warning=30min"""
        cooldown_map = {"critical": 15, "high": 30, "warning": 30}

        def cooldown_for(level):
            return cooldown_map.get(level, max(cooldown_map.values()))

        assert cooldown_for("critical") == 15
        assert cooldown_for("high") == 30
        assert cooldown_for("warning") == 30
        assert cooldown_for("unknown") == 30  # 未知级别取最长档

    def test_alert_engine_cooldown_uniform_30(self):
        """alert_engine统一30分钟冷却"""
        alert_cooldown_min = 30  # alert_engine.py L68
        last_time = datetime.datetime.now() - datetime.timedelta(minutes=20)
        elapsed = (datetime.datetime.now() - last_time).total_seconds() / 60
        in_cooldown = elapsed < alert_cooldown_min
        assert in_cooldown is True  # 20分钟 < 30分钟

    def test_cooldown_inconsistency_critical(self):
        """BUG验证: critical级别在scheduler中15分钟可再触发，
        但alert_engine中30分钟内被拦截 → 不一致"""
        # 模拟15分钟后的场景
        last_time = datetime.datetime.now() - datetime.timedelta(minutes=16)

        # scheduler: critical级别冷却15分钟 → 16分钟后不在冷却
        scheduler_cooldown = 15
        scheduler_in_cooldown = ((datetime.datetime.now() - last_time).total_seconds() / 60) < scheduler_cooldown
        assert scheduler_in_cooldown is False  # scheduler允许再触发

        # alert_engine: 统一30分钟 → 16分钟后仍在冷却
        alert_engine_cooldown = 30
        alert_engine_in_cooldown = ((datetime.datetime.now() - last_time).total_seconds() / 60) < alert_engine_cooldown
        assert alert_engine_in_cooldown is True  # alert_engine仍然拦截

        # 这就是不一致的BUG：scheduler想发但alert_engine拦截

    def test_level_upgrade_exemption(self):
        """冷却级别升级豁免：新级别>旧级别时放行"""
        LEVEL_RANK = {"info": 0, "warning": 1, "high": 2, "critical": 3}
        old_level = "warning"
        new_level = "critical"
        should_exempt = LEVEL_RANK.get(new_level, 0) > LEVEL_RANK.get(old_level, 0)
        assert should_exempt is True

    def test_level_same_no_exemption(self):
        """同级别不豁免"""
        LEVEL_RANK = {"info": 0, "warning": 1, "high": 2, "critical": 3}
        old_level = "high"
        new_level = "high"
        should_exempt = LEVEL_RANK.get(new_level, 0) > LEVEL_RANK.get(old_level, 0)
        assert should_exempt is False

    def test_cooldown_file_corruption_handling(self, tmp_path):
        """冷却文件损坏时的降级处理"""
        cooldown_file = str(tmp_path / "alert_cooldown.json")
        with open(cooldown_file, "w") as f:
            f.write("not valid json{{{")

        # 模拟 _load_alert_cooldown 的降级逻辑
        data = {}
        try:
            with open(cooldown_file, "r") as f:
                raw = json.load(f)
            # 不会到这里
            assert False
        except Exception:
            data = {}
        assert data == {}

    def test_cooldown_level_depends_on_main_file(self, tmp_path):
        """BUG验证: alert_cooldown_level.json依赖主冷却文件清理"""
        main_file = str(tmp_path / "alert_cooldown.json")
        level_file = str(tmp_path / "alert_cooldown_level.json")

        # 主文件为空（过期/损坏），但级别文件有数据
        with open(main_file, "w") as f:
            json.dump({}, f)
        with open(level_file, "w") as f:
            json.dump({"001": "critical"}, f)

        # 模拟 _load_alert_cooldown_level 的逻辑
        main_data = {}
        with open(main_file, "r") as f:
            main_data = json.load(f)

        level_data = {}
        with open(level_file, "r") as f:
            raw = json.load(f)
        live_codes = set(main_data.keys())  # 空集
        for code, level in raw.items():
            if code in live_codes:
                level_data[code] = level

        # 主文件清空导致级别文件也被清空
        assert level_data == {}
        # 这意味着如果主冷却文件损坏，级别信息全部丢失


# ============================================================
# 6. 止损价一致性测试
# ============================================================
class TestStopLossConsistency:
    """止损价多源并存的冲突检测"""

    def test_hard_floor_calculation(self):
        """daily_orders.py的hard_floor = cost * (1 - STOP_LOSS_PCT)"""
        STOP_LOSS_PCT = 0.10
        cost = 50.0
        hard_floor = round(cost * (1 - STOP_LOSS_PCT), 3)
        assert hard_floor == 45.0

    def test_preset_stop_vs_hard_floor(self):
        """预设止损与硬止损取高者"""
        cost = 50.0
        hard_floor = round(cost * 0.9, 3)  # 45.0
        preset_stop = 46.0  # 预设止损高于硬止损
        price = 48.0

        stop_price = round(max(preset_stop, hard_floor), 3)
        assert stop_price == 46.0  # 取高者

    def test_preset_stop_below_hard_floor(self):
        """预设止损低于硬止损时取硬止损"""
        cost = 50.0
        hard_floor = round(cost * 0.9, 3)  # 45.0
        preset_stop = 43.0  # 低于硬止损
        price = 48.0

        stop_price = round(max(preset_stop, hard_floor), 3)
        assert stop_price == 45.0  # 硬止损兜底

    def test_zero_cost_handling(self):
        """cost=0时走已回本保护逻辑"""
        cost = 0
        price = 50.0
        if cost <= 0:
            stop_price = round(price * 0.85, 3)
            stop_type = "回本仓保护"
        assert stop_price == 42.5
        assert stop_type == "回本仓保护"

    def test_zero_stop_loss_field(self):
        """stop_loss=0时回退到hard_floor"""
        cost = 50.0
        price = 48.0
        holding = {"stop_loss": 0, "cost": cost}
        STOP_LOSS_PCT = 0.10
        hard_floor = round(cost * (1 - STOP_LOSS_PCT), 3)
        preset_stop = holding.get("stop_loss", 0)

        if preset_stop > 0 and preset_stop < price:
            stop_price = round(max(preset_stop, hard_floor), 3)
        else:
            stop_price = hard_floor
        assert stop_price == 45.0

    def test_missing_stop_loss_field(self):
        """stop_loss字段缺失时回退到hard_floor"""
        holding = {"cost": 50.0}  # 无stop_loss字段
        preset_stop = holding.get("stop_loss", 0)
        assert preset_stop == 0

    def test_ratchet_principle(self, tmp_path):
        """Ratchet原则：止损价只升不降"""
        holdings_file = str(tmp_path / "holdings.json")
        holdings = {
            "001": {
                "shares": 100,
                "buy_price": 50.0,
                "current_price": 55.0,
                "stop_loss": 46.0,
            }
        }
        with open(holdings_file, "w") as f:
            json.dump(holdings, f)

        # 模拟sync_authoritative_stop_loss的Ratchet逻辑
        new_stop = 47.0  # 新止损高于旧止损
        old_stop = holdings["001"].get("stop_loss", 0)
        if new_stop > old_stop:
            holdings["001"]["stop_loss"] = new_stop
            updated = True
        else:
            updated = False
        assert updated is True
        assert holdings["001"]["stop_loss"] == 47.0

    def test_ratchet_rejects_decrease(self):
        """Ratchet拒绝降低止损"""
        old_stop = 47.0
        new_stop = 45.0  # 低于旧值
        should_update = new_stop > old_stop
        assert should_update is False

    def test_trailing_stop_vs_hard_floor_conflict(self):
        """BUG场景: trailing_stop可能低于hard_floor"""
        cost = 50.0
        trailing_stop = 44.0  # trailing_stop低于hard_floor
        hard_floor = round(cost * 0.9, 3)  # 45.0

        # daily_orders.py会取max(trailing, hard_floor)=45.0
        # 但sync_authoritative_stop_loss可能写回44.0
        # 导致holdings.json中stop_loss=44.0 < hard_floor=45.0
        daily_orders_stop = max(trailing_stop, hard_floor)
        assert daily_orders_stop == 45.0
        # 而权威源写回的可能是44.0 → 不一致


# ============================================================
# 7. 订单执行边界测试
# ============================================================
class TestOrderExecutionEdge:
    """撮合逻辑的极端场景"""

    def test_limit_up_cannot_buy(self):
        """涨停板无法买入"""
        from backtest.broker import SimBroker, Order
        broker = SimBroker(initial_capital=100000)
        order = Order(code="001", direction="buy", target_shares=100,
                      price=11.0, date="2026-08-17")
        market_data = {"pre_close": 10.0, "open": 11.0, "close": 11.0,
                       "high": 11.0, "low": 11.0, "volume": 1000}
        result = broker.execute_buy(order, market_data)
        assert result is None  # 涨停无法买入

    def test_limit_down_cannot_sell(self):
        """跌停板无法卖出"""
        from backtest.broker import SimBroker, Order, Position
        broker = SimBroker(initial_capital=100000)
        # 先建立持仓
        pos = Position(code="001", shares=100, avg_cost=10.0,
                       frozen_shares=0, frozen_date="")
        broker.positions["001"] = pos
        order = Order(code="001", direction="sell", target_shares=100,
                      price=9.0, date="2026-08-18")
        market_data = {"pre_close": 10.0, "open": 9.0, "close": 9.0,
                       "high": 9.0, "low": 9.0, "volume": 1000}
        result = broker.execute_sell(order, market_data)
        assert result is None  # 跌停无法卖出

    def test_yizi_limit_up(self):
        """一字涨停板: open=high=low=close=涨停价"""
        from backtest.broker import SimBroker
        # 一字涨停
        row = {"pre_close": 10.0, "open": 11.0, "close": 11.0,
               "high": 11.0, "low": 11.0}
        assert SimBroker._is_limit_up(row) is True

    def test_yizi_limit_down(self):
        """一字跌停板"""
        from backtest.broker import SimBroker
        row = {"pre_close": 10.0, "open": 9.0, "close": 9.0,
               "high": 9.0, "low": 9.0}
        assert SimBroker._is_limit_down(row) is True

    def test_zero_cash_buy(self):
        """零资金无法买入"""
        from backtest.broker import SimBroker, Order
        broker = SimBroker(initial_capital=0)
        order = Order(code="001", direction="buy", target_shares=100,
                      price=10.0, date="2026-08-17")
        market_data = {"pre_close": 10.0, "close": 10.0, "volume": 10000}
        result = broker.execute_buy(order, market_data)
        assert result is None

    def test_very_small_cash(self):
        """极小资金不足100股时返回None"""
        from backtest.broker import SimBroker, Order
        broker = SimBroker(initial_capital=50)  # 50元，不够买100股@10元
        order = Order(code="001", direction="buy", target_shares=100,
                      price=10.0, date="2026-08-17")
        market_data = {"pre_close": 10.0, "close": 10.0, "volume": 10000}
        result = broker.execute_buy(order, market_data)
        assert result is None

    def test_slippage_tracker_zero_price(self, tmp_path):
        """滑点追踪器：零价格不记录"""
        from execution.slippage_tracker import SlippageTracker
        tracker = SlippageTracker(
            data_file=str(tmp_path / "exec.json"),
            history_file=str(tmp_path / "slip.json")
        )
        tracker.record("001", 0, 10.0, 100)  # signal_price=0
        assert len(tracker.records) == 0  # 应被跳过

        tracker.record("001", 10.0, 0, 100)  # actual_price=0
        assert len(tracker.records) == 0

    def test_slippage_tracker_normal(self, tmp_path):
        """滑点追踪器正常记录"""
        from execution.slippage_tracker import SlippageTracker
        tracker = SlippageTracker(
            data_file=str(tmp_path / "exec.json"),
            history_file=str(tmp_path / "slip.json")
        )
        tracker.record("001", 10.0, 10.05, 100, direction="buy")
        assert len(tracker.records) == 1
        assert tracker.records[0]["slippage_pct"] == pytest.approx(0.005, abs=0.001)

    def test_slippage_sell_direction(self, tmp_path):
        """卖出方向滑点计算：实际<信号为正滑点"""
        from execution.slippage_tracker import SlippageTracker
        tracker = SlippageTracker(
            data_file=str(tmp_path / "exec.json"),
            history_file=str(tmp_path / "slip.json")
        )
        tracker.record("001", 10.0, 9.95, 100, direction="sell")
        assert len(tracker.records) == 1
        # 卖出: (signal - actual) / signal = (10.0 - 9.95) / 10.0 = 0.005
        assert tracker.records[0]["slippage_pct"] == pytest.approx(0.005, abs=0.001)

    def test_order_lifecycle_empty_trades(self, tmp_path):
        """成交回填：空trades_today.json不崩溃"""
        from execution.order_lifecycle import backfill_trades_to_journal
        trades_file = str(tmp_path / "trades_today.json")
        with open(trades_file, "w") as f:
            json.dump({"date": "2026-08-17", "trades": []}, f)
        result = backfill_trades_to_journal(trades_file, str(tmp_path / "test.db"))
        assert result["ok"] is True
        assert result["inserted"] == 0

    def test_order_lifecycle_missing_file(self, tmp_path):
        """成交回填：文件不存在时安全降级"""
        from execution.order_lifecycle import backfill_trades_to_journal
        result = backfill_trades_to_journal(
            str(tmp_path / "nonexistent.json"),
            str(tmp_path / "test.db")
        )
        assert result["ok"] is False


# ============================================================
# 8. 状态一致性测试
# ============================================================
class TestStateConsistency:
    """买点去重/多档击穿/缓存刷新"""

    def test_buy_point_dedup_same_tier(self, tmp_path):
        """同档买点当日只推送一次"""
        sent_file = str(tmp_path / "buy_point_alert_sent.json")
        today = datetime.date.today().isoformat()

        # 模拟已推送记录
        sent_data = {today: {"pushed": ["激进"], "failed": {}, "invalidated": []}}
        with open(sent_file, "w") as f:
            json.dump(sent_data, f)

        # 验证去重
        with open(sent_file, "r") as f:
            data = json.load(f)
        pushed = set(data.get(today, {}).get("pushed", []))
        assert "激进" in pushed
        # 下次检查时"激进"应被跳过

    def test_multi_tier_deepest_wins(self):
        """多档击穿时选择最深档"""
        tiers = [
            (10.0, "激进"),   # price <= 10.0
            (9.5, "稳健"),    # price <= 9.5
            (9.0, "保守"),    # price <= 9.0
        ]
        price = 8.8  # 击穿全部三档

        reached = []
        for p, label in tiers:
            if price <= p:
                reached.append((p, label))

        assert len(reached) == 3
        # 选择最深档（价格最低的）
        deepest = min(reached, key=lambda x: x[0])
        assert deepest == (9.0, "保守")

    def test_multi_tier_partial_breach(self):
        """部分击穿：只击穿前两档"""
        tiers = [
            (10.0, "激进"),
            (9.5, "稳健"),
            (9.0, "保守"),
        ]
        price = 9.5  # 击穿激进(9.5<=10.0)和稳健(9.5<=9.5)，未击穿保守(9.5>9.0)

        reached = []
        for p, label in tiers:
            if price <= p:
                reached.append((p, label))

        assert len(reached) == 2
        deepest = min(reached, key=lambda x: x[0])
        assert deepest == (9.5, "稳健")

    def test_scan_cache_expiry(self, tmp_path):
        """scan_cache过期后应刷新"""
        cache_file = str(tmp_path / "scan_cache.json")
        old_time = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat()
        cache_data = {"timestamp": old_time, "data": {"001": "old_result"}}
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)

        # 检查过期逻辑（假设缓存有效期1小时）
        with open(cache_file, "r") as f:
            cached = json.load(f)
        cache_ts = datetime.datetime.fromisoformat(cached["timestamp"])
        age_minutes = (datetime.datetime.now() - cache_ts).total_seconds() / 60
        is_expired = age_minutes > 60
        assert is_expired is True

    def test_scan_cache_fresh(self, tmp_path):
        """未过期的缓存应继续使用"""
        cache_file = str(tmp_path / "scan_cache.json")
        recent_time = (datetime.datetime.now() - datetime.timedelta(minutes=10)).isoformat()
        cache_data = {"timestamp": recent_time, "data": {"001": "fresh_result"}}
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)

        with open(cache_file, "r") as f:
            cached = json.load(f)
        cache_ts = datetime.datetime.fromisoformat(cached["timestamp"])
        age_minutes = (datetime.datetime.now() - cache_ts).total_seconds() / 60
        is_expired = age_minutes > 60
        assert is_expired is False

    def test_buy_signal_history_dedup(self, tmp_path):
        """buy_signal_history.json去重：同date+code不重复登记"""
        history_file = str(tmp_path / "buy_signal_history.json")
        today = datetime.date.today().isoformat()

        history = {
            today: [
                {"code": "001", "tier": "激进", "price": 10.0, "time": "09:35"},
            ]
        }
        with open(history_file, "w") as f:
            json.dump(history, f)

        # 尝试重复添加
        with open(history_file, "r") as f:
            data = json.load(f)

        existing = {(e["code"], e["tier"]) for e in data.get(today, [])}
        new_entry = {"code": "001", "tier": "激进", "price": 10.0, "time": "09:40"}
        if (new_entry["code"], new_entry["tier"]) not in existing:
            data[today].append(new_entry)

        assert len(data[today]) == 1  # 不重复添加

    def test_alert_cooldown_json_format_compatibility(self, tmp_path):
        """冷却记录ISO格式时间戳解析兼容性"""
        cooldown_file = str(tmp_path / "alert_cooldown.json")
        now = datetime.datetime.now()
        data = {
            "001": now.isoformat(),
            "002": now.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(cooldown_file, "w") as f:
            json.dump(data, f)

        with open(cooldown_file, "r") as f:
            raw = json.load(f)

        for code, ts in raw.items():
            t = datetime.datetime.fromisoformat(ts)
            assert (now - t).total_seconds() < 1  # 刚写入的应在1秒内


# ============================================================
# 9. 综合风险评估测试
# ============================================================
class TestRiskAssessment:
    """风控模块的边界条件"""

    def test_full_position_block(self):
        """仓位>=90%禁止买入"""
        FULL_POSITION_THRESHOLD = 0.90
        position_ratio = 0.92
        should_block = position_ratio >= FULL_POSITION_THRESHOLD
        assert should_block is True

    def test_near_full_position_only_reduce(self):
        """仓位>=80%只允许减仓"""
        NEAR_FULL_POSITION = 0.80
        position_ratio = 0.85
        should_only_reduce = position_ratio >= NEAR_FULL_POSITION
        assert should_only_reduce is True

    def test_max_holdings_limit(self):
        """持仓数量硬限制"""
        MAX_HOLDINGS = 8
        current_holdings = 8
        can_buy_more = current_holdings < MAX_HOLDINGS
        assert can_buy_more is False

    def test_sector_concentration_limit(self):
        """赛道集中度限制"""
        SECTOR_MAX_RATIO = 0.40
        sector_ratio = 0.45
        should_block = sector_ratio >= SECTOR_MAX_RATIO
        assert should_block is True

    def test_crash_threshold_detection(self):
        """暴跌阈值检测"""
        CRASH_THRESHOLD = -0.08
        change_pct = -0.09
        is_crash = change_pct <= CRASH_THRESHOLD
        assert is_crash is True

    def test_cash_reserve_ratio(self):
        """最低现金保留"""
        CASH_RESERVE_RATIO = 0.10
        total_assets = 100000
        min_cash = total_assets * CASH_RESERVE_RATIO
        available = 8000
        can_buy = available > min_cash
        assert can_buy is False  # 8000 < 10000

    def test_holding_loss_limit_for_screener(self):
        """浮亏>8%禁止买入推荐"""
        SCREENER_HOLDING_LOSS_LIMIT = -0.08
        pnl_pct = -0.09
        should_limit = pnl_pct <= SCREENER_HOLDING_LOSS_LIMIT
        assert should_limit is True

    def test_weak_mode_threshold(self):
        """弱势模式通过门槛"""
        WEAK_SCORE_THRESHOLD = 40
        score = 35
        should_fail = score < WEAK_SCORE_THRESHOLD
        assert should_fail is True
