"""Code-as-actions (code-mode) — the model writes Python that calls tools.

Instead of dozens of JSON tool schemas, the model gets a tiny surface
(``run_python``, ``list_tools``, ``search_tools``, ``read_tool_docs``) and
writes Python against typed stubs projected from the ``ToolRegistry``. Each
in-kernel tool call RPCs back to the host and is permission-gated there.

Public seams:
- ``Kernel`` protocol + ``SubprocessKernel`` (v1, a soft boundary — see its
  docstring; a container/gVisor kernel is a roadmap item behind the same API);
- ``Projection`` — tools → typed stub sources + a discovery catalog;
- ``HostBridge`` — routes ``on_host_call`` RPCs through hooks/permissions/dispatch;
- ``ResultRef`` — large results stay in-kernel behind a sliceable handle.
"""

from loom.codemode.bridge import HostBridge, HostCallBridge, OnPermission
from loom.codemode.kernel import (
    HostCall,
    HostReply,
    Kernel,
    KernelResult,
)
from loom.codemode.projection import Projection, func_name, stub_source
from loom.codemode.subprocess_kernel import KernelConfig, SubprocessKernel
from loom.codemode.truncate import DEFAULT_LIMIT, exceeds, to_result_ref
from loom.codemode.vfs import (
    DEFAULT_ALLOWLIST,
    HostCallDenied,
    ResultRef,
    ToolError,
    make_guarded_import,
    make_namespace,
)

__all__ = [
    "DEFAULT_ALLOWLIST",
    "DEFAULT_LIMIT",
    "HostBridge",
    "HostCall",
    "HostCallBridge",
    "HostCallDenied",
    "HostReply",
    "Kernel",
    "KernelConfig",
    "KernelResult",
    "OnPermission",
    "Projection",
    "ResultRef",
    "SubprocessKernel",
    "ToolError",
    "exceeds",
    "func_name",
    "make_guarded_import",
    "make_namespace",
    "stub_source",
    "to_result_ref",
]
