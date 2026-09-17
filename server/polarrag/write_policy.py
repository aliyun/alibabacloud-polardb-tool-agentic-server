from __future__ import annotations

from server.models import KnowledgeResource, KnowledgeResourceManagementMode
from server.polarrag.contracts import PolarRAGErrorCode, PolarRAGUpstreamError


def require_pas_managed_resource(resource: KnowledgeResource) -> None:
    if resource.management_mode == KnowledgeResourceManagementMode.EXTERNAL_SYNC:
        raise PolarRAGUpstreamError(
            PolarRAGErrorCode.EXTERNAL_SYNC_RESOURCE_READ_ONLY,
            status_code=409,
        )
