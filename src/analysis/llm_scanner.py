"""llm_scanner.py — LLM 驱动的智能代码扫描器

替换 deep_enterprise_scanner.py，用 LLM 做语义级代码分析。

核心差异：
  - 深度扫描器：AST + 正则，只能找到表面问题
  - LLM 扫描器：理解代码意图（语义），发现深层问题

覆盖 4 个维度（每模块 2 次 API 调用）：
  1. 架构评审 (architecture) — 模块边界、耦合、职责
  2. 逻辑缺陷 (quality) — 空值、异常、并发、业务逻辑
  3. 安全审计 (security) — 注入、泄露、认证、XSS
  4. 性能分析 (performance) — N+1、缓存、内存泄漏

工作流程：
  1. project_analyzer 分析项目结构
  2. 按模块目录分批
  3. 每批 2 次 LLM 调用（架构+逻辑 / 安全+性能）
  4. 同时跑轻量 AST 扫描（圈复杂度/裸except/吞异常）
  5. 聚合去重，兼容 cmd_scan 输出格式

成本：~$0.15-0.40/次，视项目规模而定

用法：
    from src.analysis.llm_scanner import scan_deep
    result = scan_deep("/path/to/project")

输出格式：
    {
        "score": int,
        "total_issues": int,
        "issue_count": int,       # 同上，兼容两种命名
        "issues": [ { ... } ],
        "by_severity": { ... },
        "files_scanned": int,
        "modules_scanned": int,
        "llm_cost_estimate": float,
    }
"""

import ast
import json
import logging
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# 自动加载 .env
_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env")

from .project_analyzer import analyze_project
from .dataflow.taint_tracker import TaintTracker
from .dataflow.triage import triage_candidates, filter_by_feedback
from .dataflow.feedback import FeedbackStore

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# LLM 响应缓存（确保相同 prompt 返回相同响应，保证扫描可重复性）
# ──────────────────────────────────────────────

_LLM_CACHE = {}  # prompt_hash -> (response, cost)
_CACHE_HIT_COUNT = 0
_CACHE_MISS_COUNT = 0

DEEPSEEK_INPUT_PRICE = 0.27 / 1_000_000    # $0.27 / M tokens
DEEPSEEK_OUTPUT_PRICE = 1.10 / 1_000_000   # $1.10 / M tokens


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文 ~2 tok/字，英文 ~0.75 tok/词）。"""
    # 粗略估算：平均每字符 0.35 token
    return int(len(text) * 0.35)


def _get_llm_cache_key(prompt: str, model: str) -> str:
    """基于 prompt 和 model 生成缓存 key（SHA256 哈希）。"""
    import hashlib
    content = f"{model}::{prompt}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _call_llm(prompt: str, model: str = "deepseek-chat", temperature: float = 0.0) -> tuple[str, float]:
    """调用 DeepSeek API（带缓存确保可重复性）。

    Returns:
        (response_text, estimated_cost_usd)
    """
    global _CACHE_HIT_COUNT, _CACHE_MISS_COUNT

    # 检查缓存
    cache_key = _get_llm_cache_key(prompt, model)
    if cache_key in _LLM_CACHE:
        _CACHE_HIT_COUNT += 1
        cached_response, cached_cost = _LLM_CACHE[cache_key]
        logger.debug("LLM 缓存命中 (hit=%d, miss=%d)", _CACHE_HIT_COUNT, _CACHE_MISS_COUNT)
        return cached_response, cached_cost

    _CACHE_MISS_COUNT += 1

    import urllib.request
    import urllib.error
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置")

    base_url = os.environ.get("DEEPSEEK_HOST", "https://api.deepseek.com")
    url = f"{base_url.rstrip('/')}/v1/chat/completions"

    input_tokens = _estimate_tokens(prompt)
    # 预计输出（max_tokens 上限的 60%，实际平均用量）
    output_tokens = min(10000, int(input_tokens * 0.3))
    estimated_cost = input_tokens * DEEPSEEK_INPUT_PRICE + output_tokens * DEEPSEEK_OUTPUT_PRICE

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 16384,
        "response_format": {"type": "json_object"},
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            response = result["choices"][0]["message"]["content"]
            # debug: 记录响应长度和 token 信息
            logger.debug("LLM 响应长度: %d 字符", len(response))
            if "usage" in result:
                usage = result["usage"]
                logger.debug("Token用量: 输入 %d / 输出 %d",
                             usage.get("prompt_tokens", 0),
                             usage.get("completion_tokens", 0))
                actual_cost = (
                    usage.get("prompt_tokens", 0) * DEEPSEEK_INPUT_PRICE
                    + usage.get("completion_tokens", 0) * DEEPSEEK_OUTPUT_PRICE
                )
                # 缓存结果
                _LLM_CACHE[cache_key] = (response, actual_cost)
                return response, actual_cost
            # 缓存结果
            _LLM_CACHE[cache_key] = (response, estimated_cost)
            return response, estimated_cost
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM API error {e.code}: {body[:300]}")
    except Exception as e:
        raise RuntimeError(f"LLM API call failed: {e}")


# ──────────────────────────────────────────────
# 模块分批
# ──────────────────────────────────────────────

def _build_module_batches(project_root: Path, blueprint) -> list[dict]:
    """按模块目录分组文件，构建 LLM 扫描批次。

    每批 = 同一目录下的一组相关 Python 文件。
    跳过 __pycache__、.git 等无关目录。
    """
    source_files = blueprint.get_source_files("python")

    # 按父目录分组
    module_groups: dict[str, list[str]] = defaultdict(list)
    for fp in source_files:
        p = Path(fp)
        try:
            rel = p.relative_to(project_root)
        except ValueError:
            rel = p
        module_dir = str(rel.parent) if str(rel.parent) != "." else "(root)"
        module_groups[module_dir].append(fp)

    batches = []
    for module_dir, files in sorted(module_groups.items()):
        rel_module = Path(module_dir)

        # 跳过无关目录
        skip_parts = {"__pycache__", ".git", ".venv", "venv", "node_modules",
                      "__pypackages__", ".mypy_cache", ".pytest_cache"}
        if any(part in skip_parts for part in rel_module.parts):
            continue
        if module_dir.startswith("."):
            continue

        py_files = sorted(f for f in files if f.endswith(".py"))
        if not py_files:
            continue

        is_test = any("test" in part.lower() for part in rel_module.parts)

        batches.append({
            "module_dir": module_dir,
            "files": py_files,
            "is_test": is_test,
            "file_count": len(py_files),
        })

    return batches


# ──────────────────────────────────────────────
# 文件读取 + 上下文构建
# ──────────────────────────────────────────────

HEADER_LINES = 30       # 架构评审时每个文件截取头部行数
SIG_CONTEXT = 5          # 函数签名上下各几行
MAX_FILE_FULL = 800     # 超过此行数的文件不会全量发送
MAX_BATCH_LINES = 2000  # 每批最多发送多少行代码（控制 token）


def _extract_function_signatures(code: str) -> list[dict]:
    """抽取文件中所有函数/方法的签名和位置。"""
    signatures = []
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = []
                for arg in node.args.args[:5]:  # 前5个参数就够了
                    if arg.arg in ("self", "cls"):
                        continue
                    ann = ""
                    if arg.annotation:
                        ann = ast.dump(arg.annotation)[:30]
                    args.append(f"{arg.arg}: {ann}" if ann else arg.arg)
                if len(node.args.args) > 5:
                    args.append("...")

                has_docstring = (
                    isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                ) if node.body else False

                return_ann = ""
                if node.returns:
                    return_ann = f" -> {ast.dump(node.returns)[:30]}"

                signatures.append({
                    "name": node.name,
                    "line": node.lineno,
                    "args": ", ".join(args),
                    "returns": return_ann,
                    "has_docstring": has_docstring,
                    "decorators": [ast.dump(d)[:20] for d in node.decorator_list],
                })
    except SyntaxError:
                logging.exception('异常捕获: ')
    return signatures


def _format_function_list(signatures: list[dict]) -> str:
    """格式化函数列表为紧凑文本。"""
    lines = []
    for sig in signatures:
        deco = f" @{', '.join(sig['decorators'])}" if sig["decorators"] else ""
        docs = " ✓doc" if sig["has_docstring"] else ""
        lines.append(f"  L{sig['line']:4d}: def {sig['name']}({sig['args']}){sig['returns']}{deco}{docs}")
    return "\n".join(lines)


def _build_module_context(
    batch: dict, project_root: Path
) -> tuple[str, int, float]:
    """构建模块的 LLM 上下文。

    策略：
      - 总行数 < MAX_BATCH_LINES：全量发送所有文件
      - 总行数 >= MAX_BATCH_LINES：紧凑模式（头部 + 函数签名 + 代表性代码）

    Returns:
        (context_text, total_lines, estimated_cost)
    """
    module_dir = batch["module_dir"]

    # 先估算总行数
    file_metadata = []
    total_lines = 0
    for fp in batch["files"]:
        try:
            code = Path(fp).read_text(encoding="utf-8", errors="ignore")
            lines = code.split("\n")
            file_metadata.append({
                "path": fp,
                "code": code,
                "line_count": len(lines),
                "rel_path": str(Path(fp).relative_to(project_root))
                if project_root in Path(fp).parents
                else Path(fp).name,
            })
            total_lines += len(lines)
        except Exception:
                        logging.exception('异常捕获: ')

    use_compact = total_lines > MAX_BATCH_LINES

    sections = []
    if use_compact:
        # 紧凑模式：头部 + 函数签名
        sections.append(f"# 模块: {module_dir}（紧凑模式，{len(file_metadata)} 文件，共 ~{total_lines} 行）\n")
        for meta in file_metadata:
            code_lines = meta["code"].split("\n")
            header = "\n".join(code_lines[:HEADER_LINES])
            sigs = _extract_function_signatures(meta["code"])
            func_list = _format_function_list(sigs)

            # 找最长（最代表性的）函数
            longest_func = ""
            longest_len = 0
            for sig in sigs:
                # 找对应函数的实际代码
                func_node = _find_function_node(meta["code"], sig["name"])
                if func_node:
                    start = max(0, func_node.lineno - 1)
                    end = min(len(code_lines), func_node.end_lineno or func_node.lineno + 50)
                    func_len = end - start
                    if func_len > longest_len and func_len <= 80:
                        longest_func = "\n".join(code_lines[start:end])
                        longest_len = func_len

            section = f"""
## {meta['rel_path']} ({meta['line_count']} 行)
```python
{header}
```

**函数/方法:**
{func_list if func_list else '  (无函数定义)'}
"""
            if longest_func:
                section += f"""
**代表性函数:**
```python
{longest_func}
```
"""
            sections.append(section)
    else:
        # 全量模式
        sections.append(f"# 模块: {module_dir}（{len(file_metadata)} 文件，共 ~{total_lines} 行）\n")
        for meta in file_metadata:
            # 如果单文件太大，截断
            code = meta["code"]
            if meta["line_count"] > MAX_FILE_FULL:
                lines = code.split("\n")
                code = "\n".join(lines[:MAX_FILE_FULL] + [f"# ... (截断，实有 {meta['line_count']} 行)"])

            section = f"""
## {meta['rel_path']} ({meta['line_count']} 行)
```python
{code}
```
"""
            sections.append(section)

    context_text = "\n".join(sections)
    return context_text, total_lines


def _find_function_node(code: str, func_name: str) -> Optional[ast.AST]:
    """根据函数名查找 AST 节点。"""
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                return node
    except SyntaxError:
                logging.exception('异常捕获: ')
    return None


# ──────────────────────────────────────────────
# Prompt 构建
# ──────────────────────────────────────────────

# ── 维度定义 ──
DIMENSION_DEFS = {
    "architecture": (
        "架构评审 (dimension: architecture)",
        """关注跨文件、跨模块的结构性问题：
- 模块边界是否清晰，职责是否单一（单一职责原则）
- 是否存在过度耦合或循环依赖
- 接口抽象是否合理，是否过度设计或抽象不足
- 分层是否清晰（controller/service/repository 或其他分层风格）
- 数据流向是否合理
- 是否存在"上帝类"或"万能函数"""
    ),
    "quality": (
        "逻辑缺陷 (dimension: quality)",
        """关注代码级别的 Bug 和隐患：
- **空值安全**：是否有未经检查的 None/null/空数组直接引用
- **条件错误**：条件判断是否遗漏、反写、永远不成立
- **异常处理**：是否吞没异常、捕获范围过大、finally 遗漏清理
- **资源管理**：文件/连接/锁是否正确释放
- **并发问题**：竞态条件、共享状态未保护、死锁风险
- **边界条件**：off-by-one、空列表/空字典处理、极端输入
- **业务逻辑错误**：算法错误、状态机遗漏状态、类型误用"""
    ),
    "security": (
        "安全审计 (dimension: security)",
        """关注实际安全风险（**不是正则匹配关键词的水平**）：
- **注入攻击**：SQL 注入、命令注入、模板注入
- **认证与授权**：认证绕过、越权访问、session 固定
- **敏感信息泄露**：硬编码密钥/token、日志泄密
- **输入验证**：用户输入是否经过合理验证和清理
- **不安全的反序列化**：pickle/json 解析
- **CSRF/XSS**：Web 端点保护、输出转义
- **SSRF**：服务器请求用户可控 URL 的限制
- **依赖风险**：危险函数使用（不止 eval/exec）"""
    ),
    "performance": (
        "性能分析 (dimension: performance)",
        """关注实际性能瓶颈（**不是数变量名长度**）：
- **N+1 查询**：循环内调用 DB/API
- **重复计算**：循环内不变计算未提取
- **大对象处理**：大文件全量读入内存
- **不必要的同步**：同步 IO 阻塞 async
- **数据结构不当**：set 替代 list in 检查
- **连接泄漏**：HTTP/DB/文件句柄未复用
- **串行瓶颈**：可并行的任务串行
- **内存泄漏**：缓存持续增长无淘汰"""
    ),
}


def _build_module_prompt(
    blueprint,
    batch: dict,
    context_text: str,
    total_lines: int,
    dimensions: list[str] | None = None,
) -> str:
    """为单个模块批次构建 LLM 分析 prompt。

    Args:
        dimensions: 要分析的维度列表，默认 ["architecture", "quality"]
    """
    if dimensions is None:
        dimensions = ["architecture", "quality"]

    module_dir = batch["module_dir"]
    is_test = batch["is_test"]

    dim_sections = []
    for i, dim in enumerate(dimensions, 1):
        if dim in DIMENSION_DEFS:
            name, body = DIMENSION_DEFS[dim]
            dim_sections.append(f"### 维度 {i}：{name}\n\n{body}")

    prompt_header = f"""你是一个专业的代码评审专家。请分析以下项目的一个模块。

## 项目背景
- 项目名称: {blueprint.project_name}
- 主要语言: {blueprint.language.primary}
- 检测到框架: {', '.join(blueprint.language.frameworks) or '无'}

## 模块信息
- 模块路径: {module_dir}
- 文件数量: {batch['file_count']} 个，共约 {total_lines} 行

## 源代码
{context_text}

## 分析要求

请从以下 **{len(dimensions)} 个维度** 深入分析代码：

"""

    prompt_body = "\n\n".join(dim_sections) + "\n"

    prompt_output = """## 输出要求（重要！）

你的响应必须是 **JSONL 格式**（每行一个独立 JSON 对象，不要用数组包裹）。

每个问题一行，格式如下：
```
{"type": "missing_null_check", "dimension": "quality", "severity": "high", "file": "相对文件路径", "line": 42, "description": "变量 x 解引用前未检查 None", "suggestion": "添加 if x is None 检查", "confidence": 0.9, "related_files": []}
```

字段说明：
- `type`: snake_case 标识，如 `missing_null_check`, `circular_dependency`, `wrong_comparison`
- `dimension`: 只能是 `"architecture"` `"quality"` `"security"` `"performance"` 之一
- `severity`: 只能是 `"critical"`, `"high"`, `"medium"`, `"low"`
- `file`: 相对于项目根目录的路径
- `line`: 整数行号（0 表示跨文件问题）
- `description`: 中文简洁描述（10-30字）
- `suggestion`: 中文修复建议（10-30字）
- `confidence`: 0.0-1.0，不确定的给低分
- `related_files`: 如果问题跨文件，列出相关文件路径

## 重要规则
1. ⚠️ **每行必须是可独立解析的合法 JSON 对象，不要输出数组包裹**
2. **只报告真实问题** — 不确定的降低 confidence 到 0.5 以下
3. **不要报告 lint 级别问题**（缩进、命名风格、import 顺序等）
4. **没有发现任何问题时不输出任何内容**
5. 注意：测试代码不需要太严格的架构评审，但逻辑缺陷仍需检查
6. ⚠️ **禁止刷屏**：同个文件中同种类型的问题最多报告 3 条，且每条必须有具体行号（line 不能为 0）。如果多行都有同样问题，只报最典型的 3 处
7. ⚠️ **missing_await 只报真实阻塞**：函数调用了其他 async 函数但未 await 才算；纯同步操作（print/sleep/文件IO）不算
8. ⚠️ **sql_injection 只报真实拼接**：使用了参数化查询（%s/?）的文件不要报 sql_injection
"""

    prompt = prompt_header + prompt_body + prompt_output

    return prompt


# ──────────────────────────────────────────────
# 响应解析
# ──────────────────────────────────────────────

def _parse_llm_response(response: str) -> list[dict]:
    """解析 LLM 返回的 issues。优先 JSONL（逐行），兼容旧 JSON 数组格式。"""
    text = response.strip()

    # 去掉 ```json ... ``` 包裹
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if json_match:
        text = json_match.group(1).strip()

    # 策略 1: JSONL — 逐行解析，每行一个独立 JSON 对象
    issues = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        # 去掉行尾可能的逗号（LLM 偶尔会加）
        if line.endswith(","):
            line = line[:-1]
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "type" in obj:
                issues.append(obj)
        except json.JSONDecodeError:
            continue  # 跳过非 JSON 行（可能是 markdown 说明文字）

    if issues:
        return issues

    # 策略 2: 兼容旧格式 — LLM 可能仍返回 {"issues": [...]} 或 [...]
    result = _try_parse_json(text)
    if result is not None:
        return result

    # 策略 3: 提取数组区域（截断恢复）
    array_start = text.find('[')
    array_end = text.rfind(']')
    if array_start >= 0:
        if array_end > array_start:
            result = _try_parse_json(text[array_start:array_end + 1])
            if result is not None:
                return result
        result = _try_recover_truncated_array(text, array_start)
        if result is not None:
            return result

    # 策略 4: {"issues": [...]} 截断恢复
    brace_start = text.find('{')
    if brace_start >= 0:
        result = _try_recover_truncated_top_object(text, brace_start)
        if result is not None:
            return result

    # 全部失败
    if text.strip():
        logger.warning("无法解析 LLM 响应 (前 300 字符): %s", text[:300])
    return []


def _try_parse_json(text: str):
    """尝试解析 JSON, 返回 issues list 或 None。
    优先从 markdown 代码块提取；逐行strip注释；容错尾逗号和单引号。"""
    import re

    # 1. 从 markdown 代码块提取
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # 2. 提取 [...] 或 {...}
    arr_m = re.search(r"\[[\s\S]*\]", text)
    obj_m = re.search(r"\{[\s\S]*\}", text)
    candidate = arr_m.group(0) if arr_m else (obj_m.group(0) if obj_m else text)

    # 3. 尝试直接解析
    for raw in (candidate, text):
        try:
            result = json.loads(raw)
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                for key in ("issues", "results", "findings", "problems", "data"):
                    if key in result and isinstance(result[key], list):
                        return result[key]
                return [result]
        except Exception:
            logger.exception("JSON解析失败，raw=%s", raw)

    # 4. 逐行清理后重试（去尾逗号、行注释）
    lines = candidate.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.split("//")[0] if "//" in line else line
        # 去掉行尾逗号后紧跟 } 或 ]
        stripped = re.sub(r",\s*([}\]])", r"\1", stripped)
        cleaned_lines.append(stripped)
    cleaned = "\n".join(cleaned_lines)
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("issues", "results", "findings", "problems", "data"):
                if key in result and isinstance(result[key], list):
                    return result[key]
            return [result]
    except Exception:
        logging.exception("Failed to parse JSON response")

    # 5. 兜底：逐个提取完整 {...} 对象（兼容旧格式截断）
    arr_pos = candidate.find("[")
    if arr_pos >= 0:
        recovered = _try_recover_truncated_array(candidate, arr_pos)
        if recovered:
            return recovered

    return None


def _try_recover_truncated_array(text: str, array_start: int):
    """尝试修复截断的 JSON 数组, 提取所有完整 issue 对象。"""
    try:
        # 从 [ 开始, 提取所有完整对象
        bracket_depth = 0
        issues = []
        current_obj_start = None
        i = array_start
        while i < len(text):
            ch = text[i]
            if ch == '{':
                if bracket_depth == 0:
                    current_obj_start = i
                bracket_depth += 1
            elif ch == '}':
                bracket_depth -= 1
                if bracket_depth == 0 and current_obj_start is not None:
                    obj_text = text[current_obj_start:i + 1]
                    try:
                        obj = json.loads(obj_text)
                        issues.append(obj)
                    except Exception:
                                                logging.exception('异常捕获: ')
                    current_obj_start = None
            elif ch == '[':
                if bracket_depth > 0:
                    bracket_depth += 1
            elif ch == ']':
                bracket_depth -= 1
                if bracket_depth == 0:
                    break  # 正常结束
            i += 1
        if issues:
            logger.debug("从截断的 JSON 中恢复了 %d 个 issue", len(issues))
            return issues
    except Exception:
                logging.exception('异常捕获: ')
    return None


def _try_recover_truncated_top_object(text: str, brace_start: int):
    """尝试修复截断的顶层 JSON 对象 ({"issues": [...]})。"""
    try:
        # 找到 issues 数组的开始
        issues_marker = text.find('"issues"', brace_start, brace_start + 100)
        if issues_marker < 0:
            return None
        colon = text.find(':', issues_marker)
        if colon < 0:
            return None
        arr_start = text.find('[', colon)
        if arr_start < 0:
            return None
        # 提取数组区域
        return _try_recover_truncated_array(text, arr_start)
    except Exception:
                logging.exception('异常捕获: ')
    return None


def _recover_issues_from_truncated(text: str) -> list[dict]:
    """暴力恢复：从截断的 JSON 中提取所有完整的 issue 对象。
    
    兼容 {"issues": [obj1, obj2, ...]} 截断的情况。
    Issue 对象位于深度 1（外层 { + 数组内部 {）。
    """
    issues = []
    depth = 0  # 当前括号深度
    issue_start = None  # 当前 issue 对象的 { 位置
    i = 0
    in_string = False
    escape = False

    while i < len(text):
        ch = text[i]

        if escape:
            escape = False
            i += 1
            continue

        if ch == '\\':
            escape = True
            i += 1
            continue

        if ch == '"':
            in_string = not in_string
            i += 1
            continue

        if in_string:
            i += 1
            continue

        if ch == '{':
            if depth == 1:
                # 数组内对象的开始
                issue_start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 1 and issue_start is not None:
                # 数组内对象结束，尝试解析
                obj_text = text[issue_start:i + 1]
                try:
                    obj = json.loads(obj_text)
                    if isinstance(obj, dict) and 'type' in obj:
                        issues.append(obj)
                except Exception:
                                        logging.exception('异常捕获: ')
                issue_start = None
            elif depth == 0:
                # 回到了顶层，可能整个 JSON 结束了
                pass

        i += 1

    # 如果 JSON 正常结束（有外层 }），depth 回到 0
    # 如果截断，depth 可能 > 0
    return issues


# ──────────────────────────────────────────────
# AST 轻量扫描
# ──────────────────────────────────────────────

def _calc_cyclomatic_complexity(node: ast.AST) -> int:
    """计算圈复杂度（McCabe）。"""
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor)):
            complexity += 1
        elif isinstance(child, ast.Try):
            complexity += len(child.handlers)
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1
        elif isinstance(child, (ast.ExceptHandler, ast.With, ast.AsyncWith)):
            complexity += 1
    return complexity


def _run_light_ast_pass(project_root: Path, blueprint) -> list[dict]:
    """轻量 AST 扫描 — 处理 LLM 不擅长但确定性强的问题。

    这些检查 AST 做又快又准，没必要浪费 LLM token：
      - 圈复杂度超标
      - 裸 except
      - 空的异常捕获（吞没异常）
    """
    issues = []

    for fp in blueprint.get_source_files("python"):
        try:
            code = Path(fp).read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(code)
            rel_path = str(Path(fp).relative_to(project_root))
        except (SyntaxError, UnicodeDecodeError, ValueError) as e:
            logging.exception("解析文件 %s 时出错", fp)
            continue

        # --- 圈复杂度 ---
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cc = _calc_cyclomatic_complexity(node)
                if cc > 20:
                    issues.append({
                        "type": "high_cyclomatic_complexity",
                        "dimension": "quality",
                        "severity": "medium",
                        "file": rel_path,
                        "line": node.lineno,
                        "description": f"函数 '{node.name}' 圈复杂度 {cc}，严重超过建议值 15",
                        "suggestion": "拆分函数，每个函数只包含一个逻辑层级",
                        "confidence": 0.95,
                    })

        # --- 裸 except ---
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                issues.append({
                    "type": "bare_except",
                    "dimension": "quality",
                    "severity": "high",
                    "file": rel_path,
                    "line": node.lineno,
                    "description": "裸 except 会捕获 SystemExit 和 KeyboardInterrupt",
                    "suggestion": "指定具体异常类型：except SomeError:",
                    "confidence": 0.98,
                })

        # --- 空的 except ---
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if not node.body or (
                    len(node.body) == 1
                    and isinstance(node.body[0], ast.Pass)
                ):
                    exc_name = "?"
                    if node.type and isinstance(node.type, ast.Name):
                        exc_name = node.type.id
                    issues.append({
                        "type": "swallowed_exception",
                        "dimension": "quality",
                        "severity": "high",
                        "file": rel_path,
                        "line": node.lineno,
                        "description": f"异常 {exc_name} 被空 except 块吞没，无法追踪错误",
                        "suggestion": "至少添加 logging.exception() 记录异常",
                        "confidence": 0.95,
                    })

    return issues



# ──────────────────────────────────────────────
# Issue Type 标准化
# ──────────────────────────────────────────────

# LLM 可能用不同名称描述同一类问题，统一映射到标准 type
_TYPE_NORMALIZE_MAP = {
    # null 检查
    "null_check": "missing_null_check",
    "null_safety": "missing_null_check",
    "none_check": "missing_null_check",
    "missing_none_check": "missing_null_check",
    "unhandled_none": "missing_null_check",
    # 类型注解
    "type_annotation": "missing_param_type",
    "missing_annotation": "missing_param_type",
    "missing_type_hint": "missing_param_type",
    "type_hint_missing": "missing_param_type",
    "missing_return_annotation": "missing_return_type",
    "return_type_missing": "missing_return_type",
    # 异常处理
    "broad_except": "bare_except",
    "catch_all": "bare_except",
    "exception_swallowing": "swallowed_exception",
    "swallowed_error": "swallowed_exception",
    "silent_exception": "swallowed_exception",
    # 资源管理
    "unclosed_resource": "resource_not_managed",
    "resource_leak": "resource_not_managed",
    "missing_context_manager": "resource_not_managed",
    "file_not_closed": "resource_not_managed",
    # 打印
    "debug_print": "print_used",
    "print_statement": "print_used",
    # 超时
    "missing_timeout": "missing_timeout_config",
    "no_timeout": "missing_timeout_config",
    # 死代码
    "unreachable_code": "dead_code",
    "unused_code": "dead_code",
    "unreachable": "dead_code",
    # 导入
    "unused_imports": "unused_import",
    "import_not_used": "unused_import",
    # 硬编码
    "hardcoded_path": "hardcoded_config",
    "hardcoded_url": "hardcoded_config",
    "hardcoded_value": "hardcoded_config",
    # SQL 注入
    "sql_inject": "sql_injection",
    "sqli": "sql_injection",
    # 命令注入
    "command_inject": "command_injection",
    "cmd_injection": "command_injection",
    "shell_injection": "command_injection",
    # 网络
    "missing_ssl_verify": "missing_tls_verification",
    "ssl_disabled": "missing_tls_verification",
    "verify_false": "missing_tls_verification",
    # 性能
    "n_plus_one": "n_plus_one_query",
    "n+1": "n_plus_one_query",
    "sync_blocking": "sync_blocking_in_async",
    "blocking_in_async": "sync_blocking_in_async",
}

# 标准 type 白名单
_VALID_TYPES = {
    "missing_null_check", "missing_param_type", "missing_return_type",
    "bare_except", "swallowed_exception", "resource_not_managed",
    "print_used", "missing_timeout_config", "dead_code", "unused_import",
    "hardcoded_config", "hardcoded_secret", "secret_leak",
    "sql_injection", "command_injection", "path_traversal",
    "missing_tls_verification", "missing_input_validation",
    "n_plus_one_query", "sync_blocking_in_async", "sync_io_in_async",
    "sync_sleep_in_async", "sync_wrapper_raises",
    "circular_dependency", "god_class", "high_coupling",
    "low_cohesion", "missing_docstring", "complex_function",
    "magic_number", "duplicate_code", "wrong_comparison",
    "off_by_one", "type_confusion", "unvalidated_redirect",
    "weak_crypto", "missing_rate_limit", "missing_error_handling",
    "configuration_error", "missing_index", "slow_query",
    "memory_leak", "cache_missing", "architectural_violation",
}


def _normalize_issues(issues: list[dict]) -> list[dict]:
    """标准化 issue_type + 过滤低信心度 + 去除无效 type。"""
    normalized = []
    for issue in issues:
        itype = issue.get("type", "")
        # 标准化 type
        itype = _TYPE_NORMALIZE_MAP.get(itype, itype)
        issue["type"] = itype
        # 过滤无效 type
        if itype not in _VALID_TYPES:
            continue
        # 过滤低信心度
        conf = issue.get("confidence", 0.5)
        if conf < 0.8:
            continue
        normalized.append(issue)
    return normalized


# ──────────────────────────────────────────────
# 智能去重
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# 智能去重
# ──────────────────────────────────────────────

def _smart_dedup(issues: list[dict]) -> list[dict]:
    """智能去重：按 (file, type, dimension) 分组，行邻近合并。

    策略：
      1. 按 (file, type, dimension) 分组
      2. 同组内，行号相近（|line1-line2| <= 3）→ 合并为一条（保留最小行号）
      3. 同组内不同行号的按描述合并（相同描述优先）
      4. 每个 (file, type) 最多保留 3 条（防止刷屏）
      5. sql_injection 假阳性降 confidence < 0.3
    """
    from collections import defaultdict

    # 第一步：按 (file, type, dimension) 分组
    groups = defaultdict(list)
    for issue in issues:
        key = (issue.get("file", ""), issue.get("type", ""), issue.get("dimension", ""))
        groups[key].append(issue)

    result = []
    total_before = len(issues)

    # 按 key 排序确保遍历顺序确定（同项目两次扫描结果一致）
    for key, group in sorted(groups.items()):
        file, itype, _ = key

        # ── sql_injection 假阳性过滤 ──
        if itype == "sql_injection":
            try:
                target_path = Path(_smart_dedup._last_project_root or "") / file
                if target_path.exists():
                    _content = target_path.read_text(encoding="utf-8", errors="ignore")
                    param_patterns = [r"%s", r"\?", r":param", r"execute\(.*,\s*\("]
                    _uses_params = any(re.search(p, _content) for p in param_patterns)
                    _has_fstring_sql = bool(re.search(
                        r"f['\"].*\b(select|insert|update|delete)\b.*\{.*\}.*['\"]", _content, re.I
                    ))
                    if _uses_params and not _has_fstring_sql:
                        for _i in group:
                            _i["confidence"] = min(_i.get("confidence", 0.5), 0.2)
            except Exception:
                pass

        # ── 按行号排序 ──
        group.sort(key=lambda x: x.get("line", 0))

        # ── 行邻近聚类合并 ──
        clusters = []
        current = None
        for issue in group:
            line = issue.get("line", 0)
            if current is None:
                current = [issue]
            else:
                last_line = current[-1].get("line", 0)
                # 同 line 或 |diff| <= 3 → 合并到当前聚类
                if line == 0 and last_line == 0 or (line > 0 and last_line > 0 and abs(line - last_line) <= 3):
                    current.append(issue)
                elif line == 0 or last_line == 0:
                    # 混合 line=0 和 line>0：如果任一为0，且对方也跨很小，合并
                    # 保守策略：line=0 单独成簇
                    clusters.append(current)
                    current = [issue]
                else:
                    clusters.append(current)
                    current = [issue]
        if current is not None:
            clusters.append(current)

        # ── 每个聚类合并为一条 ──
        for cluster in clusters:
            if len(cluster) <= 1:
                result.append(cluster[0])
                continue

            # 合并：取第一个，合并描述
            merged = dict(cluster[0])
            lines_in_cluster = [i.get("line", 0) for i in cluster]
            valid_lines = [l for l in lines_in_cluster if l > 0]
            if valid_lines:
                merged["line"] = min(valid_lines)
            else:
                merged["line"] = 0

            # 合并描述（取出现最多的描述）
            from collections import Counter
            descs = [i.get("description", "") for i in cluster if i.get("description", "")]
            if descs:
                most_common_desc = Counter(descs).most_common(1)[0][0]
                merged["description"] = f"{most_common_desc}（共 {len(cluster)} 处同类）"

            # 合并 suggestion（取出现最多的）
            suggs = [i.get("suggestion", "") for i in cluster if i.get("suggestion", "")]
            if suggs:
                merged["suggestion"] = Counter(suggs).most_common(1)[0][0]

            result.append(merged)

    # ── 最后补一道 cap：每个 (file, type) 最多 1 条（去重稳定性） ──
    seen = {}  # (file, type) → count
    capped = []
    for issue in result:
        ft_key = (issue.get("file", ""), issue.get("type", ""))
        c = seen.get(ft_key, 0)
        if c < 1:
            seen[ft_key] = c + 1
            capped.append(issue)
        else:
            logger.debug(f"  cap 掉 {ft_key} 的第 {c+1} 条")

    removed = total_before - len(capped)
    if removed > 0:
        logger.info(f"  智能去重: 合并移除 {removed} 个同类问题")

    return capped


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

def scan_deep(project_root: str | Path, skip_tests: bool = True) -> dict:
    """LLM 驱动的深度扫描入口函数。

    替换 deep_enterprise_scanner.scan_deep()，完全兼容 cmd_scan 输出格式。

    Args:
        project_root: 目标项目根目录
        skip_tests: 是否跳过测试模块（默认 True，控制成本）

    Returns:
        标准扫描结果 dict
    """
    # 为 _smart_dedup 的 sql_injection 过滤提供路径
    _smart_dedup._last_project_root = str(project_root)
    project_root = Path(project_root).resolve()
    if not project_root.exists():
        return {
            "score": 0, "total_issues": 0, "issue_count": 0,
            "issues": [], "by_severity": {},
            "files_scanned": 0, "modules_scanned": 0,
            "error": f"路径不存在: {project_root}",
        }

    # ── Phase 1: 项目分析 ──
    logger.info("📋 分析项目结构...")
    try:
        blueprint = analyze_project(str(project_root))
    except Exception as e:
        logger.error("项目分析失败: %s", e)
        return {
            "score": 0, "total_issues": 0, "issue_count": 0,
            "issues": [], "by_severity": {},
            "files_scanned": 0, "modules_scanned": 0,
            "error": f"分析失败: {e}",
        }

    source_files = blueprint.get_source_files("python")
    if not source_files:
        logger.info("  未找到 Python 文件，尝试其他语言...")
        # 如果不是 Python 项目，可以扩展
        source_files = [str(f) for f in project_root.rglob("*.py") if f.is_file()]

    logger.info(f"  发现 {len(source_files)} 个 Python 文件")

    # ── Phase 2: AST 轻量扫描 ──
    logger.info("🔧 运行 AST 轻量扫描（圈复杂度 / 裸 except / 吞没异常）...")
    ast_issues = _run_light_ast_pass(project_root, blueprint)
    logger.info(f"  AST 扫描发现 {len(ast_issues)} 个问题")

    # ── Phase 2.5: 数据流筛选 + LLM Triage ──
    # 先用廉价的污点追踪缩小搜索空间，再把候选发给 LLM 精判
    # 这比"把整个项目扔给 LLM"便宜 10 倍
    logger.info("🔗 Phase 2.5: 数据流筛选（污点追踪 → LLM Triage）...")
    tracker = TaintTracker()
    feedback = FeedbackStore()
    all_taint_candidates = []

    for fp in source_files:
        try:
            code = Path(fp).read_text(encoding="utf-8", errors="ignore")
            candidates = tracker.analyze(code, filepath=str(Path(fp).relative_to(project_root)))
            all_taint_candidates.extend(candidates)
        except Exception as e:
            logger.debug("数据流分析跳过 %s: %s", fp, e)

    logger.info("  污点追踪发现 %d 个候选", len(all_taint_candidates))

    dataflow_issues = []
    if all_taint_candidates:
        # 过滤已抑制的误报
        filtered, suppressed = filter_by_feedback(all_taint_candidates, feedback)
        if suppressed:
            logger.info("  反馈循环抑制 %d 个已知误报", suppressed)

        if filtered:
            logger.info("  发送 %d 个候选给 LLM triage...", len(filtered))
            triage_results = triage_candidates(filtered, feedback, call_llm=True)

            for tr in triage_results:
                if tr["judgment"] == "true_positive":
                    cand = tr["candidate"]
                    dataflow_issues.append({
                        "type": f"dataflow_{cand['sink_kind']}",
                        "dimension": "security",
                        "severity": tr.get("severity", "high"),
                        "file": cand["filepath"],
                        "line": cand["sink_line"],
                        "description": f"数据流: {cand['source_kind']} → {cand['sink_kind']} "
                                       f"({cand['tainted_vars']})",
                        "suggestion": tr.get("reason", ""),
                        "confidence": 0.8,
                        "source": "dataflow_triage",
                        "related_files": [],
                    })

            tp_count = sum(1 for t in triage_results if t["judgment"] == "true_positive")
            fp_count = sum(1 for t in triage_results if t["judgment"] == "false_positive")
            logger.info("  Triage 结果: %d 真阳性, %d 误报, %d 其他",
                        tp_count, fp_count, len(triage_results) - tp_count - fp_count)

    # ── Phase 3: 分批 LLM 扫描（架构+质量+性能维度）──
    # 注意：安全维度的高置信候选已被 Phase 2.5 覆盖，这里只补扫架构/质量/性能
    batches = _build_module_batches(project_root, blueprint)
    non_test_batches = [b for b in batches if not b["is_test"]]
    test_batches = [b for b in batches if b["is_test"]] if not skip_tests else []

    logger.info(f"  🤖 项目分为 {len(batches)} 个模块（test 模块: {sum(1 for b in batches if b['is_test'])}）")
    if skip_tests:
        logger.info("  跳过测试模块（skip_tests=True）")

    llm_issues = []
    total_cost = 0.0
    scan_batches = non_test_batches + (test_batches if not skip_tests else [])

    # 如果数据流已经找到安全候选并做了 triage，Phase 3 只扫架构/质量/性能
    if dataflow_issues:
        DIM_GROUPS = [
            ["architecture", "quality"],
            ["performance"],
        ]
        logger.info("  安全维度已由数据流 triage 覆盖，Phase 3 只补扫架构/质量/性能")
    else:
        DIM_GROUPS = [
            ["architecture", "quality"],
            ["security", "performance"],
        ]

    # ── 并行扫描模块（ThreadPoolExecutor）──
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    _cost_lock = threading.Lock()

    def _scan_one_module(batch_info: tuple[int, dict]) -> list[dict]:
        """扫描单个模块的所有维度组，返回该模块的 issue 列表。"""
        if batch_info is None:
            return []
        idx, batch = batch_info
        module_dir = batch["module_dir"]
        logger.info(f"  [{idx}/{len(scan_batches)}] 扫描模块: {module_dir} ({batch['file_count']} 文件)")

        try:
            context_text, total_lines = _build_module_context(batch, project_root)
        except Exception as e:
            logger.warning(f"    ⚠️  构建上下文失败: {type(e).__name__}: {e}")
            return []

        module_issues = []
        for dim_group in DIM_GROUPS:
            dim_label = "/".join(dim_group)
            prompt = _build_module_prompt(
                blueprint, batch, context_text, total_lines, dimensions=dim_group
            )
            try:
                response, cost = _call_llm(prompt)
                batch_issues = _parse_llm_response(response)
                with _cost_lock:
                    nonlocal total_cost
                    total_cost += cost

                if batch_issues:
                    for issue in batch_issues:
                        issue.setdefault("confidence", 0.5)
                        issue.setdefault("related_files", [])
                        sev = issue.get("severity", "low")
                        if sev not in ("critical", "high", "medium", "low"):
                            issue["severity"] = "medium"
                    module_issues.extend(batch_issues)
                    logger.info(f"    → [{dim_label}] 发现 {len(batch_issues)} 个问题 (${cost:.5f})")
                else:
                    logger.info(f"    → [{dim_label}] 未发现问题")
            except RuntimeError as e:
                logger.warning(f"    ⚠️  LLM 调用失败 [{dim_label}]: {e}")
            except Exception as e:
                logger.warning(f"    ⚠️  维度扫描异常 [{dim_label}]: {type(e).__name__}: {e}")

        return module_issues

    # 并发数：min(模块数, 4)，避免 API 过载
    max_workers = min(len(scan_batches), 4)
    module_results = {}  # 用 dict 按索引存储结果，确保顺序确定

    if max_workers > 1:
        logger.info(f"  ⚡ 并行扫描（workers={max_workers}）")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_scan_one_module, (i, b)): i
                for i, b in enumerate(scan_batches, 1)
            }
            for future in as_completed(futures):
                batch_idx = futures[future]
                try:
                    module_issues = future.result(timeout=600)
                    module_results[batch_idx] = module_issues
                except Exception as e:
                    logger.warning(f"    ⚠️  模块扫描任务异常: {e}")
                    module_results[batch_idx] = []
    else:
        # 只有1个模块，直接串行
        for item in enumerate(scan_batches, 1):
            idx = item[0]
            module_results[idx] = _scan_one_module(item)

    # 按模块索引顺序合并 issues，确保结果可重复
    for idx in sorted(module_results.keys()):
        llm_issues.extend(module_results[idx])

    logger.info(f"  LLM 扫描发现 {len(llm_issues)} 个问题，数据流发现 {len(dataflow_issues)} 个，总成本 ${total_cost:.4f}")

    # ── Phase 4: 聚合结果 ──
    all_issues = ast_issues + dataflow_issues + llm_issues

    # 按严重程度排序
    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_issues.sort(key=lambda x: (
        SEV_ORDER.get(x.get("severity", "low"), 99),
        x.get("file", ""),
        x.get("line", 0),
    ))

    # ── 智能去重（按 file+type+dimension 分组，合并连续行 / 相同描述）──
    # 标准化 issue_type + 过滤低信心度
    all_issues = _normalize_issues(all_issues)
    # 过滤 line=0 的模糊问题
    all_issues = [i for i in all_issues if i.get("line", 0) > 0]
    all_issues = _smart_dedup(all_issues)

    # 按严重程度统计
    by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in all_issues:
        sev = issue.get("severity", "low")
        if sev in by_severity:
            by_severity[sev] += 1

    # 分数计算：平方根压缩扣分，避免少量高权问题把分数打爆
    weights = {"critical": 15, "high": 8, "medium": 3, "low": 1}
    raw_deduction = sum(weights.get(i.get("severity", "low"), 0) for i in all_issues)
    compressed = 20 * math.sqrt(raw_deduction / 20) if raw_deduction > 0 else 0
    score = max(0, min(100, round(100 - compressed)))

    result = {
        "score": score,
        "total_issues": len(all_issues),
        "issue_count": len(all_issues),
        "issues": all_issues,
        "by_severity": by_severity,
        "files_scanned": len(source_files),
        "modules_scanned": len(scan_batches),
        "llm_cost_estimate": round(total_cost, 6),
        "dataflow_candidates": len(all_taint_candidates),
        "dataflow_issues": len(dataflow_issues),
    }

    # 输出摘要
    logger.info("")
    logger.info("=" * 50)
    logger.info(f"📊 扫描完成: {blueprint.project_name}")
    logger.info(f"  评分: {score}/100")
    logger.info(
        f"  问题: {len(all_issues)} 个"
        f" (🔴 {by_severity['critical']} / 🟠 {by_severity['high']}"
        f" / 🟡 {by_severity['medium']} / 🔵 {by_severity['low']})"
    )
    logger.info(f"  文件: {len(source_files)} 个 / 模块: {len(scan_batches)} 个")
    logger.info(f"  LLM 成本: ${total_cost:.4f}")
    logger.info("=" * 50)

    return result


def llm_issue_to_pipeline_issue(issue_dict: dict, project_root: str = "") -> dict:
    """将 llm_scanner 输出的 issue dict 转为 fix_pipeline 的 Issue dict。

    llm_scanner issue 格式:
        type, dimension, severity, file, line, description,
        suggestion, confidence, related_files

    fix_pipeline Issue 格式:
        type, severity, file, line, description, suggestion,
        scanner (str), context (dict)

    Args:
        issue_dict: llm_scanner 输出的 issue
        project_root: 项目根目录（用于 file 路径标准化）

    Returns:
        pipeline Issue dict（可直接 Issue.from_dict() 使用）
    """
    return {
        "type": issue_dict.get("type", "unknown"),
        "severity": issue_dict.get("severity", "medium"),
        "file": str(Path(issue_dict.get("file", ""))),
        "line": issue_dict.get("line", 0),
        "description": issue_dict.get("description", ""),
        "suggestion": issue_dict.get("suggestion", ""),
        "scanner": f"llm_scanner/{issue_dict.get('dimension', 'quality')}",
        "context": {
            "confidence": issue_dict.get("confidence", 0.5),
            "dimension": issue_dict.get("dimension", "quality"),
            "related_files": issue_dict.get("related_files", []),
        },
    }


def scan_and_get_issues(project_root: str, skip_tests: bool = True) -> list[dict]:
    """扫描并返回 pipeline 兼容的 Issue dict 列表。

    快捷方式：扫描 → 转换 → 按严重程度排序
    """
    result = scan_deep(project_root, skip_tests=skip_tests)
    pipeline_issues = []
    for iss in result.get("issues", []):
        pipeline_issues.append(llm_issue_to_pipeline_issue(iss, project_root))
    # 按严重程度排序
    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    pipeline_issues.sort(key=lambda x: SEV_ORDER.get(x["severity"], 99))
    return pipeline_issues


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    result = scan_deep(target)

    # 打印首屏概要
    print("\n" + json.dumps({
        "score": result["score"],
        "total_issues": result["total_issues"],
        "by_severity": result["by_severity"],
        "files_scanned": result["files_scanned"],
        "modules_scanned": result["modules_scanned"],
        "llm_cost_estimate": result["llm_cost_estimate"],
    }, indent=2, ensure_ascii=False))

    if result["issues"]:
        print("\n前 10 个问题:")
        for i in result["issues"][:10]:
            print(f'  [{i["severity"]:7s}] {i["type"]:35s} {i["file"]}:{i["line"]}')
            print(f'          {i["description"][:80]}')
