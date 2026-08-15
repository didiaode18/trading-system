"""
IC监控模块
==========
因子有效性监控：
- IC（信息系数）: 因子值与未来收益的秩相关
- IR（信息比率）: IC均值/IC标准差
- 衰减预警: 连续N天IC<阈值 → 自动降权
- 分层回测: 按因子值分5组验证单调性
"""

import os
import json
import logging
import pandas as pd
import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)

# 默认持久化路径
DEFAULT_IC_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'ic_history.json'
)

# IC快照待结算队列路径（CANSLIM cohort落盘，仿 DEFAULT_IC_HISTORY_PATH 拼法）
DEFAULT_IC_PENDING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'ic_pending.json'
)

# pending队列最多保留的cohort数量
IC_PENDING_MAX_COHORTS = 25


class ICMonitor:
    """
    因子IC监控器
    
    用法:
        monitor = ICMonitor()
        ic = monitor.calc_ic(factor_values, forward_returns)
        monitor.update(factor_name, ic)
        if monitor.is_decaying(factor_name):
            print(f"{factor_name} IC衰减，建议降权")
    """

    def __init__(self, decay_threshold: float = 0.02, decay_days: int = 5,
                 history_path: str = None):
        """
        参数:
            decay_threshold: IC衰减阈值（|IC|<此值视为无效）
            decay_days: 连续多少天低于阈值触发预警
            history_path: IC历史JSON持久化路径（None=默认路径）
        """
        self.decay_threshold = decay_threshold
        self.decay_days = decay_days
        self.history_path = history_path or DEFAULT_IC_HISTORY_PATH
        # ic_records: {factor_name: [{"date": "2026-07-24", "ic": 0.05, "ir": 1.2}, ...]}
        self.ic_records: dict[str, list] = {}
        # 初始化时自动加载历史数据
        self.load()

    def calc_ic(self, factor_values: pd.Series, forward_returns: pd.Series) -> float:
        """
        计算单期IC（Spearman秩相关）
        
        参数:
            factor_values: 因子值（截面数据，多只股票同一时点）
            forward_returns: 未来N日收益率
        
        返回:
            IC值 (-1 ~ 1)
        """
        # 去除NaN
        valid = factor_values.notna() & forward_returns.notna()
        if valid.sum() < 5:
            return 0.0

        f = factor_values[valid]
        r = forward_returns[valid]

        # Spearman秩相关
        ic, _ = stats.spearmanr(f, r)
        return ic if not np.isnan(ic) else 0.0

    def calc_ic_series(self, factor_df: pd.DataFrame, returns_df: pd.DataFrame,
                       factor_name: str) -> pd.Series:
        """
        计算时间序列IC（逐日截面IC）
        
        参数:
            factor_df: 因子面板数据 (index=date, columns=stocks)
            returns_df: 收益率面板数据
            factor_name: 因子名
        
        返回:
            IC时间序列
        """
        ic_series = []
        dates = factor_df.index.intersection(returns_df.index)

        for date in dates:
            f = factor_df.loc[date]
            r = returns_df.loc[date]
            ic = self.calc_ic(f, r)
            ic_series.append({"date": date, "ic": ic})

        return pd.DataFrame(ic_series).set_index("date")["ic"]

    def update(self, factor_name: str, ic_value: float, date: str = None):
        """更新因子IC记录并自动持久化
        
        参数:
            factor_name: 因子名称
            ic_value: 当期IC值
            date: 日期字符串(YYYY-MM-DD)，默认当天
        """
        if factor_name not in self.ic_records:
            self.ic_records[factor_name] = []
        if date is None:
            import datetime
            date = datetime.date.today().strftime('%Y-%m-%d')
        # 计算当前IR（滚动）
        records = self.ic_records[factor_name]
        all_ics = [r['ic'] if isinstance(r, dict) else r for r in records] + [ic_value]
        ir = self._calc_ir(all_ics)
        self.ic_records[factor_name].append({
            'date': date, 'ic': round(ic_value, 6), 'ir': round(ir, 4)
        })
        # 自动持久化
        self.save()

    @staticmethod
    def _calc_ir(ic_list: list) -> float:
        """计算IR = IC均值/IC标准差"""
        if len(ic_list) < 2:
            return 0.0
        arr = np.array(ic_list, dtype=float)
        std = arr.std()
        return float(arr.mean() / std) if std > 0 else 0.0

    def get_ic_stats(self, factor_name: str) -> dict:
        """获取因子IC统计"""
        records = self.ic_records.get(factor_name, [])
        if not records:
            return {"ic_mean": 0, "ic_std": 0, "ir": 0, "ic_positive_ratio": 0}

        arr = np.array([r['ic'] if isinstance(r, dict) else r for r in records])
        ic_mean = arr.mean()
        ic_std = arr.std()
        ir = ic_mean / ic_std if ic_std > 0 else 0
        positive_ratio = (arr > 0).sum() / len(arr)

        return {
            "ic_mean": round(ic_mean, 4),
            "ic_std": round(ic_std, 4),
            "ir": round(ir, 4),
            "ic_positive_ratio": round(positive_ratio, 4),
            "sample_size": len(records),
        }

    def is_decaying(self, factor_name: str) -> bool:
        """判断因子是否IC衰减（连续N天|IC|<阈值）"""
        records = self.ic_records.get(factor_name, [])
        if len(records) < self.decay_days:
            return False
        recent = records[-self.decay_days:]
        return all(
            abs(r['ic'] if isinstance(r, dict) else r) < self.decay_threshold
            for r in recent
        )

    def is_negative(self, factor_name: str, threshold: float = -0.02) -> bool:
        """判断因子是否IC持续为负（V3.2新增: 因子反向，应禁用）
        
        回测诊断: S因子IC=-0.28%, P因子IC=-0.42%, 连续为负意味因子已完全失效
        """
        records = self.ic_records.get(factor_name, [])
        if len(records) < self.decay_days:
            return False
        recent = records[-self.decay_days:]
        return all(
            (r['ic'] if isinstance(r, dict) else r) < threshold
            for r in recent
        )

    def get_negative_factors(self) -> list:
        """获取所有IC持续为负的因子（V3.2新增）"""
        return [name for name in self.ic_records if self.is_negative(name)]

    def is_strong(self, factor_name: str, threshold: float = 0.05) -> bool:
        """判断因子是否IC强劲（连续N天|IC|>阈值）"""
        records = self.ic_records.get(factor_name, [])
        if len(records) < self.decay_days:
            return False
        recent = records[-self.decay_days:]
        return all(
            abs(r['ic'] if isinstance(r, dict) else r) > threshold
            for r in recent
        )

    def get_decaying_factors(self) -> list:
        """获取所有衰减因子"""
        return [name for name in self.ic_records if self.is_decaying(name)]

    def get_strong_factors(self, threshold: float = 0.05) -> list:
        """获取所有强劲因子"""
        return [name for name in self.ic_records if self.is_strong(name, threshold)]

    def rank_factors(self) -> list:
        """按IR排序所有因子"""
        ranked = []
        for name in self.ic_records:
            stats_dict = self.get_ic_stats(name)
            ranked.append((name, stats_dict["ir"], stats_dict["ic_mean"]))
        ranked.sort(key=lambda x: abs(x[1]), reverse=True)
        return ranked

    # ============================================================
    # 持久化
    # ============================================================

    def save(self, path: str = None):
        """将IC历史序列化为JSON文件"""
        save_path = path or self.history_path
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(self.ic_records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"IC历史保存失败({save_path}): {e}")

    def load(self, path: str = None):
        """从JSON文件恢复IC历史"""
        load_path = path or self.history_path
        try:
            if os.path.exists(load_path):
                with open(load_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.ic_records = data
                    logger.info(f"IC历史已加载: {len(data)}个因子, "
                               f"共{sum(len(v) for v in data.values())}条记录")
                else:
                    logger.warning(f"IC历史文件格式异常，已忽略")
        except Exception as e:
            logger.warning(f"IC历史加载失败({load_path}): {e}")
            self.ic_records = {}

    def layer_backtest(self, factor_values: pd.Series, forward_returns: pd.Series,
                       n_layers: int = 5) -> dict:
        """
        分层回测：按因子值分N组，验证单调性
        
        返回:
            {"layer_returns": [各层平均收益], "monotonicity": 单调性评分}
        """
        valid = factor_values.notna() & forward_returns.notna()
        if valid.sum() < n_layers * 3:
            return {"layer_returns": [], "monotonicity": 0}

        f = factor_values[valid]
        r = forward_returns[valid]

        # 分层
        labels = pd.qcut(f, n_layers, labels=False, duplicates="drop")
        layer_returns = []
        for i in range(n_layers):
            mask = labels == i
            if mask.sum() > 0:
                layer_returns.append(r[mask].mean())
            else:
                layer_returns.append(0)

        # 单调性：相邻层收益差的方向一致性
        diffs = np.diff(layer_returns)
        if len(diffs) > 0:
            positive_ratio = (diffs > 0).sum() / len(diffs)
            monotonicity = max(positive_ratio, 1 - positive_ratio)  # 0.5~1
        else:
            monotonicity = 0.5

        return {
            "layer_returns": [round(r, 4) for r in layer_returns],
            "monotonicity": round(monotonicity, 4),
            "top_minus_bottom": round(layer_returns[-1] - layer_returns[0], 4),
        }


# ============================================================
# 模块级函数：IC快照落盘 与 待结算队列结算（批1-B公共模块）
# ============================================================

def save_ic_snapshot(date: str, cohort: list, pending_path: str = None) -> bool:
    """
    将某日选股cohort快照落盘到 ic_pending.json，供后续前瞻结算使用

    参数:
        date: cohort日期 (YYYY-MM-DD)
        cohort: 元素为 {"code": str, "factors": {factor_name: value}, "total_score": float}
                （CANSLIM 五因子 N/S/L/CAI/P + 总分）
        pending_path: 落盘路径（None=默认 data/ic_pending.json）

    文件结构: {"cohorts": [{"date": ..., "records": [...]}, ...]}
    同date已存在则覆盖该cohort；只保留最近25个cohort（按date排序截断）

    返回: 成功True，任何异常均返回False不抛出
    """
    path = pending_path or DEFAULT_IC_PENDING_PATH
    try:
        if not date or not isinstance(cohort, list):
            return False

        # 读取已有结构
        cohorts = []
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                if isinstance(existing, dict) and isinstance(existing.get("cohorts"), list):
                    cohorts = existing["cohorts"]
        except Exception:
            cohorts = []

        # 同date覆盖
        cohorts = [c for c in cohorts
                   if isinstance(c, dict) and c.get("date") != date]
        cohorts.append({"date": date, "records": cohort})

        # 按date排序，只保留最近25个cohort
        cohorts.sort(key=lambda c: str(c.get("date", "")))
        cohorts = cohorts[-IC_PENDING_MAX_COHORTS:]

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({"cohorts": cohorts}, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning(f"IC快照落盘失败({path}): {e}")
        return False


def settle_ic_pending(today: str, load_close_fn, forward_days: int = 20,
                      pending_path: str = None, history_path: str = None) -> list:
    """
    结算 ic_pending.json 中已到期的cohort，计算真实前瞻收益并写入IC历史

    到期判定: 使用"日历日 >= forward_days"近似判断（不做交易日历换算，
              交易日口径偏差由调用方容忍），即 (today - cohort.date) 日历天数 >= forward_days

    参数:
        today: 当前日期 (YYYY-MM-DD)
        load_close_fn: 调用方注入的函数 load_close_fn(code)，返回带日期的收盘价序列
                       [(date_str, close), ...] 升序（date_str为YYYY-MM-DD），
                       拿不到数据返回None。本函数内部按cohort基准日切片：
                       取序列中首个 >= cohort日期的交易日收盘价为基准
        forward_days: 前瞻窗口天数（默认20）
        pending_path: ic_pending.json路径（None=默认）
        history_path: ic_history.json路径（None=默认；冒烟测试务必传临时路径）

    流程:
        1. 扫描到期cohort（date <= today - forward_days 日历日）
        2. 对每个cohort内股票按基准日切片计算真实前瞻收益:
           基准=序列中首个 >= cohort日期的交易日收盘（无精确匹配取最近后一个
           交易日；基准找不到则该股票跳过），末日收盘为终点，
           fwd = (末日收盘 - 基准收盘) / 基准收盘
        3. 有效样本>=5时，对每个因子分别算 Spearman Rank IC，写入 ic_history.json
           （沿用 ICMonitor 持久化结构与每键60条截断逻辑由调用侧/ICMonitor保证；
           本函数对IR做除零与裁剪保护: ir = mean/max(std, 1e-6), clip(-10, 10)，
           因 ICMonitor.update 内部IR计算无小std保护，写入后在本层修正最后一条记录的ir）
        4. 已结算cohort从pending文件移除

    返回: 已结算列表 [{"date", "factor_count", "sample_size"}]，异常时返回空列表不抛出
    """
    path = pending_path or DEFAULT_IC_PENDING_PATH
    settled = []
    try:
        if not today or not callable(load_close_fn):
            return []
        if not os.path.exists(path):
            return []

        from datetime import datetime as _dt
        today_dt = _dt.strptime(today, "%Y-%m-%d")

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        cohorts = data.get("cohorts", []) if isinstance(data, dict) else []

        due_cohorts = []
        remain_cohorts = []
        for cohort in cohorts:
            if not isinstance(cohort, dict) or not cohort.get("date"):
                continue
            try:
                cohort_dt = _dt.strptime(cohort["date"], "%Y-%m-%d")
            except Exception:
                remain_cohorts.append(cohort)
                continue
            # 日历日近似判断到期
            if (today_dt - cohort_dt).days >= forward_days:
                due_cohorts.append(cohort)
            else:
                remain_cohorts.append(cohort)

        if not due_cohorts:
            return []

        monitor = ICMonitor(history_path=history_path)

        for cohort in due_cohorts:
            try:
                records = cohort.get("records", [])
                codes = []
                fwd_returns = []
                for rec in records:
                    if not isinstance(rec, dict):
                        continue
                    code = rec.get("code")
                    if not code:
                        continue
                    try:
                        series = load_close_fn(code)
                    except Exception:
                        series = None
                    if series is None:
                        continue
                    # 契约: [(date_str, close), ...] 升序带日期元组列表
                    try:
                        pairs = [(str(d)[:10], float(c)) for d, c in series]
                    except Exception:
                        continue
                    if not pairs:
                        continue
                    pairs.sort(key=lambda p: p[0])
                    # 按cohort基准日切片: 首个 >= cohort日期 的交易日为基准
                    # （无精确匹配取最近后一个交易日；基准找不到则跳过该股票）
                    base_idx = next((i for i, (d, _c) in enumerate(pairs)
                                     if d >= cohort["date"]), None)
                    if base_idx is None:
                        continue
                    sliced = pairs[base_idx:]
                    if len(sliced) < 2 or sliced[0][1] <= 0:
                        continue
                    # 基准日收盘=切片首日收盘；末日收盘为终点，真实前瞻收益
                    fwd = (sliced[-1][1] - sliced[0][1]) / sliced[0][1]
                    codes.append(code)
                    fwd_returns.append(fwd)

                sample_size = len(codes)
                if sample_size < 5:
                    # 样本不足，跳过结算（仍从pending移除，避免永久滞留）
                    settled.append({"date": cohort["date"],
                                    "factor_count": 0,
                                    "sample_size": sample_size})
                    continue

                fwd_series = pd.Series(fwd_returns, index=codes)

                # V4.0(G2): 推荐胜率统计落盘（cohort级前瞻收益摘要，供选股报告反馈展示）
                _save_cohort_performance(cohort["date"], fwd_series,
                                         history_path=history_path)

                # 汇总各因子截面值
                factor_names = set()
                for rec in records:
                    if isinstance(rec, dict) and isinstance(rec.get("factors"), dict):
                        factor_names.update(rec["factors"].keys())

                factor_count = 0
                for fname in sorted(factor_names):
                    fvals = {}
                    for rec in records:
                        if not isinstance(rec, dict):
                            continue
                        c = rec.get("code")
                        if c not in fwd_series.index:
                            continue
                        v = (rec.get("factors") or {}).get(fname)
                        if v is not None:
                            try:
                                fvals[c] = float(v)
                            except (TypeError, ValueError):
                                pass
                    if len(fvals) < 5:
                        continue
                    fv = pd.Series(fvals)
                    common = fv.index.intersection(fwd_series.index)
                    if len(common) < 5:
                        continue
                    try:
                        ic, _ = stats.spearmanr(fv[common], fwd_series[common])
                    except Exception:
                        continue
                    if ic is None or np.isnan(ic):
                        continue
                    # 沿用ICMonitor持久化结构写入
                    monitor.update(fname, float(ic), date=today)
                    # IR除零保护+裁剪（ICMonitor.update内部无小std保护，本层修正）
                    try:
                        rec_list = monitor.ic_records.get(fname, [])
                        if rec_list:
                            ics = [r['ic'] if isinstance(r, dict) else r for r in rec_list]
                            arr = np.array(ics, dtype=float)
                            ir = float(arr.mean() / max(float(arr.std()), 1e-6))
                            ir = max(-10.0, min(10.0, ir))
                            rec_list[-1]['ir'] = round(ir, 4)
                            # 每键60条截断（ICMonitor.update本身不截断，本层保证）
                            monitor.ic_records[fname] = rec_list[-60:]
                            monitor.save()
                    except Exception:
                        pass
                    factor_count += 1

                settled.append({"date": cohort["date"],
                                "factor_count": factor_count,
                                "sample_size": sample_size})
            except Exception as e:
                logger.warning(f"IC cohort结算失败({cohort.get('date')}): {e}")
                continue

        # 已结算cohort从pending移除，回写剩余
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({"cohorts": remain_cohorts}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"IC pending回写失败({path}): {e}")

        return settled
    except Exception as e:
        logger.warning(f"settle_ic_pending异常: {e}")
        return settled if isinstance(settled, list) else []


# ============================================================
# V4.0 (G2): 推荐cohort胜率统计（选股有效性反馈闭环）
# ============================================================

DEFAULT_PERF_PATH = os.path.join(os.path.dirname(DEFAULT_IC_PENDING_PATH),
                                 "cohort_performance.json")


def _perf_path(history_path: str = None) -> str:
    """胜率统计文件路径：默认与ic_history同目录，冒烟测试可随history_path隔离"""
    if history_path:
        return os.path.join(os.path.dirname(os.path.abspath(history_path)),
                            "cohort_performance.json")
    return DEFAULT_PERF_PATH


def _save_cohort_performance(date: str, fwd_series: "pd.Series",
                             history_path: str = None) -> bool:
    """落盘单个cohort的前瞻收益摘要（同日覆盖，最多保留60期）"""
    try:
        if fwd_series is None or len(fwd_series) == 0:
            return False
        path = _perf_path(history_path)
        entries = []
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    _d = json.load(f)
                if isinstance(_d, list):
                    entries = _d
        except Exception:
            entries = []
        rets = fwd_series.astype(float)
        entry = {
            "date": date,
            "sample": int(len(rets)),
            "win_rate": round(float((rets > 0).mean()), 4),
            "avg_ret": round(float(rets.mean()), 4),
            "median_ret": round(float(rets.median()), 4),
            "per_code": {c: round(float(v), 4) for c, v in rets.items()},
        }
        entries = [e for e in entries if isinstance(e, dict) and e.get("date") != date]
        entries.append(entry)
        entries.sort(key=lambda e: str(e.get("date", "")))
        entries = entries[-60:]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning(f"cohort胜率统计落盘失败({date}): {e}")
        return False


def load_cohort_performance_stats(history_path: str = None) -> dict:
    """
    汇总已结算cohort的推荐胜率统计（供选股报告展示历史推荐表现）

    返回:
        {"settled_cohorts": int, "total_samples": int,
         "win_rate": float, "avg_ret": float, "latest": list(最近3期)}
        无数据时返回 {"settled_cohorts": 0}
    """
    try:
        path = _perf_path(history_path)
        if not os.path.exists(path):
            return {"settled_cohorts": 0}
        with open(path, 'r', encoding='utf-8') as f:
            entries = json.load(f)
        if not isinstance(entries, list) or not entries:
            return {"settled_cohorts": 0}
        entries = [e for e in entries if isinstance(e, dict) and e.get("sample", 0) > 0]
        if not entries:
            return {"settled_cohorts": 0}
        total_samples = sum(e["sample"] for e in entries)
        # 按样本数加权的胜率/均收益
        win_rate = sum(e["win_rate"] * e["sample"] for e in entries) / total_samples
        avg_ret = sum(e["avg_ret"] * e["sample"] for e in entries) / total_samples
        return {
            "settled_cohorts": len(entries),
            "total_samples": total_samples,
            "win_rate": round(win_rate, 4),
            "avg_ret": round(avg_ret, 4),
            "latest": entries[-3:],
        }
    except Exception as e:
        logger.warning(f"cohort胜率统计读取失败: {e}")
        return {"settled_cohorts": 0}
