"""DeepSeek Harness 0.2 Profile patch helpers.

The 0.2 host registers agent presets from profile patch layers.  These helpers
preserve the upstream ``!!js`` expressions by editing the verified template as
text instead of round-tripping it through a YAML serializer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


DSH_V2_VERSION = "0.2.1-rc.5"
DSH_V2_STANDARD_PATCH_SHA256 = (
    "0d398d2a6872a73a106cb3bd19303ecccf1637e1fcad4d0bf9d8a72d2c2c68f5"
)
DSH_V2_STANDARD_PATCH_SHA256_BY_VERSION = {
    DSH_V2_VERSION: DSH_V2_STANDARD_PATCH_SHA256,
    # Observed from the installed 0.2.2-rc.1 Web bundle. This release also
    # needs the package-export compatibility gate in the host adapter.
    "0.2.2-rc.1": "3006cc3780cddaa7fb0089e5854e6bbef4409cf1d425930fd835226b95660afd",
}


class DeepSeekHarnessV2Error(RuntimeError):
    """Raised when a 0.2 Profile patch cannot be changed unambiguously."""


def _plugin_span(lines: list[str], plugin_id: str) -> tuple[int, int]:
    marker = f"          - id: {plugin_id}"
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == marker]
    if len(starts) != 1:
        raise DeepSeekHarnessV2Error(f"expected one {plugin_id} plugin row")
    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        text = lines[index].rstrip("\r\n")
        if text.startswith("          - id: ") or text.startswith("- "):
            end = index
            break
    return start, end


def _replace_plugin(lines: list[str], plugin_id: str, replacement: str) -> list[str]:
    start, end = _plugin_span(lines, plugin_id)
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    replacement_lines = [line + newline for line in replacement.rstrip("\n").split("\n")]
    return [*lines[:start], *replacement_lines, *lines[end:]]


def render_v2_preset_patch(
    template: str,
    *,
    agent_id: str,
    display_name: str,
    purpose: str,
    persona: str,
    provider_name: str,
    skill_root: Path,
    schedule_enabled: bool,
    mcp_servers: tuple[Mapping[str, object], ...] = (),
) -> str:
    """Render one 0.2 preset declaration from the verified standard patch."""

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", agent_id):
        raise DeepSeekHarnessV2Error("agent_id must be kebab-case")
    if not persona.strip() or not display_name.strip() or not purpose.strip():
        raise DeepSeekHarnessV2Error("preset identity fields must be nonblank")
    if not skill_root.is_absolute():
        raise DeepSeekHarnessV2Error("skill_root must be absolute")
    lines = template.splitlines(keepends=True)
    if sum(line.rstrip("\r\n") == "    - id: preset-standard" for line in lines) != 1:
        raise DeepSeekHarnessV2Error("unsupported DSH 0.2 standard patch shape")
    lines = [
        line.replace("    - id: preset-standard", f"    - id: preset-{agent_id}")
        for line in lines
    ]
    config_id = [
        index for index, line in enumerate(lines) if line.rstrip("\r\n") == "        id: standard"
    ]
    if len(config_id) != 1:
        raise DeepSeekHarnessV2Error("unsupported DSH 0.2 preset config")
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    index = config_id[0]
    lines[index:index + 1] = [
        f"        id: {agent_id}{newline}",
        f"        name: {json.dumps(display_name, ensure_ascii=False)}{newline}",
        f"        description: {json.dumps(purpose, ensure_ascii=False)}{newline}",
    ]
    persona_lines = "\n".join(f"                {line}" for line in persona.rstrip().splitlines())
    lines = _replace_plugin(
        lines,
        "persona",
        "          - id: persona\n"
        "            name: '@deepseek-ai/dsh-persona'\n"
        "            config:\n"
        "              prefix: |-\n"
        f"{persona_lines}",
    )
    lines = _replace_plugin(
        lines,
        "skill-filesystem",
        "          - id: skill-filesystem\n"
        "            name: '@deepseek-ai/dsh-skill-filesystem'\n"
        "            config:\n"
        f"              providerName: {provider_name}\n"
        "              includeDefaultRoots: false\n"
        "              customSkillDirs:\n"
        f"                - {json.dumps(str(skill_root), ensure_ascii=False)}\n"
        "              watch: false",
    )
    if not schedule_enabled:
        for plugin_id in ("time-context", "tool-schedule"):
            start, end = _plugin_span(lines, plugin_id)
            del lines[start:end]
    if mcp_servers:
        insertion, _ = _plugin_span(lines, "tool-skill")
        rows: list[str] = []
        for server in mcp_servers:
            config = {
                "serverName": server["server_name"],
                "transport": "stdio",
                "command": server["command"],
                "args": server["args"],
                "cwd": server["cwd"],
            }
            rows.extend(
                (
                    f"          - id: hub-mcp-{server['server_name']}{newline}",
                    f"            name: '@deepseek-ai/dsh-mcp-client'{newline}",
                    f"            config: {json.dumps(config, ensure_ascii=False)}{newline}",
                )
            )
        lines[insertion:insertion] = rows
    return "".join(lines)


def _yaml_node(source: str) -> Node | None:
    try:
        return yaml.compose(source, Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        raise DeepSeekHarnessV2Error("invalid DSH patch YAML") from None


def _field(node: Node | None, name: str) -> Node | None:
    if isinstance(node, MappingNode):
        for key, value in node.value:
            if isinstance(key, ScalarNode) and key.value == name:
                return value
    return None


def _value(node: Node | None) -> str | None:
    return node.value if isinstance(node, ScalarNode) else None


def _patch_list(source: str) -> SequenceNode | None:
    root = _yaml_node(source)
    if root is None:
        return None
    if not isinstance(root, SequenceNode):
        raise DeepSeekHarnessV2Error("DSH patch must contain a YAML list")
    return root


def _matching_rows(source: str, agent_id: str) -> list[tuple[Node, SequenceNode, bool]]:
    root = _patch_list(source)
    if root is None:
        return []
    result = []
    for patch in root.value:
        insert = _field(patch, "insert")
        rows = ((row, insert, True) for row in insert.value) if isinstance(insert, SequenceNode) else ()
        for row, parent, inserted in (*rows, (patch, root, False)):
            loader_id = _value(_field(row, "id"))
            registry_id = _value(_field(_field(row, "config"), "id"))
            if loader_id == f"preset-{agent_id}" or registry_id == agent_id:
                result.append((row, parent, inserted))
    return result


def v2_preset_conflicts(source: str, *, agent_id: str) -> bool:
    return bool(_matching_rows(source, agent_id))


def _content_end(node: Node) -> int:
    if isinstance(node, MappingNode) and not node.flow_style and node.value:
        return _content_end(node.value[-1][1])
    if isinstance(node, SequenceNode) and not node.flow_style and node.value:
        return _content_end(node.value[-1])
    return node.end_mark.index


def _managed_span(source: str, agent_id: str) -> tuple[int, int, int] | None:
    matches = _matching_rows(source, agent_id)
    if not matches:
        return None
    if len(matches) != 1:
        raise DeepSeekHarnessV2Error("DSH Preset ID has conflicting declarations")
    row, parent, inserted = matches[0]
    if (
        not inserted
        or parent.flow_style
        or _value(_field(row, "id")) != f"preset-{agent_id}"
        or _value(_field(row, "name")) != "@deepseek-ai/dsh-agent-preset"
        or _value(_field(_field(row, "config"), "id")) != agent_id
    ):
        raise DeepSeekHarnessV2Error("DSH Preset ID belongs to another declaration")
    indent = parent.start_mark.column
    start = row.start_mark.index - row.start_mark.column
    if source[start + indent:start + indent + 1] != "-":
        raise DeepSeekHarnessV2Error("unsupported DSH Preset row indentation")
    end = _content_end(row)
    if source[end - 1:end] != "\n":
        newline_at = source.find("\n", end)
        end = newline_at + 1 if newline_at >= 0 else len(source)
    return start, end, indent


def extract_v2_preset(profile_patch: str, *, agent_id: str) -> str | None:
    """Return one preset row without its surrounding whitespace or comments."""
    span = _managed_span(profile_patch, agent_id)
    if span is None:
        return None
    start, end, indent = span
    return "\n".join(line[indent:] for line in profile_patch[start:end].splitlines()) + "\n"


def merge_v2_preset(profile_patch: str, preset_patch: str, *, agent_id: str) -> str:
    """Insert or replace one managed row, preserving unrelated YAML bytes."""
    row = extract_v2_preset(preset_patch, agent_id=agent_id)
    if row is None:
        raise DeepSeekHarnessV2Error("rendered DSH Preset row is missing")
    root = _patch_list(profile_patch)
    newline = "\r\n" if "\r\n" in profile_patch else "\n"
    span = _managed_span(profile_patch, agent_id)
    if span is not None:
        start, end, indent = span
        replacement = "".join(" " * indent + line + newline for line in row.splitlines())
        return profile_patch[:start] + replacement + profile_patch[end:]
    if root is not None and root.flow_style:
        if root.value:
            raise DeepSeekHarnessV2Error("cannot append to a nonempty flow-style DSH patch")
        start, end = root.start_mark.index, root.end_mark.index
        profile_patch = profile_patch[:start] + profile_patch[end:]
    suffix = "" if not profile_patch or profile_patch.endswith("\n") else newline
    block = "".join("    " + line + newline for line in row.splitlines())
    merged = profile_patch + suffix + "- insert:" + newline + block
    _patch_list(merged)
    return merged


__all__ = [
    "DSH_V2_STANDARD_PATCH_SHA256",
    "DSH_V2_STANDARD_PATCH_SHA256_BY_VERSION",
    "DSH_V2_VERSION",
    "DeepSeekHarnessV2Error",
    "extract_v2_preset",
    "merge_v2_preset",
    "render_v2_preset_patch",
    "v2_preset_conflicts",
]
