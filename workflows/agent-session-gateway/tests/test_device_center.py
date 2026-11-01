from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_workflow_hub.xiaozhi_hermes_bridge.device_center import (
    DeviceContractError,
    capability_inventory,
    preview_maintenance_plan,
    project_audio_diagnostics,
    project_device_status,
    project_state,
    verify_release_manifest,
)


def _signed_manifest():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    value = {
        "schema_version": "1.0",
        "release_id": "lingxi-2.6.0-7",
        "board": "wildprogrammer-esp32s3-n16r8",
        "version": "2.6.0",
        "anti_rollback": 7,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "artifacts": [
            {
                "kind": "firmware",
                "url": "https://updates.example/lingxi.bin",
                "size_bytes": 2_400_000,
                "sha256": "a" * 64,
            }
        ],
        "key_id": "release-2026",
    }
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    value["signature"] = base64.urlsafe_b64encode(private.sign(payload)).decode("ascii")
    return value, {"release-2026": base64.urlsafe_b64encode(public).decode("ascii")}


def test_capability_inventory_hides_raw_tools_and_hard_blocks_maintenance() -> None:
    rows = capability_inventory(
        [
            "self_get_device_status",
            "self_audio_get_diagnostics",
            "self_audio_speaker_set_volume_verified",
        ],
        generation="generation-1",
        stop_available=True,
    )
    encoded = json.dumps(rows)
    assert "self_get_device_status" not in encoded
    assert next(item for item in rows if item["capability_id"] == "audio.diagnostics.read")[
        "available"
    ] is True
    firmware = next(
        item for item in rows if item["capability_id"] == "maintenance.firmware.update"
    )
    assert firmware["available"] is False
    assert firmware["confirmation"] == "explicit_plan"
    factory = next(
        item for item in rows if item["capability_id"] == "device.factory_reset"
    )
    assert factory["confirmation"] == "physical_only"


def test_status_and_audio_projection_drop_sensitive_or_reject_content() -> None:
    status = project_device_status(
        {
            "audio_speaker": {"volume": 42, "secret": "drop"},
            "network": {
                "type": "wifi",
                "ssid": "private-name",
                "ip": "192.168.1.2",
                "signal": "strong",
                "rssi": -44,
            },
            "unknown": {"token": "secret"},
        }
    )
    assert status == {
        "audio_speaker": {"volume": 42},
        "network": {"type": "wifi", "signal": "strong", "rssi": -44},
    }
    diagnostics = project_audio_diagnostics(
        {
            "input_enabled": True,
            "pcm_seen": True,
            "input_sample_rate": 16000,
            "last_peak": 123,
            "extra": "drop",
        }
    )
    assert diagnostics == {
        "input_enabled": True,
        "pcm_seen": True,
        "input_sample_rate": 16000,
        "last_peak": 123,
    }
    with pytest.raises(DeviceContractError, match="forbidden"):
        project_audio_diagnostics({"raw_pcm": "not-allowed"})


def test_state_projection_separates_device_phase_and_expression() -> None:
    assert project_state("thinking").device_state == "idle"
    assert project_state("thinking").conversation_phase == "thinking"
    assert project_state("thinking").expression == "thinking"
    assert project_state("updating").device_state == "updating"
    with pytest.raises(DeviceContractError):
        project_state("arbitrary")


def test_signed_manifest_verification_and_plan_remain_non_executable() -> None:
    value, keys = _signed_manifest()
    verified = verify_release_manifest(
        value,
        trusted_keys=keys,
        expected_board="wildprogrammer-esp32s3-n16r8",
        minimum_anti_rollback=7,
    )
    plan = preview_maintenance_plan(
        verified, device_id="device-1", generation="generation-1"
    )
    assert plan["executable"] is False
    assert len(plan["plan_sha256"]) == 64
    assert "signed_ota_trust_anchor_unavailable" in plan["blockers"]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda item: item.update(board="another-board"), "board"),
        (lambda item: item.update(anti_rollback=6), "anti-rollback"),
        (
            lambda item: item["artifacts"][0].update(url="http://updates.example/fw.bin"),
            "HTTPS",
        ),
        (lambda item: item["artifacts"][0].update(sha256="b" * 64), "signature"),
    ],
)
def test_manifest_rejects_wrong_target_downgrade_transport_and_tamper(
    mutator, message: str
) -> None:
    value, keys = _signed_manifest()
    changed = copy.deepcopy(value)
    mutator(changed)
    with pytest.raises(DeviceContractError, match=message):
        verify_release_manifest(
            changed,
            trusted_keys=keys,
            expected_board="wildprogrammer-esp32s3-n16r8",
            minimum_anti_rollback=7,
        )
