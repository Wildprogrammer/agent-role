import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "preflight.py"
SPEC = importlib.util.spec_from_file_location("workflow_3d_preflight", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def isolated_hub(tmp_path: Path) -> Path:
    root = tmp_path / "hub"
    for _, relative_path in MODULE.CAPABILITY_PATHS:
        source = Path.cwd() / relative_path
        destination = root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return root


def test_preflight_reports_missing_tools_without_invoking_installers(tmp_path):
    invoked = []

    checks = MODULE.inspect_environment(
        Path.cwd(),
        which=lambda _: None,
        runner=lambda command: invoked.append(command) or "",
    )

    assert not invoked
    assert {check.id for check in checks} == {
        "app.blender",
        "mcp.blender",
        "app.bambu-studio",
        "app.prusaslicer",
        "app.orcaslicer",
    }
    assert all(check.status == "missing" for check in checks)
    assert not MODULE.has_local_dependencies(checks)


def test_preflight_separates_local_dependency_readiness_from_host_smoke(tmp_path):
    hub_root = isolated_hub(tmp_path)
    (hub_root / "workspace" / "shared" / "mcp" / "blender-mcp").mkdir(
        parents=True
    )
    outputs = {
        "blender": "Blender 5.1.2",
        "uvx": "uvx 0.8.19",
        "bambu-studio": "BambuStudio 02.07.01.62",
        "prusa-slicer-console": "PrusaSlicer 2.9.6",
        "orcaslicer": "OrcaSlicer 2.4.2",
    }

    checks = MODULE.inspect_environment(
        hub_root,
        which=lambda executable: executable,
        runner=lambda command: (
            "6e99eb5a442b83766a5796975ec7bb5bfc791341"
            if Path(command[0]).name == "git"
            else outputs[Path(command[0]).name]
        ),
    )

    assert next(check for check in checks if check.id == "mcp.blender").status == (
        "host-configuration-required"
    )
    assert MODULE.has_local_dependencies(checks)


def test_preflight_accepts_verified_lower_bound_and_keeps_recommendation(tmp_path):
    hub_root = isolated_hub(tmp_path)
    outputs = {
        "blender": "Blender 4.4.0",
        "uvx": "uvx 0.8.19",
        "bambu-studio": "BambuStudio 02.07.01.62",
        "prusa-slicer-console": "PrusaSlicer 2.9.6",
        "orcaslicer": "OrcaSlicer 2.4.2",
    }
    checks = MODULE.inspect_environment(
        hub_root,
        which=lambda executable: executable,
        runner=lambda command: outputs[Path(command[0]).name],
    )
    blender = next(check for check in checks if check.id == "app.blender")

    assert blender.status == "compatible"
    assert blender.version_requirement == ">=4.4.0"
    assert blender.recommended_version == "5.1.2"

    guide = (
        hub_root
        / "workflows"
        / "3d-printing"
        / "outputs"
        / "run"
        / "INSTALLATION-GUIDE.md"
    )
    MODULE.write_installation_guide(
        hub_root, guide, checks, host="codex", language="zh-CN"
    )
    chinese = guide.read_text(encoding="utf-8")
    assert "最低兼容版本：`>=4.4.0`" in chinese
    assert "推荐安装版本：`5.1.2`" in chinese

    english_guide = guide.parent / "english" / "INSTALLATION-GUIDE.md"
    MODULE.write_installation_guide(
        hub_root, english_guide, checks, host="codex", language="en"
    )
    english = english_guide.read_text(encoding="utf-8")
    assert "Minimum compatible version: `>=4.4.0`" in english
    assert "Recommended installation version: `5.1.2`" in english


def test_preflight_reports_locked_workspace_mcp_source(tmp_path):
    hub_root = isolated_hub(tmp_path)
    source = hub_root / "workspace" / "shared" / "mcp" / "blender-mcp"
    source.mkdir(parents=True)
    outputs = {
        "blender": "Blender 4.4.0",
        "uvx": "uvx 0.8.19",
        "bambu-studio": "BambuStudio 02.07.01.62",
        "prusa-slicer-console": "PrusaSlicer 2.9.6",
        "orcaslicer": "OrcaSlicer 2.4.2",
    }
    locked_commit = "6e99eb5a442b83766a5796975ec7bb5bfc791341"

    def runner(command):
        if Path(command[0]).name == "git":
            return locked_commit
        return outputs[Path(command[0]).name]

    checks = MODULE.inspect_environment(
        hub_root,
        which=lambda executable: executable,
        runner=runner,
    )
    mcp = next(check for check in checks if check.id == "mcp.blender")

    assert mcp.status == "host-configuration-required"
    assert "workspace source verified at locked commit" in mcp.detail


def test_preflight_cli_returns_success_for_local_dependencies_pending_host_smoke(
    tmp_path, monkeypatch
):
    outputs = {
        "blender": "Blender 5.1.2",
        "uvx": "uvx 0.8.19",
        "bambu-studio": "BambuStudio 02.07.01.62",
        "prusa-slicer-console": "PrusaSlicer 2.9.6",
        "orcaslicer": "OrcaSlicer 2.4.2",
    }
    checks = MODULE.inspect_environment(
        Path.cwd(),
        which=lambda executable: executable,
        runner=lambda command: (
            "6e99eb5a442b83766a5796975ec7bb5bfc791341"
            if Path(command[0]).name == "git"
            else outputs[Path(command[0]).name]
        ),
    )
    hub_root = isolated_hub(tmp_path)
    guide = (
        hub_root
        / "workflows"
        / "3d-printing"
        / "outputs"
        / "run"
        / "INSTALLATION-GUIDE.md"
    )
    monkeypatch.setattr(
        MODULE, "inspect_environment", lambda *_args, **_kwargs: checks
    )

    result = MODULE.main(
        [
            "--hub-root",
            str(hub_root),
            "--guide",
            str(guide),
            "--language",
            "en",
        ]
    )

    assert result == 0
    assert not guide.exists()


def test_preflight_writes_localized_user_installation_guidance_only_to_explicit_path(tmp_path):
    hub_root = isolated_hub(tmp_path)
    checks = MODULE.inspect_environment(
        hub_root,
        which=lambda _: None,
        runner=lambda _: "",
    )
    guide = (
        hub_root
        / "workflows"
        / "3d-printing"
        / "outputs"
        / "preflight"
        / "INSTALLATION-GUIDE.md"
    )

    MODULE.write_installation_guide(
        hub_root, guide, checks, host="codex", language="zh-CN"
    )

    text = guide.read_text(encoding="utf-8")
    assert "Agent 可将锁定提交的 MCP 源码克隆" in text
    assert "Agent 不会安装桌面软件" in text
    assert "`app.blender`：missing" in text
    assert "blender-5.1.2-windows-x64.zip" in text
    assert "345bedea7b0acf7cc9666423d8553f9129622aea34ded65c23e8cb70f83f14ff" in text
    assert "锁定提交" in text
    assert "不要从本指南运行系统包管理器、安装程序或 Git 克隆。" in text


def test_preflight_rejects_guide_outside_selected_workflow_output(tmp_path):
    hub_root = isolated_hub(tmp_path)
    checks = MODULE.inspect_environment(
        hub_root,
        which=lambda _: None,
        runner=lambda _: "",
    )

    with pytest.raises(ValueError, match="3d-printing outputs"):
        MODULE.write_installation_guide(
            hub_root,
            tmp_path / "INSTALLATION-GUIDE.md",
            checks,
            host="codex",
            language="en",
        )


def test_preflight_rejects_linked_output_ancestor_before_path_resolution(
    tmp_path, monkeypatch
):
    hub_root = isolated_hub(tmp_path)
    output_root = hub_root / "workflows" / "3d-printing" / "outputs"
    output_root.mkdir(parents=True)
    candidate = output_root / "run" / "INSTALLATION-GUIDE.md"

    monkeypatch.setattr(
        MODULE,
        "_is_link_or_reparse",
        lambda path: path == output_root,
    )

    with pytest.raises(ValueError, match="links or reparse"):
        MODULE._assert_unlinked_output_path(candidate, output_root, hub_root)

def test_headless_dependency_readiness_does_not_require_mcp_host_setup():
    outputs = {
        "blender": "Blender 4.4.0",
        "uvx": "uvx 0.8.19",
        "bambu-studio": "BambuStudio 02.07.01.62",
        "prusa-slicer-console": "PrusaSlicer 2.9.6",
        "orcaslicer": "OrcaSlicer 2.4.2",
    }
    checks = MODULE.inspect_environment(
        Path.cwd(),
        which=lambda executable: None if executable == "uvx" else executable,
        runner=lambda command: outputs[Path(command[0]).name],
    )

    assert next(check for check in checks if check.id == "mcp.blender").status == "missing"
    assert MODULE.has_local_dependencies(checks)


def test_bambu_existing_install_can_be_checked_by_explicit_absolute_path(tmp_path):
    hub_root = isolated_hub(tmp_path)
    bambu = tmp_path / "Bambu Studio.exe"
    bambu.write_bytes(b"existing-install")
    outputs = {
        "Bambu Studio.exe": "BambuStudio 02.07.01.62",
        "blender": "Blender 4.4.0",
        "prusa-slicer-console": "PrusaSlicer 2.9.6",
        "orcaslicer": "OrcaSlicer 2.4.2",
    }

    checks = MODULE.inspect_environment(
        hub_root,
        tool_paths={"app.bambu-studio": bambu},
        which=lambda executable: None if executable == "uvx" else executable,
        runner=lambda command: outputs[Path(command[0]).name],
    )
    bambu_check = next(check for check in checks if check.id == "app.bambu-studio")

    assert bambu_check.executable == str(bambu)
    assert bambu_check.status == "automation-smoke-required"
    assert "2.7.1.62" in bambu_check.detail


def test_split_dependencies_require_bambu_provider_smoke():
    outputs = {
        "blender": "Blender 4.4.0",
        "uvx": "uvx 0.8.19",
        "bambu-studio": "BambuStudio 02.07.01.62",
        "prusa-slicer-console": "PrusaSlicer 2.9.6",
        "orcaslicer": "OrcaSlicer 2.4.2",
    }
    checks = MODULE.inspect_environment(
        Path.cwd(),
        which=lambda executable: None if executable == "uvx" else executable,
        runner=lambda command: outputs[Path(command[0]).name],
    )

    assert not MODULE.has_split_dependencies(checks)
    assert next(
        check for check in checks if check.id == "app.bambu-studio"
    ).status == "automation-smoke-required"


def test_preflight_rejects_traversal_outside_workflow_outputs(tmp_path):
    hub_root = isolated_hub(tmp_path)
    output_root = hub_root / "workflows" / "3d-printing" / "outputs"
    candidate = output_root / ".." / ".." / "outside" / "INSTALLATION-GUIDE.md"
    checks = MODULE.inspect_environment(
        hub_root,
        which=lambda _: None,
        runner=lambda _: "",
    )

    with pytest.raises(ValueError, match="3d-printing outputs"):
        MODULE.write_installation_guide(
            hub_root, candidate, checks, host="codex", language="en"
        )
