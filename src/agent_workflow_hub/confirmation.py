"""Session-bound, one-time write confirmations.

The confirmation store turns every eligible external write into a
``needs_user_confirmation`` challenge bound to the exact canonical request and
the current session context.  The challenge is consumed exactly once; any
request or context drift invalidates it and the item is removed.

This module is process-local by design: it never claims to remove a remote
challenge and never exposes private payloads.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Container, Iterable, Mapping
from dataclasses import dataclass, is_dataclass
from itertools import chain
from pathlib import Path
from types import MappingProxyType
from typing import Generic, TypeVar

T = TypeVar("T")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 10_000
_MAX_JSON_INTEGER_BITS = 14_000
_MAX_ID_ATTEMPTS = 32
_MAX_STORE_ISSUED_IDS = 4_096
_MAX_COORDINATOR_ISSUED_SET_IDS = 4_096
_MAX_COORDINATOR_TERMINAL_MEMBERS = 8_192


class ConfirmationError(ValueError):
    """Raised for any confirmation lifecycle violation."""


class _MaterializationLimitError(ConfirmationError):
    """Internal signal that a confirmation collection exceeded its limit."""


def _is_sha256(value: str) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _is_allowed_private_payload(value: object) -> bool:
    if value is None or type(value) is tuple:
        return True
    if isinstance(value, type):
        return False
    if not is_dataclass(value):
        return False
    value_type = type(value)
    if (
        "__dataclass_fields__" not in value_type.__dict__
        or "__dataclass_params__" not in value_type.__dict__
    ):
        return False
    parameters = value_type.__dict__["__dataclass_params__"]
    return parameters is not None and parameters.frozen is True


def _new_unique_token(existing: Container[str]) -> str:
    for _ in range(_MAX_ID_ATTEMPTS):
        candidate = secrets.token_urlsafe(24)
        if candidate not in existing:
            return candidate
    raise ConfirmationError("unable to allocate unique confirmation ID")


def _clone_finite_json(
    value: object,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
    _nodes: list[int] | None = None,
) -> object:
    """Validate and clone a finite JSON-compatible value.

    Accepts ``None``, bool, int, finite float, str, list/tuple, and mappings
    with string keys.  Rejects bytes, paths, sets, non-string keys, NaN,
    infinity, recursive values and custom objects.
    """

    if _depth > _MAX_JSON_DEPTH:
        raise ConfirmationError("invalid finite JSON value")
    if _nodes is None:
        _nodes = [0]
    _nodes[0] += 1
    if _nodes[0] > _MAX_JSON_NODES:
        raise ConfirmationError("invalid finite JSON value")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if value.bit_length() > _MAX_JSON_INTEGER_BITS:
            raise ConfirmationError("invalid finite JSON value")
        return value
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ConfirmationError("invalid finite JSON value") from None
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ConfirmationError("invalid finite JSON value")
        return value
    if type(value) in (bytes, Path, set, frozenset):
        raise ConfirmationError("invalid finite JSON value")
    if _seen is None:
        _seen = set()
    container_id = id(value)
    if container_id in _seen:
        raise ConfirmationError("invalid finite JSON value")
    if type(value) in (list, tuple):
        _seen.add(container_id)
        try:
            return [
                _clone_finite_json(
                    item,
                    _seen=_seen,
                    _depth=_depth + 1,
                    _nodes=_nodes,
                )
                for item in value
            ]
        finally:
            _seen.remove(container_id)
    if isinstance(value, Mapping):
        _seen.add(container_id)
        try:
            clone: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ConfirmationError("invalid finite JSON value")
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError:
                    raise ConfirmationError(
                        "invalid finite JSON value"
                    ) from None
                clone[key] = _clone_finite_json(
                    item,
                    _seen=_seen,
                    _depth=_depth + 1,
                    _nodes=_nodes,
                )
            return clone
        finally:
            _seen.remove(container_id)
    raise ConfirmationError("invalid finite JSON value")


def canonical_request_fingerprint(value: Mapping[str, object]) -> str:
    """Return the canonical SHA-256 fingerprint of a typed request."""

    try:
        if not isinstance(value, Mapping):
            raise ConfirmationError("invalid finite JSON value")
        raw = json.dumps(
            _clone_finite_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = raw.encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except MemoryError:
        raise
    except Exception:
        raise ConfirmationError("invalid finite JSON value") from None


def _freeze(value: object) -> object:
    """Recursively freeze finite JSON into immutable plain containers."""

    try:
        return _freeze_cloned(_clone_finite_json(value))
    except MemoryError:
        raise
    except Exception:
        raise ConfirmationError("invalid finite JSON value") from None


def _freeze_cloned(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_cloned(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_cloned(item) for item in value)
    return value


def _thaw_finite_json(value: object) -> object:
    """Convert frozen containers back to plain finite JSON."""

    if isinstance(value, Mapping):
        return {
            str(key): _thaw_finite_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_thaw_finite_json(item) for item in value]
    return value


@dataclass(frozen=True)
class OperationSummary:
    """User-visible summary of one write operation."""

    target: str
    environment: str
    action: str
    object_ref: str
    parameters: Mapping[str, object]
    risk: str
    rollback_or_reconcile: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        for field in (
            self.target,
            self.environment,
            self.action,
            self.object_ref,
            self.risk,
            self.rollback_or_reconcile,
        ):
            if type(field) is not str or not field.strip():
                raise ConfirmationError("summary field must not be blank")
        if not _is_sha256(self.request_fingerprint):
            raise ConfirmationError("summary fingerprint must be SHA-256")
        object.__setattr__(self, "parameters", _freeze(self.parameters))

    def to_mapping(self) -> dict[str, object]:
        try:
            return {
                "target": self.target,
                "environment": self.environment,
                "action": self.action,
                "object_ref": self.object_ref,
                "parameters": _thaw_finite_json(self.parameters),
                "risk": self.risk,
                "rollback_or_reconcile": self.rollback_or_reconcile,
                "request_fingerprint": self.request_fingerprint,
            }
        except MemoryError:
            raise
        except Exception:
            raise ConfirmationError("invalid operation summary") from None


@dataclass(frozen=True)
class ConfirmationChallenge:
    confirmation_id: str
    request_fingerprint: str
    context_fingerprint: str
    summary: OperationSummary
    expires_at: float

    def __post_init__(self) -> None:
        if (
            type(self.confirmation_id) is not str
            or not self.confirmation_id
        ):
            raise ConfirmationError("confirmation ID must not be blank")
        if not _is_sha256(self.request_fingerprint):
            raise ConfirmationError("request fingerprint must be SHA-256")
        if not _is_sha256(self.context_fingerprint):
            raise ConfirmationError("context fingerprint must be SHA-256")
        if (
            type(self.expires_at) not in (int, float)
            or not math.isfinite(self.expires_at)
        ):
            raise ConfirmationError("expiry must be finite")
        if type(self.summary) is not OperationSummary:
            raise ConfirmationError("summary must be an operation summary")


@dataclass(frozen=True)
class ConsumedConfirmation(Generic[T]):
    challenge: ConfirmationChallenge
    private_payload: T | None


@dataclass(frozen=True)
class _StoredChallenge(Generic[T]):
    challenge: ConfirmationChallenge
    request: Mapping[str, object]
    private_payload: T | None


class SessionConfirmationStore(Generic[T]):
    """Process-local, one-time confirmation store."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        capacity: int = 256,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if (
            type(ttl_seconds) not in (int, float)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ConfirmationError("TTL must be finite and positive")
        if (
            type(capacity) is not int
            or capacity <= 0
        ):
            raise ConfirmationError("capacity must be a positive integer")
        if monotonic is not None and not callable(monotonic):
            raise ConfirmationError("monotonic clock must be callable")
        self._ttl_seconds = float(ttl_seconds)
        self._capacity = capacity
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._lock = threading.RLock()
        self._items: dict[str, _StoredChallenge[T]] = {}
        self._issued_ids: set[str] = set()

    def prepare(
        self,
        *,
        request: Mapping[str, object],
        context_fingerprint: str,
        summary: OperationSummary,
        private_payload: T | None = None,
    ) -> ConfirmationChallenge:
        with self._lock:
            try:
                return self._prepare_locked(
                    request=request,
                    context_fingerprint=context_fingerprint,
                    summary=summary,
                    private_payload=private_payload,
                )
            except MemoryError:
                raise
            except Exception:
                raise ConfirmationError(
                    "invalid confirmation request"
                ) from None

    def _prepare_locked(
        self,
        *,
        request: Mapping[str, object],
        context_fingerprint: str,
        summary: OperationSummary,
        private_payload: T | None,
    ) -> ConfirmationChallenge:
        if type(summary) is not OperationSummary:
            raise ConfirmationError("invalid operation summary")
        if not _is_sha256(context_fingerprint):
            raise ConfirmationError(
                "context fingerprint must be SHA-256"
            )
        if not _is_sha256(summary.request_fingerprint):
            raise ConfirmationError(
                "summary fingerprint must be SHA-256"
            )
        if not _is_allowed_private_payload(private_payload):
            raise ConfirmationError("unsupported private payload")
        if len(self._issued_ids) >= _MAX_STORE_ISSUED_IDS:
            raise ConfirmationError("confirmation identity limit reached")
        request_clone = _clone_finite_json(request)
        fingerprint = canonical_request_fingerprint(request_clone)  # type: ignore[arg-type]
        if fingerprint != summary.request_fingerprint:
            raise ConfirmationError(
                "summary fingerprint does not match request"
            )
        request_snapshot = _freeze_cloned(request_clone)
        self._purge_expired()
        if len(self._items) >= self._capacity:
            raise ConfirmationError("confirmation store is full")
        confirmation_id = _new_unique_token(self._issued_ids)
        challenge = ConfirmationChallenge(
            confirmation_id=confirmation_id,
            request_fingerprint=fingerprint,
            context_fingerprint=context_fingerprint,
            summary=summary,
            expires_at=self._now() + self._ttl_seconds,
        )
        self._items[confirmation_id] = _StoredChallenge(
            challenge=challenge,
            request=request_snapshot,  # type: ignore[arg-type]
            private_payload=private_payload,
        )
        self._issued_ids.add(confirmation_id)
        return challenge

    def consume(
        self,
        confirmation_id: str,
        *,
        request: Mapping[str, object],
        context_fingerprint: str,
    ) -> ConsumedConfirmation[T]:
        with self._lock:
            try:
                if type(confirmation_id) is not str or not confirmation_id:
                    raise ConfirmationError("invalid confirmation ID")
                stored = self._items.pop(confirmation_id, None)
                if stored is None:
                    raise ConfirmationError(
                        "unknown or already consumed confirmation"
                    )
                if stored.challenge.expires_at <= self._now():
                    raise ConfirmationError("confirmation expired")
                fingerprint = canonical_request_fingerprint(request)
                if (
                    fingerprint != stored.challenge.request_fingerprint
                    or not _is_sha256(context_fingerprint)
                    or context_fingerprint
                    != stored.challenge.context_fingerprint
                ):
                    raise ConfirmationError("request or context drift")
                return ConsumedConfirmation(
                    challenge=stored.challenge,
                    private_payload=stored.private_payload,
                )
            except MemoryError:
                raise
            except Exception:
                raise ConfirmationError(
                    "invalid confirmation consumption"
                ) from None

    def invalidate(self, confirmation_id: str) -> bool:
        with self._lock:
            try:
                if type(confirmation_id) is not str or not confirmation_id:
                    raise ConfirmationError("invalid confirmation ID")
                return self._items.pop(confirmation_id, None) is not None
            except MemoryError:
                raise
            except Exception:
                raise ConfirmationError(
                    "invalid confirmation invalidation"
                ) from None

    def invalidate_context(self, context_fingerprint: str) -> int:
        with self._lock:
            try:
                if not _is_sha256(context_fingerprint):
                    raise ConfirmationError("invalid context fingerprint")
                keys = [
                    key
                    for key, stored in self._items.items()
                    if stored.challenge.context_fingerprint
                    == context_fingerprint
                ]
                for key in keys:
                    del self._items[key]
                return len(keys)
            except MemoryError:
                raise
            except Exception:
                raise ConfirmationError(
                    "invalid confirmation invalidation"
                ) from None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _purge_expired(self) -> None:
        now = self._now()
        expired = [
            key
            for key, stored in self._items.items()
            if stored.challenge.expires_at <= now
        ]
        for key in expired:
            del self._items[key]

    def _now(self) -> float:
        now = self._monotonic()
        if (
            type(now) not in (int, float)
            or not math.isfinite(now)
        ):
            raise ConfirmationError("monotonic clock must be finite")
        return float(now)


@dataclass(frozen=True)
class ConfirmationSetMember:
    position: int
    service: str
    confirmation_id: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        if (
            type(self.position) is not int
            or self.position < 0
        ):
            raise ConfirmationError("member position must be non-negative")
        if type(self.service) is not str or not self.service.strip():
            raise ConfirmationError("member service must not be blank")
        if (
            type(self.confirmation_id) is not str
            or not self.confirmation_id
        ):
            raise ConfirmationError("confirmation ID must not be blank")
        if not _is_sha256(self.request_fingerprint):
            raise ConfirmationError("member fingerprint must be SHA-256")


@dataclass(frozen=True)
class ConfirmationSet:
    set_id: str
    set_fingerprint: str
    members: tuple[ConfirmationSetMember, ...]

    def __post_init__(self) -> None:
        if type(self.set_id) is not str or not self.set_id:
            raise ConfirmationError("set ID must not be blank")
        if not _is_sha256(self.set_fingerprint):
            raise ConfirmationError("set fingerprint must be SHA-256")
        if type(self.members) is not tuple:
            raise ConfirmationError("set members must be a tuple")
        if any(
            type(member) is not ConfirmationSetMember
            or member.position != position
            for position, member in enumerate(self.members)
        ):
            raise ConfirmationError("set member positions must be ordered")


@dataclass(frozen=True)
class _ChallengeValue:
    service: str
    confirmation_id: str
    request_fingerprint: str
    context_fingerprint: str
    summary_fingerprint: str
    expires_at: float


@dataclass
class _ConfirmationSetState:
    prepared: ConfirmationSet
    challenges: tuple[_ChallengeValue, ...]
    keys: tuple[tuple[str, str], ...]
    released: bool = False
    next_position: int = 0


def _challenge_value(
    service: object, challenge: object
) -> _ChallengeValue:
    if type(service) is not str or not service.strip():
        raise ConfirmationError("service must not be blank")
    if type(challenge) is not ConfirmationChallenge:
        raise ConfirmationError("invalid confirmation challenge")
    confirmation_id = challenge.confirmation_id
    request_fingerprint = challenge.request_fingerprint
    context_fingerprint = challenge.context_fingerprint
    summary = challenge.summary
    expires_at = challenge.expires_at
    if (
        type(confirmation_id) is not str
        or not confirmation_id
        or not _is_sha256(request_fingerprint)
        or not _is_sha256(context_fingerprint)
        or type(summary) is not OperationSummary
        or isinstance(expires_at, bool)
        or type(expires_at) not in (int, float)
        or not math.isfinite(expires_at)
    ):
        raise ConfirmationError("invalid confirmation challenge")
    summary_mapping = summary.to_mapping()
    summary_request_fingerprint = summary_mapping.get(
        "request_fingerprint"
    )
    if (
        not _is_sha256(summary_request_fingerprint)
        or summary_request_fingerprint != request_fingerprint
    ):
        raise ConfirmationError("invalid confirmation challenge")
    summary_fingerprint = canonical_request_fingerprint(summary_mapping)
    return _ChallengeValue(
        service=service,
        confirmation_id=confirmation_id,
        request_fingerprint=request_fingerprint,
        context_fingerprint=context_fingerprint,
        summary_fingerprint=summary_fingerprint,
        expires_at=float(expires_at),
    )


def _members_fingerprint(
    members: tuple[ConfirmationSetMember, ...],
) -> str:
    return canonical_request_fingerprint(
        {
            "members": [
                {
                    "position": member.position,
                    "service": member.service,
                    "confirmation_id": member.confirmation_id,
                    "request_fingerprint": member.request_fingerprint,
                }
                for member in members
            ]
        }
    )


def _bounded_materialize(
    values: Iterable[T], *, limit: int
) -> tuple[T, ...]:
    materialized: list[T] = []
    for value in values:
        if len(materialized) >= limit:
            raise _MaterializationLimitError(
                "confirmation collection limit exceeded"
            )
        materialized.append(value)
    return tuple(materialized)


def _prepared_set_keys(
    prepared_set: object,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    if (
        type(prepared_set) is not ConfirmationSet
        or type(prepared_set.set_id) is not str
        or not prepared_set.set_id
        or not _is_sha256(prepared_set.set_fingerprint)
        or type(prepared_set.members) is not tuple
    ):
        raise ConfirmationError("invalid confirmation set")
    if len(prepared_set.members) > _MAX_COORDINATOR_TERMINAL_MEMBERS:
        raise _MaterializationLimitError(
            "confirmation collection limit exceeded"
        )
    keys: list[tuple[str, str]] = []
    for position, member in enumerate(prepared_set.members):
        if (
            type(member) is not ConfirmationSetMember
            or member.position != position
            or type(member.service) is not str
            or not member.service.strip()
            or type(member.confirmation_id) is not str
            or not member.confirmation_id
            or not _is_sha256(member.request_fingerprint)
        ):
            raise ConfirmationError("invalid confirmation set")
        keys.append((member.service, member.confirmation_id))
    if len(set(keys)) != len(keys):
        raise ConfirmationError("invalid confirmation set")
    if _members_fingerprint(prepared_set.members) != prepared_set.set_fingerprint:
        raise ConfirmationError("invalid confirmation set")
    return prepared_set.set_id, tuple(keys)


def _current_set_values(
    current_challenges: Iterable[tuple[str, ConfirmationChallenge]],
) -> tuple[tuple[_ChallengeValue, ...], tuple[tuple[str, str], ...]]:
    materialized = _bounded_materialize(
        current_challenges,
        limit=_MAX_COORDINATOR_TERMINAL_MEMBERS,
    )
    values = tuple(
        _challenge_value(service, challenge)
        for service, challenge in materialized
    )
    return values, tuple(
        (value.service, value.confirmation_id) for value in values
    )


class SessionConfirmationCoordinator:
    """Tracks ordered multi-service confirmation sets and deny lists."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sets: dict[str, _ConfirmationSetState] = {}
        self._member_sets: dict[tuple[str, str], str] = {}
        self._issued_set_ids: set[str] = set()
        self._terminal_member_keys: set[tuple[str, str]] = set()
        self._creation_closed = False

    def prepare_set(
        self, challenges: Iterable[tuple[str, ConfirmationChallenge]]
    ) -> ConfirmationSet:
        try:
            return self._prepare_set(challenges)
        except MemoryError:
            raise
        except Exception:
            raise ConfirmationError(
                "invalid confirmation set preparation"
            ) from None

    def _prepare_set(
        self, challenges: Iterable[tuple[str, ConfirmationChallenge]]
    ) -> ConfirmationSet:
        with self._lock:
            if self._creation_closed:
                raise ConfirmationError("confirmation coordinator is closed")
        try:
            materialized = _bounded_materialize(
                challenges,
                limit=_MAX_COORDINATOR_TERMINAL_MEMBERS,
            )
        except _MaterializationLimitError:
            with self._lock:
                self._creation_closed = True
            raise
        challenge_values = tuple(
            _challenge_value(service, challenge)
            for service, challenge in materialized
        )
        members = tuple(
            ConfirmationSetMember(
                position=position,
                service=value.service,
                confirmation_id=value.confirmation_id,
                request_fingerprint=value.request_fingerprint,
            )
            for position, value in enumerate(challenge_values)
        )
        keys = tuple(
            (value.service, value.confirmation_id)
            for value in challenge_values
        )
        if len(set(keys)) != len(keys):
            raise ConfirmationError("duplicate confirmation set member")
        set_fingerprint = _members_fingerprint(members)
        with self._lock:
            if self._creation_closed:
                raise ConfirmationError("confirmation coordinator is closed")
            if any(key in self._terminal_member_keys for key in keys):
                raise ConfirmationError("confirmation member is terminal")
            if any(key in self._member_sets for key in keys):
                raise ConfirmationError("confirmation already belongs to a set")
            if len(self._issued_set_ids) >= _MAX_COORDINATOR_ISSUED_SET_IDS:
                self._creation_closed = True
                raise ConfirmationError("confirmation coordinator is closed")
            reserved_keys = set(self._terminal_member_keys)
            reserved_keys.update(self._member_sets)
            for key in keys:
                if key in reserved_keys:
                    continue
                if (
                    len(reserved_keys)
                    >= _MAX_COORDINATOR_TERMINAL_MEMBERS
                ):
                    self._creation_closed = True
                    raise ConfirmationError(
                        "confirmation coordinator is closed"
                    )
                reserved_keys.add(key)
            set_id = _new_unique_token(self._issued_set_ids)
            prepared = ConfirmationSet(
                set_id=set_id,
                set_fingerprint=set_fingerprint,
                members=members,
            )
            snapshot = ConfirmationSet(
                set_id=set_id,
                set_fingerprint=set_fingerprint,
                members=tuple(
                    ConfirmationSetMember(
                        position=member.position,
                        service=member.service,
                        confirmation_id=member.confirmation_id,
                        request_fingerprint=member.request_fingerprint,
                    )
                    for member in members
                ),
            )
            self._sets[set_id] = _ConfirmationSetState(
                prepared=snapshot,
                challenges=challenge_values,
                keys=keys,
            )
            self._member_sets.update({key: set_id for key in keys})
            self._issued_set_ids.add(set_id)
            if (
                len(self._issued_set_ids)
                >= _MAX_COORDINATOR_ISSUED_SET_IDS
                or len(reserved_keys)
                >= _MAX_COORDINATOR_TERMINAL_MEMBERS
            ):
                self._creation_closed = True
            return prepared

    def validate_set(
        self,
        current_challenges: Iterable[tuple[str, ConfirmationChallenge]],
        prepared_set: ConfirmationSet,
    ) -> bool:
        try:
            set_id, prepared_keys = _prepared_set_keys(prepared_set)
        except BaseException as error:
            with self._lock:
                if isinstance(error, _MaterializationLimitError):
                    self._creation_closed = True
                self._deny_all_active(())
            if isinstance(error, MemoryError) or not isinstance(
                error, Exception
            ):
                raise
            return False
        try:
            current_values, current_keys = _current_set_values(
                current_challenges
            )
        except BaseException as error:
            with self._lock:
                if isinstance(error, _MaterializationLimitError):
                    self._creation_closed = True
                self._deny_all_active(prepared_keys)
            if isinstance(error, MemoryError) or not isinstance(
                error, Exception
            ):
                raise
            return False
        try:
            with self._lock:
                state = self._sets.get(set_id)
                if state is None:
                    self._deny_all_active(
                        chain(prepared_keys, current_keys)
                    )
                    return False
                if (
                    state.released
                    or prepared_set != state.prepared
                    or current_values != state.challenges
                ):
                    self._deny_sets_and_keys(
                        (set_id,),
                        chain(state.keys, prepared_keys, current_keys),
                    )
                    return False
                state.released = True
                if not state.keys:
                    self._remove_active_set(set_id)
                    return True
                return True
        except BaseException as error:
            with self._lock:
                self._deny_all_active(chain(prepared_keys, current_keys))
            if isinstance(error, MemoryError) or not isinstance(
                error, Exception
            ):
                raise
            return False

    def may_replay(self, service: str, confirmation_id: str) -> bool:
        if (
            type(service) is not str
            or not service
            or type(confirmation_id) is not str
            or not confirmation_id
        ):
            return False
        with self._lock:
            key = (service, confirmation_id)
            set_id = self._member_sets.get(key)
            if set_id is None:
                return False
            state = self._sets.get(set_id)
            if state is None or not state.released:
                return False
            if key in state.keys[: state.next_position]:
                return False
            if state.keys[state.next_position] != key:
                self._deny_sets_and_keys((set_id,), state.keys)
                return False
            self._record_terminal_keys((key,))
            state.next_position += 1
            if state.next_position == len(state.keys):
                self._remove_active_set(set_id)
            return True

    def _deny_all_active(
        self, extra_keys: Iterable[tuple[str, str]]
    ) -> None:
        self._deny_sets_and_keys(tuple(self._sets), extra_keys)

    def _deny_sets_and_keys(
        self,
        set_ids: Iterable[str],
        keys: Iterable[tuple[str, str]],
    ) -> None:
        terminal_keys: set[tuple[str, str]] = set()
        overflow = False
        for key in keys:
            if key in terminal_keys:
                continue
            if (
                len(terminal_keys)
                >= _MAX_COORDINATOR_TERMINAL_MEMBERS
            ):
                overflow = True
                break
            terminal_keys.add(key)
        affected_set_ids = set(set_ids)
        if overflow:
            self._creation_closed = True
            affected_set_ids = set(self._sets)
        else:
            affected_set_ids.update(
                set_id
                for key in terminal_keys
                if (set_id := self._member_sets.get(key)) is not None
            )
        for set_id in tuple(affected_set_ids):
            state = self._sets.get(set_id)
            if state is not None:
                for key in state.keys:
                    if key in terminal_keys:
                        continue
                    if (
                        len(terminal_keys)
                        >= _MAX_COORDINATOR_TERMINAL_MEMBERS
                    ):
                        overflow = True
                        break
                    terminal_keys.add(key)
            if overflow:
                break
        if overflow:
            self._creation_closed = True
            affected_set_ids = set(self._sets)
            for state in self._sets.values():
                for key in state.keys:
                    if key in terminal_keys:
                        continue
                    if (
                        len(terminal_keys)
                        >= _MAX_COORDINATOR_TERMINAL_MEMBERS
                    ):
                        break
                    terminal_keys.add(key)
        for set_id in tuple(affected_set_ids):
            self._remove_active_set(set_id)
        self._record_terminal_keys(terminal_keys)

    def _remove_active_set(self, set_id: str) -> None:
        state = self._sets.pop(set_id, None)
        if state is None:
            return
        for key in state.keys:
            if self._member_sets.get(key) == set_id:
                del self._member_sets[key]

    def _record_terminal_keys(
        self, keys: Iterable[tuple[str, str]]
    ) -> None:
        for key in keys:
            if key in self._terminal_member_keys:
                continue
            if (
                len(self._terminal_member_keys)
                >= _MAX_COORDINATOR_TERMINAL_MEMBERS
            ):
                self._creation_closed = True
                continue
            self._terminal_member_keys.add(key)
            if (
                len(self._terminal_member_keys)
                >= _MAX_COORDINATOR_TERMINAL_MEMBERS
            ):
                self._creation_closed = True
