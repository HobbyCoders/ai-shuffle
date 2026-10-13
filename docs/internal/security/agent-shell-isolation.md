# Agent shell isolation (#1957)

## Current boundary

Bash, Monitor and delegated Claude CLI children run as the worker's OS account.
An agent can invoke PowerShell from those shells on Windows; PowerShell is not a
separate isolation boundary. POSIX mode 0600/0700, Windows user-only ACLs and
DPAPI protect against *other* unprivileged accounts, not the account running the
app. An agent with a shell can still read the install's database descriptor and
keys, decrypt same-user DPAPI files, or run a database client. Environment
filtering and owner-only HTTP checks do not prevent that.

Do not treat a shell-enabled agent as unable to access owner data. A malicious
repository, web page or tool response can try to exploit this access. The
terminal API's owner checks restrict the API, not the OS account's authority.

That is the default everywhere. The Docker worker has an opt-in account
split, `AGENT_ISOLATION=on` (see [Docker split](#docker-split-opt-in-agent_isolationon)),
under which agent processes get "permission denied" on the install's
descriptor, keys and PostgreSQL files. It is off by default. The Windows
split below is still pending, and the desktop apps keep the single-account
boundary described here.

## Storage and process paths reviewed

| Material | Writer / consumer | Current protection and remaining exposure |
| --- | --- | --- |
| `DATA_DIR/managed-postgres.json`, `database-mode.json`, `postgres/` | `docker/container_runtime.py`; descriptor read by `Settings.get_database_connection()` | Container supervisor creates private files and cluster directories. The descriptor contains the app DB password. The same account can read it and the local administrative socket; changing only the DB role is insufficient. |
| Desktop PostgreSQL, descriptor and saved connection | `desktop/src/main/postgres.ts`, `worker.ts`, `settings.ts`, `private-file.ts` | Private directory ACLs precede atomic credential writes. Electron, PostgreSQL and the Python worker still run as the desktop user. Saved desktop settings also contain the connection, so protecting only the descriptor would miss a copy. |
| `.credential-keys/` and `.managed-connection/` | `credential_keys.py`, `managed_credentials.py`, `network/identity.py` | Private POSIX directories/files; Windows key payloads use current-user DPAPI. Python now installs current-user-only protected ACLs before writing bytes and on existing Windows keys/directories when used. |
| `preview-access.key` | `preview_access.py` | Atomic publication; now explicit 0600 or protected Windows DACL before writing and when reused. Same-user shells can still mint preview grants. |
| `.env` / desktop worker config | `config.py`, `desktop/src/main/worker.ts` | Settings read the canonical config selected by `AI_SHUFFLE_ENV_FILE`; worktrees do not need copies. Desktop config writes already use private storage. User-managed source `.env` files are not rewritten by this patch. A readable config path remains accessible to a same-user shell. |
| Owner auth sessions and provider credentials | `owner_auth.py`, DB credential stores | Session token hashes and encrypted credentials are stored in PostgreSQL; there is no independent OS access boundary once the DB and local key material are readable. Desktop lifecycle/attach/listener tokens are also in the server environment or private launcher state. |

Fluid Bash and delegated CLI children use `fluid.tools.child_env`. Monitor's two
entry points share `_spawn_background()` and `process_tree.child_env()`; the
latter previously copied the server environment unchanged and now uses the
same scrubber. DSN and additional authentication/key-shaped names are removed,
and passthrough cannot restore named app secrets or DB settings. The parent's
environment is unchanged. Provider/GitHub keys already allowed for tools and
explicit owner-configured child overlays remain intentional handovers, not a
promise that arbitrary secrets are detected.

Owner terminals already use the stricter `terminals.shell_environment()` and
POSIX PTYs; Windows terminals are currently unsupported. Hooks already scrub
inherited environments. Native external MCP processes use the MCP SDK's stdio
transport with server-specific environment overlays; delegated MCP processes
are started by the already-scrubbed CLI. `mcp_config.py` supplies a curated
browser environment rather than the server's entire environment. MCP servers
still share the worker's OS identity and must be included in a future split.

## Docker split (opt-in, `AGENT_ISOLATION=on`)

Implemented in `docker/agent_isolation.py`, `fluid/fluid/tools/agent_runner.py`
and `app.core.private_storage.service_files`, following the recommendation this
section used to hold. Off by default; with the setting off the image runs
exactly as before (same commands, same ownership, no new process).

**Accounts.** The image has a `shuffle` service account (uid/gid 64000, change
with `AGENT_ISOLATION_SERVICE_UID` / `AGENT_ISOLATION_SERVICE_GID`): no home, no
login shell, no sudo, and PUID's account is in none of its groups. PUID/PGID
(`appuser`) stays the agent account, so workspace ownership and host shares do
not change. The agent account has no sudo, setuid helper, Docker socket or
service-group membership.

**What the service account owns.** In `DATA_DIR`: `managed-postgres.json`,
`database-mode.json`, `.credential-keys/`, `.managed-connection/`,
`preview-access.key`, `postgres/` and `application-updates/` (update
directories get `o+x` but no `o+r`, so the app can run verified code from
them). `DATA_DIR` itself becomes `shuffle:PGID` with mode `1770` plus its
previous bits: the app and agents still create files there, the sticky bit
keeps them from renaming or deleting the service's entries. Everything else in
`DATA_DIR` stays PUID's. While the mode is on, root also owns the image code
the app loads (`/app/app`, `/app/scripts`), so an agent cannot edit it.

**Processes.** PostgreSQL and its tools run as the service account (real,
effective and saved ids). The supervisor and the app run with PUID as real
and effective uid and the service account kept only as the *saved* uid/gid,
with no supplementary groups. The app opens protected files with a per-thread
file-system uid (`service_files()`, synchronous blocks only). Every program it
executes loses the saved ids, because `execve` resets them. A PUID process
cannot `ptrace` the app or read its `/proc/<pid>/environ` because their saved
ids differ.

**The spawn runner.** Bash, Monitor shells, hooks, native MCP servers, the
delegated `claude` CLI (and with it its MCP bridge) and owner terminals start
through `agent_runner.py`, run as `python -I -S`. It is a standard-library
script with no imports from the app. It refuses (exit 126) unless the
environment holds no app secret (the same names as `child_env`) and the
working directory resolves inside an approved root. The roots are the
workspace, the agent's home, `DATA_DIR`, `/tmp` and `AGENT_ISOLATION_EXTRA_DIRS`.
It then:

- drops supplementary groups;
- settles real, effective and saved uid and gid on PUID/PGID;
- sets `no_new_privs`;
- closes every inherited descriptor above stderr;
- execs the command.

It is not a separate broker process: the app's own `Popen` runs it, so
cancellation, process-group kill, the process ledger's orphan recovery and
background-task accounting work on agent children as before. The ledger
matches the command line after the runner's exec. Owner terminals run as the
agent account. There is no service-account maintenance shell.

**Migration.** Root does it on every start, before dropping privileges. It
writes `DATA_DIR/agent-isolation.json` first: a root-owned record of each
entry's previous uid, gid and mode. Then it changes ownership of entries PUID
owns. It never creates, initializes, regenerates or deletes anything, and it
does not follow symlinks. Once the record says "enabled", files PUID has since
placed in a protected tree are logged, not adopted. With the setting off, the
next start restores the recorded uid/gid/mode and removes the record. Both
directions are idempotent and resume after an interruption. If enabling fails,
the start undoes the partial change, logs it loudly and runs unisolated, as
with the setting off.

**Deviations from the recommendation and remaining exposure**

- PostgreSQL runs as the service account, not a separate PostgreSQL account.
  The service account already holds the descriptor's password, so a second
  account would protect nothing more from agents.
- The agent can still signal the app, supervisor, PostgreSQL and Chromium
  because they share its real uid. That is denial of service only.
- The update supervisor hand-over is off while the mode is on: the image's own
  supervisor runs. Updates still apply to the app.
- Update directories built before the mode was turned on are not re-verified
  when they are taken over.
- What an agent read before the mode was on (database password, credential
  key, preview key) may have been copied. Rotate those after turning it on if
  that matters.
- Not protected: a host-mounted `/app/.env` that PUID can write, `~/.fluid`,
  provider CLI sign-ins and the workspace. These are the agent's by design.
- A `cloudflared` tunnel token in the container environment reaches that
  PUID child.
- Owner child-environment overlays named like DB settings (`DATABASE_URL`,
  `PG*`) are dropped for agents.
- An agent can pre-create a protected file that does not exist yet. That only
  blocks the app (denial of service). The migration logs it and does not adopt it.
- On the host, the data volume's root shows uid 64000 while the mode is on.
- `verify_container_postgres.py`'s `--user 99:100` SQL and `pg_controldata`
  helpers stop working while the mode is on, as intended.

**Branch previews.** A preview can no longer reuse the install's descriptor or
preview key. Run it against disposable data instead: a fresh `DATA_DIR` and
`HOME`, `LOCAL_MODE=1` and a throwaway PostgreSQL, as in the preview-branch
skill. Do not loosen the modes above to bring the old workflow back.

**Backups.** `scripts/backup.sh` run as root stays root while the mode is on
and streams the dump (`container_runtime.py dump --file -`), as
`tools/backup-db.sh` already does. Run backups from the host with
`docker exec` as root, never from an agent shell.

## Recommended Windows split (not implemented)

Run the backend and managed PostgreSQL under a dedicated non-administrator
service identity with private data/settings ACLs. Keep the Electron UI under
the interactive desktop user and use authenticated, narrowly scoped IPC to the
service. Spawn agents under the interactive user or a second dedicated agent
account with no access to service state. A restricted token or Job Object under
the *same* SID is not a substitute for separating that SID's file access.

Migrating the service identity requires an explicit DPAPI migration: the old
account must unwrap and re-protect the existing keys for the service identity
while agents are stopped, without plaintext files or key regeneration. Provider
CLI profiles, GitHub login and project permissions remain with the agent account.
Computer-use/browser helpers that need the interactive desktop stay in that
session and receive narrow capabilities over IPC, not service DB credentials.
Review service installation/elevation, update ownership, uninstall/backup access
and Windows network-share behavior before enabling this mode.

## What breaks and needs an owner decision

- Builds that use a host secret from Monitor's inherited environment must use
  an explicit approved child configuration or a private access grant. Passing an
  app secret through `FLUID_CHILD_ENV_PASSTHROUGH` no longer works.
- A real user split changes project file ownership, Git safe-directory rules,
  PATH/HOME, CLI sign-ins, SSH agents, browser profiles and permission to run local
  development servers. Provision an agent home rather than sharing the service
  home. Do not copy the service's config into project worktrees.
- Existing branch previews rely on the install's descriptor/key and canonical
  config reference. With the Docker split on they must use disposable preview
  data (see Branch previews above). Do not weaken data ACLs to preserve the old
  preview workflow.
- Backup, restore, migration and update operations must remain service-owned;
  agent build/test jobs cannot operate directly on the live database anymore.
- The owner chooses the container migration by setting `AGENT_ISOLATION=on`.
  Whether Windows service installation is acceptable is still open. Until a
  worker runs a split, owner-only routes are not secure against a compromised
  shell-enabled agent running as the app's own user.

## Acceptance tests for the split

Use disposable installs and fixture credentials only. On both platforms, have
real Bash/PowerShell, Monitor, delegated CLI and MCP children attempt reads of
all sensitive paths above (including backups and desktop settings). Each must
get an OS permission denial; service key loading, DB startup and owner sign-in
must still work. Check that children cannot read service environments or handles,
connect to trusted admin sockets, modify service code, or elevate through the
spawn broker. Exercise builds, owner terminals, previews, cancellation, updates,
restarts and rollback. Do not test by reading production credentials or inserting
an owner session in the live database.

The focused tests for this patch generate keys through the real Python writers,
inspect Windows ACL metadata and POSIX modes, and run disposable Bash,
PowerShell and Monitor children with sentinel environment variables. They do
not demonstrate denial to same-user agents; that requires the split above.

For the Docker split:

- `tests/core/test_agent_isolation_root.py` needs root; CI runs it with sudo.
  It migrates a fixture `DATA_DIR` for real. A process holding the app's ids
  then runs the real Bash tool handler and Monitor spawner through the runner,
  and each of the six entries gets "Permission denied". Its children have no
  groups and `NoNewPrivs` set, the app still reads through `service_files()`,
  and cancellation and process-tree kill reach the agent's grandchildren.
  Rolling back restores the exact ownership.
- `scripts/ci/verify_agent_isolation.py` repeats the check on the built image
  (`container-postgres.yml`). It runs a disposable install off, then on, then
  restarted, then off again. It checks the same cluster and data, PostgreSQL
  running as the service account, and a root dump.
- `tests/core/test_agent_isolation.py` covers the migration plan and the
  mode-off behaviour without root.
- `fluid/tests/unit/test_agent_runner.py` covers the runner's refusals.
