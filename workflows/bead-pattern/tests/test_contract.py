import json
from pathlib import Path

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_SKILL = REPOSITORY_ROOT / "workflows" / "bead-pattern" / "SKILL.md"
CAPABILITY = REPOSITORY_ROOT / "capabilities" / "python" / "pillow" / "CAPABILITY.md"


def test_bead_pattern_workflow_is_catalogued_and_requires_pillow():
    assert WORKFLOW_SKILL.is_file()
    assert CAPABILITY.is_file()

    frontmatter, body = parse_markdown(WORKFLOW_SKILL)
    contract = validate_skill(WORKFLOW_SKILL, frontmatter, body)

    assert contract.name == "bead-pattern"
    assert json.loads(frontmatter["metadata"]["required-capabilities"]) == [
        "python.pillow"
    ]
    for heading in (
        "用途与触发条件",
        "非目标",
        "输入",
        "输出与命名规则",
        "依赖和运行前检查",
        "系统修改与权限影响",
        "执行步骤",
        "人工确认门",
        "失败恢复",
        "重跑、幂等与覆盖策略",
        "验收标准",
        "清理方式",
    ):
        assert f"## {heading}" in body

    assert "pattern.png" in body
    assert "workspace/workflows/bead-pattern/runs/<run-id>/" in body
    assert "workflows/bead-pattern/outputs/<run-id>/pattern.png" in body


def test_pillow_capability_uses_a_hash_checked_requirements_lock():
    lock = REPOSITORY_ROOT / "requirements" / "pillow-12.3.0.txt"
    capability_body = CAPABILITY.read_text(encoding="utf-8")

    assert lock.read_text(encoding="utf-8") == (
        "Pillow==12.3.0 "
        "--hash=sha256:1cca606cd25738df4ed873d5ad46bbdb3d83b5cbca291f6b4ff13a4df6b0bbe8\n"
    )
    assert "requirements/pillow-12.3.0.txt" in capability_body
    assert "--require-hashes -r" in capability_body
    assert "--index-url https://pypi.org/simple" in capability_body
