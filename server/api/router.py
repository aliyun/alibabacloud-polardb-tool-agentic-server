from __future__ import annotations

from fastapi import APIRouter

from server.api.users import router as users_router
from server.api.departments import router as departments_router
from server.api.instances import router as instances_router
from server.api.audit_logs import router as audit_logs_router
from server.api.quota import router as quota_router
from server.api.pool import router as pool_router
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

router = APIRouter(prefix="/api", tags=["admin"])
router.include_router(configuration_router)
router.include_router(users_router)
router.include_router(departments_router)
router.include_router(instances_router)
router.include_router(audit_logs_router)
router.include_router(quota_router)
router.include_router(pool_router)
router.include_router(agents_router)
router.include_router(credentials_router)
router.include_router(provisioning_backends_router)
router.include_router(agent_bindings_router)
router.include_router(user_instance_access_router)
