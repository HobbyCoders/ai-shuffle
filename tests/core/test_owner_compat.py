"""Wire/schema names survive the neutral implementation-module rename."""
from app.core import owner_compat, owner_confirm, spending_limits
from app.db import owner_state
from app.network import control, policy


def test_legacy_owner_protocol_names_are_stable() -> None:
    assert owner_compat.OWNER_COLLECTION == "assistant"
    collection = policy.collection(owner_compat.OWNER_COLLECTION)
    assert collection.table == "assistant_account"
    assert "assistant_name" in collection.hash_fields
    assert owner_state.AGENT_ID == "builtin_assistant"
    assert owner_confirm.SUBTYPE == "assistant_confirm"
    assert owner_confirm.PASS_KEY == "assistant_confirm_pass"
    assert owner_confirm.ANSWER_KEY == "assistant_confirm_answer"
    assert owner_compat.SPENDING_INTERRUPT_SOURCE == "assistant"
    assert owner_compat.SPENDING_USAGE_SCOPE == "assistant_today"
    assert owner_compat.SPENDING_STOP_REASON_PREFIX == "assistant_"
    assert spending_limits.REMOTE_USAGE_ACTION == "remote-usage"
    assert control._LAZY_HANDLERS["remote-usage"]() is spending_limits.handle_remote_usage
