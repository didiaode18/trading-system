# -*- coding: utf-8 -*-
"""补充运行 Walk-Forward + 样本外验证（V5结果已从上一轮获取）"""
import sys, os, time, json, sqlite3, warnings, traceback, datetime, logging
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading_system'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

INITIAL_CAPITAL = 500_000
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("wf_holdout")

def load_all_data():
    conn = sqlite3.connect(config.DB_PATH)
    df_all = pd.read_sql("SELECT code, date, open, close, high, low, volume FROM daily_kline ORDER BY code, date ASC", conn)
    conn.close()
    data_dict = {}
    for code, group in df_all.groupby("code"):
        df = group.copy()
        for col in ["open", "close", "high", "low", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0].reset_index(drop=True)
        df = df[df["date"] >= "2022-10-01"].reset_index(drop=True)
        if len(df) > 100:
            data_dict[code] = df
    return data_dict

def run_walk_forward(data_dict):
    from backtest.walk_forward import WalkForwardAnalyzer
    stock_codes = [c for c in data_dict.keys() if c != "000300"][:20]
    sub_data = {c: data_dict[c] for c in stock_codes}
    if "000300" in data_dict:
        sub_data["000300"] = data_dict["000300"]
    wf = WalkForwardAnalyzer(
        train_days=120, test_days=40,
        param_grid={"initial_stop_loss": [0.06, 0.07, 0.08],
                    "min_signal_quality": [55, 60, 65],
                    "drawdown_leader": [0.05, 0.06, 0.07]},
        initial_capital=INITIAL_CAPITAL, max_windows=15)
    t0 = time.time()
    result = wf.run(sub_data, stock_codes)
    result["elapsed_sec"] = round(time.time() - t0, 1)
    return result

def run_holdout(data_dict):
    from backtest_real import backtest_stock_v5, analyze_trades
    holdout_days = 120
    stock_codes = [c for c in data_dict.keys() if c != "000300"]
    holdout_trades = []
    for code in stock_codes:
        df = data_dict[code]
        if len(df) <= holdout_days + 80:
            continue
        df_h = df.iloc[-(holdout_days + 80):].reset_index(drop=True)
        info = config.get_stock_info(code)
        info_dict = {"名称": info.get("名称", code), "类型": info.get("类型", "龙头"), "行业": info.get("赛道", "其他")}
        try:
            trades = backtest_stock_v5(df_h, code, info_dict)
            cutoff = df.iloc[-holdout_days]["date"]
            trades = [t for t in trades if t.get("buy_date", "") >= cutoff]
            holdout_trades.extend(trades)
        except Exception:
            pass
    if not holdout_trades:
        return {"error": "样本外无交易"}
    stats = analyze_trades(holdout_trades)
    hs = data_dict[stock_codes[0]].iloc[-holdout_days]["date"]
    he = data_dict[stock_codes[0]].iloc[-1]["date"]
    return {"stats": stats, "trades_count": len(holdout_trades),
            "period": f"{hs} ~ {he}", "holdout_days": holdout_days}

if __name__ == "__main__":
    logger.info("加载数据...")
    data_dict = load_all_data()
    logger.info(f"加载 {len(data_dict)} 只股票")

    logger.info("运行 Walk-Forward...")
    wf = run_walk_forward(data_dict)
    logger.info(f"Walk-Forward完成: 窗口{wf.get('num_windows',0)}, "
                f"样本外夏普{wf.get('oos_sharpe',0):.2f}, "
                f"过拟合{wf.get('overfit_ratio',0):.1%}")

    logger.info("运行样本外验证...")
    ho = run_holdout(data_dict)
    if "error" not in ho:
        hs = ho.get("stats", {})
        logger.info(f"样本外: {ho.get('trades_count',0)}笔, "
                    f"胜率{hs.get('win_rate',0)}%, 盈亏比{hs.get('profit_factor',0)}")

    # 保存
    result = {"walk_forward": {k: v for k, v in wf.items() if isinstance(v, (int, float, str, bool, type(None), list, dict))},
              "holdout": {"trades_count": ho.get("trades_count", 0), "period": ho.get("period", ""),
                          "stats": {k: v for k, v in ho.get("stats", {}).items() if isinstance(v, (int, float, str, bool, type(None), list, dict))} if "error" not in ho else ho}}
    out_path = os.path.join(config.OUTPUT_DIR, f"wf_holdout_{datetime.date.today().strftime('%Y%m%d')}.json")
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"结果保存: {out_path}")
    print("\n=== Walk-Forward ===")
    print(f"窗口数: {wf.get('num_windows', 0)}")
    print(f"样本外夏普: {wf.get('oos_sharpe', 0):.2f}")
    print(f"参数稳定性: {wf.get('stability_score', 0):.0%}")
    print(f"过拟合程度: {wf.get('overfit_ratio', 0):.1%}")
    print(f"判定: {wf.get('verdict', 'N/A')}")
    print(f"\n=== 样本外验证 ===")
    if "error" not in ho:
        hs = ho.get("stats", {})
        print(f"区间: {ho.get('period', 'N/A')}")
        print(f"交易: {ho.get('trades_count', 0)}笔")
        print(f"胜率: {hs.get('win_rate', 0)}%")
        print(f"盈亏比: {hs.get('profit_factor', 0)}")
        print(f"每笔期望: {hs.get('expectancy', 0):+.2f}%")
    else:
        print(f"失败: {ho.get('error')}")
