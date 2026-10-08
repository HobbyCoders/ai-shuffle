import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { posix } from 'node:path';
import { defineConfig } from 'vite';
import base from '../vite.config.js';
// Serves tests/one-bar.html: the phone's one-bar header on fixture data, for
// browser review at 360x800 and 412x915. No request leaves the page. The git
// stores are frontend/tests/git-panel-stubs.ts, as in the git-panel fixture.
const stubs = fileURLToPath(new URL('../../frontend/tests/git-panel-stubs.ts', import.meta.url));
const stubbed = ['/src/lib/stores/git.svelte', '/src/lib/stores/gitOperations.svelte', '/src/lib/stores/github.svelte'];
export default defineConfig({ ...base, optimizeDeps: { ...(base.optimizeDeps ?? {}), entries: [] }, server: { host: '0.0.0.0', proxy: {}, fs: { allow: ['..'] } }, plugins: [...(base.plugins ?? []), {
 name: 'one-bar-fixture', enforce: 'pre',
 resolveId(id, importer) {
  const from = importer?.replace(/\\/g, '/').split('?')[0] ?? '';
  if (from.endsWith('/tests/git-panel-stubs.ts')) return null;
  // Only the Git button and its panel read the stubs; the rest of the bar
  // (drawer, session list) keeps the real stores.
  if (!from.includes('/components/git/') && !from.endsWith('/tests/one-bar.ts')) return null;
  let path = id.replace(/\\/g, '/').replace(/^\$lib/, '/frontend/src/lib');
  if (path.startsWith('.') && from) path = posix.join(posix.dirname(from), path);
  path = path.replace(/\.(ts|js)$/, '');
  return stubbed.some((tail) => path.endsWith(tail)) ? stubs : null;
 },
 configureServer(server) {
  server.middlewares.use(async (req, res, next) => {
   if (req.url?.startsWith('/api/') || req.url?.startsWith('/health')) { res.setHeader('Content-Type','application/json'); res.end('[]'); return; }
   if (req.url?.split('?')[0] !== '/mobile/tests/one-bar.html') return next();
   try { const html = await readFile(new URL('./one-bar.html', import.meta.url),'utf8'); res.setHeader('Content-Type','text/html'); res.end(await server.transformIndexHtml(req.url,html)); } catch(error) { next(error); }
  });
 }
}] });
