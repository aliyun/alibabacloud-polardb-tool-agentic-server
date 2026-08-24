from __future__ import annotations

from fastapi import APIRouter

from server.api.users import router as users_router
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
from server.api.polarrag import router as polarrag_router
from server.api.my_resources import router as my_resources_router
from server.api.agent_polarrag_access import router as agent_polarrag_access_router
from server.api.my_agent_connections import router as my_agent_connections_router
from server.api.polarrag_documents import router as polarrag_documents_router
from server.api.identity_sources import router as identity_sources_router

router = APIRouter(prefix="/api", tags=["admin"])
router.include_router(configuration_router)
router.include_router(users_router)
router.include_router(departments_router)
router.include_router(instances_router)
router.include_router(audit_logs_router)
router.include_router(agents_router)
router.include_router(credentials_router)
router.include_router(provisioning_backends_router)
router.include_router(agent_bindings_router)
router.include_router(user_instance_access_router)
router.include_router(permission_templates_router)
router.include_router(dedicated_pools_router)
router.include_router(db_instance_resources_router)
router.include_router(polarrag_router)
router.include_router(my_resources_router)
router.include_router(agent_polarrag_access_router)
router.include_router(my_agent_connections_router)
router.include_router(polarrag_documents_router)
router.include_router(identity_sources_router)
