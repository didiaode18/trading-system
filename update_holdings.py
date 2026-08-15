# -*- coding: utf-8 -*-
"""
每日持仓更新工具 V2.0
======================
收盘后运行，将最新持仓数据喂入系统，自动更新:
  1. holdings.json（持仓明细）
  2. config.TOTAL_CAPITAL / AVAILABLE_CASH（运行时覆盖）
  3. 同步根目录与trading_system/下的holdings.json

V2.0 状态管理规则（2026-08-06）:
  - 盘中保护: 交易日09:15-15:00拒绝覆盖持仓（除非--force），更新应在收盘后统一进行
  - 首次字段锁定: buy_date/buy_price/initial_stop_loss/reason/first_shares 一经首次
    确认不得被后续更新重置；股数不变时输入新成本会被拒绝（除非--force显式修正）
  - 股数变化才视为真实交易: 接受新加权成本，但保留首次建仓日期与初始止损
  - 止损Ratchet: stop_loss 只升不降；输入0视为未提供，保留现值，绝不重置为0
  - 每日首次有效更新快照: trading_system/data/holdings_snapshots/holdings_YYYYMMDD.json
  - 审计字段: updated_at / first_confirmed_at / last_update_source / revision_count

使用方式:
  方式1（交互式）: python update_holdings.py
  方式2（命令行）: python update_holdings.py --cash 250000 --input holdings_input.txt
  方式3（单只更新）: python update_holdings.py --code 002371 --shares 400 --price 718.5
  方式4（强制修正）: python update_holdings.py --code 603288 --shares 900 --cost 40.895 --price 36.5 --force

输入格式（交互式/文件，每行一只）:
  代码,数量,成本价,现价,止损价
  002371,400,753.947,718.5,697
  002415,2300,34.301,36.14,34.5

  特殊指令:
  - 输入 "del 代码" 删除某只持仓（已清仓）
  - 输入 "done" 结束输入
"""

import os
import sys
import json
import datetime
import argparse
import shutil

# 路径设置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADING_SYSTEM_DIR = os.path.join(SCRIPT_DIR, "trading_system")
sys.path.insert(0, TRADING_SYSTEM_DIR)

import config


def get_holdings_paths():
    """获取所有需要同步的holdings.json路径"""
    paths = []
    # 主路径: trading_system/holdings.json
    primary = os.path.join(TRADING_SYSTEM_DIR, "holdings.json")
    paths.append(primary)
    # 根目录路径: holdings.json（兼容旧脚本）
    legacy = os.path.join(SCRIPT_DIR, "holdings.json")
    if legacy != primary:
        paths.append(legacy)
    return paths


def load_current_holdings():
    """加载当前holdings.json"""
    holdings_file = config.get_holdings_file()
    if os.path.exists(holdings_file):
        with open(holdings_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_holdings(holdings: dict):
    """保存holdings.json到所有路径（保持同步）"""
    paths = get_holdings_paths()
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(holdings, f, ensure_ascii=False, indent=2)
    print(f"  ✅ holdings.json 已更新 ({len(paths)}个路径同步)")


def update_config_runtime(total_capital: float, available_cash: float):
    """运行时覆盖config中的资金参数（不修改config.py文件）"""
    config.TOTAL_CAPITAL = total_capital
    config.AVAILABLE_CASH = available_cash
    print(f"  ✅ config.TOTAL_CAPITAL = {total_capital:,.2f}")
    print(f"  ✅ config.AVAILABLE_CASH = {available_cash:,.2f}")


# ============================================================
# V2.0 字段治理: 首次确认字段 vs 每日可更新字段
# ============================================================
# 首次字段: 一经首次确认即锁定，后续更新不得重置（确需修正用 --force）
FIRST_CONFIRMED_FIELDS = ("buy_date", "buy_price", "initial_stop_loss", "reason", "first_shares")
# 每日可更新字段: current_price / highest / stop_loss(Ratchet只升不降) / avg_volume / shares(有真实交易时)

SNAPSHOT_DIR = os.path.join(TRADING_SYSTEM_DIR, "data", "holdings_snapshots")


def save_daily_snapshot(holdings: dict) -> bool:
    """
    V2.0 每日首次有效更新快照: 当日首次运行时写入，当日后续更新不覆盖。
    用于留存当日基准状态，事后可审计"当日首次确认 vs 最终状态"的差异。
    返回: True=已写入新快照, False=当日快照已存在(跳过)
    """
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        path = os.path.join(SNAPSHOT_DIR, f"holdings_{datetime.date.today().strftime('%Y%m%d')}.json")
        if os.path.exists(path):
            return False
        with open(path, "w", encoding="utf-8") as f:
            json.dump(holdings, f, ensure_ascii=False, indent=2)
        print(f"  📸 每日首次状态快照已保存: {os.path.basename(path)}")
        return True
    except Exception as e:
        print(f"  ⚠️ 快照写入失败，不阻断更新: {e}")
        return False


def merge_holding(existing: dict, parsed: dict, source: str,
                  default_reason: str = "手动更新", force: bool = False) -> tuple:
    """
    V2.0 统一合并规则（交互式/文件/单只三种模式共用同一口径）:
    1. 新建仓（existing为空或已清仓）: 全量写入并记录 first_confirmed_at /
       initial_stop_loss（未提供止损时默认成本×(1-INITIAL_STOP_LOSS_PCT)）
    2. 同一标的重复更新:
       - 股数不变: 拒绝变更 buy_price（防止误覆盖原始成本，除非 --force）
       - 股数变化: 视为真实加仓/减仓，接受新加权成本，但 buy_date /
         first_shares / initial_stop_loss 等首次字段保持不变
       - stop_loss 遵循 Ratchet 只升不降；输入0视为未提供，保留现值绝不置0
       - current_price / highest 正常每日更新
    3. 审计字段: updated_at / last_update_source / revision_count 每次登记

    参数 parsed 需含: code, name, shares, buy_price, current_price, stop_loss
    返回: (合并后的持仓dict, 警告信息list)
    """
    warnings = []
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    code = parsed["code"]
    cost = parsed.get("buy_price", 0)
    stop_pct = getattr(config, "INITIAL_STOP_LOSS_PCT", 0.10)

    # ---- 新建仓 / 清仓后重建: 全量写入首次状态 ----
    is_new = not existing or existing.get("shares", 0) <= 0
    if is_new:
        hard_stop = round(cost * (1 - stop_pct), 3) if cost > 0 else 0
        merged = {
            "name": parsed.get("name") or existing.get("name", ""),
            "shares": parsed["shares"],
            "buy_price": cost,
            "current_price": parsed["current_price"],
            "stop_loss": parsed.get("stop_loss", 0) if parsed.get("stop_loss", 0) > 0 else hard_stop,
            "highest": max(existing.get("highest", 0), parsed["current_price"]),
            "buy_date": datetime.date.today().isoformat(),
            "reason": existing.get("reason") or default_reason,
            "sector": existing.get("sector", "其他"),
            "first_shares": parsed["shares"],
            "initial_stop_loss": hard_stop,
            "first_confirmed_at": now_str,
            "updated_at": now_str,
            "last_update_source": source,
            "revision_count": 0,
        }
        return merged, warnings

    # ---- 重复更新: 默认保留现有全部字段，仅按规则合并 ----
    merged = dict(existing)
    old_shares = existing.get("shares", 0)

    # 股数: 有真实交易才变更
    if parsed["shares"] != old_shares:
        merged["shares"] = parsed["shares"]
        # 股数变化 → 接受券商新加权成本，但首次字段(buy_date等)不变
        if cost > 0 and abs(cost - existing.get("buy_price", 0)) > 1e-9:
            merged["buy_price"] = cost
            warnings.append(f"ℹ️ {code} 股数{old_shares}→{parsed['shares']}，"
                            f"接受新加权成本{cost:.3f}（首次建仓日期等字段保持不变）")
    elif cost > 0 and abs(cost - existing.get("buy_price", 0)) > 1e-9:
        # 股数不变但成本不同 → 疑似误覆盖，拒绝（除非--force显式修正）
        if force:
            merged["buy_price"] = cost
            warnings.append(f"⚠️ {code} --force 强制修正成本 "
                            f"{existing.get('buy_price', 0):.3f}→{cost:.3f}")
        else:
            warnings.append(f"⚠️ {code} 股数未变但输入成本{cost:.3f}≠记录成本"
                            f"{existing.get('buy_price', 0):.3f}，已忽略"
                            f"（确需人工修正请加 --force）")

    # 止损: Ratchet 只升不降；输入0视为未提供，保留现值绝不置0
    old_stop = existing.get("stop_loss", 0)
    in_stop = parsed.get("stop_loss", 0) or 0
    if in_stop > 0:
        if in_stop < old_stop:
            merged["stop_loss"] = old_stop
            warnings.append(f"⚠️ {code} 输入止损{in_stop:.3f}低于现有{old_stop:.3f}，"
                            f"按Ratchet(只升不降)保留原值")
        else:
            merged["stop_loss"] = in_stop

    # 每日可更新字段
    merged["current_price"] = parsed["current_price"]
    merged["highest"] = max(existing.get("highest", 0), parsed["current_price"])

    # 审计字段
    merged["updated_at"] = now_str
    merged["last_update_source"] = source
    merged["revision_count"] = int(existing.get("revision_count", 0)) + 1
    # 旧数据兼容: 补全缺失的首次字段
    merged.setdefault("first_confirmed_at", existing.get("updated_at", now_str))
    merged.setdefault("initial_stop_loss",
                      round(existing.get("buy_price", 0) * (1 - stop_pct), 3))
    merged.setdefault("first_shares", existing.get("shares", 0))

    return merged, warnings


def parse_input_line(line: str) -> dict:
    """解析一行输入: 代码,数量,成本价,现价,止损价"""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    try:
        return {
            "code": parts[0],
            "shares": int(float(parts[1])),
            "buy_price": float(parts[2]),
            "current_price": float(parts[3]),
            "stop_loss": float(parts[4]) if len(parts) > 4 and parts[4] else 0,
        }
    except (ValueError, IndexError):
        return None


def interactive_mode(force: bool = False):
    """交互式输入持仓"""
    print("\n" + "=" * 55)
    print("  📊 每日持仓更新工具 V2.0")
    print("  格式: 代码,数量,成本价,现价,止损价")
    print("  指令: 'del 代码'=删除 | 'done'=完成")
    print("=" * 55)

    holdings = load_current_holdings()
    print(f"\n  当前持仓 {sum(1 for v in holdings.values() if v.get('shares', 0) > 0)} 只:")
    for code, h in holdings.items():
        if h.get("shares", 0) > 0:
            print(f"    {code} {h.get('name', '')} {h['shares']}股 成本{h.get('buy_price', 0):.3f}")

    print("\n  请输入最新持仓（逐行输入，done结束）:")
    updated_codes = set()

    while True:
        try:
            line = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue
        if line.lower() == "done":
            break

        # 删除指令
        if line.lower().startswith("del "):
            del_code = line[4:].strip()
            if del_code in holdings:
                holdings[del_code]["shares"] = 0
                holdings[del_code]["buy_price"] = 0
                holdings[del_code]["stop_loss"] = 0
                holdings[del_code]["reason"] = f"已清仓({datetime.date.today()}手动更新)"
                holdings[del_code]["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                holdings[del_code]["last_update_source"] = "交互式删除"
                print(f"    🗑️  已删除: {del_code}")
            else:
                print(f"    ⚠️ 未找到: {del_code}")
            continue

        # 解析持仓行
        parsed = parse_input_line(line)
        if not parsed:
            print("    ⚠️ 格式错误，请用: 代码,数量,成本价,现价,止损价")
            continue

        code = parsed["code"]
        # 获取股票名称（从现有数据或config）
        existing = holdings.get(code, {})
        name = existing.get("name", "")
        if not name:
            name = config.get_stock_name(code) if hasattr(config, 'get_stock_name') else code

        # V2.0 统一合并规则: 首次字段锁定 + 止损Ratchet只升不降 + 审计字段登记
        parsed["name"] = name
        merged, warns = merge_holding(existing, parsed, source="交互式",
                                      default_reason="手动更新", force=force)
        holdings[code] = merged
        for w in warns:
            print(f"    {w}")
        updated_codes.add(code)
        print(f"    ✓ {code} {name} {parsed['shares']}股 成本{merged['buy_price']:.3f} "
              f"现价{parsed['current_price']:.3f} 止损{merged['stop_loss']:.3f}")

    return holdings, updated_codes


def file_mode(input_file: str, force: bool = False):
    """从文件读取持仓"""
    holdings = load_current_holdings()
    updated_codes = set()

    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("del "):
                del_code = line[4:].strip()
                if del_code in holdings:
                    holdings[del_code]["shares"] = 0
                    holdings[del_code]["buy_price"] = 0
                    holdings[del_code]["stop_loss"] = 0
                    holdings[del_code]["reason"] = f"已清仓({datetime.date.today()}批量更新)"
                    holdings[del_code]["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    holdings[del_code]["last_update_source"] = "文件删除"
                continue

            parsed = parse_input_line(line)
            if not parsed:
                continue
            code = parsed["code"]
            existing = holdings.get(code, {})
            name = existing.get("name", config.get_stock_name(code) if hasattr(config, 'get_stock_name') else code)

            # V2.0 统一合并规则: 首次字段锁定 + 止损Ratchet只升不降 + 审计字段登记
            parsed["name"] = name
            merged, warns = merge_holding(existing, parsed, source="文件",
                                          default_reason="批量更新", force=force)
            holdings[code] = merged
            for w in warns:
                print(f"  {w}")
            updated_codes.add(code)

    return holdings, updated_codes


def single_mode(code: str, shares: int, cost: float, price: float,
                stop_loss: float = 0, force: bool = False):
    """单只更新模式（V2.0: 同样走统一合并规则，首次字段锁定+止损Ratchet）"""
    holdings = load_current_holdings()
    existing = holdings.get(code, {})
    name = existing.get("name", config.get_stock_name(code) if hasattr(config, 'get_stock_name') else code)

    parsed = {"code": code, "name": name, "shares": shares, "buy_price": cost,
              "current_price": price, "stop_loss": stop_loss}
    merged, warns = merge_holding(existing, parsed, source="单只更新",
                                  default_reason="单只更新", force=force)
    holdings[code] = merged
    for w in warns:
        print(f"  {w}")
    return holdings, {code}


def enrich_avg_volume(holdings: dict) -> int:
    """
    # FIX: 修复 holdings.json 中 avg_volume 无写入方恒为0，导致盘中监控/竞价分析
    # 4类量价规则（放量急跌/缩量下跌/量比/竞价量比）永久失效的问题
    为每只活跃持仓计算并写入 avg_volume（近20个交易日日均成交量）。
    单位说明: baostock日线volume单位是"股"，而盘中监控(intraday_monitor.py)与
    scheduler.py 比较的是腾讯实时行情volume（单位"手"，1手=100股），
    故此处统一换算为"手"写入，保证比较口径一致。
    单只写入失败仅跳过，不阻断整体更新流程。
    返回: 成功写入的股票数
    """
    try:
        import baostock as bs
    except ImportError:
        print("  ⚠️ baostock 未安装，跳过 avg_volume 写入")
        return 0

    active = {c: h for c, h in holdings.items()
              if isinstance(h, dict) and h.get("shares", 0) > 0}
    if not active:
        return 0

    try:
        bs.login()
    except Exception as e:
        print(f"  ⚠️ baostock 登录失败，跳过 avg_volume 写入: {e}")
        return 0

    end = datetime.date.today().strftime("%Y-%m-%d")
    # 取60个自然日窗口，确保覆盖至少20个交易日
    start = (datetime.date.today() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    ok_count = 0
    for code, h in active.items():
        try:
            prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"
            rs = bs.query_history_k_data_plus(
                f"{prefix}.{code}", "date,volume",
                start_date=start, end_date=end, frequency="d", adjustflag="3"
            )
            vols = []
            while rs.error_code == '0' and rs.next():
                row = rs.get_row_data()
                try:
                    v = float(row[1])
                    if v > 0:
                        vols.append(v)
                except (ValueError, IndexError):
                    continue
            if vols:
                recent = vols[-20:]  # 近20个交易日
                avg_shares = sum(recent) / len(recent)
                # FIX: 股→手换算（/100），与盘中监控使用的腾讯行情volume口径一致
                h["avg_volume"] = round(avg_shares / 100.0, 1)
                ok_count += 1
            elif code.startswith(("1", "5")):
                # FIX: ETF兜底 — baostock无数据时改用akshare东财ETF日线（返回值单位已是"手"）
                avg_etf = _avg_volume_etf_akshare(code)
                if avg_etf > 0:
                    h["avg_volume"] = avg_etf
                    ok_count += 1
                else:
                    print(f"  ⚠️ {code} baostock/akshare均无成交量数据，avg_volume保持0（量价规则自动降级）")
        except Exception as e:
            # FIX: 单只失败降级跳过，不阻断更新流程
            print(f"  ⚠️ {code} avg_volume 获取失败，跳过: {e}")

    try:
        bs.logout()
    except Exception:
        pass
    print(f"  ✅ avg_volume 已写入 {ok_count}/{len(active)} 只 (单位:手, 近20日日均)")
    return ok_count


def enrich_intraday_high(holdings: dict, updated_codes: set = None) -> int:
    """
    # FIX: 修复 highest 用更新时点现价 max(existing.highest, current_price) 而非
    # 日内最高，导致回落止盈基准偏低的问题。
    用实时行情(腾讯/东方财富多源容错)的当日最高价 high 字段校正 highest；
    行情不可得时保留现价基准（原逻辑）并降级提示，单只失败不阻断流程。
    返回: 成功用当日最高价更新的股票数
    """
    try:
        from data.realtime import fetch_realtime_batch
    except ImportError:
        print("  ⚠️ 实时行情模块不可用，highest 保留现价基准")
        return 0

    codes = list(updated_codes) if updated_codes else [
        c for c, h in holdings.items()
        if isinstance(h, dict) and h.get("shares", 0) > 0
    ]
    if not codes:
        return 0

    try:
        quotes = fetch_realtime_batch(codes)
    except Exception as e:
        print(f"  ⚠️ 实时行情获取失败，highest 保留现价基准: {e}")
        return 0

    ok_count = 0
    for code in codes:
        try:
            h = holdings.get(code)
            if not isinstance(h, dict) or h.get("shares", 0) <= 0:
                continue
            intraday_high = (quotes.get(code) or {}).get("high", 0) or 0
            if intraday_high > 0:
                # FIX: 优先使用当日最高价（而非更新时点现价）作为回落止盈基准
                h["highest"] = max(h.get("highest", 0), intraday_high)
                ok_count += 1
        except Exception:
            continue
    print(f"  ✅ highest 已按当日最高价更新 {ok_count}/{len(codes)} 只")
    return ok_count


def _avg_volume_etf_akshare(code: str) -> float:
    """
    # FIX: 修复部分ETF在baostock无日线数据导致avg_volume写为0、量价预警规则失效的问题
    ETF兜底数据源: akshare 东财ETF日线（项目已有依赖，不引入新依赖）。
    单位说明: 东财接口"成交量"字段单位本身就是"手"，与写入口径一致，无需/100换算。
    失败返回 0（读取侧已有降级，不阻断）。
    """
    try:
        import akshare as ak
        end = datetime.date.today().strftime("%Y%m%d")
        start = (datetime.date.today() - datetime.timedelta(days=60)).strftime("%Y%m%d")
        df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                 start_date=start, end_date=end, adjust="")
        if df is None or df.empty or "成交量" not in df.columns:
            return 0.0
        vols = [float(v) for v in df["成交量"].tail(20).tolist() if float(v) > 0]
        if not vols:
            return 0.0
        return round(sum(vols) / len(vols), 1)  # 单位已是"手"，直接写入
    except Exception as e:
        print(f"  ⚠️ {code} akshare ETF兜底获取失败: {e}")
        return 0.0


def calc_totals(holdings: dict, available_cash: float = None):
    """计算总资产和可用现金"""
    total_market_value = sum(
        h.get("shares", 0) * h.get("current_price", h.get("buy_price", 0))
        for h in holdings.values()
        if h.get("shares", 0) > 0
    )

    if available_cash is None:
        # 未指定现金时，用 config.TOTAL_CAPITAL - 持仓市值 推算（保持总资产不变）
        # 但如果持仓变化大，提示用户手动输入
        old_capital = getattr(config, 'TOTAL_CAPITAL', 0)
        available_cash = max(0, old_capital - total_market_value)

    total_capital = total_market_value + available_cash
    return total_capital, available_cash, total_market_value


def print_summary(holdings: dict, total_capital: float, available_cash: float, total_mv: float):
    """打印更新摘要"""
    active = {c: h for c, h in holdings.items() if h.get("shares", 0) > 0}
    total_cost = sum(h["shares"] * h.get("buy_price", 0) for h in active.values())
    total_pnl = total_mv - total_cost
    position_ratio = total_mv / total_capital * 100 if total_capital > 0 else 0

    print("\n" + "=" * 55)
    print("  📋 持仓更新摘要")
    print("=" * 55)
    print(f"  总资产:     {total_capital:>12,.2f} 元")
    print(f"  持仓市值:   {total_mv:>12,.2f} 元 ({position_ratio:.1f}%)")
    print(f"  可用现金:   {available_cash:>12,.2f} 元 ({100-position_ratio:.1f}%)")
    print(f"  持仓盈亏:   {total_pnl:>+12,.2f} 元 ({total_pnl/total_cost*100 if total_cost > 0 else 0:+.1f}%)")
    print(f"  活跃持仓:   {len(active)} 只")
    print("-" * 55)

    for code, h in sorted(active.items(), key=lambda x: x[1]["shares"] * x[1].get("current_price", 0), reverse=True):
        mv = h["shares"] * h.get("current_price", 0)
        pnl_pct = (h.get("current_price", 0) / h.get("buy_price", 1) - 1) * 100 if h.get("buy_price", 0) > 0 else 0
        ratio = mv / total_capital * 100 if total_capital > 0 else 0
        flag = "⚠️超限" if ratio > 15 else ""
        print(f"  {code} {h.get('name', ''):<6} {h['shares']:>6}股 "
              f"市值{mv/10000:>6.1f}万 占比{ratio:>5.1f}% "
              f"盈亏{pnl_pct:>+6.1f}% {flag}")

    # 风险检查
    print("-" * 55)
    alerts = []
    if position_ratio > 80:
        alerts.append(f"⚠️ 总仓位{position_ratio:.1f}%过高(>80%)，建议减至60%以下")
    if available_cash < total_capital * 0.05:
        alerts.append(f"⚠️ 现金仅{available_cash:.0f}元(<5%)，无应急缓冲")
    for code, h in active.items():
        mv = h["shares"] * h.get("current_price", 0)
        if mv / total_capital > 0.15:
            alerts.append(f"⚠️ {h.get('name', code)}占比{mv/total_capital*100:.1f}%>15%上限")
    if alerts:
        for a in alerts:
            print(f"  {a}")
    else:
        print("  ✅ 仓位结构健康，无超限预警")
    print("=" * 55)


def main():
    parser = argparse.ArgumentParser(description="每日持仓更新工具")
    parser.add_argument("--cash", type=float, default=None, help="可用现金(元)")
    parser.add_argument("--input", type=str, default=None, help="持仓输入文件路径")
    parser.add_argument("--code", type=str, default=None, help="单只更新: 股票代码")
    parser.add_argument("--shares", type=int, default=None, help="单只更新: 数量")
    parser.add_argument("--cost", type=float, default=0, help="单只更新: 成本价")
    parser.add_argument("--price", type=float, default=None, help="单只更新: 现价")
    parser.add_argument("--stop", type=float, default=0, help="单只更新: 止损价")
    parser.add_argument("--show", action="store_true", help="仅显示当前持仓，不修改")
    # FIX: 新增仅补写 avg_volume 的模式，不改动其他持仓字段、无交互输入
    parser.add_argument("--enrich-only", action="store_true", help="仅补写avg_volume字段，不改动其他持仓数据")
    # V2.0: 强制模式 —— 允许盘中更新与首次字段(成本/建仓日期等)人工修正
    parser.add_argument("--force", action="store_true",
                        help="强制模式: 允许盘中更新与首次字段人工修正(需显式确认)")
    args = parser.parse_args()

    # FIX: 仅补写 avg_volume（近20日日均成交量，单位:手），失败不阻断
    if args.enrich_only:
        holdings = load_current_holdings()
        try:
            enrich_avg_volume(holdings)
        except Exception as e:
            print(f"  ⚠️ avg_volume 补写异常: {e}")
        save_holdings(holdings)
        return

    # 仅显示模式
    if args.show:
        holdings = load_current_holdings()
        total_capital, available_cash, total_mv = calc_totals(holdings, args.cash)
        print_summary(holdings, total_capital, available_cash, total_mv)
        return

    # V2.0 盘中保护: 持仓更新应在收盘后统一进行，交易日09:15-15:00拒绝覆盖（除非--force）
    _now_dt = datetime.datetime.now()
    if datetime.time(9, 15) <= _now_dt.time() <= datetime.time(15, 0) and _now_dt.weekday() < 5:
        if not args.force:
            print("⛔ 盘中保护: 持仓更新应在收盘后(15:00后)统一进行。")
            print("   当前处于交易时段，拒绝覆盖 holdings.json；确需盘中修正请加 --force。")
            return
        print("  ⚠️ --force 强制模式: 盘中执行持仓更新（首次字段仍受锁定保护）")

    # 选择输入模式
    if args.code:
        if args.shares is None or args.price is None:
            print("❌ 单只模式需要 --shares 和 --price 参数")
            return
        holdings, updated = single_mode(args.code, args.shares, args.cost, args.price, args.stop, force=args.force)
    elif args.input:
        if not os.path.exists(args.input):
            print(f"❌ 文件不存在: {args.input}")
            return
        holdings, updated = file_mode(args.input, force=args.force)
    else:
        holdings, updated = interactive_mode(force=args.force)

    if not updated and not args.code:
        print("\n  未输入任何更新，退出。")
        return

    # V2.0 每日首次有效更新快照: 当日首次运行写入基准状态，当日后续更新不覆盖
    save_daily_snapshot(holdings)

    # 计算总资产
    total_capital, available_cash, total_mv = calc_totals(holdings, args.cash)

    # 如果用户未指定cash且持仓变化大，提示确认
    if args.cash is None:
        print(f"\n  💡 计算得可用现金: {available_cash:,.2f}元")
        try:
            confirm = input("  按回车确认，或输入实际可用现金: ").strip()
            if confirm:
                available_cash = float(confirm)
                total_capital = total_mv + available_cash
        except (EOFError, KeyboardInterrupt):
            pass

    # FIX: 保存前为持仓补写 avg_volume（近20日日均量,单位:手），供盘中量价规则使用；失败不阻断
    try:
        enrich_avg_volume(holdings)
    except Exception as e:
        print(f"  ⚠️ avg_volume 写入异常，不阻断更新: {e}")

    # FIX: 保存前用当日最高价校正 highest，避免回落止盈基准偏低；失败不阻断
    try:
        enrich_intraday_high(holdings, updated)
    except Exception as e:
        print(f"  ⚠️ highest 校正异常，保留现价基准: {e}")

    # 保存
    save_holdings(holdings)
    update_config_runtime(total_capital, available_cash)

    # 打印摘要
    print_summary(holdings, total_capital, available_cash, total_mv)

    print(f"\n  📅 更新时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("  ✅ 完成！16:15综合分析报告将使用最新数据。\n")


if __name__ == "__main__":
    main()

