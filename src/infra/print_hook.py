"""全局 print → logging 转换，0 代码改动"""
import builtins
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import logging
import os
_original_print = builtins.print

def _logged_print(*args, **kwargs):
    frame = __import__("inspect").stack()[1]
    module = frame.frame.f_globals.get("__name__", "unknown")
    logger = logging.getLogger(module)
    message = " ".join(str(a) for a in args)
    logger.info(message)

_project3_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
builtins.print = _logged_print
