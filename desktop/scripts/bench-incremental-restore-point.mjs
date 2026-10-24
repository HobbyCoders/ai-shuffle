#!/usr/bin/env node
/** Synthetic-only, single-stream I/O benchmark. Never accepts an existing data/cluster path. Build desktop first. */
import { promises as fs } from 'node:fs';
import { randomFillSync } from 'node:crypto';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
const require = createRequire(import.meta.url);
const { SinglePassClusterCopy } = require('../dist/main/update-restore-point.js');
const { IncrementalClusterCopy } = require('../dist/main/incremental-restore-point.js');
const { ColdClusterCopy } = require('../dist/main/database-restore.js');
const mib = Number(process.argv[2] ?? 3072);
if (!Number.isSafeInteger(mib) || mib < 256 || mib > 8192) throw Error('Synthetic size must be 256..8192 MiB');
const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-update-bench-'));
const block = randomFillSync(Buffer.alloc(1024 * 1024));
const result = { platform: process.platform, node: process.version, syntheticMiB: mib, segmentMiB: 64, phases: [] };
const stop = async () => {}; // fake stopped cluster only; no processes or real PostgreSQL
async function write(file, size) {
  const fd = await fs.open(file, 'w');
  try { for (let i = 0; i < size; i++) await fd.write(block); await fd.sync(); }
  finally { await fd.close(); }
}
async function measure(name, copy) {
  let start = performance.now();
  await copy.snapshot();
  const appMs = performance.now() - start;
  start = performance.now();
  await new ColdClusterCopy(root, stop).validate();
  const launcherMs = performance.now() - start;
  start = performance.now();
  await copy.discard();
  const cleanupMs = performance.now() - start;
  result.phases.push({ name, appMs, launcherMs, totalBeforeSelectMs: appMs + launcherMs, cleanupMs });
}
try {
  await fs.mkdir(path.join(root, 'data/global'), { recursive: true });
  await fs.mkdir(path.join(root, 'data/base/1'), { recursive: true });
  await fs.mkdir(path.join(root, 'data/pg_wal'));
  await fs.writeFile(path.join(root, 'data/PG_VERSION'), '16');
  await fs.writeFile(path.join(root, 'data/global/pg_control'), 'synthetic stopped control');
  for (let remaining = mib, i = 0; remaining > 0; i++) {
    const size = Math.min(64, remaining);
    await write(path.join(root, 'data/base/1', String(i)), size); remaining -= size;
  }
  await measure('before: single-pass app + full launcher validation', new SinglePassClusterCopy(root, stop));
  await measure('after: first incremental point (cold cache)', new IncrementalClusterCopy(root, stop));
  await measure('after: unchanged cluster (warm cache)', new IncrementalClusterCopy(root, stop));
  await write(path.join(root, 'data/base/1/0'), 64);
  await write(path.join(root, 'data/pg_wal/new-wal'), 16);
  await measure('after: 64 MiB relation changed + 16 MiB new WAL', new IncrementalClusterCopy(root, stop));
  console.log(JSON.stringify(result, null, 2));
} finally {
  await fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}
