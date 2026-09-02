from __future__ import annotations

import argparse
import asyncio
import base64
import dataclasses
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ConfigError, load_config, load_request
from .connections import ConnectionManager
from .forwarding import ForwardService
from .models import OperationRequest, OperationResult
from .service import SSHOperationsService, redact_values
from .transfers import (
    HighImpactConfirmationRequired,
    SCPService,
    SFTPService,
)


def asyncssh_version() -> str | None:
    try:
        return importlib.metadata.version("asyncssh")
    except importlib.metadata.PackageNotFoundError:
        return None


def build_service(config_path: Path) -> SSHOperationsService:
    config = load_config(config_path)
    return SSHOperationsService(config, ConnectionManager(config))


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def _secrets(service: SSHOperationsService) -> set[str]:
    if service.config is None:
        return set()
    values: set[str] = set()
    for target in service.config.targets.values():
        values.update(
            value for value in (
                target.password, target.sudo_password, target.private_key_passphrase
            ) if value
        )
    return values


def _print(value: Any, *, secrets: set[str] | frozenset[str] = frozenset()) -> None:
    safe = redact_values(_jsonable(value), secrets)
    print(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ssh-operations")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--config", type=Path, required=True)
    execute = subparsers.add_parser("exec")
    execute.add_argument("--config", type=Path, required=True)
    execute.add_argument("--target", action="append", required=True)
    execute.add_argument("--command", required=True)
    execute.add_argument("--sudo", action="store_true")
    execute.add_argument("--timeout", type=float)
    execute.add_argument("--max-parallel", type=int, default=1)
    execute.add_argument("--high-impact", action="store_true")
    execute.add_argument("--confirmed-high-impact", action="store_true")
    for name in ("run-steps", "sftp", "upload", "download", "forward"):
        child = subparsers.add_parser(name)
        child.add_argument("--config", type=Path, required=True)
        child.add_argument("--request", type=Path, required=True)
    return parser


def _aggregate(items: list[dict[str, Any]]) -> OperationResult:
    success = sum(item.get("status") == "success" for item in items)
    status = "success" if success == len(items) else "partial" if success else "failed"
    return OperationResult(status, tuple(items))


async def _sftp(service: SSHOperationsService, request: OperationRequest) -> OperationResult:
    assert service.config is not None and service.connection_manager is not None
    action = str(request.parameters.get("action", ""))
    results: list[dict[str, Any]] = []
    for target_name in service.resolve_targets(request.targets):
        try:
            async with service.connection_manager.connect(target_name) as connection:
                sftp = SFTPService(connection)
                path = str(request.parameters.get("path", ""))
                if action == "list":
                    value = await sftp.listdir(path)
                elif action == "stat":
                    value = repr(await sftp.stat(path))
                elif action == "lstat":
                    value = repr(await sftp.lstat(path))
                elif action == "read":
                    value = await sftp.read(path)
                elif action == "write":
                    value = await sftp.write(
                        path,
                        str(request.parameters.get("content", "")).encode(
                            str(request.parameters.get("encoding", "utf-8"))
                        ),
                        overwrite=bool(request.parameters.get("overwrite", False)),
                    )
                elif action == "mkdir":
                    value = await sftp.mkdir(path)
                elif action in {"rename", "move"}:
                    value = await sftp.rename(
                        str(request.parameters["source"]), str(request.parameters["destination"])
                    )
                elif action == "chmod":
                    mode_value = request.parameters.get("mode", "755")
                    mode = int(str(mode_value), 8) if isinstance(mode_value, str) else int(mode_value)
                    value = await sftp.chmod(path, mode)
                elif action == "symlink":
                    value = await sftp.symlink(
                        str(request.parameters["source"]), str(request.parameters["destination"])
                    )
                elif action == "readlink":
                    value = await sftp.readlink(path)
                elif action == "remove":
                    value = await sftp.remove(
                        path, confirmed_high_impact=request.confirmed_high_impact
                    )
                elif action == "rmdir":
                    value = await sftp.rmdir(
                        path, confirmed_high_impact=request.confirmed_high_impact
                    )
                else:
                    raise ValueError(f"unsupported SFTP action: {action}")
                await sftp.close()
            results.append({"target": target_name, "status": "success", "result": value})
        except HighImpactConfirmationRequired:
            raise
        except Exception as exc:
            results.append({"target": target_name, "status": "failed", "error": str(exc)})
    return _aggregate(results)


async def _transfer(
    service: SSHOperationsService, request: OperationRequest, *, direction: str
) -> OperationResult:
    assert service.config is not None and service.connection_manager is not None
    results: list[dict[str, Any]] = []
    mode = str(request.parameters.get("mode", "sftp")).casefold()
    for target_name in service.resolve_targets(request.targets):
        try:
            async with service.connection_manager.connect(target_name) as connection:
                source = request.parameters.get("source")
                destination = request.parameters.get("destination")
                if not isinstance(source, str) or not isinstance(destination, str):
                    raise ValueError("source and destination are required")
                if mode == "scp":
                    scp = SCPService(connection)
                    if direction == "upload":
                        local = Path(source)
                        if not local.is_absolute():
                            raise ValueError("local source path must be absolute")
                        value = await scp.upload(
                            [local], destination,
                            recurse=bool(request.parameters.get("recurse", False)),
                            preserve=bool(request.parameters.get("preserve", False)),
                        )
                    else:
                        local = Path(destination)
                        if not local.is_absolute():
                            raise ValueError("local destination path must be absolute")
                        value = await scp.download(
                            [source], local,
                            recurse=bool(request.parameters.get("recurse", False)),
                            preserve=bool(request.parameters.get("preserve", False)),
                        )
                elif mode == "sftp":
                    sftp = SFTPService(connection)
                    if direction == "upload":
                        local = Path(source)
                        if not local.is_absolute():
                            raise ValueError("local source path must be absolute")
                        value = await sftp.upload(
                            local, destination, request_id=request.request_id,
                            overwrite=bool(request.parameters.get("overwrite", False)),
                            resume=bool(request.parameters.get("resume", True)),
                        )
                    else:
                        local = Path(destination)
                        if not local.is_absolute():
                            raise ValueError("local destination path must be absolute")
                        value = await sftp.download(
                            source, local, request_id=request.request_id,
                            overwrite=bool(request.parameters.get("overwrite", False)),
                            resume=bool(request.parameters.get("resume", True)),
                        )
                    await sftp.close()
                else:
                    raise ValueError(f"unsupported transfer mode: {mode}")
            results.append({"target": target_name, **_jsonable(value)})
        except Exception as exc:
            results.append({"target": target_name, "status": "failed", "error": str(exc)})
    return _aggregate(results)


async def _forward(service: SSHOperationsService, request: OperationRequest) -> None:
    assert service.connection_manager is not None
    targets = service.resolve_targets(request.targets)
    if len(targets) != 1:
        raise ValueError("forward requires exactly one target")
    async with service.connection_manager.connect(targets[0]) as connection:
        forwarding = ForwardService(connection)
        mode = str(request.parameters.get("mode", "local"))
        listen_host = str(request.parameters.get("listen_host", "127.0.0.1"))
        listen_port = int(request.parameters.get("listen_port", 0))
        if mode == "local":
            handle = await forwarding.local(
                listen_host, listen_port,
                str(request.parameters["destination_host"]),
                int(request.parameters["destination_port"]),
            )
        elif mode == "remote":
            handle = await forwarding.remote(
                listen_host, listen_port,
                str(request.parameters["destination_host"]),
                int(request.parameters["destination_port"]),
            )
        elif mode == "socks":
            handle = await forwarding.socks(listen_host, listen_port)
        else:
            raise ValueError(f"unsupported forward mode: {mode}")
        _print(handle.ready, secrets=_secrets(service))
        print("forwarding active; interrupt to close", file=sys.stderr)
        await handle.wait_closed()


def _exit_code(status: str) -> int:
    return {
        "success": 0, "ready": 0, "partial": 2,
        "needs_dependency": 3, "needs-confirmation": 4,
    }.get(status, 1)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.config.is_absolute():
            raise ConfigError("config path must be absolute")
        if args.subcommand == "doctor":
            config = load_config(args.config)
            version = asyncssh_version()
            report = {
                "status": "ready" if version else "needs_dependency",
                "asyncssh_version": version,
                "targets": list(config.targets),
                "known_hosts": str(config.known_hosts),
                "connected": False,
            }
            _print(report)
            return _exit_code(report["status"])
        service = build_service(args.config)
        if args.subcommand == "exec":
            result = asyncio.run(
                service.exec_many(
                    args.target, args.command, max_parallel=args.max_parallel,
                    timeout=args.timeout, sudo=args.sudo,
                    explicit_high_impact=args.high_impact,
                    confirmed_high_impact=args.confirmed_high_impact,
                )
            )
        else:
            if not args.request.is_absolute():
                raise ConfigError("request path must be absolute")
            request = load_request(args.request)
            if request.operation != args.subcommand:
                raise ConfigError(
                    f"request operation {request.operation!r} does not match "
                    f"subcommand {args.subcommand!r}"
                )
            if args.subcommand == "run-steps":
                result = asyncio.run(
                    service.run_steps_many(
                        request.targets, request.steps, max_parallel=request.max_parallel
                    )
                )
            elif args.subcommand == "sftp":
                result = asyncio.run(_sftp(service, request))
            elif args.subcommand in {"upload", "download"}:
                result = asyncio.run(_transfer(service, request, direction=args.subcommand))
            else:
                asyncio.run(_forward(service, request))
                return 0
        _print(result, secrets=_secrets(service))
        return _exit_code(result.status)
    except HighImpactConfirmationRequired as exc:
        _print({"status": "needs-confirmation", "error": str(exc)})
        return 4
    except (ConfigError, ValueError, KeyError, FileNotFoundError) as exc:
        _print({"status": "invalid-request", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        print("forwarding closed", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
