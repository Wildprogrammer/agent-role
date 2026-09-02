"""Closed report data model and its semantic digest."""

from __future__ import annotations

from dataclasses import dataclass
import re


class ReportingError(ValueError):
    """Raised when report evidence or output identity is invalid."""


_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = frozenset(
    {
        "TESTS_PASSED",
        "TESTS_FAILED",
        "TEST_EXECUTION_INCOMPLETE",
        "TEST_RESULT_UNVERIFIED",
        "TESTS_NOT_EXECUTED",
        "NO_JENKINS_EVIDENCE",
    }
)
_CONFIDENCE = frozenset({"high", "low", "none"})
_REPORT_LANGUAGES = frozenset({"zh-CN", "en-US"})
_NOT_PROVIDED = "未提供"


@dataclass(frozen=True)
class JunitEvidence:
    total_count: int
    fail_count: int
    skip_count: int
    error_count: int = 0

    def __post_init__(self) -> None:
        values = (
            self.total_count,
            self.fail_count,
            self.skip_count,
            self.error_count,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in values
        ):
            raise ReportingError("JUnit counts must be non-negative integers")
        if self.fail_count + self.error_count + self.skip_count > self.total_count:
            raise ReportingError("JUnit counts exceed the total test count")


@dataclass(frozen=True)
class ReportContext:
    """Auditable lifecycle facts that contextualize, but never determine, a test result."""

    requirement_snapshot: str = _NOT_PROVIDED
    case_snapshot: str = _NOT_PROVIDED
    initial_master_sha: str = _NOT_PROVIDED
    temporary_branch: str = _NOT_PROVIDED
    candidate_commits: tuple[str, ...] = ()
    local_baseline_summary: str = _NOT_PROVIDED
    local_validation_summary: str = _NOT_PROVIDED
    iteration_notes: tuple[str, ...] = ()
    gate_records: tuple[str, ...] = ()
    residual_risks: tuple[str, ...] = ()
    user_stopped: bool = False
    language: str = "zh-CN"
    candidate_commit: str = _NOT_PROVIDED
    candidate_tree: str = _NOT_PROVIDED
    jenkins_evidence_sha256: str = _NOT_PROVIDED
    environment_evidence_sha256: str = _NOT_PROVIDED

    def __post_init__(self) -> None:
        for field_name in (
            "requirement_snapshot",
            "case_snapshot",
            "initial_master_sha",
            "temporary_branch",
            "local_baseline_summary",
            "local_validation_summary",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ReportingError(f"report context {field_name} is invalid")
        if (
            self.initial_master_sha != _NOT_PROVIDED
            and _COMMIT_SHA.fullmatch(self.initial_master_sha) is None
        ):
            raise ReportingError("report context initial master SHA is invalid")
        if not all(
            isinstance(value, str) and _COMMIT_SHA.fullmatch(value)
            for value in self.candidate_commits
        ):
            raise ReportingError("report context candidate commits are invalid")
        for field_name in ("iteration_notes", "gate_records", "residual_risks"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ReportingError(f"report context {field_name} is invalid")
        if not isinstance(self.user_stopped, bool):
            raise ReportingError("report context user_stopped must be boolean")
        if self.language not in _REPORT_LANGUAGES:
            raise ReportingError("report context language is unsupported")
        for field_name in ("candidate_commit", "candidate_tree"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or (value != _NOT_PROVIDED and _COMMIT_SHA.fullmatch(value) is None)
            ):
                raise ReportingError(f"report context {field_name} is invalid")
        for field_name in (
            "jenkins_evidence_sha256",
            "environment_evidence_sha256",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or (value != _NOT_PROVIDED and _SHA256.fullmatch(value) is None)
            ):
                raise ReportingError(f"report context {field_name} is invalid")


@dataclass(frozen=True)
class JenkinsClassification:
    status: str
    confidence: str
    reason: str

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ReportingError("unsupported Jenkins test classification")
        if self.confidence not in _CONFIDENCE:
            raise ReportingError("unsupported evidence confidence")
        if not isinstance(self.reason, str) or not self.reason:
            raise ReportingError("classification reason is required")


@dataclass(frozen=True)
class JenkinsAttempt:
    queue_id: str
    build_number: int
    node: str | None
    actual_commit: str
    build_result: str | None
    classification: JenkinsClassification
    junit: JunitEvidence | None = None
    failures: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.queue_id, str) or not self.queue_id:
            raise ReportingError("Jenkins queue ID is required")
        if (
            isinstance(self.build_number, bool)
            or not isinstance(self.build_number, int)
            or self.build_number < 1
        ):
            raise ReportingError("Jenkins build number must be positive")
        if self.node is not None and (
            not isinstance(self.node, str) or not self.node
        ):
            raise ReportingError("Jenkins node is invalid")
        if (
            not isinstance(self.actual_commit, str)
            or _COMMIT_SHA.fullmatch(self.actual_commit) is None
        ):
            raise ReportingError("Jenkins actual commit must be a lowercase full SHA")
        if self.build_result is not None and (
            not isinstance(self.build_result, str) or not self.build_result
        ):
            raise ReportingError("Jenkins build result is invalid")
        if not isinstance(self.classification, JenkinsClassification):
            raise ReportingError("Jenkins classification is required")
        if self.junit is not None and not isinstance(self.junit, JunitEvidence):
            raise ReportingError("JUnit evidence is invalid")
        if not all(isinstance(value, str) and value for value in self.failures):
            raise ReportingError("failure identifiers are invalid")
        if not all(isinstance(value, str) and value for value in self.artifacts):
            raise ReportingError("artifact identifiers are invalid")


@dataclass(frozen=True)
class ExecutionSummary:
    collected: int
    passed: int
    failed: int
    skipped: int
    error: int
    classification: str | None
    confidence: str | None
    conclusion: str


@dataclass(frozen=True)
class TestReportModel:
    __test__ = False

    run_id: str | None
    generated_at: str
    objective: str
    scope: str
    out_of_scope: str
    environment: str
    versions: str
    materials: tuple[tuple[str, str, str], ...]
    execution: ExecutionSummary
    failures: tuple[str, ...]
    defects: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    blockers: tuple[str, ...]
    risks: tuple[str, ...]
    lifecycle: ReportContext | None = None
    jenkins_attempts: tuple[JenkinsAttempt, ...] = ()

    def __post_init__(self) -> None:
        if self.run_id is not None and (
            not isinstance(self.run_id, str) or not self.run_id
        ):
            raise ReportingError("report run_id is invalid")
        for field_name in (
            "generated_at",
            "objective",
            "scope",
            "out_of_scope",
            "environment",
            "versions",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ReportingError(f"report {field_name} is invalid")
        if not isinstance(self.materials, tuple) or not all(
            isinstance(item, tuple)
            and len(item) == 3
            and all(isinstance(part, str) and part for part in item)
            for item in self.materials
        ):
            raise ReportingError("report materials are invalid")
        for field_name in (
            "failures",
            "defects",
            "missing_evidence",
            "blockers",
            "risks",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ReportingError(f"report {field_name} are invalid")
        if not isinstance(self.execution, ExecutionSummary):
            raise ReportingError("report execution summary is invalid")
        for field_name in ("collected", "passed", "failed", "skipped", "error"):
            value = getattr(self.execution, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReportingError(
                    f"execution {field_name} counts must be non-negative"
                )
        if (
            self.execution.passed
            + self.execution.failed
            + self.execution.skipped
            > self.execution.collected
        ):
            raise ReportingError(
                "execution counts exceed the collected test count"
            )
        if not isinstance(self.execution.conclusion, str) or not self.execution.conclusion:
            raise ReportingError("execution conclusion is required")
        for field_name in ("classification", "confidence"):
            value = getattr(self.execution, field_name)
            if value is not None and (
                not isinstance(value, str) or not value
            ):
                raise ReportingError(f"execution {field_name} is invalid")
        if self.lifecycle is not None and not isinstance(
            self.lifecycle, ReportContext
        ):
            raise ReportingError("report lifecycle context is invalid")
        if not isinstance(self.jenkins_attempts, tuple) or not all(
            isinstance(item, JenkinsAttempt) for item in self.jenkins_attempts
        ):
            raise ReportingError("report Jenkins attempts are invalid")


def semantic_report_digest(report: TestReportModel) -> dict[str, object]:
    lifecycle = report.lifecycle
    return {
        "conclusion": report.execution.conclusion,
        "counts": {
            "collected": report.execution.collected,
            "passed": report.execution.passed,
            "failed": report.execution.failed,
            "skipped": report.execution.skipped,
            "error": report.execution.error,
        },
        "classification": report.execution.classification,
        "confidence": report.execution.confidence,
        "failures": list(report.failures),
        "missing_evidence": list(report.missing_evidence),
        "blockers": list(report.blockers),
        "bindings": {
            "candidate_commit": (
                lifecycle.candidate_commit if lifecycle is not None else "未提供"
            ),
            "candidate_tree": (
                lifecycle.candidate_tree if lifecycle is not None else "未提供"
            ),
            "jenkins_evidence_sha256": (
                lifecycle.jenkins_evidence_sha256
                if lifecycle is not None
                else "未提供"
            ),
            "environment_evidence_sha256": (
                lifecycle.environment_evidence_sha256
                if lifecycle is not None
                else "未提供"
            ),
        },
        "attempts": [
            {
                "queue_id": attempt.queue_id,
                "build_number": attempt.build_number,
                "actual_commit": attempt.actual_commit,
                "classification": attempt.classification.status,
                "confidence": attempt.classification.confidence,
                "failures": list(attempt.failures),
            }
            for attempt in report.jenkins_attempts
        ],
    }
