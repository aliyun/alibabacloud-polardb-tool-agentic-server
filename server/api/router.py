from __future__ import annotations

from fastapi import APIRouter

from server.api.users import router as users_router
from server.api.access_console import router as access_console_router
from server.api.personal_tokens import router as personal_tokens_router
from server.api.features import router as features_router
from server.api.my_resources import router as my_resources_router
from server.api.agent_polarrag_access import router as agent_access_router
from server.api.departments import router as departments_router
from server.api.instances import router as instances_router
from server.api.audit_logs import router as audit_logs_router
from server.api.agents import router as agents_router
from server.api.credentials import router as credentials_router
from server.api.provisioning_backends import (
    router as provisioning_backends_router,
)
from server.api.agent_bindings import router as agent_bindings_router
from server.api.user_instance_access import (
    router as user_instance_access_router,
)
from server.api.configuration import router as configuration_router
from server.api.permission_templates import router as permission_templates_router
from server.api.dedicated_pools import router as dedicated_pools_router
from server.api.db_instance_resources import router as db_instance_resources_router
from server.api.my_agent_connections import router as my_agent_connections_router
from server.api.identity_sources import router as identity_sources_router
from server.api.my_workspace import router as my_workspace_router
from server.api.external_auth import router as external_auth_router
from server.api.external_applications import (
    router as external_applications_router,
)
core_router = APIRouter(prefix="/api", tags=["admin"])
core_router.include_router(configuration_router)
core_router.include_router(features_router)
core_router.include_router(my_resources_router)
core_router.include_router(users_router)
core_router.include_router(access_console_router)
core_router.include_router(personal_tokens_router)
core_router.include_router(agent_access_router)
core_router.include_router(departments_router)
core_router.include_router(instances_router)
core_router.include_router(audit_logs_router)
core_router.include_router(agents_router)
core_router.include_router(credentials_router)
core_router.include_router(provisioning_backends_router)
core_router.include_router(agent_bindings_router)
core_router.include_router(user_instance_access_router)
core_router.include_router(permission_templates_router)
core_router.include_router(dedicated_pools_router)
core_router.include_router(db_instance_resources_router)
core_router.include_router(my_agent_connections_router)
core_router.include_router(identity_sources_router)
core_router.include_router(my_workspace_router)
core_router.include_router(external_auth_router)
core_router.include_router(external_applications_router)


def build_knowledge_router() -> APIRouter:
    from server.api import (
        agent_knowledge_access,
        agent_knowledge_scopes,
        platform_polarrag,
        polarrag,
        polarrag_documents,
    )

    result = APIRouter(prefix="/api", tags=["knowledge"])
    for module in (
        polarrag,
        agent_knowledge_access,
        agent_knowledge_scopes,
        polarrag_documents,
        platform_polarrag,
    ):
        result.include_router(module.router)
    return result


def __getattr__(name: str):
    if name == "router":
        result = APIRouter()
        result.include_router(core_router)
        result.include_router(build_knowledge_router())
        return result
    raise AttributeError(name)
