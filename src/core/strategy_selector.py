"""strategy_selector.py — 策略选择器

设计动机（面试话术）：
  "当前 FixerRegistry.get_fixer() 按注册顺序找第一个匹配的 fixer，
   不管这个 fixer 在同类问题上的历史表现如何。策略选择器根据历史经验，
   对同类问题自动选择成功率最高的 fixer。"

核心算法：
  1. 查找所有能处理该 issue_type 的 fixer
  2. 从 ExperienceStore 获取每个 fixer 的历史成功率
  3. 如果样本足够（>=3次），选成功率最高的
  4. 如果样本不足，回退到默认注册顺序

与 fallback chain 的配合：
  - 策略选择器选第一个尝试的 fixer
  - fallback chain 负责失败后切换到下一个
"""

import logging
from typing import Optional
from src.core.adapters_pkg.fixer_adapter import FixerAdapter
from src.core.adapters_pkg.fixer_registry import FixerRegistry
from src.core.experience_store import _load as load_experience_data

logger = logging.getLogger(__name__)


def get_best_fixer(
    issue_type: str,
    fixers: FixerRegistry,
    min_samples: int = 3,
) -> Optional[FixerAdapter]:
    """根据历史经验选择最佳修复器。

    算法：
      1. 获取所有能处理该 issue_type 的 fixer
      2. 从经验库获取每个 fixer 的历史成功率
      3. 成功率最高的排第一（需要 >= min_samples 次样本）
      4. 没有足够历史数据的 fixer 按注册顺序排

    Args:
        issue_type: 问题类型
        fixers: 修复器注册表
        min_samples: 最少样本数（低于此数不参与策略选择）

    Returns:
        最佳修复器，如果没有匹配的返回 None
    """
    candidates = fixers.get_fixers_for_type(issue_type)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    try:
        data = load_experience_data()
        overrides = data.get("confidence_overrides", {})
    except Exception:
        return candidates[0]

    scored = []
    for fixer in candidates:
        override_key = f"{issue_type}:{fixer.name}"
        override = overrides.get(override_key)
        if override and override.get("sample_size", 0) >= min_samples:
            scored.append((fixer, override["success_rate"], override["sample_size"]))
        else:
            scored.append((fixer, -1.0, 0))  # -1 表示无数据

    # 分两组：有数据的按成功率降序，无数据的按注册顺序
    with_data = [(f, sr, n) for f, sr, n in scored if sr >= 0]
    without_data = [(f, sr, n) for f, sr, n in scored if sr < 0]

    with_data.sort(key=lambda x: x[1], reverse=True)

    ordered = with_data + without_data

    if ordered:
        best = ordered[0]
        if best[1] >= 0:
            logger.info(
                "[策略选择] %s → %s (成功率 %.1f%%, %d次样本)",
                issue_type, best[0].name, best[1] * 100, best[2],
            )
        return best[0]

    return candidates[0]


def get_fallback_chain(
    issue_type: str,
    fixers: FixerRegistry,
    min_samples: int = 3,
) -> list[FixerAdapter]:
    """获取按历史表现排序的 fallback chain。

    返回所有能处理该 issue_type 的 fixer 列表，
    按成功率从高到低排列（无历史数据的排最后）。

    Args:
        issue_type: 问题类型
        fixers: 修复器注册表
        min_samples: 最少样本数

    Returns:
        排序后的 fixer 列表（可能为空）
    """
    candidates = fixers.get_fixers_for_type(issue_type)
    if not candidates:
        return []

    if len(candidates) == 1:
        return candidates

    try:
        data = load_experience_data()
        overrides = data.get("confidence_overrides", {})
    except Exception:
        return candidates

    scored = []
    for fixer in candidates:
        override_key = f"{issue_type}:{fixer.name}"
        override = overrides.get(override_key)
        if override and override.get("sample_size", 0) >= min_samples:
            scored.append((fixer, override["success_rate"]))
        else:
            scored.append((fixer, -1.0))

    with_data = [(f, sr) for f, sr in scored if sr >= 0]
    without_data = [(f, sr) for f, sr in scored if sr < 0]
    with_data.sort(key=lambda x: x[1], reverse=True)

    return [f for f, _ in with_data] + [f for f, _ in without_data]
