"""Host turn-source classifications shared by routing and generation permissions.

Loop detection has separate accounting origins; they do not decide whether a
person can answer a permission prompt (notably for connector turns).
"""

# Sources typed into the composer; only these may choose the owner's most-used
# model. Unknown sources keep unattended model-routing defaults.
INTERACTIVE_TURN_SOURCES = ("user", "queue", "worktree")

# A person may answer Ask even outside model-routing interactivity. Every other
# unattended source defaults to generation admission, including future sources.
# Voice-mode sessions are also person-present, independently of their source.
PERSON_PRESENT_TURN_SOURCES = frozenset((*INTERACTIVE_TURN_SOURCES, "api", "resume", "connector"))
