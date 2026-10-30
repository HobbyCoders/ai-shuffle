/** Optional signed desktop+phone assets; source verification is independent. */
import { createHash } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { extractSource, treeDigest } from './source-archive';

export interface UIBundle {
  format: 1; revision: string; version: string; sha256: string; size: number; tree: string;
}
interface UIRelease { revision: string; version?: string; ui?: UIBundle }

/** Dedicated anonymous artifact transport. Never broaden the hub URL allowlist. */
export async function publicUIFetch(input: string | URL | Request): Promise<Response> {
  let url = new URL(String(input));
  if (url.origin !== 'https://github.com' || !/^\/HobbyCoders\/ai-shuffle\/releases\/download\/desktop-sources\/[a-f0-9]{40}-[a-f0-9]{64}-ui\.zip$/.test(url.pathname) || url.search || url.hash || url.username || url.password) throw new Error('Invalid UI asset URL');
  const signal = AbortSignal.timeout(120_000);
  for (let count = 0; count < 6; count++) {
    const response = await fetch(url, { method: 'GET', redirect: 'manual', credentials: 'omit', signal });
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      const next = new URL(response.headers.get('location') ?? '', url);
      if (next.protocol !== 'https:' || next.username || next.password || next.port ||
          !['github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'].includes(next.hostname)) throw new Error('Invalid UI asset redirect');
      await response.body?.cancel();
      url = next;
    } else return response;
  }
  throw new Error('Too many UI asset redirects');
}

export async function prepareUIBundle(stage: string, manifest: UIRelease, request: typeof fetch = publicUIFetch): Promise<string | undefined> {
  if (['0', 'false', 'off'].includes((process.env.AI_SHUFFLE_PERF_PREBUILT_UI ?? '').trim().toLowerCase()) || !manifest.ui) return undefined;
  const archive = path.join(stage, 'ui.zip'), target = path.join(stage, 'ui');
  try {
    const ui = manifest.ui;
    if (ui.format !== 1 || ui.revision !== manifest.revision || !manifest.version || ui.version !== manifest.version ||
        !/^[a-f0-9]{64}$/.test(ui.sha256) || !/^[a-f0-9]{64}$/.test(ui.tree) || !Number.isSafeInteger(ui.size) || ui.size <= 0 || ui.size > 64 * 1024 ** 2) throw new Error('Invalid UI bundle metadata');
    const url = `https://github.com/HobbyCoders/ai-shuffle/releases/download/desktop-sources/${manifest.revision}-${ui.sha256}-ui.zip`;
    const response = await request(url, { credentials: 'omit', signal: AbortSignal.timeout(120_000) });
    if (!response.ok || !response.body) throw new Error('UI bundle unavailable');
    const file = await fs.open(archive, 'wx'), hash = createHash('sha256'); let size = 0;
    try {
      for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
        size += chunk.length;
        if (size > ui.size) throw new Error('UI bundle exceeds signed size');
        hash.update(chunk); await file.writeFile(chunk);
      }
    } finally { await file.close(); }
    if (size !== ui.size || hash.digest('hex') !== ui.sha256) throw new Error('UI bundle checksum mismatch');
    const files = await extractSource(archive, target, { files: 20_000, expanded: 256 * 1024 ** 2 });
    const prefix = `ai-shuffle-${manifest.revision}/`;
    if (files.some(([name]) => !name.startsWith(prefix))) throw new Error('UI bundle revision mismatch');
    if (treeDigest(files.map(([name, digest]) => [name.slice(prefix.length), digest])) !== ui.tree) throw new Error('UI bundle tree mismatch');
    const root = path.join(target, `ai-shuffle-${manifest.revision}`);
    const stamp = JSON.parse(await fs.readFile(path.join(root, 'ui-build.json'), 'utf8'));
    if (stamp.format !== 1 || stamp.revision !== manifest.revision || stamp.version !== manifest.version || Object.keys(stamp).length !== 3) throw new Error('UI bundle build constants mismatch');
    for (const name of ['index.html', 'mobile/index.html']) if (!(await fs.stat(path.join(root, name))).isFile()) throw new Error('UI shell missing');
    return root;
  } catch {
    await fs.rm(target, { recursive: true, force: true });
    console.warn('Prebuilt UI unavailable or invalid; building the UI from verified source.');
    return undefined;
  } finally { await fs.rm(archive, { force: true }); }
}
