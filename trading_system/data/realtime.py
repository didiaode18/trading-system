"""
实时行情精准获取模块
====================
多源容错获取A股实时行情，专为盘中监控设计

数据源优先级:
  1. 腾讯行情API（批量，一次最多60只，延迟<1秒）
  2. 东方财富API（akshare，单只/批量）
  3. akshare全市场接口（最后手段，慢但稳定）

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

def fetch_realtime_eastmoney(codes: list) -> dict:
    """
    东方财富实时行情（通过akshare）
    比腾讯慢但数据更全
    """
    if not HAS_AKSHARE or not codes:
        return {}
    
    results = {}
    try:
        # 使用akshare的实时行情接口（获取全市场，然后筛选）
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return {}
        
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
                "amount": float(row.get("成交额", 0) or 0),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "turnover": float(row.get("换手率", 0) or 0),
                "amplitude": float(row.get("振幅", 0) or 0),
                "name": row.get("名称", ""),
                "time": datetime.datetime.now().strftime("%H%M%S"),
                "source": "eastmoney",
            }
    except Exception as e:
        logger.warning(f"东方财富实时行情获取失败: {e}")
    
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
# 四、统一接口（多源容错）
# ============================================================

def fetch_realtime_batch(codes: list, source: str = "auto") -> dict:
    """
    批量获取实时行情（多源容错）
    
    参数:
        codes: 股票代码列表 ["600584", "002415", ...]
        source: 数据源 "auto"(自动切换) / "tencent" / "eastmoney"
    
    返回:
        {code: {price, change_pct, high, low, volume, amount, name, time, source}}
    
    容错策略:
        1. 先用腾讯API批量获取
        2. 未获取到的用东方财富补全
        3. 仍未获取到的记录日志
    """
    if not codes:
        return {}
    
    results = {}
    
    if source in ("auto", "tencent"):
        # Phase 1: 腾讯API（快速批量）
        results = fetch_realtime_tencent(codes)
        if len(results) >= len(codes) * 0.8:
            return results  # 80%以上成功，直接返回
    
    if source in ("auto", "eastmoney"):
        # Phase 2: 东方财富补全缺失的
        missing = [c for c in codes if c not in results]
        if missing:
            em_results = fetch_realtime_eastmoney(missing)
            results.update(em_results)
    
    # 统计
    missing_final = [c for c in codes if c not in results]
    if missing_final:
        logger.warning(f"实时行情未获取到: {missing_final}")
    
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
    腾讯API: sh000300(沪深300), sh000001(上证指数)
    """
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
        logger.warning(f"指数实时行情获取失败: {e}")
        return {}
