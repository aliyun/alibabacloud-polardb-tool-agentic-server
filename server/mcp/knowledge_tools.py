"""Knowledge tool names, available without importing its implementation."""

POLARRAG_UPLOAD_TOOL_NAMES = frozenset(
    {
        "prepare_document_upload",
        "complete_document_upload",
    }
)
POLARRAG_TOOL_NAMES = (
    frozenset(
        {
            "list_knowledge_resources",
            "kb_search",
            "kb_fetch_context",
            "doc_list_chunks",
            "doc_find_by_name",
            "doc_status",
            "doc_recall",
            "doc_get_original",
            "doc_delete",
            "doc_rechunk",
        }
    )
    | POLARRAG_UPLOAD_TOOL_NAMES
)
