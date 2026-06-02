"""llm_fixer.py — LLM 驱动的智能修复器

设计动机（面试话术）：
  "规则修复器（如 enterprise_fixer）能处理 80% 的常见问题，
   但遇到复杂逻辑错误、跨文件依赖、或者需要创造性解决方案时，
   规则引擎力不从心。LLM Fixer 作为最后一道防线，对规则修复器
   搞不定的问题，调 LLM 生成修复方案。"

核心设计：
  1. 经验注入 — 把历史成功/失败经验作为 prompt 上下文
     "这类问题之前用 X 方法成功率 90%，用 Y 方法只有 30%"
  2. 多候选生成 — 每次生成 3~5 个方案，择优选择
  3. 行范围替换 — 不返回完整文件，只输出需要改动的行范围
     避免了完整文件嵌入 JSON 字符串的转义问题
  4. 评分择优 — 语法验证 + 最小 diff + 置信度加权评分
  5. 结构化输出 — LLM 必须返回 JSON 格式的修复方案

使用场景：
  - 规则修复器返回 success=False 的兜底
  - 跨文件重构、复杂逻辑修复
  - 新类型问题首次出现（没有预设规则）

Recent Changes:
  - 2026-06: 改为多候选生成（3-5个）+ 行范围替换格式
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from src.core.adapters_pkg.fix_result import FixResult
from src.core.adapters_pkg.issue import Issue

logger = logging.getLogger(__name__)


def _load_env_file():
    """从项目 .env 文件加载环境变量（如果尚未加载）。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return  # 已加载
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _call_llm(prompt: str, provider: str = "deepseek", model: str = "") -> str:
    """调用 LLM API 获取响应。

    支持的 provider:
      - deepseek: DeepSeek API（默认）
      - ollama: 本地 Ollama
      - openai_compatible: 任何 OpenAI 兼容 API

    Note: 使用 response_format: json_object 确保返回有效 JSON。
    """
    import urllib.error
    import urllib.request

    _load_env_file()

    if provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY 环境变量未设置，请在 .env 文件中配置")
        base_url = os.environ.get("DEEPSEEK_HOST", "https://api.deepseek.com")
        url = f"{base_url.rstrip('/')}/v1/chat/completions"
        model = model or "deepseek-chat"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a Python code fixer. Always respond with valid JSON only, no markdown."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
        }

    elif provider == "ollama":
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = model or "llama3.1:8b"
        url = f"{host}/api/chat"
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }

    elif provider == "openai_compatible":
        api_key = os.environ.get("LLM_API_KEY", "")
        if not api_key:
            raise ValueError("LLM_API_KEY 环境变量未设置，请在 .env 文件中配置")
        base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com")
        url = f"{base_url.rstrip('/')}/v1/chat/completions"
        model = model or "gpt-4o-mini"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 2048,
        }
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM API error {e.code}: {body[:200]}") from e
    except Exception as e:
        raise RuntimeError(f"LLM API call failed: {e}") from e


def _apply_line_range_fix(
    original_content: str,
    line_range: list[int],
    replacement: str,
) -> str:
    """将原始文件中指定 1-indexed 行范围替换为新代码。

    Args:
        original_content: 原始文件内容
        line_range: [start, end] (1-indexed, inclusive)
        replacement: 替换后的代码块

    Returns:
        替换后的完整文件内容
    """
    lines = original_content.split("\n")
    start, end = line_range
    start_idx = max(0, start - 1)
    end_idx = min(len(lines), end)

    result = lines[:start_idx] + [replacement] + lines[end_idx:]
    return "\n".join(result)


def _build_fix_prompt(
    issue: Issue,
    file_content: str,
    past_experiences: list[dict],
    failure_warnings: list[str],
    pattern_stats: dict,
) -> str:
    """构建多候选修复 prompt。

    输出格式：LLM 仅返回改动行范围，不返回完整文件内容。
    这避免了 JSON 字符串嵌入大文件的转义问题。
    """
    # 提取问题行周围的上下文（前后各15行，方便 LLM 理解范围）
    lines = file_content.split("\n")
    issue_line = max(0, issue.line - 1)  # 0-indexed
    ctx_start = max(0, issue_line - 15)
    ctx_end = min(len(lines), issue_line + 16)
    context_code = "\n".join(
        f"{'→ ' if i == issue_line else '  '}{i+1:4d}| {lines[i]}"
        for i in range(ctx_start, ctx_end)
    )

    # 构建经验上下文
    exp_section = ""
    if past_experiences:
        # 检测是否为语义检索结果（含 code_before/code_after）
        is_semantic = any(e.get("_semantic") for e in past_experiences)
        if is_semantic:
            # ── 语义检索格式：展示 code_before → code_after 示例 ──
            exp_blocks = []
            for i, exp in enumerate(past_experiences):
                score = exp.get("similarity_score", 0.0)
                action = exp.get("action", "")
                cb = exp.get("code_before", "")
                ca = exp.get("code_after", "")
                exp_blocks.append(
                    f"【相似经验 #{i+1}】（相似度: {score:.2f}）"
                )
                if cb:
                    exp_blocks.append("问题代码:")
                    exp_blocks.append(f"```python\n{cb}\n```")
                if ca:
                    exp_blocks.append("修复后代码:")
                    exp_blocks.append(f"```python\n{ca}\n```")
                if action:
                    exp_blocks.append(f"修复方法: {action}")
            exp_section = f"\n【语义检索到的相似经验（供参考借鉴）】\n{"\n".join(exp_blocks)}"
        else:
            # ── 传统格式：纯文本列表 ──
            exp_lines = []
            for exp in past_experiences:
                status = "✓成功" if exp.get("success") else "✗失败"
                exp_lines.append(
                    f"  - [{status}] {exp.get('action', 'N/A')} "
                    f"(置信度: {exp.get('confidence', 0):.2f})"
                )
            exp_section = f"\n【同类问题的历史修复记录】\n{chr(10).join(exp_lines)}"

    warn_section = ""
    if failure_warnings:
        warn_section = f"\n【已知失败场景】（避免重蹈覆辙）\n{chr(10).join(f'  - {w}' for w in failure_warnings)}"

    stats_section = ""
    if pattern_stats:
        stats_section = f"\n【模式统计】\n  成功率: {pattern_stats.get('success_rate', 'N/A')}\n  平均置信度: {pattern_stats.get('avg_confidence', 'N/A')}"

    prompt = f"""你是一个专业的 Python 代码修复专家。请修复以下代码问题。

【问题类型】{issue.type}
【问题描述】{issue.description}
【修复建议】{issue.suggestion or '无'}

【问题位置】文件: {issue.file}, 行: {issue.line}
【上下文（→ 标记问题行）】
```python
{context_code}
```
{exp_section}{warn_section}{stats_section}

【完整文件内容】
```python
{file_content[:6000]}
```

请生成 **3~5 个不同的修复方案**，返回如下 JSON：
{{
  "candidates": [
    {{
      "action": "修复动作描述",
      "line_range": [开始行号, 结束行号],
      "replacement": "替换后的代码文本（保持原始缩进）",
      "confidence": 0.0~1.0,
      "explanation": "为什么这样修复"
    }}
  ]
}}

关键要求：
1. **不要返回完整文件！** 只返回需要被替换的行范围和替换内容
2. line_range 是 1-indexed，包含起始行到结束行所有内容将被替换为 replacement
3. **尽量最小化修改** — 只改有问题的那一行或附近几行
4. 不要改 import、不要改函数签名、不要改字符串格式、不要加注释
5. 如果不确定怎么修，返回 line_range=[当前行号, 当前行号], replacement="当前行原本代码"
"""
    return prompt


def _parse_llm_response_multiple(response: str) -> list[dict]:
    """解析 LLM 返回的 JSON，提取所有候选修复方案（行范围替换格式）。

    Returns:
        候选方案列表，每个含 action/line_range/replacement/confidence/explanation
    """
    text = response.strip()
    logger.debug("LLM 响应 (前 300): %s", text[:300])

    candidates = []

    try:
        obj = json.loads(text)
        # 标准格式：{"candidates": [...]}
        if "candidates" in obj and isinstance(obj["candidates"], list):
            for item in obj["candidates"]:
                if isinstance(item, dict) and "replacement" in item and "line_range" in item:
                    candidates.append(item)
            if candidates:
                return candidates
        # 也可能是直接返回 JSON 对象数组（没有 candidates 包裹）
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and "replacement" in item and "line_range" in item:
                    candidates.append(item)
            if candidates:
                return candidates
    except json.JSONDecodeError:
                logging.exception('异常捕获: ')

    return candidates


def _select_best_candidate(
    candidates: list[dict],
    original_content: str,
    issue_line: int,
) -> Optional[dict]:
    """从多个候选方案中选出最优的。

    评分维度：
      1. 语法验证（ast.parse 替换后的完整文件）
      2. 改动范围最小（line_range 越小越好）
      3. LLM 自评置信度最高

    Returns:
        最优候选方案，或 None
    """
    import ast

    scored = []
    orig_lines = original_content.split("\n")

    for i, cand in enumerate(candidates):
        lr = cand.get("line_range", [])
        replacement = cand.get("replacement", "")
        if not lr or not replacement:
            continue

        # 生成替换后的完整文件
        try:
            fixed = _apply_line_range_fix(original_content, lr, replacement)
        except (IndexError, ValueError):
            continue

        # 没改动 → 无效候选
        if fixed == original_content:
            continue

        # 语法验证
        try:
            ast.parse(fixed)
            syntax_valid = True
        except SyntaxError:
            syntax_valid = False

        if not syntax_valid:
            continue

        # 计算改动行数
        fix_lines = fixed.split("\n")
        changed_count = abs(len(fix_lines) - len(orig_lines))
        # 加上替换内容自身的行数
        changed_count += replacement.count("\n") + 1

        # 置信度
        confidence = cand.get("confidence", 0.5)
        if isinstance(confidence, str):
            try:
                confidence = float(confidence)
            except ValueError:
                confidence = 0.5

        # 改动范围越小越好：用 line_range 的范围大小
        range_size = lr[1] - lr[0] + 1
        range_penalty = range_size / max(len(orig_lines), 1)

        # 评分：置信度 × 0.5 + 小范围奖励 × 0.5
        score = confidence * 0.5 + (1.0 - min(range_penalty, 1.0)) * 0.5

        logger.debug(
            "  候选 #%d: conf=%.2f range=%d行 range_penalty=%.3f score=%.3f",
            i, confidence, range_size, range_penalty, score,
        )
        scored.append((i, cand, score))

    if not scored:
        return None

    scored.sort(key=lambda x: x[2], reverse=True)
    best = scored[0]

    logger.info(
        "  选择候选 #%d (score=%.3f): %s",
        best[0], best[2], best[1].get("action", ""),
    )
    return best[1]


class LLMFixer:
    """LLM 驱动的智能修复器（兜底修复器）。

    放置在所有规则修复器之后，对规则引擎搞不定的问题
    调用 LLM 生成修复方案。核心流程：
      1. 多候选生成 — 调 LLM 生成 3~5 个修复方案
      2. 评分择优 — 语法验证 + 最小 diff + 置信度评分
      3. 结构化回流 — 修复经验进入 ExperienceStore
    """

    def __init__(self, provider: str = "deepseek", model: str = ""):
        self.name = "llm_fixer"
        self.provider = provider
        self.model = model
        self.supported_types = ["*"]  # 支持所有类型（兜底修复器）

    def fix(self, issue: Issue, project_root: Path) -> FixResult:
        """执行修复。

        Args:
            issue: 待修复的问题
            project_root: 项目根目录

        Returns:
            FixResult
        """
        target_file = project_root / issue.file

        if not target_file.exists():
            logger.error("文件不存在: %s", target_file)
            return FixResult(
                success=False,
                error=f"文件不存在: {target_file}",
                fixer=self.name,
                issue_type=issue.type,
                file=str(issue.file),
                line=issue.line,
            )

        file_content = target_file.read_text(encoding="utf-8")
        issue_line = issue.line or 1

        # 提取问题行附近代码（用于语义检索）
        lines = file_content.split("\n")
        issue_idx = max(0, min(issue.line - 1, len(lines) - 1))
        buggy_start = max(0, issue_idx - 2)
        buggy_end = min(len(lines), issue_idx + 3)
        buggy_code = "\n".join(lines[buggy_start:buggy_end])

        # 加载历史经验（优先语义检索 top-3）
        past_experiences = self._load_experiences(issue.type, buggy_code)
        failure_warnings = self._get_failure_warnings(issue.type)
        pattern_stats = self._get_pattern_stats(issue.type)

        prompt = _build_fix_prompt(
            issue=issue,
            file_content=file_content,
            past_experiences=past_experiences,
            failure_warnings=failure_warnings,
            pattern_stats=pattern_stats,
        )

        logger.info("调用 LLM (%s)...", self.provider)
        try:
            response = _call_llm(prompt, self.provider, self.model)
        except RuntimeError as e:
            return FixResult(
                success=False,
                error=f"LLM API 调用失败: {e}",
                fixer=self.name,
                issue_type=issue.type,
                file=str(issue.file),
                line=issue.line,
            )

        logger.debug("LLM 响应长度: %d 字符", len(response))

        candidates = _parse_llm_response_multiple(response)
        if not candidates:
            logger.warning("LLM 返回格式无法解析 (响应长 %d 字符)", len(response))
            return FixResult(
                success=False,
                error="LLM 返回格式无法解析",
                fixer=self.name,
                issue_type=issue.type,
                file=str(issue.file),
                line=issue.line,
            )

        # 多候选选择
        best = _select_best_candidate(candidates, file_content, issue_line)

        # ── 重试：所有候选失败时再试一次，给 LLM 反馈 ──
        if not best:
            logger.info("所有候选方案未通过验证，重试一次...")
            # 构建重试 prompt：追加失败信息
            retry_hint = (
                f"\n【⚠️ 之前生成的 {len(candidates)} 个候选全部未通过验证】"
                f"\n原因：每行代码替换后的完整文件必须能通过 Python 语法检查（ast.parse）。"
                f"\n要么替换后文件无法通过语法检查，要么替换内容和原内容一致（未做有效修改）。"
                f"\n\n请再生成一组更简单的修复方案："
                f'\n1. 如果当前行没有实质问题，请写出 "有实质问题的行号" 或返回空 fix'
                f'\n2. 如果要插入新行，先插入新行，不要替换原本正确的行'
                f'\n3. 尽量保持原有缩进级别，只改有问题的那一行'
            )
            retry_prompt = prompt + retry_hint
            try:
                retry_response = _call_llm(retry_prompt, self.provider, self.model)
                retry_candidates = _parse_llm_response_multiple(retry_response)
                if retry_candidates:
                    best = _select_best_candidate(retry_candidates, file_content, issue_line)
            except RuntimeError:
                pass

        if not best:
            return FixResult(
                success=False,
                error="所有候选方案均未通过语法验证或未做有效改动",
                confidence=0.0,
                fixer=self.name,
                issue_type=issue.type,
                file=str(issue.file),
                line=issue.line,
            )

        # 应用行范围替换
        line_range = best.get("line_range", [issue_line, issue_line])
        replacement = best.get("replacement", "")

        fixed_code = _apply_line_range_fix(file_content, line_range, replacement)
        if fixed_code == file_content:
            return FixResult(
                success=False,
                action=best.get("action", ""),
                error="修复方案未产生有效改动",
                confidence=0.0,
                fixer=self.name,
                issue_type=issue.type,
                file=str(issue.file),
                line=issue.line,
            )

        # 写入修复后的文件
        target_file.write_text(fixed_code, encoding="utf-8")

        llm_confidence = best.get("confidence", 0.5)
        if isinstance(llm_confidence, str):
            try:
                llm_confidence = float(llm_confidence)
            except ValueError:
                llm_confidence = 0.5

        # 结构化回流：记录 code_before → code_after
        self._record_structured_pattern(
            issue=issue,
            project_root=project_root,
            original_content=file_content,
            fixed_code=fixed_code,
            action=best.get("action", "LLM 生成修复"),
            confidence=min(llm_confidence, 0.85),
        )

        return FixResult(
            success=True,
            action=best.get("action", "LLM 生成修复"),
            confidence=min(llm_confidence, 0.85),
            fixer=self.name,
            issue_type=issue.type,
            file=str(issue.file),
            line=issue.line,
            diff=best.get("explanation", ""),
        )

    # ── 私有辅助方法 ──

    def _record_structured_pattern(
        self,
        issue: Issue,
        project_root: Path,
        original_content: str,
        fixed_code: str,
        action: str,
        confidence: float,
    ) -> None:
        """将修复方案结构化回流到 ExperienceStore。

        供 ExperienceFixer 精确匹配复用（code_before 精确匹配）。
        """
        try:
            from src.core.experience_store import record_experience

            lines = original_content.split("\n")
            issue_idx = max(0, min(issue.line - 1, len(lines) - 1))

            # 提取 code_before（问题行附近 5 行）
            code_start = max(0, issue_idx - 2)
            code_end = min(len(lines), issue_idx + 3)
            code_before = "\n".join(lines[code_start:code_end])

            # 提取 code_after（同样位置）
            fix_lines = fixed_code.split("\n")
            if code_end <= len(fix_lines):
                code_after = "\n".join(fix_lines[code_start:code_end])
            else:
                code_after = code_before

            record_experience(
                issue_type=issue.type,
                file=str(issue.file),
                line=issue.line,
                fixer=self.name,
                action=action,
                confidence=confidence,
                success=True,
                code_snippet=code_before[:300],
                project=str(project_root),
                code_before=code_before,
                code_after=code_after,
            )
            logger.debug(
                "[LLMFixer] 结构化模式已回流: %s (%s:%d) code_before=%d chars, code_after=%d chars",
                issue.type, issue.file, issue.line,
                len(code_before), len(code_after),
            )
        except Exception as e:
            logger.debug("[LLMFixer] 结构化回流失败（不影响修复）: %s", e)

    def _load_experiences(self, issue_type: str, buggy_code: str = "") -> list[dict]:
        """加载同类问题的历史经验。

        优先级：
          1. 语义检索（ExperienceRetriever）— 代码不同但问题语义相似也能命中
          2. 精确匹配（load_experiences 字符串匹配）— 兜底

        Args:
            issue_type: 问题类型
            buggy_code: 问题代码片段（用于语义查询）

        Returns:
            list[dict]，每条含 success/action/confidence/code_before/code_after 等
        """
        # ── 1. 语义检索（优先） ──
        try:
            from src.core.experience_retriever import get_retriever
            retriever = get_retriever()
            if retriever is not None:
                query_text = f"问题类型: {issue_type}。代码: {buggy_code[:200]}"
                results = retriever.search(query_text, top_k=3, min_score=0.3)
                if results:
                    logger.info(
                        "[语义检索] 找到 %d 条相关经验 (top-1 相似度: %.3f)",
                        len(results), results[0].get("similarity_score", 0),
                    )
                    return [
                        {
                            "success": r.get("success", False),
                            "action": r.get("action", ""),
                            "confidence": r.get("confidence", 0.5),
                            "code_before": r.get("code_before", ""),
                            "code_after": r.get("code_after", ""),
                            "similarity_score": r.get("similarity_score", 0.0),
                            "description": r.get("description", ""),
                            "_semantic": True,  # 标记为语义检索结果
                        }
                        for r in results
                    ]
        except Exception as e:
            logger.debug("[LLMFixer] 语义检索不可用，降级到字符串匹配: %s", e)

        # ── 2. 降级：字符串精确匹配 ──
        try:
            from src.core.experience_store import load_experiences
            exps = load_experiences(issue_type=issue_type)
            return [
                {
                    "success": e.get("success", False),
                    "action": e.get("action", ""),
                    "confidence": e.get("confidence", 0.5),
                }
                for e in (exps or [])[-20:]
            ]
        except Exception:
            return []

    def _get_failure_warnings(self, issue_type: str) -> list[str]:
        """获取已知失败模式的警告信息。"""
        try:
            from src.core.experience_store import load_experiences

            exps = load_experiences(issue_type=issue_type)
            failures = [e for e in (exps or []) if not e.get("success", True)]
            return [f.get("action", "未知修复") for f in failures[-5:]]
        except Exception:
            return []

    def _get_pattern_stats(self, issue_type: str) -> dict:
        """获取模式统计信息。"""
        try:
            from src.core.experience_store import load_experiences

            exps = load_experiences(issue_type=issue_type)
            if not exps:
                return {}

            success_count = sum(1 for e in exps if e.get("success", False))
            total = len(exps)
            avg_conf = sum(e.get("confidence", 0) for e in exps) / total if total else 0

            return {
                "success_rate": f"{success_count}/{total} ({success_count / total * 100:.0f}%)" if total else "N/A",
                "avg_confidence": f"{avg_conf:.2f}",
            }
        except Exception:
            return {}
