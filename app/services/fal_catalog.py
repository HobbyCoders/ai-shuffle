"""
fal.ai Model Catalog Service

Fetches, caches, and serves the full fal.ai model catalog (~600+ models).
Uses the fal.ai platform API to discover models dynamically, so new models
are available without code changes.

Catalog is persisted to PostgreSQL for instant availability on startup,
and refreshed in the background periodically (default: every hour).
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx
# Bound at import: tests swap the module's ``httpx`` for a stub client factory.
from httpx import HTTPStatusError, TimeoutException

from app.db.async_database import run_sync
from app.db.base import get_db

logger = logging.getLogger(__name__)

# Map fal.ai categories to our internal tool types
CATEGORY_TO_TOOL_TYPE = {
    "text-to-image": "image_generation",
    "image-to-image": "image_editing",
    "text-to-video": "video_generation",
    "image-to-video": "image_to_video",
    "video-to-video": "video_generation",
    "text-to-3d": "text_to_3d",
    "image-to-3d": "image_to_3d",
    "3d-to-3d": "text_to_3d",
    "text-to-speech": "tts",
    "speech-to-text": "stt",
    "text-to-audio": "audio_generation",  # music / sound generation (Lyria, Stable Audio, ACE-Step, ...)
    "text-to-music": "audio_generation",
    "audio-to-audio": "audio_to_audio",  # remix / style transfer / restoration
    "audio-to-video": "video_generation",
}

# Categories that are deliberately excluded from the catalog (non-inference or
# unusable by the generic executor). Everything NOT listed here is kept — even
# if its category is unmapped — so new fal.ai categories never silently vanish.
SKIP_CATEGORIES = {
    "training",     # training-only models, not runnable for inference
    "text-to-json", # structured-output helpers, not media generation
    "unknown",      # no category metadata from fal.ai
    "",
}

# Fallback tool type for inference models whose category we don't explicitly map.
# Keeps them discoverable via searchModels / runnable via runFalModel.
GENERIC_TOOL_TYPE = "generic"

# Categories that produce specific output types (for generic executor)
CATEGORY_TO_OUTPUT_TYPE = {
    "text-to-image": "image",
    "image-to-image": "image",
    "text-to-video": "video",
    "image-to-video": "video",
    "video-to-video": "video",
    "audio-to-video": "video",
    "text-to-3d": "3d",
    "image-to-3d": "3d",
    "3d-to-3d": "3d",
    "text-to-speech": "audio",
    "text-to-audio": "audio",
    "text-to-music": "audio",
    "audio-to-audio": "audio",
}

# Map the "...-to-X" suffix of a category to an output type, so we can infer the
# output of unmapped categories (used as a fallback for generic models).
_OUTPUT_SUFFIX_TO_TYPE = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "speech": "audio",
    "music": "audio",
    "sound": "audio",
    "3d": "3d",
    "mesh": "3d",
}


def resolve_tool_type(category: str) -> Optional[str]:
    """Resolve a fal.ai category to an internal tool type.

    Returns None only for categories we deliberately skip; any other (unmapped)
    inference category falls back to a generic tool type so it stays usable.
    """
    if category in SKIP_CATEGORIES:
        return None
    return CATEGORY_TO_TOOL_TYPE.get(category, GENERIC_TOOL_TYPE)


def resolve_output_type(category: str) -> Optional[str]:
    """Resolve a fal.ai category to an output type, inferring from the category
    name (e.g. "text-to-audio" -> "audio") when not explicitly mapped."""
    explicit = CATEGORY_TO_OUTPUT_TYPE.get(category)
    if explicit:
        return explicit
    suffix = category.rsplit("-to-", 1)[-1] if category else ""
    return _OUTPUT_SUFFIX_TO_TYPE.get(suffix)

FAL_CATALOG_API = "https://api.fal.ai/v1/models"
DEFAULT_CACHE_TTL = 3600  # 1 hour


@dataclass
class CatalogModel:
    """A single model from the fal.ai catalog."""
    endpoint_id: str
    display_name: str
    category: str
    description: str
    status: str = "active"
    thumbnail_url: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    group_key: Optional[str] = None
    group_label: Optional[str] = None
    updated_at: Optional[str] = None
    tool_type: Optional[str] = None
    output_type: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_api_response(cls, data: dict) -> "CatalogModel":
        """Parse a single model from the fal.ai API response."""
        metadata = data.get("metadata", {})
        category = metadata.get("category", "unknown")
        group = metadata.get("group", {})

        return cls(
            endpoint_id=data.get("endpoint_id", ""),
            display_name=metadata.get("display_name", data.get("endpoint_id", "")),
            category=category,
            description=metadata.get("description", ""),
            status=metadata.get("status", "active"),
            thumbnail_url=metadata.get("thumbnail_url"),
            tags=metadata.get("tags", []),
            group_key=group.get("key") if group else None,
            group_label=group.get("label") if group else None,
            updated_at=metadata.get("updated_at"),
            tool_type=resolve_tool_type(category),
            output_type=resolve_output_type(category),
        )

    @classmethod
    def from_db_row(cls, row: dict) -> "CatalogModel":
        """Parse from a database row.

        ``tool_type`` / ``output_type`` are *derived* values: they are persisted
        for convenience but recomputed from the raw ``category`` on load so the
        mapping code (``resolve_tool_type`` / ``resolve_output_type``) is the
        single source of truth. Without this, a mapping change (e.g. splitting
        "text-to-audio" out of the generic "audio" tool type into
        "audio_generation") would leave the cached catalog stuck on the old
        tool_type until a network refresh, leaving schema-driven dropdowns empty.
        """
        tags = []
        if row.get("tags_json"):
            try:
                tags = json.loads(row["tags_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        category = row["category"]
        return cls(
            endpoint_id=row["endpoint_id"],
            display_name=row["display_name"],
            category=category,
            description=row.get("description", ""),
            status=row.get("status", "active"),
            thumbnail_url=row.get("thumbnail_url"),
            tags=tags,
            group_key=row.get("group_key"),
            group_label=row.get("group_label"),
            updated_at=row.get("updated_at"),
            tool_type=resolve_tool_type(category),
            output_type=resolve_output_type(category),
        )


def _describe_refresh_error(error: Exception) -> str:
    """A short, key-free reason a catalog fetch failed."""
    if isinstance(error, HTTPStatusError):
        return f"fal.ai answered HTTP {error.response.status_code}"
    if isinstance(error, TimeoutException):
        return "fal.ai did not answer in time"
    return f"could not reach fal.ai ({type(error).__name__})"


class FalCatalogService:
    """
    Service that manages the fal.ai model catalog.

    - Fetches all models from fal.ai's platform API
    - Caches in memory with configurable TTL
    - Persists to PostgreSQL for instant startup
    - Provides search, filter, and lookup methods
    """

    def __init__(self, cache_ttl: int = DEFAULT_CACHE_TTL):
        self._cache: list[CatalogModel] = []
        self._cache_ttl = cache_ttl
        self._last_refresh: float = 0
        self._refreshing = False

    @property
    def is_stale(self) -> bool:
        return time.time() - self._last_refresh > self._cache_ttl

    @property
    def model_count(self) -> int:
        return len(self._cache)

    def load_from_db(self) -> int:
        """Load catalog from database on startup. Returns count of models loaded.

        Seeds ``_last_refresh`` from the persisted data's actual age
        (``fetched_at``) rather than the load time, so a snapshot that is
        already older than the TTL is treated as stale and refreshed on the
        first request after startup — instead of being trusted for another
        full hour every time the process restarts.
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM fal_model_catalog WHERE status = 'active' ORDER BY display_name"
                )
                rows = cursor.fetchall()
                self._cache = [CatalogModel.from_db_row(dict(row)) for row in rows]

                # Use the freshest persisted row's timestamp as the effective
                # last-refresh time. If it's missing (legacy rows), leave
                # _last_refresh at 0 so the catalog is considered stale and
                # refreshes promptly.
                cursor.execute(
                    "SELECT EXTRACT(EPOCH FROM MAX(fetched_at)) AS ts "
                    "FROM fal_model_catalog WHERE status = 'active'"
                )
                row = cursor.fetchone()
                fetched_epoch = dict(row).get("ts") if row else None

            if self._cache:
                self._last_refresh = float(fetched_epoch) if fetched_epoch is not None else 0.0
                age = time.time() - self._last_refresh
                logger.info(
                    f"Loaded {len(self._cache)} models from catalog cache "
                    f"(data age {age / 60:.0f} min, stale={self.is_stale})"
                )
            return len(self._cache)
        except Exception as e:
            logger.debug(f"No catalog cache in DB (expected on first run): {e}")
            return 0

    async def refresh(self, api_key: Optional[str] = None) -> int:
        """
        Fetch the full model catalog from fal.ai API.

        Paginates through all results, filters to inference models only,
        and persists to both memory and PostgreSQL.

        Returns the number of models in the catalog afterwards (the cached
        count when the fetch fails; see ``refresh_with_status``).
        """
        count, _error = await self.refresh_with_status(api_key)
        return count

    async def refresh_with_status(self, api_key: Optional[str] = None) -> tuple[int, Optional[str]]:
        """``refresh`` that also says why it kept the cache.

        Returns ``(model count, error)``: ``error`` is ``None`` when fal.ai
        answered with models (or another refresh is already running), else a
        short reason with no key material, and the count is the cached one.
        """
        if self._refreshing:
            logger.debug("Catalog refresh already in progress, skipping")
            return len(self._cache), None

        self._refreshing = True
        try:
            models = await self._fetch_all_models(api_key)
            if not models:
                logger.warning("fal.ai catalog API returned no models, keeping existing cache")
                return len(self._cache), "fal.ai returned no models"

            self._cache = models
            self._last_refresh = time.time()
            # Off-loop: _persist_to_db is a synchronous DELETE + one INSERT per
            # model (~600 rows) in a single get_db() transaction. Run inline it
            # froze every other request on the worker for the whole write, and
            # refresh() is awaited from an MCP tool (ai_tools_sdk) mid-turn as
            # well as from the catalog API routes. The scanner cannot see this
            # one: it goes through get_db() rather than a `database.*` call.
            await run_sync(self._persist_to_db, models)

            logger.info(f"Refreshed fal.ai catalog: {len(models)} models across {len(self.get_categories())} categories")
            return len(models), None
        except Exception as e:
            logger.error(f"Failed to refresh fal.ai catalog: {e}")
            # Keep existing cache on failure
            return len(self._cache), _describe_refresh_error(e)
        finally:
            self._refreshing = False

    async def _fetch_all_models(self, api_key: Optional[str] = None) -> list[CatalogModel]:
        """Paginate through the fal.ai catalog API and return all inference models."""
        all_models: list[CatalogModel] = []
        cursor: Optional[str] = None
        page = 0

        headers = {}
        if api_key:
            headers["Authorization"] = f"Key {api_key}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                page += 1
                params: dict = {
                    "status": "active",
                    "limit": "100",
                }
                if cursor:
                    params["cursor"] = cursor

                response = await client.get(
                    FAL_CATALOG_API,
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

                for model_data in data.get("models", []):
                    model = CatalogModel.from_api_response(model_data)

                    # Skip only deliberately-excluded categories (training,
                    # text-to-json, unknown); unmapped inference categories are
                    # kept with a generic tool type so new fal.ai categories
                    # (e.g. a new music model) don't silently disappear.
                    if model.tool_type is None:
                        continue

                    # Skip deprecated models
                    if model.status != "active":
                        continue

                    all_models.append(model)

                # Check for more pages
                has_more = data.get("has_more", False)
                next_cursor = data.get("next_cursor")

                if not has_more or not next_cursor:
                    break

                cursor = next_cursor

                # Safety: prevent infinite loops
                if page > 20:
                    logger.warning("Catalog fetch hit 20 pages, stopping pagination")
                    break

        return all_models

    def _persist_to_db(self, models: list[CatalogModel]) -> None:
        """Persist the catalog to database, replacing all existing entries."""
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM fal_model_catalog")
                for model in models:
                    cursor.execute(
                        """INSERT INTO fal_model_catalog
                           (endpoint_id, display_name, category, description, status,
                            thumbnail_url, tags_json, group_key, group_label,
                            tool_type, output_type, updated_at, fetched_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                        (
                            model.endpoint_id,
                            model.display_name,
                            model.category,
                            model.description,
                            model.status,
                            model.thumbnail_url,
                            json.dumps(model.tags),
                            model.group_key,
                            model.group_label,
                            model.tool_type,
                            model.output_type,
                            model.updated_at,
                        ),
                    )
        except Exception as e:
            logger.error(f"Failed to persist catalog to DB: {e}")

    # =========================================================================
    # Query Methods
    # =========================================================================

    def search(
        self,
        q: Optional[str] = None,
        category: Optional[str] = None,
        tool_type: Optional[str] = None,
        output_type: Optional[str] = None,
    ) -> list[CatalogModel]:
        """Search and filter models."""
        results = self._cache

        if category:
            results = [m for m in results if m.category == category]

        if tool_type:
            results = [m for m in results if m.tool_type == tool_type]

        if output_type:
            results = [m for m in results if m.output_type == output_type]

        if q:
            q_lower = q.lower()
            results = [
                m for m in results
                if q_lower in m.display_name.lower()
                or q_lower in m.description.lower()
                or q_lower in m.endpoint_id.lower()
                or any(q_lower in tag.lower() for tag in m.tags)
            ]

        return results

    def get_categories(self) -> list[dict]:
        """Get all categories with model counts."""
        category_counts: dict[str, int] = {}
        for model in self._cache:
            category_counts[model.category] = category_counts.get(model.category, 0) + 1

        return [
            {"category": cat, "count": count, "tool_type": resolve_tool_type(cat)}
            for cat, count in sorted(category_counts.items(), key=lambda x: -x[1])
        ]


# Singleton instance
catalog_service = FalCatalogService()
