"""Start the real PAS console with an ephemeral DB and loopback Feishu HTTP mock."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def loopback_only(event: str, args: tuple) -> None:
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
    raise ConnectionRefusedError("Feishu regression console denies non-loopback networking")


async def initialize(url: str, root_key: bytes, origin: str, password: str, log_dir: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from server.configuration.bootstrap import initialize_configuration
    from server.configuration.repository import ConfigRepository
    from server.configuration.service import ConfigService
    from server.configuration.types import ConfigActor, ConfigCommand
    from server.core.config_crypto import ConfigCrypto

    engine = create_async_engine(url)
    try:
        repository = ConfigRepository(async_sessionmaker(engine, expire_on_commit=False))
        crypto = ConfigCrypto(root_key)
        initial = await initialize_configuration(repository, crypto)
        actor = ConfigActor(
            scope="bootstrap",
            actor_type="bootstrap",
            credential_hash=hashlib.sha256(initial.bootstrap_token.encode()).hexdigest(),
        )
        service = ConfigService(repository, crypto, allow_insecure_loopback_urls=True)
        for module, config in (
            ("runtime_policy", {"external_base_url": origin}),
            ("observability", {"log_dir": log_dir}),
            ("core_admin", {"username": "admin"}),
        ):
            document = await repository.get_module(module)
            saved = await service.execute(
                ConfigCommand(action="save_draft", module=module, expected_revision=document.revision, config=config),
                actor,
            )
            validated = await service.execute(
                ConfigCommand(action="validate", module=module, expected_revision=saved.module["revision"]), actor
            )
            await service.execute(
                ConfigCommand(
                    action="activate",
                    module=module,
                    expected_revision=validated.module["revision"],
                    validation_id=validated.validation["validation_id"],
                    idempotency_key=secrets.token_urlsafe(20),
                    config={"password": password} if module == "core_admin" else None,
                ),
                actor,
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-local-development", action="store_true", required=True)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if not (ROOT / "web/dist/index.html").is_file():
        parser.error("Build the console first: cd web && npm ci && npm run build")
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", args.port))
    except OSError:
        parser.error("The loopback port is already in use; choose another --port")

    for key in list(os.environ):
        if key.startswith(("PAS_", "ALIBABA_CLOUD_")) or key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            del os.environ[key]
    sys.addaudithook(loopback_only)
    with tempfile.TemporaryDirectory(prefix="pas-feishu-regression-") as temporary:
        directory = Path(temporary)
        origin = f"http://127.0.0.1:{args.port}"
        mock_base = origin + "/__mock/feishu"
        root_key = secrets.token_bytes(32)
        url = f"sqlite+aiosqlite:///{directory}/metadata.db"
        os.environ.update(
            PAS_DATABASE_URL=url,
            PAS_ENCRYPTION_KEY=base64.b64encode(root_key).decode(),
            PAS_SERVER_PORT=str(args.port),
            PAS_STATIC_DIR=str(ROOT / "web/dist"),
            PAS_IDENTITY_SYNC_STATE_DIR=str(directory / "checkpoints"),
            NO_PROXY="127.0.0.1,localhost",
        )
        from server.db.schema import migrate_database

        migrate_database(url)
        password = "Mock!" + secrets.token_urlsafe(20)
        asyncio.run(initialize(url, root_key, origin, password, str(directory / "log")))

        from scripts.dev.mock_feishu_provider import APP_ID, APP_SECRET, create_app
        import server.app as app_module
        from server.enterprise_identity import feishu, feishu_events, feishu_tenant_verification
        from starlette.routing import Mount

        original_client = feishu.FeishuDirectoryClient

        class LocalFeishuClient(original_client):
            def __init__(self, **kwargs):
                super().__init__(**{**kwargs, "base_url": mock_base})

        feishu.FeishuDirectoryClient = LocalFeishuClient

        async def no_subscription(*_args, **_kwargs):
            await asyncio.Future()

        async def no_reconnect(*_args, **_kwargs):
            return False

        # HTTP pull regression only. Never start an SDK child process contacting real Feishu.
        feishu_events.feishu_directory_event_loop = no_subscription
        feishu_events.reconnect_feishu_listener_before_full = no_reconnect
        for name, path in (
            ("FEISHU_AUTHORIZE_URL", "authen/v1/authorize"),
            ("FEISHU_TOKEN_URL", "authen/v2/oauth/token"),
            ("FEISHU_USER_INFO_URL", "authen/v1/user_info"),
        ):
            setattr(feishu_tenant_verification, name, mock_base + "/open-apis/" + path)
        original_app = app_module.create_app
        mock = create_app(pas_origin=origin)

        def local_app(*app_args, **kwargs):
            app = original_app(*app_args, **kwargs)
            app.router.routes.insert(0, Mount("/__mock/feishu", app=mock))
            return app

        app_module.create_app = local_app
        print(
            f"Temporary console: {origin}\nUsername: admin\nPassword: {password}\n"
            f"Mock App ID: {APP_ID}\nMock App secret: {APP_SECRET}\n"
            f"Scenario control: {mock_base}/__mock/scenario\n"
            "All data is synthetic. Ctrl+C stops the console and removes its temporary database.",
            flush=True,
        )
        from server.serve import serve

        asyncio.run(serve(local_sso_dev_mode=True))


if __name__ == "__main__":
    main()
