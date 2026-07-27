# server/core/provisioning/states.py
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.aliyun.polardb_client import OPENAPI_DUPLICATE_CODES, OpenAPIError
from server.config import get_config
from server.core.binding_manager import is_valid_direct_access_credential
from server.core.crypto import decrypt, encrypt
from server.core.quota_manager import decrement_quota
from server.models import (
    BindingCapability,
    BindingOrigin,
    CredentialCapability,
    CredentialPurpose,
    Instance,
    InstanceCredential,
    InstanceStatus,
    Permission,
    User,
    UserInstanceBinding,
    UserInstanceBindingCapability,
)
from server.models.instance import ProvisioningStep

from .context import ProvisioningContext

logger = logging.getLogger(__name__)


DEDICATED_NETWORK_KEYS = frozenset({
    "region_id", "vpc_id", "vswitch_id", "zone_id", "security_ip_list",
})


async def _load_pool_settings(session) -> dict[str, str]:
    """Load purchase spec + pool network as OpenAPI parameters."""
    return get_config().polardb.provisioning_settings()


class ProvisioningState(ABC):
    """Abstract state in the provisioning state machine."""

    step: ProvisioningStep | None = None

    @abstractmethod
    async def execute(self, ctx: ProvisioningContext) -> "ProvisioningState":
        """Execute this state's logic and return the next state."""
        ...

    async def _fail(
        self, ctx: ProvisioningContext, exc: BaseException
    ) -> "ProvisioningState":
        """Common failure handler: rollback, mark FAILED, decrement quota."""
        try:
            await ctx.session.rollback()
        except Exception:  # noqa: BLE001
            logger.exception("rollback failed during provisioning failure")

        async with ctx.session_factory() as err_session:
            inst = await err_session.get(Instance, ctx.instance_id)
            if inst:
                inst.status = InstanceStatus.FAILED
                await decrement_quota(err_session, inst)
                await err_session.commit()

        logger.exception(
            "provisioning failed at step %s for instance %s",
            self.step,
            ctx.instance_id,
            exc_info=exc,
            extra={
                "metric": "provisioning.failed",
                "instance_id": ctx.instance_id,
                "step": str(self.step),
            },
        )
        return FailedState(exc)


class PendingState(ProvisioningState):
    """Create the cluster and wait for it to be Running."""

    step = ProvisioningStep.PENDING

    async def execute(self, ctx: ProvisioningContext) -> ProvisioningState:
        try:
            from server.core.provisioner import _poll_until_running
            from server.aliyun.polardb_client import get_polardb_client_async

            instance = ctx.instance
            session = ctx.session
            client = await get_polardb_client_async(ctx.session)

            if instance.cluster_id.startswith("pending"):
                settings = await _load_pool_settings(session)

                if await self._should_use_dedicated_path(ctx):
                    await self._create_dedicated_cluster(ctx, settings, client)
                else:
                    result = await client.create_agentic_db(settings)
                    instance.cluster_id = result["cluster_id"]
                    await session.commit()

            timeout = (
                get_config()
                .polardb.resource_pool.provisioning_poll_timeout_seconds
            )
            await _poll_until_running(instance.cluster_id, client, timeout)
            instance.provisioning_step = ProvisioningStep.CLUSTER_READY
            await session.commit()
            return ClusterReadyState()
        except Exception as exc:  # noqa: BLE001
            return await self._fail(ctx, exc)

    async def _should_use_dedicated_path(self, ctx: ProvisioningContext) -> bool:
        """Check if this provisioning should use the dedicated Agentic path."""
        from server.core.provisioner import resolve_provisioning_mode
        from server.models.binding import UserDepartment
        from server.models.user import ProvisioningMode

        session = ctx.session
        user = await session.get(User, ctx.user_id)
        if user is None:
            return False

        mode = resolve_provisioning_mode(user)
        if mode != ProvisioningMode.DEDICATED:
            return False

        membership_result = await session.execute(
            select(UserDepartment).where(
                UserDepartment.user_id == ctx.user_id,
                UserDepartment.is_primary == True,  # noqa: E712
            )
        )
        return membership_result.scalar_one_or_none() is not None

    async def _create_dedicated_cluster(
        self, ctx: ProvisioningContext, base_settings: dict[str, str], client
    ) -> None:
        """Create cluster via PolarDBClient SDK with Agentic params, backfill Department."""
        from server.models.binding import UserDepartment
        from server.models.department import Department

        session = ctx.session
        instance = ctx.instance

        membership_result = await session.execute(
            select(UserDepartment).where(
                UserDepartment.user_id == ctx.user_id,
                UserDepartment.is_primary == True,  # noqa: E712
            )
        )
        membership = membership_result.scalar_one()

        logger.info(
            "dedicated_cluster.resolving_department",
            extra={
                "user_id": ctx.user_id,
                "department_id": membership.department_id,
            },
        )

        # Lock department row to prevent concurrent first-creation race
        dept_result = await session.execute(
            select(Department).where(
                Department.id == membership.department_id
            ).with_for_update()
        )
        dept = dept_result.scalar_one()

        # Build params: purchase cluster spec + network/location from settings
        params = get_config().polardb.agentic_db.spec_settings()
        for key in DEDICATED_NETWORK_KEYS:
            if key in base_settings:
                params[key] = base_settings[key]

        agentic_description = dept.agentic_db_cluster_description or dept.name

        logger.info(
            "dedicated_cluster.creating",
            extra={
                "instance_id": instance.id,
                "department_id": dept.id,
                "department_name": dept.name,
                "agentic_db_cluster_id": dept.agentic_db_cluster_id,
                "params": params,
            },
        )

        response = await client.create_dedicated_cluster(
            params=params,
            agentic_db_type="dedicated",
            agentic_db_cluster_id=dept.agentic_db_cluster_id,
            agentic_db_cluster_description=agentic_description,
            db_cluster_description=instance.name,
        )

        logger.info(
            "dedicated_cluster.created",
            extra={
                "instance_id": instance.id,
                "response": response,
            },
        )

        instance.cluster_id = response["cluster_id"]

        if not dept.agentic_db_cluster_id:
            dept.agentic_db_cluster_id = response["agentic_db_cluster_id"]
            dept.agentic_db_cluster_description = response.get(
                "agentic_db_cluster_description", dept.name
            )

        await session.commit()


class ClusterReadyState(ProvisioningState):
    """Generate and store the encrypted DB password."""

    step = ProvisioningStep.CLUSTER_READY

    async def execute(self, ctx: ProvisioningContext) -> ProvisioningState:
        try:
            from server.core.provisioner import generate_db_password

            instance = ctx.instance
            session = ctx.session

            password = generate_db_password()
            credential = InstanceCredential(
                instance_id=ctx.instance_id,
                name="agentic",
                purpose=CredentialPurpose.DIRECT_ACCESS,
                capability=CredentialCapability.READWRITE,
                username_ciphertext=encrypt("agentic"),
                password_ciphertext=encrypt(password),
                database_name="agentic",
                created_by_user_id=ctx.user_id,
            )
            session.add(credential)
            instance.provisioning_step = ProvisioningStep.PASSWORD_STORED
            await session.commit()
            return PasswordStoredState()
        except Exception as exc:  # noqa: BLE001
            return await self._fail(ctx, exc)


class PasswordStoredState(ProvisioningState):
    """Create the DB account on the cluster."""

    step = ProvisioningStep.PASSWORD_STORED

    async def execute(self, ctx: ProvisioningContext) -> ProvisioningState:
        try:
            from server.aliyun.polardb_client import get_polardb_client_async

            instance = ctx.instance
            session = ctx.session
            client = await get_polardb_client_async(ctx.session)

            credential_row = await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.instance_id == ctx.instance_id,
                    InstanceCredential.name == "agentic",
                )
            )
            credential = credential_row.scalar_one()
            if credential.password_ciphertext is None:
                raise RuntimeError("provisioning credential password is unavailable")
            password = decrypt(credential.password_ciphertext)
            try:
                await client.create_account(
                    instance.cluster_id, "agentic", password
                )
            except OpenAPIError as e:
                if e.code in OPENAPI_DUPLICATE_CODES:
                    pass
                else:
                    raise
            instance.provisioning_step = ProvisioningStep.ACCOUNT_CREATED
            await session.commit()
            return AccountCreatedState()
        except Exception as exc:  # noqa: BLE001
            return await self._fail(ctx, exc)


class AccountCreatedState(ProvisioningState):
    """Create the agentic database on the cluster."""

    step = ProvisioningStep.ACCOUNT_CREATED

    async def execute(self, ctx: ProvisioningContext) -> ProvisioningState:
        try:
            from server.aliyun.polardb_client import get_polardb_client_async

            instance = ctx.instance
            session = ctx.session
            client = await get_polardb_client_async(ctx.session)

            try:
                await client.create_database(
                    instance.cluster_id,
                    "agentic",
                    "agentic",
                    "utf8",
                    "ReadWrite",
                )
            except OpenAPIError as e:
                if e.code in OPENAPI_DUPLICATE_CODES:
                    pass
                else:
                    raise
            instance.provisioning_step = ProvisioningStep.DATABASE_CREATED
            await session.commit()
            return DatabaseCreatedState()
        except Exception as exc:  # noqa: BLE001
            return await self._fail(ctx, exc)


class DatabaseCreatedState(ProvisioningState):
    """Resolve and store the cluster's primary endpoint."""

    step = ProvisioningStep.DATABASE_CREATED

    async def execute(self, ctx: ProvisioningContext) -> ProvisioningState:
        try:
            from server.core.provisioner import resolve_primary_endpoint
            from server.aliyun.polardb_client import get_polardb_client_async

            instance = ctx.instance
            session = ctx.session
            client = await get_polardb_client_async(ctx.session)

            net_type = get_config().polardb.resource_pool.endpoint_net_type
            host, port = await resolve_primary_endpoint(
                client, instance.cluster_id, preferred_net_type=net_type
            )
            instance.host = host
            instance.port = port
            instance.provisioning_step = ProvisioningStep.ENDPOINT_RESOLVED
            await session.commit()
            return EndpointResolvedState()
        except Exception as exc:  # noqa: BLE001
            return await self._fail(ctx, exc)


class EndpointResolvedState(ProvisioningState):
    """Bind the user to the instance and update default-instance pointer."""

    step = ProvisioningStep.ENDPOINT_RESOLVED

    async def execute(self, ctx: ProvisioningContext) -> ProvisioningState:
        try:
            session = ctx.session

            credential = (
                await session.execute(
                    select(InstanceCredential).where(
                        InstanceCredential.instance_id == ctx.instance_id,
                        InstanceCredential.name == "agentic",
                    )
                )
            ).scalar_one()
            if not is_valid_direct_access_credential(
                credential, ctx.instance_id
            ):
                raise RuntimeError("provisioned credential is unavailable")
            binding = (
                await session.execute(
                    select(UserInstanceBinding).where(
                        UserInstanceBinding.instance_id == ctx.instance_id,
                        UserInstanceBinding.user_id == ctx.user_id,
                    )
                )
            ).scalar_one_or_none()
            if binding is None:
                binding = UserInstanceBinding(
                    instance_id=ctx.instance_id,
                    user_id=ctx.user_id,
                    credential_id=credential.id,
                    permission=Permission.READWRITE,
                    origin=BindingOrigin.SYSTEM,
                )
                binding.capabilities = [
                    UserInstanceBindingCapability(
                        capability=BindingCapability.SQL_READ
                    ),
                    UserInstanceBindingCapability(
                        capability=BindingCapability.SQL_WRITE
                    ),
                ]
                session.add(binding)
                try:
                    await session.flush()
                except IntegrityError:
                    await session.rollback()
            else:
                binding.credential_id = credential.id
            user = await session.get(User, ctx.user_id)
            if user and user.default_instance_id is None:
                user.default_instance_id = ctx.instance_id
            instance = await session.get(Instance, ctx.instance_id)
            assert instance is not None
            instance.provisioning_step = ProvisioningStep.BOUND
            await session.commit()
            ctx.instance = instance
            return BoundState()
        except Exception as exc:  # noqa: BLE001
            return await self._fail(ctx, exc)


class BoundState(ProvisioningState):
    """Mark the instance ACTIVE; emit completion metric."""

    step = ProvisioningStep.BOUND

    async def execute(self, ctx: ProvisioningContext) -> ProvisioningState:
        try:
            instance = ctx.instance
            session = ctx.session

            instance.status = InstanceStatus.ACTIVE
            instance.provisioning_step = ProvisioningStep.DONE
            await session.commit()
            duration = time.monotonic() - ctx.start_time
            logger.info(
                "provisioning completed",
                extra={
                    "metric": "provisioning.completed",
                    "instance_id": ctx.instance_id,
                    "duration_seconds": round(duration, 1),
                },
            )
            return CompletedState()
        except Exception as exc:  # noqa: BLE001
            return await self._fail(ctx, exc)


class CompletedState(ProvisioningState):
    """Terminal success state."""

    step = ProvisioningStep.DONE

    async def execute(self, ctx: ProvisioningContext) -> ProvisioningState:
        return self


class FailedState(ProvisioningState):
    """Terminal failure state."""

    step = None

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error

    async def execute(self, ctx: ProvisioningContext) -> ProvisioningState:
        return self


_STEP_TO_STATE: dict[ProvisioningStep, type[ProvisioningState]] = {
    ProvisioningStep.PENDING: PendingState,
    ProvisioningStep.CLUSTER_READY: ClusterReadyState,
    ProvisioningStep.PASSWORD_STORED: PasswordStoredState,
    ProvisioningStep.ACCOUNT_CREATED: AccountCreatedState,
    ProvisioningStep.DATABASE_CREATED: DatabaseCreatedState,
    ProvisioningStep.ENDPOINT_RESOLVED: EndpointResolvedState,
    ProvisioningStep.BOUND: BoundState,
    ProvisioningStep.DONE: CompletedState,
}


def state_from_step(step: ProvisioningStep) -> ProvisioningState:
    """Return the State instance corresponding to a ProvisioningStep."""
    cls = _STEP_TO_STATE.get(step, PendingState)
    return cls()
