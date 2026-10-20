"""Enterprise-internal DingTalk identity facts; never infer users from caller claims."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any

import httpx


# Query-user details: https://open.dingtalk.com/document/development/query-user-details
# The FAQ also documents 60121 for left, pending, or foreign-tenant users:
# https://open.dingtalk.com/document/development/common-errors
# Unavailable means no authoritative membership, not proof of employee deletion.
_UNAVAILABLE_USER_CODES = frozenset({33012, 60121})
# Department details: https://open.dingtalk.com/document/development/query-department-details0-v2
# 40009 is also returned for a deleted department (verified in Stream E2E).
_UNAVAILABLE_DEPARTMENT_CODES = frozenset({40009, 60003})


class DingTalkIdentityError(ValueError):
    """Safe error category without upstream bodies, tokens or personal information."""

    def __init__(self, category, *, upstream_code=None):
        super().__init__(category)
        self.upstream_code = upstream_code


@dataclass(frozen=True)
class DingTalkDirectorySnapshot:
    users: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    memberships: list[dict[str, str]]


class DingTalkDirectoryClient:
    MAX_ENTRIES = 50_000

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        **_kwargs: Any,
    ):
        if not all(isinstance(x, str) and x.strip() for x in (tenant_id, client_id, client_secret)):
            raise DingTalkIdentityError("INVALID_SOURCE_CONFIGURATION")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = httpx.AsyncClient(transport=transport, timeout=15, follow_redirects=False)
        self._token: str | None = None
        self._token_until = 0.0
        self._token_lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.http.aclose()

    async def clear_checkpoint(self):
        # Full snapshots are bounded and only published after complete collection.
        # Restart always rescans; no partial cursor can outlive a config change.
        return None

    async def request(self, method: str, url: str, *, retry: bool = True, **kwargs) -> dict:
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            try:
                response = await self.http.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == attempts - 1:
                    raise DingTalkIdentityError("TRANSIENT_UPSTREAM") from None
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == attempts - 1:
                    raise DingTalkIdentityError("TRANSIENT_UPSTREAM")
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code != 200:
                raise DingTalkIdentityError("UPSTREAM_ACCESS_DENIED")
            if len(response.content) > 8 * 1024 * 1024:
                raise DingTalkIdentityError("RESPONSE_LIMIT_EXCEEDED")
            try:
                payload = response.json()
            except ValueError:
                raise DingTalkIdentityError("INVALID_UPSTREAM_RESPONSE") from None
            if not isinstance(payload, dict) or payload.get("errcode", 0) != 0 or payload.get("code"):
                raise DingTalkIdentityError(
                    "UPSTREAM_BUSINESS_ERROR",
                    upstream_code=payload.get("errcode") if isinstance(payload, dict) else None,
                )
            return payload
        raise DingTalkIdentityError("TRANSIENT_UPSTREAM")

    async def application_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_until:
                return self._token
            data = await self.request(
                "POST",
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": self.client_id, "appSecret": self.client_secret},
            )
            token = data.get("accessToken")
            expiry = data.get("expireIn")
            if not isinstance(token, str) or not token or not isinstance(expiry, (int, float)) or expiry <= 0:
                raise DingTalkIdentityError("INVALID_APPLICATION_TOKEN")
            self._token = token
            self._token_until = time.monotonic() + max(0, expiry - 60)
            return token

    async def oapi(self, path: str, body: dict | None = None) -> dict:
        token = await self.application_token()
        return await self.request(
            "POST" if body is not None else "GET",
            "https://oapi.dingtalk.com" + path,
            params={"access_token": token},
            **({"json": body} if body is not None else {}),
        )

    async def user(self, user_id: str) -> dict:
        result = (await self.oapi("/topapi/v2/user/get", {"userid": user_id})).get("result")
        if not isinstance(result, dict) or result.get("userid") != user_id:
            raise DingTalkIdentityError("INVALID_DIRECTORY_USER")
        return result

    async def department(self, department_id):
        result = (await self.oapi("/topapi/v2/department/get", {"dept_id": department_id})).get("result")
        if not isinstance(result, dict) or str(result.get("dept_id")) != department_id or not result.get("name"):
            raise DingTalkIdentityError("INVALID_DEPARTMENT")
        return {
            "id": department_id,
            "display_name": result["name"],
            "principal_type": "department",
            "parent_id": str(result["parent_id"]) if result.get("parent_id") else None,
        }

    async def authorized_scope(self):
        scopes = (await self.oapi("/auth/scopes")).get("auth_org_scopes")
        if (
            not isinstance(scopes, dict)
            or not isinstance(scopes.get("authed_dept"), list)
            or not isinstance(scopes.get("authed_user"), list)
        ):
            raise DingTalkIdentityError("INVALID_DIRECTORY_SCOPE")
        return {str(x) for x in scopes["authed_dept"]}, {str(x) for x in scopes["authed_user"]}

    async def department_path(self, department_id, roots):
        path = []
        visited = set()
        while department_id:
            if department_id in visited or len(visited) >= 100:
                raise DingTalkIdentityError("CYCLIC_DEPARTMENT_TREE")
            visited.add(department_id)
            department = await self.department(department_id)
            path.append(department)
            if department_id in roots:
                # An authorized root has no visible parent outside its scope.
                department["parent_id"] = None
                return path
            department_id = department["parent_id"]
        return []

    async def fetch_user_event_facts(self, user_id):
        roots, explicit_users = await self.authorized_scope()
        try:
            raw = await self.user(user_id)
        except DingTalkIdentityError as error:
            if error.upstream_code in _UNAVAILABLE_USER_CODES:
                return {"deleted": True}
            raise
        user = self.record_user(raw)
        if user["status"] != "active":
            return {"deleted": True}
        departments = raw.get("dept_id_list")
        if not isinstance(departments, list):
            raise DingTalkIdentityError("INVALID_DIRECTORY_USER")
        if len(departments) > 100:
            raise DingTalkIdentityError("DIRECTORY_LIMIT_EXCEEDED")
        groups, memberships = {}, []
        for department_id in departments:
            path = await self.department_path(str(department_id), roots)
            if path:
                memberships.append({"group_id": str(department_id), "member_id": user_id, "member_type": "user"})
                groups.update({d["id"]: d for d in path})
        if not groups and user_id not in explicit_users:
            return {"deleted": True}
        return {"user": user, "groups": list(groups.values()), "memberships": memberships}

    async def fetch_department_event_facts(self, department_id):
        roots, _ = await self.authorized_scope()
        try:
            path = await self.department_path(department_id, roots)
        except DingTalkIdentityError as error:
            if error.upstream_code in _UNAVAILABLE_DEPARTMENT_CODES:
                return {"deleted": True}
            raise
        if not path:
            return {"deleted": True}
        users, cursor, seen = {}, 0, set()
        disabled_users = set()
        while True:
            if cursor in seen:
                raise DingTalkIdentityError("REPEATED_DIRECTORY_CURSOR")
            seen.add(cursor)
            if len(seen) > self.MAX_ENTRIES // 100 + 1:
                raise DingTalkIdentityError("DIRECTORY_LIMIT_EXCEEDED")
            page = (
                await self.oapi("/topapi/v2/user/list", {"dept_id": department_id, "cursor": cursor, "size": 100})
            ).get("result")
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("list"), list)
                or not isinstance(page.get("has_more"), bool)
            ):
                raise DingTalkIdentityError("INVALID_DIRECTORY_PAGE")
            for raw in page["list"]:
                if not isinstance(raw, dict):
                    raise DingTalkIdentityError("INVALID_DIRECTORY_USER")
                user = self.record_user(raw)
                if user["status"] == "active":
                    users[user["id"]] = user
                else:
                    disabled_users.add(user["id"])
                if len(users) + len(disabled_users) > self.MAX_ENTRIES:
                    raise DingTalkIdentityError("DIRECTORY_LIMIT_EXCEEDED")
            if not page["has_more"]:
                break
            cursor = page.get("next_cursor")
            if isinstance(cursor, bool) or not isinstance(cursor, int):
                raise DingTalkIdentityError("INVALID_DIRECTORY_CURSOR")
        return {
            "department": path[0],
            "groups": path,
            "users": list(users.values()),
            "disabled_users": sorted(disabled_users),
        }

    async def authenticate_token(self, user_token: str) -> dict:
        info = await self.request(
            "GET", "https://api.dingtalk.com/v1.0/contact/users/me", headers={"x-acs-dingtalk-access-token": user_token}
        )
        union_id = info.get("unionId")
        if not isinstance(union_id, str) or not union_id:
            raise DingTalkIdentityError("INVALID_USER_IDENTITY")
        converted = (await self.oapi("/topapi/user/getbyunionid", {"unionid": union_id})).get("result")
        if not isinstance(converted, dict) or converted.get("contact_type") != 0:
            raise DingTalkIdentityError("NOT_INTERNAL_MEMBER")
        user_id = converted.get("userid")
        if not isinstance(user_id, str) or not user_id:
            raise DingTalkIdentityError("INVALID_USER_IDENTITY")
        details = await self.user(user_id)
        if details.get("unionid") != union_id or details.get("active") is not True:
            raise DingTalkIdentityError("UNTRUSTED_DIRECTORY_USER")
        return {
            "tenant_key": self.tenant_id,
            "user_id": user_id,
            "display_name": details.get("name") or info.get("nick") or user_id,
            "email": None,
        }

    @staticmethod
    def record_user(record: dict) -> dict:
        user_id = record.get("userid")
        if not isinstance(user_id, str) or not user_id:
            raise DingTalkIdentityError("INVALID_DIRECTORY_USER")
        return {
            "id": user_id,
            "display_name": record.get("name") or user_id,
            "email": None,
            "status": "active" if record.get("active") is True else "disabled",
        }

    async def fetch_snapshot(self) -> DingTalkDirectorySnapshot:
        scopes = (await self.oapi("/auth/scopes")).get("auth_org_scopes")
        if (
            not isinstance(scopes, dict)
            or not isinstance(scopes.get("authed_dept"), list)
            or not isinstance(scopes.get("authed_user"), list)
        ):
            raise DingTalkIdentityError("INVALID_DIRECTORY_SCOPE")
        queue = [str(x) for x in scopes["authed_dept"]]
        groups: dict[str, dict] = {}
        users: dict[str, dict] = {}
        memberships: set[tuple[str, str, str]] = set()
        visited: set[str] = set()
        while queue:
            dept = queue.pop(0)
            if dept in visited:
                continue
            visited.add(dept)
            if len(visited) > self.MAX_ENTRIES:
                raise DingTalkIdentityError("DIRECTORY_LIMIT_EXCEEDED")
            if dept not in groups:
                root = (await self.oapi("/topapi/v2/department/get", {"dept_id": dept})).get("result")
                if not isinstance(root, dict) or str(root.get("dept_id")) != dept or not root.get("name"):
                    raise DingTalkIdentityError("INVALID_DEPARTMENT")
                groups[dept] = {"id": dept, "display_name": root["name"], "principal_type": "department"}
            children = (await self.oapi("/topapi/v2/department/listsub", {"dept_id": dept})).get("result")
            if not isinstance(children, list):
                raise DingTalkIdentityError("INVALID_DEPARTMENT_PAGE")
            for child in children:
                if not isinstance(child, dict) or not child.get("dept_id"):
                    raise DingTalkIdentityError("INVALID_DEPARTMENT")
                child_id = str(child["dept_id"])
                if child_id == dept:
                    raise DingTalkIdentityError("CYCLIC_DEPARTMENT_TREE")
                groups[child_id] = {
                    "id": child_id,
                    "display_name": child.get("name") or child_id,
                    "principal_type": "department",
                }
                memberships.add((dept, "group", child_id))
                queue.append(child_id)
            cursor = 0
            cursors: set[int] = set()
            while True:
                if cursor in cursors:
                    raise DingTalkIdentityError("REPEATED_DIRECTORY_CURSOR")
                cursors.add(cursor)
                page = (await self.oapi("/topapi/v2/user/list", {"dept_id": dept, "cursor": cursor, "size": 100})).get(
                    "result"
                )
                if (
                    not isinstance(page, dict)
                    or not isinstance(page.get("list"), list)
                    or not isinstance(page.get("has_more"), bool)
                ):
                    raise DingTalkIdentityError("INVALID_DIRECTORY_PAGE")
                for record in page["list"]:
                    user = self.record_user(record)
                    users[user["id"]] = user
                    memberships.add((dept, "user", user["id"]))
                if len(users) > self.MAX_ENTRIES or len(memberships) > self.MAX_ENTRIES * 4:
                    raise DingTalkIdentityError("DIRECTORY_LIMIT_EXCEEDED")
                if not page["has_more"]:
                    break
                next_cursor = page.get("next_cursor")
                if not isinstance(next_cursor, int) or isinstance(next_cursor, bool) or next_cursor < 0:
                    raise DingTalkIdentityError("INVALID_DIRECTORY_CURSOR")
                cursor = next_cursor
        for user_id in scopes["authed_user"]:
            if not isinstance(user_id, str) or not user_id:
                raise DingTalkIdentityError("INVALID_DIRECTORY_SCOPE")
            if user_id not in users:
                users[user_id] = self.record_user(await self.user(user_id))
            if len(users) > self.MAX_ENTRIES:
                raise DingTalkIdentityError("DIRECTORY_LIMIT_EXCEEDED")
        graph = {}
        for parent, kind, child in memberships:
            if kind == "group":
                graph.setdefault(parent, set()).add(child)
        # Iterative DFS avoids recursion limits and accepts overlapping authorized roots.
        done, stack_nodes = set(), set()
        for root in graph:
            stack = [(root, False)]
            while stack:
                node, exiting = stack.pop()
                if exiting:
                    stack_nodes.discard(node)
                    done.add(node)
                elif node not in done:
                    if node in stack_nodes:
                        raise DingTalkIdentityError("CYCLIC_DEPARTMENT_TREE")
                    stack_nodes.add(node)
                    stack.append((node, True))
                    stack.extend((child, False) for child in graph.get(node, ()))
        return DingTalkDirectorySnapshot(
            list(users.values()),
            list(groups.values()),
            [{"group_id": g, "member_type": t, "member_id": m} for g, t, m in sorted(memberships)],
        )
