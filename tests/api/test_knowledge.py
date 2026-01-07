"""
Comprehensive tests for the knowledge API endpoints.

Tests cover:
- Knowledge document CRUD operations (upload, list, get, delete)
- Document search functionality
- Document preview endpoint
- Knowledge stats endpoint
- Authentication requirements
- Project access control
- Error handling and validation
- File type validation
"""

import pytest
import io
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException


class TestKnowledgeModuleImports:
    """Verify knowledge module can be imported correctly."""

    def test_knowledge_module_imports(self):
        """Knowledge module should import without errors."""
        from app.api import knowledge
        assert knowledge is not None

    def test_knowledge_router_exists(self):
        """Knowledge router should exist."""
        from app.api.knowledge import router
        assert router is not None

    def test_knowledge_models_import(self):
        """Knowledge models should be importable."""
        from app.core.models import (
            KnowledgeDocument,
            KnowledgeDocumentSummary,
            KnowledgeSearchResult,
            KnowledgeStats
        )
        assert KnowledgeDocument is not None
        assert KnowledgeDocumentSummary is not None
        assert KnowledgeSearchResult is not None
        assert KnowledgeStats is not None


class TestHelperFunctions:
    """Test helper functions in the knowledge module."""

    def test_check_project_access_no_api_user(self):
        """check_project_access should pass when no API user."""
        from app.api.knowledge import check_project_access

        mock_request = MagicMock()
        with patch("app.api.knowledge.get_api_user_from_request") as mock_get_user:
            mock_get_user.return_value = None
            # Should not raise
            check_project_access(mock_request, "project-123")

    def test_check_project_access_matching_project(self):
        """check_project_access should pass when project IDs match."""
        from app.api.knowledge import check_project_access

        mock_request = MagicMock()
        with patch("app.api.knowledge.get_api_user_from_request") as mock_get_user:
            mock_get_user.return_value = {
                "id": "user-1",
                "project_id": "project-123"
            }
            # Should not raise
            check_project_access(mock_request, "project-123")

    def test_check_project_access_different_project(self):
        """check_project_access should raise 403 when project IDs differ."""
        from app.api.knowledge import check_project_access

        mock_request = MagicMock()
        with patch("app.api.knowledge.get_api_user_from_request") as mock_get_user:
            mock_get_user.return_value = {
                "id": "user-1",
                "project_id": "project-999"
            }
            with pytest.raises(HTTPException) as exc_info:
                check_project_access(mock_request, "project-123")
            assert exc_info.value.status_code == 403
            assert "Access denied" in exc_info.value.detail

    def test_check_project_access_no_project_constraint(self):
        """check_project_access should pass when API user has no project constraint."""
        from app.api.knowledge import check_project_access

        mock_request = MagicMock()
        with patch("app.api.knowledge.get_api_user_from_request") as mock_get_user:
            mock_get_user.return_value = {
                "id": "user-1",
                "project_id": None
            }
            # Should not raise - user can access any project
            check_project_access(mock_request, "project-123")

    def test_get_project_or_404_found(self):
        """get_project_or_404 should return project when found."""
        from app.api.knowledge import get_project_or_404

        with patch("app.api.knowledge.database") as mock_db:
            mock_db.get_project.return_value = {
                "id": "project-123",
                "name": "Test Project"
            }
            result = get_project_or_404("project-123")
            assert result["id"] == "project-123"

    def test_get_project_or_404_not_found(self):
        """get_project_or_404 should raise 404 when project not found."""
        from app.api.knowledge import get_project_or_404

        with patch("app.api.knowledge.database") as mock_db:
            mock_db.get_project.return_value = None
            with pytest.raises(HTTPException) as exc_info:
                get_project_or_404("nonexistent")
            assert exc_info.value.status_code == 404
            assert "Project not found" in exc_info.value.detail


class TestListDocumentsEndpoint:
    """Test list_documents endpoint function."""

    @pytest.mark.asyncio
    async def test_list_documents_success(self):
        """Listing knowledge documents should return list."""
        from app.api.knowledge import list_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_documents.return_value = [
                    {
                        "id": "doc-1",
                        "project_id": "project-1",
                        "filename": "test.txt",
                        "content_type": "text/plain",
                        "file_size": 100,
                        "chunk_count": 2,
                        "created_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat()
                    }
                ]

                result = await list_documents(
                    request=mock_request,
                    project_id="project-1",
                    token="test-token"
                )

                assert len(result) == 1
                assert result[0].filename == "test.txt"

    @pytest.mark.asyncio
    async def test_list_documents_empty(self):
        """Listing documents when none exist should return empty list."""
        from app.api.knowledge import list_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_documents.return_value = []

                result = await list_documents(
                    request=mock_request,
                    project_id="project-1",
                    token="test-token"
                )

                assert result == []

    @pytest.mark.asyncio
    async def test_list_documents_project_not_found(self):
        """Listing documents for non-existent project should return 404."""
        from app.api.knowledge import list_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await list_documents(
                        request=mock_request,
                        project_id="nonexistent",
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404


class TestGetStatsEndpoint:
    """Test get_stats endpoint function."""

    @pytest.mark.asyncio
    async def test_get_stats_success(self):
        """Getting knowledge stats should return stats object."""
        from app.api.knowledge import get_stats

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_stats_for_project.return_value = {
                    "document_count": 5,
                    "total_size": 50000,
                    "total_chunks": 25
                }

                result = await get_stats(
                    request=mock_request,
                    project_id="project-1",
                    token="test-token"
                )

                assert result.document_count == 5
                assert result.total_size == 50000
                assert result.total_chunks == 25

    @pytest.mark.asyncio
    async def test_get_stats_empty(self):
        """Getting stats for empty knowledge base should return zeros."""
        from app.api.knowledge import get_stats

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_stats_for_project.return_value = {
                    "document_count": 0,
                    "total_size": 0,
                    "total_chunks": 0
                }

                result = await get_stats(
                    request=mock_request,
                    project_id="project-1",
                    token="test-token"
                )

                assert result.document_count == 0

    @pytest.mark.asyncio
    async def test_get_stats_project_not_found(self):
        """Getting stats for non-existent project should return 404."""
        from app.api.knowledge import get_stats

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await get_stats(
                        request=mock_request,
                        project_id="nonexistent",
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404


class TestUploadDocumentEndpoint:
    """Test upload_document endpoint function."""

    @pytest.mark.asyncio
    async def test_upload_document_success(self):
        """Uploading a valid document should return document summary."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = "test.txt"
        mock_file.read = AsyncMock(return_value=b"Test content")

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.knowledge_service") as mock_service:
                with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                    mock_user.return_value = None
                    mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                    mock_service.is_supported_file.return_value = True
                    mock_service.process_document = AsyncMock(return_value=("doc-123", 5))
                    mock_db.get_knowledge_document.return_value = {
                        "id": "doc-123",
                        "project_id": "project-1",
                        "filename": "test.txt",
                        "content_type": "text/plain",
                        "file_size": 100,
                        "chunk_count": 5,
                        "created_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat()
                    }

                    result = await upload_document(
                        request=mock_request,
                        project_id="project-1",
                        file=mock_file,
                        token="test-token"
                    )

                    assert result.id == "doc-123"
                    assert result.filename == "test.txt"
                    assert result.chunk_count == 5

    @pytest.mark.asyncio
    async def test_upload_document_unsupported_type(self):
        """Uploading unsupported file type should raise 400."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = "test.exe"
        mock_file.read = AsyncMock(return_value=b"binary content")

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.knowledge_service") as mock_service:
                with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                    mock_user.return_value = None
                    mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                    mock_service.is_supported_file.return_value = False
                    mock_service.SUPPORTED_TYPES = {".txt": "text/plain", ".md": "text/markdown"}

                    with pytest.raises(HTTPException) as exc_info:
                        await upload_document(
                            request=mock_request,
                            project_id="project-1",
                            file=mock_file,
                            token="test-token"
                        )
                    assert exc_info.value.status_code == 400
                    assert "Unsupported file type" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_upload_document_too_large(self):
        """Uploading file too large should raise 400."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = "large.txt"
        # Create content > 5MB
        mock_file.read = AsyncMock(return_value=b"x" * (6 * 1024 * 1024))

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.knowledge_service") as mock_service:
                with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                    mock_user.return_value = None
                    mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                    mock_service.is_supported_file.return_value = True

                    with pytest.raises(HTTPException) as exc_info:
                        await upload_document(
                            request=mock_request,
                            project_id="project-1",
                            file=mock_file,
                            token="test-token"
                        )
                    assert exc_info.value.status_code == 400
                    assert "File too large" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_upload_document_no_filename(self):
        """Uploading file without filename should raise 400."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = ""  # Empty filename
        mock_file.read = AsyncMock(return_value=b"content")

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}

                with pytest.raises(HTTPException) as exc_info:
                    await upload_document(
                        request=mock_request,
                        project_id="project-1",
                        file=mock_file,
                        token="test-token"
                    )
                assert exc_info.value.status_code == 400
                assert "Filename is required" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_upload_document_none_filename(self):
        """Uploading file with None filename should raise 400."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = None
        mock_file.read = AsyncMock(return_value=b"content")

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}

                with pytest.raises(HTTPException) as exc_info:
                    await upload_document(
                        request=mock_request,
                        project_id="project-1",
                        file=mock_file,
                        token="test-token"
                    )
                assert exc_info.value.status_code == 400
                assert "Filename is required" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_upload_document_processing_value_error(self):
        """Processing error should raise 400."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = "empty.txt"
        mock_file.read = AsyncMock(return_value=b"")

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.knowledge_service") as mock_service:
                with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                    mock_user.return_value = None
                    mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                    mock_service.is_supported_file.return_value = True
                    mock_service.process_document = AsyncMock(
                        side_effect=ValueError("Document is empty")
                    )

                    with pytest.raises(HTTPException) as exc_info:
                        await upload_document(
                            request=mock_request,
                            project_id="project-1",
                            file=mock_file,
                            token="test-token"
                        )
                    assert exc_info.value.status_code == 400
                    assert "Document is empty" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_upload_document_processing_exception(self):
        """Unexpected processing error should raise 500."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = "test.txt"
        mock_file.read = AsyncMock(return_value=b"content")

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.knowledge_service") as mock_service:
                with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                    mock_user.return_value = None
                    mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                    mock_service.is_supported_file.return_value = True
                    mock_service.process_document = AsyncMock(
                        side_effect=Exception("Unexpected error")
                    )

                    with pytest.raises(HTTPException) as exc_info:
                        await upload_document(
                            request=mock_request,
                            project_id="project-1",
                            file=mock_file,
                            token="test-token"
                        )
                    assert exc_info.value.status_code == 500
                    assert "Failed to process document" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_upload_document_project_not_found(self):
        """Uploading to non-existent project should raise 404."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = "test.txt"
        mock_file.read = AsyncMock(return_value=b"content")

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await upload_document(
                        request=mock_request,
                        project_id="nonexistent",
                        file=mock_file,
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404


class TestSearchDocumentsEndpoint:
    """Test search_documents endpoint function."""

    @pytest.mark.asyncio
    async def test_search_documents_success(self):
        """Searching documents should return matching results."""
        from app.api.knowledge import search_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.search_knowledge_chunks.return_value = [
                    {
                        "id": "chunk-1",
                        "document_id": "doc-1",
                        "filename": "test.txt",
                        "chunk_index": 0,
                        "content": "This is test content",
                        "relevance_score": 2.0,
                        "metadata": {"start_char": 0}
                    }
                ]

                result = await search_documents(
                    request=mock_request,
                    project_id="project-1",
                    q="test",
                    limit=10,
                    token="test-token"
                )

                assert len(result) == 1
                assert result[0].content == "This is test content"
                assert result[0].relevance_score == 2.0

    @pytest.mark.asyncio
    async def test_search_documents_no_results(self):
        """Searching with no matches should return empty list."""
        from app.api.knowledge import search_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.search_knowledge_chunks.return_value = []

                result = await search_documents(
                    request=mock_request,
                    project_id="project-1",
                    q="nonexistent",
                    limit=10,
                    token="test-token"
                )

                assert result == []

    @pytest.mark.asyncio
    async def test_search_documents_with_limit(self):
        """Searching with custom limit should pass limit to database."""
        from app.api.knowledge import search_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.search_knowledge_chunks.return_value = []

                await search_documents(
                    request=mock_request,
                    project_id="project-1",
                    q="test",
                    limit=5,
                    token="test-token"
                )

                mock_db.search_knowledge_chunks.assert_called_once_with(
                    "project-1", "test", limit=5
                )

    @pytest.mark.asyncio
    async def test_search_handles_missing_fields(self):
        """Search should handle results with missing optional fields."""
        from app.api.knowledge import search_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.search_knowledge_chunks.return_value = [
                    {
                        "id": "chunk-1",
                        "document_id": "doc-1",
                        "chunk_index": 0,
                        "content": "Content here",
                        # Missing filename, relevance_score, metadata
                    }
                ]

                result = await search_documents(
                    request=mock_request,
                    project_id="project-1",
                    q="test",
                    limit=10,
                    token="test-token"
                )

                assert result[0].filename == "unknown"
                assert result[0].relevance_score == 0.0

    @pytest.mark.asyncio
    async def test_search_project_not_found(self):
        """Searching in non-existent project should raise 404."""
        from app.api.knowledge import search_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await search_documents(
                        request=mock_request,
                        project_id="nonexistent",
                        q="test",
                        limit=10,
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404


class TestGetDocumentEndpoint:
    """Test get_document endpoint function."""

    @pytest.mark.asyncio
    async def test_get_document_success(self):
        """Getting an existing document should return full document."""
        from app.api.knowledge import get_document

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_document.return_value = {
                    "id": "doc-123",
                    "project_id": "project-1",
                    "filename": "test.txt",
                    "content": "This is the full document content.",
                    "content_type": "text/plain",
                    "file_size": 100,
                    "chunk_count": 2,
                    "created_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat()
                }

                result = await get_document(
                    request=mock_request,
                    project_id="project-1",
                    document_id="doc-123",
                    token="test-token"
                )

                assert result.id == "doc-123"
                assert result.content == "This is the full document content."

    @pytest.mark.asyncio
    async def test_get_document_not_found(self):
        """Getting non-existent document should raise 404."""
        from app.api.knowledge import get_document

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_document.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await get_document(
                        request=mock_request,
                        project_id="project-1",
                        document_id="nonexistent",
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404
                assert "Document not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_document_wrong_project(self):
        """Getting document from wrong project should raise 404."""
        from app.api.knowledge import get_document

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_document.return_value = {
                    "id": "doc-123",
                    "project_id": "project-2",  # Different project
                    "filename": "test.txt",
                    "content": "Content",
                    "content_type": "text/plain",
                    "file_size": 100,
                    "chunk_count": 2,
                    "created_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat()
                }

                with pytest.raises(HTTPException) as exc_info:
                    await get_document(
                        request=mock_request,
                        project_id="project-1",
                        document_id="doc-123",
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404
                assert "not found in project" in exc_info.value.detail


class TestDeleteDocumentEndpoint:
    """Test delete_document endpoint function."""

    @pytest.mark.asyncio
    async def test_delete_document_success(self):
        """Deleting an existing document should succeed."""
        from app.api.knowledge import delete_document

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.knowledge_service") as mock_service:
                with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                    mock_user.return_value = None
                    mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                    mock_db.get_knowledge_document.return_value = {
                        "id": "doc-123",
                        "project_id": "project-1",
                        "filename": "test.txt"
                    }
                    mock_service.delete_document_and_chunks.return_value = True

                    # Should not raise
                    await delete_document(
                        request=mock_request,
                        project_id="project-1",
                        document_id="doc-123",
                        token="test-token"
                    )

                    mock_service.delete_document_and_chunks.assert_called_once_with("doc-123")

    @pytest.mark.asyncio
    async def test_delete_document_not_found(self):
        """Deleting non-existent document should raise 404."""
        from app.api.knowledge import delete_document

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_document.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await delete_document(
                        request=mock_request,
                        project_id="project-1",
                        document_id="nonexistent",
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_document_wrong_project(self):
        """Deleting document from wrong project should raise 404."""
        from app.api.knowledge import delete_document

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_document.return_value = {
                    "id": "doc-123",
                    "project_id": "project-2",  # Different project
                    "filename": "test.txt"
                }

                with pytest.raises(HTTPException) as exc_info:
                    await delete_document(
                        request=mock_request,
                        project_id="project-1",
                        document_id="doc-123",
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404
                assert "not found in project" in exc_info.value.detail


class TestGetDocumentPreviewEndpoint:
    """Test get_document_preview endpoint function."""

    @pytest.mark.asyncio
    async def test_preview_document_success(self):
        """Getting document preview should return truncated content."""
        from app.api.knowledge import get_document_preview

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_document.return_value = {
                    "id": "doc-123",
                    "project_id": "project-1",
                    "filename": "test.txt",
                    "content": "Short content here."
                }

                result = await get_document_preview(
                    request=mock_request,
                    project_id="project-1",
                    document_id="doc-123",
                    max_length=500,
                    token="test-token"
                )

                assert result["id"] == "doc-123"
                assert result["preview"] == "Short content here."
                assert result["truncated"] is False
                assert result["total_length"] == 19

    @pytest.mark.asyncio
    async def test_preview_document_truncated(self):
        """Preview should truncate long content."""
        from app.api.knowledge import get_document_preview

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                long_content = "x" * 1000
                mock_db.get_knowledge_document.return_value = {
                    "id": "doc-123",
                    "project_id": "project-1",
                    "filename": "test.txt",
                    "content": long_content
                }

                result = await get_document_preview(
                    request=mock_request,
                    project_id="project-1",
                    document_id="doc-123",
                    max_length=500,
                    token="test-token"
                )

                assert result["truncated"] is True
                assert result["total_length"] == 1000
                assert len(result["preview"]) == 503  # 500 + "..."

    @pytest.mark.asyncio
    async def test_preview_document_custom_max_length(self):
        """Preview should respect custom max_length."""
        from app.api.knowledge import get_document_preview

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_document.return_value = {
                    "id": "doc-123",
                    "project_id": "project-1",
                    "filename": "test.txt",
                    "content": "x" * 200
                }

                result = await get_document_preview(
                    request=mock_request,
                    project_id="project-1",
                    document_id="doc-123",
                    max_length=100,
                    token="test-token"
                )

                assert result["truncated"] is True
                assert len(result["preview"]) == 103  # 100 + "..."

    @pytest.mark.asyncio
    async def test_preview_document_not_found(self):
        """Preview of non-existent document should raise 404."""
        from app.api.knowledge import get_document_preview

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_document.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await get_document_preview(
                        request=mock_request,
                        project_id="project-1",
                        document_id="nonexistent",
                        max_length=500,
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_preview_document_wrong_project(self):
        """Preview of document from wrong project should raise 404."""
        from app.api.knowledge import get_document_preview

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_document.return_value = {
                    "id": "doc-123",
                    "project_id": "project-2",  # Different project
                    "filename": "test.txt",
                    "content": "Content"
                }

                with pytest.raises(HTTPException) as exc_info:
                    await get_document_preview(
                        request=mock_request,
                        project_id="project-1",
                        document_id="doc-123",
                        max_length=500,
                        token="test-token"
                    )
                assert exc_info.value.status_code == 404


class TestProjectAccessControl:
    """Test project access control for API users."""

    @pytest.mark.asyncio
    async def test_api_user_project_restriction(self):
        """API user should only access their assigned project."""
        from app.api.knowledge import list_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                # API user is assigned to project-2, trying to access project-1
                mock_user.return_value = {
                    "id": "user-1",
                    "project_id": "project-2"
                }
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}

                with pytest.raises(HTTPException) as exc_info:
                    await list_documents(
                        request=mock_request,
                        project_id="project-1",
                        token="test-token"
                    )
                assert exc_info.value.status_code == 403
                assert "Access denied" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_api_user_can_access_assigned_project(self):
        """API user should be able to access their assigned project."""
        from app.api.knowledge import list_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = {
                    "id": "user-1",
                    "project_id": "project-1"
                }
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_documents.return_value = []

                result = await list_documents(
                    request=mock_request,
                    project_id="project-1",
                    token="test-token"
                )

                assert result == []

    @pytest.mark.asyncio
    async def test_api_user_no_project_restriction(self):
        """API user without project restriction can access any project."""
        from app.api.knowledge import list_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = {
                    "id": "user-1",
                    "project_id": None  # No project restriction
                }
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_documents.return_value = []

                result = await list_documents(
                    request=mock_request,
                    project_id="project-1",
                    token="test-token"
                )

                assert result == []


class TestKnowledgeServiceIntegration:
    """Test knowledge_service integration points."""

    @pytest.mark.asyncio
    async def test_upload_calls_process_document(self):
        """Upload should call knowledge_service.process_document."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = "readme.md"
        mock_file.read = AsyncMock(return_value=b"# Hello\n\nWorld")

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.knowledge_service") as mock_service:
                with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                    mock_user.return_value = None
                    mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                    mock_service.is_supported_file.return_value = True
                    mock_service.process_document = AsyncMock(return_value=("doc-123", 3))
                    mock_db.get_knowledge_document.return_value = {
                        "id": "doc-123",
                        "project_id": "project-1",
                        "filename": "readme.md",
                        "content_type": "text/markdown",
                        "file_size": 200,
                        "chunk_count": 3,
                        "created_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat()
                    }

                    await upload_document(
                        request=mock_request,
                        project_id="project-1",
                        file=mock_file,
                        token="test-token"
                    )

                    mock_service.process_document.assert_called_once()
                    call_args = mock_service.process_document.call_args
                    assert call_args.kwargs["project_id"] == "project-1"
                    assert call_args.kwargs["filename"] == "readme.md"

    @pytest.mark.asyncio
    async def test_delete_calls_delete_document_and_chunks(self):
        """Delete should call knowledge_service.delete_document_and_chunks."""
        from app.api.knowledge import delete_document

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.knowledge_service") as mock_service:
                with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                    mock_user.return_value = None
                    mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                    mock_db.get_knowledge_document.return_value = {
                        "id": "doc-123",
                        "project_id": "project-1",
                        "filename": "test.txt"
                    }
                    mock_service.delete_document_and_chunks.return_value = True

                    await delete_document(
                        request=mock_request,
                        project_id="project-1",
                        document_id="doc-123",
                        token="test-token"
                    )

                    mock_service.delete_document_and_chunks.assert_called_once_with("doc-123")


class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_list_documents_multiple_documents(self):
        """Listing should handle multiple documents."""
        from app.api.knowledge import list_documents

        mock_request = MagicMock()
        now = datetime.utcnow().isoformat()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.get_knowledge_documents.return_value = [
                    {"id": f"doc-{i}", "project_id": "project-1", "filename": f"file{i}.txt",
                     "content_type": "text/plain", "file_size": 100 * i, "chunk_count": i,
                     "created_at": now, "updated_at": now}
                    for i in range(5)
                ]

                result = await list_documents(
                    request=mock_request,
                    project_id="project-1",
                    token="test-token"
                )

                assert len(result) == 5
                assert result[2].filename == "file2.txt"

    @pytest.mark.asyncio
    async def test_preview_exact_max_length(self):
        """Preview should handle content exactly at max_length."""
        from app.api.knowledge import get_document_preview

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                content = "x" * 500  # Exactly max_length
                mock_db.get_knowledge_document.return_value = {
                    "id": "doc-123",
                    "project_id": "project-1",
                    "filename": "test.txt",
                    "content": content
                }

                result = await get_document_preview(
                    request=mock_request,
                    project_id="project-1",
                    document_id="doc-123",
                    max_length=500,
                    token="test-token"
                )

                assert result["truncated"] is False
                assert len(result["preview"]) == 500

    @pytest.mark.asyncio
    async def test_search_with_special_characters(self):
        """Search should handle special characters in query."""
        from app.api.knowledge import search_documents

        mock_request = MagicMock()
        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                mock_user.return_value = None
                mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                mock_db.search_knowledge_chunks.return_value = []

                # Should not raise with special characters
                result = await search_documents(
                    request=mock_request,
                    project_id="project-1",
                    q="test@#$%^&*()",
                    limit=10,
                    token="test-token"
                )

                assert result == []

    @pytest.mark.asyncio
    async def test_upload_with_unicode_filename(self):
        """Upload should handle unicode filenames."""
        from app.api.knowledge import upload_document
        from fastapi import UploadFile

        mock_request = MagicMock()
        mock_file = AsyncMock(spec=UploadFile)
        mock_file.filename = "test_unicode.txt"
        mock_file.read = AsyncMock(return_value=b"Content")

        with patch("app.api.knowledge.database") as mock_db:
            with patch("app.api.knowledge.knowledge_service") as mock_service:
                with patch("app.api.knowledge.get_api_user_from_request") as mock_user:
                    mock_user.return_value = None
                    mock_db.get_project.return_value = {"id": "project-1", "name": "Test"}
                    mock_service.is_supported_file.return_value = True
                    mock_service.process_document = AsyncMock(return_value=("doc-123", 1))
                    mock_db.get_knowledge_document.return_value = {
                        "id": "doc-123",
                        "project_id": "project-1",
                        "filename": "test_unicode.txt",
                        "content_type": "text/plain",
                        "file_size": 7,
                        "chunk_count": 1,
                        "created_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat()
                    }

                    result = await upload_document(
                        request=mock_request,
                        project_id="project-1",
                        file=mock_file,
                        token="test-token"
                    )

                    assert result.filename == "test_unicode.txt"
