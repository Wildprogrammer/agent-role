from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_workflow_hub.specialized_agent_deployment.contracts import (
    DeploymentRequest,
    HostFacts,
    SkillFile,
    SkillSelection,
    SkillSnapshot,
    canonical_sha256,
)
from agent_workflow_hub.specialized_agent_deployment.hosts.deepseek_harness import (
    SUPPORTED_DSH,
    DeepSeekHarnessAdapter,
    DeepSeekHarnessAdapterError,
    DeepSeekHarnessEvidence,
    WebBehaviorEvidence,
    patch_standard_agent_template,
    _patch_mcp_servers,
    validate_web_behavior_evidence,
)
from agent_workflow_hub.specialized_agent_deployment.planning import (
    build_deployment_plan,
    planned_manifest,
)
from agent_workflow_hub.specialized_agent_deployment.rendering import render_persona
from agent_workflow_hub.specialized_agent_deployment.runner import CommandResult
from agent_workflow_hub.specialized_agent_deployment.hosts.base import ApplyContext, VerifyContext


AGENT_TEMPLATE = """# keep-comment
- id: persona
  name: '@deepseek-ai/dsh-persona'
  config:
    text: >-
      Original persona.

- id: tool-pwsh
  name: '@deepseek-ai/dsh-tool-pwsh'
  disabled: !!js process.platform !== 'win32'

- id: skill-filesystem
  name: '@deepseek-ai/dsh-skill-filesystem'

- id: tool-skill
  name: '@deepseek-ai/dsh-tool-skill'
"""
PRESET_TEMPLATE = "name: Standard\ndescription: Standard agent\norder: 1\n"


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def run(self, argv: tuple[str, ...], *, phase: str) -> CommandResult:
        self.calls.append((argv, phase))
        return self.responses[argv]


def command_result(argv: tuple[str, ...], stdout: str) -> CommandResult:
    return CommandResult(argv=argv, exit_code=0, stdout=stdout, stderr="")


def write_runtime(tmp_path: Path, *, with_build: bool = True):
    runtime = (tmp_path / "deepseek-harness").resolve()
    agent_path = runtime / "packages/preset/agent-presets/presets/standard/agent.cordis.yml"
    preset_path = runtime / "packages/preset/agent-presets/presets/standard/preset.yml"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_bytes(AGENT_TEMPLATE.encode("utf-8"))
    preset_path.write_bytes(PRESET_TEMPLATE.encode("utf-8"))
    (runtime / "package.json").write_text(
        json.dumps({"version": "0.1.2-alpha.2"}), encoding="utf-8"
    )
    if with_build:
        for relative in (
            "apps/cli/lib/bin.js",
            "packages/bundle/base/lib/index.js",
            "packages/bundle/web-app/lib/index.js",
            "apps/web/dist/index.html",
        ):
            path = runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("built", encoding="utf-8")
    dsh_home = (tmp_path / ".dsh").resolve()
    profile = dsh_home / "profiles/web/package.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps(
            {
                "dsh": {
                    "profile": {
                        "bundles": [
                            "@deepseek-ai/dsh-base",
                            "@deepseek-ai/dsh-web-app",
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    evidence = DeepSeekHarnessEvidence(
        version="0.1.2-alpha.2",
        commit="0a53fb55bea101816fa226bb964ae2bed71c343b",
        agent_template_sha256=hashlib.sha256(AGENT_TEMPLATE.encode()).hexdigest(),
        preset_template_sha256=hashlib.sha256(PRESET_TEMPLATE.encode()).hexdigest(),
    )
    return runtime, dsh_home, evidence


def request(tmp_path: Path, runtime: Path, dsh_home: Path) -> DeploymentRequest:
    return DeploymentRequest(
        schema_version="1.0",
        deployment_id="dsh-fixture",
        agent_id="fixture-agent",
        display_name="Fixture Agent",
        purpose="test deployment",
        host="deepseek-harness",
        mode="create",
        primary_workflow="primary-flow",
        related_workflows=(),
        auxiliary_skills=(),
        workdir=str((tmp_path / "work").resolve()),
        config_refs=(),
        host_options={
            "dsh_home": str(dsh_home),
            "runtime_root": str(runtime),
            "profile": "web",
        },
    )


def runner_for(
    runtime: Path,
    commit: str = "0a53fb55bea101816fa226bb964ae2bed71c343b",
    dirty: str = "",
) -> FakeRunner:
    commands = {
        ("git", "-C", str(runtime), "remote", "get-url", "origin"):
            "https://github.com/deepseek-ai/deepseek-harness.git",
        ("git", "-C", str(runtime), "rev-parse", "HEAD"): commit,
        ("git", "-C", str(runtime), "status", "--porcelain"): dirty,
    }
    return FakeRunner({argv: command_result(argv, output) for argv, output in commands.items()})


def one_snapshot() -> tuple[SkillSnapshot, ...]:
    content = b"---\nname: primary-flow\ndescription: fixture\n---\n# Use\n"
    file = SkillFile(
        relative_path="SKILL.md",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    selection = SkillSelection(
        name="primary-flow",
        source_kind="hub-workflow",
        source="workflows/primary-flow",
        reason="primary",
    )
    return (
        SkillSnapshot(
            selection=selection,
            files=(file,),
            tree_sha256=canonical_sha256([file.to_mapping()]),
        ),
    )


def test_supported_constant_matches_locked_evidence() -> None:
    assert SUPPORTED_DSH.version == "0.1.2-alpha.2"
    assert SUPPORTED_DSH.commit == "0a53fb55bea101816fa226bb964ae2bed71c343b"
    assert SUPPORTED_DSH.agent_template_sha256 == "f04fbc6ec6d38aab78f18690c293ddcb76293107f7e6cd157904b7c0e83094bd"
    assert SUPPORTED_DSH.preset_template_sha256 == "3c61b4ce68e5dd5cb2c099693fdcb30b91d5f22bbbef546e233321b0fa68f0e4"


def test_discover_rejects_superseded_template_location(tmp_path: Path) -> None:
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    current = runtime / "packages/preset/agent-presets/presets/standard"
    superseded = runtime / "apps/cli/config/agent-presets/standard"
    superseded.mkdir(parents=True)
    for name in ("agent.cordis.yml", "preset.yml"):
        current.joinpath(name).replace(superseded / name)

    facts = DeepSeekHarnessAdapter(
        runner_for(runtime), evidence=evidence
    ).discover(request(tmp_path, runtime, dsh_home))

    assert facts.compatibility == "missing"
    assert str(current / "agent.cordis.yml") in facts.facts["missing_evidence"]


def test_discover_accepts_only_exact_clean_evidence(tmp_path: Path) -> None:
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    runner = runner_for(runtime)
    facts = DeepSeekHarnessAdapter(runner, evidence=evidence).discover(
        request(tmp_path, runtime, dsh_home)
    )
    assert facts.compatibility == "verified"
    assert all(argv[0] == "git" and phase == "preview" for argv, phase in runner.calls)

    dirty = DeepSeekHarnessAdapter(
        runner_for(runtime, dirty=" M package.json"), evidence=evidence
    ).discover(request(tmp_path, runtime, dsh_home))
    assert dirty.compatibility == "unverified"


def test_missing_build_returns_guidance_without_package_manager(tmp_path: Path) -> None:
    runtime, dsh_home, evidence = write_runtime(tmp_path, with_build=False)
    runner = runner_for(runtime)
    facts = DeepSeekHarnessAdapter(runner, evidence=evidence).discover(
        request(tmp_path, runtime, dsh_home)
    )
    assert facts.compatibility == "compatible_not_runnable"
    assert facts.facts["missing_build_artifacts"]
    assert not any(argv[0] in {"pnpm", "npm", "npx"} for argv, _ in runner.calls)


def test_missing_runtime_returns_guidance_instead_of_crashing(tmp_path: Path) -> None:
    runtime = (tmp_path / "missing-runtime").resolve()
    dsh_home = (tmp_path / ".dsh").resolve()
    requested = request(tmp_path, runtime, dsh_home)
    facts = DeepSeekHarnessAdapter(
        FakeRunner({}), evidence=SUPPORTED_DSH
    ).discover(requested)
    assert facts.compatibility == "missing"
    assert "guidance" in facts.facts


def test_patch_replaces_only_persona_and_skill_filesystem(tmp_path: Path) -> None:
    patched = patch_standard_agent_template(
        AGENT_TEMPLATE,
        persona="第一行\n第二行",
        provider_name="fixture-provider",
        skill_root=(tmp_path / "skills").resolve(),
    )
    assert "# keep-comment" in patched
    assert "!!js process.platform !== 'win32'" in patched
    assert "Original persona" not in patched
    assert "第一行" in patched and "第二行" in patched
    assert "providerName: fixture-provider" in patched
    assert "includeDefaultRoots: false" in patched
    assert "watch: false" in patched
    assert str((tmp_path / "skills").resolve()).replace("\\", "\\\\") in patched


def test_plan_isolates_skill_root_and_refuses_target_conflict(tmp_path: Path) -> None:
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    requested = request(tmp_path, runtime, dsh_home)
    adapter = DeepSeekHarnessAdapter(runner_for(runtime), evidence=evidence)
    facts = adapter.discover(requested)
    intents = adapter.plan_writes(
        requested,
        one_snapshot(),
        render_persona(requested, one_snapshot()),
        facts,
    )
    agent_intent = next(item for item in intents if Path(item.target).name == "agent.cordis.yml")
    rendered = patch_standard_agent_template(
        AGENT_TEMPLATE,
        persona=render_persona(requested, one_snapshot()),
        provider_name=f"agent-workflow-hub-{requested.agent_id}",
        skill_root=dsh_home / ".agent-presets" / requested.agent_id / "skills",
    )
    assert agent_intent.content_sha256 == hashlib.sha256(rendered.encode()).hexdigest()
    assert agent_intent.parameters["staging_relative"] == "host-files/agent.cordis.yml"

    (dsh_home / ".agent-presets" / requested.agent_id).mkdir(parents=True)
    with pytest.raises(DeepSeekHarnessAdapterError):
        adapter.plan_writes(
            requested,
            one_snapshot(),
            render_persona(requested, one_snapshot()),
            facts,
        )


def test_web_behavior_evidence_controls_full_verification(tmp_path: Path) -> None:
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    requested = request(tmp_path, runtime, dsh_home)
    adapter = DeepSeekHarnessAdapter(runner_for(runtime), evidence=evidence)
    facts = adapter.discover(requested)
    plan = build_deployment_plan(
        requested,
        one_snapshot(),
        facts,
        adapter.plan_writes(
            requested,
            one_snapshot(),
            render_persona(requested, one_snapshot()),
            facts,
        ),
    )
    manifest = planned_manifest(plan)
    missing = validate_web_behavior_evidence(None, manifest)
    assert missing.status == "partially_verified"
    evidence_value = WebBehaviorEvidence(
        session_id="session-1",
        preset_id=requested.agent_id,
        prompt_sha256=adapter.behavior_prompt_sha256(requested),
        response_sha256="a" * 64,
        identity=requested.agent_id,
        primary_workflow=requested.primary_workflow,
        first_action="完整加载主工作流",
    )
    assert validate_web_behavior_evidence(evidence_value, manifest).status == "verified"
    invalid_action = WebBehaviorEvidence(
        session_id="session-1",
        preset_id=requested.agent_id,
        prompt_sha256=adapter.behavior_prompt_sha256(requested),
        response_sha256="a" * 64,
        identity=requested.agent_id,
        primary_workflow=requested.primary_workflow,
        first_action="做其他事情",
    )
    assert validate_web_behavior_evidence(invalid_action, manifest).status == "failed"


def mcp_request(tmp_path, runtime, dsh_home):
    original = request(tmp_path, runtime, dsh_home)
    ini = (tmp_path / "service.ini").resolve()
    ini.write_text("[fixture]\n", encoding="utf-8")
    plugin = runtime / "packages/mcp/mcp-client/lib/index.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("built", encoding="utf-8")
    return replace(original, config_refs=(str(ini),), host_options={
        **original.host_options,
        "mcp_servers": [{
            "workflow": "primary-flow", "server_name": "fixture",
            "command": sys.executable,
            "args": ["-m", "agent_workflow_hub.cli", "jenkins-mcp", str(ini)],
            "cwd": str(tmp_path.resolve()),
        }],
    })


def test_mcp_mapping_is_in_plan_and_applied_preset(tmp_path):
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    requested = mcp_request(tmp_path, runtime, dsh_home)
    adapter = DeepSeekHarnessAdapter(runner_for(runtime), evidence=evidence)
    facts = adapter.discover(requested)
    assert facts.compatibility == "verified"
    snapshots = one_snapshot()
    persona = render_persona(requested, snapshots)
    writes = adapter.plan_writes(requested, snapshots, persona, facts)
    plan = build_deployment_plan(requested, snapshots, facts, writes)
    manifest = planned_manifest(plan)
    staging = tmp_path / "staging"
    skill = staging / "host-files/skills/primary-flow/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"---\nname: primary-flow\ndescription: fixture\n---\n# Use\n")
    assert not Path(facts.target_root).exists()  # preview must not write host
    adapter.apply(ApplyContext(plan=plan, manifest=manifest,
                               staging_root=staging, backup_root=tmp_path / "backups"))
    target = Path(facts.target_root) / "agent.cordis.yml"
    text = target.read_text(encoding="utf-8")
    assert "@deepseek-ai/dsh-mcp-client" in text
    assert '"serverName": "fixture"' in text
    assert "jenkins-mcp" in text
    assert "!!js process.platform" in text
    verification = adapter.verify(VerifyContext(manifest=manifest,
                                                staging_root=staging,
                                                behavior_evidence_path=None))
    assert verification.static["status"] == "passed"
    assert verification.status == "partially_verified"
    assert verification.discovery["mcp"]["status"] == "configured_not_probed"
    # A missing/changed bridge must not be hidden by identity verification.
    target.write_text(text.replace('"serverName": "fixture"', '"serverName": "lost"'), encoding="utf-8")
    assert adapter.verify(VerifyContext(manifest=manifest, staging_root=staging,
                                       behavior_evidence_path=None)).status == "failed"


def test_standalone_preset_renders_private_runtime_not_development_path(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import runtime_python
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    requested = replace(mcp_request(tmp_path, runtime, dsh_home), runtime={
        "python": sys._base_executable, "wheelhouse": str(tmp_path / "wheels"),
        "destination": str(tmp_path / "release")})
    adapter = DeepSeekHarnessAdapter(runner_for(runtime), evidence=evidence)
    facts = adapter.discover(requested)
    persona = render_persona(requested, one_snapshot())
    writes = adapter.plan_writes(requested, one_snapshot(), persona, facts)
    plan = build_deployment_plan(requested, one_snapshot(), facts, writes)
    context = ApplyContext(plan=plan, manifest=planned_manifest(plan), staging_root=tmp_path / "staging", backup_root=tmp_path / "backup")
    intent = next(w for w in writes if w.parameters.get("payload_kind") == "agent-template")
    content = adapter._generated_content(intent, context)
    assert hashlib.sha256(content).hexdigest() == intent.content_sha256
    assert json.dumps(runtime_python(requested.runtime)).encode() in content
    assert str(tmp_path / "release/hub") in persona


def test_system_source_preset_uses_system_python_and_launcher(tmp_path):
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    requested = replace(mcp_request(tmp_path, runtime, dsh_home), runtime={
        "mode": "system-source", "python": str(Path(sys._base_executable).resolve()),
        "wheelhouse": str(tmp_path / "wheels"),
        "destination": str(tmp_path / "system-release")})
    adapter = DeepSeekHarnessAdapter(runner_for(runtime), evidence=evidence)
    facts = adapter.discover(requested)
    persona = render_persona(requested, one_snapshot())
    writes = adapter.plan_writes(requested, one_snapshot(), persona, facts)
    plan = build_deployment_plan(requested, one_snapshot(), facts, writes)
    context = ApplyContext(plan=plan, manifest=planned_manifest(plan), staging_root=tmp_path / "staging", backup_root=tmp_path / "backup")
    intent = next(w for w in writes if w.parameters.get("payload_kind") == "agent-template")
    content = adapter._generated_content(intent, context).decode("utf-8")
    assert json.dumps(requested.runtime["python"]) in content
    assert json.dumps(str(Path(requested.runtime["destination"]) / "run-workflow-hub.py")) in content
    assert "venv" not in content
    assert "系统 Python" in persona


@pytest.mark.parametrize("change", ["duplicate", "unselected", "relative", "inline-env"])
def test_invalid_mcp_bindings_are_rejected(tmp_path, change):
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    requested = mcp_request(tmp_path, runtime, dsh_home)
    data = requested.to_mapping()
    server = data["host_options"]["mcp_servers"][0]
    if change == "duplicate":
        data["host_options"]["mcp_servers"].append(dict(server))
    elif change == "unselected":
        server["workflow"] = "not-selected"
    elif change == "relative":
        server["command"] = "python"
    else:
        server["env"] = {"TOKEN": "do-not-copy"}
    with pytest.raises(DeepSeekHarnessAdapterError):
        DeepSeekHarnessAdapter(runner_for(runtime), evidence=evidence).discover(
            DeploymentRequest.from_mapping(data))


def test_mcp_plugin_missing_is_not_runnable(tmp_path):
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    requested = mcp_request(tmp_path, runtime, dsh_home)
    (runtime / "packages/mcp/mcp-client/lib/index.js").unlink()
    facts = DeepSeekHarnessAdapter(runner_for(runtime), evidence=evidence).discover(requested)
    assert facts.compatibility == "compatible_not_runnable"
    assert "packages/mcp/mcp-client/lib/index.js" in facts.facts["missing_build_artifacts"]


def test_update_preserves_user_plugins(tmp_path):
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    requested = request(tmp_path, runtime, dsh_home)
    target = dsh_home / ".agent-presets" / requested.agent_id
    target.mkdir(parents=True)
    custom = AGENT_TEMPLATE + "\n- id: user-plugin\n  name: user-owned-plugin\n"
    (target / "agent.cordis.yml").write_bytes(custom.encode("utf-8"))
    (target / "preset.yml").write_text(PRESET_TEMPLATE, encoding="utf-8")
    (target / ".agent-workflow-hub-deployment.json").write_text(json.dumps({
        "schema_version": "1.0", "deployment_id": requested.deployment_id,
        "agent_id": requested.agent_id, "host": requested.host,
    }), encoding="utf-8")
    requested = replace(requested, mode="update")
    adapter = DeepSeekHarnessAdapter(runner_for(runtime), evidence=evidence)
    facts = adapter.discover(requested)
    writes = adapter.plan_writes(requested, one_snapshot(), "persona", facts)
    agent = next(item for item in writes if Path(item.target).name == "agent.cordis.yml")
    expected = patch_standard_agent_template(custom, persona="persona",
        provider_name=f"agent-workflow-hub-{requested.agent_id}", skill_root=target / "skills")
    assert agent.content_sha256 == hashlib.sha256(expected.encode()).hexdigest()


def test_preview_reports_selected_mcp_workflow_without_binding(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.service import DeploymentService
    runtime, dsh_home, evidence = write_runtime(tmp_path)
    requested = request(tmp_path, runtime, dsh_home)
    skill = tmp_path / "workflows/primary-flow/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: primary-flow\ndescription: fixture\nmetadata:\n"
                    "  entrypoints: '{\"mcp\":\"workflow-hub fixture-mcp <ABSOLUTE_INI>\"}'\n"
                    "---\n# Fixture\n", encoding="utf-8")
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(requested.to_mapping()), encoding="utf-8")
    service = DeploymentService(adapter_factory=lambda request, staging:
        DeepSeekHarnessAdapter(runner_for(runtime), evidence=evidence))
    manifest = service.preview(tmp_path.resolve(), request_path.resolve())
    assert manifest.status == "guidance_only"
    assert manifest.host_facts.facts["missing_mcp_workflows"] == ("primary-flow",)
    assert not (dsh_home / ".agent-presets" / requested.agent_id).exists()


def test_mcp_patch_is_idempotent_and_preserves_unselected_bridge(tmp_path):
    server = {"workflow": "primary-flow", "server_name": "fixture", "command": sys.executable,
              "args": ["-m", "example"], "cwd": str(tmp_path.resolve())}
    original = AGENT_TEMPLATE + "\n- id: user-mcp\n  name: '@deepseek-ai/dsh-mcp-client'\n  config:\n    serverName: other\n"
    once = _patch_mcp_servers(original, (server,))
    twice = _patch_mcp_servers(once, (server,))
    assert once == twice
    assert "serverName: other" in twice
    assert twice.count("- id: hub-mcp-fixture") == 1


def test_mcp_namespace_conflict_does_not_replace_user_row(tmp_path):
    server = {"workflow": "primary-flow", "server_name": "fixture", "command": sys.executable,
              "args": [], "cwd": str(tmp_path.resolve())}
    source = AGENT_TEMPLATE + "\n- id: user-mcp\n  name: '@deepseek-ai/dsh-mcp-client'\n  config:\n    serverName: fixture\n"
    with pytest.raises(DeepSeekHarnessAdapterError, match="namespace"):
        _patch_mcp_servers(source, (server,))
