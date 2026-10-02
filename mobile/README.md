# AI Shuffle mobile UI

The phone UI the Android and iOS apps show. A SvelteKit static app that shares
`$lib` with `../frontend` (`kit.files.lib = '../frontend/src/lib'`) and adds
the mobile shell on top: `src/shell/` (AppTopBar, PageHeader, the composer's
collapse / gesture / controls sheet) and `src/mobile.css`.

Every worker serves it at `/mobile/`: the frontend build builds this app into
`frontend/build/mobile` (`frontend/scripts/build-mobile-ui.mjs`) and
`app/mobile_ui.py` serves it. The apps load `https://<paired-worker>/mobile/`,
so a change here ships with a worker update and needs no new APK or iOS build.
The app hands the page its session and the native capabilities it offers
(`window.__SHUFFLE_MOBILE__`, `frontend/src/lib/api/runtime.ts`); check
`nativeCapability()` before relying on a native feature, because the page
outlives any one app build. The Android APK also bundles one copy for its
offline demo. See `docs/mobile-desktop-ui-split.md`.

```sh
npm run deps     # links node_modules -> ../frontend/node_modules (run `npm ci` there first)
npm run dev      # http://localhost:5173/mobile/ (same /api proxy as the web UI)
npm run check
npm test
npm run build    # -> build/ ; the frontend build copies it to frontend/build/mobile
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
