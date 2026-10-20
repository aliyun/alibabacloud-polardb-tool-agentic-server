"""Run an unmodified source/target PAS CLI with loopback-only network access."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import runpy
import sys


def network_guard(event: str, args: tuple) -> None:
    host = None
    if event in {"socket.getaddrinfo", "socket.gethostbyname"}:
        host = args[0]
    elif event in {"socket.connect", "socket.sendto"}:
        address = args[1] if event == "socket.connect" else args[-1]
        if isinstance(address, tuple):
            host = address[0]
    if host is None or host == "localhost":
        return
    try:
        if ipaddress.ip_address(str(host)).is_loopback:
            return
    except ValueError:
        pass
    raise ConnectionRefusedError("Maintenance rehearsal denies non-loopback networking")


async def initialize() -> None:
    import base64
    import hashlib
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from server.configuration.bootstrap import initialize_configuration
    from server.configuration.repository import ConfigRepository
    from server.configuration.service import ConfigService
    from server.configuration.types import ConfigActor, ConfigCommand
    from server.core.config_crypto import ConfigCrypto

    engine = create_async_engine(os.environ["PAS_DATABASE_URL"])
    try:
        repo = ConfigRepository(async_sessionmaker(engine, expire_on_commit=False))
        crypto = ConfigCrypto(base64.b64decode(os.environ["PAS_ENCRYPTION_KEY"]))
        initial = await initialize_configuration(repo, crypto)
        actor = ConfigActor(scope="bootstrap", actor_type="bootstrap",
            credential_hash=hashlib.sha256(initial.bootstrap_token.encode()).hexdigest())
        service = ConfigService(repo, crypto, allow_insecure_loopback_urls=True)
        for module, config in (("observability", {"log_dir": os.environ["PAS_MAINT_LOG_DIR"]}),
                               ("core_admin", {"username": "maintenance-admin"})):
            document = await repo.get_module(module)
            saved = await service.execute(ConfigCommand(action="save_draft", module=module,
                expected_revision=document.revision, config=config), actor)
            validated = await service.execute(ConfigCommand(action="validate", module=module,
                expected_revision=saved.module["revision"]), actor)
            await service.execute(ConfigCommand(action="activate", module=module,
                expected_revision=validated.module["revision"], validation_id=validated.validation["validation_id"],
                idempotency_key="maintenance-" + module,
                config={"password": os.environ["PAS_MAINT_PASSWORD"]} if module == "core_admin" else None), actor)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    source = Path(sys.argv.pop(1)).resolve()
    sys.path.insert(0, str(source))
    sys.addaudithook(network_guard)
    if sys.argv[1:] == ["initialize-rehearsal"]:
        import asyncio
        asyncio.run(initialize())
    else:
        runpy.run_module("server.cli", run_name="__main__")
