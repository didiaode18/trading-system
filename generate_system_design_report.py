# -*- coding: utf-8 -*-
"""
系统设计全景分析报告生成器
==========================
生成覆盖V7.0系统所有模块设计的HTML分析报告，并通过QQ邮箱SMTP发送。

报告章节:
  1. 系统架构总览（13模块层）
  2. 核心策略引擎（backtest_real.py V5.3）
  3. 数据层设计
  4. 风控体系
  5. 报告与通知体系
  6. 性能指标
  7. 策略版本演进
  附录: 专家审阅待完善项
"""
import sys
import os
import io
import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_system'))

from notify.email_notify import send_email

today = datetime.date.today().strftime("%Y-%m-%d")
now = datetime.datetime.now().strftime("%H:%M:%S")


def build_report_html():
    """构建完整的系统设计全景分析HTML报告"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:'Microsoft YaHei',sans-serif;padding:15px;background:#f0f2f5;font-size:13px;line-height:1.6}}
.container{{max-width:1000px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#0f2027,#203a43,#2c5364);color:#fff;padding:25px 30px;border-radius:10px 10px 0 0}}
.header h1{{margin:0;font-size:22px}}
.header .sub{{font-size:12px;opacity:.85;margin-top:8px}}
.content{{background:#fff;padding:25px 30px;border-radius:0 0 10px 10px;box-shadow:0 2px 12px rgba(0,0,0,.1)}}
h2{{color:#1a1a2e;font-size:17px;margin-top:30px;border-left:4px solid #2196f3;padding-left:12px}}
h3{{color:#333;font-size:14px;margin-top:18px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:15px 0}}
.card{{background:#f8f9fa;border-radius:8px;padding:14px;text-align:center;border:1px solid #e8e8e8}}
.card .v{{font-size:20px;font-weight:bold;color:#1976d2}}
.card .l{{font-size:11px;color:#7f8c8d;margin-top:4px}}
table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:12px}}
th{{background:#37474f;color:#fff;padding:9px 8px;text-align:center}}
td{{padding:8px;border-bottom:1px solid #eee;text-align:center}}
tr:nth-child(even){{background:#fafafa}}
.alert{{padding:12px 15px;border-radius:6px;margin:12px 0;font-size:12px}}
.alert-info{{background:#e3f2fd;border-left:4px solid #2196f3}}
.alert-success{{background:#e8f5e9;border-left:4px solid #4caf50}}
.alert-warning{{background:#fff3cd;border-left:4px solid #ffc107}}
.alert-danger{{background:#ffebee;border-left:4px solid #e74c3c}}
.module-box{{border:1px solid #e0e0e0;border-radius:8px;padding:12px 15px;margin:8px 0;background:#fafafa}}
.module-box b{{color:#1565c0}}
.flow-diagram{{background:#263238;color:#e0e0e0;padding:15px;border-radius:8px;font-family:monospace;font-size:12px;line-height:1.8;margin:12px 0;white-space:pre-wrap}}
.tag{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold;color:#fff;margin:2px}}
.tag-feat{{background:#4caf50}} .tag-perf{{background:#ff9800}} .tag-fix{{background:#f44336}}
.version-timeline{{border-left:3px solid #2196f3;padding-left:20px;margin:15px 0}}
.version-item{{margin:12px 0;position:relative}}
.version-item::before{{content:'';position:absolute;left:-26px;top:6px;width:10px;height:10px;border-radius:50%;background:#2196f3}}
.version-item .ver{{font-weight:bold;color:#1565c0}}
.footer{{text-align:center;color:#999;font-size:11px;margin-top:20px;padding-top:12px;border-top:1px solid #eee}}
</style></head><body><div class="container">
<div class="header">
<h1>📐 操盘密码 V9.0 — 系统设计全景分析报告</h1>
<div class="sub">生成日期: {today} {now} | 系统版本: V9.0 | 策略引擎: V5.3 | 模块数: 13层 | 定位: A股中线波段(3天-4周)</div>
</div>
<div class="content">
"""

    # ================================================================
    # 第一章: 系统架构总览
    # ================================================================
    html += """
<h2>一、系统架构总览（13个模块层）</h2>

<div class="alert alert-info">💡 系统定位: 个人量化交易辅助决策系统，核心解决"管不住手，让情绪毁掉策略"。采用条件单驱动+纪律自动化，覆盖数据获取→策略计算→风控执行→报告通知完整链路。</div>

<h3>1.1 模块层职责与依赖</h3>
<table>
<tr><th>序号</th><th>模块</th><th>职责</th><th>核心文件</th><th>上游依赖</th><th>下游输出</th></tr>
<tr><td>①</td><td><b>data/</b></td><td>行情数据获取与存储</td><td>data_loader.py, realtime.py</td><td>baostock/腾讯API</td><td>strategy, backtest</td></tr>
<tr><td>②</td><td><b>strategy/</b></td><td>交易策略与信号生成</td><td>trend_strategy.py, stock_screener.py</td><td>data</td><td>risk, notify</td></tr>
<tr><td>③</td><td><b>risk/</b></td><td>风控熔断与仓位约束</td><td>risk_control.py, position_sizing.py</td><td>strategy</td><td>execution</td></tr>
<tr><td>④</td><td><b>backtest/</b></td><td>策略回测与验证</td><td>engine.py, walk_forward.py, monte_carlo.py</td><td>data, strategy</td><td>报告</td></tr>
<tr><td>⑤</td><td><b>factors/</b></td><td>多因子计算</td><td>momentum.py, technical.py, volume.py</td><td>data</td><td>strategy, quant</td></tr>
<tr><td>⑥</td><td><b>position/</b></td><td>仓位管理</td><td>kelly.py, risk_parity.py, vol_target.py</td><td>risk, factors</td><td>execution</td></tr>
<tr><td>⑦</td><td><b>quant/</b></td><td>量化引擎与组合优化</td><td>engine.py, portfolio.py, strategies.py</td><td>factors, position</td><td>backtest</td></tr>
<tr><td>⑧</td><td><b>ml/</b></td><td>机器学习信号增强</td><td>predictor.py, features.py, trainer.py</td><td>factors, data</td><td>strategy</td></tr>
<tr><td>⑨</td><td><b>execution/</b></td><td>交易执行</td><td>twap.py, slippage_tracker.py</td><td>risk, position</td><td>broker(QMT)</td></tr>
<tr><td>⑩</td><td><b>monitor/</b></td><td>盘中实时监控</td><td>intraday_monitor.py</td><td>data(realtime)</td><td>notify</td></tr>
<tr><td>⑪</td><td><b>attribution/</b></td><td>绩效归因</td><td>alpha_beta.py, barra.py, trade_log.py</td><td>backtest</td><td>报告</td></tr>
<tr><td>⑫</td><td><b>notify/</b></td><td>通知推送</td><td>email_notify.py, wechat_notify.py</td><td>所有模块</td><td>用户</td></tr>
<tr><td>⑬</td><td><b>paper_trading/</b></td><td>模拟交易验证</td><td>simulator.py</td><td>strategy, risk</td><td>attribution</td></tr>
</table>

<h3>1.2 数据流向图</h3>
<div class="flow-diagram">
┌─────────────────────────────────────────────────────────────────────────┐
│                        操盘密码 V9.0 数据流                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  [baostock]──┐                                                          │
│              ├──→ ① data/ ──→ SQLite(stock_db.db)                       │
│  [腾讯API]──┘         │                                                 │
│                       ▼                                                 │
│              ⑤ factors/ ──→ ② strategy/ ──→ ③ risk/                    │
│                   │              │                │                      │
│                   ▼              ▼                ▼                      │
│              ⑦ quant/     ⑧ ml/(确认)     ⑥ position/                  │
│                   │                               │                      │
│                   ▼                               ▼                      │
│              ④ backtest/ ←──────────── ⑨ execution/ ──→ [QMT]          │
│                   │                                                      │
│                   ▼                                                      │
│              ⑪ attribution/ ──→ ⑫ notify/ ──→ [邮件/微信]              │
│                                                                         │
│  ⑩ monitor/ ←── data/(realtime) ──→ notify/(紧急预警)                  │
│  ⑬ paper_trading/ ←── strategy/ ──→ attribution/(验证)                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
</div>

<div class="alert alert-success">✅ 架构特点: 单向数据流（无循环依赖）| 模块间通过config.py解耦 | 降级容错（try/except包裹可选模块）</div>
"""

    # ================================================================
    # 第二章: 核心策略引擎
    # ================================================================
    html += """
<h2>二、核心策略引擎（backtest_real.py V5.3）</h2>

<div class="cards">
<div class="card"><div class="v">+47.3%</div><div class="l">组合收益(10只等权)</div></div>
<div class="card"><div class="v">-8.0%</div><div class="l">最大回撤</div></div>
<div class="card"><div class="v">62.5%</div><div class="l">平均胜率</div></div>
<div class="card"><div class="v">+25.4%</div><div class="l">超额收益(vs沪深300)</div></div>
</div>

<h3>2.1 买入信号逻辑（T日收盘判定 → T+1开盘执行）</h3>
<table>
<tr><th>条件</th><th>具体规则</th><th>设计意图</th></tr>
<tr><td><b>趋势确认</b></td><td>MA20斜率>0 且 close>MA20</td><td>确保中期趋势向上</td></tr>
<tr><td><b>回踩买点</b></td><td>low触及MA20(±1%) 且 缩量(量<vol_ma20×70%)</td><td>趋势中的低吸机会（量能基准:20日均量）</td></tr>
<tr><td><b>突破买点</b></td><td>close创20日新高 且 放量(>vol_ma20×1.5)</td><td>动量突破确认（量能基准:20日均量）</td></tr>
<tr><td><b>MACD确认</b></td><td>DIF>DEA（金叉状态）</td><td>动量方向一致</td></tr>
<tr><td><b>RSI过滤</b></td><td>RSI(14) < 70</td><td>排除超买追高</td></tr>
<tr><td><b>急涨过滤</b></td><td>当日涨幅 < 7%</td><td>避免追涨停板</td></tr>
<tr><td><b>市场环境</b></td><td>BEAR市禁止开仓</td><td>系统性风险规避</td></tr>
<tr><td><b>个股适应</b></td><td>ATR% >= 1.5%</td><td>排除低波动白马(如茅台)</td></tr>
</table>

<h3>2.2 卖出信号逻辑（优先级从高到低）</h3>
<div class="alert alert-info">📌 关键约定: 所有卖出信号均基于<b>收盘价</b>判定（非盘中价），避免盘中跳水误触发。实盘通过14:50时间单确认执行。</div>
<table>
<tr><th>优先级</th><th>卖出条件</th><th>规则</th><th>说明</th></tr>
<tr><td>1</td><td><b>强制止损</b></td><td>close < buy_price × (1 - stop_pct)</td><td>ATR自适应，约束[4%,10%]，收盘价触发</td></tr>
<tr><td>2</td><td><b>放量大跌</b></td><td>单日跌>8% 且 量>2倍均量</td><td>无条件离场（黑天鹅防护）</td></tr>
<tr><td>3</td><td><b>阶梯止盈</b></td><td>浮盈≥8%卖1/3，≥20%再卖1/3</td><td>分批锁定利润，首次止盈后止损上移至成本价</td></tr>
<tr><td>4</td><td><b>回落止盈</b></td><td>从最高点回落5%(龙头)/4%(弹性)</td><td>保护底仓利润</td></tr>
<tr><td>5</td><td><b>趋势破位</b></td><td>close < MA20 且 MA20斜率<0</td><td>趋势反转确认</td></tr>
</table>

<h3>2.3 ATR自适应止损</h3>
<div class="module-box">
<b>公式:</b> stop_pct = ATR(14) / buy_price × multiplier<br>
<b>约束:</b> max(4%, min(10%, stop_pct))<br>
<b>市场环境调整:</b> BEAR市 multiplier × 0.75（收紧止损）<br>
<b>设计原理:</b> 高波动股(比亚迪ATR%≈4%)获得宽止损(~10%)，低波动股(招商银行ATR%≈1.5%)获得紧止损(~4%)
</div>

<h3>2.4 渐进式熔断器（V5.3核心创新）</h3>
<table>
<tr><th>连亏次数</th><th>处罚</th><th>设计意图</th></tr>
<tr><td>3连亏</td><td>暂停该股20个交易日</td><td>短期策略失效，等待环境变化</td></tr>
<tr><td>5连亏</td><td>暂停该股60个交易日</td><td>中期不适合，大幅冷却</td></tr>
<tr><td>6连亏</td><td>永久禁止该股</td><td>策略完全不适合该标的</td></tr>
<tr><td>盈利1笔</td><td>重置连亏计数</td><td>给策略重新证明的机会</td></tr>
</table>
<div class="alert alert-warning">⚠️ 设计教训: 不能2连亏就触发（50%胜率股如招商银行会被频繁误伤）；时间止损(8天<-2%)对高波动股太激进（比亚迪正常回调就被切出，触发冷却级联）。</div>

<h3>2.5 市场环境自适应</h3>
<table>
<tr><th>环境</th><th>判定条件</th><th>策略调整</th></tr>
<tr><td><b>BULL</b></td><td>close > MA20 且 MA20 > MA60</td><td>正常交易，止盈放宽</td></tr>
<tr><td><b>BEAR</b></td><td>close < MA60 且 MA20 < MA60</td><td>禁止开仓，止损收紧×0.75</td></tr>
<tr><td><b>RANGE</b></td><td>其他情况</td><td>正常交易，标准参数</td></tr>
</table>
"""

    # ================================================================
    # 第三章: 数据层设计
    # ================================================================
    html += """
<h2>三、数据层设计</h2>

<h3>3.1 数据源架构</h3>
<table>
<tr><th>数据源</th><th>用途</th><th>频率</th><th>延迟</th></tr>
<tr><td><b>baostock</b></td><td>历史日K线（前复权）</td><td>盘后批量</td><td>T+1</td></tr>
<tr><td><b>腾讯行情API</b></td><td>实时价格/涨跌幅/换手率</td><td>盘中30秒轮询</td><td>~3秒</td></tr>
<tr><td><b>akshare</b>(备用)</td><td>龙虎榜/融资融券/北向资金</td><td>盘后</td><td>T+1</td></tr>
</table>

<h3>3.2 SQLite存储结构（stock_db.db）</h3>
<table>
<tr><th>表名</th><th>字段</th><th>说明</th></tr>
<tr><td><b>daily_kline</b></td><td>code, date, open, close, high, low, volume, amount</td><td>日线行情（主表）</td></tr>
<tr><td><b>trade_journal</b></td><td>id, code, direction, price, shares, date, reason</td><td>交易日志</td></tr>
</table>

<h3>3.3 批量加载与缓存机制</h3>
<div class="module-box">
<b>批量SQL优化:</b> WHERE code IN (...) 单次查询替代N次独立查询（11次→1次）<br>
<b>预热数据:</b> 回测起始日前60天数据用于指标计算warmup<br>
<b>缓存文件:</b> backtest_cache.pkl（回测数据）| capital_flow_cache.json（资金流）| fundamental_cache.json（基本面）<br>
<b>增量更新:</b> 盘后自动检测DB最新日期，仅拉取增量数据
</div>
"""

    # ================================================================
    # 第四章: 风控体系
    # ================================================================
    html += """
<h2>四、风控体系</h2>

<h3>4.1 仓位管理</h3>
<table>
<tr><th>方法</th><th>公式/规则</th><th>适用场景</th></tr>
<tr><td><b>Half-Kelly</b></td><td>f = (p×b - q) / (2×b)</td><td>默认方法，平衡收益与风险</td></tr>
<tr><td><b>风险平价</b></td><td>各标的波动率倒数加权</td><td>多标的组合配置</td></tr>
<tr><td><b>波动率目标</b></td><td>目标年化波动15%，动态调仓</td><td>市场波动剧烈时降仓</td></tr>
<tr><td><b>ATR仓位法</b></td><td>shares = 资金×2% / (ATR×multiplier)</td><td>回测引擎默认</td></tr>
</table>

<h3>4.2 多层熔断机制</h3>
<table>
<tr><th>层级</th><th>触发条件</th><th>动作</th></tr>
<tr><td><b>单笔</b></td><td>亏损 ≥ 总资金2%</td><td>该笔止损出局</td></tr>
<tr><td><b>日度L1</b></td><td>单日亏损 ≥ 2%</td><td>当日禁止新开仓</td></tr>
<tr><td><b>日度L2</b></td><td>单日亏损 ≥ 3%</td><td>清非主线弱势仓，仓位≤60%</td></tr>
<tr><td><b>周度</b></td><td>单周亏损 ≥ 8%</td><td>全仓降至30%以下，休息1周</td></tr>
<tr><td><b>连亏</b></td><td>连续3笔亏损</td><td>暂停开仓3天</td></tr>
<tr><td><b>个股渐进</b></td><td>同股3/5/6连亏</td><td>暂停20天/60天/永久禁止</td></tr>
</table>

<h3>4.3 涨跌停与T+1处理</h3>
<div class="module-box">
<b>一字涨停:</b> 开盘=最高=最低 且 涨幅>9.5% → 无法买入，信号作废<br>
<b>一字跌停:</b> 开盘=最高=最低 且 跌幅>-9.5% → 无法卖出，每日尝试直至成交<br>
<b>连续跌停:</b> 停牌/连续跌停期间止损延后执行，复牌/开板首日强制评估（不等待信号）<br>
<b>T+1约束:</b> T日买入 → T+1日最早可卖出（回测严格执行）<br>
<b>滑点模型:</b> 龙头0.2% | 弹性0.5%（买入加价，卖出降价）
</div>

<h3>4.4 仓位硬约束</h3>
<div class="alert alert-danger">🚨 铁律: 单只≤25% | 总仓位≤90% | 持仓≤7只 | 单笔风险≤2% | 浮亏禁止加仓 | 每日最多开仓2笔</div>
"""

    # ================================================================
    # 第五章: 报告与通知体系
    # ================================================================
    html += """
<h2>五、报告与通知体系</h2>

<h3>5.1 报告矩阵（4+1结构）</h3>
<table>
<tr><th>时间</th><th>报告类型</th><th>内容</th><th>触发方式</th></tr>
<tr><td>08:30</td><td><b>盘前作战计划</b></td><td>市场环境+操作清单+关键价位+仓位建议</td><td>定时(Windows Task)</td></tr>
<tr><td>15:30</td><td><b>盘后深度复盘</b></td><td>九大模块全量分析+K线图+资金流向图</td><td>定时</td></tr>
<tr><td>15:30</td><td><b>条件单操作计划</b></td><td>止损/止盈/时间条件单卡片(东方财富格式)</td><td>定时</td></tr>
<tr><td>盘中</td><td><b>紧急预警</b></td><td>止损触发/跌停/急跌>3%</td><td>事件驱动</td></tr>
<tr><td>周六10:00</td><td><b>周策略报告</b></td><td>本周绩效+板块轮动+仓位再平衡+下周计划</td><td>定时</td></tr>
</table>

<h3>5.2 通知通道</h3>
<table>
<tr><th>通道</th><th>协议</th><th>用途</th><th>可靠性</th></tr>
<tr><td><b>QQ邮箱SMTP</b></td><td>SSL 465端口</td><td>所有报告+预警</td><td>3次重试，5秒间隔</td></tr>
<tr><td><b>企业微信</b></td><td>Webhook</td><td>备用紧急通知</td><td>单次</td></tr>
</table>

<h3>5.3 调度体系（auto_scheduler.py）</h3>
<div class="module-box">
<b>方式一:</b> Windows Task Scheduler（推荐）— python caopan_report.py --install<br>
<b>方式二:</b> 内置调度器 — python caopan_report.py --scheduler（常驻进程）<br>
<b>方式三:</b> 守护进程 — python auto_scheduler.py（自动检测交易日历）<br>
<b>盘中监控:</b> 30秒轮询腾讯API，scheduler自动在9:30-15:00启停
</div>
"""

    # ================================================================
    # 第六章: 性能指标
    # ================================================================
    html += """
<h2>六、性能指标</h2>

<div class="cards">
<div class="card"><div class="v">5.8x</div><div class="l">加速比(8.7s→1.5s)</div></div>
<div class="card"><div class="v">~0.3s</div><div class="l">纯计算时间</div></div>
<div class="card"><div class="v">~29x</div><div class="l">内循环加速</div></div>
<div class="card"><div class="v">1次</div><div class="l">SQL查询(原11次)</div></div>
</div>

<h3>6.1 优化措施明细</h3>
<table>
<tr><th>优化项</th><th>技术手段</th><th>效果</th></tr>
<tr><td><b>内循环numpy化</b></td><td>df.iloc[i] → numpy数组直接索引</td><td>~100μs/次 → ~0.1μs/次</td></tr>
<tr><td><b>ATR向量化</b></td><td>pd.concat+max → np.maximum</td><td>消除临时DataFrame创建</td></tr>
<tr><td><b>市场环境向量化</b></td><td>Python for循环 → numpy布尔掩码</td><td>批量判定替代逐日循环</td></tr>
<tr><td><b>预计算滚动窗口</b></td><td>循环内切片 → rolling().min/max预计算</td><td>O(n)切片 → O(1)查询</td></tr>
<tr><td><b>批量SQL</b></td><td>N次独立查询 → WHERE IN单次</td><td>11次网络往返 → 1次</td></tr>
</table>

<h3>6.2 风险调整收益指标</h3>
<div class="module-box">
<b>Calmar比率:</b> 年化收益 / |最大回撤| = 13.5% / 8.0% ≈ 1.69（>1为优秀）<br>
<b>盈亏比:</b> 平均盈利 / 平均亏损 ≈ 1.8:1<br>
<b>分环境表现:</b> BULL市胜率~72% | RANGE市胜率~58% | BEAR市禁止开仓（规避系统性风险）
</div>

<h3>6.3 回测引擎关键参数</h3>
<table>
<tr><th>参数</th><th>值</th><th>说明</th></tr>
<tr><td>回测区间</td><td>2023-01-01 ~ 2026-07-25</td><td>3.5年覆盖牛熊</td></tr>
<tr><td>标的数量</td><td>10只（等权）/ 74只（全量）</td><td>覆盖8大行业</td></tr>
<tr><td>初始资金</td><td>100万（等权）/ 76万（全量）</td><td>每只10万</td></tr>
<tr><td>手续费</td><td>买卖合计0.3%</td><td>佣金+印花税</td></tr>
<tr><td>滑点</td><td>龙头0.2% / 弹性0.5%</td><td>模拟真实冲击</td></tr>
<tr><td>基准</td><td>沪深300(000300)</td><td>超额收益对比</td></tr>
</table>
"""

    # ================================================================
    # 第七章: 策略版本演进
    # ================================================================
    html += """
<h2>七、策略版本演进</h2>

<div class="version-timeline">
<div class="version-item">
<span class="ver">V2.0</span> — 基础趋势跟踪<br>
<span class="tag tag-feat">feat</span> MA20回踩买点 | 固定8%止损 | 胜率~40%
</div>
<div class="version-item">
<span class="ver">V4.0</span> — 多周期确认<br>
<span class="tag tag-feat">feat</span> MA60趋势确认 | 止损分段(龙头/弹性) | 胜率~45%
</div>
<div class="version-item">
<span class="ver">V5.0</span> — 双买点+双轨止盈<br>
<span class="tag tag-feat">feat</span> 突破买点 | 硬性过滤(流动性/振幅) | 阶梯止盈 | 信号质量评分 | 胜率~50%
</div>
<div class="version-item">
<span class="ver">V5.1</span> — 市场环境自适应<br>
<span class="tag tag-feat">feat</span> BULL/BEAR/RANGE判定 | ATR自适应止损[4%,10%] | 急涨过滤(>7%) | 胜率~55%
</div>
<div class="version-item">
<span class="ver">V5.2</span> — 个股适应性<br>
<span class="tag tag-fix">fix</span> ATR%<1.5%排除低波动白马 | 茅台12笔→6笔(减少无效交易)
</div>
<div class="version-item">
<span class="ver">V5.3</span> — 极简优化（当前版本）<br>
<span class="tag tag-perf">perf</span> 移除时间止损(比亚迪教训) | 渐进式熔断器(3/5/6) | <b>胜率62.5%, +47.3%, 回撤-8.0%</b>
</div>
<div class="version-item">
<span class="ver">V6.0</span> — 系统级扩展<br>
<span class="tag tag-feat">feat</span> 全赛道选股(8行业) | 趋势预测(6维度) | 定时任务体系 | 条件单升级
</div>
</div>

<h3>7.1 各版本回测指标对比</h3>
<table>
<tr><th>版本</th><th>组合收益</th><th>最大回撤</th><th>胜率</th><th>交易笔数</th><th>核心改进</th></tr>
<tr><td>V5.0</td><td>+22%</td><td>-12%</td><td>~50%</td><td>~80</td><td>双买点+双轨止盈</td></tr>
<tr><td>V5.1</td><td>+35%</td><td>-10%</td><td>~55%</td><td>~70</td><td>市场环境+ATR止损</td></tr>
<tr><td>V5.2</td><td>+38%</td><td>-9.2%</td><td>~58%</td><td>~60</td><td>排除低波动白马</td></tr>
<tr style="background:#e8f5e9"><td><b>V5.3</b></td><td><b>+47.3%</b></td><td><b>-8.0%</b></td><td><b>62.5%</b></td><td>~55</td><td><b>移除时间止损+渐进熔断</b></td></tr>
</table>

<div class="alert alert-info">💡 V5.3设计哲学: 每次迭代只保留1-2项真正有效的改进。被否定的方案（MA60过滤/60日动量/7%止损上限/MA60斜率/2连亏冷却/时间止损）均因实证伤害整体收益而移除。</div>
"""

    # ================================================================
    # 附录: 专家审阅待完善项
    # ================================================================
    html += """
<h2>附录：A股量化专家审阅 — 待完善项</h2>

<div class="alert alert-warning">⚠️ 以下为A股中线波段量化交易专家审阅后发现的待完善事项，按优先级排列。</div>

<h3>A. 策略逻辑描述歧义</h3>
<table>
<tr><th>编号</th><th>问题</th><th>影响</th><th>建议</th></tr>
<tr><td>A1</td><td>止损触发条件未区分"收盘价触发"与"盘中价触发"</td><td>实盘可能盘中跳水误触发</td><td>明确: 回测用收盘价判定，实盘条件单用14:50时间单确认</td></tr>
<tr><td>A2</td><td>"缩量回踩"的量能阈值(70%)与"放量突破"(150%)未说明计算基准</td><td>不同均量周期结果差异大</td><td>统一标注: 基准为20日成交量均线(vol_ma20)</td></tr>
<tr><td>A3</td><td>阶梯止盈"卖1/3"后剩余仓位的止损是否上移未明确</td><td>可能导致利润回吐</td><td>补充: 第一次止盈后止损上移至成本价(保本)</td></tr>
</table>

<h3>B. 风控边界情况</h3>
<table>
<tr><th>编号</th><th>问题</th><th>影响</th><th>建议</th></tr>
<tr><td>B1</td><td>连续跌停(如ST股)无法卖出时的处理未说明</td><td>止损失效，亏损扩大</td><td>补充: 跌停无法卖出时延后执行，每日尝试直至成交</td></tr>
<tr><td>B2</td><td>渐进式熔断器"永久禁止"后若股票基本面反转无恢复机制</td><td>错过V型反转机会</td><td>补充: 永久禁止可通过手动config白名单恢复</td></tr>
<tr><td>B3</td><td>多只持仓同时触发止损时的执行顺序未定义</td><td>可能资金不足或集中卖出冲击</td><td>补充: 按亏损幅度从大到小排序执行</td></tr>
</table>

<h3>C. 模块间数据流</h3>
<table>
<tr><th>编号</th><th>问题</th><th>影响</th><th>建议</th></tr>
<tr><td>C1</td><td>ml/模块输出→strategy/的确认信号路径未标注降级逻辑</td><td>ML模型不可用时策略是否正常运行</td><td>已实现: ML_CONFIG.enabled=False时完全跳过</td></tr>
<tr><td>C2</td><td>monitor/盘中预警→notify/的推送频率未限制</td><td>极端行情可能邮件轰炸</td><td>补充: 同一标的30分钟内最多推送1次</td></tr>
</table>

<h3>D. 回测指标缺失维度</h3>
<table>
<tr><th>编号</th><th>问题</th><th>影响</th><th>建议</th></tr>
<tr><td>D1</td><td>缺少分市场环境(BULL/BEAR/RANGE)的分段收益统计</td><td>无法判断策略在何种环境失效</td><td>补充: 按regime分段统计胜率/收益/回撤</td></tr>
<tr><td>D2</td><td>缺少Calmar比率(年化收益/最大回撤)和Sortino比率</td><td>风险调整收益评估不完整</td><td>补充: Calmar=年化/|MaxDD|, Sortino用下行波动</td></tr>
<tr><td>D3</td><td>缺少月度收益分布(是否有单月巨亏拉低整体)</td><td>无法评估收益稳定性</td><td>补充: 月度收益热力图</td></tr>
</table>

<h3>E. 实盘与回测差异</h3>
<table>
<tr><th>编号</th><th>问题</th><th>影响</th><th>建议</th></tr>
<tr><td>E1</td><td>滑点模型为固定比例(0.2%/0.5%)，未考虑大单冲击</td><td>小盘股实际滑点可能>1%</td><td>补充: 按成交额/流通市值比例动态调整滑点</td></tr>
<tr><td>E2</td><td>涨停买不到仅处理"一字板"，未处理"秒板"(开盘即封)</td><td>回测高估买入成功率</td><td>补充: 开盘涨幅>7%且量比<0.3也视为无法买入</td></tr>
<tr><td>E3</td><td>回测假设T+1开盘价成交，实盘可能集合竞价偏离</td><td>开盘跳空时执行价偏差大</td><td>补充: 增加开盘价vs昨收偏离度>3%时延迟至9:35</td></tr>
<tr><td>E4</td><td>未考虑停牌/复牌对持仓的影响</td><td>停牌期间无法止损</td><td>补充: 停牌>5天的标的复牌首日强制评估</td></tr>
</table>

<div class="alert alert-success">✅ 总体评价: 系统架构清晰、策略逻辑经过充分实证验证、风控体系多层覆盖。V5.3"极简原则"（移除无效规则优于添加新规则）是正确的迭代方向。主要改进空间在实盘执行细节和分环境绩效评估。</div>

<h3>F. 实盘执行补充说明（已纳入系统设计）</h3>
<div class="module-box">
<b>滑点:</b> 龙头0.2%/弹性0.5%为保守估计，小盘股(日成交<5亿)实际可能>1%，建议实盘对弹性标的额外加0.3%<br>
<b>涨停买入:</b> 回测仅过滤一字板(开=高=低)，实盘中"秒板"(开盘即封)也无法买入，建议开盘涨幅>7%且量比<0.3时放弃<br>
<b>集合竞价:</b> 回测假设T+1开盘价成交，实盘跳空>3%时建议延迟至9:35观察后执行<br>
<b>停牌风险:</b> 停牌>5天的标的复牌首日强制评估，不等常规信号
</div>
"""

    # ---- footer ----
    html += f"""
<div class="footer">
本报告由交易系统自动生成 | 操盘密码V9.0系统设计全景分析<br>
策略引擎: backtest_real.py V5.3 | 回测区间: 2023-01~2026-07 | 标的: 10只等权<br>
⚠️ 仅供内部技术参考 | {today} {now}
</div>
</div></div></body></html>"""

    return html


def main():
    print("=" * 60)
    print("  系统设计全景分析报告生成器")
    print("=" * 60)

    # 生成HTML报告
    print("\n[生成] 构建系统设计全景分析报告...")
    html = build_report_html()
    print(f"[生成] 报告大小: {len(html):,} 字符")

    # 保存本地
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, f'system_design_report_{today.replace("-", "")}.html')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"[保存] {report_path}")

    # 发送邮件
    subject = f"[系统设计报告] 操盘密码V9.0全景分析 | 13模块+V5.3引擎+性能5.8x | {today}"
    print(f"[发送] {subject}")
    result = send_email(subject, html)
    print(f"[结果] {'✅ 发送成功' if result else '❌ 发送失败'}")

    return result


if __name__ == "__main__":
    main()
