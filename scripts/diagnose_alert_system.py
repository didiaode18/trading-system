# -*- coding: utf-8 -*-
"""
预警系统全面健康诊断脚本
========================
逐项检查10大模块，输出 ✅正常 / ⚠️异常 / ❌失效 状态判定
"""
import os
import sys
import json
import datetime
import importlib

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trading_system")
sys.path.insert(0, PROJECT_ROOT)

# 强制UTF-8输出
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import config

results = []  # (模块名, 状态, 详情)

def report(module_name, status, details):
    """status: ✅ / ⚠️ / ❌"""
    results.append((module_name, status, details))
    print(f"\n{'='*60}")
    print(f"  {status}  {module_name}")
    print(f"{'='*60}")
    for d in details:
        print(f"    {d}")


# ============================================================
# 1. 调度器状态
# ============================================================
def check_scheduler():
    details = []
    status = "✅"
    # 心跳文件
    hb_file = os.path.join(PROJECT_ROOT, "output", ".scheduler_heartbeat")
    pid_file = os.path.join(PROJECT_ROOT, "output", ".scheduler.pid")
    
    if os.path.exists(hb_file):
        try:
            with open(hb_file, 'r') as f:
                content = f.read().strip()
            ts_str = content.split("|")[0]
            hb_pid = content.split("|")[1] if "|" in content else "?"
            last_beat = datetime.datetime.fromisoformat(ts_str)
            age_sec = (datetime.datetime.now() - last_beat).total_seconds()
            details.append(f"心跳文件: 存在 | 最后更新: {ts_str[:19]} | PID: {hb_pid} | 距今: {age_sec:.0f}秒")
            if age_sec > 320:
                details.append(f"⚠️ 心跳已超过320秒阈值({age_sec:.0f}秒)，可能被判定为死亡")
                status = "⚠️"
        except Exception as e:
            details.append(f"❌ 心跳文件读取失败: {e}")
            status = "❌"
    else:
        details.append("❌ 心跳文件不存在")
        status = "❌"
    
    if os.path.exists(pid_file):
        try:
            with open(pid_file, 'r') as f:
                pid = f.read().strip()
            details.append(f"PID文件: {pid}")
            # 检查进程是否存活
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                details.append(f"进程 {pid}: 存活")
            else:
                details.append(f"⚠️ 进程 {pid}: 已终止(PID文件残留)")
                if status == "✅":
                    status = "⚠️"
        except Exception as e:
            details.append(f"PID检查异常: {e}")
    else:
        details.append("⚠️ PID文件不存在")
    
    # 检查调度器注册的定时任务
    try:
        from scheduler import _register_all_schedules
        import scheduler as sched_mod
        # 检查关键函数是否存在
        key_funcs = ['run_unified_intraday_alert', '_gated_buy_point_check', 
                     'run_morning_report', 'run_evening_report', 'run_condition_orders']
        found = [f for f in key_funcs if hasattr(sched_mod, f) or f in dir(sched_mod)]
        details.append(f"关键调度函数: {len(found)}/{len(key_funcs)} 已定义")
    except Exception as e:
        details.append(f"调度模块加载异常: {e}")
    
    report("1. 调度器状态", status, details)


# ============================================================
# 2. 盘中预警引擎 (alert_engine.py)
# ============================================================
def check_alert_engine():
    details = []
    status = "✅"
    
    try:
        from notify.alert_engine import AlertEngine, ALERT_CONFIG
        details.append(f"AlertEngine类: 加载成功")
        details.append(f"ALERT_CONFIG检查间隔: {ALERT_CONFIG.get('check_interval_min', '?')}分钟")
        details.append(f"冷却时间: {ALERT_CONFIG.get('alert_cooldown_min', '?')}分钟")
        
        # 检查规则定义
        engine_source = os.path.join(PROJECT_ROOT, "notify", "alert_engine.py")
        if os.path.exists(engine_source):
            with open(engine_source, 'r', encoding='utf-8') as f:
                src = f.read()
            rules = []
            for i in range(1, 19):
                if f"R{i}." in src or f"r{i}_" in src.lower() or f"rule_r{i}" in src.lower():
                    rules.append(f"R{i}")
            details.append(f"预警规则: 文档定义R1-R18, 源码匹配: {', '.join(rules) if rules else '未匹配'}")
        
        # 尝试实例化
        try:
            engine = AlertEngine()
            details.append(f"AlertEngine实例化: 成功")
        except Exception as e:
            details.append(f"⚠️ AlertEngine实例化失败: {e}")
            status = "⚠️"
        
        # 检查持仓数据加载
        try:
            holdings_file = config.get_holdings_file()
            if os.path.exists(holdings_file):
                with open(holdings_file, 'r', encoding='utf-8') as f:
                    holdings = json.load(f)
                active = {c: v for c, v in holdings.items() if isinstance(v, dict) and v.get("shares", 0) > 0}
                details.append(f"持仓文件: {os.path.basename(holdings_file)} | 总标的: {len(holdings)} | 有效持仓: {len(active)}")
                if not active:
                    details.append("⚠️ 有效持仓为0，预警引擎将无监控目标")
                    status = "⚠️"
            else:
                details.append(f"❌ 持仓文件不存在: {holdings_file}")
                status = "❌"
        except Exception as e:
            details.append(f"❌ 持仓数据加载失败: {e}")
            status = "❌"
        
        # 检查股票池
        pool_file = os.path.join(config.PROJECT_ROOT, "stock_pool.json")
        if os.path.exists(pool_file):
            with open(pool_file, 'r', encoding='utf-8') as f:
                pool = json.load(f)
            core = len(pool.get("core_pool", {}))
            watch = len(pool.get("watch_pool", {}))
            details.append(f"股票池: core_pool={core}只 | watch_pool={watch}只")
        else:
            details.append("⚠️ stock_pool.json 不存在")
        
        # 持久化文件
        data_dir = os.path.join(PROJECT_ROOT, "data")
        persist_files = {
            "alert_stop_buffer.json": "止损缓冲",
            "r17_tier_memory.json": "R17档位记忆",
            "r18_daily_memory.json": "R18每日去重",
            "deep_loss_daily_memory.json": "深亏模式记忆",
            "alert_cooldown.json": "预警冷却",
        }
        for fname, desc in persist_files.items():
            fpath = os.path.join(data_dir, fname)
            if os.path.exists(fpath):
                details.append(f"持久化[{desc}]: {fname} 存在")
            else:
                details.append(f"持久化[{desc}]: {fname} 不存在(首次运行时自动创建)")
        
    except ImportError as e:
        details.append(f"❌ alert_engine 导入失败: {e}")
        status = "❌"
    except Exception as e:
        details.append(f"❌ 检查异常: {e}")
        status = "❌"
    
    report("2. 盘中预警引擎 (AlertEngine)", status, details)


# ============================================================
# 3. 盘中监控模块 (strategy/intraday_monitor.py)
# ============================================================
def check_intraday_monitor():
    details = []
    status = "✅"
    
    # 注意: monitor/intraday_monitor.py 是死代码，实际用 strategy/intraday_monitor.py
    monitor_path = os.path.join(PROJECT_ROOT, "strategy", "intraday_monitor.py")
    dead_path = os.path.join(PROJECT_ROOT, "monitor", "intraday_monitor.py")
    
    if os.path.exists(dead_path):
        with open(dead_path, 'r', encoding='utf-8') as f:
            first_lines = f.read(300)
        if "死代码" in first_lines or "不要在此新增" in first_lines:
            details.append("monitor/intraday_monitor.py: 已标记为死代码(正确)")
        else:
            details.append("⚠️ monitor/intraday_monitor.py 未标记为死代码")
    
    try:
        from strategy.intraday_monitor import IntradayMonitor
        details.append("strategy.intraday_monitor.IntradayMonitor: 加载成功")
        
        # 实例化
        try:
            monitor = IntradayMonitor(holdings={})
            details.append(f"实例化: 成功 | 止损预警={monitor.STOP_LOSS_ALERT}")
            details.append(f"  急跌阈值: {monitor.RAPID_DROP_PCT} | 窗口: {monitor.RAPID_DROP_WINDOW}分钟")
            details.append(f"  大盘急跌: {monitor.MARKET_DROP_PCT} | 振幅阈值: {monitor.AMPLITUDE_ALERT_PCT}")
            details.append(f"  反洗盘缓冲: {monitor.BUFFER_MINUTES}分钟 | V反保护: {monitor.V_REVERSAL_PCT}")
            details.append(f"  软止损模式: {monitor.SOFT_STOP_MODE}")
            details.append(f"  梯度减仓配置: {len(monitor.gradient_cfg.get('levels', []))}档")
        except Exception as e:
            details.append(f"⚠️ 实例化失败: {e}")
            status = "⚠️"
        
        # 状态持久化文件
        state_file = os.path.join(PROJECT_ROOT, "data", "intraday_state.json")
        if os.path.exists(state_file):
            details.append(f"状态持久化: intraday_state.json 存在")
        else:
            details.append("状态持久化: intraday_state.json 不存在(首次运行时创建)")
        
    except ImportError as e:
        details.append(f"❌ 导入失败: {e}")
        status = "❌"
    
    report("3. 盘中监控模块 (IntradayMonitor)", status, details)


# ============================================================
# 4. 预警效果评估器 (alert_evaluator.py)
# ============================================================
def check_alert_evaluator():
    details = []
    status = "✅"
    
    try:
        from monitor.alert_evaluator import AlertEvaluator, EVAL_DB_PATH
        details.append(f"AlertEvaluator: 加载成功")
        details.append(f"持久化路径: {EVAL_DB_PATH}")
        
        # 检查文件可读写
        if os.path.exists(EVAL_DB_PATH):
            try:
                with open(EVAL_DB_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                records = data.get("records", [])
                pending = data.get("pending", [])
                details.append(f"已有记录: {len(records)}条 | 待验证: {len(pending)}条")
            except Exception as e:
                details.append(f"⚠️ 读取失败: {e}")
                status = "⚠️"
        else:
            details.append("评估历史文件不存在(首次运行时创建)")
        
        # 实例化测试
        try:
            evaluator = AlertEvaluator()
            details.append(f"实例化: 成功 | records={len(evaluator.records)} | pending={len(evaluator._pending)}")
            
            # 测试写入
            test_path = EVAL_DB_PATH + ".test"
            try:
                os.makedirs(os.path.dirname(EVAL_DB_PATH), exist_ok=True)
                with open(test_path, 'w', encoding='utf-8') as f:
                    json.dump({"test": True}, f)
                os.remove(test_path)
                details.append("写入测试: 通过(目录可写)")
            except Exception as e:
                details.append(f"⚠️ 写入测试失败: {e}")
                status = "⚠️"
        except Exception as e:
            details.append(f"⚠️ 实例化失败: {e}")
            status = "⚠️"
        
        # verify_pending方法
        if hasattr(evaluator, 'verify_pending'):
            details.append("verify_pending补调方法: 存在")
        else:
            details.append("⚠️ verify_pending方法不存在")
        
        if hasattr(evaluator, 'record_alert'):
            details.append("record_alert方法: 存在")
        else:
            details.append("⚠️ record_alert方法不存在")
            
        if hasattr(evaluator, 'get_daily_report'):
            details.append("get_daily_report方法: 存在")
        else:
            details.append("⚠️ get_daily_report方法不存在")
        
    except ImportError as e:
        details.append(f"❌ 导入失败: {e}")
        status = "❌"
    
    report("4. 预警效果评估器 (AlertEvaluator)", status, details)


# ============================================================
# 5. 预警发送台账 (alert_ledger.py)
# ============================================================
def check_alert_ledger():
    details = []
    status = "✅"
    
    try:
        from notify.alert_ledger import (DEFAULT_LEDGER_PATH, LEDGER_WINDOW_DAYS,
                                          record_sent, aggregate_stats, _load_ledger, _save_ledger)
        details.append(f"alert_ledger: 加载成功")
        details.append(f"默认台账路径: {DEFAULT_LEDGER_PATH}")
        details.append(f"保留窗口: {LEDGER_WINDOW_DAYS}天")
        
        # 检查目录可达
        ledger_dir = os.path.dirname(DEFAULT_LEDGER_PATH)
        if os.path.isdir(ledger_dir):
            details.append(f"台账目录: {ledger_dir} 可达")
        else:
            details.append(f"⚠️ 台账目录不存在: {ledger_dir}")
            status = "⚠️"
        
        # 检查现有台账
        if os.path.exists(DEFAULT_LEDGER_PATH):
            records = _load_ledger(DEFAULT_LEDGER_PATH)
            details.append(f"现有台账记录: {len(records)}条")
        else:
            details.append("台账文件不存在(首次记录时创建)")
        
        # 写入测试
        try:
            test_records = [{"ts": "2026-01-01 00:00:00", "code": "test", "level": "warning",
                           "rule_name": "test", "urgency_score": 50}]
            _save_ledger(test_records, DEFAULT_LEDGER_PATH + ".test")
            test_loaded = _load_ledger(DEFAULT_LEDGER_PATH + ".test")
            os.remove(DEFAULT_LEDGER_PATH + ".test")
            if len(test_loaded) == 1:
                details.append("读写测试: 通过")
            else:
                details.append("⚠️ 读写测试: 数据不一致")
                status = "⚠️"
        except Exception as e:
            details.append(f"⚠️ 写入测试失败: {e}")
            status = "⚠️"
        
        # 检查aggregate_stats
        if hasattr(aggregate_stats, '__call__'):
            details.append("aggregate_stats聚合方法: 存在")
        
    except ImportError as e:
        details.append(f"❌ 导入失败: {e}")
        status = "❌"
    
    report("5. 预警发送台账 (alert_ledger)", status, details)


# ============================================================
# 6. 冷却与升级机制
# ============================================================
def check_cooldown_escalation():
    details = []
    status = "✅"
    
    # 分级冷却配置
    cooldown = getattr(config, 'ALERT_COOLDOWN_MINUTES', None)
    if cooldown and isinstance(cooldown, dict):
        details.append(f"分级冷却: critical={cooldown.get('critical', '?')}min | high={cooldown.get('high', '?')}min | warning={cooldown.get('warning', '?')}min")
    else:
        details.append("⚠️ ALERT_COOLDOWN_MINUTES 未配置或格式错误")
        status = "⚠️"
    
    # 变频升级配置
    esc = getattr(config, 'INTRADAY_ESCALATION_CONFIG', None)
    if esc:
        details.append(f"变频配置: 正常={esc.get('normal_interval_min', '?')}min | 预警={esc.get('warning_interval_min', '?')}min | 紧急={esc.get('emergency_interval_min', '?')}min")
        
        wt = esc.get('warning_triggers', {})
        et = esc.get('emergency_triggers', {})
        details.append(f"升级触发(warning): 持仓跌{wt.get('holding_drop_pct', '?')}% / 大盘跌{wt.get('market_drop_pct', '?')}% / 距止损{wt.get('approaching_stop_loss', '?')}")
        details.append(f"升级触发(emergency): 持仓跌{et.get('holding_drop_pct', '?')}% / 触及止损={et.get('stop_loss_touched', '?')} / 大盘跌{et.get('market_drop_pct', '?')}%")
        
        downgrade = esc.get('downgrade_after_clear', 0)
        details.append(f"降级条件: 连续{downgrade}次无异常 → 降频")
        
        suppress = esc.get('emergency_suppress_after_downgrade_min', 0)
        details.append(f"降级后抑制窗口: {suppress}分钟(防乒乓回升)")
    else:
        details.append("⚠️ INTRADAY_ESCALATION_CONFIG 未配置")
        status = "⚠️"
    
    # 冷却状态文件
    cooldown_file = os.path.join(PROJECT_ROOT, "data", "alert_cooldown.json")
    if os.path.exists(cooldown_file):
        try:
            with open(cooldown_file, 'r', encoding='utf-8') as f:
                cd_data = json.load(f)
            details.append(f"冷却状态文件: 存在 | 记录数: {len(cd_data)}")
        except Exception as e:
            details.append(f"⚠️ 冷却文件读取失败: {e}")
    else:
        details.append("冷却状态文件: 不存在(首次触发时创建)")
    
    # 冷却级别文件
    cd_level_file = os.path.join(PROJECT_ROOT, "data", "alert_cooldown_level.json")
    if os.path.exists(cd_level_file):
        details.append(f"冷却级别文件: 存在")
    
    # ACK机制
    ack_enabled = getattr(config, 'ALERT_ACK_ENABLED', False)
    details.append(f"ACK确认机制: {'开启' if ack_enabled else '关闭'}")
    
    report("6. 冷却与升级机制", status, details)


# ============================================================
# 7. 通知发送通道
# ============================================================
def check_notification_channels():
    details = []
    status = "✅"
    
    # 邮件SMTP
    sender = getattr(config, 'EMAIL_SENDER', '')
    auth = getattr(config, 'EMAIL_AUTH_CODE', '')
    receiver = getattr(config, 'EMAIL_RECEIVER', '')
    host = getattr(config, 'EMAIL_SMTP_HOST', '')
    port = getattr(config, 'EMAIL_SMTP_PORT', 0)
    timeout = getattr(config, 'EMAIL_SMTP_TIMEOUT', 30)
    
    if sender and auth:
        details.append(f"邮件SMTP: {host}:{port} | 发件人: {sender[:6]}*** | 收件人: {receiver[:6]}*** | 超时: {timeout}s")
        # SMTP连通性测试
        try:
            import smtplib
            import socket
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(5)
            try:
                smtp = smtplib.SMTP_SSL(host, port, timeout=5)
                smtp.ehlo()
                smtp.quit()
                details.append("SMTP连通性: 通过(SSL连接成功)")
            except Exception as e:
                details.append(f"⚠️ SMTP连通性: 失败({e})")
                status = "⚠️"
            finally:
                socket.setdefaulttimeout(old_timeout)
        except Exception as e:
            details.append(f"SMTP测试异常: {e}")
    else:
        details.append(f"❌ 邮件未配置: sender={'有' if sender else '无'} | auth={'有' if auth else '无'}")
        status = "❌"
    
    # 钉钉Webhook
    webhook = getattr(config, 'DINGTALK_WEBHOOK', '')
    if webhook:
        details.append(f"钉钉Webhook: 已配置({webhook[:50]}...)")
        # 简单检查URL格式
        if "oapi.dingtalk.com" in webhook and "access_token=" in webhook:
            details.append("钉钉URL格式: 合法")
        else:
            details.append("⚠️ 钉钉URL格式可能不正确")
            status = "⚠️"
    else:
        details.append("⚠️ 钉钉Webhook未配置(邮件仍可发送)")
    
    # 企业微信
    wechat = getattr(config, 'WECHAT_WORK_WEBHOOK', '')
    if wechat:
        details.append(f"企业微信: 已配置")
    else:
        details.append("企业微信: 未配置(非必须)")
    
    # 通知分级路由
    bp_only = getattr(config, 'BUY_POINT_ALERT_DINGTALK_ONLY', False)
    alert_min = getattr(config, 'ALERT_DINGTALK_MIN_LEVEL', 'high')
    details.append(f"通知路由: 买点=钉钉{'仅' if bp_only else '+邮件'} | 预警钉钉最低级别={alert_min}")
    
    # 异步发送
    async_send = getattr(config, 'ALERT_EMAIL_ASYNC', True)
    details.append(f"预警邮件异步发送: {'开启' if async_send else '关闭'}")
    
    report("7. 通知发送通道", status, details)


# ============================================================
# 8. 大盘门控与降级
# ============================================================
def check_market_regime():
    details = []
    status = "✅"
    
    # 大盘状态识别配置
    regime_cfg = getattr(config, 'STRATEGY_CONFIG', {}).get('market_regime', {})
    if regime_cfg.get('enabled', False):
        details.append(f"大盘状态识别: 已启用 | 牛市阈值={regime_cfg.get('bull_threshold')} | 熊市阈值={regime_cfg.get('bear_threshold')}")
    else:
        details.append("大盘状态识别: 未启用(STRATEGY_CONFIG.market_regime.enabled=False)")
    
    # 买点大盘门控
    try:
        from notify.buy_point_alert import _market_gate
        details.append("买点大盘闸门(_market_gate): 函数存在")
    except ImportError:
        details.append("⚠️ buy_point_alert._market_gate 导入失败")
        status = "⚠️"
    
    # 大盘环境过滤
    canslim = getattr(config, 'CANSLIM_CONFIG', {}) if hasattr(config, 'CANSLIM_CONFIG') else {}
    market_state_allow = canslim.get('market_state_allow', {})
    details.append(f"CANSLIM允许市场状态: {market_state_allow}")
    
    # 波动率突变检测
    vol_regime = getattr(config, 'VOLATILITY_REGIME_CONFIG', {})
    vol_enabled = vol_regime.get('enabled', False)
    details.append(f"波动率突变检测: {'启用' if vol_enabled else '禁用'}")
    
    # 大盘急跌阈值
    monitor_cfg = getattr(config, 'MONITOR_CONFIG', {})
    details.append(f"大盘急跌阈值: {monitor_cfg.get('market_drop_pct', '?')}")
    
    # 反洗盘-大盘联动
    anti_wash = getattr(config, 'ANTI_WASH_CONFIG', {})
    passive_market = anti_wash.get('passive_decline_market', -1.0)
    details.append(f"被动下跌大盘阈值: 大盘跌>{abs(passive_market)}%时个股下跌视为被动")
    
    report("8. 大盘门控与降级", status, details)


# ============================================================
# 9. 条件单联动
# ============================================================
def check_condition_orders():
    details = []
    status = "✅"
    
    # 检查 _generate_intraday_stop_orders 函数
    try:
        from scheduler import _generate_intraday_stop_orders
        details.append("_generate_intraday_stop_orders: 函数存在")
    except ImportError:
        details.append("❌ _generate_intraday_stop_orders 导入失败")
        status = "❌"
    
    # 梯度减仓条件单生成
    grad_cfg = getattr(config, 'GRADIENT_REDUCE_CONFIG', {})
    gen_order = grad_cfg.get('generate_condition_order', False)
    details.append(f"梯度减仓自动生成条件单: {'开启' if gen_order else '关闭'}")
    
    levels = grad_cfg.get('levels', [])
    for lv in levels:
        details.append(f"  梯度档位: 浮亏{lv.get('loss_pct', '?')*100:.0f}% → 减仓{lv.get('reduce_ratio', 0)*100:.0f}% ({lv.get('level', '?')})")
    
    deep_loss = grad_cfg.get('deep_loss', {})
    details.append(f"深亏模式: {'启用' if deep_loss.get('enabled') else '关闭'} | 阈值={deep_loss.get('threshold', '?')}")
    
    # 输出目录
    output_dir = getattr(config, 'OUTPUT_DIR', '')
    if os.path.isdir(output_dir):
        details.append(f"输出目录: {output_dir} 可达")
        # 检查今日orders文件
        today_str = datetime.date.today().strftime("%Y%m%d")
        orders_file = os.path.join(output_dir, f"orders_{today_str}.json")
        if os.path.exists(orders_file):
            try:
                with open(orders_file, 'r', encoding='utf-8') as f:
                    orders_data = json.load(f)
                n_orders = len(orders_data.get("orders", []))
                details.append(f"今日条件单: orders_{today_str}.json | {n_orders}条")
            except Exception as e:
                details.append(f"⚠️ 今日条件单读取失败: {e}")
        else:
            details.append(f"今日条件单: 未生成(盘前/条件单任务后生成)")
    else:
        details.append(f"⚠️ 输出目录不存在: {output_dir}")
        status = "⚠️"
    
    report("9. 条件单联动", status, details)


# ============================================================
# 10. 买点快路径
# ============================================================
def check_buy_point_fast():
    details = []
    status = "✅"
    
    enabled = getattr(config, 'BUY_POINT_ALERT_ENABLED', False)
    fast_enabled = getattr(config, 'BUY_POINT_FAST_CHECK_ENABLED', False)
    interval = getattr(config, 'BUY_POINT_FAST_INTERVAL_MIN', 5)
    
    details.append(f"买点提醒总开关(BUY_POINT_ALERT_ENABLED): {'开启' if enabled else '关闭'}")
    details.append(f"快路径开关(BUY_POINT_FAST_CHECK_ENABLED): {'开启' if fast_enabled else '关闭'}")
    details.append(f"快路径间隔: {interval}分钟")
    
    if not enabled:
        details.append("⚠️ 买点提醒总开关关闭，快路径不会执行")
        status = "⚠️"
    
    if not fast_enabled:
        details.append("⚠️ 快路径开关关闭，将回退到统一预警内检测")
        if status == "✅":
            status = "⚠️"
    
    # 检查 buy_point_alert 模块
    try:
        from notify.buy_point_alert import check_buy_point_alerts
        details.append("check_buy_point_alerts: 函数存在")
    except ImportError as e:
        details.append(f"❌ buy_point_alert 导入失败: {e}")
        status = "❌"
    
    # 检查买点状态文件
    bp_state = os.path.join(PROJECT_ROOT, "output", ".buy_point_state")
    if os.path.exists(bp_state):
        try:
            with open(bp_state, 'r') as f:
                bp_content = f.read().strip()
            details.append(f"买点状态文件: 存在 | 内容: {bp_content[:100]}")
        except Exception:
            details.append("买点状态文件: 存在(读取失败)")
    else:
        details.append("买点状态文件: 不存在(首次运行后创建)")
    
    # 买点冷却文件
    bp_alert_sent = os.path.join(PROJECT_ROOT, "data", "buy_point_alert_sent.json")
    if os.path.exists(bp_alert_sent):
        details.append("买点提醒发送记录: buy_point_alert_sent.json 存在")
    
    # 买点级别文件
    buy_levels = os.path.join(PROJECT_ROOT, "data", "buy_alert_levels.json")
    if os.path.exists(buy_levels):
        details.append("买点级别文件: buy_alert_levels.json 存在")
    
    report("10. 买点快路径", status, details)


# ============================================================
# 汇总报告
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  量化交易系统 - 预警系统全面健康诊断")
    print(f"  诊断时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    check_scheduler()
    check_alert_engine()
    check_intraday_monitor()
    check_alert_evaluator()
    check_alert_ledger()
    check_cooldown_escalation()
    check_notification_channels()
    check_market_regime()
    check_condition_orders()
    check_buy_point_fast()
    
    # 汇总
    print("\n" + "=" * 60)
    print("  诊断汇总")
    print("=" * 60)
    
    ok_count = sum(1 for _, s, _ in results if s == "✅")
    warn_count = sum(1 for _, s, _ in results if s == "⚠️")
    fail_count = sum(1 for _, s, _ in results if s == "❌")
    
    for name, status, _ in results:
        print(f"  {status}  {name}")
    
    print(f"\n  总计: {len(results)}项 | ✅正常: {ok_count} | ⚠️异常: {warn_count} | ❌失效: {fail_count}")
    
    if fail_count > 0:
        print("\n  ⛔ 存在失效模块，请尽快修复!")
    elif warn_count > 0:
        print("\n  ⚡ 存在异常模块，建议关注!")
    else:
        print("\n  🎉 全部模块运行正常!")
