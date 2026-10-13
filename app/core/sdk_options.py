"""Single seam for building SDK options for a session spawn (v2 core, Phase 2).

Every spawn path repeated the same pair of steps: turn a profile into
``AgentOptions`` via :func:`app.core.query_options.build_options_from_profile`,
then (usually) wire plugin + DB hooks onto it via
:func:`app.core.hook_service.attach_hooks_to_options` (the #567 seam). This
module deduplicates that pair behind one call so the historical call sites — and
the Phase-2 ``SessionRuntime`` — share one place to build options.

This is a PURE factoring: the options returned are byte-for-byte identical to
what each site built inline. ``build_options_from_profile`` still does all the
real work; the callers that need bespoke control flow (a try/except that yields
an error frame, build timing, a late hook-attach carrying an stderr callback)
keep that logic at the call site and use this seam only for the shared build.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.core.agent_types import AgentOptions

from app.core.query_options import build_options_from_profile

# Sentinel so ``hook_project_id=None`` (a real, meaningful value — "attach hooks
# with no project") is distinguishable from "don't attach hooks here".
_UNSET: Any = object()


def build_session_options(
    *,
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    resume_session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    hook_project_id: Any = _UNSET,
    stderr_callback: Any = None,
) -> Tuple[AgentOptions, Optional[Dict[str, Any]]]:
    """Build ``AgentOptions`` for a session, optionally attaching hooks.

    Args:
        profile, project, overrides, resume_session_id, api_user_id, session_id:
            passed straight through to ``build_options_from_profile`` (identical
            output to calling it directly).
        hook_project_id: when provided (anything other than the ``_UNSET``
            sentinel — including ``None``), plugin + DB hooks are wired onto the
            options via ``attach_hooks_to_options(project_id=hook_project_id,
            stderr_callback=stderr_callback)`` immediately after the build. Sites
            that attach hooks separately (e.g. after setting ``can_use_tool``, or
            outside a build try/except) leave this unset and attach themselves.
        stderr_callback: forwarded to ``attach_hooks_to_options`` only when hooks
            are attached here; ``None`` is byte-for-byte equivalent to omitting
            it.

    Returns the ``(options, agents_dict)`` tuple from
    ``build_options_from_profile``.
    """
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_session_id,
        api_user_id=api_user_id,
        session_id=session_id,
    )
    if hook_project_id is not _UNSET:
        from app.core.hook_service import attach_hooks_to_options

        attach_hooks_to_options(
            options, project_id=hook_project_id, stderr_callback=stderr_callback
        )
    return options, agents_dict
