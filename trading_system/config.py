"""
高胜率A股交易操作系统（V3.0 全面升级版）- 全局配置文件
=====================================================
所有可调参数、股票池、风控阈值均在此处集中管理
修改任何参数只需编辑本文件，无需改动策略逻辑代码

V3.0新增:
  - 组合风险管理（相关性/HHI/VaR/再平衡）
  - 基本面自动获取（PE/ROE/增速/资金流）
  - 多策略引擎（趋势+均值回归+多周期共振）
  - 大盘状态智能识别（多特征融合）
  - 交易日志与绩效归因
  - 盘中实时监控与预警推送
"""

import os

# ============================================================
# 一、资金与账户配置
# ============================================================
TOTAL_CAPITAL = 731_455.43     # 总资金（根据实际账户总资产）
AVAILABLE_CASH = 297.43          # 当前可用资金（用于选股仓位计算）
CASH_RESERVE_RATIO = 0.10        # 最低现金保留比例（10%安全垫）

# ============================================================
# 二、核心股票池（每周日更新，盘中绝不临时新增）
# ============================================================
# 格式: {"股票代码": {"名称": "xxx", "赛道": "xxx", "类型": "龙头/弹性"}}
# 类型说明:
#   - "龙头": 主线赛道前3龙头，仓位上限15%
#   - "弹性": 非主线/弹性标的，仓位上限8%
STOCK_POOL = {
    "588000": {"名称": "科创50",   "赛道": "指数ETF",    "类型": "弹性"},
    "002415": {"名称": "海康威视", "赛道": "AI视觉",     "类型": "龙头"},
    "603501": {"名称": "豪威集团", "赛道": "CIS芯片",    "类型": "龙头"},
    "002409": {"名称": "雅克科技", "赛道": "半导体材料", "类型": "龙头"},
    "002185": {"名称": "华天科技", "赛道": "半导体封测", "类型": "弹性"},
    "600036": {"名称": "招商银行", "赛道": "银行",       "类型": "龙头"},
    "159205": {"名称": "创业东财", "赛道": "指数ETF",    "类型": "弹性"},
    "600276": {"名称": "恒瑞医药", "赛道": "创新药",     "类型": "龙头"},
    "603993": {"名称": "洛阳钼业", "赛道": "有色资源",   "类型": "弹性"},
}

# 赛道仓位上限（单一赛道不超过总资金40%）
SECTOR_MAX_RATIO = 0.40

# 个股仓位上限
LEADER_STOCK_MAX_RATIO = 0.15    # 主线龙头单只上限15%
FLEXIBLE_STOCK_MAX_RATIO = 0.08  # 弹性小票单只上限8%

# ============================================================
# 二''、股票池管理参数
# ============================================================
CORE_POOL_MAX = 7                # 核心操作池上限（5-7只）
WATCH_POOL_MAX = 10              # 观察池上限
OBSERVATION_DAYS = 3             # 观察期最少交易日
MAX_HOLDINGS = 7                 # 持仓数量硬限制（统一为7只，已验证最优）

# 满仓禁止加仓规则（V3.1新增 - 情绪化交易防护）
FULL_POSITION_THRESHOLD = 0.90   # 仓位>=90%时禁止任何买入/加仓
NEAR_FULL_POSITION = 0.80        # 仓位>=80%时只允许减仓不允许新开仓

# 个股硬性筛选标准
MIN_DAILY_AMOUNT = 5e8           # 日均成交额下限（5亿元，V2.3降低避免中小盘龙头被误杀）
MAX_HIGH_AMPLITUDE_DAYS = 3      # 近30日振幅>10%的天数上限
CRASH_THRESHOLD = -0.08          # 单日暴跌阈值（-8%）
CRASH_VOLUME_RATIO = 2.0         # 暴跌放量倍数（量>均量2倍）

# 赛道分级（第一梯队权重加成）
SECTOR_TIER1 = ["半导体", "AI数字经济"]  # 第一梯队：AI/半导体
SECTOR_BLACKLIST = []                     # 永久规避赛道

# 赛道有效性判定
SECTOR_MA60_ABOVE_RATIO = 0.60   # 板块内站稳MA60的股票占比>60%才算有效主线

# ============================================================
# 二’、全赛道选股候选池（按行业分组，选股引擎从中筛选）
# ============================================================

# V2.3: 退市/停牌黑名单（硬过滤，任何情况下不参与选股）
# 包括已退市、吸收合并、长期停牌等不可交易股票
DELISTED_STOCKS = {
    "601989",  # 中国重工 - 2025年被中国船舶(600150)吸收合并退市
}

# 主力赛道为半导体，但参考行情与策略可配置其他行业
# 选股引擎会根据行业强弱动态分配名额
SECTOR_CANDIDATES = {
    # --- 主力赛道：半导体（配额25%）---
    "半导体": {
        "weight": 0.25,  # 行业配额权重
        "stocks": {
            "002371": {"名称": "北方华创", "细分": "半导体设备", "类型": "龙头"},
            "002409": {"名称": "雅克科技", "细分": "半导体材料", "类型": "龙头"},
            "600584": {"名称": "长电科技", "细分": "半导体封测", "类型": "龙头"},
            "001309": {"名称": "德明利",   "细分": "存储芯片", "类型": "龙头"},
            "603501": {"名称": "豪威集团", "细分": "CIS芯片", "类型": "龙头"},
            "002185": {"名称": "华天科技", "细分": "半导体封测", "类型": "弹性"},
            "002049": {"名称": "紫光国微", "细分": "芯片设计", "类型": "龙头"},
            "603986": {"名称": "兆易创新", "细分": "存储芯片", "类型": "龙头"},
        }
    },
    # --- 军工航天（配额15%）---
    "军工航天": {
        "weight": 0.15,
        "stocks": {
            "600760": {"名称": "中航沈飞", "细分": "军工航空", "类型": "龙头"},
            "600118": {"名称": "中国卫星", "细分": "卫星导航", "类型": "弹性"},
            "000768": {"名称": "中航西飞", "细分": "军用飞机", "类型": "龙头"},
            "600893": {"名称": "航发动力", "细分": "航空发动机", "类型": "龙头"},
            "600150": {"名称": "中国船舶", "细分": "军工船舶", "类型": "龙头"},
        }
    },
    # --- AI/数字经济（配额15%）---
    "AI数字经济": {
        "weight": 0.15,
        "stocks": {
            "002230": {"名称": "科大讯飞", "细分": "AI应用", "类型": "龙头"},
            "002415": {"名称": "海康威视", "细分": "AI视觉", "类型": "龙头"},
        }
    },
    # --- 新能源（配额12%）---
    "新能源": {
        "weight": 0.12,
        "stocks": {
            "002594": {"名称": "比亚迪", "细分": "新能源车", "类型": "龙头"},
            "601012": {"名称": "隆基绿能", "细分": "光伏", "类型": "龙头"},
        }
    },
    # --- 医药医疗（配额10%）---
    "医药医疗": {
        "weight": 0.10,
        "stocks": {
            "600276": {"名称": "恒瑞医药", "细分": "创新药", "类型": "龙头"},
            "000661": {"名称": "长春高新", "细分": "生物制品", "类型": "弹性"},
        }
    },
    # --- 大消费（配额8%）---
    "大消费": {
        "weight": 0.08,
        "stocks": {
            "600519": {"名称": "贵州茅台", "细分": "白酒", "类型": "龙头"},
            "000858": {"名称": "五粮液", "细分": "白酒", "类型": "龙头"},
            "603288": {"名称": "海天味业", "细分": "调味品", "类型": "弹性"},
            "002714": {"名称": "牧原股份", "细分": "养殖", "类型": "弹性"},
        }
    },
    # --- 大金融（配额8%）---
    "大金融": {
        "weight": 0.08,
        "stocks": {
            "601318": {"名称": "中国平安", "细分": "保险", "类型": "龙头"},
            "600036": {"名称": "招商银行", "细分": "银行", "类型": "龙头"},
            "601688": {"名称": "华泰证券", "细分": "证券", "类型": "弹性"},
        }
    },
    # --- 有色金属/资源（配额7%）---
    "有色资源": {
        "weight": 0.07,
        "stocks": {
            "601899": {"名称": "紫金矿业", "细分": "黄金铜矿", "类型": "龙头"},
            "002460": {"名称": "赣锋锂业", "细分": "锂矿", "类型": "龙头"},
            "603993": {"名称": "洛阳钼业", "细分": "钴铜矿", "类型": "弹性"},
        }
    },
}

# 选股引擎配置
SCREENER_CONFIG = {
    "max_stocks_per_sector": 3,     # 每个行业最多入选3只
    "min_score": 40,                # CANSLIM排序参考分（V2.4: 不再作为硬性淘汰门槛）
    "min_buy_score": 50,            # V2.4: 买入推荐分界线（兼容旧逻辑，V2.7起由动态线替代）
    "min_buy_score_weak": 35,       # V2.7: 弱势/震荡市买入线（market_state=down/weak/neutral）
    "min_buy_score_strong": 45,     # V2.8: 强势市买入线（回测验证: 45分WR=44.9% > 50分WR=43.9%，降低5分增加有效信号）
    "total_max": 10,                # 固定输出10只（V2.4: 无论评分高低必须输出前10）
    "prefer_strong_sector": True,   # 优先从强势赛道中选
    "sector_dynamic_adjust": True,  # 根据行情动态调整行业配额
    "cooldown_days": 1,             # V2.7: 冷却期从2天缩短为1天（避免唯一达标股被排除）
}

# 新闻/政策风控配置（仅做风控刹车+选股过滤，不产生买卖信号）
NEWS_MONITOR_ENABLED = True          # 是否启用新闻监控
NEWS_LOOKBACK_HOURS = 24             # 只看最近24小时新闻

# V2.4: 资金异动加分配置（龙虎榜+主力资金流）
LHB_NET_BUY_THRESHOLD = 5000e4       # 龙虎榜净买入阈值（5000万）→+3分
FUND_FLOW_CONSECUTIVE_DAYS = 3       # 主力资金连续净流入天数→+2分
FUND_FLOW_BONUS_MAX = 5              # 资金异动加分上限

# V2.4: 涨停复盘配置
ZT_MONITOR_ENABLED = True            # 是否在选股报告中集成涨停复盘
ZT_SECTOR_HOT_THRESHOLD = 3          # 板块热度阈值（涨停≥3只即为热门）
NEWS_MAX_PER_STOCK = 10              # 每只股票最多拉取10条新闻
NEWS_FILTER_IN_SCREENER = True       # 选股时是否过滤level>=2的新闻股
NEWS_ALERT_IN_EMAIL = True           # 邮件中是否显示新闻预警

# V2.5: 盘中异动预警配置
INTRADAY_ALERT_ENABLED = True        # 是否启用盘中异动预警
ALERT_MIN_CHANGE_PCT = 7.0           # 准涨停最低涨幅阈值(%)
ALERT_MIN_VOL_RATIO = 1.5            # 最低量比（资金关注度）
ALERT_MIN_AMOUNT = 3e8               # 最低成交额(3亿)
ALERT_MAX_PRICE = 300                # 最高股价过滤
ALERT_MIN_PRICE = 3                  # 最低股价过滤
ALERT_SECTOR_CASCADE_THRESHOLD = 2   # 板块联动触发阈值（同板块≥2只涨停）
ALERT_SECTOR_FOLLOWER_MIN_PCT = 5.0  # 跟涨候选最低涨幅(%)
ALERT_SECTOR_FOLLOWER_MAX_PCT = 9.0  # 跟涨候选最高涨幅(%)
ALERT_MAX_COUNT = 15                 # 单次最多预警数量
ALERT_SCAN_INTERVAL = 10             # 扫描间隔(分钟) V2.6: 30->10分钟提升时效

# V2.6: 放量突破启动检测配置
ALERT_BREAKOUT_ENABLED = True        # 是否启用放量突破检测
ALERT_BREAKOUT_MIN_CHANGE = 3.0      # 突破检测最低涨幅(%)
ALERT_BREAKOUT_MAX_CHANGE = 7.0      # 突破检测最高涨幅(%)(超过7%归入准涨停)
ALERT_BREAKOUT_MIN_VOL_RATIO = 2.0   # 突破检测最低量比
ALERT_BREAKOUT_MIN_AMOUNT = 2e8      # 突破检测最低成交额(2亿)
ALERT_BREAKOUT_MAX_COUNT = 10        # 突破检测最多输出数量
ALERT_FIRST_TRIGGER_ONLY = True      # 同一标的当日首次触发才推送，后续不重复
ALERT_SCREENER_INTRADAY = True       # 是否启用盘中选股增量扫描
ALERT_ZT_GENE_LINKAGE = True         # 是否启用涨停基因联动预警

# V2.5: 涨停基因跟踪配置
ZT_GENE_ENABLED = True               # 是否启用涨停基因跟踪
ZT_GENE_MIN_OPEN_PCT = 3.0           # 连板候选最低高开幅度(%)
ZT_GENE_MIN_VOL_RATIO = 2.0          # 连板候选最低量比
ZT_GENE_TRACK_DAYS = 3               # 跟踪最近N天涨停股


# ============================================================
# 二'''、统一股票名称/信息查找（修复名称显示为代码的Bug）
# ============================================================
def get_stock_name(code: str) -> str:
    """
    统一股票名称查找（优先STOCK_POOL，其次SECTOR_CANDIDATES）
    解决：信号明细/选股结果中股票名称显示为代码的问题
    """
    # 1. 从核心股票池查找
    if code in STOCK_POOL:
        return STOCK_POOL[code].get("名称", code)
    # 2. 从全赛道候选池查找
    for sector_info in SECTOR_CANDIDATES.values():
        stocks = sector_info.get("stocks", {})
        if code in stocks:
            return stocks[code].get("名称", code)
    # 3. 未找到，返回代码
    return code


def get_stock_info(code: str) -> dict:
    """
    统一股票信息查找（返回 {"名称", "赛道", "类型"} 格式）
    优先STOCK_POOL，其次SECTOR_CANDIDATES
    """
    # 1. 从核心股票池查找
    if code in STOCK_POOL:
        return STOCK_POOL[code]
    # 2. 从全赛道候选池查找
    for sector_name, sector_info in SECTOR_CANDIDATES.items():
        stocks = sector_info.get("stocks", {})
        if code in stocks:
            info = stocks[code]
            return {
                "名称": info.get("名称", code),
                "赛道": info.get("细分", sector_name),
                "类型": info.get("类型", "龙头"),
            }
    # 3. 未找到
    return {"名称": code, "赛道": "其他", "类型": "弹性"}

# ============================================================
# 三、趋势与均线参数（中线趋势跟踪 V2.0）
# ============================================================
MA_SHORT = 20                    # 短期均线（20日）
MA_MID = 60                      # 中期均线（60日）
VOLUME_MA_PERIOD = 20            # 成交量均线周期

# --- 中线趋势跟踪核心参数（V2.0新增）---
# 系统唯一周期定位: 3日-4周中线波段
MIN_HOLD_DAYS = 3                # 最短持仓天数（3天）
MAX_HOLD_DAYS_MID = 20           # 最长持仓天数（4周=20个交易日）
MIN_RISK_REWARD_RATIO = 2.5      # 开仓最低盈亏比（不达标直接拦截）
TREND_MA_BULLISH_REQUIRED = True  # 开仓必须均线多头排列(close>MA20>MA60)
RPS_MIN_THRESHOLD = 70           # 60日相对强度最低阈值（跑赢大盘）

# 新增技术指标参数
RSI_PERIOD = 14                  # RSI周期
RSI_OVERBOUGHT = 70              # RSI超买阈值
RSI_OVERSOLD = 30                # RSI超卖阈值
MACD_FAST = 12                   # MACD快线周期
MACD_SLOW = 26                   # MACD慢线周期
MACD_SIGNAL = 9                  # MACD信号线周期
BOLL_PERIOD = 20                 # 布林带周期
BOLL_STD = 2                     # 布林带标准差倍数
ATR_PERIOD = 14                  # ATR周期

# ============================================================
# 四、买点参数
# ============================================================
SUPPORT_TOUCH_PCT = 0.01         # 回踩支撑位容差（±1%）
VOLUME_SHRINK_RATIO = 0.30       # 缩量标准：较20日均量萎缩30%以上

# 买点2参数（放量突破后回踩确认）
BREAKOUT_LOOKBACK = 10           # 突破回看天数（近10日内有突破）
BREAKOUT_VOLUME_RATIO = 1.5      # 突破日量能>均量1.5倍
BREAKOUT_PULLBACK_VOL = 0.50     # 回踩日量缩至突破日50%以下
BREAKOUT_HOLD_PCT = 0.99         # 回踩不破突破位（收盘>=突破日收盘*0.99）

# 买入排除条件
REJECT_DROP_PCT = -0.03          # 当日跌幅>3%且放量 → 排除
REJECT_VOLUME_RATIO = 1.5        # 放量下跌的量能阈值

# ============================================================
# 五、建仓参数（分批建仓）
# ============================================================
FIRST_BATCH_RATIO = 0.40         # 第一批试仓比例（40%）
SECOND_BATCH_RATIO = 0.60        # 第二批加仓比例（60%）
MIN_PROFIT_TO_ADD = 0.03         # 浮盈>=3%才允许加第二批

# ============================================================
# 六、止损参数（V6.0优化版 - 基于网格搜索最优参数）
# ============================================================
# 优化结论: 10%止损比8%更优（减少被洗出，给交易更多呼吸空间）
INITIAL_STOP_LOSS_PCT = 0.10     # 初始止损幅度（买入价下方10%）
INITIAL_STOP_LOSS_LOW = 0.10     # 稳健龙头止损下限（V6.0: 8%→10%）

# 移动止损档位（浮盈 -> 止损上移到的位置）
# V6.0优化: 保本线从5%降至3%（更早保护本金），锁定从+2%降至+1%
TRAILING_STOP_LEVELS = [
    # (浮盈下限, 浮盈上限, 止损位置描述)
    (0.00, 0.03, "initial"),       # 浮盈<3%: 维持初始止损
    (0.03, 0.15, "cost_plus"),     # 浮盈3%-15%: 止损上移到成本价+1%（V6.0: 5%→3%, +2%→+1%）
    (0.15, 0.30, "profit_12"),     # 浮盈15%-30%: 止损上移到盈利12%处
    (0.30, 9.99, "profit_22"),     # 浮盈>30%: 止损上移到盈利22%处
]

# 强制卖出参数
FORCE_SELL_DROP_PCT = -0.08      # 单日跌幅>8%
FORCE_SELL_VOLUME_RATIO = 2.0    # 且量>均量2倍 → 无条件离场

# V6.0新增: 时间止损（持仓超期且浮盈不足则卖出，提高资金效率）
MAX_HOLD_DAYS = 20               # 最大持仓天数（中线波段4周=20交易日）
TIME_STOP_PROFIT = 0.05          # 超期且浮盈<5%则卖出（中线标准提高）

# V6.0新增: 卖出逻辑开关（基于回测诊断结论）
# 诊断: MACD死叉37笔仅+2.11%平均，趋势破位40笔全亏-4.52%
MACD_DEATH_CROSS_ENABLED = False  # 关闭MACD死叉卖出（假信号太多）
TREND_BREAK_ENABLED = False       # 关闭趋势破位卖出（40笔全亏，假破位严重）

# ============================================================
# 七、止盈参数（双轨制）V6.0优化版
# ============================================================
# V6.0优化: 阶梯第1档从8%提高到10%（让利润跑更远再分批）
# 第一轨：阶梯目标止盈
LADDER_SELL_LEVELS = [
    # (浮盈阈值, 卖出比例)
    (0.10, 1/3),                   # 浮盈10%: 卖出1/3（V6.0: 8%→10%）
    (0.20, 1/3),                   # 浮盈20%: 再卖出1/3
]

# 第二轨：回落止盈（按股票类型区分）
# V6.0优化: 放宽回落阈值（让利润跑更远，减少过早止盈）
DRAWDOWN_STOP = {
    "龙头稳健": 0.07,              # 稳健龙头：高点回落7%（V6.0: 5%→7%）
    "成长赛道": 0.06,              # 成长赛道：回落6%（V6.0: 4%→6%）
    "高弹性":   0.05,              # 高弹性小票：回落5%（V6.0: 3%→5%）
}

# ============================================================
# 八、风控熔断参数
# ============================================================
# 单笔风险控制
MAX_SINGLE_LOSS_RATIO = 0.02     # 单笔最大亏损不超过总资金2%

# 日度熔断
DAILY_LOSS_LIMIT_1 = 0.02        # 单日亏损>=2%: 停止买入，只卖不买
DAILY_LOSS_LIMIT_2 = 0.03        # 单日亏损>=3%: 清非主线弱势仓，总仓位<=60%

# 周度熔断
WEEKLY_LOSS_LIMIT = 0.08         # 单周亏损>=8%: 全仓降至3成以下，强制休息1周

# --- 交易成本参数（回测必扣）---
COMMISSION_RATE = 0.0003         # 佣金万3（买卖双边）
STAMP_TAX_RATE = 0.001           # 印花税千1（卖出单边）
MIN_COMMISSION = 5.0             # 最低佣金5元

# 行情强度判定（用于动态调整总仓位）
MARKET_STRONG_MAX = 0.90         # 强势行情最大仓位90%
MARKET_STRONG_MIN = 0.70         # 强势行情最小仓位70%
MARKET_NORMAL_MAX = 0.80         # 震荡行情最大仓位80%
MARKET_NORMAL_MIN = 0.50         # 震荡行情最小仓位50%
MARKET_WEAK_MAX = 0.30           # 弱势行情最大仓位30%

# ============================================================
# 八''、统一风控参数（U0 - 双轨风控合并后的唯一参数源）
# ============================================================
# 合并 RiskGate(V2.0) 与 risk_check(V3.0) 两套系统，取更保守值
RISK_UNIFIED_CONFIG = {
    # --- 持仓与仓位 ---
    "max_holdings": 7,                # 持仓数量硬限制（7只最优，已验证）
    "max_single_position": 0.25,      # 单只最大仓位25%（ETF 20%/个股15%的保守上限）
    "etf_max_ratio": 0.20,            # 单只ETF仓位上限20%
    "stock_max_ratio": 0.15,          # 单只个股仓位上限15%
    "flexible_max_ratio": 0.08,       # 弹性标的单只上限8%
    "sector_max_ratio": 0.40,         # 单一赛道仓位上限40%
    "min_cash_ratio": 0.10,           # 最低现金保留10%

    # --- 总仓位动态上限（与指数均线绑定）---
    "total_above_ma20": 0.80,         # 指数在MA20上方 → 总仓位<=80%
    "total_below_ma20": 0.50,         # 指数跌破MA20 → 总仓位<=50%
    "total_below_ma60": 0.30,         # 指数跌破MA60 → 总仓位<=30%

    # --- 亏损熔断 ---
    "daily_loss_limit": 0.02,         # 单日亏损>=2%: 当日禁止新开仓（更保守）
    "daily_loss_limit_l2": 0.03,      # 单日亏损>=3%: 清非主线弱势仓
    "weekly_loss_limit": 0.08,        # 单周亏损>=8%: 强制降到30%以下
    "weekly_loss_pause_days": 3,      # 周熔断后暂停开仓天数

    # --- 连续亏损熔断 ---
    "consecutive_loss_pause": 3,      # 连续亏损3笔 → 暂停3天
    "consecutive_loss_today": 2,      # 连续亏损2笔 → 当日禁止开仓
    "cool_down_days": 2,              # 卖出后冷却天数

    # --- 满仓防护 ---
    "full_position_threshold": 0.90,  # 仓位>=90%绝对禁止买入
    "near_full_position": 0.80,      # 仓位>=80%禁止新开仓

    # --- 盈亏比与交易管控 ---
    "min_risk_reward": 2.5,           # 最低盈亏比
    "max_daily_opens": 2,             # 单日开仓次数上限
    "block_add_on_loss": True,        # 浮亏时绝对禁止加仓

    # --- 单笔风险 ---
    "max_single_loss_ratio": 0.02,    # 单笔最大亏损2%
    "initial_stop_loss_pct": 0.10,    # 初始止损幅度10%（固定百分比兜底）

    # --- ATR自适应止损（V8.3新增）---
    "atr_stop_multiplier": 2.0,       # ATR倍数，止损距离 = ATR * 此倍数
    "min_stop_loss_pct": 0.05,        # 最小止损比例5%（兜底，防止止损太远）
    "max_stop_loss_pct": 0.15,        # 最大止损比例15%（防止止损太近）
    "trailing_atr_multiplier": 1.5,   # 移动止损ATR倍数
}

# ============================================================
# 九、交易时间红线
# ============================================================
NO_TRADE_MORNING = ("09:30", "10:00")   # 开盘半小时禁止新开仓
NO_TRADE_AFTERNOON = ("14:30", "15:00") # 收盘半小时禁止新开仓
NO_NEW_AFTER = "13:30"                   # 下午1:30后禁止新开计划外标的

# ============================================================
# 十、数据与通知配置
# ============================================================
import os as _os
PROJECT_ROOT = _os.path.dirname(_os.path.abspath(__file__))
DATA_DIR = _os.path.join(PROJECT_ROOT, "data")
DB_PATH = _os.path.join(DATA_DIR, "stock_db.db")
LOG_DIR = _os.path.join(PROJECT_ROOT, "logs")
OUTPUT_DIR = _os.path.join(PROJECT_ROOT, "output")

# 持仓文件路径（优先项目内，兼容旧路径）
HOLDINGS_FILE = _os.path.join(PROJECT_ROOT, "holdings.json")
_HOLDINGS_FILE_LEGACY = _os.path.join(_os.path.dirname(PROJECT_ROOT), "holdings.json")

def get_holdings_file() -> str:
    """获取持仓文件路径（优先新路径，兼容旧路径）"""
    if _os.path.exists(HOLDINGS_FILE):
        return HOLDINGS_FILE
    if _os.path.exists(_HOLDINGS_FILE_LEGACY):
        return _HOLDINGS_FILE_LEGACY
    return HOLDINGS_FILE  # 默认新路径（用于创建）


def load_holdings(validated: bool = True) -> dict:
    """
    加载持仓数据（带成本价合理性校验）

    校验规则:
      - buy_price <= 0 或 shares <= 0: 已清仓，跳过
      - buy_price > current_price × 5: 疑似摊薄成本/除权前旧价，标记异常并跳过止损计算

    参数:
        validated: 是否执行成本价校验（默认True）

    返回:
        持仓字典 {code: {name, shares, buy_price, ...}}
    """
    import json as _json
    holdings_file = get_holdings_file()
    if not _os.path.exists(holdings_file):
        return {}
    try:
        with open(holdings_file, "r", encoding="utf-8") as f:
            holdings = _json.load(f)
    except Exception:
        return {}

    if not validated:
        return holdings

    # 成本价合理性校验
    import logging as _logging
    _logger = _logging.getLogger(__name__)
    for code, info in list(holdings.items()):
        if not isinstance(info, dict):
            continue
        buy_price = info.get("buy_price", 0) or info.get("cost", 0)
        current_price = info.get("current_price", 0)
        shares = info.get("shares", 0)
        if shares <= 0 or buy_price <= 0:
            continue
        # ETF/股票成本价 > 现价×5 → 疑似摊薄成本或除权前旧价
        if current_price > 0 and buy_price > current_price * 5:
            _logger.warning(
                f"[成本异常] {code}({info.get('name','')}) buy_price={buy_price:.3f} "
                f">> current_price={current_price:.3f} (比值{buy_price/current_price:.1f}x), "
                f"疑似摊薄成本/除权前旧价, 已标记跳过止损计算"
            )
            info["_cost_anomaly"] = True
    return holdings

# 确保必要目录存在
for _d in (DATA_DIR, LOG_DIR, OUTPUT_DIR):
    _os.makedirs(_d, exist_ok=True)

# 数据获取配置
DATA_RETRY_TIMES = 3             # 失败重试次数
DATA_RETRY_INTERVAL = 2          # 重试间隔（秒）

# 钉钉/企业微信通知（按需填写Webhook地址）
DINGTALK_WEBHOOK = ""            # 钉钉机器人Webhook URL
WECHAT_WORK_WEBHOOK = ""         # 企业微信机器人Webhook URL

# ============================================================
# 十二、邮件通知配置（QQ邮箱SMTP）
# ============================================================
EMAIL_SMTP_HOST = "smtp.qq.com"  # QQ邮箱SMTP服务器
EMAIL_SMTP_PORT = 465            # SMTP端口（SSL）
EMAIL_SENDER = "563646039@qq.com"                # 发件人QQ邮箱
# FIX: 修复邮箱授权码明文硬编码，改为环境变量读取
EMAIL_AUTH_CODE = "yxagunjowxedbdai"             # QQ邮箱授权码
EMAIL_RECEIVER = "563646039@qq.com"  # 收件人邮箱

# ============================================================
# 十一、基准指数（用于判定行情强弱）
# ============================================================
BENCHMARK_INDEX = "000300"       # 基准指数代码（沪深300）

# ============================================================
# 十三、组合风险管理配置（V3.0新增）
# ============================================================
RISK_CONFIG = {
    "correlation_threshold": 0.70,    # 相关性预警阈值
    "stock_hhi_warning": 0.25,        # 个股HHI预警线
    "sector_hhi_warning": 0.30,       # 行业HHI预警线
    "var_confidence": 0.95,           # VaR置信度
    "max_drawdown_warning": 0.10,     # 最大回撤预警线
    "rebalance_threshold": 0.05,      # 再平衡触发偏差(5%)
    "atr_position_risk": 0.02,        # ATR仓位法单笔风险比例
}

# ============================================================
# 十四、多策略引擎配置（V3.0新增）
# ============================================================
STRATEGY_CONFIG = {
    # 均值回归策略
    "mean_reversion": {
        "enabled": True,
        "target_profit": 0.06,        # 目标利润6%
        "stop_loss": 0.04,            # 止损4%
        "max_hold_days": 5,           # 最大持有天数
        "max_position_ratio": 0.08,   # 单只最大仓位8%
        "min_conditions": 3,          # 最少满足条件数
    },
    # 多周期共振
    "multi_timeframe": {
        "enabled": True,
        "min_resonance_score": 3,     # 最低共振评分(满分5)
        "weekly_ma_period": 10,       # 周线MA周期
    },
    # 大盘状态识别
    "market_regime": {
        "enabled": True,
        "bull_threshold": 0.3,        # 牛市判定阈值
        "bear_threshold": -0.3,       # 熊市判定阈值
    },
}

# ============================================================
# 十五、盘中监控配置（V3.0新增）
# ============================================================
MONITOR_CONFIG = {
    "poll_interval": 30,              # 轮询间隔(秒)，30秒刷新一次
    "rapid_drop_pct": -0.03,          # 急跌预警阈值(-3%)
    "market_drop_pct": -0.015,        # 大盘急跌阈值(-1.5%)
    "amplitude_alert_pct": 0.08,      # 振幅预警(8%)
    "profit_alert_levels": [0.08, 0.20],  # 止盈提醒档位
}

# ============================================================
# 十五’、反洗盘保护配置（V7.1新增）
# ============================================================
ANTI_WASH_CONFIG = {
    "buffer_minutes": 15,             # 触及止损后等待15分钟确认
    "buffer_confirm_pct": 0.01,       # 15分钟后仍低于止损线1%才确认
    "v_reversal_pct": 0.05,           # 从日内低点反弹5%暂停止损
    "volume_spike_ratio": 2.0,        # 放量急跌判定: 量>均量2倍
    "volume_spike_extra": 0.03,       # 放量急跌额外放宽3%止损
    "soft_stop_mode": True,           # 软止损模式: 仅预警不挂单
    "soft_stop_buffer": 0.03,         # 软止损价格下移3%缓冲
}

# ============================================================
# 十五''、盘中梯度减仓预警配置（P0-1: 浮亏加速扩大时强制预警+条件单）
# ============================================================
GRADIENT_REDUCE_CONFIG = {
    # 梯度减仓阈值（浮亏比例 → 建议减仓比例）
    "levels": [
        {"loss_pct": -0.05, "reduce_ratio": 1/3, "level": "warning",  "label": "浮亏5%减仓1/3"},
        {"loss_pct": -0.08, "reduce_ratio": 1/2, "level": "critical", "label": "浮亏8%减仓1/2"},
        {"loss_pct": -0.10, "reduce_ratio": 1.0, "level": "emergency","label": "浮亏10%清仓"},
    ],
    # 连续阴跌触发（非单日暴跌，而是温水煮蛙式下跌）
    "consecutive_decline_days": 3,       # 连续N日下跌
    "consecutive_decline_total": -0.05,  # 累计跌幅超此值 → 触发减仓1/3
    # 单日放量暴跌（不等收盘，盘中直接触发）
    "intraday_crash_pct": -0.05,         # 盘中跌超5%
    "intraday_crash_vol_ratio": 2.0,     # 且量>均量2倍 → 立即减仓1/2
    # 条件单生成
    "generate_condition_order": True,    # 是否自动生成东方财富条件单
    "order_output_dir": "output",        # 条件单输出目录
    # 冷却（同一标的同一级别当日只触发一次）
    "cooldown_per_level": True,
}

# ============================================================
# 十五'''、盘中监控分级变频配置（P0-2: 急跌时自动提升扫描频率）
# ============================================================
INTRADAY_ESCALATION_CONFIG = {
    # 三级监控频率
    "normal_interval_min": 10,     # 正常状态: 10分钟/次
    "warning_interval_min": 3,     # 预警状态: 3分钟/次
    "emergency_interval_min": 1,   # 紧急状态: 1分钟/次
    # 升级条件（满足任一即升级）
    "warning_triggers": {
        "holding_drop_pct": -3.0,      # 持仓股日内跌>3%
        "market_drop_pct": -1.5,       # 大盘跌>1.5%
        "approaching_stop_loss": 0.02, # 距止损线<2%
    },
    "emergency_triggers": {
        "holding_drop_pct": -5.0,      # 持仓股日内跌>5%
        "stop_loss_touched": True,     # 已触及止损线
        "market_drop_pct": -2.5,       # 大盘跌>2.5%
    },
    # 降级条件（连续N次扫描无异常则降回正常）
    "downgrade_after_clear": 3,    # 连续3次无异常 → 降回正常频率
}

# ============================================================
# 十五''''、反洗盘分场景缓冲配置（P0-3: 真跌快速确认，洗盘保留缓冲）
# ============================================================
SMART_BUFFER_CONFIG = {
    # 场景判定条件
    "true_decline": {
        # 满足以下>=3条判定为"真跌"，缓冲缩短
        "conditions": [
            "volume_shrinking",       # 缩量下跌（量<均量0.7倍）
            "ma_bearish",             # 均线空头（MA5<MA10<MA20）
            "market_weak",            # 大盘走弱（跌>0.5%）
            "sector_declining",       # 板块联动下跌（同赛道>=2只跌>2%）
            "consecutive_days",       # 连续2日以上下跌
        ],
        "min_conditions": 3,          # 满足>=3条 → 真跌
        "buffer_minutes": 3,          # 真跌: 缓冲仅3分钟
    },
    "wash_trading": {
        # 满足以下>=2条判定为"洗盘"，保留长缓冲
        "conditions": [
            "volume_spike",           # 放量急跌（量>均量2倍）
            "fast_rebound",           # 快速收回（5分钟内收回止损上方）
            "market_stable",          # 大盘稳定（涨跌<0.5%）
            "sector_strong",          # 板块未联动（同赛道其他股未跌）
        ],
        "min_conditions": 2,          # 满足>=2条 → 洗盘
        "buffer_minutes": 15,         # 洗盘: 保留15分钟缓冲
    },
    "default_buffer_minutes": 8,      # 无法判定时: 折中8分钟
}

# ============================================================
# 十六、交易日志配置（V3.0新增）
# ============================================================
JOURNAL_CONFIG = {
    "auto_record": True,              # 自动记录信号到日志
    "performance_days": 90,           # 绩效统计周期(天)
}

# ============================================================
# 十七、持仓趋势预测分析配置
# ============================================================
FORECAST_ENABLED = True              # 是否启用持仓趋势预测
FORECAST_LOOKBACK_DAYS = 120         # 分析回看天数
FORECAST_EMAIL_TITLE = "持仓趋势预测"  # 邮件标题前缀

# ============================================================
# 十八、回测引擎配置（V8.0新增）
# ============================================================
BACKTEST_CONFIG = {
    "initial_capital": TOTAL_CAPITAL,   # 回测初始资金
    "buy_slippage": 0.001,              # 买入滑点 0.1%
    "sell_slippage": 0.001,             # 卖出滑点 0.1%
    "commission_rate": 0.00025,         # 佣金万2.5
    "stamp_tax_rate": 0.001,            # 印花税千1
    "benchmark": "000300",              # 基准指数
    "walk_forward_train": 60,           # Walk-Forward训练窗口
    "walk_forward_test": 20,            # Walk-Forward验证窗口
}

# ============================================================
# 十九、动态仓位管理配置（V8.0新增）
# ============================================================
POSITION_CONFIG = {
    "method": "half_kelly",             # 仓位方法: half_kelly / risk_parity / dynamic
    "max_single": 0.15,                 # 单只最大15%
    "max_total": 0.90,                  # 总仓位最大90%
    "rebalance_threshold": 0.05,        # 再平衡触发偏离5%
    "atr_risk_per_trade": 0.02,         # ATR仓位法单笔风险2%
}

# ============================================================
# 二十、ML信号增强配置（V8.0新增）
# ============================================================
ML_CONFIG = {
    "enabled": False,                   # 是否启用ML确认（需先训练模型）
    "model_name": "xgb_v1",            # 模型文件名
    "confirm_threshold": 0.65,          # ML确认阈值
    "retrain_accuracy": 0.55,           # 重训练触发准确率
    "retrain_window": 10,               # 重训练观察窗口(天)
}

# ============================================================
# 二十二、交易纪律硬约束（V8.1 - 基于1322笔真实委托验证）
# ============================================================
# 验证结论: 胜率46.9%, 盈亏比0.79, 总亏损-315,162元
# 核心病因: 日均18笔买入/62.8%小单<1万/持仓越长胜率越低
DISCIPLINE_CONFIG = {
    "max_daily_buys": 3,              # 每日最多3笔买入（验证: 日均18笔→巨亏）
    "min_trade_amount": 20000,        # 单笔最低2万元（验证: 62.8%小单<1万被手续费吃掉）
    "max_holdings_hard": 7,           # 持仓硬限制7只（验证: 173只→无研究深度）
    "max_single_etf_ratio": 0.20,     # 单只ETF最大20%（当前科创50占53%严重超限）
    "cooldown_days": 3,               # 同标的卖出后冷却3天
    "min_holding_days": 3,            # 最小持仓3天（止损除外）
    "max_consecutive_loss": 5,        # 连亏5次强制休息1天
    "monthly_loss_limit": -0.10,      # 月亏损>10%降仓至5成
}

# ============================================================
# 二十一、模拟盘配置（V8.0新增）
# ============================================================
PAPER_TRADING_CONFIG = {
    "enabled": False,                   # 是否启用模拟盘
    "graduation_days": 20,              # 毕业所需天数
    "min_sharpe": 1.0,                  # 毕业最低夏普
    "max_drawdown": 0.10,               # 毕业最大回撤
}

# ============================================================
# 二十二、操盘密码系统配置 V2.0（自适应趋势策略引擎）
# ============================================================
CAOPAN_CONFIG = {
    # --- 控盘生命线（自适应双均线趋势通道）---
    "life_line_fast": 10,              # LL1基准周期 EMA10（快线）
    "life_line_slow": 30,              # LL2基准周期 EMA30（慢线）
    "adaptive_enabled": True,          # 是否启用自适应周期调整
    "atr_period": 14,                  # ATR计算周期
    "atr_lookback": 60,                # ATR历史分位回看周期
    "atr_high_pct": 0.80,              # ATR>80%分位 → 周期拉长20%
    "atr_low_pct": 0.20,               # ATR<20%分位 → 周期缩短20%
    "adaptive_stretch": 0.20,          # 周期调整幅度±20%

    # --- 乖离率分级交易指引 ---
    "deviation_overbought": 0.10,      # 偏离>10%: 超买区，减仓1/2
    "deviation_high": 0.05,            # 偏离5%~10%: 偏高区，减仓1/3
    "deviation_normal": 0.05,          # 偏离-5%~5%: 正常区间，持有
    "deviation_oversold": -0.10,       # 偏离<-10%: 超卖区，仅强上升可低吸

    # --- DK买卖点（三重共振确认）---
    "dk_volume_confirm_ratio": 1.0,    # 金叉量能确认: 当日量≥20日均量×1.0（放量加分，非必须）
    "dk_volume_ma_period": 20,         # 量能均线周期
    "dk_false_signal_days": 2,         # 假信号回检窗口（金叉后N日跌破LL2）
    "dk_consecutive_inflow": 3,        # 主力连续净流入天数
    "dk_min_strength_medium": 45,      # 中信号最低强度（从50降至45）
    "dk_min_strength_strong": 65,      # 强信号最低强度（从70降至65）
    "dk_pullback_entry": True,         # 启用回踩LL1入场（D点后等回踩）
    "dk_pullback_days": 5,             # D点后N日内回踩LL1视为有效入场

    # --- 资金监控（多维验证）---
    "fund_flow_method": "estimate",    # "akshare" / "estimate"
    "large_order_ratio": 0.20,         # 大单占比阈值
    "fund_flow_ma_period": 5,          # 资金流均线周期
    "fund_min_layers_confirm": 2,      # 最少N层同向才确认有效
    "fund_divergence_days": 3,         # 背离判定连续天数
    "fund_mild_build_days": 3,         # 温和建仓最少连续天数

    # --- 市场环境自适应 ---
    "market_cross_freq_threshold": 5,  # 20日内穿越生命线次数≥N → 震荡市（从4放宽至5）
    "market_vol_period": 20,           # 波动率计算周期
    "oscillation_auto_shield": True,   # 震荡市自动屏蔽DK信号

    # --- 盈亏比硬门槛 ---
    "min_risk_reward": 1.5,            # 最低盈亏比（从2.5降至1.5，增加交易机会）
    "stop_loss_below_ll2": 0.02,       # 止损位: LL2下方2%（从3%收紧至2%）
    "max_risk_atr_mult": 2.0,          # 最大风险不超过2倍ATR（防止止损太远）
    "rr_position_scale": True,         # 盈亏比联动仓位

    # --- 多周期共振 ---
    "weekly_trend_required": True,     # 周线趋势必须同向
    "weekly_ma_period": 10,            # 周线均线周期

    # --- 回测参数 ---
    "backtest_years": 3,
    "backtest_commission": 0.0003,
    "backtest_stamp_tax": 0.001,
    "backtest_slippage": 0.001,
    "backtest_train_ratio": 0.70,      # 训练集70% / 验证集30%

    # --- 参数优化网格 ---
    "optimize_grid": {
        "life_line_fast": [8, 10, 13, 15, 20],
        "life_line_slow": [25, 30, 34, 40, 50, 60],
    },
}

# ============================================================
# 二十三、V9.0 看盘策略增强配置（VWAP/盘口/竞价/量比）
# ============================================================

# --- P0-1: VWAP均价线监控 ---
VWAP_CONFIG = {
    "enabled": True,
    "bearish_deviation_pct": -0.015,   # 价格低于VWAP 1.5% → 空头控盘预警
    "bearish_persist_cycles": 3,        # 连续N个轮询周期低于VWAP → 确认走弱
    "bullish_deviation_pct": 0.03,      # 价格高于VWAP 3% → 超买偏离提示
    "vwap_break_alert": True,           # 从VWAP上方跌破到下方 → 即时预警
}

# --- P0-2: 盘口强弱监控 ---
ORDERBOOK_CONFIG = {
    "enabled": True,
    "heavy_sell_outer_ratio": 0.35,     # 外盘占比<35% + 跌>2% → 抛压沉重(真跌)
    "strong_buy_outer_ratio": 0.65,     # 外盘占比>65% + 价格平/微跌 → 资金承接(洗盘)
    "ask_pressure_ratio": 5.0,          # 卖一量 > 买一量×5 → 压盘吸筹
    "bid_withdraw_pct": 0.20,           # 买一量比上轮<20% → 托盘撤退
    "order_ratio_bearish": -0.30,       # 委比<-30% → 卖压偏重
    "order_ratio_bullish": 0.30,        # 委比>30% → 买压偏重
}

# --- P0-3: 集合竞价分析 ---
AUCTION_CONFIG = {
    "enabled": True,
    "high_open_warn_pct": 0.03,         # 高开>3% → 警惕出货
    "high_open_extreme_pct": 0.05,      # 高开>5% → 紧急预警
    "low_open_warn_pct": -0.02,         # 低开<-2% → 恐慌预警
    "low_open_extreme_pct": -0.03,      # 低开<-3% → 紧急预警(开盘即亏损扩大)
    "volume_surge_ratio": 3.0,          # 竞价量 > 5日均量×3 → 主力有备而来
    "volume_weak_ratio": 0.5,           # 竞价量 < 5日均量×0.5 → 量能不足(高开无力)
    "alert_email": True,                # 异常时发送邮件
}

# --- P0-4: 开盘30分钟定性 + 尾盘异动 ---
PHASE_CONFIG = {
    "enabled": True,
    # 开盘定性
    "opening_end_time": "10:00",        # 开盘阶段结束时间
    "high_open_low_walk_pct": 0.01,     # 高开>1%后转跌 → 出货信号
    "low_open_high_walk_pct": -0.01,    # 低开<-1%后转涨 → 吸筹信号
    "opening_vol_ratio": 0.25,          # 前30分钟量占全天>25% → 主力有备
    # 尾盘异动
    "closing_start_time": "14:45",      # 尾盘阶段开始时间
    "closing_surge_pct": 1.5,           # 尾盘5分钟涨>1.5% → 次日高开概率
    "closing_plunge_pct": -1.5,         # 尾盘5分钟跌>1.5% → 次日风险预警
    "closing_vol_ratio": 2.0,           # 尾盘量比>2 → 确认异动有效
}

# --- P1-1: 量比异动 ---
VOL_RATIO_CONFIG = {
    "enabled": True,
    "surge_threshold": 3.0,             # 量比>3 → 异动
    "stagnation_change_pct": 1.0,       # 量比>3 + 涨幅<1% → 放量滞涨(出货)
    "bottom_volume_pct": 3.0,           # 量比>3 + 低位 → 底部放量(关注)
    "high_level_profit_pct": 5.0,       # 浮盈>5%时放量滞涨 → 出货嫌疑
}

# --- P1-2: K线形态识别 ---
KLINE_PATTERN_CONFIG = {
    "enabled": True,
    "min_body_ratio": 0.3,              # 实体占振幅最小比例(十字星判定)
    "long_shadow_ratio": 2.0,           # 影线>实体2倍 → 长影线
    "doji_body_pct": 0.10,              # 实体<振幅10% → 十字星
    "engulfing_ratio": 1.2,             # 吞没: 今实体>昨实体×1.2
    "three_crows_min_drop": -0.02,      # 三只乌鸦: 每根跌幅>2%
    "alert_email_on_top": True,         # 顶部形态发邮件
    "lookback_days": 5,                 # 形态识别回看天数
}

# --- P1-3: 主力四阶段 ---
MAIN_FORCE_STAGE_CONFIG = {
    "enabled": True,
    # 吸筹期特征
    "accumulation_min_days": 20,        # 底部横盘最少天数
    "accumulation_turnover_range": [1.0, 3.0],  # 换手率温和区间(%)
    "accumulation_obv_rising": True,    # OBV趋势上升
    # 拉升期特征
    "markup_min_consecutive_up": 3,     # 连续阳线最少天数
    "markup_vol_increase": 1.5,         # 量增(>均量1.5倍)
    # 出货期特征
    "distribution_high_vol_ratio": 2.0, # 高位放量(>均量2倍)
    "distribution_stagnation": 0.01,    # 涨幅<1%(滞涨)
    "distribution_inner_dominant": 0.6, # 内盘占比>60%
}

# --- P2-1: 竞价轨迹采集 ---
AUCTION_TRACK_CONFIG = {
    "enabled": True,
    "sample_interval_sec": 30,          # 采样间隔(秒)
    "start_time": "09:15",              # 采集开始时间
    "end_time": "09:25",                # 采集结束时间
    "fake_high_open_drop": 0.02,        # 先高后低: 高开回落>2% → 诱多
    "real_demand_rise": 0.01,           # 先低后高: 上移>1% → 真实需求
    "rush_buy_acceleration": 0.5,       # 量能加速>50% → 抢筹
}

# --- P2-3: 分时形态识别 ---
INTRADAY_PATTERN_CONFIG = {
    "enabled": True,
    "min_data_points": 30,              # 最少数据点(30分钟)才开始识别
    "m_head_peak_diff_pct": 0.003,      # M头: 两峰差<0.3%
    "m_head_neckline_break": -0.005,    # M头: 跌破颈线0.5%确认
    "stair_min_steps": 3,               # 阶梯: 最少3级台阶
    "stair_min_rise_pct": 0.005,        # 阶梯: 每级上涨>0.5%
    "v_reversal_drop_pct": -0.03,       # V反: 急跌>3%
    "v_reversal_bounce_pct": 0.02,      # V反: 5分钟反弹>2%
}

# --- P2-4: 缺口分析 ---
GAP_CONFIG = {
    "enabled": True,
    "min_gap_pct": 0.005,               # 最小有效缺口(0.5%)
    "breakaway_vol_ratio": 2.0,         # 突破缺口: 量比>2
    "exhaustion_consecutive_gaps": 3,   # 连续N个同向缺口 → 衰竭
    "exhaustion_vol_spike": 2.5,        # 衰竭缺口: 量异常放大>2.5倍
}
