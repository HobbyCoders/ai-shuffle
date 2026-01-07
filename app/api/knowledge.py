"""
Knowledge Base API routes

Per-project knowledge base management for RAG context injection.
"""

from typing import List

from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File, Query, Request

from app.core.models import (
    KnowledgeDocument,
    KnowledgeDocumentSummary,
    KnowledgeSearchResult,
    KnowledgeStats
)
from app.core import knowledge_service
from app.db import database
from app.api.auth import require_auth, get_api_user_from_request

router = APIRouter(prefix="/api/v1/projects/{project_id}/knowledge", tags=["Knowledge Base"])


def check_project_access(request: Request, project_id: str) -> None:
    """Check if the user has access to the project."""
    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id"):
        if api_user["project_id"] != project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project"
            )


def get_project_or_404(project_id: str):
    """Get project or raise 404"""
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )
    return project


@router.get("", response_model=List[KnowledgeDocumentSummary])
async def list_documents(
    request: Request,
    project_id: str,
    token: str = Depends(require_auth)
):
    """List all knowledge documents for a project (without full content)"""
    check_project_access(request, project_id)
    get_project_or_404(project_id)

    docs = database.get_knowledge_documents(project_id)
    return [
        KnowledgeDocumentSummary(
            id=doc["id"],
            project_id=doc["project_id"],
            filename=doc["filename"],
            content_type=doc["content_type"],
            file_size=doc["file_size"],
            chunk_count=doc["chunk_count"],
            created_at=doc["created_at"],
            updated_at=doc["updated_at"]
        )
        for doc in docs
    ]


@router.get("/stats", response_model=KnowledgeStats)
async def get_stats(
    request: Request,
    project_id: str,
    token: str = Depends(require_auth)
):
    """Get knowledge base statistics for a project"""
    check_project_access(request, project_id)
    get_project_or_404(project_id)

    stats = database.get_knowledge_stats_for_project(project_id)
    return KnowledgeStats(**stats)


@router.post("", response_model=KnowledgeDocumentSummary, status_code=status.HTTP_201_CREATED)
async def upload_document(
    request: Request,
    project_id: str,
    file: UploadFile = File(...),
    token: str = Depends(require_auth)
):
    """
    Upload a document to the knowledge base.

    Supported file types: .txt, .md, .json, .yaml, .py, .js, .ts, .html, .css, .xml, .csv
    """
    check_project_access(request, project_id)
    get_project_or_404(project_id)

    # Validate file type
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename is required"
        )

    if not knowledge_service.is_supported_file(file.filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type. Supported: {', '.join(knowledge_service.SUPPORTED_TYPES.keys())}"
        )

    # Read file content
    content = await file.read()

    # Check file size (max 5MB)
    max_size = 5 * 1024 * 1024
    if len(content) > max_size:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File too large. Maximum size is 5MB"
        )

    try:
        document_id, chunk_count = await knowledge_service.process_document(
            project_id=project_id,
            filename=file.filename,
            content=content
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process document: {str(e)}"
        )

    # Get and return the created document
    doc = database.get_knowledge_document(document_id)
    return KnowledgeDocumentSummary(
        id=doc["id"],
        project_id=doc["project_id"],
        filename=doc["filename"],
        content_type=doc["content_type"],
        file_size=doc["file_size"],
        chunk_count=doc["chunk_count"],
        created_at=doc["created_at"],
        updated_at=doc["updated_at"]
    )


@router.get("/search", response_model=List[KnowledgeSearchResult])
async def search_documents(
    request: Request,
    project_id: str,
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Maximum results"),
    token: str = Depends(require_auth)
):
    """
    Search the knowledge base for relevant content.

    Uses keyword matching to find relevant chunks.
    """
    check_project_access(request, project_id)
    get_project_or_404(project_id)

    results = database.search_knowledge_chunks(project_id, q, limit=limit)

    return [
        KnowledgeSearchResult(
            id=r["id"],
            document_id=r["document_id"],
            filename=r.get("filename", "unknown"),
            chunk_index=r["chunk_index"],
            content=r["content"],
            relevance_score=r.get("relevance_score", 0.0),
            metadata=r.get("metadata")
        )
        for r in results
    ]


@router.get("/{document_id}", response_model=KnowledgeDocument)
async def get_document(
    request: Request,
    project_id: str,
    document_id: str,
    token: str = Depends(require_auth)
):
    """Get a specific knowledge document with full content"""
    check_project_access(request, project_id)
    get_project_or_404(project_id)

    doc = database.get_knowledge_document(document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document not found: {document_id}"
        )

    # Verify document belongs to this project
    if doc["project_id"] != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document not found in project: {document_id}"
        )

    return KnowledgeDocument(
        id=doc["id"],
        project_id=doc["project_id"],
        filename=doc["filename"],
        content=doc["content"],
        content_type=doc["content_type"],
        file_size=doc["file_size"],
        chunk_count=doc["chunk_count"],
        created_at=doc["created_at"],
        updated_at=doc["updated_at"]
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    request: Request,
    project_id: str,
    document_id: str,
    token: str = Depends(require_auth)
):
    """Delete a knowledge document and all its chunks"""
    check_project_access(request, project_id)
    get_project_or_404(project_id)

    # Verify document exists and belongs to project
    doc = database.get_knowledge_document(document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document not found: {document_id}"
        )

    if doc["project_id"] != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document not found in project: {document_id}"
        )

    knowledge_service.delete_document_and_chunks(document_id)


@router.get("/{document_id}/preview")
async def get_document_preview(
    request: Request,
    project_id: str,
    document_id: str,
    max_length: int = Query(500, ge=100, le=5000),
    token: str = Depends(require_auth)
):
    """Get a preview of a document's content"""
    check_project_access(request, project_id)
    get_project_or_404(project_id)

    doc = database.get_knowledge_document(document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document not found: {document_id}"
        )

    if doc["project_id"] != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document not found in project: {document_id}"
        )

    content = doc["content"]
    preview = content[:max_length]
    if len(content) > max_length:
        preview += "..."

    return {
        "id": doc["id"],
        "filename": doc["filename"],
        "preview": preview,
        "total_length": len(content),
        "truncated": len(content) > max_length
    }
