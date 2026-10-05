"""
实时行情精准获取模块
====================
多源容错获取A股实时行情，专为盘中监控设计

数据源优先级:
  1. 腾讯行情API（批量，一次最多60只，延迟<1秒）
  2. 东方财富API（akshare，单只/批量）
  3. 新浪行情API（硬兜底，轻量HTTP，延迟<1秒）  ← V10.4 新增
  4. akshare全市场接口（最后手段，慢但稳定）

增强能力:
  - V10.4: 盘中价格交叉校验（腾讯 vs 新浪抽样校验）
  - V10.4: 指数退避冷却（60s→120s→300s→600s）
  - V10.4: 数据源质量评分机制
  - V10.4: 统一数据源健康看板

使用方式:
    from data.realtime import fetch_realtime_batch, fetch_realtime_single
    
    # 批量获取（推荐，一次网络请求）
    quotes = fetch_realtime_batch(["600584", "002415", "000725"])
    # 返回: {"600584": {"price": 45.2, "change_pct": 1.5, ...}, ...}
    
    # 单只获取
    quote = fetch_realtime_single("600584")
"""

import logging
import datetime
import urllib.request
import json as json_module

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


# ============================================================
# V9.3: 数据源健康追踪（P0-⑤ 数据源冗余+自动切换）
# ============================================================
# 连续失败计数 + 冷却跳过，避免已故障源每轮都浪费超时时间
# {source_name: {"failures": int, "last_fail": datetime, "cooldown_until": datetime}}
_SOURCE_HEALTH = {
    "tencent": {"failures": 0, "last_fail": None, "cooldown_until": None,
                "quality_score": 1.0, "total_requests": 0, "total_success": 0},
    "eastmoney": {"failures": 0, "last_fail": None, "cooldown_until": None,
                  "quality_score": 1.0, "total_requests": 0, "total_success": 0},
    "sina": {"failures": 0, "last_fail": None, "cooldown_until": None,
             "quality_score": 1.0, "total_requests": 0, "total_success": 0},
}
# V10.4: 指数退避冷却基准（秒）—— 第N次冷却 = BASE * 2^(N-1)，上限600秒
_SOURCE_COOLDOWN_BASE = 60
_SOURCE_COOLDOWN_MAX = 600
_SOURCE_MAX_FAILURES = 2        # 触发冷却的连续失败阈值

# V10.4: 交叉校验状态
_CROSSVALIDATE_STATE = {
    "last_check_time": None,     # 上次交叉校验时间
    "check_interval_seconds": 50,  # 校验间隔（≈5轮×10秒）
    "discrepancy_log": [],       # 最近偏差记录（保留10条）
}


def _is_source_healthy(name: str) -> bool:
    """检查数据源是否可用（冷却期内视为不健康）"""
    health = _SOURCE_HEALTH.get(name)
    if not health:
        return True
    if health["cooldown_until"] and datetime.datetime.now() < health["cooldown_until"]:
        return False
    return True


def _record_source_success(name: str, latency_ms: float = 0):
    """记录数据源成功，重置失败计数 + V10.4质量评分更新"""
    health = _SOURCE_HEALTH.get(name)
    if health:
        health["failures"] = 0
        health["cooldown_until"] = None
        health["total_success"] = health.get("total_success", 0) + 1
        # V10.4: 质量评分指数移动平均（成功时向1.0靠拢）
        health["quality_score"] = min(1.0, health.get("quality_score", 1.0) * 0.95 + 0.05)


def _record_source_failure(name: str, latency_ms: float = 0):
    """记录数据源失败，V10.4指数退避冷却"""
    health = _SOURCE_HEALTH.get(name)
    if health:
        health["failures"] += 1
        health["last_fail"] = datetime.datetime.now()
        # V10.4: 质量评分惩罚（失败时扣分）
        health["quality_score"] = max(0.0, health.get("quality_score", 1.0) - 0.15)
        if health["failures"] >= _SOURCE_MAX_FAILURES:
            # V10.4: 指数退避 —— cooldown = base * 2^(failures - threshold), 上限max
            backoff = _SOURCE_COOLDOWN_BASE * (2 ** (health["failures"] - _SOURCE_MAX_FAILURES))
            backoff = min(backoff, _SOURCE_COOLDOWN_MAX)
            health["cooldown_until"] = (
                datetime.datetime.now() +
                datetime.timedelta(seconds=backoff)
            )
            logger.warning(
                f"[数据源健康] {name} 连续失败{health['failures']}次，"
                f"指数退避冷却{backoff}秒"
            )


def get_data_source_status() -> dict:
    """V10.4: 统一数据源健康看板（供监控模块/Dashboard降级感知）

    返回: {
        "tencent": {"healthy": bool, "failures": int, "quality_score": float, ...},
        ...,
        "overall": "normal"|"degraded"|"outage",
        "cross_validate": {"last_check": str, "recent_discrepancies": int}
    }
    """
    now = datetime.datetime.now()
    status = {}
    any_healthy = False
    degraded_sources = 0
    for name, health in _SOURCE_HEALTH.items():
        is_healthy = (
            health["cooldown_until"] is None or now >= health["cooldown_until"]
        )
        total_req = health.get("total_requests", 0)
        total_ok = health.get("total_success", 0)
        success_rate = (total_ok / total_req) if total_req > 0 else 1.0
        status[name] = {
            "healthy": is_healthy,
            "failures": health["failures"],
            "quality_score": round(health.get("quality_score", 1.0), 3),
            "success_rate": round(success_rate, 3),
            "total_requests": total_req,
            "last_fail": str(health.get("last_fail", "")),
        }
        if is_healthy:
            any_healthy = True
        else:
            degraded_sources += 1

    # 综合状态: normal / degraded(部分降级) / outage(全灭)
    if any_healthy and degraded_sources == 0:
        overall = "normal"
    elif any_healthy:
        overall = "degraded"
    else:
        overall = "outage"
    status["overall"] = overall

    # V10.4: 交叉校验状态
    cv = _CROSSVALIDATE_STATE
    status["cross_validate"] = {
        "last_check": str(cv.get("last_check_time", "")),
        "recent_discrepancies": len(cv.get("discrepancy_log", [])),
    }
    return status


# ============================================================
# 一、腾讯行情API（主数据源，批量快速）
# ============================================================

def _to_tencent_code(code: str) -> str:
    """转换为腾讯行情API格式: 600584 -> sh600584, 002415 -> sz002415"""
    if code.startswith("sh") or code.startswith("sz"):
        return code
    if code.startswith("6") or code.startswith("9") or code.startswith("5") or code == "000300":
        return f"sh{code}"
    else:
        return f"sz{code}"


def _from_tencent_code(tc_code: str) -> str:
    """从腾讯格式转回纯数字: sh600584 -> 600584"""
    return tc_code[2:] if len(tc_code) > 2 else tc_code


def fetch_realtime_tencent(codes: list) -> dict:
    """
    腾讯行情API批量获取（一次HTTP请求，延迟极低）
    
    接口: http://qt.gtimg.cn/q=sh600584,sz002415,...
    返回格式: v_sh600584="1~长电科技~600584~45.20~44.50~44.80~1234567~..."
    
    字段解析（~分隔）:
      [1]名称 [2]代码 [3]当前价 [4]昨收 [5]今开 [6]成交量(手)
      [30]时间 [31]涨跌 [32]涨跌% [33]最高 [34]最低 [35]价格/成交量/成交额
      [36]成交量(手) [37]成交额(万) [38]换手率 [39]PE [43]振幅 [44]流通市值
    """
    if not codes:
        return {}
    
    # 构造请求URL（一次最多60只）
    tc_codes = [_to_tencent_code(c) for c in codes]
    results = {}
    
    # 分批（每批最多50只，留余量）
    batch_size = 50
    for i in range(0, len(tc_codes), batch_size):
        batch = tc_codes[i:i + batch_size]
        url = f"http://qt.gtimg.cn/q={','.join(batch)}"
        
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            req.add_header("Referer", "http://finance.qq.com")
            
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read().decode("gbk", errors="ignore")
            
            # 解析每行数据
            for line in content.strip().split("\n"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                # v_sh600584="1~长电科技~600584~45.20~..."
                try:
                    var_part, data_part = line.split("=", 1)
                    data_part = data_part.strip('"').strip(";").strip('"')
                    fields = data_part.split("~")
                    
                    if len(fields) < 45:
                        continue
                    
                    code = fields[2]  # 纯数字代码
                    price = float(fields[3]) if fields[3] else 0
                    prev_close = float(fields[4]) if fields[4] else 0
                    open_price = float(fields[5]) if fields[5] else 0
                    volume = float(fields[6]) if fields[6] else 0  # 成交量(手)
                    high = float(fields[33]) if fields[33] else 0
                    low = float(fields[34]) if fields[34] else 0
                    change_pct = float(fields[32]) if fields[32] else 0
                    amount = float(fields[37]) if fields[37] else 0  # 成交额(万)
                    turnover = float(fields[38]) if fields[38] else 0
                    amplitude = float(fields[43]) if fields[43] else 0
                    update_time = fields[30] if len(fields) > 30 else ""
                    
                    if price <= 0:
                        continue
                    
                    # V9.0: 内外盘（主动买/主动卖）
                    outer_vol = float(fields[7]) if len(fields) > 7 and fields[7] else 0  # 外盘(手)
                    inner_vol = float(fields[8]) if len(fields) > 8 and fields[8] else 0  # 内盘(手)
                    
                    # V9.0: 买卖五档盘口
                    bid1_price = float(fields[9]) if len(fields) > 9 and fields[9] else 0
                    bid1_vol = float(fields[10]) if len(fields) > 10 and fields[10] else 0
                    ask1_price = float(fields[11]) if len(fields) > 11 and fields[11] else 0
                    ask1_vol = float(fields[12]) if len(fields) > 12 and fields[12] else 0
                    bid2_vol = float(fields[14]) if len(fields) > 14 and fields[14] else 0
                    ask2_vol = float(fields[16]) if len(fields) > 16 and fields[16] else 0
                    bid3_vol = float(fields[18]) if len(fields) > 18 and fields[18] else 0
                    ask3_vol = float(fields[20]) if len(fields) > 20 and fields[20] else 0
                    bid4_vol = float(fields[22]) if len(fields) > 22 and fields[22] else 0
                    ask4_vol = float(fields[24]) if len(fields) > 24 and fields[24] else 0
                    bid5_vol = float(fields[26]) if len(fields) > 26 and fields[26] else 0
                    ask5_vol = float(fields[28]) if len(fields) > 28 and fields[28] else 0
                    
                    # V9.0: VWAP均价线 = 成交额(万)*10000 / (成交量(手)*100)
                    vwap = (amount * 100.0 / volume) if volume > 0 else price
                    
                    # V9.0: 委比 = (委买总量-委卖总量)/(委买+委卖)
                    total_bid = bid1_vol + bid2_vol + bid3_vol + bid4_vol + bid5_vol
                    total_ask = ask1_vol + ask2_vol + ask3_vol + ask4_vol + ask5_vol
                    order_ratio = ((total_bid - total_ask) / (total_bid + total_ask)
                                   if (total_bid + total_ask) > 0 else 0)
                    
                    results[code] = {
                        "price": price,
                        "prev_close": prev_close,
                        "open": open_price,
                        "high": high,
                        "low": low,
                        "volume": volume,
                        "amount": amount,
                        "change_pct": change_pct,
                        "turnover": turnover,
                        "amplitude": amplitude,
                        "name": fields[1],
                        "time": update_time,
                        "source": "tencent",
                        # V9.0 新增字段
                        "vwap": round(vwap, 3),
                        "outer_vol": outer_vol,
                        "inner_vol": inner_vol,
                        "bid1_price": bid1_price,
                        "bid1_vol": bid1_vol,
                        "ask1_price": ask1_price,
                        "ask1_vol": ask1_vol,
                        "total_bid_vol": total_bid,
                        "total_ask_vol": total_ask,
                        "order_ratio": round(order_ratio, 4),
                    }
                except (ValueError, IndexError) as e:
                    logger.debug(f"腾讯API解析异常: {e}")
                    continue
                    
        except Exception as e:
            logger.warning(f"腾讯行情API请求失败: {e}")
            continue
    
    return results


# ============================================================
# 二、东方财富API（备用数据源）
# ============================================================

# V10.5: 东财全市场结果缓存（避免每次降级都拉5000+只，60秒TTL）
_em_spot_cache = {"data": None, "ts": 0}
_EM_SPOT_CACHE_TTL = 60  # 秒


def fetch_realtime_eastmoney(codes: list) -> dict:
    """
    东方财富实时行情（通过akshare）
    比腾讯慢但数据更全

    V10.5优化: 全市场拉取结果缓存60秒TTL，
    降级触发时60秒内复用缓存，避免每次都拉5000+只股票。
    """
    if not HAS_AKSHARE or not codes:
        return {}

    results = {}
    try:
        import time as _time
        now_ts = _time.time()

        # V10.5: 检查缓存是否有效
        if _em_spot_cache["data"] is not None and (now_ts - _em_spot_cache["ts"]) < _EM_SPOT_CACHE_TTL:
            df = _em_spot_cache["data"]
        else:
            # 全市场拉取（慢，3-10秒）
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return {}
            # 缓存结果
            _em_spot_cache["data"] = df
            _em_spot_cache["ts"] = now_ts

        # 筛选目标股票
        code_set = set(codes)
        df_filtered = df[df["代码"].isin(code_set)]

        for _, row in df_filtered.iterrows():
            code = row["代码"]
            results[code] = {
                "price": float(row.get("最新价", 0) or 0),
                "prev_close": float(row.get("昨收", 0) or 0),
                "open": float(row.get("今开", 0) or 0),
                "high": float(row.get("最高", 0) or 0),
                "low": float(row.get("最低", 0) or 0),
                "volume": float(row.get("成交量", 0) or 0),
                # BUG-036修复: 东财成交额单位为元，统一转万元（与腾讯/新浪口径对齐）
                "amount": float(row.get("成交额", 0) or 0) / 10000.0,
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "turnover": float(row.get("换手率", 0) or 0),
                "amplitude": float(row.get("振幅", 0) or 0),
                "name": row.get("名称", ""),
                "time": datetime.datetime.now().strftime("%H%M%S"),
                "source": "eastmoney",
            }
    except Exception as e:
        logger.warning(f"东方财富实时行情获取失败: {e}")
        # 失败时清除缓存，下次强制刷新
        _em_spot_cache["data"] = None

    return results


# ============================================================
# 三、ETF实时行情（腾讯API同样支持）
# ============================================================

def fetch_realtime_etf(codes: list) -> dict:
    """
    ETF实时行情（腾讯API支持ETF: sh588000, sz159205）
    与股票接口相同，复用腾讯API
    """
    return fetch_realtime_tencent(codes)


# ============================================================
# 三-B、新浪行情API（V10.4 硬兜底源）
# ============================================================

def _to_sina_code(code: str) -> str:
    """转换为新浪行情API格式: 600584 -> sh600584, 002415 -> sz002415"""
    if code.startswith("sh") or code.startswith("sz"):
        return code
    if code.startswith("6") or code.startswith("9") or code.startswith("5") or code == "000300":
        return f"sh{code}"
    else:
        return f"sz{code}"


def fetch_realtime_sina(codes: list) -> dict:
    """
    V10.4: 新浪行情API批量获取（硬兜底源，轻量HTTP）

    接口: https://hq.sinajs.cn/list=sh600584,sz002415,...
    返回格式: var hq_str_sh600584="长电科技,开盘价,昨收,当前价,最高,最低,..."

    字段解析（,分隔）:
      [0]名称 [1]今开 [2]昨收 [3]当前价 [4]最高 [5]最低
      [6]买一价 [7]卖一价 [8]成交量(股) [9]成交额(元)
      [30]日期 [31]时间
    """
    if not codes:
        return {}

    sina_codes = [_to_sina_code(c) for c in codes]
    url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"
    results = {}

    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Referer", "https://finance.sina.com.cn")

        with urllib.request.urlopen(req, timeout=8) as resp:
            content = resp.read().decode("gbk", errors="ignore")

        for line in content.strip().split("\n"):
            line = line.strip()
            if not line or '="' not in line:
                continue
            try:
                # var hq_str_sh600584="长电科技,开盘价,..."
                var_part, data_part = line.split('="', 1)
                data_part = data_part.rstrip('";')
                fields = data_part.split(",")

                if len(fields) < 32:
                    continue

                # 从var_part提取代码
                sina_code = var_part.split("_")[-1]  # sh600584
                code = sina_code[2:]  # 600584

                price = float(fields[3]) if fields[3] else 0
                prev_close = float(fields[2]) if fields[2] else 0
                open_price = float(fields[1]) if fields[1] else 0
                high = float(fields[4]) if fields[4] else 0
                low = float(fields[5]) if fields[5] else 0
                volume = float(fields[8]) / 100 if fields[8] else 0  # 股→手
                amount = float(fields[9]) / 10000 if fields[9] else 0  # 元→万元

                if price <= 0:
                    continue

                change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0

                results[code] = {
                    "price": price,
                    "prev_close": prev_close,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "volume": round(volume, 0),
                    "amount": round(amount, 2),
                    "change_pct": round(change_pct, 2),
                    "turnover": 0,  # 新浪接口无换手率
                    "amplitude": round((high - low) / prev_close * 100, 2) if prev_close > 0 else 0,
                    "name": fields[0],
                    "time": fields[31].replace(":", "") if len(fields) > 31 else "",
                    "source": "sina",
                }
            except (ValueError, IndexError) as e:
                logger.debug(f"新浪API解析异常: {e}")
                continue

    except Exception as e:
        logger.warning(f"新浪行情API请求失败: {e}")

    return results


# ============================================================
# 三-C、V10.4 盘中价格交叉校验
# ============================================================

def cross_validate_realtime(codes: list, primary_results: dict) -> dict:
    """
    V10.4: 盘中价格交叉校验 —— 用新浪API抽样校验腾讯API价格

    原理: 每50秒左右，对持仓股中随机抽取min(3, N)只，
    用新浪API获取价格，与腾讯API结果对比。
    偏差>0.5%时记录警告，>1%时标记可疑。

    参数:
        codes: 要校验的股票代码
        primary_results: 腾讯API返回的结果字典

    返回: {"checked": int, "discrepancies": int, "details": [...]}
    """
    import random

    result = {"checked": 0, "discrepancies": 0, "details": []}
    now = datetime.datetime.now()

    # 检查校验间隔
    last_check = _CROSSVALIDATE_STATE.get("last_check_time")
    interval = _CROSSVALIDATE_STATE.get("check_interval_seconds", 50)
    if last_check and (now - last_check).total_seconds() < interval:
        return result

    # 筛选有腾讯结果的股票进行抽样
    candidates = [c for c in codes if c in primary_results
                  and primary_results[c].get("price", 0) > 0]
    if not candidates:
        return result

    # 抽样校验（V10.5: 最多5只，覆盖率从15%提升至25%+）
    sample = random.sample(candidates, min(5, len(candidates)))
    sina_results = fetch_realtime_sina(sample)

    for code in sample:
        if code not in sina_results:
            continue
        result["checked"] += 1
        tc_price = primary_results[code].get("price", 0)
        sn_price = sina_results[code].get("price", 0)
        if tc_price <= 0 or sn_price <= 0:
            continue

        deviation = abs(tc_price - sn_price) / sn_price
        detail = {
            "code": code,
            "tencent_price": tc_price,
            "sina_price": sn_price,
            "deviation": round(deviation, 4),
            "time": now.strftime("%H:%M:%S"),
        }

        if deviation > 0.01:  # >1% 可疑
            logger.warning(
                f"[交叉校验] {code} 腾讯{tc_price:.3f} vs 新浪{sn_price:.3f} "
                f"偏差{deviation:.2%} > 1% ✗ 可能数据异常"
            )
            detail["status"] = "suspicious"
            result["discrepancies"] += 1
        elif deviation > 0.005:  # >0.5% 警告
            logger.info(
                f"[交叉校验] {code} 腾讯{tc_price:.3f} vs 新浪{sn_price:.3f} "
                f"偏差{deviation:.2%} > 0.5% ⚠️"
            )
            detail["status"] = "warning"
        else:
            detail["status"] = "ok"

        result["details"].append(detail)

    # 更新校验状态
    _CROSSVALIDATE_STATE["last_check_time"] = now
    log = _CROSSVALIDATE_STATE["discrepancy_log"]
    if result["discrepancies"] > 0:
        log.append({"time": now.isoformat(), "count": result["discrepancies"]})
        # 保留最近10条
        _CROSSVALIDATE_STATE["discrepancy_log"] = log[-10:]

    return result


# ============================================================
# 四、统一接口（多源容错）
# ============================================================

def fetch_realtime_batch(codes: list, source: str = "auto") -> dict:
    """
    批量获取实时行情（多源容错 + V10.4 硬兜底+交叉校验）
    
    参数:
        codes: 股票代码列表 ["600584", "002415", ...]
        source: 数据源 "auto"(自动切换) / "tencent" / "eastmoney" / "sina"
    
    返回:
        {code: {price, change_pct, high, low, volume, amount, name, time, source}}
    
    容错策略（V10.4增强）:
        1. 检查数据源健康状态，跳过冷却中的源
        2. Phase 1: 腾讯API批量获取（健康时）
        3. Phase 2: 东方财富补全缺失的（健康时）
        4. Phase 3: 新浪API硬兜底（V10.4新增，健康时）
        5. 所有源不可用时记录日志+返回空
        6. V10.4: 腾讯成功时触发交叉校验（抽样对比新浪）
    """
    if not codes:
        return {}
    
    results = {}
    sources_tried = 0
    import time as _time

    if source in ("auto", "tencent"):
        if _is_source_healthy("tencent"):
            sources_tried += 1
            _t0 = _time.time()
            _SOURCE_HEALTH["tencent"]["total_requests"] = \
                _SOURCE_HEALTH["tencent"].get("total_requests", 0) + 1
            # Phase 1: 腾讯API（快速批量）
            results = fetch_realtime_tencent(codes)
            _latency = (_time.time() - _t0) * 1000
            if results:
                _record_source_success("tencent", _latency)
                # V10.4: 交叉校验（腾讯成功时触发）
                if source == "auto":
                    try:
                        cross_validate_realtime(codes, results)
                    except Exception as e:
                        logger.debug(f"[交叉校验] 异常: {e}")
                if len(results) >= len(codes) * 0.8:
                    return results  # 80%以上成功，直接返回
            else:
                _record_source_failure("tencent", _latency)
        else:
            logger.debug("[数据源] tencent 冷却中，跳过")
    
    if source in ("auto", "eastmoney"):
        if _is_source_healthy("eastmoney"):
            sources_tried += 1
            # Phase 2: 东方财富补全缺失的
            missing = [c for c in codes if c not in results]
            if missing:
                _t0 = _time.time()
                _SOURCE_HEALTH["eastmoney"]["total_requests"] = \
                    _SOURCE_HEALTH["eastmoney"].get("total_requests", 0) + 1
                em_results = fetch_realtime_eastmoney(missing)
                _latency = (_time.time() - _t0) * 1000
                if em_results:
                    _record_source_success("eastmoney", _latency)
                    results.update(em_results)
                else:
                    _record_source_failure("eastmoney", _latency)
        else:
            logger.debug("[数据源] eastmoney 冷却中，跳过")

    # V10.4: Phase 3 — 新浪API硬兜底（解决全源冷却数据黑洞）
    missing_after_em = [c for c in codes if c not in results]
    if missing_after_em and source in ("auto", "sina"):
        if _is_source_healthy("sina"):
            sources_tried += 1
            _t0 = _time.time()
            _SOURCE_HEALTH["sina"]["total_requests"] = \
                _SOURCE_HEALTH["sina"].get("total_requests", 0) + 1
            sina_results = fetch_realtime_sina(missing_after_em)
            _latency = (_time.time() - _t0) * 1000
            if sina_results:
                _record_source_success("sina", _latency)
                results.update(sina_results)
                logger.info(f"[数据源] 新浪兜底成功: {len(sina_results)}只")
            else:
                _record_source_failure("sina", _latency)
        else:
            logger.debug("[数据源] sina 冷却中，跳过")
    
    # 统计
    missing_final = [c for c in codes if c not in results]
    if missing_final:
        logger.warning(f"实时行情未获取到: {missing_final}")
    
    # V10.4: 所有源都不可用时记录严重日志
    if not results and sources_tried == 0:
        logger.error("[数据源] 所有数据源均在冷却中，行情获取完全不可用！")
    
    return results


def fetch_realtime_single(code: str) -> dict:
    """
    获取单只股票实时行情
    返回: {price, change_pct, high, low, ...} 或空字典
    """
    results = fetch_realtime_batch([code])
    return results.get(code, {})


def fetch_index_realtime(index_code: str = "000300") -> dict:
    """
    获取指数实时行情（沪深300/上证指数等）
    V9.3: 腾讯API失败时自动降级到东方财富(akshare)
    """
    # Phase 1: 腾讯API
    result = _fetch_index_tencent(index_code)
    if result:
        return result
    
    # V9.3: Phase 2 备用 — 东方财富(akshare)
    if _is_source_healthy("eastmoney"):
        result = _fetch_index_eastmoney(index_code)
        if result:
            return result
    
    logger.warning(f"指数 {index_code} 所有数据源均不可用")
    return {}


def _fetch_index_tencent(index_code: str = "000300") -> dict:
    """指数行情 — 腾讯API"""
    # 指数代码转换
    if index_code == "000300":
        tc_code = "sh000300"
    elif index_code == "000001":
        tc_code = "sh000001"
    else:
        tc_code = _to_tencent_code(index_code)
    
    url = f"http://qt.gtimg.cn/q={tc_code}"
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("gbk", errors="ignore")
        
        if "=" not in content:
            return {}
        
        data_part = content.split("=", 1)[1].strip('"').strip(";").strip('"')
        fields = data_part.split("~")
        
        if len(fields) < 35:
            return {}
        
        return {
            "price": float(fields[3]) if fields[3] else 0,
            "prev_close": float(fields[4]) if fields[4] else 0,
            "change_pct": float(fields[32]) if fields[32] else 0,
            "high": float(fields[33]) if fields[33] else 0,
            "low": float(fields[34]) if fields[34] else 0,
            "volume": float(fields[6]) if fields[6] else 0,
            "name": fields[1],
            "time": fields[30] if len(fields) > 30 else "",
            "source": "tencent",
        }
    except Exception as e:
        logger.debug(f"指数腾讯API获取失败: {e}")
        return {}


def _fetch_index_eastmoney(index_code: str = "000300") -> dict:
    """V9.3: 指数行情备用 — 东方财富(akshare)"""
    if not HAS_AKSHARE:
        return {}
    try:
        # 指数代码映射（东方财富格式）
        em_code_map = {
            "000300": "sh000300",  # 沪深300
            "000001": "sh000001",  # 上证指数
            "399001": "sz399001",  # 深证成指
            "399006": "sz399006",  # 创业板指
        }
        em_code = em_code_map.get(index_code, f"sh{index_code}")
        market = "1" if em_code.startswith("sh") else "0"
        pure_code = em_code[2:]
        
        df = ak.stock_zh_index_daily_em(symbol=pure_code)
        if df is None or df.empty:
            return {}
        
        # 取最新一行
        latest = df.iloc[-1]
        # 修复BUG-037: prev_close取前一交易日close而非当日open
        if len(df) >= 2:
            prev_close = float(df.iloc[-2].get("close", 0) or 0)
        else:
            prev_close = float(latest.get("open", 0) or 0)
        close = float(latest.get("close", 0) or 0)
        
        if close <= 0:
            return {}
        
        change_pct = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0
        
        return {
            "price": close,
            "prev_close": prev_close,
            "change_pct": round(change_pct, 2),
            "high": float(latest.get("high", 0) or 0),
            "low": float(latest.get("low", 0) or 0),
            "volume": float(latest.get("volume", 0) or 0),
            "name": index_code,
            "time": datetime.datetime.now().strftime("%H%M%S"),
            "source": "eastmoney",
        }
    except Exception as e:
        logger.debug(f"指数东方财富API获取失败: {e}")
        return {}


# ============================================================
# 五、行业板块当日涨跌幅（内存缓存，批1-B公共模块）
# ============================================================

# 模块级内存缓存: {"date": "YYYY-MM-DD", "data": {...}} 或失败状态 {"date": ..., "failed": True}
_SECTOR_CHANGES_CACHE = {"date": None, "data": None, "failed": False}


def fetch_sector_changes_cached() -> dict:
    """
    获取东财行业板块当日涨跌幅（模块级内存缓存，当日只请求一次）

    返回: {"板块名": 涨跌幅百分比}，失败/未启用/非交易日无数据时返回 None

    缓存策略:
        - 当日命中直接返回（O(1)）
        - 失败状态记忆：当日不重试直接返回 None
        - 跨日自动失效重新拉取
    绝不抛异常。
    """
    global _SECTOR_CHANGES_CACHE
    try:
        today = datetime.date.today().strftime("%Y-%m-%d")

        # 当日命中（成功或失败记忆）直接返回
        if _SECTOR_CHANGES_CACHE.get("date") == today:
            if _SECTOR_CHANGES_CACHE.get("failed"):
                return None
            return _SECTOR_CHANGES_CACHE.get("data")

        # 跨日失效，重新拉取
        try:
            import akshare as ak  # 函数内延迟导入，缺失时降级
        except ImportError:
            _SECTOR_CHANGES_CACHE = {"date": today, "data": None, "failed": True}
            logger.debug("akshare未安装，板块涨跌幅不可用")
            return None

        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            _SECTOR_CHANGES_CACHE = {"date": today, "data": None, "failed": True}
            return None

        result = {}
        for _, row in df.iterrows():
            name = row.get("板块名称", "")
            pct = row.get("涨跌幅", None)
            if not name or pct is None:
                continue
            try:
                result[str(name)] = float(pct)
            except (TypeError, ValueError):
                continue

        if not result:
            _SECTOR_CHANGES_CACHE = {"date": today, "data": None, "failed": True}
            return None

        _SECTOR_CHANGES_CACHE = {"date": today, "data": result, "failed": False}
        return result
    except Exception as e:
        logger.warning(f"板块涨跌幅获取失败: {e}")
        try:
            today = datetime.date.today().strftime("%Y-%m-%d")
            _SECTOR_CHANGES_CACHE = {"date": today, "data": None, "failed": True}
        except Exception:
            pass
        return None
