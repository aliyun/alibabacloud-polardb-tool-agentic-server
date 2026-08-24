from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class FeishuDirectoryError(ValueError):
    pass


@dataclass(frozen=True)
class FeishuDirectorySnapshot:
    users: list[dict[str, str | None]]
    groups: list[dict[str, str]]
    memberships: list[dict[str, str]]


class FeishuDirectoryClient:
    """Fetch Feishu directory facts using tenant-scoped user IDs only."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_url: str = "https://open.feishu.cn",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("Feishu app_id and app_secret are required")
        self._app_id = app_id
        self._app_secret = app_secret
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport)
        self._tenant_access_token: str | None = None

    async def __aenter__(self) -> "FeishuDirectoryClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _token(self) -> str:
        if self._tenant_access_token is not None:
            return self._tenant_access_token
        response = await self._client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        payload = self._response_payload(response)
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuDirectoryError("Feishu tenant access token is missing")
        self._tenant_access_token = token
        return token

    @staticmethod
    def _response_payload(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuDirectoryError("Feishu response must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise FeishuDirectoryError("Feishu response must be a JSON object")
        if payload.get("code") != 0:
            raise FeishuDirectoryError("Feishu API request failed")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise FeishuDirectoryError("Feishu API data must be a JSON object")
        return data

    async def _list_pages(
        self,
        path: str,
        *,
        params: dict[str, str | int],
        item_keys: tuple[str, ...],
    ) -> list[dict[str, Any] | str]:
        token = await self._token()
        items: list[dict[str, Any] | str] = []
        page_token: str | None = None
        while True:
            page_params = dict(params)
            if page_token:
                page_params["page_token"] = page_token
            response = await self._client.get(
                path,
                params=page_params,
                headers={"Authorization": f"Bearer {token}"},
            )
            data = self._response_payload(response)
            page_items: object = []
            for key in item_keys:
                if key in data:
                    page_items = data[key]
                    break
            if not isinstance(page_items, list):
                raise FeishuDirectoryError("Feishu page items must be an array")
            if any(not isinstance(item, (dict, str)) for item in page_items):
                raise FeishuDirectoryError("Feishu page contains an invalid item")
            items.extend(page_items)
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token:
                raise FeishuDirectoryError("Feishu page token is missing")

    async def fetch_snapshot(self) -> FeishuDirectorySnapshot:
        users_by_id: dict[str, dict[str, Any]] = {}
        user_items = await self._list_pages(
            "/open-apis/contact/v3/users",
            params={"user_id_type": "user_id", "page_size": 50},
            item_keys=("items",),
        )
        for item in user_items:
            if not isinstance(item, dict):
                raise FeishuDirectoryError("Feishu user must be an object")
            user_id = item.get("user_id")
            name = item.get("name")
            if not isinstance(user_id, str) or not user_id:
                raise FeishuDirectoryError("Feishu user_id is missing")
            if not isinstance(name, str) or not name:
                raise FeishuDirectoryError("Feishu user name is missing")
            users_by_id[user_id] = item

        memberships: set[tuple[str, str, str]] = set()

        groups_by_id: dict[str, dict[str, str]] = {}
        pending_department_ids = ["0"]
        visited_department_ids: set[str] = set()
        while pending_department_ids:
            parent_department_id = pending_department_ids.pop()
            if parent_department_id in visited_department_ids:
                continue
            visited_department_ids.add(parent_department_id)
            department_users = await self._list_pages(
                "/open-apis/contact/v3/users/find_by_department",
                params={
                    "department_id": parent_department_id,
                    "department_id_type": "open_department_id",
                    "user_id_type": "user_id",
                    "page_size": 50,
                },
                item_keys=("items",),
            )
            for user in department_users:
                if not isinstance(user, dict):
                    raise FeishuDirectoryError("Feishu department user must be an object")
                user_id = user.get("user_id")
                name = user.get("name")
                if not isinstance(user_id, str) or not user_id:
                    raise FeishuDirectoryError("Feishu user_id is missing")
                if not isinstance(name, str) or not name:
                    raise FeishuDirectoryError("Feishu user name is missing")
                users_by_id.setdefault(user_id, user)
                if parent_department_id != "0":
                    memberships.add((parent_department_id, "user", user_id))
            department_items = await self._list_pages(
                f"/open-apis/contact/v3/departments/{parent_department_id}/children",
                params={"department_id_type": "open_department_id", "page_size": 50},
                item_keys=("items",),
            )
            for item in department_items:
                if not isinstance(item, dict):
                    raise FeishuDirectoryError("Feishu department must be an object")
                department_id = item.get("open_department_id")
                name = item.get("name")
                if not isinstance(department_id, str) or not department_id:
                    raise FeishuDirectoryError("Feishu department ID is missing")
                if not isinstance(name, str) or not name:
                    raise FeishuDirectoryError("Feishu department name is missing")
                groups_by_id[department_id] = {
                    "id": department_id,
                    "display_name": name,
                    "principal_type": "department",
                }
                pending_department_ids.append(department_id)

        for group_type in (1, 2):
            groups = await self._list_pages(
                "/open-apis/contact/v3/group/simplelist",
                params={"type": group_type, "page_size": 100},
                item_keys=("grouplist", "items"),
            )
            for item in groups:
                if not isinstance(item, dict):
                    raise FeishuDirectoryError("Feishu group must be an object")
                group_id = item.get("group_id") or item.get("id")
                name = item.get("name")
                if not isinstance(group_id, str) or not group_id:
                    raise FeishuDirectoryError("Feishu group_id is missing")
                if not isinstance(name, str) or not name:
                    raise FeishuDirectoryError("Feishu group name is missing")
                groups_by_id[group_id] = {
                    "id": group_id,
                    "display_name": name,
                    "principal_type": "group",
                }

        for user_id, item in users_by_id.items():
            for group_type in (1, 2):
                user_groups = await self._list_pages(
                    "/open-apis/contact/v3/group/member_belong",
                    params={
                        "member_id": user_id,
                        "member_id_type": "user_id",
                        "group_type": group_type,
                        "page_size": 1000,
                    },
                    item_keys=("group_list", "grouplist", "items"),
                )
                for group in user_groups:
                    group_id = group if isinstance(group, str) else group.get("group_id") or group.get("id")
                    if isinstance(group_id, str) and group_id in groups_by_id:
                        memberships.add((group_id, "user", user_id))

        user_records: list[dict[str, str | None]] = []
        for user_id, item in sorted(users_by_id.items()):
            user_records.append(
                {
                    "id": user_id,
                    "display_name": str(item["name"]),
                    "email": item["email"] if isinstance(item.get("email"), str) else None,
                }
            )
        return FeishuDirectorySnapshot(
            users=user_records,
            groups=sorted(groups_by_id.values(), key=lambda item: item["id"]),
            memberships=[
                {"group_id": group_id, "member_type": member_type, "member_id": member_id}
                for group_id, member_type, member_id in sorted(memberships)
            ],
        )
