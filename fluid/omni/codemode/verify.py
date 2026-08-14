"""Skill verification — stored knowledge has to prove it still works.

A skill is verified by importing it (module-level code runs, so syntax/import
rot is caught) and, when it defines a ``_selftest()`` callable, running that
too — all inside a throwaway kernel with **no host tools** (any ``tools.*``
call errors), the same guarded builtins as production, and the full skill set
mounted so inter-skill imports resolve. Self-tests must therefore be pure:
exercise the skill's logic on inline fixture data, raise on failure.

Verification runs at two moments:

- **on save** (``save_skill`` / the synthesizer) — a failing skill is rejected
  with the error, so bad knowledge never lands;
- **on mount** (``CodeModeSession._ensure_kernel``) — stale skills (source
  changed since last verdict, e.g. edited on disk) are re-verified; broken
  ones are excluded from the kernel mount and flagged in ``list_tools``.

Verdicts are cached in the store's meta by file mtime, so an unchanged skill
is verified once, not per session.
"""

from __future__ import annotations

from omni.codemode.kernel import HostReply
from omni.codemode.skills import SkillStore
from omni.codemode.subprocess_kernel import KernelConfig, SubprocessKernel

DEFAULT_TIMEOUT = 10.0  # per-skill wall clock (import + selftest)


async def _no_tools(name: str, kwargs: dict) -> HostReply:
    return HostReply(
        error=(
            f"tool {name!r} is unavailable during skill verification — "
            "_selftest() must be pure (inline fixture data, no host calls)"
        )
    )


def _summarize(error: str | None) -> str | None:
    """Last meaningful line of a traceback, or None."""
    if not error:
        return None
    lines = [ln for ln in error.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else error.strip()


async def verify_sources(
    sources: dict[str, str],
    names: list[str] | None = None,
    *,
    config: KernelConfig | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, str | None]:
    """Verify ``names`` (default: all of ``sources``) in one throwaway kernel.

    Returns ``{name: error-or-None}``. All of ``sources`` is mounted so skills
    can import each other; only ``names`` are actually imported/self-tested.
    """
    targets = list(sources) if names is None else [n for n in names if n in sources]
    if not targets:
        return {}
    kernel = SubprocessKernel(config or KernelConfig())
    await kernel.start()
    results: dict[str, str | None] = {}
    try:
        await kernel.set_vfs({}, sources)
        for name in targets:
            code = (
                f"import skills.{name} as _mod\n"
                "_st = getattr(_mod, '_selftest', None)\n"
                "if callable(_st):\n"
                "    _st()\n"
            )
            result = await kernel.exec(code, timeout=timeout, on_host_call=_no_tools)
            results[name] = _summarize(result.error)
    finally:
        await kernel.stop()
    return results


async def verify_store(
    store: SkillStore,
    *,
    config: KernelConfig | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, str | None]:
    """Verify every stale skill in ``store`` and record the verdicts.

    Returns the verdicts for the skills that were (re-)verified; unchanged
    skills keep their cached verdict and are not re-run.
    """
    sources = store.sources()
    stale = [name for name in sources if store.needs_verification(name)]
    if not stale:
        return {}
    verdicts = await verify_sources(sources, stale, config=config, timeout=timeout)
    for name, error in verdicts.items():
        store.mark_verified(name, error)
    return verdicts
