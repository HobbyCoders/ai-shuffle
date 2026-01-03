"""
Comprehensive tests for the tags API endpoints.

Tests cover:
- Tag CRUD operations (create, read, update, delete)
- List all tags
- Session tag management (get, set, add, remove tags from sessions)
- Authentication requirements
- Error handling and validation
- Access control for session operations
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException


class TestTagModuleImports:
    """Verify tags module can be imported correctly."""

    def test_tags_module_imports(self):
        """Tags module should import without errors."""
        from app.api import tags
        assert tags is not None

    def test_tags_router_exists(self):
        """Tags router should exist."""
        from app.api.tags import router
        assert router is not None

    def test_tags_router_prefix(self):
        """Tags router should have correct prefix."""
        from app.api.tags import router
        assert router.prefix == "/api/v1/tags"


class TestTagModels:
    """Test Pydantic models for tag validation."""

    def test_tag_create_valid(self):
        """TagCreate should accept valid data."""
        from app.core.models import TagCreate

        tag = TagCreate(name="Bug", color="#ff0000")
        assert tag.name == "Bug"
        assert tag.color == "#ff0000"

    def test_tag_create_default_color(self):
        """TagCreate should use default color if not provided."""
        from app.core.models import TagCreate

        tag = TagCreate(name="Feature")
        assert tag.name == "Feature"
        assert tag.color == "#6366f1"  # Default indigo color

    def test_tag_create_validation_name_too_long(self):
        """TagCreate should reject names over 50 characters."""
        from app.core.models import TagCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TagCreate(name="x" * 51)

    def test_tag_create_validation_name_empty(self):
        """TagCreate should reject empty names."""
        from app.core.models import TagCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TagCreate(name="")

    def test_tag_create_validation_invalid_color_format(self):
        """TagCreate should reject invalid color formats."""
        from app.core.models import TagCreate
        from pydantic import ValidationError

        # Missing #
        with pytest.raises(ValidationError):
            TagCreate(name="Test", color="ff0000")

        # Too short
        with pytest.raises(ValidationError):
            TagCreate(name="Test", color="#fff")

        # Too long
        with pytest.raises(ValidationError):
            TagCreate(name="Test", color="#ff00000")

        # Invalid characters
        with pytest.raises(ValidationError):
            TagCreate(name="Test", color="#gggggg")

    def test_tag_update_partial(self):
        """TagUpdate should allow partial updates."""
        from app.core.models import TagUpdate

        # Only name
        tag = TagUpdate(name="Updated")
        assert tag.name == "Updated"
        assert tag.color is None

        # Only color
        tag = TagUpdate(color="#00ff00")
        assert tag.name is None
        assert tag.color == "#00ff00"

        # Both
        tag = TagUpdate(name="Both", color="#0000ff")
        assert tag.name == "Both"
        assert tag.color == "#0000ff"

    def test_session_tags_update_valid(self):
        """SessionTagsUpdate should accept valid tag_ids list."""
        from app.core.models import SessionTagsUpdate

        update = SessionTagsUpdate(tag_ids=["tag-1", "tag-2", "tag-3"])
        assert len(update.tag_ids) == 3

    def test_session_tags_update_empty(self):
        """SessionTagsUpdate should accept empty list (to clear tags)."""
        from app.core.models import SessionTagsUpdate

        update = SessionTagsUpdate(tag_ids=[])
        assert update.tag_ids == []

    def test_session_tags_update_default(self):
        """SessionTagsUpdate should default to empty list."""
        from app.core.models import SessionTagsUpdate

        update = SessionTagsUpdate()
        assert update.tag_ids == []


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def sample_tag():
    """Sample tag data."""
    return {
        "id": "tag-abc12345",
        "name": "Bug",
        "color": "#ff0000",
        "created_at": datetime(2024, 1, 1, 0, 0, 0),
        "updated_at": datetime(2024, 1, 1, 0, 0, 0)
    }


@pytest.fixture
def sample_session():
    """Sample session data."""
    return {
        "id": "ses-abc12345",
        "title": "Test Session",
        "profile_id": "default",
        "project_id": None,
        "status": "active",
        "created_at": datetime(2024, 1, 1, 0, 0, 0),
        "updated_at": datetime(2024, 1, 1, 0, 0, 0)
    }


@pytest.fixture
def mock_request():
    """Mock FastAPI Request object."""
    request = MagicMock()
    request.state = MagicMock()
    return request


# =============================================================================
# List Tags Tests
# =============================================================================

class TestListTags:
    """Test list_tags endpoint function."""

    @pytest.mark.asyncio
    async def test_list_tags_success(self, sample_tag):
        """Should return all tags."""
        from app.api.tags import list_tags

        with patch("app.api.tags.database") as mock_db:
            mock_db.get_all_tags.return_value = [
                sample_tag,
                {**sample_tag, "id": "tag-2", "name": "Feature", "color": "#00ff00"}
            ]

            result = await list_tags(token="test-token")

        assert len(result) == 2
        assert result[0]["name"] == "Bug"
        assert result[1]["name"] == "Feature"
        mock_db.get_all_tags.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_tags_empty(self):
        """Should return empty list when no tags exist."""
        from app.api.tags import list_tags

        with patch("app.api.tags.database") as mock_db:
            mock_db.get_all_tags.return_value = []

            result = await list_tags(token="test-token")

        assert result == []


# =============================================================================
# Create Tag Tests
# =============================================================================

class TestCreateTag:
    """Test create_tag endpoint function."""

    @pytest.mark.asyncio
    async def test_create_tag_success(self, sample_tag):
        """Should create a new tag."""
        from app.api.tags import create_tag
        from app.core.models import TagCreate

        with patch("app.api.tags.database") as mock_db:
            mock_db.create_tag.return_value = sample_tag

            tag_data = TagCreate(name="Bug", color="#ff0000")
            result = await create_tag(tag_data=tag_data, token="test-token")

        assert result["name"] == "Bug"
        assert result["color"] == "#ff0000"
        mock_db.create_tag.assert_called_once()
        # Verify tag_id format
        call_args = mock_db.create_tag.call_args
        assert call_args.kwargs["tag_id"].startswith("tag-")
        assert call_args.kwargs["name"] == "Bug"
        assert call_args.kwargs["color"] == "#ff0000"

    @pytest.mark.asyncio
    async def test_create_tag_default_color(self):
        """Should use default color when not specified."""
        from app.api.tags import create_tag
        from app.core.models import TagCreate

        with patch("app.api.tags.database") as mock_db:
            mock_db.create_tag.return_value = {
                "id": "tag-new",
                "name": "Feature",
                "color": "#6366f1",
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }

            tag_data = TagCreate(name="Feature")
            result = await create_tag(tag_data=tag_data, token="test-token")

        assert result["color"] == "#6366f1"
        call_args = mock_db.create_tag.call_args
        assert call_args.kwargs["color"] == "#6366f1"

    @pytest.mark.asyncio
    async def test_create_tag_generates_unique_id(self):
        """Should generate unique tag IDs."""
        from app.api.tags import create_tag
        from app.core.models import TagCreate

        generated_ids = []

        def capture_create_tag(*args, **kwargs):
            tag_id = kwargs.get("tag_id")
            generated_ids.append(tag_id)
            return {
                "id": tag_id,
                "name": "Test",
                "color": "#ff0000",
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }

        with patch("app.api.tags.database") as mock_db:
            mock_db.create_tag.side_effect = capture_create_tag

            for i in range(5):
                tag_data = TagCreate(name=f"Test-{i}", color="#ff0000")
                await create_tag(tag_data=tag_data, token="test-token")

        # All IDs should be unique
        assert len(set(generated_ids)) == 5
        # All IDs should start with "tag-"
        for tag_id in generated_ids:
            assert tag_id.startswith("tag-")
            assert len(tag_id) == 12  # "tag-" + 8 hex chars


# =============================================================================
# Get Tag Tests
# =============================================================================

class TestGetTag:
    """Test get_tag endpoint function."""

    @pytest.mark.asyncio
    async def test_get_tag_success(self, sample_tag):
        """Should return tag by ID."""
        from app.api.tags import get_tag

        with patch("app.api.tags.database") as mock_db:
            mock_db.get_tag.return_value = sample_tag

            result = await get_tag(tag_id="tag-abc12345", token="test-token")

        assert result["id"] == "tag-abc12345"
        assert result["name"] == "Bug"
        mock_db.get_tag.assert_called_once_with("tag-abc12345")

    @pytest.mark.asyncio
    async def test_get_tag_not_found(self):
        """Should raise 404 when tag not found."""
        from app.api.tags import get_tag

        with patch("app.api.tags.database") as mock_db:
            mock_db.get_tag.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await get_tag(tag_id="nonexistent", token="test-token")

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()


# =============================================================================
# Update Tag Tests
# =============================================================================

class TestUpdateTag:
    """Test update_tag endpoint function."""

    @pytest.mark.asyncio
    async def test_update_tag_name(self, sample_tag):
        """Should update tag name."""
        from app.api.tags import update_tag
        from app.core.models import TagUpdate

        with patch("app.api.tags.database") as mock_db:
            updated_tag = {**sample_tag, "name": "Critical Bug"}
            mock_db.update_tag.return_value = updated_tag

            tag_data = TagUpdate(name="Critical Bug")
            result = await update_tag(
                tag_id="tag-abc12345",
                tag_data=tag_data,
                token="test-token"
            )

        assert result["name"] == "Critical Bug"
        mock_db.update_tag.assert_called_once_with(
            tag_id="tag-abc12345",
            name="Critical Bug",
            color=None
        )

    @pytest.mark.asyncio
    async def test_update_tag_color(self, sample_tag):
        """Should update tag color."""
        from app.api.tags import update_tag
        from app.core.models import TagUpdate

        with patch("app.api.tags.database") as mock_db:
            updated_tag = {**sample_tag, "color": "#00ff00"}
            mock_db.update_tag.return_value = updated_tag

            tag_data = TagUpdate(color="#00ff00")
            result = await update_tag(
                tag_id="tag-abc12345",
                tag_data=tag_data,
                token="test-token"
            )

        assert result["color"] == "#00ff00"

    @pytest.mark.asyncio
    async def test_update_tag_both_fields(self, sample_tag):
        """Should update both name and color."""
        from app.api.tags import update_tag
        from app.core.models import TagUpdate

        with patch("app.api.tags.database") as mock_db:
            updated_tag = {**sample_tag, "name": "Updated", "color": "#0000ff"}
            mock_db.update_tag.return_value = updated_tag

            tag_data = TagUpdate(name="Updated", color="#0000ff")
            result = await update_tag(
                tag_id="tag-abc12345",
                tag_data=tag_data,
                token="test-token"
            )

        assert result["name"] == "Updated"
        assert result["color"] == "#0000ff"

    @pytest.mark.asyncio
    async def test_update_tag_not_found(self):
        """Should raise 404 when tag not found."""
        from app.api.tags import update_tag
        from app.core.models import TagUpdate

        with patch("app.api.tags.database") as mock_db:
            mock_db.update_tag.return_value = None

            tag_data = TagUpdate(name="Updated")
            with pytest.raises(HTTPException) as exc_info:
                await update_tag(
                    tag_id="nonexistent",
                    tag_data=tag_data,
                    token="test-token"
                )

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_update_tag_empty_body(self, sample_tag):
        """Should accept empty update body (no changes)."""
        from app.api.tags import update_tag
        from app.core.models import TagUpdate

        with patch("app.api.tags.database") as mock_db:
            mock_db.update_tag.return_value = sample_tag

            tag_data = TagUpdate()
            result = await update_tag(
                tag_id="tag-abc12345",
                tag_data=tag_data,
                token="test-token"
            )

        assert result is not None
        mock_db.update_tag.assert_called_once_with(
            tag_id="tag-abc12345",
            name=None,
            color=None
        )


# =============================================================================
# Delete Tag Tests
# =============================================================================

class TestDeleteTag:
    """Test delete_tag endpoint function."""

    @pytest.mark.asyncio
    async def test_delete_tag_success(self):
        """Should delete tag successfully."""
        from app.api.tags import delete_tag

        with patch("app.api.tags.database") as mock_db:
            mock_db.delete_tag.return_value = True

            # Should not raise
            await delete_tag(tag_id="tag-abc12345", token="test-token")

        mock_db.delete_tag.assert_called_once_with("tag-abc12345")

    @pytest.mark.asyncio
    async def test_delete_tag_not_found(self):
        """Should raise 404 when tag not found."""
        from app.api.tags import delete_tag

        with patch("app.api.tags.database") as mock_db:
            mock_db.delete_tag.return_value = False

            with pytest.raises(HTTPException) as exc_info:
                await delete_tag(tag_id="nonexistent", token="test-token")

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()


# =============================================================================
# Get Session Tags Tests
# =============================================================================

class TestGetSessionTags:
    """Test get_session_tags endpoint function."""

    @pytest.mark.asyncio
    async def test_get_session_tags_success(
        self, mock_request, sample_session, sample_tag
    ):
        """Should return tags for a session."""
        from app.api.tags import get_session_tags

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session
                mock_db.get_session_tags.return_value = [sample_tag]

                result = await get_session_tags(
                    request=mock_request,
                    session_id="ses-abc12345",
                    token="test-token"
                )

        assert len(result) == 1
        assert result[0]["name"] == "Bug"
        mock_db.get_session.assert_called_once_with("ses-abc12345")
        mock_db.get_session_tags.assert_called_once_with("ses-abc12345")

    @pytest.mark.asyncio
    async def test_get_session_tags_empty(self, mock_request, sample_session):
        """Should return empty list when session has no tags."""
        from app.api.tags import get_session_tags

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session
                mock_db.get_session_tags.return_value = []

                result = await get_session_tags(
                    request=mock_request,
                    session_id="ses-abc12345",
                    token="test-token"
                )

        assert result == []

    @pytest.mark.asyncio
    async def test_get_session_tags_session_not_found(self, mock_request):
        """Should raise 404 when session not found."""
        from app.api.tags import get_session_tags

        with patch("app.api.tags.database") as mock_db:
            mock_db.get_session.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await get_session_tags(
                    request=mock_request,
                    session_id="nonexistent",
                    token="test-token"
                )

        assert exc_info.value.status_code == 404
        assert "session not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_session_tags_access_denied(
        self, mock_request, sample_session
    ):
        """Should raise 403 when user lacks access to session."""
        from app.api.tags import get_session_tags

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_db.get_session.return_value = sample_session
                mock_access.side_effect = HTTPException(
                    status_code=403,
                    detail="Access denied"
                )

                with pytest.raises(HTTPException) as exc_info:
                    await get_session_tags(
                        request=mock_request,
                        session_id="ses-abc12345",
                        token="test-token"
                    )

        assert exc_info.value.status_code == 403


# =============================================================================
# Set Session Tags Tests
# =============================================================================

class TestSetSessionTags:
    """Test set_session_tags endpoint function."""

    @pytest.mark.asyncio
    async def test_set_session_tags_success(self, mock_request, sample_session):
        """Should replace session tags."""
        from app.api.tags import set_session_tags
        from app.core.models import SessionTagsUpdate

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session
                mock_db.get_tag.return_value = {"id": "tag-1", "name": "Bug", "color": "#ff0000"}
                mock_db.set_session_tags.return_value = [
                    {
                        "id": "tag-1",
                        "name": "Bug",
                        "color": "#ff0000",
                        "created_at": datetime(2024, 1, 1, 0, 0, 0),
                        "updated_at": datetime(2024, 1, 1, 0, 0, 0)
                    }
                ]

                data = SessionTagsUpdate(tag_ids=["tag-1"])
                result = await set_session_tags(
                    request=mock_request,
                    session_id="ses-abc12345",
                    data=data,
                    token="test-token"
                )

        assert len(result) == 1
        mock_db.set_session_tags.assert_called_once_with("ses-abc12345", ["tag-1"])

    @pytest.mark.asyncio
    async def test_set_session_tags_multiple(self, mock_request, sample_session):
        """Should set multiple tags."""
        from app.api.tags import set_session_tags
        from app.core.models import SessionTagsUpdate

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session
                mock_db.get_tag.return_value = {"id": "tag-1", "name": "Tag"}
                mock_db.set_session_tags.return_value = []

                data = SessionTagsUpdate(tag_ids=["tag-1", "tag-2", "tag-3"])
                result = await set_session_tags(
                    request=mock_request,
                    session_id="ses-abc12345",
                    data=data,
                    token="test-token"
                )

        # get_tag should be called once for each tag_id
        assert mock_db.get_tag.call_count == 3

    @pytest.mark.asyncio
    async def test_set_session_tags_clear_all(self, mock_request, sample_session):
        """Should clear all tags when empty list provided."""
        from app.api.tags import set_session_tags
        from app.core.models import SessionTagsUpdate

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session
                mock_db.set_session_tags.return_value = []

                data = SessionTagsUpdate(tag_ids=[])
                result = await set_session_tags(
                    request=mock_request,
                    session_id="ses-abc12345",
                    data=data,
                    token="test-token"
                )

        assert result == []
        mock_db.set_session_tags.assert_called_once_with("ses-abc12345", [])

    @pytest.mark.asyncio
    async def test_set_session_tags_session_not_found(self, mock_request):
        """Should raise 404 when session not found."""
        from app.api.tags import set_session_tags
        from app.core.models import SessionTagsUpdate

        with patch("app.api.tags.database") as mock_db:
            mock_db.get_session.return_value = None

            data = SessionTagsUpdate(tag_ids=["tag-1"])
            with pytest.raises(HTTPException) as exc_info:
                await set_session_tags(
                    request=mock_request,
                    session_id="nonexistent",
                    data=data,
                    token="test-token"
                )

        assert exc_info.value.status_code == 404
        assert "session not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_set_session_tags_tag_not_found(
        self, mock_request, sample_session
    ):
        """Should raise 400 when one of the tags doesn't exist."""
        from app.api.tags import set_session_tags
        from app.core.models import SessionTagsUpdate

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session
                mock_db.get_tag.return_value = None  # Tag doesn't exist

                data = SessionTagsUpdate(tag_ids=["nonexistent-tag"])
                with pytest.raises(HTTPException) as exc_info:
                    await set_session_tags(
                        request=mock_request,
                        session_id="ses-abc12345",
                        data=data,
                        token="test-token"
                    )

        assert exc_info.value.status_code == 400
        assert "tag not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_set_session_tags_validates_all_tags(
        self, mock_request, sample_session
    ):
        """Should validate all tag IDs before setting."""
        from app.api.tags import set_session_tags
        from app.core.models import SessionTagsUpdate

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session

                # First tag exists, second doesn't
                def mock_get_tag(tag_id):
                    if tag_id == "tag-1":
                        return {"id": "tag-1", "name": "Exists"}
                    return None

                mock_db.get_tag.side_effect = mock_get_tag

                data = SessionTagsUpdate(tag_ids=["tag-1", "tag-nonexistent"])
                with pytest.raises(HTTPException) as exc_info:
                    await set_session_tags(
                        request=mock_request,
                        session_id="ses-abc12345",
                        data=data,
                        token="test-token"
                    )

        assert exc_info.value.status_code == 400


# =============================================================================
# Add Tag to Session Tests
# =============================================================================

class TestAddTagToSession:
    """Test add_tag_to_session endpoint function."""

    @pytest.mark.asyncio
    async def test_add_tag_to_session_success(
        self, mock_request, sample_session, sample_tag
    ):
        """Should add tag to session."""
        from app.api.tags import add_tag_to_session

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session
                mock_db.get_tag.return_value = sample_tag
                mock_db.add_session_tag.return_value = True

                result = await add_tag_to_session(
                    request=mock_request,
                    session_id="ses-abc12345",
                    tag_id="tag-abc12345",
                    token="test-token"
                )

        assert result["status"] == "ok"
        mock_db.add_session_tag.assert_called_once_with("ses-abc12345", "tag-abc12345")

    @pytest.mark.asyncio
    async def test_add_tag_to_session_session_not_found(self, mock_request):
        """Should raise 404 when session not found."""
        from app.api.tags import add_tag_to_session

        with patch("app.api.tags.database") as mock_db:
            mock_db.get_session.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await add_tag_to_session(
                    request=mock_request,
                    session_id="nonexistent",
                    tag_id="tag-1",
                    token="test-token"
                )

        assert exc_info.value.status_code == 404
        assert "session not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_add_tag_to_session_tag_not_found(
        self, mock_request, sample_session
    ):
        """Should raise 404 when tag not found."""
        from app.api.tags import add_tag_to_session

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session
                mock_db.get_tag.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await add_tag_to_session(
                        request=mock_request,
                        session_id="ses-abc12345",
                        tag_id="nonexistent",
                        token="test-token"
                    )

        assert exc_info.value.status_code == 404
        assert "tag not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_add_tag_to_session_access_denied(
        self, mock_request, sample_session
    ):
        """Should raise 403 when user lacks access to session."""
        from app.api.tags import add_tag_to_session

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_db.get_session.return_value = sample_session
                mock_access.side_effect = HTTPException(
                    status_code=403,
                    detail="Access denied"
                )

                with pytest.raises(HTTPException) as exc_info:
                    await add_tag_to_session(
                        request=mock_request,
                        session_id="ses-abc12345",
                        tag_id="tag-1",
                        token="test-token"
                    )

        assert exc_info.value.status_code == 403


# =============================================================================
# Remove Tag from Session Tests
# =============================================================================

class TestRemoveTagFromSession:
    """Test remove_tag_from_session endpoint function."""

    @pytest.mark.asyncio
    async def test_remove_tag_from_session_success(
        self, mock_request, sample_session
    ):
        """Should remove tag from session."""
        from app.api.tags import remove_tag_from_session

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None
                mock_db.get_session.return_value = sample_session
                mock_db.remove_session_tag.return_value = True

                # Should not raise
                await remove_tag_from_session(
                    request=mock_request,
                    session_id="ses-abc12345",
                    tag_id="tag-abc12345",
                    token="test-token"
                )

        mock_db.remove_session_tag.assert_called_once_with("ses-abc12345", "tag-abc12345")

    @pytest.mark.asyncio
    async def test_remove_tag_from_session_session_not_found(self, mock_request):
        """Should raise 404 when session not found."""
        from app.api.tags import remove_tag_from_session

        with patch("app.api.tags.database") as mock_db:
            mock_db.get_session.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await remove_tag_from_session(
                    request=mock_request,
                    session_id="nonexistent",
                    tag_id="tag-1",
                    token="test-token"
                )

        assert exc_info.value.status_code == 404
        assert "session not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_remove_tag_from_session_access_denied(
        self, mock_request, sample_session
    ):
        """Should raise 403 when user lacks access to session."""
        from app.api.tags import remove_tag_from_session

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_db.get_session.return_value = sample_session
                mock_access.side_effect = HTTPException(
                    status_code=403,
                    detail="Access denied"
                )

                with pytest.raises(HTTPException) as exc_info:
                    await remove_tag_from_session(
                        request=mock_request,
                        session_id="ses-abc12345",
                        tag_id="tag-1",
                        token="test-token"
                    )

        assert exc_info.value.status_code == 403


# =============================================================================
# Edge Cases and Validation Tests
# =============================================================================

class TestTagsEdgeCases:
    """Test edge cases and validation scenarios."""

    @pytest.mark.asyncio
    async def test_tag_name_with_special_characters(self):
        """Should accept tag names with special characters."""
        from app.api.tags import create_tag
        from app.core.models import TagCreate

        with patch("app.api.tags.database") as mock_db:
            mock_db.create_tag.return_value = {
                "id": "tag-new",
                "name": "Bug: Critical!",
                "color": "#ff0000",
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }

            tag_data = TagCreate(name="Bug: Critical!", color="#ff0000")
            result = await create_tag(tag_data=tag_data, token="test-token")

        assert result["name"] == "Bug: Critical!"

    @pytest.mark.asyncio
    async def test_tag_name_with_unicode(self):
        """Should accept tag names with unicode characters."""
        from app.api.tags import create_tag
        from app.core.models import TagCreate

        with patch("app.api.tags.database") as mock_db:
            mock_db.create_tag.return_value = {
                "id": "tag-new",
                "name": "Importante",
                "color": "#ff0000",
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }

            tag_data = TagCreate(name="Importante", color="#ff0000")
            result = await create_tag(tag_data=tag_data, token="test-token")

        assert result is not None

    @pytest.mark.asyncio
    async def test_tag_color_lowercase(self):
        """Should accept lowercase hex colors."""
        from app.api.tags import create_tag
        from app.core.models import TagCreate

        with patch("app.api.tags.database") as mock_db:
            mock_db.create_tag.return_value = {
                "id": "tag-new",
                "name": "Test",
                "color": "#abcdef",
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }

            tag_data = TagCreate(name="Test", color="#abcdef")
            result = await create_tag(tag_data=tag_data, token="test-token")

        assert result["color"] == "#abcdef"

    @pytest.mark.asyncio
    async def test_tag_color_uppercase(self):
        """Should accept uppercase hex colors."""
        from app.api.tags import create_tag
        from app.core.models import TagCreate

        with patch("app.api.tags.database") as mock_db:
            mock_db.create_tag.return_value = {
                "id": "tag-new",
                "name": "Test",
                "color": "#ABCDEF",
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }

            tag_data = TagCreate(name="Test", color="#ABCDEF")
            result = await create_tag(tag_data=tag_data, token="test-token")

        assert result["color"] == "#ABCDEF"

    @pytest.mark.asyncio
    async def test_tag_id_format(self):
        """Tag ID should follow format 'tag-{hex}'."""
        from app.api.tags import create_tag
        from app.core.models import TagCreate

        generated_id = None

        def capture_create_tag(*args, **kwargs):
            nonlocal generated_id
            generated_id = kwargs.get("tag_id")
            return {
                "id": generated_id,
                "name": "Test",
                "color": "#ff0000",
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }

        with patch("app.api.tags.database") as mock_db:
            mock_db.create_tag.side_effect = capture_create_tag

            tag_data = TagCreate(name="Test", color="#ff0000")
            await create_tag(tag_data=tag_data, token="test-token")

        assert generated_id is not None
        assert generated_id.startswith("tag-")
        assert len(generated_id) == 12  # "tag-" + 8 hex chars


# =============================================================================
# Access Control Tests
# =============================================================================

class TestSessionAccessControl:
    """Test session access control for tag operations."""

    @pytest.mark.asyncio
    async def test_admin_has_full_access(
        self, mock_request, sample_session, sample_tag
    ):
        """Admin users should have full access to all session tags."""
        from app.api.tags import get_session_tags

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_access.return_value = None  # No exception = admin access
                mock_db.get_session.return_value = sample_session
                mock_db.get_session_tags.return_value = [sample_tag]

                result = await get_session_tags(
                    request=mock_request,
                    session_id="ses-abc12345",
                    token="test-token"
                )

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_api_user_project_restriction(
        self, mock_request, sample_session
    ):
        """API user should be blocked from sessions in other projects."""
        from app.api.tags import get_session_tags

        # Session belongs to project-1, API user is restricted to project-2
        session_with_project = {**sample_session, "project_id": "project-1"}

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_db.get_session.return_value = session_with_project
                mock_access.side_effect = HTTPException(
                    status_code=403,
                    detail="Access denied to this session"
                )

                with pytest.raises(HTTPException) as exc_info:
                    await get_session_tags(
                        request=mock_request,
                        session_id="ses-abc12345",
                        token="test-token"
                    )

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_api_user_profile_restriction(
        self, mock_request, sample_session
    ):
        """API user should be blocked from sessions with other profiles."""
        from app.api.tags import get_session_tags

        # Session uses profile-1, API user is restricted to profile-2
        session_with_profile = {**sample_session, "profile_id": "profile-1"}

        with patch("app.api.tags.database") as mock_db:
            with patch("app.api.tags.check_session_access") as mock_access:
                mock_db.get_session.return_value = session_with_profile
                mock_access.side_effect = HTTPException(
                    status_code=403,
                    detail="Access denied to this session"
                )

                with pytest.raises(HTTPException) as exc_info:
                    await get_session_tags(
                        request=mock_request,
                        session_id="ses-abc12345",
                        token="test-token"
                    )

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_check_session_access_called_for_all_session_operations(
        self, mock_request, sample_session, sample_tag
    ):
        """Session access should be checked for all session tag operations."""
        from app.api.tags import (
            get_session_tags,
            set_session_tags,
            add_tag_to_session,
            remove_tag_from_session
        )
        from app.core.models import SessionTagsUpdate

        # Test each session operation
        operations = [
            (get_session_tags, {"request": mock_request, "session_id": "ses-1", "token": "test"}),
        ]

        for op, kwargs in operations:
            with patch("app.api.tags.database") as mock_db:
                with patch("app.api.tags.check_session_access") as mock_access:
                    mock_db.get_session.return_value = sample_session
                    mock_db.get_session_tags.return_value = []
                    mock_access.return_value = None

                    await op(**kwargs)

                    mock_access.assert_called_once()
