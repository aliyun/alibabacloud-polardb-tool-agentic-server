from __future__ import annotations

from sqlalchemy import select

from server.auth.credential_mutation import (
    BuiltinPasswordRequired,
    CredentialMutationMode,
    OldPasswordIncorrect,
    OldPasswordNotAllowed,
    PasswordModificationNotAllowed,
    mutate_builtin_password_in_session,
)
from server.auth.password_policy import PasswordPolicyError
from server.configuration.idempotency import (
    ManagedCommandIdempotency,
    ManagedIdempotencyError,
    sanitized_result,
)
from server.configuration.service import ConfigService
from server.management.account_types import (
    ManagedAccount,
    ManagedAccountMutationResult,
    ManagedAccountPasswordEnvelope,
)
from server.management.identity import (
    ManagedIdentityBinder,
    ManagedIdentityError,
)
from server.management.types import ManagedTarget
from server.models import AuthProvider, User, UserRole, UserStatus


class ManagedAccountError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_exact_managed_admin(user: User | None) -> bool:
    """Reject collation-equivalent identities other than literal admin."""
    return (
        user is not None
        and user.external_id == "admin"
        and user.auth_provider == AuthProvider.BUILTIN
        and user.role == UserRole.ADMIN
        and user.status == UserStatus.ACTIVE
    )


class ManagedAccountService:
    def __init__(
        self,
        config_service: ConfigService,
        identity_binder: ManagedIdentityBinder,
    ) -> None:
        self.config_service = config_service
        self.repository = config_service.repository
        self.crypto = config_service.crypto
        self.identity_binder = identity_binder
        self.idempotency = ManagedCommandIdempotency(
            self.repository,
            self.crypto,
        )

    async def describe_admin(self, target: ManagedTarget) -> ManagedAccount:
        await self._verify_bound_identity(target)
        async with self.repository.session_factory() as session:
            user = await session.scalar(
                select(User).where(
                    User.external_id == "admin",
                    User.auth_provider == AuthProvider.BUILTIN,
                    User.role == UserRole.ADMIN,
                    User.status == UserStatus.ACTIVE,
                )
            )
        if not is_exact_managed_admin(user):
            raise ManagedAccountError("ACCOUNT_NOT_FOUND")
        return ManagedAccount(password_status=user.effective_password_state)

    async def mutate_admin_password(
        self,
        envelope: ManagedAccountPasswordEnvelope,
        *,
        lease_owner: str,
    ) -> ManagedAccountMutationResult:
        target = envelope.target
        await self._verify_bound_identity(target)
        try:
            claim = await self.idempotency.claim(
                action=(f"account.password.{envelope.operation.value.lower()}"),
                module=None,
                actor_scope=(f"control:{target.instance_id}:{target.generation}"),
                idempotency_key=envelope.idempotency_key,
                request=envelope.model_dump(mode="json"),
                instance_id=target.instance_id,
                instance_generation=target.generation,
                lease_owner=lease_owner,
            )
        except ManagedIdempotencyError as error:
            raise ManagedAccountError(error.code) from error

        if claim.status == "REPLAY":
            return ManagedAccountMutationResult.model_validate(claim.response)

        async def mutate(session):
            user = await session.scalar(
                select(User)
                .where(
                    User.external_id == "admin",
                    User.auth_provider == AuthProvider.BUILTIN,
                    User.role == UserRole.ADMIN,
                    User.status == UserStatus.ACTIVE,
                )
                .with_for_update()
            )
            if not is_exact_managed_admin(user):
                raise ManagedAccountError("ACCOUNT_NOT_FOUND")
            password_status = await mutate_builtin_password_in_session(
                session,
                user=user,
                mode=CredentialMutationMode(envelope.operation.value),
                old_password=envelope.old_password,
                new_password=envelope.new_password,
            )
            return sanitized_result(
                ManagedAccountMutationResult(
                    password_status=password_status,
                )
            )

        try:
            result = await self.idempotency.complete(
                claim,
                lease_owner=lease_owner,
                mutation=mutate,
            )
        except (
            BuiltinPasswordRequired,
            ManagedAccountError,
            OldPasswordIncorrect,
            OldPasswordNotAllowed,
            PasswordModificationNotAllowed,
            PasswordPolicyError,
        ) as error:
            try:
                await self.idempotency.abandon(
                    claim,
                    lease_owner=lease_owner,
                )
            except ManagedIdempotencyError as abandon_error:
                raise ManagedAccountError(abandon_error.code) from abandon_error
            if isinstance(error, ManagedAccountError):
                raise
            if isinstance(error, OldPasswordIncorrect):
                code = "OLD_PASSWORD_INCORRECT"
            elif isinstance(error, BuiltinPasswordRequired):
                code = "ACCOUNT_NOT_FOUND"
            else:
                code = "INVALID_ACCOUNT_PASSWORD"
            raise ManagedAccountError(code) from error
        except ManagedIdempotencyError as error:
            raise ManagedAccountError(error.code) from error
        return ManagedAccountMutationResult.model_validate(result)

    async def _verify_bound_identity(self, target: ManagedTarget) -> None:
        result = await self.identity_binder.verify(target)
        if result.status != "BOUND":
            raise ManagedIdentityError("INSTANCE_IDENTITY_MISMATCH")
