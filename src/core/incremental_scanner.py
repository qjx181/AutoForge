"""incremental_scanner.py — 增量扫描：只扫描变更文件

核心思想（面试话术）：
  "全量扫描在大项目上很慢（几百个文件 × 9 个维度 = 几分钟）。
   但大多数时候只有少量文件变更。增量扫描用 git diff 或文件 mtime
   检测变更，只扫描变更文件，把扫描时间从分钟级降到秒级。"

两种检测策略：
  1. Git 模式（优先）：git diff --name-only HEAD 检测未提交的变更
  2. Mtime 模式（降级）：比较文件修改时间和上次扫描时间戳

使用方式：
    from src.core.incremental_scanner import IncrementalScanner
    scanner = IncrementalScanner(project_root)
    changed = scanner.get_changed_files()  # 只返回变更文件
    issues = scanner.scan_changed(registry)  # 只扫描变更文件
"""

import json
import logging
import subprocess
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCAN_STATE_DIR = Path(__file__).parent.parent.parent.resolve() / "data"


def _state_file(project_root: Path) -> Path:
    """为每个项目生成独立的状态文件。"""
    import hashlib
    project_hash = hashlib.md5(str(project_root).encode()).hexdigest()[:8]
    return SCAN_STATE_DIR / f"scan_state_{project_hash}.json"


def _load_state(project_root: Path) -> dict:
    """加载扫描状态。"""
    sf = _state_file(project_root)
    if not sf.exists():
        return {"last_scan_at": None, "file_mtimes": {}}
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return {"last_scan_at": None, "file_mtimes": {}}


def _save_state(project_root: Path, state: dict) -> None:
    """保存扫描状态。"""
    sf = _state_file(project_root)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")



class IncrementalScanner:
    """增量扫描器：检测变更文件，只扫描变更部分。

    Args:
        project_root: 项目根目录
    """

    def __init__(self, project_root: Path):
        self._root = project_root

    def get_changed_files(self, extensions: Optional[set[str]] = None) -> list[Path]:
        """获取自上次扫描以来变更的文件列表。

        优先用 git diff，降级用 mtime 比较。

        Args:
            extensions: 只关注的文件扩展名（如 {'.py'}），None 则返回所有

        Returns:
            变更文件的绝对路径列表
        """
        extensions = extensions or {'.py'}

        git_changes = self._git_changed_files()
        if git_changes is not None:
            result = [self._root / f for f in git_changes if Path(f).suffix in extensions]
            logger.info("Incremental (git): %d changed files", len(result))
            return result

        mtime_changes = self._mtime_changed_files(extensions)
        logger.info("Incremental (mtime): %d changed files", len(mtime_changes))
        return mtime_changes

    def _git_changed_files(self) -> Optional[list[str]]:
        """用 git diff 检测变更文件。

        Returns:
            变更文件的相对路径列表，如果不是 git 仓库则返回 None。
        """
        try:
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                capture_output=True, cwd=str(self._root.resolve()), timeout=5,
            )

            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                capture_output=True, text=True, cwd=str(self._root), timeout=10,
            )
            changed = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]

            result_untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True, text=True, cwd=str(self._root), timeout=10,
            )
            untracked = [f.strip() for f in result_untracked.stdout.strip().split("\n") if f.strip()]

            return list(set(changed + untracked))
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            return None

    def _mtime_changed_files(self, extensions: set[str]) -> list[Path]:
        """用文件修改时间检测变更文件。"""
        state = _load_state(self._root)
        last_scan = state.get("last_scan_at")
        old_mtimes = state.get("file_mtimes", {})

        if not last_scan:
            return self._all_files(extensions)

        changed = []
        new_mtimes = {}

        for f in self._all_files(extensions):
            rel = str(f.relative_to(self._root))
            try:
                mtime = str(os.path.getmtime(f))
            except OSError:
                continue

            new_mtimes[rel] = mtime
            if rel not in old_mtimes or old_mtimes[rel] != mtime:
                changed.append(f)

        state["file_mtimes"] = new_mtimes
        state["last_scan_at"] = datetime.now().isoformat()
        _save_state(self._root, state)

        return changed

    def _all_files(self, extensions: set[str]) -> list[Path]:
        """获取项目中所有匹配扩展名的文件。"""
        result = []
        for ext in extensions:
            result.extend(self._root.rglob(f"*{ext}"))
        excluded = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".tox"}
        return [
            f for f in result
            if not any(exc in f.parts for exc in excluded)
        ]

    def mark_scan_complete(self) -> None:
        """标记扫描完成，更新状态文件（用于 mtime 模式）。"""
        state = _load_state(self._root)
        state["last_scan_at"] = datetime.now().isoformat()

        mtimes = {}
        for f in self._all_files({'.py'}):
            rel = str(f.relative_to(self._root))
            try:
                mtimes[rel] = str(os.path.getmtime(f))
            except OSError:
                                logging.exception('异常捕获: ')
        state["file_mtimes"] = mtimes

        _save_state(self._root, state)
        logger.info("Scan state saved: %d files tracked", len(mtimes))

    def scan_changed(self, scanner_registry, extensions: Optional[set[str]] = None) -> Any:
        """只扫描变更文件，返回 Issue 列表。

        Args:
            scanner_registry: ScannerRegistry 实例
            extensions: 文件扩展名过滤

        Returns:
            Issue 列表（只包含变更文件的问题）
        """
        from .adapters import Issue

        changed = self.get_changed_files(extensions)
        if not changed:
            logger.info("Incremental scan: no changes detected")
            return []

        all_issues: list[Issue] = []
        for scanner in scanner_registry.all():
            try:
                issues = scanner.scan(self._root)
                changed_str = {str(f) for f in changed}
                filtered = [i for i in issues if i.file in changed_str]
                for issue in filtered:
                    issue.scanner = scanner.name
                all_issues.extend(filtered)
            except Exception as e:
                logger.error("Incremental scanner %s failed: %s", scanner.name, e)

        self.mark_scan_complete()
        logger.info("Incremental scan: %d issues from %d changed files", len(all_issues), len(changed))
        return all_issues
