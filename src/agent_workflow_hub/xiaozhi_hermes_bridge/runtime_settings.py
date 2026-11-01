"""Typed, single-file runtime settings transactions for the Lingxi console."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode


_PLAN_TTL_SECONDS = 600.0
_PLAN_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_BACKUP_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}\.yaml$")
_LLM_MODULES = {
    "bound_agent": "BoundAgentGatewayLLM",
    "local": "OllamaLLM",
    "remote": "DeepSeekGatewayLLM",
}
_ALLOWED_PATCH_FIELDS = frozenset(
    (
        "llm_mode",
        "tts_provider",
        "tts_voice",
        "asr_provider",
        "vad_provider",
        "vad_min_silence_ms",
        "tool_call_timeout_seconds",
    )
)


class RuntimeSettingsError(RuntimeError):
    """A safe, user-facing settings transaction failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SettingsPlan:
    plan_id: str
    base_sha256: str
    candidate_sha256: str
    candidate_bytes: bytes
    changes: tuple[dict[str, object], ...]
    created_at: float


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeSettingsError("invalid_runtime_config", f"{label} must be a mapping")
    if not all(type(key) is str for key in value):
        raise RuntimeSettingsError("invalid_runtime_config", f"{label} keys must be strings")
    return cast(dict[str, object], value)


def _settings_view(value: dict[str, object]) -> dict[str, object]:
    selected = _mapping(value.get("selected_module"), "selected_module")
    llm = _mapping(value.get("LLM"), "LLM")
    tts = _mapping(value.get("TTS"), "TTS")
    vad = _mapping(value.get("VAD"), "VAD")
    selected_llm = selected.get("LLM")
    selected_tts = selected.get("TTS")
    selected_vad = selected.get("VAD")
    selected_asr = selected.get("ASR")
    if not all(
        type(item) is str
        for item in (selected_llm, selected_tts, selected_vad, selected_asr)
    ):
        raise RuntimeSettingsError(
            "invalid_runtime_config", "selected runtime modules must be strings"
        )
    llm_mode = next(
        (mode for mode, module in _LLM_MODULES.items() if module == selected_llm),
        "custom",
    )
    tts_block = _mapping(tts.get(cast(str, selected_tts)), "selected TTS")
    vad_block = _mapping(vad.get(cast(str, selected_vad)), "selected VAD")
    return {
        "llm_mode": llm_mode,
        "llm_module": selected_llm,
        "tts_provider": selected_tts,
        "tts_voice": tts_block.get("voice", ""),
        "asr_provider": selected_asr,
        "vad_provider": selected_vad,
        "vad_min_silence_ms": vad_block.get("min_silence_duration_ms"),
        "tool_call_timeout_seconds": value.get("tool_call_timeout"),
        "available": {
            "llm_modes": [
                mode for mode, module in _LLM_MODULES.items() if module in llm
            ],
            "tts_providers": sorted(tts),
            "asr_providers": sorted(
                set(_mapping(value.get("ASR", {}), "ASR")) | {cast(str, selected_asr)}
            ),
            "vad_providers": sorted(set(vad) | {cast(str, selected_vad)}),
        },
    }


def _safe_load(data: bytes) -> dict[str, object]:
    try:
        value = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeSettingsError(
            "invalid_runtime_config", f"runtime config cannot be parsed: {type(exc).__name__}"
        ) from None
    return _mapping(value, "runtime config")


def _node_for_path(root: Node, path: tuple[str, ...]) -> ScalarNode:
    node = root
    for segment in path:
        if not isinstance(node, MappingNode):
            raise RuntimeSettingsError(
                "unsupported_runtime_config", f"runtime setting path {'/'.join(path)} is unavailable"
            )
        found: Node | None = None
        for key_node, value_node in node.value:
            if isinstance(key_node, ScalarNode) and key_node.value == segment:
                found = value_node
                break
        if found is None:
            raise RuntimeSettingsError(
                "unsupported_runtime_config", f"runtime setting path {'/'.join(path)} is unavailable"
            )
        node = found
    if not isinstance(node, ScalarNode):
        raise RuntimeSettingsError(
            "unsupported_runtime_config", f"runtime setting path {'/'.join(path)} is not scalar"
        )
    return node


def _yaml_scalar(value: object) -> str:
    if type(value) is int:
        return str(value)
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False)
    raise RuntimeSettingsError("invalid_setting", "setting value type is unsupported")


def _patch_bytes(data: bytes, replacements: dict[tuple[str, ...], object]) -> bytes:
    try:
        text = data.decode("utf-8")
        root = yaml.compose(text)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeSettingsError(
            "invalid_runtime_config", f"runtime config cannot be parsed: {type(exc).__name__}"
        ) from None
    if root is None:
        raise RuntimeSettingsError("invalid_runtime_config", "runtime config is empty")
    intervals: list[tuple[int, int, str]] = []
    for path, value in replacements.items():
        node = _node_for_path(root, path)
        intervals.append((node.start_mark.index, node.end_mark.index, _yaml_scalar(value)))
    for start, end, rendered in sorted(intervals, reverse=True):
        text = text[:start] + rendered + text[end:]
    candidate = text.encode("utf-8")
    _safe_load(candidate)
    return candidate


class RuntimeSettingsManager:
    """Preview and atomically apply one reviewed Xiaozhi YAML transaction."""

    def __init__(
        self,
        path: Path,
        *,
        backup_dir: Path,
        loaded_sha256: str | None = None,
    ) -> None:
        if not path.is_absolute() or not path.is_file():
            raise RuntimeSettingsError(
                "invalid_runtime_path", "runtime config must be an existing absolute file"
            )
        if not backup_dir.is_absolute():
            raise RuntimeSettingsError(
                "invalid_backup_path", "backup directory must be absolute"
            )
        self.path = path.resolve()
        self.backup_dir = backup_dir.resolve()
        self._lock = threading.RLock()
        self._plans: dict[str, SettingsPlan] = {}
        initial = self.path.read_bytes()
        self.loaded_sha256 = loaded_sha256 or _sha256(initial)
        self.loaded_values = _settings_view(_safe_load(initial))

    def snapshot(self) -> dict[str, object]:
        data = self.path.read_bytes()
        configured_sha = _sha256(data)
        configured = _settings_view(_safe_load(data))
        return {
            "configured": configured,
            "configured_sha256": configured_sha,
            "loaded": self.loaded_values,
            "loaded_sha256": self.loaded_sha256,
            "observed": {
                "available": False,
                "reason": "device runtime facts are not published yet",
            },
            "pending_restart": configured_sha != self.loaded_sha256,
        }

    def preview(
        self, patch: dict[str, object], *, expected_configured_sha256: str
    ) -> dict[str, object]:
        if not patch or set(patch) - _ALLOWED_PATCH_FIELDS:
            raise RuntimeSettingsError(
                "invalid_patch", "patch contains no supported settings or has unknown fields"
            )
        data = self.path.read_bytes()
        current_sha = _sha256(data)
        if current_sha != expected_configured_sha256:
            raise RuntimeSettingsError(
                "configuration_conflict", "runtime config changed before preview"
            )
        current_mapping = _safe_load(data)
        current = _settings_view(current_mapping)
        replacements = self._validated_replacements(current_mapping, current, patch)
        candidate = _patch_bytes(data, replacements)
        candidate_mapping = _safe_load(candidate)
        candidate_view = _settings_view(candidate_mapping)
        changes = tuple(
            {
                "field": field,
                "before": current.get(field),
                "after": candidate_view.get(field),
            }
            for field in _ALLOWED_PATCH_FIELDS
            if field in candidate_view and current.get(field) != candidate_view.get(field)
        )
        if not changes:
            raise RuntimeSettingsError("no_change", "patch does not change runtime settings")
        plan = SettingsPlan(
            plan_id=secrets.token_hex(16),
            base_sha256=current_sha,
            candidate_sha256=_sha256(candidate),
            candidate_bytes=candidate,
            changes=changes,
            created_at=time.monotonic(),
        )
        with self._lock:
            self._prune_plans_locked()
            self._plans[plan.plan_id] = plan
        return {
            "plan_id": plan.plan_id,
            "base_sha256": plan.base_sha256,
            "candidate_sha256": plan.candidate_sha256,
            "changes": list(plan.changes),
            "restart_required": True,
            "expires_in_seconds": int(_PLAN_TTL_SECONDS),
        }

    def apply(self, *, plan_id: str, candidate_sha256: str) -> dict[str, object]:
        if _PLAN_ID_RE.fullmatch(plan_id) is None:
            raise RuntimeSettingsError("invalid_plan", "settings plan id is invalid")
        with self._lock:
            self._prune_plans_locked()
            plan = self._plans.pop(plan_id, None)
            if plan is None:
                raise RuntimeSettingsError("invalid_plan", "settings plan is absent or expired")
            if plan.candidate_sha256 != candidate_sha256:
                raise RuntimeSettingsError("invalid_plan", "candidate digest does not match plan")
            current = self.path.read_bytes()
            if _sha256(current) != plan.base_sha256:
                raise RuntimeSettingsError(
                    "configuration_conflict", "runtime config changed after preview"
                )
            backup_id = self._write_backup(current)
            self._atomic_replace(plan.candidate_bytes)
            observed = self.path.read_bytes()
            if _sha256(observed) != plan.candidate_sha256:
                raise RuntimeSettingsError(
                    "verification_failed", "runtime config digest does not match candidate"
                )
            return {
                "status": "written_pending_restart",
                "configured_sha256": plan.candidate_sha256,
                "loaded_sha256": self.loaded_sha256,
                "pending_restart": plan.candidate_sha256 != self.loaded_sha256,
                "backup_id": backup_id,
            }

    def rollback(
        self, *, backup_id: str, expected_configured_sha256: str
    ) -> dict[str, object]:
        if _BACKUP_ID_RE.fullmatch(backup_id) is None:
            raise RuntimeSettingsError("invalid_backup", "backup id is invalid")
        backup = (self.backup_dir / backup_id).resolve()
        try:
            backup.relative_to(self.backup_dir)
        except ValueError:
            raise RuntimeSettingsError("invalid_backup", "backup path escaped its root") from None
        if not backup.is_file() or backup.is_symlink():
            raise RuntimeSettingsError("invalid_backup", "backup is unavailable")
        with self._lock:
            current = self.path.read_bytes()
            if _sha256(current) != expected_configured_sha256:
                raise RuntimeSettingsError(
                    "configuration_conflict", "runtime config changed before rollback"
                )
            candidate = backup.read_bytes()
            _settings_view(_safe_load(candidate))
            redo_backup_id = self._write_backup(current)
            self._atomic_replace(candidate)
            configured_sha = _sha256(candidate)
            return {
                "status": "written_pending_restart",
                "configured_sha256": configured_sha,
                "loaded_sha256": self.loaded_sha256,
                "pending_restart": configured_sha != self.loaded_sha256,
                "backup_id": redo_backup_id,
            }

    def _validated_replacements(
        self,
        mapping: dict[str, object],
        current: dict[str, object],
        patch: dict[str, object],
    ) -> dict[tuple[str, ...], object]:
        replacements: dict[tuple[str, ...], object] = {}
        llm = _mapping(mapping.get("LLM"), "LLM")
        tts = _mapping(mapping.get("TTS"), "TTS")
        vad = _mapping(mapping.get("VAD"), "VAD")
        if "llm_mode" in patch:
            mode = patch["llm_mode"]
            if type(mode) is not str or mode not in _LLM_MODULES:
                raise RuntimeSettingsError("invalid_setting", "llm_mode is unsupported")
            module = _LLM_MODULES[cast(str, mode)]
            if module not in llm:
                raise RuntimeSettingsError("invalid_setting", "selected LLM is not configured")
            replacements[("selected_module", "LLM")] = module
        provider = patch.get("tts_provider", current["tts_provider"])
        if type(provider) is not str or provider not in tts:
            raise RuntimeSettingsError("invalid_setting", "tts_provider is not configured")
        if "tts_provider" in patch:
            replacements[("selected_module", "TTS")] = provider
        if "tts_voice" in patch:
            voice = patch["tts_voice"]
            if type(voice) is not str or not voice.strip() or len(voice) > 128:
                raise RuntimeSettingsError("invalid_setting", "tts_voice is invalid")
            _mapping(tts[provider], "selected TTS")
            replacements[("TTS", cast(str, provider), "voice")] = voice.strip()
        for field, section in (("asr_provider", "ASR"), ("vad_provider", "VAD")):
            if field not in patch:
                continue
            value = patch[field]
            available = cast(dict[str, object], current["available"])[f"{section.lower()}_providers"]
            if type(value) is not str or value not in cast(list[object], available):
                raise RuntimeSettingsError("invalid_setting", f"{field} is not configured")
            replacements[("selected_module", section)] = value
        if "vad_min_silence_ms" in patch:
            value = patch["vad_min_silence_ms"]
            selected_vad = patch.get("vad_provider", current["vad_provider"])
            if type(value) is not int or not 200 <= value <= 3000:
                raise RuntimeSettingsError(
                    "invalid_setting", "vad_min_silence_ms must be 200..3000"
                )
            if type(selected_vad) is not str or selected_vad not in vad:
                raise RuntimeSettingsError("invalid_setting", "selected VAD is not configured")
            replacements[("VAD", cast(str, selected_vad), "min_silence_duration_ms")] = value
        if "tool_call_timeout_seconds" in patch:
            value = patch["tool_call_timeout_seconds"]
            if type(value) is not int or not 30 <= value <= 900:
                raise RuntimeSettingsError(
                    "invalid_setting", "tool_call_timeout_seconds must be 30..900"
                )
            replacements[("tool_call_timeout",)] = value
        return replacements

    def _prune_plans_locked(self) -> None:
        now = time.monotonic()
        for plan_id, plan in list(self._plans.items()):
            if now - plan.created_at > _PLAN_TTL_SECONDS:
                self._plans.pop(plan_id, None)

    def _write_backup(self, data: bytes) -> str:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_id = f"{stamp}-{_sha256(data)[:12]}.yaml"
        target = self.backup_dir / backup_id
        if not target.exists():
            fd, temporary = tempfile.mkstemp(
                prefix=f".{backup_id}.", suffix=".tmp", dir=self.backup_dir
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                with contextlib.suppress(OSError):
                    target.chmod(0o600)
            finally:
                with contextlib.suppress(OSError):
                    Path(temporary).unlink()
        return backup_id

    def _atomic_replace(self, data: bytes) -> None:
        mode = self.path.stat().st_mode
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            with contextlib.suppress(OSError):
                os.chmod(temporary, mode)
            os.replace(temporary, self.path)
        except OSError as exc:
            raise RuntimeSettingsError(
                "write_failed", f"runtime config write failed: {type(exc).__name__}"
            ) from None
        finally:
            with contextlib.suppress(OSError):
                Path(temporary).unlink()


__all__ = [
    "RuntimeSettingsError",
    "RuntimeSettingsManager",
    "SettingsPlan",
]
