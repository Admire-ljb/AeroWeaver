"""
core/logger.py — AeroWeaver 统一日志系统

功能：
- 终端彩色输出
- 自动保存到 logs/YYYY-MM-DD.log
- 按日期轮转，保留 7 天
- 一次调用全局生效
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# ── 彩色格式化 ───────────────────────────────────────────

COLORS = {
    "DEBUG":    "\033[36m",    # cyan
    "INFO":     "\033[32m",    # green
    "WARNING":  "\033[33m",    # yellow
    "ERROR":    "\033[31m",    # red
    "CRITICAL": "\033[1;31m",  # bold red
}
RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """终端彩色日志格式化"""

    def __init__(self, fmt: str = None):
        super().__init__(fmt or "%(asctime)s [%(levelname)s] %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        color = COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname}{RESET}"
        return super().format(record)


class FileFormatter(logging.Formatter):
    """文件日志格式化（无颜色）"""

    def __init__(self):
        super().__init__(
            "%(asctime)s [%(levelname)-8s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )


# ── 初始化函数 ───────────────────────────────────────────

_initialized = False


def setup_logging(
    log_dir: str = "logs",
    level: str = "INFO",
    keep_days: int = 7,
) -> None:
    """
    配置全局日志。只需在 server.py 启动时调用一次。

    Args:
        log_dir:   日志文件目录
        level:     日志级别 (DEBUG/INFO/WARNING/ERROR)
        keep_days: 保留天数
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    log_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(log_level)

    # 清除已有 handlers（防止重复）
    root.handlers.clear()

    # 终端 handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(ColorFormatter())
    root.addHandler(console)

    # 文件 handler
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    file_handler = TimedRotatingFileHandler(
        filename=str(log_path / f"{today}.log"),
        when="midnight",
        interval=1,
        backupCount=keep_days,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(FileFormatter())
    root.addHandler(file_handler)

    logging.info(f"日志系统初始化完成 (级别={level}, 目录={log_dir}, 保留={keep_days}天)")


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger"""
    return logging.getLogger(name)
