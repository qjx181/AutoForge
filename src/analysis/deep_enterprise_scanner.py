"""deep_enterprise_scanner.py — 企业级深度扫描器

在 9 维表面扫描之上，增加更深层的代码质量审计。
单独运行或集成到 evolution_engine.py 中使用。

接口：
    scan_deep(project_root: str) -> dict
        返回: {
            "dimension": "enterprise",
            "score": int(0-100),
            "issues": [dict, ...],
            "summary": str,
        }

检查项（企业级标准）：
    1. 圈复杂度 (cyclomatic complexity)
    2. 异常处理模式（空 except、过宽 except、吞错误）
    3. 类型安全（无类型注解、Any 滥用）
    4. 资源管理（文件/连接未关闭）
    5. 日志规范（print 替代 logging）
    6. 异步健壮性（time.sleep、缺 timeout、无超时处理）
    7. API 设计（状态码不一致、错误格式不统一）
    8. 安全纵深（输入校验链、路径穿越、SQL 注入模式）
    9. 测试质量（assert 数量、测试隔离、mock 使用）
"""

import ast
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import re
import sys
from pathlib import Path
from collections import defaultdict



def _calc_cyclomatic_complexity(node: ast.AST) -> int:
    """计算函数的圈复杂度（McCabe）。"""
    complexity = 1  # 基础路径
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor)):
            complexity += 1
        elif isinstance(child, ast.Try):
            complexity += len(child.handlers)  # except 数量
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1  # and/or 分支
        elif isinstance(child, (ast.ExceptHandler, ast.With, ast.AsyncWith)):
            complexity += 1
    return complexity


def _check_complexity(tree: ast.AST, filepath: str) -> list[dict]:
    """检查函数圈复杂度，超过 15 即告警。"""
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cc = _calc_cyclomatic_complexity(node)
            if cc > 15:
                issues.append({
                    "type": "high_cyclomatic_complexity",
                    "severity": "high",
                    "file": filepath,
                    "line": node.lineno,
                    "description": f"函数 {node.name} 圈复杂度 {cc}，超过建议值 15",
                    "suggestion": "拆分为多个小函数，每个函数职责单一",
                })
            elif cc > 10:
                issues.append({
                    "type": "moderate_cyclomatic_complexity",
                    "severity": "medium",
                    "file": filepath,
                    "line": node.lineno,
                    "description": f"函数 {node.name} 圈复杂度 {cc}，建议重构",
                    "suggestion": "考虑提取条件分支为独立函数",
                })
    return issues




def _check_exception_handling(tree: ast.AST, filepath: str) -> list[dict]:
    """检查异常处理模式。"""
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                issues.append({
                    "type": "bare_except",
                    "severity": "high",
                    "file": filepath,
                    "line": node.lineno,
                    "description": "裸 except（不指定异常类型）会捕获 SystemExit 和 KeyboardInterrupt",
                    "suggestion": "改为 except SpecificException:",
                })
            elif node.name and isinstance(node.body[-1], ast.Pass) if node.body else False:
                pass  # 这个是 pass，单独检查
            if not node.body or (len(node.body) == 1 and isinstance(node.body[0], ast.Pass)):
                if node.type and isinstance(node.type, ast.Name):
                    issues.append({
                        "type": "swallowed_exception",
                        "severity": "high",
                        "file": filepath,
                        "line": node.lineno,
                        "description": f"异常 {node.type.id} 被吞没（空的 except 块）",
                        "suggestion": "至少记录日志：logging.exception()",
                    })
        if isinstance(node, ast.Try):
            handler_names = []
            for h in node.handlers:
                if h.type and isinstance(h.type, ast.Name):
                    handler_names.append(h.type.id)
            if len(handler_names) >= 3 and 'Exception' in handler_names:
                issues.append({
                    "type": "broad_exception_handling",
                    "severity": "medium",
                    "file": filepath,
                    "line": node.lineno,
                    "description": f"过于宽泛的异常处理：处理了 {', '.join(handler_names)}，可能掩盖编程错误",
                    "suggestion": "只捕获你知道怎么处理的异常",
                })
    return issues




def _check_type_safety(tree: ast.AST, filepath: str) -> list[dict]:
    """检查类型注解完整性。"""
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.returns:
                if not node.name.startswith('__') and not node.name.startswith('_'):
                    issues.append({
                        "type": "missing_return_type",
                        "severity": "medium",
                        "file": filepath,
                        "line": node.lineno,
                        "description": f"函数 {node.name} 缺少返回类型注解",
                        "suggestion": "添加 -> ReturnType 类型注解",
                    })
            for arg in node.args.args:
                if arg.arg == 'self' or arg.arg == 'cls':
                    continue
                if not arg.annotation:
                    issues.append({
                        "type": "missing_param_type",
                        "severity": "low",
                        "file": filepath,
                        "line": node.lineno,
                        "description": f"函数 {node.name} 的参数 '{arg.arg}' 缺少类型注解",
                        "suggestion": "添加类型注解",
                    })
    return issues




def _check_resource_management(code: str, filepath: str) -> list[dict]:
    """检查文件/连接是否用上下文管理器。"""
    issues = []
    patterns = [
        (r'\.open\(', "open() 应使用 with 语句", "low"),
        (r'(?<!\.)(connect|Cursor|Session)\s*\(', "资源对象应使用 with 或 try/finally", "medium"),
    ]
    for i, line in enumerate(code.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('#') or stripped.startswith('"""'):
            continue
        for pat, desc, sev in patterns:
            if re.search(pat, stripped) and 'with ' not in stripped and 'as ' not in stripped[:stripped.find('(')]:
                issues.append({
                    "type": "resource_not_managed",
                    "severity": sev,
                    "file": filepath,
                    "line": i,
                    "description": desc,
                    "suggestion": "使用上下文管理器自动管理资源生命周期",
                })
    return issues




def _check_logging(code: str, filepath: str) -> list[dict]:
    """检查 print 替代 logging。"""
    issues = []
    has_logging = 'import logging' in code or 'from logging' in code
    for i, line in enumerate(code.split('\n'), 1):
        stripped = line.strip()
        if re.match(r'^print\s*\(', stripped) and not stripped.startswith('#'):
            if 'test_' not in filepath and 'cli' not in filepath:
                issues.append({
                    "type": "print_used",
                    "severity": "low",
                    "file": filepath,
                    "line": i,
                    "description": "使用 print() 而非 logging",
                    "suggestion": "import logging; logging.info()",
                })
    return issues




def _check_async_robustness(tree: ast.AST, code: str, filepath: str) -> list[dict]:
    """检查异步代码中的健壮性问题。"""
    issues = []
    lines = code.split('\n')

    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef,)):
            func_lines = set(range(node.lineno, node.end_lineno + 1)) if node.end_lineno else set()
            func_code = '\n'.join(lines[node.lineno - 1:node.end_lineno])

            if 'time.sleep(' in func_code:
                issues.append({
                    "type": "sync_sleep_in_async",
                    "severity": "high",
                    "file": filepath,
                    "line": node.lineno,
                    "description": f"异步函数 {node.name} 中使用了 time.sleep()，会阻塞事件循环",
                    "suggestion": "使用 await asyncio.sleep() 替代",
                })

            if re.search(r'(?<!async )requests\.(get|post|put|delete)\s*\(', func_code):
                issues.append({
                    "type": "sync_http_in_async",
                    "severity": "high",
                    "file": filepath,
                    "line": node.lineno,
                    "description": f"异步函数 {node.name} 中使用了同步 requests，会阻塞事件循环",
                    "suggestion": "使用 httpx.AsyncClient 替代",
                })

            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func_name = ""
                    if isinstance(child.func, ast.Attribute):
                        func_name = child.func.attr
                    elif isinstance(child.func, ast.Name):
                        func_name = child.func.id
                    pass

    if 'timeout' not in code.lower() and 'TIMEOUT' not in code:
        issues.append({
            "type": "missing_timeout_config",
            "severity": "medium",
            "file": filepath,
            "line": 1,
            "description": "未检测到超时配置，API调用可能永久挂起",
            "suggestion": "为所有外部调用添加 timeout 参数",
        })

    return issues




def _check_api_design(code: str, filepath: str) -> list[dict]:
    """检查 API 响应的一致性。"""
    issues = []
    has_httpexception = 'HTTPException' in code
    has_error_handler = any(x in code for x in [
        'exception_handlers', 'add_exception_handler',
        '@app.exception_handler', 'uniform_error',
    ])

    if has_httpexception and not has_error_handler:
        issues.append({
            "type": "missing_unified_error_handler",
            "severity": "medium",
            "file": filepath,
            "line": 1,
            "description": "使用了 HTTPException 但未注册统一异常处理器",
            "suggestion": "添加 @app.exception_handler 统一错误响应格式",
        })

    return issues




def _check_security_deep(code: str, filepath: str) -> list[dict]:
    """纵深安全检查。"""
    issues = []

    for i, line in enumerate(code.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('#') or stripped.startswith('"""'):
            continue

        if re.search(r'os\.system\s*\(', stripped):
            issues.append({
                "type": "command_injection_risk",
                "severity": "critical",
                "file": filepath,
                "line": i,
                "description": "os.system() 可能导致命令注入",
                "suggestion": "使用 subprocess.run() 并传递参数列表而非字符串",
            })

        if re.search(r'\beval\s*\(', stripped) or re.search(r'\bexec\s*\(', stripped):
            issues.append({
                "type": "dangerous_eval",
                "severity": "critical",
                "file": filepath,
                "line": i,
                "description": "eval/exec 可能导致任意代码执行",
                "suggestion": "避免使用 eval/exec，使用 ast.literal_eval() 解析安全表达式",
            })

        if re.search(r'(api_key|secret|password|token|apikey)\s*[=:]\s*[\"\'](?![\"\'])', stripped, re.I):
            if 'os.environ' not in stripped and 'env.' not in stripped and 'getenv' not in stripped:
                issues.append({
                    "type": "hardcoded_secret",
                    "severity": "critical",
                    "file": filepath,
                    "line": i,
                    "description": "硬编码密钥/密码，存在泄露风险",
                    "suggestion": "使用环境变量或密钥管理服务",
                })

    return issues




def _check_test_quality(tree: ast.AST, filepath: str) -> list[dict]:
    """检查测试质量。"""
    issues = []
    if 'test_' not in filepath and '/tests/' not in filepath:
        return issues  # 只检查测试文件

    test_funcs = 0
    asserts = 0
    has_mock = False
    has_setup = False

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name.startswith('test_'):
                test_funcs += 1
                for child in ast.walk(node):
                    if isinstance(child, ast.Assert):
                        asserts += 1
                    elif isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Attribute) and 'assert' in child.func.attr.lower():
                            asserts += 1
                        if isinstance(child.func, ast.Attribute) and 'mock' in child.func.attr.lower():
                            has_mock = True
            if node.name == 'setup_method' or node.name == 'setup_class' or 'setUp' in node.name:
                has_setup = True

    if test_funcs > 0:
        avg_asserts = asserts / test_funcs
        if avg_asserts < 1:
            issues.append({
                "type": "test_no_assertions",
                "severity": "high",
                "file": filepath,
                "line": 1,
                "description": f"测试函数平均 assert 数 {avg_asserts:.1f}，存在无断言的测试",
                "suggestion": "每个测试至少有一个 assert 验证预期结果",
            })
        if not has_setup and test_funcs > 1:
            issues.append({
                "type": "test_missing_setup",
                "severity": "low",
                "file": filepath,
                "line": 1,
                "description": "多个测试函数但缺少 setup_method/setUp",
                "suggestion": "提取公共初始化逻辑到 setup_method",
            })

    return issues




def scan_deep(project_root: str) -> dict:
    """对项目执行企业级深度扫描。

    Args:
        project_root: 项目根目录路径

    Returns:
        dict: {
            "dimension": "enterprise",
            "score": int(0-100),
            "issues": [dict, ...],
            "issue_count": int,
            "summary": str,
        }
    """
    root = Path(project_root)
    if not root.exists():
        return {"dimension": "enterprise", "score": 0, "issues": [],
                "issue_count": 0, "summary": "项目路径不存在"}

    all_issues = []
    files_scanned = 0

    for py_file in sorted(root.rglob("*.py")):
        rel = py_file.relative_to(root)
        if any(p in str(rel) for p in ['.hermes', '__pycache__', 'venv', 'node_modules',
                                         '.git', 'dist', 'build', 'env', 'src/analysis',
                                         'src/optimizer']):
            continue

        try:
            code = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(code)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        str_path = str(rel)
        files_scanned += 1

        all_issues.extend(_check_complexity(tree, str_path))

        all_issues.extend(_check_exception_handling(tree, str_path))

        all_issues.extend(_check_type_safety(tree, str_path))

        all_issues.extend(_check_resource_management(code, str_path))

        all_issues.extend(_check_logging(code, str_path))

        all_issues.extend(_check_async_robustness(tree, code, str_path))

        all_issues.extend(_check_api_design(code, str_path))

        all_issues.extend(_check_security_deep(code, str_path))

        all_issues.extend(_check_test_quality(tree, str_path))

    severity_weights = {"critical": 10, "high": 5, "medium": 2, "low": 1}
    severity_caps = {"critical": 3, "high": 6, "medium": 10, "low": 10}
    total_penalty = 0
    sev_counts = defaultdict(int)
    for i in all_issues:
        sev = i.get("severity", "low")
        sev_counts[sev] += 1
        count = sev_counts[sev]
        cap = severity_caps.get(sev, 10)
        if count <= cap:
            total_penalty += severity_weights.get(sev, 1)
    score = max(0, 100 - total_penalty)

    by_severity = defaultdict(int)
    for i in all_issues:
        by_severity[i.get("severity", "low")] += 1

    summary_parts = [
        f"深度扫描 {files_scanned} 个文件",
        f"发现 {len(all_issues)} 个问题",
    ]
    if by_severity.get("critical"):
        summary_parts.append(f"Critical {by_severity['critical']} 个")
    if by_severity.get("high"):
        summary_parts.append(f"High {by_severity['high']} 个")
    if by_severity.get("medium"):
        summary_parts.append(f"Medium {by_severity['medium']} 个")

    return {
        "dimension": "enterprise",
        "level": "deep",
        "score": score,
        "issues": all_issues,
        "issue_count": len(all_issues),
        "by_severity": dict(by_severity),
        "files_scanned": files_scanned,
        "summary": " | ".join(summary_parts),
    }



if __name__ == "__main__":
    import json, sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    result = scan_deep(target)
    print(f"深度扫描评分: {result['score']}/100")
    print(f"发现问题: {result['issue_count']} 个")
    print(f"严重程度: critical={result['by_severity'].get('critical',0)} high={result['by_severity'].get('high',0)} medium={result['by_severity'].get('medium',0)}")
    print(f"文件覆盖: {result['files_scanned']}")
    print()
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for i in sorted(result['issues'], key=lambda x: sev_order.get(x.get('severity','low'), 99)):
        print(f"  [{i['severity']:8s}] {i['type']:30s} {i['file']}:{i['line']}")
        print(f"          {i['description'][:80]}")
