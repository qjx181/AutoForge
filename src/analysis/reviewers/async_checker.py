"""code_review.py — PR 代码审查 Agent 模块

自动审查代码变更，检测安全、性能、代码质量问题，输出质量报告。

模块结构:
  - SecurityReviewer    安全审查（SQL注入、命令注入、密钥泄露、XSS）
  - PerformanceReviewer 性能审查（N+1查询、sync I/O阻塞、内存泄漏）
  - QualityReviewer     代码质量审查（未用import、过深嵌套、硬编码值）
  - PRReviewer          综合 PR 审查（调用三个审查器，输出总评分和审核结论）

设计理由:
  - 纯 Python 标准库，无外部依赖，开箱即用
  - 正则 + AST 双重检测：正则捕获字面量，AST 捕获结构问题
  - 按严重级别分级（critical/high/medium/low），下游可灵活决策

面试官可能问:
  - 为什么不用 flake8/pylint？答：它们只检查质量不检查安全；此模块兼做安全和性能
  - 覆盖 JavaScript 吗？答：XSS 检测部分覆盖 JS，完整 JS 支持需要补充
  - 误报率怎么控制？答：白名单 + AST 分析减少正则误报
"""

import ast
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import re
from pathlib import Path




class AsyncSyncBoundaryChecker:
    """async/sync 边界检测器

    检测三类 async/sync 边界模式：
      1) sync-wrapper-raises: sync 包装器在检测到运行中事件循环时 raise（Critical）
      2) sync-calls-async: sync 函数调用 async 函数但未在独立线程运行（High）
      3) asyncio-run-in-loop: asyncio.run() 在非入口 sync 函数中被调用（High）

    Why AST:
      - 正则无法可靠区分 sync def 内部的函数作用域
      - AST 能精确判断函数边界、async 修饰符、调用链

    面试官可能问:
      - 为什么不用 mypy/pyright? 答：它们检查类型安全，不检查运行时 async/sync 边界
      - 误报怎么控制？答：白名单入口函数 + 名称前缀过滤
      - 能处理嵌套函数吗？答：AST 递归遍历，子函数也可检测
    """

    _IGNORE_ENTRY_POINTS: set[str] = {"main", "setup", "manage", "cli", "run", "init"}

    @staticmethod
    def check_sync_wrapper_raises(code: str) -> list[dict]:
        """检测 sync-wrapper-raises 模式（Critical）

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 问题列表，每条含 type/severity/line/code/description/suggestion

        检测目标:
          sync def 函数中调用 asyncio.get_running_loop() 检测事件循环，
          并在检测到循环运行时 raise RuntimeError。

        Why:
          这种模式是"假 sync 包装器"：看起来是同步函数，实际上内部做了事件循环检测，
          在已有事件循环的上下文中（如 FastAPI/uvicorn）会直接崩溃。
          真正的 sync 包装器应该用 asyncio.run()（在新事件循环中运行），
          而不是检测到循环就抛异常。

        面试官可能问:
          - 为什么不直接检测 raise 语句？答：raise 的具体内容重要，只检测裸 raise 会误报
          - 为什么是 critical 级别？答：会导致运行时直接崩溃，不是性能问题
        """
        issues: list[dict] = []

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Name) and dec.id in ("classmethod", "staticmethod"):
                    continue

            func_source_lines = code.split("\\n")
            func_def_line = func_source_lines[node.lineno - 1].strip() if node.lineno <= len(func_source_lines) else ""
            if func_def_line.startswith("async "):
                continue  # async def 不算 sync 包装器

            has_get_running_loop = False
            has_is_running_check = False
            has_raise = False

            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if (isinstance(func, ast.Attribute)
                            and func.attr == "get_running_loop"
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "asyncio"):
                        has_get_running_loop = True

                if isinstance(child, ast.Raise):
                    if child.exc and isinstance(child.exc, ast.Call):
                        exc_func = child.exc.func
                        if isinstance(exc_func, ast.Name) and exc_func.id == "RuntimeError":
                            has_raise = True

                if isinstance(child, ast.Call):
                    func = child.func
                    if (isinstance(func, ast.Attribute)
                            and func.attr == "is_running"
                            and isinstance(func.value, ast.Attribute)
                            and func.value.attr == "is_running") or (
                            isinstance(func, ast.Attribute)
                            and func.attr == "is_running"
                    ):
                        has_is_running_check = True

            if has_get_running_loop and has_raise:
                issues.append({
                    "type": "sync_wrapper_raises",
                    "severity": "critical",
                    "line": node.lineno,
                    "code": f"def {node.name}(...):",
                    "description": f"同步函数 '{node.name}' 检测到运行中事件循环后主动抛出异常（sync-wrapper-raises），"
                                   f"在 async 上下文（如 FastAPI）中调用时会直接崩溃",
                    "suggestion": f"将 '{node.name}' 改为 async def 让调用方用 await 调用；"
                                   f"或使用 asyncio.run() 在新事件循环中执行而不阻塞",
                })

        return issues

    @staticmethod
    def check_asyncio_run_in_func(code: str) -> list[dict]:
        """检测 asyncio-run-in-loop 模式（High）

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 问题列表

        检测目标:
          在同步函数中调用 asyncio.run(coro)，且该函数不是入口函数。
          尤其关注函数名含 _sync / _wrap_ 的包装器函数。

        Why:
          asyncio.run() 在已有事件循环（如 FastAPI/uvicorn/Jupyter）中抛出
          "RuntimeError: asyncio.run() cannot be called from a running event loop"。
          入口函数（main/setup）中调用是合理的，但包装器函数中调用则是设计问题。

        面试官可能问:
          - asyncio.run() 和 loop.run_until_complete() 的区别？
            答：asyncio.run() 总是创建新事件循环，run_until_complete 运行在已有循环上
          - 误报主要来源？答：测试代码和 CLI 入口（已通过白名单控制）
        """
        issues: list[dict] = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        lines = code.split("\\n")

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue

            func_name = node.name

            func_source_line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
            if func_source_line.startswith("async "):
                continue

            if func_name.lower() in AsyncSyncBoundaryChecker._IGNORE_ENTRY_POINTS:
                continue

            has_asyncio_run = False
            asyncio_run_line = 0
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if (isinstance(func, ast.Attribute)
                            and func.attr == "run"
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "asyncio"):
                        has_asyncio_run = True
                        asyncio_run_line = child.lineno
                        break
                    if (isinstance(func, ast.Attribute)
                            and func.attr == "run"
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "asyncio"):
                        has_asyncio_run = True
                        asyncio_run_line = child.lineno
                        break

            if not has_asyncio_run:
                continue

            is_wrapper = "_sync" in func_name or "_wrap_" in func_name or func_name.startswith("_sync_")

            severity = "high" if is_wrapper else "medium"
            desc = (
                f"同步包装器 '{func_name}' 中调用 asyncio.run()，如果被 async 上下文调用会抛出 'already running' 错误"
                if is_wrapper
                else f"同步函数 '{func_name}' 中调用 asyncio.run()，可能在已有事件循环中运行"
            )

            issues.append({
                "type": "asyncio_run_in_loop",
                "severity": severity,
                "line": asyncio_run_line,
                "code": lines[asyncio_run_line - 1].strip()[:80] if asyncio_run_line <= len(lines) else "",
                "description": desc,
                "suggestion": (
                    f"将 {func_name} 改为 async def，调用方用 await 替代；"
                    f"或确保只在主线程/新线程中调用"
                ),
            })

        return issues

    @classmethod
    def check_sync_calls_async_def(cls, code: str) -> list[dict]:
        """检测 sync-calls-async 模式——同步函数中 asyncio.run() 调用了 async 函数（High）

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 问题列表

        检测目标:
          在 sync def 函数中，asyncio.run() 的参数是对 async def 函数的调用。
          这种模式意味着调用方依赖 asyncio.run() 在同步上下文中执行 async 函数，
          但如果在已有事件循环中调用就会崩溃。

        Why:
          模式2 比模式3 更进一步：模式3 只看"调用了 asyncio.run()"，
          模式2 检查"被 asyncio.run() 包裹的函数是否是 async def"。
          如果是调用一个同步函数，asyncio.run() 也能工作但无意义；
          但如果是 async 函数，说明设计者在故意 async→sync 桥接。

        面试官可能问:
          - 和 check_asyncio_run_in_func 区别？答：前者只检测'存在 asyncio.run'，
            后者检测'asyncio.run 包裹了 async 函数'，两者可同时触发但原因不同
          - 如何知道目标函数是 async？答：在当前代码的 AST 中搜索同名 async def
        """
        issues: list[dict] = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        lines = code.split("\\n")

        async_func_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                async_func_names.add(node.name)

        if not async_func_names:
            return issues

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue

            func_source_line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
            if func_source_line.startswith("async "):
                continue  # 跳过 async def

            func_name = node.name
            if func_name.lower() in cls._IGNORE_ENTRY_POINTS:
                continue  # 跳过入口函数

            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                is_asyncio_run = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "run"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "asyncio"
                )
                if not is_asyncio_run:
                    continue

                if child.args:
                    arg = child.args[0]  # asyncio.run(arg)
                    if isinstance(arg, ast.Call):
                        called_func = arg.func
                        if isinstance(called_func, ast.Name):
                            called_name = called_func.id
                        elif isinstance(called_func, ast.Attribute):
                            called_name = called_func.attr
                        else:
                            continue

                        if called_name in async_func_names:
                            is_wrapper = "_sync" in func_name or "_wrap_" in func_name or func_name.startswith("_sync_")
                            issues.append({
                                "type": "sync_calls_async",
                                "severity": "high" if is_wrapper else "high",
                                "line": child.lineno,
                                "code": lines[child.lineno - 1].strip()[:80] if child.lineno <= len(lines) else "",
                                "description": (
                                    f"同步函数 '{func_name}' 通过 asyncio.run() 调用 async 函数 '{called_name}'，"
                                    f"在已有事件循环中会抛出 RuntimeError"
                                ),
                                "suggestion": (
                                    f"将 '{func_name}' 改为 async def 并用 await {called_name}(...) 替代；"
                                    f"或将调用移至独立线程"
                                ),
                            })

        return issues

    @staticmethod
    def check_codebase_import_chain(files: list[str]) -> list[dict]:
        """跨文件导入链分析——检测 sync 函数被导入到 async 调用方

        Args:
            files: Python 文件路径列表

        Returns:
            list[dict]: 导入链问题列表

        检测目标:
          追踪 sync 函数通过 import 语句被导入到另一个模块，
          在该模块中被当作 async 函数调用（但调用处没有 await）。

        Why:
          单文件分析无法发现跨模块的 async/sync 不匹配问题。
          一个 sync 函数被 `from A import sync_func` 到 B 模块，
          然后在 B 的 async 函数中被无 await 地调用——这在运行时不会直接崩溃，
          但违背了 async 代码的设计意图，可能阻塞事件循环。

        面试官可能问:
          - 为什么不做跨文件追踪？答：需要解析所有依赖模块的 AST 并建立导入图，
            对大型项目扫描成本较高。这里提供的是轻量级版本：只检查 import 语句
            和调用方的函数类型
          - 局限性？答：不能处理动态导入、__init__.py 重导出、条件导入
        """
        issues: list[dict] = []

        sync_funcs_by_file: dict[str, set[str]] = {}
        file_asts: dict[str, ast.Module] = {}

        for filepath in files:
            try:
                with open(filepath, encoding="utf-8") as f:
                    code_text = f.read()
            except (FileNotFoundError, IOError, OSError):
                continue

            try:
                tree = ast.parse(code_text)
            except SyntaxError:
                continue

            file_asts[filepath] = tree
            lines = code_text.split("\\n")
            sync_funcs: set[str] = set()

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    func_def_line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                    if not func_def_line.startswith("async "):
                        sync_funcs.add(node.name)

            if sync_funcs:
                sync_funcs_by_file[filepath] = sync_funcs

        for filepath, tree in file_asts.items():
            lines_for_file: list[str] = []
            try:
                with open(filepath, encoding="utf-8") as f:
                    lines_for_file = f.read().split("\\n")
            except (FileNotFoundError, IOError, OSError):
                continue

            imports: dict[str, str] = {}  # local_name → source
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for alias in node.names:
                        local_name = alias.asname or alias.name
                        imports[local_name] = f"{module}.{alias.name}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        local_name = alias.asname or alias.name
                        imports[local_name] = alias.name

            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue

                for child in ast.walk(node):
                    if not isinstance(child, ast.Call):
                        continue
                    if isinstance(child.func, ast.Name):
                        called_name = child.func.id
                    elif isinstance(child.func, ast.Attribute):
                        called_name = child.func.attr
                    else:
                        continue

                    if called_name not in imports:
                        continue

                    source = imports[called_name]
                    for src_file, sync_funcs in sync_funcs_by_file.items():
                        imported_func = source.split(".")[-1]
                        if imported_func in sync_funcs:
                            issues.append({
                                "type": "sync_calls_async_cross_file",
                                "severity": "high",
                                "line": child.lineno,
                                "code": lines_for_file[child.lineno - 1].strip()[:80] if child.lineno <= len(lines_for_file) else "",
                                "description": (
                                    f"async 函数 '{node.name}' 中无 await 调用了 sync 函数 '{called_name}'"
                                    f"（来自 {source}），可能阻塞事件循环"
                                ),
                                "suggestion": (
                                    f"使用 asyncio.to_thread({called_name}, ...) 包装同步调用，"
                                    f"或将函数改为 async def"
                                ),
                            })

        return issues

    @classmethod
    def review_all(cls, code: str) -> list[dict]:
        """综合 async/sync 边界检测

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 所有 async/sync 边界问题，按严重级别排序
        """
        issues = []
        issues.extend(cls.check_sync_wrapper_raises(code))
        issues.extend(cls.check_asyncio_run_in_func(code))
        issues.extend(cls.check_sync_calls_async_def(code))
        issues.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x["severity"], 99))
        return issues


