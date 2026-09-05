"""Finite command-line surface for specialized-agent deployments."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .service import DeploymentService
from .runtime_bundle import prepare_wheelhouse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="specialized-agent-deployment")
    commands = parser.add_subparsers(dest="command", required=True)
    wheels = commands.add_parser("runtime-wheels")
    wheels.add_argument("--hub-root", required=True)
    wheels.add_argument("--python", required=True)
    wheels.add_argument("--destination", required=True)

    preview = commands.add_parser("preview")
    preview.add_argument("--hub-root", required=True)
    preview.add_argument("--request", required=True)

    apply = commands.add_parser("apply")
    apply.add_argument("--hub-root", required=True)
    apply.add_argument("--manifest", required=True)
    apply.add_argument("--confirmed-plan-sha256", required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--hub-root", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--behavior-evidence")
    return parser


def _absolute(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path.resolve(strict=False)


def main(
    argv: Sequence[str] | None = None,
    *,
    service: DeploymentService | None = None,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        deployment = service or DeploymentService()
        hub_root = _absolute(args.hub_root, "--hub-root")
        if args.command == "runtime-wheels":
            result = prepare_wheelhouse(hub_root, _absolute(args.python, "--python"),
                                        _absolute(args.destination, "--destination"))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "preview":
            result = deployment.preview(
                hub_root,
                _absolute(args.request, "--request"),
            )
        elif args.command == "apply":
            result = deployment.apply(
                hub_root,
                _absolute(args.manifest, "--manifest"),
                args.confirmed_plan_sha256,
            )
        else:
            evidence = (
                _absolute(args.behavior_evidence, "--behavior-evidence")
                if args.behavior_evidence is not None
                else None
            )
            result = deployment.verify(
                hub_root,
                _absolute(args.manifest, "--manifest"),
                evidence,
            )
        print(
            json.dumps(
                result.to_mapping(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
