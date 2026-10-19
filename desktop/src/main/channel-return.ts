/**
 * Going back from nightly to stable without a reinstall, when that is safe.
 *
 * Safe means the code this install runs is not newer than the current stable
 * release: the same revision, or an earlier point of main. Every later stable
 * release is then newer code again, so migrations only ever run forwards.
 * Signed manifests order main by `container.sequence`; the desktop's own
 * `sequence` is a signing time and cannot say which code is older. An install
 * still on its installer's bundled build has no signed release, so its final
 * X.Y.Z version (a tagged stable release) stands in.
 *
 * The worker checks the same rule (app/core/stable_return.py) and hands over
 * the signed stable manifest it compared against; this re-verifies it with the
 * install's own update key before writing update-channel.json.
 *
 * Not in the launcher closure (scripts/ci/update_reach.py): the launcher's
 * SourceUpdates.setChannel still refuses every return, and only updated code
 * that imports this module takes this path.
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { CHANNEL_FILE, requireRelease, verifySourceManifest, type SourceManifest, type SourceUpdates } from './source-updates';

const FINAL = /^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$/;

export interface InstalledRelease { revision: string; container_sequence: number | null; version: string | null }

/** A signed manifest's main-history ordinal, or null when it has none. */
export function containerSequence(manifest: SourceManifest): number | null {
  const value = (manifest as { container?: { sequence?: unknown } }).container?.sequence;
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

/** What this install runs, as the worker's return check needs it. */
export async function installedRelease(updates: SourceUpdates, appVersion: string | null): Promise<InstalledRelease | null> {
  const revision = updates.version;
  if (!revision) return null;
  try {
    const manifest = await updates.release(revision);
    return { revision, container_sequence: containerSequence(manifest), version: manifest.version ?? null };
  } catch {
    // The installer's bundled build has no signed release.json.
    return { revision, container_sequence: null, version: revision === updates.options.baseline?.revision ? appVersion : null };
  }
}

function final(version: string | null | undefined): number[] | null {
  const match = FINAL.exec(version ?? '');
  return match ? match.slice(1).map(Number) : null;
}

/** Null when `installed` is not ahead of `stable`; otherwise why the switch is refused. */
export function stableReturnRefusal(installed: InstalledRelease, stable: SourceManifest): string | null {
  if (installed.revision === stable.revision) return null;
  const release = stable.version ? ` (${stable.version})` : '';
  const ahead = `This app runs code that is newer than the current stable release${release}, so it cannot switch back to stable yet: nightly may already have changed how its data is stored. It can switch once a stable release catches up with the version it runs.`;
  const theirs = containerSequence(stable);
  if (installed.container_sequence !== null && theirs !== null) return installed.container_sequence < theirs ? null : ahead;
  const a = final(installed.version), b = final(stable.version);
  if (a && b) {
    for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] < b[i] ? null : ahead;
    return null;
  }
  return `Cannot tell whether this app's version is older than the current stable release${release}, so it stays on nightly. Install the next update, then try again.`;
}

/** Writes {"channel": "stable"} once the signed stable release proves it safe; throws why not. */
export async function returnToStable(updates: SourceUpdates, signedStable: string, appVersion: string | null): Promise<void> {
  if (updates.options.channel) throw new Error('Channel cannot be changed');
  if (await updates.channel() === 'stable') return;
  if (updates.pendingVersion || updates.hasMigrationRisk) throw new Error('Finish the current update before changing channel');
  const stable = requireRelease(verifySourceManifest(signedStable, updates.options.publicKey), 'stable');
  const installed = await installedRelease(updates, appVersion);
  if (!installed) throw new Error('Cannot tell which version this app runs, so it stays on nightly.');
  const refusal = stableReturnRefusal(installed, stable);
  if (refusal) throw new Error(refusal);
  // As SourceUpdates.setChannel writes it.
  await fs.mkdir(updates.options.root, { recursive: true });
  const file = path.join(updates.options.root, CHANNEL_FILE);
  await fs.writeFile(file + '.tmp', JSON.stringify({ channel: 'stable' }), { mode: 0o600 });
  await fs.rename(file + '.tmp', file);
}
