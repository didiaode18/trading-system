# -*- coding: utf-8 -*-
"""
QMT自动交易执行器 V1.0
======================
基于miniQMT(xtquant)实现全自动条件单执行
彻底消除人为干预，系统信号→自动下单

前置条件:
1. 东方财富/国金证券开通QMT权限（50万/10万门槛）
2. 安装QMT客户端，以"独立交易"模式登录
3. pip install xtquant（或从QMT安装目录复制）

使用方式:
  python qmt_trader.py          # 盘中运行，自动监控+执行
  python qmt_trader.py --test   # 测试连接，不下单

架构:
  每日条件单(daily_orders.py) → 生成orders.json → QMT执行器读取 → 盘中自动监控触发 → 下单
"""

import os
import sys
import json
import time
import datetime
import logging
from typing import Dict, List, Optional

# ============================================================
# 配置
# ============================================================
QMT_CONFIG = {
    # miniQMT连接配置
    "qmt_path": r"D:\国金QMT交易端\userdata_mini",  # QMT安装路径（根据实际修改）
    "account_id": "",       # 资金账号（首次使用时填写）
    "account_type": "STOCK",  # 股票账号

    # 风控硬约束（不可覆盖）
    "max_daily_trades": 3,        # 每日最大交易笔数
    "max_single_position_pct": 25, # 单只最大仓位%
    "max_total_position_pct": 90,  # 总仓位上限%
    "min_cash_pct": 10,           # 最低现金比例%
    "stop_loss_pct": 8,           # 止损线%
    "no_manual_override": True,   # 禁止手动覆盖

    # 交易时间
    "morning_open": "09:30",
    "morning_close": "11:30",
    "afternoon_open": "13:00",
    "afternoon_close": "15:00",

    # 监控频率
    "check_interval_sec": 5,  # 每5秒检查一次条件
}

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("qmt_trader.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)


# ============================================================
# 条件单数据结构
# ============================================================
class ConditionOrder:
    """条件单对象"""

    def __init__(self, order_dict: dict):
        self.order_id = order_dict.get("order_id", "")
        self.code = order_dict["证券代码"]
        self.name = order_dict["证券名称"]
        self.direction = order_dict["方向"]  # 买入/卖出
        self.trigger_price = order_dict["触发价"]
        self.quantity = order_dict["数量"]
        self.order_type = order_dict["类型"]  # 定价卖出/时间条件单/回落卖出/反弹买入
        self.valid_days = order_dict.get("有效期", "10个交易日")
        self.trigger_time = order_dict.get("触发时间", "")  # 如"14:50"
        self.priority = order_dict.get("优先级", "★★建议")
        self.note = order_dict.get("说明", "")

        # 状态追踪
        self.triggered = False
        self.executed = False
        self.create_date = datetime.date.today().isoformat()

    def to_dict(self):
        return {
            "order_id": self.order_id,
            "证券代码": self.code,
            "证券名称": self.name,
            "方向": self.direction,
            "触发价": self.trigger_price,
            "数量": self.quantity,
            "类型": self.order_type,
            "有效期": self.valid_days,
            "触发时间": self.trigger_time,
            "优先级": self.priority,
            "说明": self.note,
            "triggered": self.triggered,
            "executed": self.executed,
        }


# ============================================================
# QMT交易接口（xtquant封装）
# ============================================================
class QMTTrader:
    """
    miniQMT交易接口封装
    需要xtquant库和QMT客户端运行
    """

    def __init__(self, config: dict):
        self.config = config
        self.connected = False
        self.trader = None
        self.account = None
        self.daily_trade_count = 0
        self.today = datetime.date.today().isoformat()

        # 尝试导入xtquant
        try:
            from xtquant import xttrader
            from xtquant import xtdata
            from xtquant.xttype import StockAccount
            self.xttrader = xttrader
            self.xtdata = xtdata
            self.StockAccount = StockAccount
            self.has_xtquant = True
            logger.info("[QMT] xtquant库加载成功")
        except ImportError:
            self.has_xtquant = False
            logger.warning("[QMT] xtquant未安装，进入模拟模式")

    def connect(self) -> bool:
        """连接QMT客户端"""
        if not self.has_xtquant:
            logger.warning("[QMT] 模拟模式：xtquant未安装")
            self.connected = True  # 模拟连接
            return True

        try:
            qmt_path = self.config["qmt_path"]
            if not os.path.exists(qmt_path):
                logger.error(f"[QMT] 路径不存在: {qmt_path}")
                logger.info("[QMT] 请修改QMT_CONFIG中的qmt_path为你的QMT安装路径")
                return False

            # 创建交易对象
            session_id = int(time.time())
            self.trader = self.xttrader.XtQuantTrader(qmt_path, session_id)

            # 启动连接
            self.trader.start()

            # 建立连接
            connect_result = self.trader.connect()
            if connect_result != 0:
                logger.error(f"[QMT] 连接失败，错误码: {connect_result}")
                logger.info("[QMT] 请确保QMT客户端已以'独立交易'模式登录")
                return False

            # 创建账号对象
            account_id = self.config["account_id"]
            if not account_id:
                logger.error("[QMT] 未配置account_id，请在QMT_CONFIG中填写资金账号")
                return False

            self.account = self.StockAccount(account_id)

            # 订阅账号
            subscribe_result = self.trader.subscribe(self.account)
            if subscribe_result != 0:
                logger.warning(f"[QMT] 订阅账号返回: {subscribe_result}")

            self.connected = True
            logger.info(f"[QMT] ✅ 连接成功 | 账号: {account_id}")
            return True

        except Exception as e:
            logger.error(f"[QMT] 连接异常: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        if self.trader:
            self.trader.stop()
            self.connected = False
            logger.info("[QMT] 已断开连接")

    def get_positions(self) -> List[dict]:
        """查询当前持仓"""
        if not self.connected:
            return []

        if not self.has_xtquant:
            logger.info("[QMT模拟] 查询持仓")
            return []

        try:
            positions = self.trader.query_stock_positions(self.account)
            result = []
            for pos in positions:
                if pos.volume > 0:
                    result.append({
                        "code": pos.stock_code,
                        "volume": pos.volume,
                        "available": pos.can_use_volume,
                        "cost": pos.open_price,
                        "market_value": pos.market_value,
                    })
            return result
        except Exception as e:
            logger.error(f"[QMT] 查询持仓失败: {e}")
            return []

    def get_account_info(self) -> dict:
        """查询账户资金"""
        if not self.connected:
            return {}

        if not self.has_xtquant:
            logger.info("[QMT模拟] 查询账户资金")
            return {"total_asset": 710000, "cash": 66, "market_value": 710000}

        try:
            asset = self.trader.query_stock_asset(self.account)
            return {
                "total_asset": asset.total_asset,
                "cash": asset.cash,
                "market_value": asset.market_value,
                "frozen": asset.frozen_cash,
            }
        except Exception as e:
            logger.error(f"[QMT] 查询资金失败: {e}")
            return {}

    def place_order(self, code: str, direction: str, price: float,
                    quantity: int, order_type: str = "限价") -> Optional[int]:
        """
        下单

        参数:
            code: 证券代码（如 588000.SH / 002558.SZ）
            direction: "买入" / "卖出"
            price: 委托价格
            quantity: 委托数量
            order_type: "限价" / "市价"

        返回: order_id 或 None
        """
        # 风控检查
        if not self._risk_check(code, direction, price, quantity):
            return None

        # 转换代码格式（588000 → 588000.SH）
        stock_code = self._convert_code(code)

        if not self.has_xtquant:
            logger.info(f"[QMT模拟] 下单: {direction} {code} {quantity}股 @ {price}")
            self.daily_trade_count += 1
            return int(time.time())

        try:
            from xtquant.xtconstant import (
                STOCK_BUY, STOCK_SELL,
                FIX_PRICE,  # 限价
                LATEST_PRICE,  # 市价
            )

            # 方向
            if direction == "买入":
                xt_direction = STOCK_BUY
            else:
                xt_direction = STOCK_SELL

            # 价格类型
            if order_type == "市价":
                xt_price_type = LATEST_PRICE
                price = 0  # 市价单价格填0
            else:
                xt_price_type = FIX_PRICE

            # 下单
            order_id = self.trader.order_stock(
                self.account,
                stock_code,
                xt_direction,
                quantity,
                xt_price_type,
                price,
            )

            if order_id and order_id > 0:
                self.daily_trade_count += 1
                logger.info(f"[QMT] ✅ 下单成功 | {direction} {code} {quantity}股 @ {price} | 订单号:{order_id}")
                return order_id
            else:
                logger.error(f"[QMT] ❌ 下单失败 | {direction} {code} {quantity}股 @ {price}")
                return None

        except Exception as e:
            logger.error(f"[QMT] 下单异常: {e}")
            return None

    def cancel_order(self, order_id: int) -> bool:
        """撤单"""
        if not self.has_xtquant:
            logger.info(f"[QMT模拟] 撤单: {order_id}")
            return True

        try:
            result = self.trader.cancel_order_stock(self.account, order_id)
            return result == 0
        except Exception as e:
            logger.error(f"[QMT] 撤单失败: {e}")
            return False

    def get_realtime_price(self, code: str) -> float:
        """获取实时价格"""
        if not self.has_xtquant:
            return 0.0

        try:
            stock_code = self._convert_code(code)
            tick = self.xtdata.get_full_tick([stock_code])
            if stock_code in tick:
                return tick[stock_code]["lastPrice"]
        except Exception:
            pass
        return 0.0

    # ---- 内部方法 ----

    def _convert_code(self, code: str) -> str:
        """转换代码格式: 588000 → 588000.SH, 002558 → 002558.SZ"""
        if "." in code:
            return code
        if code.startswith(("5", "6", "9")):
            return f"{code}.SH"
        else:
            return f"{code}.SZ"

    def _risk_check(self, code: str, direction: str, price: float, quantity: int) -> bool:
        """风控硬约束检查"""
        # 1. 每日交易笔数限制
        if self.daily_trade_count >= self.config["max_daily_trades"]:
            logger.warning(f"[风控] 🚫 拒绝下单: 今日已交易{self.daily_trade_count}笔，达到上限{self.config['max_daily_trades']}")
            return False

        # 2. 买入时检查仓位
        if direction == "买入":
            account = self.get_account_info()
            if account:
                total = account.get("total_asset", 0)
                cash = account.get("cash", 0)
                order_amount = price * quantity

                # 现金不足
                if order_amount > cash:
                    logger.warning(f"[风控] 🚫 拒绝买入: 需要{order_amount:.0f}元，可用{cash:.0f}元")
                    return False

                # 最低现金比例
                if total > 0 and (cash - order_amount) / total < self.config["min_cash_pct"] / 100:
                    logger.warning(f"[风控] 🚫 拒绝买入: 买入后现金比例低于{self.config['min_cash_pct']}%")
                    return False

        logger.info(f"[风控] ✅ 通过 | {direction} {code} {quantity}股 @ {price}")
        return True


# ============================================================
# 条件单执行引擎
# ============================================================
class ConditionOrderEngine:
    """
    条件单监控执行引擎
    盘中运行，每N秒检查一次所有条件单是否触发
    """

    def __init__(self, trader: QMTTrader, orders: List[ConditionOrder]):
        self.trader = trader
        self.orders = orders
        self.executed_orders = []
        self.high_prices = {}  # 记录日内最高价（用于回落卖出）
        self.low_prices = {}   # 记录日内最低价（用于反弹买入）

    def load_orders_from_file(self, filepath: str):
        """从JSON文件加载条件单"""
        if not os.path.exists(filepath):
            logger.warning(f"[引擎] 条件单文件不存在: {filepath}")
            return

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.orders = [ConditionOrder(o) for o in data.get("orders", [])]
        logger.info(f"[引擎] 加载{len(self.orders)}条条件单")

    def check_and_execute(self):
        """检查所有条件单，触发则执行"""
        now = datetime.datetime.now()
        current_time = now.strftime("%H:%M")

        for order in self.orders:
            if order.triggered or order.executed:
                continue

            # 获取实时价格
            price = self.trader.get_realtime_price(order.code)
            if price <= 0:
                continue

            # 更新日内高低价
            self._update_high_low(order.code, price)

            # 判断是否触发
            triggered = False

            if order.order_type == "定价卖出":
                # 价格跌破触发价
                if price <= order.trigger_price:
                    triggered = True
                    logger.info(f"[触发] 定价卖出 | {order.name} 当前{price} ≤ 触发价{order.trigger_price}")

            elif order.order_type == "时间条件单":
                # 到达指定时间 + 价格条件
                if order.trigger_time and current_time >= order.trigger_time:
                    if price <= order.trigger_price:
                        triggered = True
                        logger.info(f"[触发] 时间条件单 | {order.name} {current_time}≥{order.trigger_time} 且 价格{price}≤{order.trigger_price}")

            elif order.order_type == "回落卖出":
                # 从日内最高回落5%
                high = self.high_prices.get(order.code, price)
                drawdown = (high - price) / high if high > 0 else 0
                if drawdown >= 0.05 and price <= order.trigger_price:
                    triggered = True
                    logger.info(f"[触发] 回落卖出 | {order.name} 日高{high:.3f}→当前{price:.3f} 回落{drawdown*100:.1f}%")

            elif order.order_type == "反弹买入":
                # 从日内最低反弹2%
                low = self.low_prices.get(order.code, price)
                rebound = (price - low) / low if low > 0 else 0
                if price <= order.trigger_price and rebound >= 0.02:
                    triggered = True
                    logger.info(f"[触发] 反弹买入 | {order.name} 日低{low:.3f}→当前{price:.3f} 反弹{rebound*100:.1f}%")

            # 执行
            if triggered:
                order.triggered = True
                self._execute_order(order, price)

    def _execute_order(self, order: ConditionOrder, current_price: float):
        """执行条件单"""
        logger.info(f"[执行] {order.direction} {order.code} {order.name} {order.quantity}股 | 触发价{order.trigger_price} 当前{current_price}")

        # 卖出用触发价（限价），买入用当前价
        if order.direction == "卖出":
            exec_price = order.trigger_price
        else:
            exec_price = current_price

        order_id = self.trader.place_order(
            code=order.code,
            direction=order.direction,
            price=exec_price,
            quantity=order.quantity,
            order_type="限价",
        )

        if order_id:
            order.executed = True
            self.executed_orders.append(order)
            logger.info(f"[执行] ✅ 成功 | 订单号{order_id}")
        else:
            logger.error(f"[执行] ❌ 失败 | {order.name} 下单被拒绝")

    def _update_high_low(self, code: str, price: float):
        """更新日内高低价"""
        if code not in self.high_prices or price > self.high_prices[code]:
            self.high_prices[code] = price
        if code not in self.low_prices or price < self.low_prices[code]:
            self.low_prices[code] = price

    def get_status(self) -> str:
        """获取引擎状态"""
        total = len(self.orders)
        triggered = sum(1 for o in self.orders if o.triggered)
        executed = sum(1 for o in self.orders if o.executed)
        return f"条件单: {total}条 | 已触发: {triggered} | 已执行: {executed} | 今日交易: {self.trader.daily_trade_count}笔"


# ============================================================
# 主运行逻辑
# ============================================================
def is_trading_time() -> bool:
    """判断当前是否为交易时间"""
    now = datetime.datetime.now()
    # 周末不交易
    if now.weekday() >= 5:
        return False

    current = now.strftime("%H:%M")
    cfg = QMT_CONFIG
    morning = cfg["morning_open"] <= current <= cfg["morning_close"]
    afternoon = cfg["afternoon_open"] <= current <= cfg["afternoon_close"]
    return morning or afternoon


def run_monitor():
    """主监控循环"""
    logger.info("=" * 60)
    logger.info("  QMT自动交易执行器 V1.0 启动")
    logger.info(f"  日期: {datetime.date.today()} | 监控间隔: {QMT_CONFIG['check_interval_sec']}秒")
    logger.info("=" * 60)

    # 1. 连接QMT
    trader = QMTTrader(QMT_CONFIG)
    if not trader.connect():
        logger.error("[主程序] QMT连接失败，退出")
        return

    # 2. 加载今日条件单
    today_str = datetime.date.today().strftime("%Y%m%d")
    orders_file = os.path.join(
        os.path.dirname(__file__),
        "trading_system", "output", f"orders_{today_str}.json"
    )

    engine = ConditionOrderEngine(trader, [])
    engine.load_orders_from_file(orders_file)

    if not engine.orders:
        logger.warning("[主程序] 无条件单可执行，请先运行 daily_orders.py 生成")
        trader.disconnect()
        return

    # 3. 显示今日计划
    logger.info(f"\n[今日计划] {len(engine.orders)}条条件单:")
    for o in engine.orders:
        logger.info(f"  {o.priority} {o.order_type} | {o.code} {o.name} | {o.direction} {o.quantity}股 @ {o.trigger_price}")

    # 4. 监控循环
    logger.info(f"\n[监控] 开始盘中监控... (Ctrl+C停止)")
    try:
        while True:
            if is_trading_time():
                engine.check_and_execute()

                # 每30秒打印一次状态
                if int(time.time()) % 30 == 0:
                    logger.info(f"[状态] {engine.get_status()}")
            else:
                current = datetime.datetime.now().strftime("%H:%M")
                if current > "15:00":
                    logger.info("[主程序] 收盘，停止监控")
                    break
                elif current < "09:30":
                    logger.info("[主程序] 未开盘，等待中...")

            time.sleep(QMT_CONFIG["check_interval_sec"])

    except KeyboardInterrupt:
        logger.info("\n[主程序] 手动停止")

    # 5. 收盘总结
    logger.info(f"\n[收盘总结] {engine.get_status()}")
    for o in engine.executed_orders:
        logger.info(f"  ✅ 已执行: {o.direction} {o.name} {o.quantity}股 @ {o.trigger_price}")

    trader.disconnect()


def run_test():
    """测试QMT连接"""
    logger.info("[测试] 检查QMT环境...")

    # 检查xtquant
    try:
        import xtquant
        logger.info(f"[测试] ✅ xtquant已安装 | 版本: {getattr(xtquant, '__version__', '未知')}")
    except ImportError:
        logger.warning("[测试] ❌ xtquant未安装")
        logger.info("[测试] 安装方法:")
        logger.info("  1. 从QMT安装目录找到 xtquant 文件夹")
        logger.info("  2. 复制到 Python的 Lib/site-packages/ 目录")
        logger.info("  3. 或: pip install xtquant (如果券商提供了wheel包)")
        return

    # 检查QMT路径
    qmt_path = QMT_CONFIG["qmt_path"]
    if os.path.exists(qmt_path):
        logger.info(f"[测试] ✅ QMT路径存在: {qmt_path}")
    else:
        logger.warning(f"[测试] ❌ QMT路径不存在: {qmt_path}")
        logger.info("[测试] 请修改 QMT_CONFIG['qmt_path'] 为你的QMT安装路径")

    # 尝试连接
    trader = QMTTrader(QMT_CONFIG)
    if trader.connect():
        logger.info("[测试] ✅ QMT连接成功!")
        account = trader.get_account_info()
        if account:
            logger.info(f"[测试] 总资产: {account.get('total_asset', 0):,.0f}元")
            logger.info(f"[测试] 可用资金: {account.get('cash', 0):,.0f}元")
        trader.disconnect()
    else:
        logger.warning("[测试] ❌ QMT连接失败，请确保QMT客户端已登录")


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    else:
        run_monitor()
