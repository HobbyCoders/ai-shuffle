"""Run the identical concurrent-ingest ratchets against the native PG pool."""
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401
from tests.test_transcript_projection_ingest_budget import (  # noqa: F401
    test_background_projection_keeps_each_stage_nonblocking,
    test_stage_budget_detects_real_blocking,
    test_stage_budget_rejects_failed_cycles,
    test_stage_budget_tolerates_one_slow_append,
    test_projection_commit_does_not_block_same_session_append,
    test_large_preparation_allows_ingest_before_release,
)

pytestmark = requires_real_pg
