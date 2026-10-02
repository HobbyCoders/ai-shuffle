# AI Shuffle mobile UI

The mobile UI the Android app bundles. A SvelteKit static app that shares
`$lib` with `../frontend` (`kit.files.lib = '../frontend/src/lib'`) and adds
the mobile shell on top: `src/shell/` (AppTopBar, PageHeader, the composer's
collapse / gesture / controls sheet) and `src/mobile.css`.

It never comes from a worker. The app serves it from its own assets at
`https://appassets.androidplatform.net/assets/mobile-ui/` and talks to the
paired worker only through its HTTP/WebSocket APIs
(`frontend/src/lib/api/runtime.ts`). See `docs/mobile-desktop-ui-split.md`.

```sh
npm run deps     # links node_modules -> ../frontend/node_modules (run `npm ci` there first)
npm run dev      # http://localhost:5173/assets/mobile-ui/ (same /api proxy as the web UI)
npm run check
npm test
npm run build    # -> build/ ; android/scripts/sync-mobile-ui.sh copies it into the APK
```

## Browser regression for the composer gesture

`tests/composer-gesture.html` mounts the shared composer inside the mobile
shell with isolated transports; `tests/verify-composer-gesture.mjs` drives it
with Playwright (touch swipes in both layouts and both sheet states, native
draft scrolling, the controls drawer, the context row geometry, 44px composer
touch targets and 16px editor text, the shorter placeholder at 360px and
below, a call from a fresh composer creating its session before the call
starts, and the explanation when another device is picked as the runner). Needs
`browser-mcp`'s `playwright-core` (`cd ../browser-mcp && npm ci`) and a
Chromium binary.

```sh
npx vite dev --config tests/vite.composer-gesture.config.js --port 5181 --strictPort
CHROMIUM_PATH=/usr/bin/chromium node tests/verify-composer-gesture.mjs
```
