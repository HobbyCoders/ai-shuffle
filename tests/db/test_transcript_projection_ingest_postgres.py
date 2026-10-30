"""Run the identical concurrent-ingest ratchets against the native PG pool."""
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401
from tests.test_transcript_projection_ingest_budget import (  # noqa: F401
    test_background_projection_preserves_ingest_p95,
    test_large_admissible_folds_preserve_loop_lag_and_ingest_p95,
)

pytestmark = requires_real_pg
