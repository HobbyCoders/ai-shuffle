from app.db import database


def _register(install_id: str, *, approve_new: bool = False):
    return database.register_install(
        install_id=install_id,
        device_name=install_id,
        base_url=f"http://{install_id}:8000",
        platform="test",
        version="1",
        capabilities={},
        approve_new=approve_new,
    )


def test_new_runner_is_quarantined_until_approved():
    pending = _register("pending-runner")
    assert pending is not None
    assert pending["approved"] is False
    assert database.is_install_approved("pending-runner") is False

    approved = database.set_install_approval("pending-runner", True)
    assert approved is not None
    assert approved["approved"] is True
    assert database.is_install_approved("pending-runner") is True

    revoked = database.set_install_approval("pending-runner", False)
    assert revoked is not None
    assert revoked["approved"] is False
    assert revoked["revoked_at"] is not None


def test_first_runner_can_be_created_as_approved():
    runner = _register("first-runner", approve_new=True)
    assert runner is not None
    assert runner["approved"] is True
