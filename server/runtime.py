from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Any

from server.db.schema import DatabaseSchemaError


class RuntimePhase(StrEnum):
    STARTING = "STARTING"
    SCHEMA_OUTDATED = "SCHEMA_OUTDATED"
    SCHEMA_TOO_NEW = "SCHEMA_TOO_NEW"
    SETUP = "SETUP"
    READY = "READY"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


ApplicationLifespan = Callable[[Any], AbstractAsyncContextManager[None]]


class PasRuntime:
    """Own exactly one shared PAS application lifecycle."""

    def __init__(self, application_lifespan: ApplicationLifespan) -> None:
        self._application_lifespan = application_lifespan
        self._context: AbstractAsyncContextManager[None] | None = None
        self._start_attempted = False
        self._stopped = False
        self._application_state: Any | None = None
        self.application: Any | None = None
        self.phase = RuntimePhase.STARTING
        self.error_code: str | None = None

    async def start(self, app: Any) -> None:
        if self._start_attempted:
            raise RuntimeError("PAS runtime is already started")
        self._start_attempted = True
        self._application_state = app.state
        self.application = app
        context = self._application_lifespan(app)
        try:
            await context.__aenter__()
        except DatabaseSchemaError as error:
            # Schema validation precedes resource/background-task initialization.
            # An external migration can make a later start safe without DDL here.
            self._start_attempted = False
            self.error_code = error.code
            self.phase = {
                "DATABASE_SCHEMA_OUTDATED": RuntimePhase.SCHEMA_OUTDATED,
                "DATABASE_SCHEMA_TOO_NEW": RuntimePhase.SCHEMA_TOO_NEW,
            }.get(error.code, RuntimePhase.FAILED)
            raise
        except Exception:
            self.error_code = "RUNTIME_INITIALIZATION_FAILED"
            self.phase = RuntimePhase.FAILED
            raise
        self._context = context
        self.error_code = None
        mode = getattr(
            getattr(app.state, "runtime_access_policy", None),
            "mode",
            "READY",
        )
        self.phase = (
            RuntimePhase.SETUP
            if mode == RuntimePhase.SETUP.value
            else RuntimePhase.READY
        )

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.phase = RuntimePhase.STOPPING
        context = self._context
        self._context = None
        if context is not None:
            await context.__aexit__(None, None, None)

    def snapshot(self) -> dict[str, str | None]:
        return {
            "phase": self.phase.value,
            "error_code": self.error_code,
        }

    @property
    def application_state(self) -> Any | None:
        return self._application_state
