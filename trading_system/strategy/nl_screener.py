"""
自然语言选股接口（简化版"问财"）
================================
支持用户用自然语言描述选股条件，系统自动解析为结构化查询并执行筛选。

核心功能:
  1. 规则解析 + 关键词映射实现轻量级自然语言理解（不依赖外部LLM API）
  2. 使用正则表达式提取"指标 + 操作符 + 数值"模式
  3. 支持中英文混合（"PE小于20" 和 "市盈率小于20"）
  4. 支持"且"/"和"/"同时" → AND 逻辑
  5. 支持"或" → OR 逻辑
  6. 行业识别：维护常见行业关键词列表（消费、医药、科技、半导体、新能源、金融等）
  7. 数值解析：支持百分比（"15%"→15）、亿/万单位（"100亿"→1e10）

使用方式:
    from strategy.nl_screener import NLScreener
    screener = NLScreener()
    result = screener.search("市盈率小于30且ROE大于10%")
    print(result["summary"])
    print(result["results"])
"""

import os
import sys
import re
import logging
import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False


# ============================================================
# 行业关键词映射表
# ============================================================
INDUSTRY_MAP = {
    "消费": ["消费", "食品", "饮料", "白酒", "啤酒", "乳业", "家电", "零售",
             "日用品", "纺织", "服装", "化妆品", "餐饮", "商贸"],
    "医药": ["医药", "医疗", "生物", "制药", "疫苗", "中药", "器械",
             "化学制药", "生物科技", "医疗服务", "CRO"],
    "科技": ["科技", "软件", "互联网", "云计算", "人工智能", "AI",
             "信息技术", "计算机", "通信", "传媒", "数据"],
    "半导体": ["半导体", "芯片", "集成电路", "晶圆", "封测", "IC设计",
               "光刻", "EDA"],
    "新能源": ["新能源", "光伏", "锂电", "储能", "风电", "太阳能",
               "电池", "充电桩", "氢能", "新能源车"],
    "金融": ["金融", "银行", "保险", "券商", "证券", "信托", "期货"],
    "制造": ["制造", "机械", "工业", "自动化", "机器人", "设备",
             "仪器", "仪表", "电气设备"],
    "军工": ["军工", "航天", "国防", "航空", "船舶", "兵器"],
    "地产": ["地产", "房地产", "物业", "建筑", "建材", "装修"],
    "周期": ["周期", "钢铁", "有色", "煤炭", "化工", "石化",
             "水泥", "铝", "铜"],
    "交通": ["交通", "运输", "物流", "港口", "航空", "铁路", "高速"],
    "农业": ["农业", "养殖", "种植", "畜牧", "饲料", "种子", "农药"],
}


class NLScreener:
    """自然语言选股引擎"""

    # 支持的筛选维度及关键词映射
    DIMENSION_KEYWORDS = {
        "pe": ["市盈率", "PE", "pe", "Pe"],
        "pb": ["市净率", "PB", "pb", "Pb"],
        "roe": ["ROE", "净资产收益率", "收益率"],
        "revenue_growth": ["营收增速", "收入增长", "营收增长"],
        "profit_growth": ["利润增速", "净利润增长", "盈利增长", "净利润增速"],
        "market_cap": ["市值", "总市值", "流通市值"],
        "price_change": ["涨幅", "涨跌幅", "涨"],
        "volume_ratio": ["量比", "成交量"],
        "turnover": ["换手率", "换手"],
        "price_range": ["价格", "股价"],
    }

    # 操作符映射（支持更多自然语言表达）
    OPERATOR_MAP = {
        # 大于
        "大于": ">", "高于": ">", "超过": ">", "多于": ">",
        # 大于等于
        "不低于": ">=", "不少于": ">=", "不小于": ">=",
        "至少": ">=", "以上": ">=",
        # 小于
        "小于": "<", "低于": "<", "不到": "<", "少于": "<",
        # 小于等于
        "不超过": "<=", "不大于": "<=", "至多": "<=",
        "不要超过": "<=", "以下": "<=",
        # 等于
        "等于": "==", "为": "==",
    }

    def __init__(self):
        """初始化选股引擎"""
        self._fa = None
        self._industry_df = None

    # ============================================================
    # 一、查询解析
    # ============================================================

    def parse_query(self, query: str) -> dict:
        """
        解析自然语言查询为结构化条件

        参数:
            query: 自然语言查询字符串

        返回:
            {
                "conditions": [
                    {"field": "pe", "operator": "<", "value": 20},
                    {"field": "roe", "operator": ">", "value": 15},
                ],
                "industry": "消费",
                "logic": "and"
            }
        """
        result = {
            "conditions": [],
            "industry": None,
            "logic": "and",
        }

        if not query or not isinstance(query, str):
            return result

        query = query.strip()

        # 检测逻辑关系
        # "或"/"或者" → OR
        # "且"/"和"/"同时"/"并且"/"而且"/"以及" → AND（显式）
        if re.search(r"或|或者", query):
            result["logic"] = "or"
        # 默认 AND，无需显式检测

        # 识别行业
        result["industry"] = self._extract_industry(query)

        # 提取数值条件
        result["conditions"] = self._extract_conditions(query)

        return result

    def _extract_industry(self, query: str) -> str:
        """从查询中提取行业关键词"""
        for industry, keywords in INDUSTRY_MAP.items():
            for keyword in keywords:
                if keyword in query:
                    return industry
        return None

    def _extract_conditions(self, query: str) -> list:
        """
        从查询中提取数值条件（纯正则驱动，支持无分隔符的多条件）

        正则模式: (维度关键词) + (操作符) + (数值[单位])
        所有匹配均通过正则完成，不依赖硬编码字符串查找。

        示例:
          "换手率大于5%涨幅大于3%" → 两个条件
          "PE低于20" → 一个条件
          "ROE在15%以上" → 一个条件（"以上"映射为 >=）
          "市值大于100亿" → 一个条件
          "市净率不超过3" → 一个条件
        """
        conditions = []

        # --- 步骤1: 构建维度正则 ---
        # 将所有维度关键词拼成一个捕获组，按长度降序确保优先匹配长关键词
        kw_to_field = []
        for field, keywords in self.DIMENSION_KEYWORDS.items():
            for kw in keywords:
                kw_to_field.append((kw, field))
        kw_to_field.sort(key=lambda x: len(x[0]), reverse=True)
        # 构建正则交替组: 净利润增速|营收增速|换手率|市盈率|PE|...
        dim_alts = "|".join(re.escape(kw) for kw, _ in kw_to_field)
        # 关键词 → field 的快速查找
        kw_lookup = {kw: field for kw, field in kw_to_field}

        # --- 步骤2: 构建操作符正则 ---
        op_keywords = sorted(self.OPERATOR_MAP.keys(), key=len, reverse=True)
        op_alts = "|".join(re.escape(op) for op in op_keywords)

        # --- 步骤3: 数值正则（支持整数/小数 + 可选单位 %/亿/万）---
        number_re = r"(\d+(?:\.\d+)?(?:%|亿|万)?)"

        # --- 步骤4: 组合完整正则 ---
        # 主模式: (维度关键词) + 可选空白 + (操作符) + 可选空白 + (数值)
        # 用 re.IGNORECASE 支持 pe/PE/Pe 等大小写混合
        main_pattern = re.compile(
            r"(" + dim_alts + r")" +   # group(1): 维度关键词
            r"\s*" +
            r"(" + op_alts + r")" +    # group(2): 操作符
            r"\s*" +
            number_re,                  # group(3): 数值
            re.IGNORECASE
        )

        # 后置操作符模式: "在{数值}{以上/以下}" → 操作符在数值之后
        # 例: "ROE在15%以上" → roe >= 15
        post_op_alts = "|".join(re.escape(op) for op in ("以上", "以下"))
        post_pattern = re.compile(
            r"(" + dim_alts + r")" +          # group(1): 维度关键词
            r"\s*在\s*" +
            number_re +                        # group(2): 数值
            r"\s*(" + post_op_alts + r")",     # group(3): 后置操作符
            re.IGNORECASE
        )

        matched_spans = []  # 已匹配的文本区间，避免重叠

        # 先匹配后置操作符模式（优先级高，更具体）
        for m in post_pattern.finditer(query):
            span = (m.start(), m.end())
            if any(not (span[1] <= s[0] or span[0] >= s[1]) for s in matched_spans):
                continue

            raw_kw = m.group(1)
            value_str = m.group(2)
            post_op = m.group(3)  # "以上" or "以下"

            field = self._resolve_field(raw_kw, kw_lookup, kw_to_field)
            if field is None:
                continue

            operator = ">=" if post_op == "以上" else "<="
            value = self._parse_value(value_str, field)

            conditions.append({
                "field": field,
                "operator": operator,
                "value": value,
            })
            matched_spans.append(span)

        # 再匹配主模式
        for m in main_pattern.finditer(query):
            span = (m.start(), m.end())
            # 跳过与已匹配区间重叠的结果
            if any(not (span[1] <= s[0] or span[0] >= s[1]) for s in matched_spans):
                continue

            raw_kw = m.group(1)      # 匹配到的维度关键词原文
            raw_op = m.group(2)      # 匹配到的操作符原文
            value_str = m.group(3)   # 数值字符串

            field = self._resolve_field(raw_kw, kw_lookup, kw_to_field)
            if field is None:
                continue

            # 操作符查找
            operator = self.OPERATOR_MAP.get(raw_op)
            if operator is None:
                continue

            value = self._parse_value(value_str, field)

            conditions.append({
                "field": field,
                "operator": operator,
                "value": value,
            })
            matched_spans.append(span)

        return conditions

    @staticmethod
    def _resolve_field(raw_kw: str, kw_lookup: dict, kw_to_field: list):
        """解析维度关键词为 field 标识（大小写不敏感）"""
        field = kw_lookup.get(raw_kw)
        if field:
            return field
        field = kw_lookup.get(raw_kw.upper())
        if field:
            return field
        field = kw_lookup.get(raw_kw.lower())
        if field:
            return field
        # 最终兜底：大小写不敏感遍历
        for kw, f in kw_to_field:
            if kw.lower() == raw_kw.lower():
                return f
        return None

    def _parse_value(self, value_str: str, field: str) -> float:
        """
        解析数值字符串

        - "15%" → 15（百分比字段保持数值原值，后续比较时用同单位）
        - "100亿" → 100（市值字段用亿为单位）
        - "5000万" → 0.5（市值字段用亿为单位）
        - "30" → 30
        """
        if not value_str:
            return 0.0

        value_str = value_str.strip()

        # 百分比
        if value_str.endswith("%"):
            return float(value_str[:-1])

        # 亿
        if value_str.endswith("亿"):
            return float(value_str[:-1])

        # 万 → 转为亿
        if value_str.endswith("万"):
            return float(value_str[:-1]) / 10000.0

        try:
            return float(value_str)
        except ValueError:
            return 0.0

    # ============================================================
    # 二、数据获取
    # ============================================================

    def _get_fundamental_analyzer(self):
        """延迟初始化 FundamentalAnalyzer"""
        if self._fa is None:
            try:
                from strategy.fundamental import FundamentalAnalyzer
                self._fa = FundamentalAnalyzer()
            except Exception as e:
                logger.warning(f"[NL选股] FundamentalAnalyzer初始化失败: {e}")
        return self._fa

    def _get_stock_codes(self) -> list:
        """获取全部候选股票代码"""
        try:
            import config
            codes = set(config.STOCK_POOL.keys())
            sector_candidates = getattr(config, "SECTOR_CANDIDATES", {})
            for sector_info in sector_candidates.values():
                codes.update(sector_info.get("stocks", {}).keys())
            # 过滤创业板(300)和科创板(688)
            codes = {c for c in codes
                     if not c.startswith("300") and not c.startswith("688")
                     and not c.startswith("588") and not c.startswith("159")
                     and c != "000300"}
            return sorted(codes)
        except Exception as e:
            logger.error(f"[NL选股] 获取股票列表失败: {e}")
            return []

    def _load_industry_data(self) -> pd.DataFrame:
        """加载行业分类数据（akshare），带缓存"""
        if self._industry_df is not None:
            return self._industry_df

        if not HAS_AKSHARE:
            logger.warning("[NL选股] akshare未安装，无法获取行业分类")
            return pd.DataFrame()

        try:
            # 东方财富行业分类
            df = ak.stock_board_industry_name_em()
            if df is not None and not df.empty:
                self._industry_df = df
                logger.info(f"[NL选股] 行业分类加载成功: {len(df)}个行业")
            return self._industry_df or pd.DataFrame()
        except Exception as e:
            logger.warning(f"[NL选股] 行业分类获取失败: {e}")
            return pd.DataFrame()

    def _get_stock_industry_akshare(self, code: str) -> str:
        """从akshare获取个股所属行业"""
        try:
            df = ak.stock_individual_info_em(symbol=code)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    if "行业" in str(row.iloc[0]):
                        return str(row.iloc[1])
        except Exception:
            pass
        return ""

    def _build_stock_data(self, code: str) -> dict:
        """
        构建单只股票的筛选数据

        数据来源:
          - 基本面(PE/PB/ROE): FundamentalAnalyzer
          - 行情(涨跌幅/换手率): akshare 实时行情
          - 行业: config 或 akshare
        """
        data = {"code": code}

        # 股票名称和行业（优先从 config 获取）
        try:
            import config
            stock_info = config.get_stock_info(code)
            data["name"] = stock_info.get("名称", code)
            data["industry"] = stock_info.get("赛道", "")
        except Exception:
            data["name"] = code
            data["industry"] = ""

        # 基本面数据
        fa = self._get_fundamental_analyzer()
        if fa:
            try:
                fin = fa.get_financial_indicators(code)
                data["pe"] = fin.get("pe_ttm")
                data["pb"] = fin.get("pb")
                data["roe"] = fin.get("roe")
                data["revenue_growth"] = fin.get("revenue_growth")
                data["profit_growth"] = fin.get("net_profit_growth")
            except Exception as e:
                logger.debug(f"[NL选股] {code} 基本面获取失败: {e}")

        # 行情数据（涨跌幅、换手率等）
        try:
            if HAS_AKSHARE:
                df = ak.stock_zh_a_spot_em()
                if df is not None and not df.empty:
                    row = df[df["代码"] == code]
                    if not row.empty:
                        row = row.iloc[0]
                        data["price_change"] = self._safe_float(
                            row.get("涨跌幅"))
                        data["turnover"] = self._safe_float(
                            row.get("换手率"))
                        data["volume_ratio"] = self._safe_float(
                            row.get("量比"))
                        data["market_cap"] = self._safe_float(
                            row.get("总市值"))
                        # 补充名称（config 可能没有）
                        if data["name"] == code:
                            data["name"] = row.get("名称", code)
        except Exception as e:
            logger.debug(f"[NL选股] {code} 行情获取失败: {e}")

        # 市值转亿为单位
        if data.get("market_cap") and data["market_cap"] > 0:
            data["market_cap_yi"] = round(data["market_cap"] / 1e8, 2)
        else:
            data["market_cap_yi"] = None

        return data

    @staticmethod
    def _safe_float(val) -> float:
        """安全转float"""
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    # ============================================================
    # 三、查询执行
    # ============================================================

    def execute_query(self, parsed_query: dict) -> pd.DataFrame:
        """
        执行结构化查询，返回符合条件的股票DataFrame

        参数:
            parsed_query: parse_query() 返回的结构化条件

        返回:
            DataFrame（含代码、名称、各筛选指标值）
        """
        conditions = parsed_query.get("conditions", [])
        industry = parsed_query.get("industry")
        logic = parsed_query.get("logic", "and")

        if not conditions and not industry:
            logger.warning("[NL选股] 无有效查询条件")
            return pd.DataFrame()

        stock_codes = self._get_stock_codes()
        if not stock_codes:
            logger.error("[NL选股] 无法获取股票列表")
            return pd.DataFrame()

        logger.info(f"[NL选股] 开始筛选 {len(stock_codes)} 只股票 "
                    f"(条件={len(conditions)}, 行业={industry}, 逻辑={logic})")

        # 如果有行情条件，一次性获取全市场行情（避免逐只请求）
        market_df = None
        need_market = any(c["field"] in ("price_change", "turnover",
                                         "volume_ratio", "market_cap")
                         for c in conditions)
        if need_market and HAS_AKSHARE:
            try:
                market_df = ak.stock_zh_a_spot_em()
                if market_df is not None:
                    market_df = market_df.set_index("代码")
                    logger.info(f"[NL选股] 全市场行情加载成功: {len(market_df)}只")
            except Exception as e:
                logger.warning(f"[NL选股] 全市场行情获取失败: {e}")

        results = []
        skipped = 0
        for code in stock_codes:
            try:
                row_data = self._evaluate_stock(
                    code, conditions, industry, logic, market_df)
                if row_data:
                    results.append(row_data)
            except Exception as e:
                skipped += 1
                logger.debug(f"[NL选股] {code} 处理异常: {e}")

        if skipped > 0:
            logger.info(f"[NL选股] 跳过 {skipped} 只异常股票")

        if not results:
            return pd.DataFrame()

        df = pd.DataFrame(results)
        logger.info(f"[NL选股] 筛选完成: {len(df)} 只股票符合条件")
        return df

    def _evaluate_stock(self, code: str, conditions: list,
                        industry: str, logic: str,
                        market_df: pd.DataFrame) -> dict:
        """
        评估单只股票是否符合条件

        返回: 符合条件的行数据 dict，或 None
        """
        # ---- 行情数据（从批量 DataFrame 取，避免逐只请求）----
        pe_val = pb_val = roe_val = None
        rev_growth = profit_g = None
        price_chg = turnover_val = vol_ratio = mcap = mcap_yi = None
        name = code
        stock_industry = ""

        # 从 config 获取名称/行业
        try:
            import config
            info = config.get_stock_info(code)
            name = info.get("名称", code)
            stock_industry = info.get("赛道", "")
        except Exception:
            pass

        # 行情数据
        if market_df is not None and code in market_df.index:
            row = market_df.loc[code]
            price_chg = self._safe_float(row.get("涨跌幅"))
            turnover_val = self._safe_float(row.get("换手率"))
            vol_ratio = self._safe_float(row.get("量比"))
            mcap = self._safe_float(row.get("总市值"))
            if mcap and mcap > 0:
                mcap_yi = round(mcap / 1e8, 2)
            if name == code:
                name = row.get("名称", code)

        # 基本面数据（仅在有相关条件时才获取，减少API调用）
        need_fundamental = any(c["field"] in ("pe", "pb", "roe",
                                               "revenue_growth", "profit_growth")
                               for c in conditions)
        if need_fundamental:
            fa = self._get_fundamental_analyzer()
            if fa:
                try:
                    fin = fa.get_financial_indicators(code)
                    pe_val = fin.get("pe_ttm")
                    pb_val = fin.get("pb")
                    roe_val = fin.get("roe")
                    rev_growth = fin.get("revenue_growth")
                    profit_g = fin.get("net_profit_growth")
                except Exception:
                    pass

        # 构建字段值映射
        field_values = {
            "pe": pe_val,
            "pb": pb_val,
            "roe": roe_val,
            "revenue_growth": rev_growth,
            "profit_growth": profit_g,
            "market_cap": mcap_yi,
            "price_change": price_chg,
            "volume_ratio": vol_ratio,
            "turnover": turnover_val,
            "price_range": self._safe_float(
                market_df.loc[code].get("最新价"))
                if market_df is not None and code in market_df.index else None,
        }

        # ---- 行业过滤 ----
        if industry:
            if not self._industry_match(stock_industry, industry):
                return None

        # ---- 数值条件过滤 ----
        if conditions:
            match_results = []
            for cond in conditions:
                field = cond["field"]
                operator = cond["operator"]
                target = cond["value"]
                actual = field_values.get(field)

                if actual is None:
                    match_results.append(False)
                    continue

                match_results.append(
                    self._compare(actual, operator, target))

            if logic == "and":
                if not all(match_results):
                    return None
            else:
                if not any(match_results):
                    return None

        # 符合条件，返回行数据
        return {
            "代码": code,
            "名称": name,
            "行业": stock_industry,
            "PE": self._round2(pe_val),
            "PB": self._round2(pb_val),
            "ROE(%)": self._round2(roe_val),
            "营收增速(%)": self._round2(rev_growth),
            "利润增速(%)": self._round2(profit_g),
            "市值(亿)": self._round2(mcap_yi),
            "涨跌幅(%)": self._round2(price_chg),
            "换手率(%)": self._round2(turnover_val),
            "量比": self._round2(vol_ratio),
        }

    @staticmethod
    def _compare(actual: float, operator: str, target: float) -> bool:
        """数值比较"""
        try:
            actual = float(actual)
            target = float(target)
        except (ValueError, TypeError):
            return False

        if operator == ">":
            return actual > target
        elif operator == ">=":
            return actual >= target
        elif operator == "<":
            return actual < target
        elif operator == "<=":
            return actual <= target
        elif operator == "==":
            return abs(actual - target) < 0.01
        return False

    @staticmethod
    def _industry_match(stock_industry: str, target_industry: str) -> bool:
        """判断股票行业是否匹配目标行业（模糊匹配）"""
        if not stock_industry:
            return False
        # 精确匹配
        if target_industry in stock_industry or stock_industry in target_industry:
            return True
        # 关键词匹配
        keywords = INDUSTRY_MAP.get(target_industry, [])
        for kw in keywords:
            if kw in stock_industry:
                return True
        return False

    @staticmethod
    def _round2(val):
        """安全保留2位小数"""
        if val is None:
            return None
        try:
            return round(float(val), 2)
        except (ValueError, TypeError):
            return None

    # ============================================================
    # 四、一站式接口
    # ============================================================

    def search(self, query: str) -> dict:
        """
        一站式接口：解析 + 执行 + 格式化结果

        参数:
            query: 自然语言查询

        返回:
            {
                "query": 原始查询,
                "parsed": 解析后的条件,
                "results": DataFrame,
                "count": 结果数量,
                "summary": "找到N只符合条件的股票: ..."
            }
        """
        logger.info(f"[NL选股] 查询: {query}")

        # 解析查询
        parsed = self.parse_query(query)

        if not parsed["conditions"] and not parsed["industry"]:
            return {
                "query": query,
                "parsed": parsed,
                "results": pd.DataFrame(),
                "count": 0,
                "summary": (
                    "无法解析查询条件。示例用法:\n"
                    "  - 市盈率小于30且ROE大于10%\n"
                    "  - 市值大于100亿的科技股\n"
                    "  - 换手率大于5%涨幅大于3%\n"
                    "支持的指标: 市盈率/PE、市净率/PB、ROE、营收增速、"
                    "利润增速、市值、涨幅、换手率、量比、价格"
                ),
            }

        # 执行查询
        results = self.execute_query(parsed)

        # 生成摘要
        count = len(results)
        if count > 0:
            cond_parts = []
            for c in parsed["conditions"]:
                field_cn = {
                    "pe": "PE", "pb": "PB", "roe": "ROE",
                    "revenue_growth": "营收增速", "profit_growth": "利润增速",
                    "market_cap": "市值(亿)", "price_change": "涨跌幅",
                    "volume_ratio": "量比", "turnover": "换手率",
                    "price_range": "股价",
                }.get(c["field"], c["field"])
                op_cn = {">": ">", ">=": "≥", "<": "<",
                         "<=": "≤", "==": "="}.get(c["operator"], c["operator"])
                cond_parts.append(f"{field_cn}{op_cn}{c['value']}")
            cond_str = (" 且 " if parsed["logic"] == "and" else " 或 ").join(cond_parts)
            summary = f"找到 {count} 只符合条件的股票"
            if cond_str:
                summary += f"（{cond_str}）"
            if parsed["industry"]:
                summary += f"，行业：{parsed['industry']}"
        else:
            summary = (
                "未找到符合条件的股票，建议放宽筛选条件。\n"
                "例如：将'市盈率小于20'放宽为'市盈率小于50'"
            )

        return {
            "query": query,
            "parsed": parsed,
            "results": results,
            "count": count,
            "summary": summary,
        }


# ============================================================
# 独立运行测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 60)
    print("  自然语言选股引擎（简化版'问财'）- 测试")
    print("=" * 60)

    screener = NLScreener()

    # 测试1: 解析验证
    test_queries = [
        "市盈率小于30且ROE大于10%",
        "市值大于100亿的科技股",
        "换手率大于5%涨幅大于3%",
        "市净率低于3同时利润增速超过20%的医药股",
        "PE大于50或PB大于5",
        "PE低于20",
        "ROE在15%以上",
        "市盈率不超过30并且市值至少100亿",
        "市值不到50亿的消费股",
        "换手率不少于5%且量比大于1.5",
    ]

    for q in test_queries:
        parsed = screener.parse_query(q)
        print(f"\n查询: {q}")
        print(f"  条件: {parsed['conditions']}")
        print(f"  行业: {parsed['industry']}")
        print(f"  逻辑: {parsed['logic']}")

    # 测试2: 实际执行（需网络）
    print("\n" + "=" * 60)
    print("  实际查询测试（需要网络连接）")
    print("=" * 60)

    result = screener.search("市盈率小于30且ROE大于10%")
    print(f"\n摘要: {result['summary']}")
    if result["count"] > 0:
        print(result["results"].to_string(index=False))

    print("\n[OK] 自然语言选股引擎测试完成")
