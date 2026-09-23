# Object sync (retired)

The optional encrypted cloud backup ("object sync protocol v2") has been
removed. Nothing in this repository writes objects to R2 any more. Worker sync
is [direct peer exchange](peer-first.md) only. This page records what the
backup stored, and what is left in the account hub, because account deletion
depends on it.

## History

- **#1201, merged 2026-09-11.** Workers uploaded gzip bundles of end-to-end
  encrypted operation envelopes, file parts and recovery checkpoints to the
  private R2 Standard bucket `shuffle-network-sync`. The account hub never
  wrote objects itself. For each object it first inserted a `reserved` row
  into D1 `network_objects`, then issued a presigned, create-only PUT grant
  that expired within 20 seconds, and marked the row `committed` after
  verifying the upload. The hub never implemented a remote delete.
- **Object key:** `networks/{network_hub_id}/{object_id}`, where
  `network_hub_id` is `networks.id` in D1 and `object_id` is the row's
  `object_id`. Every object the hub could have received therefore has a
  `network_objects` row naming its exact key.
- **#1264, merged 2026-09-13.** Removed the hub's object and feed routes and
  the worker's uploader, downloader, cloud restore and checkpoint code.
  Historical objects and the D1 tables were deliberately left in place. The
  hub test suite checks that `/objects/prepare`, `/ops` and `/feed` return 404.
- **Deployments.** `deploy-hub.yml` runs from 2026-09-11 04:40 UTC through
  2026-09-11 21:59 UTC used `object_writes: true`. The next hub deploy was on
  2026-09-15 and shipped #1264's removal. Object writes were therefore
  switched on for the live hub between those two deploys, if the bucket-scoped
  `SYNC_R2_*` Worker secrets were also installed. Whether any worker actually
  uploaded depends on production state that the repository cannot show: a
  worker with backup enabled, and those secrets.

What remains today:

- D1 tables `network_objects` and `network_object_streams`, from migration
  0012, possibly holding rows from that window.
- The `SYNC_BUCKET` R2 binding and `SYNC_R2_*` variables in `deploy-hub.yml`.
  No hub code reads them. The binding is kept so that a future purge of
  historical objects can use it. The `object_writes` input no longer has any
  effect.
- Any objects already in `shuffle-network-sync`.

## Account deletion

Self-service deletion (`android/hub/src/account-deletion.ts`) refuses an
account that owns any `network_objects` row, whether reserved or committed. It
refuses before revoking anything. A queued deletion stays pending, with its
descriptors kept, if such a row appears during cleanup. Deleting the D1 rows
alone would orphan the objects and remove the only index of their keys, so the
guard stays until a purge exists that deletes the bytes and confirms that none
remain.

Affected users see a message saying their account still holds copies from the
retired cloud backup, that nothing has been deleted, and that they should
contact support. Accounts without such rows are not affected.

## If historical objects exist

To find out, the owner can run a read-only count of `network_objects` rows in
production D1, and check the object count of `shuffle-network-sync` in the
Cloudflare dashboard. If both are zero, the guard can never trigger.

If objects do exist, a purge can work from D1 alone without guessing keys:

1. For each of the account's `networks.id`, list the bucket with the exact
   prefix `networks/{networks.id}/`. That prefix belongs to one account's
   network.
2. Delete the listed objects and the keys named by the account's
   `network_objects` rows. The rows cover reserved objects whose upload
   finished but whose commit never did. No upload grant can still be
   outstanding: grants lasted 20 seconds and the issuing routes are gone.
3. List the prefix again and require it to be empty.
4. Only then delete the D1 rows.
5. Stay retryable: on any failure, keep the rows and leave the deletion
   pending.

Never delete the whole bucket or a prefix outside the account's own networks.
A purge also needs Worker tests with an R2 test binding, covering unrelated
accounts, partial failures and read-back, before the guard is removed.
