# Migrating existing installs to the worker network

## Single install

Nothing to do. On the first start after upgrading, an install whose database
already served a user (onboarding complete, or any session on file) becomes a
network of one: a device identity is created under `.credential-keys`, the
`network_state` row is written, every session, automation, connector and
artifact this install ran is stamped with its device id, and the local
attachment bytes get their content hashes. Settings > Devices shows the new
device with its fingerprint and offers to create a recovery key.

Each browser imports nothing: UI state already lived in `user_preferences`
and keeps its keys.

## Shared-database cluster

Two or more installs sharing one PostgreSQL must be cut over together to
independent databases. Automatic network enrollment is disabled when the legacy
registry contains multiple installs. A network database cannot be opened with
another worker's device identity.

1. **Back up and rehearse first.** Take a PostgreSQL backup and a matching backup
   of every worker's persistent data, especially `.credential-keys`, attachments,
   artifacts and workspaces. Retain the previous image/APK versions. Verify a
   restore in isolated staging; never let cloned identities contact the live
   network. An application-image rollback alone does not roll back the database.
2. **Quiesce the whole cluster.** Pause automations/connectors, drain queued and
   running turns, block new UI/API writes, then stop every worker. Do not leave a
   legacy peer writing while another worker enrolls. The export refuses undrained
   turns and uses one repeatable-read PostgreSQL snapshot.
3. **Export each non-source worker**, using its existing `INSTANCE_ID` against
   the stopped shared database:
   ```sh
   python -m scripts.network_migrate export --instance-id <existing-id> --out lab-sessions.json
   ```
   Retain the bundles with the backups. They include transcripts, queued prompts,
   connector session routes, upload bytes, SDK/fluid executing state, checkouts and
   artifact metadata; they are sensitive data, not encrypted network envelopes.
4. **Choose the source**, normally the worker with the most local files. Upgrade
   it alone, keeping its identity and data directory. Its database must now be
   private. Explicitly choose **Create network** in onboarding/Settings; a shared
   legacy registry is never converted automatically. Keep jobs paused.
5. **Move each other worker to a fresh PostgreSQL database.** Preserve its data
   directory and existing `INSTANCE_ID`, upgrade it, and join the source's network.
   Wait for snapshot hydration to complete. The join carries its legacy ID so
   session and resource ownership can be mapped to its new device identity.
6. **Restore executing state on each new owner**:
   ```sh
   python -m scripts.network_migrate import --bundle lab-sessions.json --dry-run
   python -m scripts.network_migrate import --bundle lab-sessions.json
   ```
   Dry-run executes the import and rolls back, including constraint checks.
   Re-imports are idempotent. Existing catalog mirrors retain shared metadata and
   receive missing local resume fields. Copy required artifact/workspace files;
   metadata alone is not the file content. Reconnect non-portable OAuth bundles.
7. **Verify before resuming**: compare source and destination sessions, messages,
   queued prompts, connector routes and upload bytes; check SDK resume handles,
   artifact reads, local and remote Git operations, and session execution on each
   owner. Every device must show Ready. Explicitly confirm automation/connector
   ownership and credentials before enabling scheduled/external work.
8. **Cut over clients only after the worker/hub smoke tests pass.** Preserve
   `INSTANCE_ID` during this migration: local checkout and execution records still
   use it. Retire legacy `CLUSTER_SECRET` configuration only when all workers have
   migrated and the rollback window is closed.

### Rollback

Stop all new workers and external writers. Restore the matched pre-cutover
PostgreSQL and persistent-data backups, restore the previous application images,
and only then reconnect the old shared-database cluster. Simply pointing a joined
worker back at the modified database is not a rollback. Preserve any new private
DBs separately: post-cutover changes are not automatically merged into the backup.

## Credentials

The source decrypts provider credentials with its data key and re-encrypts
them as network records; the receiving worker re-encrypts them under its own
data key. Windows-bound wrapping files are never copied. OAuth bundles that
cannot be reused elsewhere stay marked for reconnection on the worker that
uses them.
