"""Tests for the fal.ai catalog service — focused on tool-type resolution.

Regression coverage for the bug where the Text-to-Audio / Audio-to-Audio model
dropdowns were empty: the catalog persists a derived ``tool_type`` column, and
after the mapping changed ("text-to-audio" moved from the generic "audio" tool
type to "audio_generation") the cached rows still carried the *old* tool_type,
so the schema-driven dropdowns (which have no curated fallback) filtered to an
empty list. Loading must recompute tool_type/output_type from the raw category.
"""

from app.services.fal_catalog import (
    CatalogModel,
    resolve_tool_type,
    resolve_output_type,
)


def test_from_db_row_recomputes_tool_type_from_category():
    """A row persisted under the OLD mapping must be re-derived on load."""
    stale_row = {
        "endpoint_id": "fal-ai/lyria2",
        "display_name": "Lyria 2",
        "category": "text-to-audio",
        "description": "Music generation",
        "status": "active",
        "thumbnail_url": None,
        "tags_json": None,
        "group_key": None,
        "group_label": None,
        "updated_at": None,
        # Stale derived values from before the mapping split.
        "tool_type": "audio",
        "output_type": "audio",
    }

    model = CatalogModel.from_db_row(stale_row)

    # Recomputed from the (unchanged) raw category, not trusted from the column.
    assert model.tool_type == "audio_generation"
    assert model.output_type == "audio"


def test_from_db_row_recomputes_audio_to_audio():
    stale_row = {
        "endpoint_id": "fal-ai/some-remix",
        "display_name": "Remix",
        "category": "audio-to-audio",
        "tool_type": "audio",  # stale
        "output_type": "audio",
    }

    model = CatalogModel.from_db_row(stale_row)

    assert model.tool_type == "audio_to_audio"
    assert model.output_type == "audio"


def test_from_db_row_preserves_correct_mappings():
    """Rows whose persisted tool_type already matches stay correct."""
    row = {
        "endpoint_id": "fal-ai/flux-pro/v1.1-ultra",
        "display_name": "FLUX1.1 [pro] ultra",
        "category": "text-to-image",
        "tool_type": "image_generation",
        "output_type": "image",
    }

    model = CatalogModel.from_db_row(row)

    assert model.tool_type == "image_generation"
    assert model.output_type == "image"


def test_mapping_helpers_split_audio_tool_types():
    assert resolve_tool_type("text-to-audio") == "audio_generation"
    assert resolve_tool_type("text-to-music") == "audio_generation"
    assert resolve_tool_type("audio-to-audio") == "audio_to_audio"
    assert resolve_tool_type("text-to-speech") == "tts"
    assert resolve_output_type("text-to-audio") == "audio"
    assert resolve_output_type("audio-to-audio") == "audio"
