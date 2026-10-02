import { readFile } from 'node:fs/promises';
import { defineConfig } from 'vite';
import base from '../vite.config.js';
export default defineConfig({ ...base, server: { proxy: {}, fs: { allow: ['..'] } }, plugins: [...(base.plugins ?? []), {
 name: 'composer-gesture-fixture', enforce: 'pre', configureServer(server) {
  // Never forward fixture API traffic to the configured application backend.
  server.middlewares.use(async (req, res, next) => {
   if (req.url?.startsWith('/api/') || req.url?.startsWith('/health')) { res.setHeader('Content-Type','application/json'); res.end('[]'); return; }
   // Under the app's base path (kit.paths.base) so `./composer-gesture.ts` resolves through Vite.
   if (req.url?.split('?')[0] !== '/mobile/tests/composer-gesture.html') return next();
   try { const html = await readFile(new URL('./composer-gesture.html', import.meta.url),'utf8'); res.setHeader('Content-Type','text/html'); res.end(await server.transformIndexHtml(req.url,html)); } catch(error) { next(error); }
  });
 }
}] });
