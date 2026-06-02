"""layered_scanner.py — 分层扫描管线

与 tiered_scanner.py 的区别：
  tiered_scanner: 降级策略（云端→本地→规则），一次选一级
  layered_scanner: 串联策略，每一层检出结果传给下一层去重

设计动机：
  "为什么用 LLM 做 linter 也能做的事？每层负责自己能做好的：
   - Layer 0 (Ruff) → 代码风格、未用导入、常见陷阱（0 成本）
   - Layer 1 (mypy) → 类型安全、None 解引用（0 成本）
   - Layer 2 (AST规则) → 圈复杂度、裸 except、吞异常（0 成本）
   - Layer 3 (LLM) → 逻辑缺陷、架构评审（有成本，只做前3层做不了的）
   每层结果传给下一层去重，LLM 不会收到"已经扫过的"问题。"

输出格式同 scan_deep():
    {
        "score": int,
        "total_issues": int,
        "issues": [...],
        "by_severity": {...},
        "layers": {...},        # 新增：每层的结果统计
        "llm_cost_estimate": float,
    }
"""

import json
import logging
import math
import subprocess
import sys
import re
import hashlib
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _issue_signature(issue: dict) -> tuple:
    """生成问题指纹，用于跨层去重。

    同 file + 同 type 家族 + 行号接近 → 视为同一问题。
    type 家族映射（避免不同工具用不同命名）：
      - "unused-import" / "unused_import" / "F401" → 同族
      - "missing-return-type" / "missing_return_type" / "ANN201" → 同族
    """
    file = issue.get("file", "")
    itype = issue.get("type", "")

    # type 家族归一化
    type_family = _normalize_type_family(itype)

    line = issue.get("line", 0)
    # 对于 line=0 的问题，用文件级别指纹
    return (file, type_family, line // 3 if line > 0 else 0)


_TYPE_FAMILIES = {
    # 未用导入
    "unused_import": "unused_import",
    "unused-import": "unused_import",
    "F401": "unused_import",
    # 未使用变量
    "unused_variable": "unused_variable",
    "unused-variable": "unused_variable",
    "F841": "unused_variable",
    # 类型注解
    "missing_return_type": "missing_annotation",
    "missing-return-type": "missing_annotation",
    "ANN201": "missing_annotation",
    "ANN202": "missing_annotation",
    "missing_param_type": "missing_annotation",
    "missing-param-type": "missing_annotation",
    "ANN001": "missing_annotation",
    "ANN002": "missing_annotation",
    # 类型错误
    "arg-type": "type_error",
    "return-type": "type_error",
    "assignment": "type_error",
    "operator": "type_error",
    "list-item": "type_error",
    "union-attr": "type_error",
    # None 安全
    "missing_null_check": "none_safety",
    "value-has-type-none": "none_safety",
    "object-none": "none_safety",
    # 异常处理
    "bare_except": "exception_handling",
    "swallowed_exception": "exception_handling",
    "broad-exception-caught": "exception_handling",
    "try-except-raise": "exception_handling",
    "W0702": "exception_handling",
    "W0718": "exception_handling",
    # 死代码/不可达
    "dead_code": "dead_code",
    "unreachable": "dead_code",
    "R100": "dead_code",
    # 硬编码
    "hardcoded_secret": "hardcoded",
    "hardcoded_config": "hardcoded",
    "hardcoded_password": "hardcoded",
    "S105": "hardcoded",
    "S106": "hardcoded",
    "S107": "hardcoded",
    # 危险函数
    "dangerous_function": "dangerous_call",
    "exec_used": "dangerous_call",
    "eval_used": "dangerous_call",
    "B102": "dangerous_call",
    "B307": "dangerous_call",
    # 日志/打印
    "print_used": "logging",
    "print-statement": "logging",
    "T201": "logging",
    "WARN": "logging",
    # SQL 注入
    "sql_injection": "sql_injection",
    "B608": "sql_injection",
    # 命令注入
    "command_injection": "command_injection",
    "subprocess-without-shell": "command_injection",
    "B603": "command_injection",
    "B604": "command_injection",
    # 资源管理
    "resource_not_managed": "resource_management",
    "file-not-closed": "resource_management",
    "with-open": "resource_management",
    # 圈复杂度
    "high_cyclomatic_complexity": "complexity",
    "complex-function": "complexity",
    "C901": "complexity",
    # 函数过长
    "too-many-branches": "complexity",
    "too-many-statements": "complexity",
    "R0912": "complexity",
    "R0915": "complexity",
}


def _normalize_type_family(itype: str) -> str:
    """将不同工具的 type 映射到统一家族。"""
    if itype in _TYPE_FAMILIES:
        return _TYPE_FAMILIES[itype]
    # 无映射则原样返回
    return itype


def _merge_issues(layers: dict) -> list[dict]:
    """合并多层的 issue，按指纹去重，低层优先。

    策略：
      - 生成所有 issue 的指纹 (file, type_family, line_cluster)
      - 按层号排序（低层 = 高优先级）
      - 保留每个指纹的第一条
    """
    seen = set()
    merged = []

    # 按层号从低到高
    for layer_id in sorted(layers.keys()):
        for issue in layers[layer_id]:
            sig = _issue_signature(issue)
            if sig not in seen:
                # 标记来源
                issue["_layer"] = layer_id
                seen.add(sig)
                merged.append(issue)

    return merged


# ──────────────────────────────────────────────
# Layer 0: Ruff linter
# ──────────────────────────────────────────────

def _ruff_available() -> bool:
    """检查 Ruff 是否已安装。"""
    try:
        subprocess.run(
            ["ruff", "--version"],
            capture_output=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Ruff 规则 → issue type 映射
_RUFF_RULE_MAP = {
    "F401": "unused_import",
    "F841": "unused_variable",
    "F821": "type_error",        # undefined name
    "F823": "type_error",        # local variable referenced before assignment
    "E999": "syntax_error",
    "W605": "deprecated_escape",
    "C901": "high_cyclomatic_complexity",
    "T201": "print_used",
    "T203": "print_used",
    "S105": "hardcoded_secret",
    "S106": "hardcoded_secret",
    "S107": "hardcoded_secret",
    "W0702": "bare_except",
    "W0703": "bare_except",
    "W0718": "bare_except",
    "B006": "mutable_default_arg",
    "B007": "unused_variable",    # loop variable unused
    "B009": "dangerous_call",     # getattr with constant
    "B010": "dangerous_call",     # setattr with constant
}

# Ruff 规则 → 严重程度
_RUFF_SEVERITY = {
    "F401": "medium",
    "F841": "medium",
    "F821": "high",
    "F823": "high",
    "E999": "critical",
    "W605": "low",
    "C901": "medium",
    "T201": "low",
    "T203": "low",
    "S105": "high",
    "S106": "high",
    "S107": "high",
    "W0702": "high",
    "W0703": "high",
    "W0718": "high",
    "B006": "medium",
    "B007": "low",
    "B009": "medium",
    "B010": "medium",
}

# Ruff 规则 → 维度
_RUFF_DIMENSION = {
    "F401": "quality",
    "F841": "quality",
    "F821": "quality",
    "F823": "quality",
    "E999": "quality",
    "W605": "quality",
    "C901": "quality",
    "T201": "quality",
    "T203": "quality",
    "S105": "security",
    "S106": "security",
    "S107": "security",
    "W0702": "quality",
    "W0703": "quality",
    "W0718": "quality",
    "B006": "quality",
    "B007": "quality",
    "B009": "security",
    "B010": "security",
}

_RUFF_DESCRIPTION = {
    "F401": "模块导入后未被使用",
    "F841": "局部变量被赋值后未被使用",
    "F821": "变量未定义",
    "F823": "局部变量在赋值前被引用",
    "E999": "语法错误",
    "W605": "使用了已弃用的转义序列",
    "C901": "函数圈复杂度过高",
    "T201": "使用了 print()，应改用 logging",
    "T203": "使用了 print()，应改用 logging",
    "S105": "疑似硬编码密码/密钥",
    "S106": "疑似硬编码密码/密钥",
    "S107": "疑似硬编码密码/密钥",
    "W0702": "裸 except 捕获所有异常",
    "W0703": "裸 except 捕获所有异常",
    "W0718": "裸 except 捕获所有异常",
    "B006": "可变默认参数（列表/字典）",
    "B007": "循环变量未使用",
    "B009": "getattr() 使用字符串字面量",
    "B010": "setattr() 使用字符串字面量",
}


def _run_ruff(target_dir: Path) -> list[dict]:
    """运行 Ruff 扫描，返回标准问题列表。

    只取有映射的高价值规则，过滤掉风格类（E、W、I 系列）。
    每个 issue 带 scanner="ruff" 标记。
    """
    if not _ruff_available():
        logger.info("  Layer 0 (Ruff): 未安装，跳过")
        return []

    logger.info("  Layer 0 (Ruff): 运行中...")

    try:
        result = subprocess.run(
            ["ruff", "check", "--output-format", "json", "--no-cache", str(target_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if result.returncode not in (0, 1):  # 0=clean, 1=issues found
            logger.warning(f"  Ruff 异常退出: {result.stderr[:200]}")
            return []
    except subprocess.TimeoutExpired:
        logger.warning("  Ruff 超时，跳过")
        return []
    except Exception as e:
        logger.warning(f"  Ruff 失败: {e}")
        return []

    issues = []
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("  Ruff 输出解析失败")
        return []

    for item in raw:
        rule_code = item.get("code", "")
        mapped_type = _RUFF_RULE_MAP.get(rule_code)
        if mapped_type is None:
            # 只取有映射的规则，其他规则（风格类）忽略
            continue

        rel_path = item.get("filename", "")
        try:
            rel_path = str(Path(rel_path).relative_to(target_dir))
        except ValueError:
            rel_path = Path(rel_path).name

        sev = _RUFF_SEVERITY.get(rule_code, "medium")
        dim = _RUFF_DIMENSION.get(rule_code, "quality")
        desc = _RUFF_DESCRIPTION.get(rule_code, item.get("message", ""))
        conf = 0.95  # Ruff 是确定性分析

        issues.append({
            "type": mapped_type,
            "dimension": dim,
            "severity": sev,
            "file": rel_path,
            "line": item.get("location", {}).get("row", item.get("row", 0)),
            "description": desc,
            "suggestion": (item.get("fix") or {}).get("message", ""),
            "confidence": conf,
            "scanner": f"ruff/{rule_code}",
        })

    logger.info(f"  Layer 0 (Ruff): 发现 {len(issues)} 个问题")
    return issues


# ──────────────────────────────────────────────
# Layer 1: mypy type checker
# ──────────────────────────────────────────────

def _mypy_available() -> bool:
    """检查 mypy 是否已安装。"""
    try:
        subprocess.run(
            ["mypy", "--version"],
            capture_output=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# mypy error code → issue type
_MYPY_CODE_MAP = {
    "arg-type": "type_error",
    "return-type": "type_error",
    "assignment": "type_error",
    "operator": "type_error",
    "list-item": "type_error",
    "union-attr": "type_error",
    "attr-defined": "type_error",
    "call-arg": "type_error",
    "misc": "type_error",
    "no-any-return": "type_error",
    "no-untyped-def": "missing_return_type",
    "no-untyped-call": "missing_param_type",
    "has-type": "type_error",
    "override": "type_error",
    "abstract": "type_error",
    "truthy-bool": "type_error",
    "comparison-overlap": "type_error",
    "name-defined": "type_error",
    "return-value": "type_error",
    "var-annotated": "type_error",
    "value-has-type-none": "missing_null_check",
}

_MYPY_SEVERITY = {
    "arg-type": "high",
    "return-type": "high",
    "assignment": "high",
    "operator": "high",
    "union-attr": "high",
    "attr-defined": "high",
    "call-arg": "high",
    "value-has-type-none": "high",
    "misc": "medium",
    "no-any-return": "low",
    "no-untyped-def": "low",
    "no-untyped-call": "low",
}


def _parse_mypy_line(line: str) -> Optional[dict]:
    """解析 mypy 单行输出，返回 issue dict 或 None。"""
    # mypy 输出格式: file:line[:col] error|note|warning: message  [error-code]
    pattern = r'^(.+?):(\d+):\s*(error|note|warning):\s+(.+?)(?:\s{2,}\[(.+?)\])?$'
    m = re.match(pattern, line)
    if not m:
        return None

    raw_file = m.group(1)
    line_num = int(m.group(2))
    kind = m.group(3)          # error / note / warning
    message = m.group(4).strip()
    error_code = m.group(5) if m.group(5) else ""

    if kind not in ("error",):
        return None  # 只关心 error 级别

    mapped_type = _MYPY_CODE_MAP.get(error_code, "type_error")
    sev = _MYPY_SEVERITY.get(error_code, "high")
    dim = "security" if mapped_type == "missing_null_check" else "quality"

    # 从 message 中提取更具体的信息
    return {
        "type": mapped_type,
        "dimension": dim,
        "severity": sev,
        "file": raw_file,
        "line": line_num,
        "description": message[:80],
        "suggestion": f"检查类型: {message[:60]}",
        "confidence": 0.92,
        "scanner": f"mypy/{error_code}" if error_code else "mypy",
    }


def _run_mypy(target_dir: Path) -> list[dict]:
    """运行 mypy 类型检查，返回标准问题列表。

    配置：--show-error-codes --no-error-summary --ignore-missing-imports
    """
    if not _mypy_available():
        logger.info("  Layer 1 (mypy): 未安装，跳过")
        return []

    logger.info("  Layer 1 (mypy): 运行中...")

    try:
        # mypy 退出码：0=clean, 1=type errors, 2=解析错误(部分结果仍有效)
        result = subprocess.run(
            [
                "mypy", "--show-error-codes",
                "--no-error-summary",
                "--ignore-missing-imports",
                "--follow-imports", "skip",
                str(target_dir),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if result.returncode not in (0, 1, 2):
            logger.warning(f"  mypy 异常退出: {result.stderr[:200]}")
            return []
        if result.returncode == 2:
            logger.info("  mypy 遇到解析错误（退出码2），但仍可提取部分结果...")
    except subprocess.TimeoutExpired:
        logger.warning("  mypy 超时，跳过")
        return []
    except Exception as e:
        logger.warning(f"  mypy 失败: {e}")
        return []

    issues = []
    for line in result.stdout.splitlines():
        issue = _parse_mypy_line(line)
        if issue is not None:
            try:
                issue["file"] = str(Path(issue["file"]).relative_to(target_dir))
            except ValueError:
                pass  # 保持原始路径
            issues.append(issue)

    logger.info(f"  Layer 1 (mypy): 发现 {len(issues)} 个问题")
    return issues


# ──────────────────────────────────────────────
# Layer 2: AST 规则扫描
# ──────────────────────────────────────────────

def _run_ast_rules(target_dir: Path) -> list[dict]:
    """运行 AST 规则扫描 (LLM 扫描器内的 _run_light_ast_pass)。

    复用 llm_scanner 中的 AST 检查逻辑。
    """
    logger.info("  Layer 2 (AST 规则): 运行中...")

    from src.analysis.project_analyzer import analyze_project
    from src.analysis.llm_scanner import _run_light_ast_pass

    try:
        blueprint = analyze_project(str(target_dir))
    except Exception as e:
        logger.warning(f"  项目分析失败: {e}")
        return []

    issues = _run_light_ast_pass(target_dir, blueprint)

    # 添加 scanner 标记
    for issue in issues:
        issue["scanner"] = "ast_rules"

    logger.info(f"  Layer 2 (AST 规则): 发现 {len(issues)} 个问题")
    return issues


# ──────────────────────────────────────────────
# Layer 3: LLM 扫描器（带去重）
# ──────────────────────────────────────────────

def _hash_project(target_dir: Path) -> str:
    """计算项目所有 .py 文件的 SHA256 哈希（用于缓存键）。"""
    hasher = hashlib.sha256()
    py_files = sorted(target_dir.rglob("*.py"))
    for pf in py_files:
        # 跳过 .p3_cache 目录
        if ".p3_cache" in pf.parts:
            continue
        try:
            hasher.update(pf.read_bytes())
        except (OSError, PermissionError):
            continue
    return hasher.hexdigest()[:16]


def _load_llm_cache(target_dir: Path, runs: int) -> list[dict] | None:
    """从缓存加载 LLM 扫描结果（基于文件哈希匹配）。"""
    cache_dir = target_dir / ".p3_cache"
    cache_file = cache_dir / "llm_scan_cache.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        current_hash = _hash_project(target_dir)
        if data.get("hash") == current_hash and data.get("runs") >= runs:
            logger.info(f"  ⚡ 命中 LLM 扫描缓存（{data.get('runs', 0)} 轮结果）")
            return data.get("issues", [])
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _save_llm_cache(target_dir: Path, issues: list[dict], runs: int):
    """保存 LLM 扫描结果到缓存。"""
    cache_dir = target_dir / ".p3_cache"
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / "llm_scan_cache.json"
    data = {
        "hash": _hash_project(target_dir),
        "runs": runs,
        "issues": issues,
        "cached_at": datetime.now().isoformat(),
    }
    cache_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"  ✔ LLM 扫描结果已缓存（{len(issues)} 个问题）")


def _run_llm_with_dedup(
    target_dir: Path,
    known_signatures: set,
    skip_tests: bool = True,
    runs: int = 2,
) -> tuple[list[dict], float]:
    """运行 LLM 扫描，但过滤已知的 issue 指纹。

    策略：
      1. 检查缓存（基于项目文件哈希），命中则跳过 API 调用
      2. 正常调用 scan_deep
      3. 将结果中匹配 known_signatures 的 issue 过滤掉
      4. 匹配条件：相同 (file, type_family, line_cluster)
      5. （可选）运行 runs 次取并集，减少随机波动

    Returns:
        (filtered_issues, llm_cost)
    """
    from src.analysis.llm_scanner import scan_deep

    runs = max(1, runs)

    # ── 缓存命中检查 ──
    cached_issues = _load_llm_cache(target_dir, runs)
    if cached_issues is not None:
        # 缓存命中，只做低层去重过滤
        filtered = []
        for issue in cached_issues:
            sig = _issue_signature(issue)
            if sig not in known_signatures:
                issue["scanner"] = f"llm/{issue.get('dimension', 'quality')}"
                filtered.append(issue)
        logger.info(
            f"  Layer 3 (LLM): 缓存命中 → {len(filtered)} 个去重后问题"
        )
        return filtered, 0.0

    # ── 缓存未命中，正常扫描 ──
    seen_signatures = set()  # 跨轮去重用
    all_filtered = []
    total_cost = 0.0

    for r in range(runs):
        if runs > 1:
            logger.info(f"  Layer 3 (LLM): 第 {r+1}/{runs} 轮...")
        else:
            logger.info("  Layer 3 (LLM): 运行中...")

        try:
            result = scan_deep(str(target_dir), skip_tests=skip_tests)
        except Exception as e:
            logger.error(f"  LLM 扫描失败: {e}")
            continue

        all_issues = result.get("issues", [])
        cost = result.get("llm_cost_estimate", 0.0)
        total_cost += cost

        # 1) 过滤已知指纹（低层覆盖）
        # 2) 跨轮去重（同轮内也去重）
        round_filtered = 0
        skip_layer3 = 0
        skip_within_round = 0
        for issue in all_issues:
            sig = _issue_signature(issue)
            if sig in known_signatures:
                skip_layer3 += 1
                continue
            if sig in seen_signatures:
                skip_within_round += 1
                continue
            seen_signatures.add(sig)
            issue["scanner"] = f"llm/{issue.get('dimension', 'quality')}"
            all_filtered.append(issue)
            round_filtered += 1

        if runs > 1:
            logger.info(
                f"    轮次 {r+1}: {len(all_issues)} 原始 → "
                f"{round_filtered} 新保留 "
                f"(跳过 {skip_layer3} 低层 + {skip_within_round} 去重)"
            )

    total_unique = len(all_filtered)

    # 保存缓存
    _save_llm_cache(target_dir, all_filtered, runs)

    logger.info(
        f"  Layer 3 (LLM): 共 {total_unique} 个去重后问题"
        f"（{runs} 轮扫描，总成本 ${total_cost:.4f}）"
    )
    return all_filtered, total_cost


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

def scan_layered(
    project_root: str | Path,
    skip_tests: bool = True,
    enable_layers: list[int] | None = None,
    llm_runs: int = 2,
) -> dict:
    """分层扫描主入口。

    Args:
        project_root: 目标项目根目录
        skip_tests: 是否跳过测试文件（仅影响 Layer 3 LLM 扫描）
        enable_layers: 启用的层列表，默认 [0, 1, 2, 3]

    Returns:
        标准扫描结果 dict，额外包含 "layers" 字段
    """
    if enable_layers is None:
        enable_layers = [0, 1, 2, 3]

    target_dir = Path(project_root).resolve()
    if not target_dir.exists():
        return {
            "score": 0, "total_issues": 0, "issue_count": 0,
            "issues": [], "by_severity": {},
            "error": f"路径不存在: {target_dir}",
        }

    logger.info("=" * 50)
    logger.info("🔬 分层扫描管线启动")
    logger.info(f"   目标: {target_dir}")
    logger.info(f"   启用层: {[f'Layer {l}' for l in enable_layers]}")
    logger.info("=" * 50)

    # 收集每层结果
    layers: dict[int, list[dict]] = {}
    all_known_signatures: set = set()

    # ── Layer 0: Ruff ──
    if 0 in enable_layers:
        issues_0 = _run_ruff(target_dir)
        layers[0] = issues_0
        for iss in issues_0:
            all_known_signatures.add(_issue_signature(iss))

    # ── Layer 1: mypy ──
    if 1 in enable_layers:
        issues_1 = _run_mypy(target_dir)
        layers[1] = issues_1
        for iss in issues_1:
            all_known_signatures.add(_issue_signature(iss))

    # ── Layer 2: AST 规则 ──
    if 2 in enable_layers:
        issues_2 = _run_ast_rules(target_dir)
        layers[2] = issues_2
        for iss in issues_2:
            all_known_signatures.add(_issue_signature(iss))

    # ── Layer 3: LLM 扫描（去重后）──
    llm_cost = 0.0
    if 3 in enable_layers:
        issues_3, llm_cost = _run_llm_with_dedup(
            target_dir, all_known_signatures, skip_tests, runs=llm_runs
        )
        layers[3] = issues_3

    # ── 合并结果 ──
    merged = _merge_issues(layers)

    # 统计
    by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in merged:
        sev = issue.get("severity", "low")
        if sev in by_severity:
            by_severity[sev] += 1

    # 分数计算：平方根压缩扣分，避免少量高权问题把分数打爆
    weights = {"critical": 15, "high": 8, "medium": 3, "low": 1}
    raw_deduction = sum(weights.get(i.get("severity", "low"), 0) for i in merged)
    # 平方根压缩：0→0, 20→~20, 80→~40, 180→~60, 500→~100
    compressed = 20 * math.sqrt(raw_deduction / 20) if raw_deduction > 0 else 0
    score = max(0, min(100, round(100 - compressed)))

    # 层统计
    layer_stats = {}
    for layer_id, issues in sorted(layers.items()):
        layer_stats[f"layer_{layer_id}"] = {
            "tool": {0: "ruff", 1: "mypy", 2: "ast_rules", 3: "llm"}[layer_id],
            "issues_found": len(issues),
            "issues_kept": sum(1 for iss in merged if iss.get("_layer") == layer_id),
        }

    result = {
        "score": score,
        "total_issues": len(merged),
        "issue_count": len(merged),
        "issues": merged,
        "by_severity": by_severity,
        "layers": layer_stats,
        "llm_cost_estimate": llm_cost,
        "files_scanned": sum(
            len({i.get("file", "") for i in layer})
            for layer in layers.values()
        ),
    }

    # 输出摘要
    logger.info("")
    logger.info("=" * 50)
    logger.info(f"📊 分层扫描完成")
    logger.info(f"  评分: {score}/100")
    logger.info(
        f"  问题: {len(merged)} 个"
        f" (🔴 {by_severity['critical']} / 🟠 {by_severity['high']}"
        f" / 🟡 {by_severity['medium']} / 🔵 {by_severity['low']})"
    )
    logger.info(f"  LLM 成本: ${llm_cost:.4f}" if llm_cost else "  LLM 成本: $0 (未运行 LLM 层)")
    logger.info("")
    logger.info("  层贡献:")
    for layer_id, stats in sorted(layer_stats.items()):
        kept = stats["issues_kept"]
        found = stats["issues_found"]
        pct = f" ({kept/found*100:.0f}% 保留)" if found > 0 else ""
        logger.info(f"    Layer {layer_id} ({stats['tool']}): {kept}/{found} 问题{pct}")
    logger.info("=" * 50)

    return result


# ──────────────────────────────────────────────
# CLI 入口
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    skip = "--no-skip-tests" not in sys.argv

    if "--layers" in sys.argv:
        idx = sys.argv.index("--layers")
        layers = [int(x) for x in sys.argv[idx + 1].split(",")]
    else:
        layers = None

    llm_runs = 2
    if "--llm-runs" in sys.argv:
        idx = sys.argv.index("--llm-runs")
        llm_runs = int(sys.argv[idx + 1])

    result = scan_layered(target, skip_tests=skip, enable_layers=layers, llm_runs=llm_runs)
    print(json.dumps({
        "score": result["score"],
        "total_issues": result["total_issues"],
        "by_severity": result["by_severity"],
        "layers": result.get("layers", {}),
        "llm_cost_estimate": result.get("llm_cost_estimate", 0.0),
    }, indent=2, ensure_ascii=False))
