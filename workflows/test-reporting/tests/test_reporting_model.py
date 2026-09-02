"""Model, classification, digest and single-renderer tests for test-reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_workflow_hub.test_reporting import (
    ExecutionSummary,
    JenkinsAttempt,
    JenkinsClassification,
    JunitEvidence,
    ReportContext,
    ReportingError,
    TestReportModel,
    classify_jenkins_attempt,
    render_test_report,
    semantic_report_digest,
)


def _sha40(character: str) -> str:
    return character * 40


def _sha256(character: str) -> str:
    return character * 64


def _context(*, environment: bool = True) -> ReportContext:
    return ReportContext(
        candidate_commit=_sha40("a"),
        candidate_tree=_sha40("b"),
        jenkins_evidence_sha256=_sha256("c"),
        environment_evidence_sha256=(
            _sha256("d") if environment else "未提供"
        ),
    )


def _model(**overrides: object) -> TestReportModel:
    base: dict[str, object] = {
        "run_id": "run-001",
        "generated_at": "2026-08-21T14:56:35Z",
        "objective": "验证 API 超时行为",
        "scope": "tests/test_api.py",
        "out_of_scope": "性能压测",
        "environment": "test",
        "versions": "python 3.12 / pytest 8",
        "materials": (("需求", "requirements.md", "provided"),),
        "execution": ExecutionSummary(
            collected=12,
            passed=11,
            failed=0,
            skipped=1,
            error=0,
            classification="TESTS_PASSED",
            confidence="high",
            conclusion="测试通过（可信 Jenkins/JUnit 证据）",
        ),
        "failures": (),
        "defects": (),
        "missing_evidence": (),
        "blockers": (),
        "risks": (),
        "lifecycle": _context(),
        "jenkins_attempts": (),
    }
    base.update(overrides)
    return TestReportModel(**base)  # type: ignore[arg-type]


def test_classification_keeps_build_and_pytest_evidence_separate() -> None:
    passed = classify_jenkins_attempt(
        build_result="SUCCESS",
        junit=JunitEvidence(total_count=12, fail_count=0, skip_count=1),
        console_hint=False,
    )
    failed = classify_jenkins_attempt(
        build_result="FAILURE",
        junit=JunitEvidence(total_count=12, fail_count=1, skip_count=0),
        console_hint=True,
    )
    zero_tests = classify_jenkins_attempt(
        build_result="SUCCESS",
        junit=JunitEvidence(total_count=0, fail_count=0, skip_count=0),
        console_hint=False,
    )
    all_skipped = classify_jenkins_attempt(
        build_result="SUCCESS",
        junit=JunitEvidence(total_count=5, fail_count=0, skip_count=5),
        console_hint=False,
    )
    absent = classify_jenkins_attempt(
        build_result=None,
        junit=None,
        console_hint=False,
    )

    assert (passed.status, passed.confidence) == ("TESTS_PASSED", "high")
    assert (failed.status, failed.confidence) == ("TESTS_FAILED", "high")
    assert zero_tests.status == "TESTS_NOT_EXECUTED"
    assert all_skipped.status == "TESTS_NOT_EXECUTED"
    assert absent.status == "NO_JENKINS_EVIDENCE"


def test_report_context_validates_provenance_sha_shapes() -> None:
    assert _context().candidate_commit == _sha40("a")
    assert _context(environment=False).environment_evidence_sha256 == "未提供"

    with pytest.raises(ReportingError, match="candidate_commit"):
        ReportContext(candidate_commit="not-a-sha")
    with pytest.raises(ReportingError, match="candidate_tree"):
        ReportContext(candidate_tree="")
    with pytest.raises(ReportingError, match="jenkins_evidence_sha256"):
        ReportContext(jenkins_evidence_sha256=_sha40("a"))
    with pytest.raises(ReportingError, match="environment_evidence_sha256"):
        ReportContext(environment_evidence_sha256="x" * 64)


def test_test_report_model_rejects_invalid_counts_and_tuple_elements() -> None:
    with pytest.raises(ReportingError, match="counts"):
        _model(
            execution=ExecutionSummary(
                collected=3,
                passed=4,
                failed=0,
                skipped=0,
                error=0,
                classification="TESTS_PASSED",
                confidence="high",
                conclusion="结论",
            )
        )
    with pytest.raises(ReportingError, match="counts"):
        _model(
            execution=ExecutionSummary(
                collected=3,
                passed=-1,
                failed=0,
                skipped=0,
                error=0,
                classification="TESTS_PASSED",
                confidence="high",
                conclusion="结论",
            )
        )
    with pytest.raises(ReportingError, match="failures"):
        _model(failures=("",))
    with pytest.raises(ReportingError, match="materials"):
        _model(materials=(("需求", "", "provided"),))


def test_semantic_digest_is_closed_and_deterministic() -> None:
    model = _model()

    first = semantic_report_digest(model)
    second = semantic_report_digest(model)

    assert first == second
    assert set(first) == {
        "conclusion",
        "counts",
        "classification",
        "confidence",
        "failures",
        "missing_evidence",
        "blockers",
        "bindings",
        "attempts",
    }
    assert first["counts"] == {
        "collected": 12,
        "passed": 11,
        "failed": 0,
        "skipped": 1,
        "error": 0,
    }
    assert first["bindings"]["candidate_commit"] == _sha40("a")
    assert first["bindings"]["environment_evidence_sha256"] == _sha256("d")


def test_renderer_is_the_only_public_entry_and_emits_nine_sections() -> None:
    model = _model(
        lifecycle=ReportContext(
            candidate_commit=_sha40("a"),
            candidate_tree=_sha40("b"),
            jenkins_evidence_sha256=_sha256("c"),
            environment_evidence_sha256=_sha256("d"),
        ),
        jenkins_attempts=(
            JenkinsAttempt(
                queue_id="queue-17",
                build_number=17,
                node=None,
                actual_commit=_sha40("a"),
                build_result="SUCCESS",
                classification=JenkinsClassification(
                    "TESTS_PASSED",
                    "high",
                    "Jenkins 成功且 JUnit 表明所有已执行测试通过",
                ),
                junit=JunitEvidence(12, 0, 1),
                failures=(),
                artifacts=(),
            ),
        ),
    )

    markdown = render_test_report(model)

    for heading in (
        "## 报告基本信息",
        "## 测试目标与范围",
        "## 测试环境与版本",
        "## 材料清单与来源",
        "## 执行汇总",
        "## 详细结果/失败项",
        "## 缺陷汇总",
        "## 结论",
        "## 风险、限制与缺失信息",
    ):
        assert heading in markdown
    assert markdown.count("## ") == 9
    assert "candidate_commit: `" + _sha40("a") in markdown
    assert "environment_evidence_sha256: `" + _sha256("d") in markdown
    assert "build=17 commit=" + _sha40("a") in markdown


def test_renderer_honors_language_and_rejects_unsupported_values() -> None:
    model = _model()
    english = render_test_report(model, language="en-US")

    assert "# Test Report" in english
    assert "## Report Information" in english
    assert "## 测试报告" not in english
    with pytest.raises(ReportingError, match="language"):
        render_test_report(model, language="ja-JP")
