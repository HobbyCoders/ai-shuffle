import { readFile } from 'node:fs/promises';
import { defineConfig } from 'vite';
import base from '../vite.config.js';
// Serves tests/chat-controls.html: the phone's chat controls (#1816) on fixture
// data, for browser review at 390x844. No request leaves the page.
export default defineConfig({ ...base, server: { host: '0.0.0.0', proxy: {}, fs: { allow: ['..'] } }, plugins: [...(base.plugins ?? []), {
 name: 'chat-controls-fixture', enforce: 'pre', configureServer(server) {
  server.middlewares.use(async (req, res, next) => {
   if (req.url?.startsWith('/api/') || req.url?.startsWith('/health')) { res.setHeader('Content-Type','application/json'); res.end('[]'); return; }
   if (req.url?.split('?')[0] !== '/mobile/tests/chat-controls.html') return next();
   try { const html = await readFile(new URL('./chat-controls.html', import.meta.url),'utf8'); res.setHeader('Content-Type','text/html'); res.end(await server.transformIndexHtml(req.url,html)); } catch(error) { next(error); }
  });
 }
}] });
