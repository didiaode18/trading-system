# -*- coding: utf-8 -*-
"""
板块轮动背离分析模块（任务#1新增）
==================================
基于 stock_screener 每日09:25写入的 sector_rotation_cache.json，分析:
  1. 持仓赛道评分对照（细粒度赛道评分归一化到粗粒度）
  2. 滞涨持仓识别（赛道落入weak名单或评分处于后1/3 → 建议减持）
  3. 未覆盖的强势热点赛道（missed_hotspots）
  4. 今日最强3赛道

另提供 merge_report_candidates() 三源候选合并去重函数（静态池/观察池/动态扫描），
供 generate_holdings_report.py 选股扩面使用。

纯函数实现，不依赖网络，可独立单元测试。
stock_screener 导入失败时自动降级为内置简化映射表，绝不抛异常。
"""

import json
import datetime


# 内置简化映射表（细粒度赛道 → 粗粒度赛道）
# 作为 stock_screener._build_coarse_sector_map 导入失败时的降级方案
_FALLBACK_COARSE_MAP = {
    # 半导体
    "半导体设备": "半导体", "半导体材料": "半导体", "半导体封测": "半导体",
    "存储芯片": "半导体", "CIS芯片": "半导体", "芯片设计": "半导体",
    "晶圆代工": "半导体", "AI芯片": "半导体", "CPU芯片": "半导体",
    "内存接口芯片": "半导体", "半导体测试": "半导体",
    # 军工航天
    "军工航空": "军工航天", "卫星导航": "军工航天", "军用飞机": "军工航天",
    "航空发动机": "军工航天", "军工船舶": "军工航天",
    # AI数字经济
    "AI应用": "AI数字经济", "AI视觉": "AI数字经济",
    # 新能源
    "新能源车": "新能源", "光伏": "新能源",
    # 医药医疗
    "创新药": "医药医疗", "生物制品": "医药医疗",
    # 大消费
    "白酒": "大消费", "调味品": "大消费", "养殖": "大消费",
    # 大金融
    "保险": "大金融", "银行": "大金融", "证券": "大金融",
    # 有色资源
    "黄金铜矿": "有色资源", "锂矿": "有色资源", "钴铜矿": "有色资源",
}

# 缓存有效期（小时）
CACHE_VALID_HOURS = 24


def get_coarse_sector_map():
    """
    获取 细粒度赛道→粗粒度赛道 映射表

    优先导入 stock_screener._build_coarse_sector_map（从config自动推导），
    导入失败时降级为内置简化映射表。任何情况下不抛异常。
    """
    try:
        try:
            from trading_system.strategy.stock_screener import _build_coarse_sector_map
        except ImportError:
            from strategy.stock_screener import _build_coarse_sector_map
        mapping = _build_coarse_sector_map()
        if mapping:
            # 内置表兜底补充（配置推导表不含的键用内置值）
            merged = dict(_FALLBACK_COARSE_MAP)
            merged.update(mapping)
            return merged
    except Exception:
        pass
    return dict(_FALLBACK_COARSE_MAP)


def load_sector_cache(cache_path):
    """
    读取板块轮动缓存（sector_rotation_cache.json）

    校验 updated 时间戳，距今超过24小时视为过期返回 None（降级信号）。
    文件不存在/格式错误同样返回 None。
    """
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    updated = data.get("updated", "")
    try:
        ts = datetime.datetime.fromisoformat(str(updated))
    except Exception:
        return None
    if (datetime.datetime.now() - ts).total_seconds() > CACHE_VALID_HOURS * 3600:
        return None
    return data


def detect_divergence(holdings, sector_cache, scan_cache=None):
    """
    检测持仓板块与市场热点的背离

    参数:
        holdings: list[dict]，每项含 code/name/sector（粗或细粒度）/市值/price（最新价）
        sector_cache: load_sector_cache() 返回的缓存dict，None时返回降级结构
        scan_cache: 预留参数（动态扫描缓存），当前未使用

    返回:
        dict:
          - available: 数据是否可用
          - holding_sector_scores: {code: 所属粗赛道评分}，映射不到记None
          - lagging_positions: 滞涨持仓（赛道weak或评分后1/3），含建议减持金额(市值×0.3)
            与建议减持股数（取整100股）
          - missed_hotspots: strong名单中无持仓覆盖的粗赛道
          - top_strong_sectors: 今日最强3赛道（按粗粒度评分）
          - weak_sectors / strong_sectors: 粗粒度weak/strong名单
    """
    empty = {
        "available": False,
        "holding_sector_scores": {},
        "lagging_positions": [],
        "missed_hotspots": [],
        "top_strong_sectors": [],
        "weak_sectors": [],
        "strong_sectors": [],
    }
    if not sector_cache:
        return empty

    sector_scores = sector_cache.get("sector_scores", {}) or {}
    strong = sector_cache.get("strong", []) or []
    weak = sector_cache.get("weak", []) or []
    cmap = get_coarse_sector_map()

    def _coarse(s):
        return cmap.get(s, s)

    # 细粒度评分归一到粗粒度（同粗赛道取最高分）
    coarse_scores = {}
    for fine, score in sector_scores.items():
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        c = _coarse(fine)
        if c not in coarse_scores or score > coarse_scores[c]:
            coarse_scores[c] = round(score, 1)

    weak_coarse = [_coarse(s) for s in weak]
    strong_coarse = []
    for s in strong:
        c = _coarse(s)
        if c not in strong_coarse:
            strong_coarse.append(c)

    # 评分后1/3的粗赛道集合
    ranked = sorted(coarse_scores.items(), key=lambda kv: kv[1])
    bottom_n = max(1, len(ranked) // 3) if ranked else 0
    bottom_third = set(s for s, _ in ranked[:bottom_n])

    holding_sector_scores = {}
    held_coarse = set()
    lagging_positions = []
    for h in (holdings or []):
        sector = h.get("sector", "")
        c = _coarse(sector)
        held_coarse.add(c)
        score = coarse_scores.get(c)
        code = h.get("code", "")
        holding_sector_scores[code] = score  # 映射不到记None

        if c in weak_coarse or (score is not None and c in bottom_third):
            mv = float(h.get("市值", 0) or 0)
            price = float(h.get("price", 0) or 0)
            reduce_amount = round(mv * 0.3, 2)
            reduce_shares = int(reduce_amount / price / 100) * 100 if price > 0 else 0
            reason = "赛道weak" if c in weak_coarse else "评分后1/3"
            lagging_positions.append({
                "code": code,
                "name": h.get("name", ""),
                "sector": c,
                "score": score,
                "市值": mv,
                "建议减持金额": reduce_amount,
                "建议减持股数": reduce_shares,
                "原因": reason,
            })

    missed_hotspots = [c for c in strong_coarse if c not in held_coarse]
    top_strong_sectors = [s for s, _ in sorted(coarse_scores.items(),
                                                key=lambda kv: kv[1], reverse=True)[:3]]

    return {
        "available": True,
        "holding_sector_scores": holding_sector_scores,
        "lagging_positions": lagging_positions,
        "missed_hotspots": missed_hotspots,
        "top_strong_sectors": top_strong_sectors,
        "weak_sectors": weak_coarse,
        "strong_sectors": strong_coarse,
        "coarse_scores": coarse_scores,
    }


def merge_report_candidates(static_candidates, pool_candidates=(), scan_candidates=(),
                            held_codes=None, max_total=20):
    """
    三源候选股合并去重（选股扩面）

    按"静态池优先、其次观察池、再次动态扫描"顺序合并，按code去重，
    排除已持仓代码，截断到 max_total 只。

    参数:
        static_candidates: config.SECTOR_CANDIDATES 构建的静态候选列表
        pool_candidates: stock_pool.json（core_pool+watch_pool）候选列表
        scan_candidates: scan_cache.json 动态扫描候选列表
        held_codes: 已持仓代码集合（排除）
        max_total: 合并后上限

    返回:
        合并后的候选列表，每项保留原始dict（含code/name/sector/type）
    """
    held = set(held_codes or ())
    merged = []
    seen = set()
    for source in (static_candidates, pool_candidates, scan_candidates):
        for item in (source or []):
            code = item.get("code", "") if isinstance(item, dict) else ""
            if not code or code in seen or code in held:
                continue
            seen.add(code)
            merged.append(item)
            if len(merged) >= max_total:
                return merged
    return merged
