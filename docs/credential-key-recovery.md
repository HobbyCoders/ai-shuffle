# Credential keys and recovery

Local/LAN setup requires no account or password. On an empty database, Shuffle
creates a random data encryption key and keeps its wrapping key under
`DATA_DIR/.credential-keys/<database-id>.key`. PostgreSQL contains the database
identity and encrypted data key, never the wrapping key. Linux key files are
owned by the application user with mode 0600 in a 0700 directory. Windows files
are additionally protected by DPAPI for the Windows user that runs Shuffle.
Keep DATA_DIR on durable local storage; changing INSTANCE_ID does not change the
database's credential key. Remote account sign-in does not unlock encryption.

## Upgrade an existing installation

1. Take and validate a database dump and DATA_DIR backup. Stop all app writers,
   including other instances sharing the database and old password-change tools.
2. Start the new release with the existing legacy ADMIN_PASSWORD still available,
   or run `python -m scripts.credential_keys adopt` while the app is stopped and
   enter the original password at its private terminal prompt.
3. Verify `python -m scripts.credential_keys status` reports `ready: true`.
   The migration checks the encrypted values in settings, both automation
   columns, connectors, API-user credentials, provider gateway tokens, Codex
   OAuth access/refresh tokens and the OpenAI Admin key, including
   inactive/orphaned rows. Encrypted mobile key-renewal receipts are not
   checked. No credential, login hash, provider metadata or ciphertext is rewritten.
4. Make a new complete backup and encrypted portable export. Remove the legacy
   ADMIN_PASSWORD from the container configuration and verify restart. Keep the
   old password privately only while retaining older, unadopted backups.

A nonsecret pending database identity is committed before file publication. The
complete private key is flushed and published without overwrite before the
wrapped data key is committed. Interrupted adoption can be retried with the same
legacy password. An unreadable table, invalid ciphertext, wrong legacy password,
missing salt, corrupt metadata or unsafe file stops initialization; it is never
treated as an empty database. No secret writer falls back to plaintext.

## Portable recovery and another instance

Run these commands as the application's OS user with the intended DATA_DIR and
PostgreSQL configuration. They print only readiness and a database identity.
Passphrases are prompted, never accepted in command-line arguments.

In the Docker/Unraid container console, the shell may start as `root` while
Shuffle runs as `appuser`. The key owner must match the process user, so even
root receives a recovery error when reading an appuser-owned key. Run the
commands through the same user switch as the container entrypoint:

```sh
setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser \
  python -m scripts.credential_keys status
setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser \
  python -m scripts.credential_keys export /tmp/shuffle-key-recovery.json
```

An export can run while the source app is running. Keep the destination app
stopped during import. Preserve the key's ownership and permissions.

```sh
python -m scripts.credential_keys status
python -m scripts.credential_keys export /private/shuffle-key-recovery.json
# After restoring the matching database, with the destination app stopped:
python -m scripts.credential_keys import /private/shuffle-key-recovery.json
python -m scripts.credential_keys status
```

Use a strong recovery passphrase (minimum 12 characters) stored separately from
the export. The export uses scrypt and authenticated encryption. Keep it off the
original host, alongside the corresponding database backup. It is bound to the
database ID and exact wrapped data key; importing it into another database or
overwriting a different existing key is refused. Reimporting the same key is
safe. Export files are created exclusively; choose a new path for each export.

All instances using one PostgreSQL database need that same wrapping key. Before
starting an additional instance with a separate DATA_DIR, provision it using the
export/import commands. Its INSTANCE_ID must remain unique. Windows DPAPI files
cannot simply be copied to another machine or OS user; the encrypted export
provides a portable recovery path and is protected again for the destination user.
The independent `.managed-connection` key and instance identity must also be
preserved when restoring an existing managed worker.

## Missing or damaged custody

After adoption, a missing key never falls back to ADMIN_PASSWORD and never
silently creates a replacement. Stop the app and restore the matching private
key backup or import its encrypted recovery export. If a damaged file occupies
the key path, preserve it in private quarantine before importing; the tool will
not overwrite it. Restart and verify status plus saved provider access.

If both the key and its recovery export are lost, adopted credentials cannot be
recovered from a database dump alone. Retain the database and seek recovery from
another authorized instance or backup. Do not delete credential-key metadata,
change the salt, or run the legacy password rekey tool to force startup.

The migration retains the old data key, so old binaries can still decrypt old
ciphertext with the original password during a controlled rollback. They must
not change the password or rekey after adoption. Restore a coherent pre-upgrade
backup if returning to old password-management behavior. Fresh installations
have random data keys and cannot be downgraded to password-derived encryption.
