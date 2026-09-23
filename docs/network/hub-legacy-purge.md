# Purging the hub's legacy sync data

The retired cloud backup ([object-sync.md](object-sync.md)) left three D1
tables in the account hub (`network_feed`, `network_objects`,
`network_object_streams`) and possibly some objects in the private R2 bucket
`shuffle-network-sync`. Migration `0017_drop_legacy_sync.sql` drops the tables,
and the same change removes the `SYNC_BUCKET` binding and the deletion guard
that blocked affected accounts.

**Order matters.** `network_objects` is the only index of what the bucket
holds. Empty the bucket first, then deploy the migration. Once the change is
on `main`, any run of the *Deploy account hub* workflow applies 0017. So either
merge only after step 4 below, or do not dispatch that workflow until the
bucket is empty.

Every command below runs from an empty scratch directory, so Wrangler looks
the database up by name instead of using the placeholder `database_id` in
`android/hub/wrangler.toml`. Set `CLOUDFLARE_API_TOKEN` and
`CLOUDFLARE_ACCOUNT_ID` for the owner's account. The token needs D1 edit and
R2 edit. Use the Wrangler version pinned in `android/hub/package.json`
(`npx --prefix <repo>/android/hub wrangler ...`).

## 1. Inventory (read-only)

```sh
sh android/hub/scripts/legacy-inventory.sh
```

It prints, without writing anything:

- which legacy tables exist;
- `network_feed` rows and bytes;
- `network_objects` rows and bytes, by status;
- `network_object_streams` rows;
- each account that holds legacy rows (these accounts could not self-delete);
- `shuffle-network-sync` object count and size (`wrangler r2 bucket info`);
- the bucket's lifecycle rules.

Run it from the repository root; it moves to its own scratch directory. Save
the output with the purge date. If every count is zero and the bucket is
empty, still take the backup in step 2, then skip to step 5.

## 2. Back up D1

```sh
wrangler d1 export shuffle-account-hub --remote --output hub-before-legacy-purge.sql
```

The export holds every account table, including password hashes and server
secret hashes. Store it encrypted, offline and under the owner's control, and
delete it once the purge has been verified and the retention period you choose
has passed. A running export blocks other database requests, so run it at a
quiet time. D1 Time Travel is a second safety net for 30 days on Workers Paid.

To keep only the legacy data instead, add
`--table network_feed --table network_objects --table network_object_streams --table networks`.

## 3. Empty the R2 bucket

Pick one of the two methods.

**Lifecycle rule (any object count).** Expire every object, and abort any
unfinished multipart upload, after one day:

```sh
wrangler r2 bucket lifecycle add shuffle-network-sync purge-legacy --expire-days 1 --abort-multipart-days 1
```

Wrangler asks you to confirm a rule with no prefix. Answer yes: the rule is
meant to cover the whole bucket, and nothing else lives in it.

Cloudflare removes existing objects within about 24 hours, sometimes longer for
large buckets. Wait, then continue to step 4.

**Delete by key (a handful of objects).** Keys are
`networks/{network_hub_id}/{object_id}`. List them from D1:

```sh
wrangler d1 execute shuffle-account-hub --remote --json \
  --command "SELECT 'networks/' || network_hub_id || '/' || object_id AS key FROM network_objects"
```

Then delete each one with `--remote` (Wrangler otherwise targets local
storage):

```sh
wrangler r2 object delete "shuffle-network-sync/networks/<network_hub_id>/<object_id>" --remote
```

The D1 rows cover every object the hub ever issued an upload grant for,
including reservations whose commit never finished.

## 4. Confirm the bucket is empty

```sh
wrangler r2 bucket info shuffle-network-sync --json
```

Continue only when the object count is 0. If objects remain, go back to step
3. Do not deploy while any object is left.

## 5. Deploy with the migration

Merge the branch, then run *Deploy account hub* from GitHub Actions with the
production `database_id`. The workflow applies pending migrations, including
0015 to 0017, before it deploys the Worker, which no longer binds
`SYNC_BUCKET`.

## 6. Verify

```sh
wrangler d1 execute shuffle-account-hub --remote --command \
  "SELECT name FROM sqlite_master WHERE name IN ('network_feed','network_objects','network_object_streams')"
```

- The result is empty.
- `GET https://hobbycoders.com/v1/health` returns `{"ok":true}`.
- A worker keeps its network status in Settings > Devices.
- For an account the inventory listed as blocked, `/delete-account` now
  completes. Tell those users or support that self-service deletion works
  again.
- The next daily usage row (`hub_usage`) shows the smaller database size.

## 7. Clean up Cloudflare

- Delete the bucket: `wrangler r2 bucket delete shuffle-network-sync`. R2
  refuses to delete a bucket that still has objects, which is one more check.
- The deploy workflow sets `keep_vars`, so the old `SYNC_R2_ACCOUNT_ID`,
  `SYNC_R2_BUCKET` and `SYNC_OBJECT_WRITES` variables stay on the Worker. Delete
  them under **Workers & Pages** > `shuffle-account-hub` > **Settings** >
  **Variables and Secrets**, along with any `SYNC_R2_*` secrets.
- Revoke any R2 API token created for the backup (**R2** > **Manage API
  tokens**).
- Once the backup from step 2 has passed its retention period, delete it.

## Rolling back

Before step 5, nothing in D1 has changed. After step 5, the dropped tables can
be restored from the step 2 export, or with D1 Time Travel
(`wrangler d1 time-travel restore`) to a point before the deploy. Neither
restores R2 objects. That is why the bucket is emptied deliberately and
checked before the migration runs.
