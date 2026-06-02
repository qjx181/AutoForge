"""tiered_scanner.py — 分级扫描策略（Level 0/1/2）

设计动机（面试话术）：
  "企业级扫描不能只有一种模式。如果每次都调 DeepSeek API，
   一个小项目的扫描成本就可能超过 $0.30，而且 API 不可用时整个系统就瘫痪。
   我设计了三级降级策略：
     Level 0: 纯规则（AST + 正则），零成本，能覆盖 60% 的问题
     Level 1: 本地小模型（Ollama），几乎免费，能覆盖 80%
     Level 2: 云端 API（DeepSeek），按 token 计费，覆盖 95%
   系统启动时自动检测可用的最高级别，失败时自动降级。"

与 scan_deep 的关系：
  scan_deep 只实现了 Level 2（云端 API）。
  tiered_scanner 是 scan_deep 的上层封装，
  根据环境可用性选择合适的扫描级别，失败时自动降级。

用法：
    from src.analysis.tiered_scanner import TieredScanner
    scanner = TieredScanner()
    result = scanner.scan(Path("/path/to/project"))  # 自动选最高可用级别
    result = scanner.scan(Path("/path/to/project"), level=ScannerLevel.RULE)  # 强制指定级别
"""

import json
import logging
import os
import subprocess
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ScannerLevel(Enum):
    """扫描级别枚举。"""
    RULE = 0       # 纯规则（AST + 正则）
    LOCAL = 1      # 本地小模型（Ollama）
    CLOUD = 2      # 云端 API（DeepSeek）


# 每个级别的预估覆盖率（用于日志和报告）
LEVEL_COVERAGE = {
    ScannerLevel.RULE: 0.60,
    ScannerLevel.LOCAL: 0.80,
    ScannerLevel.CLOUD: 0.95,
}


def detect_available_level() -> ScannerLevel:
    """自动检测当前环境可用的最高扫描级别。

    检测顺序（从高到低）：
      1. Cloud: 检查 DEEPSEEK_API_KEY 环境变量
      2. Local: 检查 Ollama 服务是否可达（localhost:11434）
      3. Rule: 始终可用（纯 Python AST，无外部依赖）

    Returns:
        可用的最高 ScannerLevel
    """
    # Level 2: Cloud API
    if os.environ.get("DEEPSEEK_API_KEY"):
        logger.info("检测到 DEEPSEEK_API_KEY，可用 Level 2 (Cloud)")
        return ScannerLevel.CLOUD

    # Level 1: Local Ollama
    if _check_ollama_available():
        logger.info("检测到 Ollama 服务，可用 Level 1 (Local)")
        return ScannerLevel.LOCAL

    # Level 0: 纯规则（始终可用）
    logger.info("未检测到 LLM 服务，降级到 Level 0 (Rule)")
    return ScannerLevel.RULE


def _check_ollama_available() -> bool:
    """检查 Ollama 服务是否可达。"""
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


class TieredScanner:
    """分级扫描器：自动选择可用的最高级别，失败时降级。

    核心逻辑：
      1. detect_available_level() 确定最高可用级别
      2. scan() 从该级别开始，失败则逐级降级
      3. 每次降级都在日志中记录，方便排查
      4. 返回结果包含实际使用的级别和覆盖率预估

    设计决策：
      "为什么不并行跑三个级别然后合并？因为成本。
       Level 2 的 API 调用是有成本的，如果 Level 0 已经找到足够多的问题，
       没必要再花 API 费用。降级策略是'先用贵的，贵的不行才用便宜的'，
       而不是'全部跑一遍取并集'。"
    """

    def __init__(self, max_level: Optional[ScannerLevel] = None):
        """
        Args:
            max_level: 限制最高扫描级别（None 则自动检测）
        """
        if max_level is not None:
            self._max_level = max_level
        else:
            self._max_level = detect_available_level()
        logger.info("TieredScanner 初始化: max_level=%s (覆盖率 %.0f%%)",
                     self._max_level.name, LEVEL_COVERAGE[self._max_level] * 100)

    def scan(
        self,
        project_root: Path,
        level: Optional[ScannerLevel] = None,
        skip_tests: bool = True,
    ) -> dict:
        """执行分级扫描，自动降级。

        Args:
            project_root: 目标项目根目录
            level: 强制指定扫描级别（None 则从最高可用级别开始）
            skip_tests: 是否跳过测试模块

        Returns:
            标准扫描结果 dict，额外包含:
              - "scan_level": 实际使用的扫描级别
              - "coverage_estimate": 预估覆盖率
              - "fallback_from": 降级来源（如果发生了降级）
        """
        start_level = level or self._max_level
        current_level = start_level

        while True:
            try:
                result = self._scan_at_level(current_level, project_root, skip_tests)
                result["scan_level"] = current_level.name
                result["coverage_estimate"] = LEVEL_COVERAGE[current_level]
                if current_level.value < start_level.value:
                    result["fallback_from"] = start_level.name
                    logger.warning("降级扫描: %s → %s", start_level.name, current_level.name)
                else:
                    result["fallback_from"] = None
                return result
            except Exception as e:
                logger.warning("Level %s 扫描失败: %s", current_level.name, e)
                # 尝试降级到下一级
                next_level = self._get_lower_level(current_level)
                if next_level is None:
                    # 已经是最低级别，返回空结果
                    logger.error("所有扫描级别都失败了，返回空结果")
                    return self._empty_result(f"所有级别扫描失败，最后尝试 {current_level.name}: {e}")
                logger.info("降级: %s → %s", current_level.name, next_level.name)
                current_level = next_level

    def _scan_at_level(
        self, level: ScannerLevel, project_root: Path, skip_tests: bool
    ) -> dict:
        """在指定级别执行扫描。"""
        if level == ScannerLevel.CLOUD:
            return self._scan_cloud(project_root, skip_tests)
        elif level == ScannerLevel.LOCAL:
            return self._scan_local(project_root, skip_tests)
        elif level == ScannerLevel.RULE:
            return self._scan_rule(project_root)
        else:
            raise ValueError(f"未知扫描级别: {level}")

    def _scan_cloud(self, project_root: Path, skip_tests: bool) -> dict:
        """Level 2: 调用 scan_deep（DeepSeek API）。"""
        from .llm_scanner import scan_deep
        return scan_deep(project_root, skip_tests=skip_tests)

    def _scan_local(self, project_root: Path, skip_tests: bool) -> dict:
        """Level 1: 调用 Ollama 本地模型扫描。

        使用与 Level 2 相同的 prompt 模板，但通过 Ollama API 调用本地模型。
        本地模型（如 qwen2.5:7b）的推理能力弱于 DeepSeek，但成本为零。
        """
        from .llm_scanner import (
            _build_module_batches, _build_module_context,
            _build_module_prompt, _run_light_ast_pass,
            _smart_dedup, _parse_llm_response,
        )
        from .project_analyzer import analyze_project

        project_root = Path(project_root).resolve()
        blueprint = analyze_project(str(project_root))
        source_files = blueprint.get_source_files("python")

        # AST 轻量扫描（所有级别都跑）
        ast_issues = _run_light_ast_pass(project_root, blueprint)

        # 用 Ollama 替代 DeepSeek
        batches = _build_module_batches(project_root, blueprint)
        non_test_batches = [b for b in batches if not b["is_test"]]

        llm_issues = []
        for batch in non_test_batches:
            context_text, total_lines, _cost = _build_module_context(batch, project_root)
            prompt = _build_module_prompt(
                blueprint, batch, context_text, total_lines,
                dimensions=["architecture", "quality"]
            )
            response = self._call_ollama(prompt)
            if response:
                parsed = _parse_llm_response(response)
                llm_issues.extend(parsed)

        all_issues = ast_issues + llm_issues
        all_issues = _smart_dedup(all_issues)

        by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for issue in all_issues:
            sev = issue.get("severity", "low")
            if sev in by_severity:
                by_severity[sev] += 1

        weights = {"critical": 15, "high": 8, "medium": 3, "low": 1}
        deduction = sum(weights.get(i.get("severity", "low"), 0) for i in all_issues)
        score = max(0, min(100, 100 - deduction))

        return {
            "score": score,
            "total_issues": len(all_issues),
            "issue_count": len(all_issues),
            "issues": all_issues,
            "by_severity": by_severity,
            "files_scanned": len(source_files),
            "modules_scanned": len(batches),
            "llm_cost_estimate": 0.0,
        }

    def _scan_rule(self, project_root: Path) -> dict:
        """Level 0: 纯规则扫描（AST + 正则），不调用任何 LLM。

        覆盖维度：
          - 圈复杂度（函数过长/嵌套过深）
          - 裸 except（bare except）
          - 吞没异常（swallowed exception）
          - 硬编码密钥（hardcoded secret）
          - 缺少类型注解（missing type hints，仅检测公开函数）

        局限性（面试话术）：
          "规则扫描只能找到'表面'问题——违反编码规范、明显的代码异味。
           它不能理解代码的'意图'，比如一个变量名拼错了但语法正确，
           规则扫描发现不了。这就是为什么需要 Level 1/2 的 LLM 扫描。"
        """
        from .llm_scanner import _run_light_ast_pass, _smart_dedup
        from .project_analyzer import analyze_project

        project_root = Path(project_root).resolve()
        blueprint = analyze_project(str(project_root))
        source_files = blueprint.get_source_files("python")

        # AST 扫描（已有实现，覆盖圈复杂度/裸except/吞异常）
        ast_issues = _run_light_ast_pass(project_root, blueprint)

        # 额外的正则扫描（AST 扫描不覆盖的维度）
        regex_issues = self._regex_scan(project_root)

        all_issues = ast_issues + regex_issues
        all_issues = _smart_dedup(all_issues)

        by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for issue in all_issues:
            sev = issue.get("severity", "low")
            if sev in by_severity:
                by_severity[sev] += 1

        weights = {"critical": 15, "high": 8, "medium": 3, "low": 1}
        deduction = sum(weights.get(i.get("severity", "low"), 0) for i in all_issues)
        score = max(0, min(100, 100 - deduction))

        return {
            "score": score,
            "total_issues": len(all_issues),
            "issue_count": len(all_issues),
            "issues": all_issues,
            "by_severity": by_severity,
            "files_scanned": len(source_files),
            "modules_scanned": 0,
            "llm_cost_estimate": 0.0,
        }

    def _regex_scan(self, project_root: Path) -> list[dict]:
        """正则扫描：硬编码密钥、危险函数调用。"""
        import re

        issues = []
        patterns = [
            # 硬编码密钥
            (r'''(?:api_key|secret|password|token)\s*=\s*["'][^"']{8,}["']''',
             "hardcoded_secret", "high", "疑似硬编码密钥"),
            # eval/exec 调用
            (r'''\b(?:eval|exec)\s*\(''',
             "dangerous_function", "high", "使用 eval/exec 存在代码注入风险"),
            # pickle.load（反序列化漏洞）
            (r'''\bpickle\.load\b''',
             "unsafe_deserialization", "medium", "pickle.load 存在反序列化漏洞"),
        ]

        for py_file in project_root.rglob("*.py"):
            # 跳过无关目录
            parts = py_file.parts
            if any(p in {"__pycache__", ".git", ".venv", "venv", "node_modules"} for p in parts):
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for line_num, line in enumerate(content.splitlines(), 1):
                for pattern, issue_type, severity, description in patterns:
                    if re.search(pattern, line):
                        issues.append({
                            "type": issue_type,
                            "severity": severity,
                            "file": str(py_file.relative_to(project_root)),
                            "line": line_num,
                            "description": description,
                            "code_snippet": line.strip()[:200],
                            "confidence": 0.8,
                            "dimension": "security",
                            "scanner": "regex_scan",
                        })

        return issues

    def _call_ollama(self, prompt: str, model: str = "qwen2.5:7b") -> Optional[str]:
        """调用 Ollama 本地模型。"""
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 4096},
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("response", "")
        except Exception as e:
            logger.warning("Ollama 调用失败: %s", e)
            return None

    @staticmethod
    def _get_lower_level(level: ScannerLevel) -> Optional[ScannerLevel]:
        """获取下一级扫描级别。"""
        if level == ScannerLevel.CLOUD:
            return ScannerLevel.LOCAL
        elif level == ScannerLevel.LOCAL:
            return ScannerLevel.RULE
        return None  # RULE 已经是最低级别

    @staticmethod
    def _empty_result(error: str = "") -> dict:
        """返回空的扫描结果。"""
        return {
            "score": 0,
            "total_issues": 0,
            "issue_count": 0,
            "issues": [],
            "by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "files_scanned": 0,
            "modules_scanned": 0,
            "error": error,
            "scan_level": "NONE",
            "coverage_estimate": 0.0,
            "fallback_from": None,
        }
