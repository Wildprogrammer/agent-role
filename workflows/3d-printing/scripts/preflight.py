from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_workflow_hub.frontmatter import parse_markdown


CAPABILITY_PATHS = (
    ("app.blender", "capabilities/app/blender/CAPABILITY.md"),
    ("mcp.blender", "capabilities/mcp/blender/CAPABILITY.md"),
    ("app.bambu-studio", "capabilities/app/bambu-studio/CAPABILITY.md"),
    ("app.prusaslicer", "capabilities/app/prusaslicer/CAPABILITY.md"),
    ("app.orcaslicer", "capabilities/app/orcaslicer/CAPABILITY.md"),
)
SLICER_IDS = frozenset(
    {"app.bambu-studio", "app.prusaslicer", "app.orcaslicer"}
)
GUIDE_LANGUAGES = frozenset({"en", "zh-CN"})


@dataclass(frozen=True)
class CapabilityCheck:
    id: str
    locked_version: str
    version_requirement: str
    recommended_version: str
    official_source: str
    integrity_method: str
    integrity_value: str
    detect_command: str
    installation_policy: str
    installation_scope: str
    installation_methods: tuple[str, ...]
    automation_status: str
    workspace_source: str | None
    executable: str | None
    observed: str | None
    status: str
    detail: str


Runner = Callable[[Sequence[str]], str]
Which = Callable[[str], str | None]


def _run_read_only(command: Sequence[str]) -> str:
    environment = os.environ.copy()
    environment["DISABLE_TELEMETRY"] = "true"
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode:
        raise RuntimeError(f"exit {completed.returncode}: {output}")
    return output


def _observed_version(output: str) -> tuple[int, ...] | None:
    match = re.search(r"\d+(?:\.\d+){2,3}", output)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _minimum_version(requirement: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r">=(\d+(?:\.\d+){2,3})", requirement)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _load_capability(hub_root: Path, relative_path: str) -> tuple[dict, str]:
    frontmatter, _ = parse_markdown(hub_root / relative_path)
    detect = frontmatter["detect"]
    integrity = frontmatter["integrity"]
    installation = frontmatter["installation"]
    return {
        "locked_version": frontmatter["locked_version"],
        "version_requirement": frontmatter["version_requirement"],
        "recommended_version": frontmatter["recommended_version"],
        "official_source": frontmatter["official_source"],
        "integrity_method": integrity["method"],
        "integrity_value": integrity["value"],
        "detect_command": detect["command"],
        "installation_policy": installation["policy"],
        "installation_scope": installation["scope"],
        "installation_methods": tuple(installation["methods"]),
        "automation_status": frontmatter.get("automation_status", "conditional"),
        "workspace_source": frontmatter.get("workspace_source"),
    }, detect["command"]


def _workspace_source_state(
    hub_root: Path, metadata: dict, runner: Runner
) -> tuple[str, str | None]:
    relative_source = metadata.get("workspace_source")
    if not relative_source:
        return "not-declared", None
    root = hub_root.resolve(strict=True)
    source = (hub_root / relative_source).resolve(strict=False)
    try:
        source.relative_to(root)
    except ValueError:
        return "outside-hub", None
    if not source.is_dir():
        return "missing", None
    try:
        revision = runner(("git", "-C", str(source), "rev-parse", "HEAD")).strip()
    except (OSError, RuntimeError):
        return "unreadable", None
    expected = metadata["locked_version"].removeprefix("git:")
    if revision.casefold() != expected.casefold():
        return "mismatch", revision
    return "verified", revision


def _check_mcp(
    capability_id: str,
    metadata: dict,
    *,
    hub_root: Path,
    which: Which,
    runner: Runner,
) -> CapabilityCheck:
    executable = which("uvx")
    if executable is None:
        return CapabilityCheck(
            capability_id,
            executable=None,
            observed=None,
            status="missing",
            detail="uvx is not available; Blender MCP cannot be configured.",
            **metadata,
        )
    try:
        observed = runner((executable, "--version"))
    except (OSError, RuntimeError) as exc:
        return CapabilityCheck(
            capability_id,
            executable=executable,
            observed=None,
            status="detection-failed",
            detail=str(exc),
            **metadata,
        )
    source_state, source_revision = _workspace_source_state(hub_root, metadata, runner)
    if source_state != "verified":
        details = {
            "missing": "the locked workspace source is absent; clone it before host setup",
            "mismatch": "the workspace source HEAD does not match the locked commit",
            "outside-hub": "the declared workspace source resolves outside HUB_ROOT",
            "unreadable": "the workspace source Git HEAD could not be read",
            "not-declared": "no workspace source is declared",
        }
        return CapabilityCheck(
            capability_id,
            executable=executable,
            observed=observed,
            status="source-unverified",
            detail=details[source_state],
            **metadata,
        )
    return CapabilityCheck(
        capability_id,
        executable=executable,
        observed=f"{observed}\ngit HEAD {source_revision}",
        status="host-configuration-required",
        detail=(
            "uvx is available and the workspace source verified at locked commit; "
            "Codex MCP mapping requires user-performed configuration and a separate "
            "host smoke."
        ),
        **metadata,
    )


def _check_application(
    capability_id: str,
    metadata: dict,
    *,
    executable_override: Path | None = None,
    which: Which,
    runner: Runner,
) -> CapabilityCheck:
    command = tuple(shlex.split(metadata["detect_command"]))
    executable = str(executable_override) if executable_override else which(command[0])
    if executable is None:
        return CapabilityCheck(
            capability_id,
            executable=None,
            observed=None,
            status="missing",
            detail=f"{command[0]} was not found on PATH.",
            **metadata,
        )
    probe = (executable, *command[1:])
    try:
        observed = runner(probe)
    except (OSError, RuntimeError) as exc:
        return CapabilityCheck(
            capability_id,
            executable=executable,
            observed=None,
            status="detection-failed",
            detail=str(exc),
            **metadata,
        )
    minimum = _minimum_version(metadata["version_requirement"])
    detected = _observed_version(observed)
    if minimum and detected and detected >= minimum:
        status = (
            "automation-smoke-required"
            if capability_id == "app.bambu-studio"
            else "compatible"
        )
        detail = (
            f"Detected compatible version {'.'.join(map(str, detected))}; "
            f"minimum is {metadata['version_requirement']}, recommended is "
            f"{metadata['recommended_version']}."
        )
        if status == "automation-smoke-required":
            detail += " Provider smoke evidence is required before automation."
    else:
        status = "version-unconfirmed"
        detail = (
            "Executable responded, but a version meeting the minimum requirement "
            f"{metadata['version_requirement']} was not proven by the read-only "
            "detector."
        )
    return CapabilityCheck(
        capability_id,
        executable=executable,
        observed=observed,
        status=status,
        detail=detail,
        **metadata,
    )


def inspect_environment(
    hub_root: Path,
    *,
    tool_paths: Mapping[str, Path] | None = None,
    which: Which = shutil.which,
    runner: Runner = _run_read_only,
) -> tuple[CapabilityCheck, ...]:
    tool_paths = tool_paths or {}
    checks = []
    for capability_id, relative_path in CAPABILITY_PATHS:
        metadata, _ = _load_capability(hub_root, relative_path)
        if capability_id == "mcp.blender":
            check = _check_mcp(
                capability_id,
                metadata,
                hub_root=hub_root,
                which=which,
                runner=runner,
            )
        else:
            check = _check_application(
                capability_id,
                metadata,
                executable_override=tool_paths.get(capability_id),
                which=which,
                runner=runner,
            )
        checks.append(check)
    return tuple(checks)


def has_local_dependencies(checks: Sequence[CapabilityCheck]) -> bool:
    by_id = {check.id: check for check in checks}
    return (
        by_id["app.blender"].status == "compatible"
        and any(by_id[capability_id].status == "compatible" for capability_id in SLICER_IDS)
    )


def has_split_dependencies(checks: Sequence[CapabilityCheck]) -> bool:
    by_id = {check.id: check for check in checks}
    return (
        by_id["app.blender"].status == "compatible"
        and by_id["app.bambu-studio"].status == "compatible"
    )


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError):
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _guide_output_path(hub_root: Path, path: Path) -> Path:
    root = hub_root.resolve(strict=True)
    if not path.is_absolute():
        raise ValueError("installation guide path must be absolute")
    output_root = (root / "workflows" / "3d-printing" / "outputs").resolve()
    path = path.resolve(strict=False)
    try:
        path.relative_to(output_root)
    except ValueError as exc:
        raise ValueError("installation guide must stay inside 3d-printing outputs") from exc
    if path.name != "INSTALLATION-GUIDE.md":
        raise ValueError("installation guide must be named INSTALLATION-GUIDE.md")
    _assert_unlinked_output_path(path, output_root, root)
    return path


def _assert_unlinked_output_path(candidate: Path, output_root: Path, root: Path) -> None:
    try:
        candidate.relative_to(output_root)
    except ValueError as exc:
        raise ValueError("installation guide must stay inside 3d-printing outputs") from exc
    current = candidate
    while current != root:
        if current.exists() and _is_link_or_reparse(current):
            raise ValueError("installation guide path cannot contain links or reparse points")
        current = current.parent
    if current != root:
        raise ValueError("installation guide path must stay below hub root")


def write_installation_guide(
    hub_root: Path,
    path: Path,
    checks: Sequence[CapabilityCheck],
    *,
    host: str,
    language: str,
) -> None:
    if language not in GUIDE_LANGUAGES:
        raise ValueError(f"unsupported guide language: {language}")

    lines = _guide_opening(language, host)
    for check in checks:
        lines.extend(_detection_lines(language, check))

    lines.extend(_preparation_heading(language))
    for check in checks:
        lines.extend(_preparation_lines(language, check))

    lines.extend(_safety_lines(language))
    target = _guide_output_path(hub_root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _legacy_guide_opening(language: str, host: str) -> list[str]:
    if language == "zh-CN":
        return [
            "# 3D 打印环境安装指南",
            "",
            "## 范围",
            "",
            "Agent 不会安装桌面软件、系统级软件或 MCP 宿主配置。本指南记录只读检测结果，"
            "并说明由使用者完成 3D 打印工作流准备的选项。",
            "",
            f"- 选定宿主：`{host}`",
            "- 安装策略：先检测；本工作流能力均为 `user-managed`，由使用者选择并执行安装。",
            "- 不得发送、排队或启动打印。",
            "",
            "## 当前检测结果",
            "",
        ]
    return [
        "# 3D Printing Environment Installation Guide",
        "",
        "## Scope",
        "",
        "Agent does not install user-managed desktop software, system software, or "
        "MCP host configuration. This guide records read-only detection and the "
        "user-performed preparation options for the 3D Printing workflow.",
        "",
        f"- Selected host: `{host}`",
        "- Installation policy: detect first; every workflow capability is "
        "`user-managed` and the user selects and performs setup.",
        "- No action may send, queue, or start a print.",
        "",
        "## Current Detection",
        "",
    ]


def _guide_opening(language: str, host: str) -> list[str]:
    if language == "zh-CN":
        return [
            "# 3D 打印环境安装指南",
            "",
            "## 范围",
            "",
            "Agent 不会安装桌面软件或系统级软件。本指南记录只读检测结果，"
            "并说明由使用者完成 3D 打印工作流准备的选项。",
            "",
            f"- 选定宿主：`{host}`",
            "- 安装策略：先检测；MCP 源码可由 Agent 按锁定提交准备到共享工作区，"
            "桌面软件和 MCP 宿主配置由使用者完成。",
            "- 不得发送、排队或启动打印。",
            "",
            "## 当前检测结果",
            "",
        ]
    return [
        "# 3D Printing Environment Installation Guide",
        "",
        "## Scope",
        "",
        "Agent does not install desktop or system software. This guide records "
        "read-only detection and the user-performed preparation options; the "
        "pinned MCP source may be prepared in the shared workspace.",
        "",
        f"- Selected host: `{host}`",
        "- Installation policy: detect first; the user handles desktop software "
        "and MCP host configuration, while the Agent may prepare the pinned MCP "
        "source in the shared workspace.",
        "- No action may send, queue, or start a print.",
        "",
        "## Current Detection",
        "",
    ]


def _detection_lines(language: str, check: CapabilityCheck) -> list[str]:
    if language == "zh-CN":
        return [
            f"- `{check.id}`：{check.status}",
            f"  - 详情：{check.detail}",
            f"  - 检测路径：{check.executable or '未找到'}",
            f"  - 检测输出：{check.observed or '不可用'}",
            f"  - 安装契约：`{check.installation_policy}` / "
            f"`{check.installation_scope}` / {', '.join(check.installation_methods)}",
        ]
    return [
        f"- `{check.id}`: {check.status}",
        f"  - Detail: {check.detail}",
        f"  - Detected path: {check.executable or 'not found'}",
        f"  - Observed output: {check.observed or 'not available'}",
        f"  - Installation contract: `{check.installation_policy}` / "
        f"`{check.installation_scope}` / {', '.join(check.installation_methods)}",
    ]


def _preparation_heading(language: str) -> list[str]:
    return ["", "## 使用者执行的准备工作"] if language == "zh-CN" else [
        "",
        "## User-Performed Preparation",
    ]


def _preparation_lines(language: str, check: CapabilityCheck) -> list[str]:
    if language == "zh-CN":
        lines = [
            "",
            f"### {check.id}",
            "",
            f"- 不可变来源锁：`{check.locked_version}`",
            f"- 最低兼容版本：`{check.version_requirement}`",
            f"- 推荐安装版本：`{check.recommended_version}`",
            f"- 官方来源：{check.official_source}",
            f"- 完整性：`{check.integrity_method}` `{check.integrity_value}`",
            f"- 安装后只读检查：`{check.detect_command}`",
        ]
        if check.id == "mcp.blender" and check.installation_policy == "agent-managed":
            return lines + [
                "- Agent 可将锁定提交的 MCP 源码克隆到 `workspace/shared/mcp/blender-mcp`，"
                "并验证 Git HEAD。",
                "- 使用者仍需手动配置 Codex MCP 映射并完成宿主 smoke。",
            ]
        if check.id == "mcp.blender":
            return lines + [
                "- 请选择由使用者执行的源码方式：复用匹配的 MCP 配置，或在锁定提交获取官方源码。",
                "- Git 仅适用于此源码能力；验证源码后，仍需手动配置 Codex MCP 映射。",
            ]
        return lines + [
            "- 请自行选择一种方式：复用兼容的既有安装、获取锁定的官方制品、在核对准确包名和版本后"
            "使用系统包管理器，或按文档手动设置。"
        ]

    lines = [
        "",
        f"### {check.id}",
        "",
        f"- Immutable source lock: `{check.locked_version}`",
        f"- Minimum compatible version: `{check.version_requirement}`",
        f"- Recommended installation version: `{check.recommended_version}`",
        f"- Official source: {check.official_source}",
        f"- Integrity: `{check.integrity_method}` `{check.integrity_value}`",
        f"- Read-only post-install check: `{check.detect_command}`",
    ]
    if check.id == "mcp.blender" and check.installation_policy == "agent-managed":
        return lines + [
            "- The Agent may clone the locked MCP commit to "
            "`workspace/shared/mcp/blender-mcp` and verify Git HEAD.",
            "- The user must still configure the Codex MCP mapping and complete "
            "the host smoke.",
        ]
    if check.id == "mcp.blender":
        return lines + [
            "- Choose a user-performed source method: reuse a matching MCP setup or "
            "obtain the official source at the locked commit.",
            "- Git is appropriate only for this source-based capability; configure "
            "the Codex MCP mapping manually after source verification.",
        ]
    return lines + [
        "- Choose one method yourself: reuse a compatible existing install, obtain "
        "the locked official artifact, use a system package manager after checking "
        "its exact package ID/version, or perform the documented manual setup."
    ]


def _safety_lines(language: str) -> list[str]:
    if language == "zh-CN":
        return [
            "",
            "## 安全与验证",
            "",
            "- 不要从本指南运行系统包管理器、安装程序或 Git 克隆。",
            "- 不要仅凭名称接受包管理器安装；记录解析后的版本，并验证要求的版本与完整性证据。",
            "- 若安装失败，保留错误输出并显式选择下一种方式；不要自动回退到其他安装方式。",
            "- 使用者完成准备后，请 Agent 重新执行只读检测、Codex 适配器 smoke 测试和所选切片器"
            " smoke 测试，再开始 Gate A/B/C 工作。",
        ]
    return [
        "",
        "## Safety And Verification",
        "",
        "- Do not run a system package manager, installer, or Git clone from this guide.",
        "- Do not accept a package-manager install merely by name; record its resolved "
        "version and verify the required version/integrity evidence.",
        "- If an installer fails, keep its error output and choose the next method "
        "explicitly; do not automatically fall back to another method.",
        "- After user-performed preparation, ask the Agent to rerun read-only "
        "detection, Codex adapter smoke, and the selected slicer smoke before "
        "starting Gate A/B/C work.",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only 3D printing preflight")
    parser.add_argument("--hub-root", required=True, type=Path)
    parser.add_argument("--guide", type=Path)
    parser.add_argument("--host", default="codex")
    parser.add_argument(
        "--mode",
        choices=("standard", "split-and-slice-bambu"),
        default="standard",
    )
    parser.add_argument(
        "--tool-path",
        action="append",
        default=[],
        metavar="CAPABILITY=ABSOLUTE_PATH",
    )
    parser.add_argument("--language", choices=sorted(GUIDE_LANGUAGES))
    args = parser.parse_args(argv)

    hub_root = args.hub_root.resolve()
    tool_paths = {}
    valid_tool_ids = {
        capability_id
        for capability_id, _ in CAPABILITY_PATHS
        if capability_id.startswith("app.")
    }
    for value in args.tool_path:
        capability_id, separator, raw_path = value.partition("=")
        candidate = Path(raw_path) if separator else Path()
        if (
            not separator
            or capability_id not in valid_tool_ids
            or not candidate.is_absolute()
            or not candidate.is_file()
        ):
            parser.error(
                "--tool-path must be CAPABILITY=existing absolute executable path"
            )
        tool_paths[capability_id] = candidate

    checks = inspect_environment(hub_root, tool_paths=tool_paths)
    for check in checks:
        print(f"{check.id}: {check.status}: {check.detail}")

    local_dependencies_ready = (
        has_split_dependencies(checks)
        if args.mode == "split-and-slice-bambu"
        else has_local_dependencies(checks)
    )
    if args.guide is not None and not local_dependencies_ready:
        if args.language is None:
            parser.error("--language is required with --guide")
        write_installation_guide(
            hub_root,
            args.guide,
            checks,
            host=args.host,
            language=args.language,
        )
        print(f"installation guidance: {args.guide}")
    return 0 if local_dependencies_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
