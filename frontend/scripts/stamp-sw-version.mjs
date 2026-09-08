#!/usr/bin/env node
/**
 * Stamp the service worker CACHE_VERSION so every build busts the SW cache.
 *
 * Runs as the npm "postbuild" step (after `vite build`). Rewrites the
 * `const CACHE_VERSION = ...;` line in build/sw.js (the build OUTPUT copy,
 * not the tracked static/sw.js source) with a value derived from
 * package.json version + git short SHA, e.g. "0.8.5-a2850458". If any single
 * build changes app code, the SHA changes → CACHE_VERSION changes → clients
 * drop the old cache on activate.
 *
 * Pure Node (no bash) so it works identically on Windows, Linux, and in the
 * Docker frontend-builder stage. Falls back to the package version plus a
 * short timestamp if git is unavailable (e.g. a source tarball with no .git),
 * so the stamp is still unique per build.
 *
 * Stamping happens post-build, in build/sw.js only. This keeps the tracked
 * frontend/static/sw.js source file untouched (it stays at its committed
 * 'dev' placeholder) so `git status` is clean after every build. Deploy
 * scripts that copy frontend/build/* to app/static still ship the real
 * stamped value to production unchanged.
 */
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(__dirname, '..');
const swPath = join(frontendRoot, 'build', 'sw.js');
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
  if (!existsSync(swPath)) {
    console.error(
      `[stamp-sw] ${swPath} not found — expected the build output copy of sw.js. ` +
        'Did `vite build` run (and copy static/ into build/) before this postbuild step?'
    );
    process.exit(1);
  }

  const pkg = JSON.parse(readFileSync(pkgPath, 'utf8'));
  const version = pkg.version || '0.0.0';
  const sha = shortSha();
  const stamp = `${version}-${sha}`;

  let sw = readFileSync(swPath, 'utf8');
  const re = /const CACHE_VERSION = .*;/;
  if (!re.test(sw)) {
    console.error(`[stamp-sw] CACHE_VERSION line not found in ${swPath}`);
    process.exit(1);
  }
  sw = sw.replace(re, `const CACHE_VERSION = '${stamp}';`);
  writeFileSync(swPath, sw);
  console.log(`[stamp-sw] CACHE_VERSION stamped to ${stamp} in build/sw.js`);
}

main();
