from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .catalog import RepositoryCatalogError, load_repository_catalog
from .config_init import ConfigInitError, initialize_config
from .doctor import WorkflowDoctorError, doctor, workflow_doctor
from .jenkins_mcp.server import run_jenkins_mcp
from .mysql_mcp.server import run_mysql_mcp
from .repository import validate_repository
from .support import PROJECT_HOSTS, host_compatibility


def _tsv_field(value: str) -> str:
    unsafe_categories = {"Cc", "Cf", "Cs", "Zl", "Zp"}
    return "".join(
        json.dumps(
            character,
            ensure_ascii=(
                unicodedata.category(character) in unsafe_categories
            ),
        )[1:-1]
        for character in value
    )


def _write_stdout_line(line: str) -> None:
    stream = sys.stdout
    encoding = getattr(stream, "encoding", None)
    if encoding:
        line = line.encode(
            encoding, errors="backslashreplace"
        ).decode(encoding)
    stream.write(line + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow-hub")
    parser.add_argument(
        "--version",
        action="version",
        version=f"agent-workflow-hub {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("root")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("root")
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("workflow")
    inspect_parser.add_argument("--host", required=True)
    inspect_parser.add_argument("root")
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--host", required=True)
    doctor_parser.add_argument("--workflow")
    doctor_parser.add_argument("--config", action="append", default=[])
    doctor_parser.add_argument("--capability", action="append", default=[])
    doctor_parser.add_argument("root", nargs="?", default=".")
    init_parser = subparsers.add_parser("init-config")
    init_parser.add_argument("workflow")
    init_parser.add_argument("target")
    init_parser.add_argument("root")
    jenkins_mcp_parser = subparsers.add_parser("jenkins-mcp")
    jenkins_mcp_parser.add_argument("ini_config")
    mysql_mcp_parser = subparsers.add_parser("mysql-mcp")
    mysql_mcp_parser.add_argument("ini_config")
    return parser


def _assignments(
    values: Sequence[str], *, kind: str
) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise WorkflowDoctorError(f"{kind} must use LABEL=VALUE")
        label, item = value.split("=", 1)
        if not label or not item:
            raise WorkflowDoctorError(f"{kind} must use nonblank LABEL=VALUE")
        if label in parsed:
            raise WorkflowDoctorError(f"duplicate {kind} label")
        parsed[label] = item
    return parsed


def _workflow_doctor_payload(report) -> dict[str, object]:
    return {
        "workflow": report.workflow,
        "status": report.status,
        "configs": [
            {
                "label": config.label,
                "required": config.required,
                "scope": config.scope,
                "status": config.status,
                "path": str(config.path) if config.path is not None else None,
            }
            for config in report.configs
        ],
        "capabilities": [
            {
                "capability_id": capability.capability_id,
                "required": capability.required,
                "status": capability.status,
                "details": list(capability.details),
            }
            for capability in report.capabilities
        ],
        "host_compatibility": report.host_compatibility,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command == "validate":
        root = Path(os.path.abspath(args.root))
        issues = validate_repository(root, require_project_markers=True)
        for issue in issues:
            print(f"{issue.code}: {issue.path}: {issue.message}")
        return 1 if issues else 0
    if args.command in {"list", "inspect"}:
        root = Path(os.path.abspath(args.root))
        try:
            catalog = load_repository_catalog(root)
        except RepositoryCatalogError:
            print("error: invalid repository catalog", file=sys.stderr)
            return 2
        if args.command == "list":
            for workflow in catalog.workflows:
                line = "\t".join(
                    (
                        workflow.name,
                        _tsv_field(workflow.description),
                        str(len(workflow.required_capabilities)),
                    )
                )
                _write_stdout_line(line)
            return 0

        workflow = next(
            (
                descriptor
                for descriptor in catalog.workflows
                if descriptor.name == args.workflow
            ),
            None,
        )
        if workflow is None:
            print("error: unknown workflow", file=sys.stderr)
            return 2
        if args.host not in PROJECT_HOSTS:
            print("error: unknown host", file=sys.stderr)
            return 2

        compatibility = host_compatibility(
            PROJECT_HOSTS,
            frozenset(workflow.supported_hosts),
            (
                catalog.capabilities[capability_id].hosts
                for capability_id in workflow.required_capabilities
            ),
            {},
        )
        if args.host not in compatibility:
            print("error: unsupported workflow host", file=sys.stderr)
            return 2

        capability_ids = set(workflow.required_capabilities)
        for candidates in workflow.capability_slots.values():
            capability_ids.update(candidates)
        capabilities = []
        for capability_id in sorted(capability_ids):
            capability = catalog.capabilities[capability_id]
            installation = capability.installation
            capabilities.append(
                {
                    "id": capability.id,
                    "type": capability.type,
                    "locked_version": capability.locked_version,
                    "version_requirement": capability.version_requirement,
                    "recommended_version": capability.recommended_version,
                    "installation": {
                        "scope": installation["scope"],
                        "policy": installation["policy"],
                        "methods": list(installation["methods"]),
                    },
                }
            )
        payload = {
            "workflow": {
                "name": workflow.name,
                "description": workflow.description,
                "required_capabilities": list(
                    workflow.required_capabilities
                ),
                "capability_slots": {
                    slot: list(workflow.capability_slots[slot])
                    for slot in sorted(workflow.capability_slots)
                },
                "supported_hosts": list(workflow.supported_hosts),
            },
            "capabilities": capabilities,
            "entrypoints": dict(workflow.entrypoints),
            "config_templates": [
                {
                    "label": template.label,
                    "relative_path": template.relative_path,
                    "output_name": template.output_name,
                    "scope": template.scope,
                    "required": template.required,
                    "sha256": template.sha256,
                }
                for template in workflow.config_templates
            ],
            "host_compatibility": compatibility[args.host],
        }
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.command == "doctor":
        root = Path(os.path.abspath(args.root))
        if args.host not in PROJECT_HOSTS:
            print("error: unknown host", file=sys.stderr)
            return 2
        if args.workflow is not None:
            try:
                catalog = load_repository_catalog(root)
                raw_configs = _assignments(args.config, kind="config")
                selections = _assignments(
                    args.capability, kind="capability"
                )
                report = workflow_doctor(
                    catalog,
                    args.workflow,
                    host=args.host,
                    config_paths={
                        label: Path(path)
                        for label, path in raw_configs.items()
                    },
                    selected_capabilities=selections,
                )
            except RepositoryCatalogError:
                print("error: invalid repository catalog", file=sys.stderr)
                return 2
            except WorkflowDoctorError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(_workflow_doctor_payload(report), sort_keys=True))
            return 0 if report.status in {
                "ready",
                "ready_with_optional_gaps",
            } else 2
        if args.config or args.capability:
            print(
                "error: --config and --capability require --workflow",
                file=sys.stderr,
            )
            return 2
        report = doctor(root, host=args.host)
        print(
            f"{report.host}: {report.status} "
            "(adapter evidence only; not workflow readiness)"
        )
        for detail in report.details:
            print(f"  {detail}")
        return 0 if report.status == "verified" else 2
    if args.command == "init-config":
        root = Path(os.path.abspath(args.root))
        target = Path(os.path.abspath(args.target))
        try:
            catalog = load_repository_catalog(root)
            result = initialize_config(catalog, args.workflow, target)
        except RepositoryCatalogError:
            print("error: invalid repository catalog", file=sys.stderr)
            return 2
        except ConfigInitError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "workflow": result.workflow,
                    "target": str(result.target),
                    "files": [str(path) for path in result.files],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "jenkins-mcp":
        run_jenkins_mcp(Path(os.path.abspath(args.ini_config)))
        return 0
    if args.command == "mysql-mcp":
        run_mysql_mcp(Path(os.path.abspath(args.ini_config)))
        return 0
    parser.print_help()
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
