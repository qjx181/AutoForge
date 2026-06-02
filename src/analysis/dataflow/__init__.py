"""dataflow — 数据流引导的代码分析模块。

核心思路（借鉴 LLift/OOPSLA 2024）：
  1. 用廉价的数据流追踪缩小搜索空间
  2. 只把真正的候选点发给 LLM 做精判
  3. 误报反馈循环持续降低噪音
"""

from .taint_tracker import TaintTracker, TaintCandidate
from .feedback import FeedbackStore
from .triage import triage_candidates

__all__ = [
    "TaintTracker",
    "TaintCandidate",
    "FeedbackStore",
    "triage_candidates",
]
