"""
Patch the SDK's session-store resume so it reuses the host's real Claude
config dir instead of a fresh temp ``CLAUDE_CONFIG_DIR`` per spawn.

Why this exists
---------------
The CLI's Claude-in-Chrome setup runs on every spawn that carries ``--chrome``
and writes a Chrome native-messaging manifest whose ``path`` embeds
``$CLAUDE_CONFIG_DIR/chrome/chrome-native-host.bat``. The stock SDK
materializes every store-backed resume into a brand-new temp dir
(``claude-resume-*``), so that path — and therefore the manifest content —
differs on every resumed spawn. The CLI treats any manifest rewrite as a
first-time install and OPENS THE EXTENSION RECONNECT PAGE IN THE BROWSER,
which is the "Chrome MCP reauth window on every message" users see whenever
their session was evicted from the pool between messages. Worse, the manifest
is left pointing into a temp dir that is deleted on disconnect, breaking the
extension's native host until the next spawn rewrites it.

The fix: when no custom ``CLAUDE_CONFIG_DIR`` is configured (the app's normal
deployment), materialize the store transcript into the REAL config dir —
``~/.claude/projects/<key>/<session>.jsonl``, the exact file the CLI resumes
from natively — and spawn the subprocess with NO config-dir override. Resumed
spawns then look identical to fresh spawns as far as the CLI's manifest check
is concerned: the manifest is stable, no reconnect page ever opens, and the
subprocess uses the real credentials file (so OAuth token refresh works,
unlike the stock temp copy which strips ``refreshToken``).

Deployments that DO set ``CLAUDE_CONFIG_DIR`` (process env or per-session
``options.env``) keep the SDK's stock temp-dir behavior untouched.

Trade-off (accepted): materialization overwrites the CLI's local transcript
for that session with the store's copy. The store is this app's source of
truth (it is populated from these very transcripts after each turn), so the
local file is a mirror, not independent data.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_PATCH_MARKER = "_ai_shuffle_stable_resume"


def _default_config_dir() -> Path:
    """The config dir the CLI uses when CLAUDE_CONFIG_DIR is unset (~/.claude)."""
    from claude_agent_sdk._internal.sessions import _get_claude_config_home_dir

    return _get_claude_config_home_dir()


def install_stable_resume_patch() -> None:
    """Install the stable-config-dir resume patch. Idempotent.

    Replaces ``materialize_resume_session`` and ``apply_materialized_options``
    in ``claude_agent_sdk._internal.session_resume`` (looked up lazily by
    ``ClaudeSDKClient.connect``) and re-binds the module-level references in
    ``claude_agent_sdk._internal.client`` (the one-shot ``query()`` path).
    """
    import claude_agent_sdk._internal.session_resume as sr

    if getattr(sr.materialize_resume_session, _PATCH_MARKER, False):
        return

    _orig_materialize = sr.materialize_resume_session
    _orig_apply = sr.apply_materialized_options

    async def materialize_stable(options):
        # Custom config dir in play → the manifest path is whatever the
        # operator chose; keep the SDK's isolated temp-dir behavior.
        custom = (options.env or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get(
            "CLAUDE_CONFIG_DIR"
        )
        if custom:
            return await _orig_materialize(options)

        store = options.session_store
        if store is None:
            return None
        if options.resume is None and not options.continue_conversation:
            return None

        timeout_s = options.load_timeout_ms / 1000
        project_key = sr.project_key_for_directory(options.cwd)

        if options.resume is not None:
            # Session id becomes a path component — same UUID guard as stock.
            if sr._validate_uuid(options.resume) is None:
                return None
            resolved = await sr._load_candidate(
                store, project_key, options.resume, timeout_s
            )
        else:
            resolved = await sr._resolve_continue_candidate(
                store, project_key, timeout_s
            )
        if resolved is None:
            return None
        session_id, entries = resolved

        config_dir = _default_config_dir()
        project_dir = config_dir / "projects" / project_key
        sr._write_jsonl(project_dir / f"{session_id}.jsonl", entries)
        # No auth-file copy: the subprocess reads the real ~/.claude
        # credentials directly (refresh token intact), exactly like a
        # fresh non-resume spawn.
        if sr._store_implements(store, "list_subkeys"):
            await sr._materialize_subkeys(
                store, config_dir, project_dir, project_key, session_id, timeout_s
            )

        async def _noop_cleanup() -> None:
            # The real config dir must never be deleted on disconnect.
            return None

        logger.debug(
            "[SDK patch] Materialized resume for %s into real config dir %s",
            session_id,
            config_dir,
        )
        return sr.MaterializedResume(
            config_dir=config_dir,
            resume_session_id=session_id,
            cleanup=_noop_cleanup,
        )

    def apply_stable(options, materialized):
        # Real config dir → spawn with NO CLAUDE_CONFIG_DIR override so the
        # subprocess resolves ~/.claude.json / credentials / chrome manifest
        # exactly like a fresh spawn (setting CLAUDE_CONFIG_DIR=~/.claude
        # would move the CLI's .claude.json lookup from ~/.claude.json to
        # ~/.claude/.claude.json, which does not exist).
        if Path(materialized.config_dir) == _default_config_dir():
            from dataclasses import replace

            return replace(
                options,
                resume=materialized.resume_session_id,
                continue_conversation=False,
            )
        return _orig_apply(options, materialized)

    setattr(materialize_stable, _PATCH_MARKER, True)
    setattr(apply_stable, _PATCH_MARKER, True)
    sr.materialize_resume_session = materialize_stable
    sr.apply_materialized_options = apply_stable

    # _internal.client binds these at module import time — re-bind if loaded.
    import claude_agent_sdk._internal.client as internal_client

    internal_client.materialize_resume_session = materialize_stable
    internal_client.apply_materialized_options = apply_stable

    logger.info(
        "[SDK patch] Stable-config-dir resume installed "
        "(chrome native-host manifest churn / per-message reauth fix)"
    )
