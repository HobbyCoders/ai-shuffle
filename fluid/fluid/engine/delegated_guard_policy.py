"""Unavailable delegated output guard policy, separate from the guard itself."""

# One-line selection: "deny" = A; "warn" = B; "split" = C.
UNAVAILABLE_GUARD_POLICY = "split"

# A conservative, directly verified support bound, NOT the hook's introduction.
# Older builds still use the guard when it loads. Unknown versions fail closed.
OLDEST_VERIFIED_GUARD_CLI = "2.1.286"

UNGUARDED_WARNING = (
    "Claude Code could not enable tool-output framing. Tools will continue "
    "without this guard for this older CLI. Update Claude Code and retry the session."
)
