"""Safe device-capability, state, diagnostics, and release-preview contracts.

This module deliberately contains no device mutation path.  It is the software
boundary used by the Lingxi control plane while signed OTA, dual-slot assets,
and authenticated provisioning are still blocked by their firmware gates.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import cast
from urllib.parse import urlsplit


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
_ARTIFACT_KINDS = frozenset(("firmware", "assets"))


class DeviceContractError(ValueError):
    """Raised when a device-center value cannot cross the public boundary."""


@dataclass(frozen=True, slots=True)
class StateProjection:
    device_state: str
    conversation_phase: str
    expression: str
    expression_version: str = "1.0"


_STATE_PROJECTIONS = {
    "offline": StateProjection("offline", "none", "offline"),
    "provisioning": StateProjection("provisioning", "none", "pairing"),
    "idle": StateProjection("idle", "none", "neutral"),
    "listening": StateProjection("idle", "capturing", "listening"),
    "transcribing": StateProjection("idle", "transcribing", "listening"),
    "thinking": StateProjection("idle", "thinking", "thinking"),
    "waiting_confirmation": StateProjection(
        "idle", "waiting_confirmation", "question"
    ),
    "speaking": StateProjection("speaking", "playing", "speaking"),
    "success": StateProjection("idle", "none", "happy"),
    "interrupted": StateProjection("idle", "interrupted", "neutral"),
    "audio_testing": StateProjection("idle", "none", "audio_testing"),
    "updating": StateProjection("updating", "none", "updating"),
    "error": StateProjection("error", "none", "error"),
}


def project_state(state: str) -> StateProjection:
    """Map legacy runtime states to the versioned device/phase/expression model."""

    try:
        return _STATE_PROJECTIONS[state]
    except KeyError:
        raise DeviceContractError("device state is unsupported") from None


@dataclass(frozen=True, slots=True)
class Capability:
    capability_id: str
    version: str
    available: bool
    effect: str
    confirmation: str
    source: str
    observed_generation: str
    constraints: Mapping[str, object]
    blocker: str = ""

    def public(self) -> dict[str, object]:
        return asdict(self)


_TOOL_CAPABILITIES: tuple[tuple[frozenset[str], dict[str, object]], ...] = (
    (
        frozenset(("self_get_device_status",)),
        {
            "capability_id": "device.status.read",
            "effect": "read",
            "confirmation": "none",
            "constraints": {},
        },
    ),
    (
        frozenset(("self_audio_get_diagnostics",)),
        {
            "capability_id": "audio.diagnostics.read",
            "effect": "read",
            "confirmation": "none",
            "constraints": {"contains_audio": False},
        },
    ),
    (
        frozenset(
            (
                "self_audio_speaker_set_volume_verified",
                "self_audio_speaker_set_volume",
            )
        ),
        {
            "capability_id": "speaker.volume.set",
            "effect": "write",
            "confirmation": "none",
            "constraints": {"minimum": 0, "maximum": 100},
        },
    ),
    (
        frozenset(("self_screen_set_brightness",)),
        {
            "capability_id": "screen.brightness.set",
            "effect": "write",
            "confirmation": "none",
            "constraints": {"minimum": 0, "maximum": 100},
        },
    ),
    (
        frozenset(("self_screen_set_theme",)),
        {
            "capability_id": "screen.theme.set",
            "effect": "write",
            "confirmation": "none",
            "constraints": {"values": ["light", "dark"]},
        },
    ),
)

_BLOCKED_MAINTENANCE = (
    (
        "maintenance.firmware.update",
        "explicit_plan",
        "signed_ota_trust_anchor_unavailable",
    ),
    (
        "maintenance.assets.update",
        "explicit_plan",
        "dual_slot_assets_unavailable",
    ),
    (
        "network.provisioning.enter",
        "physical_only",
        "authenticated_provisioning_unavailable",
    ),
    (
        "device.reboot",
        "explicit_plan",
        "device_acknowledged_confirmation_unavailable",
    ),
    (
        "device.factory_reset",
        "physical_only",
        "remote_factory_reset_forbidden",
    ),
)


def capability_inventory(
    tool_names: Sequence[str], *, generation: str, stop_available: bool
) -> list[dict[str, object]]:
    """Return stable public capabilities without exposing raw MCP tool names."""

    if not generation or len(generation) > 128:
        raise DeviceContractError("connection generation is invalid")
    available_tools = frozenset(tool_names)
    rows: list[Capability] = []
    if stop_available:
        rows.append(
            Capability(
                capability_id="speech.stop",
                version="1.0",
                available=True,
                effect="transient_write",
                confirmation="none",
                source="xiaozhi_runtime",
                observed_generation=generation,
                constraints={"result_states": ["requested", "acknowledged", "outcome_unknown"]},
            )
        )
    for aliases, template in _TOOL_CAPABILITIES:
        rows.append(
            Capability(
                capability_id=cast(str, template["capability_id"]),
                version="1.0",
                available=bool(aliases & available_tools),
                effect=cast(str, template["effect"]),
                confirmation=cast(str, template["confirmation"]),
                source="device_mcp",
                observed_generation=generation,
                constraints=cast(Mapping[str, object], template["constraints"]),
                blocker="" if aliases & available_tools else "device_tool_unavailable",
            )
        )
    for capability_id, confirmation, blocker in _BLOCKED_MAINTENANCE:
        rows.append(
            Capability(
                capability_id=capability_id,
                version="1.0",
                available=False,
                effect="maintenance",
                confirmation=confirmation,
                source="policy",
                observed_generation=generation,
                constraints={},
                blocker=blocker,
            )
        )
    return [item.public() for item in rows]


def _bounded_number(
    value: object, *, minimum: float, maximum: float
) -> int | float | None:
    if type(value) not in {int, float}:
        return None
    number = cast(float, value)
    if not minimum <= number <= maximum:
        return None
    return value


def project_device_status(value: Mapping[str, object]) -> dict[str, object]:
    """Project a status response and drop SSIDs, addresses, IDs, and unknown data."""

    result: dict[str, object] = {}
    speaker = value.get("audio_speaker")
    if isinstance(speaker, Mapping):
        volume = _bounded_number(speaker.get("volume"), minimum=0, maximum=100)
        result["audio_speaker"] = {"volume": volume} if volume is not None else {}
    screen = value.get("screen")
    if isinstance(screen, Mapping):
        projected_screen: dict[str, object] = {}
        brightness = _bounded_number(screen.get("brightness"), minimum=0, maximum=100)
        if brightness is not None:
            projected_screen["brightness"] = brightness
        theme = screen.get("theme")
        if theme in {"light", "dark"}:
            projected_screen["theme"] = theme
        result["screen"] = projected_screen
    battery = value.get("battery")
    if isinstance(battery, Mapping):
        level = _bounded_number(battery.get("level"), minimum=0, maximum=100)
        projected_battery: dict[str, object] = {}
        if level is not None:
            projected_battery["level"] = level
        if type(battery.get("charging")) is bool:
            projected_battery["charging"] = battery["charging"]
        result["battery"] = projected_battery
    network = value.get("network")
    if isinstance(network, Mapping):
        projected_network: dict[str, object] = {}
        if network.get("type") in {"wifi", "ethernet", "cellular"}:
            projected_network["type"] = network["type"]
        if network.get("signal") in {"weak", "medium", "strong"}:
            projected_network["signal"] = network["signal"]
        rssi = _bounded_number(network.get("rssi"), minimum=-150, maximum=0)
        if rssi is not None:
            projected_network["rssi"] = rssi
        result["network"] = projected_network
    return result


def project_audio_diagnostics(value: Mapping[str, object]) -> dict[str, object]:
    """Allow metrics and booleans only; raw audio and transcript fields are rejected."""

    allowed_bool = {
        "input_enabled",
        "output_enabled",
        "duplex",
        "pcm_seen",
        "voice_detected",
        "wake_word_running",
        "audio_processor_running",
    }
    limits = {
        "input_sample_rate": (1, 192_000),
        "output_sample_rate": (1, 192_000),
        "volume": (0, 100),
        "input_gain": (-100, 100),
        "last_rms": (0, 32_768),
        "last_peak": (0, 32_768),
        "empty_reads": (0, 2_147_483_647),
        "encode_drops": (0, 2_147_483_647),
    }
    forbidden = {"audio", "pcm", "transcript", "prompt", "password", "token", "ssid"}
    for key in value:
        lowered = key.lower()
        if any(term in lowered for term in forbidden) and key != "pcm_seen":
            raise DeviceContractError("audio diagnostics contain a forbidden field")
    result: dict[str, object] = {}
    for key in allowed_bool:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key, (minimum, maximum) in limits.items():
        projected = _bounded_number(value.get(key), minimum=minimum, maximum=maximum)
        if projected is not None:
            result[key] = projected
    return result


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    kind: str
    url: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedReleaseManifest:
    schema_version: str
    release_id: str
    board: str
    version: str
    anti_rollback: int
    expires_at: str
    artifacts: tuple[ReleaseArtifact, ...]
    key_id: str
    signature: str

    def public(self) -> dict[str, object]:
        value = asdict(self)
        value["artifacts"] = [asdict(item) for item in self.artifacts]
        return value


def _manifest_payload(value: Mapping[str, object]) -> bytes:
    payload = {key: item for key, item in value.items() if key != "signature"}
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def verify_release_manifest(
    value: Mapping[str, object],
    *,
    trusted_keys: Mapping[str, str],
    expected_board: str,
    minimum_anti_rollback: int,
    now: datetime | None = None,
) -> VerifiedReleaseManifest:
    """Validate and verify a closed Ed25519 release manifest.

    Public keys and signatures are URL-safe/base64 encoded raw Ed25519 bytes.
    Artifact URLs are HTTPS-only.  Verification does not download or mutate.
    """

    required = {
        "schema_version",
        "release_id",
        "board",
        "version",
        "anti_rollback",
        "expires_at",
        "artifacts",
        "key_id",
        "signature",
    }
    if set(value) != required or value.get("schema_version") != "1.0":
        raise DeviceContractError("release manifest fields are invalid")
    release_id = value.get("release_id")
    board = value.get("board")
    version = value.get("version")
    anti_rollback = value.get("anti_rollback")
    expires_at = value.get("expires_at")
    key_id = value.get("key_id")
    signature = value.get("signature")
    if not all(type(item) is str for item in (release_id, board, version, expires_at, key_id, signature)):
        raise DeviceContractError("release manifest string fields are invalid")
    release_id = cast(str, release_id)
    board = cast(str, board)
    version = cast(str, version)
    expires_at = cast(str, expires_at)
    key_id = cast(str, key_id)
    signature = cast(str, signature)
    if _ID_RE.fullmatch(release_id) is None or _ID_RE.fullmatch(key_id) is None:
        raise DeviceContractError("release identity is invalid")
    if board != expected_board or _ID_RE.fullmatch(board) is None:
        raise DeviceContractError("release board does not match the device")
    if _VERSION_RE.fullmatch(version) is None:
        raise DeviceContractError("release version is invalid")
    if type(anti_rollback) is not int or anti_rollback < minimum_anti_rollback:
        raise DeviceContractError("release anti-rollback sequence is stale")
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        raise DeviceContractError("release expiry is invalid") from None
    if expires.tzinfo is None:
        raise DeviceContractError("release expiry must include a timezone")
    current = now or datetime.now(timezone.utc)
    if expires.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
        raise DeviceContractError("release manifest has expired")
    raw_artifacts = value.get("artifacts")
    if not isinstance(raw_artifacts, list) or not 1 <= len(raw_artifacts) <= 2:
        raise DeviceContractError("release artifacts are invalid")
    artifacts: list[ReleaseArtifact] = []
    seen_kinds: set[str] = set()
    for raw in raw_artifacts:
        if not isinstance(raw, Mapping) or set(raw) != {"kind", "url", "size_bytes", "sha256"}:
            raise DeviceContractError("release artifact fields are invalid")
        kind, url, size_bytes, sha256 = (
            raw.get("kind"),
            raw.get("url"),
            raw.get("size_bytes"),
            raw.get("sha256"),
        )
        if kind not in _ARTIFACT_KINDS or kind in seen_kinds:
            raise DeviceContractError("release artifact kind is invalid")
        if type(url) is not str or urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            raise DeviceContractError("release artifact URL must use HTTPS")
        if type(size_bytes) is not int or not 1 <= size_bytes <= 32 * 1024 * 1024:
            raise DeviceContractError("release artifact size is invalid")
        if type(sha256) is not str or _SHA256_RE.fullmatch(sha256) is None:
            raise DeviceContractError("release artifact digest is invalid")
        seen_kinds.add(cast(str, kind))
        artifacts.append(
            ReleaseArtifact(cast(str, kind), cast(str, url), size_bytes, cast(str, sha256))
        )
    encoded_key = trusted_keys.get(key_id)
    if encoded_key is None:
        raise DeviceContractError("release signing key is not trusted")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public_key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(encoded_key))
        public_key.verify(base64.urlsafe_b64decode(signature), _manifest_payload(value))
    except InvalidSignature:
        raise DeviceContractError("release signature is invalid") from None
    except (ImportError, ValueError):
        raise DeviceContractError("release signature verifier is unavailable") from None
    return VerifiedReleaseManifest(
        schema_version="1.0",
        release_id=release_id,
        board=board,
        version=version,
        anti_rollback=cast(int, anti_rollback),
        expires_at=expires.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        artifacts=tuple(artifacts),
        key_id=key_id,
        signature=signature,
    )


def preview_maintenance_plan(
    manifest: VerifiedReleaseManifest, *, device_id: str, generation: str
) -> dict[str, object]:
    """Create a deterministic non-executable plan while firmware gates remain closed."""

    if _ID_RE.fullmatch(device_id) is None or _ID_RE.fullmatch(generation) is None:
        raise DeviceContractError("maintenance target identity is invalid")
    basis = {
        "schema_version": "1.0",
        "device_id": device_id,
        "connection_generation": generation,
        "release_id": manifest.release_id,
        "board": manifest.board,
        "version": manifest.version,
        "anti_rollback": manifest.anti_rollback,
        "artifacts": [asdict(item) for item in manifest.artifacts],
        "executable": False,
        "blockers": [
            "signed_ota_trust_anchor_unavailable",
            "dual_slot_assets_unavailable",
            "device_acknowledged_confirmation_unavailable",
        ],
    }
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**basis, "plan_sha256": digest}


__all__ = [
    "Capability",
    "DeviceContractError",
    "ReleaseArtifact",
    "StateProjection",
    "VerifiedReleaseManifest",
    "capability_inventory",
    "preview_maintenance_plan",
    "project_audio_diagnostics",
    "project_device_status",
    "project_state",
    "verify_release_manifest",
]
