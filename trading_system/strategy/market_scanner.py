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


_SINA_SPOT_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/"
                  "api/json_v2.php/Market_Center.getHQNodeData")


def _fetch_spot_sina_direct(pages: int = 15, num_per_page: int = 80) -> pd.DataFrame:
    """
    V4.4 P4: 新浪行情中心直连HTTP（按涨幅降序翻页，兼容东财列名的备用源3）

    akshare与东财均失败时的第三道兜底（历史案例: 两源同日全灭致盘中发现通道瘫痪）。
    新浪接口无量比字段，量比置NaN由评分侧按缺省值处理。
    返回与东财直连同构的DataFrame（中文列名，复用_normalize_spot_df标准化）。
    """
    import re
    import requests
    rows = []
    headers = {
        "User-Agent": _EM_HEADERS["User-Agent"],
        "Referer": "https://finance.sina.com.cn/",
    }
    try:
        for page in range(1, pages + 1):
            params = {"page": page, "num": num_per_page, "sort": "changepercent",
                      "asc": 0, "node": "hs_a", "symbol": "", "_s_r_a": "page"}
            resp = requests.get(_SINA_SPOT_URL, params=params, timeout=10,
                                headers=headers)
            if resp.status_code != 200 or not resp.text.strip() or resp.text.strip() == "null":
                break
            resp.encoding = "gbk"  # 新浪接口返回GB2312编码
            # 新浪返回非标准JSON（键无引号），补齐后解析
            _txt = re.sub(r'([{,])(\w+):', r'\1"\2":', resp.text)
            try:
                items = json.loads(_txt)
            except Exception:
                break
            if not items:
                break
            for r in items:
                try:
                    price = float(r.get("trade") or 0)
                    if price <= 0:  # 停牌股
                        continue
                    rows.append({
                        "代码": str(r.get("code", "")),
                        "名称": r.get("name", ""),
                        "最新价": price,
                        "涨跌幅": float(r.get("changepercent") or 0),
                        "成交额": float(r.get("amount") or 0),
                        "换手率": float(r.get("turnoverratio") or 0),
                        "量比": float("nan"),
                    })
                except (TypeError, ValueError):
                    continue
        if rows:
            logger.info(f"[市场扫描] 新浪直连HTTP翻页完成: {len(rows)}只 ({page}页)")
            return pd.DataFrame(rows)
    except Exception as e:
        logger.warning(f"[市场扫描] 新浪直连失败: {e}")
    return None


def _fetch_spot_df(max_retries: int = 3):
    """V4.0(G3): 多源容错获取全市场行情快照（akshare→东财直连→新浪直连）

    返回: (df或None, last_error)，供 scan_market_hot_stocks 与
    build_investable_universe 复用，避免两套拉取逻辑分叉
    """
    df = None
    last_error = None
    if HAS_AKSHARE:
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"[市场扫描] akshare获取全市场行情...(第{attempt}次)")
                df = ak.stock_zh_a_spot_em()
                if df is not None and not df.empty:
                    return df, None
                logger.warning(f"[市场扫描] 第{attempt}次获取数据为空")
                df = None
            except Exception as e:
                last_error = e
                logger.warning(f"[市场扫描] akshare第{attempt}次异常: {e}")
                if attempt < max_retries:
                    time.sleep(2)
    try:
        logger.info("[市场扫描] akshare失败，切换东方财富直接HTTP接口...")
        df = _fetch_spot_em_direct()
        if df is not None and not df.empty:
            logger.info(f"[市场扫描] 直接HTTP成功: {len(df)}只")
            return df, None
        df = None
    except Exception as e:
        last_error = e
        logger.warning(f"[市场扫描] 直接HTTP也失败: {e}")
    # V4.4 P4: 备用源3（新浪直连），两源全灭时的第三道兜底
    try:
        logger.info("[市场扫描] 东财直连失败，切换新浪直接HTTP接口...")
        df = _fetch_spot_sina_direct()
        if df is not None and not df.empty:
            logger.info(f"[市场扫描] 新浪直连成功: {len(df)}只")
            return df, None
        df = None
    except Exception as e:
        last_error = e
        logger.warning(f"[市场扫描] 新浪直连也失败: {e}")
    return None, last_error


def _normalize_spot_df(df) -> "pd.DataFrame":
    """V4.0(G3): 行情快照列名标准化 + 数值列转换"""
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
    for col in ["price", "change_pct", "amount", "turnover", "vol_ratio"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def build_investable_universe(min_amount: float = 2e8, max_retries: int = 3) -> dict:
    """
    V4.0(G3): 全市场可投资域粗筛漏斗（Investable Universe）

    过滤链（逐项统计剔除数，便于度量覆盖质量）:
      1. ST/*ST/退市整理股
      2. N/C开头次新股（上市初期波动大、无足够历史K线）
      3. 停牌股（价格无效或成交额为0）
      4. 流动性不足（成交额 < min_amount，默认2亿）
      5. 股价区间（3元 ≤ 价 ≤ DYNAMIC_SCAN_MAX_PRICE）

    返回:
        {"success": bool, "total": int(全市场总数), "size": int(投资域大小),
         "codes": [str], "df": DataFrame, "filter_stats": {过滤项: 剔除数}}
    """
    result = {"success": False, "total": 0, "size": 0,
              "codes": [], "df": None, "filter_stats": {}}
    df, _err = _fetch_spot_df(max_retries)
    if df is None or df.empty:
        logger.warning("[投资域] 行情快照获取失败，无法构建可投资域")
        return result

    df = _normalize_spot_df(df)
    result["total"] = len(df)
    stats = {}

    def _apply(mask, label):
        nonlocal df
        removed = int((~mask).sum())
        stats[label] = removed
        df = df[mask]

    if "name" in df.columns:
        _apply(~df["name"].str.contains("ST|退", na=False), "ST/退市剔除")
        _apply(~df["name"].str.startswith(("N", "C"), na=False), "次新股剔除")
    if "price" in df.columns:
        _apply(df["price"].notna() & (df["price"] > 0), "停牌/无效价剔除")
    if "amount" in df.columns:
        _apply(df["amount"].fillna(0) >= min_amount, "流动性不足剔除")
    if "price" in df.columns:
        _max_price = getattr(_config, "DYNAMIC_SCAN_MAX_PRICE", 1500) if _config else 1500
        _apply((df["price"] >= 3) & (df["price"] <= _max_price), "股价区间剔除")

    codes = [str(c).zfill(6) for c in df["code"].astype(str)] if "code" in df.columns else []
    result.update({"success": True, "size": len(df), "codes": codes,
                   "df": df, "filter_stats": stats})
    logger.info(f"[投资域] 全市场{result['total']}只 → 可投资域{result['size']}只 | "
                f"剔除明细: {stats}")
    return result


def scan_market_hot_stocks(max_per_sector: int = 3, total_max: int = 20,
                           min_amount: float = 5e8, max_retries: int = 3,
                           spot_df=None) -> dict:
    """
    全市场扫描，发现当日强势股票
    
    参数:
        max_per_sector: 每个行业最多取几只
        total_max: 总共最多返回几只
        min_amount: 最低成交额（默认5亿）
        max_retries: 网络异常重试次数（V2.4）
        spot_df: V4.0(G3) 可选外部传入的行情快照（列名不限，内部标准化），
                 传入后不再重复拉取网络，供与build_investable_universe共享单次拉取
    
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
    
    # V2.9/V4.0(G3): 多源容错获取全市场行情（统一拉取入口，支持外部传入免重复拉取）
    if spot_df is not None and not getattr(spot_df, "empty", True):
        df, last_error = spot_df.copy(), None
    else:
        df, last_error = _fetch_spot_df(max_retries)

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

        # V4.0(G3): 统一列名标准化
        df = _normalize_spot_df(df)
        
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

        # V4.4: 横盘企稳口径 —— 预留部分名额给低波动企稳股（非仅当日强势），
        # 减少静默筑底股盲区；企稳候选后续仍由选股引擎hard_filter全量校验。
        # 异常时降级为纯强势口径（旧行为）。
        _base_max = 0
        _base_codes = set()
        try:
            _base_ratio = float(getattr(_config, "SCAN_BASE_RATIO", 0.2)) if _config else 0.2
            if _base_ratio > 0 and total_max > 1 and all(
                    c in df.columns for c in ("change_pct", "vol_ratio", "turnover")):
                _base_max = min(int(total_max * _base_ratio), total_max - 1)
                _momentum_max = total_max - _base_max
                selected = df.head(_momentum_max)
                _base_cand = df[(df["change_pct"].between(-1.0, 3.0))
                                & (df["vol_ratio"].fillna(1.0).between(0.6, 2.5))
                                & (df["turnover"].fillna(0.0).between(1.0, 12.0))]
                _selected_codes = {str(c).zfill(6) for c in selected["code"]}
                _base_cand = _base_cand[~_base_cand["code"].astype(str).str.zfill(6).isin(_selected_codes)]
                # 量比越低越缩量企稳，优先纳入
                _base_cand = _base_cand.sort_values("vol_ratio").head(_base_max)
                _base_codes = {str(c).zfill(6) for c in _base_cand["code"]}
                selected = pd.concat([selected, _base_cand], ignore_index=True)
                if _base_codes:
                    logger.info(f"[市场扫描] 企稳口径纳入{len(_base_codes)}只: {sorted(_base_codes)}")
            else:
                selected = df.head(total_max)
        except Exception as _e:
            logger.warning(f"[市场扫描] 企稳口径异常, 降级纯强势口径: {_e}")
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
                "scan_tag": "企稳" if code in _base_codes else "强势",  # V4.4: 口径溯源
            })
        
        result["codes"] = codes[:total_max]
        result["details"] = details[:total_max]
        result["success"] = True
        result["from_cache"] = False
        logger.info(f"[市场扫描] 动态发现 {len(result['codes'])} 只强势股")
        
        # V2.4: 成功时更新缓存（V4.4: 外部注入spot_df为测试/共享场景，不写生产缓存）
        if spot_df is None:
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


# ============================================================
# V4.4: 盘后全量复扫可投资域（两段漏斗，补充观察池）
# ============================================================

def run_universe_rescan() -> dict:
    """V4.4: 盘后对可投资域做全量复扫，发现池外筑底/企稳强势股补充观察池

    两段漏斗:
      1. 快照粗筛: 可投资域内当日涨跌温和(-3%~7%) + 量比不异常，按成交额取前N
      2. K线精筛: MA20向上 + 收盘站上MA20 + 距20日高点回撤≤8% + 60日跌幅≤20%
         （与选股引擎hard_filter强势市口径对齐，仅取其核心四条控成本）
    合格且不在现有池中的标的补充进观察池（受WATCH_POOL_MAX约束），
    次日09:25选股自动纳入候选。全程异常静默降级，不影响盘后主流程。
    """
    result = {"success": False, "candidates": [], "pool_added": [], "output_file": ""}
    if _config is None or not getattr(_config, "UNIVERSE_RESCAN_ENABLED", True):
        logger.info("[全域复扫] 开关关闭，跳过")
        return result

    try:
        # ---- 第一段: 快照粗筛 ----
        universe = build_investable_universe(min_amount=5e8)
        if not universe.get("success") or universe.get("df") is None or universe["df"].empty:
            logger.warning("[全域复扫] 行情快照获取失败，本次复扫跳过")
            return result
        df = universe["df"]

        _kline_max = int(getattr(_config, "UNIVERSE_RESCAN_KLINE_MAX", 80))
        mask = df["change_pct"].between(-3.0, 7.0)
        if "vol_ratio" in df.columns:
            mask &= df["vol_ratio"].fillna(1.0).between(0.5, 3.5)
        coarse = df[mask].sort_values("amount", ascending=False).head(_kline_max)
        logger.info(f"[全域复扫] 可投资域{universe['size']}只 → 粗筛{_kline_max}只进入K线精筛")

        # ---- 第二段: K线精筛（复用选股引擎强势市核心口径）----
        from data.data_loader import fetch_stock_daily_baostock, _bs_logout
        start = (datetime.datetime.now() - datetime.timedelta(days=300)).strftime("%Y-%m-%d")
        candidates = []
        for _, row in coarse.iterrows():
            code = str(row.get("code", "")).zfill(6)
            # 与选股引擎一致: 创业板/科创板/ETF不参与
            if code.startswith(("300", "688", "588", "159")):
                continue
            try:
                kdf = fetch_stock_daily_baostock(code, start_date=start)
                if kdf is None or len(kdf) < 60:
                    continue
                close = kdf["close"]
                ma20 = close.rolling(20).mean()
                ma20_slope = ma20.diff(3).iloc[-1]
                high20 = kdf["high"].rolling(20).max().iloc[-1]
                c = float(close.iloc[-1])
                if pd.isna(ma20.iloc[-1]) or pd.isna(ma20_slope):
                    continue
                if ma20_slope <= 0 or c <= float(ma20.iloc[-1]):
                    continue
                if high20 and c < float(high20) * 0.92:
                    continue  # 距20日高点回撤>8%
                if len(close) >= 60 and c < float(close.iloc[-60]) * 0.8:
                    continue  # 60日累计跌幅>20%
                candidates.append({
                    "code": code,
                    "name": str(row.get("name", "")),
                    "price": round(c, 2),
                    "change_pct": round(float(row.get("change_pct", 0)), 2),
                    "amount": round(float(row.get("amount", 0)) / 1e8, 2),
                    "vol_ratio": round(float(row.get("vol_ratio", 0) or 0), 2),
                    "sector": classify_stock_sector(str(row.get("name", ""))),
                })
            except Exception:
                continue
        _bs_logout()
        result["candidates"] = candidates
        result["success"] = True
        logger.info(f"[全域复扫] K线精筛通过{len(candidates)}只")

        # ---- 候选落盘（供人工复核/后续追溯）----
        try:
            _out_dir = getattr(_config, "OUTPUT_DIR", "")
            if _out_dir:
                _out_file = os.path.join(
                    _out_dir, f"universe_rescan_{datetime.datetime.now():%Y%m%d}.json")
                os.makedirs(_out_dir, exist_ok=True)
                with open(_out_file, "w", encoding="utf-8") as f:
                    json.dump({"scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               "universe_size": universe["size"],
                               "candidates": candidates}, f, ensure_ascii=False, indent=1)
                result["output_file"] = _out_file
        except Exception as e:
            logger.warning(f"[全域复扫] 候选落盘失败(不阻断): {e}")

        # ---- 补充观察池（受WATCH_POOL_MAX约束，不驱逐存量）----
        if candidates:
            try:
                from strategy.pool_manager import PoolManager
                pm = PoolManager()
                watch_max = int(getattr(_config, "WATCH_POOL_MAX", 15))
                add_max = int(getattr(_config, "UNIVERSE_RESCAN_POOL_ADD_MAX", 5))
                for item in candidates:
                    if len(result["pool_added"]) >= add_max:
                        break
                    if len(pm.watch_pool) >= watch_max:
                        logger.info("[全域复扫] 观察池已满，其余候选仅落盘")
                        break
                    code = item["code"]
                    if (code in pm.core_pool or code in pm.watch_pool
                            or code in pm.blacklist):
                        continue
                    pm.watch_pool[code] = {
                        "名称": item["name"],
                        "赛道": item["sector"],
                        "类型": "全域复扫",
                        "observe_start": datetime.date.today().strftime("%Y-%m-%d"),
                        "score": 0,
                    }
                    result["pool_added"].append(code)
                if result["pool_added"]:
                    pm._save()
                    logger.info(f"[全域复扫] 观察池新增{len(result['pool_added'])}只: "
                                f"{result['pool_added']}")
            except Exception as e:
                logger.warning(f"[全域复扫] 观察池补充失败(不阻断): {e}")
    except Exception as e:
        logger.warning(f"[全域复扫] 异常(不阻断): {e}")
    return result


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
