"""
参数扫描与实验台账
==================
V10 P1-3: 替代根目录散落的手工 smoke 脚本。

核心功能:
    - 网格搜索 / 随机搜索
    - joblib 并行
    - 实验台账持久化（SQLite experiments 表）
    - experiments_report() 输出 top-N 与参数敏感性

参考范式: vectorbt 向量化思路、mlflow 实验管理

用法:
    from backtest.param_sweep import ParamSweeper
    sweeper = ParamSweeper(db_path="experiments.db")
    results = sweeper.grid_search(
        base_params={"atr_multiplier": 2.0, "stop_loss": 0.05},
        param_grid={"atr_multiplier": [1.5, 2.0, 2.5], "stop_loss": [0.03, 0.05, 0.08]},
        backtest_fn=run_backtest,
    )
    report = sweeper.experiments_report(top_n=10)
"""

import hashlib
import itertools
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# joblib 为可选依赖
try:
    from joblib import Parallel, delayed
    HAS_JOBLIB = True
except ImportError:
    HAS_JOBLIB = False


@dataclass
class ExperimentResult:
    """单次实验结果"""
    experiment_id: str = ""
    params: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    code_version: str = ""
    duration_seconds: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    status: str = "completed"  # completed / failed
    error: str = ""


class ParamSweeper:
    """参数扫描引擎

    支持网格搜索和随机搜索，结果持久化到 SQLite。

    参数:
        db_path: 实验台账数据库路径
    """

    def __init__(self, db_path: str = None):
        self._db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "experiments.db"
        )
        self._conn = sqlite3.connect(self._db_path)
        self._init_tables()

    def _init_tables(self):
        """创建实验台账表"""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS experiments (
                experiment_id TEXT PRIMARY KEY,
                params_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                code_version TEXT DEFAULT '',
                duration_seconds REAL DEFAULT 0,
                status TEXT DEFAULT 'completed',
                error TEXT DEFAULT '',
                created_at TEXT
            )
        """)
        self._conn.commit()

    def _get_code_version(self) -> str:
        """获取当前代码版本（git hash 或时间戳）"""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return datetime.now().strftime("%Y%m%d%H%M%S")

    def _make_experiment_id(self, params: dict) -> str:
        """根据参数生成唯一实验ID"""
        param_str = json.dumps(params, sort_keys=True)
        return hashlib.md5(param_str.encode()).hexdigest()[:12]

    def grid_search(self, base_params: dict, param_grid: dict,
                    backtest_fn: Callable, max_workers: int = 1,
                    **fn_kwargs) -> list[ExperimentResult]:
        """网格搜索

        参数:
            base_params: 基础参数
            param_grid: 参数网格 {param_name: [value1, value2, ...]}
            backtest_fn: 回测函数 fn(params, **fn_kwargs) -> metrics_dict
            max_workers: 并行度
            **fn_kwargs: 传递给回测函数的额外参数

        返回:
            实验结果列表
        """
        # 生成参数组合
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        combinations = list(itertools.product(*param_values))

        all_params = []
        for combo in combinations:
            params = dict(base_params)
            for name, value in zip(param_names, combo):
                params[name] = value
            all_params.append(params)

        logger.info(
            f"[ParamSweep] 网格搜索: {len(all_params)} 组合, "
            f"并行度={max_workers}"
        )

        # 执行
        if max_workers > 1 and HAS_JOBLIB:
            results = Parallel(n_jobs=max_workers)(
                delayed(self._run_single)(params, backtest_fn, fn_kwargs)
                for params in all_params
            )
        else:
            results = [
                self._run_single(params, backtest_fn, fn_kwargs)
                for params in all_params
            ]

        # 持久化
        for r in results:
            self._persist_result(r)

        return results

    def random_search(self, base_params: dict, param_ranges: dict,
                      backtest_fn: Callable, n_iter: int = 50,
                      max_workers: int = 1, seed: int = 42,
                      **fn_kwargs) -> list[ExperimentResult]:
        """随机搜索

        参数:
            base_params: 基础参数
            param_ranges: 参数范围 {param_name: (min, max)} 或 {param_name: [choices]}
            backtest_fn: 回测函数
            n_iter: 迭代次数
            max_workers: 并行度
            seed: 随机种子
            **fn_kwargs: 传递给回测函数的额外参数

        返回:
            实验结果列表
        """
        rng = np.random.RandomState(seed)
        all_params = []

        for _ in range(n_iter):
            params = dict(base_params)
            for name, range_spec in param_ranges.items():
                if isinstance(range_spec, (list, tuple)) and len(range_spec) == 2:
                    # 连续范围 (min, max)
                    if isinstance(range_spec[0], int) and isinstance(range_spec[1], int):
                        params[name] = int(rng.randint(range_spec[0], range_spec[1]))
                    else:
                        params[name] = float(rng.uniform(range_spec[0], range_spec[1]))
                elif isinstance(range_spec, list):
                    # 离散选择
                    params[name] = rng.choice(range_spec)
            all_params.append(params)

        logger.info(f"[ParamSweep] 随机搜索: {n_iter} 次迭代")

        if max_workers > 1 and HAS_JOBLIB:
            results = Parallel(n_jobs=max_workers)(
                delayed(self._run_single)(params, backtest_fn, fn_kwargs)
                for params in all_params
            )
        else:
            results = [
                self._run_single(params, backtest_fn, fn_kwargs)
                for params in all_params
            ]

        for r in results:
            self._persist_result(r)

        return results

    def _run_single(self, params: dict, backtest_fn: Callable,
                    fn_kwargs: dict) -> ExperimentResult:
        """执行单次实验"""
        exp_id = self._make_experiment_id(params)
        code_version = self._get_code_version()
        start_time = time.time()

        try:
            metrics = backtest_fn(params, **fn_kwargs)
            duration = time.time() - start_time

            if not isinstance(metrics, dict):
                metrics = {"result": str(metrics)}

            return ExperimentResult(
                experiment_id=exp_id,
                params=params,
                metrics=metrics,
                code_version=code_version,
                duration_seconds=round(duration, 2),
                status="completed",
            )
        except Exception as e:
            duration = time.time() - start_time
            logger.warning(f"[ParamSweep] 实验 {exp_id} 失败: {e}")
            return ExperimentResult(
                experiment_id=exp_id,
                params=params,
                metrics={},
                code_version=code_version,
                duration_seconds=round(duration, 2),
                status="failed",
                error=str(e),
            )

    def _persist_result(self, result: ExperimentResult):
        """持久化实验结果"""
        self._conn.execute(
            "INSERT OR REPLACE INTO experiments "
            "(experiment_id, params_json, metrics_json, code_version, "
            "duration_seconds, status, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (result.experiment_id,
             json.dumps(result.params, ensure_ascii=False),
             json.dumps(result.metrics, ensure_ascii=False),
             result.code_version,
             result.duration_seconds,
             result.status,
             result.error,
             result.created_at)
        )
        self._conn.commit()

    def experiments_report(self, top_n: int = 10,
                           sort_by: str = "return") -> pd.DataFrame:
        """生成实验报告

        参数:
            top_n: 返回 top N 结果
            sort_by: 排序指标名称

        返回:
            DataFrame: 实验结果表（按指定指标排序）
        """
        df = pd.read_sql_query(
            "SELECT * FROM experiments WHERE status='completed' ORDER BY created_at DESC",
            self._conn
        )
        if df.empty:
            return df

        # 解析 JSON 列
        df["params"] = df["params_json"].apply(json.loads)
        df["metrics"] = df["metrics_json"].apply(json.loads)

        # 展开 metrics 为列
        metrics_df = pd.json_normalize(df["metrics"])
        result_df = pd.concat([df[["experiment_id", "params", "duration_seconds", "created_at"]],
                               metrics_df], axis=1)

        # 展开 params 为列
        param_cols = {}
        for _, row in result_df.iterrows():
            for k, v in row["params"].items():
                param_cols.setdefault(k, []).append(v)
        for k, v in param_cols.items():
            result_df[f"param_{k}"] = v

        # 排序
        if sort_by in result_df.columns:
            result_df = result_df.sort_values(sort_by, ascending=False)

        return result_df.head(top_n).reset_index(drop=True)

    def sensitivity_analysis(self, param_name: str,
                             metric_name: str) -> pd.DataFrame:
        """参数敏感性分析

        参数:
            param_name: 参数名
            metric_name: 指标名

        返回:
            DataFrame: {param_value, mean_metric, std_metric, count}
        """
        df = pd.read_sql_query(
            "SELECT params_json, metrics_json FROM experiments WHERE status='completed'",
            self._conn
        )
        if df.empty:
            return pd.DataFrame()

        rows = []
        for _, row in df.iterrows():
            params = json.loads(row["params_json"])
            metrics = json.loads(row["metrics_json"])
            if param_name in params and metric_name in metrics:
                rows.append({
                    "param_value": params[param_name],
                    "metric_value": metrics[metric_name],
                })

        if not rows:
            return pd.DataFrame()

        sdf = pd.DataFrame(rows)
        return sdf.groupby("param_value")["metric_value"].agg(
            ["mean", "std", "count"]
        ).reset_index().sort_values("param_value")

    def close(self):
        if self._conn:
            self._conn.close()
