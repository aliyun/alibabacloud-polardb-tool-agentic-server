from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import uuid

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
_CHECKPOINT_VERSION = 1


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
        max_concurrency: int = 8,
        checkpoint_dir: Path | str | None = None,
        checkpoint_key: str | None = None,
    ) -> None:
        if not all((tenant_id, client_id, client_secret)):
            raise ValueError("SharePoint tenant_id, client_id, and client_secret are required")
        if max_concurrency < 1:
            raise ValueError("SharePoint max_concurrency must be positive")
        default_authority_url, default_graph_url = sharepoint_cloud_endpoints(cloud)
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._authority_url = (authority_url or default_authority_url).rstrip("/")
        self._graph_url = (graph_url or default_graph_url).rstrip("/")
        self._graph_origin = urlsplit(self._graph_url)
        self._client = httpx.AsyncClient(transport=transport)
        self._request_semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._access_token: str | None = None
        self._checkpoint_lock = asyncio.Lock()
        self._checkpoint_key = checkpoint_key
        if checkpoint_key is None:
            self._checkpoint_path: Path | None = None
        else:
            state_dir = Path(
                checkpoint_dir
                if checkpoint_dir is not None
                else os.environ.get("PAS_IDENTITY_SYNC_STATE_DIR", "/var/run/pas/identity-sync")
            )
            digest = hashlib.sha256(checkpoint_key.encode("utf-8")).hexdigest()
            self._checkpoint_path = state_dir / f"sharepoint-sync-{digest}.json"

    async def __aenter__(self) -> "SharePointDirectoryClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _new_checkpoint(self) -> dict[str, Any]:
        return {
            "version": _CHECKPOINT_VERSION,
            "source": self._checkpoint_key,
            "stage": "users",
            "has_more": True,
            "sync_started_at": None,
            "users_next_link": None,
            "users_done": False,
            "groups_next_link": None,
            "groups_done": False,
            "membership_last_group_id": None,
            "memberships_done": False,
        }

    async def _load_checkpoint(self) -> dict[str, Any]:
        if self._checkpoint_path is None:
            return self._new_checkpoint()
        try:
            payload = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._new_checkpoint()
        except (OSError, ValueError):
            return self._new_checkpoint()
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _CHECKPOINT_VERSION
            or payload.get("source") != self._checkpoint_key
        ):
            return self._new_checkpoint()
        return payload

    async def _save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        if self._checkpoint_path is None:
            return
        async with self._checkpoint_lock:
            checkpoint_dir = self._checkpoint_path.parent
            checkpoint_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary_path = checkpoint_dir / f".{self._checkpoint_path.name}.{uuid.uuid4().hex}.tmp"
            try:
                with open(temporary_path, "x", encoding="utf-8") as handle:
                    os.chmod(temporary_path, 0o600)
                    handle.write(json.dumps(checkpoint, sort_keys=True, separators=(",", ":")))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self._checkpoint_path)
                os.chmod(self._checkpoint_path, 0o600)
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    async def clear_checkpoint(self) -> None:
        if self._checkpoint_path is None:
            return
        try:
            self._checkpoint_path.unlink()
        except FileNotFoundError:
            pass

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
            async with self._request_semaphore:
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

    async def _visit_pages(
        self,
        url: str,
        *,
        params: dict[str, str] | None,
        on_page: Any,
    ) -> None:
        token = await self._token()
        next_url: str | None = url
        next_params = params
        while next_url is not None:
            async with self._request_semaphore:
                response = await self._client.get(
                    next_url,
                    params=next_params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            payload = self._json_object(response, "directory response")
            values = payload.get("value")
            if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                raise SharePointDirectoryError("Microsoft Graph directory value must be an array of objects")
            next_link = payload.get("@odata.nextLink")
            next_url = self._next_page_url(next_link) if next_link is not None else None
            await on_page(values, next_url)
            next_params = None

    async def sync_to_sink(self, sink: Any) -> None:
        checkpoint = await self._load_checkpoint()
        sync_started_at = checkpoint.get("sync_started_at")
        if not isinstance(sync_started_at, str) or not sync_started_at:
            sync_started_at = datetime.now(UTC).isoformat()
            checkpoint["sync_started_at"] = sync_started_at
            await self._save_checkpoint(checkpoint)
        await sink.begin_sync(sync_started_at)

        async def save_checkpoint(stage: str, has_more: bool) -> None:
            checkpoint["stage"] = stage
            checkpoint["has_more"] = has_more
            await self._save_checkpoint(checkpoint)

        if not checkpoint.get("users_done"):
            async def save_user_page(items: list[dict[str, Any]], next_link: str | None) -> None:
                users: list[dict[str, str | None]] = []
                for item in items:
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
                    users.append({"id": user_id, "display_name": display_name, "email": email})
                await sink.upsert_users(users)
                checkpoint["users_next_link"] = next_link
                checkpoint["users_done"] = next_link is None
                await save_checkpoint("users", next_link is not None)

            await self._visit_pages(
                checkpoint.get("users_next_link") or f"{self._graph_url}/users",
                params=None if checkpoint.get("users_next_link") else {
                    "$select": "id,displayName,mail,userPrincipalName,accountEnabled"
                },
                on_page=save_user_page,
            )

        if not checkpoint.get("groups_done"):
            async def save_group_page(items: list[dict[str, Any]], next_link: str | None) -> None:
                groups: list[dict[str, str]] = []
                for item in items:
                    group_id = item.get("id")
                    display_name = item.get("displayName")
                    if not isinstance(group_id, str) or not group_id:
                        raise SharePointDirectoryError("Microsoft Graph group ID is missing")
                    if not isinstance(display_name, str) or not display_name:
                        raise SharePointDirectoryError("Microsoft Graph group display name is missing")
                    groups.append({"id": group_id, "display_name": display_name, "principal_type": "group"})
                await sink.upsert_groups(groups)
                checkpoint["groups_next_link"] = next_link
                checkpoint["groups_done"] = next_link is None
                await save_checkpoint("groups", next_link is not None)

            await self._visit_pages(
                checkpoint.get("groups_next_link") or f"{self._graph_url}/groups",
                params=None if checkpoint.get("groups_next_link") else {"$select": "id,displayName"},
                on_page=save_group_page,
            )

        if not checkpoint.get("memberships_done"):
            while True:
                last_group_id = checkpoint.get("membership_last_group_id")
                if last_group_id is not None and not isinstance(last_group_id, str):
                    raise SharePointDirectoryError("Microsoft Graph checkpoint is invalid")
                group_ids = await sink.membership_group_ids(last_group_id, self._max_concurrency)
                if not group_ids:
                    checkpoint["memberships_done"] = True
                    break

                async def fetch_group_members(group_id: str) -> None:
                    async def save_membership_page(items: list[dict[str, Any]], _next_link: str | None) -> None:
                        memberships: list[dict[str, str]] = []
                        for item in items:
                            member_id = item.get("id")
                            member_type = item.get("@odata.type")
                            if not isinstance(member_id, str) or not member_id:
                                raise SharePointDirectoryError("Microsoft Graph group member ID is missing")
                            if member_type == "#microsoft.graph.user":
                                memberships.append(
                                    {"group_id": group_id, "member_type": "user", "member_id": member_id}
                                )
                            elif member_type == "#microsoft.graph.group":
                                memberships.append(
                                    {"group_id": group_id, "member_type": "group", "member_id": member_id}
                                )
                        await sink.upsert_memberships(memberships)

                    await self._visit_pages(
                        f"{self._graph_url}/groups/{group_id}/members",
                        params={"$select": "id"},
                        on_page=save_membership_page,
                    )

                await asyncio.gather(*(fetch_group_members(group_id) for group_id in group_ids))
                checkpoint["membership_last_group_id"] = group_ids[-1]
                await save_checkpoint("memberships", True)

        checkpoint["stage"] = "complete"
        checkpoint["has_more"] = False
        await sink.complete_sync()
        await self._save_checkpoint(checkpoint)

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

        async def fetch_group_members(group_id: str) -> list[tuple[str, str, str]]:
            member_items = await self._list_pages(
                f"{self._graph_url}/groups/{group_id}/members",
                params={"$select": "id"},
            )
            group_memberships: list[tuple[str, str, str]] = []
            for item in member_items:
                member_id = item.get("id")
                member_type = item.get("@odata.type")
                if not isinstance(member_id, str) or not member_id:
                    raise SharePointDirectoryError("Microsoft Graph group member ID is missing")
                if member_type == "#microsoft.graph.user" and member_id in users_by_id:
                    group_memberships.append((group_id, "user", member_id))
                elif member_type == "#microsoft.graph.group" and member_id in groups_by_id:
                    group_memberships.append((group_id, "group", member_id))
            return group_memberships

        group_ids = sorted(groups_by_id)
        for index in range(0, len(group_ids), self._max_concurrency):
            group_membership_batches = await asyncio.gather(
                *(fetch_group_members(group_id) for group_id in group_ids[index:index + self._max_concurrency])
            )
            for group_memberships in group_membership_batches:
                memberships.update(group_memberships)

        return SharePointDirectorySnapshot(
            users=[users_by_id[user_id] for user_id in sorted(users_by_id)],
            groups=[groups_by_id[group_id] for group_id in sorted(groups_by_id)],
            memberships=[
                {"group_id": group_id, "member_type": member_type, "member_id": member_id}
                for group_id, member_type, member_id in sorted(memberships)
            ],
        )
