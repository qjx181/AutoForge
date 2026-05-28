"""fix_pipeline.py — 集成管道：扫描 → 修复 → 审批 → 经验积累

设计动机（面试话术）：
  "之前的 _run_deep_scan_and_tasks 只做了'扫描 → 写 JSON 文件'，
   修复和经验记录是断开的。新的 fix_pipeline 把三个模块串联成闭环：
   ScannerRegistry 扫描 → FixerRegistry 修复 → ConfidenceGate 审批
   → ExperienceStore 记录 → 下次修复时注入经验。"

管道流程：
  1. scan: 用 ScannerRegistry 扫描目标项目
  2. fix:  对每个 Issue，查找 FixerRegistry 中的修复器
  3. gate: 对每个 FixResult，用 ConfidenceGate 做三级决策
  4. apply: AUTO_APPLY 的直接写文件，PENDING_REVIEW 入队列
  5. learn: 所有结果记录到 ExperienceStore

使用方式（一行调用）：
    from src.core.fix_pipeline import run_pipeline
    result = run_pipeline(Path("/path/to/project"))
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .adapters import (
    Issue, FixResult, ScannerRegistry, FixerRegistry,
    IssueProcessor, IssueIR,
    build_default_scanner_registry, build_default_fixer_registry,
)
from .confidence_gate import process_fix, GateDecision, expire_stale
from .success_verifier import VerifierChain, build_default_verifier_chain, VerifyChainResult
from .experience_store import (
    record_experience, get_calibrated_confidence,
    get_relevant_experiences, get_failure_warnings,
)

logger = logging.getLogger(__name__)



class PipelineResult:
    """管道执行结果汇总。"""

    def __init__(self):
        self.scanned_files: int = 0
        self.issues_found: int = 0
        self.fixes_attempted: int = 0
        self.auto_applied: int = 0
        self.pending_review: int = 0
        self.rejected: int = 0
        self.errors: int = 0
        self.details: list[dict] = []
        self.started_at: str = ""
        self.finished_at: str = ""

    def to_dict(self) -> dict:
        return {
            "scanned_files": self.scanned_files,
            "issues_found": self.issues_found,
            "fixes_attempted": self.fixes_attempted,
            "auto_applied": self.auto_applied,
            "pending_review": self.pending_review,
            "rejected": self.rejected,
            "errors": self.errors,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "details": self.details,
        }

    def summary(self) -> str:
        """一行摘要，用于日志输出。"""
        return (
            f"扫描 {self.issues_found} 个问题 → "
            f"尝试修复 {self.fixes_attempted} → "
            f"自动应用 {self.auto_applied}, "
            f"待审批 {self.pending_review}, "
            f"拒绝 {self.rejected}, "
            f"失败 {self.errors}"
        )



class PipelineRunState:
    """管道运行状态机（借鉴 HiveWard Run State Machine）。

    状态流转：
      queued → running → succeeded / failed / cancelled

    用途：
    - 追踪管道执行状态（queued/running/succeeded/failed/cancelled）
    - 支持取消操作（通过 cancelled 标志）
    - 记录每个阶段的耗时
    - 提供状态查询接口

    与 PipelineResult 的区别：
    - PipelineResult 是执行结果统计（修了多少、成功多少）
    - PipelineRunState 是执行过程状态（当前在哪个阶段、能不能取消）
    """

    VALID_STATES = {"queued", "running", "succeeded", "failed", "cancelled"}

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.state = "queued"
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.error_message: Optional[str] = None
        self.phase_timings: dict[str, float] = {}  # phase_name → duration_seconds
        self._cancel_requested = False

    def start(self):
        """状态转移：queued → running"""
        if self.state != "queued":
            raise ValueError(f"Cannot start from state {self.state}")
        self.state = "running"
        self.started_at = datetime.now()

    def succeed(self):
        """状态转移：running → succeeded"""
        if self.state != "running":
            raise ValueError(f"Cannot succeed from state {self.state}")
        self.state = "succeeded"
        self.finished_at = datetime.now()

    def fail(self, error: str):
        """状态转移：running → failed"""
        if self.state != "running":
            raise ValueError(f"Cannot fail from state {self.state}")
        self.state = "failed"
        self.finished_at = datetime.now()
        self.error_message = error

    def cancel(self):
        """请求取消（实际取消在下一次检查点）"""
        self._cancel_requested = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_requested

    def check_cancel(self):
        """检查点：如果请求了取消，转移到 cancelled 状态并抛异常"""
        if self._cancel_requested:
            self.state = "cancelled"
            self.finished_at = datetime.now()
            raise PipelineCancelled(f"Pipeline {self.run_id} cancelled by user")

    def record_phase(self, phase_name: str, duration_seconds: float):
        """记录阶段耗时"""
        self.phase_timings[phase_name] = duration_seconds

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error_message": self.error_message,
            "phase_timings": self.phase_timings,
            "duration_seconds": (
                (self.finished_at - self.started_at).total_seconds()
                if self.started_at and self.finished_at
                else None
            ),
        }


class PipelineCancelled(Exception):
    """管道被取消时抛出的异常"""
    pass




def _run_scan_phase(
    project_root: Path,
    scanners: ScannerRegistry,
    dimensions: Optional[list[str]],
) -> list[Issue]:
    """扫描阶段：按维度或全量扫描，返回 Issue 列表。"""
    if not dimensions:
        return scanners.scan_all(project_root)
    all_issues: list[Issue] = []
    for dim in dimensions:
        for scanner in scanners.by_dimension(dim):
            try:
                found = scanner.scan(project_root)
                for issue in found:
                    issue.scanner = scanner.name
                all_issues.extend(found)
            except Exception as e:
                logger.error("Scanner %s failed: %s", scanner.name, e)
    return all_issues


def _run_single_fix(
    ir: IssueIR,
    fixers: FixerRegistry,
    project_root: Path,
    verifier: Optional[VerifierChain],
    result: PipelineResult,
    run_state: Optional[PipelineRunState] = None,
):
    """执行单个 Issue 的修复流程（供串行和并行模式共用）。"""
    issue = ir.issue
    fixer = fixers.get_fixer(issue.type)
    if not fixer:
        result.details.append({"issue": issue.to_dict(), "decision": "no_fixer"})
        return

    target_file = project_root / issue.file
    original_content = None
    if target_file.exists():
        original_content = target_file.read_text(encoding="utf-8")

    past_experiences = get_relevant_experiences(issue.type, issue.file)
    failure_warnings = get_failure_warnings(issue.type)
    if past_experiences or failure_warnings:
        issue.context["past_experiences"] = [
            {"action": e["action"], "success": e["success"], "confidence": e["confidence"]}
            for e in past_experiences[:3]
        ]
        issue.context["failure_warnings"] = failure_warnings[:3]

    try:
        fix_result = fixer.fix(issue, project_root)
    except Exception as e:
        logger.error("Fixer %s threw exception for %s: %s", fixer.name, issue.type, e)
        result.errors += 1
        record_experience(
            issue_type=issue.type, file=issue.file, line=issue.line,
            fixer=fixer.name, action="", confidence=0, success=False,
            code_snippet="", project=str(project_root), error=str(e),
        )
        return

    verify_result: Optional[VerifyChainResult] = None
    if verifier and fix_result.success:
        verify_result = verifier.verify(
            issue_type=issue.type,
            file_path=target_file,
            fix_action=fix_result.action,
            project_root=project_root,
        )
        fix_result.confidence = max(0.0, min(1.0,
            fix_result.confidence + verify_result.total_delta
        ))
        logger.info("Verification %s: %s", issue.type, verify_result.summary())

        if verify_result.should_rollback and original_content is not None:
            target_file.write_text(original_content, encoding="utf-8")
            logger.warning("Rolled back fix for %s:%s due to verification failure", issue.file, issue.line)
            fix_result.success = False
            fix_result.error = f"验证失败已回滚: {'; '.join(r.detail for r in verify_result.results if not r.passed)}"
            result.errors += 1
            record_experience(
                issue_type=issue.type, file=issue.file, line=issue.line,
                fixer=fixer.name, action=fix_result.action,
                confidence=0, success=False,
                code_snippet="", project=str(project_root),
                error=fix_result.error,
            )
            result.details.append({
                "issue": issue.to_dict(), "fix_result": fix_result.to_dict(),
                "decision": "rolled_back", "verify": verify_result.summary(),
            })
            return

    original_conf = fix_result.confidence
    fix_result.confidence = get_calibrated_confidence(
        issue.type, fixer.name, fix_result.confidence
    )
    if fix_result.confidence != original_conf:
        logger.info(
            "Confidence calibrated: %.2f → %.2f for %s/%s",
            original_conf, fix_result.confidence, issue.type, fixer.name,
        )

    result.fixes_attempted += 1

    gate_result = process_fix(fix_result, issue, apply_fn=lambda fr: fr.success)

    decision = gate_result["decision"]
    if decision == "auto_apply":
        result.auto_applied += 1
    elif decision == "pending_review":
        result.pending_review += 1
    else:
        result.rejected += 1

    record_experience(
        issue_type=issue.type, file=issue.file, line=issue.line,
        fixer=fixer.name, action=fix_result.action,
        confidence=fix_result.confidence, success=fix_result.success,
        code_snippet=issue.description[:200], project=str(project_root),
        error=fix_result.error,
    )

    detail = {
        "issue": issue.to_dict(),
        "fix_result": fix_result.to_dict(),
        "decision": decision,
        "item_id": gate_result.get("item_id"),
        "confidence_calibrated": fix_result.confidence != original_conf,
        "ir_complexity": ir.fix_complexity,
        "ir_tags": ir.tags,
        "ir_estimated_effort": ir.estimated_effort,
        "ir_fix_strategy": ir.fix_strategy.approach,
        "ir_risk": ir.risk_assessment.regression_risk,
    }
    if verify_result:
        detail["verify"] = verify_result.summary()
        detail["verify_passed"] = verify_result.passed
    result.details.append(detail)


def _run_parallel_fixes(
    ir_list: list[IssueIR],
    fixers: FixerRegistry,
    project_root: Path,
    verifier: Optional[VerifierChain],
    result: PipelineResult,
    run_state: Optional[PipelineRunState] = None,
    max_concurrency: int = 4,
    timeout_per_fix: int = 120,
):
    """并行执行多个修复（借鉴 HiveWard maxConcurrency 设计）。

    使用 ThreadPoolExecutor 并行执行，每个修复有独立的超时控制。
    同一文件的修复会自动串行化（避免文件写入冲突）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
    import threading

    by_file: dict[str, list[IssueIR]] = {}
    for ir in ir_list:
        by_file.setdefault(ir.issue.file, []).append(ir)

    file_locks: dict[str, threading.Lock] = {}
    for file_path in by_file:
        file_locks[file_path] = threading.Lock()

    def fix_with_lock(ir: IssueIR):
        """带文件锁的修复执行"""
        lock = file_locks[ir.issue.file]
        with lock:
            if run_state:
                run_state.check_cancel()
            _run_single_fix(ir, fixers, project_root, verifier, result, run_state)

    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = {}
        for ir in ir_list:
            future = executor.submit(fix_with_lock, ir)
            futures[future] = ir

        for future in as_completed(futures, timeout=timeout_per_fix * len(ir_list)):
            ir = futures[future]
            try:
                future.result(timeout=timeout_per_fix)
            except TimeoutError:
                logger.warning("Fix timed out for %s:%s", ir.issue.file, ir.issue.line)
                result.errors += 1
                result.details.append({
                    "issue": ir.issue.to_dict(),
                    "decision": "timeout",
                    "error": f"Fix timed out after {timeout_per_fix}s",
                })
            except PipelineCancelled:
                logger.info("Parallel fix cancelled for %s:%s", ir.issue.file, ir.issue.line)
                raise
            except Exception as e:
                logger.error("Parallel fix failed for %s:%s: %s", ir.issue.file, ir.issue.line, e)
                result.errors += 1
                result.details.append({
                    "issue": ir.issue.to_dict(),
                    "decision": "error",
                    "error": str(e),
                })


def _run_fix_phase(
    ir_list: list[IssueIR],
    fixers: FixerRegistry,
    project_root: Path,
    max_fixes: int,
    verifier: Optional[VerifierChain] = None,
    run_state: Optional[PipelineRunState] = None,
) -> PipelineResult:
    """修复阶段：逐个 Issue 匹配修复器 → 修复 → 验证 → 门控 → 经验记录。

    SWE-agent 模式：修复后自动验证（语法 + 重扫），
    验证失败则回滚，不信任修复器自评。

    返回部分填充的 PipelineResult（不含扫描阶段的统计）。
    """
    result = PipelineResult()

    parallel_irs = []
    sequential_irs = []
    for ir in ir_list[:max_fixes]:
        if ir.fix_strategy.can_parallelize and ir.risk_assessment.regression_risk != "high":
            parallel_irs.append(ir)
        else:
            sequential_irs.append(ir)

    logger.info("Fix phase: %d parallel, %d sequential", len(parallel_irs), len(sequential_irs))

    if parallel_irs:
        _run_parallel_fixes(parallel_irs, fixers, project_root, verifier, result, run_state, max_concurrency=4)

    for ir in sequential_irs:
        if run_state:
            run_state.check_cancel()
        _run_single_fix(ir, fixers, project_root, verifier, result, run_state)

    return result


def run_pipeline(
    project_root: Path,
    scanner_registry: Optional[ScannerRegistry] = None,
    fixer_registry: Optional[FixerRegistry] = None,
    dimensions: Optional[list[str]] = None,
    max_fixes: int = 50,
    dry_run: bool = False,
    incremental: bool = False,
    run_state: Optional[PipelineRunState] = None,
) -> PipelineResult:
    """运行完整的扫描-修复-审批-学习管道。

    Args:
        project_root: 目标项目根目录
        scanner_registry: 扫描器注册表（None 则用默认的 9 维 + 企业级）
        fixer_registry: 修复器注册表（None 则用默认的 6 种修复器）
        dimensions: 只运行指定维度（None 则运行全部）
        max_fixes: 最多尝试修复多少个问题（防止单轮消耗过多）
        dry_run: 只扫描不修复（用于评估阶段）
        incremental: 增量模式（只扫描变更文件，大幅提速）
        run_state: 管道运行状态机（None 则自动创建）

    Returns:
        PipelineResult 包含完整的执行统计

    设计决策（面试话术）：
      "为什么 max_fixes 默认 50？因为每个修复都可能引入新问题，
       如果一轮修太多，出问题后很难定位是哪个修复导致的。
       50 是经验值：足够覆盖大部分场景，又不至于失控。
       dry_run 模式让系统可以先'看看有什么问题'再决定修不修。
       incremental 模式用 git diff 或 mtime 检测变更，只扫描变更文件，
       把大项目的扫描时间从分钟级降到秒级。"
    """
    if run_state is None:
        import uuid
        run_state = PipelineRunState(run_id=str(uuid.uuid4())[:8])
    run_state.start()

    result = PipelineResult()
    result.started_at = datetime.now().isoformat()

    try:
        scan_start = datetime.now()
        scanners = scanner_registry or build_default_scanner_registry()
        logger.info("Pipeline: scanning %s (incremental=%s)", project_root, incremental)

        if incremental:
            from .incremental_scanner import IncrementalScanner
            inc = IncrementalScanner(project_root)
            all_issues = inc.scan_changed(scanners)
        else:
            all_issues = _run_scan_phase(project_root, scanners, dimensions)
        result.issues_found = len(all_issues)
        run_state.record_phase("scan", (datetime.now() - scan_start).total_seconds())
        logger.info("Pipeline: %d issues found", result.issues_found)

        run_state.check_cancel()

        if dry_run or not all_issues:
            result.finished_at = datetime.now().isoformat()
            run_state.succeed()
            return result

        ir_start = datetime.now()
        processor = IssueProcessor()
        ir_list = processor.process(all_issues)
        result.issues_found = len(ir_list)
        run_state.record_phase("ir_process", (datetime.now() - ir_start).total_seconds())
        logger.info("Pipeline: %d issues after IR processing (dedup+validate)", len(ir_list))

        run_state.check_cancel()

        expired_count = expire_stale()
        if expired_count:
            logger.info("Pipeline: expired %d stale inbox items", expired_count)

        fix_start = datetime.now()
        fixers = fixer_registry or build_default_fixer_registry()
        verifier = build_default_verifier_chain(scanners)
        fix_result = _run_fix_phase(ir_list, fixers, project_root, max_fixes, verifier, run_state)
        result.fixes_attempted = fix_result.fixes_attempted
        result.auto_applied = fix_result.auto_applied
        result.pending_review = fix_result.pending_review
        result.rejected = fix_result.rejected
        result.errors = fix_result.errors
        result.details = fix_result.details
        run_state.record_phase("fix", (datetime.now() - fix_start).total_seconds())

        result.finished_at = datetime.now().isoformat()
        run_state.succeed()
        logger.info("Pipeline: %s", result.summary())
        return result

    except PipelineCancelled:
        result.finished_at = datetime.now().isoformat()
        logger.info("Pipeline: cancelled by user")
        return result
    except Exception as e:
        run_state.fail(str(e))
        result.finished_at = datetime.now().isoformat()
        logger.error("Pipeline failed: %s", e)
        raise



def run_scan_only(project_root: Path, dimensions: Optional[list[str]] = None) -> list[dict]:
    """只扫描不修复，返回 Issue 列表（dict 格式）。

    用于 self_evolve_round.py 的 dry_run 模式。
    """
    scanners = build_default_scanner_registry()
    if dimensions:
        issues = []
        for dim in dimensions:
            for scanner in scanners.by_dimension(dim):
                try:
                    found = scanner.scan(project_root)
                    for issue in found:
                        issue.scanner = scanner.name
                    issues.extend(found)
                except Exception:
                    logging.exception("Scanner %s failed", scanner.name)
    else:
        issues = scanners.scan_all(project_root)
    return [issue.to_dict() for issue in issues]


def run_fix_for_issue(issue_dict: dict, project_root: Path) -> dict:
    """修复单个问题（供外部调用，如子 Agent 或 cronjob）。

    Args:
        issue_dict: Issue 的 dict 格式（来自 deep_fix_tasks.json）
        project_root: 项目根目录

    Returns:
        gate_result: {"decision": str, "item_id": str|None, "applied": bool}
    """
    issue = Issue.from_dict(issue_dict)
    fixers = build_default_fixer_registry()

    fixer = fixers.get_fixer(issue.type)
    if not fixer:
        return {"decision": "no_fixer", "item_id": None, "applied": False}

    fix_result = fixer.fix(issue, project_root)
    fix_result.confidence = get_calibrated_confidence(
        issue.type, fixer.name, fix_result.confidence
    )

    gate_result = process_fix(fix_result, issue)

    record_experience(
        issue_type=issue.type, file=issue.file, line=issue.line,
        fixer=fixer.name, action=fix_result.action,
        confidence=fix_result.confidence, success=fix_result.success,
        project=str(project_root), error=fix_result.error,
    )

    return gate_result


def save_pipeline_report(result: PipelineResult, output_path: Optional[Path] = None) -> Path:
    """保存管道执行报告到 JSON 文件。"""
    if output_path is None:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(__file__).parent.parent.parent / "data" / f"pipeline_report_{ts}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path
