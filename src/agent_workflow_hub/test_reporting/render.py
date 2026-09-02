"""The single Markdown renderer for the unified test-report model."""

from __future__ import annotations

from .model import ReportingError, TestReportModel


def render_test_report(
    report: TestReportModel,
    *,
    language: str = "zh-CN",
) -> str:
    if language not in {"zh-CN", "en-US"}:
        raise ReportingError("unsupported report language")
    zh = language == "zh-CN"
    headings = (
        ("报告基本信息", "Report Information"),
        ("测试目标与范围", "Objective and Scope"),
        ("测试环境与版本", "Environment and Versions"),
        ("材料清单与来源", "Materials and Sources"),
        ("执行汇总", "Execution Summary"),
        ("详细结果/失败项", "Detailed Results and Failures"),
        ("缺陷汇总", "Defect Summary"),
        ("结论", "Conclusion"),
        ("风险、限制与缺失信息", "Risks, Limits, and Missing Information"),
    )
    title = "# 测试报告" if zh else "# Test Report"
    lines = [title, "", f"## {headings[0][0 if zh else 1]}", ""]
    lines.extend(
        (
            f"- run_id: `{report.run_id or 'not-provided'}`",
            f"- generated_at: `{report.generated_at}`",
        )
    )
    if report.lifecycle is not None:
        lines.extend(
            (
                f"- candidate_commit: `{report.lifecycle.candidate_commit}`",
                f"- candidate_tree: `{report.lifecycle.candidate_tree}`",
                f"- jenkins_evidence_sha256: `{report.lifecycle.jenkins_evidence_sha256}`",
                f"- environment_evidence_sha256: `{report.lifecycle.environment_evidence_sha256}`",
            )
        )
    lines.extend(("", f"## {headings[1][0 if zh else 1]}", ""))
    lines.extend(
        (
            f"- objective: {report.objective}",
            f"- scope: {report.scope}",
            f"- out_of_scope: {report.out_of_scope}",
        )
    )
    lines.extend(("", f"## {headings[2][0 if zh else 1]}", ""))
    lines.extend(
        (f"- environment: {report.environment}", f"- versions: {report.versions}")
    )
    lines.extend(
        (
            "",
            f"## {headings[3][0 if zh else 1]}",
            "",
            "| material | source | status |",
            "| --- | --- | --- |",
        )
    )
    lines.extend(
        f"| {kind} | {source} | {status} |"
        for kind, source, status in report.materials
    )
    lines.extend(("", f"## {headings[4][0 if zh else 1]}", ""))
    lines.extend(
        (
            f"- collected: {report.execution.collected}",
            f"- passed: {report.execution.passed}",
            f"- failed: {report.execution.failed}",
            f"- skipped: {report.execution.skipped}",
            f"- error: {report.execution.error}",
            f"- classification: {report.execution.classification or 'not-provided'}",
            f"- confidence: {report.execution.confidence or 'none'}",
        )
    )
    lines.extend(("", f"## {headings[5][0 if zh else 1]}", ""))
    lines.extend(f"- {failure}" for failure in report.failures)
    for attempt in report.jenkins_attempts:
        lines.append(
            f"- build={attempt.build_number} commit={attempt.actual_commit} "
            f"status={attempt.classification.status} confidence={attempt.classification.confidence}"
        )
    lines.extend(("", f"## {headings[6][0 if zh else 1]}", ""))
    lines.extend(f"- {defect}" for defect in report.defects)
    lines.extend(
        ("", f"## {headings[7][0 if zh else 1]}", "", f"- {report.execution.conclusion}")
    )
    lines.extend(("", f"## {headings[8][0 if zh else 1]}", ""))
    lines.extend(f"- missing_evidence: {item}" for item in report.missing_evidence)
    lines.extend(f"- blocker: {item}" for item in report.blockers)
    lines.extend(f"- {risk}" for risk in report.risks)
    return "\n".join(lines).rstrip() + "\n"
