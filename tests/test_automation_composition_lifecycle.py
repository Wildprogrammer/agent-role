import pytest

from agent_workflow_hub.automation_composition.lifecycle import (
    LifecyclePhase,
    initial_state,
    transition,
)


def test_already_complete_skips_action() -> None:
    state = initial_state(
        has_already_complete=True,
        has_precondition=True,
        has_recovery=True,
    )
    assert state.phase is LifecyclePhase.CHECK_ALREADY_COMPLETE

    state = transition(state, "assertion-passed")

    assert state.phase is LifecyclePhase.COMPLETE
    assert state.outcome == "already-complete"


def test_failed_precondition_runs_one_recovery_then_action() -> None:
    state = initial_state(
        has_already_complete=False,
        has_precondition=True,
        has_recovery=True,
    )
    state = transition(state, "assertion-failed")
    assert state.phase is LifecyclePhase.RUN_RECOVERY
    state = transition(state, "action-passed")
    assert state.phase is LifecyclePhase.RECHECK_PRECONDITION
    state = transition(state, "assertion-passed")
    assert state.phase is LifecyclePhase.RUN_ACTION
    state = transition(state, "action-passed")
    assert state.phase is LifecyclePhase.CHECK_POSTCONDITION

    assert transition(state, "assertion-passed").outcome == "component-verified"


def test_recovery_cannot_repeat() -> None:
    state = initial_state(
        has_already_complete=False,
        has_precondition=True,
        has_recovery=True,
    )
    state = transition(state, "assertion-failed")
    state = transition(state, "action-passed")
    failed = transition(state, "assertion-failed")

    assert failed.phase is LifecyclePhase.FAILED
    assert failed.error == "component_precondition_failed"
    with pytest.raises(ValueError, match="terminal lifecycle state"):
        transition(failed, "action-passed")


def test_recovery_action_failure_has_stable_error() -> None:
    state = initial_state(
        has_already_complete=False,
        has_precondition=True,
        has_recovery=True,
    )
    state = transition(state, "assertion-failed")

    failed = transition(state, "action-failed")

    assert failed.phase is LifecyclePhase.FAILED
    assert failed.error == "component_recovery_failed"


def test_postcondition_failure_has_stable_error() -> None:
    state = initial_state(
        has_already_complete=False,
        has_precondition=False,
        has_recovery=False,
    )
    state = transition(state, "action-passed")

    failed = transition(state, "assertion-failed")

    assert failed.phase is LifecyclePhase.FAILED
    assert failed.error == "component_postcondition_failed"


def test_unknown_action_result_is_terminal() -> None:
    state = initial_state(
        has_already_complete=False,
        has_precondition=False,
        has_recovery=False,
    )

    failed = transition(state, "action-unknown")

    assert failed.phase is LifecyclePhase.FAILED
    assert failed.error == "action-unknown"
    with pytest.raises(ValueError, match="terminal lifecycle state"):
        transition(failed, "action-passed")


def test_invalid_phase_event_pair_is_rejected() -> None:
    state = initial_state(
        has_already_complete=True,
        has_precondition=False,
        has_recovery=False,
    )

    with pytest.raises(ValueError, match="invalid lifecycle event"):
        transition(state, "action-passed")
