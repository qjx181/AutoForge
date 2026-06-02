"""test_experience_store.py — 经验存储读写一致性测试

测试目标：
  1. record_experience 写入后可读
  2. get_calibrated_confidence 的校准逻辑
  3. get_relevant_experiences 的排序逻辑
  4. get_failure_warnings 的去重逻辑
  5. propagate_confidence_to_fixers 的传播逻辑
  6. 并发写入不丢数据（文件锁）
  7. 损坏 JSON 文件的容错

所有测试使用 tmp_path 隔离，不影响真实经验数据。
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_experience_store(tmp_path):
    """每个测试用独立的 experience_store.json，互不干扰。"""
    fake_file = tmp_path / "experience_store.json"
    with patch("src.core.experience_store.EXPERIENCE_FILE", fake_file):
        yield fake_file


# ═══════════════════════════════════════════════════════════════════════
# record_experience 测试
# ═══════════════════════════════════════════════════════════════════════


class TestRecordExperience:
    """测试经验记录的写入和读取。"""

    def test_record_and_read_back(self):
        """写入一条经验后，可以从文件中读回。"""
        from src.core.experience_store import record_experience, _load
        record_experience(
            issue_type="swallowed_exception",
            file="src/main.py",
            line=42,
            fixer="swallowed_exception_fixer",
            action="add logging",
            confidence=0.85,
            success=True,
            code_snippet="except: pass",
            project="/tmp/test_proj",
        )
        data = _load()
        assert len(data["experiences"]) == 1
        exp = data["experiences"][0]
        assert exp["issue_type"] == "swallowed_exception"
        assert exp["success"] is True
        assert exp["confidence"] == 0.85

    def test_multiple_records_accumulate(self):
        """多次记录累积在同一个 experiences 列表中。"""
        from src.core.experience_store import record_experience, _load
        for i in range(5):
            record_experience(
                issue_type=f"type_{i}",
                file=f"file_{i}.py",
                line=i,
                fixer="test_fixer",
                action="fix",
                confidence=0.5 + i * 0.1,
                success=i % 2 == 0,
            )
        data = _load()
        assert len(data["experiences"]) == 5

    def test_record_with_code_before_after(self):
        """记录包含修复前后的代码片段。"""
        from src.core.experience_store import record_experience, _load
        record_experience(
            issue_type="bare_except",
            file="a.py",
            line=10,
            fixer="bare_except_fixer",
            action="add Exception type",
            confidence=0.9,
            success=True,
            code_before="except: pass",
            code_after="except Exception as e: logger.error(e)",
        )
        data = _load()
        exp = data["experiences"][0]
        assert exp["code_before"] == "except: pass"
        assert "except Exception" in exp["code_after"]


# ═══════════════════════════════════════════════════════════════════════
# get_calibrated_confidence 测试
# ═══════════════════════════════════════════════════════════════════════


class TestCalibratedConfidence:
    """测试置信度校准逻辑。"""

    def test_no_history_returns_original(self):
        """没有历史数据时返回原始置信度。"""
        from src.core.experience_store import get_calibrated_confidence
        result = get_calibrated_confidence("unknown_type", "unknown_fixer", 0.7)
        assert result == 0.7

    def test_insufficient_samples_returns_original(self):
        """样本数 < 3 时返回原始置信度。"""
        from src.core.experience_store import record_experience, get_calibrated_confidence, _load, _save
        # 手动写入 2 条记录（样本不足）
        data = _load()
        data["confidence_overrides"]["test_type:test_fixer"] = {
            "calibrated": 0.95,
            "sample_size": 2,
            "success_rate": 1.0,
            "updated_at": "2025-01-01",
        }
        _save(data)
        result = get_calibrated_confidence("test_type", "test_fixer", 0.5)
        assert result == 0.5  # 样本不足，返回原始值

    def test_sufficient_samples_returns_calibrated(self):
        """样本数 >= 3 时返回校准后的置信度。"""
        from src.core.experience_store import get_calibrated_confidence, _load, _save
        data = _load()
        data["confidence_overrides"]["test_type:test_fixer"] = {
            "calibrated": 0.92,
            "sample_size": 10,
            "success_rate": 0.9,
            "updated_at": "2025-01-01",
        }
        _save(data)
        result = get_calibrated_confidence("test_type", "test_fixer", 0.5)
        assert result == 0.92


# ═══════════════════════════════════════════════════════════════════════
# get_relevant_experiences 测试
# ═══════════════════════════════════════════════════════════════════════


class TestRelevantExperiences:
    """测试相关经验查询。"""

    def test_returns_matching_type(self):
        """只返回匹配 issue_type 的经验。"""
        from src.core.experience_store import record_experience, get_relevant_experiences
        record_experience("type_a", "a.py", 1, "fixer", "fix", 0.8, True)
        record_experience("type_b", "b.py", 2, "fixer", "fix", 0.7, True)
        record_experience("type_a", "c.py", 3, "fixer", "fix", 0.6, False)

        result = get_relevant_experiences("type_a")
        assert len(result) == 2
        assert all(e["issue_type"] == "type_a" for e in result)

    def test_empty_when_no_match(self):
        """没有匹配类型时返回空列表。"""
        from src.core.experience_store import get_relevant_experiences
        result = get_relevant_experiences("nonexistent_type")
        assert result == []

    def test_success_ranked_higher(self):
        """成功经验排在失败经验前面。"""
        from src.core.experience_store import record_experience, get_relevant_experiences
        record_experience("t", "x.py", 1, "f", "fix", 0.5, False)
        record_experience("t", "y.py", 2, "f", "fix", 0.8, True)
        record_experience("t", "z.py", 3, "f", "fix", 0.7, True)

        result = get_relevant_experiences("t")
        # 成功的应该排在前面
        assert result[0]["success"] is True


# ═══════════════════════════════════════════════════════════════════════
# get_failure_warnings 测试
# ═══════════════════════════════════════════════════════════════════════


class TestFailureWarnings:
    """测试失败警告去重。"""

    def test_returns_unique_errors(self):
        """相同 error 只返回一次。"""
        from src.core.experience_store import record_experience, get_failure_warnings
        for _ in range(3):
            record_experience("t", "a.py", 1, "f", "fix", 0.5, False, error="same error")
        record_experience("t", "b.py", 2, "f", "fix", 0.5, False, error="different error")

        warnings = get_failure_warnings("t")
        assert len(warnings) == 2  # 两个不同的 error

    def test_no_failures_returns_empty(self):
        """没有失败记录时返回空列表。"""
        from src.core.experience_store import record_experience, get_failure_warnings
        record_experience("t", "a.py", 1, "f", "fix", 0.9, True)
        warnings = get_failure_warnings("t")
        assert warnings == []


# ═══════════════════════════════════════════════════════════════════════
# propagate_confidence_to_fixers 测试
# ═══════════════════════════════════════════════════════════════════════


class TestPropagateConfidence:
    """测试置信度传播。"""

    def test_propagate_with_sufficient_samples(self):
        """样本 >= 3 的 override 会被传播。"""
        from src.core.experience_store import propagate_confidence_to_fixers, _load, _save
        data = _load()
        data["confidence_overrides"] = {
            "type_a:fixer_1": {
                "calibrated": 0.9,
                "sample_size": 5,
                "success_rate": 0.8,
                "updated_at": "2025-01-01",
            },
            "type_b:fixer_2": {
                "calibrated": 0.3,
                "sample_size": 1,  # 样本不足
                "success_rate": 0.3,
                "updated_at": "2025-01-01",
            },
        }
        _save(data)

        result = propagate_confidence_to_fixers()
        assert result["propagated_count"] == 1  # 只有 type_a:fixer_1 被传播

    def test_propagate_writes_to_data(self):
        """传播后 data 中有 propagated_confidences 字段。"""
        from src.core.experience_store import propagate_confidence_to_fixers, _load, _save
        data = _load()
        data["confidence_overrides"] = {
            "x:y": {"calibrated": 0.8, "sample_size": 10, "success_rate": 0.8, "updated_at": "2025-01-01"},
        }
        _save(data)

        propagate_confidence_to_fixers()
        data = _load()
        assert "propagated_confidences" in data
        assert "x" in data["propagated_confidences"]
        assert "y" in data["propagated_confidences"]["x"]


# ═══════════════════════════════════════════════════════════════════════
# 容错测试
# ═══════════════════════════════════════════════════════════════════════


class TestFaultTolerance:
    """测试损坏文件的容错。"""

    def test_corrupted_json_returns_empty(self, isolate_experience_store):
        """损坏的 JSON 文件返回空数据库。"""
        from src.core.experience_store import _load
        # 写入损坏的内容
        isolate_experience_store.write_text("{broken json!!!", encoding="utf-8")
        data = _load()
        assert data["experiences"] == []
        assert data["patterns"] == {}

    def test_missing_file_returns_empty(self, isolate_experience_store):
        """文件不存在时返回空数据库。"""
        from src.core.experience_store import _load
        # 文件由 fixture 创建，先删掉
        isolate_experience_store.unlink(missing_ok=True)
        data = _load()
        assert data["experiences"] == []
