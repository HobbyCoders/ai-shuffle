"""
Documentation API - Serves documentation content for LLM/agent consumption.

Provides structured access to all application documentation including
connector setup guides, feature documentation, and API reference.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from app.api.auth import require_admin, require_auth
from app.core.docs_content import docs_index

router = APIRouter(
    prefix="/api/v1/documentation",
    tags=["Documentation"],
    dependencies=[Depends(require_auth)],
)


@router.get("")
async def list_documentation():
    """
    List all documentation organized by category.

    Returns categories with their docs (metadata only, no content).
    Useful for building navigation or discovering available docs.
    """
    return {"categories": docs_index.list_all()}


@router.get("/search")
async def search_documentation(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
):
    """
    Full-text search across all documentation.

    Returns matching docs with relevance scores and text snippets.
    Searches across titles, descriptions, and content.
    """
    results = docs_index.search(q, limit=limit)
    return {"query": q, "results": results, "total": len(results)}


@router.get("/reload", dependencies=[Depends(require_admin)])
async def reload_documentation():
    """
    Reload documentation from disk.

    Use after adding or modifying markdown files without restarting the server.
    """
    docs_index.reload()
    return {"status": "ok", "message": "Documentation reloaded"}


@router.get("/{category}")
async def get_category(category: str):
    """
    List all docs in a specific category.

    Categories: getting-started, connectors, features, api-reference
    """
    docs = docs_index.get_category_docs(category)
    if docs is None:
        raise HTTPException(
            status_code=404,
            detail=f"Category '{category}' not found. "
            "Valid categories: getting-started, connectors, features, api-reference",
        )
    return {"category": category, "docs": docs}


@router.get("/{category}/{slug}")
async def get_doc(category: str, slug: str):
    """
    Get a single documentation page with full markdown content.

    Returns the complete markdown content along with extracted
    section headings for table-of-contents generation.
    """
    doc = docs_index.get_doc(category, slug)
    if not doc:
        raise HTTPException(
            status_code=404,
            detail=f"Doc '{category}/{slug}' not found",
        )
    return doc
