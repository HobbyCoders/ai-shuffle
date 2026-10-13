"""The seven prompt-cache miss classes, re-exported from fluid.

The classifier lives in ``fluid.context.cache_miss_classes`` so the engine can
stamp each class on its own event log; the app's adapter counts them.
"""

from fluid.context.cache_miss_classes import (  # noqa: F401
    APPEND_GROWTH,
    COMPACTION_FOLLOWON,
    DEFAULT_CACHE_TTL_SECONDS,
    HARNESS_MUTATION,
    HARNESS_UPGRADE,
    IDLE_PAST_TTL,
    MISS_CLASSES,
    PLANNED_REBUILD,
    PROVIDER_FALLBACK,
    UNEXPECTED_CLASSES,
    classify_cache_miss,
    usage_miss_class,
)
