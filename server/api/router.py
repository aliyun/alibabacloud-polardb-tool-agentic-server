from __future__ import annotations

from fastapi import APIRouter

from server.api.users import router as users_router
from server.api.departments import router as departments_router
from server.api.instances import router as instances_router
from server.api.audit_logs import router as audit_logs_router
from server.api.settings import router as settings_router
from server.api.quota import router as quota_router
from server.api.pool import router as pool_router

router = APIRouter(prefix="/api", tags=["admin"])
router.include_router(users_router)
router.include_router(departments_router)
router.include_router(instances_router)
router.include_router(audit_logs_router)
router.include_router(settings_router)
router.include_router(quota_router)
router.include_router(pool_router)
