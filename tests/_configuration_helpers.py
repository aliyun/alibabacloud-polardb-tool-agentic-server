from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from server.configuration.bootstrap import initialize_configuration
from server.configuration.repository import ConfigRepository
from server.configuration.service import ConfigService
from server.core.config_crypto import ConfigCrypto
from server.models import Base

ROOT_KEY = b"01234567890123456789012345678901"


@dataclass(slots=True)
class ConfigTestContext:
    engine: AsyncEngine
    repository: ConfigRepository
    crypto: ConfigCrypto
    service: ConfigService

    async def close(self) -> None:
        await self.engine.dispose()


async def create_config_context() -> ConfigTestContext:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    crypto = ConfigCrypto(ROOT_KEY)
    await initialize_configuration(repository, crypto)
    return ConfigTestContext(
        engine=engine,
        repository=repository,
        crypto=crypto,
        service=ConfigService(repository, crypto),
    )
