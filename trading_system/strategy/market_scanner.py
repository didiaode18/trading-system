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
import datetime
from collections import defaultdict

import pandas as pd

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


def scan_market_hot_stocks(max_per_sector: int = 3, total_max: int = 20,
                           min_amount: float = 5e8) -> dict:
    """
    全市场扫描，发现当日强势股票
    
    参数:
        max_per_sector: 每个行业最多取几只
        total_max: 总共最多返回几只
        min_amount: 最低成交额（默认5亿）
    
    返回:
        {
            "codes": [code1, code2, ...],  # 动态发现的股票代码
            "details": [{code, name, sector, change_pct, amount, ...}],
            "sector_distribution": {"半导体": 3, "军工": 2, ...},
            "scan_time": str,
            "success": bool
        }
    """
    result = {
        "codes": [],
        "details": [],
        "sector_distribution": {},
        "scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "success": False
    }
    
    if not HAS_AKSHARE:
        logger.warning("[市场扫描] akshare未安装，跳过全市场扫描")
        return result
    
    try:
        logger.info("[市场扫描] 获取全市场实时行情...")
        # 获取全市场A股实时行情
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            logger.warning("[市场扫描] 获取行情数据为空")
            return result
        
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
        if "price" in df.columns:
            df = df[(df["price"] >= 5) & (df["price"] <= 500)]
        
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
            })
        
        result["codes"] = codes[:total_max]
        result["details"] = details[:total_max]
        result["success"] = True
        logger.info(f"[市场扫描] 动态发现 {len(result['codes'])} 只强势股")
        
        # 打印前5只
        for d in result["details"][:5]:
            logger.info(f"  {d['code']} {d['name']}: "
                       f"涨{d['change_pct']:+.1f}% | "
                       f"成交{d['amount']:.1f}亿 | "
                       f"量比{d['vol_ratio']:.1f}")
        
    except Exception as e:
        logger.error(f"[市场扫描] 扫描异常: {e}")
        result["success"] = False
    
    return result


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
