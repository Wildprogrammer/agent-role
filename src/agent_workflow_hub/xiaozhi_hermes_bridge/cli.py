"""CLI and line-oriented stdio adapter for the Xiaozhi Hermes bridge."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
import re

from .binding_store import BindingStore, BindingStoreError, default_binding_store_path
from .contracts import BridgeConfig, BridgeContractError, BridgeRequest, read_config, read_request
from .desktop_gateway import (
    DesktopGatewayError,
    HermesDesktopGateway,
    discover_desktop_gateway_backend,
)
from .dsh_web_gateway import DeepSeekHarnessWebGateway, DshGatewayError
from .shared_service import (
    ConversationGateway,
    LifecycleBindingError,
    SharedConversationService,
)
from .service import (
    BridgeRejected,
    BridgeService,
    HermesRunResult,
    HermesRunsTransport,
    HermesTransportError,
    RunsTransport,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_REQUEST_LINE_CHARS = 65_536
_MAX_RESPONSE_CHARS = 1_048_576
_SHARED_OPERATIONS = frozenset(
    (
        "list_agents",
        "list_sessions",
        "bind_session",
        "show_binding",
        "unbind_session",
        "dispatch_shared",
        "dispatch_bound_voice",
        "cancel_bound_voice",
        "create_session",
        "compress_session",
    )
)


def _validate_attachment_files(
    request: BridgeRequest, environment: Mapping[str, str]
) -> None:
    if not request.attachments:
        return
    raw_root = environment.get("AGENT_GATEWAY_ATTACHMENT_ROOT", environment.get("XIAOAI_MCP_ATTACHMENT_ROOT", "")).strip()
    root = Path(raw_root)
    if not root.is_absolute():
        raise BridgeRejected("attachment staging root is unavailable")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        raise BridgeRejected("attachment staging root is unavailable") from None
    for attachment in request.attachments:
        try:
            path = attachment.path.resolve(strict=True)
            if attachment.path.is_symlink() or not path.is_file():
                raise BridgeRejected("attachment file is unavailable")
            path.relative_to(resolved_root)
            stat = path.stat()
        except ValueError:
            raise BridgeRejected("attachment path is outside the staging root") from None
        except OSError:
            raise BridgeRejected("attachment file is unavailable") from None
        if stat.st_size != attachment.bytes:
            raise BridgeRejected("attachment size changed after upload")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            raise BridgeRejected("attachment file cannot be verified") from None
        if not hmac.compare_digest(digest.hexdigest(), attachment.sha256):
            raise BridgeRejected("attachment content changed after upload")


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BridgeContractError(f"invalid arguments: {message}")


class _ResponseTooLarge(ValueError):
    pass


def _absolute_file(value: str) -> Path:
    if "\0" in value:
        raise argparse.ArgumentTypeError("path must not contain NUL")
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    if not path.is_file():
        raise argparse.ArgumentTypeError("path must name an existing file")
    return path


def _absolute_path(value: str) -> Path:
    if "\0" in value:
        raise argparse.ArgumentTypeError("path must not contain NUL")
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path.resolve()


def _write_json(value: Mapping[str, object], *, stream: object) -> None:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded) > _MAX_RESPONSE_CHARS:
        raise _ResponseTooLarge("response exceeds the stdio limit")
    stream.write(encoded + "\n")  # type: ignore[attr-defined]
    stream.flush()  # type: ignore[attr-defined]


def _sha256(value: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("digest must be 64 lowercase hexadecimal characters")
    return value


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configure_standard_stream(stream: object, *, errors: str) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors=errors)


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="xiaozhi-hermes-bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "simulate", "serve-stdio"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=_absolute_file)
        if name == "simulate":
            command.add_argument("--request", required=True, type=_absolute_file)
        if name == "serve-stdio":
            command.add_argument(
                "--confirmed-config-sha256", required=True, type=_sha256
            )
    desktop_doctor = commands.add_parser("desktop-doctor")
    desktop_doctor.add_argument("--config", required=True, type=_absolute_file)
    desktop_agents = commands.add_parser("desktop-list-agents")
    desktop_agents.add_argument("--config", required=True, type=_absolute_file)
    desktop_list = commands.add_parser("desktop-list-sessions")
    desktop_list.add_argument("--config", required=True, type=_absolute_file)
    desktop_list.add_argument(
        "--host", choices=("hermes", "deepseek-harness"), default="hermes"
    )
    desktop_list.add_argument("--profile", required=True)
    desktop_list.add_argument("--limit", type=int, default=20)
    desktop_cancel = commands.add_parser("desktop-cancel-session")
    desktop_cancel.add_argument("--config", required=True, type=_absolute_file)
    desktop_cancel.add_argument("--session-id", required=True)
    desktop_remove_queue = commands.add_parser("desktop-remove-queued-item")
    desktop_remove_queue.add_argument("--config", required=True, type=_absolute_file)
    desktop_remove_queue.add_argument("--session-id", required=True)
    desktop_remove_queue.add_argument("--item-id", required=True)
    desktop_bind = commands.add_parser("desktop-bind")
    desktop_bind.add_argument("--config", required=True, type=_absolute_file)
    desktop_bind.add_argument("--state", type=_absolute_path)
    desktop_bind.add_argument("--device-id", required=True)
    desktop_bind.add_argument(
        "--host", choices=("hermes", "deepseek-harness"), default="hermes"
    )
    desktop_bind.add_argument("--profile", required=True)
    desktop_bind.add_argument("--stored-session-id", required=True)
    desktop_binding = commands.add_parser("desktop-show-binding")
    desktop_binding.add_argument("--config", required=True, type=_absolute_file)
    desktop_binding.add_argument("--state", type=_absolute_path)
    desktop_binding.add_argument("--device-id", required=True)
    desktop_unbind = commands.add_parser("desktop-unbind")
    desktop_unbind.add_argument("--config", required=True, type=_absolute_file)
    desktop_unbind.add_argument("--state", type=_absolute_path)
    desktop_unbind.add_argument("--device-id", required=True)
    desktop_send = commands.add_parser("desktop-send")
    desktop_send.add_argument("--config", required=True, type=_absolute_file)
    desktop_send.add_argument("--state", type=_absolute_path)
    desktop_send.add_argument("--device-id", required=True)
    desktop_send.add_argument("--text", required=True)
    desktop_send.add_argument("--confirmed", action="store_true")
    return parser


class _SimulationTransport:
    def run(self, route: object, *, input: str, session_id: str) -> HermesRunResult:
        return HermesRunResult(status="simulated", run_id="not-submitted", output=None)


def _doctor(
    config: BridgeConfig,
    environment: Mapping[str, str],
    *,
    config_sha256: str,
) -> dict[str, object]:
    profiles = [
        {
            "name": route.name,
            "api_server_base_url": route.api_server_base_url,
            "api_key_env": route.api_key_env,
            "api_key_present": bool(environment.get(route.api_key_env)),
        }
        for route in config.profiles
    ]
    keys_ready = all(item["api_key_present"] for item in profiles)
    deepseek = config.deepseek_harness
    return {
        "schema_version": "1.0",
        "status": (
            "configuration_ready_live_not_checked"
            if config.dispatch_enabled and keys_ready
            else "safe_disabled"
            if not config.dispatch_enabled
            else "needs_configuration"
        ),
        "dispatch_enabled": config.dispatch_enabled,
        "require_confirmation": config.require_confirmation,
        "bound_voice_dispatch_mode": config.bound_voice_dispatch_mode,
        "trusted_caller_id": config.trusted_caller_id,
        "config_sha256": config_sha256,
        "live_readiness_checked": False,
        "default_profile": config.default_profile,
        "profiles": profiles,
        "deepseek_harness": (
            {
                "configured": True,
                "base_url": deepseek.base_url,
                "expected_version": deepseek.expected_version,
                "launch_token_env": deepseek.launch_token_env,
                "launch_token_present": bool(environment.get(deepseek.launch_token_env)),
                "live_readiness_checked": False,
            }
            if deepseek is not None
            else {"configured": False, "live_readiness_checked": False}
        ),
    }


def _error_response(
    exc: Exception,
    *,
    request: BridgeRequest | None = None,
) -> dict[str, object]:
    if isinstance(exc, LifecycleBindingError):
        return {
            "schema_version": "1.0",
            "status": exc.status,
            "error": str(exc),
            "host": exc.host,
            "profile": exc.profile,
            "stored_session_id": exc.stored_session_id,
            "runtime_session_id": exc.runtime_session_id,
            "old_stored_session_id": exc.old_stored_session_id,
            "old_binding_revision": exc.old_binding_revision,
            **(
                {"device_id": request.device_id}
                if request is not None and request.device_id is not None
                else {}
            ),
            **(
                {"operation": request.operation, "session_id": request.session_id}
                if request
                else {}
            ),
        }
    rejected = isinstance(exc, (json.JSONDecodeError, BridgeContractError, BridgeRejected))
    outcome_unknown = (
        isinstance(exc, HermesTransportError) and exc.outcome_unknown
    ) or (
        isinstance(exc, DesktopGatewayError) and exc.outcome_unknown
    ) or (
        isinstance(exc, DshGatewayError) and exc.outcome_unknown
    )
    return {
        "schema_version": "1.0",
        "status": (
            "rejected" if rejected else "outcome_unknown" if outcome_unknown else "failed"
        ),
        "error": str(exc),
        **({"operation": request.operation, "session_id": request.session_id} if request else {}),
        **(
            {"run_id": exc.run_id}
            if isinstance(exc, HermesTransportError) and exc.run_id is not None
            else {}
        ),
    }


def _desktop_gateway(environment: Mapping[str, str]) -> HermesDesktopGateway:
    backend = discover_desktop_gateway_backend(environment)
    return HermesDesktopGateway(backend)


def _shared_service(
    config: BridgeConfig,
    environment: Mapping[str, str],
    state_path: Path | None,
) -> SharedConversationService:
    path = state_path or default_binding_store_path(environment)
    gateways: dict[str, ConversationGateway] = {}
    try:
        hermes_gateway = _desktop_gateway(environment)
    except DesktopGatewayError:
        hermes_gateway = None
    try:
        deepseek_gateway = _deepseek_gateway(config, environment)
    except DshGatewayError:
        deepseek_gateway = None
    if deepseek_gateway is not None:
        gateways["deepseek-harness"] = deepseek_gateway
    return SharedConversationService(
        config,
        BindingStore(path),
        hermes_gateway,
        gateways=gateways,
    )


def _deepseek_gateway(
    config: BridgeConfig, environment: Mapping[str, str]
) -> DeepSeekHarnessWebGateway | None:
    dsh = config.deepseek_harness
    if dsh is None:
        return None
    launch_token = environment.get(dsh.launch_token_env, "").strip()
    if not launch_token:
        return None
    return DeepSeekHarnessWebGateway(
        base_url=dsh.base_url,
        launch_token=launch_token,
        dsh_home=dsh.dsh_home,
        expected_version=dsh.expected_version,
        allowed_presets=dsh.allowed_presets,
        request_timeout_seconds=config.request_timeout_seconds,
        poll_interval_seconds=config.poll_interval_seconds,
        max_polls=config.max_polls,
    )


def _handle_shared_request(
    service: SharedConversationService,
    request: BridgeRequest,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    common: dict[str, object] = {
        "schema_version": "1.0",
        "status": "ok",
        "operation": request.operation,
        "session_id": request.session_id,
    }
    if request.operation == "list_agents":
        return {**common, "agents": list(service.list_agents())}
    if request.operation == "list_sessions":
        assert (
            request.profile is not None
            and request.limit is not None
            and request.host is not None
        )
        return {
            **common,
            "host": request.host,
            "profile": request.profile,
            "sessions": list(
                service.list_sessions(
                    host=request.host, profile=request.profile, limit=request.limit
                )
            ),
        }
    assert request.device_id is not None
    common["device_id"] = request.device_id
    if request.operation == "bind_session":
        assert (
            request.profile is not None
            and request.stored_session_id is not None
            and request.host is not None
        )
        binding = service.bind(
            device_id=request.device_id,
            host=request.host,
            profile=request.profile,
            stored_session_id=request.stored_session_id,
            expected_revision=request.binding_revision,
        )
        return {**common, **service.binding_response(binding)}
    if request.operation == "show_binding":
        return {**common, **service.binding_response(service.binding(request.device_id))}
    if request.operation == "unbind_session":
        return {
            **common,
            "removed": service.unbind(
                request.device_id, expected_revision=request.binding_revision
            ),
        }
    if request.operation == "cancel_bound_voice":
        assert (
            request.host is not None
            and request.profile is not None
            and request.stored_session_id is not None
            and request.binding_revision is not None
        )
        cancellation_status = service.cancel_bound_voice(
            device_id=request.device_id,
            expected_host=request.host,
            expected_profile=request.profile,
            expected_stored_session_id=request.stored_session_id,
            expected_revision=request.binding_revision,
        )
        return {
            **common,
            "status": cancellation_status,
            "host": request.host,
            "profile": request.profile,
            "stored_session_id": request.stored_session_id,
            "binding_revision": request.binding_revision,
        }
    if request.operation == "create_session":
        assert (
            request.idempotency_key is not None
            and request.host is not None
            and request.profile is not None
            and request.stored_session_id is not None
            and request.binding_revision is not None
        )
        previous = service.binding(request.device_id)
        if previous is None:
            raise BridgeRejected("device is not bound to a Hermes conversation")
        created = service.create_session(
            device_id=request.device_id,
            confirmed=request.confirmed,
            idempotency_key=request.idempotency_key,
            expected_host=request.host,
            expected_profile=request.profile,
            expected_stored_session_id=request.stored_session_id,
            expected_revision=request.binding_revision,
        )
        return {
            **common,
            "status": "completed",
            "host": created.binding.host,
            "profile": created.binding.profile,
            "old_stored_session_id": previous.stored_session_id,
            "stored_session_id": created.binding.stored_session_id,
            "runtime_session_id": created.runtime_session_id,
        }
    if request.operation == "compress_session":
        assert (
            request.host is not None
            and request.profile is not None
            and request.stored_session_id is not None
            and request.binding_revision is not None
        )
        compressed = service.compress_session(
            device_id=request.device_id,
            confirmed=request.confirmed,
            expected_host=request.host,
            expected_profile=request.profile,
            expected_stored_session_id=request.stored_session_id,
            expected_revision=request.binding_revision,
        )
        result = compressed.result
        if result.status == "pending":
            return {
                **common,
                "status": "pending",
                "host": compressed.binding.host,
                "profile": compressed.binding.profile,
                "stored_session_id": compressed.binding.stored_session_id,
                "runtime_session_id": result.runtime_session_id,
                "compression_status": "pending",
                **({"message": result.message} if result.message else {}),
            }
        return {
            **common,
            "status": "completed",
            "host": compressed.binding.host,
            "profile": compressed.binding.profile,
            "stored_session_id": compressed.binding.stored_session_id,
            "runtime_session_id": result.runtime_session_id,
            "compression_status": result.status,
            **(
                {"removed_message_count": result.removed_message_count}
                if result.removed_message_count is not None
                else {}
            ),
            **(
                {"before_messages": result.before_messages}
                if result.before_messages is not None
                else {}
            ),
            **(
                {"after_messages": result.after_messages}
                if result.after_messages is not None
                else {}
            ),
            **({"message": result.message} if result.message else {}),
        }
    assert (
        request.operation in {"dispatch_shared", "dispatch_bound_voice"}
        and request.input is not None
        and request.host is not None
        and request.profile is not None
        and request.stored_session_id is not None
        and request.binding_revision is not None
    )
    send_args: dict[str, object] = {
        "device_id": request.device_id,
        "text": request.input,
        "confirmed": request.confirmed,
        "voice_direct": request.operation == "dispatch_bound_voice",
        "expected_host": request.host,
        "expected_profile": request.profile,
        "expected_stored_session_id": request.stored_session_id,
        "expected_revision": request.binding_revision,
    }
    if request.attachments:
        send_args["attachments"] = request.attachments
    if on_progress is not None:
        send_args["on_progress"] = on_progress
    turn = service.send(**send_args)
    return {
        **common,
        "status": "completed" if turn.status == "complete" else turn.status,
        "host": request.host,
        "stored_session_id": turn.stored_session_id,
        "runtime_session_id": turn.runtime_session_id,
        "output": turn.text,
        **({"user_row_id": turn.user_row_id} if turn.user_row_id is not None else {}),
        **({"error_code": turn.error_code} if turn.error_code is not None else {}),
        **({"error": turn.error_message} if turn.error_message is not None else {}),
    }


def _reject_oversized_line(*, stdout: object) -> None:
    _write_json(
        {
            "schema_version": "1.0",
            "status": "rejected",
            "error": f"request line exceeds {_MAX_REQUEST_LINE_CHARS} characters",
        },
        stream=stdout,
    )


def _write_bounded_response(response: dict[str, object], *, stdout: object) -> None:
    try:
        _write_json(response, stream=stdout)
    except _ResponseTooLarge:
        reduced = {key: value for key, value in response.items() if key != "output"}
        reduced["output_omitted"] = True
        reduced["error"] = "response output exceeded the stdio limit"
        _write_json(reduced, stream=stdout)
    except (TypeError, ValueError):
        _write_json(
            {
                "schema_version": "1.0",
                "status": "failed",
                "error": "bridge response was not finite JSON",
                **(
                    {"operation": response["operation"]}
                    if "operation" in response
                    else {}
                ),
                **(
                    {"session_id": response["session_id"]}
                    if "session_id" in response
                    else {}
                ),
                **({"run_id": response["run_id"]} if "run_id" in response else {}),
            },
            stream=stdout,
        )


def _serve_stdio(
    config: BridgeConfig,
    *,
    environment: Mapping[str, str],
    stdin: object,
    stdout: object,
    transport: RunsTransport | None = None,
    shared: SharedConversationService | None = None,
) -> int:
    selected_transport = transport or HermesRunsTransport(
        config,
        environment=environment,
    )
    service = BridgeService(config, selected_transport)
    shared_service = shared
    while True:
        line = stdin.readline(_MAX_REQUEST_LINE_CHARS + 2)  # type: ignore[attr-defined]
        if line == "":
            break
        if len(line) > _MAX_REQUEST_LINE_CHARS:
            while line and not line.endswith("\n"):
                line = stdin.readline(_MAX_REQUEST_LINE_CHARS + 2)  # type: ignore[attr-defined]
            _reject_oversized_line(stdout=stdout)
            continue
        if "\ufffd" in line:
            _write_json(
                {
                    "schema_version": "1.0",
                    "status": "rejected",
                    "error": "request line is not valid UTF-8",
                },
                stream=stdout,
            )
            continue
        request: BridgeRequest | None = None
        try:
            parsed = json.loads(
                line,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    BridgeContractError(f"non-finite JSON constant: {value}")
                ),
            )
            if not isinstance(parsed, Mapping):
                raise BridgeContractError("bridge request must be an object")
            request = BridgeRequest.from_mapping(parsed)
            if request.caller_id != config.trusted_caller_id:
                raise BridgeRejected("caller_id is not trusted")
            _validate_attachment_files(request, environment)
            if request.operation in _SHARED_OPERATIONS:
                if shared_service is None:
                    shared_service = _shared_service(config, environment, None)
                progress = None
                if (
                    request.operation == "dispatch_bound_voice"
                    and request.stream_progress
                ):
                    def progress(text: str) -> None:
                        _write_bounded_response(
                            {
                                "schema_version": "1.0",
                                "status": "progress",
                                "operation": request.operation,
                                "session_id": request.session_id,
                                "device_id": request.device_id,
                                "host": request.host,
                                "stored_session_id": request.stored_session_id,
                                "output": text,
                            },
                            stdout=stdout,
                        )
                response = _handle_shared_request(
                    shared_service, request, on_progress=progress
                )
            else:
                response = service.handle(request)
        except (
            BindingStoreError,
            json.JSONDecodeError,
            BridgeContractError,
            BridgeRejected,
            DesktopGatewayError,
            DshGatewayError,
            HermesTransportError,
            LifecycleBindingError,
        ) as exc:
            response = _error_response(exc, request=request)
        except Exception:
            response = {
                "schema_version": "1.0",
                "status": "failed",
                "error": "internal bridge error",
                **(
                    {"operation": request.operation, "session_id": request.session_id}
                    if request
                    else {}
                ),
            }
        _write_bounded_response(response, stdout=stdout)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    stdin: object = sys.stdin,
    stdout: object = sys.stdout,
) -> int:
    selected_environment = environment if environment is not None else os.environ
    try:
        if stdin is sys.stdin:
            _configure_standard_stream(stdin, errors="replace")
        if stdout is sys.stdout:
            _configure_standard_stream(stdout, errors="strict")
        args = build_parser().parse_args(argv)
        config = read_config(args.config)
        config_sha256 = _file_sha256(args.config)
        if args.command == "doctor":
            report = _doctor(
                config,
                selected_environment,
                config_sha256=config_sha256,
            )
            _write_json(report, stream=stdout)
            return (
                0
                if report["status"]
                in {"configuration_ready_live_not_checked", "safe_disabled"}
                else 2
            )
        if args.command == "simulate":
            request = read_request(args.request)
            response = BridgeService(config, _SimulationTransport()).handle(request)
            _write_json(response, stream=stdout)
            return 0
        if args.command == "desktop-doctor":
            gateway = _desktop_gateway(selected_environment)
            backend = gateway.backend.describe("default")
            deepseek_report: dict[str, object] = {"configured": False}
            if config.deepseek_harness is not None:
                try:
                    dsh_gateway = _deepseek_gateway(config, selected_environment)
                except DshGatewayError:
                    dsh_gateway = None
                    deepseek_report = {
                        "configured": True,
                        "status": "unavailable",
                        "version": config.deepseek_harness.expected_version,
                    }
                if dsh_gateway is None:
                    if deepseek_report.get("status") != "unavailable":
                        deepseek_report = {
                            "configured": True,
                            "status": "launch_token_unavailable",
                            "version": config.deepseek_harness.expected_version,
                        }
                else:
                    try:
                        facts = dsh_gateway.doctor()
                    except DshGatewayError:
                        deepseek_report = {
                            "configured": True,
                            "status": "unavailable",
                            "version": config.deepseek_harness.expected_version,
                        }
                    else:
                        deepseek_report = {
                            "configured": True,
                            "status": "ready",
                            "version": facts.version,
                            "preset_count": facts.preset_count,
                            "session_count": facts.session_count,
                        }
            _write_json(
                {
                    "schema_version": "1.0",
                    "status": "ready",
                    "backend_transport": backend.transport,
                    "backend_pid": backend.pid,
                    "backend_host": backend.host,
                    "backend_port": backend.port,
                    "credential_mode": backend.credential_mode,
                    "token_present": backend.credential_mode == "published-token",
                    "token_value_exposed": False,
                    "profiles": [route.name for route in config.profiles],
                    "deepseek_harness": deepseek_report,
                },
                stream=stdout,
            )
            return 0
        if args.command == "desktop-list-agents":
            service = _shared_service(config, selected_environment, None)
            _write_json(
                {
                    "schema_version": "1.0",
                    "status": "ok",
                    "agents": list(service.list_agents()),
                },
                stream=stdout,
            )
            return 0
        if args.command == "desktop-list-sessions":
            service = _shared_service(config, selected_environment, None)
            sessions = service.list_sessions(
                host=args.host, profile=args.profile, limit=args.limit
            )
            _write_json(
                {
                    "schema_version": "1.0",
                    "status": "ok",
                    "host": args.host,
                    "profile": args.profile,
                    "sessions": list(sessions),
                },
                stream=stdout,
            )
            return 0
        if args.command == "desktop-cancel-session":
            dsh_gateway = _deepseek_gateway(config, selected_environment)
            if dsh_gateway is None:
                raise DshGatewayError("DSH launch token is unavailable")
            dsh_gateway.cancel_session(args.session_id)
            _write_json(
                {
                    "schema_version": "1.0",
                    "status": "ok",
                    "session_id": args.session_id,
                    "cancelled": True,
                },
                stream=stdout,
            )
            return 0
        if args.command == "desktop-remove-queued-item":
            dsh_gateway = _deepseek_gateway(config, selected_environment)
            if dsh_gateway is None:
                raise DshGatewayError("DSH launch token is unavailable")
            dsh_gateway.remove_queued_item(
                session_id=args.session_id, item_id=args.item_id
            )
            _write_json(
                {
                    "schema_version": "1.0",
                    "status": "ok",
                    "session_id": args.session_id,
                    "item_id": args.item_id,
                    "removed": True,
                },
                stream=stdout,
            )
            return 0
        if args.command in {
            "desktop-bind",
            "desktop-show-binding",
            "desktop-unbind",
            "desktop-send",
        }:
            state_path = args.state or default_binding_store_path(selected_environment)
            if args.command == "desktop-show-binding":
                result = dict(
                    SharedConversationService.binding_response(
                        BindingStore(state_path).get(args.device_id)
                    )
                )
            elif args.command == "desktop-unbind":
                store = BindingStore(state_path)
                with store.device_operation(args.device_id):
                    removed = store.unbind(args.device_id)
                result = {
                    "device_id": args.device_id,
                    "removed": removed,
                }
            else:
                service = _shared_service(config, selected_environment, state_path)
            if args.command == "desktop-bind":
                binding = service.bind(
                    device_id=args.device_id,
                    host=args.host,
                    profile=args.profile,
                    stored_session_id=args.stored_session_id,
                )
                result = dict(service.binding_response(binding))
            elif args.command == "desktop-send":
                expected = service.binding(args.device_id)
                if expected is None:
                    raise BridgeRejected("device is not bound to a conversation")
                turn = service.send(
                    device_id=args.device_id,
                    text=args.text,
                    confirmed=args.confirmed,
                    expected_host=expected.host,
                    expected_profile=expected.profile,
                    expected_stored_session_id=expected.stored_session_id,
                    expected_revision=expected.revision,
                )
                result = {
                    "device_id": args.device_id,
                    "stored_session_id": turn.stored_session_id,
                    "runtime_session_id": turn.runtime_session_id,
                    "turn_status": turn.status,
                    "output": turn.text,
                    "user_row_id": turn.user_row_id,
                }
            _write_json(
                {"schema_version": "1.0", "status": "ok", **result},
                stream=stdout,
            )
            return 0
        if args.confirmed_config_sha256 != config_sha256:
            raise BridgeContractError(
                "confirmed config SHA-256 does not match the current config bytes"
            )
        return _serve_stdio(
            config,
            environment=selected_environment,
            stdin=stdin,
            stdout=stdout,
        )
    except (
        BindingStoreError,
        BridgeContractError,
        BridgeRejected,
        DesktopGatewayError,
        DshGatewayError,
        HermesTransportError,
        LifecycleBindingError,
    ) as exc:
        _write_json(_error_response(exc), stream=stdout)
        return 2


__all__ = ["build_parser", "main"]
