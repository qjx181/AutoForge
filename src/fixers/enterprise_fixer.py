"""enterprise_fixer.py — 企业级自动修复器

修复 deep_enterprise_scanner.py 发现的各种深层问题。

接口：
    try_fix_deep(issue: dict, project_root: Path) -> dict
        返回: {"success": bool, "action": str, "error": str?}
"""

import ast
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import re
import logging
from pathlib import Path


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _write_file(path: Path, content: str) -> bool:
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def _check_syntax(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False




def _find_except_block_end(lines: list, line_num: int, except_indent: int) -> int:
    """查找 except 块的结束行号。"""
    block_end = line_num
    for i in range(line_num, len(lines)):
        if i == line_num:
            continue
        stripped_i = lines[i].strip()
        if not stripped_i or stripped_i.startswith("#"):
            continue
        indent_i = len(lines[i]) - len(stripped_i)
        if indent_i <= except_indent:
            block_end = i
            break
    else:
        block_end = len(lines)
    return block_end


def _check_block_empty(lines: list, line_num: int, block_end: int) -> bool:
    """检查 except 块是否为空（或只含 pass）。"""
    block_lines = [l.strip() for l in lines[line_num:block_end]]
    return all(l == "" or l == "pass" or l.startswith("#") for l in block_lines)


def _ensure_logging_import(lines: list) -> tuple:
    """确保文件中有 import logging，返回 (更新后的lines, 是否有logging)。"""
    has_logging = any(
        l.strip().startswith("import logging") or l.strip().startswith("from logging")
        for l in lines
    )
    if not has_logging:
        insert_pos = 0
        for i, l in enumerate(lines):
            if l.strip().startswith("import ") or l.strip().startswith("from "):
                insert_pos = i + 1
        lines.insert(insert_pos, "import logging")
    return lines, has_logging


def _build_replacement_code(lines: list, line_num: int, block_end: int,
                            except_indent: int) -> str:
    """构建替换后的 except 块代码（加入 logging.exception）。"""
    insert_indent = " " * (except_indent + 4)
    existing_code = "\n".join(lines[line_num - 1:block_end])
    replacement = existing_code.replace("pass", f"{insert_indent}logging.exception('异常捕获: ')")
    if "pass" not in existing_code:
        replacement = existing_code.rstrip() + f"\n{insert_indent}logging.exception('异常捕获: ')"
    return replacement


def fix_swallowed_exception(filepath: Path, line_num: int) -> dict:
    """修复空 except 块：加入 logging.exception()"""
    code = _read_file(filepath)
    if not code:
        return {"success": False, "error": "无法读取文件"}

    lines = code.split("\n")
    if line_num < 1 or line_num > len(lines):
        return {"success": False, "error": f"行号 {line_num} 超出范围"}

    except_line = lines[line_num - 1]
    except_indent = len(except_line) - len(except_line.lstrip())
    stripped = except_line.strip()
    if not stripped.startswith("except"):
        return {"success": False, "error": f"行 {line_num} 不是 except 语句: {stripped[:40]}"}

    block_end = _find_except_block_end(lines, line_num, except_indent)
    if not _check_block_empty(lines, line_num, block_end):
        return {"success": False, "reason": "except 块已有代码，不需要修复"}

    replacement_code = _build_replacement_code(lines, line_num, block_end, except_indent)
    new_lines = lines[:line_num - 1] + replacement_code.split("\n") + lines[block_end:]

    new_lines, _ = _ensure_logging_import(new_lines)
    new_code = "\n".join(new_lines)

    if not _check_syntax(new_code):
        return {"success": False, "error": "修复后语法错误"}

    _write_file(filepath, new_code)
    return {"success": True, "action": "空 except → logging.exception()"}




def fix_bare_except(filepath: Path, line_num: int) -> dict:
    """修复裸 except（`except:` → `except Exception:`）。

    裸 except 会捕获 KeyboardInterrupt 和 SystemExit 等系统异常，
    应改为 except Exception 只捕获预期的异常类型。

    Args:
        filepath: 文件路径
        line_num: except 语句所在行号

    Returns:
        {"success": bool, "action"|"reason"|"error": str}
    """
    code = _read_file(filepath)
    if not code:
        return {"success": False, "error": "无法读取文件"}

    lines = code.split("\n")
    if line_num < 1 or line_num > len(lines):
        return {"success": False, "error": f"行号 {line_num} 超出范围"}

    line = lines[line_num - 1]
    if re.match(r'^\s*except\s*:', line):
        new_line = line.replace("except:", "except Exception:")
        lines[line_num - 1] = new_line
        new_code = "\n".join(lines)
        if _check_syntax(new_code):
            _write_file(filepath, new_code)
            return {"success": True, "action": "裸 except → except Exception"}
    return {"success": False, "reason": "不是裸 except"}


def fix_print_to_logging(filepath: Path, line_num: int) -> dict:
    code = _read_file(filepath)
    if not code:
        return {"success": False, "error": "无法读取文件"}

    lines = code.split("\n")
    if line_num < 1 or line_num > len(lines):
        return {"success": False, "error": f"行号 {line_num} 超出范围"}

    line = lines[line_num - 1]
    stripped = line.strip()

    m = re.match(r'^(.*)print\s*\((.*)\)\s*$', stripped)
    if not m:
        return {"success": False, "reason": "不是 print 调用"}

    indent = line[:len(line) - len(line.lstrip())]
    content = m.group(2)

    new_stripped = f"logging.info({content})"
    new_line = indent + new_stripped

    has_logging = any(l.strip().startswith("import logging") or l.strip().startswith("from logging")
                      for l in lines)

    lines[line_num - 1] = new_line
    if not has_logging:
        insert_pos = 0
        for i, l in enumerate(lines):
            if l.strip().startswith("import ") or l.strip().startswith("from "):
                insert_pos = i + 1
        lines.insert(insert_pos, "import logging")

    new_code = "\n".join(lines)
    if not _check_syntax(new_code):
        return {"success": False, "error": "修复后语法错误"}

    _write_file(filepath, new_code)
    return {"success": True, "action": f"print → logging.info"}

def fix_resource_management(filepath: Path, line_num: int) -> dict:
    code = _read_file(filepath)
    if not code:
        return {"success": False, "error": "无法读取文件"}

    lines = code.split("\n")
    if line_num < 1 or line_num > len(lines):
        return {"success": False, "error": f"行号 {line_num} 超出范围"}

    line = lines[line_num - 1]
    stripped = line.strip()
    indent = line[:len(line) - len(line.lstrip())]

    m = re.match(r'(.*?)open\(([^)]+)\)(.*)', stripped)
    if not m:
        return {"success": False, "reason": "不是 open() 调用"}
    if "with " in stripped:
        return {"success": False, "reason": "已有 with 语句"}

    before = m.group(1).strip()
    args = m.group(2)
    after = m.group(3).strip()

    var_match = re.match(r'(\w+)\s*=', before) if before else None
    var_name = var_match.group(1) if var_match else "f"

    if after:
        new_line = f"{indent}with open({args}) as {var_name}:\n{indent}    {before} {var_name} {after}"
    else:
        new_line = f"{indent}with open({args}) as {var_name}:\n{indent}    {before}{var_name}"

    lines[line_num - 1] = new_line
    new_code = "\n".join(lines)
    if not _check_syntax(new_code):
        return {"success": False, "error": "修复后语法错误"}

    _write_file(filepath, new_code)
    return {"success": True, "action": "open → with open"}

def fix_missing_timeout(filepath: Path, line_num: int) -> dict:
    """修复 requests.get/post() 缺少 timeout 参数"""
    code = _read_file(filepath)
    if not code:
        return {"success": False, "error": "无法读取文件"}

    lines = code.split("\n")
    if line_num < 1 or line_num > len(lines):
        return {"success": False, "error": f"行号 {line_num} 超出范围"}

    line = lines[line_num - 1]
    stripped = line.strip()

    m = re.match(r'(.*)(requests\.(get|post|put|delete|patch|request)\s*\([^)]*)\)(.*)', stripped)
    if not m:
        return {"success": False, "reason": "不是 requests 调用"}

    before = m.group(1)
    call = m.group(2)
    after = m.group(4)

    if 'timeout' in call:
        return {"success": False, "reason": "已有 timeout 参数"}

    new_call = call.rstrip() + ', timeout=30)'
    lines[line_num - 1] = before + new_call + after
    new_code = "\n".join(lines)

    if not _check_syntax(new_code):
        return {"success": False, "error": "修复后语法错误"}

    _write_file(filepath, new_code)
    return {"success": True, "action": "添加 timeout=30"}

def fix_missing_return_type(filepath: Path, line_num: int) -> dict:
    code = _read_file(filepath)
    if not code:
        return {"success": False, "error": "无法读取文件"}

    lines = code.split("\n")
    if line_num < 1 or line_num > len(lines):
        return {"success": False, "error": f"行号 {line_num} 超出范围"}

    line = lines[line_num - 1]
    stripped = line.strip()

    m = re.match(r'^(.*def\s+\w+\s*\([^)]*\))\s*:\s*(.*)', stripped)
    if not m:
        return {"success": False, "reason": "不是 def 行或已有返回类型"}

    if "->" in stripped:
        return {"success": False, "reason": "已有返回类型"}

    has_return = False
    for i in range(line_num, min(line_num + 50, len(lines))):
        if lines[i].strip().startswith("return "):
            has_return = True
            break

    return_type = "None" if not has_return else "Any"

    if not return_type:
        return {"success": False, "reason": "无法推断返回类型"}

    indent = line[:len(line) - len(stripped)]
    new_line = f"{indent}{m.group(1)} -> {return_type}:"
    if m.group(2).strip():
        new_line += f"  {m.group(2).strip()}"

    lines[line_num - 1] = new_line
    new_code = "\n".join(lines)

    if not _check_syntax(new_code):
        return {"success": False, "error": "修复后语法错误"}

    _write_file(filepath, new_code)
    return {"success": True, "action": f"添加 -> {return_type}"}


def fix_missing_param_type(filepath: Path, line_num: int) -> dict:
    """修复函数参数缺少类型注解的问题。

    自动为缺少类型注解的参数添加 `: Any`。
    如果文件没有 `from typing import Any` 导入，会自动添加。

    Args:
        filepath: 文件路径
        line_num: 函数定义所在行号

    Returns:
        {"success": bool, "action"|"reason"|"error": str}

    设计决策（面试话术）：
      "为什么默认用 Any 而不是推断具体类型？
       因为自动推断类型需要分析函数体内的所有使用场景，
       容易出错（比如参数既当 str 又当 int 用）。
       用 Any 是最安全的默认值——不改变运行时行为，
       只是让类型检查器知道'这里有个参数'。
       后续可以手动或用更智能的工具替换为具体类型。"
    """
    code = _read_file(filepath)
    if not code:
        return {"success": False, "error": "无法读取文件"}

    lines = code.split("\n")
    if line_num < 1 or line_num > len(lines):
        return {"success": False, "error": f"行号 {line_num} 超出范围"}

    # 找到函数定义行
    func_line_idx = line_num - 1
    func_line = lines[func_line_idx]

    # 检查是否是函数定义
    if not re.match(r'^\s*(async\s+)?def\s+', func_line):
        return {"success": False, "error": "不是函数定义行"}

    # 使用正则表达式解析函数签名
    # 匹配模式：def funcname(param1, param2: Type, param3=default, ...)
    # 提取括号内的参数列表
    match = re.search(r'def\s+\w+\s*\(([^)]*)\)', func_line)
    if not match:
        # 可能是多行函数定义，尝试合并
        # 但为简化实现，先处理单行情况
        return {"success": False, "error": "无法解析函数签名（可能需要多行解析）"}

    params_str = match.group(1).strip()
    if not params_str:
        return {"success": False, "reason": "函数没有参数"}

    # 解析参数列表
    # 需要处理嵌套括号、默认值等情况
    params = []
    depth = 0
    current_param = ""
    for ch in params_str:
        if ch == '(' or ch == '[':
            depth += 1
            current_param += ch
        elif ch == ')' or ch == ']':
            depth -= 1
            current_param += ch
        elif ch == ',' and depth == 0:
            params.append(current_param.strip())
            current_param = ""
        else:
            current_param += ch
    if current_param.strip():
        params.append(current_param.strip())

    # 找出缺少类型注解的参数
    missing_params = []
    for param in params:
        # 跳过 self/cls
        if param.strip().startswith('self') or param.strip().startswith('cls'):
            continue
        # 检查是否有类型注解（包含冒号）
        if ':' not in param:
            # 提取参数名（去掉默认值）
            param_name = param.split('=')[0].strip()
            if param_name:
                missing_params.append(param_name)

    if not missing_params:
        return {"success": False, "reason": "所有参数已有类型注解"}

    # 使用正则表达式添加类型注解
    new_func_line = func_line
    for param_name in missing_params:
        # 匹配参数名后面没有冒号的情况
        # 处理 "def foo(a, b):" -> "def foo(a: Any, b: Any):"
        # 需要确保不匹配已有类型注解的参数
        pattern = rf'(\b{re.escape(param_name)}\b)(\s*[,):])'
        replacement = rf'\1: Any\2'
        new_func_line = re.sub(pattern, replacement, new_func_line, count=1)

    if new_func_line == func_line:
        return {"success": False, "reason": "无法匹配参数位置"}

    lines[func_line_idx] = new_func_line

    # 确保有 typing.Any 导入
    has_typing_import = False
    import_insert_pos = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('from typing import') and 'Any' in stripped:
            has_typing_import = True
            break
        elif stripped.startswith('import ') or stripped.startswith('from '):
            import_insert_pos = i + 1

    if not has_typing_import:
        # 检查是否有其他 typing 导入可以扩展
        typing_import_found = False
        for i, line in enumerate(lines):
            if line.strip().startswith('from typing import'):
                # 在现有导入中添加 Any
                if 'Any' not in line:
                    lines[i] = line.rstrip() + ', Any'
                typing_import_found = True
                break

        if not typing_import_found:
            lines.insert(import_insert_pos, 'from typing import Any')

    new_code = "\n".join(lines)
    if not _check_syntax(new_code):
        return {"success": False, "error": "修复后语法错误"}

    _write_file(filepath, new_code)
    return {"success": True, "action": f"添加参数类型注解: {', '.join(missing_params)} -> Any"}


def fix_sync_sleep_in_async(filepath: Path, line_num: int) -> dict:
    """修复异步函数中使用 time.sleep 的问题。

    把 `time.sleep(N)` 改为 `await asyncio.sleep(N)`。
    如果文件没有 `import asyncio`，会自动添加。

    Args:
        filepath: 文件路径
        line_num: time.sleep 所在行号

    Returns:
        {"success": bool, "action"|"reason"|"error": str}

    设计决策（面试话术）：
      "为什么不能直接替换？因为 time.sleep 是同步阻塞的，
       会阻塞整个事件循环。asyncio.sleep 是异步的，
       会让出控制权给其他协程。
       但替换的前提是：调用者必须在异步函数中（async def）。
       如果在同步函数中调用 time.sleep，替换为 asyncio.sleep 会报错，
       因为同步函数不能 await。所以我会检查调用者是否是异步函数。"
    """
    code = _read_file(filepath)
    if not code:
        return {"success": False, "error": "无法读取文件"}

    lines = code.split("\n")
    if line_num < 1 or line_num > len(lines):
        return {"success": False, "error": f"行号 {line_num} 超出范围"}

    line = lines[line_num - 1]
    stripped = line.strip()

    # 检查是否是 time.sleep 调用
    if not re.search(r'time\.sleep\s*\(', stripped):
        return {"success": False, "reason": "不是 time.sleep 调用"}

    # 检查是否在异步函数中
    in_async_func = False
    for i in range(line_num - 2, -1, -1):
        prev_line = lines[i].strip()
        if prev_line.startswith('async def'):
            in_async_func = True
            break
        elif prev_line.startswith('def '):
            in_async_func = False
            break

    if not in_async_func:
        return {"success": False, "reason": "不在异步函数中，不能替换为 asyncio.sleep"}

    # 检查是否已经在 await 中
    if 'await' in stripped and 'asyncio.sleep' in stripped:
        return {"success": False, "reason": "已经是 await asyncio.sleep"}

    # 替换 time.sleep -> await asyncio.sleep
    indent = line[:len(line) - len(line.lstrip())]
    new_stripped = re.sub(
        r'time\.sleep\s*\(([^)]+)\)',
        r'await asyncio.sleep(\1)',
        stripped
    )

    # 如果原来没有 await，需要添加
    if 'await' not in new_stripped:
        # 检查是否已经有 await
        if stripped.startswith('await '):
            new_line = indent + new_stripped
        else:
            new_line = indent + 'await ' + new_stripped.lstrip()
    else:
        new_line = indent + new_stripped

    lines[line_num - 1] = new_line

    # 确保有 asyncio 导入
    has_asyncio_import = any('import asyncio' in line for line in lines)
    if not has_asyncio_import:
        import_insert_pos = 0
        for i, line in enumerate(lines):
            if line.strip().startswith('import ') or line.strip().startswith('from '):
                import_insert_pos = i + 1
        lines.insert(import_insert_pos, 'import asyncio')

    new_code = "\n".join(lines)
    if not _check_syntax(new_code):
        return {"success": False, "error": "修复后语法错误"}

    _write_file(filepath, new_code)
    return {"success": True, "action": "time.sleep → await asyncio.sleep"}


DEEP_FIXERS = {
    "swallowed_exception": fix_swallowed_exception,
    "bare_except": fix_bare_except,
    "print_used": fix_print_to_logging,
    "resource_not_managed": fix_resource_management,
    "missing_timeout_config": fix_missing_timeout,
    "missing_return_type": fix_missing_return_type,
    "missing_param_type": fix_missing_param_type,
    "high_cyclomatic_complexity": None,  # 太复杂，需人工介入
    "moderate_cyclomatic_complexity": None,
    "test_no_assertions": None,
    "test_missing_setup": None,
    "command_injection_risk": None,
    "dangerous_eval": None,
    "hardcoded_secret": None,
    "sync_sleep_in_async": fix_sync_sleep_in_async,
    "sync_http_in_async": None,  # 比较复杂，暂时跳过
    "missing_unified_error_handler": None,
}


def try_fix_deep(issue: dict, project_root: Path) -> dict:
    """尝试修复一个深层问题"""
    issue_type = issue.get("type", "")
    file_rel = issue.get("file", "")
    line = issue.get("line", 0)

    if not file_rel:
        return {"success": False, "reason": "无文件路径"}

    fixer = DEEP_FIXERS.get(issue_type)
    if not fixer:
        return {"success": False, "reason": f"无修复器: {issue_type}"}

    filepath = project_root / file_rel if not Path(file_rel).is_absolute() else Path(file_rel)
    if not filepath.exists():
        return {"success": False, "reason": f"文件不存在: {filepath}"}

    try:
        result = fixer(filepath, line)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}
