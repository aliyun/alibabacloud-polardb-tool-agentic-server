from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


_CLOUD_ENDPOINTS = {
    "global": (
        "https://login.microsoftonline.com",
        "https://graph.microsoft.com/v1.0",
    ),
    "china": (
        "https://login.partner.microsoftonline.cn",
        "https://microsoftgraph.chinacloudapi.cn/v1.0",
    ),
}


class SharePointDirectoryError(ValueError):
    pass


def sharepoint_cloud_endpoints(cloud: str) -> tuple[str, str]:
    try:
        return _CLOUD_ENDPOINTS[cloud]
    except KeyError as exc:
        raise ValueError("SharePoint cloud must be global or china") from exc


@dataclass(frozen=True)
class SharePointDirectorySnapshot:
    users: list[dict[str, str | None]]
    groups: list[dict[str, str]]
    memberships: list[dict[str, str]]


class SharePointDirectoryClient:
    """Fetch Entra directory facts with app-only Microsoft Graph access."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        cloud: str = "global",
        authority_url: str | None = None,
        graph_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not all((tenant_id, client_id, client_secret)):
            raise ValueError("SharePoint tenant_id, client_id, and client_secret are required")
        default_authority_url, default_graph_url = sharepoint_cloud_endpoints(cloud)
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._authority_url = (authority_url or default_authority_url).rstrip("/")
        self._graph_url = (graph_url or default_graph_url).rstrip("/")
        self._graph_origin = urlsplit(self._graph_url)
        self._client = httpx.AsyncClient(transport=transport)
        self._access_token: str | None = None

    async def __aenter__(self) -> "SharePointDirectoryClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _json_object(response: httpx.Response, label: str) -> dict[str, Any]:
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise SharePointDirectoryError(f"Microsoft Graph {label} must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise SharePointDirectoryError(f"Microsoft Graph {label} must be a JSON object")
        return payload

    async def _token(self) -> str:
        if self._access_token is not None:
            return self._access_token
        response = await self._client.post(
            f"{self._authority_url}/{self._tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": f"{self._graph_origin.scheme}://{self._graph_origin.netloc}/.default",
                "grant_type": "client_credentials",
            },
        )
        token = self._json_object(response, "token response").get("access_token")
        if not isinstance(token, str) or not token:
            raise SharePointDirectoryError("Microsoft Graph access token is missing")
        self._access_token = token
        return token

    def _next_page_url(self, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise SharePointDirectoryError("Microsoft Graph nextLink is invalid")
        parsed = urlsplit(value)
        if (
            parsed.scheme != self._graph_origin.scheme
            or parsed.netloc != self._graph_origin.netloc
            or not parsed.path.startswith(f"{self._graph_origin.path}/")
        ):
            raise SharePointDirectoryError("Microsoft Graph nextLink is outside the Graph endpoint")
        return value

    async def _list_pages(self, url: str, *, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        token = await self._token()
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params = params
        while next_url is not None:
            response = await self._client.get(
                next_url,
                params=next_params,
                headers={"Authorization": f"Bearer {token}"},
            )
            payload = self._json_object(response, "directory response")
            values = payload.get("value")
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise SharePointDirectoryError("Microsoft Graph directory value must be an array of objects")
            items.extend(values)
            next_link = payload.get("@odata.nextLink")
            next_url = self._next_page_url(next_link) if next_link is not None else None
            next_params = None
        return items

    async def fetch_snapshot(self) -> SharePointDirectorySnapshot:
        user_items = await self._list_pages(
            f"{self._graph_url}/users",
            params={"$select": "id,displayName,mail,userPrincipalName,accountEnabled"},
        )
        users_by_id: dict[str, dict[str, str | None]] = {}
        for item in user_items:
            if item.get("accountEnabled") is False:
                continue
            user_id = item.get("id")
            display_name = item.get("displayName")
            email = item.get("mail") or item.get("userPrincipalName")
            if not isinstance(user_id, str) or not user_id:
                raise SharePointDirectoryError("Microsoft Graph user ID is missing")
            if not isinstance(display_name, str) or not display_name:
                raise SharePointDirectoryError("Microsoft Graph user display name is missing")
            if email is not None and not isinstance(email, str):
                raise SharePointDirectoryError("Microsoft Graph user email is invalid")
            users_by_id[user_id] = {
                "id": user_id,
                "display_name": display_name,
                "email": email,
            }

        group_items = await self._list_pages(
            f"{self._graph_url}/groups",
            params={"$select": "id,displayName"},
        )
        groups_by_id: dict[str, dict[str, str]] = {}
        for item in group_items:
            group_id = item.get("id")
            display_name = item.get("displayName")
            if not isinstance(group_id, str) or not group_id:
                raise SharePointDirectoryError("Microsoft Graph group ID is missing")
            if not isinstance(display_name, str) or not display_name:
                raise SharePointDirectoryError("Microsoft Graph group display name is missing")
            groups_by_id[group_id] = {"id": group_id, "display_name": display_name}

        memberships: set[tuple[str, str, str]] = set()
        for group_id in groups_by_id:
            member_items = await self._list_pages(
                f"{self._graph_url}/groups/{group_id}/members",
                params={"$select": "id"},
            )
            for item in member_items:
                member_id = item.get("id")
                member_type = item.get("@odata.type")
                if not isinstance(member_id, str) or not member_id:
                    raise SharePointDirectoryError("Microsoft Graph group member ID is missing")
                if member_type == "#microsoft.graph.user" and member_id in users_by_id:
                    memberships.add((group_id, "user", member_id))
                elif member_type == "#microsoft.graph.group" and member_id in groups_by_id:
                    memberships.add((group_id, "group", member_id))

        return SharePointDirectorySnapshot(
            users=[users_by_id[user_id] for user_id in sorted(users_by_id)],
            groups=[groups_by_id[group_id] for group_id in sorted(groups_by_id)],
            memberships=[
                {"group_id": group_id, "member_type": member_type, "member_id": member_id}
                for group_id, member_type, member_id in sorted(memberships)
            ],
        )
