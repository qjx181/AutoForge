"""schemas.py — 数据流分析的数据模型。

定义 SourceKind、SinkKind、TaintCandidate 等核心类型。
不依赖任何第三方库，纯 dataclass。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class SourceKind(enum.Enum):
    """污点来源类型"""
    HTTP_PARAM = "http_param"       # request.args / Query()
    HTTP_BODY = "http_body"         # request.form / request.json
    USER_INPUT = "user_input"       # input()
    ENV_VAR = "env_var"             # os.environ / os.getenv
    FILE_READ = "file_read"         # open().read()
    ARGV = "argv"                   # sys.argv
    DESERIALIZED = "deserialized"   # pickle.loads / yaml.load
    CONFIG = "config"               # 配置文件读取


class SinkKind(enum.Enum):
    """危险操作类型"""
    SQL_QUERY = "sql_query"         # cursor.execute / raw SQL
    SHELL_EXEC = "shell_exec"       # os.system / subprocess
    CODE_EXEC = "code_exec"         # eval / exec
    XSS_WRITE = "xss_write"        # HTML拼接输出
    PATH_TRAVERSAL = "path_traversal"  # 路径拼接
    LOG_INJECT = "log_inject"       # 日志注入
    SSRF = "ssrf"                   # requests.get(user_url)
    PICKLE = "pickle"               # pickle.loads


@dataclass
class TaintHop:
    """污点传播的一跳：从 variable 在 lineno 处被赋值/传播。"""
    variable: str
    lineno: int
    code_line: str      # 原始代码行
    hop_type: str = "assign"  # assign / propagate / fstring / call_arg


@dataclass
class TaintCandidate:
    """一个完整的污点候选：source → hops → sink。

    这是发给 LLM triage 的最小单元。
    """
    source_kind: SourceKind
    source_pattern: str        # 触发 source 的代码片段
    source_line: int
    sink_kind: SinkKind
    sink_pattern: str          # 触发 sink 的代码片段
    sink_line: int
    hops: list[TaintHop] = field(default_factory=list)
    filepath: str = ""
    function_name: str = ""
    snippet: str = ""          # 上下文代码片段（source到sink之间）
    confidence: float = 0.0    # 数据流层的置信度（0-1）

    @property
    def hop_count(self) -> int:
        return len(self.hops)

    @property
    def tainted_vars(self) -> str:
        return " → ".join(h.variable for h in self.hops) if self.hops else "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind.value,
            "source_pattern": self.source_pattern,
            "source_line": self.source_line,
            "sink_kind": self.sink_kind.value,
            "sink_pattern": self.sink_pattern,
            "sink_line": self.sink_line,
            "hop_count": self.hop_count,
            "tainted_vars": self.tainted_vars,
            "filepath": self.filepath,
            "function_name": self.function_name,
            "snippet": self.snippet[:500],
        }
