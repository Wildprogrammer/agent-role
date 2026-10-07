from __future__ import annotations

from pathlib import Path

import pytest

from agent_workflow_hub.specialized_agent_deployment.contracts import (
    DeploymentRequest,
    SkillSelection,
)
from agent_workflow_hub.specialized_agent_deployment.sources import (
    SourceDriftError,
    SourceSnapshotError,
    copy_snapshot,
    resolve_skill_source,
    snapshot_composition,
    snapshot_skill,
)


def write_skill(root: Path, name: str) -> Path:
    skill_root = root / name
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {name} test fixture\n"
        "---\n"
        "# Instructions\n\nUse the fixture.\n",
        encoding="utf-8",
    )
    return skill_root


def selection(
    name: str,
    *,
    source_kind: str = "hub-workflow",
    source: str | None = None,
) -> SkillSelection:
    return SkillSelection.from_mapping(
        {
            "name": name,
            "source_kind": source_kind,
            "source": source or f"workflows/{name}",
            "reason": "fixture selection",
        }
    )


@pytest.fixture
def hub_root(tmp_path: Path) -> Path:
    root = tmp_path / "hub"
    write_skill(root / "workflows", "primary-flow")
    write_skill(root / "workflows", "related-flow")
    return root


@pytest.fixture
def external_skill(tmp_path: Path) -> Path:
    return write_skill(tmp_path / "external", "helper-skill")


def request_for(
    tmp_path: Path,
    external_skill: Path,
) -> DeploymentRequest:
    return DeploymentRequest.from_mapping(
        {
            "schema_version": "1.0",
            "deployment_id": "fixture-deployment",
            "agent_id": "fixture-agent",
            "display_name": "Fixture Agent",
            "purpose": "Exercise snapshot composition",
            "host": "hermes",
            "mode": "create",
            "primary_workflow": "primary-flow",
            "related_workflows": [
                selection("related-flow").to_mapping(),
            ],
            "auxiliary_skills": [
                selection(
                    "helper-skill",
                    source_kind="external-skill",
                    source=str(external_skill.resolve()),
                ).to_mapping(),
            ],
            "workdir": str((tmp_path / "work").resolve()),
            "config_refs": [],
            "host_options": {},
        }
    )


def test_composition_uses_explicit_agent_selection_only(
    hub_root: Path, external_skill: Path, tmp_path: Path
) -> None:
    request = request_for(tmp_path, external_skill)
    snapshots = snapshot_composition(hub_root, request)
    assert [item.selection.name for item in snapshots] == [
        request.primary_workflow,
        *(item.name for item in request.related_workflows),
        *(item.name for item in request.auxiliary_skills),
    ]


def test_snapshot_accepts_valid_files_that_sort_before_skill_md(
    hub_root: Path,
) -> None:
    skill_root = hub_root / "workflows" / "primary-flow"
    (skill_root / "README.md").write_text("# Fixture README\n", encoding="utf-8")

    snapshot = snapshot_skill(hub_root, selection("primary-flow"))

    assert [item.relative_path for item in snapshot.files][:2] == [
        "README.md",
        "SKILL.md",
    ]


def test_hub_source_is_fixed_under_workflows(hub_root: Path) -> None:
    selected = selection("primary-flow")
    assert resolve_skill_source(hub_root, selected) == (
        hub_root / "workflows" / "primary-flow"
    ).resolve()

    spoofed = SkillSelection(
        name="primary-flow",
        source_kind="hub-workflow",
        source="elsewhere/primary-flow",
        reason="spoofed",
    )
    with pytest.raises(SourceSnapshotError):
        resolve_skill_source(hub_root, spoofed)


def test_external_source_must_be_absolute(tmp_path: Path) -> None:
    selected = SkillSelection(
        name="helper-skill",
        source_kind="external-skill",
        source=str((tmp_path / "missing").resolve()),
        reason="external",
    )
    with pytest.raises(SourceSnapshotError):
        resolve_skill_source(tmp_path.resolve(), selected)


def test_snapshot_calls_hub_parser_and_validator(
    hub_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_workflow_hub.specialized_agent_deployment.sources as sources

    calls = {"parse": 0, "validate": 0}
    real_parse = sources.parse_markdown
    real_validate = sources.validate_skill

    def tracked_parse(path: Path):
        calls["parse"] += 1
        return real_parse(path)

    def tracked_validate(path: Path, frontmatter, body):
        calls["validate"] += 1
        return real_validate(path, frontmatter, body)

    monkeypatch.setattr(sources, "parse_markdown", tracked_parse)
    monkeypatch.setattr(sources, "validate_skill", tracked_validate)
    snapshot_skill(hub_root, selection("primary-flow"))
    assert calls == {"parse": 1, "validate": 1}


def test_skill_name_must_match_selection(hub_root: Path) -> None:
    skill_file = hub_root / "workflows" / "primary-flow" / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace(
            "name: primary-flow", "name: other-flow"
        ),
        encoding="utf-8",
    )
    with pytest.raises(SourceSnapshotError):
        snapshot_skill(hub_root, selection("primary-flow"))


def test_snapshot_includes_resources_and_skips_only_runtime_data(
    hub_root: Path,
) -> None:
    root = hub_root / "workflows" / "primary-flow"
    (root / "references").mkdir()
    (root / "references" / "guide.txt").write_text("guide", encoding="utf-8")
    for directory in (
        "outputs",
        "tests",
        "__pycache__",
        ".pytest_cache",
        ".git",
        ".codex-remote-attachments",
    ):
        (root / directory).mkdir()
        (root / directory / "ignored.txt").write_text("ignored", encoding="utf-8")
    (root / "scratch.tmp").write_text("temporary", encoding="utf-8")
    (root / "notes.txt~").write_text("temporary", encoding="utf-8")

    snapshot = snapshot_skill(hub_root, selection("primary-flow"))
    paths = [item.relative_path for item in snapshot.files]
    assert paths == ["SKILL.md", "references/guide.txt"]


def test_snapshot_rejects_symlink(
    hub_root: Path,
) -> None:
    root = hub_root / "workflows" / "primary-flow"
    target = root / "real.txt"
    target.write_text("real", encoding="utf-8")
    link = root / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(SourceSnapshotError):
        snapshot_skill(hub_root, selection("primary-flow"))


def test_snapshot_detects_source_drift(
    hub_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = hub_root / "workflows" / "primary-flow"
    resource = root / "resource.txt"
    resource.write_text("before", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def drifting_read(path: Path) -> bytes:
        data = original_read_bytes(path)
        if path == resource:
            path.write_text("changed", encoding="utf-8")
        return data

    monkeypatch.setattr(Path, "read_bytes", drifting_read)
    with pytest.raises(SourceDriftError):
        snapshot_skill(hub_root, selection("primary-flow"))


def test_snapshot_does_not_read_remote_attachments(
    hub_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = hub_root / "workflows" / "primary-flow"
    attachment = root / ".codex-remote-attachments" / "secret.bin"
    attachment.parent.mkdir()
    attachment.write_bytes(b"do-not-read")
    original_read_bytes = Path.read_bytes

    def guarded_read(path: Path) -> bytes:
        if ".codex-remote-attachments" in path.parts:
            raise AssertionError("attachment was read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    snapshot_skill(hub_root, selection("primary-flow"))


def test_copy_snapshot_revalidates_content(
    hub_root: Path, tmp_path: Path
) -> None:
    selected = selection("primary-flow")
    snapshot = snapshot_skill(hub_root, selected)
    source = resolve_skill_source(hub_root, selected)
    destination = (tmp_path / "copied").resolve()
    copy_snapshot(snapshot, source, destination)
    assert (destination / "SKILL.md").read_bytes() == (
        source / "SKILL.md"
    ).read_bytes()

    (source / "SKILL.md").write_text("changed", encoding="utf-8")
    with pytest.raises(SourceDriftError):
        copy_snapshot(snapshot, source, (tmp_path / "other-copy").resolve())


def test_repository_has_no_second_dependency_definition() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    assert not list(repository_root.rglob("dependencies.yml"))
    assert not list(repository_root.rglob("workflow-dependencies.yml"))
