"""Cache-stable attribution policy shared by native prompts and delegated CLI."""

COMMIT_TRAILER = "Co-Authored-By: AI Shuffle <noreply@hobbycoders.com>"
PR_LINE = "🤖 Generated with [AI Shuffle](https://hobbycoders.com)"


def prompt_block(enabled: bool) -> str:
    """Native-only: the delegated CLI renders this policy from its settings."""
    if not enabled:
        return "# Attribution\nAdd no co-author trailer or generated-by line."
    return (
        "# Attribution\nWhen you write a git commit, end it with exactly "
        f"`{COMMIT_TRAILER}`; end PR descriptions with `{PR_LINE}`; "
        "never add provider or model co-authors."
    )


def cli_attribution(enabled: bool) -> dict[str, str | bool]:
    """Empty strings suppress the CLI defaults; never append a provider URL."""
    return {
        "commit": COMMIT_TRAILER if enabled else "",
        "pr": PR_LINE if enabled else "",
        "sessionUrl": False,
    }
