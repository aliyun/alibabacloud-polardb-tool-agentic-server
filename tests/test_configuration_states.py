from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.configuration.types import (
    ConfigStateError,
    EffectiveConfig,
    ModuleDocument,
    ModuleState,
    ValidationOperation,
    transition,
)


def document(
    state: ModuleState,
    *,
    with_effective: bool = False,
) -> ModuleDocument:
    effective = (
        EffectiveConfig(
            revision=1,
            state=ModuleState.ACTIVE,
            config={"value": "current"},
        )
        if with_effective
        else None
    )
    return ModuleDocument(
        revision=1,
        workflow_state=state,
        initial_state=ModuleState.NOT_CONFIGURED,
        effective=effective,
    )


@pytest.mark.parametrize(
    ("start", "action", "end"),
    [
        (ModuleState.NOT_CONFIGURED, "save_draft", ModuleState.DRAFT),
        (ModuleState.SKIPPED, "save_draft", ModuleState.DRAFT),
        (ModuleState.DRAFT, "begin_validation", ModuleState.VALIDATING),
        (
            ModuleState.VALIDATING,
            "validation_passed",
            ModuleState.VALIDATED,
        ),
        (
            ModuleState.VALIDATING,
            "validation_failed",
            ModuleState.ERROR,
        ),
        (ModuleState.VALIDATED, "edit", ModuleState.DRAFT),
        (ModuleState.ACTIVE, "save_draft", ModuleState.DRAFT),
        (ModuleState.ACTIVE, "disable", ModuleState.DISABLED),
        (ModuleState.DISABLED, "save_draft", ModuleState.DRAFT),
    ],
)
def test_legal_transition(
    start: ModuleState, action: str, end: ModuleState
) -> None:
    current = document(
        start,
        with_effective=start
        in {ModuleState.ACTIVE, ModuleState.DISABLED},
    )

    result = transition(current, action)

    assert result.workflow_state == end


def test_new_draft_preserves_effective_configuration() -> None:
    current = document(ModuleState.ACTIVE, with_effective=True)

    result = transition(current, "save_draft")

    assert result.workflow_state == ModuleState.DRAFT
    assert result.effective == current.effective


def test_skip_rejects_previously_effective_module() -> None:
    with pytest.raises(ConfigStateError, match="skip"):
        transition(
            document(ModuleState.DISABLED, with_effective=True),
            "skip",
        )


@pytest.mark.parametrize(
    "state",
    [ModuleState.DRAFT, ModuleState.VALIDATED, ModuleState.ERROR],
)
def test_reset_discards_pending_work(state: ModuleState) -> None:
    current = document(state, with_effective=True).model_copy(
        update={"draft": {"value": "candidate"}}
    )

    result = transition(current, "reset")

    assert result.workflow_state == ModuleState.ACTIVE
    assert result.draft is None
    assert result.last_validation is None
    assert result.effective == current.effective


def test_reset_rejects_live_validation() -> None:
    with pytest.raises(ConfigStateError, match="reset"):
        transition(document(ModuleState.VALIDATING), "reset")


def test_expired_validation_lease_recovers_to_draft() -> None:
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    current = document(ModuleState.VALIDATING).model_copy(
        update={
            "validation_operation": ValidationOperation(
                operation_id="op",
                started_at=expired - timedelta(minutes=1),
                lease_expires_at=expired,
            )
        }
    )

    result = transition(current, "recover_validation")

    assert result.workflow_state == ModuleState.DRAFT
    assert result.validation_operation is None
    assert result.last_error_code == "VALIDATION_INTERRUPTED"


def test_validation_recovery_rejects_unexpired_lease() -> None:
    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    current = document(ModuleState.VALIDATING).model_copy(
        update={
            "validation_operation": ValidationOperation(
                operation_id="op",
                started_at=datetime.now(timezone.utc),
                lease_expires_at=future,
            )
        }
    )

    with pytest.raises(ConfigStateError, match="lease"):
        transition(current, "recover_validation")

