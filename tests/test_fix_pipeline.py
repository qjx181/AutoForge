"""test_fix_pipeline.py — 修复管道核心流程测试

测试目标：
  1. PipelineResult 的统计聚合
  2. PipelineRunState 状态机的合法/非法转移
  3. run_pipeline 的 dry_run 模式（只扫描不修复）
  4. _run_scan_phase 的维度过滤
  5. fallback chain 的降级逻辑

所有外部依赖（ScannerRegistry、FixerRegistry）用 mock 替代。
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest


# ═══════════════════════════════════════════════════════════════════════
# PipelineResult 测试
# ═══════════════════════════════════════════════════════════════════════


class TestPipelineResult:
    """测试 PipelineResult 统计聚合。"""

    def test_initial_state(self):
        """初始状态所有计数器为 0。"""
        from src.core.fix_pipeline import PipelineResult
        r = PipelineResult()
        assert r.scanned_files == 0
        assert r.issues_found == 0
        assert r.fixes_attempted == 0
        assert r.auto_applied == 0
        assert r.pending_review == 0
        assert r.rejected == 0
        assert r.errors == 0
        assert r.details == []

    def test_to_dict(self):
        """to_dict 包含所有必要字段。"""
        from src.core.fix_pipeline import PipelineResult
        r = PipelineResult()
        r.issues_found = 5
        r.auto_applied = 2
        d = r.to_dict()
        assert d["issues_found"] == 5
        assert d["auto_applied"] == 2
        assert "scanned_files" in d
        assert "started_at" in d
        assert "finished_at" in d

    def test_summary_format(self):
        """summary() 输出人类可读的一行摘要。"""
        from src.core.fix_pipeline import PipelineResult
        r = PipelineResult()
        r.issues_found = 10
        r.fixes_attempted = 8
        r.auto_applied = 5
        r.pending_review = 2
        r.rejected = 1
        r.errors = 0
        s = r.summary()
        assert "10" in s
        assert "5" in s
        assert "待审批" in s or "pending" in s.lower() or "2" in s


# ═══════════════════════════════════════════════════════════════════════
# PipelineRunState 状态机测试
# ═══════════════════════════════════════════════════════════════════════


class TestPipelineRunState:
    """测试 PipelineRunState 状态转移。"""

    def test_normal_lifecycle(self):
        """正常生命周期：queued → running → succeeded。"""
        from src.core.fix_pipeline import PipelineRunState
        state = PipelineRunState(run_id="test-001")
        assert state.state == "queued"

        state.start()
        assert state.state == "running"
        assert state.started_at is not None

        state.succeed()
        assert state.state == "succeeded"
        assert state.finished_at is not None

    def test_failure_lifecycle(self):
        """失败生命周期：queued → running → failed。"""
        from src.core.fix_pipeline import PipelineRunState
        state = PipelineRunState(run_id="test-002")
        state.start()
        state.fail("some error")
        assert state.state == "failed"
        assert state.error_message == "some error"

    def test_cannot_start_from_running(self):
        """不能从 running 状态再次 start。"""
        from src.core.fix_pipeline import PipelineRunState
        state = PipelineRunState(run_id="test-003")
        state.start()
        with pytest.raises(ValueError, match="Cannot start"):
            state.start()

    def test_cannot_succeed_from_queued(self):
        """不能从 queued 状态直接 succeed。"""
        from src.core.fix_pipeline import PipelineRunState
        state = PipelineRunState(run_id="test-004")
        with pytest.raises(ValueError, match="Cannot succeed"):
            state.succeed()

    def test_cancel_request(self):
        """cancel() 设置取消标志，check_cancel 抛异常。"""
        from src.core.fix_pipeline import PipelineRunState, PipelineCancelled
        state = PipelineRunState(run_id="test-005")
        state.start()
        state.cancel()
        assert state.is_cancelled
        with pytest.raises(PipelineCancelled):
            state.check_cancel()
        assert state.state == "cancelled"

    def test_phase_timing(self):
        """record_phase 记录阶段耗时。"""
        from src.core.fix_pipeline import PipelineRunState
        state = PipelineRunState(run_id="test-006")
        state.record_phase("scan", 1.5)
        state.record_phase("fix", 3.2)
        assert state.phase_timings["scan"] == 1.5
        assert state.phase_timings["fix"] == 3.2

    def test_to_dict(self):
        """to_dict 包含完整状态信息。"""
        from src.core.fix_pipeline import PipelineRunState
        state = PipelineRunState(run_id="test-007")
        state.start()
        state.succeed()
        d = state.to_dict()
        assert d["run_id"] == "test-007"
        assert d["state"] == "succeeded"
        assert d["started_at"] is not None
        assert d["finished_at"] is not None


# ═══════════════════════════════════════════════════════════════════════
# run_pipeline 测试（mock 外部依赖）
# ═══════════════════════════════════════════════════════════════════════


class TestRunPipeline:
    """测试 run_pipeline 核心流程。"""

    def test_dry_run_returns_immediately(self, tmp_path):
        """dry_run 模式只扫描不修复。"""
        from src.core.fix_pipeline import run_pipeline

        # mock scanner_registry 返回空 Issue 列表
        mock_scanners = MagicMock()
        mock_scanners.scan_all.return_value = []

        result = run_pipeline(
            project_root=tmp_path,
            scanner_registry=mock_scanners,
            dry_run=True,
        )
        assert result.fixes_attempted == 0
        assert result.auto_applied == 0
        assert result.started_at != ""
        assert result.finished_at != ""

    def test_no_issues_skips_fix(self, tmp_path):
        """没有发现 issue 时跳过修复阶段。"""
        from src.core.fix_pipeline import run_pipeline

        mock_scanners = MagicMock()
        mock_scanners.scan_all.return_value = []

        result = run_pipeline(
            project_root=tmp_path,
            scanner_registry=mock_scanners,
        )
        assert result.issues_found == 0
        assert result.fixes_attempted == 0

    def test_run_state_lifecycle(self, tmp_path):
        """run_pipeline 正常完成后 run_state 为 succeeded。"""
        from src.core.fix_pipeline import run_pipeline, PipelineRunState

        mock_scanners = MagicMock()
        mock_scanners.scan_all.return_value = []

        state = PipelineRunState(run_id="pipe-001")
        result = run_pipeline(
            project_root=tmp_path,
            scanner_registry=mock_scanners,
            run_state=state,
        )
        assert state.state == "succeeded"

    def test_cancel_before_fix(self, tmp_path):
        """在修复前取消，应正常返回。"""
        from src.core.fix_pipeline import run_pipeline, PipelineRunState

        # 创建一个会在 check_cancel 之前返回 issue 的 mock
        mock_issue = MagicMock()
        mock_issue.to_dict.return_value = {"type": "test", "severity": "low"}
        mock_issue.type = "test"
        mock_issue.file = "test.py"
        mock_issue.line = 1
        mock_issue.description = "test issue"
        mock_issue.context = {}
        mock_issue.scanner = "mock"

        mock_scanners = MagicMock()
        mock_scanners.scan_all.return_value = [mock_issue]

        state = PipelineRunState(run_id="cancel-001")
        # 立即取消
        state.cancel()

        result = run_pipeline(
            project_root=tmp_path,
            scanner_registry=mock_scanners,
            run_state=state,
        )
        # 应该被取消，不会尝试修复
        assert result.fixes_attempted == 0
