# Windows sandbox design

Status: proposal only; no Windows sandbox is implemented by this change.
The Android emulator's Windows backend is independent of this design.

## Goals and threat model

Run untrusted code-mode Python and explicitly granted folder commands without
letting them read worker credentials, unrelated files, other sessions or host
services. Treat generated code, dependencies and child processes as hostile.
The trusted broker and permission-gated host tools remain outside the boundary;
a successful sandbox escape through an overly broad broker is still a failure.
Do not claim that the ordinary agent Bash tool is confined merely because its
code-mode caller is confined. Kernel/administrator compromise and side-channel
resistance are outside the initial guarantee.

Require explicit capabilities, bounded CPU/memory/time/output, cancellation that
reaps descendants, and an observable isolation state. No privilege escalation,
Windows feature installation, reboot, firewall change or security-policy change
may happen implicitly. The emulator's hardware virtualization is not a sandbox
for agent code.

## What bubblewrap provides today

There are two different consumers, not one universal agent shell sandbox:

- `fluid/fluid/codemode/os_sandbox.py`, used by
  `fluid/fluid/codemode/subprocess_kernel.py`, launches the Python code-mode
  child with new user, PID, network, IPC, UTS and cgroup namespaces. Its root is
  empty except for read-only Python/runtime files, minimal devices and a private
  `/tmp` and `/proc`. Home, workspace and worker data are absent. Calls to trusted
  tools travel over stdio, not a socket. Network is normally absent;
  `allow_network=True` explicitly shares the host network. The parent-death and
  session flags bind process lifetime to the worker. There is no seccomp filter
  for this consumer; Python audit/import guards are defence in depth, not the
  OS boundary. Address-space limits are applied by the child, not by bwrap.
- `app/core/chat_shell.py` runs **private folder-grant shell commands** with
  only the granted directory at `/work`, bound from an already-open descriptor.
  Read-only grants are mounted read-only. It has private namespaces and temp,
  read-only runtime files, no network, CPU/address-space/file/process/FD/time
  limits, and a seccomp filter blocking socket escape routes, io_uring and
  keyring access. It refuses suspicious hard links and cleans setuid/setgid
  bits. `app/core/chat_files.py` separately uses descriptor-relative file access;
  that protection must not be replaced with a string-prefix path check.

These guarantees depend on a trusted Linux kernel and permitted user namespaces.
They do not confine the server, permission-granted host calls, ordinary native
shell tools or delegated provider CLIs.

Code mode has `auto`, `required` and `off` modes. On Linux, `auto` logs a warning
and falls back to in-process guards if the bwrap self-test fails. On Windows,
`auto` returns an unsandboxed plan **without logging a warning**. The
`auto` + `allow_fs` path also falls back without a warning. `required` refuses
execution when the sandbox is unavailable. `off` is explicitly unsandboxed. Private folder shell execution has **no fallback**:
non-Linux hosts or failed preconditions refuse commands. These differences must
remain visible; neither a Python audit hook nor a process group is an OS sandbox.

## Options

| Option | Security guarantees and limits | User setup | Performance | Shell / file-tool compatibility |
| --- | --- | --- | --- | --- |
| AppContainer / LPAC | Kernel-enforced lowbox token, capability/ACL checks and network isolation when no network capability is granted. LPAC removes broad ALL APPLICATION PACKAGES access. Not a private filesystem or VM: anything accessible to its token/capabilities remains visible. Registry, named pipes, COM, inherited handles and broker requests need explicit review. Pair with a Job object and a hardened runtime tree. | No optional VM feature or reboot for supported Windows versions. Requires a native launcher, private package/profile lifecycle and tightly scoped runtime ACLs; LPAC support must be probed, never silently downgraded. | Near-native CPU and fast process startup; per-session profile/ACL provisioning adds small costs. | Good fit for the stdlib-only Python child and stdio RPC. PowerShell, Git Bash and build chains may depend on profile/COM/services denied by LPAC. Arbitrary folder grants need brokered handle-based tools, not broad user-directory ACL edits. |
| Job objects + restricted tokens | Jobs bound descendants and resource use; restricted tokens can remove privileges and deny identities. Alone these do not create network or filesystem isolation: same-user/public ACLs and host IPC can remain accessible. A Job is lifecycle containment, not an adequate confidentiality boundary. | Native helper, token policy and careful suspended creation/assignment; generally no reboot. Elevation must not be required for normal sessions. | Near-native; least overhead. | Broad native executable compatibility, but cannot honestly provide bwrap-equivalent file/network guarantees without an additional access-control boundary. Useful as a component, not the default sandbox. |
| Windows Sandbox | Disposable Hyper-V VM with a separate kernel and ephemeral state. Disable network/clipboard and map only explicitly allowed folders, read-only by default. Writable mappings deliberately expose host data; configuration mistakes weaken isolation. | Supported Windows editions, virtualization/feature enablement, often admin approval/reboot; not available on Home editions. Automation needs a guest runner and a secure results protocol. | VM boot, memory and image overhead; unsuitable for every short Python cell unless pooled, which introduces state-lifetime risks. | Native Windows tools work inside the guest after provisioning. Host paths are not guest paths; no transparent existing folder descriptor semantics. Good optional high-isolation task runner, poor transparent kernel replacement. |
| WSL2 + bubblewrap | VM boundary plus Linux namespace isolation if the actual distro/kernel permits user namespaces. WSL interoperability, automounts, Windows executable launch, sockets and shared paths must be disabled or excluded from the workload. WSL by itself is not a sandbox for the logged-in user. | Enable/install WSL2, a maintained distro, Linux runtime and bwrap; virtualization, downloads and possibly admin/reboot. Verify policies with adversarial self-tests. | Linux process startup is fast in a warm VM. Cold boot and cross-filesystem I/O cost more; keep execution on the Linux filesystem. | Best Linux shell compatibility, but Windows drive letters, symlinks, permissions and handles require an explicit adapter/copy protocol. Use a Linux worker inside WSL, not an unreviewed `wsl.exe bash -c` wrapper for Windows grants. |
| Docker / Hyper-V isolation | Linux containers share the VM kernel; process-isolated Windows containers share the host kernel. Hyper-V-isolated containers/VMs add a separate-kernel boundary. Never expose the Docker socket, host credentials or broad mounts to the workload. | Docker Desktop/runtime or a managed Hyper-V VM, virtualization, large images, admin/feature setup and possible commercial licensing. Windows image/host-version constraints complicate support. | Warm containers are reasonably fast; VM/image pulls and disk/memory footprint are substantial. | Linux images support Linux shell tools, Windows images need their own runtime. File mounts still require grant enforcement and host/guest path translation. Good opt-in infrastructure backend, not a zero-setup desktop default. |

## Recommendation

Build a **native LPAC code-mode backend with a Job object and a narrow stdio
broker** as the default Windows direction. First prove that the packaged Python
runtime can start in LPAC with no network, no inherited ambient handles and no
user-profile/data-directory access. This is a design recommendation, not a claim
that arbitrary Win32 programs already work in LPAC. If the feasibility gates
fail, keep Windows `required` unavailable rather than substituting restricted
tokens alone or weakening Linux policy.

Use a dedicated runtime copy with ACLs for a per-session lowbox identity; do not
change ACLs recursively on a user's Python installation or project. Launch
suspended, attach to a non-breakaway Job with kill-on-close and CPU/memory/process
limits, then resume. Inherit only explicit stdio handles through a handle list;
clear the environment and block interactive UI/clipboard access as applicable.
Reject unsupported nested-job situations. Avoid network capabilities, broad
package capabilities and ambient credential access. Validate named-pipe and
loopback denial independently of Internet denial.

Keep file operations brokered with a Windows handle-based grant implementation:
open handles with suitable sharing, verify volume/file identity, reject reparse
points/junction escapes, alternate data streams, device/UNC namespaces and
outside hard links, and enforce read-only writes at the broker. Define rename
and revocation semantics before enabling writes. Do not grant LPAC access to
arbitrary host folders merely to make shell tools work. Start private folder
shell support later with a restricted runtime or copied workspace and explicit
copy-back conflict checks; refusal is better than an unsafe transparent mapping.

Offer WSL2 + bwrap as an explicitly configured **Linux worker** option for users
who need POSIX tools. Consider Windows Sandbox / Hyper-V only as a later opt-in
high-isolation executor for full applications. Do not make Docker Desktop a
mandatory dependency, install it automatically, or add paid CI jobs.

## Phases, tests and acceptance criteria

1. **Expose capability and policy (before a new backend).** Define backend,
   active/available, configured mode, failure code, human-readable reason and
   scope fields. Surface these to the owner and execution result metadata.
   Test `auto`/`required`/`off`, missing runtime, failed self-test and restart.
   Acceptance: no UI, agent response or log labels an unsandboxed run isolated;
   `required` never spawns on failure. Folder shell continues to fail closed.
2. **LPAC feasibility spike, explicitly opt-in.** Build a small audited native
   launcher; prove stdio RPC with the packaged Python and Windows security APIs.
   Test on supported Home and Pro editions with standard-user accounts and
   virtualized features disabled, without weakening machine-wide policy.
   Acceptance: read/write canary files outside the runtime/private temp, worker
   secrets in environment, registry secrets, external and loopback network,
   named pipes and sibling-process handles are all inaccessible. Required
   runtime reads and broker calls succeed. Record actual OS/build support.
3. **Harden code mode.** Add per-session identity/temp cleanup, handle allowlists,
   Job limits, crash recovery and parent-death cleanup; cache only successful
   capability probes for the relevant runtime/policy revision. Use malicious
   native children as well as Python guard-bypass attempts so tests exercise
   the OS boundary. Test process-spawn floods, memory/CPU/output exhaustion,
   cancellation during spawn, parent crash and PID reuse. Acceptance: descendants
   cannot break away; cancellation completes within a documented bound; no
   cross-session state or leaked grants survive. Compare median/p95 startup and
   RPC latency to unsandboxed execution with an agreed benchmark budget.
4. **Brokered Windows folder tools.** Implement handle-relative grant semantics
   and revocation independently of shell support. Adversarially race renames,
   junctions, symlinks and hard links; cover case folding, long/Unicode paths,
   drive/UNC/device paths and read-only grants. Acceptance: no outside canary can
   be read or changed, including after grant root replacement or revocation.
5. **Restricted shell and rollout.** Inventory supported PowerShell/Git Bash
   commands; explicitly document unsupported CLIs. Test approved copied-workspace
   or brokered paths, deletion/rename conflicts and bounded copy-back. Acceptance:
   the same escape suite passes for every supported shell, failures remain
   actionable and standard-user install/uninstall leaves no broad ACLs/profiles.
   Ship behind opt-in until reviewed, then consider an `auto` default. A backend
   error must never trigger an invisible downgrade.

Unit tests simulate feature/token/job/ACL failures on all development platforms;
real acceptance needs owner-approved Windows runs and security review. Keep Linux
bwrap regression tests and cross-backend broker contracts. No Windows known-failure
exclusions and no new paid hosted runners. A green mocked test suite is not proof
of LPAC confinement.

## What Windows users should see today

No sandbox implementation or mode-default change is included in this proposal.
The existing folder shell already refuses on Windows; keep that refusal and
show its reason directly: **"Isolated folder commands are unavailable on this
Windows worker. No command was run. Use a Linux worker with bubblewrap."**
Do not retry it through ordinary Bash or a less restrictive grant.

Code-mode `required` already refuses when bwrap is unavailable. On Windows,
`auto` currently falls back without even a log warning; `auto` + `allow_fs`
also has no warning. This silent fallback is a disclosure gap, not a sandbox
guarantee. The first implementation phase should show a persistent session/settings status:
**"OS sandbox unavailable on Windows. Code mode uses in-process guards only;
permission-granted host tools still run on the worker."** Display the reason,
mode and scope, and offer a supported Linux worker or `required` (refuse unsafe
execution), not a button claiming to enable bwrap on Windows. `off` must read
**"OS sandbox disabled"**, not "unavailable". Runtime fallback must emit a visible
warning and tool-result metadata, not only a log line. Track this disclosure work
as pending implementation; this design does not pretend it is already shipped.
