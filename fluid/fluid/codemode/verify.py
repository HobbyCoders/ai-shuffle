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
is verified once, not per session. A kernel that cannot run at all
(``code_mode_os_sandbox=required`` on a host without the sandbox) is a host
condition, not a verdict: nothing is recorded, and the skills stay stale until
a pass whose kernel starts.

A mount pays at most ``MOUNT_VERIFY_BUDGET`` seconds of verification: skills
not reached inside it stay unverified (and unmounted) until the next mount,
which starts where the verdicts stopped, so one slow skill never delays the
first ``run_python`` by a whole library's worth of timeouts.
"""

from __future__ import annotations

import time

from fluid.codemode.kernel import HostReply
from fluid.codemode.os_sandbox import OsSandboxUnavailable
from fluid.codemode.skills import SkillStore
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel

DEFAULT_TIMEOUT = 10.0  # per-skill wall clock (import + selftest)
MOUNT_VERIFY_BUDGET = 30.0  # total wall clock one mount spends verifying
# Under this much budget left, the next skill would only ever time out.
_MIN_SLICE = 1.0


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
    budget: float | None = None,
) -> dict[str, str | None]:
    """Verify ``names`` (default: all of ``sources``) in one throwaway kernel.

    Returns ``{name: error-or-None}``. All of ``sources`` is mounted so skills
    can import each other; only ``names`` are actually imported/self-tested.
    With a ``budget`` (seconds for the whole call) each skill gets the
    smaller of ``timeout`` and what is left, and skills the budget never
    reaches are absent from the result rather than failed.

    Raises :class:`OsSandboxUnavailable` when the kernel cannot start (the
    ``required`` sandbox is unavailable): no skill was run, so none has a
    verdict. A kernel that goes down that way mid-pass ends the pass; the
    skill it was on and the rest are left out, as if the budget ran out.
    """
    targets = list(sources) if names is None else [n for n in names if n in sources]
    if not targets:
        return {}
    deadline = None if budget is None else time.monotonic() + budget
    kernel = SubprocessKernel(config or KernelConfig())
    await kernel.start()
    if kernel.refusal:
        raise OsSandboxUnavailable(kernel.refusal)
    results: dict[str, str | None] = {}
    try:
        await kernel.set_vfs({}, sources)
        for name in targets:
            slice_timeout = timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < _MIN_SLICE:
                    break
                slice_timeout = min(timeout, remaining)
            code = (
                f"import skills.{name} as _mod\n"
                "_st = getattr(_mod, '_selftest', None)\n"
                "if callable(_st):\n"
                "    _st()\n"
            )
            result = await kernel.exec(code, timeout=slice_timeout, on_host_call=_no_tools)
            if kernel.refusal:
                break  # the host lost the sandbox; not this skill's fault
            results[name] = _summarize(result.error)
    finally:
        await kernel.stop()
    return results


async def verify_store(
    store: SkillStore,
    *,
    config: KernelConfig | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    budget: float | None = MOUNT_VERIFY_BUDGET,
) -> dict[str, str | None]:
    """Verify every stale skill in ``store`` and record the verdicts.

    Returns the verdicts for the skills that were (re-)verified; unchanged
    skills keep their cached verdict and are not re-run, and skills the
    ``budget`` did not reach stay stale for the next call.
    """
    sources = store.sources()
    stale = [name for name in sources if store.needs_verification(name)]
    if not stale:
        return {}
    verdicts = await verify_sources(
        sources, stale, config=config, timeout=timeout, budget=budget,
    )
    for name, error in verdicts.items():
        store.mark_verified(name, error)
    return verdicts
