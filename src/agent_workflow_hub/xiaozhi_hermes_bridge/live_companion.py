"""Ephemeral, non-replayable text and audio-state projection for the companion UI."""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .control_plane import ControlPlaneError


QUESTION_LIMIT = 512
ANSWER_LIMIT = 16_384
ANSWER_HEAD_LIMIT = 2_048
TURN_TTL_SECONDS = 5 * 60
COMPLETED_TTL_SECONDS = 60
CAPTURE_TTL_SECONDS = 90
SUBSCRIPTION_TTL_SECONDS = 30
SUBSCRIPTION_RESUME_TTL_SECONDS = 5 * 60
MIN_POLL_INTERVAL_SECONDS = 0.2
MAX_SUBSCRIPTIONS = 64
MAX_RESUME_LEASES = 64


@dataclass(slots=True)
class _LiveProjection:
    device_id: str
    connection_generation: str
    sequence: int
    turn_started_sequence: int
    turn_id: str
    revision: int
    capture_active: bool
    speech_active: bool
    question: str
    answer: str
    question_truncated: bool
    answer_truncated: bool
    answer_head_length: int
    answer_omitted_characters: int
    answer_source: str
    expires_at: float


@dataclass(slots=True)
class _Subscription:
    subscription_id: str
    session_token: str
    device_id: str
    connection_generation: str
    baseline_sequence: int
    resume_token: str
    last_access: float
    last_poll: float


@dataclass(slots=True)
class _ResumeLease:
    resume_token: str
    session_token: str
    device_id: str
    connection_generation: str
    baseline_sequence: int
    expires_at: float


class TransientCompanionBroker:
    """Thread-safe latest-turn projection with per-page no-replay baselines.

    This object deliberately has no persistence API and is not part of the
    control snapshot or durable event stream.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.stream_epoch = uuid.uuid4().hex
        self._clock = clock
        self._sequence = 0
        self._devices: dict[str, _LiveProjection] = {}
        self._subscriptions: dict[str, _Subscription] = {}
        self._resume_leases: dict[str, _ResumeLease] = {}
        self._lock = threading.RLock()

    def register_connection(self, *, device_id: str, connection_generation: str) -> None:
        now = self._clock()
        with self._lock:
            self._reap_locked(now)
            current = self._devices.get(device_id)
            if current is not None and current.connection_generation == connection_generation:
                return
            self._drop_device_subscriptions_locked(device_id)
            sequence = self._next_sequence_locked()
            self._devices[device_id] = _LiveProjection(
                device_id=device_id,
                connection_generation=connection_generation,
                sequence=sequence,
                turn_started_sequence=0,
                turn_id="",
                revision=0,
                capture_active=False,
                speech_active=False,
                question="",
                answer="",
                question_truncated=False,
                answer_truncated=False,
                answer_head_length=0,
                answer_omitted_characters=0,
                answer_source="",
                expires_at=now + TURN_TTL_SECONDS,
            )

    def unregister_connection(self, *, device_id: str, connection_generation: str) -> None:
        with self._lock:
            current = self._devices.get(device_id)
            if current is None or current.connection_generation != connection_generation:
                return
            self._devices.pop(device_id, None)
            self._drop_device_subscriptions_locked(device_id)
            self._next_sequence_locked()

    def start_turn(
        self,
        *,
        device_id: str,
        connection_generation: str,
        capture_active: bool,
    ) -> str | None:
        now = self._clock()
        with self._lock:
            current = self._matching_device_locked(device_id, connection_generation, now)
            if current is None:
                return None
            sequence = self._next_sequence_locked()
            turn_id = uuid.uuid4().hex
            current.sequence = sequence
            current.turn_started_sequence = sequence
            current.turn_id = turn_id
            current.revision = 0
            current.capture_active = capture_active
            current.speech_active = False
            current.question = ""
            current.answer = ""
            current.question_truncated = False
            current.answer_truncated = False
            current.answer_head_length = 0
            current.answer_omitted_characters = 0
            current.answer_source = ""
            current.expires_at = now + (
                CAPTURE_TTL_SECONDS if capture_active else TURN_TTL_SECONDS
            )
            return turn_id

    def set_capture(
        self,
        *,
        device_id: str,
        connection_generation: str,
        active: bool,
    ) -> bool:
        now = self._clock()
        with self._lock:
            current = self._matching_device_locked(device_id, connection_generation, now)
            if current is None:
                return False
            if current.capture_active == active:
                return True
            current.capture_active = active
            current.sequence = self._next_sequence_locked()
            current.expires_at = now + (
                CAPTURE_TTL_SECONDS if active else TURN_TTL_SECONDS
            )
            return True

    def publish_question(
        self,
        *,
        device_id: str,
        connection_generation: str,
        turn_id: str,
        text: str,
        replace: bool = True,
    ) -> bool:
        value, truncated = self._bounded_text(text, QUESTION_LIMIT)
        if not value:
            return False
        now = self._clock()
        with self._lock:
            current = self._matching_turn_locked(
                device_id, connection_generation, turn_id, now
            )
            if current is None:
                return False
            if current.question and not replace:
                return True
            if current.question == value and current.question_truncated == truncated:
                return True
            current.question = value
            current.question_truncated = truncated
            current.revision += 1
            current.sequence = self._next_sequence_locked()
            current.expires_at = now + TURN_TTL_SECONDS
            return True

    def publish_answer(
        self,
        *,
        device_id: str,
        connection_generation: str,
        turn_id: str,
        text: str,
        source: str = "agent",
    ) -> bool:
        value, truncated, head_length, omitted = self._bounded_answer(text)
        if not value:
            return False
        now = self._clock()
        with self._lock:
            current = self._matching_turn_locked(
                device_id, connection_generation, turn_id, now
            )
            if current is None:
                return False
            if (
                current.answer == value
                and current.answer_truncated == truncated
                and current.answer_head_length == head_length
                and current.answer_omitted_characters == omitted
            ):
                return True
            current.answer = value
            current.answer_truncated = truncated
            current.answer_head_length = head_length
            current.answer_omitted_characters = omitted
            current.answer_source = source
            current.revision += 1
            current.sequence = self._next_sequence_locked()
            current.expires_at = now + COMPLETED_TTL_SECONDS
            return True

    def append_spoken_answer(
        self,
        *,
        device_id: str,
        connection_generation: str,
        turn_id: str,
        text: str,
    ) -> bool:
        value = self._normalize_text(text)
        if not value:
            return False
        now = self._clock()
        with self._lock:
            current = self._matching_turn_locked(
                device_id, connection_generation, turn_id, now
            )
            if current is None:
                return False
            if current.answer_source == "agent":
                return True
            separator = "" if not current.answer or current.answer[-1].isspace() else " "
            combined, truncated, head_length, newly_omitted = self._bounded_answer(
                f"{current.answer}{separator}{value}"
            )
            omitted = current.answer_omitted_characters + newly_omitted
            if (
                combined == current.answer
                and truncated == current.answer_truncated
                and omitted == current.answer_omitted_characters
            ):
                return True
            current.answer = combined
            current.answer_truncated = truncated
            current.answer_head_length = head_length
            current.answer_omitted_characters = omitted
            current.answer_source = "speech"
            current.revision += 1
            current.sequence = self._next_sequence_locked()
            current.expires_at = now + COMPLETED_TTL_SECONDS
            return True

    def set_speech(
        self,
        *,
        device_id: str,
        connection_generation: str,
        active: bool,
    ) -> bool:
        now = self._clock()
        with self._lock:
            current = self._matching_device_locked(device_id, connection_generation, now)
            if current is None:
                return False
            if current.speech_active == active:
                return True
            current.speech_active = active
            current.sequence = self._next_sequence_locked()
            current.expires_at = now + (
                TURN_TTL_SECONDS if active else COMPLETED_TTL_SECONDS
            )
            return True

    def subscribe(
        self,
        *,
        session_token: str,
        device_id: str,
        connection_generation: str,
        resume_token: str = "",
        after_sequence: int | None = None,
    ) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            self._reap_locked(now)
            current = self._devices.get(device_id)
            if current is None or current.connection_generation != connection_generation:
                raise ControlPlaneError(
                    "stale_connection",
                    "the selected Lingxi device is no longer connected",
                    http_status=409,
                )
            baseline_sequence = self._sequence
            cursor = self._sequence
            resumed = False
            if resume_token:
                if type(after_sequence) is not int or after_sequence < 0:
                    raise ControlPlaneError(
                        "invalid_request",
                        "after must be a non-negative integer when resuming",
                        http_status=400,
                    )
                lease = self._resume_leases.get(resume_token)
                if (
                    lease is None
                    or lease.expires_at <= now
                    or not secrets.compare_digest(lease.session_token, session_token)
                    or lease.device_id != device_id
                    or lease.connection_generation != connection_generation
                ):
                    raise ControlPlaneError(
                        "resume_unavailable",
                        "live companion resume lease is unavailable",
                        http_status=404,
                    )
                baseline_sequence = lease.baseline_sequence
                cursor = min(max(after_sequence, baseline_sequence), self._sequence)
                resumed = True
                self._resume_leases.pop(resume_token, None)
                for key, existing in list(self._subscriptions.items()):
                    if secrets.compare_digest(existing.resume_token, resume_token):
                        self._subscriptions.pop(key, None)
            elif after_sequence is not None:
                raise ControlPlaneError(
                    "invalid_request",
                    "after is only accepted when resuming",
                    http_status=400,
                )
            while len(self._subscriptions) >= MAX_SUBSCRIPTIONS:
                oldest = min(
                    self._subscriptions.values(), key=lambda item: item.last_access
                )
                self._subscriptions.pop(oldest.subscription_id, None)
            subscription_id = secrets.token_urlsafe(24)
            next_resume_token = secrets.token_urlsafe(32)
            while len(self._resume_leases) >= MAX_RESUME_LEASES:
                oldest_lease = min(
                    self._resume_leases.values(), key=lambda item: item.expires_at
                )
                self._resume_leases.pop(oldest_lease.resume_token, None)
            self._resume_leases[next_resume_token] = _ResumeLease(
                resume_token=next_resume_token,
                session_token=session_token,
                device_id=device_id,
                connection_generation=connection_generation,
                baseline_sequence=baseline_sequence,
                expires_at=now + SUBSCRIPTION_RESUME_TTL_SECONDS,
            )
            self._subscriptions[subscription_id] = _Subscription(
                subscription_id=subscription_id,
                session_token=session_token,
                device_id=device_id,
                connection_generation=connection_generation,
                baseline_sequence=baseline_sequence,
                resume_token=next_resume_token,
                last_access=now,
                last_poll=0.0,
            )
            return {
                "schema_version": "1.0",
                "stream_epoch": self.stream_epoch,
                "subscription": subscription_id,
                "resume_token": next_resume_token,
                "cursor": cursor,
                "resumed": resumed,
            }

    def unsubscribe(
        self, *, session_token: str, subscription_id: str, resume_token: str = ""
    ) -> None:
        with self._lock:
            subscription = self._subscriptions.get(subscription_id)
            if subscription is not None and secrets.compare_digest(
                subscription.session_token, session_token
            ):
                self._subscriptions.pop(subscription_id, None)
                self._resume_leases.pop(subscription.resume_token, None)
            if resume_token:
                lease = self._resume_leases.get(resume_token)
                if lease is not None and secrets.compare_digest(
                    lease.session_token, session_token
                ):
                    self._resume_leases.pop(resume_token, None)

    def drop_session(self, session_token: str) -> None:
        with self._lock:
            for key, subscription in list(self._subscriptions.items()):
                if secrets.compare_digest(subscription.session_token, session_token):
                    self._subscriptions.pop(key, None)
            for key, lease in list(self._resume_leases.items()):
                if secrets.compare_digest(lease.session_token, session_token):
                    self._resume_leases.pop(key, None)

    def poll(
        self,
        *,
        session_token: str,
        subscription_id: str,
        after_sequence: int,
    ) -> dict[str, object]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ControlPlaneError(
                "invalid_request", "after must be a non-negative integer", http_status=400
            )
        now = self._clock()
        with self._lock:
            self._reap_locked(now)
            subscription = self._subscriptions.get(subscription_id)
            if subscription is None or not secrets.compare_digest(
                subscription.session_token, session_token
            ):
                raise ControlPlaneError(
                    "subscription_unavailable",
                    "live companion subscription is unavailable",
                    http_status=404,
                )
            if now - subscription.last_poll < MIN_POLL_INTERVAL_SECONDS:
                raise ControlPlaneError(
                    "live_rate_limited",
                    "live companion polling is too frequent",
                    http_status=429,
                )
            subscription.last_poll = now
            subscription.last_access = now
            lease = self._resume_leases.get(subscription.resume_token)
            if lease is not None:
                lease.expires_at = now + SUBSCRIPTION_RESUME_TTL_SECONDS
            current = self._devices.get(subscription.device_id)
            if (
                current is None
                or current.connection_generation != subscription.connection_generation
            ):
                self._subscriptions.pop(subscription_id, None)
                return {
                    "schema_version": "1.0",
                    "stream_epoch": self.stream_epoch,
                    "cursor": self._sequence,
                    "connected": False,
                    "changed": True,
                }
            if current.sequence <= after_sequence:
                return {
                    "schema_version": "1.0",
                    "stream_epoch": self.stream_epoch,
                    "cursor": self._sequence,
                    "connected": True,
                    "changed": False,
                }
            visible = (
                bool(current.turn_id)
                and current.turn_started_sequence > subscription.baseline_sequence
            )
            return {
                "schema_version": "1.0",
                "stream_epoch": self.stream_epoch,
                "cursor": self._sequence,
                "connected": True,
                "changed": True,
                "projection": {
                    "device_id": current.device_id,
                    "connection_generation": current.connection_generation,
                    "turn_id": current.turn_id if visible else "",
                    "revision": current.revision if visible else 0,
                    "capture_active": current.capture_active,
                    "speech_active": current.speech_active,
                    "question": current.question if visible else "",
                    "answer": current.answer if visible else "",
                    "question_truncated": current.question_truncated if visible else False,
                    "answer_truncated": current.answer_truncated if visible else False,
                    "answer_head_length": current.answer_head_length if visible else 0,
                    "answer_omitted_characters": (
                        current.answer_omitted_characters if visible else 0
                    ),
                },
            }

    def _matching_device_locked(
        self, device_id: str, connection_generation: str, now: float
    ) -> _LiveProjection | None:
        self._reap_locked(now)
        current = self._devices.get(device_id)
        if current is None or current.connection_generation != connection_generation:
            return None
        return current

    def _matching_turn_locked(
        self,
        device_id: str,
        connection_generation: str,
        turn_id: str,
        now: float,
    ) -> _LiveProjection | None:
        current = self._matching_device_locked(device_id, connection_generation, now)
        if current is None or not turn_id or current.turn_id != turn_id:
            return None
        return current

    def _reap_locked(self, now: float) -> None:
        for key, subscription in list(self._subscriptions.items()):
            if now - subscription.last_access > SUBSCRIPTION_TTL_SECONDS:
                self._subscriptions.pop(key, None)
        for key, lease in list(self._resume_leases.items()):
            if lease.expires_at <= now:
                self._resume_leases.pop(key, None)
        for current in self._devices.values():
            if current.turn_id and current.expires_at <= now:
                current.sequence = self._next_sequence_locked()
                current.revision += 1
                current.capture_active = False
                current.speech_active = False
                current.question = ""
                current.answer = ""
                current.question_truncated = False
                current.answer_truncated = False
                current.answer_head_length = 0
                current.answer_omitted_characters = 0
                current.answer_source = ""
                # Keep only the opaque turn identity as a tombstone. A late STT,
                # Agent, or TTS result may still update this same turn, while a
                # subsequently started turn continues to reject the stale id.
                current.expires_at = float("inf")

    def _drop_device_subscriptions_locked(self, device_id: str) -> None:
        for key, subscription in list(self._subscriptions.items()):
            if subscription.device_id == device_id:
                self._subscriptions.pop(key, None)
        for key, lease in list(self._resume_leases.items()):
            if lease.device_id == device_id:
                self._resume_leases.pop(key, None)

    def _next_sequence_locked(self) -> int:
        self._sequence += 1
        return self._sequence

    @staticmethod
    def _normalize_text(value: str) -> str:
        if type(value) is not str:
            return ""
        return (
            value.replace("\x00", "")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )

    @classmethod
    def _bounded_text(cls, value: str, limit: int) -> tuple[str, bool]:
        normalized = cls._normalize_text(value)
        if len(normalized) <= limit:
            return normalized, False
        return normalized[:limit].rstrip(), True

    @classmethod
    def _bounded_answer(cls, value: str) -> tuple[str, bool, int, int]:
        normalized = cls._normalize_text(value)
        if len(normalized) <= ANSWER_LIMIT:
            return normalized, False, 0, 0
        tail_limit = ANSWER_LIMIT - ANSWER_HEAD_LIMIT
        head = normalized[:ANSWER_HEAD_LIMIT]
        tail = normalized[-tail_limit:]
        omitted = len(normalized) - ANSWER_LIMIT
        return f"{head}{tail}", True, len(head), omitted


__all__ = [
    "ANSWER_LIMIT",
    "ANSWER_HEAD_LIMIT",
    "QUESTION_LIMIT",
    "TransientCompanionBroker",
]
