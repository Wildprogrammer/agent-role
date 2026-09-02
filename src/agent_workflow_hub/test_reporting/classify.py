"""Evidence-first Jenkins/JUnit test classification."""

from __future__ import annotations

from .model import (
    JenkinsClassification,
    JunitEvidence,
    ReportingError,
)


def classify_jenkins_attempt(
    *,
    build_result: str | None,
    junit: JunitEvidence | None,
    console_hint: bool,
) -> JenkinsClassification:
    """Classify test evidence without promoting a Jenkins build result to a pass."""

    if not isinstance(console_hint, bool):
        raise ReportingError("console hint must be boolean")
    if build_result is None:
        return JenkinsClassification(
            "NO_JENKINS_EVIDENCE",
            "none",
            "没有可读取的 Jenkins 构建结果或 JUnit 证据",
        )
    if not isinstance(build_result, str) or not build_result:
        raise ReportingError("Jenkins build result is invalid")
    if junit is None:
        if build_result == "SUCCESS":
            if console_hint:
                return JenkinsClassification(
                    "TEST_RESULT_UNVERIFIED",
                    "low",
                    "构建成功但只有控制台测试迹象，缺少 JUnit 结果",
                )
            return JenkinsClassification(
                "TESTS_NOT_EXECUTED",
                "low",
                "构建成功但没有 pytest/JUnit 执行证据",
            )
        if console_hint:
            return JenkinsClassification(
                "TEST_EXECUTION_INCOMPLETE",
                "low",
                "构建未成功且只有不完整的控制台测试迹象",
            )
        return JenkinsClassification(
            "NO_JENKINS_EVIDENCE",
            "none",
            "构建未成功且没有可验证的 JUnit 结果",
        )
    if junit.total_count == 0 or junit.skip_count == junit.total_count:
        return JenkinsClassification(
            "TESTS_NOT_EXECUTED",
            "high",
            "JUnit 已发布但没有实际执行的测试，默认不视为通过",
        )
    if junit.fail_count or junit.error_count:
        return JenkinsClassification(
            "TESTS_FAILED",
            "high",
            "JUnit 表明存在失败或错误测试",
        )
    if build_result == "SUCCESS":
        return JenkinsClassification(
            "TESTS_PASSED",
            "high",
            "Jenkins 成功且 JUnit 表明所有已执行测试通过",
        )
    return JenkinsClassification(
        "TEST_EXECUTION_INCOMPLETE",
        "high",
        "JUnit 已发布但 Jenkins 构建未成功，不能证明完整测试链路通过",
    )
