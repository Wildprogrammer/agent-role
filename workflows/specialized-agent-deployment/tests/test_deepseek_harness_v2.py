from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_workflow_hub.specialized_agent_deployment.contracts import DeploymentRequest, SkillSelection
from agent_workflow_hub.specialized_agent_deployment.hosts import deepseek_harness as adapter_module
from agent_workflow_hub.specialized_agent_deployment.hosts.base import ApplyContext, VerifyContext
from agent_workflow_hub.specialized_agent_deployment.hosts.deepseek_harness import (
    DeepSeekHarnessAdapter,
    DeepSeekHarnessAdapterError,
)
from agent_workflow_hub.specialized_agent_deployment.hosts.deepseek_harness_v2 import (
    extract_v2_preset,
    merge_v2_preset,
    render_v2_preset_patch,
    v2_preset_conflicts,
)
from agent_workflow_hub.specialized_agent_deployment.planning import (
    build_deployment_plan,
    planned_manifest,
)
from agent_workflow_hub.specialized_agent_deployment.rendering import render_persona
from agent_workflow_hub.specialized_agent_deployment.sources import snapshot_skill


STANDARD = """- insert:
    - id: preset-standard
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: standard
        order: 1
        plugins:
          - id: persona
            name: '@deepseek-ai/dsh-persona'
            config:
              prefix: Original
          - id: agent-instructions
            name: '@deepseek-ai/dsh-agent-instructions'
          - id: time-context
            name: '@deepseek-ai/dsh-time-context'
          - id: tool-schedule
            name: '@deepseek-ai/dsh-tool-schedule'
          - id: skill-filesystem
            name: '@deepseek-ai/dsh-skill-filesystem'
          - id: tool-skill
            name: '@deepseek-ai/dsh-tool-skill'
          - id: tool-subagent-control
            name: '@deepseek-ai/dsh-tool-subagent-control'
"""


class NoopRunner:
    def run(self, argv, *, phase):  # pragma: no cover - v2 discovery uses no commands
        raise AssertionError((argv, phase))


def _request(tmp_path: Path, *, schedule: bool = False) -> DeploymentRequest:
    return DeploymentRequest(
        schema_version="1.0",
        deployment_id="dsh-v2-fixture",
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
            "dsh_home": str((tmp_path / ".dsh").resolve()),
            "profile": "web",
            "schedule": {"enabled": schedule},
        },
    )


def _write_profile(tmp_path: Path, version: str) -> Path:
    home = (tmp_path / ".dsh").resolve()
    modules = home / "profiles/node_modules/@deepseek-ai"
    packages = (
        "dsh", "dsh-base", "dsh-web-app", "dsh-persona",
        "dsh-agent-instructions", "dsh-skill-filesystem", "dsh-tool-skill",
        "dsh-time-context", "dsh-tool-schedule",
    )
    if version == "0.2.2-rc.1":
        packages += ("dsh-agent-preset", "dsh-agent-preset-registry", "dsh-tool-subagent-control")
    for name in packages:
        root = modules / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "package.json").write_text(
            json.dumps({
                "name": f"@deepseek-ai/{name}", "version": version,
                **({"exports": {".": "./lib/index.js"}} if name == "dsh-tool-subagent-control" else {}),
            }),
            encoding="utf-8",
        )
    standard = modules / "dsh-web-app/presets/standard.patch.yml"
    standard.parent.mkdir(parents=True, exist_ok=True)
    standard.write_text(STANDARD, encoding="utf-8")
    web = home / "profiles/web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "package.json").write_text(
        json.dumps({"dsh": {"profile": {
            "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"],
        }}}),
        encoding="utf-8",
    )
    (web / "cordis.patch.yml").write_text("# local\n[]\n", encoding="utf-8")
    return home


def test_render_and_merge_preserve_js_and_toggle_schedule(tmp_path: Path) -> None:
    rendered = render_v2_preset_patch(
        STANDARD + "# !!js preserved\n",
        agent_id="fixture-agent",
        display_name="Fixture Agent",
        purpose="fixture",
        persona="Line one\nLine two",
        provider_name="agent-workflow-hub-fixture-agent",
        skill_root=(tmp_path / "skills").resolve(),
        schedule_enabled=False,
    )

    assert "preset-fixture-agent" in rendered
    assert "name: \"Fixture Agent\"" in rendered
    assert "Line one" in rendered and "Line two" in rendered
    assert "tool-schedule" not in rendered and "time-context" not in rendered
    assert "!!js preserved" in rendered
    merged = merge_v2_preset("# local\n", rendered, agent_id="fixture-agent")
    assert extract_v2_preset(merged, agent_id="fixture-agent") is not None
    assert merge_v2_preset(merged, rendered, agent_id="fixture-agent") == merged
    assert merge_v2_preset("# local\n[]\n", rendered, agent_id="fixture-agent").startswith(
        "# local\n\n- insert:\n"
    )
    with_comment = merged + "\n# preserve this comment\n- id: other\n  disabled: true\n"
    assert merge_v2_preset(with_comment, rendered, agent_id="fixture-agent") == with_comment
    assert v2_preset_conflicts(
        '- insert:\n  - id: "other-id"\n    config: {id: "fixture-agent"}\n',
        agent_id="fixture-agent",
    )


@pytest.mark.parametrize("version", ["0.2.1-rc.5", "0.2.2-rc.1"])
def test_v2_discovery_apply_and_verify_on_mixed_newlines(
    tmp_path: Path, monkeypatch, version: str
) -> None:
    home = _write_profile(tmp_path, version)
    profile_patch = home / "profiles/web/cordis.patch.yml"
    original_patch = b"# local\r\n- id: unrelated\r\n  disabled: true\r\n"
    profile_patch.write_bytes(original_patch)
    monkeypatch.setitem(
        adapter_module.DSH_V2_STANDARD_PATCH_SHA256_BY_VERSION,
        version,
        hashlib.sha256(
            (
                home
                / "profiles/node_modules/@deepseek-ai/dsh-web-app/presets/standard.patch.yml"
            ).read_bytes()
        ).hexdigest(),
    )
    requested = _request(tmp_path, schedule=True)
    adapter = DeepSeekHarnessAdapter(NoopRunner())

    facts = adapter.discover(requested)
    assert facts.compatibility == "verified"
    assert facts.version == version
    assert facts.facts["generation"] == "profile-v2"
    skill = tmp_path / "primary-flow/SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: primary-flow\ndescription: Fixture\n---\n# Fixture\n", encoding="utf-8")
    snapshots = (snapshot_skill(tmp_path, SkillSelection(
        name="primary-flow", source_kind="external-skill", source=str(skill.parent),
        reason="fixture",
    )),)
    writes = adapter.plan_writes(requested, snapshots, render_persona(requested, snapshots), facts)
    assert {item.parameters.get("payload_kind") for item in writes} >= {
        "v2-preset", "v2-profile-patch", "marker"
    }
    assert profile_patch.read_bytes() == original_patch
    plan = build_deployment_plan(requested, snapshots, facts, writes)
    manifest = planned_manifest(plan)
    staging = tmp_path / "staging"
    staged_skill = staging / "host-files/skills/primary-flow/SKILL.md"
    staged_skill.parent.mkdir(parents=True)
    staged_skill.write_bytes(skill.read_bytes())
    adapter.apply(ApplyContext(
        plan=plan, manifest=manifest, staging_root=staging, backup_root=tmp_path / "backups",
    ))
    actual_patch = profile_patch.read_bytes()
    assert actual_patch.startswith(original_patch)
    assert b"name: '@deepseek-ai/dsh-tool-subagent-control'" in actual_patch
    assert b"dsh-tool-subagent-control/list-agents" not in actual_patch
    assert adapter.verify(VerifyContext(
        manifest=manifest, staging_root=staging, behavior_evidence_path=None,
    )).status == "partially_verified"
    profile_patch.write_bytes(actual_patch + b"\r\n# user note\r\n")
    assert adapter.verify(VerifyContext(
        manifest=manifest, staging_root=staging, behavior_evidence_path=None,
    )).status == "partially_verified"
    update = replace(requested, mode="update")
    assert adapter.plan_writes(update, snapshots, plan.persona, adapter.discover(update))
    profile_patch.write_bytes(actual_patch.replace(b"Fixture Agent", b"External edit"))
    with pytest.raises(DeepSeekHarnessAdapterError, match="differs from its managed copy"):
        adapter.plan_writes(update, snapshots, plan.persona, adapter.discover(update))
    if version == "0.2.2-rc.1":
        control = home / "profiles/node_modules/@deepseek-ai/dsh-tool-subagent-control/package.json"
        package = json.loads(control.read_text(encoding="utf-8"))
        package["exports"] = {"./list-agents": "./lib/list-agents.js"}
        control.write_text(json.dumps(package), encoding="utf-8")
        assert adapter.discover(requested).compatibility == "unverified"


@pytest.mark.parametrize("preset_id", ["standard", "fixture-agent"])
def test_v2_create_rejects_existing_registration(tmp_path: Path, monkeypatch, preset_id: str) -> None:
    version = "0.2.2-rc.1"
    home = _write_profile(tmp_path, version)
    standard = home / "profiles/node_modules/@deepseek-ai/dsh-web-app/presets/standard.patch.yml"
    monkeypatch.setitem(
        adapter_module.DSH_V2_STANDARD_PATCH_SHA256_BY_VERSION,
        version,
        hashlib.sha256(standard.read_bytes()).hexdigest(),
    )
    requested = replace(_request(tmp_path), agent_id=preset_id)
    if preset_id == "fixture-agent":
        bundle = home / "profiles/web/node_modules/example-bundle"
        bundle.mkdir(parents=True)
        (bundle / "package.json").write_text(
            '{"dsh":{"bundle":{"patch":"./cordis.patch.yml"}}}', encoding="utf-8",
        )
        (bundle / "cordis.patch.yml").write_text(
            '- insert:\n  - id: custom-id\n    config: {id: "fixture-agent"}\n',
            encoding="utf-8",
        )
        manifest = home / "profiles/web/package.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["dsh"]["profile"]["bundles"].append("example-bundle")
        manifest.write_text(json.dumps(data), encoding="utf-8")
    adapter = DeepSeekHarnessAdapter(NoopRunner())
    facts = adapter.discover(requested)
    assert facts.compatibility == "verified"
    with pytest.raises(DeepSeekHarnessAdapterError, match="already registers"):
        adapter.plan_writes(requested, (), "Fixture persona", facts)
