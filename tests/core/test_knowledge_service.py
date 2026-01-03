"""
Unit tests for knowledge service module.

Tests cover:
- File type detection and content type mapping
- Text extraction from file bytes
- Document chunking with overlap
- Document processing and storage
- Context retrieval with keyword search
- Context formatting for prompts
- Document deletion
- Error handling and edge cases
"""

import pytest
import uuid
from unittest.mock import patch, MagicMock, call

from app.core import knowledge_service


class TestGetContentType:
    """Test MIME type detection based on file extension."""

    def test_text_file(self):
        """Should return text/plain for .txt files."""
        assert knowledge_service.get_content_type("document.txt") == "text/plain"

    def test_markdown_file(self):
        """Should return text/markdown for .md files."""
        assert knowledge_service.get_content_type("readme.md") == "text/markdown"

    def test_markdown_full_extension(self):
        """Should return text/markdown for .markdown files."""
        assert knowledge_service.get_content_type("docs.markdown") == "text/markdown"

    def test_json_file(self):
        """Should return application/json for .json files."""
        assert knowledge_service.get_content_type("config.json") == "application/json"

    def test_yaml_file(self):
        """Should return application/yaml for .yaml files."""
        assert knowledge_service.get_content_type("config.yaml") == "application/yaml"

    def test_yml_file(self):
        """Should return application/yaml for .yml files."""
        assert knowledge_service.get_content_type("config.yml") == "application/yaml"

    def test_python_file(self):
        """Should return text/x-python for .py files."""
        assert knowledge_service.get_content_type("script.py") == "text/x-python"

    def test_javascript_file(self):
        """Should return application/javascript for .js files."""
        assert knowledge_service.get_content_type("app.js") == "application/javascript"

    def test_typescript_file(self):
        """Should return application/typescript for .ts files."""
        assert knowledge_service.get_content_type("app.ts") == "application/typescript"

    def test_html_file(self):
        """Should return text/html for .html files."""
        assert knowledge_service.get_content_type("index.html") == "text/html"

    def test_css_file(self):
        """Should return text/css for .css files."""
        assert knowledge_service.get_content_type("styles.css") == "text/css"

    def test_xml_file(self):
        """Should return application/xml for .xml files."""
        assert knowledge_service.get_content_type("data.xml") == "application/xml"

    def test_csv_file(self):
        """Should return text/csv for .csv files."""
        assert knowledge_service.get_content_type("data.csv") == "text/csv"

    def test_log_file(self):
        """Should return text/plain for .log files."""
        assert knowledge_service.get_content_type("app.log") == "text/plain"

    def test_unknown_extension(self):
        """Should return text/plain for unknown extensions."""
        assert knowledge_service.get_content_type("document.xyz") == "text/plain"

    def test_no_extension(self):
        """Should return text/plain for files without extension."""
        assert knowledge_service.get_content_type("Makefile") == "text/plain"

    def test_case_insensitive(self):
        """Should handle uppercase extensions."""
        assert knowledge_service.get_content_type("README.MD") == "text/markdown"
        assert knowledge_service.get_content_type("CONFIG.JSON") == "application/json"

    def test_nested_path(self):
        """Should handle full file paths."""
        assert knowledge_service.get_content_type("/path/to/document.txt") == "text/plain"
        assert knowledge_service.get_content_type("src/config.yaml") == "application/yaml"


class TestIsSupportedFile:
    """Test file type support detection."""

    def test_supported_text_file(self):
        """Should return True for supported .txt files."""
        assert knowledge_service.is_supported_file("document.txt") is True

    def test_supported_markdown_file(self):
        """Should return True for supported .md files."""
        assert knowledge_service.is_supported_file("readme.md") is True

    def test_supported_code_files(self):
        """Should return True for supported code files."""
        assert knowledge_service.is_supported_file("script.py") is True
        assert knowledge_service.is_supported_file("app.js") is True
        assert knowledge_service.is_supported_file("app.ts") is True

    def test_supported_config_files(self):
        """Should return True for supported config files."""
        assert knowledge_service.is_supported_file("config.json") is True
        assert knowledge_service.is_supported_file("config.yaml") is True
        assert knowledge_service.is_supported_file("config.yml") is True

    def test_unsupported_binary_files(self):
        """Should return False for unsupported binary files."""
        assert knowledge_service.is_supported_file("image.png") is False
        assert knowledge_service.is_supported_file("document.pdf") is False
        assert knowledge_service.is_supported_file("archive.zip") is False

    def test_unsupported_extension(self):
        """Should return False for unknown extensions."""
        assert knowledge_service.is_supported_file("file.xyz") is False

    def test_no_extension(self):
        """Should return False for files without extension."""
        assert knowledge_service.is_supported_file("Makefile") is False

    def test_case_insensitive(self):
        """Should handle uppercase extensions."""
        assert knowledge_service.is_supported_file("README.MD") is True
        assert knowledge_service.is_supported_file("CONFIG.JSON") is True


class TestExtractTextFromFile:
    """Test text extraction from file bytes."""

    def test_extract_utf8_content(self):
        """Should decode UTF-8 content correctly."""
        content = "Hello, world! Unicode: \u00e9\u00e8\u00ea"
        result = knowledge_service.extract_text_from_file(
            content.encode("utf-8"),
            "test.txt"
        )
        assert result == content

    def test_extract_latin1_content(self):
        """Should fall back to latin-1 for non-UTF-8 content."""
        # Latin-1 specific character that's not valid UTF-8 alone
        content = "Hello \xe9\xe8\xea"  # latin-1 encoded accented chars
        result = knowledge_service.extract_text_from_file(
            content.encode("latin-1"),
            "test.txt"
        )
        assert result == content

    def test_extract_invalid_encoding(self):
        """Should use lossy decode for completely invalid encoding."""
        # Create bytes that are invalid in both UTF-8 and latin-1
        # This is tricky since latin-1 accepts all bytes, so test UTF-8 with replacement
        invalid_bytes = b"Hello \xff\xfe world"
        result = knowledge_service.extract_text_from_file(invalid_bytes, "test.txt")
        # Should contain at least the ASCII parts
        assert "Hello" in result
        assert "world" in result

    def test_extract_empty_content(self):
        """Should handle empty content."""
        result = knowledge_service.extract_text_from_file(b"", "test.txt")
        assert result == ""

    def test_extract_multiline_content(self):
        """Should preserve newlines."""
        content = "Line 1\nLine 2\nLine 3"
        result = knowledge_service.extract_text_from_file(
            content.encode("utf-8"),
            "test.txt"
        )
        assert result == content


class TestChunkDocument:
    """Test document chunking functionality."""

    def test_chunk_empty_content(self):
        """Should return empty list for empty content."""
        assert knowledge_service.chunk_document("") == []
        assert knowledge_service.chunk_document("   ") == []
        assert knowledge_service.chunk_document("\n\n") == []

    def test_chunk_none_content(self):
        """Should handle None content."""
        assert knowledge_service.chunk_document(None) == []

    def test_chunk_small_document(self):
        """Should return single chunk for small document."""
        content = "This is a small document."
        chunks = knowledge_service.chunk_document(content)

        assert len(chunks) == 1
        assert chunks[0]["content"] == content
        assert "start_char" in chunks[0]["metadata"]
        assert "end_char" in chunks[0]["metadata"]

    def test_chunk_preserves_paragraphs(self):
        """Should split on paragraph boundaries."""
        content = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        chunks = knowledge_service.chunk_document(content, chunk_size=50, chunk_overlap=0)

        # Should keep paragraphs together when possible
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk["content"].strip() != ""

    def test_chunk_with_overlap(self):
        """Should include overlap between chunks."""
        # Create content that will need multiple chunks
        para1 = "First paragraph with some content. " * 20  # ~700 chars
        para2 = "Second paragraph with more content. " * 20  # ~720 chars
        content = para1 + "\n\n" + para2

        chunks = knowledge_service.chunk_document(
            content,
            chunk_size=800,
            chunk_overlap=100
        )

        assert len(chunks) >= 2

    def test_chunk_without_overlap(self):
        """Should work without overlap."""
        para1 = "First paragraph. " * 50
        para2 = "Second paragraph. " * 50
        content = para1 + "\n\n" + para2

        chunks = knowledge_service.chunk_document(
            content,
            chunk_size=500,
            chunk_overlap=0
        )

        assert len(chunks) >= 2

    def test_chunk_metadata_has_positions(self):
        """Should include character positions in metadata."""
        content = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = knowledge_service.chunk_document(content)

        for chunk in chunks:
            assert "metadata" in chunk
            assert "start_char" in chunk["metadata"]
            assert "end_char" in chunk["metadata"]
            assert isinstance(chunk["metadata"]["start_char"], int)
            assert isinstance(chunk["metadata"]["end_char"], int)

    def test_chunk_long_single_paragraph(self):
        """Should handle very long single paragraph."""
        content = "Word " * 500  # ~2500 characters

        chunks = knowledge_service.chunk_document(
            content,
            chunk_size=800,
            chunk_overlap=100
        )

        # Even a long single paragraph should be returned
        assert len(chunks) >= 1
        total_content = "".join(c["content"] for c in chunks)
        # Should contain at least some of the content
        assert "Word" in total_content

    def test_chunk_uses_default_settings(self):
        """Should use default chunk size and overlap."""
        content = "Test paragraph.\n\n" * 50

        chunks = knowledge_service.chunk_document(content)

        # Should create chunks (using defaults)
        assert len(chunks) >= 1

    def test_chunk_strips_whitespace(self):
        """Should strip whitespace from chunks."""
        content = "  Paragraph with spaces.  \n\n  Another paragraph.  "
        chunks = knowledge_service.chunk_document(content)

        for chunk in chunks:
            assert chunk["content"] == chunk["content"].strip()


class TestProcessDocument:
    """Test document processing and storage."""

    @pytest.fixture
    def mock_database(self):
        """Mock the database module."""
        with patch.object(knowledge_service, "database") as mock_db:
            mock_db.create_knowledge_document = MagicMock()
            mock_db.create_knowledge_chunk = MagicMock()
            yield mock_db

    @pytest.fixture
    def mock_uuid(self):
        """Mock UUID generation for deterministic tests."""
        with patch.object(knowledge_service, "uuid") as mock:
            call_count = [0]
            def generate_uuid():
                call_count[0] += 1
                return MagicMock(
                    __str__=lambda self: f"test-uuid-{call_count[0]}"
                )
            mock.uuid4 = generate_uuid
            yield mock

    @pytest.mark.asyncio
    async def test_process_document_success(self, mock_database, mock_uuid):
        """Should process and store a document successfully."""
        project_id = "test-project"
        filename = "test.txt"
        content = b"This is test content.\n\nWith multiple paragraphs."

        doc_id, chunk_count = await knowledge_service.process_document(
            project_id, filename, content
        )

        assert doc_id == "test-uuid-1"
        assert chunk_count >= 1
        mock_database.create_knowledge_document.assert_called_once()
        assert mock_database.create_knowledge_chunk.call_count == chunk_count

    @pytest.mark.asyncio
    async def test_process_document_empty_content_raises(self, mock_database):
        """Should raise ValueError for empty content."""
        with pytest.raises(ValueError, match="empty"):
            await knowledge_service.process_document(
                "project-id",
                "empty.txt",
                b""
            )

    @pytest.mark.asyncio
    async def test_process_document_whitespace_only_raises(self, mock_database):
        """Should raise ValueError for whitespace-only content."""
        with pytest.raises(ValueError, match="empty"):
            await knowledge_service.process_document(
                "project-id",
                "whitespace.txt",
                b"   \n\n   "
            )

    @pytest.mark.asyncio
    async def test_process_document_stores_correct_metadata(self, mock_database, mock_uuid):
        """Should store correct document metadata."""
        project_id = "test-project"
        filename = "document.md"
        content = b"# Heading\n\nContent here."

        await knowledge_service.process_document(project_id, filename, content)

        call_args = mock_database.create_knowledge_document.call_args
        assert call_args.kwargs["document_id"] == "test-uuid-1"
        assert call_args.kwargs["project_id"] == project_id
        assert call_args.kwargs["filename"] == filename
        assert call_args.kwargs["content_type"] == "text/markdown"
        assert call_args.kwargs["file_size"] == len(content)

    @pytest.mark.asyncio
    async def test_process_document_creates_chunks(self, mock_database, mock_uuid):
        """Should create chunks with correct indices."""
        content = "Paragraph 1.\n\n" * 20  # Multiple paragraphs

        await knowledge_service.process_document(
            "project-id",
            "test.txt",
            content.encode("utf-8")
        )

        # Verify chunks were created
        assert mock_database.create_knowledge_chunk.called

        # Check chunk indices are sequential
        calls = mock_database.create_knowledge_chunk.call_args_list
        for i, c in enumerate(calls):
            assert c.kwargs["chunk_index"] == i


class TestGetRelevantContext:
    """Test context retrieval for queries."""

    @pytest.fixture
    def mock_database(self):
        """Mock the database module."""
        with patch.object(knowledge_service, "database") as mock_db:
            yield mock_db

    def test_get_context_empty_query(self, mock_database):
        """Should return empty string for empty query."""
        result = knowledge_service.get_relevant_context("project-id", "")
        assert result == ""
        mock_database.search_knowledge_chunks.assert_not_called()

    def test_get_context_empty_project_id(self, mock_database):
        """Should return empty string for empty project ID."""
        result = knowledge_service.get_relevant_context("", "test query")
        assert result == ""
        mock_database.search_knowledge_chunks.assert_not_called()

    def test_get_context_none_values(self, mock_database):
        """Should handle None values."""
        result = knowledge_service.get_relevant_context(None, "query")
        assert result == ""
        result = knowledge_service.get_relevant_context("project", None)
        assert result == ""

    def test_get_context_no_results(self, mock_database):
        """Should return empty string when no matching chunks."""
        mock_database.search_knowledge_chunks.return_value = []

        result = knowledge_service.get_relevant_context("project-id", "query")

        assert result == ""

    def test_get_context_single_result(self, mock_database):
        """Should format single result correctly."""
        mock_database.search_knowledge_chunks.return_value = [
            {"filename": "test.txt", "content": "Relevant content here."}
        ]

        result = knowledge_service.get_relevant_context("project-id", "query")

        assert "[From: test.txt]" in result
        assert "Relevant content here." in result

    def test_get_context_multiple_results(self, mock_database):
        """Should format multiple results with separators."""
        mock_database.search_knowledge_chunks.return_value = [
            {"filename": "doc1.txt", "content": "First content."},
            {"filename": "doc2.txt", "content": "Second content."}
        ]

        result = knowledge_service.get_relevant_context("project-id", "query")

        assert "[From: doc1.txt]" in result
        assert "First content." in result
        assert "[From: doc2.txt]" in result
        assert "Second content." in result
        assert "---" in result  # Separator

    def test_get_context_respects_max_chunks(self, mock_database):
        """Should respect max_chunks parameter."""
        mock_database.search_knowledge_chunks.return_value = [
            {"filename": f"doc{i}.txt", "content": f"Content {i}."}
            for i in range(10)
        ]

        result = knowledge_service.get_relevant_context(
            "project-id",
            "query",
            max_chunks=3
        )

        # Should only include 3 chunks
        assert result.count("[From:") == 3

    def test_get_context_respects_max_chars(self, mock_database):
        """Should respect max_chars parameter and truncate."""
        mock_database.search_knowledge_chunks.return_value = [
            {"filename": "doc.txt", "content": "A" * 3000}
        ]

        result = knowledge_service.get_relevant_context(
            "project-id",
            "query",
            max_chars=500
        )

        assert len(result) <= 550  # Allow some buffer for formatting
        assert "..." in result  # Should be truncated

    def test_get_context_skips_when_too_long(self, mock_database):
        """Should skip chunks that would exceed max_chars."""
        mock_database.search_knowledge_chunks.return_value = [
            {"filename": "doc1.txt", "content": "Short content."},
            {"filename": "doc2.txt", "content": "A" * 5000}  # Very long
        ]

        result = knowledge_service.get_relevant_context(
            "project-id",
            "query",
            max_chars=100
        )

        # Should only include the short one
        assert "Short content." in result

    def test_get_context_all_chunks_too_large(self, mock_database):
        """Should return empty string when all chunks are too large to fit."""
        # All chunks are too large to fit even as truncated versions
        mock_database.search_knowledge_chunks.return_value = [
            {"filename": "doc1.txt", "content": "A" * 5000},
            {"filename": "doc2.txt", "content": "B" * 5000}
        ]

        result = knowledge_service.get_relevant_context(
            "project-id",
            "query",
            max_chars=50  # Very small, can't fit truncated content
        )

        # Should return empty since nothing fits
        assert result == ""

    def test_get_context_missing_filename(self, mock_database):
        """Should handle missing filename in results."""
        mock_database.search_knowledge_chunks.return_value = [
            {"content": "Content without filename."}
        ]

        result = knowledge_service.get_relevant_context("project-id", "query")

        assert "[From: unknown]" in result
        assert "Content without filename." in result

    def test_get_context_calls_search_with_double_limit(self, mock_database):
        """Should search with double the max_chunks limit."""
        mock_database.search_knowledge_chunks.return_value = []

        knowledge_service.get_relevant_context(
            "project-id",
            "query",
            max_chunks=5
        )

        mock_database.search_knowledge_chunks.assert_called_once_with(
            "project-id", "query", limit=10
        )


class TestFormatContextForPrompt:
    """Test context formatting for prompt injection."""

    def test_format_empty_context(self):
        """Should return empty string for empty context."""
        assert knowledge_service.format_context_for_prompt("") == ""

    def test_format_none_context(self):
        """Should return empty string for None context."""
        assert knowledge_service.format_context_for_prompt(None) == ""

    def test_format_whitespace_context(self):
        """Should handle whitespace-only context."""
        # Whitespace is still truthy in Python, so it will be formatted
        result = knowledge_service.format_context_for_prompt("   ")
        assert "<knowledge-base>" in result

    def test_format_context_includes_wrapper_tags(self):
        """Should wrap context in knowledge-base tags."""
        context = "Some relevant content."
        result = knowledge_service.format_context_for_prompt(context)

        assert "<knowledge-base>" in result
        assert "</knowledge-base>" in result
        assert "Some relevant content." in result

    def test_format_context_includes_description(self):
        """Should include helpful description."""
        context = "Content here."
        result = knowledge_service.format_context_for_prompt(context)

        assert "knowledge base" in result.lower()
        assert "relevant" in result.lower()

    def test_format_preserves_content(self):
        """Should preserve the original context content."""
        context = "[From: doc.txt]\nLine 1\nLine 2\n\n---\n\n[From: doc2.txt]\nMore content"
        result = knowledge_service.format_context_for_prompt(context)

        assert context in result


class TestGetAllContextForProject:
    """Test retrieving all context for a project."""

    @pytest.fixture
    def mock_database(self):
        """Mock the database module."""
        with patch.object(knowledge_service, "database") as mock_db:
            yield mock_db

    def test_get_all_context_no_chunks(self, mock_database):
        """Should return empty string when no chunks."""
        mock_database.get_all_knowledge_chunks_for_project.return_value = []

        result = knowledge_service.get_all_context_for_project("project-id")

        assert result == ""

    def test_get_all_context_single_document(self, mock_database):
        """Should format single document correctly."""
        mock_database.get_all_knowledge_chunks_for_project.return_value = [
            {"filename": "doc.txt", "content": "Chunk 1"},
            {"filename": "doc.txt", "content": "Chunk 2"}
        ]

        result = knowledge_service.get_all_context_for_project("project-id")

        assert "[Document: doc.txt]" in result
        assert "Chunk 1" in result
        assert "Chunk 2" in result

    def test_get_all_context_multiple_documents(self, mock_database):
        """Should group chunks by document."""
        mock_database.get_all_knowledge_chunks_for_project.return_value = [
            {"filename": "doc1.txt", "content": "Doc1 Chunk"},
            {"filename": "doc2.txt", "content": "Doc2 Chunk"}
        ]

        result = knowledge_service.get_all_context_for_project("project-id")

        assert "[Document: doc1.txt]" in result
        assert "[Document: doc2.txt]" in result
        assert "Doc1 Chunk" in result
        assert "Doc2 Chunk" in result
        assert "---" in result  # Separator

    def test_get_all_context_respects_max_chars(self, mock_database):
        """Should respect max_chars and truncate."""
        mock_database.get_all_knowledge_chunks_for_project.return_value = [
            {"filename": "doc.txt", "content": "A" * 10000}
        ]

        result = knowledge_service.get_all_context_for_project(
            "project-id",
            max_chars=500
        )

        assert len(result) <= 600  # Allow buffer
        assert "..." in result

    def test_get_all_context_skips_documents_when_full(self, mock_database):
        """Should skip entire documents when max_chars would be exceeded."""
        mock_database.get_all_knowledge_chunks_for_project.return_value = [
            {"filename": "doc1.txt", "content": "Short"},
            {"filename": "doc2.txt", "content": "A" * 10000}
        ]

        result = knowledge_service.get_all_context_for_project(
            "project-id",
            max_chars=100
        )

        assert "[Document: doc1.txt]" in result
        assert "Short" in result

    def test_get_all_context_all_documents_too_large(self, mock_database):
        """Should return empty string when all documents are too large."""
        mock_database.get_all_knowledge_chunks_for_project.return_value = [
            {"filename": "doc1.txt", "content": "A" * 10000},
            {"filename": "doc2.txt", "content": "B" * 10000}
        ]

        result = knowledge_service.get_all_context_for_project(
            "project-id",
            max_chars=50  # Very small, even truncated won't fit meaningfully
        )

        # First document should get truncated and added
        # But if remaining is <= 200, it breaks before adding
        # This tests the edge case where max_chars is so small nothing fits
        assert result == ""

    def test_get_all_context_missing_filename(self, mock_database):
        """Should handle missing filename."""
        mock_database.get_all_knowledge_chunks_for_project.return_value = [
            {"content": "Content without filename"}
        ]

        result = knowledge_service.get_all_context_for_project("project-id")

        assert "[Document: unknown]" in result
        assert "Content without filename" in result

    def test_get_all_context_default_max_chars(self, mock_database):
        """Should use default max_chars of 8000."""
        mock_database.get_all_knowledge_chunks_for_project.return_value = [
            {"filename": "doc.txt", "content": "A" * 100}
        ]

        result = knowledge_service.get_all_context_for_project("project-id")

        # Should work without explicit max_chars
        assert "[Document: doc.txt]" in result


class TestDeleteDocumentAndChunks:
    """Test document and chunk deletion."""

    @pytest.fixture
    def mock_database(self):
        """Mock the database module."""
        with patch.object(knowledge_service, "database") as mock_db:
            yield mock_db

    def test_delete_existing_document(self, mock_database):
        """Should return True when document is deleted."""
        mock_database.delete_knowledge_document.return_value = True

        result = knowledge_service.delete_document_and_chunks("doc-id")

        assert result is True
        mock_database.delete_knowledge_document.assert_called_once_with("doc-id")

    def test_delete_nonexistent_document(self, mock_database):
        """Should return False when document doesn't exist."""
        mock_database.delete_knowledge_document.return_value = False

        result = knowledge_service.delete_document_and_chunks("nonexistent-id")

        assert result is False


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_chunk_with_only_whitespace_paragraphs(self):
        """Should handle content with whitespace-only paragraphs."""
        content = "Real content\n\n   \n\n   \n\nMore content"
        chunks = knowledge_service.chunk_document(content)

        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk["content"].strip() != ""

    def test_chunk_with_mixed_line_endings(self):
        """Should handle different line ending styles."""
        content = "Para 1\r\n\r\nPara 2\n\nPara 3\r\rPara 4"
        chunks = knowledge_service.chunk_document(content)

        assert len(chunks) >= 1

    def test_get_content_type_with_multiple_dots(self):
        """Should handle filenames with multiple dots."""
        assert knowledge_service.get_content_type("file.test.txt") == "text/plain"
        assert knowledge_service.get_content_type("archive.tar.gz") == "text/plain"  # .gz not supported

    def test_is_supported_with_dot_only(self):
        """Should handle edge case of dot-only filename."""
        assert knowledge_service.is_supported_file(".") is False
        assert knowledge_service.is_supported_file("..") is False

    def test_extract_text_with_bom(self):
        """Should handle UTF-8 BOM."""
        content_with_bom = b"\xef\xbb\xbfHello, world!"
        result = knowledge_service.extract_text_from_file(content_with_bom, "test.txt")
        # BOM should be preserved in decode but doesn't break anything
        assert "Hello, world!" in result


class TestSupportedTypes:
    """Test the SUPPORTED_TYPES constant."""

    def test_all_supported_types_are_strings(self):
        """All keys and values should be strings."""
        for ext, mime_type in knowledge_service.SUPPORTED_TYPES.items():
            assert isinstance(ext, str)
            assert isinstance(mime_type, str)
            assert ext.startswith(".")

    def test_common_text_types_supported(self):
        """Common text file types should be supported."""
        expected = [".txt", ".md", ".json", ".yaml", ".yml", ".py", ".js", ".html", ".css"]
        for ext in expected:
            assert ext in knowledge_service.SUPPORTED_TYPES


class TestDefaultChunkSettings:
    """Test default chunk size and overlap constants."""

    def test_default_chunk_size_is_reasonable(self):
        """Default chunk size should be a reasonable value."""
        assert knowledge_service.DEFAULT_CHUNK_SIZE > 100
        assert knowledge_service.DEFAULT_CHUNK_SIZE < 10000

    def test_default_overlap_is_less_than_chunk_size(self):
        """Overlap should be less than chunk size."""
        assert knowledge_service.DEFAULT_CHUNK_OVERLAP < knowledge_service.DEFAULT_CHUNK_SIZE

    def test_default_overlap_is_reasonable(self):
        """Default overlap should be a reasonable value."""
        assert knowledge_service.DEFAULT_CHUNK_OVERLAP >= 0
        assert knowledge_service.DEFAULT_CHUNK_OVERLAP < 500
