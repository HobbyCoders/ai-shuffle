"""
Comprehensive unit tests for app/api/templates.py

Tests all template management endpoints:
- GET /api/v1/templates (list)
- GET /api/v1/templates/categories (list categories)
- POST /api/v1/templates (create)
- GET /api/v1/templates/{template_id} (get single)
- PATCH /api/v1/templates/{template_id} (update)
- DELETE /api/v1/templates/{template_id} (delete)
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from fastapi import HTTPException


# =============================================================================
# Module Import Tests
# =============================================================================

class TestTemplateModuleImports:
    """Verify templates module can be imported correctly."""

    def test_templates_module_imports(self):
        """Templates module should import without errors."""
        from app.api import templates
        assert templates is not None

    def test_templates_router_exists(self):
        """Templates router should exist."""
        from app.api.templates import router
        assert router is not None

    def test_templates_router_prefix(self):
        """Templates router should have correct prefix."""
        from app.api.templates import router
        assert router.prefix == "/api/v1/templates"


# =============================================================================
# Model Validation Tests
# =============================================================================

class TestTemplateModels:
    """Test Pydantic models for template validation."""

    def test_template_create_valid(self):
        """TemplateCreate should accept valid data."""
        from app.core.models import TemplateCreate

        template = TemplateCreate(
            name="Test Template",
            prompt="Test prompt",
            description="A description",
            icon="code",
            category="coding"
        )
        assert template.name == "Test Template"
        assert template.prompt == "Test prompt"

    def test_template_create_minimal(self):
        """TemplateCreate should accept minimal required fields."""
        from app.core.models import TemplateCreate

        template = TemplateCreate(name="Minimal", prompt="Minimal prompt")
        assert template.name == "Minimal"
        assert template.prompt == "Minimal prompt"
        assert template.description is None
        assert template.profile_id is None
        assert template.icon is None
        assert template.category is None

    def test_template_create_validation_name_empty(self):
        """TemplateCreate should reject empty names."""
        from app.core.models import TemplateCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TemplateCreate(name="", prompt="Test prompt")

    def test_template_create_validation_prompt_empty(self):
        """TemplateCreate should reject empty prompts."""
        from app.core.models import TemplateCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TemplateCreate(name="Test", prompt="")

    def test_template_create_validation_name_too_long(self):
        """TemplateCreate should reject names over 100 characters."""
        from app.core.models import TemplateCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TemplateCreate(name="x" * 101, prompt="Test prompt")

    def test_template_update_partial(self):
        """TemplateUpdate should allow partial updates."""
        from app.core.models import TemplateUpdate

        # Only name
        update = TemplateUpdate(name="Updated Name")
        assert update.name == "Updated Name"
        assert update.prompt is None
        assert update.description is None

        # Only prompt
        update = TemplateUpdate(prompt="Updated prompt")
        assert update.name is None
        assert update.prompt == "Updated prompt"

        # Multiple fields
        update = TemplateUpdate(name="New", description="Desc", prompt="Prompt")
        assert update.name == "New"
        assert update.description == "Desc"
        assert update.prompt == "Prompt"

    def test_template_update_empty_valid(self):
        """TemplateUpdate should accept empty payload (no-op update)."""
        from app.core.models import TemplateUpdate

        update = TemplateUpdate()
        assert update.name is None
        assert update.description is None
        assert update.prompt is None
        assert update.profile_id is None
        assert update.icon is None
        assert update.category is None

    def test_template_model_full(self):
        """Template response model should accept complete data."""
        from app.core.models import Template

        now = datetime.utcnow()
        template = Template(
            id="tmpl-test123",
            name="Full Template",
            description="Description",
            prompt="Test prompt",
            profile_id="profile-1",
            icon="star",
            category="general",
            is_builtin=False,
            created_at=now,
            updated_at=now
        )
        assert template.id == "tmpl-test123"
        assert template.is_builtin is False


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def sample_template():
    """Sample template data."""
    return {
        "id": "tmpl-test1234",
        "name": "Test Template",
        "description": "A test template description",
        "prompt": "Test prompt content",
        "profile_id": None,
        "icon": "code",
        "category": "coding",
        "is_builtin": False,
        "created_at": datetime(2024, 1, 1, 0, 0, 0),
        "updated_at": datetime(2024, 1, 1, 0, 0, 0)
    }


@pytest.fixture
def sample_builtin_template():
    """Sample builtin template data."""
    return {
        "id": "tmpl-builtin1",
        "name": "Builtin Template",
        "description": "A builtin template",
        "prompt": "Builtin prompt content",
        "profile_id": None,
        "icon": "star",
        "category": "general",
        "is_builtin": True,
        "created_at": datetime(2024, 1, 1, 0, 0, 0),
        "updated_at": datetime(2024, 1, 1, 0, 0, 0)
    }


@pytest.fixture
def sample_profile():
    """Sample profile data."""
    return {
        "id": "test-profile-id",
        "name": "Test Profile",
        "description": "A test profile",
        "config": {},
        "is_builtin": False,
        "created_at": datetime(2024, 1, 1, 0, 0, 0),
        "updated_at": datetime(2024, 1, 1, 0, 0, 0)
    }


# =============================================================================
# GET /api/v1/templates - List Templates
# =============================================================================

class TestListTemplates:
    """Tests for listing templates."""

    @pytest.mark.asyncio
    async def test_list_templates_success(self, sample_template):
        """Test successfully listing all templates."""
        from app.api.templates import list_templates

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_all_templates.return_value = [sample_template]

            result = await list_templates(profile_id=None, category=None, token="test-token")

        assert len(result) == 1
        assert result[0]["id"] == sample_template["id"]
        assert result[0]["name"] == sample_template["name"]
        mock_database.get_all_templates.assert_called_once_with(profile_id=None, category=None)

    @pytest.mark.asyncio
    async def test_list_templates_empty(self):
        """Test listing templates when none exist."""
        from app.api.templates import list_templates

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_all_templates.return_value = []

            result = await list_templates(profile_id=None, category=None, token="test-token")

        assert result == []

    @pytest.mark.asyncio
    async def test_list_templates_filter_by_profile(self, sample_template):
        """Test listing templates filtered by profile ID."""
        from app.api.templates import list_templates

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_all_templates.return_value = [sample_template]

            result = await list_templates(profile_id="test-profile", category=None, token="test-token")

        mock_database.get_all_templates.assert_called_once_with(profile_id="test-profile", category=None)

    @pytest.mark.asyncio
    async def test_list_templates_filter_by_category(self, sample_template):
        """Test listing templates filtered by category."""
        from app.api.templates import list_templates

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_all_templates.return_value = [sample_template]

            result = await list_templates(profile_id=None, category="coding", token="test-token")

        mock_database.get_all_templates.assert_called_once_with(profile_id=None, category="coding")

    @pytest.mark.asyncio
    async def test_list_templates_filter_combined(self, sample_template):
        """Test listing templates with both profile and category filters."""
        from app.api.templates import list_templates

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_all_templates.return_value = [sample_template]

            result = await list_templates(profile_id="test-profile", category="coding", token="test-token")

        mock_database.get_all_templates.assert_called_once_with(profile_id="test-profile", category="coding")

    @pytest.mark.asyncio
    async def test_list_templates_multiple(self, sample_template, sample_builtin_template):
        """Test listing multiple templates."""
        from app.api.templates import list_templates

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_all_templates.return_value = [sample_template, sample_builtin_template]

            result = await list_templates(profile_id=None, category=None, token="test-token")

        assert len(result) == 2


# =============================================================================
# GET /api/v1/templates/categories - List Categories
# =============================================================================

class TestListCategories:
    """Tests for listing template categories."""

    @pytest.mark.asyncio
    async def test_list_categories_success(self):
        """Test successfully listing all categories."""
        from app.api.templates import list_template_categories

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template_categories.return_value = ["coding", "writing", "general"]

            result = await list_template_categories(token="test-token")

        assert len(result) == 3
        assert "coding" in result
        assert "writing" in result
        assert "general" in result
        mock_database.get_template_categories.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_categories_empty(self):
        """Test listing categories when none exist."""
        from app.api.templates import list_template_categories

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template_categories.return_value = []

            result = await list_template_categories(token="test-token")

        assert result == []


# =============================================================================
# POST /api/v1/templates - Create Template
# =============================================================================

class TestCreateTemplate:
    """Tests for creating templates."""

    @pytest.mark.asyncio
    async def test_create_template_success(self, sample_template):
        """Test successfully creating a template."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            mock_database.create_template.return_value = sample_template

            template_data = TemplateCreate(
                name="Test Template",
                prompt="Test prompt content",
                description="A test template description",
                icon="code",
                category="coding"
            )

            result = await create_template(template_data=template_data, token="test-token")

        assert result["name"] == sample_template["name"]
        mock_database.create_template.assert_called_once()
        # Verify template_id format
        call_kwargs = mock_database.create_template.call_args.kwargs
        assert call_kwargs["template_id"].startswith("tmpl-")

    @pytest.mark.asyncio
    async def test_create_template_minimal(self, sample_template):
        """Test creating a template with minimal required fields."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            mock_database.create_template.return_value = sample_template

            template_data = TemplateCreate(name="Minimal Template", prompt="Minimal prompt")

            result = await create_template(template_data=template_data, token="test-token")

        assert result is not None
        mock_database.create_template.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_template_with_profile(self, sample_template, sample_profile):
        """Test creating a template linked to a profile."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_profile.return_value = sample_profile
            template_with_profile = {**sample_template, "profile_id": sample_profile["id"]}
            mock_database.create_template.return_value = template_with_profile

            template_data = TemplateCreate(
                name="Profile Template",
                prompt="Profile-linked prompt",
                profile_id=sample_profile["id"]
            )

            result = await create_template(template_data=template_data, token="test-token")

        assert result["profile_id"] == sample_profile["id"]
        mock_database.get_profile.assert_called_once_with(sample_profile["id"])

    @pytest.mark.asyncio
    async def test_create_template_invalid_profile(self):
        """Test creating a template with non-existent profile."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_profile.return_value = None

            template_data = TemplateCreate(
                name="Bad Profile Template",
                prompt="Some prompt",
                profile_id="non-existent-profile"
            )

            with pytest.raises(HTTPException) as exc_info:
                await create_template(template_data=template_data, token="test-token")

        assert exc_info.value.status_code == 400
        assert "Profile not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_template_null_profile(self, sample_template):
        """Test creating template with null profile_id (global template)."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            mock_database.create_template.return_value = sample_template

            template_data = TemplateCreate(
                name="Global Template",
                prompt="Global prompt",
                profile_id=None
            )

            result = await create_template(template_data=template_data, token="test-token")

        assert result is not None
        # get_profile should not be called when profile_id is None
        mock_database.get_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_template_all_fields(self, sample_template):
        """Test creating template with all optional fields."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            mock_database.create_template.return_value = sample_template

            template_data = TemplateCreate(
                name="Full Template",
                prompt="Full prompt",
                description="Full description",
                icon="star",
                category="general",
                profile_id=None
            )

            result = await create_template(template_data=template_data, token="test-token")

        call_kwargs = mock_database.create_template.call_args.kwargs
        assert call_kwargs["name"] == "Full Template"
        assert call_kwargs["prompt"] == "Full prompt"
        assert call_kwargs["description"] == "Full description"
        assert call_kwargs["icon"] == "star"
        assert call_kwargs["category"] == "general"


# =============================================================================
# GET /api/v1/templates/{template_id} - Get Single Template
# =============================================================================

class TestGetTemplate:
    """Tests for getting a single template."""

    @pytest.mark.asyncio
    async def test_get_template_success(self, sample_template):
        """Test successfully getting a template by ID."""
        from app.api.templates import get_template

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template

            result = await get_template(template_id=sample_template["id"], token="test-token")

        assert result["id"] == sample_template["id"]
        assert result["name"] == sample_template["name"]
        mock_database.get_template.assert_called_once_with(sample_template["id"])

    @pytest.mark.asyncio
    async def test_get_template_not_found(self):
        """Test getting a non-existent template."""
        from app.api.templates import get_template

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await get_template(template_id="non-existent", token="test-token")

        assert exc_info.value.status_code == 404
        assert "Template not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_builtin_template(self, sample_builtin_template):
        """Test getting a builtin template."""
        from app.api.templates import get_template

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_builtin_template

            result = await get_template(template_id=sample_builtin_template["id"], token="test-token")

        assert result["is_builtin"] is True


# =============================================================================
# PATCH /api/v1/templates/{template_id} - Update Template
# =============================================================================

class TestUpdateTemplate:
    """Tests for updating templates."""

    @pytest.mark.asyncio
    async def test_update_template_success(self, sample_template):
        """Test successfully updating a template."""
        from app.api.templates import update_template
        from app.core.models import TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template
            updated_template = {**sample_template, "name": "Updated Name"}
            mock_database.update_template.return_value = updated_template

            update_data = TemplateUpdate(name="Updated Name")
            result = await update_template(
                template_id=sample_template["id"],
                template_data=update_data,
                token="test-token"
            )

        assert result["name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_update_template_all_fields(self, sample_template):
        """Test updating all template fields."""
        from app.api.templates import update_template
        from app.core.models import TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template
            updated_template = {
                **sample_template,
                "name": "New Name",
                "description": "New Description",
                "prompt": "New Prompt",
                "icon": "new-icon",
                "category": "new-category"
            }
            mock_database.update_template.return_value = updated_template

            update_data = TemplateUpdate(
                name="New Name",
                description="New Description",
                prompt="New Prompt",
                icon="new-icon",
                category="new-category"
            )
            result = await update_template(
                template_id=sample_template["id"],
                template_data=update_data,
                token="test-token"
            )

        assert result["name"] == "New Name"
        mock_database.update_template.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_template_with_profile(self, sample_template, sample_profile):
        """Test updating template with a valid profile."""
        from app.api.templates import update_template
        from app.core.models import TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template
            mock_database.get_profile.return_value = sample_profile
            updated_template = {**sample_template, "profile_id": sample_profile["id"]}
            mock_database.update_template.return_value = updated_template

            update_data = TemplateUpdate(profile_id=sample_profile["id"])
            result = await update_template(
                template_id=sample_template["id"],
                template_data=update_data,
                token="test-token"
            )

        assert result["profile_id"] == sample_profile["id"]
        mock_database.get_profile.assert_called_once_with(sample_profile["id"])

    @pytest.mark.asyncio
    async def test_update_template_invalid_profile(self, sample_template):
        """Test updating template with non-existent profile."""
        from app.api.templates import update_template
        from app.core.models import TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template
            mock_database.get_profile.return_value = None

            update_data = TemplateUpdate(profile_id="non-existent-profile")

            with pytest.raises(HTTPException) as exc_info:
                await update_template(
                    template_id=sample_template["id"],
                    template_data=update_data,
                    token="test-token"
                )

        assert exc_info.value.status_code == 400
        assert "Profile not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_template_not_found(self):
        """Test updating a non-existent template."""
        from app.api.templates import update_template
        from app.core.models import TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = None

            update_data = TemplateUpdate(name="New Name")

            with pytest.raises(HTTPException) as exc_info:
                await update_template(
                    template_id="non-existent",
                    template_data=update_data,
                    token="test-token"
                )

        assert exc_info.value.status_code == 404
        assert "Template not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_builtin_template_forbidden(self, sample_builtin_template):
        """Test that updating a builtin template is forbidden."""
        from app.api.templates import update_template
        from app.core.models import TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_builtin_template

            update_data = TemplateUpdate(name="New Name")

            with pytest.raises(HTTPException) as exc_info:
                await update_template(
                    template_id=sample_builtin_template["id"],
                    template_data=update_data,
                    token="test-token"
                )

        assert exc_info.value.status_code == 403
        assert "Cannot modify builtin templates" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_template_db_returns_none(self, sample_template):
        """Test handling when update_template returns None."""
        from app.api.templates import update_template
        from app.core.models import TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template
            mock_database.update_template.return_value = None

            update_data = TemplateUpdate(name="New Name")

            with pytest.raises(HTTPException) as exc_info:
                await update_template(
                    template_id=sample_template["id"],
                    template_data=update_data,
                    token="test-token"
                )

        assert exc_info.value.status_code == 404
        assert "Template not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_template_empty_payload(self, sample_template):
        """Test updating template with empty payload (no-op)."""
        from app.api.templates import update_template
        from app.core.models import TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template
            mock_database.update_template.return_value = sample_template

            update_data = TemplateUpdate()
            result = await update_template(
                template_id=sample_template["id"],
                template_data=update_data,
                token="test-token"
            )

        assert result is not None


# =============================================================================
# DELETE /api/v1/templates/{template_id} - Delete Template
# =============================================================================

class TestDeleteTemplate:
    """Tests for deleting templates."""

    @pytest.mark.asyncio
    async def test_delete_template_success(self, sample_template):
        """Test successfully deleting a template."""
        from app.api.templates import delete_template

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template
            mock_database.delete_template.return_value = True

            # Should not raise an exception
            result = await delete_template(template_id=sample_template["id"], token="test-token")

        mock_database.delete_template.assert_called_once_with(sample_template["id"])

    @pytest.mark.asyncio
    async def test_delete_template_not_found(self):
        """Test deleting a non-existent template."""
        from app.api.templates import delete_template

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await delete_template(template_id="non-existent", token="test-token")

        assert exc_info.value.status_code == 404
        assert "Template not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_builtin_template_forbidden(self, sample_builtin_template):
        """Test that deleting a builtin template is forbidden."""
        from app.api.templates import delete_template

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_builtin_template

            with pytest.raises(HTTPException) as exc_info:
                await delete_template(template_id=sample_builtin_template["id"], token="test-token")

        assert exc_info.value.status_code == 403
        assert "Cannot delete builtin templates" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_template_db_returns_false(self, sample_template):
        """Test handling when delete_template returns False."""
        from app.api.templates import delete_template

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template
            mock_database.delete_template.return_value = False

            with pytest.raises(HTTPException) as exc_info:
                await delete_template(template_id=sample_template["id"], token="test-token")

        assert exc_info.value.status_code == 404
        assert "Template not found" in exc_info.value.detail


# =============================================================================
# Edge Cases and Additional Coverage
# =============================================================================

class TestTemplatesEdgeCases:
    """Edge case tests for template endpoints."""

    @pytest.mark.asyncio
    async def test_template_with_special_characters_in_name(self, sample_template):
        """Test creating template with special characters in name."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            special_template = {**sample_template, "name": "Test <Template> & 'Special' \"Chars\""}
            mock_database.create_template.return_value = special_template

            template_data = TemplateCreate(
                name="Test <Template> & 'Special' \"Chars\"",
                prompt="Some prompt"
            )

            result = await create_template(template_data=template_data, token="test-token")

        assert result["name"] == "Test <Template> & 'Special' \"Chars\""

    @pytest.mark.asyncio
    async def test_template_with_long_prompt(self, sample_template):
        """Test creating template with a very long prompt."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            long_prompt = "x" * 10000
            long_template = {**sample_template, "prompt": long_prompt}
            mock_database.create_template.return_value = long_template

            template_data = TemplateCreate(name="Long Prompt Template", prompt=long_prompt)

            result = await create_template(template_data=template_data, token="test-token")

        assert result is not None

    @pytest.mark.asyncio
    async def test_template_with_unicode(self, sample_template):
        """Test creating template with unicode content."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            unicode_template = {**sample_template, "name": "Template with Unicode", "prompt": "Hello World"}
            mock_database.create_template.return_value = unicode_template

            template_data = TemplateCreate(
                name="Template with Unicode",
                prompt="Hello World",
                description="Contains unicode"
            )

            result = await create_template(template_data=template_data, token="test-token")

        assert result is not None

    @pytest.mark.asyncio
    async def test_template_with_multiline_prompt(self, sample_template):
        """Test creating template with multiline prompt."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            multiline_prompt = "Line 1\nLine 2\nLine 3"
            multiline_template = {**sample_template, "prompt": multiline_prompt}
            mock_database.create_template.return_value = multiline_template

            template_data = TemplateCreate(name="Multiline Template", prompt=multiline_prompt)

            result = await create_template(template_data=template_data, token="test-token")

        assert result is not None

    @pytest.mark.asyncio
    async def test_list_templates_with_profile_that_returns_global(self, sample_template):
        """Test that listing with profile_id includes global templates."""
        from app.api.templates import list_templates

        with patch("app.api.templates.database") as mock_database:
            global_template = {**sample_template, "profile_id": None}
            profile_template = {**sample_template, "id": "tmpl-profile", "profile_id": "some-profile"}
            mock_database.get_all_templates.return_value = [global_template, profile_template]

            result = await list_templates(profile_id="some-profile", category=None, token="test-token")

        assert len(result) == 2
        # Database function handles the filtering
        mock_database.get_all_templates.assert_called_once_with(profile_id="some-profile", category=None)


# =============================================================================
# Integration-style Tests
# =============================================================================

class TestTemplatesIntegration:
    """Integration-style tests simulating workflows."""

    @pytest.mark.asyncio
    async def test_create_and_get_template(self):
        """Test creating and then retrieving a template."""
        from app.api.templates import create_template, get_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            created = {
                "id": "tmpl-created1",
                "name": "Created Template",
                "description": "Test",
                "prompt": "Test prompt",
                "profile_id": None,
                "icon": None,
                "category": None,
                "is_builtin": False,
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }
            mock_database.create_template.return_value = created

            template_data = TemplateCreate(
                name="Created Template",
                description="Test",
                prompt="Test prompt"
            )
            create_result = await create_template(template_data=template_data, token="test-token")
            template_id = create_result["id"]

            # Get the template
            mock_database.get_template.return_value = created
            get_result = await get_template(template_id=template_id, token="test-token")

        assert get_result["name"] == "Created Template"

    @pytest.mark.asyncio
    async def test_create_update_delete_template(self):
        """Test full lifecycle: create, update, delete template."""
        from app.api.templates import create_template, update_template, delete_template
        from app.core.models import TemplateCreate, TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            template_id = "tmpl-lifecycle"

            # Create
            created = {
                "id": template_id,
                "name": "Lifecycle Template",
                "description": None,
                "prompt": "Original prompt",
                "profile_id": None,
                "icon": None,
                "category": None,
                "is_builtin": False,
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }
            mock_database.create_template.return_value = created

            create_data = TemplateCreate(name="Lifecycle Template", prompt="Original prompt")
            create_result = await create_template(template_data=create_data, token="test-token")
            assert create_result["name"] == "Lifecycle Template"

            # Update
            mock_database.get_template.return_value = created
            updated = {**created, "name": "Updated Lifecycle", "prompt": "Updated prompt"}
            mock_database.update_template.return_value = updated

            update_data = TemplateUpdate(name="Updated Lifecycle", prompt="Updated prompt")
            update_result = await update_template(
                template_id=template_id,
                template_data=update_data,
                token="test-token"
            )
            assert update_result["name"] == "Updated Lifecycle"

            # Delete
            mock_database.get_template.return_value = updated
            mock_database.delete_template.return_value = True

            await delete_template(template_id=template_id, token="test-token")
            mock_database.delete_template.assert_called_once_with(template_id)

    @pytest.mark.asyncio
    async def test_template_categories_workflow(self, sample_template):
        """Test categories are correctly returned."""
        from app.api.templates import create_template, list_template_categories
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            # Create template with category
            template_with_category = {**sample_template, "category": "development"}
            mock_database.create_template.return_value = template_with_category

            template_data = TemplateCreate(
                name="Dev Template",
                prompt="Development prompt",
                category="development"
            )
            await create_template(template_data=template_data, token="test-token")

            # List categories
            mock_database.get_template_categories.return_value = ["development", "testing"]
            categories = await list_template_categories(token="test-token")

        assert "development" in categories


# =============================================================================
# Additional Coverage Tests
# =============================================================================

class TestAdditionalCoverage:
    """Additional tests to ensure maximum coverage."""

    @pytest.mark.asyncio
    async def test_create_template_with_all_none_optionals(self, sample_template):
        """Test creating template with explicitly None optional values."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            mock_database.create_template.return_value = sample_template

            template_data = TemplateCreate(
                name="Test",
                prompt="Test prompt",
                description=None,
                icon=None,
                category=None,
                profile_id=None
            )

            result = await create_template(template_data=template_data, token="test-token")

        assert result is not None
        call_kwargs = mock_database.create_template.call_args.kwargs
        assert call_kwargs["description"] is None
        assert call_kwargs["icon"] is None
        assert call_kwargs["category"] is None
        assert call_kwargs["profile_id"] is None

    @pytest.mark.asyncio
    async def test_update_template_partial_fields(self, sample_template):
        """Test updating template with only some fields."""
        from app.api.templates import update_template
        from app.core.models import TemplateUpdate

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_template.return_value = sample_template
            updated = {**sample_template, "description": "New Description"}
            mock_database.update_template.return_value = updated

            update_data = TemplateUpdate(description="New Description")
            result = await update_template(
                template_id=sample_template["id"],
                template_data=update_data,
                token="test-token"
            )

        assert result["description"] == "New Description"
        call_kwargs = mock_database.update_template.call_args.kwargs
        assert call_kwargs["description"] == "New Description"
        assert call_kwargs["name"] is None  # Other fields should be None

    @pytest.mark.asyncio
    async def test_list_templates_returns_builtin_and_custom(self, sample_template, sample_builtin_template):
        """Test that both builtin and custom templates are returned."""
        from app.api.templates import list_templates

        with patch("app.api.templates.database") as mock_database:
            mock_database.get_all_templates.return_value = [sample_builtin_template, sample_template]

            result = await list_templates(profile_id=None, category=None, token="test-token")

        assert len(result) == 2
        builtin_count = sum(1 for t in result if t.get("is_builtin"))
        custom_count = sum(1 for t in result if not t.get("is_builtin"))
        assert builtin_count == 1
        assert custom_count == 1

    @pytest.mark.asyncio
    async def test_template_id_format_is_correct(self):
        """Test that generated template IDs follow expected format."""
        from app.api.templates import create_template
        from app.core.models import TemplateCreate

        with patch("app.api.templates.database") as mock_database:
            mock_database.create_template.return_value = {"id": "tmpl-12345678"}

            template_data = TemplateCreate(name="Test", prompt="Test")
            await create_template(template_data=template_data, token="test-token")

        call_kwargs = mock_database.create_template.call_args.kwargs
        template_id = call_kwargs["template_id"]
        assert template_id.startswith("tmpl-")
        assert len(template_id) == 13  # tmpl- (5) + 8 hex chars
