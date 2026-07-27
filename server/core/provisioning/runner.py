# server/core/provisioning/runner.py
from __future__ import annotations

import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.models import Instance
from server.models.instance import ProvisioningStep

from .context import ProvisioningContext
from .states import (
    CompletedState,
    FailedState,
    ProvisioningState,
    state_from_step,
)

logger = logging.getLogger(__name__)


async def run_provisioning(
    instance_id: str,
    user_id: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> ProvisioningState:
    """Drive the provisioning state machine to a terminal state.

    Loads the instance, picks the State matching its current
    provisioning_step, then calls execute() in a loop until
    CompletedState or FailedState is reached.
    """
    start_time = time.monotonic()

    async with session_factory() as session:
        instance = await session.get(Instance, instance_id)
        if instance is None:
            logger.error(
                "Instance %s not found for provisioning", instance_id
            )
            return FailedState()

        ctx = ProvisioningContext(
            instance=instance,
            session=session,
            session_factory=session_factory,
            instance_id=instance_id,
            user_id=user_id,
            start_time=start_time,
        )
        state = state_from_step(instance.provisioning_step or ProvisioningStep.PENDING)

        while not isinstance(state, (CompletedState, FailedState)):
            state = await state.execute(ctx)

        return state
