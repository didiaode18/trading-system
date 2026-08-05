"""
全市场动态扫描模块
==================
利用akshare获取全市场实时行情，动态发现强势股票补充候选池

核心逻辑:
  1. 获取全市场A股实时行情快照
  2. 预筛选: 成交额>5亿、非ST、非次新、近5日有资金关注
  3. 按行业分组，每个行业取前3-5只
  4. 返回动态发现的股票代码列表，供data_loader拉取历史数据

使用方式:
    from strategy.market_scanner import scan_market_hot_stocks
    hot_codes = scan_market_hot_stocks()
"""

import logging
import os
import time
import datetime
import json
import urllib.request
from collections import defaultdict

import pandas as pd

logger = logging.getLogger(__name__)

try:
    import config as _config
except ImportError:
    try:
        from trading_system import config as _config
    except ImportError:
        _config = None

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False

# V2.4: 扫描结果缓存（网络失败时降级使用）
_SCAN_CACHE = {"data": None, "time": None}  # 有效期24小时

# V3.0-FIX P0: 缓存持久化落盘路径（内存缓存进程重启即失，且从未成功过则永远无兑底）
_SCAN_CACHE_FILE = None
if _config is not None:
    try:
        _SCAN_CACHE_FILE = os.path.join(getattr(_config, "DATA_DIR", ""), "scan_cache.json")
    except Exception:
        _SCAN_CACHE_FILE = None


# ============================================================
# V2.9: 东方财富直接HTTP接口（akshare失败时的备用通道）
# ============================================================

_EM_SPOT_URL = "http://82.push2.eastmoney.com/api/qt/clist/get"
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "http://quote.eastmoney.com/",
}


def _fetch_spot_em_direct(page_size: int = 5000) -> pd.DataFrame:
    """
    V2.9: 直接HTTP请求东方财富全市场行情（绕过akshare封装）
    
    返回与 ak.stock_zh_a_spot_em() 兼容的 DataFrame
    
    FIX P1: 服务端单页实际只返回约100行，原实现未翻页导致备用通道仅能覆盖100只；
    现改为按涨幅降序翻页拉取（最多_MAX_PAGES页），满足强势股扫描需求。
    """
    _MAX_PAGES = 30  # 约3000只，覆盖涨幅榜前列（扫描仅取强势股，无需全市场）
    all_rows = []
    seen = set()
    for page in range(1, _MAX_PAGES + 1):
        params = (
            f"?pn={page}&pz={page_size}&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
            f"&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
            f"&fields=f2,f3,f5,f6,f8,f10,f12,f14,f15,f16,f17"
        )
        url = _EM_SPOT_URL + params
        req = urllib.request.Request(url, headers=_EM_HEADERS)

        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")

        data = json.loads(raw)
        if not data or data.get("data") is None:
            break

        items = data["data"].get("diff", [])
        if not items:
            break

        # 映射东方财富字段 -> 标准列名
        for item in items:
            code = str(item.get("f12", "")).zfill(6)
            if code in seen:
                continue
            seen.add(code)
            all_rows.append({
                "代码": code,
                "名称": item.get("f14", ""),
                "最新价": item.get("f2"),       # 价格(分->元已处理)
                "涨跌幅": item.get("f3"),       # %
                "成交额": item.get("f6"),       # 元
                "换手率": item.get("f8"),       # %
                "量比": item.get("f10"),
                "最高": item.get("f15"),
                "最低": item.get("f16"),
                "今开": item.get("f17"),
            })

        # 返回不足一页 → 已到末尾；涨幅榜跌破扫描关注线(-2%)也可提前终止
        if len(items) < 100:
            break
        try:
            last_chg = float(items[-1].get("f3", 0))
            if last_chg < -2:
                break
        except (TypeError, ValueError):
            pass

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    # 东方财富返回 "-" 表示无效值
    for col in ["最新价", "涨跌幅", "成交额", "换手率", "量比"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    logger.info(f"[市场扫描] 东财直接HTTP翻页完成: {len(df)}只 ({page}页)")
    return df


def scan_market_hot_stocks(max_per_sector: int = 3, total_max: int = 20,
                           min_amount: float = 5e8, max_retries: int = 3) -> dict:
    """
    全市场扫描，发现当日强势股票
    
    参数:
        max_per_sector: 每个行业最多取几只
        total_max: 总共最多返回几只
        min_amount: 最低成交额（默认5亿）
        max_retries: 网络异常重试次数（V2.4）
    
    返回:
        {
            "codes": [code1, code2, ...],  # 动态发现的股票代码
            "details": [{code, name, sector, change_pct, amount, ...}],
            "sector_distribution": {"半导体": 3, "军工": 2, ...},
            "scan_time": str,
            "success": bool,
            "from_cache": bool,  # V2.4: 是否使用缓存降级
        }
    """
    result = {
        "codes": [],
        "details": [],
        "sector_distribution": {},
        "scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "success": False,
        "from_cache": False,
    }
    
    # V2.9: 多源容错获取全市场行情
    df = None
    last_error = None
    
    # 方案1: akshare封装（原有逻辑）
    if HAS_AKSHARE:
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"[市场扫描] akshare获取全市场行情...(第{attempt}次)")
                df = ak.stock_zh_a_spot_em()
                if df is not None and not df.empty:
                    break
                logger.warning(f"[市场扫描] 第{attempt}次获取数据为空")
                df = None
            except (ConnectionError, TimeoutError, OSError) as e:
                last_error = e
                logger.warning(f"[市场扫描] akshare第{attempt}次网络异常: {e}")
                if attempt < max_retries:
                    time.sleep(2)
            except Exception as e:
                last_error = e
                logger.warning(f"[市场扫描] akshare第{attempt}次异常: {e}")
                if attempt < max_retries:
                    time.sleep(2)
    
    # 方案2: V2.9直接HTTP请求东方财富API（绕过akshare反爬限制）
    if df is None or (hasattr(df, 'empty') and df.empty):
        try:
            logger.info("[市场扫描] akshare失败，切换东方财富直接HTTP接口...")
            df = _fetch_spot_em_direct()
            if df is not None and not df.empty:
                logger.info(f"[市场扫描] 直接HTTP成功: {len(df)}只")
            else:
                df = None
        except Exception as e:
            last_error = e
            logger.warning(f"[市场扫描] 直接HTTP也失败: {e}")
            df = None
    
    # V2.4: 全部失败，尝试使用缓存降级
    if df is None or df.empty:
        cache_data = _get_scan_cache()
        if cache_data is not None:
            logger.info("[市场扫描] 重试失败，使用缓存扫描结果降级")
            cache_data["from_cache"] = True
            cache_data["scan_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + "(缓存)"
            return cache_data
        logger.error(f"[市场扫描] 全部数据源失败且无缓存: {last_error}")
        return result
    
    # 数据获取成功，开始处理
    try:
        logger.info(f"[市场扫描] 获取到 {len(df)} 只股票行情")
        
        # 标准化列名
        col_map = {
            "代码": "code",
            "名称": "name",
            "最新价": "price",
            "涨跌幅": "change_pct",
            "成交额": "amount",
            "换手率": "turnover",
            "量比": "vol_ratio",
            "60日涨跌幅": "change_60d",
        }
        df = df.rename(columns=col_map)
        
        # 确保数值列
        for col in ["price", "change_pct", "amount", "turnover", "vol_ratio"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        
        # ---- 预筛选 ----
        initial_count = len(df)
        
        # 1. 排除ST股
        df = df[~df["name"].str.contains("ST|退", na=False)]
        
        # 2. 排除次新股（代码以N/C开头的名称）
        df = df[~df["name"].str.startswith(("N", "C"), na=False)]
        
        # 3. 成交额 > min_amount
        if "amount" in df.columns:
            df = df[df["amount"] > min_amount]
        
        # 4. 股价合理（排除低价股和超高价股）
        # FIX P2: 上限500元会误杀寒武纪等高价龙头，改用config.DYNAMIC_SCAN_MAX_PRICE(1500)
        _max_price = getattr(_config, "DYNAMIC_SCAN_MAX_PRICE", 1500) if _config else 1500
        if "price" in df.columns:
            df = df[(df["price"] >= 5) & (df["price"] <= _max_price)]
        
        # 5. 当日涨跌幅 > -2%（排除暴跌股）
        if "change_pct" in df.columns:
            df = df[df["change_pct"] > -2]
        
        logger.info(f"[市场扫描] 预筛选后剩余 {len(df)} 只（原始{initial_count}只）")
        
        if df.empty:
            return result
        
        # ---- 按强势程度排序 ----
        # 综合评分: 当日涨幅(40%) + 量比(30%) + 换手率(30%)
        df["score"] = 0.0
        if "change_pct" in df.columns:
            df["score"] += df["change_pct"].clip(-5, 10) * 4
        if "vol_ratio" in df.columns:
            df["score"] += df["vol_ratio"].clip(0, 5) * 3
        if "turnover" in df.columns:
            df["score"] += df["turnover"].clip(0, 15) * 2
        
        df = df.sort_values("score", ascending=False)
        
        # ---- 尝试获取行业信息并分组 ----
        # 简化处理：按代码前缀粗略分组（实际应用中应获取行业分类）
        # 这里直接取综合评分最高的股票
        selected = df.head(total_max)
        
        codes = []
        details = []
        for _, row in selected.iterrows():
            code = str(row.get("code", "")).zfill(6)
            if not code or len(code) != 6:
                continue
            # 排除已停牌的（最新价为0或NaN）
            price = row.get("price", 0)
            if pd.isna(price) or price <= 0:
                continue
            
            codes.append(code)
            details.append({
                "code": code,
                "name": row.get("name", ""),
                "price": round(float(price), 2),
                "change_pct": round(float(row.get("change_pct", 0)), 2),
                "amount": round(float(row.get("amount", 0)) / 1e8, 2),  # 转为亿
                "turnover": round(float(row.get("turnover", 0)), 2),
                "vol_ratio": round(float(row.get("vol_ratio", 0)), 2),
                "sector": classify_stock_sector(str(row.get("name", ""))),  # V2.3-P3
            })
        
        result["codes"] = codes[:total_max]
        result["details"] = details[:total_max]
        result["success"] = True
        result["from_cache"] = False
        logger.info(f"[市场扫描] 动态发现 {len(result['codes'])} 只强势股")
        
        # V2.4: 成功时更新缓存
        _set_scan_cache(result)
        
        # 打印前5只
        for d in result["details"][:5]:
            logger.info(f"  {d['code']} {d['name']}: "
                       f"涨{d['change_pct']:+.1f}% | "
                       f"成交{d['amount']:.1f}亿 | "
                       f"量比{d['vol_ratio']:.1f}")
        
    except Exception as e:
        logger.error(f"[市场扫描] 扫描异常: {e}")
        # V2.4: 处理阶段异常也尝试缓存降级
        cache_data = _get_scan_cache()
        if cache_data is not None:
            logger.info("[市场扫描] 处理异常，使用缓存扫描结果降级")
            cache_data["from_cache"] = True
            return cache_data
        result["success"] = False
    
    return result


def _get_scan_cache() -> dict:
    """V2.4: 获取缓存的扫描结果（有效期24小时）
    
    FIX P0: 新增磁盘缓存层——内存缓存进程重启即失，且首次成功前永远无兑底；
    现优先读内存，内存无则读磁盘文件（跨进程/跨重启有效）。
    """
    global _SCAN_CACHE
    if _SCAN_CACHE["data"] is not None and _SCAN_CACHE["time"] is not None:
        elapsed = (datetime.datetime.now() - _SCAN_CACHE["time"]).total_seconds()
        if elapsed <= 86400:  # 24小时内有效
            import copy
            return copy.deepcopy(_SCAN_CACHE["data"])
        _SCAN_CACHE = {"data": None, "time": None}

    # 磁盘缓存降级
    if _SCAN_CACHE_FILE and os.path.exists(_SCAN_CACHE_FILE):
        try:
            with open(_SCAN_CACHE_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
            saved_at = datetime.datetime.strptime(payload.get("saved_at", ""), "%Y-%m-%d %H:%M:%S")
            if (datetime.datetime.now() - saved_at).total_seconds() <= 86400:
                data = payload.get("data") or {}
                if data.get("codes"):
                    # 回填内存缓存
                    _SCAN_CACHE = {"data": data, "time": saved_at}
                    logger.info(f"[市场扫描] 磁盘缓存命中 ({payload.get('saved_at')}, {len(data['codes'])}只)")
                    return data
        except Exception as e:
            logger.warning(f"[市场扫描] 磁盘缓存读取失败: {e}")
    return None


def _set_scan_cache(result: dict):
    """V2.4: 缓存成功的扫描结果（FIX P0: 同步落盘，跨进程可用）"""
    global _SCAN_CACHE
    import copy
    now = datetime.datetime.now()
    _SCAN_CACHE["data"] = copy.deepcopy(result)
    _SCAN_CACHE["time"] = now
    # 落盘持久化
    if _SCAN_CACHE_FILE:
        try:
            os.makedirs(os.path.dirname(_SCAN_CACHE_FILE), exist_ok=True)
            with open(_SCAN_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"saved_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                           "data": result}, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[市场扫描] 磁盘缓存写入失败: {e}")


def merge_scan_results_to_pool(scan_result: dict, existing_codes: set) -> list:
    """
    将动态扫描结果与现有候选池合并（去重）
    
    参数:
        scan_result: scan_market_hot_stocks()的返回
        existing_codes: 已有的股票代码集合
    
    返回:
        新增的股票代码列表（不在existing_codes中的）
    """
    if not scan_result.get("success"):
        return []
    
    new_codes = []
    for code in scan_result["codes"]:
        if code not in existing_codes:
            new_codes.append(code)
    
    logger.info(f"[市场扫描] 新增 {len(new_codes)} 只动态候选股（去重后）")
    return new_codes


# V2.3-P3: 动态扫描结果自动行业分类（关键词匹配，轻量级无额外API调用）
_SECTOR_KEYWORDS = {
    "半导体": ["芯", "半导", "微电子", "集成电路", "晶圆", "光刻", "封装", "存储"],
    "军工航天": ["军", "航", "船舶", "导弹", "雷达", "卫星", "飞机", "重工"],
    "AI数字经济": ["智能", "AI", "人工", "数据", "云计算", "软件", "信息", "数字"],
    "新能源": ["新能源", "光伏", "锂电", "储能", "风电", "氢能", "电池", "充电"],
    "医药医疗": ["医", "药", "生物", "基因", "细胞", "诊断", "器械"],
    "大消费": ["酒", "食品", "饮料", "养殖", "农业", "服装", "家电", "零售"],
    "大金融": ["银行", "证券", "保险", "信托", "基金", "期货"],
    "有色资源": ["矿", "铜", "铝", "锂", "钴", "稀土", "黄金", "钢铁", "煤"],
}


def classify_stock_sector(name: str) -> str:
    """根据股票名称关键词匹配行业分类"""
    if not name:
        return "其他"
    for sector, keywords in _SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                return sector
    return "其他"
