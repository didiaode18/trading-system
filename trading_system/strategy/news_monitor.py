"""
新闻/政策风险监控模块
======================
定位：风控刹车 + 选股过滤器（绝不产生核心买卖信号）

功能:
  1. 通过AKShare拉取个股新闻和财经要闻
  2. 基于负面关键词字典进行情感检测（分3级）
  3. 输出风险报告供选股过滤和邮件预警使用

安全边界:
  - 本模块输出永远不会写入 buy_signal 或 sell_signal 字段
  - 不修改任何技术信号逻辑
  - 所有预警标注"仅供参考，非交易信号"

使用方式:
    from strategy.news_monitor import scan_news_risk
    news_risk = scan_news_risk(codes, holdings)
"""

import os
import sys
import logging
import datetime
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger(__name__)

# 尝试导入akshare
try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    HAS_AKSHARE = False
    logger.warning("[新闻监控] akshare未安装，新闻功能不可用")


# ============================================================
# 一、负面关键词字典（分级）
# ============================================================

# level 3: 重大利空 - 选股直接排除
KEYWORDS_CRITICAL = [
    "立案调查", "证监会处罚", "强制退市", "财务造假", "欺诈发行",
    "重大违法", "暂停上市", "终止上市", "被ST", "被*ST",
    "刑事立案", "实控人被捕", "资金链断裂", "债务违约",
]

# level 2: 警惕 - 选股降权/标记
KEYWORDS_WARNING = [
    "行政处罚", "监管函", "问询函", "大股东减持", "业绩暴雷",
    "诉讼仲裁", "质押爆仓", "违规担保", "信息披露违规",
    "交易所谴责", "限制交易", "冻结资产", "破产重整",
    "高管被查", "关联交易", "利润虚增", "审计保留意见",
]

# level 1: 关注 - 仅提示
KEYWORDS_NOTICE = [
    "业绩下滑", "高管辞职", "行业制裁", "政策收紧", "评级下调",
    "商誉减值", "产能过剩", "价格战", "反垄断", "环保处罚",
    "产品召回", "质量事故", "劳资纠纷", "税务稽查",
    "减持计划", "解禁压力", "定增摊薄", "可转债回售",
]


# ============================================================
# 二、新闻拉取
# ============================================================

def fetch_stock_news(code: str, max_count: int = None) -> list:
    """
    通过AKShare获取个股新闻（东方财富来源）
    
    参数:
        code: 股票代码（6位数字）
        max_count: 最多返回条数
    
    返回:
        [{"title": str, "time": str, "source": str, "url": str}, ...]
    """
    if not HAS_AKSHARE:
        return []
    
    if max_count is None:
        max_count = getattr(config, 'NEWS_MAX_PER_STOCK', 10)
    
    lookback_hours = getattr(config, 'NEWS_LOOKBACK_HOURS', 24)
    cutoff_time = datetime.datetime.now() - datetime.timedelta(hours=lookback_hours)
    
    news_list = []
    try:
        df = ak.stock_news_em(symbol=code)
        if df is None or df.empty:
            return []
        
        # 标准化列名（akshare版本差异）
        title_col = None
        time_col = None
        for col in df.columns:
            if "标题" in col or "新闻标题" in col or "title" in col.lower():
                title_col = col
            if "时间" in col or "发布时间" in col or "time" in col.lower():
                time_col = col
        
        if title_col is None:
            # 尝试第一列作为标题
            title_col = df.columns[0]
        
        for _, row in df.head(max_count * 2).iterrows():
            title = str(row.get(title_col, ""))
            pub_time_str = str(row.get(time_col, "")) if time_col else ""
            
            # 解析时间，过滤超时新闻
            pub_time = _parse_news_time(pub_time_str)
            if pub_time and pub_time < cutoff_time:
                continue
            
            news_list.append({
                "title": title,
                "time": pub_time_str,
                "pub_time": pub_time,
                "source": "eastmoney",
            })
            
            if len(news_list) >= max_count:
                break
    
    except Exception as e:
        logger.debug(f"[新闻监控] {code} 新闻拉取失败: {e}")
    
    return news_list


def fetch_market_alerts(max_count: int = 30) -> list:
    """
    获取财联社电报（政策/行业级别要闻）
    
    返回:
        [{"title": str, "time": str, "source": "cls"}, ...]
    """
    if not HAS_AKSHARE:
        return []
    
    lookback_hours = getattr(config, 'NEWS_LOOKBACK_HOURS', 24)
    cutoff_time = datetime.datetime.now() - datetime.timedelta(hours=lookback_hours)
    
    alerts = []
    try:
        df = ak.stock_zh_a_alerts_cls()
        if df is None or df.empty:
            return []
        
        title_col = None
        time_col = None
        for col in df.columns:
            if "标题" in col or "内容" in col or "title" in col.lower():
                title_col = col
            if "时间" in col or "time" in col.lower():
                time_col = col
        
        if title_col is None:
            title_col = df.columns[0]
        
        for _, row in df.head(max_count * 3).iterrows():
            title = str(row.get(title_col, ""))
            pub_time_str = str(row.get(time_col, "")) if time_col else ""
            
            pub_time = _parse_news_time(pub_time_str)
            if pub_time and pub_time < cutoff_time:
                continue
            
            alerts.append({
                "title": title,
                "time": pub_time_str,
                "pub_time": pub_time,
                "source": "cls",
            })
            
            if len(alerts) >= max_count:
                break
    
    except Exception as e:
        logger.debug(f"[新闻监控] 财联社电报拉取失败: {e}")
    
    return alerts


# ============================================================
# 三、情感分析（关键词匹配）
# ============================================================

def analyze_sentiment(title: str, content: str = "") -> dict:
    """
    基于负面关键词字典匹配，返回风险等级
    
    返回:
        {"level": int, "keywords": [str], "category": str}
        level: 0=正常, 1=关注, 2=警惕, 3=重大利空
    """
    text = f"{title} {content}".lower()
    
    # 检查 level 3 (重大利空)
    matched_critical = [kw for kw in KEYWORDS_CRITICAL if kw.lower() in text]
    if matched_critical:
        return {"level": 3, "keywords": matched_critical, "category": "重大利空"}
    
    # 检查 level 2 (警惕)
    matched_warning = [kw for kw in KEYWORDS_WARNING if kw.lower() in text]
    if matched_warning:
        return {"level": 2, "keywords": matched_warning, "category": "警惕"}
    
    # 检查 level 1 (关注)
    matched_notice = [kw for kw in KEYWORDS_NOTICE if kw.lower() in text]
    if matched_notice:
        return {"level": 1, "keywords": matched_notice, "category": "关注"}
    
    return {"level": 0, "keywords": [], "category": "正常"}


# ============================================================
# 四、批量扫描
# ============================================================

def scan_news_risk(codes: list, holdings: dict = None) -> dict:
    """
    批量扫描股票新闻风险
    
    参数:
        codes: 要扫描的股票代码列表
        holdings: 当前持仓（持仓股优先扫描）
    
    返回:
        {code: {"level": int, "alerts": [{"title", "time", "keywords", "category"}]}}
        只返回level>=1的股票
    """
    if not HAS_AKSHARE:
        logger.warning("[新闻监控] akshare不可用，跳过新闻扫描")
        return {}
    
    if not getattr(config, 'NEWS_MONITOR_ENABLED', False):
        return {}
    
    # 去重 + 持仓优先
    codes = list(dict.fromkeys(codes))
    if holdings:
        holding_codes = list(holdings.keys())
        codes = holding_codes + [c for c in codes if c not in holding_codes]
    
    risk_result = {}
    scanned = 0
    
    for code in codes:
        # 跳过指数
        if code == "000300" or code.startswith("399"):
            continue
        
        news_list = fetch_stock_news(code)
        scanned += 1
        
        # 分析每条新闻
        stock_alerts = []
        max_level = 0
        
        for news in news_list:
            sentiment = analyze_sentiment(news["title"])
            if sentiment["level"] > 0:
                stock_alerts.append({
                    "title": news["title"],
                    "time": news["time"],
                    "keywords": sentiment["keywords"],
                    "category": sentiment["category"],
                    "level": sentiment["level"],
                })
                max_level = max(max_level, sentiment["level"])
        
        if max_level > 0:
            stock_name = config.get_stock_name(code)
            risk_result[code] = {
                "level": max_level,
                "name": stock_name,
                "alerts": sorted(stock_alerts, key=lambda x: -x["level"]),
                "in_holdings": code in (holdings or {}),
            }
            level_label = {1: "关注", 2: "警惕", 3: "重大利空"}
            logger.info(f"  [新闻风险] {code} {stock_name}: "
                       f"{level_label.get(max_level, '?')} | "
                       f"{stock_alerts[0]['title'][:40]}")
        
        # 控制请求频率，避免被封
        if scanned % 5 == 0:
            time.sleep(0.5)
    
    # 扫描市场级别要闻（政策/行业）
    market_alerts = fetch_market_alerts()
    if market_alerts:
        for alert in market_alerts:
            sentiment = analyze_sentiment(alert["title"])
            if sentiment["level"] >= 2:
                # 检查是否涉及持仓股所在行业
                _match_market_alert_to_stocks(alert, sentiment, risk_result, holdings)
    
    return risk_result


def _match_market_alert_to_stocks(alert: dict, sentiment: dict,
                                   risk_result: dict, holdings: dict):
    """将市场级别新闻匹配到相关持仓股"""
    if not holdings:
        return
    
    title = alert["title"]
    for code, pos in holdings.items():
        sector = pos.get("sector", "")
        name = config.get_stock_name(code)
        # 如果新闻标题包含行业关键词或股票名称
        if sector and sector in title or name and name in title:
            if code not in risk_result:
                risk_result[code] = {
                    "level": sentiment["level"],
                    "name": name,
                    "alerts": [],
                    "in_holdings": True,
                }
            risk_result[code]["alerts"].append({
                "title": f"[行业政策] {title[:50]}",
                "time": alert["time"],
                "keywords": sentiment["keywords"],
                "category": sentiment["category"],
                "level": sentiment["level"],
            })
            risk_result[code]["level"] = max(
                risk_result[code]["level"], sentiment["level"]
            )


# ============================================================
# 五、生成预警HTML（供邮件使用）
# ============================================================

def generate_news_alert_html(news_risk: dict) -> str:
    """
    生成新闻风险预警HTML片段（嵌入邮件使用）
    
    参数:
        news_risk: scan_news_risk()的返回结果
    
    返回:
        HTML字符串，无风险时返回空字符串
    """
    if not news_risk:
        return ""
    
    if not getattr(config, 'NEWS_ALERT_IN_EMAIL', True):
        return ""
    
    level_labels = {3: "重大", 2: "警惕", 1: "关注"}
    level_colors = {3: "#FF4D4F", 2: "#FA8C16", 1: "#1890FF"}
    
    # 按level降序排列
    sorted_items = sorted(news_risk.items(), key=lambda x: -x[1]["level"])
    
    html = """
    <div style="background:#FFF1F0; border-left:4px solid #FF4D4F; padding:14px 16px; margin:15px 0; border-radius:4px">
        <h3 style="margin:0 0 10px 0; font-size:14px; color:#CF1322">
            新闻/政策风险预警（仅供参考，非交易信号）
        </h3>
        <ul style="margin:0; padding-left:18px; line-height:2">
"""
    
    for code, info in sorted_items:
        level = info["level"]
        label = level_labels.get(level, "?")
        color = level_colors.get(level, "#333")
        name = info.get("name", code)
        holding_tag = " [持仓]" if info.get("in_holdings") else ""
        
        # 取最高级别的alert标题
        top_alert = info["alerts"][0] if info["alerts"] else {}
        title = top_alert.get("title", "")[:50]
        pub_time = top_alert.get("time", "")
        
        html += f'            <li><span style="color:{color};font-weight:bold">[{label}]</span> '
        html += f'{code} {name}{holding_tag}: "{title}"'
        if pub_time:
            html += f' <span style="color:#999;font-size:11px">({pub_time})</span>'
        html += '</li>\n'
    
    html += """        </ul>
        <p style="color:#888; font-size:11px; margin:8px 0 0 0">
            以上为新闻关键词自动匹配结果，不构成买卖建议，请自行判断
        </p>
    </div>
"""
    return html


# ============================================================
# 辅助函数
# ============================================================

def _parse_news_time(time_str: str):
    """解析新闻时间字符串为datetime"""
    if not time_str:
        return None
    
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y%m%d %H:%M:%S",
        "%m-%d %H:%M",
    ]
    
    for fmt in formats:
        try:
            dt = datetime.datetime.strptime(time_str.strip(), fmt)
            # 如果没有年份，补充当前年份
            if dt.year == 1900:
                dt = dt.replace(year=datetime.datetime.now().year)
            return dt
        except (ValueError, TypeError):
            continue
    
    return None


# ============================================================
# 命令行测试入口
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    
    print("=" * 50)
    print("  新闻/政策风险监控 - 测试")
    print("=" * 50)
    
    if not HAS_AKSHARE:
        print("[ERROR] akshare未安装")
        sys.exit(1)
    
    # 测试持仓股新闻扫描
    import json
    holdings_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "..", "holdings.json")
    holdings = {}
    if os.path.exists(holdings_file):
        with open(holdings_file, "r", encoding="utf-8") as f:
            holdings = json.load(f)
    
    test_codes = list(holdings.keys())[:5] if holdings else ["600519", "002371", "600584"]
    
    print(f"\n扫描股票: {test_codes}")
    print("-" * 50)
    
    result = scan_news_risk(test_codes, holdings)
    
    if result:
        print(f"\n发现 {len(result)} 只股票有新闻风险:")
        for code, info in sorted(result.items(), key=lambda x: -x[1]["level"]):
            level_label = {1: "关注", 2: "警惕", 3: "重大利空"}
            print(f"  [{level_label[info['level']]}] {code} {info['name']}:")
            for alert in info["alerts"][:3]:
                print(f"    - {alert['title'][:50]} ({alert['time']})")
    else:
        print("\n未发现负面新闻风险（全部正常）")
    
    # 测试HTML生成
    html = generate_news_alert_html(result)
    if html:
        print(f"\nHTML预警片段: {len(html)}字符")
    else:
        print("\n无预警HTML（无风险）")
    
    print("\n测试完成")
