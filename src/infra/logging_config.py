"""项目三 日志配置 — 统一管理所有输出"""

import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / "p3.log"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_configured = False

def setup_logging(level=logging.INFO, log_file=None):
    """配置全局日志系统
    
    - 控制台：只有消息本身（干净）
    - 文件：完整的时间/级别/模块信息（可追溯）
    """
    global _configured
    if _configured:
        return
    _configured = True

    log_file = log_file or LOG_FILE
    log_file = Path(log_file)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, encoding="utf-8", mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(ch)

class PrintToLogger:
    """替换 print 的过渡方案
    
    用法：在模块顶部 print = PrintToLogger(__name__).info
    
    效果：原来的 print("xxx") 变成 logger.info("xxx") 的行为，
    但不需要逐个改代码。
    """
    
    def __init__(self, name=None):
        self._logger = logging.getLogger(name or __name__)
    
    def info(self, *args, **kwargs):
        self._logger.info(" ".join(str(a) for a in args))
    
    def debug(self, *args, **kwargs):
        self._logger.debug(" ".join(str(a) for a in args))
    
    def warning(self, *args, **kwargs):
        self._logger.warning(" ".join(str(a) for a in args))
    
    def error(self, *args, **kwargs):
        self._logger.error(" ".join(str(a) for a in args))
