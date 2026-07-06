#!/usr/bin/env node
/**
 * Stamp the service worker CACHE_VERSION so every build busts the SW cache.
 *
 * Runs as the npm "prebuild" step (before `vite build`). Rewrites the
 * `const CACHE_VERSION = ...;` line in static/sw.js with a value derived from
 * package.json version + git short SHA, e.g. "1.2.1-a2850458". If any single
 * build changes app code, the SHA changes → CACHE_VERSION changes → clients
 * drop the old cache on activate.
 *
 * Pure Node (no bash) so it works identically on Windows, Linux, and in the
 * Docker frontend-builder stage. Never fails the build: if git is unavailable
 * (e.g. a source tarball with no .git), it falls back to the package version
 * plus a short timestamp so the value is still unique per build.
 *
 * NOTE: the committed static/sw.js keeps its pinned integer default; this
 * script overwrites it only in the working tree during a build. Do not commit
 * the stamped value — it is build output.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(__dirname, '..');
const swPath = join(frontendRoot, 'static', 'sw.js');
const pkgPath = join(frontendRoot, 'package.json');

function shortSha() {
  try {
    return execSync('git rev-parse --short HEAD', {
      cwd: frontendRoot,
      stdio: ['ignore', 'pipe', 'ignore'],
    })
      .toString()
      .trim();
  } catch {
    // No git (source tarball / detached export): fall back to a timestamp so
    // the stamp is still unique per build.
    return `t${Date.now().toString(36)}`;
  }
}

function main() {
  const pkg = JSON.parse(readFileSync(pkgPath, 'utf8'));
  const version = pkg.version || '0.0.0';
  const sha = shortSha();
  const stamp = `${version}-${sha}`;

  let sw = readFileSync(swPath, 'utf8');
  const re = /const CACHE_VERSION = .*;/;
  if (!re.test(sw)) {
    console.error('[stamp-sw] CACHE_VERSION line not found in sw.js — skipping stamp');
    process.exit(0); // never fail the build over a missing stamp
  }
  sw = sw.replace(re, `const CACHE_VERSION = '${stamp}';`);
  writeFileSync(swPath, sw);
  console.log(`[stamp-sw] CACHE_VERSION stamped to ${stamp}`);
}

main();
