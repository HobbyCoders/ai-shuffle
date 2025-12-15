"""
Knowledge Base Service

Handles document processing, chunking, and context retrieval for RAG.
"""

import logging
import re
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from app.db import database

logger = logging.getLogger(__name__)

# Supported file types and their MIME types
SUPPORTED_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".py": "text/x-python",
    ".js": "application/javascript",
    ".ts": "application/typescript",
    ".html": "text/html",
    ".css": "text/css",
    ".xml": "application/xml",
    ".csv": "text/csv",
    ".log": "text/plain",
}

# Default chunk settings
DEFAULT_CHUNK_SIZE = 800  # characters per chunk
DEFAULT_CHUNK_OVERLAP = 100  # overlap between chunks


def get_content_type(filename: str) -> str:
    """Get MIME type based on file extension"""
    ext = Path(filename).suffix.lower()
    return SUPPORTED_TYPES.get(ext, "text/plain")


def is_supported_file(filename: str) -> bool:
    """Check if file type is supported for knowledge base"""
    ext = Path(filename).suffix.lower()
    return ext in SUPPORTED_TYPES


def extract_text_from_file(content: bytes, filename: str) -> str:
    """
    Extract text content from uploaded file.

    Currently supports text-based files. For future enhancement,
    this can be extended to support PDF, DOCX, etc.
    """
    # Try UTF-8 first, then fall back to latin-1
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except UnicodeDecodeError:
            logger.warning(f"Could not decode file {filename}, using lossy decode")
            text = content.decode("utf-8", errors="replace")

    return text


def chunk_document(
    content: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
) -> List[Dict[str, Any]]:
    """
    Split document content into chunks for retrieval.

    Uses paragraph-aware splitting to maintain context.

    Args:
        content: The full document text
        chunk_size: Target size for each chunk in characters
        chunk_overlap: Number of characters to overlap between chunks

    Returns:
        List of chunk dicts with 'content' and 'metadata' keys
    """
    if not content or not content.strip():
        return []

    # Split by paragraphs first (double newlines)
    paragraphs = re.split(r'\n\s*\n', content)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks = []
    current_chunk = ""
    current_start = 0
    char_position = 0

    for para in paragraphs:
        # If adding this paragraph would exceed chunk size
        if current_chunk and len(current_chunk) + len(para) + 2 > chunk_size:
            # Save current chunk
            chunks.append({
                "content": current_chunk.strip(),
                "metadata": {
                    "start_char": current_start,
                    "end_char": char_position
                }
            })

            # Start new chunk with overlap
            if chunk_overlap > 0 and len(current_chunk) > chunk_overlap:
                # Find a good break point for overlap
                overlap_text = current_chunk[-chunk_overlap:]
                # Try to break at word boundary
                space_pos = overlap_text.find(" ")
                if space_pos > 0:
                    overlap_text = overlap_text[space_pos + 1:]
                current_chunk = overlap_text + "\n\n" + para
                current_start = char_position - len(overlap_text)
            else:
                current_chunk = para
                current_start = char_position
        else:
            # Add to current chunk
            if current_chunk:
                current_chunk += "\n\n" + para
            else:
                current_chunk = para
                current_start = char_position

        char_position += len(para) + 2  # +2 for paragraph separator

    # Don't forget the last chunk
    if current_chunk.strip():
        chunks.append({
            "content": current_chunk.strip(),
            "metadata": {
                "start_char": current_start,
                "end_char": char_position
            }
        })

    return chunks


async def process_document(
    project_id: str,
    filename: str,
    content: bytes
) -> Tuple[str, int]:
    """
    Process an uploaded document for the knowledge base.

    Args:
        project_id: The project to add the document to
        filename: Original filename
        content: Raw file content

    Returns:
        Tuple of (document_id, chunk_count)
    """
    # Extract text
    text_content = extract_text_from_file(content, filename)

    if not text_content.strip():
        raise ValueError("Document is empty or could not be parsed")

    # Generate document ID
    document_id = str(uuid.uuid4())
    content_type = get_content_type(filename)

    # Chunk the document
    chunks = chunk_document(text_content)

    # Save document to database
    database.create_knowledge_document(
        document_id=document_id,
        project_id=project_id,
        filename=filename,
        content=text_content,
        content_type=content_type,
        file_size=len(content),
        chunk_count=len(chunks)
    )

    # Save chunks
    for idx, chunk in enumerate(chunks):
        chunk_id = str(uuid.uuid4())
        database.create_knowledge_chunk(
            chunk_id=chunk_id,
            document_id=document_id,
            chunk_index=idx,
            content=chunk["content"],
            metadata=chunk["metadata"]
        )

    logger.info(f"Processed document {filename}: {len(chunks)} chunks, {len(content)} bytes")

    return document_id, len(chunks)


def get_relevant_context(
    project_id: str,
    query: str,
    max_chunks: int = 5,
    max_chars: int = 4000
) -> str:
    """
    Retrieve relevant knowledge chunks for a query.

    Uses simple keyword matching. For future enhancement,
    this can be replaced with vector similarity search.

    Args:
        project_id: The project to search
        query: The user's query/message
        max_chunks: Maximum number of chunks to return
        max_chars: Maximum total characters to return

    Returns:
        Formatted context string, or empty string if no relevant content
    """
    if not query or not project_id:
        return ""

    # Search for relevant chunks
    results = database.search_knowledge_chunks(project_id, query, limit=max_chunks * 2)

    if not results:
        return ""

    # Build context string, respecting max_chars
    context_parts = []
    total_chars = 0
    chunks_used = 0

    for result in results:
        if chunks_used >= max_chunks:
            break

        chunk_text = f"[From: {result.get('filename', 'unknown')}]\n{result['content']}"

        if total_chars + len(chunk_text) > max_chars:
            # Try to fit a truncated version
            remaining = max_chars - total_chars - 50  # Leave room for ellipsis
            if remaining > 200:  # Only include if meaningful
                chunk_text = chunk_text[:remaining] + "..."
            else:
                break

        context_parts.append(chunk_text)
        total_chars += len(chunk_text)
        chunks_used += 1

    if not context_parts:
        return ""

    return "\n\n---\n\n".join(context_parts)


def format_context_for_prompt(context: str) -> str:
    """
    Format retrieved context for injection into the system prompt or message.

    Args:
        context: The raw context string from get_relevant_context

    Returns:
        Formatted context with header
    """
    if not context:
        return ""

    return f"""<knowledge-base>
The following information is from the project's knowledge base and may be relevant to the user's request:

{context}
</knowledge-base>
"""


def get_all_context_for_project(project_id: str, max_chars: int = 8000) -> str:
    """
    Get all knowledge base content for a project (for small knowledge bases).

    This is useful when the knowledge base is small enough to include entirely.

    Args:
        project_id: The project ID
        max_chars: Maximum characters to return

    Returns:
        Formatted context string with all knowledge base content
    """
    chunks = database.get_all_knowledge_chunks_for_project(project_id)

    if not chunks:
        return ""

    # Group by document
    docs: Dict[str, List[str]] = {}
    for chunk in chunks:
        filename = chunk.get("filename", "unknown")
        if filename not in docs:
            docs[filename] = []
        docs[filename].append(chunk["content"])

    # Build context
    parts = []
    total_chars = 0

    for filename, contents in docs.items():
        doc_content = f"[Document: {filename}]\n" + "\n\n".join(contents)

        if total_chars + len(doc_content) > max_chars:
            remaining = max_chars - total_chars - 50
            if remaining > 200:
                doc_content = doc_content[:remaining] + "..."
                parts.append(doc_content)
            break

        parts.append(doc_content)
        total_chars += len(doc_content)

    if not parts:
        return ""

    return "\n\n---\n\n".join(parts)


def delete_document_and_chunks(document_id: str) -> bool:
    """
    Delete a document and all its chunks.

    Args:
        document_id: The document ID to delete

    Returns:
        True if deleted, False if not found
    """
    return database.delete_knowledge_document(document_id)
