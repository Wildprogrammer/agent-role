"""Unified standalone test-reporting domain.

The package is the single authority for test-report data, Jenkins/JUnit
classification, Markdown rendering and UTF-8 byte hashes. Optional
``ReportContext`` provenance never determines the test conclusion.
"""

from .classify import classify_jenkins_attempt
from .files import report_sha256, write_test_report, write_test_report_file
from .model import (
    ExecutionSummary,
    JenkinsAttempt,
    JenkinsClassification,
    JunitEvidence,
    ReportContext,
    ReportingError,
    TestReportModel,
    semantic_report_digest,
)
from .render import render_test_report

__all__ = (
    "ExecutionSummary",
    "JenkinsAttempt",
    "JenkinsClassification",
    "JunitEvidence",
    "ReportContext",
    "ReportingError",
    "TestReportModel",
    "classify_jenkins_attempt",
    "render_test_report",
    "report_sha256",
    "semantic_report_digest",
    "write_test_report",
    "write_test_report_file",
)
