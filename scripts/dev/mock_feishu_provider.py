"""Reusable, synthetic subset of Feishu HTTP APIs; never use as an identity provider.

Contracts and deliberate limitations are documented in feishu-regression.md.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

APP_ID = "cli_mock_regression"
APP_SECRET = "mock-feishu-app-secret"
TENANT_KEY = "mock-feishu-tenant"
TENANT_TOKEN = "t-mock-regression-only"
Scenario = Literal["normal", "removed_user", "second_page_failure", "missing_page_token", "permission_denied"]


class ScenarioRequest(BaseModel):
    scenario: Scenario


@dataclass
class MockState:
    scenario: Scenario
    requests: list[dict] = field(default_factory=list)
    codes: dict[str, tuple[str, float]] = field(default_factory=dict)
    user_tokens: set[str] = field(default_factory=set)

    @property
    def users(self) -> list[dict]:
        users = [
            {
                "user_id": "mock-alice",
                "open_id": "ou_mock_alice",
                "name": "Alice Mock",
                "email": "alice@example.invalid",
                "department_ids": ["od-mock-research"],
            },
            {
                "user_id": "mock-bob",
                "open_id": "ou_mock_bob",
                "name": "Bob Mock",
                "email": "bob@example.invalid",
                "department_ids": ["od-mock-child"],
            },
        ]
        return users[:1] if self.scenario == "removed_user" else users


def reply(data: dict | None = None, *, code: int = 0, status: int = 200) -> JSONResponse:
    payload = {"code": code, "msg": "ok" if code == 0 else "Synthetic Feishu API failure"}
    if data is not None:
        payload["data"] = data
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


def create_app(*, scenario: Scenario = "normal", pas_origin: str = "http://127.0.0.1:18760") -> FastAPI:
    origin = urlsplit(pas_origin)
    if (
        origin.scheme != "http"
        or origin.hostname != "127.0.0.1"
        or origin.path
        or origin.query
        or origin.fragment
        or origin.username
        or origin.password
    ):
        raise ValueError("Mock callbacks require an exact loopback PAS origin")
    state = MockState(ScenarioRequest(scenario=scenario).scenario)
    app = FastAPI(title="Local synthetic Feishu API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.feishu_mock = state
    callbacks = {
        pas_origin + "/auth/feishu/" + path
        for path in (
            "tenant-verification/callback",
            "login/callback",
        )
    }

    @app.post("/__mock/scenario")
    async def set_scenario(body: ScenarioRequest):
        state.scenario = body.scenario
        return {"scenario": state.scenario}

    @app.get("/__mock/status")
    async def status():
        return {"scenario": state.scenario, "requests": state.requests}

    @app.post("/open-apis/auth/v3/tenant_access_token/internal")
    async def tenant_token(request: Request):
        body = await request.json()
        if body.get("app_id") != APP_ID or body.get("app_secret") != APP_SECRET:
            return reply(code=10015, status=400)
        return JSONResponse(
            {"code": 0, "msg": "ok", "tenant_access_token": TENANT_TOKEN, "expire": 7200},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/open-apis/authen/v1/authorize")
    async def authorize(request: Request):
        query = request.query_params
        redirect = query.get("redirect_uri", "")
        if query.get("client_id") != APP_ID or redirect not in callbacks or not query.get("state"):
            return reply(code=10003, status=400)
        code = secrets.token_urlsafe(24)
        state.codes[code] = (redirect, time.monotonic() + 60)
        return RedirectResponse(
            redirect + "?" + urlencode({"code": code, "state": query["state"]}),
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/open-apis/authen/v2/oauth/token")
    async def user_token(request: Request):
        body = await request.json()
        code = body.get("code")
        saved = state.codes.get(code)
        if (
            body.get("grant_type") != "authorization_code"
            or body.get("client_id") != APP_ID
            or body.get("client_secret") != APP_SECRET
            or not saved
            or saved[0] != body.get("redirect_uri")
            or saved[1] < time.monotonic()
        ):
            return reply(code=10204, status=400)
        del state.codes[code]
        token = "u-" + secrets.token_urlsafe(24)
        state.user_tokens.add(token)
        return JSONResponse(
            {"code": 0, "access_token": token, "token_type": "Bearer", "expires_in": 7200},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/open-apis/authen/v1/user_info")
    async def user_info(request: Request):
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in state.user_tokens:
            return reply(code=99991663, status=401)
        return reply({**state.users[0], "tenant_key": TENANT_KEY})

    departments = {
        "od-mock-research": {
            "open_department_id": "od-mock-research",
            "name": "Research Mock",
            "parent_department_id": "0",
        },
        "od-mock-child": {
            "open_department_id": "od-mock-child",
            "name": "Platform Mock",
            "parent_department_id": "od-mock-research",
        },
    }

    @app.get("/open-apis/contact/v3/{path:path}")
    async def contacts(path: str, request: Request):
        # Only directory query fields are recorded; never record OAuth state, codes, headers or secrets.
        params = dict(request.query_params)
        state.requests.append(
            {
                "method": "GET",
                "path": path,
                "query": {
                    key: value
                    for key, value in params.items()
                    if key
                    in {
                        "user_id_type",
                        "department_id_type",
                        "department_id",
                        "member_id_type",
                        "member_type",
                        "member_id",
                        "type",
                        "group_type",
                        "page_size",
                        "page_token",
                    }
                },
            }
        )
        if request.headers.get("authorization") != "Bearer " + TENANT_TOKEN:
            return reply(code=99991663, status=401)
        if state.scenario == "permission_denied":
            return reply(code=99991672)
        required = {"user_id_type": "user_id"} if path.startswith("users") else {}
        if path.startswith("departments/") or path == "users/find_by_department":
            required["department_id_type"] = "open_department_id"
        if path.startswith("group/") and "/member/" in path:
            required.update(member_id_type="user_id", member_type="user")
        if any(params.get(key) != value for key, value in required.items()):
            return reply(code=10003, status=400)

        items, key = [], "items"
        if path == "users":
            items = state.users
        elif path == "users/find_by_department":
            if "department_id" not in params:
                return reply(code=10003, status=400)
            items = [user for user in state.users if params["department_id"] in user["department_ids"]]
        elif path.startswith("users/"):
            user = next((user for user in state.users if user["user_id"] == path.removeprefix("users/")), None)
            return reply({"user": user}) if user else reply(code=10004, status=404)
        elif path.startswith("departments/"):
            parts = path.split("/")
            department = parts[1]
            if len(parts) == 3 and parts[2] == "children":
                items = [item for item in departments.values() if item["parent_department_id"] == department]
            elif len(parts) == 2 and department in departments:
                return reply({"department": departments[department]})
            else:
                return reply(code=10003, status=404)
        elif path == "group/simplelist":
            if params.get("type") not in {"1", "2"}:
                return reply(code=10003, status=400)
            key = "grouplist"
            items = [{"group_id": "mock-reviewers", "name": "Reviewers Mock"}] if params["type"] == "1" else []
        elif path == "group/mock-reviewers/member/simplelist":
            key = "memberlist"
            items = [{"member_id": "mock-bob", "member_type": "user"}] if len(state.users) == 2 else []
        else:
            return reply(code=10003, status=404)

        cursor = params.get("page_token")
        if cursor not in {None, "mock-page-1"}:
            return reply(code=40012, status=400)
        if path == "users" and cursor and state.scenario == "second_page_failure":
            return reply(code=10500, status=503)
        index = 1 if cursor else 0
        data = {key: items[index : index + 1], "has_more": len(items) > index + 1}
        if data["has_more"] and state.scenario != "missing_page_token":
            data["page_token"] = "mock-page-1"
        return reply(data)

    return app
