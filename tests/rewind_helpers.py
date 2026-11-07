"""Finish real rewind requests before a test closes their loop or database."""


async def wait_for_rewind_release(session_id):
    # These callers own all requests for this test session. A response can arrive
    # before its tracked write threads release the lease; one sleep(0) is not a
    # completion guarantee. Run this on the request's loop (TestClient: portal).
    from app.core.provider import session_admission

    lease = session_admission._rewinds.get(session_id)
    if lease is not None:
        from tests._admission_cleanup import wait_for_lease_release

        await wait_for_lease_release(lease)


async def execute_rewind_and_wait(session_id, request):
    from app.api.commands import execute_rewind

    try:
        return await execute_rewind(session_id, request)
    finally:
        await wait_for_rewind_release(session_id)
