"""Stable names for retired-owner schema, installed clients and mixed-version peers.

Only the implementation modules and current HTTP callers were renamed. These
values must not change with Python names: old peers account remote work through
this collection/action, and saved transcripts replay the approval subtype/keys.
Historical SQL names remain in app.db.owner_state and app.db.spending_limits;
renaming them would strand existing identity and local/remote spending history.
None of these tombstones creates or executes a dedicated agent or hidden home.
"""

OWNER_COLLECTION = "assistant"
REMOTE_USAGE_ACTION = "remote-usage"
SPENDING_INTERRUPT_SOURCE = "assistant"
SPENDING_USAGE_SCOPE = "assistant_today"
SPENDING_STOP_REASON_PREFIX = "assistant_"
APPROVAL_SUBTYPE = "assistant_confirm"
APPROVAL_PASS_KEY = "assistant_confirm_pass"
APPROVAL_ANSWER_KEY = "assistant_confirm_answer"
LEGACY_HTTP_PREFIX = "/api/v1/assistant"
RETIRED_AGENT_ID = "builtin_assistant"
