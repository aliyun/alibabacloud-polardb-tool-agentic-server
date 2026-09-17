from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.repository import ConfigRepository
from server.management.settings import ManagedIdentitySettings
from server.models import Base, ManagedInstanceBinding


@pytest.fixture
async def identity_repository():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    await repository.ensure_setup_status()
    yield repository
    await engine.dispose()


async def _binding_count(repository: ConfigRepository) -> int:
    async with repository.session_factory() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(ManagedInstanceBinding)
            )
            or 0
        )


async def test_describe_does_not_create_identity_binding(
    identity_repository: ConfigRepository,
) -> None:
    from server.management.identity import ManagedIdentityBinder
    from server.management.types import ManagedTarget

    binder = ManagedIdentityBinder(
        identity_repository,
        ManagedIdentitySettings("pmcp-a", 1),
    )

    result = await binder.verify(ManagedTarget(instance_id="pmcp-a", generation=1))

    assert result.status == "UNBOUND"
    assert await _binding_count(identity_repository) == 0


async def test_first_mutation_binds_and_exact_replay_is_stable(
    identity_repository: ConfigRepository,
) -> None:
    from server.management.identity import ManagedIdentityBinder
    from server.management.types import ManagedTarget

    binder = ManagedIdentityBinder(
        identity_repository,
        ManagedIdentitySettings("pmcp-a", 1),
    )
    target = ManagedTarget(instance_id="pmcp-a", generation=1)

    first = await binder.bind(target)
    replay = await binder.bind(target)

    assert first.status == replay.status == "BOUND"
    assert first.instance_id == replay.instance_id == "pmcp-a"
    assert first.generation == replay.generation == 1
    assert await _binding_count(identity_repository) == 1


@pytest.mark.parametrize(
    ("target", "code"),
    [
        (
            {"instance_id": "pmcp-other", "generation": 1},
            "INSTANCE_IDENTITY_MISMATCH",
        ),
        (
            {"instance_id": "pmcp-a", "generation": 2},
            "INSTANCE_GENERATION_MISMATCH",
        ),
    ],
)
async def test_environment_request_mismatch_makes_no_write(
    identity_repository: ConfigRepository,
    target: dict[str, object],
    code: str,
) -> None:
    from server.management.identity import (
        ManagedIdentityBinder,
        ManagedIdentityError,
    )
    from server.management.types import ManagedTarget

    binder = ManagedIdentityBinder(
        identity_repository,
        ManagedIdentitySettings("pmcp-a", 1),
    )

    with pytest.raises(ManagedIdentityError) as captured:
        await binder.bind(ManagedTarget.model_validate(target))

    assert captured.value.code == code
    assert await _binding_count(identity_repository) == 0


async def test_database_identity_mismatch_rejects_second_node(
    identity_repository: ConfigRepository,
) -> None:
    from server.management.identity import (
        ManagedIdentityBinder,
        ManagedIdentityError,
    )
    from server.management.types import ManagedTarget

    first = ManagedIdentityBinder(
        identity_repository,
        ManagedIdentitySettings("pmcp-a", 2),
    )
    await first.bind(ManagedTarget(instance_id="pmcp-a", generation=2))
    second = ManagedIdentityBinder(
        identity_repository,
        ManagedIdentitySettings("pmcp-b", 1),
    )

    with pytest.raises(ManagedIdentityError) as captured:
        await second.bind(ManagedTarget(instance_id="pmcp-b", generation=1))

    assert captured.value.code == "INSTANCE_IDENTITY_MISMATCH"
    assert await _binding_count(identity_repository) == 1


async def test_stale_database_generation_is_rejected(
    identity_repository: ConfigRepository,
) -> None:
    from server.management.identity import (
        ManagedIdentityBinder,
        ManagedIdentityError,
    )
    from server.management.types import ManagedTarget

    async with identity_repository.session_factory() as session:
        session.add(
            ManagedInstanceBinding(
                instance_id="pmcp-a",
                instance_generation=3,
                bound_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
    binder = ManagedIdentityBinder(
        identity_repository,
        ManagedIdentitySettings("pmcp-a", 2),
    )

    with pytest.raises(ManagedIdentityError) as captured:
        await binder.bind(ManagedTarget(instance_id="pmcp-a", generation=2))

    assert captured.value.code == "INSTANCE_GENERATION_MISMATCH"
    assert await _binding_count(identity_repository) == 1
