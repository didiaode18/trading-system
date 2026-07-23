"""
高胜率A股交易操作系统 V7.0
=========================

定位: 中线波段(3天-4周) · 条件单驱动 · 纪律自动化
数据源: baostock(历史K线) + 腾讯行情API(实时价格)
输出: 邮件报告 + 条件单JSON + QMT自动执行

模块架构:
    data/        数据层 - 行情获取(baostock/akshare/腾讯实时)
    strategy/    策略层 - 趋势分析/五层选股/条件单生成/反洗盘
    risk/        风控层 - 熔断/仓位限制/强制卖出
    output/      输出层 - 条件单Excel/日报/周报/晨间简报
    notify/      通知层 - 邮件(QQ SMTP)/企业微信
    backtest/    回测层 - 策略回测/蒙特卡洛/滚动优化
    factors/     因子层 - 动量/技术/量能/复合因子
    position/    仓位层 - Kelly/风险平价/波动率目标
    quant/       量化层 - 多因子引擎/组合优化/绩效归因
    ml/          机器学习 - 特征工程/预测/监控
    execution/   执行层 - TWAP/滑点追踪
    monitor/     监控层 - 盘中实时预警
    attribution/ 归因层 - Alpha/Beta/Barra

根目录脚本:
    run.py                    统一CLI入口
    daily_orders.py           每日条件单生成器
    generate_holdings_report.py  盘后综合分析报告
    qmt_trader.py             QMT自动交易执行器
    auto_scheduler.py         自动化调度守护进程
    setup.py                  环境初始化
"""

__version__ = "7.0.0"
__author__ = "Trading System"
