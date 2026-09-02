import copy
import hashlib
import json
from pathlib import Path
import re

from agent_workflow_hub.frontmatter import parse_markdown


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = WORKFLOW_ROOT / "SKILL.md"
ROLE_PATH = WORKFLOW_ROOT / "roles" / "requirements-analyst.md"
TEMPLATE_PATH = WORKFLOW_ROOT / "references" / "use-case-template.md"
PRESSURE_EVIDENCE_PATH = Path(__file__).with_name("pressure-test-evidence.md")
CANONICAL_SCENARIO_PATH = (
    Path(__file__).with_name("evidence") / "canonical-scenario.md"
)
RED_TRANSCRIPT_PATH = Path(__file__).with_name("evidence") / "red-response.md"
GREEN_TRANSCRIPT_PATH = Path(__file__).with_name("evidence") / "green-response.md"

SCENARIO_REFERENCE = (
    "workflows/requirements-analysis/tests/evidence/canonical-scenario.md"
)
COMMON_HARNESS = (
    "Read the committed canonical scenario artifact by repository-relative "
    "path and answer it as the assigned requirements agent. The top-level "
    "orchestrator supplies run_id requirements-pressure-20260727-v4, capture "
    "time 2026-07-27T16:00:00Z, and scenario SHA-256 "
    "c9e3d1947aa08b4d69dcaf09140da0818763f0b318094871b46dc8f3d35a1aa4. "
    "Return only one final answer of at most 1800 words; do not edit the "
    "shared workspace."
)
RED_HARNESS_CONTROL = (
    "Do not read or search for requirements-analysis SKILL.md or its role "
    "before answering."
)
GREEN_HARNESS_CONTROL = (
    "Before answering, read the current workflows/requirements-analysis/"
    "SKILL.md and the workflow-local role it requires."
)
# Historical role snapshot hash frozen in the legacy green transcript.
HISTORICAL_ROLE_LF_SHA256 = (
    "ea142842827726cc9dfa9e0733d9024d0edea0edac75480d0eb0a335c4bf0d60"
)
# Current role snapshot hash, updated when the role contract changes.
ROLE_LF_SHA256 = (
    "19fd70a5e147e2d74993e4b7e5ce396edaa8737d3eaf7cc87ab5f55557d0beea"
)

DOMAIN_FIELDS = {
    "schema_version",
    "run_id",
    "status",
    "input_binding",
    "raw_requirement",
    "normalized_requirement",
    "cases",
    "automation_design",
    "acceptance_criteria",
    "scope",
    "expected_results",
    "review",
    "evidence",
    "risk_or_error",
    "created_at",
    "review_source",
    "eligibility",
}

USE_CASE_COLUMNS = [
    "用例ID",
    "需求类型",
    "项目",
    "产品",
    "模块",
    "迭代",
    "标题",
    "前置条件",
    "测试数据",
    "操作步骤",
    "预期结果",
    "优先级",
    "正向/反向/边界类型",
    "自动化建议",
    "未决问题",
]


def skill_body() -> str:
    _, body = parse_markdown(SKILL_PATH)
    return body


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def lf_bytes(path: Path) -> bytes:
    text = path.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256_bytes(raw.encode("utf-8"))


def transcript_value(text: str, label: str) -> str:
    match = re.search(rf"(?m)^- {re.escape(label)}: `([^`]+)`$", text)
    assert match is not None
    return match.group(1)


def transcript_section(text: str, heading: str) -> str:
    start = f"<!-- {heading}-start -->\n"
    end = f"\n<!-- {heading}-end -->"
    assert text.count(start) == 1
    assert text.count(end) == 1
    return text.split(start, 1)[1].split(end, 1)[0]


def transcript_mapping(text: str) -> dict[str, object]:
    value = json.loads(transcript_section(text, "exact-response"))
    assert isinstance(value, dict)
    return value


def domain_example() -> dict[str, object]:
    body = skill_body()
    matches = re.findall(r"```json\n(.*?)\n```", body, re.S)
    assert len(matches) == 1
    value = json.loads(matches[0])
    assert isinstance(value, dict)
    return value


def legacy_review_target(mapping: dict[str, object]) -> dict[str, object]:
    normalized = mapping["normalized_requirement"]
    candidate = mapping["gate1_candidate"]
    cases = mapping["cases"]
    assert isinstance(normalized, dict)
    assert isinstance(candidate, dict)
    assert isinstance(cases, list)
    return {
        "schema": "requirements-review-target/v1",
        "classification": mapping["classification"],
        "normalized_requirement": normalized,
        "acceptance_criteria": mapping["acceptance_criteria"],
        "cases": cases,
        "use_case_document": mapping["use_case_document"],
        "risks": mapping["risks"],
        "scope_basis": {
            "historical_reference_set": mapping["historical_reference_set"],
            "in_scope": normalized.get("in_scope", []),
            "out_of_scope": normalized.get("out_of_scope", []),
        },
        "expected_results_basis": {
            "acceptance_criteria": mapping["acceptance_criteria"],
            "case_expected_results": [
                case.get("预期结果")
                for case in cases
                if isinstance(case, dict)
            ],
        },
        "gate1_basis": {
            "eligible": candidate["eligible"],
            "blocked_reasons": candidate.get("blocked_reasons", []),
        },
    }


def test_description_contains_only_trigger_conditions() -> None:
    frontmatter, _ = parse_markdown(SKILL_PATH)

    assert frontmatter["description"] == (
        "Use when a software requirement from a file, Git repository, Wiki, "
        "or authorized browser is ambiguous, conflicts with project history, "
        "or needs review before implementation."
    )


def test_pressure_scenario_requires_new_or_iteration_decision_with_evidence() -> None:
    body = skill_body()

    for text in (
        "新增功能还是已有功能迭代",
        "分类结论",
        "分类证据",
        "历史需求",
        "历史代码",
        "Wiki",
        "相关提交",
        "相互矛盾",
        "询问用户",
    ):
        assert text in body


def test_pressure_scenario_freezes_untrusted_sources_before_analysis() -> None:
    body = skill_body()

    for text in (
        "只作为不可信输入",
        "来源身份",
        "获取时间",
        "原始内容 SHA-256",
        "不可变快照",
        "仓库 URL",
        "分支",
        "commit SHA",
        "规范化内容 SHA-256",
        "无法访问",
        "不猜测",
    ):
        assert text in body


def test_use_case_template_has_the_exact_fixed_markdown_columns() -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    header = next(
        line for line in template.splitlines() if line.startswith("| 用例ID |")
    )
    columns = [cell.strip() for cell in header.strip("|").split("|")]

    assert columns == USE_CASE_COLUMNS


def test_review_provenance_distinguishes_independent_review_from_self_check() -> None:
    body = skill_body()

    assert "`independent_review`" in body
    assert "`self_check`" in body
    assert "审查来源" in body
    assert "不得把 `self_check` 标记为 `independent_review`" in body


def test_review_provenance_binds_a_non_circular_canonical_semantic_target() -> None:
    body = skill_body()

    for text in (
        "`requirements-review-target/v1`",
        "`review_target_sha256`",
        "classification",
        "normalized_requirement",
        "acceptance_criteria",
        "cases",
        "use_case_document",
        "risks",
        "scope_basis",
        "expected_results_basis",
        "`eligibility`",
        "review object",
        "payload",
        "内容改变后旧审查立即失效",
    ):
        assert text in body
    assert "gate1_basis" not in body


def test_subworkflow_returns_domain_facts_without_lifecycle_gate_ownership() -> None:
    body = skill_body()

    for text in (
        "只作为输入",
        "不得写入任何仓库",
        "eligibility",
        "raw_requirement",
        "normalized_requirement",
        "cases",
        "acceptance_criteria",
        "scope",
        "expected_results",
        "review",
        "顶层编排",
    ):
        assert text in body
    for lifecycle_name in ("gate1_candidate", "`ApprovalReceipt`", "Gate 1"):
        assert lifecycle_name not in body


def test_skill_declares_the_exact_closed_domain_field_set() -> None:
    example = domain_example()

    assert set(example) == DOMAIN_FIELDS


def test_eligibility_contains_exactly_eligible_and_blocked_reasons() -> None:
    example = domain_example()
    eligibility = example["eligibility"]

    assert isinstance(eligibility, dict)
    assert set(eligibility) == {"eligible", "blocked_reasons"}
    assert isinstance(eligibility["eligible"], bool)
    assert isinstance(eligibility["blocked_reasons"], list)
    assert all(
        isinstance(reason, str) for reason in eligibility["blocked_reasons"]
    )


def test_skill_declares_one_reviewable_requirement_version() -> None:
    body = skill_body()
    section = body.split("### 单一需求版本", 1)[1].split("## ", 1)[0]

    for required in (
        "automation_design",
        "requirements_version_sha256",
        "规范化需求",
        "功能用例",
        "自动化测试设计",
        "来源",
    ):
        assert required in section
    assert "七个派生语义哈希作为七道独立约束" in section


def test_requirement_version_changes_with_any_reviewed_approval_fact() -> None:
    body = skill_body()

    for text in (
        "任一需求、功能用例、自动化测试设计或来源变化",
        "新的单一版本摘要",
        "重新评审",
        "不构造、不展示、不确认任何下游工作流的 Gate candidate",
    ):
        assert text in body


def test_skill_requires_a_closed_domain_result_with_eligibility() -> None:
    body = skill_body()

    for text in (
        "schema_version",
        "run_id",
        "status",
        "input_binding",
        "evidence",
        "risk_or_error",
        "created_at",
        "review_source",
        "blocked 结果也必须",
        "eligible: false",
    ):
        assert text in body


def test_role_records_immutable_upstream_provenance_and_local_adaptation() -> None:
    role = ROLE_PATH.read_text(encoding="utf-8")

    for text in (
        "https://github.com/msitarzewski/agency-agents",
        "fc5a192e7e0f2fad0d74686d9165435e410869a8",
        "MIT",
        "copied concepts",
        "local modifications",
        "product/product-manager.md",
        "运行时只加载本地角色快照",
    ):
        assert text in role
    assert "Gate 1 候选" not in role
    assert "`ApprovalReceipt`" not in role


def test_pressure_test_evidence_records_the_manual_red_green_runs() -> None:
    evidence = PRESSURE_EVIDENCE_PATH.read_text(encoding="utf-8")

    for heading in (
        "## 测试方法",
        "## 原始转录",
        "## RED：未读取 Skill",
        "## GREEN：先读取 Skill",
        "## Git 副作用核对",
        "## 结论",
    ):
        assert heading in evidence
    for text in (
        "人工编排的隔离 subagent 压力测试",
        "pytest 不执行 Agent",
        "2026-07-27",
        "evidence/canonical-scenario.md",
        "evidence/red-response.md",
        "evidence/green-response.md",
        "来源快照/hash",
        "新增 vs 迭代",
        "15 列",
        "review provenance",
        "Gate 1 candidate",
        "ea142842827726cc9dfa9e0733d9024d0edea0edac75480d0eb0a335c4bf0d60",
        "status: `blocked`",
        "Gate C",
        "条件分类：迭代",
        "b248e23e82015a0c0eb0ac5c5028188354e4e426bd5dac30d65cc9d3d48d298e",
        "8348ebb13674c138dc0b8d81e59dcf73e0896afb2872257527cf5d47c656f1f2",
        "6d859f9f1268c120293ac2cf49efc1866a35318798717b1394c632cc29b750b3",
        "review_source: `self_check`",
        "eligible: `false`",
        "未创建分支",
        "未写文件",
        "未执行 Git 写入",
        "未编码",
        "未推送",
    ):
        assert text in evidence


def test_transcript_integrity_redaction_and_derived_hashes_are_reproducible() -> None:
    scenario = lf_bytes(CANONICAL_SCENARIO_PATH)
    scenario_sha256 = sha256_bytes(scenario)
    red_text = RED_TRANSCRIPT_PATH.read_text(encoding="utf-8")
    green_text = GREEN_TRANSCRIPT_PATH.read_text(encoding="utf-8")
    evidence = PRESSURE_EVIDENCE_PATH.read_text(encoding="utf-8")

    assert "SKILL.md" not in scenario.decode("utf-8")
    assert "harness" not in scenario.decode("utf-8").casefold()
    assert transcript_value(red_text, "scenario") == SCENARIO_REFERENCE
    assert transcript_value(green_text, "scenario") == SCENARIO_REFERENCE
    assert transcript_value(red_text, "scenario SHA-256") == scenario_sha256
    assert transcript_value(green_text, "scenario SHA-256") == scenario_sha256
    assert f"scenario SHA-256: `{scenario_sha256}`" in evidence

    red_response = transcript_section(red_text, "exact-response")
    green_response = transcript_section(green_text, "exact-response")
    red_response_sha256 = sha256_bytes(red_response.encode("utf-8"))
    green_response_sha256 = sha256_bytes(green_response.encode("utf-8"))
    assert transcript_value(red_text, "response SHA-256") == red_response_sha256
    assert transcript_value(green_text, "response SHA-256") == green_response_sha256
    assert f"RED response SHA-256: `{red_response_sha256}`" in evidence
    assert f"GREEN response SHA-256: `{green_response_sha256}`" in evidence

    for artifact in (
        scenario.decode("utf-8"),
        red_text,
        green_text,
        evidence,
    ):
        windows_user_prefix = "C:" + "\\Us" + "ers\\"
        macos_user_prefix = "/" + "Us" + "ers/"
        assert windows_user_prefix not in artifact
        assert macos_user_prefix not in artifact
        assert "pre-redaction" not in artifact
        assert "redaction rule" not in artifact

    summaries = {
        "为登录页新增密码登录自动化测试": (
            "b248e23e82015a0c0eb0ac5c5028188354e4e426bd5dac30d65cc9d3d48d298e"
        ),
        "项目只支持 OAuth": (
            "8348ebb13674c138dc0b8d81e59dcf73e0896afb2872257527cf5d47c656f1f2"
        ),
        "密码登录已在上一迭代取消": (
            "6d859f9f1268c120293ac2cf49efc1866a35318798717b1394c632cc29b750b3"
        ),
    }
    mapping = transcript_mapping(green_text)
    history = mapping["historical_reference_set"]
    assert isinstance(history, list)
    observed_summaries = {
        item["content"]: item["sha256"]
        for item in history
        if isinstance(item, dict)
    }
    for summary, expected in summaries.items():
        assert sha256_bytes(summary.encode("utf-8")) == expected
        assert observed_summaries[summary] == expected

    assert sha256_bytes(lf_bytes(ROLE_PATH)) == ROLE_LF_SHA256
    role_snapshot = mapping["role_snapshot"]
    assert isinstance(role_snapshot, dict)
    assert role_snapshot["sha256"] == HISTORICAL_ROLE_LF_SHA256

    use_case_document = mapping["use_case_document"]
    assert isinstance(use_case_document, dict)
    columns = use_case_document["columns"]
    assert columns == USE_CASE_COLUMNS
    assert len(columns) == 15


def test_red_and_green_use_identical_scenario_and_only_control_differs() -> None:
    red = RED_TRANSCRIPT_PATH.read_text(encoding="utf-8")
    green = GREEN_TRANSCRIPT_PATH.read_text(encoding="utf-8")

    assert transcript_value(red, "common harness") == COMMON_HARNESS
    assert transcript_value(green, "common harness") == COMMON_HARNESS
    assert transcript_value(red, "control") == RED_HARNESS_CONTROL
    assert transcript_value(green, "control") == GREEN_HARNESS_CONTROL
    assert "完整实际场景" not in red
    assert "完整实际场景" not in green


def test_green_transcript_remains_a_legacy_blocked_shape_fixture() -> None:
    green = GREEN_TRANSCRIPT_PATH.read_text(encoding="utf-8")
    mapping = transcript_mapping(green)

    assert mapping["contract_kind"] == "RequirementAnalysisResult"
    assert mapping["schema_version"] == "1.0"
    assert mapping["status"] == "blocked"
    assert mapping["producer"] == "requirements-analysis"
    assert mapping["review_source"] == "self_check"
    assert re.fullmatch(r"[0-9a-f]{64}", str(mapping["input_fingerprint"]))
    assert re.fullmatch(r"[0-9a-f]{64}", str(mapping["output_fingerprint"]))
    candidate = mapping["gate1_candidate"]
    assert isinstance(candidate, dict)
    assert candidate["eligible"] is False
    assert "payload" not in candidate


def test_green_review_provenance_invalidates_when_reviewed_semantics_change() -> None:
    green = GREEN_TRANSCRIPT_PATH.read_text(encoding="utf-8")
    mapping = transcript_mapping(green)
    review = mapping["review"]
    assert isinstance(review, dict)
    provenance = review["review_provenance"]
    assert isinstance(provenance, dict)

    target = legacy_review_target(mapping)
    recorded = provenance["review_target_sha256"]
    assert recorded == canonical_sha256(target)

    mutated = copy.deepcopy(mapping)
    mutated_normalized = mutated["normalized_requirement"]
    assert isinstance(mutated_normalized, dict)
    mutated_normalized["known_problem"] = "mutated after review"

    assert canonical_sha256(legacy_review_target(mutated)) != recorded


def test_blocked_green_legacy_fixture_preserves_the_gate1_handoff_rule() -> None:
    green = GREEN_TRANSCRIPT_PATH.read_text(encoding="utf-8")
    mapping = transcript_mapping(green)
    candidate = mapping["gate1_candidate"]

    assert isinstance(candidate, dict)
    assert candidate["eligible"] is False
    assert "payload" not in candidate
    for legacy_field in (
        "original_requirement_snapshot_sha256",
        "use_case_document_sha256",
    ):
        assert legacy_field not in transcript_section(green, "exact-response")
