"""
高胜率A股交易操作系统 V10.0
=========================

定位: 中线波段(3天-4周) · 条件单驱动 · 纪律自动化
数据源: baostock(历史K线) + 腾讯行情API(实时价格+盘口五档)
输出: 邮件报告 + 条件单JSON + QMT自动执行

V10.0新增: 大盘分析模块拆分(market_analysis) | 盘中监控拆分(intraday_patterns/market_gate) |
          因子体系统一(quant→factors) | 预警参数自优化 | 均值回归策略 |
          ETF轮动 | 财报深度分析 | 护城河指标 | 宏观先行指标 | DAG任务编排

模块架构:
    data/        数据层 - 行情获取(baostock/akshare/腾讯实时)
    strategy/    策略层 - 趋势分析/五层选股/条件单生成/反洗盘/大盘分析/盘中形态/大盘门控
    risk/        风控层 - 熔断/仓位限制/强制卖出
    output/      输出层 - 条件单/日报/周报/晨间简报/条件单邮件
    notify/      通知层 - 邮件(QQ SMTP)/企业微信
    backtest/    回测层 - 策略回测/蒙特卡洛/滚动优化
    factors/     因子层 - 动量/技术/量能/复合因子
    position/    仓位层 - Kelly/风险平价/波动率目标
    quant/       量化层 - 多因子引擎/组合优化/绩效归因
    ml/          机器学习 - 特征工程/预测/监控
    execution/   执行层 - 滑点追踪
    monitor/     监控层 - 盘中实时预警
    attribution/ 归因层 - Alpha/Beta/Barra

根目录脚本:
    run.py                    统一CLI入口
    daily_orders.py           每日条件单生成器
    generate_holdings_report.py  向后兼容垫片（实际逻辑在 output/generate_holdings_report.py）
    qmt_trader.py             QMT自动交易执行器
    auto_scheduler.py         自动化调度守护进程
    setup.py                  环境初始化
"""

# P0-7: 统一路径配置——确保 trading_system/ 在 sys.path 中，
# 子模块可直接 import config，无需各自 sys.path.insert
import os as _os
import sys as _sys
_pkg_dir = _os.path.dirname(_os.path.abspath(__file__))
if _pkg_dir not in _sys.path:
    _sys.path.insert(0, _pkg_dir)
del _os, _sys, _pkg_dir

__version__ = "10.0.0"
SYSTEM_VERSION = "V10.0"  # 报告/邮件中统一引用的版本标识
__author__ = "Trading System"
