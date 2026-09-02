from pathlib import Path

from agent_workflow_hub.contracts import validate_capability, validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


CAPABILITIES = {
    "app.blender",
    "app.bambu-studio",
    "mcp.blender",
    "app.prusaslicer",
    "app.orcaslicer",
}
REQUIRED_SECTIONS = (
    "## Purpose",
    "## Install",
    "## Security",
    "## Success",
    "## Known limitations",
    "## Alternatives",
    "## Rollback",
)


def test_3d_capabilities_have_valid_contracts():
    found = set()
    bodies = {}
    for path in Path("capabilities").glob("*/*/CAPABILITY.md"):
        contract = validate_capability(path, *parse_markdown(path))
        if contract.id in CAPABILITIES:
            found.add(contract.id)
            bodies[contract.id] = contract.body

    assert found == CAPABILITIES
    assert all(
        section in body
        for body in bodies.values()
        for section in REQUIRED_SECTIONS
    )


def test_orcaslicer_is_not_claimed_as_automated_before_smoke_evidence():
    path = Path("capabilities/app/orcaslicer/CAPABILITY.md")
    frontmatter, _ = parse_markdown(path)

    assert frontmatter["automation_status"] == "manual"
    assert all(state == "unverified" for state in frontmatter["hosts"].values())


def test_blender_mcp_source_can_be_agent_managed_in_shared_workspace():
    path = Path("capabilities/mcp/blender/CAPABILITY.md")
    frontmatter, _ = parse_markdown(path)

    assert frontmatter["installation"]["policy"] == "agent-managed"
    assert frontmatter["installation"]["scope"] == "workspace-shared"
    assert frontmatter["installation"]["methods"] == ["existing", "git"]
    assert frontmatter["workspace_source"] == "workspace/shared/mcp/blender-mcp"


def test_bambu_capability_is_user_managed_and_conditional():
    path = Path("capabilities/app/bambu-studio/CAPABILITY.md")
    frontmatter, _ = parse_markdown(path)

    assert frontmatter["installation"]["policy"] == "user-managed"
    assert frontmatter["automation_status"] == "conditional"
    assert frontmatter["detect"]["mode"] == "read-only"


def test_3d_skill_contains_required_safety_boundaries():
    path = Path("workflows/3d-printing/SKILL.md")
    contract = validate_skill(path, *parse_markdown(path))
    body = contract.body

    assert "不得发送或启动打印" in body
    assert "切换 slicer provider" in body
    assert "重新确认门 B" in body
    assert "已提交且哈希匹配的 Blender Python" in body
    assert "DISABLE_TELEMETRY=true" in body
    assert "Blender and slicer applications are `user-managed` system setup" in body
    assert "INSTALLATION-GUIDE.md" in body
    assert "--language <current-user-language>" in body
    assert "`version_requirement` minimum" in body
    assert "`recommended_version`" in body

def test_3d_skill_allows_gui_as_evidence_gated_assistance():
    path = Path("workflows/3d-printing/SKILL.md")
    frontmatter, body = parse_markdown(path)
    contract = validate_skill(path, frontmatter, body)

    assert frontmatter["metadata"]["required-capabilities"] == '["app.blender"]'
    assert frontmatter["metadata"]["workflow-version"] == "0.4.10"
    assert "Split-and-plate is opt-in only" in contract.body
    assert "headless Blender" in contract.body
    assert "PrusaSlicer and OrcaSlicer are optional" in contract.body
    assert "Bambu Studio is required only for Bambu G-code 3MF delivery" in contract.body
    assert "needs_user_split_request" in contract.body
    assert "needs_provider_support" in contract.body
    assert "GUI 可作为辅助通道" in contract.body
    assert "GUI 操作本身不得作为成功证据" in contract.body
    assert "needs_user_validation" in contract.body
    assert "No GUI automation" not in contract.body
    assert "never fall back to GUI automation" not in contract.body


def test_3d_skill_ends_at_reviewed_artifact_delivery():
    path = Path("workflows/3d-printing/SKILL.md")
    frontmatter, body = parse_markdown(path)

    assert frontmatter["metadata"]["workflow-version"] == "0.4.10"
    assert "The workflow ends at reviewed artifact delivery" in body
    assert "It never uploads, queues, sends, or starts a print" in body


def test_3d_skill_defaults_to_light_validation_and_user_review():
    path = Path("workflows/3d-printing/SKILL.md")
    _frontmatter, body = parse_markdown(path)
    smoke = Path("workflows/3d-printing/references/smoke-test.md").read_text(
        encoding="utf-8"
    )

    assert "`--validation-level light`" in body
    assert "generated_for_user_review" in body
    assert "accepted_by_user" in body
    assert "不得重复执行完整验证" in body
    assert "Run every check in mesh-validation.md" not in smoke
    assert "Full validation is opt-in" in smoke
    assert "connector local" in smoke
    assert "wall/edge samples" in smoke
    assert "exact Blender Python" not in smoke
    assert "自动检查已通过，或未完成项已明确交由用户且获得" in body
    assert "`accepted_by_user`" in body


def test_3d_skill_separates_generated_review_from_user_accepted_delivery():
    body = Path("workflows/3d-printing/SKILL.md").read_text(encoding="utf-8")

    assert "artifact-review state" in body
    assert "does not require a manifest" in body
    assert "gate-c-confirmed" in body
    assert "delivered" in body


def test_3d_skill_requires_workspace_rooted_formal_paths():
    body = Path("workflows/3d-printing/SKILL.md").read_text(encoding="utf-8")

    assert "resolve the Hub root once" in body
    assert "plan and output paths" in body
    assert "outputs/<run-id>" in body


def test_3d_skill_constrains_unconfirmed_split_proposals():
    body = Path("workflows/3d-printing/SKILL.md").read_text(encoding="utf-8")

    assert "coordinate axes do not establish" in body
    assert "semantic labels such as left/right" in body
    assert "must not use a pin diameter" in body
    assert "needs_user_split_plan" in body
    assert "visual-capable host" in body
    assert "proposed-cuts" in body
    assert "requires_gate_a_confirm" in body
    assert "must never be named `split_plan.json`" in body
    assert "three_mf_import.py" in body
    assert "load_3mf_mesh" in body
    assert "needs_host_vision_support" in body
    assert "must not publish semantic labels, proposed cuts, or proposed connectors" in body
    assert "A global plane alone must not be labelled as isolating an arm" in body
    assert "component-selection evidence" in body
    assert "signed seed-to-plane distance" in body
    assert "must match `target_side`" in body


def test_3d_skill_routes_mcp_assistance_and_headless_execution_separately():
    body = Path("workflows/3d-printing/SKILL.md").read_text(encoding="utf-8")

    assert "MCP-assisted inspection" in body
    assert "候选连通体标色" in body
    assert "正式拆件仍使用 headless Blender" in body
    assert "双向 BMesh bisect" in body
    assert "returned_components" in body
    assert "局部有界 cutter" not in body
    assert "纯 STL/结构图拆件只要求经过验证的 headless cutter" in body
    assert "package verifier 只在对应" in body
    assert "3MF 交付分支需要" in body


def test_3d_skill_promotes_outputs_before_any_authorized_legacy_delete():
    body = Path("workflows/3d-printing/SKILL.md").read_text(encoding="utf-8")

    assert "replacement → final" in body
    assert "获得 `accepted_by_user` 前不得删除旧产物" in body
    assert "精确路径授权" in body


def test_3d_skill_declares_keyed_connector_inspection_and_approval_boundary():
    path = Path("workflows/3d-printing/SKILL.md")
    _frontmatter, body = parse_markdown(path)

    assert "integrated-keyed-pin" in body
    assert "six-view read-only inspection is optional" in body
    assert "Gate A must confirm every exact connector parameter" in body
    assert "只展示脚本路径、SHA-256" in body
    assert "exact Blender background command" in body
    assert "脚本内容或哈希发生变化" in body
    assert "优先推荐 `integrated-keyed-pin`，胶水仅作为备选" in body
    assert "推荐不等于 Gate A 批准" in body
    assert "接合面、壁厚、边距、打印方向、装配次数和公差" in body


def test_3d_skill_standard_mesh_branch_needs_no_bambu_provider_or_gui_success_claim():
    path = Path("workflows/3d-printing/SKILL.md")
    _frontmatter, body = parse_markdown(path)

    assert "Standard multi-object 3MF does not require a Bambu provider" in body
    assert "source model remains immutable" in body
    assert "独立证据复核" in body
    assert "never fall back to GUI automation" not in body
    assert "upload=false" in body
    assert "send=false" in body
    assert "queue=false" in body
    assert "printer_started=false" in body


def test_gui_assistance_requires_independent_evidence_in_references():
    smoke = Path("workflows/3d-printing/references/smoke-test.md").read_text(
        encoding="utf-8"
    )
    split = Path(
        "workflows/3d-printing/references/split-and-slice-bambu.md"
    ).read_text(encoding="utf-8")

    assert "GUI can assist" in smoke
    assert "needs_user_validation" in smoke
    assert "independent artifact evidence" in split
    assert "失败时不回退 GUI" not in split


def test_3d_skill_defines_component_safe_stl_diagram_delivery():
    path = Path("workflows/3d-printing/SKILL.md")
    _frontmatter, body = parse_markdown(path)

    assert "每个最终 STL 必须恰好包含一个连通体" in body
    assert "不得按体积、面数或尺寸" in body
    assert "保留并归还原部位" in body
    assert "needs_user_component_assignment" in body
    assert "structure_diagram_filename" in body
    assert "Formal artifact boundary" in body
    assert "renderer failure is a blocked delivery" in body
    assert "结构图不是打印分盘图" in body
    assert "STL + 结构图" in body
    assert "不要求 Gate B" in body
    assert "distance_tolerance_mm=0.02" in body
    assert "dominance_ratio=10.0" in body
    assert "适用时记录 provider/profile snapshot" in body
    assert "只有交付目标包含打印盘" in body
    assert "Bambu G-code 3MF 交付目标另加" in body
    assert "STL + 结构图计划必须保留逻辑 assembly mapping" in body
