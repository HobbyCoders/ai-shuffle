"""
Resolve the ``claude`` CLI binary this deployment actually runs.

Two callers share this resolver:

  * omni's ``DelegatedLoop`` (via ``app/core/provider/harness_adapter.py``),
    which SPAWNS the CLI for the delegated Claude leg. It prefers a ``claude``
    on PATH (dev machines with a global install) and falls back to the bundled
    copy this module locates.
  * the deploy-time interrupt patcher (``app/core/cli_interrupt_patch.py``),
    which byte-patches that same bundled binary so a user "Stop" spares
    in-flight background sub-agents.

The bundled copy is the Docker fallback: in the image the CLI is not on PATH —
only the ~257MB Bun binary shipped inside the installed ``claude_agent_sdk``
package files exists. Keeping the resolver here (rather than inside the
interrupt patcher) means CLI resolution does not depend on the opt-in patch
module.
"""

import platform
from pathlib import Path


def resolve_bundled_cli_path() -> Path | None:
    """Locate the bundled ``claude`` binary, or None if it is absent.

    Mirrors the SDK's own ``_find_bundled_cli`` resolver: the ``_bundled``
    directory sits three levels up from ``subprocess_cli.py`` inside the
    installed ``claude_agent_sdk`` package. The binary is named ``claude.exe``
    on Windows and ``claude`` elsewhere, so mirror that too.
    """
    from claude_agent_sdk._internal.transport import subprocess_cli

    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    bundled = (
        Path(subprocess_cli.__file__).parent.parent.parent / "_bundled" / cli_name
    )
    if bundled.exists() and bundled.is_file():
        return bundled
    return None
