"""Unavailable delegated output guard policy, separate from the guard itself."""

# One-line selection: "deny" = A; "warn" = B; "split" = C.
UNAVAILABLE_GUARD_POLICY = "warn"

# Used only by the optional split policy, not the selected warn policy.
# A directly verified support bound, NOT the hook's introduction.
OLDEST_VERIFIED_GUARD_CLI = "2.1.286"

UNGUARDED_WARNING = (
    "Claude Code could not enable tool-output framing. Tools will continue "
    "without this guard. Update or repair Claude Code and retry the session."
)
