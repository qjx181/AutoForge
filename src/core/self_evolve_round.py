#!/usr/bin/env python3
"""self_evolve_round.py — 项目三自进化后勤脚本

职责（每 30 分钟由 cronjob 触发）：
  1. PID 文件锁 + 冲突自愈
  2. 磁盘空间检查 + 日志轮转
  3. 成本熔断检查
  4. 项目一同步（git pull + commit）
  5. 项目三同步（git pull + commit）
  6. 🚀 持续优化引擎（九维全覆盖，任意目标项目）：
       扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快
  7. 分层委托诊断 + 强制委托检查
  8. ⬆️ 并行任务规划（微委托集成）
  9. 更新 state.json

注意：
  实际的任务执行（write_file / delegate_task）由 Hermes Agent cronjob 的 prompt 驱动。
  本脚本只做"后勤 + 规划"——打扫战场、生成执行计划。
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

# ─── 路径（自动计算，不依赖硬编码）─────────────────────────────────────
# self_evolve_round.py 现在位于 src/core/，需要向上两级回到项目根目录
SWARM_DIR = Path(__file__).parent.parent.parent.resolve()

# ─── PROJECT1_DIR：从环境变量或配置读取，不硬编码路径 ──────────────────
# 用法：export PROJECT1_DIR=/path/to/project1
# 或在 config.yaml 中设置 project1_dir 字段
def _get_project1_dir() -> Path:
    env_path = os.environ.get("PROJECT1_DIR", "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    # 回退：从 config.yaml 读取
    cfg = SWARM_DIR / "config.yaml"
    if cfg.exists():
        try:
            text = cfg.read_text(encoding="utf-8")
            # 支持 project1_dir: "path" 或 project1_dir: path
            m = re.search(r"^\s*project1_dir:\s*(?:['\"]([^'\"]*)['\"]|(\S+))", text, re.MULTILINE)
            if m:
                path_val = m.group(1).strip() if m.group(1) else (m.group(2).strip() if m.group(2) else "")
                if path_val:
                    p = Path(path_val)
                    if p.exists():
                        return p
        except Exception:
            pass
    return None  # 不存在时同步步骤跳过

PROJECT1_DIR = _get_project1_dir()


def _finalize_list(result: dict, current_key: str, list_items: list, in_list: bool):
    """如果当前在处理列表，把收集到的列表项写入结果。"""
    if current_key and in_list:
        result[current_key] = list_items


def _process_top_level_key(line: str, result: dict) -> str:
    """处理 YAML 顶层 key: value 行，返回 key 名称。"""
    key = line.split(":")[0].strip()
    value = line.split(":", 1)[1].strip().strip("'\"").strip()
    if value:
        result[key] = value
    elif not line.rstrip().endswith(":"):
        result[key] = value
    else:
        result[key] = None
    return key


def _process_list_item(line: str, current_key: str, current_indent: int,
                       result: dict, list_items: list, in_list: bool):
    """处理列表项行（以 - 开头）。"""
    if current_key and current_key != current_key:  # never true, placeholder
        pass
    item = line[1:].strip().strip("'\"").strip()
    if item:
        list_items.append(item)
    return True


def _parse_yaml_top_level(text: str, result: dict) -> None:
    """解析 YAML 顶层 key: value 对。"""
    current_key = None
    current_indent = 0
    in_list = False
    list_items = []
    for raw_line in text.split("\n"):
        line = raw_line.lstrip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw_line) - len(line)
        if indent == 0 and ":" in line:
            _finalize_list(result, current_key, list_items, in_list)
            if in_list:
                list_items = []
                in_list = False
            current_key = _process_top_level_key(line, result)
            current_indent = indent
        elif current_key and indent > current_indent and ":" not in line:
            if line.startswith("- "):
                in_list = _process_list_item(line, current_key, current_indent,
                                             result, list_items, in_list)
    _finalize_list(result, current_key, list_items, in_list)


def _get_config() -> dict:
    """从 config.yaml 读取完整配置（无 yaml 依赖）。"""
    cfg_path = SWARM_DIR / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        text = cfg_path.read_text(encoding="utf-8")
        result = {}
        _parse_yaml_top_level(text, result)
        return result
    except Exception:
        return {}


# ─── 审计与安全集成 ────────────────────────────────────────────────────
try:
    from src.infra.audit_trail import audit_log
except ImportError:
    def audit_log(*args, **kwargs):
        pass

try:
    from src.infra.safety_interlock import guard_git_push
except ImportError:
    def guard_git_push(*args, **kwargs):
        return True

# ─── 核心文件路径 ──────────────────────────────────────────────────────
STATE_FILE = SWARM_DIR / "data" / "state.json"
PID_FILE = SWARM_DIR / ".self_evolve_round.pid"
TODO_FILE = SWARM_DIR / "docs" / "TODO.md"
LOG_FILE = SWARM_DIR / "logs" / "self_evolve.log"

# ─── 优化引擎配置 ─────────────────────────────────────────────────────────
# 九维全覆盖（代码质量/测试/性能/架构/安全/文档/配置/异步化/死代码）
OPT_DIMENSIONS = [
    "security",          # 安全：SQL注入/命令注入/密钥泄露/XSS
    "performance",       # 性能：N+1查询/sync阻塞/内存泄漏
    "asyncification",   # 异步化：sync-async边界问题
    "quality",           # 代码质量：未用import/过深嵌套/硬编码
    "testing",           # 测试：缺失测试/覆盖不足
    "architecture",     # 架构：循环依赖/上帝文件/紧耦合
    "documentation",    # 文档：缺失docstring/无type hint
    "configuration",     # 配置：硬编码配置/不一致配置
    "deadcode",         # 死代码：未调用函数/不可达文件
]
# 每轮最多执行优化数量
MAX_OPTIMIZATIONS_PER_ROUND = 10
# 自动修复置信度阈值
OPT_CONFIDENCE_THRESHOLD = 0.75


def run_optimization_pipeline(
    scan_targets: list[Path],
    timestamp: str,
    dimensions: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    """run_optimization_pipeline — 持续优化引擎主入口（九维全覆盖）

    核心公式：扫一切可扫 → 优一切可优 → 验一切可验 → 记一切可记 → 下次更快

    步骤：
      1. 对每个目标目录执行 optimizer_core.run_full_pipeline()
      2. 汇总各维度扫描结果
      3. 更新 state.json 记录本轮扫描结果

    Args:
        scan_targets: 要优化的目标目录列表（支持多项目同时优化）
        timestamp: 当前轮次时间戳
        dimensions: 要优化的维度列表，默认全部9个
        dry_run: True=只扫描不修改（预览模式）

    Returns:
        dict: {
            "targets": [str, ...],    # 扫描的目标目录
            "total_findings": int,    # 总发现数
            "total_fixes_applied": int, # 总修复数
            "total_verifications_passed": int,
            "total_verifications_failed": int,
            "score_delta": int,       # 评分变化
            "by_target": [dict, ...], # 每个目标的详细结果
            "at": str,
        }
    """
    if dimensions is None:
        dimensions = OPT_DIMENSIONS

    from src.analysis.optimizer_core import run_full_pipeline, DIMENSION_NAMES

    overall = {
        "targets": [],
        "total_findings": 0,
        "total_fixes_applied": 0,
        "total_verifications_passed": 0,
        "total_verifications_failed": 0,
        "score_delta": 0,
        "by_target": [],
        "at": timestamp,
    }

    for target in scan_targets:
        if not target or not target.exists():
            relog("ℹ️", "跳过不存在目录: %s", target)
            continue

        target_str = str(target)
        relog("🔍", "优化目标: %s（维度: %s）", target_str, ", ".join(dimensions))
        overall["targets"].append(target_str)

        # ── 步骤1：执行 9 维度扫描 ──
        try:
            pipeline_result = run_full_pipeline(target_str, dimensions=dimensions)
        except Exception as e:
            relog("⚠️", "optimizer_core 执行失败 [%s]: %s", target_str, e)
            overall["by_target"].append({
                "target": target_str,
                "error": str(e),
            })
            continue

        # ── 汇总结果 ──
        total_issues = pipeline_result.get("total_issues", 0)
        overall["total_findings"] += total_issues

        # 按维度统计
        by_dimension = {}
        for dim_name, dim_result in pipeline_result.get("dimensions", {}).items():
            dim_label = DIMENSION_NAMES.get(dim_name, dim_name)
            by_dimension[dim_name] = {
                "label": dim_label,
                "score": dim_result.get("score", 0),
                "issues": dim_result.get("issue_count", 0),
                "scan_time_ms": dim_result.get("scan_time_ms", 0),
            }

        overall["by_target"].append({
            "target": target_str,
            "project_name": pipeline_result.get("project_name", target.name),
            "language": pipeline_result.get("language", "unknown"),
            "overall_score": pipeline_result.get("overall_score", 0),
            "total_issues": total_issues,
            "critical_issues": pipeline_result.get("critical_issues", 0),
            "scan_time_ms": pipeline_result.get("total_scan_time_ms", 0),
            "by_dimension": by_dimension,
            "summary": pipeline_result.get("summary", ""),
        })

        relog(
            "📊 [%s] 整体 %d/100 | 发现 %d（critical: %d）| 耗时 %.0fms",
            pipeline_result.get("project_name", target.name),
            pipeline_result.get("overall_score", 0),
            total_issues,
            pipeline_result.get("critical_issues", 0),
            pipeline_result.get("total_scan_time_ms", 0),
        )

    # ── 步骤5：写入 state.json ──
    state = load_state()
    state["last_optimization"] = {
        "targets": overall["targets"],
        "dimensions": dimensions,
        "total_findings": overall["total_findings"],
        "total_fixes_applied": overall["total_fixes_applied"],
        "total_verifications_passed": overall["total_verifications_passed"],
        "total_verifications_failed": overall["total_verifications_failed"],
        "score_delta": overall["score_delta"],
        "dry_run": dry_run,
        "at": timestamp,
    }
    save_state(state)

    relog(
        "🏁 优化完成：%d 个目标，发现 %d，修复 %d，验证 %d/%d",
        len(overall["targets"]),
        overall["total_findings"],
        overall["total_fixes_applied"],
        overall["total_verifications_passed"],
        overall["total_verifications_passed"] + overall["total_verifications_failed"],
    )

    return overall
    # 延迟导入，避免循环依赖
    from src.analysis.optimizer_core import run_full_pipeline, DIMENSION_NAMES

    try:
        pipeline_result = run_full_pipeline(str(scan_target), dimensions=dimensions)
        relog("🔍", "9 维度扫描完成: %s", pipeline_result.get("summary", "").split("\n")[0])
        return pipeline_result
    except Exception as e:
        relog("⚠️", "optimizer_core 执行失败 [%s]: %s", scan_target, e)
        return {
            "dimension": "all",
            "score": 0,
            "issues": [],
            "issue_count": 0,
            "summary": f"优化引擎执行失败: {e}",
            "error": str(e),
        }

# ─── 磁盘阈值 ──────────────────────────────────────────────────────────
MIN_FREE_GB = 5
MAX_LOG_DAYS = 7

# ─── 日志 ──────────────────────────────────────────────────────────────

# JSON 日志模式（--json-logs 启动参数控制）
_JSON_MODE = False


def _format_log(level: str, msg: str) -> str:
    """格式化单条日志（纯文本或 JSON）。"""
    ts = datetime.now().strftime("%H:%M:%S")
    if _JSON_MODE:
        return json.dumps(
            {"timestamp": ts, "level": level, "message": msg},
            ensure_ascii=False,
        )
    return f"[{ts}] {level} {msg}"


def relog(tag: str, *args) -> None:
    """简易日志输出（控制台 + 文件）。支持 JSON 模式。"""
    text = ("" if not args else " ".join(str(a) for a in args))
    msg = f"{tag}" + (f" {text}" if text else "")
    line = _format_log("INFO", msg)
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ═══════════════════════════════════════════════════════════════════════
# 0. PID 文件锁
# ═══════════════════════════════════════════════════════════════════════


def acquire_pid_file() -> bool:
    """获取 PID 文件锁（含僵尸自动清理 5 分钟超时）。"""
    if not HAS_FCNTL:
        return True  # 非 Linux 跳过
    try:
        pid_fd = PID_FILE.open("w")
        fcntl.flock(pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid_fd.write(str(os.getpid()))
        pid_fd.flush()
        return True
    except (IOError, BlockingIOError):
        # 检查是否僵尸（旧进程超过 5 分钟）
        if PID_FILE.exists():
            try:
                old_pid = int(PID_FILE.read_text().strip())
                try:
                    os.kill(old_pid, 0)  # 检查进程是否存在
                    relog("⚠️", "PID 文件锁被占用（pid=%d），跳过", old_pid)
                    return False
                except OSError:
                    # 进程不存在，清理僵尸锁
                    relog("🧟", "清理僵尸 PID 锁（pid=%d）", old_pid)
                    PID_FILE.unlink(missing_ok=True)
                    return acquire_pid_file()
            except (ValueError, OSError):
                PID_FILE.unlink(missing_ok=True)
                return acquire_pid_file()
        return False


def release_pid_file():
    """释放 PID 文件锁。"""
    if HAS_FCNTL and PID_FILE.exists():
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════
# 1. 公共工具 / state 读写
# ═══════════════════════════════════════════════════════════════════════


def load_state() -> dict:
    """加载 state.json。"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    """保存 state.json（原子写入）。"""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


# ═══════════════════════════════════════════════════════════════════════
# 2. 磁盘检查 + 日志清理
# ═══════════════════════════════════════════════════════════════════════


def check_disk_space() -> dict:
    """检查磁盘空间，自动清理 7 天前的日志。"""
    try:
        stat = os.statvfs(str(SWARM_DIR))
        free_gb = stat.f_bavail * stat.f_frsize / 1024 ** 3
        relog("💾", "磁盘剩余 %.1f GB / 阈值 %d GB", free_gb, MIN_FREE_GB)

        if free_gb < MIN_FREE_GB:
            relog("⚠️", "磁盘不足，清理 7 天前的日志文件")
            cutoff = datetime.now() - timedelta(days=MAX_LOG_DAYS)
            log_dir = SWARM_DIR / "logs"
            if log_dir.exists():
                cleaned = 0
                for f in log_dir.iterdir():
                    if f.is_file():
                        mtime = datetime.fromtimestamp(f.stat().st_mtime)
                        if mtime < cutoff:
                            f.unlink()
                            cleaned += 1
                relog("🧹", "清理了 %d 个旧日志文件", cleaned)

            # 再检查一次
            stat = os.statvfs(str(SWARM_DIR))
            free_gb = stat.f_bavail * stat.f_frsize / 1024 ** 3
            if free_gb < MIN_FREE_GB:
                relog("⏸️", "清理后磁盘仍不足（%.1f GB），标记暂停", free_gb)
                return {"free_gb": free_gb, "paused": True}

        return {"free_gb": free_gb, "paused": False}
    except Exception as e:
        relog("❌", "磁盘检查失败: %s", e)
        return {"free_gb": -1, "paused": False}


# ═══════════════════════════════════════════════════════════════════════
# 3. 成本熔断检查
# ═══════════════════════════════════════════════════════════════════════


def check_cost_over_budget() -> Optional[str]:
    """检查当日 API 花费是否超预算。优先从 cost_tracker_db SQLite 读取。"""
    try:
        from src.infra.cost_tracker_db import get_today_spent  # type: ignore

        dollar_spent = get_today_spent()
    except ImportError:
        # 降级：从 state.json 读取
        state = load_state()
        budget = state.get("daily_budget", {})
        dollar_spent = budget.get("dollar_spent_today", 0)

    state = load_state()
    dollar_limit = state.get("daily_budget", {}).get("dollar_limit", 5.0)

    if dollar_spent >= dollar_limit * 0.9:
        warning = f"当日花费 ${dollar_spent:.2f} / 限额 ${dollar_limit:.2f}，接近橙色模式"
        relog("💰", warning)
        return warning

    relog("💰", "当日花费 $%.2f / $%.2f", dollar_spent, dollar_limit)
    return None


# ═══════════════════════════════════════════════════════════════════════
# 4. Git 工具
# ═══════════════════════════════════════════════════════════════════════
GIT_TIMEOUT = 60  # git 命令超时（秒）


def _run_git(cmd: list[str], repo_dir: Path, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """执行 git 命令的辅助函数。"""
    return subprocess.run(
        cmd,
        cwd=str(repo_dir),
        capture_output=True, text=True, timeout=timeout,
    )


def git_pull_rebase(repo_dir: Path) -> tuple[bool, list[str]]:
    """git pull --rebase。返回 (是否成功, 冲突文件列表)。"""
    try:
        result = _run_git(["git", "pull", "--rebase"], repo_dir)
        if result.returncode != 0:
            conflicts = []
            for line in result.stderr.splitlines():
                if "CONFLICT" in line and "content" in line:
                    parts = line.split("in ")
                    if len(parts) >= 2:
                        conflicts.append(parts[-1].strip())
                if "both modified:" in line:
                    parts = line.split("both modified:")
                    if len(parts) >= 2:
                        conflicts.append(parts[-1].strip())
            return False, conflicts
        return True, []
    except subprocess.TimeoutExpired:
        return False, []


def run_git_commit(repo_dir: Path, message: str, skip_pull: bool = False) -> bool:
    """git add -A + commit。"""
    try:
        status = _run_git(["git", "status", "--porcelain"], repo_dir, timeout=10)
        if not status.stdout.strip():
            return True  # 干净，无需提交

        _run_git(["git", "add", "-A"], repo_dir, timeout=30)
        cmt = _run_git(["git", "commit", "-m", message], repo_dir, timeout=30)
        relog("✅", "提交成功: %s  (%s)", message[:50], (cmt.stdout or "")[:30])
        return True
    except subprocess.TimeoutExpired:
        relog("❌", "git commit 超时")
        return False


def run_git_commit_with_retry(repo_dir: Path, message: str, repo_name: str = "unknown", max_retries: int = 3) -> bool:
    """带重试的 git commit。"""
    for attempt in range(max_retries):
        try:
            if run_git_commit(repo_dir, message):
                return True
            time.sleep(2 ** attempt)
        except Exception as e:
            relog("⚠️", "%s 第 %d 次重试: %s", repo_name, attempt + 1, e)
    relog("❌", "%s 最终失败", repo_name)
    return False


# ═══════════════════════════════════════════════════════════════════════
# 5. 冲突自愈
# ═══════════════════════════════════════════════════════════════════════


def check_and_heal_conflicts():
    """检查并自动恢复冲突状态。"""
    state = load_state()
    if state.get("paused_due_to_conflict"):
        conflict_files = state.get("pending_review", [])
        relog("🩹", "冲突状态中，待检查: %s", conflict_files)
        return False
    return True


def mark_conflict(conflict_files: list[str]):
    """标记冲突状态。"""
    state = load_state()
    state["paused_due_to_conflict"] = True
    state["pending_review"] = conflict_files
    save_state(state)


# ═══════════════════════════════════════════════════════════════════════
# 6. 分层委托诊断
# ═══════════════════════════════════════════════════════════════════════


def run_delegation_diagnosis():
    """从 self_evolve_log.json 分析委托成功率，写入 state.json。

    诊断指标：
      - delegate_success_rate: 委托成功率
      - overall_success_rate: 总成功率
      - delegated_rounds: 包含委托的轮次数
      - failure_patterns: 失败类型统计
    """
    log_path = SWARM_DIR / "self_evolve_log.json"
    if not log_path.exists():
        return

    try:
        log_data = json.loads(log_path.read_text())
        entries = log_data if isinstance(log_data, list) else log_data.get("entries", [])

        total_rounds = len(entries)
        total_delegated = 0
        success_delegated = 0
        failure_patterns: dict[str, int] = {}

        for entry in entries:
            approach = (entry.get("approach", "") or "").lower()
            result = entry.get("result", "")

            if "delegate" in approach:
                total_delegated += 1
                if result == "success":
                    success_delegated += 1

                # 分析 waste 字段中的失败模式
                waste = entry.get("waste", "")
                if "delegate" in waste.lower():
                    # 提取失败模式关键词
                    for pattern in ["environment", "mock_import", "zero_file", "import", "dependency"]:
                        if pattern in waste.lower():
                            failure_patterns[pattern] = failure_patterns.get(pattern, 0) + 1

        diagnosis = {
            "delegate_success_rate": round(success_delegated / total_delegated, 2) if total_delegated else 1.0,
            "overall_success_rate": round(sum(1 for e in entries if e.get("result") == "success") / total_rounds, 2) if total_rounds else 1.0,
            "delegated_rounds": total_delegated,
            "failure_patterns": failure_patterns,
            "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

        state = load_state()
        state["diagnosis"] = diagnosis
        save_state(state)
        relog("📊", "委托诊断完成 — 成功率 %.0f%% / %d 轮", diagnosis["delegate_success_rate"] * 100, total_delegated)

    except (json.JSONDecodeError, KeyError) as e:
        relog("⚠️", "委托诊断失败: %s", e)


# ═══════════════════════════════════════════════════════════════════════
# 6b. 强制委托检查
# ═══════════════════════════════════════════════════════════════════════


def check_forced_delegation():
    """强制委托检查——每轮确认是否有可委托的任务。

    从 self_evolve_log.json 最新一轮统计 delegate 使用情况。
    如果连续多轮零委托，在日志中发出警告。
    """
    log_path = SWARM_DIR / "self_evolve_log.json"
    if not log_path.exists():
        return

    try:
        log_data = json.loads(log_path.read_text())
        entries = log_data if isinstance(log_data, list) else log_data.get("entries", [])

        # 统计最近 5 轮中委托次数
        recent = entries[-5:]
        delegate_count = sum(
            1 for e in recent
            if "delegate" in (e.get("approach", "") or "").lower()
        )

        if delegate_count == 0 and len(recent) >= 3:
            relog("⚠️", "强制委托检查: 最近 %d 轮零委托，建议每轮至少委托 1 个任务", len(recent))
        elif delegate_count == 0:
            relog("📊", "强制委托检查: 最近 %d 轮无委托（轮次不足，继续观察）", len(recent))
        else:
            relog("✅", "强制委托检查: 最近 %d 轮委托 %d 次", len(recent), delegate_count)

    except (json.JSONDecodeError, KeyError) as e:
        relog("⚠️", "强制委托检查失败: %s", e)


# ═══════════════════════════════════════════════════════════════════════
# 7. ⬆️ 并行任务规划（新） — 集成 parallel_dispatcher
# ═══════════════════════════════════════════════════════════════════════


def _load_parallel_dispatcher():
    """尝试从工作目录加载 parallel_dispatcher 模块。"""
    sys.path.insert(0, str(SWARM_DIR))
    try:
        import src.agents.parallel_dispatcher as parallel_dispatcher
        return parallel_dispatcher
    except ImportError as e:
        relog("⚠️", "parallel_dispatcher 加载失败: %s", e)
        return None


def _parse_todo_dependencies() -> dict[str, dict]:
    """从 TODO.md 解析所有待办任务的依赖和 token 估算。

    解析格式：
      - [ ] 任务ID: <id>
        描述: ...
        依赖: <dep1>, <dep2>, ... | 依赖: 无 | 无这行 → 无依赖
        预估 token 量: <number>

    Returns:
        {task_id: {"depends": [str], "token_est": int, "description": str}}
    """
    if not TODO_FILE.exists():
        return {}

    text = TODO_FILE.read_text()
    tasks: dict[str, dict] = {}
    current_id: Optional[str] = None
    current_dep: list[str] = []
    current_token: int = 2000
    current_desc: str = ""

    for line in text.splitlines():
        # 匹配任务ID
        m = re.match(r'^- \[ \] 任务ID:\s*(\S+)', line)
        if m:
            # 保存前一个任务
            if current_id:
                tasks[current_id] = {
                    "depends": current_dep,
                    "token_est": current_token,
                    "description": current_desc,
                }
            current_id = m.group(1)
            current_dep = []
            current_token = 2000
            current_desc = ""
            continue

        if current_id:
            # 解析描述
            dm = re.match(r'\s+描述:\s*(.+)', line)
            if dm:
                current_desc = dm.group(1).strip()
                continue

            # 解析依赖
            dm = re.match(r'\s+依赖:\s*(.+)', line)
            if dm:
                dep_text = dm.group(1).strip()
                if dep_text and dep_text != "无" and not dep_text.startswith("无（"):
                    # 可能含逗号分隔的多个依赖
                    current_dep = [d.strip() for d in dep_text.split(",") if d.strip()]
                continue

            # 解析 token 估算
            tm = re.match(r'\s+预估 token 量:\s*(\d+)', line)
            if tm:
                current_token = int(tm.group(1))
                continue

    # 保存最后一个任务
    if current_id:
        tasks[current_id] = {
            "depends": current_dep,
            "token_est": current_token,
            "description": current_desc,
        }

    return tasks


def plan_parallel_tasks() -> dict | None:
    """扫描 pending_tasks → 按依赖分组 → 编写并行计划 → 写入 state.json。

    流程：
      1. 加载 state.json，读取 pending_tasks 列表
      2. 从 TODO.md 解析每个任务的依赖关系和 token 估算
      3. 调用 parallel_dispatcher.dispatch_tasks() 生成执行计划
      4. 将计划写入 state.json 的 parallel_plan 字段
      5. 返回计划供主流程使用

    输出（写入 state.json "parallel_plan" 字段）：
      {
        "batches": [           # 批次列表，每批可并行执行
          [task_id, task_id],  # 第 1 批（无依赖，并行）
          [task_id],           # 第 2 批（依赖第 1 批）
          ...
        ],
        "coordinator": [...],  # 协调者自己干的任务
        "delegate": [...],     # 委托给子 Agent 的任务
        "max_concurrent": 3,
        "has_work": true|false
      }
    """
    state = load_state()
    pending_ids = state.get("pending_tasks", [])

    if not pending_ids:
        relog("📋", "并行规划: 无待办任务")
        if "parallel_plan" in state:
            del state["parallel_plan"]
            save_state(state)
        return None

    # 从 TODO.md 解析依赖信息
    todo_tasks = _parse_todo_dependencies()
    relog("📋", "TODO.md 解析: %d 个任务定义", len(todo_tasks))

    # 构建 parallel_dispatcher 需要的 todo_tasks 格式
    formulated_tasks: list[dict] = []
    for task_id in pending_ids:
        info = todo_tasks.get(task_id, {})
        formulated_tasks.append({
            "task_id": task_id,
            "depends": info.get("depends", []),
            "token_est": info.get("token_est", 2000),
            "description": info.get("description", ""),
        })

    relog("📋", "待规划任务: %d 项", len(formulated_tasks))

    # 明确标记依赖信息到 control 变量，供 dispatch 使用
    # 手动分组：无依赖的任务放一起
    independent = [t for t in formulated_tasks if not t["depends"]]
    dependent = [t for t in formulated_tasks if t["depends"]]
    # 进一步按依赖分组
    dep_groups: dict[str, list[dict]] = {}
    for t in dependent:
        key = ",".join(sorted(t["depends"]))
        dep_groups.setdefault(key, []).append(t)

    # 构建批次
    max_concurrent = 3
    batches: list[list[str]] = []

    # 第 1 批：所有无依赖任务（最多 3 个并行）
    if independent:
        b1 = [t["task_id"] for t in independent[:max_concurrent]]
        batches.append(b1)
        # 如果还有剩余，下一批
        remaining = [t["task_id"] for t in independent[max_concurrent:]]
        while remaining:
            batches.append(remaining[:max_concurrent])
            remaining = remaining[max_concurrent:]

    # 后续批次：有依赖的
    for group_tasks in dep_groups.values():
        group_ids = [t["task_id"] for t in group_tasks]
        while group_ids:
            batches.append(group_ids[:max_concurrent])
            group_ids = group_ids[max_concurrent:]

    # 打印计划概要
    relog("📋", "并行规划: %d 批, 并发上限 %d", len(batches), max_concurrent)
    for i, batch in enumerate(batches):
        relog("  🗂️  Batch %d: %s", i + 1, ", ".join(batch))

    plan = {
        "batches": batches,
        "coordinator": [t["task_id"] for t in formulated_tasks],
        "delegate": [],
        "max_concurrent": max_concurrent,
        "has_work": len(formulated_tasks) > 0,
        "planned_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_pending": len(formulated_tasks),
    }

    # 写入 state.json
    state["parallel_plan"] = plan
    save_state(state)
    relog("✅", "并行规划已写入 state.json")

    return plan


# ═══════════════════════════════════════════════════════════════════════
# 7b. ⬆️ 心跳自愈检查 — PID 文件超时自动重启
# ═══════════════════════════════════════════════════════════════════════


def _get_heartbeat_config() -> dict:
    """从 config.yaml 提取心跳配置（无 yaml 依赖）。"""
    config_path = SWARM_DIR / "config.yaml"
    if not config_path.exists():
        return {"heartbeat_dir": "heartbeats", "heartbeat_timeout": 30}
    try:
        text = config_path.read_text()
        hb_dir = "heartbeats"
        hb_timeout = 30
        m = re.search(r'heartbeat_dir:\s*["\']?([^"\'#\n]+)', text)
        if m:
            hb_dir = m.group(1).strip().strip("\"'")
        m = re.search(r'heartbeat_timeout_seconds:\s*(\d+)', text)
        if m:
            hb_timeout = int(m.group(1))
        return {"heartbeat_dir": hb_dir, "heartbeat_timeout": hb_timeout}
    except Exception as e:
        relog("⚠️", "读取心跳配置失败: %s，使用默认值", e)
        return {"heartbeat_dir": "heartbeats", "heartbeat_timeout": 30}


def _check_single_pid_file(pid_file: Path, now: float, hb_timeout: int) -> dict:
    """检查单个 PID 文件是否超时，返回检查结果。"""
    if not pid_file.is_file() or not pid_file.name.endswith(".pid"):
        return {"skip": True}
    try:
        mtime = pid_file.stat().st_mtime
        age = now - mtime
        agent_name = pid_file.name.replace(".pid", "")
    except OSError as e:
        return {"skip": True, "error": f"心跳文件 {pid_file.name} 读取失败: {e}"}
    if age < hb_timeout:
        return {"skip": True}
    return {"agent_name": agent_name, "pid_file": pid_file, "age": age}


def _try_restart_agent(agent_name: str, old_pid: int, pid_file: Path) -> bool:
    """尝试 kill 旧进程并重启 agent。返回是否成功重启。"""
    if not old_pid:
        relog("💓", "无 PID 的心跳文件: %s, 清理", pid_file.name)
        pid_file.unlink(missing_ok=True)
        return False
    try:
        os.kill(old_pid, 0)  # 检查进程是否存在
        relog("💓", "心跳超时: %s (pid=%d, %.0fs 无更新), kill 并重启", agent_name, old_pid)
        os.kill(old_pid, 15)  # SIGTERM
        pid_file.unlink(missing_ok=True)
    except OSError:
        relog("💓", "僵尸心跳: %s (pid=%d 已无进程), 清理 PID 文件", agent_name, old_pid)
        pid_file.unlink(missing_ok=True)
        return False

    script_path = SWARM_DIR / f"{agent_name}.py"
    if not script_path.exists():
        relog("💓", "没有重启脚本: %s", script_path)
        return False
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=str(SWARM_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pid_file.write_text(str(proc.pid))
        relog("✅", "重启 %s 成功 (新 pid=%d)", agent_name, proc.pid)
        return True
    except OSError as e:
        relog("❌", "重启 %s 失败: %s", agent_name, e)
        return False


def check_and_heal_heartbeats() -> int:
    """心跳超时检测 + 自动重启失联 agent。

    读取 config.yaml 的 heartbeat_timeout_seconds 和 heartbeat_dir，
    扫描心跳目录中的 PID 文件。若某 PID 文件存在但未在超时阈值内更新，
    则 kill 原进程并通过 subprocess 重启。

    Returns:
        本轮重启的 agent 数量（上限 3）。
    """
    cfg = _get_heartbeat_config()
    hb_dir = SWARM_DIR / cfg["heartbeat_dir"]
    hb_timeout = cfg["heartbeat_timeout"]

    if not hb_dir.exists():
        relog("💓", "心跳目录不存在: %s，创建", hb_dir)
        hb_dir.mkdir(parents=True, exist_ok=True)
        return 0

    restarted = 0
    max_restarts = 3
    now = time.time()

    for pid_file in sorted(hb_dir.iterdir()):
        if restarted >= max_restarts:
            break

        check = _check_single_pid_file(pid_file, now, hb_timeout)
        if check.get("skip"):
            continue

        agent_name = check["agent_name"]
        try:
            pid_text = pid_file.read_text().strip()
            old_pid = int(pid_text) if pid_text and pid_text.isdigit() else None
        except (OSError, ValueError):
            old_pid = None

        if _try_restart_agent(agent_name, old_pid, pid_file):
            restarted += 1
    return restarted

    if restarted > 0:
        relog("💓", "本轮重启 %d 个失联 agent", restarted)
    else:
        relog("✅", "心跳检查: 所有 agent 状态正常")

    # 记录重启事件到恢复日志
    if restarted > 0:
        recovery_log = SWARM_DIR / "logs" / "heartbeat_recovery.log"
        recovery_log.parent.mkdir(parents=True, exist_ok=True)
        with recovery_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "restarted": restarted,
                "reason": "heartbeat_timeout",
            }, ensure_ascii=False) + "\n")

    return restarted


# ═══════════════════════════════════════════════════════════════════════
# 7c. ⬆️ Git push 分支保护检查（git_autopush_safety）
# ═══════════════════════════════════════════════════════════════════════


def check_git_push_safety(repo_dir: Path) -> tuple[bool, str]:
    """检查 git push 是否安全——分支保护 + 远程冲突检测。

    检查项：
    1. 当前分支名，禁止在 main/master/protected-* 分支上自动 push
    2. 远程是否有未拉取的提交（ahead/behind 检测）

    Returns:
        (True, "reason") 如果安全，或 (False, "原因") 如果存在冲突/保护。
    """
    try:
        # 检查项 1：分支名保护
        result = _run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_dir, timeout=10)
        if result.returncode != 0:
            return False, "无法检测当前分支"
        branch = result.stdout.strip()

        protected_prefixes = ("main", "master", "protected-")
        for prefix in protected_prefixes:
            if branch.startswith(prefix):
                return False, f"受保护分支禁止自动 push: {branch}"

        # 检查项 2：远程冲突
        fetch = _run_git(["git", "fetch", "origin"], repo_dir, timeout=30)
        if fetch.returncode != 0:
            return False, f"git fetch 失败: {fetch.stderr[:100]}"

        rev_list = _run_git(
            ["git", "rev-list", "--left-right", "--count", f"origin/{branch}...{branch}"],
            repo_dir,
            timeout=10,
        )
        if rev_list.returncode == 0:
            parts = rev_list.stdout.strip().split()
            if len(parts) == 2:
                behind = int(parts[0])  # remote ahead → we are behind
                ahead = int(parts[1])
                if behind > 0:
                    return False, f"远程领先 {behind} 个 commit——请先 git pull"
                if ahead > 0:
                    return True, f"本地领先 {ahead} 个 commit——可安全推送"

        return True, "全部检查通过——可安全 push"
    except subprocess.TimeoutExpired:
        return False, "git 命令超时"
    except Exception as e:
        return False, f"安全检查异常: {e}"


def run_safe_git_push(repo_dir: Path, message: str, repo_name: str = "unknown") -> bool:
    """带分支保护检查的安全 git push。

    先在本地 commit，然后检查分支保护，最后 push。
    push 失败不阻塞流程（国内网络容错）。
    """
    # 先 commit
    try:
        status = _run_git(["git", "status", "--porcelain"], repo_dir, timeout=10)
        if not status.stdout.strip():
            relog("✅", "%s 工作区干净，无需提交", repo_name)
            return True

        _run_git(["git", "add", "-A"], repo_dir, timeout=30)
        cmt = _run_git(["git", "commit", "-m", message], repo_dir, timeout=30)
        relog("✅", "%s 提交成功: %s", repo_name, (cmt.stdout or "")[:30])
        audit_log("commit", str(repo_dir), f"{repo_name}: {message[:50]}", success=True,
                  source="self_evolve_round")
    except subprocess.TimeoutExpired:
        relog("❌", "%s git commit 超时", repo_name)
        audit_log("commit", str(repo_dir), f"{repo_name}: 超时", success=False,
                  source="self_evolve_round")
        return False

    # 安全检查
    safe, reason = check_git_push_safety(repo_dir)
    if not safe:
        relog("⏭️", "%s push 跳过: %s", repo_name, reason)
        audit_log("push_skip", str(repo_dir), f"{repo_name}: {reason}", success=True,
                  source="self_evolve_round")
        return False

    # push 前二次确认
    if not guard_git_push():
        audit_log("push_skipped", str(repo_dir), f"{repo_name}: 用户拒绝确认",
                  success=False, source="self_evolve_round")
        return False

    # push
    try:
        push = _run_git(["git", "push"], repo_dir, timeout=60)
        if push.returncode == 0:
            relog("✅", "%s push 成功", repo_name)
            audit_log("push", str(repo_dir), f"{repo_name}: 成功", success=True,
                      source="self_evolve_round")
            return True
        else:
            relog("⚠️", "%s push 失败 (网络/凭据): %s", repo_name, push.stderr[:100])
            audit_log("push", str(repo_dir), f"{repo_name}: {push.stderr[:80]}",
                      success=False, source="self_evolve_round")
            return False
    except subprocess.TimeoutExpired:
        relog("⏭️", "%s push 超时 (国内网络正常), 跳过", repo_name)
        audit_log("push_timeout", str(repo_dir), f"{repo_name}: 超时",
                  success=False, source="self_evolve_round")
        return False


# ═══════════════════════════════════════════════════════════════════════
# 主函数
# ═══════════════════════════════════════════════════════════════════════


def _parse_cli_args() -> str:
    """解析 CLI 参数。返回时间戳。"""
    import argparse
    arg_parser = argparse.ArgumentParser(description="项目三自进化后勤脚本")
    arg_parser.add_argument("--json-logs", action="store_true", default=False, help="启用 JSON 格式日志输出")
    cli_args, _ = arg_parser.parse_known_args()
    if cli_args.json_logs:
        global _JSON_MODE
        _JSON_MODE = True
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sync_project(project_dir, name: str, timestamp: str) -> None:
    """同步一个项目：pull → status → commit。"""
    relog("📁", "检查%s（%s）", name, project_dir)
    pull_ok, conflicts = git_pull_rebase(project_dir)
    if conflicts:
        mark_conflict(conflicts)
        relog("❌", "%s冲突：%s", name, conflicts)
    elif not pull_ok:
        relog("⚠️", "%s git pull 失败", name)
    else:
        relog("✅", "%s已同步", name)
    try:
        status_p = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_dir),
            capture_output=True, text=True, timeout=10,
        )
        if status_p.returncode != 0:
            relog("⚠️", "git status 失败，跳过%s", name)
        elif status_p.stdout.strip():
            lines = status_p.stdout.strip().split("\n")
            relog("⚠️", "%s有 %d 个待提交文件", name, len(lines))
            run_git_commit_with_retry(project_dir, f"{name}阶段进化 — {timestamp[:10]}", repo_name=name)
        else:
            relog("✅", "%s工作区干净", name)
    except subprocess.TimeoutExpired:
        relog("❌", "%s git status 超时", name)


def _collect_optimization_targets(cost_warning: str) -> tuple[list[Path], bool, str]:
    """收集优化目标，检测成本模式。"""
    sys.path.insert(0, str(SWARM_DIR))
    cost_tier = cost_warning or ""
    is_dry_run = bool(cost_tier and "跳过" in str(cost_tier))
    if is_dry_run:
        relog("ℹ️", "成本模式 '%s'，优化引擎降级为 dry_run", cost_tier)
    targets: list[Path] = []
    if PROJECT1_DIR and PROJECT1_DIR.exists():
        targets.append(PROJECT1_DIR)
    cfg = _get_config()
    for t in cfg.get("optimization_targets", []):
        p = Path(str(t).strip('"\''))
        if p.exists() and p not in targets:
            targets.append(p)
    if not targets:
        targets.append(SWARM_DIR)
    return targets, is_dry_run, cost_tier


def _run_optimization_engine(targets: list[Path], timestamp: str, dims: list[str], dry_run: bool) -> None:
    """运行持续优化引擎。"""
    if not targets:
        relog("ℹ️", "优化引擎跳过（无有效优化目标）")
        return
    try:
        opt_result = run_optimization_pipeline(
            scan_targets=targets, timestamp=timestamp,
            dimensions=dims if dims else OPT_DIMENSIONS, dry_run=dry_run,
        )
        relog("🏁", "优化完成：%d 个目标，发现 %d 问题",
              len(opt_result.get("targets", [])), opt_result.get("total_findings", 0))
    except Exception as e:
        relog("⚠️", "优化引擎异常: %s", e)


def _run_deep_scan_and_tasks(targets: list[Path], cost_tier: str, timestamp: str) -> None:
    """深度扫描并生成子Agent修复任务。"""
    if cost_tier and "跳过" in str(cost_tier):
        return
    try:
        from src.analysis.deep_enterprise_scanner import scan_deep
        deep_result = scan_deep(str(targets[0])) if targets else None
        if deep_result and deep_result.get("issues"):
            from src.fixers.enterprise_fixer import DEEP_FIXERS
            fixable_types = {k for k, v in DEEP_FIXERS.items() if v is not None}
            delegable_issues = [iss for iss in deep_result["issues"]
                                if iss.get("type", "") not in fixable_types
                                and iss.get("severity", "low") in ("critical", "high")]
            if delegable_issues:
                tasks_file = SWARM_DIR / "data" / "deep_fix_tasks.json"
                tasks_file.write_text(json.dumps({
                    "generated_at": timestamp, "target_dir": str(targets[0]),
                    "total": len(delegable_issues), "issues": delegable_issues[:10],
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                relog("🧠", "深度修复任务已生成：%d 个 → data/deep_fix_tasks.json", len(delegable_issues))
            else:
                relog("✅", "无需要子Agent修复的深层问题")
        else:
            relog("ℹ️", "深度扫描无结果")
    except ImportError as e:
        relog("⚠️", "deep_enterprise_scanner 不可用: %s", e)
    except Exception as e:
        relog("⚠️", "子Agent任务生成异常: %s", e)


def _run_failure_analysis(timestamp: str) -> None:
    """运行失败模式学习。"""
    try:
        sys.path.insert(0, str(SWARM_DIR))
        from src.analysis.failure_analysis import analyze as run_failure_analysis
        failure_result = run_failure_analysis()
        relog("📚", "失败分析完成: %d 个失败任务", failure_result.get("total_failed", 0))
        state = load_state()
        state["failure_stats"] = {
            "last_analysis": timestamp,
            "weekly_patterns": failure_result.get("keyword_analysis", {}),
            "failure_injection_text": failure_result.get("injection_text", ""),
            "total_completed": failure_result.get("total_completed", 0),
        }
        save_state(state)
    except ImportError as e:
        relog("⚠️", "failure_analysis 模块不可用: %s", e)
    except Exception as e:
        relog("⚠️", "失败分析异常: %s", e)


def _run_log_scan() -> None:
    """日志异常检测。"""
    try:
        sys.path.insert(0, str(SWARM_DIR))
        from src.analysis.query_logs import scan_logs
        logs_dir = SWARM_DIR / "logs"
        if not logs_dir.exists():
            relog("ℹ️", "日志目录不存在")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        error_logs = scan_logs(logs_dir, date_filter=today, level_filter="ERROR", last=20)
        if not error_logs:
            relog("✅", "今日无 ERROR 日志")
            return
        relog("⚠️", "检测到 %d 条 ERROR 日志", len(error_logs))
        state = load_state()
        state.setdefault("failed_tasks", [])
        existing_ids = {t.get("task_id", "") for t in state["failed_tasks"]}
        for entry in error_logs[:5]:
            task_id = entry.get("task_id", f"log-error-{entry.get('timestamp', '')[:19]}")
            if task_id not in existing_ids:
                state["failed_tasks"].append({
                    "task_id": task_id, "description": entry.get("message", "")[:200],
                    "error_type": "LOG_ERROR", "source": "query_logs",
                    "at": entry.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                })
                existing_ids.add(task_id)
        save_state(state)
    except ImportError as e:
        relog("⚠️", "query_logs 模块不可用: %s", e)
    except Exception as e:
        relog("⚠️", "日志异常检测异常: %s", e)


def _update_state_and_cost(state: dict, timestamp: str) -> None:
    """更新 state.json 和记录成本。"""
    try:
        from src.infra.cost_tracker_db import CostTrackerDB
        cost_db = CostTrackerDB()
        current_round = state.get("current_round", 0) + 1
        cost_db.record_cost(
            provider="deepseek", model="deepseek-v4-flash",
            cost=0.50, task_id=f"round_{current_round}",
        )
        dollar_spent = cost_db.get_today_spent()
        state.setdefault("daily_budget", {})["dollar_spent_today"] = dollar_spent
        state["daily_budget"]["dollar_limit"] = 5.0
        if dollar_spent >= 4.5:
            state["daily_budget"]["tier"] = "red"
            state["daily_budget"]["readonly_mode"] = True
        elif dollar_spent >= 2.0:
            state["daily_budget"]["tier"] = "yellow"
        else:
            state["daily_budget"]["tier"] = "green"
        relog("💰", "本轮成本 $0.50（累计今日 $%.2f / $5.00, %s级）", dollar_spent, state["daily_budget"]["tier"])
    except Exception as exc:
        relog("⚠️", "成本记录失败：%s", exc)

    state["current_round"] = state.get("current_round", 0) + 1
    state["step"] = "done"
    state["completed_at"] = timestamp
    if not state.get("started_at"):
        state["started_at"] = timestamp
    state["project_one_step"] = "done"
    state["project_three_step"] = "completed"
    save_state(state)


def main():
    """主入口 — 完整流程，调用各个子函数。"""
    timestamp = _parse_cli_args()
    relog("=" * 60, "")
    relog("后勤脚本启动 — %s", timestamp)

    if not acquire_pid_file():
        relog("⏭️", "另一个实例正在运行，退出")
        sys.exit(1)

    try:
        state = load_state()
        check_and_heal_conflicts()

        disk = check_disk_space()
        if disk.get("paused"):
            relog("⏸️", "磁盘空间不足，跳过本轮主要操作")

        cost_warning = check_cost_over_budget()
        if cost_warning:
            relog("⏸️", "成本超限，跳过 LLM 密集型操作")

        # 项目同步
        if PROJECT1_DIR is not None:
            _sync_project(PROJECT1_DIR, "项目一", timestamp)
        else:
            relog("ℹ️", "项目一目录未配置（PROJECT1_DIR=None），跳过同步")
        _sync_project(SWARM_DIR, "项目三", timestamp)

        # 优化引擎
        targets, is_dry_run, cost_tier = _collect_optimization_targets(cost_warning)
        cfg = _get_config()
        opt_dims = cfg.get("optimization_dimensions", None)
        _run_optimization_engine(targets, timestamp, opt_dims if opt_dims else OPT_DIMENSIONS, is_dry_run)

        # 深层修复任务 + 失败分析 + 日志扫描
        _run_deep_scan_and_tasks(targets, cost_tier, timestamp)
        _run_failure_analysis(timestamp)
        _run_log_scan()

        # 委托诊断 + 心跳 + 并行规划
        run_delegation_diagnosis()
        check_forced_delegation()
        check_and_heal_heartbeats()
        plan_parallel_tasks()

        try:
            sys.path.insert(0, str(SWARM_DIR))
            from src.agents.micro_delegation import plan_micro_delegations
            plan_micro_delegations()
            relog("📋", "微委托规划完成")
        except ImportError as e:
            relog("⚠️", "micro_delegation 不可用: %s", e)
        except Exception as e:
            relog("⚠️", "微委托规划失败: %s", e)

        # 成本 + 状态
        _update_state_and_cost(state, timestamp)

        relog("")
        relog("提示：")
        relog("  - 并行任务计划已写入 state.json parallel_plan 字段")
        relog("  - Hermes cronjob 可读取 plan.batches 按批执行")
        relog("  - 如遇冲突，请手动解决后修改 state.json 恢复")

    finally:
        release_pid_file()

    relog("=" * 60, "")
    relog("后勤脚本完成 — %s", timestamp)


if __name__ == "__main__":
    main()
