from __future__ import annotations

import asyncio
import io
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.api.configuration import router
from server.cli import ConfigProtocolClient, apply_declaration
from server.configuration.bootstrap import rotate_bootstrap_token
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
)
from tests._configuration_helpers import (
    ConfigTestContext,
    create_config_context,
)


ACTOR = ConfigActor(scope="admin:test", actor_type="admin")
CANDIDATE = {"enabled": False}


async def _apply_direct(context: ConfigTestContext) -> None:
    saved = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="agent_token_auth",
            expected_revision=0,
            config=CANDIDATE,
        ),
        ACTOR,
    )
    validated = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="agent_token_auth",
            expected_revision=saved.module["revision"],
        ),
        ACTOR,
    )
    await context.service.execute(
        ConfigCommand(
            action=ConfigAction.ACTIVATE,
            module="agent_token_auth",
            expected_revision=validated.module["revision"],
            validation_id=validated.validation["validation_id"],
            idempotency_key="direct-activation",
        ),
        ACTOR,
    )


class _LoopProtocol(ConfigProtocolClient):
    def __init__(
        self,
        context: ConfigTestContext,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.context = context
        self.loop = loop

    def command(self, body: dict[str, Any]) -> dict[str, Any]:
        command = ConfigCommand.model_validate(
            {"protocol_version": 1, **body}
        )
        future = asyncio.run_coroutine_threadsafe(
            self.context.service.execute(command, ACTOR),
            self.loop,
        )
        return future.result().model_dump(
            mode="json", exclude_none=True
        )


async def _apply_cli(context: ConfigTestContext) -> None:
    protocol = _LoopProtocol(context, asyncio.get_running_loop())
    declaration = {
        "protocol_version": 1,
        "agent_token_auth": {
            "desired_state": "active",
            "config": CANDIDATE,
        },
    }
    await asyncio.to_thread(
        apply_declaration,
        protocol,
        declaration,
        dry_run=False,
        stdin=io.StringIO(),
    )


async def _apply_ui_sequence(context: ConfigTestContext) -> None:
    token = await rotate_bootstrap_token(context.repository)
    app = FastAPI()
    app.state.config_service = context.service
    app.include_router(router, prefix="/api")
    headers = {"Authorization": f"Bootstrap {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        saved = await client.post(
            "/api/config",
            json={
                "protocol_version": 1,
                "action": "save_draft",
                "module": "agent_token_auth",
                "expected_revision": 0,
                "config": CANDIDATE,
            },
        )
        assert saved.status_code == 200
        validated = await client.post(
            "/api/config",
            json={
                "protocol_version": 1,
                "action": "validate",
                "module": "agent_token_auth",
                "expected_revision": saved.json()["module"][
                    "revision"
                ],
            },
        )
        assert validated.status_code == 200
        activated = await client.post(
            "/api/config",
            json={
                "protocol_version": 1,
                "action": "activate",
                "module": "agent_token_auth",
                "expected_revision": validated.json()["module"][
                    "revision"
                ],
                "validation_id": validated.json()["validation"][
                    "validation_id"
                ],
                "idempotency_key": "ui-activation",
            },
        )
        assert activated.status_code == 200


async def test_api_cli_and_ui_sequence_store_equivalent_documents() -> None:
    contexts = [await create_config_context() for _ in range(3)]
    direct, cli, ui = contexts
    try:
        await _apply_direct(direct)
        await _apply_cli(cli)
        await _apply_ui_sequence(ui)

        documents = [
            await context.repository.get_module("agent_token_auth")
            for context in contexts
        ]
        assert documents[0] == documents[1] == documents[2]
        assert documents[0].effective.config == CANDIDATE
        assert documents[0].workflow_state.value == "ACTIVE"
    finally:
        for context in contexts:
            await context.close()
