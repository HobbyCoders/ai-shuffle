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

Two or more installs sharing one PostgreSQL become one network of independent
databases. One install is the **source**; every other install gets a fresh
database and joins.

1. **Upgrade every install** to this version. The shared database gains the
   network tables; the first install to boot creates the network of one and
   claims the rows it ran (`instance_id` = its own). Rows written by the other
   installs keep their `instance_id` and stay unowned until that install's
   device joins.
2. **Pick the source**: the install with the most local files (checkouts,
   artifacts). It keeps the shared database as its own private database.
3. **For every other install**, one at a time:
   1. Stop it. Export the sessions it ran, against the shared database:
      ```sh
      python -m scripts.network_migrate export --instance-id <that install's INSTANCE_ID> --out lab-sessions.json
      ```
      (Run this on the source host, or anywhere with the shared `DATABASE_URL`.)
   2. Point the install at a **fresh PostgreSQL** (a new database on the same
      server is fine; the desktop app's bundled PostgreSQL is fine).
   3. Start it and choose **Join from another worker** on first run (or
      Settings > Devices > Join on the source's invite code). The join carries
      the install's legacy `INSTANCE_ID`, so the source maps the old rows to
      the new device: the sessions it ran, with their transcripts, are handed
      to the new device through the network on approval.
   4. Import the executing state the network does not carry (messages, the
      fluid node tree, session store entries, checkouts and artifact rows):
      ```sh
      python -m scripts.network_migrate import --bundle lab-sessions.json --dry-run
      python -m scripts.network_migrate import --bundle lab-sessions.json
      ```
      Imports are idempotent (`ON CONFLICT DO NOTHING`).
   5. Reconnect what is local by nature: the workspace path, and any OAuth
      bundle marked non-portable. Provider API keys arrive with the network.
4. **Verify**: every device shows Ready in Settings > Devices on every worker,
   every session lists its owner, sessions run on their owner, secrets are
   usable on their owners.
5. **Cut over**: unset `CLUSTER_SECRET` and `INSTANCE_ID` overrides. The
   `installs` table and the cluster secret are inert once every worker is
   enrolled (the runner directory comes from the network) and are removed in a
   later release.

### Rollback before cutover

Until step 5 the shared database is untouched by the joined installs (they
write only to their own databases). To roll back, point an install back at the
shared database and restart it; its old rows are still there with their
`instance_id`. Rows created on the new private database in between stay
there.

## Credentials

The source decrypts provider credentials with its data key and re-encrypts
them as network records; the receiving worker re-encrypts them under its own
data key. Windows-bound wrapping files are never copied. OAuth bundles that
cannot be reused elsewhere stay marked for reconnection on the worker that
uses them.
