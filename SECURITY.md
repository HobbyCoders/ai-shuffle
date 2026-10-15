# Security

## Agent access and the trust boundary

By default, agent shell commands run as the same operating-system user as AI Shuffle. This applies to Docker and the Windows, macOS and Linux desktop apps. Background commands (Monitor), delegated agent CLIs and local MCP servers are not a separate security boundary.

An agent with shell access can read or change files that the app's user can access, not just files in its project or worktree. This includes the install's database connection details, database files and credential-encryption keys. Possession of the database connection and keys can expose saved credentials and allow changes to application data. File permissions restricted to the app user, or encryption whose key is readable by that user, do not protect against that same user's shell.

**Owner-only API checks are not an operating-system sandbox.** They restrict access through the API; they do not stop a shell-enabled agent from accessing the underlying files or database. Filtering secrets out of a child process's environment does not remove its file access. The code-mode Python sandbox does not sandbox the Bash or Monitor commands it calls through tools. Separate worktrees isolate edits, not privileges.

A malicious repository, web page or tool response can try to persuade an agent to misuse this access. Permission prompts and agent instructions reduce accidental actions but are not a boundary against malicious code once it runs. Treat enabling shell access as trusting the agent and the code it executes with the app user's access.

### Restricting exposure

- Use **Ask** rather than **Bypass** for untrusted work, review commands before approving them, and do not run code you do not trust. Do not enable shell execution where that level of trust is inappropriate.
- Run AI Shuffle without administrator privileges on a dedicated OS account, container or virtual machine. Keep unrelated personal files and credentials outside that account's access. A dedicated account for the whole app limits access to the rest of the machine; it does **not** separate agents from that install's own data.
- For Docker, mount only the project folders and data volumes the worker needs. Do not mount the Docker socket, host root filesystem or unrelated secret directories, and do not run the container privileged. A container limits host exposure only to the extent its mounts and privileges allow; it does not by itself separate agents from the app inside it.
- Give tools only narrowly scoped, revocable credentials. Do not put service credentials or copies of the app's configuration in project folders. If secrets may already have been read, revoke or rotate them; changing permissions afterwards cannot revoke copies.

### Optional Docker isolation

The Docker image includes an **opt-in** `AGENT_ISOLATION=on` mode. It keeps agent processes as the workspace user (`PUID`/`PGID`) while assigning the install's database descriptor, credential keys, preview signing key and PostgreSQL files to a separate service account. See [Docker agent isolation](docs/content/getting-started/install-docker-compose.md#keep-agents-away-from-ai-shuffles-own-keys-optional) for setup, backup and rollback instructions.

**This mode is off by default and is not fail-closed.** It requires the normal root-started container entrypoint; with `--user` it is ignored. If the ownership migration cannot be applied, startup can roll it back and continue without isolation. Check the startup log for `agent isolation is on` after every start before relying on it. Test with a disposable install before enabling it on existing data: working-directory restrictions, database environment variables, previews and maintenance workflows can be affected.

**It is not safe to make this default-on yet, even for new installs.** An invalid service identity, unsupported ownership or a failed migration can leave a running worker unisolated; `--user` bypasses the split. A required/fail-closed startup mode and disposable-image validation of database access, privileged imports, all child-process paths and normal workflows are prerequisites, not implied by the setting. See the [validation procedure](docs/internal/security/agent-shell-isolation.md#validation-before-changing-the-default). Existing installs and new installs therefore keep the opt-in default.

This is not a general-purpose sandbox: workspace files, tool sign-ins and other files readable by the agent account remain accessible. Do not supply readable host-mounted configuration containing app secrets. Turning isolation on does not revoke credentials an agent could have copied earlier.

Desktop installations currently have no equivalent agent/service account split. Windows user-only ACLs and current-user encryption, and macOS/Linux user-only file modes, protect against other users, not an agent running as the app user. Use a dedicated account or disposable VM to limit exposure outside the install, and do not treat owner-only features as protection from a shell-enabled agent inside it.

## Reporting a vulnerability

Report suspected vulnerabilities privately to support@hobbycoders.com. Do not open a public issue with vulnerability details before maintainers have had a chance to investigate.

Include the affected version and update channel, installation type, what happened, and steps to reproduce with synthetic data. Explain the possible impact. Never post secrets in issues, and never send passwords, API keys, access tokens, sign-in codes or private connection details by email. Review any logs or screenshots before sharing them.

If a credential has been exposed, revoke or rotate it with its provider. Deleting it from an issue does not make it safe to use again.

Security reports are handled on a best-effort basis. There is no guaranteed response time or security-update period. For ordinary bugs, see [SUPPORT.md](SUPPORT.md).
