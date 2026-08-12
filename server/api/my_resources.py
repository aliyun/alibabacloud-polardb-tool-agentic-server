from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import get_current_user
from server.auth.principal import Principal, PrincipalKind
from server.core.db_instance_query import query_db_instances
from server.db.engine import get_session
from server.models import User
from server.polarrag.access import list_visible_knowledge_resources

router = APIRouter(prefix="/me", tags=["my-resources"])


@router.get("/resources")
async def list_my_resources(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    database_page = await query_db_instances(
        session,
        Principal(PrincipalKind.USER, user.id),
        limit=200,
    )
    knowledge_resources = await list_visible_knowledge_resources(
        session,
        user,
    )
    return {
        "database_instances": [
            {
                "db_instance_id": instance.db_instance_id,
                "name": instance.name,
                "db_type": instance.db_type,
                "source": instance.source,
                "status": instance.status,
                "permission": instance.permission,
                "capabilities": list(instance.capabilities),
            }
            for instance in database_page.instances
        ],
        "knowledge_resources": [
            {
                "knowledge_resource_id": resource.id,
                "knowledge_space_id": resource.knowledge_space_id,
                "knowledge_space_name": resource.space.name,
                "polarrag_instance_id": resource.polarrag_instance_id,
                "polarrag_instance_name": resource.space.instance.name,
                "name": resource.name,
                "kb_type": resource.kb_type,
                "usage": resource.usage,
                "upload_ready": bool(
                    resource.space.oss_config_validated
                    and resource.space.oss_bucket
                    and resource.space.oss_endpoint
                    and resource.space.oss_object_prefix
                    and resource.space.oss_access_key_id_ciphertext
                    and resource.space.oss_access_key_secret_ciphertext
                ),
            }
            for resource in knowledge_resources
        ],
    }
