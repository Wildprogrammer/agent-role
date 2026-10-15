from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Literal


class LifecyclePhase(str, Enum):
    CHECK_ALREADY_COMPLETE = "check-already-complete"
    CHECK_PRECONDITION = "check-precondition"
    RUN_RECOVERY = "run-recovery"
    RECHECK_PRECONDITION = "recheck-precondition"
    RUN_ACTION = "run-action"
    CHECK_POSTCONDITION = "check-postcondition"
    COMPLETE = "complete"
    FAILED = "failed"


LifecycleEvent = Literal[
    "assertion-passed",
    "assertion-failed",
    "action-passed",
    "action-failed",
    "action-unknown",
]


@dataclass(frozen=True)
class LifecycleState:
    phase: LifecyclePhase
    has_precondition: bool
    has_recovery: bool
    recovery_used: bool = False
    outcome: str | None = None
    error: str | None = None


def initial_state(
    *,
    has_already_complete: bool,
    has_precondition: bool,
    has_recovery: bool,
) -> LifecycleState:
    if has_already_complete:
        phase = LifecyclePhase.CHECK_ALREADY_COMPLETE
    elif has_precondition:
        phase = LifecyclePhase.CHECK_PRECONDITION
    else:
        phase = LifecyclePhase.RUN_ACTION
    return LifecycleState(
        phase=phase,
        has_precondition=has_precondition,
        has_recovery=has_recovery,
    )


def _failed(state: LifecycleState, error: str) -> LifecycleState:
    return replace(
        state,
        phase=LifecyclePhase.FAILED,
        outcome="failed",
        error=error,
    )


def transition(state: LifecycleState, event: LifecycleEvent) -> LifecycleState:
    if state.phase in {LifecyclePhase.COMPLETE, LifecyclePhase.FAILED}:
        raise ValueError("terminal lifecycle state cannot transition")

    if state.phase is LifecyclePhase.CHECK_ALREADY_COMPLETE:
        if event == "assertion-passed":
            return replace(
                state,
                phase=LifecyclePhase.COMPLETE,
                outcome="already-complete",
            )
        if event == "assertion-failed":
            return replace(
                state,
                phase=(
                    LifecyclePhase.CHECK_PRECONDITION
                    if state.has_precondition
                    else LifecyclePhase.RUN_ACTION
                ),
            )

    elif state.phase is LifecyclePhase.CHECK_PRECONDITION:
        if event == "assertion-passed":
            return replace(state, phase=LifecyclePhase.RUN_ACTION)
        if event == "assertion-failed":
            if state.has_recovery and not state.recovery_used:
                return replace(
                    state,
                    phase=LifecyclePhase.RUN_RECOVERY,
                    recovery_used=True,
                )
            return _failed(state, "component_precondition_failed")

    elif state.phase is LifecyclePhase.RUN_RECOVERY:
        if event == "action-passed":
            return replace(state, phase=LifecyclePhase.RECHECK_PRECONDITION)
        if event in {"action-failed", "action-unknown"}:
            return _failed(state, "component_recovery_failed")

    elif state.phase is LifecyclePhase.RECHECK_PRECONDITION:
        if event == "assertion-passed":
            return replace(state, phase=LifecyclePhase.RUN_ACTION)
        if event == "assertion-failed":
            return _failed(state, "component_precondition_failed")

    elif state.phase is LifecyclePhase.RUN_ACTION:
        if event == "action-passed":
            return replace(state, phase=LifecyclePhase.CHECK_POSTCONDITION)
        if event == "action-failed":
            return _failed(state, "action-failed")
        if event == "action-unknown":
            return _failed(state, "action-unknown")

    elif state.phase is LifecyclePhase.CHECK_POSTCONDITION:
        if event == "assertion-passed":
            return replace(
                state,
                phase=LifecyclePhase.COMPLETE,
                outcome="component-verified",
            )
        if event == "assertion-failed":
            return _failed(state, "component_postcondition_failed")

    raise ValueError(
        f"invalid lifecycle event {event!r} for phase {state.phase.value!r}"
    )
