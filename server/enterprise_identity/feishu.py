from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from itertools import islice
import json
import logging
import os
from pathlib import Path
import time
from typing import Any
import uuid

import httpx


logger = logging.getLogger(__name__)
_LEGACY_CHECKPOINT_VERSION = 1
_STREAM_CHECKPOINT_VERSION = 2


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
        max_concurrency: int = 8,
        checkpoint_dir: Path | str | None = None,
        checkpoint_key: str | None = None,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("Feishu app_id and app_secret are required")
        if max_concurrency < 1:
            raise ValueError("Feishu max_concurrency must be positive")
        self._app_id = app_id
        self._app_secret = app_secret
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport)
        self._max_concurrency = max_concurrency
        self._request_semaphore = asyncio.Semaphore(max_concurrency)
        self._tenant_access_token: str | None = None
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
            self._checkpoint_path = state_dir / f"feishu-sync-{digest}.json"

    async def __aenter__(self) -> "FeishuDirectoryClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        if self._checkpoint_path is None:
            return
        async with self._checkpoint_lock:
            checkpoint_dir = self._checkpoint_path.parent
            checkpoint_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary_path = checkpoint_dir / f".{self._checkpoint_path.name}.{uuid.uuid4().hex}.tmp"
            payload = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            try:
                with open(temporary_path, "x", encoding="utf-8") as handle:
                    os.chmod(temporary_path, 0o600)
                    handle.write(payload)
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

    async def _request(self, operation: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
        started = time.perf_counter()
        async with self._request_semaphore:
            response = await self._client.request(method, path, **kwargs)
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.log(
            logging.WARNING if response.is_error else logging.INFO,
            "feishu.api_response operation=%s status=%s duration_ms=%s",
            operation,
            response.status_code,
            duration_ms,
            extra={
                "operation": operation,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    async def _token(self) -> str:
        if self._tenant_access_token is not None:
            return self._tenant_access_token
        response = await self._request(
            "tenant_access_token",
            "POST",
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
        operation: str,
        params: dict[str, str | int],
        item_keys: tuple[str, ...],
        page_token: str | None = None,
        on_page: Any = None,
    ) -> list[dict[str, Any] | str]:
        token = await self._token()
        items: list[dict[str, Any] | str] = []
        next_page_token = page_token
        page_index = 0
        while True:
            page_params = dict(params)
            if next_page_token:
                page_params["page_token"] = next_page_token
            response = await self._request(
                operation,
                "GET",
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
            has_more = bool(data.get("has_more"))
            logger.info(
                "feishu.page_progress",
                extra={
                    "operation": operation,
                    "page_index": page_index,
                    "item_count": len(page_items),
                    "has_more": has_more,
                },
            )
            page_token_value = data.get("page_token") if has_more else None
            if has_more and (not isinstance(page_token_value, str) or not page_token_value):
                raise FeishuDirectoryError("Feishu page token is missing")
            if on_page is not None:
                await on_page(page_items, page_token_value, has_more)
            if not has_more:
                return items
            next_page_token = page_token_value
            page_index += 1

    async def _visit_pages(
        self,
        path: str,
        *,
        operation: str,
        params: dict[str, str | int],
        item_keys: tuple[str, ...],
        page_token: str | None = None,
        on_page: Any,
    ) -> None:
        token = await self._token()
        next_page_token = page_token
        page_index = 0
        while True:
            page_params = dict(params)
            if next_page_token:
                page_params["page_token"] = next_page_token
            response = await self._request(
                operation,
                "GET",
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
            has_more = bool(data.get("has_more"))
            page_token_value = data.get("page_token") if has_more else None
            if has_more and (not isinstance(page_token_value, str) or not page_token_value):
                raise FeishuDirectoryError("Feishu page token is missing")
            logger.info(
                "feishu.page_progress",
                extra={
                    "operation": operation,
                    "page_index": page_index,
                    "item_count": len(page_items),
                    "has_more": has_more,
                },
            )
            await on_page(page_items, page_token_value, has_more)
            if not has_more:
                return
            next_page_token = page_token_value
            page_index += 1

    def _new_stream_checkpoint(self) -> dict[str, Any]:
        return {
            "version": _STREAM_CHECKPOINT_VERSION,
            "source": self._checkpoint_key,
            "stage": "users",
            "has_more": True,
            "sync_started_at": None,
            "users_page_token": None,
            "users_done": False,
            "pending_departments": ["0"],
            "visited_departments": [],
            "group_page_tokens": {"1": None, "2": None},
            "groups_done": {"1": False, "2": False},
            "membership_group_type": 1,
            "membership_last_user_id": None,
            "memberships_done": False,
        }

    async def _load_stream_checkpoint(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if self._checkpoint_path is None:
            return self._new_stream_checkpoint(), None
        try:
            payload = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._new_stream_checkpoint(), None
        except (OSError, ValueError):
            logger.warning("feishu.checkpoint_invalid", extra={"has_more": True})
            return self._new_stream_checkpoint(), None
        if not isinstance(payload, dict) or payload.get("source") != self._checkpoint_key:
            logger.warning("feishu.checkpoint_invalid", extra={"has_more": True})
            return self._new_stream_checkpoint(), None
        if payload.get("version") == _STREAM_CHECKPOINT_VERSION:
            return payload, None
        if payload.get("version") == _LEGACY_CHECKPOINT_VERSION:
            return self._new_stream_checkpoint(), payload
        logger.warning("feishu.checkpoint_invalid", extra={"has_more": True})
        return self._new_stream_checkpoint(), None

    @staticmethod
    def _stream_user(item: object) -> dict[str, str | None]:
        if not isinstance(item, dict):
            raise FeishuDirectoryError("Feishu user must be an object")
        user_id = item.get("user_id")
        name = item.get("name")
        if not isinstance(user_id, str) or not user_id:
            raise FeishuDirectoryError("Feishu user_id is missing")
        if not isinstance(name, str) or not name:
            raise FeishuDirectoryError("Feishu user name is missing")
        email = item.get("email")
        return {
            "id": user_id,
            "display_name": name,
            "email": email if isinstance(email, str) else None,
        }

    @staticmethod
    def _stream_group(item: object, *, principal_type: str = "group") -> dict[str, str]:
        if not isinstance(item, dict):
            raise FeishuDirectoryError("Feishu group must be an object")
        group_id = item.get("group_id") or item.get("id") or item.get("open_department_id")
        name = item.get("name")
        if not isinstance(group_id, str) or not group_id:
            raise FeishuDirectoryError("Feishu group ID is missing")
        if not isinstance(name, str) or not name:
            raise FeishuDirectoryError("Feishu group name is missing")
        return {"id": group_id, "display_name": name, "principal_type": principal_type}

    async def _replay_legacy_checkpoint(self, sink: Any, checkpoint: dict[str, Any]) -> None:
        users = checkpoint.get("users")
        groups = checkpoint.get("groups")
        memberships = checkpoint.get("memberships")
        if not isinstance(users, dict) or not isinstance(groups, dict) or not isinstance(memberships, list):
            raise FeishuDirectoryError("Feishu checkpoint is invalid")
        def batches(items: Any, size: int) -> Any:
            iterator = iter(items)
            while batch := list(islice(iterator, size)):
                yield batch

        for batch in batches(users.values(), 100):
            await sink.upsert_users([self._stream_user(item) for item in batch])
        for batch in batches(groups.values(), 100):
            await sink.upsert_groups([
                {
                    "id": str(item.get("id") or ""),
                    "display_name": str(item.get("display_name") or ""),
                    "principal_type": str(item.get("principal_type") or "group"),
                }
                for item in batch
                if isinstance(item, dict)
            ])
        for batch in batches(memberships, 200):
            records: list[dict[str, str]] = []
            for item in batch:
                if (
                    isinstance(item, list)
                    and len(item) == 3
                    and all(isinstance(value, str) for value in item)
                ):
                    records.append(
                        {"group_id": item[0], "member_type": item[1], "member_id": item[2]}
                    )
            await sink.upsert_memberships(records)

    async def sync_to_sink(self, sink: Any) -> None:
        """Stream one Feishu directory run into a durable sink.

        The checkpoint only holds provider cursors and department work.  Directory
        facts are committed by the sink as each page arrives, so a large tenant is
        never accumulated in process memory or in the checkpoint file.
        """
        checkpoint, legacy_checkpoint = await self._load_stream_checkpoint()
        if legacy_checkpoint is not None:
            await self._replay_legacy_checkpoint(sink, legacy_checkpoint)
            checkpoint["users_done"] = bool(legacy_checkpoint.get("users_done"))
            checkpoint["pending_departments"] = list(legacy_checkpoint.get("pending_departments") or [])
            checkpoint["visited_departments"] = list(legacy_checkpoint.get("visited_departments") or [])
            checkpoint["group_page_tokens"] = dict(legacy_checkpoint.get("group_page_tokens") or {"1": None, "2": None})
            checkpoint["groups_done"] = dict(legacy_checkpoint.get("groups_done") or {"1": False, "2": False})
            checkpoint["membership_group_type"] = 1
            checkpoint["membership_last_user_id"] = None
            checkpoint["memberships_done"] = False
            checkpoint["stage"] = "memberships"
            checkpoint["has_more"] = True
            await self._save_checkpoint(checkpoint)

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
            async def save_user_page(
                page_items: list[dict[str, Any] | str], page_token: str | None, has_more: bool
            ) -> None:
                await sink.upsert_users([self._stream_user(item) for item in page_items])
                checkpoint["users_page_token"] = page_token
                checkpoint["users_done"] = not has_more
                await save_checkpoint("users", has_more)

            await self._visit_pages(
                "/open-apis/contact/v3/users",
                operation="users",
                params={"user_id_type": "user_id", "page_size": 50},
                item_keys=("items",),
                page_token=checkpoint.get("users_page_token"),
                on_page=save_user_page,
            )

        pending_department_ids = checkpoint.get("pending_departments")
        visited_department_ids = checkpoint.get("visited_departments")
        if not isinstance(pending_department_ids, list) or not isinstance(visited_department_ids, list):
            raise FeishuDirectoryError("Feishu checkpoint is invalid")
        pending_department_ids = [item for item in pending_department_ids if isinstance(item, str)]
        visited_department_ids = {item for item in visited_department_ids if isinstance(item, str)}

        async def process_department(department_id: str) -> list[str]:
            children: list[str] = []

            async def save_department_users(
                page_items: list[dict[str, Any] | str], _token: str | None, _has_more: bool
            ) -> None:
                users = [self._stream_user(item) for item in page_items]
                await sink.upsert_users(users)
                if department_id != "0":
                    await sink.upsert_memberships(
                        [
                            {"group_id": department_id, "member_type": "user", "member_id": user["id"]}
                            for user in users
                        ]
                    )

            async def save_department_children(
                page_items: list[dict[str, Any] | str], _token: str | None, _has_more: bool
            ) -> None:
                groups = [self._stream_group(item, principal_type="department") for item in page_items]
                await sink.upsert_groups(groups)
                if department_id != "0":
                    await sink.upsert_memberships(
                        [
                            {
                                "group_id": department_id,
                                "member_type": "group",
                                "member_id": group["id"],
                            }
                            for group in groups
                        ]
                    )
                children.extend(group["id"] for group in groups)

            await asyncio.gather(
                self._visit_pages(
                    "/open-apis/contact/v3/users/find_by_department",
                    operation="users.find_by_department",
                    params={
                        "department_id": department_id,
                        "department_id_type": "open_department_id",
                        "user_id_type": "user_id",
                        "page_size": 50,
                    },
                    item_keys=("items",),
                    on_page=save_department_users,
                ),
                self._visit_pages(
                    f"/open-apis/contact/v3/departments/{department_id}/children",
                    operation="departments.children",
                    params={"department_id_type": "open_department_id", "page_size": 50},
                    item_keys=("items",),
                    on_page=save_department_children,
                ),
            )
            return children

        while pending_department_ids:
            current_department_ids: list[str] = []
            while pending_department_ids and len(current_department_ids) < self._max_concurrency:
                department_id = pending_department_ids.pop(0)
                if department_id not in visited_department_ids:
                    current_department_ids.append(department_id)
            visited_department_ids.update(current_department_ids)
            children_by_department = await asyncio.gather(
                *(process_department(department_id) for department_id in current_department_ids)
            )
            for children in children_by_department:
                pending_department_ids.extend(
                    department_id
                    for department_id in children
                    if department_id not in visited_department_ids
                )
            checkpoint["pending_departments"] = pending_department_ids
            checkpoint["visited_departments"] = sorted(visited_department_ids)
            await save_checkpoint("departments", bool(pending_department_ids))

        groups_done = checkpoint.get("groups_done")
        group_page_tokens = checkpoint.get("group_page_tokens")
        if not isinstance(groups_done, dict) or not isinstance(group_page_tokens, dict):
            raise FeishuDirectoryError("Feishu checkpoint is invalid")
        for group_type in (1, 2):
            group_type_key = str(group_type)
            if groups_done.get(group_type_key):
                continue

            async def save_group_page(
                page_items: list[dict[str, Any] | str], page_token: str | None, has_more: bool
            ) -> None:
                await sink.upsert_groups([self._stream_group(item) for item in page_items])
                group_page_tokens[group_type_key] = page_token
                groups_done[group_type_key] = not has_more
                await save_checkpoint("groups", has_more)

            await self._visit_pages(
                "/open-apis/contact/v3/group/simplelist",
                operation="group.simplelist",
                params={"type": group_type, "page_size": 100},
                item_keys=("grouplist", "items"),
                page_token=group_page_tokens.get(group_type_key),
                on_page=save_group_page,
            )

        if not checkpoint.get("memberships_done"):
            membership_errors: Counter[str] = Counter()
            unavailable_membership_user_ids: set[str] = set()
            checkpoint_group_type = checkpoint.get("membership_group_type")
            if not isinstance(checkpoint_group_type, int) or checkpoint_group_type not in (1, 2):
                raise FeishuDirectoryError("Feishu checkpoint is invalid")
            group_type = checkpoint_group_type
            while group_type in (1, 2):
                last_user_id = checkpoint.get("membership_last_user_id")
                if last_user_id is not None and not isinstance(last_user_id, str):
                    raise FeishuDirectoryError("Feishu checkpoint is invalid")
                user_ids = await sink.membership_user_ids(last_user_id, self._max_concurrency)
                if not user_ids:
                    if group_type == 1:
                        group_type = 2
                        checkpoint["membership_group_type"] = group_type
                        checkpoint["membership_last_user_id"] = None
                        await save_checkpoint("memberships", True)
                        continue
                    checkpoint["memberships_done"] = True
                    break

                async def fetch_memberships(user_id: str) -> None:
                    if user_id in unavailable_membership_user_ids:
                        return

                    async def save_membership_page(
                        page_items: list[dict[str, Any] | str],
                        _token: str | None,
                        _has_more: bool,
                    ) -> None:
                        records = []
                        for group in page_items:
                            group_id = group if isinstance(group, str) else group.get("group_id") or group.get("id")
                            if isinstance(group_id, str) and group_id:
                                records.append(
                                    {"group_id": group_id, "member_type": "user", "member_id": user_id}
                                )
                        await sink.upsert_memberships(records)

                    try:
                        await self._visit_pages(
                            "/open-apis/contact/v3/group/member_belong",
                            operation="group.member_belong",
                            params={
                                "member_id": user_id,
                                "member_id_type": "user_id",
                                "group_type": group_type,
                                "page_size": 1000,
                            },
                            item_keys=("group_list", "grouplist", "items"),
                            on_page=save_membership_page,
                        )
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 400:
                            raise
                        try:
                            payload = exc.response.json()
                        except ValueError:
                            payload = {}
                        provider_code = payload.get("code") if isinstance(payload, dict) else None
                        membership_errors[str(provider_code) if provider_code is not None else "HTTP_400"] += 1
                        unavailable_membership_user_ids.add(user_id)
                        await sink.preserve_user_memberships([user_id])

                await asyncio.gather(*(fetch_memberships(user_id) for user_id in user_ids))
                checkpoint["membership_last_user_id"] = user_ids[-1]
                await save_checkpoint("memberships", True)

            if membership_errors:
                warning_details = {
                    "skipped_count": sum(membership_errors.values()),
                    "provider_errors": dict(sorted(membership_errors.items())),
                }
                await sink.record_sync_warning(
                    "MEMBERSHIPS_PARTIAL",
                    warning_details,
                )
                logger.warning(
                    "feishu.memberships_partial",
                    extra=warning_details,
                )

        checkpoint["stage"] = "complete"
        checkpoint["has_more"] = False
        await sink.complete_sync()
        await self._save_checkpoint(checkpoint)

    async def fetch_snapshot(self) -> FeishuDirectorySnapshot:
        class SnapshotSink:
            def __init__(self) -> None:
                self.users: dict[str, dict[str, str | None]] = {}
                self.groups: dict[str, dict[str, str]] = {}
                self.memberships: set[tuple[str, str, str]] = set()

            async def begin_sync(self, _started_at: str) -> None:
                return None

            async def upsert_users(self, users: list[dict[str, str | None]]) -> None:
                for user in users:
                    user_id = user.get("id")
                    if not isinstance(user_id, str):
                        raise FeishuDirectoryError("Feishu user ID is missing")
                    previous = self.users.get(user_id)
                    if previous is not None and user["email"] is None:
                        user = {**user, "email": previous["email"]}
                    self.users[user_id] = user

            async def upsert_groups(self, groups: list[dict[str, str]]) -> None:
                self.groups.update({group["id"]: group for group in groups})

            async def upsert_memberships(self, memberships: list[dict[str, str]]) -> None:
                self.memberships.update(
                    (membership["group_id"], membership["member_type"], membership["member_id"])
                    for membership in memberships
                    if membership["group_id"] in self.groups
                )

            async def preserve_user_memberships(self, _user_ids: list[str]) -> None:
                return None

            async def record_sync_warning(
                self, _code: str, _details: dict[str, object]
            ) -> None:
                return None

            async def membership_user_ids(self, after_user_id: str | None, limit: int) -> list[str]:
                return [
                    user_id
                    for user_id in sorted(self.users)
                    if after_user_id is None or user_id > after_user_id
                ][:limit]

            async def complete_sync(self) -> None:
                return None

        sink = SnapshotSink()
        await self.sync_to_sink(sink)
        return FeishuDirectorySnapshot(
            users=[sink.users[user_id] for user_id in sorted(sink.users)],
            groups=[sink.groups[group_id] for group_id in sorted(sink.groups)],
            memberships=[
                {"group_id": group_id, "member_type": member_type, "member_id": member_id}
                for group_id, member_type, member_id in sorted(sink.memberships)
            ],
        )
