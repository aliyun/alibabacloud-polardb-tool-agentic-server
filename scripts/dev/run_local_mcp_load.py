"""Run the existing enqueue latency case over loopback HTTP on private metadata DBs."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.dev.run_local_regression import LocalDatabases, clean_environment, free_port, owned_database_name  # noqa: E402


async def exercise(url: str, dialect: str) -> None:
    import uvicorn
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from server.app import create_app
    from server.auth.jwt_manager import reset_keys
    from server.config import get_config, reset_config
    from server.core.agent_token_service import get_or_create_token
    from server.core.crypto import encrypt
    from server.db import engine as engine_mod
    from server.mcp.transport import mcp_lifespan, reset_mcp
    from server.models import (
        Agent, AgentProvisioningBinding, CredentialCapability, CredentialPurpose,
        DBInstanceResource, DBInstanceStatus, Instance, InstanceCredential,
        InstanceStatus, InstanceTopology, ProvisioningBackend, ProvisioningBackendHealth, User,
    )
    from server.models.base import utc_now
    from tests._helpers import init_test_jwt_keys
    from tests.perf.test_create_db_instance_latency import test_create_db_instance_reports_configured_environment_latency

    reset_config()
    reset_keys()
    reset_mcp()
    init_test_jwt_keys()
    # This profile measures enqueue/delete-request handling only. No application
    # provisioning lifespan or adapter worker is started, so no physical resource
    # can be created. Keep the synthetic health observation valid for the run.
    get_config().polardb.tenant_provisioning.backend_health_stale_after_seconds = 3600
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    engine_mod._engine, engine_mod._session_factory = engine, factory
    server = None
    task = None
    try:
        async with factory() as session:
            admin = User(external_id="local-load-owner", display_name="Local load owner")
            agent = Agent(name="local-load-agent", max_active_resources=1100)
            instance = Instance(cluster_id="pc-local-load", name="Synthetic backend without a worker",
                                topology=InstanceTopology.MULTITENANT, status=InstanceStatus.ACTIVE,
                                host="never-contact.invalid", port=3306)
            session.add_all([admin, agent, instance])
            await session.flush()
            credential = InstanceCredential(instance_id=instance.id, name="synthetic",
                purpose=CredentialPurpose.PROVISIONING_ADMIN, capability=CredentialCapability.ADMIN,
                username_ciphertext=encrypt("synthetic"), password_ciphertext=encrypt("synthetic"),
                created_by_user_id=admin.id)
            session.add(credential)
            await session.flush()
            backend = ProvisioningBackend(instance_id=instance.id, admin_credential_id=credential.id,
                                          max_active_resources=1100)
            session.add(backend)
            await session.flush()
            session.add_all([ProvisioningBackendHealth(backend_id=backend.id, healthy=True, checked_at=utc_now()),
                             AgentProvisioningBinding(agent_id=agent.id, backend_id=backend.id,
                                                      created_by_user_id=admin.id)])
            _, token = await get_or_create_token(session, agent.id, None)
            await session.commit()
        app = create_app()
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off",
                                               access_log=False, log_level="warning"))
        async with mcp_lifespan():
            task = asyncio.create_task(server.serve())
            async with asyncio.timeout(15):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("Local MCP listener stopped before startup")
                    await asyncio.sleep(.05)
            os.environ.update(PAS_PERF_MCP_URL=f"http://127.0.0.1:{port}/mcp", PAS_PERF_AGENT_TOKEN=token,
                              PAS_PERF_METADB_TYPE=dialect, PAS_PERF_ENVIRONMENT="local-loopback-enqueue-only-no-workers")
            await test_create_db_instance_reports_configured_environment_latency()
            async with factory() as session:
                count = await session.scalar(select(func.count()).select_from(DBInstanceResource))
                unexpected = await session.scalar(select(func.count()).select_from(DBInstanceResource)
                    .where(DBInstanceResource.status != DBInstanceStatus.DELETING))
                assert count == 1000 and unexpected == 0
            print(json.dumps({"dialect": dialect, "result": "passed", "requests": 1000,
                              "concurrency": 20, "physical_workers": False}), flush=True)
    finally:
        if server is not None:
            server.should_exit = True
        if task is not None:
            await asyncio.wait_for(task, timeout=15)
        for key in ("PAS_PERF_MCP_URL", "PAS_PERF_AGENT_TOKEN", "PAS_PERF_METADB_TYPE", "PAS_PERF_ENVIRONMENT"):
            os.environ.pop(key, None)
        await engine.dispose()
        engine_mod.reset_engine()
        reset_mcp()
        reset_keys()
        reset_config()


def configure_profile() -> dict[str, str]:
    env = clean_environment(dict(os.environ))
    env.update(PAS_ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode(), NO_PROXY="127.0.0.1,localhost")
    os.environ.clear()
    os.environ.update(env)
    # Apply the same outbound-network guard as browser tests before loading PAS.
    import runpy
    sys.addaudithook(runpy.run_path(str(ROOT / "web/e2e/pas_runner.py"))["network_guard"])
    # The original legacy-mode case emits one expected warning per request.
    logging.disable(logging.WARNING)
    return env


def run_owned_profile(url: str, dialect: str) -> None:
    """Internal child entry point; the public CLI never accepts database URLs."""
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    drivers = {"mysql": "mysql+asyncmy", "postgresql": "postgresql+asyncpg"}
    if parsed.host != "127.0.0.1" or not parsed.port or parsed.drivername != drivers.get(dialect):
        raise ValueError("Load profile requires an owned native loopback database")
    owned_database_name(parsed.database or "")
    configure_profile()
    from sse_starlette.sse import AppStatus
    assert not AppStatus.should_exit, "A fresh load-profile process must not inherit SSE shutdown state"
    print(json.dumps({"dialect": dialect, "pid": os.getpid(), "initial_sse_should_exit": False}), flush=True)
    asyncio.run(exercise(url, dialect))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = configure_profile()
    results = {"completed": [], "cleanup_complete": False, "profile": "local-loopback-enqueue-only-no-workers"}
    with tempfile.TemporaryDirectory(prefix="pas-local-load-", dir="/tmp") as temporary:
        databases = LocalDatabases(Path(temporary), output, env)
        try:
            databases.start()
            results.update(mysql_version=databases.mysql_version, postgresql_version=databases.pg_version)
            for dialect in ("mysql", "postgresql"):
                with databases.database(dialect) as url:
                    with (output / f"migrate-{dialect}.log").open("w") as log:
                        subprocess.run([sys.executable, str(ROOT / "web/e2e/pas_runner.py"), "database", "migrate"],
                                       cwd=ROOT, env=dict(env, PAS_DATABASE_URL=url), stdout=log,
                                       stderr=subprocess.STDOUT, check=True)
                    # sse-starlette retains process-wide graceful-shutdown state.
                    # Give each server one process/loop, as in normal deployment.
                    subprocess.run([sys.executable, "-c",
                        "import os; from scripts.dev.run_local_mcp_load import run_owned_profile; "
                        "run_owned_profile(os.environ['PAS_LOCAL_LOAD_DATABASE_URL'], os.environ['PAS_LOCAL_LOAD_DIALECT'])"],
                        cwd=ROOT, env=dict(env, PAS_LOCAL_LOAD_DATABASE_URL=url, PAS_LOCAL_LOAD_DIALECT=dialect), check=True)
                    results["completed"].append(dialect)
        finally:
            databases.close()
            results["cleanup_complete"] = True
            (output / "local-load.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
