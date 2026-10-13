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

## Purge

The owner decided to purge what the backup left behind. The runbook is
[the cloud purge runbook](https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/docs/network/hub-legacy-purge.md):
[inventory](https://github.com/HobbyCoders/hobbycoders-cloud/blob/main/hub/scripts/legacy-inventory.sh), back up D1, empty the
`shuffle-network-sync` bucket, then deploy migration
`0017_drop_legacy_sync.sql`, which drops `network_feed`, `network_objects` and
`network_object_streams`. The same change removed the `SYNC_BUCKET` binding,
the `SYNC_R2_*` variables and the `object_writes` input from `deploy-hub.yml`.

The bucket must be empty before 0017 deploys: `network_objects` is the only
index of the bucket's keys.

## Account deletion

Until that change, self-service deletion refused any account that owned a
`network_objects` row, because deleting the rows would have orphaned the
objects they index. With the bucket emptied and the tables dropped, the guard
is gone and every account can delete itself again. Accounts the inventory
lists as holding legacy rows were the ones affected; support can tell them
self-service deletion works once the purge is verified.
