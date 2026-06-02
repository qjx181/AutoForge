"""experience_fixer.py — 经验驱动的修复器

设计动机（面试话术）：
  "规则修复器（enterprise_fixer）能处理已知模式，但无法从历史中学习。
   LLM Fixer 能处理未知模式，但每次都要重新推理，成本高。
   经验 Fixer 是中间层——从历史成功修复中提取结构化模式，
   下次遇到同类问题时，直接套用已验证的方案。零 API 调用，零推理成本。"

核心机制：
  1. 启动时从 ExperienceStore 加载所有成功修复记录
  2. 按 issue_type 聚合，提取 (pattern → fix_action) 映射
  3. 修复时，匹配当前 issue 与历史模式，找到最相似的成功方案
  4. 直接套用该方案的修复动作

与 Skill 的关系：
  - Skill 是给人看的文档（SKILL.md），不可执行
  - 经验 Fixer 是可执行的代码路径，直接参与修复链
  - 两者互补：Skill 帮人理解，经验 Fixer 帮机器执行
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from src.core.adapters_pkg.fixer_adapter import FixerAdapter
from src.core.adapters_pkg.issue import Issue
from src.core.adapters_pkg.fix_result import FixResult

logger = logging.getLogger(__name__)


@dataclass
class FixPattern:
    """从历史经验中提取的修复模式。"""
    issue_type: str
    fixer_name: str
    action: str           # 原始修复动作描述
    code_before: str      # 修复前的代码片段（用于模式匹配）
    code_after: str       # 修复后的代码片段（实际应用）
    success_count: int    # 该模式的成功次数
    avg_confidence: float # 平均置信度
    file_pattern: str     # 文件路径模式（如 src/api/*.py）


class ExperienceFixer(FixerAdapter):
    """从经验库中学习的修复器。

    不调 LLM，不做规则匹配——直接从历史成功修复中找最相似的方案。
    核心价值：零推理成本 + 已验证方案 = 高效 + 可靠。
    """

    def __init__(self):
        self._patterns: dict[str, list[FixPattern]] = {}  # issue_type → patterns
        self._loaded = False

    @property
    def name(self) -> str:
        return "experience_fixer"

    @property
    def supported_types(self) -> list[str]:
        return ["*"]  # 兜底，但优先级低于规则 fixer

    def _ensure_loaded(self) -> None:
        """延迟加载经验数据。"""
        if self._loaded:
            return
        self._loaded = True
        try:
            from src.core.experience_store import _load as load_experience_data
            data = load_experience_data()
            self._extract_patterns(data)
            logger.info(
                "[ExperienceFixer] 加载了 %d 个 issue_type 的 %d 个修复模式",
                len(self._patterns),
                sum(len(v) for v in self._patterns.values()),
            )
        except Exception as e:
            logger.warning("[ExperienceFixer] 加载经验数据失败: %s", e)

    def _extract_patterns(self, data: dict) -> None:
        """从经验数据中提取结构化修复模式。

        策略：
        1. 只提取成功且置信度 >= 0.5 的经验
        2. 按 (issue_type, action) 去重，保留成功次数最多的
        3. 记录文件路径模式，用于相似度匹配
        4. 优先使用 code_before/code_after（精确代码对），其次用 action 文本
        """
        from collections import defaultdict
        # (issue_type, action_normalized) → {count, total_conf, fixer, action, files, code_before, code_after}
        pattern_map: dict[tuple[str, str], dict] = defaultdict(lambda: {
            "count": 0, "total_conf": 0.0, "fixer": "", "action": "",
            "files": set(), "code_before": "", "code_after": "",
        })

        for exp in data.get("experiences", []):
            if not exp.get("success", False):
                continue
            if exp.get("confidence", 0) < 0.5:
                continue

            issue_type = exp.get("issue_type", "")
            action = exp.get("action", "")
            if not issue_type or not action:
                continue

            # 优先用 code_before 作为模式键（精确匹配）
            code_before = exp.get("code_before", "")
            if code_before:
                action_key = f"__code__{code_before[:100]}"
            else:
                action_key = self._normalize_action(action)

            key = (issue_type, action_key)
            p = pattern_map[key]
            p["count"] += 1
            p["total_conf"] += exp.get("confidence", 0)
            p["fixer"] = exp.get("fixer", "unknown")
            p["action"] = action
            p["files"].add(exp.get("context", {}).get("file", ""))
            # 保留最长的 code_before/code_after
            if code_before and len(code_before) > len(p["code_before"]):
                p["code_before"] = code_before
                p["code_after"] = exp.get("code_after", "")

        # 转换为 FixPattern 列表
        for (issue_type, _), p in pattern_map.items():
            if p["count"] < 1:  # 至少成功 1 次
                continue
            pattern = FixPattern(
                issue_type=issue_type,
                fixer_name=p["fixer"],
                action=p["action"],
                code_before=p["code_before"],
                code_after=p["code_after"],
                success_count=p["count"],
                avg_confidence=p["total_conf"] / p["count"],
                file_pattern=self._extract_file_pattern(p["files"]),
            )
            self._patterns.setdefault(issue_type, []).append(pattern)

        # 按成功次数降序排列
        for patterns in self._patterns.values():
            patterns.sort(key=lambda x: (x.success_count, x.avg_confidence), reverse=True)

    def fix(self, issue: Issue, project_root: Path) -> FixResult:
        """用经验模式修复问题。

        策略：
        1. 查找该 issue_type 的所有成功模式
        2. 用文件路径相似度 + 代码片段相似度加权排序
        3. 取最高分的模式，套用其修复方案
        """
        self._ensure_loaded()
        patterns = self._patterns.get(issue.type, [])
        if not patterns:
            return FixResult(
                success=False,
                action="",
                confidence=0.0,
                fixer=self.name,
                issue_type=issue.type,
                file=issue.file,
                line=issue.line,
                error=f"[ExperienceFixer] 没有 {issue.type} 的历史成功经验",
            )

        # 选择最匹配的模式
        best_pattern = self._select_best_pattern(patterns, issue)
        if not best_pattern:
            return FixResult(
                success=False,
                action="",
                confidence=0.0,
                fixer=self.name,
                issue_type=issue.type,
                file=issue.file,
                line=issue.line,
                error="[ExperienceFixer] 无法匹配到合适的修复模式",
            )

        # 尝试应用修复
        try:
            success = self._apply_pattern(best_pattern, issue, project_root)
            if success:
                return FixResult(
                    success=True,
                    action=f"[经验修复] 基于 {best_pattern.fixer_name} 的历史方案: {best_pattern.action[:200]}",
                    confidence=best_pattern.avg_confidence,
                    fixer=self.name,
                    issue_type=issue.type,
                    file=issue.file,
                    line=issue.line,
                    diff=f"历史成功 {best_pattern.success_count} 次, 平均置信度 {best_pattern.avg_confidence:.2f}, 来源: {best_pattern.fixer_name}",
                )
            else:
                return FixResult(
                    success=False,
                    action=best_pattern.action,
                    confidence=0.0,
                    fixer=self.name,
                    issue_type=issue.type,
                    file=issue.file,
                    line=issue.line,
                    error="[ExperienceFixer] 模式匹配但应用失败，建议 fallback 到 LLM Fixer",
                )
        except Exception as e:
            return FixResult(
                success=False,
                action=best_pattern.action,
                confidence=0.0,
                fixer=self.name,
                issue_type=issue.type,
                file=issue.file,
                line=issue.line,
                error=f"[ExperienceFixer] 应用异常: {e}",
            )

    def _select_best_pattern(self, patterns: list[FixPattern], issue: Issue) -> Optional[FixPattern]:
        """选择最匹配的修复模式。

        匹配算法：
        1. 文件路径相似度（同目录 > 同后缀 > 全局）
        2. 代码片段相似度（Jaccard on tokens）
        3. 加权：0.4 * 文件相似度 + 0.6 * 代码相似度
        """
        if not patterns:
            return None

        scored = []
        for p in patterns:
            file_sim = self._file_similarity(p.file_pattern, issue.file)
            code_sim = self._code_similarity(p.code_before, issue.description)
            score = 0.4 * file_sim + 0.6 * code_sim
            scored.append((score, p))

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_pattern = scored[0]

        # 阈值：至少 0.3 的相似度
        if best_score < 0.3:
            logger.debug(
                "[ExperienceFixer] 最高相似度 %.2f 低于阈值，跳过 %s",
                best_score, issue.type,
            )
            return None

        logger.info(
            "[ExperienceFixer] 匹配模式: %s (相似度 %.2f, 成功 %d 次)",
            issue.type, best_score, best_pattern.success_count,
        )
        return best_pattern

    def _apply_pattern(self, pattern: FixPattern, issue: Issue, project_root: Path) -> bool:
        """尝试将经验模式应用到目标文件。

        当前实现：如果经验中记录了 code_before/code_after，做精确替换。
        如果只有 action 描述，返回 False（fallback 到 LLM）。
        """
        if not pattern.code_before or not pattern.code_after:
            # 没有精确的代码对，无法自动应用
            return False

        target_file = project_root / issue.file
        if not target_file.exists():
            return False

        content = target_file.read_text(encoding="utf-8")
        if pattern.code_before not in content:
            return False

        new_content = content.replace(pattern.code_before, pattern.code_after, 1)
        target_file.write_text(new_content, encoding="utf-8")

        # 语法检查
        try:
            import ast
            ast.parse(new_content)
            return True
        except SyntaxError:
            # 回滚
            target_file.write_text(content, encoding="utf-8")
            return False

    @staticmethod
    def _normalize_action(action: str) -> str:
        """归一化修复动作，去掉变化部分。"""
        # 去掉文件名、行号、具体变量名
        normalized = re.sub(r'[\\/][\w./\\]+\.\w+', '<FILE>', action)
        normalized = re.sub(r'line \d+', 'line N', normalized)
        normalized = re.sub(r'\b[a-z_]\w{20,}\b', '<LONG_NAME>', normalized)
        return normalized[:200]

    @staticmethod
    def _extract_file_pattern(files: set[str]) -> str:
        """从文件集合中提取通用模式。"""
        if not files:
            return "*"
        # 取最长公共前缀的目录部分
        parts_list = [f.replace("\\", "/").split("/") for f in files if f]
        if not parts_list:
            return "*"
        # 简单策略：取最短路径的目录部分
        shortest = min(parts_list, key=len)
        if len(shortest) > 1:
            return "/".join(shortest[:-1]) + "/*"
        return "*"

    @staticmethod
    def _file_similarity(pattern_path: str, issue_file: str) -> float:
        """计算文件路径相似度。"""
        p = pattern_path.replace("\\", "/").split("/")
        f = issue_file.replace("\\", "/").split("/")
        # 同目录 = 1.0，同后缀 = 0.5，不同 = 0.1
        if len(p) > 1 and len(f) > 1 and p[-2] == f[-2]:
            return 1.0
        if p[-1].endswith(f[-1].split(".")[-1]) if "." in f[-1] else False:
            return 0.5
        return 0.1

    @staticmethod
    def _code_similarity(a: str, b: str) -> float:
        """计算代码片段相似度（Jaccard on tokens）。"""
        if not a or not b:
            return 0.0
        tokens_a = set(re.findall(r'\b\w+\b', a.lower()))
        tokens_b = set(re.findall(r'\b\w+\b', b.lower()))
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(intersection) / len(union)
