# -*- coding: utf-8 -*-
"""
文件 I/O 安全工具
=================
提供原子写入等文件操作工具函数，防止进程中断导致数据文件截断/损坏。

设计原则:
  - 所有 JSON 写入均先写临时文件再 os.replace 原子替换
  - 失败时清理临时文件，不留下半成品
  - 绝不抛异常到调用方（写入失败仅日志警告，返回 False）
"""

import os
import json
import time
import logging

logger = logging.getLogger(__name__)

# FIX(P0-16): 跨进程文件锁——Windows 用 msvcrt.locking，POSIX 用 fcntl.flock
try:
    import msvcrt  # Windows
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False
try:
    import fcntl  # POSIX
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


class FileLock:
    """
    跨进程文件锁（FIX P0-16: holdings.json 多写入方 lost update 防护）

    用法:
        with FileLock(path):        # 锁文件为 path + ".lock"
            data = read(path)
            ...
            write(path, data)

    语义:
      - 阻塞式获取，timeout 秒后放弃并返回未持锁（degraded=True）
      - 锁失败不抛异常（调用方可用 lock.acquired 判断），避免锁机制自身阻断主流程
      - 支持 with 语句，退出时自动释放
    """

    def __init__(self, path: str, timeout: float = 10.0):
        self.lock_path = path + ".lock"
        self.timeout = timeout
        self.acquired = False
        self.degraded = False
        self._fh = None

    def acquire(self) -> bool:
        deadline = time.monotonic() + self.timeout
        try:
            os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
            self._fh = open(self.lock_path, "a+")
            while True:
                try:
                    if _HAS_MSVCRT:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    elif _HAS_FCNTL:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        # 无锁可用平台：降级为无锁运行（保持原行为）
                        self.degraded = True
                        return False
                    self.acquired = True
                    return True
                except OSError:
                    if time.monotonic() >= deadline:
                        self.degraded = True
                        logger.warning(
                            f"[文件锁] {self.lock_path} 获取超时({self.timeout}s)，降级为无锁运行")
                        return False
                    time.sleep(0.05)
        except Exception as e:
            self.degraded = True
            logger.warning(f"[文件锁] {self.lock_path} 异常，降级为无锁运行: {e}")
            return False

    def release(self):
        if self._fh is not None:
            try:
                if self.acquired:
                    if _HAS_MSVCRT:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    elif _HAS_FCNTL:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                logger.warning(f"[文件锁] 释放失败 {self.lock_path}: {e}")
            finally:
                self.acquired = False
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


def locked_json_update(path: str, mutator, *, timeout: float = 10.0):
    """
    加锁的读-改-写 JSON 更新（FIX P0-16）

    参数:
        path: JSON 文件路径
        mutator: callable(data) -> data，接收旧数据（文件不存在时为 None），返回新数据；
                 返回 None 表示不写入
    返回:
        True=已写入或未变更, False=写入失败
    """
    with FileLock(path, timeout=timeout):
        data = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning(f"[加锁更新] {path} 读取失败，中止更新以防覆盖: {e}")
                return False
        new_data = mutator(data)
        if new_data is None:
            return True
        return atomic_json_write(path, new_data)


def atomic_json_write(path: str, data, *, encoding: str = "utf-8",
                      indent: int = 2, ensure_ascii: bool = False) -> bool:
    """
    原子写入 JSON 文件（临时文件 + os.replace）

    流程:
      1. 将 data 序列化写入 path + ".tmp"
      2. os.replace 原子替换正式文件（Windows 下同盘原子操作）
      3. 任何步骤失败 → 删除 .tmp 残留 → 记日志 → 返回 False

    参数:
        path: 目标文件路径（自动创建父目录）
        data: 可 JSON 序列化的数据
        encoding: 文件编码，默认 utf-8
        indent: JSON 缩进，默认 2
        ensure_ascii: 是否转义非 ASCII 字符，默认 False

    返回:
        True=写入成功, False=写入失败（已记日志）
    """
    tmp_path = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            # FIX(P0-16): fsync 确保落盘，避免断电/崩溃后 os.replace 指向空洞文件
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logger.warning(f"[原子写入] {path} 写入失败: {e}")
        # 清理临时文件残留
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False
